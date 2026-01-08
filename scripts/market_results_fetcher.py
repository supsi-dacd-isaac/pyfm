#!/usr/bin/env python3
"""
Market Results Fetcher Script

This script periodically fetches results from closed markets (15-minute time slots)
for a configurable time period and exports them to a JSON file.

Usage:
    python market_results_fetcher.py --config_file ../conf/test_fm01_aem.json --player dso

Configuration options (via command line or config file):
    --period_mode: "dynamic" (last N hours) or "static" (fixed time range)
    --hours_back: Number of hours to look back (for dynamic mode)
    --period_from: Start of period in ISO format (for static mode)
    --period_to: End of period in ISO format (for static mode)
    --interval: Fetch interval in seconds (0 for single run)
    --output_file: Path to output JSON file
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from classes.nodes_interface import NODESInterface


def format_mw(value) -> str:
    """Return MW values with three decimal places as strings."""
    try:
        return f"{float(value):.3f}"
    except (TypeError, ValueError):
        return "0.000"


class MarketResultsFetcher:
    """
    Fetches closed market results from the NODES API.
    """

    def __init__(self, cfg: dict, player_cfg: dict, logger: logging.Logger):
        """
        Initialize the fetcher.
        
        :param cfg: Main configuration dictionary
        :param player_cfg: Player-specific configuration
        :param logger: Logger instance
        """
        self.cfg = cfg
        self.player_cfg = player_cfg
        self.logger = logger
        self.nodes_interface = NODESInterface(cfg["nodesAPI"], logger)
        self.nodes_interface.set_token(player_cfg)
        
        # Store actor ID from config
        self.actor_id = player_cfg.get("id")
        self.actor_role = player_cfg.get("role")
        
        # Get organization info
        self.organization = self._get_organization()
        
        # Get market info
        self.markets = self._get_markets()
        
        # Get portfolios for FSP players (assets loaded separately if needed)
        self.portfolios = {}
        self.include_assets = False
        
        # Cache for organization lookups
        self._org_cache = {}
    
    def load_portfolios(self, include_assets: bool = False):
        """
        Load portfolios for FSP organization.
        
        :param include_assets: If True, include asset details for each portfolio
        """
        if self.actor_role == "fsp" and self.organization:
            self.include_assets = include_assets
            self.portfolios = self._get_portfolios(include_assets=include_assets)
        
    def _get_organization(self) -> Optional[dict]:
        """Get organization information for the player."""
        res = self.nodes_interface.get_request(
            "%sorganizations?name=%s" % (
                self.nodes_interface.cfg["mainEndpoint"],
                self.player_cfg["name"]
            )
        )
        if res and "items" in res and len(res["items"]) > 0:
            return res["items"][0]
        return None

    def _get_markets(self) -> list:
        """Get available markets."""
        res = self.nodes_interface.get_request(
            "%smarkets" % self.nodes_interface.cfg["mainEndpoint"]
        )
        if res and "items" in res:
            return res["items"]
        return []

    def _get_portfolios(self, include_assets: bool = False) -> dict:
        """
        Get portfolios for FSP organization.
        Returns a dict mapping portfolio ID to portfolio info (id, name, assets).
        
        :param include_assets: If True, fetch and include asset details for each portfolio
        """
        if not self.organization:
            return {}
        
        org_id = self.organization.get("id")
        res = self.nodes_interface.get_request(
            "%sAssetPortfolios?managedByOrganizationId=%s" % (
                self.nodes_interface.cfg["mainEndpoint"],
                org_id
            )
        )
        
        portfolios = {}
        if res and "items" in res:
            for portfolio in res["items"]:
                portfolio_id = portfolio.get("id")
                portfolio_info = {
                    "id": portfolio_id,
                    "name": portfolio.get("name"),
                    "description": portfolio.get("description"),
                }
                
                # Fetch assets if requested
                if include_assets:
                    assets = self._get_assets_for_portfolio(portfolio_id)
                    portfolio_info["assets"] = assets
                
                portfolios[portfolio_id] = portfolio_info
            
            self.logger.info(f"Found {len(portfolios)} portfolios for organization")
        
        return portfolios

    def _get_assets_for_portfolio(self, portfolio_id: str) -> list:
        """
        Get assets assigned to a portfolio.
        
        :param portfolio_id: Portfolio ID
        :return: List of asset info dicts
        """
        # First get portfolio assignments
        assignments_res = self.nodes_interface.get_request(
            "%sassetportfolioassignments?assetPortfolioId=%s" % (
                self.nodes_interface.cfg["mainEndpoint"],
                portfolio_id
            )
        )
        
        if not assignments_res or "items" not in assignments_res:
            return []
        
        # Get grid assignments to map to assets
        org_id = self.organization.get("id")
        grid_assignments_res = self.nodes_interface.get_request(
            "%sassetgridassignments?managedByOrganizationId=%s" % (
                self.nodes_interface.cfg["mainEndpoint"],
                org_id
            )
        )
        
        grid_assignments = {}
        if grid_assignments_res and "items" in grid_assignments_res:
            for ga in grid_assignments_res["items"]:
                grid_assignments[ga.get("id")] = ga
        
        # Get all assets for the organization
        assets_res = self.nodes_interface.get_request(
            "%sassets?operatedByOrganizationId=%s" % (
                self.nodes_interface.cfg["mainEndpoint"],
                org_id
            )
        )
        
        assets_by_id = {}
        if assets_res and "items" in assets_res:
            for asset in assets_res["items"]:
                assets_by_id[asset.get("id")] = asset
        
        # Build list of assets for this portfolio
        portfolio_assets = []
        for assignment in assignments_res["items"]:
            grid_assignment_id = assignment.get("assetGridAssignmentId")
            if grid_assignment_id in grid_assignments:
                grid_assignment = grid_assignments[grid_assignment_id]
                asset_id = grid_assignment.get("assetId")
                mpid = grid_assignment.get("mpid")
                
                if asset_id in assets_by_id:
                    asset = assets_by_id[asset_id]
                    portfolio_assets.append({
                        "id": asset_id,
                        "name": asset.get("name"),
                        "mpid": mpid,
                        "assetType": asset.get("assetType"),
                        "description": asset.get("description"),
                    })
        
        self.logger.info(f"Found {len(portfolio_assets)} assets for portfolio {portfolio_id}")
        return portfolio_assets

    def get_closed_orders(
        self,
        period_from: datetime,
        period_to: datetime,
        market_id: Optional[str] = None
    ) -> list:
        """
        Fetch orders that have been closed/settled in the given period.
        
        :param period_from: Start of the period (UTC)
        :param period_to: End of the period (UTC)
        :param market_id: Optional market ID filter
        :return: List of closed orders
        """
        from_str = period_from.strftime("%Y-%m-%dT%H:%M:%SZ")
        to_str = period_to.strftime("%Y-%m-%dT%H:%M:%SZ")
        
        # Build filter parameters
        filter_params = [
            f"periodFrom.GreaterThanOrEqual={from_str}",
            f"periodTo.LessThanOrEqual={to_str}",
        ]
        
        if market_id:
            filter_params.append(f"marketId={market_id}")
            
        filter_str = "&".join(filter_params)
        
        endpoint = "%sorders?%s" % (
            self.nodes_interface.cfg["mainEndpoint"],
            filter_str
        )
        
        self.logger.info(f"Fetching orders from {from_str} to {to_str}")
        res = self.nodes_interface.get_request(endpoint)
        
        if res and "items" in res:
            # Filter to only include closed/settled orders
            closed_orders = [
                order for order in res["items"]
                if order.get("completionType") is not None
            ]
            self.logger.info(
                f"Found {len(closed_orders)} closed orders out of {len(res['items'])} total"
            )
            return closed_orders
        return []

    def get_settlements(
        self,
        period_from: datetime,
        period_to: datetime,
        market_id: Optional[str] = None
    ) -> list:
        """
        Fetch settlement data for the given period.
        
        Note: The settlements endpoint may not be available in all NODES API environments.
        This method attempts a single request and returns an empty list if not available.
        
        :param period_from: Start of the period (UTC)
        :param period_to: End of the period (UTC)
        :param market_id: Optional market ID filter
        :return: List of settlements
        """
        from_str = period_from.strftime("%Y-%m-%dT%H:%M:%SZ")
        to_str = period_to.strftime("%Y-%m-%dT%H:%M:%SZ")
        
        filter_params = [
            f"periodFrom={from_str}",
            f"periodTo={to_str}",
        ]
        
        if market_id:
            filter_params.append(f"marketId={market_id}")
            
        filter_str = "&".join(filter_params)
        
        endpoint = "%ssettlements?%s" % (
            self.nodes_interface.cfg["mainEndpoint"],
            filter_str
        )
        
        # Try only once for settlements (endpoint may not be available)
        self.logger.info(f"Fetching settlements from {from_str} to {to_str}")
        try:
            import requests
            import http
            response = requests.get(
                endpoint,
                headers=self.nodes_interface.headers,
                timeout=self.nodes_interface.cfg.get("requestTimeout", 3)
            )
            if response.status_code == http.HTTPStatus.OK:
                res = response.json()
                if "items" in res:
                    return res["items"]
                elif isinstance(res, list):
                    return res
            elif response.status_code == http.HTTPStatus.NOT_FOUND:
                self.logger.info("Settlements endpoint not available (404) - skipping")
            else:
                self.logger.warning(f"Settlements request failed with status {response.status_code}")
        except Exception as e:
            self.logger.warning(f"Failed to fetch settlements: {str(e)}")
        
        return []

    def get_trades(
        self,
        period_from: datetime,
        period_to: datetime,
        market_id: Optional[str] = None
    ) -> list:
        """
        Fetch trade data for the given period.
        
        :param period_from: Start of the period (UTC)
        :param period_to: End of the period (UTC)
        :param market_id: Optional market ID filter
        :return: List of trades
        """
        from_str = period_from.strftime("%Y-%m-%dT%H:%M:%SZ")
        to_str = period_to.strftime("%Y-%m-%dT%H:%M:%SZ")
        
        filter_params = [
            f"periodFrom.GreaterThanOrEqual={from_str}",
            f"periodTo.LessThanOrEqual={to_str}",
        ]
        
        if market_id:
            filter_params.append(f"marketId={market_id}")
            
        filter_str = "&".join(filter_params)
        
        endpoint = "%strades?%s" % (
            self.nodes_interface.cfg["mainEndpoint"],
            filter_str
        )
        
        self.logger.info(f"Fetching trades from {from_str} to {to_str}")
        res = self.nodes_interface.get_request(endpoint)
        
        if res and "items" in res:
            return res["items"]
        elif res and isinstance(res, list):
            return res
        return []

    def get_market_results(
        self,
        period_from: datetime,
        period_to: datetime,
        market_name: Optional[str] = None,
        include_settlements: bool = False
    ) -> dict:
        """
        Fetch comprehensive market results for the given period.
        
        :param period_from: Start of the period (UTC)
        :param period_to: End of the period (UTC)
        :param market_name: Optional market name filter
        :return: Dictionary containing market results
        """
        # Find market ID if market name is provided
        market_id = None
        if market_name:
            for market in self.markets:
                if market.get("name") == market_name:
                    market_id = market.get("id")
                    break
        
        # Fetch all relevant data
        closed_orders = self.get_closed_orders(period_from, period_to, market_id)
        trades = self.get_trades(period_from, period_to, market_id)
        
        # Settlements are optional (endpoint may not be available in all environments)
        if include_settlements:
            settlements = self.get_settlements(period_from, period_to, market_id)
        else:
            settlements = []
        
        # Group orders by 15-minute time slots
        time_slots = self._group_by_time_slots(closed_orders, period_from, period_to)
        
        # Enrich trades with portfolio names and organization details
        enriched_trades = self._enrich_trades_with_details(trades)
        
        # Enrich orders with buyer, seller, and portfolio information
        enriched_orders = self._enrich_orders_with_participants(closed_orders)
        
        # Build metadata
        metadata = {
            "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "period_from": period_from.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "period_to": period_to.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "actor_id": self.actor_id,
            "actor_role": self.actor_role,
            "market_name": market_name,
            "market_id": market_id,
            "organization": self.organization.get("name") if self.organization else None,
            "organization_id": self.organization.get("id") if self.organization else None,
            "granularity_minutes": 15,
            "total_time_slots": len(time_slots),
            "total_closed_orders": len(closed_orders),
            "total_trades": len(trades),
            "total_settlements": len(settlements),
        }
        
        # Add portfolios info for FSP
        if self.actor_role == "fsp" and self.portfolios:
            metadata["portfolios"] = list(self.portfolios.values())
        
        results = {
            "metadata": metadata,
            "time_slots": time_slots,
            "closed_orders": enriched_orders,
            "trades": enriched_trades,
            "settlements": settlements,
            "summary": self._calculate_summary(enriched_orders, enriched_trades),
        }
        
        return results

    def _group_by_time_slots(
        self,
        orders: list,
        period_from: datetime,
        period_to: datetime
    ) -> dict:
        """
        Group orders by 15-minute time slots.
        
        :param orders: List of orders
        :param period_from: Start of the period
        :param period_to: End of the period
        :return: Dictionary with time slots as keys
        """
        time_slots = {}
        granularity = timedelta(minutes=15)
        
        # Generate all time slots in the period
        current_slot = period_from.replace(
            minute=(period_from.minute // 15) * 15,
            second=0,
            microsecond=0
        )
        
        while current_slot < period_to:
            slot_key = current_slot.strftime("%Y-%m-%dT%H:%M:%SZ")
            time_slots[slot_key] = {
                "period_from": slot_key,
                "period_to": (current_slot + granularity).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "buy_orders": [],
                "sell_orders": [],
                "total_buy_quantity": 0.0,
                "total_sell_quantity": 0.0,
                "matched_quantity": 0.0,
                "avg_price": 0.0,
                "status": "no_activity",
            }
            current_slot += granularity
        
        # Assign orders to time slots
        for order in orders:
            order_period_from = order.get("periodFrom")
            if order_period_from in time_slots:
                slot = time_slots[order_period_from]
                
                if order.get("side") == "Buy":
                    slot["buy_orders"].append(order)
                    slot["total_buy_quantity"] += float(order.get("quantity", 0) or 0)
                elif order.get("side") == "Sell":
                    slot["sell_orders"].append(order)
                    slot["total_sell_quantity"] += float(order.get("quantity", 0) or 0)
                
                # Update matched quantity using quantityCompleted (actual traded amount)
                # This works for all completion types including Expired orders with partial fills
                quantity_completed = float(order.get("quantityCompleted", 0) or 0)
                if quantity_completed > 0:
                    slot["matched_quantity"] += quantity_completed
                elif order.get("completionType") == "PartiallyFilled":
                    # Fallback for compatibility
                    filled_qty = float(order.get("filledQuantity", 0) or 0)
                    slot["matched_quantity"] += filled_qty
                
                # Calculate average price for orders that had trades
                prices = []
                for o in slot["buy_orders"] + slot["sell_orders"]:
                    qty_completed = float(o.get("quantityCompleted", 0) or 0)
                    if qty_completed > 0:
                        prices.append(float(o.get("unitPrice", 0)))
                if prices:
                    slot["avg_price"] = round(sum(prices) / len(prices), 2)
                
                # Update status
                if slot["matched_quantity"] > 0:
                    slot["status"] = "cleared"
                elif slot["buy_orders"] or slot["sell_orders"]:
                    slot["status"] = "no_match"
        
        return time_slots

    def _get_organization_by_id(self, org_id: str) -> Optional[dict]:
        """
        Get organization details by ID (with caching).
        
        :param org_id: Organization ID
        :return: Organization info dict or None
        """
        if not org_id:
            return None
        
        # Check cache first
        if org_id in self._org_cache:
            return self._org_cache[org_id]
        
        # Fetch from API
        res = self.nodes_interface.get_request(
            "%sorganizations/%s" % (
                self.nodes_interface.cfg["mainEndpoint"],
                org_id
            )
        )
        
        if res and isinstance(res, dict) and "id" in res:
            org_info = {
                "id": res.get("id"),
                "name": res.get("name"),
            }
            self._org_cache[org_id] = org_info
            return org_info
        
        return None

    def _get_portfolio_by_id(self, portfolio_id: str) -> Optional[dict]:
        """
        Get portfolio details by ID.
        
        :param portfolio_id: Portfolio ID
        :return: Portfolio info dict or None
        """
        if not portfolio_id:
            return None
        
        # Check if already in our portfolios cache
        if portfolio_id in self.portfolios:
            return self.portfolios[portfolio_id]
        
        # Fetch from API
        res = self.nodes_interface.get_request(
            "%sAssetPortfolios/%s" % (
                self.nodes_interface.cfg["mainEndpoint"],
                portfolio_id
            )
        )
        
        if res and isinstance(res, dict) and "id" in res:
            portfolio_info = {
                "id": res.get("id"),
                "name": res.get("name"),
                "description": res.get("description"),
            }
            # Add to cache
            self.portfolios[portfolio_id] = portfolio_info
            return portfolio_info
        
        return None

    def _enrich_orders_with_participants(self, orders: list) -> list:
        """
        Enrich orders with buyer, seller, and portfolio information.
        
        :param orders: List of orders
        :return: Enriched orders list
        """
        enriched = []
        
        for order in orders:
            order_copy = order.copy()
            
            owner_org_id = order.get("ownerOrganizationId")
            side = order.get("side")
            portfolio_id = order.get("assetPortfolioId")
            
            # Get owner organization info
            owner_org = self._get_organization_by_id(owner_org_id)
            
            # Determine buyer and seller based on side
            if side == "Buy":
                # Owner is the buyer (typically DSO)
                order_copy["buyer"] = {
                    "organizationId": owner_org_id,
                    "organizationName": owner_org.get("name") if owner_org else None,
                }
                order_copy["seller"] = None  # Will be filled from counterpart if available
                order_copy["portfolio"] = None
            elif side == "Sell":
                # Owner is the seller (typically FSP)
                order_copy["seller"] = {
                    "organizationId": owner_org_id,
                    "organizationName": owner_org.get("name") if owner_org else None,
                }
                order_copy["buyer"] = None  # Will be filled from counterpart if available
                
                # Get portfolio info for sell orders
                if portfolio_id:
                    portfolio = self._get_portfolio_by_id(portfolio_id)
                    order_copy["portfolio"] = {
                        "id": portfolio_id,
                        "name": portfolio.get("name") if portfolio else None,
                    }
                else:
                    order_copy["portfolio"] = None
            
            enriched.append(order_copy)
        
        return enriched

    def _enrich_trades_with_details(self, trades: list) -> list:
        """
        Enrich trades with portfolio names (FSP) and owner organization names.
        
        :param trades: List of trades
        :return: Enriched trades list
        """
        enriched = []
        for trade in trades:
            trade_copy = trade.copy()
            
            # Add portfolio name for FSP
            portfolio_id = trade.get("assetPortfolioId")
            if portfolio_id and portfolio_id in self.portfolios:
                trade_copy["portfolioName"] = self.portfolios[portfolio_id].get("name")
            
            # Add owner organization name (the party who made this trade)
            owner_org_id = trade.get("ownerOrganizationId")
            if owner_org_id:
                owner_name = self._get_owner_organization_name(owner_org_id)
                if owner_name:
                    trade_copy["ownerOrganizationName"] = owner_name
            
            enriched.append(trade_copy)
        
        return enriched

    def _get_owner_organization_name(self, org_id: str) -> Optional[str]:
        """
        Get organization name by ID (uses existing cache).
        
        :param org_id: Organization ID
        :return: Organization name or None
        """
        org = self._get_organization_by_id(org_id)
        if org and isinstance(org, dict):
            return org.get("name")
        return None

    @staticmethod
    def _extract_executed_quantity(order: dict) -> float:
        raw_quantity = order.get("quantity")
        quantity = float(raw_quantity) if raw_quantity not in (None, "") else 0.0
        if quantity != 0.0:
            return quantity
        for key in ("quantityCompleted", "filledQuantity"):
            fallback = order.get(key)
            if fallback not in (None, ""):
                fallback_value = float(fallback)
                if fallback_value != 0.0:
                    return fallback_value
        return quantity

    def _calculate_summary(self, orders: list, trades: list) -> dict:
        """
        Calculate summary statistics.
        
        :param orders: List of orders
        :param trades: List of trades
        :return: Summary dictionary
        """
        total_buy_quantity = 0.0
        total_sell_quantity = 0.0
        total_matched_quantity = 0.0
        filled_orders = 0
        partially_filled_orders = 0
        cancelled_orders = 0
        expired_orders = 0
        expired_with_partial_fill = 0
        expired_with_no_fill = 0
        
        for order in orders:
            quantity = self._extract_executed_quantity(order)
            completion_type = order.get("completionType")
            
            if order.get("side") == "Buy":
                total_buy_quantity += quantity
            elif order.get("side") == "Sell":
                total_sell_quantity += quantity
            
            # Get the actual matched/traded quantity from quantityCompleted
            # This field contains the real traded amount regardless of completion type
            quantity_completed = float(order.get("quantityCompleted", 0) or 0)
            
            if completion_type == "Filled":
                filled_orders += 1
                total_matched_quantity += quantity_completed
            elif completion_type == "PartiallyFilled":
                partially_filled_orders += 1
                # Use quantityCompleted, fall back to filledQuantity for compatibility
                matched = quantity_completed or float(order.get("filledQuantity", 0) or 0)
                total_matched_quantity += matched
            elif completion_type == "Cancelled":
                cancelled_orders += 1
            elif completion_type == "Expired":
                expired_orders += 1
                # Expired orders may have partial fills recorded in quantityCompleted
                total_matched_quantity += quantity_completed
                # Track expired orders with/without partial fills separately
                if quantity_completed > 0:
                    expired_with_partial_fill += 1
                else:
                    expired_with_no_fill += 1
        
        return {
            "total_buy_quantity_mw": format_mw(total_buy_quantity),
            "total_sell_quantity_mw": format_mw(total_sell_quantity),
            "total_matched_quantity_mw": format_mw(total_matched_quantity),
            "filled_orders": filled_orders,
            "partially_filled_orders": partially_filled_orders,
            "cancelled_orders": cancelled_orders,
            "expired_orders": expired_orders,
            "expired_with_partial_fill": expired_with_partial_fill,
            "expired_with_no_fill": expired_with_no_fill,
            "total_trades": len(trades),
        }


def generate_output_filename(
    output_dir: str,
    actor_role: str,
    actor_id: str,
    period_mode: str,
    hours_back: float = None,
    period_from: str = None,
    period_to: str = None
) -> str:
    """
    Generate a descriptive output filename.
    
    :param output_dir: Output directory
    :param actor_role: Actor role (dso/fsp)
    :param actor_id: Actor identifier
    :param period_mode: Period mode (dynamic/static)
    :param hours_back: Hours back (for dynamic mode)
    :param period_from: Period start (for static mode)
    :param period_to: Period end (for static mode)
    :return: Generated filename (without extension)
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    
    # Build period description
    if period_mode == "dynamic" and hours_back:
        hours_str = f"{hours_back:.0f}" if hours_back == int(hours_back) else f"{hours_back}"
        period_desc = f"last{hours_str}h"
    else:
        period_desc = "static"
    
    # Build filename
    filename = f"market_results_{actor_role}_{actor_id.lower()}_{period_desc}_created_at_{timestamp}"
    
    return os.path.join(output_dir, filename)


def save_results_to_json(results: dict, output_file: str, logger: logging.Logger):
    """
    Save market results to a JSON file.
    
    :param results: Market results dictionary
    :param output_file: Path to output file
    :param logger: Logger instance
    """
    try:
        # Ensure directory exists
        output_dir = os.path.dirname(output_file)
        if output_dir and not os.path.exists(output_dir):
            os.makedirs(output_dir)
        
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        
        logger.info(f"Results saved to {output_file}")
    except Exception as e:
        logger.error(f"Failed to save results to {output_file}: {str(e)}")


def save_results_to_markdown(results: dict, output_file: str, logger: logging.Logger):
    """
    Save market results to a markdown README file.
    
    :param results: Market results dictionary
    :param output_file: Path to output file (should end with .md)
    :param logger: Logger instance
    """
    try:
        # Ensure directory exists
        output_dir = os.path.dirname(output_file)
        if output_dir and not os.path.exists(output_dir):
            os.makedirs(output_dir)
        
        metadata = results.get("metadata", {})
        summary = results.get("summary", {})
        closed_orders = results.get("closed_orders", [])
        trades = results.get("trades", [])
        portfolios = metadata.get("portfolios", [])
        
        md_content = []
        
        # Title
        md_content.append(f"# Market Results Report\n")
        md_content.append(f"**Generated:** {metadata.get('fetched_at', 'N/A')}\n")
        
        # Metadata section
        md_content.append("## Metadata\n")
        md_content.append("| Field | Value |")
        md_content.append("|-------|-------|")
        md_content.append(f"| Actor ID | {metadata.get('actor_id', 'N/A')} |")
        md_content.append(f"| Actor Role | {metadata.get('actor_role', 'N/A')} |")
        md_content.append(f"| Organization | {metadata.get('organization', 'N/A')} |")
        md_content.append(f"| Organization ID | {metadata.get('organization_id', 'N/A')} |")
        md_content.append(f"| Market | {metadata.get('market_name', 'N/A')} |")
        md_content.append(f"| Market ID | {metadata.get('market_id', 'N/A')} |")
        md_content.append(f"| Period From | {metadata.get('period_from', 'N/A')} |")
        md_content.append(f"| Period To | {metadata.get('period_to', 'N/A')} |")
        md_content.append(f"| Period Mode | {metadata.get('period_mode', 'N/A')} |")
        if metadata.get('period_mode') == 'dynamic':
            md_content.append(f"| Hours Back | {metadata.get('hours_back', 'N/A')} |")
        md_content.append(f"| Granularity | {metadata.get('granularity_minutes', 15)} minutes |")
        md_content.append("")
        
        # Portfolios (FSP only)
        if portfolios:
            md_content.append("## Portfolios\n")
            for p in portfolios:
                md_content.append(f"### {p.get('name', 'N/A')}\n")
                md_content.append(f"- **ID:** {p.get('id', 'N/A')}")
                md_content.append(f"- **Description:** {p.get('description') or '-'}")
                
                # Include assets if available
                assets = p.get('assets', [])
                if assets:
                    md_content.append(f"- **Assets:** {len(assets)}")
                    md_content.append("")
                    md_content.append("| Asset ID | Name | MPID | Type |")
                    md_content.append("|----------|------|------|------|")
                    for asset in assets:
                        md_content.append(
                            f"| {asset.get('id', 'N/A')[:8]}... | "
                            f"{asset.get('name', 'N/A')} | "
                            f"{asset.get('mpid', 'N/A')} | "
                            f"{asset.get('assetType', 'N/A')} |"
                        )
                md_content.append("")
        
        # Summary section
        md_content.append("## Summary\n")
        md_content.append("| Metric | Value |")
        md_content.append("|--------|-------|")
        md_content.append(f"| Total Closed Orders | {metadata.get('total_closed_orders', 0)} |")
        md_content.append(f"| Total Trades | {metadata.get('total_trades', 0)} |")
        md_content.append(f"| Total Time Slots | {metadata.get('total_time_slots', 0)} |")
        md_content.append(f"| Total Buy Quantity | {format_mw(summary.get('total_buy_quantity_mw', 0))} MW |")
        md_content.append(f"| Total Sell Quantity | {format_mw(summary.get('total_sell_quantity_mw', 0))} MW |")
        md_content.append(f"| Matched Quantity | {format_mw(summary.get('total_matched_quantity_mw', 0))} MW |")
        md_content.append(f"| Filled Orders | {summary.get('filled_orders', 0)} |")
        md_content.append(f"| Partially Filled | {summary.get('partially_filled_orders', 0)} |")
        md_content.append(f"| Cancelled Orders | {summary.get('cancelled_orders', 0)} |")
        md_content.append(f"| Expired Orders | {summary.get('expired_orders', 0)} |")
        
        # Show expired breakdown if there are any expired orders
        expired_total = summary.get('expired_orders', 0)
        if expired_total > 0:
            expired_partial = summary.get('expired_with_partial_fill', 0)
            expired_no_fill = summary.get('expired_with_no_fill', 0)
            md_content.append(f"| ↳ with partial fill | {expired_partial} |")
            md_content.append(f"| ↳ with no fill | {expired_no_fill} |")
        
        md_content.append("")
        
        # Closed Orders section
        if closed_orders:
            md_content.append("## Closed Orders\n")
            md_content.append("| Period | Side | Quantity | Price | Status | Buyer | Seller | Portfolio |")
            md_content.append("|--------|------|----------|-------|--------|-------|--------|-----------|")
            for order in closed_orders:
                period = order.get('periodFrom', 'N/A')
                if period and '+' in period:
                    period = period.split('+')[0] + 'Z'
                side = order.get('side', 'N/A')
                qty = order.get('quantity', 0)
                # Use quantityCompleted if quantity is 0 (filled orders)
                if qty == 0:
                    qty = order.get('quantityCompleted', 0)
                price = order.get('unitPrice', 0)
                status = order.get('completionType', 'N/A')
                
                buyer = order.get('buyer')
                buyer_str = buyer.get('organizationName', 'N/A') if buyer else '-'
                
                seller = order.get('seller')
                seller_str = seller.get('organizationName', 'N/A') if seller else '-'
                
                portfolio = order.get('portfolio')
                portfolio_str = portfolio.get('name', 'N/A') if portfolio else '-'
                
                md_content.append(f"| {period} | {side} | {qty} MW | {price} | {status} | {buyer_str} | {seller_str} | {portfolio_str} |")
            md_content.append("")
        
        # Trades section
        if trades:
            md_content.append("## Trades\n")
            actor_role = metadata.get('actor_role', '')
            
            if actor_role == 'dso':
                # DSO view: show deal ID (to match with FSP trades)
                md_content.append("| Period | Side | Quantity | Price | Organization | Deal ID |")
                md_content.append("|--------|------|----------|-------|--------------|---------|")
            else:
                # FSP view: show portfolio and organization info
                md_content.append("| Period | Side | Quantity | Price | Organization | Portfolio | Deal ID |")
                md_content.append("|--------|------|----------|-------|--------------|-----------|---------|")
            
            for trade in trades:
                period = trade.get('periodFrom', 'N/A')
                if period and '+' in period:
                    period = period.split('+')[0] + 'Z'
                side = trade.get('side', 'N/A')
                qty = trade.get('quantity', 0)
                price = trade.get('unitPrice', 0)
                portfolio_name = trade.get('portfolioName', '-')
                org_name = trade.get('ownerOrganizationName', '-')
                deal_id = trade.get('dealId', '-')
                # Truncate deal ID for readability
                deal_id_short = deal_id[:8] + '...' if deal_id and len(deal_id) > 8 else deal_id
                
                if actor_role == 'dso':
                    md_content.append(f"| {period} | {side} | {qty} MW | {price} | {org_name} | {deal_id_short} |")
                else:
                    md_content.append(f"| {period} | {side} | {qty} MW | {price} | {org_name} | {portfolio_name} | {deal_id_short} |")
            
            md_content.append("")
            md_content.append("> **Note:** To see counterparty information, run the script for both DSO and FSP,")
            md_content.append("> then match trades by Deal ID.")
            md_content.append("")
        
        # Footer
        md_content.append("---")
        md_content.append(f"*Report generated by market_results_fetcher.py*")
        
        # Write to file
        with open(output_file, "w", encoding="utf-8") as f:
            f.write("\n".join(md_content))
        
        logger.info(f"Markdown report saved to {output_file}")
    except Exception as e:
        logger.error(f"Failed to save markdown to {output_file}: {str(e)}")


def parse_datetime(dt_str: str) -> datetime:
    """
    Parse a datetime string in various formats.
    
    :param dt_str: Datetime string
    :return: Parsed datetime object
    """
    formats = [
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ]
    
    for fmt in formats:
        try:
            return datetime.strptime(dt_str, fmt)
        except ValueError:
            continue
    
    raise ValueError(f"Unable to parse datetime: {dt_str}")


def main():
    # --------------------------------------------------------------------------- #
    # Argument parsing
    # --------------------------------------------------------------------------- #
    arg_parser = argparse.ArgumentParser(
        description="Fetch closed market results from NODES API",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Fetch results from last 2 hours (single run)
  python market_results_fetcher.py --config_file ../conf/test_fm01_aem.json --player dso --hours_back 2

  # Fetch results periodically every 15 minutes
  python market_results_fetcher.py --config_file ../conf/test_fm01_aem.json --player dso --hours_back 2 --interval 900

  # Fetch results for a specific static period
  python market_results_fetcher.py --config_file ../conf/test_fm01_aem.json --player dso --period_mode static --period_from "2025-12-09T08:00:00Z" --period_to "2025-12-09T10:00:00Z"
        """
    )
    
    # Required arguments
    arg_parser.add_argument(
        "--config_file",
        required=True,
        help="Path to configuration file"
    )
    arg_parser.add_argument(
        "--player",
        required=True,
        choices=["dso", "fsp"],
        help="Player type to use for authentication (dso or fsp)"
    )
    
    # Period configuration
    arg_parser.add_argument(
        "--period_mode",
        default="dynamic",
        choices=["dynamic", "static"],
        help="Period mode: 'dynamic' (last N hours) or 'static' (fixed time range)"
    )
    arg_parser.add_argument(
        "--hours_back",
        type=float,
        default=2.0,
        help="Number of hours to look back (for dynamic mode, default: 2)"
    )
    arg_parser.add_argument(
        "--period_from",
        help="Start of period in ISO format (for static mode)"
    )
    arg_parser.add_argument(
        "--period_to",
        help="End of period in ISO format (for static mode)"
    )
    
    # Optional arguments
    arg_parser.add_argument(
        "--fsp_id",
        help="FSP identifier (required if player is 'fsp')"
    )
    arg_parser.add_argument(
        "--market_name",
        help="Filter by market name (optional)"
    )
    arg_parser.add_argument(
        "--include_settlements",
        action="store_true",
        default=False,
        help="Include settlements data (disabled by default as endpoint may not be available)"
    )
    arg_parser.add_argument(
        "--include_assets",
        action="store_true",
        default=False,
        help="Include asset details for each portfolio (FSP only)"
    )
    arg_parser.add_argument(
        "--interval",
        type=int,
        default=0,
        help="Fetch interval in seconds (0 for single run, default: 0)"
    )
    # Use script directory for relative default path
    script_dir = os.path.dirname(os.path.abspath(__file__))
    default_output_dir = os.path.normpath(
        os.path.join(script_dir, "..", "data", "market_results")
    )
    
    arg_parser.add_argument(
        "--output_dir",
        default=default_output_dir,
        help=f"Directory for output files (default: {default_output_dir})"
    )
    arg_parser.add_argument(
        "--output_file",
        default=None,
        help="Override output filename (without extension). If not set, auto-generated with timestamp."
    )
    arg_parser.add_argument(
        "--log_file",
        help="Log file path (optional, logs to stdout if not specified)"
    )
    
    args = arg_parser.parse_args()

    # --------------------------------------------------------------------------- #
    # Validate arguments
    # --------------------------------------------------------------------------- #
    if args.player == "fsp" and not args.fsp_id:
        print("ERROR: --fsp_id is required when player is 'fsp'")
        sys.exit(1)
    
    if args.period_mode == "static":
        if not args.period_from or not args.period_to:
            print("ERROR: --period_from and --period_to are required for static mode")
            sys.exit(1)

    # --------------------------------------------------------------------------- #
    # Load configuration
    # --------------------------------------------------------------------------- #
    config_file = args.config_file
    if not os.path.isfile(config_file):
        print(f"\nERROR: Unable to open configuration file {config_file}\n")
        sys.exit(1)

    cfg = json.loads(open(config_file).read())
    
    # Resolve the connections file path relative to the config file
    config_dir = os.path.dirname(os.path.abspath(config_file))
    connections_file = os.path.normpath(
        os.path.join(config_dir, cfg["connectionsFile"])
    )
    cfg_conns = json.loads(open(connections_file).read())
    cfg.update(cfg_conns)
    
    # Also fix token files folder path if relative
    if not os.path.isabs(cfg["nodesAPI"]["tokenFilesFolder"]):
        cfg["nodesAPI"]["tokenFilesFolder"] = os.path.normpath(
            os.path.join(config_dir, cfg["nodesAPI"]["tokenFilesFolder"])
        )

    # --------------------------------------------------------------------------- #
    # Setup logging
    # --------------------------------------------------------------------------- #
    logger = logging.getLogger()
    logging.basicConfig(
        format="%(asctime)-15s::%(levelname)s::%(funcName)s::%(message)s",
        level=logging.INFO,
        filename=args.log_file if args.log_file else None,
    )

    logger.info("Starting Market Results Fetcher")

    # --------------------------------------------------------------------------- #
    # Get player configuration
    # --------------------------------------------------------------------------- #
    if args.player == "dso":
        player_cfg = cfg["fm"]["actors"]["dso"]
    else:
        player_cfg = cfg["fm"]["actors"]["fsps"][args.fsp_id]

    # --------------------------------------------------------------------------- #
    # Initialize fetcher
    # --------------------------------------------------------------------------- #
    try:
        fetcher = MarketResultsFetcher(cfg, player_cfg, logger)
        logger.info(
            f"Fetcher initialized for organization: "
            f"{fetcher.organization.get('name') if fetcher.organization else 'Unknown'}"
        )
        
        # Load portfolios for FSP (with optional assets)
        fetcher.load_portfolios(include_assets=args.include_assets)
        
    except Exception as e:
        logger.error(f"Failed to initialize fetcher: {str(e)}")
        sys.exit(2)

    # --------------------------------------------------------------------------- #
    # Determine market name filter
    # --------------------------------------------------------------------------- #
    market_name = args.market_name or cfg["fm"].get("marketName")

    # --------------------------------------------------------------------------- #
    # Main loop
    # --------------------------------------------------------------------------- #
    def fetch_and_save():
        """Perform a single fetch and save operation."""
        # Determine period
        if args.period_mode == "dynamic":
            period_to = datetime.now(timezone.utc).replace(tzinfo=None)
            period_from = period_to - timedelta(hours=args.hours_back)
        else:
            period_from = parse_datetime(args.period_from)
            period_to = parse_datetime(args.period_to)
        
        logger.info(
            f"Fetching market results from {period_from.strftime('%Y-%m-%dT%H:%M:%SZ')} "
            f"to {period_to.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        )
        
        # Fetch results
        results = fetcher.get_market_results(
            period_from, period_to, market_name, args.include_settlements
        )
        
        # Add run configuration to metadata
        results["metadata"]["period_mode"] = args.period_mode
        if args.period_mode == "dynamic":
            results["metadata"]["hours_back"] = args.hours_back
        
        # Generate output filename
        if args.output_file:
            # Use provided filename (add to output_dir)
            base_filename = os.path.join(args.output_dir, args.output_file)
        else:
            # Auto-generate descriptive filename
            actor_id = args.fsp_id if args.player == "fsp" else player_cfg.get("id", "unknown")
            base_filename = generate_output_filename(
                output_dir=args.output_dir,
                actor_role=args.player,
                actor_id=actor_id,
                period_mode=args.period_mode,
                hours_back=args.hours_back if args.period_mode == "dynamic" else None,
                period_from=args.period_from if args.period_mode == "static" else None,
                period_to=args.period_to if args.period_mode == "static" else None,
            )
        
        json_file = f"{base_filename}.json"
        md_file = f"{base_filename}.md"
        
        # Save results as JSON
        save_results_to_json(results, json_file, logger)
        
        # Save results as Markdown
        save_results_to_markdown(results, md_file, logger)
        
        # Log summary
        summary = results.get("summary", {})
        logger.info(
            f"Summary - Matched: {summary.get('total_matched_quantity_mw', '0.000')} MW, "
            f"Filled: {summary.get('filled_orders', 0)}, "
            f"Partial: {summary.get('partially_filled_orders', 0)}, "
            f"Cancelled: {summary.get('cancelled_orders', 0)}, "
            f"Expired: {summary.get('expired_orders', 0)}"
        )

    # Execute
    if args.interval > 0:
        logger.info(f"Running in periodic mode with interval: {args.interval} seconds")
        while True:
            try:
                fetch_and_save()
            except Exception as e:
                logger.error(f"Error during fetch: {str(e)}")
            
            logger.info(f"Sleeping for {args.interval} seconds...")
            time.sleep(args.interval)
    else:
        logger.info("Running in single-run mode")
        fetch_and_save()

    logger.info("Market Results Fetcher completed")


if __name__ == "__main__":
    main()

