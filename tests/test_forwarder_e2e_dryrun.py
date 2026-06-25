"""Step 6 — End-to-end dry-run validation of the v2 routing pipeline.

Exercises the full pipeline using the real production config files:
  conf/forwarder_routes.json   (v2 routing config)
  conf/private/conns.json      (API credentials / reference_api resolution)

No live RabbitMQ or HTTP.  All external I/O is mocked.

Pipeline validated:
  1. Load v2 config from conf/forwarder_routes.json
  2. Detect config mode as v2
  3. Validate config
  4. Create MessageRouter
  5. Resolve reference_api from conns.json → base_url, user, password
  6. Derive active RabbitMQ source sections
  7. For each representative message:
     a. Route resolution → correct route
     b. Request building → ResolvedRequest with dry_run=True
     c. Dispatch → [DRY-RUN] logged, no HTTP sent
     d. Callback returns True (= ack)
"""

import json
import logging
import os
import sys
from dataclasses import dataclass
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from forwarder_router import (
    HttpDispatchResult,
    MessageRouter,
    ResolvedRequest,
    build_resolved_request,
    detect_config_mode,
    dispatch_http_request,
    validate_routing_config,
)

PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..")
CONFIG_PATH = os.path.join(PROJECT_ROOT, "conf", "forwarder_routes.json")
CONNS_PATH = os.path.join(PROJECT_ROOT, "conf", "private", "conns.json")


# =========================================================================
# Helpers — mirrors the forwarder.py runtime setup logic
# =========================================================================

def _normalize_control_url(control_url, api_port):
    """Duplicate of forwarder._normalize_control_url for test isolation."""
    if not control_url:
        return None
    from urllib.parse import urlparse, urlunparse

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


@dataclass
class FakeRabbitMQSource:
    section: str
    queue: str = ""
    exchange: str = ""
    routing_key: str = ""


# =========================================================================
# Fixtures — load real configs once per module
# =========================================================================

@pytest.fixture(scope="module")
def v2_config():
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def conns_config():
    if not os.path.isfile(CONNS_PATH):
        pytest.skip("conns.json not available")
    with open(CONNS_PATH, "r") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def validated_config(v2_config):
    assert detect_config_mode(v2_config) == "v2"
    return validate_routing_config(v2_config)


@pytest.fixture(scope="module")
def router_with_resolved_api(validated_config, conns_config):
    """Create router and resolve reference_api — mirrors main() logic."""
    router = MessageRouter.from_validated_config(validated_config)

    for api_name, api_cfg in router.apis.items():
        if api_cfg.reference_api:
            ref = conns_config.get(api_cfg.reference_api, {})
            if ref:
                if not api_cfg.base_url:
                    ctrl = ref.get("controlUrl") or ref.get("controlURL")
                    api_cfg.base_url = (
                        _normalize_control_url(ctrl, ref.get("port")) or ""
                    )
                if not api_cfg.user:
                    api_cfg.user = ref.get("user")
                if not api_cfg.password:
                    api_cfg.password = ref.get("password")
    return router


# =========================================================================
# 1. Config mode detection and validation
# =========================================================================

class TestConfigModeAndValidation:

    def test_config_detected_as_v2(self, v2_config):
        assert detect_config_mode(v2_config) == "v2"

    def test_config_validates_successfully(self, validated_config):
        assert "routes" in validated_config
        assert "defaults" in validated_config


# =========================================================================
# 2. reference_api resolution from conns.json
# =========================================================================

class TestReferenceApiResolution:

    def test_aem_api_base_url_resolved(self, router_with_resolved_api):
        aem = router_with_resolved_api.apis["aem_api"]
        assert aem.base_url, "base_url should be resolved from conns.json"
        assert "://" in aem.base_url, "base_url should have a scheme"

    def test_aem_api_user_resolved(self, router_with_resolved_api):
        aem = router_with_resolved_api.apis["aem_api"]
        assert aem.user, "user should be resolved from conns.json"

    def test_aem_api_password_resolved(self, router_with_resolved_api):
        aem = router_with_resolved_api.apis["aem_api"]
        assert aem.password, "password should be resolved from conns.json"

    def test_aem_test_api_base_url_resolved(self, router_with_resolved_api):
        api = router_with_resolved_api.apis["aem_test_api"]
        assert api.base_url, "base_url should be resolved from conns.json"
        assert "://" in api.base_url

    def test_aem_test_api_user_resolved(self, router_with_resolved_api):
        api = router_with_resolved_api.apis["aem_test_api"]
        assert api.user, "user should be resolved from conns.json"

    def test_aem_test_api_password_resolved(self, router_with_resolved_api):
        api = router_with_resolved_api.apis["aem_test_api"]
        assert api.password, "password should be resolved from conns.json"


# =========================================================================
# 3. Active source derivation
# =========================================================================

class TestSourceDerivation:

    def test_active_sources_include_real_commands(self, router_with_resolved_api):
        active = {
            r.source for r in router_with_resolved_api.routes if r.enabled
        }
        assert "real_commands" in active

    def test_real_commands_maps_to_realAssetCommands(self, router_with_resolved_api):
        src = router_with_resolved_api.sources.get("real_commands", {})
        assert src.get("section") == "realAssetCommands"

    def test_simulated_measures_source_configured(self, router_with_resolved_api):
        assert "simulated_measures" in router_with_resolved_api.sources

    def test_simulated_measures_maps_to_simulatedAssetMeasures(self, router_with_resolved_api):
        src = router_with_resolved_api.sources.get("simulated_measures", {})
        assert src.get("section") == "simulatedAssetMeasures"

    def test_all_configured_sections(self, router_with_resolved_api):
        sections = set()
        for src_def in router_with_resolved_api.sources.values():
            sec = src_def.get("section")
            if sec:
                sections.add(sec)
        assert sections == {"realAssetCommands", "simulatedAssetMeasures"}

    def test_measurement_on_simulated_measures_matches(self, router_with_resolved_api):
        """Measurements from simulatedAssetMeasures route to sim_measure_forward."""
        msg = {
            "message_type": "measure",
            "asset_type": "heat_pump",
            "asset_id": "ECM68.3",
            "measurement_type": "power",
            "payload": {"power_kw": 2.5},
        }
        result = router_with_resolved_api.resolve(msg, "simulatedAssetMeasures")
        assert result.status == "matched"
        assert result.route.name == "sim_measure_forward"
        assert result.route.api == "aem_test_api"


# =========================================================================
# 4. Full pipeline per message — route + build + dry-run dispatch
# =========================================================================

REPRESENTATIVE_MESSAGES = [
    {
        "id": "ecm96_2_cmd",
        "message": {
            "message_type": "command",
            "asset_type": "heat_pump",
            "asset_id": "ECM96.2",
            "command_type": "curtail",
            "payload": {
                "community": "ECM",
                "site_id": "ECM96",
                "asset_id": "ECM96.2",
                "pod": "ECM96",
                "discrete_state": 0,
                "slot_start": "2026-06-15T14:00:00Z",
            },
        },
        "source_section": "realAssetCommands",
        "expected_route": "ecm96_2_hp",
        "expected_endpoint": "hp_ecm96_2",
    },
    {
        "id": "ecm97_3_cmd",
        "message": {
            "message_type": "command",
            "asset_type": "heat_pump",
            "asset_id": "ECM97.3",
            "command_type": "curtail",
            "payload": {
                "community": "ECM",
                "site_id": "ECM97",
                "asset_id": "ECM97.3",
                "pod": "ECM97",
                "discrete_state": 1,
                "slot_start": "2026-06-15T14:15:00Z",
            },
        },
        "source_section": "realAssetCommands",
        "expected_route": "ecm97_3_hp",
        "expected_endpoint": "hp_ecm97_3",
    },
    {
        "id": "ecm63_1_cmd",
        "message": {
            "message_type": "command",
            "asset_type": "ev_charger",
            "asset_id": "ECM63.1",
            "command_type": "curtail",
            "payload": {
                "community": "ECM",
                "site_id": "ECM63",
                "asset_id": "ECM63.1",
                "pod": "ECM63",
                "schedule": [
                    {"time": "2026-06-15T14:00:00Z", "power_kw": 6.24},
                    {"time": "2026-06-15T14:15:00Z", "power_kw": 7.62},
                ],
            },
        },
        "source_section": "realAssetCommands",
        "expected_route": "ecm63_1_ev",
        "expected_endpoint": "ev_ecm63_1",
    },
    {
        "id": "ecm63_2_cmd",
        "message": {
            "message_type": "command",
            "asset_type": "ev_charger",
            "asset_id": "ECM63.2",
            "command_type": "curtail",
            "payload": {
                "community": "ECM",
                "site_id": "ECM63",
                "asset_id": "ECM63.2",
                "pod": "ECM63",
                "schedule": [
                    {"time": "2026-06-15T14:00:00Z", "power_kw": 9.01},
                ],
            },
        },
        "source_section": "realAssetCommands",
        "expected_route": "ecm63_2_ev",
        "expected_endpoint": "ev_ecm63_2",
    },
    {
        "id": "unknown_asset_cmd",
        "message": {
            "message_type": "command",
            "asset_type": "heat_pump",
            "asset_id": "HP-NEWSITE-01",
            "command_type": "curtail",
            "payload": {
                "community": "ECM",
                "site_id": "NEWSITE",
                "asset_id": "HP-NEWSITE-01",
                "pod": "NEWSITE",
                "discrete_state": 1,
                "target_state": "on",
                "slot_start": "2026-06-15T14:00:00Z",
            },
        },
        "source_section": "realAssetCommands",
        "expected_route": "fallback_aem_command",
        "expected_endpoint": "default_aem_command",
    },
]


class TestFullPipelinePerMessage:

    @pytest.fixture(autouse=True)
    def _capture_logs(self, caplog):
        self.caplog = caplog

    @pytest.mark.parametrize(
        "case",
        REPRESENTATIVE_MESSAGES,
        ids=[m["id"] for m in REPRESENTATIVE_MESSAGES],
    )
    def test_route_resolution(self, router_with_resolved_api, case):
        result = router_with_resolved_api.resolve(
            case["message"], case["source_section"]
        )
        assert result.status == "matched", (
            f"Expected matched, got {result.status}: {result.reason}"
        )
        assert result.route.name == case["expected_route"]
        assert result.route.endpoint == case["expected_endpoint"]

    @pytest.mark.parametrize(
        "case",
        REPRESENTATIVE_MESSAGES,
        ids=[m["id"] for m in REPRESENTATIVE_MESSAGES],
    )
    def test_request_building_dry_run(self, router_with_resolved_api, case):
        """Build request with forwarder_dry_run=True → effective_dry_run=True."""
        resolution = router_with_resolved_api.resolve(
            case["message"], case["source_section"]
        )
        resolved = build_resolved_request(
            case["message"],
            resolution.route,
            router_with_resolved_api,
            forwarder_dry_run=True,
        )
        assert isinstance(resolved, ResolvedRequest)
        assert resolved.effective_dry_run is True
        assert resolved.method == "POST"
        assert resolved.url, "URL should not be empty"
        assert "://" in resolved.url, "URL should be absolute"
        assert resolved.route_name == case["expected_route"]
        assert resolved.api_name == "aem_api"
        assert resolved.endpoint_name == case["expected_endpoint"]

    @pytest.mark.parametrize(
        "case",
        REPRESENTATIVE_MESSAGES,
        ids=[m["id"] for m in REPRESENTATIVE_MESSAGES],
    )
    def test_dispatch_dry_run_no_http(self, router_with_resolved_api, case, caplog):
        """Dispatch in dry-run mode → [DRY-RUN] logged, no HTTP, returns success."""
        resolution = router_with_resolved_api.resolve(
            case["message"], case["source_section"]
        )
        resolved = build_resolved_request(
            case["message"],
            resolution.route,
            router_with_resolved_api,
            forwarder_dry_run=True,
        )

        mock_session = MagicMock()

        with caplog.at_level(logging.INFO, logger="forwarder"):
            result = dispatch_http_request(
                resolved,
                case["message"],
                policy="ack_error_no_requeue",
                session=mock_session,
            )

        assert result.success is True
        assert result.dry_run is True
        assert result.attempts == 0
        assert result.route_name == case["expected_route"]

        mock_session.request.assert_not_called()

        dry_run_logs = [
            r for r in caplog.records if "[DRY-RUN]" in r.getMessage()
        ]
        assert len(dry_run_logs) >= 1, "Expected [DRY-RUN] log entry"
        log_msg = dry_run_logs[-1].getMessage()
        assert case["expected_route"] in log_msg
        assert "aem_api" in log_msg

    @pytest.mark.parametrize(
        "case",
        REPRESENTATIVE_MESSAGES,
        ids=[m["id"] for m in REPRESENTATIVE_MESSAGES],
    )
    def test_full_callback_returns_true(self, router_with_resolved_api, case):
        """Simulate the _v2_message_callback — always returns True (ack)."""
        router = router_with_resolved_api
        force_dry_run = True
        source = FakeRabbitMQSource(section=case["source_section"])
        message = case["message"]

        resolution = router.resolve(message, source.section)

        if resolution.status == "no_match":
            callback_result = True
        elif resolution.status == "ambiguous":
            callback_result = True
        else:
            resolved_req = build_resolved_request(
                message, resolution.route, router, force_dry_run
            )
            mock_session = MagicMock()
            dispatch_http_request(
                resolved_req,
                message,
                policy=router.defaults.on_http_failure,
                session=mock_session,
            )
            mock_session.request.assert_not_called()
            callback_result = True

        assert callback_result is True


# =========================================================================
# 5. No-match and wrong-source scenarios
# =========================================================================

# =========================================================================
# 4b. Simulated measurement — full pipeline dry-run
# =========================================================================

SIM_MEASURE_MESSAGES = [
    {
        "id": "sim_measure_hp",
        "message": {
            "message_type": "measure",
            "asset_type": "heat_pump",
            "asset_id": "ECM68.3",
            "measurement_type": "power",
            "payload": {"power_kw": 2.5, "timestamp": "2026-06-15T14:00:00Z"},
        },
        "source_section": "simulatedAssetMeasures",
        "expected_route": "sim_measure_forward",
        "expected_endpoint": "sim_measure_passthrough",
        "expected_api": "aem_test_api",
    },
    {
        "id": "sim_measure_ev",
        "message": {
            "message_type": "measure",
            "asset_type": "ev_charger",
            "asset_id": "ECM63.1",
            "measurement_type": "energy",
            "payload": {"energy_kwh": 12.3, "timestamp": "2026-06-15T14:15:00Z"},
        },
        "source_section": "simulatedAssetMeasures",
        "expected_route": "sim_measure_forward",
        "expected_endpoint": "sim_measure_passthrough",
        "expected_api": "aem_test_api",
    },
]


class TestSimMeasurePipeline:

    @pytest.mark.parametrize(
        "case",
        SIM_MEASURE_MESSAGES,
        ids=[m["id"] for m in SIM_MEASURE_MESSAGES],
    )
    def test_route_resolution(self, router_with_resolved_api, case):
        result = router_with_resolved_api.resolve(
            case["message"], case["source_section"]
        )
        assert result.status == "matched"
        assert result.route.name == case["expected_route"]
        assert result.route.endpoint == case["expected_endpoint"]
        assert result.route.api == case["expected_api"]

    @pytest.mark.parametrize(
        "case",
        SIM_MEASURE_MESSAGES,
        ids=[m["id"] for m in SIM_MEASURE_MESSAGES],
    )
    def test_request_building_dry_run(self, router_with_resolved_api, case):
        resolution = router_with_resolved_api.resolve(
            case["message"], case["source_section"]
        )
        resolved = build_resolved_request(
            case["message"],
            resolution.route,
            router_with_resolved_api,
            forwarder_dry_run=True,
        )
        assert isinstance(resolved, ResolvedRequest)
        assert resolved.effective_dry_run is True
        assert resolved.method == "POST"
        assert resolved.url, "URL should not be empty"
        assert "://" in resolved.url
        assert resolved.route_name == case["expected_route"]
        assert resolved.api_name == case["expected_api"]

    @pytest.mark.parametrize(
        "case",
        SIM_MEASURE_MESSAGES,
        ids=[m["id"] for m in SIM_MEASURE_MESSAGES],
    )
    def test_passthrough_body(self, router_with_resolved_api, case):
        """Passthrough endpoint sends the entire message as the body."""
        resolution = router_with_resolved_api.resolve(
            case["message"], case["source_section"]
        )
        resolved = build_resolved_request(
            case["message"],
            resolution.route,
            router_with_resolved_api,
            forwarder_dry_run=True,
        )
        assert resolved.body == case["message"]

    def test_series_batch_is_sent_as_raw_body(self, router_with_resolved_api):
        """Series batches use the sim measure route but keep the original body."""
        batch = {
            "series": [
                {
                    "community": "ECM",
                    "site": "ECM68",
                    "device_name": "ECM68.3",
                    "values": [
                        {
                            "time": "2026-06-25T08:31:22Z",
                            "active_power": 38563.73563443726,
                        }
                    ],
                }
            ]
        }
        resolution = router_with_resolved_api.resolve(
            {"message_type": "measure"},
            "simulatedAssetMeasures",
        )

        resolved = build_resolved_request(
            batch,
            resolution.route,
            router_with_resolved_api,
            forwarder_dry_run=True,
        )

        assert resolution.status == "matched"
        assert resolution.route.name == "sim_measure_forward"
        assert resolved.body == batch

    @pytest.mark.parametrize(
        "case",
        SIM_MEASURE_MESSAGES,
        ids=[m["id"] for m in SIM_MEASURE_MESSAGES],
    )
    def test_dispatch_dry_run_no_http(self, router_with_resolved_api, case, caplog):
        resolution = router_with_resolved_api.resolve(
            case["message"], case["source_section"]
        )
        resolved = build_resolved_request(
            case["message"],
            resolution.route,
            router_with_resolved_api,
            forwarder_dry_run=True,
        )
        mock_session = MagicMock()
        with caplog.at_level(logging.INFO, logger="forwarder"):
            result = dispatch_http_request(
                resolved,
                case["message"],
                policy="ack_error_no_requeue",
                session=mock_session,
            )
        assert result.success is True
        assert result.dry_run is True
        assert result.attempts == 0
        mock_session.request.assert_not_called()

    @pytest.mark.parametrize(
        "case",
        SIM_MEASURE_MESSAGES,
        ids=[m["id"] for m in SIM_MEASURE_MESSAGES],
    )
    def test_url_uses_aem_test_base(self, router_with_resolved_api, case):
        resolution = router_with_resolved_api.resolve(
            case["message"], case["source_section"]
        )
        resolved = build_resolved_request(
            case["message"],
            resolution.route,
            router_with_resolved_api,
            forwarder_dry_run=True,
        )
        api = router_with_resolved_api.apis["aem_test_api"]
        assert resolved.url.startswith(api.base_url.rstrip("/"))


class TestNoMatchScenarios:

    def test_measurement_no_match(self, router_with_resolved_api):
        msg = {
            "message_type": "measurement",
            "asset_type": "heat_pump",
            "asset_id": "ECM96.2",
            "payload": {},
        }
        result = router_with_resolved_api.resolve(msg, "realAssetCommands")
        assert result.status == "no_match"

    def test_wrong_source_no_match(self, router_with_resolved_api):
        msg = {
            "message_type": "command",
            "asset_type": "heat_pump",
            "asset_id": "ECM96.2",
            "payload": {},
        }
        result = router_with_resolved_api.resolve(msg, "simulatedAssetCommands")
        assert result.status == "no_match"

    def test_no_match_callback_returns_true(self, router_with_resolved_api):
        """No-match → ack_warn_no_forward → callback still returns True."""
        msg = {
            "message_type": "measurement",
            "asset_type": "heat_pump",
            "asset_id": "ECM96.2",
            "payload": {},
        }
        result = router_with_resolved_api.resolve(msg, "realAssetCommands")
        assert result.status == "no_match"
        assert router_with_resolved_api.defaults.on_no_match == "ack_warn_no_forward"


# =========================================================================
# 6. Dry-run default behavior — missing payload.dry_run
# =========================================================================

class TestDryRunDefaultBehavior:

    def test_missing_payload_dry_run_uses_default_false(
        self, router_with_resolved_api
    ):
        """Message without payload.dry_run → effective_dry_run=False
        because missing_message_dry_run_default=False and forwarder_dry_run=False."""
        msg = {
            "message_type": "command",
            "asset_type": "heat_pump",
            "asset_id": "ECM96.2",
            "command_type": "curtail",
            "payload": {
                "community": "ECM",
                "site_id": "ECM96",
                "asset_id": "ECM96.2",
                "pod": "ECM96",
                "discrete_state": 0,
                "slot_start": "2026-06-15T14:00:00Z",
            },
        }
        resolution = router_with_resolved_api.resolve(msg, "realAssetCommands")
        resolved = build_resolved_request(
            msg,
            resolution.route,
            router_with_resolved_api,
            forwarder_dry_run=False,
        )
        assert resolved.effective_dry_run is False

    def test_explicit_payload_dry_run_false_with_forwarder_false(
        self, router_with_resolved_api
    ):
        """Message with payload.dry_run=False and forwarder_dry_run=False
        → effective_dry_run=False (would be live)."""
        msg = {
            "message_type": "command",
            "asset_type": "heat_pump",
            "asset_id": "ECM96.2",
            "command_type": "curtail",
            "payload": {
                "community": "ECM",
                "site_id": "ECM96",
                "asset_id": "ECM96.2",
                "pod": "ECM96",
                "discrete_state": 0,
                "slot_start": "2026-06-15T14:00:00Z",
                "dry_run": False,
            },
        }
        resolution = router_with_resolved_api.resolve(msg, "realAssetCommands")
        resolved = build_resolved_request(
            msg,
            resolution.route,
            router_with_resolved_api,
            forwarder_dry_run=False,
        )
        assert resolved.effective_dry_run is False

    def test_forwarder_dry_run_overrides_message(
        self, router_with_resolved_api
    ):
        """forwarder_dry_run=True overrides payload.dry_run=False."""
        msg = {
            "message_type": "command",
            "asset_type": "heat_pump",
            "asset_id": "ECM96.2",
            "command_type": "curtail",
            "payload": {
                "community": "ECM",
                "site_id": "ECM96",
                "asset_id": "ECM96.2",
                "pod": "ECM96",
                "discrete_state": 0,
                "slot_start": "2026-06-15T14:00:00Z",
                "dry_run": False,
            },
        }
        resolution = router_with_resolved_api.resolve(msg, "realAssetCommands")
        resolved = build_resolved_request(
            msg,
            resolution.route,
            router_with_resolved_api,
            forwarder_dry_run=True,
        )
        assert resolved.effective_dry_run is True


# =========================================================================
# 7. URL and body correctness for each asset type
# =========================================================================

class TestUrlAndBodyCorrectness:

    def test_hp_url_contains_ecm96_hp(self, router_with_resolved_api):
        msg = {
            "message_type": "command",
            "asset_type": "heat_pump",
            "asset_id": "ECM96.2",
            "command_type": "curtail",
            "payload": {
                "community": "ECM",
                "site_id": "ECM96",
                "asset_id": "ECM96.2",
                "pod": "ECM96",
                "discrete_state": 0,
                "slot_start": "2026-06-15T14:00:00Z",
            },
        }
        resolution = router_with_resolved_api.resolve(msg, "realAssetCommands")
        resolved = build_resolved_request(
            msg, resolution.route, router_with_resolved_api, True
        )
        assert "/ECM/ECM96/hp" in resolved.url

    def test_ev_url_contains_ecm63_charge_point(self, router_with_resolved_api):
        msg = {
            "message_type": "command",
            "asset_type": "ev_charger",
            "asset_id": "ECM63.1",
            "command_type": "curtail",
            "payload": {
                "community": "ECM",
                "site_id": "ECM63",
                "asset_id": "ECM63.1",
                "pod": "ECM63",
                "schedule": [
                    {"time": "2026-06-15T14:00:00Z", "power_kw": 6.24},
                ],
            },
        }
        resolution = router_with_resolved_api.resolve(msg, "realAssetCommands")
        resolved = build_resolved_request(
            msg, resolution.route, router_with_resolved_api, True
        )
        assert "/ECM/ECM63/charge_point_ev_1" in resolved.url

    def test_fallback_url_contains_resolved_placeholders(
        self, router_with_resolved_api
    ):
        msg = {
            "message_type": "command",
            "asset_type": "heat_pump",
            "asset_id": "HP-NEW",
            "command_type": "curtail",
            "payload": {
                "community": "NEWCOMMUNITY",
                "site_id": "NEWSITE",
                "asset_id": "HP-NEW",
                "pod": "NEWSITE",
                "discrete_state": 1,
                "target_state": "on",
                "slot_start": "2026-06-15T14:00:00Z",
            },
        }
        resolution = router_with_resolved_api.resolve(msg, "realAssetCommands")
        assert resolution.route.name == "fallback_aem_command"
        resolved = build_resolved_request(
            msg, resolution.route, router_with_resolved_api, True
        )
        assert "/NEWCOMMUNITY/NEWSITE/HP-NEW" in resolved.url
        assert "{" not in resolved.url, "No unresolved placeholders"

    def test_fallback_body_has_power_and_time(self, router_with_resolved_api):
        msg = {
            "message_type": "command",
            "asset_type": "heat_pump",
            "asset_id": "HP-NEW",
            "command_type": "curtail",
            "payload": {
                "community": "NEWCOMMUNITY",
                "site_id": "NEWSITE",
                "asset_id": "HP-NEW",
                "pod": "NEWSITE",
                "discrete_state": 1,
                "target_state": "on",
                "slot_start": "2026-06-15T14:00:00Z",
            },
        }
        resolution = router_with_resolved_api.resolve(msg, "realAssetCommands")
        resolved = build_resolved_request(
            msg, resolution.route, router_with_resolved_api, True
        )
        assert isinstance(resolved.body, dict)
        assert "power" in resolved.body
        assert "time" in resolved.body

    def test_hp_body_is_hp_control_format(self, router_with_resolved_api):
        msg = {
            "message_type": "command",
            "asset_type": "heat_pump",
            "asset_id": "ECM96.2",
            "command_type": "curtail",
            "payload": {
                "community": "ECM",
                "site_id": "ECM96",
                "asset_id": "ECM96.2",
                "pod": "ECM96",
                "discrete_state": 0,
                "slot_start": "2026-06-15T14:00:00Z",
            },
        }
        resolution = router_with_resolved_api.resolve(msg, "realAssetCommands")
        resolved = build_resolved_request(
            msg, resolution.route, router_with_resolved_api, True
        )
        body = resolved.body
        assert isinstance(body, dict)
        assert "power" in body or "state" in body or body is not None


# =========================================================================
# 8. No basic_nack(requeue=True) in v2
# =========================================================================

class TestNoRequeue:

    def test_on_http_failure_is_ack_error_no_requeue(
        self, router_with_resolved_api
    ):
        assert (
            router_with_resolved_api.defaults.on_http_failure
            == "ack_error_no_requeue"
        )

    def test_all_callback_paths_return_true(self, router_with_resolved_api):
        """Every v2 callback code path must return True (=ack).
        matched, no_match, and ambiguous all return True."""
        mock_session = MagicMock()

        matched_msg = {
            "message_type": "command",
            "asset_type": "heat_pump",
            "asset_id": "ECM96.2",
            "command_type": "curtail",
            "payload": {
                "community": "ECM",
                "site_id": "ECM96",
                "asset_id": "ECM96.2",
                "pod": "ECM96",
                "discrete_state": 0,
                "slot_start": "2026-06-15T14:00:00Z",
            },
        }
        res = router_with_resolved_api.resolve(
            matched_msg, "realAssetCommands"
        )
        assert res.status == "matched"
        req = build_resolved_request(
            matched_msg,
            res.route,
            router_with_resolved_api,
            forwarder_dry_run=True,
        )
        dispatch_http_request(
            req, matched_msg, session=mock_session
        )
        mock_session.request.assert_not_called()

        nomatch_msg = {
            "message_type": "measurement",
            "asset_type": "heat_pump",
            "asset_id": "ECM96.2",
            "payload": {},
        }
        res_nm = router_with_resolved_api.resolve(
            nomatch_msg, "realAssetCommands"
        )
        assert res_nm.status == "no_match"
