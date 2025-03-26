import unittest
from classes.bidder import Bidder

class TestBidder(unittest.TestCase):

    def setUp(self):
        self.bidder = Bidder(id='bidder1')

    def test_select_buyer_no_buyers(self):
        buyers_info = []
        selected_buyer = self.bidder.select_buyer(buyers_info)
        self.assertIsNone(selected_buyer)

    def test_select_buyer_single_buyer(self):
        buyers_info = [{'id': 'buyer1', 'requested_power': 100, 'wtp': 50}]
        selected_buyer = self.bidder.select_buyer(buyers_info)
        self.assertEqual(selected_buyer['id'], 'buyer1')

    def test_select_buyer_multiple_buyers(self):
        buyers_info = [
            {'id': 'buyer1', 'requested_power': 100, 'wtp': 50},
            {'id': 'buyer2', 'requested_power': 150, 'wtp': 60}
        ]
        selected_buyer = self.bidder.select_buyer(buyers_info)
        self.assertEqual(selected_buyer['id'], 'buyer2')

    def test_select_buyer_with_memory(self):
        self.bidder.update_history('buyer1', '2024-01-01 00:00', 40, 100, True)
        self.bidder.update_history('buyer2', '2024-01-01 00:00', 50, 150, False)
        buyers_info = [
            {'id': 'buyer1', 'requested_power': 100, 'wtp': 50},
            {'id': 'buyer2', 'requested_power': 150, 'wtp': 60}
        ]
        selected_buyer = self.bidder.select_buyer(buyers_info)
        self.assertEqual(selected_buyer['id'], 'buyer1')

    def test_select_buyer_with_long_memory(self):
        # Extend the memory length for this test
        self.bidder.L = 10

        # Update history with more than 7 entries for each buyer
        for i in range(12):
            self.bidder.update_history('buyer1', f'2024-01-01 00:{i:02d}', 40 + i, 100, i % 2 == 0)
            self.bidder.update_history('buyer2', f'2024-01-01 00:{i:02d}', 50 + i, 150, i % 3 == 0)
            self.bidder.update_history('buyer3', f'2024-01-01 00:{i:02d}', 60 + i, 200, i % 4 == 0)
            self.bidder.update_history('buyer4', f'2024-01-01 00:{i:02d}', 70 + i, 250, i % 5 == 0)

        buyers_info = [
            {'id': 'buyer1', 'requested_power': 100, 'wtp': 50},
            {'id': 'buyer2', 'requested_power': 150, 'wtp': 60},
            {'id': 'buyer3', 'requested_power': 200, 'wtp': 70},
            {'id': 'buyer4', 'requested_power': 250, 'wtp': 80}
        ]

        selected_buyer = self.bidder.select_buyer(buyers_info)
        self.assertIsNotNone(selected_buyer)
        self.assertIn(selected_buyer['id'], ['buyer1', 'buyer2', 'buyer3', 'buyer4'])

    def test_select_buyer_with_equal_priority(self):
        buyers_info = [
            {'id': 'buyer1', 'requested_power': 100, 'wtp': 50},
            {'id': 'buyer2', 'requested_power': 100, 'wtp': 50}
        ]
        selected_buyer = self.bidder.select_buyer(buyers_info)
        self.assertIn(selected_buyer['id'], ['buyer1', 'buyer2'])

if __name__ == '__main__':
    unittest.main()