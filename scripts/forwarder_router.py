#!/usr/bin/env python3
"""
Forwarder Router (forwarder_router.py)

Configurable routing core for the forwarder.

Implements the route = Qx + Mx -> Ax + Ex abstraction:
  Qx = RabbitMQ source / queue section
  Mx = message profile / matching rule
  Ax = API definition
  Ex = endpoint / request definition

Design constraints:
  - No fan-out: one message selects at most one route.
  - Ambiguous matches at the same priority are configuration errors.
  - Routes are evaluated by descending priority.
"""

import copy
import json
import logging
import re
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple, Union


logger = logging.getLogger("forwarder")

# ---------------------------------------------------------------------------
# Valid policy values (only the defaults implemented in Step 1)
# ---------------------------------------------------------------------------

VALID_NO_MATCH_POLICIES = frozenset({"ack_warn_no_forward", "ack_silent_no_forward"})
VALID_AMBIGUOUS_MATCH_POLICIES = frozenset({"ack_error_no_forward"})
VALID_HTTP_FAILURE_POLICIES = frozenset({"ack_error_no_requeue"})

DEFAULT_NO_MATCH_POLICY = "ack_warn_no_forward"
DEFAULT_AMBIGUOUS_MATCH_POLICY = "ack_error_no_forward"
DEFAULT_HTTP_FAILURE_POLICY = "ack_error_no_requeue"
DEFAULT_MISSING_MESSAGE_DRY_RUN = True
DEFAULT_ROUTE_PRIORITY = 0


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class RoutingConfigError(Exception):
    """Raised when the routing configuration is invalid."""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class MessageProfile:
    """A configurable matching rule for incoming messages."""

    name: str
    message_type: Optional[str] = None
    asset_types: Optional[List[str]] = None
    asset_ids: Optional[List[str]] = None
    command_types: Optional[List[str]] = None

    def matches(self, message: dict) -> bool:
        """Return True if the message satisfies all configured filters.

        Each filter that is not None must match; None means wildcard.
        For ``command_types``, the message field ``command_type`` is tried
        first, falling back to ``command``.
        """
        if self.message_type is not None:
            if message.get("message_type") != self.message_type:
                return False

        if self.asset_types is not None:
            if message.get("asset_type") not in self.asset_types:
                return False

        if self.asset_ids is not None:
            if message.get("asset_id") not in self.asset_ids:
                return False

        if self.command_types is not None:
            cmd = message.get("command_type") or message.get("command")
            if cmd not in self.command_types:
                return False

        return True

    @property
    def specificity(self) -> int:
        """Return a specificity score; higher is more specific.

        Weights: asset_ids (8) > asset_types (4) > command_types (2)
                 > message_type (1).
        """
        score = 0
        if self.message_type is not None:
            score += 1
        if self.command_types is not None:
            score += 2
        if self.asset_types is not None:
            score += 4
        if self.asset_ids is not None:
            score += 8
        return score

    @classmethod
    def from_dict(cls, name: str, data: dict) -> "MessageProfile":
        return cls(
            name=name,
            message_type=data.get("message_type"),
            asset_types=data.get("asset_types"),
            asset_ids=data.get("asset_ids"),
            command_types=data.get("command_types"),
        )


@dataclass
class ApiConfig:
    """API server definition."""

    name: str
    base_url: str = ""
    reference_api: Optional[str] = None
    base_url_key: Optional[str] = None
    user: Optional[str] = None
    password: Optional[str] = None
    timeout: float = 10.0
    retries: int = 3
    verify_ssl: bool = False

    @classmethod
    def from_dict(cls, name: str, data: dict) -> "ApiConfig":
        return cls(
            name=name,
            base_url=data.get("base_url", ""),
            reference_api=data.get("reference_api"),
            base_url_key=data.get("base_url_key"),
            user=data.get("user"),
            password=data.get("password"),
            timeout=float(data.get("timeout", 10.0)),
            retries=int(data.get("retries", 3)),
            verify_ssl=data.get("verify_ssl", False),
        )


@dataclass
class EndpointConfig:
    """Endpoint / request definition."""

    name: str
    method: str = "POST"
    path_template: str = ""
    body_template: Optional[Any] = None
    body_mode: Optional[str] = None
    headers: Optional[Dict[str, str]] = None
    success_status_codes: Optional[List[int]] = None

    @classmethod
    def from_dict(cls, name: str, data: dict) -> "EndpointConfig":
        return cls(
            name=name,
            method=data.get("method", "POST"),
            path_template=data.get("path_template", ""),
            body_template=data.get("body_template"),
            body_mode=data.get("body_mode"),
            headers=data.get("headers"),
            success_status_codes=data.get("success_status_codes"),
        )


@dataclass
class RouteConfig:
    """A route connecting source + message profile -> API + endpoint."""

    name: str
    source: str
    message_profile: str
    api: str
    endpoint: str
    priority: int = DEFAULT_ROUTE_PRIORITY
    enabled: bool = True
    dry_run: Optional[bool] = None

    @classmethod
    def from_dict(cls, data: dict) -> "RouteConfig":
        return cls(
            name=data["name"],
            source=data["source"],
            message_profile=data["message_profile"],
            api=data["api"],
            endpoint=data["endpoint"],
            priority=data.get("priority", DEFAULT_ROUTE_PRIORITY),
            enabled=data.get("enabled", True),
            dry_run=data.get("dry_run"),
        )


@dataclass
class RouteResolutionResult:
    """Result of route resolution."""

    status: str  # "matched" | "no_match" | "ambiguous"
    route: Optional[RouteConfig] = None
    matching_routes: List[RouteConfig] = field(default_factory=list)
    reason: str = ""


@dataclass
class RoutingDefaults:
    """Routing policy defaults."""

    on_no_match: str = DEFAULT_NO_MATCH_POLICY
    on_ambiguous_match: str = DEFAULT_AMBIGUOUS_MATCH_POLICY
    on_http_failure: str = DEFAULT_HTTP_FAILURE_POLICY
    missing_message_dry_run_default: bool = DEFAULT_MISSING_MESSAGE_DRY_RUN

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "RoutingDefaults":
        if not data:
            return cls()
        return cls(
            on_no_match=data.get("on_no_match", DEFAULT_NO_MATCH_POLICY),
            on_ambiguous_match=data.get(
                "on_ambiguous_match", DEFAULT_AMBIGUOUS_MATCH_POLICY
            ),
            on_http_failure=data.get("on_http_failure", DEFAULT_HTTP_FAILURE_POLICY),
            missing_message_dry_run_default=data.get(
                "missing_message_dry_run_default", DEFAULT_MISSING_MESSAGE_DRY_RUN
            ),
        )


# ---------------------------------------------------------------------------
# Dry-run resolution
# ---------------------------------------------------------------------------

def resolve_effective_dry_run(
    forwarder_dry_run: bool,
    route_dry_run: Optional[bool],
    message_dry_run: Optional[bool],
    missing_message_dry_run_default: bool = DEFAULT_MISSING_MESSAGE_DRY_RUN,
) -> bool:
    """Compute effective dry-run with a clear priority cascade.

    Priority (highest wins):
      1. *forwarder_dry_run* — global CLI/env kill switch; always wins.
      2. *route_dry_run* — explicit per-route override (True=dry-run,
         False=live).  ``None`` means "defer to message".
      3. *message_dry_run* — per-message payload flag.
      4. *missing_message_dry_run_default* — fallback when neither route
         nor message specifies a value.
    """
    if forwarder_dry_run:
        return True

    if route_dry_run is not None:
        return route_dry_run

    if message_dry_run is not None:
        return message_dry_run

    return missing_message_dry_run_default


# ---------------------------------------------------------------------------
# Config mode detection
# ---------------------------------------------------------------------------

def detect_config_mode(config: dict) -> str:
    """Detect whether *config* uses legacy targets or v2 routing.

    Returns ``"legacy"`` or ``"v2"``.

    Raises :class:`RoutingConfigError` on mutually-exclusive or
    inconsistent combinations.
    """
    has_targets = "targets" in config
    has_routes = "routes" in config
    version = config.get("version")

    if has_targets and has_routes:
        raise RoutingConfigError(
            "Configuration contains both 'targets' and 'routes'; "
            "these are mutually exclusive"
        )

    if has_routes:
        return "v2"

    if version == 2 and not has_routes:
        raise RoutingConfigError(
            "Configuration has version: 2 but no 'routes' section"
        )

    return "legacy"


# ---------------------------------------------------------------------------
# Config validation (v2 only)
# ---------------------------------------------------------------------------

_KNOWN_TOP_LEVEL_KEYS = frozenset({
    "version", "sources", "message_profiles", "apis", "endpoints",
    "routes", "on_no_match", "on_ambiguous_match", "on_http_failure",
    "defaults", "comment",
})

_KNOWN_SOURCE_KEYS = frozenset({"section", "comment"})

_KNOWN_PROFILE_KEYS = frozenset({
    "message_type", "asset_types", "asset_ids", "command_types", "comment",
})

_KNOWN_API_KEYS = frozenset({
    "base_url", "reference_api", "base_url_key", "user", "password",
    "timeout", "retries", "verify_ssl", "comment",
})

_KNOWN_ENDPOINT_KEYS = frozenset({
    "path_template", "method", "body_mode", "body_template",
    "headers", "success_status_codes", "comment",
})

_KNOWN_ROUTE_KEYS = frozenset({
    "name", "source", "message_profile", "api", "endpoint",
    "priority", "enabled", "dry_run", "comment",
})

_KNOWN_DEFAULTS_KEYS = frozenset({
    "on_no_match", "on_ambiguous_match", "on_http_failure",
    "missing_message_dry_run_default", "comment",
})

_ENDPOINT_FIELD_SUGGESTIONS: Dict[str, str] = {
    "path": "path_template",
    "url": "path_template",
    "template": "path_template",
    "status_codes": "success_status_codes",
    "body": "body_template",
}


def _check_unknown_fields(
    data: dict,
    allowed: frozenset,
    section_type: str,
    item_name: str,
    errors: List[str],
    suggestions: Optional[Dict[str, str]] = None,
) -> None:
    """Append errors for any keys in *data* not in *allowed*."""
    for key in data:
        if key not in allowed:
            hint = ""
            if suggestions and key in suggestions:
                hint = f". Did you mean '{suggestions[key]}'?"
            errors.append(
                f"Unknown {section_type} field '{key}' "
                f"in {section_type} '{item_name}'{hint}"
            )


def validate_routing_config(config: dict) -> dict:
    """Validate a v2 routing configuration.

    Returns a normalised dict with keys:
    ``defaults``, ``sources``, ``message_profiles``, ``apis``,
    ``endpoints``, ``routes``.

    Raises :class:`RoutingConfigError` on any validation failure.
    """
    errors: List[str] = []

    # -- defaults / policies -------------------------------------------------
    defaults_data = config.get("defaults", {})
    if isinstance(defaults_data, dict):
        _check_unknown_fields(
            defaults_data, _KNOWN_DEFAULTS_KEYS, "defaults", "defaults", errors
        )
    defaults = RoutingDefaults.from_dict(defaults_data)

    if defaults.on_no_match not in VALID_NO_MATCH_POLICIES:
        errors.append(
            f"Invalid on_no_match policy: '{defaults.on_no_match}'; "
            f"valid: {sorted(VALID_NO_MATCH_POLICIES)}"
        )
    if defaults.on_ambiguous_match not in VALID_AMBIGUOUS_MATCH_POLICIES:
        errors.append(
            f"Invalid on_ambiguous_match policy: '{defaults.on_ambiguous_match}'; "
            f"valid: {sorted(VALID_AMBIGUOUS_MATCH_POLICIES)}"
        )
    if defaults.on_http_failure not in VALID_HTTP_FAILURE_POLICIES:
        errors.append(
            f"Invalid on_http_failure policy: '{defaults.on_http_failure}'; "
            f"valid: {sorted(VALID_HTTP_FAILURE_POLICIES)}"
        )

    # -- named collections ---------------------------------------------------
    sources = config.get("sources", {})
    if not isinstance(sources, dict):
        errors.append("'sources' must be an object")
        sources = {}
    else:
        for src_name, src_data in sources.items():
            if isinstance(src_data, dict):
                _check_unknown_fields(
                    src_data, _KNOWN_SOURCE_KEYS, "source", src_name, errors
                )

    profiles_data = config.get("message_profiles", {})
    if not isinstance(profiles_data, dict):
        errors.append("'message_profiles' must be an object")
        profiles_data = {}
    else:
        for prof_name, prof_data in profiles_data.items():
            if isinstance(prof_data, dict):
                _check_unknown_fields(
                    prof_data, _KNOWN_PROFILE_KEYS,
                    "message_profile", prof_name, errors,
                )
    profiles = {
        name: MessageProfile.from_dict(name, pdata)
        for name, pdata in profiles_data.items()
    }

    apis_data = config.get("apis", {})
    if not isinstance(apis_data, dict):
        errors.append("'apis' must be an object")
        apis_data = {}
    else:
        for api_name, api_data in apis_data.items():
            if isinstance(api_data, dict):
                _check_unknown_fields(
                    api_data, _KNOWN_API_KEYS, "api", api_name, errors
                )
    apis = {
        name: ApiConfig.from_dict(name, adata) for name, adata in apis_data.items()
    }

    endpoints_data = config.get("endpoints", {})
    if not isinstance(endpoints_data, dict):
        errors.append("'endpoints' must be an object")
        endpoints_data = {}
    else:
        for ep_name, ep_data in endpoints_data.items():
            if isinstance(ep_data, dict):
                _check_unknown_fields(
                    ep_data, _KNOWN_ENDPOINT_KEYS,
                    "endpoint", ep_name, errors,
                    suggestions=_ENDPOINT_FIELD_SUGGESTIONS,
                )
    endpoints = {
        name: EndpointConfig.from_dict(name, edata)
        for name, edata in endpoints_data.items()
    }

    # -- routes --------------------------------------------------------------
    routes_data = config.get("routes", [])
    if not isinstance(routes_data, list):
        errors.append("'routes' must be a list")
        routes_data = []

    routes: List[RouteConfig] = []
    route_names_seen: set = set()

    for idx, rdata in enumerate(routes_data):
        if not isinstance(rdata, dict):
            errors.append(f"Route at index {idx} must be an object")
            continue

        route_label = rdata.get("name", f"index {idx}")
        _check_unknown_fields(
            rdata, _KNOWN_ROUTE_KEYS, "route", route_label, errors
        )

        for req_field in ("name", "source", "message_profile", "api", "endpoint"):
            if req_field not in rdata:
                errors.append(
                    f"Route at index {idx} missing required field '{req_field}'"
                )

        route_name = rdata.get("name")
        if route_name is not None:
            if route_name in route_names_seen:
                errors.append(f"Duplicate route name: '{route_name}'")
            route_names_seen.add(route_name)

        if rdata.get("source") and rdata["source"] not in sources:
            errors.append(
                f"Route '{route_name}' references missing source "
                f"'{rdata['source']}'"
            )
        if rdata.get("message_profile") and rdata["message_profile"] not in profiles:
            errors.append(
                f"Route '{route_name}' references missing message_profile "
                f"'{rdata['message_profile']}'"
            )
        if rdata.get("api") and rdata["api"] not in apis:
            errors.append(
                f"Route '{route_name}' references missing api '{rdata['api']}'"
            )
        if rdata.get("endpoint") and rdata["endpoint"] not in endpoints:
            errors.append(
                f"Route '{route_name}' references missing endpoint "
                f"'{rdata['endpoint']}'"
            )

        has_all = all(
            rdata.get(f) is not None
            for f in ("name", "source", "message_profile", "api", "endpoint")
        )
        if has_all:
            routes.append(RouteConfig.from_dict(rdata))

    if errors:
        raise RoutingConfigError(
            "Routing configuration validation failed:\n- "
            + "\n- ".join(errors)
        )

    return {
        "defaults": defaults,
        "sources": sources,
        "message_profiles": profiles,
        "apis": apis,
        "endpoints": endpoints,
        "routes": routes,
    }


# ---------------------------------------------------------------------------
# Message router
# ---------------------------------------------------------------------------

class MessageRouter:
    """Route resolver: given a message and its source section, find the
    single matching route (or report no-match / ambiguity)."""

    def __init__(
        self,
        routes: List[RouteConfig],
        sources: Dict[str, dict],
        profiles: Dict[str, MessageProfile],
        apis: Dict[str, ApiConfig],
        endpoints: Dict[str, EndpointConfig],
        defaults: Optional[RoutingDefaults] = None,
    ):
        self.routes = list(routes)
        self.sources = sources
        self.profiles = profiles
        self.apis = apis
        self.endpoints = endpoints
        self.defaults = defaults or RoutingDefaults()

    @classmethod
    def from_validated_config(cls, validated: dict) -> "MessageRouter":
        """Build a router from the dict returned by
        :func:`validate_routing_config`."""
        return cls(
            routes=validated["routes"],
            sources=validated["sources"],
            profiles=validated["message_profiles"],
            apis=validated["apis"],
            endpoints=validated["endpoints"],
            defaults=validated["defaults"],
        )

    def resolve(
        self, message: dict, source_section: str
    ) -> RouteResolutionResult:
        """Resolve which route should handle *message* from *source_section*.

        Algorithm
        ---------
        1. Disabled routes are ignored.
        2. Remaining routes are grouped by priority (descending).
        3. At each priority level, collect routes whose source section and
           message profile both match.
           - Exactly one match  → return *matched*.
           - More than one      → return *ambiguous*.
           - None               → continue to next lower priority.
        4. After all levels exhausted → return *no_match*.
        """
        active_routes = [r for r in self.routes if r.enabled]

        if not active_routes:
            return RouteResolutionResult(
                status="no_match",
                reason="no active routes configured",
            )

        priority_groups: Dict[int, List[RouteConfig]] = {}
        for route in active_routes:
            priority_groups.setdefault(route.priority, []).append(route)

        for priority in sorted(priority_groups, reverse=True):
            matched: List[RouteConfig] = []

            for route in priority_groups[priority]:
                source_def = self.sources.get(route.source, {})
                if source_def.get("section") != source_section:
                    continue

                profile = self.profiles.get(route.message_profile)
                if profile is None:
                    continue

                if profile.matches(message):
                    matched.append(route)

            if len(matched) == 1:
                return RouteResolutionResult(
                    status="matched",
                    route=matched[0],
                    matching_routes=matched,
                    reason=(
                        f"matched route '{matched[0].name}' "
                        f"at priority {priority}"
                    ),
                )

            if len(matched) > 1:
                names = [r.name for r in matched]
                return RouteResolutionResult(
                    status="ambiguous",
                    matching_routes=matched,
                    reason=(
                        f"ambiguous: {len(matched)} routes matched "
                        f"at priority {priority}: "
                        + ", ".join(f"'{n}'" for n in names)
                    ),
                )

        return RouteResolutionResult(
            status="no_match",
            reason="no route matched the message",
        )


# ---------------------------------------------------------------------------
# File-level config loader (convenience wrapper)
# ---------------------------------------------------------------------------

def load_routing_config(path: str) -> dict:
    """Load a routing / targets config from a JSON file.

    Returns the raw parsed dict.  Use :func:`detect_config_mode` and
    :func:`validate_routing_config` to interpret it.
    """
    with open(path, "r") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Template and body-building helpers
#
# These are intentionally duplicated from forwarder.py because that module
# calls ``sys.exit(1)`` at import time when pika is not installed, making
# it unsafe to import in test or library contexts.  Only the minimal pure
# functions needed for template rendering and body construction are
# reproduced here.
# ---------------------------------------------------------------------------

DEFAULT_SUCCESS_STATUS_CODES: List[int] = [200, 201, 202, 204]

_VALID_BODY_MODES = frozenset({"hp_control", "ev_power_timeseries"})


def _get_by_path(data: dict, path: str) -> Any:
    """Resolve a dotted path like ``'payload.slot_start'`` inside a dict."""
    if not path:
        return None
    value = data
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _first_by_paths(data: dict, paths: List[str]) -> Any:
    """Return the first non-None value found for the given dotted paths."""
    for path in paths:
        value = _get_by_path(data, path)
        if value is not None:
            return value
    return None


def _render_template(
    template: str, context: dict, *, strict: bool = False
) -> str:
    """Render a template string with ``{path}`` placeholders.

    When *strict* is True, an unresolvable placeholder raises
    :class:`ValueError` instead of being silently replaced with ``""``.
    """
    unresolved: List[str] = []

    def _replace(match):
        path = match.group(1)
        value = _get_by_path(context, path)
        if value is None:
            if strict:
                unresolved.append(path)
            return ""
        return str(value)

    result = re.sub(r"\{([^}]+)\}", _replace, template)
    if strict and unresolved:
        raise ValueError(
            "Unresolved template placeholder(s): "
            + ", ".join(f"'{p}'" for p in unresolved)
        )
    return result


# -- type coercion helpers used by _apply_template --------------------------

def _to_bool(value: Any) -> Optional[bool]:
    """Convert common string/number representations to bool."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "on", "yes"}:
            return True
        if lowered in {"false", "0", "off", "no"}:
            return False
    return None


def _to_on_off(value: Any) -> Optional[bool]:
    """Convert ON/OFF string values to bool."""
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered == "on":
            return True
        if lowered == "off":
            return False
    return None


def _coerce_datetime_utc_naive(value: Any) -> Optional[datetime]:
    """Parse an ISO-like datetime and return a naive UTC datetime."""
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _format_datetime_utc(value: Any) -> Optional[str]:
    """Parse an ISO-like datetime and return a UTC naive ISO string."""
    dt = _coerce_datetime_utc_naive(value)
    if dt is None:
        return None
    return dt.isoformat()


def _utc_now_naive() -> datetime:
    """Return current UTC time as a naive datetime."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _format_datetime_utc_future_minute(
    value: Any, now: Optional[datetime] = None
) -> Optional[str]:
    """Return the later of the mapped time and the upcoming UTC minute."""
    dt = _coerce_datetime_utc_naive(value)
    if dt is None:
        return None

    reference_now = now if now is not None else _utc_now_naive()
    if reference_now.tzinfo is not None:
        reference_now = (
            reference_now.astimezone(timezone.utc).replace(tzinfo=None)
        )

    next_minute = reference_now.replace(second=0, microsecond=0) + timedelta(
        minutes=1
    )
    return max(dt, next_minute).isoformat(timespec="seconds")


def _coerce_float(value: Any, field_name: str) -> float:
    """Convert a value to float or raise a clear ValueError."""
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{field_name} value {value!r} is not a valid float"
        ) from exc


def _apply_template(template: Any, context: dict) -> Any:
    """Apply a body template to the context data.

    Supports ``$map`` directives with optional ``type`` coercions
    (``bool``, ``on_off``, ``datetime_utc``, ``datetime_utc_future_minute``).
    """
    if isinstance(template, dict):
        if "$map" in template:
            map_spec = template.get("$map", "")
            if isinstance(map_spec, list):
                value = None
                for path in map_spec:
                    value = _get_by_path(context, path)
                    if value is not None:
                        break
            else:
                value = _get_by_path(context, map_spec)
            value_type = template.get("type")
            if value_type == "bool":
                converted = _to_bool(value)
                return converted if converted is not None else value
            if value_type == "on_off":
                converted = _to_on_off(value)
                return converted if converted is not None else value
            if value_type == "datetime_utc":
                converted = _format_datetime_utc(value)
                return converted if converted is not None else value
            if value_type == "datetime_utc_future_minute":
                converted = _format_datetime_utc_future_minute(value)
                return converted if converted is not None else value
            return value
        return {k: _apply_template(v, context) for k, v in template.items()}
    if isinstance(template, list):
        return [_apply_template(item, context) for item in template]
    if isinstance(template, str):
        return _render_template(template, context)
    return template


# -- body-mode builders -----------------------------------------------------

def _build_ev_power_timeseries_body(context: dict) -> dict:
    """Build the AEM EV request body from a single step or schedule."""
    payload = context.get("payload") or {}
    schedule = payload.get("schedule")

    if schedule is not None:
        if isinstance(schedule, list):
            if not schedule:
                raise ValueError("EV schedule is empty")

            body: dict = {}
            for index, entry in enumerate(schedule):
                if not isinstance(entry, dict):
                    raise ValueError(
                        f"EV schedule entry at index {index} must be an object"
                    )

                timestamp = None
                for key in ["time", "slot_start", "timestamp"]:
                    if entry.get(key) is not None:
                        timestamp = entry.get(key)
                        break
                if timestamp is None:
                    raise ValueError(
                        f"EV schedule entry at index {index} is missing "
                        "timestamp (time/slot_start/timestamp)"
                    )

                formatted_time = _format_datetime_utc(timestamp)
                if formatted_time is None:
                    raise ValueError(
                        f"EV schedule entry at index {index} has invalid "
                        f"timestamp {timestamp!r}"
                    )

                power_value = None
                for key in ["power_kw", "target_power_kw", "kw", "power"]:
                    if entry.get(key) is not None:
                        power_value = entry.get(key)
                        break
                if power_value is None:
                    raise ValueError(
                        f"EV schedule entry at index {index} is missing "
                        "power (power_kw/target_power_kw/kw/power)"
                    )

                body[formatted_time] = _coerce_float(
                    power_value,
                    f"EV schedule entry at index {index} power",
                )

            return body

        if isinstance(schedule, dict):
            if not schedule:
                raise ValueError("EV schedule is empty")

            body = {}
            for ts_key, power_value in schedule.items():
                formatted_time = _format_datetime_utc(ts_key)
                if formatted_time is None:
                    raise ValueError(
                        f"EV schedule entry timestamp {ts_key!r} is invalid"
                    )
                body[formatted_time] = _coerce_float(
                    power_value,
                    f"EV schedule entry for {formatted_time} power",
                )

            return body

        raise ValueError("EV schedule must be a list or object")

    timestamp = _first_by_paths(
        context,
        [
            "payload.slot_start",
            "payload.time",
            "payload.timestamp",
            "timestamp",
        ],
    )
    if timestamp is None:
        raise ValueError(
            "EV request is missing timestamp "
            "(payload.slot_start/payload.time/payload.timestamp/timestamp)"
        )

    formatted_time = _format_datetime_utc(timestamp)
    if formatted_time is None:
        raise ValueError(f"EV request timestamp {timestamp!r} is invalid")

    power_value = _first_by_paths(
        context,
        [
            "payload.power_kw",
            "payload.target_power_kw",
            "payload.kw",
            "payload.power",
        ],
    )
    if power_value is None:
        raise ValueError(
            "EV request is missing power "
            "(payload.power_kw/payload.target_power_kw/payload.kw/"
            "payload.power)"
        )

    return {formatted_time: _coerce_float(power_value, "EV request power")}


def _build_hp_control_body(message: dict) -> dict:
    """Build the standard HP control request body."""
    command_type = (
        message.get("command_type") or message.get("command") or "unknown"
    )
    return {
        "command": command_type,
        "asset_id": message.get("asset_id"),
        "asset_type": message.get("asset_type"),
        "timestamp": message.get("timestamp"),
        "payload": message.get("payload", {}),
    }


# ---------------------------------------------------------------------------
# Resolved request
# ---------------------------------------------------------------------------

@dataclass
class ResolvedRequest:
    """A fully resolved HTTP request ready to be dispatched (or dry-run
    logged).  Built from a matched route, its API and endpoint configs,
    and the incoming message."""

    route_name: str
    api_name: str
    endpoint_name: str
    method: str
    url: str
    headers: Optional[Dict[str, str]]
    body: Any
    auth: Optional[Tuple[str, str]]
    timeout_seconds: float
    verify_ssl: bool
    request_retries: int
    success_status_codes: List[int]
    effective_dry_run: bool


# ---------------------------------------------------------------------------
# Message-level dry-run extraction
# ---------------------------------------------------------------------------

def extract_message_dry_run(message: dict) -> Optional[bool]:
    """Extract the dry-run flag from a message's payload or slot_info.

    Returns ``None`` when the message does not carry a dry-run flag.
    """
    for section in ("payload", "slot_info"):
        data = message.get(section, {})
        if isinstance(data, dict) and "dry_run" in data:
            return data["dry_run"]
    return None


# ---------------------------------------------------------------------------
# Request building
# ---------------------------------------------------------------------------

def _build_request_context(
    message: dict, api_config: ApiConfig
) -> dict:
    """Build the template rendering context from a message and API config.

    The context is a **shallow copy** of the message with an ``api`` key
    added.  The original message dict is never mutated.
    """
    context = dict(message)
    context["api"] = {
        "base_url": api_config.base_url,
        "controlUrl": api_config.base_url,
        "url": api_config.base_url,
        "user": api_config.user,
        "name": api_config.name,
    }
    return context


def _resolve_url(
    api_config: ApiConfig,
    endpoint_config: EndpointConfig,
    context: dict,
) -> str:
    """Build the full request URL from the API base URL and endpoint path.

    * If the rendered path is an absolute URL, it is used as-is.
    * Otherwise, it is appended to the API's ``base_url``.

    Raises :class:`ValueError` when a required placeholder cannot be
    resolved or the base URL is missing for a relative path.
    """
    rendered_path = _render_template(
        endpoint_config.path_template, context, strict=True
    )

    if rendered_path.startswith("http://") or rendered_path.startswith(
        "https://"
    ):
        return rendered_path

    base = api_config.base_url.rstrip("/")
    if not base:
        raise ValueError(
            f"Endpoint '{endpoint_config.name}' has a relative path "
            f"template but API '{api_config.name}' has no base_url"
        )

    return f"{base}/{rendered_path.lstrip('/')}"


def _build_body(
    endpoint_config: EndpointConfig,
    message: dict,
    context: dict,
) -> Any:
    """Build the request body according to the endpoint configuration.

    Priority: ``body_mode`` > ``body_template`` > passthrough (deep-copy
    of the original message).
    """
    if endpoint_config.body_mode is not None:
        if endpoint_config.body_mode == "hp_control":
            return _build_hp_control_body(message)
        if endpoint_config.body_mode == "ev_power_timeseries":
            return _build_ev_power_timeseries_body(context)
        raise ValueError(
            f"Unsupported body_mode '{endpoint_config.body_mode}'"
        )

    if endpoint_config.body_template is not None:
        return _apply_template(endpoint_config.body_template, context)

    return copy.deepcopy(message)


def build_resolved_request(
    message: dict,
    route: RouteConfig,
    router: MessageRouter,
    forwarder_dry_run: bool,
) -> ResolvedRequest:
    """Build a :class:`ResolvedRequest` from a matched route and message.

    This function does **not** send any HTTP request.  It resolves the
    URL, body, auth, dry-run, and all metadata needed for the dispatcher
    (implemented in a later step).

    The input *message* is never mutated.

    Raises :class:`ValueError` when required configuration pieces
    (base_url, placeholders, body_mode) cannot be resolved.
    """
    api_config = router.apis.get(route.api)
    if api_config is None:
        raise ValueError(
            f"Route '{route.name}' references unknown API '{route.api}'"
        )

    endpoint_config = router.endpoints.get(route.endpoint)
    if endpoint_config is None:
        raise ValueError(
            f"Route '{route.name}' references unknown endpoint "
            f"'{route.endpoint}'"
        )

    context = _build_request_context(message, api_config)

    url = _resolve_url(api_config, endpoint_config, context)
    body = _build_body(endpoint_config, message, context)

    auth: Optional[Tuple[str, str]] = None
    if api_config.user and api_config.password:
        auth = (api_config.user, api_config.password)

    message_dry_run = extract_message_dry_run(message)
    effective_dry_run = resolve_effective_dry_run(
        forwarder_dry_run=forwarder_dry_run,
        route_dry_run=route.dry_run,
        message_dry_run=message_dry_run,
        missing_message_dry_run_default=(
            router.defaults.missing_message_dry_run_default
        ),
    )

    success_codes = endpoint_config.success_status_codes
    if success_codes is None:
        success_codes = list(DEFAULT_SUCCESS_STATUS_CODES)

    return ResolvedRequest(
        route_name=route.name,
        api_name=api_config.name,
        endpoint_name=endpoint_config.name,
        method=endpoint_config.method,
        url=url,
        headers=endpoint_config.headers,
        body=body,
        auth=auth,
        timeout_seconds=api_config.timeout,
        verify_ssl=api_config.verify_ssl,
        request_retries=api_config.retries,
        success_status_codes=success_codes,
        effective_dry_run=effective_dry_run,
    )


# ---------------------------------------------------------------------------
# HTTP dispatch
# ---------------------------------------------------------------------------

_RETRYABLE_STATUS_CODES = frozenset({429})

try:
    from urllib3.exceptions import InsecureRequestWarning
except ImportError:
    InsecureRequestWarning = type(
        "InsecureRequestWarning", (UserWarning,), {}
    )


def _is_retryable_status(status_code: int) -> bool:
    """Return True for status codes that should trigger a retry."""
    return status_code >= 500 or status_code in _RETRYABLE_STATUS_CODES


def _format_body_preview(body: Any, limit: int = 200) -> str:
    """Serialize a request body to a bounded preview string for logging."""
    try:
        text = json.dumps(body, sort_keys=True, default=str)
    except (TypeError, ValueError):
        text = str(body)
    if len(text) > limit:
        return text[:limit] + "..."
    return text


@dataclass
class HttpDispatchResult:
    """Result of dispatching a single HTTP request (or dry-run skip)."""

    success: bool
    dry_run: bool
    status_code: Optional[int] = None
    error: Optional[str] = None
    attempts: int = 0
    policy: str = DEFAULT_HTTP_FAILURE_POLICY
    route_name: str = ""
    api_name: str = ""
    endpoint_name: str = ""


def dispatch_http_request(
    request: ResolvedRequest,
    message: Union[dict, list],
    policy: str = DEFAULT_HTTP_FAILURE_POLICY,
    session: Any = None,
    _logger: Optional[logging.Logger] = None,
) -> HttpDispatchResult:
    """Dispatch (or dry-run log) an HTTP request built from a matched route.

    Parameters
    ----------
    request:
        The fully resolved request produced by :func:`build_resolved_request`.
    message:
        The original RabbitMQ message (dict or list of dicts) — used only for
        logging context (``asset_id``, ``asset_type``, ``message_type``).
        Never mutated.
    policy:
        The failure policy to record in the result (default:
        ``ack_error_no_requeue``).  This step does not perform RabbitMQ
        ack/nack; the policy is informational for the caller.
    session:
        An injectable HTTP session (must support
        ``session.request(method, url, **kwargs)``).  When ``None``, a
        ``requests.Session`` is created lazily.  Inject a mock in tests.
    _logger:
        Optional override logger.

    Returns
    -------
    HttpDispatchResult
        Indicates success/failure, dry-run, status code, attempts, etc.

    Retry semantics
    ---------------
    ``total_attempts = request.request_retries + 1`` (matching the existing
    ``TargetHandler`` convention where ``retries`` means *additional*
    attempts after the first).  Minimum 1 attempt.

    Retryable conditions: 5xx, 429, timeout/connection exceptions.
    Non-retryable: 4xx (except 429).
    """
    log = _logger or logger

    result_kwargs = dict(
        route_name=request.route_name,
        api_name=request.api_name,
        endpoint_name=request.endpoint_name,
        policy=policy,
    )

    # -- dry-run -------------------------------------------------------------
    if request.effective_dry_run:
        log.info(
            "[DRY-RUN] %s %s route='%s' api='%s' endpoint='%s' body=%s",
            request.method,
            request.url,
            request.route_name,
            request.api_name,
            request.endpoint_name,
            _format_body_preview(request.body),
        )
        return HttpDispatchResult(
            success=True, dry_run=True, attempts=0, **result_kwargs
        )

    # -- live dispatch -------------------------------------------------------
    if session is None:
        try:
            import requests as _requests_lib  # noqa: F811
        except ImportError:
            log.error(
                "Cannot dispatch route '%s': 'requests' library not "
                "installed",
                request.route_name,
            )
            return HttpDispatchResult(
                success=False,
                dry_run=False,
                error="requests library not installed",
                attempts=0,
                **result_kwargs,
            )
        session = _requests_lib.Session()

    total_attempts = max(1, request.request_retries + 1)
    last_error: str = "unknown error"
    last_status: Optional[int] = None
    attempt = 0

    for attempt in range(1, total_attempts + 1):
        try:
            request_kwargs: Dict[str, Any] = {
                "json": request.body,
                "headers": request.headers,
                "timeout": request.timeout_seconds,
                "verify": request.verify_ssl,
            }
            if request.auth is not None:
                request_kwargs["auth"] = request.auth

            if request.verify_ssl:
                response = session.request(
                    request.method, request.url, **request_kwargs
                )
            else:
                with warnings.catch_warnings():
                    warnings.filterwarnings(
                        "ignore", category=InsecureRequestWarning
                    )
                    response = session.request(
                        request.method, request.url, **request_kwargs
                    )

        except Exception as exc:
            last_error = str(exc)
            last_status = None
            if attempt < total_attempts:
                log.warning(
                    "Route '%s' attempt %d/%d failed: %s; retrying",
                    request.route_name,
                    attempt,
                    total_attempts,
                    last_error,
                )
                continue
            break

        last_status = response.status_code

        # -- success ---------------------------------------------------------
        if response.status_code in request.success_status_codes:
            if attempt > 1:
                log.info(
                    "Route '%s' request succeeded on attempt %d/%d",
                    request.route_name,
                    attempt,
                    total_attempts,
                )
            return HttpDispatchResult(
                success=True,
                dry_run=False,
                status_code=response.status_code,
                attempts=attempt,
                **result_kwargs,
            )

        # -- retryable failure -----------------------------------------------
        if _is_retryable_status(response.status_code):
            last_error = f"HTTP {response.status_code}"
            if attempt < total_attempts:
                log.warning(
                    "Route '%s' attempt %d/%d returned %d; retrying",
                    request.route_name,
                    attempt,
                    total_attempts,
                    response.status_code,
                )
                continue
            break

        # -- non-retryable failure -------------------------------------------
        last_error = f"HTTP {response.status_code}"
        break

    # -- all attempts exhausted or non-retryable error -----------------------
    if isinstance(message, list):
        asset_id = ", ".join(m.get("asset_id", "unknown") for m in message if isinstance(m, dict)) or "unknown"
        asset_type = next((m.get("asset_type", "unknown") for m in message if isinstance(m, dict)), "unknown")
        message_type = next((m.get("message_type", "unknown") for m in message if isinstance(m, dict)), "unknown")
    else:
        asset_id = message.get("asset_id", "unknown")
        asset_type = message.get("asset_type", "unknown")
        message_type = message.get("message_type", "unknown")

    log.error(
        "Route '%s' HTTP dispatch failed after %d attempt(s): "
        "api='%s' endpoint='%s' url='%s' status=%s error='%s' "
        "asset_id='%s' asset_type='%s' message_type='%s' "
        "policy='%s'",
        request.route_name,
        attempt,
        request.api_name,
        request.endpoint_name,
        request.url,
        last_status,
        last_error,
        asset_id,
        asset_type,
        message_type,
        policy,
    )
    log.error(
        "Route '%s' HTTP dispatch failed payload: %s",
        request.route_name,
        _format_body_preview(request.body, limit=2000),
    )

    return HttpDispatchResult(
        success=False,
        dry_run=False,
        status_code=last_status,
        error=last_error,
        attempts=attempt,
        **result_kwargs,
    )
