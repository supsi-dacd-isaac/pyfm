# baseline_updater.py

This script updates the baselines for a given Flexibility Service Provider (FSP) in the Opentunity-CH flexibility market setup.

It is intended to be run periodically (e.g., via cron or a scheduler) to refresh baselines that will later be used by the FSP trading logic and by the FMO (Flexibility Market Operator).

## High-level behaviour

1. Reads a JSON configuration file passed via command line.
2. Merges it with the connection configuration pointed to by `connectionsFile`.
3. Instantiates an `FSP` object using the configuration under `fm.actors.fsps[<FSP_ID>]`.
4. Connects the FSP to the Nodes API (via `nodes_interface`) and prints basic information.
5. Calls `fsp.update_baselines(cfg["baseline"])`, which performs the baseline update according to the `baseline` configuration section.

All market details (market name, actors, baseline source, asset mappings, etc.) come from the JSON config file, for example `conf/test_fm01_aem.json`.

---

## Command-line interface

Run from the project root:

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

You can also use the provided shell wrapper from the `scripts/` directory:

```bash
cd scripts
./baseline_updater.sh
```

The wrapper currently runs:

```bash
python baseline_updater.py --config_file ../conf/test_fm01_aem.json --fsp supsi01
```

So it expects the current working directory to be `scripts/`.

If you prefer to run it from the project root, use:

```bash
scripts/baseline_updater.sh
```

after adjusting the wrapper command to `python scripts/baseline_updater.py --config_file conf/test_fm01_aem.json --fsp supsi01`.

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
  "source": "db",
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

This object is passed as-is to `fsp.update_baselines()` and controls:

- **`source`**: how baselines are generated.
  - `"db"`: query historical asset measurements from InfluxDB and shift them forward as a persistence baseline.
  - `"file"`: currently selected by the code path, but `FSP.create_df_baseline_from_file()` is not implemented in `classes/fsp.py`; using this source will fail unless that method is added.
- **`shiftMinutes`**: temporal shift applied to the rounded current time before calculating the baseline window.
- **`fileSettings.profileFile`**: intended path to a CSV profile for file-based baselines, but unused until file support is implemented.
- **`dbSettings`**: parameters for DB-based baselines, e.g.:
  - `upcomingHoursToQuery`: horizon into the future.
  - `daysToGoBack`: history length for baseline calculation.

DB baselines also require:

- A root-level `asset_mapping` section. Each portfolio asset name is looked up in that mapping to determine the InfluxDB `device_name` tag and field to query. The asset MPID from Nodes is used as the InfluxDB `site`.
- `influxDB.assetsMeasurement` in `connectionsFile`, which names the source measurement for historical asset data. In the current private config this is `assets_data`.
- `influxDB.saveBaselineMeasurement` in `connectionsFile`, which enables or disables saving generated baselines back to InfluxDB.
- `influxDB.baselineMeasurement` in `connectionsFile`, which names the destination measurement where generated baselines are saved when `saveBaselineMeasurement` is `true`. In the current private config this is `baseline_intervals`.

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
   - `fsp.update_baselines(cfg["baseline"])` rounds the current UTC time down to a 15-minute boundary and adds `baseline.shiftMinutes`.
   - For every portfolio managed by the FSP, it builds a baseline DataFrame.
   - With `source: "db"`, it queries InfluxDB from `influxDB.assetsMeasurement` for the same time window `daysToGoBack` days in the past, aggregates all mapped asset measurements per timestamp, shifts those timestamps forward by `daysToGoBack`, converts W to MW, and formats the result as Nodes baseline intervals.
   - If a portfolio has no mapped assets, it uploads a zero baseline over `upcomingHoursToQuery`.
   - If mapped assets exist but no valid measurements are returned, that portfolio is skipped.
   - `fsp.update_portfolio_baseline()` writes the DataFrame to `<tmpFolder>/<portfolio_id>.csv` and uploads it to the Nodes `BaselineIntervals/import` endpoint.
   - If `influxDB.saveBaselineMeasurement` is `true`, `fsp.save_portfolio_baseline_to_influx()` writes the same generated baseline points to `influxDB.baselineMeasurement`.

The generated CSV columns are:

```text
assetPortfolioId,periodFrom,periodTo,quantity,quantityType
```

When enabled, the generated InfluxDB baseline measurement uses `periodFrom` as the time index.

Tags:

- `asset_portfolio_id`
- `fsp_id`
- `fsp_name`
- `market_name`
- `source`
- `quantity_type`

Fields:

- `quantity_w`
- `period_to`
- `granularity_minutes`

## How to read a successful run log

A successful DB-based run normally shows these phases:

1. **Authentication and setup**
   - Existing Nodes token is reused or refreshed.
   - InfluxDB connection is opened.
   - Nodes organization, portfolios, assets, grid assignments, market, and user information are fetched.
2. **Asset mapping**
   - Each portfolio asset is mapped to an InfluxDB query target, for example:
     - `Asset ECM97.3 -> site=ECM97, device=v_shelly_3em_pro_heat_pump, field=active_power`
     - `Asset ECM63.1 -> site=ECM63, device=charge_point_ev_1, field=power`
3. **Historical InfluxDB queries**
   - One query is executed per mapped asset.
   - In a run started at `2026-05-06 10:31` with `shiftMinutes: 30`, the adjusted baseline start is `2026-05-06T09:00:00Z` because the code uses UTC time, rounds down to the previous 15-minute boundary, then adds 30 minutes.
   - With `daysToGoBack: 7` and `upcomingHoursToQuery: 24`, the query window is `2026-04-29T09:00:00Z` to `2026-04-30T09:00:00Z`.
4. **Baseline upload**
   - The generated baseline is shifted forward by 7 days and uploaded for the current/future period.
   - The log line `Update baseline of portfolio ..., period [2026-05-06 09:15:00+00:00-2026-05-07 09:00:00+00:00]` reports the first and last `periodTo` values. The corresponding `periodFrom` values start at `09:00` and end at `08:45`.
   - The `Baseline times`, `Baseline values (MW)`, and `Baseline statistics (MW)` lines summarize the CSV payload before upload.
   - A `POST ... BaselineIntervals/import, status code: 200` line confirms that Nodes accepted the baseline import.
   - A `Saved ... baseline points to InfluxDB measurement baseline_intervals ...` line confirms that the same baseline was persisted to InfluxDB when `saveBaselineMeasurement` is enabled. If disabled, the log reports that baseline saving was skipped.

---

## Typical usage pattern

1. Configure FSPs and baselines in `conf/test_fm01_aem.json` (or a similar config file).
2. Run `baseline_updater.py` for each FSP you want to maintain baselines for, e.g. hourly.
3. Later, use `trader_fsp.py` to place offers based on the updated baselines.

---

## Dependencies and environment

- Python 3.10+
- Project modules available in `classes/` (notably `classes.fsp.FSP`).
- Configuration file structured as shown above.
- Access to the Nodes platform and/or database as configured in `connectionsFile`.
- For `source: "db"`, InfluxDB access, root-level `asset_mapping` entries for the portfolio assets, and write permission on `influxDB.baselineMeasurement`.

The script resolves `config_file`, `connectionsFile`, and other relative paths against the current working directory, not against the location of the config file. Run it from the directory expected by the paths, or use paths adjusted for your working directory.

