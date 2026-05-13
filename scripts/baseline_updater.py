# Importing section
import argparse
import logging
import os
import sys
import json

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from classes.fsp import FSP

if __name__ == "__main__":
    # --------------------------------------------------------------------------- #
    # Configuration file
    # --------------------------------------------------------------------------- #
    arg_parser = argparse.ArgumentParser()
    arg_parser.add_argument("--config_file", help="configuration file")
    arg_parser.add_argument("--fsp", help="FSP identifier")
    arg_parser.add_argument(
        "--log_file", help="log file (optional, if empty log redirected on stdout)"
    )
    arg_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build and log baseline data without uploading to NODES or saving to InfluxDB",
    )
    args = arg_parser.parse_args()

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

    if args.dry_run:
        logger.info("Starting program (DRY-RUN MODE - no baseline upload or InfluxDB save)")
    else:
        logger.info("Starting program")

    # Main features
    fsp = FSP(cfg["fm"]["actors"]["fsps"][fsp_identifier], cfg, logger)
    user_info = fsp.nodes_interface.get_user_info()

    fsp.set_markets(filter_dict={"name": cfg["fm"]["marketName"]})
    fsp.set_organization(filter_dict={"name": fsp.cfg["id"]})

    fsp.print_user_info(user_info)
    fsp.print_player_info()

    # Update baselines
    if fsp.update_baselines(cfg["baseline"], dry_run=args.dry_run) is False:
        logger.error("Baseline update failed")
        sys.exit(2)

    if args.dry_run:
        logger.info("Ending program (DRY-RUN)")
    else:
        logger.info("Ending program")
