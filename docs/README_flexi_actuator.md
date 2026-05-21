# Flexibility Actuator (flexi_actuator.py)

`scripts/flexi_actuator.py` is a one-shot command publisher for flexibility assets. It builds manager-compatible command envelopes and publishes them to RabbitMQ without running the full flexibility manager workflow.

Use it when you need to manually force one or more configured assets off, on, or back to normal operation.

## Basic Usage

Run from the `scripts` directory:

```bash
python flexi_actuator.py --config_file ../conf/test_fm01_aem.json --fsp supsi01 --flexibility ECM96.2 --command force_off
```

Multiple assets can be passed with repeated `--flexibility` options:

```bash
python flexi_actuator.py --config_file ../conf/test_fm01_aem.json --fsp supsi01 --flexibility ECM96.2 --flexibility ECM97.1 --command force_off
```

Or with `--flexibilities`:

```bash
python flexi_actuator.py --config_file ../conf/test_fm01_aem.json --fsp supsi01 --flexibilities ECM96.2 ECM97.1 --command force_off
```

## Supported Commands

- `force_off`: curtail the asset to zero or OFF where applicable
- `force_on`: request no curtailment
- `restore`: send a restore command

Aliases are also accepted for some commands:

- `off` -> `force_off`
- `on` -> `force_on`

## Dry Run

Use `--dry-run` to validate the command envelope without publishing to RabbitMQ:

```bash
python flexi_actuator.py --config_file ../conf/test_fm01_aem.json --fsp supsi01 --flexibility ECM96.2 --command force_off --dry-run
```

Dry-run output logs the requested actuator payload, queued commands, RabbitMQ destination, and the fact that publishing was skipped.

## Verbose RabbitMQ JSON

Use `--verbose` to print the RabbitMQ JSON message body strings. In live mode, these are the bodies sent to RabbitMQ. In dry-run mode, these are the bodies that would be sent.

```bash
python flexi_actuator.py --config_file ../conf/test_fm01_aem.json --fsp supsi02 --flexibilities ECM97.1 --command force_off --dry-run --verbose
```

Verbose output includes the batch header message and each command message:

```text
RabbitMQ batch header JSON (routing_key=real_asset.command): {...}
RabbitMQ command JSON (routing_key=real_asset.command): {...}
```

## RabbitMQ Configuration

The script reads RabbitMQ connection and destination settings from the configured JSON files.

The main config points to the connections file:

```json
{
  "connectionsFile": "../conf/private/conns.json"
}
```

The RabbitMQ command destination is resolved from each asset's
`asset_mapping.<asset>.rabbitCommandSection`. Allowed command values are
`realAssetCommands` and `simulatedAssetCommands`; `simulatedAssetMeasures` is
not valid for actuator commands.

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
  },
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
    }
  }
}
```

If `rabbitCommandSection` is missing, invalid, points to a missing `rabbitMQ`
section, or selects a section missing `exchange`, `queue`, or `routingKey`, the
script exits without publishing. It does not fall back to `rabbitMQ.exchange`
or `commands.{asset_type}.{asset_id}`.

## RabbitMQ Destination Overrides

Destination override flags are deprecated and ignored:

- `--rabbitmq-exchange`
- `--rabbit-exchange`
- `--rabbit-queue`
- `--rabbit-routing-key`

The script logs the section-resolved destination before publishing:

```text
RabbitMQ destination for asset ECM68.3 via section simulatedAssetCommands: exchange=flexi_sim_commands, queue=flexi_sim_commands_queue, routing_key=sim_asset.command
```

## Connection Overrides

The existing RabbitMQ connection options are still available:

```bash
python flexi_actuator.py --config_file ../conf/test_fm01_aem.json --fsp supsi01 --flexibility ECM96.2 --command force_off --rabbitmq-host localhost --rabbitmq-port 5672 --rabbitmq-user guest --rabbitmq-pass guest --rabbitmq-vhost /
```

`--rabbitmq-exchange` is still accepted by the CLI for backward compatibility,
but it is ignored. Message exchanges are read only from
`rabbitMQ.realAssetCommands`, `rabbitMQ.simulatedAssetCommands`, or
`rabbitMQ.simulatedAssetMeasures`.

## Common Examples

Current behavior without destination overrides:

```bash
python flexi_actuator.py --config_file ../conf/test_fm01_aem.json --fsp supsi01 --flexibility ECM96.2 --command force_off
```

Print the RabbitMQ JSON without publishing:

```bash
python flexi_actuator.py --config_file ../conf/test_fm01_aem.json --fsp supsi02 --flexibilities ECM97.1 --command force_off --dry-run --verbose
```

Restore an asset:

```bash
python flexi_actuator.py --config_file ../conf/test_fm01_aem.json --fsp supsi01 --flexibility ECM96.2 --command restore
```

Set a custom command duration:

```bash
python flexi_actuator.py --config_file ../conf/test_fm01_aem.json --fsp supsi01 --flexibility ECM63.1 --command force_on --duration-minutes 30
```

## Notes

- Asset labels must exist in `asset_mapping`.
- The selected FSP must exist in `fm.actors.fsps`.
- If the FSP config lists assets, each requested flexibility label must belong to that FSP.
- EV charger payloads include a generated schedule aligned to the configured EV interval.
