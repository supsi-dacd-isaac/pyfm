import pandas as pd

class MarketOperator:
    """
    A class representing the market operator that:
      1) Receives flexibility requests from Buyers.
      2) Receives bids from Bidders.
      3) Solves the pay-as-bid market.
      4) Settles/Remunerates based on the approach described in the attached PDF (Chapter 4).
    """

    def __init__(self, alpha_rem, beta_rem, threshold_rem, power_ref, price_ref):
        """
        Initialize the MarketOperator with remuneration parameters.
        """
        self.alpha_rem = alpha_rem
        self.beta_rem = beta_rem
        self.threshold_rem = threshold_rem

        # Data structures to store incoming requests/bids
        self.buyer_requests = {}
        self.bidder_bids = {}

        # Data structures for accepted outcomes (post market-solving)
        self.time_slot_data = {}
        self.time_slot_baseline = {}

        # Fields to store average reference values (computed from data)
        # By default, set them to None or 0.0 until computed
        self.power_ref = power_ref
        self.price_ref = price_ref

        # Fields to store the accepted prices and requested powers
        self.requested_powers = []
        self.accepted_prices = []

        # Store time slots that have been cleared
        self.cleared_time_slots = set()

        # Store bidder baselines
        self.bidder_baselines = {}

        # Store bidder actuals
        self.bidder_actuals = {}

        # Clearing results history
        self.clearing_results_history = {}

    # -------------------------------------------------------------------------
    # Receiving Buyer Requests and Bidder Bids
    # -------------------------------------------------------------------------
    def receive_buyer_request(self, time_slot, request_info):
        """
        Receive a new flexibility request from a Buyer for a given time_slot.

        Parameters
        ----------
        time_slot : str or any hashable time identifier
            The time slot for which the Buyer requests flexibility.
        request_info : dict
            Must include at least:
                {
                  "buyer_id": <str>,
                  "requested_power": <float>,
                  "max_price": <float>,
                  ... (and any other relevant fields)
                }
        """
        self.requested_powers.append(request_info['requested_power'])
        if time_slot not in self.buyer_requests:
            self.buyer_requests[time_slot] = []
        self.buyer_requests[time_slot].append(request_info)

    def receive_bid_from_bidder(self, time_slot, bid_info):
        """
        Receive a new flexibility bid from a Bidder for a given time_slot.

        Parameters
        ----------
        time_slot : str or any hashable time identifier
            The time slot for which the Bidder offers flexibility.
        bid_info : dict
            Must include at least:
                {
                  "bidder_id": <str>,
                  "capacity_offered": <float>,
                  "availability_price": <float>,
                  "utilization_price": <float>,
                  ... (and any other relevant fields)
                }
        """
        if time_slot not in self.bidder_bids:
            self.bidder_bids[time_slot] = []
        self.bidder_bids[time_slot].append(bid_info)

    def receive_baseline_from_bidder(self, time_slot, bidder_id, baseline_value):
        """
        (Optional) Store baseline info reported by a Bidder for a given time_slot.
        """
        if time_slot not in self.time_slot_baseline:
            self.time_slot_baseline[time_slot] = {}
        self.time_slot_baseline[time_slot][bidder_id] = baseline_value

    # -------------------------------------------------------------------------
    # Market Solving (Pay-as-Bid) & Settlement
    # -------------------------------------------------------------------------
    def pay_as_bid_market_solving(self, steps_to_clear=1):
        """
        Solve the pay-as-bid market and return two dictionaries:
        - accepted_bids: Dictionary of accepted bids for each time slot.
        - non_accepted_bids: Dictionary of non-accepted bids for each time slot.
        """
        accepted_bids = {}
        non_accepted_bids = {}
        clearing_results = {}

        time_slots_to_clear = [ts for ts in sorted(self.buyer_requests.keys()) if not self.is_time_slot_cleared(ts)][
                              -steps_to_clear:]

        for time_slot in time_slots_to_clear:
            bids_list = self.bidder_bids.get(time_slot, [])

            clearing_results[time_slot] = []
            accepted_bids[time_slot] = []
            non_accepted_bids[time_slot] = []

            for request in self.buyer_requests[time_slot]:
                buyer_id = request['id']
                demand = request['requested_power']
                max_price = request['wtp']

                if demand <= 0:
                    continue  # Skip buyers with zero demand

                buyer_bids = [bid for bid in bids_list if bid['buyer_id'] == buyer_id and bid['price'] <= max_price]
                buyer_bids.sort(key=lambda x: x['price'])

                allocations = []
                remaining_demand = demand

                for bid in buyer_bids:
                    baseline_value = self.get_bidder_baseline(bid['bidder_id'], time_slot)
                    actual_value = self.get_bidder_actual(bid['bidder_id'], time_slot)
                    real_flexibility = baseline_value - actual_value

                    allocated_power = min(real_flexibility, remaining_demand)
                    if allocated_power > 0:
                        bid['reward'] = self.calculate_reward(bid['price'], real_flexibility, request['requested_power'])
                        allocations.append({
                            'bidder_id': bid['bidder_id'],
                            'bidded_flexibility': bid['power'],
                            'provided_flexibility': real_flexibility,
                            'allocated_flexibility': allocated_power,
                            'baseline_value': baseline_value,
                            'actual_value': actual_value,
                            'remaining_demand': remaining_demand,
                            'price': bid['price'],
                            'reward': bid['reward']
                        })
                        remaining_demand -= allocated_power
                        accepted_bids[time_slot].append(bid)
                        self.accepted_prices.append(bid['price'])
                        if remaining_demand <= 0:
                            break  # Demand is fully satisfied
                    else:
                        non_accepted_bids[time_slot].append(bid)

                clearing_results[time_slot].append({
                    'buyer_id': buyer_id,
                    'allocations': allocations,
                    'unfulfilled_demand': remaining_demand
                })

            # Add remaining non-accepted bids
            for bid in bids_list:
                if bid not in accepted_bids[time_slot]:
                    non_accepted_bids[time_slot].append(bid)

            # Tag the time slot as cleared
            self.tag_time_slot_as_cleared(time_slot)

        for time_slot, results in clearing_results.items():
            print(f"Clearing results for {time_slot}:")
            for result in results:
                print(result)

        # Append the clearing_results to clearing_results_history
        self.clearing_results_history.update(clearing_results)

        return accepted_bids, non_accepted_bids

    # -------------------------------------------------------------------------
    # (Optional) Helper Methods
    # -------------------------------------------------------------------------
    def get_requests_for_time_slot(self, time_slot):
        """
        Return a list of all Buyer requests for a given time slot.
        """
        return self.buyer_requests.get(time_slot, [])

    def get_bids_for_time_slot(self, time_slot):
        """
        Return a list of all Bidder bids for a given time slot.
        """
        return self.bidder_bids.get(time_slot, [])

    def get_baseline_for_time_slot(self, time_slot, bidder_id):
        """
        Return the baseline value for a given time_slot and bidder_id,
        if it exists.
        """
        if time_slot not in self.time_slot_baseline:
            return None
        return self.time_slot_baseline[time_slot].get(bidder_id, None)

    # -------------------------------------------------------------------------
    # METHODS TO COMPUTE AVERAGE REFERENCE VALUES
    # -------------------------------------------------------------------------
    def compute_power_ref(self):
        """
        Compute and store the average historical power request among all Buyers.

        This method loops through self.buyer_requests for all time slots,
        collects the 'demand' values, and computes the average.
        """
        demands = []
        for time_slot, requests in self.buyer_requests.items():
            for req in requests:
                if "requested_power" in req:
                    demands.append(req["requested_power"])

        if len(demands) > 0:
            self.power_ref = float(sum(demands)) / len(demands)
        else:
            self.power_ref = 0.0

    def compute_price_ref(self):
        """
        Compute and store the average accepted price among all Buyers,
        based on self.time_slot_data.

        This method looks at all accepted bids (those recorded in time_slot_data),
        collects the 'accepted_price' values, and computes the average.
        """
        accepted_prices = []
        for time_slot, accepted_bids in self.time_slot_data.items():
            for bid_info in accepted_bids:
                if "accepted_price" in bid_info:
                    accepted_prices.append(bid_info["accepted_price"])

        if len(accepted_prices) > 0:
            self.price_ref = float(sum(accepted_prices)) / len(accepted_prices)
        else:
            self.price_ref = 0.0

    def average_last_n_requested_powers(self, N):
        """
        Return the average of the last N values in requested_powers.
        If N is greater than the length of requested_powers, return the average of all available values.
        """
        if not self.requested_powers:
            return 0.0
        return sum(self.requested_powers[-N:]) / min(N, len(self.requested_powers))

    def average_last_n_accepted_prices(self, N):
        """
        Return the average of the last N values in accepted_prices.
        If N is greater than the length of accepted_prices, return the average of all available values.
        """
        if not self.accepted_prices:
            return 0.0
        return sum(self.accepted_prices[-N:]) / min(N, len(self.accepted_prices))

    def tag_time_slot_as_cleared(self, time_slot):
        self.cleared_time_slots.add(time_slot)

    def is_time_slot_cleared(self, time_slot):
        return time_slot in self.cleared_time_slots

    def calculate_reward(self, price, real_flexibility, power_requested):
        """
        Calculate the reward for a given accepted bid based on the real flexibility amount and power requested.

        Parameters
        ----------
        price : float
            The price offered by the bidder.
        real_flexibility : float
            The actual flexibility provided by the bidder.
        power_requested : float
            The power requested by the buyer.

        Returns
        -------
        float
            The calculated reward.
        """
        base = min(power_requested, real_flexibility) * price
        penalty = self.alpha_rem * max(power_requested - real_flexibility - (self.threshold_rem * power_requested), 0) * price
        adjustment = self.beta_rem * max(real_flexibility - power_requested - (self.threshold_rem * power_requested), 0) * price

        return base - penalty + adjustment

    def store_bidder_baseline(self, bidder_id, baseline):
        if isinstance(baseline, pd.DataFrame):
            self.bidder_baselines[bidder_id] = baseline
        else:
            raise ValueError("Baseline must be a pandas DataFrame")

    def get_bidder_baseline(self, bidder_id, time_slot):
        if bidder_id in self.bidder_baselines:
            baseline = self.bidder_baselines[bidder_id]
            if time_slot in baseline.index:
                return baseline.loc[time_slot].values[0]
        return 0.0  # Default value if the baseline is not available

    def store_bidder_actual(self, bidder_id, time_slot, actual_value):
        if bidder_id not in self.bidder_actuals:
            self.bidder_actuals[bidder_id] = pd.DataFrame(columns=['actual_value'])
        self.bidder_actuals[bidder_id].loc[time_slot] = actual_value

    def get_bidder_actual(self, bidder_id, time_slot):
        if bidder_id in self.bidder_actuals:
            actuals = self.bidder_actuals[bidder_id]
            if time_slot in actuals.index:
                return actuals.loc[time_slot].values[0]
        return 0.0  # Default value if the actual value is not available