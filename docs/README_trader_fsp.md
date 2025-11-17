# trader_fsp.py

This script represents the **FSP trading agent** in the Opentunity-CH flexibility market. It reacts to the DSO’s flexibility requests and places corresponding **Sell** orders based on the FSP’s portfolios and baselines.

## High-level behaviour

1. Reads configuration from a JSON file and merges `connectionsFile`.
2. Instantiates both a `DSO` object (to read flexibility requests) and an `FSP` object (to place Sell orders).
3. Computes the target market timeslot (`slot_time`) from `fm.granularity` and `fm.ordersTimeShift`.
4. Uses the DSO instance to query the market for current flexibility requests (`dso.get_flexibility_requests`).
5. Downloads or refreshes baselines for the FSP (`fsp.download_baselines`).
6. For each DSO request and each FSP portfolio, calls `fsp.sell_flexibility` to construct Sell orders.
7. Posts resulting Sell orders to the market ledger through an `FMO` instance.

There is also a helper function `create_dataframe_for_portfolio_baseline` that shows how to build a baseline DataFrame from a CSV file, although it is not used in the main flow.

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
  --log_file logs/trader_fsp.log
```

**Arguments**

- `--config_file` (required): path to configuration JSON.
- `--fsp` (required): FSP identifier (key under `fm.actors.fsps`, e.g. `supsi01`).
- `--log_file` (optional): path to log file. If omitted, logs go to stdout.

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
    "source": "constant",
    "constant": 5.0,
    "forecasting_multiplier": 1.0,
    "activationCost": 1.0
  },
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

- **`baselines`**: controls how long the baseline horizon is and where temporary files are stored. This is used when the FSP downloads and maintains baselines.
- **`orderSection.quantityPercBaseline`**: percentage of the available baseline flexibility to offer to the market.
- **`orderSection.mainSettings`**: basic order parameters for Sell offers.
- **`pricing`**: how the Sell offer prices are set (constant or forecast-based, plus activation cost, etc.).
- **`forecast`**: how the FSP obtains forecasts (e.g. from AEM CSV).

The `FSP` class uses these parameters in methods such as `download_baselines` and `sell_flexibility`.

---

## Detailed execution steps

1. **Argument parsing**<br>
   Uses `argparse` to read `--config_file`, `--fsp`, and `--log_file`.

2. **Configuration loading**<br>
   - Reads the main JSON configuration file.
   - Reads and merges the JSON from `cfg["connectionsFile"]`.

3. **Logging setup**<br>
   Configures logging via `logging.basicConfig` with the given log file or stdout.

4. **Database connection (optional)**<br>
   Tries to create a `PostgreSQLInterface(cfg["postgreSQL"], logger)`. On failure, logs an error and continues.

5. **Timeslot calculation**<br>
   - Creates a temporary `DSO` instance for time alignment:

     ```python
     dso = DSO(cfg["fm"]["actors"]["dso"], cfg, logger)
     dso.set_organization(filter_dict={"name": dso.cfg["id"]})
     slot_time = dso.get_adjusted_time(cfg["fm"]["granularity"], cfg["fm"]["ordersTimeShift"])
     ```

   - `slot_time` is the reference timeslot for both demand and offers.

6. **FSP setup**<br>
   - Initialize FSP:

     ```python
     fsp = FSP(cfg["fm"]["actors"]["fsps"][fsp_identifier], cfg, logger)
     user_info = fsp.nodes_interface.get_user_info()
     fsp.set_markets(filter_dict={"name": cfg["fm"]["marketName"]})
     fsp.set_organization(filter_dict={"name": fsp.cfg["id"]})
     ```

   - Logs market id and market name for traceability.

7. **Obtain DSO flexibility requests**<br>
   - The script calls:

     ```python
     dso_demands = dso.get_flexibility_requests(
         slot_time, cfg["fm"]["granularity"], "Buy", "Power"
     )
     ```

   - This should return a list of demand objects for the given slot, side, and product type (e.g. Power).

8. **Baseline download**<br>
   - Calls `fsp.download_baselines(slot_time)` to ensure current baselines are available for all portfolios and assets.

9. **FMO initialization**<br>
   - `fmo = FMO(fsp.cfg, logger, pgi)`.

10. **Offer construction and posting**<br>
    For every DSO demand and for every FSP portfolio:

    ```python
    for dso_demand in dso_demands:
        for p_k in fsp.portfolios.keys():
            resp_selling = fsp.sell_flexibility(slot_time, p_k, dso_demand)
            for k in resp_selling.keys():
                if resp_selling[k] is not False:
                    fmo.add_entry_to_market_ledger(
                        timeslot=slot_time,
                        player=fsp,
                        portfolio=fsp.portfolios[p_k].metadata["name"],
                        features=resp_selling[k],
                    )
    ```

    - `sell_flexibility` returns a dictionary of order descriptions (possibly one per product, type or time slice).
    - Only non-`False` entries are posted to the market ledger.

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

By changing the FSP-specific configuration, you can control how aggressively the FSP sells flexibility, the price levels, and the time coverage.

---

## Typical usage pattern

1. Ensure baselines have been created/updated (e.g. with `baseline_updater.py`).
2. Run `trader_dso.py` to submit DSO Buy orders for upcoming slots.
3. Run `trader_fsp.py` shortly afterward (or on a schedule) so that FSP Sell offers react to current DSO requests.

---

## Dependencies and environment

- Requires `classes.dso.DSO`, `classes.fsp.FSP`, `classes.fmo.FMO`, `classes.postgresql_interface.PostgreSQLInterface`.
- Requires `pandas` for the helper function.
- Needs configuration and connections as defined in `conf/test_fm01_aem.json` and the referenced `connectionsFile`.
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
