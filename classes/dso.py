# import section
from datetime import timedelta
from classes.player import Player


class DSO(Player):
    """
    DSO (Flexibility Service Provider) class
    """

    def __init__(self, dso_cfg, main_cfg, logger):
        """
        Constructor
        """
        super().__init__(dso_cfg, main_cfg, logger)

        # Get identifier of NODES platform
        res = self.get_grid_areas()
        self.nodes_id = res['items'][0]['id']

    def add_node_to_grid(self, parent_node_id, node_id, node_subgrid):
        # node_subgrid is a dictionary containing the metadata of the node and children and the hierarchy,
        return True

    def remove_node_from_grid(self, node_id, cascade):
        # if cascade is True:
        #     # Delete the node and its children
        # else:
        #     # Check if the node has no children and in case delete it
        return True

    def get_nodes_list(self):
        grid_area_id = self.nodes_interface.cfg['players'][self.cfg['id']]['gridAreaId']
        res = self.nodes_interface.get_request('%s%s' % (self.nodes_interface.cfg['mainEndpoint'],
                                                         'gridnodes?gridAreaId=%s' % grid_area_id))
        return res

    def get_grid_areas(self):
        res = self.nodes_interface.get_request('%s%s' % (self.nodes_interface.cfg['mainEndpoint'],
                                                         'GridAreas?name=%s' % self.cfg['name']))
        return res

    def request_contract(self, dt_slot, fmo):
        if self.cfg['flexibilitySource'] == 'random':
            quantity = self.get_random_quantity()
        elif self.cfg['flexibilitySource'] == 'db':
            quantity = self.get_quantity_from_db(dt_slot)
        else:
            self.logger.error('Option \'%s\' not available for creating a contract '
                              'request' % self.cfg['flexibilitySource'])
            return False

        # Get the node id configured to demand flexibility
        node_id = None
        for n in self.grid_nodes:
            if n['name'] in self.cfg['orderSection']['nodeName']:
                node_id = n['id']

        # We suppose here to consider only the case with one market
        body = {
            "name": 'contract_request_%s_%s' % (self.cfg['id'], dt_slot.strftime('%Y%m%d')),
            "contractType": "Standard",
            "comments": "",
            "marketId": self.markets[0]['id'],
            "gridNodeId": node_id,
            "periodFrom": (dt_slot+timedelta(days=7)).strftime('%Y-%m-%dT00:00:00Z'),
            "periodTo": (dt_slot+timedelta(days=14)).strftime('%Y-%m-%dT00:00:00Z'),
            "buyerOrganizationId": self.organization['id'],
            "regulationType": "Up",
            "quantity": quantity,
            "quantityType": "Power",
            "unitPrice": self.cfg['contractSection']['mainSettings']['unitPrice'],
            "availabilityPrice": self.cfg['contractSection']['mainSettings']['availabilityPrice'],
            # * * * * *
            # | | | | |-> Day Of Week (1-7) 1 is Monday, 7 is Sunday
            # | | | |---> Month of Year (1-12)
            # | | |-----> Day Of Month (1-31)
            # | |-------> Hour (0-23)
            # |---------> Minute (0-59)
            "crontab": self.cfg['contractSection']['mainSettings']['crontab']
        }
        body.update(self.cfg['orderSection']['mainSettings'])

        response = self.nodes_interface.post_request('%s%s' % (self.nodes_interface.cfg['mainEndpoint'],
                                                               'longflexcontracts'), body)
        result = self.handle_response(response, body)
        if result is not False:
            # The delivery of the request has been successful, save the data in the ledger
            fmo.add_entry_to_contract_request_ledger(self.cfg, self.markets[0]['name'], body)
        return result

    def sign_contract(self, contract_proposal, fmo):
        body = {
            "approvedByBuyer": True,
            "visibility": "Public",
            "id": contract_proposal['id'],
        }

        str_url = '%s%s/%s' % (self.nodes_interface.cfg['mainEndpoint'], 'longflexcontracts', contract_proposal['id'])
        response = self.nodes_interface.patch_request(str_url, body)

        result = self.handle_response(response, body)
        if result is not False:
            fmo.update_contract_request(contract_proposal, body)
        return result
