import copy
import logging
import sys
import types
from importlib.machinery import ModuleSpec
from importlib.util import find_spec
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _stub_module_if_missing(name):
    try:
        missing = find_spec(name) is None
    except ValueError:
        missing = True
    if missing:
        module = types.ModuleType(name)
        module.__spec__ = ModuleSpec(name, loader=None)
        if name == "pandas":
            for attr in ("DataFrame", "Series", "Timestamp", "Timedelta", "DatetimeIndex"):
                setattr(module, attr, type(attr, (), {}))
        sys.modules[name] = module


_stub_module_if_missing("pandas")
_stub_module_if_missing("pytz")
try:
    psycopg2_missing = find_spec("psycopg2") is None
except ValueError:
    psycopg2_missing = True
if psycopg2_missing:
    psycopg2_stub = types.ModuleType("psycopg2")
    psycopg2_extras_stub = types.ModuleType("psycopg2.extras")
    psycopg2_stub.__spec__ = ModuleSpec("psycopg2", loader=None)
    psycopg2_extras_stub.__spec__ = ModuleSpec("psycopg2.extras", loader=None)
    psycopg2_stub.extras = psycopg2_extras_stub
    sys.modules["psycopg2"] = psycopg2_stub
    sys.modules["psycopg2.extras"] = psycopg2_extras_stub

from scripts import flexi_manager as fm  # noqa: E402


class FakeRabbitMQPublisher:
    def __init__(self):
        self.calls = []

    def publish_batch_commands(self, commands, slot_info=None):
        self.calls.append((copy.deepcopy(commands), copy.deepcopy(slot_info)))
        return len(commands)


def _logger():
    return logging.getLogger("test_flexi_manager_rabbitmq_routing")


def _rabbitmq_config():
    return {
        "realAssetCommands": {
            "exchange": "flexi_commands",
            "queue": "flexi_commands_queue",
            "routingKey": "real_asset.command",
        },
        "simulatedAssetCommands": {
            "exchange": "flexi_sim_commands",
            "queue": "flexi_sim_commands_queue",
            "routingKey": "sim_asset.command",
        },
        "simulatedAssetMeasures": {
            "exchange": "flexi_sim_measures",
            "queue": "flexi_sim_measures_queue",
            "routingKey": "sim_asset.measure",
        },
    }


def _pending_command(asset_id, asset_type="heat_pump"):
    return {
        "asset_id": asset_id,
        "asset_type": asset_type,
        "command_type": "curtail",
        "payload": {"asset_id": asset_id, "asset_type": asset_type},
        "priority": 7,
    }


def _publish_pending(asset_mapping, pending_commands, rabbitmq_config=None):
    publisher = FakeRabbitMQPublisher()
    controller = fm.AssetController(
        asset_mapping=asset_mapping,
        logger=_logger(),
        rabbitmq_publisher=publisher,
        rabbitmq_config=rabbitmq_config or _rabbitmq_config(),
    )
    controller._pending_commands = list(pending_commands)
    published = controller.publish_pending_commands(
        slot_info={
            "slot_start": "2099-01-01T00:00:00",
            "slot_end": "2099-01-01T00:15:00",
        },
        dry_run=True,
    )
    return published, publisher, controller


def test_real_asset_command_uses_real_asset_commands_destination():
    published, publisher, _controller = _publish_pending(
        {
            "ECM96.2": {
                "type": "heat_pump",
                "rabbitCommandSection": "realAssetCommands",
            }
        },
        [_pending_command("ECM96.2")],
    )

    assert published == 1
    command = publisher.calls[0][0][0]
    assert command["rabbitmq_destination"] == {
        "section": "realAssetCommands",
        "exchange": "flexi_commands",
        "queue": "flexi_commands_queue",
        "routing_key": "real_asset.command",
    }


def test_simulated_asset_command_uses_simulated_asset_commands_destination():
    published, publisher, _controller = _publish_pending(
        {
            "ECM68.3": {
                "type": "heat_pump",
                "rabbitCommandSection": "simulatedAssetCommands",
            }
        },
        [_pending_command("ECM68.3")],
    )

    assert published == 1
    command = publisher.calls[0][0][0]
    assert command["rabbitmq_destination"] == {
        "section": "simulatedAssetCommands",
        "exchange": "flexi_sim_commands",
        "queue": "flexi_sim_commands_queue",
        "routing_key": "sim_asset.command",
    }


def test_asset_missing_rabbit_command_section_is_skipped_with_warning(caplog):
    with caplog.at_level(logging.WARNING):
        published, publisher, controller = _publish_pending(
            {"ECM97.3": {"type": "heat_pump"}},
            [_pending_command("ECM97.3")],
        )

    assert published == 0
    assert publisher.calls == []
    assert controller.get_pending_commands() == []
    assert (
        "Skipping RabbitMQ command for asset ECM97.3: "
        "missing asset_mapping.rabbitCommandSection"
    ) in caplog.text


def test_invalid_rabbit_command_section_is_skipped_with_warning(caplog):
    with caplog.at_level(logging.WARNING):
        published, publisher, _controller = _publish_pending(
            {
                "ECM68.3": {
                    "type": "heat_pump",
                    "rabbitCommandSection": "simulatedAssetMeasures",
                }
            },
            [_pending_command("ECM68.3")],
        )

    assert published == 0
    assert publisher.calls == []
    assert "invalid asset_mapping.rabbitCommandSection 'simulatedAssetMeasures'" in caplog.text


def test_missing_destination_config_section_is_skipped_with_warning(caplog):
    rabbitmq_config = {
        "simulatedAssetCommands": _rabbitmq_config()["simulatedAssetCommands"],
    }

    with caplog.at_level(logging.WARNING):
        published, publisher, _controller = _publish_pending(
            {
                "ECM96.2": {
                    "type": "heat_pump",
                    "rabbitCommandSection": "realAssetCommands",
                }
            },
            [_pending_command("ECM96.2")],
            rabbitmq_config=rabbitmq_config,
        )

    assert published == 0
    assert publisher.calls == []
    assert "rabbitMQ.realAssetCommands section not found" in caplog.text


def test_destination_config_missing_required_field_is_skipped_with_warning(caplog):
    rabbitmq_config = {
        "realAssetCommands": {
            "exchange": "flexi_commands",
            "routingKey": "real_asset.command",
        }
    }

    with caplog.at_level(logging.WARNING):
        published, publisher, _controller = _publish_pending(
            {
                "ECM96.2": {
                    "type": "heat_pump",
                    "rabbitCommandSection": "realAssetCommands",
                }
            },
            [_pending_command("ECM96.2")],
            rabbitmq_config=rabbitmq_config,
        )

    assert published == 0
    assert publisher.calls == []
    assert "rabbitMQ.realAssetCommands missing required field(s): queue" in caplog.text
