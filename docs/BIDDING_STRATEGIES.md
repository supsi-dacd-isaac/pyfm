# Bidding Strategies for Flexibility Market

This document explains the five bidding strategies available for the FSP (Flexibility Service Provider) to participate in the flexibility market.

## Overview

The flexibility market allows FSPs to sell load reduction capabilities to the DSO (Distribution System Operator). The FSP portfolio includes:

| Asset | Type | Capacity | Description |
|-------|------|----------|-------------|
| ECM96.2 | Heat Pump | 4 kW | HP Small |
| ECM97.1 | Heat Pump | 15 kW | HP Cinema 1 |
| ECM97.2 | Heat Pump | 15 kW | HP Cinema 2 |
| ECM63.1 | EV Charger | 11 kW | EV Charger 1 |
| ECM63.2 | EV Charger | 11 kW | EV Charger 2 |

**Total capacity:** 56 kW (34 kW heat pumps + 22 kW EV chargers)

### Key Concepts

- **Flexibility:** The amount of load that can be reduced (curtailed) on request
- **Downward flexibility:** Reducing consumption (what the DSO typically needs during peak hours)
- **Bid price:** Minimum price the FSP accepts to provide flexibility (CHF/MW)
- **Activation cost:** Cost incurred when flexibility is actually activated

---

## Strategy 1: HP Only

### Description
A conservative strategy that only uses heat pumps, providing full-day coverage with moderate bidding.

### Rationale
- Heat pumps have predictable consumption patterns
- High availability throughout the day
- No dependency on EV charger occupancy (which is typically low: 5-10%)

### Assets Used
- ✅ ECM96.2 (HP Small)
- ✅ ECM97.1 (HP Cinema 1)
- ✅ ECM97.2 (HP Cinema 2)
- ❌ EV Chargers excluded

### Time Schedule

```
06:30 ─────────────────────────────────────────────────────────────── 06:30
   │                                                                     │
   │  MORNING PEAK      LATE MORNING     AFTERNOON      EVENING PEAK    OFF-PEAK
   │  06:30-09:00       09:00-12:00      12:00-16:00    16:00-19:00     19:00-06:30
   │  ────────────      ───────────      ───────────    ───────────     ───────────
   │  15 kW @ 9.0       12 kW @ 8.5      8 kW @ 7.0     10 kW @ 9.5     6 kW @ 6.0
   │  CHF/MW            CHF/MW           CHF/MW         CHF/MW          CHF/MW
```

| Time Slot | Hours | Flexibility | Min Price | Characteristics |
|-----------|-------|-------------|-----------|-----------------|
| Morning Peak | 06:30-09:00 | 15 kW | 9.0 CHF/MW | High HP activity |
| Late Morning | 09:00-12:00 | 12 kW | 8.5 CHF/MW | Moderate activity |
| Afternoon | 12:00-16:00 | 8 kW | 7.0 CHF/MW | Lower activity |
| Evening Peak | 16:00-19:00 | 10 kW | 9.5 CHF/MW | Second peak |
| Off-peak | 19:00-06:30 | 6 kW | 6.0 CHF/MW | Minimal activity |

### When to Use
- Default conservative approach
- When EV charger availability is unreliable
- For consistent, predictable market participation

---

## Strategy 2: Full Portfolio + Evening EV Focus

### Description
Uses all assets (heat pumps + EV chargers) with special focus on evening hours when EVs are more likely to be charging.

### Rationale
- Maximizes portfolio utilization
- Evening hours (17:00-20:00) show higher EV charger occupancy
- Provides additional flexibility during evening peak demand

### Assets Used
- ✅ All heat pumps (ECM96.2, ECM97.1, ECM97.2)
- ✅ All EV chargers (ECM63.1, ECM63.2) - evening only

### Time Schedule

| Time Slot | Hours | HP Flexibility | EV Flexibility | Min Price |
|-----------|-------|----------------|----------------|-----------|
| Morning Peak | 07:00-10:00 | 12 kW | — | 8.0 CHF/MW |
| Midday | 10:00-17:00 | 6 kW | — | 6.0 CHF/MW |
| **Evening Peak** | 17:00-20:00 | 10 kW | **0.8 kW** | 9.5 / 10.0 CHF/MW |
| Off-peak | 20:00-07:00 | 5 kW | — | 5.5 CHF/MW |

### Evening EV Bidding
During 17:00-20:00:
- HP flexibility: 10 kW @ 9.5 CHF/MW
- EV flexibility: 0.8 kW @ 10.0 CHF/MW (higher price due to user impact)

### When to Use
- When you want to test EV charger participation
- During periods of higher expected EV usage
- To maximize total portfolio revenue

---

## Strategy 3: Morning Peak Focus

### Description
An aggressive strategy that concentrates bidding during the morning peak when Cinema heat pumps are at maximum consumption.

### Rationale
- Cinema HPs (ECM97.1, ECM97.2) show highest consumption during 06:30-09:00
- Combined capacity of 30 kW (2 × 15 kW)
- Morning peak is when DSO often has highest flexibility demand

### Assets Used
- ❌ ECM96.2 (HP Small) excluded
- ✅ ECM97.1 (HP Cinema 1)
- ✅ ECM97.2 (HP Cinema 2)
- ❌ EV Chargers excluded

### Time Schedule

```
                    AGGRESSIVE BIDDING
                    ┌─────────────────┐
                    │  06:30 - 09:00  │
                    │   20 kW @ 9.0   │
                    │    CHF/MW       │
                    └─────────────────┘
                           │
    ───────────────────────┼────────────────────────────
    │                      │                           │
    │    LATE MORNING      │     AFTERNOON/EVENING     │     NIGHT
    │    09:00-12:00       │     12:00-20:00           │     20:00-06:30
    │    10 kW @ 7.5       │     6 kW @ 6.0            │     4 kW @ 5.0
```

| Time Slot | Hours | Flexibility | Min Price | Strategy |
|-----------|-------|-------------|-----------|----------|
| **Morning Peak** | 06:30-09:00 | **20 kW** | 9.0 CHF/MW | **Aggressive** |
| Late Morning | 09:00-12:00 | 10 kW | 7.5 CHF/MW | Conservative |
| Afternoon/Evening | 12:00-20:00 | 6 kW | 6.0 CHF/MW | Minimal |
| Night | 20:00-06:30 | 4 kW | 5.0 CHF/MW | Minimal |

### When to Use
- When morning peak prices are highest
- To concentrate effort on high-value periods
- When Cinema HPs have reliable morning consumption

---

## Strategy 4: Hybrid (S3+S1) ⭐ RECOMMENDED

### Description
Combines the aggressive morning approach of Strategy 3 with the full-day HP coverage of Strategy 1. This is the **recommended default strategy**.

### Rationale
- Captures high-value morning peak with aggressive bidding (like S3)
- Maintains market presence throughout the day (like S1)
- Best overall profitability based on historical analysis

### Assets Used
- ✅ All heat pumps (ECM96.2, ECM97.1, ECM97.2)
- ❌ EV Chargers excluded

### Time Schedule

```
    MORNING: Strategy 3 approach          REST OF DAY: Strategy 1 approach
    ┌─────────────────────────┐          ┌─────────────────────────────────┐
    │      AGGRESSIVE         │          │         CONSERVATIVE            │
    │    06:30 - 09:00        │          │       09:00 - 06:30             │
    │    20 kW @ 9.0          │          │    Variable pricing             │
    └─────────────────────────┘          └─────────────────────────────────┘
```

| Time Slot | Hours | Flexibility | Min Price | Approach |
|-----------|-------|-------------|-----------|----------|
| **Morning Peak** | 06:30-09:00 | **20 kW** | **9.0 CHF/MW** | **Aggressive (S3)** |
| Late Morning | 09:00-12:00 | 12 kW | 8.5 CHF/MW | Conservative (S1) |
| Afternoon | 12:00-16:00 | 8 kW | 7.0 CHF/MW | Conservative (S1) |
| Evening Peak | 16:00-19:00 | 10 kW | 9.5 CHF/MW | Conservative (S1) |
| Off-peak | 19:00-06:30 | 6 kW | 6.0 CHF/MW | Conservative (S1) |

### Expected Performance (30-day estimate)
- Total slots activated: ~351
- Total revenue: ~35.43 CHF
- Activation costs: ~2.59 CHF
- **Net profit: ~32.84 CHF**

### When to Use
- **Default recommended strategy**
- For maximum profitability
- When you want balanced risk/reward

---

## Strategy 5: Hybrid2 (S3+S2)

### Description
Combines the aggressive morning approach of Strategy 3 with Strategy 2's full-day coverage including EV chargers in the evening.

### Rationale
- Aggressive morning bidding (like S3)
- Includes EV chargers during evening peak (like S2)
- Tests EV participation while maintaining strong morning presence

### Assets Used
- ✅ All heat pumps (ECM96.2, ECM97.1, ECM97.2)
- ✅ EV chargers (ECM63.1, ECM63.2) - evening only

### Time Schedule

| Time Slot | Hours | HP Flexibility | EV Flexibility | Min Price |
|-----------|-------|----------------|----------------|-----------|
| **Morning Peak** | 06:30-09:00 | **20 kW** | — | **9.0 CHF/MW** |
| Late Morning | 09:00-12:00 | 10 kW | — | 7.5 CHF/MW |
| Afternoon | 12:00-17:00 | 6 kW | — | 6.0 CHF/MW |
| **Evening Peak** | 17:00-20:00 | 10 kW | **0.8 kW** | 9.5 / 10.0 CHF/MW |
| Off-peak | 20:00-06:30 | 5 kW | — | 5.5 CHF/MW |

### Expected Performance (30-day estimate)
- Total HP slots activated: ~309
- Total EV slots activated: ~54
- **Net profit: ~28.42 CHF**

### When to Use
- When you want to include EV chargers
- To test combined HP + EV approach
- When evening EV occupancy is expected to be higher

---

## Strategy Comparison

| Metric | S1 (HP Only) | S2 (Full+EV) | S3 (Morning) | **S4 (Hybrid)** | S5 (Hybrid2) |
|--------|--------------|--------------|--------------|-----------------|--------------|
| **Est. Net Profit** | 22.28 CHF | 17.23 CHF | 20.62 CHF | **32.84 CHF** ⭐ | 28.42 CHF |
| Uses EV Chargers | ❌ | ✅ | ❌ | ❌ | ✅ |
| Morning Aggressive | ❌ | ❌ | ✅ | ✅ | ✅ |
| Full Day Coverage | ✅ | ✅ | ❌ | ✅ | ✅ |
| Complexity | Low | Medium | Medium | Medium | Medium |

---

## How Strategies Work

### 1. Asset Filtering
Each strategy defines which assets can participate:
- Heat pumps only (S1, S3, S4)
- All assets (S2, S5)
- Specific assets (S3 uses only Cinema HPs)

### 2. Time-Based Pricing
Each strategy defines minimum acceptable prices for different time periods:
```
if DSO_price >= strategy_min_price:
    place_order()
else:
    skip_order()  # Price too low
```

### 3. Flexibility Quantity
The bid quantity is the minimum of:
- Strategy's configured flexibility for the time slot
- Actual available flexibility from assets

```
bid_quantity = min(strategy_config_flex, actual_available_flex)
```

### 4. Price Acceptance
Orders are only placed when the DSO's offered price meets the strategy's minimum:
- Morning peak: Higher minimum prices (9.0-9.5 CHF/MW)
- Off-peak: Lower minimum prices (5.0-6.0 CHF/MW)

---

## Configuration

Strategies are configured in `conf/test_fm01_aem.json`:

```json
"bidding_strategies": {
  "strategy_4": {
    "name": "Hybrid (S3+S1)",
    "description": "Morning peak aggressive + full day HP coverage",
    "asset_types": ["heat_pump"],
    "time_slots": [
      {"name": "Morning Peak", "start": "06:30", "end": "09:00", 
       "flexibility_mw": 0.020, "bid_price": 9.0, "activation_cost": 2.5},
      ...
    ]
  }
}
```

### FSP Configuration
Each FSP can have a default strategy:
```json
"supsi01": {
  "strategy": "strategy_4",
  ...
}
```

---

## Usage

### List Available Strategies
```bash
python scripts/trader_fsp.py --config_file conf/test_fm01_aem.json \
    --fsp supsi01 --list-strategies
```

### Run with Specific Strategy
```bash
python scripts/trader_fsp.py --config_file conf/test_fm01_aem.json \
    --fsp supsi01 --strategy strategy_4 --dry-run
```

### Run with FSP's Default Strategy
```bash
python scripts/trader_fsp.py --config_file conf/test_fm01_aem.json \
    --fsp supsi01 --dry-run
```

### Evaluate Strategy Performance
```bash
python scripts/strategy_evaluator.py --config_file conf/test_fm01_aem.json \
    --start_date 2025-12-01 --end_date 2025-12-31
```

---

## Recommendations

1. **Start with Strategy 4** - Best overall performance
2. **Use Strategy 1** if you want simplicity without aggressive morning bidding
3. **Use Strategy 5** if you want to include EV chargers
4. **Avoid Strategy 2** unless specifically testing EV participation (lowest profitability)
5. **Use Strategy 3** for morning-only focused trading

---

## Glossary

| Term | Definition |
|------|------------|
| **FSP** | Flexibility Service Provider - sells flexibility to DSO |
| **DSO** | Distribution System Operator - buys flexibility |
| **Flexibility** | Load that can be reduced on demand |
| **Bid Price** | Minimum price FSP accepts (CHF/MW) |
| **Activation Cost** | Cost of actually reducing load (CHF/MWh) |
| **Time Slot** | 15-minute market period |
| **Peak Hours** | High-demand periods (morning 06:30-10:00, evening 16:00-20:00) |

