# baseline_updater.py

This script updates portfolio baselines for an FSP in the Opentunity-CH setup.

It supports two DB-backed baseline strategies:

- `persistence`: the original shifted historical baseline
- `aem_forecast_actual`: direct retrieval from AEM measurement `hyp_baseline_forecast_actual`

The implementation is additive. The persistence path remains available and is still the conservative default.

## Command-line interface

Run from the project root:

```bash
python scripts/baseline_updater.py \
  --config_file conf/test_fm01_aem.json \
  --fsp supsi01 \
  --log_file logs/baseline_updater.log
```

Arguments:

- `--config_file`: configuration file path
- `--fsp`: FSP identifier under `fm.actors.fsps`
- `--log_file`: optional log file path
- `--dry_run`: build and log the baseline payload without uploading it to Nodes or saving it to InfluxDB

If `--dry_run` is omitted, production behavior is unchanged.

## High-level behavior

1. Load the main config JSON.
2. Load and merge the connection config from `connectionsFile`.
3. Apply conservative defaults if they are missing:
   - `baseline.strategy = "persistence"`
   - `baseline.missingAssetPolicy = "zero_fill"`
4. Instantiate `FSP`.
5. Call `fsp.update_baselines(cfg["baseline"])`.

## Baseline configuration

Example:

```json
"baseline": {
  "source": "db",
  "strategy": "persistence",
  "missingAssetPolicy": "zero_fill",
  "shiftMinutes": 30,
  "fileSettings": {
    "profileFile": "../data/baselines/example01.csv"
  },
  "dbSettings": {
    "upcomingHoursToQuery": 24,
    "daysToGoBack": 7
  },
  "aemForecastActualSettings": {
    "measurement": "hyp_baseline_forecast_actual"
  }
}
```

Supported keys:

- `source`
  - `"db"`: baseline comes from InfluxDB-backed logic
  - `"file"`: legacy file branch selected by the script, but `create_df_baseline_from_file()` is still not implemented
- `strategy`
  - `"persistence"`: original shifted historical baseline
  - `"aem_forecast_actual"`: one-slot incremental baseline from AEM forecast actual
- `missingAssetPolicy`
  - `"zero_fill"`: missing assets in AEM contribute `0` for that asset
  - `"persistence_fallback"`: missing assets in AEM are filled per asset with the persistence logic
  - `"skip"`: missing assets are excluded from the final payload
- `shiftMinutes`: offset applied after rounding current UTC time down to the 15-minute boundary
- `dbSettings.upcomingHoursToQuery`: persistence horizon
- `dbSettings.daysToGoBack`: persistence lookback
- `aemForecastActualSettings.measurement`: AEM source measurement, default `hyp_baseline_forecast_actual`
- `dryRun` / `dry_run`: config-level equivalent of `--dry_run`

## What "select the strategy" means

It means the script reads `baseline.strategy` from the configuration and chooses which baseline-building function to run.

- If `baseline.strategy = "persistence"`, it uses the original shifted historical baseline logic.
- If `baseline.strategy = "aem_forecast_actual"`, it reads the next quarter-hour baseline directly from `hyp_baseline_forecast_actual`.

In practice, "select the strategy" is just a configuration choice.

Examples:

### Example 1: keep the original behavior

```json
"baseline": {
  "source": "db",
  "strategy": "persistence",
  "shiftMinutes": 30,
  "dbSettings": {
    "upcomingHoursToQuery": 24,
    "daysToGoBack": 7
  }
}
```

This is the conservative default. The script builds the original 96-point shifted baseline series.

### Example 2: use AEM for the next quarter-hour, fill missing assets with `0`

```json
"baseline": {
  "source": "db",
  "strategy": "aem_forecast_actual",
  "missingAssetPolicy": "zero_fill",
  "shiftMinutes": 30,
  "dbSettings": {
    "upcomingHoursToQuery": 24,
    "daysToGoBack": 7
  },
  "aemForecastActualSettings": {
    "measurement": "hyp_baseline_forecast_actual"
  }
}
```

This is the new default AEM configuration. The script tries to read the upcoming slot from AEM and, if an asset is missing, it contributes `0` for that asset.

### Example 3: use AEM for the next quarter-hour, fallback missing assets to persistence

```json
"baseline": {
  "source": "db",
  "strategy": "aem_forecast_actual",
  "missingAssetPolicy": "persistence_fallback",
  "shiftMinutes": 30,
  "dbSettings": {
    "upcomingHoursToQuery": 24,
    "daysToGoBack": 7
  },
  "aemForecastActualSettings": {
    "measurement": "hyp_baseline_forecast_actual"
  }
}
```

Use this only if you explicitly want the old fallback behavior for missing assets.

### Example 4: use AEM for the next quarter-hour, skip missing assets

```json
"baseline": {
  "source": "db",
  "strategy": "aem_forecast_actual",
  "missingAssetPolicy": "skip",
  "shiftMinutes": 30,
  "dbSettings": {
    "upcomingHoursToQuery": 24,
    "daysToGoBack": 7
  },
  "aemForecastActualSettings": {
    "measurement": "hyp_baseline_forecast_actual"
  }
}
```

This also reads the upcoming slot from AEM, but if an asset is missing, that asset is excluded from the final payload instead of being backfilled.

### Example 5: validate the AEM strategy without uploading

```bash
python scripts/baseline_updater.py \
  --config_file conf/test_fm01_aem.json \
  --fsp supsi01 \
  --dry_run
```

This does not change the selected strategy by itself. It just tells the script to build and log the payload without uploading or storing it.

## Strategy behavior

### `persistence`

This is the original behavior.

For each mapped asset in the portfolio:

1. Query `influxDB.assetsMeasurement` for the window starting `daysToGoBack` days in the past.
2. Aggregate asset values in W.
3. Shift timestamps forward by `daysToGoBack`.
4. Convert W to MW for the Nodes payload.

With the common configuration:

- `upcomingHoursToQuery = 24`
- `granularity = 15`

the persistence strategy produces the original 96-point shifted baseline series.

### `aem_forecast_actual`

This strategy queries AEM measurement:

- measurement: `hyp_baseline_forecast_actual`
- tags: `asset_id`, `asset_label`, `unit`
- field: `value`

Observed unit for `value` is W.

The strategy:

1. Computes the upcoming quarter-hour slot:
   - `periodFrom = adjusted_time`
   - `periodTo = adjusted_time + granularity`
2. Queries `hyp_baseline_forecast_actual` for that single slot only.
3. Matches rows to portfolio assets by `asset_label`.
4. Sums the included asset values in W.
5. Converts the total from W to MW for the Nodes payload.

This strategy intentionally emits a one-row incremental baseline update for the upcoming quarter-hour only.

This is compatible with the current downstream flow:

- Nodes accepts partial interval imports
- later intervals remain available from previously imported baselines
- `trader_fsp.py` reads the baseline for the specific slot it needs

## Missing asset handling

Missing assets are handled per asset, not globally.

### `missingAssetPolicy = "zero_fill"`

If an AEM row is missing for an asset:

1. The asset is logged as missing.
2. `0.0 W` is added for that asset.

This is the default missing-asset behavior.

### `missingAssetPolicy = "persistence_fallback"`

If an AEM row is missing for an asset:

1. The asset is queried with the existing persistence logic for the same quarter-hour.
2. That fallback value is added to the final aggregate.

### `missingAssetPolicy = "skip"`

If an AEM row is missing for an asset:

1. The asset is logged as missing.
2. Nothing is added for that asset.

Skipped assets are excluded from the final payload. They are not added as zeroes.

## Asset mapping

For persistence:

- `site` comes from the Nodes MPID or `asset_mapping.<asset>.pod`
- `device_name` comes from `asset_mapping.<asset>.device_name_tag`
- `field` comes from `asset_mapping.<asset>.field`

For AEM forecast actual:

- matching is done with `asset_label`
- by default this uses the portfolio asset name, for example `ECM96.2`
- optional overrides can be provided in `asset_mapping`:
  - `aem_baseline_asset_label`
  - `baseline_asset_label`

## Units and storage

Unit handling is explicit:

- source values from `assets_data` are in W
- source values from `hyp_baseline_forecast_actual` are in W
- Nodes baseline payload uses MW
- `portfolios_baselines` stores `quantity_w` in W

Conversions:

- W -> MW before Nodes upload
- MW -> W before saving to `portfolios_baselines`

In the current private connection config:

- `influxDB.assetsMeasurement = "assets_data"`
- `influxDB.baselineMeasurement = "portfolios_baselines"`

## Dry-run mode

Use `--dry_run` for safe validation.

Example:

```bash
python scripts/baseline_updater.py \
  --config_file conf/test_fm01_aem.json \
  --fsp supsi01 \
  --dry_run
```

In dry-run mode the script:

- builds the baseline payload
- logs the times, values, and statistics
- does not upload to Nodes
- does not save to `portfolios_baselines`

This is useful for validating historical slots or strategy behavior without causing Nodes `400` errors for past timestamps.

## Execution flow

For `source = "db"`:

1. Compute `adjusted_time` from current UTC time and `shiftMinutes`.
2. Read `baseline.strategy` from the config and select the matching path:
   - `persistence` -> original shifted historical baseline
   - `aem_forecast_actual` -> one-slot AEM baseline for the upcoming quarter-hour
3. Build a baseline DataFrame.
4. If `dry_run` is enabled:
   - log the payload summary
   - stop before upload/storage
5. Otherwise:
   - upload to Nodes `BaselineIntervals/import`
   - save the same payload to `portfolios_baselines` if enabled

## Log behavior

The script logs:

- selected baseline strategy
- missing-asset policy
- dry-run state
- upcoming quarter-hour slot
- persistence query details
- AEM query details
- number of AEM assets returned
- assets missing from AEM
- whether each missing asset used fallback or skip
- final set of assets included
- baseline times, values, and statistics

For AEM strategy, the logs also state that:

- AEM values are in W
- AEM emits a single-row upcoming-slot update
- Nodes accepts partial interval imports

## Operational notes

- The script resolves config paths relative to the current working directory.
- The provided shell wrapper expects to run from `scripts/`.
- `source = "file"` is still not a functional production path because `create_df_baseline_from_file()` is not implemented in `classes/fsp.py`.
