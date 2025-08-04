# import section
import random
import sys
from datetime import datetime, timedelta
from influxdb import DataFrameClient
import pandas as pd

from classes.nodes_interface import NODESInterface


class Player:
    """
    Player (Flexibility Service Provider) class
    """

    def __init__(self, player_cfg, main_cfg, logger):
        """
        Constructor
        """
        self.cfg = player_cfg
        self.main_cfg = main_cfg
        self.logger = logger
        self.nodes_interface = NODESInterface(main_cfg['nodesAPI'], logger)
        self.nodes_interface.set_token(player_cfg)
        self.markets = []
        self.grid_nodes = []
        self.organization = None
        self.grid_area = None

        self.logger.info('Connection to InfluxDb server on socket [%s:%s]' % (main_cfg['influxDB']['host'],
                                                                              main_cfg['influxDB']['port']))
        try:
            self.influx_client = DataFrameClient(host=main_cfg['influxDB']['host'],
                                                 port=main_cfg['influxDB']['port'],
                                                 password=main_cfg['influxDB']['password'],
                                                 username=main_cfg['influxDB']['user'],
                                                 database=main_cfg['influxDB']['database'],
                                                 ssl=main_cfg['influxDB']['ssl'])
        except Exception as e:
            self.logger.error('EXCEPTION: %s' % str(e))
            sys.exit(3)
        self.logger.info('Connection successful')

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

    def get_contracts(self, filter_dict=None):
        return self.get_nodes_api_info('longflexcontracts', filter_dict)

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

    def demand_flexibility(self, dt_slot):
        # Get demanded quantity
        if self.cfg['flexibilitySource'] == 'random':
            demanded_flexibility = self.get_random_quantity()
        elif self.cfg['flexibilitySource'] == 'db':
            demanded_flexibility = self.get_quantity_from_db(dt_slot)
        elif self.cfg['flexibilitySource'] == 'forecast':
            demanded_flexibility = self.get_quantity_from_forecast(dt_slot)
        else:
            self.logger.error('Option \'%s\' not available for demanding flexibility '
                              'strategy' % self.cfg['flexibilitySource'])
            return False

        # Check that demanded flexibility is not zero
        if demanded_flexibility == 0.0:
            self.logger.info('No flexibility demanded for slot %s' % dt_slot.strftime('%Y-%m-%dT%H:%M:%SZ'))
            return False
        
        # Get the node id configured to demand flexibility
        node_id = None
        for n in self.grid_nodes:
            if n['name'] in self.cfg['orderSection']['nodeName']:
                node_id = n['id']

        body = {
            "ownerOrganizationId": self.organization['id'],
            "periodFrom": dt_slot.strftime('%Y-%m-%dT%H:%M:%SZ'),
            "periodTo": (dt_slot+timedelta(minutes=15)).strftime('%Y-%m-%dT%H:%M:%SZ'),
            "validTo": (dt_slot+timedelta(minutes=15)).strftime('%Y-%m-%dT%H:%M:%SZ'),
            "marketId": self.markets[0]['id'],
            "gridNodeId": node_id,
            "quantity": demanded_flexibility,
        }
        body.update(self.cfg['orderSection']['mainSettings'])
        response = self.nodes_interface.post_request('%s%s' % (self.nodes_interface.cfg['mainEndpoint'], 'orders'), body)
        return self.handle_response(response, body)

    def get_random_quantity(self):
        index = random.randint(0, len(self.cfg['orderSection']['quantities']['random']) - 1)
        return self.cfg['orderSection']['quantities']['random'][index]

    def get_quantity_from_db(self, dt_slot):
        start_dt = dt_slot - timedelta(days=self.cfg['orderSection']['quantities']['db']['daysToGoBack'])
        start_dt_str = start_dt.strftime('%Y-%m-%dT%H:%M:%SZ')
        end_dt_str = (start_dt + timedelta(minutes=self.main_cfg['fm']['granularity'])).strftime('%Y-%m-%dT%H:%M:%SZ')
        str_fields = ''
        for f in self.cfg['orderSection']['quantities']['db']['fields']:
            str_fields = '%s mean(%s),' % (str_fields, f)
        str_fields = str_fields[1:-1]

        query = ("SELECT %s from %s WHERE energy_community=\'%s\' AND device_name=\'%s\' AND time>='%s' AND time<'%s' "
                 "GROUP BY time(%im)") % (str_fields, self.main_cfg['influxDB']['measurement'],
                                          self.cfg['orderSection']['quantities']['db']['community'],
                                          self.cfg['orderSection']['quantities']['db']['device'],
                                          start_dt_str,
                                          end_dt_str,
                                          self.main_cfg['fm']['granularity'])

        self.logger.info('Query: %s' % query)
        try:
            res = self.influx_client.query(query)
            df_data = res[self.main_cfg['influxDB']['measurement']]
            return round(df_data.sum(axis=1).values[0]/1e3, 3)
        except Exception as e:
            self.logger.error('EXCEPTION: %s' % str(e))
            return False
        
    def get_quantity_from_forecast(self, dt_slot):
        """Quantity is calculated based on the forecasted value and a threshold (cut).

        Args:
            dt_slot (_type_): time slot

        Returns:
            _type_: max(forecasted_value - cut_value, 0)
        """
        forecasted_value = 0.0
        if self.cfg['orderSection']['quantities']['forecast']["source"] == 'file':
            df = pd.read_csv(self.cfg['orderSection']['quantities']['forecast']["filename"], sep=',')
            df['slot_dt'] = pd.to_datetime(df['slot'])
            forecasted_value = df[df['slot_dt'] > datetime.now()].iloc[0]["quantity"]
            pass
        elif self.cfg['orderSection']['quantities']['forecast']["source"] == 'aem':
            #TODO read from AEM db
            pass
        else:
            self.logger.error('Option \'%s\' not available for forecasting input '
                              % self.cfg['orderSection']['quantities']['forecast']["source"])
            raise Exception('Option \'%s\' not available for forecasting input '
                            % self.cfg['orderSection']['quantities']['forecast']["source"])
        
        cut_value = self.cfg['orderSection']['quantities']['forecast']["cut"]
        return max(forecasted_value - cut_value, 0.0)

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
                if response is not False:
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
        quantity_dn = 0.0
        for order in orders:
            # Only not-settled order will be considered
            if order['completionType'] is None:
                if order['regulationType'] == 'Down':
                    quantity_dn += float(order['quantity'])
                elif order['regulationType'] == 'Up':
                    quantity_up += float(order['quantity'])

        quantity_up = round(quantity_up, 3)
        quantity_dn = round(quantity_dn, 3)
        self.logger.info('Flexibility demanded by the DSO (%s): Up = %.3f MW, Down = %.3f MW' % (quantity_type,
                                                                                                 quantity_up,
                                                                                                 quantity_dn))
        return {'Up': quantity_up, 'Down': quantity_dn}

    def calculate_quantity_to_sell_basic(self, timeslot, demand, baseline_time_series):
        if demand > 0:
            # Basic approach, only the baseline value of the timeslot is taking into account,
            # past and future are not considered
            baseline = baseline_time_series.loc[timeslot.strftime('%Y-%m-%dT%H:%M:%SZ')]
            self.logger.info('Baseline: %.3f MW (%i%% to be sold)' % (baseline, self.cfg['orderSection']['quantityPercBaseline']))
            marketable_quantity = round(baseline * self.cfg['orderSection']['quantityPercBaseline'] / 1e2, 3)

            if marketable_quantity <= demand:
                # Everything is sold
                return marketable_quantity
            else:
                # Only a part of the flexibility is sold covering the entire demand
                return demand
        else:
            return 0.0

    @staticmethod
    def handle_response(response, body):
        if response is True:
            return body
        else:
            return response
