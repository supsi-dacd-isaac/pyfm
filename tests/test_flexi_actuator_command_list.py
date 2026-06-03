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


def test_publish_batch_commands_sends_single_json_list():
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
