#!/usr/bin/env python3
"""
Strategy Evaluator - Evaluate bidding strategies for flexibility market

This script evaluates the expected performance of different bidding strategies
based on asset characteristics from the configuration file and market assumptions.

Usage:
    python strategy_evaluator.py --config_file conf/test_fm01_aem.json
    python strategy_evaluator.py --config_file conf/test_fm01_aem.json --days 30 --output results.json

Optional plotting:
    python strategy_evaluator.py --config_file conf/test_fm01_aem.json --strategy strategy_7 --plot
    python strategy_evaluator.py --config_file conf/test_fm01_aem.json --strategy all --plot
"""

import argparse
import json
import os
import sys
import logging
from dataclasses import dataclass
from typing import Dict, List
from datetime import datetime, timedelta

import pandas as pd
import matplotlib
import numpy as np

# Headless plotting for servers/CI
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

# Add parent directory to path for imports
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from classes.postgresql_interface import PostgreSQLInterface


# --------------------------
# Plotting utilities
# --------------------------

def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _time_to_slot_idx(hhmm: str) -> int:
    """Convert HH:MM into 15-min slot index [0..95]."""
    hour_str, minute_str = hhmm.split(":")
    h = int(hour_str)
    m = int(minute_str)
    if m not in (0, 15, 30, 45):
        raise ValueError(f"Time must be aligned to 15-min slots, got {hhmm}")
    return h * 4 + (m // 15)


def _window_to_slot_ranges(start_hhmm: str, end_hhmm: str) -> List[tuple[int, int]]:
    """Return inclusive slot ranges for a possibly wrap-around window."""
    s = _time_to_slot_idx(start_hhmm)
    e = _time_to_slot_idx(end_hhmm)
    if s < e:
        return [(s, e - 1)]
    if s > e:
        return [(s, 95), (0, e - 1)]
    return [(0, 95)]


def _load_boxplot_csv(csv_path: str) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    df["slot_idx"] = df["slot_idx"].astype(int)
    df = df.sort_values("slot_idx").reset_index(drop=True)

    # Some slots have no data (count=0 → NaNs). Interpolate for smoother plots.
    for col in ("mean", "median", "min", "max", "p25", "p75"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
            df[col] = df[col].interpolate(limit_direction="both")

    return df


def _slot_axis_format(ax: plt.Axes) -> None:
    hour_ticks = list(range(0, 96, 4))
    hour_labels = [f"{h:02d}:00" for h in range(24)]
    ax.set_xticks(hour_ticks)
    ax.set_xticklabels(hour_labels, rotation=0)
    ax.set_xticks(list(range(96)), minor=True)


def _build_expected_activated_profile(strategy: "Strategy") -> List[float]:
    """Return a 96-slot expected activated flexibility profile (kW) for a strategy."""
    slot_kw = [0.0] * 96
    for ts in strategy.time_slots:
        expected_kw = float(ts.flexibility_mw) * float(ts.activation_rate) * 1000.0
        for s, e in _window_to_slot_ranges(ts.start, ts.end):
            for i in range(s, e + 1):
                slot_kw[i] += expected_kw
    return slot_kw


def plot_strategy_7_schedule_overlays(
    demand_dir: str,
    output_dir: str,
) -> List[str]:
    """Create Strategy 7 schedule overlay plots and return file paths."""
    price_csv = os.path.join(demand_dir, "price_by_hour_boxplot_15min.csv")
    demand_csv = os.path.join(demand_dir, "demand_by_hour_boxplot_15min.csv")

    missing = [p for p in (price_csv, demand_csv) if not os.path.isfile(p)]
    if missing:
        raise FileNotFoundError(f"Missing required CSV(s): {missing}")

    _ensure_dir(output_dir)
    out_paths: List[str] = []

    df_price = _load_boxplot_csv(price_csv)
    df_demand = _load_boxplot_csv(demand_csv)

    windows = [
        ("Pre-heat 1", "04:00", "06:00", "preheat"),
        ("Transition 1", "06:00", "07:00", "transition"),
        ("Morning Flex", "07:00", "11:00", "flex"),
        ("Transition 2", "11:00", "12:00", "transition"),
        ("Pre-heat 2", "12:00", "15:00", "preheat"),
        ("Transition 3", "15:00", "16:00", "transition"),
        ("Evening Flex", "16:00", "20:00", "flex"),
        ("Night Recovery", "20:00", "04:00", "recovery"),
    ]
    styles = {
        "preheat": {"color": "#fdbf6f", "alpha": 0.35},
        "flex": {"color": "#33a02c", "alpha": 0.28},
        "transition": {"color": "#1f78b4", "alpha": 0.18},
        "recovery": {"color": "#b2df8a", "alpha": 0.12},
    }

    def add_overlays(ax: plt.Axes) -> None:
        for _, start, end, kind in windows:
            for s, e in _window_to_slot_ranges(start, end):
                ax.axvspan(s - 0.5, e + 0.5, **styles[kind])

        handles = []
        labels = []
        for kind, st in styles.items():
            patch = plt.Rectangle((0, 0), 1, 1, color=st["color"], alpha=st["alpha"])
            handles.append(patch)
            labels.append(kind)
        ax.legend(handles, labels, loc="upper left", frameon=True)

    def plot_mean_iqr(df: pd.DataFrame, title: str, y_label: str, out_name: str) -> str:
        x = df["slot_idx"].to_numpy()
        fig, ax = plt.subplots(figsize=(16, 6))
        ax.plot(x, df["mean"].to_numpy(), color="black", linewidth=2.0, label="Mean")
        ax.fill_between(
            x,
            df["p25"].to_numpy(),
            df["p75"].to_numpy(),
            color="#6a3d9a",
            alpha=0.18,
            label="IQR (p25-p75)",
        )
        add_overlays(ax)
        ax.set_title(title)
        ax.set_xlabel("Time")
        ax.set_ylabel(y_label)
        ax.grid(True, alpha=0.3)
        _slot_axis_format(ax)
        fig.tight_layout()
        out_path = os.path.join(output_dir, out_name)
        fig.savefig(out_path, dpi=150)
        plt.close(fig)
        return out_path

    out_paths.append(
        plot_mean_iqr(
            df_price,
            title="Strategy 7 schedule overlay on price stats (15-min)",
            y_label="Price (CHF/MW)",
            out_name="strategy_7_price_schedule.png",
        )
    )

    out_paths.append(
        plot_mean_iqr(
            df_demand,
            title="Strategy 7 schedule overlay on demand stats (15-min)",
            y_label="Demand (kW)",
            out_name="strategy_7_demand_schedule.png",
        )
    )

    legacy = os.path.join(output_dir, "strategy_7_price_schedule_box.png")
    if os.path.isfile(legacy):
        try:
            os.remove(legacy)
        except OSError:
            pass

    return out_paths


def plot_strategies_comparison(
    results: Dict[str, "StrategyResult"],
    output_dir: str,
) -> List[str]:
    """Create comparison plots across strategies and return file paths."""
    _ensure_dir(output_dir)
    out_paths: List[str] = []

    sorted_items = sorted(results.items(), key=lambda kv: kv[1].net_profit_chf, reverse=True)
    labels = [kv[1].strategy_name for kv in sorted_items]
    net = [kv[1].net_profit_chf for kv in sorted_items]
    revenue = [kv[1].total_revenue_chf for kv in sorted_items]
    cost = [kv[1].total_activation_cost_chf for kv in sorted_items]

    fig, ax = plt.subplots(figsize=(12, 6))
    ax.barh(labels, net, color=["#33a02c" if i == 0 else "#1f78b4" for i in range(len(labels))])
    ax.set_title("Strategy Comparison - Net Profit")
    ax.set_xlabel("Net profit (CHF)")
    ax.grid(True, axis="x", alpha=0.3)
    ax.invert_yaxis()
    fig.tight_layout()
    out1 = os.path.join(output_dir, "strategies_net_profit.png")
    fig.savefig(out1, dpi=150)
    plt.close(fig)
    out_paths.append(out1)

    fig, ax = plt.subplots(figsize=(12, 6))
    y = list(range(len(labels)))
    ax.barh(y, revenue, color="#6a3d9a", alpha=0.75, label="Revenue")
    ax.barh(y, cost, color="#e31a1c", alpha=0.75, label="Activation cost")
    ax.set_yticks(y)
    ax.set_yticklabels(labels)
    ax.set_title("Strategy Comparison - Revenue and Activation Cost")
    ax.set_xlabel("CHF")
    ax.grid(True, axis="x", alpha=0.3)
    ax.invert_yaxis()
    ax.legend(loc="lower right")
    fig.tight_layout()
    out2 = os.path.join(output_dir, "strategies_revenue_cost.png")
    fig.savefig(out2, dpi=150)
    plt.close(fig)
    out_paths.append(out2)

    return out_paths


# --- Strategy 7 extra plots helpers ---

def _parse_ts_utc(ts: str) -> pd.Timestamp:
    """Parse timestamps like '2026-01-09T12:00:00Z' into UTC pandas Timestamp."""
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return pd.to_datetime(ts, utc=True)


def _slot_idx_from_ts(ts: pd.Timestamp) -> int:
    return int(ts.hour * 4 + ts.minute // 15)


def _load_bid_records_dir_for_assets(bid_records_dir: str, asset_ids: List[str]) -> pd.DataFrame:
    """Load slot-level bid-record JSONs and extract per-asset activated kW.

    Notes:
      The repository's bid records (data/bid_records/*.json) contain an
      `assets_to_activate` list with `available_flexibility_kw` and (optionally)
      `flexibility_factor`. We use:

        activated_kw(asset) = available_flexibility_kw * flexibility_factor

      This is a best-effort reconstruction usable for plotting.
    """
    rows: List[Dict] = []
    if not os.path.isdir(bid_records_dir):
        return pd.DataFrame(columns=["ts", "slot_idx", *[f"kw_{a}" for a in asset_ids], "src", "strategy_id"])

    for fn in sorted(os.listdir(bid_records_dir)):
        if not fn.endswith(".json"):
            continue
        fp = os.path.join(bid_records_dir, fn)
        try:
            obj = json.loads(open(fp, "r", encoding="utf-8").read())
        except Exception:
            continue

        slot_start = obj.get("slot_start") or obj.get("period_from")
        if not slot_start:
            continue

        ts = _parse_ts_utc(str(slot_start))
        per_asset = {a: 0.0 for a in asset_ids}
        for a in obj.get("assets_to_activate") or []:
            aid = str(a.get("asset_id") or a.get("id") or a.get("name") or "")
            if aid not in per_asset:
                continue
            flex_kw = float(a.get("available_flexibility_kw") or a.get("available_flexibility") or 0.0)
            fac = float(a.get("flexibility_factor") or 1.0)
            per_asset[aid] += flex_kw * fac

        row = {
            "ts": ts,
            "slot_idx": _slot_idx_from_ts(ts),
            "src": fn,
            "strategy_id": (obj.get("strategy") or {}).get("id"),
        }
        for aid in asset_ids:
            row[f"kw_{aid}"] = per_asset[aid]
        rows.append(row)

    if not rows:
        return pd.DataFrame(columns=["ts", "slot_idx", *[f"kw_{a}" for a in asset_ids], "src", "strategy_id"])

    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df.sort_values("ts").reset_index(drop=True)
    return df


def _activation_case_2assets(df: pd.DataFrame, a_col: str, b_col: str, eps_kw: float = 1e-9) -> pd.Series:
    a = df[a_col].fillna(0.0).to_numpy() > eps_kw
    b = df[b_col].fillna(0.0).to_numpy() > eps_kw

    out = ["none"] * len(df)
    for i in range(len(df)):
        if a[i] and b[i]:
            out[i] = "ECM96.2 and ECM97.3"
        elif a[i] and not b[i]:
            out[i] = "only ECM96.2"
        elif (not a[i]) and b[i]:
            out[i] = "only ECM97.3"
    return pd.Series(out, index=df.index, name="case")


def plot_s7_mean_activated_kw_by_case(
    *,
    bid_records_dir: str,
    output_dir: str,
    asset_a: str = "ECM96.2",
    asset_b: str = "ECM97.3",
) -> str:
    """Plot mean activated kW by time-of-day for 4 activation cases."""
    _ensure_dir(output_dir)

    df = _load_bid_records_dir_for_assets(bid_records_dir, [asset_a, asset_b])
    if df.empty:
        raise FileNotFoundError(
            f"No bid records found in {bid_records_dir}. "
            "I need slot-level JSON bid records to compute activated kW by case."
        )

    a_col = f"kw_{asset_a}"
    b_col = f"kw_{asset_b}"
    df["case"] = _activation_case_2assets(df, a_col=a_col, b_col=b_col)
    df["activated_kw_total"] = df[[a_col, b_col]].sum(axis=1)

    prof = (
        df.groupby(["case", "slot_idx"], as_index=False)
        .agg(mean_kw=("activated_kw_total", "mean"))
        .sort_values(["case", "slot_idx"])
    )

    order = ["none", "only ECM96.2", "only ECM97.3", "ECM96.2 and ECM97.3"]
    colors = {
        "none": "#9e9e9e",
        "only ECM96.2": "#1f78b4",
        "only ECM97.3": "#33a02c",
        "ECM96.2 and ECM97.3": "#e31a1c",
    }

    fig, ax = plt.subplots(figsize=(16, 6))
    for c in order:
        g = prof[prof["case"] == c]
        if g.empty:
            continue
        ax.plot(g["slot_idx"], g["mean_kw"], label=c, linewidth=2.0, color=colors.get(c, None))

    hour_ticks = list(range(0, 96, 4))
    hour_labels = [f"{h:02d}" for h in range(24)]
    ax.set_xticks(hour_ticks)
    ax.set_xticklabels(hour_labels)
    ax.set_xticks(list(range(96)), minor=True)

    ax.set_title("Mean activated power during a day (Strategy 7) - 4 activation cases")
    ax.set_xlabel("Time")
    ax.set_ylabel("Activated power (kW, mean)")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper right")

    out_path = os.path.join(output_dir, "strategy_7_mean_activated_kw_by_case.png")
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path


def _load_energy_signature_daily(ecm96_csv: str, ecm97_csv: str) -> pd.DataFrame:
    """Load daily BAU power and avg outdoor temperature from energy signature CSVs."""
    d96 = pd.read_csv(ecm96_csv)
    d97 = pd.read_csv(ecm97_csv)

    for d in (d96, d97):
        if "date" not in d.columns:
            raise ValueError(f"Missing 'date' column in {ecm96_csv if d is d96 else ecm97_csv}")
        d["date"] = pd.to_datetime(d["date"]).dt.date

    out = pd.DataFrame({"date": d96["date"]})
    out = out.merge(
        d96[["date", "power_kw", "avg_temp_c"]].rename(columns={"power_kw": "bau_ecm96_kw"}),
        on="date",
        how="inner",
    )
    out = out.merge(
        d97[["date", "power_kw"]].rename(columns={"power_kw": "bau_ecm97_kw"}),
        on="date",
        how="inner",
    )
    out["bau_kw"] = out[["bau_ecm96_kw", "bau_ecm97_kw"]].sum(axis=1)
    return out


def _strategy7_daily_power_proxy_from_bid_records(
    bid_records_dir: str,
    asset_a: str = "ECM96.2",
    asset_b: str = "ECM97.3",
) -> pd.DataFrame:
    """Best-effort daily series; uses *mean activated kW* across slots per day.

    This is a proxy. If you have real daily delivered power for Strategy 7,
    supply it through --strategy7_daily_power_csv instead.
    """
    df = _load_bid_records_dir_for_assets(bid_records_dir, [asset_a, asset_b])
    if df.empty:
        return pd.DataFrame(columns=["date", "strategy_7_kw"])

    a_col = f"kw_{asset_a}"
    b_col = f"kw_{asset_b}"
    df["date"] = pd.to_datetime(df["ts"], utc=True).dt.date
    df["activated_kw_total"] = df[[a_col, b_col]].sum(axis=1)
    out = df.groupby("date", as_index=False).agg(strategy_7_kw=("activated_kw_total", "mean"))
    return out


def _load_strategy7_daily_power_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    required = {"date", "power_kw"}
    miss = required - set(df.columns)
    if miss:
        raise ValueError(f"Strategy 7 daily power CSV missing columns: {sorted(miss)}")
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df["power_kw"] = pd.to_numeric(df["power_kw"], errors="coerce")
    return df.rename(columns={"power_kw": "strategy_7_kw"})


def plot_s7_vs_bau_daily_power_and_temp(
    *,
    energy_signature_ecm96_csv: str,
    energy_signature_ecm97_csv: str,
    bid_records_dir: str,
    output_dir: str,
    strategy7_daily_power_csv: str | None = None,
) -> str:
    _ensure_dir(output_dir)

    df_bau = _load_energy_signature_daily(energy_signature_ecm96_csv, energy_signature_ecm97_csv)
    if strategy7_daily_power_csv:
        df_s7 = _load_strategy7_daily_power_csv(strategy7_daily_power_csv)
    else:
        df_s7 = _strategy7_daily_power_proxy_from_bid_records(bid_records_dir)

    df = df_bau.merge(df_s7, on="date", how="inner").sort_values("date")
    if df.empty:
        raise RuntimeError("No overlapping dates between BAU (energy signature) and Strategy 7 series.")

    x = pd.to_datetime(pd.Series(df["date"]))

    fig, ax1 = plt.subplots(figsize=(16, 6))
    ax1.plot(x, df["bau_kw"], label="BAU (energy signature)", color="#1f78b4", linewidth=2.0)
    ax1.plot(x, df["strategy_7_kw"], label="Strategy 7", color="#33a02c", linewidth=2.0)
    ax1.set_ylabel("Daily average power (kW)")
    ax1.set_xlabel("Day")
    ax1.grid(True, alpha=0.3)

    ax2 = ax1.twinx()
    ax2.plot(x, df["avg_temp_c"], label="Avg outdoor temp (°C)", color="#e31a1c", linewidth=1.8, alpha=0.9)
    ax2.set_ylabel("Avg outdoor temperature (°C)")

    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, loc="upper left")

    ax1.set_title("Daily power: Strategy 7 vs BAU (with outdoor temperature)")

    out_path = os.path.join(output_dir, "strategy_7_vs_bau_daily_power_and_temp.png")
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path


def plot_strategy_activated_flexibility_heatmap(
    *,
    strategy: "Strategy",
    start_date: datetime,
    end_date: datetime,
    output_dir: str,
    out_name: str,
    activations_df: pd.DataFrame | None = None,
) -> str:
    """Plot a day-by-slot heatmap of activated flexibility (kW)."""
    _ensure_dir(output_dir)

    if activations_df is None:
        days = pd.date_range(start=start_date, end=end_date, freq="D")
        profile = _build_expected_activated_profile(strategy)
        data = np.tile(np.array(profile, dtype=float), (len(days), 1))
    else:
        data, days = _build_heatmap_data_from_activations(activations_df, start_date, end_date)

    fig, ax = plt.subplots(figsize=(16, 8))
    im = ax.imshow(data, aspect="auto", cmap="viridis", origin="upper")

    hour_ticks = list(range(0, 96, 4))
    hour_labels = [f"{h:02d}" for h in range(24)]
    ax.set_xticks(hour_ticks)
    ax.set_xticklabels(hour_labels)
    ax.set_xticks(list(range(96)), minor=True)

    # Show a subset of day labels to keep the axis readable
    y_tick_step = max(1, len(days) // 10)
    raw_ticks = list(range(0, len(days), y_tick_step))
    y_ticks = sorted(set([0, len(days) - 1] + raw_ticks))
    ax.set_yticks(y_ticks)
    ax.set_yticklabels([days[i].strftime("%Y-%m-%d") for i in y_ticks])

    ax.xaxis.grid(True, which="minor", color="w", linestyle="-", linewidth=0.35, alpha=0.65)
    ax.xaxis.grid(True, which="major", color="w", linestyle="-", linewidth=0.45, alpha=0.85)
    ax.yaxis.grid(True, color="w", linestyle="-", linewidth=0.3, alpha=0.5)

    ax.set_xlabel("Time")
    ax.set_ylabel("Day")
    ax.set_title(f"{strategy.name} - Activated flexibility heatmap (kW)")
    cbar = fig.colorbar(im, ax=ax)
    cbar.set_label("Activated flexibility (kW)")

    out_path = os.path.join(output_dir, out_name)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path


def plot_s7_expected_activated_flexibility_profile(
    *,
    strategy: "Strategy",
    output_dir: str,
) -> str:
    """Strategy 7: average-day expected activated flexibility profile (15-min)."""
    _ensure_dir(output_dir)

    slot_kw = _build_expected_activated_profile(strategy)
    df = pd.DataFrame({"slot_idx": list(range(96)), "expected_activated_kw": slot_kw})

    fig, ax = plt.subplots(figsize=(16, 6))
    ax.plot(df["slot_idx"], df["expected_activated_kw"], color="#33a02c", linewidth=2.5)

    hour_ticks = list(range(0, 96, 4))
    hour_labels = [f"{h:02d}" for h in range(24)]
    ax.set_xticks(hour_ticks)
    ax.set_xticklabels(hour_labels)

    ax.set_title("Strategy 7 - expected activated flexibility (average day, 15-min)")
    ax.set_xlabel("Time")
    ax.set_ylabel("Expected activated flexibility (kW)")
    ax.grid(True, alpha=0.3)

    out_path = os.path.join(output_dir, "strategy_7_expected_activated_flexibility_profile.png")
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path


# --------------------------
# Core evaluator (existing)
# --------------------------


@dataclass
class TimeSlot:
    """Represents a time period for bidding."""
    name: str
    start: str  # HH:MM format
    end: str    # HH:MM format
    slots_per_day: int  # Number of 15-min slots in this period
    flexibility_mw: float
    bid_price_chf_mw: float
    activation_rate: float  # Expected activation probability (0-1)
    activation_cost_chf_mwh: float = 2.5  # Default HP activation cost
    include_ev: bool = False
    ev_flexibility_mw: float = 0.0
    ev_activation_rate: float = 0.0
    ev_bid_price: float = 0.0
    ev_activation_cost: float = 4.5  # EV activation cost is higher
    # Preheat parameters for Strategy 6
    is_preheat_period: bool = False  # If True, this is a preheat window (HP forced ON)
    preheat_power_mw: float = 0.0    # Power consumed during preheat
    preheat_elec_cost_chf_mwh: float = 80.0  # Off-peak electricity price (~0.08 CHF/kWh)


@dataclass
class Strategy:
    """Represents a bidding strategy."""
    name: str
    description: str
    assets_included: List[str]
    time_slots: List[TimeSlot]
    uses_ev: bool = False


@dataclass
class StrategyResult:
    """Results of strategy evaluation."""
    strategy_name: str
    total_revenue_chf: float
    total_activation_cost_chf: float
    net_profit_chf: float
    total_slots_activated: int
    avg_profit_per_slot: float
    period_breakdown: List[Dict]


class StrategyEvaluator:
    """Evaluates bidding strategies based on configuration and market assumptions."""
    
    def __init__(self, config: dict, start_date: datetime, end_date: datetime):
        """
        Initialize the evaluator.
        
        :param config: Configuration dictionary from JSON file
        :param start_date: Start date of evaluation period
        :param end_date: End date of evaluation period
        """
        self.config = config
        self.start_date = start_date
        self.end_date = end_date
        self.days = (end_date - start_date).days + 1  # Include both start and end dates
        self.asset_mapping = config.get("asset_mapping", {})
        self.assets_grouping = config.get("assets_grouping", {})
        
        # Extract asset information
        self._parse_assets()
        
        # Define strategies
        self.strategies = self._define_strategies()

    def _apply_activation_rate_overrides(self, strategies: Dict[str, "Strategy"]) -> None:
        cfg_strategies = self.config.get("bidding_strategies", {})
        if not isinstance(cfg_strategies, dict):
            return
        for strategy_id, strategy in strategies.items():
            cfg = cfg_strategies.get(strategy_id, {})
            slots_cfg = cfg.get("time_slots", [])
            if not slots_cfg:
                continue
            rate_by_name = {
                s.get("name"): s.get("activation_rate")
                for s in slots_cfg
                if isinstance(s, dict) and "activation_rate" in s
            }
            for ts in strategy.time_slots:
                if ts.name in rate_by_name and rate_by_name[ts.name] is not None:
                    ts.activation_rate = float(rate_by_name[ts.name])

    def _parse_assets(self):
        """Parse asset information from configuration."""
        self.hp_assets = {}
        self.ev_assets = {}
        
        for asset_id, mapping in self.asset_mapping.items():
            if isinstance(mapping, dict):
                asset_type = mapping.get("type", "")
                capacity_kw = mapping.get("capacity_kw", 0)
                flex_factor = mapping.get("flexibility_factor", 0.5)
                description = mapping.get("description", asset_id)
                
                asset_info = {
                    "id": asset_id,
                    "description": description,
                    "capacity_kw": capacity_kw,
                    "capacity_mw": capacity_kw / 1000,
                    "flexibility_factor": flex_factor,
                    "max_flex_kw": capacity_kw * flex_factor,
                    "max_flex_mw": capacity_kw * flex_factor / 1000,
                }
                
                if asset_type == "heat_pump":
                    self.hp_assets[asset_id] = asset_info
                elif asset_type == "ev_charger":
                    self.ev_assets[asset_id] = asset_info
        
        # Calculate totals
        self.total_hp_capacity_mw = sum(a["capacity_mw"] for a in self.hp_assets.values())
        self.total_hp_flex_mw = sum(a["max_flex_mw"] for a in self.hp_assets.values())
        self.total_ev_capacity_mw = sum(a["capacity_mw"] for a in self.ev_assets.values())
        self.total_ev_flex_mw = sum(a["max_flex_mw"] for a in self.ev_assets.values())
    
    def _define_strategies(self) -> Dict[str, Strategy]:
        """
        Define the 5 bidding strategies.
        
        Strategy parameters are based on:
        - Historical asset usage patterns from analysis scripts
        - DSO willingness-to-pay from market data (~9.5 CHF/MW average)
        - Reasonable activation rate assumptions (5-30%)
        """
        strategies = {}
        
        # =====================================================================
        # STRATEGY 1: HP Only - Conservative full-day coverage
        # =====================================================================
        strategies["strategy_1"] = Strategy(
            name="Strategy 1: HP Only",
            description="Heat pumps only, full day coverage, no EV chargers",
            assets_included=list(self.hp_assets.keys()),
            uses_ev=False,
            time_slots=[
                TimeSlot(
                    name="Morning Peak",
                    start="06:30", end="09:00",
                    slots_per_day=10,  # 2.5 hours × 4 slots/hour
                    flexibility_mw=0.015,  # Moderate HP usage
                    bid_price_chf_mw=9.0,
                    activation_rate=0.20,
                    activation_cost_chf_mwh=2.5
                ),
                TimeSlot(
                    name="Late Morning",
                    start="09:00", end="12:00",
                    slots_per_day=12,
                    flexibility_mw=0.012,
                    bid_price_chf_mw=8.5,
                    activation_rate=0.15,
                    activation_cost_chf_mwh=2.5
                ),
                TimeSlot(
                    name="Afternoon",
                    start="12:00", end="16:00",
                    slots_per_day=16,
                    flexibility_mw=0.008,
                    bid_price_chf_mw=7.0,
                    activation_rate=0.10,
                    activation_cost_chf_mwh=2.5
                ),
                TimeSlot(
                    name="Evening Peak",
                    start="16:00", end="19:00",
                    slots_per_day=12,
                    flexibility_mw=0.010,
                    bid_price_chf_mw=9.5,
                    activation_rate=0.25,
                    activation_cost_chf_mwh=2.5
                ),
                TimeSlot(
                    name="Off-peak",
                    start="19:00", end="06:30",
                    slots_per_day=46,  # ~11.5 hours
                    flexibility_mw=0.006,
                    bid_price_chf_mw=6.0,
                    activation_rate=0.05,
                    activation_cost_chf_mwh=2.5
                ),
            ]
        )
        
        # =====================================================================
        # STRATEGY 2: Full Portfolio + Evening EV Focus
        # =====================================================================
        strategies["strategy_2"] = Strategy(
            name="Strategy 2: Full Portfolio + Evening EV",
            description="All assets, focus on evening when EVs might be charging",
            assets_included=list(self.hp_assets.keys()) + list(self.ev_assets.keys()),
            uses_ev=True,
            time_slots=[
                TimeSlot(
                    name="Morning Peak",
                    start="07:00", end="10:00",
                    slots_per_day=12,
                    flexibility_mw=0.012,
                    bid_price_chf_mw=8.0,
                    activation_rate=0.15,
                    activation_cost_chf_mwh=2.5
                ),
                TimeSlot(
                    name="Midday",
                    start="10:00", end="17:00",
                    slots_per_day=28,
                    flexibility_mw=0.006,
                    bid_price_chf_mw=6.0,
                    activation_rate=0.05,
                    activation_cost_chf_mwh=2.5
                ),
                TimeSlot(
                    name="Evening Peak (HP + EV)",
                    start="17:00", end="20:00",
                    slots_per_day=12,
                    flexibility_mw=0.010,
                    bid_price_chf_mw=9.5,
                    activation_rate=0.25,
                    activation_cost_chf_mwh=2.5,
                    include_ev=True,
                    ev_flexibility_mw=0.0008,  # Very low due to 5-10% occupancy
                    ev_activation_rate=0.15,
                    ev_bid_price=10.0,
                    ev_activation_cost=4.5
                ),
                TimeSlot(
                    name="Off-peak",
                    start="20:00", end="07:00",
                    slots_per_day=44,
                    flexibility_mw=0.005,
                    bid_price_chf_mw=5.5,
                    activation_rate=0.05,
                    activation_cost_chf_mwh=2.5
                ),
            ]
        )
        
        # =====================================================================
        # STRATEGY 3: Morning Peak Focus
        # =====================================================================
        strategies["strategy_3"] = Strategy(
            name="Strategy 3: Morning Peak Focus",
            description="Aggressive bidding during morning peak when Cinema HPs are at maximum",
            assets_included=["ECM97.1", "ECM97.2"],  # Cinema HPs only
            uses_ev=False,
            time_slots=[
                TimeSlot(
                    name="Morning Peak (Aggressive)",
                    start="06:30", end="09:00",
                    slots_per_day=10,
                    flexibility_mw=0.020,  # Cinema HPs at peak consumption
                    bid_price_chf_mw=9.0,
                    activation_rate=0.30,  # Aggressive bidding
                    activation_cost_chf_mwh=2.5
                ),
                TimeSlot(
                    name="Late Morning",
                    start="09:00", end="12:00",
                    slots_per_day=12,
                    flexibility_mw=0.010,
                    bid_price_chf_mw=7.5,
                    activation_rate=0.10,
                    activation_cost_chf_mwh=2.5
                ),
                TimeSlot(
                    name="Afternoon/Evening",
                    start="12:00", end="20:00",
                    slots_per_day=32,
                    flexibility_mw=0.006,
                    bid_price_chf_mw=6.0,
                    activation_rate=0.05,
                    activation_cost_chf_mwh=2.5
                ),
                TimeSlot(
                    name="Night",
                    start="20:00", end="06:30",
                    slots_per_day=42,
                    flexibility_mw=0.004,
                    bid_price_chf_mw=5.0,
                    activation_rate=0.03,
                    activation_cost_chf_mwh=2.5
                ),
            ]
        )
        
        # =====================================================================
        # STRATEGY 4: Hybrid (Strategy 3 morning + Strategy 1 rest)
        # =====================================================================
        strategies["strategy_4"] = Strategy(
            name="Strategy 4: Hybrid (S3+S1)",
            description="Morning peak aggressive (S3) + full day HP coverage (S1)",
            assets_included=list(self.hp_assets.keys()),
            uses_ev=False,
            time_slots=[
                TimeSlot(
                    name="Morning Peak (Aggressive)",
                    start="06:30", end="09:00",
                    slots_per_day=10,
                    flexibility_mw=0.020,  # Cinema HPs at peak
                    bid_price_chf_mw=9.0,
                    activation_rate=0.30,  # Aggressive
                    activation_cost_chf_mwh=2.5
                ),
                TimeSlot(
                    name="Late Morning",
                    start="09:00", end="12:00",
                    slots_per_day=12,
                    flexibility_mw=0.012,
                    bid_price_chf_mw=8.5,
                    activation_rate=0.15,
                    activation_cost_chf_mwh=2.5
                ),
                TimeSlot(
                    name="Afternoon",
                    start="12:00", end="16:00",
                    slots_per_day=16,
                    flexibility_mw=0.008,
                    bid_price_chf_mw=7.0,
                    activation_rate=0.10,
                    activation_cost_chf_mwh=2.5
                ),
                TimeSlot(
                    name="Evening Peak",
                    start="16:00", end="19:00",
                    slots_per_day=12,
                    flexibility_mw=0.010,
                    bid_price_chf_mw=9.5,
                    activation_rate=0.25,
                    activation_cost_chf_mwh=2.5
                ),
                TimeSlot(
                    name="Off-peak",
                    start="19:00", end="06:30",
                    slots_per_day=46,
                    flexibility_mw=0.006,
                    bid_price_chf_mw=6.0,
                    activation_rate=0.05,
                    activation_cost_chf_mwh=2.5
                ),
            ]
        )
        
        # =====================================================================
        # STRATEGY 5: Hybrid2 (Strategy 3 morning + Strategy 2 rest with EVs)
        # =====================================================================
        strategies["strategy_5"] = Strategy(
            name="Strategy 5: Hybrid2 (S3+S2)",
            description="Morning peak aggressive (S3) + evening EV focus (S2)",
            assets_included=list(self.hp_assets.keys()) + list(self.ev_assets.keys()),
            uses_ev=True,
            time_slots=[
                TimeSlot(
                    name="Morning Peak (Aggressive)",
                    start="06:30", end="09:00",
                    slots_per_day=10,
                    flexibility_mw=0.020,
                    bid_price_chf_mw=9.0,
                    activation_rate=0.30,
                    activation_cost_chf_mwh=2.5
                ),
                TimeSlot(
                    name="Late Morning",
                    start="09:00", end="12:00",
                    slots_per_day=12,
                    flexibility_mw=0.010,
                    bid_price_chf_mw=7.5,
                    activation_rate=0.10,
                    activation_cost_chf_mwh=2.5
                ),
                TimeSlot(
                    name="Afternoon",
                    start="12:00", end="17:00",
                    slots_per_day=20,
                    flexibility_mw=0.006,
                    bid_price_chf_mw=6.0,
                    activation_rate=0.05,
                    activation_cost_chf_mwh=2.5
                ),
                TimeSlot(
                    name="Evening Peak (HP + EV)",
                    start="17:00", end="20:00",
                    slots_per_day=12,
                    flexibility_mw=0.010,
                    bid_price_chf_mw=9.5,
                    activation_rate=0.25,
                    activation_cost_chf_mwh=2.5,
                    include_ev=True,
                    ev_flexibility_mw=0.0008,
                    ev_activation_rate=0.15,
                    ev_bid_price=10.0,
                    ev_activation_cost=4.5
                ),
                TimeSlot(
                    name="Off-peak",
                    start="20:00", end="06:30",
                    slots_per_day=42,
                    flexibility_mw=0.005,
                    bid_price_chf_mw=5.5,
                    activation_rate=0.05,
                    activation_cost_chf_mwh=2.5
                ),
            ]
        )

        # =====================================================================
        # STRATEGY 6: Smart Preheat - Maximize morning peak flexibility
        # =====================================================================
        # This strategy exploits thermal inertia of buildings:
        # - 04:00-06:00: Force HP ON during off-peak (cheap electricity, preheat)
        # - 06:00-07:00: Transition period, HP still running but can start reducing
        # - 07:00-10:00: HP OFF (full flexibility available for DSO morning peak)
        # - 10:00-16:00: Normal operation, moderate flexibility
        # - 16:00-19:00: Evening peak, partial flexibility
        # - 19:00-04:00: Off-peak, low flexibility
        #
        # COST MODEL:
        # - The preheat cost is the INCREMENTAL cost vs normal operation
        # - HP would run anyway during 07:00-10:00 in normal operation
        # - By preheating, we shift consumption from peak (0.12 CHF/kWh) to off-peak (0.08 CHF/kWh)
        # - Net cost = preheat_energy × (offpeak_price - avoided_peak_price)
        # - This is often NEGATIVE (a savings!) because off-peak is cheaper
        #
        # BENEFITS:
        # 1. Higher activation revenue during morning peak (100% availability)
        # 2. Electricity cost savings (off-peak vs peak pricing)
        # 3. Building thermal mass stores the heat for 3-4 hours
        # =====================================================================
        strategies["strategy_6"] = Strategy(
            name="Strategy 6: Smart Preheat",
            description="Preheat 04-06h to guarantee 100% HP flexibility during morning peak 07-10h",
            assets_included=list(self.hp_assets.keys()),
            uses_ev=False,
            time_slots=[
                # Preheat period: HP is forced ON, NO flexibility offered
                # Incremental cost = preheat energy × electricity price
                # If off-peak < peak, this is actually a SAVINGS!
                # Model: 0.020 MW × 2h = 0.040 MWh, but we save 0.040 MWh during peak
                # Net cost = 0.040 × (80 - 120) = -1.6 CHF (a savings!)
                # However, we model conservatively with small net cost
                TimeSlot(
                    name="Preheat (HP ON)",
                    start="04:00", end="06:00",
                    slots_per_day=8,  # 2 hours × 4 slots/hour
                    flexibility_mw=0.0,  # No flex offered - HP is intentionally running
                    bid_price_chf_mw=0.0,
                    activation_rate=0.0,
                    activation_cost_chf_mwh=0.0,
                    is_preheat_period=True,
                    preheat_power_mw=0.020,  # HP running at ~20 kW during preheat
                    # NET incremental cost: off-peak rate minus avoided peak rate
                    # Off-peak: ~0.08 CHF/kWh = 80 CHF/MWh
                    # Peak: ~0.12 CHF/kWh = 120 CHF/MWh
                    # Net: 80 - 120 = -40 CHF/MWh (negative = savings!)
                    # But we use 20 as conservative estimate (some extra heating)
                    preheat_elec_cost_chf_mwh=20.0,  # Conservative incremental cost
                ),
                # Transition: HP winding down, building still warm
                TimeSlot(
                    name="Transition",
                    start="06:00", end="07:00",
                    slots_per_day=4,
                    flexibility_mw=0.010,  # Partial flex as HP winds down
                    bid_price_chf_mw=8.0,
                    activation_rate=0.15,
                    activation_cost_chf_mwh=2.5
                ),
                # Morning Peak: HP OFF, FULL flexibility available!
                # Building thermal mass from preheat maintains comfort
                # DSO sees 100% reliable load reduction capability
                # DSO PREMIUM: Higher willingness-to-pay for guaranteed availability
                # - Normal bid: ~9.5 CHF/MW with 30% activation
                # - Preheat bid: ~10.5 CHF/MW with 50% activation (DSO prefers reliable)
                TimeSlot(
                    name="Morning Peak (HP OFF - Full Flex)",
                    start="07:00", end="10:00",
                    slots_per_day=12,  # 3 hours × 4 slots/hour
                    flexibility_mw=0.028,  # FULL HP capacity available (higher than S4's 0.020)
                    bid_price_chf_mw=10.5,  # Premium price for guaranteed availability
                    activation_rate=0.50,  # Higher rate - DSO loves reliable flex
                    activation_cost_chf_mwh=1.0,  # Lower cost - HP already OFF, just maintain
                ),
                # Post-peak recovery: HP can restart if needed
                TimeSlot(
                    name="Post-Peak Recovery",
                    start="10:00", end="12:00",
                    slots_per_day=8,
                    flexibility_mw=0.010,  # Moderate - HP might need to run
                    bid_price_chf_mw=7.5,
                    activation_rate=0.12,
                    activation_cost_chf_mwh=2.5
                ),
                # Afternoon: Normal operation
                TimeSlot(
                    name="Afternoon",
                    start="12:00", end="16:00",
                    slots_per_day=16,
                    flexibility_mw=0.008,
                    bid_price_chf_mw=7.0,
                    activation_rate=0.10,
                    activation_cost_chf_mwh=2.5
                ),
                # Evening peak: Good flexibility, but not as high as morning
                TimeSlot(
                    name="Evening Peak",
                    start="16:00", end="19:00",
                    slots_per_day=12,
                    flexibility_mw=0.012,
                    bid_price_chf_mw=9.5,
                    activation_rate=0.25,
                    activation_cost_chf_mwh=2.5
                ),
                # Night: Building cooling, HP might run, low flex
                TimeSlot(
                    name="Night",
                    start="19:00", end="04:00",
                    slots_per_day=36,  # 9 hours × 4 slots/hour
                    flexibility_mw=0.005,
                    bid_price_chf_mw=5.5,
                    activation_rate=0.05,
                    activation_cost_chf_mwh=2.5
                ),
            ]
        )

        # =====================================================================
        # STRATEGY 7: Double Pre-heating - Demand & Price Optimized
        # =====================================================================
        # This strategy is based on empirical demand/price analysis from:
        # - data/csv/demand/price_by_hour_boxplot_15min.csv
        # - data/csv/demand/demand_by_hour_boxplot_15min.csv
        # - data/csv/demand/heatmap_wtp.csv and heatmap_quantity_kw.csv
        #
        # KEY FINDINGS FROM DATA ANALYSIS:
        # 1. Morning Peak (07:00-11:00):
        #    - Prices: ~9.3-10.1 CHF/MW mean
        #    - Demand: ~65-82 kW mean
        #    - Good but not optimal
        #
        # 2. Evening Peak (16:00-20:00) - HIGHEST VALUE!
        #    - Prices: ~10.3-12.1 CHF/MW mean (peak at 17:30 = 12.1 CHF/MW!)
        #    - Demand: ~105-130 kW mean (peak at 17:30 = 128 kW!)
        #    - Thursday/Friday evenings: up to 600+ kW total demand
        #
        # 3. Afternoon Lull (12:00-16:00):
        #    - Prices: ~8.0-8.6 CHF/MW mean (lowest during day)
        #    - Demand: ~38-80 kW mean
        #    - Perfect for second pre-heating period
        #
        # STRATEGY DESIGN:
        # - Pre-heat 1: 04:00-06:00 (off-peak electricity, prepares for morning)
        # - Morning Flex: 07:00-11:00 (HP OFF, sell flexibility)
        # - Transition: 11:00-12:00 (HP can recover if needed)
        # - Pre-heat 2: 12:00-15:00 (afternoon lull, prepares for evening)
        # - Evening Flex: 16:00-20:00 (HP OFF, sell flexibility - PREMIUM!)
        # - Night Recovery: 20:00-04:00 (normal operation, low flex)
        #
        # ENERGY SIGNATURE INTEGRATION:
        # Using data from energy_signature_data_ECM96.csv and ECM97.csv:
        # - ECM96.2: ~2-8 kW daily average depending on temperature
        # - ECM97.3: ~6-14 kW daily average in winter
        # - Pre-heat energy calculated based on previous day's avg temperature
        # - For Feb 2026 (cold month), assume ~5°C avg temp → ~7 kW ECM96, ~11 kW ECM97
        # - Total HP power for pre-heat: ~18 kW = 0.018 MW
        #
        # THERMAL BUDGET PER FLEXIBILITY WINDOW:
        # - Morning window (07:00-11:00): 4 hours, needs ~72 kWh thermal reserve
        # - Evening window (16:00-20:00): 4 hours, needs ~72 kWh thermal reserve
        # - Pre-heat 1 (04:00-06:00): 2h × 18kW × COP(3.5) = 126 kWh thermal ✓
        # - Pre-heat 2 (12:00-15:00): 3h × 18kW × COP(3.5) = 189 kWh thermal ✓
        # =====================================================================
        strategies["strategy_7"] = Strategy(
            name="Strategy 7: Double Pre-heating",
            description="Dual preheat (04-06h + 12-15h) for maximum flex during BOTH morning (07-11h) and evening (16-20h) peaks",
            assets_included=["ECM96.2", "ECM97.3"],  # Only heat pumps that can be pre-heated
            uses_ev=False,
            time_slots=[
                # PRE-HEAT 1: Early morning, prepare for morning peak
                # Off-peak electricity rates apply (~0.08 CHF/kWh = 80 CHF/MWh)
                # This shifts energy from peak (07-11h) to off-peak (04-06h)
                TimeSlot(
                    name="Pre-heat 1 (04:00-06:00)",
                    start="04:00", end="06:00",
                    slots_per_day=8,  # 2 hours × 4 slots/hour
                    flexibility_mw=0.0,  # No flex - HP forced ON
                    bid_price_chf_mw=0.0,
                    activation_rate=0.0,
                    activation_cost_chf_mwh=0.0,
                    is_preheat_period=True,
                    preheat_power_mw=0.018,  # ~18 kW total for ECM96.2 + ECM97.3
                    # Net cost: off-peak (80) - peak avoided (120) = -40 CHF/MWh (savings!)
                    # Conservative estimate: 15 CHF/MWh incremental cost
                    preheat_elec_cost_chf_mwh=15.0,
                ),
                # TRANSITION 1: Building warming up, HP winding down
                TimeSlot(
                    name="Transition 1 (06:00-07:00)",
                    start="06:00", end="07:00",
                    slots_per_day=4,
                    flexibility_mw=0.008,  # Some flex available
                    bid_price_chf_mw=8.0,
                    activation_rate=0.10,
                    activation_cost_chf_mwh=2.5
                ),
                # MORNING PEAK FLEXIBILITY: HP OFF, full capacity to market
                # From data: 07:00-11:00 has prices ~9.3-10.1 CHF/MW, demand ~65-82 kW
                TimeSlot(
                    name="Morning Peak Flex (07:00-11:00)",
                    start="07:00", end="11:00",
                    slots_per_day=16,  # 4 hours × 4 slots/hour
                    flexibility_mw=0.025,  # Higher than S6 - targeting both HPs
                    bid_price_chf_mw=10.0,  # Slightly below peak WTP (~10.1)
                    activation_rate=0.45,  # High rate - reliable availability
                    activation_cost_chf_mwh=1.0,  # Low - HP already OFF
                ),
                # TRANSITION 2: Allow HP recovery if needed
                TimeSlot(
                    name="Transition 2 (11:00-12:00)",
                    start="11:00", end="12:00",
                    slots_per_day=4,
                    flexibility_mw=0.006,  # Reduced flex
                    bid_price_chf_mw=7.5,
                    activation_rate=0.08,
                    activation_cost_chf_mwh=2.5
                ),
                # PRE-HEAT 2: Afternoon lull, prepare for evening peak
                # Data shows 12:00-15:00 has LOWEST demand/prices - perfect for pre-heating
                TimeSlot(
                    name="Pre-heat 2 (12:00-15:00)",
                    start="12:00", end="15:00",
                    slots_per_day=12,  # 3 hours × 4 slots/hour
                    flexibility_mw=0.0,  # No flex - HP forced ON
                    bid_price_chf_mw=0.0,
                    activation_rate=0.0,
                    activation_cost_chf_mwh=0.0,
                    is_preheat_period=True,
                    preheat_power_mw=0.018,  # Same as pre-heat 1
                    # Daytime electricity slightly higher (~100 CHF/MWh)
                    # But we avoid expensive evening peak (150+ CHF/MWh)
                    # Net: 100 - 150 = -50 CHF/MWh (savings!)
                    # Conservative: 10 CHF/MWh
                    preheat_elec_cost_chf_mwh=10.0,
                ),
                # TRANSITION 3: Building charged, prepare for evening peak
                TimeSlot(
                    name="Transition 3 (15:00-16:00)",
                    start="15:00", end="16:00",
                    slots_per_day=4,
                    flexibility_mw=0.010,
                    bid_price_chf_mw=8.5,
                    activation_rate=0.12,
                    activation_cost_chf_mwh=2.5
                ),
                # EVENING PEAK FLEXIBILITY: HP OFF - MAXIMUM VALUE PERIOD!
                # From data: 16:00-20:00 has HIGHEST prices (up to 12.1 CHF/MW at 17:30)
                # and HIGHEST demand (up to 130 kW mean, 600+ kW on Thu/Fri evenings)
                # This is the GOLDEN WINDOW for flexibility sales!
                TimeSlot(
                    name="Evening Peak Flex (16:00-20:00)",
                    start="16:00", end="20:00",
                    slots_per_day=16,  # 4 hours × 4 slots/hour
                    flexibility_mw=0.028,  # Maximum capacity - PREMIUM PERIOD
                    bid_price_chf_mw=11.5,  # Premium price (data shows WTP up to 12.1)
                    activation_rate=0.55,  # Highest rate - DSO needs this period most
                    activation_cost_chf_mwh=0.8,  # Very low - HP pre-conditioned
                ),
                # NIGHT RECOVERY: Normal operation, building cools down
                # HP runs to maintain comfort overnight
                TimeSlot(
                    name="Night Recovery (20:00-04:00)",
                    start="20:00", end="04:00",
                    slots_per_day=32,  # 8 hours × 4 slots/hour
                    flexibility_mw=0.004,  # Minimal flex - HP needs to run
                    bid_price_chf_mw=5.5,
                    activation_rate=0.03,
                    activation_cost_chf_mwh=3.0  # Higher cost if activated - recovery needed
                ),
            ]
        )

        self._apply_activation_rate_overrides(strategies)
        return strategies
    
    def evaluate_time_slot(self, slot: TimeSlot) -> Dict:
        """
        Evaluate a single time slot.
        
        Revenue formula: flexibility_mw × bid_price × activated_slots
        Energy formula: flexibility_mw × 0.25h × activated_slots (MWh)
        Activation cost: energy × activation_cost_per_mwh

        For preheat periods (Strategy 6):
        - No flexibility revenue (HP is forced ON)
        - Preheat cost = preheat_energy × 0.25h × slots × electricity_price
        """
        total_slots = slot.slots_per_day * self.days
        
        # Check if this is a preheat period (Strategy 6)
        if slot.is_preheat_period:
            # Preheat cost: energy consumed × electricity price
            preheat_energy_mwh = slot.preheat_power_mw * 0.25 * total_slots
            preheat_cost = preheat_energy_mwh * slot.preheat_elec_cost_chf_mwh

            return {
                "name": slot.name,
                "period": f"{slot.start}-{slot.end}",
                "total_slots": total_slots,
                "hp_activated": 0,
                "hp_flexibility_mw": 0.0,
                "hp_bid_price": 0.0,
                "hp_revenue": 0.0,
                "hp_energy_mwh": 0.0,
                "hp_activation_cost": 0.0,
                "hp_net": 0.0,
                "ev_activated": 0,
                "ev_revenue": 0,
                "ev_activation_cost": 0,
                "ev_net": 0,
                "is_preheat": True,
                "preheat_power_mw": slot.preheat_power_mw,
                "preheat_energy_mwh": preheat_energy_mwh,
                "preheat_cost": preheat_cost,
                "total_revenue": 0.0,
                "total_activation_cost": preheat_cost,  # Count preheat as a cost
                "total_net": -preheat_cost,  # Negative = cost
                "total_activated": 0,
            }

        # HP component (normal flexibility bidding)
        hp_activated = int(total_slots * slot.activation_rate)
        hp_revenue = slot.flexibility_mw * slot.bid_price_chf_mw * hp_activated
        hp_energy_mwh = slot.flexibility_mw * 0.25 * hp_activated
        hp_activation_cost = hp_energy_mwh * slot.activation_cost_chf_mwh
        hp_net = hp_revenue - hp_activation_cost
        
        result = {
            "name": slot.name,
            "period": f"{slot.start}-{slot.end}",
            "total_slots": total_slots,
            "hp_activated": hp_activated,
            "hp_flexibility_mw": slot.flexibility_mw,
            "hp_bid_price": slot.bid_price_chf_mw,
            "hp_revenue": hp_revenue,
            "hp_energy_mwh": hp_energy_mwh,
            "hp_activation_cost": hp_activation_cost,
            "hp_net": hp_net,
            "ev_activated": 0,
            "ev_revenue": 0,
            "ev_activation_cost": 0,
            "ev_net": 0,
            "is_preheat": False,
            "total_revenue": hp_revenue,
            "total_activation_cost": hp_activation_cost,
            "total_net": hp_net,
            "total_activated": hp_activated,
        }
        
        # EV component (if included)
        if slot.include_ev and slot.ev_flexibility_mw > 0:
            ev_activated = int(total_slots * slot.ev_activation_rate)
            ev_revenue = slot.ev_flexibility_mw * slot.ev_bid_price * ev_activated
            ev_energy_mwh = slot.ev_flexibility_mw * 0.25 * ev_activated
            ev_activation_cost = ev_energy_mwh * slot.ev_activation_cost
            ev_net = ev_revenue - ev_activation_cost
            
            result["ev_activated"] = ev_activated
            result["ev_flexibility_mw"] = slot.ev_flexibility_mw
            result["ev_bid_price"] = slot.ev_bid_price
            result["ev_revenue"] = ev_revenue
            result["ev_energy_mwh"] = ev_energy_mwh
            result["ev_activation_cost"] = ev_activation_cost
            result["ev_net"] = ev_net
            result["total_revenue"] = hp_revenue + ev_revenue
            result["total_activation_cost"] = hp_activation_cost + ev_activation_cost
            result["total_net"] = hp_net + ev_net
            result["total_activated"] = hp_activated + ev_activated
        
        return result
    
    def evaluate_strategy(self, strategy: Strategy) -> StrategyResult:
        """Evaluate a complete strategy."""
        period_results = []
        total_revenue = 0
        total_cost = 0
        total_activated = 0
        
        for slot in strategy.time_slots:
            result = self.evaluate_time_slot(slot)
            period_results.append(result)
            total_revenue += result["total_revenue"]
            total_cost += result["total_activation_cost"]
            total_activated += result["total_activated"]
        
        net_profit = total_revenue - total_cost
        avg_per_slot = net_profit / total_activated if total_activated > 0 else 0
        
        return StrategyResult(
            strategy_name=strategy.name,
            total_revenue_chf=total_revenue,
            total_activation_cost_chf=total_cost,
            net_profit_chf=net_profit,
            total_slots_activated=total_activated,
            avg_profit_per_slot=avg_per_slot,
            period_breakdown=period_results
        )
    
    def evaluate_all(self) -> Dict[str, StrategyResult]:
        """Evaluate all strategies."""
        results = {}
        for strategy_id, strategy in self.strategies.items():
            results[strategy_id] = self.evaluate_strategy(strategy)
        return results
    
    def print_asset_summary(self):
        """Print summary of configured assets."""
        print("\n" + "=" * 70)
        print("ASSET CONFIGURATION SUMMARY")
        print("=" * 70)
        
        print("\nHeat Pumps:")
        print(f"{'Asset':<12} {'Description':<20} {'Capacity':<12} {'Max Flex':<12}")
        print("-" * 56)
        for asset_id, info in self.hp_assets.items():
            print(f"{asset_id:<12} {info['description']:<20} {info['capacity_kw']:>8.1f} kW  {info['max_flex_kw']:>8.1f} kW")
        print(f"{'TOTAL':<12} {'':<20} {self.total_hp_capacity_mw*1000:>8.1f} kW  {self.total_hp_flex_mw*1000:>8.1f} kW")
        
        print("\nEV Chargers:")
        print(f"{'Asset':<12} {'Description':<20} {'Capacity':<12} {'Max Flex':<12}")
        print("-" * 56)
        for asset_id, info in self.ev_assets.items():
            print(f"{asset_id:<12} {info['description']:<20} {info['capacity_kw']:>8.1f} kW  {info['max_flex_kw']:>8.1f} kW")
        print(f"{'TOTAL':<12} {'':<20} {self.total_ev_capacity_mw*1000:>8.1f} kW  {self.total_ev_flex_mw*1000:>8.1f} kW")
        
        print("\n" + "=" * 70)
    
    def print_strategy_details(self, strategy_id: str, result: StrategyResult):
        """Print detailed results for a single strategy."""
        strategy = self.strategies[strategy_id]
        
        print("\n" + "=" * 70)
        print(f"{strategy.name}")
        print(f"Description: {strategy.description}")
        print(f"Uses EV Chargers: {'Yes' if strategy.uses_ev else 'No'}")
        print(f"Assets: {', '.join(strategy.assets_included)}")
        print("=" * 70)
        
        for period in result.period_breakdown:
            print(f"\n--- {period['name']} ({period['period']}) ---")

            # Check if this is a preheat period
            if period.get("is_preheat", False):
                print(f"  Slots: {period['total_slots']} total (HP forced ON for preheating)")
                print(f"  Preheat Power: {period['preheat_power_mw']:.3f} MW")
                print(f"  Preheat Energy: {period['preheat_energy_mwh']:.3f} MWh")
                print(f"  PREHEAT COST: -{period['preheat_cost']:.2f} CHF")
            else:
                print(f"  Slots: {period['total_slots']} total, {period['hp_activated']} HP activated")
                print(f"  HP: {period['hp_flexibility_mw']:.3f} MW × {period['hp_bid_price']:.1f} CHF/MW = {period['hp_revenue']:.2f} CHF")
                print(f"      Cost: {period['hp_energy_mwh']:.3f} MWh × {period.get('hp_activation_cost', 0)/max(period['hp_energy_mwh'], 0.001):.1f} CHF/MWh = {period['hp_activation_cost']:.2f} CHF")
                print(f"      NET: {period['hp_net']:.2f} CHF")

            if period.get("ev_activated", 0) > 0:
                print(f"  EV: {period.get('ev_flexibility_mw', 0):.4f} MW × {period.get('ev_bid_price', 0):.1f} CHF/MW × {period['ev_activated']} = {period['ev_revenue']:.2f} CHF")
                print(f"      NET: {period['ev_net']:.2f} CHF")
        
        print("\n" + "-" * 70)
        print(f"TOTAL ({self.start_date.strftime('%Y-%m-%d')} to {self.end_date.strftime('%Y-%m-%d')} - {self.days} days)")
        print("-" * 70)
        print(f"  Total slots activated: {result.total_slots_activated}")
        print(f"  Total revenue:         {result.total_revenue_chf:.2f} CHF")
        print(f"  Total activation cost: {result.total_activation_cost_chf:.2f} CHF")
        print(f"  NET PROFIT:            {result.net_profit_chf:.2f} CHF")
        print(f"  Average per slot:      {result.avg_profit_per_slot:.3f} CHF")
        print("=" * 70)
    
    def print_comparison_table(self, results: Dict[str, StrategyResult]):
        """Print comparison table of all strategies."""
        print("\n" + "=" * 90)
        print("STRATEGY COMPARISON")
        print("=" * 90)
        print(f"{'Strategy':<35} {'Net Profit':<15} {'Slots':<10} {'Avg/Slot':<12} {'Uses EV':<10}")
        print("-" * 90)
        
        # Sort by net profit descending
        sorted_results = sorted(results.items(), key=lambda x: x[1].net_profit_chf, reverse=True)
        
        best_strategy = sorted_results[0][0]
        
        for strategy_id, result in sorted_results:
            strategy = self.strategies[strategy_id]
            marker = "⭐ BEST" if strategy_id == best_strategy else ""
            ev_str = "Yes" if strategy.uses_ev else "No"
            print(f"{strategy.name:<35} {result.net_profit_chf:>10.2f} CHF  {result.total_slots_activated:>6}    {result.avg_profit_per_slot:>8.3f} CHF  {ev_str:<6} {marker}")
        
        print("=" * 90)
        
        # Recommendation
        best_result = results[best_strategy]
        print(f"\n✅ RECOMMENDATION: {self.strategies[best_strategy].name}")
        print(f"   Period: {self.start_date.strftime('%Y-%m-%d')} to {self.end_date.strftime('%Y-%m-%d')} ({self.days} days)")
        print(f"   Expected net profit: {best_result.net_profit_chf:.2f} CHF")
    
    def export_results(self, results: Dict[str, StrategyResult], output_file: str):
        """Export results to JSON file."""
        export_data = {
            "evaluation_date": datetime.now().isoformat(),
            "evaluation_period": {
                "start_date": self.start_date.strftime("%Y-%m-%d"),
                "end_date": self.end_date.strftime("%Y-%m-%d"),
                "days": self.days
            },
            "asset_summary": {
                "heat_pumps": {
                    "count": len(self.hp_assets),
                    "total_capacity_mw": self.total_hp_capacity_mw,
                    "total_max_flex_mw": self.total_hp_flex_mw,
                    "assets": self.hp_assets
                },
                "ev_chargers": {
                    "count": len(self.ev_assets),
                    "total_capacity_mw": self.total_ev_capacity_mw,
                    "total_max_flex_mw": self.total_ev_flex_mw,
                    "assets": self.ev_assets
                }
            },
            "strategies": {}
        }
        
        for strategy_id, result in results.items():
            strategy = self.strategies[strategy_id]
            export_data["strategies"][strategy_id] = {
                "name": strategy.name,
                "description": strategy.description,
                "uses_ev": strategy.uses_ev,
                "assets_included": strategy.assets_included,
                "results": {
                    "net_profit_chf": round(result.net_profit_chf, 2),
                    "total_revenue_chf": round(result.total_revenue_chf, 2),
                    "total_activation_cost_chf": round(result.total_activation_cost_chf, 2),
                    "total_slots_activated": result.total_slots_activated,
                    "avg_profit_per_slot_chf": round(result.avg_profit_per_slot, 4),
                },
                "period_breakdown": result.period_breakdown
            }
        
        with open(output_file, "w") as f:
            json.dump(export_data, f, indent=2, default=str)
        
        print(f"\n📄 Results exported to: {output_file}")


def parse_date(date_str: str) -> datetime:
    """Parse a date string in YYYY-MM-DD format."""
    try:
        return datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        raise argparse.ArgumentTypeError(f"Invalid date format: {date_str}. Use YYYY-MM-DD")


def main():
    parser = argparse.ArgumentParser(
        description="Evaluate bidding strategies for flexibility market"
    )
    parser.add_argument(
        "--config_file",
        required=True,
        help="Path to configuration file (e.g., conf/test_fm01_aem.json)"
    )
    parser.add_argument(
        "--start_date",
        type=parse_date,
        help="Start date of evaluation period (YYYY-MM-DD). Default: 30 days ago"
    )
    parser.add_argument(
        "--end_date",
        type=parse_date,
        help="End date of evaluation period (YYYY-MM-DD). Default: today"
    )
    parser.add_argument(
        "--output",
        help="Output JSON file for results (optional)"
    )
    parser.add_argument(
        "--strategy",
        choices=["strategy_1", "strategy_2", "strategy_3", "strategy_4", "strategy_5", "strategy_6", "strategy_7", "all"],
        default="all",
        help="Strategy to evaluate (default: all)"
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show detailed breakdown for each strategy"
    )
    parser.add_argument(
        "--plot",
        action="store_true",
        help="Generate plots (strategy schedule overlays + comparison)"
    )
    parser.add_argument(
        "--plot_dir",
        default="data/plots/strategies",
        help="Base output directory for plots (default: data/plots/strategies)"
    )
    parser.add_argument(
        "--demand_dir",
        default="data/csv/demand",
        help="Directory containing demand CSV exports (default: data/csv/demand)"
    )

    # Strategy 7 extra plots inputs
    parser.add_argument(
        "--bid_records_dir",
        default="data/bid_records",
        help="Directory containing bid record JSONs (default: data/bid_records)"
    )
    parser.add_argument(
        "--energy_signature_ecm96_csv",
        default="data/energy_signature/20250101_20251231/energy_signature_data_ECM96.csv",
        help="Daily energy signature CSV for ECM96 (BAU power + temperature)"
    )
    parser.add_argument(
        "--energy_signature_ecm97_csv",
        default="data/energy_signature/20250101_20251231/energy_signature_data_ECM97.csv",
        help="Daily energy signature CSV for ECM97 (BAU power)"
    )
    parser.add_argument(
        "--strategy7_daily_power_csv",
        default=None,
        help="Optional CSV with Strategy 7 daily delivered power (columns: date,power_kw). If omitted, a proxy from bid-records is used."
    )

    args = parser.parse_args()

    # Load configuration
    if not os.path.isfile(args.config_file):
        print(f"ERROR: Configuration file not found: {args.config_file}")
        sys.exit(1)
    
    with open(args.config_file, "r") as f:
        config = json.load(f)
    
    # Set default dates if not provided
    if args.end_date is None:
        end_date = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        end_date = args.end_date
    
    if args.start_date is None:
        start_date = end_date - timedelta(days=29)  # 30 days including end_date
    else:
        start_date = args.start_date
    
    # Validate date range
    if start_date > end_date:
        print(f"ERROR: Start date ({start_date.strftime('%Y-%m-%d')}) must be before end date ({end_date.strftime('%Y-%m-%d')})")
        sys.exit(1)
    
    days = (end_date - start_date).days + 1
    
    print(f"Configuration loaded from: {args.config_file}")
    print(f"Evaluation period: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')} ({days} days)")
    
    # Initialize evaluator
    evaluator = StrategyEvaluator(config, start_date=start_date, end_date=end_date)
    
    # Print asset summary
    evaluator.print_asset_summary()
    
    # Evaluate strategies
    if args.strategy == "all":
        results = evaluator.evaluate_all()
        
        if args.verbose:
            for strategy_id, result in results.items():
                evaluator.print_strategy_details(strategy_id, result)
        
        evaluator.print_comparison_table(results)
    else:
        strategy = evaluator.strategies[args.strategy]
        result = evaluator.evaluate_strategy(strategy)
        evaluator.print_strategy_details(args.strategy, result)
        results = {args.strategy: result}
    
    # Export results if requested
    if args.output:
        evaluator.export_results(results, args.output)

    # Plotting (optional)
    if args.plot:
        base_plot_dir = args.plot_dir
        _ensure_dir(base_plot_dir)

        conns_path = _resolve_connections_path(args.config_file, config)

        # Strategy 7 schedule plots
        if args.strategy in ("strategy_7", "all"):
            s7_dir = os.path.join(base_plot_dir, "strategy_7")
            _ensure_dir(s7_dir)
            out_paths = plot_strategy_7_schedule_overlays(args.demand_dir, s7_dir)

            # Requested plots for Strategy 7
            try:
                # Average-day activated flexibility per 15-min slot (schedule-based)
                out_paths.append(
                    plot_s7_expected_activated_flexibility_profile(
                        strategy=evaluator.strategies["strategy_7"],
                        output_dir=s7_dir,
                    )
                )
            except Exception as e:
                print(f"\n⚠️  Unable to plot Strategy 7 expected activated flexibility profile: {e}")

            try:
                out_paths.append(
                    plot_s7_vs_bau_daily_power_and_temp(
                        energy_signature_ecm96_csv=args.energy_signature_ecm96_csv,
                        energy_signature_ecm97_csv=args.energy_signature_ecm97_csv,
                        bid_records_dir=args.bid_records_dir,
                        output_dir=s7_dir,
                        strategy7_daily_power_csv=args.strategy7_daily_power_csv,
                    )
                )
            except Exception:
                pass

            print("\n📈 Strategy 7 plots saved:")
            for p in out_paths:
                print(f"  - {p}")

        # Strategy heatmaps (all strategies) - use DB activations
        heatmap_paths: List[str] = []
        for strategy_id, strategy in results.items():
            out_dir = os.path.join(base_plot_dir, strategy_id)
            _ensure_dir(out_dir)
            activations_df = _load_db_activations_for_strategy(
                conns_path=conns_path,
                start_date=evaluator.start_date,
                end_date=evaluator.end_date,
                strategy_id=strategy_id,
            )
            heatmap_paths.append(
                plot_strategy_activated_flexibility_heatmap(
                    strategy=evaluator.strategies[strategy_id],
                    start_date=evaluator.start_date,
                    end_date=evaluator.end_date,
                    output_dir=out_dir,
                    out_name=f"{strategy_id}_activated_flexibility_heatmap.png",
                    activations_df=activations_df,
                )
            )

        if heatmap_paths:
            print("\n🗺️  Strategy heatmaps saved:")
            for p in heatmap_paths:
                print(f"  - {p}")

        # Cross-strategy comparison plots (always useful)
        comp_paths = plot_strategies_comparison(results, base_plot_dir)
        print("\n📊 Comparison plots saved:")
        for p in comp_paths:
            print(f"  - {p}")


if __name__ == "__main__":
    main()

