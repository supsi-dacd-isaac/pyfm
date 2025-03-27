class Bidder:
    """
    A class representing a Bidder in a pay-as-bid market, with an adaptive strategy
    that uses memory, incremental price adjustments, and baseline consideration.
    """

    def __init__(self, id, alpha=0.05, beta=0.05, gamma=0.5, L=7, w1=1.0, w2=1.0, w3=1.0,
                 baseline=0.10, pow_req_ref=100.0, avg_acc_ref=50.0):
        """
        Initialize the bidder with the given parameters.

        :param alpha: Increment added to the price when the last offer was accepted
        :param beta: Decrement subtracted from the price when the last offer was rejected
        :param gamma: Factor to scale the alpha/beta adjustments (dampening factor)
        :param L: Number of past offers stored in memory (history length per Buyer)
        :param w1: Weight for the requested power in the priority calculation
        :param w2: Weight for the average accepted price in the priority calculation
        :param w3: Weight for penalizing a low success ratio in the priority calculation
        :param baseline: Time-series dataset for the bidder baseline.
        """
        self.id = id
        self.alpha = alpha
        self.beta = beta
        self.gamma = gamma
        self.L = L
        self.w1 = w1
        self.w2 = w2
        self.w3 = w3
        self.baseline = baseline if baseline is not None else pd.DataFrame()
        self.pow_req_ref = pow_req_ref
        self.avg_acc_ref = avg_acc_ref
        self.memory = {}

        self.current_bidding = {}

    def update_history(self, buyer_id, time_slot, offered_price, offered_power, accepted):
        """
        Store the result of an offer in the memory of this bidder.

        :param buyer_id: Identifier of the Buyer
        :param time_slot: The time (or slot) of the market session
        :param offered_price: Price offered to the Buyer
        :param offered_power: Power offered to the Buyer
        :param accepted: Boolean indicating whether the offer was accepted (reward) or not
        """
        if buyer_id not in self.memory:
            self.memory[buyer_id] = []

        # Append this record
        self.memory[buyer_id].append({
            "time": time_slot,
            "price": offered_price,
            "power": offered_power,
            "accepted": accepted
        })

        # Keep only the last L entries to avoid unbounded growth
        if len(self.memory[buyer_id]) > self.L:
            self.memory[buyer_id].pop(0)

    def get_buyer_stats(self, buyer_id):
        """
        Compute useful statistics for a specific Buyer based on the stored memory.
        Returns:
            - average_accepted_price: Mean of accepted prices (if any)
            - average_rejected_price: Mean of rejected prices (if any)
            - success_ratio: Ratio of accepted to total offers
            - p_min: Minimum accepted price
            - p_max: Maximum accepted price
        """
        if buyer_id not in self.memory or len(self.memory[buyer_id]) == 0:
            # Default values if no history
            return 0.0, 0.0, 0.0, 0.0, 0.0

        records = self.memory[buyer_id]
        accepted_prices = [r["price"] for r in records if r["accepted"]]
        rejected_prices = [r["price"] for r in records if not r["accepted"]]

        # Compute average accepted price
        avg_acc = sum(accepted_prices) / len(accepted_prices) if len(accepted_prices) > 0 else 0.0
        # Compute average rejected price
        avg_rej = sum(rejected_prices) / len(rejected_prices) if len(rejected_prices) > 0 else 0.0
        # Compute success ratio
        success_ratio = len(accepted_prices) / len(records)
        # Compute min and max of accepted prices
        p_min = min(accepted_prices) if len(accepted_prices) > 0 else 0.0
        p_max = max(accepted_prices) if len(accepted_prices) > 0 else 0.0

        return avg_acc, avg_rej, success_ratio, p_min, p_max

    def compute_priority(self, buyer_id, pow_req, wtp):
        """
        Compute the priority for serving a specific Buyer.

        :param buyer_id: Identifier of the Buyer
        :param pow_req: The requested power from this Buyer
        :return: A numeric priority score
        """
        avg_acc, _, success_ratio, _, _ = self.get_buyer_stats(buyer_id)
        normalized_pow_req = pow_req / self.pow_req_ref
        normalized_avg_acc = avg_acc / self.avg_acc_ref

        # Priority function:
        pi_j = (self.w1 * normalized_pow_req) + (self.w2 * normalized_avg_acc) - (self.w3 * (1.0 - success_ratio))
        return pi_j

    def select_buyer(self, buyers_info):
        """
        Select the best Buyer to serve based on a priority measure.

        :param buyers_info: A list of dict containing:
                               [
                                 {
                                  "id": buyer_id,
                                  "requested_power": float,
                                  "wtp": float
                                 },
                                 ...
                               ]
        :return: A dict representing the best Buyer according to the computed priority,
                 or None if the list is empty.
        """
        if not buyers_info:
            return None

        best_buyer = None
        best_priority = float("-inf")

        for buyer_info in buyers_info:
            prio = self.compute_priority(buyer_info["id"], buyer_info["requested_power"], buyer_info["wtp"])
            if prio > best_priority:
                best_priority = prio
                best_buyer = buyer_info

        return best_buyer

    def build_offer(self, buyer_id, pow_req, pow_bid):
        """
        Build an offer (price, final power) for the selected Buyer based on the extended strategy.

        :param buyer_id: Identifier of the Buyer
        :param pow_req: The power requested by this Buyer
        :param pow_bid: Maximum power the Bidder can actually offer (must be <= pow_base)
        :return: (offer_price, offer_power)
        """
        # Retrieve Buyer-specific stats
        avg_acc, avg_rej, success_ratio, p_min, p_max = self.get_buyer_stats(buyer_id)

        # Start price (p_start) could be an average accepted price if available
        if avg_acc > 0:
            p_start = avg_acc
        else:
            # If we have no accepted offers or no history, fallback to an average of rejected
            # or a default
            p_start = avg_rej if avg_rej > 0 else 1.0  # default fallback

        # Adjust price based on success ratio
        if success_ratio > 0.7:
            p_start *= (1.0 + self.gamma * 0.5)
        elif success_ratio < 0.3:
            p_start *= (1.0 - self.gamma * 0.5)

        # Check if we have a record from the last known offer to this buyer
        last_record = self.get_last_record(buyer_id)
        if last_record is not None:
            if last_record["accepted"]:
                p_start += self.gamma * self.alpha
            else:
                p_start -= self.gamma * self.beta

        # Make sure we don't go negative or zero
        p_start = max(0.01, p_start)

        # Decide the power to offer:
        # If the requested power is bigger than pow_bid, we can only offer pow_bid
        # Otherwise, offer exactly the requested power
        final_power = min(pow_req, pow_bid)

        return p_start, final_power

    def get_last_record(self, buyer_id):
        """
        Retrieve the most recent record from memory for a given Buyer.

        :param buyer_id: Identifier of the Buyer
        :return: The latest record dict or None if not available
        """
        if buyer_id not in self.memory or len(self.memory[buyer_id]) == 0:
            return None
        return self.memory[buyer_id][-1]

    def update_current_bidding(self, buyer_id, offered_power, offered_price):
        """
        Update current bidding.
        :param buyer_id: Identifier of the Buyer
        :param offered_power: Power offered to the Buyer
        :param offered_price: Price offered to the Buyer
        """
        self.current_bidding = {
            "bidder_id": self.id,
            "buyer_id": buyer_id,
            "power": offered_power,
            "price": offered_price,
        }

    def set_reference_values(self, pow_req_ref, avg_acc_ref):
        """
        Update set_reference_values for priority calculation.
        :param pow_req_ref: Power requested
        :param avg_acc_ref: Price accepted
        """
        if pow_req_ref != 0.0:
            self.pow_req_ref = pow_req_ref
        if avg_acc_ref != 0.0:
            self.avg_acc_ref = avg_acc_ref