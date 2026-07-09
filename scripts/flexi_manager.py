#!/usr/bin/env python3
"""
Flexibility Manager (flexi_manager.py)

This script manages the activation of flexibility for an FSP by:
1. Querying market results for the current/upcoming time slot
2. Determining what flexibility was sold (accepted trades)
3. Calculating asset-level curtailment requirements
4. Sending control signals to assets (dry-run or live)

Usage:
    python flexi_manager.py --fsp supsi01 --dry-run
    python flexi_manager.py --fsp supsi01 --slot "2026-01-09T12:00:00"

Example timing:
    Run at ~11:59 to manage slot 12:00-12:15
"""

import os
import sys
import json
import argparse
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# RabbitMQ support (optional)
try:
    import pika
    RABBITMQ_AVAILABLE = True
except ImportError:
    RABBITMQ_AVAILABLE = False

# InfluxDB support (optional, needed for activation-current measurement)
try:
    from influxdb import InfluxDBClient
    INFLUXDB_AVAILABLE = True
except ImportError:
    INFLUXDB_AVAILABLE = False

from classes.nodes_interface import NODESInterface as NodesInterface
from classes.postgresql_interface import PostgreSQLInterface
from classes.bid_record_repository import BidRecordRepository
from classes.demand_record_repository import DemandRecordRepository
from classes.bidding_strategy import BiddingStrategy, StrategyManager
from classes.flexibility_forecaster import (
    _get_discrete_ev_states,
    _select_discrete_ev_target_for_curtailment,
)

# For statistics
import statistics


BID_ASSET_REFERENCE_POWER_FIELDS = (
    ("reference_power_kw", 1.0, "bid_record reference_power_kw"),
    ("reference_power_w", 0.001, "bid_record reference_power_w"),
    ("recent_profile_expected_power_w", 0.001, "bid_record recent_profile_expected_power_w"),
    ("expected_power_w", 0.001, "bid_record expected_power_w"),
    ("expected_power_kw", 1.0, "bid_record expected_power_kw"),
    ("baseline_power_w", 0.001, "bid_record baseline_power_w"),
    ("baseline_power_kw", 1.0, "bid_record baseline_power_kw"),
    ("typical_load_kw", 1.0, "bid_record typical_load_kw"),
)


def _coerce_optional_float(value) -> Optional[float]:
    """Return a float for numeric-like values, otherwise None."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_bid_asset_reference_power(asset: dict) -> Tuple[Optional[float], Optional[str]]:
    """Extract a bid-time baseline/reference power in kW from a bid asset row."""
    if not isinstance(asset, dict):
        return None, None

    for field_name, multiplier, source in BID_ASSET_REFERENCE_POWER_FIELDS:
        raw_value = asset.get(field_name)
        value = _coerce_optional_float(raw_value)
        if value is None:
            continue
        if not math.isfinite(value) or value <= 0:
            continue
        if field_name.startswith("reference_power"):
            source = asset.get("reference_power_source") or source
        return value * multiplier, source

    return None, None


def _build_bid_asset_reference_power_lookup(
    bid_record: Optional[Dict],
    logger: logging.Logger,
) -> Dict[str, Dict]:
    """Build asset_id -> reference power metadata from bid_record assets."""
    lookup = {}
    if not bid_record:
        return lookup

    for asset in bid_record.get("assets_to_activate", []):
        asset_id = asset.get("asset_id") if isinstance(asset, dict) else None
        if not asset_id:
            continue

        reference_power_kw, reference_power_source = _extract_bid_asset_reference_power(asset)
        if reference_power_kw is None:
            continue

        lookup[asset_id] = {
            "reference_power_kw": reference_power_kw,
            "reference_power_source": reference_power_source,
        }

    if lookup:
        logger.info(
            "Loaded bid-record reference power for activation target calculation: %s",
            {
                asset_id: {
                    "reference_power_kw": round(info["reference_power_kw"], 6),
                    "source": info["reference_power_source"],
                }
                for asset_id, info in lookup.items()
            },
        )

    return lookup


ACTIVATION_CURRENT_DEFAULTS = {
    "max_measurement_age_minutes": 15,
    "active_threshold_w": 4000,
}


EV_COMFORT_GUARD_DEFAULTS = {
    # Maximum number of consecutive 15-minute slots a strategy_10 recent-profile
    # EV may stay under control before a mandatory cooldown is enforced.
    "max_consecutive_activation_slots": 4,
    # Number of slots the EV is released (cooldown) once the maximum consecutive
    # activation cap is reached.
    "cooldown_slots_after_max_activation": 2,
}


class ActivationMeasurementProvider:
    """Minimal provider that queries latest power measurement for an asset at activation time.

    Reuses the same InfluxDB query pattern as FlexibilityForecaster._get_latest_grouped_measurement
    but without requiring the full forecaster instance.
    """

    def __init__(self, influx_client, asset_mapping: dict, config: dict, logger: logging.Logger):
        self.influx_client = influx_client
        self.asset_mapping = asset_mapping
        self.logger = logger
        self.assets_measurement = config.get("influxDB", {}).get(
            "assetsMeasurement", "assets_data"
        )
        self.granularity = config.get("fm", {}).get("granularity", 15)

    def get_latest_power(
        self,
        asset_id: str,
        current_time_utc: datetime,
        max_age_minutes: int,
    ) -> Dict:
        """Query latest grouped measurement for an asset.

        Returns dict with keys: timestamp_utc, power_w, age_minutes, valid.
        """
        mapping = self.asset_mapping.get(asset_id, {})
        if not isinstance(mapping, dict):
            return {"valid": False, "reason": "asset_mapping entry not a dict"}

        device_name = mapping.get("device_name_tag")
        if not device_name:
            return {"valid": False, "reason": "missing device_name_tag"}

        field = mapping.get("field", "active_power")
        site = mapping.get("pod", asset_id.split(".")[0])

        window_start_utc = current_time_utc - timedelta(minutes=max_age_minutes)
        query = (
            f"SELECT MEAN({field}) as mean_power FROM {self.assets_measurement} "
            f"WHERE time >= '{window_start_utc.strftime('%Y-%m-%dT%H:%M:%SZ')}' "
            f"AND time < '{current_time_utc.strftime('%Y-%m-%dT%H:%M:%SZ')}' "
            f"AND site='{site}' AND device_name='{device_name}' "
            f"GROUP BY time({self.granularity}m) fill(none)"
        )

        try:
            result = self.influx_client.query(query)
        except Exception as exc:
            self.logger.warning(
                "Activation measurement query failed for %s: %s", asset_id, exc
            )
            return {"valid": False, "reason": f"query_error: {exc}"}

        import pandas as pd

        rows = {}
        for series in result.raw.get("series", []):
            columns = series.get("columns", [])
            values = series.get("values", [])
            if "time" not in columns or "mean_power" not in columns:
                continue
            time_idx = columns.index("time")
            value_idx = columns.index("mean_power")
            for row in values:
                ts_raw = row[time_idx]
                val_raw = row[value_idx]
                if ts_raw is None or val_raw is None:
                    continue
                ts = pd.to_datetime(ts_raw, utc=True, errors="coerce")
                if pd.isna(ts):
                    continue
                rows[ts] = float(val_raw)

        if not rows:
            return {"valid": False, "reason": "no_data_in_window"}

        latest_ts = max(rows.keys())
        latest_power_w = rows[latest_ts]
        age_minutes = (
            current_time_utc - latest_ts.to_pydatetime().replace(tzinfo=None)
        ).total_seconds() / 60.0

        if not math.isfinite(latest_power_w):
            return {"valid": False, "reason": "non_finite_power_value"}

        return {
            "valid": True,
            "timestamp_utc": latest_ts,
            "power_w": latest_power_w,
            "age_minutes": age_minutes,
        }


def _resolve_activation_current_config(strategy_obj, config: dict) -> Dict:
    """Resolve max_measurement_age_minutes and active_threshold_w for activation-current logic.

    Looks in strategy.recentProfileSettings first, then flexibility.persistenceSettings,
    then uses ACTIVATION_CURRENT_DEFAULTS.
    """
    strategy_config = strategy_obj.config if strategy_obj else {}
    rps = strategy_config.get("recentProfileSettings", {})
    persistence_cfg = config.get("flexibility", {}).get("persistenceSettings", {})

    def _first_not_none(*values, default):
        # Explicit None check so an explicit 0 is honored instead of being
        # treated as "unset" by truthiness (e.g. activeThresholdW: 0.0).
        for value in values:
            if value is not None:
                return value
        return default

    max_age = _first_not_none(
        rps.get("maxCurrentMeasurementAgeMinutes"),
        persistence_cfg.get("maxCurrentMeasurementAgeMinutes"),
        default=ACTIVATION_CURRENT_DEFAULTS["max_measurement_age_minutes"],
    )
    active_threshold_w = float(_first_not_none(
        rps.get("activeThresholdW"),
        persistence_cfg.get("activeThresholdW"),
        default=ACTIVATION_CURRENT_DEFAULTS["active_threshold_w"],
    ))

    return {
        "max_measurement_age_minutes": int(max_age),
        "active_threshold_w": active_threshold_w,
    }


def _resolve_ev_comfort_guard_config(strategy_obj, config: dict) -> Dict:
    """Resolve the strategy_10 recent-profile EV comfort guard configuration.

    Resolution priority for each setting:
        1. strategy.recentProfileSettings.<key>
        2. flexibility.persistenceSettings.<key>
        3. EV_COMFORT_GUARD_DEFAULTS

    ``maxConsecutiveActivationSlots`` must be a positive integer (>= 1).
    ``cooldownSlotsAfterMaxActivation`` must be a non-negative integer (>= 0).
    Invalid or missing values fall back to the defaults.
    """
    strategy_config = strategy_obj.config if strategy_obj else {}
    rps = strategy_config.get("recentProfileSettings", {}) or {}
    persistence_cfg = config.get("flexibility", {}).get("persistenceSettings", {}) or {}

    def _resolve_int(key: str, default: int, minimum: int) -> int:
        for source in (rps, persistence_cfg):
            if not isinstance(source, dict):
                continue
            if key in source and source.get(key) is not None:
                try:
                    value = int(source.get(key))
                except (TypeError, ValueError):
                    continue
                if value >= minimum:
                    return value
        return default

    max_slots = _resolve_int(
        "maxConsecutiveActivationSlots",
        EV_COMFORT_GUARD_DEFAULTS["max_consecutive_activation_slots"],
        minimum=1,
    )
    cooldown_slots = _resolve_int(
        "cooldownSlotsAfterMaxActivation",
        EV_COMFORT_GUARD_DEFAULTS["cooldown_slots_after_max_activation"],
        minimum=0,
    )

    return {
        "max_consecutive_activation_slots": max_slots,
        "cooldown_slots_after_max_activation": cooldown_slots,
    }


ALLOWED_RABBIT_DESTINATION_SECTIONS = (
    "realAssetCommands",
    "simulatedAssetCommands",
    "simulatedAssetMeasures",
)
ALLOWED_RABBIT_COMMAND_SECTIONS = ("realAssetCommands", "simulatedAssetCommands")
RABBIT_COMMAND_DESTINATION_FIELDS = ("exchange", "queue", "routingKey")


def _available_rabbit_destination_sections(rabbitmq_cfg: dict) -> List[str]:
    """Return configured RabbitMQ sections that may provide destinations."""
    return [
        section_name
        for section_name in ALLOWED_RABBIT_DESTINATION_SECTIONS
        if isinstance(rabbitmq_cfg.get(section_name), dict)
    ]


def _resolve_rabbitmq_command_destination(
    asset_id: str,
    asset_mapping: dict,
    rabbitmq_cfg: dict,
    logger: logging.Logger,
) -> Optional[dict]:
    """Resolve the RabbitMQ command destination for one asset command.

    Assets must explicitly set ``rabbitCommandSection``.  Missing or invalid
    configuration means the command is skipped rather than routed through any
    legacy fallback.
    """
    asset_config = asset_mapping.get(asset_id, {})
    if not isinstance(asset_config, dict):
        logger.warning(
            "Skipping RabbitMQ command for asset %s: asset_mapping entry is missing or invalid",
            asset_id,
        )
        return None

    section_name = asset_config.get("rabbitCommandSection")
    if not section_name:
        logger.warning(
            "Skipping RabbitMQ command for asset %s: missing asset_mapping.rabbitCommandSection",
            asset_id,
        )
        return None

    if section_name not in ALLOWED_RABBIT_COMMAND_SECTIONS:
        logger.warning(
            "Skipping RabbitMQ command for asset %s: invalid asset_mapping.rabbitCommandSection '%s' "
            "(allowed: %s)",
            asset_id,
            section_name,
            ", ".join(ALLOWED_RABBIT_COMMAND_SECTIONS),
        )
        return None

    section = rabbitmq_cfg.get(section_name)
    if not isinstance(section, dict):
        logger.warning(
            "Skipping RabbitMQ command for asset %s: rabbitMQ.%s section not found",
            asset_id,
            section_name,
        )
        return None

    missing_fields = [
        field_name
        for field_name in RABBIT_COMMAND_DESTINATION_FIELDS
        if not section.get(field_name)
    ]
    if missing_fields:
        logger.warning(
            "Skipping RabbitMQ command for asset %s: rabbitMQ.%s missing required field(s): %s",
            asset_id,
            section_name,
            ", ".join(missing_fields),
        )
        return None

    return {
        "section": section_name,
        "exchange": section["exchange"],
        "queue": section["queue"],
        "routing_key": section["routingKey"],
    }


# =============================================================================
# RABBITMQ PUBLISHER
# =============================================================================

class RabbitMQPublisher:
    """
    Publishes control commands and measurements to RabbitMQ for forwarding.
    
    This enables decoupling of command generation (flexi_manager) from
    command actuation (forwarder), allowing for better scalability and
    reliable message delivery.
    
    Messages are published only to explicit destinations resolved from
    RabbitMQ destination sections such as realAssetCommands,
    simulatedAssetCommands, or simulatedAssetMeasures.
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 5672,
        username: str = "guest",
        password: str = "guest",
        virtual_host: str = "/",
        logger: logging.Logger = None
    ):
        """
        Initialize RabbitMQ publisher.
        
        :param host: RabbitMQ server hostname
        :param port: RabbitMQ server port
        :param username: RabbitMQ username
        :param password: RabbitMQ password
        :param virtual_host: RabbitMQ virtual host
        :param logger: Logger instance
        """
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
        """
        Establish connection to RabbitMQ server.
        
        :return: True if connected successfully
        """
        try:
            credentials = pika.PlainCredentials(self.username, self.password)
            parameters = pika.ConnectionParameters(
                host=self.host,
                port=self.port,
                virtual_host=self.virtual_host,
                credentials=credentials,
                heartbeat=600,
                blocked_connection_timeout=300
            )
            
            self.connection = pika.BlockingConnection(parameters)
            self.channel = self.connection.channel()
            
            self._connected = True
            self.logger.info(
                "Connected to RabbitMQ at %s:%d (vhost: %s)",
                self.host, self.port, self.virtual_host
            )
            return True
            
        except Exception as e:
            self.logger.error("Failed to connect to RabbitMQ: %s", str(e))
            self._connected = False
            return False
    
    def disconnect(self):
        """Close RabbitMQ connection."""
        if self.connection and self.connection.is_open:
            try:
                self.connection.close()
                self.logger.info("Disconnected from RabbitMQ")
            except Exception as e:
                self.logger.warning("Error closing RabbitMQ connection: %s", str(e))
        self._connected = False
    
    def is_connected(self) -> bool:
        """Check if connected to RabbitMQ."""
        return self._connected and self.connection and self.connection.is_open

    def _declare_destination(
        self,
        exchange: str,
        queue: str,
        routing_key: str,
    ) -> None:
        """Declare and bind a RabbitMQ destination once per connection."""
        destination_key = (exchange, queue, routing_key)
        if destination_key in self._declared_destinations:
            return

        self.channel.exchange_declare(
            exchange=exchange,
            exchange_type='topic',
            durable=True
        )
        self.channel.queue_declare(
            queue=queue,
            durable=True
        )
        self.channel.queue_bind(
            exchange=exchange,
            queue=queue,
            routing_key=routing_key
        )
        self._declared_destinations.add(destination_key)

    def _publish_batch_header(
        self,
        slot_info: dict,
        command_count: int,
        exchange: str,
        queue: str,
        routing_key: str,
    ) -> None:
        """Publish a batch header to a concrete command destination."""
        batch_header = {
            "message_type": "batch_start",
            "slot_info": slot_info,
            "command_count": command_count,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        self._declare_destination(exchange, queue, routing_key)
        self.channel.basic_publish(
            exchange=exchange,
            routing_key=routing_key,
            body=json.dumps(batch_header),
            properties=pika.BasicProperties(
                delivery_mode=2,
                content_type='application/json'
            )
        )
    
    def publish_command(
        self,
        asset_id: str,
        asset_type: str,
        command_type: str,
        payload: dict,
        priority: int = 5,
        exchange: str = None,
        queue: str = None,
        routing_key: str = None,
    ) -> bool:
        """
        Publish a control command to RabbitMQ.
        
        :param asset_id: Asset identifier
        :param asset_type: Asset type (heat_pump, ev_charger, etc.)
        :param command_type: Command type (curtail, restore, set_power, etc.)
        :param payload: Command payload dictionary
        :param priority: Message priority (0-9, higher = more urgent)
        :param exchange: Destination exchange from a RabbitMQ destination section.
        :param queue: Destination queue from a RabbitMQ destination section.
        :param routing_key: Destination routing key from a RabbitMQ destination section.
        :return: True if published successfully
        """
        if not self.is_connected():
            self.logger.warning("Not connected to RabbitMQ - cannot publish command")
            return False

        if not exchange or not queue or not routing_key:
            self.logger.warning(
                "Missing RabbitMQ destination for command asset %s - cannot publish",
                asset_id,
            )
            return False
        
        destination_exchange = exchange
        destination_queue = queue
        destination_routing_key = routing_key
        
        message = {
            "message_type": "command",
            "asset_id": asset_id,
            "asset_type": asset_type,
            "command_type": command_type,
            "payload": payload,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "priority": priority
        }

        # self.logger.info("RabbitMQ command JSON: %s", json.dumps(message))

        try:
            self._declare_destination(
                destination_exchange,
                destination_queue,
                destination_routing_key,
            )
            self.channel.basic_publish(
                exchange=destination_exchange,
                routing_key=destination_routing_key,
                body=json.dumps(message),
                properties=pika.BasicProperties(
                    delivery_mode=2,  # Persistent message
                    content_type='application/json',
                    priority=priority
                )
            )
            self.logger.debug(
                "Published command to exchange=%s queue=%s routing_key=%s: %s -> %s",
                destination_exchange,
                destination_queue,
                destination_routing_key,
                command_type,
                asset_id
            )
            return True
            
        except Exception as e:
            self.logger.error("Failed to publish command: %s", str(e))
            return False
    
    def publish_measurement(
        self,
        asset_id: str,
        asset_type: str,
        measurement_type: str,
        payload: dict,
        exchange: str = None,
        queue: str = None,
        routing_key: str = None,
    ) -> bool:
        """
        Publish a measurement to RabbitMQ.
        
        :param asset_id: Asset identifier
        :param asset_type: Asset type
        :param measurement_type: Measurement type (power, energy, status, etc.)
        :param payload: Measurement payload dictionary
        :return: True if published successfully
        """
        if not self.is_connected():
            self.logger.warning("Not connected to RabbitMQ - cannot publish measurement")
            return False

        if not exchange or not queue or not routing_key:
            self.logger.warning(
                "Missing RabbitMQ destination for measurement asset %s - cannot publish",
                asset_id,
            )
            return False
        
        message = {
            "message_type": "measurement",
            "asset_id": asset_id,
            "asset_type": asset_type,
            "measurement_type": measurement_type,
            "payload": payload,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }

        self.logger.info("RabbitMQ measurement JSON: %s", json.dumps(message))

        try:
            self._declare_destination(
                exchange,
                queue,
                routing_key,
            )
            self.channel.basic_publish(
                exchange=exchange,
                routing_key=routing_key,
                body=json.dumps(message),
                properties=pika.BasicProperties(
                    delivery_mode=2,
                    content_type='application/json'
                )
            )
            self.logger.debug(
                "Published measurement to %s: %s -> %s",
                routing_key, measurement_type, asset_id
            )
            return True
            
        except Exception as e:
            self.logger.error("Failed to publish measurement: %s", str(e))
            return False
    
    def publish_batch_commands(
        self,
        commands: List[dict],
        slot_info: dict = None
    ) -> int:
        """
        Publish multiple commands as a batch.
        
        :param commands: List of command dictionaries
        :param slot_info: Optional slot information to include
        :return: Number of successfully published commands
        """
        if not self.is_connected():
            self.logger.warning("Not connected to RabbitMQ - cannot publish batch")
            return 0
        
        success_count = 0
        
        # Optionally publish a batch header per resolved destination so each
        # queue receives metadata for the commands it will consume.
        if slot_info:
            destination_counts = {}
            for cmd in commands:
                destination = cmd.get("rabbitmq_destination") or {}
                if destination:
                    destination_key = (
                        destination.get("exchange"),
                        destination.get("queue"),
                        destination.get("routing_key"),
                    )
                    destination_counts[destination_key] = destination_counts.get(destination_key, 0) + 1
                else:
                    self.logger.warning(
                        "Skipping RabbitMQ batch header for command without destination: asset=%s",
                        cmd.get("asset_id"),
                    )

            try:
                for (exchange, queue, routing_key), command_count in destination_counts.items():
                    self._publish_batch_header(
                        slot_info=slot_info,
                        command_count=command_count,
                        exchange=exchange,
                        queue=queue,
                        routing_key=routing_key,
                    )
            except Exception as e:
                self.logger.warning("Failed to publish batch header: %s", str(e))
        
        for cmd in commands:
            destination = cmd.get("rabbitmq_destination") or {}
            if self.publish_command(
                asset_id=cmd.get("asset_id"),
                asset_type=cmd.get("asset_type"),
                command_type=cmd.get("command_type"),
                payload=cmd.get("payload", {}),
                priority=cmd.get("priority", 5),
                exchange=destination.get("exchange"),
                queue=destination.get("queue"),
                routing_key=destination.get("routing_key"),
            ):
                success_count += 1
        
        self.logger.info(
            "Published %d/%d commands to RabbitMQ",
            success_count, len(commands)
        )
        return success_count


# =============================================================================
# LOGGING SETUP
# =============================================================================

def setup_logging(log_level: str = "INFO", log_file: Optional[str] = None) -> logging.Logger:
    """Configure logging with timestamp and level.

    :param log_level: Logging level (DEBUG, INFO, WARNING, ERROR)
    :param log_file: Optional path to log file. If provided, logs will be written to this file.
    :return: Configured logger instance
    """
    logger = logging.getLogger("flexi_manager")
    logger.setLevel(getattr(logging, log_level.upper()))
    
    formatter = logging.Formatter(
        "%(asctime)s::%(levelname)s::%(funcName)s::%(message)s"
    )

    # Always add console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # Optionally add file handler
    if log_file:
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger


def parse_time_offset(offset_str: str) -> timedelta:
    """
    Parse a time offset string like '30m', '2h', '1h30m' into a timedelta.
    
    Supported formats:
    - '30m' or '30M' -> 30 minutes
    - '2h' or '2H' -> 2 hours
    - '1h30m' -> 1 hour 30 minutes
    - '90' -> 90 minutes (default unit is minutes)
    
    :param offset_str: Time offset string
    :return: timedelta object
    :raises ValueError: If format is invalid
    """
    import re
    
    offset_str = offset_str.strip().lower()
    
    # Try to parse combined format like "1h30m"
    combined_pattern = r'^(?:(\d+)h)?(?:(\d+)m)?$'
    match = re.match(combined_pattern, offset_str)
    
    if match and (match.group(1) or match.group(2)):
        hours = int(match.group(1)) if match.group(1) else 0
        minutes = int(match.group(2)) if match.group(2) else 0
        return timedelta(hours=hours, minutes=minutes)
    
    # Try pure number (assume minutes)
    if offset_str.isdigit():
        return timedelta(minutes=int(offset_str))
    
    raise ValueError(
        f"Invalid offset format: '{offset_str}'. "
        f"Use formats like '30m', '2h', '1h30m', or just '30' for minutes."
    )


def calculate_slot_from_offset(offset: timedelta) -> str:
    """
    Calculate the slot start time based on an offset from now.
    
    The slot is aligned to the 15-minute boundary containing (now - offset).
    
    :param offset: Time offset from now
    :return: ISO format slot start time string
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    target_time = now - offset
    
    # Align to 15-minute boundary (floor)
    minutes = (target_time.minute // 15) * 15
    slot_start = target_time.replace(minute=minutes, second=0, microsecond=0)
    
    return slot_start.strftime("%Y-%m-%dT%H:%M:%S")
    
    return logger


# =============================================================================
# ASSET CONTROLLER
# =============================================================================

class AssetController:
    """
    Controls flexible assets to deliver sold flexibility.
    
    Supports multiple control interfaces:
    - MQTT: For IoT devices
    - HTTP API: For smart devices with REST APIs
    - Modbus: For industrial equipment
    - RabbitMQ: For decoupled command forwarding via message broker
    - Simulation: For testing (logs only)
    
    Modulation-aware control:
    - Discrete assets (e.g., heat pumps): Sends ON/OFF state commands
    - Continuous assets (e.g., EV chargers): Sends power setpoint commands
    """
    
    def __init__(
        self, 
        asset_mapping: dict, 
        logger: logging.Logger,
        rabbitmq_publisher: 'RabbitMQPublisher' = None,
        community: str = None,
        rabbitmq_config: dict = None,
    ):
        self.asset_mapping = asset_mapping
        self.logger = logger
        self.control_results = {}
        self.rabbitmq_publisher = rabbitmq_publisher
        self.community = community
        self.rabbitmq_config = rabbitmq_config or {}
        self._pending_commands = []  # Buffer for batch publishing
    
    def _get_modulation_type(self, asset_config: dict) -> str:
        """
        Get modulation type for an asset, with fallback to defaults by asset type.
        
        :param asset_config: Asset configuration dictionary
        :return: 'continuous' or 'discrete'
        """
        # Explicit configuration takes precedence
        if "modulation_type" in asset_config:
            return asset_config["modulation_type"]
        
        # Fallback to default by asset type
        asset_type = asset_config.get("type", "")
        defaults = MODULATION_DEFAULTS.get(asset_type, {})
        return defaults.get("modulation_type", "continuous")
    
    def _determine_discrete_state(
        self, 
        asset_config: dict, 
        curtailment_kw: float
    ) -> tuple:
        """
        Determine the target discrete state (ON/OFF) based on curtailment request.
        
        For ON/OFF assets, if curtailment is requested (> threshold), turn OFF.
        Otherwise, keep ON.
        
        :param asset_config: Asset configuration dictionary
        :param curtailment_kw: Requested curtailment in kW
        :return: Tuple of (state_name, target_power_kw)
        """
        capacity = asset_config.get("capacity_kw", 0)
        
        # Get discrete states
        discrete_states = asset_config.get("discrete_states_kw", [0.0, capacity])
        
        # Threshold: if curtailment >= 50% of capacity, switch OFF
        # This can be configured per asset if needed
        threshold_pct = asset_config.get("curtailment_threshold_pct", 50.0)
        threshold_kw = capacity * (threshold_pct / 100.0)
        
        if curtailment_kw >= threshold_kw:
            # Turn OFF (use minimum state)
            target_power = min(discrete_states)
            state_name = "OFF"
        else:
            # Keep ON (use maximum state)
            target_power = max(discrete_states)
            state_name = "ON"
        
        return state_name, target_power

    def _calculate_continuous_target_power(
        self,
        asset_id: str,
        modulation_type: str,
        nominal_power_kw: float,
        min_power_kw: float,
        allocated_curtailment_kw: float,
        reference_power_kw: Optional[float] = None,
        reference_power_source: Optional[str] = None,
        activation_current_power_kw: Optional[float] = None,
    ) -> Dict:
        """Translate accepted continuous curtailment into an absolute target power.

        When activation_current_power_kw is provided (for recent_profile_baseline assets),
        it overrides the bid-time reference_power_kw as the activation reference.
        """
        reference_power_kw = _coerce_optional_float(reference_power_kw)
        if (
            reference_power_kw is None
            or not math.isfinite(reference_power_kw)
            or reference_power_kw <= 0
        ):
            self.logger.error(
                "Continuous target calculation skipped for %s:\n"
                "  allocated_curtailment=%.5f kW\n"
                "  reference_power=%s\n"
                "  nominal_power=%.5f kW\n"
                "  reason=missing reference power; nominal fallback disabled",
                asset_id,
                allocated_curtailment_kw,
                reference_power_kw,
                nominal_power_kw,
            )
            return {
                "reference_power_kw": reference_power_kw,
                "reference_power_source": reference_power_source,
                "allocated_curtailment_kw": allocated_curtailment_kw,
                "computed_target_power_kw": None,
                "uncapped_target_power_kw": None,
                "formula_used": None,
                "skip_activation": True,
                "skip_reason": "missing reference power; nominal fallback disabled",
            }

        selected_reference_kw = reference_power_kw
        activation_current_power_kw = _coerce_optional_float(activation_current_power_kw)
        if activation_current_power_kw is not None and math.isfinite(activation_current_power_kw):
            selected_reference_kw = activation_current_power_kw

        formula_used = (
            "target_power=max(min_power, selected_reference - curtailment), "
            "capped_to_nominal"
        )

        uncapped_target_kw = max(
            min_power_kw,
            selected_reference_kw - allocated_curtailment_kw,
        )
        computed_target_power_kw = min(uncapped_target_kw, nominal_power_kw)

        self.logger.info(
            "Continuous target calculation for %s:\n"
            "  modulation_type=%s\n"
            "  reference_power=%.5f kW\n"
            "  reference_power_source=%s\n"
            "  activation_current_power_kw=%s\n"
            "  selected_activation_reference_kw=%.5f\n"
            "  allocated_curtailment=%.5f kW\n"
            "  nominal_power=%.5f kW\n"
            "  min_power=%.5f kW\n"
            "  formula=max(min_power, selected_reference - curtailment)\n"
            "  target_power=%.5f kW\n"
            "  formula_used=%s",
            asset_id,
            modulation_type,
            reference_power_kw,
            reference_power_source,
            ("%.5f" % activation_current_power_kw) if activation_current_power_kw is not None else "N/A",
            selected_reference_kw,
            allocated_curtailment_kw,
            nominal_power_kw,
            min_power_kw,
            computed_target_power_kw,
            formula_used,
        )

        return {
            "reference_power_kw": reference_power_kw,
            "reference_power_source": reference_power_source,
            "activation_current_power_kw": activation_current_power_kw,
            "selected_activation_reference_kw": selected_reference_kw,
            "allocated_curtailment_kw": allocated_curtailment_kw,
            "computed_target_power_kw": computed_target_power_kw,
            "uncapped_target_power_kw": uncapped_target_kw,
            "formula_used": formula_used,
            "skip_activation": False,
            "skip_reason": None,
        }

    def _calculate_discrete_ev_target_power(
        self,
        asset_id: str,
        asset_config: dict,
        allocated_curtailment_kw: float,
        reference_power_kw: Optional[float] = None,
        reference_power_source: Optional[str] = None,
        activation_current_power_kw: Optional[float] = None,
        strategy_cfg: Optional[dict] = None,
    ) -> Tuple[float, float]:
        """Select a valid discrete OCPP power state for an EV charger.

        Uses the overdelivery-tolerant policy: pick the smallest feasible
        curtailment >= requested; fall back to maximum below requested.

        Returns (target_power_kw, actual_curtailment_kw).
        """
        capacity_kw = float(asset_config.get("capacity_kw", 0) or 0)

        ref_kw = _coerce_optional_float(reference_power_kw)
        act_kw = _coerce_optional_float(activation_current_power_kw)
        if act_kw is not None and math.isfinite(act_kw) and act_kw > 0:
            selected_ref = act_kw
        elif ref_kw is not None and math.isfinite(ref_kw) and ref_kw > 0:
            selected_ref = ref_kw
        else:
            selected_ref = capacity_kw

        valid_states = _get_discrete_ev_states(asset_config, strategy_cfg)

        selection = _select_discrete_ev_target_for_curtailment(
            reference_power_kw=selected_ref,
            allocated_or_desired_curtailment_kw=allocated_curtailment_kw,
            states_kw=valid_states,
        )

        if selection is not None:
            target = selection["target_power_kw"]
            actual = selection["actual_curtailment_kw"]
        else:
            target = max(valid_states) if valid_states else capacity_kw
            actual = max(selected_ref - target, 0.0)

        self.logger.info(
            "Discrete EV activation for %s:\n"
            "  reference_power=%.3f kW (source=%s)\n"
            "  activation_current=%.3f kW\n"
            "  selected_reference=%.3f kW\n"
            "  allocated_curtailment=%.3f kW\n"
            "  selected_target=%.3f kW\n"
            "  actual_curtailment=%.3f kW\n"
            "  valid_states=%s",
            asset_id,
            ref_kw or 0.0, reference_power_source,
            act_kw or 0.0,
            selected_ref,
            allocated_curtailment_kw,
            target, actual,
            valid_states,
        )

        return target, actual

    def curtail_asset(
        self, 
        asset_id: str, 
        curtailment_kw: float, 
        duration_minutes: int = 15,
        dry_run: bool = True,
        force_discrete_off: bool = False,
        reference_power_kw: Optional[float] = None,
        reference_power_source: Optional[str] = None,
        activation_current_power_kw: Optional[float] = None,
        strategy_cfg: Optional[Dict] = None,
    ) -> Dict:
        """
        Send curtailment command to an asset.
        
        For discrete assets: Sends ON/OFF commands based on curtailment threshold.
        For continuous assets: Sends specific power reduction values.
        
        :param asset_id: Asset identifier (e.g., "ECM97.1")
        :param curtailment_kw: Amount of power to reduce (kW)
        :param duration_minutes: Duration of curtailment
        :param dry_run: If True, only log what would be done
        :param force_discrete_off: If True, positive discrete curtailment forces OFF
        :param activation_current_power_kw: Latest measured power at activation time (kW).
            Used as activation reference for recent_profile_baseline continuous assets.
        :param strategy_cfg: Strategy configuration dict for discrete EV state filtering.
        :return: Result dictionary with status and details
        """
        asset_config = self.asset_mapping.get(asset_id, {})
        asset_type = asset_config.get("type", "unknown")
        description = asset_config.get("description", asset_id)
        capacity_kw = float(asset_config.get("capacity_kw", 0) or 0)
        min_power_kw = float(asset_config.get("min_power_kw", 0.0) or 0.0)
        modulation_type = self._get_modulation_type(asset_config)
        
        # Calculate curtailment percentage
        curtailment_pct = (curtailment_kw / capacity_kw * 100) if capacity_kw > 0 else 0
        
        # For discrete assets, determine the actual state to set
        if modulation_type == "discrete":
            is_discrete_ev = (
                asset_type == "ev_charger"
                and len(asset_config.get("discrete_states_kw", [])) >= 2
            )
            if is_discrete_ev:
                target_power_kw, actual_curtailment_kw = (
                    self._calculate_discrete_ev_target_power(
                        asset_id=asset_id,
                        asset_config=asset_config,
                        allocated_curtailment_kw=curtailment_kw,
                        reference_power_kw=reference_power_kw,
                        reference_power_source=reference_power_source,
                        activation_current_power_kw=activation_current_power_kw,
                        strategy_cfg=strategy_cfg,
                    )
                )
                state_name = f"LIMIT_{target_power_kw:.2f}kW"
            elif force_discrete_off and curtailment_kw > 0:
                discrete_states = asset_config.get("discrete_states_kw", [0.0, capacity_kw])
                target_power_kw = min(discrete_states)
                state_name = "OFF"
            else:
                state_name, target_power_kw = self._determine_discrete_state(asset_config, curtailment_kw)
            if not is_discrete_ev:
                actual_curtailment_kw = capacity_kw - target_power_kw
        else:
            state_name = None
            target_calculation = self._calculate_continuous_target_power(
                asset_id=asset_id,
                modulation_type=modulation_type,
                nominal_power_kw=capacity_kw,
                min_power_kw=min_power_kw,
                allocated_curtailment_kw=curtailment_kw,
                reference_power_kw=reference_power_kw,
                reference_power_source=reference_power_source,
                activation_current_power_kw=activation_current_power_kw,
            )
            if target_calculation["skip_activation"]:
                result = {
                    "asset_id": asset_id,
                    "description": description,
                    "asset_type": asset_type,
                    "modulation_type": modulation_type,
                    "requested_curtailment_kw": curtailment_kw,
                    "actual_curtailment_kw": 0.0,
                    "target_power_kw": None,
                    "curtailment_pct": curtailment_pct,
                    "duration_minutes": duration_minutes,
                    "dry_run": dry_run,
                    "status": "skipped",
                    "message": target_calculation["skip_reason"],
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    **target_calculation,
                }
                self.logger.error(
                    "Continuous activation skipped for %s:\n"
                    "  allocated_curtailment=%.5f kW\n"
                    "  reference_power missing/invalid\n"
                    "  nominal fallback disabled",
                    asset_id,
                    curtailment_kw,
                )
                self.control_results[asset_id] = result
                return result
            target_power_kw = target_calculation["computed_target_power_kw"]
            actual_curtailment_kw = curtailment_kw
        
        result = {
            "asset_id": asset_id,
            "description": description,
            "asset_type": asset_type,
            "modulation_type": modulation_type,
            "requested_curtailment_kw": curtailment_kw,
            "actual_curtailment_kw": actual_curtailment_kw,
            "target_power_kw": target_power_kw,
            "curtailment_pct": curtailment_pct,
            "duration_minutes": duration_minutes,
            "dry_run": dry_run,
            "status": "pending",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        
        if modulation_type == "discrete":
            result["discrete_state"] = state_name
        else:
            result.update(target_calculation)
        
        # Build command payload for RabbitMQ
        # Get site_id (pod) from asset config
        site_id = asset_config.get("pod", "")

        command_payload = {
            "community": self.community,
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
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        
        if modulation_type == "discrete":
            command_payload["discrete_state"] = state_name
        else:
            command_payload.update({
                "reference_power_kw": target_calculation["reference_power_kw"],
                "reference_power_source": target_calculation["reference_power_source"],
                "allocated_curtailment_kw": target_calculation["allocated_curtailment_kw"],
                "computed_target_power_kw": target_calculation["computed_target_power_kw"],
                "target_formula_used": target_calculation["formula_used"],
            })

        if asset_type == "ev_charger":
            command_payload["power_kw"] = float(target_power_kw)

        # Queue command for RabbitMQ (always, regardless of dry_run)
        # Note: slot_start/slot_end, dry_run, and EV schedule will be added
        # by publish_pending_commands via _prepare_command_payload_for_slot
        if self.rabbitmq_publisher:
            self._pending_commands.append({
                "asset_id": asset_id,
                "asset_type": asset_type,
                "command_type": "curtail",
                "payload": command_payload,
                "priority": 7  # High priority for curtailment commands
            })
            result["rabbitmq_queued"] = True
            
            # When RabbitMQ is enabled, actuation is delegated to forwarder
            if modulation_type == "discrete":
                self.logger.info(
                    "Queued command: set %s (%s) to %s (target: %.2f kW) for %d minutes",
                    asset_id, description, state_name, target_power_kw, duration_minutes
                )
            else:
                self.logger.info(
                    "Queued command: curtail %s (%s): %.2f kW (target: %.2f kW) for %d minutes",
                    asset_id, description, curtailment_kw, target_power_kw, duration_minutes
                )
            result["status"] = "queued"
            result["message"] = "Command queued for RabbitMQ - actuation delegated to forwarder"
        elif dry_run:
            # Local dry-run (no RabbitMQ)
            if modulation_type == "discrete":
                self.logger.info(
                    "[LOCAL DRY-RUN] Would set %s (%s) to %s (target: %.2f kW) for %d minutes",
                    asset_id, description, state_name, target_power_kw, duration_minutes
                )
            else:
                self.logger.info(
                    "[LOCAL DRY-RUN] Would curtail %s (%s): %.2f kW (target: %.2f kW) for %d minutes",
                    asset_id, description, curtailment_kw, target_power_kw, duration_minutes
                )
            result["status"] = "simulated"
            result["message"] = "Local dry-run mode - no actual command sent"
        else:
            # Actual control logic
            try:
                if asset_type == "heat_pump":
                    self._control_heat_pump(
                        asset_id,
                        asset_config,
                        curtailment_kw,
                        duration_minutes,
                        force_discrete_off=force_discrete_off,
                        reference_power_kw=reference_power_kw,
                        reference_power_source=reference_power_source,
                    )
                elif asset_type == "ev_charger":
                    self._control_ev_charger(
                        asset_id,
                        asset_config,
                        curtailment_kw,
                        duration_minutes,
                        reference_power_kw=reference_power_kw,
                        reference_power_source=reference_power_source,
                    )
                else:
                    self.logger.warning("Unknown asset type: %s", asset_type)
                    result["status"] = "error"
                    result["message"] = f"Unknown asset type: {asset_type}"
                    return result
                
                result["status"] = "success"
                result["message"] = "Control command sent"
                if modulation_type == "discrete":
                    self.logger.info(
                        "Set %s (%s) to %s for %d minutes",
                        asset_id, description, state_name, duration_minutes
                    )
                else:
                    self.logger.info(
                        "Curtailed %s (%s): %.2f kW for %d minutes",
                        asset_id, description, curtailment_kw, duration_minutes
                    )
            except Exception as e:
                result["status"] = "error"
                result["message"] = str(e)
                self.logger.error("Failed to curtail %s: %s", asset_id, str(e))
        
        self.control_results[asset_id] = result
        return result
    
    def _control_heat_pump(
        self, 
        asset_id: str, 
        config: dict, 
        curtailment_kw: float,
        duration_minutes: int,
        force_discrete_off: bool = False,
        reference_power_kw: Optional[float] = None,
        reference_power_source: Optional[str] = None,
    ):
        """
        Send control command to a heat pump.
        
        For discrete (ON/OFF) heat pumps: Sends state command (ON/OFF).
        For modulating heat pumps (rare): Sends power setpoint.
        
        Control methods (configured per asset):
        - mqtt: Publish to MQTT topic
        - http: POST to device API
        - modbus: Write to Modbus register
        """
        control_cfg = config.get("control", {})
        control_type = control_cfg.get("type", "simulation")
        modulation_type = self._get_modulation_type(config)
        capacity_kw = float(config.get("capacity_kw", 0) or 0)
        
        # Determine command based on modulation type
        if modulation_type == "discrete":
            if force_discrete_off and curtailment_kw > 0:
                discrete_states = config.get("discrete_states_kw", [0.0, capacity_kw])
                target_power_kw = min(discrete_states)
                state_name = "OFF"
            else:
                state_name, target_power_kw = self._determine_discrete_state(config, curtailment_kw)
            command_payload = {
                "command": "set_state",
                "state": state_name,
                "target_power_kw": target_power_kw,
                "duration_minutes": duration_minutes,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
            log_msg = f"HP {asset_id}: set state to {state_name} (target: {target_power_kw:.2f} kW) for {duration_minutes} min"
        else:
            # Continuous modulation (rare for HPs, but supported)
            min_power_kw = float(config.get("min_power_kw", 0.0) or 0.0)
            target_calculation = self._calculate_continuous_target_power(
                asset_id=asset_id,
                modulation_type=modulation_type,
                nominal_power_kw=capacity_kw,
                min_power_kw=min_power_kw,
                allocated_curtailment_kw=curtailment_kw,
                reference_power_kw=reference_power_kw,
                reference_power_source=reference_power_source,
            )
            if target_calculation["skip_activation"]:
                raise ValueError(target_calculation["skip_reason"])
            target_power_kw = target_calculation["computed_target_power_kw"]
            command_payload = {
                "command": "set_power",
                "target_power_kw": target_power_kw,
                "reduction_kw": curtailment_kw,
                "duration_minutes": duration_minutes,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
            command_payload.update({
                "reference_power_kw": target_calculation["reference_power_kw"],
                "reference_power_source": target_calculation["reference_power_source"],
                "allocated_curtailment_kw": target_calculation["allocated_curtailment_kw"],
                "computed_target_power_kw": target_calculation["computed_target_power_kw"],
                "target_formula_used": target_calculation["formula_used"],
            })
            log_msg = f"HP {asset_id}: set power to {target_power_kw:.2f} kW (reduce by {curtailment_kw:.2f} kW) for {duration_minutes} min"
        
        if control_type == "mqtt":
            topic = control_cfg.get("topic", f"assets/{asset_id}/control")
            self.logger.info("MQTT publish to %s: %s", topic, json.dumps(command_payload))
            # TODO: Implement actual MQTT publish
            # mqtt_client.publish(topic, json.dumps(command_payload))
            
        elif control_type == "http":
            endpoint = control_cfg.get("endpoint", "")
            self.logger.info("HTTP POST to %s: %s", endpoint, json.dumps(command_payload))
            # TODO: Implement actual HTTP request
            # requests.post(endpoint, json=command_payload)
            
        elif control_type == "simulation":
            self.logger.info("[SIMULATION] %s", log_msg)
        else:
            raise ValueError(f"Unknown control type: {control_type}")
    
    def _control_ev_charger(
        self, 
        asset_id: str, 
        config: dict, 
        curtailment_kw: float,
        duration_minutes: int,
        reference_power_kw: Optional[float] = None,
        reference_power_source: Optional[str] = None,
    ):
        """
        Send control command to an EV charger.
        
        EV chargers support continuous modulation (linear 0 to max power).
        Control commands set a specific charging power limit.
        
        Control methods:
        - ocpp: OCPP SetChargingProfile command
        - http: REST API call to charger
        - simulation: Log only (for testing)
        """
        control_cfg = config.get("control", {})
        control_type = control_cfg.get("type", "simulation")
        capacity_kw = float(config.get("capacity_kw", 11.0) or 0)
        min_power_kw = float(config.get("min_power_kw", 0.0) or 0.0)
        modulation_type = self._get_modulation_type(config)
        
        target_calculation = self._calculate_continuous_target_power(
            asset_id=asset_id,
            modulation_type=modulation_type,
            nominal_power_kw=capacity_kw,
            min_power_kw=min_power_kw,
            allocated_curtailment_kw=curtailment_kw,
            reference_power_kw=reference_power_kw,
            reference_power_source=reference_power_source,
        )
        if target_calculation["skip_activation"]:
            raise ValueError(target_calculation["skip_reason"])
        target_power_kw = target_calculation["computed_target_power_kw"]
        
        # Build command payload
        command_payload = {
            "command": "set_charging_limit",
            "target_power_kw": target_power_kw,
            "max_power_kw": capacity_kw,
            "reduction_kw": curtailment_kw,
            "duration_minutes": duration_minutes,
            "modulation_type": modulation_type,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        command_payload.update({
            "reference_power_kw": target_calculation["reference_power_kw"],
            "reference_power_source": target_calculation["reference_power_source"],
            "allocated_curtailment_kw": target_calculation["allocated_curtailment_kw"],
            "computed_target_power_kw": target_calculation["computed_target_power_kw"],
            "target_formula_used": target_calculation["formula_used"],
        })
        
        log_msg = f"EV {asset_id}: set charging limit to {target_power_kw:.2f} kW (reduce by {curtailment_kw:.2f} kW) for {duration_minutes} min"
        
        if control_type == "ocpp":
            # OCPP (Open Charge Point Protocol) for EV chargers
            charger_id = control_cfg.get("charger_id", asset_id)
            self.logger.info(
                "OCPP SetChargingProfile for %s: limit=%.2f kW, duration=%d min",
                charger_id, target_power_kw, duration_minutes
            )
            # TODO: Implement OCPP command
            # ocpp_client.set_charging_profile(charger_id, target_power_kw, duration_minutes)
            
        elif control_type == "http":
            endpoint = control_cfg.get("endpoint", "")
            self.logger.info("HTTP POST to %s: %s", endpoint, json.dumps(command_payload))
            # TODO: Implement HTTP request
            # requests.post(endpoint, json=command_payload)
            
        elif control_type == "simulation":
            self.logger.info("[SIMULATION] %s", log_msg)
        else:
            raise ValueError(f"Unknown control type: {control_type}")
    
    def restore_asset(self, asset_id: str, dry_run: bool = True) -> Dict:
        """
        Restore asset to normal operation after flexibility activation.

        For heat pumps the desired state is ON at full capacity.
        For EV chargers the restore power is chosen from
        ``restore_power_kw`` > ``default_power_kw`` > ``capacity_kw``.

        :param asset_id: Asset identifier
        :param dry_run: If True, only log what would be done
        :return: Result dictionary
        """
        asset_config = self.asset_mapping.get(asset_id, {})
        description = asset_config.get("description", asset_id)
        asset_type = asset_config.get("type", "unknown")
        site_id = asset_config.get("pod", "")
        capacity_kw = float(asset_config.get("capacity_kw", 0) or 0)

        restore_payload = {
            "community": self.community,
            "site_id": site_id,
            "asset_id": asset_id,
            "description": description,
            "asset_type": asset_type,
            "action": "restore",
            "capacity_kw": capacity_kw,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        modulation_type = self._get_modulation_type(asset_config)

        if modulation_type == "discrete":
            restore_payload["target_state"] = "ON"
            restore_payload["discrete_state"] = "ON"
            restore_payload["target_power_kw"] = capacity_kw
            restore_payload["modulation_type"] = "discrete"

        if asset_type == "ev_charger":
            if asset_config.get("restore_power_kw") is not None:
                restore_power_kw = float(asset_config["restore_power_kw"])
            elif asset_config.get("default_power_kw") is not None:
                restore_power_kw = float(asset_config["default_power_kw"])
            else:
                restore_power_kw = capacity_kw
            restore_payload["target_power_kw"] = restore_power_kw
            restore_payload["power_kw"] = restore_power_kw

        # Queue restore command for RabbitMQ
        if self.rabbitmq_publisher:
            self._pending_commands.append({
                "asset_id": asset_id,
                "asset_type": asset_type,
                "command_type": "restore",
                "payload": restore_payload,
                "priority": 5,
            })

        if dry_run:
            self.logger.info("[DRY-RUN] Would restore %s (%s) to normal operation", asset_id, description)
            return {"asset_id": asset_id, "status": "simulated", "action": "restore"}
        else:
            self.logger.info("Restoring %s (%s) to normal operation", asset_id, description)
            return {"asset_id": asset_id, "status": "success", "action": "restore"}
    
    def publish_pending_commands(self, slot_info: dict = None, dry_run: bool = True) -> int:
        """
        Publish all pending commands to RabbitMQ.

        Each command payload is enriched with clean ``slot_start`` /
        ``slot_end`` timestamps (no timezone suffix, no microseconds) and
        the ``dry_run`` flag.  EV charger commands also receive a
        ``schedule`` dictionary.

        :param slot_info: Optional slot information for batch header
        :param dry_run: Whether forwarder should operate in dry-run mode
        :return: Number of successfully published commands
        """
        if not self.rabbitmq_publisher:
            self.logger.debug("No RabbitMQ publisher configured - skipping publish")
            return 0

        if not self._pending_commands:
            self.logger.debug("No pending commands to publish")
            return 0

        self.logger.info(
            "Publishing %d commands to RabbitMQ (dry_run=%s)...",
            len(self._pending_commands), dry_run,
        )

        # Parse market slot boundaries from slot_info
        slot_start_dt = None
        slot_end_dt = None
        if slot_info:
            raw_start = slot_info.get("slot_start")
            raw_end = slot_info.get("slot_end")
            if raw_start is not None:
                slot_start_dt = _parse_slot_datetime(raw_start)
            if raw_end is not None:
                slot_end_dt = _parse_slot_datetime(raw_end)

        # Warn if the slot start is already in the past
        if slot_start_dt is not None:
            now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
            if slot_start_dt <= now_utc:
                self.logger.warning(
                    "slot_start %s is not in the future (now=%s). "
                    "AEM controls require future timestamps. "
                    "NOT shifting the slot to avoid breaking the market-cleared interval.",
                    _format_aem_utc(slot_start_dt),
                    now_utc.strftime("%Y-%m-%dT%H:%M:%S"),
                )

        publishable_commands = []

        # Enrich each command payload with timing, dry_run, EV schedule, and
        # explicit asset-level RabbitMQ destination.
        for cmd in self._pending_commands:
            if slot_start_dt is not None and slot_end_dt is not None:
                _prepare_command_payload_for_slot(
                    command=cmd,
                    slot_start=slot_start_dt,
                    slot_end=slot_end_dt,
                    dry_run=dry_run,
                )
            else:
                cmd.setdefault("payload", {})["dry_run"] = dry_run

            # Log prepared command
            payload = cmd.get("payload", {})
            asset_id = cmd.get("asset_id", "?")
            asset_type = cmd.get("asset_type", "?")
            cmd_type = cmd.get("command_type", "?")

            if asset_type == "ev_charger":
                self.logger.info(
                    "Prepared EV schedule for %s: power=%.2f kW, "
                    "slot=%s -> %s, points=%d, dry_run=%s",
                    asset_id,
                    payload.get("target_power_kw", 0.0),
                    payload.get("slot_start", "?"),
                    payload.get("slot_end", "?"),
                    len(payload.get("schedule", {})),
                    dry_run,
                )
            elif asset_type == "heat_pump":
                state = payload.get("discrete_state", payload.get("target_state", "?"))
                self.logger.info(
                    "Prepared HP command for %s: state=%s, "
                    "slot=%s -> %s, dry_run=%s",
                    asset_id,
                    state,
                    payload.get("slot_start", "?"),
                    payload.get("slot_end", "?"),
                    dry_run,
                )
            else:
                self.logger.info(
                    "Prepared %s command for %s: slot=%s -> %s, dry_run=%s",
                    cmd_type, asset_id,
                    payload.get("slot_start", "?"),
                    payload.get("slot_end", "?"),
                    dry_run,
                )

            destination = _resolve_rabbitmq_command_destination(
                asset_id=asset_id,
                asset_mapping=self.asset_mapping,
                rabbitmq_cfg=self.rabbitmq_config,
                logger=self.logger,
            )
            if destination is None:
                continue

            cmd["rabbitmq_destination"] = destination
            self.logger.info(
                "Publishing command for asset %s via section %s: exchange=%s, queue=%s, routing_key=%s",
                asset_id,
                destination["section"],
                destination["exchange"],
                destination["queue"],
                destination["routing_key"],
            )
            publishable_commands.append(cmd)

        if not publishable_commands:
            self.logger.warning(
                "No pending RabbitMQ commands were published because no commands resolved to a valid destination"
            )
            self._pending_commands = []
            return 0

        published = self.rabbitmq_publisher.publish_batch_commands(
            publishable_commands,
            slot_info=slot_info,
        )

        # Clear pending commands after publishing
        self._pending_commands = []

        return published
    
    def get_pending_commands(self) -> List[dict]:
        """Get list of pending commands (for inspection/logging)."""
        return self._pending_commands.copy()
    
    def clear_pending_commands(self):
        """Clear pending commands without publishing."""
        self._pending_commands = []


# =============================================================================
# MARKET RESULTS HANDLER
# =============================================================================

class MarketResultsHandler:
    """
    Handles querying and processing market results from NODES platform.
    """
    
    def __init__(self, nodes_interface: NodesInterface, logger: logging.Logger):
        self.nodes = nodes_interface
        self.logger = logger

    @staticmethod
    def _extract_response_items(response) -> List[Dict]:
        """Return list-like payloads from common NODES response envelopes."""
        if isinstance(response, list):
            return response
        if isinstance(response, dict):
            for key in ("items", "data", "results", "value"):
                value = response.get(key)
                if isinstance(value, list):
                    return value
            return [response]
        return []

    @staticmethod
    def _parse_nodes_datetime(value) -> Optional[datetime]:
        if isinstance(value, datetime):
            dt = value
        elif isinstance(value, str) and value:
            try:
                dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
        else:
            return None

        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt.replace(second=0, microsecond=0)

    @staticmethod
    def _to_float(value) -> Optional[float]:
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _get_first_value(data: Dict, keys: Tuple[str, ...]):
        for key in keys:
            if key in data and data.get(key) not in (None, ""):
                return data.get(key)
        return None

    def _trade_matches_slot(self, trade: Dict, slot_start: datetime, slot_end: datetime) -> bool:
        start_value = self._get_first_value(
            trade,
            ("periodFrom", "period_from", "startTime", "start", "deliveryStart", "validFrom"),
        )
        end_value = self._get_first_value(
            trade,
            ("periodTo", "period_to", "endTime", "end", "deliveryEnd", "validTo"),
        )
        trade_start = self._parse_nodes_datetime(start_value)
        trade_end = self._parse_nodes_datetime(end_value)

        expected_start = self._parse_nodes_datetime(slot_start)
        expected_end = self._parse_nodes_datetime(slot_end)
        if trade_start is not None and expected_start is not None and trade_start != expected_start:
            return False
        if trade_end is not None and expected_end is not None and trade_end != expected_end:
            return False
        return True

    def _trade_matches_organization(self, trade: Dict, organization_id: str) -> bool:
        organization_candidates = set()
        for key in (
            "organizationId",
            "ownerOrganizationId",
            "sellerOrganizationId",
            "fspOrganizationId",
            "participantOrganizationId",
        ):
            value = trade.get(key)
            if value not in (None, ""):
                organization_candidates.add(str(value))

        for key in ("organization", "ownerOrganization", "sellerOrganization", "seller"):
            value = trade.get(key)
            if isinstance(value, dict):
                nested_id = self._get_first_value(value, ("id", "organizationId"))
                if nested_id not in (None, ""):
                    organization_candidates.add(str(nested_id))

        if not organization_candidates:
            return True
        return str(organization_id) in organization_candidates

    def _trade_is_accepted(self, trade: Dict) -> bool:
        status_value = self._get_first_value(
            trade,
            ("status", "tradeStatus", "state", "completionType"),
        )
        if status_value in (None, ""):
            return True

        normalized = str(status_value).replace("_", "").replace(" ", "").lower()
        accepted_values = {
            "accepted",
            "cleared",
            "completed",
            "executed",
            "filled",
            "partiallyfilled",
            "settled",
        }
        return normalized in accepted_values

    def _normalise_trade_for_activation(self, trade: Dict) -> Optional[Dict]:
        quantity_value = self._get_first_value(
            trade,
            (
                "quantity",
                "quantityMw",
                "quantityMW",
                "quantityCompleted",
                "filledQuantity",
                "matchedQuantity",
                "volume",
            ),
        )
        quantity = self._to_float(quantity_value)
        if quantity is None or quantity <= 0:
            return None

        normalised = dict(trade)
        normalised["quantity"] = quantity

        if normalised.get("price") in (None, ""):
            price = self._get_first_value(
                normalised,
                ("unitPrice", "clearingPrice", "matchedPrice", "averagePrice"),
            )
            if price not in (None, ""):
                normalised["price"] = price

        return normalised
    
    def get_accepted_trades_for_slot(
        self, 
        organization_id: str,
        slot_start: datetime,
        slot_end: datetime
    ) -> List[Dict]:
        """
        Query NODES for accepted trades in the specified time slot.
        
        :param organization_id: FSP organization ID
        :param slot_start: Start of time slot
        :param slot_end: End of time slot
        :return: List of accepted trade dictionaries
        """
        self.logger.info(
            "Querying accepted trades for slot %s - %s",
            slot_start.strftime("%Y-%m-%d %H:%M"),
            slot_end.strftime("%H:%M")
        )
        
        # Query trades from NODES. The trades endpoint follows the same
        # filter syntax used by market_results_fetcher.py; acceptance and
        # ownership are applied locally because server-side status and
        # organization filters have returned HTTP 400 for this endpoint.
        try:
            # Format times for API
            period_from = slot_start.strftime("%Y-%m-%dT%H:%M:%SZ")
            period_to = slot_end.strftime("%Y-%m-%dT%H:%M:%SZ")
            
            filter_params = [
                f"periodFrom.GreaterThanOrEqual={period_from}",
                f"periodTo.LessThanOrEqual={period_to}",
            ]
            filter_str = "&".join(filter_params)

            endpoint = (
                f"{self.nodes.cfg['mainEndpoint']}trades"
                f"?{filter_str}"
            )

            self.logger.info("Fetching NODES trades from %s to %s", period_from, period_to)
            
            response = self.nodes.get_request(endpoint)
            trades = self._extract_response_items(response) if response else []
            self.logger.info("NODES trades returned %d raw rows for this slot", len(trades))

            accepted_sell_trades = []
            for trade in trades:
                if not isinstance(trade, dict):
                    self.logger.debug("Skipping non-dict trade row: %r", trade)
                    continue

                side = str(trade.get("side", "")).lower()
                if side != "sell":
                    self.logger.debug(
                        "Skipping trade %s: side is %r",
                        trade.get("id", trade.get("tradeId", "unknown")),
                        trade.get("side"),
                    )
                    continue

                if not self._trade_matches_organization(trade, organization_id):
                    self.logger.debug(
                        "Skipping trade %s: does not match organization %s",
                        trade.get("id", trade.get("tradeId", "unknown")),
                        organization_id,
                    )
                    continue

                if not self._trade_matches_slot(trade, slot_start, slot_end):
                    self.logger.debug(
                        "Skipping trade %s: outside requested slot",
                        trade.get("id", trade.get("tradeId", "unknown")),
                    )
                    continue

                if not self._trade_is_accepted(trade):
                    self.logger.debug(
                        "Skipping trade %s: status/completion is not accepted",
                        trade.get("id", trade.get("tradeId", "unknown")),
                    )
                    continue

                normalised_trade = self._normalise_trade_for_activation(trade)
                if normalised_trade is None:
                    self.logger.info(
                        "Skipping trade %s: missing or non-positive quantity",
                        trade.get("id", trade.get("tradeId", "unknown")),
                    )
                    continue

                accepted_sell_trades.append(normalised_trade)

            self.logger.info(
                "Kept %d accepted sell trades for organization %s and requested slot",
                len(accepted_sell_trades),
                organization_id,
            )
            if not accepted_sell_trades:
                self.logger.info("No trades found for this slot")
            return accepted_sell_trades
                
        except Exception as e:
            self.logger.error("Error querying trades: %s", str(e))
            return []
    
    def get_settlements_for_slot(
        self,
        organization_id: str,
        slot_start: datetime,
        slot_end: datetime
    ) -> List[Dict]:
        """
        Query NODES for settlements (activated flexibility) in the specified time slot.
        
        Settlements indicate that the DSO has requested activation of flexibility.
        """
        self.logger.info(
            "Querying settlements for slot %s - %s",
            slot_start.strftime("%Y-%m-%d %H:%M"),
            slot_end.strftime("%H:%M")
        )
        
        try:
            period_from = slot_start.strftime("%Y-%m-%dT%H:%M:%SZ")
            period_to = slot_end.strftime("%Y-%m-%dT%H:%M:%SZ")
            
            # Build endpoint with query params
            endpoint = (
                f"{self.nodes.cfg['mainEndpoint']}settlements"
                f"?organizationId={organization_id}"
                f"&periodFrom={period_from}"
                f"&periodTo={period_to}"
            )
            
            response = self.nodes.get_request(endpoint)
            
            # Handle paginated response
            if response:
                if isinstance(response, dict):
                    settlements = response.get("items", [])
                elif isinstance(response, list):
                    settlements = response
                else:
                    settlements = []
                
                self.logger.info("Found %d settlements for this slot", len(settlements))
                return settlements
            else:
                return []
                
        except Exception as e:
            self.logger.error("Error querying settlements: %s", str(e))
            return []


# =============================================================================
# FLEXIBILITY ALLOCATOR
# =============================================================================

# Default modulation types by asset type (used when not explicitly configured)
MODULATION_DEFAULTS = {
    "heat_pump": {"modulation_type": "discrete", "discrete_states_kw": [0.0, 1.0]},  # 0 or 100% of capacity
    "ev_charger": {"modulation_type": "continuous", "min_power_kw": 0.0},
}

DEFAULT_EV_INTERVAL_MINUTES = 15


def _parse_slot_datetime(value) -> datetime:
    """Parse a slot datetime from a string or datetime, returning a naive UTC datetime
    with seconds and microseconds zeroed out."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise TypeError(f"Expected str or datetime, got {type(value).__name__}")
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.replace(second=0, microsecond=0)


def _format_aem_utc(dt: datetime) -> str:
    """Format a datetime as a naive UTC ISO string with second precision.

    Converts timezone-aware values to UTC and strips tzinfo.
    Example output: ``2026-04-27T12:15:00``
    """
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.isoformat(timespec="seconds")


def _parse_aem_utc(value) -> Optional[datetime]:
    """Parse a naive UTC ISO string (as produced by ``_format_aem_utc``).

    Returns a naive UTC ``datetime`` or ``None`` if the value is missing or
    cannot be parsed.
    """
    if not value:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _build_ev_schedule(
    slot_start: datetime,
    slot_end: datetime,
    power_kw: float,
    interval_minutes: int = DEFAULT_EV_INTERVAL_MINUTES,
) -> dict:
    """Build an EV power schedule dict keyed by clean UTC timestamp strings.

    One entry every *interval_minutes* from *slot_start* up to (but not
    including) *slot_end*.  Raises ``ValueError`` if the resulting schedule
    would be empty.
    """
    if interval_minutes <= 0:
        raise ValueError("interval_minutes must be a positive integer")

    power_value = float(power_kw)
    schedule: dict = {}
    current = slot_start

    while current < slot_end:
        schedule[_format_aem_utc(current)] = power_value
        current += timedelta(minutes=interval_minutes)

    if not schedule:
        raise ValueError(
            f"EV schedule is empty for start={_format_aem_utc(slot_start)} "
            f"end={_format_aem_utc(slot_end)}"
        )
    return schedule


def _prepare_command_payload_for_slot(
    command: dict,
    slot_start: datetime,
    slot_end: datetime,
    dry_run: bool,
    ev_interval_minutes: int = DEFAULT_EV_INTERVAL_MINUTES,
) -> None:
    """Enrich a pending command's payload with slot timing and dry-run flag.

    For EV charger commands the method also builds and injects the
    ``schedule`` dictionary.  Operates **in-place** on *command*.
    """
    payload = command.setdefault("payload", {})
    payload["dry_run"] = dry_run
    payload["slot_start"] = _format_aem_utc(slot_start)
    payload["slot_end"] = _format_aem_utc(slot_end)

    if command.get("asset_type") == "ev_charger":
        power_kw = payload.get("power_kw")
        if power_kw is None:
            power_kw = payload.get("target_power_kw")
        if power_kw is not None:
            power_value = float(power_kw)
            payload["target_power_kw"] = power_value
            payload["power_kw"] = power_value
            payload["schedule"] = _build_ev_schedule(
                slot_start=slot_start,
                slot_end=slot_end,
                power_kw=power_value,
                interval_minutes=ev_interval_minutes,
            )


class FlexibilityAllocator:
    """
    Allocates sold flexibility across available assets.
    
    Allocation strategies:
    - modulation_aware: Smart allocation respecting discrete (ON/OFF) vs continuous assets (RECOMMENDED)
    - proportional: Distribute based on asset capacity (legacy, ignores modulation constraints)
    - priority: Fill high-flexibility assets first (legacy)
    - cost_optimal: Minimize activation costs (legacy)
    
    Modulation types:
    - continuous: Asset can modulate power linearly (e.g., EV chargers 0-11 kW)
    - discrete: Asset can only switch between fixed states (e.g., HP ON/OFF)
    """
    
    def __init__(self, asset_mapping: dict, logger: logging.Logger):
        self.asset_mapping = asset_mapping
        self.logger = logger
    
    def _get_modulation_type(self, asset_config: dict) -> str:
        """
        Get modulation type for an asset, with fallback to defaults by asset type.
        
        :param asset_config: Asset configuration dictionary
        :return: 'continuous' or 'discrete'
        """
        # Explicit configuration takes precedence
        if "modulation_type" in asset_config:
            return asset_config["modulation_type"]
        
        # Fallback to default by asset type
        asset_type = asset_config.get("type", "")
        defaults = MODULATION_DEFAULTS.get(asset_type, {})
        return defaults.get("modulation_type", "continuous")
    
    def _get_discrete_states(self, asset_config: dict) -> List[float]:
        """
        Get discrete power states for an asset.
        
        :param asset_config: Asset configuration dictionary
        :return: List of valid power states in kW (e.g., [0.0, 15.0] for ON/OFF)
        """
        # Explicit configuration
        if "discrete_states_kw" in asset_config:
            return asset_config["discrete_states_kw"]
        
        # Generate from capacity (simple ON/OFF)
        capacity = asset_config.get("capacity_kw", 0)
        return [0.0, capacity]
    
    def allocate_flexibility(
        self,
        total_flexibility_kw: float,
        allowed_assets: List[str] = None,
        strategy: str = "modulation_aware"
    ) -> Dict[str, float]:
        """
        Allocate total flexibility requirement across assets.
        
        :param total_flexibility_kw: Total flexibility to deliver (kW)
        :param allowed_assets: List of asset IDs to use (None = all)
        :param strategy: Allocation strategy (default: modulation_aware)
        :return: Dictionary mapping asset_id to curtailment_kw
        """
        self.logger.info(
            "Allocating %.2f kW flexibility using '%s' strategy",
            total_flexibility_kw, strategy
        )
        
        # Filter assets
        if allowed_assets:
            assets = {k: v for k, v in self.asset_mapping.items() if k in allowed_assets}
        else:
            assets = self.asset_mapping
        
        if not assets:
            self.logger.warning("No assets available for allocation")
            return {}
        
        if strategy == "modulation_aware":
            return self._allocate_modulation_aware(total_flexibility_kw, assets)
        elif strategy == "proportional":
            return self._allocate_proportional(total_flexibility_kw, assets)
        elif strategy == "priority":
            return self._allocate_priority(total_flexibility_kw, assets)
        elif strategy == "cost_optimal":
            return self._allocate_cost_optimal(total_flexibility_kw, assets)
        else:
            self.logger.warning("Unknown strategy '%s', using modulation_aware", strategy)
            return self._allocate_modulation_aware(total_flexibility_kw, assets)
    
    def _allocate_proportional(
        self, 
        total_kw: float, 
        assets: Dict
    ) -> Dict[str, float]:
        """
        Allocate proportionally based on asset capacity × flexibility factor.
        """
        allocations = {}
        
        # Calculate total available flexibility
        total_available = sum(
            a.get("capacity_kw", 0) * a.get("flexibility_factor", 0.5)
            for a in assets.values()
        )
        
        if total_available <= 0:
            return {}
        
        # Allocate proportionally
        remaining = total_kw
        for asset_id, config in assets.items():
            capacity = config.get("capacity_kw", 0)
            flex_factor = config.get("flexibility_factor", 0.5)
            max_flex = capacity * flex_factor
            
            # Proportional share
            share = max_flex / total_available
            allocation = min(share * total_kw, max_flex, remaining)
            
            if allocation > 0:
                allocations[asset_id] = round(allocation, 3)
                remaining -= allocation
        
        self.logger.info("Proportional allocation: %s", allocations)
        return allocations
    
    def _allocate_modulation_aware(
        self, 
        total_kw: float, 
        assets: Dict
    ) -> Dict[str, float]:
        """
        Smart allocation that respects modulation constraints.
        
        Strategy:
        1. Separate assets into continuous and discrete
        2. For discrete assets: use subset-sum to find best combination of ON/OFF states
        3. For continuous assets: use proportional allocation for remaining flexibility
        4. Combine both allocations
        
        This ensures we don't ask an ON/OFF device to "reduce by 7.5 kW" when
        it can only do 0 or 15 kW.
        
        :param total_kw: Target flexibility to deliver (kW)
        :param assets: Dictionary of asset configurations
        :return: Dictionary mapping asset_id to curtailment_kw
        """
        allocations = {}
        
        # Separate assets by modulation type
        continuous_assets = {}
        discrete_assets = {}
        
        for asset_id, config in assets.items():
            if not isinstance(config, dict):
                continue
            mod_type = self._get_modulation_type(config)
            if mod_type == "continuous":
                continuous_assets[asset_id] = config
            else:
                discrete_assets[asset_id] = config
        
        self.logger.info(
            "Modulation-aware allocation: %d continuous, %d discrete assets",
            len(continuous_assets), len(discrete_assets)
        )
        
        remaining = total_kw
        
        # Phase 1: Allocate to discrete assets first (they have constraints)
        # Use greedy subset-sum to find best combination
        if discrete_assets and remaining > 0:
            discrete_alloc = self._allocate_discrete_subset(remaining, discrete_assets)
            allocations.update(discrete_alloc)
            discrete_total = sum(discrete_alloc.values())
            remaining -= discrete_total
            self.logger.info(
                "Discrete allocation: %.2f kW from %d assets, remaining: %.2f kW",
                discrete_total, len(discrete_alloc), remaining
            )
        
        # Phase 2: Fill remaining with continuous assets (proportional)
        if continuous_assets and remaining > 0:
            continuous_alloc = self._allocate_proportional(remaining, continuous_assets)
            allocations.update(continuous_alloc)
            continuous_total = sum(continuous_alloc.values())
            self.logger.info(
                "Continuous allocation: %.2f kW from %d assets",
                continuous_total, len(continuous_alloc)
            )
        
        # Log final allocation summary
        total_allocated = sum(allocations.values())
        deviation = total_allocated - total_kw
        deviation_pct = (deviation / total_kw * 100) if total_kw > 0 else 0
        
        if abs(deviation) < 0.01:
            deviation_msg = "(exact match)"
        elif deviation > 0:
            deviation_msg = f"(+{deviation:.2f} kW / +{deviation_pct:.1f}% over-delivery due to discrete assets)"
        else:
            deviation_msg = f"({deviation:.2f} kW / {deviation_pct:.1f}% under-delivery)"
        
        self.logger.info(
            "Modulation-aware final: requested=%.2f kW, will deliver=%.2f kW %s",
            total_kw, total_allocated, deviation_msg
        )
        
        return allocations
    
    def _allocate_discrete_subset(
        self, 
        target_kw: float, 
        discrete_assets: Dict
    ) -> Dict[str, float]:
        """
        Allocate flexibility from discrete (ON/OFF) assets using greedy subset-sum.
        
        For ON/OFF assets, we can only deliver flexibility in discrete chunks
        (e.g., 0 kW or 15 kW, nothing in between). This method finds the best
        combination of assets to switch OFF that gets closest to the target
        without significantly over-delivering.
        
        IMPORTANT: For discrete assets, the curtailment is the FULL capacity when 
        switched OFF, NOT capacity × flexibility_factor. The flexibility_factor 
        for discrete assets represents availability probability, not power reduction.
        
        Uses a greedy approach: sort by capacity descending, add largest that fits.
        
        :param target_kw: Target flexibility to deliver
        :param discrete_assets: Dictionary of discrete asset configurations
        :return: Dictionary mapping asset_id to curtailment_kw (full capacity when OFF)
        """
        allocations = {}
        
        # Build list of (asset_id, curtailable_power_kw)
        # For ON/OFF assets, curtailable power = FULL capacity (when switched OFF)
        candidates = []
        for asset_id, config in discrete_assets.items():
            states = self._get_discrete_states(config)
            # Curtailable power = difference between max and min state
            # For simple ON/OFF: [0, 15] -> curtailable = 15 (full capacity)
            if len(states) >= 2:
                curtailable = max(states) - min(states)
            else:
                curtailable = config.get("capacity_kw", 0)
            
            # NOTE: We do NOT apply flexibility_factor here!
            # For discrete assets, flex_factor represents availability probability,
            # not the amount of power that can be curtailed.
            # When switched OFF, the asset delivers FULL capacity as curtailment.
            
            if curtailable > 0:
                candidates.append((asset_id, curtailable, config))
                self.logger.debug(
                    "Discrete candidate: %s, curtailable=%.2f kW (full capacity when OFF)",
                    asset_id, curtailable
                )
        
        # Sort by curtailable power descending (largest first for greedy)
        candidates.sort(key=lambda x: -x[1])
        
        remaining = target_kw
        
        for asset_id, curtailable_kw, config in candidates:
            if remaining <= 0:
                break
            
            # Only include if it fits (doesn't cause excessive over-delivery)
            # Allow small over-delivery (within 10% or 1 kW tolerance)
            tolerance = max(target_kw * 0.1, 1.0)
            
            if curtailable_kw <= remaining + tolerance:
                # Switch this asset OFF -> delivers FULL capacity as curtailment
                allocations[asset_id] = round(curtailable_kw, 3)
                remaining -= curtailable_kw
                self.logger.debug(
                    "Discrete: %s -> switch OFF, curtail %.2f kW (remaining target: %.2f kW)",
                    asset_id, curtailable_kw, remaining
                )
        
        total_allocated = sum(allocations.values())
        over_delivery = total_allocated - target_kw
        
        self.logger.info(
            "Discrete subset allocation: target=%.2f kW, selected %d assets to switch OFF, "
            "total curtailment=%.2f kW %s",
            target_kw, len(allocations), total_allocated,
            f"(+{over_delivery:.2f} kW over-delivery due to discretization)" if over_delivery > 0 else ""
        )
        
        return allocations
    
    def _allocate_priority(
        self, 
        total_kw: float, 
        assets: Dict
    ) -> Dict[str, float]:
        """
        Allocate by filling highest flexibility assets first.
        
        Priority order: heat_pump > ev_charger (HPs are more reliable)
        """
        allocations = {}
        remaining = total_kw
        
        # Sort assets by type (HP first) then by capacity
        sorted_assets = sorted(
            assets.items(),
            key=lambda x: (
                0 if x[1].get("type") == "heat_pump" else 1,
                -x[1].get("capacity_kw", 0)
            )
        )
        
        for asset_id, config in sorted_assets:
            if remaining <= 0:
                break
            
            capacity = config.get("capacity_kw", 0)
            flex_factor = config.get("flexibility_factor", 0.5)
            max_flex = capacity * flex_factor
            
            allocation = min(max_flex, remaining)
            if allocation > 0:
                allocations[asset_id] = round(allocation, 3)
                remaining -= allocation
        
        self.logger.info("Priority allocation: %s", allocations)
        return allocations
    
    def _allocate_cost_optimal(
        self, 
        total_kw: float, 
        assets: Dict
    ) -> Dict[str, float]:
        """
        Allocate to minimize total activation cost.
        
        Uses activation_cost_per_kw from asset config.
        """
        allocations = {}
        remaining = total_kw
        
        # Sort by activation cost (lowest first)
        sorted_assets = sorted(
            assets.items(),
            key=lambda x: x[1].get("activation_cost_per_kw", 1.0)
        )
        
        for asset_id, config in sorted_assets:
            if remaining <= 0:
                break
            
            capacity = config.get("capacity_kw", 0)
            flex_factor = config.get("flexibility_factor", 0.5)
            max_flex = capacity * flex_factor
            
            allocation = min(max_flex, remaining)
            if allocation > 0:
                allocations[asset_id] = round(allocation, 3)
                remaining -= allocation
        
        self.logger.info("Cost-optimal allocation: %s", allocations)
        return allocations


# =============================================================================
# BID RECORD HANDLER (Database-backed)
# =============================================================================

class BidRecordHandler:
    """
    Handles reading bid records from PostgreSQL database.
    
    Bid records contain information about:
    - Strategy used when bidding
    - Assets allowed by the strategy
    - Orders that were placed
    
    This replaces the file-based approach for better reliability and querying.
    """
    
    def __init__(self, bid_repo: BidRecordRepository, logger: logging.Logger):
        self.bid_repo = bid_repo
        self.logger = logger
    
    def get_bid_record(self, fsp_id: str, slot_time: datetime) -> Optional[Dict]:
        """
        Get the bid record for a specific FSP and time slot from database.
        
        :param fsp_id: FSP identifier
        :param slot_time: Target time slot
        :return: Bid record dict or None
        """
        if self.bid_repo is None:
            self.logger.warning("No database connection - cannot load bid record")
            return None
        
        record = self.bid_repo.get_bid_record(fsp_id, slot_time)
        
        if record:
            self.logger.info("Loaded bid record ID %s from database", record.get("id", "?"))
        else:
            self.logger.warning("No bid record found for %s @ %s", fsp_id, slot_time)
        
        return record
    
    def get_allowed_assets(self, bid_record: Dict) -> List[str]:
        """
        Get list of asset IDs that were included in the bid.
        
        :param bid_record: Bid record dictionary
        :return: List of asset IDs
        """
        if not bid_record:
            return []
        
        assets_to_activate = bid_record.get("assets_to_activate", [])
        return [a["asset_id"] for a in assets_to_activate]

    def get_bid_asset_flexibilities(self, bid_record: Dict) -> Dict[str, float]:
        """
        Get positive per-asset bid-time flexibility quantities from bid assets.

        :param bid_record: Bid record dictionary
        :return: Mapping of asset_id to available flexibility in kW
        """
        if not bid_record:
            return {}

        planned_flexibilities = {}
        for asset in bid_record.get("assets_to_activate", []):
            asset_id = asset.get("asset_id")
            if not asset_id:
                continue

            try:
                available_kw = float(asset.get("available_flexibility_kw") or 0.0)
            except (TypeError, ValueError):
                available_kw = 0.0

            if available_kw > 0:
                planned_flexibilities[asset_id] = available_kw

        return planned_flexibilities
    
    def get_strategy_info(self, bid_record: Dict) -> Optional[Dict]:
        """
        Get strategy information from bid record.
        
        :param bid_record: Bid record dictionary
        :return: Strategy info dict or None
        """
        if not bid_record:
            return None
        return bid_record.get("strategy")
    
    def get_total_quantity(self, bid_record: Dict) -> float:
        """
        Get total quantity that was bid (in MW).
        
        :param bid_record: Bid record dictionary
        :return: Total quantity in MW
        """
        if not bid_record:
            return 0.0
        qty = bid_record.get("total_quantity_mw", 0.0)
        # Handle Decimal type from database
        return float(qty) if qty else 0.0
    
    def mark_activated(self, fsp_id: str, slot_time: datetime) -> bool:
        """
        Mark a bid record as activated in the database.
        
        :param fsp_id: FSP identifier
        :param slot_time: Target time slot
        :return: True if successful
        """
        if self.bid_repo is None:
            return False
        return self.bid_repo.mark_activated(fsp_id, slot_time)
    
    def get_trades_from_ledger(self, player_id: str, slot_time: datetime) -> List[Dict]:
        """
        Get trades from the local market_ledger table for a specific slot.
        
        This is useful when NODES API is not available or organization is not found.
        
        :param player_id: Player identifier (e.g., 'SUPSI')
        :param slot_time: Target time slot
        :return: List of trade dictionaries
        """
        if self.bid_repo is None or self.bid_repo.conn is None:
            self.logger.warning("No database connection - cannot query market_ledger")
            return []
        
        try:
            cur = self.bid_repo.conn.cursor()
            cur.execute("""
                SELECT id, timeslot_market, player_id, side, regulation, 
                       flexibility_quantity, price, bid_record_id
                FROM public.market_ledger
                WHERE player_id = %s AND timeslot_market = %s AND side = 'Sell'
            """, (player_id, slot_time))
            
            rows = cur.fetchall()
            trades = []
            for row in rows:
                trades.append({
                    "id": str(row[0]),
                    "timeslot": row[1],
                    "player_id": row[2],
                    "side": row[3],
                    "regulation": row[4],
                    "quantity": row[5],  # flexibility_quantity in MW
                    "price": row[6],
                    "bid_record_id": str(row[7]) if row[7] else None,
                })
            
            cur.close()
            
            if trades:
                self.logger.info("Found %d trades in market_ledger for %s @ %s", 
                               len(trades), player_id, slot_time)
            
            return trades
            
        except Exception as e:
            self.logger.error("Error querying market_ledger: %s", str(e))
            return []


# =============================================================================
# PRICE PREDICTOR (Autonomous Decision Support)
# =============================================================================

class PricePredictor:
    """
    Analyzes historical demand records to predict future DSO willingness to pay.
    
    Uses the demand_records table to understand when DSO is likely to pay more
    for flexibility. Separates analysis by day type (weekday vs weekend) to
    capture different behavior patterns.
    
    This enables autonomous pre-activation decisions:
    - If current price is low but higher prices expected soon, pre-heat/pre-cool
    - If current price is high, hold flexibility for activation
    """
    
    def __init__(
        self,
        demand_repo: DemandRecordRepository,
        dso_id: str,
        logger: logging.Logger,
        historical_days: int = 7
    ):
        """
        Initialize the price predictor.
        
        :param demand_repo: DemandRecordRepository instance
        :param dso_id: DSO identifier to analyze
        :param logger: Logger instance
        :param historical_days: Number of days to analyze (default: 7)
        """
        self.demand_repo = demand_repo
        self.dso_id = dso_id
        self.logger = logger
        self.historical_days = historical_days
        
        # Cache for price patterns (populated on first query)
        self._weekday_patterns = None
        self._weekend_patterns = None
    
    def _is_weekend(self, dt: datetime) -> bool:
        """Check if a datetime is on a weekend (Saturday=5, Sunday=6)."""
        return dt.weekday() >= 5
    
    def _get_slot_key(self, dt: datetime) -> str:
        """
        Generate a slot key (HH:MM) for grouping 15-minute slots.
        
        :param dt: Datetime to convert
        :return: String key like "09:15"
        """
        return dt.strftime("%H:%M")
    
    def _load_historical_patterns(self) -> None:
        """
        Load and analyze historical demand records.
        
        Groups data by:
        - Day type (weekday/weekend)
        - Time slot (15-minute intervals)
        
        Calculates statistics for price_offered per slot.
        """
        if self.demand_repo is None:
            self.logger.warning("No demand repository - cannot load patterns")
            self._weekday_patterns = {}
            self._weekend_patterns = {}
            return
        
        end_date = datetime.now(timezone.utc).replace(tzinfo=None)
        start_date = end_date - timedelta(days=self.historical_days)
        
        self.logger.info(
            "Loading demand history for %s from %s to %s",
            self.dso_id,
            start_date.strftime("%Y-%m-%d"),
            end_date.strftime("%Y-%m-%d")
        )
        
        try:
            records = self.demand_repo.get_history(self.dso_id, start_date, end_date)
            
            # Group by day type and slot
            weekday_data = {}  # slot_key -> list of prices
            weekend_data = {}
            
            for record in records:
                slot_start = record.get("slot_start")
                price = record.get("price_offered")
                
                if slot_start is None or price is None:
                    continue
                
                # Handle timezone-aware datetimes
                if hasattr(slot_start, 'tzinfo') and slot_start.tzinfo is not None:
                    slot_start = slot_start.replace(tzinfo=None)
                
                slot_key = self._get_slot_key(slot_start)
                price_float = float(price)
                
                if self._is_weekend(slot_start):
                    if slot_key not in weekend_data:
                        weekend_data[slot_key] = []
                    weekend_data[slot_key].append(price_float)
                else:
                    if slot_key not in weekday_data:
                        weekday_data[slot_key] = []
                    weekday_data[slot_key].append(price_float)
            
            # Calculate statistics for each slot
            self._weekday_patterns = self._calculate_slot_stats(weekday_data)
            self._weekend_patterns = self._calculate_slot_stats(weekend_data)
            
            self.logger.info(
                "Loaded patterns: %d weekday slots, %d weekend slots from %d records",
                len(self._weekday_patterns),
                len(self._weekend_patterns),
                len(records)
            )
            
        except Exception as e:
            self.logger.error("Error loading demand patterns: %s", str(e))
            self._weekday_patterns = {}
            self._weekend_patterns = {}
    
    def _calculate_slot_stats(self, slot_data: Dict[str, List[float]]) -> Dict[str, Dict]:
        """
        Calculate statistics for each time slot.
        
        :param slot_data: Dictionary of slot_key -> list of prices
        :return: Dictionary of slot_key -> stats dict
        """
        result = {}
        
        for slot_key, prices in slot_data.items():
            if not prices:
                continue
            
            result[slot_key] = {
                "count": len(prices),
                "avg": statistics.mean(prices),
                "min": min(prices),
                "max": max(prices),
                "std": statistics.stdev(prices) if len(prices) > 1 else 0.0,
                "values": prices  # Keep raw values for detailed analysis
            }
        
        return result
    
    def get_current_slot_price(self, slot_time: datetime = None) -> Optional[Dict]:
        """
        Get the current or specified slot's demand record price.
        
        :param slot_time: Optional slot time (defaults to current slot)
        :return: Dictionary with current demand info or None
        """
        if self.demand_repo is None:
            return None
        
        if slot_time is None:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            minutes = (now.minute // 15) * 15
            slot_time = now.replace(minute=minutes, second=0, microsecond=0)
        
        try:
            record = self.demand_repo.get_demand_record(self.dso_id, slot_time)
            if record:
                return {
                    "slot_start": record.get("slot_start"),
                    "price_offered": float(record.get("price_offered") or 0),
                    "quantity_up_mw": float(record.get("quantity_up_mw") or 0),
                    "quantity_down_mw": float(record.get("quantity_down_mw") or 0),
                    "status": record.get("status")
                }
            return None
        except Exception as e:
            self.logger.error("Error getting current slot price: %s", str(e))
            return None
    
    def predict_price_evolution(
        self,
        start_time: datetime = None,
        lookahead_hours: float = 3.0
    ) -> Dict:
        """
        Predict price evolution for the next N hours.
        
        Uses historical patterns to estimate expected prices for each 15-minute
        slot in the lookahead period.
        
        :param start_time: Starting time (defaults to now)
        :param lookahead_hours: Hours to look ahead (default: 3)
        :return: Dictionary with predictions and statistics
        """
        # Load patterns if not cached
        if self._weekday_patterns is None:
            self._load_historical_patterns()
        
        if start_time is None:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            # Align to current 15-minute slot
            minutes = (now.minute // 15) * 15
            start_time = now.replace(minute=minutes, second=0, microsecond=0)
        
        # Handle timezone-aware datetimes
        if hasattr(start_time, 'tzinfo') and start_time.tzinfo is not None:
            start_time = start_time.replace(tzinfo=None)
        
        # Calculate number of slots to predict
        slots_count = int(lookahead_hours * 4)  # 4 slots per hour
        
        # Determine which pattern set to use based on start day
        is_weekend = self._is_weekend(start_time)
        patterns = self._weekend_patterns if is_weekend else self._weekday_patterns
        day_type = "weekend" if is_weekend else "weekday"
        
        # Generate predictions for each slot
        predictions = []
        all_predicted_prices = []
        
        current_time = start_time
        for i in range(slots_count):
            slot_key = self._get_slot_key(current_time)
            slot_stats = patterns.get(slot_key, {})
            
            prediction = {
                "slot": current_time.isoformat(),
                "slot_key": slot_key,
                "offset_minutes": i * 15,
                "has_data": bool(slot_stats),
                "avg": slot_stats.get("avg"),
                "min": slot_stats.get("min"),
                "max": slot_stats.get("max"),
                "std": slot_stats.get("std"),
                "sample_count": slot_stats.get("count", 0)
            }
            
            predictions.append(prediction)
            
            if slot_stats.get("avg") is not None:
                all_predicted_prices.append(slot_stats["avg"])
            
            current_time = current_time + timedelta(minutes=15)
        
        # Calculate overall statistics
        overall_stats = {}
        if all_predicted_prices:
            overall_stats = {
                "avg": statistics.mean(all_predicted_prices),
                "min": min(all_predicted_prices),
                "max": max(all_predicted_prices),
                "std": statistics.stdev(all_predicted_prices) if len(all_predicted_prices) > 1 else 0.0,
                "slot_count_with_data": len(all_predicted_prices),
                "slot_count_total": slots_count
            }
        
        return {
            "start_time": start_time.isoformat(),
            "lookahead_hours": lookahead_hours,
            "day_type": day_type,
            "slots_analyzed": slots_count,
            "predictions": predictions,
            "overall_stats": overall_stats,
            "pattern_source": f"last {self.historical_days} days"
        }
    
    def should_preactivate(
        self,
        current_price: float,
        lookahead_hours: float = 3.0,
        threshold_pct: float = 20.0,
        start_time: datetime = None
    ) -> Dict:
        """
        Determine if pre-activation (e.g., pre-heating) makes sense.
        
        Pre-activation is recommended when:
        - Future prices are expected to be significantly higher than current
        - Asset is currently OFF and could be turned ON for pre-heating
        
        :param current_price: Current DSO price offer (or 0 if no current request)
        :param lookahead_hours: Hours to look ahead
        :param threshold_pct: Percentage increase to trigger recommendation
        :param start_time: Starting time for prediction (defaults to now)
        :return: Dictionary with recommendation and analysis
        """
        prediction = self.predict_price_evolution(
            lookahead_hours=lookahead_hours,
            start_time=start_time
        )
        
        overall = prediction.get("overall_stats", {})
        max_predicted = overall.get("max", 0)
        avg_predicted = overall.get("avg", 0)
        
        # Find peak slot
        peak_slot = None
        peak_price = 0
        for pred in prediction.get("predictions", []):
            if pred.get("avg") and pred["avg"] > peak_price:
                peak_price = pred["avg"]
                peak_slot = pred
        
        # Calculate potential gain
        price_increase = max_predicted - current_price if current_price > 0 else max_predicted
        price_increase_pct = (price_increase / current_price * 100) if current_price > 0 else 100
        
        # Recommendation logic
        recommend = False
        reason = ""
        
        if current_price == 0:
            # No current request
            if max_predicted > 0:
                recommend = True
                reason = f"No current DSO request, but peak of {max_predicted:.2f} CHF/MW expected"
            else:
                reason = "No current request and no historical price data"
        elif price_increase_pct >= threshold_pct:
            recommend = True
            reason = f"Price expected to increase by {price_increase_pct:.1f}% (from {current_price:.2f} to {max_predicted:.2f} CHF/MW)"
        else:
            reason = f"Price increase ({price_increase_pct:.1f}%) below threshold ({threshold_pct:.1f}%)"
        
        return {
            "recommend_preactivation": recommend,
            "reason": reason,
            "current_price": current_price,
            "max_predicted_price": max_predicted,
            "avg_predicted_price": avg_predicted,
            "price_increase": price_increase,
            "price_increase_pct": price_increase_pct,
            "peak_slot": peak_slot,
            "prediction_summary": prediction
        }
    
    def print_price_forecast(
        self,
        current_price: float = None,
        lookahead_hours: float = 3.0,
        start_time: datetime = None
    ) -> None:
        """
        Print a formatted price forecast to the logger.
        
        :param current_price: Current DSO price (optional)
        :param lookahead_hours: Hours to look ahead
        :param start_time: Starting time for prediction (defaults to now)
        """
        prediction = self.predict_price_evolution(
            lookahead_hours=lookahead_hours,
            start_time=start_time
        )
        overall = prediction.get("overall_stats", {})
        
        self.logger.info("=" * 70)
        self.logger.info("AUTONOMOUS MODE - PRICE FORECAST")
        self.logger.info("=" * 70)
        
        if current_price is not None and current_price > 0:
            self.logger.info("Current DSO price: %.2f CHF/MW", current_price)
        else:
            self.logger.info("Current DSO price: No current request")
        
        self.logger.info("-" * 70)
        self.logger.info(
            "Forecast for next %.1f hours (%d slots) based on %s (%s):",
            lookahead_hours,
            prediction.get("slots_analyzed", 0),
            prediction.get("pattern_source", "?"),
            prediction.get("day_type", "?")
        )
        self.logger.info("-" * 70)
        
        # Print slot-by-slot predictions
        self.logger.info("  %-8s  %-8s  %-8s  %-8s  %-8s  %-6s", 
                        "Time", "Avg", "Min", "Max", "Std", "N")
        self.logger.info("  " + "-" * 52)
        
        for pred in prediction.get("predictions", []):
            if pred.get("has_data"):
                self.logger.info(
                    "  %-8s  %8.2f  %8.2f  %8.2f  %8.2f  %6d",
                    pred["slot_key"],
                    pred.get("avg", 0),
                    pred.get("min", 0),
                    pred.get("max", 0),
                    pred.get("std", 0),
                    pred.get("sample_count", 0)
                )
            else:
                self.logger.info("  %-8s  %8s  %8s  %8s  %8s  %6s",
                               pred["slot_key"], "-", "-", "-", "-", "0")
        
        self.logger.info("-" * 70)
        self.logger.info("Overall forecast statistics:")
        if overall:
            self.logger.info("  Average:  %.2f CHF/MW", overall.get("avg", 0))
            self.logger.info("  Minimum:  %.2f CHF/MW", overall.get("min", 0))
            self.logger.info("  Maximum:  %.2f CHF/MW", overall.get("max", 0))
            self.logger.info("  Std Dev:  %.2f CHF/MW", overall.get("std", 0))
            self.logger.info("  Data coverage: %d/%d slots", 
                           overall.get("slot_count_with_data", 0),
                           overall.get("slot_count_total", 0))
        else:
            self.logger.info("  No historical data available for prediction")
        
        self.logger.info("=" * 70)


# =============================================================================
# MAIN FLEXIBILITY MANAGER
# =============================================================================

class FlexibilityManager:
    """
    Main class coordinating flexibility activation.
    
    Key feature: Uses bid records from PostgreSQL database to know which strategy
    was used and which assets should be activated.
    
    Autonomous mode: When no bid is active, analyzes historical demand patterns
    to predict when pre-activation (e.g., pre-heating) would be beneficial.
    """
    
    def __init__(
        self,
        config: dict,
        fsp_id: str,
        nodes_interface: NodesInterface,
        bid_repo: BidRecordRepository,
        logger: logging.Logger,
        nodes_authenticated: bool = False,
        rabbitmq_publisher: 'RabbitMQPublisher' = None,
        rabbitmq_config: dict = None,
        demand_repo: DemandRecordRepository = None,
        state_file: str = None,
        influx_client=None,
    ):
        self.config = config
        self.fsp_id = fsp_id
        self.fsp_config = config["fm"]["actors"]["fsps"].get(fsp_id, {})
        self.asset_mapping = config.get("asset_mapping", {})
        self.logger = logger
        self.bid_repo = bid_repo
        self.demand_repo = demand_repo
        self.nodes_authenticated = nodes_authenticated
        self.rabbitmq_publisher = rabbitmq_publisher
        self.rabbitmq_config = rabbitmq_config or config.get("rabbitMQ", {})
        self.state_file = state_file or os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..", "logs", "flexi_manager_state.json",
        )
        
        # Get community from config (fm.community)
        self.community = config.get("fm", {}).get("community", "")

        # Get FSP's allowed assets (default from config)
        self.fsp_assets = self.fsp_config.get("assets", list(self.asset_mapping.keys()))
        
        # Initialize strategy manager to derive assets from strategy if needed
        self.strategy_manager = StrategyManager(config, logger)
        
        # Initialize components
        self.market_handler = MarketResultsHandler(nodes_interface, logger)
        self.allocator = FlexibilityAllocator(self.asset_mapping, logger)
        self.controller = AssetController(
            self.asset_mapping,
            logger,
            rabbitmq_publisher,
            community=self.community,
            rabbitmq_config=self.rabbitmq_config,
        )
        self.bid_handler = BidRecordHandler(bid_repo, logger)
        self.activation_measurement_provider = None
        if influx_client is not None:
            self.activation_measurement_provider = ActivationMeasurementProvider(
                influx_client=influx_client,
                asset_mapping=self.asset_mapping,
                config=config,
                logger=logger,
            )
        
        # Autonomous mode configuration
        self.autonomous_config = config.get("autonomous", {})
        self.autonomous_enabled = self.autonomous_config.get("enabled", False)
        self.autonomous_dso_id = self.autonomous_config.get("dso_id", "AEM")
        self.autonomous_lookahead = self.autonomous_config.get("lookahead_hours", 3.0)
        self.autonomous_historical_days = self.autonomous_config.get("historical_days", 7)
        self.autonomous_threshold_pct = self.autonomous_config.get("price_increase_threshold_pct", 20.0)
        self.autonomous_preactivation_enabled = self.autonomous_config.get(
            "preactivation_enabled",
            True,
        )
        
        # Initialize price predictor for autonomous mode
        self.price_predictor = None
        if self.autonomous_enabled and demand_repo:
            self.price_predictor = PricePredictor(
                demand_repo=demand_repo,
                dso_id=self.autonomous_dso_id,
                logger=logger,
                historical_days=self.autonomous_historical_days
            )
            logger.info(
                "Autonomous mode enabled: lookahead=%.1fh, history=%d days, threshold=%.1f%%, preactivation=%s",
                self.autonomous_lookahead,
                self.autonomous_historical_days,
                self.autonomous_threshold_pct,
                "enabled" if self.autonomous_preactivation_enabled else "disabled",
            )
        
        # Get organization ID from NODES only if authenticated
        self.organization_id = None
        if nodes_authenticated:
            try:
                # Use /me endpoint which provides user's organization directly
                me_response = nodes_interface.get_request(
                    f"{nodes_interface.cfg['mainEndpoint']}me"
                )
                if me_response and isinstance(me_response, dict):
                    # Get organization from user's organizations list
                    orgs = me_response.get("organizations", [])
                    fsp_name = self.fsp_config.get("name", "")
                    
                    for org in orgs:
                        if isinstance(org, dict) and org.get("name") == fsp_name:
                            self.organization_id = org.get("id")
                            logger.info("NODES organization ID: %s (from /me)", self.organization_id)
                            break
                    
                    # If not found by name, use first organization
                    if not self.organization_id and orgs:
                        self.organization_id = orgs[0].get("id")
                        logger.info("Using first organization from /me: %s", self.organization_id)
                    
                    if not self.organization_id:
                        logger.warning("No organization found in /me response - will use bid record data")
            except Exception as e:
                logger.warning("Could not get organization ID: %s", str(e))
        else:
            logger.info("NODES API not authenticated - will use bid record data only")
    
    # ------------------------------------------------------------------
    # Persistent controlled-asset state
    # ------------------------------------------------------------------

    def _load_controlled_state(self) -> Dict:
        """Load the set of assets currently under manager control from disk.

        Returns a dict mapping asset_id to metadata (asset_type, slot, etc.).
        If the file is missing or unreadable the method returns an empty dict
        and logs a warning.
        """
        try:
            with open(self.state_file, "r") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return data
            self.logger.warning("Controlled-state file has unexpected format, ignoring")
            return {}
        except FileNotFoundError:
            return {}
        except Exception as exc:
            self.logger.warning(
                "Could not read controlled-state file %s: %s",
                self.state_file, exc,
            )
            return {}

    def _save_controlled_state(self, state: Dict) -> None:
        """Persist the controlled-asset state to disk.

        Logs an error (but does not crash) if the write fails.
        """
        try:
            state_dir = os.path.dirname(self.state_file)
            if state_dir:
                os.makedirs(state_dir, exist_ok=True)
            with open(self.state_file, "w") as fh:
                json.dump(state, fh, indent=2)
        except Exception as exc:
            self.logger.error(
                "Could not write controlled-state file %s: %s",
                self.state_file, exc,
            )

    def _queue_restores_for_previous_state(
        self,
        previous_state: Dict,
        current_curtailed: set,
        dry_run: bool,
    ) -> None:
        """Queue restore commands for assets that were previously controlled
        but are *not* selected for curtailment in the current slot.

        Assets whose stored ``state`` is ``"cooldown"`` are skipped: they have
        already been restored when the cooldown began and must not be restored
        again on every cooldown slot.  Entries without a ``state`` field are
        treated as ``"controlled"`` for backward compatibility.
        """
        for asset_id, meta in previous_state.items():
            if asset_id in current_curtailed:
                continue
            if isinstance(meta, dict) and meta.get("state") == "cooldown":
                continue
            slot_str = meta.get("slot_start", "?") if isinstance(meta, dict) else "?"
            self.logger.info(
                "Restoring previously controlled asset %s (last slot=%s)",
                asset_id, slot_str,
            )
            self.controller.restore_asset(asset_id, dry_run=dry_run)

    def get_target_slot(self, slot_override: str = None) -> Tuple[datetime, datetime]:
        """
        Determine the target time slot for flexibility activation.
        
        :param slot_override: Optional slot start time (ISO format)
        :return: Tuple of (slot_start, slot_end)
        """
        if slot_override:
            slot_start = datetime.fromisoformat(slot_override.replace("Z", "+00:00"))
            if slot_start.tzinfo:
                slot_start = slot_start.replace(tzinfo=None)
        else:
            # Default: next 15-minute slot
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            # Round up to next 15-minute boundary
            minutes = (now.minute // 15 + 1) * 15
            if minutes >= 60:
                slot_start = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
            else:
                slot_start = now.replace(minute=minutes, second=0, microsecond=0)
        
        slot_end = slot_start + timedelta(minutes=15)
        return slot_start, slot_end
    
    def _run_autonomous_analysis(self, slot_start: datetime, summary: Dict, dry_run: bool = True) -> None:
        """
        Run autonomous analysis when no bid record exists.
        
        Analyzes historical demand patterns to predict future DSO willingness
        to pay and determine if pre-activation would be beneficial.
        
        When pre-activation is recommended and dry_run=False, this method will
        send pre-heating commands only if autonomous.preactivation_enabled is true.

        :param slot_start: Current slot start time
        :param summary: Summary dictionary to update
        :param dry_run: If True, only log what would be done; if False, send commands
        """
        self.logger.info("")
        self.logger.info("=" * 70)
        self.logger.info("AUTONOMOUS MODE ANALYSIS")
        self.logger.info("=" * 70)
        
        # Get current demand/price (if any)
        current_info = self.price_predictor.get_current_slot_price(slot_start)
        current_price = current_info.get("price_offered", 0) if current_info else 0
        
        if current_info and current_price > 0:
            self.logger.info("Current slot demand from DSO:")
            self.logger.info("  Price offered: %.2f CHF/MW", current_price)
            self.logger.info("  Quantity Up:   %.3f MW", current_info.get("quantity_up_mw", 0))
            self.logger.info("  Quantity Down: %.3f MW", current_info.get("quantity_down_mw", 0))
        else:
            self.logger.info("No current DSO demand request for this slot")
            current_price = 0
        
        # Print price forecast starting from the analyzed slot
        self.price_predictor.print_price_forecast(
            current_price=current_price,
            lookahead_hours=self.autonomous_lookahead,
            start_time=slot_start
        )
        
        # Get pre-activation recommendation starting from the analyzed slot
        recommendation = self.price_predictor.should_preactivate(
            current_price=current_price,
            lookahead_hours=self.autonomous_lookahead,
            threshold_pct=self.autonomous_threshold_pct,
            start_time=slot_start
        )
        
        self.logger.info("")
        self.logger.info("=" * 70)
        self.logger.info("PRE-ACTIVATION RECOMMENDATION")
        self.logger.info("=" * 70)
        
        if recommendation.get("recommend_preactivation"):
            self.logger.info(">>> RECOMMENDATION: PRE-ACTIVATE ASSETS")
            self.logger.info(">>> Reason: %s", recommendation.get("reason", "N/A"))
            
            # Show peak slot details
            peak_slot = recommendation.get("peak_slot")
            if peak_slot:
                self.logger.info(">>> Peak expected at: %s with price %.2f CHF/MW",
                               peak_slot.get("slot_key", "?"),
                               peak_slot.get("avg", 0))
            
            # Show price improvement
            self.logger.info(">>> Current price: %.2f CHF/MW", current_price)
            self.logger.info(">>> Max predicted: %.2f CHF/MW (+%.1f%%)",
                           recommendation.get("max_predicted_price", 0),
                           recommendation.get("price_increase_pct", 0))
            
            # List assets that could be pre-activated (HPs for pre-heating)
            self.logger.info("")
            self.logger.info("Assets suitable for pre-activation (pre-heating):")

            preactivation_assets = []
            for asset_id in self.fsp_assets:
                asset_config = self.asset_mapping.get(asset_id, {})
                asset_type = asset_config.get("type", "")
                if asset_type == "heat_pump":
                    self.logger.info("  - %s (%s): capacity %.1f kW",
                                   asset_id,
                                   asset_config.get("description", ""),
                                   asset_config.get("capacity_kw", 0))
                    preactivation_assets.append(asset_id)

            # Execute pre-activation if not in dry-run mode
            if preactivation_assets:
                if not self.autonomous_preactivation_enabled:
                    self.logger.info("")
                    self.logger.info(
                        "Pre-activation command execution is disabled by configuration; skipping command publication"
                    )
                    summary["preactivation_executed"] = False
                    summary["preactivation_skipped_reason"] = "disabled_by_configuration"
                elif dry_run:
                    self.logger.info("")
                    self.logger.info("[DRY-RUN] Would send pre-heating (ON) commands to %d heat pump(s)",
                                   len(preactivation_assets))
                else:
                    self.logger.info("")
                    self.logger.info("=" * 70)
                    self.logger.info("EXECUTING PRE-ACTIVATION COMMANDS")
                    self.logger.info("=" * 70)

                    preactivation_results = []
                    for asset_id in preactivation_assets:
                        result = self._send_preactivation_command(asset_id, slot_start)
                        preactivation_results.append(result)

                    # Publish pending commands to RabbitMQ if configured
                    if self.rabbitmq_publisher and self.rabbitmq_publisher.is_connected():
                        slot_end = slot_start + timedelta(minutes=15)
                        slot_info = {
                            "fsp_id": self.fsp_id,
                            "slot_start": _format_aem_utc(slot_start),
                            "slot_end": _format_aem_utc(slot_end),
                            "command_type": "preactivation",
                            "dry_run": False,
                        }
                        published = self.controller.publish_pending_commands(slot_info, dry_run=False)
                        self.logger.info("Published %d pre-activation commands to RabbitMQ", published)

                    summary["preactivation_executed"] = True
                    summary["preactivation_results"] = preactivation_results
                    self.logger.info("Pre-activation commands sent to %d asset(s)", len(preactivation_results))
        else:
            self.logger.info(">>> RECOMMENDATION: NO PRE-ACTIVATION NEEDED")
            self.logger.info(">>> Reason: %s", recommendation.get("reason", "N/A"))
        
        self.logger.info("=" * 70)
        
        # Update summary with autonomous analysis results
        summary["autonomous_analysis"] = {
            "enabled": True,
            "current_price": current_price,
            "lookahead_hours": self.autonomous_lookahead,
            "recommendation": recommendation.get("recommend_preactivation", False),
            "reason": recommendation.get("reason", ""),
            "max_predicted_price": recommendation.get("max_predicted_price", 0),
            "avg_predicted_price": recommendation.get("avg_predicted_price", 0),
            "price_increase_pct": recommendation.get("price_increase_pct", 0),
            "peak_slot": recommendation.get("peak_slot", {}),
            "historical_days_analyzed": self.autonomous_historical_days,
            "threshold_pct": self.autonomous_threshold_pct,
            "preactivation_enabled": self.autonomous_preactivation_enabled,
            "preactivation_execution_skipped_reason": summary.get("preactivation_skipped_reason", ""),
        }
    
    def _send_preactivation_command(self, asset_id: str, slot_start: datetime) -> Dict:
        """
        Send a pre-activation (pre-heating) command to a heat pump asset.

        Pre-activation means turning the heat pump ON to pre-heat the building
        before an expected high-price period, so the building can coast through
        the high-price period with the heat pump OFF.

        :param asset_id: Asset identifier
        :param slot_start: Start of the slot
        :return: Result dictionary
        """
        asset_config = self.asset_mapping.get(asset_id, {})
        description = asset_config.get("description", asset_id)
        asset_type = asset_config.get("type", "heat_pump")
        capacity_kw = asset_config.get("capacity_kw", 0)
        site_id = asset_config.get("pod", "")

        slot_end = slot_start + timedelta(minutes=15)

        result = {
            "asset_id": asset_id,
            "description": description,
            "command": "preactivate",
            "target_state": "ON",
            "slot_start": _format_aem_utc(slot_start),
            "slot_end": _format_aem_utc(slot_end),
            "status": "pending",
        }

        command_payload = {
            "community": self.community,
            "site_id": site_id,
            "asset_id": asset_id,
            "description": description,
            "asset_type": asset_type,
            "modulation_type": "discrete",
            "command": "preactivate",
            "discrete_state": "ON",
            "target_state": "ON",
            "target_power_kw": float(capacity_kw),
            "capacity_kw": float(capacity_kw),
            "duration_minutes": 15,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "dry_run": False,
            "slot_start": _format_aem_utc(slot_start),
            "slot_end": _format_aem_utc(slot_end),
        }

        # Queue command for RabbitMQ
        if self.rabbitmq_publisher:
            self.controller._pending_commands.append({
                "asset_id": asset_id,
                "asset_type": asset_type,
                "command_type": "preactivate",
                "payload": command_payload,
                "priority": 6  # Medium-high priority for pre-activation
            })
            result["status"] = "queued"
            result["message"] = "Pre-activation command queued for RabbitMQ"
            self.logger.info(
                "Queued pre-activation: set %s (%s) to ON (%.1f kW) for pre-heating",
                asset_id, description, capacity_kw
            )
        else:
            # Direct actuation (not implemented yet)
            self.logger.warning(
                "No RabbitMQ publisher - cannot send pre-activation command to %s",
                asset_id
            )
            result["status"] = "error"
            result["message"] = "No RabbitMQ publisher configured"

        return result

    def _get_activation_modulation_type(self, asset_id: str) -> str:
        asset_config = self.asset_mapping.get(asset_id, {})
        mod_type = asset_config.get("modulation_type", "")
        if mod_type:
            return mod_type

        asset_type = asset_config.get("type", "")
        return "discrete" if asset_type == "heat_pump" else "continuous"

    def _build_ev_skip_result(
        self,
        asset_id: str,
        asset_config: Dict,
        mod_type: str,
        curtailment_kw: float,
        bid_reference_power_kw: Optional[float],
        bid_reference_source: Optional[str],
        skip_reason: str,
    ) -> Dict:
        """Build a uniform skipped control-result dict for a recent-profile EV asset."""
        return {
            "asset_id": asset_id,
            "description": asset_config.get("description", asset_id),
            "asset_type": asset_config.get("type", "unknown"),
            "modulation_type": mod_type,
            "requested_curtailment_kw": curtailment_kw,
            "actual_curtailment_kw": 0.0,
            "target_power_kw": None,
            "computed_target_power_kw": None,
            "reference_power_kw": bid_reference_power_kw,
            "reference_power_source": bid_reference_source,
            "status": "skipped",
            "skip_activation": True,
            "skip_reason": skip_reason,
            "message": skip_reason,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def _resolve_recent_profile_activation_current(
        self,
        asset_id: str,
        curtailment_kw: float,
        bid_reference_power_kw: Optional[float],
        bid_reference_source: Optional[str],
        activation_current_cfg: Dict,
        is_continuation: bool = False,
        is_discrete_ev: bool = False,
    ):
        """Resolve activation-time current power for a recent_profile_baseline continuous EV asset.

        When *is_continuation* is True the asset was controlled in the immediately
        previous slot.  The measured current may already reflect the previous
        command limit so the ``current_power < allocated_curtailment`` skip is
        bypassed and ``bid_reference_power_kw`` is returned as the activation
        reference instead.  The active-threshold check is still enforced because
        a very low reading may indicate the EV has disconnected.

        When *is_discrete_ev* is True the asset is actuated by setting a fixed
        OCPP power state (e.g. full OFF or a current step).  That command is
        deterministic regardless of the live draw, so the
        ``current_power < allocated_curtailment`` margin skip is bypassed: the
        command is always actuated and the live measurement is only used as the
        discrete-state selection reference.  The active-threshold and staleness
        checks still apply (they detect a disconnected/unmeasured EV).

        Returns:
            float: activation_current_power_kw (or bid_reference_power_kw for
                   continuation) if activation should proceed.
            str: skip_reason if activation should be skipped (fail-closed).
        """
        max_age_minutes = activation_current_cfg["max_measurement_age_minutes"]
        active_threshold_w = activation_current_cfg["active_threshold_w"]
        active_threshold_kw = active_threshold_w / 1000.0

        bid_ref_str = "%.3f" % bid_reference_power_kw if bid_reference_power_kw else "N/A"

        if self.activation_measurement_provider is None:
            reason = (
                "skipped: no measurement provider available for recent_profile_baseline "
                "continuous EV activation-current reference"
            )
            self.logger.warning(
                "Continuous activation reference decision for %s:\n"
                "  allocated_curtailment_kw=%.3f\n"
                "  bid_reference_power_kw=%s\n"
                "  bid_reference_source=%s\n"
                "  activation_current_power_kw=N/A\n"
                "  is_continuation=%s\n"
                "  decision=%s",
                asset_id, curtailment_kw,
                bid_ref_str, bid_reference_source,
                is_continuation, reason,
            )
            return reason

        current_time_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        measurement = self.activation_measurement_provider.get_latest_power(
            asset_id=asset_id,
            current_time_utc=current_time_utc,
            max_age_minutes=max_age_minutes,
        )

        if not measurement.get("valid"):
            reason = (
                f"skipped: no valid current measurement for recent_profile_baseline "
                f"continuous EV (reason={measurement.get('reason', 'unknown')})"
            )
            self.logger.warning(
                "Continuous activation reference decision for %s:\n"
                "  allocated_curtailment_kw=%.3f\n"
                "  bid_reference_power_kw=%s\n"
                "  bid_reference_source=%s\n"
                "  activation_current_power_kw=N/A\n"
                "  max_measurement_age_minutes=%d\n"
                "  is_continuation=%s\n"
                "  decision=%s",
                asset_id, curtailment_kw,
                bid_ref_str, bid_reference_source,
                max_age_minutes, is_continuation, reason,
            )
            return reason

        current_power_w = measurement["power_w"]
        current_power_kw = current_power_w / 1000.0
        age_minutes = measurement["age_minutes"]
        measurement_time_utc = measurement["timestamp_utc"]

        if age_minutes > max_age_minutes:
            reason = (
                f"skipped: current measurement too stale for recent_profile_baseline "
                f"continuous EV (age={age_minutes:.1f} min > max={max_age_minutes} min)"
            )
            self.logger.warning(
                "Continuous activation reference decision for %s:\n"
                "  allocated_curtailment_kw=%.3f\n"
                "  bid_reference_power_kw=%s\n"
                "  bid_reference_source=%s\n"
                "  activation_current_power_kw=%.3f\n"
                "  activation_current_time_utc=%s\n"
                "  activation_measurement_age_minutes=%.1f\n"
                "  max_measurement_age_minutes=%d\n"
                "  is_continuation=%s\n"
                "  decision=%s",
                asset_id, curtailment_kw,
                bid_ref_str, bid_reference_source,
                current_power_kw, str(measurement_time_utc), age_minutes,
                max_age_minutes, is_continuation, reason,
            )
            return reason

        if current_power_kw < active_threshold_kw:
            qualifier = (
                "continuation asset appears inactive/disconnected"
                if is_continuation
                else "EV appears inactive for recent_profile_baseline activation"
            )
            reason = (
                f"skipped: current power {current_power_kw:.3f} kW below "
                f"active threshold {active_threshold_kw:.3f} kW; {qualifier}"
            )
            self.logger.warning(
                "Continuous activation reference decision for %s:\n"
                "  allocated_curtailment_kw=%.3f\n"
                "  bid_reference_power_kw=%s\n"
                "  bid_reference_source=%s\n"
                "  activation_current_power_kw=%.3f\n"
                "  activation_current_time_utc=%s\n"
                "  activation_measurement_age_minutes=%.1f\n"
                "  active_threshold_kw=%.3f\n"
                "  is_continuation=%s\n"
                "  decision=%s",
                asset_id, curtailment_kw,
                bid_ref_str, bid_reference_source,
                current_power_kw, str(measurement_time_utc), age_minutes,
                active_threshold_kw, is_continuation, reason,
            )
            return reason

        # --- Consecutive-slot continuation ---
        if is_continuation:
            continuation_ref = bid_reference_power_kw
            continuation_target = max(0.0, continuation_ref - curtailment_kw)
            self.logger.info(
                "Consecutive activation continuation for %s:\n"
                "  bid_reference_power_kw=%.3f\n"
                "  allocated_curtailment_kw=%.3f\n"
                "  activation_current_power_kw=%.3f\n"
                "  activation_current_time_utc=%s\n"
                "  activation_measurement_age_minutes=%.1f\n"
                "  active_threshold_kw=%.3f\n"
                "  decision=continuing previous control; "
                "activation current may be control-affected\n"
                "  continuation_reference_kw=%.3f\n"
                "  target_power_kw=%.3f\n"
                "  restore_suppressed=True",
                asset_id,
                bid_reference_power_kw, curtailment_kw,
                current_power_kw, str(measurement_time_utc), age_minutes,
                active_threshold_kw,
                continuation_ref, continuation_target,
            )
            return continuation_ref

        if current_power_kw < curtailment_kw and not is_discrete_ev:
            reason = (
                f"skipped: current power {current_power_kw:.3f} kW below allocated "
                f"curtailment {curtailment_kw:.3f} kW; skipping recent-profile EV "
                f"activation to avoid uncertain delivery"
            )
            self.logger.warning(
                "Continuous activation reference decision for %s:\n"
                "  allocated_curtailment_kw=%.3f\n"
                "  bid_reference_power_kw=%s\n"
                "  bid_reference_source=%s\n"
                "  activation_current_power_kw=%.3f\n"
                "  activation_current_time_utc=%s\n"
                "  activation_measurement_age_minutes=%.1f\n"
                "  active_threshold_kw=%.3f\n"
                "  decision=%s",
                asset_id, curtailment_kw,
                bid_ref_str, bid_reference_source,
                current_power_kw, str(measurement_time_utc), age_minutes,
                active_threshold_kw, reason,
            )
            return reason

        if current_power_kw < curtailment_kw and is_discrete_ev:
            self.logger.info(
                "Continuous activation reference decision for %s:\n"
                "  allocated_curtailment_kw=%.3f\n"
                "  bid_reference_power_kw=%s\n"
                "  bid_reference_source=%s\n"
                "  activation_current_power_kw=%.3f\n"
                "  activation_current_time_utc=%s\n"
                "  activation_measurement_age_minutes=%.1f\n"
                "  active_threshold_kw=%.3f\n"
                "  decision=discrete EV command is deterministic (fixed power state); "
                "actuating despite current power below allocated curtailment",
                asset_id, curtailment_kw,
                bid_ref_str, bid_reference_source,
                current_power_kw, str(measurement_time_utc), age_minutes,
                active_threshold_kw,
            )
            return current_power_kw

        self.logger.info(
            "Continuous activation reference decision for %s:\n"
            "  allocated_curtailment_kw=%.3f\n"
            "  bid_reference_power_kw=%s\n"
            "  bid_reference_source=%s\n"
            "  activation_current_power_kw=%.3f\n"
            "  activation_current_time_utc=%s\n"
            "  activation_measurement_age_minutes=%.1f\n"
            "  active_threshold_kw=%.3f\n"
            "  selected_activation_reference_kw=%.3f\n"
            "  target_power_kw=%.3f\n"
            "  decision=used activation current power for recent_profile_baseline continuous EV",
            asset_id, curtailment_kw,
            bid_ref_str, bid_reference_source,
            current_power_kw, str(measurement_time_utc), age_minutes,
            active_threshold_kw, current_power_kw,
            max(0.0, current_power_kw - curtailment_kw),
        )
        return current_power_kw

    def _build_persistence_activation_allocations(
        self,
        planned_asset_flex_kw: Dict[str, float],
        accepted_kw: float,
    ) -> Tuple[Dict[str, float], Dict]:
        """
        Build a conservative persistence activation plan.

        Discrete assets are selected as whole assets; continuous assets fill the
        remaining accepted quantity without exceeding their stored bid-time plan.
        """
        tolerance = 1e-9
        # Allow a discrete asset to overshoot the accepted quantity by up to one
        # market rounding unit (0.001 MW = 1 kW). The bid quantity is floored to
        # MW precision, so an asset can be ~1 kW above accepted_kw purely due to
        # rounding; a tiny over-delivery is far cheaper than delivering nothing.
        overshoot_tolerance = 1.0
        planned_kw = sum(planned_asset_flex_kw.values())
        discrete_assets = []
        continuous_assets = []
        asset_details = {}

        for asset_id, planned_asset_kw in planned_asset_flex_kw.items():
            mod_type = self._get_activation_modulation_type(asset_id)
            asset_config = self.asset_mapping.get(asset_id, {})
            asset_type = asset_config.get("type", "unknown")
            detail = {
                "asset_id": asset_id,
                "asset_type": asset_type,
                "modulation_type": mod_type,
                "stored_kw": planned_asset_kw,
                "selected": False,
                "activation_kw": 0.0,
                "reason": "not selected",
            }
            asset_details[asset_id] = detail

            if mod_type == "discrete":
                discrete_assets.append((asset_id, planned_asset_kw))
            else:
                continuous_assets.append((asset_id, planned_asset_kw))

        discrete_planned_total = sum(kw for _, kw in discrete_assets)
        continuous_planned_total = sum(kw for _, kw in continuous_assets)

        if accepted_kw + overshoot_tolerance + tolerance >= planned_kw:
            allocations = dict(planned_asset_flex_kw)
            for asset_id, activation_kw in allocations.items():
                detail = asset_details[asset_id]
                detail["selected"] = True
                detail["activation_kw"] = activation_kw
                detail["reason"] = "accepted quantity covers full stored plan"

            return allocations, {
                "planned_kw": planned_kw,
                "accepted_kw": accepted_kw,
                "discrete_planned_total": discrete_planned_total,
                "continuous_planned_total": continuous_planned_total,
                "selected_discrete_total": discrete_planned_total,
                "continuous_activation_total": continuous_planned_total,
                "expected_delivered_kw": planned_kw,
                "under_delivery_kw": max(0.0, accepted_kw - planned_kw),
                "asset_details": list(asset_details.values()),
            }

        best_subset = []
        best_total = 0.0
        for mask in range(1 << len(discrete_assets)):
            subset = []
            subset_total = 0.0
            for index, item in enumerate(discrete_assets):
                if mask & (1 << index):
                    subset.append(item)
                    subset_total += item[1]

            if subset_total > accepted_kw + overshoot_tolerance + tolerance:
                continue

            if (
                subset_total > best_total + tolerance
                or (
                    abs(subset_total - best_total) <= tolerance
                    and len(subset) < len(best_subset)
                )
            ):
                best_subset = subset
                best_total = subset_total

        allocations = {}
        selected_discrete_ids = {asset_id for asset_id, _ in best_subset}
        for asset_id, selected_kw in best_subset:
            allocations[asset_id] = selected_kw
            detail = asset_details[asset_id]
            detail["selected"] = True
            detail["activation_kw"] = selected_kw
            detail["reason"] = "selected discrete subset"

        for asset_id, planned_asset_kw in discrete_assets:
            if asset_id in selected_discrete_ids:
                continue
            detail = asset_details[asset_id]
            if planned_asset_kw > accepted_kw + overshoot_tolerance + tolerance:
                detail["reason"] = "not selected because it would exceed accepted_kw"
            else:
                detail["reason"] = "not selected by best discrete subset"

        remaining_kw = max(0.0, accepted_kw - best_total)
        continuous_activation_total = 0.0
        if continuous_assets and remaining_kw > tolerance:
            if continuous_planned_total <= remaining_kw + tolerance:
                continuous_scale = 1.0
            else:
                continuous_scale = remaining_kw / continuous_planned_total

            for asset_id, planned_asset_kw in continuous_assets:
                activation_kw = planned_asset_kw * continuous_scale
                if activation_kw <= tolerance:
                    asset_details[asset_id]["reason"] = "no remaining accepted quantity"
                    continue

                allocations[asset_id] = activation_kw
                continuous_activation_total += activation_kw
                detail = asset_details[asset_id]
                detail["selected"] = True
                detail["activation_kw"] = activation_kw
                detail["reason"] = (
                    "continuous fill scaled"
                    if continuous_scale < 1.0 - tolerance
                    else "continuous fill full stored plan"
                )
        else:
            for asset_id, _ in continuous_assets:
                asset_details[asset_id]["reason"] = "no remaining accepted quantity"

        expected_delivered_kw = sum(allocations.values())

        return allocations, {
            "planned_kw": planned_kw,
            "accepted_kw": accepted_kw,
            "discrete_planned_total": discrete_planned_total,
            "continuous_planned_total": continuous_planned_total,
            "selected_discrete_total": best_total,
            "continuous_activation_total": continuous_activation_total,
            "expected_delivered_kw": expected_delivered_kw,
            "under_delivery_kw": max(0.0, accepted_kw - expected_delivered_kw),
            "asset_details": list(asset_details.values()),
        }

    # ------------------------------------------------------------------
    # Strategy-12 maintained prepared portfolio lifecycle (Step 3)
    #
    # The manager is the single owner of ON/OFF intent for the strategy's
    # binary heat pumps during the strategy-controlled window:
    #
    #   before prepareStart      -> idle       (no lifecycle commands)
    #   prepareStart..flexStart  -> prepare    (all scope assets desired ON)
    #   flexStart..maintainUntil -> maintain   (selected-for-delivery OFF,
    #                                            everything else ON)
    #   >= maintainUntil         -> release    (all owned assets OFF, cleared)
    #
    # This is schedule-driven and strategy-scoped, and is intentionally kept
    # separate from the price-driven autonomous pre-activation subsystem.
    # ------------------------------------------------------------------

    _PRECONDITIONING_STATES = ("prepared", "controlled")
    _PRECONDITIONING_WEATHER_GATE_PREFIX = "__preconditioning_weather_gate__:"

    # Command transport statuses treated as "the command was accepted"
    # (mirrors run()'s success set at the activation-tail).
    _COMMAND_SUCCESS_STATUSES = ("success", "simulated", "queued")

    @staticmethod
    def _parse_hhmm_to_minutes(value: str) -> int:
        """Parse an ``"HH:MM"`` string into minutes since midnight."""
        if not isinstance(value, str):
            raise ValueError(f"time value must be a string, got {value!r}")
        parts = value.strip().split(":")
        if len(parts) != 2:
            raise ValueError(f"invalid HH:MM time value: {value!r}")
        try:
            hours = int(parts[0])
            minutes = int(parts[1])
        except ValueError:
            raise ValueError(f"invalid HH:MM time value: {value!r}")
        if not (0 <= hours <= 23 and 0 <= minutes <= 59):
            raise ValueError(f"HH:MM time value out of range: {value!r}")
        return hours * 60 + minutes

    def _parse_preconditioning_settings(self, raw: Dict, strategy_id: str) -> Optional[Dict]:
        """Validate and parse a strategy's ``preconditioningSettings`` block.

        Returns a normalised settings dict, or ``None`` when the block is
        absent or explicitly disabled.  Structurally invalid configuration
        raises ``ValueError`` so the operator sees a clear failure instead of
        silently mis-controlling assets.
        """
        if not raw:
            return None
        if not isinstance(raw, dict):
            raise ValueError(
                f"{strategy_id}.preconditioningSettings must be an object"
            )

        enabled = raw.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ValueError(
                f"{strategy_id}.preconditioningSettings.enabled must be a boolean"
            )
        if not enabled:
            return None

        prepare_start = self._parse_hhmm_to_minutes(raw.get("prepareStart"))
        flexibility_start = self._parse_hhmm_to_minutes(raw.get("flexibilityStart"))
        maintain_until = self._parse_hhmm_to_minutes(raw.get("maintainUntil"))

        if not (prepare_start < flexibility_start < maintain_until):
            raise ValueError(
                f"{strategy_id}.preconditioningSettings requires "
                "prepareStart < flexibilityStart < maintainUntil "
                f"(got {raw.get('prepareStart')}, {raw.get('flexibilityStart')}, "
                f"{raw.get('maintainUntil')}); overnight windows are not supported"
            )

        release_action = raw.get("releaseAction", "force_off")
        if release_action != "force_off":
            raise ValueError(
                f"{strategy_id}.preconditioningSettings.releaseAction must be "
                f"'force_off' (got {release_action!r})"
            )

        owner_tag = raw.get("ownerTag", strategy_id)
        if not isinstance(owner_tag, str) or not owner_tag:
            raise ValueError(
                f"{strategy_id}.preconditioningSettings.ownerTag must be a "
                "non-empty string"
            )

        return {
            "enabled": True,
            "prepare_start_min": prepare_start,
            "flexibility_start_min": flexibility_start,
            "maintain_until_min": maintain_until,
            "release_action": release_action,
            "owner_tag": owner_tag,
            "prepare_start": raw.get("prepareStart"),
            "flexibility_start": raw.get("flexibilityStart"),
            "maintain_until": raw.get("maintainUntil"),
        }

    def _parse_preconditioning_weather_gate_settings(
        self,
        raw: Optional[Dict],
        strategy_id: str,
        preconditioning_settings: Dict,
    ) -> Dict:
        """Validate Strategy-owned weather gate settings.

        The gate is optional and defaults to disabled for backward
        compatibility. When enabled, the threshold is compared against the
        configured forecast window for the current lifecycle day.
        """
        source = f"bidding_strategies.{strategy_id}.weatherGateSettings"
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise ValueError(f"{source} must be an object")

        enabled = raw.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ValueError(f"{source}.enabled must be a boolean")
        if not enabled:
            return {
                "enabled": False,
                "source": raw.get("source", "flexibility.temperature.forecast"),
                "settings_source": source,
            }

        try:
            threshold_c = float(raw.get("temperatureThresholdC"))
        except (TypeError, ValueError):
            raise ValueError(f"{source}.temperatureThresholdC must be numeric")
        if not math.isfinite(threshold_c):
            raise ValueError(f"{source}.temperatureThresholdC must be finite")

        evaluation_start = raw.get(
            "evaluationStart", preconditioning_settings["flexibility_start"]
        )
        evaluation_end = raw.get(
            "evaluationEnd", preconditioning_settings["maintain_until"]
        )
        evaluation_start_min = self._parse_hhmm_to_minutes(evaluation_start)
        evaluation_end_min = self._parse_hhmm_to_minutes(evaluation_end)
        if evaluation_start_min >= evaluation_end_min:
            raise ValueError(
                f"{source} requires evaluationStart < evaluationEnd "
                f"(got {evaluation_start!r}, {evaluation_end!r})"
            )

        aggregation = raw.get("aggregation", "max")
        if aggregation not in ("max", "mean"):
            raise ValueError(f"{source}.aggregation must be 'max' or 'mean'")

        missing_policy = raw.get("missingForecastPolicy", "skip_preconditioning")
        if missing_policy not in ("skip_preconditioning", "fail"):
            raise ValueError(
                f"{source}.missingForecastPolicy must be "
                "'skip_preconditioning' or 'fail'"
            )

        return {
            "enabled": True,
            "settings_source": source,
            "source": raw.get("source", "flexibility.temperature.forecast"),
            "temperature_threshold_c": threshold_c,
            "evaluation_start": evaluation_start,
            "evaluation_end": evaluation_end,
            "evaluation_start_min": evaluation_start_min,
            "evaluation_end_min": evaluation_end_min,
            "aggregation": aggregation,
            "missing_forecast_policy": missing_policy,
            "constant_temperature_c": raw.get("constantTemperatureC"),
            "file": raw.get("file"),
        }

    def _resolve_active_strategy_id(self, fallback_strategy: Optional[str] = None) -> Optional[str]:
        """Resolve the strategy that is *active* for this FSP.

        Authoritative order (mirrors ``trader_fsp.py``):
          1. explicit override (``--fallback-strategy`` on the manager);
          2. the FSP config ``strategy`` field.

        A strategy merely *existing* in ``bidding_strategies`` never makes it
        active; it must be selected for this FSP.
        """
        if fallback_strategy:
            return fallback_strategy
        configured = self.fsp_config.get("strategy")
        if configured:
            return configured
        return None

    def _resolve_preconditioning_context(
        self, fallback_strategy: Optional[str] = None
    ) -> Optional[Dict]:
        """Resolve the preconditioning lifecycle context for this FSP.

        Returns ``None`` when the active strategy has no enabled
        ``preconditioningSettings`` block, meaning the lifecycle is inactive
        and ``run()`` proceeds with its normal activation flow.
        """
        active_strategy_id = self._resolve_active_strategy_id(fallback_strategy)
        if not active_strategy_id:
            return None

        strategies_cfg = self.config.get("bidding_strategies", {})
        strategy_cfg = strategies_cfg.get(active_strategy_id)
        if not strategy_cfg:
            return None

        settings = self._parse_preconditioning_settings(
            strategy_cfg.get("preconditioningSettings"),
            active_strategy_id,
        )
        if not settings:
            return None
        weather_gate_settings = self._parse_preconditioning_weather_gate_settings(
            strategy_cfg.get("weatherGateSettings"),
            active_strategy_id,
            settings,
        )

        # Scope strictly to the active strategy's allowed assets (assets_filter
        # intersected with asset_types), never the full FSP/asset_mapping set.
        strategy_obj = self.strategy_manager.get_strategy(active_strategy_id)
        scope_assets = [
            a for a in (strategy_obj.allowed_assets if strategy_obj else [])
            if a in self.asset_mapping
        ]

        return {
            "strategy_id": active_strategy_id,
            "owner_tag": settings["owner_tag"],
            "settings": settings,
            "weather_gate_settings": weather_gate_settings,
            "scope_assets": scope_assets,
        }

    def _resolve_preconditioning_phase(self, slot_start: datetime, settings: Dict) -> str:
        """Map the current slot time to a lifecycle phase.

        Uses the manager's existing UTC-naive slot time basis (see
        ``get_target_slot``); the configured ``HH:MM`` boundaries are compared
        against the slot's minutes-of-day on that same basis.
        """
        minutes_of_day = slot_start.hour * 60 + slot_start.minute
        if minutes_of_day < settings["prepare_start_min"]:
            return "idle"
        if minutes_of_day < settings["flexibility_start_min"]:
            return "prepare"
        if minutes_of_day < settings["maintain_until_min"]:
            return "maintain"
        return "release"

    def _is_owned_by_preconditioning(
        self, previous_state: Dict, asset_id: str, owner_tag: str
    ) -> bool:
        """Return True when ``asset_id`` is currently owned by this lifecycle."""
        meta = previous_state.get(asset_id)
        if not isinstance(meta, dict):
            return False
        if meta.get("state") not in self._PRECONDITIONING_STATES:
            return False
        return meta.get("owner") == owner_tag

    def _compute_preconditioning_desired_state(
        self,
        phase: str,
        scope_assets: List[str],
        selected_off: set,
        previous_state: Dict,
        owner_tag: str,
    ) -> Dict[str, str]:
        """Core lifecycle oracle: desired ON/OFF per scope asset.

        Returns a mapping of ``asset_id -> "ON"|"OFF"``.  Assets that must be
        left untouched are simply omitted from the mapping.
        """
        desired: Dict[str, str] = {}

        if phase in ("prepare", "maintain"):
            for asset_id in scope_assets:
                # During 14:00-20:00 every scope asset is owned by the
                # lifecycle: selected-for-delivery -> OFF, otherwise ON.
                if phase == "maintain" and asset_id in selected_off:
                    desired[asset_id] = "OFF"
                else:
                    desired[asset_id] = "ON"
        elif phase in ("release", "idle"):
            # Release: all currently-owned scope assets are switched OFF and
            # ownership is cleared.  Idle only ever touches leftover ownership
            # (e.g. a missed release after a restart); unowned assets are never
            # touched before prepareStart.
            for asset_id in scope_assets:
                if self._is_owned_by_preconditioning(previous_state, asset_id, owner_tag):
                    desired[asset_id] = "OFF"

        return desired

    def _preconditioning_weather_gate_key(self, strategy_id: str) -> str:
        return f"{self._PRECONDITIONING_WEATHER_GATE_PREFIX}{strategy_id}"

    def _preconditioning_day(self, slot_start: datetime) -> str:
        return slot_start.date().isoformat()

    def _stored_preconditioning_weather_gate(
        self,
        previous_state: Dict,
        strategy_id: str,
        gate_date: str,
    ) -> Optional[Dict]:
        meta = previous_state.get(self._preconditioning_weather_gate_key(strategy_id))
        if not isinstance(meta, dict):
            return None
        if meta.get("date") != gate_date:
            return None
        return meta

    def _has_preconditioning_ownership(
        self, previous_state: Dict, scope_assets: List[str], owner_tag: str
    ) -> bool:
        return any(
            self._is_owned_by_preconditioning(previous_state, asset_id, owner_tag)
            for asset_id in scope_assets
        )

    @staticmethod
    def _is_valid_temperature_value(value) -> bool:
        if value is None:
            return False
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return False
        return math.isfinite(numeric)

    @staticmethod
    def _parse_weather_forecast_timestamp(value) -> Optional[datetime]:
        if not isinstance(value, str):
            return None
        normalized = value.strip()
        if not normalized:
            return None
        if normalized.endswith("Z"):
            normalized = f"{normalized[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed.replace(second=0, microsecond=0)

    def _weather_gate_window_bounds(
        self, slot_start: datetime, settings: Dict
    ) -> Tuple[datetime, datetime]:
        day = slot_start.replace(hour=0, minute=0, second=0, microsecond=0)
        start = day + timedelta(minutes=settings["evaluation_start_min"])
        end = day + timedelta(minutes=settings["evaluation_end_min"])
        return start, end

    def _weather_gate_source_config(self, settings: Dict) -> Tuple[str, Dict]:
        temp_cfg = self.config.get("flexibility", {}).get("temperature", {})
        forecast_cfg = dict(temp_cfg.get("forecast", {}) or {})
        configured_source = settings.get("source") or "flexibility.temperature.forecast"
        if configured_source == "flexibility.temperature.forecast":
            source_type = forecast_cfg.get("type", "constant")
        else:
            source_type = configured_source
            forecast_cfg.setdefault("type", source_type)
        return source_type, {
            "temperature_enabled": bool(temp_cfg.get("enabled", False)),
            "forecast": forecast_cfg,
        }

    def _load_weather_gate_forecast_values(
        self,
        slot_start: datetime,
        settings: Dict,
    ) -> Tuple[List[float], str, str]:
        """Return forecast values in Celsius for the configured gate window."""
        source_type, source_cfg = self._weather_gate_source_config(settings)
        forecast_cfg = source_cfg["forecast"]
        if not source_cfg["temperature_enabled"]:
            return [], source_type, "missing_forecast"

        window_start, window_end = self._weather_gate_window_bounds(slot_start, settings)
        if source_type == "constant":
            raw_value = settings.get("constant_temperature_c")
            if raw_value is None:
                raw_value = forecast_cfg.get("value", forecast_cfg.get("constant"))
            if not self._is_valid_temperature_value(raw_value):
                return [], source_type, "invalid_forecast"
            granularity = int(self.config.get("fm", {}).get("granularity", 15) or 15)
            count = max(
                1,
                int((window_end - window_start).total_seconds() // (granularity * 60)),
            )
            return [float(raw_value)] * count, source_type, "ok"

        if source_type == "file":
            forecast_file = settings.get("file") or forecast_cfg.get("file")
            if not forecast_file or not os.path.isfile(forecast_file):
                return [], source_type, "missing_forecast"
            try:
                with open(forecast_file, "r") as fh:
                    forecast_data = json.load(fh)
            except Exception:
                return [], source_type, "invalid_forecast"

            values = []
            invalid_seen = False
            for entry in forecast_data.get("forecast", []):
                if not isinstance(entry, dict):
                    invalid_seen = True
                    continue
                timestamp = self._parse_weather_forecast_timestamp(
                    entry.get("timestamp")
                )
                if timestamp is None:
                    invalid_seen = True
                    continue
                if not (window_start <= timestamp < window_end):
                    continue
                raw_temp = entry.get("temperature")
                if not self._is_valid_temperature_value(raw_temp):
                    invalid_seen = True
                    continue
                values.append(float(raw_temp))
            if invalid_seen:
                return [], source_type, "invalid_forecast"
            if not values:
                return [], source_type, "missing_forecast"
            return values, source_type, "ok"

        # Historical average support exists in FlexibilityForecaster, but the
        # manager has no historical temperature cache here. Treat it as missing
        # unless a deterministic forecast file/source is provided.
        return [], source_type, "missing_forecast"

    def _evaluate_preconditioning_weather_gate(
        self,
        settings: Dict,
        slot_start: datetime,
    ) -> Dict:
        if not settings.get("enabled"):
            return {
                "enabled": False,
                "decision": "open",
                "reason": "disabled",
                "source": settings.get("source", "flexibility.temperature.forecast"),
            }

        window_start, window_end = self._weather_gate_window_bounds(slot_start, settings)
        values, source_type, load_status = self._load_weather_gate_forecast_values(
            slot_start, settings
        )
        result = {
            "enabled": True,
            "decision": "closed",
            "reason": load_status,
            "source": source_type,
            "evaluation_start": settings["evaluation_start"],
            "evaluation_end": settings["evaluation_end"],
            "evaluation_window": {
                "start": _format_aem_utc(window_start),
                "end": _format_aem_utc(window_end),
            },
            "aggregation": settings["aggregation"],
            "aggregated_temperature_c": None,
            "threshold_c": settings["temperature_threshold_c"],
            "forecast_sample_count": len(values),
            "forecast_values_c": values,
            "missing_forecast_policy": settings["missing_forecast_policy"],
        }
        if load_status != "ok":
            if load_status == "missing_forecast" and settings["missing_forecast_policy"] == "fail":
                raise ValueError(
                    "Strategy 12 weather gate forecast is unavailable and "
                    "missingForecastPolicy is 'fail'"
                )
            return result

        if settings["aggregation"] == "mean":
            aggregated = sum(values) / len(values)
        else:
            aggregated = max(values)
        result["aggregated_temperature_c"] = aggregated
        if aggregated >= settings["temperature_threshold_c"]:
            result["decision"] = "open"
            result["reason"] = "threshold_met"
        else:
            result["reason"] = "threshold_not_met"
        return result

    def _weather_gate_summary_from_metadata(
        self,
        metadata: Dict,
        settings: Dict,
        reason: Optional[str] = None,
    ) -> Dict:
        summary = dict(metadata.get("result", {}) if isinstance(metadata, dict) else {})
        summary["enabled"] = bool(settings.get("enabled"))
        summary["decision"] = metadata.get("decision", summary.get("decision", "closed"))
        summary["reason"] = reason or metadata.get("reason", summary.get("reason", "stored"))
        summary["date"] = metadata.get("date")
        summary["stable_daily_decision"] = True
        return summary

    def _weather_gate_metadata_from_result(
        self,
        result: Dict,
        gate_date: str,
        strategy_id: str,
    ) -> Dict:
        return {
            "owner": strategy_id,
            "strategy_id": strategy_id,
            "state": "weather_gate_decision",
            "date": gate_date,
            "decision": result.get("decision"),
            "reason": result.get("reason"),
            "result": result,
        }

    def _resolve_preconditioning_weather_gate(
        self,
        pc_ctx: Dict,
        phase: str,
        slot_start: datetime,
        previous_state: Dict,
    ) -> Tuple[Dict, Optional[Dict], bool]:
        """Resolve the stable daily weather-gate decision.

        Returns ``(summary_result, metadata_to_persist, lifecycle_allowed)``.
        Release/idle cleanup is never blocked by weather.
        """
        settings = pc_ctx.get("weather_gate_settings", {"enabled": False})
        strategy_id = pc_ctx["strategy_id"]
        owner_tag = pc_ctx["owner_tag"]
        scope_assets = pc_ctx["scope_assets"]

        if not settings.get("enabled"):
            return self._evaluate_preconditioning_weather_gate(settings, slot_start), None, True

        gate_date = self._preconditioning_day(slot_start)
        if phase in ("release", "idle"):
            result = self._evaluate_preconditioning_weather_gate(settings, slot_start)
            result["decision"] = "open"
            result["reason"] = f"{phase}_cleanup_not_gated"
            return result, None, True

        has_ownership = self._has_preconditioning_ownership(
            previous_state, scope_assets, owner_tag
        )
        if has_ownership:
            result = self._evaluate_preconditioning_weather_gate(settings, slot_start)
            result["decision"] = "open"
            result["reason"] = "existing_lifecycle_ownership"
            metadata = self._weather_gate_metadata_from_result(
                result, gate_date, strategy_id
            )
            return result, metadata, True

        stored = self._stored_preconditioning_weather_gate(
            previous_state, strategy_id, gate_date
        )
        if stored:
            result = self._weather_gate_summary_from_metadata(stored, settings)
            return result, None, result.get("decision") == "open"

        if phase == "maintain":
            result = self._evaluate_preconditioning_weather_gate(settings, slot_start)
            result["decision"] = "closed"
            result["reason"] = "no_preparation_ownership"
            metadata = self._weather_gate_metadata_from_result(
                result, gate_date, strategy_id
            )
            return result, metadata, False

        result = self._evaluate_preconditioning_weather_gate(settings, slot_start)
        metadata = self._weather_gate_metadata_from_result(
            result, gate_date, strategy_id
        )
        return result, metadata, result.get("decision") == "open"

    def _complete_preconditioning_weather_gate_skip(
        self,
        summary: Dict,
        previous_state: Dict,
        pc_ctx: Dict,
        phase: str,
        weather_gate_result: Dict,
        weather_gate_metadata: Optional[Dict],
    ) -> Dict:
        strategy_id = pc_ctx["strategy_id"]
        gate_key = self._preconditioning_weather_gate_key(strategy_id)
        new_state = dict(previous_state)
        if weather_gate_metadata is not None:
            new_state[gate_key] = weather_gate_metadata
        summary["preconditioning"]["weather_gate"] = weather_gate_result
        summary["preconditioning"]["selected_for_delivery"] = []
        summary["preconditioning"]["transitions"] = []
        summary["preconditioning"]["command_results"] = []
        summary["preconditioning"]["delivery_assets"] = []
        summary["preconditioning"]["successful_commands"] = 0
        summary["preconditioning"]["failed_commands"] = 0
        summary["preconditioning"]["delivery_activation_records_written"] = 0
        summary["preconditioning"]["delivery_source"] = "weather_gate_closed"
        summary["preconditioning"]["batch_metadata"] = {
            "lifecycle": "preconditioned_binary",
            "phase": phase,
            "strategy_id": strategy_id,
            "has_delivery": False,
            "delivery_assets": [],
            "weather_gate_decision": weather_gate_result.get("decision"),
        }
        self._save_controlled_state(new_state)
        self.logger.info(
            "WEATHER_GATE decision=CLOSED reason=%s phase=%s no Strategy 12 commands issued",
            weather_gate_result.get("reason"),
            phase,
        )
        return summary

    def _resolve_current_delivery_selection(
        self,
        pc_ctx: Dict,
        slot_start: datetime,
        slot_end: datetime,
        dry_run: bool,
        simulate_sold_mw: Optional[float],
        allow_market_ledger_fallback: bool,
        summary: Dict,
    ) -> Dict:
        """Determine which scope assets are selected for the current accepted
        delivery, reusing the existing bid-record + accepted-trade + discrete
        allocation machinery (no second allocator).

        Returns a delivery dict::

            {
                "selected_off": set(asset_id),   # assets to switch OFF now
                "allocations": {asset_id: kw},   # delivered flexibility per asset
                "bid_record_id": <id or None>,   # for activation-record linkage
            }

        An empty ``selected_off`` means "no accepted delivery -> keep all
        prepared ON".
        """
        empty: Dict = {"selected_off": set(), "allocations": {}, "bid_record_id": None}
        scope = set(pc_ctx["scope_assets"])

        bid_record = self.bid_handler.get_bid_record(self.fsp_id, slot_start)
        if not bid_record:
            summary["preconditioning"]["delivery_source"] = "no_bid_record"
            return empty

        bid_record_id = bid_record.get("id")

        planned = self.bid_handler.get_bid_asset_flexibilities(bid_record)
        planned = {a: kw for a, kw in planned.items() if a in scope}
        if not planned:
            summary["preconditioning"]["delivery_source"] = "no_planned_scope_assets"
            return {"selected_off": set(), "allocations": {}, "bid_record_id": bid_record_id}

        # Resolve accepted quantity (mirrors run()'s market-result handling).
        if simulate_sold_mw is not None:
            total_sold_kw = simulate_sold_mw * 1000.0
            summary["preconditioning"]["delivery_source"] = "simulate_sold_mw"
        else:
            trades = []
            if self.organization_id:
                trades = self.market_handler.get_accepted_trades_for_slot(
                    self.organization_id, slot_start, slot_end
                )
            if not trades and allow_market_ledger_fallback:
                player_name = self.fsp_config.get("name", self.fsp_id)
                trades = self.bid_handler.get_trades_from_ledger(player_name, slot_start)
            total_sold_kw = sum(t.get("quantity", 0) for t in trades) * 1000.0
            summary["preconditioning"]["delivery_source"] = (
                "nodes_or_ledger" if trades else "no_trades"
            )

        if total_sold_kw <= 0:
            return {"selected_off": set(), "allocations": {}, "bid_record_id": bid_record_id}

        allocations, _selection = self._build_persistence_activation_allocations(
            planned, total_sold_kw
        )
        selected_allocations = {
            asset_id: curtailment_kw
            for asset_id, curtailment_kw in allocations.items()
            if curtailment_kw > 0 and asset_id in scope
        }
        selected_off = set(selected_allocations.keys())
        summary["preconditioning"]["current_delivery_allocation"] = dict(selected_allocations)
        return {
            "selected_off": selected_off,
            "allocations": selected_allocations,
            "bid_record_id": bid_record_id,
        }

    def _run_preconditioning_lifecycle(
        self,
        pc_ctx: Dict,
        slot_start: datetime,
        slot_end: datetime,
        previous_state: Dict,
        dry_run: bool,
        summary: Dict,
        simulate_sold_mw: Optional[float] = None,
        allow_market_ledger_fallback: bool = False,
    ) -> Dict:
        """Execute the manager-owned Strategy-12 lifecycle for one slot.

        When preconditioning is active for this FSP the manager is the single
        owner of ON/OFF intent for the scope assets, so this method fully
        handles the slot and ``run()`` returns its result directly.
        """
        settings = pc_ctx["settings"]
        scope_assets = pc_ctx["scope_assets"]
        owner_tag = pc_ctx["owner_tag"]
        strategy_id = pc_ctx["strategy_id"]
        phase = self._resolve_preconditioning_phase(slot_start, settings)

        summary["strategy_used"] = strategy_id
        summary["flexibility_method"] = "preconditioned_binary"
        summary["preconditioning"] = {
            "active": True,
            "strategy_id": strategy_id,
            "owner_tag": owner_tag,
            "phase": phase,
            "scope_assets": list(scope_assets),
            "window": {
                "prepareStart": settings["prepare_start"],
                "flexibilityStart": settings["flexibility_start"],
                "maintainUntil": settings["maintain_until"],
            },
        }

        self.logger.info("=" * 70)
        self.logger.info("PRECONDITIONING LIFECYCLE - %s", self.fsp_id)
        self.logger.info("=" * 70)
        self.logger.info(
            "PRECONDITIONING phase=%s strategy=%s assets=%d slot=%s",
            phase, strategy_id, len(scope_assets),
            slot_start.strftime("%Y-%m-%d %H:%M"),
        )

        weather_gate_result, weather_gate_metadata, lifecycle_allowed = (
            self._resolve_preconditioning_weather_gate(
                pc_ctx, phase, slot_start, previous_state
            )
        )
        summary["preconditioning"]["weather_gate"] = weather_gate_result
        if not lifecycle_allowed:
            return self._complete_preconditioning_weather_gate_skip(
                summary,
                previous_state,
                pc_ctx,
                phase,
                weather_gate_result,
                weather_gate_metadata,
            )
        if weather_gate_result.get("enabled"):
            aggregated = weather_gate_result.get("aggregated_temperature_c")
            self.logger.info(
                "WEATHER_GATE decision=%s reason=%s aggregated=%sC "
                "threshold=%sC source=%s",
                str(weather_gate_result.get("decision", "")).upper(),
                weather_gate_result.get("reason"),
                f"{aggregated:.1f}" if isinstance(aggregated, (int, float)) else "n/a",
                weather_gate_result.get("threshold_c"),
                weather_gate_result.get("source"),
            )

        # Resolve the current accepted-delivery selection only in the
        # maintained-flexibility phase; preparation and release do not consult
        # the market.
        delivery: Dict = {"selected_off": set(), "allocations": {}, "bid_record_id": None}
        if phase == "maintain":
            delivery = self._resolve_current_delivery_selection(
                pc_ctx, slot_start, slot_end, dry_run,
                simulate_sold_mw, allow_market_ledger_fallback, summary,
            )
        selected_off = delivery["selected_off"]
        delivery_allocations = delivery["allocations"]
        delivery_bid_record_id = delivery["bid_record_id"]
        summary["preconditioning"]["selected_for_delivery"] = sorted(selected_off)

        desired = self._compute_preconditioning_desired_state(
            phase, scope_assets, selected_off, previous_state, owner_tag,
        )

        # Preserve any state entries that are NOT owned by this lifecycle
        # (defensive: the state file may be shared).  Rebuild scope entries.
        new_state: Dict = {
            asset_id: meta
            for asset_id, meta in previous_state.items()
            if asset_id not in scope_assets
        }
        gate_key = self._preconditioning_weather_gate_key(strategy_id)
        if phase == "release":
            new_state.pop(gate_key, None)
        elif weather_gate_metadata is not None:
            new_state[gate_key] = weather_gate_metadata

        transitions = []
        command_results = []
        successful_commands = 0
        failed_commands = 0
        # Transport status of any command actually issued this slot, keyed by
        # asset. Used to stamp delivery activation-record status truthfully.
        emitted_status_by_asset: Dict[str, str] = {}

        for asset_id in scope_assets:
            want = desired.get(asset_id)
            prev_meta = previous_state.get(asset_id)
            prev_owned = self._is_owned_by_preconditioning(
                previous_state, asset_id, owner_tag
            )
            prev_cmd = (
                prev_meta.get("last_commanded_state")
                if isinstance(prev_meta, dict) else None
            )
            prev_state_label = (
                prev_meta.get("state") if isinstance(prev_meta, dict) else None
            )

            if want is None:
                # Idle + unowned asset -> untouched, no ownership recorded.
                continue

            if phase in ("release", "idle"):
                # Definitive release: always emit OFF for owned assets. Release
                # is NOT a market activation and never produces an activation
                # DB record. Ownership is cleared only after the OFF command is
                # accepted; otherwise the previous entry is preserved so the
                # next manager run naturally retries cleanup.
                result = self._emit_preconditioning_command(asset_id, "OFF", dry_run)
                status = (result or {}).get("status", "unknown")
                emitted_status_by_asset[asset_id] = status
                command_ok = status in self._COMMAND_SUCCESS_STATUSES
                if command_ok:
                    successful_commands += 1
                else:
                    failed_commands += 1
                command_results.append({
                    "asset_id": asset_id, "category": "release",
                    "desired": "OFF", "status": status,
                })
                action = "OFF" if command_ok else "OFF_failed"
                transitions.append((asset_id, prev_state_label, "released", action))
                if not command_ok and isinstance(prev_meta, dict):
                    new_state[asset_id] = prev_meta
                self.logger.info(
                    "PRECONDITIONING phase=%s asset=%s previous=%s action=OFF "
                    "category=release status=%s ownership=%s",
                    phase, asset_id, prev_state_label or "none", status,
                    "cleared" if command_ok else "retained",
                )
                continue

            # Preparation / maintained-flexibility phases.
            lifecycle_state = "controlled" if want == "OFF" else "prepared"
            # Business category (see Step 3.5 semantics):
            #   preparation  : prepare-phase ON        (Category A, no DB record)
            #   maintenance  : maintain-phase ON        (Category B, no DB record)
            #   delivery     : maintain-phase OFF       (Category C, DB record)
            if phase == "prepare":
                category = "preparation"
            else:  # maintain
                category = "delivery" if want == "OFF" else "maintenance"

            # Idempotency: only (re)issue a command when the desired state
            # differs from the last manager-owned command, or when the asset is
            # not yet owned by this lifecycle.
            need_command = (not prev_owned) or (prev_cmd != want)
            command_ok = True
            if need_command:
                result = self._emit_preconditioning_command(asset_id, want, dry_run)
                status = (result or {}).get("status", "unknown")
                emitted_status_by_asset[asset_id] = status
                command_ok = status in self._COMMAND_SUCCESS_STATUSES
                if command_ok:
                    successful_commands += 1
                else:
                    failed_commands += 1
                command_results.append({
                    "asset_id": asset_id, "category": category,
                    "desired": want, "status": status,
                })
                action = want if command_ok else f"{want}_failed"
            else:
                action = "none"

            transitions.append((asset_id, prev_state_label, lifecycle_state, action))
            self.logger.info(
                "PRECONDITIONING phase=%s asset=%s previous=%s desired=%s "
                "action=%s category=%s",
                phase, asset_id, prev_state_label or "none", lifecycle_state,
                action, category,
            )

            # Command-state bookkeeping (no telemetry confirmation available):
            # advance to the desired lifecycle state when the command was
            # accepted or was unnecessary (idempotent no-op). On a FAILED
            # command, preserve the previous known state so the next per-slot
            # run naturally re-asserts the desired state (implicit retry).
            if need_command and not command_ok:
                if isinstance(prev_meta, dict):
                    new_state[asset_id] = prev_meta
                # not previously owned -> leave unowned (no ownership on failure)
                continue

            asset_cfg = self.asset_mapping.get(asset_id, {})
            prepared_since = (
                prev_meta.get("prepared_since")
                if (prev_owned and isinstance(prev_meta, dict)
                    and prev_meta.get("prepared_since"))
                else _format_aem_utc(slot_start)
            )
            new_state[asset_id] = {
                "state": lifecycle_state,
                "owner": owner_tag,
                "strategy_id": strategy_id,
                "asset_type": asset_cfg.get("type", "unknown"),
                "last_commanded_state": want,
                "prepared_since": prepared_since,
                "slot_start": _format_aem_utc(slot_start),
                "slot_end": _format_aem_utc(slot_end),
            }

        # ------------------------------------------------------------------
        # Delivery bookkeeping (Category C only): persist activation DB records
        # for maintain-phase selected-OFF delivery commands, reusing the normal
        # activation-record API. Non-fatal on error (mirrors run() Step 5).
        # ------------------------------------------------------------------
        activation_records_written = 0
        if phase == "maintain" and selected_off:
            activation_records_written = self._persist_delivery_activations(
                selected_off=selected_off,
                delivery_allocations=delivery_allocations,
                emitted_status_by_asset=emitted_status_by_asset,
                bid_record_id=delivery_bid_record_id,
                slot_start=slot_start,
                slot_end=slot_end,
                dry_run=dry_run,
            )

        summary["preconditioning"]["transitions"] = [
            {
                "asset_id": a,
                "previous": prev,
                "desired": desired_label,
                "action": action,
            }
            for (a, prev, desired_label, action) in transitions
        ]
        summary["preconditioning"]["command_results"] = command_results
        summary["preconditioning"]["delivery_assets"] = sorted(selected_off)
        summary["preconditioning"]["successful_commands"] = successful_commands
        summary["preconditioning"]["failed_commands"] = failed_commands
        summary["preconditioning"]["delivery_activation_records_written"] = (
            activation_records_written
        )

        # Batch/lifecycle observability metadata. A maintain batch can legitimately
        # be MIXED (delivery OFF + maintenance ON), so we expose the phase and the
        # explicit delivery subset rather than a single misleading label.
        has_delivery = bool(selected_off)
        batch_metadata = {
            "lifecycle": "preconditioned_binary",
            "phase": phase,
            "strategy_id": strategy_id,
            "has_delivery": has_delivery,
            "delivery_assets": sorted(selected_off),
        }
        summary["preconditioning"]["batch_metadata"] = batch_metadata

        # Publish queued commands (if RabbitMQ is configured). Per-command
        # command_type ("curtail"/"restore") is unchanged; only the batch header
        # gains richer lifecycle metadata.
        published = None
        if self.rabbitmq_publisher and self.rabbitmq_publisher.is_connected():
            slot_info = {
                "fsp_id": self.fsp_id,
                "slot_start": _format_aem_utc(slot_start),
                "slot_end": _format_aem_utc(slot_end),
                # Kept for backward compatibility; no longer the only semantic tag.
                "command_type": "preconditioning",
                "dry_run": dry_run,
                **batch_metadata,
            }
            published = self.controller.publish_pending_commands(slot_info, dry_run=dry_run)
            summary["rabbitmq_published"] = published
            self.logger.info("Published %d preconditioning commands to RabbitMQ", published)

        self._save_controlled_state(new_state)

        # Top-level status: surface command failures without adding retries.
        if failed_commands and successful_commands:
            summary["status"] = "partial_success"
        elif failed_commands and not successful_commands:
            summary["status"] = "activation_failed"
        elif published is not None:
            summary["status"] = "queued"
        # else: leave the default "success"

        self.logger.info("=" * 70)
        self.logger.info(
            "PRECONDITIONING COMPLETE phase=%s owned_assets=%d has_delivery=%s "
            "delivery_records=%d ok=%d failed=%d status=%s",
            phase,
            sum(
                1 for meta in new_state.values()
                if isinstance(meta, dict)
                and meta.get("owner") == owner_tag
                and meta.get("state") in self._PRECONDITIONING_STATES
            ),
            has_delivery,
            activation_records_written,
            successful_commands,
            failed_commands,
            summary["status"],
        )
        self.logger.info("=" * 70)
        return summary

    def _persist_delivery_activations(
        self,
        selected_off: set,
        delivery_allocations: Dict[str, float],
        emitted_status_by_asset: Dict[str, str],
        bid_record_id,
        slot_start: datetime,
        slot_end: datetime,
        dry_run: bool,
    ) -> int:
        """Persist activation DB records for maintain-phase delivery OFF commands.

        Reuses ``bid_repo.save_asset_activations_batch`` (no new table/schema).
        Records are written ONLY for the selected-OFF delivery subset (Category
        C); preparation/maintenance/release commands never reach this method.
        Failures are logged and swallowed so control commands are never
        duplicated and the lifecycle is never re-run (mirrors run() Step 5).

        Returns the number of activation records written (0 on error or when no
        repository is configured).
        """
        bid_repo = getattr(self, "bid_repo", None)
        if bid_repo is None:
            self.logger.info(
                "PRECONDITIONING no bid repository configured; skipping delivery "
                "activation-record persistence"
            )
            return 0

        # Status for an idempotent (already-OFF) delivery slot where no fresh
        # command was issued: reflect the transport that WOULD carry it.
        connected = bool(self.rabbitmq_publisher and self.rabbitmq_publisher.is_connected())
        fallback_status = "queued" if connected else ("simulated" if dry_run else "success")

        activation_records = []
        for asset_id in sorted(selected_off):
            asset_cfg = self.asset_mapping.get(asset_id, {})
            capacity_kw = float(asset_cfg.get("capacity_kw", 0) or 0)
            # Delivered downward flexibility = the allocator's per-asset block
            # (switching the binary HP OFF removes its full ON-state power).
            delivered_kw = float(delivery_allocations.get(asset_id, capacity_kw) or 0.0)
            status = emitted_status_by_asset.get(asset_id, fallback_status)
            activation_records.append({
                "asset_id": asset_id,
                "power_kw": delivered_kw,
                "description": asset_cfg.get("description", asset_id),
                "asset_type": asset_cfg.get("type"),
                "percentage": 100.0 if capacity_kw > 0 else None,
                "status": status,
            })

        if not activation_records:
            return 0

        if bid_record_id is None:
            self.logger.warning(
                "PRECONDITIONING writing %d delivery activation record(s) WITHOUT "
                "bid_record_id linkage (schema allows NULL); current bid record "
                "had no id", len(activation_records),
            )

        try:
            saved = bid_repo.save_asset_activations_batch(
                fsp_id=self.fsp_id,
                slot_start=slot_start,
                slot_end=slot_end,
                activations=activation_records,
                allocation_strategy="preconditioned_binary",
                dry_run=dry_run,
                bid_record_id=bid_record_id,
            )
            self.logger.info(
                "PRECONDITIONING persisted delivery activation records: assets=%s "
                "dry_run=%s bid_record_id=%s (repo reported %s)",
                [r["asset_id"] for r in activation_records], dry_run, bid_record_id,
                saved,
            )
            return len(activation_records)
        except Exception as exc:
            self.logger.error(
                "PRECONDITIONING could not save delivery activation records: %s", exc
            )
            return 0

    def _emit_preconditioning_command(
        self, asset_id: str, desired: str, dry_run: bool
    ) -> Dict:
        """Emit an unambiguous binary ON/OFF command for a scope asset.

        Reuses the existing manager command primitives:
          * ON  -> ``restore_asset`` (discrete_state=ON, target_power=capacity);
          * OFF -> ``curtail_asset(..., force_discrete_off=True)``
                   (discrete_state=OFF, target_power=0).
        """
        if desired == "ON":
            return self.controller.restore_asset(asset_id, dry_run=dry_run)
        capacity_kw = float(self.asset_mapping.get(asset_id, {}).get("capacity_kw", 0) or 0)
        return self.controller.curtail_asset(
            asset_id,
            capacity_kw,
            dry_run=dry_run,
            force_discrete_off=True,
        )

    def run(
        self,
        slot_override: str = None,
        dry_run: bool = True,
        allocation_strategy: str = "modulation_aware",
        fallback_strategy: str = None,
        simulate_sold_mw: Optional[float] = None,
        allow_market_ledger_fallback: bool = False
    ) -> Dict:
        """
        Run the flexibility activation process.
        
        The process:
        1. Check for bid record from trader_fsp.py
        2. If found, use strategy info to determine allowed assets
        3. Query market for trades (or use bid record quantity)
        4. Allocate flexibility across allowed assets
        5. Send control commands
        
        :param slot_override: Optional slot start time
        :param dry_run: If True, simulate only
        :param allocation_strategy: How to distribute flexibility across assets
        :param simulate_sold_mw: Dry-run-only simulated accepted quantity in MW
        :param allow_market_ledger_fallback: If True, use local market_ledger only when NODES has no accepted trades
        :return: Summary of actions taken
        """
        if simulate_sold_mw is not None and not dry_run:
            raise ValueError("--simulate-sold-mw can only be used with --dry-run")

        slot_start, slot_end = self.get_target_slot(slot_override)

        # Load previously controlled assets so we can restore any that are
        # no longer needed for the current slot.
        previous_state = self._load_controlled_state()

        self.logger.info("=" * 70)
        self.logger.info("FLEXIBILITY MANAGER - %s", self.fsp_id)
        self.logger.info("=" * 70)
        self.logger.info("Target slot: %s - %s", 
                        slot_start.strftime("%Y-%m-%d %H:%M"),
                        slot_end.strftime("%H:%M"))
        self.logger.info("Mode: %s", "DRY-RUN" if dry_run else "LIVE")
        self.logger.info("Allocation strategy: %s", allocation_strategy)
        if previous_state:
            self.logger.info("Previously controlled assets: %s", list(previous_state.keys()))
        self.logger.info("-" * 70)
        
        summary = {
            "fsp_id": self.fsp_id,
            "slot_start": slot_start.isoformat(),
            "slot_end": slot_end.isoformat(),
            "dry_run": dry_run,
            "allocation_strategy": allocation_strategy,
            "simulate_sold_mw": simulate_sold_mw,
            "allow_market_ledger_fallback": allow_market_ledger_fallback,
            "market_result_source": "none",
            "bid_record": None,
            "strategy_used": None,
            "allowed_assets": [],
            "trades": [],
            "total_flexibility_sold_mw": 0,
            "total_flexibility_sold_kw": 0,
            "allocations": {},
            "control_results": {},
            "status": "success"
        }

        # ==============================================================
        # Strategy-12 maintained prepared portfolio lifecycle (Step 3).
        #
        # When the FSP's active strategy owns a preconditioning lifecycle,
        # the manager becomes the single owner of ON/OFF intent for the
        # scope assets and fully governs this slot here.  This is resolved
        # from the FSP/CLI-configured active strategy (NOT merely a strategy
        # existing in config, and NOT the bid record), so preparation can
        # begin before the first evening bid record exists.
        # ==============================================================
        pc_ctx = self._resolve_preconditioning_context(fallback_strategy)
        if pc_ctx is not None:
            return self._run_preconditioning_lifecycle(
                pc_ctx,
                slot_start,
                slot_end,
                previous_state,
                dry_run,
                summary,
                simulate_sold_mw=simulate_sold_mw,
                allow_market_ledger_fallback=allow_market_ledger_fallback,
            )

        strategy_info = None
        strategy_obj = None
        flexibility_method = None
        is_persistence_strategy = False
        planned_bid_asset_flex_kw = {}
        bid_asset_reference_power_by_id = {}
        
        # Step 0: Check for bid record from trader_fsp.py
        self.logger.info("Step 0: Checking for bid record...")
        bid_record = self.bid_handler.get_bid_record(self.fsp_id, slot_start)
        
        if bid_record:
            summary["bid_record"] = bid_record
            bid_asset_reference_power_by_id = _build_bid_asset_reference_power_lookup(
                bid_record,
                self.logger,
            )
            if bid_asset_reference_power_by_id:
                summary["activation_reference_power_by_asset"] = bid_asset_reference_power_by_id
            strategy_info = self.bid_handler.get_strategy_info(bid_record)
            allowed_assets = self.bid_handler.get_allowed_assets(bid_record)
            bid_quantity_mw = self.bid_handler.get_total_quantity(bid_record)
            
            if strategy_info and strategy_info.get("id"):
                strategy_obj = self.strategy_manager.get_strategy(strategy_info.get("id"))
                if strategy_obj:
                    flexibility_method = strategy_obj.config.get("flexibility_method")
                    # Strategies whose bidder pre-computes a per-asset
                    # activation plan and stores it in bid_record_assets.
                    # Persistence (strategy_8/9), recent_profile (strategy_10)
                    # and preconditioned_binary (strategy_12) all follow this
                    # contract, so they reuse the same activation pipeline
                    # downstream (binary HPs are switched OFF via force_off).
                    is_persistence_strategy = flexibility_method in (
                        "persistence",
                        "recent_profile",
                        "preconditioned_binary",
                    )
                    summary["flexibility_method"] = flexibility_method

                self.logger.info("  Strategy: %s (%s)", 
                               strategy_info.get("id"), 
                               strategy_info.get("name", "N/A"))
                if flexibility_method:
                    self.logger.info("  Flexibility method: %s", flexibility_method)
                summary["strategy_used"] = strategy_info.get("id")
            else:
                self.logger.info("  Strategy: None (simple mode)")
            
            if is_persistence_strategy:
                planned_bid_asset_flex_kw = self.bid_handler.get_bid_asset_flexibilities(bid_record)
                allowed_assets = list(planned_bid_asset_flex_kw.keys())
                summary["allowed_assets"] = allowed_assets
                summary["allocation_source"] = "bid_record_assets.available_flexibility_kw"

                if allowed_assets:
                    self.logger.info(
                        "  Persistence assets with positive bid-time flexibility: %s",
                        allowed_assets
                    )
                    for _pid, _pkw in planned_bid_asset_flex_kw.items():
                        self.logger.info(
                            "    %s: %.3f kW", _pid, _pkw
                        )

                    # Validate that every persistence bid asset exists in
                    # asset_mapping so modulation type detection is reliable.
                    missing_assets = [
                        a for a in allowed_assets if a not in self.asset_mapping
                    ]
                    if missing_assets:
                        self.logger.error(
                            "  Persistence strategy %s references assets not in "
                            "asset_mapping: %s; failing closed to avoid mis-classification",
                            strategy_info.get("id"),
                            missing_assets,
                        )
                        self.logger.info("=" * 70)
                        self.logger.info("NO ACTIVATION - ASSET MAPPING INCOMPLETE")
                        self.logger.info("=" * 70)
                        self.logger.info("No assets will be activated.")

                        summary["status"] = "persistence_asset_mapping_incomplete"
                        summary["message"] = (
                            f"Persistence bid assets {missing_assets} not found in "
                            "asset_mapping - activation aborted for safety"
                        )
                        summary["missing_assets"] = missing_assets
                        summary["total_flexibility_sold_mw"] = 0
                        summary["total_flexibility_sold_kw"] = 0
                        summary["allocation"] = {}
                        summary["allocations"] = {}
                        summary["activation_results"] = []

                        if previous_state:
                            self._queue_restores_for_previous_state(
                                previous_state, current_curtailed=set(), dry_run=dry_run,
                            )
                            if self.rabbitmq_publisher and self.rabbitmq_publisher.is_connected():
                                restore_slot_info = {
                                    "fsp_id": self.fsp_id,
                                    "slot_start": _format_aem_utc(slot_start),
                                    "slot_end": _format_aem_utc(slot_end),
                                    "dry_run": dry_run,
                                }
                                self.controller.publish_pending_commands(restore_slot_info, dry_run=dry_run)
                            self._save_controlled_state({})

                        return summary
                else:
                    self.logger.error(
                        "  Persistence strategy %s has no positive "
                        "bid_record_assets.available_flexibility_kw rows; failing closed",
                        strategy_info.get("id")
                    )
                    self.logger.info("=" * 70)
                    self.logger.info("NO ACTIVATION REQUIRED")
                    self.logger.info("=" * 70)
                    self.logger.info("No assets will be activated.")

                    summary["status"] = "persistence_no_positive_bid_assets"
                    summary["message"] = (
                        "Persistence strategy has no positive bid_record_assets."
                        "available_flexibility_kw rows - no activation performed"
                    )
                    summary["total_flexibility_sold_mw"] = 0
                    summary["total_flexibility_sold_kw"] = 0
                    summary["allocation"] = {}
                    summary["allocations"] = {}
                    summary["activation_results"] = []

                    if previous_state:
                        self._queue_restores_for_previous_state(
                            previous_state, current_curtailed=set(), dry_run=dry_run,
                        )
                        if self.rabbitmq_publisher and self.rabbitmq_publisher.is_connected():
                            restore_slot_info = {
                                "fsp_id": self.fsp_id,
                                "slot_start": _format_aem_utc(slot_start),
                                "slot_end": _format_aem_utc(slot_end),
                                "dry_run": dry_run,
                            }
                            self.controller.publish_pending_commands(restore_slot_info, dry_run=dry_run)
                        self._save_controlled_state({})

                    return summary
            elif allowed_assets:
                self.logger.info("  Allowed assets from bid record: %s", allowed_assets)
                summary["allowed_assets"] = allowed_assets
            else:
                # No explicit assets stored - try to derive from strategy
                self.logger.warning("  No explicit assets in bid record")
                
                if strategy_info and strategy_info.get("id"):
                    # Derive assets from strategy definition
                    if strategy_obj and strategy_obj.allowed_assets:
                        allowed_assets = strategy_obj.allowed_assets
                        self.logger.info("  Derived assets from strategy %s: %s", 
                                       strategy_info.get("id"), allowed_assets)
                        summary["allowed_assets"] = allowed_assets
                        summary["assets_derived_from_strategy"] = True
                    else:
                        self.logger.warning("  Could not derive assets from strategy - using FSP defaults")
                        allowed_assets = self.fsp_assets
                        summary["allowed_assets"] = allowed_assets
                else:
                    self.logger.warning("  No strategy info - using FSP defaults")
                    allowed_assets = self.fsp_assets
                    summary["allowed_assets"] = allowed_assets
            
            self.logger.info("  Bid quantity: %.3f MW", bid_quantity_mw)
            
            # If bid quantity is 0, treat it as "no actual bidding" for autonomous mode
            if bid_quantity_mw == 0:
                self.logger.info("  Note: Bid quantity is 0 MW - no actual flexibility was bid")
        else:
            self.logger.warning("  No bid record found for this slot")
            self.logger.info("=" * 70)
            self.logger.info("NO ACTIVATION REQUIRED")
            self.logger.info("=" * 70)
            self.logger.info("No bid was placed for slot %s - %s", 
                           slot_start.strftime("%Y-%m-%d %H:%M"),
                           slot_end.strftime("%H:%M"))
            self.logger.info("No assets will be activated.")
            
            summary["status"] = "no_bid_record"
            summary["message"] = "No bid record found for this slot - no activation performed"
            summary["allowed_assets"] = []
            summary["total_flexibility_sold_mw"] = 0
            summary["total_flexibility_sold_kw"] = 0
            summary["allocation"] = {}
            summary["activation_results"] = []

            # Restore all previously controlled assets
            if previous_state:
                self._queue_restores_for_previous_state(
                    previous_state, current_curtailed=set(), dry_run=dry_run,
                )
                if self.rabbitmq_publisher and self.rabbitmq_publisher.is_connected():
                    restore_slot_info = {
                        "fsp_id": self.fsp_id,
                        "slot_start": _format_aem_utc(slot_start),
                        "slot_end": _format_aem_utc(slot_end),
                        "dry_run": dry_run,
                    }
                    self.controller.publish_pending_commands(restore_slot_info, dry_run=dry_run)
                self._save_controlled_state({})

            # ========================================================
            # AUTONOMOUS MODE: Analyze price evolution when no bid exists
            # When dry_run=False, pre-heating commands will actually be sent
            # ========================================================
            if self.autonomous_enabled and self.price_predictor:
                self._run_autonomous_analysis(slot_start, summary, dry_run=dry_run)

            return summary
        
        # Step 1: Query market results
        self.logger.info("-" * 70)
        self.logger.info("Step 1: Querying market results...")
        
        trades = []
        settlements = []

        if simulate_sold_mw is not None:
            simulated_sold_kw = simulate_sold_mw * 1000
            summary["market_result_source"] = "simulate_sold_mw"
            self.logger.warning(
                "DRY-RUN SIMULATION: using simulated sold quantity %.6f MW (%.3f kW) "
                "for activation-path testing",
                simulate_sold_mw,
                simulated_sold_kw
            )
            self.logger.warning(
                "DRY-RUN SIMULATION: target slot %s - %s",
                slot_start.strftime("%Y-%m-%d %H:%M"),
                slot_end.strftime("%H:%M")
            )
            if strategy_info and strategy_info.get("id"):
                self.logger.warning(
                    "DRY-RUN SIMULATION: strategy %s (%s)",
                    strategy_info.get("id"),
                    strategy_info.get("name", "N/A")
                )
            self.logger.warning(
                "DRY-RUN SIMULATION: bypassing local market_ledger and NODES accepted-trade lookup"
            )
            trades = [{
                "id": "dry-run-simulated",
                "timeslot": slot_start,
                "player_id": self.fsp_config.get("name", self.fsp_id),
                "side": "Sell",
                "regulation": "simulated",
                "quantity": simulate_sold_mw,
                "price": None,
                "bid_record_id": str(bid_record.get("id")) if bid_record else None,
                "simulated": True,
            }]
        else:
            if self.organization_id:
                self.logger.info("Querying NODES accepted trades before any local ledger fallback...")
                trades = self.market_handler.get_accepted_trades_for_slot(
                    self.organization_id, slot_start, slot_end
                )
                if trades:
                    self.logger.info("MARKET RESULT SOURCE: NODES accepted trades")
                    summary["market_result_source"] = "nodes_accepted_trades"
                    settlements = self.market_handler.get_settlements_for_slot(
                        self.organization_id, slot_start, slot_end
                    )
                else:
                    self.logger.warning(
                        "No NODES accepted trades found for slot %s - %s",
                        slot_start.strftime("%Y-%m-%d %H:%M"),
                        slot_end.strftime("%H:%M")
                    )
            else:
                self.logger.warning("No NODES organization ID available; cannot query accepted trades")

            if not trades:
                if allow_market_ledger_fallback:
                    self.logger.warning("WARNING: MARKET RESULT SOURCE: local public.market_ledger fallback")
                    self.logger.warning(
                        "WARNING: market_ledger rows may represent posted orders, not accepted/cleared trades."
                    )
                    self.logger.warning(
                        "WARNING: Use this fallback only for controlled testing or when operations guarantee ledger rows are confirmed market results."
                    )
                    player_name = self.fsp_config.get("name", self.fsp_id)
                    local_trades = self.bid_handler.get_trades_from_ledger(player_name, slot_start)
                    if local_trades:
                        trades = local_trades
                        summary["market_result_source"] = "market_ledger_fallback"
                    else:
                        self.logger.info("No local market_ledger fallback rows found for this slot")
                        summary["market_result_source"] = "none"
                else:
                    self.logger.warning(
                        "market_ledger fallback is disabled; no activation will be performed."
                    )
                    summary["market_result_source"] = "none"
        
        # Calculate total sold flexibility from ACTUAL trades only
        total_sold_mw = sum(t.get("quantity", 0) for t in trades)
        
        # If no actual trades, there's nothing to activate
        # (bid record quantity is just what we offered, not what was accepted)
        if total_sold_mw == 0:
            self.logger.info("=" * 70)
            self.logger.info("NO ACTIVATION REQUIRED")
            self.logger.info("=" * 70)
            self.logger.info("No trades found for slot %s - %s", 
                           slot_start.strftime("%Y-%m-%d %H:%M"),
                           slot_end.strftime("%H:%M"))
            self.logger.info("No assets will be activated.")
            
            summary["status"] = "no_trades"
            summary["message"] = "No trades found for this slot - no activation performed"
            summary["trades"] = []
            summary["total_flexibility_sold_mw"] = 0
            summary["total_flexibility_sold_kw"] = 0
            summary["allocation"] = {}
            summary["activation_results"] = []

            # Restore all previously controlled assets
            if previous_state:
                self._queue_restores_for_previous_state(
                    previous_state, current_curtailed=set(), dry_run=dry_run,
                )
                if self.rabbitmq_publisher and self.rabbitmq_publisher.is_connected():
                    restore_slot_info = {
                        "fsp_id": self.fsp_id,
                        "slot_start": _format_aem_utc(slot_start),
                        "slot_end": _format_aem_utc(slot_end),
                        "dry_run": dry_run,
                    }
                    self.controller.publish_pending_commands(restore_slot_info, dry_run=dry_run)
                self._save_controlled_state({})

            # ========================================================
            # AUTONOMOUS MODE: Analyze price evolution when no trades
            # Even if a bid record exists, if there were no trades,
            # we want to show the price forecast for the upcoming hours
            # When dry_run=False, pre-heating commands will actually be sent
            # ========================================================
            if self.autonomous_enabled and self.price_predictor:
                self._run_autonomous_analysis(slot_start, summary, dry_run=dry_run)

            return summary
        
        total_sold_kw = total_sold_mw * 1000
        
        summary["trades"] = trades
        summary["total_flexibility_sold_mw"] = total_sold_mw
        summary["total_flexibility_sold_kw"] = total_sold_kw
        
        self.logger.info("-" * 70)
        self.logger.info("Total flexibility to deliver: %.3f MW (%.2f kW)", total_sold_mw, total_sold_kw)
        
        if total_sold_kw <= 0:
            self.logger.info("No flexibility to deliver - exiting")
            summary["status"] = "no_flexibility"
            return summary
        
        # Step 2: Allocate flexibility across allowed assets
        self.logger.info("-" * 70)
        if is_persistence_strategy:
            self.logger.info("Step 2: Building persistence activation plan from bid record assets...")
            planned_kw = sum(planned_bid_asset_flex_kw.values())

            if planned_kw <= 0:
                self.logger.error(
                    "Persistence strategy %s planned %.3f kW; failing closed",
                    strategy_info.get("id") if strategy_info else "unknown",
                    planned_kw
                )
                summary["status"] = "persistence_no_positive_bid_assets"
                summary["message"] = (
                    "Persistence strategy has no positive bid_record_assets."
                    "available_flexibility_kw rows - no activation performed"
                )
                summary["allocation"] = {}
                summary["allocations"] = {}

                if previous_state:
                    self._queue_restores_for_previous_state(
                        previous_state, current_curtailed=set(), dry_run=dry_run,
                    )
                    if self.rabbitmq_publisher and self.rabbitmq_publisher.is_connected():
                        restore_slot_info = {
                            "fsp_id": self.fsp_id,
                            "slot_start": _format_aem_utc(slot_start),
                            "slot_end": _format_aem_utc(slot_end),
                            "dry_run": dry_run,
                        }
                        self.controller.publish_pending_commands(restore_slot_info, dry_run=dry_run)
                    self._save_controlled_state({})

                return summary

            allocations, persistence_selection = self._build_persistence_activation_allocations(
                planned_bid_asset_flex_kw,
                total_sold_kw,
            )

            summary["allocation_source"] = "bid_record_assets.available_flexibility_kw"
            summary["persistence_planned_kw"] = persistence_selection["planned_kw"]
            summary["persistence_discrete_planned_total_kw"] = persistence_selection["discrete_planned_total"]
            summary["persistence_continuous_planned_total_kw"] = persistence_selection["continuous_planned_total"]
            summary["persistence_selected_discrete_total_kw"] = persistence_selection["selected_discrete_total"]
            summary["persistence_continuous_activation_total_kw"] = persistence_selection["continuous_activation_total"]
            summary["persistence_expected_delivered_kw"] = persistence_selection["expected_delivered_kw"]
            summary["persistence_under_delivery_kw"] = persistence_selection["under_delivery_kw"]

            self.logger.info("PERSISTENCE ACTIVATION PLAN FROM BID RECORD ASSETS")
            self.logger.info(
                "  Strategy: %s (%s)",
                strategy_info.get("id") if strategy_info else "unknown",
                strategy_info.get("name", "N/A") if strategy_info else "N/A",
            )
            self.logger.info("  allocation_source = bid_record_assets.available_flexibility_kw")
            self.logger.info("PERSISTENCE DISCRETE-AWARE ACTIVATION SELECTION")
            self.logger.info("  accepted_kw: %.3f", persistence_selection["accepted_kw"])
            self.logger.info("  planned_kw: %.3f", persistence_selection["planned_kw"])
            self.logger.info(
                "  discrete planned total: %.3f",
                persistence_selection["discrete_planned_total"]
            )
            self.logger.info(
                "  continuous planned total: %.3f",
                persistence_selection["continuous_planned_total"]
            )
            self.logger.info(
                "  selected discrete total: %.3f",
                persistence_selection["selected_discrete_total"]
            )
            self.logger.info(
                "  continuous activation total: %.3f",
                persistence_selection["continuous_activation_total"]
            )
            self.logger.info(
                "  expected delivered total: %.3f",
                persistence_selection["expected_delivered_kw"]
            )
            self.logger.info(
                "  under_delivery_kw: %.3f",
                persistence_selection["under_delivery_kw"]
            )
            selected_discrete_assets = [
                d["asset_id"]
                for d in persistence_selection["asset_details"]
                if d["selected"] and d["modulation_type"] == "discrete"
            ]
            self.logger.info(
                "  discrete subset selected: %s",
                selected_discrete_assets if selected_discrete_assets else []
            )
            for detail in persistence_selection["asset_details"]:
                self.logger.info(
                    "  %s: stored available_flexibility_kw=%.3f, type=%s, "
                    "modulation=%s, selected=%s, final activation_kw=%.3f, reason=%s",
                    detail["asset_id"],
                    detail["stored_kw"],
                    detail["asset_type"],
                    detail["modulation_type"],
                    "yes" if detail["selected"] else "no",
                    detail["activation_kw"],
                    detail["reason"],
                )
        else:
            self.logger.info("Step 2: Allocating flexibility across ALLOWED assets...")
            self.logger.info("  Allowed assets: %s", allowed_assets)
            self.logger.info("  allocation_source = generic_allocator")

            allocations = self.allocator.allocate_flexibility(
                total_sold_kw,
                allowed_assets=allowed_assets,  # Use assets from bid record
                strategy=allocation_strategy
            )
            summary["allocation_source"] = "generic_allocator"
        
        summary["allocations"] = allocations
        
        if not allocations:
            self.logger.warning("Could not allocate flexibility to any asset")
            summary["status"] = "allocation_failed"
            return summary
        
        # Log allocation details with modulation type info
        self.logger.info("-" * 70)
        self.logger.info("Allocation plan:")
        total_allocated = 0
        for asset_id, curtailment_kw in allocations.items():
            asset_config = self.asset_mapping.get(asset_id, {})
            asset_desc = asset_config.get("description", asset_id)
            
            # Determine modulation type for display
            mod_type = self._get_activation_modulation_type(asset_id)
            
            if mod_type == "discrete":
                capacity = asset_config.get("capacity_kw", curtailment_kw)
                # For discrete assets, curtailment = full capacity means switching OFF
                force_discrete_off = is_persistence_strategy and curtailment_kw > 0
                state = "OFF" if force_discrete_off or curtailment_kw >= capacity * 0.5 else "ON"
                self.logger.info(
                    "  %s (%s): %.2f kW [discrete → %s]", 
                    asset_id, asset_desc, curtailment_kw, state
                )
            else:
                capacity = float(asset_config.get("capacity_kw", 0) or 0)
                min_power = float(asset_config.get("min_power_kw", 0.0) or 0.0)
                reference_info = bid_asset_reference_power_by_id.get(asset_id, {})
                reference_power = reference_info.get("reference_power_kw")
                reference_source = reference_info.get("reference_power_source")
                if reference_power is None:
                    self.logger.warning(
                        "  %s (%s): %.2f kW [continuous -> activation will be "
                        "skipped; reference_power missing; nominal fallback disabled]",
                        asset_id, asset_desc, curtailment_kw,
                    )
                else:
                    target_power = min(
                        max(min_power, reference_power - curtailment_kw),
                        capacity,
                    )
                    self.logger.info(
                        "  %s (%s): %.2f kW [continuous -> limit %.2f kW; "
                        "reference %.2f kW source=%s]",
                        asset_id, asset_desc, curtailment_kw, target_power,
                        reference_power, reference_source,
                    )
            
            total_allocated += curtailment_kw
        
        self.logger.info("  Total to deliver: %.2f kW", total_allocated)
        
        # Show deviation from requested if applicable
        deviation = total_allocated - total_sold_kw
        if abs(deviation) > 0.01:
            if deviation > 0:
                self.logger.info(
                    "  Note: +%.2f kW over-delivery due to discrete asset constraints",
                    deviation
                )
        
        # Step 3: Send control commands
        self.logger.info("-" * 70)
        self.logger.info("Step 3: Sending control commands...")

        current_curtailed = set()
        deliverable_allocated = 0.0

        activation_current_cfg = _resolve_activation_current_config(strategy_obj, self.config)
        ev_comfort_cfg = _resolve_ev_comfort_guard_config(strategy_obj, self.config)
        current_slot_start_str = _format_aem_utc(slot_start)
        slot_duration = slot_end - slot_start
        # Per-asset comfort-guard bookkeeping for this run.
        ev_activation_meta = {}      # controlled EV -> {consecutive_activation_slots, control_sequence_start}
        newly_blocked_cooldown = {}  # EV blocked by the cap this slot -> cooldown state entry
        self.logger.info(
            "EV comfort guard config: "
            "max_consecutive_activation_slots=%d, cooldown_slots_after_max_activation=%d",
            ev_comfort_cfg["max_consecutive_activation_slots"],
            ev_comfort_cfg["cooldown_slots_after_max_activation"],
        )

        for asset_id, curtailment_kw in allocations.items():
            mod_type = self._get_activation_modulation_type(asset_id)
            force_discrete_off = (
                is_persistence_strategy
                and mod_type == "discrete"
                and curtailment_kw > 0
            )

            bid_ref_info = bid_asset_reference_power_by_id.get(asset_id, {})
            bid_reference_power_kw = bid_ref_info.get("reference_power_kw")
            bid_reference_source = bid_ref_info.get("reference_power_source")

            activation_current_power_kw = None

            asset_config = self.asset_mapping.get(asset_id, {})
            asset_type = asset_config.get("type", "unknown")

            is_discrete_ev = (
                asset_type == "ev_charger"
                and mod_type == "discrete"
                and len(asset_config.get("discrete_states_kw", [])) >= 2
            )

            # Discrete HP assets skip the comfort guard path entirely (ON/OFF).
            # Discrete EV chargers (strategy_11) and continuous EVs
            # (strategy_10) both use the recent-profile comfort guard.
            enters_ev_comfort_guard = (
                bid_reference_source == "recent_profile_baseline"
                and (mod_type != "discrete" or is_discrete_ev)
            )

            if is_discrete_ev:
                force_discrete_off = False

            if enters_ev_comfort_guard:
                if asset_type == "ev_charger":
                    prev_meta = previous_state.get(asset_id, {})
                    if not isinstance(prev_meta, dict):
                        prev_meta = {}
                    # Missing "state" means a legacy controlled entry.
                    prev_state_type = prev_meta.get("state", "controlled")

                    is_continuation = (
                        asset_id in previous_state
                        and prev_meta.get("slot_end") == current_slot_start_str
                    )

                    max_slots = ev_comfort_cfg["max_consecutive_activation_slots"]
                    cooldown_slots = ev_comfort_cfg["cooldown_slots_after_max_activation"]

                    # --- Comfort cooldown: block while the cooldown window is open ---
                    if prev_state_type == "cooldown":
                        cooldown_until = _parse_aem_utc(
                            prev_meta.get("cooldown_until_slot_start")
                        )
                        if cooldown_until is not None and slot_start < cooldown_until:
                            reason = (
                                "skipped: comfort cooldown active until "
                                f"{prev_meta.get('cooldown_until_slot_start')}"
                            )
                            self.logger.info(
                                "EV activation sequence for %s:\n"
                                "  state=cooldown\n"
                                "  cooldown_until_slot_start=%s\n"
                                "  current_slot_start=%s\n"
                                "  decision=skipped: comfort cooldown active",
                                asset_id,
                                prev_meta.get("cooldown_until_slot_start"),
                                current_slot_start_str,
                            )
                            summary["control_results"][asset_id] = self._build_ev_skip_result(
                                asset_id, asset_config, mod_type, curtailment_kw,
                                bid_reference_power_kw, bid_reference_source, reason,
                            )
                            continue
                        # Cooldown expired -> treat as a brand new activation.
                        self.logger.info(
                            "EV activation sequence for %s:\n"
                            "  state=cooldown\n"
                            "  cooldown_until_slot_start=%s\n"
                            "  current_slot_start=%s\n"
                            "  decision=cooldown expired; treating as new activation",
                            asset_id,
                            prev_meta.get("cooldown_until_slot_start"),
                            current_slot_start_str,
                        )
                        is_continuation = False

                    bid_ref_valid = (
                        bid_reference_power_kw is not None
                        and math.isfinite(bid_reference_power_kw)
                        and bid_reference_power_kw > 0
                    )
                    if is_continuation and not bid_ref_valid:
                        is_continuation = False
                        self.logger.warning(
                            "Consecutive-slot continuation disabled for %s: "
                            "bid_reference_power_kw=%s is invalid",
                            asset_id, bid_reference_power_kw,
                        )

                    # Resolve the previous consecutive-activation count.
                    if is_continuation and prev_state_type == "controlled":
                        prev_count = prev_meta.get("consecutive_activation_slots", 1)
                        try:
                            prev_count = int(prev_count)
                        except (TypeError, ValueError):
                            prev_count = 1
                        if prev_count < 1:
                            prev_count = 1
                    else:
                        prev_count = 0

                    # --- Max consecutive activation cap ---
                    if is_continuation and prev_count >= max_slots:
                        cooldown_until_dt = slot_start + cooldown_slots * slot_duration
                        cooldown_until_str = _format_aem_utc(cooldown_until_dt)
                        last_seq_start = prev_meta.get(
                            "control_sequence_start", prev_meta.get("slot_start")
                        )
                        reason = (
                            "skipped: maximum consecutive activation slots reached "
                            f"(previous_count={prev_count}, max={max_slots}); restoring "
                            f"and entering cooldown until {cooldown_until_str}"
                        )
                        self.logger.warning(
                            "EV activation sequence for %s:\n"
                            "  state=controlled\n"
                            "  continuation=True\n"
                            "  previous_consecutive_slots=%d\n"
                            "  max_consecutive_slots=%d\n"
                            "  cooldown_slots_after_max_activation=%d\n"
                            "  cooldown_until_slot_start=%s\n"
                            "  decision=skipped: maximum consecutive activation slots "
                            "reached; restoring and entering cooldown",
                            asset_id, prev_count, max_slots, cooldown_slots,
                            cooldown_until_str,
                        )
                        newly_blocked_cooldown[asset_id] = {
                            "state": "cooldown",
                            "asset_type": asset_type,
                            "cooldown_reason": "max_consecutive_activation_slots",
                            "cooldown_started_slot": current_slot_start_str,
                            "cooldown_until_slot_start": cooldown_until_str,
                            "cooldown_slots_after_max_activation": cooldown_slots,
                            "last_control_sequence_start": last_seq_start,
                            "last_consecutive_activation_slots": prev_count,
                            "strategy_id": strategy_info.get("id") if strategy_info else None,
                        }
                        summary["control_results"][asset_id] = self._build_ev_skip_result(
                            asset_id, asset_config, mod_type, curtailment_kw,
                            bid_reference_power_kw, bid_reference_source, reason,
                        )
                        # Not added to current_curtailed -> existing restore logic
                        # restores this controlled asset exactly once; the cooldown
                        # entry then suppresses further restores.
                        continue

                    # Activation allowed (new activation or continuation below cap).
                    if is_continuation and prev_count >= 1:
                        new_count = prev_count + 1
                        control_sequence_start = prev_meta.get(
                            "control_sequence_start",
                            prev_meta.get("slot_start", current_slot_start_str),
                        )
                        decision_label = "continuation allowed"
                    else:
                        new_count = 1
                        control_sequence_start = current_slot_start_str
                        decision_label = "activation allowed"

                    self.logger.info(
                        "EV activation sequence for %s:\n"
                        "  state=controlled\n"
                        "  continuation=%s\n"
                        "  previous_consecutive_slots=%d\n"
                        "  max_consecutive_slots=%d\n"
                        "  cooldown_slots_after_max_activation=%d\n"
                        "  new_consecutive_slots=%d\n"
                        "  control_sequence_start=%s\n"
                        "  decision=%s",
                        asset_id, is_continuation, prev_count, max_slots,
                        cooldown_slots, new_count, control_sequence_start,
                        decision_label,
                    )

                    skip_reason = self._resolve_recent_profile_activation_current(
                        asset_id=asset_id,
                        curtailment_kw=curtailment_kw,
                        bid_reference_power_kw=bid_reference_power_kw,
                        bid_reference_source=bid_reference_source,
                        activation_current_cfg=activation_current_cfg,
                        is_continuation=is_continuation,
                        is_discrete_ev=is_discrete_ev,
                    )
                    if isinstance(skip_reason, str):
                        summary["control_results"][asset_id] = self._build_ev_skip_result(
                            asset_id, asset_config, mod_type, curtailment_kw,
                            bid_reference_power_kw, bid_reference_source, skip_reason,
                        )
                        continue
                    else:
                        activation_current_power_kw = skip_reason
                        ev_activation_meta[asset_id] = {
                            "consecutive_activation_slots": new_count,
                            "control_sequence_start": control_sequence_start,
                        }
            elif not is_discrete_ev and mod_type != "discrete" and bid_reference_source and bid_reference_source != "recent_profile_baseline":
                self.logger.info(
                    "Continuous activation reference decision for %s:\n"
                    "  allocated_curtailment_kw=%.3f\n"
                    "  bid_reference_power_kw=%s\n"
                    "  bid_reference_source=%s\n"
                    "  selected_activation_reference_kw=%s\n"
                    "  decision=used bid reference because source is not recent_profile_baseline",
                    asset_id, curtailment_kw,
                    "%.3f" % bid_reference_power_kw if bid_reference_power_kw else "N/A",
                    bid_reference_source,
                    "%.3f" % bid_reference_power_kw if bid_reference_power_kw else "N/A",
                )

            result = self.controller.curtail_asset(
                asset_id,
                curtailment_kw,
                duration_minutes=15,
                dry_run=dry_run,
                force_discrete_off=force_discrete_off,
                reference_power_kw=bid_reference_power_kw,
                reference_power_source=bid_reference_source,
                activation_current_power_kw=activation_current_power_kw,
                strategy_cfg=strategy_obj.config if strategy_obj else None,
            )
            summary["control_results"][asset_id] = result
            if result.get("status") in ["success", "simulated", "queued"]:
                current_curtailed.add(asset_id)
                deliverable_allocated += curtailment_kw

        skipped_assets = [
            asset_id for asset_id, result in summary["control_results"].items()
            if result.get("status") == "skipped"
        ]
        if skipped_assets:
            summary["skipped_assets"] = skipped_assets
            summary["skipped_flexibility_kw"] = sum(
                allocations.get(asset_id, 0.0) for asset_id in skipped_assets
            )

        # Queue restore commands for previously controlled assets that are
        # no longer selected for curtailment in the current slot.
        if previous_state:
            self._queue_restores_for_previous_state(
                previous_state, current_curtailed=current_curtailed, dry_run=dry_run,
            )

        # Step 4: Publish commands to RabbitMQ (if configured)
        if self.rabbitmq_publisher and self.rabbitmq_publisher.is_connected():
            self.logger.info("-" * 70)
            self.logger.info("Step 4: Publishing commands to RabbitMQ...")
            self.logger.info("  Forwarder mode: %s", "DRY-RUN" if dry_run else "LIVE ACTUATION")

            slot_info = {
                "fsp_id": self.fsp_id,
                "slot_start": _format_aem_utc(slot_start),
                "slot_end": _format_aem_utc(slot_end),
                "total_flexibility_kw": total_sold_kw,
                "allocation_strategy": allocation_strategy,
                "dry_run": dry_run,
            }

            published = self.controller.publish_pending_commands(slot_info, dry_run=dry_run)
            summary["rabbitmq_published"] = published
            summary["forwarder_dry_run"] = dry_run
            self.logger.info("Published %d commands to RabbitMQ", published)

        # Persist next controlled/cooldown state for the next run.
        new_state = {}
        for asset_id in current_curtailed:
            ac = self.asset_mapping.get(asset_id, {})
            cr = summary["control_results"].get(asset_id, {})
            controlled_entry = {
                "state": "controlled",
                "asset_type": ac.get("type", "unknown"),
                "slot_start": _format_aem_utc(slot_start),
                "slot_end": _format_aem_utc(slot_end),
                "target_power_kw": cr.get("target_power_kw"),
                "allocated_curtailment_kw": cr.get("allocated_curtailment_kw"),
                "reference_power_kw": cr.get("reference_power_kw"),
                "reference_power_source": cr.get("reference_power_source"),
                "strategy_id": strategy_info.get("id") if strategy_info else None,
            }
            ev_meta = ev_activation_meta.get(asset_id)
            if ev_meta:
                controlled_entry["consecutive_activation_slots"] = ev_meta[
                    "consecutive_activation_slots"
                ]
                controlled_entry["control_sequence_start"] = ev_meta[
                    "control_sequence_start"
                ]
            new_state[asset_id] = controlled_entry

        # Cooldown entries created this slot (assets blocked by the comfort cap).
        for asset_id, cd_entry in newly_blocked_cooldown.items():
            new_state[asset_id] = cd_entry

        # Carry over cooldown entries that are still within their cooldown window
        # so the cooldown persists across its M slots without repeated restores.
        # Expired cooldown entries are intentionally dropped (pruned).
        for asset_id, meta in previous_state.items():
            if not isinstance(meta, dict) or meta.get("state") != "cooldown":
                continue
            if asset_id in new_state:
                continue
            cooldown_until = _parse_aem_utc(meta.get("cooldown_until_slot_start"))
            if cooldown_until is not None and slot_start < cooldown_until:
                new_state[asset_id] = meta

        self._save_controlled_state(new_state)
        
        # Step 5: Save activation records to database
        if self.bid_repo:
            self.logger.info("-" * 70)
            self.logger.info("Step 5: Saving activation records to database...")
            
            # Get bid record ID if available
            bid_record = summary.get("bid_record")
            bid_record_id = bid_record.get("id") if bid_record else None
            
            # Build activation records
            activation_records = []
            for asset_id, curtailment_kw in allocations.items():
                asset_info = self.asset_mapping.get(asset_id, {})
                control_result = summary["control_results"].get(asset_id, {})
                
                activation_records.append({
                    "asset_id": asset_id,
                    "power_kw": curtailment_kw,
                    "description": asset_info.get("description", asset_id),
                    "asset_type": asset_info.get("type"),
                    "percentage": control_result.get("percentage"),
                    "status": control_result.get("status", "unknown"),
                    "reference_power_kw": control_result.get("reference_power_kw"),
                    "reference_power_source": control_result.get("reference_power_source"),
                    "allocated_curtailment_kw": control_result.get("allocated_curtailment_kw"),
                    "computed_target_power_kw": control_result.get("computed_target_power_kw"),
                })
            
            try:
                saved_count = self.bid_repo.save_asset_activations_batch(
                    fsp_id=self.fsp_id,
                    slot_start=slot_start,
                    slot_end=slot_end,
                    activations=activation_records,
                    allocation_strategy=allocation_strategy,
                    dry_run=dry_run,
                    bid_record_id=bid_record_id
                )
                self.logger.info("Saved %d activation records", saved_count)
            except Exception as e:
                self.logger.warning("Could not save activation records: %s", str(e))
        
        # Summary
        self.logger.info("=" * 70)
        self.logger.info("FLEXIBILITY ACTIVATION COMPLETE")
        self.logger.info("=" * 70)
        
        # Count successful statuses: "success", "simulated", or "queued" (RabbitMQ)
        successful = sum(1 for r in summary["control_results"].values() 
                        if r.get("status") in ["success", "simulated", "queued"])
        failed = len(summary["control_results"]) - successful
        
        # Adjust message based on whether RabbitMQ is used
        if self.rabbitmq_publisher and self.rabbitmq_publisher.is_connected():
            self.logger.info("Commands queued: %d (actuation delegated to forwarder)", successful)
        else:
            self.logger.info("Assets controlled: %d successful, %d failed", successful, failed)
        self.logger.info(
            "Total flexibility deliverable: %.2f kW (%.3f MW)",
            deliverable_allocated,
            deliverable_allocated / 1000,
        )
        if skipped_assets:
            self.logger.error(
                "Continuous activation skipped for %d asset(s); skipped flexibility %.2f kW",
                len(skipped_assets),
                summary.get("skipped_flexibility_kw", 0.0),
            )
        
        summary["total_flexibility_deliverable_kw"] = deliverable_allocated
        summary["total_flexibility_deliverable_mw"] = deliverable_allocated / 1000

        if successful == 0 and summary["control_results"]:
            summary["status"] = "activation_failed"
            summary["message"] = (
                "No deliverable flexibility after continuous reference-power validation"
            )
        elif failed > 0:
            summary["status"] = "partial_success"
        elif self.rabbitmq_publisher and self.rabbitmq_publisher.is_connected():
            summary["status"] = "queued"  # All commands queued for forwarder
        
        return summary


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Flexibility Manager - Activate sold flexibility on assets",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Dry-run for upcoming slot (next 15-min boundary)
  python flexi_manager.py --fsp supsi01 --dry-run

  # Dry-run for slot 30 minutes ago
  python flexi_manager.py --fsp supsi01 --offset 30m --dry-run

  # Dry-run for slot 2 hours ago
  python flexi_manager.py --fsp supsi01 --offset 2h --dry-run

  # Dry-run for specific slot (exact time)
  python flexi_manager.py --fsp supsi01 --slot "2026-01-13T16:00:00" --dry-run

  # List available strategies
  python flexi_manager.py --fsp supsi01 --list-strategies

  # Live activation (CAUTION!)
  python flexi_manager.py --fsp supsi01 --live

  # Use priority allocation (HP first)
  python flexi_manager.py --fsp supsi01 --dry-run --allocation priority
        """
    )
    
    parser.add_argument(
        "--config_file", "-c",
        default="../conf/test_fm01_aem.json",
        help="Path to configuration file (default: ../conf/test_fm01_aem.json)"
    )
    parser.add_argument(
        "--fsp", "-f",
        required=True,
        help="FSP identifier (e.g., supsi01)"
    )
    parser.add_argument(
        "--slot", "-s",
        help="Target slot start time in ISO format (default: next 15-min slot)"
    )
    parser.add_argument(
        "--offset", "-t",
        help="Time offset from now (e.g., '30m' for 30 minutes ago, '2h' for 2 hours ago). "
             "The slot is aligned to the 15-minute boundary containing (now - offset)."
    )
    parser.add_argument(
        "--dry-run", "-d",
        action="store_true",
        default=True,
        help="Simulate only, don't send actual commands (default)"
    )
    parser.add_argument(
        "--live", "-l",
        action="store_true",
        help="Actually send control commands (CAUTION!)"
    )
    parser.add_argument(
        "--simulate-sold-mw",
        type=float,
        default=None,
        help="DRY-RUN ONLY: simulate accepted/sold quantity in MW for activation-path testing."
    )
    parser.add_argument(
        "--allow-market-ledger-fallback",
        action="store_true",
        default=False,
        help=(
            "Allow fallback to local public.market_ledger when no NODES accepted trades are found. "
            "WARNING: ledger rows may represent posted orders, not accepted trades."
        )
    )
    parser.add_argument(
        "--allocation", "-a",
        choices=["modulation_aware", "proportional", "priority", "cost_optimal"],
        default="modulation_aware",
        help="Allocation strategy (default: modulation_aware)"
    )
    parser.add_argument(
        "--fallback-strategy",
        help="Strategy to use when no bid record exists (e.g., strategy_4). "
             "Determines which assets can be activated."
    )
    parser.add_argument(
        "--list-strategies",
        action="store_true",
        help="List available strategies and exit"
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Logging level (default: INFO)"
    )
    parser.add_argument(
        "--log_file",
        help="Path to log file. If provided, logs will be written to this file in addition to console."
    )
    parser.add_argument(
        "--output", "-o",
        help="Output file for JSON summary"
    )
    parser.add_argument(
        "--state-file",
        default=None,
        help="Path to the controlled-asset state file "
             "(default: logs/flexi_manager_state.json)"
    )
    
    # RabbitMQ arguments
    parser.add_argument(
        "--rabbitmq",
        action="store_true",
        help="Enable RabbitMQ message publishing for command forwarding"
    )
    parser.add_argument(
        "--rabbitmq-host",
        default=None,
        help="RabbitMQ server hostname (default: rabbitMQ.host from connectionsFile, else localhost)"
    )
    parser.add_argument(
        "--rabbitmq-port",
        type=int,
        default=None,
        help="RabbitMQ server port (default: rabbitMQ.port from connectionsFile, else 5672)"
    )
    parser.add_argument(
        "--rabbitmq-user",
        default=None,
        help="RabbitMQ username (default: rabbitMQ.username from connectionsFile, else guest)"
    )
    parser.add_argument(
        "--rabbitmq-pass",
        default=None,
        help="RabbitMQ password (default: rabbitMQ.password from connectionsFile, else guest)"
    )
    parser.add_argument(
        "--rabbitmq-vhost",
        default=None,
        help="RabbitMQ virtual host (default: rabbitMQ.virtualHost from connectionsFile, else /)"
    )
    parser.add_argument(
        "--rabbitmq-exchange",
        default=None,
        help=(
            "Deprecated and ignored. RabbitMQ message exchanges are read only "
            "from destination sections in the connections file."
        )
    )
    
    # Autonomous mode arguments
    parser.add_argument(
        "--autonomous",
        action="store_true",
        help="Enable autonomous mode (analyze price evolution when no bid exists)"
    )
    parser.add_argument(
        "--no-autonomous",
        action="store_true",
        help="Disable autonomous mode (overrides config file)"
    )
    parser.add_argument(
        "--autonomous-lookahead",
        type=float,
        help="Hours to look ahead for price prediction (default: from config or 3)"
    )
    parser.add_argument(
        "--autonomous-history",
        type=int,
        help="Days of history to analyze (default: from config or 7)"
    )
    
    args = parser.parse_args()
    
    # Setup logging
    logger = setup_logging(args.log_level, args.log_file)

    # Determine dry-run mode
    dry_run = not args.live

    if args.simulate_sold_mw is not None and not dry_run:
        parser.error("--simulate-sold-mw can only be used with --dry-run")
    if args.simulate_sold_mw is not None and args.simulate_sold_mw < 0:
        parser.error("--simulate-sold-mw must be non-negative")
    
    # Load configuration
    config_path = args.config_file
    if not os.path.isabs(config_path):
        config_path = os.path.join(os.path.dirname(__file__), config_path)
    
    try:
        with open(config_path, "r") as f:
            config = json.load(f)
    except FileNotFoundError:
        logger.error("Configuration file not found: %s", config_path)
        sys.exit(1)
    except json.JSONDecodeError as e:
        logger.error("Invalid JSON in configuration file: %s", str(e))
        sys.exit(1)
    
    # Override autonomous settings from command line
    if "autonomous" not in config:
        config["autonomous"] = {}
    
    if args.autonomous:
        config["autonomous"]["enabled"] = True
    elif args.no_autonomous:
        config["autonomous"]["enabled"] = False
    
    if args.autonomous_lookahead is not None:
        config["autonomous"]["lookahead_hours"] = args.autonomous_lookahead
    
    if args.autonomous_history is not None:
        config["autonomous"]["historical_days"] = args.autonomous_history
    
    # Handle --list-strategies before validating FSP
    if args.list_strategies:
        strategy_manager = StrategyManager(config, logger)
        strategy_manager.print_all_strategies()
        sys.exit(0)
    
    # Validate FSP
    if args.fsp not in config.get("fm", {}).get("actors", {}).get("fsps", {}):
        logger.error("FSP '%s' not found in configuration", args.fsp)
        available = list(config.get("fm", {}).get("actors", {}).get("fsps", {}).keys())
        logger.error("Available FSPs: %s", available)
        sys.exit(1)
    
    # Load connections for NODES interface and database
    nodes_interface = None
    pg_interface = None
    bid_repo = None
    demand_repo = None
    nodes_authenticated = False
    conns = {}
    
    try:
        conns_path = config.get("connectionsFile", "../conf/private/conns.json")
        if not os.path.isabs(conns_path):
            conns_path = os.path.join(os.path.dirname(config_path), conns_path)
        
        with open(conns_path, "r") as f:
            conns = json.load(f)
        
        # Initialize NODES interface with proper authentication
        nodes_cfg = conns.get("nodesAPI", {})
        nodes_interface = NodesInterface(nodes_cfg, logger)
        
        # Get FSP config and set token for authentication
        fsp_config = config["fm"]["actors"]["fsps"][args.fsp]
        try:
            nodes_interface.set_token(fsp_config)
            # Verify token works
            user_info = nodes_interface.get_user_info()
            if user_info:
                nodes_authenticated = True
                logger.info("NODES API authenticated successfully")
            else:
                logger.warning("NODES API authentication failed - will run without market data")
        except Exception as e:
            logger.warning("Could not authenticate with NODES API: %s", str(e))
            logger.warning("Will run without market data (using bid record quantities)")
        
        # Initialize PostgreSQL connection and repositories
        pg_cfg = conns.get("postgreSQL", {})
        demand_repo = None
        if pg_cfg:
            try:
                pg_interface = PostgreSQLInterface(pg_cfg, logger)
                bid_repo = BidRecordRepository(pg_interface, logger)
                demand_repo = DemandRecordRepository(pg_interface, logger)
                logger.info("Connected to PostgreSQL, bid and demand record repositories ready")
            except Exception as e:
                logger.warning("Could not connect to PostgreSQL: %s", str(e))
                logger.warning("Bid/demand record loading will not be available")
        
    except FileNotFoundError:
        logger.warning("Connections file not found - running in offline mode")
    except Exception as e:
        logger.warning("Error loading connections: %s - running in offline mode", str(e))

    # Initialize InfluxDB client for activation-time measurement queries
    influx_client = None
    influx_cfg = conns.get("influxDB", {})
    if influx_cfg and INFLUXDB_AVAILABLE:
        try:
            influx_client = InfluxDBClient(
                host=influx_cfg["host"],
                port=influx_cfg["port"],
                username=influx_cfg["user"],
                password=influx_cfg["password"],
                database=influx_cfg["database"],
            )
            logger.info("InfluxDB client initialized for activation-current measurements")
        except Exception as e:
            logger.warning("Could not initialize InfluxDB client: %s", str(e))
    elif not INFLUXDB_AVAILABLE:
        logger.warning(
            "influxdb package not installed; activation-current measurement "
            "for recent-profile EV assets will not be available"
        )
    if influx_cfg:
        config.setdefault("influxDB", {}).update(influx_cfg)

    rabbitmq_cfg = conns.get("rabbitMQ", {})
    
    # Initialize RabbitMQ publisher if requested
    rabbitmq_publisher = None
    if args.rabbitmq:
        if not RABBITMQ_AVAILABLE:
            logger.error("RabbitMQ requested but pika library not installed. Run: pip install pika")
            sys.exit(1)
        
        try:
            rabbitmq_host = args.rabbitmq_host or rabbitmq_cfg.get("host", "localhost")
            rabbitmq_port_value = (
                args.rabbitmq_port
                if args.rabbitmq_port is not None
                else rabbitmq_cfg.get("port", 5672)
            )
            rabbitmq_port = int(rabbitmq_port_value or 5672)
            rabbitmq_user = args.rabbitmq_user or rabbitmq_cfg.get("username", "guest")
            rabbitmq_password = args.rabbitmq_pass or rabbitmq_cfg.get("password", "guest")
            rabbitmq_vhost = args.rabbitmq_vhost or rabbitmq_cfg.get("virtualHost", "/")
            available_sections = _available_rabbit_destination_sections(rabbitmq_cfg)

            logger.info(
                "RabbitMQ connection: host=%s, port=%d, vhost=%s",
                rabbitmq_host,
                rabbitmq_port,
                rabbitmq_vhost,
            )
            logger.info(
                "RabbitMQ destination sections available: %s",
                ", ".join(available_sections) if available_sections else "none",
            )
            if args.rabbitmq_exchange:
                logger.warning(
                    "--rabbitmq-exchange is ignored; RabbitMQ exchanges are resolved "
                    "only from rabbitMQ.realAssetCommands, rabbitMQ.simulatedAssetCommands, "
                    "or rabbitMQ.simulatedAssetMeasures"
                )

            rabbitmq_publisher = RabbitMQPublisher(
                host=rabbitmq_host,
                port=rabbitmq_port,
                username=rabbitmq_user,
                password=rabbitmq_password,
                virtual_host=rabbitmq_vhost,
                logger=logger
            )
            if rabbitmq_publisher.connect():
                logger.info("RabbitMQ publisher initialized successfully")
            else:
                logger.warning("Could not connect to RabbitMQ - continuing without message publishing")
                rabbitmq_publisher = None
        except Exception as e:
            logger.warning("Failed to initialize RabbitMQ publisher: %s", str(e))
            rabbitmq_publisher = None
    
    # Create and run flexibility manager
    if nodes_interface or bid_repo:
        manager = FlexibilityManager(
            config, args.fsp, nodes_interface, bid_repo, logger,
            nodes_authenticated=nodes_authenticated,
            rabbitmq_publisher=rabbitmq_publisher,
            rabbitmq_config=rabbitmq_cfg,
            demand_repo=demand_repo,
            state_file=args.state_file,
            influx_client=influx_client,
        )
    else:
        # Create a mock manager for testing
        logger.warning("Running without NODES and database connections")
        
        class MockNodesInterface:
            def __init__(self):
                self.cfg = {"mainEndpoint": ""}
            def get_request(self, endpoint, params=None):
                return []
        
        manager = FlexibilityManager(
            config, args.fsp, MockNodesInterface(), None, logger,
            nodes_authenticated=False,
            rabbitmq_publisher=rabbitmq_publisher,
            rabbitmq_config=rabbitmq_cfg,
            demand_repo=None,
            state_file=args.state_file,
            influx_client=influx_client,
        )
    
    # Determine slot override from --slot or --offset
    slot_override = args.slot
    
    if args.offset:
        if args.slot:
            logger.warning("Both --slot and --offset provided; --slot takes precedence")
        else:
            try:
                offset = parse_time_offset(args.offset)
                slot_override = calculate_slot_from_offset(offset)
                logger.info("Using offset '%s' -> slot: %s", args.offset, slot_override)
            except ValueError as e:
                logger.error("Invalid offset: %s", str(e))
                sys.exit(1)
    
    # Run
    summary = manager.run(
        slot_override=slot_override,
        dry_run=dry_run,
        allocation_strategy=args.allocation,
        fallback_strategy=args.fallback_strategy,
        simulate_sold_mw=args.simulate_sold_mw,
        allow_market_ledger_fallback=args.allow_market_ledger_fallback
    )
    
    # Output summary
    if args.output:
        with open(args.output, "w") as f:
            json.dump(summary, f, indent=2)
        logger.info("Summary written to %s", args.output)
    
    # Cleanup RabbitMQ connection
    if rabbitmq_publisher:
        rabbitmq_publisher.disconnect()
    
    # Exit code based on status
    if summary["status"] in ["success", "queued", "no_flexibility", "no_trades", "no_bid_record"]:
        sys.exit(0)
    elif summary["status"] == "partial_success":
        sys.exit(1)
    else:
        sys.exit(2)


if __name__ == "__main__":
    main()
