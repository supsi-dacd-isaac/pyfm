# trader_fsp.py

This script represents the **FSP trading agent** in the Opentunity-CH flexibility market. It reacts to the DSO's flexibility requests and places corresponding **Sell** orders based on the FSP's portfolios, baselines, and bidding strategies.

## High-level behaviour

1. Reads configuration from a JSON file and merges `connectionsFile`.
2. Instantiates both a `DSO` object (to read flexibility requests) and an `FSP` object (to place Sell orders).
3. Computes the target market timeslot (`slot_time`) from `fm.granularity` and `fm.ordersTimeShift`.
4. Uses the DSO instance to query the market for current flexibility requests (`dso.get_flexibility_requests`).
5. Downloads or refreshes baselines for the FSP (`fsp.download_baselines`).
6. **Calculates available flexibility** using the `FlexibilityForecaster` with the selected strategy's flexibility method. Strategies without `flexibility_method: "persistence"` use the historical/legacy path.
7. Applies the selected **bidding strategy** to determine price and quantity.
8. For each DSO request and each FSP portfolio, constructs and posts Sell orders.
9. Records bid information in the database for auditing.

There is also a helper function `create_dataframe_for_portfolio_baseline` that shows how to build a baseline DataFrame from a CSV file, although it is not used in the main flow.

---

## Operational Persistence Mode

The trader now supports a portfolio-scoped persistence mode for operational bidding. In strategy mode this is selected per strategy, not globally for all strategies.

```text
target_slot_utc = floor(current_time_utc, fm.granularity) + fm.ordersTimeShift
flexibility_go_back_i =
    asset_mapping.<asset>.flexibility_persistence_go_back_minutes
    if present
    else flexibility.persistenceSettings.persistenceGoBackMinutes

baseline_source_time_i = target_slot_utc - flexibility_go_back_i
baseline_i(t) = measured_power_i(baseline_source_time_i)

if current_measured_power_i <= activeThresholdW:
    flexibility_i(t) = 0
else:
    flexibility_i(t) = safety_factor_i * min(baseline_i(t), nominal_power_i)
```

Important distinctions:

- Baseline and flexibility remain separate.
- The current-state gate is applied to flexibility only, never to the baseline.
- Current measured power is the latest grouped measurement at or before `current_time_utc`, not the lagged source slot unless they happen to coincide.
- Aggregation is asset first, then portfolio total. The trader does not compute one global persistence flexibility number and reuse it for every portfolio.

Example with the current config:

```text
current_time_utc         = 2026-05-12 08:20
target_slot_utc          = 2026-05-12 09:45
ECM96.2 source time    = 2026-05-12 07:45  (120 min override)
ECM97.3 source time    = 2026-05-12 07:45  (120 min override)
ECM63.1 source time    = 2026-05-12 07:45  (120 min)
ECM63.2 source time    = 2026-05-12 07:45  (120 min)
```

Persistence settings are read from the global `flexibility.persistenceSettings` block:

```json
"flexibility": {
  "method": "persistence",
  "persistenceSettings": {
    "persistenceGoBackMinutes": 90,
    "activeThresholdW": 500,
    "defaultSafetyFactor": 1.0,
    "missingMeasurementPolicy": "skip_asset",
    "maxCurrentMeasurementAgeMinutes": 30
  }
}
```

The baseline uploader can independently use:

```json
"baseline": {
  "source": "db",
  "shiftMinutes": 90,
  "dbSettings": {
    "strategy": "slot_persistence",
    "missingMeasurementPolicy": "zero_fill_asset"
  }
}
```

These two persistence concepts are independent:

- `baseline.dbSettings.strategy = "slot_persistence"` affects baseline uploading.
- `flexibility_method: "persistence"` inside a bidding strategy affects trader bidding flexibility.

Existing strategies default to historical/legacy flexibility. In the current config, `strategy_8` and `strategy_9` explicitly request persistence; `strategy_4` remains historical/legacy.

---

## Discretization-Aware Bidding

The script implements **discretization-aware flexibility calculation** that correctly handles the physical constraints of different asset types:

### Asset Modulation Types

| Asset Type | Modulation | Control | Example |
|------------|------------|---------|---------|
| **Heat Pumps** | Discrete (ON/OFF) | Can only be fully ON or fully OFF | 0 kW or 15 kW |
| **EV Chargers** | Continuous | Can be modulated to any value in range | 0-11 kW |

### How It Works

1. **Enumerate discrete combinations**: For N discrete assets with ON/OFF control, calculate all 2^N achievable power levels.
   
   Example with two HP assets (4 and 30 kW):
   - Achievable levels: {0, 4, 30, 34} kW

2. **Calculate continuous range**: Sum of available flexibility from modulatable assets (EVs).
   
   Example with 2 EVs (11 kW each, 70% occupancy):
   - Continuous range: 0 - 15.4 kW

3. **Find optimal bid**: For a target flexibility:
   - Select best discrete combination (≤ target)
   - Fill remaining gap with continuous assets
   - Bid the exact achievable quantity

### Example Allocation

For a **15 kW target**:

```
DISCRETIZATION-AWARE FLEXIBILITY ANALYSIS:
  Target flexibility: 15.000 kW (0.015000 MW)
  Discrete assets: ['ECM96.2', 'ECM97.3']
  Continuous assets: ['ECM63.1', 'ECM63.2']
  Achievable discrete levels (kW): [0.0, 4.0, 30.0, 34.0]
  Continuous range (kW): 0.00 - 15.40
  Total achievable range (kW): 0.00 - 49.40
----------------------------------------------------------------------
RECOMMENDED BID (quantity used for bidding): 15.000 kW (0.015000 MW) (exact match)
  Discrete allocation (ON/OFF):
    - ECM96.2: 4.0 kW (ON)
    - ECM97.3: 0.0 kW (OFF)
  Continuous allocation (modulated): 11.00 kW total
    - ECM63.1: 5.50 kW (setpoint: 50.0% of 11.0 kW capacity)
    - ECM63.2: 5.50 kW (setpoint: 50.0% of 11.0 kW capacity)
```

### Benefits

- **Accurate bidding**: Bids only what can actually be delivered
- **No delivery mismatch**: Activation exactly matches the bid
- **Optimal portfolio use**: Combines discrete and continuous assets intelligently
- **Consistent with flexi_manager**: Same allocation logic at bidding and activation

---

## Pricing model

The **price** of FSP Sell orders is configured in the `pricing` block of each FSP entry in the configuration. Example from `conf/test_fm01_aem.json`:

```json
"pricing": {
  "source": "constant",
  "constant": 5.0,
  "forecasting_multiplier": 1.0,
  "activationCost": 1.0
}
```

This block is interpreted in `FSP.sell_flexibility(...)` (see `classes/fsp.py`) and typically works as follows:

- **`source`**
  - `"constant"`: all Sell orders are priced at a fixed value (plus optional activation cost).
  - Other sources (e.g. `"forecast"`) may use forecast data to build dynamic prices.
- **`constant`**: base energy price (e.g. in CHF/MWh) for the flexible power offered.
- **`forecasting_multiplier`**: scaling factor applied when prices are derived from a forecast signal. For a forecast-based source, an indicative formula may be:
  - `price = constant + forecasting_multiplier * forecast_value`.
- **`activationCost`**: additional cost associated with activating flexibility (e.g. discomfort, degradation). This is usually added on top of the base energy price and can be reflected in:
  - higher offer prices, or
  - separate fields if the market distinguishes energy price from activation price.

`trader_fsp.py` itself does not perform pricing calculations. Instead, when it calls:

```python
resp_selling = fsp.sell_flexibility(slot_time, p_k, dso_demand)
```

`resp_selling` already contains price information derived from this `pricing` configuration, taking into account:

- the current baseline and available flexibility,
- the DSO’s requested quantity and regulation type,
- the chosen pricing model (constant or forecast-based).

These prices are then forwarded unchanged to the market ledger via `FMO.add_entry_to_market_ledger(...)`.

---

## Command-line interface

```bash
python scripts/trader_fsp.py \
  --config_file conf/test_fm01_aem.json \
  --fsp supsi01 \
  --strategy strategy_8 \
  --dry-run \
  --log_file logs/trader_fsp.log
```

**Arguments**

- `--config_file` (required): path to configuration JSON.
- `--fsp` (required): FSP identifier (key under `fm.actors.fsps`, e.g. `supsi01`).
- `--strategy` (optional): Bidding strategy to use (`strategy_1` through `strategy_9`). If not specified, uses FSP's configured strategy or falls back to simple mode.
- `--list-strategies`: List all available strategies and exit.
- `--dry-run` (optional): Simulate bidding without placing actual orders. Useful for testing.
- `--log_file` (optional): path to log file. If omitted, logs go to stdout.

Run the HP-only persistence strategy from `scripts/`:

```bash
cd scripts
python3.10 trader_fsp.py --config_file ../conf/test_fm01_aem.json --fsp supsi01 --strategy strategy_8 --dry-run
```

Run the HP + EV persistence strategy from `scripts/`:

```bash
cd scripts
python3.10 trader_fsp.py --config_file ../conf/test_fm01_aem.json --fsp supsi01 --strategy strategy_9 --dry-run
```

### Dry-Run Mode

Use `--dry-run` to simulate the bidding process without placing actual orders:

```bash
python scripts/trader_fsp.py \
  --config_file conf/test_fm01_aem.json \
  --fsp supsi01 \
  --dry-run
```

This will:
- Calculate available flexibility
- Show recommended bid quantities and allocations
- Log what orders **would** be placed
- NOT actually submit orders to the market

The current code may still create local bid/demand audit records when repository dependencies and database connectivity are available, because those records are created before order placement. `--dry-run` prevents market order submission.

In persistence mode, dry-run still computes and logs:

- `target_slot_utc`
- `baseline_source_time_utc`
- per-asset baseline power
- per-asset current gate measurement time and power
- active/inactive state
- nominal cap and safety factor
- portfolio-scoped flexibility totals

### Strategy-Mode Log Interpretation

Recent strategy-mode logs intentionally separate diagnostic portfolio totals from selected-strategy bidding quantities:

```text
PORTFOLIO AVAILABLE FLEXIBILITY (all assigned assets before strategy filter): ...
STRATEGY AVAILABLE FLEXIBILITY (<strategy_id> allowed assets only): ...
RECOMMENDED BID (quantity used for bidding): ...
CAN MEET DSO DEMAND WITH STRATEGY <strategy_id> (<strategy_name>): ...
```

Interpretation:

- **Portfolio available flexibility** is diagnostic only. It is the total across assigned portfolio assets before applying the selected strategy filter.
- **Strategy available flexibility** is the relevant available flexibility after applying the selected strategy's allowed assets.
- **Recommended bid** is the operational quantity used for bidding after strategy filtering and achievable-flexibility logic.
- **DSO feasibility in strategy mode** uses strategy-filtered availability, not full portfolio availability.

Before live bidding, validate the target slot, baseline source time, current-state gate, strategy asset filtering, recommended bid, DSO demand, and price check. For EV strategies, treat missing or delayed telemetry conservatively.

---

## Bidding Strategies

The script supports multiple bidding strategies that control which assets to use, when to bid, and at what price.

### Available Strategies

| Strategy | Name | Flexibility method | Description |
|----------|------|--------------------|-------------|
| `strategy_1` | HP Only | Historical/legacy | Heat pumps only, conservative full-day coverage |
| `strategy_2` | Full Portfolio + Evening EV | Historical/legacy | All assets, focus on evening when EVs charge |
| `strategy_3` | Morning Peak Focus | Historical/legacy | Aggressive morning peak focus, filtered to `ECM97.3` |
| `strategy_4` | Hybrid (S3+S1) | Historical/legacy | Recommended HP hybrid using `ECM96.2` and `ECM97.3` |
| `strategy_5` | Hybrid2 (S3+S2) | Historical/legacy | Morning aggressive + evening EV focus |
| `strategy_6` | Smart Preheat | Historical/legacy | Preheat from 04:00-06:00, then morning peak HP flexibility |
| `strategy_7` | Double Pre-heating | Historical/legacy | Dual preheat schedule for `ECM96.2` morning and evening peak flexibility |
| `strategy_8` | Persistence HP Strategy | Persistence | HP-only persistence strategy for `ECM96.2` and `ECM97.3` |
| `strategy_9` | Persistence HP + EV Strategy | Persistence | Persistence strategy for `ECM63.1`, `ECM63.2`, `ECM96.2`, and `ECM97.3` |

`strategy_8` is the safer persistence option because it excludes EV chargers. `strategy_9` includes EV chargers and should be monitored carefully because EV telemetry has been observed to be more problematic than HP telemetry.

### Strategy Configuration

Strategies are defined in `bidding_strategies` section of the config:

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
      {"name": "Evening Peak", "start": "16:00", "end": "19:00", 
       "flexibility_mw": 0.040, "bid_price": 9.5, "activation_cost": 2.5},
      {"name": "Off-peak", "start": "19:00", "end": "06:30", 
       "flexibility_mw": 0.006, "bid_price": 6.0, "activation_cost": 2.5}
    ]
  }
}
```

Persistence strategies opt in explicitly:

```json
"strategy_8": {
  "name": "Persistence HP Strategy",
  "asset_types": ["heat_pump"],
  "assets_filter": ["ECM96.2", "ECM97.3"],
  "flexibility_method": "persistence"
}
```

### Strategy Selection Priority

1. Command-line `--strategy` argument (highest priority)
2. FSP config `"strategy"` field
3. Simple mode (baseline-based, no strategy)

---

## Configuration structure

The script uses the same global structure as other scripts, notably `fm` and `connectionsFile`.

Important parts from `conf/test_fm01_aem.json`:

```json
{
  "connectionsFile": "../conf/private/conns.json",
  "fm": {
    "granularity": 15,
    "ordersTimeShift": 90,
    "marketName": "Opentunity-CH",
    "actors": {
      "dso": { ... },
      "fsps": {
        "supsi01": { ... },
        "supsi02": { ... }
      }
    }
  }
}
```

### FSP configuration `fm.actors.fsps[<FSP_ID>]`

Example for `supsi01`:

```json
"supsi01": {
  "id": "SUPSI",
  "name": "SUPSI",
  "role": "fsp",
  "baselines": {
    "tmpFolder": "../data/tmp",
    "fromBeforeNowHours": 12,
    "toAfterNowHours": 12
  },
  "orderSection": {
    "quantityPercBaseline": 50,
    "mainSettings": {
      "side": "Sell",
      "priceType": "Limit",
      "currency": "CHF",
      "fillType": "Normal"
    }
  },
  "pricing": {
    "source": "strategy",
    "constant": 5.0,
    "forecasting_multiplier": 1.0,
    "activationCost": 2.5
  },
  "strategy": "strategy_4",
  "assets": ["ECM96.2", "ECM97.3", "ECM63.1", "ECM63.2"],
  "forecast": {
    "source": "aem",
    "filename": "../data/forecast/example01.csv"
  },
  "contractSection": {
    "mainSettings": {
      "autoCreateExpiry": 7200
    }
  }
}
```

Key elements:

- **`baselines`**: controls how long the baseline horizon is and where temporary files are stored.
- **`orderSection.quantityPercBaseline`**: percentage of the available baseline flexibility to offer to the market.
- **`orderSection.mainSettings`**: basic order parameters for Sell offers.
- **`pricing`**: how the Sell offer prices are set. When `"source": "strategy"`, prices come from the bidding strategy.
- **`strategy`**: default bidding strategy to use (e.g., `"strategy_4"`).
- **`assets`**: list of asset IDs that belong to this FSP's portfolio.
- **`forecast`**: how the FSP obtains forecasts (e.g. from AEM CSV).

For persistence-based operation, the baseline/flexibility mode is controlled outside the FSP block:

- `baseline.source = "db"`
- `baseline.shiftMinutes`
- `baseline.dbSettings.strategy`
- `baseline.dbSettings.missingMeasurementPolicy`
- `flexibility.method`
- `flexibility.persistenceSettings.*`

The `FSP` class uses these parameters in methods such as `download_baselines` and `sell_flexibility`.

### Asset Mapping configuration `asset_mapping`

The `asset_mapping` section defines individual assets with their modulation characteristics:

```json
"asset_mapping": {
  "ECM63.1": {
    "device_name_tag": "charge_point_ev_1",
    "field": "power",
    "type": "ev_charger",
    "description": "EV Charger 1",
    "capacity_kw": 11.0,
    "flexibility_factor": 0.70,
    "modulation_type": "continuous",
    "min_power_kw": 0.0
  },
  "ECM97.1": {
    "device_name_tag": "shelly_3em_pro_heat_pump_1",
    "field": "active_power",
    "type": "heat_pump",
    "description": "HP Cinema 1",
    "capacity_kw": 15.0,
    "flexibility_factor": 0.85,
    "modulation_type": "discrete",
    "discrete_states_kw": [0.0, 15.0]
  }
}
```

Key fields:

| Field | Description |
|-------|-------------|
| `type` | Asset type: `"ev_charger"` or `"heat_pump"` |
| `capacity_kw` | Nominal power capacity |
| `flexibility_factor` | Availability factor (0.0-1.0) |
| `modulation_type` | `"continuous"` (any value) or `"discrete"` (ON/OFF) |
| `min_power_kw` | Minimum power for continuous assets (default: 0) |
| `discrete_states_kw` | Valid power states for discrete assets (e.g., `[0.0, 15.0]`) |
| `nominal_power_w` | Explicit nominal power used by persistence flexibility |
| `persistence_safety_factor` | Optional per-asset safety factor for persistence flexibility |
| `baseline_persistence_go_back_minutes` | Optional baseline lag override for `slot_persistence`; otherwise baseline code uses its configured default/fallback |
| `flexibility_persistence_go_back_minutes` | Optional flexibility lag override for persistence mode; falls back to `flexibility.persistenceSettings.persistenceGoBackMinutes` |

Per-asset persistence go-back overrides exist because all assets remain in the same NODES portfolio, but some telemetry can arrive later than other telemetry. The portfolio baseline and flexibility are still sums of asset-level predictions; no missing asset values are silently filled. Current operational settings set `ECM63.1`, `ECM63.2`, `ECM96.2`, and `ECM97.3` to 120 minutes for both baseline and flexibility persistence overrides.

If `modulation_type` is not specified:
- Heat pumps default to `"discrete"` with `[0.0, capacity_kw]`
- EV chargers default to `"continuous"`

---

## Detailed execution steps

1. **Argument parsing**<br>
   Uses `argparse` to read `--config_file`, `--fsp`, `--strategy`, `--dry-run`, and `--log_file`.

2. **Configuration loading**<br>
   - Reads the main JSON configuration file.
   - Reads and merges the JSON from `cfg["connectionsFile"]`.

3. **Strategy initialization**<br>
   - Initializes `StrategyManager` with all configured strategies.
   - Determines which strategy to use (CLI > FSP config > simple mode).

4. **Logging setup**<br>
   Configures logging via `logging.basicConfig` with the given log file or stdout.

5. **Database connection (optional)**<br>
   Tries to create a `PostgreSQLInterface(cfg["postgreSQL"], logger)`. On failure, logs an error and continues.

6. **Timeslot calculation**<br>
   - Creates a temporary `DSO` instance for time alignment:

     ```python
     dso = DSO(cfg["fm"]["actors"]["dso"], cfg, logger)
     dso.set_organization(filter_dict={"name": dso.cfg["id"]})
     slot_time = dso.get_adjusted_time(cfg["fm"]["granularity"], cfg["fm"]["ordersTimeShift"])
     ```

   - `slot_time` is the reference timeslot for both demand and offers.
   - With `fm.granularity = 15` and `fm.ordersTimeShift = 90`, `08:20 UTC` maps to the `09:45 UTC` delivery slot.

7. **FSP setup**<br>
   - Initialize FSP:

     ```python
     fsp = FSP(cfg["fm"]["actors"]["fsps"][fsp_identifier], cfg, logger)
     user_info = fsp.nodes_interface.get_user_info()
     fsp.set_markets(filter_dict={"name": cfg["fm"]["marketName"]})
     fsp.set_organization(filter_dict={"name": fsp.cfg["id"]})
     ```

   - Logs market id and market name for traceability.

8. **Obtain DSO flexibility requests**<br>
   - The script calls:

     ```python
     dso_demands = dso.get_flexibility_requests(
         slot_time, cfg["fm"]["granularity"], "Buy", "Power"
     )
     ```

   - This should return a list of demand objects for the given slot, side, and product type (e.g. Power).

9. **Baseline download**<br>
   - Calls `fsp.download_baselines(slot_time)` to ensure current baselines are available for all portfolios and assets.

10. **Flexibility forecasting (discretization-aware)**<br>
    - Initializes `FlexibilityForecaster` for the asset portfolio.
    - In strategy mode, `trader_fsp.py` resolves the selected strategy's flexibility method. Strategies without `flexibility_method: "persistence"` use the historical/legacy path even if persistence settings exist elsewhere in the config.
    - If the selected strategy uses persistence, the script:
      - identifies the assets assigned to each portfolio,
      - computes `baseline_i(t) = measured_power_i(t - persistenceGoBackMinutes)`,
      - obtains the latest grouped current measurement at or before `current_time_utc`,
      - applies the current-state gate, nominal cap, and safety factor per asset,
      - aggregates asset flexibility at portfolio level,
      - keeps strategy filtering as the intersection of portfolio assets and allowed strategy assets.
    - Uses `get_achievable_flexibility()` to calculate:
      - discrete asset combinations (ON/OFF states),
      - continuous asset ranges (modulation),
      - recommended bid quantity.

11. **Strategy-based bid calculation**<br>
    If using strategy mode:

    ```python
    flexibility_to_bid_mw, achievable_details = get_strategy_flexibility_discrete(
        strategy, slot_time, flex_forecaster, logger
    )
    ```

    This returns the **achievable** flexibility (not just a fractional sum) and the allocation plan.
    In persistence mode this is computed per portfolio, not once globally.

12. **FMO initialization**<br>
    - `fmo = FMO(fsp.cfg, logger, pgi)`.

13. **Bid record creation**<br>
    - Creates a bid record in the database before placing orders.
    - Includes assets to activate, prices, and strategy info.

14. **Offer construction and posting**<br>
    For strategy mode:

    ```python
    orders_summary, used_strategy = run_strategy_mode(
        strategy, strategy_id, fsp, fmo, dso_demands, slot_time,
        asset_breakdown, flex_forecaster, dry_run, logger
    )
    ```

    For simple mode:

    ```python
    orders_summary = run_simple_mode(
        fsp, fmo, dso_demands, slot_time, total_available_flex_mw, dry_run, logger
    )
    ```

    - In persistence simple mode, the baseline-based quantity is capped by the portfolio's persistence flexibility total before any order is posted.
    - In dry-run mode, logs what would be placed without actual submission.
    - In live mode, posts orders to the market ledger.

15. **Bid record update**<br>
    - Updates the bid record with actual orders placed and final prices.

---

## Example Log Output

When running a persistence strategy such as `strategy_8`, the important lines look like:

```
======================================================================
FSP: supsi01
Mode: STRATEGY-BASED
Strategy: strategy_8 - Persistence HP Strategy
Allowed assets: ['ECM96.2', 'ECM97.3']
Strategy flexibility method: persistence
======================================================================
FLEXIBILITY ANALYSIS FOR SLOT (UTC): 2026-05-13 11:15Z
======================================================================
Peak hour: YES
----------------------------------------------------------------------
PORTFOLIO AVAILABLE FLEXIBILITY (all assigned assets before strategy filter): 3.493 kW (0.003493 MW)
DSO DEMANDS:
  TOTAL DSO DEMAND: Up=0.000 MW, Down=0.000 MW
Strategy-mode DSO feasibility will be checked after applying selected strategy asset filters.
======================================================================
RUNNING IN STRATEGY MODE: strategy_8 - Persistence HP Strategy
======================================================================
Time slot: Morning Peak (Aggressive)
Strategy bid price: 9.00 CHF/MW
Strategy flexibility target: 0.0400 MW
----------------------------------------------------------------------
PORTFOLIO AVAILABLE FLEXIBILITY (all assigned assets before strategy filter): 3.493 kW (0.003493 MW)
STRATEGY AVAILABLE FLEXIBILITY (strategy_8 allowed assets only): 3.121 kW (0.003121 MW)
RECOMMENDED BID (quantity used for bidding): 3.121 kW (0.003121 MW)
CAN MEET DSO DEMAND WITH STRATEGY strategy_8 (Persistence HP Strategy): YES (strategy_available=0.003121 MW, required=0.000000 MW, recommended_bid=0.003121 MW)
----------------------------------------------------------------------
Portfolio ECM_REAL_ASSETS summary: portfolio_available=3.493 kW (0.003493 MW), strategy_available=3.121 kW (0.003121 MW), recommended_bid=3.121 kW (0.003121 MW)
======================================================================
DRY-RUN SUMMARY
======================================================================
Mode: Strategy-based (strategy_8 - Persistence HP Strategy)
```

**Legend for asset markers:**
- `[✓]` / `[✗]`: Asset allowed/not allowed by strategy
- `[D]`: Discrete asset (ON/OFF control)
- `[C]`: Continuous asset (modulated control)

---

## Helper: `create_dataframe_for_portfolio_baseline`

This function illustrates how to construct a baseline DataFrame from a CSV file. It is not invoked in the main script but can be useful for offline or batch baseline generation.

```python
def create_dataframe_for_portfolio_baseline(p_id, data_file_path):
    current_time = datetime.utcnow()
    adjusted_time = current_time.replace(
        minute=(current_time.minute // 15) * 15, second=0, microsecond=0
    ) + timedelta(minutes=30)
    time_step = timedelta(minutes=15)

    df = pd.read_csv(data_file_path)
    df.insert(loc=0, column="assetPortfolioId", value=p_id)

    period_from = [adjusted_time + i * time_step for i in range(len(df))]
    period_from_iso = [dt.strftime("%Y-%m-%dT%H:%M:%SZ") for dt in period_from]
    df.insert(loc=1, column="periodFrom", value=period_from_iso)

    period_to = period_from[1:]
    period_to.append(period_to[-1] + timedelta(minutes=15))
    period_to_iso = [dt.strftime("%Y-%m-%dT%H:%M:%SZ") for dt in period_to]
    df.insert(loc=2, column="periodTo", value=period_to_iso)
    return df
```

Behaviour:

- Reads a CSV with baseline values.
- Aligns timestamps to the next 15-min slot (`adjusted_time`) shifted by 30 minutes.
- Generates `periodFrom`/`periodTo` ISO timestamps for each row.
- Attaches the given portfolio id via an `assetPortfolioId` column.

This structure can match what Nodes expects for baseline upload.

---

## How `test_fm01_aem.json` drives trader_fsp

- `fm.granularity` and `fm.ordersTimeShift` define the time resolution and horizon.
- `fm.actors.dso` is used only for reading requests and time alignment.
- `fm.actors.fsps[<FSP_ID>]` contains baseline, pricing and order configuration used to:
  - download current baselines,
  - compute available flexibility,
  - price Sell offers.
- `baseline.dbSettings.strategy = "slot_persistence"` switches the baseline uploader to slot-level persistence without removing the legacy day-based path.
- In strategy mode, `flexibility_method: "persistence"` inside the selected strategy switches that strategy to the operational persistence logic described above. Strategies without that field use the historical/legacy path.
- The global `flexibility.persistenceSettings` block provides persistence parameters used by persistence strategies.
- `asset_mapping.<asset>.nominal_power_w` and optional `asset_mapping.<asset>.persistence_safety_factor` control the asset-level flexibility cap.

By changing the FSP-specific configuration, you can control how aggressively the FSP sells flexibility, the price levels, and the time coverage.

---

## Typical usage pattern

1. Ensure baselines have been created/updated (e.g. with `baseline_updater.py`).
2. Run `trader_dso.py` to submit DSO Buy orders for upcoming slots.
3. Run `trader_fsp.py` shortly afterward (or on a schedule) so that FSP Sell offers react to current DSO requests.

---

## Dependencies and environment

- Requires `classes.dso.DSO`, `classes.fsp.FSP`, `classes.fmo.FMO`, `classes.postgresql_interface.PostgreSQLInterface`.
- Requires `classes.flexibility_forecaster.FlexibilityForecaster` for discretization-aware flexibility calculation.
- Requires `classes.bidding_strategy.BiddingStrategy`, `classes.bidding_strategy.StrategyManager` for strategy-based bidding.
- Requires `classes.bid_record_repository.BidRecordRepository` for storing bid records.
- Requires `classes.demand_record_repository.DemandRecordRepository` for storing DSO demand records.
- Requires `pandas` for the helper function and data manipulation.
- Requires `influxdb.InfluxDBClient` for historical data queries.
- Needs configuration and connections as defined in `conf/test_fm01_aem.json` and the referenced `connectionsFile`.

---

## Troubleshooting

### Under-delivery due to discrete constraints

If the log shows significant under-delivery:
```
RECOMMENDED BID (quantity used for bidding): 15.000 kW (-5.00 kW / -25.0% under target)
```

This means the discrete asset combinations cannot reach the target. Consider:
1. Adding more discrete assets to the portfolio
2. Including continuous assets (EVs) in the strategy
3. Reducing the flexibility target

### Over-delivery due to discrete constraints

If the log shows over-delivery:
```
RECOMMENDED BID (quantity used for bidding): 19.000 kW (+4.00 kW / +26.7% over target)
```

This is expected when the best discrete combination exceeds the target. The system prefers slight under-delivery by default. To change this behavior, adjust the `_calculate_best_bid()` logic in `FlexibilityForecaster`.

### No continuous assets for fine-tuning

If your strategy only allows heat pumps (discrete assets), you lose the ability to fine-tune bids:
- Consider using strategies that include EV chargers (continuous)
- Or accept that bids will be at discrete power levels only
# baseline_updater.py

This script updates the baselines for a given Flexibility Service Provider (FSP) in the Opentunity-CH flexibility market setup.

It is intended to be run periodically (e.g., via cron or a scheduler) to refresh baselines that will later be used by the FSP trading logic and by the FMO (Flexibility Market Operator).

## High-level behaviour

1. Reads a JSON configuration file passed via command line.
2. Merges it with the connection configuration pointed to by `connectionsFile`.
3. Instantiates an `FSP` object using the configuration under `fm.actors.fsps[<FSP_ID>]`.
4. Connects the FSP to the Nodes API (via `nodes_interface`) and prints basic information.
5. Calls `fsp.update_baselines(cfg["baseline"])`, which actually performs the baseline update according to the `baseline` configuration section.

All market details (market name, actors, baseline source, etc.) come from the JSON config file, for example `conf/test_fm01_aem.json`.

---

## Command-line interface

```bash
python scripts/baseline_updater.py \
  --config_file conf/test_fm01_aem.json \
  --fsp supsi01 \
  --log_file logs/baseline_updater.log
```

**Arguments**

- `--config_file` (required): path to a JSON configuration file. For example: `conf/test_fm01_aem.json`.
- `--fsp` (required): identifier of the FSP to use. This must match a key under `fm.actors.fsps` in the configuration (e.g. `supsi01`, `supsi02`).
- `--log_file` (optional): path to a log file. If omitted, logs are printed to stdout.

If the configuration file does not exist, the script exits with code 1 and prints an error.

---

## Configuration structure

The script expects at least the following keys in the main configuration JSON:

```json
{
  "connectionsFile": "../conf/private/conns.json",
  "baseline": { ... },
  "fm": {
    "marketName": "Opentunity-CH",
    "actors": {
      "fsps": {
        "supsi01": { ... },
        "supsi02": { ... }
      }
    }
  }
}
```

### `connectionsFile`

Path to a JSON file with connection settings (e.g. URLs, credentials, PostgreSQL parameters). This file is loaded and its keys are merged into the main `cfg` dictionary.

### `baseline` section

Example from `conf/test_fm01_aem.json`:

```json
"baseline": {
  "source": "file",
  "shiftMinutes": 30,
  "fileSettings": {
    "profileFile": "../data/baselines/example01.csv"
  },
  "dbSettings": {
    "upcomingHoursToQuery": 24,
    "daysToGoBack": 7
  }
}
```

This object is passed as-is to `fsp.update_baselines()` and usually controls:

- **`source`**: how baselines are generated.
  - `"file"`: load an external CSV profilefile (see `fileSettings.profileFile`).
  - other values may be supported by `FSP.update_baselines` (e.g. `"db"`) depending on implementation.
- **`shiftMinutes`**: temporal shift applied to baseline timestamps relative to current time.
- **`fileSettings.profileFile`**: path to a CSV containing a reference profile used to build baselines.
- **`dbSettings`**: parameters for DB-based baselines (if `source` uses them), e.g.:
  - `upcomingHoursToQuery`: horizon into the future.
  - `daysToGoBack`: history length for baseline calculation.

### `fm.actors.fsps[<FSP_ID>]`

For each FSP, e.g. `supsi01`:

```json
"fsps": {
  "supsi01": {
    "id": "SUPSI",
    "name": "SUPSI",
    "role": "fsp",
    "baselines": {
      "tmpFolder": "../data/tmp",
      "fromBeforeNowHours": 12,
      "toAfterNowHours": 12
    },
    "orderSection": { ... },
    "pricing": { ... },
    "forecast": { ... },
    "contractSection": { ... }
  }
}
```

The script uses this block to instantiate the `FSP` class:

```python
fsp = FSP(cfg["fm"]["actors"]["fsps"][fsp_identifier], cfg, logger)
```

Within `FSP`, the `baselines` subsection typically controls:

- `tmpFolder`: where temporary baseline files are stored.
- `fromBeforeNowHours`: how many hours before now the baseline should start.
- `toAfterNowHours`: how many hours after now the baseline should extend.

---

## Execution steps in detail

1. **Argument parsing** using `argparse`.
2. **Configuration loading**:
   - Load `config_file` JSON.
   - Read `cfg["connectionsFile"]` and merge its content into `cfg`.
3. **Logging setup**: basic configuration with `logging.basicConfig` using the given log file (or stdout).
4. **FSP initialization**:
   - `fsp_identifier = args.fsp`.
   - `fsp = FSP(cfg["fm"]["actors"]["fsps"][fsp_identifier], cfg, logger)`.
   - `user_info = fsp.nodes_interface.get_user_info()`.
   - `fsp.set_markets(filter_dict={"name": cfg["fm"]["marketName"]})`.
   - `fsp.set_organization(filter_dict={"name": fsp.cfg["id"]})`.
   - `fsp.print_user_info(user_info)` and `fsp.print_player_info()` log useful info.
5. **Baseline update**:
   - `fsp.update_baselines(cfg["baseline"])` triggers creation/upload of baselines to the Node / DB. The details depend on the `FSP` implementation and the `baseline` config.

---

## Typical usage pattern

1. Configure FSPs and baselines in `conf/test_fm01_aem.json` (or similar config file).
2. Run `baseline_updater.py` for each FSP you want to maintain baselines for, e.g. hourly.
3. Later, use `trader_fsp.py` to place offers based on the baselines.

---

## Dependencies and environment

- Python 3.10+
- Project modules available in `classes/` (notably `classes.fsp.FSP`).
- Configuration file structured as shown above.
- Access to the Nodes platform and/or database as configured in `connectionsFile`.

The script assumes it is executed from the project root or with paths in the configuration adjusted accordingly.
