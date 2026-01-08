# Flexibility Analysis

This document explains how the system analyzes and calculates available flexibility from the asset portfolio.

## Overview

Before placing bids on the flexibility market, the system performs a **Flexibility Analysis** to determine:
1. How much load each asset is typically consuming at this time
2. What percentage of that load can be curtailed (reduced)
3. For EV chargers: the probability that a car is actually connected

This analysis uses **30 days of historical data** from InfluxDB to build consumption patterns.

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
│  2. CALCULATE TYPICAL LOAD                                              │
│     └─> Average power consumption for this time slot                    │
│     └─> Based on day type (weekday/weekend)                             │
│                                                                          │
│  3. APPLY FLEXIBILITY FACTOR                                            │
│     └─> HP: typical_load × flexibility_factor                           │
│     └─> EV: typical_load × flexibility_factor × occupancy_probability   │
│                                                                          │
│  4. SUM AVAILABLE FLEXIBILITY                                           │
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

```
available_flexibility = typical_load × flexibility_factor
```

**Example: ECM97.2 at 17:00**
```
typical_load = 11.27 kW
flexibility_factor = 0.85
available_flexibility = 11.27 × 0.85 = 9.58 kW
```

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
  "peak_hours": {
    "morning": {"start": 7, "end": 10},
    "evening": {"start": 16, "end": 20}
  },
  "historical_days_back": 30,
  "default_flexibility_factor": 0.50,
  "ev_charger": {
    "occupancy_threshold_w": 100
  }
}
```

| Parameter | Value | Description |
|-----------|-------|-------------|
| `historical_days_back` | 30 | Days of history to analyze |
| `default_flexibility_factor` | 0.50 | Default if not specified per asset |
| `occupancy_threshold_w` | 100 | Power > 100W means car is connected |

### Asset Configuration

From `asset_mapping`:

```json
"ECM97.2": {
  "device_name_tag": "shelly_3em_pro_heat_pump_2",
  "field": "active_power",
  "type": "heat_pump",
  "description": "HP Cinema 2",
  "capacity_kw": 15.0,
  "flexibility_factor": 0.85
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

Key methods:
- `_load_historical_patterns()` - Load 30-day consumption patterns
- `_load_ev_occupancy_patterns()` - Calculate EV occupancy probabilities
- `get_asset_flexibility_breakdown()` - Per-asset flexibility calculation
- `_is_peak_hour()` - Peak hour detection

---

## Related Documentation

- [BIDDING_STRATEGIES.md](BIDDING_STRATEGIES.md) - How strategies use flexibility data
- [README_strategy_evaluator.md](../scripts/README_strategy_evaluator.md) - Strategy evaluation tool

