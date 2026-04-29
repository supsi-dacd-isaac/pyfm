# Strategy Evaluator

A script for evaluating and comparing bidding strategies for the flexibility market.

## Overview

The Strategy Evaluator calculates expected revenue, costs, and net profit for different bidding strategies based on:

- **Asset configuration** from the JSON config file (capacities, flexibility factors)
- **Historical patterns** assumptions (when assets are typically active)
- **Market data** assumptions (DSO willingness-to-pay ~9.5 CHF/MW average)
- **Activation rates** assumptions (5-30% depending on bid aggressiveness)

## Quick Start

```bash
# Basic usage - evaluate all strategies (default: last 30 days)
.venv/bin/python scripts/strategy_evaluator.py --config_file conf/test_fm01_aem.json

# Evaluate for a specific date range
.venv/bin/python scripts/strategy_evaluator.py --config_file conf/test_fm01_aem.json \
    --start_date 2025-12-01 --end_date 2025-12-31

# Evaluate a specific strategy with detailed output
.venv/bin/python scripts/strategy_evaluator.py --config_file conf/test_fm01_aem.json \
    --strategy strategy_4 --verbose

# Export results to JSON
.venv/bin/python scripts/strategy_evaluator.py --config_file conf/test_fm01_aem.json \
    --start_date 2025-12-01 --end_date 2025-12-31 --output data/strategy_results.json
```

## Command Line Arguments

| Argument | Required | Default | Description |
|----------|----------|---------|-------------|
| `--config_file` | Yes | - | Path to configuration file |
| `--start_date` | No | 30 days ago | Start date of evaluation (YYYY-MM-DD) |
| `--end_date` | No | Today | End date of evaluation (YYYY-MM-DD) |
| `--strategy` | No | all | Strategy to evaluate (`strategy_1` to `strategy_7` or `all`) |
| `--output` | No | - | Export results to JSON file |
| `--verbose` | No | False | Show detailed breakdown per time period |

## Defined Strategies

### Strategy 1: HP Only
- **Description:** Heat pumps only, conservative full-day coverage
- **Assets:** All 3 heat pumps (ECM96.2, ECM97.1, ECM97.2)
- **EV Chargers:** ❌ Not used
- **Approach:** Moderate bidding throughout the day

### Strategy 2: Full Portfolio + Evening EV
- **Description:** All assets, focus on evening when EVs might be charging
- **Assets:** All 3 HPs + 2 EVs
- **EV Chargers:** ✅ Used during evening peak (17:00-20:00)
- **Approach:** Conservative with EV focus in evening

### Strategy 3: Morning Peak Focus
- **Description:** Aggressive bidding during morning peak (Cinema HPs)
- **Assets:** Cinema HPs only (ECM97.1, ECM97.2)
- **EV Chargers:** ❌ Not used
- **Approach:** Maximize morning peak (06:30-09:00) with 30% activation rate

### Strategy 4: Hybrid (S3+S1) ⭐ RECOMMENDED
- **Description:** Morning aggressive (S3) + full day HP coverage (S1)
- **Assets:** All 3 heat pumps
- **EV Chargers:** ❌ Not used
- **Approach:** Best of both worlds - aggressive morning + broad coverage

### Strategy 5: Hybrid2 (S3+S2)
- **Description:** Morning aggressive (S3) + evening EV focus (S2)
- **Assets:** All 3 HPs + 2 EVs
- **EV Chargers:** ✅ Used during evening peak
- **Approach:** Aggressive morning + EV inclusion in evening

### Strategy 6: Smart Preheat
- **Description:** Preheat buildings 04:00-06:00 to guarantee 100% HP flexibility during morning peak 07:00-10:00
- **Assets:** All 3 heat pumps
- **EV Chargers:** ❌ Not used
- **Approach:** Exploit building thermal inertia for maximum morning peak flexibility

### Strategy 7: Double Pre-heating ⭐⭐ BEST PERFORMER
- **Description:** Dual preheat (04:00-06:00 + 12:00-15:00) for maximum flexibility during BOTH morning (07:00-11:00) and evening (16:00-20:00) peaks
- **Assets:** ECM96.2 and ECM97.3 (heat pumps)
- **EV Chargers:** ❌ Not used
- **Approach:** Data-driven strategy targeting the two highest-value periods identified from demand analysis

**Key advantages:**
- **Evening peak capture:** Data shows 16:00-20:00 has highest prices (up to 12.1 CHF/MW) AND highest demand (up to 130 kW)
- **Double flexibility windows:** 8 hours of premium flexibility per day vs 3-4 hours in other strategies
- **Afternoon lull exploitation:** Pre-heat 2 during 12:00-15:00 when demand/prices are lowest
- **140% better than Strategy 6:** 106.49 CHF vs 44.26 CHF over 30 days

**How it works:**
1. **04:00-06:00 (Pre-heat 1):** Force HP ON during off-peak (prepares for morning)
2. **06:00-07:00 (Transition 1):** HP winding down
3. **07:00-11:00 (Morning Flex):** HP OFF - full flexibility for morning peak
4. **11:00-12:00 (Transition 2):** HP recovery if needed
5. **12:00-15:00 (Pre-heat 2):** Force HP ON during afternoon lull (prepares for evening)
6. **15:00-16:00 (Transition 3):** HP winding down
7. **16:00-20:00 (Evening Flex):** HP OFF - full flexibility for evening peak (PREMIUM!)
8. **20:00-04:00 (Night Recovery):** Normal operation

**Data-driven design:**
Based on analysis of `data/csv/demand/` files:
- `price_by_hour_boxplot_15min.csv`: Evening peak prices 10-12 CHF/MW (highest)
- `demand_by_hour_boxplot_15min.csv`: Evening demand 105-130 kW (highest)
- `heatmap_quantity_kw.csv`: Thu/Fri evenings reach 600+ kW total demand

See [README_strategy_7_analysis.md](../docs/README_strategy_7_analysis.md) for detailed analysis.

**How it works:**
1. **04:00-06:00 (Preheat):** Force HP ON during off-peak hours (cheap electricity). No flexibility bids.
2. **06:00-07:00 (Transition):** HP winding down, building warm. Partial flexibility available.
3. **07:00-10:00 (Morning Peak):** HP OFF - 100% flexibility available! Building thermal mass maintains comfort.
4. **10:00-19:00 (Normal):** Standard operation with moderate flexibility.
5. **19:00-04:00 (Night):** Low flexibility, building cooling.

**Key advantages:**
- **100% reliability:** DSO gets guaranteed load reduction capability
- **Higher activation rate:** 50% vs 30% for conventional strategies
- **Premium pricing:** 10.5 CHF/MW vs 9.5 CHF/MW (DSO willingness-to-pay for reliability)
- **Lower activation cost:** HP already OFF, just maintain state
- **Electricity arbitrage:** Off-peak preheat (0.08 CHF/kWh) vs avoided peak (0.12 CHF/kWh)

**Integration with Energy Signature Analyzer:**
The `HeatingDemandEstimator` class in `energy_signature_analyzer.py` provides:
- Temperature-dependent preheat energy calculation
- Maximum safe HP-off duration estimation
- Seasonal flexibility factor adjustment

## Output Example

```
Configuration loaded from: conf/test_fm01_aem.json
Evaluation period: 2026-01-13 to 2026-02-11 (30 days)

======================================================================
STRATEGY COMPARISON
======================================================================
Strategy                            Net Profit      Slots     Avg/Slot     Uses EV   
------------------------------------------------------------------------------------------
Strategy 7: Double Pre-heating         106.49 CHF    543       0.196 CHF  No     ⭐ BEST
Strategy 6: Smart Preheat               44.26 CHF    418       0.106 CHF  No     
Strategy 4: Hybrid (S3+S1)              32.84 CHF    351       0.094 CHF  No     
Strategy 5: Hybrid2 (S3+S2)             28.42 CHF    363       0.078 CHF  Yes    
Strategy 1: HP Only                     25.30 CHF    321       0.079 CHF  No     
Strategy 3: Morning Peak Focus          19.75 CHF    211       0.094 CHF  No     
Strategy 2: Full Portfolio + Evening EV 16.11 CHF    306       0.053 CHF  Yes    
======================================================================

✅ RECOMMENDATION: Strategy 7: Double Pre-heating
   Period: 2026-01-13 to 2026-02-11 (30 days)
   Expected net profit: 106.49 CHF
```

## Calculation Methodology

### Revenue Formula
```
Revenue = flexibility_mw × bid_price_chf_mw × activated_slots
```

### Energy Curtailed
```
Energy (MWh) = flexibility_mw × 0.25h × activated_slots
```
(Each slot is 15 minutes = 0.25 hours)

### Activation Cost
```
Activation_cost = energy_mwh × activation_cost_per_mwh
```

Default activation costs:
- Heat Pumps: **2.5 CHF/MWh**
- EV Chargers: **4.5 CHF/MWh** (higher due to user inconvenience)

### Net Profit
```
Net_profit = Revenue - Activation_cost
```

## Key Assumptions

| Parameter | Value | Source |
|-----------|-------|--------|
| Morning peak HP flex | 0.020 MW | Analysis of Cinema HPs patterns |
| Average HP flex | 0.008-0.012 MW | Historical data analysis |
| EV flexibility | 0.0008 MW | Very low due to 5-10% occupancy |
| Peak bid price | 9.0-9.5 CHF/MW | DSO market data (~9.5 CHF/MW avg) |
| Off-peak bid price | 5.5-6.0 CHF/MW | Conservative estimate |
| Peak activation rate | 25-30% | Aggressive bidding assumption |
| Off-peak activation rate | 5% | Conservative assumption |

## JSON Output Format

When using `--output`, the script generates a JSON file with this structure:

```json
{
  "evaluation_date": "2026-01-08T15:30:00",
  "evaluation_period": {
    "start_date": "2025-12-01",
    "end_date": "2025-12-31",
    "days": 31
  },
  "asset_summary": {
    "heat_pumps": {
      "count": 3,
      "total_capacity_mw": 0.034,
      "total_max_flex_mw": 0.0289,
      "assets": {...}
    },
    "ev_chargers": {...}
  },
  "strategies": {
    "strategy_1": {
      "name": "Strategy 1: HP Only",
      "description": "...",
      "results": {
        "net_profit_chf": 22.28,
        "total_revenue_chf": 24.50,
        "total_activation_cost_chf": 2.22,
        ...
      },
      "period_breakdown": [...]
    },
    ...
  }
}
```

## Extending the Script

### Adding a New Strategy

Edit the `_define_strategies()` method in `strategy_evaluator.py`:

```python
strategies["strategy_6"] = Strategy(
    name="Strategy 6: Custom",
    description="Your custom strategy description",
    assets_included=["ECM97.1", "ECM97.2"],
    uses_ev=False,
    time_slots=[
        TimeSlot(
            name="Custom Period",
            start="08:00", end="12:00",
            slots_per_day=16,
            flexibility_mw=0.015,
            bid_price_chf_mw=8.0,
            activation_rate=0.20,
            activation_cost_chf_mwh=2.5
        ),
        # ... more time slots
    ]
)
```

### Modifying Assumptions

Key parameters to adjust based on your market experience:
- `activation_rate`: Increase if your bids are frequently accepted
- `bid_price_chf_mw`: Adjust based on DSO willingness-to-pay
- `flexibility_mw`: Update based on actual asset performance
- `activation_cost_chf_mwh`: Adjust based on actual costs

## Related Scripts

- `ev_charger_analysis.py` - Analyze EV charger patterns (input for flexibility estimates)
- `heat_pump_analysis.py` - Analyze heat pump patterns (input for flexibility estimates)
- `market_results_fetcher.py` - Fetch DSO market data (input for price estimates)
- `trader_fsp.py` - Actual trading script (to be updated with strategy selection)

## Notes

⚠️ **Important:** This script provides **estimates** based on assumptions. Actual market performance may vary due to:
- Real-time asset availability
- DSO demand fluctuations
- Market competition from other FSPs
- Weather conditions affecting heat pump usage

