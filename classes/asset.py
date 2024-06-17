# import section

class Asset:
    """
    Asset (generic asset able to sell flexibility) class
    """
    def __init__(self, identifier, metadata):
        """
        Constructor
        """
        self.id = identifier
        self.metadata = metadata
        self.mpid = None

    def set_mpid(self, mpid):
        self.mpid = mpid
