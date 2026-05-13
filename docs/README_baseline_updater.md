# baseline_updater.py

This script updates the baselines for a given Flexibility Service Provider (FSP) in the Opentunity-CH flexibility market setup.

It is intended to be run periodically (e.g., via cron or a scheduler) to refresh baselines that will later be used by the FSP trading logic and by the FMO (Flexibility Market Operator).

## High-level behaviour

1. Reads a JSON configuration file passed via command line.
2. Merges it with the connection configuration pointed to by `connectionsFile`.
3. Instantiates an `FSP` object using the configuration under `fm.actors.fsps[<FSP_ID>]`.
4. Connects the FSP to the Nodes API (via `nodes_interface`) and prints basic information.
5. Calls `fsp.update_baselines(cfg["baseline"], dry_run=args.dry_run)`, which performs the baseline update according to the `baseline` configuration section.

All market details (market name, actors, baseline source, asset mappings, etc.) come from the JSON config file, for example `conf/test_fm01_aem.json`.

---

## Command-line interface

Run from the project root:

```bash
python scripts/baseline_updater.py \
  --config_file conf/test_fm01_aem.json \
  --fsp supsi01 \
  --dry-run \
  --log_file logs/baseline_updater.log
```

**Arguments**

- `--config_file` (required): path to a JSON configuration file. For example: `conf/test_fm01_aem.json`.
- `--fsp` (required): identifier of the FSP to use. This must match a key under `fm.actors.fsps` in the configuration (e.g. `supsi01`, `supsi02`).
- `--dry-run` (optional): build and log baseline DataFrames without uploading them to NODES or saving them to InfluxDB.
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
  "shiftMinutes": 90,
  "fileSettings": {
    "profileFile": "../data/baselines/example01.csv"
  },
  "dbSettings": {
    "strategy": "slot_persistence",
    "upcomingHoursToQuery": 24,
    "daysToGoBack": 7,
    "persistenceGoBackMinutes": 90,
    "uploadOnlyComputableHorizon": true,
    "maxSlotsToUpload": 1,
    "missingMeasurementPolicy": "zero_fill_asset"
  }
}
```

This object is passed as-is to `fsp.update_baselines()` and controls:

- **`source`**: how baselines are generated.
  - `"db"`: query historical asset measurements from InfluxDB.
  - `"file"`: currently selected by the code path, but `FSP.create_df_baseline_from_file()` is not implemented in `classes/fsp.py`; using this source will fail unless that method is added.
- **`shiftMinutes`**: temporal shift applied to the rounded current UTC time before calculating the baseline window. In the operational setup this should match the closed market window; the current config uses `90`.
- **`fileSettings.profileFile`**: intended path to a CSV profile for file-based baselines, but unused until file support is implemented.
- **`dbSettings`**: parameters for DB-based baselines, e.g.:
  - `upcomingHoursToQuery`: horizon into the future.
  - `strategy`: DB baseline strategy.
    - `slot_persistence`: use `baseline_i(t) = measured_power_i(t - persistenceGoBackMinutes)`.
    - `day_persistence` / `legacy` / missing strategy: preserve the existing day-based behavior using `daysToGoBack`.
  - `daysToGoBack`: history length for the legacy day-based strategy.
  - `persistenceGoBackMinutes`: slot-level persistence lag in minutes. It must align with `fm.granularity`.
  - `uploadOnlyComputableHorizon`: for `slot_persistence`, restrict uploads to target slots whose lagged source measurements are already available. The default is `true`.
  - `maxSlotsToUpload`: for `slot_persistence` with `uploadOnlyComputableHorizon: true`, cap the number of computable slots uploaded in one run. The default is `1`. A positive integer uploads at most that many slots; `0`, `null`, or `"all"` uploads all currently computable slots.
  - `missingMeasurementPolicy`: `fail_portfolio`, `skip_asset`, or `zero_fill_asset` for `slot_persistence`.
- **`asset_mapping.<asset>.baseline_persistence_go_back_minutes`**: optional per-asset override for `slot_persistence`. If omitted, the asset uses `baseline.dbSettings.persistenceGoBackMinutes`. The value must align with `fm.granularity`.

For the operational persistence mode:

```text
current_time_utc          = 2026-05-12 08:20
rounded_time_utc          = 2026-05-12 08:15
target_slot_utc           = rounded_time_utc + shiftMinutes = 2026-05-12 09:45
baseline_source_time_utc  = target_slot_utc - persistenceGoBackMinutes = 2026-05-12 08:15
```

The baseline is built asset first and aggregated at portfolio level:

```text
baseline_i(t) = measured_power_i(t - persistenceGoBackMinutes)
portfolio_baseline(t) = sum_i baseline_i(t)
```

When per-asset overrides are configured, the formula becomes:

```text
baseline_go_back_i =
    asset_mapping.<asset>.baseline_persistence_go_back_minutes
    if present
    else baseline.dbSettings.persistenceGoBackMinutes

source_time_i(t) = t - baseline_go_back_i
baseline_i(t) = measured_power_i(source_time_i(t))
portfolio_baseline(t) = sum_i baseline_i(t)
```

The target slots remain common for the NODES portfolio, but each asset can read from a different lagged source timestamp. This is useful when assets stay in the same NODES portfolio but have different telemetry latency. In the current operational config the HP assets `ECM96.2` and `ECM97.3` use 90 minutes, while delayed EV charger assets `ECM63.1` and `ECM63.2` use 120 minutes.

With a 90-minute persistence lag, the updater cannot usually generate a full 24-hour horizon in one run. For example, if `current_time_utc` is `12:00` and the first open target slot is `13:30`, the source measurement for that first target is `12:00` and can be queried. The next target slot, `13:45`, requires a source measurement at `12:15`, which does not exist yet at `12:00`.

The computable horizon is the contiguous block of target slots for which the required lagged source measurements are available. With per-asset go-back overrides, a target slot is computable under `fail_portfolio` only when every mapped asset has its own required source measurement at its own source time. Under `skip_asset`, missing assets are omitted and the slot is computable when at least one asset contributes. Under `zero_fill_asset`, missing asset source measurements are explicit zero-valued addends, so the slot remains computable as long as the asset queries themselves complete. With `uploadOnlyComputableHorizon: true`, the updater stops at the first non-computable source slot. With `maxSlotsToUpload: 1`, only the first computable target slot is uploaded.

The operational default therefore gives rolling incremental baseline upload:

```text
persistenceGoBackMinutes = 90
maxSlotsToUpload = 1
```

Every 15-minute run computes the first open target slot, reads each asset value at its configured lagged source time, sums the asset values into the portfolio baseline, and uploads that single baseline interval. For example, for target slot `13:45`, HP assets with a 90-minute lag read `12:15`, while EV assets with a 120-minute lag read `11:45`. NODES accepts overwriting baseline intervals, so a deployment can upload more than one computable slot by increasing `maxSlotsToUpload`, for example to `4`. The default remains one slot to avoid trying to publish target slots whose source measurements are still in the future.

Missing measurements are never silently hidden and assets are not removed from the NODES portfolio. The available `slot_persistence` policies are:

- `fail_portfolio`: strict mode; any missing required asset measurement prevents the portfolio baseline.
- `skip_asset`: omits missing assets from the sum; useful for partial portfolios but can understate the true portfolio if used carelessly.
- `zero_fill_asset`: keeps the asset in the portfolio contribution list and uses `0 W` when its source measurement is missing. Every zero-filled contribution is logged.

The operational baseline config uses `zero_fill_asset` so the rolling updater can still publish the next slot when delayed or missing telemetry affects one asset. The portfolio baseline remains the sum of all asset addends: measured values plus explicit zero-filled missing values.

The baseline summary logs asset-level addends for every generated slot, for example:

```text
Baseline addends for 2026-05-12T14:30:00Z:
  ECM97.3: 15.98 W (0.000016 MW), source=2026-05-12T13:00:00Z, go_back=90 min, status=measured
  ECM96.2: 0.00 W (0.000000 MW), source=2026-05-12T13:00:00Z, go_back=90 min, status=missing_zero_fallback
  ECM63.1: 11100.00 W (0.011100 MW), source=2026-05-12T12:30:00Z, go_back=120 min, status=measured
  ECM63.2: 0.00 W (0.000000 MW), source=2026-05-12T12:30:00Z, go_back=120 min, status=missing_zero_fallback
  portfolio total: 11115.98 W (0.011116 MW)
```

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
   - `fsp.update_baselines(cfg["baseline"], dry_run=args.dry_run)` rounds the current UTC time down to the configured `fm.granularity` boundary and adds `baseline.shiftMinutes`.
   - For every portfolio managed by the FSP, it builds a baseline DataFrame.
   - With `source: "db"` and `dbSettings.strategy: "slot_persistence"`, it builds the nominal target horizon from the shifted current time through `upcomingHoursToQuery`, queries InfluxDB from `influxDB.assetsMeasurement` for the lagged source window, aligns each target slot to its source slot, optionally restricts the upload to the computable horizon, sums asset contributions in W, and converts to MW only in the final Nodes payload.
   - With `source: "db"` and legacy/day persistence strategy, it preserves the existing behavior: query the same window `daysToGoBack` days in the past, aggregate all mapped asset measurements per timestamp, and shift those timestamps forward by `daysToGoBack`.
   - If a portfolio has no mapped assets, it uploads a zero baseline. In `slot_persistence` mode with the default computable-horizon settings, this is capped by `maxSlotsToUpload`.
   - If mapped assets exist but no valid measurements are returned, that portfolio is skipped or failed according to the selected missing-measurement policy.
   - `fsp.update_portfolio_baseline()` writes the DataFrame to `<tmpFolder>/<portfolio_id>.csv` and uploads it to the Nodes `BaselineIntervals/import` endpoint.
   - If `influxDB.saveBaselineMeasurement` is `true`, `fsp.save_portfolio_baseline_to_influx()` writes the same generated baseline points to `influxDB.baselineMeasurement`.
   - In `--dry-run` mode, the script still builds and logs the baseline DataFrame but skips both the NODES upload and the InfluxDB write.

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
   - In a run started at `2026-05-12 08:20` with `shiftMinutes: 90`, the first target slot is `2026-05-12T09:45:00Z` because the code uses UTC time, rounds down to the previous 15-minute boundary, then adds 90 minutes.
   - With `strategy: "slot_persistence"` and `persistenceGoBackMinutes: 90`, the first source slot is `2026-05-12T08:15:00Z`.
   - With `strategy: "day_persistence"` / legacy and `daysToGoBack: 7`, the query window remains one week earlier.
4. **Baseline upload**
   - In `slot_persistence`, the generated baseline is a future horizon built from lagged source slots.
   - In legacy/day persistence, the generated baseline is still shifted forward by `daysToGoBack` days.
   - The log line `Update baseline of portfolio ..., period [2026-05-06 09:15:00+00:00-2026-05-07 09:00:00+00:00]` reports the first and last `periodTo` values. The corresponding `periodFrom` values start at `09:00` and end at `08:45`.
   - The `Baseline times`, `Baseline values (MW)`, and `Baseline statistics (MW)` lines summarize the CSV payload before upload.
   - A `POST ... BaselineIntervals/import, status code: 200` line confirms that Nodes accepted the baseline import.
   - A `Saved ... baseline points to InfluxDB measurement baseline_intervals ...` line confirms that the same baseline was persisted to InfluxDB when `saveBaselineMeasurement` is enabled. If disabled, the log reports that baseline saving was skipped.
   - In `--dry-run` mode, upload/save lines are replaced by dry-run messages.

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
