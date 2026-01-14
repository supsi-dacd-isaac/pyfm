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

from classes.nodes_interface import NODESInterface as NodesInterface
from classes.postgresql_interface import PostgreSQLInterface
from classes.bid_record_repository import BidRecordRepository
from classes.bidding_strategy import BiddingStrategy, StrategyManager


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
    - Simulation: For testing (logs only)
    """
    
    def __init__(self, asset_mapping: dict, logger: logging.Logger):
        self.asset_mapping = asset_mapping
        self.logger = logger
        self.control_results = {}
    
    def curtail_asset(
        self, 
        asset_id: str, 
        curtailment_kw: float, 
        duration_minutes: int = 15,
        dry_run: bool = True
    ) -> Dict:
        """
        Send curtailment command to an asset.
        
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
        
        # Calculate curtailment percentage
        curtailment_pct = (curtailment_kw / capacity_kw * 100) if capacity_kw > 0 else 0
        
        result = {
            "asset_id": asset_id,
            "description": description,
            "asset_type": asset_type,
            "curtailment_kw": curtailment_kw,
            "curtailment_pct": curtailment_pct,
            "duration_minutes": duration_minutes,
            "dry_run": dry_run,
            "status": "pending",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        
        if dry_run:
            self.logger.info(
                "[DRY-RUN] Would curtail %s (%s): %.2f kW (%.1f%%) for %d minutes",
                asset_id, description, curtailment_kw, curtailment_pct, duration_minutes
            )
            result["status"] = "simulated"
            result["message"] = "Dry-run mode - no actual command sent"
        else:
            # Actual control logic would go here
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
        
        Control methods (configured per asset):
        - mqtt: Publish to MQTT topic
        - http: POST to device API
        - modbus: Write to Modbus register
        """
        control_cfg = config.get("control", {})
        control_type = control_cfg.get("type", "simulation")
        
        if control_type == "mqtt":
            topic = control_cfg.get("topic", f"assets/{asset_id}/control")
            payload = {
                "command": "curtail",
                "power_reduction_kw": curtailment_kw,
                "duration_minutes": duration_minutes,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
            self.logger.info("MQTT publish to %s: %s", topic, json.dumps(payload))
            # TODO: Implement actual MQTT publish
            # mqtt_client.publish(topic, json.dumps(payload))
            
        elif control_type == "http":
            endpoint = control_cfg.get("endpoint", "")
            payload = {
                "action": "reduce_power",
                "reduction_kw": curtailment_kw,
                "duration_s": duration_minutes * 60
            }
            self.logger.info("HTTP POST to %s: %s", endpoint, json.dumps(payload))
            # TODO: Implement actual HTTP request
            # requests.post(endpoint, json=payload)
            
        elif control_type == "simulation":
            self.logger.info(
                "[SIMULATION] HP %s: reduce power by %.2f kW for %d min",
                asset_id, curtailment_kw, duration_minutes
            )
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
        
        EV chargers typically support:
        - Set maximum charging power
        - Pause/resume charging
        - Smart charging profiles
        """
        control_cfg = config.get("control", {})
        control_type = control_cfg.get("type", "simulation")
        capacity_kw = config.get("capacity_kw", 11.0)
        
        # Calculate new charging limit
        new_limit_kw = max(0, capacity_kw - curtailment_kw)
        
        if control_type == "ocpp":
            # OCPP (Open Charge Point Protocol) for EV chargers
            charger_id = control_cfg.get("charger_id", asset_id)
            self.logger.info(
                "OCPP SetChargingProfile for %s: limit=%.2f kW, duration=%d min",
                charger_id, new_limit_kw, duration_minutes
            )
            # TODO: Implement OCPP command
            
        elif control_type == "http":
            endpoint = control_cfg.get("endpoint", "")
            payload = {
                "action": "set_charging_limit",
                "limit_kw": new_limit_kw,
                "duration_s": duration_minutes * 60
            }
            self.logger.info("HTTP POST to %s: %s", endpoint, json.dumps(payload))
            # TODO: Implement HTTP request
            
        elif control_type == "simulation":
            self.logger.info(
                "[SIMULATION] EV %s: set charging limit to %.2f kW for %d min",
                asset_id, new_limit_kw, duration_minutes
            )
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
        
        if dry_run:
            self.logger.info("[DRY-RUN] Would restore %s (%s) to normal operation", asset_id, description)
            return {"asset_id": asset_id, "status": "simulated", "action": "restore"}
        else:
            self.logger.info("Restoring %s (%s) to normal operation", asset_id, description)
            # TODO: Implement actual restore logic
            return {"asset_id": asset_id, "status": "success", "action": "restore"}


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

class FlexibilityAllocator:
    """
    Allocates sold flexibility across available assets.
    
    Allocation strategies:
    - proportional: Distribute based on asset capacity
    - priority: Fill high-flexibility assets first
    - cost_optimal: Minimize activation costs
    """
    
    def __init__(self, asset_mapping: dict, logger: logging.Logger):
        self.asset_mapping = asset_mapping
        self.logger = logger
    
    def allocate_flexibility(
        self,
        total_flexibility_kw: float,
        allowed_assets: List[str] = None,
        strategy: str = "proportional"
    ) -> Dict[str, float]:
        """
        Allocate total flexibility requirement across assets.
        
        :param total_flexibility_kw: Total flexibility to deliver (kW)
        :param allowed_assets: List of asset IDs to use (None = all)
        :param strategy: Allocation strategy
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
        
        if strategy == "proportional":
            return self._allocate_proportional(total_flexibility_kw, assets)
        elif strategy == "priority":
            return self._allocate_priority(total_flexibility_kw, assets)
        elif strategy == "cost_optimal":
            return self._allocate_cost_optimal(total_flexibility_kw, assets)
        else:
            self.logger.warning("Unknown strategy '%s', using proportional", strategy)
            return self._allocate_proportional(total_flexibility_kw, assets)
    
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
        nodes_authenticated: bool = False
    ):
        self.config = config
        self.fsp_id = fsp_id
        self.fsp_config = config["fm"]["actors"]["fsps"].get(fsp_id, {})
        self.asset_mapping = config.get("asset_mapping", {})
        self.logger = logger
        self.bid_repo = bid_repo
        self.nodes_authenticated = nodes_authenticated
        
        # Get FSP's allowed assets (default from config)
        self.fsp_assets = self.fsp_config.get("assets", list(self.asset_mapping.keys()))
        
        # Initialize strategy manager to derive assets from strategy if needed
        self.strategy_manager = StrategyManager(config, logger)
        
        # Initialize components
        self.market_handler = MarketResultsHandler(nodes_interface, logger)
        self.allocator = FlexibilityAllocator(self.asset_mapping, logger)
        self.controller = AssetController(self.asset_mapping, logger)
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
        allocation_strategy: str = "proportional",
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
        
        # Log allocation details
        self.logger.info("-" * 70)
        self.logger.info("Allocation plan:")
        total_allocated = 0
        for asset_id, curtailment_kw in allocations.items():
            asset_desc = self.asset_mapping.get(asset_id, {}).get("description", asset_id)
            self.logger.info("  %s (%s): %.2f kW", asset_id, asset_desc, curtailment_kw)
            total_allocated += curtailment_kw
        self.logger.info("  Total allocated: %.2f kW", total_allocated)
        
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
        
        # Step 4: Save activation records to database
        if self.bid_repo:
            self.logger.info("-" * 70)
            self.logger.info("Step 4: Saving activation records to database...")
            
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
        "--config", "-c",
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
        choices=["proportional", "priority", "cost_optimal"],
        default="proportional",
        help="Allocation strategy (default: proportional)"
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
    
    args = parser.parse_args()
    
    # Setup logging
    logger = setup_logging(args.log_level, args.log_file)

    # Determine dry-run mode
    dry_run = not args.live
    
    # Load configuration
    config_path = args.config
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
    
    # Create and run flexibility manager
    if nodes_interface or bid_repo:
        manager = FlexibilityManager(
            config, args.fsp, nodes_interface, bid_repo, logger,
            nodes_authenticated=nodes_authenticated
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
            nodes_authenticated=False
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
