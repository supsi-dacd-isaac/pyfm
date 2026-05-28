#!/usr/bin/env python3
"""
Historical Strategy Bidding Replay / Simulation Tool

READ-ONLY offline replay of bidding strategies over historical data.
Compares what strategy_8/9/10 (or any configured strategy) WOULD have bid
if run at each historical timestamp.

This script reuses the SAME production logic from:
  - FlexibilityForecaster (persistence, recent_profile, historical methods)
  - BiddingStrategy / StrategyManager (strategy resolution, asset filtering)
  - get_achievable_flexibility (discretization-aware allocation)
  - resolve_strategy_flexibility_method (method resolution)
  - build_persistence_assets_to_activate (allocation building)

It does NOT:
  - Publish bids or orders
  - Send RabbitMQ commands
  - Call NODES write endpoints
  - Trigger activations
  - Modify production state

Usage:
    python scripts/simulate_strategy_bidding.py \\
        --config_file conf/test_fm01_aem.json \\
        --fsp supsi01 \\
        --strategies strategy_8 strategy_9 strategy_10 \\
        --start 2026-05-01T00:00:00Z \\
        --end   2026-05-07T00:00:00Z

    python scripts/simulate_strategy_bidding.py \\
        --config_file conf/test_fm01_aem.json \\
        --fsp supsi01 \\
        --strategies strategy_10 \\
        --start 2026-05-20T06:00:00Z \\
        --end   2026-05-20T18:00:00Z \\
        --output_dir outputs/replay_20260520

    python scripts/simulate_strategy_bidding.py \\
        --config_file conf/test_fm01_aem.json \\
        --fsp supsi01 \\
        --strategies strategy_8 strategy_9 strategy_10 \\
        --start 2026-05-01T00:00:00Z \\
        --end   2026-05-07T00:00:00Z \\
        --output_dir outputs/replay_with_plots \\
        --plots
"""

import argparse
import csv
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import pandas as pd

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

INFLUXDB_AVAILABLE = True
try:
    from influxdb import InfluxDBClient
except ImportError:
    INFLUXDB_AVAILABLE = False

from classes.bidding_strategy import BiddingStrategy, StrategyManager
from classes.flexibility_forecaster import FlexibilityForecaster
from scripts.trader_fsp import (
    resolve_strategy_flexibility_method,
    get_strategy_flexibility_discrete,
)


def parse_utc_timestamp(ts_str: str) -> datetime:
    """Parse a UTC timestamp string into a naive UTC datetime."""
    ts_str = ts_str.rstrip("Z")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(ts_str, fmt)
        except ValueError:
            continue
    raise ValueError(f"Cannot parse timestamp: {ts_str}")


def generate_replay_timestamps(
    start: datetime, end: datetime, granularity_minutes: int
) -> List[datetime]:
    """Generate aligned replay timestamps between start and end."""
    timestamps = []
    current = start.replace(
        minute=(start.minute // granularity_minutes) * granularity_minutes,
        second=0,
        microsecond=0,
    )
    while current < end:
        timestamps.append(current)
        current += timedelta(minutes=granularity_minutes)
    return timestamps


def _ordered_intersection(primary_assets: List[str], candidate_assets: List[str]) -> List[str]:
    """Return assets that are in both lists, preserving the FSP-configured order."""
    candidate_set = set(candidate_assets)
    return [asset_id for asset_id in primary_assets if asset_id in candidate_set]


def _strategy_candidate_assets(strategy: BiddingStrategy, main_cfg: dict) -> List[str]:
    """
    Resolve the strategy-side replay candidates without expanding beyond strategy scope.

    If assets_filter is present, use it exactly. Otherwise, derive candidates from
    the strategy asset_types against asset_mapping, matching BiddingStrategy behavior.
    """
    asset_mapping = main_cfg.get("asset_mapping", {})
    assets_filter = getattr(strategy, "assets_filter", None)
    if assets_filter:
        return list(assets_filter)

    asset_types = set(getattr(strategy, "asset_types", []) or [])
    return [
        asset_id
        for asset_id, mapping in asset_mapping.items()
        if isinstance(mapping, dict) and mapping.get("type", "") in asset_types
    ]


def resolve_replay_asset_scope(
    fsp_identifier: str,
    fsp_config: dict,
    strategy_id: str,
    strategy: BiddingStrategy,
    main_cfg: dict,
    logger: logging.Logger,
) -> Dict[str, List[str]]:
    """
    Resolve the replay-only asset universe as FSP assets intersected with strategy assets.

    This intentionally does not modify BiddingStrategy or live trading behavior.
    """
    asset_mapping = main_cfg.get("asset_mapping", {})
    configured_fsp_assets = fsp_config.get("assets")

    if configured_fsp_assets is None:
        fsp_assets = list(getattr(strategy, "allowed_assets", []) or [])
        logger.warning(
            "FSP %s has no configured assets; replay falls back to strategy assets for backward compatibility: %s",
            fsp_identifier,
            fsp_assets,
        )
    else:
        fsp_assets = list(configured_fsp_assets)

    strategy_assets = _strategy_candidate_assets(strategy, main_cfg)

    missing_fsp_assets = sorted(asset_id for asset_id in fsp_assets if asset_id not in asset_mapping)
    if missing_fsp_assets:
        message = (
            f"FSP '{fsp_identifier}' references assets missing from asset_mapping: "
            f"{missing_fsp_assets}"
        )
        logger.error(message)
        raise ValueError(message)

    missing_strategy_filter_assets: List[str] = []
    if getattr(strategy, "assets_filter", None):
        missing_strategy_filter_assets = sorted(
            asset_id for asset_id in strategy_assets if asset_id not in asset_mapping
        )
        if missing_strategy_filter_assets:
            message = (
                f"Strategy '{strategy_id}' assets_filter references assets missing from "
                f"asset_mapping: {missing_strategy_filter_assets}"
            )
            logger.error(message)
            raise ValueError(message)

    replay_assets = _ordered_intersection(fsp_assets, strategy_assets)
    excluded_assets = sorted((set(fsp_assets) | set(strategy_assets)) - set(replay_assets))

    logger.info("Replay asset scope for FSP=%s strategy=%s", fsp_identifier, strategy_id)
    logger.info("  FSP assets:                %s", fsp_assets)
    logger.info("  Strategy candidate assets: %s", strategy_assets)
    logger.info("  Final replay assets:       %s", replay_assets)
    logger.info("  Excluded assets:           %s", excluded_assets)

    if not replay_assets:
        logger.warning(
            "No replay assets remain after intersecting FSP %s assets with strategy %s candidates",
            fsp_identifier,
            strategy_id,
        )

    return {
        "fsp_assets": fsp_assets,
        "strategy_assets": strategy_assets,
        "replay_assets_used": replay_assets,
        "excluded_assets": excluded_assets,
    }


def replay_single_timestamp(
    replay_time_utc: datetime,
    strategy: BiddingStrategy,
    strategy_id: str,
    flex_forecaster: FlexibilityForecaster,
    fsp_config: dict,
    main_cfg: dict,
    orders_time_shift: int,
    granularity: int,
    logger: logging.Logger,
    replay_asset_ids: Optional[List[str]] = None,
) -> List[Dict]:
    """
    Replay a single bidding decision at a given historical timestamp.

    This function emulates what trader_fsp.py does at one point in time:
    1. Compute the target delivery slot (replay_time + ordersTimeShift)
    2. Reconstruct the information available at replay_time
    3. Call the SAME production flexibility/bidding logic
    4. Return simulated results (no side effects)

    :param replay_time_utc: The moment the trader would have run (UTC)
    :param strategy: BiddingStrategy instance
    :param strategy_id: Strategy identifier string
    :param flex_forecaster: FlexibilityForecaster instance (reused)
    :param fsp_config: FSP-specific config
    :param main_cfg: Full configuration
    :param orders_time_shift: Minutes ahead for delivery slot
    :param granularity: Market granularity in minutes
    :param logger: Logger
    :return: List of per-asset result dicts for this timestamp
    """
    slot_time = replay_time_utc + timedelta(minutes=orders_time_shift)

    bid_params = strategy.get_bid_parameters(slot_time)
    if not bid_params.get("should_bid", False):
        return [{
            "replay_timestamp_utc": replay_time_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "delivery_slot_utc": slot_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "strategy": strategy_id,
            "asset_id": "_portfolio",
            "modulation_type": "N/A",
            "current_power_w": 0.0,
            "expected_power_w": 0.0,
            "available_flexibility_w": 0.0,
            "bid_quantity_w": 0.0,
            "active_threshold_w": 0.0,
            "is_currently_active": False,
            "estimation_method": flex_forecaster.method,
            "skip_reason": "no_bid_slot",
        }]

    if replay_asset_ids is not None and not replay_asset_ids:
        logger.warning(
            "No replay-scoped assets available for strategy %s at %s; skipping asset queries",
            strategy_id,
            replay_time_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        return [{
            "replay_timestamp_utc": replay_time_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "delivery_slot_utc": slot_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "strategy": strategy_id,
            "asset_id": "_portfolio",
            "modulation_type": "portfolio",
            "current_power_w": 0.0,
            "expected_power_w": 0.0,
            "available_flexibility_w": 0.0,
            "bid_quantity_w": 0.0,
            "active_threshold_w": 0.0,
            "is_currently_active": False,
            "estimation_method": flex_forecaster.method,
            "skip_reason": "no_replay_assets",
        }]

    strategy_flexibility_method = resolve_strategy_flexibility_method(strategy, logger)
    strategy_target_kw = bid_params["flexibility_mw"] * 1000
    if bid_params.get("ev_flexibility_mw", 0) > 0:
        strategy_target_kw += bid_params["ev_flexibility_mw"] * 1000

    portfolio_asset_ids = (
        list(replay_asset_ids)
        if replay_asset_ids is not None
        else list(flex_forecaster.asset_capacities.keys())
    )
    allowed_assets = strategy.allowed_assets if hasattr(strategy, "allowed_assets") else None

    gated_methods = {"persistence", "recent_profile"}
    is_gated = strategy_flexibility_method in gated_methods

    breakdown = flex_forecaster.get_asset_flexibility_breakdown(
        period_from=slot_time,
        use_temperature=(not is_gated),
        asset_ids=portfolio_asset_ids,
        current_time_utc=replay_time_utc,
    )

    achievable = flex_forecaster.get_achievable_flexibility(
        period_from=slot_time,
        target_kw=strategy_target_kw,
        allowed_assets=allowed_assets,
        use_temperature=(not is_gated),
        asset_ids=portfolio_asset_ids,
        current_time_utc=replay_time_utc,
    )

    recommended_bid_kw = achievable.get("recommended_bid_kw", 0.0)
    recommended_bid_w = recommended_bid_kw * 1000

    results = []
    for asset_id, info in breakdown.items():
        is_strategy_asset = asset_id in (allowed_assets or portfolio_asset_ids)
        skip_reason = None

        if not is_strategy_asset:
            skip_reason = "not_in_strategy"
        elif not info.get("is_available_for_flexibility", True) and is_gated:
            if not info.get("is_currently_active", True):
                skip_reason = "inactive"
            elif info.get("recent_profile_skip_reason"):
                skip_reason = info["recent_profile_skip_reason"]
            else:
                skip_reason = "not_available"

        results.append({
            "replay_timestamp_utc": replay_time_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "delivery_slot_utc": slot_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "strategy": strategy_id,
            "asset_id": asset_id,
            "modulation_type": flex_forecaster._get_modulation_type(asset_id),
            "current_power_w": info.get("current_measured_power_w", 0.0) or 0.0,
            "expected_power_w": info.get("baseline_power_w", 0.0) or 0.0,
            "available_flexibility_w": (info.get("available_flexibility_kw", 0.0) or 0.0) * 1000,
            "bid_quantity_w": 0.0,
            "active_threshold_w": info.get("active_threshold_w", 0.0) or 0.0,
            "is_currently_active": bool(info.get("is_currently_active", False)),
            "estimation_method": info.get("estimation_method", flex_forecaster.method),
            "skip_reason": skip_reason,
        })

    portfolio_row = {
        "replay_timestamp_utc": replay_time_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "delivery_slot_utc": slot_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "strategy": strategy_id,
        "asset_id": "_portfolio",
        "modulation_type": "portfolio",
        "current_power_w": sum(r["current_power_w"] for r in results),
        "expected_power_w": sum(r["expected_power_w"] for r in results),
        "available_flexibility_w": sum(
            r["available_flexibility_w"]
            for r in results
            if r["skip_reason"] is None
        ),
        "bid_quantity_w": recommended_bid_w,
        "active_threshold_w": 0.0,
        "is_currently_active": any(r["is_currently_active"] for r in results),
        "estimation_method": flex_forecaster.method,
        "skip_reason": None,
    }
    results.append(portfolio_row)

    return results


def build_portfolio_summary(asset_results: List[Dict]) -> List[Dict]:
    """
    Build portfolio-level summary from per-asset results.

    Groups by (replay_timestamp_utc, strategy) and summarizes.
    """
    summary_map: Dict[Tuple[str, str], Dict] = {}

    for row in asset_results:
        if row["asset_id"] == "_portfolio":
            continue
        key = (row["replay_timestamp_utc"], row["strategy"])
        if key not in summary_map:
            summary_map[key] = {
                "replay_timestamp_utc": row["replay_timestamp_utc"],
                "delivery_slot_utc": row["delivery_slot_utc"],
                "strategy": row["strategy"],
                "total_bid_quantity_w": 0.0,
                "total_available_flexibility_w": 0.0,
                "number_active_assets": 0,
                "number_skipped_assets": 0,
                "estimation_method": row["estimation_method"],
            }
        entry = summary_map[key]
        if row["skip_reason"] is None:
            entry["total_available_flexibility_w"] += row["available_flexibility_w"]
            if row["is_currently_active"]:
                entry["number_active_assets"] += 1
        else:
            entry["number_skipped_assets"] += 1

    for row in asset_results:
        if row["asset_id"] == "_portfolio":
            key = (row["replay_timestamp_utc"], row["strategy"])
            if key in summary_map:
                summary_map[key]["total_bid_quantity_w"] = row["bid_quantity_w"]

    return list(summary_map.values())


def write_csv(filepath: str, rows: List[Dict], fieldnames: List[str]) -> None:
    """Write rows to a CSV file."""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def print_replay_summary(
    asset_results: List[Dict],
    portfolio_summary: List[Dict],
    strategies: List[str],
    logger: logging.Logger,
) -> None:
    """Print a console summary of the replay results."""
    logger.info("=" * 80)
    logger.info("REPLAY SUMMARY")
    logger.info("=" * 80)

    for strat_id in strategies:
        strat_portfolio = [r for r in portfolio_summary if r["strategy"] == strat_id]
        if not strat_portfolio:
            logger.info("  %s: NO DATA", strat_id)
            continue

        bid_values = [r["total_bid_quantity_w"] for r in strat_portfolio]
        flex_values = [r["total_available_flexibility_w"] for r in strat_portfolio]
        active_counts = [r["number_active_assets"] for r in strat_portfolio]
        skipped_counts = [r["number_skipped_assets"] for r in strat_portfolio]

        avg_bid = sum(bid_values) / len(bid_values) if bid_values else 0
        max_bid = max(bid_values) if bid_values else 0
        non_zero_bids = sum(1 for v in bid_values if v > 0)
        avg_flex = sum(flex_values) / len(flex_values) if flex_values else 0
        avg_active = sum(active_counts) / len(active_counts) if active_counts else 0
        avg_skipped = sum(skipped_counts) / len(skipped_counts) if skipped_counts else 0

        logger.info("-" * 80)
        logger.info("Strategy: %s", strat_id)
        logger.info("  Timestamps replayed: %d", len(strat_portfolio))
        logger.info("  Non-zero bid slots:  %d (%.1f%%)", non_zero_bids,
                    non_zero_bids / len(strat_portfolio) * 100 if strat_portfolio else 0)
        logger.info("  Avg bid quantity:    %.1f W (%.4f kW, %.6f MW)", avg_bid, avg_bid / 1000, avg_bid / 1e6)
        logger.info("  Max bid quantity:    %.1f W (%.4f kW, %.6f MW)", max_bid, max_bid / 1000, max_bid / 1e6)
        logger.info("  Avg available flex:  %.1f W (%.4f kW)", avg_flex, avg_flex / 1000)
        logger.info("  Avg active assets:   %.1f", avg_active)
        logger.info("  Avg skipped assets:  %.1f", avg_skipped)

    logger.info("=" * 80)


def _load_plotting_backend():
    """
    Import matplotlib in headless mode.

    Called only when --plots is requested so replay works without matplotlib installed.
    """
    if "MPLCONFIGDIR" not in os.environ:
        os.environ["MPLCONFIGDIR"] = os.path.join("/tmp", "matplotlib-pyfm")

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    return plt, mdates


def _prepare_timestamp_column(df: pd.DataFrame, column: str = "replay_timestamp_utc") -> pd.DataFrame:
    prepared = df.copy()
    prepared[column] = pd.to_datetime(prepared[column], utc=True, errors="coerce")
    prepared = prepared.dropna(subset=[column])
    return prepared.sort_values(column)


def _save_figure(fig, output_path: str) -> str:
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    fig.savefig(output_path, dpi=150, bbox_inches="tight")
    return output_path


def _format_time_axis(ax, mdates_module) -> None:
    ax.xaxis.set_major_locator(mdates_module.AutoDateLocator())
    ax.xaxis.set_major_formatter(mdates_module.DateFormatter("%Y-%m-%d %H:%M"))
    for label in ax.get_xticklabels():
        label.set_rotation(30)
        label.set_ha("right")


def plot_portfolio_bid_timeseries(
    portfolio_df: pd.DataFrame,
    output_dir: str,
    plt,
    mdates_module,
    logger: logging.Logger,
) -> Optional[str]:
    """Plot portfolio bid quantity vs available flexibility over time."""
    required = {"replay_timestamp_utc", "total_bid_quantity_w", "total_available_flexibility_w"}
    if portfolio_df.empty or not required.issubset(portfolio_df.columns):
        logger.warning("Skipping portfolio_bid_timeseries.png: missing columns or no data")
        return None

    df = _prepare_timestamp_column(portfolio_df)
    if df.empty:
        logger.warning("Skipping portfolio_bid_timeseries.png: no valid timestamps")
        return None

    strategies = sorted(df["strategy"].unique()) if "strategy" in df.columns else ["portfolio"]
    fig, axes = plt.subplots(
        len(strategies), 1,
        figsize=(12, 4 * max(len(strategies), 1)),
        squeeze=False,
    )

    plotted = False
    for idx, strategy in enumerate(strategies):
        ax = axes[idx, 0]
        subset = df[df["strategy"] == strategy] if "strategy" in df.columns else df
        if subset.empty:
            continue

        ax.plot(
            subset["replay_timestamp_utc"],
            subset["total_bid_quantity_w"] / 1000.0,
            label="Total bid quantity",
            linewidth=1.5,
        )
        ax.plot(
            subset["replay_timestamp_utc"],
            subset["total_available_flexibility_w"] / 1000.0,
            label="Total available flexibility",
            linewidth=1.5,
        )
        ax.set_title(f"Portfolio bid time series — {strategy}")
        ax.set_xlabel("Replay timestamp (UTC)")
        ax.set_ylabel("Power (kW)")
        ax.grid(True, alpha=0.3)
        ax.legend()
        _format_time_axis(ax, mdates_module)
        plotted = True

    if not plotted:
        plt.close(fig)
        logger.warning("Skipping portfolio_bid_timeseries.png: no plottable rows")
        return None

    fig.tight_layout()
    output_path = os.path.join(output_dir, "portfolio_bid_timeseries.png")
    saved = _save_figure(fig, output_path)
    plt.close(fig)
    return saved


def plot_active_skipped_assets_timeseries(
    portfolio_df: pd.DataFrame,
    output_dir: str,
    plt,
    mdates_module,
    logger: logging.Logger,
) -> Optional[str]:
    """Plot active vs skipped asset counts over time."""
    required = {"replay_timestamp_utc", "number_active_assets", "number_skipped_assets"}
    if portfolio_df.empty or not required.issubset(portfolio_df.columns):
        logger.warning("Skipping active_skipped_assets_timeseries.png: missing columns or no data")
        return None

    df = _prepare_timestamp_column(portfolio_df)
    if df.empty:
        logger.warning("Skipping active_skipped_assets_timeseries.png: no valid timestamps")
        return None

    strategies = sorted(df["strategy"].unique()) if "strategy" in df.columns else ["portfolio"]
    fig, axes = plt.subplots(
        len(strategies), 1,
        figsize=(12, 4 * max(len(strategies), 1)),
        squeeze=False,
    )

    plotted = False
    for idx, strategy in enumerate(strategies):
        ax = axes[idx, 0]
        subset = df[df["strategy"] == strategy] if "strategy" in df.columns else df
        if subset.empty:
            continue

        ax.plot(
            subset["replay_timestamp_utc"],
            subset["number_active_assets"],
            label="Active assets",
            linewidth=1.5,
        )
        ax.plot(
            subset["replay_timestamp_utc"],
            subset["number_skipped_assets"],
            label="Skipped assets",
            linewidth=1.5,
        )
        ax.set_title(f"Active vs skipped assets — {strategy}")
        ax.set_xlabel("Replay timestamp (UTC)")
        ax.set_ylabel("Asset count")
        ax.grid(True, alpha=0.3)
        ax.legend()
        _format_time_axis(ax, mdates_module)
        plotted = True

    if not plotted:
        plt.close(fig)
        logger.warning("Skipping active_skipped_assets_timeseries.png: no plottable rows")
        return None

    fig.tight_layout()
    output_path = os.path.join(output_dir, "active_skipped_assets_timeseries.png")
    saved = _save_figure(fig, output_path)
    plt.close(fig)
    return saved


def plot_asset_flexibility_timeseries(
    asset_df: pd.DataFrame,
    output_dir: str,
    plt,
    mdates_module,
    logger: logging.Logger,
) -> Optional[str]:
    """Plot per-asset available flexibility over time."""
    required = {"replay_timestamp_utc", "asset_id", "available_flexibility_w"}
    if asset_df.empty or not required.issubset(asset_df.columns):
        logger.warning("Skipping asset_flexibility_timeseries.png: missing columns or no data")
        return None

    df = _prepare_timestamp_column(asset_df)
    df = df[~df["asset_id"].astype(str).str.startswith("_")]
    if df.empty:
        logger.warning("Skipping asset_flexibility_timeseries.png: no asset rows")
        return None

    strategies = sorted(df["strategy"].unique()) if "strategy" in df.columns else ["portfolio"]
    fig, axes = plt.subplots(
        len(strategies), 1,
        figsize=(12, 4.5 * max(len(strategies), 1)),
        squeeze=False,
    )

    plotted = False
    for idx, strategy in enumerate(strategies):
        ax = axes[idx, 0]
        subset = df[df["strategy"] == strategy] if "strategy" in df.columns else df
        if subset.empty:
            continue

        for asset_id, asset_rows in subset.groupby("asset_id"):
            asset_rows = asset_rows.sort_values("replay_timestamp_utc")
            ax.plot(
                asset_rows["replay_timestamp_utc"],
                asset_rows["available_flexibility_w"] / 1000.0,
                label=str(asset_id),
                linewidth=1.2,
            )

        ax.set_title(f"Per-asset available flexibility — {strategy}")
        ax.set_xlabel("Replay timestamp (UTC)")
        ax.set_ylabel("Available flexibility (kW)")
        ax.grid(True, alpha=0.3)
        ax.legend(loc="best", fontsize="small")
        _format_time_axis(ax, mdates_module)
        plotted = True

    if not plotted:
        plt.close(fig)
        logger.warning("Skipping asset_flexibility_timeseries.png: no plottable rows")
        return None

    fig.tight_layout()
    output_path = os.path.join(output_dir, "asset_flexibility_timeseries.png")
    saved = _save_figure(fig, output_path)
    plt.close(fig)
    return saved


def plot_asset_current_vs_expected_power(
    asset_df: pd.DataFrame,
    output_dir: str,
    plt,
    mdates_module,
    logger: logging.Logger,
) -> Optional[str]:
    """Plot per-asset current vs expected power over time."""
    required = {"replay_timestamp_utc", "asset_id", "current_power_w", "expected_power_w"}
    if asset_df.empty or not required.issubset(asset_df.columns):
        logger.warning("Skipping asset_current_vs_expected_power.png: missing columns or no data")
        return None

    df = _prepare_timestamp_column(asset_df)
    df = df[~df["asset_id"].astype(str).str.startswith("_")]
    if df.empty:
        logger.warning("Skipping asset_current_vs_expected_power.png: no asset rows")
        return None

    asset_ids = sorted(df["asset_id"].unique())
    n_assets = len(asset_ids)
    n_cols = 2 if n_assets > 1 else 1
    n_rows = (n_assets + n_cols - 1) // n_cols

    fig, axes = plt.subplots(
        n_rows, n_cols,
        figsize=(12, 3.5 * max(n_rows, 1)),
        squeeze=False,
    )

    plotted = False
    for index, asset_id in enumerate(asset_ids):
        row_idx = index // n_cols
        col_idx = index % n_cols
        ax = axes[row_idx, col_idx]
        asset_rows = df[df["asset_id"] == asset_id].sort_values("replay_timestamp_utc")
        if asset_rows.empty:
            continue

        ax.plot(
            asset_rows["replay_timestamp_utc"],
            asset_rows["current_power_w"] / 1000.0,
            label="Current power",
            linewidth=1.2,
        )
        ax.plot(
            asset_rows["replay_timestamp_utc"],
            asset_rows["expected_power_w"] / 1000.0,
            label="Expected power",
            linewidth=1.2,
            linestyle="--",
        )
        ax.set_title(str(asset_id))
        ax.set_xlabel("Replay timestamp (UTC)")
        ax.set_ylabel("Power (kW)")
        ax.grid(True, alpha=0.3)
        ax.legend(fontsize="small")
        _format_time_axis(ax, mdates_module)
        plotted = True

    for index in range(n_assets, n_rows * n_cols):
        row_idx = index // n_cols
        col_idx = index % n_cols
        axes[row_idx, col_idx].axis("off")

    if not plotted:
        plt.close(fig)
        logger.warning("Skipping asset_current_vs_expected_power.png: no plottable rows")
        return None

    fig.suptitle("Asset current vs expected power", y=1.02)
    fig.tight_layout()
    output_path = os.path.join(output_dir, "asset_current_vs_expected_power.png")
    saved = _save_figure(fig, output_path)
    plt.close(fig)
    return saved


def plot_skip_reason_counts(
    asset_df: pd.DataFrame,
    output_dir: str,
    plt,
    logger: logging.Logger,
) -> Optional[str]:
    """Plot counts of skip reasons across the replay window."""
    if asset_df.empty or "skip_reason" not in asset_df.columns:
        logger.warning("Skipping skip_reason_counts.png: missing columns or no data")
        return None

    df = asset_df[~asset_df["asset_id"].astype(str).str.startswith("_")].copy()
    reasons = (
        df["skip_reason"]
        .dropna()
        .astype(str)
        .str.strip()
        .replace({"": pd.NA, "None": pd.NA, "nan": pd.NA})
        .dropna()
    )
    if reasons.empty:
        logger.info("Skipping skip_reason_counts.png: no skip reasons recorded")
        return None

    counts = reasons.value_counts().sort_values(ascending=True)
    fig, ax = plt.subplots(figsize=(10, max(4, 0.4 * len(counts))))
    ax.barh(counts.index.astype(str), counts.values, color="steelblue")
    ax.set_title("Skip reason counts")
    ax.set_xlabel("Count")
    ax.set_ylabel("Skip reason")
    ax.grid(True, axis="x", alpha=0.3)
    fig.tight_layout()

    output_path = os.path.join(output_dir, "skip_reason_counts.png")
    saved = _save_figure(fig, output_path)
    plt.close(fig)
    return saved


def generate_replay_plots(output_dir: str, logger: logging.Logger) -> List[str]:
    """
    Generate optional PNG plots from replay CSV outputs.

    Plotting failures are logged and do not abort the replay run.
    """
    try:
        plt, mdates_module = _load_plotting_backend()
    except ImportError as exc:
        logger.error("matplotlib is required for --plots: %s", exc)
        print("ERROR: matplotlib is required for --plots. Install with: pip install matplotlib")
        return []

    asset_csv_path = os.path.join(output_dir, "replay_asset_detail.csv")
    portfolio_csv_path = os.path.join(output_dir, "replay_portfolio_summary.csv")

    if not os.path.isfile(asset_csv_path) or not os.path.isfile(portfolio_csv_path):
        logger.warning("Skipping plot generation: replay CSV files not found in %s", output_dir)
        return []

    asset_df = pd.read_csv(asset_csv_path)
    portfolio_df = pd.read_csv(portfolio_csv_path)

    plotters = [
        ("portfolio_bid_timeseries.png", lambda: plot_portfolio_bid_timeseries(
            portfolio_df, output_dir, plt, mdates_module, logger
        )),
        ("active_skipped_assets_timeseries.png", lambda: plot_active_skipped_assets_timeseries(
            portfolio_df, output_dir, plt, mdates_module, logger
        )),
        ("asset_flexibility_timeseries.png", lambda: plot_asset_flexibility_timeseries(
            asset_df, output_dir, plt, mdates_module, logger
        )),
        ("asset_current_vs_expected_power.png", lambda: plot_asset_current_vs_expected_power(
            asset_df, output_dir, plt, mdates_module, logger
        )),
        ("skip_reason_counts.png", lambda: plot_skip_reason_counts(
            asset_df, output_dir, plt, logger
        )),
    ]

    generated_paths: List[str] = []
    for plot_name, plotter in plotters:
        try:
            plot_path = plotter()
            if plot_path:
                generated_paths.append(plot_path)
                logger.info("Plot written: %s", plot_path)
        except Exception as exc:
            logger.warning("Failed to generate %s: %s", plot_name, exc)

    return generated_paths


def main():
    arg_parser = argparse.ArgumentParser(
        description="Historical Strategy Bidding Replay (READ-ONLY simulation)"
    )
    arg_parser.add_argument("--config_file", required=True, help="Configuration file path")
    arg_parser.add_argument("--fsp", required=True, help="FSP identifier")
    arg_parser.add_argument(
        "--strategies", nargs="+", required=True,
        help="Strategy IDs to replay (e.g. strategy_8 strategy_9 strategy_10)"
    )
    arg_parser.add_argument("--start", required=True, help="Replay start time (UTC ISO format)")
    arg_parser.add_argument("--end", required=True, help="Replay end time (UTC ISO format)")
    arg_parser.add_argument(
        "--output_dir", default=None,
        help="Output directory for CSV files (default: outputs/replay_<timestamp>)"
    )
    arg_parser.add_argument("--log_file", default=None, help="Log file (stdout if not set)")
    arg_parser.add_argument(
        "--quiet", action="store_true",
        help="Reduce per-timestamp logging to essential info only"
    )
    arg_parser.add_argument(
        "--plots",
        action="store_true",
        help="Generate PNG plots from replay CSV outputs (requires matplotlib)",
    )
    args = arg_parser.parse_args()

    logger = logging.getLogger("replay")
    logging.Formatter.converter = time.gmtime
    log_level = logging.WARNING if args.quiet else logging.INFO
    logging.basicConfig(
        format="%(asctime)sZ::%(levelname)s::%(funcName)s::%(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        level=log_level,
        filename=args.log_file,
    )

    config_file = args.config_file
    if not os.path.isfile(config_file):
        print(f"ERROR: Configuration file not found: {config_file}")
        sys.exit(1)

    cfg = json.loads(open(config_file).read())

    try:
        cfg_conns = json.loads(open(cfg["connectionsFile"]).read())
        cfg.update(cfg_conns)
    except (FileNotFoundError, KeyError) as e:
        print(f"ERROR: Cannot load connections file: {e}")
        print("InfluxDB credentials are required for historical replay.")
        sys.exit(1)

    fsp_identifier = args.fsp
    if fsp_identifier not in cfg["fm"]["actors"]["fsps"]:
        print(f"ERROR: FSP '{fsp_identifier}' not found. Available: {list(cfg['fm']['actors']['fsps'].keys())}")
        sys.exit(1)
    fsp_config = cfg["fm"]["actors"]["fsps"][fsp_identifier]

    strategy_manager = StrategyManager(cfg, logger)
    strategies_to_replay: List[Tuple[str, BiddingStrategy]] = []
    for sid in args.strategies:
        s = strategy_manager.get_strategy(sid)
        if s is None:
            print(f"ERROR: Strategy '{sid}' not found. Available: {strategy_manager.list_strategies()}")
            sys.exit(1)
        strategies_to_replay.append((sid, s))

    if not INFLUXDB_AVAILABLE:
        print("ERROR: influxdb package is not installed. Install with: pip install influxdb")
        sys.exit(1)

    influx_client = InfluxDBClient(
        host=cfg["influxDB"]["host"],
        port=cfg["influxDB"]["port"],
        password=cfg["influxDB"]["password"],
        username=cfg["influxDB"]["user"],
        database=cfg["influxDB"]["database"],
        ssl=cfg["influxDB"]["ssl"],
    )

    granularity = cfg["fm"]["granularity"]
    orders_time_shift = cfg["fm"]["ordersTimeShift"]

    start_time = parse_utc_timestamp(args.start)
    end_time = parse_utc_timestamp(args.end)
    replay_timestamps = generate_replay_timestamps(start_time, end_time, granularity)

    if not replay_timestamps:
        print("ERROR: No timestamps to replay (check --start and --end)")
        sys.exit(1)

    output_dir = args.output_dir or os.path.join(
        "outputs",
        f"replay_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}",
    )
    os.makedirs(output_dir, exist_ok=True)

    logger.info("=" * 80)
    logger.info("HISTORICAL STRATEGY BIDDING REPLAY")
    logger.info("=" * 80)
    logger.info("Config:          %s", config_file)
    logger.info("FSP:             %s", fsp_identifier)
    logger.info("Strategies:      %s", [s[0] for s in strategies_to_replay])
    logger.info("Replay window:   %s -> %s", start_time.isoformat(), end_time.isoformat())
    logger.info("Timestamps:      %d (every %d min)", len(replay_timestamps), granularity)
    logger.info("OrdersTimeShift: %d min", orders_time_shift)
    logger.info("Output dir:      %s", output_dir)
    logger.info("=" * 80)
    logger.info("NOTE: This is a READ-ONLY replay. No bids will be published.")
    logger.info("=" * 80)

    all_asset_results: List[Dict] = []
    replay_scope_by_strategy: Dict[str, Dict[str, List[str]]] = {}
    total_steps = len(replay_timestamps) * len(strategies_to_replay)
    step = 0

    for strategy_id, strategy in strategies_to_replay:
        strategy_method = resolve_strategy_flexibility_method(strategy, logger)
        logger.info("Replaying strategy %s (method=%s)...", strategy_id, strategy_method)
        try:
            replay_scope = resolve_replay_asset_scope(
                fsp_identifier=fsp_identifier,
                fsp_config=fsp_config,
                strategy_id=strategy_id,
                strategy=strategy,
                main_cfg=cfg,
                logger=logger,
            )
        except ValueError as exc:
            print(f"ERROR: {exc}")
            sys.exit(1)
        replay_scope_by_strategy[strategy_id] = replay_scope

        flex_forecaster = FlexibilityForecaster(
            cfg,
            influx_client,
            logger,
            method_override=strategy_method,
            strategy_config=strategy.config,
            strategy_id=strategy_id,
        )

        for i, replay_ts in enumerate(replay_timestamps):
            step += 1
            if not args.quiet and (i % 10 == 0 or i == len(replay_timestamps) - 1):
                logger.info(
                    "[%d/%d] %s @ %s",
                    step, total_steps, strategy_id,
                    replay_ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                )

            try:
                results = replay_single_timestamp(
                    replay_time_utc=replay_ts,
                    strategy=strategy,
                    strategy_id=strategy_id,
                    flex_forecaster=flex_forecaster,
                    fsp_config=fsp_config,
                    main_cfg=cfg,
                    orders_time_shift=orders_time_shift,
                    granularity=granularity,
                    logger=logger,
                    replay_asset_ids=replay_scope["replay_assets_used"],
                )
                all_asset_results.extend(results)
            except Exception as e:
                logger.error(
                    "Error replaying %s @ %s: %s",
                    strategy_id, replay_ts.isoformat(), str(e),
                )
                all_asset_results.append({
                    "replay_timestamp_utc": replay_ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "delivery_slot_utc": "",
                    "strategy": strategy_id,
                    "asset_id": "_error",
                    "modulation_type": "N/A",
                    "current_power_w": 0.0,
                    "expected_power_w": 0.0,
                    "available_flexibility_w": 0.0,
                    "bid_quantity_w": 0.0,
                    "active_threshold_w": 0.0,
                    "is_currently_active": False,
                    "estimation_method": "error",
                    "skip_reason": str(e),
                })

    portfolio_summary = build_portfolio_summary(all_asset_results)

    asset_fieldnames = [
        "replay_timestamp_utc",
        "delivery_slot_utc",
        "strategy",
        "asset_id",
        "modulation_type",
        "current_power_w",
        "expected_power_w",
        "available_flexibility_w",
        "bid_quantity_w",
        "active_threshold_w",
        "is_currently_active",
        "estimation_method",
        "skip_reason",
    ]

    portfolio_fieldnames = [
        "replay_timestamp_utc",
        "delivery_slot_utc",
        "strategy",
        "total_bid_quantity_w",
        "total_available_flexibility_w",
        "number_active_assets",
        "number_skipped_assets",
        "estimation_method",
    ]

    asset_csv_path = os.path.join(output_dir, "replay_asset_detail.csv")
    portfolio_csv_path = os.path.join(output_dir, "replay_portfolio_summary.csv")

    write_csv(asset_csv_path, all_asset_results, asset_fieldnames)
    write_csv(portfolio_csv_path, portfolio_summary, portfolio_fieldnames)

    logger.info("CSV outputs written:")
    logger.info("  Asset detail:      %s (%d rows)", asset_csv_path, len(all_asset_results))
    logger.info("  Portfolio summary:  %s (%d rows)", portfolio_csv_path, len(portfolio_summary))

    print_replay_summary(
        all_asset_results, portfolio_summary,
        [s[0] for s in strategies_to_replay], logger
    )

    plot_paths: List[str] = []
    if args.plots:
        plot_paths = generate_replay_plots(output_dir, logger)
        if plot_paths:
            logger.info("Plot outputs written: %d PNG file(s)", len(plot_paths))
        else:
            logger.warning("No plots were generated")

    metadata = {
        "config_file": config_file,
        "fsp": fsp_identifier,
        "strategies": [s[0] for s in strategies_to_replay],
        "start": start_time.isoformat() + "Z",
        "end": end_time.isoformat() + "Z",
        "granularity_minutes": granularity,
        "orders_time_shift_minutes": orders_time_shift,
        "timestamps_replayed": len(replay_timestamps),
        "total_asset_rows": len(all_asset_results),
        "total_portfolio_rows": len(portfolio_summary),
        "fsp_assets": (
            next(iter(replay_scope_by_strategy.values()))["fsp_assets"]
            if replay_scope_by_strategy
            else []
        ),
        "strategy_assets": {
            strategy_id: scope["strategy_assets"]
            for strategy_id, scope in replay_scope_by_strategy.items()
        },
        "replay_assets_used": {
            strategy_id: scope["replay_assets_used"]
            for strategy_id, scope in replay_scope_by_strategy.items()
        },
        "excluded_assets": {
            strategy_id: scope["excluded_assets"]
            for strategy_id, scope in replay_scope_by_strategy.items()
        },
        "plots_enabled": bool(args.plots),
        "plot_files": [os.path.basename(path) for path in plot_paths],
        "generated_at_utc": datetime.utcnow().isoformat() + "Z",
        "read_only": True,
        "side_effects": "NONE",
    }
    metadata_path = os.path.join(output_dir, "replay_metadata.json")
    with open(metadata_path, "w") as f:
        json.dump(metadata, f, indent=2)

    logger.info("Metadata: %s", metadata_path)
    logger.info("Replay complete. No bids were published. No production state was modified.")

    print(f"\nReplay complete. Output in: {output_dir}")
    print(f"  - {asset_csv_path}")
    print(f"  - {portfolio_csv_path}")
    print(f"  - {metadata_path}")
    for plot_path in plot_paths:
        print(f"  - {plot_path}")


if __name__ == "__main__":
    main()
