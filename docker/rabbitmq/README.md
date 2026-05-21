# RabbitMQ for PyFM

Message broker for the flexibility command forwarding system.

## Quick Start

```bash
cd docker/rabbitmq

# Start RabbitMQ
docker-compose up -d

# Check status
docker-compose ps

# Stop
docker-compose down
```

## Connection Details

| Setting | Value |
|---------|-------|
| **Management UI** | http://localhost:15672 |
| **AMQP Port** | 5672 |
| **Username** | `guest` |
| **Password** | `guest` |

## Usage with Forwarder

After starting RabbitMQ, start the forwarder service:

```bash
cd ../forwarder
docker-compose up -d
```

Or run forwarder manually for development:

```bash
cd ../../scripts
python forwarder.py --dry-run --conns ../conf/private/conns.json --rabbit-sections realAssetCommands,simulatedAssetCommands,simulatedAssetMeasures
```

## Usage with flexi_manager

Send commands from flexi_manager:

```bash
cd ../../scripts
python flexi_manager.py --fsp supsi01 --rabbitmq --dry-run
```

## Monitoring

```bash
# View logs
docker-compose logs -f

# List queues
docker exec pyfm_rabbitmq rabbitmqctl list_queues name messages consumers

# List connections
docker exec pyfm_rabbitmq rabbitmqctl list_connections

# Purge a queue
docker exec pyfm_rabbitmq rabbitmqctl purge_queue flexi_commands_queue
```

## Network

RabbitMQ creates a `pyfm_network` bridge network. Other services can join this network to communicate with RabbitMQ using hostname `rabbitmq` or `pyfm_rabbitmq`.
