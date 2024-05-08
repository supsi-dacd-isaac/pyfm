# import section
import os

import pandas as pd
from classes.player import Player
from datetime import datetime, timedelta


class FSP(Player):
    """
    FSP (Flexibility Service Provider) class
    """

    def __init__(self, fsp_cfg, nodes_cfg, logger):
        """
        Constructor
        """
        super().__init__(fsp_cfg, nodes_cfg, logger)

        # Get identifier of NODES platform
        res = self.get_organization_id()
        self.nodes_id = res['items'][0]['id']

        # Portfolios owned by the FSP
        self.portfolios = self.get_portfolios()['items']
        self.assets = self.get_assets()['items']

        # Get asset assigned to portfolios
        self.assets_portfolios_assignments = self.get_assets_portfolios_assignments()
        self.baselines = {}

    def set_baselines(self, slot_time):
        # slot_time_to = slot_time + timedelta(minutes=granularity)
        slot_time_from = slot_time - timedelta(hours=self.cfg['baselines']['fromBeforeNowHours'])
        slot_time_to = slot_time + timedelta(hours=self.cfg['baselines']['toAfterNowHours'])

        from_str = slot_time_from.strftime('%Y-%m-%dT%H:%M:%SZ')
        to_str = slot_time_to.strftime('%Y-%m-%dT%H:%M:%SZ')

        bs = {}
        for p in self.portfolios:
            res = self.nodes_interface.get_request('%s%s' % (self.nodes_interface.cfg['mainEndpoint'],
                                                             'BaselineIntervals?assetPortfolioId=%s&'
                                                             'periodFrom.GreaterThanOrEqual=%s&'
                                                             'periodFrom.LessThanOrEqual=%s&'
                                                             'orderBy=PeriodFrom Asc' % (p['id'], from_str, to_str)))
            df = pd.DataFrame(res['items'])
            df['periodFrom'] = pd.to_datetime(df['periodFrom'], utc=True).dt.strftime('%Y-%m-%dT%H:%M:%SZ')
            df.set_index('periodFrom', inplace=True)

            bs[p['id']] = df
        self.baselines = bs

    def update_baselines(self, portfolio_id, baseline_dataframe):
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
        for p in self.portfolios:
            tmp_assets_portfolios_assignments[p['id']] = self.get_assets_assigned_to_portfolio(p['id'])

        # Cycle over the portfolios that have at least an assignment
        assets_portfolios_assignments = {}
        for k_p in tmp_assets_portfolios_assignments.keys():
            # Cycle over the asset assigned to the portfolio
            for p_assignment in tmp_assets_portfolios_assignments[k_p]['items']:
                asset_id = tmp_assets_grid_assignments[p_assignment['assetGridAssignmentId']]['assetId']
                assets_portfolios_assignments[asset_id] = k_p
        return assets_portfolios_assignments

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





