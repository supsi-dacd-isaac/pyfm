# import section
import os
import copy
import pandas as pd
from datetime import datetime, timedelta

from classes.asset import Asset
from classes.player import Player
from classes.portfolio import Portfolio


class FSP(Player):
    """
    FSP (Flexibility Service Provider) class
    """

    def __init__(self, fsp_cfg, main_cfg, logger):
        """
        Constructor
        """
        super().__init__(fsp_cfg, main_cfg, logger)

        # Get identifier of NODES platform
        res = self.get_organization_id()
        self.nodes_id = res['items'][0]['id']

        # Portfolios owned by the FSP
        self.portfolios = {}
        for p in self.get_portfolios()['items']:
            self.portfolios[p['id']] = Portfolio(p['id'], p)

        # Asset owned by the FSP
        self.assets = {}
        for a in self.get_assets()['items']:
            self.assets[a['id']] = Asset(a['id'], a)

        # Get asset assigned to portfolios
        self.assets_ids, self.assets_mpids = self.get_assets_portfolios_assignments()

        # Assign MPID to assets
        self.set_assets_mpids()

        # Assign assets to portfolios
        self.set_portfolios_assets()

        # Baselines
        self.baselines = {}

    def set_assets_mpids(self):
        for k_p in self.portfolios.keys():
            for a_id in self.assets_ids[k_p]:
                self.assets[a_id].set_mpid(self.assets_mpids[k_p][a_id])

    def set_portfolios_assets(self):
        for k_p in self.portfolios.keys():
            tmp_assets = []
            for a_id in self.assets_ids[k_p]:
                tmp_assets.append(self.assets[a_id])
            self.portfolios[k_p].set_assets(tmp_assets)

    def set_baselines(self, slot_time):
        # slot_time_to = slot_time + timedelta(minutes=granularity)
        slot_time_from = slot_time - timedelta(hours=self.cfg['baselines']['fromBeforeNowHours'])
        slot_time_to = slot_time + timedelta(hours=self.cfg['baselines']['toAfterNowHours'])

        from_str = slot_time_from.strftime('%Y-%m-%dT%H:%M:%SZ')
        to_str = slot_time_to.strftime('%Y-%m-%dT%H:%M:%SZ')

        bs = {}
        for p_k in self.portfolios.keys():
            res = self.nodes_interface.get_request('%s%s' % (self.nodes_interface.cfg['mainEndpoint'],
                                                             'BaselineIntervals?assetPortfolioId=%s&'
                                                             'periodFrom.GreaterThanOrEqual=%s&'
                                                             'periodFrom.LessThanOrEqual=%s&'
                                                             'orderBy=PeriodFrom Asc' % (p_k, from_str, to_str)))
            df = pd.DataFrame(res['items'])
            df['periodFrom'] = pd.to_datetime(df['periodFrom'], utc=True).dt.strftime('%Y-%m-%dT%H:%M:%SZ')
            df.set_index('periodFrom', inplace=True)

            bs[p_k] = df
        self.baselines = bs

    def update_portfolio_baseline(self, portfolio_id, baseline_dataframe):
        tmp_baseline_file = '%s%s%s.csv' % (self.cfg['baselines']['tmpFolder'], os.sep, portfolio_id)
        baseline_dataframe.to_csv(tmp_baseline_file, index=False)

        self.logger.info('Update baseline of portfolio %s, period [%s-%s]' % (portfolio_id,
                                                                              baseline_dataframe['periodTo'].iloc[0],
                                                                              baseline_dataframe['periodTo'].iloc[-1]))

        endpoint = '%s%s' % (self.nodes_interface.cfg['mainEndpoint'], 'BaselineIntervals/import')
        return self.nodes_interface.post_csv_file_request(endpoint, tmp_baseline_file)

    def get_assets_portfolios_assignments(self):
        tmp_assets_grid_assignments = {}
        for elem in self.get_assets_grid_assignments()['items']:
            tmp_assets_grid_assignments[elem['id']] = elem

        tmp_assets_portfolios_assignments = {}
        for p_k in self.portfolios.keys():
            tmp_assets_portfolios_assignments[p_k] = self.get_assets_assigned_to_portfolio(p_k)

        # Cycle over the portfolios that have at least an assignment
        # assets_portfolios_assignments = {}
        assets_mpids = {}
        assets_ids = {}
        for k_p in tmp_assets_portfolios_assignments.keys():
            assets_mpids[k_p] = {}
            assets_ids[k_p] = []
            # Cycle over the asset assigned to the portfolio
            for p_assignment in tmp_assets_portfolios_assignments[k_p]['items']:
                asset_id = tmp_assets_grid_assignments[p_assignment['assetGridAssignmentId']]['assetId']
                # assets_portfolios_assignments[asset_id] = k_p
                # assets_mpids[k_p].append(tmp_assets_grid_assignments[p_assignment['assetGridAssignmentId']]['mpid'])
                assets_mpids[k_p][asset_id] = tmp_assets_grid_assignments[p_assignment['assetGridAssignmentId']]['mpid']
                assets_ids[k_p].append(asset_id)
        return assets_ids, assets_mpids

    def get_organization_id(self):
        res = self.nodes_interface.get_request('%s%s' % (self.nodes_interface.cfg['mainEndpoint'],
                                                         'organizations?name=%s' % self.cfg['name']))
        return res

    def get_assets(self):
        res = self.nodes_interface.get_request('%s%s' % (self.nodes_interface.cfg['mainEndpoint'],
                                                         'assets?operatedByOrganizationId=%s' % self.nodes_id))
        return res

    def get_portfolios(self):
        res = self.nodes_interface.get_request('%s%s' % (self.nodes_interface.cfg['mainEndpoint'],
                                                         'AssetPortfolios?managedByOrganizationId=%s' % self.nodes_id))
        return res

    def get_assets_assigned_to_portfolio(self, portfolio_id):
        res = self.nodes_interface.get_request('%s%s' % (self.nodes_interface.cfg['mainEndpoint'],
                                                         'assetportfolioassignments?assetPortfolioId=%s' % portfolio_id))
        return res

    def get_assets_grid_assignments(self):
        res = self.nodes_interface.get_request('%s%s' % (self.nodes_interface.cfg['mainEndpoint'],
                                                         'assetgridassignments?managedByOrganizationId=%s' % self.nodes_id))
        return res

    def delete_baseline_interval(self, portfolio_id, from_period, to_period):
        endpoint = '%s%s' % (self.nodes_interface.cfg['mainEndpoint'],
                             'BaselineIntervals?assetPortfolioId=%s&periodFrom=%s&periodTo=%s' % (portfolio_id,
                                                                                                  from_period,
                                                                                                  to_period))
        res = self.nodes_interface.delete_request(endpoint)
        return res

    @staticmethod
    def calc_from_to_period(days):
        from_dt = datetime.utcnow()
        to_dt = from_dt + timedelta(days=days)
        return from_dt.strftime('%Y-%m-%dT%H:%M:%SZ'), to_dt.strftime('%Y-%m-%dT%H:%M:%SZ')

    def update_baselines(self, bs_cfg):
        current_time = datetime.utcnow()
        adjusted_time = (current_time.replace(minute=(current_time.minute // 15) * 15, second=0, microsecond=0) +
                         timedelta(minutes=bs_cfg['shiftMinutes']))

        # Cycle over the portfolios
        for k_p in self.portfolios.keys():

            if bs_cfg['source'] == 'file':
                df = self.create_df_baseline_from_file(self.portfolios[k_p], adjusted_time, bs_cfg['fileSettings'])
            elif bs_cfg['source'] == 'db':
                df = self.create_df_baseline_from_db(self.portfolios[k_p], adjusted_time, bs_cfg['dbSettings'])
            else:
                self.logger.error('Baseline source option \'%s\' not available' % bs_cfg['source'])
                return False
            self.update_portfolio_baseline(k_p, df)
        return True

    def create_df_baseline_from_db(self, portfolio, adjusted_time, bs_cfg):
        start_dt = adjusted_time - timedelta(days=bs_cfg['daysToGoBack'])
        start_dt_str = start_dt.strftime('%Y-%m-%dT%H:%M:%SZ')
        end_dt_str = (start_dt+timedelta(hours=bs_cfg['upcomingHoursToQuery'])).strftime('%Y-%m-%dT%H:%M:%SZ')
        str_mpids = '('
        for mpid in portfolio.get_assets_mpids():
            str_mpids = '%s OR site_name=\'%s\'' % (str_mpids, mpid)
        str_mpids = '%s)' % str_mpids.replace('( OR ', '(')

        query = ("SELECT sum(import) AS portfolio_cons, sum(export) AS portfolio_exp from %s WHERE "
                 "time>='%s' AND time<'%s' AND %s GROUP BY time(%im)") % (self.main_cfg['influxDB']['measurement'],
                                                                          start_dt_str, end_dt_str, str_mpids,
                                                                          self.main_cfg['fm']['granularity'])
        self.logger.info('Query: %s' % query)
        try:
            res = self.influx_client.query(query)
            df_data = res[self.main_cfg['influxDB']['measurement']]
            df_data_bs = copy.deepcopy(df_data)
            df_data_bs.index = df_data_bs.index + pd.DateOffset(days=bs_cfg['daysToGoBack'])

            # Handle columns and indexes
            df_data_bs['periodFrom'] = df_data_bs.index
            df_data_bs['periodTo'] = df_data_bs['periodFrom'] + pd.Timedelta(minutes=self.main_cfg['fm']['granularity'])
            df_data_bs.insert(loc=0, column='assetPortfolioId', value=portfolio.id)
            df_data_bs.insert(loc=1, column='quantityType', value='Power')
            df_data_bs.rename(columns={'portfolio_cons': 'quantity'}, inplace=True)
            df_data_bs['quantity'] = df_data_bs['quantity'] / 1e3
            df_data_bs.reset_index(drop=True, inplace=True)

            df_data_bs = df_data_bs[['assetPortfolioId', 'periodFrom', 'periodTo', 'quantity', 'quantityType']]
            return df_data_bs
        except Exception as e:
            self.logger.error('EXCEPTION: %s' % str(e))
            return None

    def create_df_baseline_from_file(self, portfolio, adjusted_time, bs_file_cfg):
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



