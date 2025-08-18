import unittest
import pandas as pd
from datetime import datetime
from classes.market_operator import MarketOperator


class TestMarketOperator(unittest.TestCase):
    def setUp(self):
        self.market_operator = MarketOperator(
            alpha_rem=0.1, beta_rem=0.1, threshold_rem=0.1, power_ref=0.0, price_ref=0.0
        )
        self.time_slot = datetime(2025, 1, 1, 0, 0)
        self.bid = {
            "price": 50.0,
            "bidder_id": "BIDDER_01",
            "buyer_id": "BUYER_01",
            "power": 10.0,
        }
        self.real_flexibility = 8.0
        self.power_requested = 10.0

    def test_receive_buyer_request(self):
        self.market_operator.receive_buyer_request(
            self.time_slot, {"id": "buyer1", "requested_power": 100, "wtp": 50}
        )
        self.assertIn(self.time_slot, self.market_operator.buyer_requests)
        self.assertEqual(len(self.market_operator.buyer_requests[self.time_slot]), 1)

    def test_receive_bid_from_bidder(self):
        self.market_operator.receive_bid_from_bidder(
            self.time_slot,
            {"bidder_id": "bidder1", "buyer_id": "buyer1", "power": 50, "price": 40},
        )
        self.assertIn(self.time_slot, self.market_operator.bidder_bids)
        self.assertEqual(len(self.market_operator.bidder_bids[self.time_slot]), 1)

    def test_receive_baseline_from_bidder(self):
        self.market_operator.receive_baseline_from_bidder(
            self.time_slot, "bidder1", 100
        )
        self.assertIn(self.time_slot, self.market_operator.time_slot_baseline)
        self.assertIn(
            "bidder1", self.market_operator.time_slot_baseline[self.time_slot]
        )
        self.assertEqual(
            self.market_operator.time_slot_baseline[self.time_slot]["bidder1"], 100
        )

    def test_get_requests_for_time_slot(self):
        self.market_operator.receive_buyer_request(
            self.time_slot, {"id": "buyer1", "requested_power": 100, "wtp": 50}
        )
        requests = self.market_operator.get_requests_for_time_slot(self.time_slot)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["id"], "buyer1")

    def test_get_bids_for_time_slot(self):
        self.market_operator.receive_bid_from_bidder(
            self.time_slot,
            {"bidder_id": "bidder1", "buyer_id": "buyer1", "power": 50, "price": 40},
        )
        bids = self.market_operator.get_bids_for_time_slot(self.time_slot)
        self.assertEqual(len(bids), 1)
        self.assertEqual(bids[0]["bidder_id"], "bidder1")

    def test_get_baseline_for_time_slot(self):
        self.market_operator.receive_baseline_from_bidder(
            self.time_slot, "bidder1", 100
        )
        baseline = self.market_operator.get_baseline_for_time_slot(
            self.time_slot, "bidder1"
        )
        self.assertEqual(baseline, 100)

    def test_compute_power_ref(self):
        self.market_operator.receive_buyer_request(
            self.time_slot, {"id": "buyer1", "requested_power": 100, "wtp": 50}
        )
        self.market_operator.receive_buyer_request(
            self.time_slot, {"id": "buyer2", "requested_power": 200, "wtp": 60}
        )
        self.market_operator.compute_power_ref()
        self.assertEqual(self.market_operator.power_ref, 150.0)

    def test_compute_price_ref(self):
        self.market_operator.time_slot_data = {
            self.time_slot: [
                {"accepted_price": 40},
                {"accepted_price": 50},
                {"accepted_price": 60},
            ]
        }
        self.market_operator.compute_price_ref()
        self.assertEqual(self.market_operator.price_ref, 50.0)

    def test_no_demand(self):
        self.market_operator.receive_buyer_request(
            self.time_slot, {"id": "buyer1", "requested_power": 0, "wtp": 50}
        )
        (
            accepted_bids,
            non_accepted_bids,
        ) = self.market_operator.pay_as_bid_market_solving()
        self.assertEqual(len(accepted_bids[self.time_slot]), 0)
        self.assertEqual(len(non_accepted_bids[self.time_slot]), 0)

    def test_exceeding_wtp(self):
        self.market_operator.receive_buyer_request(
            self.time_slot, {"id": "buyer1", "requested_power": 100, "wtp": 50}
        )
        self.market_operator.receive_bid_from_bidder(
            self.time_slot,
            {"bidder_id": "bidder1", "buyer_id": "buyer1", "power": 50, "price": 60},
        )
        (
            accepted_bids,
            non_accepted_bids,
        ) = self.market_operator.pay_as_bid_market_solving()
        self.assertEqual(len(accepted_bids[self.time_slot]), 0)
        self.assertEqual(len(non_accepted_bids[self.time_slot]), 1)

    def test_partial_fulfillment(self):
        self.market_operator.receive_buyer_request(
            self.time_slot, {"id": "buyer1", "requested_power": 100, "wtp": 50}
        )
        self.market_operator.receive_bid_from_bidder(
            self.time_slot,
            {"bidder_id": "bidder1", "buyer_id": "buyer1", "power": 50, "price": 40},
        )

        # Store baseline and actual values for the bidder
        self.market_operator.store_bidder_baseline(
            "bidder1", pd.DataFrame({"value": [100]}, index=[self.time_slot])
        )
        self.market_operator.store_bidder_actual("bidder1", self.time_slot, 90)

        (
            accepted_bids,
            non_accepted_bids,
        ) = self.market_operator.pay_as_bid_market_solving()
        self.assertEqual(len(accepted_bids[self.time_slot]), 1)
        self.assertEqual(len(non_accepted_bids[self.time_slot]), 0)

    def test_full_fulfillment(self):
        self.market_operator.receive_buyer_request(
            self.time_slot, {"id": "buyer1", "requested_power": 100, "wtp": 50}
        )
        self.market_operator.receive_bid_from_bidder(
            self.time_slot,
            {"bidder_id": "bidder1", "buyer_id": "buyer1", "power": 50, "price": 40},
        )
        self.market_operator.receive_bid_from_bidder(
            self.time_slot,
            {"bidder_id": "bidder2", "buyer_id": "buyer1", "power": 50, "price": 45},
        )

        # Store baseline and actual values for the bidders
        self.market_operator.store_bidder_baseline(
            "bidder1", pd.DataFrame({"value": [100]}, index=[self.time_slot])
        )
        self.market_operator.store_bidder_baseline(
            "bidder2", pd.DataFrame({"value": [100]}, index=[self.time_slot])
        )
        self.market_operator.store_bidder_actual("bidder1", self.time_slot, 90)
        self.market_operator.store_bidder_actual("bidder2", self.time_slot, 90)

        (
            accepted_bids,
            non_accepted_bids,
        ) = self.market_operator.pay_as_bid_market_solving()
        self.assertEqual(len(accepted_bids[self.time_slot]), 2)
        self.assertEqual(len(non_accepted_bids[self.time_slot]), 0)

    def test_multiple_buyers_bidders(self):
        self.market_operator.receive_buyer_request(
            self.time_slot, {"id": "buyer1", "requested_power": 100, "wtp": 50}
        )
        self.market_operator.receive_buyer_request(
            self.time_slot, {"id": "buyer2", "requested_power": 150, "wtp": 60}
        )
        self.market_operator.receive_bid_from_bidder(
            self.time_slot,
            {"bidder_id": "bidder1", "buyer_id": "buyer1", "power": 50, "price": 40},
        )
        self.market_operator.receive_bid_from_bidder(
            self.time_slot,
            {"bidder_id": "bidder2", "buyer_id": "buyer1", "power": 60, "price": 45},
        )
        self.market_operator.receive_bid_from_bidder(
            self.time_slot,
            {"bidder_id": "bidder3", "buyer_id": "buyer2", "power": 100, "price": 55},
        )
        self.market_operator.receive_bid_from_bidder(
            self.time_slot,
            {"bidder_id": "bidder4", "buyer_id": "buyer2", "power": 60, "price": 50},
        )

        # Store baseline and actual values for the bidders
        self.market_operator.store_bidder_baseline(
            "bidder1", pd.DataFrame({"value": [100]}, index=[self.time_slot])
        )
        self.market_operator.store_bidder_baseline(
            "bidder2", pd.DataFrame({"value": [100]}, index=[self.time_slot])
        )
        self.market_operator.store_bidder_baseline(
            "bidder3", pd.DataFrame({"value": [100]}, index=[self.time_slot])
        )
        self.market_operator.store_bidder_baseline(
            "bidder4", pd.DataFrame({"value": [100]}, index=[self.time_slot])
        )
        self.market_operator.store_bidder_actual("bidder1", self.time_slot, 90)
        self.market_operator.store_bidder_actual("bidder2", self.time_slot, 90)
        self.market_operator.store_bidder_actual("bidder3", self.time_slot, 90)
        self.market_operator.store_bidder_actual("bidder4", self.time_slot, 90)

        (
            accepted_bids,
            non_accepted_bids,
        ) = self.market_operator.pay_as_bid_market_solving()
        self.assertEqual(len(accepted_bids[self.time_slot]), 4)
        self.assertEqual(len(non_accepted_bids[self.time_slot]), 0)

    def test_multiple_buyers_bidders_with_no_accepted_bids(self):
        self.market_operator.receive_buyer_request(
            self.time_slot, {"id": "buyer1", "requested_power": 100, "wtp": 50}
        )
        self.market_operator.receive_buyer_request(
            self.time_slot, {"id": "buyer2", "requested_power": 150, "wtp": 60}
        )
        self.market_operator.receive_bid_from_bidder(
            self.time_slot,
            {"bidder_id": "bidder1", "buyer_id": "buyer1", "power": 50, "price": 55},
        )
        self.market_operator.receive_bid_from_bidder(
            self.time_slot,
            {"bidder_id": "bidder2", "buyer_id": "buyer1", "power": 60, "price": 65},
        )
        self.market_operator.receive_bid_from_bidder(
            self.time_slot,
            {"bidder_id": "bidder3", "buyer_id": "buyer2", "power": 100, "price": 70},
        )
        self.market_operator.receive_bid_from_bidder(
            self.time_slot,
            {"bidder_id": "bidder4", "buyer_id": "buyer2", "power": 60, "price": 75},
        )

        # Store baseline and actual values for the bidders
        self.market_operator.store_bidder_baseline(
            "bidder1", pd.DataFrame({"value": [100]}, index=[self.time_slot])
        )
        self.market_operator.store_bidder_baseline(
            "bidder2", pd.DataFrame({"value": [100]}, index=[self.time_slot])
        )
        self.market_operator.store_bidder_baseline(
            "bidder3", pd.DataFrame({"value": [100]}, index=[self.time_slot])
        )
        self.market_operator.store_bidder_baseline(
            "bidder4", pd.DataFrame({"value": [100]}, index=[self.time_slot])
        )
        self.market_operator.store_bidder_actual("bidder1", self.time_slot, 90)
        self.market_operator.store_bidder_actual("bidder2", self.time_slot, 90)
        self.market_operator.store_bidder_actual("bidder3", self.time_slot, 90)
        self.market_operator.store_bidder_actual("bidder4", self.time_slot, 90)

        (
            accepted_bids,
            non_accepted_bids,
        ) = self.market_operator.pay_as_bid_market_solving()
        self.assertEqual(len(accepted_bids[self.time_slot]), 0)
        self.assertEqual(len(non_accepted_bids[self.time_slot]), 4)

    def test_multiple_buyers_bidders_with_mixed_acceptance(self):
        self.market_operator.receive_buyer_request(
            self.time_slot, {"id": "buyer1", "requested_power": 100, "wtp": 50}
        )
        self.market_operator.receive_buyer_request(
            self.time_slot, {"id": "buyer2", "requested_power": 150, "wtp": 60}
        )
        self.market_operator.receive_bid_from_bidder(
            self.time_slot,
            {"bidder_id": "bidder1", "buyer_id": "buyer1", "power": 50, "price": 40},
        )
        self.market_operator.receive_bid_from_bidder(
            self.time_slot,
            {"bidder_id": "bidder2", "buyer_id": "buyer1", "power": 60, "price": 55},
        )
        self.market_operator.receive_bid_from_bidder(
            self.time_slot,
            {"bidder_id": "bidder3", "buyer_id": "buyer2", "power": 100, "price": 55},
        )
        self.market_operator.receive_bid_from_bidder(
            self.time_slot,
            {"bidder_id": "bidder4", "buyer_id": "buyer2", "power": 60, "price": 65},
        )

        # Store baseline and actual values for the bidders
        self.market_operator.store_bidder_baseline(
            "bidder1", pd.DataFrame({"value": [100]}, index=[self.time_slot])
        )
        self.market_operator.store_bidder_baseline(
            "bidder2", pd.DataFrame({"value": [100]}, index=[self.time_slot])
        )
        self.market_operator.store_bidder_baseline(
            "bidder3", pd.DataFrame({"value": [100]}, index=[self.time_slot])
        )
        self.market_operator.store_bidder_baseline(
            "bidder4", pd.DataFrame({"value": [100]}, index=[self.time_slot])
        )
        self.market_operator.store_bidder_actual("bidder1", self.time_slot, 90)
        self.market_operator.store_bidder_actual("bidder2", self.time_slot, 90)
        self.market_operator.store_bidder_actual("bidder3", self.time_slot, 90)
        self.market_operator.store_bidder_actual("bidder4", self.time_slot, 90)

        (
            accepted_bids,
            non_accepted_bids,
        ) = self.market_operator.pay_as_bid_market_solving()
        self.assertEqual(len(accepted_bids[self.time_slot]), 2)
        self.assertEqual(len(non_accepted_bids[self.time_slot]), 2)

    def test_no_bids(self):
        self.market_operator.receive_buyer_request(
            self.time_slot, {"id": "buyer1", "requested_power": 100, "wtp": 50}
        )
        (
            accepted_bids,
            non_accepted_bids,
        ) = self.market_operator.pay_as_bid_market_solving()
        self.assertEqual(len(accepted_bids[self.time_slot]), 0)
        self.assertEqual(len(non_accepted_bids[self.time_slot]), 0)

    def test_zero_wtp(self):
        self.market_operator.receive_buyer_request(
            self.time_slot, {"id": "buyer1", "requested_power": 100, "wtp": 0}
        )
        self.market_operator.receive_bid_from_bidder(
            self.time_slot,
            {"bidder_id": "bidder1", "buyer_id": "buyer1", "power": 50, "price": 40},
        )

        # Store baseline and actual values for the bidder
        self.market_operator.store_bidder_baseline(
            "bidder1", pd.DataFrame({"value": [100]}, index=[self.time_slot])
        )
        self.market_operator.store_bidder_actual("bidder1", self.time_slot, 90)

        (
            accepted_bids,
            non_accepted_bids,
        ) = self.market_operator.pay_as_bid_market_solving()
        self.assertEqual(len(accepted_bids[self.time_slot]), 0)
        self.assertEqual(len(non_accepted_bids[self.time_slot]), 1)

    def test_store_and_get_bidder_baseline(self):
        baseline = pd.DataFrame({"value": [1.0]}, index=[self.time_slot])
        self.market_operator.store_bidder_baseline("BIDDER_01", baseline)
        self.assertEqual(
            self.market_operator.get_bidder_baseline("BIDDER_01", self.time_slot), 1.0
        )

    def test_store_and_get_bidder_actual(self):
        actual_value = 0.85
        self.market_operator.store_bidder_actual(
            "BIDDER_01", self.time_slot, actual_value
        )
        self.assertEqual(
            self.market_operator.get_bidder_actual("BIDDER_01", self.time_slot),
            actual_value,
        )

    def test_calculate_reward(self):
        price = self.bid["price"]
        reward = self.market_operator.calculate_reward(
            price, self.real_flexibility, self.power_requested
        )
        expected_reward = (
            min(self.power_requested, self.real_flexibility) * price
            - self.market_operator.alpha_rem
            * max(
                self.power_requested
                - self.real_flexibility
                - (self.market_operator.threshold_rem * self.power_requested),
                0,
            )
            * price
            + self.market_operator.beta_rem
            * max(
                self.real_flexibility
                - self.power_requested
                - (self.market_operator.threshold_rem * self.power_requested),
                0,
            )
            * price
        )
        self.assertAlmostEqual(reward, expected_reward)


if __name__ == "__main__":
    unittest.main()
