"""Tests for the forwarder configurable routing core (scripts/forwarder_router.py).

Covers:
  - MessageProfile matching
  - Route resolution (priority, ambiguity, no-match, disabled, source)
  - Config mode detection and validation
  - Dry-run resolution helper
"""

import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from forwarder_router import (
    ApiConfig,
    EndpointConfig,
    MessageProfile,
    MessageRouter,
    RouteConfig,
    RouteResolutionResult,
    RoutingConfigError,
    RoutingDefaults,
    detect_config_mode,
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
            route_dry_run=False,
            message_dry_run=True,
        ) is True

    def test_all_false_means_live(self):
        assert resolve_effective_dry_run(
            forwarder_dry_run=False,
            route_dry_run=False,
            message_dry_run=False,
        ) is False

    def test_missing_message_dry_run_uses_configured_default_true(self):
        assert resolve_effective_dry_run(
            forwarder_dry_run=False,
            route_dry_run=False,
            message_dry_run=None,
            missing_message_dry_run_default=True,
        ) is True

    def test_missing_message_dry_run_uses_configured_default_false(self):
        assert resolve_effective_dry_run(
            forwarder_dry_run=False,
            route_dry_run=False,
            message_dry_run=None,
            missing_message_dry_run_default=False,
        ) is False

    def test_route_without_dry_run_defaults_to_false(self):
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
