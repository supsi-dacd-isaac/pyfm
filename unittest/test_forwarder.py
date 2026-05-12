"""
Targeted tests for the forwarder command forwarding flow.

Validates:
- dry_run flag propagation and semantics
- Template rendering (endpoint + body)
- TargetConfig matching logic
- Edge cases: missing fields, type coercion, restore commands
"""

import json
import logging
import sys
import os
import unittest
import requests
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from forwarder import (
    DEFAULT_REQUEST_RETRIES,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    TargetConfig,
    TargetHandler,
    CommandHandler,
    _get_by_path,
    _to_bool,
    _to_on_off,
    _format_datetime_utc,
    _format_datetime_utc_future_minute,
    _render_template,
    _apply_template,
    _normalize_control_url,
)


logger = logging.getLogger("test_forwarder")


# =============================================================================
# _get_by_path
# =============================================================================

class TestGetByPath(unittest.TestCase):

    def test_simple_key(self):
        assert _get_by_path({"a": 1}, "a") == 1

    def test_nested_key(self):
        assert _get_by_path({"a": {"b": {"c": 3}}}, "a.b.c") == 3

    def test_missing_key(self):
        assert _get_by_path({"a": 1}, "b") is None

    def test_missing_nested(self):
        assert _get_by_path({"a": 1}, "a.b") is None

    def test_empty_path(self):
        assert _get_by_path({"a": 1}, "") is None

    def test_none_in_path(self):
        assert _get_by_path({"a": None}, "a.b") is None


# =============================================================================
# _to_bool / _to_on_off
# =============================================================================

class TestTypeCasting(unittest.TestCase):

    def test_to_bool_true_values(self):
        for v in [True, 1, 1.0, "true", "True", "1", "on", "yes"]:
            assert _to_bool(v) is True, f"Expected True for {v!r}"

    def test_to_bool_false_values(self):
        for v in [False, 0, 0.0, "false", "False", "0", "off", "no"]:
            assert _to_bool(v) is False, f"Expected False for {v!r}"

    def test_to_bool_unknown(self):
        assert _to_bool("maybe") is None
        assert _to_bool([]) is None

    def test_to_on_off(self):
        assert _to_on_off("ON") is True
        assert _to_on_off("OFF") is False
        assert _to_on_off("on") is True
        assert _to_on_off("off") is False
        assert _to_on_off("maybe") is None
        assert _to_on_off(True) is None  # not a string


# =============================================================================
# _format_datetime_utc
# =============================================================================

class TestFormatDatetimeUtc(unittest.TestCase):

    def test_iso_string_with_tz(self):
        result = _format_datetime_utc("2025-01-15T10:30:00+01:00")
        assert result == "2025-01-15T09:30:00"

    def test_naive_iso_string(self):
        result = _format_datetime_utc("2025-01-15T10:30:00")
        assert result == "2025-01-15T10:30:00"

    def test_none_input(self):
        assert _format_datetime_utc(None) is None

    def test_empty_string(self):
        assert _format_datetime_utc("") is None

    def test_invalid_string(self):
        assert _format_datetime_utc("not-a-date") is None

    def test_datetime_object(self):
        dt = datetime(2025, 1, 15, 10, 30, tzinfo=timezone.utc)
        assert _format_datetime_utc(dt) == "2025-01-15T10:30:00"


class TestFormatDatetimeUtcFutureMinute(unittest.TestCase):

    def test_bumps_to_upcoming_minute_when_time_is_now(self):
        now = datetime(2026, 4, 27, 10, 19, 38, 583806)
        result = _format_datetime_utc_future_minute(
            "2026-04-27T10:19:38.583806+00:00",
            now=now,
        )
        assert result == "2026-04-27T10:20:00"

    def test_keeps_later_future_time(self):
        now = datetime(2026, 4, 27, 10, 19, 38, 583806)
        result = _format_datetime_utc_future_minute(
            "2026-04-27T10:30:00+00:00",
            now=now,
        )
        assert result == "2026-04-27T10:30:00"


# =============================================================================
# _normalize_control_url
# =============================================================================

class TestNormalizeControlUrl(unittest.TestCase):

    def test_none(self):
        assert _normalize_control_url(None, 6000) is None

    def test_with_scheme(self):
        assert _normalize_control_url("https://host:6000", None) == "https://host:6000"

    def test_without_scheme(self):
        result = _normalize_control_url("myhost", 6000)
        assert result == "https://myhost:6000"

    def test_without_port(self):
        result = _normalize_control_url("https://myhost", 6000)
        assert result == "https://myhost:6000"

    def test_with_port_already(self):
        result = _normalize_control_url("https://myhost:7000", 6000)
        assert result == "https://myhost:7000"


# =============================================================================
# _render_template / _apply_template
# =============================================================================

class TestTemplateRendering(unittest.TestCase):

    def test_simple_template(self):
        result = _render_template("{a}/{b}", {"a": "hello", "b": "world"})
        assert result == "hello/world"

    def test_missing_placeholder(self):
        result = _render_template("{a}/{missing}", {"a": "hello"})
        assert result == "hello/"

    def test_nested_template(self):
        ctx = {"payload": {"community": "ECM", "site_id": "ECM97"}}
        result = _render_template("{payload.community}/{payload.site_id}", ctx)
        assert result == "ECM/ECM97"

    def test_apply_template_map(self):
        template = {"$map": "payload.value", "type": "bool"}
        ctx = {"payload": {"value": "true"}}
        result = _apply_template(template, ctx)
        assert result is True

    def test_apply_template_map_on_off(self):
        template = {"$map": "payload.discrete_state", "type": "on_off"}
        ctx = {"payload": {"discrete_state": "OFF"}}
        result = _apply_template(template, ctx)
        assert result is False

    def test_apply_template_map_list_fallback(self):
        template = {"$map": ["payload.primary", "payload.secondary"], "type": "on_off"}
        ctx = {"payload": {"secondary": "ON"}}
        result = _apply_template(template, ctx)
        assert result is True

    def test_apply_template_datetime_utc(self):
        template = {"$map": "payload.slot_start", "type": "datetime_utc"}
        ctx = {"payload": {"slot_start": "2025-01-15T10:30:00+01:00"}}
        result = _apply_template(template, ctx)
        assert result == "2025-01-15T09:30:00"

    def test_apply_template_datetime_utc_future_minute(self):
        template = {"$map": "payload.slot_start", "type": "datetime_utc_future_minute"}
        ctx = {"payload": {"slot_start": "2026-04-27T10:19:38.583806+00:00"}}
        with patch("forwarder._utc_now_naive", return_value=datetime(2026, 4, 27, 10, 19, 38, 583806)):
            result = _apply_template(template, ctx)
        assert result == "2026-04-27T10:20:00"


# =============================================================================
# TargetConfig
# =============================================================================

class TestTargetConfig(unittest.TestCase):

    def _make_target(self, **overrides):
        defaults = {
            "name": "test-target",
            "url": "https://aem.local:6000",
            "enabled": True,
        }
        defaults.update(overrides)
        return TargetConfig.from_dict(defaults)

    def test_matches_all(self):
        t = self._make_target()
        assert t.matches("heat_pump", "ECM97.1") is True

    def test_matches_by_asset_type(self):
        t = self._make_target(asset_types=["heat_pump"])
        assert t.matches("heat_pump", "ECM97.1") is True
        assert t.matches("ev_charger", "ECM63.1") is False

    def test_matches_by_asset_id(self):
        t = self._make_target(asset_ids=["ECM97.1"])
        assert t.matches("heat_pump", "ECM97.1") is True
        assert t.matches("heat_pump", "ECM97.2") is False

    def test_disabled(self):
        t = self._make_target(enabled=False)
        assert t.matches("heat_pump", "ECM97.1") is False

    def test_default_request_timeout_and_retries(self):
        t = self._make_target()
        assert t.request_timeout_seconds == DEFAULT_REQUEST_TIMEOUT_SECONDS
        assert t.timeout == DEFAULT_REQUEST_TIMEOUT_SECONDS
        assert t.request_retries == DEFAULT_REQUEST_RETRIES

    def test_request_timeout_seconds_overrides_legacy_timeout(self):
        t = self._make_target(timeout=5.0, request_timeout_seconds=12.5, request_retries=4)
        assert t.request_timeout_seconds == 12.5
        assert t.timeout == 12.5
        assert t.request_retries == 4

    def test_invalid_request_settings_fall_back_to_passed_defaults(self):
        t = TargetConfig.from_dict(
            {
                "name": "test-target",
                "url": "https://aem.local:6000",
                "request_timeout_seconds": "not-a-number",
                "request_retries": "bad",
            },
            default_request_timeout_seconds=14.0,
            default_request_retries=2,
        )
        assert t.request_timeout_seconds == 14.0
        assert t.request_retries == 2

    def test_api_request_timeout_applies_when_target_timeout_missing(self):
        t = self._make_target(url="", timeout=None, request_timeout_seconds=None)
        t.apply_api_config({
            "controlUrl": "https://aem.local",
            "requestTimeout": "7.5",
        })
        assert t.request_timeout_seconds == 7.5
        assert t.timeout == 7.5

    def test_build_request_default_body(self):
        t = self._make_target()
        msg = {
            "command_type": "curtail",
            "asset_id": "ECM97.1",
            "asset_type": "heat_pump",
            "timestamp": "2025-01-15T10:00:00Z",
            "payload": {"target_power_kw": 0}
        }
        endpoint, body = t.build_request(msg)
        assert endpoint == "https://aem.local:6000/control"
        assert body["command"] == "curtail"
        assert body["asset_id"] == "ECM97.1"

    def test_build_request_with_endpoint_template(self):
        t = self._make_target(
            endpoint_template="{api.controlUrl}/{payload.community}/{payload.site_id}/{payload.asset_id}",
        )
        t.apply_api_config({"controlUrl": "https://aem.local:6000"})
        msg = {
            "command_type": "curtail",
            "asset_id": "ECM97.1",
            "payload": {
                "community": "ECM",
                "site_id": "ECM97",
                "asset_id": "ECM97.1",
            }
        }
        endpoint, body = t.build_request(msg)
        assert endpoint == "https://aem.local:6000/ECM/ECM97/ECM97.1"

    def test_build_request_with_relative_endpoint_override(self):
        t = self._make_target(
            endpoint_template="{api.controlUrl}/{payload.community}/{payload.site_id}/{payload.asset_id}",
            endpoint_overrides={"ECM96.2": "/ECM/ECM96/hp"},
        )
        t.apply_api_config({"controlUrl": "https://red.aemsa.ch/control"})
        msg = {
            "command_type": "curtail",
            "asset_id": "ECM96.2",
            "payload": {
                "community": "",
                "site_id": "",
                "asset_id": "ECM96.2",
            }
        }
        with self.assertLogs("forwarder", level="INFO") as logs:
            endpoint, body = t.build_request(msg)
        assert endpoint == "https://red.aemsa.ch/control/ECM/ECM96/hp"
        assert "using endpoint override for asset 'ECM96.2': /ECM/ECM96/hp" in "\n".join(logs.output)

    def test_build_request_with_absolute_endpoint_override(self):
        t = self._make_target(
            endpoint_template="{api.controlUrl}/{payload.community}/{payload.site_id}/{payload.asset_id}",
            endpoint_overrides={"ECM96.2": "https://red.aemsa.ch/control/ECM/ECM96/hp"},
        )
        t.apply_api_config({"controlUrl": "https://aem.local:6000"})
        msg = {
            "command_type": "curtail",
            "asset_id": "ECM96.2",
            "payload": {
                "community": "",
                "site_id": "",
                "asset_id": "ECM96.2",
            }
        }
        endpoint, body = t.build_request(msg)
        assert endpoint == "https://red.aemsa.ch/control/ECM/ECM96/hp"

    def test_build_request_with_relative_endpoint_override_requires_base_url(self):
        t = self._make_target(
            url="",
            endpoint_template="{payload.asset_id}",
            endpoint_overrides={"ECM96.2": "/ECM/ECM96/hp"},
        )
        msg = {
            "command_type": "curtail",
            "asset_id": "ECM96.2",
            "payload": {"asset_id": "ECM96.2"}
        }
        with self.assertRaisesRegex(ValueError, "Endpoint override requires base URL"):
            t.build_request(msg)

    def test_from_dict_loads_endpoint_overrides_aliases(self):
        custom_commands = TargetConfig.from_dict({
            "name": "test-target",
            "url": "https://aem.local:6000",
            "custom_commands": {"ECM96.2": "/ECM/ECM96/hp"},
        })
        custom_commans = TargetConfig.from_dict({
            "name": "test-target",
            "url": "https://aem.local:6000",
            "custom_commans": {"ECM96.2": "/ECM/ECM96/hp"},
        })
        assert custom_commands.endpoint_overrides == {"ECM96.2": "/ECM/ECM96/hp"}
        assert custom_commans.endpoint_overrides == {"ECM96.2": "/ECM/ECM96/hp"}

    def test_from_dict_loads_asset_request_profiles(self):
        target = TargetConfig.from_dict({
            "name": "test-target",
            "url": "https://aem.local:6000",
            "asset_request_profiles": {
                "ECM96.2": {"endpoint": "/ECM/ECM96/hp", "body_mode": "hp_control"},
                "EV01": {"endpoint": "/ECM/ECM96/ev", "body_mode": "ev_power_timeseries"},
            },
        })
        assert target.asset_request_profiles["ECM96.2"]["body_mode"] == "hp_control"
        assert target.asset_request_profiles["EV01"]["endpoint"] == "/ECM/ECM96/ev"

    def test_build_request_with_asset_profile_hp_control(self):
        t = self._make_target(
            endpoint_template="{api.controlUrl}/{payload.community}/{payload.site_id}/{payload.asset_id}",
            asset_request_profiles={
                "ECM96.2": {
                    "endpoint": "/ECM/ECM96/hp",
                    "body_mode": "hp_control",
                }
            },
            body_template={
                "power": {"$map": ["payload.discrete_state", "payload.target_state", "target_state"], "type": "on_off"},
                "time": {"$map": "payload.slot_start", "type": "datetime_utc"}
            },
        )
        t.apply_api_config({"controlUrl": "https://red.aemsa.ch/control"})
        msg = {
            "command_type": "curtail",
            "asset_id": "ECM96.2",
            "payload": {
                "community": "ECM",
                "site_id": "ECM96",
                "asset_id": "ECM96.2",
                "discrete_state": "OFF",
                "slot_start": "2025-01-15T10:00:00+01:00",
            }
        }
        with self.assertLogs("forwarder", level="INFO") as logs:
            endpoint, body = t.build_request(msg)
        assert endpoint == "https://red.aemsa.ch/control/ECM/ECM96/hp"
        assert body == {"power": False, "time": "2025-01-15T09:00:00"}
        output = "\n".join(logs.output)
        assert "using request profile for asset 'ECM96.2': body_mode=hp_control, endpoint=/ECM/ECM96/hp" in output

    def test_build_request_with_asset_profile_ev_power_single_step(self):
        t = self._make_target(
            endpoint_template="{api.controlUrl}/{payload.community}/{payload.site_id}/{payload.asset_id}",
            asset_request_profiles={
                "EV01": {
                    "endpoint": "/ECM/ECM96/ev",
                    "body_mode": "ev_power_timeseries",
                }
            },
            body_template={
                "power": {"$map": "payload.discrete_state", "type": "on_off"},
            },
        )
        t.apply_api_config({"controlUrl": "https://red.aemsa.ch/control"})
        msg = {
            "command_type": "charge",
            "asset_id": "EV01",
            "asset_type": "ev_charger",
            "timestamp": "2026-04-27T10:00:00+00:00",
            "payload": {
                "community": "ECM",
                "site_id": "ECM96",
                "asset_id": "EV01",
                "slot_start": "2026-04-27T10:00:00+00:00",
                "power_kw": 1.0,
                "dry_run": True,
            }
        }
        endpoint, body = t.build_request(msg)
        assert endpoint == "https://red.aemsa.ch/control/ECM/ECM96/ev"
        assert body == {"2026-04-27T10:00:00": 1.0}

    def test_build_request_with_asset_profile_ev_schedule_list(self):
        t = self._make_target(
            asset_request_profiles={
                "EV01": {
                    "endpoint": "/ECM/ECM96/ev",
                    "body_mode": "ev_power_timeseries",
                }
            },
        )
        t.apply_api_config({"controlUrl": "https://red.aemsa.ch/control"})
        msg = {
            "command_type": "charge_schedule",
            "asset_id": "EV01",
            "asset_type": "ev_charger",
            "payload": {
                "schedule": [
                    {"time": "2026-04-27T10:00:00+00:00", "power_kw": 1.0},
                    {"time": "2026-04-27T10:15:00+00:00", "power_kw": -1.0},
                ],
                "dry_run": True,
            }
        }
        endpoint, body = t.build_request(msg)
        assert endpoint == "https://red.aemsa.ch/control/ECM/ECM96/ev"
        assert body == {
            "2026-04-27T10:00:00": 1.0,
            "2026-04-27T10:15:00": -1.0,
        }

    def test_build_request_with_asset_profile_ev_schedule_dict(self):
        t = self._make_target(
            asset_request_profiles={
                "EV01": {
                    "endpoint": "/ECM/ECM96/ev",
                    "body_mode": "ev_power_timeseries",
                }
            },
        )
        t.apply_api_config({"controlUrl": "https://red.aemsa.ch/control"})
        msg = {
            "command_type": "charge_schedule",
            "asset_id": "EV01",
            "asset_type": "ev_charger",
            "payload": {
                "schedule": {
                    "2026-04-27T10:00:00+00:00": 1.0,
                    "2026-04-27T10:15:00+00:00": -1.0,
                },
                "dry_run": True,
            }
        }
        endpoint, body = t.build_request(msg)
        assert body == {
            "2026-04-27T10:00:00": 1.0,
            "2026-04-27T10:15:00": -1.0,
        }

    def test_build_request_with_asset_profile_ev_requires_timestamp(self):
        t = self._make_target(
            asset_request_profiles={
                "EV01": {
                    "endpoint": "/ECM/ECM96/ev",
                    "body_mode": "ev_power_timeseries",
                }
            },
        )
        msg = {
            "command_type": "charge",
            "asset_id": "EV01",
            "payload": {
                "power_kw": 1.0,
            }
        }
        with self.assertRaisesRegex(ValueError, "missing timestamp"):
            t.build_request(msg)

    def test_build_request_with_asset_profile_ev_requires_power(self):
        t = self._make_target(
            asset_request_profiles={
                "EV01": {
                    "endpoint": "/ECM/ECM96/ev",
                    "body_mode": "ev_power_timeseries",
                }
            },
        )
        msg = {
            "command_type": "charge",
            "asset_id": "EV01",
            "payload": {
                "slot_start": "2026-04-27T10:00:00+00:00",
            }
        }
        with self.assertRaisesRegex(ValueError, "missing power"):
            t.build_request(msg)

    def test_build_request_with_asset_profile_ev_rejects_invalid_power(self):
        t = self._make_target(
            asset_request_profiles={
                "EV01": {
                    "endpoint": "/ECM/ECM96/ev",
                    "body_mode": "ev_power_timeseries",
                }
            },
        )
        msg = {
            "command_type": "charge",
            "asset_id": "EV01",
            "payload": {
                "slot_start": "2026-04-27T10:00:00+00:00",
                "power_kw": "abc",
            }
        }
        with self.assertRaisesRegex(ValueError, "not a valid float"):
            t.build_request(msg)

    def test_build_request_with_asset_profile_ev_rejects_empty_schedule(self):
        t = self._make_target(
            asset_request_profiles={
                "EV01": {
                    "endpoint": "/ECM/ECM96/ev",
                    "body_mode": "ev_power_timeseries",
                }
            },
        )
        msg = {
            "command_type": "charge_schedule",
            "asset_id": "EV01",
            "payload": {
                "schedule": [],
            }
        }
        with self.assertRaisesRegex(ValueError, "schedule is empty"):
            t.build_request(msg)

    def test_build_request_with_body_template_discrete(self):
        """Simulate a discrete heat pump curtail command through the AEM template."""
        t = self._make_target(
            endpoint_template="{api.controlUrl}/{payload.community}/{payload.site_id}/{payload.asset_id}",
            body_template={
                "power": {"$map": ["payload.discrete_state", "payload.target_state", "target_state"], "type": "on_off"},
                "time": {"$map": "payload.slot_start", "type": "datetime_utc"}
            },
        )
        t.apply_api_config({"controlUrl": "https://aem.local:6000"})
        msg = {
            "command_type": "curtail",
            "asset_id": "ECM97.1",
            "payload": {
                "community": "ECM",
                "site_id": "ECM97",
                "asset_id": "ECM97.1",
                "discrete_state": "OFF",
                "slot_start": "2025-01-15T10:00:00+01:00",
            }
        }
        endpoint, body = t.build_request(msg)
        assert endpoint == "https://aem.local:6000/ECM/ECM97/ECM97.1"
        assert body["power"] is False
        assert body["time"] == "2025-01-15T09:00:00"

    def test_build_request_restore_no_power_field(self):
        """Restore commands don't set discrete_state -- power will be None."""
        t = self._make_target(
            body_template={
                "power": {"$map": ["payload.discrete_state", "payload.target_state"], "type": "on_off"},
                "time": {"$map": "payload.slot_start", "type": "datetime_utc"}
            },
        )
        msg = {
            "command_type": "restore",
            "asset_id": "ECM97.1",
            "payload": {
                "community": "ECM",
                "site_id": "ECM97",
                "asset_id": "ECM97.1",
                "action": "restore",
            }
        }
        endpoint, body = t.build_request(msg)
        # power resolves to None because neither discrete_state nor target_state exist
        assert body["power"] is None


# =============================================================================
# TargetHandler dry_run semantics
# =============================================================================

class TestTargetHandlerDryRun(unittest.TestCase):

    def _make_handler(self, dry_run=True, **target_kwargs):
        handler = TargetHandler(logger, dry_run=dry_run)
        target = TargetConfig(
            name="test",
            url="https://aem.local:6000",
            **target_kwargs,
        )
        handler.add_target(target)
        return handler

    def _make_message(self, dry_run_flag=True):
        return {
            "command_type": "curtail",
            "asset_id": "ECM97.1",
            "asset_type": "heat_pump",
            "timestamp": "2025-01-15T10:00:00Z",
            "payload": {
                "community": "ECM",
                "site_id": "ECM97",
                "asset_id": "ECM97.1",
                "discrete_state": "OFF",
                "dry_run": dry_run_flag,
                "slot_start": "2025-01-15T10:00:00Z",
            }
        }

    def test_message_dry_run_true_no_http(self):
        """When payload.dry_run=True, no HTTP request should be made."""
        handler = self._make_handler(dry_run=False)
        msg = self._make_message(dry_run_flag=True)
        with patch("forwarder.requests") as mock_requests:
            result = handler.forward_command(msg)
            assert result is True
            mock_requests.post.assert_not_called()

    def test_message_dry_run_false_sends_http(self):
        """When payload.dry_run=False, HTTP POST should be made."""
        handler = self._make_handler(dry_run=False)
        msg = self._make_message(dry_run_flag=False)
        with patch("forwarder.requests") as mock_requests:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.text = '{"status": "ok"}'
            mock_requests.post.return_value = mock_response
            result = handler.forward_command(msg)
            assert result is True
            mock_requests.post.assert_called_once()

    def test_message_dry_run_false_logs_payload(self):
        """Live forwarding should log the outgoing POST payload at INFO level."""
        handler = self._make_handler(dry_run=False)
        msg = self._make_message(dry_run_flag=False)
        with patch("forwarder.requests") as mock_requests:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.text = '{"status": "ok"}'
            mock_requests.post.return_value = mock_response
            with self.assertLogs("test_forwarder", level="INFO") as logs:
                result = handler.forward_command(msg)
            assert result is True
        output = "\n".join(logs.output)
        assert "Forwarding payload to target 'test': {" in output
        assert '"command": "curtail"' in output
        assert '"dry_run": false' in output

    def test_message_dry_run_false_suppresses_insecure_https_warning_when_verify_disabled(self):
        """verify_ssl=False should not leak urllib3's InsecureRequestWarning into logs/output."""
        handler = self._make_handler(dry_run=False)
        msg = self._make_message(dry_run_flag=False)
        with patch("forwarder.requests") as mock_requests, \
             patch("forwarder.warnings.catch_warnings") as mock_catch_warnings, \
             patch("forwarder.warnings.simplefilter") as mock_simplefilter:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.text = '{"status": "ok"}'
            mock_requests.post.return_value = mock_response

            context = MagicMock()
            mock_catch_warnings.return_value.__enter__.return_value = context
            mock_catch_warnings.return_value.__exit__.return_value = None

            result = handler.forward_command(msg)

            assert result is True
            mock_requests.post.assert_called_once()
            mock_catch_warnings.assert_called_once()
            mock_simplefilter.assert_called_once()

    def test_global_dry_run_ignored_by_target_handler(self):
        """CRITICAL finding: TargetHandler.dry_run is not used in _send_to_target.
        Even with global dry_run=True, if message says dry_run=False, HTTP fires."""
        handler = self._make_handler(dry_run=True)  # global dry_run
        msg = self._make_message(dry_run_flag=False)  # message says live
        with patch("forwarder.requests") as mock_requests:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.text = '{"status": "ok"}'
            mock_requests.post.return_value = mock_response
            result = handler.forward_command(msg)
            assert result is True
            # This proves the global dry_run flag is NOT consulted
            mock_requests.post.assert_called_once()

    def test_missing_dry_run_defaults_to_true(self):
        """When payload has no dry_run field, it defaults to True (safe)."""
        handler = self._make_handler(dry_run=False)
        msg = self._make_message()
        del msg["payload"]["dry_run"]
        with patch("forwarder.requests") as mock_requests:
            result = handler.forward_command(msg)
            assert result is True
            mock_requests.post.assert_not_called()

    def test_configured_timeout_is_passed_to_post(self):
        handler = self._make_handler(dry_run=False, request_timeout_seconds=12.5)
        msg = self._make_message(dry_run_flag=False)

        with patch("forwarder.requests.post") as mock_post:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.text = '{"status": "ok"}'
            mock_post.return_value = mock_response

            result = handler.forward_command(msg)

        assert result is True
        assert mock_post.call_args.kwargs["timeout"] == 12.5

    def test_failed_response_is_retried_up_to_configured_number(self):
        handler = self._make_handler(dry_run=False, request_retries=2)
        msg = self._make_message(dry_run_flag=False)

        with patch("forwarder.requests.post") as mock_post:
            mock_response = MagicMock()
            mock_response.status_code = 500
            mock_response.text = '{"status": "error"}'
            mock_post.return_value = mock_response

            with self.assertLogs("test_forwarder", level="WARNING") as logs:
                result = handler.forward_command(msg)

        assert result is False
        assert mock_post.call_count == 3
        output = "\n".join(logs.output)
        assert "attempt 1/3 returned error" in output
        assert "attempt 2/3 returned error" in output
        assert "request failed after 3 attempts (2 retries)" in output

    def test_timeout_exception_is_retried_and_logs_final_failure(self):
        handler = self._make_handler(
            dry_run=False,
            request_timeout_seconds=2.0,
            request_retries=1,
        )
        msg = self._make_message(dry_run_flag=False)

        with patch("forwarder.requests.post", side_effect=requests.exceptions.Timeout) as mock_post:
            with self.assertLogs("test_forwarder", level="WARNING") as logs:
                result = handler.forward_command(msg)

        assert result is False
        assert mock_post.call_count == 2
        output = "\n".join(logs.output)
        assert "attempt 1/2 request timed out after 2.0s; retrying" in output
        assert "request failed after 2 attempts (1 retries): request timed out after 2.0s" in output


# =============================================================================
# CommandHandler dry_run semantics
# =============================================================================

class TestCommandHandlerDryRun(unittest.TestCase):

    def test_force_dry_run_overrides_message(self):
        """CommandHandler.force_dry_run=True makes handle_command treat as dry-run
        even when message says dry_run=False."""
        handler = CommandHandler(logger, force_dry_run=True, target_handler=None)
        msg = {
            "command_type": "curtail",
            "asset_id": "ECM97.1",
            "asset_type": "heat_pump",
            "timestamp": "2025-01-15T10:00:00Z",
            "payload": {
                "dry_run": False,
                "slot_start": "2025-01-15T10:00:00Z",
                "slot_end": "2025-01-15T10:15:00Z",
                "modulation_type": "discrete",
                "discrete_state": "OFF",
                "actual_curtailment_kw": 15.0,
                "target_power_kw": 0.0,
                "duration_minutes": 15,
            }
        }
        result = handler.handle_command(msg)
        assert result is True
        assert handler.commands_received == 1

    def test_handle_command_always_returns_true(self):
        """handle_command returns True even if forwarding fails."""
        target_handler = TargetHandler(logger, dry_run=True)
        target = TargetConfig(name="broken", url="")  # will fail
        target_handler.add_target(target)

        handler = CommandHandler(logger, force_dry_run=False, target_handler=target_handler)
        msg = {
            "command_type": "curtail",
            "asset_id": "ECM97.1",
            "asset_type": "heat_pump",
            "payload": {"dry_run": True},
        }
        result = handler.handle_command(msg)
        assert result is True  # always True, regardless of forwarding outcome

    def test_handle_batch_header_logs_live_when_message_is_live(self):
        mock_logger = MagicMock()
        handler = CommandHandler(mock_logger, force_dry_run=False, target_handler=None)
        msg = {
            "message_type": "batch_start",
            "slot_info": {
                "fsp_id": "supsi01",
                "slot_start": "2026-04-29T07:00:00",
                "slot_end": "2026-04-29T07:15:00",
                "total_flexibility_kw": 0.0,
                "allocation_strategy": "",
                "dry_run": False,
            },
            "command_count": 3,
            "timestamp": "2026-04-29T06:59:13.621188+00:00",
        }

        result = handler.handle_batch_header(msg)

        assert result is True
        mock_logger.info.assert_any_call("%s BATCH START - Expecting %d commands", "[LIVE]", 3)
        mock_logger.info.assert_any_call("  Dry Run:      %s", False)

    def test_handle_batch_header_respects_force_dry_run_override(self):
        mock_logger = MagicMock()
        handler = CommandHandler(mock_logger, force_dry_run=True, target_handler=None)
        msg = {
            "message_type": "batch_start",
            "slot_info": {
                "fsp_id": "supsi01",
                "slot_start": "2026-04-29T07:00:00",
                "slot_end": "2026-04-29T07:15:00",
                "total_flexibility_kw": 0.0,
                "allocation_strategy": "",
                "dry_run": False,
            },
            "command_count": 3,
            "timestamp": "2026-04-29T06:59:13.621188+00:00",
        }

        result = handler.handle_batch_header(msg)

        assert result is True
        mock_logger.info.assert_any_call("%s BATCH START - Expecting %d commands", "[DRY-RUN]", 3)
        mock_logger.info.assert_any_call("  Dry Run:      %s", True)

    def test_handle_measurement_logs_live_when_not_in_dry_run(self):
        mock_logger = MagicMock()
        handler = CommandHandler(mock_logger, force_dry_run=False, target_handler=None)
        msg = {
            "message_type": "measurement",
            "asset_id": "ECM97.1",
            "asset_type": "heat_pump",
            "measurement_type": "power",
            "payload": {"value_kw": 12.5},
            "timestamp": "2026-04-29T06:59:13.621188+00:00",
        }

        result = handler.handle_measurement(msg)

        assert result is True
        mock_logger.info.assert_any_call("%s MEASUREMENT RECEIVED", "[LIVE]")


# =============================================================================
# Body template: power validation in _send_to_target
# =============================================================================

class TestPowerValidation(unittest.TestCase):

    def test_non_bool_power_rejected(self):
        """If body_template produces a non-bool 'power', _send_to_target rejects it."""
        handler = TargetHandler(logger, dry_run=False)
        target = TargetConfig(
            name="test",
            url="https://aem.local:6000",
            body_template={
                "power": {"$map": "payload.some_number"},
                "time": {"$map": "payload.slot_start", "type": "datetime_utc"}
            }
        )
        handler.add_target(target)

        msg = {
            "command_type": "curtail",
            "asset_id": "ECM97.1",
            "asset_type": "heat_pump",
            "payload": {
                "some_number": 42,
                "dry_run": False,
                "slot_start": "2025-01-15T10:00:00Z",
            }
        }
        result = handler.forward_command(msg)
        assert result is False
        assert handler.stats["requests_failed"] == 1

    def test_bool_power_accepted(self):
        """If body_template produces a bool 'power', validation passes."""
        handler = TargetHandler(logger, dry_run=False)
        target = TargetConfig(
            name="test",
            url="https://aem.local:6000",
            body_template={
                "power": {"$map": "payload.discrete_state", "type": "on_off"},
                "time": {"$map": "payload.slot_start", "type": "datetime_utc"}
            }
        )
        handler.add_target(target)

        msg = {
            "command_type": "curtail",
            "asset_id": "ECM97.1",
            "asset_type": "heat_pump",
            "payload": {
                "discrete_state": "OFF",
                "dry_run": False,
                "slot_start": "2025-01-15T10:00:00Z",
            }
        }
        with patch("forwarder.requests") as mock_requests:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.text = '{"status": "ok"}'
            mock_requests.post.return_value = mock_response
            result = handler.forward_command(msg)
            assert result is True
            call_kwargs = mock_requests.post.call_args
            body_sent = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
            assert body_sent["power"] is False


# =============================================================================
# End-to-end: AEM target config rendering
# =============================================================================

class TestAEMConfigEndToEnd(unittest.TestCase):
    """Test with the actual forwarder_targets.json structure."""

    AEM_TARGET = {
        "name": "aem-api",
        "reference_api": "aemAPITest",
        "timeout": 5.0,
        "verify_ssl": False,
        "enabled": True,
        "endpoint_template": "{api.controlUrl}/{payload.community}/{payload.site_id}/{payload.asset_id}",
        "asset_request_profiles": {
            "ECM96.2": {
                "endpoint": "/ECM/ECM96/hp",
                "body_mode": "hp_control"
            },
            "EV01": {
                "endpoint": "/ECM/ECM96/ev",
                "body_mode": "ev_power_timeseries"
            }
        },
        "body_template": {
            "power": {"$map": ["payload.discrete_state", "payload.target_state", "target_state"], "type": "on_off"},
            "time": {"$map": "payload.slot_start", "type": "datetime_utc_future_minute"}
        }
    }

    AEM_API_CONFIG = {
        "controlUrl": "https://aem.test.local",
        "port": 6000,
        "user": "supsi",
        "password": "supsi1234",
    }

    def setUp(self):
        self.utc_now_patcher = patch(
            "forwarder._utc_now_naive",
            return_value=datetime(2025, 1, 1, 0, 0, 0),
        )
        self.utc_now_patcher.start()

    def tearDown(self):
        self.utc_now_patcher.stop()

    def test_discrete_curtail_off(self):
        target = TargetConfig.from_dict(self.AEM_TARGET)
        target.apply_api_config(self.AEM_API_CONFIG)

        msg = {
            "message_type": "command",
            "command_type": "curtail",
            "asset_id": "ECM97.1",
            "asset_type": "heat_pump",
            "payload": {
                "community": "ECM",
                "site_id": "ECM97",
                "asset_id": "ECM97.1",
                "discrete_state": "OFF",
                "slot_start": "2025-06-15T07:00:00+02:00",
                "slot_end": "2025-06-15T07:15:00+02:00",
                "dry_run": False,
            }
        }
        endpoint, body = target.build_request(msg)
        assert endpoint == "https://aem.test.local:6000/ECM/ECM97/ECM97.1"
        assert body == {"power": False, "time": "2025-06-15T05:00:00"}

    def test_discrete_curtail_on(self):
        target = TargetConfig.from_dict(self.AEM_TARGET)
        target.apply_api_config(self.AEM_API_CONFIG)

        msg = {
            "command_type": "curtail",
            "asset_id": "ECM97.1",
            "payload": {
                "community": "ECM",
                "site_id": "ECM97",
                "asset_id": "ECM97.1",
                "discrete_state": "ON",
                "slot_start": "2025-06-15T07:00:00+02:00",
            }
        }
        endpoint, body = target.build_request(msg)
        assert body["power"] is True

    def test_preactivate_with_target_state(self):
        target = TargetConfig.from_dict(self.AEM_TARGET)
        target.apply_api_config(self.AEM_API_CONFIG)

        msg = {
            "command_type": "preactivate",
            "asset_id": "ECM97.1",
            "payload": {
                "community": "ECM",
                "site_id": "ECM97",
                "asset_id": "ECM97.1",
                "target_state": "ON",
                "slot_start": "2025-06-15T04:00:00+02:00",
            }
        }
        endpoint, body = target.build_request(msg)
        assert body["power"] is True

    def test_restore_missing_state(self):
        """Restore commands from flexi_manager don't set discrete_state/target_state."""
        target = TargetConfig.from_dict(self.AEM_TARGET)
        target.apply_api_config(self.AEM_API_CONFIG)

        msg = {
            "command_type": "restore",
            "asset_id": "ECM97.1",
            "payload": {
                "community": "ECM",
                "site_id": "ECM97",
                "asset_id": "ECM97.1",
                "action": "restore",
                "slot_start": "2025-06-15T07:15:00+02:00",
            }
        }
        endpoint, body = target.build_request(msg)
        # power is None because no discrete_state/target_state
        # This confirms the review finding: restore won't produce a valid AEM body
        assert body["power"] is None

    def test_continuous_curtail_no_state(self):
        """Continuous assets (EV chargers) don't set discrete_state."""
        target = TargetConfig.from_dict(self.AEM_TARGET)
        target.apply_api_config(self.AEM_API_CONFIG)

        msg = {
            "command_type": "curtail",
            "asset_id": "ECM63.1",
            "payload": {
                "community": "ECM",
                "site_id": "ECM63",
                "asset_id": "ECM63.1",
                "target_power_kw": 5.0,
                "slot_start": "2025-06-15T07:00:00+02:00",
            }
        }
        endpoint, body = target.build_request(msg)
        # power is None because no discrete_state/target_state/target_state
        assert body["power"] is None

    def test_asset_specific_override_takes_precedence_over_template(self):
        target = TargetConfig.from_dict(self.AEM_TARGET)
        target.apply_api_config({
            "controlUrl": "https://red.aemsa.ch/control",
            "user": "supsi",
            "password": "supsi1234",
        })

        msg = {
            "command_type": "curtail",
            "asset_id": "ECM96.2",
            "payload": {
                "community": "",
                "site_id": "",
                "asset_id": "ECM96.2",
                "discrete_state": "OFF",
                "slot_start": "2025-06-15T07:00:00+02:00",
            }
        }
        endpoint, body = target.build_request(msg)
        assert endpoint == "https://red.aemsa.ch/control/ECM/ECM96/hp"
        assert body == {"power": False, "time": "2025-06-15T05:00:00"}

    def test_asset_profile_ev_schedule_renders_timeseries_body(self):
        target = TargetConfig.from_dict(self.AEM_TARGET)
        target.apply_api_config({
            "controlUrl": "https://red.aemsa.ch/control",
            "user": "supsi",
            "password": "supsi1234",
        })

        msg = {
            "command_type": "charge_schedule",
            "asset_id": "EV01",
            "asset_type": "ev_charger",
            "payload": {
                "schedule": [
                    {"time": "2026-04-27T10:00:00+00:00", "power_kw": 1.0},
                    {"time": "2026-04-27T10:15:00+00:00", "power_kw": -1.0},
                ],
                "dry_run": True,
            }
        }
        endpoint, body = target.build_request(msg)
        assert endpoint == "https://red.aemsa.ch/control/ECM/ECM96/ev"
        assert body == {
            "2026-04-27T10:00:00": 1.0,
            "2026-04-27T10:15:00": -1.0,
        }


if __name__ == "__main__":
    unittest.main()
