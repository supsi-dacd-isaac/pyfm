#!/usr/bin/env python3
"""
Forwarder (forwarder.py)

This script receives control commands and measurements from RabbitMQ
and forwards them to the actual asset control interfaces.

The forwarder acts as the actuation layer, decoupled from the decision
logic in flexi_manager.py. This separation allows:
- Better scalability (multiple forwarders can consume from the same queue)
- Reliable message delivery (RabbitMQ persistence and acknowledgments)
- Easier testing (dry-run mode)
- Protocol translation (RabbitMQ -> MQTT/HTTP/Modbus/OCPP)

Usage:
    # Dry-run mode (just logs commands without actuating)
    python forwarder.py --dry-run

    # Listen for specific asset types only
    python forwarder.py --dry-run --asset-types heat_pump,ev_charger

    # Custom RabbitMQ configuration
    python forwarder.py --dry-run --rabbitmq-host rabbitmq.local --rabbitmq-port 5672

Environment variables (for Docker/containerized deployment):
    FORWARDER_MODE          - "dry-run" or "live" (default: dry-run)
    FORWARDER_LOG_LEVEL     - DEBUG, INFO, WARNING, ERROR (default: INFO)
    FORWARDER_ASSET_TYPES   - Comma-separated list of asset types to filter
    FORWARDER_QUEUES        - Comma-separated list: commands,measurements
    RABBITMQ_HOST           - RabbitMQ hostname (default: localhost)
    RABBITMQ_PORT           - RabbitMQ port (default: 5672)
    RABBITMQ_USER           - RabbitMQ username (default: guest)
    RABBITMQ_PASS           - RabbitMQ password (default: guest)
    RABBITMQ_VHOST          - RabbitMQ virtual host (default: /)
    RABBITMQ_EXCHANGE       - RabbitMQ exchange (default: flexi_commands)

Example flow:
    1. flexi_manager.py publishes commands to RabbitMQ exchange 'flexi_commands'
    2. forwarder.py consumes from queue 'asset_commands'
    3. forwarder.py translates and forwards to actual device protocols
"""

import os
import sys
import json
import argparse
import logging
import signal
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Callable


def get_env(name: str, default: str = None) -> str:
    """Get environment variable with optional default."""
    return os.environ.get(name, default)

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# RabbitMQ support
try:
    import pika
    RABBITMQ_AVAILABLE = True
except ImportError:
    RABBITMQ_AVAILABLE = False
    print("ERROR: pika library not installed. Run: pip install pika", file=sys.stderr)
    sys.exit(1)


# =============================================================================
# LOGGING SETUP
# =============================================================================

def setup_logging(log_level: str = "INFO", log_file: Optional[str] = None) -> logging.Logger:
    """Configure logging with timestamp and level.

    :param log_level: Logging level (DEBUG, INFO, WARNING, ERROR)
    :param log_file: Optional path to log file
    :return: Configured logger instance
    """
    logger = logging.getLogger("forwarder")
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


# =============================================================================
# COMMAND HANDLERS (DRY-RUN MODE)
# =============================================================================

class CommandHandler:
    """
    Handles commands received from RabbitMQ.
    
    Supports two modes:
    - dry_run=True: Logs what would be done without actual actuation
    - dry_run=False: Would send commands to actual devices (NOT IMPLEMENTED YET)
    
    The dry_run mode can be:
    - Set globally via --dry-run flag (overrides message flag)
    - Read from each message's payload (if no global override)
    """
    
    def __init__(self, logger: logging.Logger, force_dry_run: bool = True):
        """
        Initialize command handler.
        
        :param logger: Logger instance
        :param force_dry_run: If True, always use dry-run mode regardless of message flag
        """
        self.logger = logger
        self.force_dry_run = force_dry_run
        self.commands_received = 0
        self.commands_by_type = {}
        self.commands_by_asset = {}
    
    def handle_command(self, message: dict) -> bool:
        """
        Handle a command message.
        
        Decides whether to actuate based on:
        1. force_dry_run (global override from --dry-run flag)
        2. dry_run flag in the message payload
        
        :param message: Command message dictionary
        :return: True if handled successfully
        """
        self.commands_received += 1
        
        asset_id = message.get("asset_id", "unknown")
        asset_type = message.get("asset_type", "unknown")
        command_type = message.get("command_type", "unknown")
        payload = message.get("payload", {})
        timestamp = message.get("timestamp", "")
        
        # Determine if this should be dry-run
        # Global force_dry_run takes precedence, otherwise use message flag
        message_dry_run = payload.get("dry_run", True)
        is_dry_run = self.force_dry_run or message_dry_run
        
        mode_label = "[DRY-RUN]" if is_dry_run else "[LIVE]"
        
        # Update statistics
        self.commands_by_type[command_type] = self.commands_by_type.get(command_type, 0) + 1
        self.commands_by_asset[asset_id] = self.commands_by_asset.get(asset_id, 0) + 1
        
        # Extract slot timing information
        slot_start = payload.get("slot_start", "N/A")
        slot_end = payload.get("slot_end", "N/A")
        
        # Log the command details
        self.logger.info("=" * 60)
        self.logger.info("%s COMMAND RECEIVED #%d", mode_label, self.commands_received)
        self.logger.info("=" * 60)
        self.logger.info("  Asset ID:     %s", asset_id)
        self.logger.info("  Asset Type:   %s", asset_type)
        self.logger.info("  Command:      %s", command_type)
        self.logger.info("-" * 60)
        self.logger.info("  SLOT START:   %s", slot_start)
        self.logger.info("  SLOT END:     %s", slot_end)
        self.logger.info("-" * 60)
        self.logger.info("  Timestamp:    %s", timestamp)
        
        # Log payload details based on command type
        if command_type == "curtail":
            modulation_type = payload.get("modulation_type", "unknown")
            target_power = payload.get("target_power_kw", 0)
            curtailment = payload.get("actual_curtailment_kw", 0)
            duration = payload.get("duration_minutes", 15)
            
            if modulation_type == "discrete":
                state = payload.get("discrete_state", "unknown")
                self.logger.info("  Modulation:   %s (state: %s)", modulation_type, state)
            else:
                self.logger.info("  Modulation:   %s", modulation_type)
            
            self.logger.info("  Curtailment:  %.2f kW", curtailment)
            self.logger.info("  Target Power: %.2f kW", target_power)
            self.logger.info("  Duration:     %d minutes", duration)
            
            # Execute or simulate
            self.logger.info("-" * 60)
            if is_dry_run:
                if modulation_type == "discrete":
                    self.logger.info(
                        "%s Would send %s command to %s (%s) for slot %s",
                        mode_label, state, asset_id, payload.get("description", ""), slot_start
                    )
                else:
                    self.logger.info(
                        "%s Would set power limit to %.2f kW on %s (%s) for slot %s",
                        mode_label, target_power, asset_id, payload.get("description", ""), slot_start
                    )
            else:
                # LIVE MODE - actual actuation would happen here
                # TODO: Implement actual protocol handlers (MQTT, HTTP, Modbus, OCPP)
                self.logger.warning(
                    "%s LIVE ACTUATION NOT IMPLEMENTED - would actuate %s (%s)",
                    mode_label, asset_id, payload.get("description", "")
                )
        
        elif command_type == "restore":
            self.logger.info("-" * 60)
            if is_dry_run:
                self.logger.info(
                    "%s Would restore %s (%s) to normal operation at %s",
                    mode_label, asset_id, payload.get("description", ""), slot_end
                )
            else:
                # LIVE MODE
                self.logger.warning(
                    "%s LIVE RESTORE NOT IMPLEMENTED - would restore %s (%s)",
                    mode_label, asset_id, payload.get("description", "")
                )
        
        else:
            self.logger.info("  Payload:      %s", json.dumps(payload, indent=4))
        
        self.logger.info("=" * 60)
        return True
    
    def handle_measurement(self, message: dict) -> bool:
        """
        Handle a measurement message in dry-run mode.
        
        :param message: Measurement message dictionary
        :return: True if handled successfully
        """
        asset_id = message.get("asset_id", "unknown")
        asset_type = message.get("asset_type", "unknown")
        measurement_type = message.get("measurement_type", "unknown")
        payload = message.get("payload", {})
        timestamp = message.get("timestamp", "")
        
        self.logger.info("-" * 40)
        self.logger.info("[DRY-RUN] MEASUREMENT RECEIVED")
        self.logger.info("  Asset ID:     %s", asset_id)
        self.logger.info("  Type:         %s", measurement_type)
        self.logger.info("  Timestamp:    %s", timestamp)
        self.logger.info("  Payload:      %s", json.dumps(payload))
        self.logger.info("-" * 40)
        
        return True
    
    def handle_batch_header(self, message: dict) -> bool:
        """
        Handle a batch header message.
        
        :param message: Batch header message dictionary
        :return: True if handled successfully
        """
        slot_info = message.get("slot_info", {})
        command_count = message.get("command_count", 0)
        timestamp = message.get("timestamp", "")
        
        self.logger.info("*" * 60)
        self.logger.info("[DRY-RUN] BATCH START - Expecting %d commands", command_count)
        self.logger.info("*" * 60)
        self.logger.info("  FSP ID:       %s", slot_info.get("fsp_id", "unknown"))
        self.logger.info("  Slot Start:   %s", slot_info.get("slot_start", ""))
        self.logger.info("  Slot End:     %s", slot_info.get("slot_end", ""))
        self.logger.info("  Total Flex:   %.2f kW", slot_info.get("total_flexibility_kw", 0))
        self.logger.info("  Strategy:     %s", slot_info.get("allocation_strategy", ""))
        self.logger.info("  Dry Run:      %s", slot_info.get("dry_run", True))
        self.logger.info("*" * 60)
        
        return True
    
    def get_statistics(self) -> dict:
        """Get statistics about processed commands."""
        return {
            "total_commands": self.commands_received,
            "by_type": self.commands_by_type,
            "by_asset": self.commands_by_asset
        }


# =============================================================================
# RABBITMQ CONSUMER
# =============================================================================

class RabbitMQConsumer:
    """
    Consumes messages from RabbitMQ and dispatches them to handlers.
    
    Subscribes to:
    - asset_commands queue: Control commands from flexi_manager
    - asset_measurements queue: Measurement data
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
        logger: logging.Logger = None,
        command_handler: Callable = None,
        measurement_handler: Callable = None,
        asset_types_filter: List[str] = None
    ):
        """
        Initialize RabbitMQ consumer.
        
        :param host: RabbitMQ server hostname
        :param port: RabbitMQ server port
        :param username: RabbitMQ username
        :param password: RabbitMQ password
        :param virtual_host: RabbitMQ virtual host
        :param exchange: Exchange name (default: flexi_commands)
        :param logger: Logger instance
        :param command_handler: Callback function for command messages
        :param measurement_handler: Callback function for measurement messages
        :param asset_types_filter: List of asset types to process (None = all)
        """
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.virtual_host = virtual_host
        self.exchange = exchange or self.DEFAULT_EXCHANGE
        self.logger = logger or logging.getLogger(__name__)
        
        self.command_handler = command_handler
        self.measurement_handler = measurement_handler
        self.asset_types_filter = asset_types_filter
        
        self.connection = None
        self.channel = None
        self._running = False
        self._messages_processed = 0
    
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
            
            # Declare exchange (should already exist from publisher)
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
            
            # Bind queues to exchange
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
            
            # Set QoS (prefetch count)
            self.channel.basic_qos(prefetch_count=1)
            
            self.logger.info(
                "Connected to RabbitMQ at %s:%d (exchange: %s)",
                self.host, self.port, self.exchange
            )
            return True
            
        except Exception as e:
            self.logger.error("Failed to connect to RabbitMQ: %s", str(e))
            return False
    
    def disconnect(self):
        """Close RabbitMQ connection."""
        self._running = False
        if self.connection and self.connection.is_open:
            try:
                self.connection.close()
                self.logger.info("Disconnected from RabbitMQ")
            except Exception as e:
                self.logger.warning("Error closing RabbitMQ connection: %s", str(e))
    
    def _process_message(self, ch, method, properties, body):
        """
        Process a received message.
        
        :param ch: Channel
        :param method: Method frame
        :param properties: Message properties
        :param body: Message body
        """
        try:
            message = json.loads(body.decode('utf-8'))
            message_type = message.get("message_type", "unknown")
            asset_type = message.get("asset_type", "")
            
            # Apply asset type filter
            if self.asset_types_filter and asset_type:
                if asset_type not in self.asset_types_filter:
                    self.logger.debug(
                        "Skipping message for asset type '%s' (not in filter)",
                        asset_type
                    )
                    ch.basic_ack(delivery_tag=method.delivery_tag)
                    return
            
            # Dispatch to appropriate handler
            handled = False
            
            if message_type == "command":
                if self.command_handler:
                    handled = self.command_handler(message)
                else:
                    self.logger.warning("No command handler configured")
            
            elif message_type == "measurement":
                if self.measurement_handler:
                    handled = self.measurement_handler(message)
                else:
                    self.logger.debug("No measurement handler configured")
                    handled = True  # Don't fail on missing measurement handler
            
            elif message_type == "batch_start":
                # Batch header - call command handler with special handling
                if self.command_handler and hasattr(self.command_handler, '__self__'):
                    handler_obj = self.command_handler.__self__
                    if hasattr(handler_obj, 'handle_batch_header'):
                        handled = handler_obj.handle_batch_header(message)
                    else:
                        handled = True
                else:
                    handled = True
            
            else:
                self.logger.warning("Unknown message type: %s", message_type)
                handled = True  # Acknowledge to avoid requeueing unknown messages
            
            # Acknowledge message
            if handled:
                ch.basic_ack(delivery_tag=method.delivery_tag)
                self._messages_processed += 1
            else:
                # Negative acknowledge - requeue message
                ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
                self.logger.warning("Message not handled, requeueing")
            
        except json.JSONDecodeError as e:
            self.logger.error("Invalid JSON in message: %s", str(e))
            ch.basic_ack(delivery_tag=method.delivery_tag)  # Ack to avoid infinite loop
        except Exception as e:
            self.logger.error("Error processing message: %s", str(e))
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
    
    def start_consuming(self, queues: List[str] = None):
        """
        Start consuming messages from specified queues.
        
        :param queues: List of queue names to consume from (default: commands queue)
        """
        if queues is None:
            queues = [self.DEFAULT_QUEUE_COMMANDS]
        
        self._running = True
        
        for queue in queues:
            self.channel.basic_consume(
                queue=queue,
                on_message_callback=self._process_message,
                auto_ack=False
            )
            self.logger.info("Consuming from queue: %s", queue)
        
        self.logger.info("Waiting for messages... (Press Ctrl+C to stop)")
        
        try:
            while self._running:
                self.connection.process_data_events(time_limit=1)
        except KeyboardInterrupt:
            self.logger.info("Received interrupt signal")
        finally:
            self.disconnect()
    
    def get_messages_processed(self) -> int:
        """Get count of messages processed."""
        return self._messages_processed


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Forwarder - Receive and forward flexibility commands from RabbitMQ",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Start forwarder in dry-run mode (default)
  python forwarder.py --dry-run

  # Listen with verbose logging
  python forwarder.py --dry-run --log-level DEBUG

  # Filter by asset types
  python forwarder.py --dry-run --asset-types heat_pump,ev_charger

  # Custom RabbitMQ server
  python forwarder.py --dry-run --rabbitmq-host rabbitmq.local

  # With logging to file
  python forwarder.py --dry-run --log-file /var/log/forwarder.log
        """
    )
    
    parser.add_argument(
        "--dry-run", "-d",
        action="store_true",
        default=(get_env("FORWARDER_MODE", "dry-run").lower() == "dry-run"),
        help="Dry-run mode - log commands without actuating (default, env: FORWARDER_MODE)"
    )
    parser.add_argument(
        "--live", "-l",
        action="store_true",
        default=(get_env("FORWARDER_MODE", "dry-run").lower() == "live"),
        help="Live mode - actually forward commands (env: FORWARDER_MODE=live)"
    )
    parser.add_argument(
        "--asset-types",
        default=get_env("FORWARDER_ASSET_TYPES"),
        help="Comma-separated list of asset types to process (env: FORWARDER_ASSET_TYPES)"
    )
    parser.add_argument(
        "--queues",
        default=get_env("FORWARDER_QUEUES", "commands"),
        help="Comma-separated list of queues to consume (env: FORWARDER_QUEUES)"
    )
    
    # RabbitMQ arguments (with environment variable defaults for Docker)
    parser.add_argument(
        "--rabbitmq-host",
        default=get_env("RABBITMQ_HOST", "localhost"),
        help="RabbitMQ server hostname (env: RABBITMQ_HOST)"
    )
    parser.add_argument(
        "--rabbitmq-port",
        type=int,
        default=int(get_env("RABBITMQ_PORT", "5672")),
        help="RabbitMQ server port (env: RABBITMQ_PORT)"
    )
    parser.add_argument(
        "--rabbitmq-user",
        default=get_env("RABBITMQ_USER", "guest"),
        help="RabbitMQ username (env: RABBITMQ_USER)"
    )
    parser.add_argument(
        "--rabbitmq-pass",
        default=get_env("RABBITMQ_PASS", "guest"),
        help="RabbitMQ password (env: RABBITMQ_PASS)"
    )
    parser.add_argument(
        "--rabbitmq-vhost",
        default=get_env("RABBITMQ_VHOST", "/"),
        help="RabbitMQ virtual host (env: RABBITMQ_VHOST)"
    )
    parser.add_argument(
        "--rabbitmq-exchange",
        default=get_env("RABBITMQ_EXCHANGE", "flexi_commands"),
        help="RabbitMQ exchange name (env: RABBITMQ_EXCHANGE)"
    )
    
    # Logging arguments
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default=get_env("FORWARDER_LOG_LEVEL", "INFO"),
        help="Logging level (env: FORWARDER_LOG_LEVEL)"
    )
    parser.add_argument(
        "--log-file",
        default=get_env("FORWARDER_LOG_FILE"),
        help="Path to log file (env: FORWARDER_LOG_FILE)"
    )
    
    args = parser.parse_args()
    
    # Setup logging
    logger = setup_logging(args.log_level, args.log_file)
    
    # Determine mode
    # --dry-run (default) forces all commands to dry-run regardless of message flag
    # --live allows respecting the dry_run flag from each message
    force_dry_run = not args.live
    
    logger.info("=" * 60)
    logger.info("FORWARDER - Command/Measurement Forwarding Service")
    logger.info("=" * 60)
    if force_dry_run:
        logger.info("Mode: DRY-RUN (forced - all commands will be simulated)")
    else:
        logger.info("Mode: LIVE (actuation mode determined by each message)")
        logger.warning("WARNING: Live actuation is NOT YET IMPLEMENTED!")
    logger.info("RabbitMQ: %s:%d", args.rabbitmq_host, args.rabbitmq_port)
    logger.info("Exchange: %s", args.rabbitmq_exchange)
    logger.info("-" * 60)
    
    # Parse asset types filter
    asset_types_filter = None
    if args.asset_types:
        asset_types_filter = [t.strip() for t in args.asset_types.split(",")]
        logger.info("Asset type filter: %s", asset_types_filter)
    
    # Parse queues
    queue_map = {
        "commands": RabbitMQConsumer.DEFAULT_QUEUE_COMMANDS,
        "measurements": RabbitMQConsumer.DEFAULT_QUEUE_MEASUREMENTS
    }
    queues_to_consume = []
    for q in args.queues.split(","):
        q = q.strip().lower()
        if q in queue_map:
            queues_to_consume.append(queue_map[q])
        else:
            logger.warning("Unknown queue '%s', skipping", q)
    
    if not queues_to_consume:
        logger.error("No valid queues specified")
        sys.exit(1)
    
    logger.info("Queues: %s", queues_to_consume)
    
    # Create handler
    handler = CommandHandler(logger, force_dry_run=force_dry_run)
    
    # Create consumer
    consumer = RabbitMQConsumer(
        host=args.rabbitmq_host,
        port=args.rabbitmq_port,
        username=args.rabbitmq_user,
        password=args.rabbitmq_pass,
        virtual_host=args.rabbitmq_vhost,
        exchange=args.rabbitmq_exchange,
        logger=logger,
        command_handler=handler.handle_command,
        measurement_handler=handler.handle_measurement,
        asset_types_filter=asset_types_filter
    )
    
    # Setup signal handlers for graceful shutdown
    def signal_handler(signum, frame):
        logger.info("Received signal %d, shutting down...", signum)
        consumer.disconnect()
        
        # Print statistics
        stats = handler.get_statistics()
        logger.info("=" * 60)
        logger.info("FORWARDER SHUTDOWN - Statistics")
        logger.info("=" * 60)
        logger.info("Total commands processed: %d", stats["total_commands"])
        if stats["by_type"]:
            logger.info("Commands by type:")
            for cmd_type, count in stats["by_type"].items():
                logger.info("  %s: %d", cmd_type, count)
        if stats["by_asset"]:
            logger.info("Commands by asset:")
            for asset_id, count in stats["by_asset"].items():
                logger.info("  %s: %d", asset_id, count)
        logger.info("=" * 60)
        
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Connect with retry (useful for Docker where RabbitMQ might not be ready yet)
    max_retries = int(get_env("RABBITMQ_CONNECT_RETRIES", "10"))
    retry_delay = int(get_env("RABBITMQ_CONNECT_RETRY_DELAY", "5"))
    
    connected = False
    for attempt in range(1, max_retries + 1):
        logger.info("Connecting to RabbitMQ (attempt %d/%d)...", attempt, max_retries)
        if consumer.connect():
            connected = True
            break
        else:
            if attempt < max_retries:
                logger.warning("Connection failed, retrying in %d seconds...", retry_delay)
                time.sleep(retry_delay)
            else:
                logger.error("Failed to connect to RabbitMQ after %d attempts", max_retries)
    
    if not connected:
        sys.exit(1)
    
    try:
        consumer.start_consuming(queues_to_consume)
    except Exception as e:
        logger.error("Error during consumption: %s", str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
