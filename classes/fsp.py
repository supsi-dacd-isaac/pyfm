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

    def get_baselines(self, from_period, to_period):
        bs = {}
        for p in self.portfolios:
            # res = self.nodes_interface.get_request('%s%s' % (self.nodes_interface.cfg['mainEndpoint'],
            #                                                  'BaselineIntervals?assetPortfolioId=%s&periodFrom=%s&periodTo=%s' % (p['id'], from_period, to_period)))
            res = self.nodes_interface.get_request('%s%s' % (self.nodes_interface.cfg['mainEndpoint'],
                                                             'BaselineIntervals?assetPortfolioId=%s' % p['id']))
            df = pd.DataFrame(res['items'])
            df.set_index('periodFrom', inplace=True)

            bs[p['id']] = df
        return bs

    def update_baselines(self, portfolio_id, baseline_dataframe):
        tmp_baseline_file = '%s%s%s.csv' % (self.cfg['baselines']['tmpFolder'], os.sep, portfolio_id)
        baseline_dataframe.to_csv(tmp_baseline_file, index=False)

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

    # def add_baseline_interval(self, portfolio_id, from_period, to_period):
    #     body = {
    #         # "id": "string",
    #         # "status": "Received",
    #         # "created": datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
    #         # "createdByUserId": "string",
    #         # "lastModified": "2024-04-30T11:42:56.287Z",
    #         # "lastModifiedByUserId": "string",
    #         "assetPortfolioId": portfolio_id,
    #         "periodFrom": from_period,
    #         "periodTo": to_period,
    #         # "batchReference": "string",
    #         "quantity": 0.1,
    #         "quantityType": "Power"
    #     }
    #
    #     # body = {
    #     #     "assetPortfolioId": portfolio_id,
    #     #     "periodFrom": from_period,
    #     #     "periodTo": to_period,
    #     #     "quantity": 0.03,
    #     #     "quantityType": "Power"
    #     # }
    #     self.nodes_interface.post_request('%s%s' % (self.nodes_interface.cfg['mainEndpoint'], 'BaselineIntervals/import'),
    #                                       body)


    @staticmethod
    def calc_from_to_period(days):
        from_dt = datetime.utcnow()
        to_dt = from_dt + timedelta(days=days)
        return from_dt.strftime('%Y-%m-%dT%H:%M:%SZ'), to_dt.strftime('%Y-%m-%dT%H:%M:%SZ')

    # def create_portfolio(self, portfolio_id, portfolio_metadata):
    #     self.portfolios[portfolio_id] = Portfolio(portfolio_id, portfolio_metadata)
    #     return True
    #
    # def delete_portfolio(self, portfolio_id):
    #     if len(self.portfolios[portfolio_id].get_assets().keys()) == 0:
    #         del self.portfolios[portfolio_id]
    #         return True
    #     else:
    #         return False
    #
    # def add_asset_to_portfolio(self, portfolio_id, asset):
    #     if asset.dso.id == self.dso.cfg['id'] and asset.approved is True:
    #         self.portfolios[portfolio_id].add_asset(asset)
    #         return True
    #     else:
    #         return False
    #
    # def remove_asset_from_portfolio(self, portfolio_id, asset):
    #     if asset.dso.id == self.id and asset.approved is True:
    #         self.portfolios[portfolio_id].remove_asset(asset)
    #         return True
    #     else:
    #         return False
    #
    # def add_baseline_to_portfolio(self, portfolio_id, baseline_id, baseline_metadata, baseline_timeseries):
    #     return self.portfolios[portfolio_id].add_baseline(baseline_id, baseline_metadata, baseline_timeseries)



