#!/usr/bin/env python3
"""
EV Charger Analysis Script

Analyzes historical data for EV chargers:
- Occupancy patterns (when cars are plugged in)
- Charging power patterns
- Statistics by time of day, day of week
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


def load_ev_charger_data(client, asset_id: str, mapping: dict, 
                          start_time: datetime, end_time: datetime) -> pd.DataFrame:
    """Load raw EV charger data from InfluxDB."""
    device_name = mapping.get("device_name_tag")
    field = mapping.get("field", "power")
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
        df = load_ev_charger_data(client, asset_id, mapping, start_time, end_time)
        
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


def get_group_nominal_power(group_config: dict, asset_mapping: dict, default_power: float = 11.0) -> float:
    """Calculate total nominal power for a group (sum of individual asset capacities)."""
    asset_ids = group_config.get("assets", [])
    total_power = 0.0
    
    for asset_id in asset_ids:
        if asset_id in asset_mapping:
            total_power += asset_mapping[asset_id].get("capacity_kw", default_power)
        else:
            total_power += default_power
    
    return total_power


def calculate_statistics(df: pd.DataFrame, occupancy_threshold: float = 100) -> dict:
    """Calculate statistics for EV charger data."""
    if len(df) == 0:
        return {}
    
    # Mark occupied slots (power > threshold)
    df["is_occupied"] = df["power_w"] > occupancy_threshold
    
    stats = {
        "total_slots": len(df),
        "occupied_slots": df["is_occupied"].sum(),
        "occupancy_rate": df["is_occupied"].mean() * 100,
        "power_mean_kw": df["power_kw"].mean(),
        "power_std_kw": df["power_kw"].std(),
        "power_min_kw": df["power_kw"].min(),
        "power_max_kw": df["power_kw"].max(),
        "power_median_kw": df["power_kw"].median(),
        "power_when_charging_mean_kw": df.loc[df["is_occupied"], "power_kw"].mean() if df["is_occupied"].any() else 0,
        "power_when_charging_max_kw": df.loc[df["is_occupied"], "power_kw"].max() if df["is_occupied"].any() else 0,
    }
    
    # Weekday vs weekend
    weekday_df = df[~df["is_weekend"]]
    weekend_df = df[df["is_weekend"]]
    
    stats["weekday_occupancy_rate"] = weekday_df["is_occupied"].mean() * 100 if len(weekday_df) > 0 else 0
    stats["weekend_occupancy_rate"] = weekend_df["is_occupied"].mean() * 100 if len(weekend_df) > 0 else 0
    stats["weekday_power_mean_kw"] = weekday_df["power_kw"].mean() if len(weekday_df) > 0 else 0
    stats["weekend_power_mean_kw"] = weekend_df["power_kw"].mean() if len(weekend_df) > 0 else 0
    
    return stats


def calculate_hourly_patterns(df: pd.DataFrame, occupancy_threshold: float = 100) -> pd.DataFrame:
    """Calculate patterns per 15-minute slot."""
    if len(df) == 0:
        return pd.DataFrame()
    
    df["is_occupied"] = df["power_w"] > occupancy_threshold
    
    # Group by slot index
    patterns = df.groupby("slot_idx").agg({
        "power_kw": ["mean", "std", "min", "max", "count"],
        "is_occupied": ["sum", "mean"]
    }).reset_index()
    
    patterns.columns = ["slot_idx", "power_mean", "power_std", "power_min", 
                        "power_max", "count", "occupied_count", "occupancy_rate"]
    
    # Add time labels
    patterns["time_label"] = patterns["slot_idx"].apply(
        lambda x: f"{x // 4:02d}:{(x % 4) * 15:02d}"
    )
    
    return patterns


def calculate_daily_patterns(df: pd.DataFrame, occupancy_threshold: float = 100) -> pd.DataFrame:
    """Calculate patterns per day of week."""
    if len(df) == 0:
        return pd.DataFrame()
    
    df["is_occupied"] = df["power_w"] > occupancy_threshold
    
    patterns = df.groupby(["day_of_week", "day_name"]).agg({
        "power_kw": ["mean", "std", "max"],
        "is_occupied": ["sum", "mean", "count"]
    }).reset_index()
    
    patterns.columns = ["day_of_week", "day_name", "power_mean", "power_std", 
                        "power_max", "occupied_count", "occupancy_rate", "total_slots"]
    
    return patterns.sort_values("day_of_week")


def plot_time_series(df: pd.DataFrame, asset_id: str, description: str, output_dir: str):
    """Plot time series of charging power."""
    if len(df) == 0:
        return
    
    fig, axes = plt.subplots(2, 1, figsize=(14, 8), sharex=True)
    
    # Power over time
    ax1 = axes[0]
    ax1.plot(df["timestamp"], df["power_kw"], linewidth=0.5, alpha=0.7)
    ax1.set_ylabel("Power (kW)")
    ax1.set_title(f"{asset_id} - {description}: Charging Power Over Time")
    ax1.grid(True, alpha=0.3)
    ax1.axhline(y=0.1, color='r', linestyle='--', alpha=0.5, label='Occupancy threshold (0.1 kW)')
    ax1.legend()
    
    # Occupancy (binary)
    ax2 = axes[1]
    ax2.fill_between(df["timestamp"], df["power_w"] > 100, alpha=0.5, color='green', label='Occupied')
    ax2.set_ylabel("Occupied")
    ax2.set_xlabel("Date")
    ax2.set_title("Charger Occupancy (Car Plugged In)")
    ax2.grid(True, alpha=0.3)
    ax2.set_yticks([0, 1])
    ax2.set_yticklabels(["No", "Yes"])
    
    plt.tight_layout()
    plt.savefig(os.path.join(output_dir, f"{asset_id}_time_series.png"), dpi=150)
    plt.close()


def plot_hourly_patterns(patterns: pd.DataFrame, asset_id: str, description: str, output_dir: str):
    """Plot hourly patterns (average power and occupancy by time of day)."""
    if len(patterns) == 0:
        return
    
    fig, axes = plt.subplots(2, 1, figsize=(14, 8))
    
    # Average power by time slot
    ax1 = axes[0]
    bars1 = ax1.bar(patterns["slot_idx"], patterns["power_mean"], 
                    yerr=patterns["power_std"], capsize=2, alpha=0.7, color='steelblue')
    ax1.set_ylabel("Average Power (kW)")
    ax1.set_title(f"{asset_id} - {description}: Average Charging Power by Time of Day")
    ax1.set_xticks(range(0, 96, 4))
    ax1.set_xticklabels([f"{h:02d}:00" for h in range(24)], rotation=45)
    ax1.grid(True, alpha=0.3, axis='y')
    
    # Occupancy rate by time slot
    ax2 = axes[1]
    colors = ['green' if occ > 0.1 else 'lightgray' for occ in patterns["occupancy_rate"]]
    bars2 = ax2.bar(patterns["slot_idx"], patterns["occupancy_rate"] * 100, 
                    alpha=0.7, color=colors)
    ax2.set_ylabel("Occupancy Rate (%)")
    ax2.set_xlabel("Time of Day")
    ax2.set_title("Occupancy Probability by Time of Day")
    ax2.set_xticks(range(0, 96, 4))
    ax2.set_xticklabels([f"{h:02d}:00" for h in range(24)], rotation=45)
    ax2.grid(True, alpha=0.3, axis='y')
    ax2.set_ylim(0, 100)
    
    plt.tight_layout()
    plt.savefig(os.path.join(output_dir, f"{asset_id}_hourly_patterns.png"), dpi=150)
    plt.close()


def plot_daily_patterns(patterns: pd.DataFrame, asset_id: str, description: str, output_dir: str):
    """Plot patterns by day of week."""
    if len(patterns) == 0:
        return
    
    fig, axes = plt.subplots(1, 2, figsize=(12, 5))
    
    # Average power by day
    ax1 = axes[0]
    bars1 = ax1.bar(patterns["day_name"], patterns["power_mean"], 
                    yerr=patterns["power_std"], capsize=3, alpha=0.7, color='steelblue')
    ax1.set_ylabel("Average Power (kW)")
    ax1.set_title(f"{asset_id}: Average Power by Day of Week")
    ax1.tick_params(axis='x', rotation=45)
    ax1.grid(True, alpha=0.3, axis='y')
    
    # Occupancy rate by day
    ax2 = axes[1]
    colors = ['coral' if i >= 5 else 'steelblue' for i in patterns["day_of_week"]]
    bars2 = ax2.bar(patterns["day_name"], patterns["occupancy_rate"] * 100, 
                    alpha=0.7, color=colors)
    ax2.set_ylabel("Occupancy Rate (%)")
    ax2.set_title(f"{asset_id}: Occupancy Rate by Day of Week")
    ax2.tick_params(axis='x', rotation=45)
    ax2.grid(True, alpha=0.3, axis='y')
    
    plt.tight_layout()
    plt.savefig(os.path.join(output_dir, f"{asset_id}_daily_patterns.png"), dpi=150)
    plt.close()


def plot_power_histogram(df: pd.DataFrame, asset_id: str, description: str, 
                         occupancy_threshold: float, output_dir: str):
    """Plot histogram of charging power."""
    if len(df) == 0:
        return
    
    fig, axes = plt.subplots(1, 2, figsize=(12, 5))
    
    # All power values
    ax1 = axes[0]
    ax1.hist(df["power_kw"], bins=50, alpha=0.7, color='steelblue', edgecolor='black')
    ax1.axvline(x=occupancy_threshold/1000, color='r', linestyle='--', 
                label=f'Occupancy threshold ({occupancy_threshold/1000} kW)')
    ax1.set_xlabel("Power (kW)")
    ax1.set_ylabel("Frequency")
    ax1.set_title(f"{asset_id}: Power Distribution (All Slots)")
    ax1.legend()
    ax1.grid(True, alpha=0.3)
    
    # Only when charging
    charging_df = df[df["power_w"] > occupancy_threshold]
    ax2 = axes[1]
    if len(charging_df) > 0:
        ax2.hist(charging_df["power_kw"], bins=30, alpha=0.7, color='green', edgecolor='black')
        ax2.set_xlabel("Power (kW)")
        ax2.set_ylabel("Frequency")
        ax2.set_title(f"{asset_id}: Power Distribution (When Charging)")
        ax2.grid(True, alpha=0.3)
    else:
        ax2.text(0.5, 0.5, "No charging data", ha='center', va='center', transform=ax2.transAxes)
        ax2.set_title(f"{asset_id}: Power Distribution (When Charging)")
    
    plt.tight_layout()
    plt.savefig(os.path.join(output_dir, f"{asset_id}_power_histogram.png"), dpi=150)
    plt.close()


def plot_heatmap(df: pd.DataFrame, asset_id: str, description: str, output_dir: str):
    """Plot heatmap of occupancy by hour and day of week."""
    if len(df) == 0:
        return
    
    df["is_occupied"] = df["power_w"] > 100
    
    # Create pivot table: rows = 15-min slots (0-95), columns = days (0-6)
    heatmap_data = df.groupby(["slot_idx", "day_of_week"])["is_occupied"].mean().unstack()

    # Fill missing days/slots with 0
    heatmap_data = heatmap_data.reindex(
        index=range(96),
        columns=range(7),
        fill_value=0
    )

    fig, ax = plt.subplots(figsize=(10, 8))
    
    im = ax.imshow(heatmap_data.values * 100, cmap='YlOrRd', aspect='auto', 
                   vmin=0, vmax=max(50, heatmap_data.values.max() * 100))
    
    ax.set_xticks(range(7))
    ax.set_xticklabels(['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'])
    ax.set_yticks(range(0, 96, 4))
    ax.set_yticklabels([f"{h:02d}:00" for h in range(24)])
    
    ax.set_xlabel("Day of Week")
    ax.set_ylabel("Time of Day (15-min slots)")
    ax.set_title(f"{asset_id} - {description}: Occupancy Heatmap (%)")
    
    cbar = plt.colorbar(im, ax=ax, label='Occupancy Rate (%)')
    
    plt.tight_layout()
    plt.savefig(os.path.join(output_dir, f"{asset_id}_heatmap.png"), dpi=150)
    plt.close()


def plot_flexibility_heatmap(df: pd.DataFrame, asset_id: str, description: str, 
                              nominal_power_kw: float, output_dir: str,
                              occupancy_threshold: float = 100):
    """Plot heatmap of available downward flexibility probability by hour and day of week.
    
    Shows the probability of having at least 25%, 50%, 75%, 100% of the nominal 
    power available as DOWNWARD flexibility (i.e., load that can be curtailed/reduced).
    
    Downward flexibility = current charging power (what can be reduced)
    
    Note: Flexibility is only available when a car is plugged in and charging.
    When the charger is idle, there is NO flexibility (nothing to curtail).
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
    
    # Color maps: shades of blue-green to indicate flexibility availability
    cmaps = ['YlGnBu', 'YlGnBu', 'YlGnBu', 'YlGnBu']
    
    for idx, (level, level_str, power_str) in enumerate(flexibility_levels):
        ax = axes[idx]
        
        # Calculate threshold: power usage must be at least (level * nominal)
        # to have at least 'level' downward flexibility available
        min_power_kw = nominal_power_kw * level
        
        # Mark slots where charger is occupied AND has enough power to curtail
        # Flexibility = current power being used (can be reduced to 0)
        # We need power_kw >= min_power_kw to provide that level of flexibility
        df[f"flex_{level_str}"] = df["power_kw"] >= min_power_kw
        
        # Create pivot table: rows = 15-min slots (0-95), columns = days (0-6)
        heatmap_data = df.groupby(["slot_idx", "day_of_week"])[f"flex_{level_str}"].mean().unstack()

        # Fill missing days/slots with 0
        heatmap_data = heatmap_data.reindex(
            index=range(96),
            columns=range(7),
            fill_value=0
        )
        
        im = ax.imshow(heatmap_data.values * 100, cmap=cmaps[idx], aspect='auto', 
                       vmin=0, vmax=100)
        
        ax.set_xticks(range(7))
        ax.set_xticklabels(['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'])
        ax.set_yticks(range(0, 96, 4))
        ax.set_yticklabels([f"{h:02d}:00" for h in range(24)])
        
        ax.set_xlabel("Day of Week")
        ax.set_ylabel("Time of Day (15-min slots)")
        ax.set_title(f"{level_str} Downward Flexibility ({power_str})")
        
        plt.colorbar(im, ax=ax, label='Probability (%)')
    
    fig.suptitle(f"{asset_id} - {description}: Downward Flexibility Heatmap\n"
                 f"(Nominal Power: {nominal_power_kw} kW - Probability of having curtailable load)", 
                 fontsize=12, fontweight='bold')
    
    plt.tight_layout()
    plt.savefig(os.path.join(output_dir, f"{asset_id}_flexibility_heatmap.png"), dpi=150)
    plt.close()


def plot_comparison(all_data: dict, output_dir: str):
    """Plot comparison between EV chargers."""
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
    ax1.set_title("Average Charging Power by Time of Day")
    ax1.legend()
    ax1.set_xticks(range(0, 96, 8))
    ax1.set_xticklabels([f"{h:02d}:00" for h in range(0, 24, 2)])
    ax1.grid(True, alpha=0.3)
    
    # Compare occupancy by hour
    ax2 = axes[0, 1]
    for i, (asset_id, data) in enumerate(all_data.items()):
        patterns = data["hourly_patterns"]
        if len(patterns) > 0:
            ax2.plot(patterns["slot_idx"], patterns["occupancy_rate"] * 100, 
                    label=data["description"], color=colors[i], linewidth=2)
    ax2.set_xlabel("Time Slot")
    ax2.set_ylabel("Occupancy Rate (%)")
    ax2.set_title("Occupancy Probability by Time of Day")
    ax2.legend()
    ax2.set_xticks(range(0, 96, 8))
    ax2.set_xticklabels([f"{h:02d}:00" for h in range(0, 24, 2)])
    ax2.grid(True, alpha=0.3)
    
    # Compare overall statistics
    ax3 = axes[1, 0]
    stats_data = {
        "Occupancy\nRate (%)": [data["stats"]["occupancy_rate"] for data in all_data.values()],
        "Avg Power\n(kW)": [data["stats"]["power_mean_kw"] for data in all_data.values()],
        "Avg When\nCharging (kW)": [data["stats"]["power_when_charging_mean_kw"] for data in all_data.values()],
    }
    x = np.arange(len(stats_data))
    width = 0.35
    
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
    width = 0.2
    
    for i, (asset_id, data) in enumerate(all_data.items()):
        stats = data["stats"]
        offset = width * (i - len(all_data)/2 + 0.5)
        values = [stats["weekday_occupancy_rate"], stats["weekend_occupancy_rate"]]
        ax4.bar(x + offset, values, width, label=data["description"], color=colors[i])
    
    ax4.set_xticks(x)
    ax4.set_xticklabels(["Weekday", "Weekend"])
    ax4.set_ylabel("Occupancy Rate (%)")
    ax4.set_title("Weekday vs Weekend Occupancy")
    ax4.legend()
    ax4.grid(True, alpha=0.3, axis='y')
    
    plt.tight_layout()
    plt.savefig(os.path.join(output_dir, "ev_chargers_comparison.png"), dpi=150)
    plt.close()


def print_statistics(all_data: dict):
    """Print detailed statistics for all EV chargers."""
    print("\n" + "=" * 80)
    print("EV CHARGER ANALYSIS - STATISTICS")
    print("=" * 80)
    
    for asset_id, data in all_data.items():
        stats = data["stats"]
        print(f"\n{'─' * 80}")
        print(f"Asset: {asset_id} ({data['description']})")
        print(f"{'─' * 80}")
        
        print(f"\n  OVERALL STATISTICS:")
        print(f"    Total data points:         {stats['total_slots']:,}")
        print(f"    Time period:               {data['days_back']} days")
        
        print(f"\n  OCCUPANCY:")
        print(f"    Overall occupancy rate:    {stats['occupancy_rate']:.1f}%")
        print(f"    Weekday occupancy rate:    {stats['weekday_occupancy_rate']:.1f}%")
        print(f"    Weekend occupancy rate:    {stats['weekend_occupancy_rate']:.1f}%")
        print(f"    Occupied slots:            {stats['occupied_slots']:,} / {stats['total_slots']:,}")
        
        print(f"\n  POWER CONSUMPTION:")
        print(f"    Mean power (all slots):    {stats['power_mean_kw']:.2f} kW")
        print(f"    Std deviation:             {stats['power_std_kw']:.2f} kW")
        print(f"    Min power:                 {stats['power_min_kw']:.2f} kW")
        print(f"    Max power:                 {stats['power_max_kw']:.2f} kW")
        print(f"    Median power:              {stats['power_median_kw']:.2f} kW")
        
        print(f"\n  WHEN CHARGING (power > threshold):")
        print(f"    Mean power:                {stats['power_when_charging_mean_kw']:.2f} kW")
        print(f"    Max power:                 {stats['power_when_charging_max_kw']:.2f} kW")
        
        # Top occupancy hours
        patterns = data["hourly_patterns"]
        if len(patterns) > 0:
            top_hours = patterns.nlargest(5, "occupancy_rate")[["time_label", "occupancy_rate", "power_mean"]]
            print(f"\n  TOP 5 SLOTS BY OCCUPANCY:")
            for _, row in top_hours.iterrows():
                print(f"    {row['time_label']}: {row['occupancy_rate']*100:.1f}% occupancy, {row['power_mean']:.2f} kW avg")
    
    # Summary comparison
    print(f"\n{'=' * 80}")
    print("SUMMARY COMPARISON")
    print("=" * 80)
    print(f"\n{'Asset':<15} {'Occupancy':>12} {'Weekday':>10} {'Weekend':>10} {'Avg Power':>12} {'When Charging':>15}")
    print("-" * 80)
    
    for asset_id, data in all_data.items():
        stats = data["stats"]
        print(f"{asset_id:<15} {stats['occupancy_rate']:>10.1f}% {stats['weekday_occupancy_rate']:>8.1f}% "
              f"{stats['weekend_occupancy_rate']:>8.1f}% {stats['power_mean_kw']:>10.2f} kW "
              f"{stats['power_when_charging_mean_kw']:>13.2f} kW")
    
    print("=" * 80)


def main():
    parser = argparse.ArgumentParser(description="Analyze EV Charger Historical Data")
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
        default="../data/ev_analysis",
        help="Output directory for plots"
    )
    parser.add_argument(
        "--occupancy_threshold",
        type=float,
        default=100,
        help="Power threshold (W) to consider charger occupied"
    )
    parser.add_argument(
        "--nominal_power_kw",
        type=float,
        default=11.0,
        help="Nominal power of EV chargers in kW (default: 11 kW)"
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
    
    print(f"\nEV Charger Analysis")
    print(f"Time range: {start_time.strftime('%Y-%m-%d')} to {end_time.strftime('%Y-%m-%d')}")
    print(f"Occupancy threshold: {args.occupancy_threshold} W")
    print(f"Nominal power: {args.nominal_power_kw} kW")
    print(f"Output directory: {output_dir}")
    
    # Get EV chargers from config
    asset_mapping = cfg.get("asset_mapping", {})
    ev_chargers = {
        k: v for k, v in asset_mapping.items() 
        if isinstance(v, dict) and v.get("type") == "ev_charger"
    }
    
    if not ev_chargers:
        print("\nNo EV chargers found in configuration!")
        return
    
    print(f"\nFound {len(ev_chargers)} EV chargers: {list(ev_chargers.keys())}")
    
    # Analyze each EV charger
    all_data = {}
    
    for asset_id, mapping in ev_chargers.items():
        description = mapping.get("description", asset_id)
        print(f"\nLoading data for {asset_id} ({description})...")
        
        # Load data
        df = load_ev_charger_data(client, asset_id, mapping, start_time, end_time)
        
        if len(df) == 0:
            print(f"  No data found for {asset_id}")
            continue
        
        print(f"  Loaded {len(df)} data points")
        
        # Calculate statistics
        stats = calculate_statistics(df, args.occupancy_threshold)
        hourly_patterns = calculate_hourly_patterns(df, args.occupancy_threshold)
        daily_patterns = calculate_daily_patterns(df, args.occupancy_threshold)
        
        # Store for comparison
        all_data[asset_id] = {
            "description": description,
            "df": df,
            "stats": stats,
            "hourly_patterns": hourly_patterns,
            "daily_patterns": daily_patterns,
            "days_back": args.days_back,
        }
        
        # Generate plots for this charger
        print(f"  Generating plots...")
        plot_time_series(df, asset_id, description, output_dir)
        plot_hourly_patterns(hourly_patterns, asset_id, description, output_dir)
        plot_daily_patterns(daily_patterns, asset_id, description, output_dir)
        plot_power_histogram(df, asset_id, description, args.occupancy_threshold, output_dir)
        plot_heatmap(df, asset_id, description, output_dir)
        plot_flexibility_heatmap(df, asset_id, description, args.nominal_power_kw, output_dir)
    
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
    ev_charger_groups = {
        k: v for k, v in assets_grouping.items()
        if isinstance(v, dict) and v.get("type") == "ev_charger"
    }
    
    if ev_charger_groups:
        print(f"\n{'=' * 80}")
        print("GROUPED ANALYSIS")
        print(f"{'=' * 80}")
        print(f"\nFound {len(ev_charger_groups)} EV charger groups: {list(ev_charger_groups.keys())}")
        
        all_group_data = {}
        
        for group_name, group_config in ev_charger_groups.items():
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
            group_nominal_power = get_group_nominal_power(group_config, asset_mapping, 
                                                          args.nominal_power_kw)
            description = f"Group: {', '.join(asset_ids)} (Total: {group_nominal_power:.1f} kW)"
            
            # For occupancy in grouped data, consider occupied if total power > threshold * num_assets
            # This is a simplified approach - at least one charger is in use
            group_occupancy_threshold = args.occupancy_threshold
            
            # Calculate statistics
            stats = calculate_statistics(df, group_occupancy_threshold)
            hourly_patterns = calculate_hourly_patterns(df, group_occupancy_threshold)
            daily_patterns = calculate_daily_patterns(df, group_occupancy_threshold)
            
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
            plot_time_series(df, group_name, description, output_dir)
            plot_hourly_patterns(hourly_patterns, group_name, description, output_dir)
            plot_daily_patterns(daily_patterns, group_name, description, output_dir)
            plot_power_histogram(df, group_name, description, group_occupancy_threshold, output_dir)
            plot_heatmap(df, group_name, description, output_dir)
            plot_flexibility_heatmap(df, group_name, description, group_nominal_power, output_dir)
        
        # Print group statistics
        if all_group_data:
            print_statistics(all_group_data)
    
    print(f"\nPlots saved to: {output_dir}")
    print("Done!")


if __name__ == "__main__":
    main()
