# import section
import pandas as pd


class FMO:
    """
    FMO (Flexibility Martket Operator) class
    """

    def __init__(self, cfg, logger, pgi):
        """
        Constructor
        :param cfg: Dictionary with the configurable settings
        :type cfg: dict
        :param logger: Logger
        :type logger: Logger object
        """
        # Market ledger
        self.ledger = pd.DataFrame()
        ledger = pd.DataFrame(columns=['market_timeslot', 'created_at', 'player', 'role', 'case', 'portfolio', 'amount', 'unit', 'price'])
        ledger['market_timeslot'] = pd.to_datetime(ledger['market_timeslot'])
        ledger['created_at'] = pd.to_datetime(ledger['created_at'])
        ledger['player'] = ledger['player'].astype(str)
        ledger['role'] = ledger['role'].astype(str)
        ledger['portfolio'] = ledger['portfolio'].astype(str)
        ledger['case'] = ledger['case'].astype(str)
        ledger['amount'] = ledger['amount'].astype(float)
        ledger['unit'] = ledger['unit'].astype(str)
        ledger['price'] = ledger['price'].astype(float)
        self.ledger = ledger
        self.pgi = pgi

    def add_entry_to_ledger(self, timeslot, player, portfolio, features):
        if portfolio is None:
            portfolio_id = 'none'
            price = 0.0
        else:
            portfolio_id = portfolio
            price = float(features['unitPrice'])

        sql = ('INSERT INTO market_ledger ' \
              '(timeslot_market, player_id, player_role, portfolio_id, side, regulation, flexibility_quantity, '
               'flexibility_unit, price, currency) VALUES' \
              '(\'%s\',\'%s\',\'%s\',\'%s\',\'%s\',\'%s\',\'%s\',\'%s\',\'%s\',\'%s\')' %
               (timeslot, player.cfg['id'], player.cfg['role'], portfolio_id, features['side'],
                features['regulationType'], features['quantity'], 'MW', float(price),
                features['currency']))
        cur = self.pgi.conn.cursor()
        cur.execute(sql)
        cur.close()
        return True

    # def place_order(self, ts_slot, grid_node, portfolio, order_data):
    #     return True

