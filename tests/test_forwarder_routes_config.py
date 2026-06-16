"""Tests for the production v2 routing config: conf/forwarder_routes.json.

Validates the config loads, passes schema validation, routes resolve
correctly for representative messages, and policies/defaults are set.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from forwarder_router import (
    MessageRouter,
    RoutingDefaults,
    detect_config_mode,
    extract_message_dry_run,
    resolve_effective_dry_run,
    validate_routing_config,
)


CONFIG_PATH = os.path.join(
    os.path.dirname(__file__), "..", "conf", "forwarder_routes.json"
)


@pytest.fixture(scope="module")
def config_data():
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def validated(config_data):
    return validate_routing_config(config_data)


@pytest.fixture(scope="module")
def router(validated):
    return MessageRouter.from_validated_config(validated)


# =========================================================================
# Config loading and detection
# =========================================================================

class TestConfigLoading:

    def test_file_exists(self):
        assert os.path.isfile(CONFIG_PATH)

    def test_detected_as_v2(self, config_data):
        assert detect_config_mode(config_data) == "v2"

    def test_validation_passes(self, config_data):
        result = validate_routing_config(config_data)
        assert "routes" in result
        assert "defaults" in result
        assert "sources" in result
        assert "apis" in result
        assert "endpoints" in result

    def test_router_instantiates(self, router):
        assert router is not None
        assert len(router.routes) > 0


# =========================================================================
# Route uniqueness and no ambiguity
# =========================================================================

class TestRouteIntegrity:

    def test_route_names_are_unique(self, router):
        names = [r.name for r in router.routes]
        assert len(names) == len(set(names))

    def test_no_ambiguity_ecm96_2(self, router):
        msg = {
            "message_type": "command",
            "asset_type": "heat_pump",
            "asset_id": "ECM96.2",
            "payload": {"dry_run": True},
        }
        result = router.resolve(msg, "realAssetCommands")
        assert result.status == "matched"

    def test_no_ambiguity_ecm97_3(self, router):
        msg = {
            "message_type": "command",
            "asset_type": "heat_pump",
            "asset_id": "ECM97.3",
            "payload": {},
        }
        result = router.resolve(msg, "realAssetCommands")
        assert result.status == "matched"

    def test_no_ambiguity_ecm63_1(self, router):
        msg = {
            "message_type": "command",
            "asset_type": "ev_charger",
            "asset_id": "ECM63.1",
            "payload": {},
        }
        result = router.resolve(msg, "realAssetCommands")
        assert result.status == "matched"

    def test_no_ambiguity_ecm63_2(self, router):
        msg = {
            "message_type": "command",
            "asset_type": "ev_charger",
            "asset_id": "ECM63.2",
            "payload": {},
        }
        result = router.resolve(msg, "realAssetCommands")
        assert result.status == "matched"

    def test_no_ambiguity_unknown_asset(self, router):
        msg = {
            "message_type": "command",
            "asset_type": "heat_pump",
            "asset_id": "HP-UNKNOWN",
            "payload": {},
        }
        result = router.resolve(msg, "realAssetCommands")
        assert result.status == "matched"


# =========================================================================
# Route resolution for representative messages
# =========================================================================

class TestRouteResolution:

    def test_ecm96_2_resolves_to_hp_route(self, router):
        msg = {
            "message_type": "command",
            "asset_type": "heat_pump",
            "asset_id": "ECM96.2",
            "command_type": "curtail",
            "payload": {"dry_run": False},
        }
        result = router.resolve(msg, "realAssetCommands")
        assert result.status == "matched"
        assert result.route.name == "ecm96_2_hp"
        assert result.route.endpoint == "hp_ecm96_2"

    def test_ecm97_3_resolves_to_hp_route(self, router):
        msg = {
            "message_type": "command",
            "asset_type": "heat_pump",
            "asset_id": "ECM97.3",
            "command_type": "curtail",
            "payload": {"dry_run": False},
        }
        result = router.resolve(msg, "realAssetCommands")
        assert result.status == "matched"
        assert result.route.name == "ecm97_3_hp"
        assert result.route.endpoint == "hp_ecm97_3"

    def test_ecm63_1_resolves_to_ev_route(self, router):
        msg = {
            "message_type": "command",
            "asset_type": "ev_charger",
            "asset_id": "ECM63.1",
            "command_type": "curtail",
            "payload": {"dry_run": False},
        }
        result = router.resolve(msg, "realAssetCommands")
        assert result.status == "matched"
        assert result.route.name == "ecm63_1_ev"
        assert result.route.endpoint == "ev_ecm63_1"

    def test_ecm63_2_resolves_to_ev_route(self, router):
        msg = {
            "message_type": "command",
            "asset_type": "ev_charger",
            "asset_id": "ECM63.2",
            "command_type": "curtail",
            "payload": {"dry_run": False},
        }
        result = router.resolve(msg, "realAssetCommands")
        assert result.status == "matched"
        assert result.route.name == "ecm63_2_ev"
        assert result.route.endpoint == "ev_ecm63_2"

    def test_unknown_command_resolves_to_fallback(self, router):
        msg = {
            "message_type": "command",
            "asset_type": "heat_pump",
            "asset_id": "HP-UNKNOWN",
            "command_type": "curtail",
            "payload": {},
        }
        result = router.resolve(msg, "realAssetCommands")
        assert result.status == "matched"
        assert result.route.name == "fallback_aem_command"
        assert result.route.endpoint == "default_aem_command"

    def test_measurement_no_match(self, router):
        msg = {
            "message_type": "measurement",
            "asset_type": "heat_pump",
            "asset_id": "ECM96.2",
            "payload": {},
        }
        result = router.resolve(msg, "realAssetCommands")
        assert result.status == "no_match"

    def test_wrong_source_section_no_match(self, router):
        msg = {
            "message_type": "command",
            "asset_type": "heat_pump",
            "asset_id": "ECM96.2",
            "payload": {},
        }
        result = router.resolve(msg, "simulatedAssetCommands")
        assert result.status == "no_match"

    def test_simulated_measure_matches(self, router):
        """Measurements from simulatedAssetMeasures route to sim_measure_forward."""
        msg = {
            "message_type": "measurement",
            "asset_type": "heat_pump",
            "asset_id": "ECM68.3",
            "measurement_type": "power",
            "payload": {"power_kw": 2.5},
        }
        result = router.resolve(msg, "simulatedAssetMeasures")
        assert result.status == "matched"
        assert result.route.name == "sim_measure_forward"
        assert result.route.api == "aem_test_api"
        assert result.route.endpoint == "sim_measure_passthrough"


# =========================================================================
# Source derivation
# =========================================================================

class TestSourceDerivation:

    def test_config_contains_real_commands_source(self, validated):
        assert "real_commands" in validated["sources"]

    def test_config_contains_simulated_measures_source(self, validated):
        assert "simulated_measures" in validated["sources"]

    def test_simulated_measures_section_is_correct(self, validated):
        src = validated["sources"]["simulated_measures"]
        assert src.get("section") == "simulatedAssetMeasures"

    def test_all_configured_sources_derive_two_sections(self, validated):
        sections = set()
        for src_def in validated["sources"].values():
            sec = src_def.get("section")
            if sec:
                sections.add(sec)
        assert sections == {"realAssetCommands", "simulatedAssetMeasures"}

    def test_sim_measure_route_exists(self, router):
        """simulated_measures has exactly one route: sim_measure_forward."""
        sim_routes = [r for r in router.routes if r.source == "simulated_measures"]
        assert len(sim_routes) == 1
        assert sim_routes[0].name == "sim_measure_forward"

    def test_no_route_references_simulatedAssetCommands(self, router):
        for route in router.routes:
            src_def = router.sources.get(route.source, {})
            assert src_def.get("section") != "simulatedAssetCommands"


# =========================================================================
# Policies and defaults
# =========================================================================

class TestPoliciesAndDefaults:

    def test_on_no_match_policy(self, validated):
        defaults: RoutingDefaults = validated["defaults"]
        assert defaults.on_no_match == "ack_warn_no_forward"

    def test_on_ambiguous_match_policy(self, validated):
        defaults: RoutingDefaults = validated["defaults"]
        assert defaults.on_ambiguous_match == "ack_error_no_forward"

    def test_on_http_failure_policy(self, validated):
        defaults: RoutingDefaults = validated["defaults"]
        assert defaults.on_http_failure == "ack_error_no_requeue"

    def test_missing_message_dry_run_default_true(self, validated):
        defaults: RoutingDefaults = validated["defaults"]
        assert defaults.missing_message_dry_run_default is True

    def test_dry_run_resolves_with_missing_payload_flag(self, router):
        msg = {
            "message_type": "command",
            "asset_type": "heat_pump",
            "asset_id": "ECM96.2",
            "payload": {"power_kw": 3.0},
        }
        message_dry_run = extract_message_dry_run(msg)
        assert message_dry_run is None

        effective = resolve_effective_dry_run(
            forwarder_dry_run=False,
            route_dry_run=None,
            message_dry_run=message_dry_run,
            missing_message_dry_run_default=(
                router.defaults.missing_message_dry_run_default
            ),
        )
        assert effective is True


# =========================================================================
# Endpoint and API config
# =========================================================================

class TestEndpointAndApiConfig:

    def test_aem_api_has_reference_api(self, validated):
        aem = validated["apis"]["aem_api"]
        assert aem.reference_api == "aemAPI"

    def test_aem_api_timeout(self, validated):
        aem = validated["apis"]["aem_api"]
        assert aem.timeout == 5.0

    def test_aem_api_retries(self, validated):
        aem = validated["apis"]["aem_api"]
        assert aem.retries == 3

    def test_aem_api_verify_ssl_false(self, validated):
        aem = validated["apis"]["aem_api"]
        assert aem.verify_ssl is False

    def test_hp_endpoint_body_mode(self, validated):
        ep = validated["endpoints"]["hp_ecm96_2"]
        assert ep.body_mode == "hp_control"

    def test_ev_endpoint_body_mode(self, validated):
        ep = validated["endpoints"]["ev_ecm63_1"]
        assert ep.body_mode == "ev_power_timeseries"

    def test_fallback_endpoint_has_body_template(self, validated):
        ep = validated["endpoints"]["default_aem_command"]
        assert ep.body_template is not None
        assert "$map" in ep.body_template.get("power", {})

    def test_real_command_routes_reference_aem_api(self, router):
        for route in router.routes:
            if route.source == "real_commands":
                assert route.api == "aem_api"

    def test_sim_measure_route_references_aem_test_api(self, router):
        sim = [r for r in router.routes if r.name == "sim_measure_forward"]
        assert len(sim) == 1
        assert sim[0].api == "aem_test_api"

    def test_aem_test_api_has_reference_api(self, validated):
        api = validated["apis"]["aem_test_api"]
        assert api.reference_api == "aemAPITest"

    def test_aem_test_api_timeout(self, validated):
        api = validated["apis"]["aem_test_api"]
        assert api.timeout == 5.0

    def test_aem_test_api_verify_ssl_false(self, validated):
        api = validated["apis"]["aem_test_api"]
        assert api.verify_ssl is False

    def test_sim_measure_endpoint_is_passthrough(self, validated):
        ep = validated["endpoints"]["sim_measure_passthrough"]
        assert ep.method == "POST"
        assert ep.path_template == ""
        assert ep.body_mode is None
        assert ep.body_template is None

    def test_asset_specific_routes_are_high_priority(self, router):
        for route in router.routes:
            if route.name not in ("fallback_aem_command", "sim_measure_forward"):
                assert route.priority == 100

    def test_fallback_route_is_low_priority(self, router):
        fallback = [r for r in router.routes if r.name == "fallback_aem_command"]
        assert len(fallback) == 1
        assert fallback[0].priority == 0
