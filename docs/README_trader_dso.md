# trader_dso.py

This script represents the **DSO trading agent** in the Opentunity-CH flexibility market. It is responsible for requesting flexibility from the market by placing **Buy** orders on behalf of the Distribution System Operator (DSO).

## High-level behaviour

1. Reads a JSON configuration file (`--config_file`).
2. Merges in connection settings from `connectionsFile`.
3. Creates a `DSO` actor, connects it to the Nodes platform, and selects the relevant market and grid area.
4. Computes the market timeslot for which to request flexibility (based on `fm.granularity` and `fm.ordersTimeShift`).
5. Uses `dso.demand_flexibility(slot_time)` to compute the actual flexibility request (quantity, price, etc.) based on configuration (including forecasts, peak, and orderSection).
6. If a flexibility request is produced, writes it into the market ledger via an `FMO` (market operator) object.

---

## Pricing model

The **price** of the DSO Buy order is controlled by the `orderSection.unitPrice` block in the configuration:

```json
"unitPrice": {
  "source": "forecast",
  "constant": 10.0,
  "forecast_base": 1.0,
  "forecast_multiplier": 10.0,
  "day_of_month_max_increase": 3.0
}
```

This block is interpreted inside `DSO.demand_flexibility(...)` and typically works as follows (exact maths are in `classes/dso.py`):

- **`source`**
  - `"forecast"`: the order price is derived from a forecast signal (e.g. spot prices or expected system stress) combined with the parameters below.
  - Other sources (e.g. `"constant"`) may be supported; in that case the constant value is used directly.
- **`constant`**: fallback / base price level when the source is constant or when no forecast data are available.
- **`forecast_base`** and **`forecast_multiplier`**: parameters that scale the forecast signal into a price, e.g.
  - `price = forecast_base + forecast_multiplier * forecast_value`.
- **`day_of_month_max_increase`**: optional rule to adjust prices depending on the day of month (e.g. higher prices near the end of the month).

In practice, when `trader_dso.py` calls:

```python
resp_demand = dso.demand_flexibility(slot_time)
```

the returned `resp_demand` structure already contains a `unitPrice` (or equivalent field) computed using this pricing configuration. `trader_dso.py` itself does not implement pricing logic; it just forwards whatever price the `DSO` object computed to the market ledger via `FMO.add_entry_to_market_ledger(...)`.

---

## Command-line interface

```bash
python scripts/trader_dso.py \
  --config_file conf/test_fm01_aem.json \
  --log_file logs/trader_dso.log
```

**Arguments**

- `--config_file` (required): path to configuration JSON.
- `--log_file` (optional): path to a log file. If omitted, logs are emitted on stdout.

---

## Configuration structure

The script relies on the same configuration format as `baseline_updater.py`. Key sections from `conf/test_fm01_aem.json`:

```json
{
  "connectionsFile": "../conf/private/conns.json",
  "fm": {
    "granularity": 15,
    "ordersTimeShift": 90,
    "marketName": "Opentunity-CH",
    "gridAreaName": "Switzerland",
    "actors": {
      "dso": { ... },
      "fsps": { ... }
    }
  }
}
```

### `fm` common parameters

- **`granularity`**: market resolution in minutes (e.g. 15 minutes).
- **`ordersTimeShift`**: time shift in minutes used to compute the target timeslot relative to “now” when placing orders (e.g. 90 minutes ahead).
- **`marketName`**: the Nodes market on which orders are placed.
- **`gridAreaName`**: the grid area in which the DSO is operating.

### DSO configuration `fm.actors.dso`

Example from `conf/test_fm01_aem.json`:

```json
"dso": {
  "id": "AEM",
  "name": "Switzerland",
  "role": "dso",
  "flexibilitySource": "forecast",
  "forecast": {
    "source": "aem",
    "filename": "../data/forecast/example01.csv"
  },
  "peak": {
    "source": "file",
    "filename": "../data/peak/example01.csv"
  },
  "orderSection": {
    "nodeName": "Massagno_1",
    "quantities": {
      "random": [0.005, 0.008, 0.01, 0.013, 0.015, 0.018, 0.02, 0.023, 0.025, 0.028, 0.03],
      "db": {
        "fields": ["CH2ActivePowL1", "CH2ActivePowL2", "CH2ActivePowL3"],
        "community": "ECM",
        "device": "sgim",
        "daysToGoBack": 7
      },
      "forecasting_cut": 240.0,
      "cut_margin": 10.0,
      "day_of_month_cut_max_increase": 10.0
    },
    "unitPrice": {
      "source": "forecast",
      "constant": 10.0,
      "forecast_base": 1.0,
      "forecast_multiplier": 10.0,
      "day_of_month_max_increase": 3.0
    },
    "mainSettings": {
      "side": "Buy",
      "regulationType": "Up",
      "priceType": "Limit",
      "currency": "CHF",
      "fillType": "Normal"
    }
  },
  "contractSection": { ... }
}
```

Key elements:

- **`flexibilitySource`**: where the DSO takes its demand signal from (e.g. `"forecast"`).
- **`forecast`**: configuration of forecast data used to derive expected load and flexibility needs.
- **`peak`**: reference peak load or limits.
- **`orderSection.nodeName`**: grid node at which the flexibility is requested.
- **`orderSection.quantities`**: how requested flexibility quantities are derived:
  - `random`: list of discrete MW values that can be randomly picked.
  - `db`: how to query measurements from a DB (`fields`, `community`, `device`, `daysToGoBack`).
  - `forecasting_cut`, `cut_margin`, `day_of_month_cut_max_increase`: parameters controlling how much flexibility is requested depending on forecast and date.
- **`orderSection.unitPrice`**: how the bid price is set, e.g. from a forecast-dependent pricing rule.
- **`orderSection.mainSettings`**: static order attributes (`side`, `regulationType`, `priceType`, etc.).

These parameters are consumed inside `DSO.demand_flexibility(...)` to build a structure like:

```python
{
    "quantity": <MW>,
    "regulationType": "Up",  # etc.
    "nodeName": "Massagno_1",
    ...
}
```

---

## Execution steps in detail

1. **Argument parsing** with `argparse`.
2. **Configuration loading**:
   - Load `config_file` JSON.
   - Load `cfg["connectionsFile"]` and merge into `cfg`.
3. **Logging setup** using `logging.basicConfig`.
4. **Database connection** (optional):
   - Attempts to instantiate `PostgreSQLInterface(cfg["postgreSQL"], logger)`.
   - On failure, logs an error and continues with `pgi = None`.
5. **DSO creation and setup**:
   - `dso = DSO(cfg["fm"]["actors"]["dso"], cfg, logger)`.
   - `user_info = dso.nodes_interface.get_user_info()`.
   - `dso.set_markets(filter_dict={"name": cfg["fm"]["marketName"]})`.
   - `dso.set_organization(filter_dict={"name": dso.cfg["id"]})`.
   - `dso.set_grid_area(filter_dict={"name": cfg["fm"]["gridAreaName"]})`.
   - `dso.set_grid_nodes(filter_dict={"gridAreaId": dso.grid_area["id"]})`.
6. **Timeslot computation**:
   - `slot_time = dso.get_adjusted_time(cfg["fm"]["granularity"], cfg["fm"]["ordersTimeShift"])`.
   - This typically snaps current time to the market 15-min granularity and shifts it forward by `ordersTimeShift` minutes.
7. **Informative prints**:
   - `dso.print_user_info(user_info)` and `dso.print_player_info()` log Nodes-related meta-data.
8. **FMO initialization**:
   - `fmo = FMO(dso.cfg, logger, pgi)`.
9. **Demand flexibility and place order**:
   - `resp_demand = dso.demand_flexibility(slot_time)`.
   - If `resp_demand` is not `False`, the script logs the requested flexibility and calls:

     ```python
     fmo.add_entry_to_market_ledger(
         timeslot=slot_time,
         player=dso,
         portfolio=None,
         features=resp_demand,
     )
     ```

   - This adds the DSO order to the market ledger for the computed timeslot.

---

## How `test_fm01_aem.json` drives trader_dso

- The **time-related** parameters `granularity` and `ordersTimeShift` set the temporal resolution and look-ahead of the order.
- The **DSO actor block** defines the forecasting and peak data sources, along with order construction logic, via `orderSection`.
- The **connection settings** in `connectionsFile` give URLs/credentials for Nodes and PostgreSQL.

By changing these values, you control how much flexibility the DSO requests, at which node, at what price, and for which future interval.

---

## Typical usage pattern

1. Keep baseline and forecast data updated (e.g. using `baseline_updater.py` and external forecasting tools).
2. Run `trader_dso.py` periodically (e.g. every 15 minutes) so that the DSO continuously requests flexibility for upcoming timeslots.
3. FSP agents (via `trader_fsp.py`) will respond with Sell offers based on their baselines.

---

## Dependencies and environment

- Requires `classes.dso.DSO`, `classes.fmo.FMO`, and `classes.postgresql_interface.PostgreSQLInterface`.
- Requires a configuration file similar to `conf/test_fm01_aem.json`.
- Requires connectivity to the Nodes platform and optionally to PostgreSQL.
