import logging
import sys
import types
from datetime import datetime
from importlib.machinery import ModuleSpec
from importlib.util import find_spec

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
    pandas_missing = find_spec("pandas") is None
except ValueError:
    pandas_missing = True
if pandas_missing:
    pandas_module = types.ModuleType("pandas")
    pandas_module.__spec__ = ModuleSpec("pandas", loader=None)
    sys.modules["pandas"] = pandas_module
else:
    pandas_module = sys.modules.get("pandas")
if pandas_module is not None:
    for attr in ("DataFrame", "Series", "Timestamp", "Timedelta", "DatetimeIndex"):
        if not hasattr(pandas_module, attr):
            setattr(pandas_module, attr, type(attr, (), {}))

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

try:
    numpy_missing = find_spec("numpy") is None
except ValueError:
    numpy_missing = True
if numpy_missing:
    numpy_stub = types.ModuleType("numpy")
    numpy_stub.__spec__ = ModuleSpec("numpy", loader=None)
    numpy_stub.mean = lambda values: sum(values) / len(values) if values else 0.0
    numpy_stub.isscalar = lambda value: isinstance(
        value, (int, float, complex, bool, str, bytes)
    )
    sys.modules["numpy"] = numpy_stub


from classes.flexibility_forecaster import FlexibilityForecaster  # noqa: E402
from scripts.trader_fsp import build_persistence_assets_to_activate  # noqa: E402


def _new_forecaster(asset_mapping, asset_flex_kw):
    forecaster = FlexibilityForecaster.__new__(FlexibilityForecaster)
    forecaster.method = "persistence"
    forecaster.asset_mapping = asset_mapping
    forecaster.logger = logging.getLogger("test_trader_fsp_persistence_bidding")

    def get_asset_flexibility_breakdown(
        period_from,
        use_temperature=True,
        asset_ids=None,
        current_time_utc=None,
    ):
        selected_assets = asset_ids or list(asset_mapping)
        breakdown = {}
        for asset_id in selected_assets:
            mapping = asset_mapping[asset_id]
            available_kw = asset_flex_kw.get(asset_id, 0.0)
            breakdown[asset_id] = {
                "description": mapping.get("description", asset_id),
                "asset_type": mapping.get("type", "unknown"),
                "nominal_capacity_kw": mapping.get("capacity_kw", available_kw),
                "typical_load_kw": available_kw,
                "flexibility_factor": 1.0,
                "available_flexibility_kw": available_kw,
                "is_available_for_flexibility": available_kw > 0,
            }
        return breakdown

    forecaster.get_asset_flexibility_breakdown = get_asset_flexibility_breakdown
    return forecaster


def _allocation(result):
    allocation = result["recommended_allocation"]
    discrete = {
        asset_id: power_kw
        for asset_id, power_kw in allocation.get("discrete", {}).items()
        if power_kw > 0
    }
    continuous = {
        asset_id: details["power_kw"]
        for asset_id, details in allocation.get("continuous", {}).items()
        if details.get("power_kw", 0) > 0
    }
    return discrete, continuous


def test_persistence_large_hp_above_4kw_target_is_not_bid():
    forecaster = _new_forecaster(
        {
            "HP": {
                "type": "heat_pump",
                "modulation_type": "discrete",
                "capacity_kw": 10.88,
            }
        },
        {"HP": 10.88},
    )

    result = forecaster.get_achievable_flexibility(
        datetime(2026, 5, 21, 10, 0), target_kw=4.0
    )

    assert result["recommended_bid_kw"] == pytest.approx(0.0)
    assert result["recommended_allocation"] == {
        "discrete": {},
        "continuous": {},
        "continuous_total_kw": 0,
    }
    assert _allocation(result) == ({}, {})


def test_persistence_large_hp_above_8kw_target_is_not_bid():
    forecaster = _new_forecaster(
        {
            "HP": {
                "type": "heat_pump",
                "modulation_type": "discrete",
                "capacity_kw": 10.88,
            }
        },
        {"HP": 10.88},
    )

    result = forecaster.get_achievable_flexibility(
        datetime(2026, 5, 21, 10, 0), target_kw=8.0
    )

    assert result["recommended_bid_kw"] == pytest.approx(0.0)
    assert result["recommended_allocation"] == {
        "discrete": {},
        "continuous": {},
        "continuous_total_kw": 0,
    }
    assert _allocation(result) == ({}, {})


def test_persistence_target_uses_ev_only_when_hp_chunk_exceeds_target():
    forecaster = _new_forecaster(
        {
            "HP": {
                "type": "heat_pump",
                "modulation_type": "discrete",
                "capacity_kw": 10.88,
            },
            "EV": {
                "type": "ev_charger",
                "modulation_type": "continuous",
                "capacity_kw": 7.0,
            },
        },
        {"HP": 10.88, "EV": 5.0},
    )

    result = forecaster.get_achievable_flexibility(
        datetime(2026, 5, 21, 10, 0), target_kw=4.0
    )

    discrete, continuous = _allocation(result)
    assert result["recommended_bid_kw"] == pytest.approx(4.0)
    assert discrete == {}
    assert continuous == pytest.approx({"EV": 4.0})


def test_persistence_small_hp_plus_ev_fills_target():
    forecaster = _new_forecaster(
        {
            "HP_SMALL": {
                "type": "heat_pump",
                "modulation_type": "discrete",
                "capacity_kw": 1.1,
            },
            "EV": {
                "type": "ev_charger",
                "modulation_type": "continuous",
                "capacity_kw": 7.0,
            },
        },
        {"HP_SMALL": 1.1, "EV": 3.0},
    )

    result = forecaster.get_achievable_flexibility(
        datetime(2026, 5, 21, 10, 0), target_kw=4.0
    )

    discrete, continuous = _allocation(result)
    assert result["recommended_bid_kw"] == pytest.approx(4.0)
    assert discrete == pytest.approx({"HP_SMALL": 1.1})
    assert continuous == pytest.approx({"EV": 2.9})


def test_persistence_large_hp_excluded_small_hp_and_ev_under_deliver():
    forecaster = _new_forecaster(
        {
            "HP_SMALL": {
                "type": "heat_pump",
                "modulation_type": "discrete",
                "capacity_kw": 1.1,
            },
            "HP_LARGE": {
                "type": "heat_pump",
                "modulation_type": "discrete",
                "capacity_kw": 10.88,
            },
            "EV": {
                "type": "ev_charger",
                "modulation_type": "continuous",
                "capacity_kw": 7.0,
            },
        },
        {"HP_SMALL": 1.1, "HP_LARGE": 10.88, "EV": 2.0},
    )

    result = forecaster.get_achievable_flexibility(
        datetime(2026, 5, 21, 10, 0), target_kw=4.0
    )

    discrete, continuous = _allocation(result)
    assert result["recommended_bid_kw"] == pytest.approx(3.1)
    assert discrete == pytest.approx({"HP_SMALL": 1.1})
    assert continuous == pytest.approx({"EV": 2.0})
    assert "HP_LARGE" not in discrete


def test_persistence_bid_assets_are_selected_allocation_only():
    contexts = {
        "portfolio-1": {
            "asset_breakdown": {
                "HP_LARGE": {
                    "description": "large heat pump",
                    "asset_type": "heat_pump",
                    "available_flexibility_kw": 10.88,
                    "flexibility_factor": 1.0,
                },
                "EV": {
                    "description": "charger",
                    "asset_type": "ev_charger",
                    "available_flexibility_kw": 5.0,
                    "flexibility_factor": 1.0,
                },
            },
            "achievable_details": {
                "recommended_allocation": {
                    "discrete": {"HP_LARGE": 0.0},
                    "continuous": {
                        "EV": {
                            "power_kw": 4.0,
                            "available_flex_kw": 5.0,
                        }
                    },
                    "continuous_total_kw": 4.0,
                }
            },
        }
    }

    assets = build_persistence_assets_to_activate(contexts)

    assert len(assets) == 1
    selected = assets[0]
    assert selected["asset_id"] == "EV"
    assert selected["description"] == "charger"
    assert selected["asset_type"] == "ev_charger"
    assert selected["available_flexibility_kw"] == pytest.approx(4.0)
    assert selected["flexibility_factor"] == pytest.approx(1.0)
    assert all(asset["asset_id"] != "HP_LARGE" for asset in assets)


def test_non_persistence_default_still_allows_overdelivery_when_closer():
    forecaster = FlexibilityForecaster.__new__(FlexibilityForecaster)

    result = forecaster._calculate_best_bid(
        target_kw=8.0,
        discrete_combos=[(0.0, {"HP": 0.0}), (10.88, {"HP": 10.88})],
        continuous_max_kw=0.0,
        continuous_assets={},
    )

    assert result["bid_kw"] == pytest.approx(10.88)
    assert result["allocation"]["discrete"] == pytest.approx({"HP": 10.88})
