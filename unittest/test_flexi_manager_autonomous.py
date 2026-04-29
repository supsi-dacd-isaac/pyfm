import logging
import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from flexi_manager import FlexibilityManager  # noqa: E402


logger = logging.getLogger("test_flexi_manager_autonomous")


class _FakePublisher:
    def __init__(self):
        self.published = []

    def is_connected(self):
        return True

    def publish_batch_commands(self, commands, slot_info=None):
        self.published.extend(commands)
        return len(commands)


class _FakePredictor:
    def get_current_slot_price(self, slot_time):
        return {
            "price_offered": 9.03,
            "quantity_up_mw": 0.01,
            "quantity_down_mw": 0.0,
        }

    def print_price_forecast(self, current_price=None, lookahead_hours=3.0, start_time=None):
        return None

    def should_preactivate(self, current_price, lookahead_hours=3.0, threshold_pct=20.0, start_time=None):
        return {
            "recommend_preactivation": True,
            "reason": "Price expected to increase",
            "current_price": current_price,
            "max_predicted_price": 12.32,
            "avg_predicted_price": 9.86,
            "price_increase": 3.29,
            "price_increase_pct": 36.4,
            "peak_slot": {
                "slot_key": "16:30",
                "avg": 12.32,
            },
            "prediction_summary": {},
        }


def _build_config(preactivation_enabled):
    return {
        "fm": {
            "community": "ECM",
            "actors": {
                "fsps": {
                    "supsi01": {
                        "assets": ["ECM96.2"],
                    }
                }
            },
        },
        "asset_mapping": {
            "ECM96.2": {
                "type": "heat_pump",
                "description": "HP Small",
                "pod": "ECM96",
                "capacity_kw": 4.0,
                "modulation_type": "discrete",
                "discrete_states_kw": [0.0, 4.0],
            }
        },
        "autonomous": {
            "enabled": True,
            "dso_id": "AEM",
            "lookahead_hours": 3,
            "historical_days": 7,
            "price_increase_threshold_pct": 20,
            "preactivation_enabled": preactivation_enabled,
        },
    }


class TestAutonomousPreactivationSwitch(unittest.TestCase):
    def _make_manager(self, preactivation_enabled):
        publisher = _FakePublisher()
        manager = FlexibilityManager(
            config=_build_config(preactivation_enabled),
            fsp_id="supsi01",
            nodes_interface=None,
            bid_repo=None,
            logger=logger,
            nodes_authenticated=False,
            rabbitmq_publisher=publisher,
            demand_repo=None,
        )
        manager.price_predictor = _FakePredictor()
        return manager, publisher

    def test_disabled_switch_keeps_analysis_but_suppresses_commands(self):
        manager, publisher = self._make_manager(preactivation_enabled=False)

        summary = {}
        manager._run_autonomous_analysis(datetime(2026, 4, 29, 13, 45), summary, dry_run=False)

        assert publisher.published == []
        assert summary["autonomous_analysis"]["recommendation"] is True
        assert summary["autonomous_analysis"]["preactivation_enabled"] is False
        assert (
            summary["autonomous_analysis"]["preactivation_execution_skipped_reason"]
            == "disabled_by_configuration"
        )
        assert summary["preactivation_executed"] is False

    def test_enabled_switch_preserves_command_publication(self):
        manager, publisher = self._make_manager(preactivation_enabled=True)

        summary = {}
        manager._run_autonomous_analysis(datetime(2026, 4, 29, 13, 45), summary, dry_run=False)

        assert len(publisher.published) == 1
        payload = publisher.published[0]["payload"]
        assert payload["command"] == "preactivate"
        assert payload["target_state"] == "ON"
        assert summary["autonomous_analysis"]["preactivation_enabled"] is True
        assert summary["preactivation_executed"] is True


if __name__ == "__main__":
    unittest.main()
