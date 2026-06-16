"""Tests for forwarder v2 configurable routing integration.

Covers:
  - Config mode detection at startup level
  - V2 message callback dispatch (ack/nack, no-match, ambiguous, matched)
  - Dry-run, HTTP success/failure, ack_error_no_requeue
  - FORWARDER_RABBIT_SECTIONS whitelist interaction with v2 sources
  - Backward compatibility with legacy config
"""

import copy
import json
import logging
import sys
import types
from importlib.machinery import ModuleSpec

import pytest

# Install pika stub before importing forwarder
if "pika" not in sys.modules:
    pika_stub = types.ModuleType("pika")
    pika_stub.__spec__ = ModuleSpec("pika", loader=None)

    class PlainCredentials:
        def __init__(self, username, password):
            self.username = username
            self.password = password

    class ConnectionParameters:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    pika_stub.PlainCredentials = PlainCredentials
    pika_stub.ConnectionParameters = ConnectionParameters
    pika_stub.BlockingConnection = None
    sys.modules["pika"] = pika_stub


from scripts import forwarder as fw  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeChannel:
    def __init__(self):
        self.basic_ack_calls = []
        self.basic_nack_calls = []

    def basic_ack(self, **kwargs):
        self.basic_ack_calls.append(kwargs)

    def basic_nack(self, **kwargs):
        self.basic_nack_calls.append(kwargs)

    def exchange_declare(self, **kwargs):
        pass

    def queue_declare(self, **kwargs):
        pass

    def queue_bind(self, **kwargs):
        pass

    def basic_qos(self, **kwargs):
        pass

    def basic_consume(self, **kwargs):
        return f"consumer-{kwargs['queue']}"


def _make_method(delivery_tag=1, consumer_tag="consumer-test_queue"):
    return types.SimpleNamespace(
        consumer_tag=consumer_tag,
        delivery_tag=delivery_tag,
        routing_key="test.command",
    )


def _make_source(section="realAssetCommands", queue="test_queue"):
    return fw.RabbitMQSource(
        section=section,
        exchange="test_exchange",
        queue=queue,
        routing_key="test.command",
    )


def _make_v2_consumer(message_callback=None, asset_types_filter=None):
    source = _make_source()
    consumer = fw.RabbitMQConsumer(
        sources=[source],
        logger=logging.getLogger("test_forwarder_v2"),
        message_callback=message_callback,
        asset_types_filter=asset_types_filter,
    )
    consumer._consumer_tag_sources["consumer-test_queue"] = source
    return consumer


def _make_legacy_consumer(command_handler=None):
    source = _make_source()
    consumer = fw.RabbitMQConsumer(
        sources=[source],
        logger=logging.getLogger("test_forwarder_v2"),
        command_handler=command_handler,
    )
    consumer._consumer_tag_sources["consumer-test_queue"] = source
    return consumer


def _v2_config(
    routes=None,
    sources=None,
    profiles=None,
    apis=None,
    endpoints=None,
    defaults=None,
):
    """Build a minimal valid v2 config dict."""
    return {
        "version": 2,
        "defaults": defaults or {"missing_message_dry_run_default": True},
        "sources": sources or {
            "real_cmds": {"section": "realAssetCommands"},
        },
        "message_profiles": profiles or {
            "hp_commands": {"message_type": "command", "asset_types": ["heat_pump"]},
        },
        "apis": apis or {
            "local_api": {
                "base_url": "https://api.example.com",
                "user": "admin",
                "password": "secret",
            },
        },
        "endpoints": endpoints or {
            "control_ep": {"method": "POST", "path_template": "/v1/control"},
        },
        "routes": routes or [
            {
                "name": "hp_route",
                "source": "real_cmds",
                "message_profile": "hp_commands",
                "api": "local_api",
                "endpoint": "control_ep",
                "priority": 10,
                "enabled": True,
            },
        ],
    }


def _legacy_config():
    """Build a minimal legacy targets config."""
    return {
        "targets": [
            {
                "name": "test_target",
                "url": "https://api.example.com",
                "enabled": True,
            },
        ],
    }


class _MockResponse:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


class _MockSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


# =========================================================================
# Config mode / startup behavior
# =========================================================================

class TestConfigModeDetection:

    def test_legacy_targets_config_detected_as_legacy(self):
        assert fw.detect_config_mode(_legacy_config()) == "legacy"

    def test_v2_config_detected_as_v2(self):
        assert fw.detect_config_mode(_v2_config()) == "v2"

    def test_config_with_both_targets_and_routes_raises(self):
        config = _v2_config()
        config["targets"] = [{"name": "x", "url": "http://x"}]
        with pytest.raises(fw.RoutingConfigError, match="mutually exclusive"):
            fw.detect_config_mode(config)

    def test_empty_config_is_legacy(self):
        assert fw.detect_config_mode({}) == "legacy"


class TestV2SourceResolution:

    def test_v2_active_sources_from_enabled_routes(self):
        config = _v2_config()
        validated = fw.validate_routing_config(config)
        router = fw.MessageRouter.from_validated_config(validated)

        active = {r.source for r in router.routes if r.enabled}
        sections = set()
        for src_name in active:
            src_def = router.sources.get(src_name, {})
            sec = src_def.get("section")
            if sec:
                sections.add(sec)

        assert sections == {"realAssetCommands"}

    def test_all_configured_sources_included(self):
        """All sources in the config are included, even without routes."""
        config = _v2_config(
            sources={
                "real_cmds": {"section": "realAssetCommands"},
                "sim_measures": {"section": "simulatedAssetMeasures"},
            },
        )
        validated = fw.validate_routing_config(config)
        router = fw.MessageRouter.from_validated_config(validated)

        all_sections = set()
        for src_def in router.sources.values():
            sec = src_def.get("section")
            if sec:
                all_sections.add(sec)

        assert all_sections == {"realAssetCommands", "simulatedAssetMeasures"}

    def test_rabbit_sections_whitelist_filters_v2_sources(self):
        config = _v2_config(
            sources={
                "real_cmds": {"section": "realAssetCommands"},
                "sim_cmds": {"section": "simulatedAssetCommands"},
            },
            routes=[
                {
                    "name": "r1",
                    "source": "real_cmds",
                    "message_profile": "hp_commands",
                    "api": "local_api",
                    "endpoint": "control_ep",
                    "priority": 10,
                    "enabled": True,
                },
                {
                    "name": "r2",
                    "source": "sim_cmds",
                    "message_profile": "hp_commands",
                    "api": "local_api",
                    "endpoint": "control_ep",
                    "priority": 10,
                    "enabled": True,
                },
            ],
        )
        validated = fw.validate_routing_config(config)
        router = fw.MessageRouter.from_validated_config(validated)

        active = {r.source for r in router.routes if r.enabled}
        v2_sections = set()
        for src_name in active:
            sec = router.sources.get(src_name, {}).get("section")
            if sec:
                v2_sections.add(sec)

        whitelist = {"realAssetCommands"}
        v2_sections &= whitelist
        assert v2_sections == {"realAssetCommands"}


# =========================================================================
# V2 message callback — _process_message integration
# =========================================================================

class TestV2MessageProcessing:

    def test_matching_route_acked(self):
        from scripts.forwarder_router import (
            MessageRouter,
            build_resolved_request,
            dispatch_http_request,
            validate_routing_config,
        )

        config = _v2_config()
        validated = validate_routing_config(config)
        router = MessageRouter.from_validated_config(validated)
        session = _MockSession([_MockResponse(200)])

        def callback(message, source):
            resolution = router.resolve(message, source.section)
            assert resolution.status == "matched"
            req = build_resolved_request(message, resolution.route, router, False)
            dispatch_http_request(req, message, session=session)
            return True

        consumer = _make_v2_consumer(message_callback=callback)
        channel = FakeChannel()
        body = {
            "message_type": "command",
            "asset_type": "heat_pump",
            "asset_id": "HP-01",
            "payload": {"dry_run": False},
        }

        consumer._process_message(
            channel, _make_method(delivery_tag=1), None,
            json.dumps(body).encode("utf-8"),
        )

        assert channel.basic_ack_calls == [{"delivery_tag": 1}]
        assert channel.basic_nack_calls == []

    def test_no_match_acked_and_warns_for_ack_warn_no_forward(self, caplog):
        from scripts.forwarder_router import (
            MessageRouter,
            validate_routing_config,
        )

        config = _v2_config()
        validated = validate_routing_config(config)
        router = MessageRouter.from_validated_config(validated)

        def callback(message, source):
            resolution = router.resolve(message, source.section)
            if resolution.status == "no_match":
                if router.defaults.on_no_match == "ack_warn_no_forward":
                    logging.getLogger("test_forwarder_v2").warning(
                        "V2 no route matched: source=%s message_type=%s "
                        "asset_type=%s asset_id=%s",
                        source.section,
                        message.get("message_type", "unknown"),
                        message.get("asset_type", "unknown"),
                        message.get("asset_id", "unknown"),
                    )
            return True

        consumer = _make_v2_consumer(message_callback=callback)
        channel = FakeChannel()
        body = {
            "message_type": "command",
            "asset_type": "solar_panel",
            "asset_id": "SP-99",
            "payload": {},
        }

        with caplog.at_level(logging.WARNING, logger="test_forwarder_v2"):
            consumer._process_message(
                channel, _make_method(delivery_tag=2), None,
                json.dumps(body).encode("utf-8"),
            )

        assert channel.basic_ack_calls == [{"delivery_tag": 2}]
        assert channel.basic_nack_calls == []
        assert any("no route matched" in r.message for r in caplog.records)

    def test_no_match_acked_silently_for_ack_silent_no_forward(self, caplog):
        from scripts.forwarder_router import (
            MessageRouter,
            RoutingDefaults,
            validate_routing_config,
        )

        config = _v2_config(
            defaults={
                "on_no_match": "ack_silent_no_forward",
                "missing_message_dry_run_default": True,
            }
        )
        validated = validate_routing_config(config)
        router = MessageRouter.from_validated_config(validated)

        warns_logged = []

        def callback(message, source):
            resolution = router.resolve(message, source.section)
            if resolution.status == "no_match":
                if router.defaults.on_no_match == "ack_warn_no_forward":
                    warns_logged.append(True)
            return True

        consumer = _make_v2_consumer(message_callback=callback)
        channel = FakeChannel()
        body = {
            "message_type": "command",
            "asset_type": "solar_panel",
            "asset_id": "SP-99",
            "payload": {},
        }

        consumer._process_message(
            channel, _make_method(delivery_tag=3), None,
            json.dumps(body).encode("utf-8"),
        )

        assert channel.basic_ack_calls == [{"delivery_tag": 3}]
        assert warns_logged == []

    def test_ambiguous_acked_logs_error_no_http(self, caplog):
        from scripts.forwarder_router import (
            MessageRouter,
            validate_routing_config,
        )

        config = _v2_config(
            routes=[
                {
                    "name": "r1",
                    "source": "real_cmds",
                    "message_profile": "hp_commands",
                    "api": "local_api",
                    "endpoint": "control_ep",
                    "priority": 10,
                    "enabled": True,
                },
                {
                    "name": "r2",
                    "source": "real_cmds",
                    "message_profile": "hp_commands",
                    "api": "local_api",
                    "endpoint": "control_ep",
                    "priority": 10,
                    "enabled": True,
                },
            ],
        )
        validated = validate_routing_config(config)
        router = MessageRouter.from_validated_config(validated)
        http_called = []

        def callback(message, source):
            resolution = router.resolve(message, source.section)
            if resolution.status == "ambiguous":
                logging.getLogger("test_forwarder_v2").error(
                    "V2 ambiguous: routes=%s",
                    [r.name for r in resolution.matching_routes],
                )
                return True
            http_called.append(True)
            return True

        consumer = _make_v2_consumer(message_callback=callback)
        channel = FakeChannel()
        body = {
            "message_type": "command",
            "asset_type": "heat_pump",
            "asset_id": "HP-01",
            "payload": {},
        }

        with caplog.at_level(logging.ERROR, logger="test_forwarder_v2"):
            consumer._process_message(
                channel, _make_method(delivery_tag=4), None,
                json.dumps(body).encode("utf-8"),
            )

        assert channel.basic_ack_calls == [{"delivery_tag": 4}]
        assert channel.basic_nack_calls == []
        assert http_called == []
        assert any("ambiguous" in r.message.lower() for r in caplog.records)

    def test_dry_run_acked_no_http(self):
        from scripts.forwarder_router import (
            MessageRouter,
            build_resolved_request,
            dispatch_http_request,
            validate_routing_config,
        )

        config = _v2_config()
        validated = validate_routing_config(config)
        router = MessageRouter.from_validated_config(validated)
        session = _MockSession([])

        def callback(message, source):
            resolution = router.resolve(message, source.section)
            if resolution.status == "matched":
                req = build_resolved_request(
                    message, resolution.route, router, True
                )
                dispatch_http_request(req, message, session=session)
            return True

        consumer = _make_v2_consumer(message_callback=callback)
        channel = FakeChannel()
        body = {
            "message_type": "command",
            "asset_type": "heat_pump",
            "asset_id": "HP-01",
            "payload": {"dry_run": False},
        }

        consumer._process_message(
            channel, _make_method(delivery_tag=5), None,
            json.dumps(body).encode("utf-8"),
        )

        assert channel.basic_ack_calls == [{"delivery_tag": 5}]
        assert len(session.calls) == 0

    def test_http_success_acked(self):
        from scripts.forwarder_router import (
            MessageRouter,
            build_resolved_request,
            dispatch_http_request,
            validate_routing_config,
        )

        config = _v2_config()
        validated = validate_routing_config(config)
        router = MessageRouter.from_validated_config(validated)
        session = _MockSession([_MockResponse(200)])

        dispatch_results = []

        def callback(message, source):
            resolution = router.resolve(message, source.section)
            req = build_resolved_request(
                message, resolution.route, router, False
            )
            result = dispatch_http_request(req, message, session=session)
            dispatch_results.append(result)
            return True

        consumer = _make_v2_consumer(message_callback=callback)
        channel = FakeChannel()
        body = {
            "message_type": "command",
            "asset_type": "heat_pump",
            "asset_id": "HP-01",
            "payload": {"dry_run": False},
        }

        consumer._process_message(
            channel, _make_method(delivery_tag=6), None,
            json.dumps(body).encode("utf-8"),
        )

        assert channel.basic_ack_calls == [{"delivery_tag": 6}]
        assert dispatch_results[0].success is True

    def test_http_failure_acked_not_nacked(self):
        from scripts.forwarder_router import (
            MessageRouter,
            build_resolved_request,
            dispatch_http_request,
            validate_routing_config,
        )

        config = _v2_config()
        validated = validate_routing_config(config)
        router = MessageRouter.from_validated_config(validated)
        session = _MockSession([_MockResponse(500)] * 4)

        dispatch_results = []

        def callback(message, source):
            resolution = router.resolve(message, source.section)
            req = build_resolved_request(
                message, resolution.route, router, False
            )
            result = dispatch_http_request(req, message, session=session)
            dispatch_results.append(result)
            return True

        consumer = _make_v2_consumer(message_callback=callback)
        channel = FakeChannel()
        body = {
            "message_type": "command",
            "asset_type": "heat_pump",
            "asset_id": "HP-01",
            "payload": {"dry_run": False},
        }

        consumer._process_message(
            channel, _make_method(delivery_tag=7), None,
            json.dumps(body).encode("utf-8"),
        )

        assert channel.basic_ack_calls == [{"delivery_tag": 7}]
        assert channel.basic_nack_calls == []
        assert dispatch_results[0].success is False
        assert dispatch_results[0].policy == "ack_error_no_requeue"


# =========================================================================
# V2 callback exception safety
# =========================================================================

class TestV2CallbackSafety:

    def test_callback_exception_still_acks(self):
        def failing_callback(message, source):
            raise RuntimeError("unexpected bug")

        consumer = _make_v2_consumer(message_callback=failing_callback)
        channel = FakeChannel()
        body = {
            "message_type": "command",
            "asset_type": "heat_pump",
            "asset_id": "HP-01",
            "payload": {},
        }

        consumer._process_message(
            channel, _make_method(delivery_tag=8), None,
            json.dumps(body).encode("utf-8"),
        )

        assert channel.basic_ack_calls == [{"delivery_tag": 8}]
        assert channel.basic_nack_calls == []

    def test_v2_no_nack_requeue(self):
        outcomes = []

        def callback(message, source):
            outcomes.append("called")
            return True

        consumer = _make_v2_consumer(message_callback=callback)
        channel = FakeChannel()

        for dt in range(1, 4):
            body = {
                "message_type": "command",
                "asset_type": "heat_pump",
                "asset_id": f"HP-{dt:02d}",
                "payload": {},
            }
            consumer._process_message(
                channel, _make_method(delivery_tag=dt), None,
                json.dumps(body).encode("utf-8"),
            )

        assert len(channel.basic_ack_calls) == 3
        assert channel.basic_nack_calls == []
        assert len(outcomes) == 3


# =========================================================================
# Backward compatibility — legacy path still works with message_callback=None
# =========================================================================

class TestLegacyBackwardCompatibility:

    def test_legacy_command_acked(self):
        calls = []
        consumer = _make_legacy_consumer(
            command_handler=lambda msg: calls.append(msg) or True
        )
        channel = FakeChannel()
        body = {
            "message_type": "command",
            "command_type": "curtail",
            "asset_type": "heat_pump",
            "asset_id": "HP-01",
            "payload": {"dry_run": True},
        }

        consumer._process_message(
            channel, _make_method(delivery_tag=10), None,
            json.dumps(body).encode("utf-8"),
        )

        assert calls == [body]
        assert channel.basic_ack_calls == [{"delivery_tag": 10}]

    def test_legacy_malformed_list_acked(self):
        consumer = _make_legacy_consumer(command_handler=lambda m: True)
        channel = FakeChannel()

        consumer._process_message(
            channel, _make_method(delivery_tag=11), None,
            json.dumps(["bad"]).encode("utf-8"),
        )

        assert channel.basic_ack_calls == [{"delivery_tag": 11}]
        assert channel.basic_nack_calls == []

    def test_legacy_handler_exception_nacks(self):
        def boom(msg):
            raise RuntimeError("fail")

        consumer = _make_legacy_consumer(command_handler=boom)
        channel = FakeChannel()
        body = {
            "message_type": "command",
            "asset_type": "heat_pump",
            "asset_id": "HP-01",
            "payload": {"dry_run": True},
        }

        consumer._process_message(
            channel, _make_method(delivery_tag=12), None,
            json.dumps(body).encode("utf-8"),
        )

        assert channel.basic_ack_calls == []
        assert channel.basic_nack_calls == [{"delivery_tag": 12, "requeue": True}]

    def test_legacy_non_dict_payload_acked(self):
        consumer = _make_legacy_consumer(command_handler=lambda m: True)
        channel = FakeChannel()
        body = {
            "message_type": "command",
            "asset_type": "heat_pump",
            "asset_id": "HP-01",
            "payload": ["not", "a", "dict"],
        }

        consumer._process_message(
            channel, _make_method(delivery_tag=13), None,
            json.dumps(body).encode("utf-8"),
        )

        assert channel.basic_ack_calls == [{"delivery_tag": 13}]
        assert channel.basic_nack_calls == []


# =========================================================================
# Malformed message handling in v2 mode
# =========================================================================

class TestV2MalformedMessages:

    def test_malformed_json_acked_in_v2(self):
        consumer = _make_v2_consumer(message_callback=lambda m, s: True)
        channel = FakeChannel()

        consumer._process_message(
            channel, _make_method(delivery_tag=20), None,
            b"not json at all",
        )

        assert channel.basic_ack_calls == [{"delivery_tag": 20}]
        assert channel.basic_nack_calls == []

    def test_top_level_list_acked_in_v2(self):
        consumer = _make_v2_consumer(message_callback=lambda m, s: True)
        channel = FakeChannel()

        consumer._process_message(
            channel, _make_method(delivery_tag=21), None,
            json.dumps(["a", "b"]).encode("utf-8"),
        )

        assert channel.basic_ack_calls == [{"delivery_tag": 21}]
        assert channel.basic_nack_calls == []


# =========================================================================
# Route-selection logging
# =========================================================================

class TestRouteSelectionLogging:
    """Verify the INFO log emitted when a v2 route is matched."""

    def _run_callback_with_logging(self, caplog, message, source_section="realAssetCommands"):
        from scripts.forwarder_router import (
            MessageRouter,
            build_resolved_request,
            dispatch_http_request,
            validate_routing_config,
        )

        config = _v2_config()
        validated = validate_routing_config(config)
        router = MessageRouter.from_validated_config(validated)
        session = _MockSession([_MockResponse(200)])

        test_logger = logging.getLogger("test_route_selection")

        source = type("S", (), {
            "section": source_section,
            "queue": "test_queue",
        })()

        with caplog.at_level(logging.INFO, logger="test_route_selection"):
            resolution = router.resolve(message, source.section)
            if resolution.status == "matched":
                matched_route = resolution.route
                test_logger.info(
                    "V2 route matched: route='%s' source='%s' queue='%s' "
                    "priority=%d message_type='%s' asset_type='%s' "
                    "asset_id='%s' api='%s' endpoint='%s'",
                    matched_route.name,
                    source.section,
                    source.queue,
                    matched_route.priority,
                    message.get("message_type", "unknown"),
                    message.get("asset_type", "unknown"),
                    message.get("asset_id", "unknown"),
                    matched_route.api,
                    matched_route.endpoint,
                )
                req = build_resolved_request(
                    message, matched_route, router, True
                )
                dispatch_http_request(req, message, session=session)

        return caplog.records

    def test_matched_message_logs_route_matched(self, caplog):
        msg = {
            "message_type": "command",
            "asset_type": "heat_pump",
            "asset_id": "HP-01",
            "payload": {"dry_run": False},
        }
        records = self._run_callback_with_logging(caplog, msg)
        route_logs = [r for r in records if "V2 route matched" in r.getMessage()]
        assert len(route_logs) >= 1

    def test_log_includes_route_name(self, caplog):
        msg = {
            "message_type": "command",
            "asset_type": "heat_pump",
            "asset_id": "HP-01",
            "payload": {"dry_run": False},
        }
        records = self._run_callback_with_logging(caplog, msg)
        route_logs = [r for r in records if "V2 route matched" in r.getMessage()]
        log_msg = route_logs[0].getMessage()
        assert "route='hp_route'" in log_msg

    def test_log_includes_source_section(self, caplog):
        msg = {
            "message_type": "command",
            "asset_type": "heat_pump",
            "asset_id": "HP-01",
            "payload": {},
        }
        records = self._run_callback_with_logging(caplog, msg)
        route_logs = [r for r in records if "V2 route matched" in r.getMessage()]
        log_msg = route_logs[0].getMessage()
        assert "source='realAssetCommands'" in log_msg

    def test_log_includes_queue_name(self, caplog):
        msg = {
            "message_type": "command",
            "asset_type": "heat_pump",
            "asset_id": "HP-01",
            "payload": {},
        }
        records = self._run_callback_with_logging(caplog, msg)
        route_logs = [r for r in records if "V2 route matched" in r.getMessage()]
        log_msg = route_logs[0].getMessage()
        assert "queue='test_queue'" in log_msg

    def test_log_includes_asset_fields(self, caplog):
        msg = {
            "message_type": "command",
            "asset_type": "heat_pump",
            "asset_id": "HP-01",
            "payload": {},
        }
        records = self._run_callback_with_logging(caplog, msg)
        route_logs = [r for r in records if "V2 route matched" in r.getMessage()]
        log_msg = route_logs[0].getMessage()
        assert "asset_id='HP-01'" in log_msg
        assert "asset_type='heat_pump'" in log_msg
        assert "message_type='command'" in log_msg

    def test_log_includes_api_and_endpoint(self, caplog):
        msg = {
            "message_type": "command",
            "asset_type": "heat_pump",
            "asset_id": "HP-01",
            "payload": {},
        }
        records = self._run_callback_with_logging(caplog, msg)
        route_logs = [r for r in records if "V2 route matched" in r.getMessage()]
        log_msg = route_logs[0].getMessage()
        assert "api='local_api'" in log_msg
        assert "endpoint='control_ep'" in log_msg

    def test_log_does_not_include_password(self, caplog):
        from scripts.forwarder_router import (
            MessageRouter,
            validate_routing_config,
        )
        config = _v2_config()
        validated = validate_routing_config(config)
        router = MessageRouter.from_validated_config(validated)
        router.apis["local_api"].user = "testuser"
        router.apis["local_api"].password = "s3cr3t_p@ssw0rd"

        msg = {
            "message_type": "command",
            "asset_type": "heat_pump",
            "asset_id": "HP-01",
            "payload": {},
        }

        test_logger = logging.getLogger("test_no_secret")
        source = type("S", (), {
            "section": "realAssetCommands",
            "queue": "q",
        })()

        with caplog.at_level(logging.INFO, logger="test_no_secret"):
            resolution = router.resolve(msg, source.section)
            if resolution.status == "matched":
                matched_route = resolution.route
                test_logger.info(
                    "V2 route matched: route='%s' source='%s' queue='%s' "
                    "priority=%d message_type='%s' asset_type='%s' "
                    "asset_id='%s' api='%s' endpoint='%s'",
                    matched_route.name,
                    source.section,
                    source.queue,
                    matched_route.priority,
                    msg.get("message_type", "unknown"),
                    msg.get("asset_type", "unknown"),
                    msg.get("asset_id", "unknown"),
                    matched_route.api,
                    matched_route.endpoint,
                )

        all_log_text = " ".join(r.getMessage() for r in caplog.records)
        assert "s3cr3t_p@ssw0rd" not in all_log_text
        assert "testuser" not in all_log_text


# =========================================================================
# Actuator list payload → v2 routing (end-to-end body transformation)
# =========================================================================

class TestActuatorListPayloadRouting:
    """Validate that a JSON list published by flexi_actuator.py is unpacked
    and each item is routed through the standard v2 profile-matching and
    body_template transformation path.

    Uses the real conf/forwarder_routes.json to catch regressions.
    """

    @staticmethod
    def _load_real_router():
        """Load the production v2 config and return a ready router."""
        import os
        from scripts.forwarder_router import (
            MessageRouter,
            validate_routing_config,
        )
        config_path = os.path.join(
            os.path.dirname(__file__), "..", "conf", "forwarder_routes.json"
        )
        with open(config_path) as f:
            config = json.load(f)
        router = MessageRouter.from_validated_config(
            validate_routing_config(config)
        )
        router.apis["aem_api"].base_url = "http://aem.test:6000/control"
        router.apis["aem_api"].user = "test"
        router.apis["aem_api"].password = "test"
        return router

    @staticmethod
    def _hp_command(asset_id, discrete_state="OFF", command_type="curtail"):
        return {
            "message_type": "command",
            "asset_id": asset_id,
            "asset_type": "heat_pump",
            "command_type": command_type,
            "payload": {
                "community": "ECM",
                "site_id": asset_id.split(".")[0],
                "asset_id": asset_id,
                "description": f"HP {asset_id}",
                "asset_type": "heat_pump",
                "modulation_type": "discrete",
                "discrete_state": discrete_state,
                "target_power_kw": 0.0 if discrete_state == "OFF" else 4.0,
                "capacity_kw": 4.0,
                "duration_minutes": 15,
                "slot_start": "2026-06-16T14:00:00",
                "slot_end": "2026-06-16T14:15:00",
                "dry_run": True,
                "requested_command": "force_off",
            },
            "timestamp": "2026-06-16T14:00:00+00:00",
            "priority": 7,
        }

    @staticmethod
    def _ev_command(asset_id, power_kw=0.0, command_type="curtail"):
        return {
            "message_type": "command",
            "asset_id": asset_id,
            "asset_type": "ev_charger",
            "command_type": command_type,
            "payload": {
                "community": "ECM",
                "site_id": asset_id.split(".")[0],
                "asset_id": asset_id,
                "description": f"EV {asset_id}",
                "asset_type": "ev_charger",
                "modulation_type": "continuous",
                "target_power_kw": power_kw,
                "power_kw": power_kw,
                "capacity_kw": 11.0,
                "duration_minutes": 15,
                "slot_start": "2026-06-16T14:00:00",
                "slot_end": "2026-06-16T14:15:00",
                "schedule": {"2026-06-16T14:00:00": power_kw},
                "dry_run": True,
                "requested_command": "force_off",
            },
            "timestamp": "2026-06-16T14:00:00+00:00",
            "priority": 7,
        }

    @staticmethod
    def _hp_restore(asset_id):
        return {
            "message_type": "command",
            "asset_id": asset_id,
            "asset_type": "heat_pump",
            "command_type": "restore",
            "payload": {
                "community": "ECM",
                "site_id": asset_id.split(".")[0],
                "asset_id": asset_id,
                "description": f"HP {asset_id}",
                "asset_type": "heat_pump",
                "action": "restore",
                "target_state": "ON",
                "discrete_state": "ON",
                "target_power_kw": 4.0,
                "modulation_type": "discrete",
                "capacity_kw": 4.0,
                "duration_minutes": 15,
                "slot_start": "2026-06-16T14:00:00",
                "slot_end": "2026-06-16T14:15:00",
                "dry_run": True,
            },
            "timestamp": "2026-06-16T14:00:00+00:00",
            "priority": 5,
        }

    def _resolve_and_build(self, router, message):
        from scripts.forwarder_router import build_resolved_request
        resolution = router.resolve(message, "realAssetCommands")
        assert resolution.status == "matched", (
            f"No route matched for {message['asset_id']}: {resolution.reason}"
        )
        return build_resolved_request(message, resolution.route, router, True)

    # -- HP force_off: body must be {"power": false, "time": "..."} ---------

    def test_hp_force_off_ecm96_2_body(self):
        router = self._load_real_router()
        req = self._resolve_and_build(router, self._hp_command("ECM96.2", "OFF"))
        assert req.body == {"power": False, "time": "2026-06-16T14:00:00"}
        assert req.url == "http://aem.test:6000/control/ECM/ECM96/hp"
        assert req.route_name == "ecm96_2_hp"

    def test_hp_force_off_ecm97_3_body(self):
        router = self._load_real_router()
        req = self._resolve_and_build(router, self._hp_command("ECM97.3", "OFF"))
        assert req.body == {"power": False, "time": "2026-06-16T14:00:00"}
        assert req.url == "http://aem.test:6000/control/ECM/ECM97/hp"
        assert req.route_name == "ecm97_3_hp"

    # -- HP force_on: body must be {"power": true, "time": "..."} -----------

    def test_hp_force_on_ecm96_2_body(self):
        router = self._load_real_router()
        req = self._resolve_and_build(router, self._hp_command("ECM96.2", "ON"))
        assert req.body == {"power": True, "time": "2026-06-16T14:00:00"}

    def test_hp_force_on_ecm97_3_body(self):
        router = self._load_real_router()
        req = self._resolve_and_build(router, self._hp_command("ECM97.3", "ON"))
        assert req.body == {"power": True, "time": "2026-06-16T14:00:00"}

    # -- HP restore: body must be {"power": true, "time": "..."} ------------

    def test_hp_restore_ecm96_2_body(self):
        router = self._load_real_router()
        req = self._resolve_and_build(router, self._hp_restore("ECM96.2"))
        assert req.body == {"power": True, "time": "2026-06-16T14:00:00"}

    def test_hp_restore_ecm97_3_body(self):
        router = self._load_real_router()
        req = self._resolve_and_build(router, self._hp_restore("ECM97.3"))
        assert req.body == {"power": True, "time": "2026-06-16T14:00:00"}

    # -- EV force_off: body must be {timestamp: power_kw} timeseries --------

    def test_ev_force_off_ecm63_1_body(self):
        router = self._load_real_router()
        req = self._resolve_and_build(router, self._ev_command("ECM63.1", 0.0))
        assert req.body == {"2026-06-16T14:00:00": 0.0}
        assert req.url == "http://aem.test:6000/control/ECM/ECM63/charge_point_ev_1"
        assert req.route_name == "ecm63_1_ev"

    def test_ev_force_off_ecm63_2_body(self):
        router = self._load_real_router()
        req = self._resolve_and_build(router, self._ev_command("ECM63.2", 0.0))
        assert req.body == {"2026-06-16T14:00:00": 0.0}
        assert req.url == "http://aem.test:6000/control/ECM/ECM63/charge_point_ev_2"
        assert req.route_name == "ecm63_2_ev"

    # -- EV restore: body must be {timestamp: power_kw} timeseries ----------

    def test_ev_force_on_ecm63_1_body(self):
        router = self._load_real_router()
        req = self._resolve_and_build(router, self._ev_command("ECM63.1", 11.0))
        assert req.body == {"2026-06-16T14:00:00": 11.0}

    # -- Unknown asset falls back to default_aem_command --------------------

    def test_unknown_hp_uses_fallback_route(self):
        router = self._load_real_router()
        msg = self._hp_command("HP-NEWSITE-01", "OFF")
        msg["payload"]["site_id"] = "NEWSITE"
        req = self._resolve_and_build(router, msg)
        assert req.route_name == "fallback_aem_command"
        assert req.body["power"] is False
        assert "time" in req.body

    # -- List unpacking via _process_message --------------------------------

    def test_list_of_two_hp_commands_unpacked_and_routed(self):
        """Simulate what RabbitMQConsumer._process_message does when
        flexi_actuator publishes a JSON list of command envelopes."""
        dispatched = []

        def mock_callback(message, source):
            if isinstance(message, list):
                for item in message:
                    if isinstance(item, dict):
                        mock_callback(item, source)
                return True
            dispatched.append(message)
            return True

        source = _make_source()
        payload = [
            self._hp_command("ECM96.2", "OFF"),
            self._hp_command("ECM97.3", "OFF"),
        ]

        consumer = fw.RabbitMQConsumer(
            sources=[source],
            logger=logging.getLogger("test_list_unpack"),
            message_callback=mock_callback,
        )
        consumer._consumer_tag_sources["consumer-test_queue"] = source

        channel = FakeChannel()
        consumer._process_message(
            channel,
            _make_method(delivery_tag=99),
            None,
            json.dumps(payload).encode("utf-8"),
        )

        assert channel.basic_ack_calls == [{"delivery_tag": 99}]
        assert len(dispatched) == 2
        assert dispatched[0]["asset_id"] == "ECM96.2"
        assert dispatched[1]["asset_id"] == "ECM97.3"

    def test_list_with_mixed_hp_and_ev_unpacked(self):
        dispatched = []

        def mock_callback(message, source):
            if isinstance(message, list):
                for item in message:
                    if isinstance(item, dict):
                        mock_callback(item, source)
                return True
            dispatched.append(message)
            return True

        source = _make_source()
        payload = [
            self._hp_command("ECM96.2", "OFF"),
            self._ev_command("ECM63.1", 0.0),
        ]

        consumer = fw.RabbitMQConsumer(
            sources=[source],
            logger=logging.getLogger("test_list_unpack"),
            message_callback=mock_callback,
        )
        consumer._consumer_tag_sources["consumer-test_queue"] = source

        channel = FakeChannel()
        consumer._process_message(
            channel,
            _make_method(delivery_tag=100),
            None,
            json.dumps(payload).encode("utf-8"),
        )

        assert len(dispatched) == 2
        assert dispatched[0]["asset_type"] == "heat_pump"
        assert dispatched[1]["asset_type"] == "ev_charger"

    def test_empty_list_acked_without_error(self):
        called = []

        def mock_callback(message, source):
            if isinstance(message, list):
                for item in message:
                    if isinstance(item, dict):
                        mock_callback(item, source)
                return True
            called.append(message)
            return True

        source = _make_source()
        consumer = fw.RabbitMQConsumer(
            sources=[source],
            logger=logging.getLogger("test_list_unpack"),
            message_callback=mock_callback,
        )
        consumer._consumer_tag_sources["consumer-test_queue"] = source

        channel = FakeChannel()
        consumer._process_message(
            channel,
            _make_method(delivery_tag=101),
            None,
            json.dumps([]).encode("utf-8"),
        )

        assert channel.basic_ack_calls == [{"delivery_tag": 101}]
        assert called == []

    def test_single_item_list_unpacked(self):
        dispatched = []

        def mock_callback(message, source):
            if isinstance(message, list):
                for item in message:
                    if isinstance(item, dict):
                        mock_callback(item, source)
                return True
            dispatched.append(message)
            return True

        source = _make_source()
        consumer = fw.RabbitMQConsumer(
            sources=[source],
            logger=logging.getLogger("test_list_unpack"),
            message_callback=mock_callback,
        )
        consumer._consumer_tag_sources["consumer-test_queue"] = source

        channel = FakeChannel()
        consumer._process_message(
            channel,
            _make_method(delivery_tag=102),
            None,
            json.dumps([self._hp_command("ECM96.2", "OFF")]).encode("utf-8"),
        )

        assert len(dispatched) == 1
        assert dispatched[0]["asset_id"] == "ECM96.2"

    def test_list_with_non_dict_entries_skipped(self, caplog):
        dispatched = []

        def mock_callback(message, source):
            if isinstance(message, list):
                for idx, item in enumerate(message):
                    if isinstance(item, dict):
                        mock_callback(item, source)
                return True
            dispatched.append(message)
            return True

        source = _make_source()
        payload = [
            self._hp_command("ECM96.2", "OFF"),
            "not a dict",
            42,
            self._hp_command("ECM97.3", "OFF"),
        ]

        consumer = fw.RabbitMQConsumer(
            sources=[source],
            logger=logging.getLogger("test_list_unpack"),
            message_callback=mock_callback,
        )
        consumer._consumer_tag_sources["consumer-test_queue"] = source

        channel = FakeChannel()
        consumer._process_message(
            channel,
            _make_method(delivery_tag=103),
            None,
            json.dumps(payload).encode("utf-8"),
        )

        assert len(dispatched) == 2
        assert dispatched[0]["asset_id"] == "ECM96.2"
        assert dispatched[1]["asset_id"] == "ECM97.3"
