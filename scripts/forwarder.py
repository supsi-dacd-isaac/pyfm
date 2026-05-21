#!/usr/bin/env python3
"""
Forwarder (forwarder.py)

This script receives control commands and measurements from section-based
RabbitMQ sources and forwards them to the actual asset control interfaces.

The forwarder acts as the actuation layer, decoupled from the decision
logic in flexi_manager.py. This separation allows:
- Better scalability (multiple forwarders can consume from the same queue)
- Reliable message delivery (RabbitMQ persistence and acknowledgments)
- Easier testing (dry-run mode)
- Protocol translation (RabbitMQ -> MQTT/HTTP/Modbus/OCPP)

Usage:
    # Dry-run mode (just logs commands without actuating)
    python forwarder.py --dry-run

    # Listen for specific asset types only
    python forwarder.py --dry-run --asset-types heat_pump,ev_charger

    # Custom RabbitMQ configuration
    python forwarder.py --dry-run --rabbitmq-host rabbitmq.local --rabbitmq-port 5672

Environment variables (for Docker/containerized deployment):
    FORWARDER_MODE          - "dry-run" or "live" (default: dry-run)
    FORWARDER_LOG_LEVEL     - DEBUG, INFO, WARNING, ERROR (default: INFO)
    FORWARDER_ASSET_TYPES   - Comma-separated list of asset types to filter
    FORWARDER_RABBIT_SECTIONS
                            - Comma-separated rabbitMQ sections to consume
    RABBITMQ_HOST           - RabbitMQ hostname (default: localhost)
    RABBITMQ_PORT           - RabbitMQ port (default: 5672)
    RABBITMQ_USER           - RabbitMQ username (default: guest)
    RABBITMQ_PASS           - RabbitMQ password (default: guest)
    RABBITMQ_VHOST          - RabbitMQ virtual host (default: /)
    RABBITMQ_EXCHANGE       - Legacy option ignored for section-based sources

Example flow:
    1. flexi_manager.py publishes commands to asset-specific RabbitMQ sections
    2. forwarder.py consumes from the configured section queues
    3. forwarder.py translates and forwards to actual device protocols
"""

import os
import sys
import json
import argparse
import logging
import signal
import time
import warnings
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Callable, Any, Iterable, Union
from urllib.parse import urlparse, urlunparse

# HTTP requests for forwarding to targets
try:
    import requests
    REQUESTS_AVAILABLE = True
    try:
        from urllib3.exceptions import InsecureRequestWarning
    except ImportError:
        InsecureRequestWarning = None
except ImportError:
    REQUESTS_AVAILABLE = False
    InsecureRequestWarning = None


DEFAULT_REQUEST_TIMEOUT_SECONDS = 10.0
DEFAULT_REQUEST_RETRIES = 3


def get_env(name: str, default: str = None) -> str:
    """Get environment variable with optional default."""
    return os.environ.get(name, default)

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# RabbitMQ support
try:
    import pika
    RABBITMQ_AVAILABLE = True
except ImportError:
    RABBITMQ_AVAILABLE = False
    print("ERROR: pika library not installed. Run: pip install pika", file=sys.stderr)
    sys.exit(1)


# =============================================================================
# LOGGING SETUP
# =============================================================================

def setup_logging(log_level: str = "INFO", log_file: Optional[str] = None) -> logging.Logger:
    """Configure logging with timestamp and level.

    :param log_level: Logging level (DEBUG, INFO, WARNING, ERROR)
    :param log_file: Optional path to log file
    :return: Configured logger instance
    """
    logger = logging.getLogger("forwarder")
    logger.setLevel(getattr(logging, log_level.upper()))
    
    formatter = logging.Formatter(
        "%(asctime)s::%(levelname)s::%(funcName)s::%(message)s"
    )

    # Always add console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # Optionally add file handler
    if log_file:
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger


# =============================================================================
# TARGET HANDLERS - Forward commands to external systems
# =============================================================================

def _get_by_path(data: dict, path: str) -> Any:
    """Resolve a dotted path like 'payload.slot_start' inside a dict."""
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


def _format_datetime_utc_future_minute(value: Any, now: Optional[datetime] = None) -> Optional[str]:
    """Return the later of the mapped time and the upcoming UTC minute."""
    dt = _coerce_datetime_utc_naive(value)
    if dt is None:
        return None

    reference_now = now if now is not None else _utc_now_naive()
    if reference_now.tzinfo is not None:
        reference_now = reference_now.astimezone(timezone.utc).replace(tzinfo=None)

    next_minute = reference_now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    return max(dt, next_minute).isoformat(timespec="seconds")


def _coerce_float(value: Any, field_name: str) -> float:
    """Convert a value to float or raise a clear ValueError."""
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} value {value!r} is not a valid float") from exc


def _coerce_request_timeout_seconds(value: Any, default: float = DEFAULT_REQUEST_TIMEOUT_SECONDS) -> float:
    """Convert a timeout to a positive float, or fall back to the default."""
    if isinstance(value, bool):
        return default
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        return default
    return timeout if timeout > 0 else default


def _coerce_request_retries(value: Any, default: int = DEFAULT_REQUEST_RETRIES) -> int:
    """Convert retries to a non-negative integer, or fall back to the default."""
    if isinstance(value, bool):
        return default
    try:
        retries = int(float(value))
    except (TypeError, ValueError):
        return default
    return retries if retries >= 0 else default


def _build_ev_power_timeseries_body(context: dict) -> dict:
    """Build the AEM EV request body from a single step or schedule."""
    payload = context.get("payload") or {}
    schedule = payload.get("schedule")

    if schedule is not None:
        if isinstance(schedule, list):
            if not schedule:
                raise ValueError("EV schedule is empty")

            body = {}
            for index, entry in enumerate(schedule):
                if not isinstance(entry, dict):
                    raise ValueError(f"EV schedule entry at index {index} must be an object")

                timestamp = None
                for key in ["time", "slot_start", "timestamp"]:
                    if entry.get(key) is not None:
                        timestamp = entry.get(key)
                        break
                if timestamp is None:
                    raise ValueError(
                        f"EV schedule entry at index {index} is missing timestamp "
                        "(time/slot_start/timestamp)"
                    )

                formatted_time = _format_datetime_utc(timestamp)
                if formatted_time is None:
                    raise ValueError(
                        f"EV schedule entry at index {index} has invalid timestamp {timestamp!r}"
                    )

                power_value = None
                for key in ["power_kw", "target_power_kw", "kw", "power"]:
                    if entry.get(key) is not None:
                        power_value = entry.get(key)
                        break
                if power_value is None:
                    raise ValueError(
                        f"EV schedule entry at index {index} is missing power "
                        "(power_kw/target_power_kw/kw/power)"
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
            for timestamp, power_value in schedule.items():
                formatted_time = _format_datetime_utc(timestamp)
                if formatted_time is None:
                    raise ValueError(f"EV schedule entry timestamp {timestamp!r} is invalid")
                body[formatted_time] = _coerce_float(
                    power_value,
                    f"EV schedule entry for {formatted_time} power",
                )

            return body

        raise ValueError("EV schedule must be a list or object")

    timestamp = _first_by_paths(
        context,
        ["payload.slot_start", "payload.time", "payload.timestamp", "timestamp"],
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
        ["payload.power_kw", "payload.target_power_kw", "payload.kw", "payload.power"],
    )
    if power_value is None:
        raise ValueError(
            "EV request is missing power "
            "(payload.power_kw/payload.target_power_kw/payload.kw/payload.power)"
        )

    return {formatted_time: _coerce_float(power_value, "EV request power")}


def _render_template(template: str, context: dict) -> str:
    """Render a template string with {path} placeholders from the context."""
    def _replace(match):
        path = match.group(1)
        value = _get_by_path(context, path)
        return "" if value is None else str(value)

    import re
    return re.sub(r"\{([^}]+)\}", _replace, template)


def _apply_template(template: Any, context: dict) -> Any:
    """Apply a body template to the context data."""
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


def _format_request_body_for_log(request_body: Any) -> str:
    """Serialize a request body to a stable single-line string for logging."""
    try:
        return json.dumps(request_body, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(request_body)


def _normalize_control_url(control_url: Optional[str], api_port: Optional[int]) -> Optional[str]:
    """Ensure controlUrl has a scheme and optional port if missing."""
    if not control_url:
        return None
    normalized = control_url.strip()
    if "://" not in normalized:
        normalized = f"https://{normalized.lstrip('/')}"

    parsed = urlparse(normalized)
    if not parsed.hostname:
        return normalized

    if parsed.port is None and api_port:
        netloc = f"{parsed.hostname}:{api_port}"
        parsed = parsed._replace(netloc=netloc)
        normalized = urlunparse(parsed)

    return normalized


class TargetConfig:
    """Configuration for a forwarding target."""

    def __init__(
        self,
        name: str,
        url: str,
        auth_user: str = None,
        auth_password: str = None,
        timeout: Optional[float] = None,
        request_timeout_seconds: Optional[float] = None,
        request_retries: Optional[int] = None,
        verify_ssl: bool = False,
        asset_types: List[str] = None,
        asset_ids: List[str] = None,
        enabled: bool = True,
        endpoint_template: Optional[str] = None,
        endpoint_overrides: Optional[dict] = None,
        asset_request_profiles: Optional[dict] = None,
        body_template: Optional[dict] = None,
        reference_api: Optional[str] = None,
        default_request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        default_request_retries: int = DEFAULT_REQUEST_RETRIES,
        timeout_is_configured: bool = False
    ):
        """
        Initialize target configuration.

        :param name: Target name (e.g., 'aem-server-simulator')
        :param url: Base URL for the target API
        :param auth_user: Username for Basic Auth (optional)
        :param auth_password: Password for Basic Auth (optional)
        :param timeout: Legacy request timeout in seconds
        :param request_timeout_seconds: Request timeout in seconds
        :param request_retries: Number of retries after the initial attempt
        :param verify_ssl: Whether to verify SSL certificates
        :param asset_types: List of asset types this target handles (None = all)
        :param asset_ids: List of asset IDs this target handles (None = all)
        :param enabled: Whether this target is enabled
        :param endpoint_template: Optional endpoint template for this target
        :param endpoint_overrides: Optional asset-specific endpoint overrides
        :param asset_request_profiles: Optional asset-specific request profiles
        :param body_template: Optional body template for this target
        :param reference_api: Optional API key to load from conns.json
        :param default_request_timeout_seconds: Default request timeout in seconds
        :param default_request_retries: Default number of retries
        :param timeout_is_configured: Whether timeout was explicitly set in config
        """
        self.name = name
        self.url = (url or "").rstrip('/')
        self.auth_user = auth_user
        self.auth_password = auth_password
        self._default_request_timeout_seconds = _coerce_request_timeout_seconds(
            default_request_timeout_seconds,
            DEFAULT_REQUEST_TIMEOUT_SECONDS,
        )
        self._default_request_retries = _coerce_request_retries(
            default_request_retries,
            DEFAULT_REQUEST_RETRIES,
        )
        configured_timeout = request_timeout_seconds
        if configured_timeout is None:
            configured_timeout = timeout
        self.request_timeout_seconds = _coerce_request_timeout_seconds(
            configured_timeout,
            self._default_request_timeout_seconds,
        )
        self.timeout = self.request_timeout_seconds
        self.request_retries = _coerce_request_retries(
            request_retries,
            self._default_request_retries,
        )
        self.verify_ssl = verify_ssl
        self.asset_types = asset_types
        self.asset_ids = asset_ids
        self.enabled = enabled
        self.endpoint_template = endpoint_template
        self.endpoint_overrides = endpoint_overrides or {}
        self.asset_request_profiles = asset_request_profiles or {}
        self.body_template = body_template
        self.reference_api = reference_api
        self._timeout_is_configured = timeout_is_configured
        self._api_config: Optional[dict] = None

    def matches(self, asset_type: str, asset_id: str) -> bool:
        """
        Check if this target should handle the given asset.

        :param asset_type: Asset type (e.g., 'heat_pump', 'ev_charger')
        :param asset_id: Asset ID (e.g., 'ECM97.1')
        :return: True if this target should handle the asset
        """
        if not self.enabled:
            return False

        # Check asset_ids first (more specific)
        if self.asset_ids is not None:
            return asset_id in self.asset_ids

        # Check asset_types
        if self.asset_types is not None:
            return asset_type in self.asset_types

        # No filter = handle all
        return True

    def get_auth(self):
        """Get auth tuple for requests library."""
        if self.auth_user and self.auth_password:
            return (self.auth_user, self.auth_password)
        return None

    @classmethod
    def from_dict(
        cls,
        data: dict,
        default_request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        default_request_retries: int = DEFAULT_REQUEST_RETRIES,
    ) -> 'TargetConfig':
        """Create TargetConfig from dictionary."""
        timeout_is_configured = (
            data.get("timeout") is not None or
            data.get("request_timeout_seconds") is not None
        )
        return cls(
            name=data.get("name", "unknown"),
            url=data.get("url", ""),
            auth_user=data.get("user"),
            auth_password=data.get("password"),
            timeout=data.get("timeout"),
            request_timeout_seconds=data.get("request_timeout_seconds"),
            request_retries=data.get("request_retries"),
            verify_ssl=data.get("verify_ssl", False),
            asset_types=data.get("asset_types"),
            asset_ids=data.get("asset_ids"),
            enabled=data.get("enabled", True),
            endpoint_template=data.get("endpoint_template"),
            endpoint_overrides=data.get("endpoint_overrides") or data.get("custom_commands") or data.get("custom_commans"),
            asset_request_profiles=data.get("asset_request_profiles"),
            body_template=data.get("body_template"),
            reference_api=data.get("reference_api"),
            default_request_timeout_seconds=default_request_timeout_seconds,
            default_request_retries=default_request_retries,
            timeout_is_configured=timeout_is_configured
        )

    def apply_api_config(self, api_config: dict):
        """Merge API config from conns.json into this target."""
        self._api_config = dict(api_config)
        control_url = api_config.get("controlUrl") or api_config.get("controlURL")
        control_url = _normalize_control_url(control_url, api_config.get("port"))
        if control_url:
            self.url = control_url.rstrip('/')
            self._api_config["controlUrl"] = control_url
        if not self.auth_user and api_config.get("user"):
            self.auth_user = api_config.get("user")
        if not self.auth_password and api_config.get("password"):
            self.auth_password = api_config.get("password")
        if not self._timeout_is_configured and api_config.get("requestTimeout") is not None:
            self.request_timeout_seconds = _coerce_request_timeout_seconds(
                api_config.get("requestTimeout"),
                self._default_request_timeout_seconds,
            )
            self.timeout = self.request_timeout_seconds

    def _resolve_configured_endpoint(self, endpoint_value: Any, context: dict, asset_id: Optional[str], label: str) -> str:
        """Resolve an absolute or relative endpoint value against the base URL."""
        rendered = _render_template(str(endpoint_value), context)
        if not rendered:
            raise ValueError(f"{label} rendered empty for asset_id='{asset_id}'")

        if rendered.startswith("http://") or rendered.startswith("https://"):
            return rendered

        if not self.url:
            api_control_url = None
            if isinstance(self._api_config, dict):
                api_control_url = self._api_config.get("controlUrl") or self._api_config.get("controlURL")
            raise ValueError(
                f"{label} requires base URL but none is configured "
                f"(asset_id='{asset_id}', value='{endpoint_value}', api.controlUrl='{api_control_url}')"
            )

        return f"{self.url.rstrip('/')}/{rendered.lstrip('/')}"

    def _build_default_request_body(self, message: dict, command_type: str, payload: dict, context: dict) -> dict:
        """Build the request body using the legacy body_template/default behavior."""
        if self.body_template:
            return _apply_template(self.body_template, context)

        return {
            "command": command_type,
            "asset_id": message.get("asset_id"),
            "asset_type": message.get("asset_type"),
            "timestamp": message.get("timestamp"),
            "payload": payload
        }

    def _build_body_for_mode(self, body_mode: Optional[str], message: dict, command_type: str, payload: dict, context: dict) -> dict:
        """Build the request body for the selected profile mode."""
        if not body_mode or body_mode == "hp_control":
            return self._build_default_request_body(message, command_type, payload, context)

        if body_mode == "ev_power_timeseries":
            return _build_ev_power_timeseries_body(context)

        raise ValueError(f"Unsupported body_mode '{body_mode}'")

    def build_request(self, message: dict) -> (str, dict):
        """Build endpoint and body for this target, using templates when provided."""
        command_type = message.get("command_type") or message.get("command") or "unknown"
        payload = message.get("payload", {})
        context = dict(message)
        if self._api_config:
            context["api"] = self._api_config
        asset_id = message.get("asset_id") or payload.get("asset_id")
        request_profile = self.asset_request_profiles.get(asset_id) if asset_id else None
        profile_endpoint = None
        profile_body_mode = None

        if request_profile is not None:
            if not isinstance(request_profile, dict):
                raise ValueError(
                    f"Target '{self.name}' request profile for asset '{asset_id}' must be an object"
                )
            profile_endpoint = request_profile.get("endpoint")
            profile_body_mode = request_profile.get("body_mode")
            logging.getLogger("forwarder").info(
                "Target '%s' using request profile for asset '%s': body_mode=%s, endpoint=%s",
                self.name,
                asset_id,
                profile_body_mode,
                profile_endpoint,
            )

        endpoint_override = self.endpoint_overrides.get(asset_id) if asset_id else None

        if profile_endpoint is not None:
            endpoint = self._resolve_configured_endpoint(
                profile_endpoint,
                context,
                asset_id,
                "Request profile endpoint",
            )
        elif endpoint_override is not None:
            logging.getLogger("forwarder").info(
                "Target '%s' using endpoint override for asset '%s': %s",
                self.name,
                asset_id,
                _render_template(str(endpoint_override), context),
            )
            endpoint = self._resolve_configured_endpoint(
                endpoint_override,
                context,
                asset_id,
                "Endpoint override",
            )
        elif self.endpoint_template:
            if "api." in self.endpoint_template and "api" not in context:
                raise ValueError("Endpoint template references api.* but reference_api is not loaded")
            rendered = _render_template(self.endpoint_template, context)
            if not rendered:
                raise ValueError("Endpoint template rendered empty")
            if rendered.startswith("http://") or rendered.startswith("https://"):
                endpoint = rendered
            else:
                if not self.url:
                    api_control_url = None
                    if isinstance(self._api_config, dict):
                        api_control_url = self._api_config.get("controlUrl") or self._api_config.get("controlURL")
                    raise ValueError(
                        "Endpoint template requires base URL but none is configured "
                        f"(endpoint_template='{self.endpoint_template}', api.controlUrl='{api_control_url}')"
                    )
                endpoint = f"{self.url}/{rendered.lstrip('/')}"
        else:
            if command_type in ["curtail", "restore", "preactivate"]:
                endpoint = f"{self.url}/control"
            else:
                endpoint = f"{self.url}/command"

        request_body = self._build_body_for_mode(
            profile_body_mode,
            message,
            command_type,
            payload,
            context,
        )

        return endpoint, request_body


class TargetHandler:
    """
    Manages forwarding commands to external HTTP targets.

    Supports multiple targets with different configurations.
    Routes commands based on asset type or asset ID.
    """

    def __init__(self, logger: logging.Logger, dry_run: bool = True):
        """
        Initialize target handler.

        :param logger: Logger instance
        :param dry_run: If True, don't actually send HTTP requests
        """
        self.logger = logger
        self.dry_run = dry_run
        self.targets: Dict[str, TargetConfig] = {}
        self.stats = {
            "requests_sent": 0,
            "requests_success": 0,
            "requests_failed": 0,
            "by_target": {}
        }
        self._conns_path: Optional[str] = None
        self._conns_base_dirs: List[str] = []
        self._conns_cache: dict = {}
        self._conns_fallback_paths: List[str] = []

        if not REQUESTS_AVAILABLE and not dry_run:
            self.logger.warning("requests library not installed - HTTP forwarding disabled")

    def set_conns_lookup(self, conns_path: str, base_dirs: List[str], conns_cache: dict, fallback_paths: Optional[List[str]] = None):
        """Configure conns.json lookup for lazy reference_api loading."""
        self._conns_path = conns_path
        self._conns_base_dirs = base_dirs or []
        self._conns_cache = conns_cache or {}
        self._conns_fallback_paths = fallback_paths or []

    def add_target(self, config: TargetConfig):
        """
        Add a forwarding target.

        :param config: Target configuration
        """
        self.targets[config.name] = config
        self.stats["by_target"][config.name] = {"sent": 0, "success": 0, "failed": 0}
        self.logger.info(
            "Added target '%s': %s (auth: %s, types: %s, ids: %s)",
            config.name,
            config.url,
            "enabled" if config.get_auth() else "disabled",
            config.asset_types or "all",
            config.asset_ids or "all"
        )

    def add_target_from_env(self, prefix: str = "TARGET"):
        """
        Add a target from environment variables.

        Looks for variables like:
        - TARGET_NAME, TARGET_URL, TARGET_USER, TARGET_PASSWORD, etc.

        :param prefix: Environment variable prefix
        """
        name = get_env(f"{prefix}_NAME")
        url = get_env(f"{prefix}_URL")

        if not name or not url:
            return

        asset_types_str = get_env(f"{prefix}_ASSET_TYPES")
        asset_ids_str = get_env(f"{prefix}_ASSET_IDS")

        config = TargetConfig(
            name=name,
            url=url,
            auth_user=get_env(f"{prefix}_USER"),
            auth_password=get_env(f"{prefix}_PASSWORD"),
            timeout=get_env(f"{prefix}_TIMEOUT"),
            request_timeout_seconds=get_env(f"{prefix}_REQUEST_TIMEOUT_SECONDS"),
            request_retries=get_env(f"{prefix}_REQUEST_RETRIES"),
            verify_ssl=get_env(f"{prefix}_VERIFY_SSL", "false").lower() == "true",
            asset_types=asset_types_str.split(",") if asset_types_str else None,
            asset_ids=asset_ids_str.split(",") if asset_ids_str else None,
            enabled=get_env(f"{prefix}_ENABLED", "true").lower() == "true"
        )

        self.add_target(config)

    def get_targets_for_asset(self, asset_type: str, asset_id: str) -> List[TargetConfig]:
        """
        Get all targets that should handle a given asset.

        :param asset_type: Asset type
        :param asset_id: Asset ID
        :return: List of matching target configs
        """
        return [t for t in self.targets.values() if t.matches(asset_type, asset_id)]

    def forward_command(self, message: dict) -> bool:
        """
        Forward a command to all matching targets.

        :param message: Command message dictionary
        :return: True if forwarded successfully to at least one target
        """
        asset_id = message.get("asset_id", "unknown")
        asset_type = message.get("asset_type", "unknown")
        payload = message.get("payload", {})

        matching_targets = self.get_targets_for_asset(asset_type, asset_id)

        if not matching_targets:
            self.logger.debug("No targets configured for asset %s (type: %s)", asset_id, asset_type)
            return True  # Not an error - just no targets configured

        success = False

        for target in matching_targets:
            result = self._send_to_target(target, message)
            if result:
                success = True

        return success

    def _send_to_target(self, target: TargetConfig, message: dict) -> bool:
        """
        Send a command to a specific target.

        :param target: Target configuration
        :param message: Command message dictionary
        :return: True if sent successfully
        """
        self.stats["requests_sent"] += 1
        self.stats["by_target"][target.name]["sent"] += 1

        if target.reference_api and not target._api_config:
            api_config = self._conns_cache.get(target.reference_api)
            if not api_config and self._conns_path:
                self.logger.info(
                    "Loading reference_api '%s' from conns.json (%s)",
                    target.reference_api, self._conns_path
                )
                self._conns_cache = _load_conns_config(
                    self._conns_path,
                    self.logger,
                    extra_base_dirs=self._conns_base_dirs
                )
                api_config = self._conns_cache.get(target.reference_api)
            if not api_config and self._conns_fallback_paths:
                for fallback_path in self._conns_fallback_paths:
                    self.logger.info("Trying fallback conns.json at: %s", fallback_path)
                    self._conns_cache = _load_conns_config(fallback_path, self.logger)
                    api_config = self._conns_cache.get(target.reference_api)
                    if api_config:
                        break
            if api_config:
                target.apply_api_config(api_config)

        try:
            endpoint, request_body = target.build_request(message)
        except ValueError as exc:
            self.logger.error(
                "Target '%s' request build error: %s (reference_api=%s, url='%s').",
                target.name, str(exc), target.reference_api, target.url
            )
            self.stats["requests_failed"] += 1
            self.stats["by_target"][target.name]["failed"] += 1
            return False

        parsed_endpoint = urlparse(endpoint)
        socket_info = parsed_endpoint.netloc or parsed_endpoint.path
        self.logger.info(
            "Target '%s' endpoint resolved: %s (socket: %s)",
            target.name, endpoint, socket_info
        )
        if not parsed_endpoint.scheme or not parsed_endpoint.netloc:
            self.logger.error(
                "Target '%s' has invalid endpoint '%s'. Check reference_api and conns.json.",
                target.name, endpoint
            )
            self.stats["requests_failed"] += 1
            self.stats["by_target"][target.name]["failed"] += 1
            return False

        if isinstance(request_body, dict) and "power" in request_body:
            if not isinstance(request_body.get("power"), bool):
                payload_keys = []
                try:
                    payload_keys = list((message.get("payload") or {}).keys())
                except Exception:
                    payload_keys = []
                self.logger.error(
                    "Target '%s' invalid power type: %s (value=%r, message_keys=%s, payload_keys=%s)",
                    target.name,
                    type(request_body.get("power")).__name__,
                    request_body.get("power"),
                    list(message.keys()),
                    payload_keys
                )
                self.stats["requests_failed"] += 1
                self.stats["by_target"][target.name]["failed"] += 1
                return False

        # Check dry_run:
        # - The message's dry_run flag controls whether HTTP requests are sent
        # - This allows live commands to be forwarded even when forwarder is in dry-run mode
        payload = message.get("payload", {})
        message_dry_run = payload.get("dry_run", True)  # Default to True if not specified
        is_dry_run = message_dry_run  # Let the message control dry_run for forwarding

        self.logger.debug(
            "Forwarding decision for %s: message_dry_run=%s, is_dry_run=%s",
            message.get("asset_id", "?"), message_dry_run, is_dry_run
        )

        if is_dry_run:
            self.logger.info(
                "[DRY-RUN] Would forward to target '%s': POST %s",
                target.name, endpoint
            )
            self.logger.info(
                "[DRY-RUN] Payload for target '%s': %s",
                target.name,
                _format_request_body_for_log(request_body)
            )
            self.stats["requests_success"] += 1
            self.stats["by_target"][target.name]["success"] += 1
            return True

        if not REQUESTS_AVAILABLE:
            self.logger.error("Cannot forward - requests library not installed")
            self.stats["requests_failed"] += 1
            self.stats["by_target"][target.name]["failed"] += 1
            return False

        self.logger.info("Forwarding to target '%s': POST %s", target.name, endpoint)
        self.logger.info(
            "Forwarding payload to target '%s': %s",
            target.name,
            _format_request_body_for_log(request_body)
        )
        request_kwargs = {
            "json": request_body,
            "auth": target.get_auth(),
            "timeout": target.request_timeout_seconds,
            "verify": target.verify_ssl,
        }

        def _post_request():
            if target.verify_ssl or InsecureRequestWarning is None:
                return requests.post(endpoint, **request_kwargs)

            # The target is explicitly configured to skip certificate validation.
            # Suppress urllib3's noisy warning and rely on config-driven behavior.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", InsecureRequestWarning)
                return requests.post(endpoint, **request_kwargs)

        total_attempts = target.request_retries + 1
        last_failure_summary = "unknown error"

        for attempt in range(1, total_attempts + 1):
            try:
                response = _post_request()
                response_text = " ".join((response.text or "").split())

                if response.status_code in [200, 201, 202, 204]:
                    if attempt > 1:
                        self.logger.info(
                            "Target '%s' request succeeded on attempt %d/%d",
                            target.name, attempt, total_attempts
                        )
                    self.logger.info(
                        "Target '%s' responded: %d - %s",
                        target.name, response.status_code, response_text[:200]
                    )
                    self.stats["requests_success"] += 1
                    self.stats["by_target"][target.name]["success"] += 1
                    return True

                last_failure_summary = f"{response.status_code} - {response_text[:200]}"
                if attempt < total_attempts:
                    self.logger.warning(
                        "Target '%s' attempt %d/%d returned error: %s; retrying",
                        target.name, attempt, total_attempts, last_failure_summary
                    )
                    continue
            except requests.exceptions.Timeout:
                last_failure_summary = (
                    f"request timed out after {target.request_timeout_seconds:.1f}s"
                )
                if attempt < total_attempts:
                    self.logger.warning(
                        "Target '%s' attempt %d/%d %s; retrying",
                        target.name, attempt, total_attempts, last_failure_summary
                    )
                    continue
            except requests.exceptions.RequestException as e:
                last_failure_summary = str(e)
                if attempt < total_attempts:
                    self.logger.warning(
                        "Target '%s' attempt %d/%d request failed: %s; retrying",
                        target.name, attempt, total_attempts, last_failure_summary
                    )
                    continue
            except Exception as e:
                last_failure_summary = str(e)
                if attempt < total_attempts:
                    self.logger.warning(
                        "Target '%s' attempt %d/%d request failed: %s; retrying",
                        target.name, attempt, total_attempts, last_failure_summary
                    )
                    continue

            break

        self.logger.error(
            "Target '%s' request failed after %d attempts (%d retries): %s",
            target.name,
            total_attempts,
            target.request_retries,
            last_failure_summary,
        )
        self.stats["requests_failed"] += 1
        self.stats["by_target"][target.name]["failed"] += 1
        return False

    def get_statistics(self) -> dict:
        """Get forwarding statistics."""
        return self.stats


# =============================================================================
# COMMAND HANDLERS (DRY-RUN MODE)
# =============================================================================

class CommandHandler:
    """
    Handles commands received from RabbitMQ.
    
    Supports two modes:
    - dry_run=True: Logs what would be done without actual actuation
    - dry_run=False: Would send commands to actual devices (NOT IMPLEMENTED YET)
    
    The dry_run mode can be:
    - Set globally via --dry-run flag (overrides message flag)
    - Read from each message's payload (if no global override)

    When targets are configured, commands are forwarded to the matching
    HTTP endpoints (e.g., aem-server-simulator).
    """
    
    def __init__(
        self,
        logger: logging.Logger,
        force_dry_run: bool = True,
        target_handler: TargetHandler = None
    ):
        """
        Initialize command handler.
        
        :param logger: Logger instance
        :param force_dry_run: If True, always use dry-run mode regardless of message flag
        :param target_handler: Optional TargetHandler for forwarding to external systems
        """
        self.logger = logger
        self.force_dry_run = force_dry_run
        self.target_handler = target_handler
        self.commands_received = 0
        self.commands_by_type = {}
        self.commands_by_asset = {}

    def _resolve_dry_run(self, message: dict, default: bool = True) -> bool:
        """
        Resolve the effective dry-run mode for a message.

        Command messages carry ``dry_run`` in ``payload`` while batch headers
        carry it in ``slot_info``. When ``force_dry_run`` is enabled it always
        takes precedence.
        """
        if self.force_dry_run:
            return True

        for section in ("payload", "slot_info"):
            data = message.get(section, {})
            if isinstance(data, dict) and "dry_run" in data:
                return data["dry_run"]

        return default

    @staticmethod
    def _mode_label(is_dry_run: bool) -> str:
        """Return the log label for the effective mode."""
        return "[DRY-RUN]" if is_dry_run else "[LIVE]"
    
    def handle_command(self, message: dict) -> bool:
        """
        Handle a command message.
        
        Decides whether to actuate based on:
        1. force_dry_run (global override from --dry-run flag)
        2. dry_run flag in the message payload
        
        :param message: Command message dictionary
        :return: True if handled successfully
        """
        self.commands_received += 1
        
        asset_id = message.get("asset_id", "unknown")
        asset_type = message.get("asset_type", "unknown")
        command_type = message.get("command_type") or message.get("command") or "unknown"
        payload = message.get("payload", {})
        timestamp = message.get("timestamp", "")
        
        # Determine if this should be dry-run
        # Global force_dry_run takes precedence, otherwise use message flag
        is_dry_run = self._resolve_dry_run(message, default=True)
        mode_label = self._mode_label(is_dry_run)
        
        # Update statistics
        self.commands_by_type[command_type] = self.commands_by_type.get(command_type, 0) + 1
        self.commands_by_asset[asset_id] = self.commands_by_asset.get(asset_id, 0) + 1
        
        # Extract slot timing information
        slot_start = payload.get("slot_start", "N/A")
        slot_end = payload.get("slot_end", "N/A")
        
        # Log the command details
        self.logger.info("=" * 60)
        self.logger.info("%s COMMAND RECEIVED #%d", mode_label, self.commands_received)
        self.logger.info("=" * 60)
        self.logger.info("  Asset ID:     %s", asset_id)
        self.logger.info("  Asset Type:   %s", asset_type)
        self.logger.info("  Command:      %s", command_type)
        self.logger.info("-" * 60)
        self.logger.info("  SLOT START:   %s", slot_start)
        self.logger.info("  SLOT END:     %s", slot_end)
        self.logger.info("-" * 60)
        self.logger.info("  Timestamp:    %s", timestamp)
        
        # Log payload details based on command type
        if command_type == "curtail":
            modulation_type = payload.get("modulation_type", "unknown")
            target_power = payload.get("target_power_kw", 0)
            curtailment = payload.get("actual_curtailment_kw", 0)
            duration = payload.get("duration_minutes", 15)
            
            if modulation_type == "discrete":
                state = payload.get("discrete_state", "unknown")
                self.logger.info("  Modulation:   %s (state: %s)", modulation_type, state)
            else:
                self.logger.info("  Modulation:   %s", modulation_type)
            
            self.logger.info("  Curtailment:  %.2f kW", curtailment)
            self.logger.info("  Target Power: %.2f kW", target_power)
            self.logger.info("  Duration:     %d minutes", duration)
            
            # Execute or simulate
            self.logger.info("-" * 60)
            if is_dry_run:
                if modulation_type == "discrete":
                    self.logger.info(
                        "%s Would send %s command to %s (%s) for slot %s",
                        mode_label, state, asset_id, payload.get("description", ""), slot_start
                    )
                else:
                    self.logger.info(
                        "%s Would set power limit to %.2f kW on %s (%s) for slot %s",
                        mode_label, target_power, asset_id, payload.get("description", ""), slot_start
                    )
            else:
                # LIVE MODE - actual actuation would happen here
                # TODO: Implement actual protocol handlers (MQTT, HTTP, Modbus, OCPP)
                self.logger.warning(
                    "%s LIVE ACTUATION NOT IMPLEMENTED - would actuate %s (%s)",
                    mode_label, asset_id, payload.get("description", "")
                )
        
        elif command_type == "restore":
            self.logger.info("-" * 60)
            if is_dry_run:
                self.logger.info(
                    "%s Would restore %s (%s) to normal operation at %s",
                    mode_label, asset_id, payload.get("description", ""), slot_end
                )
            else:
                # LIVE MODE
                self.logger.warning(
                    "%s LIVE RESTORE NOT IMPLEMENTED - would restore %s (%s)",
                    mode_label, asset_id, payload.get("description", "")
                )
        
        else:
            # Log payload only at DEBUG level
            self.logger.debug("  Payload: %s", json.dumps(payload, indent=4))
        
        self.logger.info("=" * 60)

        # Forward to configured targets (if any)
        if self.target_handler and self.target_handler.targets:
            self.logger.info("-" * 60)
            self.logger.info("FORWARDING TO TARGETS...")
            forward_success = self.target_handler.forward_command(message)
            if forward_success:
                self.logger.info("Forwarding completed successfully")
            else:
                self.logger.warning("Forwarding failed for some targets")
            self.logger.info("=" * 60)

        return True
    
    def handle_measurement(self, message: dict) -> bool:
        """
        Handle a measurement message.
        
        :param message: Measurement message dictionary
        :return: True if handled successfully
        """
        asset_id = message.get("asset_id", "unknown")
        asset_type = message.get("asset_type", "unknown")
        measurement_type = message.get("measurement_type", "unknown")
        payload = message.get("payload", {})
        timestamp = message.get("timestamp", "")
        is_dry_run = self._resolve_dry_run(message, default=False)
        mode_label = self._mode_label(is_dry_run)
        
        self.logger.info("-" * 40)
        self.logger.info("%s MEASUREMENT RECEIVED", mode_label)
        self.logger.info("  Asset ID:     %s", asset_id)
        self.logger.info("  Type:         %s", measurement_type)
        self.logger.info("  Timestamp:    %s", timestamp)
        self.logger.info("  Payload:      %s", json.dumps(payload))
        self.logger.info("-" * 40)
        
        return True
    
    def handle_batch_header(self, message: dict) -> bool:
        """
        Handle a batch header message.
        
        :param message: Batch header message dictionary
        :return: True if handled successfully
        """
        slot_info = message.get("slot_info", {})
        command_count = message.get("command_count", 0)
        timestamp = message.get("timestamp", "")
        is_dry_run = self._resolve_dry_run(message, default=False)
        mode_label = self._mode_label(is_dry_run)
        
        self.logger.info("*" * 60)
        self.logger.info("%s BATCH START - Expecting %d commands", mode_label, command_count)
        self.logger.info("*" * 60)
        self.logger.info("  FSP ID:       %s", slot_info.get("fsp_id", "unknown"))
        self.logger.info("  Slot Start:   %s", slot_info.get("slot_start", ""))
        self.logger.info("  Slot End:     %s", slot_info.get("slot_end", ""))
        self.logger.info("  Total Flex:   %.2f kW", slot_info.get("total_flexibility_kw", 0))
        self.logger.info("  Strategy:     %s", slot_info.get("allocation_strategy", ""))
        self.logger.info("  Dry Run:      %s", is_dry_run)
        self.logger.info("*" * 60)
        
        return True
    
    def get_statistics(self) -> dict:
        """Get statistics about processed commands."""
        return {
            "total_commands": self.commands_received,
            "by_type": self.commands_by_type,
            "by_asset": self.commands_by_asset
        }


# =============================================================================
# RABBITMQ CONSUMER
# =============================================================================

@dataclass(frozen=True)
class RabbitMQSource:
    """A section-based RabbitMQ source configured under conns.json rabbitMQ."""

    section: str
    exchange: str
    queue: str
    routing_key: str


_RABBITMQ_SOURCE_FIELDS = ("exchange", "queue", "routingKey")


def _parse_rabbitmq_sections(
    requested_sections: Optional[Union[str, Iterable[str]]]
) -> Optional[List[str]]:
    """Normalize requested RabbitMQ section names from CLI/env/test inputs."""
    if requested_sections is None:
        return None

    if isinstance(requested_sections, str):
        raw_sections = requested_sections.split(",")
    else:
        raw_sections = list(requested_sections)

    return [str(section).strip() for section in raw_sections if str(section).strip()]


def _rabbitmq_source_from_section(section: str, section_cfg: Any) -> RabbitMQSource:
    """Build a RabbitMQSource or raise ValueError with a startup-safe message."""
    if section_cfg is None:
        raise ValueError(f"rabbitMQ.{section} section not found")
    if not isinstance(section_cfg, dict):
        raise ValueError(f"rabbitMQ.{section} must be an object")

    missing_fields = [
        field
        for field in _RABBITMQ_SOURCE_FIELDS
        if section_cfg.get(field) is None or not str(section_cfg.get(field)).strip()
    ]
    if missing_fields:
        raise ValueError(
            f"rabbitMQ.{section} missing required field(s): {', '.join(missing_fields)}"
        )

    return RabbitMQSource(
        section=section,
        exchange=str(section_cfg["exchange"]).strip(),
        queue=str(section_cfg["queue"]).strip(),
        routing_key=str(section_cfg["routingKey"]).strip(),
    )


def _is_section_like_rabbitmq_config(section_cfg: Any) -> bool:
    """Return True when a rabbitMQ child looks like an exchange/queue section."""
    return (
        isinstance(section_cfg, dict)
        and any(field in section_cfg for field in _RABBITMQ_SOURCE_FIELDS)
    )


def resolve_rabbitmq_sources(
    rabbitmq_cfg: dict,
    requested_sections: Optional[Union[str, Iterable[str]]] = None,
    logger: Optional[logging.Logger] = None,
) -> List[RabbitMQSource]:
    """
    Resolve RabbitMQ source sections from conns.json rabbitMQ configuration.

    Explicitly requested sections are strict and fail startup when missing or
    malformed. Without an explicit request, all valid section-like entries are
    consumed and malformed optional entries are skipped with a warning.
    """
    logger = logger or logging.getLogger("forwarder")
    if not isinstance(rabbitmq_cfg, dict):
        raise ValueError("rabbitMQ configuration must be an object")

    sections = _parse_rabbitmq_sections(requested_sections)

    if sections is not None:
        if not sections:
            raise ValueError("No RabbitMQ sections requested")
        return [
            _rabbitmq_source_from_section(section, rabbitmq_cfg.get(section))
            for section in sections
        ]

    sources: List[RabbitMQSource] = []
    for section, section_cfg in rabbitmq_cfg.items():
        if not isinstance(section_cfg, dict):
            continue
        if not _is_section_like_rabbitmq_config(section_cfg):
            continue
        try:
            sources.append(_rabbitmq_source_from_section(section, section_cfg))
        except ValueError as exc:
            logger.warning("Skipping malformed RabbitMQ source section: %s", str(exc))

    if not sources:
        raise ValueError(
            "No valid RabbitMQ sources configured under rabbitMQ; "
            "add section objects with exchange, queue, and routingKey "
            "or set FORWARDER_RABBIT_SECTIONS/--rabbit-sections"
        )

    return sources


class RabbitMQConsumer:
    """
    Consumes messages from RabbitMQ and dispatches them to handlers.
    
    Subscribes to one or more section-based RabbitMQ sources configured under
    conns.json rabbitMQ.
    """
    
    DEFAULT_EXCHANGE = "flexi_commands"
    DEFAULT_QUEUE_COMMANDS = "asset_commands"
    DEFAULT_QUEUE_MEASUREMENTS = "asset_measurements"
    
    def __init__(
        self,
        sources: List[RabbitMQSource],
        host: str = "localhost",
        port: int = 5672,
        username: str = "guest",
        password: str = "guest",
        virtual_host: str = "/",
        exchange: str = None,
        logger: logging.Logger = None,
        command_handler: Callable = None,
        measurement_handler: Callable = None,
        asset_types_filter: List[str] = None
    ):
        """
        Initialize RabbitMQ consumer.
        
        :param sources: RabbitMQ source sections to consume
        :param host: RabbitMQ server hostname
        :param port: RabbitMQ server port
        :param username: RabbitMQ username
        :param password: RabbitMQ password
        :param virtual_host: RabbitMQ virtual host
        :param exchange: Legacy exchange option, ignored when sources are provided
        :param logger: Logger instance
        :param command_handler: Callback function for command messages
        :param measurement_handler: Callback function for measurement messages
        :param asset_types_filter: List of asset types to process (None = all)
        """
        if not sources:
            raise ValueError("RabbitMQConsumer requires at least one RabbitMQSource")

        self.sources = list(sources)
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.virtual_host = virtual_host
        self.exchange = exchange or self.DEFAULT_EXCHANGE
        self.logger = logger or logging.getLogger(__name__)
        
        self.command_handler = command_handler
        self.measurement_handler = measurement_handler
        self.asset_types_filter = asset_types_filter
        
        self.connection = None
        self.channel = None
        self._running = False
        self._messages_processed = 0
        self._consumer_tag_sources: Dict[str, RabbitMQSource] = {}
    
    def connect(self) -> bool:
        """
        Establish connection to RabbitMQ server.
        
        :return: True if connected successfully
        """
        try:
            credentials = pika.PlainCredentials(self.username, self.password)
            parameters = pika.ConnectionParameters(
                host=self.host,
                port=self.port,
                virtual_host=self.virtual_host,
                credentials=credentials,
                heartbeat=600,
                blocked_connection_timeout=300
            )
            
            self.connection = pika.BlockingConnection(parameters)
            self.channel = self.connection.channel()
            
            for source in self.sources:
                self.channel.exchange_declare(
                    exchange=source.exchange,
                    exchange_type='topic',
                    durable=True
                )
                self.channel.queue_declare(
                    queue=source.queue,
                    durable=True
                )
                self.channel.queue_bind(
                    exchange=source.exchange,
                    queue=source.queue,
                    routing_key=source.routing_key
                )
            
            # Set QoS (prefetch count)
            self.channel.basic_qos(prefetch_count=1)
            
            self.logger.info(
                "Connected to RabbitMQ at %s:%d with %d source(s)",
                self.host, self.port, len(self.sources)
            )
            return True
            
        except Exception as e:
            self.logger.error("Failed to connect to RabbitMQ: %s", str(e))
            return False
    
    def disconnect(self):
        """Close RabbitMQ connection."""
        self._running = False
        if self.connection and self.connection.is_open:
            try:
                self.connection.close()
                self.logger.info("Disconnected from RabbitMQ")
            except Exception as e:
                self.logger.warning("Error closing RabbitMQ connection: %s", str(e))
    
    def _process_message(self, ch, method, properties, body):
        """
        Process a received message.
        
        :param ch: Channel
        :param method: Method frame
        :param properties: Message properties
        :param body: Message body
        """
        try:
            consumer_tag = getattr(method, "consumer_tag", None)
            source = self._consumer_tag_sources.get(consumer_tag)
            queue_name = source.queue if source else "unknown"
            section = source.section if source else "unknown"
            self.logger.debug(
                "Received RabbitMQ message: queue=%s section=%s delivery_tag=%s routing_key=%s",
                queue_name,
                section,
                getattr(method, "delivery_tag", None),
                getattr(method, "routing_key", None),
            )

            message = json.loads(body.decode('utf-8'))
            message_type = message.get("message_type", "unknown")
            asset_type = message.get("asset_type", "")
            
            # Apply asset type filter
            if self.asset_types_filter and asset_type:
                if asset_type not in self.asset_types_filter:
                    self.logger.debug(
                        "Skipping message for asset type '%s' (not in filter)",
                        asset_type
                    )
                    ch.basic_ack(delivery_tag=method.delivery_tag)
                    return
            
            # Dispatch to appropriate handler
            handled = False
            
            if message_type == "command":
                if self.command_handler:
                    handled = self.command_handler(message)
                else:
                    self.logger.warning("No command handler configured")
            
            elif message_type == "measurement":
                if self.measurement_handler:
                    handled = self.measurement_handler(message)
                else:
                    self.logger.debug("No measurement handler configured")
                    handled = True  # Don't fail on missing measurement handler
            
            elif message_type == "batch_start":
                # Batch header - call command handler with special handling
                if self.command_handler and hasattr(self.command_handler, '__self__'):
                    handler_obj = self.command_handler.__self__
                    if hasattr(handler_obj, 'handle_batch_header'):
                        handled = handler_obj.handle_batch_header(message)
                    else:
                        handled = True
                else:
                    handled = True
            
            else:
                self.logger.warning("Unknown message type: %s", message_type)
                handled = True  # Acknowledge to avoid requeueing unknown messages
            
            # Acknowledge message
            if handled:
                ch.basic_ack(delivery_tag=method.delivery_tag)
                self._messages_processed += 1
            else:
                # Negative acknowledge - requeue message
                ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
                self.logger.warning("Message not handled, requeueing")
            
        except json.JSONDecodeError as e:
            self.logger.error("Invalid JSON in message: %s", str(e))
            ch.basic_ack(delivery_tag=method.delivery_tag)  # Ack to avoid infinite loop
        except Exception as e:
            self.logger.error("Error processing message: %s", str(e))
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
    
    def start_consuming(self):
        """
        Start consuming messages from all configured source queues.
        """
        self._running = True
        
        for source in self.sources:
            consumer_tag = self.channel.basic_consume(
                queue=source.queue,
                on_message_callback=self._process_message,
                auto_ack=False
            )
            if consumer_tag:
                self._consumer_tag_sources[consumer_tag] = source
            self.logger.info(
                "Consuming from RabbitMQ source %s: queue=%s",
                source.section,
                source.queue,
            )
        
        self.logger.info("Waiting for messages... (Press Ctrl+C to stop)")
        
        try:
            while self._running:
                self.connection.process_data_events(time_limit=1)
        except KeyboardInterrupt:
            self.logger.info("Received interrupt signal")
        finally:
            self.disconnect()
    
    def get_messages_processed(self) -> int:
        """Get count of messages processed."""
        return self._messages_processed


# =============================================================================
# CONNS.JSON LOADER
# =============================================================================

def _load_conns_config(path: str, logger: logging.Logger, extra_base_dirs: Optional[List[str]] = None) -> dict:
    """Load conns.json configuration if present."""
    if not path:
        return {}

    candidate_paths = []
    if os.path.isabs(path):
        candidate_paths.append(path)
    else:
        script_dir = os.path.dirname(__file__)
        candidate_paths.append(os.path.join(script_dir, path))
        candidate_paths.append(os.path.join(os.getcwd(), path))
        for base_dir in extra_base_dirs or []:
            candidate_paths.append(os.path.join(base_dir, path))

    candidate_paths = [os.path.normpath(p) for p in candidate_paths]

    for config_path in candidate_paths:
        if not os.path.exists(config_path):
            logger.debug("conns.json not found at: %s", config_path)
            continue
        try:
            with open(config_path, 'r') as f:
                data = json.load(f)
            logger.info("Loaded conns.json from %s (keys: %s)", config_path, sorted(data.keys()))
            return data
        except Exception as e:
            logger.error("Failed to load conns.json from %s: %s", config_path, str(e))
            return {}

    logger.warning("conns.json not found. Paths tried: %s", candidate_paths)
    return {}


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Forwarder - Receive and forward flexibility commands from RabbitMQ",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Start forwarder with default config file (conf/forwarder_targets.json)
  python forwarder.py

  # Start forwarder with custom config file
  python forwarder.py --config ../conf/forwarder_targets.json

  # Start in live mode (actually forward commands)
  python forwarder.py --live --config ../conf/forwarder_targets.json

  # Listen with verbose logging
  python forwarder.py --log-level DEBUG

  # Filter by asset types
  python forwarder.py --asset-types heat_pump,ev_charger

  # Custom RabbitMQ server
  python forwarder.py --rabbitmq-host rabbitmq.local
        """
    )
    
    parser.add_argument(
        "--dry-run", "-d",
        action="store_true",
        default=(get_env("FORWARDER_MODE", "dry-run").lower() == "dry-run"),
        help="Dry-run mode - log commands without actuating (default, env: FORWARDER_MODE)"
    )
    parser.add_argument(
        "--live", "-l",
        action="store_true",
        default=(get_env("FORWARDER_MODE", "dry-run").lower() == "live"),
        help="Live mode - actually forward commands (env: FORWARDER_MODE=live)"
    )
    parser.add_argument(
        "--asset-types",
        default=get_env("FORWARDER_ASSET_TYPES"),
        help="Comma-separated list of asset types to process (env: FORWARDER_ASSET_TYPES)"
    )
    parser.add_argument(
        "--queues",
        default=get_env("FORWARDER_QUEUES"),
        help="Legacy queue selector ignored for section-based sources (env: FORWARDER_QUEUES)"
    )
    parser.add_argument(
        "--rabbit-sections",
        default=get_env("FORWARDER_RABBIT_SECTIONS"),
        help=(
            "Comma-separated rabbitMQ sections to consume, e.g. "
            "realAssetCommands,simulatedAssetCommands,simulatedAssetMeasures "
            "(env: FORWARDER_RABBIT_SECTIONS)"
        )
    )
    
    # RabbitMQ arguments (with environment variable defaults for Docker)
    parser.add_argument(
        "--rabbitmq-host",
        default=get_env("RABBITMQ_HOST"),
        help="RabbitMQ server hostname (env: RABBITMQ_HOST, default: rabbitMQ.host or localhost)"
    )
    parser.add_argument(
        "--rabbitmq-port",
        default=get_env("RABBITMQ_PORT"),
        help="RabbitMQ server port (env: RABBITMQ_PORT, default: rabbitMQ.port or 5672)"
    )
    parser.add_argument(
        "--rabbitmq-user",
        default=get_env("RABBITMQ_USER"),
        help="RabbitMQ username (env: RABBITMQ_USER, default: rabbitMQ.username or guest)"
    )
    parser.add_argument(
        "--rabbitmq-pass",
        default=get_env("RABBITMQ_PASS"),
        help="RabbitMQ password (env: RABBITMQ_PASS, default: rabbitMQ.password or guest)"
    )
    parser.add_argument(
        "--rabbitmq-vhost",
        default=get_env("RABBITMQ_VHOST"),
        help="RabbitMQ virtual host (env: RABBITMQ_VHOST, default: rabbitMQ.virtualHost or /)"
    )
    parser.add_argument(
        "--rabbitmq-exchange",
        default=get_env("RABBITMQ_EXCHANGE"),
        help="Legacy RabbitMQ exchange option ignored for section-based sources (env: RABBITMQ_EXCHANGE)"
    )
    
    # Logging arguments
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default=get_env("FORWARDER_LOG_LEVEL", "INFO"),
        help="Logging level (env: FORWARDER_LOG_LEVEL)"
    )
    parser.add_argument(
        "--log-file",
        default=get_env("FORWARDER_LOG_FILE"),
        help="Path to log file (env: FORWARDER_LOG_FILE)"
    )
    
    # Target configuration arguments
    parser.add_argument(
        "--config", "-c",
        default=get_env("FORWARDER_CONFIG", "../conf/forwarder_targets.json"),
        help="Path to targets configuration file (env: FORWARDER_CONFIG)"
    )
    parser.add_argument(
        "--conns",
        default=get_env("FORWARDER_CONNS", "../conf/private/conns.json"),
        help="Path to conns.json file (env: FORWARDER_CONNS)"
    )

    args = parser.parse_args()
    
    # Setup logging
    logger = setup_logging(args.log_level, args.log_file)
    
    # Determine mode
    # --dry-run (default) forces all commands to dry-run regardless of message flag
    # --live allows respecting the dry_run flag from each message
    force_dry_run = not args.live
    
    logger.info("=" * 60)
    logger.info("FORWARDER - Command/Measurement Forwarding Service")
    logger.info("=" * 60)
    if force_dry_run:
        logger.info("Mode: DRY-RUN (forced - all commands will be simulated)")
    else:
        logger.info("Mode: LIVE (actuation mode determined by each message)")
        logger.warning("WARNING: Live actuation is NOT YET IMPLEMENTED!")
    logger.info("-" * 60)
    
    # Parse asset types filter
    asset_types_filter = None
    if args.asset_types:
        asset_types_filter = [t.strip() for t in args.asset_types.split(",")]
        logger.info("Asset type filter: %s", asset_types_filter)
    
    if args.queues:
        logger.warning(
            "FORWARDER_QUEUES / --queues is ignored for section-based RabbitMQ sources."
        )
    
    # Create target handler for forwarding to external systems
    target_handler = TargetHandler(logger, dry_run=force_dry_run)

    # Resolve targets config path
    config_path = args.config
    if not os.path.isabs(config_path):
        config_path = os.path.join(os.path.dirname(__file__), config_path)
    config_dir = os.path.dirname(config_path)
    fallback_conns_path = os.path.join(config_dir, "private", "conns.json")
    fallback_conns_loaded = False

    # Load conns.json (optional, used for reference_api targets)
    logger.info("Using conns.json path: %s", args.conns)
    conns_config = _load_conns_config(args.conns, logger, extra_base_dirs=[config_dir])
    target_handler.set_conns_lookup(args.conns, [config_dir], conns_config, [fallback_conns_path])
    rabbitmq_cfg = conns_config.get("rabbitMQ", {})

    try:
        rabbitmq_sources = resolve_rabbitmq_sources(
            rabbitmq_cfg,
            requested_sections=args.rabbit_sections,
            logger=logger,
        )
    except ValueError as exc:
        logger.error("Invalid RabbitMQ source configuration: %s", str(exc))
        sys.exit(1)

    rabbitmq_host = (
        args.rabbitmq_host
        if args.rabbitmq_host is not None
        else rabbitmq_cfg.get("host", "localhost")
    )
    rabbitmq_port_value = (
        args.rabbitmq_port
        if args.rabbitmq_port is not None
        else rabbitmq_cfg.get("port", 5672)
    )
    try:
        rabbitmq_port = int(rabbitmq_port_value or 5672)
    except (TypeError, ValueError):
        logger.error("Invalid RabbitMQ port value: %r", rabbitmq_port_value)
        sys.exit(1)
    rabbitmq_user = (
        args.rabbitmq_user
        if args.rabbitmq_user is not None
        else rabbitmq_cfg.get("username", "guest")
    )
    rabbitmq_password = (
        args.rabbitmq_pass
        if args.rabbitmq_pass is not None
        else rabbitmq_cfg.get("password", "guest")
    )
    rabbitmq_vhost = (
        args.rabbitmq_vhost
        if args.rabbitmq_vhost is not None
        else rabbitmq_cfg.get("virtualHost", "/")
    )

    logger.info("RabbitMQ: %s:%d (vhost: %s)", rabbitmq_host, rabbitmq_port, rabbitmq_vhost)
    if args.rabbitmq_exchange:
        logger.warning(
            "RABBITMQ_EXCHANGE / --rabbitmq-exchange is ignored for section-based RabbitMQ sources."
        )
    for source in rabbitmq_sources:
        logger.info(
            "Consuming RabbitMQ source %s: exchange=%s, queue=%s, routing_key=%s",
            source.section,
            source.exchange,
            source.queue,
            source.routing_key,
        )

    # Load targets from config file
    if os.path.exists(config_path):
        try:
            with open(config_path, 'r') as f:
                targets_config = json.load(f)

            default_request_timeout_seconds = _coerce_request_timeout_seconds(
                targets_config.get("request_timeout_seconds")
                if targets_config.get("request_timeout_seconds") is not None
                else targets_config.get("default_timeout"),
                DEFAULT_REQUEST_TIMEOUT_SECONDS,
            )
            default_request_retries = _coerce_request_retries(
                targets_config.get("request_retries")
                if targets_config.get("request_retries") is not None
                else targets_config.get("max_retries"),
                DEFAULT_REQUEST_RETRIES,
            )

            for target_data in targets_config.get("targets", []):
                target_config = TargetConfig.from_dict(
                    target_data,
                    default_request_timeout_seconds=default_request_timeout_seconds,
                    default_request_retries=default_request_retries,
                )
                reference_api = target_config.reference_api
                if reference_api:
                    api_config = conns_config.get(reference_api)
                    if not api_config and not fallback_conns_loaded and os.path.exists(fallback_conns_path):
                        logger.info("Attempting fallback conns.json at: %s", fallback_conns_path)
                        conns_config = _load_conns_config(fallback_conns_path, logger)
                        target_handler.set_conns_lookup(fallback_conns_path, [config_dir], conns_config, [fallback_conns_path])
                        fallback_conns_loaded = True
                        api_config = conns_config.get(reference_api)
                    if api_config:
                        target_config.apply_api_config(api_config)
                        logger.info(
                            "Target '%s' reference_api '%s' resolved controlUrl '%s' (user: %s)",
                            target_config.name,
                            reference_api,
                            target_config.url or "",
                            target_config.auth_user or ""
                        )
                    else:
                        logger.warning(
                            "Target '%s' references unknown API '%s' in conns.json",
                            target_config.name, reference_api
                        )
                else:
                    logger.debug("Target '%s' has no reference_api configured", target_config.name)
                target_handler.add_target(target_config)

            logger.info("Loaded %d target(s) from config file: %s",
                       len(target_handler.targets), config_path)
        except Exception as e:
            logger.error("Failed to load targets config from %s: %s", config_path, str(e))
    else:
        logger.warning("Config file not found: %s", config_path)
        logger.info("No forwarding targets configured - commands will only be logged")

    if target_handler.targets:
        logger.info("-" * 60)
        logger.info("Configured forwarding targets:")
        for name, target in target_handler.targets.items():
            logger.info("  - %s: %s (auth: %s)",
                       name, target.url,
                       "enabled" if target.get_auth() else "disabled")
        logger.info("-" * 60)

    # Create handler
    handler = CommandHandler(logger, force_dry_run=force_dry_run, target_handler=target_handler)

    # Create consumer
    consumer = RabbitMQConsumer(
        sources=rabbitmq_sources,
        host=rabbitmq_host,
        port=rabbitmq_port,
        username=rabbitmq_user,
        password=rabbitmq_password,
        virtual_host=rabbitmq_vhost,
        logger=logger,
        command_handler=handler.handle_command,
        measurement_handler=handler.handle_measurement,
        asset_types_filter=asset_types_filter
    )
    
    # Setup signal handlers for graceful shutdown
    def signal_handler(signum, frame):
        logger.info("Received signal %d, shutting down...", signum)
        consumer.disconnect()
        
        # Print statistics
        stats = handler.get_statistics()
        logger.info("=" * 60)
        logger.info("FORWARDER SHUTDOWN - Statistics")
        logger.info("=" * 60)
        logger.info("Total commands processed: %d", stats["total_commands"])
        if stats["by_type"]:
            logger.info("Commands by type:")
            for cmd_type, count in stats["by_type"].items():
                logger.info("  %s: %d", cmd_type, count)
        if stats["by_asset"]:
            logger.info("Commands by asset:")
            for asset_id, count in stats["by_asset"].items():
                logger.info("  %s: %d", asset_id, count)
        logger.info("=" * 60)
        
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Connect with retry (useful for Docker where RabbitMQ might not be ready yet)
    max_retries = int(get_env("RABBITMQ_CONNECT_RETRIES", "10"))
    retry_delay = int(get_env("RABBITMQ_CONNECT_RETRY_DELAY", "5"))
    
    connected = False
    for attempt in range(1, max_retries + 1):
        logger.info("Connecting to RabbitMQ (attempt %d/%d)...", attempt, max_retries)
        if consumer.connect():
            connected = True
            break
        else:
            if attempt < max_retries:
                logger.warning("Connection failed, retrying in %d seconds...", retry_delay)
                time.sleep(retry_delay)
            else:
                logger.error("Failed to connect to RabbitMQ after %d attempts", max_retries)
    
    if not connected:
        sys.exit(1)
    
    try:
        consumer.start_consuming()
    except Exception as e:
        logger.error("Error during consumption: %s", str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
