# import section
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

