#!/usr/bin/env python3
"""
One-shot RabbitMQ actuator for flexibility assets.

This script builds a direct command map such as:
    {"ECM96.2": "force_off", "ECM97.1": "force_off"}

It then translates supported commands into the same RabbitMQ command
envelopes used by flexi_manager.py so the existing forwarder/consumer
stack can process them, without running the full flexibility manager.
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

# RabbitMQ support (optional)
try:
    import pika

    RABBITMQ_AVAILABLE = True
except ImportError:
    RABBITMQ_AVAILABLE = False


SUPPORTED_COMMANDS = {
    "force_off": "force_off",
    "off": "force_off",
    "force_on": "force_on",
    "on": "force_on",
    "restore": "restore",
}

MODULATION_DEFAULTS = {
    "heat_pump": {"modulation_type": "discrete"},
    "ev_charger": {"modulation_type": "continuous"},
}

DEFAULT_EV_INTERVAL_MINUTES = 15
RABBIT_DESTINATION_SECTIONS = (
    "realAssetCommands",
    "simulatedAssetCommands",
    "simulatedAssetMeasures",
)
RABBIT_COMMAND_SECTIONS = ("realAssetCommands", "simulatedAssetCommands")
RABBIT_DESTINATION_FIELDS = ("exchange", "queue", "routingKey")


def _build_rabbitmq_batch_header(slot_info: dict, command_count: int) -> dict:
    """Build the RabbitMQ batch header message."""
    return {
        "message_type": "batch_start",
        "slot_info": slot_info,
        "command_count": command_count,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _build_rabbitmq_command_message(
    asset_id: str,
    asset_type: str,
    command_type: str,
    payload: dict,
    priority: int,
) -> dict:
    """Build one RabbitMQ command envelope object."""
    return {
        "message_type": "command",
        "asset_id": asset_id,
        "asset_type": asset_type,
        "command_type": command_type,
        "payload": payload,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "priority": priority,
    }


def _build_rabbitmq_command_envelopes(commands: List[dict]) -> List[dict]:
    """Build the RabbitMQ command list body from prepared actuator commands."""
    return [
        _build_rabbitmq_command_message(
            asset_id=command.get("asset_id"),
            asset_type=command.get("asset_type"),
            command_type=command.get("command_type"),
            payload=command.get("payload", {}),
            priority=command.get("priority", 5),
        )
        for command in commands
    ]


def _group_commands_by_rabbitmq_destination(commands: List[dict]) -> Dict[tuple, List[dict]]:
    """Group prepared commands by resolved RabbitMQ destination."""
    grouped: Dict[tuple, List[dict]] = {}
    for command in commands:
        destination = command.get("rabbitmq_destination", {})
        destination_key = (
            destination.get("exchange"),
            destination.get("queue"),
            destination.get("routing_key"),
        )
        grouped.setdefault(destination_key, []).append(command)
    return grouped


def _log_rabbitmq_messages(
    commands: List[dict],
    slot_info: Optional[dict],
    logger: logging.Logger,
) -> None:
    """Log the RabbitMQ JSON body strings that will be published.

    The batch_start header is not published for manual actuator commands, so
    slot_info is logged once for traceability but not as a per-destination
    RabbitMQ message.
    """
    if slot_info:
        logger.info("Slot info (log-only, not published): %s", json.dumps(slot_info))

    grouped_commands = _group_commands_by_rabbitmq_destination(commands)

    for (exchange, queue, routing_key), destination_commands in grouped_commands.items():
        if not exchange or not queue or not routing_key:
            continue

        command_body = json.dumps(_build_rabbitmq_command_envelopes(destination_commands))
        logger.info(
            "RabbitMQ command JSON (exchange=%s, queue=%s, routing_key=%s): %s",
            exchange,
            queue,
            routing_key,
            command_body,
        )


class RabbitMQPublisher:
    """
    Publishes control commands to RabbitMQ using explicit destination sections.
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 5672,
        username: str = "guest",
        password: str = "guest",
        virtual_host: str = "/",
        logger: Optional[logging.Logger] = None,
    ):
        if not RABBITMQ_AVAILABLE:
            raise RuntimeError("pika library not installed. Run: pip install pika")

        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.virtual_host = virtual_host
        self.logger = logger or logging.getLogger(__name__)

        self.connection = None
        self.channel = None
        self._connected = False
        self._declared_destinations = set()

    def connect(self) -> bool:
        """Establish connection to RabbitMQ server."""
        try:
            credentials = pika.PlainCredentials(self.username, self.password)
            parameters = pika.ConnectionParameters(
                host=self.host,
                port=self.port,
                virtual_host=self.virtual_host,
                credentials=credentials,
                heartbeat=600,
                blocked_connection_timeout=300,
            )

            self.connection = pika.BlockingConnection(parameters)
            self.channel = self.connection.channel()

            self._connected = True
            self.logger.info(
                "Connected to RabbitMQ at %s:%d (vhost: %s)",
                self.host,
                self.port,
                self.virtual_host,
            )
            return True

        except Exception as exc:
            self.logger.error("Failed to connect to RabbitMQ: %s", str(exc))
            self._connected = False
            return False

    def disconnect(self):
        """Close RabbitMQ connection."""
        if self.connection and self.connection.is_open:
            try:
                self.connection.close()
                self.logger.info("Disconnected from RabbitMQ")
            except Exception as exc:
                self.logger.warning("Error closing RabbitMQ connection: %s", str(exc))
        self._connected = False

    def is_connected(self) -> bool:
        """Check if connected to RabbitMQ."""
        return self._connected and self.connection and self.connection.is_open

    def _declare_destination(self, exchange: str, queue: str, routing_key: str) -> None:
        """Declare and bind a RabbitMQ destination once per connection."""
        destination_key = (exchange, queue, routing_key)
        if destination_key in self._declared_destinations:
            return

        self.channel.exchange_declare(
            exchange=exchange,
            exchange_type="topic",
            durable=True,
        )
        self.channel.queue_declare(
            queue=queue,
            durable=True,
        )
        self.channel.queue_bind(
            exchange=exchange,
            queue=queue,
            routing_key=routing_key,
        )
        self._declared_destinations.add(destination_key)

    def _publish_batch_header(
        self,
        slot_info: dict,
        command_count: int,
        exchange: str,
        queue: str,
        routing_key: str,
        verbose: bool,
    ) -> None:
        """Publish a batch header to a concrete destination."""
        batch_header = _build_rabbitmq_batch_header(slot_info, command_count)
        body = json.dumps(batch_header)

        self._declare_destination(exchange, queue, routing_key)
        if verbose:
            self.logger.info(
                "RabbitMQ batch header JSON (exchange=%s, queue=%s, routing_key=%s): %s",
                exchange,
                queue,
                routing_key,
                body,
            )
        self.channel.basic_publish(
            exchange=exchange,
            routing_key=routing_key,
            body=body,
            properties=pika.BasicProperties(
                delivery_mode=2,
                content_type="application/json",
            ),
        )

    def publish_command_list(
        self,
        command_envelopes: List[dict],
        verbose: bool = False,
        exchange: Optional[str] = None,
        queue: Optional[str] = None,
        routing_key: Optional[str] = None,
    ) -> bool:
        """Publish a JSON list of command envelopes to RabbitMQ in one message."""
        if not self.is_connected():
            self.logger.warning("Not connected to RabbitMQ - cannot publish command list")
            return False

        if not exchange or not queue or not routing_key:
            self.logger.error(
                "Missing RabbitMQ destination for command list - cannot publish"
            )
            return False

        if not command_envelopes:
            self.logger.warning("No command envelopes to publish")
            return False

        body = json.dumps(command_envelopes)
        message_priority = max(
            envelope.get("priority", 5) for envelope in command_envelopes
        )

        try:
            self._declare_destination(exchange, queue, routing_key)
            if verbose:
                self.logger.info(
                    "RabbitMQ command JSON (exchange=%s, queue=%s, routing_key=%s): %s",
                    exchange,
                    queue,
                    routing_key,
                    body,
                )
            self.channel.basic_publish(
                exchange=exchange,
                routing_key=routing_key,
                body=body,
                properties=pika.BasicProperties(
                    delivery_mode=2,
                    content_type="application/json",
                    priority=message_priority,
                ),
            )
            asset_ids = ", ".join(
                envelope.get("asset_id", "unknown") for envelope in command_envelopes
            )
            self.logger.debug(
                "Published command list to exchange=%s queue=%s routing_key=%s: %d command(s) [%s]",
                exchange,
                queue,
                routing_key,
                len(command_envelopes),
                asset_ids,
            )
            return True

        except Exception as exc:
            self.logger.error("Failed to publish command list: %s", str(exc))
            return False

    def publish_batch_commands(
        self,
        commands: List[dict],
        slot_info: Optional[dict] = None,
        verbose: bool = False,
    ) -> int:
        """Publish multiple commands as a batch."""
        if not self.is_connected():
            self.logger.warning("Not connected to RabbitMQ - cannot publish batch")
            return 0

        success_count = 0
        grouped_commands = _group_commands_by_rabbitmq_destination(commands)

        if slot_info:
            try:
                for (exchange, queue, routing_key), destination_commands in grouped_commands.items():
                    if not exchange or not queue or not routing_key:
                        for command in destination_commands:
                            self.logger.warning(
                                "Skipping RabbitMQ batch header for command without destination: asset=%s",
                                command.get("asset_id"),
                            )
                        continue
                    self._publish_batch_header(
                        slot_info=slot_info,
                        command_count=len(destination_commands),
                        exchange=exchange,
                        queue=queue,
                        routing_key=routing_key,
                        verbose=verbose,
                    )
            except Exception as exc:
                self.logger.warning("Failed to publish batch header: %s", str(exc))

        for (exchange, queue, routing_key), destination_commands in grouped_commands.items():
            if not exchange or not queue or not routing_key:
                for command in destination_commands:
                    self.logger.error(
                        "Missing RabbitMQ destination for command asset %s - cannot publish",
                        command.get("asset_id"),
                    )
                continue

            command_envelopes = _build_rabbitmq_command_envelopes(destination_commands)
            if self.publish_command_list(
                command_envelopes=command_envelopes,
                verbose=verbose,
                exchange=exchange,
                queue=queue,
                routing_key=routing_key,
            ):
                success_count += len(destination_commands)

        self.logger.info(
            "Published %d/%d commands to RabbitMQ",
            success_count,
            len(commands),
        )
        return success_count


def setup_logging(log_level: str = "INFO", log_file: Optional[str] = None) -> logging.Logger:
    """Configure logging with the same formatter used by flexi_manager.py."""
    logger = logging.getLogger("flexi_actuator")
    logger.setLevel(getattr(logging, log_level.upper()))
    logger.handlers.clear()
    logger.propagate = False

    formatter = logging.Formatter("%(asctime)s::%(levelname)s::%(funcName)s::%(message)s")

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    if log_file:
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger


def _resolve_config_path(config_path: str) -> str:
    """Resolve config path with the same relative-path behavior as flexi_manager.py."""
    if os.path.isabs(config_path):
        return config_path
    return os.path.normpath(os.path.join(os.path.dirname(__file__), config_path))


def _load_config(config_path: str, logger: logging.Logger) -> dict:
    """Load JSON configuration from disk."""
    try:
        with open(config_path, "r") as handle:
            return json.load(handle)
    except FileNotFoundError:
        logger.error("Configuration file not found: %s", config_path)
        sys.exit(1)
    except json.JSONDecodeError as exc:
        logger.error("Invalid JSON in configuration file: %s", str(exc))
        sys.exit(1)


def _load_rabbitmq_config(config: dict, config_path: str, logger: logging.Logger) -> dict:
    """Load RabbitMQ settings from the configured connections file, if present."""
    conns_path = config.get("connectionsFile")
    if not conns_path:
        return {}

    if not os.path.isabs(conns_path):
        conns_path = os.path.normpath(os.path.join(os.path.dirname(config_path), conns_path))

    try:
        with open(conns_path, "r") as handle:
            conns = json.load(handle)
    except FileNotFoundError:
        logger.warning("Connections file not found: %s", conns_path)
        return {}
    except json.JSONDecodeError as exc:
        logger.warning("Invalid JSON in connections file: %s", str(exc))
        return {}

    return conns.get("rabbitMQ", {})


def _available_rabbit_destination_sections(rabbitmq_config: dict) -> List[str]:
    """Return configured RabbitMQ sections that may provide destinations."""
    return [
        section_name
        for section_name in RABBIT_DESTINATION_SECTIONS
        if isinstance(rabbitmq_config.get(section_name), dict)
    ]


def _resolve_rabbitmq_command_destination(
    asset_id: str,
    asset_config: dict,
    rabbitmq_config: dict,
    logger: logging.Logger,
    override_exchange: Optional[str] = None,
    override_queue: Optional[str] = None,
    override_routing_key: Optional[str] = None,
) -> Optional[dict]:
    """Resolve the RabbitMQ command destination for one actuator command."""
    section_name = asset_config.get("rabbitCommandSection")
    if not section_name:
        logger.error(
            "Skipping RabbitMQ command for asset %s: missing asset_mapping.rabbitCommandSection",
            asset_id,
        )
        return None

    if section_name not in RABBIT_COMMAND_SECTIONS:
        logger.error(
            "Skipping RabbitMQ command for asset %s: invalid asset_mapping.rabbitCommandSection '%s' "
            "(allowed: %s)",
            asset_id,
            section_name,
            ", ".join(RABBIT_COMMAND_SECTIONS),
        )
        return None

    section = rabbitmq_config.get(section_name)
    if not isinstance(section, dict):
        logger.error(
            "Skipping RabbitMQ command for asset %s: rabbitMQ.%s section not found",
            asset_id,
            section_name,
        )
        return None

    missing_fields = [
        field_name
        for field_name in RABBIT_DESTINATION_FIELDS
        if not section.get(field_name)
    ]
    if missing_fields:
        logger.error(
            "Skipping RabbitMQ command for asset %s: rabbitMQ.%s missing required field(s): %s",
            asset_id,
            section_name,
            ", ".join(missing_fields),
        )
        return None

    exchange = (
        override_exchange.strip()
        if isinstance(override_exchange, str)
        else override_exchange
    )
    queue = override_queue.strip() if isinstance(override_queue, str) else override_queue
    routing_key = (
        override_routing_key.strip()
        if isinstance(override_routing_key, str)
        else override_routing_key
    )

    return {
        "section": section_name,
        "exchange": exchange or section["exchange"],
        "queue": queue or section["queue"],
        "routing_key": routing_key or section["routingKey"],
    }


def _collect_flexibilities(args) -> List[str]:
    """Collect flexibility labels from both supported CLI styles."""
    labels: List[str] = []
    if args.flexibility:
        labels.extend(args.flexibility)
    if args.flexibilities:
        labels.extend(args.flexibilities)
    return labels


def _deduplicate_labels(labels: List[str], logger: logging.Logger) -> List[str]:
    """Preserve order while removing duplicates."""
    seen = set()
    unique_labels = []

    for label in labels:
        if label in seen:
            logger.warning("Ignoring duplicate flexibility label: %s", label)
            continue
        seen.add(label)
        unique_labels.append(label)

    return unique_labels


def _normalize_command(command: str, logger: logging.Logger) -> str:
    """Normalize and validate the requested actuator command."""
    normalized = (command or "").strip().lower()

    if not normalized:
        logger.error("Command must be non-empty")
        sys.exit(1)

    if normalized not in SUPPORTED_COMMANDS:
        logger.error(
            "Unsupported command '%s'. Supported commands: %s",
            command,
            sorted(SUPPORTED_COMMANDS.keys()),
        )
        sys.exit(1)

    return SUPPORTED_COMMANDS[normalized]


def _validate_labels(labels: List[str], config: dict, fsp: str, logger: logging.Logger) -> List[str]:
    """Validate that all requested labels are defined and belong to the FSP."""
    asset_mapping = config.get("asset_mapping", {})
    fsp_assets = set(config.get("fm", {}).get("actors", {}).get("fsps", {}).get(fsp, {}).get("assets", []))

    unknown = [label for label in labels if label not in asset_mapping]
    if unknown:
        logger.error("Unknown flexibility labels: %s", unknown)
        logger.error("Available labels: %s", sorted(asset_mapping.keys()))
        sys.exit(1)

    if fsp_assets:
        unavailable = [label for label in labels if label not in fsp_assets]
        if unavailable:
            logger.error("Flexibility labels not configured for FSP '%s': %s", fsp, unavailable)
            logger.error("FSP '%s' assets: %s", fsp, sorted(fsp_assets))
            sys.exit(1)

    return labels


def _get_modulation_type(asset_config: dict) -> str:
    """Get modulation type with the same fallback behavior as flexi_manager.py."""
    if "modulation_type" in asset_config:
        return asset_config["modulation_type"]
    asset_type = asset_config.get("type", "")
    defaults = MODULATION_DEFAULTS.get(asset_type, {})
    return defaults.get("modulation_type", "continuous")


def _determine_discrete_state(asset_config: dict, curtailment_kw: float) -> tuple:
    """Determine the target ON/OFF state using manager logic."""
    capacity_kw = asset_config.get("capacity_kw", 0)
    discrete_states = asset_config.get("discrete_states_kw", [0.0, capacity_kw])
    threshold_pct = asset_config.get("curtailment_threshold_pct", 50.0)
    threshold_kw = capacity_kw * (threshold_pct / 100.0)

    if curtailment_kw >= threshold_kw:
        return "OFF", min(discrete_states)
    return "ON", max(discrete_states)


def _build_curtail_command(
    asset_id: str,
    asset_config: dict,
    community: Optional[str],
    curtailment_kw: float,
    duration_minutes: int,
    original_command: str,
    logger: logging.Logger,
) -> tuple:
    """Build a manager-compatible curtail command envelope."""
    asset_type = asset_config.get("type", "unknown")
    description = asset_config.get("description", asset_id)
    site_id = asset_config.get("pod", "")
    capacity_kw = float(asset_config.get("capacity_kw", 0) or 0)
    modulation_type = _get_modulation_type(asset_config)

    if modulation_type == "discrete":
        discrete_state, target_power_kw = _determine_discrete_state(asset_config, curtailment_kw)
        actual_curtailment_kw = capacity_kw - target_power_kw
    else:
        discrete_state = None
        target_power_kw = max(0.0, capacity_kw - curtailment_kw)
        actual_curtailment_kw = curtailment_kw

    payload = {
        "community": community,
        "site_id": site_id,
        "asset_id": asset_id,
        "description": description,
        "asset_type": asset_type,
        "modulation_type": modulation_type,
        "requested_curtailment_kw": curtailment_kw,
        "actual_curtailment_kw": actual_curtailment_kw,
        "target_power_kw": target_power_kw,
        "capacity_kw": capacity_kw,
        "duration_minutes": duration_minutes,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "requested_command": original_command,
    }

    if modulation_type == "discrete":
        payload["discrete_state"] = discrete_state
        logger.info(
            "Queued command: set %s (%s) to %s (target: %.2f kW) for %d minutes",
            asset_id,
            description,
            discrete_state,
            target_power_kw,
            duration_minutes,
        )
    else:
        logger.info(
            "Queued command: curtail %s (%s): %.2f kW (target: %.2f kW) for %d minutes",
            asset_id,
            description,
            curtailment_kw,
            target_power_kw,
            duration_minutes,
        )

    command = {
        "asset_id": asset_id,
        "asset_type": asset_type,
        "command_type": "curtail",
        "payload": payload,
        "priority": 7,
    }

    return command, actual_curtailment_kw


def _build_restore_command(
    asset_id: str,
    asset_config: dict,
    community: Optional[str],
    original_command: str,
    duration_minutes: int,
    logger: logging.Logger,
) -> tuple:
    """Build a manager-compatible restore command envelope."""
    asset_type = asset_config.get("type", "unknown")
    description = asset_config.get("description", asset_id)
    site_id = asset_config.get("pod", "")
    capacity_kw = float(asset_config.get("capacity_kw", 0) or 0)

    payload = {
        "community": community,
        "site_id": site_id,
        "asset_id": asset_id,
        "description": description,
        "asset_type": asset_type,
        "action": "restore",
        "duration_minutes": duration_minutes,
        "capacity_kw": capacity_kw,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "requested_command": original_command,
    }

    if asset_type == "ev_charger":
        if asset_config.get("restore_power_kw") is not None:
            restore_power_kw = float(asset_config.get("restore_power_kw"))
            restore_source = "restore_power_kw"
        elif asset_config.get("default_power_kw") is not None:
            restore_power_kw = float(asset_config.get("default_power_kw"))
            restore_source = "default_power_kw"
        else:
            restore_power_kw = capacity_kw
            restore_source = "capacity_kw"

        payload["target_power_kw"] = restore_power_kw
        payload["power_kw"] = restore_power_kw
        logger.info(
            "Queued EV restore command for %s (%s) using %s=%.2f kW",
            asset_id,
            description,
            restore_source,
            restore_power_kw,
        )
    else:
        logger.info("Queued restore command for %s (%s)", asset_id, description)

    command = {
        "asset_id": asset_id,
        "asset_type": asset_type,
        "command_type": "restore",
        "payload": payload,
        "priority": 5,
    }

    return command, 0.0


def _build_rabbitmq_command(
    asset_id: str,
    asset_config: dict,
    community: Optional[str],
    normalized_command: str,
    original_command: str,
    duration_minutes: int,
    logger: logging.Logger,
) -> tuple:
    """Translate direct actuator commands into manager-compatible RabbitMQ commands."""
    if normalized_command == "force_off":
        capacity_kw = float(asset_config.get("capacity_kw", 0) or 0)
        return _build_curtail_command(
            asset_id=asset_id,
            asset_config=asset_config,
            community=community,
            curtailment_kw=capacity_kw,
            duration_minutes=duration_minutes,
            original_command=original_command,
            logger=logger,
        )

    if normalized_command == "force_on":
        return _build_curtail_command(
            asset_id=asset_id,
            asset_config=asset_config,
            community=community,
            curtailment_kw=0.0,
            duration_minutes=duration_minutes,
            original_command=original_command,
            logger=logger,
        )

    if normalized_command == "restore":
        return _build_restore_command(
            asset_id=asset_id,
            asset_config=asset_config,
            community=community,
            original_command=original_command,
            duration_minutes=duration_minutes,
            logger=logger,
        )

    raise ValueError(f"Unsupported actuator command: {normalized_command}")


def _build_slot_info(
    fsp: str,
    command_map: Dict[str, str],
    duration_minutes: int,
    dry_run: bool,
    normalized_command: str,
    total_flexibility_kw: float,
) -> dict:
    """Build minimal batch metadata for RabbitMQ publishing."""
    slot_start = datetime.now(timezone.utc)
    slot_end = slot_start + timedelta(minutes=duration_minutes)

    return {
        "fsp_id": fsp,
        "slot_start": slot_start.isoformat(),
        "slot_end": slot_end.isoformat(),
        "duration_minutes": duration_minutes,
        "total_flexibility_kw": total_flexibility_kw,
        "allocation_strategy": "manual_actuation",
        "strategy_id": "",
        "dry_run": dry_run,
        "requested_command": normalized_command,
        "requested_payload": command_map,
    }


def _ceil_to_next_interval(dt: datetime, interval_minutes: int = DEFAULT_EV_INTERVAL_MINUTES) -> datetime:
    """Round to the next UTC interval boundary, never the current instant."""
    if interval_minutes <= 0:
        raise ValueError("interval_minutes must be a positive integer")

    if dt.tzinfo is None:
        dt_utc = dt.replace(tzinfo=timezone.utc)
    else:
        dt_utc = dt.astimezone(timezone.utc)

    base = dt_utc.replace(second=0, microsecond=0)
    minutes_to_add = interval_minutes - (base.minute % interval_minutes)
    if minutes_to_add == 0:
        minutes_to_add = interval_minutes
    return base + timedelta(minutes=minutes_to_add)


def _format_aem_utc(dt: datetime) -> str:
    """Format a datetime as a UTC naive ISO string with second precision."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


def _build_ev_schedule(
    slot_start: datetime,
    slot_end: datetime,
    power_kw: float,
    interval_minutes: int = DEFAULT_EV_INTERVAL_MINUTES,
) -> dict:
    """Build an EV power schedule using clean UTC timestamps."""
    if interval_minutes <= 0:
        raise ValueError("interval_minutes must be a positive integer")

    power_value = float(power_kw)
    schedule = {}
    current = slot_start

    while current < slot_end:
        schedule[_format_aem_utc(current)] = power_value
        current += timedelta(minutes=interval_minutes)

    if not schedule:
        raise ValueError(
            f"EV schedule is empty for start={_format_aem_utc(slot_start)} end={_format_aem_utc(slot_end)}"
        )

    return schedule


def _get_command_duration_minutes(payload: dict, slot_info: dict) -> int:
    """Resolve the command duration from payload or batch metadata."""
    duration_value = payload.get("duration_minutes")
    if duration_value is None:
        duration_value = slot_info.get("duration_minutes")

    duration_minutes = int(duration_value or 0)
    if duration_minutes <= 0:
        raise ValueError(f"Invalid duration_minutes for command: {duration_value!r}")

    return duration_minutes


def _add_ev_schedule_to_payload(
    command: dict,
    slot_start: datetime,
    slot_end: datetime,
    interval_minutes: int,
    logger: logging.Logger,
) -> None:
    """Inject a clean future EV schedule into the command payload."""
    payload = command.get("payload", {})
    asset_id = command.get("asset_id", "unknown")

    power_kw = payload.get("power_kw")
    if power_kw is None:
        power_kw = payload.get("target_power_kw")
    if power_kw is None:
        raise ValueError(f"EV command for {asset_id} is missing power_kw/target_power_kw")

    power_value = float(power_kw)
    payload["target_power_kw"] = power_value
    payload["power_kw"] = power_value
    payload["slot_start"] = _format_aem_utc(slot_start)
    payload["slot_end"] = _format_aem_utc(slot_end)
    payload["schedule"] = _build_ev_schedule(
        slot_start=slot_start,
        slot_end=slot_end,
        power_kw=power_value,
        interval_minutes=interval_minutes,
    )

    logger.info(
        "EV schedule for %s: power=%.2f kW, start=%s, end=%s, points=%d",
        asset_id,
        power_value,
        payload["slot_start"],
        payload["slot_end"],
        len(payload["schedule"]),
    )


def _prepare_commands_for_publish(
    commands: List[dict],
    slot_info: dict,
    dry_run: bool,
    ev_interval_minutes: int,
    logger: logging.Logger,
) -> None:
    """Apply runtime payload fields to all commands before publish or dry-run output."""
    ev_slot_start = None

    for command in commands:
        payload = command.setdefault("payload", {})
        payload["dry_run"] = dry_run

        if command.get("asset_type") == "ev_charger":
            if ev_slot_start is None:
                ev_slot_start = _ceil_to_next_interval(
                    datetime.now(timezone.utc),
                    interval_minutes=ev_interval_minutes,
                )
            duration_minutes = _get_command_duration_minutes(payload, slot_info)
            ev_slot_end = ev_slot_start + timedelta(minutes=duration_minutes)
            _add_ev_schedule_to_payload(
                command=command,
                slot_start=ev_slot_start,
                slot_end=ev_slot_end,
                interval_minutes=ev_interval_minutes,
                logger=logger,
            )
        else:
            payload["slot_start"] = slot_info.get("slot_start")
            payload["slot_end"] = slot_info.get("slot_end")


def _publish_commands(
    publisher: RabbitMQPublisher,
    commands: List[dict],
    slot_info: dict,
    dry_run: bool,
    logger: logging.Logger,
    verbose: bool = False,
) -> int:
    """Publish already-prepared commands to RabbitMQ.

    The batch_start header is intentionally not published for manual actuator
    commands.  It is only meaningful for scheduled manager batches.  The
    slot_info metadata is still logged for traceability but not sent to
    RabbitMQ, so consumers only receive actual command messages.
    """
    logger.info("Publishing %d commands to RabbitMQ (dry_run=%s)...", len(commands), dry_run)
    if verbose and slot_info:
        logger.info("Slot info (log-only, not published): %s", json.dumps(slot_info))
    return publisher.publish_batch_commands(commands, slot_info=None, verbose=verbose)


def main():
    parser = argparse.ArgumentParser(
        description="Flexibility Actuator - Publish one-shot flexibility commands to RabbitMQ",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python flexi_actuator.py --config_file ../conf/test_fm01_aem.json --fsp supsi01 --flexibilities ECM96.2 ECM97.1 --command force_off
  python flexi_actuator.py --config_file ../conf/test_fm01_aem.json --fsp supsi01 --flexibility ECM96.2 --flexibility ECM97.1 --command force_off
  python flexi_actuator.py --config_file ../conf/test_fm01_aem.json --fsp supsi01 --flexibilities ECM96.2 --command force_on --dry-run
  python flexi_actuator.py --config_file ../conf/test_fm01_aem.json --fsp supsi01 --flexibility ECM63.1 --command force_off --dry-run
  python flexi_actuator.py --config_file ../conf/test_fm01_aem.json --fsp supsi01 --flexibility ECM63.1 --command force_on --duration-minutes 30 --dry-run
  python flexi_actuator.py --config_file ../conf/test_fm01_aem.json --fsp supsi01 --flexibility ECM63.1 --command restore --dry-run
        """
    )

    parser.add_argument(
        "--config_file", "-c",
        default="../conf/test_fm01_aem.json",
        help="Path to configuration file (default: ../conf/test_fm01_aem.json)",
    )
    parser.add_argument(
        "--fsp", "-f",
        required=True,
        help="FSP identifier (e.g., supsi01)",
    )
    parser.add_argument(
        "--flexibility",
        action="append",
        help="Flexibility label to actuate. Repeat the option to pass multiple labels.",
    )
    parser.add_argument(
        "--flexibilities",
        nargs="+",
        help="Flexibility labels to actuate.",
    )
    parser.add_argument(
        "--command",
        required=True,
        help="Direct command to apply (supported: force_off, force_on, restore)",
    )
    parser.add_argument(
        "--duration-minutes",
        type=int,
        help="Command duration in minutes (default: config fm.granularity or 15)",
    )
    parser.add_argument(
        "--ev-interval-minutes",
        type=int,
        default=DEFAULT_EV_INTERVAL_MINUTES,
        help=f"EV schedule interval in minutes (default: {DEFAULT_EV_INTERVAL_MINUTES})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print/log the generated payload without publishing to RabbitMQ",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print RabbitMQ JSON message bodies sent, or that would be sent in dry-run mode",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Logging level (default: INFO)",
    )
    parser.add_argument(
        "--log_file",
        help="Path to log file. If provided, logs will be written to this file in addition to console.",
    )
    parser.add_argument(
        "--rabbitmq-host",
        default=None,
        help="RabbitMQ server hostname (default: localhost)",
    )
    parser.add_argument(
        "--rabbitmq-port",
        type=int,
        default=None,
        help="RabbitMQ server port (default: 5672)",
    )
    parser.add_argument(
        "--rabbitmq-user",
        default=None,
        help="RabbitMQ username (default: guest)",
    )
    parser.add_argument(
        "--rabbitmq-pass",
        default=None,
        help="RabbitMQ password (default: guest)",
    )
    parser.add_argument(
        "--rabbitmq-vhost",
        default=None,
        help="RabbitMQ virtual host (default: /)",
    )
    parser.add_argument(
        "--rabbitmq-exchange",
        default=None,
        help="Alias for --rabbit-exchange. Overrides the configured RabbitMQ destination exchange.",
    )
    parser.add_argument(
        "--rabbit-exchange",
        help="Override the configured RabbitMQ destination exchange.",
    )
    parser.add_argument(
        "--rabbit-queue",
        help="Override the configured RabbitMQ destination queue.",
    )
    parser.add_argument(
        "--rabbit-routing-key",
        help="Override the configured RabbitMQ destination routing key.",
    )

    args = parser.parse_args()

    logger = setup_logging(args.log_level, args.log_file)

    labels = _deduplicate_labels(_collect_flexibilities(args), logger)
    if not labels:
        logger.error("At least one flexibility label must be provided")
        sys.exit(1)

    normalized_command = _normalize_command(args.command, logger)

    config_path = _resolve_config_path(args.config_file)
    config = _load_config(config_path, logger)
    rabbitmq_config = _load_rabbitmq_config(config, config_path, logger)
    logger.info(
        "RabbitMQ destination sections available: %s",
        ", ".join(_available_rabbit_destination_sections(rabbitmq_config)) or "none",
    )
    rabbit_exchange_override = args.rabbit_exchange or args.rabbitmq_exchange
    if rabbit_exchange_override or args.rabbit_queue or args.rabbit_routing_key:
        logger.info(
            "RabbitMQ destination CLI overrides: exchange=%s, queue=%s, routing_key=%s",
            rabbit_exchange_override or "(config)",
            args.rabbit_queue or "(config)",
            args.rabbit_routing_key or "(config)",
        )

    fsps = config.get("fm", {}).get("actors", {}).get("fsps", {})
    if args.fsp not in fsps:
        logger.error("FSP '%s' not found in configuration", args.fsp)
        logger.error("Available FSPs: %s", sorted(fsps.keys()))
        sys.exit(1)

    labels = _validate_labels(labels, config, args.fsp, logger)
    command_map = {label: args.command.strip() for label in labels}

    duration_minutes = args.duration_minutes
    if duration_minutes is None:
        duration_minutes = int(config.get("fm", {}).get("granularity", 15) or 15)
    if duration_minutes <= 0:
        logger.error("Duration must be a positive integer")
        sys.exit(1)
    if args.ev_interval_minutes <= 0:
        logger.error("EV interval must be a positive integer")
        sys.exit(1)

    logger.info("Requested actuator payload: %s", json.dumps(command_map))

    community = config.get("fm", {}).get("community")
    asset_mapping = config.get("asset_mapping", {})
    commands = []
    total_flexibility_kw = 0.0

    for asset_id in labels:
        try:
            asset_config = asset_mapping[asset_id]
            command, actual_curtailment_kw = _build_rabbitmq_command(
                asset_id=asset_id,
                asset_config=asset_config,
                community=community,
                normalized_command=normalized_command,
                original_command=args.command.strip(),
                duration_minutes=duration_minutes,
                logger=logger,
            )
            destination = _resolve_rabbitmq_command_destination(
                asset_id=asset_id,
                asset_config=asset_config,
                rabbitmq_config=rabbitmq_config,
                logger=logger,
                override_exchange=rabbit_exchange_override,
                override_queue=args.rabbit_queue,
                override_routing_key=args.rabbit_routing_key,
            )
            if destination is None:
                sys.exit(1)
            command["rabbitmq_destination"] = destination
            logger.info(
                "RabbitMQ final destination for asset %s: exchange=%s, queue=%s, routing_key=%s",
                asset_id,
                destination["exchange"],
                destination["queue"],
                destination["routing_key"],
            )
            commands.append(command)
            total_flexibility_kw += actual_curtailment_kw
        except Exception as exc:
            logger.error("Failed to prepare command for %s: %s", asset_id, str(exc))
            sys.exit(1)

    slot_info = _build_slot_info(
        fsp=args.fsp,
        command_map=command_map,
        duration_minutes=duration_minutes,
        dry_run=args.dry_run,
        normalized_command=normalized_command,
        total_flexibility_kw=total_flexibility_kw,
    )

    _prepare_commands_for_publish(
        commands=commands,
        slot_info=slot_info,
        dry_run=args.dry_run,
        ev_interval_minutes=args.ev_interval_minutes,
        logger=logger,
    )

    rabbit_host = args.rabbitmq_host or rabbitmq_config.get("host") or "localhost"
    rabbit_port = args.rabbitmq_port
    if rabbit_port is None:
        rabbit_port = int(rabbitmq_config.get("port", 5672) or 5672)
    rabbit_user = args.rabbitmq_user or rabbitmq_config.get("username") or rabbitmq_config.get("user") or "guest"
    rabbit_password = args.rabbitmq_pass or rabbitmq_config.get("password") or "guest"
    rabbit_vhost = args.rabbitmq_vhost or rabbitmq_config.get("virtualHost") or rabbitmq_config.get("virtual_host") or "/"
    logger.info(
        "RabbitMQ connection: host=%s, port=%d, vhost=%s",
        rabbit_host,
        rabbit_port,
        rabbit_vhost,
    )

    if args.dry_run:
        if args.verbose:
            _log_rabbitmq_messages(
                commands=commands,
                slot_info=slot_info,
                logger=logger,
            )
        logger.info("[DRY-RUN] Skipping RabbitMQ publish")
        sys.exit(0)

    if not RABBITMQ_AVAILABLE:
        logger.error("pika library not installed. Run: pip install pika")
        sys.exit(1)

    publisher = RabbitMQPublisher(
        host=rabbit_host,
        port=rabbit_port,
        username=rabbit_user,
        password=rabbit_password,
        virtual_host=rabbit_vhost,
        logger=logger,
    )

    try:
        if not publisher.connect():
            logger.error("Could not connect to RabbitMQ")
            sys.exit(1)

        expected_count = len(commands)
        published_count = _publish_commands(
            publisher=publisher,
            commands=commands,
            slot_info=slot_info,
            dry_run=False,
            logger=logger,
            verbose=args.verbose,
        )

        if published_count != expected_count:
            logger.error(
                "RabbitMQ publish incomplete: published %d/%d commands",
                published_count,
                expected_count,
            )
            sys.exit(1)

        logger.info(
            "Successfully published actuator payload for FSP '%s' to RabbitMQ: %s",
            args.fsp,
            json.dumps(command_map, separators=(",", ":")),
        )
        sys.exit(0)

    except Exception as exc:
        logger.error("RabbitMQ publish failed: %s", str(exc))
        sys.exit(1)
    finally:
        publisher.disconnect()


if __name__ == "__main__":
    main()
