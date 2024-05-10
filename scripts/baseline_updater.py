# Importing section
import argparse
import logging
import os
import sys
import json
import datetime
from datetime import datetime, timedelta
import pandas as pd

from classes.fsp import FSP


def create_dataframe_for_portfolio_baseline(p_id, data_file_path,bs_shift):
    current_time = datetime.utcnow()
    adjusted_time = (current_time.replace(minute=(current_time.minute // 15) * 15, second=0, microsecond=0) +
                     timedelta(minutes=bs_shift))

    df = pd.read_csv(data_file_path)

    df['slot_dt'] = pd.to_datetime(df['slot'], format='%H:%M')
    df['minutes_in_day'] = df['slot_dt'].dt.hour * 60 + df['slot_dt'].dt.minute
    current_daily_minutes = adjusted_time.hour * 60 + adjusted_time.minute

    df_today = df[df['minutes_in_day'] >= current_daily_minutes]
    df_today = df_today.copy()
    df_today.loc[:, 'periodFrom'] = pd.to_datetime(adjusted_time.strftime('%Y-%m-%d ') + df_today['slot'])

    df_tomorrow = df[df['minutes_in_day'] < current_daily_minutes]
    df_tomorrow = df_tomorrow.copy()
    df_tomorrow.loc[:, 'periodFrom'] = pd.to_datetime((adjusted_time+timedelta(days=1)).strftime('%Y-%m-%d ') + df_tomorrow['slot'])

    df = pd.concat([df_today, df_tomorrow], ignore_index=True)
    df['periodTo'] = df['periodFrom'] + pd.Timedelta(minutes=15)
    df.insert(loc=0, column='assetPortfolioId', value=p_id)

    # assetPortfolioId, periodFrom, periodTo, quantity, quantityType
    df = df[['assetPortfolioId', 'periodFrom', 'periodTo', 'quantity', 'quantityType']]

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

    # Logger object
    if not args.l:
        log_file = None
    else:
        log_file = args.log_file
    logger = logging.getLogger()
    logging.basicConfig(format='%(asctime)-15s::%(levelname)s::%(funcName)s::%(message)s', level=logging.INFO,
                        filename=log_file)

    # FSP identifier
    fsp_identifier = args.fsp

    logger.info('Starting program')

    # Main features
    fsp = FSP(cfg['fm']['actors']['fsps'][fsp_identifier], cfg['nodesAPI'], logger)
    user_info = fsp.nodes_interface.get_user_info()

    fsp.set_markets(filter_dict={'name': cfg['fm']['marketName']})
    fsp.set_organization(filter_dict={'name': fsp.cfg['id']})

    fsp.print_user_info(user_info)
    fsp.print_player_info()

    # Update baselines
    for p in fsp.portfolios:
        df = create_dataframe_for_portfolio_baseline(p['id'], cfg['utils']['baselineProfileFile'],
                                                     cfg['utils']['baselineShiftMinutes'])
        fsp.update_baselines(p['id'], df)

    logger.info('Ending program')
