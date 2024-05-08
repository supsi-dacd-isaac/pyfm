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
from classes.postgresql_interface import PostgreSQLInterface


def create_dataframe_for_portfolio_baseline(p_id, data_file_path):
    current_time = datetime.utcnow()
    adjusted_time = (current_time.replace(minute=(current_time.minute // 15) * 15, second=0, microsecond=0) +
                     timedelta(minutes=30))
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

    logger = logging.getLogger()
    logging.basicConfig(format='%(asctime)-15s::%(levelname)s::%(funcName)s::%(message)s', level=logging.INFO,
                        filename=None)

    # FSP identifier
    fsp_identifier = args.fsp

    logger.info('Starting program')

    # Database connection
    pgi = PostgreSQLInterface(cfg['postgreSQL'], logger)

    # Actors definition
    # DSO
    dso = DSO(cfg['fm']['actors']['dso'], cfg['nodesAPI'], logger)
    dso.set_organization(filter_dict={'name': dso.cfg['id']})
    slot_time = dso.get_adjusted_time(cfg['fm']['granularity'], cfg['fm']['ordersTimeShift'])

    # FSP
    fsp = FSP(cfg['fm']['actors']['fsps'][fsp_identifier], cfg['nodesAPI'], logger)
    user_info = fsp.nodes_interface.get_user_info()

    fsp.set_markets(filter_dict={'name': cfg['fm']['marketName']})
    fsp.set_organization(filter_dict={'name': fsp.cfg['id']})

    logger.info('market id: %s' % fsp.markets[0]['id'])
    logger.info('market name: %s' % fsp.markets[0]['name'])

    # Get quantities demanded by the DSO in the
    dso_demand = dso.get_flexibility_quantities(slot_time, cfg['fm']['granularity'], 'Buy', 'Power')

    # Get baselines
    fsp.set_baselines(slot_time)

    # Place orders
    for p in fsp.portfolios:
        fsp.sell_flexibility(slot_time, p['id'], dso_demand)
        # fmo.add_entry_to_ledger(timeslot=ts_slot, player=fsp, portfolio=fsp.portfolios['portfolio_hps'],
        #                         case='sell', data={'amount': 10, 'unit': 'kW', 'price': 1})

    logger.info('Ending program')
