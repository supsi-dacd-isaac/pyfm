# Flexibility Manager (flexi_manager.py)

This script manages the **activation** of flexibility for an FSP. After the FSP has bid and won trades on the flexibility market, this script controls the actual assets to deliver the promised flexibility.

By default the manager now treats **NODES accepted trades** as the authoritative market result. The local `public.market_ledger` table can still be used, but only when explicitly enabled as a fallback for controlled testing.

## Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                      FLEXIBILITY ACTIVATION FLOW                         │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  11:00 (trader_fsp.py)                                                  │
│    │                                                                     │
│    ▼                                                                     │
│  ┌──────────────────────────────────────┐                               │
│  │  BID: 0.015 MW using Strategy 4      │                               │
│  │  → Writes bid record with:           │                               │
│  │    - Strategy: strategy_4            │                               │
│  │    - Allowed: HPs only               │                               │
│  │    - Assets: ECM96.2, ECM97.1, ECM97.2│                              │
│  └──────────────────────────────────────┘                               │
│                          │                                               │
│                          ▼                                               │
│          data/bid_records/supsi01_20260109_1200.json                    │
│                          │                                               │
│                          ▼                                               │
│  11:59 (flexi_manager.py)                                               │
│    │                                                                     │
│    ▼                                                                     │
│  ┌──────────────────────────────────────┐                               │
│  │  ACTIVATE: Read bid record           │                               │
│  │  → Only activate allowed assets      │                               │
│  │  → ECM97.1: 15 kW → OFF (discrete)   │                               │
│  │  → ECM96.2:  4 kW → OFF (discrete)   │                               │
│  │  → Total: 19 kW delivered            │                               │
│  │  → EV chargers: EXCLUDED             │                               │
│  └──────────────────────────────────────┘                               │
│                                                                          │
└─────────────────────────────────────────────────────────────────────────┘
```

## Key Feature: Strategy Awareness via PostgreSQL

The `flexi_manager.py` reads **bid records from PostgreSQL database** (written by `trader_fsp.py`) to know:
- Which **strategy** was used when bidding
- Which **assets** are allowed by that strategy
- How much **quantity** was bid

This ensures that if Strategy 4 (HP Only) was used for bidding, only heat pumps are activated - not EV chargers.

### Database Schema

Bid records are stored in the `public` schema alongside `market_ledger`:

```sql
-- What we intend to bid (created by trader_fsp.py)
public.bid_records (
    id SERIAL PRIMARY KEY,
    fsp_id VARCHAR(100),
    slot_start TIMESTAMP,
    slot_end TIMESTAMP,
    strategy_id VARCHAR(100),
    strategy_name VARCHAR(200),
    total_quantity_mw DECIMAL(10, 6),
    status VARCHAR(50),  -- 'pending', 'activated'
    activated_at TIMESTAMP,
    UNIQUE(fsp_id, slot_start)
)

-- Orders planned for each bid
public.bid_record_orders (
    bid_record_id INTEGER REFERENCES public.bid_records(id),
    regulation_type VARCHAR(50),
    quantity_mw DECIMAL(10, 6),
    ...
)

-- Assets to activate for each bid
public.bid_record_assets (
    bid_record_id INTEGER REFERENCES public.bid_records(id),
    asset_id VARCHAR(100),
    asset_type VARCHAR(100),
    available_flexibility_kw DECIMAL(10, 3),
    ...
)

-- Local market ledger (optional fallback; NODES accepted trades are authoritative)
public.market_ledger (
    id UUID PRIMARY KEY,
    timeslot_market TIMESTAMP,
    player_id VARCHAR,
    ...
    bid_record_id INTEGER REFERENCES public.bid_records(id)  -- Links trade to bid
)
```

### Table Relationships

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         DATABASE RELATIONSHIPS                          │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│   ┌──────────────────┐                                                  │
│   │  bid_records     │ ─── What we PLANNED to bid                      │
│   │  (id, fsp_id,    │                                                  │
│   │   slot_start,    │                                                  │
│   │   strategy_id)   │                                                  │
│   └────────┬─────────┘                                                  │
│            │                                                            │
│      ┌─────┴─────┬─────────────────┐                                   │
│      │           │                 │                                    │
│      ▼           ▼                 ▼                                    │
│   ┌─────────┐ ┌─────────────┐ ┌───────────────┐                        │
│   │ orders  │ │   assets    │ │ market_ledger │                        │
│   │ planned │ │ to activate │ │ opt-in fallback│                       │
│   └─────────┘ └─────────────┘ └───────────────┘                        │
│                                    │                                    │
│                                    │ FK: bid_record_id                  │
│                                    ▼                                    │
│                          Local fallback market rows                     │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

## Usage

### Basic Usage (Dry-Run)

```bash
# Activate virtual environment
source .venv/bin/activate

# Run for upcoming slot (dry-run by default)
cd scripts
python flexi_manager.py --fsp supsi01 --dry-run
```

### Time Offset (Recommended for Production)

```bash
# Check slot from 30 minutes ago
python flexi_manager.py --fsp supsi01 --offset 30m --dry-run

# Check slot from 2 hours ago
python flexi_manager.py --fsp supsi01 --offset 2h --dry-run

# Check slot from 1 hour 30 minutes ago
python flexi_manager.py --fsp supsi01 --offset 1h30m --dry-run
```

The `--offset` parameter calculates the slot by going back from the current time and aligning to the 15-minute boundary.

### Specific Time Slot

```bash
# Manage a specific slot (exact time)
python flexi_manager.py --fsp supsi01 --slot "2026-01-09T12:00:00" --dry-run
```

### Live Mode (CAUTION!)

```bash
# Actually send control commands
python flexi_manager.py --fsp supsi01 --live
```

### Dry-Run Activation Simulation

```bash
# Exercise the activation path with a simulated accepted quantity
python flexi_manager.py --fsp supsi01 --dry-run --simulate-sold-mw 0.015
```

`--simulate-sold-mw` is dry-run only. It bypasses both NODES and `market_ledger`, creates an in-memory simulated sell trade for the target slot, and is intended for allocation/control-path testing without depending on market results.

### Market Ledger Fallback

```bash
# Prefer NODES accepted trades, but fall back to local market_ledger if none are found
python flexi_manager.py --fsp supsi01 --dry-run --allow-market-ledger-fallback
```

Without `--allow-market-ledger-fallback`, a slot with no accepted NODES trades results in no activation. Use the fallback only when operations guarantee that local `market_ledger` rows represent confirmed market results, because they may otherwise be posted orders rather than accepted/cleared trades.

### Different Allocation Strategies

```bash
# Modulation-aware (default): respects discrete (ON/OFF) vs continuous assets
python flexi_manager.py --fsp supsi01 --allocation modulation_aware

# Proportional: distribute based on capacity (legacy, ignores modulation constraints)
python flexi_manager.py --fsp supsi01 --allocation proportional

# Priority: fill HPs first, then EVs
python flexi_manager.py --fsp supsi01 --allocation priority

# Cost-optimal: minimize activation costs
python flexi_manager.py --fsp supsi01 --allocation cost_optimal
```

### Strategy Utilities

```bash
# List configured bidding strategies and exit
python flexi_manager.py --fsp supsi01 --list-strategies

# Use a configured strategy as a fallback asset filter when no bid record exists
python flexi_manager.py --fsp supsi01 --fallback-strategy strategy_4 --dry-run
```

### Save Output to File

```bash
python flexi_manager.py --fsp supsi01 --dry-run --output activation_result.json
```

### Autonomous Mode

```
python flexi_manager.py --fsp supsi01 --dry-run --autonomous
python flexi_manager.py --fsp supsi01 --dry-run --autonomous --autonomous-lookahead 6 --autonomous-history 14
python flexi_manager.py --fsp supsi01 --dry-run --autonomous --no-autonomous  # Disable config-driven autonomous  mode
```

When no bid record exists for the slot, autonomous mode lets the manager **analyze historical DSO demand** from `demand_records` and predict the next few hours (default 3h) of willingness to pay. It prints:
1. The current DSO request (price/quantity) if any
2. A table of avg/min/max/std per 15-minute slot for the lookahead window
3. Overall statistics (avg/min/max/std) and data coverage
4. A recommendation whether to pre-activate assets (e.g., pre-heat HPs) based on a configurable price increase threshold
5. A list of suitable heat pump assets that can be turned ON ahead of a peak

Configuration defaults live in `conf/test_fm01_aem.json` under the new `autonomous` section:

```json
"autonomous": {
  "enabled": true,
  "lookahead_hours": 3,
  "historical_days": 7,
  "price_increase_threshold_pct": 20,
  "dso_id": "AEM",
  "preactivation_enabled": false
}
```

Autonomous mode can publish actual pre-activation commands when you run with `--live` and `autonomous.preactivation_enabled` is `true`. For the AEM setup, keep the analysis but suppress pre-heating by setting `preactivation_enabled` to `false`.

---

## Command Line Arguments

| Argument        | Short | Description | Default |
|-----------------|-------|-------------|---------|
| `--config_file` | `-c` | Configuration file path | `../conf/test_fm01_aem.json` |
| `--fsp`         | `-f` | FSP identifier (required) | - |
| `--slot`        | `-s` | Target slot start (ISO format) | Next 15-min slot |
| `--offset`      | `-t` | Time offset from now (e.g., `30m`, `2h`, `1h30m`) | - |
| `--dry-run`     | `-d` | Simulate only | Yes |
| `--live`        | `-l` | Send actual commands | No |
| `--simulate-sold-mw` | - | Dry-run-only accepted quantity simulation in MW | - |
| `--allow-market-ledger-fallback` | - | Allow local `public.market_ledger` fallback when NODES has no accepted trades | No |
| `--allocation`  | `-a` | Allocation strategy | `modulation_aware` |
| `--fallback-strategy` | - | Strategy to use when no bid record exists | - |
| `--list-strategies` | - | List configured strategies and exit | No |
| `--state-file`  | - | Path to the controlled-asset state file | `logs/flexi_manager_state.json` |
| `--log-level`   | - | Logging verbosity | `INFO` |
| `--log_file`    | - | Path to log file | - |
| `--output`      | `-o` | JSON output file | - |
| `--rabbitmq`    | - | Enable RabbitMQ command publishing | No |
| `--rabbitmq-host` | - | RabbitMQ hostname | `localhost` |
| `--rabbitmq-port` | - | RabbitMQ port | `5672` |
| `--rabbitmq-user` | - | RabbitMQ username | `guest` |
| `--rabbitmq-pass` | - | RabbitMQ password | `guest` |
| `--rabbitmq-vhost` | - | RabbitMQ virtual host | `/` |
| `--rabbitmq-exchange` | - | Deprecated and ignored; exchanges come only from RabbitMQ destination sections | - |
| `--autonomous` | - | Enable autonomous analysis when no activation is required | From config |
| `--no-autonomous` | - | Disable autonomous analysis, overriding config | From config |
| `--autonomous-lookahead` | - | Hours to forecast for autonomous analysis | From config or `3` |
| `--autonomous-history` | - | Historical days to analyze | From config or `7` |

### Offset Format Examples

| Format | Meaning |
|--------|---------|
| `30m` | 30 minutes ago |
| `2h` | 2 hours ago |
| `1h30m` | 1 hour 30 minutes ago |
| `90` | 90 minutes ago (default unit is minutes) |

---

## How It Works

### Step 0: Load Bid Record from Database

First, the script queries PostgreSQL for a bid record from `trader_fsp.py`:

```sql
SELECT * FROM public.bid_records
WHERE fsp_id = 'supsi01' AND slot_start = '2026-01-09 12:00:00'
```

The record contains:
| Field | Value |
|-------|-------|
| strategy_id | strategy_4 |
| strategy_name | Hybrid (S3+S1) |
| total_quantity_mw | 0.015 |
| status | pending |

Related assets from `public.bid_record_assets`:
| asset_id | asset_type | available_flexibility_kw |
|----------|------------|--------------------------|
| ECM96.2 | heat_pump | 1.65 |
| ECM97.1 | heat_pump | 2.64 |
| ECM97.2 | heat_pump | 9.58 |

**If no bid record is found:** The script does not activate new curtailable assets from the bid path. It may still restore assets that were controlled in a previous slot, and if autonomous mode is enabled it can run price-forecast analysis and optionally queue pre-activation commands for heat pumps.

### Step 1: Query Market Results

The script queries for actual accepted trades in this order:

1. **Dry-run simulation** if `--simulate-sold-mw` is provided
2. **NODES API accepted sell trades** for the organization and slot
3. **Local `public.market_ledger` fallback** only if `--allow-market-ledger-fallback` is set

**Only actual trades trigger activation.** The bid record quantity (what we offered) is NOT used - only what was actually accepted matters.

```python
trades = market_handler.get_accepted_trades_for_slot(org_id, slot_start, slot_end)
total_sold_mw = sum(t.get("quantity", 0) for t in trades)

# If no trades found -> NO activation
if total_sold_mw == 0:
    return "no_trades"
```

For NODES results, the manager queries the `trades` endpoint with the slot time range, then filters locally for:

- `side == "Sell"`
- matching organization when organization identifiers are present
- matching slot boundaries when trade timestamps are present
- accepted/cleared/executed/filled/settled status values
- positive quantity

The local filtering is intentional: server-side organization and status filters have returned HTTP 400 for this endpoint in practice.

### Step 2: Allocate Flexibility

The total sold flexibility is distributed across **allowed assets only**, respecting each asset's **modulation type**:

| Strategy | Description |
|----------|-------------|
| `modulation_aware` | **Default.** Smart allocation respecting discrete (ON/OFF) vs continuous assets |
| `proportional` | Distribute based on capacity × flexibility_factor (legacy) |
| `priority` | Fill HPs first (more reliable), then EVs |
| `cost_optimal` | Minimize total activation cost |

#### Modulation Types

Different assets have different control capabilities:

| Modulation Type | Description | Curtailment | Example Assets |
|-----------------|-------------|-------------|----------------|
| `continuous` | Can modulate power linearly (any value from min to max) | Any value in range | EV chargers (0-11 kW) |
| `discrete` | Can only switch between fixed states (ON/OFF) | **Full capacity** when OFF | Heat pumps |

**Important for discrete assets:** When a heat pump is switched OFF, the curtailment delivered is the **full capacity** (e.g., 15 kW), not `capacity × flexibility_factor`. The `flexibility_factor` for discrete assets represents *availability probability* for bidding purposes, not the amount of power curtailed.

The `modulation_aware` strategy:
1. Allocates to **discrete assets first** using subset-sum optimization
2. Fills remaining flexibility with **continuous assets** proportionally
3. May result in **over-delivery** if only discrete assets are available

**Example allocation for 15 kW (with modulation_aware, discrete-only):**
```
Target: 15 kW
Available discrete assets:
  - ECM97.1 (HP Cinema 1): 15 kW capacity
  - ECM97.2 (HP Cinema 2): 15 kW capacity  
  - ECM96.2 (HP Small):     4 kW capacity

Best combination: ECM97.1 (15 kW) + ECM96.2 (4 kW) = 19 kW
Result: +4 kW over-delivery due to discrete constraints
```

**Example allocation for 30 kW (with modulation_aware):**
```
Discrete assets (ON/OFF):
  ECM97.2 (HP Cinema 2): 15.0 kW → Switch OFF
  ECM97.1 (HP Cinema 1): 15.0 kW → Switch OFF
  Total: 30 kW (exact match)

Continuous assets (if needed for remaining):
  ECM63.1 (EV Charger 1): 0 kW   → No change needed
```

#### Persistence Strategies

If the bid strategy has `flexibility_method: "persistence"`, allocation does **not** use the generic allocator. Instead, the manager reconstructs the activation plan from `public.bid_record_assets.available_flexibility_kw`, using only assets with positive stored bid-time flexibility.

Persistence activation is conservative:

1. Discrete assets are selected as whole assets using the stored bid-time kW values, without exceeding the accepted quantity when possible.
2. Continuous assets fill the remaining accepted quantity, capped by their stored bid-time plan.
3. If the accepted quantity covers the full stored plan, all stored assets are activated.
4. If no positive bid asset rows exist, the manager fails closed with `persistence_no_positive_bid_assets` and performs no new activation.

For persistence strategies, any selected discrete asset with positive activation kW is forced **OFF**. This avoids the normal 50% capacity threshold from leaving a selected heat pump ON when the stored bid-time flexibility is smaller than its configured capacity.

### Step 3: Control Assets

Send curtailment commands to each asset based on modulation type:

| Asset Type | Modulation | Control Command |
|------------|------------|-----------------|
| Heat Pump | Discrete (ON/OFF) | `set_state: OFF` or `set_state: ON` |
| EV Charger | Continuous | `set_charging_limit: 6.5 kW` |

| Asset Type | Control Methods |
|------------|----------------|
| Heat Pump | MQTT, HTTP API, Modbus |
| EV Charger | OCPP, HTTP API |

When RabbitMQ is enabled (`--rabbitmq`), the manager also queues **restore commands** for any previously controlled assets that are no longer selected for the current slot (see [Previously Controlled Assets](#previously-controlled-assets) below).

### Step 4: Publish to RabbitMQ

When `--rabbitmq` is active, all pending commands (curtailment + restore) are
published through the RabbitMQ destination selected by each asset's
`asset_mapping.<asset>.rabbitCommandSection`. Each command payload is enriched with:

- **Clean `slot_start` / `slot_end`** timestamps (`YYYY-MM-DDTHH:MM:SS`, no timezone suffix, no microseconds)
- **`dry_run`** flag
- **EV `schedule`** dictionary (for EV charger commands only)

The destination exchange, queue, and routing key are read from the `rabbitMQ`
section in the configured `connectionsFile`.

---

## RabbitMQ Command Forwarding

### Architecture

```
Real asset command path:
flexi_manager.py / flexi_actuator.py
  -> rabbitMQ.realAssetCommands
  -> forwarder.py
  -> real API / AEM API

Simulated asset path:
flexi_manager.py / flexi_actuator.py
  -> rabbitMQ.simulatedAssetCommands
  -> external simulator application
  -> rabbitMQ.simulatedAssetMeasures
  -> forwarder.py
  -> downstream API / measurement target
```

The **manager** decides **what** should happen for the next market slot. For
real assets, the **forwarder** decides **how** to translate that into
AEM-specific HTTP requests. For simulated assets, commands go first to an
external simulator application; that simulator is not implemented in `pyfm`.
`pyfm` only publishes simulated asset commands and can consume simulated
measures produced by the external simulator.

### Command Destinations

Asset commands require an explicit RabbitMQ command section in `asset_mapping`:

```json
{
  "asset_mapping": {
    "ECM96.2": {
      "type": "heat_pump",
      "rabbitCommandSection": "realAssetCommands"
    },
    "ECM68.3": {
      "type": "heat_pump",
      "rabbitCommandSection": "simulatedAssetCommands"
    }
  }
}
```

Allowed values are `realAssetCommands` and `simulatedAssetCommands`.
`simulatedAssetMeasures` is not valid for commands.

The section name selects a destination from `connectionsFile`:

```json
{
  "rabbitMQ": {
    "host": "localhost",
    "port": 5672,
    "username": "guest",
    "password": "guest",
    "virtualHost": "/",
    "realAssetCommands": {
      "exchange": "flexi_commands",
      "queue": "flexi_commands_queue",
      "routingKey": "real_asset.command"
    },
    "simulatedAssetCommands": {
      "exchange": "flexi_sim_commands",
      "queue": "flexi_sim_commands_queue",
      "routingKey": "sim_asset.command"
    },
    "simulatedAssetMeasures": {
      "exchange": "flexi_sim_measures",
      "queue": "flexi_sim_measures_queue",
      "routingKey": "sim_asset.measure"
    }
  }
}
```

Section responsibilities:

| Section | Producer | Consumer | Purpose |
|---------|----------|----------|---------|
| `rabbitMQ.realAssetCommands` | `flexi_manager.py` / `flexi_actuator.py` | `forwarder.py` | Real physical asset commands |
| `rabbitMQ.simulatedAssetCommands` | `flexi_manager.py` / `flexi_actuator.py` | External simulator application | Commands for simulated assets |
| `rabbitMQ.simulatedAssetMeasures` | External simulator application | `forwarder.py` | Simulated measurements/results |

The standard forwarder deployment should use
`FORWARDER_RABBIT_SECTIONS=realAssetCommands,simulatedAssetMeasures` and should
not consume `simulatedAssetCommands`.

If `rabbitCommandSection` is missing, invalid, points to a missing `rabbitMQ`
section, or selects a section missing `exchange`, `queue`, or `routingKey`, the
manager logs a warning and skips that asset command. There is no implicit
fallback to the old `commands.{asset_type}.{asset_id}` route.

`rabbitCommandSection` controls command publishing only. Simulated measurements
are produced by the external simulator, not by `flexi_manager.py`.

### Timestamp Formatting

All timestamps in command payloads use a clean UTC format:

```
YYYY-MM-DDTHH:MM:SS
```

- No timezone suffix (`+00:00`, `Z`)
- No microseconds
- No arbitrary seconds
- Seconds always `:00`

Example: `2026-04-27T12:15:00`

### HP Curtailment Payload

When a heat pump is curtailed (switched OFF) for a market slot:

```json
{
  "asset_id": "ECM96.2",
  "asset_type": "heat_pump",
  "modulation_type": "discrete",
  "discrete_state": "OFF",
  "target_power_kw": 0.0,
  "capacity_kw": 4.0,
  "slot_start": "2026-04-27T12:15:00",
  "slot_end": "2026-04-27T12:30:00",
  "dry_run": false
}
```

### HP Restore Payload

When a heat pump is restored (switched back ON) for the next slot:

```json
{
  "asset_id": "ECM96.2",
  "asset_type": "heat_pump",
  "target_state": "ON",
  "discrete_state": "ON",
  "target_power_kw": 4.0,
  "capacity_kw": 4.0,
  "slot_start": "2026-04-27T12:30:00",
  "slot_end": "2026-04-27T12:45:00",
  "dry_run": false
}
```

**Important:** An AEM HP `force_off` command may persist longer than the 15-minute market slot (~1 hour). The manager therefore acts as a **slot-based desired-state publisher**: if flexibility is still needed in the next slot, it sends OFF again; if not, it sends ON/restore.

### EV Curtailment Payload

When an EV charger is curtailed for a market slot:

```json
{
  "asset_id": "ECM63.1",
  "asset_type": "ev_charger",
  "target_power_kw": 0.0,
  "power_kw": 0.0,
  "slot_start": "2026-04-27T12:15:00",
  "slot_end": "2026-04-27T12:30:00",
  "schedule": {
    "2026-04-27T12:15:00": 0.0
  },
  "dry_run": false
}
```

The `schedule` dictionary contains one entry every 15 minutes from `slot_start` up to (but not including) `slot_end`. For a standard 15-minute slot this is a single entry. For a 30-minute slot it would be two entries.

### EV Restore Payload

When an EV charger is restored to normal charging:

```json
{
  "asset_id": "ECM63.1",
  "asset_type": "ev_charger",
  "target_power_kw": 6.0,
  "power_kw": 6.0,
  "slot_start": "2026-04-27T12:30:00",
  "slot_end": "2026-04-27T12:45:00",
  "schedule": {
    "2026-04-27T12:30:00": 6.0
  },
  "dry_run": false
}
```

The restore power is chosen using this fallback chain:

1. `restore_power_kw` (explicit restore power in asset config)
2. `default_power_kw` (default charging power)
3. `capacity_kw` (full capacity)

### Dry-Run Behaviour

| Mode | RabbitMQ enabled | What happens |
|------|------------------|--------------|
| `--dry-run` | No | Commands logged locally, nothing sent |
| `--dry-run` | Yes (`--rabbitmq`) | Commands with a valid `rabbitCommandSection` are published to RabbitMQ with `dry_run=true`; real-asset commands are consumed by the forwarder, simulated-asset commands are consumed by the external simulator |
| `--live` | No | Direct local actuation (MQTT/HTTP/OCPP/simulation) |
| `--live` | Yes (`--rabbitmq`) | Commands with a valid `rabbitCommandSection` are published to RabbitMQ with `dry_run=false`; real-asset commands can be forwarded to AEM, simulated-asset commands go to the external simulator |

### Slot Timing

The manager is intended to run shortly before the next quarter-hour slot:

| Run time | Prepares commands for slot |
|----------|---------------------------|
| 12:14 | 12:15 – 12:30 |
| 12:29 | 12:30 – 12:45 |
| 12:44 | 12:45 – 13:00 |

All command timestamps use the **market slot** (`slot_start`, `slot_end`), not "next quarter-hour from now". If the manager is late and `slot_start <= now`, it logs a warning but does **not** silently shift the slot, because shifting would break the market-cleared activation interval.

---

## Previously Controlled Assets

The manager maintains a lightweight JSON state file to track which assets are currently under control. This enables **automatic restore** when an asset is no longer selected for the next slot.

### State File

Default path: `logs/flexi_manager_state.json` (configurable with `--state-file`).

Example contents after curtailing two assets:

```json
{
  "ECM96.2": {
    "asset_type": "heat_pump",
    "slot_start": "2026-04-27T12:15:00",
    "slot_end": "2026-04-27T12:30:00"
  },
  "ECM63.1": {
    "asset_type": "ev_charger",
    "slot_start": "2026-04-27T12:15:00",
    "slot_end": "2026-04-27T12:30:00"
  }
}
```

### Restore Logic

On each run the manager:

1. **Loads** the previous state file.
2. **Determines** which assets are selected for curtailment in the current slot.
3. For each previously controlled asset that is **not** in the current selection: queues a **restore command** for the current slot.
4. **Publishes** all commands (curtailment + restore) to RabbitMQ.
5. **Saves** the new state (only currently curtailed assets).

This handles three scenarios:

| Scenario | Previous state | Current selection | Action |
|----------|---------------|-------------------|--------|
| No trades | ECM96.2=OFF | (empty) | Restore ECM96.2 to ON |
| Asset rotated out | ECM96.2=OFF, ECM97.1=OFF | ECM97.1 only | Restore ECM96.2, keep ECM97.1 OFF |
| All assets still needed | ECM96.2=OFF | ECM96.2 | Re-send ECM96.2 OFF for new slot |

If the state file cannot be read (missing, corrupted), the manager logs a warning and continues with an empty state. If it cannot be written, it logs an error but does not crash after commands were already published.

---

## Example Output

### Modulation-Aware Allocation with Over-Delivery

When the target doesn't exactly match available discrete combinations, the system reports over-delivery:

```
2026-01-15 07:30:00::INFO::run::======================================================================
2026-01-15 07:30:00::INFO::run::FLEXIBILITY MANAGER - supsi01
2026-01-15 07:30:00::INFO::run::======================================================================
2026-01-15 07:30:00::INFO::run::Target slot: 2026-01-15 07:30 - 07:45
2026-01-15 07:30:00::INFO::run::Mode: DRY-RUN
2026-01-15 07:30:00::INFO::run::Allocation strategy: modulation_aware
2026-01-15 07:30:00::INFO::run::----------------------------------------------------------------------
2026-01-15 07:30:00::INFO::run::Step 1: Querying market results...
2026-01-15 07:30:01::INFO::run::Querying NODES accepted trades before any local ledger fallback...
2026-01-15 07:30:01::INFO::get_accepted_trades_for_slot::Kept 1 accepted sell trades for organization <org_id> and requested slot
2026-01-15 07:30:01::INFO::run::MARKET RESULT SOURCE: NODES accepted trades
2026-01-15 07:30:01::INFO::run::----------------------------------------------------------------------
2026-01-15 07:30:01::INFO::run::Total flexibility to deliver: 0.015 MW (15.00 kW)
2026-01-15 07:30:01::INFO::run::----------------------------------------------------------------------
2026-01-15 07:30:01::INFO::run::Step 2: Allocating flexibility across ALLOWED assets...
2026-01-15 07:30:01::INFO::allocate_flexibility::Allocating 15.00 kW flexibility using 'modulation_aware' strategy
2026-01-15 07:30:01::INFO::_allocate_modulation_aware::Modulation-aware allocation: 0 continuous, 3 discrete assets
2026-01-15 07:30:01::INFO::_allocate_discrete_subset::Discrete subset allocation: target=15.00 kW, selected 2 assets to switch OFF, total curtailment=19.00 kW (+4.00 kW over-delivery due to discretization)
2026-01-15 07:30:01::INFO::_allocate_modulation_aware::Modulation-aware final: requested=15.00 kW, will deliver=19.00 kW (+4.00 kW / +26.7% over-delivery due to discrete assets)
2026-01-15 07:30:01::INFO::run::----------------------------------------------------------------------
2026-01-15 07:30:01::INFO::run::Allocation plan:
2026-01-15 07:30:01::INFO::run::  ECM97.1 (HP Cinema 1): 15.00 kW [discrete → OFF]
2026-01-15 07:30:01::INFO::run::  ECM96.2 (HP Small): 4.00 kW [discrete → OFF]
2026-01-15 07:30:01::INFO::run::  Total to deliver: 19.00 kW
2026-01-15 07:30:01::INFO::run::  Note: +4.00 kW over-delivery due to discrete asset constraints
2026-01-15 07:30:01::INFO::run::----------------------------------------------------------------------
2026-01-15 07:30:01::INFO::run::Step 3: Sending control commands...
2026-01-15 07:30:01::INFO::curtail_asset::[DRY-RUN] Would set ECM97.1 (HP Cinema 1) to OFF (target: 0.00 kW) for 15 minutes
2026-01-15 07:30:01::INFO::curtail_asset::[DRY-RUN] Would set ECM96.2 (HP Small) to OFF (target: 0.00 kW) for 15 minutes
2026-01-15 07:30:01::INFO::run::======================================================================
2026-01-15 07:30:01::INFO::run::FLEXIBILITY ACTIVATION COMPLETE
2026-01-15 07:30:01::INFO::run::======================================================================
2026-01-15 07:30:01::INFO::run::Assets controlled: 2 successful, 0 failed
2026-01-15 07:30:01::INFO::run::Total flexibility delivered: 19.00 kW (0.019 MW)
```

### Exact Match (30 kW = 15 + 15)

When discrete combinations match the target exactly:

```
2026-01-09 11:59:01::INFO::allocate_flexibility::Allocating 30.00 kW flexibility using 'modulation_aware' strategy
2026-01-09 11:59:01::INFO::_allocate_modulation_aware::Modulation-aware allocation: 0 continuous, 3 discrete assets
2026-01-09 11:59:01::INFO::_allocate_discrete_subset::Discrete subset allocation: target=30.00 kW, selected 2 assets to switch OFF, total curtailment=30.00 kW 
2026-01-09 11:59:01::INFO::_allocate_modulation_aware::Modulation-aware final: requested=30.00 kW, will deliver=30.00 kW (exact match)
2026-01-09 11:59:01::INFO::run::----------------------------------------------------------------------
2026-01-09 11:59:01::INFO::run::Allocation plan:
2026-01-09 11:59:01::INFO::run::  ECM97.2 (HP Cinema 2): 15.00 kW [discrete → OFF]
2026-01-09 11:59:01::INFO::run::  ECM97.1 (HP Cinema 1): 15.00 kW [discrete → OFF]
2026-01-09 11:59:01::INFO::run::  Total to deliver: 30.00 kW
```

### Mixed Assets (HPs + EV Chargers)

When both discrete and continuous assets are available, continuous assets fill the gap precisely:

```
2026-01-09 11:59:01::INFO::_allocate_modulation_aware::Modulation-aware allocation: 2 continuous, 3 discrete assets
2026-01-09 11:59:01::INFO::_allocate_discrete_subset::Discrete subset allocation: target=20.00 kW, selected 1 assets to switch OFF, total curtailment=15.00 kW 
2026-01-09 11:59:01::INFO::_allocate_modulation_aware::Discrete allocation: 15.00 kW from 1 assets, remaining: 5.00 kW
2026-01-09 11:59:01::INFO::_allocate_proportional::Proportional allocation: {'ECM63.1': 3.5, 'ECM63.2': 1.5}
2026-01-09 11:59:01::INFO::_allocate_modulation_aware::Modulation-aware final: requested=20.00 kW, will deliver=20.00 kW (exact match)
2026-01-09 11:59:01::INFO::run::----------------------------------------------------------------------
2026-01-09 11:59:01::INFO::run::Allocation plan:
2026-01-09 11:59:01::INFO::run::  ECM97.2 (HP Cinema 2): 15.00 kW [discrete → OFF]
2026-01-09 11:59:01::INFO::run::  ECM63.1 (EV Charger 1): 3.50 kW [continuous → limit 7.50 kW]
2026-01-09 11:59:01::INFO::run::  ECM63.2 (EV Charger 2): 1.50 kW [continuous → limit 9.50 kW]
2026-01-09 11:59:01::INFO::run::  Total to deliver: 20.00 kW
2026-01-09 11:59:01::INFO::run::----------------------------------------------------------------------
2026-01-09 11:59:01::INFO::run::Step 3: Sending control commands...
2026-01-09 11:59:01::INFO::curtail_asset::[DRY-RUN] Would set ECM97.2 (HP Cinema 2) to OFF (target: 0.00 kW) for 15 minutes
2026-01-09 11:59:01::INFO::curtail_asset::[DRY-RUN] Would curtail ECM63.1 (EV Charger 1): 3.50 kW (target: 7.50 kW) for 15 minutes
2026-01-09 11:59:01::INFO::curtail_asset::[DRY-RUN] Would curtail ECM63.2 (EV Charger 2): 1.50 kW (target: 9.50 kW) for 15 minutes
```

---

## Configuration

### Asset Control Configuration

To enable actual asset control, configure each asset in `asset_mapping` with:
- **Modulation type**: `discrete` (ON/OFF) or `continuous` (linear)
- **Control interface**: How to send commands to the device
- **RabbitMQ command section**: `realAssetCommands` or `simulatedAssetCommands`
  when commands should be published through `--rabbitmq`

```json
{
  "asset_mapping": {
    "ECM97.1": {
      "device_name_tag": "shelly_3em_pro_heat_pump_1",
      "type": "heat_pump",
      "description": "HP Cinema 1",
      "capacity_kw": 15.0,
      "flexibility_factor": 0.85,
      "rabbitCommandSection": "realAssetCommands",
      "modulation_type": "discrete",
      "discrete_states_kw": [0.0, 15.0],
      "control": {
        "type": "mqtt",
        "topic": "assets/ECM97.1/control",
        "broker": "mqtt://localhost:1883"
      }
    },
    "ECM63.1": {
      "device_name_tag": "charge_point_ev_1",
      "type": "ev_charger",
      "description": "EV Charger 1",
      "capacity_kw": 11.0,
      "flexibility_factor": 0.70,
      "rabbitCommandSection": "realAssetCommands",
      "modulation_type": "continuous",
      "min_power_kw": 0.0,
      "restore_power_kw": 6.0,
      "control": {
        "type": "ocpp",
        "charger_id": "CP001",
        "endpoint": "ws://ocpp.server.local:9000"
      }
    }
  }
}
```

### Modulation Configuration

| Field | Type | Description | Default |
|-------|------|-------------|---------|
| `modulation_type` | string | `"continuous"` or `"discrete"` | By asset type* |
| `discrete_states_kw` | array | Valid power states for discrete assets | `[0, capacity_kw]` |
| `min_power_kw` | number | Minimum power for continuous assets | `0.0` |
| `restore_power_kw` | number | EV restore power (highest priority) | - |
| `default_power_kw` | number | EV default charging power (second priority) | - |
| `rabbitCommandSection` | string | RabbitMQ command destination section: `realAssetCommands` or `simulatedAssetCommands` | No command sent when omitted |

*Default by type: `heat_pump` → `discrete`, `ev_charger` → `continuous`

The EV restore power fallback chain is: `restore_power_kw` → `default_power_kw` → `capacity_kw`.

**Example discrete states:**
```json
// Simple ON/OFF heat pump
"discrete_states_kw": [0.0, 15.0]

// 2-stage compressor (OFF / LOW / HIGH)
"discrete_states_kw": [0.0, 7.5, 15.0]
```

### Supported Control Types

| Type | Asset Types | Description |
|------|-------------|-------------|
| `mqtt` | HP, EV | Publish JSON to MQTT topic |
| `http` | HP, EV | POST to REST API endpoint |
| `ocpp` | EV | OCPP SetChargingProfile |
| `simulation` | All | Log only (default) |

Direct local `mqtt`, `http`, and `ocpp` handlers currently log the intended action and are placeholders for protocol-specific clients. The production AEM path is `--rabbitmq` -> `forwarder.py`, where HTTP target forwarding is implemented from target configuration.

---

## Allocation Strategies

### 1. Modulation-Aware (Default) ⭐

Smart allocation that respects physical constraints of each asset:

```
1. Separate assets into DISCRETE (ON/OFF) and CONTINUOUS (linear)
2. Allocate to discrete assets using subset-sum optimization
3. Fill remaining with continuous assets proportionally
```

**How it works:**
- **Discrete assets** (heat pumps): Deliver **full capacity** when switched OFF (e.g., 15 kW HP → 15 kW curtailment)
- **Continuous assets** (EV chargers): Can deliver any amount within their range

**Example 1:** Target = 20 kW (with mixed assets)
```
Available:
  - HP Cinema 2: 15 kW (discrete ON/OFF)
  - EV Charger 1: 0-11 kW (continuous)

Allocation:
  - HP Cinema 2 → OFF (15 kW)
  - EV Charger 1 → limit to 6 kW (5 kW reduction)
  Total: 20 kW ✓ (exact match)
```

**Example 2:** Target = 15 kW (discrete-only, over-delivery)
```
Available discrete only:
  - HP Cinema 1: 15 kW
  - HP Cinema 2: 15 kW
  - HP Small: 4 kW

Best combination: HP Cinema 1 (15 kW) + HP Small (4 kW) = 19 kW
Result: +4 kW over-delivery (unavoidable with ON/OFF assets)
```

**Pros:** Respects physical constraints, transparent about over/under-delivery
**Cons:** May over-deliver when only discrete assets available (clearly reported in logs)

### 2. Proportional (Legacy)

Distributes flexibility based on each asset's share of total capacity:

```
share = (capacity × flex_factor) / total_capacity
allocation = share × total_required
```

**Pros:** Fair distribution, all assets contribute
**Cons:** Ignores modulation constraints - may request impossible values from ON/OFF assets

### 3. Priority

Fills assets in order of reliability:
1. Heat pumps (most reliable)
2. EV chargers (dependent on car presence)

**Pros:** Maximizes delivery probability
**Cons:** Ignores modulation constraints

### 4. Cost-Optimal

Fills cheapest assets first (based on `activation_cost_per_kw`):

```json
"ECM97.1": {
  "activation_cost_per_kw": 0.10  // CHF per kW per 15min
}
```

**Pros:** Minimizes activation costs
**Cons:** Ignores modulation constraints

---

## Integration with trader_fsp.py

The typical workflow is:

1. **trader_fsp.py** runs at e.g., 11:00 to bid for 12:00 slot
   - Uses strategy (e.g., strategy_4 = HP only)
   - Saves bid record to PostgreSQL database (`public.bid_records`)
2. Market clears, trades are matched
3. **flexi_manager.py** runs at 11:59 to activate flexibility for 12:00 slot
   - Reads bid record from database to know which assets to activate
   - Only activates assets allowed by the strategy
   - Marks bid record as `activated` after completion

```bash
# Cron example
# trader_fsp.py runs at minute 0 to bid for slot starting at minute 30 (90-min ahead)
0 * * * *  cd /path/to/pyfm && .venv/bin/python scripts/trader_fsp.py --config conf/test_fm01_aem.json --fsp supsi01

# flexi_manager.py runs at minute 14, 29, 44, 59 to activate the upcoming slot.
# With --rabbitmq, real asset commands go through RabbitMQ -> forwarder -> AEM.
# Simulated asset commands go through RabbitMQ -> external simulator.
14,29,44,59 * * * * cd /path/to/pyfm && .venv/bin/python scripts/flexi_manager.py --fsp supsi01 --live --rabbitmq
```

### Database Tables

Records are stored in PostgreSQL:

```
public.bid_records           # Main record (one per FSP per slot) - created by trader_fsp.py
public.bid_record_orders     # Orders placed for this bid
public.bid_record_assets     # Assets to activate
public.asset_activations     # Actual activation records - created by flexi_manager.py
```

The `asset_activations` table stores every activation command sent:

| Column | Description |
|--------|-------------|
| `fsp_id` | FSP identifier |
| `slot_start` / `slot_end` | Time slot |
| `asset_id` | Asset identifier (e.g., ECM97.1) |
| `asset_description` | Human-readable description |
| `asset_type` | heat_pump, ev_charger |
| `modulation_type` | discrete, continuous |
| `discrete_state` | ON/OFF (for discrete assets) |
| `requested_curtailment_kw` | Requested power reduction |
| `actual_curtailment_kw` | Actual power reduction (may differ for discrete) |
| `target_power_kw` | Target power setpoint |
| `percentage_of_capacity` | % of asset capacity |
| `allocation_strategy` | modulation_aware, proportional, priority, cost_optimal |
| `dry_run` | TRUE if simulated |
| `activation_status` | success, failed, simulated |
| `bid_record_id` | Link to originating bid |

Tables are automatically created on first run.

---

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success (or no flexibility to deliver) |
| 1 | Partial success (some assets failed) |
| 2 | Failure (allocation or critical error) |

---

## Troubleshooting

### "No bid record found for this slot"

This is expected behavior when no bid was placed by `trader_fsp.py` for the target slot:

```
WARNING::  No bid record found for this slot
======================================================================
NO ACTIVATION REQUIRED
======================================================================
No bid was placed for slot 2026-01-13 17:30 - 17:45
No assets will be activated.
```

**This is a safety feature** - without knowing which assets were included in the bid, it's safer to do nothing than to guess.

### "No trades found for this slot"

This means no actual trades were matched for this slot - the bid was placed but the DSO didn't buy it:

```
======================================================================
NO ACTIVATION REQUIRED
======================================================================
No trades found for slot 2026-01-14 07:00 - 07:15
No assets will be activated.
```

**This is correct behavior** - we only activate flexibility when there's an actual trade (DSO accepted our offer). The bid record quantity (what we offered) is NOT used for activation.

By default, this check is based on NODES accepted trades. If you need the old local-ledger behavior for controlled testing, pass `--allow-market-ledger-fallback`; otherwise `market_ledger` rows are ignored because they may represent posted orders.

### "persistence_no_positive_bid_assets"

This means the selected bid strategy uses `flexibility_method: "persistence"`, but the bid record has no positive `public.bid_record_assets.available_flexibility_kw` rows. The manager fails closed, performs no new activation, and restores previously controlled assets if needed.

Check that `trader_fsp.py` stored the per-asset bid plan and that the expected assets have positive `available_flexibility_kw`.

### "Could not allocate flexibility to any asset"

- Check FSP's `assets` list in config
- Verify asset capacities and flexibility factors

### "Unknown control type"

- Add `control` section to asset in config
- Use `simulation` type for testing

### "Modulation-aware final: over-delivery due to discrete assets"

This is expected when only discrete (ON/OFF) assets are available and the target doesn't match an exact combination:

```
Requested: 15 kW
Available discrete assets: HP1 (15 kW), HP2 (15 kW), HP3 (4 kW)
Best combination: HP1 (15 kW) + HP3 (4 kW) = 19 kW
Over-delivery: +4 kW (26.7%)
```

**Why this happens:** Discrete assets (heat pumps) can only be ON or OFF. When switched OFF, they deliver their **full capacity** as curtailment. The system finds the smallest combination that meets or exceeds the target.

**Possible combinations for the example:**
- HP3 alone: 4 kW (under-delivers by 11 kW) ❌
- HP1 alone: 15 kW (exact match) ✅
- HP1 + HP3: 19 kW (over-delivers by 4 kW) - chosen if HP1 alone isn't sufficient

**Solutions:**
- Add continuous assets (EV chargers) to fill gaps precisely
- Accept small over-delivery for discrete-only portfolios
- Adjust bidding to offer only achievable discrete combinations

### "Modulation-aware final: under-delivery"

Under-delivery occurs when the target is larger than all available discrete assets combined:

```
Requested: 40 kW
Available discrete assets: HP1 (15 kW), HP2 (15 kW), HP3 (4 kW)
Maximum possible: 15 + 15 + 4 = 34 kW
Under-delivery: -6 kW (15%)
```

### "Discrete state: OFF but curtailment < threshold"

The system uses a threshold (default 50% of capacity) to decide ON/OFF:
- Curtailment ≥ 50% of capacity → Switch OFF
- Curtailment < 50% of capacity → Keep ON

Exception: persistence strategies force selected discrete assets OFF for any positive activation kW, because the stored bid-time flexibility is the strategy's activation plan.

Configure per asset if needed:
```json
"ECM97.1": {
  "curtailment_threshold_pct": 30.0  // More aggressive switching
}
```

---

## Testing

Unit tests for the command-preparation helpers live in:

```
unittest/test_flexi_manager_commands.py
```

Run them with:

```bash
cd /path/to/pyfm
.venv/bin/python -m pytest unittest/test_flexi_manager_commands.py -v
```

The tests cover:

| Test class | What it validates |
|---|---|
| `TestParseSlotDatetime` | Naive/aware/string parsing, TZ conversion, second stripping |
| `TestFormatAemUtc` | Clean formatting, no TZ suffix, no microseconds |
| `TestBuildEvSchedule` | Single slot, multi-slot, empty window, float conversion |
| `TestPrepareCommandPayload` | HP/EV curtail/restore slot injection, dry_run flag |
| `TestCurtailPayloads` | HP `discrete_state`, EV `power_kw` in curtail commands |
| `TestRestorePayloads` | HP ON state, EV restore fallback chain (3 cases) |
| `TestPublishPendingCommands` | End-to-end HP/EV curtail+restore publish, batch publishing |
| `TestControlledStatePersistence` | State file round-trip, missing file handling |

---

## Related Documentation

- [BIDDING_STRATEGIES.md](BIDDING_STRATEGIES.md) - Bidding strategy configuration
- [FLEXIBILITY_ANALYSIS.md](FLEXIBILITY_ANALYSIS.md) - How flexibility is calculated
- [README_strategy_evaluator.md](../scripts/README_strategy_evaluator.md) - Strategy evaluation
