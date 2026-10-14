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


from classes.asset_commitment_coordinator import ActivationSequencePolicy  # noqa: E402
from classes.flexibility_forecaster import FlexibilityForecaster  # noqa: E402
from scripts.trader_fsp import (  # noqa: E402
    build_persistence_assets_to_activate,
    coordinate_strategy_assets_for_bidding,
)


class FakeSequenceCoordinator:
    def __init__(self, decisions=None, error=None):
        self.decisions = decisions or {}
        self.error = error
        self.calls = []

    def evaluate_assets(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.decisions

    @staticmethod
    def eligible_assets(decisions):
        return [
            asset_id
            for asset_id, decision in decisions.items()
            if decision.eligible
        ]


def _sequence_decisions(target_slot):
    policy = ActivationSequencePolicy(15, 2, 2)
    previous = target_slot.replace(minute=target_slot.minute - 15)
    two_slots_ago = target_slot.replace(minute=target_slot.minute - 30)
    return policy, {
        "blocked": policy.evaluate(
            "blocked", target_slot, [two_slots_ago, previous]
        ),
        "eligible": policy.evaluate("eligible", target_slot, [previous]),
    }


def test_bid_coordination_without_policy_is_a_passthrough():
    target = datetime(2026, 10, 13, 8, 30)

    assets, decisions, force = coordinate_strategy_assets_for_bidding(
        fsp_id="fsp",
        candidate_assets=["A", "A", "B"],
        target_slot=target,
        activation_sequence_policy=None,
        commitment_coordinator=None,
        force_strategy_bid=True,
        logger=logging.getLogger("test-bid-coordination-no-policy"),
    )

    assert assets == ["A", "B"]
    assert decisions == {}
    assert force is True


def test_bid_coordination_unavailable_fails_closed_and_disables_force(caplog):
    target = datetime(2026, 10, 13, 8, 30)
    policy = ActivationSequencePolicy(15, 2, 2)

    with caplog.at_level(logging.INFO):
        assets, decisions, force = coordinate_strategy_assets_for_bidding(
            fsp_id="fsp",
            candidate_assets=["A"],
            target_slot=target,
            activation_sequence_policy=policy,
            commitment_coordinator=None,
            force_strategy_bid=True,
            logger=logging.getLogger("test-bid-coordination-unavailable"),
        )

    assert assets == []
    assert decisions == {}
    assert force is False
    assert "state=ACTIVE" in caplog.text
    assert "state=FORCE_DISABLED" in caplog.text
    assert "state=UNAVAILABLE" in caplog.text


def test_bid_coordination_repository_error_fails_closed(caplog):
    target = datetime(2026, 10, 13, 8, 30)
    policy = ActivationSequencePolicy(15, 2, 2)
    coordinator = FakeSequenceCoordinator(error=RuntimeError("db down"))

    with caplog.at_level(logging.INFO):
        assets, decisions, force = coordinate_strategy_assets_for_bidding(
            fsp_id="fsp",
            candidate_assets=["A"],
            target_slot=target,
            activation_sequence_policy=policy,
            commitment_coordinator=coordinator,
            force_strategy_bid=False,
            logger=logging.getLogger("test-bid-coordination-error"),
        )

    assert assets == []
    assert decisions == {}
    assert force is False
    assert "state=ERROR" in caplog.text
    assert "action=EXCLUDE_ALL_ASSETS" in caplog.text


def test_bid_coordination_keeps_only_eligible_assets_and_logs_reduction(caplog):
    target = datetime(2026, 10, 13, 8, 30)
    policy, decisions = _sequence_decisions(target)
    coordinator = FakeSequenceCoordinator(decisions=decisions)

    with caplog.at_level(logging.INFO):
        assets, returned_decisions, force = coordinate_strategy_assets_for_bidding(
            fsp_id="fsp",
            candidate_assets=["blocked", "eligible"],
            target_slot=target,
            activation_sequence_policy=policy,
            commitment_coordinator=coordinator,
            force_strategy_bid=False,
            logger=logging.getLogger("test-bid-coordination-partial"),
        )

    assert assets == ["eligible"]
    assert returned_decisions == decisions
    assert force is False
    assert coordinator.calls[0]["asset_ids"] == ["blocked", "eligible"]
    assert "state=ASSET_POOL_REDUCED" in caplog.text


def test_bid_coordination_suppresses_bid_when_every_asset_is_blocked(caplog):
    target = datetime(2026, 10, 13, 8, 30)
    policy, decisions = _sequence_decisions(target)
    coordinator = FakeSequenceCoordinator(
        decisions={"blocked": decisions["blocked"]}
    )

    with caplog.at_level(logging.INFO):
        assets, _, _ = coordinate_strategy_assets_for_bidding(
            fsp_id="fsp",
            candidate_assets=["blocked"],
            target_slot=target,
            activation_sequence_policy=policy,
            commitment_coordinator=coordinator,
            force_strategy_bid=False,
            logger=logging.getLogger("test-bid-coordination-suppressed"),
        )

    assert assets == []
    assert "state=BID_SUPPRESSED" in caplog.text


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


def test_explicit_empty_allowed_assets_produces_zero_bid():
    forecaster = _new_forecaster(
        {
            "HP": {
                "type": "heat_pump",
                "modulation_type": "discrete",
                "capacity_kw": 6.0,
            }
        },
        {"HP": 6.0},
    )

    result = forecaster.get_achievable_flexibility(
        datetime(2026, 5, 21, 10, 0),
        target_kw=6.0,
        allowed_assets=[],
    )

    assert result["recommended_bid_kw"] == pytest.approx(0.0)
    assert result["asset_breakdown"] == {}
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
