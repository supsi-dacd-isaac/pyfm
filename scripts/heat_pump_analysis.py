#!/usr/bin/env python3
"""
Heat Pump Analysis Script

Analyzes historical data for heat pumps:
- Power consumption patterns
- Statistics by time of day, day of week
- Flexibility availability analysis
- Visualizations (plots, histograms, heatmaps)
"""

import sys
import os
import json
import argparse
from datetime import datetime, timedelta

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from influxdb import InfluxDBClient

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load_config(config_file: str) -> dict:
    """Load configuration from JSON file."""
    with open(config_file, "r") as f:
        cfg = json.load(f)
    
    conn_rel_path = cfg.get("connectionsFile", "")
    config_dir = os.path.dirname(config_file)
    conn_file = os.path.normpath(os.path.join(config_dir, conn_rel_path))
    
    if os.path.exists(conn_file):
        with open(conn_file, "r") as f:
            cfg.update(json.load(f))
    
    return cfg


def load_heat_pump_data(client, asset_id: str, mapping: dict, 
                        start_time: datetime, end_time: datetime) -> pd.DataFrame:
    """Load raw heat pump data from InfluxDB."""
    device_name = mapping.get("device_name_tag")
    field = mapping.get("field", "active_power")
    site = asset_id.split(".")[0]
    
    query = (
        f"SELECT MEAN({field}) as power FROM assets_data "
        f"WHERE time >= '{start_time.strftime('%Y-%m-%dT%H:%M:%SZ')}' "
        f"AND time < '{end_time.strftime('%Y-%m-%dT%H:%M:%SZ')}' "
        f"AND site='{site}' AND device_name='{device_name}' "
        f"GROUP BY time(15m)"
    )
    
    res = client.query(query)
    
    data = []
    for series in res.raw.get("series", []):
        columns = series.get("columns", [])
        values = series.get("values", [])
        
        time_idx = columns.index("time") if "time" in columns else 0
        power_idx = columns.index("power") if "power" in columns else 1
        
        for row in values:
            timestamp = pd.to_datetime(row[time_idx])
            power = row[power_idx] if row[power_idx] is not None else 0
            data.append({
                "timestamp": timestamp,
                "power_w": power,
                "power_kw": power / 1000,
            })
    
    df = pd.DataFrame(data)
    if len(df) > 0:
        df["hour"] = df["timestamp"].dt.hour
        df["minute"] = df["timestamp"].dt.minute
        df["slot_idx"] = df["hour"] * 4 + df["minute"] // 15
        df["day_of_week"] = df["timestamp"].dt.dayofweek
        df["day_name"] = df["timestamp"].dt.day_name()
        df["is_weekend"] = df["day_of_week"] >= 5
        df["date"] = df["timestamp"].dt.date
    
    return df


def load_grouped_data(client, group_name: str, group_config: dict, asset_mapping: dict,
                      start_time: datetime, end_time: datetime) -> pd.DataFrame:
    """Load and aggregate data from multiple assets in a group.
    
    Data from all assets in the group is summed for each timestamp.
    """
    asset_ids = group_config.get("assets", [])
    
    if not asset_ids:
        return pd.DataFrame()
    
    # Load data from each asset
    all_dfs = []
    for asset_id in asset_ids:
        if asset_id not in asset_mapping:
            print(f"    Warning: Asset {asset_id} not found in asset_mapping, skipping")
            continue
        
        mapping = asset_mapping[asset_id]
        df = load_heat_pump_data(client, asset_id, mapping, start_time, end_time)
        
        if len(df) > 0:
            # Keep only timestamp and power columns for aggregation
            df_agg = df[["timestamp", "power_w", "power_kw"]].copy()
            df_agg = df_agg.set_index("timestamp")
            all_dfs.append(df_agg)
    
    if not all_dfs:
        return pd.DataFrame()
    
    # Combine all dataframes and sum power values for each timestamp
    combined = pd.concat(all_dfs, axis=1)
    
    # Sum power_w and power_kw columns separately
    power_w_cols = [i for i in range(0, len(combined.columns), 2)]
    power_kw_cols = [i for i in range(1, len(combined.columns), 2)]
    
    result = pd.DataFrame({
        "timestamp": combined.index,
        "power_w": combined.iloc[:, power_w_cols].sum(axis=1, skipna=True).values,
        "power_kw": combined.iloc[:, power_kw_cols].sum(axis=1, skipna=True).values,
    })
    
    # Add time-based columns
    if len(result) > 0:
        result["hour"] = result["timestamp"].dt.hour
        result["minute"] = result["timestamp"].dt.minute
        result["slot_idx"] = result["hour"] * 4 + result["minute"] // 15
        result["day_of_week"] = result["timestamp"].dt.dayofweek
        result["day_name"] = result["timestamp"].dt.day_name()
        result["is_weekend"] = result["day_of_week"] >= 5
        result["date"] = result["timestamp"].dt.date
    
    return result


def get_group_nominal_power(group_config: dict, asset_mapping: dict, default_power: float = 10.0) -> float:
    """Calculate total nominal power for a group (sum of individual asset capacities)."""
    asset_ids = group_config.get("assets", [])
    total_power = 0.0
    
    for asset_id in asset_ids:
        if asset_id in asset_mapping:
            total_power += asset_mapping[asset_id].get("capacity_kw", default_power)
        else:
            total_power += default_power
    
    return total_power


def calculate_statistics(df: pd.DataFrame, nominal_power_kw: float) -> dict:
    """Calculate statistics for heat pump data."""
    if len(df) == 0:
        return {}
    
    stats = {
        "total_slots": len(df),
        "power_mean_kw": df["power_kw"].mean(),
        "power_std_kw": df["power_kw"].std(),
        "power_min_kw": df["power_kw"].min(),
        "power_max_kw": df["power_kw"].max(),
        "power_median_kw": df["power_kw"].median(),
        "power_p25_kw": df["power_kw"].quantile(0.25),
        "power_p75_kw": df["power_kw"].quantile(0.75),
        "power_p95_kw": df["power_kw"].quantile(0.95),
        "nominal_power_kw": nominal_power_kw,
        "avg_utilization_pct": (df["power_kw"].mean() / nominal_power_kw) * 100 if nominal_power_kw > 0 else 0,
        "max_utilization_pct": (df["power_kw"].max() / nominal_power_kw) * 100 if nominal_power_kw > 0 else 0,
    }
    
    # Active slots (power > 100W threshold)
    active_threshold_kw = 0.1
    stats["active_slots"] = (df["power_kw"] > active_threshold_kw).sum()
    stats["active_rate"] = (df["power_kw"] > active_threshold_kw).mean() * 100
    
    # Weekday vs weekend
    weekday_df = df[~df["is_weekend"]]
    weekend_df = df[df["is_weekend"]]
    
    stats["weekday_power_mean_kw"] = weekday_df["power_kw"].mean() if len(weekday_df) > 0 else 0
    stats["weekend_power_mean_kw"] = weekend_df["power_kw"].mean() if len(weekend_df) > 0 else 0
    stats["weekday_active_rate"] = (weekday_df["power_kw"] > active_threshold_kw).mean() * 100 if len(weekday_df) > 0 else 0
    stats["weekend_active_rate"] = (weekend_df["power_kw"] > active_threshold_kw).mean() * 100 if len(weekend_df) > 0 else 0
    
    return stats


def calculate_hourly_patterns(df: pd.DataFrame) -> pd.DataFrame:
    """Calculate patterns per 15-minute slot."""
    if len(df) == 0:
        return pd.DataFrame()
    
    # Group by slot index
    patterns = df.groupby("slot_idx").agg({
        "power_kw": ["mean", "std", "min", "max", "count", 
                     lambda x: x.quantile(0.25), 
                     lambda x: x.quantile(0.75),
                     lambda x: x.quantile(0.95)]
    }).reset_index()
    
    patterns.columns = ["slot_idx", "power_mean", "power_std", "power_min", 
                        "power_max", "count", "power_p25", "power_p75", "power_p95"]
    
    # Add time labels
    patterns["time_label"] = patterns["slot_idx"].apply(
        lambda x: f"{x // 4:02d}:{(x % 4) * 15:02d}"
    )
    
    return patterns


def calculate_daily_patterns(df: pd.DataFrame) -> pd.DataFrame:
    """Calculate patterns per day of week."""
    if len(df) == 0:
        return pd.DataFrame()
    
    patterns = df.groupby(["day_of_week", "day_name"]).agg({
        "power_kw": ["mean", "std", "max", "count",
                     lambda x: x.quantile(0.95)]
    }).reset_index()
    
    patterns.columns = ["day_of_week", "day_name", "power_mean", "power_std", 
                        "power_max", "total_slots", "power_p95"]
    
    return patterns.sort_values("day_of_week")


def plot_time_series(df: pd.DataFrame, asset_id: str, description: str, 
                     nominal_power_kw: float, output_dir: str):
    """Plot time series of heat pump power consumption."""
    if len(df) == 0:
        return
    
    fig, ax = plt.subplots(figsize=(14, 6))
    
    # Power over time
    ax.plot(df["timestamp"], df["power_kw"], linewidth=0.5, alpha=0.7, color='steelblue')
    ax.axhline(y=nominal_power_kw, color='r', linestyle='--', alpha=0.5, 
               label=f'Nominal power ({nominal_power_kw} kW)')
    ax.set_ylabel("Power (kW)")
    ax.set_xlabel("Date")
    ax.set_title(f"{asset_id} - {description}: Power Consumption Over Time")
    ax.grid(True, alpha=0.3)
    ax.legend()
    ax.set_ylim(bottom=0)
    
    plt.tight_layout()
    plt.savefig(os.path.join(output_dir, f"{asset_id}_time_series.png"), dpi=150)
    plt.close()


def plot_hourly_patterns(patterns: pd.DataFrame, asset_id: str, description: str, 
                         nominal_power_kw: float, output_dir: str):
    """Plot hourly patterns (average power by time of day)."""
    if len(patterns) == 0:
        return
    
    fig, axes = plt.subplots(2, 1, figsize=(14, 8))
    
    # Average power by time slot with error bars
    ax1 = axes[0]
    bars1 = ax1.bar(patterns["slot_idx"], patterns["power_mean"], 
                    yerr=patterns["power_std"], capsize=1, alpha=0.7, color='steelblue',
                    error_kw={'linewidth': 0.5, 'alpha': 0.5})
    ax1.axhline(y=nominal_power_kw, color='r', linestyle='--', alpha=0.5, 
                label=f'Nominal ({nominal_power_kw} kW)')
    ax1.set_ylabel("Average Power (kW)")
    ax1.set_title(f"{asset_id} - {description}: Average Power by Time of Day")
    ax1.set_xticks(range(0, 96, 4))
    ax1.set_xticklabels([f"{h:02d}:00" for h in range(24)], rotation=45)
    ax1.grid(True, alpha=0.3, axis='y')
    ax1.legend()
    ax1.set_ylim(bottom=0)
    
    # Percentiles view
    ax2 = axes[1]
    ax2.fill_between(patterns["slot_idx"], patterns["power_p25"], patterns["power_p75"], 
                     alpha=0.3, color='steelblue', label='25th-75th percentile')
    ax2.plot(patterns["slot_idx"], patterns["power_mean"], 
             color='steelblue', linewidth=2, label='Mean')
    ax2.plot(patterns["slot_idx"], patterns["power_p95"], 
             color='coral', linewidth=1, linestyle='--', label='95th percentile')
    ax2.axhline(y=nominal_power_kw, color='r', linestyle='--', alpha=0.5, 
                label=f'Nominal ({nominal_power_kw} kW)')
    ax2.set_ylabel("Power (kW)")
    ax2.set_xlabel("Time of Day")
    ax2.set_title("Power Distribution by Time of Day")
    ax2.set_xticks(range(0, 96, 4))
    ax2.set_xticklabels([f"{h:02d}:00" for h in range(24)], rotation=45)
    ax2.grid(True, alpha=0.3, axis='y')
    ax2.legend()
    ax2.set_ylim(bottom=0)
    
    plt.tight_layout()
    plt.savefig(os.path.join(output_dir, f"{asset_id}_hourly_patterns.png"), dpi=150)
    plt.close()


def plot_daily_patterns(patterns: pd.DataFrame, asset_id: str, description: str, 
                        nominal_power_kw: float, output_dir: str):
    """Plot patterns by day of week."""
    if len(patterns) == 0:
        return
    
    fig, axes = plt.subplots(1, 2, figsize=(12, 5))
    
    # Average power by day
    ax1 = axes[0]
    colors = ['coral' if i >= 5 else 'steelblue' for i in patterns["day_of_week"]]
    bars1 = ax1.bar(patterns["day_name"], patterns["power_mean"], 
                    yerr=patterns["power_std"], capsize=3, alpha=0.7, color=colors)
    ax1.axhline(y=nominal_power_kw, color='r', linestyle='--', alpha=0.5, 
                label=f'Nominal ({nominal_power_kw} kW)')
    ax1.set_ylabel("Average Power (kW)")
    ax1.set_title(f"{asset_id}: Average Power by Day of Week")
    ax1.tick_params(axis='x', rotation=45)
    ax1.grid(True, alpha=0.3, axis='y')
    ax1.legend()
    ax1.set_ylim(bottom=0)
    
    # Max and P95 power by day
    ax2 = axes[1]
    x = np.arange(len(patterns))
    width = 0.35
    bars2 = ax2.bar(x - width/2, patterns["power_max"], width, 
                    alpha=0.7, color='coral', label='Max')
    bars3 = ax2.bar(x + width/2, patterns["power_p95"], width, 
                    alpha=0.7, color='steelblue', label='95th percentile')
    ax2.axhline(y=nominal_power_kw, color='r', linestyle='--', alpha=0.5, 
                label=f'Nominal ({nominal_power_kw} kW)')
    ax2.set_xticks(x)
    ax2.set_xticklabels(patterns["day_name"], rotation=45)
    ax2.set_ylabel("Power (kW)")
    ax2.set_title(f"{asset_id}: Peak Power by Day of Week")
    ax2.legend()
    ax2.grid(True, alpha=0.3, axis='y')
    ax2.set_ylim(bottom=0)
    
    plt.tight_layout()
    plt.savefig(os.path.join(output_dir, f"{asset_id}_daily_patterns.png"), dpi=150)
    plt.close()


def plot_power_histogram(df: pd.DataFrame, asset_id: str, description: str, 
                         nominal_power_kw: float, output_dir: str):
    """Plot histogram of power consumption."""
    if len(df) == 0:
        return
    
    fig, axes = plt.subplots(1, 2, figsize=(12, 5))
    
    # All power values
    ax1 = axes[0]
    ax1.hist(df["power_kw"], bins=50, alpha=0.7, color='steelblue', edgecolor='black')
    ax1.axvline(x=nominal_power_kw, color='r', linestyle='--', 
                label=f'Nominal power ({nominal_power_kw} kW)')
    ax1.set_xlabel("Power (kW)")
    ax1.set_ylabel("Frequency")
    ax1.set_title(f"{asset_id}: Power Distribution (All Slots)")
    ax1.legend()
    ax1.grid(True, alpha=0.3)
    
    # CDF (Cumulative Distribution Function)
    ax2 = axes[1]
    sorted_power = np.sort(df["power_kw"])
    cdf = np.arange(1, len(sorted_power) + 1) / len(sorted_power)
    ax2.plot(sorted_power, cdf * 100, color='steelblue', linewidth=2)
    ax2.axvline(x=nominal_power_kw, color='r', linestyle='--', alpha=0.5,
                label=f'Nominal power ({nominal_power_kw} kW)')
    ax2.axhline(y=50, color='gray', linestyle=':', alpha=0.5, label='Median')
    ax2.axhline(y=95, color='orange', linestyle=':', alpha=0.5, label='95th percentile')
    ax2.set_xlabel("Power (kW)")
    ax2.set_ylabel("Cumulative Probability (%)")
    ax2.set_title(f"{asset_id}: Power CDF")
    ax2.legend()
    ax2.grid(True, alpha=0.3)
    ax2.set_xlim(left=0)
    ax2.set_ylim(0, 100)
    
    plt.tight_layout()
    plt.savefig(os.path.join(output_dir, f"{asset_id}_power_histogram.png"), dpi=150)
    plt.close()


def plot_power_heatmap(df: pd.DataFrame, asset_id: str, description: str, 
                       nominal_power_kw: float, output_dir: str):
    """Plot heatmap of average power by hour and day of week."""
    if len(df) == 0:
        return
    
    # Create pivot table: rows = hours (0-23), columns = days (0-6)
    heatmap_data = df.groupby(["hour", "day_of_week"])["power_kw"].mean().unstack()
    
    # Fill missing days/hours with 0
    heatmap_data = heatmap_data.reindex(
        index=range(24), 
        columns=range(7), 
        fill_value=0
    )
    
    fig, ax = plt.subplots(figsize=(10, 8))
    
    # Use a colormap that shows power intensity
    im = ax.imshow(heatmap_data.values, cmap='YlOrRd', aspect='auto', 
                   vmin=0, vmax=max(nominal_power_kw, heatmap_data.values.max()))
    
    ax.set_xticks(range(7))
    ax.set_xticklabels(['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'])
    ax.set_yticks(range(24))
    ax.set_yticklabels([f"{h:02d}:00" for h in range(24)])
    
    ax.set_xlabel("Day of Week")
    ax.set_ylabel("Hour of Day")
    ax.set_title(f"{asset_id} - {description}: Average Power Heatmap (kW)")
    
    cbar = plt.colorbar(im, ax=ax, label='Average Power (kW)')
    
    # Add text annotations
    for i in range(24):
        for j in range(7):
            value = heatmap_data.values[i, j]
            if value > 0.1:  # Only annotate non-negligible values
                text_color = 'white' if value > (nominal_power_kw * 0.5) else 'black'
                ax.text(j, i, f'{value:.1f}', ha='center', va='center', 
                       fontsize=6, color=text_color)
    
    plt.tight_layout()
    plt.savefig(os.path.join(output_dir, f"{asset_id}_power_heatmap.png"), dpi=150)
    plt.close()


def plot_flexibility_heatmap(df: pd.DataFrame, asset_id: str, description: str, 
                              nominal_power_kw: float, output_dir: str):
    """Plot heatmap of available downward flexibility probability by hour and day of week.
    
    Shows the probability of having at least 25%, 50%, 75%, 100% of the nominal 
    power available as DOWNWARD flexibility (i.e., load that can be curtailed/reduced).
    
    Downward flexibility = current power consumption (what can be reduced)
    """
    if len(df) == 0:
        return
    
    df = df.copy()
    
    # Flexibility levels to analyze (percentage of nominal power that can be curtailed)
    flexibility_levels = [
        (0.25, "25%", f"≥ {nominal_power_kw * 0.25:.2f} kW"),
        (0.50, "50%", f"≥ {nominal_power_kw * 0.50:.2f} kW"),
        (0.75, "75%", f"≥ {nominal_power_kw * 0.75:.2f} kW"),
        (1.00, "100%", f"≥ {nominal_power_kw:.2f} kW"),
    ]
    
    fig, axes = plt.subplots(2, 2, figsize=(14, 12))
    axes = axes.flatten()
    
    for idx, (level, level_str, power_str) in enumerate(flexibility_levels):
        ax = axes[idx]
        
        # Calculate threshold: power usage must be at least (level * nominal)
        # to have at least 'level' downward flexibility available
        min_power_kw = nominal_power_kw * level
        
        # Mark slots where heat pump has enough power to curtail
        # Flexibility = current power being used (can be reduced to 0)
        df[f"flex_{level_str}"] = df["power_kw"] >= min_power_kw
        
        # Create pivot table: rows = hours (0-23), columns = days (0-6)
        heatmap_data = df.groupby(["hour", "day_of_week"])[f"flex_{level_str}"].mean().unstack()
        
        # Fill missing days/hours with 0
        heatmap_data = heatmap_data.reindex(
            index=range(24), 
            columns=range(7), 
            fill_value=0
        )
        
        im = ax.imshow(heatmap_data.values * 100, cmap='YlGnBu', aspect='auto', 
                       vmin=0, vmax=100)
        
        ax.set_xticks(range(7))
        ax.set_xticklabels(['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'])
        ax.set_yticks(range(24))
        ax.set_yticklabels([f"{h:02d}:00" for h in range(24)])
        
        ax.set_xlabel("Day of Week")
        ax.set_ylabel("Hour of Day")
        ax.set_title(f"{level_str} Downward Flexibility ({power_str})")
        
        plt.colorbar(im, ax=ax, label='Probability (%)')
        
        # Add text annotations for cells with notable values
        for i in range(24):
            for j in range(7):
                value = heatmap_data.values[i, j] * 100
                if value > 0:
                    text_color = 'white' if value > 50 else 'black'
                    ax.text(j, i, f'{value:.0f}', ha='center', va='center', 
                           fontsize=6, color=text_color)
    
    fig.suptitle(f"{asset_id} - {description}: Downward Flexibility Heatmap\n"
                 f"(Nominal Power: {nominal_power_kw} kW - Probability of having curtailable load)", 
                 fontsize=12, fontweight='bold')
    
    plt.tight_layout()
    plt.savefig(os.path.join(output_dir, f"{asset_id}_flexibility_heatmap.png"), dpi=150)
    plt.close()


def plot_comparison(all_data: dict, output_dir: str):
    """Plot comparison between heat pumps."""
    if len(all_data) < 2:
        return
    
    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    
    asset_ids = list(all_data.keys())
    colors = plt.cm.Set2(np.linspace(0, 1, len(asset_ids)))
    
    # Compare average power by hour
    ax1 = axes[0, 0]
    for i, (asset_id, data) in enumerate(all_data.items()):
        patterns = data["hourly_patterns"]
        if len(patterns) > 0:
            ax1.plot(patterns["slot_idx"], patterns["power_mean"], 
                    label=data["description"], color=colors[i], linewidth=2)
    ax1.set_xlabel("Time Slot")
    ax1.set_ylabel("Average Power (kW)")
    ax1.set_title("Average Power Consumption by Time of Day")
    ax1.legend()
    ax1.set_xticks(range(0, 96, 8))
    ax1.set_xticklabels([f"{h:02d}:00" for h in range(0, 24, 2)])
    ax1.grid(True, alpha=0.3)
    ax1.set_ylim(bottom=0)
    
    # Compare 95th percentile by hour
    ax2 = axes[0, 1]
    for i, (asset_id, data) in enumerate(all_data.items()):
        patterns = data["hourly_patterns"]
        if len(patterns) > 0:
            ax2.plot(patterns["slot_idx"], patterns["power_p95"], 
                    label=data["description"], color=colors[i], linewidth=2)
    ax2.set_xlabel("Time Slot")
    ax2.set_ylabel("95th Percentile Power (kW)")
    ax2.set_title("Peak Power (95th Percentile) by Time of Day")
    ax2.legend()
    ax2.set_xticks(range(0, 96, 8))
    ax2.set_xticklabels([f"{h:02d}:00" for h in range(0, 24, 2)])
    ax2.grid(True, alpha=0.3)
    ax2.set_ylim(bottom=0)
    
    # Compare overall statistics
    ax3 = axes[1, 0]
    stats_data = {
        "Avg Power\n(kW)": [data["stats"]["power_mean_kw"] for data in all_data.values()],
        "P95 Power\n(kW)": [data["stats"]["power_p95_kw"] for data in all_data.values()],
        "Utilization\n(%)": [data["stats"]["avg_utilization_pct"] for data in all_data.values()],
    }
    x = np.arange(len(stats_data))
    width = 0.8 / len(all_data)
    
    for i, (asset_id, data) in enumerate(all_data.items()):
        offset = width * (i - len(all_data)/2 + 0.5)
        values = [stats_data[k][i] for k in stats_data.keys()]
        ax3.bar(x + offset, values, width, label=data["description"], color=colors[i])
    
    ax3.set_xticks(x)
    ax3.set_xticklabels(stats_data.keys())
    ax3.set_ylabel("Value")
    ax3.set_title("Comparison of Key Statistics")
    ax3.legend()
    ax3.grid(True, alpha=0.3, axis='y')
    
    # Weekday vs Weekend comparison
    ax4 = axes[1, 1]
    x = np.arange(2)
    width = 0.8 / len(all_data)
    
    for i, (asset_id, data) in enumerate(all_data.items()):
        stats = data["stats"]
        offset = width * (i - len(all_data)/2 + 0.5)
        values = [stats["weekday_power_mean_kw"], stats["weekend_power_mean_kw"]]
        ax4.bar(x + offset, values, width, label=data["description"], color=colors[i])
    
    ax4.set_xticks(x)
    ax4.set_xticklabels(["Weekday", "Weekend"])
    ax4.set_ylabel("Average Power (kW)")
    ax4.set_title("Weekday vs Weekend Power Consumption")
    ax4.legend()
    ax4.grid(True, alpha=0.3, axis='y')
    ax4.set_ylim(bottom=0)
    
    plt.tight_layout()
    plt.savefig(os.path.join(output_dir, "heat_pumps_comparison.png"), dpi=150)
    plt.close()


def print_statistics(all_data: dict):
    """Print detailed statistics for all heat pumps."""
    print("\n" + "=" * 80)
    print("HEAT PUMP ANALYSIS - STATISTICS")
    print("=" * 80)
    
    for asset_id, data in all_data.items():
        stats = data["stats"]
        print(f"\n{'─' * 80}")
        print(f"Asset: {asset_id} ({data['description']})")
        print(f"{'─' * 80}")
        
        print(f"\n  OVERALL STATISTICS:")
        print(f"    Total data points:         {stats['total_slots']:,}")
        print(f"    Time period:               {data['days_back']} days")
        print(f"    Nominal power:             {stats['nominal_power_kw']:.1f} kW")
        
        print(f"\n  ACTIVITY:")
        print(f"    Active rate (>100W):       {stats['active_rate']:.1f}%")
        print(f"    Active slots:              {stats['active_slots']:,} / {stats['total_slots']:,}")
        print(f"    Weekday active rate:       {stats['weekday_active_rate']:.1f}%")
        print(f"    Weekend active rate:       {stats['weekend_active_rate']:.1f}%")
        
        print(f"\n  POWER CONSUMPTION:")
        print(f"    Mean power:                {stats['power_mean_kw']:.2f} kW")
        print(f"    Std deviation:             {stats['power_std_kw']:.2f} kW")
        print(f"    Min power:                 {stats['power_min_kw']:.2f} kW")
        print(f"    Max power:                 {stats['power_max_kw']:.2f} kW")
        print(f"    Median power:              {stats['power_median_kw']:.2f} kW")
        print(f"    25th percentile:           {stats['power_p25_kw']:.2f} kW")
        print(f"    75th percentile:           {stats['power_p75_kw']:.2f} kW")
        print(f"    95th percentile:           {stats['power_p95_kw']:.2f} kW")
        
        print(f"\n  UTILIZATION:")
        print(f"    Avg utilization:           {stats['avg_utilization_pct']:.1f}%")
        print(f"    Max utilization:           {stats['max_utilization_pct']:.1f}%")
        print(f"    Weekday avg power:         {stats['weekday_power_mean_kw']:.2f} kW")
        print(f"    Weekend avg power:         {stats['weekend_power_mean_kw']:.2f} kW")
        
        # Top power consumption hours
        patterns = data["hourly_patterns"]
        if len(patterns) > 0:
            top_hours = patterns.nlargest(5, "power_mean")[["time_label", "power_mean", "power_p95"]]
            print(f"\n  TOP 5 SLOTS BY AVERAGE POWER:")
            for _, row in top_hours.iterrows():
                print(f"    {row['time_label']}: {row['power_mean']:.2f} kW avg, {row['power_p95']:.2f} kW P95")
    
    # Summary comparison
    print(f"\n{'=' * 80}")
    print("SUMMARY COMPARISON")
    print("=" * 80)
    print(f"\n{'Asset':<15} {'Nominal':>10} {'Avg Power':>12} {'P95 Power':>12} {'Utilization':>12} {'Active Rate':>12}")
    print("-" * 80)
    
    for asset_id, data in all_data.items():
        stats = data["stats"]
        print(f"{asset_id:<15} {stats['nominal_power_kw']:>8.1f} kW {stats['power_mean_kw']:>10.2f} kW "
              f"{stats['power_p95_kw']:>10.2f} kW {stats['avg_utilization_pct']:>10.1f}% "
              f"{stats['active_rate']:>10.1f}%")
    
    print("=" * 80)


def main():
    parser = argparse.ArgumentParser(description="Analyze Heat Pump Historical Data")
    parser.add_argument(
        "--config_file",
        type=str,
        default="../conf/test_fm01_aem.json",
        help="Configuration file path"
    )
    parser.add_argument(
        "--days_back",
        type=int,
        default=30,
        help="Number of days to analyze"
    )
    parser.add_argument(
        "--output_dir",
        type=str,
        default="../data/hp_analysis",
        help="Output directory for plots"
    )
    
    args = parser.parse_args()
    
    # Load config
    config_path = os.path.join(os.path.dirname(__file__), args.config_file)
    cfg = load_config(config_path)
    
    # Create output directory
    output_dir = os.path.join(os.path.dirname(__file__), args.output_dir)
    os.makedirs(output_dir, exist_ok=True)
    
    # Create InfluxDB client
    client = InfluxDBClient(
        host=cfg["influxDB"]["host"],
        port=cfg["influxDB"]["port"],
        password=cfg["influxDB"]["password"],
        username=cfg["influxDB"]["user"],
        database=cfg["influxDB"]["database"],
        ssl=cfg["influxDB"]["ssl"],
    )
    
    # Time range
    end_time = datetime.utcnow()
    start_time = end_time - timedelta(days=args.days_back)
    
    print(f"\nHeat Pump Analysis")
    print(f"Time range: {start_time.strftime('%Y-%m-%d')} to {end_time.strftime('%Y-%m-%d')}")
    print(f"Output directory: {output_dir}")
    
    # Get heat pumps from config
    asset_mapping = cfg.get("asset_mapping", {})
    heat_pumps = {
        k: v for k, v in asset_mapping.items() 
        if isinstance(v, dict) and v.get("type") == "heat_pump"
    }
    
    if not heat_pumps:
        print("\nNo heat pumps found in configuration!")
        return
    
    print(f"\nFound {len(heat_pumps)} heat pumps: {list(heat_pumps.keys())}")
    
    # Analyze each heat pump
    all_data = {}
    
    for asset_id, mapping in heat_pumps.items():
        description = mapping.get("description", asset_id)
        nominal_power_kw = mapping.get("capacity_kw", 10.0)  # Default 10 kW if not specified
        print(f"\nLoading data for {asset_id} ({description}, {nominal_power_kw} kW)...")
        
        # Load data
        df = load_heat_pump_data(client, asset_id, mapping, start_time, end_time)
        
        if len(df) == 0:
            print(f"  No data found for {asset_id}")
            continue
        
        print(f"  Loaded {len(df)} data points")
        
        # Calculate statistics
        stats = calculate_statistics(df, nominal_power_kw)
        hourly_patterns = calculate_hourly_patterns(df)
        daily_patterns = calculate_daily_patterns(df)
        
        # Store for comparison
        all_data[asset_id] = {
            "description": description,
            "df": df,
            "stats": stats,
            "hourly_patterns": hourly_patterns,
            "daily_patterns": daily_patterns,
            "days_back": args.days_back,
            "nominal_power_kw": nominal_power_kw,
        }
        
        # Generate plots for this heat pump
        print(f"  Generating plots...")
        plot_time_series(df, asset_id, description, nominal_power_kw, output_dir)
        plot_hourly_patterns(hourly_patterns, asset_id, description, nominal_power_kw, output_dir)
        plot_daily_patterns(daily_patterns, asset_id, description, nominal_power_kw, output_dir)
        plot_power_histogram(df, asset_id, description, nominal_power_kw, output_dir)
        plot_power_heatmap(df, asset_id, description, nominal_power_kw, output_dir)
        plot_flexibility_heatmap(df, asset_id, description, nominal_power_kw, output_dir)
    
    # Generate comparison plots
    if len(all_data) >= 2:
        print("\nGenerating comparison plots...")
        plot_comparison(all_data, output_dir)
    
    # Print statistics
    print_statistics(all_data)
    
    # =========================================================================
    # GROUPED ANALYSIS
    # =========================================================================
    assets_grouping = cfg.get("assets_grouping", {})
    heat_pump_groups = {
        k: v for k, v in assets_grouping.items()
        if isinstance(v, dict) and v.get("type") == "heat_pump"
    }
    
    if heat_pump_groups:
        print(f"\n{'=' * 80}")
        print("GROUPED ANALYSIS")
        print(f"{'=' * 80}")
        print(f"\nFound {len(heat_pump_groups)} heat pump groups: {list(heat_pump_groups.keys())}")
        
        all_group_data = {}
        
        for group_name, group_config in heat_pump_groups.items():
            asset_ids = group_config.get("assets", [])
            print(f"\nLoading grouped data for {group_name} ({len(asset_ids)} assets: {asset_ids})...")
            
            # Load aggregated data
            df = load_grouped_data(client, group_name, group_config, asset_mapping, 
                                   start_time, end_time)
            
            if len(df) == 0:
                print(f"  No data found for group {group_name}")
                continue
            
            print(f"  Loaded {len(df)} aggregated data points")
            
            # Calculate group nominal power (sum of individual capacities)
            group_nominal_power = get_group_nominal_power(group_config, asset_mapping)
            description = f"Group: {', '.join(asset_ids)} (Total: {group_nominal_power:.1f} kW)"
            
            # Calculate statistics
            stats = calculate_statistics(df, group_nominal_power)
            hourly_patterns = calculate_hourly_patterns(df)
            daily_patterns = calculate_daily_patterns(df)
            
            # Store for comparison
            all_group_data[group_name] = {
                "description": description,
                "df": df,
                "stats": stats,
                "hourly_patterns": hourly_patterns,
                "daily_patterns": daily_patterns,
                "days_back": args.days_back,
                "nominal_power_kw": group_nominal_power,
            }
            
            # Generate plots for this group
            print(f"  Generating plots for group...")
            plot_time_series(df, group_name, description, group_nominal_power, output_dir)
            plot_hourly_patterns(hourly_patterns, group_name, description, group_nominal_power, output_dir)
            plot_daily_patterns(daily_patterns, group_name, description, group_nominal_power, output_dir)
            plot_power_histogram(df, group_name, description, group_nominal_power, output_dir)
            plot_power_heatmap(df, group_name, description, group_nominal_power, output_dir)
            plot_flexibility_heatmap(df, group_name, description, group_nominal_power, output_dir)
        
        # Print group statistics
        if all_group_data:
            print_statistics(all_group_data)
    
    print(f"\nPlots saved to: {output_dir}")
    print("Done!")


if __name__ == "__main__":
    main()

