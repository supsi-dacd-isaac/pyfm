# import section
import random
from datetime import datetime, timedelta

from classes.nodes_interface import NODESInterface


class Player:
    """
    Player (Flexibility Service Provider) class
    """

    def __init__(self, player_cfg, nodes_cfg, logger):
        """
        Constructor
        """
        self.cfg = player_cfg
        self.logger = logger
        self.nodes_interface = NODESInterface(nodes_cfg, logger)
        self.nodes_interface.set_token(player_cfg)
        self.markets = []
        self.grid_nodes = []
        self.organization = None
        self.grid_area = None

    def set_markets(self, filter_dict=None):
        self.markets = self.get_nodes_api_info('markets', filter_dict)

    @staticmethod
    def get_adjusted_time(granularity, shift):
        current_time = datetime.utcnow()
        adj_time = current_time.replace(minute=(current_time.minute // granularity) * granularity)
        adj_time = adj_time.replace(second=0, microsecond=0)
        adj_time += timedelta(minutes=shift)
        return adj_time

    def set_organization(self, filter_dict=None):
        orgs = self.get_nodes_api_info('organizations', filter_dict)
        if len(orgs) == 1:
            self.organization = orgs[0]
        else:
            self.logger.error('Unable to get organization information with this filter: %s' % filter_dict)

    def set_grid_area(self, filter_dict=None):
        ga = self.get_nodes_api_info('GridAreas', filter_dict)
        if len(ga) == 1:
            self.grid_area = ga[0]
        else:
            self.logger.error('Unable to get the grid area information with this filter: %s' % filter_dict)

    def set_grid_nodes(self, filter_dict=None):
        self.grid_nodes = self.get_nodes_api_info('gridnodes', filter_dict)

    def get_resolutions(self):
        return self.nodes_interface.get_request('%s%s' % (self.nodes_interface.cfg['mainEndpoint'],
                                                          'settlements/resolutions'))

    def get_orders(self, filter_dict=None):
        return self.get_nodes_api_info('orders', filter_dict)

    def get_nodes_api_info(self, request_type, filter_dict=None):
        if filter_dict is not None:
            filter_str = '?'
            for k in filter_dict.keys():
                filter_str += '%s=%s&' % (k, filter_dict[k])
            filter_str = filter_str[:-1]
        else:
            filter_str = ''
        res = self.nodes_interface.get_request('%s%s' % (self.nodes_interface.cfg['mainEndpoint'],
                                                         '%s%s' % (request_type, filter_str)))
        if 'items' in res.keys():
            return res['items']
        else:
            return []

    def get_random_quantity(self):
        index = random.randint(0, len(self.cfg['orderSection']['quantities']) - 1)
        return self.cfg['orderSection']['quantities'][index]

    def demand_flexibility(self, dt):
        # Get demanded quantity
        demanded_flexibility = self.get_random_quantity()

        # Get the node id configured to demand flexibility
        node_id = None
        for n in self.grid_nodes:
            if n['name'] in self.cfg['orderSection']['nodeName']:
                node_id = n['id']

        body = {
            "ownerOrganizationId": self.organization['id'],
            "periodFrom": dt.strftime('%Y-%m-%dT%H:%M:%SZ'),
            "periodTo": (dt+timedelta(minutes=15)).strftime('%Y-%m-%dT%H:%M:%SZ'),
            "validTo": (dt+timedelta(minutes=15)).strftime('%Y-%m-%dT%H:%M:%SZ'),
            "marketId": self.markets[0]['id'],
            "gridNodeId": node_id,
            "quantity": demanded_flexibility,
        }
        body.update(self.cfg['orderSection']['mainSettings'])
        response = self.nodes_interface.post_request('%s%s' % (self.nodes_interface.cfg['mainEndpoint'], 'orders'), body)
        if response is True:
            return body
        else:
            return response

    def sell_flexibility(self, dt, p_id, dso_demand):
        selling_result = {}
        for k_regulation_type in dso_demand.keys():
            quantity_to_sell = self.calculate_quantity_to_sell_basic(dt, dso_demand[k_regulation_type],
                                                                     self.baselines[p_id]['quantity'])

            self.logger.info('Portfolio: %s, Regulation: %s, Bidded flexibility: %s' % (p_id, k_regulation_type,
                                                                                        quantity_to_sell))
            if quantity_to_sell > 0:
                body = {
                    "ownerOrganizationId": self.organization['id'],
                    "periodFrom": dt.strftime('%Y-%m-%dT%H:%M:%SZ'),
                    "periodTo": (dt+timedelta(minutes=15)).strftime('%Y-%m-%dT%H:%M:%SZ'),
                    "validTo": (dt+timedelta(minutes=15)).strftime('%Y-%m-%dT%H:%M:%SZ'),
                    "marketId": self.markets[0]['id'],
                    "assetPortfolioId": p_id,
                    "regulationType": k_regulation_type,
                    "quantity": quantity_to_sell,
                }
                body.update(self.cfg['orderSection']['mainSettings'])
                response = self.nodes_interface.post_request('%s%s' % (self.nodes_interface.cfg['mainEndpoint'], 'orders'), body)
                if response is True:
                    selling_result[k_regulation_type] = body
                else:
                    selling_result[k_regulation_type] = False
            else:
                selling_result[k_regulation_type] = False
        return selling_result

    def print_user_info(self, user_info):
        self.logger.info('user id: %s' % user_info['user']['id'])
        self.logger.info('user givenname: %s' % user_info['user']['givenName'])
        self.logger.info('user familyname: %s' % user_info['user']['familyName'])
        self.logger.info('user type: %s' % user_info['user']['userType'])

    def print_player_info(self):
        self.logger.info('market id: %s' % self.markets[0]['id'])
        self.logger.info('market name: %s' % self.markets[0]['name'])

    def get_flexibility_quantities(self, slot_time, granularity, order_type, quantity_type):
        filter_dict = {
            'ownerOrganizationId': self.organization['id'],
            'periodFrom': slot_time.strftime('%Y-%m-%dT%H:%M:%SZ'),
            'periodTo': (slot_time + timedelta(minutes=granularity)).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'type': order_type,
            'quantityType': quantity_type,
        }
        orders = self.get_orders(filter_dict=filter_dict)

        quantity_up = 0.0
        quantity_down = 0.0
        for order in orders:
            # Only not-settled order will be considered
            if order['completionType'] is None:
                if order['regulationType'] == 'Down':
                    quantity_down += float(order['quantity'])
                elif order['regulationType'] == 'Up':
                    quantity_up += float(order['quantity'])

        self.logger.info('Flexibility quantities (%s): UP = %.3f, DOWN = %.3f' % (quantity_type, quantity_up, quantity_down))
        return {'Up': quantity_up, 'Down': quantity_down}

    def calculate_quantity_to_sell_basic(self, timeslot, demand, baseline_time_seris):
        if demand > 0:
            # Basic approach, only the baseline value of the timeslot is taking into account,
            # past and and future are not considered
            baseline = baseline_time_seris.loc[timeslot.strftime('%Y-%m-%dT%H:%M:%SZ')]
            marketable_quantity = baseline * self.cfg['orderSection']['quantityPercBaseline'] / 1e2

            if marketable_quantity <= demand:
                # Everything is sold
                return marketable_quantity
            else:
                # Only a part of the flexibility is sold covering the entire demand
                return demand
        else:
            return 0.0

