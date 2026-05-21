# RabbitMQ for PyFM

Message broker for the flexibility command forwarding system.

## Topology

```text
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

`pyfm` does not implement the simulated asset application. It only publishes
simulated asset commands and can consume simulated measures produced by an
external simulator.

| Section | Exchange | Queue | Routing key | Producer | Consumer |
|---------|----------|-------|-------------|----------|----------|
| `rabbitMQ.realAssetCommands` | `flexi_commands` | `flexi_commands_queue` | `real_asset.command` | `flexi_manager.py` / `flexi_actuator.py` | `forwarder.py` |
| `rabbitMQ.simulatedAssetCommands` | `flexi_sim_commands` | `flexi_sim_commands_queue` | `sim_asset.command` | `flexi_manager.py` / `flexi_actuator.py` | External simulator application |
| `rabbitMQ.simulatedAssetMeasures` | `flexi_sim_measures` | `flexi_sim_measures_queue` | `sim_asset.measure` | External simulator application | `forwarder.py` |

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
python forwarder.py --dry-run --conns ../conf/private/conns.json --rabbit-sections realAssetCommands,simulatedAssetMeasures
```

Do not include `simulatedAssetCommands` in the standard forwarder deployment;
that queue is consumed by the external simulator.

## Usage with flexi_manager

Send commands from flexi_manager:

```bash
cd ../../scripts
python flexi_manager.py --fsp supsi01 --rabbitmq --dry-run
```

Each asset command destination is selected by
`asset_mapping.<asset_id>.rabbitCommandSection`. Use `realAssetCommands` for
real assets and `simulatedAssetCommands` for simulated assets. Assets without
`rabbitCommandSection` are skipped by the command publishers.

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

## Troubleshooting

- If the forwarder receives simulated asset commands,
  `FORWARDER_RABBIT_SECTIONS` probably includes `simulatedAssetCommands`.
- If the simulator does not receive commands, check
  `rabbitMQ.simulatedAssetCommands` and the simulated assets'
  `rabbitCommandSection`.
- If the forwarder does not receive simulated measurements, check
  `rabbitMQ.simulatedAssetMeasures` and
  `FORWARDER_RABBIT_SECTIONS=realAssetCommands,simulatedAssetMeasures`.
- If real commands are not forwarded, check `rabbitMQ.realAssetCommands` and
  the real assets' `rabbitCommandSection`.

## Network

RabbitMQ creates a `pyfm_network` bridge network. Other services can join this network to communicate with RabbitMQ using hostname `rabbitmq` or `pyfm_rabbitmq`.
