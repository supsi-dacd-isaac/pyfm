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
- **Protocol Translation**: The forwarder can translate to different device protocols (MQTT, HTTP, Modbus, OCPP)
- **Decoupling**: flexi_manager and forwarder can be deployed and scaled independently

### Data Flow

```
┌─────────────────┐     ┌──────────────┐     ┌─────────────────┐     ┌─────────────┐
│ flexi_manager   │────▶│   RabbitMQ   │────▶│    forwarder    │────▶│   Devices   │
│ (Decision)      │     │   (Broker)   │     │   (Actuation)   │     │ (HP, EV...) │
└─────────────────┘     └──────────────┘     └─────────────────┘     └─────────────┘
        │                       │                     │
        │                       │                     │
   Calculates             Queues and            Receives and
   flexibility            persists              forwards to
   allocation             messages              actual devices
```

---

## Architecture

### System Components

| Component | Role | Script/Service |
|-----------|------|----------------|
| **flexi_manager** | Calculates flexibility allocation and publishes commands | `scripts/flexi_manager.py` |
| **RabbitMQ** | Message broker for reliable command delivery | Docker container |
| **forwarder** | Consumes commands and forwards to devices | `scripts/forwarder.py` |

### RabbitMQ Topology

```
Exchange: flexi_commands (topic)
    │
    ├── Routing Key: commands.#
    │       └── Queue: asset_commands
    │               └── Consumer: forwarder.py
    │
    └── Routing Key: measurements.#
            └── Queue: asset_measurements
                    └── Consumer: forwarder.py (optional)
```

### Message Types

| Type | Description | Routing Key Pattern |
|------|-------------|---------------------|
| `command` | Control commands (curtail, restore) | `commands.{asset_type}.{asset_id}` |
| `measurement` | Measurement data | `measurements.{asset_type}.{asset_id}` |
| `batch_start` | Batch header with slot info | `commands.batch.header` |

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
- Consuming commands from RabbitMQ
- Translating commands to device protocols
- Sending commands to actual devices (or logging in dry-run mode)

**Key Classes:**
- `RabbitMQConsumer`: Handles connection and message consumption
- `DryRunHandler`: Logs commands without actuation (current implementation)

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
# Navigate to RabbitMQ docker folder
cd docker/rabbitmq

# Start RabbitMQ container
docker-compose up -d

# Verify it's running
docker-compose ps

# Check logs
docker-compose logs -f rabbitmq
```

### Step 3: Verify RabbitMQ is Ready

Open the management UI in your browser:
- **URL**: http://localhost:15672
- **Username**: `pyfm` (or `guest`)
- **Password**: `pyfm_secret` (or `guest`)

You should see the pre-configured exchanges and queues.

---

## Configuration

### RabbitMQ Connection Settings

#### Default Configuration (localhost)

| Parameter | Default Value |
|-----------|---------------|
| Host | `localhost` |
| Port | `5672` |
| Username | `guest` |
| Password | `guest` |
| Virtual Host | `/` |
| Exchange | `flexi_commands` |

### Environment Variables (Optional)

You can set these environment variables instead of using command-line arguments:

```bash
export RABBITMQ_HOST=localhost
export RABBITMQ_PORT=5672
export RABBITMQ_USER=guest
export RABBITMQ_PASS=guest
export RABBITMQ_VHOST=/
```

### Configuration File (conns.json)

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
    "exchange": "flexi_commands"
  }
}
```

---

## Usage

### Basic Workflow

1. **Start RabbitMQ** (if not already running)
2. **Start the forwarder** to listen for commands
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
| `--rabbitmq-host` | `localhost` | RabbitMQ server hostname |
| `--rabbitmq-port` | `5672` | RabbitMQ server port |
| `--rabbitmq-user` | `guest` | RabbitMQ username |
| `--rabbitmq-pass` | `guest` | RabbitMQ password |
| `--rabbitmq-vhost` | `/` | RabbitMQ virtual host |
| `--rabbitmq-exchange` | `flexi_commands` | RabbitMQ exchange name |

#### forwarder.py Arguments

| Argument | Default | Description |
|----------|---------|-------------|
| `--dry-run` / `-d` | enabled | Dry-run mode (log only) |
| `--live` / `-l` | disabled | Live mode (NOT IMPLEMENTED) |
| `--asset-types` | all | Comma-separated list of asset types to filter |
| `--queues` | `commands` | Queues to consume: `commands`, `measurements` |
| `--rabbitmq-host` | `localhost` | RabbitMQ server hostname |
| `--rabbitmq-port` | `5672` | RabbitMQ server port |
| `--rabbitmq-user` | `guest` | RabbitMQ username |
| `--rabbitmq-pass` | `guest` | RabbitMQ password |
| `--rabbitmq-vhost` | `/` | RabbitMQ virtual host |
| `--rabbitmq-exchange` | `flexi_commands` | RabbitMQ exchange name |
| `--log-level` | `INFO` | Logging level |
| `--log-file` | none | Log file path |

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
    "timestamp": "2026-01-16T10:00:00+00:00"
  },
  "timestamp": "2026-01-16T10:00:00+00:00",
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
    "timestamp": "2026-01-16T10:15:00+00:00"
  },
  "timestamp": "2026-01-16T10:15:00+00:00",
  "priority": 5
}
```

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

#### 5. Queue Not Found

**Symptom**: `NOT_FOUND - no queue 'asset_commands' in vhost '/'`

**Solutions**:
- Make sure RabbitMQ started with the definitions.json loaded
- Recreate the container to reload definitions:

```bash
cd docker/rabbitmq
docker-compose down -v
docker-compose up -d
```

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
docker exec pyfm_rabbitmq rabbitmqctl purge_queue asset_commands
```

### Log Files

- **flexi_manager**: Use `--log_file` argument
- **forwarder**: Use `--log-file` argument  
- **RabbitMQ**: `docker-compose logs -f rabbitmq`

---

## Future Enhancements

The current implementation includes dry-run mode only for the forwarder. Future enhancements may include:

1. **Live Mode Implementation**
   - MQTT protocol handler for IoT devices
   - HTTP/REST handler for API-based devices
   - Modbus handler for industrial equipment
   - OCPP handler for EV chargers

2. **Enhanced Reliability**
   - Dead letter queues for failed messages
   - Retry logic with exponential backoff
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
