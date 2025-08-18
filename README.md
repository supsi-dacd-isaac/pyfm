# pyfm

A Python-based framework for simulating and managing energy market operations. The repository is organized for modularity and extensibility, supporting various market roles and interfaces.

## Table of Contents
- [Directory Structure](#directory-structure)
- [Configuration](#configuration)
- [Docker Usage](#docker-usage)
- [Getting Started](#getting-started)
- [Testing](#testing)
- [License](#license)


## Directory Structure
```
classes/           # Core market participant classes and interfaces
conf/              # Configuration files (JSON)
scripts/           # Main scripts for trading, contracts, and utilities
data/              # Baseline, forecast, and peak data
unittest/          # Unit tests
```

## Configuration
### `conf/test_fm01_aem.json`
This file contains the configuration for a market simulation scenario. Key sections typically include:
- **Market Parameters**: Market type, time intervals, pricing rules
- **Participants**: List and settings for DSOs, FSPs, buyers, etc.
- **Assets**: Asset definitions, capacities, and constraints
- **Data Sources**: Paths to baseline, forecast, and peak data files
- **Connection Settings**: Database and API endpoints

Fields description:
- `flexibilitySource`: Defines the source of flexibility data (e.g., forecast, db, random)
- `forecasting_cut`: Specifies the limit that the load should not exceed, if exceeded flexibility will be requested. If there is a monthly peak higher than this value, that value will be used as the cut.
- `cut_margin`: Margin for the cut (e.g. if `forecasting_cut` is 100, `cut_margine` is 10, and the `forecasting value` is 110, the flexibility request will be for 20).
- `day_of_month_cut_max_increase`: Extra margin for the cut, scales linearly with the day of the month from 0 to the value specified. 
- for unitPrice and pricing:
  - `constant`: Constant price used when source is `constant`.
  - `pricing`: Specifies the pricing model to be used in the simulation.
  - `forecast_base`: Base price for forecasting, used when source is `forecast`.
  - `forecast_multiplier`: This value is multiplied by the forecasting (scaled in range [0;1]) and summed to `forecast_base`, used when source is `forecast`.
  - `day_of_month_max_increase`: Extra value added to the price, scales linearly with the day of the month from 0 to the value specified.
  - `activationCost`: Same as `constant`, but used on the fsp side to accept or reject flexibility requests.


## Docker Usage
Dockerfiles are provided for key components:
- `docker/Dockerfile.baseline_updater`: Baseline data updater
- `docker/Dockerfile.trader_dso`: Trader for DSO
- `docker/Dockerfile.trader_fsp`: Trader for FSP

### Compose
A `docker-compose.yaml` is provided for orchestrating multiple services. Usage:
```cmd
> docker-compose build
> docker-compose up
```

For specific services, you can run:
```cmd
> docker-compose up trader_dso
> docker-compose up trader_fsp
> docker-compose up baseline_updater
```

## Getting Started
1. Clone the repository
2. Install dependencies:
   ```cmd
   > pip install -r requirements.txt
   ```
3. Configure your scenario in `conf/test_fm01_aem.json`
4. Run scripts from the `scripts/` directory as needed
```cmd
> python baseline_updater.py --config_file ../conf/test_fm01_aem.json --fsp supsi01
> python trader_dso.py --config_file ../conf/test_fm01_aem.json
> python trader_fsp.py --config_file ../conf/test_fm01_aem.json --fsp supsi01
```

