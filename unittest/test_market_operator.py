import unittest
from classes.market_operator import MarketOperator

class TestMarketOperator(unittest.TestCase):

    def setUp(self):
        self.market_operator = MarketOperator(alpha_rem=0.1, beta_rem=0.1, threshold_rem=0.1, power_ref=0.0, price_ref=0.0)

    def test_receive_buyer_request(self):
        self.market_operator.receive_buyer_request('2024-01-01 00:00', {'id': 'buyer1', 'requested_power': 100, 'wtp': 50})
        self.assertIn('2024-01-01 00:00', self.market_operator.buyer_requests)
        self.assertEqual(len(self.market_operator.buyer_requests['2024-01-01 00:00']), 1)

    def test_receive_bid_from_bidder(self):
        self.market_operator.receive_bid_from_bidder('2024-01-01 00:00', {'bidder_id': 'bidder1', 'buyer_id': 'buyer1', 'power': 50, 'price': 40})
        self.assertIn('2024-01-01 00:00', self.market_operator.bidder_bids)
        self.assertEqual(len(self.market_operator.bidder_bids['2024-01-01 00:00']), 1)

    def test_receive_baseline_from_bidder(self):
        self.market_operator.receive_baseline_from_bidder('2024-01-01 00:00', 'bidder1', 100)
        self.assertIn('2024-01-01 00:00', self.market_operator.time_slot_baseline)
        self.assertIn('bidder1', self.market_operator.time_slot_baseline['2024-01-01 00:00'])
        self.assertEqual(self.market_operator.time_slot_baseline['2024-01-01 00:00']['bidder1'], 100)

    def test_get_requests_for_time_slot(self):
        self.market_operator.receive_buyer_request('2024-01-01 00:00', {'id': 'buyer1', 'requested_power': 100, 'wtp': 50})
        requests = self.market_operator.get_requests_for_time_slot('2024-01-01 00:00')
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]['id'], 'buyer1')

    def test_get_bids_for_time_slot(self):
        self.market_operator.receive_bid_from_bidder('2024-01-01 00:00', {'bidder_id': 'bidder1', 'buyer_id': 'buyer1', 'power': 50, 'price': 40})
        bids = self.market_operator.get_bids_for_time_slot('2024-01-01 00:00')
        self.assertEqual(len(bids), 1)
        self.assertEqual(bids[0]['bidder_id'], 'bidder1')

    def test_get_baseline_for_time_slot(self):
        self.market_operator.receive_baseline_from_bidder('2024-01-01 00:00', 'bidder1', 100)
        baseline = self.market_operator.get_baseline_for_time_slot('2024-01-01 00:00', 'bidder1')
        self.assertEqual(baseline, 100)

    def test_compute_power_ref(self):
        self.market_operator.receive_buyer_request('2024-01-01 00:00', {'id': 'buyer1', 'requested_power': 100, 'wtp': 50})
        self.market_operator.receive_buyer_request('2024-01-01 01:00', {'id': 'buyer2', 'requested_power': 200, 'wtp': 60})
        self.market_operator.compute_power_ref()
        self.assertEqual(self.market_operator.power_ref, 150.0)

    def test_compute_price_ref(self):
        self.market_operator.time_slot_data = {
            '2024-01-01 00:00': [{'accepted_price': 40}, {'accepted_price': 50}],
            '2024-01-01 01:00': [{'accepted_price': 60}]
        }
        self.market_operator.compute_price_ref()
        self.assertEqual(self.market_operator.price_ref, 50.0)

    def test_no_demand(self):
        self.market_operator.receive_buyer_request('2024-01-01 00:00', {'id': 'buyer1', 'requested_power': 0, 'wtp': 50})
        accepted_bids, non_accepted_bids = self.market_operator.pay_as_bid_market_solving()
        self.assertEqual(len(accepted_bids['2024-01-01 00:00']), 0)
        self.assertEqual(len(non_accepted_bids['2024-01-01 00:00']), 0)

    def test_exceeding_wtp(self):
        self.market_operator.receive_buyer_request('2024-01-01 00:00', {'id': 'buyer1', 'requested_power': 100, 'wtp': 50})
        self.market_operator.receive_bid_from_bidder('2024-01-01 00:00', {'bidder_id': 'bidder1', 'buyer_id': 'buyer1', 'power': 50, 'price': 60})
        accepted_bids, non_accepted_bids = self.market_operator.pay_as_bid_market_solving()
        self.assertEqual(len(accepted_bids['2024-01-01 00:00']), 0)
        self.assertEqual(len(non_accepted_bids['2024-01-01 00:00']), 1)

    def test_partial_fulfillment(self):
        self.market_operator.receive_buyer_request('2024-01-01 00:00', {'id': 'buyer1', 'requested_power': 100, 'wtp': 50})
        self.market_operator.receive_bid_from_bidder('2024-01-01 00:00', {'bidder_id': 'bidder1', 'buyer_id': 'buyer1', 'power': 50, 'price': 40})
        accepted_bids, non_accepted_bids = self.market_operator.pay_as_bid_market_solving()
        self.assertEqual(len(accepted_bids['2024-01-01 00:00']), 1)
        self.assertEqual(len(non_accepted_bids['2024-01-01 00:00']), 0)

    def test_full_fulfillment(self):
        self.market_operator.receive_buyer_request('2024-01-01 00:00', {'id': 'buyer1', 'requested_power': 100, 'wtp': 50})
        self.market_operator.receive_bid_from_bidder('2024-01-01 00:00', {'bidder_id': 'bidder1', 'buyer_id': 'buyer1', 'power': 50, 'price': 40})
        self.market_operator.receive_bid_from_bidder('2024-01-01 00:00', {'bidder_id': 'bidder2', 'buyer_id': 'buyer1', 'power': 50, 'price': 45})
        accepted_bids, non_accepted_bids = self.market_operator.pay_as_bid_market_solving()
        self.assertEqual(len(accepted_bids['2024-01-01 00:00']), 2)
        self.assertEqual(len(non_accepted_bids['2024-01-01 00:00']), 0)

    def test_multiple_buyers_bidders(self):
        self.market_operator.receive_buyer_request('2024-01-01 00:00', {'id': 'buyer1', 'requested_power': 100, 'wtp': 50})
        self.market_operator.receive_buyer_request('2024-01-01 00:00', {'id': 'buyer2', 'requested_power': 150, 'wtp': 60})
        self.market_operator.receive_bid_from_bidder('2024-01-01 00:00', {'bidder_id': 'bidder1', 'buyer_id': 'buyer1', 'power': 50, 'price': 40})
        self.market_operator.receive_bid_from_bidder('2024-01-01 00:00', {'bidder_id': 'bidder2', 'buyer_id': 'buyer1', 'power': 60, 'price': 45})
        self.market_operator.receive_bid_from_bidder('2024-01-01 00:00', {'bidder_id': 'bidder3', 'buyer_id': 'buyer2', 'power': 100, 'price': 55})
        self.market_operator.receive_bid_from_bidder('2024-01-01 00:00', {'bidder_id': 'bidder4', 'buyer_id': 'buyer2', 'power': 60, 'price': 50})
        accepted_bids, non_accepted_bids = self.market_operator.pay_as_bid_market_solving()
        self.assertEqual(len(accepted_bids['2024-01-01 00:00']), 4)
        self.assertEqual(len(non_accepted_bids['2024-01-01 00:00']), 0)

    def test_multiple_buyers_bidders_with_no_accepted_bids(self):
        self.market_operator.receive_buyer_request('2024-01-01 00:00', {'id': 'buyer1', 'requested_power': 100, 'wtp': 50})
        self.market_operator.receive_buyer_request('2024-01-01 00:00', {'id': 'buyer2', 'requested_power': 150, 'wtp': 60})
        self.market_operator.receive_bid_from_bidder('2024-01-01 00:00', {'bidder_id': 'bidder1', 'buyer_id': 'buyer1', 'power': 50, 'price': 55})
        self.market_operator.receive_bid_from_bidder('2024-01-01 00:00', {'bidder_id': 'bidder2', 'buyer_id': 'buyer1', 'power': 60, 'price': 65})
        self.market_operator.receive_bid_from_bidder('2024-01-01 00:00', {'bidder_id': 'bidder3', 'buyer_id': 'buyer2', 'power': 100, 'price': 70})
        self.market_operator.receive_bid_from_bidder('2024-01-01 00:00', {'bidder_id': 'bidder4', 'buyer_id': 'buyer2', 'power': 60, 'price': 75})
        accepted_bids, non_accepted_bids = self.market_operator.pay_as_bid_market_solving()
        self.assertEqual(len(accepted_bids['2024-01-01 00:00']), 0)
        self.assertEqual(len(non_accepted_bids['2024-01-01 00:00']), 4)

    def test_multiple_buyers_bidders_with_mixed_acceptance(self):
        self.market_operator.receive_buyer_request('2024-01-01 00:00', {'id': 'buyer1', 'requested_power': 100, 'wtp': 50})
        self.market_operator.receive_buyer_request('2024-01-01 00:00', {'id': 'buyer2', 'requested_power': 150, 'wtp': 60})
        self.market_operator.receive_bid_from_bidder('2024-01-01 00:00', {'bidder_id': 'bidder1', 'buyer_id': 'buyer1', 'power': 50, 'price': 40})
        self.market_operator.receive_bid_from_bidder('2024-01-01 00:00', {'bidder_id': 'bidder2', 'buyer_id': 'buyer1', 'power': 60, 'price': 55})
        self.market_operator.receive_bid_from_bidder('2024-01-01 00:00', {'bidder_id': 'bidder3', 'buyer_id': 'buyer2', 'power': 100, 'price': 55})
        self.market_operator.receive_bid_from_bidder('2024-01-01 00:00', {'bidder_id': 'bidder4', 'buyer_id': 'buyer2', 'power': 60, 'price': 65})
        accepted_bids, non_accepted_bids = self.market_operator.pay_as_bid_market_solving()
        self.assertEqual(len(accepted_bids['2024-01-01 00:00']), 2)
        self.assertEqual(len(non_accepted_bids['2024-01-01 00:00']), 2)

    def test_no_bids(self):
        self.market_operator.receive_buyer_request('2024-01-01 00:00', {'id': 'buyer1', 'requested_power': 100, 'wtp': 50})
        accepted_bids, non_accepted_bids = self.market_operator.pay_as_bid_market_solving()
        self.assertEqual(len(accepted_bids['2024-01-01 00:00']), 0)
        self.assertEqual(len(non_accepted_bids['2024-01-01 00:00']), 0)

    def test_zero_wtp(self):
        self.market_operator.receive_buyer_request('2024-01-01 00:00', {'id': 'buyer1', 'requested_power': 100, 'wtp': 0})
        self.market_operator.receive_bid_from_bidder('2024-01-01 00:00', {'bidder_id': 'bidder1', 'buyer_id': 'buyer1', 'power': 50, 'price': 40})
        accepted_bids, non_accepted_bids = self.market_operator.pay_as_bid_market_solving()
        self.assertEqual(len(accepted_bids['2024-01-01 00:00']), 0)
        self.assertEqual(len(non_accepted_bids['2024-01-01 00:00']), 1)

if __name__ == '__main__':
    unittest.main()