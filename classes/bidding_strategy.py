"""
Bidding Strategy Module

This module implements different bidding strategies for flexibility market trading.
Strategies determine:
- Which assets to include in bids
- What price to bid at different times of day
- How much flexibility to offer

Available Strategies:
- strategy_1: HP Only - Conservative full-day coverage
- strategy_2: Full Portfolio + Evening EV Focus
- strategy_3: Morning Peak Focus (Cinema HPs only)
- strategy_4: Hybrid (S3+S1) - RECOMMENDED
- strategy_5: Hybrid2 (S3+S2) - Morning aggressive + evening EV
"""

from datetime import datetime, time
from typing import Dict, List, Optional, Tuple
import logging


class TimeSlot:
    """Represents a time period with specific bidding parameters."""
    
    def __init__(self, config: dict):
        """
        Initialize a time slot from configuration.
        
        :param config: Dictionary with time slot configuration
        """
        self.name = config.get("name", "Unknown")
        self.start = self._parse_time(config.get("start", "00:00"))
        self.end = self._parse_time(config.get("end", "23:59"))
        self.flexibility_mw = config.get("flexibility_mw", 0.0)
        self.bid_price = config.get("bid_price", 5.0)
        self.activation_cost = config.get("activation_cost", 2.5)
        
        # EV-specific parameters (optional)
        self.ev_flexibility_mw = config.get("ev_flexibility_mw", 0.0)
        self.ev_bid_price = config.get("ev_bid_price", 0.0)
        self.ev_activation_cost = config.get("ev_activation_cost", 4.5)
        self.has_ev = self.ev_flexibility_mw > 0
    
    def _parse_time(self, time_str: str) -> time:
        """Parse time string (HH:MM) to time object."""
        parts = time_str.split(":")
        return time(int(parts[0]), int(parts[1]))
    
    def contains_time(self, dt: datetime) -> bool:
        """
        Check if the given datetime falls within this time slot.
        
        Handles overnight slots (e.g., 19:00-06:30).
        """
        current_time = dt.time()
        
        if self.start <= self.end:
            # Normal slot (e.g., 09:00-12:00)
            return self.start <= current_time < self.end
        else:
            # Overnight slot (e.g., 19:00-06:30)
            return current_time >= self.start or current_time < self.end
    
    def __repr__(self):
        return f"TimeSlot({self.name}, {self.start}-{self.end}, {self.flexibility_mw}MW @ {self.bid_price}CHF/MW)"


class BiddingStrategy:
    """
    Implements a bidding strategy for flexibility market.
    
    A strategy defines:
    - Which asset types to use (heat_pump, ev_charger)
    - Optional specific asset filter
    - Time-based bidding parameters (price, quantity)
    """
    
    def __init__(self, strategy_id: str, config: dict, asset_mapping: dict, logger: logging.Logger = None):
        """
        Initialize a bidding strategy.
        
        :param strategy_id: Strategy identifier (e.g., "strategy_4")
        :param config: Strategy configuration dictionary
        :param asset_mapping: Asset mapping from main config
        :param logger: Logger instance
        """
        self.strategy_id = strategy_id
        self.config = config
        self.asset_mapping = asset_mapping
        self.logger = logger or logging.getLogger(__name__)
        
        # Parse configuration
        self.name = config.get("name", strategy_id)
        self.description = config.get("description", "")
        self.asset_types = config.get("asset_types", ["heat_pump"])
        self.assets_filter = config.get("assets_filter", None)  # Optional specific assets
        
        # Parse time slots
        self.time_slots = [
            TimeSlot(slot_config) 
            for slot_config in config.get("time_slots", [])
        ]
        
        # Build list of allowed assets
        self._build_allowed_assets()
        
        self.logger.info(
            "BiddingStrategy initialized: %s (%s), assets: %s, time_slots: %d",
            self.strategy_id, self.name, self.allowed_assets, len(self.time_slots)
        )
    
    def _build_allowed_assets(self):
        """Build list of assets allowed by this strategy."""
        self.allowed_assets = []
        
        for asset_id, mapping in self.asset_mapping.items():
            if not isinstance(mapping, dict):
                continue
            
            asset_type = mapping.get("type", "")
            
            # Check if asset type is allowed
            if asset_type not in self.asset_types:
                continue
            
            # Check if asset is in filter (if filter is specified)
            if self.assets_filter and asset_id not in self.assets_filter:
                continue
            
            self.allowed_assets.append(asset_id)
        
        # Separate by type for convenience
        self.hp_assets = [
            a for a in self.allowed_assets 
            if self.asset_mapping.get(a, {}).get("type") == "heat_pump"
        ]
        self.ev_assets = [
            a for a in self.allowed_assets 
            if self.asset_mapping.get(a, {}).get("type") == "ev_charger"
        ]
    
    def get_current_time_slot(self, dt: datetime) -> Optional[TimeSlot]:
        """
        Get the time slot that contains the given datetime.
        
        :param dt: Datetime to check
        :return: TimeSlot or None if no matching slot
        """
        for slot in self.time_slots:
            if slot.contains_time(dt):
                return slot
        return None
    
    def get_bid_parameters(self, dt: datetime) -> Dict:
        """
        Get bidding parameters for a specific datetime.
        
        :param dt: Datetime for the bid
        :return: Dictionary with bid parameters
        """
        slot = self.get_current_time_slot(dt)
        
        if slot is None:
            self.logger.warning(
                "No time slot found for %s, using defaults", 
                dt.strftime("%H:%M")
            )
            return {
                "slot_name": "default",
                "flexibility_mw": 0.0,
                "bid_price": 5.0,
                "activation_cost": 2.5,
                "hp_assets": self.hp_assets,
                "ev_assets": [],
                "ev_flexibility_mw": 0.0,
                "ev_bid_price": 0.0,
                "should_bid": False,
            }
        
        # Determine if we should include EVs for this slot
        use_ev = slot.has_ev and len(self.ev_assets) > 0
        
        return {
            "slot_name": slot.name,
            "flexibility_mw": slot.flexibility_mw,
            "bid_price": slot.bid_price,
            "activation_cost": slot.activation_cost,
            "hp_assets": self.hp_assets,
            "ev_assets": self.ev_assets if use_ev else [],
            "ev_flexibility_mw": slot.ev_flexibility_mw if use_ev else 0.0,
            "ev_bid_price": slot.ev_bid_price if use_ev else 0.0,
            "ev_activation_cost": slot.ev_activation_cost if use_ev else 0.0,
            "should_bid": slot.flexibility_mw > 0,
        }
    
    def should_bid(self, dt: datetime) -> bool:
        """Check if we should place a bid at the given time."""
        params = self.get_bid_parameters(dt)
        return params["should_bid"]
    
    def is_asset_allowed(self, asset_id: str) -> bool:
        """Check if an asset is allowed by this strategy."""
        return asset_id in self.allowed_assets
    
    def get_flexibility_quantity(self, dt: datetime, available_flex_mw: float = None) -> float:
        """
        Get the flexibility quantity to bid for a given time.
        
        :param dt: Datetime for the bid
        :param available_flex_mw: Actual available flexibility (optional, for capping)
        :return: Flexibility quantity in MW
        """
        params = self.get_bid_parameters(dt)
        flex_mw = params["flexibility_mw"]
        
        # Add EV flexibility if applicable
        if params["ev_flexibility_mw"] > 0:
            flex_mw += params["ev_flexibility_mw"]
        
        # Cap at available flexibility if provided
        if available_flex_mw is not None and flex_mw > available_flex_mw:
            self.logger.info(
                "Capping flexibility from %.4f to %.4f MW (available)",
                flex_mw, available_flex_mw
            )
            flex_mw = available_flex_mw
        
        return flex_mw
    
    def get_bid_price(self, dt: datetime) -> float:
        """
        Get the bid price for a given time.
        
        :param dt: Datetime for the bid
        :return: Bid price in CHF/MW
        """
        params = self.get_bid_parameters(dt)
        return params["bid_price"]
    
    def check_dso_price_acceptable(self, dt: datetime, dso_price: float) -> bool:
        """
        Check if the DSO's offered price is acceptable.
        
        The DSO price should be >= our minimum acceptable price.
        
        :param dt: Datetime for the bid
        :param dso_price: Price offered by DSO in CHF/MW
        :return: True if price is acceptable
        """
        params = self.get_bid_parameters(dt)
        min_price = params["bid_price"]
        
        # Accept if DSO price >= our minimum
        acceptable = dso_price >= min_price
        
        self.logger.info(
            "Price check: DSO=%.2f CHF/MW, min=%.2f CHF/MW, acceptable=%s",
            dso_price, min_price, acceptable
        )
        
        return acceptable
    
    def print_summary(self):
        """Print a summary of the strategy."""
        print(f"\n{'='*60}")
        print(f"Strategy: {self.strategy_id} - {self.name}")
        print(f"{'='*60}")
        print(f"Description: {self.description}")
        print(f"Asset types: {self.asset_types}")
        print(f"HP assets: {self.hp_assets}")
        print(f"EV assets: {self.ev_assets}")
        print(f"\nTime Slots:")
        print(f"{'Name':<30} {'Time':<15} {'Flex (MW)':<12} {'Price':<10}")
        print("-" * 67)
        for slot in self.time_slots:
            time_range = f"{slot.start.strftime('%H:%M')}-{slot.end.strftime('%H:%M')}"
            print(f"{slot.name:<30} {time_range:<15} {slot.flexibility_mw:<12.4f} {slot.bid_price:<10.1f}")
            if slot.has_ev:
                print(f"  + EV: {slot.ev_flexibility_mw:.4f} MW @ {slot.ev_bid_price:.1f} CHF/MW")
        print(f"{'='*60}\n")


class StrategyManager:
    """
    Manages multiple bidding strategies and provides strategy selection.
    """
    
    def __init__(self, config: dict, logger: logging.Logger = None):
        """
        Initialize the strategy manager.
        
        :param config: Main configuration dictionary
        :param logger: Logger instance
        """
        self.config = config
        self.logger = logger or logging.getLogger(__name__)
        self.asset_mapping = config.get("asset_mapping", {})
        self.strategies_config = config.get("bidding_strategies", {})
        
        # Load all strategies
        self.strategies = {}
        for strategy_id, strategy_config in self.strategies_config.items():
            self.strategies[strategy_id] = BiddingStrategy(
                strategy_id, 
                strategy_config, 
                self.asset_mapping,
                self.logger
            )
        
        self.logger.info("StrategyManager initialized with %d strategies", len(self.strategies))
    
    def get_strategy(self, strategy_id: str) -> Optional[BiddingStrategy]:
        """
        Get a strategy by ID.
        
        :param strategy_id: Strategy identifier
        :return: BiddingStrategy or None
        """
        return self.strategies.get(strategy_id)
    
    def list_strategies(self) -> List[str]:
        """Get list of available strategy IDs."""
        return list(self.strategies.keys())
    
    def print_all_strategies(self):
        """Print summary of all available strategies."""
        print("\n" + "=" * 70)
        print("AVAILABLE BIDDING STRATEGIES")
        print("=" * 70)
        
        for strategy_id, strategy in self.strategies.items():
            uses_ev = "ev_charger" in strategy.asset_types
            print(f"\n{strategy_id}: {strategy.name}")
            print(f"  {strategy.description}")
            print(f"  Assets: HP={len(strategy.hp_assets)}, EV={len(strategy.ev_assets)}")
            print(f"  Time slots: {len(strategy.time_slots)}")
        
        print("\n" + "=" * 70)

