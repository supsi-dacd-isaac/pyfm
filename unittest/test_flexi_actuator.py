"""
Focused tests for flexi_actuator EV scheduling behavior.

Validates:
- interval rounding and AEM UTC formatting
- EV schedule construction
- EV restore power selection
- command preparation for EV and heat pump assets
"""

import logging
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from flexi_actuator import (  # noqa: E402
    _build_ev_schedule,
    _build_restore_command,
    _build_rabbitmq_command,
    _ceil_to_next_interval,
    _format_aem_utc,
    _prepare_commands_for_publish,
)


logger = logging.getLogger("test_flexi_actuator")


class TestDatetimeHelpers(unittest.TestCase):

    def test_ceil_to_next_interval_moves_to_next_boundary(self):
        dt = datetime(2026, 4, 27, 13, 15, 34, tzinfo=timezone.utc)
        result = _ceil_to_next_interval(dt, interval_minutes=15)
        assert result == datetime(2026, 4, 27, 13, 30, 0, tzinfo=timezone.utc)

    def test_ceil_to_next_interval_moves_even_on_boundary(self):
        dt = datetime(2026, 4, 27, 13, 30, 0, tzinfo=timezone.utc)
        result = _ceil_to_next_interval(dt, interval_minutes=15)
        assert result == datetime(2026, 4, 27, 13, 45, 0, tzinfo=timezone.utc)

    def test_format_aem_utc_removes_tz_and_microseconds(self):
        dt = datetime(2026, 4, 27, 13, 30, 0, 123456, tzinfo=timezone.utc)
        assert _format_aem_utc(dt) == "2026-04-27T13:30:00"


class TestEvScheduleHelpers(unittest.TestCase):

    def test_build_ev_schedule_single_point(self):
        slot_start = datetime(2026, 4, 27, 13, 30, 0, tzinfo=timezone.utc)
        slot_end = slot_start + timedelta(minutes=15)
        schedule = _build_ev_schedule(slot_start, slot_end, 0.0, interval_minutes=15)
        assert schedule == {"2026-04-27T13:30:00": 0.0}

    def test_build_ev_schedule_multiple_points(self):
        slot_start = datetime(2026, 4, 27, 13, 30, 0, tzinfo=timezone.utc)
        slot_end = slot_start + timedelta(minutes=30)
        schedule = _build_ev_schedule(slot_start, slot_end, -1.0, interval_minutes=15)
        assert schedule == {
            "2026-04-27T13:30:00": -1.0,
            "2026-04-27T13:45:00": -1.0,
        }

    def test_build_ev_schedule_rejects_empty_window(self):
        slot_start = datetime(2026, 4, 27, 13, 30, 0, tzinfo=timezone.utc)
        with self.assertRaisesRegex(ValueError, "EV schedule is empty"):
            _build_ev_schedule(slot_start, slot_start, 0.0, interval_minutes=15)


class TestRestoreSelection(unittest.TestCase):

    def test_restore_uses_restore_power_kw_first(self):
        command, _ = _build_restore_command(
            asset_id="EV01",
            asset_config={
                "type": "ev_charger",
                "description": "EV 01",
                "pod": "ECM96",
                "capacity_kw": 11.0,
                "restore_power_kw": 4.5,
                "default_power_kw": 6.0,
            },
            community="ECM",
            original_command="restore",
            duration_minutes=15,
            logger=logger,
        )
        assert command["payload"]["target_power_kw"] == 4.5
        assert command["payload"]["power_kw"] == 4.5

    def test_restore_uses_default_power_kw_if_restore_missing(self):
        command, _ = _build_restore_command(
            asset_id="EV01",
            asset_config={
                "type": "ev_charger",
                "description": "EV 01",
                "pod": "ECM96",
                "capacity_kw": 11.0,
                "default_power_kw": 6.0,
            },
            community="ECM",
            original_command="restore",
            duration_minutes=15,
            logger=logger,
        )
        assert command["payload"]["target_power_kw"] == 6.0
        assert command["payload"]["power_kw"] == 6.0

    def test_restore_uses_capacity_if_no_explicit_restore_power(self):
        command, _ = _build_restore_command(
            asset_id="EV01",
            asset_config={
                "type": "ev_charger",
                "description": "EV 01",
                "pod": "ECM96",
                "capacity_kw": 11.0,
            },
            community="ECM",
            original_command="restore",
            duration_minutes=15,
            logger=logger,
        )
        assert command["payload"]["target_power_kw"] == 11.0
        assert command["payload"]["power_kw"] == 11.0


class TestPrepareCommands(unittest.TestCase):

    def _make_ev_asset(self):
        return {
            "type": "ev_charger",
            "description": "EV Charger 1",
            "pod": "ECM63",
            "capacity_kw": 11.0,
            "modulation_type": "continuous",
        }

    def _make_hp_asset(self):
        return {
            "type": "heat_pump",
            "description": "HP Small",
            "pod": "ECM96",
            "capacity_kw": 4.0,
            "modulation_type": "discrete",
            "discrete_states_kw": [0.0, 4.0],
        }

    def test_prepare_commands_adds_ev_force_off_schedule(self):
        command, _ = _build_rabbitmq_command(
            asset_id="ECM63.1",
            asset_config=self._make_ev_asset(),
            community="ECM",
            normalized_command="force_off",
            original_command="force_off",
            duration_minutes=15,
            logger=logger,
        )
        slot_info = {
            "slot_start": "2026-04-27T13:15:34.683925+00:00",
            "slot_end": "2026-04-27T13:30:34.683925+00:00",
            "duration_minutes": 15,
        }
        fixed_start = datetime(2026, 4, 27, 13, 30, 0, tzinfo=timezone.utc)

        with patch("flexi_actuator._ceil_to_next_interval", return_value=fixed_start):
            _prepare_commands_for_publish(
                commands=[command],
                slot_info=slot_info,
                dry_run=True,
                ev_interval_minutes=15,
                logger=logger,
            )

        payload = command["payload"]
        assert payload["target_power_kw"] == 0.0
        assert payload["power_kw"] == 0.0
        assert payload["slot_start"] == "2026-04-27T13:30:00"
        assert payload["slot_end"] == "2026-04-27T13:45:00"
        assert payload["schedule"] == {"2026-04-27T13:30:00": 0.0}
        assert payload["dry_run"] is True

    def test_prepare_commands_adds_ev_force_on_schedule(self):
        command, _ = _build_rabbitmq_command(
            asset_id="ECM63.1",
            asset_config=self._make_ev_asset(),
            community="ECM",
            normalized_command="force_on",
            original_command="force_on",
            duration_minutes=15,
            logger=logger,
        )
        slot_info = {"slot_start": "ignored", "slot_end": "ignored", "duration_minutes": 15}
        fixed_start = datetime(2026, 4, 27, 13, 30, 0, tzinfo=timezone.utc)

        with patch("flexi_actuator._ceil_to_next_interval", return_value=fixed_start):
            _prepare_commands_for_publish(
                commands=[command],
                slot_info=slot_info,
                dry_run=True,
                ev_interval_minutes=15,
                logger=logger,
            )

        payload = command["payload"]
        assert payload["target_power_kw"] == 11.0
        assert payload["power_kw"] == 11.0
        assert payload["schedule"] == {"2026-04-27T13:30:00": 11.0}

    def test_prepare_commands_shares_common_ev_start_across_batch(self):
        command1, _ = _build_rabbitmq_command(
            asset_id="ECM63.1",
            asset_config=self._make_ev_asset(),
            community="ECM",
            normalized_command="force_off",
            original_command="force_off",
            duration_minutes=15,
            logger=logger,
        )
        command2, _ = _build_rabbitmq_command(
            asset_id="ECM63.2",
            asset_config=self._make_ev_asset(),
            community="ECM",
            normalized_command="force_on",
            original_command="force_on",
            duration_minutes=30,
            logger=logger,
        )
        slot_info = {"slot_start": "ignored", "slot_end": "ignored", "duration_minutes": 15}
        fixed_start = datetime(2026, 4, 27, 13, 30, 0, tzinfo=timezone.utc)

        with patch("flexi_actuator._ceil_to_next_interval", return_value=fixed_start):
            _prepare_commands_for_publish(
                commands=[command1, command2],
                slot_info=slot_info,
                dry_run=True,
                ev_interval_minutes=15,
                logger=logger,
            )

        assert command1["payload"]["slot_start"] == "2026-04-27T13:30:00"
        assert command2["payload"]["slot_start"] == "2026-04-27T13:30:00"
        assert command2["payload"]["slot_end"] == "2026-04-27T14:00:00"
        assert command2["payload"]["schedule"] == {
            "2026-04-27T13:30:00": 11.0,
            "2026-04-27T13:45:00": 11.0,
        }

    def test_prepare_commands_preserves_hp_slot_fields(self):
        command, _ = _build_rabbitmq_command(
            asset_id="ECM96.2",
            asset_config=self._make_hp_asset(),
            community="ECM",
            normalized_command="force_off",
            original_command="force_off",
            duration_minutes=15,
            logger=logger,
        )
        slot_info = {
            "slot_start": "2026-04-27T13:15:34.683925+00:00",
            "slot_end": "2026-04-27T13:30:34.683925+00:00",
            "duration_minutes": 15,
        }

        _prepare_commands_for_publish(
            commands=[command],
            slot_info=slot_info,
            dry_run=True,
            ev_interval_minutes=15,
            logger=logger,
        )

        payload = command["payload"]
        assert payload["slot_start"] == slot_info["slot_start"]
        assert payload["slot_end"] == slot_info["slot_end"]
        assert payload["discrete_state"] == "OFF"
        assert "schedule" not in payload


if __name__ == "__main__":
    unittest.main()
