# RabbitMQ for PyFM

Simple RabbitMQ setup for the flexibility command forwarding system.

## Quick Start

```bash
cd docker/rabbitmq

# Start
docker-compose up -d

# Check it's running
docker-compose ps

# Stop
docker-compose down
```

## Connection

- **URL**: http://localhost:15672
- **AMQP Port**: 5672
- **Username**: `guest`
- **Password**: `guest`

## Usage

```bash
# Start forwarder (Terminal 1)
python scripts/forwarder.py --dry-run

# Run flexi_manager with RabbitMQ (Terminal 2)
python scripts/flexi_manager.py --fsp supsi01 --dry-run --rabbitmq
```

The exchanges and queues are created automatically by the scripts.
