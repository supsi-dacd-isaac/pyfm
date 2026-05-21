# Flexibility Command Forwarding System

This document describes the architecture and configuration of the flexibility command forwarding system, which decouples command generation from command actuation using RabbitMQ as a message broker.

## Table of Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Components](#components)
4. [Installation](#installation)
5. [Configuration](#configuration)
6. [Usage](#usage)
7. [Message Format](#message-format)
8. [Troubleshooting](#troubleshooting)

---

## Overview

The flexibility command forwarding system separates the **decision logic** (what flexibility to activate) from the **actuation layer** (how to send commands to devices). This separation provides several benefits:

- **Scalability**: Multiple forwarders can consume from the same queue
- **Reliability**: RabbitMQ provides message persistence and acknowledgments
- **Testability**: Dry-run mode allows testing without affecting real devices
- **Protocol Translation**: The forwarder translates manager commands into configured HTTP target requests
- **Decoupling**: flexi_manager and forwarder can be deployed and scaled independently

### Data Flow

```
Real asset command path:
flexi_manager.py / flexi_actuator.py
  -> rabbitMQ.realAssetCommands
  -> forwarder.py
  -> real API / AEM API

Simulated asset path:
flexi_manager.py / flexi_actuator.py
  -> rabbitMQ.simulatedAssetCommands
  -> external simulator application
  -> rabbitMQ.simulatedAssetMeasures
  -> forwarder.py
  -> downstream API / measurement target
```

`pyfm` does not implement the simulated asset application. `pyfm` only
publishes simulated asset commands and can consume simulated measures produced
by an external simulator.

---

## Architecture

### System Components

| Component | Role | Script/Service |
|-----------|------|----------------|
| **flexi_manager** | Calculates flexibility allocation and publishes commands | `scripts/flexi_manager.py` |
| **flexi_actuator** | Publishes manual asset commands | `scripts/flexi_actuator.py` |
| **RabbitMQ** | Message broker for reliable command delivery | Docker container |
| **external simulator** | Consumes simulated commands and publishes simulated measurements | Outside this repository |
| **forwarder** | Consumes real commands and simulated measurements from selected sections | `scripts/forwarder.py` |

### RabbitMQ Topology

Each `rabbitMQ` section in `conns.json` provides its own exchange, queue, and
routing key.

| Section | Exchange | Queue | Routing key | Producer | Consumer | Purpose |
|---------|----------|-------|-------------|----------|----------|---------|
| `rabbitMQ.realAssetCommands` | `flexi_commands` | `flexi_commands_queue` | `real_asset.command` | `flexi_manager.py` / `flexi_actuator.py` | `forwarder.py` | Real physical asset commands forwarded to the real API / AEM API |
| `rabbitMQ.simulatedAssetCommands` | `flexi_sim_commands` | `flexi_sim_commands_queue` | `sim_asset.command` | `flexi_manager.py` / `flexi_actuator.py` | External simulator application | Commands for simulated assets |
| `rabbitMQ.simulatedAssetMeasures` | `flexi_sim_measures` | `flexi_sim_measures_queue` | `sim_asset.measure` | External simulator application | `forwarder.py` | Simulated measurements/results forwarded downstream |

The standard forwarder deployment consumes:

```bash
FORWARDER_RABBIT_SECTIONS=realAssetCommands,simulatedAssetMeasures
```

Do not include `simulatedAssetCommands` in the standard forwarder deployment.
That section is consumed by the external simulator.

### Asset Command Destination Selection

`flexi_manager.py` and `flexi_actuator.py` select the command destination from
each asset's `asset_mapping.<asset_id>.rabbitCommandSection`.

```json
{
  "asset_mapping": {
    "ECM96.2": {
      "type": "heat_pump",
      "rabbitCommandSection": "realAssetCommands"
    },
    "ECM68.3": {
      "type": "heat_pump",
      "rabbitCommandSection": "simulatedAssetCommands"
    }
  }
}
```

Assets without `rabbitCommandSection` are skipped by the command publishers.
`rabbitCommandSection` controls command publishing only. Simulated measurements
are produced by the external simulator, not by `flexi_manager.py`.

### Message Types

| Type | Description | Routing Key Pattern |
|------|-------------|---------------------|
| `command` | Control commands (curtail, restore) | Configured section `routingKey` |
| `measurement` | Measurement data | Configured section `routingKey` |
| `batch_start` | Batch header with slot info | Same command section as the batch commands |

### External Simulator Contract

The simulator is external to `pyfm`. It must:

- Consume from `rabbitMQ.simulatedAssetCommands.exchange`, `.queue`, and
  `.routingKey`.
- Parse command envelopes with `message_type="command"`, `asset_id`,
  `asset_type`, `command_type`, `payload`, `timestamp`, and `priority`.
- Publish measurement envelopes to `rabbitMQ.simulatedAssetMeasures.exchange`,
  `.queue`, and `.routingKey`.
- Use measurement messages with `message_type="measurement"`, `asset_id`,
  `asset_type`, `timestamp`, and `payload`. `measurement_type` is optional but
  useful for logs and downstream routing.

---

## Components

### 1. flexi_manager.py

The flexibility manager is responsible for:
- Querying market results and bid records
- Calculating flexibility allocation across assets
- Generating control commands
- Publishing commands to RabbitMQ (when `--rabbitmq` flag is used)

**Key Classes:**
- `RabbitMQPublisher`: Handles connection and message publishing
- `AssetController`: Generates commands and queues them for publishing
- `FlexibilityManager`: Orchestrates the entire process

### 2. RabbitMQ

The message broker provides:
- Durable queues for message persistence
- Topic exchange for flexible routing
- Management UI for monitoring
- Message acknowledgments for reliability

### 3. forwarder.py

The forwarder is responsible for:
- Consuming real asset commands from `rabbitMQ.realAssetCommands`
- Consuming simulated measurements from `rabbitMQ.simulatedAssetMeasures`
- Translating commands into configured HTTP target requests
- Sending commands to matching targets in live mode, or logging them in dry-run mode

In the standard topology it does not consume
`rabbitMQ.simulatedAssetCommands`; that queue is consumed by the external
simulator.

**Key Classes:**
- `RabbitMQConsumer`: Handles connection and message consumption
- `CommandHandler`: Processes commands (dry-run or live mode)

**Deployment Options:**
- **Docker service**: Runs as container alongside RabbitMQ
- **Manual execution**: Run directly for development/debugging

---

## Installation

### Prerequisites

- Python 3.10+
- Docker and Docker Compose
- Access to the pyfm project

### Step 1: Install Python Dependencies

```bash
cd /path/to/pyfm

# Activate virtual environment (if using one)
source .venv/bin/activate

# Install dependencies including pika (RabbitMQ client)
pip install -r requirements.txt
```

### Step 2: Start RabbitMQ

```bash
cd docker/rabbitmq
docker-compose up -d
```

### Step 3: Start Forwarder

You have two options:

#### Option A: Docker Service (Production)

```bash
cd docker/forwarder
docker-compose up -d

# View logs
docker-compose logs -f
```

#### Option B: Manual Execution (Development)

```bash
cd scripts
python forwarder.py --dry-run --log-level DEBUG

# AEM target setup
python forwarder.py --dry-run --config ../conf/forwarder_targets_aem.json
```

### Step 4: Verify Services

**RabbitMQ Management UI:**
- **URL**: http://localhost:15672
- **Username**: `guest`
- **Password**: `guest`

**Check Forwarder logs:**
```bash
# If running as Docker service
cd docker/forwarder
docker-compose logs -f
```

The exchanges and queues are created automatically by the scripts.

---

## Configuration

### RabbitMQ Connection Settings

#### Default Configuration (localhost)

| Parameter | Default Value |
|-----------|---------------|
| Host | `rabbitMQ.host`, then `localhost` |
| Port | `rabbitMQ.port`, then `5672` |
| Username | `rabbitMQ.username`, then `guest` |
| Password | `rabbitMQ.password`, then `guest` |
| Virtual Host | `rabbitMQ.virtualHost`, then `/` |
| Sources | all valid `rabbitMQ` sections with `exchange`, `queue`, and `routingKey` |

### Environment Variables (Optional)

You can set these environment variables instead of using command-line arguments:

```bash
export RABBITMQ_HOST=localhost
export RABBITMQ_PORT=5672
export RABBITMQ_USER=guest
export RABBITMQ_PASS=guest
export RABBITMQ_VHOST=/
export FORWARDER_RABBIT_SECTIONS=realAssetCommands,simulatedAssetMeasures
export FORWARDER_CONFIG=../conf/forwarder_targets_aem.json
export FORWARDER_CONNS=../conf/private/conns.json
```

### Connection File (conns.json)

You can also add RabbitMQ configuration to your `conf/private/conns.json`:

```json
{
  "nodesAPI": { ... },
  "postgreSQL": { ... },
  "rabbitMQ": {
    "host": "localhost",
    "port": 5672,
    "username": "guest",
    "password": "guest",
    "virtualHost": "/",
    "realAssetCommands": {
      "exchange": "flexi_commands",
      "queue": "flexi_commands_queue",
      "routingKey": "real_asset.command"
    },
    "simulatedAssetCommands": {
      "exchange": "flexi_sim_commands",
      "queue": "flexi_sim_commands_queue",
      "routingKey": "sim_asset.command"
    },
    "simulatedAssetMeasures": {
      "exchange": "flexi_sim_measures",
      "queue": "flexi_sim_measures_queue",
      "routingKey": "sim_asset.measure"
    }
  }
}
```

The forwarder uses `conns.json` as the source of truth for RabbitMQ
exchange/queue/routing key topology. `FORWARDER_RABBIT_SECTIONS` or
`--rabbit-sections` only selects which configured sections to consume. If no
sections are selected explicitly, the forwarder consumes all valid section-like
entries under `rabbitMQ`. The legacy `RABBITMQ_EXCHANGE` /
`--rabbitmq-exchange` option is ignored for section-based sources.

For normal deployments, set `FORWARDER_RABBIT_SECTIONS` explicitly to
`realAssetCommands,simulatedAssetMeasures` so the forwarder does not consume
simulated commands intended for the external simulator.

The forwarder target configuration can also reference API entries in `conns.json`. For example, `conf/forwarder_targets_aem.json` uses `reference_api: "aemAPI"` and resolves the target base URL, credentials, and request-timeout fallback from that connection entry.

### Target Configuration

Targets are loaded from `--config` / `FORWARDER_CONFIG` and describe where matching commands are forwarded. The current forwarder supports:

| Field | Description |
|-------|-------------|
| `name` | Logical target name used in logs |
| `url` | Base target URL, unless resolved from `reference_api` |
| `reference_api` | Key in `conns.json` containing `controlUrl`, credentials, and optional timeout fallback |
| `request_timeout_seconds` | Per-target HTTP POST timeout in seconds; defaults to `10` |
| `request_retries` | Per-target retry count after the initial attempt; defaults to `3` |
| `timeout` | Legacy timeout field; still supported for backward compatibility |
| `asset_types` / `asset_ids` | Optional filters for which assets this target handles |
| `endpoint_template` | Template for building an endpoint from message payload/API fields |
| `endpoint_overrides` | Asset-specific endpoint overrides |
| `asset_request_profiles` | Asset-specific endpoint and body mode settings |
| `body_template` | Template for default request bodies |

Supported request body modes are `hp_control` (default/body template behavior) and `ev_power_timeseries` for EV charger time-series power requests.

At the top level of the targets config file, you can also define default request settings:

| Field | Description |
|-------|-------------|
| `request_timeout_seconds` | Default timeout applied when a target does not set its own value; default `10` |
| `request_retries` | Default retry count applied when a target does not set its own value; default `3` |

Legacy top-level `default_timeout` and `max_retries` are still accepted as compatibility fallbacks for existing configuration files.

For every live outbound POST, the forwarder uses the resolved timeout and retries on:

- network/client exceptions
- request timeouts
- non-success HTTP response status codes

Each failed non-final attempt is logged, and the final exhausted failure is logged as an error.

---

## Usage

### Basic Workflow

1. **Start RabbitMQ** (if not already running)
2. **Start the forwarder** to listen for real commands and simulated measurements
3. **Run flexi_manager** with RabbitMQ enabled to publish commands

### Step-by-Step Examples

#### 1. Start RabbitMQ

```bash
cd docker/rabbitmq
docker-compose up -d
```

#### 2. Start the Forwarder (Terminal 1)

```bash
cd scripts

# Basic dry-run mode (uses default guest/guest credentials)
python forwarder.py --dry-run

# Dry-run with AEM target config
python forwarder.py --dry-run --config ../conf/forwarder_targets_aem.json

# Live forwarding; each message payload still controls whether the HTTP request is dry-run
python forwarder.py --live --config ../conf/forwarder_targets_aem.json

# With verbose logging
python forwarder.py --dry-run --log-level DEBUG

# Filter specific asset types
python forwarder.py --dry-run --asset-types heat_pump,ev_charger
```

#### 3. Run flexi_manager with RabbitMQ (Terminal 2)

```bash
cd scripts

# Dry-run with RabbitMQ publishing (uses default guest/guest credentials)
python flexi_manager.py --fsp supsi01 --dry-run --rabbitmq

# Specify a past time slot
python flexi_manager.py --fsp supsi01 --offset 30m --dry-run --rabbitmq
```

### Command-Line Arguments

#### flexi_manager.py RabbitMQ Arguments

| Argument | Default | Description |
|----------|---------|-------------|
| `--rabbitmq` | disabled | Enable RabbitMQ message publishing |
| `--rabbitmq-host` | `rabbitMQ.host` or `localhost` | RabbitMQ server hostname |
| `--rabbitmq-port` | `rabbitMQ.port` or `5672` | RabbitMQ server port |
| `--rabbitmq-user` | `rabbitMQ.username` or `guest` | RabbitMQ username |
| `--rabbitmq-pass` | `rabbitMQ.password` or `guest` | RabbitMQ password |
| `--rabbitmq-vhost` | `rabbitMQ.virtualHost` or `/` | RabbitMQ virtual host |
| `--rabbitmq-exchange` | none | Deprecated and ignored; exchanges come from destination sections |

#### forwarder.py Arguments

| Argument | Default | Description |
|----------|---------|-------------|
| `--dry-run` / `-d` | enabled | Dry-run mode (log only) |
| `--live` / `-l` | disabled | Allow live HTTP forwarding when message payload `dry_run=false` |
| `--asset-types` | all | Comma-separated list of asset types to filter |
| `--rabbit-sections` | all valid sections | Comma-separated `rabbitMQ` sections to consume; standard deployment uses `realAssetCommands,simulatedAssetMeasures` |
| `--queues` | none | Legacy queue selector; ignored for section-based sources |
| `--rabbitmq-host` | `rabbitMQ.host` or `localhost` | RabbitMQ server hostname |
| `--rabbitmq-port` | `rabbitMQ.port` or `5672` | RabbitMQ server port |
| `--rabbitmq-user` | `rabbitMQ.username` or `guest` | RabbitMQ username |
| `--rabbitmq-pass` | `rabbitMQ.password` or `guest` | RabbitMQ password |
| `--rabbitmq-vhost` | `rabbitMQ.virtualHost` or `/` | RabbitMQ virtual host |
| `--rabbitmq-exchange` | none | Legacy exchange option; ignored for section-based sources |
| `--log-level` | `INFO` | Logging level |
| `--log-file` | none | Log file path |
| `--config` / `-c` | `../conf/forwarder_targets.json` | Forwarding target configuration |
| `--conns` | `../conf/private/conns.json` | Connection file used by RabbitMQ sources and `reference_api` targets |

---

## Message Format

### Command Message

Published by `flexi_manager.py` when flexibility needs to be activated:

```json
{
  "message_type": "command",
  "asset_id": "ECM97.1",
  "asset_type": "heat_pump",
  "command_type": "curtail",
  "payload": {
    "asset_id": "ECM97.1",
    "description": "Heat Pump Building A",
    "asset_type": "heat_pump",
    "modulation_type": "discrete",
    "requested_curtailment_kw": 15.0,
    "actual_curtailment_kw": 15.0,
    "target_power_kw": 0.0,
    "capacity_kw": 15.0,
    "duration_minutes": 15,
    "discrete_state": "OFF",
    "slot_start": "2026-01-16T10:00:00",
    "slot_end": "2026-01-16T10:15:00",
    "dry_run": true,
    "timestamp": "2026-01-16T09:59:30+00:00"
  },
  "timestamp": "2026-01-16T09:59:30+00:00",
  "priority": 7
}
```

### Batch Header Message

Published before a batch of commands:

```json
{
  "message_type": "batch_start",
  "slot_info": {
    "fsp_id": "supsi01",
    "slot_start": "2026-01-16T10:00:00",
    "slot_end": "2026-01-16T10:15:00",
    "total_flexibility_kw": 45.0,
    "allocation_strategy": "modulation_aware",
    "dry_run": true
  },
  "command_count": 3,
  "timestamp": "2026-01-16T09:59:30+00:00"
}
```

### Restore Command

Published when flexibility activation period ends:

```json
{
  "message_type": "command",
  "asset_id": "ECM97.1",
  "asset_type": "heat_pump",
  "command_type": "restore",
  "payload": {
    "asset_id": "ECM97.1",
    "description": "Heat Pump Building A",
    "asset_type": "heat_pump",
    "action": "restore",
    "target_state": "ON",
    "discrete_state": "ON",
    "target_power_kw": 15.0,
    "capacity_kw": 15.0,
    "slot_start": "2026-01-16T10:15:00",
    "slot_end": "2026-01-16T10:30:00",
    "dry_run": true,
    "timestamp": "2026-01-16T10:14:30+00:00"
  },
  "timestamp": "2026-01-16T10:14:30+00:00",
  "priority": 5
}
```

For EV charger commands, `flexi_manager.py` enriches the payload with `power_kw` and a `schedule` dictionary containing one power value per 15-minute point in the slot window.

### Measurement Message

Published by the external simulator to `rabbitMQ.simulatedAssetMeasures`:

```json
{
  "message_type": "measurement",
  "asset_id": "ECM68.3",
  "asset_type": "heat_pump",
  "measurement_type": "power",
  "payload": {
    "power_kw": 3.8
  },
  "timestamp": "2026-01-16T10:00:00+00:00"
}
```

`forwarder.py` dispatches `message_type="measurement"` to its measurement
handler and reads `asset_id`, `asset_type`, `measurement_type`, `timestamp`,
and `payload`.

### Dry-Run and Live Forwarding

The forwarder has two levels of dry-run control:

| Forwarder mode | Message payload `dry_run` | Result |
|----------------|---------------------------|--------|
| `--dry-run` | any value | Logs only; no HTTP request is sent |
| `--live` | `true` | Logs only; no HTTP request is sent |
| `--live` | `false` | Sends HTTP POST requests to configured matching targets |

The built-in local protocol handlers in `CommandHandler` still only log actions. Actual external actuation happens through configured HTTP targets such as the AEM simulator/API.

---

## Troubleshooting

### Common Issues

#### 1. Connection Refused

**Symptom**: `Failed to connect to RabbitMQ: [Errno 111] Connection refused`

**Solutions**:
- Ensure RabbitMQ is running: `docker-compose ps`
- Check if port 5672 is available: `netstat -tlnp | grep 5672`
- Verify the host/port configuration

```bash
# Check if RabbitMQ is accessible
docker exec pyfm_rabbitmq rabbitmqctl status
```

#### 2. Authentication Failed

**Symptom**: `ACCESS_REFUSED - Login was refused using authentication mechanism PLAIN`

**Solutions**:
- Verify username and password
- Check if user has permissions for the virtual host
- Use the management UI to verify user exists

```bash
# List users
docker exec pyfm_rabbitmq rabbitmqctl list_users

# List permissions
docker exec pyfm_rabbitmq rabbitmqctl list_permissions -p /pyfm
```

#### 3. Messages Not Being Received

**Symptom**: flexi_manager publishes but forwarder doesn't receive

**Solutions**:
- Ensure both are using the same virtual host
- Check queue bindings in management UI
- Verify routing keys match
- For real commands, check `rabbitMQ.realAssetCommands` and each real asset's
  `rabbitCommandSection`
- For simulated measurements, check `rabbitMQ.simulatedAssetMeasures` and
  `FORWARDER_RABBIT_SECTIONS`

```bash
# List queues and message counts
docker exec pyfm_rabbitmq rabbitmqctl list_queues -p / name messages consumers

# List bindings
docker exec pyfm_rabbitmq rabbitmqctl list_bindings -p /
```

#### 4. pika Not Installed

**Symptom**: `ModuleNotFoundError: No module named 'pika'`

**Solution**:
```bash
pip install pika==1.3.2
# or
pip install -r requirements.txt
```

#### 5. RabbitMQ Source Not Found

**Symptom**: startup fails with `rabbitMQ.<section> section not found` or
`missing required field(s): exchange, queue, routingKey`.

**Solutions**:
- Check `FORWARDER_RABBIT_SECTIONS` / `--rabbit-sections`.
- Confirm each selected section exists under `rabbitMQ` in `conns.json`.
- Confirm each selected section has non-empty `exchange`, `queue`, and
  `routingKey` values.

```bash
export FORWARDER_RABBIT_SECTIONS=realAssetCommands,simulatedAssetMeasures
```

#### 6. Forwarder Receives Simulated Commands

**Symptom**: forwarder logs `command` messages from simulated assets.

**Solution**: remove `simulatedAssetCommands` from
`FORWARDER_RABBIT_SECTIONS`. That queue is for the external simulator.

#### 7. Simulator Does Not Receive Commands

**Solutions**:
- Check `rabbitMQ.simulatedAssetCommands.exchange`, `.queue`, and
  `.routingKey`.
- Check simulated assets have
  `asset_mapping.<asset_id>.rabbitCommandSection = "simulatedAssetCommands"`.

#### 8. Forwarder Does Not Receive Simulated Measurements

**Solutions**:
- Check `rabbitMQ.simulatedAssetMeasures.exchange`, `.queue`, and
  `.routingKey`.
- Check `FORWARDER_RABBIT_SECTIONS=realAssetCommands,simulatedAssetMeasures`.

### Monitoring Commands

```bash
# Check RabbitMQ status
docker exec pyfm_rabbitmq rabbitmqctl status

# List connections
docker exec pyfm_rabbitmq rabbitmqctl list_connections

# List channels
docker exec pyfm_rabbitmq rabbitmqctl list_channels

# List queues with message counts
docker exec pyfm_rabbitmq rabbitmqctl list_queues name messages consumers

# List bindings
docker exec pyfm_rabbitmq rabbitmqctl list_bindings

# Purge a queue (clear all messages)
docker exec pyfm_rabbitmq rabbitmqctl purge_queue flexi_commands_queue
```

### Log Files

- **flexi_manager**: Use `--log_file` argument
- **forwarder (manual)**: Use `--log-file` argument
- **forwarder (Docker)**: Log file at `docker/forwarder/logs/forwarder.log`
- **RabbitMQ**: `docker-compose logs -f`

```bash
# View forwarder console logs (Docker)
cd docker/forwarder
docker-compose logs -f

# View forwarder log file
tail -f logs/forwarder.log
```

---

## Docker Deployment

The system is deployed as **two separate services** for flexibility and independent scaling.

### 1. Start RabbitMQ

```bash
cd docker/rabbitmq
docker-compose up -d
```

### 2. Start Forwarder

```bash
cd docker/forwarder
docker-compose up -d
```

### Environment Variables

Configure the forwarder via environment variables or `.env` file:

| Variable | Default | Description |
|----------|---------|-------------|
| `FORWARDER_MODE` | `dry-run` | `dry-run` or `live` |
| `FORWARDER_LOG_LEVEL` | `INFO` | DEBUG, INFO, WARNING, ERROR |
| `FORWARDER_LOG_FILE` | `/app/logs/forwarder.log` | Log file path (Docker mount: `./logs`) |
| `FORWARDER_ASSET_TYPES` | (all) | Filter: `heat_pump,ev_charger` |
| `FORWARDER_RABBIT_SECTIONS` | `realAssetCommands,simulatedAssetMeasures` in Docker examples; otherwise all valid sections when unset | RabbitMQ sections consumed by the forwarder; do not include `simulatedAssetCommands` in the standard deployment |
| `FORWARDER_QUEUES` | none | Legacy queue selector; ignored |
| `FORWARDER_CONFIG` | `../conf/forwarder_targets.json` | Target configuration path |
| `FORWARDER_CONNS` | `../conf/private/conns.json` | Connection file for RabbitMQ sources and `reference_api` targets |
| `RABBITMQ_HOST` | `rabbitMQ.host` or `localhost` | RabbitMQ hostname override |
| `RABBITMQ_PORT` | `rabbitMQ.port` or `5672` | RabbitMQ port override |
| `RABBITMQ_USER` | `rabbitMQ.username` or `guest` | RabbitMQ username override |
| `RABBITMQ_PASS` | `rabbitMQ.password` or `guest` | RabbitMQ password override |
| `RABBITMQ_VHOST` | `rabbitMQ.virtualHost` or `/` | RabbitMQ virtual host override |
| `RABBITMQ_EXCHANGE` | none | Legacy exchange override; ignored |
| `RABBITMQ_CONNECT_RETRIES` | `10` | Connection retry attempts |
| `RABBITMQ_CONNECT_RETRY_DELAY` | `5` | Seconds between retries |

### Building the Image

```bash
# From project root
docker build -f docker/Dockerfile.forwarder -t pyfm-forwarder .

# Or via docker-compose
cd docker/forwarder
docker-compose build
```

### Viewing Logs

```bash
# RabbitMQ logs
cd docker/rabbitmq
docker-compose logs -f

# Forwarder console logs
cd docker/forwarder
docker-compose logs -f

# Forwarder log file
tail -f docker/forwarder/logs/forwarder.log
```

### Stopping Services

```bash
# Stop forwarder
cd docker/forwarder
docker-compose down

# Stop RabbitMQ
cd docker/rabbitmq
docker-compose down
```

---

## Future Enhancements

The current implementation can forward live HTTP requests to configured targets. Future enhancements may include:

1. **Additional Protocol Handlers**
   - MQTT protocol handler for IoT devices
   - Modbus handler for industrial equipment
   - OCPP handler for EV chargers

2. **Enhanced Reliability**
   - Dead letter queues for failed messages
   - Retry backoff/jitter beyond the current fixed retry loop
   - Circuit breaker pattern for device communication

3. **Monitoring & Alerting**
   - Prometheus metrics export
   - Grafana dashboards
   - Alert rules for queue depth, failures, etc.

4. **Security**
   - TLS encryption for RabbitMQ connections
   - Certificate-based authentication
   - Message signing/verification

---

## Related Documentation

- [README_flexi_manager.md](README_flexi_manager.md) - Detailed flexi_manager documentation
- [README_trader_fsp.md](README_trader_fsp.md) - FSP trading agent documentation
- [docker/rabbitmq/README.md](../docker/rabbitmq/README.md) - RabbitMQ Docker setup
- [docker/forwarder/README.md](../docker/forwarder/README.md) - Forwarder Docker setup
