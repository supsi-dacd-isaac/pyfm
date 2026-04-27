# Flexibility Manager (flexi_manager.py)

This script manages the **activation** of flexibility for an FSP. After the FSP has bid and won trades on the flexibility market, this script controls the actual assets to deliver the promised flexibility.

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

-- What actually happened in the market (existing table, updated with FK)
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
│   │ planned │ │ to activate │ │ (actual trade)│                        │
│   └─────────┘ └─────────────┘ └───────────────┘                        │
│                                    │                                    │
│                                    │ FK: bid_record_id                  │
│                                    ▼                                    │
│                             What ACTUALLY happened                      │
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

### Save Output to File

```bash
python flexi_manager.py --fsp supsi01 --dry-run --output activation_result.json
```

### Autonomous Mode (Dry-Run Only)

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
  "dso_id": "AEM"
}
```

Even though autonomous mode does not trigger actual control commands, it lets you **see how the price is expected to evolve** so you can warm up assets in advance and be ready for a future activation.

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
| `--allocation`  | `-a` | Allocation strategy | `modulation_aware` |
| `--log-level`   | - | Logging verbosity | `INFO` |
| `--log_file`    | - | Path to log file | - |
| `--output`      | `-o` | JSON output file | - |

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
SELECT * FROM pyfm.bid_records 
WHERE fsp_id = 'supsi01' AND slot_start = '2026-01-09 12:00:00'
```

The record contains:
| Field | Value |
|-------|-------|
| strategy_id | strategy_4 |
| strategy_name | Hybrid (S3+S1) |
| total_quantity_mw | 0.015 |
| status | pending |

Related assets from `pyfm.bid_record_assets`:
| asset_id | asset_type | available_flexibility_kw |
|----------|------------|--------------------------|
| ECM96.2 | heat_pump | 1.65 |
| ECM97.1 | heat_pump | 2.64 |
| ECM97.2 | heat_pump | 9.58 |

**If no bid record found:** The script stops and no assets are activated. This is a safety measure - without a bid record, we don't know which assets were intended to be used.

### Step 1: Query Market Results

The script queries for actual trades:
1. **Local market_ledger** (PostgreSQL) - checked first for speed
2. **NODES API** - fallback if no local data

**Only actual trades trigger activation.** The bid record quantity (what we offered) is NOT used - only what was actually accepted matters.

```python
trades = market_handler.get_accepted_trades_for_slot(org_id, slot_start, slot_end)
total_sold_mw = sum(t.get("quantity", 0) for t in trades)

# If no trades found -> NO activation
if total_sold_mw == 0:
    return "no_trades"
```

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
2026-01-15 07:30:01::INFO::get_trades_from_ledger::Found 1 trades in market_ledger
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

```json
{
  "asset_mapping": {
    "ECM97.1": {
      "device_name_tag": "shelly_3em_pro_heat_pump_1",
      "type": "heat_pump",
      "description": "HP Cinema 1",
      "capacity_kw": 15.0,
      "flexibility_factor": 0.85,
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
      "modulation_type": "continuous",
      "min_power_kw": 0.0,
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

*Default by type: `heat_pump` → `discrete`, `ev_charger` → `continuous`

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
| `modbus` | HP | Write to Modbus register |
| `ocpp` | EV | OCPP SetChargingProfile |
| `simulation` | All | Log only (default) |

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
   - Saves bid record to PostgreSQL database (`pyfm.bid_records`)
2. Market clears, trades are matched
3. **flexi_manager.py** runs at 11:59 to activate flexibility for 12:00 slot
   - Reads bid record from database to know which assets to activate
   - Only activates assets allowed by the strategy
   - Marks bid record as `activated` after completion

```bash
# Cron example
# trader_fsp.py runs at minute 0 to bid for slot starting at minute 30 (90-min ahead)
0 * * * *  cd /path/to/pyfm && .venv/bin/python scripts/trader_fsp.py --config conf/test_fm01_aem.json --fsp supsi01

# flexi_manager.py runs at minute 14, 29, 44, 59 to activate the just-passed slot
14,29,44,59 * * * * cd /path/to/pyfm && .venv/bin/python scripts/flexi_manager.py --fsp supsi01 --offset 15m --live
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

Configure per asset if needed:
```json
"ECM97.1": {
  "curtailment_threshold_pct": 30.0  // More aggressive switching
}
```

---

## Related Documentation

- [BIDDING_STRATEGIES.md](BIDDING_STRATEGIES.md) - Bidding strategy configuration
- [FLEXIBILITY_ANALYSIS.md](FLEXIBILITY_ANALYSIS.md) - How flexibility is calculated
- [README_strategy_evaluator.md](../scripts/README_strategy_evaluator.md) - Strategy evaluation
