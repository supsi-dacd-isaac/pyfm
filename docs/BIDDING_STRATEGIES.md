# Bidding Strategies for Flexibility Market

This document explains all 12 bidding strategies configured for the FSPs (Flexibility Service Providers) in `conf/test_fm01_aem.json`.

## Overview

The flexibility market allows FSPs to sell load reduction capabilities to the DSO (Distribution System Operator). The FSP portfolio includes:

| Asset | Type | Capacity | Description |
|-------|------|----------|-------------|
| ECM96.2 | Heat Pump | 4 kW config capacity / 7.5 kW persistence nominal | HP Small |
| ECM97.1 | Heat Pump | 15 kW | HP Cinema 1 |
| ECM97.2 | Heat Pump | 15 kW | HP Cinema 2 |
| ECM97.3 | Heat Pump | 30 kW | HP Cinema aggregate |
| ECM63.1 | EV Charger | 11 kW | EV Charger 1 — 7 discrete OCPP states (6.93–11.0 kW) |
| ECM63.2 | EV Charger | 11 kW | EV Charger 2 — 7 discrete OCPP states (6.93–11.0 kW) |
| ECM62.10 | Simulated Heat Pump | 36.0 kW | Strategy 12 binary HP |
| ECM68.3 | Simulated Heat Pump | 8.4 kW | Strategy 12 binary HP |
| ECM162.1 | Simulated Heat Pump | 6.0 kW | Strategy 12 binary HP |

The configuration has two active FSP portfolios:

- `supsi01` uses `strategy_11` by default and lists `ECM96.2`, `ECM97.3`, `ECM63.1`, and `ECM63.2`.
- `supsi02` uses `strategy_12` by default and lists `ECM62.10`, `ECM68.3`, and `ECM162.1`.

Some strategies refer to all assets by type, so the final allowed set is the intersection of the strategy filter, asset mapping, and the selected FSP portfolio. Treat `ECM62.10` as one independent asset; do not substitute, aggregate, or fan out to `ECM62.1`, `ECM62.2`, or `ECM62.3`.

### Key Concepts

- **Flexibility:** The amount of load that can be reduced (curtailed) on request
- **Downward flexibility:** Reducing consumption (what the DSO typically needs during peak hours)
- **Bid price:** Minimum price the FSP accepts to provide flexibility (CHF/MW)
- **Activation cost:** Cost incurred when flexibility is actually activated

### Strategy-Scoped Flexibility Method

In strategy mode, flexibility method selection is strategy-scoped:

- Strategies without an explicit `flexibility_method` default to the historical/legacy flexibility path.
- A strategy uses persistence only when it explicitly sets `flexibility_method: "persistence"`.
- A strategy uses recent-profile forecasting only when it explicitly sets `flexibility_method: "recent_profile"`.
- A strategy uses preconditioned binary forecasting only when it explicitly sets `flexibility_method: "preconditioned_binary"`.
- This prevents new persistence, recent-profile, or preconditioned-binary strategies from changing existing historical strategies.

Baseline persistence and bidding strategy persistence are separate concepts:

- `baseline.dbSettings.strategy = "slot_persistence"` affects baseline generation/uploading.
- `flexibility_method: "persistence"` inside a bidding strategy affects trader bidding flexibility for the selected strategy.

In the current configuration, `strategy_8` and `strategy_9` are the persistence bidding strategies, while `strategy_10` and `strategy_11` use recent-profile forecasting. Both configured EV assets are discrete OCPP-controlled assets, so both recent-profile strategies operate on discrete EV state mappings; Strategy 11 adds explicit discrete-EV policy settings. `strategy_12` is the simulated heat-pump preconditioned-binary strategy. `strategy_4` remains the recommended historical/legacy alternative, but it is not the configured default for either FSP.

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
| `strategy_9` | Persistence HP + EV Strategy | HP + EV | `ECM63.1`, `ECM63.2`, `ECM96.2`, `ECM97.3` | Persistence | Persistence bidding including discrete EV chargers | Monitor EV telemetry carefully. |
| `strategy_10` | Recent-profile Short-term Flexibility (EV) | EV (HP-capable) | `ECM63.1`, `ECM63.2` | Recent profile | EV-only short-term profile bidding using the current discrete mappings | Historical continuous-EV approximation; current mappings make it discrete. |
| `strategy_11` | Recent-profile discrete EV current-step flexibility | EV | `ECM63.1`, `ECM63.2` | Recent profile | Discrete OCPP current-step EV bidding | Maps flexibility to feasible OCPP power states; overdelivery-tolerant. |
| `strategy_12` | Preconditioned Binary HP Flexibility | Simulated HP | `ECM62.10`, `ECM68.3`, `ECM162.1` | Preconditioned binary | Scheduled preparation and binary HP delivery | Lifecycle enabled; weather gate currently disabled. |

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
   │  15 kW @ 9.0       12 kW @ 8.5      8 kW @ 7.0     15 kW @ 9.5     6 kW @ 6.0
   │  CHF/MW            CHF/MW           CHF/MW         CHF/MW          CHF/MW
```

| Time Slot | Hours | Flexibility | Min Price | Characteristics |
|-----------|-------|-------------|-----------|-----------------|
| Morning Peak | 06:30-09:00 | 15 kW | 9.0 CHF/MW | High HP activity |
| Late Morning | 09:00-12:00 | 12 kW | 8.5 CHF/MW | Moderate activity |
| Afternoon | 12:00-16:00 | 8 kW | 7.0 CHF/MW | Lower activity |
| Evening Peak | 16:00-19:00 | 15 kW | 9.5 CHF/MW | Second peak |
| Off-peak | 19:00-06:30 | 6 kW | 6.0 CHF/MW | Minimal activity |

### When to Use
- Conservative full-day option
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
- ✅ All EV chargers (`ECM63.1`, `ECM63.2`) are strategy-eligible in every slot

The evening slot is the only slot with an additional `ev_flexibility_mw` target and EV-specific economic fields. These fields do not act as a time-scoped asset filter.

### Time Schedule

| Time Slot | Hours | Base Target | EV Add-on | Main Min Price |
|-----------|-------|-------------|-----------|----------------|
| Morning Peak | 07:00-10:00 | 15 kW | — | 8.0 CHF/MW |
| Midday | 10:00-17:00 | 6 kW | — | 6.0 CHF/MW |
| **Evening Peak** | 17:00-20:00 | 15 kW | **0.8 kW** | 9.5 CHF/MW |
| Off-peak | 20:00-07:00 | 5 kW | — | 5.5 CHF/MW |

### Evening EV Configuration
During 17:00-20:00:
- Base flexibility target: 15 kW at the slot's 9.5 CHF/MW minimum
- EV add-on target: 0.8 kW
- EV-specific metadata: `ev_bid_price: 10.0` and `ev_activation_cost: 4.5`

The current trader adds `ev_flexibility_mw` to the configured target but uses the main slot `bid_price` for order-price acceptance. The EV-specific price and activation-cost values do not create a second live order price.

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
Combines aggressive peak targets with full-day HP coverage. It is marked as recommended in the strategy description, but `supsi01` currently defaults to `strategy_11`.

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
    │    40 kW @ 9.0          │          │    Variable pricing             │
    └─────────────────────────┘          └─────────────────────────────────┘
```

| Time Slot | Hours | Flexibility | Min Price | Approach |
|-----------|-------|-------------|-----------|----------|
| **Morning Peak** | 06:30-09:00 | **40 kW** | **9.0 CHF/MW** | **Aggressive (S3)** |
| Late Morning | 09:00-12:00 | 12 kW | 8.5 CHF/MW | Conservative (S1) |
| Afternoon | 12:00-16:00 | 8 kW | 7.0 CHF/MW | Conservative (S1) |
| Evening Peak | 16:00-19:00 | 40 kW | 9.5 CHF/MW | Aggressive |
| Off-peak | 19:00-06:30 | 6 kW | 6.0 CHF/MW | Conservative (S1) |

### When to Use
- **Recommended historical/legacy strategy**
- For maximum profitability
- When you want balanced risk/reward
- When you want the restored historical/legacy path, not persistence

---

## Strategy 5: Hybrid2 (S3+S2)

### Description
Combines the aggressive morning approach of Strategy 3 with Strategy 2's full-day coverage and additional evening EV target.

### Rationale
- Aggressive morning bidding (like S3)
- Adds an EV-specific quantity target during evening peak (like S2)
- Tests EV participation while maintaining strong morning presence

### Assets Used
- ✅ Heat pumps in the active portfolio (currently `ECM96.2`, `ECM97.3`)
- ✅ EV chargers (`ECM63.1`, `ECM63.2`) are strategy-eligible in every slot

As in Strategy 2, only the evening slot adds an explicit EV target and EV-specific economic fields; it does not restrict EV eligibility to that slot.

### Time Schedule

| Time Slot | Hours | Base Target | EV Add-on | Main Min Price |
|-----------|-------|-------------|-----------|----------------|
| **Morning Peak** | 06:30-09:00 | **20 kW** | — | **9.0 CHF/MW** |
| Late Morning | 09:00-12:00 | 10 kW | — | 7.5 CHF/MW |
| Afternoon | 12:00-17:00 | 6 kW | — | 6.0 CHF/MW |
| **Evening Peak** | 17:00-20:00 | 20 kW | **0.8 kW** | 9.5 CHF/MW |
| Off-peak | 20:00-06:30 | 5 kW | — | 5.5 CHF/MW |

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

## Strategy 10: Recent-profile Short-term Flexibility (EV)

### Description
EV-only short-term flexibility strategy based on a recent power profile rather than a single lagged persistence baseline.

**Flexibility method:** recent profile via `flexibility_method: "recent_profile"`.

Unlike `strategy_8` and `strategy_9`, this strategy does **not** use simple lagged persistence. It estimates expected upcoming power from recent 15-minute measurements. Although the strategy name and JSON description reflect its original continuous-EV intent, the current `ECM63.1` and `ECM63.2` asset mappings explicitly set `modulation_type: "discrete"`. The effective configuration therefore applies the discrete factor and maps the result to configured OCPP states.

### Why It Was Introduced
- EV charging power varies within a session; a single lagged sample is often a poor baseline.
- A recent-profile estimate adapts to the current charging level while staying conservative.
- The same architecture can later support discrete HP assets, but HPs are currently excluded from bidding.

### Assets Used
- ✅ ECM63.1 (EV Charger 1)
- ✅ ECM63.2 (EV Charger 2)
- ❌ Heat pumps excluded by `assets_filter` (warm-season deployment; architecture supports discrete HPs for future use)

### Recent-profile Forecasting Logic

For each allowed asset at bid time:

1. **Choose the lookback:** the current discrete EV mappings use the configured 90-minute window. The 45/60-minute adaptive rules apply only to assets mapped as continuous.
2. **Collect 15-minute grouped measurements** over that lookback.
3. **Apply the current-power activity gate:** if the latest measurement is at or below `activeThresholdW` (5000 W), the asset contributes 0 flexibility.
4. **Estimate expected power** as q25 of recent samples.
5. **Cap expected power** at `nominal_power_w`.
6. **Apply the configured modulation factor:** the current assets are discrete, so `discreteFactor: 1.0` is used. `continuousFactor: 0.5` would apply only if an allowed asset were mapped as continuous.
7. **Map to a feasible OCPP state:** the desired flexibility is converted to an achievable curtailment using the asset's configured states, currently `6.93` through `11.0` kW.

The JSON description still says "last 2h", but `recentProfileSettings.lookbackMinutes` is `90`; the strategy-owned setting is the effective value.

### Difference vs Persistence (`strategy_8` / `strategy_9`)

| Aspect | Persistence (`strategy_8` / `strategy_9`) | Recent profile (`strategy_10`) |
|--------|-------------------------------------------|--------------------------------|
| Baseline source | Single lagged measurement (e.g. t-90 min) | Lower quantile of recent lookback window |
| Typical use | HP and mixed HP+EV persistence validation | Short-term EV profile estimation |
| EV treatment | Uses lagged EV power as baseline | Uses recent charging profile (q25) |
| Current-power gate | Yes | Yes |
| Discrete mapping | Uses the configured asset modulation in allocation/activation | Applies discrete OCPP mapping with the current asset mappings |
| Activation path | Persistence bid-record / flexi_manager path | Same persistence activation infrastructure |

Both persistence and recent-profile strategies are **gated methods**: they require a fresh current measurement, skip unsafe assets, and reuse the same bid-record activation pipeline in `trader_fsp.py` and `flexi_manager.py`.

### Recent-profile Behaviour
- Requires at least `minSamples` recent measurements (default: 2); otherwise the asset contributes 0 flexibility.
- Missing, failed, or stale current measurements skip the affected asset under `missingMeasurementPolicy: "skip_asset"`.
- Recommended bid quantity is constrained to an achievable discrete allocation for the current EV mappings.
- `bid_record_assets` stores the selected allocation only, not the full asset list.

### Strategy-owned Settings

Recent-profile parameters belong to the strategy config:

```text
bidding_strategies.strategy_10.recentProfileSettings
```

They are **not** global `flexibility.recentProfileSettings` anymore. For backward compatibility, a legacy global block is still accepted with a warning if the strategy block is missing.

| Setting | Configured value | Purpose |
|---------|------------------|---------|
| `lookbackMinutes` | 90 | Fallback recent measurement window |
| `quantile` | 0.25 | Conservative expected-power estimate (q25) |
| `continuousFactor` | 0.5 | Used only for assets mapped as continuous |
| `discreteFactor` | 1.0 | Effective factor for the current EV mappings |
| `activeThresholdW` | 5000 | Current-power activity gate |
| `maxConsecutiveActivationSlots` | 4 | Maximum consecutive controlled slots |
| `cooldownSlotsAfterMaxActivation` | 2 | Released slots after reaching the cap |
| `minSamples` | 2 | Minimum recent samples required |
| `missingMeasurementPolicy` | `skip_asset` | Safe handling when data is missing |
| `adaptiveLookback` | enabled for `continuous` | Defines 45/60-minute windows at load ratios 0.80/0.65; inactive for the current discrete EV mappings |

### When to Use
- When testing short-term EV flexibility based on current charging behaviour
- During warm season when HP participation is intentionally disabled
- After validating EV telemetry quality on `ECM63.1` and `ECM63.2`

### Caution
- Start with `--dry-run` and inspect per-asset recent-profile logs before live bidding.
- HP support is architecturally ready but currently disabled via `assets_filter`.
- Strategy 10 is not a continuous fallback with the current shared EV asset mappings. Continuous operation requires changing or overriding those mappings.

---

## Strategy 11: Recent-profile Discrete EV Current-step Flexibility

### Description
EV-only strategy that combines recent-profile reference power estimation with discrete OCPP current-step power states. Both configured chargers expose 7 power states. Compared with `strategy_10`, Strategy 11 uses a shorter fixed lookback, a higher activity threshold, a smaller discrete factor, and explicit discrete-EV policy settings.

**Flexibility method:** recent profile via `flexibility_method: "recent_profile"`.

### Why It Was Introduced
- Real OCPP chargers accept integer current limits, not arbitrary kW setpoints.
- Sending low-power commands (below ~6 kW) caused unstable charger behavior.
- A continuous curtailment value (e.g. 5.3 kW) cannot be sent; the nearest valid OCPP state must be selected.
- Overdelivery (sending slightly more curtailment than requested) is preferred over underdelivery to meet market obligations.

### Assets Used
- ✅ ECM63.1 (EV Charger 1) — `modulation_type: "discrete"`, 7 OCPP states
- ✅ ECM63.2 (EV Charger 2) — `modulation_type: "discrete"`, 7 OCPP states
- ❌ Heat pumps excluded (EV-only strategy)

### EV Charger Discrete States

Both chargers are configured with 7 feasible OCPP power states (kW):

```
6.93  7.62  8.31  9.01  9.70  10.39  11.0
```

Key constraints:
- **No 0 kW state**: normal flexibility activation never commands the charger to stop entirely.
- **Effective minimum state is 6.93 kW**: the configured `min_target_power_kw` is 6.24 kW, but that setting filters states and does not create the missing 6.24 kW state.
- **Maximum state is 11.0 kW**: full charger capacity.

### Bidding Logic

Strategy_11 reuses the same recent-profile reference pipeline as strategy_10, then adds a discrete mapping step:

1. **Collect recent measurements** over the lookback window (45 minutes).
2. **Apply the current-power activity gate:** if the latest measurement is at or below `activeThresholdW` (6000 W), the asset contributes 0 flexibility.
3. **Estimate expected power** as q25 of recent samples, capped at nominal.
4. **Compute raw desired flexibility:** `discreteFactor × expected_power` (factor: 0.5).
5. **Map to discrete curtailment** using the overdelivery-tolerant selection policy.

### Overdelivery-tolerant Selection Policy

Given the reference power and desired flexibility, the algorithm selects which OCPP state to target:

1. Compute feasible curtailments: `curtailment = reference_power - state` for each state below the reference.
2. Choose the **smallest feasible curtailment ≥ desired** (minimizes overdelivery).
3. If no feasible curtailment ≥ desired exists, choose the **largest feasible curtailment below desired** (maximum reachable).

Example with reference = 11.0 kW:

| Target State | Feasible Curtailment |
|-------------|---------------------|
| 10.39 kW | 0.61 kW |
| 9.70 kW | 1.30 kW |
| 9.01 kW | 1.99 kW |
| 8.31 kW | 2.69 kW |
| 7.62 kW | 3.38 kW |
| 6.93 kW | 4.07 kW |

| Desired Flexibility | Selected Target | Actual Curtailment | Reason |
|--------------------|-----------------|-------------------|--------|
| 2.0 kW | 8.31 kW | 2.69 kW | Smallest curtailment ≥ 2.0 |
| 1.0 kW | 9.70 kW | 1.30 kW | Smallest curtailment ≥ 1.0 |
| 5.5 kW | 6.93 kW | 4.07 kW | No curtailment ≥ 5.5; maximum reachable |

The bid's `available_flexibility_kw` is the **actual discrete curtailment**, not the raw continuous desired value.

### Difference vs Strategy_10

| Aspect | `strategy_10` | `strategy_11` |
|--------|---------------|---------------|
| EV modulation type | `discrete` with current asset mappings | `discrete` |
| Control command | One of 7 OCPP states | One of 7 OCPP states |
| Discrete factor | 1.0 | 0.5 |
| Effective minimum power | 6.93 kW | 6.93 kW |
| Explicit discrete policy | No `discreteEvSettings` block | `smallest_overdelivery`, zero disabled, requested floor 6.24 kW |
| Active threshold | 5000 W | 6000 W |
| Lookback | 90 min for current discrete mappings; adaptive block applies only to continuous assets | 45 min |

### Activation Behaviour

At activation time, `flexi_manager.py` detects a discrete EV charger (asset type `ev_charger` with `modulation_type: "discrete"` and more than 2 states) and uses the EV-specific discrete activation path instead of the HP ON/OFF path:

1. Resolve the activation reference (activation-current measurement for new activations; bid reference for continuations).
2. Select the best discrete OCPP state using the same overdelivery-tolerant policy.
3. Send **only** a power value from `discrete_states_kw` — never an arbitrary continuous value, never 0.0.

### Comfort Cooldown Guard

The same EV comfort guard as strategy_10 applies to strategy_11:

| Setting | Value | Purpose |
|---------|-------|---------|
| `maxConsecutiveActivationSlots` | 4 | Maximum consecutive 15-min slots under control |
| `cooldownSlotsAfterMaxActivation` | 2 | Slots released after reaching the cap |

After 4 consecutive activation slots, the charger is restored and enters a 2-slot cooldown. This prevents indefinite curtailment of EV charging sessions. The guard does **not** apply to heat pumps.

### Strategy-owned Settings

```text
bidding_strategies.strategy_11.recentProfileSettings
bidding_strategies.strategy_11.discreteEvSettings
```

| Setting | Value | Purpose |
|---------|-------|---------|
| `lookbackMinutes` | 45 | Recent measurement window |
| `quantile` | 0.25 | Conservative expected-power estimate (q25) |
| `discreteFactor` | 0.5 | Flexibility fraction for discrete EV assets |
| `activeThresholdW` | 6000 | Current-power activity gate; below the effective 6.93 kW target floor |
| `minSamples` | 2 | Minimum recent samples required |
| `selection_policy` | `smallest_overdelivery` | Prefer minimal overdelivery |
| `allow_zero_state` | `false` | Never command 0 kW |
| `min_target_power_kw` | 6.24 | Lower-bound filter; effective floor is 6.93 kW because 6.24 is absent from the asset states |

### When to Use
- When controlling real EV chargers through OCPP integer current limits
- When the chargers cannot accept arbitrary continuous power setpoints
- When low-current commands below the configured 6.93 kW floor must be avoided for stability
- After validating EV telemetry and OCPP state transitions on `ECM63.1` and `ECM63.2`

### Caution
- Start with `--dry-run` and verify that selected target states match expected OCPP behaviour.
- The 7-state configuration assumes a specific charger model and voltage; adjust `discrete_states_kw` if the charger hardware or site voltage differs.
- Strategy 10 uses different recent-profile parameters, but it is also discrete under the current shared asset mapping.

---

## Strategy 12: Preconditioned Binary HP Flexibility

### Description
Strategy 12 is the simulated heat-pump flexibility strategy for binary ON/OFF HP assets. It is designed for a controlled workflow where the HP portfolio is prepared before the delivery window, validated through telemetry, and then used for discrete OFF delivery.

**Flexibility method:** preconditioned binary via `flexibility_method: "preconditioned_binary"`.

### Why It Was Introduced
- Simulated HPs are binary assets with two usable states: OFF and ON.
- Flexibility is only safe to bid when the asset has actually been prepared and recent telemetry proves it is ON.
- The strategy needs lifecycle ownership so the manager remembers which HPs it prepared and can release them safely after the delivery period.
- The workflow supports simulator integration without changing the existing real-asset command and forwarder architecture.

### Assets Used
- ✅ ECM62.10 — one independent simulated HP asset, `[0.0, 36.0]` kW
- ✅ ECM68.3 — simulated HP asset, `[0.0, 8.4]` kW
- ✅ ECM162.1 — simulated HP asset, `[0.0, 6.0]` kW
- ❌ ECM62.1, ECM62.2, ECM62.3 are not Strategy 12 assets and must not be used as substitutes
- ❌ EV chargers excluded

### Binary HP States

| Asset | OFF state | ON state | Flexibility block |
|-------|----------:|---------:|------------------:|
| ECM62.10 | 0.0 kW | 36.0 kW | 36.0 kW |
| ECM68.3 | 0.0 kW | 8.4 kW | 8.4 kW |
| ECM162.1 | 0.0 kW | 6.0 kW | 6.0 kW |

### Weather Gate

Strategy 12 has a strategy-owned weather gate that can answer one question:

```text
Should Strategy 12 prepare the HP portfolio today?
```

The weather gate is **currently disabled** with `weatherGateSettings.enabled: false`. The enabled preconditioning lifecycle therefore runs without a temperature admission decision. If the gate is enabled, it reads `flexibility.temperature.forecast`, evaluates 17:00-20:00 using the maximum temperature, and opens when that value reaches the configured 24.0 C threshold.

The intended cooling-oriented decision is:

```text
aggregated forecast temperature >= temperatureThresholdC
    -> prepare portfolio

aggregated forecast temperature < temperatureThresholdC
    -> skip preparation
```

Missing or invalid forecast data must not increase preparation activity. The safe policy is `missingForecastPolicy: "skip_preconditioning"`.

### Lifecycle

Strategy 12 is not a normal stateless activation strategy. Its lifecycle is owned by `flexi_manager.py`:

| Phase | Time | Desired state | Notes |
|-------|------|---------------|-------|
| Idle | Before 14:00 | No new preparation | Stale Strategy 12 ownership may be cleaned up by OFF commands. |
| Prepare | 14:00-17:00 | All Strategy 12 assets ON | Unconditional while the weather gate is disabled; gate-controlled if enabled. |
| Maintain | 17:00-20:00 | Selected delivery assets OFF; non-selected assets ON | The selected OFF assets provide contracted flexibility. |
| Release | At/after 20:00 | All Strategy 12-owned assets OFF | Ownership is cleared only after the OFF command is accepted. |

When the weather gate is enabled and admits the day, ownership remains stable for that day. Later forecast changes must not toggle the portfolio ON/OFF every manager run.

The 14:00-17:00 `time_slots` entry is only a non-bidding marker. Its `is_preheat_period` flag does not drive `flexi_actuator.py`; lifecycle commands come from the separate enabled `preconditioningSettings` block and are owned by `flexi_manager.py`.

### Bidding Logic

Strategy 12 uses `preconditioned_binary` telemetry validation:

1. Read recent active-power measurements for each Strategy 12 asset.
2. Convert configured binary states from kW to W.
3. Classify samples as ON, OFF, or invalid using `stateToleranceW`.
4. Require enough valid samples, a fresh latest sample, a latest ON state when configured, and the configured minimum ON ratio.
5. Offer the full binary block for assets that pass the gates; offer 0 for assets that fail.

Typical settings:

| Setting | Purpose |
|---------|---------|
| `stateToleranceW` | Allowed W tolerance around configured OFF/ON states |
| `minSamples` | Minimum recent classified samples required |
| `requireLatestOn` | Requires the latest sample to be ON |
| `minOnRatio` | Minimum ON ratio over valid samples |
| `maxCurrentMeasurementAgeMinutes` | Freshness limit for the latest sample |
| `missingMeasurementPolicy` | Safe handling when telemetry is missing |

### Activation Behaviour

During the maintain phase, selected assets are commanded OFF for delivery and non-selected assets remain ON. Activation DB records are written only for delivery OFF commands in the 17:00-20:00 maintain window.

Release OFF commands and idle cleanup OFF commands are lifecycle cleanup operations, not delivery activations, and must not create activation records.

### Command and Simulator Routing

Strategy 12 commands use the existing controller and RabbitMQ command machinery:

```text
flexi_manager.py
    -> controller.restore_asset(...) for ON
    -> controller.curtail_asset(..., force_discrete_off=True) for OFF
    -> rabbitMQ.simulatedAssetCommands
    -> external simulator
```

The forwarder is not in the simulated command path. It consumes simulated measurements from `simulatedAssetMeasures` and forwards them to the configured measurement target.

### Ownership and Cleanup Semantics

Strategy 12 lifecycle ownership is persistent. This is what lets the manager know which assets were prepared and which assets still need cleanup.

For release and stale idle cleanup:

```text
OFF accepted
    -> clear Strategy 12 ownership

OFF failed
    -> preserve ownership
    -> next periodic manager run naturally retries cleanup
```

There is no explicit retry counter, backoff, or sleep loop. The periodic manager execution is the retry mechanism.

### When to Use
- When testing simulated HP flexibility with the external simulator.
- When a scheduled preconditioning workflow is required.
- After enabling and validating `weatherGateSettings`, when temperature-gated preparation is required.
- When binary HP availability must be proven from recent telemetry before bidding.
- When validating the full simulated command and measurement loop for `ECM62.10`, `ECM68.3`, and `ECM162.1`.

### Caution
- Do not use `ECM62.1`, `ECM62.2`, or `ECM62.3` for Strategy 12.
- Confirm simulator command consumption from `simulatedAssetCommands` before live tests.
- Confirm simulator measurements publish active power in W with exact `device_name` values.
- Do not interpret the preconditioning time-slot marker as an actuator trigger; the enabled manager lifecycle is configured separately.
- Use dry-run and local/fake simulator tests before any live closed-loop run.

---

## Strategy Comparison

| Strategy | Forecasting logic | Asset types | Control |
|----------|-------------------|-------------|---------|
| `strategy_8` | Persistence (lagged baseline) | HP | ON/OFF |
| `strategy_9` | Persistence (lagged baseline) | HP + EV | Mixed, with discrete EV mappings |
| `strategy_10` | Recent profile q25; 90-minute lookback for current mappings | EV (currently) | Discrete OCPP under current mappings |
| `strategy_11` | Recent profile q25 + discrete mapping | EV (discrete) | OCPP current-step |
| `strategy_12` | Preconditioned binary; weather gate disabled | Simulated HP | Binary ON/OFF lifecycle |

| Metric | S1 | S2 | S3 | S4 | S5 | S6 | S7 | S8 | S9 | S10 | S11 | S12 |
|--------|----|----|----|----|----|----|----|----|----|-----|-----|-----|
| Flexibility method | Historical | Historical | Historical | Historical | Historical | Historical | Historical | Persistence | Persistence | Recent profile | Recent profile | Preconditioned binary |
| Uses EV chargers | No | Yes | No | No | Yes | No | No | No | Yes | Yes | Yes | No |
| EV asset mapping | — | Discrete | — | — | Discrete | — | — | — | Discrete | Discrete | Discrete | — |
| Explicit asset filter | No | No | `ECM97.3` | `ECM96.2`, `ECM97.3` | No | No | `ECM96.2` | `ECM96.2`, `ECM97.3` | `ECM63.1`, `ECM63.2`, `ECM96.2`, `ECM97.3` | `ECM63.1`, `ECM63.2` | `ECM63.1`, `ECM63.2` | `ECM62.10`, `ECM68.3`, `ECM162.1` |
| Preheat / preconditioning | No | No | No | No | No | Yes | Yes | No | No | No | No | Yes, lifecycle-owned; weather gate off |
| Gated current-state method | No | No | No | No | No | No | No | Yes | Yes | Yes | Yes | Yes, binary telemetry validation |
| Discrete state mapping | No | EV mapping | No | No | EV mapping | No | No | No | EV mapping | Yes with current mapping | Yes | Binary OFF/ON |
| Strategy-owned forecast settings | No | No | No | No | No | No | No | No | No | `recentProfileSettings` | `recentProfileSettings`, `discreteEvSettings` | `preconditionedBinarySettings`, `preconditioningSettings`, `weatherGateSettings` |

---

## How Strategies Work

### 1. Asset Filtering
Each strategy defines which assets can participate:
- Heat pumps only: `strategy_1`, `strategy_3`, `strategy_4`, `strategy_6`, `strategy_7`, `strategy_8`
- Heat pumps plus EV chargers: `strategy_2`, `strategy_5`, `strategy_9`
- EV chargers only (recent profile, discrete under the current mappings): `strategy_10`, `strategy_11`
- Simulated binary heat pumps only: `strategy_12`
- Specific asset filters: `strategy_3`, `strategy_4`, `strategy_7`, `strategy_8`, `strategy_9`, `strategy_10`, `strategy_11`, `strategy_12`

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

For `strategy_12`, available flexibility is the sum of binary blocks whose telemetry proves they are currently ON and eligible under `preconditioned_binary`. A prepared asset that is not selected for current delivery remains ON during the maintain window; selected assets are switched OFF for delivery.

### 4. Price Acceptance
Orders are only placed when the DSO's offered price meets the active slot's main `bid_price`. Across the current strategies, morning-slot minimums range from 7.5 to 10.5 CHF/MW, while bidding off-peak minimums range from 4.5 to 6.0 CHF/MW. Strategy 12 uses 11.5 CHF/MW for its 17:00-20:00 delivery window and zero-price markers outside that window.

---

## Configuration

Strategies are configured in `conf/test_fm01_aem.json`:

```json
"bidding_strategies": {
  "strategy_4": {
    "name": "Hybrid (S3+S1)",
    "description": "Morning peak aggressive (S3) + full day HP coverage (S1) - RECOMMENDED",
    "asset_types": ["heat_pump"],
    "assets_filter": ["ECM96.2", "ECM97.3"],
    "time_slots": [
      {"name": "Morning Peak (Aggressive)", "start": "06:30", "end": "09:00",
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

Recent-profile strategies include strategy-owned forecast settings:

```json
"strategy_10": {
  "name": "Recent-profile Short-term Flexibility (EV)",
  "asset_types": ["ev_charger", "heat_pump"],
  "assets_filter": ["ECM63.1", "ECM63.2"],
  "flexibility_method": "recent_profile",
  "recentProfileSettings": {
    "lookbackMinutes": 90,
    "quantile": 0.25,
    "continuousFactor": 0.5,
    "discreteFactor": 1.0,
    "activeThresholdW": 5000,
    "maxConsecutiveActivationSlots": 4,
    "cooldownSlotsAfterMaxActivation": 2,
    "minSamples": 2,
    "missingMeasurementPolicy": "skip_asset",
    "adaptiveLookback": {
      "enabled": true,
      "appliesTo": "continuous",
      "mode": "load_ratio",
      "rules": [
        {"minLoadRatio": 0.80, "lookbackMinutes": 45},
        {"minLoadRatio": 0.65, "lookbackMinutes": 60}
      ]
    }
  },
  "time_slots": [
    {"name": "Morning Peak", "start": "06:30", "end": "09:00",
     "flexibility_mw": 0.011, "bid_price": 7.5, "activation_cost": 2.5},
    ...
  ]
}
```

Discrete EV strategies add a `discreteEvSettings` block:

```json
"strategy_11": {
  "name": "Recent-profile discrete EV current-step flexibility",
  "enabled": true,
  "asset_types": ["ev_charger"],
  "assets_filter": ["ECM63.1", "ECM63.2"],
  "flexibility_method": "recent_profile",
  "recentProfileSettings": {
    "lookbackMinutes": 45,
    "quantile": 0.25,
    "continuousFactor": 0.5,
    "discreteFactor": 0.5,
    "activeThresholdW": 6000,
    "minSamples": 2,
    "maxConsecutiveActivationSlots": 4,
    "cooldownSlotsAfterMaxActivation": 2
  },
  "discreteEvSettings": {
    "selection_policy": "smallest_overdelivery",
    "allow_zero_state": false,
    "min_target_power_kw": 6.24
  },
  "time_slots": [...]
}
```

Settings resolution order for `recentProfileSettings`:
1. `bidding_strategies.<strategy_id>.recentProfileSettings` (preferred)
2. legacy global `flexibility.recentProfileSettings` (warning fallback)
3. built-in defaults

Preconditioned-binary strategies use Strategy 12-owned settings. The relevant current configuration fields are:

```json
"strategy_12": {
  "name": "Preconditioned Binary HP Flexibility",
  "enabled": true,
  "implementation_status": "preconditioned_binary_method_live",
  "asset_types": ["heat_pump"],
  "assets_filter": ["ECM62.10", "ECM68.3", "ECM162.1"],
  "flexibility_method": "preconditioned_binary",
  "nominal_portfolio_flexibility_mw": 0.0504,
  "preconditionedBinarySettings": {
    "maxCurrentMeasurementAgeMinutes": 30,
    "minSamples": 2,
    "requireLatestOn": true,
    "minOnRatio": 0.8,
    "stateToleranceW": 100,
    "missingMeasurementPolicy": "skip_asset"
  },
  "preconditioningSettings": {
    "enabled": true,
    "prepareStart": "14:00",
    "flexibilityStart": "17:00",
    "maintainUntil": "20:00",
    "releaseAction": "force_off",
    "ownerTag": "strategy_12"
  },
  "weatherGateSettings": {
    "enabled": false,
    "source": "flexibility.temperature.forecast",
    "temperatureThresholdC": 24.0,
    "evaluationStart": "17:00",
    "evaluationEnd": "20:00",
    "aggregation": "max",
    "missingForecastPolicy": "skip_preconditioning"
  },
  "time_slots": [
    {"name": "Pre-conditioning (configuration only)",
     "start": "14:00", "end": "17:00", "flexibility_mw": 0.0,
     "bid_price": 0.0, "activation_cost": 0.0, "is_preheat_period": true},
    {"name": "Evening Peak Flex (preconditioned_binary)",
     "start": "17:00", "end": "20:00", "flexibility_mw": 0.0504,
     "bid_price": 11.5, "activation_cost": 0.8,
     "target_window": true, "target_flexibility_mw": 0.0504},
    {"name": "Off-window (no bid)",
     "start": "20:00", "end": "14:00", "flexibility_mw": 0.0,
     "bid_price": 0.0, "activation_cost": 0.0}
  ]
}
```

The Strategy 12 asset mapping must use exact simulated asset IDs and binary states:

```json
"ECM62.10": {
  "device_name_tag": "ECM62.10",
  "pod": "ECM62",
  "field": "active_power",
  "type": "heat_pump",
  "rabbitCommandSection": "simulatedAssetCommands",
  "capacity_kw": 36.0,
  "nominal_power_w": 36000,
  "modulation_type": "discrete",
  "discrete_states_kw": [0.0, 36.0]
}
```

Use equivalent entries for `ECM68.3` (`[0.0, 8.4]`) and `ECM162.1` (`[0.0, 6.0]`).

### FSP Configuration
Each configured FSP has its own portfolio and default strategy:
```json
"supsi01": {
  "strategy": "strategy_11",
  "assets": ["ECM96.2", "ECM97.3", "ECM63.1", "ECM63.2"]
},
"supsi02": {
  "strategy": "strategy_12",
  "assets": ["ECM62.10", "ECM68.3", "ECM162.1"]
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

Recent-profile dry run (current discrete EV mappings):

```bash
python scripts/trader_fsp.py --config_file conf/test_fm01_aem.json \
    --fsp supsi01 --strategy strategy_10 --dry-run
```

Discrete EV dry run:

```bash
python scripts/trader_fsp.py --config_file conf/test_fm01_aem.json \
    --fsp supsi01 --strategy strategy_11 --dry-run
```

Strategy 12 simulated HP dry run:

```bash
python scripts/trader_fsp.py --config_file conf/test_fm01_aem.json \
    --fsp supsi02 --strategy strategy_12 --dry-run
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
The legacy evaluator defines most strategy slot assumptions internally rather than loading every current `bidding_strategies` value. Treat its output as a scenario estimate and verify its internal definitions before comparing results with live configuration.

```bash
python scripts/strategy_evaluator.py --config_file conf/test_fm01_aem.json \
    --start_date 2025-12-01 --end_date 2025-12-31
```

---

## Recommendations

1. **Use `strategy_4`** when you want the configured recommended historical/legacy HP hybrid.
2. **Use `strategy_8`** when validating persistence bidding with the safer HP-only asset set.
3. **Use `strategy_9`** only after validating EV telemetry quality, because it includes EV chargers.
4. **Use `strategy_10`** when testing its 90-minute recent-profile EV parameters; current EV mappings make its bidding and activation discrete.
5. **Use `strategy_11`** for real OCPP EV chargers with discrete current-step states; this is the preferred strategy for production EV activation on `ECM63.1` and `ECM63.2`.
6. **Use `strategy_12`** when testing scheduled simulated binary HP preparation and simulator measurement feedback. Enable and validate the weather gate separately if temperature admission is required.
7. **Use `strategy_6` or `strategy_7`** when specifically testing the configured preheat schedules.
8. **Use dry-run first** before enabling live bidding for any strategy.

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
| **Recent profile** | Short-term flexibility method using a lower quantile of recent measurements |
| **Persistence current-state gate** | Rule that assets below `activeThresholdW` contribute zero flexibility |
| **Gated flexibility method** | Method that gates bids on current measurements; `persistence` and `recent_profile` reuse the persistence activation path, while `preconditioned_binary` uses the Strategy 12 lifecycle |
| **Discrete states** | Finite set of power levels an asset can be commanded to; for OCPP EVs these correspond to integer current limits |
| **OCPP current-step** | A specific integer current limit sent to the charger via OCPP; each step maps to a fixed kW power level |
| **Overdelivery-tolerant selection** | Policy that prefers slightly more curtailment than requested over less, when exact discrete match is unavailable |
| **Comfort cooldown guard** | Mechanism limiting consecutive EV activation slots and enforcing a cooldown period to protect user charging sessions |
| **Preconditioned binary** | Strategy 12 flexibility method that bids only binary HP blocks whose recent telemetry proves they are ON |
| **Weather gate** | Strategy 12 daily decision that controls whether the simulated HP portfolio should be prepared |
| **Lifecycle ownership** | Persistent Strategy 12 state indicating that the manager prepared or controls an asset and must later release it |
| **Natural cleanup retry** | Strategy 12 behavior where failed cleanup OFF ownership is preserved so the next manager run retries without an explicit retry loop |
