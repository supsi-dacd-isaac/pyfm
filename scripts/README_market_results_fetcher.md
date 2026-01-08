# Market Results Fetcher

A Python script to periodically fetch results from closed flexibility markets on the NODES platform and export them to JSON files.

## Overview

The `market_results_fetcher.py` script connects to the NODES Market API and retrieves:
- **Closed Orders**: Orders that have been completed (Filled, PartiallyFilled, Cancelled, or Expired)
- **Trades**: Executed trades within the specified period
- **Settlements**: Settlement data (when available)
- **Time Slot Analysis**: Data grouped by 15-minute market intervals

## Requirements

- Python 3.8+
- Valid NODES API credentials configured in `conf/private/conns.json`
- Access to the NODES Market test environment

## Installation

The script uses the existing project dependencies. Ensure you have activated the virtual environment:

```bash
cd /path/to/pyfm
source venv/bin/activate
export PYTHONPATH="${PYTHONPATH}:$(pwd)"
```

## Usage

### Basic Syntax

```bash
python scripts/market_results_fetcher.py --config_file <config> --player <dso|fsp> [options]
```

### Required Arguments

| Argument | Description |
|----------|-------------|
| `--config_file` | Path to the main configuration file (e.g., `conf/test_fm01_aem.json`) |
| `--player` | Player type for authentication: `dso` or `fsp` |

### Optional Arguments

| Argument | Default | Description |
|----------|---------|-------------|
| `--period_mode` | `dynamic` | Period selection mode: `dynamic` (last N hours) or `static` (fixed range) |
| `--hours_back` | `2.0` | Hours to look back (for dynamic mode) |
| `--period_from` | - | Start of period in ISO format (for static mode) |
| `--period_to` | - | End of period in ISO format (for static mode) |
| `--fsp_id` | - | FSP identifier (required when `--player fsp`) |
| `--market_name` | From config | Filter results by market name |
| `--include_settlements` | `false` | Include settlements data (disabled by default) |
| `--include_assets` | `false` | Include asset details for each portfolio (FSP only) |
| `--interval` | `0` | Fetch interval in seconds (0 = single run) |
| `--output_dir` | `data/market_results/` | Directory for output files |
| `--output_file` | Auto-generated | Override output filename (without extension) |
| `--log_file` | - | Log file path (logs to stdout if not specified) |

## Examples

### Example 1: Single Fetch - Last 2 Hours

Fetch market results from the last 2 hours and save to the default output file:

```bash
python scripts/market_results_fetcher.py \
    --config_file conf/test_fm01_aem.json \
    --player dso \
    --hours_back 2
```

### Example 2: Single Fetch - Last 4 Hours with Custom Output

```bash
python scripts/market_results_fetcher.py \
    --config_file conf/test_fm01_aem.json \
    --player dso \
    --hours_back 4 \
    --output_file data/market_results_4h.json
```

### Example 3: Static Period

Fetch results for a specific time range:

```bash
python scripts/market_results_fetcher.py \
    --config_file conf/test_fm01_aem.json \
    --player dso \
    --period_mode static \
    --period_from "2025-12-09T08:00:00Z" \
    --period_to "2025-12-09T12:00:00Z"
```

### Example 4: Periodic Fetching (Every 15 Minutes)

Run the script continuously, fetching every 15 minutes (900 seconds):

```bash
python scripts/market_results_fetcher.py \
    --config_file conf/test_fm01_aem.json \
    --player dso \
    --hours_back 2 \
    --interval 900
```

When running periodically, output files are timestamped automatically:
- `data/market_results_20251209_120000.json`
- `data/market_results_20251209_121500.json`
- etc.

### Example 5: Using FSP Credentials

```bash
python scripts/market_results_fetcher.py \
    --config_file conf/test_fm01_aem.json \
    --player fsp \
    --fsp_id supsi01 \
    --hours_back 2
```

### Example 6: With Logging to File

```bash
python scripts/market_results_fetcher.py \
    --config_file conf/test_fm01_aem.json \
    --player dso \
    --hours_back 2 \
    --log_file logs/market_fetcher.log
```

### Example 7: Background Execution (Linux/macOS)

Run as a background process:

```bash
nohup python scripts/market_results_fetcher.py \
    --config_file conf/test_fm01_aem.json \
    --player dso \
    --hours_back 2 \
    --interval 900 \
    --log_file logs/market_fetcher.log \
    > /dev/null 2>&1 &
```

### Example 8: Filter by Specific Market

```bash
python scripts/market_results_fetcher.py \
    --config_file conf/test_fm01_aem.json \
    --player dso \
    --hours_back 2 \
    --market_name "Opentunity-CH"
```

### Example 9: Include Settlements Data

By default, settlements are not fetched (the endpoint may not be available in all environments). To include settlements:

```bash
python scripts/market_results_fetcher.py \
    --config_file conf/test_fm01_aem.json \
    --player dso \
    --hours_back 2 \
    --include_settlements
```

### Example 10: Include Portfolio Assets (FSP only)

For FSP players, include the list of assets assigned to each portfolio:

```bash
python scripts/market_results_fetcher.py \
    --config_file conf/test_fm01_aem.json \
    --player fsp \
    --fsp_id supsi01 \
    --hours_back 2 \
    --include_assets
```

This adds asset details (id, name, MPID, type) to each portfolio in the output.

## Output Files

The script generates two files for each run:

1. **JSON file** - Complete machine-readable data
2. **Markdown file** - Human-readable report

### Filename Format

Files are automatically named with descriptive information:

```
market_results_{role}_{actor_id}_{period}_{timestamp}.{ext}
```

**Examples:**
- `market_results_dso_aem_last2h_created_at_20251209T142919Z.json`
- `market_results_dso_aem_last2h_created_at_20251209T142919Z.md`
- `market_results_fsp_supsi01_last4h_created_at_20251209T143000Z.json`
- `market_results_fsp_supsi01_last4h_created_at_20251209T143000Z.md`

**Components:**
| Part | Description |
|------|-------------|
| `role` | Actor role: `dso` or `fsp` |
| `actor_id` | Configured actor ID (e.g., `aem`, `supsi01`) |
| `period` | Time period: `last2h`, `last4h`, or `static` |
| `timestamp` | ISO8601 creation timestamp |
| `ext` | File extension: `json` or `md` |

## JSON Output Format

The script generates a JSON file with the following structure:

```json
{
  "metadata": {
    "fetched_at": "2025-12-09T13:23:56Z",
    "period_from": "2025-12-09T11:23:55Z",
    "period_to": "2025-12-09T13:23:55Z",
    "actor_id": "SUPSI",
    "actor_role": "fsp",
    "market_name": "Opentunity-CH",
    "market_id": "40034bfd-5ccb-4f7f-a9a0-ea1a79cdb9b4",
    "organization": "SUPSI",
    "organization_id": "f3f32d1a-6e80-4f81-ad5e-884d2d9fc923",
    "granularity_minutes": 15,
    "total_time_slots": 9,
    "total_closed_orders": 5,
    "total_trades": 5,
    "total_settlements": 0,
    "portfolios": [
      {
        "id": "068aa3f3-f0cb-4dd1-9acc-b30d0081e8bf",
        "name": "ECM_REAL_ASSETS",
        "description": null,
        "assets": [
          {
            "id": "e91f4b10-...",
            "name": "ECM97.4",
            "mpid": "ECM97",
            "assetType": "None"
          }
        ]
      },
      {
        "id": "566ae6bd-60c4-4212-9ff7-048d6164bbfe",
        "name": "ECM_REAL_ASSETS_OLD",
        "description": null,
        "assets": []
      }
    ],
    "period_mode": "dynamic",
    "hours_back": 2.0
  },
  "time_slots": {
    "2025-12-09T10:30:00Z": {
      "period_from": "2025-12-09T10:30:00Z",
      "period_to": "2025-12-09T10:45:00Z",
      "buy_orders": [],
      "sell_orders": [],
      "total_buy_quantity": 0.0,
      "total_sell_quantity": 0.0,
      "matched_quantity": 0.0,
      "avg_price": 0.0,
      "status": "no_activity"
    },
    "2025-12-09T10:45:00Z": {
      "period_from": "2025-12-09T10:45:00Z",
      "period_to": "2025-12-09T11:00:00Z",
      "buy_orders": [...],
      "sell_orders": [...],
      "total_buy_quantity": 0.015,
      "total_sell_quantity": 0.008,
      "matched_quantity": 0.008,
      "avg_price": 6.07,
      "status": "cleared"
    }
  },
  "closed_orders": [
    {
      "id": "f1adec65-3f4f-42af-93b1-b3ae00b6b26c",
      "completionType": "Filled",
      "side": "Sell",
      "regulationType": "Up",
      "quantity": 0.0,
      "unitPrice": 6.31,
      "periodFrom": "2025-12-09T12:30:00+00:00",
      "periodTo": "2025-12-09T12:45:00+00:00",
      "status": "Completed",
      "buyer": null,
      "seller": {
        "organizationId": "f3f32d1a-6e80-4f81-ad5e-884d2d9fc923",
        "organizationName": "SUPSI"
      },
      "portfolio": {
        "id": "068aa3f3-f0cb-4dd1-9acc-b30d0081e8bf",
        "name": "ECM_REAL_ASSETS"
      }
    }
  ],
  "trades": [
    {
      "id": "d25f5525-cb89-4827-a672-a48e4ed417d0",
      "orderId": "f1adec65-3f4f-42af-93b1-b3ae00b6b26c",
      "quantity": 0.007,
      "unitPrice": 6.31,
      "side": "Sell",
      "regulationType": "Up",
      "periodFrom": "2025-12-09T12:30:00+00:00",
      "periodTo": "2025-12-09T12:45:00+00:00",
      "assetPortfolioId": "068aa3f3-f0cb-4dd1-9acc-b30d0081e8bf",
      "portfolioName": "ECM_REAL_ASSETS"
    }
  ],
  "settlements": [],
  "summary": {
    "total_buy_quantity_mw": 0.059,
    "total_sell_quantity_mw": 0.0,
    "total_matched_quantity_mw": 0.0,
    "filled_orders": 3,
    "partially_filled_orders": 0,
    "cancelled_orders": 0,
    "expired_orders": 4,
    "total_trades": 7
  }
}
```

### Output Fields Explained

#### Metadata
| Field | Description |
|-------|-------------|
| `fetched_at` | Timestamp when the data was fetched |
| `period_from` / `period_to` | Time range of the query |
| `actor_id` | Configured actor identifier (e.g., "AEM", "SUPSI") |
| `actor_role` | Actor role: "dso" or "fsp" |
| `market_name` / `market_id` | Market identification |
| `organization` / `organization_id` | Organization name and NODES platform ID |
| `portfolios` | List of portfolios (FSP only) with id, name, description, assets (if `--include_assets`) |
| `granularity_minutes` | Market time slot duration (15 min) |
| `total_*` | Counts of retrieved data |

#### Closed Orders (enriched fields)
Each order includes participant information:

| Field | Description |
|-------|-------------|
| `buyer` | Buyer info (for Buy orders): `organizationId`, `organizationName` |
| `seller` | Seller info (for Sell orders): `organizationId`, `organizationName` |
| `portfolio` | Portfolio info (for Sell orders): `id`, `name` |

**Note:** 
- For **Buy orders** (DSO): `buyer` is populated, `seller` and `portfolio` are null
- For **Sell orders** (FSP): `seller` and `portfolio` are populated, `buyer` is null

#### Trades (FSP-specific fields)
| Field | Description |
|-------|-------------|
| `assetPortfolioId` | Portfolio ID for the trade |
| `portfolioName` | Portfolio name (enriched by the script for FSP) |

#### Time Slots
Each 15-minute slot contains:
| Field | Description |
|-------|-------------|
| `buy_orders` / `sell_orders` | Lists of orders in this slot |
| `total_buy_quantity` / `total_sell_quantity` | Aggregated quantities (MW) |
| `matched_quantity` | Quantity that was successfully matched |
| `avg_price` | Average price of matched orders |
| `status` | `no_activity`, `no_match`, or `cleared` |

#### Summary Statistics
| Field | Description |
|-------|-------------|
| `total_buy_quantity_mw` | Total buy quantity from **your organization's** orders (MW) |
| `total_sell_quantity_mw` | Total sell quantity from **your organization's** orders (MW) |
| `total_matched_quantity_mw` | Successfully matched/executed quantity (MW) |
| `filled_orders` | Orders completely filled |
| `partially_filled_orders` | Orders partially filled |
| `cancelled_orders` | Cancelled orders |
| `expired_orders` | Orders that expired |
| `total_trades` | Number of executed trades |

> **Note:** The `total_buy_quantity_mw` and `total_sell_quantity_mw` reflect only your organization's orders:
> - **FSP** will see mostly sell quantities (FSPs sell flexibility)
> - **DSO** will see mostly buy quantities (DSOs buy flexibility)
> 
> To see the complete market picture, run the script for both DSO and FSP.

## Time Slot Status Values

| Status | Description |
|--------|-------------|
| `no_activity` | No orders in this time slot |
| `no_match` | Orders exist but none matched |
| `cleared` | At least one order was matched |

## Order Completion Types

| Type | Description |
|------|-------------|
| `Filled` | Order was completely filled |
| `PartiallyFilled` | Order was partially filled |
| `Cancelled` | Order was cancelled |
| `Expired` | Order expired without matching |

## Markdown Report

The markdown report (`.md` file) provides a human-readable summary including:

- **Metadata** - Actor, organization, market, and period information
- **Portfolios** - List of FSP portfolios (FSP only)
- **Summary** - Aggregated statistics (filled, expired, cancelled orders)
- **Closed Orders** - Table with period, side, quantity, price, status, buyer, seller, portfolio
- **Trades** - Table of executed trades with portfolio names

Example output:

```markdown
# Market Results Report

**Generated:** 2025-12-09T14:28:30Z

## Summary

| Metric | Value |
|--------|-------|
| Total Closed Orders | 5 |
| Filled Orders | 5 |
| Expired Orders | 0 |

## Closed Orders

| Period | Side | Quantity | Price | Status | Buyer | Seller | Portfolio |
|--------|------|----------|-------|--------|-------|--------|-----------|
| 2025-12-09T12:30:00Z | Sell | 0.007 MW | 6.31 | Filled | - | SUPSI | ECM_REAL_ASSETS |
```

## Troubleshooting

### Token Expired
If you see authentication errors (401), the script automatically requests a new token. If issues persist, delete the token file in `tkns/` folder.

### Settlements Not Available
The settlements endpoint may return 404 if no settlement data exists for the queried period. This is handled gracefully - the `settlements` array will be empty.

### Connection Timeouts
The script uses configurable retries (default: 5 attempts). Increase timeout in `conf/private/conns.json` if needed:
```json
{
  "nodesAPI": {
    "requestTimeout": 10,
    "retries": 5
  }
}
```

## Integration Examples

### Cron Job (Linux)

Add to crontab for hourly execution:
```bash
0 * * * * cd /path/to/pyfm && source venv/bin/activate && python scripts/market_results_fetcher.py --config_file conf/test_fm01_aem.json --player dso --hours_back 1 --output_file data/hourly/market_$(date +\%Y\%m\%d_\%H).json
```

### Systemd Service

Create `/etc/systemd/system/market-fetcher.service`:
```ini
[Unit]
Description=Market Results Fetcher
After=network.target

[Service]
Type=simple
User=your_user
WorkingDirectory=/path/to/pyfm
Environment=PYTHONPATH=/path/to/pyfm
ExecStart=/path/to/pyfm/venv/bin/python scripts/market_results_fetcher.py --config_file conf/test_fm01_aem.json --player dso --hours_back 2 --interval 900
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl enable market-fetcher
sudo systemctl start market-fetcher
```

## Related Scripts

- `trader_dso.py` - DSO trading operations
- `trader_fsp.py` - FSP trading operations
- `orders_reader.py` - Read and display orders
- `baseline_updater.py` - Update portfolio baselines

## Author

SUPSI - Institute for Systems and Applied Electronics

## License

See project LICENSE file.

