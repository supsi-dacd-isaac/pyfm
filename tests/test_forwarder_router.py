"""Tests for the forwarder configurable routing core (scripts/forwarder_router.py).

Covers:
  Step 1 — MessageProfile matching, route resolution, config detection/validation,
           dry-run resolution helper.
  Step 2 — URL/path resolution, body building (templates, body_mode, passthrough),
           ResolvedRequest construction, request-level dry-run.
"""

import copy
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from forwarder_router import (
    ApiConfig,
    EndpointConfig,
    HttpDispatchResult,
    MessageProfile,
    MessageRouter,
    ResolvedRequest,
    RouteConfig,
    RouteResolutionResult,
    RoutingConfigError,
    RoutingDefaults,
    build_resolved_request,
    detect_config_mode,
    dispatch_http_request,
    extract_message_dry_run,
    resolve_effective_dry_run,
    validate_routing_config,
)


# ========================================================================
# Helpers — minimal valid v2 config for reuse
# ========================================================================

def _minimal_v2_config(**overrides):
    """Return a valid v2 routing config dict, with optional overrides."""
    cfg = {
        "version": 2,
        "defaults": {
            "on_no_match": "ack_warn_no_forward",
            "on_ambiguous_match": "ack_error_no_forward",
            "on_http_failure": "ack_error_no_requeue",
            "missing_message_dry_run_default": True,
        },
        "sources": {
            "src_cmd": {"section": "realAssetCommands"},
            "src_sim": {"section": "simulatedAssetCommands"},
        },
        "message_profiles": {
            "hp_commands": {
                "message_type": "command",
                "asset_types": ["heat_pump"],
            },
            "ev_commands": {
                "message_type": "command",
                "asset_types": ["ev_charger"],
            },
            "all_commands": {
                "message_type": "command",
            },
            "catch_all": {},
        },
        "apis": {
            "aem_api": {
                "base_url": "https://aem.example.com",
                "timeout": 10.0,
                "retries": 3,
                "verify_ssl": False,
            },
        },
        "endpoints": {
            "hp_control": {
                "method": "POST",
                "path_template": "/api/v1/assets/{asset_id}/control",
            },
            "ev_power": {
                "method": "POST",
                "path_template": "/api/v1/ev/{asset_id}/power",
            },
        },
        "routes": [
            {
                "name": "hp_to_aem",
                "source": "src_cmd",
                "message_profile": "hp_commands",
                "api": "aem_api",
                "endpoint": "hp_control",
                "priority": 100,
                "enabled": True,
            },
            {
                "name": "ev_to_aem",
                "source": "src_cmd",
                "message_profile": "ev_commands",
                "api": "aem_api",
                "endpoint": "ev_power",
                "priority": 100,
                "enabled": True,
            },
        ],
    }
    cfg.update(overrides)
    return cfg


def _build_router(config=None):
    """Validate a config and return a MessageRouter."""
    cfg = config or _minimal_v2_config()
    validated = validate_routing_config(cfg)
    return MessageRouter.from_validated_config(validated)


# ========================================================================
# MessageProfile matching
# ========================================================================

class TestMessageProfileMatching:

    def test_matches_by_message_type(self):
        p = MessageProfile(name="p", message_type="command")
        assert p.matches({"message_type": "command"})
        assert not p.matches({"message_type": "measurement"})

    def test_matches_by_asset_types(self):
        p = MessageProfile(name="p", asset_types=["heat_pump", "boiler"])
        assert p.matches({"asset_type": "heat_pump"})
        assert p.matches({"asset_type": "boiler"})
        assert not p.matches({"asset_type": "ev_charger"})

    def test_matches_by_asset_ids(self):
        p = MessageProfile(name="p", asset_ids=["HP-01", "HP-02"])
        assert p.matches({"asset_id": "HP-01"})
        assert not p.matches({"asset_id": "HP-99"})

    def test_matches_by_command_types(self):
        p = MessageProfile(name="p", command_types=["curtail", "restore"])
        assert p.matches({"command_type": "curtail"})
        assert not p.matches({"command_type": "preactivate"})

    def test_command_type_fallback_to_command_field(self):
        p = MessageProfile(name="p", command_types=["curtail"])
        assert p.matches({"command": "curtail"})
        assert not p.matches({"command": "restore"})

    def test_combined_profile_filters(self):
        p = MessageProfile(
            name="p",
            message_type="command",
            asset_types=["heat_pump"],
            command_types=["curtail"],
        )
        msg_ok = {
            "message_type": "command",
            "asset_type": "heat_pump",
            "command_type": "curtail",
        }
        assert p.matches(msg_ok)

        msg_wrong_type = dict(msg_ok, message_type="measurement")
        assert not p.matches(msg_wrong_type)

        msg_wrong_asset = dict(msg_ok, asset_type="ev_charger")
        assert not p.matches(msg_wrong_asset)

    def test_missing_field_does_not_match_filter(self):
        p = MessageProfile(name="p", asset_types=["heat_pump"])
        assert not p.matches({"message_type": "command"})

        p2 = MessageProfile(name="p2", command_types=["curtail"])
        assert not p2.matches({"message_type": "command"})

    def test_measurement_profile_matching(self):
        p = MessageProfile(name="p", message_type="measurement")
        assert p.matches({"message_type": "measurement", "asset_type": "heat_pump"})
        assert not p.matches({"message_type": "command"})

    def test_wildcard_empty_profile_matches_everything(self):
        p = MessageProfile(name="p")
        assert p.matches({"message_type": "command", "asset_type": "heat_pump"})
        assert p.matches({"message_type": "measurement"})
        assert p.matches({})

    def test_specificity_score(self):
        p_empty = MessageProfile(name="empty")
        p_type = MessageProfile(name="type", message_type="command")
        p_asset_types = MessageProfile(name="at", asset_types=["heat_pump"])
        p_asset_ids = MessageProfile(name="ai", asset_ids=["HP-01"])
        p_combined = MessageProfile(
            name="combined",
            message_type="command",
            asset_types=["heat_pump"],
            asset_ids=["HP-01"],
            command_types=["curtail"],
        )

        assert p_empty.specificity == 0
        assert p_type.specificity == 1
        assert p_asset_types.specificity > p_type.specificity
        assert p_asset_ids.specificity > p_asset_types.specificity
        assert p_combined.specificity == 1 + 2 + 4 + 8


# ========================================================================
# Route resolution
# ========================================================================

class TestRouteResolution:

    def test_single_route_selected(self):
        router = _build_router()
        msg = {
            "message_type": "command",
            "asset_type": "heat_pump",
            "asset_id": "HP-01",
        }
        result = router.resolve(msg, "realAssetCommands")

        assert result.status == "matched"
        assert result.route is not None
        assert result.route.name == "hp_to_aem"

    def test_source_mismatch_skipped(self):
        router = _build_router()
        msg = {
            "message_type": "command",
            "asset_type": "heat_pump",
            "asset_id": "HP-01",
        }
        result = router.resolve(msg, "nonexistentSection")
        assert result.status == "no_match"

    def test_disabled_route_skipped(self):
        cfg = _minimal_v2_config()
        cfg["routes"][0]["enabled"] = False
        router = _build_router(cfg)

        msg = {
            "message_type": "command",
            "asset_type": "heat_pump",
            "asset_id": "HP-01",
        }
        result = router.resolve(msg, "realAssetCommands")
        assert result.status == "no_match"

    def test_higher_priority_wins(self):
        cfg = _minimal_v2_config()
        cfg["message_profiles"]["all_hp_fallback"] = {
            "message_type": "command",
            "asset_types": ["heat_pump"],
        }
        cfg["endpoints"]["fallback_ep"] = {
            "method": "POST",
            "path_template": "/fallback",
        }
        cfg["routes"].append({
            "name": "hp_fallback",
            "source": "src_cmd",
            "message_profile": "all_hp_fallback",
            "api": "aem_api",
            "endpoint": "fallback_ep",
            "priority": 50,
            "enabled": True,
        })
        router = _build_router(cfg)

        msg = {
            "message_type": "command",
            "asset_type": "heat_pump",
            "asset_id": "HP-01",
        }
        result = router.resolve(msg, "realAssetCommands")
        assert result.status == "matched"
        assert result.route.name == "hp_to_aem"

    def test_same_queue_different_profiles_route_correctly(self):
        router = _build_router()

        hp_msg = {
            "message_type": "command",
            "asset_type": "heat_pump",
            "asset_id": "HP-01",
        }
        ev_msg = {
            "message_type": "command",
            "asset_type": "ev_charger",
            "asset_id": "EV-01",
        }

        r1 = router.resolve(hp_msg, "realAssetCommands")
        r2 = router.resolve(ev_msg, "realAssetCommands")

        assert r1.status == "matched"
        assert r1.route.name == "hp_to_aem"
        assert r2.status == "matched"
        assert r2.route.name == "ev_to_aem"

    def test_same_profile_on_different_queues_routes_correctly(self):
        cfg = _minimal_v2_config()
        cfg["routes"].append({
            "name": "hp_sim",
            "source": "src_sim",
            "message_profile": "hp_commands",
            "api": "aem_api",
            "endpoint": "hp_control",
            "priority": 100,
            "enabled": True,
        })
        router = _build_router(cfg)

        msg = {
            "message_type": "command",
            "asset_type": "heat_pump",
            "asset_id": "HP-01",
        }

        r_real = router.resolve(msg, "realAssetCommands")
        r_sim = router.resolve(msg, "simulatedAssetCommands")

        assert r_real.status == "matched"
        assert r_real.route.name == "hp_to_aem"
        assert r_sim.status == "matched"
        assert r_sim.route.name == "hp_sim"

    def test_catch_all_lower_priority_used_when_specific_does_not_match(self):
        cfg = _minimal_v2_config()
        cfg["routes"].append({
            "name": "catch_all_route",
            "source": "src_cmd",
            "message_profile": "catch_all",
            "api": "aem_api",
            "endpoint": "hp_control",
            "priority": 0,
            "enabled": True,
        })
        router = _build_router(cfg)

        msg = {
            "message_type": "command",
            "asset_type": "battery",
            "asset_id": "BAT-01",
        }
        result = router.resolve(msg, "realAssetCommands")
        assert result.status == "matched"
        assert result.route.name == "catch_all_route"

    def test_no_match_result(self):
        router = _build_router()
        msg = {
            "message_type": "command",
            "asset_type": "battery",
            "asset_id": "BAT-01",
        }
        result = router.resolve(msg, "realAssetCommands")
        assert result.status == "no_match"
        assert result.route is None

    def test_ambiguity_at_same_priority_returns_no_selected_route(self):
        cfg = _minimal_v2_config()
        cfg["message_profiles"]["hp_commands_dup"] = {
            "message_type": "command",
            "asset_types": ["heat_pump"],
        }
        cfg["routes"].append({
            "name": "hp_to_aem_dup",
            "source": "src_cmd",
            "message_profile": "hp_commands_dup",
            "api": "aem_api",
            "endpoint": "hp_control",
            "priority": 100,
            "enabled": True,
        })
        router = _build_router(cfg)

        msg = {
            "message_type": "command",
            "asset_type": "heat_pump",
            "asset_id": "HP-01",
        }
        result = router.resolve(msg, "realAssetCommands")
        assert result.status == "ambiguous"
        assert result.route is None

    def test_ambiguity_result_includes_all_conflicting_route_names(self):
        cfg = _minimal_v2_config()
        cfg["message_profiles"]["hp_commands_dup"] = {
            "message_type": "command",
            "asset_types": ["heat_pump"],
        }
        cfg["routes"].append({
            "name": "hp_to_aem_dup",
            "source": "src_cmd",
            "message_profile": "hp_commands_dup",
            "api": "aem_api",
            "endpoint": "hp_control",
            "priority": 100,
            "enabled": True,
        })
        router = _build_router(cfg)

        msg = {
            "message_type": "command",
            "asset_type": "heat_pump",
            "asset_id": "HP-01",
        }
        result = router.resolve(msg, "realAssetCommands")

        assert result.status == "ambiguous"
        names = [r.name for r in result.matching_routes]
        assert "hp_to_aem" in names
        assert "hp_to_aem_dup" in names
        assert len(result.matching_routes) == 2


# ========================================================================
# Config mode detection and validation
# ========================================================================

class TestConfigDetection:

    def test_valid_v2_config_loads(self):
        cfg = _minimal_v2_config()
        assert detect_config_mode(cfg) == "v2"
        validated = validate_routing_config(cfg)
        assert "routes" in validated
        assert len(validated["routes"]) == 2

    def test_legacy_config_with_targets_detected(self):
        cfg = {"targets": [{"name": "t1", "url": "http://localhost"}]}
        assert detect_config_mode(cfg) == "legacy"

    def test_config_with_both_targets_and_routes_raises(self):
        cfg = {
            "targets": [{"name": "t1", "url": "http://localhost"}],
            "routes": [],
        }
        with pytest.raises(RoutingConfigError, match="mutually exclusive"):
            detect_config_mode(cfg)

    def test_config_version_2_without_routes_raises(self):
        cfg = {"version": 2, "targets": []}
        with pytest.raises(RoutingConfigError, match="no 'routes' section"):
            detect_config_mode(cfg)

    def test_config_version_2_no_targets_no_routes_raises(self):
        cfg = {"version": 2}
        with pytest.raises(RoutingConfigError, match="no 'routes' section"):
            detect_config_mode(cfg)

    def test_empty_config_is_legacy(self):
        assert detect_config_mode({}) == "legacy"


class TestConfigValidation:

    def test_invalid_on_no_match_policy_raises(self):
        cfg = _minimal_v2_config()
        cfg["defaults"]["on_no_match"] = "reject_requeue"
        with pytest.raises(RoutingConfigError, match="on_no_match"):
            validate_routing_config(cfg)

    def test_invalid_on_ambiguous_match_policy_raises(self):
        cfg = _minimal_v2_config()
        cfg["defaults"]["on_ambiguous_match"] = "first_match"
        with pytest.raises(RoutingConfigError, match="on_ambiguous_match"):
            validate_routing_config(cfg)

    def test_invalid_on_http_failure_policy_raises(self):
        cfg = _minimal_v2_config()
        cfg["defaults"]["on_http_failure"] = "nack_requeue"
        with pytest.raises(RoutingConfigError, match="on_http_failure"):
            validate_routing_config(cfg)

    def test_route_referencing_missing_source_raises(self):
        cfg = _minimal_v2_config()
        cfg["routes"][0]["source"] = "nonexistent_source"
        with pytest.raises(RoutingConfigError, match="missing source"):
            validate_routing_config(cfg)

    def test_route_referencing_missing_message_profile_raises(self):
        cfg = _minimal_v2_config()
        cfg["routes"][0]["message_profile"] = "nonexistent_profile"
        with pytest.raises(RoutingConfigError, match="missing message_profile"):
            validate_routing_config(cfg)

    def test_route_referencing_missing_api_raises(self):
        cfg = _minimal_v2_config()
        cfg["routes"][0]["api"] = "nonexistent_api"
        with pytest.raises(RoutingConfigError, match="missing api"):
            validate_routing_config(cfg)

    def test_route_referencing_missing_endpoint_raises(self):
        cfg = _minimal_v2_config()
        cfg["routes"][0]["endpoint"] = "nonexistent_ep"
        with pytest.raises(RoutingConfigError, match="missing endpoint"):
            validate_routing_config(cfg)

    def test_duplicate_route_names_raise(self):
        cfg = _minimal_v2_config()
        cfg["routes"].append(dict(cfg["routes"][0]))
        with pytest.raises(RoutingConfigError, match="Duplicate route name"):
            validate_routing_config(cfg)

    def test_missing_required_route_fields_raise(self):
        cfg = _minimal_v2_config()
        cfg["routes"].append({"name": "incomplete_route"})
        with pytest.raises(RoutingConfigError, match="missing required field"):
            validate_routing_config(cfg)

    def test_missing_message_dry_run_default_supports_true(self):
        cfg = _minimal_v2_config()
        cfg["defaults"]["missing_message_dry_run_default"] = True
        validated = validate_routing_config(cfg)
        assert validated["defaults"].missing_message_dry_run_default is True

    def test_missing_message_dry_run_default_supports_false(self):
        cfg = _minimal_v2_config()
        cfg["defaults"]["missing_message_dry_run_default"] = False
        validated = validate_routing_config(cfg)
        assert validated["defaults"].missing_message_dry_run_default is False

    def test_absent_missing_message_dry_run_default_defaults_to_true(self):
        cfg = _minimal_v2_config()
        del cfg["defaults"]["missing_message_dry_run_default"]
        validated = validate_routing_config(cfg)
        assert validated["defaults"].missing_message_dry_run_default is True

    def test_absent_defaults_section_uses_all_defaults(self):
        cfg = _minimal_v2_config()
        del cfg["defaults"]
        validated = validate_routing_config(cfg)
        d = validated["defaults"]
        assert d.on_no_match == "ack_warn_no_forward"
        assert d.on_ambiguous_match == "ack_error_no_forward"
        assert d.on_http_failure == "ack_error_no_requeue"
        assert d.missing_message_dry_run_default is True


# ========================================================================
# Dry-run resolution
# ========================================================================

class TestDryRunResolution:

    def test_forwarder_dry_run_overrides_all(self):
        assert resolve_effective_dry_run(
            forwarder_dry_run=True,
            route_dry_run=False,
            message_dry_run=False,
        ) is True

    def test_route_dry_run_overrides_message_live_mode(self):
        assert resolve_effective_dry_run(
            forwarder_dry_run=False,
            route_dry_run=True,
            message_dry_run=False,
        ) is True

    def test_message_dry_run_true_makes_effective_true(self):
        assert resolve_effective_dry_run(
            forwarder_dry_run=False,
            route_dry_run=None,
            message_dry_run=True,
        ) is True

    def test_all_none_message_false_means_live(self):
        assert resolve_effective_dry_run(
            forwarder_dry_run=False,
            route_dry_run=None,
            message_dry_run=False,
        ) is False

    def test_explicit_route_false_means_live(self):
        assert resolve_effective_dry_run(
            forwarder_dry_run=False,
            route_dry_run=False,
            message_dry_run=None,
            missing_message_dry_run_default=True,
        ) is False

    def test_explicit_route_false_overrides_message_default(self):
        """route dry_run=false wins over missing_message_dry_run_default=true."""
        assert resolve_effective_dry_run(
            forwarder_dry_run=False,
            route_dry_run=False,
            message_dry_run=True,
        ) is False

    def test_missing_message_dry_run_uses_configured_default_true(self):
        assert resolve_effective_dry_run(
            forwarder_dry_run=False,
            route_dry_run=None,
            message_dry_run=None,
            missing_message_dry_run_default=True,
        ) is True

    def test_missing_message_dry_run_uses_configured_default_false(self):
        assert resolve_effective_dry_run(
            forwarder_dry_run=False,
            route_dry_run=None,
            message_dry_run=None,
            missing_message_dry_run_default=False,
        ) is False

    def test_route_without_dry_run_defers_to_message(self):
        assert resolve_effective_dry_run(
            forwarder_dry_run=False,
            route_dry_run=None,
            message_dry_run=False,
        ) is False


# ========================================================================
# Data class construction helpers
# ========================================================================

class TestDataclassConstruction:

    def test_message_profile_from_dict(self):
        p = MessageProfile.from_dict("test", {
            "message_type": "command",
            "asset_types": ["heat_pump"],
            "command_types": ["curtail"],
        })
        assert p.name == "test"
        assert p.message_type == "command"
        assert p.asset_types == ["heat_pump"]
        assert p.command_types == ["curtail"]
        assert p.asset_ids is None

    def test_api_config_from_dict(self):
        a = ApiConfig.from_dict("api1", {
            "base_url": "https://example.com",
            "user": "admin",
            "password": "secret",
            "timeout": 30.0,
            "retries": 5,
            "verify_ssl": True,
        })
        assert a.name == "api1"
        assert a.base_url == "https://example.com"
        assert a.timeout == 30.0
        assert a.retries == 5
        assert a.verify_ssl is True

    def test_endpoint_config_from_dict(self):
        e = EndpointConfig.from_dict("ep1", {
            "method": "PUT",
            "path_template": "/api/{asset_id}",
            "success_status_codes": [200, 204],
        })
        assert e.name == "ep1"
        assert e.method == "PUT"
        assert e.success_status_codes == [200, 204]

    def test_route_config_from_dict_defaults(self):
        r = RouteConfig.from_dict({
            "name": "r1",
            "source": "src",
            "message_profile": "prof",
            "api": "api",
            "endpoint": "ep",
        })
        assert r.priority == 0
        assert r.enabled is True
        assert r.dry_run is None

    def test_routing_defaults_from_empty_dict(self):
        d = RoutingDefaults.from_dict({})
        assert d.on_no_match == "ack_warn_no_forward"
        assert d.on_ambiguous_match == "ack_error_no_forward"
        assert d.on_http_failure == "ack_error_no_requeue"
        assert d.missing_message_dry_run_default is True

    def test_routing_defaults_from_none(self):
        d = RoutingDefaults.from_dict(None)
        assert d.missing_message_dry_run_default is True


# ========================================================================
# Step 2 — Helpers for request-building tests
# ========================================================================

def _request_building_config(**overrides):
    """Return a v2 config tailored for request-building tests."""
    cfg = {
        "version": 2,
        "defaults": {
            "on_no_match": "ack_warn_no_forward",
            "on_ambiguous_match": "ack_error_no_forward",
            "on_http_failure": "ack_error_no_requeue",
            "missing_message_dry_run_default": True,
        },
        "sources": {
            "src_cmd": {"section": "realAssetCommands"},
        },
        "message_profiles": {
            "hp_commands": {
                "message_type": "command",
                "asset_types": ["heat_pump"],
            },
            "ev_commands": {
                "message_type": "command",
                "asset_types": ["ev_charger"],
            },
            "all_commands": {
                "message_type": "command",
            },
        },
        "apis": {
            "aem_api": {
                "base_url": "https://aem.example.com",
                "user": "admin",
                "password": "secret",
                "timeout": 15.0,
                "retries": 2,
                "verify_ssl": True,
            },
            "bare_api": {
                "base_url": "https://bare.example.com",
            },
        },
        "endpoints": {
            "hp_control": {
                "method": "POST",
                "path_template": "/api/v1/assets/{asset_id}/control",
            },
            "ev_power": {
                "method": "POST",
                "path_template": "/api/v1/ev/{asset_id}/power",
                "body_mode": "ev_power_timeseries",
            },
            "hp_mode": {
                "method": "POST",
                "path_template": "/api/v1/hp/{asset_id}/control",
                "body_mode": "hp_control",
            },
            "nested_path": {
                "method": "POST",
                "path_template": "/api/v1/{asset_type}/{payload.pod}/cmd",
            },
            "absolute_ep": {
                "method": "PUT",
                "path_template": "https://other.example.com/override/{asset_id}",
                "headers": {"X-Custom": "value"},
                "success_status_codes": [200, 204],
            },
            "template_body_ep": {
                "method": "POST",
                "path_template": "/api/cmd",
                "body_template": {
                    "cmd": {"$map": "command_type"},
                    "asset": {"$map": "asset_id"},
                    "power": {"$map": "payload.target_power_kw"},
                },
            },
            "map_list_fallback_ep": {
                "method": "POST",
                "path_template": "/api/cmd",
                "body_template": {
                    "ts": {
                        "$map": ["payload.slot_start", "timestamp"],
                    },
                },
            },
            "both_mode_and_template_ep": {
                "method": "POST",
                "path_template": "/api/both",
                "body_mode": "hp_control",
                "body_template": {"should_be": "ignored"},
            },
            "passthrough_ep": {
                "method": "POST",
                "path_template": "/api/pass",
            },
            "missing_placeholder_ep": {
                "method": "POST",
                "path_template": "/api/{nonexistent_field}/control",
            },
        },
        "routes": [
            {
                "name": "hp_to_aem",
                "source": "src_cmd",
                "message_profile": "hp_commands",
                "api": "aem_api",
                "endpoint": "hp_control",
                "priority": 100,
                "enabled": True,
            },
            {
                "name": "ev_to_aem",
                "source": "src_cmd",
                "message_profile": "ev_commands",
                "api": "aem_api",
                "endpoint": "ev_power",
                "priority": 100,
                "enabled": True,
            },
        ],
    }
    cfg.update(overrides)
    return cfg


def _build_request_router(config=None):
    """Validate config and return a MessageRouter for request tests."""
    cfg = config or _request_building_config()
    validated = validate_routing_config(cfg)
    return MessageRouter.from_validated_config(validated)


def _hp_message(**overrides):
    """A standard heat-pump command message."""
    msg = {
        "message_type": "command",
        "asset_type": "heat_pump",
        "asset_id": "HP-01",
        "command_type": "curtail",
        "timestamp": "2026-06-15T12:00:00",
        "payload": {
            "target_power_kw": 2.5,
            "slot_start": "2026-06-15T12:00:00",
            "slot_end": "2026-06-15T12:15:00",
            "dry_run": False,
        },
    }
    msg.update(overrides)
    return msg


def _ev_message(**overrides):
    """A standard EV charger command message."""
    msg = {
        "message_type": "command",
        "asset_type": "ev_charger",
        "asset_id": "EV-01",
        "command_type": "curtail",
        "timestamp": "2026-06-15T12:00:00",
        "payload": {
            "power_kw": 7.4,
            "slot_start": "2026-06-15T12:00:00",
            "slot_end": "2026-06-15T12:15:00",
            "dry_run": False,
        },
    }
    msg.update(overrides)
    return msg


# ========================================================================
# URL / path resolution
# ========================================================================

class TestUrlResolution:

    def test_relative_path_appended_to_base_url(self):
        router = _build_request_router()
        msg = _hp_message()
        route = router.routes[0]  # hp_to_aem

        req = build_resolved_request(msg, route, router, forwarder_dry_run=True)
        assert req.url == "https://aem.example.com/api/v1/assets/HP-01/control"

    def test_absolute_path_used_as_is(self):
        cfg = _request_building_config()
        cfg["routes"].append({
            "name": "hp_abs",
            "source": "src_cmd",
            "message_profile": "hp_commands",
            "api": "aem_api",
            "endpoint": "absolute_ep",
            "priority": 200,
        })
        router = _build_request_router(cfg)
        msg = _hp_message()
        route = [r for r in router.routes if r.name == "hp_abs"][0]

        req = build_resolved_request(msg, route, router, forwarder_dry_run=True)
        assert req.url == "https://other.example.com/override/HP-01"

    def test_duplicate_slashes_handled(self):
        cfg = _request_building_config()
        cfg["apis"]["slash_api"] = {"base_url": "https://example.com/"}
        cfg["endpoints"]["slash_ep"] = {
            "method": "POST",
            "path_template": "/api/{asset_id}",
        }
        cfg["routes"].append({
            "name": "slash_route",
            "source": "src_cmd",
            "message_profile": "hp_commands",
            "api": "slash_api",
            "endpoint": "slash_ep",
            "priority": 200,
        })
        router = _build_request_router(cfg)
        msg = _hp_message()
        route = [r for r in router.routes if r.name == "slash_route"][0]

        req = build_resolved_request(msg, route, router, forwarder_dry_run=True)
        assert req.url == "https://example.com/api/HP-01"
        assert "//" not in req.url.split("://", 1)[1]

    def test_top_level_placeholder_resolves(self):
        router = _build_request_router()
        msg = _hp_message()
        route = router.routes[0]

        req = build_resolved_request(msg, route, router, forwarder_dry_run=True)
        assert "HP-01" in req.url

    def test_nested_placeholder_resolves(self):
        cfg = _request_building_config()
        cfg["routes"].append({
            "name": "nested_route",
            "source": "src_cmd",
            "message_profile": "hp_commands",
            "api": "aem_api",
            "endpoint": "nested_path",
            "priority": 200,
        })
        router = _build_request_router(cfg)
        msg = _hp_message()
        msg["payload"]["pod"] = "POD-42"
        route = [r for r in router.routes if r.name == "nested_route"][0]

        req = build_resolved_request(msg, route, router, forwarder_dry_run=True)
        assert req.url == "https://aem.example.com/api/v1/heat_pump/POD-42/cmd"

    def test_missing_placeholder_raises(self):
        cfg = _request_building_config()
        cfg["routes"].append({
            "name": "bad_route",
            "source": "src_cmd",
            "message_profile": "hp_commands",
            "api": "aem_api",
            "endpoint": "missing_placeholder_ep",
            "priority": 200,
        })
        router = _build_request_router(cfg)
        msg = _hp_message()
        route = [r for r in router.routes if r.name == "bad_route"][0]

        with pytest.raises(ValueError, match="Unresolved template placeholder"):
            build_resolved_request(msg, route, router, forwarder_dry_run=True)

    def test_api_fields_resolve_in_path(self):
        cfg = _request_building_config()
        cfg["endpoints"]["api_ref_ep"] = {
            "method": "POST",
            "path_template": "{api.base_url}/custom/{asset_id}",
        }
        cfg["routes"].append({
            "name": "api_ref_route",
            "source": "src_cmd",
            "message_profile": "hp_commands",
            "api": "aem_api",
            "endpoint": "api_ref_ep",
            "priority": 200,
        })
        router = _build_request_router(cfg)
        msg = _hp_message()
        route = [r for r in router.routes if r.name == "api_ref_route"][0]

        req = build_resolved_request(msg, route, router, forwarder_dry_run=True)
        assert req.url == "https://aem.example.com/custom/HP-01"

    def test_missing_base_url_for_relative_path_raises(self):
        cfg = _request_building_config()
        cfg["apis"]["no_url_api"] = {"base_url": ""}
        cfg["routes"].append({
            "name": "no_url_route",
            "source": "src_cmd",
            "message_profile": "hp_commands",
            "api": "no_url_api",
            "endpoint": "hp_control",
            "priority": 200,
        })
        router = _build_request_router(cfg)
        msg = _hp_message()
        route = [r for r in router.routes if r.name == "no_url_route"][0]

        with pytest.raises(ValueError, match="no base_url"):
            build_resolved_request(msg, route, router, forwarder_dry_run=True)


# ========================================================================
# Body building
# ========================================================================

class TestBodyBuilding:

    def test_body_template_maps_top_level_fields(self):
        cfg = _request_building_config()
        cfg["routes"].append({
            "name": "tmpl_route",
            "source": "src_cmd",
            "message_profile": "hp_commands",
            "api": "aem_api",
            "endpoint": "template_body_ep",
            "priority": 200,
        })
        router = _build_request_router(cfg)
        msg = _hp_message()
        route = [r for r in router.routes if r.name == "tmpl_route"][0]

        req = build_resolved_request(msg, route, router, forwarder_dry_run=True)
        assert req.body["cmd"] == "curtail"
        assert req.body["asset"] == "HP-01"

    def test_body_template_maps_nested_payload_fields(self):
        cfg = _request_building_config()
        cfg["routes"].append({
            "name": "tmpl_route",
            "source": "src_cmd",
            "message_profile": "hp_commands",
            "api": "aem_api",
            "endpoint": "template_body_ep",
            "priority": 200,
        })
        router = _build_request_router(cfg)
        msg = _hp_message()
        route = [r for r in router.routes if r.name == "tmpl_route"][0]

        req = build_resolved_request(msg, route, router, forwarder_dry_run=True)
        assert req.body["power"] == 2.5

    def test_map_with_list_fallback(self):
        cfg = _request_building_config()
        cfg["routes"].append({
            "name": "fallback_route",
            "source": "src_cmd",
            "message_profile": "hp_commands",
            "api": "aem_api",
            "endpoint": "map_list_fallback_ep",
            "priority": 200,
        })
        router = _build_request_router(cfg)
        msg = _hp_message()
        route = [r for r in router.routes if r.name == "fallback_route"][0]

        req = build_resolved_request(msg, route, router, forwarder_dry_run=True)
        assert req.body["ts"] == "2026-06-15T12:00:00"

    def test_map_list_falls_through_to_second(self):
        cfg = _request_building_config()
        cfg["routes"].append({
            "name": "fallback_route",
            "source": "src_cmd",
            "message_profile": "hp_commands",
            "api": "aem_api",
            "endpoint": "map_list_fallback_ep",
            "priority": 200,
        })
        router = _build_request_router(cfg)
        msg = _hp_message()
        del msg["payload"]["slot_start"]
        route = [r for r in router.routes if r.name == "fallback_route"][0]

        req = build_resolved_request(msg, route, router, forwarder_dry_run=True)
        assert req.body["ts"] == "2026-06-15T12:00:00"

    def test_body_mode_hp_control(self):
        cfg = _request_building_config()
        cfg["routes"].append({
            "name": "hp_mode_route",
            "source": "src_cmd",
            "message_profile": "hp_commands",
            "api": "aem_api",
            "endpoint": "hp_mode",
            "priority": 200,
        })
        router = _build_request_router(cfg)
        msg = _hp_message()
        route = [r for r in router.routes if r.name == "hp_mode_route"][0]

        req = build_resolved_request(msg, route, router, forwarder_dry_run=True)
        assert req.body["command"] == "curtail"
        assert req.body["asset_id"] == "HP-01"
        assert req.body["asset_type"] == "heat_pump"
        assert req.body["timestamp"] == "2026-06-15T12:00:00"
        assert "payload" in req.body

    def test_body_mode_ev_power_timeseries(self):
        router = _build_request_router()
        msg = _ev_message()
        route = router.routes[1]  # ev_to_aem

        req = build_resolved_request(msg, route, router, forwarder_dry_run=True)
        assert isinstance(req.body, dict)
        assert len(req.body) == 1
        ts_key = list(req.body.keys())[0]
        assert "2026-06-15" in ts_key
        assert req.body[ts_key] == 7.4

    def test_body_mode_takes_precedence_over_body_template(self):
        cfg = _request_building_config()
        cfg["routes"].append({
            "name": "both_route",
            "source": "src_cmd",
            "message_profile": "hp_commands",
            "api": "aem_api",
            "endpoint": "both_mode_and_template_ep",
            "priority": 200,
        })
        router = _build_request_router(cfg)
        msg = _hp_message()
        route = [r for r in router.routes if r.name == "both_route"][0]

        req = build_resolved_request(msg, route, router, forwarder_dry_run=True)
        assert "command" in req.body
        assert "should_be" not in req.body

    def test_passthrough_body_when_no_mode_or_template(self):
        cfg = _request_building_config()
        cfg["routes"].append({
            "name": "pass_route",
            "source": "src_cmd",
            "message_profile": "hp_commands",
            "api": "aem_api",
            "endpoint": "passthrough_ep",
            "priority": 200,
        })
        router = _build_request_router(cfg)
        msg = _hp_message()
        route = [r for r in router.routes if r.name == "pass_route"][0]

        req = build_resolved_request(msg, route, router, forwarder_dry_run=True)
        assert req.body["message_type"] == "command"
        assert req.body["asset_id"] == "HP-01"
        assert req.body["payload"]["target_power_kw"] == 2.5

    def test_input_message_is_not_mutated(self):
        cfg = _request_building_config()
        cfg["routes"].append({
            "name": "tmpl_route",
            "source": "src_cmd",
            "message_profile": "hp_commands",
            "api": "aem_api",
            "endpoint": "template_body_ep",
            "priority": 200,
        })
        router = _build_request_router(cfg)
        msg = _hp_message()
        original = copy.deepcopy(msg)
        route = [r for r in router.routes if r.name == "tmpl_route"][0]

        build_resolved_request(msg, route, router, forwarder_dry_run=True)
        assert msg == original

    def test_passthrough_body_is_independent_copy(self):
        cfg = _request_building_config()
        cfg["routes"].append({
            "name": "pass_route",
            "source": "src_cmd",
            "message_profile": "hp_commands",
            "api": "aem_api",
            "endpoint": "passthrough_ep",
            "priority": 200,
        })
        router = _build_request_router(cfg)
        msg = _hp_message()
        route = [r for r in router.routes if r.name == "pass_route"][0]

        req = build_resolved_request(msg, route, router, forwarder_dry_run=True)
        req.body["payload"]["target_power_kw"] = 999.0
        assert msg["payload"]["target_power_kw"] == 2.5


# ========================================================================
# Request object
# ========================================================================

class TestResolvedRequest:

    def test_method_defaults_to_post(self):
        router = _build_request_router()
        msg = _hp_message()
        route = router.routes[0]

        req = build_resolved_request(msg, route, router, forwarder_dry_run=True)
        assert req.method == "POST"

    def test_custom_method_is_respected(self):
        cfg = _request_building_config()
        cfg["routes"].append({
            "name": "put_route",
            "source": "src_cmd",
            "message_profile": "hp_commands",
            "api": "aem_api",
            "endpoint": "absolute_ep",
            "priority": 200,
        })
        router = _build_request_router(cfg)
        msg = _hp_message()
        route = [r for r in router.routes if r.name == "put_route"][0]

        req = build_resolved_request(msg, route, router, forwarder_dry_run=True)
        assert req.method == "PUT"

    def test_headers_are_included(self):
        cfg = _request_building_config()
        cfg["routes"].append({
            "name": "hdr_route",
            "source": "src_cmd",
            "message_profile": "hp_commands",
            "api": "aem_api",
            "endpoint": "absolute_ep",
            "priority": 200,
        })
        router = _build_request_router(cfg)
        msg = _hp_message()
        route = [r for r in router.routes if r.name == "hdr_route"][0]

        req = build_resolved_request(msg, route, router, forwarder_dry_run=True)
        assert req.headers == {"X-Custom": "value"}

    def test_success_status_codes_default(self):
        router = _build_request_router()
        msg = _hp_message()
        route = router.routes[0]

        req = build_resolved_request(msg, route, router, forwarder_dry_run=True)
        assert req.success_status_codes == [200, 201, 202, 204]

    def test_custom_success_status_codes(self):
        cfg = _request_building_config()
        cfg["routes"].append({
            "name": "code_route",
            "source": "src_cmd",
            "message_profile": "hp_commands",
            "api": "aem_api",
            "endpoint": "absolute_ep",
            "priority": 200,
        })
        router = _build_request_router(cfg)
        msg = _hp_message()
        route = [r for r in router.routes if r.name == "code_route"][0]

        req = build_resolved_request(msg, route, router, forwarder_dry_run=True)
        assert req.success_status_codes == [200, 204]

    def test_api_timeout_retries_verify_ssl(self):
        router = _build_request_router()
        msg = _hp_message()
        route = router.routes[0]

        req = build_resolved_request(msg, route, router, forwarder_dry_run=True)
        assert req.timeout_seconds == 15.0
        assert req.request_retries == 2
        assert req.verify_ssl is True

    def test_api_defaults_for_bare_api(self):
        cfg = _request_building_config()
        cfg["routes"].append({
            "name": "bare_route",
            "source": "src_cmd",
            "message_profile": "hp_commands",
            "api": "bare_api",
            "endpoint": "hp_control",
            "priority": 200,
        })
        router = _build_request_router(cfg)
        msg = _hp_message()
        route = [r for r in router.routes if r.name == "bare_route"][0]

        req = build_resolved_request(msg, route, router, forwarder_dry_run=True)
        assert req.timeout_seconds == 10.0
        assert req.request_retries == 3
        assert req.verify_ssl is False
        assert req.auth is None

    def test_auth_from_api_config(self):
        router = _build_request_router()
        msg = _hp_message()
        route = router.routes[0]

        req = build_resolved_request(msg, route, router, forwarder_dry_run=True)
        assert req.auth == ("admin", "secret")

    def test_diagnostic_names_preserved(self):
        router = _build_request_router()
        msg = _hp_message()
        route = router.routes[0]

        req = build_resolved_request(msg, route, router, forwarder_dry_run=True)
        assert req.route_name == "hp_to_aem"
        assert req.api_name == "aem_api"
        assert req.endpoint_name == "hp_control"


# ========================================================================
# Dry-run in request building
# ========================================================================

class TestRequestDryRun:

    def test_forwarder_dry_run_prevents_live(self):
        router = _build_request_router()
        msg = _hp_message()
        route = router.routes[0]

        req = build_resolved_request(msg, route, router, forwarder_dry_run=True)
        assert req.effective_dry_run is True

    def test_route_dry_run_prevents_live(self):
        cfg = _request_building_config()
        cfg["routes"][0]["dry_run"] = True
        router = _build_request_router(cfg)
        msg = _hp_message()
        route = router.routes[0]

        req = build_resolved_request(msg, route, router, forwarder_dry_run=False)
        assert req.effective_dry_run is True

    def test_message_dry_run_prevents_live(self):
        router = _build_request_router()
        msg = _hp_message()
        msg["payload"]["dry_run"] = True
        route = router.routes[0]

        req = build_resolved_request(msg, route, router, forwarder_dry_run=False)
        assert req.effective_dry_run is True

    def test_all_false_means_live(self):
        router = _build_request_router()
        msg = _hp_message()
        msg["payload"]["dry_run"] = False
        route = router.routes[0]

        req = build_resolved_request(msg, route, router, forwarder_dry_run=False)
        assert req.effective_dry_run is False

    def test_missing_message_dry_run_default_true(self):
        router = _build_request_router()
        msg = _hp_message()
        del msg["payload"]["dry_run"]
        route = router.routes[0]

        req = build_resolved_request(msg, route, router, forwarder_dry_run=False)
        assert req.effective_dry_run is True

    def test_missing_message_dry_run_default_false(self):
        cfg = _request_building_config()
        cfg["defaults"]["missing_message_dry_run_default"] = False
        router = _build_request_router(cfg)
        msg = _hp_message()
        del msg["payload"]["dry_run"]
        route = router.routes[0]

        req = build_resolved_request(msg, route, router, forwarder_dry_run=False)
        assert req.effective_dry_run is False

    def test_slot_info_dry_run_extracted(self):
        router = _build_request_router()
        msg = _hp_message()
        del msg["payload"]["dry_run"]
        msg["slot_info"] = {"dry_run": True}
        route = router.routes[0]

        req = build_resolved_request(msg, route, router, forwarder_dry_run=False)
        assert req.effective_dry_run is True


# ========================================================================
# extract_message_dry_run helper
# ========================================================================

class TestExtractMessageDryRun:

    def test_reads_payload_dry_run(self):
        msg = {"payload": {"dry_run": True}}
        assert extract_message_dry_run(msg) is True

    def test_reads_slot_info_dry_run(self):
        msg = {"slot_info": {"dry_run": False}}
        assert extract_message_dry_run(msg) is False

    def test_payload_takes_precedence_over_slot_info(self):
        msg = {"payload": {"dry_run": False}, "slot_info": {"dry_run": True}}
        assert extract_message_dry_run(msg) is False

    def test_returns_none_when_absent(self):
        msg = {"payload": {"power_kw": 3.0}}
        assert extract_message_dry_run(msg) is None

    def test_returns_none_for_empty_message(self):
        assert extract_message_dry_run({}) is None


# ========================================================================
# Step 3 — HTTP dispatch helpers
# ========================================================================

class _MockResponse:
    """Minimal mock for an HTTP response."""

    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


class _MockSession:
    """Injectable mock session that records calls and returns pre-set
    responses (or raises pre-set exceptions)."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _make_resolved_request(
    dry_run: bool = False,
    retries: int = 2,
    success_codes=None,
    **overrides,
) -> ResolvedRequest:
    """Build a ResolvedRequest with sensible defaults for dispatch tests."""
    defaults = dict(
        route_name="test_route",
        api_name="test_api",
        endpoint_name="test_ep",
        method="POST",
        url="https://api.example.com/v1/control",
        headers=None,
        body={"command": "curtail", "asset_id": "HP-01"},
        auth=("admin", "secret"),
        timeout_seconds=10.0,
        verify_ssl=True,
        request_retries=retries,
        success_status_codes=success_codes or [200, 201, 202, 204],
        effective_dry_run=dry_run,
    )
    defaults.update(overrides)
    return ResolvedRequest(**defaults)


def _simple_message(**overrides):
    """A minimal message dict for dispatch logging context."""
    msg = {
        "message_type": "command",
        "asset_type": "heat_pump",
        "asset_id": "HP-01",
    }
    msg.update(overrides)
    return msg


# ========================================================================
# Dry-run dispatch
# ========================================================================

class TestDryRunDispatch:

    def test_dry_run_does_not_call_http_client(self):
        session = _MockSession([])
        req = _make_resolved_request(dry_run=True)

        result = dispatch_http_request(req, _simple_message(), session=session)
        assert len(session.calls) == 0

    def test_dry_run_returns_success_true_and_dry_run_true(self):
        req = _make_resolved_request(dry_run=True)
        result = dispatch_http_request(
            req, _simple_message(), session=_MockSession([])
        )
        assert result.success is True
        assert result.dry_run is True

    def test_dry_run_logs_marker(self, caplog):
        req = _make_resolved_request(dry_run=True)
        with caplog.at_level("INFO", logger="forwarder"):
            dispatch_http_request(
                req, _simple_message(), session=_MockSession([])
            )
        assert any("[DRY-RUN]" in r.message for r in caplog.records)

    def test_dry_run_log_includes_route_api_endpoint_method_url(self, caplog):
        req = _make_resolved_request(dry_run=True)
        with caplog.at_level("INFO", logger="forwarder"):
            dispatch_http_request(
                req, _simple_message(), session=_MockSession([])
            )
        log_text = " ".join(r.message for r in caplog.records)
        assert "test_route" in log_text
        assert "test_api" in log_text
        assert "test_ep" in log_text
        assert "POST" in log_text
        assert "https://api.example.com/v1/control" in log_text


# ========================================================================
# Successful HTTP dispatch
# ========================================================================

class TestSuccessfulDispatch:

    def test_post_success_200(self):
        session = _MockSession([_MockResponse(200)])
        req = _make_resolved_request()

        result = dispatch_http_request(req, _simple_message(), session=session)
        assert result.success is True
        assert result.dry_run is False
        assert result.status_code == 200

    def test_status_202_in_success_codes(self):
        session = _MockSession([_MockResponse(202)])
        req = _make_resolved_request()

        result = dispatch_http_request(req, _simple_message(), session=session)
        assert result.success is True
        assert result.status_code == 202

    def test_custom_method_headers_auth_timeout_verify_passed(self):
        session = _MockSession([_MockResponse(200)])
        req = _make_resolved_request(
            method="PUT",
            headers={"X-Token": "abc"},
            auth=("user", "pass"),
            timeout_seconds=30.0,
            verify_ssl=False,
        )

        dispatch_http_request(req, _simple_message(), session=session)

        call = session.calls[0]
        assert call["method"] == "PUT"
        assert call["headers"] == {"X-Token": "abc"}
        assert call["auth"] == ("user", "pass")
        assert call["timeout"] == 30.0
        assert call["verify"] is False

    def test_custom_success_status_codes(self):
        session = _MockSession([_MockResponse(206)])
        req = _make_resolved_request(success_codes=[200, 206])

        result = dispatch_http_request(req, _simple_message(), session=session)
        assert result.success is True
        assert result.status_code == 206

    def test_success_on_retry(self):
        session = _MockSession([
            _MockResponse(500),
            _MockResponse(200),
        ])
        req = _make_resolved_request(retries=2)

        result = dispatch_http_request(req, _simple_message(), session=session)
        assert result.success is True
        assert result.attempts == 2


# ========================================================================
# HTTP failure policy
# ========================================================================

class TestHttpFailurePolicy:

    def test_500_after_retries_returns_failure(self):
        session = _MockSession([
            _MockResponse(500),
            _MockResponse(500),
            _MockResponse(500),
        ])
        req = _make_resolved_request(retries=2)

        result = dispatch_http_request(req, _simple_message(), session=session)
        assert result.success is False
        assert result.status_code == 500

    def test_failure_result_has_ack_error_no_requeue(self):
        session = _MockSession([_MockResponse(500)] * 3)
        req = _make_resolved_request(retries=2)

        result = dispatch_http_request(req, _simple_message(), session=session)
        assert result.policy == "ack_error_no_requeue"

    def test_failure_logs_error_with_diagnostics(self, caplog):
        session = _MockSession([_MockResponse(500)] * 3)
        req = _make_resolved_request(retries=2)

        with caplog.at_level("ERROR", logger="forwarder"):
            dispatch_http_request(req, _simple_message(), session=session)

        error_msgs = [
            r.message for r in caplog.records if r.levelname == "ERROR"
        ]
        assert len(error_msgs) >= 1
        text = error_msgs[-1]
        assert "test_route" in text
        assert "test_api" in text
        assert "test_ep" in text
        assert "https://api.example.com/v1/control" in text
        assert "500" in text
        assert "HP-01" in text
        assert "heat_pump" in text
        assert "command" in text
        assert "ack_error_no_requeue" in text

    def test_failure_records_number_of_attempts(self):
        session = _MockSession([_MockResponse(500)] * 3)
        req = _make_resolved_request(retries=2)

        result = dispatch_http_request(req, _simple_message(), session=session)
        assert result.attempts == 3

    def test_failure_result_names(self):
        session = _MockSession([_MockResponse(400)])
        req = _make_resolved_request(retries=0)

        result = dispatch_http_request(req, _simple_message(), session=session)
        assert result.route_name == "test_route"
        assert result.api_name == "test_api"
        assert result.endpoint_name == "test_ep"


# ========================================================================
# Retry behavior
# ========================================================================

class TestRetryBehavior:

    def test_500_is_retried(self):
        session = _MockSession([_MockResponse(500), _MockResponse(200)])
        req = _make_resolved_request(retries=1)

        result = dispatch_http_request(req, _simple_message(), session=session)
        assert result.success is True
        assert result.attempts == 2
        assert len(session.calls) == 2

    def test_503_is_retried(self):
        session = _MockSession([_MockResponse(503), _MockResponse(200)])
        req = _make_resolved_request(retries=1)

        result = dispatch_http_request(req, _simple_message(), session=session)
        assert result.success is True
        assert result.attempts == 2

    def test_timeout_exception_is_retried(self):
        session = _MockSession([
            ConnectionError("connection refused"),
            _MockResponse(200),
        ])
        req = _make_resolved_request(retries=1)

        result = dispatch_http_request(req, _simple_message(), session=session)
        assert result.success is True
        assert result.attempts == 2

    def test_429_is_retried(self):
        session = _MockSession([_MockResponse(429), _MockResponse(200)])
        req = _make_resolved_request(retries=1)

        result = dispatch_http_request(req, _simple_message(), session=session)
        assert result.success is True
        assert result.attempts == 2

    def test_400_is_not_retried(self):
        session = _MockSession([_MockResponse(400), _MockResponse(200)])
        req = _make_resolved_request(retries=1)

        result = dispatch_http_request(req, _simple_message(), session=session)
        assert result.success is False
        assert result.attempts == 1
        assert len(session.calls) == 1

    def test_401_is_not_retried(self):
        session = _MockSession([_MockResponse(401), _MockResponse(200)])
        req = _make_resolved_request(retries=1)

        result = dispatch_http_request(req, _simple_message(), session=session)
        assert result.success is False
        assert result.attempts == 1

    def test_404_is_not_retried(self):
        session = _MockSession([_MockResponse(404)])
        req = _make_resolved_request(retries=2)

        result = dispatch_http_request(req, _simple_message(), session=session)
        assert result.success is False
        assert result.attempts == 1

    def test_422_is_not_retried(self):
        session = _MockSession([_MockResponse(422)])
        req = _make_resolved_request(retries=2)

        result = dispatch_http_request(req, _simple_message(), session=session)
        assert result.success is False
        assert result.attempts == 1

    def test_connection_error_exhausts_retries(self):
        session = _MockSession([
            OSError("network unreachable"),
            OSError("network unreachable"),
            OSError("network unreachable"),
        ])
        req = _make_resolved_request(retries=2)

        result = dispatch_http_request(req, _simple_message(), session=session)
        assert result.success is False
        assert result.attempts == 3
        assert result.status_code is None
        assert "network unreachable" in result.error


# ========================================================================
# Safety
# ========================================================================

class TestDispatchSafety:

    def test_resolved_request_not_mutated(self):
        session = _MockSession([_MockResponse(200)])
        req = _make_resolved_request()
        original_url = req.url
        original_body = copy.deepcopy(req.body)

        dispatch_http_request(req, _simple_message(), session=session)

        assert req.url == original_url
        assert req.body == original_body
        assert req.effective_dry_run is False
        assert req.method == "POST"

    def test_message_not_mutated(self):
        session = _MockSession([_MockResponse(200)])
        req = _make_resolved_request()
        msg = _simple_message()
        original = copy.deepcopy(msg)

        dispatch_http_request(req, msg, session=session)
        assert msg == original

    def test_zero_retries_means_one_attempt(self):
        session = _MockSession([_MockResponse(500)])
        req = _make_resolved_request(retries=0)

        result = dispatch_http_request(req, _simple_message(), session=session)
        assert result.success is False
        assert result.attempts == 1
        assert len(session.calls) == 1


# ============================================================================
# Unknown-field validation
# ============================================================================

class TestUnknownFieldValidation:
    """Verify that validate_routing_config rejects unknown/mistyped fields."""

    def _base_config(self):
        return {
            "version": 2,
            "sources": {"src": {"section": "realAssetCommands"}},
            "message_profiles": {"mp": {"message_type": "command"}},
            "apis": {"api1": {"base_url": "http://example.com"}},
            "endpoints": {"ep1": {"path_template": "/test", "method": "POST"}},
            "routes": [{
                "name": "r1", "source": "src",
                "message_profile": "mp", "api": "api1", "endpoint": "ep1",
            }],
        }

    def test_endpoint_unknown_field_raises(self):
        cfg = self._base_config()
        cfg["endpoints"]["ep1"]["bogus_field"] = True
        with pytest.raises(RoutingConfigError, match="Unknown endpoint field 'bogus_field'"):
            validate_routing_config(cfg)

    def test_endpoint_path_raises_with_suggestion(self):
        cfg = self._base_config()
        cfg["endpoints"]["ep1"]["path"] = "/wrong"
        with pytest.raises(RoutingConfigError, match="Did you mean 'path_template'"):
            validate_routing_config(cfg)

    def test_route_unknown_field_raises(self):
        cfg = self._base_config()
        cfg["routes"][0]["bogus"] = "x"
        with pytest.raises(RoutingConfigError, match="Unknown route field 'bogus'"):
            validate_routing_config(cfg)

    def test_api_unknown_field_raises(self):
        cfg = self._base_config()
        cfg["apis"]["api1"]["bogus"] = True
        with pytest.raises(RoutingConfigError, match="Unknown api field 'bogus'"):
            validate_routing_config(cfg)

    def test_message_profile_unknown_field_raises(self):
        cfg = self._base_config()
        cfg["message_profiles"]["mp"]["bogus"] = True
        with pytest.raises(RoutingConfigError, match="Unknown message_profile field 'bogus'"):
            validate_routing_config(cfg)

    def test_source_unknown_field_raises(self):
        cfg = self._base_config()
        cfg["sources"]["src"]["bogus"] = True
        with pytest.raises(RoutingConfigError, match="Unknown source field 'bogus'"):
            validate_routing_config(cfg)

    def test_defaults_unknown_field_raises(self):
        cfg = self._base_config()
        cfg["defaults"] = {"missing_message_dry_run_default": True, "bogus": 1}
        with pytest.raises(RoutingConfigError, match="Unknown defaults field 'bogus'"):
            validate_routing_config(cfg)

    def test_comment_field_is_allowed_everywhere(self):
        cfg = self._base_config()
        cfg["sources"]["src"]["comment"] = "test"
        cfg["message_profiles"]["mp"]["comment"] = "test"
        cfg["apis"]["api1"]["comment"] = "test"
        cfg["endpoints"]["ep1"]["comment"] = "test"
        cfg["routes"][0]["comment"] = "test"
        cfg["defaults"] = {"missing_message_dry_run_default": True, "comment": "test"}
        result = validate_routing_config(cfg)
        assert "routes" in result

    def test_production_config_still_validates(self):
        import json
        cfg_path = os.path.join(
            os.path.dirname(__file__), "..", "conf", "forwarder_routes.json"
        )
        with open(cfg_path) as f:
            cfg = json.load(f)
        result = validate_routing_config(cfg)
        assert len(result["routes"]) > 0
