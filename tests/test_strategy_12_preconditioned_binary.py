import logging
import sys
import types
from datetime import datetime, timedelta
from importlib.machinery import ModuleSpec
from importlib.util import find_spec

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
    resolve_preconditioned_binary_settings,
)
from scripts.trader_fsp import (  # noqa: E402
    build_persistence_assets_to_activate,
    resolve_strategy_flexibility_method,
)


CURRENT_TIME = datetime(2026, 5, 26, 17, 30)
SLOT_TIME = datetime(2026, 5, 26, 18, 0)

ASSET_STATES = {
    "ECM62.10": (0.0, 36.0),
    "ECM68.3": (0.0, 8.4),
    "ECM162.1": (0.0, 6.0),
}


def _hp_asset(asset_id, off_kw, on_kw):
    return {
        "device_name_tag": asset_id,
        "pod": asset_id.split(".")[0],
        "field": "active_power",
        "type": "heat_pump",
        "rabbitCommandSection": "simulatedAssetCommands",
        "description": f"HP {asset_id} simulated",
        "capacity_kw": on_kw,
        "nominal_power_w": int(on_kw * 1000),
        "modulation_type": "discrete",
        "discrete_states_kw": [off_kw, on_kw],
    }


def _default_settings():
    return {
        "maxCurrentMeasurementAgeMinutes": 30,
        "minSamples": 2,
        "requireLatestOn": True,
        "minOnRatio": 0.8,
        "stateToleranceW": 100,
        "missingMeasurementPolicy": "skip_asset",
    }


def _base_cfg(asset_mapping=None, settings=None, global_settings=None):
    if asset_mapping is None:
        asset_mapping = {
            asset_id: _hp_asset(asset_id, off, on)
            for asset_id, (off, on) in ASSET_STATES.items()
        }
    strategy_12 = {
        "name": "Preconditioned Binary HP Flexibility",
        "description": "test strategy_12",
        "enabled": True,
        "asset_types": ["heat_pump"],
        "assets_filter": list(asset_mapping.keys()),
        "flexibility_method": "preconditioned_binary",
        "preconditionedBinarySettings": (
            settings if settings is not None else _default_settings()
        ),
        "time_slots": [
            {
                "name": "Evening Peak Flex",
                "start": "17:00",
                "end": "20:00",
                "flexibility_mw": 0.0504,
                "bid_price": 11.5,
                "activation_cost": 0.8,
            }
        ],
    }

    flexibility = {
        "method": "historical",
        "persistenceSettings": {
            "activeThresholdW": 500,
            "maxCurrentMeasurementAgeMinutes": 30,
            "missingMeasurementPolicy": "skip_asset",
        },
    }
    if global_settings is not None:
        flexibility["preconditionedBinarySettings"] = global_settings

    return {
        "fm": {"granularity": 15},
        "influxDB": {"assetsMeasurement": "assets_data"},
        "flexibility": flexibility,
        "asset_mapping": asset_mapping,
        "bidding_strategies": {"strategy_12": strategy_12},
    }


def _series(values, current_time=CURRENT_TIME, step_minutes=15, latest_age_minutes=0):
    """Build a tz-aware series whose latest sample is `latest_age_minutes` old."""
    n = len(values)
    index = [
        pd.Timestamp(
            current_time
            - timedelta(minutes=latest_age_minutes + step_minutes * (n - 1 - i)),
            tz="UTC",
        )
        for i in range(n)
    ]
    return pd.Series(values, index=index, dtype=float)


def _forecaster(cfg, logger=None):
    strategy_config = cfg["bidding_strategies"]["strategy_12"]
    return FlexibilityForecaster(
        cfg,
        influx_client=object(),
        logger=logger or logging.getLogger("test_strategy_12_preconditioned_binary"),
        method_override="preconditioned_binary",
        strategy_config=strategy_config,
        strategy_id="strategy_12",
    )


def _install_measurements(forecaster, series_by_asset):
    forecaster._test_query_windows = []

    def query(asset_id, start_time_utc, end_time_utc):
        forecaster._test_query_windows.append((asset_id, start_time_utc, end_time_utc))
        return series_by_asset.get(asset_id, pd.Series(dtype=float))

    forecaster._query_grouped_asset_series = query


def _breakdown_one(forecaster, asset_id):
    return forecaster.get_asset_flexibility_breakdown(
        SLOT_TIME,
        asset_ids=[asset_id],
        current_time_utc=CURRENT_TIME,
    )[asset_id]


# --------------------------------------------------------------------------- #
# Method resolution
# --------------------------------------------------------------------------- #


def test_method_resolves_to_preconditioned_binary_without_fallback(caplog):
    strategy = types.SimpleNamespace(
        strategy_id="strategy_12",
        config={"flexibility_method": "preconditioned_binary"},
    )
    with caplog.at_level(logging.WARNING):
        method = resolve_strategy_flexibility_method(
            strategy, logging.getLogger(__name__)
        )
    assert method == "preconditioned_binary"
    assert "Unknown flexibility_method" not in caplog.text
    assert "using historical" not in caplog.text


@pytest.mark.parametrize(
    "raw", ["preconditioned-binary", "preconditionedBinary", "PRECONDITIONED_BINARY"]
)
def test_method_resolution_aliases(raw):
    strategy = types.SimpleNamespace(
        strategy_id="strategy_12", config={"flexibility_method": raw}
    )
    assert (
        resolve_strategy_flexibility_method(strategy, logging.getLogger(__name__))
        == "preconditioned_binary"
    )


# --------------------------------------------------------------------------- #
# Settings resolution
# --------------------------------------------------------------------------- #


def test_settings_use_strategy_level_block():
    cfg = _base_cfg()
    forecaster = _forecaster(cfg)
    assert (
        forecaster.preconditioned_binary_settings_source
        == "bidding_strategies.strategy_12.preconditionedBinarySettings"
    )
    assert forecaster.preconditioned_binary_min_samples == 2
    assert forecaster.preconditioned_binary_require_latest_on is True
    assert forecaster.preconditioned_binary_min_on_ratio == pytest.approx(0.8)
    assert forecaster.preconditioned_binary_state_tolerance_w == pytest.approx(100)
    assert (
        forecaster.preconditioned_binary_max_current_measurement_age_minutes == 30
    )
    assert forecaster.preconditioned_binary_missing_measurement_policy == "skip_asset"


def test_settings_defaults_and_global_fallback(caplog):
    with caplog.at_level(logging.WARNING):
        resolved_default = resolve_preconditioned_binary_settings(
            {"flexibility": {}}, strategy_config={}, strategy_id="strategy_12"
        )
    assert resolved_default["source"] == "defaults"
    assert resolved_default["min_samples"] == 2

    global_block = {"minSamples": 3, "minOnRatio": 0.9}
    resolved_global = resolve_preconditioned_binary_settings(
        {"flexibility": {"preconditionedBinarySettings": global_block}},
        strategy_config={},
        strategy_id="strategy_12",
        logger=logging.getLogger(__name__),
    )
    assert resolved_global["source"] == "flexibility.preconditionedBinarySettings"
    assert resolved_global["min_samples"] == 3
    assert resolved_global["min_on_ratio"] == pytest.approx(0.9)


@pytest.mark.parametrize(
    "bad",
    [
        {"minSamples": 0},
        {"minOnRatio": 1.5},
        {"minOnRatio": -0.1},
        {"stateToleranceW": -1},
        {"maxCurrentMeasurementAgeMinutes": 0},
        {"missingMeasurementPolicy": "invent_data"},
    ],
)
def test_invalid_settings_raise_value_error(bad):
    settings = {**_default_settings(), **bad}
    with pytest.raises(ValueError):
        resolve_preconditioned_binary_settings(
            {"flexibility": {}},
            strategy_config={"preconditionedBinarySettings": settings},
            strategy_id="strategy_12",
        )


# --------------------------------------------------------------------------- #
# Binary state classification
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "value_w,expected",
    [
        (0.0, "off"),
        (40.0, "off"),
        (35950.0, "on"),
        (36020.0, "on"),
        (18000.0, "invalid"),
        (None, "invalid"),
        (float("nan"), "invalid"),
    ],
)
def test_classify_binary_state(value_w, expected):
    assert (
        FlexibilityForecaster._classify_binary_state(
            value_w, off_state_w=0.0, on_state_w=36000.0, tolerance_w=100.0
        )
        == expected
    )


# --------------------------------------------------------------------------- #
# Exact ON / OFF availability
# --------------------------------------------------------------------------- #


def test_exact_on_state_offers_full_block():
    cfg = _base_cfg()
    forecaster = _forecaster(cfg)
    _install_measurements(
        forecaster, {"ECM62.10": _series([35980, 36010, 35990, 36000])}
    )
    info = _breakdown_one(forecaster, "ECM62.10")
    assert info["available_flexibility_kw"] == pytest.approx(36.0)
    assert info["is_available_for_flexibility"] is True
    assert info["estimation_method"] == "preconditioned_binary"
    assert info["preconditioned_binary_on_ratio"] == pytest.approx(1.0)
    assert info["preconditioned_binary_rejection_reason"] is None


def test_exact_off_state_offers_zero():
    cfg = _base_cfg()
    forecaster = _forecaster(cfg)
    _install_measurements(forecaster, {"ECM62.10": _series([0, 30, 10, 20])})
    info = _breakdown_one(forecaster, "ECM62.10")
    assert info["available_flexibility_kw"] == pytest.approx(0.0)
    assert info["is_available_for_flexibility"] is False
    assert info["preconditioned_binary_latest_state"] == "off"
    assert info["preconditioned_binary_rejection_reason"] == "latest_not_on"


@pytest.mark.parametrize("asset_id,on_kw", [(a, ASSET_STATES[a][1]) for a in ASSET_STATES])
def test_each_asset_offers_its_own_block(asset_id, on_kw):
    cfg = _base_cfg()
    forecaster = _forecaster(cfg)
    on_w = on_kw * 1000
    _install_measurements(forecaster, {asset_id: _series([on_w, on_w, on_w, on_w])})
    info = _breakdown_one(forecaster, asset_id)
    assert info["available_flexibility_kw"] == pytest.approx(on_kw)


# --------------------------------------------------------------------------- #
# Freshness / missing data
# --------------------------------------------------------------------------- #


def test_fresh_on_is_eligible_but_stale_on_is_zero():
    cfg = _base_cfg()
    forecaster = _forecaster(cfg)
    _install_measurements(
        forecaster, {"ECM62.10": _series([36000, 36000], latest_age_minutes=5)}
    )
    fresh = _breakdown_one(forecaster, "ECM62.10")
    assert fresh["available_flexibility_kw"] == pytest.approx(36.0)

    _install_measurements(
        forecaster, {"ECM62.10": _series([36000, 36000], latest_age_minutes=45)}
    )
    stale = _breakdown_one(forecaster, "ECM62.10")
    assert stale["available_flexibility_kw"] == pytest.approx(0.0)
    assert stale["preconditioned_binary_rejection_reason"] == "stale_measurement"


def test_missing_telemetry_is_zero_under_skip_asset():
    cfg = _base_cfg()
    forecaster = _forecaster(cfg)
    _install_measurements(forecaster, {"ECM62.10": pd.Series(dtype=float)})
    info = _breakdown_one(forecaster, "ECM62.10")
    assert info["available_flexibility_kw"] == pytest.approx(0.0)
    assert info["preconditioned_binary_rejection_reason"] == "missing_telemetry"


def test_query_failure_fail_portfolio_returns_empty():
    settings = {**_default_settings(), "missingMeasurementPolicy": "fail_portfolio"}
    cfg = _base_cfg(settings=settings)
    forecaster = _forecaster(cfg)

    def failing_query(asset_id, start_time_utc, end_time_utc):
        return None

    forecaster._query_grouped_asset_series = failing_query
    breakdown = forecaster.get_asset_flexibility_breakdown(
        SLOT_TIME, asset_ids=["ECM62.10"], current_time_utc=CURRENT_TIME
    )
    assert breakdown == {}


# --------------------------------------------------------------------------- #
# Minimum samples / latest state / ON ratio
# --------------------------------------------------------------------------- #


def test_insufficient_valid_samples_is_zero():
    cfg = _base_cfg()
    forecaster = _forecaster(cfg)
    # Latest is ON, but only one valid classified sample (other is invalid).
    _install_measurements(forecaster, {"ECM62.10": _series([18000, 36000])})
    info = _breakdown_one(forecaster, "ECM62.10")
    assert info["preconditioned_binary_valid_sample_count"] == 1
    assert info["available_flexibility_kw"] == pytest.approx(0.0)
    assert info["preconditioned_binary_rejection_reason"] == "insufficient_samples"


def test_latest_off_with_require_latest_on_is_zero():
    cfg = _base_cfg()
    forecaster = _forecaster(cfg)
    _install_measurements(
        forecaster, {"ECM62.10": _series([36000, 36000, 36000, 0])}
    )
    info = _breakdown_one(forecaster, "ECM62.10")
    assert info["preconditioned_binary_latest_state"] == "off"
    assert info["available_flexibility_kw"] == pytest.approx(0.0)
    assert info["preconditioned_binary_rejection_reason"] == "latest_not_on"


def test_latest_invalid_with_require_latest_on_is_zero():
    cfg = _base_cfg()
    forecaster = _forecaster(cfg)
    _install_measurements(
        forecaster, {"ECM62.10": _series([36000, 36000, 36000, 18000])}
    )
    info = _breakdown_one(forecaster, "ECM62.10")
    assert info["preconditioned_binary_latest_state"] == "invalid"
    assert info["available_flexibility_kw"] == pytest.approx(0.0)
    assert info["preconditioned_binary_rejection_reason"] == "latest_not_on"


def test_on_ratio_below_threshold_is_zero():
    cfg = _base_cfg()
    forecaster = _forecaster(cfg)
    # 3 valid samples, 2 ON -> ratio 0.667 < 0.8, latest is ON.
    _install_measurements(forecaster, {"ECM62.10": _series([0, 36000, 36000])})
    info = _breakdown_one(forecaster, "ECM62.10")
    assert info["preconditioned_binary_valid_sample_count"] == 3
    assert info["preconditioned_binary_on_sample_count"] == 2
    assert info["preconditioned_binary_on_ratio"] == pytest.approx(2 / 3)
    assert info["available_flexibility_kw"] == pytest.approx(0.0)
    assert info["preconditioned_binary_rejection_reason"] == "on_ratio_below_threshold"


def test_on_ratio_denominator_ignores_invalid_samples():
    cfg = _base_cfg()
    forecaster = _forecaster(cfg)
    # samples: OFF, ON, invalid, ON -> valid=3, on=2 -> ratio 2/3 < 0.8.
    _install_measurements(
        forecaster, {"ECM62.10": _series([0, 36000, 18000, 36000])}
    )
    info = _breakdown_one(forecaster, "ECM62.10")
    assert info["preconditioned_binary_valid_sample_count"] == 3
    assert info["preconditioned_binary_on_sample_count"] == 2
    assert info["preconditioned_binary_on_ratio"] == pytest.approx(2 / 3)


def test_on_ratio_at_threshold_offers_block():
    cfg = _base_cfg()
    forecaster = _forecaster(cfg)
    # 5 samples, 4 ON, latest ON -> ratio 0.8 == minOnRatio.
    _install_measurements(
        forecaster, {"ECM62.10": _series([0, 36000, 36000, 36000, 36000])}
    )
    info = _breakdown_one(forecaster, "ECM62.10")
    assert info["preconditioned_binary_on_ratio"] == pytest.approx(0.8)
    assert info["available_flexibility_kw"] == pytest.approx(36.0)


# --------------------------------------------------------------------------- #
# Invalid intermediate state must never contribute
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "asset_id,intermediate_w",
    [
        ("ECM62.10", 18000),
        ("ECM68.3", 4200),
        ("ECM162.1", 3000),
    ],
)
def test_intermediate_state_never_contributes(asset_id, intermediate_w):
    cfg = _base_cfg()
    forecaster = _forecaster(cfg)
    _install_measurements(
        forecaster, {asset_id: _series([intermediate_w] * 4)}
    )
    info = _breakdown_one(forecaster, asset_id)
    assert info["preconditioned_binary_latest_state"] == "invalid"
    assert info["preconditioned_binary_valid_sample_count"] == 0
    assert info["available_flexibility_kw"] == pytest.approx(0.0)


def test_tolerance_window_classification():
    cfg = _base_cfg()
    forecaster = _forecaster(cfg)
    _install_measurements(
        forecaster, {"ECM62.10": _series([35950, 36000, 35980, 36010])}
    )
    on_info = _breakdown_one(forecaster, "ECM62.10")
    assert on_info["available_flexibility_kw"] == pytest.approx(36.0)

    _install_measurements(forecaster, {"ECM62.10": _series([18000] * 4)})
    invalid_info = _breakdown_one(forecaster, "ECM62.10")
    assert invalid_info["available_flexibility_kw"] == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# Discrete portfolio combinations
# --------------------------------------------------------------------------- #


def _achievable(forecaster, target_kw, allowed=None):
    asset_ids = list(ASSET_STATES.keys())
    return forecaster.get_achievable_flexibility(
        SLOT_TIME,
        target_kw=target_kw,
        allowed_assets=allowed or asset_ids,
        asset_ids=asset_ids,
        current_time_utc=CURRENT_TIME,
    )


def test_all_three_available_produces_full_combination_set():
    cfg = _base_cfg()
    forecaster = _forecaster(cfg)
    _install_measurements(
        forecaster,
        {
            "ECM62.10": _series([36000, 36000]),
            "ECM68.3": _series([8400, 8400]),
            "ECM162.1": _series([6000, 6000]),
        },
    )
    result = _achievable(forecaster, target_kw=50.4)
    levels = sorted(round(x, 2) for x in result["discrete_levels_kw"])
    assert levels == [0.0, 6.0, 8.4, 14.4, 36.0, 42.0, 44.4, 50.4]
    assert result["total_achievable_range_kw"][1] == pytest.approx(50.4)


def test_partial_availability_reduces_combination_set():
    cfg = _base_cfg()
    forecaster = _forecaster(cfg)
    # ECM162.1 unavailable (latest OFF); the other two ON.
    _install_measurements(
        forecaster,
        {
            "ECM62.10": _series([36000, 36000]),
            "ECM68.3": _series([8400, 8400]),
            "ECM162.1": _series([0, 0]),
        },
    )
    result = _achievable(forecaster, target_kw=50.4)
    levels = sorted(round(x, 2) for x in result["discrete_levels_kw"])
    assert levels == [0.0, 8.4, 36.0, 44.4]
    assert result["total_achievable_range_kw"][1] == pytest.approx(44.4)


def test_single_asset_available_caps_target():
    cfg = _base_cfg()
    forecaster = _forecaster(cfg)
    _install_measurements(
        forecaster,
        {
            "ECM62.10": _series([0, 0]),
            "ECM68.3": _series([8400, 8400]),
            "ECM162.1": _series([0, 0]),
        },
    )
    result = _achievable(forecaster, target_kw=50.4)
    assert result["total_achievable_range_kw"][1] == pytest.approx(8.4)
    assert result["recommended_bid_kw"] == pytest.approx(8.4)


# --------------------------------------------------------------------------- #
# Bid-record subset allocation
# --------------------------------------------------------------------------- #


def test_selected_bid_activates_only_chosen_assets():
    cfg = _base_cfg()
    forecaster = _forecaster(cfg)
    _install_measurements(
        forecaster,
        {
            "ECM62.10": _series([36000, 36000]),
            "ECM68.3": _series([8400, 8400]),
            "ECM162.1": _series([6000, 6000]),
        },
    )
    result = _achievable(forecaster, target_kw=42.0)
    assert result["recommended_bid_kw"] == pytest.approx(42.0)
    allocation = result["recommended_allocation"]["discrete"]
    assert set(allocation.keys()) == {"ECM62.10", "ECM162.1"}
    assert allocation["ECM62.10"] == pytest.approx(36.0)
    assert allocation["ECM162.1"] == pytest.approx(6.0)

    contexts = {
        "supsi02": {
            "asset_breakdown": result["asset_breakdown"],
            "achievable_details": result,
        }
    }
    assets = build_persistence_assets_to_activate(contexts)
    activated = {a["asset_id"]: a["available_flexibility_kw"] for a in assets}
    assert activated == {"ECM62.10": 36.0, "ECM162.1": 6.0}
    # A binary HP carries no recent_profile reference power.
    for asset in assets:
        assert asset.get("reference_power_kw") is None


def test_strategy_manager_loads_strategy_12_filter():
    cfg = _base_cfg()
    strategy = StrategyManager(cfg).get_strategy("strategy_12")
    assert sorted(strategy.allowed_assets) == ["ECM162.1", "ECM62.10", "ECM68.3"]
    assert resolve_strategy_flexibility_method(
        strategy, logging.getLogger(__name__)
    ) == "preconditioned_binary"
