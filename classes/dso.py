# import section
from datetime import timedelta

from classes.player import Player


class DSO(Player):
    """
    DSO (Flexibility Service Provider) class
    """

    def __init__(self, dso_cfg, nodes_cfg, logger):
        """
        Constructor
        """
        super().__init__(dso_cfg, nodes_cfg, logger)

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

    def demand_flexibility(self, dt):

        body = {
            "side": "Buy",
            "regulationType": "Down",
            "quantity": 0.001,
            "minimumQuantity": 0.001,
            "unitPrice": 1,
            # "periodFrom": "2024-05-03T16:00:00.000Z",
            # "periodTo": "2024-05-03T16:15:00.000Z",
            # "validTo": "2024-05-03T16:15:00.000Z",
            "periodFrom": dt.strftime('%Y-%m-%dT%H:%M:%SZ'),
            "periodTo": (dt+timedelta(minutes=15)).strftime('%Y-%m-%dT%H:%M:%SZ'),
            "validTo": (dt+timedelta(minutes=15)).strftime('%Y-%m-%dT%H:%M:%SZ'),
            "currency": "CHF",
            "gridNodeId": "8db506d5-842c-4e90-86af-ff74ccad9685",
            "fillType": "Normal",
        }
        # {
        #     "id": "string",
        #     "status": "Received",
        #     "created": "2024-05-03T13:18:55.538Z",
        #     "createdByUserId": "string",
        #     "lastModified": "2024-05-03T13:18:55.538Z",
        #     "lastModifiedByUserId": "string",
        #     "completionType": "Filled",
        #     "ownerOrganizationId": "string",
        #     "validFrom": "2024-05-03T13:18:55.538Z",
        #     "validTo": "2024-05-03T13:18:55.538Z",
        #     "gridNodeId": "string",
        #     "marketId": "string",
        #     "assetPortfolioId": "string",
        #     "supplierOrganizationId": "string",
        #     "side": "Buy",
        #     "comments": "string",
        #     "externalReference": "string",
        #     "fillType": "Normal",
        #     "regulationType": "Up",
        #     "priceType": "Limit",
        #     "quantity": 0,
        #     "minimumQuantity": 0,
        #     "unitPrice": 0,
        #     "quantityCompleted": 0,
        #     "targetOrganizationId": "string",
        #     "visibility": "Public",
        #     "blockSizeInSeconds": 0,
        #     "orderGroupId": "string",
        #     "minBlocks": 0,
        #     "maxBlocks": 0,
        #     "minAdjacentBlocks": 0,
        #     "maxAdjacentBlocks": 0,
        #     "minAdjacentRestBlocks": 0,
        #     "periodFrom": "2024-05-03T13:18:55.538Z",
        #     "periodTo": "2024-05-03T13:18:55.538Z",
        #     "priorityTimestamp": "2024-05-03T13:18:55.538Z",
        #     "longFlexContractId": "string",
        #     "assetTypeId": "string",
        #     "renewableType": "Renewable",
        #     "minRampUpRate": 0,
        #     "maxRampUpRate": 0,
        #     "minRampDownRate": 0,
        #     "maxRampDownRate": 0,
        #     "quantityType": "Power",
        #     "customProperties": {
        #         "additionalProp1": "string",
        #         "additionalProp2": "string",
        #         "additionalProp3": "string"
        #     },
        #     "targetOrderId": "string",
        #     "ownerSubscriptionTypes": [
        #         "NodesOperator"
        #     ],
        #     "revisionNumber": 0,
        #     "sysStartTime": "2024-05-03T13:18:55.538Z",
        #     "sysEndTime": "2024-05-03T13:18:55.538Z"
        # }
        return self.nodes_interface.post_request('%s%s' % (self.nodes_interface.cfg['mainEndpoint'], 'orders'), body)

