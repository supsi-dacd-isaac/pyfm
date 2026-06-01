import logging
import math
import re
import sys
import types
from importlib.util import find_spec
from importlib.machinery import ModuleSpec
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _stub_module_if_missing(name):
    try:
        missing = find_spec(name) is None
    except ValueError:
        missing = True
    if missing:
        module = types.ModuleType(name)
        module.__spec__ = ModuleSpec(name, loader=None)
        if name == "pandas":
            for attr in ("DataFrame", "Series", "Timestamp", "Timedelta", "DatetimeIndex"):
                setattr(module, attr, type(attr, (), {}))
        sys.modules[name] = module


_stub_module_if_missing("pandas")
_stub_module_if_missing("pytz")
try:
    psycopg2_missing = find_spec("psycopg2") is None
except ValueError:
    psycopg2_missing = True
if psycopg2_missing:
    psycopg2_stub = types.ModuleType("psycopg2")
    psycopg2_extras_stub = types.ModuleType("psycopg2.extras")
    psycopg2_stub.__spec__ = ModuleSpec("psycopg2", loader=None)
    psycopg2_extras_stub.__spec__ = ModuleSpec("psycopg2.extras", loader=None)
    psycopg2_stub.extras = psycopg2_extras_stub
    sys.modules["psycopg2"] = psycopg2_stub
    sys.modules["psycopg2.extras"] = psycopg2_extras_stub

from scripts import flexi_manager as fm  # noqa: E402


def _logger():
    return logging.getLogger("test_flexi_manager_activation")


def _manager_for_assets(asset_mapping):
    manager = fm.FlexibilityManager.__new__(fm.FlexibilityManager)
    manager.asset_mapping = asset_mapping
    return manager


def _detail_by_asset(selection):
    return {detail["asset_id"]: detail for detail in selection["asset_details"]}


def test_persistence_all_discrete_partial_acceptance_selects_small_subset():
    manager = _manager_for_assets({
        "ECM97.3": {
            "type": "heat_pump",
            "modulation_type": "discrete",
            "capacity_kw": 11.793,
        },
        "ECM96.2": {
            "type": "heat_pump",
            "modulation_type": "discrete",
            "capacity_kw": 1.141,
        },
    })

    allocations, selection = manager._build_persistence_activation_allocations(
        {"ECM97.3": 11.793, "ECM96.2": 1.141},
        accepted_kw=1.794,
    )

    assert allocations == {"ECM96.2": pytest.approx(1.141)}
    assert sum(allocations.values()) <= 1.794
    details = _detail_by_asset(selection)
    assert details["ECM96.2"]["selected"] is True
    assert details["ECM97.3"]["selected"] is False
    assert selection["under_delivery_kw"] == pytest.approx(1.794 - 1.141)


def test_persistence_all_discrete_acceptance_above_plan_uses_only_stored_values():
    manager = _manager_for_assets({
        "ECM97.3": {
            "type": "heat_pump",
            "modulation_type": "discrete",
            "capacity_kw": 11.793,
        },
        "ECM96.2": {
            "type": "heat_pump",
            "modulation_type": "discrete",
            "capacity_kw": 1.141,
        },
    })
    planned = {"ECM97.3": 11.793, "ECM96.2": 1.141}

    allocations, selection = manager._build_persistence_activation_allocations(
        planned,
        accepted_kw=20.0,
    )

    assert allocations == pytest.approx(planned)
    assert set(allocations) == set(planned)
    assert sum(allocations.values()) == pytest.approx(sum(planned.values()))
    assert selection["expected_delivered_kw"] == pytest.approx(sum(planned.values()))


def test_persistence_all_continuous_partial_acceptance_scales_evenly():
    manager = _manager_for_assets({
        "ECM63.1": {
            "type": "ev_charger",
            "modulation_type": "continuous",
            "capacity_kw": 7.0,
        },
        "ECM63.2": {
            "type": "ev_charger",
            "modulation_type": "continuous",
            "capacity_kw": 7.0,
        },
    })

    allocations, selection = manager._build_persistence_activation_allocations(
        {"ECM63.1": 3.0, "ECM63.2": 3.0},
        accepted_kw=3.0,
    )

    assert allocations == pytest.approx({"ECM63.1": 1.5, "ECM63.2": 1.5})
    assert sum(allocations.values()) == pytest.approx(3.0)
    assert selection["continuous_activation_total"] == pytest.approx(3.0)


def test_persistence_mixed_partial_acceptance_selects_discrete_then_scales_continuous():
    manager = _manager_for_assets({
        "HP1": {
            "type": "heat_pump",
            "modulation_type": "discrete",
            "capacity_kw": 4.0,
        },
        "HP2": {
            "type": "heat_pump",
            "modulation_type": "discrete",
            "capacity_kw": 10.0,
        },
        "EV1": {
            "type": "ev_charger",
            "modulation_type": "continuous",
            "capacity_kw": 7.0,
        },
        "EV2": {
            "type": "ev_charger",
            "modulation_type": "continuous",
            "capacity_kw": 7.0,
        },
    })

    allocations, selection = manager._build_persistence_activation_allocations(
        {"HP1": 4.0, "HP2": 10.0, "EV1": 3.0, "EV2": 3.0},
        accepted_kw=6.0,
    )

    assert allocations == pytest.approx({"HP1": 4.0, "EV1": 1.0, "EV2": 1.0})
    assert sum(allocations.values()) == pytest.approx(6.0)
    details = _detail_by_asset(selection)
    assert details["HP1"]["selected"] is True
    assert details["HP2"]["selected"] is False
    assert selection["selected_discrete_total"] == pytest.approx(4.0)
    assert selection["continuous_activation_total"] == pytest.approx(2.0)


def test_discrete_force_off_overrides_legacy_threshold_without_rabbitmq():
    controller = fm.AssetController(
        {
            "HP1": {
                "type": "heat_pump",
                "description": "test heat pump",
                "capacity_kw": 10.0,
                "modulation_type": "discrete",
                "discrete_states_kw": [0.0, 10.0],
            }
        },
        _logger(),
        rabbitmq_publisher=None,
    )

    forced = controller.curtail_asset(
        "HP1",
        curtailment_kw=1.0,
        dry_run=True,
        force_discrete_off=True,
    )
    legacy = controller.curtail_asset(
        "HP1",
        curtailment_kw=1.0,
        dry_run=True,
        force_discrete_off=False,
    )

    assert forced["discrete_state"] == "OFF"
    assert forced["target_power_kw"] == pytest.approx(0.0)
    assert "rabbitmq_queued" not in forced
    assert legacy["discrete_state"] == "ON"
    assert legacy["target_power_kw"] == pytest.approx(10.0)


class FakeNodesInterface:
    cfg = {"mainEndpoint": "https://offline.invalid/"}

    def get_request(self, endpoint):
        raise AssertionError("NODES API must not be called by these offline tests")


class FakeMarketHandler:
    def __init__(self, trades):
        self.trades = trades
        self.accepted_trade_calls = 0
        self.settlement_calls = 0

    def get_accepted_trades_for_slot(self, organization_id, slot_start, slot_end):
        self.accepted_trade_calls += 1
        return list(self.trades)

    def get_settlements_for_slot(self, organization_id, slot_start, slot_end):
        self.settlement_calls += 1
        return []


class FakeBidHandler:
    def __init__(self, ledger_rows=None):
        self.ledger_rows = ledger_rows or []
        self.ledger_calls = 0
        self.bid_record = {
            "id": "bid-1",
            "total_quantity_mw": 0.001,
            "assets_to_activate": [{"asset_id": "EV1"}],
            "strategy": None,
        }

    def get_bid_record(self, fsp_id, slot_start):
        return self.bid_record

    def get_strategy_info(self, bid_record):
        return bid_record.get("strategy")

    def get_allowed_assets(self, bid_record):
        return [asset["asset_id"] for asset in bid_record.get("assets_to_activate", [])]

    def get_total_quantity(self, bid_record):
        return float(bid_record.get("total_quantity_mw") or 0.0)

    def get_bid_asset_flexibilities(self, bid_record):
        return {
            asset["asset_id"]: float(asset.get("available_flexibility_kw") or 0.0)
            for asset in bid_record.get("assets_to_activate", [])
            if float(asset.get("available_flexibility_kw") or 0.0) > 0
        }

    def get_trades_from_ledger(self, player_id, slot_start):
        self.ledger_calls += 1
        return list(self.ledger_rows)


def _new_run_manager(tmp_path, market_trades=None, ledger_rows=None):
    config = {
        "fm": {
            "community": "test",
            "actors": {
                "fsps": {
                    "fsp1": {
                        "name": "FSP1",
                        "assets": ["EV1"],
                    }
                }
            },
        },
        "asset_mapping": {
            "EV1": {
                "type": "ev_charger",
                "description": "test charger",
                "capacity_kw": 7.0,
                "modulation_type": "continuous",
                "flexibility_factor": 1.0,
            }
        },
        "bidding_strategies": {},
        "autonomous": {"enabled": False},
    }
    manager = fm.FlexibilityManager(
        config=config,
        fsp_id="fsp1",
        nodes_interface=FakeNodesInterface(),
        bid_repo=None,
        logger=_logger(),
        nodes_authenticated=False,
        rabbitmq_publisher=None,
        demand_repo=None,
        state_file=str(tmp_path / "controlled_state.json"),
    )
    manager.organization_id = "org-1"
    manager.market_handler = FakeMarketHandler(market_trades or [])
    manager.bid_handler = FakeBidHandler(ledger_rows or [])
    return manager


def test_market_result_source_priority_is_offline_and_respects_fallback_flag(tmp_path):
    slot = "2026-05-14T13:15:00Z"

    simulated = _new_run_manager(tmp_path / "simulated")
    simulated_summary = simulated.run(
        slot_override=slot,
        dry_run=True,
        simulate_sold_mw=0.0,
        allow_market_ledger_fallback=True,
    )
    assert simulated_summary["market_result_source"] == "simulate_sold_mw"
    assert simulated.market_handler.accepted_trade_calls == 0
    assert simulated.bid_handler.ledger_calls == 0

    no_fallback = _new_run_manager(tmp_path / "no_fallback")
    no_fallback_summary = no_fallback.run(
        slot_override=slot,
        dry_run=True,
        allow_market_ledger_fallback=False,
    )
    assert no_fallback.market_handler.accepted_trade_calls == 1
    assert no_fallback.bid_handler.ledger_calls == 0
    assert no_fallback_summary["market_result_source"] == "none"
    assert no_fallback_summary["status"] == "no_trades"

    fallback = _new_run_manager(
        tmp_path / "fallback",
        ledger_rows=[{
            "id": "ledger-1",
            "timeslot": slot,
            "player_id": "FSP1",
            "side": "Sell",
            "regulation": "up",
            "quantity": 0.001,
            "price": 1.0,
            "bid_record_id": "bid-1",
        }],
    )
    fallback_summary = fallback.run(
        slot_override=slot,
        dry_run=True,
        allow_market_ledger_fallback=True,
    )
    assert fallback.market_handler.accepted_trade_calls == 1
    assert fallback.bid_handler.ledger_calls == 1
    assert fallback_summary["market_result_source"] == "market_ledger_fallback"
    assert fallback_summary["status"] == "activation_failed"
    assert fallback_summary["total_flexibility_deliverable_kw"] == pytest.approx(0.0)


def test_nodes_interface_does_not_log_raw_access_token_value():
    source = (REPO_ROOT / "classes" / "nodes_interface.py").read_text()

    assert "Access Token: %s" not in source
    assert 'logger.info("Access Token:' not in source
    assert "logger.info(\"Access Token:" not in source
    assert 'logger.info("Access token acquired successfully")' in source
    assert 'logger.info("Access Token: %s" % self.token_data["access_token"])' not in source
    assert "logger.info('Access Token: %s' % self.token_data[\"access_token\"])" not in source
    assert not re.search(
        r"logger\.\w+\([^)]*self\.token_data\[[\"']access_token[\"']\]",
        source,
    )


# =============================================================================
# PERSISTENCE ACTIVATION SAFETY TESTS
# =============================================================================

_PERSISTENCE_STRATEGY_ID = "strategy_8"


def _persistence_strategy_config():
    return {
        "name": "Persistence HP Strategy",
        "description": "test persistence",
        "asset_types": ["heat_pump"],
        "assets_filter": ["ECM96.2", "ECM97.3"],
        "flexibility_method": "persistence",
        "time_slots": [
            {
                "name": "All day",
                "start": "00:00",
                "end": "23:59",
                "flexibility_mw": 0.013,
                "bid_price": 9.0,
                "activation_cost": 2.5,
            }
        ],
    }


def _new_persistence_manager(
    tmp_path,
    asset_mapping,
    bid_record_assets,
    market_trades=None,
    simulate_sold_mw=None,
):
    """Create a FlexibilityManager configured for persistence strategy tests."""
    config = {
        "fm": {
            "community": "test",
            "actors": {
                "fsps": {
                    "fsp1": {
                        "name": "FSP1",
                        "assets": list(asset_mapping.keys()),
                    }
                }
            },
        },
        "asset_mapping": asset_mapping,
        "bidding_strategies": {
            _PERSISTENCE_STRATEGY_ID: _persistence_strategy_config(),
        },
        "autonomous": {"enabled": False},
    }
    manager = fm.FlexibilityManager(
        config=config,
        fsp_id="fsp1",
        nodes_interface=FakeNodesInterface(),
        bid_repo=None,
        logger=_logger(),
        nodes_authenticated=False,
        rabbitmq_publisher=None,
        demand_repo=None,
        state_file=str(tmp_path / "controlled_state.json"),
    )
    manager.organization_id = "org-1"
    manager.market_handler = FakeMarketHandler(market_trades or [])

    bid_handler = FakeBidHandler()
    bid_handler.bid_record = {
        "id": "bid-persistence-1",
        "total_quantity_mw": 0.013,
        "assets_to_activate": bid_record_assets,
        "strategy": {
            "id": _PERSISTENCE_STRATEGY_ID,
            "name": "Persistence HP Strategy",
            "description": "test persistence",
        },
    }
    manager.bid_handler = bid_handler
    return manager


def test_persistence_missing_asset_mapping_fails_closed(tmp_path):
    """If bid_record_assets references an asset not in asset_mapping, fail closed."""
    asset_mapping = {
        "ECM96.2": {
            "type": "heat_pump",
            "modulation_type": "discrete",
            "capacity_kw": 1.141,
            "description": "HP small",
        },
        # ECM97.3 deliberately missing
    }
    bid_record_assets = [
        {"asset_id": "ECM96.2", "available_flexibility_kw": 1.141},
        {"asset_id": "ECM97.3", "available_flexibility_kw": 11.793},
    ]

    manager = _new_persistence_manager(
        tmp_path, asset_mapping, bid_record_assets, simulate_sold_mw=0.013
    )

    summary = manager.run(
        slot_override="2026-05-14T12:00:00Z",
        dry_run=True,
        simulate_sold_mw=0.013,
    )

    assert summary["status"] == "persistence_asset_mapping_incomplete"
    assert "ECM97.3" in summary.get("missing_assets", [])
    assert summary.get("allocations") == {}
    assert summary.get("control_results", {}) == {}


def test_persistence_all_zero_bid_assets_fails_closed_no_fallback(tmp_path):
    """If all bid_record_assets have zero kW, fail closed without fallback."""
    asset_mapping = {
        "ECM96.2": {
            "type": "heat_pump",
            "modulation_type": "discrete",
            "capacity_kw": 1.141,
            "description": "HP small",
        },
        "ECM97.3": {
            "type": "heat_pump",
            "modulation_type": "discrete",
            "capacity_kw": 11.793,
            "description": "HP large",
        },
    }
    bid_record_assets = [
        {"asset_id": "ECM96.2", "available_flexibility_kw": 0.0},
        {"asset_id": "ECM97.3", "available_flexibility_kw": 0.0},
    ]

    manager = _new_persistence_manager(
        tmp_path, asset_mapping, bid_record_assets, simulate_sold_mw=0.013
    )

    summary = manager.run(
        slot_override="2026-05-14T12:00:00Z",
        dry_run=True,
        simulate_sold_mw=0.013,
    )

    assert summary["status"] == "persistence_no_positive_bid_assets"
    assert summary.get("allocations", {}) == {}
    assert summary.get("control_results", {}) == {}


def test_persistence_uses_only_bid_record_assets_not_strategy_allowed(tmp_path):
    """Persistence path activates only assets with positive bid_record_assets kW,
    even if the strategy's allowed_assets includes more."""
    asset_mapping = {
        "ECM96.2": {
            "type": "heat_pump",
            "modulation_type": "discrete",
            "capacity_kw": 1.141,
            "description": "HP small",
        },
        "ECM97.3": {
            "type": "heat_pump",
            "modulation_type": "discrete",
            "capacity_kw": 11.793,
            "description": "HP large",
        },
    }
    # Only ECM96.2 has positive flexibility; ECM97.3 stored as 0
    bid_record_assets = [
        {"asset_id": "ECM96.2", "available_flexibility_kw": 1.141},
        {"asset_id": "ECM97.3", "available_flexibility_kw": 0.0},
    ]

    manager = _new_persistence_manager(
        tmp_path, asset_mapping, bid_record_assets, simulate_sold_mw=0.002
    )

    summary = manager.run(
        slot_override="2026-05-14T12:00:00Z",
        dry_run=True,
        simulate_sold_mw=0.002,
    )

    assert summary["status"] == "success"
    allocations = summary.get("allocations", {})
    assert "ECM96.2" in allocations
    assert "ECM97.3" not in allocations
    assert summary.get("allocation_source") == "bid_record_assets.available_flexibility_kw"


# =============================================================================
# STRATEGY 10 RECENT-PROFILE ACTIVATION TARGET TESTS
# =============================================================================

_RECENT_PROFILE_STRATEGY_ID = "strategy_10"


def _recent_profile_strategy_config():
    return {
        "name": "Recent Profile EV Strategy",
        "description": "test recent profile",
        "asset_types": ["ev_charger"],
        "assets_filter": ["ECM63.2"],
        "flexibility_method": "recent_profile",
        "time_slots": [
            {
                "name": "All day",
                "start": "00:00",
                "end": "23:59",
                "flexibility_mw": 0.002,
                "bid_price": 9.0,
                "activation_cost": 2.5,
            }
        ],
    }


def _new_recent_profile_manager(tmp_path, asset_mapping, bid_record_assets):
    config = {
        "fm": {
            "community": "test",
            "actors": {
                "fsps": {
                    "fsp1": {
                        "name": "FSP1",
                        "assets": list(asset_mapping.keys()),
                    }
                }
            },
        },
        "asset_mapping": asset_mapping,
        "bidding_strategies": {
            _RECENT_PROFILE_STRATEGY_ID: _recent_profile_strategy_config(),
        },
        "autonomous": {"enabled": False},
    }
    manager = fm.FlexibilityManager(
        config=config,
        fsp_id="fsp1",
        nodes_interface=FakeNodesInterface(),
        bid_repo=None,
        logger=_logger(),
        nodes_authenticated=False,
        rabbitmq_publisher=None,
        demand_repo=None,
        state_file=str(tmp_path / "controlled_state.json"),
    )
    manager.organization_id = "org-1"
    manager.market_handler = FakeMarketHandler([])

    bid_handler = FakeBidHandler()
    bid_handler.bid_record = {
        "id": "bid-recent-profile-1",
        "total_quantity_mw": 0.00217,
        "assets_to_activate": bid_record_assets,
        "strategy": {
            "id": _RECENT_PROFILE_STRATEGY_ID,
            "name": "Recent Profile EV Strategy",
            "description": "test recent profile",
        },
    }
    manager.bid_handler = bid_handler
    return manager


def _continuous_controller(min_power_kw=0.0):
    return fm.AssetController(
        {
            "ECM63.2": {
                "type": "ev_charger",
                "description": "continuous charger",
                "capacity_kw": 11.0,
                "min_power_kw": min_power_kw,
                "modulation_type": "continuous",
            }
        },
        _logger(),
        rabbitmq_publisher=None,
    )


def test_strategy_10_continuous_asset_uses_bid_record_reference_power(tmp_path):
    manager = _new_recent_profile_manager(
        tmp_path,
        {
            "ECM63.2": {
                "type": "ev_charger",
                "description": "EV 2",
                "capacity_kw": 11.0,
                "min_power_kw": 0.0,
                "modulation_type": "continuous",
            }
        },
        [
            {
                "asset_id": "ECM63.2",
                "description": "EV 2",
                "asset_type": "ev_charger",
                "available_flexibility_kw": 2.99,
                "reference_power_kw": 5.98,
                "reference_power_source": "recent_profile_baseline",
                "modulation_type": "continuous",
            }
        ],
    )

    summary = manager.run(
        slot_override="2026-05-14T12:00:00Z",
        dry_run=True,
        simulate_sold_mw=0.00299,
    )

    result = summary["control_results"]["ECM63.2"]
    assert summary["status"] == "success"
    assert summary["allocations"]["ECM63.2"] == pytest.approx(2.99)
    assert result["target_power_kw"] == pytest.approx(2.99)
    assert result["computed_target_power_kw"] == pytest.approx(2.99)
    assert result["reference_power_kw"] == pytest.approx(5.98)
    assert result["reference_power_source"] == "recent_profile_baseline"
    assert result["target_power_kw"] != pytest.approx(8.01)


def test_continuous_missing_reference_skips_nominal_power_fallback(caplog):
    controller = _continuous_controller()

    with caplog.at_level(logging.ERROR, logger="test_flexi_manager_activation"):
        result = controller.curtail_asset("ECM63.2", curtailment_kw=2.99, dry_run=True)

    assert result["status"] == "skipped"
    assert result["target_power_kw"] is None
    assert result["computed_target_power_kw"] is None
    assert result["actual_curtailment_kw"] == pytest.approx(0.0)
    assert controller._pending_commands == []
    assert result["target_power_kw"] != pytest.approx(8.01)
    assert "nominal fallback disabled" in caplog.text


def test_strategy_10_missing_reference_fails_closed_without_command(tmp_path, caplog):
    manager = _new_recent_profile_manager(
        tmp_path,
        {
            "ECM63.2": {
                "type": "ev_charger",
                "description": "EV 2",
                "capacity_kw": 11.0,
                "min_power_kw": 0.0,
                "modulation_type": "continuous",
            }
        },
        [
            {
                "asset_id": "ECM63.2",
                "description": "EV 2",
                "asset_type": "ev_charger",
                "available_flexibility_kw": 2.99,
                "modulation_type": "continuous",
            }
        ],
    )

    with caplog.at_level(logging.ERROR, logger="test_flexi_manager_activation"):
        summary = manager.run(
            slot_override="2026-05-14T12:00:00Z",
            dry_run=True,
            simulate_sold_mw=0.00299,
        )

    result = summary["control_results"]["ECM63.2"]
    assert summary["status"] == "activation_failed"
    assert summary["total_flexibility_deliverable_kw"] == pytest.approx(0.0)
    assert result["status"] == "skipped"
    assert result["target_power_kw"] is None
    assert result["target_power_kw"] != pytest.approx(8.01)
    assert manager.controller._pending_commands == []
    assert "nominal fallback disabled" in caplog.text


@pytest.mark.parametrize("reference_power_kw", [None, math.nan, 0.0, -1.0])
def test_continuous_invalid_reference_power_skips_activation(reference_power_kw):
    controller = _continuous_controller()

    result = controller.curtail_asset(
        "ECM63.2",
        curtailment_kw=2.99,
        dry_run=True,
        reference_power_kw=reference_power_kw,
        reference_power_source="recent_profile_baseline",
    )

    assert result["status"] == "skipped"
    assert result["target_power_kw"] is None
    assert result["computed_target_power_kw"] is None
    assert controller._pending_commands == []


def test_continuous_target_respects_lower_bound():
    controller = _continuous_controller(min_power_kw=0.0)

    result = controller.curtail_asset(
        "ECM63.2",
        curtailment_kw=2.0,
        dry_run=True,
        reference_power_kw=1.0,
        reference_power_source="recent_profile_baseline",
    )

    assert result["target_power_kw"] == pytest.approx(0.0)


def test_continuous_target_respects_upper_bound():
    controller = _continuous_controller()

    result = controller.curtail_asset(
        "ECM63.2",
        curtailment_kw=1.0,
        dry_run=True,
        reference_power_kw=14.0,
        reference_power_source="recent_profile_baseline",
    )

    assert result["target_power_kw"] == pytest.approx(11.0)
    assert result["uncapped_target_power_kw"] == pytest.approx(13.0)


def test_discrete_asset_behavior_unchanged_by_reference_power():
    controller = fm.AssetController(
        {
            "HP1": {
                "type": "heat_pump",
                "description": "test heat pump",
                "capacity_kw": 10.0,
                "modulation_type": "discrete",
                "discrete_states_kw": [0.0, 10.0],
            }
        },
        _logger(),
        rabbitmq_publisher=None,
    )

    result = controller.curtail_asset(
        "HP1",
        curtailment_kw=1.0,
        dry_run=True,
        force_discrete_off=False,
        reference_power_kw=4.0,
        reference_power_source="recent_profile_baseline",
    )

    assert result["discrete_state"] == "ON"
    assert result["target_power_kw"] == pytest.approx(10.0)
    assert "reference_power_kw" not in result


def test_continuous_target_calculation_logging_mentions_reference_and_target(caplog):
    controller = _continuous_controller()

    with caplog.at_level(logging.INFO, logger="test_flexi_manager_activation"):
        controller.curtail_asset(
            "ECM63.2",
            curtailment_kw=2.0,
            dry_run=True,
            reference_power_kw=4.34052,
            reference_power_source="recent_profile_baseline",
        )

    assert "Continuous target calculation for ECM63.2" in caplog.text
    assert "reference_power_source=recent_profile_baseline" in caplog.text
    assert "reference_power=4.34052 kW" in caplog.text
    assert "allocated_curtailment=2.00000 kW" in caplog.text
    assert "target_power=2.34052 kW" in caplog.text
