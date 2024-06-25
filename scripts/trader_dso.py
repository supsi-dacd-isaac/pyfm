# Importing section
import argparse
import logging
import os
import sys
import json

from classes.dso import DSO
from classes.fmo import FMO
from classes.postgresql_interface import PostgreSQLInterface


if __name__ == "__main__":
    # --------------------------------------------------------------------------- #
    # Configuration file
    # --------------------------------------------------------------------------- #
    arg_parser = argparse.ArgumentParser()
    arg_parser.add_argument('--config_file', help='configuration file')
    arg_parser.add_argument('--log_file', help='log file (optional, if empty log redirected on stdout)')
    args = arg_parser.parse_args()

    # Load the main parameters
    config_file = args.config_file
    if os.path.isfile(config_file) is False:
        print('\nATTENTION! Unable to open configuration file %s\n' % config_file)
        sys.exit(1)

    # Load configuration
    cfg = json.loads(open(config_file).read())
    cfg_conns = json.loads(open(cfg['connectionsFile']).read())
    cfg.update(cfg_conns)

    # Logger object
    if not args.log_file:
        log_file = None
    else:
        log_file = args.log_file
    logger = logging.getLogger()
    logging.basicConfig(format='%(asctime)-15s::%(levelname)s::%(funcName)s::%(message)s', level=logging.INFO,
                        filename=log_file)

    logger.info('Starting program')

    # Database connection
    pgi = PostgreSQLInterface(cfg['postgreSQL'], logger)

    # Market main parameters

    # DSO
    dso = DSO(cfg['fm']['actors']['dso'], cfg, logger)
    user_info = dso.nodes_interface.get_user_info()

    dso.set_markets(filter_dict={'name': cfg['fm']['marketName']})
    dso.set_organization(filter_dict={'name': dso.cfg['id']})
    dso.set_grid_area(filter_dict={'name': cfg['fm']['gridAreaName']})
    dso.set_grid_nodes(filter_dict={'gridAreaId': dso.grid_area['id']})

    slot_time = dso.get_adjusted_time(cfg['fm']['granularity'], cfg['fm']['ordersTimeShift'])

    dso.print_user_info(user_info)
    dso.print_player_info()

    # FMO object
    fmo = FMO({}, logger, pgi)

    # Place order
    resp_demand = dso.demand_flexibility(slot_time)
    if resp_demand is not False:
        logger.info('Requested flexibility: %.3f MW (type=%s)' % (resp_demand['quantity'], resp_demand['regulationType']))
        fmo.add_entry_to_ledger(timeslot=slot_time, player=dso, portfolio=None, features=resp_demand)

    logger.info('Ending program')
