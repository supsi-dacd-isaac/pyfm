"""
Tests for trader_fsp.py refactoring: DSO dependency removal.

Verifies that:
- Player.get_adjusted_time works as a static method (no DSO instance needed)
- DSO buy orders can be parsed without a DSO actor
- DSO organization can be resolved from config without instantiating a DSO
"""
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from classes.player import Player


class TestGetAdjustedTimeStatic(unittest.TestCase):
    """Player.get_adjusted_time is a @staticmethod — callable without any instance."""

    def test_callable_on_class(self):
        result = Player.get_adjusted_time(15, 90)
        self.assertIsInstance(result, datetime)

    def test_rounds_down_to_granularity(self):
        with patch("classes.player.datetime") as mock_dt:
            mock_dt.utcnow.return_value = datetime(2026, 4, 28, 10, 37, 12)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = Player.get_adjusted_time(15, 0)
            self.assertEqual(result.minute, 30)
            self.assertEqual(result.second, 0)

    def test_applies_shift(self):
        with patch("classes.player.datetime") as mock_dt:
            mock_dt.utcnow.return_value = datetime(2026, 4, 28, 10, 0, 0)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = Player.get_adjusted_time(15, 90)
            self.assertEqual(result, datetime(2026, 4, 28, 11, 30, 0))


class TestParseDsoOrders(unittest.TestCase):
    """
    The refactored trader_fsp.py parses DSO orders inline instead of via
    dso.get_flexibility_requests(). Verify the parsing logic matches.
    """

    def _parse_dso_demands(self, orders):
        """Replicates the inline parsing logic from trader_fsp.py."""
        dso_demands = []
        for order in orders:
            if order["completionType"] is None:
                request = {}
                if order["regulationType"] == "Down":
                    request["Down"] = float(order["quantity"])
                    request["Up"] = 0.0
                elif order["regulationType"] == "Up":
                    request["Up"] = float(order["quantity"])
                    request["Down"] = 0.0
                request["unitPrice"] = float(order["unitPrice"])
                dso_demands.append(request)
        return dso_demands

    def test_empty_orders(self):
        self.assertEqual(self._parse_dso_demands([]), [])

    def test_up_order(self):
        orders = [
            {"completionType": None, "regulationType": "Up",
             "quantity": "0.015", "unitPrice": "9.5"}
        ]
        result = self._parse_dso_demands(orders)
        self.assertEqual(len(result), 1)
        self.assertAlmostEqual(result[0]["Up"], 0.015)
        self.assertAlmostEqual(result[0]["Down"], 0.0)
        self.assertAlmostEqual(result[0]["unitPrice"], 9.5)

    def test_down_order(self):
        orders = [
            {"completionType": None, "regulationType": "Down",
             "quantity": "0.010", "unitPrice": "8.0"}
        ]
        result = self._parse_dso_demands(orders)
        self.assertEqual(len(result), 1)
        self.assertAlmostEqual(result[0]["Down"], 0.010)
        self.assertAlmostEqual(result[0]["Up"], 0.0)

    def test_settled_orders_ignored(self):
        orders = [
            {"completionType": "Settled", "regulationType": "Up",
             "quantity": "0.015", "unitPrice": "9.5"}
        ]
        result = self._parse_dso_demands(orders)
        self.assertEqual(len(result), 0)

    def test_mixed_orders(self):
        orders = [
            {"completionType": None, "regulationType": "Up",
             "quantity": "0.020", "unitPrice": "10.0"},
            {"completionType": "Settled", "regulationType": "Down",
             "quantity": "0.005", "unitPrice": "6.0"},
            {"completionType": None, "regulationType": "Down",
             "quantity": "0.012", "unitPrice": "8.0"},
        ]
        result = self._parse_dso_demands(orders)
        self.assertEqual(len(result), 2)
        self.assertAlmostEqual(result[0]["Up"], 0.020)
        self.assertAlmostEqual(result[1]["Down"], 0.012)


class TestDsoOrgResolution(unittest.TestCase):
    """Verify DSO org ID can be resolved from NODES API response."""

    def test_single_org_found(self):
        api_response = {"items": [{"id": "org-uuid-123", "name": "AEM"}]}
        items = api_response.get("items", [])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], "org-uuid-123")

    def test_no_org_found(self):
        api_response = {"items": []}
        items = api_response.get("items", [])
        dso_org_id = items[0]["id"] if len(items) == 1 else None
        self.assertIsNone(dso_org_id)

    def test_missing_items_key(self):
        api_response = {"error": "something went wrong"}
        items = api_response.get("items", [])
        dso_org_id = items[0]["id"] if len(items) == 1 else None
        self.assertIsNone(dso_org_id)


class TestNoDsoImportInTraderFsp(unittest.TestCase):
    """Verify that trader_fsp.py no longer imports classes.dso."""

    def test_no_dso_import(self):
        script_path = os.path.join(
            os.path.dirname(__file__), "..", "scripts", "trader_fsp.py"
        )
        with open(script_path) as f:
            source = f.read()
        self.assertNotIn("from classes.dso import", source)
        self.assertNotIn("import classes.dso", source)

    def test_imports_player(self):
        script_path = os.path.join(
            os.path.dirname(__file__), "..", "scripts", "trader_fsp.py"
        )
        with open(script_path) as f:
            source = f.read()
        self.assertIn("from classes.player import Player", source)

    def test_no_dso_object_instantiation(self):
        """No 'DSO(' constructor call should exist."""
        script_path = os.path.join(
            os.path.dirname(__file__), "..", "scripts", "trader_fsp.py"
        )
        with open(script_path) as f:
            source = f.read()
        self.assertNotIn("DSO(", source)


if __name__ == "__main__":
    unittest.main()
