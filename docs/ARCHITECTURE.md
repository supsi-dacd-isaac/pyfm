# pyfm Architecture

This document provides a high-level overview of the pyfm system: a Python framework for participating in the Opentunity-CH flexibility market. It describes the full operational pipeline from baseline computation through bidding, activation, and command forwarding, without going into component-level detail. Each section references the dedicated documentation for deeper information.

---

## System Overview

pyfm implements an end-to-end flexibility market workflow for a Flexibility Service Provider (FSP). The FSP manages a portfolio of physical assets (heat pumps and EV chargers) and participates in a 15-minute-resolution energy flexibility market operated on the NODES platform.

The system is organized around four sequential pipeline stages, each implemented as an independent scheduled process:

```
┌──────────────┐     ┌──────────────────┐     ┌──────────────────┐     ┌──────────────────┐
│   BASELINE   │     │  TRADING &       │     │   ACTIVATION     │     │   FORWARDING     │
│   UPDATER    │────▶│  BIDDING         │────▶│   MANAGER        │────▶│   (RabbitMQ +    │
│              │     │                  │     │                  │     │    Forwarder)    │
│ baseline_    │     │ trader_dso.py    │     │ flexi_manager.py │     │ forwarder.py     │
│ updater.py   │     │ trader_fsp.py    │     │                  │     │                  │
└──────────────┘     └──────────────────┘     └──────────────────┘     └──────────────────┘
    InfluxDB             NODES API               PostgreSQL              RabbitMQ
    NODES API            PostgreSQL               NODES API              HTTP / AEM API
                         InfluxDB                 RabbitMQ
```

All components share a common JSON configuration file (e.g. `conf/test_fm01_aem.json`) and a private connections overlay (`conf/private/conns.json`) containing credentials for NODES, PostgreSQL, InfluxDB, and RabbitMQ.

---

## Pipeline Stages

### 1. Baseline

The baseline is the reference power consumption profile for each asset portfolio. It represents what the assets would consume without any flexibility activation. The NODES market uses this baseline to measure the actual flexibility delivered.

**What it does:**

- Queries recent asset power measurements from InfluxDB.
- Computes a per-asset baseline using a configurable strategy (slot persistence or day persistence).
- Aggregates asset-level baselines into a portfolio-level baseline.
- Uploads the baseline to the NODES platform and optionally saves it to InfluxDB.

**Operational mode:** In the current deployment, `slot_persistence` is used. Each 15-minute run computes the next open target slot by reading the measured power at a configured lag (e.g. 90 minutes) and uploading that single baseline interval. Different assets can have different lag overrides to accommodate telemetry latency differences.

**Scheduling:** Runs every 15 minutes, ahead of the trading window, to keep baselines fresh for upcoming market slots.

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
2. Computing available flexibility from the asset portfolio, using either historical patterns or real-time persistence measurements.
3. Applying a bidding strategy that determines which assets to include, the bid quantity, and the price for the current time-of-day window.
4. Posting Sell orders to the NODES market.
5. Recording bid details (strategy, assets, quantities) in PostgreSQL for the activation stage.

**Discretization-aware bidding:** The FSP correctly handles the physical constraints of discrete assets (heat pumps: ON/OFF only) and continuous assets (EV chargers: any power level). Bids reflect only quantities that can actually be delivered, avoiding delivery mismatches.

**Bidding strategies:** The system supports multiple configurable strategies (currently `strategy_1` through `strategy_11`), each defining allowed asset types, time-of-day windows, flexibility targets, and prices. Strategies can use different flexibility methods: historical averages, slot persistence, or recent-profile forecasting.

**Scheduling:** The FSP trader runs every 15 minutes (at minutes :05, :20, :35, :50) and bids 90 minutes ahead of the delivery slot (`fm.ordersTimeShift = 90`).

**Detailed documentation:**

- [README_trader_dso.md](README_trader_dso.md) -- DSO agent configuration, pricing model, and execution steps
- [README_trader_fsp.md](README_trader_fsp.md) -- FSP agent, persistence mode, discretization-aware bidding, strategy selection
- [BIDDING_STRATEGIES.md](BIDDING_STRATEGIES.md) -- full reference for all 11 bidding strategies with configuration examples
- [FLEXIBILITY_ANALYSIS.md](FLEXIBILITY_ANALYSIS.md) -- how available flexibility is calculated (historical and persistence modes)
- [README_simulate_strategy_bidding.md](README_simulate_strategy_bidding.md) -- historical replay tool for strategy evaluation without market interaction

---

### 3. Activation

After the market clears and trades are accepted, the activation stage delivers the promised flexibility by controlling physical assets.

**What it does:**

1. Reads the bid record from PostgreSQL to recover which strategy was used and which assets are allowed.
2. Queries NODES for accepted trades to determine the actual sold quantity (not the bid quantity).
3. Allocates the sold quantity across allowed assets using a modulation-aware algorithm that respects discrete (ON/OFF) and continuous (linear) constraints.
4. Generates curtailment commands (switch OFF for heat pumps, reduce power for EV chargers).
5. Tracks previously controlled assets via a state file and automatically generates restore commands when assets are no longer needed.
6. Publishes all commands to RabbitMQ for downstream forwarding.
7. Records activation details in PostgreSQL (`public.asset_activations`).

**Allocation strategies:** The default `modulation_aware` strategy allocates to discrete assets first using subset-sum optimization, then fills remaining flexibility with continuous assets. Alternative strategies include `proportional`, `priority`, and `cost_optimal`.

**Autonomous mode:** When no bid record exists for a slot, the manager can optionally analyze historical DSO demand patterns to predict upcoming price increases and recommend (or execute) pre-activation of heat pump assets.

**Manual activation:** The `flexi_actuator.py` script provides a one-shot CLI for forcing individual assets on/off without running the full manager workflow.

**Scheduling:** Runs every 15 minutes (at minutes :14, :29, :44, :59), just before the delivery slot begins.

**Detailed documentation:**

- [README_flexi_manager.md](README_flexi_manager.md) -- full activation engine guide, allocation strategies, RabbitMQ integration, state management, and troubleshooting
- [README_flexi_actuator.md](README_flexi_actuator.md) -- manual command publisher for direct asset control
- [RABBITMQ_COMMANDS.md](RABBITMQ_COMMANDS.md) -- command message formats, envelope structure, and RabbitMQ topology

---

### 4. Forwarding

The forwarding layer decouples command generation from physical device actuation using RabbitMQ as a message broker.

**What it does:**

- Consumes control commands from RabbitMQ queues.
- Translates generic curtail/restore commands into protocol-specific HTTP requests for configured targets (e.g. the AEM API).
- Supports dry-run (log only) and live (HTTP POST) modes, with the payload `dry_run` flag providing an additional safety layer.
- Handles retries, timeouts, and per-target configuration.

**RabbitMQ topology:** The system uses three section-based message paths:

| Section | Producer | Consumer | Purpose |
|---------|----------|----------|---------|
| `realAssetCommands` | flexi_manager / flexi_actuator | forwarder | Real physical asset commands |
| `simulatedAssetCommands` | flexi_manager / flexi_actuator | External simulator (not in pyfm) | Commands for simulated assets |
| `simulatedAssetMeasures` | External simulator | forwarder | Simulated measurement results |

Each asset's `rabbitCommandSection` in `asset_mapping` determines which path its commands follow.

**Scheduling:** The forwarder runs as a long-lived consumer process (typically a Docker service), continuously listening for commands.

**Detailed documentation:**

- [README_flexi_forwarder.md](README_flexi_forwarder.md) -- full architecture, target configuration, deployment (Docker and manual), troubleshooting
- [RABBITMQ_COMMANDS.md](RABBITMQ_COMMANDS.md) -- message formats and topology reference

---

## Data Flow and Infrastructure

### External Services

| Service | Role | Used By |
|---------|------|---------|
| **NODES API** | Flexibility market platform (baselines, orders, trades, settlements) | All components |
| **InfluxDB** | Time-series storage for asset measurements and generated baselines | baseline_updater, trader_fsp |
| **PostgreSQL** | Bid records, demand records, market ledger, asset activations | trader_fsp, flexi_manager |
| **RabbitMQ** | Message broker for command forwarding | flexi_manager, forwarder |

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

For a delivery slot at `12:15-12:30 UTC`:

| Wall Clock (UTC) | Component | Action |
|-------------------|-----------|--------|
| ~12:00 (rolling) | `baseline_updater.py` | Uploads baseline for the 12:15 slot using 90-min lagged measurements |
| 10:50 | `trader_fsp.py` | Bids for 12:15 slot (90 min ahead), writes bid record to PostgreSQL |
| 10:50 - 12:14 | NODES market | Clears orders, matches trades |
| 12:14 | `flexi_manager.py` | Reads bid record, queries accepted trades, allocates flexibility, publishes commands |
| 12:14 | `forwarder.py` | Receives commands from RabbitMQ, forwards HTTP requests to AEM API |
| 12:15 - 12:30 | Physical assets | Deliver flexibility (heat pumps OFF, EV chargers reduced) |
| 12:29 | `flexi_manager.py` | Next slot: restores assets no longer needed, activates new ones |

---

## Operational Workflow

The scheduled cron jobs that drive the system are documented in detail in [jobs_shortflex_workflow.md](jobs_shortflex_workflow.md). The key points:

```cron
# Baseline updater (every 15 minutes)
*/15 * * * * .venv/bin/python scripts/baseline_updater.py --config_file conf/test_fm01_aem.json --fsp supsi01

# FSP trader (minutes :05, :20, :35, :50)
5,20,35,50 * * * * .venv/bin/python scripts/trader_fsp.py --config_file conf/test_fm01_aem.json --fsp supsi01 --strategy strategy_4

# Flexibility manager (minutes :14, :29, :44, :59)
14,29,44,59 * * * * .venv/bin/python scripts/flexi_manager.py --config_file conf/test_fm01_aem.json --fsp supsi01 --live --rabbitmq
```

The forwarder runs as a persistent Docker service consuming from `realAssetCommands` and `simulatedAssetMeasures`.

**Detailed documentation:** [jobs_shortflex_workflow.md](jobs_shortflex_workflow.md) -- execution timeline, config dependencies, dry-run vs production behavior, and debugging checklist.

---

## Analysis and Tooling

Beyond the operational pipeline, the repository includes several analysis and evaluation tools:

| Tool | Purpose | Documentation |
|------|---------|---------------|
| Strategy Evaluator | Backtest bidding strategies against historical market data | [README_strategy_evaluator.md](../scripts/README_strategy_evaluator.md) |
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

1. **PostgreSQL as the trader-to-manager handoff:** Bid records in PostgreSQL are the sole coordination mechanism. There is no file-based or message-based handoff between trading and activation.

2. **NODES accepted trades as the activation trigger:** The manager activates flexibility based on what was actually sold (accepted trades), not what was offered (bid quantity). This prevents over-activation.

3. **Strategy awareness through the full pipeline:** The bidding strategy chosen at trading time is persisted in the bid record and honored at activation time, ensuring that only the assets included in the bid are activated.

4. **Separation of baseline and flexibility persistence:** Baseline uploading (`slot_persistence`) and bidding flexibility (`flexibility_method: "persistence"`) are independent configurations that can be mixed with other modes.

5. **Decoupled forwarding:** Command generation (flexi_manager) is separated from physical actuation (forwarder) via RabbitMQ, enabling independent scaling, protocol translation, and robust dry-run testing.

6. **Discretization awareness:** Both the bidding and activation stages correctly handle the physical constraints of ON/OFF assets (heat pumps) vs continuously modulatable assets (EV chargers), preventing delivery mismatches.
