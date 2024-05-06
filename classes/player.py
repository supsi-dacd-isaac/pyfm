# import section
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

    def get_orders(self):
        res = self.nodes_interface.get_request('%s%s' % (self.nodes_interface.cfg['mainEndpoint'], 'orders'))
        return res
