"""
Tests for flexi_manager command-preparation helpers.

Validates:
- timestamp parsing and formatting
- EV schedule construction
- HP curtail / restore payload preparation
- EV curtail / restore payload preparation
- restore command generation for previously controlled assets
"""

import json
import logging
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from flexi_manager import (  # noqa: E402
    _build_ev_schedule,
    _format_aem_utc,
    _parse_slot_datetime,
    _prepare_command_payload_for_slot,
    AssetController,
)

logger = logging.getLogger("test_flexi_manager_commands")


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------


class TestParseSlotDatetime(unittest.TestCase):

    def test_naive_datetime_passthrough(self):
        dt = datetime(2026, 4, 27, 12, 15, 0)
        result = _parse_slot_datetime(dt)
        assert result == datetime(2026, 4, 27, 12, 15, 0)

    def test_aware_datetime_converted_to_naive_utc(self):
        dt = datetime(2026, 4, 27, 14, 15, 0, tzinfo=timezone(timedelta(hours=2)))
        result = _parse_slot_datetime(dt)
        assert result == datetime(2026, 4, 27, 12, 15, 0)

    def test_iso_string_parsed(self):
        result = _parse_slot_datetime("2026-04-27T12:15:00")
        assert result == datetime(2026, 4, 27, 12, 15, 0)

    def test_iso_string_with_tz_parsed(self):
        result = _parse_slot_datetime("2026-04-27T14:15:00+02:00")
        assert result == datetime(2026, 4, 27, 12, 15, 0)

    def test_seconds_and_microseconds_stripped(self):
        result = _parse_slot_datetime("2026-04-27T12:15:34.123456")
        assert result == datetime(2026, 4, 27, 12, 15, 0)

    def test_z_suffix_handled(self):
        result = _parse_slot_datetime("2026-04-27T12:15:00Z")
        assert result == datetime(2026, 4, 27, 12, 15, 0)


class TestFormatAemUtc(unittest.TestCase):

    def test_naive_datetime(self):
        dt = datetime(2026, 4, 27, 12, 15, 0)
        assert _format_aem_utc(dt) == "2026-04-27T12:15:00"

    def test_aware_datetime_converted(self):
        dt = datetime(2026, 4, 27, 14, 15, 0, tzinfo=timezone(timedelta(hours=2)))
        assert _format_aem_utc(dt) == "2026-04-27T12:15:00"

    def test_microseconds_removed(self):
        dt = datetime(2026, 4, 27, 12, 15, 0, 999999, tzinfo=timezone.utc)
        assert _format_aem_utc(dt) == "2026-04-27T12:15:00"

    def test_no_timezone_suffix(self):
        dt = datetime(2026, 4, 27, 12, 15, 0, tzinfo=timezone.utc)
        result = _format_aem_utc(dt)
        assert "+" not in result
        assert "Z" not in result


# ---------------------------------------------------------------------------
# EV schedule
# ---------------------------------------------------------------------------


class TestBuildEvSchedule(unittest.TestCase):

    def test_single_15min_slot(self):
        start = datetime(2026, 4, 27, 12, 15)
        end = datetime(2026, 4, 27, 12, 30)
        schedule = _build_ev_schedule(start, end, 0.0)
        assert schedule == {"2026-04-27T12:15:00": 0.0}

    def test_30min_slot_two_points(self):
        start = datetime(2026, 4, 27, 12, 15)
        end = datetime(2026, 4, 27, 12, 45)
        schedule = _build_ev_schedule(start, end, 3.5)
        assert schedule == {
            "2026-04-27T12:15:00": 3.5,
            "2026-04-27T12:30:00": 3.5,
        }

    def test_empty_window_raises(self):
        start = datetime(2026, 4, 27, 12, 15)
        with self.assertRaises(ValueError):
            _build_ev_schedule(start, start, 0.0)

    def test_power_converted_to_float(self):
        start = datetime(2026, 4, 27, 12, 15)
        end = datetime(2026, 4, 27, 12, 30)
        schedule = _build_ev_schedule(start, end, 5)
        assert schedule["2026-04-27T12:15:00"] == 5.0
        assert isinstance(schedule["2026-04-27T12:15:00"], float)


# ---------------------------------------------------------------------------
# _prepare_command_payload_for_slot
# ---------------------------------------------------------------------------


class TestPrepareCommandPayload(unittest.TestCase):

    def test_hp_curtail_gets_clean_slot(self):
        cmd = {
            "asset_id": "ECM96.2",
            "asset_type": "heat_pump",
            "command_type": "curtail",
            "payload": {
                "asset_id": "ECM96.2",
                "asset_type": "heat_pump",
                "discrete_state": "OFF",
                "target_power_kw": 0.0,
            },
        }
        slot_start = datetime(2026, 4, 27, 12, 15)
        slot_end = datetime(2026, 4, 27, 12, 30)

        _prepare_command_payload_for_slot(cmd, slot_start, slot_end, dry_run=False)

        p = cmd["payload"]
        assert p["slot_start"] == "2026-04-27T12:15:00"
        assert p["slot_end"] == "2026-04-27T12:30:00"
        assert p["dry_run"] is False
        assert p["discrete_state"] == "OFF"
        assert "schedule" not in p

    def test_ev_curtail_gets_schedule(self):
        cmd = {
            "asset_id": "ECM63.1",
            "asset_type": "ev_charger",
            "command_type": "curtail",
            "payload": {
                "asset_id": "ECM63.1",
                "asset_type": "ev_charger",
                "target_power_kw": 0.0,
                "power_kw": 0.0,
            },
        }
        slot_start = datetime(2026, 4, 27, 12, 15)
        slot_end = datetime(2026, 4, 27, 12, 30)

        _prepare_command_payload_for_slot(cmd, slot_start, slot_end, dry_run=False)

        p = cmd["payload"]
        assert p["slot_start"] == "2026-04-27T12:15:00"
        assert p["slot_end"] == "2026-04-27T12:30:00"
        assert p["target_power_kw"] == 0.0
        assert p["power_kw"] == 0.0
        assert p["schedule"] == {"2026-04-27T12:15:00": 0.0}
        assert p["dry_run"] is False

    def test_ev_restore_gets_schedule_with_power(self):
        cmd = {
            "asset_id": "ECM63.1",
            "asset_type": "ev_charger",
            "command_type": "restore",
            "payload": {
                "asset_id": "ECM63.1",
                "asset_type": "ev_charger",
                "target_power_kw": 6.0,
                "power_kw": 6.0,
            },
        }
        slot_start = datetime(2026, 4, 27, 12, 30)
        slot_end = datetime(2026, 4, 27, 12, 45)

        _prepare_command_payload_for_slot(cmd, slot_start, slot_end, dry_run=False)

        p = cmd["payload"]
        assert p["schedule"] == {"2026-04-27T12:30:00": 6.0}
        assert p["target_power_kw"] == 6.0
        assert p["power_kw"] == 6.0

    def test_dry_run_flag_set(self):
        cmd = {
            "asset_id": "ECM96.2",
            "asset_type": "heat_pump",
            "command_type": "curtail",
            "payload": {"discrete_state": "OFF"},
        }
        slot_start = datetime(2026, 4, 27, 12, 15)
        slot_end = datetime(2026, 4, 27, 12, 30)

        _prepare_command_payload_for_slot(cmd, slot_start, slot_end, dry_run=True)
        assert cmd["payload"]["dry_run"] is True


# ---------------------------------------------------------------------------
# AssetController curtail / restore payloads
# ---------------------------------------------------------------------------


def _make_hp_asset():
    return {
        "type": "heat_pump",
        "description": "HP Small",
        "pod": "ECM96",
        "capacity_kw": 4.0,
        "modulation_type": "discrete",
        "discrete_states_kw": [0.0, 4.0],
    }


def _make_ev_asset():
    return {
        "type": "ev_charger",
        "description": "EV Charger 1",
        "pod": "ECM63",
        "capacity_kw": 11.0,
        "modulation_type": "continuous",
    }


class _FakePublisher:
    """Minimal publisher stub so controller queues commands."""

    def is_connected(self):
        return True

    def publish_batch_commands(self, commands, slot_info=None):
        return len(commands)


class TestCurtailPayloads(unittest.TestCase):

    def test_hp_curtail_payload_contains_discrete_state(self):
        ctrl = AssetController(
            {"ECM96.2": _make_hp_asset()}, logger,
            rabbitmq_publisher=_FakePublisher(), community="ECM",
        )
        ctrl.curtail_asset("ECM96.2", curtailment_kw=4.0, dry_run=True)

        assert len(ctrl._pending_commands) == 1
        p = ctrl._pending_commands[0]["payload"]
        assert p["discrete_state"] == "OFF"
        assert p["target_power_kw"] == 0.0

    def test_ev_curtail_payload_has_power_kw(self):
        ctrl = AssetController(
            {"ECM63.1": _make_ev_asset()}, logger,
            rabbitmq_publisher=_FakePublisher(), community="ECM",
        )
        ctrl.curtail_asset("ECM63.1", curtailment_kw=11.0, dry_run=True)

        assert len(ctrl._pending_commands) == 1
        p = ctrl._pending_commands[0]["payload"]
        assert p["target_power_kw"] == 0.0
        assert p["power_kw"] == 0.0


class TestRestorePayloads(unittest.TestCase):

    def test_hp_restore_has_on_state(self):
        ctrl = AssetController(
            {"ECM96.2": _make_hp_asset()}, logger,
            rabbitmq_publisher=_FakePublisher(), community="ECM",
        )
        ctrl.restore_asset("ECM96.2", dry_run=True)

        assert len(ctrl._pending_commands) == 1
        p = ctrl._pending_commands[0]["payload"]
        assert p["target_state"] == "ON"
        assert p["discrete_state"] == "ON"
        assert p["target_power_kw"] == 4.0

    def test_ev_restore_uses_restore_power_kw(self):
        ev = _make_ev_asset()
        ev["restore_power_kw"] = 4.5
        ev["default_power_kw"] = 6.0

        ctrl = AssetController(
            {"ECM63.1": ev}, logger,
            rabbitmq_publisher=_FakePublisher(), community="ECM",
        )
        ctrl.restore_asset("ECM63.1", dry_run=True)

        p = ctrl._pending_commands[0]["payload"]
        assert p["target_power_kw"] == 4.5
        assert p["power_kw"] == 4.5

    def test_ev_restore_falls_back_to_default_power(self):
        ev = _make_ev_asset()
        ev["default_power_kw"] = 6.0

        ctrl = AssetController(
            {"ECM63.1": ev}, logger,
            rabbitmq_publisher=_FakePublisher(), community="ECM",
        )
        ctrl.restore_asset("ECM63.1", dry_run=True)

        p = ctrl._pending_commands[0]["payload"]
        assert p["target_power_kw"] == 6.0
        assert p["power_kw"] == 6.0

    def test_ev_restore_falls_back_to_capacity(self):
        ctrl = AssetController(
            {"ECM63.1": _make_ev_asset()}, logger,
            rabbitmq_publisher=_FakePublisher(), community="ECM",
        )
        ctrl.restore_asset("ECM63.1", dry_run=True)

        p = ctrl._pending_commands[0]["payload"]
        assert p["target_power_kw"] == 11.0
        assert p["power_kw"] == 11.0


# ---------------------------------------------------------------------------
# Publish pending commands (integration-level)
# ---------------------------------------------------------------------------


class _RecordingPublisher(_FakePublisher):
    """Captures published commands for assertion."""

    def __init__(self):
        self.published = []

    def publish_batch_commands(self, commands, slot_info=None):
        self.published.extend(commands)
        return len(commands)


class TestPublishPendingCommands(unittest.TestCase):

    def test_hp_curtail_published_with_clean_timestamps(self):
        pub = _RecordingPublisher()
        ctrl = AssetController(
            {"ECM96.2": _make_hp_asset()}, logger,
            rabbitmq_publisher=pub, community="ECM",
        )
        ctrl.curtail_asset("ECM96.2", curtailment_kw=4.0, dry_run=False)

        slot_info = {
            "fsp_id": "test",
            "slot_start": "2026-04-27T12:15:00",
            "slot_end": "2026-04-27T12:30:00",
        }
        ctrl.publish_pending_commands(slot_info, dry_run=False)

        assert len(pub.published) == 1
        p = pub.published[0]["payload"]
        assert p["slot_start"] == "2026-04-27T12:15:00"
        assert p["slot_end"] == "2026-04-27T12:30:00"
        assert p["dry_run"] is False
        assert p["discrete_state"] == "OFF"
        assert "schedule" not in p

    def test_ev_curtail_published_with_schedule(self):
        pub = _RecordingPublisher()
        ctrl = AssetController(
            {"ECM63.1": _make_ev_asset()}, logger,
            rabbitmq_publisher=pub, community="ECM",
        )
        ctrl.curtail_asset("ECM63.1", curtailment_kw=11.0, dry_run=False)

        slot_info = {
            "fsp_id": "test",
            "slot_start": "2026-04-27T12:15:00",
            "slot_end": "2026-04-27T12:30:00",
        }
        ctrl.publish_pending_commands(slot_info, dry_run=False)

        assert len(pub.published) == 1
        p = pub.published[0]["payload"]
        assert p["slot_start"] == "2026-04-27T12:15:00"
        assert p["slot_end"] == "2026-04-27T12:30:00"
        assert p["target_power_kw"] == 0.0
        assert p["power_kw"] == 0.0
        assert p["schedule"] == {"2026-04-27T12:15:00": 0.0}
        assert p["dry_run"] is False

    def test_ev_restore_published_with_schedule(self):
        pub = _RecordingPublisher()
        ev = _make_ev_asset()
        ev["default_power_kw"] = 6.0
        ctrl = AssetController(
            {"ECM63.1": ev}, logger,
            rabbitmq_publisher=pub, community="ECM",
        )
        ctrl.restore_asset("ECM63.1", dry_run=False)

        slot_info = {
            "fsp_id": "test",
            "slot_start": "2026-04-27T12:30:00",
            "slot_end": "2026-04-27T12:45:00",
        }
        ctrl.publish_pending_commands(slot_info, dry_run=False)

        p = pub.published[0]["payload"]
        assert p["schedule"] == {"2026-04-27T12:30:00": 6.0}
        assert p["target_power_kw"] == 6.0
        assert p["power_kw"] == 6.0

    def test_hp_restore_published_with_on_state(self):
        pub = _RecordingPublisher()
        ctrl = AssetController(
            {"ECM96.2": _make_hp_asset()}, logger,
            rabbitmq_publisher=pub, community="ECM",
        )
        ctrl.restore_asset("ECM96.2", dry_run=False)

        slot_info = {
            "fsp_id": "test",
            "slot_start": "2026-04-27T12:30:00",
            "slot_end": "2026-04-27T12:45:00",
        }
        ctrl.publish_pending_commands(slot_info, dry_run=False)

        p = pub.published[0]["payload"]
        assert p["slot_start"] == "2026-04-27T12:30:00"
        assert p["slot_end"] == "2026-04-27T12:45:00"
        assert p["target_state"] == "ON"
        assert p["discrete_state"] == "ON"
        assert p["target_power_kw"] == 4.0
        assert p["dry_run"] is False

    def test_multiple_commands_published_in_batch(self):
        pub = _RecordingPublisher()
        ctrl = AssetController(
            {"ECM96.2": _make_hp_asset(), "ECM63.1": _make_ev_asset()},
            logger, rabbitmq_publisher=pub, community="ECM",
        )
        ctrl.curtail_asset("ECM96.2", curtailment_kw=4.0, dry_run=False)
        ctrl.curtail_asset("ECM63.1", curtailment_kw=11.0, dry_run=False)

        slot_info = {
            "fsp_id": "test",
            "slot_start": "2026-04-27T12:15:00",
            "slot_end": "2026-04-27T12:30:00",
        }
        published = ctrl.publish_pending_commands(slot_info, dry_run=False)

        assert published == 2
        assert len(pub.published) == 2


# ---------------------------------------------------------------------------
# State file persistence (FlexibilityManager helpers)
# ---------------------------------------------------------------------------


class TestControlledStatePersistence(unittest.TestCase):
    """Tests for _load_controlled_state / _save_controlled_state via a
    minimal FlexibilityManager-like object."""

    def _make_manager_stub(self, state_file):
        """Build a lightweight object with just the state methods."""
        from flexi_manager import FlexibilityManager

        class Stub:
            pass

        stub = Stub()
        stub.state_file = state_file
        stub.logger = logger
        stub._load_controlled_state = (
            FlexibilityManager._load_controlled_state.__get__(stub)
        )
        stub._save_controlled_state = (
            FlexibilityManager._save_controlled_state.__get__(stub)
        )
        return stub

    def test_round_trip(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            mgr = self._make_manager_stub(path)
            state = {
                "ECM96.2": {
                    "asset_type": "heat_pump",
                    "slot_start": "2026-04-27T12:15:00",
                },
            }
            mgr._save_controlled_state(state)
            loaded = mgr._load_controlled_state()
            assert loaded == state
        finally:
            os.unlink(path)

    def test_missing_file_returns_empty(self):
        mgr = self._make_manager_stub("/tmp/nonexistent_test_state_9999.json")
        assert mgr._load_controlled_state() == {}


if __name__ == "__main__":
    unittest.main()
