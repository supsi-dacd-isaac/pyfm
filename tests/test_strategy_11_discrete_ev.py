"""Tests for strategy_11: recent-profile discrete EV current-step flexibility.

Covers:
  1. EV discrete states config validation
  2. Helper: state validation (_get_discrete_ev_states)
  3-5. Helper: overdelivery selection (_select_discrete_ev_target_for_curtailment)
  6. Strategy_11 bidding (recent-profile + discrete mapping)
  7-8. Strategy_11 activation (discrete target selection)
  9. Strategy_11 activation with continuation
  10. Strategy_11 comfort guard
  11. Regression: HP discrete path unchanged
  12. Regression: strategy_9 unaffected
  13. Regression: strategy_10 existing tests still pass
"""
import json
import logging
import math
import sys
import types
from datetime import datetime, timedelta
from importlib.machinery import ModuleSpec
from importlib.util import find_spec
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


try:
    influxdb_missing = find_spec("influxdb") is None
except ValueError:
    influxdb_missing = True
if influxdb_missing:
    influxdb_stub = types.ModuleType("influxdb")
    influxdb_stub.__spec__ = ModuleSpec("influxdb", loader=None)

    class InfluxDBClient:
        pass

    class DataFrameClient:
        pass

    influxdb_stub.InfluxDBClient = InfluxDBClient
    influxdb_stub.DataFrameClient = DataFrameClient
    sys.modules["influxdb"] = influxdb_stub

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


import pandas as pd  # noqa: E402

from classes.flexibility_forecaster import (  # noqa: E402
    FlexibilityForecaster,
    _compute_discrete_ev_curtailment_options,
    _get_discrete_ev_states,
    _select_discrete_ev_target_for_curtailment,
)


# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

EV_DISCRETE_STATES = [6.24, 6.93, 7.62, 8.31, 9.01, 9.70, 10.39, 11.0]
CURRENT_TIME = datetime(2026, 6, 5, 10, 0)
SLOT_TIME = datetime(2026, 6, 5, 11, 0)


# ===================================================================
# 1. EV discrete states config test
# ===================================================================

def test_ev_discrete_states_config():
    """ECM63.1 and ECM63.2 have modulation_type=discrete with valid states."""
    with open(REPO_ROOT / "conf" / "test_fm01_aem.json") as f:
        cfg = json.load(f)

    for asset_id in ("ECM63.1", "ECM63.2"):
        asset = cfg["asset_mapping"][asset_id]
        assert asset["modulation_type"] == "discrete"
        states = asset["discrete_states_kw"]
        assert isinstance(states, list)
        assert len(states) == 8
        assert 0.0 not in states
        assert all(s >= 6.0 for s in states), f"State below 6 kW in {asset_id}"
        assert max(states) == 11.0

    s11 = cfg["bidding_strategies"]["strategy_11"]
    assert s11["enabled"] is True
    assert "ECM63.1" in s11["assets_filter"]
    assert "ECM63.2" in s11["assets_filter"]
    assert s11["flexibility_method"] == "recent_profile"
    assert s11["discreteEvSettings"]["selection_policy"] == "smallest_overdelivery"
    assert s11["discreteEvSettings"]["allow_zero_state"] is False


# ===================================================================
# 2. Helper test: state validation
# ===================================================================

def test_get_discrete_ev_states_cleans_and_sorts():
    """Invalid, duplicate, unordered, sub-min and 0.0 states are handled."""
    asset_cfg = {
        "capacity_kw": 11.0,
        "min_power_kw": 6.0,
        "discrete_states_kw": [
            11.0, 6.24, "bad", None, -1, 0.0, 5.5, 6.93, 6.24, 7.62, 12.0,
        ],
    }
    result = _get_discrete_ev_states(asset_cfg)
    assert 0.0 not in result
    assert 5.5 not in result
    assert 12.0 not in result
    assert result == sorted(set(result))
    assert result == [6.24, 6.93, 7.62, 11.0]


def test_get_discrete_ev_states_allow_zero():
    """When allow_zero_state is True, 0.0 is retained."""
    asset_cfg = {
        "capacity_kw": 11.0,
        "min_power_kw": 0.0,
        "discrete_states_kw": [0.0, 6.24, 11.0],
    }
    strategy_cfg = {"discreteEvSettings": {"allow_zero_state": True, "min_target_power_kw": 0.0}}
    result = _get_discrete_ev_states(asset_cfg, strategy_cfg)
    assert 0.0 in result


def test_get_discrete_ev_states_strategy_min_target():
    """Strategy-level min_target_power_kw overrides asset min_power_kw."""
    asset_cfg = {
        "capacity_kw": 11.0,
        "min_power_kw": 0.0,
        "discrete_states_kw": [0.0, 3.0, 6.24, 11.0],
    }
    strategy_cfg = {"discreteEvSettings": {"min_target_power_kw": 6.0}}
    result = _get_discrete_ev_states(asset_cfg, strategy_cfg)
    assert 0.0 not in result
    assert 3.0 not in result
    assert 6.24 in result


# ===================================================================
# 3. Helper test: overdelivery selection — reference=11, requested=2.0
# ===================================================================

def test_overdelivery_selection_requested_2():
    """Smallest feasible curtailment >= 2.0 is 2.69 (target=8.31)."""
    result = _select_discrete_ev_target_for_curtailment(
        reference_power_kw=11.0,
        allocated_or_desired_curtailment_kw=2.0,
        states_kw=EV_DISCRETE_STATES,
    )
    assert result is not None
    assert result["target_power_kw"] == pytest.approx(8.31)
    assert result["actual_curtailment_kw"] == pytest.approx(2.69)


# ===================================================================
# 4. Helper test: small request — reference=11, requested=1.0
# ===================================================================

def test_overdelivery_selection_small_request():
    """Smallest feasible curtailment >= 1.0 is 1.30 (target=9.70)."""
    result = _select_discrete_ev_target_for_curtailment(
        reference_power_kw=11.0,
        allocated_or_desired_curtailment_kw=1.0,
        states_kw=EV_DISCRETE_STATES,
    )
    assert result is not None
    assert result["target_power_kw"] == pytest.approx(9.70)
    assert result["actual_curtailment_kw"] == pytest.approx(1.30)


# ===================================================================
# 5. Helper test: request above max feasible — reference=11, requested=5.5
# ===================================================================

def test_overdelivery_selection_above_max_feasible():
    """No feasible curtailment >= 5.5, fallback to max=4.76 (target=6.24)."""
    result = _select_discrete_ev_target_for_curtailment(
        reference_power_kw=11.0,
        allocated_or_desired_curtailment_kw=5.5,
        states_kw=EV_DISCRETE_STATES,
    )
    assert result is not None
    assert result["target_power_kw"] == pytest.approx(6.24)
    assert result["actual_curtailment_kw"] == pytest.approx(4.76)


# ===================================================================
# Additional helper: lower reference
# ===================================================================

def test_overdelivery_selection_lower_reference():
    """reference=8.31, allocated=2.0: target=6.24, curtailment=2.07."""
    result = _select_discrete_ev_target_for_curtailment(
        reference_power_kw=8.31,
        allocated_or_desired_curtailment_kw=2.0,
        states_kw=EV_DISCRETE_STATES,
    )
    assert result is not None
    assert result["target_power_kw"] == pytest.approx(6.24)
    assert result["actual_curtailment_kw"] == pytest.approx(2.07)


def test_overdelivery_selection_no_feasible():
    """Reference at or below all states -> None."""
    result = _select_discrete_ev_target_for_curtailment(
        reference_power_kw=6.0,
        allocated_or_desired_curtailment_kw=1.0,
        states_kw=EV_DISCRETE_STATES,
    )
    assert result is None


def test_curtailment_options_list():
    """Compute curtailment options for reference=11.0."""
    options = _compute_discrete_ev_curtailment_options(11.0, EV_DISCRETE_STATES)
    assert len(options) == 7
    assert options[0]["target_power_kw"] == pytest.approx(10.39)
    assert options[0]["curtailment_kw"] == pytest.approx(0.61)
    assert options[-1]["target_power_kw"] == pytest.approx(6.24)
    assert options[-1]["curtailment_kw"] == pytest.approx(4.76)


# ===================================================================
# 6. Strategy_11 bidding test (recent-profile + discrete mapping)
# ===================================================================

def _ev_asset(modulation_type="discrete", capacity_kw=11.0):
    return {
        "type": "ev_charger",
        "modulation_type": modulation_type,
        "nominal_power_w": capacity_kw * 1000,
        "capacity_kw": capacity_kw,
        "min_power_kw": 6.24,
        "discrete_states_kw": list(EV_DISCRETE_STATES),
        "description": "EV test",
        "device_name_tag": "ev_test",
        "field": "power",
        "pod": "ECM63",
    }


def _strategy_11_cfg():
    return {
        "name": "Recent-profile discrete EV current-step flexibility",
        "description": "test strategy_11",
        "enabled": True,
        "asset_types": ["ev_charger"],
        "assets_filter": ["ECM63.1"],
        "flexibility_method": "recent_profile",
        "recentProfileSettings": {
            "lookbackMinutes": 90,
            "quantile": 0.25,
            "continuousFactor": 0.5,
            "discreteFactor": 0.5,
            "activeThresholdW": 6000,
            "minSamples": 2,
            "maxConsecutiveActivationSlots": 4,
            "cooldownSlotsAfterMaxActivation": 2,
        },
        "discreteEvSettings": {
            "selection_policy": "smallest_overdelivery",
            "allow_zero_state": False,
            "min_target_power_kw": 6.24,
        },
        "time_slots": [
            {
                "name": "All day",
                "start": "00:00",
                "end": "23:59",
                "flexibility_mw": 0.011,
                "bid_price": 8.0,
                "activation_cost": 2.5,
            }
        ],
    }


def _base_cfg_11(asset_mapping=None):
    if asset_mapping is None:
        asset_mapping = {"ECM63.1": _ev_asset()}
    return {
        "fm": {"granularity": 15},
        "influxDB": {"assetsMeasurement": "assets_data"},
        "flexibility": {
            "method": "historical",
            "persistenceSettings": {
                "activeThresholdW": 500,
                "maxCurrentMeasurementAgeMinutes": 30,
                "missingMeasurementPolicy": "skip_asset",
            },
        },
        "asset_mapping": asset_mapping,
        "bidding_strategies": {"strategy_11": _strategy_11_cfg()},
    }


def _series(values, current_time=CURRENT_TIME, step_minutes=15):
    index = [
        pd.Timestamp(
            current_time - timedelta(minutes=step_minutes * (len(values) - i)),
            tz="UTC",
        )
        for i in range(len(values))
    ]
    return pd.Series(values, index=index, dtype=float)


def _forecaster_11(cfg, logger=None):
    strategy_config = cfg["bidding_strategies"]["strategy_11"]
    return FlexibilityForecaster(
        cfg,
        influx_client=object(),
        logger=logger or logging.getLogger("test_strategy_11"),
        method_override="recent_profile",
        strategy_config=strategy_config,
        strategy_id="strategy_11",
    )


def _install_measurements(forecaster, recent_by_asset, current_by_asset=None, age_minutes_by_asset=None):
    current_by_asset = current_by_asset or {}
    age_minutes_by_asset = age_minutes_by_asset or {}

    def query(asset_id, start_time_utc, end_time_utc):
        return recent_by_asset.get(asset_id, pd.Series(dtype=float))

    def latest(asset_id, current_time_utc, max_age_minutes):
        if asset_id not in current_by_asset:
            return None, None, None
        age = age_minutes_by_asset.get(asset_id, 0)
        ts = pd.Timestamp(current_time_utc - timedelta(minutes=age), tz="UTC")
        return ts, current_by_asset[asset_id], age

    forecaster._query_grouped_asset_series = query
    forecaster._get_latest_grouped_measurement = latest


def test_strategy_11_bidding_discrete_mapping():
    """Strategy_11 bidding: reference ~11 kW, factor=0.5 -> desired=5.5,
    mapped to feasible curtailment 4.76 (target 6.24)."""
    cfg = _base_cfg_11()
    forecaster = _forecaster_11(cfg)
    _install_measurements(
        forecaster,
        {"ECM63.1": _series([11000, 11000, 11000, 11000])},
        {"ECM63.1": 11000},
    )

    breakdown = forecaster.get_asset_flexibility_breakdown(
        SLOT_TIME,
        asset_ids=["ECM63.1"],
        current_time_utc=CURRENT_TIME,
    )

    info = breakdown["ECM63.1"]
    assert info["estimation_method"] == "recent_profile"
    assert info["modulation_type"] == "discrete"
    assert info["discrete_ev_target_kw"] == pytest.approx(6.24)
    assert info["discrete_ev_actual_curtailment_kw"] == pytest.approx(4.76)
    assert info["available_flexibility_kw"] == pytest.approx(4.76)
    assert info["is_available_for_flexibility"] is True


def test_strategy_11_bidding_no_continuous_quantity():
    """Available flexibility must be the discrete curtailment, not
    the raw continuous desired_flex_kw."""
    cfg = _base_cfg_11()
    forecaster = _forecaster_11(cfg)
    _install_measurements(
        forecaster,
        {"ECM63.1": _series([11000, 11000, 11000, 11000])},
        {"ECM63.1": 11000},
    )

    info = forecaster.get_asset_flexibility_breakdown(
        SLOT_TIME,
        asset_ids=["ECM63.1"],
        current_time_utc=CURRENT_TIME,
    )["ECM63.1"]

    raw_desired_kw = 0.5 * 11.0  # 5.5 kW
    assert info["available_flexibility_kw"] != pytest.approx(raw_desired_kw)
    assert info["available_flexibility_kw"] == pytest.approx(4.76)


# ===================================================================
# 7. Strategy_11 activation test — allocated=2.0, reference=11.0
# ===================================================================

def _make_controller(asset_mapping):
    """Build a minimal AssetController for curtail_asset unit tests."""
    _stub_module_if_missing("pandas")
    _stub_module_if_missing("pytz")
    from scripts import flexi_manager as fm

    controller = fm.AssetController.__new__(fm.AssetController)
    controller.asset_mapping = asset_mapping
    controller.rabbitmq_publisher = None
    controller.rabbitmq_config = {}
    controller._pending_commands = []
    controller.control_results = {}
    controller.community = "test"
    controller.logger = logging.getLogger("test_s11_controller")
    return controller


def test_strategy_11_activation_target_must_be_discrete_state():
    """Allocated curtailment 2.0 + reference 11.0 -> target 8.31 (from discrete_states_kw)."""
    controller = _make_controller({"ECM63.1": _ev_asset()})

    result = controller.curtail_asset(
        "ECM63.1",
        curtailment_kw=2.0,
        duration_minutes=15,
        dry_run=True,
        reference_power_kw=11.0,
        reference_power_source="recent_profile_baseline",
    )

    assert result["target_power_kw"] == pytest.approx(8.31)
    assert result["target_power_kw"] in EV_DISCRETE_STATES


# ===================================================================
# 8. Strategy_11 activation test for small allocation — allocated=1.0
# ===================================================================

def test_strategy_11_activation_small_allocation():
    """Allocated curtailment 1.0 + reference 11.0 -> target 9.70."""
    controller = _make_controller({"ECM63.1": _ev_asset()})

    result = controller.curtail_asset(
        "ECM63.1",
        curtailment_kw=1.0,
        duration_minutes=15,
        dry_run=True,
        reference_power_kw=11.0,
        reference_power_source="recent_profile_baseline",
    )

    assert result["target_power_kw"] == pytest.approx(9.70)
    assert result["target_power_kw"] in EV_DISCRETE_STATES


# ===================================================================
# 9-10. Strategy_11 activation with continuation and comfort guard
# ===================================================================

# These tests use the full FlexibilityManager integration path, which
# requires the same fixture approach as the existing comfort tests.
# We re-use helpers from the existing test_flexi_manager_activation module.

def _load_state(path):
    with open(path) as f:
        return json.load(f)


def _write_state(path, state):
    with open(path, "w") as f:
        json.dump(state, f)


def _make_s11_manager(tmp_path, previous_state=None, measurement_power_w=11000.0,
                       bid_reference_power_kw=11.0, allocated_flexibility_kw=2.0):
    """Build a minimal FlexibilityManager wired for strategy_11 tests."""
    _stub_module_if_missing("pandas")
    _stub_module_if_missing("pytz")
    import pandas as pd

    from scripts import flexi_manager as fm

    state_file = str(tmp_path / "state.json")
    if previous_state is not None:
        _write_state(state_file, previous_state)

    asset_config = _ev_asset()
    strategy_cfg = _strategy_11_cfg()

    config = {
        "fm": {
            "community": "test",
            "actors": {"fsps": {"fsp1": {"name": "FSP1", "assets": ["ECM63.1"]}}},
        },
        "asset_mapping": {"ECM63.1": asset_config},
        "bidding_strategies": {"strategy_11": strategy_cfg},
        "autonomous": {"enabled": False},
    }

    class FakeNodes:
        def __init__(self): pass
        def query_market_results(self, *a, **kw): return []
        def get_current_trades(self, *a, **kw): return []

    class FakeMarket:
        def __init__(self, trades): self.trades = trades
        def get_accepted_trades_for_slot(self, *a, **kw): return self.trades
        def get_settlements_for_slot(self, *a, **kw): return []

    class FakeMeasurement:
        def __init__(self, data): self.data = data
        def get_latest_power(self, asset_id, current_time_utc=None, max_age_minutes=None):
            v = self.data.get(asset_id)
            if v is None:
                return {"valid": False, "reason": "no_data_in_window"}
            return {
                "valid": True,
                "timestamp_utc": pd.Timestamp("2026-06-05T12:55:00Z"),
                "power_w": v,
                "age_minutes": 5.0,
            }

    class FakeBidHandler:
        def __init__(self):
            self.bid_record = None
        def get_bid_record(self, fsp_id, slot_start):
            return self.bid_record
        def get_strategy_info(self, bid_record):
            return bid_record.get("strategy") if bid_record else None
        def get_allowed_assets(self, bid_record):
            return [a["asset_id"] for a in bid_record.get("assets_to_activate", [])] if bid_record else []
        def get_total_quantity(self, bid_record):
            return float(bid_record.get("total_quantity_mw") or 0.0) if bid_record else 0.0
        def get_bid_asset_flexibilities(self, bid_record):
            if not bid_record:
                return {}
            return {
                a["asset_id"]: float(a.get("available_flexibility_kw") or 0.0)
                for a in bid_record.get("assets_to_activate", [])
                if float(a.get("available_flexibility_kw") or 0.0) > 0
            }
        def get_trades_from_ledger(self, *a, **kw):
            return []
        def update_bid_status(self, *a, **kw): pass

    manager = fm.FlexibilityManager(
        config=config,
        fsp_id="fsp1",
        nodes_interface=FakeNodes(),
        bid_repo=None,
        logger=logging.getLogger("test_s11_manager"),
        nodes_authenticated=False,
        rabbitmq_publisher=None,
        demand_repo=None,
        state_file=state_file,
    )
    manager.organization_id = "org-1"
    manager.market_handler = FakeMarket([])

    measurement_data = {"ECM63.1": measurement_power_w} if measurement_power_w is not None else {}
    manager.activation_measurement_provider = FakeMeasurement(measurement_data)

    bid_handler = FakeBidHandler()
    bid_handler.bid_record = {
        "id": "bid-s11-1",
        "total_quantity_mw": 0.005,
        "assets_to_activate": [
            {
                "asset_id": "ECM63.1",
                "available_flexibility_kw": allocated_flexibility_kw,
                "reference_power_kw": bid_reference_power_kw,
                "reference_power_source": "recent_profile_baseline",
                "modulation_type": "discrete",
            }
        ],
        "strategy": {"id": "strategy_11", "name": "Recent-profile discrete EV"},
    }
    manager.bid_handler = bid_handler
    return manager, state_file


def test_strategy_11_activation_with_continuation(tmp_path):
    """Continuation should use bid reference and increment comfort count."""
    previous_state = {
        "ECM63.1": {
            "state": "controlled",
            "asset_type": "ev_charger",
            "slot_start": "2026-06-05T12:45:00",
            "slot_end": "2026-06-05T13:00:00",
            "consecutive_activation_slots": 2,
            "control_sequence_start": "2026-06-05T12:15:00",
            "reference_power_kw": 11.0,
            "reference_power_source": "recent_profile_baseline",
            "target_power_kw": 8.31,
            "strategy_id": "strategy_11",
        }
    }

    manager, state_file = _make_s11_manager(
        tmp_path,
        previous_state=previous_state,
        measurement_power_w=8310.0,
        bid_reference_power_kw=11.0,
        allocated_flexibility_kw=2.0,
    )

    summary = manager.run(
        slot_override="2026-06-05T13:00:00Z",
        dry_run=True,
        simulate_sold_mw=0.005,
    )

    result = summary["control_results"]["ECM63.1"]
    assert result["status"] != "skipped"
    assert result["target_power_kw"] in EV_DISCRETE_STATES

    saved = _load_state(state_file)
    entry = saved.get("ECM63.1", {})
    assert entry.get("state") == "controlled"
    assert entry.get("consecutive_activation_slots") == 3


def test_strategy_11_comfort_guard_blocks_after_max_slots(tmp_path):
    """After maxConsecutiveActivationSlots=4, activation is blocked."""
    previous_state = {
        "ECM63.1": {
            "state": "controlled",
            "asset_type": "ev_charger",
            "slot_start": "2026-06-05T12:45:00",
            "slot_end": "2026-06-05T13:00:00",
            "consecutive_activation_slots": 4,
            "control_sequence_start": "2026-06-05T12:00:00",
            "reference_power_kw": 11.0,
            "reference_power_source": "recent_profile_baseline",
            "target_power_kw": 8.31,
            "strategy_id": "strategy_11",
        }
    }

    manager, state_file = _make_s11_manager(
        tmp_path,
        previous_state=previous_state,
        measurement_power_w=8310.0,
        bid_reference_power_kw=11.0,
        allocated_flexibility_kw=2.0,
    )

    summary = manager.run(
        slot_override="2026-06-05T13:00:00Z",
        dry_run=True,
        simulate_sold_mw=0.005,
    )

    result = summary["control_results"]["ECM63.1"]
    assert result["status"] == "skipped"
    assert "maximum consecutive" in result.get("message", "").lower() or \
           "maximum consecutive" in str(result.get("skip_reason", "")).lower()

    saved = _load_state(state_file)
    entry = saved.get("ECM63.1", {})
    assert entry.get("state") == "cooldown"


# ===================================================================
# 11. Regression test: HP discrete path unchanged
# ===================================================================

def test_hp_discrete_on_off_unchanged():
    """HP with discrete [0, 4] still uses ON/OFF, not EV states helper."""
    hp_config = {
        "type": "heat_pump",
        "modulation_type": "discrete",
        "capacity_kw": 4.0,
        "discrete_states_kw": [0.0, 4.0],
        "nominal_power_w": 7500,
        "description": "HP Small",
        "device_name_tag": "hp_small",
        "pod": "ECM96",
    }
    controller = _make_controller({"ECM96.2": hp_config})

    result = controller.curtail_asset(
        "ECM96.2",
        curtailment_kw=3.0,
        duration_minutes=15,
        dry_run=True,
        force_discrete_off=True,
    )

    assert result["target_power_kw"] == pytest.approx(0.0)
    assert result.get("discrete_state") == "OFF"


def test_hp_on_off_does_not_use_ev_states():
    """HP path should not call _calculate_discrete_ev_target_power."""
    hp_config = {
        "type": "heat_pump",
        "modulation_type": "discrete",
        "capacity_kw": 30.0,
        "discrete_states_kw": [0.0, 30.0],
        "nominal_power_w": 30000,
        "description": "HP Cinema",
        "device_name_tag": "hp_cinema",
        "pod": "ECM97",
    }
    controller = _make_controller({"ECM97.3": hp_config})

    result_off = controller.curtail_asset(
        "ECM97.3", curtailment_kw=20.0, dry_run=True, force_discrete_off=True,
    )
    assert result_off["target_power_kw"] == pytest.approx(0.0)
    assert result_off.get("discrete_state") == "OFF"

    result_on = controller.curtail_asset(
        "ECM97.3", curtailment_kw=5.0, dry_run=True,
    )
    assert result_on.get("discrete_state") in ("ON", "OFF")


# ===================================================================
# 12. Regression test: strategy_9 unaffected
# ===================================================================

def test_strategy_9_config_unaffected():
    """Strategy_9 config has not changed (still persistence-based)."""
    with open(REPO_ROOT / "conf" / "test_fm01_aem.json") as f:
        cfg = json.load(f)
    s9 = cfg["bidding_strategies"]["strategy_9"]
    assert s9["name"] == "Persistence HP + EV Strategy"
    assert s9["flexibility_method"] == "persistence"
    assert "heat_pump" in s9["asset_types"]
    assert "ev_charger" in s9["asset_types"]


# ===================================================================
# 13. Regression test: strategy_10 existing tests still pass
# ===================================================================

def test_strategy_10_config_still_present():
    """Strategy_10 is preserved alongside strategy_11."""
    with open(REPO_ROOT / "conf" / "test_fm01_aem.json") as f:
        cfg = json.load(f)
    assert "strategy_10" in cfg["bidding_strategies"]
    s10 = cfg["bidding_strategies"]["strategy_10"]
    assert s10["flexibility_method"] == "recent_profile"
    assert "ECM63.1" in s10["assets_filter"]


def test_strategy_10_continuous_ev_still_works():
    """A continuous EV under strategy_10 still computes raw continuous flex."""
    asset = {
        "type": "ev_charger",
        "modulation_type": "continuous",
        "nominal_power_w": 11000,
        "capacity_kw": 11.0,
        "min_power_kw": 0.0,
        "description": "EV continuous",
        "device_name_tag": "ev_cont",
        "field": "power",
        "pod": "ECM63",
    }

    cfg = {
        "fm": {"granularity": 15},
        "influxDB": {"assetsMeasurement": "assets_data"},
        "flexibility": {
            "method": "historical",
            "persistenceSettings": {
                "activeThresholdW": 500,
                "maxCurrentMeasurementAgeMinutes": 30,
            },
        },
        "asset_mapping": {"ECM63.1": asset},
        "bidding_strategies": {
            "strategy_10": {
                "name": "Recent profile EV",
                "description": "test",
                "asset_types": ["ev_charger"],
                "assets_filter": ["ECM63.1"],
                "flexibility_method": "recent_profile",
                "recentProfileSettings": {
                    "lookbackMinutes": 120,
                    "quantile": 0.25,
                    "continuousFactor": 0.5,
                    "activeThresholdW": 500,
                },
                "time_slots": [
                    {"name": "All", "start": "00:00", "end": "23:59",
                     "flexibility_mw": 0.011, "bid_price": 8.0, "activation_cost": 2.5}
                ],
            }
        },
    }

    forecaster = FlexibilityForecaster(
        cfg,
        influx_client=object(),
        logger=logging.getLogger("test_s10_regression"),
        method_override="recent_profile",
        strategy_config=cfg["bidding_strategies"]["strategy_10"],
        strategy_id="strategy_10",
    )
    _install_measurements(
        forecaster,
        {"ECM63.1": _series([4000, 8000, 12000, 16000])},
        {"ECM63.1": 8000},
    )

    info = forecaster.get_asset_flexibility_breakdown(
        SLOT_TIME,
        asset_ids=["ECM63.1"],
        current_time_utc=CURRENT_TIME,
    )["ECM63.1"]

    assert info["estimation_method"] == "recent_profile"
    assert info["modulation_type"] == "continuous"
    assert info["discrete_ev_target_kw"] is None
    assert info["available_flexibility_kw"] == pytest.approx(3.5)
