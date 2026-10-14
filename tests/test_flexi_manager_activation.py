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
    import pandas as pd

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
    manager.activation_measurement_provider = FakeMeasurementProvider({
        "ECM63.2": {
            "valid": True,
            "timestamp_utc": pd.Timestamp("2026-05-14T11:55:00Z"),
            "power_w": 5980.0,
            "age_minutes": 5.0,
        }
    })

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


# =============================================================================
# ACTIVATION-CURRENT REFERENCE TESTS (strategy_10 recent-profile EV safety fix)
# =============================================================================


class FakeMeasurementProvider:
    """Injectable fake for ActivationMeasurementProvider in tests."""

    def __init__(self, measurements: dict = None):
        self.measurements = measurements or {}

    def get_latest_power(self, asset_id, current_time_utc, max_age_minutes):
        if asset_id in self.measurements:
            return self.measurements[asset_id]
        return {"valid": False, "reason": "no_data_in_window"}


def _controller_with_activation_current(min_power_kw=0.0, capacity_kw=11.0):
    return fm.AssetController(
        {
            "ECM63.1": {
                "type": "ev_charger",
                "description": "EV charger 1",
                "capacity_kw": capacity_kw,
                "min_power_kw": min_power_kw,
                "modulation_type": "continuous",
            }
        },
        _logger(),
        rabbitmq_publisher=None,
    )


def test_activation_current_higher_than_bid_reference_uses_current():
    """Test A: Reported bug - current higher than bid reference."""
    controller = _controller_with_activation_current()

    result = controller.curtail_asset(
        "ECM63.1",
        curtailment_kw=1.71,
        dry_run=True,
        reference_power_kw=3.59,
        reference_power_source="recent_profile_baseline",
        activation_current_power_kw=11.0,
    )

    assert result["status"] != "skipped"
    assert result["computed_target_power_kw"] == pytest.approx(9.29, abs=0.01)
    assert result["target_power_kw"] == pytest.approx(9.29, abs=0.01)
    assert result["selected_activation_reference_kw"] == pytest.approx(11.0)
    assert result["activation_current_power_kw"] == pytest.approx(11.0)
    assert result["reference_power_kw"] == pytest.approx(3.59)
    assert result["target_power_kw"] != pytest.approx(1.88, abs=0.1)


def test_activation_current_approximately_equals_bid_reference():
    """Test B: Current approximately equals reference."""
    controller = _controller_with_activation_current()

    result = controller.curtail_asset(
        "ECM63.1",
        curtailment_kw=1.71,
        dry_run=True,
        reference_power_kw=3.59,
        reference_power_source="recent_profile_baseline",
        activation_current_power_kw=3.60,
    )

    assert result["status"] != "skipped"
    assert result["computed_target_power_kw"] == pytest.approx(1.89, abs=0.01)
    assert result["target_power_kw"] == pytest.approx(1.89, abs=0.01)
    assert result["selected_activation_reference_kw"] == pytest.approx(3.60)


def test_missing_current_measurement_skips_activation(tmp_path):
    """Test C: Missing/stale current measurement causes skip (fail-closed)."""
    import pandas as pd

    config = {
        "fm": {
            "community": "test",
            "actors": {"fsps": {"fsp1": {"name": "FSP1", "assets": ["ECM63.1"]}}},
        },
        "asset_mapping": {
            "ECM63.1": {
                "type": "ev_charger",
                "description": "EV charger 1",
                "capacity_kw": 11.0,
                "min_power_kw": 0.0,
                "modulation_type": "continuous",
                "device_name_tag": "ev1",
                "pod": "ECM63",
            }
        },
        "bidding_strategies": {
            "strategy_10": _recent_profile_strategy_config(),
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
        state_file=str(tmp_path / "state.json"),
    )
    manager.organization_id = "org-1"
    manager.market_handler = FakeMarketHandler([])
    manager.activation_measurement_provider = FakeMeasurementProvider({})

    bid_handler = FakeBidHandler()
    bid_handler.bid_record = {
        "id": "bid-rp-1",
        "total_quantity_mw": 0.00171,
        "assets_to_activate": [
            {
                "asset_id": "ECM63.1",
                "available_flexibility_kw": 1.71,
                "reference_power_kw": 3.59,
                "reference_power_source": "recent_profile_baseline",
                "modulation_type": "continuous",
            }
        ],
        "strategy": {"id": "strategy_10", "name": "Recent Profile EV Strategy"},
    }
    manager.bid_handler = bid_handler

    summary = manager.run(
        slot_override="2026-05-14T12:00:00Z",
        dry_run=True,
        simulate_sold_mw=0.00171,
    )

    result = summary["control_results"]["ECM63.1"]
    assert result["status"] == "skipped"
    assert result["target_power_kw"] is None
    assert "no valid current measurement" in result.get("skip_reason", "")
    assert manager.controller._pending_commands == []


def test_current_below_active_threshold_skips_activation(tmp_path):
    """Test D: Current below active threshold causes skip."""
    import pandas as pd

    config = {
        "fm": {
            "community": "test",
            "actors": {"fsps": {"fsp1": {"name": "FSP1", "assets": ["ECM63.1"]}}},
        },
        "asset_mapping": {
            "ECM63.1": {
                "type": "ev_charger",
                "description": "EV charger 1",
                "capacity_kw": 11.0,
                "min_power_kw": 0.0,
                "modulation_type": "continuous",
                "device_name_tag": "ev1",
                "pod": "ECM63",
            }
        },
        "bidding_strategies": {
            "strategy_10": _recent_profile_strategy_config(),
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
        state_file=str(tmp_path / "state.json"),
    )
    manager.organization_id = "org-1"
    manager.market_handler = FakeMarketHandler([])
    manager.activation_measurement_provider = FakeMeasurementProvider({
        "ECM63.1": {
            "valid": True,
            "timestamp_utc": pd.Timestamp("2026-05-14T11:55:00Z"),
            "power_w": 2000.0,
            "age_minutes": 5.0,
        }
    })

    bid_handler = FakeBidHandler()
    bid_handler.bid_record = {
        "id": "bid-rp-1",
        "total_quantity_mw": 0.00171,
        "assets_to_activate": [
            {
                "asset_id": "ECM63.1",
                "available_flexibility_kw": 1.71,
                "reference_power_kw": 3.59,
                "reference_power_source": "recent_profile_baseline",
                "modulation_type": "continuous",
            }
        ],
        "strategy": {"id": "strategy_10", "name": "Recent Profile EV Strategy"},
    }
    manager.bid_handler = bid_handler

    summary = manager.run(
        slot_override="2026-05-14T12:00:00Z",
        dry_run=True,
        simulate_sold_mw=0.00171,
    )

    result = summary["control_results"]["ECM63.1"]
    assert result["status"] == "skipped"
    assert result["target_power_kw"] is None
    assert "active threshold" in result.get("skip_reason", "") or "inactive" in result.get("skip_reason", "")


def test_current_below_allocated_curtailment_skips_activation(tmp_path):
    """Test E: Current lower than allocated curtailment causes skip."""
    import pandas as pd

    config = {
        "fm": {
            "community": "test",
            "actors": {"fsps": {"fsp1": {"name": "FSP1", "assets": ["ECM63.1"]}}},
        },
        "asset_mapping": {
            "ECM63.1": {
                "type": "ev_charger",
                "description": "EV charger 1",
                "capacity_kw": 11.0,
                "min_power_kw": 0.0,
                "modulation_type": "continuous",
                "device_name_tag": "ev1",
                "pod": "ECM63",
            }
        },
        "bidding_strategies": {
            "strategy_10": _recent_profile_strategy_config(),
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
        state_file=str(tmp_path / "state.json"),
    )
    manager.organization_id = "org-1"
    manager.market_handler = FakeMarketHandler([])
    manager.activation_measurement_provider = FakeMeasurementProvider({
        "ECM63.1": {
            "valid": True,
            "timestamp_utc": pd.Timestamp("2026-05-14T11:55:00Z"),
            "power_w": 5000.0,
            "age_minutes": 5.0,
        }
    })

    bid_handler = FakeBidHandler()
    bid_handler.bid_record = {
        "id": "bid-rp-1",
        "total_quantity_mw": 0.006,
        "assets_to_activate": [
            {
                "asset_id": "ECM63.1",
                "available_flexibility_kw": 6.0,
                "reference_power_kw": 3.59,
                "reference_power_source": "recent_profile_baseline",
                "modulation_type": "continuous",
            }
        ],
        "strategy": {"id": "strategy_10", "name": "Recent Profile EV Strategy"},
    }
    manager.bid_handler = bid_handler

    summary = manager.run(
        slot_override="2026-05-14T12:00:00Z",
        dry_run=True,
        simulate_sold_mw=0.006,
    )

    result = summary["control_results"]["ECM63.1"]
    assert result["status"] == "skipped"
    assert result["target_power_kw"] is None
    assert "below allocated curtailment" in result.get("skip_reason", "")


def test_strategy9_persistence_ev_unchanged_by_activation_current(tmp_path):
    """Test F: Strategy 9 / non-recent-profile EV uses bid reference, not current."""
    import pandas as pd

    config = {
        "fm": {
            "community": "test",
            "actors": {"fsps": {"fsp1": {"name": "FSP1", "assets": ["ECM63.1"]}}},
        },
        "asset_mapping": {
            "ECM63.1": {
                "type": "ev_charger",
                "description": "EV charger 1",
                "capacity_kw": 11.0,
                "min_power_kw": 0.0,
                "modulation_type": "continuous",
                "device_name_tag": "ev1",
                "pod": "ECM63",
            }
        },
        "bidding_strategies": {
            "strategy_9": {
                "name": "Persistence EV Strategy",
                "description": "test persistence EV",
                "asset_types": ["ev_charger"],
                "assets_filter": ["ECM63.1"],
                "flexibility_method": "persistence",
                "time_slots": [{
                    "name": "All day",
                    "start": "00:00",
                    "end": "23:59",
                    "flexibility_mw": 0.002,
                    "bid_price": 9.0,
                    "activation_cost": 2.5,
                }],
            },
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
        state_file=str(tmp_path / "state.json"),
    )
    manager.organization_id = "org-1"
    manager.market_handler = FakeMarketHandler([])
    manager.activation_measurement_provider = FakeMeasurementProvider({
        "ECM63.1": {
            "valid": True,
            "timestamp_utc": pd.Timestamp("2026-05-14T11:55:00Z"),
            "power_w": 11000.0,
            "age_minutes": 5.0,
        }
    })

    bid_handler = FakeBidHandler()
    bid_handler.bid_record = {
        "id": "bid-persistence-1",
        "total_quantity_mw": 0.00171,
        "assets_to_activate": [
            {
                "asset_id": "ECM63.1",
                "available_flexibility_kw": 1.71,
                "reference_power_kw": 3.59,
                "reference_power_source": "persistence_baseline",
                "modulation_type": "continuous",
            }
        ],
        "strategy": {"id": "strategy_9", "name": "Persistence EV Strategy"},
    }
    manager.bid_handler = bid_handler

    summary = manager.run(
        slot_override="2026-05-14T12:00:00Z",
        dry_run=True,
        simulate_sold_mw=0.00171,
    )

    result = summary["control_results"]["ECM63.1"]
    assert result["status"] != "skipped"
    assert result["computed_target_power_kw"] == pytest.approx(1.88, abs=0.01)
    assert result["target_power_kw"] == pytest.approx(1.88, abs=0.01)
    assert result.get("activation_current_power_kw") is None
    assert result["reference_power_kw"] == pytest.approx(3.59)


def test_discrete_hp_unchanged_by_activation_current():
    """Test G: Discrete HP force_discrete_off unchanged by activation current."""
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
        force_discrete_off=True,
        reference_power_kw=4.0,
        reference_power_source="recent_profile_baseline",
        activation_current_power_kw=11.0,
    )

    assert result["discrete_state"] == "OFF"
    assert result["target_power_kw"] == pytest.approx(0.0)
    assert "activation_current_power_kw" not in result


def test_activation_current_none_uses_bid_reference_for_non_recent_profile():
    """When activation_current_power_kw is None, uses bid reference unchanged."""
    controller = _controller_with_activation_current()

    result = controller.curtail_asset(
        "ECM63.1",
        curtailment_kw=1.71,
        dry_run=True,
        reference_power_kw=3.59,
        reference_power_source="persistence_baseline",
        activation_current_power_kw=None,
    )

    assert result["status"] != "skipped"
    assert result["computed_target_power_kw"] == pytest.approx(1.88, abs=0.01)
    assert result["selected_activation_reference_kw"] == pytest.approx(3.59)


def test_activation_current_valid_measurement_produces_correct_target(tmp_path):
    """Full integration: valid measurement -> correct target with activation current."""
    import pandas as pd

    config = {
        "fm": {
            "community": "test",
            "actors": {"fsps": {"fsp1": {"name": "FSP1", "assets": ["ECM63.1"]}}},
        },
        "asset_mapping": {
            "ECM63.1": {
                "type": "ev_charger",
                "description": "EV charger 1",
                "capacity_kw": 11.0,
                "min_power_kw": 0.0,
                "modulation_type": "continuous",
                "device_name_tag": "ev1",
                "pod": "ECM63",
            }
        },
        "bidding_strategies": {
            "strategy_10": _recent_profile_strategy_config(),
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
        state_file=str(tmp_path / "state.json"),
    )
    manager.organization_id = "org-1"
    manager.market_handler = FakeMarketHandler([])
    manager.activation_measurement_provider = FakeMeasurementProvider({
        "ECM63.1": {
            "valid": True,
            "timestamp_utc": pd.Timestamp("2026-05-14T11:55:00Z"),
            "power_w": 11000.0,
            "age_minutes": 5.0,
        }
    })

    bid_handler = FakeBidHandler()
    bid_handler.bid_record = {
        "id": "bid-rp-1",
        "total_quantity_mw": 0.00171,
        "assets_to_activate": [
            {
                "asset_id": "ECM63.1",
                "available_flexibility_kw": 1.71,
                "reference_power_kw": 3.59,
                "reference_power_source": "recent_profile_baseline",
                "modulation_type": "continuous",
            }
        ],
        "strategy": {"id": "strategy_10", "name": "Recent Profile EV Strategy"},
    }
    manager.bid_handler = bid_handler

    summary = manager.run(
        slot_override="2026-05-14T12:00:00Z",
        dry_run=True,
        simulate_sold_mw=0.00171,
    )

    result = summary["control_results"]["ECM63.1"]
    assert summary["status"] == "success"
    assert result["status"] != "skipped"
    assert result["computed_target_power_kw"] == pytest.approx(9.29, abs=0.01)
    assert result["target_power_kw"] == pytest.approx(9.29, abs=0.01)
    assert result["selected_activation_reference_kw"] == pytest.approx(11.0)
    assert result["activation_current_power_kw"] == pytest.approx(11.0)
    assert result["reference_power_kw"] == pytest.approx(3.59)
    assert result["target_power_kw"] != pytest.approx(1.88, abs=0.1)


# =============================================================================
# CONSECUTIVE-SLOT EV ACTIVATION CONTINUATION TESTS
# =============================================================================


def _write_previous_state(state_file, state_dict):
    """Write a controlled-state JSON file to simulate a previous run."""
    import json
    with open(state_file, "w") as fh:
        json.dump(state_dict, fh)


def _new_continuation_manager(
    tmp_path,
    previous_state=None,
    measurement_data=None,
    bid_reference_power_kw=10.3645,
    allocated_flexibility_kw=5.0,
    slot_override="2026-06-05T13:00:00Z",
    simulate_sold_mw=0.005,
    strategy_id="strategy_10",
    reference_power_source="recent_profile_baseline",
    asset_id="ECM63.1",
    recent_profile_settings=None,
    persistence_settings=None,
):
    """Create a FlexibilityManager wired for consecutive-slot continuation tests."""
    import pandas as pd

    state_file = str(tmp_path / "state.json")
    if previous_state is not None:
        _write_previous_state(state_file, previous_state)

    strategy_cfg = _recent_profile_strategy_config()
    if recent_profile_settings is not None:
        strategy_cfg["recentProfileSettings"] = recent_profile_settings

    config = {
        "fm": {
            "community": "test",
            "actors": {"fsps": {"fsp1": {"name": "FSP1", "assets": [asset_id]}}},
        },
        "asset_mapping": {
            asset_id: {
                "type": "ev_charger",
                "description": "EV charger 1",
                "capacity_kw": 11.0,
                "min_power_kw": 0.0,
                "modulation_type": "continuous",
                "device_name_tag": "ev1",
                "pod": asset_id.split(".")[0],
            }
        },
        "bidding_strategies": {
            strategy_id: strategy_cfg,
        },
        "autonomous": {"enabled": False},
    }
    if persistence_settings is not None:
        config["flexibility"] = {
            "persistenceSettings": persistence_settings,
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
        state_file=state_file,
    )
    manager.organization_id = "org-1"
    manager.market_handler = FakeMarketHandler([])

    if measurement_data is not None:
        manager.activation_measurement_provider = FakeMeasurementProvider(
            measurement_data
        )
    else:
        manager.activation_measurement_provider = FakeMeasurementProvider({})

    bid_handler = FakeBidHandler()
    bid_handler.bid_record = {
        "id": "bid-cont-1",
        "total_quantity_mw": simulate_sold_mw,
        "assets_to_activate": [
            {
                "asset_id": asset_id,
                "available_flexibility_kw": allocated_flexibility_kw,
                "reference_power_kw": bid_reference_power_kw,
                "reference_power_source": reference_power_source,
                "modulation_type": "continuous",
            }
        ],
        "strategy": {"id": strategy_id, "name": "Recent Profile EV Strategy"},
    }
    manager.bid_handler = bid_handler
    return manager, state_file


def test_consecutive_activation_uses_bid_reference_and_suppresses_restore(tmp_path):
    """Test B: Previously controlled + adjacent slot -> continuation target."""
    import pandas as pd

    previous_state = {
        "ECM63.1": {
            "asset_type": "ev_charger",
            "slot_start": "2026-06-05T12:45:00",
            "slot_end": "2026-06-05T13:00:00",
        }
    }
    measurement = {
        "ECM63.1": {
            "valid": True,
            "timestamp_utc": pd.Timestamp("2026-06-05T12:45:00Z"),
            "power_w": 4158.0,
            "age_minutes": 14.1,
        }
    }
    manager, state_file = _new_continuation_manager(
        tmp_path,
        previous_state=previous_state,
        measurement_data=measurement,
        bid_reference_power_kw=10.3645,
        allocated_flexibility_kw=5.0,
        slot_override="2026-06-05T13:00:00Z",
        simulate_sold_mw=0.005,
    )

    summary = manager.run(
        slot_override="2026-06-05T13:00:00Z",
        dry_run=True,
        simulate_sold_mw=0.005,
    )

    result = summary["control_results"]["ECM63.1"]
    assert result["status"] != "skipped", (
        f"Expected continuation, got skip: {result.get('skip_reason')}"
    )
    assert result["target_power_kw"] == pytest.approx(5.3645, abs=0.01)
    assert result["computed_target_power_kw"] == pytest.approx(5.3645, abs=0.01)
    assert result["selected_activation_reference_kw"] == pytest.approx(10.3645)
    assert result["reference_power_kw"] == pytest.approx(10.3645)

    assert manager.controller._pending_commands == [] or all(
        cmd.get("command_type") != "restore"
        for cmd in manager.controller._pending_commands
    )

    import json
    with open(state_file) as fh:
        saved = json.load(fh)
    assert "ECM63.1" in saved
    assert saved["ECM63.1"]["slot_start"] == "2026-06-05T13:00:00"
    assert saved["ECM63.1"]["slot_end"] == "2026-06-05T13:15:00"


def test_previously_controlled_not_selected_restores(tmp_path):
    """Test C: Previously controlled but different asset selected -> restore."""
    import pandas as pd

    previous_state = {
        "ECM63.1": {
            "asset_type": "ev_charger",
            "slot_start": "2026-06-05T12:45:00",
            "slot_end": "2026-06-05T13:00:00",
        }
    }
    measurement = {
        "ECM63.2": {
            "valid": True,
            "timestamp_utc": pd.Timestamp("2026-06-05T12:55:00Z"),
            "power_w": 11000.0,
            "age_minutes": 5.0,
        }
    }

    state_file = str(tmp_path / "state.json")
    _write_previous_state(state_file, previous_state)

    config = {
        "fm": {
            "community": "test",
            "actors": {"fsps": {"fsp1": {"name": "FSP1", "assets": ["ECM63.1", "ECM63.2"]}}},
        },
        "asset_mapping": {
            "ECM63.1": {
                "type": "ev_charger",
                "description": "EV charger 1",
                "capacity_kw": 11.0,
                "min_power_kw": 0.0,
                "modulation_type": "continuous",
                "device_name_tag": "ev1",
                "pod": "ECM63",
            },
            "ECM63.2": {
                "type": "ev_charger",
                "description": "EV charger 2",
                "capacity_kw": 11.0,
                "min_power_kw": 0.0,
                "modulation_type": "continuous",
                "device_name_tag": "ev2",
                "pod": "ECM63",
            },
        },
        "bidding_strategies": {
            "strategy_10": _recent_profile_strategy_config(),
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
        state_file=state_file,
    )
    manager.organization_id = "org-1"
    manager.market_handler = FakeMarketHandler([])
    manager.activation_measurement_provider = FakeMeasurementProvider(measurement)

    bid_handler = FakeBidHandler()
    bid_handler.bid_record = {
        "id": "bid-cont-2",
        "total_quantity_mw": 0.003,
        "assets_to_activate": [
            {
                "asset_id": "ECM63.2",
                "available_flexibility_kw": 3.0,
                "reference_power_kw": 8.0,
                "reference_power_source": "recent_profile_baseline",
                "modulation_type": "continuous",
            }
        ],
        "strategy": {"id": "strategy_10", "name": "Recent Profile EV Strategy"},
    }
    manager.bid_handler = bid_handler

    summary = manager.run(
        slot_override="2026-06-05T13:00:00Z",
        dry_run=True,
        simulate_sold_mw=0.003,
    )

    import json
    with open(state_file) as fh:
        saved = json.load(fh)
    assert "ECM63.1" not in saved, "ECM63.1 should NOT be in controlled state (was restored)"
    assert "ECM63.2" in saved, "ECM63.2 should be in controlled state"

    result_2 = summary["control_results"]["ECM63.2"]
    assert result_2["status"] != "skipped"


def test_previously_controlled_no_trade_restores(tmp_path):
    """Test D: Previously controlled + no accepted trade -> restore."""
    previous_state = {
        "ECM63.1": {
            "asset_type": "ev_charger",
            "slot_start": "2026-06-05T12:45:00",
            "slot_end": "2026-06-05T13:00:00",
        }
    }
    state_file = str(tmp_path / "state.json")
    _write_previous_state(state_file, previous_state)

    config = {
        "fm": {
            "community": "test",
            "actors": {"fsps": {"fsp1": {"name": "FSP1", "assets": ["ECM63.1"]}}},
        },
        "asset_mapping": {
            "ECM63.1": {
                "type": "ev_charger",
                "description": "EV charger 1",
                "capacity_kw": 11.0,
                "min_power_kw": 0.0,
                "modulation_type": "continuous",
                "device_name_tag": "ev1",
                "pod": "ECM63",
            },
        },
        "bidding_strategies": {
            "strategy_10": _recent_profile_strategy_config(),
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
        state_file=state_file,
    )
    manager.organization_id = "org-1"
    manager.market_handler = FakeMarketHandler([])
    manager.activation_measurement_provider = FakeMeasurementProvider({})

    bid_handler = FakeBidHandler()
    bid_handler.bid_record = {
        "id": "bid-cont-3",
        "total_quantity_mw": 0.005,
        "assets_to_activate": [
            {
                "asset_id": "ECM63.1",
                "available_flexibility_kw": 5.0,
                "reference_power_kw": 10.3645,
                "reference_power_source": "recent_profile_baseline",
                "modulation_type": "continuous",
            }
        ],
        "strategy": {"id": "strategy_10", "name": "Recent Profile EV Strategy"},
    }
    manager.bid_handler = bid_handler

    summary = manager.run(
        slot_override="2026-06-05T13:00:00Z",
        dry_run=True,
    )

    assert summary["status"] in ("no_trades", "no_bid_record")

    import json
    with open(state_file) as fh:
        saved = json.load(fh)
    assert saved == {}, "State should be empty after restore (no trades)"


def test_previously_controlled_gap_between_slots_not_continuation(tmp_path):
    """Test E: Gap between previous slot and current -> not continuation."""
    import pandas as pd

    previous_state = {
        "ECM63.1": {
            "asset_type": "ev_charger",
            "slot_start": "2026-06-05T12:30:00",
            "slot_end": "2026-06-05T12:45:00",
        }
    }
    measurement = {
        "ECM63.1": {
            "valid": True,
            "timestamp_utc": pd.Timestamp("2026-06-05T12:55:00Z"),
            "power_w": 4158.0,
            "age_minutes": 5.0,
        }
    }
    manager, state_file = _new_continuation_manager(
        tmp_path,
        previous_state=previous_state,
        measurement_data=measurement,
        bid_reference_power_kw=10.3645,
        allocated_flexibility_kw=5.0,
        slot_override="2026-06-05T13:00:00Z",
        simulate_sold_mw=0.005,
    )

    summary = manager.run(
        slot_override="2026-06-05T13:00:00Z",
        dry_run=True,
        simulate_sold_mw=0.005,
    )

    result = summary["control_results"]["ECM63.1"]
    assert result["status"] == "skipped"
    assert "below allocated curtailment" in result.get("skip_reason", "")


def test_continuation_current_below_active_threshold_skips(tmp_path):
    """Test F: Continuation + current below active threshold -> skip/restore."""
    import pandas as pd

    previous_state = {
        "ECM63.1": {
            "asset_type": "ev_charger",
            "slot_start": "2026-06-05T12:45:00",
            "slot_end": "2026-06-05T13:00:00",
        }
    }
    measurement = {
        "ECM63.1": {
            "valid": True,
            "timestamp_utc": pd.Timestamp("2026-06-05T12:55:00Z"),
            "power_w": 1000.0,
            "age_minutes": 5.0,
        }
    }
    manager, state_file = _new_continuation_manager(
        tmp_path,
        previous_state=previous_state,
        measurement_data=measurement,
        bid_reference_power_kw=10.3645,
        allocated_flexibility_kw=5.0,
        slot_override="2026-06-05T13:00:00Z",
        simulate_sold_mw=0.005,
    )

    summary = manager.run(
        slot_override="2026-06-05T13:00:00Z",
        dry_run=True,
        simulate_sold_mw=0.005,
    )

    result = summary["control_results"]["ECM63.1"]
    assert result["status"] == "skipped"
    assert "active threshold" in result.get("skip_reason", "") or \
           "inactive" in result.get("skip_reason", "") or \
           "disconnected" in result.get("skip_reason", "")


def test_continuation_missing_measurement_skips(tmp_path):
    """Test G: Continuation + no measurement data -> skip/fail closed."""
    previous_state = {
        "ECM63.1": {
            "asset_type": "ev_charger",
            "slot_start": "2026-06-05T12:45:00",
            "slot_end": "2026-06-05T13:00:00",
        }
    }
    manager, state_file = _new_continuation_manager(
        tmp_path,
        previous_state=previous_state,
        measurement_data={},
        bid_reference_power_kw=10.3645,
        allocated_flexibility_kw=5.0,
        slot_override="2026-06-05T13:00:00Z",
        simulate_sold_mw=0.005,
    )

    summary = manager.run(
        slot_override="2026-06-05T13:00:00Z",
        dry_run=True,
        simulate_sold_mw=0.005,
    )

    result = summary["control_results"]["ECM63.1"]
    assert result["status"] == "skipped"
    assert "no valid current measurement" in result.get("skip_reason", "")


def test_strategy9_unchanged_by_continuation_logic(tmp_path):
    """Test H: Strategy 9 / persistence_baseline is NOT affected by continuation."""
    import pandas as pd

    previous_state = {
        "ECM63.1": {
            "asset_type": "ev_charger",
            "slot_start": "2026-06-05T12:45:00",
            "slot_end": "2026-06-05T13:00:00",
        }
    }
    measurement = {
        "ECM63.1": {
            "valid": True,
            "timestamp_utc": pd.Timestamp("2026-06-05T12:55:00Z"),
            "power_w": 4158.0,
            "age_minutes": 5.0,
        }
    }
    state_file = str(tmp_path / "state.json")
    _write_previous_state(state_file, previous_state)

    config = {
        "fm": {
            "community": "test",
            "actors": {"fsps": {"fsp1": {"name": "FSP1", "assets": ["ECM63.1"]}}},
        },
        "asset_mapping": {
            "ECM63.1": {
                "type": "ev_charger",
                "description": "EV charger 1",
                "capacity_kw": 11.0,
                "min_power_kw": 0.0,
                "modulation_type": "continuous",
                "device_name_tag": "ev1",
                "pod": "ECM63",
            },
        },
        "bidding_strategies": {
            "strategy_9": {
                "name": "Persistence EV Strategy",
                "description": "test persistence EV",
                "asset_types": ["ev_charger"],
                "assets_filter": ["ECM63.1"],
                "flexibility_method": "persistence",
                "time_slots": [{
                    "name": "All day",
                    "start": "00:00",
                    "end": "23:59",
                    "flexibility_mw": 0.002,
                    "bid_price": 9.0,
                    "activation_cost": 2.5,
                }],
            },
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
        state_file=state_file,
    )
    manager.organization_id = "org-1"
    manager.market_handler = FakeMarketHandler([])
    manager.activation_measurement_provider = FakeMeasurementProvider(measurement)

    bid_handler = FakeBidHandler()
    bid_handler.bid_record = {
        "id": "bid-persistence-cont",
        "total_quantity_mw": 0.005,
        "assets_to_activate": [
            {
                "asset_id": "ECM63.1",
                "available_flexibility_kw": 5.0,
                "reference_power_kw": 10.3645,
                "reference_power_source": "persistence_baseline",
                "modulation_type": "continuous",
            }
        ],
        "strategy": {"id": "strategy_9", "name": "Persistence EV Strategy"},
    }
    manager.bid_handler = bid_handler

    summary = manager.run(
        slot_override="2026-06-05T13:00:00Z",
        dry_run=True,
        simulate_sold_mw=0.005,
    )

    result = summary["control_results"]["ECM63.1"]
    assert result["status"] != "skipped"
    assert result["computed_target_power_kw"] == pytest.approx(5.3645, abs=0.01)
    assert result.get("activation_current_power_kw") is None
    assert result["reference_power_kw"] == pytest.approx(10.3645)


def test_discrete_hp_unchanged_by_continuation_logic(tmp_path):
    """Test I: Discrete HP is NOT affected by continuation logic."""
    import pandas as pd

    previous_state = {
        "HP1": {
            "asset_type": "heat_pump",
            "slot_start": "2026-06-05T12:45:00",
            "slot_end": "2026-06-05T13:00:00",
        }
    }
    state_file = str(tmp_path / "state.json")
    _write_previous_state(state_file, previous_state)

    config = {
        "fm": {
            "community": "test",
            "actors": {"fsps": {"fsp1": {"name": "FSP1", "assets": ["HP1"]}}},
        },
        "asset_mapping": {
            "HP1": {
                "type": "heat_pump",
                "description": "test heat pump",
                "capacity_kw": 10.0,
                "modulation_type": "discrete",
                "discrete_states_kw": [0.0, 10.0],
            },
        },
        "bidding_strategies": {
            "strategy_8": {
                "name": "Persistence HP Strategy",
                "description": "test persistence",
                "asset_types": ["heat_pump"],
                "assets_filter": ["HP1"],
                "flexibility_method": "persistence",
                "time_slots": [{
                    "name": "All day",
                    "start": "00:00",
                    "end": "23:59",
                    "flexibility_mw": 0.010,
                    "bid_price": 9.0,
                    "activation_cost": 2.5,
                }],
            },
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
        state_file=state_file,
    )
    manager.organization_id = "org-1"
    manager.market_handler = FakeMarketHandler([])
    manager.activation_measurement_provider = FakeMeasurementProvider({})

    bid_handler = FakeBidHandler()
    bid_handler.bid_record = {
        "id": "bid-hp-cont",
        "total_quantity_mw": 0.010,
        "assets_to_activate": [
            {
                "asset_id": "HP1",
                "available_flexibility_kw": 10.0,
                "reference_power_kw": 10.0,
                "reference_power_source": "persistence_baseline",
                "modulation_type": "discrete",
            }
        ],
        "strategy": {
            "id": "strategy_8",
            "name": "Persistence HP Strategy",
            "description": "test persistence",
        },
    }
    manager.bid_handler = bid_handler

    summary = manager.run(
        slot_override="2026-06-05T13:00:00Z",
        dry_run=True,
        simulate_sold_mw=0.010,
    )

    result = summary["control_results"]["HP1"]
    assert result["status"] != "skipped"
    assert result["discrete_state"] == "OFF"
    assert result["target_power_kw"] == pytest.approx(0.0)


def test_enriched_state_file_has_diagnostic_fields(tmp_path):
    """Verify state file includes target_power_kw, reference, etc."""
    import pandas as pd

    measurement = {
        "ECM63.1": {
            "valid": True,
            "timestamp_utc": pd.Timestamp("2026-06-05T12:55:00Z"),
            "power_w": 11000.0,
            "age_minutes": 5.0,
        }
    }
    manager, state_file = _new_continuation_manager(
        tmp_path,
        previous_state=None,
        measurement_data=measurement,
        bid_reference_power_kw=10.3645,
        allocated_flexibility_kw=5.0,
        slot_override="2026-06-05T13:00:00Z",
        simulate_sold_mw=0.005,
    )

    manager.run(
        slot_override="2026-06-05T13:00:00Z",
        dry_run=True,
        simulate_sold_mw=0.005,
    )

    import json
    with open(state_file) as fh:
        saved = json.load(fh)

    assert "ECM63.1" in saved
    entry = saved["ECM63.1"]
    assert entry["slot_start"] == "2026-06-05T13:00:00"
    assert entry["slot_end"] == "2026-06-05T13:15:00"
    assert entry.get("target_power_kw") is not None
    assert entry.get("allocated_curtailment_kw") is not None
    assert entry.get("reference_power_kw") is not None
    assert entry.get("reference_power_source") == "recent_profile_baseline"
    assert entry.get("strategy_id") == "strategy_10"


# =============================================================================
# EV COMFORT GUARD: MAX CONSECUTIVE SLOTS + COOLDOWN TESTS
# =============================================================================


def _controlled_prev_entry(
    slot_start="2026-06-05T12:45:00",
    slot_end="2026-06-05T13:00:00",
    consecutive=None,
    control_sequence_start=None,
    strategy_id="strategy_10",
):
    """Build a controlled previous-state entry for comfort guard tests."""
    entry = {
        "state": "controlled",
        "asset_type": "ev_charger",
        "slot_start": slot_start,
        "slot_end": slot_end,
        "target_power_kw": 5.36,
        "allocated_curtailment_kw": 5.0,
        "reference_power_kw": 10.3645,
        "reference_power_source": "recent_profile_baseline",
        "strategy_id": strategy_id,
    }
    if consecutive is not None:
        entry["consecutive_activation_slots"] = consecutive
    if control_sequence_start is not None:
        entry["control_sequence_start"] = control_sequence_start
    return entry


def _valid_measurement(power_w=4158.0, age_minutes=10.0):
    import pandas as pd
    return {
        "ECM63.1": {
            "valid": True,
            "timestamp_utc": pd.Timestamp("2026-06-05T12:55:00Z"),
            "power_w": power_w,
            "age_minutes": age_minutes,
        }
    }


def _load_state(state_file):
    import json
    with open(state_file) as fh:
        return json.load(fh)


def test_comfort_A_new_activation_starts_sequence(tmp_path):
    """A: New activation (no previous state) -> controlled, count=1."""
    manager, state_file = _new_continuation_manager(
        tmp_path,
        previous_state=None,
        measurement_data=_valid_measurement(power_w=11000.0),
        bid_reference_power_kw=10.3645,
        allocated_flexibility_kw=5.0,
    )

    summary = manager.run(
        slot_override="2026-06-05T13:00:00Z",
        dry_run=True,
        simulate_sold_mw=0.005,
    )

    result = summary["control_results"]["ECM63.1"]
    assert result["status"] != "skipped"
    saved = _load_state(state_file)
    assert saved["ECM63.1"]["state"] == "controlled"
    assert saved["ECM63.1"]["consecutive_activation_slots"] == 1
    assert saved["ECM63.1"]["control_sequence_start"] == "2026-06-05T13:00:00"


def test_comfort_B_continuation_below_max_increments(tmp_path):
    """B: Continuation below max -> count increments, no restore."""
    previous_state = {
        "ECM63.1": _controlled_prev_entry(
            consecutive=2, control_sequence_start="2026-06-05T12:30:00"
        )
    }
    manager, state_file = _new_continuation_manager(
        tmp_path,
        previous_state=previous_state,
        measurement_data=_valid_measurement(power_w=4158.0),
        bid_reference_power_kw=10.3645,
        allocated_flexibility_kw=5.0,
    )

    summary = manager.run(
        slot_override="2026-06-05T13:00:00Z",
        dry_run=True,
        simulate_sold_mw=0.005,
    )

    result = summary["control_results"]["ECM63.1"]
    assert result["status"] != "skipped"
    assert result["target_power_kw"] == pytest.approx(5.3645, abs=0.01)
    saved = _load_state(state_file)
    assert saved["ECM63.1"]["state"] == "controlled"
    assert saved["ECM63.1"]["consecutive_activation_slots"] == 3
    assert saved["ECM63.1"]["control_sequence_start"] == "2026-06-05T12:30:00"


def test_comfort_C_continuation_reaching_max_allowed(tmp_path):
    """C: Continuation reaching max (3 -> 4) still allowed."""
    previous_state = {
        "ECM63.1": _controlled_prev_entry(
            consecutive=3, control_sequence_start="2026-06-05T12:15:00"
        )
    }
    manager, state_file = _new_continuation_manager(
        tmp_path,
        previous_state=previous_state,
        measurement_data=_valid_measurement(power_w=4158.0),
        bid_reference_power_kw=10.3645,
        allocated_flexibility_kw=5.0,
    )

    summary = manager.run(
        slot_override="2026-06-05T13:00:00Z",
        dry_run=True,
        simulate_sold_mw=0.005,
    )

    result = summary["control_results"]["ECM63.1"]
    assert result["status"] != "skipped"
    saved = _load_state(state_file)
    assert saved["ECM63.1"]["state"] == "controlled"
    assert saved["ECM63.1"]["consecutive_activation_slots"] == 4


def test_comfort_D_beyond_max_blocked_and_enters_cooldown(tmp_path):
    """D: Continuation beyond max (prev=4, max=4) blocked, restored, cooldown."""
    previous_state = {
        "ECM63.1": _controlled_prev_entry(
            consecutive=4, control_sequence_start="2026-06-05T12:00:00"
        )
    }
    manager, state_file = _new_continuation_manager(
        tmp_path,
        previous_state=previous_state,
        measurement_data=_valid_measurement(power_w=4158.0),
        bid_reference_power_kw=10.3645,
        allocated_flexibility_kw=5.0,
    )
    # Truthy controller publisher so restore_asset queues a restore command we
    # can count (manager-level publisher stays None so publish step is skipped).
    manager.controller.rabbitmq_publisher = object()

    summary = manager.run(
        slot_override="2026-06-05T13:00:00Z",
        dry_run=True,
        simulate_sold_mw=0.005,
    )

    result = summary["control_results"]["ECM63.1"]
    assert result["status"] == "skipped"
    assert "maximum consecutive activation slots" in result.get("skip_reason", "")

    restore_cmds = [
        cmd for cmd in manager.controller._pending_commands
        if cmd.get("command_type") == "restore" and cmd.get("asset_id") == "ECM63.1"
    ]
    assert len(restore_cmds) == 1, "ECM63.1 should be restored exactly once"

    curtail_cmds = [
        cmd for cmd in manager.controller._pending_commands
        if cmd.get("command_type") == "curtail" and cmd.get("asset_id") == "ECM63.1"
    ]
    assert curtail_cmds == [], "Blocked asset must not be curtailed"

    saved = _load_state(state_file)
    assert saved["ECM63.1"]["state"] == "cooldown"
    assert saved["ECM63.1"]["cooldown_reason"] == "max_consecutive_activation_slots"
    # cooldown_until = 13:00 + 2 * 15min = 13:30
    assert saved["ECM63.1"]["cooldown_until_slot_start"] == "2026-06-05T13:30:00"
    assert saved["ECM63.1"]["last_consecutive_activation_slots"] == 4


def test_comfort_E_active_cooldown_blocks_without_repeated_restore(tmp_path):
    """E: Active cooldown blocks activation, no curtail, no restore."""
    previous_state = {
        "ECM63.1": {
            "state": "cooldown",
            "asset_type": "ev_charger",
            "cooldown_reason": "max_consecutive_activation_slots",
            "cooldown_started_slot": "2026-06-05T13:00:00",
            "cooldown_until_slot_start": "2026-06-05T13:30:00",
            "cooldown_slots_after_max_activation": 2,
            "last_control_sequence_start": "2026-06-05T12:00:00",
            "last_consecutive_activation_slots": 4,
            "strategy_id": "strategy_10",
        }
    }
    manager, state_file = _new_continuation_manager(
        tmp_path,
        previous_state=previous_state,
        measurement_data=_valid_measurement(power_w=11000.0),
        bid_reference_power_kw=10.3645,
        allocated_flexibility_kw=5.0,
    )
    manager.controller.rabbitmq_publisher = object()

    # Current slot 13:15 is still inside cooldown window (until 13:30).
    summary = manager.run(
        slot_override="2026-06-05T13:15:00Z",
        dry_run=True,
        simulate_sold_mw=0.005,
    )

    result = summary["control_results"]["ECM63.1"]
    assert result["status"] == "skipped"
    assert "cooldown" in result.get("skip_reason", "").lower()

    restore_cmds = [
        cmd for cmd in manager.controller._pending_commands
        if cmd.get("command_type") == "restore"
    ]
    assert restore_cmds == [], "No restore should be sent during active cooldown"
    curtail_cmds = [
        cmd for cmd in manager.controller._pending_commands
        if cmd.get("command_type") == "curtail"
    ]
    assert curtail_cmds == [], "No curtail should be sent during active cooldown"

    saved = _load_state(state_file)
    assert saved["ECM63.1"]["state"] == "cooldown"
    assert saved["ECM63.1"]["cooldown_until_slot_start"] == "2026-06-05T13:30:00"


def test_comfort_F_cooldown_expiry_allows_new_activation(tmp_path):
    """F: Cooldown expired -> new activation, count reset to 1."""
    previous_state = {
        "ECM63.1": {
            "state": "cooldown",
            "asset_type": "ev_charger",
            "cooldown_reason": "max_consecutive_activation_slots",
            "cooldown_started_slot": "2026-06-05T13:00:00",
            "cooldown_until_slot_start": "2026-06-05T13:30:00",
            "cooldown_slots_after_max_activation": 2,
            "last_control_sequence_start": "2026-06-05T12:00:00",
            "last_consecutive_activation_slots": 4,
            "strategy_id": "strategy_10",
        }
    }
    manager, state_file = _new_continuation_manager(
        tmp_path,
        previous_state=previous_state,
        measurement_data=_valid_measurement(power_w=11000.0),
        bid_reference_power_kw=10.3645,
        allocated_flexibility_kw=5.0,
    )

    # Current slot 13:30 == cooldown_until -> eligible again.
    summary = manager.run(
        slot_override="2026-06-05T13:30:00Z",
        dry_run=True,
        simulate_sold_mw=0.005,
    )

    result = summary["control_results"]["ECM63.1"]
    assert result["status"] != "skipped"
    saved = _load_state(state_file)
    assert saved["ECM63.1"]["state"] == "controlled"
    assert saved["ECM63.1"]["consecutive_activation_slots"] == 1
    assert saved["ECM63.1"]["control_sequence_start"] == "2026-06-05T13:30:00"


def test_comfort_G_gap_resets_count(tmp_path):
    """G: Gap (non-adjacent) with high count -> treated as new activation."""
    previous_state = {
        "ECM63.1": _controlled_prev_entry(
            slot_start="2026-06-05T12:15:00",
            slot_end="2026-06-05T12:30:00",  # gap before 13:00
            consecutive=4,
            control_sequence_start="2026-06-05T11:30:00",
        )
    }
    manager, state_file = _new_continuation_manager(
        tmp_path,
        previous_state=previous_state,
        measurement_data=_valid_measurement(power_w=11000.0),
        bid_reference_power_kw=10.3645,
        allocated_flexibility_kw=5.0,
    )

    summary = manager.run(
        slot_override="2026-06-05T13:00:00Z",
        dry_run=True,
        simulate_sold_mw=0.005,
    )

    result = summary["control_results"]["ECM63.1"]
    assert result["status"] != "skipped"
    saved = _load_state(state_file)
    assert saved["ECM63.1"]["state"] == "controlled"
    assert saved["ECM63.1"]["consecutive_activation_slots"] == 1
    assert saved["ECM63.1"]["control_sequence_start"] == "2026-06-05T13:00:00"


def test_comfort_H_missing_count_defaults_safely(tmp_path):
    """H: Adjacent controlled entry with no count -> prev treated as 1, new=2."""
    previous_state = {
        "ECM63.1": _controlled_prev_entry(consecutive=None)
    }
    manager, state_file = _new_continuation_manager(
        tmp_path,
        previous_state=previous_state,
        measurement_data=_valid_measurement(power_w=4158.0),
        bid_reference_power_kw=10.3645,
        allocated_flexibility_kw=5.0,
    )

    summary = manager.run(
        slot_override="2026-06-05T13:00:00Z",
        dry_run=True,
        simulate_sold_mw=0.005,
    )

    result = summary["control_results"]["ECM63.1"]
    assert result["status"] != "skipped"
    saved = _load_state(state_file)
    assert saved["ECM63.1"]["consecutive_activation_slots"] == 2


def test_comfort_I_strategy_recent_profile_settings_override_blocks_and_sets_cooldown(tmp_path):
    """I: strategy recentProfileSettings max=2/cooldown=1 override defaults."""
    previous_state = {
        "ECM63.1": _controlled_prev_entry(
            consecutive=2, control_sequence_start="2026-06-05T12:30:00"
        )
    }
    manager, state_file = _new_continuation_manager(
        tmp_path,
        previous_state=previous_state,
        measurement_data=_valid_measurement(power_w=4158.0),
        bid_reference_power_kw=10.3645,
        allocated_flexibility_kw=5.0,
        recent_profile_settings={
            "maxConsecutiveActivationSlots": 2,
            "cooldownSlotsAfterMaxActivation": 1,
        },
    )

    summary = manager.run(
        slot_override="2026-06-05T13:00:00Z",
        dry_run=True,
        simulate_sold_mw=0.005,
    )

    result = summary["control_results"]["ECM63.1"]
    assert result["status"] == "skipped"
    assert "maximum consecutive activation slots" in result.get("skip_reason", "")
    saved = _load_state(state_file)
    assert saved["ECM63.1"]["state"] == "cooldown"
    assert saved["ECM63.1"]["cooldown_until_slot_start"] == "2026-06-05T13:15:00"


def test_comfort_J_config_override_cooldown1(tmp_path):
    """J: cooldownSlotsAfterMaxActivation=1 -> only current slot blocked."""
    previous_state = {
        "ECM63.1": _controlled_prev_entry(
            consecutive=4, control_sequence_start="2026-06-05T12:00:00"
        )
    }
    manager, state_file = _new_continuation_manager(
        tmp_path,
        previous_state=previous_state,
        measurement_data=_valid_measurement(power_w=4158.0),
        bid_reference_power_kw=10.3645,
        allocated_flexibility_kw=5.0,
        recent_profile_settings={
            "maxConsecutiveActivationSlots": 4,
            "cooldownSlotsAfterMaxActivation": 1,
        },
    )

    summary = manager.run(
        slot_override="2026-06-05T13:00:00Z",
        dry_run=True,
        simulate_sold_mw=0.005,
    )

    saved = _load_state(state_file)
    assert saved["ECM63.1"]["state"] == "cooldown"
    # cooldown_until = 13:00 + 1 * 15min = 13:15 -> slot starting 13:15 eligible
    assert saved["ECM63.1"]["cooldown_until_slot_start"] == "2026-06-05T13:15:00"


def test_comfort_config_defaults_used_when_keys_missing(tmp_path):
    """Missing comfort keys -> defaults max=4, cooldown=2."""
    previous_state = {
        "ECM63.1": _controlled_prev_entry(
            consecutive=4, control_sequence_start="2026-06-05T12:00:00"
        )
    }
    manager, state_file = _new_continuation_manager(
        tmp_path,
        previous_state=previous_state,
        measurement_data=_valid_measurement(power_w=4158.0),
        bid_reference_power_kw=10.3645,
        allocated_flexibility_kw=5.0,
        recent_profile_settings=None,
    )

    summary = manager.run(
        slot_override="2026-06-05T13:00:00Z",
        dry_run=True,
        simulate_sold_mw=0.005,
    )

    result = summary["control_results"]["ECM63.1"]
    assert result["status"] == "skipped"
    assert "maximum consecutive activation slots" in result.get("skip_reason", "")
    saved = _load_state(state_file)
    assert saved["ECM63.1"]["state"] == "cooldown"
    # Default cooldown=2 -> 13:00 + 2 * 15min = 13:30
    assert saved["ECM63.1"]["cooldown_until_slot_start"] == "2026-06-05T13:30:00"


def test_comfort_invalid_strategy_config_values_fall_back_to_defaults(tmp_path):
    """Invalid strategy comfort values -> defaults max=4, cooldown=2."""
    previous_state = {
        "ECM63.1": _controlled_prev_entry(
            consecutive=4, control_sequence_start="2026-06-05T12:00:00"
        )
    }
    manager, state_file = _new_continuation_manager(
        tmp_path,
        previous_state=previous_state,
        measurement_data=_valid_measurement(power_w=4158.0),
        bid_reference_power_kw=10.3645,
        allocated_flexibility_kw=5.0,
        recent_profile_settings={
            "maxConsecutiveActivationSlots": "bad",
            "cooldownSlotsAfterMaxActivation": -1,
        },
    )

    summary = manager.run(
        slot_override="2026-06-05T13:00:00Z",
        dry_run=True,
        simulate_sold_mw=0.005,
    )

    result = summary["control_results"]["ECM63.1"]
    assert result["status"] == "skipped"
    saved = _load_state(state_file)
    assert saved["ECM63.1"]["state"] == "cooldown"
    assert saved["ECM63.1"]["cooldown_until_slot_start"] == "2026-06-05T13:30:00"


def test_comfort_persistence_settings_fallback_used_when_strategy_keys_missing(tmp_path):
    """flexibility.persistenceSettings fallback applies when strategy keys are absent."""
    previous_state = {
        "ECM63.1": _controlled_prev_entry(
            consecutive=2, control_sequence_start="2026-06-05T12:30:00"
        )
    }
    manager, state_file = _new_continuation_manager(
        tmp_path,
        previous_state=previous_state,
        measurement_data=_valid_measurement(power_w=4158.0),
        bid_reference_power_kw=10.3645,
        allocated_flexibility_kw=5.0,
        recent_profile_settings=None,
        persistence_settings={
            "maxConsecutiveActivationSlots": 2,
            "cooldownSlotsAfterMaxActivation": 1,
        },
    )

    summary = manager.run(
        slot_override="2026-06-05T13:00:00Z",
        dry_run=True,
        simulate_sold_mw=0.005,
    )

    result = summary["control_results"]["ECM63.1"]
    assert result["status"] == "skipped"
    assert "maximum consecutive activation slots" in result.get("skip_reason", "")
    saved = _load_state(state_file)
    assert saved["ECM63.1"]["state"] == "cooldown"
    # persistenceSettings cooldown=1 -> 13:00 + 1 * 15min = 13:15
    assert saved["ECM63.1"]["cooldown_until_slot_start"] == "2026-06-05T13:15:00"


def test_comfort_K_strategy9_unaffected(tmp_path):
    """K: strategy_9 / persistence_baseline ignores comfort cap."""
    import pandas as pd

    previous_state = {
        "ECM63.1": {
            "state": "controlled",
            "asset_type": "ev_charger",
            "slot_start": "2026-06-05T12:45:00",
            "slot_end": "2026-06-05T13:00:00",
            "consecutive_activation_slots": 10,
            "control_sequence_start": "2026-06-05T10:00:00",
            "strategy_id": "strategy_9",
        }
    }
    state_file = str(tmp_path / "state.json")
    _write_previous_state(state_file, previous_state)

    config = {
        "fm": {
            "community": "test",
            "actors": {"fsps": {"fsp1": {"name": "FSP1", "assets": ["ECM63.1"]}}},
        },
        "asset_mapping": {
            "ECM63.1": {
                "type": "ev_charger",
                "description": "EV charger 1",
                "capacity_kw": 11.0,
                "min_power_kw": 0.0,
                "modulation_type": "continuous",
                "device_name_tag": "ev1",
                "pod": "ECM63",
            },
        },
        "bidding_strategies": {
            "strategy_9": {
                "name": "Persistence EV Strategy",
                "description": "test persistence EV",
                "asset_types": ["ev_charger"],
                "assets_filter": ["ECM63.1"],
                "flexibility_method": "persistence",
                "time_slots": [{
                    "name": "All day",
                    "start": "00:00",
                    "end": "23:59",
                    "flexibility_mw": 0.002,
                    "bid_price": 9.0,
                    "activation_cost": 2.5,
                }],
            },
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
        state_file=state_file,
    )
    manager.organization_id = "org-1"
    manager.market_handler = FakeMarketHandler([])
    manager.activation_measurement_provider = FakeMeasurementProvider(
        _valid_measurement(power_w=11000.0)
    )

    bid_handler = FakeBidHandler()
    bid_handler.bid_record = {
        "id": "bid-s9",
        "total_quantity_mw": 0.005,
        "assets_to_activate": [
            {
                "asset_id": "ECM63.1",
                "available_flexibility_kw": 5.0,
                "reference_power_kw": 10.3645,
                "reference_power_source": "persistence_baseline",
                "modulation_type": "continuous",
            }
        ],
        "strategy": {"id": "strategy_9", "name": "Persistence EV Strategy"},
    }
    manager.bid_handler = bid_handler

    summary = manager.run(
        slot_override="2026-06-05T13:00:00Z",
        dry_run=True,
        simulate_sold_mw=0.005,
    )

    result = summary["control_results"]["ECM63.1"]
    # strategy_9 uses bid reference directly and is not blocked by the cap
    assert result["status"] != "skipped"
    assert result.get("activation_current_power_kw") is None
    saved = _load_state(state_file)
    # No cooldown introduced; comfort fields not added for strategy_9
    assert saved["ECM63.1"].get("state") != "cooldown"
    assert "consecutive_activation_slots" not in saved["ECM63.1"]


def test_comfort_L_discrete_hp_unaffected(tmp_path):
    """L: discrete HP ignores comfort cap."""
    previous_state = {
        "HP1": {
            "state": "controlled",
            "asset_type": "heat_pump",
            "slot_start": "2026-06-05T12:45:00",
            "slot_end": "2026-06-05T13:00:00",
            "consecutive_activation_slots": 9,
            "strategy_id": "strategy_8",
        }
    }
    state_file = str(tmp_path / "state.json")
    _write_previous_state(state_file, previous_state)

    config = {
        "fm": {
            "community": "test",
            "actors": {"fsps": {"fsp1": {"name": "FSP1", "assets": ["HP1"]}}},
        },
        "asset_mapping": {
            "HP1": {
                "type": "heat_pump",
                "description": "test heat pump",
                "capacity_kw": 10.0,
                "modulation_type": "discrete",
                "discrete_states_kw": [0.0, 10.0],
            },
        },
        "bidding_strategies": {
            "strategy_8": {
                "name": "Persistence HP Strategy",
                "description": "test persistence",
                "asset_types": ["heat_pump"],
                "assets_filter": ["HP1"],
                "flexibility_method": "persistence",
                "time_slots": [{
                    "name": "All day",
                    "start": "00:00",
                    "end": "23:59",
                    "flexibility_mw": 0.010,
                    "bid_price": 9.0,
                    "activation_cost": 2.5,
                }],
            },
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
        state_file=state_file,
    )
    manager.organization_id = "org-1"
    manager.market_handler = FakeMarketHandler([])
    manager.activation_measurement_provider = FakeMeasurementProvider({})

    bid_handler = FakeBidHandler()
    bid_handler.bid_record = {
        "id": "bid-hp",
        "total_quantity_mw": 0.010,
        "assets_to_activate": [
            {
                "asset_id": "HP1",
                "available_flexibility_kw": 10.0,
                "reference_power_kw": 10.0,
                "reference_power_source": "persistence_baseline",
                "modulation_type": "discrete",
            }
        ],
        "strategy": {
            "id": "strategy_8",
            "name": "Persistence HP Strategy",
            "description": "test persistence",
        },
    }
    manager.bid_handler = bid_handler

    summary = manager.run(
        slot_override="2026-06-05T13:00:00Z",
        dry_run=True,
        simulate_sold_mw=0.010,
    )

    result = summary["control_results"]["HP1"]
    assert result["status"] != "skipped"
    assert result["discrete_state"] == "OFF"
    saved = _load_state(state_file)
    assert saved["HP1"].get("state") != "cooldown"


def test_comfort_M_separate_assets_tracked_independently(tmp_path):
    """M: ECM63.1 (count=4) blocked, ECM63.2 (count=1) continues; no contamination."""
    import pandas as pd

    previous_state = {
        "ECM63.1": _controlled_prev_entry(
            consecutive=4, control_sequence_start="2026-06-05T12:00:00"
        ),
        "ECM63.2": {
            "state": "controlled",
            "asset_type": "ev_charger",
            "slot_start": "2026-06-05T12:45:00",
            "slot_end": "2026-06-05T13:00:00",
            "consecutive_activation_slots": 1,
            "control_sequence_start": "2026-06-05T12:45:00",
            "reference_power_kw": 8.0,
            "reference_power_source": "recent_profile_baseline",
            "strategy_id": "strategy_10",
        },
    }
    state_file = str(tmp_path / "state.json")
    _write_previous_state(state_file, previous_state)

    config = {
        "fm": {
            "community": "test",
            "actors": {"fsps": {"fsp1": {"name": "FSP1", "assets": ["ECM63.1", "ECM63.2"]}}},
        },
        "asset_mapping": {
            "ECM63.1": {
                "type": "ev_charger",
                "description": "EV charger 1",
                "capacity_kw": 11.0,
                "min_power_kw": 0.0,
                "modulation_type": "continuous",
                "device_name_tag": "ev1",
                "pod": "ECM63",
            },
            "ECM63.2": {
                "type": "ev_charger",
                "description": "EV charger 2",
                "capacity_kw": 11.0,
                "min_power_kw": 0.0,
                "modulation_type": "continuous",
                "device_name_tag": "ev2",
                "pod": "ECM63",
            },
        },
        "bidding_strategies": {
            "strategy_10": _recent_profile_strategy_config(),
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
        state_file=state_file,
    )
    manager.organization_id = "org-1"
    manager.market_handler = FakeMarketHandler([])
    manager.activation_measurement_provider = FakeMeasurementProvider({
        "ECM63.1": {
            "valid": True,
            "timestamp_utc": pd.Timestamp("2026-06-05T12:55:00Z"),
            "power_w": 4158.0,
            "age_minutes": 10.0,
        },
        "ECM63.2": {
            "valid": True,
            "timestamp_utc": pd.Timestamp("2026-06-05T12:55:00Z"),
            "power_w": 4158.0,
            "age_minutes": 10.0,
        },
    })
    manager.controller.rabbitmq_publisher = object()

    bid_handler = FakeBidHandler()
    bid_handler.bid_record = {
        "id": "bid-multi",
        "total_quantity_mw": 0.008,
        "assets_to_activate": [
            {
                "asset_id": "ECM63.1",
                "available_flexibility_kw": 5.0,
                "reference_power_kw": 10.3645,
                "reference_power_source": "recent_profile_baseline",
                "modulation_type": "continuous",
            },
            {
                "asset_id": "ECM63.2",
                "available_flexibility_kw": 3.0,
                "reference_power_kw": 8.0,
                "reference_power_source": "recent_profile_baseline",
                "modulation_type": "continuous",
            },
        ],
        "strategy": {"id": "strategy_10", "name": "Recent Profile EV Strategy"},
    }
    manager.bid_handler = bid_handler

    summary = manager.run(
        slot_override="2026-06-05T13:00:00Z",
        dry_run=True,
        simulate_sold_mw=0.008,
    )

    r1 = summary["control_results"]["ECM63.1"]
    r2 = summary["control_results"]["ECM63.2"]
    assert r1["status"] == "skipped"
    assert "maximum consecutive activation slots" in r1.get("skip_reason", "")
    assert r2["status"] != "skipped"

    restore_1 = [
        cmd for cmd in manager.controller._pending_commands
        if cmd.get("command_type") == "restore" and cmd.get("asset_id") == "ECM63.1"
    ]
    assert len(restore_1) == 1
    restore_2 = [
        cmd for cmd in manager.controller._pending_commands
        if cmd.get("command_type") == "restore" and cmd.get("asset_id") == "ECM63.2"
    ]
    assert restore_2 == []

    saved = _load_state(state_file)
    assert saved["ECM63.1"]["state"] == "cooldown"
    assert saved["ECM63.2"]["state"] == "controlled"
    assert saved["ECM63.2"]["consecutive_activation_slots"] == 2


def test_comfort_N_existing_continuation_still_works(tmp_path):
    """N: Observed case continuation below max -> target 5.3645, no restore."""
    previous_state = {
        "ECM63.1": _controlled_prev_entry(
            consecutive=1, control_sequence_start="2026-06-05T12:45:00"
        )
    }
    import pandas as pd
    manager, state_file = _new_continuation_manager(
        tmp_path,
        previous_state=previous_state,
        measurement_data={
            "ECM63.1": {
                "valid": True,
                "timestamp_utc": pd.Timestamp("2026-06-05T12:45:00Z"),
                "power_w": 4158.0,
                "age_minutes": 14.1,
            }
        },
        bid_reference_power_kw=10.3645,
        allocated_flexibility_kw=5.0,
    )
    manager.controller.rabbitmq_publisher = object()

    summary = manager.run(
        slot_override="2026-06-05T13:00:00Z",
        dry_run=True,
        simulate_sold_mw=0.005,
    )

    result = summary["control_results"]["ECM63.1"]
    assert result["status"] != "skipped"
    assert result["target_power_kw"] == pytest.approx(5.3645, abs=0.01)
    restore_cmds = [
        cmd for cmd in manager.controller._pending_commands
        if cmd.get("command_type") == "restore"
    ]
    assert restore_cmds == []
    saved = _load_state(state_file)
    assert saved["ECM63.1"]["state"] == "controlled"
    assert saved["ECM63.1"]["consecutive_activation_slots"] == 2


# =============================================================================
# STRATEGY 13 HP OFF-ONLY ACTIVATION TESTS
# =============================================================================

_HP_OFF_ONLY_STRATEGY_ID = "strategy_13"


def _hp_off_only_strategy_config():
    return {
        "name": "Conservative HP OFF-only Strategy",
        "description": "test HP OFF-only strategy",
        "asset_types": ["heat_pump"],
        "assets_filter": ["ECM96.2", "ECM97.3"],
        "flexibility_method": "hp_off_only",
        "hpOffOnlySettings": {
            "controlMode": "off_only",
            "allowPreheating": False,
            "baselineGoBackMinutes": 1440,
            "recentWindowMinutes": 60,
            "quantile": 0.25,
            "minSamples": 3,
            "minActiveRatio": 0.75,
            "activeThresholdW": 500,
            "maxCurrentMeasurementAgeMinutes": 30,
            "defaultSafetyFactor": 1.0,
            "missingMeasurementPolicy": "skip_asset",
            "bidStepKw": 1.0,
            "minimumBidKw": 1.0,
            "maxConsecutiveActivationSlots": 2,
            "cooldownSlotsAfterMaxActivation": 2,
        },
        "time_slots": [{
            "name": "All day",
            "start": "00:00",
            "end": "23:59",
            "flexibility_mw": 0.036,
            "bid_price": 9.0,
            "activation_cost": 2.5,
        }],
    }


def test_production_hp_off_only_continuation_limits_are_two_plus_two():
    import json

    config_path = REPO_ROOT / "conf" / "fm02_hp.json"
    config = json.loads(config_path.read_text())
    strategy = config["bidding_strategies"][_HP_OFF_ONLY_STRATEGY_ID]
    resolved = fm.resolve_hp_off_only_settings(
        config,
        strategy_config=strategy,
        strategy_id=_HP_OFF_ONLY_STRATEGY_ID,
        granularity=config["fm"].get("granularity", 15),
    )

    assert resolved["max_consecutive_activation_slots"] == 2
    assert resolved["cooldown_slots_after_max_activation"] == 2


@pytest.mark.parametrize(
    ("key", "value", "expected_error"),
    [
        ("maxConsecutiveActivationSlots", 0, "maxConsecutiveActivationSlots"),
        (
            "cooldownSlotsAfterMaxActivation",
            -1,
            "cooldownSlotsAfterMaxActivation",
        ),
        ("maxConsecutiveActivationSlots", "invalid", "non-numeric value"),
    ],
)
def test_hp_off_only_rejects_invalid_continuation_settings(
    key, value, expected_error
):
    strategy = _hp_off_only_strategy_config()
    strategy["hpOffOnlySettings"][key] = value

    with pytest.raises(ValueError, match=expected_error):
        fm.resolve_hp_off_only_settings(
            {"flexibility": {}},
            strategy_config=strategy,
            strategy_id=_HP_OFF_ONLY_STRATEGY_ID,
            granularity=15,
        )


def _hp_asset(capacity_kw, description):
    return {
        "type": "heat_pump",
        "description": description,
        "capacity_kw": capacity_kw,
        "modulation_type": "discrete",
        "discrete_states_kw": [0.0, capacity_kw],
        "device_name_tag": description.lower().replace(" ", "_"),
        "pod": "ECM96" if capacity_kw == 6.0 else "ECM97",
        "rabbitCommandSection": "realAssetCommands",
    }


class TrackingMeasurementProvider(FakeMeasurementProvider):
    def __init__(self, measurements=None):
        super().__init__(measurements)
        self.calls = []

    def get_latest_power(self, asset_id, current_time_utc, max_age_minutes):
        self.calls.append((asset_id, max_age_minutes))
        return super().get_latest_power(asset_id, current_time_utc, max_age_minutes)


class DisconnectedRabbitPublisher:
    def is_connected(self):
        return False


class RecordingRabbitPublisher:
    def __init__(self):
        self.calls = []

    def is_connected(self):
        return True

    def publish_batch_commands(self, commands, slot_info=None):
        import copy
        self.calls.append((copy.deepcopy(commands), copy.deepcopy(slot_info)))
        return len(commands)


_NO_MEASUREMENT_PROVIDER = object()


def _new_hp_off_only_manager(
    tmp_path,
    bid_record_assets,
    measurement_provider=_NO_MEASUREMENT_PROVIDER,
    previous_state=None,
    rabbitmq_publisher=None,
    autonomous=None,
):
    asset_mapping = {
        "ECM96.2": _hp_asset(6.0, "HP small"),
        "ECM97.3": _hp_asset(30.0, "HP large"),
    }
    config = {
        "fm": {
            "community": "test",
            "granularity": 15,
            "actors": {
                "fsps": {
                    "supsi02": {
                        "name": "SUPSI_HP",
                        "strategy": _HP_OFF_ONLY_STRATEGY_ID,
                        "assets": list(asset_mapping),
                    }
                }
            },
        },
        "asset_mapping": asset_mapping,
        "bidding_strategies": {
            _HP_OFF_ONLY_STRATEGY_ID: _hp_off_only_strategy_config(),
        },
        "rabbitMQ": {
            "realAssetCommands": {
                "exchange": "flexi_commands",
                "queue": "flexi_commands_queue",
                "routingKey": "real_asset.command",
            },
        },
        "autonomous": autonomous or {"enabled": False},
    }
    state_file = str(tmp_path / "hp_state.json")
    if previous_state is not None:
        _write_previous_state(state_file, previous_state)

    manager = fm.FlexibilityManager(
        config=config,
        fsp_id="supsi02",
        nodes_interface=FakeNodesInterface(),
        bid_repo=None,
        logger=_logger(),
        nodes_authenticated=False,
        rabbitmq_publisher=rabbitmq_publisher,
        demand_repo=None,
        state_file=state_file,
    )
    manager.organization_id = "org-hp"
    manager.market_handler = FakeMarketHandler([])
    if measurement_provider is not _NO_MEASUREMENT_PROVIDER:
        manager.activation_measurement_provider = measurement_provider

    bid_handler = FakeBidHandler()
    bid_handler.bid_record = {
        "id": "bid-hp-off-only-1",
        "total_quantity_mw": sum(
            row.get("available_flexibility_kw", 0.0) for row in bid_record_assets
        ) / 1000.0,
        "assets_to_activate": bid_record_assets,
        "strategy": {
            "id": _HP_OFF_ONLY_STRATEGY_ID,
            "name": "Conservative HP OFF-only Strategy",
        },
    }
    manager.bid_handler = bid_handler
    return manager, state_file


def _fresh_hp_measurement(power_w, age_minutes=5.0):
    return {
        "valid": True,
        "timestamp_utc": "2026-10-08T11:55:00Z",
        "power_w": power_w,
        "age_minutes": age_minutes,
    }


def test_shared_commitment_coordinator_blocks_third_slot_without_local_state(
    tmp_path,
):
    class CommitmentRepository:
        def get_committed_asset_slots(
            self, fsp_id, asset_ids, from_slot, before_slot
        ):
            assert fsp_id == "supsi02"
            assert asset_ids == ["ECM96.2"]
            return [
                {
                    "asset_id": "ECM96.2",
                    "slot_start": fm.datetime(2026, 10, 8, 11, 30),
                },
                {
                    "asset_id": "ECM96.2",
                    "slot_start": fm.datetime(2026, 10, 8, 11, 45),
                },
            ]

        def save_asset_activations_batch(self, **kwargs):
            return len(kwargs.get("activations", []))

    provider = TrackingMeasurementProvider({
        "ECM96.2": _fresh_hp_measurement(4200.0),
    })
    manager, state_file = _new_hp_off_only_manager(
        tmp_path,
        [{"asset_id": "ECM96.2", "available_flexibility_kw": 6.0}],
        measurement_provider=provider,
    )
    repository = CommitmentRepository()
    manager.bid_repo = repository
    manager.commitment_coordinator = fm.AssetCommitmentCoordinator(
        repository, manager.logger
    )

    summary = manager.run(
        slot_override="2026-10-08T12:00:00Z",
        dry_run=True,
        simulate_sold_mw=0.006,
    )

    result = summary["control_results"]["ECM96.2"]
    assert result["status"] == "skipped"
    assert result["eligibility_basis"] == "shared_commitment_coordinator"
    assert result["sequence_reason_code"] == "sequence_cooldown"
    assert summary["commitment_sequence_checks"]["ECM96.2"][
        "blocked_until_slot"
    ] == "2026-10-08T12:30:00"
    assert provider.calls == []
    assert manager.controller._pending_commands == []
    state = _load_state(state_file)
    assert state["ECM96.2"]["state"] == "cooldown"
    assert state["ECM96.2"]["cooldown_until_slot_start"] == (
        "2026-10-08T12:30:00"
    )


def test_shared_commitment_repository_error_fails_dispatch_closed(
    tmp_path, caplog
):
    class FailingCommitmentRepository:
        def get_committed_asset_slots(self, **kwargs):
            raise RuntimeError("commitment database unavailable")

        def save_asset_activations_batch(self, **kwargs):
            return len(kwargs.get("activations", []))

    provider = TrackingMeasurementProvider({
        "ECM96.2": _fresh_hp_measurement(4200.0),
    })
    manager, _ = _new_hp_off_only_manager(
        tmp_path,
        [{"asset_id": "ECM96.2", "available_flexibility_kw": 6.0}],
        measurement_provider=provider,
    )
    repository = FailingCommitmentRepository()
    manager.bid_repo = repository
    manager.commitment_coordinator = fm.AssetCommitmentCoordinator(
        repository, manager.logger
    )

    with caplog.at_level(logging.INFO):
        summary = manager.run(
            slot_override="2026-10-08T12:00:00Z",
            dry_run=True,
            simulate_sold_mw=0.006,
        )

    result = summary["control_results"]["ECM96.2"]
    assert result["status"] == "skipped"
    assert result["sequence_reason_code"] == "coordination_unavailable"
    assert result["delivery_at_risk"] is True
    assert summary["commitment_coordination_status"] == "unavailable"
    assert summary["status"] == "activation_failed"
    assert provider.calls == []
    assert manager.controller._pending_commands == []
    assert "ACTIVATION_SEQUENCE_DISPATCH_GUARD state=ERROR" in caplog.text
    assert "state=DISPATCH_BLOCKED" in caplog.text


def test_missing_shared_coordinator_with_live_repository_fails_closed(tmp_path):
    class ActivationOnlyRepository:
        def save_asset_activations_batch(self, **kwargs):
            return len(kwargs.get("activations", []))

    provider = TrackingMeasurementProvider({
        "ECM96.2": _fresh_hp_measurement(4200.0),
    })
    manager, _ = _new_hp_off_only_manager(
        tmp_path,
        [{"asset_id": "ECM96.2", "available_flexibility_kw": 6.0}],
        measurement_provider=provider,
    )
    manager.bid_repo = ActivationOnlyRepository()
    manager.commitment_coordinator = None

    summary = manager.run(
        slot_override="2026-10-08T12:00:00Z",
        dry_run=True,
        simulate_sold_mw=0.006,
    )

    result = summary["control_results"]["ECM96.2"]
    assert result["status"] == "skipped"
    assert result["sequence_reason_code"] == "coordination_unavailable"
    assert summary["commitment_coordination_status"] == "unavailable"
    assert provider.calls == []


def test_shared_commitment_guard_blocks_one_asset_and_dispatches_the_other(
    tmp_path, caplog
):
    class PartialCommitmentRepository:
        def get_committed_asset_slots(
            self, fsp_id, asset_ids, from_slot, before_slot
        ):
            assert set(asset_ids) == {"ECM96.2", "ECM97.3"}
            return [
                {
                    "asset_id": "ECM96.2",
                    "slot_start": fm.datetime(2026, 10, 8, 11, 30),
                },
                {
                    "asset_id": "ECM96.2",
                    "slot_start": fm.datetime(2026, 10, 8, 11, 45),
                },
            ]

        def save_asset_activations_batch(self, **kwargs):
            return len(kwargs.get("activations", []))

    provider = TrackingMeasurementProvider({
        "ECM96.2": _fresh_hp_measurement(4200.0),
        "ECM97.3": _fresh_hp_measurement(20000.0),
    })
    manager, _ = _new_hp_off_only_manager(
        tmp_path,
        [
            {"asset_id": "ECM96.2", "available_flexibility_kw": 6.0},
            {"asset_id": "ECM97.3", "available_flexibility_kw": 30.0},
        ],
        measurement_provider=provider,
    )
    repository = PartialCommitmentRepository()
    manager.bid_repo = repository
    manager.commitment_coordinator = fm.AssetCommitmentCoordinator(
        repository, manager.logger
    )

    with caplog.at_level(logging.INFO):
        summary = manager.run(
            slot_override="2026-10-08T12:00:00Z",
            dry_run=True,
            simulate_sold_mw=0.036,
        )

    assert summary["control_results"]["ECM96.2"]["status"] == "skipped"
    assert summary["control_results"]["ECM97.3"]["status"] == "simulated"
    assert summary["commitment_coordination_status"] == "assets_blocked"
    assert provider.calls == [("ECM97.3", 30)]
    assert "state=ASSETS_BLOCKED" in caplog.text


def test_hp_off_only_fresh_active_hp_receives_off_and_reports_delivery_risk(tmp_path):
    provider = TrackingMeasurementProvider({
        "ECM96.2": _fresh_hp_measurement(4200.0),
    })
    manager, _ = _new_hp_off_only_manager(
        tmp_path,
        [{"asset_id": "ECM96.2", "available_flexibility_kw": 6.0}],
        measurement_provider=provider,
    )

    summary = manager.run(
        slot_override="2026-10-08T12:00:00Z",
        dry_run=True,
        simulate_sold_mw=0.006,
    )

    result = summary["control_results"]["ECM96.2"]
    assert result["status"] == "simulated"
    assert result["discrete_state"] == "OFF"
    assert result["target_power_kw"] == pytest.approx(0.0)
    assert result["activation_current_power_kw"] == pytest.approx(4.2)
    assert result["estimated_delivery_kw"] == pytest.approx(4.2)
    assert result["delivery_shortfall_risk_kw"] == pytest.approx(1.8)
    assert result["delivery_at_risk"] is True
    assert summary["hp_off_only_commanded_assets"] == ["ECM96.2"]
    assert summary["hp_off_only_estimated_delivery_kw"] == pytest.approx(4.2)
    assert summary["hp_off_only_delivery_shortfall_risk_kw"] == pytest.approx(1.8)
    assert provider.calls == [("ECM96.2", 30)]
    state = _load_state(manager.state_file)
    assert state["ECM96.2"]["consecutive_activation_slots"] == 1
    assert state["ECM96.2"]["control_sequence_start"] == "2026-10-08T12:00:00"


@pytest.mark.parametrize("power_w", [0.0, 499.9, 500.0])
def test_hp_off_only_at_or_below_500w_is_ineligible_and_receives_no_off(
    tmp_path, power_w
):
    provider = TrackingMeasurementProvider({
        "ECM96.2": _fresh_hp_measurement(power_w),
    })
    manager, _ = _new_hp_off_only_manager(
        tmp_path,
        [{"asset_id": "ECM96.2", "available_flexibility_kw": 6.0}],
        measurement_provider=provider,
    )

    summary = manager.run(
        slot_override="2026-10-08T12:00:00Z",
        dry_run=True,
        simulate_sold_mw=0.006,
    )

    result = summary["control_results"]["ECM96.2"]
    assert result["status"] == "skipped"
    assert result["target_power_kw"] is None
    assert "at or below" in result["skip_reason"]
    assert manager.controller._pending_commands == []
    assert summary["hp_off_only_commanded_assets"] == []
    assert summary["total_flexibility_deliverable_kw"] == pytest.approx(0.0)
    assert summary["status"] == "activation_failed"


@pytest.mark.parametrize(
    ("measurement_provider", "expected_reason"),
    [
        (_NO_MEASUREMENT_PROVIDER, "no activation measurement provider"),
        (TrackingMeasurementProvider({}), "no valid current measurement"),
        (
            TrackingMeasurementProvider({
                "ECM96.2": _fresh_hp_measurement(4000.0, age_minutes=31.0),
            }),
            "stale or future-dated",
        ),
    ],
)
def test_hp_off_only_missing_or_stale_telemetry_fails_closed(
    tmp_path, measurement_provider, expected_reason
):
    manager, _ = _new_hp_off_only_manager(
        tmp_path,
        [{"asset_id": "ECM96.2", "available_flexibility_kw": 6.0}],
        measurement_provider=measurement_provider,
    )

    summary = manager.run(
        slot_override="2026-10-08T12:00:00Z",
        dry_run=True,
        simulate_sold_mw=0.006,
    )

    result = summary["control_results"]["ECM96.2"]
    assert result["status"] == "skipped"
    assert expected_reason in result["skip_reason"]
    assert manager.controller._pending_commands == []


def test_hp_off_only_partial_acceptance_checks_and_commands_selected_subset_only(tmp_path):
    provider = TrackingMeasurementProvider({
        "ECM96.2": _fresh_hp_measurement(5500.0),
        "ECM97.3": _fresh_hp_measurement(20000.0),
    })
    manager, _ = _new_hp_off_only_manager(
        tmp_path,
        [
            {"asset_id": "ECM97.3", "available_flexibility_kw": 30.0},
            {"asset_id": "ECM96.2", "available_flexibility_kw": 6.0},
        ],
        measurement_provider=provider,
    )

    summary = manager.run(
        slot_override="2026-10-08T12:00:00Z",
        dry_run=True,
        simulate_sold_mw=0.006,
    )

    assert summary["allocations"] == pytest.approx({"ECM96.2": 6.0})
    assert set(summary["control_results"]) == {"ECM96.2"}
    assert summary["control_results"]["ECM96.2"]["discrete_state"] == "OFF"
    assert provider.calls == [("ECM96.2", 30)]


def test_hp_off_only_adjacent_controlled_hp_continues_off_below_threshold(tmp_path):
    previous_state = {
        "ECM96.2": {
            "state": "controlled",
            "asset_type": "heat_pump",
            "slot_start": "2026-10-08T11:45:00",
            "slot_end": "2026-10-08T12:00:00",
            "target_power_kw": 0.0,
            "consecutive_activation_slots": 1,
            "control_sequence_start": "2026-10-08T11:45:00",
            "strategy_id": "strategy_13",
        }
    }
    publisher = DisconnectedRabbitPublisher()
    provider = TrackingMeasurementProvider({
        "ECM96.2": _fresh_hp_measurement(300.0),
    })
    manager, state_file = _new_hp_off_only_manager(
        tmp_path,
        [{"asset_id": "ECM96.2", "available_flexibility_kw": 6.0}],
        measurement_provider=provider,
        previous_state=previous_state,
        rabbitmq_publisher=publisher,
    )

    summary = manager.run(
        slot_override="2026-10-08T12:00:00Z",
        dry_run=True,
        simulate_sold_mw=0.006,
    )

    result = summary["control_results"]["ECM96.2"]
    assert result["status"] == "queued"
    assert result["discrete_state"] == "OFF"
    assert result["target_power_kw"] == pytest.approx(0.0)
    assert result["is_continuation"] is True
    assert result["eligibility_basis"] == "controlled_state_continuation"
    assert provider.calls == []
    commands = manager.controller._pending_commands
    assert len(commands) == 1
    assert commands[0]["command_type"] == "curtail"
    assert commands[0]["payload"]["discrete_state"] == "OFF"
    state = _load_state(state_file)
    assert state["ECM96.2"]["consecutive_activation_slots"] == 2
    assert state["ECM96.2"]["control_sequence_start"] == "2026-10-08T11:45:00"


def test_hp_off_only_continuation_works_without_measurement_provider(tmp_path):
    previous_state = {
        "ECM96.2": {
            "state": "controlled",
            "asset_type": "heat_pump",
            "slot_start": "2026-10-08T11:45:00",
            "slot_end": "2026-10-08T12:00:00",
            "consecutive_activation_slots": 1,
        }
    }
    manager, state_file = _new_hp_off_only_manager(
        tmp_path,
        [{"asset_id": "ECM96.2", "available_flexibility_kw": 6.0}],
        previous_state=previous_state,
    )

    summary = manager.run(
        slot_override="2026-10-08T12:00:00Z",
        dry_run=True,
        simulate_sold_mw=0.006,
    )

    result = summary["control_results"]["ECM96.2"]
    assert result["status"] == "simulated"
    assert result["discrete_state"] == "OFF"
    assert result["is_continuation"] is True
    assert _load_state(state_file)["ECM96.2"][
        "consecutive_activation_slots"
    ] == 2


def test_hp_off_only_third_consecutive_slot_restores_and_enters_cooldown(tmp_path):
    previous_state = {
        "ECM96.2": {
            "state": "controlled",
            "asset_type": "heat_pump",
            "slot_start": "2026-10-08T11:45:00",
            "slot_end": "2026-10-08T12:00:00",
            "consecutive_activation_slots": 2,
            "control_sequence_start": "2026-10-08T11:30:00",
            "strategy_id": "strategy_13",
        }
    }
    publisher = DisconnectedRabbitPublisher()
    provider = TrackingMeasurementProvider({
        "ECM96.2": _fresh_hp_measurement(0.0),
    })
    manager, state_file = _new_hp_off_only_manager(
        tmp_path,
        [{"asset_id": "ECM96.2", "available_flexibility_kw": 6.0}],
        measurement_provider=provider,
        previous_state=previous_state,
        rabbitmq_publisher=publisher,
    )

    summary = manager.run(
        slot_override="2026-10-08T12:00:00Z",
        dry_run=True,
        simulate_sold_mw=0.006,
    )

    result = summary["control_results"]["ECM96.2"]
    assert result["status"] == "skipped"
    assert result["eligibility_basis"] == "max_consecutive_activation_guard"
    assert provider.calls == []
    commands = manager.controller._pending_commands
    assert len(commands) == 1
    assert commands[0]["command_type"] == "restore"
    assert commands[0]["payload"]["discrete_state"] == "ON"
    state = _load_state(state_file)
    assert state["ECM96.2"]["state"] == "cooldown"
    assert state["ECM96.2"]["cooldown_until_slot_start"] == (
        "2026-10-08T12:30:00"
    )
    assert state["ECM96.2"]["last_consecutive_activation_slots"] == 2


def test_hp_off_only_cooldown_blocks_without_repeated_restore(tmp_path):
    previous_state = {
        "ECM96.2": {
            "state": "cooldown",
            "asset_type": "heat_pump",
            "cooldown_started_slot": "2026-10-08T12:00:00",
            "cooldown_until_slot_start": "2026-10-08T12:30:00",
            "cooldown_slots_after_max_activation": 2,
            "last_consecutive_activation_slots": 2,
            "strategy_id": "strategy_13",
        }
    }
    publisher = DisconnectedRabbitPublisher()
    provider = TrackingMeasurementProvider({
        "ECM96.2": _fresh_hp_measurement(5000.0),
    })
    manager, state_file = _new_hp_off_only_manager(
        tmp_path,
        [{"asset_id": "ECM96.2", "available_flexibility_kw": 6.0}],
        measurement_provider=provider,
        previous_state=previous_state,
        rabbitmq_publisher=publisher,
    )

    summary = manager.run(
        slot_override="2026-10-08T12:15:00Z",
        dry_run=True,
        simulate_sold_mw=0.006,
    )

    result = summary["control_results"]["ECM96.2"]
    assert result["status"] == "skipped"
    assert result["eligibility_basis"] == "comfort_cooldown"
    assert provider.calls == []
    assert manager.controller._pending_commands == []
    assert _load_state(state_file)["ECM96.2"]["state"] == "cooldown"


def test_hp_off_only_cooldown_survives_a_slot_with_no_accepted_trade(tmp_path):
    previous_state = {
        "ECM96.2": {
            "state": "cooldown",
            "asset_type": "heat_pump",
            "cooldown_started_slot": "2026-10-08T12:00:00",
            "cooldown_until_slot_start": "2026-10-08T12:30:00",
            "cooldown_slots_after_max_activation": 2,
            "last_consecutive_activation_slots": 2,
            "strategy_id": "strategy_13",
        }
    }
    publisher = DisconnectedRabbitPublisher()
    manager, state_file = _new_hp_off_only_manager(
        tmp_path,
        [{"asset_id": "ECM96.2", "available_flexibility_kw": 6.0}],
        previous_state=previous_state,
        rabbitmq_publisher=publisher,
    )

    summary = manager.run(
        slot_override="2026-10-08T12:15:00Z",
        dry_run=True,
    )

    assert summary["status"] == "no_trades"
    assert manager.controller._pending_commands == []
    state = _load_state(state_file)
    assert state["ECM96.2"]["state"] == "cooldown"
    assert state["ECM96.2"]["cooldown_until_slot_start"] == (
        "2026-10-08T12:30:00"
    )


def test_hp_off_only_controlled_hp_is_restored_when_next_slot_has_no_trade(tmp_path):
    previous_state = {
        "ECM96.2": {
            "state": "controlled",
            "asset_type": "heat_pump",
            "slot_start": "2026-10-08T11:45:00",
            "slot_end": "2026-10-08T12:00:00",
            "consecutive_activation_slots": 1,
            "strategy_id": "strategy_13",
        }
    }
    publisher = DisconnectedRabbitPublisher()
    manager, state_file = _new_hp_off_only_manager(
        tmp_path,
        [{"asset_id": "ECM96.2", "available_flexibility_kw": 6.0}],
        previous_state=previous_state,
        rabbitmq_publisher=publisher,
    )

    summary = manager.run(
        slot_override="2026-10-08T12:00:00Z",
        dry_run=True,
    )

    assert summary["status"] == "no_trades"
    commands = manager.controller._pending_commands
    assert len(commands) == 1
    assert commands[0]["command_type"] == "restore"
    assert commands[0]["payload"]["discrete_state"] == "ON"
    assert _load_state(state_file) == {}


def test_hp_off_only_expired_cooldown_requires_fresh_active_telemetry(tmp_path):
    previous_state = {
        "ECM96.2": {
            "state": "cooldown",
            "asset_type": "heat_pump",
            "cooldown_started_slot": "2026-10-08T12:00:00",
            "cooldown_until_slot_start": "2026-10-08T12:30:00",
            "last_consecutive_activation_slots": 2,
            "strategy_id": "strategy_13",
        }
    }
    provider = TrackingMeasurementProvider({
        "ECM96.2": _fresh_hp_measurement(4200.0),
    })
    manager, state_file = _new_hp_off_only_manager(
        tmp_path,
        [{"asset_id": "ECM96.2", "available_flexibility_kw": 6.0}],
        measurement_provider=provider,
        previous_state=previous_state,
    )

    summary = manager.run(
        slot_override="2026-10-08T12:30:00Z",
        dry_run=True,
        simulate_sold_mw=0.006,
    )

    result = summary["control_results"]["ECM96.2"]
    assert result["status"] == "simulated"
    assert result["is_continuation"] is False
    assert result["eligibility_basis"] == "fresh_uncontrolled_telemetry"
    assert provider.calls == [("ECM96.2", 30)]
    state = _load_state(state_file)
    assert state["ECM96.2"]["state"] == "controlled"
    assert state["ECM96.2"]["consecutive_activation_slots"] == 1


def test_hp_off_only_non_adjacent_state_is_not_continuation(tmp_path):
    previous_state = {
        "ECM96.2": {
            "state": "controlled",
            "asset_type": "heat_pump",
            "slot_start": "2026-10-08T11:30:00",
            "slot_end": "2026-10-08T11:45:00",
            "consecutive_activation_slots": 1,
        }
    }
    publisher = DisconnectedRabbitPublisher()
    provider = TrackingMeasurementProvider({
        "ECM96.2": _fresh_hp_measurement(300.0),
    })
    manager, state_file = _new_hp_off_only_manager(
        tmp_path,
        [{"asset_id": "ECM96.2", "available_flexibility_kw": 6.0}],
        measurement_provider=provider,
        previous_state=previous_state,
        rabbitmq_publisher=publisher,
    )

    summary = manager.run(
        slot_override="2026-10-08T12:00:00Z",
        dry_run=True,
        simulate_sold_mw=0.006,
    )

    result = summary["control_results"]["ECM96.2"]
    assert result["status"] == "skipped"
    assert result["is_continuation"] is False
    assert provider.calls == [("ECM96.2", 30)]
    commands = manager.controller._pending_commands
    assert len(commands) == 1
    assert commands[0]["command_type"] == "restore"
    assert commands[0]["payload"]["discrete_state"] == "ON"
    assert _load_state(state_file) == {}


def test_hp_off_only_asset_rotation_restores_old_hp_and_switches_new_hp_off(tmp_path):
    previous_state = {
        "ECM96.2": {
            "state": "controlled",
            "asset_type": "heat_pump",
            "slot_start": "2026-10-08T11:45:00",
            "slot_end": "2026-10-08T12:00:00",
            "consecutive_activation_slots": 1,
        }
    }
    publisher = DisconnectedRabbitPublisher()
    provider = TrackingMeasurementProvider({
        "ECM97.3": _fresh_hp_measurement(20000.0),
    })
    manager, state_file = _new_hp_off_only_manager(
        tmp_path,
        [{"asset_id": "ECM97.3", "available_flexibility_kw": 30.0}],
        measurement_provider=provider,
        previous_state=previous_state,
        rabbitmq_publisher=publisher,
    )

    summary = manager.run(
        slot_override="2026-10-08T12:00:00Z",
        dry_run=True,
        simulate_sold_mw=0.030,
    )

    assert summary["control_results"]["ECM97.3"]["status"] == "queued"
    assert provider.calls == [("ECM97.3", 30)]
    commands = manager.controller._pending_commands
    assert [command["command_type"] for command in commands] == [
        "curtail",
        "restore",
    ]
    assert commands[0]["asset_id"] == "ECM97.3"
    assert commands[0]["payload"]["discrete_state"] == "OFF"
    assert commands[1]["asset_id"] == "ECM96.2"
    assert commands[1]["payload"]["discrete_state"] == "ON"
    state = _load_state(state_file)
    assert set(state) == {"ECM97.3"}
    assert state["ECM97.3"]["consecutive_activation_slots"] == 1


def test_hp_off_only_rabbitmq_publishes_off_for_new_and_continuation_slots(tmp_path):
    first_publisher = RecordingRabbitPublisher()
    first_provider = TrackingMeasurementProvider({
        "ECM96.2": _fresh_hp_measurement(4200.0),
    })
    first, state_file = _new_hp_off_only_manager(
        tmp_path,
        [{"asset_id": "ECM96.2", "available_flexibility_kw": 6.0}],
        measurement_provider=first_provider,
        rabbitmq_publisher=first_publisher,
    )
    first_summary = first.run(
        slot_override="2026-10-08T12:00:00Z",
        dry_run=True,
        simulate_sold_mw=0.006,
    )

    assert first_summary["rabbitmq_published"] == 1
    first_commands, _ = first_publisher.calls[0]
    assert len(first_commands) == 1
    assert first_commands[0]["command_type"] == "curtail"
    assert first_commands[0]["payload"]["discrete_state"] == "OFF"
    assert first_commands[0]["payload"]["target_power_kw"] == pytest.approx(0.0)
    assert first_commands[0]["payload"]["slot_start"] == "2026-10-08T12:00:00"
    assert first_commands[0]["payload"]["slot_end"] == "2026-10-08T12:15:00"

    second_publisher = RecordingRabbitPublisher()
    second_provider = TrackingMeasurementProvider({
        "ECM96.2": _fresh_hp_measurement(0.0),
    })
    second, _ = _new_hp_off_only_manager(
        tmp_path,
        [{"asset_id": "ECM96.2", "available_flexibility_kw": 6.0}],
        measurement_provider=second_provider,
        rabbitmq_publisher=second_publisher,
    )
    second_summary = second.run(
        slot_override="2026-10-08T12:15:00Z",
        dry_run=True,
        simulate_sold_mw=0.006,
    )

    assert second_summary["rabbitmq_published"] == 1
    second_commands, _ = second_publisher.calls[0]
    assert len(second_commands) == 1
    assert second_commands[0]["command_type"] == "curtail"
    assert second_commands[0]["payload"]["discrete_state"] == "OFF"
    assert second_commands[0]["payload"]["target_power_kw"] == pytest.approx(0.0)
    assert second_commands[0]["payload"]["slot_start"] == "2026-10-08T12:15:00"
    assert second_commands[0]["payload"]["slot_end"] == "2026-10-08T12:30:00"
    assert second_provider.calls == []
    assert _load_state(state_file)["ECM96.2"][
        "consecutive_activation_slots"
    ] == 2

    third_publisher = RecordingRabbitPublisher()
    third, _ = _new_hp_off_only_manager(
        tmp_path,
        [{"asset_id": "ECM96.2", "available_flexibility_kw": 6.0}],
        rabbitmq_publisher=third_publisher,
    )
    third_summary = third.run(
        slot_override="2026-10-08T12:30:00Z",
        dry_run=True,
        simulate_sold_mw=0.006,
    )

    assert third_summary["rabbitmq_published"] == 1
    third_commands, _ = third_publisher.calls[0]
    assert len(third_commands) == 1
    assert third_commands[0]["command_type"] == "restore"
    assert third_commands[0]["payload"]["discrete_state"] == "ON"
    assert third_commands[0]["payload"]["target_power_kw"] == pytest.approx(6.0)
    assert third_commands[0]["payload"]["slot_start"] == "2026-10-08T12:30:00"
    assert third_commands[0]["payload"]["slot_end"] == "2026-10-08T12:45:00"
    assert _load_state(state_file)["ECM96.2"]["state"] == "cooldown"


def test_default_manager_state_files_are_isolated_per_fsp():
    config = {
        "fm": {
            "community": "test",
            "actors": {
                "fsps": {
                    "supsi01": {"name": "SUPSI_EV", "assets": []},
                    "supsi02": {"name": "SUPSI_HP", "assets": []},
                }
            },
        },
        "asset_mapping": {},
        "bidding_strategies": {},
        "autonomous": {"enabled": False},
    }
    ev_manager = fm.FlexibilityManager(
        config, "supsi01", FakeNodesInterface(), None, _logger()
    )
    hp_manager = fm.FlexibilityManager(
        config, "supsi02", FakeNodesInterface(), None, _logger()
    )

    assert ev_manager.state_file != hp_manager.state_file
    assert ev_manager.state_file.endswith("flexi_manager_state_supsi01.json")
    assert hp_manager.state_file.endswith("flexi_manager_state_supsi02.json")


def test_hp_off_only_blocks_preactivation_even_when_autonomous_config_enables_it(tmp_path):
    publisher = DisconnectedRabbitPublisher()
    manager, _ = _new_hp_off_only_manager(
        tmp_path,
        [{"asset_id": "ECM96.2", "available_flexibility_kw": 6.0}],
        rabbitmq_publisher=publisher,
        autonomous={"enabled": True, "preactivation_enabled": True},
    )
    manager.config["bidding_strategies"][_HP_OFF_ONLY_STRATEGY_ID][
        "preconditioningSettings"
    ] = {"enabled": True}

    assert manager.hp_off_only_policy_active is True
    assert manager.autonomous_preactivation_enabled is False
    assert manager._resolve_preconditioning_context() is None

    result = manager._send_preactivation_command(
        "ECM96.2", fm.datetime(2026, 10, 8, 12, 0)
    )
    assert result["status"] == "skipped"
    assert "prohibits pre-heating/ON commands" in result["skip_reason"]
    assert "target_state" not in result
    assert manager.controller._pending_commands == []
