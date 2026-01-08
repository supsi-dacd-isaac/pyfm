# Importing section
import argparse
import logging
import os
import sys
import json
import datetime
from datetime import datetime, timedelta
import pandas as pd
from influxdb import InfluxDBClient

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from classes.dso import DSO
from classes.fsp import FSP
from classes.fmo import FMO
from classes.postgresql_interface import PostgreSQLInterface
from classes.flexibility_forecaster import FlexibilityForecaster


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
    cfg_conns = json.loads(open(cfg["connectionsFile"]).read())
    cfg.update(cfg_conns)

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

    # FSP identifier
    fsp_identifier = args.fsp

    if dry_run:
        logger.info("Starting program (DRY-RUN MODE - no orders will be placed)")
    else:
        logger.info("Starting program")

    # Database connection
    pgi = None
    try:
        pgi = PostgreSQLInterface(cfg["postgreSQL"], logger)
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
    # DSO
    dso = DSO(cfg["fm"]["actors"]["dso"], cfg, logger)
    dso.set_organization(filter_dict={"name": dso.cfg["id"]})
    slot_time = dso.get_adjusted_time(
        cfg["fm"]["granularity"], cfg["fm"]["ordersTimeShift"]
    )

    # FSP
    fsp = FSP(cfg["fm"]["actors"]["fsps"][fsp_identifier], cfg, logger)
    user_info = fsp.nodes_interface.get_user_info()

    fsp.set_markets(filter_dict={"name": cfg["fm"]["marketName"]})
    fsp.set_organization(filter_dict={"name": fsp.cfg["id"]})

    logger.info("market id: %s" % fsp.markets[0]["id"])
    logger.info("market name: %s" % fsp.markets[0]["name"])

    # Get quantities demanded by the DSO (DSO runs 1 minute before FSP)
    dso_demands = dso.get_flexibility_requests(
        slot_time, cfg["fm"]["granularity"], "Buy", "Power"
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
    
    # Get per-asset flexibility breakdown for this slot
    asset_breakdown = flex_forecaster.get_asset_flexibility_breakdown(slot_time)
    
    total_available_flex_kw = 0
    logger.info("-" * 70)
    logger.info("Asset flexibility breakdown:")
    for asset_id, info in asset_breakdown.items():
        occupancy = info.get("occupancy_probability")
        if occupancy is not None:
            # EV charger with occupancy
            logger.info(
                "  %s (%s): typical=%.2f kW, occupancy=%.0f%%, available_flex=%.2f kW (factor=%.0f%%)",
                asset_id,
                info["description"],
                info["typical_load_kw"],
                occupancy * 100,
                info["available_flexibility_kw"],
                info["flexibility_factor"] * 100
            )
        else:
            # Heat pump or other asset
            logger.info(
                "  %s (%s): typical=%.2f kW, available_flex=%.2f kW (factor=%.0f%%)",
                asset_id,
                info["description"],
                info["typical_load_kw"],
                info["available_flexibility_kw"],
                info["flexibility_factor"] * 100
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

    # Process orders (place or simulate in dry-run mode)
    if dry_run:
        logger.info("=" * 70)
        logger.info("DRY-RUN: Simulating order placement (no actual orders will be placed)")
        logger.info("=" * 70)
    
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
                    
                    if quantity_to_sell > 0 and fsp.check_demand_price(
                        slot_time, dso_demand, quantity_to_sell
                    ):
                        order_info = {
                            "portfolio": fsp.portfolios[p_k].metadata["name"],
                            "regulation_type": k_regulation_type,
                            "quantity_mw": quantity_to_sell,
                            "unit_price": dso_demand["unitPrice"],
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
                            "quantity_mw": resp_selling[k]["quantity"],
                            "unit_price": resp_selling[k]["unitPrice"],
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
                        )

    # Print summary
    logger.info("=" * 70)
    if dry_run:
        logger.info("DRY-RUN SUMMARY")
    else:
        logger.info("EXECUTION SUMMARY")
    logger.info("=" * 70)
    
    if orders_summary:
        total_quantity = sum(o["quantity_mw"] for o in orders_summary)
        logger.info("Total orders: %d", len(orders_summary))
        logger.info("Total quantity: %.3f MW", total_quantity)
        for order in orders_summary:
            logger.info(
                "  - %s %s: %.3f MW @ %.2f CHF/MW",
                order["portfolio"],
                order["regulation_type"],
                order["quantity_mw"],
                order["unit_price"]
            )
    else:
        logger.info("No orders %s", "would be placed" if dry_run else "placed")
    
    logger.info("=" * 70)
    
    if dry_run:
        logger.info("Ending program (DRY-RUN - no changes made to market)")
    else:
        logger.info("Ending program")
