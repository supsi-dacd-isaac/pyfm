# simulate_strategy_bidding.py

Historical **read-only** replay tool for comparing bidding strategies over past
measurements. It answers:

> *If strategy_8 / strategy_9 / strategy_10 had been running over past days,
> what bids and flexibility estimates would they have generated?*

This is **not** a market simulator. It does not model DSO clearing, activations,
or revenue. It replays the **same production flexibility and allocation logic**
used by `trader_fsp.py` against historical InfluxDB data.

---

## What it does

- Iterates through historical bidding timestamps aligned to `fm.granularity`
- Reconstructs the information available at each replay time `T`
- Calls production components:
  - `FlexibilityForecaster` (persistence, recent_profile, historical)
  - `BiddingStrategy` / `StrategyManager`
  - `get_achievable_flexibility` (discretization-aware, no-overdelivery for gated methods)
  - `resolve_strategy_flexibility_method` (from `trader_fsp.py`)
- Writes CSV summaries and optional PNG plots

## What it does **not** do

- Publish bids or NODES orders
- Send RabbitMQ commands
- Write to PostgreSQL
- Trigger activations or contract creation
- Modify any production state

Safe to run while production is active.

---

## Quick start

From the pyfm repository root:

```bash
# Activate virtualenv if available
source venv/bin/activate   # or .venv/bin/activate

# Compare three strategies over one week
python scripts/simulate_strategy_bidding.py \
    --config_file conf/test_fm01_aem.json \
    --fsp supsi01 \
    --strategies strategy_8 strategy_9 strategy_10 \
    --start 2026-05-01T00:00:00Z \
    --end   2026-05-07T00:00:00Z

# Single-day replay for strategy_10 with custom output directory
python scripts/simulate_strategy_bidding.py \
    --config_file conf/test_fm01_aem.json \
    --fsp supsi01 \
    --strategies strategy_10 \
    --start 2026-05-20T06:00:00Z \
    --end   2026-05-20T18:00:00Z \
    --output_dir outputs/replay_s10_may20

# Same replay with PNG plots
python scripts/simulate_strategy_bidding.py \
    --config_file conf/test_fm01_aem.json \
    --fsp supsi01 \
    --strategies strategy_8 strategy_9 strategy_10 \
    --start 2026-05-01T00:00:00Z \
    --end   2026-05-07T00:00:00Z \
    --output_dir outputs/replay_with_plots \
    --plots
```

---

## Prerequisites

| Requirement | Notes |
|-------------|-------|
| Python 3 | Same environment as other pyfm scripts |
| `pandas` | Used for CSV handling and plot data loading |
| `influxdb` | Required — historical measurements are read from InfluxDB |
| `matplotlib` | Optional — only needed when `--plots` is passed |
| Config file | e.g. `conf/test_fm01_aem.json` |
| Connections file | Referenced by `connectionsFile` in config (InfluxDB credentials) |

Install optional plot dependency:

```bash
pip install matplotlib
```

---

## Command-line arguments

| Argument | Required | Default | Description |
|----------|----------|---------|-------------|
| `--config_file` | Yes | — | Path to FM configuration JSON |
| `--fsp` | Yes | — | FSP identifier (e.g. `supsi01`) |
| `--strategies` | Yes | — | One or more strategy IDs (space-separated) |
| `--start` | Yes | — | Replay window start (UTC ISO, e.g. `2026-05-01T00:00:00Z`) |
| `--end` | Yes | — | Replay window end (UTC ISO) |
| `--output_dir` | No | `outputs/replay_<timestamp>` | Directory for CSV/JSON/PNG outputs |
| `--log_file` | No | stdout | Log file path |
| `--quiet` | No | off | Reduce per-timestamp logging |
| `--plots` | No | off | Generate PNG plots from CSV outputs |

### Timestamp formats

Accepted for `--start` and `--end`:

- `2026-05-01T00:00:00Z`
- `2026-05-01T06:30`
- `2026-05-01`

Timestamps are interpreted as UTC. Replay steps are aligned to `fm.granularity`
(default 15 minutes).

---

## How replay works

```text
For each strategy in --strategies:
  Resolve flexibility_method (historical / persistence / recent_profile)
  Create FlexibilityForecaster with strategy config

  For each replay_timestamp T in [--start, --end):
    delivery_slot = T + fm.ordersTimeShift

    1. strategy.get_bid_parameters(delivery_slot)
    2. flex_forecaster.get_asset_flexibility_breakdown(
         period_from=delivery_slot,
         current_time_utc=T          ← historical visibility
       )
    3. flex_forecaster.get_achievable_flexibility(
         period_from=delivery_slot,
         target_kw=strategy_target,
         allowed_assets=strategy.allowed_assets,
         current_time_utc=T
       )
    4. Collect per-asset and portfolio-level results

Write replay_asset_detail.csv
Write replay_portfolio_summary.csv
Write replay_metadata.json
Optionally generate PNG plots (--plots)
```

### Historical visibility

At replay time `T`, only measurements with `time <= T` are visible:

- InfluxDB queries use `time < T` as the upper bound
- Recent-profile lookback window: `[T - lookbackMinutes, T]`
- Persistence baseline source: `delivery_slot - go_back_minutes`
- Current-power activity gate uses the latest measurement at or before `T`

This matches what the production trader would have known when it ran at `T`.

### Time semantics

| Field | Meaning |
|-------|---------|
| `replay_timestamp_utc` | When the trader would have executed |
| `delivery_slot_utc` | Target delivery slot (`replay_time + ordersTimeShift`) |

With default config (`ordersTimeShift = 90`, `granularity = 15`):

```text
replay at 08:00 UTC  →  delivery slot 09:30 UTC
```

---

## Supported strategies

Any strategy defined in `bidding_strategies` in the config file works
automatically. Common comparison targets:

| Strategy | Method | Assets | Description |
|----------|--------|--------|-------------|
| **strategy_8** | `persistence` | HP: ECM96.2, ECM97.3 | Lagged baseline + active gate |
| **strategy_9** | `persistence` | HP + EV: ECM63.1, ECM63.2, ECM96.2, ECM97.3 | Persistence with EV inclusion |
| **strategy_10** | `recent_profile` | EV: ECM63.1, ECM63.2 | q25 of recent 2h window × continuous factor |

Strategy_1 through strategy_7 use the **historical** (time-pattern) method unless
`flexibility_method` is set explicitly in config.

See also: [BIDDING_STRATEGIES.md](./BIDDING_STRATEGIES.md), [README_trader_fsp.md](./README_trader_fsp.md).

---

## Output files

All outputs are written to `--output_dir` (created automatically).

### CSV: `replay_asset_detail.csv`

One row per (timestamp × strategy × asset), plus a `_portfolio` summary row.

| Column | Description |
|--------|-------------|
| `replay_timestamp_utc` | When the trader would have run |
| `delivery_slot_utc` | Target delivery slot |
| `strategy` | Strategy ID |
| `asset_id` | Asset identifier (`_portfolio` for portfolio row) |
| `modulation_type` | `discrete`, `continuous`, or `portfolio` |
| `current_power_w` | Measured power at replay time (activity gate) |
| `expected_power_w` | Baseline or expected power (persistence / q25) |
| `available_flexibility_w` | Computed available flexibility |
| `bid_quantity_w` | Recommended bid quantity (portfolio row only) |
| `active_threshold_w` | Threshold for active/inactive gate |
| `is_currently_active` | Whether asset was active at replay time |
| `estimation_method` | `persistence`, `recent_profile`, `historical`, etc. |
| `skip_reason` | Why asset was excluded (empty if included) |

Common `skip_reason` values:

| Value | Meaning |
|-------|---------|
| *(empty)* | Asset included in flexibility calculation |
| `inactive` | Current power below active threshold |
| `not_in_strategy` | Asset not in strategy's `assets_filter` |
| `insufficient_samples` | Recent-profile: not enough lookback samples |
| `not_available` | Gated method: asset not available for flexibility |
| `no_bid_slot` | Strategy time slot has zero target flexibility |

### CSV: `replay_portfolio_summary.csv`

One row per (timestamp × strategy).

| Column | Description |
|--------|-------------|
| `replay_timestamp_utc` | When the trader would have run |
| `delivery_slot_utc` | Target delivery slot |
| `strategy` | Strategy ID |
| `total_bid_quantity_w` | Recommended portfolio bid (after allocation) |
| `total_available_flexibility_w` | Sum of available flexibility (non-skipped assets) |
| `number_active_assets` | Count of active assets |
| `number_skipped_assets` | Count of skipped assets |
| `estimation_method` | Flexibility method used |

### JSON: `replay_metadata.json`

Run parameters and provenance:

```json
{
  "config_file": "conf/test_fm01_aem.json",
  "fsp": "supsi01",
  "strategies": ["strategy_8", "strategy_9", "strategy_10"],
  "start": "2026-05-01T00:00:00Z",
  "end": "2026-05-07T00:00:00Z",
  "granularity_minutes": 15,
  "orders_time_shift_minutes": 90,
  "timestamps_replayed": 576,
  "plots_enabled": false,
  "plot_files": [],
  "read_only": true,
  "side_effects": "NONE"
}
```

---

## Optional plots (`--plots`)

When `--plots` is passed, five PNG files are generated from the CSV outputs.
Plotting runs **after** CSVs are written and does not change replay results.

| File | Content |
|------|---------|
| `portfolio_bid_timeseries.png` | Bid quantity vs available flexibility over time (kW) |
| `active_skipped_assets_timeseries.png` | Active vs skipped asset counts over time |
| `asset_flexibility_timeseries.png` | Per-asset available flexibility (kW) |
| `asset_current_vs_expected_power.png` | Per-asset current vs expected power (kW) |
| `skip_reason_counts.png` | Bar chart of skip reason frequencies |

Plot behaviour:

- Uses matplotlib (Agg backend, headless-safe)
- Reads CSV files with pandas — no re-computation
- Multi-strategy runs: portfolio plots use one subplot per strategy
- Many assets: current vs expected uses a 2-column subplot grid
- Missing or empty data: plot skipped with a log message; replay still succeeds
- Without `--plots`: matplotlib is not imported

If `--plots` is used but matplotlib is not installed, replay completes normally
and a clear error message is printed. No plots are generated.

---

## Analysis examples

### Compare average bid sizes across strategies

```python
import pandas as pd

summary = pd.read_csv("outputs/replay_20260501/replay_portfolio_summary.csv")
summary.groupby("strategy")["total_bid_quantity_w"].agg(["mean", "max", "count"])
```

### Find timestamps where strategy_10 bids but strategy_8 does not

```python
pivot = summary.pivot_table(
    index="replay_timestamp_utc",
    columns="strategy",
    values="total_bid_quantity_w",
)
diff = pivot["strategy_10"].fillna(0) - pivot["strategy_8"].fillna(0)
diff[diff > 0].head()
```

### Inspect inactive gating for EV assets

```python
detail = pd.read_csv("outputs/replay_20260501/replay_asset_detail.csv")
detail[
    (detail["strategy"] == "strategy_10")
    & (detail["skip_reason"] == "inactive")
][["replay_timestamp_utc", "asset_id", "current_power_w", "active_threshold_w"]]
```

### Compare persistence vs recent-profile estimation methods

```python
detail.groupby(["strategy", "estimation_method"])["available_flexibility_w"].mean()
```

---

## Comparison with related tools

| Tool | Purpose |
|------|---------|
| **`simulate_strategy_bidding.py`** | Historical replay of **actual** production bidding logic over InfluxDB data |
| **`trader_fsp.py`** | Live production bidding — places real orders on NODES |
| **`strategy_evaluator.py`** | Forward-looking **assumption-based** revenue/cost estimation (no InfluxDB replay) |
| **`baseline_forecast_evaluator.py`** | Baseline forecast accuracy analysis |

Use the replay tool when you need to know what production code **would have
produced** given real historical measurements. Use the strategy evaluator when
you need quick what-if revenue projections from assumptions.

---

## Architecture

The replay script is a thin **historical orchestrator**. It does not duplicate
strategy logic:

```text
simulate_strategy_bidding.py
  ├── StrategyManager / BiddingStrategy     (strategy resolution, asset filter)
  ├── FlexibilityForecaster                 (persistence / recent_profile / historical)
  ├── get_achievable_flexibility()          (discrete allocation, no-overdelivery)
  └── resolve_strategy_flexibility_method() (imported from trader_fsp.py)
```

Production code paths are unchanged. Future strategies added to config will
work in replay without script modifications.

---

## Testing

Smoke and plot tests live in `tests/test_simulate_strategy_bidding.py`.

Run from the repository root:

```bash
PYTHONPATH=. python -m pytest tests/test_simulate_strategy_bidding.py -v
```

Tests cover:

- Strategy replay execution (strategy_8, strategy_9, strategy_10 paths)
- CSV schema and generation
- No write-side effects (no NODES, RabbitMQ, PostgreSQL)
- Historical-only visibility (queries bounded by replay time)
- Plot PNG generation from synthetic CSV data
- Plotting does not modify CSV output
- Graceful handling of missing matplotlib and empty data

---

## Troubleshooting

### `ERROR: influxdb package is not installed`

```bash
pip install influxdb
```

### `ERROR: Cannot load connections file`

Ensure `connectionsFile` in your config points to a valid JSON file with
InfluxDB credentials (typically `conf/private/conns.json`).

### `ERROR: No timestamps to replay`

Check that `--start` is before `--end` and that the window spans at least one
granularity step (default 15 minutes).

### Many assets skipped with `inactive`

Expected when historical measurements show assets below `activeThresholdW`
(default 500 W) at the replay time. Compare `current_power_w` vs
`active_threshold_w` in the asset detail CSV.

### Many assets skipped with `insufficient_samples` (strategy_10)

Recent-profile requires at least `minSamples` (default 2) measurements in the
lookback window. Early replay timestamps or sparse InfluxDB data may trigger this.

### `--plots` produces no PNG files

- Confirm matplotlib is installed: `pip install matplotlib`
- Check logs for per-plot skip warnings (empty data, missing columns)
- Verify CSV files exist in `--output_dir` before plotting runs

### Replay is slow over long windows

Each timestamp × strategy combination queries InfluxDB independently. For long
windows, start with a shorter `--start`/`--end` range or use `--quiet` to reduce
logging overhead.

---

## Related documentation

- [README_trader_fsp.md](./README_trader_fsp.md) — production bidding agent
- [BIDDING_STRATEGIES.md](./BIDDING_STRATEGIES.md) — strategy definitions
- [FLEXIBILITY_ANALYSIS.md](./FLEXIBILITY_ANALYSIS.md) — flexibility forecaster overview
- [README_flexi_manager.md](./README_flexi_manager.md) — activation (downstream of bidding)
