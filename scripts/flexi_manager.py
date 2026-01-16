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

from classes.nodes_interface import NODESInterface as NodesInterface
from classes.postgresql_interface import PostgreSQLInterface
from classes.bid_record_repository import BidRecordRepository
from classes.bidding_strategy import BiddingStrategy, StrategyManager


# =============================================================================
# RABBITMQ PUBLISHER
# =============================================================================

class RabbitMQPublisher:
    """
    Publishes control commands and measurements to RabbitMQ for forwarding.
    
    This enables decoupling of command generation (flexi_manager) from
    command actuation (forwarder), allowing for better scalability and
    reliable message delivery.
    
    Exchange: flexi_commands (topic exchange)
    Routing keys:
        - commands.{asset_type}.{asset_id}   - Control commands
        - measurements.{asset_type}.{asset_id} - Measurement data
    """
    
    DEFAULT_EXCHANGE = "flexi_commands"
    DEFAULT_QUEUE_COMMANDS = "asset_commands"
    DEFAULT_QUEUE_MEASUREMENTS = "asset_measurements"
    
    def __init__(
        self,
        host: str = "localhost",
        port: int = 5672,
        username: str = "guest",
        password: str = "guest",
        virtual_host: str = "/",
        exchange: str = None,
        logger: logging.Logger = None
    ):
        """
        Initialize RabbitMQ publisher.
        
        :param host: RabbitMQ server hostname
        :param port: RabbitMQ server port
        :param username: RabbitMQ username
        :param password: RabbitMQ password
        :param virtual_host: RabbitMQ virtual host
        :param exchange: Exchange name (default: flexi_commands)
        :param logger: Logger instance
        """
        if not RABBITMQ_AVAILABLE:
            raise RuntimeError("pika library not installed. Run: pip install pika")
        
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.virtual_host = virtual_host
        self.exchange = exchange or self.DEFAULT_EXCHANGE
        self.logger = logger or logging.getLogger(__name__)
        
        self.connection = None
        self.channel = None
        self._connected = False
    
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
            
            # Declare exchange (topic type for flexible routing)
            self.channel.exchange_declare(
                exchange=self.exchange,
                exchange_type='topic',
                durable=True
            )
            
            # Declare queues
            self.channel.queue_declare(
                queue=self.DEFAULT_QUEUE_COMMANDS,
                durable=True
            )
            self.channel.queue_declare(
                queue=self.DEFAULT_QUEUE_MEASUREMENTS,
                durable=True
            )
            
            # Bind queues to exchange with routing patterns
            self.channel.queue_bind(
                exchange=self.exchange,
                queue=self.DEFAULT_QUEUE_COMMANDS,
                routing_key="commands.#"
            )
            self.channel.queue_bind(
                exchange=self.exchange,
                queue=self.DEFAULT_QUEUE_MEASUREMENTS,
                routing_key="measurements.#"
            )
            
            self._connected = True
            self.logger.info(
                "Connected to RabbitMQ at %s:%d (exchange: %s)",
                self.host, self.port, self.exchange
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
    
    def publish_command(
        self,
        asset_id: str,
        asset_type: str,
        command_type: str,
        payload: dict,
        priority: int = 5
    ) -> bool:
        """
        Publish a control command to RabbitMQ.
        
        :param asset_id: Asset identifier
        :param asset_type: Asset type (heat_pump, ev_charger, etc.)
        :param command_type: Command type (curtail, restore, set_power, etc.)
        :param payload: Command payload dictionary
        :param priority: Message priority (0-9, higher = more urgent)
        :return: True if published successfully
        """
        if not self.is_connected():
            self.logger.warning("Not connected to RabbitMQ - cannot publish command")
            return False
        
        routing_key = f"commands.{asset_type}.{asset_id}"
        
        message = {
            "message_type": "command",
            "asset_id": asset_id,
            "asset_type": asset_type,
            "command_type": command_type,
            "payload": payload,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "priority": priority
        }
        
        try:
            self.channel.basic_publish(
                exchange=self.exchange,
                routing_key=routing_key,
                body=json.dumps(message),
                properties=pika.BasicProperties(
                    delivery_mode=2,  # Persistent message
                    content_type='application/json',
                    priority=priority
                )
            )
            self.logger.debug(
                "Published command to %s: %s -> %s",
                routing_key, command_type, asset_id
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
        payload: dict
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
        
        routing_key = f"measurements.{asset_type}.{asset_id}"
        
        message = {
            "message_type": "measurement",
            "asset_id": asset_id,
            "asset_type": asset_type,
            "measurement_type": measurement_type,
            "payload": payload,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        
        try:
            self.channel.basic_publish(
                exchange=self.exchange,
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
        
        # Optionally publish a batch header
        if slot_info:
            batch_header = {
                "message_type": "batch_start",
                "slot_info": slot_info,
                "command_count": len(commands),
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
            try:
                self.channel.basic_publish(
                    exchange=self.exchange,
                    routing_key="commands.batch.header",
                    body=json.dumps(batch_header),
                    properties=pika.BasicProperties(
                        delivery_mode=2,
                        content_type='application/json'
                    )
                )
            except Exception as e:
                self.logger.warning("Failed to publish batch header: %s", str(e))
        
        for cmd in commands:
            if self.publish_command(
                asset_id=cmd.get("asset_id"),
                asset_type=cmd.get("asset_type"),
                command_type=cmd.get("command_type"),
                payload=cmd.get("payload", {}),
                priority=cmd.get("priority", 5)
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
        rabbitmq_publisher: 'RabbitMQPublisher' = None
    ):
        self.asset_mapping = asset_mapping
        self.logger = logger
        self.control_results = {}
        self.rabbitmq_publisher = rabbitmq_publisher
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
    
    def curtail_asset(
        self, 
        asset_id: str, 
        curtailment_kw: float, 
        duration_minutes: int = 15,
        dry_run: bool = True
    ) -> Dict:
        """
        Send curtailment command to an asset.
        
        For discrete assets: Sends ON/OFF commands based on curtailment threshold.
        For continuous assets: Sends specific power reduction values.
        
        :param asset_id: Asset identifier (e.g., "ECM97.1")
        :param curtailment_kw: Amount of power to reduce (kW)
        :param duration_minutes: Duration of curtailment
        :param dry_run: If True, only log what would be done
        :return: Result dictionary with status and details
        """
        asset_config = self.asset_mapping.get(asset_id, {})
        asset_type = asset_config.get("type", "unknown")
        description = asset_config.get("description", asset_id)
        capacity_kw = asset_config.get("capacity_kw", 0)
        modulation_type = self._get_modulation_type(asset_config)
        
        # Calculate curtailment percentage
        curtailment_pct = (curtailment_kw / capacity_kw * 100) if capacity_kw > 0 else 0
        
        # For discrete assets, determine the actual state to set
        if modulation_type == "discrete":
            state_name, target_power_kw = self._determine_discrete_state(asset_config, curtailment_kw)
            actual_curtailment_kw = capacity_kw - target_power_kw
        else:
            state_name = None
            target_power_kw = max(0, capacity_kw - curtailment_kw)
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
        
        # Build command payload for RabbitMQ
        command_payload = {
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
        
        # Queue command for RabbitMQ (always, regardless of dry_run)
        # Note: slot_start/slot_end and dry_run will be added by publish_pending_commands
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
                    self._control_heat_pump(asset_id, asset_config, curtailment_kw, duration_minutes)
                elif asset_type == "ev_charger":
                    self._control_ev_charger(asset_id, asset_config, curtailment_kw, duration_minutes)
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
        duration_minutes: int
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
        capacity_kw = config.get("capacity_kw", 0)
        
        # Determine command based on modulation type
        if modulation_type == "discrete":
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
            target_power_kw = max(0, capacity_kw - curtailment_kw)
            command_payload = {
                "command": "set_power",
                "target_power_kw": target_power_kw,
                "reduction_kw": curtailment_kw,
                "duration_minutes": duration_minutes,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
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
        duration_minutes: int
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
        capacity_kw = config.get("capacity_kw", 11.0)
        min_power_kw = config.get("min_power_kw", 0.0)
        modulation_type = self._get_modulation_type(config)
        
        # Calculate new charging limit (continuous modulation)
        # Respect minimum power setting (some chargers have minimum charging power)
        target_power_kw = max(min_power_kw, capacity_kw - curtailment_kw)
        
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
        
        :param asset_id: Asset identifier
        :param dry_run: If True, only log what would be done
        :return: Result dictionary
        """
        asset_config = self.asset_mapping.get(asset_id, {})
        description = asset_config.get("description", asset_id)
        asset_type = asset_config.get("type", "unknown")
        
        # Queue restore command for RabbitMQ
        if self.rabbitmq_publisher:
            restore_payload = {
                "asset_id": asset_id,
                "description": description,
                "asset_type": asset_type,
                "action": "restore",
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
            self._pending_commands.append({
                "asset_id": asset_id,
                "asset_type": asset_type,
                "command_type": "restore",
                "payload": restore_payload,
                "priority": 5
            })
        
        if dry_run:
            self.logger.info("[DRY-RUN] Would restore %s (%s) to normal operation", asset_id, description)
            return {"asset_id": asset_id, "status": "simulated", "action": "restore"}
        else:
            self.logger.info("Restoring %s (%s) to normal operation", asset_id, description)
            # TODO: Implement actual restore logic
            return {"asset_id": asset_id, "status": "success", "action": "restore"}
    
    def publish_pending_commands(self, slot_info: dict = None, dry_run: bool = True) -> int:
        """
        Publish all pending commands to RabbitMQ.
        
        This method should be called after all curtail_asset calls to
        batch-publish commands to the message broker.
        
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
        
        self.logger.info("Publishing %d commands to RabbitMQ (dry_run=%s)...", 
                        len(self._pending_commands), dry_run)
        
        # Add slot_start, slot_end, and dry_run to each command's payload
        for cmd in self._pending_commands:
            cmd["payload"]["dry_run"] = dry_run
            if slot_info:
                cmd["payload"]["slot_start"] = slot_info.get("slot_start")
                cmd["payload"]["slot_end"] = slot_info.get("slot_end")
        
        published = self.rabbitmq_publisher.publish_batch_commands(
            self._pending_commands,
            slot_info=slot_info
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
        
        # Query trades from NODES
        try:
            # Format times for API
            period_from = slot_start.strftime("%Y-%m-%dT%H:%M:%SZ")
            period_to = slot_end.strftime("%Y-%m-%dT%H:%M:%SZ")
            
            # Build endpoint with query params
            endpoint = (
                f"{self.nodes.cfg['mainEndpoint']}trades"
                f"?organizationId={organization_id}"
                f"&periodFrom={period_from}"
                f"&periodTo={period_to}"
                f"&status=Accepted"
            )
            
            response = self.nodes.get_request(endpoint)
            
            # Handle paginated response (dict with 'items' key)
            if response:
                if isinstance(response, dict):
                    trades = response.get("items", [])
                elif isinstance(response, list):
                    trades = response
                else:
                    trades = []
                
                # Filter for sell trades (FSP sells flexibility)
                sell_trades = [
                    t for t in trades 
                    if t.get("side") == "Sell"
                ]
                self.logger.info("Found %d accepted sell trades for this slot", len(sell_trades))
                return sell_trades
            else:
                self.logger.info("No trades found for this slot")
                return []
                
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
# MAIN FLEXIBILITY MANAGER
# =============================================================================

class FlexibilityManager:
    """
    Main class coordinating flexibility activation.
    
    Key feature: Uses bid records from PostgreSQL database to know which strategy
    was used and which assets should be activated.
    """
    
    def __init__(
        self,
        config: dict,
        fsp_id: str,
        nodes_interface: NodesInterface,
        bid_repo: BidRecordRepository,
        logger: logging.Logger,
        nodes_authenticated: bool = False,
        rabbitmq_publisher: 'RabbitMQPublisher' = None
    ):
        self.config = config
        self.fsp_id = fsp_id
        self.fsp_config = config["fm"]["actors"]["fsps"].get(fsp_id, {})
        self.asset_mapping = config.get("asset_mapping", {})
        self.logger = logger
        self.bid_repo = bid_repo
        self.nodes_authenticated = nodes_authenticated
        self.rabbitmq_publisher = rabbitmq_publisher
        
        # Get FSP's allowed assets (default from config)
        self.fsp_assets = self.fsp_config.get("assets", list(self.asset_mapping.keys()))
        
        # Initialize strategy manager to derive assets from strategy if needed
        self.strategy_manager = StrategyManager(config, logger)
        
        # Initialize components
        self.market_handler = MarketResultsHandler(nodes_interface, logger)
        self.allocator = FlexibilityAllocator(self.asset_mapping, logger)
        self.controller = AssetController(self.asset_mapping, logger, rabbitmq_publisher)
        self.bid_handler = BidRecordHandler(bid_repo, logger)
        
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
    
    def run(
        self,
        slot_override: str = None,
        dry_run: bool = True,
        allocation_strategy: str = "modulation_aware",
        fallback_strategy: str = None
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
        :return: Summary of actions taken
        """
        slot_start, slot_end = self.get_target_slot(slot_override)
        
        self.logger.info("=" * 70)
        self.logger.info("FLEXIBILITY MANAGER - %s", self.fsp_id)
        self.logger.info("=" * 70)
        self.logger.info("Target slot: %s - %s", 
                        slot_start.strftime("%Y-%m-%d %H:%M"),
                        slot_end.strftime("%H:%M"))
        self.logger.info("Mode: %s", "DRY-RUN" if dry_run else "LIVE")
        self.logger.info("Allocation strategy: %s", allocation_strategy)
        self.logger.info("-" * 70)
        
        summary = {
            "fsp_id": self.fsp_id,
            "slot_start": slot_start.isoformat(),
            "slot_end": slot_end.isoformat(),
            "dry_run": dry_run,
            "allocation_strategy": allocation_strategy,
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
        
        # Step 0: Check for bid record from trader_fsp.py
        self.logger.info("Step 0: Checking for bid record...")
        bid_record = self.bid_handler.get_bid_record(self.fsp_id, slot_start)
        
        if bid_record:
            summary["bid_record"] = bid_record
            strategy_info = self.bid_handler.get_strategy_info(bid_record)
            allowed_assets = self.bid_handler.get_allowed_assets(bid_record)
            bid_quantity_mw = self.bid_handler.get_total_quantity(bid_record)
            
            if strategy_info and strategy_info.get("id"):
                self.logger.info("  Strategy: %s (%s)", 
                               strategy_info.get("id"), 
                               strategy_info.get("name", "N/A"))
                summary["strategy_used"] = strategy_info.get("id")
            else:
                self.logger.info("  Strategy: None (simple mode)")
            
            if allowed_assets:
                self.logger.info("  Allowed assets from bid record: %s", allowed_assets)
                summary["allowed_assets"] = allowed_assets
            else:
                # No explicit assets stored - try to derive from strategy
                self.logger.warning("  No explicit assets in bid record")
                
                if strategy_info and strategy_info.get("id"):
                    # Derive assets from strategy definition
                    strategy_obj = self.strategy_manager.get_strategy(strategy_info.get("id"))
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
            
            return summary
        
        # Step 1: Query market results
        self.logger.info("-" * 70)
        self.logger.info("Step 1: Querying market results...")
        
        trades = []
        settlements = []
        
        # Try local market_ledger FIRST (faster and more reliable)
        player_name = self.fsp_config.get("name", self.fsp_id)
        local_trades = self.bid_handler.get_trades_from_ledger(player_name, slot_start)
        if local_trades:
            self.logger.info("Found trades in local market_ledger")
            trades = local_trades
        elif self.organization_id:
            # Fall back to NODES API only if no local data
            self.logger.info("No local trades, querying NODES API...")
            trades = self.market_handler.get_accepted_trades_for_slot(
                self.organization_id, slot_start, slot_end
            )
            settlements = self.market_handler.get_settlements_for_slot(
                self.organization_id, slot_start, slot_end
            )
        else:
            self.logger.warning("No local trades and no NODES organization ID")
        
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
        self.logger.info("Step 2: Allocating flexibility across ALLOWED assets...")
        self.logger.info("  Allowed assets: %s", allowed_assets)
        
        allocations = self.allocator.allocate_flexibility(
            total_sold_kw,
            allowed_assets=allowed_assets,  # Use assets from bid record
            strategy=allocation_strategy
        )
        
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
            mod_type = asset_config.get("modulation_type", "")
            if not mod_type:
                # Fallback to default by asset type
                asset_type = asset_config.get("type", "")
                mod_type = "discrete" if asset_type == "heat_pump" else "continuous"
            
            if mod_type == "discrete":
                capacity = asset_config.get("capacity_kw", curtailment_kw)
                # For discrete assets, curtailment = full capacity means switching OFF
                state = "OFF" if curtailment_kw >= capacity * 0.5 else "ON"
                self.logger.info(
                    "  %s (%s): %.2f kW [discrete → %s]", 
                    asset_id, asset_desc, curtailment_kw, state
                )
            else:
                capacity = asset_config.get("capacity_kw", 0)
                target_power = max(0, capacity - curtailment_kw)
                self.logger.info(
                    "  %s (%s): %.2f kW [continuous → limit %.2f kW]", 
                    asset_id, asset_desc, curtailment_kw, target_power
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
        
        for asset_id, curtailment_kw in allocations.items():
            result = self.controller.curtail_asset(
                asset_id,
                curtailment_kw,
                duration_minutes=15,
                dry_run=dry_run
            )
            summary["control_results"][asset_id] = result
        
        # Step 4: Publish commands to RabbitMQ (if configured)
        if self.rabbitmq_publisher and self.rabbitmq_publisher.is_connected():
            self.logger.info("-" * 70)
            self.logger.info("Step 4: Publishing commands to RabbitMQ...")
            self.logger.info("  Forwarder mode: %s", "DRY-RUN" if dry_run else "LIVE ACTUATION")
            
            slot_info = {
                "fsp_id": self.fsp_id,
                "slot_start": slot_start.isoformat(),
                "slot_end": slot_end.isoformat(),
                "total_flexibility_kw": total_sold_kw,
                "allocation_strategy": allocation_strategy,
                "dry_run": dry_run
            }
            
            published = self.controller.publish_pending_commands(slot_info, dry_run=dry_run)
            summary["rabbitmq_published"] = published
            summary["forwarder_dry_run"] = dry_run
            self.logger.info("Published %d commands to RabbitMQ", published)
        
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
                    "status": control_result.get("status", "unknown")
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
        
        successful = sum(1 for r in summary["control_results"].values() 
                        if r.get("status") in ["success", "simulated"])
        failed = len(summary["control_results"]) - successful
        
        self.logger.info("Assets controlled: %d successful, %d failed", successful, failed)
        self.logger.info("Total flexibility delivered: %.2f kW (%.3f MW)", 
                        total_allocated, total_allocated / 1000)
        
        if failed > 0:
            summary["status"] = "partial_success"
        
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
    
    # RabbitMQ arguments
    parser.add_argument(
        "--rabbitmq",
        action="store_true",
        help="Enable RabbitMQ message publishing for command forwarding"
    )
    parser.add_argument(
        "--rabbitmq-host",
        default="localhost",
        help="RabbitMQ server hostname (default: localhost)"
    )
    parser.add_argument(
        "--rabbitmq-port",
        type=int,
        default=5672,
        help="RabbitMQ server port (default: 5672)"
    )
    parser.add_argument(
        "--rabbitmq-user",
        default="guest",
        help="RabbitMQ username (default: guest)"
    )
    parser.add_argument(
        "--rabbitmq-pass",
        default="guest",
        help="RabbitMQ password (default: guest)"
    )
    parser.add_argument(
        "--rabbitmq-vhost",
        default="/",
        help="RabbitMQ virtual host (default: /)"
    )
    parser.add_argument(
        "--rabbitmq-exchange",
        default="flexi_commands",
        help="RabbitMQ exchange name (default: flexi_commands)"
    )
    
    args = parser.parse_args()
    
    # Setup logging
    logger = setup_logging(args.log_level, args.log_file)

    # Determine dry-run mode
    dry_run = not args.live
    
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
    nodes_authenticated = False
    
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
        
        # Initialize PostgreSQL connection and bid record repository
        pg_cfg = conns.get("postgreSQL", {})
        if pg_cfg:
            try:
                pg_interface = PostgreSQLInterface(pg_cfg, logger)
                bid_repo = BidRecordRepository(pg_interface, logger)
                logger.info("Connected to PostgreSQL, bid record repository ready")
            except Exception as e:
                logger.warning("Could not connect to PostgreSQL: %s", str(e))
                logger.warning("Bid record loading will not be available")
        
    except FileNotFoundError:
        logger.warning("Connections file not found - running in offline mode")
    except Exception as e:
        logger.warning("Error loading connections: %s - running in offline mode", str(e))
    
    # Initialize RabbitMQ publisher if requested
    rabbitmq_publisher = None
    if args.rabbitmq:
        if not RABBITMQ_AVAILABLE:
            logger.error("RabbitMQ requested but pika library not installed. Run: pip install pika")
            sys.exit(1)
        
        try:
            rabbitmq_publisher = RabbitMQPublisher(
                host=args.rabbitmq_host,
                port=args.rabbitmq_port,
                username=args.rabbitmq_user,
                password=args.rabbitmq_pass,
                virtual_host=args.rabbitmq_vhost,
                exchange=args.rabbitmq_exchange,
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
            rabbitmq_publisher=rabbitmq_publisher
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
            rabbitmq_publisher=rabbitmq_publisher
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
        fallback_strategy=args.fallback_strategy
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
    if summary["status"] == "success":
        sys.exit(0)
    elif summary["status"] == "no_flexibility":
        sys.exit(0)
    elif summary["status"] == "partial_success":
        sys.exit(1)
    else:
        sys.exit(2)


if __name__ == "__main__":
    main()
