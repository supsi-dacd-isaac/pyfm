# Forwarder Service

Docker configuration for the PyFM Forwarder service, which receives commands from RabbitMQ and forwards them to actual devices.

## Prerequisites

RabbitMQ must be running. Start it first:

```bash
cd ../rabbitmq
docker-compose up -d
```

## Quick Start

```bash
cd docker/forwarder

# Build and start
docker-compose up -d

# View console logs
docker-compose logs -f

# View log file
tail -f logs/forwarder.log

# Stop
docker-compose down
```

## Logging

Logs are written to both:
- **Console**: View with `docker-compose logs -f`
- **File**: `./logs/forwarder.log` (mounted volume)

The logs directory is automatically created when the container starts.

```bash
# View live log file
tail -f logs/forwarder.log

# View last 100 lines
tail -n 100 logs/forwarder.log

# Search for errors
grep ERROR logs/forwarder.log
```

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `FORWARDER_MODE` | `dry-run` | `dry-run` or `live` |
| `FORWARDER_LOG_LEVEL` | `INFO` | DEBUG, INFO, WARNING, ERROR |
| `FORWARDER_LOG_FILE` | `/app/logs/forwarder.log` | Log file path (inside container) |
| `FORWARDER_ASSET_TYPES` | (all) | Filter: `heat_pump,ev_charger` |
| `FORWARDER_QUEUES` | `commands` | Queues: `commands,measurements` |
| `RABBITMQ_HOST` | `localhost` | RabbitMQ hostname |
| `RABBITMQ_PORT` | `5672` | RabbitMQ port |
| `RABBITMQ_USER` | `guest` | RabbitMQ username |
| `RABBITMQ_PASS` | `guest` | RabbitMQ password |
| `RABBITMQ_VHOST` | `/` | RabbitMQ virtual host |
| `RABBITMQ_EXCHANGE` | `flexi_commands` | Exchange name |
| `RABBITMQ_CONNECT_RETRIES` | `10` | Connection retry attempts |
| `RABBITMQ_CONNECT_RETRY_DELAY` | `5` | Seconds between retries |

### Using .env File

Create a `.env` file to customize settings:

```bash
# .env
FORWARDER_MODE=dry-run
FORWARDER_LOG_LEVEL=DEBUG
RABBITMQ_HOST=192.168.1.100
```

### Network Modes

By default, the forwarder uses `host` network mode to connect to RabbitMQ on localhost.

**Option 1: Host Network (default)**
```yaml
network_mode: host
```
- Forwarder connects to `localhost:5672`
- Simple, works when RabbitMQ is on the same host

**Option 2: Bridge Network (for Docker-to-Docker)**

If RabbitMQ is in a Docker network:

```bash
# Set environment variable
export FORWARDER_NETWORK_MODE=bridge
export RABBITMQ_HOST=pyfm_rabbitmq

# Or edit docker-compose.yml to use networks
```

## Usage Examples

### Start with Debug Logging

```bash
FORWARDER_LOG_LEVEL=DEBUG docker-compose up -d
```

### Connect to Remote RabbitMQ

```bash
RABBITMQ_HOST=rabbitmq.example.com docker-compose up -d
```

### Filter Specific Asset Types

Edit `docker-compose.yml` and uncomment:
```yaml
FORWARDER_ASSET_TYPES: heat_pump,ev_charger
```

### Rebuild After Code Changes

```bash
docker-compose up --build -d
```

## Log Rotation

For production, consider setting up log rotation. Create `/etc/logrotate.d/pyfm-forwarder`:

```
/path/to/pyfm/docker/forwarder/logs/*.log {
    daily
    rotate 7
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
```

## Monitoring

```bash
# View real-time console logs
docker-compose logs -f

# View real-time file logs
tail -f logs/forwarder.log

# Check container status
docker-compose ps

# Inspect container
docker inspect pyfm_forwarder
```

## Integration with flexi_manager

Once the forwarder is running, use flexi_manager to send commands:

```bash
cd ../../scripts
python flexi_manager.py --fsp supsi01 --rabbitmq --dry-run
```

The forwarder will receive and process the commands, logging to both console and file.
