# Bidding Strategies for Flexibility Market

This document explains the bidding strategies configured for the FSP (Flexibility Service Provider) to participate in the flexibility market. It reflects the current `conf/test_fm01_aem.json` configuration, including strategies `strategy_1` through `strategy_9`.

## Overview

The flexibility market allows FSPs to sell load reduction capabilities to the DSO (Distribution System Operator). The FSP portfolio includes:

| Asset | Type | Capacity | Description |
|-------|------|----------|-------------|
| ECM96.2 | Heat Pump | 4 kW config capacity / 7.5 kW persistence nominal | HP Small |
| ECM97.1 | Heat Pump | 15 kW | HP Cinema 1 |
| ECM97.2 | Heat Pump | 15 kW | HP Cinema 2 |
| ECM97.3 | Heat Pump | 30 kW | HP Cinema aggregate |
| ECM63.1 | EV Charger | 11 kW | EV Charger 1 |
| ECM63.2 | EV Charger | 11 kW | EV Charger 2 |

The active FSP portfolio in `conf/test_fm01_aem.json` currently lists `ECM96.2`, `ECM97.3`, `ECM63.1`, and `ECM63.2`. Some strategies refer to all assets by type, so the final allowed set is the intersection of the strategy filter, asset mapping, and active FSP portfolio.

### Key Concepts

- **Flexibility:** The amount of load that can be reduced (curtailed) on request
- **Downward flexibility:** Reducing consumption (what the DSO typically needs during peak hours)
- **Bid price:** Minimum price the FSP accepts to provide flexibility (CHF/MW)
- **Activation cost:** Cost incurred when flexibility is actually activated

### Strategy-Scoped Flexibility Method

In strategy mode, flexibility method selection is strategy-scoped:

- Strategies without `flexibility_method: "persistence"` default to the historical/legacy flexibility path.
- A strategy uses persistence only when it explicitly sets `flexibility_method: "persistence"`.
- This prevents new persistence strategies from changing existing historical strategies.

Baseline persistence and bidding strategy persistence are separate concepts:

- `baseline.dbSettings.strategy = "slot_persistence"` affects baseline generation/uploading.
- `flexibility_method: "persistence"` inside a bidding strategy affects trader bidding flexibility for the selected strategy.

In the current configuration, `strategy_8` and `strategy_9` are the persistence bidding strategies. `strategy_4` is historical/legacy.

## Current Strategy Summary

| Strategy | Name | Asset Types | Allowed Assets / Filter | Flexibility Method | Typical Use | Notes |
|----------|------|-------------|-------------------------|--------------------|-------------|-------|
| `strategy_1` | HP Only | Heat pumps | All HP assets in the active portfolio | Historical/legacy | Conservative HP-only full-day bidding | No `flexibility_method` override. |
| `strategy_2` | Full Portfolio + Evening EV | HP + EV | All HP and EV assets in the active portfolio | Historical/legacy | Full portfolio with configured evening EV target | EV contribution is configured through `ev_flexibility_mw` in the evening slot. |
| `strategy_3` | Morning Peak Focus | Heat pumps | `ECM97.3` | Historical/legacy | Morning peak focus | Current config filters to the Cinema HP aggregate. |
| `strategy_4` | Hybrid (S3+S1) | Heat pumps | `ECM96.2`, `ECM97.3` | Historical/legacy | Recommended HP hybrid | Explicitly not persistence. |
| `strategy_5` | Hybrid2 (S3+S2) | HP + EV | All HP and EV assets in the active portfolio | Historical/legacy | Hybrid with evening EV participation | No `flexibility_method` override. |
| `strategy_6` | Smart Preheat | Heat pumps | All HP assets in the active portfolio | Historical/legacy | 04:00-06:00 preheat, morning peak flexibility | Intent is taken from config preheat fields. |
| `strategy_7` | Double Pre-heating | Heat pumps | `ECM96.2` | Historical/legacy | Morning and evening preheat/peak cycle | Intent is taken from config preheat fields. |
| `strategy_8` | Persistence HP Strategy | Heat pumps | `ECM96.2`, `ECM97.3` | Persistence | Safer HP-only persistence bidding | Excludes EV chargers. |
| `strategy_9` | Persistence HP + EV Strategy | HP + EV | `ECM63.1`, `ECM63.2`, `ECM96.2`, `ECM97.3` | Persistence | Persistence bidding including EV chargers | Monitor EV telemetry carefully. |

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
- ✅ ECM97.3 (HP Cinema aggregate)
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
- ✅ Heat pumps in the active portfolio (currently `ECM96.2`, `ECM97.3`)
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
- Cinema HP aggregate `ECM97.3` is the configured focus asset
- Configured aggregate capacity is 30 kW
- Morning peak is when DSO often has highest flexibility demand

### Assets Used
- ❌ ECM96.2 (HP Small) excluded
- ✅ ECM97.3 (HP Cinema aggregate)
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
Combines aggressive peak targets with full-day HP coverage. This is the configured default strategy for `supsi01`.

**Flexibility method:** historical/legacy. `strategy_4` does not set `flexibility_method: "persistence"`.

### Rationale
- Captures high-value morning peak with aggressive bidding (like S3)
- Maintains market presence throughout the day (like S1)
- Best overall profitability based on historical analysis

### Assets Used
- ✅ ECM96.2 (HP Small)
- ✅ ECM97.3 (HP Cinema aggregate)
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
| **Morning Peak** | 06:30-09:00 | **40 kW** | **9.0 CHF/MW** | **Aggressive (S3)** |
| Late Morning | 09:00-12:00 | 12 kW | 8.5 CHF/MW | Conservative (S1) |
| Afternoon | 12:00-16:00 | 8 kW | 7.0 CHF/MW | Conservative (S1) |
| Evening Peak | 16:00-19:00 | 40 kW | 9.5 CHF/MW | Aggressive |
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
- When you want the restored historical/legacy path, not persistence

---

## Strategy 5: Hybrid2 (S3+S2)

### Description
Combines the aggressive morning approach of Strategy 3 with Strategy 2's full-day coverage including EV chargers in the evening.

### Rationale
- Aggressive morning bidding (like S3)
- Includes EV chargers during evening peak (like S2)
- Tests EV participation while maintaining strong morning presence

### Assets Used
- ✅ Heat pumps in the active portfolio (currently `ECM96.2`, `ECM97.3`)
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

## Strategy 6: Smart Preheat

### Description
Configured as a heat-pump strategy that preheats from 04:00-06:00 to increase or guarantee flexibility during the 07:00-10:00 morning peak.

**Flexibility method:** historical/legacy. `strategy_6` does not set `flexibility_method: "persistence"`.

### Assets Used
- ✅ Heat-pump assets in the active portfolio
- ❌ EV chargers excluded

### Time Schedule

| Time Slot | Hours | Flexibility | Min Price | Notes |
|-----------|-------|-------------|-----------|-------|
| Preheat (HP ON) | 04:00-06:00 | 0 kW | 0.0 CHF/MW | `is_preheat_period: true`, `preheat_power_mw: 0.020` |
| Transition | 06:00-07:00 | 10 kW | 8.0 CHF/MW | Ramp from preheat to peak |
| Morning Peak (HP OFF - Full Flex) | 07:00-10:00 | 28 kW | 10.5 CHF/MW | Main operational target |
| Post-Peak Recovery | 10:00-12:00 | 10 kW | 7.5 CHF/MW | Recovery period |
| Afternoon | 12:00-16:00 | 8 kW | 7.0 CHF/MW | Lower target |
| Evening Peak | 16:00-19:00 | 12 kW | 9.5 CHF/MW | Evening target |
| Night | 19:00-04:00 | 5 kW | 5.5 CHF/MW | Overnight |

### When to Use
- When validating the configured preheat concept for morning peak availability
- When the operator wants HP-only historical/legacy bidding with a preheat-shaped schedule

The intent above is inferred from the strategy name, description, time-slot names, and preheat fields in `conf/test_fm01_aem.json`.

---

## Strategy 7: Double Pre-heating

### Description
Configured as a single-asset HP strategy with preheat windows before both morning and evening peak flexibility periods.

**Flexibility method:** historical/legacy. `strategy_7` does not set `flexibility_method: "persistence"`.

### Assets Used
- ✅ ECM96.2 (HP Small)
- ❌ Other HP assets excluded by `assets_filter`
- ❌ EV chargers excluded

### Time Schedule

| Time Slot | Hours | Flexibility | Min Price | Notes |
|-----------|-------|-------------|-----------|-------|
| Pre-heat 1 | 04:00-06:00 | 0 kW | 0.0 CHF/MW | `is_preheat_period: true`, `preheat_power_mw: 0.018` |
| Transition 1 | 06:00-07:00 | 8 kW | 8.0 CHF/MW | Transition to morning peak |
| Morning Peak Flex | 07:00-11:00 | 25 kW | 10.0 CHF/MW | Morning target |
| Transition 2 | 11:00-12:00 | 6 kW | 7.5 CHF/MW | Between peak and second preheat |
| Pre-heat 2 | 12:00-15:00 | 0 kW | 0.0 CHF/MW | `is_preheat_period: true`, `preheat_power_mw: 0.018` |
| Transition 3 | 15:00-16:00 | 10 kW | 8.5 CHF/MW | Transition to evening peak |
| Evening Peak Flex | 16:00-20:00 | 28 kW | 11.5 CHF/MW | Evening target |
| Night Recovery | 20:00-04:00 | 4 kW | 5.5 CHF/MW | Overnight recovery |

### When to Use
- When testing the double-preheat schedule configured for `ECM96.2`
- When focusing on the small HP asset rather than the wider HP portfolio

The intent above is inferred from the strategy name, description, time-slot names, and preheat fields in `conf/test_fm01_aem.json`.

---

## Strategy 8: Persistence HP Strategy

### Description
HP-only persistence strategy for `ECM96.2` and `ECM97.3`.

**Flexibility method:** persistence via `flexibility_method: "persistence"`.

### Assets Used
- ✅ ECM96.2 (HP Small)
- ✅ ECM97.3 (HP Cinema aggregate)
- ❌ EV chargers excluded

### Persistence Behaviour
- Uses lagged measured power as the baseline source for the target slot.
- Applies the current-state gate: if current measured power is at or below `activeThresholdW`, the asset contributes 0 flexibility.
- If the asset is active, available flexibility is based on lagged measured baseline, nominal cap, and safety factor.
- Missing or stale measurements skip the affected asset under the current `missingMeasurementPolicy: "skip_asset"`.

### When to Use
- Safer persistence validation because EV chargers are excluded
- HP-only operational bidding where current HP state should gate the bid

---

## Strategy 9: Persistence HP + EV Strategy

### Description
Persistence strategy including both HPs and EV chargers.

**Flexibility method:** persistence via `flexibility_method: "persistence"`.

### Assets Used
- ✅ ECM63.1 (EV Charger 1)
- ✅ ECM63.2 (EV Charger 2)
- ✅ ECM96.2 (HP Small)
- ✅ ECM97.3 (HP Cinema aggregate)

### Persistence Behaviour
- Uses the same current-state gate as `strategy_8`.
- Missing or unavailable asset data must not increase the bid. In the current implementation, missing/problematic source or current measurements cause that asset to contribute nothing.
- This makes the strategy robust but possibly conservative.

### When to Use
- When testing persistence bidding across both HP and EV assets
- When EV telemetry has been validated for the target slot

### Caution
EV telemetry has been observed to be more problematic than HP telemetry. Monitor `ECM63.1` and `ECM63.2` carefully before enabling real bidding.

---

## Strategy Comparison

| Metric | S1 | S2 | S3 | S4 | S5 | S6 | S7 | S8 | S9 |
|--------|----|----|----|----|----|----|----|----|----|
| Flexibility method | Historical | Historical | Historical | Historical | Historical | Historical | Historical | Persistence | Persistence |
| Uses EV chargers | No | Yes | No | No | Yes | No | No | No | Yes |
| Explicit asset filter | No | No | `ECM97.3` | `ECM96.2`, `ECM97.3` | No | No | `ECM96.2` | `ECM96.2`, `ECM97.3` | `ECM63.1`, `ECM63.2`, `ECM96.2`, `ECM97.3` |
| Preheat fields | No | No | No | No | No | Yes | Yes | No | No |
| Persistence current-state gate | No | No | No | No | No | No | No | Yes | Yes |

---

## How Strategies Work

### 1. Asset Filtering
Each strategy defines which assets can participate:
- Heat pumps only: `strategy_1`, `strategy_3`, `strategy_4`, `strategy_6`, `strategy_7`, `strategy_8`
- Heat pumps plus EV chargers: `strategy_2`, `strategy_5`, `strategy_9`
- Specific asset filters: `strategy_3`, `strategy_4`, `strategy_7`, `strategy_8`, `strategy_9`

### 2. Time-Based Pricing
Each strategy defines minimum acceptable prices for different time periods:
```
if DSO_price >= strategy_min_price:
    place_order()
else:
    skip_order()  # Price too low
```

### 3. Flexibility Quantity
The operational bid quantity is the recommended bid after applying:

- the selected strategy asset filter;
- the strategy's configured target for the time slot;
- available flexibility from the relevant assets;
- achievable-flexibility logic for discrete and continuous assets.

In strategy mode, logs distinguish portfolio availability from strategy availability. Portfolio availability before strategy filtering is diagnostic only; `STRATEGY AVAILABLE FLEXIBILITY` and `RECOMMENDED BID` are the values to use for operator validation.

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
    "assets_filter": ["ECM96.2", "ECM97.3"],
    "time_slots": [
      {"name": "Morning Peak", "start": "06:30", "end": "09:00", 
       "flexibility_mw": 0.040, "bid_price": 9.0, "activation_cost": 2.5},
      ...
    ]
  }
}
```

Persistence strategies include an explicit method flag:

```json
"strategy_8": {
  "name": "Persistence HP Strategy",
  "asset_types": ["heat_pump"],
  "assets_filter": ["ECM96.2", "ECM97.3"],
  "flexibility_method": "persistence"
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
    --fsp supsi01 --strategy strategy_8 --dry-run
```

From the `scripts/` directory:

```bash
python3.10 trader_fsp.py --config_file ../conf/test_fm01_aem.json \
    --fsp supsi01 --strategy strategy_9 --dry-run
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

1. **Use `strategy_4`** when you want the configured recommended historical/legacy HP hybrid.
2. **Use `strategy_8`** when validating persistence bidding with the safer HP-only asset set.
3. **Use `strategy_9`** only after validating EV telemetry quality, because it includes EV chargers.
4. **Use `strategy_6` or `strategy_7`** when specifically testing the configured preheat schedules.
5. **Use dry-run first** before enabling live bidding for any strategy.

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
| **Portfolio Available Flexibility** | Diagnostic availability across assigned portfolio assets before strategy filtering |
| **Strategy Available Flexibility** | Availability after applying the selected strategy asset filter |
| **Recommended Bid** | Quantity used for bidding after strategy filtering and achievable-flexibility logic |
