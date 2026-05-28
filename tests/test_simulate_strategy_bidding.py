"""
Smoke tests for scripts/simulate_strategy_bidding.py

Verifies:
- Strategy replay execution (mocked InfluxDB)
- CSV generation with correct schema
- No write-side effects (no NODES / RabbitMQ / PostgreSQL calls)
- strategy_10 recent-profile replay path
- Historical-only visibility semantics (current_time_utc respected)
"""

import csv
import io
import logging
import os
import sys
import types
from datetime import datetime, timedelta
from importlib.machinery import ModuleSpec
from importlib.util import find_spec
from unittest.mock import MagicMock, patch

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

from classes.bidding_strategy import BiddingStrategy, StrategyManager
from classes.flexibility_forecaster import FlexibilityForecaster
from scripts.simulate_strategy_bidding import (
    build_portfolio_summary,
    generate_replay_plots,
    generate_replay_timestamps,
    parse_utc_timestamp,
    plot_portfolio_bid_timeseries,
    plot_skip_reason_counts,
    replay_single_timestamp,
    resolve_replay_asset_scope,
    write_csv,
)
from scripts.trader_fsp import resolve_strategy_flexibility_method


MINIMAL_CONFIG = {
    "fm": {
        "community": "ECM",
        "granularity": 15,
        "ordersTimeShift": 90,
        "marketName": "Test-Market",
        "actors": {
            "fsps": {
                "test_fsp": {
                    "id": "TEST",
                    "strategy": "strategy_10",
                    "assets": ["ECM96.2", "ECM97.3", "ECM63.1", "ECM63.2"],
                }
            }
        },
    },
    "asset_mapping": {
        "ECM63.1": {
            "device_name_tag": "charge_point_ev_1",
            "pod": "ECM63",
            "field": "power",
            "type": "ev_charger",
            "description": "EV Charger 1",
            "capacity_kw": 11.0,
            "nominal_power_w": 11000,
            "flexibility_factor": 1.0,
            "persistence_safety_factor": 0.5,
            "flexibility_persistence_go_back_minutes": 120,
            "modulation_type": "continuous",
            "min_power_kw": 0.0,
        },
        "ECM63.2": {
            "device_name_tag": "ECM63.2",
            "pod": "ECM63",
            "field": "power",
            "type": "ev_charger",
            "description": "EV Charger 2",
            "capacity_kw": 11.0,
            "nominal_power_w": 11000,
            "flexibility_factor": 1.0,
            "persistence_safety_factor": 0.5,
            "flexibility_persistence_go_back_minutes": 120,
            "modulation_type": "continuous",
            "min_power_kw": 0.0,
        },
        "ECM96.2": {
            "device_name_tag": "shelly_hp",
            "pod": "ECM96",
            "field": "active_power",
            "type": "heat_pump",
            "description": "HP Small",
            "capacity_kw": 4.0,
            "nominal_power_w": 7500,
            "flexibility_factor": 1.0,
            "persistence_safety_factor": 1.0,
            "flexibility_persistence_go_back_minutes": 120,
            "modulation_type": "discrete",
            "discrete_states_kw": [0.0, 4.0],
        },
        "ECM97.3": {
            "device_name_tag": "ECM97.3",
            "pod": "ECM97",
            "field": "active_power",
            "type": "heat_pump",
            "description": "HP FSP member but not strategy_10 member",
            "capacity_kw": 4.0,
            "nominal_power_w": 7500,
            "flexibility_factor": 1.0,
            "persistence_safety_factor": 1.0,
            "flexibility_persistence_go_back_minutes": 120,
            "modulation_type": "discrete",
            "discrete_states_kw": [0.0, 4.0],
        },
        "ECM68.3": {
            "device_name_tag": "ECM68.3",
            "pod": "ECM68",
            "field": "active_power",
            "type": "heat_pump",
            "description": "Other FSP HP",
            "capacity_kw": 4.0,
            "nominal_power_w": 7500,
            "flexibility_factor": 1.0,
            "persistence_safety_factor": 1.0,
            "flexibility_persistence_go_back_minutes": 120,
            "modulation_type": "discrete",
            "discrete_states_kw": [0.0, 4.0],
        },
        "ECM97.1": {
            "device_name_tag": "ECM97.1",
            "pod": "ECM97",
            "field": "active_power",
            "type": "heat_pump",
            "description": "Other FSP HP",
            "capacity_kw": 4.0,
            "nominal_power_w": 7500,
            "flexibility_factor": 1.0,
            "persistence_safety_factor": 1.0,
            "flexibility_persistence_go_back_minutes": 120,
            "modulation_type": "discrete",
            "discrete_states_kw": [0.0, 4.0],
        },
        "ECM97.2": {
            "device_name_tag": "ECM97.2",
            "pod": "ECM97",
            "field": "active_power",
            "type": "heat_pump",
            "description": "Other FSP HP",
            "capacity_kw": 4.0,
            "nominal_power_w": 7500,
            "flexibility_factor": 1.0,
            "persistence_safety_factor": 1.0,
            "flexibility_persistence_go_back_minutes": 120,
            "modulation_type": "discrete",
            "discrete_states_kw": [0.0, 4.0],
        },
    },
    "flexibility": {
        "method": "recent_profile",
        "peak_hours": {"morning": {"start": 7, "end": 10}, "evening": {"start": 16, "end": 20}},
        "historical_days_back": 7,
        "default_flexibility_factor": 0.50,
        "persistenceSettings": {
            "persistenceGoBackMinutes": 90,
            "activeThresholdW": 500,
            "defaultSafetyFactor": 1.0,
            "missingMeasurementPolicy": "skip_asset",
            "maxCurrentMeasurementAgeMinutes": 30,
        },
    },
    "bidding_strategies": {
        "strategy_10": {
            "name": "Recent-profile (test)",
            "description": "Test strategy for replay",
            "asset_types": ["ev_charger", "heat_pump"],
            "assets_filter": ["ECM63.1", "ECM63.2"],
            "flexibility_method": "recent_profile",
            "recentProfileSettings": {
                "lookbackMinutes": 120,
                "quantile": 0.25,
                "continuousFactor": 0.5,
                "discreteFactor": 1.0,
                "activeThresholdW": 500,
                "minSamples": 2,
                "missingMeasurementPolicy": "skip_asset",
            },
            "time_slots": [
                {
                    "name": "All day",
                    "start": "00:00",
                    "end": "23:59",
                    "flexibility_mw": 0.011,
                    "bid_price": 8.0,
                    "activation_cost": 2.5,
                },
            ],
        },
        "strategy_8": {
            "name": "Persistence HP (test)",
            "description": "Test persistence strategy",
            "asset_types": ["heat_pump"],
            "assets_filter": ["ECM96.2"],
            "flexibility_method": "persistence",
            "time_slots": [
                {
                    "name": "All day",
                    "start": "00:00",
                    "end": "23:59",
                    "flexibility_mw": 0.030,
                    "bid_price": 9.0,
                    "activation_cost": 2.5,
                },
            ],
        },
    },
    "influxDB": {
        "host": "localhost",
        "port": 8086,
        "user": "test",
        "password": "test",
        "database": "test",
        "ssl": False,
        "assetsMeasurement": "assets_data",
    },
}


def _make_influx_response(power_values, start_time, granularity_min=15):
    """Build a mock InfluxDB response with time series data."""
    values = []
    for i, pv in enumerate(power_values):
        ts = start_time + timedelta(minutes=i * granularity_min)
        values.append([ts.strftime("%Y-%m-%dT%H:%M:%SZ"), pv])

    class MockResult:
        raw = {
            "series": [
                {
                    "columns": ["time", "mean_power"],
                    "values": values,
                }
            ]
        }

    return MockResult()


@pytest.fixture
def logger():
    return logging.getLogger("test_replay")


@pytest.fixture
def mock_influx_client():
    client = MagicMock()
    base_time = datetime(2026, 5, 20, 8, 0)
    client.query.return_value = _make_influx_response(
        [3000, 3500, 4000, 3200, 2800, 3100, 3600, 4200],
        base_time,
    )
    return client


class TestParseTimestamp:
    def test_iso_with_z(self):
        dt = parse_utc_timestamp("2026-05-01T00:00:00Z")
        assert dt == datetime(2026, 5, 1, 0, 0, 0)

    def test_iso_without_seconds(self):
        dt = parse_utc_timestamp("2026-05-01T06:30")
        assert dt == datetime(2026, 5, 1, 6, 30)

    def test_date_only(self):
        dt = parse_utc_timestamp("2026-05-01")
        assert dt == datetime(2026, 5, 1, 0, 0)

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            parse_utc_timestamp("not-a-date")


class TestGenerateReplayTimestamps:
    def test_aligned_generation(self):
        start = datetime(2026, 5, 1, 10, 0)
        end = datetime(2026, 5, 1, 11, 0)
        ts = generate_replay_timestamps(start, end, 15)
        assert len(ts) == 4
        assert ts[0] == datetime(2026, 5, 1, 10, 0)
        assert ts[-1] == datetime(2026, 5, 1, 10, 45)

    def test_unaligned_start_is_floored(self):
        start = datetime(2026, 5, 1, 10, 7)
        end = datetime(2026, 5, 1, 11, 0)
        ts = generate_replay_timestamps(start, end, 15)
        assert ts[0] == datetime(2026, 5, 1, 10, 0)

    def test_empty_window(self):
        start = datetime(2026, 5, 1, 11, 0)
        end = datetime(2026, 5, 1, 10, 0)
        ts = generate_replay_timestamps(start, end, 15)
        assert ts == []


class TestReplayAssetScope:
    def test_strategy_10_scope_intersects_fsp_assets_and_strategy_filter(self, logger):
        strategy_mgr = StrategyManager(MINIMAL_CONFIG, logger)
        strategy = strategy_mgr.get_strategy("strategy_10")

        scope = resolve_replay_asset_scope(
            fsp_identifier="test_fsp",
            fsp_config=MINIMAL_CONFIG["fm"]["actors"]["fsps"]["test_fsp"],
            strategy_id="strategy_10",
            strategy=strategy,
            main_cfg=MINIMAL_CONFIG,
            logger=logger,
        )

        assert scope["fsp_assets"] == ["ECM96.2", "ECM97.3", "ECM63.1", "ECM63.2"]
        assert scope["strategy_assets"] == ["ECM63.1", "ECM63.2"]
        assert scope["replay_assets_used"] == ["ECM63.1", "ECM63.2"]
        assert scope["excluded_assets"] == ["ECM96.2", "ECM97.3"]

    def test_strategy_10_scoped_replay_does_not_query_non_fsp_or_excluded_assets(
        self, logger, mock_influx_client
    ):
        strategy_mgr = StrategyManager(MINIMAL_CONFIG, logger)
        strategy = strategy_mgr.get_strategy("strategy_10")
        method = resolve_strategy_flexibility_method(strategy, logger)
        scope = resolve_replay_asset_scope(
            fsp_identifier="test_fsp",
            fsp_config=MINIMAL_CONFIG["fm"]["actors"]["fsps"]["test_fsp"],
            strategy_id="strategy_10",
            strategy=strategy,
            main_cfg=MINIMAL_CONFIG,
            logger=logger,
        )

        flex_forecaster = FlexibilityForecaster(
            MINIMAL_CONFIG,
            mock_influx_client,
            logger,
            method_override=method,
            strategy_config=strategy.config,
            strategy_id="strategy_10",
        )

        results = replay_single_timestamp(
            replay_time_utc=datetime(2026, 5, 20, 10, 0),
            strategy=strategy,
            strategy_id="strategy_10",
            flex_forecaster=flex_forecaster,
            fsp_config=MINIMAL_CONFIG["fm"]["actors"]["fsps"]["test_fsp"],
            main_cfg=MINIMAL_CONFIG,
            orders_time_shift=90,
            granularity=15,
            logger=logger,
            replay_asset_ids=scope["replay_assets_used"],
        )

        queried_sql = "\n".join(call_args[0][0] for call_args in mock_influx_client.query.call_args_list)
        assert "ECM68" not in queried_sql
        assert "ECM68.3" not in queried_sql
        assert "ECM97.1" not in queried_sql
        assert "ECM97.2" not in queried_sql
        assert "ECM97.3" not in queried_sql
        assert "ECM96" not in queried_sql

        result_asset_ids = {r["asset_id"] for r in results if not r["asset_id"].startswith("_")}
        assert result_asset_ids == {"ECM63.1", "ECM63.2"}

    def test_scoped_summary_counts_ignore_excluded_assets(self, logger, mock_influx_client):
        strategy_mgr = StrategyManager(MINIMAL_CONFIG, logger)
        strategy = strategy_mgr.get_strategy("strategy_10")
        method = resolve_strategy_flexibility_method(strategy, logger)
        scope = resolve_replay_asset_scope(
            fsp_identifier="test_fsp",
            fsp_config=MINIMAL_CONFIG["fm"]["actors"]["fsps"]["test_fsp"],
            strategy_id="strategy_10",
            strategy=strategy,
            main_cfg=MINIMAL_CONFIG,
            logger=logger,
        )
        flex_forecaster = FlexibilityForecaster(
            MINIMAL_CONFIG,
            mock_influx_client,
            logger,
            method_override=method,
            strategy_config=strategy.config,
            strategy_id="strategy_10",
        )

        results = replay_single_timestamp(
            replay_time_utc=datetime(2026, 5, 20, 10, 0),
            strategy=strategy,
            strategy_id="strategy_10",
            flex_forecaster=flex_forecaster,
            fsp_config=MINIMAL_CONFIG["fm"]["actors"]["fsps"]["test_fsp"],
            main_cfg=MINIMAL_CONFIG,
            orders_time_shift=90,
            granularity=15,
            logger=logger,
            replay_asset_ids=scope["replay_assets_used"],
        )
        summary = build_portfolio_summary(results)

        assert len(summary) == 1
        assert summary[0]["number_active_assets"] <= 2
        assert summary[0]["number_skipped_assets"] <= 2
        assert summary[0]["number_active_assets"] + summary[0]["number_skipped_assets"] == 2

    def test_empty_replay_scope_does_not_fall_back_to_all_assets(self, logger, mock_influx_client):
        strategy_mgr = StrategyManager(MINIMAL_CONFIG, logger)
        strategy = strategy_mgr.get_strategy("strategy_10")
        method = resolve_strategy_flexibility_method(strategy, logger)
        scope = resolve_replay_asset_scope(
            fsp_identifier="test_fsp",
            fsp_config={"assets": ["ECM96.2"]},
            strategy_id="strategy_10",
            strategy=strategy,
            main_cfg=MINIMAL_CONFIG,
            logger=logger,
        )
        assert scope["replay_assets_used"] == []

        flex_forecaster = FlexibilityForecaster(
            MINIMAL_CONFIG,
            mock_influx_client,
            logger,
            method_override=method,
            strategy_config=strategy.config,
            strategy_id="strategy_10",
        )

        results = replay_single_timestamp(
            replay_time_utc=datetime(2026, 5, 20, 10, 0),
            strategy=strategy,
            strategy_id="strategy_10",
            flex_forecaster=flex_forecaster,
            fsp_config={"assets": ["ECM96.2"]},
            main_cfg=MINIMAL_CONFIG,
            orders_time_shift=90,
            granularity=15,
            logger=logger,
            replay_asset_ids=scope["replay_assets_used"],
        )

        assert mock_influx_client.query.call_count == 0
        assert results == [
            {
                "replay_timestamp_utc": "2026-05-20T10:00:00Z",
                "delivery_slot_utc": "2026-05-20T11:30:00Z",
                "strategy": "strategy_10",
                "asset_id": "_portfolio",
                "modulation_type": "portfolio",
                "current_power_w": 0.0,
                "expected_power_w": 0.0,
                "available_flexibility_w": 0.0,
                "bid_quantity_w": 0.0,
                "active_threshold_w": 0.0,
                "is_currently_active": False,
                "estimation_method": "recent_profile",
                "skip_reason": "no_replay_assets",
            }
        ]

    def test_invalid_fsp_asset_fails_validation(self, logger):
        strategy_mgr = StrategyManager(MINIMAL_CONFIG, logger)
        strategy = strategy_mgr.get_strategy("strategy_10")
        bad_fsp_config = {"assets": ["ECM63.1", "MISSING_ASSET"]}

        with pytest.raises(ValueError, match="missing from asset_mapping"):
            resolve_replay_asset_scope(
                fsp_identifier="test_fsp",
                fsp_config=bad_fsp_config,
                strategy_id="strategy_10",
                strategy=strategy,
                main_cfg=MINIMAL_CONFIG,
                logger=logger,
            )

    def test_invalid_strategy_filter_asset_fails_validation(self, logger):
        bad_config = dict(MINIMAL_CONFIG)
        bad_config["bidding_strategies"] = dict(MINIMAL_CONFIG["bidding_strategies"])
        bad_strategy_10 = dict(MINIMAL_CONFIG["bidding_strategies"]["strategy_10"])
        bad_strategy_10["assets_filter"] = ["ECM63.1", "MISSING_STRATEGY_ASSET"]
        bad_config["bidding_strategies"]["strategy_10"] = bad_strategy_10
        strategy_mgr = StrategyManager(bad_config, logger)
        strategy = strategy_mgr.get_strategy("strategy_10")

        with pytest.raises(ValueError, match="assets_filter"):
            resolve_replay_asset_scope(
                fsp_identifier="test_fsp",
                fsp_config=bad_config["fm"]["actors"]["fsps"]["test_fsp"],
                strategy_id="strategy_10",
                strategy=strategy,
                main_cfg=bad_config,
                logger=logger,
            )

    def test_missing_fsp_assets_falls_back_for_backward_compatibility(self, logger):
        strategy_mgr = StrategyManager(MINIMAL_CONFIG, logger)
        strategy = strategy_mgr.get_strategy("strategy_8")

        scope = resolve_replay_asset_scope(
            fsp_identifier="legacy_fsp",
            fsp_config={"id": "LEGACY", "strategy": "strategy_8"},
            strategy_id="strategy_8",
            strategy=strategy,
            main_cfg=MINIMAL_CONFIG,
            logger=logger,
        )

        assert scope["fsp_assets"] == ["ECM96.2"]
        assert scope["replay_assets_used"] == ["ECM96.2"]


class TestReplaySingleTimestamp:
    def test_strategy_10_recent_profile_path(self, logger, mock_influx_client):
        """Verify that strategy_10 uses recent_profile method in replay."""
        strategy_mgr = StrategyManager(MINIMAL_CONFIG, logger)
        strategy = strategy_mgr.get_strategy("strategy_10")
        assert strategy is not None

        method = resolve_strategy_flexibility_method(strategy, logger)
        assert method == "recent_profile"

        flex_forecaster = FlexibilityForecaster(
            MINIMAL_CONFIG,
            mock_influx_client,
            logger,
            method_override=method,
            strategy_config=strategy.config,
            strategy_id="strategy_10",
        )
        assert flex_forecaster.method == "recent_profile"

        replay_time = datetime(2026, 5, 20, 10, 0)
        results = replay_single_timestamp(
            replay_time_utc=replay_time,
            strategy=strategy,
            strategy_id="strategy_10",
            flex_forecaster=flex_forecaster,
            fsp_config=MINIMAL_CONFIG["fm"]["actors"]["fsps"]["test_fsp"],
            main_cfg=MINIMAL_CONFIG,
            orders_time_shift=90,
            granularity=15,
            logger=logger,
        )

        assert len(results) > 0
        portfolio_rows = [r for r in results if r["asset_id"] == "_portfolio"]
        assert len(portfolio_rows) == 1
        assert results[0]["strategy"] == "strategy_10"
        assert results[0]["replay_timestamp_utc"] == "2026-05-20T10:00:00Z"

    def test_persistence_path(self, logger, mock_influx_client):
        """Verify that strategy_8 uses persistence method in replay."""
        strategy_mgr = StrategyManager(MINIMAL_CONFIG, logger)
        strategy = strategy_mgr.get_strategy("strategy_8")
        assert strategy is not None

        method = resolve_strategy_flexibility_method(strategy, logger)
        assert method == "persistence"

        flex_forecaster = FlexibilityForecaster(
            MINIMAL_CONFIG,
            mock_influx_client,
            logger,
            method_override=method,
            strategy_config=strategy.config,
            strategy_id="strategy_8",
        )

        replay_time = datetime(2026, 5, 20, 10, 0)
        results = replay_single_timestamp(
            replay_time_utc=replay_time,
            strategy=strategy,
            strategy_id="strategy_8",
            flex_forecaster=flex_forecaster,
            fsp_config=MINIMAL_CONFIG["fm"]["actors"]["fsps"]["test_fsp"],
            main_cfg=MINIMAL_CONFIG,
            orders_time_shift=90,
            granularity=15,
            logger=logger,
        )

        assert len(results) > 0
        assert results[0]["strategy"] == "strategy_8"

    def test_historical_visibility_respected(self, logger, mock_influx_client):
        """
        Verify that replay at time T only sees measurements <= T.

        The FlexibilityForecaster receives current_time_utc=replay_time,
        so the latest measurement query window is bounded by it.
        """
        strategy_mgr = StrategyManager(MINIMAL_CONFIG, logger)
        strategy = strategy_mgr.get_strategy("strategy_10")
        method = resolve_strategy_flexibility_method(strategy, logger)

        flex_forecaster = FlexibilityForecaster(
            MINIMAL_CONFIG,
            mock_influx_client,
            logger,
            method_override=method,
            strategy_config=strategy.config,
            strategy_id="strategy_10",
        )

        replay_time = datetime(2026, 5, 20, 10, 0)
        replay_single_timestamp(
            replay_time_utc=replay_time,
            strategy=strategy,
            strategy_id="strategy_10",
            flex_forecaster=flex_forecaster,
            fsp_config=MINIMAL_CONFIG["fm"]["actors"]["fsps"]["test_fsp"],
            main_cfg=MINIMAL_CONFIG,
            orders_time_shift=90,
            granularity=15,
            logger=logger,
        )

        for call_args in mock_influx_client.query.call_args_list:
            query_str = call_args[0][0]
            if "time <" in query_str:
                time_bound_part = query_str.split("time <")[1].split("'")[1]
                bound_time = datetime.strptime(time_bound_part, "%Y-%m-%dT%H:%M:%SZ")
                assert bound_time <= replay_time, (
                    f"Query has time bound {bound_time} which is after replay_time {replay_time}"
                )


class TestNoWriteSideEffects:
    def test_no_nodes_calls(self, logger, mock_influx_client):
        """Replay must not instantiate or call NODES interface."""
        strategy_mgr = StrategyManager(MINIMAL_CONFIG, logger)
        strategy = strategy_mgr.get_strategy("strategy_10")
        method = resolve_strategy_flexibility_method(strategy, logger)

        flex_forecaster = FlexibilityForecaster(
            MINIMAL_CONFIG,
            mock_influx_client,
            logger,
            method_override=method,
            strategy_config=strategy.config,
            strategy_id="strategy_10",
        )

        results = replay_single_timestamp(
            replay_time_utc=datetime(2026, 5, 20, 10, 0),
            strategy=strategy,
            strategy_id="strategy_10",
            flex_forecaster=flex_forecaster,
            fsp_config=MINIMAL_CONFIG["fm"]["actors"]["fsps"]["test_fsp"],
            main_cfg=MINIMAL_CONFIG,
            orders_time_shift=90,
            granularity=15,
            logger=logger,
        )

        assert len(results) > 0
        import scripts.simulate_strategy_bidding as replay_mod
        source = open(replay_mod.__file__).read()
        assert "nodes_interface" not in source
        assert "post_request" not in source

    def test_no_rabbitmq_calls(self, logger, mock_influx_client):
        """Replay must not import or use pika/RabbitMQ."""
        strategy_mgr = StrategyManager(MINIMAL_CONFIG, logger)
        strategy = strategy_mgr.get_strategy("strategy_10")
        method = resolve_strategy_flexibility_method(strategy, logger)

        flex_forecaster = FlexibilityForecaster(
            MINIMAL_CONFIG,
            mock_influx_client,
            logger,
            method_override=method,
            strategy_config=strategy.config,
            strategy_id="strategy_10",
        )

        import scripts.simulate_strategy_bidding as replay_mod
        source = open(replay_mod.__file__).read()
        assert "import pika" not in source
        assert "pika." not in source
        assert "post_request" not in source
        assert "sell_flexibility" not in source


class TestCsvGeneration:
    def test_write_and_read_csv(self, tmp_path):
        rows = [
            {
                "replay_timestamp_utc": "2026-05-20T10:00:00Z",
                "strategy": "strategy_10",
                "asset_id": "ECM63.1",
                "available_flexibility_w": 1500.0,
            }
        ]
        filepath = str(tmp_path / "test.csv")
        write_csv(filepath, rows, ["replay_timestamp_utc", "strategy", "asset_id", "available_flexibility_w"])

        assert os.path.isfile(filepath)
        with open(filepath) as f:
            reader = csv.DictReader(f)
            read_rows = list(reader)
        assert len(read_rows) == 1
        assert read_rows[0]["strategy"] == "strategy_10"
        assert float(read_rows[0]["available_flexibility_w"]) == 1500.0


class TestPortfolioSummary:
    def test_aggregation(self):
        asset_results = [
            {
                "replay_timestamp_utc": "2026-05-20T10:00:00Z",
                "delivery_slot_utc": "2026-05-20T11:30:00Z",
                "strategy": "strategy_10",
                "asset_id": "ECM63.1",
                "available_flexibility_w": 2000.0,
                "is_currently_active": True,
                "skip_reason": None,
                "estimation_method": "recent_profile",
            },
            {
                "replay_timestamp_utc": "2026-05-20T10:00:00Z",
                "delivery_slot_utc": "2026-05-20T11:30:00Z",
                "strategy": "strategy_10",
                "asset_id": "ECM96.2",
                "available_flexibility_w": 0.0,
                "is_currently_active": False,
                "skip_reason": "inactive",
                "estimation_method": "recent_profile",
            },
            {
                "replay_timestamp_utc": "2026-05-20T10:00:00Z",
                "delivery_slot_utc": "2026-05-20T11:30:00Z",
                "strategy": "strategy_10",
                "asset_id": "_portfolio",
                "available_flexibility_w": 2000.0,
                "bid_quantity_w": 1000.0,
                "is_currently_active": True,
                "skip_reason": None,
                "estimation_method": "recent_profile",
            },
        ]

        summary = build_portfolio_summary(asset_results)
        assert len(summary) == 1
        assert summary[0]["total_available_flexibility_w"] == 2000.0
        assert summary[0]["total_bid_quantity_w"] == 1000.0
        assert summary[0]["number_active_assets"] == 1
        assert summary[0]["number_skipped_assets"] == 1


def _write_synthetic_replay_csvs(output_dir):
    """Write minimal replay CSVs suitable for plot generation tests."""
    asset_rows = [
        {
            "replay_timestamp_utc": "2026-05-20T10:00:00Z",
            "delivery_slot_utc": "2026-05-20T11:30:00Z",
            "strategy": "strategy_10",
            "asset_id": "ECM63.1",
            "modulation_type": "continuous",
            "current_power_w": 6000.0,
            "expected_power_w": 5000.0,
            "available_flexibility_w": 2500.0,
            "bid_quantity_w": 0.0,
            "active_threshold_w": 500.0,
            "is_currently_active": True,
            "estimation_method": "recent_profile",
            "skip_reason": None,
        },
        {
            "replay_timestamp_utc": "2026-05-20T10:15:00Z",
            "delivery_slot_utc": "2026-05-20T11:45:00Z",
            "strategy": "strategy_10",
            "asset_id": "ECM63.1",
            "modulation_type": "continuous",
            "current_power_w": 7000.0,
            "expected_power_w": 5200.0,
            "available_flexibility_w": 2600.0,
            "bid_quantity_w": 0.0,
            "active_threshold_w": 500.0,
            "is_currently_active": True,
            "estimation_method": "recent_profile",
            "skip_reason": None,
        },
        {
            "replay_timestamp_utc": "2026-05-20T10:00:00Z",
            "delivery_slot_utc": "2026-05-20T11:30:00Z",
            "strategy": "strategy_10",
            "asset_id": "ECM96.2",
            "modulation_type": "discrete",
            "current_power_w": 0.0,
            "expected_power_w": 0.0,
            "available_flexibility_w": 0.0,
            "bid_quantity_w": 0.0,
            "active_threshold_w": 500.0,
            "is_currently_active": False,
            "estimation_method": "recent_profile",
            "skip_reason": "inactive",
        },
        {
            "replay_timestamp_utc": "2026-05-20T10:00:00Z",
            "delivery_slot_utc": "2026-05-20T11:30:00Z",
            "strategy": "strategy_10",
            "asset_id": "_portfolio",
            "modulation_type": "portfolio",
            "current_power_w": 6000.0,
            "expected_power_w": 5000.0,
            "available_flexibility_w": 2500.0,
            "bid_quantity_w": 2000.0,
            "active_threshold_w": 0.0,
            "is_currently_active": True,
            "estimation_method": "recent_profile",
            "skip_reason": None,
        },
    ]
    portfolio_rows = [
        {
            "replay_timestamp_utc": "2026-05-20T10:00:00Z",
            "delivery_slot_utc": "2026-05-20T11:30:00Z",
            "strategy": "strategy_10",
            "total_bid_quantity_w": 2000.0,
            "total_available_flexibility_w": 2500.0,
            "number_active_assets": 1,
            "number_skipped_assets": 1,
            "estimation_method": "recent_profile",
        },
        {
            "replay_timestamp_utc": "2026-05-20T10:15:00Z",
            "delivery_slot_utc": "2026-05-20T11:45:00Z",
            "strategy": "strategy_10",
            "total_bid_quantity_w": 2100.0,
            "total_available_flexibility_w": 2600.0,
            "number_active_assets": 1,
            "number_skipped_assets": 0,
            "estimation_method": "recent_profile",
        },
    ]

    asset_fields = [
        "replay_timestamp_utc", "delivery_slot_utc", "strategy", "asset_id",
        "modulation_type", "current_power_w", "expected_power_w",
        "available_flexibility_w", "bid_quantity_w", "active_threshold_w",
        "is_currently_active", "estimation_method", "skip_reason",
    ]
    portfolio_fields = [
        "replay_timestamp_utc", "delivery_slot_utc", "strategy",
        "total_bid_quantity_w", "total_available_flexibility_w",
        "number_active_assets", "number_skipped_assets", "estimation_method",
    ]

    write_csv(os.path.join(output_dir, "replay_asset_detail.csv"), asset_rows, asset_fields)
    write_csv(
        os.path.join(output_dir, "replay_portfolio_summary.csv"),
        portfolio_rows,
        portfolio_fields,
    )
    return asset_rows, portfolio_rows


class TestReplayPlots:
    def test_generate_replay_plots_creates_expected_pngs(self, tmp_path, logger):
        _write_synthetic_replay_csvs(str(tmp_path))

        plot_paths = generate_replay_plots(str(tmp_path), logger)
        expected_names = {
            "portfolio_bid_timeseries.png",
            "active_skipped_assets_timeseries.png",
            "asset_flexibility_timeseries.png",
            "asset_current_vs_expected_power.png",
            "skip_reason_counts.png",
        }

        assert expected_names.issubset({os.path.basename(path) for path in plot_paths})
        for name in expected_names:
            plot_file = tmp_path / name
            assert plot_file.is_file()
            assert plot_file.stat().st_size > 0

    def test_plotting_does_not_modify_csv_output(self, tmp_path, logger):
        asset_path = tmp_path / "replay_asset_detail.csv"
        portfolio_path = tmp_path / "replay_portfolio_summary.csv"
        _write_synthetic_replay_csvs(str(tmp_path))

        asset_before = asset_path.read_text()
        portfolio_before = portfolio_path.read_text()

        generate_replay_plots(str(tmp_path), logger)

        assert asset_path.read_text() == asset_before
        assert portfolio_path.read_text() == portfolio_before

    def test_empty_plot_data_does_not_crash(self, tmp_path, logger):
        write_csv(
            str(tmp_path / "replay_asset_detail.csv"),
            [],
            ["replay_timestamp_utc", "asset_id", "skip_reason"],
        )
        write_csv(
            str(tmp_path / "replay_portfolio_summary.csv"),
            [],
            [
                "replay_timestamp_utc",
                "strategy",
                "total_bid_quantity_w",
                "total_available_flexibility_w",
                "number_active_assets",
                "number_skipped_assets",
            ],
        )

        plot_paths = generate_replay_plots(str(tmp_path), logger)
        assert plot_paths == []

    def test_missing_matplotlib_returns_empty_list(self, tmp_path, logger, monkeypatch):
        _write_synthetic_replay_csvs(str(tmp_path))

        def _raise_import_error():
            raise ImportError("matplotlib missing")

        monkeypatch.setattr(
            "scripts.simulate_strategy_bidding._load_plotting_backend",
            _raise_import_error,
        )

        plot_paths = generate_replay_plots(str(tmp_path), logger)
        assert plot_paths == []

    def test_skip_reason_plot_skips_when_no_reasons(self, tmp_path, logger):
        try:
            from scripts.simulate_strategy_bidding import _load_plotting_backend
            plt, _mdates = _load_plotting_backend()
        except ImportError:
            pytest.skip("matplotlib not available")

        asset_df = pd.DataFrame([
            {
                "replay_timestamp_utc": "2026-05-20T10:00:00Z",
                "asset_id": "ECM63.1",
                "skip_reason": None,
            }
        ])

        result = plot_skip_reason_counts(asset_df, str(tmp_path), plt, logger)
        assert result is None

    def test_replay_without_plots_does_not_require_matplotlib(self, monkeypatch):
        """Replay helpers work when matplotlib is only loaded on demand for --plots."""
        def _raise_import_error():
            raise ImportError("matplotlib missing")

        monkeypatch.setattr(
            "scripts.simulate_strategy_bidding._load_plotting_backend",
            _raise_import_error,
        )

        timestamps = generate_replay_timestamps(
            datetime(2026, 5, 20, 10, 0),
            datetime(2026, 5, 20, 11, 0),
            15,
        )
        assert len(timestamps) == 4

        import scripts.simulate_strategy_bidding as replay_mod
        source = open(replay_mod.__file__).read()
        assert "matplotlib.use(" not in source.split("def _load_plotting_backend", 1)[0]
