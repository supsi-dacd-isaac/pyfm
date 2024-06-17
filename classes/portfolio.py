# import section
from classes.asset import Asset

class Portfolio:
    """
    Portfolio class
    """
    def __init__(self, identifier, metadata):
        """
        Constructor
        """
        self.id = identifier
        self.metadata = metadata

        # Assets belonging to the portfolio
        self.assets = []

    def set_assets(self, assets):
        self.assets = assets

    def get_assets(self):
        return self.assets

    def get_assets_mpids(self):
        mpids = []
        for a in self.assets:
            mpids.append(a.mpid)
        return sorted(mpids)
