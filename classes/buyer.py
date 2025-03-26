import pandas as pd

class Buyer:
    """
    Represents a buyer in the flexibility market.

    Attributes:
        buyer_id (str): Identifier for this buyer.
        demand_curve (pd.DataFrame):
            A time-indexed DataFrame containing demand values.
            For example, it may have one column named 'demand'.
            The index should be a pandas DateTimeIndex.
        willingness_to_pay (pd.DataFrame):
            A time-indexed DataFrame containing the buyer's
            willingness to pay (price) values over time.
            Typically, this might include a column named 'price'.
    """

    def __init__(self, buyer_id, demand_curve, willingness_to_pay):
        """
        Initializes the Buyer with:
            id (str): Identifier of the buyer.
            demand_curve (pd.DataFrame): Buyer’s demand specification.
            willingness_to_pay (pd.DataFrame): Buyer’s max price over time.

        Assumes both 'demand_curve' and 'willingness_to_pay' have a DateTimeIndex.
        """
        self.id = buyer_id
        self.demand_curve = demand_curve
        self.willingness_to_pay = willingness_to_pay

    # -------------------------------------------------------------------------
    # GETTER METHODS
    # -------------------------------------------------------------------------
    def get_demand(self, start_time, end_time=None):
        """
        Retrieve the buyer's demand for a specified time or interval.
        """
        if end_time is None:
            return self.demand_curve.loc[start_time]
        else:
            return self.demand_curve.loc[start_time:end_time]

    def get_willingness_to_pay(self, start_time, end_time=None):
        """
        Retrieve the buyer’s willingness to pay for a specified time or interval.
        """
        if end_time is None:
            return self.willingness_to_pay.loc[start_time]
        else:
            return self.willingness_to_pay.loc[start_time:end_time]

    # -------------------------------------------------------------------------
    # DEMAND DATAFRAME MODIFIERS
    # -------------------------------------------------------------------------
    def add_demand_entry(self, timestamp, demand_value):
        """
        Add a new row or overwrite an existing row in the demand_curve DataFrame
        at the specified timestamp with the given demand_value.
        """
        if not isinstance(timestamp, pd.Timestamp):
            timestamp = pd.to_datetime(timestamp)

        # If the row doesn't exist, this will create it; if it does, this updates it.
        self.demand_curve.loc[timestamp, 'demand'] = demand_value
        self.demand_curve.sort_index(inplace=True)

    def update_demand_entry(self, timestamp, demand_value):
        """
        Update the demand value for an existing row in demand_curve.
        Raises KeyError if the row doesn't exist.
        """
        if not isinstance(timestamp, pd.Timestamp):
            timestamp = pd.to_datetime(timestamp)

        if timestamp not in self.demand_curve.index:
            raise KeyError(f"Timestamp {timestamp} not found in demand_curve.")

        self.demand_curve.at[timestamp, 'demand'] = demand_value

    def remove_demand_entry(self, timestamp):
        """
        Remove a row from the demand_curve DataFrame by the specified timestamp.
        Raises KeyError if the row doesn't exist.
        """
        if not isinstance(timestamp, pd.Timestamp):
            timestamp = pd.to_datetime(timestamp)

        if timestamp not in self.demand_curve.index:
            raise KeyError(f"Timestamp {timestamp} not found in demand_curve.")

        self.demand_curve.drop(timestamp, inplace=True)

    # -------------------------------------------------------------------------
    # WILLINGNESS_TO_PAY DATAFRAME MODIFIERS
    # -------------------------------------------------------------------------
    def add_willingness_entry(self, timestamp, price_value):
        """
        Add a new row or overwrite an existing row in the willingness_to_pay
        DataFrame at the specified timestamp with the given price_value.
        """
        if not isinstance(timestamp, pd.Timestamp):
            timestamp = pd.to_datetime(timestamp)

        self.willingness_to_pay.loc[timestamp, 'price'] = price_value
        self.willingness_to_pay.sort_index(inplace=True)

    def update_willingness_entry(self, timestamp, price_value):
        """
        Update the price value for an existing row in willingness_to_pay.
        Raises KeyError if the row doesn't exist.
        """
        if not isinstance(timestamp, pd.Timestamp):
            timestamp = pd.to_datetime(timestamp)

        if timestamp not in self.willingness_to_pay.index:
            raise KeyError(f"Timestamp {timestamp} not found in willingness_to_pay.")

        self.willingness_to_pay.at[timestamp, 'price'] = price_value

    def remove_willingness_entry(self, timestamp):
        """
        Remove a row from the willingness_to_pay DataFrame by the specified timestamp.
        Raises KeyError if the row doesn't exist.
        """
        if not isinstance(timestamp, pd.Timestamp):
            timestamp = pd.to_datetime(timestamp)

        if timestamp not in self.willingness_to_pay.index:
            raise KeyError(f"Timestamp {timestamp} not found in willingness_to_pay.")

        self.willingness_to_pay.drop(timestamp, inplace=True)

    # -------------------------------------------------------------------------
    # REQUESTING FLEXIBILITY
    # -------------------------------------------------------------------------
    def request_flexibility(self, start_time, end_time=None):
        """
        Creates a request for flexibility over a time period (or a single timestamp).
        """
        demand_data = self.get_demand(start_time, end_time)
        price_data = self.get_willingness_to_pay(start_time, end_time)

        # If the retrieved data is a DataFrame (multiple rows), we aggregate
        if isinstance(demand_data, pd.DataFrame):
            total_demand = demand_data['demand'].sum()
        else:
            total_demand = demand_data['demand']

        if isinstance(price_data, pd.DataFrame):
            avg_price = price_data['price'].mean()
        else:
            avg_price = price_data['price']

        return {
            "buyer_id": self.id,
            "time_window": (start_time, end_time),
            "total_demand": total_demand,
            "average_price_limit": avg_price
        }
