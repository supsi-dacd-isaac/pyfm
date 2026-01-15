# import section
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
import json
import os


class FlexibilityForecaster:
    """
    Forecasts flexibility available from FSP assets for flexibility market bidding.
    
    Flexibility = amount of power that can be reduced from baseline on request.
    
    Features:
    - Time-based patterns (15-min slots, weekday/weekend)
    - Temperature-aware analysis for heat pumps
    - Occupancy probability for EV chargers
    
    Configuration is loaded from main_cfg:
    - asset_mapping: defines assets with capacity_kw, flexibility_factor, etc.
    - flexibility: defines peak_hours, historical_days_back, default_flexibility_factor
    - temperature: defines temperature data source and forecast settings
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
        
        # Temperature configuration
        temp_cfg = flex_cfg.get("temperature", {})
        self.temperature_enabled = temp_cfg.get("enabled", False)
        self.temperature_source = temp_cfg.get("source", {})
        self.temperature_bins = temp_cfg.get("bins", [-5, 0, 5, 10, 15, 20, 25])
        self.forecast_source = temp_cfg.get("forecast", {})
        
        # Build asset capacities and flexibility factors from asset_mapping
        self.asset_capacities = {}
        self.flexibility_factors = {}
        self.asset_descriptions = {}
        self.asset_types = {}
        
        for asset_id, mapping in self.asset_mapping.items():
            if isinstance(mapping, dict):
                self.asset_capacities[asset_id] = mapping.get("capacity_kw", 0.0)
                self.flexibility_factors[asset_id] = mapping.get(
                    "flexibility_factor", self.default_flexibility_factor
                )
                self.asset_descriptions[asset_id] = mapping.get("description", asset_id)
                self.asset_types[asset_id] = mapping.get("type", "unknown")
            else:
                # Legacy format - use defaults
                self.asset_capacities[asset_id] = 0.0
                self.flexibility_factors[asset_id] = self.default_flexibility_factor
                self.asset_descriptions[asset_id] = asset_id
                self.asset_types[asset_id] = "unknown"
        
        # EV charger specific settings
        ev_cfg = flex_cfg.get("ev_charger", {})
        self.ev_occupancy_threshold_w = ev_cfg.get("occupancy_threshold_w", 100)
        
        self.logger.info(
            "FlexibilityForecaster initialized: %d assets, peak hours: %s-%s, %s-%s",
            len(self.asset_mapping),
            f"{self.morning_peak[0]:02d}:00", f"{self.morning_peak[1]:02d}:00",
            f"{self.evening_peak[0]:02d}:00", f"{self.evening_peak[1]:02d}:00"
        )
        
        if self.temperature_enabled:
            self.logger.info("Temperature-aware HP analysis: ENABLED")
        
        # Cache for historical patterns (power consumption)
        self._historical_patterns = {}
        
        # Cache for EV charger occupancy patterns
        self._ev_occupancy_patterns = {}
        
        # Cache for temperature-power relationship (HP assets)
        self._hp_temperature_profiles = {}
        
        # Cache for historical temperature data
        self._temperature_history = {}
        
        # Cache for temperature forecast
        self._temperature_forecast = {}
    
    # =========================================================================
    # TEMPERATURE DATA LOADING
    # =========================================================================
    
    def _load_temperature_history(self, days_back: int = None):
        """
        Load historical temperature data from InfluxDB.
        
        Temperature data is used to correlate HP consumption with external temperature.
        """
        if not self.temperature_enabled:
            return
        
        if days_back is None:
            days_back = self.historical_days_back
        
        self.logger.info("Loading temperature history from last %d days", days_back)
        
        end_time = datetime.utcnow()
        start_time = end_time - timedelta(days=days_back)
        
        # Get temperature source config
        site = self.temperature_source.get("site", "ECM")
        device = self.temperature_source.get("device", "weather_station")
        field = self.temperature_source.get("field", "temperature")
        
        # Query temperature data with 15-min aggregation
        query = (
            f"SELECT MEAN({field}) as mean_temp FROM assets_data "
            f"WHERE time >= '{start_time.strftime('%Y-%m-%dT%H:%M:%SZ')}' "
            f"AND time < '{end_time.strftime('%Y-%m-%dT%H:%M:%SZ')}' "
            f"AND site='{site}' AND device_name='{device}' "
            f"GROUP BY time(15m)"
        )
        
        try:
            res = self.influx_client.query(query)
            
            temp_data = []
            for series in res.raw.get("series", []):
                columns = series.get("columns", [])
                values = series.get("values", [])
                
                mean_idx = columns.index("mean") if "mean" in columns else 1
                time_idx = columns.index("time") if "time" in columns else 0
                
                for row in values:
                    timestamp = pd.to_datetime(row[time_idx])
                    value = row[mean_idx]
                    if value is not None:
                        temp_data.append({
                            "timestamp": timestamp,
                            "temperature": value,
                            "slot_idx": timestamp.hour * 4 + timestamp.minute // 15,
                            "is_weekend": timestamp.weekday() >= 5
                        })
            
            self._temperature_history = pd.DataFrame(temp_data)
            
            if len(self._temperature_history) > 0:
                avg_temp = self._temperature_history["temperature"].mean()
                min_temp = self._temperature_history["temperature"].min()
                max_temp = self._temperature_history["temperature"].max()
                self.logger.info(
                    "Loaded %d temperature records: avg=%.1f°C, min=%.1f°C, max=%.1f°C",
                    len(self._temperature_history), avg_temp, min_temp, max_temp
                )
            else:
                self.logger.warning("No temperature data found")
                self._temperature_history = pd.DataFrame()
                
        except Exception as e:
            self.logger.error("Error loading temperature history: %s", str(e))
            self._temperature_history = pd.DataFrame()
    
    def _load_temperature_forecast(self, hours_ahead: int = 24):
        """
        Load temperature forecast for the next hours.
        
        Supports multiple sources:
        - file: Load from JSON file
        - api: Fetch from weather API (not implemented)
        - constant: Use a constant value (for testing)
        """
        if not self.temperature_enabled:
            return
        
        source_type = self.forecast_source.get("type", "constant")
        
        if source_type == "file":
            forecast_file = self.forecast_source.get("file", "")
            if os.path.isfile(forecast_file):
                try:
                    with open(forecast_file, "r") as f:
                        forecast_data = json.load(f)
                    
                    self._temperature_forecast = {}
                    for entry in forecast_data.get("forecast", []):
                        ts = pd.to_datetime(entry["timestamp"])
                        self._temperature_forecast[ts] = entry["temperature"]
                    
                    self.logger.info(
                        "Loaded temperature forecast from %s: %d entries",
                        forecast_file, len(self._temperature_forecast)
                    )
                except Exception as e:
                    self.logger.error("Error loading forecast file: %s", str(e))
            else:
                self.logger.warning("Forecast file not found: %s", forecast_file)
        
        elif source_type == "constant":
            # Use constant temperature for all slots (useful for testing)
            constant_temp = self.forecast_source.get("value", 10.0)
            self.logger.info("Using constant temperature forecast: %.1f°C", constant_temp)
            
            now = datetime.utcnow()
            for h in range(hours_ahead * 4):  # 15-min slots
                slot_time = now + timedelta(minutes=15 * h)
                slot_time = slot_time.replace(
                    minute=(slot_time.minute // 15) * 15, 
                    second=0, 
                    microsecond=0
                )
                self._temperature_forecast[slot_time] = constant_temp
        
        elif source_type == "historical_avg":
            # Use historical average for same time/day (simple persistence)
            if len(self._temperature_history) == 0:
                self._load_temperature_history()
            
            self.logger.info("Using historical average for temperature forecast")
            now = datetime.utcnow()
            for h in range(hours_ahead * 4):
                slot_time = now + timedelta(minutes=15 * h)
                slot_time = slot_time.replace(
                    minute=(slot_time.minute // 15) * 15,
                    second=0,
                    microsecond=0
                )
                slot_idx = slot_time.hour * 4 + slot_time.minute // 15
                is_weekend = slot_time.weekday() >= 5
                
                # Get average temperature for this slot
                if len(self._temperature_history) > 0:
                    mask = (
                        (self._temperature_history["slot_idx"] == slot_idx) &
                        (self._temperature_history["is_weekend"] == is_weekend)
                    )
                    matching = self._temperature_history[mask]
                    if len(matching) > 0:
                        self._temperature_forecast[slot_time] = matching["temperature"].mean()
                    else:
                        self._temperature_forecast[slot_time] = 10.0  # Default
                else:
                    self._temperature_forecast[slot_time] = 10.0
    
    def get_forecast_temperature(self, dt: datetime) -> Optional[float]:
        """
        Get the forecast temperature for a specific datetime.
        
        :param dt: Datetime to get forecast for
        :return: Temperature in Celsius or None if not available
        """
        if not self.temperature_enabled:
            return None
        
        if not self._temperature_forecast:
            self._load_temperature_forecast()
        
        # Round to 15-min slot
        slot_time = dt.replace(
            minute=(dt.minute // 15) * 15,
            second=0,
            microsecond=0
        )
        
        # Try exact match first
        if slot_time in self._temperature_forecast:
            return self._temperature_forecast[slot_time]
        
        # Find closest forecast
        if self._temperature_forecast:
            closest = min(
                self._temperature_forecast.keys(),
                key=lambda x: abs((x - slot_time).total_seconds())
            )
            if abs((closest - slot_time).total_seconds()) < 3600:  # Within 1 hour
                return self._temperature_forecast[closest]
        
        return None
    
    # =========================================================================
    # HP TEMPERATURE PROFILE BUILDING
    # =========================================================================
    
    def _build_hp_temperature_profiles(self, days_back: int = None):
        """
        Build temperature-dependent consumption profiles for heat pumps.
        
        Creates a model of HP consumption as a function of:
        - External temperature (binned)
        - Time of day (15-min slots)
        - Day type (weekday/weekend)
        
        This allows more accurate flexibility estimation based on expected temperature.
        """
        if not self.temperature_enabled:
            return
        
        if days_back is None:
            days_back = self.historical_days_back
        
        self.logger.info("Building HP temperature profiles from last %d days", days_back)
        
        # Ensure we have temperature history
        if len(self._temperature_history) == 0:
            self._load_temperature_history(days_back)
        
        if len(self._temperature_history) == 0:
            self.logger.warning("No temperature data available for HP profiles")
            return
        
        end_time = datetime.utcnow()
        start_time = end_time - timedelta(days=days_back)
        
        # Process each HP asset
        for asset_id, mapping in self.asset_mapping.items():
            if not isinstance(mapping, dict):
                continue
            if mapping.get("type") != "heat_pump":
                continue
            
            device_name = mapping.get("device_name_tag")
            field = mapping.get("field", "active_power")
            site = asset_id.split(".")[0]
            
            # Query HP power data
            query = (
                f"SELECT MEAN({field}) as mean_power FROM assets_data "
                f"WHERE time >= '{start_time.strftime('%Y-%m-%dT%H:%M:%SZ')}' "
                f"AND time < '{end_time.strftime('%Y-%m-%dT%H:%M:%SZ')}' "
                f"AND site='{site}' AND device_name='{device_name}' "
                f"GROUP BY time(15m)"
            )
            
            try:
                res = self.influx_client.query(query)
                
                power_data = []
                for series in res.raw.get("series", []):
                    columns = series.get("columns", [])
                    values = series.get("values", [])
                    
                    mean_idx = columns.index("mean") if "mean" in columns else 1
                    time_idx = columns.index("time") if "time" in columns else 0
                    
                    for row in values:
                        timestamp = pd.to_datetime(row[time_idx])
                        value = row[mean_idx]
                        if value is not None and value > 0:
                            power_data.append({
                                "timestamp": timestamp,
                                "power_w": value,
                                "slot_idx": timestamp.hour * 4 + timestamp.minute // 15,
                                "is_weekend": timestamp.weekday() >= 5
                            })
                
                if not power_data:
                    self.logger.warning("No power data for %s", asset_id)
                    continue
                
                power_df = pd.DataFrame(power_data)
                
                # Merge with temperature data
                # Round timestamps to match
                power_df["ts_rounded"] = power_df["timestamp"].dt.floor("15min")
                temp_df = self._temperature_history.copy()
                temp_df["ts_rounded"] = temp_df["timestamp"].dt.floor("15min")
                
                merged = pd.merge(
                    power_df, 
                    temp_df[["ts_rounded", "temperature"]], 
                    on="ts_rounded", 
                    how="inner"
                )
                
                if len(merged) == 0:
                    self.logger.warning(
                        "No matching temperature data for %s power records", 
                        asset_id
                    )
                    continue
                
                # Bin temperatures
                merged["temp_bin"] = pd.cut(
                    merged["temperature"],
                    bins=[-100] + self.temperature_bins + [100],
                    labels=self._get_temp_bin_labels()
                )
                
                # Build profile: average power for each (temp_bin, slot_idx, day_type)
                profile = {}
                
                for day_type in ["weekday", "weekend"]:
                    is_weekend = (day_type == "weekend")
                    day_data = merged[merged["is_weekend"] == is_weekend]
                    
                    profile[day_type] = {}
                    for slot_idx in range(96):
                        slot_data = day_data[day_data["slot_idx"] == slot_idx]
                        
                        profile[day_type][slot_idx] = {}
                        for temp_bin in merged["temp_bin"].unique():
                            bin_data = slot_data[slot_data["temp_bin"] == temp_bin]
                            if len(bin_data) > 0:
                                profile[day_type][slot_idx][str(temp_bin)] = {
                                    "mean_power_w": bin_data["power_w"].mean(),
                                    "std_power_w": bin_data["power_w"].std(),
                                    "count": len(bin_data)
                                }
                
                # Also compute overall temperature correlation
                correlation = merged["power_w"].corr(merged["temperature"])
                
                self._hp_temperature_profiles[asset_id] = {
                    "profile": profile,
                    "correlation": correlation,
                    "data_points": len(merged),
                    "temp_range": (merged["temperature"].min(), merged["temperature"].max())
                }
                
                self.logger.info(
                    "Built temperature profile for %s: %d data points, correlation=%.3f, temp range=%.1f-%.1f°C",
                    asset_id, len(merged), correlation,
                    merged["temperature"].min(), merged["temperature"].max()
                )
                
            except Exception as e:
                self.logger.error("Error building temperature profile for %s: %s", asset_id, str(e))
    
    def _get_temp_bin_labels(self) -> List[str]:
        """Generate labels for temperature bins."""
        labels = []
        bins = [-100] + self.temperature_bins + [100]
        for i in range(len(bins) - 1):
            if bins[i] == -100:
                labels.append(f"<{bins[i+1]}°C")
            elif bins[i+1] == 100:
                labels.append(f">{bins[i]}°C")
            else:
                labels.append(f"{bins[i]}-{bins[i+1]}°C")
        return labels
    
    def _get_temp_bin(self, temperature: float) -> str:
        """Get the temperature bin label for a given temperature."""
        bins = self.temperature_bins
        if temperature < bins[0]:
            return f"<{bins[0]}°C"
        for i in range(len(bins) - 1):
            if bins[i] <= temperature < bins[i+1]:
                return f"{bins[i]}-{bins[i+1]}°C"
        return f">{bins[-1]}°C"
    
    def get_hp_expected_power(
        self, 
        asset_id: str, 
        slot_idx: int, 
        is_weekend: bool,
        temperature: float
    ) -> Tuple[float, float, str]:
        """
        Get expected HP power consumption for a given temperature and time slot.
        
        :param asset_id: Heat pump asset ID
        :param slot_idx: 15-minute slot index (0-95)
        :param is_weekend: True if weekend
        :param temperature: Expected external temperature in Celsius
        :return: Tuple of (expected_power_w, confidence, source)
        """
        if not self.temperature_enabled or asset_id not in self._hp_temperature_profiles:
            return None, None, "no_profile"
        
        profile = self._hp_temperature_profiles[asset_id]
        day_type = "weekend" if is_weekend else "weekday"
        temp_bin = self._get_temp_bin(temperature)
        
        # Try to get exact match
        slot_profile = profile.get("profile", {}).get(day_type, {}).get(slot_idx, {})
        bin_data = slot_profile.get(temp_bin)
        
        if bin_data and bin_data.get("count", 0) >= 3:
            return (
                bin_data["mean_power_w"],
                "high" if bin_data["count"] >= 10 else "medium",
                f"temp_profile:{temp_bin}"
            )
        
        # Fallback: interpolate from adjacent bins or use overall average
        all_bin_powers = []
        for tb, data in slot_profile.items():
            if data.get("count", 0) > 0:
                all_bin_powers.append(data["mean_power_w"])
        
        if all_bin_powers:
            # Use average of all bins for this slot
            return np.mean(all_bin_powers), "low", "temp_interpolated"
        
        return None, None, "no_data"
    
    # =========================================================================
    # ORIGINAL METHODS (Enhanced)
    # =========================================================================
    
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
        slot_idx = period_from.hour * 4 + period_from.minute // 15
        day_of_week = period_from.weekday()
        
        if not self._historical_patterns:
            self._load_historical_patterns()
        
        total_flex_mw = 0.0
        confidence_scores = []
        
        for asset_id, mapping in self.asset_mapping.items():
            asset_pattern = self._historical_patterns.get(asset_id, {})
            
            is_weekend = day_of_week >= 5
            day_type = "weekend" if is_weekend else "weekday"
            
            typical_load_w = asset_pattern.get(day_type, {}).get(slot_idx, 0)
            
            flex_factor = self.flexibility_factors.get(asset_id, self.default_flexibility_factor)
            asset_flex_w = typical_load_w * flex_factor
            
            data_points = asset_pattern.get("data_points", {}).get(slot_idx, 0)
            if data_points > 20:
                confidence_scores.append(0.9)
            elif data_points > 10:
                confidence_scores.append(0.7)
            elif data_points > 5:
                confidence_scores.append(0.5)
            else:
                confidence_scores.append(0.3)
            
            total_flex_mw += asset_flex_w / 1e6
        
        total_flex_mw = min(total_flex_mw, baseline_mw * 0.9)
        
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
        """Simple flexibility forecast: percentage of baseline."""
        hour = period_from.hour
        
        if self._is_peak_hour(period_from):
            flex_percentage = 0.60
            confidence = "medium"
        elif 6 <= hour < 22:
            flex_percentage = 0.50
            confidence = "medium"
        else:
            flex_percentage = 0.30
            confidence = "low"
        
        flex_mw = baseline_mw * flex_percentage
        return flex_mw, confidence
    
    def _forecast_conservative(
        self, 
        period_from: datetime, 
        baseline_mw: float
    ) -> Tuple[float, str]:
        """Conservative flexibility forecast."""
        max_nominal_flex_kw = sum(
            cap * self.flexibility_factors.get(asset_id, self.default_flexibility_factor) 
            for asset_id, cap in self.asset_capacities.items()
        )
        max_nominal_flex_mw = max_nominal_flex_kw / 1000
        flex_mw = min(baseline_mw * 0.40, max_nominal_flex_mw)
        return flex_mw, "high"
    
    def _load_historical_patterns(self, days_back: int = None):
        """Load historical consumption patterns from InfluxDB."""
        if days_back is None:
            days_back = self.historical_days_back
        self.logger.info("Loading historical patterns from last %d days (15-min aggregation)", days_back)
        
        end_time = datetime.utcnow()
        start_time = end_time - timedelta(days=days_back)
        
        slots_per_day = 96
        
        for asset_id, mapping in self.asset_mapping.items():
            if isinstance(mapping, dict):
                device_name = mapping.get("device_name_tag")
                field = mapping.get("field", "active_power")
            else:
                device_name = mapping
                field = "active_power"
            
            site = asset_id.split(".")[0]
            
            query = (
                f"SELECT MEAN({field}) as mean_power FROM assets_data "
                f"WHERE time >= '{start_time.strftime('%Y-%m-%dT%H:%M:%SZ')}' "
                f"AND time < '{end_time.strftime('%Y-%m-%dT%H:%M:%SZ')}' "
                f"AND site='{site}' AND device_name='{device_name}' "
                f"GROUP BY time(15m)"
            )
            
            try:
                res = self.influx_client.query(query)
                
                weekday_slots = {s: [] for s in range(slots_per_day)}
                weekend_slots = {s: [] for s in range(slots_per_day)}
                
                for series in res.raw.get("series", []):
                    columns = series.get("columns", [])
                    values = series.get("values", [])
                    
                    mean_idx = columns.index("mean") if "mean" in columns else 1
                    time_idx = columns.index("time") if "time" in columns else 0
                    
                    for row in values:
                        timestamp = pd.to_datetime(row[time_idx])
                        value = row[mean_idx]
                        
                        slot_idx = timestamp.hour * 4 + timestamp.minute // 15
                        
                        if value is not None and value > 0:
                            if timestamp.weekday() >= 5:
                                weekend_slots[slot_idx].append(value)
                            else:
                                weekday_slots[slot_idx].append(value)
                
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
                self.logger.info("Loaded patterns for %s: %d data points", asset_id, total_points)
                
            except Exception as e:
                self.logger.error("Error loading patterns for %s: %s", asset_id, str(e))
                self._historical_patterns[asset_id] = {
                    "weekday": {s: 0 for s in range(slots_per_day)},
                    "weekend": {s: 0 for s in range(slots_per_day)},
                    "data_points": {s: 0 for s in range(slots_per_day)}
                }
    
    def _load_ev_occupancy_patterns(self, days_back: int = None):
        """Load EV charger occupancy patterns from InfluxDB."""
        if days_back is None:
            days_back = self.historical_days_back
        
        self.logger.info("Loading EV occupancy patterns from last %d days", days_back)
        
        end_time = datetime.utcnow()
        start_time = end_time - timedelta(days=days_back)
        
        slots_per_day = 96
        
        for asset_id, mapping in self.asset_mapping.items():
            if not isinstance(mapping, dict):
                continue
            asset_type = mapping.get("type", "")
            if asset_type != "ev_charger":
                continue
            
            device_name = mapping.get("device_name_tag")
            field = mapping.get("field", "power")
            site = asset_id.split(".")[0]
            
            query = (
                f"SELECT MEAN({field}) as mean_power FROM assets_data "
                f"WHERE time >= '{start_time.strftime('%Y-%m-%dT%H:%M:%SZ')}' "
                f"AND time < '{end_time.strftime('%Y-%m-%dT%H:%M:%SZ')}' "
                f"AND site='{site}' AND device_name='{device_name}' "
                f"GROUP BY time(15m)"
            )
            
            try:
                res = self.influx_client.query(query)
                
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
        
        if peak_slots > 0:
            peak_df = df[df["is_peak_hour"]]
            self.logger.info("-" * 70)
            self.logger.info("PEAK HOURS (07-10, 16-20):")
            self.logger.info(
                "  Flexibility - avg: %.4f MW (%.1f kW)",
                peak_df["flexibility_mw"].mean(),
                peak_df["flexibility_kw"].mean()
            )
        
        confidence_counts = df["confidence"].value_counts()
        self.logger.info("-" * 70)
        self.logger.info("Confidence: %s", dict(confidence_counts))
        self.logger.info("=" * 70)
    
    def get_asset_flexibility_breakdown(
        self, 
        period_from: datetime,
        use_temperature: bool = True
    ) -> Dict[str, Dict]:
        """
        Get detailed flexibility breakdown per asset for a specific 15-minute slot.
        
        For heat pumps with temperature enabled:
        - Uses temperature-based power estimates instead of simple averages
        
        For EV chargers:
        - Weights flexibility by occupancy probability
        
        :param period_from: Start of time slot
        :param use_temperature: Whether to use temperature-based HP estimation
        :return: Dictionary with per-asset flexibility info
        """
        slot_idx = period_from.hour * 4 + period_from.minute // 15
        is_weekend = period_from.weekday() >= 5
        day_type = "weekend" if is_weekend else "weekday"
        
        if not self._historical_patterns:
            self._load_historical_patterns()
        
        if not self._ev_occupancy_patterns:
            self._load_ev_occupancy_patterns()
        
        # Load temperature profiles if enabled and not loaded
        if self.temperature_enabled and use_temperature:
            if not self._hp_temperature_profiles:
                self._build_hp_temperature_profiles()
            forecast_temp = self.get_forecast_temperature(period_from)
        else:
            forecast_temp = None
        
        breakdown = {}
        
        for asset_id in self.asset_capacities.keys():
            mapping = self.asset_mapping.get(asset_id, {})
            asset_type = mapping.get("type", "") if isinstance(mapping, dict) else ""
            
            asset_pattern = self._historical_patterns.get(asset_id, {})
            typical_load_w = asset_pattern.get(day_type, {}).get(slot_idx, 0)
            typical_load_kw = typical_load_w / 1000
            flex_factor = self.flexibility_factors.get(asset_id, self.default_flexibility_factor)
            nominal_kw = self.asset_capacities.get(asset_id, 0)
            
            if asset_type == "ev_charger":
                # EV charger: factor in occupancy probability
                occupancy_pattern = self._ev_occupancy_patterns.get(asset_id, {})
                occupancy_prob = occupancy_pattern.get(day_type, {}).get(slot_idx, 0)
                
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
                    "estimation_method": "occupancy_weighted",
                }
            
            elif asset_type == "heat_pump" and self.temperature_enabled and forecast_temp is not None:
                # Heat pump with temperature-aware estimation
                temp_power_w, confidence, source = self.get_hp_expected_power(
                    asset_id, slot_idx, is_weekend, forecast_temp
                )
                
                if temp_power_w is not None:
                    # Use temperature-based estimate
                    temp_load_kw = temp_power_w / 1000
                    available_flex_kw = temp_load_kw * flex_factor
                    
                    breakdown[asset_id] = {
                        "description": self.asset_descriptions.get(asset_id, asset_id),
                        "asset_type": asset_type,
                        "nominal_capacity_kw": nominal_kw,
                        "typical_load_kw": typical_load_kw,  # Original (time-based)
                        "temperature_adjusted_load_kw": temp_load_kw,  # Temperature-based
                        "forecast_temperature_c": forecast_temp,
                        "flexibility_factor": flex_factor,
                        "occupancy_probability": None,
                        "available_flexibility_kw": available_flex_kw,
                        "max_flexibility_kw": nominal_kw * flex_factor,
                        "estimation_method": source,
                        "estimation_confidence": confidence,
                    }
                else:
                    # Fallback to simple time-based
                    available_flex_kw = typical_load_kw * flex_factor
                    
                    breakdown[asset_id] = {
                        "description": self.asset_descriptions.get(asset_id, asset_id),
                        "asset_type": asset_type,
                        "nominal_capacity_kw": nominal_kw,
                        "typical_load_kw": typical_load_kw,
                        "flexibility_factor": flex_factor,
                        "occupancy_probability": None,
                        "available_flexibility_kw": available_flex_kw,
                        "max_flexibility_kw": nominal_kw * flex_factor,
                        "estimation_method": "time_based_fallback",
                    }
            
            else:
                # Heat pump (no temperature) or other asset
                breakdown[asset_id] = {
                    "description": self.asset_descriptions.get(asset_id, asset_id),
                    "asset_type": asset_type,
                    "nominal_capacity_kw": nominal_kw,
                    "typical_load_kw": typical_load_kw,
                    "flexibility_factor": flex_factor,
                    "occupancy_probability": None,
                    "available_flexibility_kw": typical_load_kw * flex_factor,
                    "max_flexibility_kw": nominal_kw * flex_factor,
                    "estimation_method": "time_based",
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
        
        if self.temperature_enabled:
            print(f"Temperature-aware analysis: ENABLED (bins: {self.temperature_bins})")
        print()
    
    # =========================================================================
    # DISCRETIZATION-AWARE FLEXIBILITY CALCULATION
    # =========================================================================
    
    def _get_modulation_type(self, asset_id: str) -> str:
        """
        Get the modulation type for an asset.
        
        :param asset_id: Asset identifier
        :return: 'continuous' or 'discrete'
        """
        mapping = self.asset_mapping.get(asset_id, {})
        if not isinstance(mapping, dict):
            return "continuous"
        
        # Explicit modulation_type in config takes priority
        if "modulation_type" in mapping:
            return mapping["modulation_type"]
        
        # Default based on asset type
        asset_type = mapping.get("type", "")
        if asset_type == "heat_pump":
            return "discrete"
        return "continuous"
    
    def _get_discrete_states(self, asset_id: str) -> List[float]:
        """
        Get the discrete power states for an asset.
        
        :param asset_id: Asset identifier
        :return: List of achievable power states in kW (e.g., [0.0, 15.0] for ON/OFF)
        """
        mapping = self.asset_mapping.get(asset_id, {})
        if not isinstance(mapping, dict):
            return [0.0]
        
        # Explicit discrete_states_kw in config
        if "discrete_states_kw" in mapping:
            return mapping["discrete_states_kw"]
        
        # Default: ON/OFF at full capacity
        capacity = mapping.get("capacity_kw", 0.0)
        return [0.0, capacity]
    
    def _enumerate_discrete_combinations(
        self, 
        discrete_assets: Dict[str, Dict]
    ) -> List[Tuple[float, Dict[str, float]]]:
        """
        Enumerate all possible power combinations from discrete assets.
        
        Uses subset-sum enumeration. For N assets with binary states, 
        this produces 2^N combinations.
        
        :param discrete_assets: Dict of {asset_id: {'capacity_kw': X, 'available': True/False, ...}}
        :return: List of (total_power_kw, {asset_id: power_kw}) tuples, sorted by total power
        """
        if not discrete_assets:
            return [(0.0, {})]
        
        # Start with empty combination
        combinations = [(0.0, {})]
        
        for asset_id, info in discrete_assets.items():
            # Get the discrete states for this asset
            states = self._get_discrete_states(asset_id)
            available = info.get("available", True)
            
            if not available:
                # Asset not available - can only be in OFF state
                states = [0.0]
            
            new_combinations = []
            for total, allocation in combinations:
                for state in states:
                    new_total = total + state
                    new_allocation = allocation.copy()
                    new_allocation[asset_id] = state
                    new_combinations.append((new_total, new_allocation))
            
            combinations = new_combinations
        
        # Sort by total power
        combinations.sort(key=lambda x: x[0])
        
        # Remove duplicates (same total power)
        unique_combinations = []
        seen_totals = set()
        for total, allocation in combinations:
            # Round to avoid floating point issues
            rounded_total = round(total, 2)
            if rounded_total not in seen_totals:
                seen_totals.add(rounded_total)
                unique_combinations.append((total, allocation))
        
        return unique_combinations
    
    def get_achievable_flexibility(
        self,
        period_from: datetime,
        target_kw: float = None,
        allowed_assets: List[str] = None,
        use_temperature: bool = True
    ) -> Dict:
        """
        Calculate achievable flexibility considering discrete asset constraints.
        
        This is the discretization-aware version of get_asset_flexibility_breakdown().
        It calculates what power levels can actually be delivered, not just
        fractional sums.
        
        :param period_from: Start of time slot
        :param target_kw: Optional target flexibility (if None, returns max achievable)
        :param allowed_assets: Optional list of asset IDs to consider
        :param use_temperature: Whether to use temperature-based HP estimation
        :return: Dictionary with:
            - 'discrete_combinations': All achievable discrete power levels
            - 'continuous_range': (min, max) from continuous assets
            - 'total_achievable_range': (min, max) total flexibility
            - 'recommended_bid_kw': Best bid quantity for target (if provided)
            - 'asset_breakdown': Per-asset details
            - 'discrete_assets': List of discrete asset IDs
            - 'continuous_assets': List of continuous asset IDs
        """
        # Get base asset breakdown
        breakdown = self.get_asset_flexibility_breakdown(period_from, use_temperature)
        
        # Filter by allowed assets if specified
        if allowed_assets:
            breakdown = {k: v for k, v in breakdown.items() if k in allowed_assets}
        
        # Separate into discrete and continuous assets
        discrete_assets = {}
        continuous_assets = {}
        
        for asset_id, info in breakdown.items():
            mod_type = self._get_modulation_type(asset_id)
            
            # Check if asset is available (has flexibility)
            available_flex = info.get("available_flexibility_kw", 0)
            flex_factor = info.get("flexibility_factor", 0.5)
            nominal_kw = info.get("nominal_capacity_kw", 0)
            
            # For discrete assets, availability is probabilistic
            # We consider it "available" if flex_factor > threshold
            is_available = flex_factor >= 0.5  # 50% threshold
            
            if mod_type == "discrete":
                discrete_assets[asset_id] = {
                    "capacity_kw": nominal_kw,
                    "available": is_available,
                    "availability_prob": flex_factor,
                    "info": info
                }
            else:
                # Continuous assets can be modulated to any value within their range
                # Get min_power_kw from config (some EVs have minimum charging power)
                mapping = self.asset_mapping.get(asset_id, {})
                min_power_kw = mapping.get("min_power_kw", 0.0) if isinstance(mapping, dict) else 0.0
                
                # Controllable range: from min_power to capacity (or 0 if turned off)
                # available_flex_kw is the expected value considering occupancy
                continuous_assets[asset_id] = {
                    "capacity_kw": nominal_kw,
                    "min_power_kw": min_power_kw,
                    "available_flex_kw": available_flex,  # Expected availability
                    "controllable_range_kw": (0.0, nominal_kw),  # Full modulation range
                    "typical_load_kw": info.get("typical_load_kw", 0),
                    "occupancy_prob": info.get("occupancy_probability", 1.0),
                    "info": info
                }
        
        # Enumerate discrete combinations
        discrete_combos = self._enumerate_discrete_combinations(discrete_assets)
        discrete_levels = [combo[0] for combo in discrete_combos]
        
        # Calculate continuous range
        # For bidding: use available_flex_kw (expected value based on occupancy)
        # For control: full range from 0 to sum of capacities
        continuous_min = 0.0
        continuous_available = sum(
            a["available_flex_kw"] for a in continuous_assets.values()
        )
        continuous_max_capacity = sum(
            a["capacity_kw"] for a in continuous_assets.values()
        )
        
        # Calculate total achievable range
        # Min: smallest discrete + no continuous
        # Max: largest discrete + all continuous (use available for bidding)
        total_min = min(discrete_levels) if discrete_levels else 0.0
        total_max_available = max(discrete_levels) + continuous_available if discrete_levels else continuous_available
        total_max_capacity = max(discrete_levels) + continuous_max_capacity if discrete_levels else continuous_max_capacity
        
        result = {
            "period_from": period_from.isoformat(),
            "discrete_combinations": discrete_combos,
            "discrete_levels_kw": discrete_levels,
            "continuous_range_kw": (continuous_min, continuous_available),  # For bidding
            "continuous_max_capacity_kw": continuous_max_capacity,  # Full control range
            "total_achievable_range_kw": (total_min, total_max_available),  # For bidding
            "total_max_capacity_kw": total_max_capacity,  # If all assets available
            "asset_breakdown": breakdown,
            "discrete_assets": list(discrete_assets.keys()),
            "continuous_assets": list(continuous_assets.keys()),
            "discrete_asset_details": discrete_assets,
            "continuous_asset_details": continuous_assets,
        }
        
        # If target is provided, calculate the best bid
        if target_kw is not None:
            best_bid = self._calculate_best_bid(
                target_kw, discrete_combos, continuous_available, continuous_assets
            )
            result["target_kw"] = target_kw
            result["recommended_bid_kw"] = best_bid["bid_kw"]
            result["recommended_allocation"] = best_bid["allocation"]
            result["bid_deviation_kw"] = best_bid["bid_kw"] - target_kw
            result["bid_deviation_pct"] = (
                (best_bid["bid_kw"] - target_kw) / target_kw * 100 
                if target_kw > 0 else 0
            )
        
        return result
    
    def _calculate_best_bid(
        self,
        target_kw: float,
        discrete_combos: List[Tuple[float, Dict[str, float]]],
        continuous_max_kw: float,
        continuous_assets: Dict[str, Dict] = None
    ) -> Dict:
        """
        Calculate the best achievable bid for a target flexibility.
        
        Strategy:
        1. Find the largest discrete combination <= target
        2. Fill remaining with continuous assets (proportionally distributed)
        3. If still under target, consider next discrete level
        
        For continuous assets (e.g., EV chargers):
        - Can be modulated to any value within [min_power_kw, capacity_kw]
        - Distribution is proportional to available flexibility
        
        :param target_kw: Target flexibility in kW
        :param discrete_combos: List of (total, allocation) from discrete assets
        :param continuous_max_kw: Maximum available from continuous assets
        :param continuous_assets: Dict of continuous asset details for per-asset allocation
        :return: Dict with 'bid_kw' and detailed allocation
        """
        continuous_assets = continuous_assets or {}
        
        if not discrete_combos:
            # No discrete assets - just use continuous
            bid = min(target_kw, continuous_max_kw)
            continuous_alloc = self._allocate_continuous(bid, continuous_assets)
            return {
                "bid_kw": round(bid, 3),
                "allocation": {
                    "discrete": {},
                    "continuous": continuous_alloc,
                    "continuous_total_kw": round(bid, 3)
                },
                "discrete_kw": 0,
                "continuous_kw": round(bid, 3),
                "strategy": "continuous_only"
            }
        
        best_bid = None
        best_deviation = float('inf')
        
        for discrete_total, discrete_alloc in discrete_combos:
            # Can we reach target with this discrete combination + continuous?
            remaining = target_kw - discrete_total
            
            if remaining <= 0:
                # Discrete alone exceeds target
                # This is an over-delivery scenario
                continuous_contrib = 0
                total_bid = discrete_total
            elif remaining <= continuous_max_kw:
                # We can exactly match or fill with continuous
                continuous_contrib = remaining
                total_bid = discrete_total + continuous_contrib
            else:
                # Even with max continuous, we're under target
                continuous_contrib = continuous_max_kw
                total_bid = discrete_total + continuous_contrib
            
            deviation = abs(total_bid - target_kw)
            
            # Prefer slight under-delivery over over-delivery (conservative)
            # Add small penalty for over-delivery
            if total_bid > target_kw:
                deviation += 0.01  # Small penalty for over-delivery
            
            if deviation < best_deviation:
                best_deviation = deviation
                # Calculate per-asset allocation for continuous assets
                continuous_alloc = self._allocate_continuous(continuous_contrib, continuous_assets)
                
                best_bid = {
                    "bid_kw": round(total_bid, 3),
                    "allocation": {
                        "discrete": discrete_alloc,
                        "continuous": continuous_alloc,
                        "continuous_total_kw": round(continuous_contrib, 3)
                    },
                    "discrete_kw": round(discrete_total, 3),
                    "continuous_kw": round(continuous_contrib, 3),
                    "strategy": "discrete_first" if discrete_total > 0 else "continuous_only"
                }
        
        return best_bid
    
    def _allocate_continuous(
        self, 
        target_kw: float, 
        continuous_assets: Dict[str, Dict]
    ) -> Dict[str, Dict]:
        """
        Allocate power to continuous assets proportionally.
        
        Continuous assets (like EV chargers) can be modulated to any value
        within their controllable range [min_power_kw, capacity_kw].
        
        :param target_kw: Total power to allocate to continuous assets
        :param continuous_assets: Dict of {asset_id: {capacity_kw, min_power_kw, available_flex_kw, ...}}
        :return: Dict of {asset_id: {power_kw, min_kw, max_kw, setpoint_pct}}
        """
        if not continuous_assets or target_kw <= 0:
            return {}
        
        # Calculate total available flexibility for proportional distribution
        total_available = sum(a.get("available_flex_kw", 0) for a in continuous_assets.values())
        
        if total_available <= 0:
            return {}
        
        allocation = {}
        remaining = target_kw
        
        for asset_id, info in continuous_assets.items():
            available = info.get("available_flex_kw", 0)
            capacity = info.get("capacity_kw", 0)
            min_power = info.get("min_power_kw", 0)
            
            if available <= 0 or capacity <= 0:
                continue
            
            # Proportional allocation based on available flexibility
            proportion = available / total_available
            asset_share = target_kw * proportion
            
            # Clamp to asset's controllable range
            # For curtailment: we're reducing from typical load, so max reduction = available
            power_kw = min(asset_share, available, remaining)
            power_kw = max(power_kw, 0)  # Can't allocate negative
            
            # Calculate setpoint as percentage of capacity
            setpoint_pct = (power_kw / capacity * 100) if capacity > 0 else 0
            
            allocation[asset_id] = {
                "power_kw": round(power_kw, 3),
                "min_power_kw": min_power,
                "max_power_kw": capacity,
                "available_flex_kw": round(available, 3),
                "setpoint_pct": round(setpoint_pct, 1),
                "modulation": "continuous"
            }
            
            remaining -= power_kw
        
        return allocation
    
    def get_biddable_quantities(
        self,
        period_from: datetime,
        allowed_assets: List[str] = None,
        use_temperature: bool = True
    ) -> List[float]:
        """
        Get all exactly-achievable bid quantities for a time slot.
        
        This returns the discrete "steps" at which the FSP can bid,
        useful for quantum-style bidding or for understanding constraints.
        
        :param period_from: Start of time slot
        :param allowed_assets: Optional list of asset IDs to consider
        :param use_temperature: Whether to use temperature-based HP estimation
        :return: List of achievable power levels in kW, sorted ascending
        """
        result = self.get_achievable_flexibility(
            period_from, 
            target_kw=None,
            allowed_assets=allowed_assets,
            use_temperature=use_temperature
        )
        
        discrete_levels = result["discrete_levels_kw"]
        continuous_max = result["continuous_range_kw"][1]
        
        # Each discrete level can be augmented with continuous up to continuous_max
        # For simplicity, return discrete levels + their max extensions
        biddable = []
        for level in discrete_levels:
            biddable.append(level)
            if continuous_max > 0:
                biddable.append(level + continuous_max)
        
        # Remove duplicates and sort
        biddable = sorted(set(round(b, 3) for b in biddable))
        
        return biddable