#!/usr/bin/env python3
"""
Strategy Evaluator - Evaluate bidding strategies for flexibility market

This script evaluates the expected performance of different bidding strategies
based on asset characteristics from the configuration file and market assumptions.

Usage:
    python strategy_evaluator.py --config_file conf/test_fm01_aem.json
    python strategy_evaluator.py --config_file conf/test_fm01_aem.json --days 30 --output results.json
"""

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from datetime import datetime, timedelta

# Add parent directory to path for imports
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


@dataclass
class TimeSlot:
    """Represents a time period for bidding."""
    name: str
    start: str  # HH:MM format
    end: str    # HH:MM format
    slots_per_day: int  # Number of 15-min slots in this period
    flexibility_mw: float
    bid_price_chf_mw: float
    activation_rate: float  # Expected activation probability (0-1)
    activation_cost_chf_mwh: float = 2.5  # Default HP activation cost
    include_ev: bool = False
    ev_flexibility_mw: float = 0.0
    ev_activation_rate: float = 0.0
    ev_bid_price: float = 0.0
    ev_activation_cost: float = 4.5  # EV activation cost is higher


@dataclass
class Strategy:
    """Represents a bidding strategy."""
    name: str
    description: str
    assets_included: List[str]
    time_slots: List[TimeSlot]
    uses_ev: bool = False


@dataclass
class StrategyResult:
    """Results of strategy evaluation."""
    strategy_name: str
    total_revenue_chf: float
    total_activation_cost_chf: float
    net_profit_chf: float
    total_slots_activated: int
    avg_profit_per_slot: float
    period_breakdown: List[Dict]


class StrategyEvaluator:
    """Evaluates bidding strategies based on configuration and market assumptions."""
    
    def __init__(self, config: dict, start_date: datetime, end_date: datetime):
        """
        Initialize the evaluator.
        
        :param config: Configuration dictionary from JSON file
        :param start_date: Start date of evaluation period
        :param end_date: End date of evaluation period
        """
        self.config = config
        self.start_date = start_date
        self.end_date = end_date
        self.days = (end_date - start_date).days + 1  # Include both start and end dates
        self.asset_mapping = config.get("asset_mapping", {})
        self.assets_grouping = config.get("assets_grouping", {})
        
        # Extract asset information
        self._parse_assets()
        
        # Define strategies
        self.strategies = self._define_strategies()
    
    def _parse_assets(self):
        """Parse asset information from configuration."""
        self.hp_assets = {}
        self.ev_assets = {}
        
        for asset_id, mapping in self.asset_mapping.items():
            if isinstance(mapping, dict):
                asset_type = mapping.get("type", "")
                capacity_kw = mapping.get("capacity_kw", 0)
                flex_factor = mapping.get("flexibility_factor", 0.5)
                description = mapping.get("description", asset_id)
                
                asset_info = {
                    "id": asset_id,
                    "description": description,
                    "capacity_kw": capacity_kw,
                    "capacity_mw": capacity_kw / 1000,
                    "flexibility_factor": flex_factor,
                    "max_flex_kw": capacity_kw * flex_factor,
                    "max_flex_mw": capacity_kw * flex_factor / 1000,
                }
                
                if asset_type == "heat_pump":
                    self.hp_assets[asset_id] = asset_info
                elif asset_type == "ev_charger":
                    self.ev_assets[asset_id] = asset_info
        
        # Calculate totals
        self.total_hp_capacity_mw = sum(a["capacity_mw"] for a in self.hp_assets.values())
        self.total_hp_flex_mw = sum(a["max_flex_mw"] for a in self.hp_assets.values())
        self.total_ev_capacity_mw = sum(a["capacity_mw"] for a in self.ev_assets.values())
        self.total_ev_flex_mw = sum(a["max_flex_mw"] for a in self.ev_assets.values())
    
    def _define_strategies(self) -> Dict[str, Strategy]:
        """
        Define the 5 bidding strategies.
        
        Strategy parameters are based on:
        - Historical asset usage patterns from analysis scripts
        - DSO willingness-to-pay from market data (~9.5 CHF/MW average)
        - Reasonable activation rate assumptions (5-30%)
        """
        strategies = {}
        
        # =====================================================================
        # STRATEGY 1: HP Only - Conservative full-day coverage
        # =====================================================================
        strategies["strategy_1"] = Strategy(
            name="Strategy 1: HP Only",
            description="Heat pumps only, full day coverage, no EV chargers",
            assets_included=list(self.hp_assets.keys()),
            uses_ev=False,
            time_slots=[
                TimeSlot(
                    name="Morning Peak",
                    start="06:30", end="09:00",
                    slots_per_day=10,  # 2.5 hours × 4 slots/hour
                    flexibility_mw=0.015,  # Moderate HP usage
                    bid_price_chf_mw=9.0,
                    activation_rate=0.20,
                    activation_cost_chf_mwh=2.5
                ),
                TimeSlot(
                    name="Late Morning",
                    start="09:00", end="12:00",
                    slots_per_day=12,
                    flexibility_mw=0.012,
                    bid_price_chf_mw=8.5,
                    activation_rate=0.15,
                    activation_cost_chf_mwh=2.5
                ),
                TimeSlot(
                    name="Afternoon",
                    start="12:00", end="16:00",
                    slots_per_day=16,
                    flexibility_mw=0.008,
                    bid_price_chf_mw=7.0,
                    activation_rate=0.10,
                    activation_cost_chf_mwh=2.5
                ),
                TimeSlot(
                    name="Evening Peak",
                    start="16:00", end="19:00",
                    slots_per_day=12,
                    flexibility_mw=0.010,
                    bid_price_chf_mw=9.5,
                    activation_rate=0.25,
                    activation_cost_chf_mwh=2.5
                ),
                TimeSlot(
                    name="Off-peak",
                    start="19:00", end="06:30",
                    slots_per_day=46,  # ~11.5 hours
                    flexibility_mw=0.006,
                    bid_price_chf_mw=6.0,
                    activation_rate=0.05,
                    activation_cost_chf_mwh=2.5
                ),
            ]
        )
        
        # =====================================================================
        # STRATEGY 2: Full Portfolio + Evening EV Focus
        # =====================================================================
        strategies["strategy_2"] = Strategy(
            name="Strategy 2: Full Portfolio + Evening EV",
            description="All assets, focus on evening when EVs might be charging",
            assets_included=list(self.hp_assets.keys()) + list(self.ev_assets.keys()),
            uses_ev=True,
            time_slots=[
                TimeSlot(
                    name="Morning Peak",
                    start="07:00", end="10:00",
                    slots_per_day=12,
                    flexibility_mw=0.012,
                    bid_price_chf_mw=8.0,
                    activation_rate=0.15,
                    activation_cost_chf_mwh=2.5
                ),
                TimeSlot(
                    name="Midday",
                    start="10:00", end="17:00",
                    slots_per_day=28,
                    flexibility_mw=0.006,
                    bid_price_chf_mw=6.0,
                    activation_rate=0.05,
                    activation_cost_chf_mwh=2.5
                ),
                TimeSlot(
                    name="Evening Peak (HP + EV)",
                    start="17:00", end="20:00",
                    slots_per_day=12,
                    flexibility_mw=0.010,
                    bid_price_chf_mw=9.5,
                    activation_rate=0.25,
                    activation_cost_chf_mwh=2.5,
                    include_ev=True,
                    ev_flexibility_mw=0.0008,  # Very low due to 5-10% occupancy
                    ev_activation_rate=0.15,
                    ev_bid_price=10.0,
                    ev_activation_cost=4.5
                ),
                TimeSlot(
                    name="Off-peak",
                    start="20:00", end="07:00",
                    slots_per_day=44,
                    flexibility_mw=0.005,
                    bid_price_chf_mw=5.5,
                    activation_rate=0.05,
                    activation_cost_chf_mwh=2.5
                ),
            ]
        )
        
        # =====================================================================
        # STRATEGY 3: Morning Peak Focus
        # =====================================================================
        strategies["strategy_3"] = Strategy(
            name="Strategy 3: Morning Peak Focus",
            description="Aggressive bidding during morning peak when Cinema HPs are at maximum",
            assets_included=["ECM97.1", "ECM97.2"],  # Cinema HPs only
            uses_ev=False,
            time_slots=[
                TimeSlot(
                    name="Morning Peak (Aggressive)",
                    start="06:30", end="09:00",
                    slots_per_day=10,
                    flexibility_mw=0.020,  # Cinema HPs at peak consumption
                    bid_price_chf_mw=9.0,
                    activation_rate=0.30,  # Aggressive bidding
                    activation_cost_chf_mwh=2.5
                ),
                TimeSlot(
                    name="Late Morning",
                    start="09:00", end="12:00",
                    slots_per_day=12,
                    flexibility_mw=0.010,
                    bid_price_chf_mw=7.5,
                    activation_rate=0.10,
                    activation_cost_chf_mwh=2.5
                ),
                TimeSlot(
                    name="Afternoon/Evening",
                    start="12:00", end="20:00",
                    slots_per_day=32,
                    flexibility_mw=0.006,
                    bid_price_chf_mw=6.0,
                    activation_rate=0.05,
                    activation_cost_chf_mwh=2.5
                ),
                TimeSlot(
                    name="Night",
                    start="20:00", end="06:30",
                    slots_per_day=42,
                    flexibility_mw=0.004,
                    bid_price_chf_mw=5.0,
                    activation_rate=0.03,
                    activation_cost_chf_mwh=2.5
                ),
            ]
        )
        
        # =====================================================================
        # STRATEGY 4: Hybrid (Strategy 3 morning + Strategy 1 rest)
        # =====================================================================
        strategies["strategy_4"] = Strategy(
            name="Strategy 4: Hybrid (S3+S1)",
            description="Morning peak aggressive (S3) + full day HP coverage (S1)",
            assets_included=list(self.hp_assets.keys()),
            uses_ev=False,
            time_slots=[
                TimeSlot(
                    name="Morning Peak (Aggressive)",
                    start="06:30", end="09:00",
                    slots_per_day=10,
                    flexibility_mw=0.020,  # Cinema HPs at peak
                    bid_price_chf_mw=9.0,
                    activation_rate=0.30,  # Aggressive
                    activation_cost_chf_mwh=2.5
                ),
                TimeSlot(
                    name="Late Morning",
                    start="09:00", end="12:00",
                    slots_per_day=12,
                    flexibility_mw=0.012,
                    bid_price_chf_mw=8.5,
                    activation_rate=0.15,
                    activation_cost_chf_mwh=2.5
                ),
                TimeSlot(
                    name="Afternoon",
                    start="12:00", end="16:00",
                    slots_per_day=16,
                    flexibility_mw=0.008,
                    bid_price_chf_mw=7.0,
                    activation_rate=0.10,
                    activation_cost_chf_mwh=2.5
                ),
                TimeSlot(
                    name="Evening Peak",
                    start="16:00", end="19:00",
                    slots_per_day=12,
                    flexibility_mw=0.010,
                    bid_price_chf_mw=9.5,
                    activation_rate=0.25,
                    activation_cost_chf_mwh=2.5
                ),
                TimeSlot(
                    name="Off-peak",
                    start="19:00", end="06:30",
                    slots_per_day=46,
                    flexibility_mw=0.006,
                    bid_price_chf_mw=6.0,
                    activation_rate=0.05,
                    activation_cost_chf_mwh=2.5
                ),
            ]
        )
        
        # =====================================================================
        # STRATEGY 5: Hybrid2 (Strategy 3 morning + Strategy 2 rest with EVs)
        # =====================================================================
        strategies["strategy_5"] = Strategy(
            name="Strategy 5: Hybrid2 (S3+S2)",
            description="Morning peak aggressive (S3) + evening EV focus (S2)",
            assets_included=list(self.hp_assets.keys()) + list(self.ev_assets.keys()),
            uses_ev=True,
            time_slots=[
                TimeSlot(
                    name="Morning Peak (Aggressive)",
                    start="06:30", end="09:00",
                    slots_per_day=10,
                    flexibility_mw=0.020,
                    bid_price_chf_mw=9.0,
                    activation_rate=0.30,
                    activation_cost_chf_mwh=2.5
                ),
                TimeSlot(
                    name="Late Morning",
                    start="09:00", end="12:00",
                    slots_per_day=12,
                    flexibility_mw=0.010,
                    bid_price_chf_mw=7.5,
                    activation_rate=0.10,
                    activation_cost_chf_mwh=2.5
                ),
                TimeSlot(
                    name="Afternoon",
                    start="12:00", end="17:00",
                    slots_per_day=20,
                    flexibility_mw=0.006,
                    bid_price_chf_mw=6.0,
                    activation_rate=0.05,
                    activation_cost_chf_mwh=2.5
                ),
                TimeSlot(
                    name="Evening Peak (HP + EV)",
                    start="17:00", end="20:00",
                    slots_per_day=12,
                    flexibility_mw=0.010,
                    bid_price_chf_mw=9.5,
                    activation_rate=0.25,
                    activation_cost_chf_mwh=2.5,
                    include_ev=True,
                    ev_flexibility_mw=0.0008,
                    ev_activation_rate=0.15,
                    ev_bid_price=10.0,
                    ev_activation_cost=4.5
                ),
                TimeSlot(
                    name="Off-peak",
                    start="20:00", end="06:30",
                    slots_per_day=42,
                    flexibility_mw=0.005,
                    bid_price_chf_mw=5.5,
                    activation_rate=0.05,
                    activation_cost_chf_mwh=2.5
                ),
            ]
        )
        
        return strategies
    
    def evaluate_time_slot(self, slot: TimeSlot) -> Dict:
        """
        Evaluate a single time slot.
        
        Revenue formula: flexibility_mw × bid_price × activated_slots
        Energy formula: flexibility_mw × 0.25h × activated_slots (MWh)
        Activation cost: energy × activation_cost_per_mwh
        """
        total_slots = slot.slots_per_day * self.days
        
        # HP component
        hp_activated = int(total_slots * slot.activation_rate)
        hp_revenue = slot.flexibility_mw * slot.bid_price_chf_mw * hp_activated
        hp_energy_mwh = slot.flexibility_mw * 0.25 * hp_activated
        hp_activation_cost = hp_energy_mwh * slot.activation_cost_chf_mwh
        hp_net = hp_revenue - hp_activation_cost
        
        result = {
            "name": slot.name,
            "period": f"{slot.start}-{slot.end}",
            "total_slots": total_slots,
            "hp_activated": hp_activated,
            "hp_flexibility_mw": slot.flexibility_mw,
            "hp_bid_price": slot.bid_price_chf_mw,
            "hp_revenue": hp_revenue,
            "hp_energy_mwh": hp_energy_mwh,
            "hp_activation_cost": hp_activation_cost,
            "hp_net": hp_net,
            "ev_activated": 0,
            "ev_revenue": 0,
            "ev_activation_cost": 0,
            "ev_net": 0,
            "total_revenue": hp_revenue,
            "total_activation_cost": hp_activation_cost,
            "total_net": hp_net,
            "total_activated": hp_activated,
        }
        
        # EV component (if included)
        if slot.include_ev and slot.ev_flexibility_mw > 0:
            ev_activated = int(total_slots * slot.ev_activation_rate)
            ev_revenue = slot.ev_flexibility_mw * slot.ev_bid_price * ev_activated
            ev_energy_mwh = slot.ev_flexibility_mw * 0.25 * ev_activated
            ev_activation_cost = ev_energy_mwh * slot.ev_activation_cost
            ev_net = ev_revenue - ev_activation_cost
            
            result["ev_activated"] = ev_activated
            result["ev_flexibility_mw"] = slot.ev_flexibility_mw
            result["ev_bid_price"] = slot.ev_bid_price
            result["ev_revenue"] = ev_revenue
            result["ev_energy_mwh"] = ev_energy_mwh
            result["ev_activation_cost"] = ev_activation_cost
            result["ev_net"] = ev_net
            result["total_revenue"] = hp_revenue + ev_revenue
            result["total_activation_cost"] = hp_activation_cost + ev_activation_cost
            result["total_net"] = hp_net + ev_net
            result["total_activated"] = hp_activated + ev_activated
        
        return result
    
    def evaluate_strategy(self, strategy: Strategy) -> StrategyResult:
        """Evaluate a complete strategy."""
        period_results = []
        total_revenue = 0
        total_cost = 0
        total_activated = 0
        
        for slot in strategy.time_slots:
            result = self.evaluate_time_slot(slot)
            period_results.append(result)
            total_revenue += result["total_revenue"]
            total_cost += result["total_activation_cost"]
            total_activated += result["total_activated"]
        
        net_profit = total_revenue - total_cost
        avg_per_slot = net_profit / total_activated if total_activated > 0 else 0
        
        return StrategyResult(
            strategy_name=strategy.name,
            total_revenue_chf=total_revenue,
            total_activation_cost_chf=total_cost,
            net_profit_chf=net_profit,
            total_slots_activated=total_activated,
            avg_profit_per_slot=avg_per_slot,
            period_breakdown=period_results
        )
    
    def evaluate_all(self) -> Dict[str, StrategyResult]:
        """Evaluate all strategies."""
        results = {}
        for strategy_id, strategy in self.strategies.items():
            results[strategy_id] = self.evaluate_strategy(strategy)
        return results
    
    def print_asset_summary(self):
        """Print summary of configured assets."""
        print("\n" + "=" * 70)
        print("ASSET CONFIGURATION SUMMARY")
        print("=" * 70)
        
        print("\nHeat Pumps:")
        print(f"{'Asset':<12} {'Description':<20} {'Capacity':<12} {'Max Flex':<12}")
        print("-" * 56)
        for asset_id, info in self.hp_assets.items():
            print(f"{asset_id:<12} {info['description']:<20} {info['capacity_kw']:>8.1f} kW  {info['max_flex_kw']:>8.1f} kW")
        print(f"{'TOTAL':<12} {'':<20} {self.total_hp_capacity_mw*1000:>8.1f} kW  {self.total_hp_flex_mw*1000:>8.1f} kW")
        
        print("\nEV Chargers:")
        print(f"{'Asset':<12} {'Description':<20} {'Capacity':<12} {'Max Flex':<12}")
        print("-" * 56)
        for asset_id, info in self.ev_assets.items():
            print(f"{asset_id:<12} {info['description']:<20} {info['capacity_kw']:>8.1f} kW  {info['max_flex_kw']:>8.1f} kW")
        print(f"{'TOTAL':<12} {'':<20} {self.total_ev_capacity_mw*1000:>8.1f} kW  {self.total_ev_flex_mw*1000:>8.1f} kW")
        
        print("\n" + "=" * 70)
    
    def print_strategy_details(self, strategy_id: str, result: StrategyResult):
        """Print detailed results for a single strategy."""
        strategy = self.strategies[strategy_id]
        
        print("\n" + "=" * 70)
        print(f"{strategy.name}")
        print(f"Description: {strategy.description}")
        print(f"Uses EV Chargers: {'Yes' if strategy.uses_ev else 'No'}")
        print(f"Assets: {', '.join(strategy.assets_included)}")
        print("=" * 70)
        
        for period in result.period_breakdown:
            print(f"\n--- {period['name']} ({period['period']}) ---")
            print(f"  Slots: {period['total_slots']} total, {period['hp_activated']} HP activated")
            print(f"  HP: {period['hp_flexibility_mw']:.3f} MW × {period['hp_bid_price']:.1f} CHF/MW = {period['hp_revenue']:.2f} CHF")
            print(f"      Cost: {period['hp_energy_mwh']:.3f} MWh × {period.get('hp_activation_cost', 0)/max(period['hp_energy_mwh'], 0.001):.1f} CHF/MWh = {period['hp_activation_cost']:.2f} CHF")
            print(f"      NET: {period['hp_net']:.2f} CHF")
            
            if period.get("ev_activated", 0) > 0:
                print(f"  EV: {period.get('ev_flexibility_mw', 0):.4f} MW × {period.get('ev_bid_price', 0):.1f} CHF/MW × {period['ev_activated']} = {period['ev_revenue']:.2f} CHF")
                print(f"      NET: {period['ev_net']:.2f} CHF")
        
        print("\n" + "-" * 70)
        print(f"TOTAL ({self.start_date.strftime('%Y-%m-%d')} to {self.end_date.strftime('%Y-%m-%d')} - {self.days} days)")
        print("-" * 70)
        print(f"  Total slots activated: {result.total_slots_activated}")
        print(f"  Total revenue:         {result.total_revenue_chf:.2f} CHF")
        print(f"  Total activation cost: {result.total_activation_cost_chf:.2f} CHF")
        print(f"  NET PROFIT:            {result.net_profit_chf:.2f} CHF")
        print(f"  Average per slot:      {result.avg_profit_per_slot:.3f} CHF")
        print("=" * 70)
    
    def print_comparison_table(self, results: Dict[str, StrategyResult]):
        """Print comparison table of all strategies."""
        print("\n" + "=" * 90)
        print("STRATEGY COMPARISON")
        print("=" * 90)
        print(f"{'Strategy':<35} {'Net Profit':<15} {'Slots':<10} {'Avg/Slot':<12} {'Uses EV':<10}")
        print("-" * 90)
        
        # Sort by net profit descending
        sorted_results = sorted(results.items(), key=lambda x: x[1].net_profit_chf, reverse=True)
        
        best_strategy = sorted_results[0][0]
        
        for strategy_id, result in sorted_results:
            strategy = self.strategies[strategy_id]
            marker = "⭐ BEST" if strategy_id == best_strategy else ""
            ev_str = "Yes" if strategy.uses_ev else "No"
            print(f"{strategy.name:<35} {result.net_profit_chf:>10.2f} CHF  {result.total_slots_activated:>6}    {result.avg_profit_per_slot:>8.3f} CHF  {ev_str:<6} {marker}")
        
        print("=" * 90)
        
        # Recommendation
        best_result = results[best_strategy]
        print(f"\n✅ RECOMMENDATION: {self.strategies[best_strategy].name}")
        print(f"   Period: {self.start_date.strftime('%Y-%m-%d')} to {self.end_date.strftime('%Y-%m-%d')} ({self.days} days)")
        print(f"   Expected net profit: {best_result.net_profit_chf:.2f} CHF")
    
    def export_results(self, results: Dict[str, StrategyResult], output_file: str):
        """Export results to JSON file."""
        export_data = {
            "evaluation_date": datetime.now().isoformat(),
            "evaluation_period": {
                "start_date": self.start_date.strftime("%Y-%m-%d"),
                "end_date": self.end_date.strftime("%Y-%m-%d"),
                "days": self.days
            },
            "asset_summary": {
                "heat_pumps": {
                    "count": len(self.hp_assets),
                    "total_capacity_mw": self.total_hp_capacity_mw,
                    "total_max_flex_mw": self.total_hp_flex_mw,
                    "assets": self.hp_assets
                },
                "ev_chargers": {
                    "count": len(self.ev_assets),
                    "total_capacity_mw": self.total_ev_capacity_mw,
                    "total_max_flex_mw": self.total_ev_flex_mw,
                    "assets": self.ev_assets
                }
            },
            "strategies": {}
        }
        
        for strategy_id, result in results.items():
            strategy = self.strategies[strategy_id]
            export_data["strategies"][strategy_id] = {
                "name": strategy.name,
                "description": strategy.description,
                "uses_ev": strategy.uses_ev,
                "assets_included": strategy.assets_included,
                "results": {
                    "net_profit_chf": round(result.net_profit_chf, 2),
                    "total_revenue_chf": round(result.total_revenue_chf, 2),
                    "total_activation_cost_chf": round(result.total_activation_cost_chf, 2),
                    "total_slots_activated": result.total_slots_activated,
                    "avg_profit_per_slot_chf": round(result.avg_profit_per_slot, 4),
                },
                "period_breakdown": result.period_breakdown
            }
        
        with open(output_file, "w") as f:
            json.dump(export_data, f, indent=2, default=str)
        
        print(f"\n📄 Results exported to: {output_file}")


def parse_date(date_str: str) -> datetime:
    """Parse a date string in YYYY-MM-DD format."""
    try:
        return datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        raise argparse.ArgumentTypeError(f"Invalid date format: {date_str}. Use YYYY-MM-DD")


def main():
    parser = argparse.ArgumentParser(
        description="Evaluate bidding strategies for flexibility market"
    )
    parser.add_argument(
        "--config_file",
        required=True,
        help="Path to configuration file (e.g., conf/test_fm01_aem.json)"
    )
    parser.add_argument(
        "--start_date",
        type=parse_date,
        help="Start date of evaluation period (YYYY-MM-DD). Default: 30 days ago"
    )
    parser.add_argument(
        "--end_date",
        type=parse_date,
        help="End date of evaluation period (YYYY-MM-DD). Default: today"
    )
    parser.add_argument(
        "--output",
        help="Output JSON file for results (optional)"
    )
    parser.add_argument(
        "--strategy",
        choices=["strategy_1", "strategy_2", "strategy_3", "strategy_4", "strategy_5", "all"],
        default="all",
        help="Strategy to evaluate (default: all)"
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show detailed breakdown for each strategy"
    )
    
    args = parser.parse_args()
    
    # Load configuration
    if not os.path.isfile(args.config_file):
        print(f"ERROR: Configuration file not found: {args.config_file}")
        sys.exit(1)
    
    with open(args.config_file, "r") as f:
        config = json.load(f)
    
    # Set default dates if not provided
    if args.end_date is None:
        end_date = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        end_date = args.end_date
    
    if args.start_date is None:
        start_date = end_date - timedelta(days=29)  # 30 days including end_date
    else:
        start_date = args.start_date
    
    # Validate date range
    if start_date > end_date:
        print(f"ERROR: Start date ({start_date.strftime('%Y-%m-%d')}) must be before end date ({end_date.strftime('%Y-%m-%d')})")
        sys.exit(1)
    
    days = (end_date - start_date).days + 1
    
    print(f"Configuration loaded from: {args.config_file}")
    print(f"Evaluation period: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')} ({days} days)")
    
    # Initialize evaluator
    evaluator = StrategyEvaluator(config, start_date=start_date, end_date=end_date)
    
    # Print asset summary
    evaluator.print_asset_summary()
    
    # Evaluate strategies
    if args.strategy == "all":
        results = evaluator.evaluate_all()
        
        if args.verbose:
            for strategy_id, result in results.items():
                evaluator.print_strategy_details(strategy_id, result)
        
        evaluator.print_comparison_table(results)
    else:
        strategy = evaluator.strategies[args.strategy]
        result = evaluator.evaluate_strategy(strategy)
        evaluator.print_strategy_details(args.strategy, result)
        results = {args.strategy: result}
    
    # Export results if requested
    if args.output:
        evaluator.export_results(results, args.output)


if __name__ == "__main__":
    main()

