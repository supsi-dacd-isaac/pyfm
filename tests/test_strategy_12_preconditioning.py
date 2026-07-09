"""Tests for the Strategy-12 manager-owned preconditioning lifecycle (Step 3).

These tests are simulator-independent: they drive the lifecycle with a mocked
command controller, mocked market/bid state and a temporary state file, and
assert the desired ON/OFF decisions, issued commands and persisted ownership.
"""

import json
import logging
import math
import sys
import types
from datetime import datetime
from importlib.util import find_spec
from importlib.machinery import ModuleSpec
from pathlib import Path
from unittest.mock import MagicMock

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
    _psycopg2_missing = find_spec("psycopg2") is None
except ValueError:
    _psycopg2_missing = True
if _psycopg2_missing:
    _psycopg2_stub = types.ModuleType("psycopg2")
    _psycopg2_extras_stub = types.ModuleType("psycopg2.extras")
    _psycopg2_stub.__spec__ = ModuleSpec("psycopg2", loader=None)
    _psycopg2_extras_stub.__spec__ = ModuleSpec("psycopg2.extras", loader=None)
    _psycopg2_stub.extras = _psycopg2_extras_stub
    sys.modules["psycopg2"] = _psycopg2_stub
    sys.modules["psycopg2.extras"] = _psycopg2_extras_stub

from scripts import flexi_manager as fm  # noqa: E402
from classes.bidding_strategy import StrategyManager  # noqa: E402


CONFIG_PATH = REPO_ROOT / "conf" / "test_fm01_aem.json"

SCOPE_ASSETS = ["ECM62.10", "ECM68.3", "ECM162.1"]
CAPACITIES = {"ECM62.10": 36.0, "ECM68.3": 8.4, "ECM162.1": 6.0}
UNRELATED_ASSETS = ["ECM96.2", "ECM97.3", "ECM62.1", "ECM62.2", "ECM62.3"]


def _logger():
    logger = logging.getLogger("test_strategy_12_preconditioning")
    logger.addHandler(logging.NullHandler())
    return logger


def _load_config():
    with open(CONFIG_PATH, "r") as fh:
        return json.load(fh)


def _make_manager(tmp_path, fsp_strategy="strategy_12", rabbit=False):
    """Build a FlexibilityManager wired only with the pieces the lifecycle uses."""
    config = _load_config()

    # Ensure unrelated assets exist so scope-isolation assertions are meaningful.
    for asset_id in UNRELATED_ASSETS:
        config["asset_mapping"].setdefault(asset_id, {
            "type": "heat_pump",
            "modulation_type": "discrete",
            "capacity_kw": 5.0,
            "discrete_states_kw": [0.0, 5.0],
            "pod": asset_id.split(".")[0],
        })

    logger = _logger()
    manager = fm.FlexibilityManager.__new__(fm.FlexibilityManager)
    manager.config = config
    manager.logger = logger
    manager.fsp_id = "supsi02"
    manager.fsp_config = dict(config["fm"]["actors"]["fsps"]["supsi02"])
    if fsp_strategy is None:
        manager.fsp_config.pop("strategy", None)
    else:
        manager.fsp_config["strategy"] = fsp_strategy
    manager.asset_mapping = config["asset_mapping"]
    manager.strategy_manager = StrategyManager(config, logger)
    manager.controller = MagicMock()
    manager.controller.restore_asset.return_value = {"status": "simulated"}
    manager.controller.curtail_asset.return_value = {"status": "simulated"}
    manager.bid_handler = MagicMock()
    manager.market_handler = MagicMock()
    manager.organization_id = None
    manager.rabbitmq_publisher = None
    manager.bid_repo = MagicMock()
    manager.bid_repo.save_asset_activations_batch.return_value = 0
    manager.state_file = str(tmp_path / "flexi_manager_state.json")
    return manager


def _slot(hour, minute=0):
    return f"2026-01-13T{hour:02d}:{minute:02d}:00"


def _dt(hour, minute=0):
    return datetime(2026, 1, 13, hour, minute)


def _write_state(manager, state):
    with open(manager.state_file, "w") as fh:
        json.dump(state, fh)


def _read_state(manager):
    return manager._load_controlled_state()


def _owned_entry(strategy_id="strategy_12", state="prepared", last="ON"):
    return {
        "state": state,
        "owner": strategy_id,
        "strategy_id": strategy_id,
        "asset_type": "heat_pump",
        "last_commanded_state": last,
        "prepared_since": "2026-01-13T14:00:00",
        "slot_start": "2026-01-13T14:00:00",
        "slot_end": "2026-01-13T14:15:00",
    }


def _enable_weather_gate(
    manager,
    *,
    threshold=24.0,
    aggregation="max",
    missing_policy="skip_preconditioning",
    evaluation_start="17:00",
    evaluation_end="20:00",
    forecast_type="constant",
    forecast_value=28.0,
    forecast_file=None,
):
    strategy_cfg = manager.config["bidding_strategies"]["strategy_12"]
    strategy_cfg["weatherGateSettings"] = {
        "enabled": True,
        "source": "flexibility.temperature.forecast",
        "temperatureThresholdC": threshold,
        "evaluationStart": evaluation_start,
        "evaluationEnd": evaluation_end,
        "aggregation": aggregation,
        "missingForecastPolicy": missing_policy,
    }
    temp_cfg = manager.config["flexibility"]["temperature"]
    temp_cfg["enabled"] = True
    forecast = {"type": forecast_type}
    if forecast_type == "constant":
        forecast["value"] = forecast_value
    if forecast_file is not None:
        forecast["file"] = str(forecast_file)
    temp_cfg["forecast"] = forecast


def _write_weather_forecast(path, entries):
    with open(path, "w") as fh:
        json.dump({"forecast": entries}, fh)


def _off_calls(manager):
    return {c.args[0] for c in manager.controller.curtail_asset.call_args_list}


def _on_calls(manager):
    return {c.args[0] for c in manager.controller.restore_asset.call_args_list}


# ---------------------------------------------------------------------------
# Active-strategy resolution
# ---------------------------------------------------------------------------

def test_context_active_when_fsp_uses_strategy_12(tmp_path):
    manager = _make_manager(tmp_path)
    ctx = manager._resolve_preconditioning_context()
    assert ctx is not None
    assert ctx["strategy_id"] == "strategy_12"
    assert ctx["owner_tag"] == "strategy_12"
    assert set(ctx["scope_assets"]) == set(SCOPE_ASSETS)


def test_context_inactive_when_fsp_has_no_strategy(tmp_path):
    manager = _make_manager(tmp_path, fsp_strategy=None)
    assert manager._resolve_preconditioning_context() is None


def test_strategy_existing_but_not_active_does_not_prepare(tmp_path):
    # strategy_12 exists in config, but this FSP uses a different strategy.
    manager = _make_manager(tmp_path, fsp_strategy="strategy_4")
    assert manager._resolve_preconditioning_context() is None


def test_cli_fallback_strategy_override_activates(tmp_path):
    manager = _make_manager(tmp_path, fsp_strategy=None)
    ctx = manager._resolve_preconditioning_context(fallback_strategy="strategy_12")
    assert ctx is not None and ctx["strategy_id"] == "strategy_12"


@pytest.mark.parametrize("other", ["strategy_8", "strategy_9", "strategy_10", "strategy_11"])
def test_other_strategies_do_not_enter_lifecycle(tmp_path, other):
    manager = _make_manager(tmp_path, fsp_strategy=other)
    assert manager._resolve_preconditioning_context() is None


# ---------------------------------------------------------------------------
# Settings validation
# ---------------------------------------------------------------------------

def test_disabled_settings_return_none(tmp_path):
    manager = _make_manager(tmp_path)
    raw = {"enabled": False, "prepareStart": "14:00",
           "flexibilityStart": "17:00", "maintainUntil": "20:00"}
    assert manager._parse_preconditioning_settings(raw, "strategy_12") is None


def test_invalid_time_order_raises(tmp_path):
    manager = _make_manager(tmp_path)
    raw = {"enabled": True, "prepareStart": "17:00",
           "flexibilityStart": "14:00", "maintainUntil": "20:00"}
    with pytest.raises(ValueError):
        manager._parse_preconditioning_settings(raw, "strategy_12")


def test_invalid_release_action_raises(tmp_path):
    manager = _make_manager(tmp_path)
    raw = {"enabled": True, "prepareStart": "14:00", "flexibilityStart": "17:00",
           "maintainUntil": "20:00", "releaseAction": "restore"}
    with pytest.raises(ValueError):
        manager._parse_preconditioning_settings(raw, "strategy_12")


def test_non_boolean_enabled_raises(tmp_path):
    manager = _make_manager(tmp_path)
    raw = {"enabled": "yes", "prepareStart": "14:00",
           "flexibilityStart": "17:00", "maintainUntil": "20:00"}
    with pytest.raises(ValueError):
        manager._parse_preconditioning_settings(raw, "strategy_12")


def test_weather_gate_disabled_bypasses_lifecycle_gate(tmp_path):
    manager = _make_manager(tmp_path)
    settings = manager._resolve_preconditioning_context()["weather_gate_settings"]
    assert settings["enabled"] is False

    summary = manager.run(slot_override=_slot(14, 0), dry_run=True)

    assert _on_calls(manager) == set(SCOPE_ASSETS)
    assert _pc(summary)["weather_gate"]["decision"] == "open"
    assert _pc(summary)["weather_gate"]["reason"] == "disabled"


def test_invalid_weather_gate_settings_raise(tmp_path):
    manager = _make_manager(tmp_path)
    pc_settings = manager._resolve_preconditioning_context()["settings"]
    with pytest.raises(ValueError):
        manager._parse_preconditioning_weather_gate_settings(
            {
                "enabled": True,
                "temperatureThresholdC": "hot",
                "evaluationStart": "17:00",
                "evaluationEnd": "20:00",
            },
            "strategy_12",
            pc_settings,
        )
    with pytest.raises(ValueError):
        manager._parse_preconditioning_weather_gate_settings(
            {
                "enabled": True,
                "temperatureThresholdC": 24.0,
                "evaluationStart": "20:00",
                "evaluationEnd": "17:00",
            },
            "strategy_12",
            pc_settings,
        )
    with pytest.raises(ValueError):
        manager._parse_preconditioning_weather_gate_settings(
            {
                "enabled": True,
                "temperatureThresholdC": 24.0,
                "evaluationStart": "17:00",
                "evaluationEnd": "20:00",
                "aggregation": "median",
            },
            "strategy_12",
            pc_settings,
        )
    with pytest.raises(ValueError):
        manager._parse_preconditioning_weather_gate_settings(
            {
                "enabled": True,
                "temperatureThresholdC": 24.0,
                "evaluationStart": "17:00",
                "evaluationEnd": "20:00",
                "missingForecastPolicy": "prepare_anyway",
            },
            "strategy_12",
            pc_settings,
        )


# ---------------------------------------------------------------------------
# Phase detection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("hour,minute,expected", [
    (13, 59, "idle"),
    (14, 0, "prepare"),
    (16, 59, "prepare"),
    (17, 0, "maintain"),
    (19, 59, "maintain"),
    (20, 0, "release"),
    (21, 0, "release"),
    (2, 0, "idle"),
])
def test_phase_boundaries(tmp_path, hour, minute, expected):
    manager = _make_manager(tmp_path)
    settings = manager._resolve_preconditioning_context()["settings"]
    assert manager._resolve_preconditioning_phase(_dt(hour, minute), settings) == expected


# ---------------------------------------------------------------------------
# Desired-state oracle (truth table)
# ---------------------------------------------------------------------------

def test_oracle_prepare_all_on(tmp_path):
    manager = _make_manager(tmp_path)
    desired = manager._compute_preconditioning_desired_state(
        "prepare", SCOPE_ASSETS, set(), {}, "strategy_12")
    assert desired == {a: "ON" for a in SCOPE_ASSETS}


def test_oracle_maintain_selected_off_rest_on(tmp_path):
    manager = _make_manager(tmp_path)
    desired = manager._compute_preconditioning_desired_state(
        "maintain", SCOPE_ASSETS, {"ECM62.10", "ECM162.1"}, {}, "strategy_12")
    assert desired == {"ECM62.10": "OFF", "ECM162.1": "OFF", "ECM68.3": "ON"}


def test_oracle_maintain_no_activation_all_on(tmp_path):
    manager = _make_manager(tmp_path)
    desired = manager._compute_preconditioning_desired_state(
        "maintain", SCOPE_ASSETS, set(), {}, "strategy_12")
    assert desired == {a: "ON" for a in SCOPE_ASSETS}


def test_oracle_release_owned_off_unowned_untouched(tmp_path):
    manager = _make_manager(tmp_path)
    previous = {
        "ECM62.10": _owned_entry(state="controlled", last="OFF"),
        "ECM68.3": _owned_entry(state="prepared", last="ON"),
        # ECM162.1 not owned -> untouched
    }
    desired = manager._compute_preconditioning_desired_state(
        "release", SCOPE_ASSETS, set(), previous, "strategy_12")
    assert desired == {"ECM62.10": "OFF", "ECM68.3": "OFF"}


def test_oracle_idle_no_ownership_untouched(tmp_path):
    manager = _make_manager(tmp_path)
    desired = manager._compute_preconditioning_desired_state(
        "idle", SCOPE_ASSETS, set(), {}, "strategy_12")
    assert desired == {}


# ---------------------------------------------------------------------------
# Preparation phase (14:00)
# ---------------------------------------------------------------------------

def test_preparation_all_three_on_and_owned(tmp_path):
    manager = _make_manager(tmp_path)
    summary = manager.run(slot_override=_slot(14, 0), dry_run=True)

    assert _on_calls(manager) == set(SCOPE_ASSETS)
    assert manager.controller.curtail_asset.call_count == 0
    state = _read_state(manager)
    for asset_id in SCOPE_ASSETS:
        assert state[asset_id]["state"] == "prepared"
        assert state[asset_id]["owner"] == "strategy_12"
        assert state[asset_id]["last_commanded_state"] == "ON"
    assert summary["preconditioning"]["phase"] == "prepare"


# ---------------------------------------------------------------------------
# Step 4 weather gate
# ---------------------------------------------------------------------------

def test_weather_gate_temperature_above_threshold_opens_preparation(tmp_path):
    manager = _make_manager(tmp_path)
    _enable_weather_gate(manager, threshold=24.0, forecast_value=28.0)

    summary = manager.run(slot_override=_slot(14, 0), dry_run=True)

    assert _on_calls(manager) == set(SCOPE_ASSETS)
    state = _read_state(manager)
    for asset_id in SCOPE_ASSETS:
        assert state[asset_id]["owner"] == "strategy_12"
    gate = _pc(summary)["weather_gate"]
    assert gate["decision"] == "open"
    assert gate["reason"] == "threshold_met"
    assert gate["aggregated_temperature_c"] == pytest.approx(28.0)


def test_weather_gate_temperature_below_threshold_skips_preparation(tmp_path):
    manager = _make_manager(tmp_path)
    _enable_weather_gate(manager, threshold=24.0, forecast_value=22.0)

    summary = manager.run(slot_override=_slot(14, 0), dry_run=True)

    assert manager.controller.restore_asset.call_count == 0
    assert manager.controller.curtail_asset.call_count == 0
    state = _read_state(manager)
    for asset_id in SCOPE_ASSETS:
        assert asset_id not in state
    gate = _pc(summary)["weather_gate"]
    assert gate["decision"] == "closed"
    assert gate["reason"] == "threshold_not_met"


def test_weather_gate_temperature_equal_threshold_opens(tmp_path):
    manager = _make_manager(tmp_path)
    _enable_weather_gate(manager, threshold=24.0, forecast_value=24.0)

    summary = manager.run(slot_override=_slot(14, 0), dry_run=True)

    assert _on_calls(manager) == set(SCOPE_ASSETS)
    assert _pc(summary)["weather_gate"]["decision"] == "open"


def test_weather_gate_missing_forecast_skips_preconditioning(tmp_path):
    manager = _make_manager(tmp_path)
    missing_path = tmp_path / "missing_temperature_forecast.json"
    _enable_weather_gate(
        manager,
        threshold=24.0,
        forecast_type="file",
        forecast_file=missing_path,
    )

    summary = manager.run(slot_override=_slot(14, 0), dry_run=True)

    assert manager.controller.restore_asset.call_count == 0
    assert _pc(summary)["weather_gate"]["decision"] == "closed"
    assert _pc(summary)["weather_gate"]["reason"] == "missing_forecast"


def test_weather_gate_invalid_forecast_values_do_not_open(tmp_path):
    manager = _make_manager(tmp_path)
    forecast_path = tmp_path / "invalid_temperature_forecast.json"
    _write_weather_forecast(forecast_path, [
        {"timestamp": "2026-01-13T17:00:00", "temperature": None},
        {"timestamp": "2026-01-13T17:15:00", "temperature": math.nan},
        {"timestamp": "2026-01-13T17:30:00", "temperature": "hot"},
    ])
    _enable_weather_gate(
        manager,
        threshold=24.0,
        forecast_type="file",
        forecast_file=forecast_path,
    )

    summary = manager.run(slot_override=_slot(14, 0), dry_run=True)

    assert manager.controller.restore_asset.call_count == 0
    assert _pc(summary)["weather_gate"]["decision"] == "closed"
    assert _pc(summary)["weather_gate"]["reason"] == "invalid_forecast"


def test_weather_gate_uses_configured_evaluation_window(tmp_path):
    manager = _make_manager(tmp_path)
    forecast_path = tmp_path / "window_temperature_forecast.json"
    _write_weather_forecast(forecast_path, [
        {"timestamp": "2026-01-13T16:45:00", "temperature": 30.0},
        {"timestamp": "2026-01-13T17:00:00", "temperature": 21.0},
        {"timestamp": "2026-01-13T18:00:00", "temperature": 22.0},
        {"timestamp": "2026-01-13T20:00:00", "temperature": 31.0},
    ])
    _enable_weather_gate(
        manager,
        threshold=24.0,
        forecast_type="file",
        forecast_file=forecast_path,
    )

    summary = manager.run(slot_override=_slot(14, 0), dry_run=True)

    gate = _pc(summary)["weather_gate"]
    assert gate["decision"] == "closed"
    assert gate["aggregated_temperature_c"] == pytest.approx(22.0)
    assert manager.controller.restore_asset.call_count == 0


def test_weather_gate_max_aggregation_uses_maximum(tmp_path):
    manager = _make_manager(tmp_path)
    forecast_path = tmp_path / "max_temperature_forecast.json"
    _write_weather_forecast(forecast_path, [
        {"timestamp": "2026-01-13T17:00:00", "temperature": 20.0},
        {"timestamp": "2026-01-13T18:00:00", "temperature": 28.0},
        {"timestamp": "2026-01-13T19:00:00", "temperature": 21.0},
    ])
    _enable_weather_gate(
        manager,
        threshold=24.0,
        forecast_type="file",
        forecast_file=forecast_path,
    )

    summary = manager.run(slot_override=_slot(14, 0), dry_run=True)

    gate = _pc(summary)["weather_gate"]
    assert gate["decision"] == "open"
    assert gate["aggregation"] == "max"
    assert gate["aggregated_temperature_c"] == pytest.approx(28.0)
    assert _on_calls(manager) == set(SCOPE_ASSETS)


def test_weather_gate_mean_aggregation_when_configured(tmp_path):
    manager = _make_manager(tmp_path)
    forecast_path = tmp_path / "mean_temperature_forecast.json"
    _write_weather_forecast(forecast_path, [
        {"timestamp": "2026-01-13T17:00:00", "temperature": 20.0},
        {"timestamp": "2026-01-13T18:00:00", "temperature": 28.0},
        {"timestamp": "2026-01-13T19:00:00", "temperature": 21.0},
    ])
    _enable_weather_gate(
        manager,
        threshold=24.0,
        aggregation="mean",
        forecast_type="file",
        forecast_file=forecast_path,
    )

    summary = manager.run(slot_override=_slot(14, 0), dry_run=True)

    gate = _pc(summary)["weather_gate"]
    assert gate["decision"] == "closed"
    assert gate["aggregation"] == "mean"
    assert gate["aggregated_temperature_c"] == pytest.approx(23.0)


def test_weather_gate_open_daily_decision_is_stable(tmp_path):
    manager = _make_manager(tmp_path)
    _enable_weather_gate(manager, threshold=24.0, forecast_value=28.0)
    first = manager.run(slot_override=_slot(14, 0), dry_run=True)
    assert _pc(first)["weather_gate"]["decision"] == "open"

    manager.controller.reset_mock()
    manager.config["flexibility"]["temperature"]["forecast"]["value"] = 18.0
    second = manager.run(slot_override=_slot(14, 15), dry_run=True)

    assert manager.controller.restore_asset.call_count == 0
    assert manager.controller.curtail_asset.call_count == 0
    assert _pc(second)["weather_gate"]["decision"] == "open"
    state = _read_state(manager)
    for asset_id in SCOPE_ASSETS:
        assert state[asset_id]["owner"] == "strategy_12"


def test_weather_gate_closed_daily_decision_does_not_start_later(tmp_path):
    manager = _make_manager(tmp_path)
    _enable_weather_gate(manager, threshold=24.0, forecast_value=20.0)
    first = manager.run(slot_override=_slot(14, 0), dry_run=True)
    assert _pc(first)["weather_gate"]["decision"] == "closed"

    manager.controller.reset_mock()
    manager.config["flexibility"]["temperature"]["forecast"]["value"] = 30.0
    second = manager.run(slot_override=_slot(14, 15), dry_run=True)

    assert manager.controller.restore_asset.call_count == 0
    assert manager.controller.curtail_asset.call_count == 0
    assert _pc(second)["weather_gate"]["decision"] == "closed"
    state = _read_state(manager)
    for asset_id in SCOPE_ASSETS:
        assert asset_id not in state


def test_weather_gate_open_maintain_behavior_unchanged(tmp_path):
    manager = _make_manager(tmp_path)
    _enable_weather_gate(manager, threshold=24.0, forecast_value=28.0)
    manager.run(slot_override=_slot(14, 0), dry_run=True)
    manager.controller.reset_mock()
    _wire_delivery(manager, {"ECM62.10": 36.0, "ECM68.3": 8.4, "ECM162.1": 6.0})

    summary = manager.run(slot_override=_slot(17, 0), dry_run=True, simulate_sold_mw=0.042)

    assert _pc(summary)["weather_gate"]["decision"] == "open"
    assert _off_calls(manager) == {"ECM62.10", "ECM162.1"}
    assert _on_calls(manager) == set()
    state = _read_state(manager)
    assert state["ECM62.10"]["state"] == "controlled"
    assert state["ECM162.1"]["state"] == "controlled"
    assert state["ECM68.3"]["state"] == "prepared"


def test_weather_gate_closed_maintain_does_not_deliver_unprepared_assets(tmp_path):
    manager = _make_manager(tmp_path)
    _enable_weather_gate(manager, threshold=24.0, forecast_value=20.0)
    manager.run(slot_override=_slot(14, 0), dry_run=True)
    manager.controller.reset_mock()
    _wire_delivery(manager, {"ECM62.10": 36.0, "ECM68.3": 8.4, "ECM162.1": 6.0})

    summary = manager.run(slot_override=_slot(17, 0), dry_run=True, simulate_sold_mw=0.042)

    assert manager.controller.restore_asset.call_count == 0
    assert manager.controller.curtail_asset.call_count == 0
    assert manager.bid_repo.save_asset_activations_batch.call_count == 0
    assert _pc(summary)["weather_gate"]["decision"] == "closed"
    assert _pc(summary)["delivery_source"] == "weather_gate_closed"


def test_weather_gate_open_day_release_unchanged(tmp_path):
    manager = _make_manager(tmp_path)
    _enable_weather_gate(manager, threshold=24.0, forecast_value=28.0)
    manager.run(slot_override=_slot(14, 0), dry_run=True)
    manager.controller.reset_mock()

    summary = manager.run(slot_override=_slot(20, 0), dry_run=True)

    assert _off_calls(manager) == set(SCOPE_ASSETS)
    state = _read_state(manager)
    for asset_id in SCOPE_ASSETS:
        assert asset_id not in state
    assert _pc(summary)["failed_commands"] == 0


def test_weather_gate_active_strategy_isolation(tmp_path):
    manager = _make_manager(tmp_path, fsp_strategy="strategy_11")
    _enable_weather_gate(manager, threshold=24.0, forecast_value=28.0)
    assert manager._resolve_preconditioning_context() is None


def test_weather_gate_open_asset_scope_isolation(tmp_path):
    manager = _make_manager(tmp_path)
    _enable_weather_gate(manager, threshold=24.0, forecast_value=28.0)

    manager.run(slot_override=_slot(14, 0), dry_run=True)

    touched = _on_calls(manager) | _off_calls(manager)
    assert touched == set(SCOPE_ASSETS)
    for asset_id in UNRELATED_ASSETS:
        assert asset_id not in touched


# ---------------------------------------------------------------------------
# Exact asset scope
# ---------------------------------------------------------------------------

def test_unrelated_assets_never_touched(tmp_path):
    manager = _make_manager(tmp_path)
    # Seed unrelated assets as (foreign) controlled entries in the state file.
    _write_state(manager, {
        "ECM96.2": {"state": "controlled", "owner": "other", "asset_type": "heat_pump"},
        "ECM97.3": {"state": "controlled", "owner": "other", "asset_type": "heat_pump"},
    })
    manager.run(slot_override=_slot(14, 0), dry_run=True)

    touched = _on_calls(manager) | _off_calls(manager)
    for asset_id in UNRELATED_ASSETS:
        assert asset_id not in touched
    # Foreign state entries are preserved, not clobbered.
    state = _read_state(manager)
    assert state["ECM96.2"]["owner"] == "other"
    assert state["ECM97.3"]["owner"] == "other"


# ---------------------------------------------------------------------------
# Maintained-flexibility phase
# ---------------------------------------------------------------------------

def _wire_delivery(manager, planned):
    manager.bid_handler.get_bid_record.return_value = {"id": 1}
    manager.bid_handler.get_bid_asset_flexibilities.return_value = planned


def test_partial_activation_selected_off_rest_on(tmp_path):
    manager = _make_manager(tmp_path)
    _wire_delivery(manager, {"ECM62.10": 36.0, "ECM68.3": 8.4, "ECM162.1": 6.0})
    # 42 kW accepted -> discrete allocator selects {ECM62.10, ECM162.1}.
    manager.run(slot_override=_slot(17, 0), dry_run=True, simulate_sold_mw=0.042)

    assert _off_calls(manager) == {"ECM62.10", "ECM162.1"}
    assert _on_calls(manager) == {"ECM68.3"}
    state = _read_state(manager)
    assert state["ECM62.10"]["state"] == "controlled"
    assert state["ECM162.1"]["state"] == "controlled"
    assert state["ECM68.3"]["state"] == "prepared"


def test_no_activation_slot_all_on(tmp_path):
    manager = _make_manager(tmp_path)
    manager.bid_handler.get_bid_record.return_value = None
    manager.run(slot_override=_slot(18, 30), dry_run=True)

    assert _on_calls(manager) == set(SCOPE_ASSETS)
    assert manager.controller.curtail_asset.call_count == 0
    state = _read_state(manager)
    for asset_id in SCOPE_ASSETS:
        assert state[asset_id]["state"] == "prepared"


def test_consecutive_slots_switch_selection(tmp_path):
    manager = _make_manager(tmp_path)
    _wire_delivery(manager, {"ECM62.10": 36.0, "ECM68.3": 8.4, "ECM162.1": 6.0})

    # Slot N: 42 kW -> {ECM62.10, ECM162.1} OFF, ECM68.3 ON.
    manager.run(slot_override=_slot(17, 0), dry_run=True, simulate_sold_mw=0.042)
    assert _off_calls(manager) == {"ECM62.10", "ECM162.1"}

    # Slot N+1: reuse same manager/state; 8.4 kW -> {ECM68.3} OFF, others ON.
    manager.controller.reset_mock()
    manager.run(slot_override=_slot(17, 15), dry_run=True, simulate_sold_mw=0.0084)

    assert _off_calls(manager) == {"ECM68.3"}
    assert _on_calls(manager) == {"ECM62.10", "ECM162.1"}
    state = _read_state(manager)
    assert state["ECM68.3"]["state"] == "controlled"
    assert state["ECM62.10"]["state"] == "prepared"
    assert state["ECM162.1"]["state"] == "prepared"


def test_no_activation_after_activation_returns_on(tmp_path):
    manager = _make_manager(tmp_path)
    _wire_delivery(manager, {"ECM62.10": 36.0, "ECM68.3": 8.4, "ECM162.1": 6.0})

    # 17:00 ECM62.10 delivered OFF.
    manager.run(slot_override=_slot(17, 0), dry_run=True, simulate_sold_mw=0.036)
    assert "ECM62.10" in _off_calls(manager)

    # 17:15 no activation -> ECM62.10 returns ON.
    manager.controller.reset_mock()
    manager.bid_handler.get_bid_record.return_value = None
    manager.run(slot_override=_slot(17, 15), dry_run=True)
    assert "ECM62.10" in _on_calls(manager)
    assert _read_state(manager)["ECM62.10"]["state"] == "prepared"


# ---------------------------------------------------------------------------
# Window-end release + restore race
# ---------------------------------------------------------------------------

def test_release_switches_all_owned_off_and_clears_ownership(tmp_path):
    manager = _make_manager(tmp_path)
    _write_state(manager, {
        "ECM62.10": _owned_entry(state="prepared", last="ON"),
        "ECM68.3": _owned_entry(state="controlled", last="OFF"),
        "ECM162.1": _owned_entry(state="prepared", last="ON"),
    })
    manager.run(slot_override=_slot(20, 0), dry_run=True)

    assert _off_calls(manager) == set(SCOPE_ASSETS)
    # Release must never turn a Strategy-12 asset back ON in the same run.
    assert manager.controller.restore_asset.call_count == 0
    state = _read_state(manager)
    for asset_id in SCOPE_ASSETS:
        assert asset_id not in state


def test_failed_release_retains_ownership(tmp_path):
    manager = _make_manager(tmp_path)
    manager.controller.curtail_asset.return_value = {"status": "error"}
    _write_state(manager, {"ECM62.10": _owned_entry(state="prepared", last="ON")})

    summary = manager.run(slot_override=_slot(20, 0), dry_run=True)

    state = _read_state(manager)
    assert state["ECM62.10"]["owner"] == "strategy_12"
    assert state["ECM62.10"]["last_commanded_state"] == "ON"
    assert _pc(summary)["failed_commands"] == 1
    assert _pc(summary)["command_results"] == [{
        "asset_id": "ECM62.10",
        "category": "release",
        "desired": "OFF",
        "status": "error",
    }]
    assert summary["status"] == "activation_failed"


def test_successful_release_clears_ownership(tmp_path):
    manager = _make_manager(tmp_path)
    manager.controller.curtail_asset.return_value = {"status": "success"}
    _write_state(manager, {"ECM62.10": _owned_entry(state="prepared", last="ON")})

    summary = manager.run(slot_override=_slot(20, 0), dry_run=True)

    assert "ECM62.10" not in _read_state(manager)
    assert _pc(summary)["successful_commands"] == 1
    assert _pc(summary)["failed_commands"] == 0


def test_queued_release_clears_ownership(tmp_path):
    manager = _make_manager(tmp_path)
    manager.controller.curtail_asset.return_value = {"status": "queued"}
    _write_state(manager, {"ECM62.10": _owned_entry(state="prepared", last="ON")})

    summary = manager.run(slot_override=_slot(20, 0), dry_run=True)

    assert "ECM62.10" not in _read_state(manager)
    assert _pc(summary)["successful_commands"] == 1
    assert _pc(summary)["failed_commands"] == 0


def test_simulated_release_clears_ownership(tmp_path):
    manager = _make_manager(tmp_path)
    _write_state(manager, {"ECM62.10": _owned_entry(state="prepared", last="ON")})

    summary = manager.run(slot_override=_slot(20, 0), dry_run=True)

    assert "ECM62.10" not in _read_state(manager)
    assert _pc(summary)["command_results"][0]["status"] == "simulated"
    assert _pc(summary)["successful_commands"] == 1


def test_partial_release_failure_preserves_only_failed_asset(tmp_path):
    manager = _make_manager(tmp_path)

    def curtail(asset_id, *args, **kwargs):
        return {"status": "error"} if asset_id == "ECM68.3" else {"status": "success"}

    manager.controller.curtail_asset.side_effect = curtail
    _write_state(manager, {a: _owned_entry(state="prepared", last="ON") for a in SCOPE_ASSETS})

    summary = manager.run(slot_override=_slot(20, 0), dry_run=True)

    state = _read_state(manager)
    assert "ECM62.10" not in state
    assert "ECM162.1" not in state
    assert state["ECM68.3"]["owner"] == "strategy_12"
    assert _pc(summary)["successful_commands"] == 2
    assert _pc(summary)["failed_commands"] == 1
    assert summary["status"] == "partial_success"


def test_next_run_retries_failed_release_cleanup(tmp_path):
    manager = _make_manager(tmp_path)
    manager.controller.curtail_asset.return_value = {"status": "error"}
    _write_state(manager, {"ECM62.10": _owned_entry(state="prepared", last="ON")})

    manager.run(slot_override=_slot(20, 0), dry_run=True)
    assert "ECM62.10" in _read_state(manager)

    manager.controller.reset_mock()
    manager.controller.curtail_asset.return_value = {"status": "success"}
    summary = manager.run(slot_override=_slot(20, 15), dry_run=True)

    assert _off_calls(manager) == {"ECM62.10"}
    assert "ECM62.10" not in _read_state(manager)
    assert _pc(summary)["successful_commands"] == 1


def test_release_after_20_is_noop_without_ownership(tmp_path):
    manager = _make_manager(tmp_path)
    manager.run(slot_override=_slot(20, 15), dry_run=True)
    assert manager.controller.restore_asset.call_count == 0
    assert manager.controller.curtail_asset.call_count == 0
    assert _read_state(manager) == {}


# ---------------------------------------------------------------------------
# Idle release of stale ownership
# ---------------------------------------------------------------------------

def test_idle_releases_stale_ownership(tmp_path):
    manager = _make_manager(tmp_path)
    _write_state(manager, {"ECM62.10": _owned_entry(state="prepared", last="ON")})
    manager.run(slot_override=_slot(13, 0), dry_run=True)
    assert _off_calls(manager) == {"ECM62.10"}
    assert "ECM62.10" not in _read_state(manager)


def test_failed_idle_cleanup_retains_ownership(tmp_path):
    manager = _make_manager(tmp_path)
    manager.controller.curtail_asset.return_value = {"status": "error"}
    _write_state(manager, {"ECM62.10": _owned_entry(state="prepared", last="ON")})

    summary = manager.run(slot_override=_slot(13, 0), dry_run=True)

    state = _read_state(manager)
    assert state["ECM62.10"]["owner"] == "strategy_12"
    assert _pc(summary)["failed_commands"] == 1
    assert summary["status"] == "activation_failed"


def test_successful_idle_cleanup_clears_ownership(tmp_path):
    manager = _make_manager(tmp_path)
    manager.controller.curtail_asset.return_value = {"status": "success"}
    _write_state(manager, {"ECM62.10": _owned_entry(state="prepared", last="ON")})

    summary = manager.run(slot_override=_slot(13, 0), dry_run=True)

    assert "ECM62.10" not in _read_state(manager)
    assert _pc(summary)["successful_commands"] == 1


def test_cleanup_commands_do_not_write_activation_records(tmp_path):
    release_tmp = tmp_path / "release"
    release_tmp.mkdir()
    release_manager = _make_manager(release_tmp)
    _write_state(release_manager, {"ECM62.10": _owned_entry(state="prepared", last="ON")})
    release_manager.run(slot_override=_slot(20, 0), dry_run=True)
    assert release_manager.bid_repo.save_asset_activations_batch.call_count == 0

    idle_tmp = tmp_path / "idle"
    idle_tmp.mkdir()
    idle_manager = _make_manager(idle_tmp)
    _write_state(idle_manager, {"ECM62.10": _owned_entry(state="prepared", last="ON")})
    idle_manager.run(slot_override=_slot(13, 0), dry_run=True)
    assert idle_manager.bid_repo.save_asset_activations_batch.call_count == 0


def test_idle_without_ownership_is_noop(tmp_path):
    manager = _make_manager(tmp_path)
    manager.run(slot_override=_slot(10, 0), dry_run=True)
    assert manager.controller.restore_asset.call_count == 0
    assert manager.controller.curtail_asset.call_count == 0


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

def test_idempotent_prepared_on_no_recommand(tmp_path):
    manager = _make_manager(tmp_path)
    _write_state(manager, {a: _owned_entry(state="prepared", last="ON") for a in SCOPE_ASSETS})
    manager.run(slot_override=_slot(15, 0), dry_run=True)
    assert manager.controller.restore_asset.call_count == 0
    assert manager.controller.curtail_asset.call_count == 0
    # Ownership preserved.
    state = _read_state(manager)
    for asset_id in SCOPE_ASSETS:
        assert state[asset_id]["state"] == "prepared"


def test_idempotent_controlled_off_no_recommand(tmp_path):
    manager = _make_manager(tmp_path)
    _wire_delivery(manager, {"ECM62.10": 36.0, "ECM68.3": 8.4, "ECM162.1": 6.0})
    _write_state(manager, {
        "ECM62.10": _owned_entry(state="controlled", last="OFF"),
        "ECM68.3": _owned_entry(state="prepared", last="ON"),
        "ECM162.1": _owned_entry(state="prepared", last="ON"),
    })
    # 36 kW -> ECM62.10 selected OFF (already OFF), others ON (already ON).
    manager.run(slot_override=_slot(17, 30), dry_run=True, simulate_sold_mw=0.036)
    assert manager.controller.curtail_asset.call_count == 0
    assert manager.controller.restore_asset.call_count == 0


# ---------------------------------------------------------------------------
# Process restart
# ---------------------------------------------------------------------------

def test_process_restart_reconstructs_from_state(tmp_path):
    manager = _make_manager(tmp_path)
    _wire_delivery(manager, {"ECM62.10": 36.0, "ECM68.3": 8.4, "ECM162.1": 6.0})
    manager.run(slot_override=_slot(17, 0), dry_run=True, simulate_sold_mw=0.042)

    # Fresh manager instance sharing the same state file (simulated restart).
    manager2 = _make_manager(tmp_path)
    manager2.state_file = manager.state_file
    manager2.bid_handler.get_bid_record.return_value = None  # no activation now

    manager2.run(slot_override=_slot(17, 15), dry_run=True)
    # Previously OFF assets are brought back ON since no delivery now; the
    # asset that was already prepared ON is left alone (idempotency).
    assert _on_calls(manager2) == {"ECM62.10", "ECM162.1"}
    assert manager2.controller.curtail_asset.call_count == 0
    state = _read_state(manager2)
    for asset_id in SCOPE_ASSETS:
        assert state[asset_id]["state"] == "prepared"


def test_prepared_since_preserved_across_runs(tmp_path):
    manager = _make_manager(tmp_path)
    manager.run(slot_override=_slot(14, 0), dry_run=True)
    first = _read_state(manager)["ECM62.10"]["prepared_since"]

    manager.run(slot_override=_slot(15, 0), dry_run=True)
    second = _read_state(manager)["ECM62.10"]["prepared_since"]
    assert first == second


# ---------------------------------------------------------------------------
# Non-preconditioning FSP is untouched by the lifecycle
# ---------------------------------------------------------------------------

def test_non_preconditioning_fsp_returns_no_context(tmp_path):
    manager = _make_manager(tmp_path, fsp_strategy="strategy_11")
    assert manager._resolve_preconditioning_context() is None


# ===========================================================================
# Step 3.5 - delivery bookkeeping, command results, metadata
# ===========================================================================

def _save_calls(manager):
    return manager.bid_repo.save_asset_activations_batch.call_args_list


def _last_save_kwargs(manager):
    call = manager.bid_repo.save_asset_activations_batch.call_args
    assert call is not None, "save_asset_activations_batch was not called"
    return call.kwargs


def _pc(summary):
    return summary["preconditioning"]


# --- Delivery activation records -------------------------------------------

def test_maintain_delivery_writes_activation_records_for_selected_only(tmp_path):
    manager = _make_manager(tmp_path)
    _wire_delivery(manager, {"ECM62.10": 36.0, "ECM68.3": 8.4, "ECM162.1": 6.0})
    manager.bid_handler.get_bid_record.return_value = {"id": "BID-42"}

    summary = manager.run(slot_override=_slot(17, 0), dry_run=True, simulate_sold_mw=0.042)

    assert manager.bid_repo.save_asset_activations_batch.call_count == 1
    kwargs = _last_save_kwargs(manager)
    assert kwargs["fsp_id"] == "supsi02"
    assert kwargs["dry_run"] is True
    assert kwargs["bid_record_id"] == "BID-42"
    assert kwargs["allocation_strategy"] == "preconditioned_binary"
    recs = {r["asset_id"]: r for r in kwargs["activations"]}
    assert set(recs) == {"ECM62.10", "ECM162.1"}
    assert "ECM68.3" not in recs
    assert recs["ECM62.10"]["power_kw"] == pytest.approx(36.0)
    assert recs["ECM162.1"]["power_kw"] == pytest.approx(6.0)
    # slot boundaries are passed through
    assert kwargs["slot_start"] is not None and kwargs["slot_end"] is not None
    assert _pc(summary)["delivery_activation_records_written"] == 2
    assert _pc(summary)["delivery_assets"] == ["ECM162.1", "ECM62.10"]


def test_prepare_phase_writes_no_activation_records(tmp_path):
    manager = _make_manager(tmp_path)
    summary = manager.run(slot_override=_slot(14, 0), dry_run=True)
    assert manager.bid_repo.save_asset_activations_batch.call_count == 0
    assert _pc(summary)["delivery_activation_records_written"] == 0


def test_maintain_non_selected_on_not_in_activation_records(tmp_path):
    manager = _make_manager(tmp_path)
    _wire_delivery(manager, {"ECM62.10": 36.0, "ECM68.3": 8.4, "ECM162.1": 6.0})
    # 36 kW -> only ECM62.10 selected OFF.
    manager.run(slot_override=_slot(17, 0), dry_run=True, simulate_sold_mw=0.036)
    kwargs = _last_save_kwargs(manager)
    recs = {r["asset_id"] for r in kwargs["activations"]}
    assert recs == {"ECM62.10"}


def test_no_activation_maintain_writes_no_records(tmp_path):
    manager = _make_manager(tmp_path)
    manager.bid_handler.get_bid_record.return_value = None
    manager.run(slot_override=_slot(18, 30), dry_run=True)
    assert manager.bid_repo.save_asset_activations_batch.call_count == 0


def test_release_writes_no_activation_records(tmp_path):
    manager = _make_manager(tmp_path)
    _write_state(manager, {a: _owned_entry(state="prepared", last="ON") for a in SCOPE_ASSETS})
    manager.run(slot_override=_slot(20, 0), dry_run=True)
    assert manager.bid_repo.save_asset_activations_batch.call_count == 0


def test_dry_run_delivery_writes_dry_run_records(tmp_path):
    manager = _make_manager(tmp_path)
    _wire_delivery(manager, {"ECM62.10": 36.0, "ECM68.3": 8.4, "ECM162.1": 6.0})
    manager.bid_handler.get_bid_record.return_value = {"id": "BID-1"}
    manager.run(slot_override=_slot(17, 0), dry_run=True, simulate_sold_mw=0.036)
    assert _last_save_kwargs(manager)["dry_run"] is True


def test_delivery_records_linked_to_bid_record(tmp_path):
    manager = _make_manager(tmp_path)
    _wire_delivery(manager, {"ECM62.10": 36.0, "ECM68.3": 8.4, "ECM162.1": 6.0})
    manager.bid_handler.get_bid_record.return_value = {"id": "BID-XYZ"}
    manager.run(slot_override=_slot(17, 0), dry_run=True, simulate_sold_mw=0.036)
    assert _last_save_kwargs(manager)["bid_record_id"] == "BID-XYZ"


def test_no_activation_records_when_bid_repo_missing(tmp_path):
    manager = _make_manager(tmp_path)
    manager.bid_repo = None  # no repository configured
    _wire_delivery(manager, {"ECM62.10": 36.0, "ECM68.3": 8.4, "ECM162.1": 6.0})
    # Must not raise and must still control assets.
    summary = manager.run(slot_override=_slot(17, 0), dry_run=True, simulate_sold_mw=0.036)
    assert "ECM62.10" in _off_calls(manager)
    assert _pc(summary)["delivery_activation_records_written"] == 0


# --- Command result status --------------------------------------------------

def test_successful_delivery_status_in_records_and_summary(tmp_path):
    manager = _make_manager(tmp_path)
    manager.controller.curtail_asset.return_value = {"status": "queued"}
    _wire_delivery(manager, {"ECM62.10": 36.0, "ECM68.3": 8.4, "ECM162.1": 6.0})
    manager.bid_handler.get_bid_record.return_value = {"id": "BID-1"}
    summary = manager.run(slot_override=_slot(17, 0), dry_run=True, simulate_sold_mw=0.042)

    recs = {r["asset_id"]: r for r in _last_save_kwargs(manager)["activations"]}
    assert recs["ECM62.10"]["status"] == "queued"
    assert recs["ECM162.1"]["status"] == "queued"
    assert _pc(summary)["failed_commands"] == 0
    assert _pc(summary)["successful_commands"] >= 2


def test_failed_delivery_command_visible_in_records_and_summary(tmp_path):
    manager = _make_manager(tmp_path)

    def curtail(asset_id, *args, **kwargs):
        return {"status": "error"} if asset_id == "ECM162.1" else {"status": "simulated"}

    manager.controller.curtail_asset.side_effect = curtail
    _wire_delivery(manager, {"ECM62.10": 36.0, "ECM68.3": 8.4, "ECM162.1": 6.0})
    manager.bid_handler.get_bid_record.return_value = {"id": "BID-1"}
    summary = manager.run(slot_override=_slot(17, 0), dry_run=True, simulate_sold_mw=0.042)

    recs = {r["asset_id"]: r for r in _last_save_kwargs(manager)["activations"]}
    assert recs["ECM62.10"]["status"] == "simulated"
    assert recs["ECM162.1"]["status"] == "error"
    # Both selected assets still got a command (no early abort, no retry).
    assert _off_calls(manager) == {"ECM62.10", "ECM162.1"}
    assert _pc(summary)["failed_commands"] == 1
    # ECM62.10 OFF + ECM68.3 maintenance ON both succeeded.
    assert _pc(summary)["successful_commands"] == 2
    assert summary["status"] == "partial_success"


def test_failed_delivery_preserves_previous_state(tmp_path):
    manager = _make_manager(tmp_path)

    def curtail(asset_id, *args, **kwargs):
        return {"status": "error"} if asset_id == "ECM62.10" else {"status": "simulated"}

    manager.controller.curtail_asset.side_effect = curtail
    # ECM62.10 previously prepared ON; a failed OFF must not advance it to controlled.
    _write_state(manager, {"ECM62.10": _owned_entry(state="prepared", last="ON")})
    _wire_delivery(manager, {"ECM62.10": 36.0, "ECM68.3": 8.4, "ECM162.1": 6.0})
    manager.bid_handler.get_bid_record.return_value = {"id": "BID-1"}
    manager.run(slot_override=_slot(17, 0), dry_run=True, simulate_sold_mw=0.036)

    state = _read_state(manager)
    assert state["ECM62.10"]["state"] == "prepared"
    assert state["ECM62.10"]["last_commanded_state"] == "ON"


# --- Batch / lifecycle metadata --------------------------------------------

def test_prepare_batch_metadata(tmp_path):
    manager = _make_manager(tmp_path)
    summary = manager.run(slot_override=_slot(14, 0), dry_run=True)
    meta = _pc(summary)["batch_metadata"]
    assert meta["lifecycle"] == "preconditioned_binary"
    assert meta["phase"] == "prepare"
    assert meta["strategy_id"] == "strategy_12"
    assert meta["has_delivery"] is False
    assert meta["delivery_assets"] == []


def test_maintain_partial_delivery_batch_metadata(tmp_path):
    manager = _make_manager(tmp_path)
    _wire_delivery(manager, {"ECM62.10": 36.0, "ECM68.3": 8.4, "ECM162.1": 6.0})
    summary = manager.run(slot_override=_slot(17, 0), dry_run=True, simulate_sold_mw=0.042)
    meta = _pc(summary)["batch_metadata"]
    assert meta["phase"] == "maintain"
    assert meta["has_delivery"] is True
    assert meta["delivery_assets"] == ["ECM162.1", "ECM62.10"]


def test_release_batch_metadata(tmp_path):
    manager = _make_manager(tmp_path)
    _write_state(manager, {a: _owned_entry(state="prepared", last="ON") for a in SCOPE_ASSETS})
    summary = manager.run(slot_override=_slot(20, 0), dry_run=True)
    meta = _pc(summary)["batch_metadata"]
    assert meta["phase"] == "release"
    assert meta["has_delivery"] is False
    assert meta["delivery_assets"] == []


def test_mixed_maintain_batch_semantics(tmp_path):
    manager = _make_manager(tmp_path)
    _wire_delivery(manager, {"ECM62.10": 36.0, "ECM68.3": 8.4, "ECM162.1": 6.0})
    manager.bid_handler.get_bid_record.return_value = {"id": "BID-1"}
    # 36 kW -> ECM62.10 OFF (delivery); ECM68.3 + ECM162.1 ON (maintenance).
    manager.run(slot_override=_slot(17, 0), dry_run=True, simulate_sold_mw=0.036)

    # Per-command types remain correct.
    assert _off_calls(manager) == {"ECM62.10"}
    assert _on_calls(manager) == {"ECM68.3", "ECM162.1"}
    # Only the delivery asset is persisted.
    recs = {r["asset_id"] for r in _last_save_kwargs(manager)["activations"]}
    assert recs == {"ECM62.10"}


def test_command_results_categories(tmp_path):
    manager = _make_manager(tmp_path)
    _wire_delivery(manager, {"ECM62.10": 36.0, "ECM68.3": 8.4, "ECM162.1": 6.0})
    summary = manager.run(slot_override=_slot(17, 0), dry_run=True, simulate_sold_mw=0.036)
    cats = {r["asset_id"]: r["category"] for r in _pc(summary)["command_results"]}
    assert cats["ECM62.10"] == "delivery"
    assert cats["ECM68.3"] == "maintenance"
    assert cats["ECM162.1"] == "maintenance"
