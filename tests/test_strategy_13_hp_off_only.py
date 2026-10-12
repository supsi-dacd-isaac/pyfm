import json
import logging
import sys
import types
from datetime import datetime
from importlib.machinery import ModuleSpec
from importlib.util import find_spec
from pathlib import Path

import pandas as pd
import pytest


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


from classes.bidding_strategy import StrategyManager  # noqa: E402
from classes.flexibility_forecaster import (  # noqa: E402
    FlexibilityForecaster,
    resolve_hp_off_only_settings,
)
from scripts.trader_fsp import (  # noqa: E402
    build_persistence_assets_to_activate,
    resolve_strategy_flexibility_method,
)
from scripts.flexi_manager import uses_bid_record_activation_plan  # noqa: E402


REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "conf" / "fm02_hp.json"
CURRENT_TIME = datetime(2026, 10, 8, 14, 0)
SLOT_TIME = datetime(2026, 10, 8, 15, 30)


def _settings(**overrides):
    settings = {
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
    }
    settings.update(overrides)
    return settings


def _hp_asset(capacity_kw=6.0, nominal_power_w=7500):
    return {
        "device_name_tag": "hp_test",
        "pod": "ECM96",
        "field": "active_power",
        "type": "heat_pump",
        "description": "HP test",
        "capacity_kw": capacity_kw,
        "nominal_power_w": nominal_power_w,
        "flexibility_factor": 1.0,
        "baseline_persistence_go_back_minutes": 1440,
        "flexibility_persistence_go_back_minutes": 120,
        "modulation_type": "discrete",
        "discrete_states_kw": [0.0, capacity_kw],
    }


def _base_cfg(settings=None, asset_mapping=None):
    asset_mapping = asset_mapping or {"ECM96.2": _hp_asset()}
    strategy = {
        "name": "Conservative HP OFF-only Strategy",
        "asset_types": ["heat_pump"],
        "assets_filter": list(asset_mapping),
        "flexibility_method": "hp_off_only",
        "hpOffOnlySettings": settings or _settings(),
        "time_slots": [
            {
                "name": "All day",
                "start": "00:00",
                "end": "00:00",
                "flexibility_mw": 0.040,
                "bid_price": 9.0,
                "activation_cost": 2.5,
            }
        ],
    }
    return {
        "fm": {"granularity": 15},
        "influxDB": {"assetsMeasurement": "assets_data"},
        "flexibility": {
            "method": "historical",
            "persistenceSettings": {
                "persistenceGoBackMinutes": 90,
                "activeThresholdW": 500,
                "defaultSafetyFactor": 1.0,
                "missingMeasurementPolicy": "skip_asset",
                "maxCurrentMeasurementAgeMinutes": 30,
            },
        },
        "asset_mapping": asset_mapping,
        "bidding_strategies": {"strategy_13": strategy},
    }


def _forecaster(cfg=None):
    cfg = cfg or _base_cfg()
    strategy = cfg["bidding_strategies"]["strategy_13"]
    return FlexibilityForecaster(
        cfg,
        influx_client=object(),
        logger=logging.getLogger("test_strategy_13_hp_off_only"),
        method_override="hp_off_only",
        strategy_config=strategy,
        strategy_id="strategy_13",
    )


def _install_measurements(
    forecaster,
    baseline_w=5500,
    short_w=5000,
    recent_values=(4000, 4500, 5000, 5500),
    failed_window=None,
):
    baseline_start = pd.Timestamp(SLOT_TIME, tz="UTC") - pd.Timedelta(hours=24)
    short_start = pd.Timestamp(SLOT_TIME, tz="UTC") - pd.Timedelta(minutes=120)
    recent_end = pd.Timestamp(CURRENT_TIME, tz="UTC")
    recent_start = recent_end - pd.Timedelta(minutes=60)

    series_by_start = {
        baseline_start: pd.Series(
            [baseline_w], index=pd.DatetimeIndex([baseline_start]), dtype=float
        ),
        short_start: pd.Series(
            [short_w], index=pd.DatetimeIndex([short_start]), dtype=float
        ),
        recent_start: pd.Series(
            recent_values,
            index=pd.date_range(
                recent_start,
                periods=len(recent_values),
                freq="15min",
            ),
            dtype=float,
        ),
    }
    forecaster._test_query_windows = []

    def query(asset_id, start_time_utc, end_time_utc):
        start = pd.Timestamp(start_time_utc)
        forecaster._test_query_windows.append((asset_id, start, pd.Timestamp(end_time_utc)))
        if failed_window == start:
            return None
        return series_by_start.get(start, pd.Series(dtype=float))

    forecaster._query_grouped_asset_series = query
    return {
        "baseline_start": baseline_start,
        "short_start": short_start,
        "recent_start": recent_start,
    }


def _breakdown(forecaster):
    return forecaster.get_asset_flexibility_breakdown(
        SLOT_TIME,
        asset_ids=["ECM96.2"],
        current_time_utc=CURRENT_TIME,
    )["ECM96.2"]


def test_production_config_selects_strategy_13_and_only_hp_assets():
    cfg = json.loads(CONFIG_PATH.read_text())
    fsp_cfg = cfg["fm"]["actors"]["fsps"]["supsi02"]
    strategy_8 = cfg["bidding_strategies"]["strategy_8"]
    strategy_13 = cfg["bidding_strategies"]["strategy_13"]

    assert fsp_cfg["name"] == "SUPSI_HP"
    assert fsp_cfg["portfolioName"] == "ECM_HP"
    assert fsp_cfg["strategy"] == "strategy_13"
    assert strategy_13["flexibility_method"] == "hp_off_only"
    assert strategy_13["assets_filter"] == ["ECM96.2", "ECM97.3"]
    assert strategy_13["hpOffOnlySettings"]["controlMode"] == "off_only"
    assert strategy_13["hpOffOnlySettings"]["allowPreheating"] is False
    assert strategy_13["hpOffOnlySettings"]["maxConsecutiveActivationSlots"] == 2
    assert strategy_13["hpOffOnlySettings"]["cooldownSlotsAfterMaxActivation"] == 2
    assert all(
        "is_preheat_period" not in slot and "preheat_power_mw" not in slot
        for slot in strategy_13["time_slots"]
    )
    for old_slot, new_slot in zip(strategy_8["time_slots"], strategy_13["time_slots"]):
        for key in ("start", "end", "flexibility_mw", "bid_price", "activation_cost"):
            assert new_slot[key] == old_slot[key]


def test_production_hp_states_and_capacities_are_binary():
    cfg = json.loads(CONFIG_PATH.read_text())
    small = cfg["asset_mapping"]["ECM96.2"]
    large = cfg["asset_mapping"]["ECM97.3"]

    assert small["capacity_kw"] == pytest.approx(6.0)
    assert small["discrete_states_kw"] == [0.0, 6.0]
    assert large["capacity_kw"] == pytest.approx(30.0)
    assert sorted(set(large["discrete_states_kw"])) == [0.0, 30.0]


def test_method_resolution_accepts_hp_off_only_and_aliases():
    logger = logging.getLogger(__name__)
    for raw in ("hp_off_only", "hp-off-only", "hpoffonly", "HP_OFF_ONLY"):
        strategy = types.SimpleNamespace(
            strategy_id="strategy_13", config={"flexibility_method": raw}
        )
        assert resolve_strategy_flexibility_method(strategy, logger) == "hp_off_only"


def test_activation_uses_exact_bid_record_plan_for_hp_off_only():
    assert uses_bid_record_activation_plan("hp_off_only") is True
    assert uses_bid_record_activation_plan("historical") is False


def test_settings_are_strategy_owned_and_reject_preheating():
    cfg = _base_cfg()
    resolved = resolve_hp_off_only_settings(
        cfg,
        strategy_config=cfg["bidding_strategies"]["strategy_13"],
        strategy_id="strategy_13",
        granularity=15,
    )
    assert resolved["source"] == "bidding_strategies.strategy_13.hpOffOnlySettings"
    assert resolved["baseline_go_back_minutes"] == 1440
    assert resolved["recent_window_minutes"] == 60
    assert resolved["quantile"] == pytest.approx(0.25)
    assert resolved["max_consecutive_activation_slots"] == 2
    assert resolved["cooldown_slots_after_max_activation"] == 2

    bad_cfg = _base_cfg(settings=_settings(allowPreheating=True))
    with pytest.raises(ValueError, match="allowPreheating must be false"):
        _forecaster(bad_cfg)


def test_settings_reject_non_off_only_control_and_unaligned_windows():
    for settings, message in (
        (_settings(controlMode="on_off"), "controlMode must be 'off_only'"),
        (_settings(baselineGoBackMinutes=1439), "baselineGoBackMinutes"),
        (_settings(recentWindowMinutes=55), "recentWindowMinutes"),
    ):
        with pytest.raises(ValueError, match=message):
            _forecaster(_base_cfg(settings=settings))


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        (
            _settings(maxConsecutiveActivationSlots=0),
            "maxConsecutiveActivationSlots",
        ),
        (
            _settings(cooldownSlotsAfterMaxActivation=-1),
            "cooldownSlotsAfterMaxActivation",
        ),
    ],
)
def test_settings_reject_invalid_hp_continuation_limits(settings, message):
    with pytest.raises(ValueError, match=message):
        _forecaster(_base_cfg(settings=settings))


@pytest.mark.parametrize(
    "asset_update",
    [
        {"modulation_type": "continuous"},
        {"discrete_states_kw": [0.0, 3.0, 6.0]},
        {"discrete_states_kw": [0.0, 5.0]},
    ],
)
def test_non_binary_hp_configuration_is_rejected_without_querying(asset_update):
    asset = _hp_asset()
    asset.update(asset_update)
    forecaster = _forecaster(_base_cfg(asset_mapping={"ECM96.2": asset}))
    query_calls = []
    forecaster._query_grouped_asset_series = lambda **kwargs: query_calls.append(kwargs)

    info = _breakdown(forecaster)

    assert info["available_flexibility_kw"] == 0.0
    assert info["hp_off_only_rejection_reason"] == "asset_not_binary_off_only"
    assert query_calls == []


def test_conservative_minimum_is_quantized_down_to_one_kw():
    forecaster = _forecaster()
    windows = _install_measurements(forecaster)

    info = _breakdown(forecaster)

    assert info["settlement_baseline_power_w"] == pytest.approx(5500)
    assert info["short_term_power_w"] == pytest.approx(5000)
    assert info["recent_quantile_power_w"] == pytest.approx(4375)
    assert info["hp_off_only_raw_available_kw"] == pytest.approx(4.375)
    assert info["available_flexibility_kw"] == pytest.approx(4.0)
    assert info["is_available_for_flexibility"] is True
    assert info["hp_off_only_control_mode"] == "off_only"
    assert info["hp_off_only_preheating_allowed"] is False
    assert [window[1] for window in forecaster._test_query_windows] == [
        windows["baseline_start"],
        windows["short_start"],
        windows["recent_start"],
    ]


def test_capacity_hard_cap_is_applied_before_quantization():
    forecaster = _forecaster()
    _install_measurements(
        forecaster,
        baseline_w=9000,
        short_w=8500,
        recent_values=(7000, 7500, 8000, 8500),
    )

    info = _breakdown(forecaster)

    assert info["hp_off_only_raw_available_kw"] == pytest.approx(6.0)
    assert info["available_flexibility_kw"] == pytest.approx(6.0)
    assert info["available_flexibility_kw"] <= info["nominal_capacity_kw"]


def test_unstable_or_inactive_recent_load_is_not_biddable():
    forecaster = _forecaster()
    _install_measurements(
        forecaster,
        recent_values=(0, 0, 4000, 5000),
    )
    unstable = _breakdown(forecaster)
    assert unstable["available_flexibility_kw"] == 0.0
    assert unstable["hp_off_only_recent_active_ratio"] == pytest.approx(0.5)
    assert unstable["hp_off_only_rejection_reason"] == "recent_active_ratio_below_minimum"

    _install_measurements(
        forecaster,
        recent_values=(4000, 4000, 4000, 100),
    )
    inactive = _breakdown(forecaster)
    assert inactive["available_flexibility_kw"] == 0.0
    assert inactive["hp_off_only_rejection_reason"] == "latest_measurement_not_active"


def test_query_failure_skips_asset_or_fails_portfolio_by_policy():
    forecaster = _forecaster()
    windows = _install_measurements(forecaster)
    _install_measurements(forecaster, failed_window=windows["short_start"])
    skipped = _breakdown(forecaster)
    assert skipped["available_flexibility_kw"] == 0.0
    assert skipped["hp_off_only_rejection_reason"] == "measurement_query_failed"

    cfg = _base_cfg(settings=_settings(missingMeasurementPolicy="fail_portfolio"))
    fail_closed = _forecaster(cfg)
    _install_measurements(fail_closed, failed_window=windows["short_start"])
    result = fail_closed.get_asset_flexibility_breakdown(
        SLOT_TIME,
        asset_ids=["ECM96.2"],
        current_time_utc=CURRENT_TIME,
    )
    assert result == {}


def test_bid_allocation_uses_only_exact_selected_hp_and_stores_reference():
    forecaster = _forecaster()
    _install_measurements(forecaster)

    achievable = forecaster.get_achievable_flexibility(
        SLOT_TIME,
        target_kw=5.0,
        allowed_assets=["ECM96.2"],
        asset_ids=["ECM96.2"],
        current_time_utc=CURRENT_TIME,
    )

    assert achievable["recommended_bid_kw"] == pytest.approx(4.0)
    assert achievable["recommended_allocation"]["discrete"] == {"ECM96.2": 4.0}
    assets = build_persistence_assets_to_activate(
        {
            "supsi02": {
                "asset_breakdown": achievable["asset_breakdown"],
                "achievable_details": achievable,
            }
        }
    )
    assert len(assets) == 1
    assert assets[0]["asset_id"] == "ECM96.2"
    assert assets[0]["available_flexibility_kw"] == pytest.approx(4.0)
    assert assets[0]["reference_power_kw"] == pytest.approx(4.375)
    assert (
        assets[0]["reference_power_source"]
        == "hp_off_only_conservative_target"
    )


def test_strategy_manager_exposes_only_configured_hp_assets():
    cfg = _base_cfg(
        asset_mapping={
            "ECM96.2": _hp_asset(6.0, 7500),
            "ECM97.3": _hp_asset(30.0, 30000),
        }
    )
    strategy = StrategyManager(cfg).get_strategy("strategy_13")
    assert strategy.allowed_assets == ["ECM96.2", "ECM97.3"]
    assert resolve_strategy_flexibility_method(
        strategy, logging.getLogger(__name__)
    ) == "hp_off_only"
