# RabbitMQ Command Messages

This note summarizes the RabbitMQ messages produced by `scripts/flexi_manager.py`
and the direct actuator script `scripts/flexi_actuator.py`. The requested
`scripts/flexi_activation.py` file is not present in this repository.

## Topology

Both scripts publish to the durable topic exchange `flexi_commands`.

| Routing key | Queue | Purpose |
| --- | --- | --- |
| `commands.{asset_type}.{asset_id}` | `asset_commands` | Asset control commands |
| `commands.batch.header` | `asset_commands` | Optional batch metadata sent before commands |
| `measurements.{asset_type}.{asset_id}` | `asset_measurements` | Asset measurement messages; publisher helper exists in `flexi_manager.py` |

Command messages are JSON with persistent delivery (`delivery_mode=2`),
`content_type=application/json`, and a RabbitMQ priority matching the message
`priority` field.

## Common Command Envelope

Every asset command uses this outer envelope:

```json
{
  "message_type": "command",
  "asset_id": "ECM97.1",
  "asset_type": "heat_pump",
  "command_type": "curtail",
  "payload": {},
  "timestamp": "2026-05-20T10:00:00+00:00",
  "priority": 7
}
```

The payload is command-specific. Before publishing pending commands,
`flexi_manager.py` adds `dry_run`, `slot_start`, and `slot_end` when slot
metadata is available. EV charger payloads also receive a `schedule` dictionary
keyed by UTC timestamps, with one power value per 15-minute interval.

## Batch Header

When commands are published as a batch, a header can be sent first on
`commands.batch.header`:

```json
{
  "message_type": "batch_start",
  "slot_info": {
    "fsp_id": "supsi01",
    "slot_start": "2026-05-20T10:00:00",
    "slot_end": "2026-05-20T10:15:00",
    "total_flexibility_kw": 45.0,
    "allocation_strategy": "modulation_aware",
    "dry_run": true
  },
  "command_count": 3,
  "timestamp": "2026-05-20T09:59:30+00:00"
}
```

`flexi_actuator.py` uses the same header shape, with
`allocation_strategy=manual_actuation`, plus `duration_minutes`,
`requested_command`, and `requested_payload`.

## `curtail`

Produced by `flexi_manager.py` when an activation allocation reduces asset
power. Also produced by `flexi_actuator.py` for direct `force_off` and
`force_on` commands.

Priority: `7`

Typical payload:

```json
{
  "community": "community-id",
  "site_id": "POD-ID",
  "asset_id": "ECM97.1",
  "description": "Heat pump",
  "asset_type": "heat_pump",
  "modulation_type": "discrete",
  "requested_curtailment_kw": 15.0,
  "actual_curtailment_kw": 15.0,
  "target_power_kw": 0.0,
  "capacity_kw": 15.0,
  "duration_minutes": 15,
  "discrete_state": "OFF",
  "dry_run": true,
  "slot_start": "2026-05-20T10:00:00",
  "slot_end": "2026-05-20T10:15:00",
  "timestamp": "2026-05-20T09:59:30+00:00"
}
```

For continuous assets, `discrete_state` is omitted and `target_power_kw` is
computed as `capacity_kw - requested_curtailment_kw`. For EV chargers,
`power_kw` is set to the target power and a `schedule` is added:

```json
{
  "power_kw": 6.0,
  "target_power_kw": 6.0,
  "schedule": {
    "2026-05-20T10:00:00": 6.0
  }
}
```

In `flexi_actuator.py`, `force_off` maps to `curtail` with
`requested_curtailment_kw` equal to the asset capacity. `force_on` maps to
`curtail` with `requested_curtailment_kw=0.0`. The actuator also adds
`requested_command` to the payload.

## `restore`

Produced when an asset previously controlled by the manager is no longer
selected for curtailment, or by `flexi_actuator.py --command restore`.

Priority: `5`

Typical heat-pump payload:

```json
{
  "community": "community-id",
  "site_id": "POD-ID",
  "asset_id": "ECM97.1",
  "description": "Heat pump",
  "asset_type": "heat_pump",
  "action": "restore",
  "capacity_kw": 15.0,
  "target_state": "ON",
  "discrete_state": "ON",
  "target_power_kw": 15.0,
  "modulation_type": "discrete",
  "dry_run": true,
  "slot_start": "2026-05-20T10:15:00",
  "slot_end": "2026-05-20T10:30:00",
  "timestamp": "2026-05-20T10:14:30+00:00"
}
```

For EV chargers, restore sets `target_power_kw` and `power_kw` from
`restore_power_kw`, then `default_power_kw`, then `capacity_kw`.

## `preactivate`

Produced by `flexi_manager.py` autonomous pre-activation logic for heat pumps
when pre-heating is enabled.

Priority: `6`

Typical payload:

```json
{
  "community": "community-id",
  "site_id": "POD-ID",
  "asset_id": "ECM97.1",
  "description": "Heat pump",
  "asset_type": "heat_pump",
  "modulation_type": "discrete",
  "command": "preactivate",
  "discrete_state": "ON",
  "target_state": "ON",
  "target_power_kw": 15.0,
  "capacity_kw": 15.0,
  "duration_minutes": 15,
  "dry_run": false,
  "slot_start": "2026-05-20T09:45:00",
  "slot_end": "2026-05-20T10:00:00",
  "timestamp": "2026-05-20T09:44:30+00:00"
}
```

The outer envelope uses `command_type=preactivate`.

## Measurement Envelope

`flexi_manager.py` also contains a generic measurement publisher, although the
activation paths above publish commands. Measurement messages use
`measurements.{asset_type}.{asset_id}`:

```json
{
  "message_type": "measurement",
  "asset_id": "ECM97.1",
  "asset_type": "heat_pump",
  "measurement_type": "power",
  "payload": {},
  "timestamp": "2026-05-20T10:00:00+00:00"
}
```
