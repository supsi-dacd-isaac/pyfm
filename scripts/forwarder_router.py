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

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


logger = logging.getLogger("forwarder")

# ---------------------------------------------------------------------------
# Valid policy values (only the defaults implemented in Step 1)
# ---------------------------------------------------------------------------

VALID_NO_MATCH_POLICIES = frozenset({"ack_warn_no_forward"})
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
    headers: Optional[Dict[str, str]] = None
    success_status_codes: Optional[List[int]] = None

    @classmethod
    def from_dict(cls, name: str, data: dict) -> "EndpointConfig":
        return cls(
            name=name,
            method=data.get("method", "POST"),
            path_template=data.get("path_template", ""),
            body_template=data.get("body_template"),
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
    """Compute effective dry-run = forwarder OR route OR message.

    * *forwarder_dry_run*: global CLI / env flag.
    * *route_dry_run*: per-route setting; ``None`` treated as ``False``.
    * *message_dry_run*: per-message payload flag; ``None`` falls back to
      *missing_message_dry_run_default*.
    """
    if forwarder_dry_run:
        return True

    if route_dry_run is not None and route_dry_run:
        return True

    if message_dry_run is None:
        return missing_message_dry_run_default

    return message_dry_run


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

    profiles_data = config.get("message_profiles", {})
    if not isinstance(profiles_data, dict):
        errors.append("'message_profiles' must be an object")
        profiles_data = {}
    profiles = {
        name: MessageProfile.from_dict(name, pdata)
        for name, pdata in profiles_data.items()
    }

    apis_data = config.get("apis", {})
    if not isinstance(apis_data, dict):
        errors.append("'apis' must be an object")
        apis_data = {}
    apis = {
        name: ApiConfig.from_dict(name, adata) for name, adata in apis_data.items()
    }

    endpoints_data = config.get("endpoints", {})
    if not isinstance(endpoints_data, dict):
        errors.append("'endpoints' must be an object")
        endpoints_data = {}
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
