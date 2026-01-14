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
│  │  → ECM96.2: 1.67 kW                  │                               │
│  │  → ECM97.1: 6.66 kW                  │                               │
│  │  → ECM97.2: 6.66 kW                  │                               │
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
# Proportional: distribute based on capacity
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

---

## Command Line Arguments

| Argument | Short | Description | Default |
|----------|-------|-------------|---------|
| `--config` | `-c` | Configuration file path | `../conf/test_fm01_aem.json` |
| `--fsp` | `-f` | FSP identifier (required) | - |
| `--slot` | `-s` | Target slot start (ISO format) | Next 15-min slot |
| `--offset` | `-t` | Time offset from now (e.g., `30m`, `2h`, `1h30m`) | - |
| `--dry-run` | `-d` | Simulate only | Yes |
| `--live` | `-l` | Send actual commands | No |
| `--allocation` | `-a` | Allocation strategy | `proportional` |
| `--log-level` | - | Logging verbosity | `INFO` |
| `--output` | `-o` | JSON output file | - |

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

The total sold flexibility is distributed across **allowed assets only**:

| Strategy | Description |
|----------|-------------|
| `proportional` | Distribute based on capacity × flexibility_factor |
| `priority` | Fill HPs first (more reliable), then EVs |
| `cost_optimal` | Minimize total activation cost |

**Example allocation for 15 kW:**
```
ECM97.2 (HP Cinema 2):  8.5 kW (56%)  ← Largest capacity
ECM97.1 (HP Cinema 1):  4.0 kW (27%)
ECM96.2 (HP Small):     2.5 kW (17%)
```

### Step 3: Control Assets

Send curtailment commands to each asset:

| Asset Type | Control Methods |
|------------|----------------|
| Heat Pump | MQTT, HTTP API, Modbus |
| EV Charger | OCPP, HTTP API |

---

## Example Output

```
2026-01-09 11:59:00::INFO::run::======================================================================
2026-01-09 11:59:00::INFO::run::FLEXIBILITY MANAGER - supsi01
2026-01-09 11:59:00::INFO::run::======================================================================
2026-01-09 11:59:00::INFO::run::Target slot: 2026-01-09 12:00 - 12:15
2026-01-09 11:59:00::INFO::run::Mode: DRY-RUN
2026-01-09 11:59:00::INFO::run::Allocation strategy: proportional
2026-01-09 11:59:00::INFO::run::----------------------------------------------------------------------
2026-01-09 11:59:00::INFO::run::Step 1: Querying market results...
2026-01-09 11:59:01::INFO::get_accepted_trades_for_slot::Querying accepted trades for slot 2026-01-09 12:00 - 12:15
2026-01-09 11:59:01::INFO::get_accepted_trades_for_slot::Found 1 accepted sell trades for this slot
2026-01-09 11:59:01::INFO::run::----------------------------------------------------------------------
2026-01-09 11:59:01::INFO::run::Total flexibility sold: 0.015 MW (15.00 kW)
2026-01-09 11:59:01::INFO::run::----------------------------------------------------------------------
2026-01-09 11:59:01::INFO::run::Step 2: Allocating flexibility across assets...
2026-01-09 11:59:01::INFO::allocate_flexibility::Allocating 15.00 kW flexibility using 'proportional' strategy
2026-01-09 11:59:01::INFO::_allocate_proportional::Proportional allocation: {'ECM97.2': 8.478, 'ECM97.1': 4.239, 'ECM96.2': 2.283}
2026-01-09 11:59:01::INFO::run::----------------------------------------------------------------------
2026-01-09 11:59:01::INFO::run::Allocation plan:
2026-01-09 11:59:01::INFO::run::  ECM97.2 (HP Cinema 2): 8.48 kW
2026-01-09 11:59:01::INFO::run::  ECM97.1 (HP Cinema 1): 4.24 kW
2026-01-09 11:59:01::INFO::run::  ECM96.2 (HP Small): 2.28 kW
2026-01-09 11:59:01::INFO::run::  Total allocated: 15.00 kW
2026-01-09 11:59:01::INFO::run::----------------------------------------------------------------------
2026-01-09 11:59:01::INFO::run::Step 3: Sending control commands...
2026-01-09 11:59:01::INFO::curtail_asset::[DRY-RUN] Would curtail ECM97.2 (HP Cinema 2): 8.48 kW (56.5%) for 15 minutes
2026-01-09 11:59:01::INFO::curtail_asset::[DRY-RUN] Would curtail ECM97.1 (HP Cinema 1): 4.24 kW (28.3%) for 15 minutes
2026-01-09 11:59:01::INFO::curtail_asset::[DRY-RUN] Would curtail ECM96.2 (HP Small): 2.28 kW (57.1%) for 15 minutes
2026-01-09 11:59:01::INFO::run::======================================================================
2026-01-09 11:59:01::INFO::run::FLEXIBILITY ACTIVATION COMPLETE
2026-01-09 11:59:01::INFO::run::======================================================================
2026-01-09 11:59:01::INFO::run::Assets controlled: 3 successful, 0 failed
2026-01-09 11:59:01::INFO::run::Total flexibility delivered: 15.00 kW (0.015 MW)
```

---

## Configuration

### Asset Control Configuration

To enable actual asset control, add `control` section to each asset in `asset_mapping`:

```json
{
  "asset_mapping": {
    "ECM97.1": {
      "device_name_tag": "shelly_3em_pro_heat_pump_1",
      "type": "heat_pump",
      "description": "HP Cinema 1",
      "capacity_kw": 15.0,
      "flexibility_factor": 0.85,
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
      "control": {
        "type": "ocpp",
        "charger_id": "CP001",
        "endpoint": "ws://ocpp.server.local:9000"
      }
    }
  }
}
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

### 1. Proportional (Default)

Distributes flexibility based on each asset's share of total capacity:

```
share = (capacity × flex_factor) / total_capacity
allocation = share × total_required
```

**Pros:** Fair distribution, all assets contribute
**Cons:** May under-utilize high-capacity assets

### 2. Priority

Fills assets in order of reliability:
1. Heat pumps (most reliable)
2. EV chargers (dependent on car presence)

**Pros:** Maximizes delivery probability
**Cons:** May overload some assets

### 3. Cost-Optimal

Fills cheapest assets first (based on `activation_cost_per_kw`):

```json
"ECM97.1": {
  "activation_cost_per_kw": 0.10  // CHF per kW per 15min
}
```

**Pros:** Minimizes activation costs
**Cons:** May concentrate load on few assets

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
0 * * * *  cd /path/to/pyfm && .venv/bin/python scripts/trader_fsp.py --config_file conf/test_fm01_aem.json --fsp supsi01

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
| `power_to_activate_kw` | Power curtailment in kW |
| `percentage_of_capacity` | % of asset capacity |
| `allocation_strategy` | proportional, priority, cost_optimal |
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

---

## Related Documentation

- [BIDDING_STRATEGIES.md](BIDDING_STRATEGIES.md) - Bidding strategy configuration
- [FLEXIBILITY_ANALYSIS.md](FLEXIBILITY_ANALYSIS.md) - How flexibility is calculated
- [README_strategy_evaluator.md](../scripts/README_strategy_evaluator.md) - Strategy evaluation
