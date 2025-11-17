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

You can also use the provided shell wrapper:

```bash
scripts/baseline_updater.sh
```

(adjust it as needed to point to your config and FSP id.)

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
  - `"file"`: load an external CSV profile (see `fileSettings.profileFile`).
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

1. Configure FSPs and baselines in `conf/test_fm01_aem.json` (or a similar config file).
2. Run `baseline_updater.py` for each FSP you want to maintain baselines for, e.g. hourly.
3. Later, use `trader_fsp.py` to place offers based on the updated baselines.

---

## Dependencies and environment

- Python 3.10+
- Project modules available in `classes/` (notably `classes.fsp.FSP`).
- Configuration file structured as shown above.
- Access to the Nodes platform and/or database as configured in `connectionsFile`.

The script assumes it is executed from the project root or with paths in the configuration adjusted accordingly.

