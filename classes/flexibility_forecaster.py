# import section
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple


class FlexibilityForecaster:
    """
    Forecasts flexibility available from FSP assets for flexibility market bidding.
    
    Flexibility = amount of power that can be reduced from baseline on request.
    
    Configuration is loaded from main_cfg:
    - asset_mapping: defines assets with capacity_kw, flexibility_factor, etc.
    - flexibility: defines peak_hours, historical_days_back, default_flexibility_factor
    """

    def __init__(self, main_cfg: dict, influx_client, logger):
        """
        Initialize the FlexibilityForecaster.
        
        :param main_cfg: Main configuration dictionary
        :param influx_client: InfluxDB client for historical queries
        :param logger: Logger instance
        """
        self.main_cfg = main_cfg
        self.influx_client = influx_client
        self.logger = logger
        self.asset_mapping = main_cfg.get("asset_mapping", {})
        self.granularity = main_cfg.get("fm", {}).get("granularity", 15)
        
        # Load flexibility configuration
        flex_cfg = main_cfg.get("flexibility", {})
        
        # Peak hours from config
        peak_hours = flex_cfg.get("peak_hours", {})
        morning = peak_hours.get("morning", {"start": 7, "end": 10})
        evening = peak_hours.get("evening", {"start": 16, "end": 20})
        self.morning_peak = (morning.get("start", 7), morning.get("end", 10))
        self.evening_peak = (evening.get("start", 16), evening.get("end", 20))
        
        # Other flexibility settings
        self.historical_days_back = flex_cfg.get("historical_days_back", 30)
        self.default_flexibility_factor = flex_cfg.get("default_flexibility_factor", 0.50)
        
        # Build asset capacities and flexibility factors from asset_mapping
        self.asset_capacities = {}
        self.flexibility_factors = {}
        self.asset_descriptions = {}
        
        for asset_id, mapping in self.asset_mapping.items():
            if isinstance(mapping, dict):
                self.asset_capacities[asset_id] = mapping.get("capacity_kw", 0.0)
                self.flexibility_factors[asset_id] = mapping.get(
                    "flexibility_factor", self.default_flexibility_factor
                )
                self.asset_descriptions[asset_id] = mapping.get("description", asset_id)
            else:
                # Legacy format - use defaults
                self.asset_capacities[asset_id] = 0.0
                self.flexibility_factors[asset_id] = self.default_flexibility_factor
                self.asset_descriptions[asset_id] = asset_id
        
        # EV charger specific settings
        ev_cfg = flex_cfg.get("ev_charger", {})
        self.ev_occupancy_threshold_w = ev_cfg.get("occupancy_threshold_w", 100)  # Power > 100W = occupied
        
        self.logger.info(
            "FlexibilityForecaster initialized: %d assets, peak hours: %s-%s, %s-%s",
            len(self.asset_mapping),
            f"{self.morning_peak[0]:02d}:00", f"{self.morning_peak[1]:02d}:00",
            f"{self.evening_peak[0]:02d}:00", f"{self.evening_peak[1]:02d}:00"
        )
        
        # Cache for historical patterns (power consumption)
        self._historical_patterns = {}
        
        # Cache for EV charger occupancy patterns (probability of car being plugged in)
        self._ev_occupancy_patterns = {}
        
    def forecast_flexibility(
        self, 
        baseline_df: pd.DataFrame, 
        current_time: Optional[datetime] = None,
        method: str = "historical"
    ) -> pd.DataFrame:
        """
        Forecast flexibility available for each time slot.
        
        :param baseline_df: DataFrame with baseline forecast (periodFrom, periodTo, quantity)
        :param current_time: Current time (defaults to now)
        :param method: Forecasting method ('historical', 'simple', 'conservative')
        :return: DataFrame with flexibility forecast
        """
        if current_time is None:
            current_time = datetime.utcnow()
            
        self.logger.info(
            "Forecasting flexibility for %d time slots using method '%s'",
            len(baseline_df), method
        )
        
        # Build flexibility forecast
        results = []
        
        for _, row in baseline_df.iterrows():
            period_from = pd.to_datetime(row["periodFrom"])
            period_to = pd.to_datetime(row["periodTo"])
            baseline_mw = row["quantity"]
            
            if method == "historical":
                flex_mw, confidence = self._forecast_historical(
                    period_from, baseline_mw
                )
            elif method == "simple":
                flex_mw, confidence = self._forecast_simple(
                    period_from, baseline_mw
                )
            else:  # conservative
                flex_mw, confidence = self._forecast_conservative(
                    period_from, baseline_mw
                )
            
            # Check if this is a peak hour (high value flexibility)
            is_peak = self._is_peak_hour(period_from)
            
            results.append({
                "periodFrom": period_from,
                "periodTo": period_to,
                "baseline_mw": baseline_mw,
                "flexibility_mw": flex_mw,
                "flexibility_kw": flex_mw * 1000,
                "flex_percentage": (flex_mw / baseline_mw * 100) if baseline_mw > 0 else 0,
                "confidence": confidence,
                "is_peak_hour": is_peak,
            })
        
        df_result = pd.DataFrame(results)
        
        # Log summary
        self._log_forecast_summary(df_result)
        
        return df_result
    
    def _forecast_historical(
        self, 
        period_from: datetime, 
        baseline_mw: float
    ) -> Tuple[float, str]:
        """
        Forecast flexibility based on historical patterns.
        
        Analyzes what assets were typically running at this 15-minute slot
        and estimates how much can be curtailed.
        """
        # Calculate 15-minute slot index: hour * 4 + minute // 15
        slot_idx = period_from.hour * 4 + period_from.minute // 15
        day_of_week = period_from.weekday()  # 0=Monday, 6=Sunday
        
        # Load historical patterns if not cached
        if not self._historical_patterns:
            self._load_historical_patterns()
        
        # Estimate per-asset flexibility
        total_flex_mw = 0.0
        confidence_scores = []
        
        for asset_id, mapping in self.asset_mapping.items():
            asset_pattern = self._historical_patterns.get(asset_id, {})
            
            # Get typical load for this 15-min slot and day type
            is_weekend = day_of_week >= 5
            day_type = "weekend" if is_weekend else "weekday"
            
            typical_load_w = asset_pattern.get(day_type, {}).get(slot_idx, 0)
            
            # Apply flexibility factor
            flex_factor = self.flexibility_factors.get(asset_id, self.default_flexibility_factor)
            asset_flex_w = typical_load_w * flex_factor
            
            # Confidence based on data availability
            data_points = asset_pattern.get("data_points", {}).get(slot_idx, 0)
            if data_points > 20:
                confidence_scores.append(0.9)
            elif data_points > 10:
                confidence_scores.append(0.7)
            elif data_points > 5:
                confidence_scores.append(0.5)
            else:
                confidence_scores.append(0.3)
            
            total_flex_mw += asset_flex_w / 1e6  # Convert W to MW
        
        # Cap flexibility at baseline (can't reduce more than what's running)
        total_flex_mw = min(total_flex_mw, baseline_mw * 0.9)
        
        # Overall confidence
        avg_confidence = np.mean(confidence_scores) if confidence_scores else 0.3
        if avg_confidence > 0.7:
            confidence = "high"
        elif avg_confidence > 0.5:
            confidence = "medium"
        else:
            confidence = "low"
        
        return total_flex_mw, confidence
    
    def _forecast_simple(
        self, 
        period_from: datetime, 
        baseline_mw: float
    ) -> Tuple[float, str]:
        """
        Simple flexibility forecast: percentage of baseline.
        
        Uses a fixed percentage based on time of day.
        """
        hour = period_from.hour
        
        # During peak hours, assume higher baseline = more flexibility
        if self._is_peak_hour(period_from):
            # Peak hours: 60% of baseline can be flexed
            flex_percentage = 0.60
            confidence = "medium"
        elif 6 <= hour < 22:
            # Daytime: 50% flexibility
            flex_percentage = 0.50
            confidence = "medium"
        else:
            # Night: lower baseline, lower flexibility
            flex_percentage = 0.30
            confidence = "low"
        
        flex_mw = baseline_mw * flex_percentage
        
        return flex_mw, confidence
    
    def _forecast_conservative(
        self, 
        period_from: datetime, 
        baseline_mw: float
    ) -> Tuple[float, str]:
        """
        Conservative flexibility forecast.
        
        Only bid what we're very confident we can deliver.
        """
        # Conservative: only 40% of baseline, capped at nominal capacity
        max_nominal_flex_kw = sum(
            cap * self.flexibility_factors.get(asset_id, self.default_flexibility_factor) 
            for asset_id, cap in self.asset_capacities.items()
        )
        max_nominal_flex_mw = max_nominal_flex_kw / 1000
        
        # Take minimum of 40% baseline and nominal capacity
        flex_mw = min(baseline_mw * 0.40, max_nominal_flex_mw)
        
        return flex_mw, "high"  # Conservative = high confidence
    
    def _load_historical_patterns(self, days_back: int = None):
        """
        Load historical consumption patterns from InfluxDB.
        
        Builds a profile of typical consumption per asset, per 15-minute slot, 
        for weekdays vs weekends. Uses 96 slots per day (24h * 4 slots/h).
        """
        if days_back is None:
            days_back = self.historical_days_back
        self.logger.info("Loading historical patterns from last %d days (15-min aggregation)", days_back)
        
        end_time = datetime.utcnow()
        start_time = end_time - timedelta(days=days_back)
        
        # 96 slots per day (15-min granularity)
        slots_per_day = 96
        
        for asset_id, mapping in self.asset_mapping.items():
            if isinstance(mapping, dict):
                device_name = mapping.get("device_name_tag")
                field = mapping.get("field", "active_power")
            else:
                device_name = mapping
                field = "active_power"
            
            site = asset_id.split(".")[0]
            
            # Use 15-minute aggregation
            query = (
                f"SELECT MEAN({field}) as mean_power FROM assets_data "
                f"WHERE time >= '{start_time.strftime('%Y-%m-%dT%H:%M:%SZ')}' "
                f"AND time < '{end_time.strftime('%Y-%m-%dT%H:%M:%SZ')}' "
                f"AND site='{site}' AND device_name='{device_name}' "
                f"GROUP BY time(15m)"
            )
            
            try:
                res = self.influx_client.query(query)
                
                # 96 slots per day: slot 0 = 00:00, slot 1 = 00:15, ..., slot 95 = 23:45
                weekday_slots = {s: [] for s in range(slots_per_day)}
                weekend_slots = {s: [] for s in range(slots_per_day)}
                
                # Handle InfluxDB query result
                for series in res.raw.get("series", []):
                    columns = series.get("columns", [])
                    values = series.get("values", [])
                    
                    # Find the index of the mean column
                    mean_idx = columns.index("mean") if "mean" in columns else 1
                    time_idx = columns.index("time") if "time" in columns else 0
                    
                    for row in values:
                        timestamp = pd.to_datetime(row[time_idx])
                        value = row[mean_idx]
                        
                        # Calculate slot index: hour * 4 + minute // 15
                        slot_idx = timestamp.hour * 4 + timestamp.minute // 15
                        
                        if value is not None and value > 0:
                            if timestamp.weekday() >= 5:
                                weekend_slots[slot_idx].append(value)
                            else:
                                weekday_slots[slot_idx].append(value)
                
                # Calculate averages per slot
                self._historical_patterns[asset_id] = {
                    "weekday": {
                        s: np.mean(vals) if vals else 0 
                        for s, vals in weekday_slots.items()
                    },
                    "weekend": {
                        s: np.mean(vals) if vals else 0 
                        for s, vals in weekend_slots.items()
                    },
                    "data_points": {
                        s: len(weekday_slots[s]) + len(weekend_slots[s])
                        for s in range(slots_per_day)
                    }
                }
                
                total_points = sum(len(weekday_slots[s]) + len(weekend_slots[s]) for s in range(slots_per_day))
                self.logger.info(
                    "Loaded patterns for %s: %d data points",
                    asset_id, total_points
                )
                
            except Exception as e:
                self.logger.error("Error loading patterns for %s: %s", asset_id, str(e))
                self._historical_patterns[asset_id] = {
                    "weekday": {s: 0 for s in range(slots_per_day)},
                    "weekend": {s: 0 for s in range(slots_per_day)},
                    "data_points": {s: 0 for s in range(slots_per_day)}
                }
    
    def _load_ev_occupancy_patterns(self, days_back: int = None):
        """
        Load EV charger occupancy patterns from InfluxDB.
        
        Occupancy = probability that a car is plugged in and charging at a given time slot.
        Calculated as: (number of slots with power > threshold) / (total slots)
        
        This helps estimate flexibility more accurately:
        - If occupancy is 0%, no car is expected → no flexibility available
        - If occupancy is 100%, car always present → full flexibility available
        """
        if days_back is None:
            days_back = self.historical_days_back
        
        self.logger.info("Loading EV occupancy patterns from last %d days", days_back)
        
        end_time = datetime.utcnow()
        start_time = end_time - timedelta(days=days_back)
        
        slots_per_day = 96  # 15-min granularity
        
        for asset_id, mapping in self.asset_mapping.items():
            # Only process EV chargers
            if not isinstance(mapping, dict):
                continue
            asset_type = mapping.get("type", "")
            if asset_type != "ev_charger":
                continue
            
            device_name = mapping.get("device_name_tag")
            field = mapping.get("field", "power")
            site = asset_id.split(".")[0]
            
            # Query to get all 15-min average power values
            query = (
                f"SELECT MEAN({field}) as mean_power FROM assets_data "
                f"WHERE time >= '{start_time.strftime('%Y-%m-%dT%H:%M:%SZ')}' "
                f"AND time < '{end_time.strftime('%Y-%m-%dT%H:%M:%SZ')}' "
                f"AND site='{site}' AND device_name='{device_name}' "
                f"GROUP BY time(15m)"
            )
            
            try:
                res = self.influx_client.query(query)
                
                # Track occupied vs total slots
                weekday_occupied = {s: 0 for s in range(slots_per_day)}
                weekday_total = {s: 0 for s in range(slots_per_day)}
                weekend_occupied = {s: 0 for s in range(slots_per_day)}
                weekend_total = {s: 0 for s in range(slots_per_day)}
                
                for series in res.raw.get("series", []):
                    columns = series.get("columns", [])
                    values = series.get("values", [])
                    
                    mean_idx = columns.index("mean") if "mean" in columns else 1
                    time_idx = columns.index("time") if "time" in columns else 0
                    
                    for row in values:
                        timestamp = pd.to_datetime(row[time_idx])
                        value = row[mean_idx]
                        slot_idx = timestamp.hour * 4 + timestamp.minute // 15
                        
                        if timestamp.weekday() >= 5:
                            weekend_total[slot_idx] += 1
                            if value is not None and value > self.ev_occupancy_threshold_w:
                                weekend_occupied[slot_idx] += 1
                        else:
                            weekday_total[slot_idx] += 1
                            if value is not None and value > self.ev_occupancy_threshold_w:
                                weekday_occupied[slot_idx] += 1
                
                # Calculate occupancy probability per slot
                self._ev_occupancy_patterns[asset_id] = {
                    "weekday": {
                        s: (weekday_occupied[s] / weekday_total[s]) if weekday_total[s] > 0 else 0
                        for s in range(slots_per_day)
                    },
                    "weekend": {
                        s: (weekend_occupied[s] / weekend_total[s]) if weekend_total[s] > 0 else 0
                        for s in range(slots_per_day)
                    },
                    "data_points": {
                        s: weekday_total[s] + weekend_total[s]
                        for s in range(slots_per_day)
                    }
                }
                
                # Calculate average occupancy for logging
                avg_weekday_occ = np.mean(list(self._ev_occupancy_patterns[asset_id]["weekday"].values()))
                avg_weekend_occ = np.mean(list(self._ev_occupancy_patterns[asset_id]["weekend"].values()))
                
                self.logger.info(
                    "Loaded EV occupancy for %s: avg occupancy weekday=%.1f%%, weekend=%.1f%%",
                    asset_id, avg_weekday_occ * 100, avg_weekend_occ * 100
                )
                
            except Exception as e:
                self.logger.error("Error loading EV occupancy for %s: %s", asset_id, str(e))
                self._ev_occupancy_patterns[asset_id] = {
                    "weekday": {s: 0 for s in range(slots_per_day)},
                    "weekend": {s: 0 for s in range(slots_per_day)},
                    "data_points": {s: 0 for s in range(slots_per_day)}
                }
    
    def _is_peak_hour(self, dt: datetime) -> bool:
        """Check if the given time is during peak demand hours."""
        hour = dt.hour
        return (
            self.morning_peak[0] <= hour < self.morning_peak[1] or
            self.evening_peak[0] <= hour < self.evening_peak[1]
        )
    
    def _log_forecast_summary(self, df: pd.DataFrame):
        """Log summary statistics of the flexibility forecast."""
        total_slots = len(df)
        peak_slots = df["is_peak_hour"].sum()
        
        self.logger.info("=" * 70)
        self.logger.info("FLEXIBILITY FORECAST SUMMARY")
        self.logger.info("=" * 70)
        self.logger.info("Total time slots: %d (%.1f hours)", total_slots, total_slots * 0.25)
        self.logger.info("Peak hour slots: %d", peak_slots)
        self.logger.info("-" * 70)
        self.logger.info(
            "Baseline  - avg: %.4f MW, min: %.4f MW, max: %.4f MW",
            df["baseline_mw"].mean(),
            df["baseline_mw"].min(),
            df["baseline_mw"].max()
        )
        self.logger.info(
            "Flexibility - avg: %.4f MW, min: %.4f MW, max: %.4f MW",
            df["flexibility_mw"].mean(),
            df["flexibility_mw"].min(),
            df["flexibility_mw"].max()
        )
        self.logger.info(
            "Flex %% of baseline - avg: %.1f%%, min: %.1f%%, max: %.1f%%",
            df["flex_percentage"].mean(),
            df["flex_percentage"].min(),
            df["flex_percentage"].max()
        )
        
        # Peak hours summary
        if peak_slots > 0:
            peak_df = df[df["is_peak_hour"]]
            self.logger.info("-" * 70)
            self.logger.info("PEAK HOURS (07-10, 16-20):")
            self.logger.info(
                "  Flexibility - avg: %.4f MW (%.1f kW)",
                peak_df["flexibility_mw"].mean(),
                peak_df["flexibility_kw"].mean()
            )
        
        # Confidence breakdown
        confidence_counts = df["confidence"].value_counts()
        self.logger.info("-" * 70)
        self.logger.info("Confidence: %s", dict(confidence_counts))
        self.logger.info("=" * 70)
    
    def get_asset_flexibility_breakdown(
        self, 
        period_from: datetime
    ) -> Dict[str, Dict]:
        """
        Get detailed flexibility breakdown per asset for a specific 15-minute slot.
        
        For EV chargers, flexibility is weighted by occupancy probability:
        - available_flexibility = typical_load * flex_factor * occupancy_probability
        
        :param period_from: Start of time slot
        :return: Dictionary with per-asset flexibility info
        """
        # Calculate 15-minute slot index: hour * 4 + minute // 15
        slot_idx = period_from.hour * 4 + period_from.minute // 15
        is_weekend = period_from.weekday() >= 5
        day_type = "weekend" if is_weekend else "weekday"
        
        if not self._historical_patterns:
            self._load_historical_patterns()
        
        # Load EV occupancy patterns if not cached
        if not self._ev_occupancy_patterns:
            self._load_ev_occupancy_patterns()
        
        breakdown = {}
        
        for asset_id in self.asset_capacities.keys():
            mapping = self.asset_mapping.get(asset_id, {})
            asset_type = mapping.get("type", "") if isinstance(mapping, dict) else ""
            
            asset_pattern = self._historical_patterns.get(asset_id, {})
            # Historical data is in W, convert to kW
            typical_load_w = asset_pattern.get(day_type, {}).get(slot_idx, 0)
            typical_load_kw = typical_load_w / 1000
            flex_factor = self.flexibility_factors.get(asset_id, self.default_flexibility_factor)
            nominal_kw = self.asset_capacities.get(asset_id, 0)
            
            # For EV chargers, factor in occupancy probability
            if asset_type == "ev_charger":
                occupancy_pattern = self._ev_occupancy_patterns.get(asset_id, {})
                occupancy_prob = occupancy_pattern.get(day_type, {}).get(slot_idx, 0)
                
                # Available flexibility = load * flex_factor * occupancy_probability
                # If no car is expected (occupancy=0), flexibility is 0
                available_flex_kw = typical_load_kw * flex_factor * occupancy_prob
                
                breakdown[asset_id] = {
                    "description": self.asset_descriptions.get(asset_id, asset_id),
                    "asset_type": asset_type,
                    "nominal_capacity_kw": nominal_kw,
                    "typical_load_kw": typical_load_kw,
                    "flexibility_factor": flex_factor,
                    "occupancy_probability": occupancy_prob,
                    "available_flexibility_kw": available_flex_kw,
                    "max_flexibility_kw": nominal_kw * flex_factor,
                }
            else:
                # Heat pumps and other assets: no occupancy factor
                breakdown[asset_id] = {
                    "description": self.asset_descriptions.get(asset_id, asset_id),
                    "asset_type": asset_type,
                    "nominal_capacity_kw": nominal_kw,
                    "typical_load_kw": typical_load_kw,
                    "flexibility_factor": flex_factor,
                    "occupancy_probability": None,  # Not applicable
                    "available_flexibility_kw": typical_load_kw * flex_factor,
                    "max_flexibility_kw": nominal_kw * flex_factor,
                }
        
        return breakdown
    
    def print_asset_summary(self):
        """Print a summary of configured assets and their flexibility potential."""
        print("\n" + "=" * 70)
        print("ASSET FLEXIBILITY SUMMARY")
        print("=" * 70)
        print(f"{'Asset':<12} {'Description':<25} {'Capacity':<12} {'Max Flex':<12}")
        print("-" * 70)
        
        total_capacity = 0
        total_flex = 0
        
        for asset_id, capacity in self.asset_capacities.items():
            flex_factor = self.flexibility_factors.get(asset_id, self.default_flexibility_factor)
            max_flex = capacity * flex_factor
            description = self.asset_descriptions.get(asset_id, asset_id)
            
            print(f"{asset_id:<12} {description:<25} {capacity:>8.1f} kW  {max_flex:>8.1f} kW")
            
            total_capacity += capacity
            total_flex += max_flex
        
        print("-" * 70)
        print(f"{'TOTAL':<12} {'':<25} {total_capacity:>8.1f} kW  {total_flex:>8.1f} kW")
        print(f"{'TOTAL (MW)':<12} {'':<25} {total_capacity/1000:>8.3f} MW  {total_flex/1000:>8.3f} MW")
        print("=" * 70)
        print(f"\nPeak demand windows: Morning {self.morning_peak[0]:02d}:00-{self.morning_peak[1]:02d}:00, "
              f"Evening {self.evening_peak[0]:02d}:00-{self.evening_peak[1]:02d}:00")
        print()

