# pyfm Architecture

This document provides a high-level overview of the pyfm system: a Python framework for participating in the Opentunity-CH flexibility market. It describes the full operational pipeline from baseline computation through bidding, activation, and command forwarding, without going into component-level detail. Each section references the dedicated documentation for deeper information.

---

## System Overview

pyfm implements an end-to-end flexibility-market workflow for two configured Flexibility Service Providers (FSPs). The FSPs manage real heat pumps and EV chargers plus a separate simulated heat-pump portfolio in a 15-minute NODES market.

The operational flow has shared market and database handoffs, followed by two different RabbitMQ routes:

```text
InfluxDB -> baseline_updater.py -> NODES baselines --+
trader_dso.py ------------------> NODES buy orders   +-> NODES market/trades
trader_fsp.py <------------------ NODES baselines    |
trader_fsp.py ------------------> NODES sell orders -+
      |
      +-> PostgreSQL bid records and selected assets

PostgreSQL records + NODES accepted trades -> flexi_manager.py
                                                |
                                                +-> realAssetCommands
                                                |        -> forwarder.py -> AEM/HTTP target
                                                |
                                                +-> simulatedAssetCommands
                                                         -> external simulator
                                                                  |
                                                                  +-> simulatedAssetMeasures
                                                                           -> forwarder.py
                                                                           -> configured target
```

Simulated commands do not pass through `forwarder.py`; the external simulator consumes them directly. The forwarder consumes real commands and simulated measurements.

The market and control components load `conf/test_fm01_aem.json` and merge the configured private `conf/private/conns.json` overlay. The forwarder has its own target configuration and also resolves RabbitMQ/API connection details from the private overlay.

### Current Configuration Snapshot

| Setting | Configured value |
|---------|------------------|
| Location | Lugano (`46.0037`, `8.9511`) |
| Market | `Opentunity-CH`, Switzerland |
| Community / DSO | `ECM` / `AEM` |
| Market granularity | 15 minutes |
| Order and baseline target shift | 90 minutes |
| Baseline source / strategy | InfluxDB-backed `db` / `slot_persistence` |
| Global flexibility fallback | `persistence` |

| FSP | Default strategy | Assigned assets | Command path |
|-----|------------------|-----------------|--------------|
| `supsi01` | `strategy_11` | `ECM96.2`, `ECM97.3`, `ECM63.1`, `ECM63.2` | `realAssetCommands` |
| `supsi02` | `strategy_12` | `ECM62.10`, `ECM68.3`, `ECM162.1` | `simulatedAssetCommands` |

The asset mapping contains additional assets that are not assigned to these FSP portfolios. Strategy eligibility is always intersected with the selected FSP portfolio. `ECM62.10` is one independent 36 kW simulated asset, not an alias for `ECM62.1`, `ECM62.2`, or `ECM62.3`.

---

## Pipeline Stages

### 1. Baseline

The baseline is the reference power consumption profile for each asset portfolio. It represents what the assets would consume without any flexibility activation. The NODES market uses this baseline to measure the actual flexibility delivered.

**What it does:**

- Queries recent asset power measurements from InfluxDB.
- Computes a per-asset baseline using a configurable strategy (slot persistence or day persistence).
- Aggregates asset-level baselines into a portfolio-level baseline.
- Uploads the baseline to the NODES platform and optionally saves it to InfluxDB.

**Current operational mode:**

- `baseline.source` is `db` and `baseline.dbSettings.strategy` is `slot_persistence`.
- `shiftMinutes: 90` selects the target horizon 90 minutes after the current 15-minute boundary.
- `upcomingHoursToQuery: 24` builds the candidate horizon, while `uploadOnlyComputableHorizon: true` and `maxSlotsToUpload: 1` limit each run to the first computable target slot.
- Every asset assigned to either current FSP has `baseline_persistence_go_back_minutes: 120`, so its source measurement is taken 120 minutes before the target slot. The 90-minute shift and 120-minute source lag are separate settings.
- `missingMeasurementPolicy: "zero_fill_asset"` contributes zero for an asset whose source measurement is missing instead of failing the entire portfolio.
- `valueMultiplier: -1.0` inverts the aggregated baseline sign before upload.

**Scheduling:** The deployment examples run the updater every 15 minutes. Scheduling is external to the JSON configuration.

**Detailed documentation:**

- [README_baseline_updater.md](README_baseline_updater.md) -- full operational guide, configuration, and log interpretation
- [baseline_forecast_evaluator.md](baseline_forecast_evaluator.md) -- baseline quality analysis tool
- [FLEXIBILITY_ANALYSIS.md](FLEXIBILITY_ANALYSIS.md) -- persistence formulas and the relationship between baseline persistence and bidding flexibility persistence

---

### 2. Trading and Bidding

Trading is the market interaction phase where buy and sell orders are placed on the NODES platform. Two actors participate:

**DSO (Distribution System Operator) -- Buy side:**

The DSO agent computes how much flexibility the grid needs for a future 15-minute slot, determines a price based on forecast signals, and places a Buy order through the FMO (Flexibility Market Operator) into the market ledger.

**FSP (Flexibility Service Provider) -- Sell side:**

The FSP agent responds to DSO flexibility requests by:

1. Downloading current baselines from NODES.
2. Computing available flexibility from the asset portfolio using the selected strategy's historical, persistence, recent-profile, or preconditioned-binary method.
3. Applying a bidding strategy that determines which assets to include, the bid quantity, and the price for the current time-of-day window.
4. Posting Sell orders to the NODES market.
5. Recording bid details (strategy, assets, quantities) in PostgreSQL for the activation stage.

**Discretization-aware bidding:** Every asset in the current mapping is discrete. Heat pumps have binary OFF/ON states, while `ECM63.1` and `ECM63.2` expose seven OCPP power states from 6.93 to 11.0 kW. The engine also supports continuous assets, but none are currently mapped as continuous. Recommended bids are constrained to achievable allocations.

**Bidding strategies:** The configuration contains `strategy_1` through `strategy_12`:

| Strategies | Flexibility method | Current role |
|------------|--------------------|--------------|
| `strategy_1`-`strategy_7` | Historical/legacy | Legacy schedules; no explicit method override |
| `strategy_8`, `strategy_9` | `persistence` | Current-state-gated lagged flexibility |
| `strategy_10`, `strategy_11` | `recent_profile` | Quantile-based EV forecasting with the current discrete EV mappings |
| `strategy_12` | `preconditioned_binary` | Telemetry-gated simulated binary-HP flexibility |

The global `flexibility.method` is `persistence`, but strategy mode resolves the method from the selected strategy: strategies without an explicit `flexibility_method` remain historical/legacy. `supsi01` defaults to Strategy 11 and `supsi02` defaults to Strategy 12.

**Scheduling:** The deployment examples run the FSP trader at minutes `:05`, `:20`, `:35`, and `:50`. The code rounds the current UTC time down to a 15-minute boundary and then applies `fm.ordersTimeShift: 90`; the cron schedule itself is not stored in this JSON file.

**Detailed documentation:**

- [README_trader_dso.md](README_trader_dso.md) -- DSO agent configuration, pricing model, and execution steps
- [README_trader_fsp.md](README_trader_fsp.md) -- FSP agent, persistence mode, discretization-aware bidding, strategy selection
- [BIDDING_STRATEGIES.md](BIDDING_STRATEGIES.md) -- full reference for all 12 bidding strategies with current configuration examples
- [FLEXIBILITY_ANALYSIS.md](FLEXIBILITY_ANALYSIS.md) -- how available flexibility is calculated (historical and persistence modes)
- [README_simulate_strategy_bidding.md](README_simulate_strategy_bidding.md) -- historical replay tool for strategy evaluation without market interaction

---

### 3. Activation

The activation manager handles normal accepted-trade delivery and the separate Strategy 12 preconditioning lifecycle.

**What it does:**

1. Resolves the selected FSP and active strategy, including any enabled strategy-owned lifecycle.
2. Reads the bid record from PostgreSQL to recover the strategy and selected bid assets for the target slot.
3. Queries the local market ledger and then NODES accepted trades to determine the actual sold quantity, not merely the offered quantity.
4. Allocates the sold quantity across allowed assets using modulation-aware achievable-flexibility logic.
5. Generates curtailment commands: binary OFF for heat pumps and a feasible lower OCPP state for the currently discrete EV chargers.
6. Tracks controlled or lifecycle-owned assets in persistent state and generates restore/cleanup commands when required.
7. Publishes commands to the `rabbitCommandSection` selected by each asset mapping.
8. Records delivery activations in PostgreSQL (`public.asset_activations`).

**Allocation strategies:** The default `modulation_aware` strategy allocates discrete assets using achievable combinations and can then fill any remainder with continuous assets. The latter capability is available to the engine even though the current configuration has no continuous mappings. Alternative strategies include `proportional`, `priority`, and `cost_optimal`.

**Strategy 12 lifecycle:** `supsi02` has `preconditioningSettings.enabled: true`:

| Phase | Configured time | Manager intent |
|-------|-----------------|----------------|
| Prepare | 14:00-17:00 | Command all `ECM62.10`, `ECM68.3`, and `ECM162.1` assets ON |
| Maintain | 17:00-20:00 | Keep non-selected assets ON and command selected delivery assets OFF |
| Release | At/after 20:00 | Command all lifecycle-owned assets OFF and clear ownership after acceptance |

The Strategy 12 weather gate is configured but currently disabled, so temperature does not gate preparation. Preparation, release, and idle-cleanup commands are lifecycle operations; only delivery OFF commands create activation records.

**Autonomous mode:** The general no-bid analyzer is enabled with a 3-hour lookahead, 7 historical days, and a 20% price-increase threshold. `preactivation_enabled` is `false`, so it can recommend pre-activation but does not publish autonomous pre-activation commands. This subsystem is separate from the enabled Strategy 12 lifecycle.

**Manual activation:** The `flexi_actuator.py` script provides a one-shot CLI for forcing individual assets on/off without running the full manager workflow.

**Scheduling:** Deployment examples run the manager at minutes `:14`, `:29`, `:44`, and `:59`, just before the next delivery boundary. Scheduling is external to the JSON configuration.

**Detailed documentation:**

- [README_flexi_manager.md](README_flexi_manager.md) -- full activation engine guide, allocation strategies, RabbitMQ integration, state management, and troubleshooting
- [README_flexi_actuator.md](README_flexi_actuator.md) -- manual command publisher for direct asset control
- [RABBITMQ_COMMANDS.md](RABBITMQ_COMMANDS.md) -- command message formats, envelope structure, and RabbitMQ topology

---

### 4. Forwarding

RabbitMQ decouples command generation from consumers, but the consumer differs by section: the pyfm forwarder handles real commands and simulated measurements, while the external simulator handles simulated commands.

**What it does:**

- Consumes real control commands from `realAssetCommands`.
- Consumes simulated measurement envelopes from `simulatedAssetMeasures`.
- Translates supported messages into protocol-specific HTTP requests for configured targets such as the AEM API.
- Supports dry-run (log only) and live (HTTP POST) modes, with the payload `dry_run` flag providing an additional safety layer.
- Handles retries, timeouts, and per-target configuration.

**RabbitMQ topology:** The system uses three section-based message paths:

| Section | Producer | Consumer | Purpose |
|---------|----------|----------|---------|
| `realAssetCommands` | `flexi_manager.py` / `flexi_actuator.py` | `forwarder.py` | Real physical asset commands |
| `simulatedAssetCommands` | `flexi_manager.py` / `flexi_actuator.py` | External simulator, not pyfm | Commands for simulated assets |
| `simulatedAssetMeasures` | External simulator | `forwarder.py` | Simulated measurements forwarded to the configured target |

Each asset's `rabbitCommandSection` in `asset_mapping` determines its command path. All real heat pumps and EV chargers map to `realAssetCommands`; all `ECM62.*`, `ECM68.3`, and `ECM162.1` simulated heat pumps map to `simulatedAssetCommands`. `simulatedAssetMeasures` is a measurement source and is not a valid asset command destination.

**Scheduling:** The forwarder runs as a long-lived consumer process, typically a Docker service, with the standard section selection `realAssetCommands,simulatedAssetMeasures`. It must not consume `simulatedAssetCommands` in the standard simulator topology.

**Detailed documentation:**

- [README_flexi_forwarder.md](README_flexi_forwarder.md) -- full architecture, target configuration, deployment (Docker and manual), troubleshooting
- [RABBITMQ_COMMANDS.md](RABBITMQ_COMMANDS.md) -- message formats and topology reference

---

## Data Flow and Infrastructure

### External Services

| Service | Role | Used By |
|---------|------|---------|
| **NODES API** | Flexibility market platform for baselines, orders, trades, and settlements | baseline updater, DSO/FSP traders, flexibility manager |
| **InfluxDB** | Time-series storage queried for asset measurements and used for generated baselines | baseline updater, FSP trader |
| **PostgreSQL** | Bid records, selected bid assets, demand records, market ledger, and delivery activations | FSP trader, flexibility manager |
| **RabbitMQ** | Section-based transport for real commands, simulated commands, and simulated measurements | manager, actuator, forwarder, external simulator |
| **AEM / HTTP APIs** | Downstream real-command and measurement targets | forwarder |
| **External simulator** | Consumes simulated HP commands and publishes simulated measurements | Strategy 12 path |

### Database Handoff

The primary coordination mechanism between trading and activation is PostgreSQL:

```
trader_fsp.py                              flexi_manager.py
     │                                           │
     ├─▶ public.bid_records          ◀───────────┤ (reads bid record)
     ├─▶ public.bid_record_assets    ◀───────────┤ (reads allowed assets)
     ├─▶ public.bid_record_orders                 │
     ├─▶ public.demand_records                    │
     ├─▶ public.market_ledger                     │
     │                                            ├─▶ public.asset_activations
     │                                            │
```

### End-to-End Timeline for a Single Delivery Slot

For a normal `supsi01` delivery slot at `12:15-12:30 UTC`, assuming the documented cron examples:

| Wall Clock (UTC) | Component | Action |
|-------------------|-----------|--------|
| 10:45 | `baseline_updater.py` | Selects the 12:15 target (`+90 min`), reads each assigned asset at 10:15 (`target - 120 min`), and uploads one baseline slot |
| 10:50 | `trader_fsp.py` | Floors time to 10:45, applies `+90 min`, bids for 12:15, and writes the bid record and selected assets |
| 10:50 - 12:14 | NODES market | Clears orders, matches trades |
| 12:14 | `flexi_manager.py` | Reads bid record, queries accepted trades, allocates flexibility, publishes commands |
| 12:14 | `forwarder.py` | Consumes `realAssetCommands` and sends configured HTTP requests |
| 12:15 - 12:30 | Real assets | Deliver flexibility using binary HP states or feasible EV OCPP states |
| 12:29 | `flexi_manager.py` | Next slot: restores assets no longer needed, activates new ones |

Strategy 12 adds schedule-driven manager work outside this accepted-trade sequence: preparation from 14:00-17:00, maintain/delivery from 17:00-20:00, and release at or after 20:00. Its commands go to the external simulator, not the forwarder.

---

## Operational Workflow

The scheduled cron jobs are deployment concerns rather than JSON settings. A configuration-aligned example runs the same pipeline once per FSP and lets each FSP use its configured default strategy:

```cron
# Real portfolio baseline and trading
*/15 * * * * .venv/bin/python scripts/baseline_updater.py --config_file conf/test_fm01_aem.json --fsp supsi01
5,20,35,50 * * * * .venv/bin/python scripts/trader_fsp.py --config_file conf/test_fm01_aem.json --fsp supsi01
14,29,44,59 * * * * .venv/bin/python scripts/flexi_manager.py --config_file conf/test_fm01_aem.json --fsp supsi01 --live --rabbitmq

# Simulated Strategy 12 portfolio
*/15 * * * * .venv/bin/python scripts/baseline_updater.py --config_file conf/test_fm01_aem.json --fsp supsi02
5,20,35,50 * * * * .venv/bin/python scripts/trader_fsp.py --config_file conf/test_fm01_aem.json --fsp supsi02
14,29,44,59 * * * * .venv/bin/python scripts/flexi_manager.py --config_file conf/test_fm01_aem.json --fsp supsi02 --live --rabbitmq
```

Without a CLI `--strategy` override, `supsi01` selects `strategy_11` and `supsi02` selects `strategy_12`. The forwarder runs as a persistent Docker service consuming `realAssetCommands` and `simulatedAssetMeasures`; the external simulator separately consumes `simulatedAssetCommands`.

**Detailed documentation:** [jobs_shortflex_workflow.md](jobs_shortflex_workflow.md) -- execution timeline, config dependencies, dry-run vs production behavior, and debugging checklist.

---

## Analysis and Tooling

Beyond the operational pipeline, the repository includes several analysis and evaluation tools:

| Tool | Purpose | Documentation |
|------|---------|---------------|
| Strategy Evaluator | Legacy scenario evaluator with internal strategy definitions; not every current JSON slot value is loaded | [README_strategy_evaluator.md](../scripts/README_strategy_evaluator.md) |
| Strategy Simulator | Replay production bidding logic without market interaction | [README_simulate_strategy_bidding.md](README_simulate_strategy_bidding.md) |
| Baseline Forecast Evaluator | Compare baseline forecasts against actual measurements | [baseline_forecast_evaluator.md](baseline_forecast_evaluator.md) |
| AEM WTP/Demand Analysis | Analyze DSO willingness-to-pay patterns and demand heatmaps | [README_aem_wtp_demand_analysis.md](README_aem_wtp_demand_analysis.md) |
| Energy Signature Analyzer | Correlate heat pump power with outdoor temperature | [README_energy_signature_analyzer.md](README_energy_signature_analyzer.md) |
| Heat Pump Analysis | Historical HP power patterns and flexibility heatmaps | [README_heat_pump_analysis.md](README_heat_pump_analysis.md) |
| EV Charger Analysis | EV occupancy patterns and flexibility heatmaps | [README_ev_charger_analysis.md](README_ev_charger_analysis.md) |

Strategy design documents:

- [BIDDING_STRATEGIES_ANALYSIS.md](BIDDING_STRATEGIES_ANALYSIS.md) -- profitability comparison of Strategies 1-5
- [README_strategy_7_analysis.md](README_strategy_7_analysis.md) -- evaluation of the Double Pre-heating strategy
- [README_strategy_qrf.md](README_strategy_qrf.md) -- design for a future QRF-based pay-as-bid strategy
- [QRF_DATA_CONTRACT.md](QRF_DATA_CONTRACT.md) -- data schema for QRF model training

---

## Key Design Decisions

1. **PostgreSQL as the normal trader-to-manager handoff:** Bid records and selected bid assets coordinate accepted-trade delivery. Strategy 12 preparation and cleanup are instead driven by its active configuration and persistent lifecycle ownership.

2. **NODES accepted trades as the normal delivery trigger:** The manager delivers the quantity actually sold, not merely the bid quantity. Strategy 12 prepare/release phases are schedule-driven lifecycle operations rather than sold-flexibility activations.

3. **Strategy awareness through the full pipeline:** The bidding strategy chosen at trading time is persisted in the bid record and honored at activation time, ensuring that only the assets included in the bid are activated.

4. **Separation of baseline and flexibility persistence:** Baseline uploading (`slot_persistence`) and bidding flexibility (`flexibility_method: "persistence"`) are independent configurations that can be mixed with other modes.

5. **Decoupled command consumption:** Command generation in `flexi_manager.py` is separated from consumers through RabbitMQ. The forwarder handles the real path, while the external simulator handles simulated commands.

6. **Discretization awareness:** The engine supports binary, multi-state, and continuous assets. In the current configuration all mappings are discrete: heat pumps are binary and EV chargers use seven OCPP states from 6.93 to 11.0 kW.

7. **Strategy-scoped flexibility methods:** Historical, persistence, recent-profile, and preconditioned-binary strategies coexist without the global persistence fallback changing strategies that omit an explicit method.

8. **Section-owned command routing:** Real commands go through the forwarder, simulated commands go directly to the external simulator, and simulated measurements return through the forwarder.
