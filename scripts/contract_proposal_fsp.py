# Importing section
import argparse
import logging
import os
import sys
import json
from datetime import timedelta

from classes.fsp import FSP
from classes.postgresql_interface import PostgreSQLInterface


if __name__ == "__main__":
    # --------------------------------------------------------------------------- #
    # Configuration file
    # --------------------------------------------------------------------------- #
    arg_parser = argparse.ArgumentParser()
    arg_parser.add_argument('--config_file', help='configuration file')
    arg_parser.add_argument('--fsp', help='FSP identifier')
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

    # FSP identifier
    fsp_identifier = args.fsp

    logger.info('Starting program')

    # Database connection
    pgi = PostgreSQLInterface(cfg['postgreSQL'], logger)

    # FSP
    fsp = FSP(cfg['fm']['actors']['fsps'][fsp_identifier], cfg, logger)
    slot_time = fsp.get_adjusted_time(cfg['fm']['granularity'], cfg['fm']['ordersTimeShift'])
    user_info = fsp.nodes_interface.get_user_info()

    fsp.set_markets(filter_dict={'name': cfg['fm']['marketName']})
    fsp.set_organization(filter_dict={'name': fsp.cfg['id']})

    logger.info('market id: %s' % fsp.markets[0]['id'])
    logger.info('market name: %s' % fsp.markets[0]['name'])

    # Read the available contracts requests for the given period
    contracts_requests = fsp.get_contracts(filter_dict={
                                                        'marketId': fsp.markets[0]['id'],
                                                        'periodFrom': (slot_time + timedelta(days=7)).strftime('%Y-%m-%dT00:00:00Z'),
                                                        'periodTo': (slot_time + timedelta(days=14)).strftime('%Y-%m-%dT00:00:00Z')
                                                       })

    for contract_request in contracts_requests:
        if 'request' in contract_request['name'] and contract_request['baseContractId'] is None:
            fsp.propose_contract(slot_time, contract_request)

    logger.info('Ending program')
