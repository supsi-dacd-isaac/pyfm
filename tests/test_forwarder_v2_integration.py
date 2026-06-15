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
            "control_ep": {"method": "POST", "path": "/v1/control"},
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
