# Importing section
import argparse
import logging
import os
import sys
import json
import datetime
from datetime import datetime, timedelta
import pandas as pd

from classes.dso import DSO
from classes.fsp import FSP

DSO_ID = 0
FSP01_ID = 1

if __name__ == "__main__":
    # --------------------------------------------------------------------------- #
    # Configuration file
    # --------------------------------------------------------------------------- #
    arg_parser = argparse.ArgumentParser()
    arg_parser.add_argument("--config_file", help="configuration file")
    arg_parser.add_argument("--hours_around_now", help="hours_around_now")
    arg_parser.add_argument("--log_file", help="log file (optional, if empty log redirected on stdout)")
    args = arg_parser.parse_args()

    # Load the main parameters
    config_file = args.config_file
    if os.path.isfile(config_file) is False:
        print('\nATTENTION! Unable to open configuration file %s\n' % config_file)
        sys.exit(1)
    hours_around_now = int(args.hours_around_now)

    # Load configuration
    cfg = json.loads(open(config_file).read())
    cfg_conns = json.loads(open(cfg['connectionsFile']).read())
    cfg.update(cfg_conns)

    logger = logging.getLogger()
    logging.basicConfig(format='%(asctime)-15s::%(levelname)s::%(funcName)s::%(message)s', level=logging.INFO,
                        filename=None)

    now = datetime.utcnow()
    from_str = (now - timedelta(hours=hours_around_now)).strftime('%Y-%m-%dT%H:%M:%SZ')
    to_str = (now + timedelta(hours=hours_around_now)).strftime('%Y-%m-%dT%H:%M:%SZ')

    dso = DSO(cfg['fm']['actors']['dso'], cfg['nodesAPI'], logger)
    dso.set_organization(filter_dict={'name': dso.cfg['id']})

    # Print orders performed by DSO
    dso_orders = dso.get_orders(filter_dict={'ownerOrganizationId': dso.organization['id'],
                                             'periodFrom.GreaterThanOrEqual': from_str,
                                             'periodFrom.LessThanOrEqual': to_str})
    logger.info('DSO ORDERS:')
    for dso_order in dso_orders:
        logger.info('Created: %s, ValidFrom: %s, ValidTo: %s, Side: %s, Type: %s, Quantity: %s, completionType: %s' %
                    (dso_order['created'], dso_order['validFrom'], dso_order['validTo'], dso_order['side'],
                     dso_order['regulationType'], dso_order['quantity'], dso_order['completionType']))

    # Print orders performed by FSPs
    for fsp_identifier in cfg['fm']['actors']['fsps'].keys():
        fsp = FSP(cfg['fm']['actors']['fsps'][fsp_identifier], cfg['nodesAPI'], logger)
        fsp.set_organization(filter_dict={'name': fsp.cfg['id']})

        fsp_orders = fsp.get_orders(filter_dict={'ownerOrganizationId': fsp.organization['id'],
                                                 'periodFrom.GreaterThanOrEqual': from_str,
                                                 'periodFrom.LessThanOrEqual': to_str})

        logger.info('FSP ORDERS:')
        for fsp_order in fsp_orders:
            logger.info('Created: %s, ValidFrom: %s, ValidTo: %s, Side: %s, Type: %s, Quantity: %s, completionType: %s' %
                        (fsp_order['created'], fsp_order['validFrom'], fsp_order['validTo'], fsp_order['side'],
                         fsp_order['regulationType'], fsp_order['quantity'], fsp_order['completionType']))


