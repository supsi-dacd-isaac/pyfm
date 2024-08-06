# Importing section
import argparse
import logging
import os
import sys
import json
from datetime import timedelta

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

    # DSO
    dso = DSO(cfg['fm']['actors']['dso'], cfg, logger)
    user_info = dso.nodes_interface.get_user_info()

    dso.set_markets(filter_dict={'name': cfg['fm']['marketName']})
    slot_time = dso.get_adjusted_time(cfg['fm']['granularity'], cfg['fm']['ordersTimeShift'])

    dso.print_user_info(user_info)
    dso.print_player_info()

    # Read the available contracts requests for the given period
    contracts_requests = dso.get_contracts(filter_dict={
                                                        'marketId': dso.markets[0]['id'],
                                                        'periodFrom': (slot_time + timedelta(days=7)).strftime('%Y-%m-%dT00:00:00Z'),
                                                        'periodTo': (slot_time + timedelta(days=14)).strftime('%Y-%m-%dT00:00:00Z')
                                                       })

    fmo = FMO({}, logger, pgi)
    for contract_request in contracts_requests:
        if 'request' in contract_request['name'] and contract_request['baseContractId'] is None:
            # Get the id of the contract proposal
            contracts_proposals = dso.get_contracts(filter_dict={'baseContractId': contract_request['id']})

            # todo in this case I take the first proposal without any evaluation
            if len(contracts_proposals) > 0:
                dso.sign_contract(contracts_proposals[0], fmo)
            else:
                logger.warning('No available contract proposals available for request contract %s',
                               contract_request['id'])

    logger.info('Ending program')
