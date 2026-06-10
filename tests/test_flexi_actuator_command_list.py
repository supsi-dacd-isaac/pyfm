import json
import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from scripts import flexi_actuator as actuator  # noqa: E402


def _sample_command(asset_id: str) -> dict:
    return {
        "asset_id": asset_id,
        "asset_type": "heat_pump",
        "command_type": "curtail",
        "payload": {"asset_id": asset_id, "requested_command": "force_on"},
        "priority": 7,
        "rabbitmq_destination": {
            "exchange": "flexi_sim_commands",
            "queue": "flexi_sim_commands_queue",
            "routing_key": "sim_asset.command",
        },
    }


def test_build_rabbitmq_command_envelopes_preserves_schema():
    commands = [_sample_command("ECM68.3"), _sample_command("ECM68.4")]
    envelopes = actuator._build_rabbitmq_command_envelopes(commands)

    assert len(envelopes) == 2
    for envelope, command in zip(envelopes, commands):
        assert envelope["message_type"] == "command"
        assert envelope["asset_id"] == command["asset_id"]
        assert envelope["asset_type"] == command["asset_type"]
        assert envelope["command_type"] == command["command_type"]
        assert envelope["payload"] == command["payload"]
        assert envelope["priority"] == command["priority"]
        assert "timestamp" in envelope


def test_log_rabbitmq_messages_emits_json_list(caplog):
    """Dry-run log emits one JSON list per destination."""
    commands = [_sample_command("ECM68.3"), _sample_command("ECM68.4")]
    logger = logging.getLogger("test_flexi_actuator_command_list")

    with caplog.at_level(logging.INFO):
        actuator._log_rabbitmq_messages(commands=commands, slot_info=None, logger=logger)

    command_logs = [
        record.message
        for record in caplog.records
        if record.message.startswith("RabbitMQ command JSON")
    ]
    assert len(command_logs) == 1

    payload_text = command_logs[0].split(": ", 1)[-1]
    payload = json.loads(payload_text)
    assert isinstance(payload, list)
    assert len(payload) == 2
    assert {item["asset_id"] for item in payload} == {"ECM68.3", "ECM68.4"}


def test_log_rabbitmq_messages_no_batch_header_published(caplog):
    """Even with slot_info, no batch_start header is logged as a RabbitMQ message."""
    commands = [_sample_command("ECM68.3")]
    slot_info = {"fsp_id": "test", "slot_start": "2026-01-01T00:00:00"}
    logger = logging.getLogger("test_flexi_actuator_command_list")

    with caplog.at_level(logging.INFO):
        actuator._log_rabbitmq_messages(commands=commands, slot_info=slot_info, logger=logger)

    batch_header_logs = [
        record.message
        for record in caplog.records
        if "batch header" in record.message.lower()
    ]
    assert len(batch_header_logs) == 0

    slot_logs = [
        record.message
        for record in caplog.records
        if "Slot info (log-only" in record.message
    ]
    assert len(slot_logs) == 1


def test_publish_batch_commands_sends_single_json_list():
    """All commands for one destination are published as one JSON list message."""
    commands = [_sample_command("ECM68.3"), _sample_command("ECM68.4")]
    publisher = actuator.RabbitMQPublisher(logger=logging.getLogger("test_flexi_actuator_command_list"))
    publisher._connected = True
    publisher.connection = MagicMock(is_open=True)
    publisher.channel = MagicMock()

    with patch.object(publisher, "is_connected", return_value=True):
        published = publisher.publish_batch_commands(commands, slot_info=None, verbose=False)

    assert published == 2
    publisher.channel.basic_publish.assert_called_once()
    body = publisher.channel.basic_publish.call_args.kwargs["body"]
    payload = json.loads(body)
    assert isinstance(payload, list)
    assert len(payload) == 2
    assert {item["asset_id"] for item in payload} == {"ECM68.3", "ECM68.4"}


def test_single_command_produces_one_publish_as_list():
    """One command is still published as a JSON list with one element."""
    commands = [_sample_command("ECM68.3")]
    publisher = actuator.RabbitMQPublisher(logger=logging.getLogger("test_flexi_actuator_command_list"))
    publisher._connected = True
    publisher.connection = MagicMock(is_open=True)
    publisher.channel = MagicMock()

    with patch.object(publisher, "is_connected", return_value=True):
        published = publisher.publish_batch_commands(commands, slot_info=None, verbose=False)

    assert published == 1
    publisher.channel.basic_publish.assert_called_once()
    body = json.loads(publisher.channel.basic_publish.call_args.kwargs["body"])
    assert isinstance(body, list)
    assert len(body) == 1
    assert body[0]["message_type"] == "command"
    assert body[0]["asset_id"] == "ECM68.3"


def test_publish_commands_does_not_send_batch_header():
    """_publish_commands (the actuator entry point) never sends a batch_start."""
    commands = [_sample_command("ECM68.3")]
    slot_info = {"fsp_id": "test", "slot_start": "2026-01-01T00:00:00"}
    publisher = actuator.RabbitMQPublisher(logger=logging.getLogger("test_flexi_actuator_command_list"))
    publisher._connected = True
    publisher.connection = MagicMock(is_open=True)
    publisher.channel = MagicMock()

    with patch.object(publisher, "is_connected", return_value=True):
        published = actuator._publish_commands(
            publisher=publisher,
            commands=commands,
            slot_info=slot_info,
            dry_run=False,
            logger=logging.getLogger("test_flexi_actuator_command_list"),
            verbose=False,
        )

    assert published == 1
    publisher.channel.basic_publish.assert_called_once()

    body = json.loads(publisher.channel.basic_publish.call_args.kwargs["body"])
    assert isinstance(body, list)
    assert all(item["message_type"] == "command" for item in body)


def test_command_envelopes_in_list_have_all_required_fields():
    """Each command envelope inside the published list has all required fields."""
    commands = [_sample_command("ECM68.3")]
    publisher = actuator.RabbitMQPublisher(logger=logging.getLogger("test_flexi_actuator_command_list"))
    publisher._connected = True
    publisher.connection = MagicMock(is_open=True)
    publisher.channel = MagicMock()

    with patch.object(publisher, "is_connected", return_value=True):
        publisher.publish_batch_commands(commands, slot_info=None, verbose=False)

    body = json.loads(publisher.channel.basic_publish.call_args.kwargs["body"])
    for envelope in body:
        assert envelope.get("message_type") == "command"
        assert "asset_id" in envelope
        assert "asset_type" in envelope
        assert "command_type" in envelope
        assert "payload" in envelope
        assert "timestamp" in envelope
        assert "priority" in envelope
