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
    build_recent_profile_available_flex_explanation,
    format_recent_profile_asset_log_lines,
    resolve_recent_profile_settings,
)
from scripts.trader_fsp import (  # noqa: E402
    build_persistence_assets_to_activate,
    resolve_strategy_flexibility_method,
)


CURRENT_TIME = datetime(2026, 5, 26, 10, 0)
SLOT_TIME = datetime(2026, 5, 26, 11, 0)


def _asset(
    asset_type="ev_charger",
    modulation_type="continuous",
    nominal_power_w=12000,
    capacity_kw=12.0,
    description="test asset",
):
    return {
        "type": asset_type,
        "modulation_type": modulation_type,
        "nominal_power_w": nominal_power_w,
        "capacity_kw": capacity_kw,
        "description": description,
        "device_name_tag": description.replace(" ", "_"),
        "field": "active_power",
        "pod": "ECM",
    }


def _base_cfg(asset_mapping, recent_settings=None, global_recent_settings=None):
    strategy_10 = {
        "name": "Recent profile EV",
        "description": "test strategy_10",
        "asset_types": ["ev_charger", "heat_pump"],
        "assets_filter": ["ECM63.1", "ECM63.2"],
        "flexibility_method": "recent_profile",
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
    if recent_settings is not None:
        strategy_10["recentProfileSettings"] = recent_settings

    flexibility = {
        "method": "historical",
        "persistenceSettings": {
            "activeThresholdW": 500,
            "maxCurrentMeasurementAgeMinutes": 30,
            "missingMeasurementPolicy": "skip_asset",
        },
    }
    if global_recent_settings is not None:
        flexibility["recentProfileSettings"] = global_recent_settings

    return {
        "fm": {"granularity": 15},
        "influxDB": {"assetsMeasurement": "assets_data"},
        "flexibility": flexibility,
        "asset_mapping": asset_mapping,
        "bidding_strategies": {"strategy_10": strategy_10},
    }


def _series(values, current_time=CURRENT_TIME, step_minutes=15):
    index = [
        pd.Timestamp(current_time - timedelta(minutes=step_minutes * (len(values) - i)), tz="UTC")
        for i in range(len(values))
    ]
    return pd.Series(values, index=index, dtype=float)


def _forecaster(cfg, logger=None):
    strategy_config = cfg["bidding_strategies"]["strategy_10"]
    return FlexibilityForecaster(
        cfg,
        influx_client=object(),
        logger=logger or logging.getLogger("test_strategy_10_recent_profile"),
        method_override="recent_profile",
        strategy_config=strategy_config,
        strategy_id="strategy_10",
    )


def _install_measurements(
    forecaster,
    recent_by_asset,
    current_by_asset=None,
    age_minutes_by_asset=None,
):
    current_by_asset = current_by_asset or {}
    age_minutes_by_asset = age_minutes_by_asset or {}

    def query(asset_id, start_time_utc, end_time_utc):
        return recent_by_asset.get(asset_id, pd.Series(dtype=float))

    def latest(asset_id, current_time_utc, max_age_minutes):
        if asset_id not in current_by_asset:
            return None, None, None
        age_minutes = age_minutes_by_asset.get(asset_id, 0)
        measurement_time = pd.Timestamp(
            current_time_utc - timedelta(minutes=age_minutes),
            tz="UTC",
        )
        return measurement_time, current_by_asset[asset_id], age_minutes

    forecaster._query_grouped_asset_series = query
    forecaster._get_latest_grouped_measurement = latest


def test_recent_profile_uses_strategy_level_settings():
    settings = {
        "lookbackMinutes": 60,
        "quantile": 0.5,
        "continuousFactor": 0.4,
        "discreteFactor": 0.8,
        "activeThresholdW": 700,
        "minSamples": 3,
        "missingMeasurementPolicy": "fail_portfolio",
    }
    cfg = _base_cfg({"ECM63.1": _asset()}, recent_settings=settings)

    resolved = resolve_recent_profile_settings(
        cfg,
        strategy_config=cfg["bidding_strategies"]["strategy_10"],
        strategy_id="strategy_10",
    )
    forecaster = _forecaster(cfg)

    assert resolved["source"] == "bidding_strategies.strategy_10.recentProfileSettings"
    assert forecaster.recent_profile_settings_source == resolved["source"]
    assert forecaster.recent_profile_lookback_minutes == 60
    assert forecaster.recent_profile_quantile == pytest.approx(0.5)
    assert forecaster.recent_profile_continuous_factor == pytest.approx(0.4)
    assert forecaster.recent_profile_discrete_factor == pytest.approx(0.8)
    assert forecaster.recent_profile_active_threshold_w == pytest.approx(700)
    assert forecaster.recent_profile_min_samples == 3
    assert forecaster.recent_profile_missing_measurement_policy == "fail_portfolio"


def test_recent_profile_uses_legacy_global_fallback(caplog):
    global_settings = {
        "lookbackMinutes": 90,
        "continuousFactor": 0.35,
        "activeThresholdW": 650,
    }
    cfg = _base_cfg({"ECM63.1": _asset()}, global_recent_settings=global_settings)

    with caplog.at_level(logging.WARNING):
        forecaster = _forecaster(cfg)

    assert (
        forecaster.recent_profile_settings_source
        == "flexibility.recentProfileSettings"
    ), "legacy global settings should report the fallback source"
    assert forecaster.recent_profile_lookback_minutes == 90
    assert forecaster.recent_profile_continuous_factor == pytest.approx(0.35)
    assert forecaster.recent_profile_active_threshold_w == pytest.approx(650)
    assert "falling back to legacy global" in caplog.text


def test_recent_profile_uses_defaults_without_strategy_or_global_settings(caplog):
    cfg = _base_cfg({"ECM63.1": _asset()})

    with caplog.at_level(logging.WARNING):
        forecaster = _forecaster(cfg)

    assert forecaster.recent_profile_settings_source == "defaults"
    assert forecaster.recent_profile_lookback_minutes == 120
    assert forecaster.recent_profile_quantile == pytest.approx(0.25)
    assert forecaster.recent_profile_continuous_factor == pytest.approx(0.5)
    assert forecaster.recent_profile_discrete_factor == pytest.approx(1.0)
    assert forecaster.recent_profile_active_threshold_w == pytest.approx(500)
    assert forecaster.recent_profile_min_samples == 2
    assert "using built-in defaults" in caplog.text


def test_active_ev_charger_uses_q25_recent_profile_and_continuous_factor():
    cfg = _base_cfg(
        {"ECM63.1": _asset()},
        recent_settings={
            "lookbackMinutes": 120,
            "quantile": 0.25,
            "continuousFactor": 0.5,
            "activeThresholdW": 500,
        },
    )
    forecaster = _forecaster(cfg)
    _install_measurements(
        forecaster,
        {"ECM63.1": _series([4000, 8000, 12000, 16000])},
        {"ECM63.1": 8000},
    )

    breakdown = forecaster.get_asset_flexibility_breakdown(
        SLOT_TIME,
        asset_ids=["ECM63.1"],
        current_time_utc=CURRENT_TIME,
    )

    info = breakdown["ECM63.1"]
    assert info["recent_profile_expected_power_w"] == pytest.approx(7000)
    assert info["available_flexibility_kw"] == pytest.approx(3.5)
    assert info["is_available_for_flexibility"] is True
    assert info["estimation_method"] == "recent_profile"


def test_inactive_ev_charger_reports_zero_flexibility():
    cfg = _base_cfg({"ECM63.1": _asset()})
    forecaster = _forecaster(cfg)
    _install_measurements(
        forecaster,
        {"ECM63.1": _series([4000, 8000, 12000, 16000])},
        {"ECM63.1": 200},
    )

    info = forecaster.get_asset_flexibility_breakdown(
        SLOT_TIME,
        asset_ids=["ECM63.1"],
        current_time_utc=CURRENT_TIME,
    )["ECM63.1"]

    assert info["is_currently_active"] is False
    assert info["available_flexibility_kw"] == pytest.approx(0.0)
    assert info["recent_profile_skip_reason"] == "inactive"


def test_recent_profile_expected_power_is_capped_at_nominal_power():
    cfg = _base_cfg({"ECM63.1": _asset(nominal_power_w=9000, capacity_kw=9.0)})
    forecaster = _forecaster(cfg)
    _install_measurements(
        forecaster,
        {"ECM63.1": _series([12000, 14000, 16000, 18000])},
        {"ECM63.1": 10000},
    )

    info = forecaster.get_asset_flexibility_breakdown(
        SLOT_TIME,
        asset_ids=["ECM63.1"],
        current_time_utc=CURRENT_TIME,
    )["ECM63.1"]

    assert info["recent_profile_expected_power_w"] == pytest.approx(9000)
    assert info["available_flexibility_kw"] == pytest.approx(4.5)


@pytest.mark.parametrize(
    "recent_series,current_power,age_minutes,expected_reason",
    [
        (_series([7000]), 7000, 0, "insufficient_samples"),
        (_series([7000, 9000]), 7000, 45, None),
        (pd.Series(dtype=float), 7000, 0, "insufficient_samples"),
    ],
)
def test_recent_profile_skips_safely_for_missing_or_unusable_measurements(
    recent_series,
    current_power,
    age_minutes,
    expected_reason,
):
    cfg = _base_cfg({"ECM63.1": _asset()})
    forecaster = _forecaster(cfg)
    _install_measurements(
        forecaster,
        {"ECM63.1": recent_series},
        {"ECM63.1": current_power},
        {"ECM63.1": age_minutes},
    )

    breakdown = forecaster.get_asset_flexibility_breakdown(
        SLOT_TIME,
        asset_ids=["ECM63.1"],
        current_time_utc=CURRENT_TIME,
    )

    if age_minutes > forecaster.max_current_measurement_age_minutes:
        assert breakdown == {}, "stale current measurements should skip the asset"
    else:
        info = breakdown["ECM63.1"]
        assert info["available_flexibility_kw"] == pytest.approx(0.0)
        assert info["is_available_for_flexibility"] is False
        assert info["recent_profile_skip_reason"] == expected_reason


@pytest.mark.parametrize(
    "modulation_type,settings,expected_factor,expected_kw",
    [
        ("continuous", {"continuousFactor": 0.5, "discreteFactor": 1.0}, 0.5, 3.5),
        ("discrete", {"continuousFactor": 0.5, "discreteFactor": 0.8}, 0.8, 5.6),
    ],
)
def test_recent_profile_applies_modulation_specific_factor(
    modulation_type,
    settings,
    expected_factor,
    expected_kw,
):
    asset_type = "heat_pump" if modulation_type == "discrete" else "ev_charger"
    cfg = _base_cfg(
        {"ASSET": _asset(asset_type=asset_type, modulation_type=modulation_type)},
        recent_settings={"lookbackMinutes": 120, "quantile": 0.25, **settings},
    )
    forecaster = _forecaster(cfg)
    _install_measurements(
        forecaster,
        {"ASSET": _series([4000, 8000, 12000, 16000])},
        {"ASSET": 8000},
    )

    info = forecaster.get_asset_flexibility_breakdown(
        SLOT_TIME,
        asset_ids=["ASSET"],
        current_time_utc=CURRENT_TIME,
    )["ASSET"]

    assert info["flexibility_factor"] == pytest.approx(expected_factor)
    assert info["available_flexibility_kw"] == pytest.approx(expected_kw)


def test_recent_profile_unknown_modulation_type_warns_and_uses_continuous_factor(caplog):
    cfg = _base_cfg(
        {"ASSET": _asset(modulation_type="stepped")},
        recent_settings={"continuousFactor": 0.5, "discreteFactor": 1.0},
    )
    forecaster = _forecaster(cfg)
    _install_measurements(
        forecaster,
        {"ASSET": _series([4000, 8000, 12000, 16000])},
        {"ASSET": 8000},
    )

    with caplog.at_level(logging.WARNING):
        info = forecaster.get_asset_flexibility_breakdown(
            SLOT_TIME,
            asset_ids=["ASSET"],
            current_time_utc=CURRENT_TIME,
        )["ASSET"]

    assert "Unknown modulation_type=stepped" in caplog.text
    assert info["modulation_type"] == "continuous"
    assert info["available_flexibility_kw"] == pytest.approx(3.5)


def test_strategy_10_portfolio_sums_evs_and_filters_out_heat_pumps():
    cfg = _base_cfg(
        {
            "ECM63.1": _asset(description="EV 1"),
            "ECM63.2": _asset(description="EV 2"),
            "ECM96.2": _asset(
                asset_type="heat_pump",
                modulation_type="discrete",
                description="HP excluded",
            ),
        }
    )
    strategy = StrategyManager(cfg).get_strategy("strategy_10")
    forecaster = _forecaster(cfg)
    _install_measurements(
        forecaster,
        {
            "ECM63.1": _series([4000, 8000, 12000, 16000]),
            "ECM63.2": _series([2000, 6000, 10000, 14000]),
            "ECM96.2": _series([10000, 10000, 10000, 10000]),
        },
        {"ECM63.1": 8000, "ECM63.2": 7000, "ECM96.2": 10000},
    )

    result = forecaster.get_achievable_flexibility(
        SLOT_TIME,
        target_kw=11.0,
        allowed_assets=strategy.allowed_assets,
        asset_ids=["ECM63.1", "ECM63.2", "ECM96.2"],
        current_time_utc=CURRENT_TIME,
    )

    assert strategy.allowed_assets == ["ECM63.1", "ECM63.2"]
    assert "ECM96.2" not in result["asset_breakdown"]
    assert result["total_achievable_range_kw"][1] == pytest.approx(6.0)
    assert result["recommended_bid_kw"] == pytest.approx(6.0)
    assert result["recommended_bid_kw"] <= 11.0


def test_strategy_10_bid_record_assets_use_selected_recent_profile_allocation_only():
    contexts = {
        "portfolio-1": {
            "asset_breakdown": {
                "ECM63.1": {
                    "description": "EV 1",
                    "asset_type": "ev_charger",
                    "baseline_power_w": 7000.0,
                    "available_flexibility_kw": 3.5,
                    "flexibility_factor": 0.5,
                    "estimation_method": "recent_profile",
                },
                "ECM63.2": {
                    "description": "EV 2",
                    "asset_type": "ev_charger",
                    "baseline_power_w": 5000.0,
                    "available_flexibility_kw": 2.5,
                    "flexibility_factor": 0.5,
                    "estimation_method": "recent_profile",
                },
            },
            "achievable_details": {
                "recommended_allocation": {
                    "discrete": {},
                    "continuous": {
                        "ECM63.1": {"power_kw": 2.917, "available_flex_kw": 3.5},
                        "ECM63.2": {"power_kw": 2.083, "available_flex_kw": 2.5},
                    },
                    "continuous_total_kw": 5.0,
                }
            },
        }
    }

    assets = build_persistence_assets_to_activate(contexts)

    assert assets == [
        {
            "asset_id": "ECM63.1",
            "description": "EV 1",
            "asset_type": "ev_charger",
            "available_flexibility_kw": 2.917,
            "flexibility_factor": 0.5,
            "reference_power_kw": 7.0,
            "reference_power_source": "recent_profile_baseline",
        },
        {
            "asset_id": "ECM63.2",
            "description": "EV 2",
            "asset_type": "ev_charger",
            "available_flexibility_kw": 2.083,
            "flexibility_factor": 0.5,
            "reference_power_kw": 5.0,
            "reference_power_source": "recent_profile_baseline",
        },
    ]


def test_persistence_bid_record_assets_keep_reference_power_null_for_legacy_methods():
    contexts = {
        "portfolio-1": {
            "asset_breakdown": {
                "ECM63.1": {
                    "description": "EV 1",
                    "asset_type": "ev_charger",
                    "baseline_power_w": 7000.0,
                    "available_flexibility_kw": 3.5,
                    "flexibility_factor": 0.5,
                    "estimation_method": "persistence",
                },
            },
            "achievable_details": {
                "recommended_allocation": {
                    "discrete": {},
                    "continuous": {
                        "ECM63.1": {"power_kw": 2.917, "available_flex_kw": 3.5},
                    },
                    "continuous_total_kw": 2.917,
                }
            },
        }
    }

    assets = build_persistence_assets_to_activate(contexts)

    assert assets[0]["reference_power_kw"] is None
    assert assets[0]["reference_power_source"] is None


def test_recent_profile_gated_path_disallows_overdelivery():
    cfg = _base_cfg(
        {
            "HP": _asset(
                asset_type="heat_pump",
                modulation_type="discrete",
                nominal_power_w=10000,
                capacity_kw=10.0,
            )
        }
    )
    forecaster = _forecaster(cfg)
    _install_measurements(
        forecaster,
        {"HP": _series([10000, 10000, 10000, 10000])},
        {"HP": 10000},
    )

    result = forecaster.get_achievable_flexibility(
        SLOT_TIME,
        target_kw=8.0,
        asset_ids=["HP"],
        current_time_utc=CURRENT_TIME,
    )

    assert result["recommended_bid_kw"] == pytest.approx(0.0)
    assert result["recommended_allocation"]["discrete"] == {}


def test_strategy_10_method_resolves_to_gated_recent_profile():
    strategy = types.SimpleNamespace(
        strategy_id="strategy_10",
        config={"flexibility_method": "recent-profile"},
    )

    assert (
        resolve_strategy_flexibility_method(strategy, logging.getLogger(__name__))
        == "recent_profile"
    )


def test_recent_profile_breakdown_includes_logging_metadata():
    cfg = _base_cfg(
        {"ECM63.1": _asset()},
        recent_settings={
            "lookbackMinutes": 120,
            "quantile": 0.25,
            "continuousFactor": 0.5,
            "minSamples": 2,
            "activeThresholdW": 500,
        },
    )
    forecaster = _forecaster(cfg)
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
    assert info["recent_profile_positive_sample_count"] == 4
    assert info["recent_profile_zero_sample_count"] == 0
    assert info["target_slot_utc"] == "2026-05-26T11:00:00Z"
    assert info["lookback_window_end_utc"] == "2026-05-26T10:00:00Z"
    assert info["aggregation_resolution_minutes"] == 15
    assert info["recent_profile_min_samples"] == 2
    assert "available_flex_kw = 0.500 × 7.00 = 3.50 kW" in info[
        "recent_profile_available_flex_explanation"
    ]


def test_recent_profile_zero_reference_while_currently_charging():
    """ECM63.2-style case: active charging but q25 reference stays zero."""
    cfg = _base_cfg(
        {
            "ECM63.2": _asset(description="ECM63.2 charger"),
        },
        recent_settings={
            "lookbackMinutes": 120,
            "quantile": 0.25,
            "continuousFactor": 0.5,
            "activeThresholdW": 500,
        },
    )
    forecaster = _forecaster(cfg)
    _install_measurements(
        forecaster,
        {
            "ECM63.2": _series(
                [0, 0, 0, 0, 0, 10920, 10920, 10920],
                step_minutes=15,
            )
        },
        {"ECM63.2": 10920},
    )

    info = forecaster.get_asset_flexibility_breakdown(
        SLOT_TIME,
        asset_ids=["ECM63.2"],
        current_time_utc=CURRENT_TIME,
    )["ECM63.2"]

    assert info["is_currently_active"] is True
    assert info["recent_profile_expected_power_w"] == pytest.approx(0.0)
    assert info["available_flexibility_kw"] == pytest.approx(0.0)
    assert info["recent_profile_positive_sample_count"] == 3
    assert info["recent_profile_zero_sample_count"] == 5
    assert "q25 over the last 120 min is zero" in info[
        "recent_profile_available_flex_explanation"
    ]
    assert "No bid flexibility is assigned" in info[
        "recent_profile_available_flex_explanation"
    ]

    log_lines = format_recent_profile_asset_log_lines("ECM63.2", info)
    joined = "\n".join(log_lines)
    assert "Recent-profile reference calculation for ECM63.2" in joined
    assert "positive_samples=3" in joined
    assert "zero_samples=5" in joined
    assert "q25_reference=0.00 kW" in joined
    assert "current_power=10.92 kW" in joined


def test_recent_profile_available_flex_explanation_helper():
    explanation = build_recent_profile_available_flex_explanation(
        "ECM63.2",
        skip_reason=None,
        is_currently_active=True,
        current_measured_power_w=10920,
        expected_power_w=0.0,
        available_flexibility_kw=0.0,
        modulation_factor=0.5,
        nominal_power_w=12000,
        quantile=0.25,
        lookback_minutes=120,
        positive_sample_count=3,
        sample_count=8,
        active_threshold_w=500,
    )
    assert "ECM63.2" in explanation
    assert "10.92 kW" in explanation
    assert "positive_samples=3" in explanation
    assert "total_samples=8" in explanation

