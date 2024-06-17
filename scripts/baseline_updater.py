# Importing section
import argparse
import logging
import os
import sys
import json
import datetime
import pandas as pd
import copy

from datetime import datetime, timedelta
from influxdb import DataFrameClient

from classes.fsp import FSP


def create_df_baseline(portfolio, bs_cfg, ifc, db_cfg):
    current_time = datetime.utcnow()
    adjusted_time = (current_time.replace(minute=(current_time.minute // 15) * 15, second=0, microsecond=0) +
                     timedelta(minutes=bs_cfg['shiftMinutes']))

    if bs_cfg['case'] == 'from_file':
        return create_df_baseline_from_file(portfolio, adjusted_time, bs_cfg['fileSettings'])
    else:
        return create_df_baseline_from_db(portfolio, adjusted_time, ifc, db_cfg, bs_cfg['dbSettings'])


def create_df_baseline_from_db(portfolio, adjusted_time, ifc, db_cfg, bs_cfg):
    start_dt = adjusted_time - timedelta(days=bs_cfg['daysToGoBack'])
    start_dt_str = start_dt.strftime('%Y-%m-%dT%H:%M:%SZ')
    end_dt_str = (start_dt+timedelta(hours=bs_cfg['upcomingHoursToQuery'])).strftime('%Y-%m-%dT%H:%M:%SZ')
    str_mpids = '('
    for mpid in portfolio.get_assets_mpids():
        str_mpids = '%s OR site_name=\'%s\'' % (str_mpids, mpid)
    str_mpids = '%s)' % str_mpids.replace('( OR ', '(')

    query = ("SELECT sum(import) AS portfolio_cons, sum(export) AS portfolio_exp from %s "
             "WHERE time>='%s' AND time<'%s' AND %s GROUP BY time(%s)") % (db_cfg['measurement'],
                                                                           start_dt_str,
                                                                           end_dt_str,
                                                                           str_mpids,
                                                                           bs_cfg['timeGrouping'])
    logger.info('Query: %s' % query)
    try:
        res = ifc.query(query)
        df_data = res[db_cfg['measurement']]
        df_data_bs = copy.deepcopy(df_data)
        df_data_bs.index = df_data_bs.index + pd.DateOffset(days=bs_cfg['daysToGoBack'])

        # Handle columns and indexes
        df_data_bs['periodFrom'] = df_data_bs.index
        df_data_bs['periodTo'] = df_data_bs['periodFrom'] + pd.Timedelta(minutes=int(bs_cfg['timeGrouping'][0:-1]))
        df_data_bs.insert(loc=0, column='assetPortfolioId', value=portfolio.id)
        df_data_bs.insert(loc=1, column='quantityType', value='Power')
        df_data_bs.rename(columns={'portfolio_cons': 'quantity'}, inplace=True)
        df_data_bs['quantity'] = df_data_bs['quantity'] / 1e3
        df_data_bs.reset_index(drop=True, inplace=True)

        df_data_bs = df_data_bs[['assetPortfolioId', 'periodFrom', 'periodTo', 'quantity', 'quantityType']]
        return df_data_bs
    except Exception as e:
        logger.error('EXCEPTION: %s' & str(e))
        return None


def create_df_baseline_from_file(portfolio, adjusted_time, bs_file_cfg):
    df = pd.read_csv(bs_file_cfg['profileFile'])

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
    df.insert(loc=0, column='assetPortfolioId', value=portfolio.id)

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
    if not args.log_file:
        log_file = None
    else:
        log_file = args.log_file
    logger = logging.getLogger()
    logging.basicConfig(format='%(asctime)-15s::%(levelname)s::%(funcName)s::%(message)s', level=logging.INFO,
                        filename=log_file)

    # --------------------------------------------------------------------------- #
    # InfluxDB connection
    # --------------------------------------------------------------------------- #
    logger.info('Connection to InfluxDb server on socket [%s:%s]' % (cfg['influxDB']['host'], cfg['influxDB']['port']))
    try:
        influx_client = DataFrameClient(host=cfg['influxDB']['host'], port=cfg['influxDB']['port'],
                                        password=cfg['influxDB']['password'], username=cfg['influxDB']['user'],
                                        database=cfg['influxDB']['database'], ssl=cfg['influxDB']['ssl'])
    except Exception as e:
        logger.error('EXCEPTION: %s' % str(e))
        sys.exit(3)
    logger.info('Connection successful')


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
    for k_p in fsp.portfolios.keys():
        df = create_df_baseline(fsp.portfolios[k_p], cfg['baseline'], influx_client, cfg['influxDB'])
        fsp.update_baselines(k_p, df)

    logger.info('Ending program')
