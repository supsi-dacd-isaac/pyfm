# Importing section
import argparse
import logging
import os
import sys
import json
import datetime
from datetime import datetime, timedelta
from typing import Tuple
import pandas as pd
from influxdb import InfluxDBClient

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from classes.fsp import FSP
from classes.player import Player
from classes.fmo import FMO
from classes.postgresql_interface import PostgreSQLInterface
from classes.flexibility_forecaster import FlexibilityForecaster
from classes.bidding_strategy import BiddingStrategy, StrategyManager
from classes.bid_record_repository import BidRecordRepository
from classes.demand_record_repository import DemandRecordRepository


def create_dataframe_for_portfolio_baseline(p_id, data_file_path):
    current_time = datetime.utcnow()
    adjusted_time = current_time.replace(
        minute=(current_time.minute // 15) * 15, second=0, microsecond=0
    ) + timedelta(minutes=30)
    time_step = timedelta(minutes=15)

    df = pd.read_csv(data_file_path)
    df.insert(loc=0, column="assetPortfolioId", value=p_id)

    period_from = [adjusted_time + i * time_step for i in range(len(df))]
    period_from_iso = [dt.strftime("%Y-%m-%dT%H:%M:%SZ") for dt in period_from]
    df.insert(loc=1, column="periodFrom", value=period_from_iso)

    period_to = period_from[1:]
    period_to.append(period_to[-1] + timedelta(minutes=15))
    period_to_iso = [dt.strftime("%Y-%m-%dT%H:%M:%SZ") for dt in period_to]
    df.insert(loc=2, column="periodTo", value=period_to_iso)
    return df


def get_strategy_flexibility(
    strategy: BiddingStrategy, 
    slot_time: datetime, 
    asset_breakdown: dict,
    logger: logging.Logger
) -> float:
    """
    Calculate available flexibility based on strategy and asset data.
    
    NOTE: This is the legacy continuous-sum method. Use get_strategy_flexibility_discrete()
    for discretization-aware bidding.
    
    :param strategy: BiddingStrategy instance
    :param slot_time: Time slot for the bid
    :param asset_breakdown: Per-asset flexibility breakdown
    :param logger: Logger instance
    :return: Available flexibility in MW
    """
    # Get strategy parameters for this time slot
    params = strategy.get_bid_parameters(slot_time)
    
    # Calculate actual available flexibility from allowed assets
    actual_flex_kw = 0
    for asset_id, info in asset_breakdown.items():
        if strategy.is_asset_allowed(asset_id):
            actual_flex_kw += info["available_flexibility_kw"]
    
    actual_flex_mw = actual_flex_kw / 1000
    
    # Get strategy's configured flexibility
    strategy_flex_mw = params["flexibility_mw"]
    if params["ev_flexibility_mw"] > 0:
        strategy_flex_mw += params["ev_flexibility_mw"]
    
    # Use the minimum of strategy config and actual available
    final_flex_mw = min(strategy_flex_mw, actual_flex_mw)
    
    logger.info(
        "Strategy flexibility: config=%.4f MW, actual=%.4f MW, using=%.4f MW",
        strategy_flex_mw, actual_flex_mw, final_flex_mw
    )
    
    return final_flex_mw


def get_strategy_flexibility_discrete(
    strategy: BiddingStrategy,
    slot_time: datetime,
    flex_forecaster: FlexibilityForecaster,
    logger: logging.Logger
) -> Tuple[float, dict]:
    """
    Calculate achievable flexibility considering discrete asset constraints.
    
    This is the discretization-aware version that calculates what can actually
    be delivered, not just fractional sums.
    
    :param strategy: BiddingStrategy instance
    :param slot_time: Time slot for the bid
    :param flex_forecaster: FlexibilityForecaster instance
    :param logger: Logger instance
    :return: Tuple of (achievable flexibility in MW, allocation details)
    """
    # Get strategy parameters
    params = strategy.get_bid_parameters(slot_time)
    
    # Get strategy's target flexibility (as target for discrete calculation)
    target_flex_kw = params["flexibility_mw"] * 1000  # Convert to kW
    if params.get("ev_flexibility_mw", 0) > 0:
        target_flex_kw += params["ev_flexibility_mw"] * 1000
    
    # Get allowed assets from strategy
    allowed_assets = strategy.allowed_assets if hasattr(strategy, 'allowed_assets') else None
    
    # Get discretization-aware flexibility calculation
    achievable = flex_forecaster.get_achievable_flexibility(
        period_from=slot_time,
        target_kw=target_flex_kw,
        allowed_assets=allowed_assets,
        use_temperature=True
    )
    
    # Log detailed breakdown
    logger.info("-" * 70)
    logger.info("DISCRETIZATION-AWARE FLEXIBILITY ANALYSIS:")
    logger.info("  Target flexibility: %.3f kW (%.6f MW)", target_flex_kw, target_flex_kw / 1000)
    logger.info("  Discrete assets: %s", achievable["discrete_assets"])
    logger.info("  Continuous assets: %s", achievable["continuous_assets"])
    logger.info("  Achievable discrete levels (kW): %s", achievable["discrete_levels_kw"])
    logger.info("  Continuous range (kW): %.2f - %.2f", 
                achievable["continuous_range_kw"][0], 
                achievable["continuous_range_kw"][1])
    logger.info("  Total achievable range (kW): %.2f - %.2f",
                achievable["total_achievable_range_kw"][0],
                achievable["total_achievable_range_kw"][1])
    
    # Get recommended bid
    recommended_kw = achievable.get("recommended_bid_kw", 0)
    recommended_mw = recommended_kw / 1000
    
    deviation_kw = achievable.get("bid_deviation_kw", 0)
    deviation_pct = achievable.get("bid_deviation_pct", 0)
    
    if abs(deviation_kw) < 0.01:
        deviation_msg = "(exact match)"
    elif deviation_kw > 0:
        deviation_msg = f"(+{deviation_kw:.2f} kW / +{deviation_pct:.1f}% over target)"
    else:
        deviation_msg = f"({deviation_kw:.2f} kW / {deviation_pct:.1f}% under target)"
    
    logger.info("-" * 70)
    logger.info("RECOMMENDED BID: %.3f kW (%.6f MW) %s", 
                recommended_kw, recommended_mw, deviation_msg)
    
    allocation = achievable.get("recommended_allocation", {})
    if allocation:
        discrete_alloc = allocation.get("discrete", {})
        continuous_alloc = allocation.get("continuous", {})
        continuous_total = allocation.get("continuous_total_kw", 0)
        
        if discrete_alloc:
            logger.info("  Discrete allocation (ON/OFF):")
            for asset_id, power in discrete_alloc.items():
                state = "ON" if power > 0 else "OFF"
                logger.info("    - %s: %.1f kW (%s)", asset_id, power, state)
        
        if continuous_alloc:
            logger.info("  Continuous allocation (modulated): %.2f kW total", continuous_total)
            for asset_id, details in continuous_alloc.items():
                power = details.get("power_kw", 0)
                max_power = details.get("max_power_kw", 0)
                setpoint_pct = details.get("setpoint_pct", 0)
                logger.info("    - %s: %.2f kW (setpoint: %.1f%% of %.1f kW capacity)", 
                           asset_id, power, setpoint_pct, max_power)
    
    logger.info("-" * 70)
    
    return recommended_mw, achievable


def run_simple_mode(fsp, fmo, dso_demands, slot_time, total_available_flex_mw, dry_run, logger, 
                    bid_record_id=None, demand_record_id=None):
    """
    Run the original simple bidding mode (baseline-based).
    
    This is the legacy approach that uses:
    - Baseline-based quantity calculation
    - Constant pricing from FSP config
    """
    logger.info("=" * 70)
    logger.info("RUNNING IN SIMPLE MODE (baseline-based bidding)")
    logger.info("=" * 70)
    
    if dry_run:
        logger.info("DRY-RUN: Simulating order placement (no actual orders will be placed)")
    
    orders_summary = []
    
    for dso_demand in dso_demands:
        for p_k in fsp.portfolios.keys():
            # Log the decision for this portfolio
            baseline_value = fsp.baselines[p_k]["quantity"].loc[
                slot_time.strftime("%Y-%m-%dT%H:%M:%SZ")
            ] if slot_time.strftime("%Y-%m-%dT%H:%M:%SZ") in fsp.baselines[p_k]["quantity"].index else 0
            
            logger.info(
                "Portfolio %s: baseline=%.6f MW, forecasted_flex=%.6f MW",
                fsp.portfolios[p_k].metadata["name"],
                baseline_value,
                total_available_flex_mw
            )
            
            if dry_run:
                # Simulate what would be bid without actually placing orders
                for k_regulation_type in ["Up", "Down"]:
                    quantity_to_sell = fsp.calculate_quantity_to_sell_basic(
                        slot_time, dso_demand[k_regulation_type], fsp.baselines[p_k]["quantity"]
                    )
                    # Convert to native Python float to avoid numpy issues in DB
                    quantity_to_sell = float(quantity_to_sell) if quantity_to_sell else 0.0
                    
                    if quantity_to_sell > 0 and fsp.check_demand_price(
                        slot_time, dso_demand, quantity_to_sell
                    ):
                        order_info = {
                            "portfolio": fsp.portfolios[p_k].metadata["name"],
                            "regulation_type": k_regulation_type,
                            "quantity_mw": quantity_to_sell,
                            "unit_price": float(dso_demand["unitPrice"]),
                            "period_from": slot_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "period_to": (slot_time + timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        }
                        orders_summary.append(order_info)
                        logger.info(
                            "[DRY-RUN] WOULD PLACE ORDER: %s regulation, quantity=%.3f MW, price=%.2f CHF/MW",
                            k_regulation_type,
                            quantity_to_sell,
                            dso_demand["unitPrice"]
                        )
                    else:
                        if quantity_to_sell <= 0:
                            logger.info(
                                "[DRY-RUN] NO ORDER for %s: quantity=0 (no demand or no flexibility)",
                                k_regulation_type
                            )
            else:
                # Actually place orders
                resp_selling = fsp.sell_flexibility(slot_time, p_k, dso_demand)
                for k in resp_selling.keys():
                    if resp_selling[k] is not False:
                        order_info = {
                            "portfolio": fsp.portfolios[p_k].metadata["name"],
                            "regulation_type": k,
                            "quantity_mw": float(resp_selling[k]["quantity"]),
                            "unit_price": float(resp_selling[k]["unitPrice"]),
                            "period_from": resp_selling[k]["periodFrom"],
                            "period_to": resp_selling[k]["periodTo"],
                        }
                        orders_summary.append(order_info)
                        logger.info(
                            "ORDER PLACED: %s regulation, quantity=%.3f MW, price=%.2f CHF/MW",
                            k,
                            resp_selling[k]["quantity"],
                            resp_selling[k]["unitPrice"]
                        )
                        fmo.add_entry_to_market_ledger(
                            timeslot=slot_time,
                            player=fsp,
                            portfolio=fsp.portfolios[p_k].metadata["name"],
                            features=resp_selling[k],
                            bid_record_id=bid_record_id,
                            demand_record_id=demand_record_id,
                        )
    
    return orders_summary


def run_strategy_mode(strategy, strategy_id, fsp, fmo, dso_demands, slot_time, 
                      asset_breakdown, flex_forecaster, dry_run, logger, 
                      bid_record_id=None, demand_record_id=None):
    """
    Run strategy-based bidding mode with discretization-aware flexibility calculation.
    
    Uses the configured bidding strategy to determine:
    - Which assets to use
    - What price to bid
    - How much flexibility to offer (considering discrete asset constraints)
    """
    logger.info("=" * 70)
    logger.info("RUNNING IN STRATEGY MODE: %s - %s", strategy_id, strategy.name)
    logger.info("=" * 70)
    
    # Get strategy parameters for current time slot
    bid_params = strategy.get_bid_parameters(slot_time)
    logger.info("Time slot: %s", bid_params["slot_name"])
    logger.info("Strategy bid price: %.2f CHF/MW", bid_params["bid_price"])
    logger.info("Strategy flexibility target: %.4f MW", bid_params["flexibility_mw"])
    
    # Filter assets based on strategy and calculate flexibility
    total_available_flex_kw = 0
    strategy_flex_kw = 0
    logger.info("-" * 70)
    logger.info("Asset flexibility breakdown (strategy filter: %s):", strategy_id)
    
    for asset_id, info in asset_breakdown.items():
        is_allowed = strategy.is_asset_allowed(asset_id)
        status = "✓" if is_allowed else "✗"
        
        # Get modulation type for display
        mod_type = flex_forecaster._get_modulation_type(asset_id)
        mod_label = "[D]" if mod_type == "discrete" else "[C]"
        
        occupancy = info.get("occupancy_probability")
        if occupancy is not None:
            logger.info(
                "  [%s] %s %s (%s): typical=%.2f kW, occupancy=%.0f%%, available_flex=%.2f kW",
                status, mod_label, asset_id, info["description"],
                info["typical_load_kw"], occupancy * 100, info["available_flexibility_kw"],
            )
        else:
            logger.info(
                "  [%s] %s %s (%s): typical=%.2f kW, available_flex=%.2f kW",
                status, mod_label, asset_id, info["description"],
                info["typical_load_kw"], info["available_flexibility_kw"],
            )
        
        total_available_flex_kw += info["available_flexibility_kw"]
        if is_allowed:
            strategy_flex_kw += info["available_flexibility_kw"]
    
    logger.info("-" * 70)
    logger.info("TOTAL AVAILABLE (all assets, continuous sum): %.3f kW (%.6f MW)", 
                total_available_flex_kw, total_available_flex_kw / 1000)
    logger.info("STRATEGY AVAILABLE (filtered, continuous sum): %.3f kW (%.6f MW)", 
                strategy_flex_kw, strategy_flex_kw / 1000)
    
    # Get the flexibility to bid using discretization-aware calculation
    flexibility_to_bid_mw, achievable_details = get_strategy_flexibility_discrete(
        strategy, slot_time, flex_forecaster, logger
    )
    
    if dry_run:
        logger.info("-" * 70)
        logger.info("DRY-RUN: Simulating order placement (no actual orders will be placed)")
    
    orders_summary = []
    
    for dso_demand in dso_demands:
        dso_price = dso_demand.get("unitPrice", 0)
        
        # Check if DSO price is acceptable according to strategy
        price_acceptable = strategy.check_dso_price_acceptable(slot_time, dso_price)
        
        if not price_acceptable:
            logger.info(
                "DSO price %.2f CHF/MW is below strategy minimum %.2f CHF/MW - SKIPPING",
                dso_price, bid_params["bid_price"]
            )
            continue
        
        for p_k in fsp.portfolios.keys():
            baseline_value = fsp.baselines[p_k]["quantity"].loc[
                slot_time.strftime("%Y-%m-%dT%H:%M:%SZ")
            ] if slot_time.strftime("%Y-%m-%dT%H:%M:%SZ") in fsp.baselines[p_k]["quantity"].index else 0
            
            logger.info(
                "Portfolio %s: baseline=%.6f MW, strategy_flex=%.6f MW",
                fsp.portfolios[p_k].metadata["name"],
                baseline_value,
                flexibility_to_bid_mw
            )
            
            if dry_run:
                for k_regulation_type in ["Up", "Down"]:
                    if k_regulation_type == "Up":
                        quantity_to_sell = min(flexibility_to_bid_mw, dso_demand.get("Up", 0))
                    else:
                        quantity_to_sell = min(flexibility_to_bid_mw, dso_demand.get("Down", 0))
                    
                    # Round to 3 decimal places (NODES API requirement) and convert to native float
                    quantity_to_sell = float(round(quantity_to_sell, 3))
                    
                    if quantity_to_sell > 0:
                        order_info = {
                            "portfolio": fsp.portfolios[p_k].metadata["name"],
                            "regulation_type": k_regulation_type,
                            "quantity_mw": quantity_to_sell,
                            "unit_price": float(dso_price),
                            "strategy": strategy_id,
                            "time_slot": bid_params["slot_name"],
                            "period_from": slot_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "period_to": (slot_time + timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        }
                        orders_summary.append(order_info)
                        logger.info(
                            "[DRY-RUN] WOULD PLACE ORDER: %s regulation, quantity=%.3f MW, price=%.2f CHF/MW (strategy: %s)",
                            k_regulation_type, quantity_to_sell, dso_price, strategy_id
                        )
                    else:
                        logger.info(
                            "[DRY-RUN] NO ORDER for %s: quantity=0 (no demand or no flexibility)",
                            k_regulation_type
                        )
            else:
                for k_regulation_type in ["Up", "Down"]:
                    if k_regulation_type == "Up":
                        quantity_to_sell = min(flexibility_to_bid_mw, dso_demand.get("Up", 0))
                    else:
                        quantity_to_sell = min(flexibility_to_bid_mw, dso_demand.get("Down", 0))
                    
                    # Round to 3 decimal places (NODES API requirement) and convert to native float
                    quantity_to_sell = float(round(quantity_to_sell, 3))
                    
                    if quantity_to_sell > 0:
                        body = {
                            "ownerOrganizationId": fsp.organization["id"],
                            "periodFrom": slot_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "periodTo": (slot_time + timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "validTo": (slot_time + timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "marketId": fsp.markets[0]["id"],
                            "assetPortfolioId": p_k,
                            "regulationType": k_regulation_type,
                            "quantity": quantity_to_sell,
                            "unitPrice": float(dso_price),
                        }
                        body.update(fsp.cfg["orderSection"]["mainSettings"])
                        
                        response = fsp.nodes_interface.post_request(
                            "%s%s" % (fsp.nodes_interface.cfg["mainEndpoint"], "orders"), body
                        )
                        
                        if response is not False:
                            order_info = {
                                "portfolio": fsp.portfolios[p_k].metadata["name"],
                                "regulation_type": k_regulation_type,
                                "quantity_mw": quantity_to_sell,
                                "unit_price": float(dso_price),
                                "strategy": strategy_id,
                                "time_slot": bid_params["slot_name"],
                                "period_from": body["periodFrom"],
                                "period_to": body["periodTo"],
                            }
                            orders_summary.append(order_info)
                            logger.info(
                                "ORDER PLACED: %s regulation, quantity=%.3f MW, price=%.2f CHF/MW (strategy: %s)",
                                k_regulation_type, quantity_to_sell, dso_price, strategy_id
                            )
                            fmo.add_entry_to_market_ledger(
                                timeslot=slot_time,
                                player=fsp,
                                portfolio=fsp.portfolios[p_k].metadata["name"],
                                features=body,
                                bid_record_id=bid_record_id,
                                demand_record_id=demand_record_id,
                            )
                        else:
                            logger.error(
                                "FAILED to place order: %s regulation, quantity=%.3f MW",
                                k_regulation_type, quantity_to_sell
                            )
    
    return orders_summary, strategy_id


if __name__ == "__main__":
    # --------------------------------------------------------------------------- #
    # Configuration file
    # --------------------------------------------------------------------------- #
    arg_parser = argparse.ArgumentParser(
        description="FSP Trader - Estimate and bid flexibility on the market"
    )
    arg_parser.add_argument("--config_file", help="configuration file", required=True)
    arg_parser.add_argument("--fsp", help="FSP identifier", required=True)
    arg_parser.add_argument(
        "--strategy",
        help="Bidding strategy to use. Options: strategy_1, strategy_2, strategy_3, strategy_4, strategy_5. "
             "If not specified and FSP has no strategy configured, uses simple baseline-based bidding."
    )
    arg_parser.add_argument(
        "--list-strategies",
        action="store_true",
        help="List available strategies and exit"
    )
    arg_parser.add_argument(
        "--log_file", help="log file (optional, if empty log redirected on stdout)"
    )
    arg_parser.add_argument(
        "--dry-run", 
        action="store_true",
        help="Estimate flexibility without placing orders on the market"
    )
    args = arg_parser.parse_args()
    
    # Dry run mode
    dry_run = args.dry_run

    # Load the main parameters
    config_file = args.config_file
    if os.path.isfile(config_file) is False:
        print("\nATTENTION! Unable to open configuration file %s\n" % config_file)
        sys.exit(1)

    # Load configuration
    cfg = json.loads(open(config_file).read())

    # Logger object
    if not args.log_file:
        log_file = None
    else:
        log_file = args.log_file
    logger = logging.getLogger()
    logging.basicConfig(
        format="%(asctime)-15s::%(levelname)s::%(funcName)s::%(message)s",
        level=logging.INFO,
        filename=log_file,
    )

    # Initialize strategy manager (doesn't need connections)
    strategy_manager = StrategyManager(cfg, logger)
    
    # List strategies if requested (doesn't need connections)
    if args.list_strategies:
        strategy_manager.print_all_strategies()
        sys.exit(0)
    
    # Load connections file (needed for actual trading)
    try:
        cfg_conns = json.loads(open(cfg["connectionsFile"]).read())
        cfg.update(cfg_conns)
    except FileNotFoundError:
        print(f"\nERROR: Connections file not found: {cfg['connectionsFile']}")
        print("This file contains database credentials and API keys.")
        print("Please create it based on the template or contact your administrator.")
        sys.exit(1)

    # FSP identifier
    fsp_identifier = args.fsp
    
    # Check FSP exists
    if fsp_identifier not in cfg["fm"]["actors"]["fsps"]:
        print(f"\nERROR: FSP '{fsp_identifier}' not found in configuration")
        print(f"Available FSPs: {list(cfg['fm']['actors']['fsps'].keys())}")
        sys.exit(1)
    
    fsp_config = cfg["fm"]["actors"]["fsps"][fsp_identifier]
    
    # Determine which mode to use:
    # - Strategy mode: if --strategy is provided OR if FSP config has "strategy" field
    # - Simple mode: otherwise (original baseline-based approach)
    use_strategy_mode = False
    strategy = None
    strategy_id = None
    
    if args.strategy:
        # Command line strategy takes priority
        strategy_id = args.strategy
        strategy = strategy_manager.get_strategy(strategy_id)
        if strategy is None:
            print(f"\nERROR: Strategy '{strategy_id}' not found")
            print(f"Available strategies: {strategy_manager.list_strategies()}")
            sys.exit(1)
        use_strategy_mode = True
    elif "strategy" in fsp_config:
        # FSP config has strategy defined
        strategy_id = fsp_config["strategy"]
        strategy = strategy_manager.get_strategy(strategy_id)
        if strategy is None:
            print(f"\nERROR: Strategy '{strategy_id}' (from FSP config) not found")
            print(f"Available strategies: {strategy_manager.list_strategies()}")
            sys.exit(1)
        use_strategy_mode = True
    else:
        # No strategy specified - use simple mode
        use_strategy_mode = False

    if dry_run:
        logger.info("Starting program (DRY-RUN MODE - no orders will be placed)")
    else:
        logger.info("Starting program")
    
    logger.info("=" * 70)
    logger.info("FSP: %s", fsp_identifier)
    if use_strategy_mode:
        logger.info("Mode: STRATEGY-BASED")
        logger.info("Strategy: %s - %s", strategy_id, strategy.name)
        logger.info("Description: %s", strategy.description)
        logger.info("Allowed assets: %s", strategy.allowed_assets)
    else:
        logger.info("Mode: SIMPLE (baseline-based)")
        logger.info("Pricing: %s", fsp_config.get("pricing", {}).get("source", "constant"))
        logger.info("Min price: %.2f CHF/MW", fsp_config.get("pricing", {}).get("constant", 5.0))
    logger.info("=" * 70)

    # Database connection
    pgi = None
    bid_repo = None
    demand_repo = None
    try:
        pgi = PostgreSQLInterface(cfg["postgreSQL"], logger)
        # Initialize bid record repository for storing bid info
        bid_repo = BidRecordRepository(pgi, logger)
        logger.info("Bid record repository initialized")
        # Initialize demand record repository for storing DSO demand info
        demand_repo = DemandRecordRepository(pgi, logger)
        logger.info("Demand record repository initialized")
    except Exception as e:
        logger.error("Unable to connect to PostgreSQL: %s" % str(e))

    # InfluxDB connection for flexibility forecasting
    influx_client = InfluxDBClient(
        host=cfg["influxDB"]["host"],
        port=cfg["influxDB"]["port"],
        password=cfg["influxDB"]["password"],
        username=cfg["influxDB"]["user"],
        database=cfg["influxDB"]["database"],
        ssl=cfg["influxDB"]["ssl"],
    )

    # Actors definition
    # Slot time (static computation, no actor needed)
    slot_time = Player.get_adjusted_time(
        cfg["fm"]["granularity"], cfg["fm"]["ordersTimeShift"]
    )

    # FSP
    fsp = FSP(fsp_config, cfg, logger)
    user_info = fsp.nodes_interface.get_user_info()

    fsp.set_markets(filter_dict={"name": cfg["fm"]["marketName"]})
    fsp.set_organization(filter_dict={"name": fsp.cfg["id"]})

    logger.info("market id: %s" % fsp.markets[0]["id"])
    logger.info("market name: %s" % fsp.markets[0]["name"])

    # Resolve DSO organization ID via the FSP's own NODES token
    dso_cfg = cfg["fm"]["actors"]["dso"]
    dso_orgs = fsp.nodes_interface.get_request(
        "%s%s" % (fsp.nodes_interface.cfg["mainEndpoint"],
                   "organizations?name=%s" % dso_cfg["id"])
    )
    dso_org_id = None
    if "items" in dso_orgs and len(dso_orgs["items"]) == 1:
        dso_org_id = dso_orgs["items"][0]["id"]
    else:
        logger.error("Unable to resolve DSO organization '%s' via FSP token", dso_cfg["id"])

    # Get quantities demanded by the DSO (DSO runs 1 minute before FSP)
    dso_demands = []
    if dso_org_id:
        filter_dict = {
            "ownerOrganizationId": dso_org_id,
            "periodFrom": slot_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "periodTo": (slot_time + timedelta(minutes=cfg["fm"]["granularity"])).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "type": "Buy",
            "quantityType": "Power",
        }
        filter_str = "?" + "&".join("%s=%s" % (k, v) for k, v in filter_dict.items())
        res = fsp.nodes_interface.get_request(
            "%s%s" % (fsp.nodes_interface.cfg["mainEndpoint"], "orders%s" % filter_str)
        )
        orders = res.get("items", [])
        for order in orders:
            if order["completionType"] is None:
                request = {}
                if order["regulationType"] == "Down":
                    request["Down"] = float(order["quantity"])
                    request["Up"] = 0.0
                elif order["regulationType"] == "Up":
                    request["Up"] = float(order["quantity"])
                    request["Down"] = 0.0
                request["unitPrice"] = float(order["unitPrice"])
                dso_demands.append(request)
                logger.info(
                    "Flexibility demanded by the DSO (Power): Up = %.3f MW, Down = %.3f MW, Price = %.3f",
                    request.get("Up", 0), request.get("Down", 0), request["unitPrice"]
                )

    # Set current baselines for FSP
    fsp.download_baselines(slot_time)

    # Initialize FlexibilityForecaster for the 5-asset portfolio
    flex_forecaster = FlexibilityForecaster(cfg, influx_client, logger)
    
    # FMO object
    fmo = FMO(fsp.cfg, logger, pgi)

    # Get flexibility forecast for the current slot
    logger.info("=" * 70)
    logger.info("FLEXIBILITY ANALYSIS FOR SLOT: %s", slot_time.strftime("%Y-%m-%d %H:%M"))
    logger.info("=" * 70)
    
    # Check if this is a peak hour
    is_peak = flex_forecaster._is_peak_hour(slot_time)
    logger.info("Peak hour: %s", "YES" if is_peak else "NO")
    
    # Log temperature information if enabled
    if flex_forecaster.temperature_enabled:
        forecast_temp = flex_forecaster.get_forecast_temperature(slot_time)
        if forecast_temp is not None:
            logger.info("Temperature forecast: %.1f°C", forecast_temp)
            logger.info("Temperature-aware HP analysis: ENABLED")
        else:
            logger.info("Temperature forecast: N/A (using time-based estimates)")
    
    # Get per-asset flexibility breakdown for this slot
    asset_breakdown = flex_forecaster.get_asset_flexibility_breakdown(slot_time)
    
    total_available_flex_kw = 0
    logger.info("-" * 70)
    logger.info("Asset flexibility breakdown:")
    for asset_id, info in asset_breakdown.items():
        occupancy = info.get("occupancy_probability")
        estimation_method = info.get("estimation_method", "time_based")
        temp_adjusted_load = info.get("temperature_adjusted_load_kw")
        forecast_temp = info.get("forecast_temperature_c")
        
        if occupancy is not None:
            # EV charger with occupancy probability
            logger.info(
                "  %s (%s): typical=%.2f kW, occupancy=%.0f%%, available_flex=%.2f kW (factor=%.0f%%)",
                asset_id, info["description"],
                info["typical_load_kw"], occupancy * 100,
                info["available_flexibility_kw"], info["flexibility_factor"] * 100
            )
        elif temp_adjusted_load is not None:
            # Heat pump with temperature-adjusted estimate
            logger.info(
                "  %s (%s): time_based=%.2f kW, temp_adjusted=%.2f kW @ %.1f°C, available_flex=%.2f kW (method=%s)",
                asset_id, info["description"],
                info["typical_load_kw"], temp_adjusted_load, forecast_temp,
                info["available_flexibility_kw"], estimation_method
            )
        else:
            # Standard time-based estimate
            logger.info(
                "  %s (%s): typical=%.2f kW, available_flex=%.2f kW (factor=%.0f%%, method=%s)",
                asset_id, info["description"],
                info["typical_load_kw"], info["available_flexibility_kw"],
                info["flexibility_factor"] * 100, estimation_method
            )
        total_available_flex_kw += info["available_flexibility_kw"]
    
    total_available_flex_mw = total_available_flex_kw / 1000
    logger.info("-" * 70)
    logger.info("TOTAL AVAILABLE FLEXIBILITY: %.3f kW (%.6f MW)", 
                total_available_flex_kw, total_available_flex_mw)
    
    # Log DSO demands
    logger.info("-" * 70)
    logger.info("DSO DEMANDS:")
    total_dso_demand_up = 0
    total_dso_demand_down = 0
    for i, dso_demand in enumerate(dso_demands):
        logger.info(
            "  Demand %d: Up=%.3f MW, Down=%.3f MW, Price=%.2f CHF/MW",
            i + 1,
            dso_demand.get("Up", 0),
            dso_demand.get("Down", 0),
            dso_demand.get("unitPrice", 0)
        )
        total_dso_demand_up += dso_demand.get("Up", 0)
        total_dso_demand_down += dso_demand.get("Down", 0)
    
    logger.info("  TOTAL DSO DEMAND: Up=%.3f MW, Down=%.3f MW", 
                total_dso_demand_up, total_dso_demand_down)
    
    # Get price information for bid record
    dso_offered_price = dso_demands[0].get("unitPrice", 0) if dso_demands else 0
    
    # Get FSP's minimum acceptable price from strategy (if in strategy mode)
    fsp_min_price = None
    if use_strategy_mode and strategy:
        bid_params = strategy.get_bid_parameters(slot_time)
        fsp_min_price = bid_params.get("bid_price")
    
    # Compare available flexibility vs DSO demand
    logger.info("-" * 70)
    can_meet_demand = total_available_flex_mw >= total_dso_demand_up
    logger.info(
        "CAN MEET DSO DEMAND (Up): %s (available=%.3f MW, required=%.3f MW)",
        "YES" if can_meet_demand else "NO",
        total_available_flex_mw,
        total_dso_demand_up
    )
    logger.info("=" * 70)

    # Save demand record (DSO request) BEFORE placing orders
    demand_record_id = None
    if demand_repo and (total_dso_demand_up > 0 or total_dso_demand_down > 0):
        try:
            slot_end = slot_time + timedelta(minutes=cfg["fm"]["granularity"])
            
            # Build orders list from dso_demands
            demand_orders = []
            for dso_demand in dso_demands:
                if dso_demand.get("Up", 0) > 0:
                    demand_orders.append({
                        "regulation_type": "Up",
                        "quantity_mw": float(dso_demand.get("Up", 0)),
                        "unit_price": float(dso_demand.get("unitPrice", 0)),
                        "period_from": slot_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "period_to": slot_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "order_status": "observed"
                    })
                if dso_demand.get("Down", 0) > 0:
                    demand_orders.append({
                        "regulation_type": "Down",
                        "quantity_mw": float(dso_demand.get("Down", 0)),
                        "unit_price": float(dso_demand.get("unitPrice", 0)),
                        "period_from": slot_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "period_to": slot_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "order_status": "observed"
                    })
            
            demand_record_id = demand_repo.save_demand_record(
                dso_id=dso_cfg["id"],
                slot_start=slot_time,
                slot_end=slot_end,
                quantity_up_mw=float(total_dso_demand_up) if total_dso_demand_up > 0 else None,
                quantity_down_mw=float(total_dso_demand_down) if total_dso_demand_down > 0 else None,
                quantity_unit="MW",
                price_offered=float(dso_offered_price) if dso_offered_price else None,
                currency=fsp.cfg.get("orderSection", {}).get("mainSettings", {}).get("currency", "CHF"),
                regulation_type="Both" if total_dso_demand_up > 0 and total_dso_demand_down > 0 
                                else ("Up" if total_dso_demand_up > 0 else "Down"),
                request_source="trader_fsp_observation",
                request_reason="DSO demand observed by FSP before bidding",
                orders=demand_orders
            )
            logger.info("Demand record created with ID: %s (DSO: %s)", demand_record_id, dso_cfg["id"])
        except Exception as e:
            logger.error("Error saving demand record: %s", str(e))

    # Save bid record BEFORE placing orders (to get the ID for market_ledger linkage)
    bid_record_id = None
    if bid_repo:
        try:
            # Build assets_to_activate list
            # Convert numpy types to native Python types to avoid SQL issues
            assets_to_activate = []
            if use_strategy_mode and strategy:
                for asset_id, info in asset_breakdown.items():
                    if strategy.is_asset_allowed(asset_id):
                        assets_to_activate.append({
                            "asset_id": asset_id,
                            "description": info.get("description", asset_id),
                            "asset_type": info.get("asset_type", "unknown"),
                            "available_flexibility_kw": float(info.get("available_flexibility_kw", 0)),
                            "flexibility_factor": float(info.get("flexibility_factor", 0.5)),
                        })
            else:
                for asset_id, info in asset_breakdown.items():
                    assets_to_activate.append({
                        "asset_id": asset_id,
                        "description": info.get("description", asset_id),
                        "asset_type": info.get("asset_type", "unknown"),
                        "available_flexibility_kw": float(info.get("available_flexibility_kw", 0)),
                        "flexibility_factor": float(info.get("flexibility_factor", 0.5)),
                    })
            
            bid_record_id = bid_repo.save_bid_record(
                fsp_id=args.fsp,
                slot_start=slot_time,
                slot_end=slot_time + timedelta(minutes=15),
                orders=[],  # Will be updated after orders are placed
                strategy_id=strategy_id if use_strategy_mode else None,
                strategy_name=strategy.name if use_strategy_mode and strategy else None,
                strategy_description=strategy.description if use_strategy_mode and strategy else None,
                assets_to_activate=assets_to_activate,
                dso_offered_price=float(dso_offered_price) if dso_offered_price else None,
                fsp_min_price=float(fsp_min_price) if fsp_min_price else None,
                actual_price=None,  # Will be set after orders are placed
                currency=fsp.cfg.get("orderSection", {}).get("mainSettings", {}).get("currency", "CHF"),
            )
            logger.info("Bid record created with ID: %s (status: pending)", bid_record_id)
        except Exception as e:
            logger.warning("Could not create bid record: %s", str(e))

    # Run appropriate mode
    if use_strategy_mode:
        orders_summary, used_strategy = run_strategy_mode(
            strategy, strategy_id, fsp, fmo, dso_demands, slot_time,
            asset_breakdown, flex_forecaster, dry_run, logger, 
            bid_record_id=bid_record_id, demand_record_id=demand_record_id
        )
    else:
        orders_summary = run_simple_mode(
            fsp, fmo, dso_demands, slot_time, total_available_flex_mw, dry_run, logger, 
            bid_record_id=bid_record_id, demand_record_id=demand_record_id
        )
        used_strategy = None

    # Print summary
    logger.info("=" * 70)
    if dry_run:
        logger.info("DRY-RUN SUMMARY")
    else:
        logger.info("EXECUTION SUMMARY")
    logger.info("=" * 70)
    
    if use_strategy_mode:
        logger.info("Mode: Strategy-based (%s - %s)", strategy_id, strategy.name)
    else:
        logger.info("Mode: Simple (baseline-based)")
    
    if orders_summary:
        total_quantity = sum(o["quantity_mw"] for o in orders_summary)
        total_value = sum(o["quantity_mw"] * o["unit_price"] for o in orders_summary)
        logger.info("Total orders: %d", len(orders_summary))
        logger.info("Total quantity: %.4f MW", total_quantity)
        logger.info("Total potential revenue: %.2f CHF", total_value)
        for order in orders_summary:
            if "strategy" in order:
                logger.info(
                    "  - %s %s: %.4f MW @ %.2f CHF/MW = %.2f CHF (strategy: %s)",
                    order["portfolio"], order["regulation_type"],
                    order["quantity_mw"], order["unit_price"],
                    order["quantity_mw"] * order["unit_price"],
                    order["strategy"]
                )
            else:
                logger.info(
                    "  - %s %s: %.4f MW @ %.2f CHF/MW = %.2f CHF",
                    order["portfolio"], order["regulation_type"],
                    order["quantity_mw"], order["unit_price"],
                    order["quantity_mw"] * order["unit_price"]
                )
    else:
        logger.info("No orders %s", "would be placed" if dry_run else "placed")
    
    logger.info("=" * 70)
    
    # Update bid record with actual orders placed
    if bid_record_id and orders_summary and bid_repo:
        try:
            # Extract the actual transaction price from orders (use max price offered)
            actual_transaction_price = max(
                (o.get("unit_price", 0) for o in orders_summary), 
                default=dso_offered_price
            )
            
            # Update the bid record with actual orders
            bid_repo.save_bid_record(
                fsp_id=args.fsp,
                slot_start=slot_time,
                slot_end=slot_time + timedelta(minutes=15),
                orders=orders_summary,
                strategy_id=strategy_id if use_strategy_mode else None,
                strategy_name=strategy.name if use_strategy_mode and strategy else None,
                strategy_description=strategy.description if use_strategy_mode and strategy else None,
                assets_to_activate=None,  # Already set, won't be updated
                dso_offered_price=float(dso_offered_price) if dso_offered_price else None,
                fsp_min_price=float(fsp_min_price) if fsp_min_price else None,
                actual_price=float(actual_transaction_price) if actual_transaction_price else None,
                currency=fsp.cfg.get("orderSection", {}).get("mainSettings", {}).get("currency", "CHF"),
            )
            logger.info("Bid record ID %s updated with %d orders (DSO: %.2f, FSP min: %.2f, actual: %.2f CHF/MW)", 
                       bid_record_id, len(orders_summary), 
                       dso_offered_price or 0, fsp_min_price or 0, actual_transaction_price or 0)
        except Exception as e:
            logger.error("Failed to update bid record: %s", str(e))
    
    if dry_run:
        logger.info("Ending program (DRY-RUN - no changes made to market)")
    else:
        logger.info("Ending program")
