# Flexibility Analysis

This document explains how the system analyzes and calculates available flexibility from the asset portfolio.

The repository now supports two forecasting modes:

- `flexibility.method = "historical"`: the existing historical/time-pattern logic described in most of this document.
- `flexibility.method = "persistence"`: the operational mode used for current bidding, based on recent measured power from `assets_data`.

## Overview

Before placing bids on the flexibility market, the system performs a **Flexibility Analysis** to determine:
1. How much load each asset is typically consuming at this time
2. What percentage of that load can be curtailed (reduced)
3. For EV chargers: the probability that a car is actually connected

This analysis uses **30 days of historical data** from InfluxDB to build consumption patterns.

---

## Operational Persistence Mode

For the operational path, flexibility is computed directly from recent measured values rather than from long historical averages.

### Target slot and source slot

```text
target_slot_utc = floor(current_time_utc, fm.granularity) + fm.ordersTimeShift
flexibility_go_back_i =
    asset_mapping.<asset>.flexibility_persistence_go_back_minutes
    if present
    else flexibility.persistenceSettings.persistenceGoBackMinutes

baseline_source_time_i = target_slot_utc - flexibility_go_back_i
```

Example with the current config:

```text
current_time_utc         = 2026-05-12 08:20
target_slot_utc          = 2026-05-12 09:45
ECM96.2 source time    = 2026-05-12 08:15  (90 min)
ECM97.3 source time    = 2026-05-12 08:15  (90 min)
ECM63.1 source time    = 2026-05-12 07:45  (120 min)
ECM63.2 source time    = 2026-05-12 07:45  (120 min)
```

### Baseline formula

```text
baseline_i(t) = measured_power_i(t - persistence_go_back_i)
portfolio_baseline(t) = sum_i baseline_i(t)
```

For baseline upload, `persistence_go_back_i` comes from `asset_mapping.<asset>.baseline_persistence_go_back_minutes` when present, otherwise from `baseline.dbSettings.persistenceGoBackMinutes`. For flexibility, it comes from `asset_mapping.<asset>.flexibility_persistence_go_back_minutes` when present, otherwise from `flexibility.persistenceSettings.persistenceGoBackMinutes`.

Per-asset overrides allow assets with slower telemetry, such as EV chargers `ECM63.1` and `ECM63.2`, to use a longer lag while staying in the same NODES portfolio as HP assets `ECM96.2` and `ECM97.3`. The portfolio baseline remains the sum of asset-level measured persistence values.

This baseline logic is separate from flexibility. The current-state gate is never applied to the baseline.

### Flexibility formula

For each asset:

```text
current_measured_power_i = latest grouped measurement at or before current_time_utc

if current_measured_power_i <= activeThresholdW:
    flexibility_i(t) = 0
else:
    flexibility_i(t) = safety_factor_i * min(baseline_i(t), nominal_power_i)
```

Then:

```text
portfolio_flexibility(t) = sum_i flexibility_i(t)
```

Operational rules:

- internal asset-level values stay in W,
- `nominal_power_w` must be configured per asset for persistence mode,
- `persistence_safety_factor` falls back to `defaultSafetyFactor`,
- missing baseline source measurements do not get invented,
- missing or stale current gate measurements do not imply that an asset is active.
- per-asset go-back values must align with `fm.granularity`; invalid values fail instead of being rounded.

The default policies are conservative:

- baseline slot persistence: `missingMeasurementPolicy = "fail_portfolio"`
- flexibility persistence: `missingMeasurementPolicy = "skip_asset"`

---

## The Flexibility Analysis Process

```
┌─────────────────────────────────────────────────────────────────────────┐
│                    FLEXIBILITY ANALYSIS FOR SLOT                         │
│                        2026-01-08 17:00                                  │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  1. LOAD HISTORICAL PATTERNS (30 days)                                  │
│     └─> Query InfluxDB for each asset                                   │
│     └─> Aggregate by 15-min slots                                       │
│     └─> Separate weekday vs weekend patterns                            │
│                                                                          │
│  2. LOAD TEMPERATURE DATA (for Heat Pumps)                              │
│     └─> Get historical temperatures                                     │
│     └─> Build temperature-power correlation profiles                    │
│     └─> Get temperature forecast for target slot                        │
│                                                                          │
│  3. CALCULATE TYPICAL/EXPECTED LOAD                                     │
│     └─> HP with temperature: power at forecast temp for this slot       │
│     └─> HP without temperature: average for this time slot              │
│     └─> EV: average for this time slot × occupancy probability          │
│                                                                          │
│  4. APPLY FLEXIBILITY FACTOR                                            │
│     └─> HP: expected_load × flexibility_factor                          │
│     └─> EV: typical_load × flexibility_factor × occupancy_probability   │
│                                                                          │
│  5. SUM AVAILABLE FLEXIBILITY                                           │
│     └─> Total from all assets (or filtered by strategy)                 │
│                                                                          │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Key Concepts

### 1. Typical Load

The **average power consumption** for a specific 15-minute slot, based on historical data.

```
Example for ECM97.2 (HP Cinema 2) at 17:00 on a weekday:
- Historical data: [10.5, 11.2, 12.1, 10.8, 11.5, ...] kW
- Typical load = average = 11.27 kW
```

The system uses **96 time slots per day** (24 hours × 4 slots/hour) and tracks patterns separately for:
- **Weekdays** (Monday-Friday)
- **Weekends** (Saturday-Sunday)

### 2. Flexibility Factor

A configurable percentage indicating how much of the typical load can be curtailed.

| Asset | Flexibility Factor | Meaning |
|-------|-------------------|---------|
| ECM96.2 (HP Small) | 80% | Can reduce up to 80% of its load |
| ECM97.1 (HP Cinema 1) | 85% | Can reduce up to 85% of its load |
| ECM97.2 (HP Cinema 2) | 85% | Can reduce up to 85% of its load |
| ECM63.1 (EV Charger 1) | 70% | Can reduce up to 70% of charging power |
| ECM63.2 (EV Charger 2) | 70% | Can reduce up to 70% of charging power |

**Why not 100%?**
- Heat pumps may need minimum runtime for thermal comfort
- EV chargers may have minimum charging requirements
- Safety margins for reliable delivery

### 3. Occupancy Probability (EV Chargers Only)

For EV chargers, flexibility is only available **when a car is plugged in**. The system calculates:

```
Occupancy = (slots with power > 100W) / (total slots)
```

**Example from log:**
```
ECM63.1: avg occupancy weekday=13.9%, weekend=5.7%
ECM63.2: avg occupancy weekday=20.1%, weekend=7.8%
```

This means:
- On weekdays, ECM63.1 has a car connected only ~14% of the time
- On weekdays, ECM63.2 has a car connected only ~20% of the time

---

## Flexibility Calculation Formulas

### Heat Pumps

#### Basic (Time-Based) Estimation

```
available_flexibility = typical_load × flexibility_factor
```

**Example: ECM97.2 at 17:00**
```
typical_load = 11.27 kW
flexibility_factor = 0.85
available_flexibility = 11.27 × 0.85 = 9.58 kW
```

#### Temperature-Aware Estimation (Enhanced)

When temperature analysis is enabled, the system uses **external temperature** to better predict HP consumption:

```
expected_load = f(temperature, time_slot, day_type)
available_flexibility = expected_load × flexibility_factor
```

**How it works:**

1. **Build Temperature-Power Profile**: Correlate historical HP consumption with external temperatures
2. **Bin Temperatures**: Group temperatures into ranges (e.g., <0°C, 0-5°C, 5-10°C, etc.)
3. **Get Forecast Temperature**: Use weather forecast for the target time slot
4. **Lookup Expected Power**: Find average consumption for this temperature bin + time slot

**Example: ECM97.2 at 17:00 with Temperature**
```
forecast_temperature = 2.5°C
temperature_bin = 0-5°C
expected_load (from profile) = 13.8 kW  # Higher than average due to cold
flexibility_factor = 0.85
available_flexibility = 13.8 × 0.85 = 11.73 kW
```

**Why this matters:**
- Cold weather → Higher HP consumption → More flexibility available
- Warm weather → Lower HP consumption → Less flexibility available
- Standard time-based average ignores temperature variations

### EV Chargers

```
available_flexibility = typical_load × flexibility_factor × occupancy_probability
```

**Example: ECM63.2 at 17:00**
```
typical_load = 5.81 kW
flexibility_factor = 0.70
occupancy_probability = 0.18 (18%)
available_flexibility = 5.81 × 0.70 × 0.18 = 0.74 kW
```

**Why multiply by occupancy?**
- If a car is connected (18% chance), we can curtail 70% of load
- If no car is connected (82% chance), there's nothing to curtail
- Expected value = potential flexibility × probability

---

## Example Analysis Output

### Standard Output (Time-Based)

```
======================================================================
FLEXIBILITY ANALYSIS FOR SLOT: 2026-01-08 17:00
======================================================================
Peak hour: YES

Loading historical patterns from last 30 days (15-min aggregation)
├─ ECM63.1: 511 data points
├─ ECM63.2: 649 data points
├─ ECM96.2: 2019 data points
├─ ECM97.1: 2881 data points
└─ ECM97.2: 2881 data points

Loading EV occupancy patterns from last 30 days
├─ ECM63.1: avg occupancy weekday=13.9%, weekend=5.7%
└─ ECM63.2: avg occupancy weekday=20.1%, weekend=7.8%

----------------------------------------------------------------------
Asset flexibility breakdown:
----------------------------------------------------------------------
  ECM63.1 (EV Charger 1): typical=3.36 kW, occupancy=18%, available_flex=0.43 kW (factor=70%)
  ECM63.2 (EV Charger 2): typical=5.81 kW, occupancy=18%, available_flex=0.74 kW (factor=70%)
  ECM96.2 (HP Small):     typical=2.07 kW,                 available_flex=1.65 kW (factor=80%)
  ECM97.1 (HP Cinema 1):  typical=3.10 kW,                 available_flex=2.64 kW (factor=85%)
  ECM97.2 (HP Cinema 2):  typical=11.27 kW,                available_flex=9.58 kW (factor=85%)
----------------------------------------------------------------------
TOTAL AVAILABLE FLEXIBILITY: 15.036 kW (0.015 MW)
----------------------------------------------------------------------
```

### Temperature-Aware Output (Enhanced)

When temperature analysis is enabled, the output includes temperature information:

```
======================================================================
FLEXIBILITY ANALYSIS FOR SLOT: 2026-01-08 17:00
======================================================================
Peak hour: YES
Temperature forecast: 3.2°C
Temperature-aware HP analysis: ENABLED

Loading historical patterns from last 30 days (15-min aggregation)
├─ ECM63.1: 511 data points
├─ ECM63.2: 649 data points
├─ ECM96.2: 2019 data points
├─ ECM97.1: 2881 data points
└─ ECM97.2: 2881 data points

Loading temperature history from last 30 days
└─ Loaded 2880 temperature records: avg=8.3°C, min=-2.1°C, max=18.5°C

Building HP temperature profiles from last 30 days
├─ ECM96.2: 1853 data points, correlation=-0.72, temp range=-2.1-18.5°C
├─ ECM97.1: 2654 data points, correlation=-0.78, temp range=-2.1-18.5°C
└─ ECM97.2: 2654 data points, correlation=-0.81, temp range=-2.1-18.5°C

Loading EV occupancy patterns from last 30 days
├─ ECM63.1: avg occupancy weekday=13.9%, weekend=5.7%
└─ ECM63.2: avg occupancy weekday=20.1%, weekend=7.8%

----------------------------------------------------------------------
Asset flexibility breakdown:
----------------------------------------------------------------------
  ECM63.1 (EV Charger 1): typical=3.36 kW, occupancy=18%, available_flex=0.43 kW (factor=70%)
  ECM63.2 (EV Charger 2): typical=5.81 kW, occupancy=18%, available_flex=0.74 kW (factor=70%)
  ECM96.2 (HP Small):     time_based=2.07 kW, temp_adjusted=2.85 kW @ 3.2°C, available_flex=2.28 kW (method=temp_profile:0-5°C)
  ECM97.1 (HP Cinema 1):  time_based=3.10 kW, temp_adjusted=4.25 kW @ 3.2°C, available_flex=3.61 kW (method=temp_profile:0-5°C)
  ECM97.2 (HP Cinema 2):  time_based=11.27 kW, temp_adjusted=14.10 kW @ 3.2°C, available_flex=11.99 kW (method=temp_profile:0-5°C)
----------------------------------------------------------------------
TOTAL AVAILABLE FLEXIBILITY: 19.05 kW (0.019 MW)
----------------------------------------------------------------------
```

**Key differences:**
- **Temperature forecast** is shown at the top (3.2°C)
- **Temperature correlation** is logged for each HP (negative = colder → more power)
- **HP output shows both estimates**: `time_based` (simple average) and `temp_adjusted` (temperature-based)
- **Available flexibility is higher** when it's cold (14.10 kW vs 11.27 kW for ECM97.2)

---

## Temperature-Power Correlation

Heat pumps show a strong **negative correlation** between external temperature and power consumption:

```
Temperature ↓  →  HP Power ↑  →  Flexibility ↑
Temperature ↑  →  HP Power ↓  →  Flexibility ↓
```

### Typical Correlation Values

| Asset | Correlation | Interpretation |
|-------|-------------|----------------|
| ECM97.2 | -0.81 | Strong: cold = high power |
| ECM97.1 | -0.78 | Strong: cold = high power |
| ECM96.2 | -0.72 | Moderate-strong |

**Correlation interpretation:**
- `-1.0` = Perfect negative: every °C drop = proportional power increase
- `-0.5` = Moderate negative: temperature matters but other factors too
- `0` = No relationship

### Why This Matters for Bidding

| Scenario | Temperature | HP Power | Flexibility | Strategy |
|----------|-------------|----------|-------------|----------|
| Cold Winter Day | -2°C | ~15 kW | ~13 kW | Bid aggressively |
| Mild Spring Day | 12°C | ~6 kW | ~5 kW | Bid conservatively |
| Warm Summer Day | 22°C | ~2 kW | ~1.5 kW | Focus on EVs instead |

### Temperature Profile by Time Slot

The system builds profiles for each **combination of**:
- Temperature bin (e.g., 0-5°C)
- Time slot (e.g., 17:00-17:15)
- Day type (weekday/weekend)

This allows accurate predictions like:
> "On a Thursday at 17:00 with 3°C forecast, ECM97.2 typically consumes 14.1 kW"

---

## Strategy Filtering

When a bidding strategy is active, assets are filtered:

```
Asset flexibility breakdown (strategy filter: strategy_4):
----------------------------------------------------------------------
  [✗] ECM63.1 (EV Charger 1): typical=3.36 kW, occupancy=18%, available_flex=0.43 kW
  [✗] ECM63.2 (EV Charger 2): typical=5.81 kW, occupancy=18%, available_flex=0.74 kW
  [✓] ECM96.2 (HP Small):     typical=2.07 kW, available_flex=1.65 kW
  [✓] ECM97.1 (HP Cinema 1):  typical=3.10 kW, available_flex=2.64 kW
  [✓] ECM97.2 (HP Cinema 2):  typical=11.27 kW, available_flex=9.58 kW
----------------------------------------------------------------------
TOTAL AVAILABLE (all assets): 15.036 kW (0.015 MW)
STRATEGY AVAILABLE (filtered): 13.87 kW (0.014 MW)
----------------------------------------------------------------------
```

- ✓ = Asset included in strategy
- ✗ = Asset excluded by strategy

---

## Comparison with DSO Demand

The analysis compares available flexibility with DSO's requested quantity:

```
DSO DEMANDS:
  Demand 1: Up=0.141 MW, Down=0.000 MW, Price=11.77 CHF/MW
  TOTAL DSO DEMAND: Up=0.141 MW, Down=0.000 MW
----------------------------------------------------------------------
CAN MEET DSO DEMAND (Up): NO (available=0.015 MW, required=0.141 MW)
```

| Scenario | Meaning |
|----------|---------|
| `CAN MEET: YES` | We have enough flexibility to cover DSO's full request |
| `CAN MEET: NO` | We can only provide partial coverage |

**In this example:**
- DSO wants **141 kW** of flexibility
- We can only provide **15 kW**
- We'll bid what we have (partial coverage)

---

## Data Sources

### InfluxDB Query Structure

```sql
SELECT MEAN(power) as mean_power 
FROM assets_data 
WHERE time >= '2025-12-09T00:00:00Z' 
  AND time < '2026-01-08T17:00:00Z'
  AND site='ECM' 
  AND device_name='shelly_3em_pro_heat_pump_2'
GROUP BY time(15m)
```

### Configuration Parameters

From `test_fm01_aem.json`:

```json
"flexibility": {
  "method": "persistence",
  "peak_hours": {
    "morning": {"start": 7, "end": 10},
    "evening": {"start": 16, "end": 20}
  },
  "historical_days_back": 30,
  "default_flexibility_factor": 0.50,
  "persistenceSettings": {
    "persistenceGoBackMinutes": 90,
    "activeThresholdW": 500,
    "defaultSafetyFactor": 1.0,
    "missingMeasurementPolicy": "skip_asset",
    "maxCurrentMeasurementAgeMinutes": 30
  },
  "ev_charger": {
    "occupancy_threshold_w": 100
  },
  "temperature": {
    "enabled": false,
    "source": {
      "site": "ECM",
      "device": "weather_station",
      "field": "temperature"
    },
    "bins": [-5, 0, 5, 10, 15, 20, 25],
    "forecast": {
      "type": "historical_avg",
      "file": "../data/forecast/temperature_forecast.json"
    }
  }
}
```

| Parameter | Value | Description |
|-----------|-------|-------------|
| `method` | `persistence` or `historical` | Select operational persistence or legacy historical analysis |
| `historical_days_back` | 30 | Days of history to analyze |
| `default_flexibility_factor` | 0.50 | Default if not specified per asset |
| `persistenceGoBackMinutes` | 90 | Lag between target slot and persistence source slot |
| `activeThresholdW` | 500 | Gate threshold for deciding whether an asset is currently active |
| `defaultSafetyFactor` | 1.0 | Default persistence safety factor |
| `missingMeasurementPolicy` | `skip_asset` | Flexibility persistence behavior when required data is missing |
| `maxCurrentMeasurementAgeMinutes` | 30 | Maximum allowed age of the gate measurement |
| `occupancy_threshold_w` | 100 | Power > 100W means car is connected |

### Temperature Configuration

| Parameter | Description |
|-----------|-------------|
| `temperature.enabled` | Enable/disable temperature-aware HP analysis |
| `temperature.source.site` | InfluxDB site tag for weather data |
| `temperature.source.device` | InfluxDB device tag for weather sensor |
| `temperature.source.field` | Field name for temperature readings |
| `temperature.bins` | Temperature bin boundaries in °C |
| `temperature.forecast.type` | Forecast source: `constant`, `historical_avg`, or `file` |
| `temperature.forecast.file` | Path to JSON file with temperature forecast |

#### Forecast Types

| Type | Description |
|------|-------------|
| `constant` | Use a fixed temperature (for testing) |
| `historical_avg` | Use historical average for same time/day |
| `file` | Load from JSON file with hourly forecasts |

#### Temperature Bins

The `bins` array defines temperature ranges for grouping historical data:

```
bins: [-5, 0, 5, 10, 15, 20, 25]

Creates these ranges:
  <-5°C, -5-0°C, 0-5°C, 5-10°C, 10-15°C, 15-20°C, 20-25°C, >25°C
```

### Asset Configuration

From `asset_mapping`:

```json
"ECM97.2": {
  "device_name_tag": "shelly_3em_pro_heat_pump_2",
  "field": "active_power",
  "type": "heat_pump",
  "description": "HP Cinema 2",
  "capacity_kw": 15.0,
  "nominal_power_w": 15000,
  "flexibility_factor": 0.85,
  "persistence_safety_factor": 1.0
}
```

---

## Peak Hour Detection

The system identifies peak hours for logging purposes:

```python
def _is_peak_hour(self, dt):
    hour = dt.hour
    return (7 <= hour < 10) or (16 <= hour < 20)
```

**Peak hours:**
- Morning: 07:00 - 10:00
- Evening: 16:00 - 20:00

During peak hours, DSO typically:
- Has higher demand for flexibility
- Offers higher prices
- Places larger buy orders

---

## Understanding the Numbers

### Why is EV flexibility so low?

```
ECM63.2: typical=5.81 kW, occupancy=18%, available_flex=0.74 kW
```

Even though the charger can deliver 5.81 kW when active, the **18% occupancy** means:
- 82% of the time: no car → no flexibility
- 18% of the time: car present → 5.81 × 0.70 = 4.07 kW available
- Expected value: 4.07 × 0.18 = **0.74 kW**

### Why can't we meet DSO demand?

```
CAN MEET DSO DEMAND (Up): NO (available=0.015 MW, required=0.141 MW)
```

Our portfolio is small:
- Total capacity: 56 kW
- Realistic availability: ~15 kW
- DSO asking for: 141 kW

We're providing **~10%** of what DSO needs. This is fine - the market aggregates flexibility from multiple FSPs.

---

## Class Reference

The flexibility analysis is implemented in:

**`classes/flexibility_forecaster.py`**

### Core Methods

| Method | Description |
|--------|-------------|
| `_load_historical_patterns()` | Load 30-day consumption patterns from InfluxDB |
| `_load_ev_occupancy_patterns()` | Calculate EV occupancy probabilities |
| `get_asset_flexibility_breakdown()` | Per-asset flexibility calculation |
| `_get_asset_flexibility_breakdown_persistence()` | Per-asset persistence baseline + current-state gate calculation |
| `_get_latest_grouped_measurement()` | Latest grouped asset measurement at or before current UTC time |
| `get_achievable_flexibility()` | Discretization-aware achievable quantity using either historical or persistence inputs |
| `_is_peak_hour()` | Peak hour detection |

### Temperature-Aware Methods

| Method | Description |
|--------|-------------|
| `_load_temperature_history()` | Load historical temperature data |
| `_build_hp_temperature_profiles()` | Build temperature-power correlation profiles |
| `_load_temperature_forecast()` | Load forecast from file, API, or historical average |
| `get_forecast_temperature()` | Get temperature forecast for a specific time |
| `get_hp_expected_power()` | Get expected HP power based on temperature |
| `_get_temp_bin()` | Map temperature to bin label |
| `_get_temp_bin_labels()` | Generate temperature bin labels |

### Attributes

| Attribute | Type | Description |
|-----------|------|-------------|
| `temperature_enabled` | bool | Whether temperature analysis is active |
| `temperature_bins` | list | Temperature bin boundaries |
| `_temperature_history` | DataFrame | Historical temperature data |
| `_hp_temperature_profiles` | dict | Temperature-power profiles per HP |
| `_temperature_forecast` | dict | Forecast temperatures by datetime |

---

## Related Documentation

- [BIDDING_STRATEGIES.md](BIDDING_STRATEGIES.md) - How strategies use flexibility data
- [README_strategy_evaluator.md](../scripts/README_strategy_evaluator.md) - Strategy evaluation tool
