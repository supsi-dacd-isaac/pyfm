import logging
import os
import sys
import types
from importlib.machinery import ModuleSpec
from importlib.util import find_spec

import pandas as pd
import pytest


sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    influxdb_missing = find_spec("influxdb") is None
except ValueError:
    influxdb_missing = True
if influxdb_missing:
    influxdb_stub = types.ModuleType("influxdb")
    influxdb_stub.__spec__ = ModuleSpec("influxdb", loader=None)

    class DataFrameClient:
        pass

    influxdb_stub.DataFrameClient = DataFrameClient
    sys.modules["influxdb"] = influxdb_stub

from classes.fsp import FSP  # noqa: E402
from classes.portfolio import Portfolio  # noqa: E402


class FakeNodesInterface:
    def __init__(self):
        self.cfg = {"mainEndpoint": "https://nodes.example/api/"}
        self.uploaded_dataframe = None
        self.upload_count = 0
        self.upload_endpoint = None

    def post_csv_file_request(self, endpoint, tmp_baseline_file):
        self.upload_endpoint = endpoint
        self.uploaded_dataframe = pd.read_csv(tmp_baseline_file)
        self.upload_count += 1
        return True


def _baseline_dataframe(quantities):
    start = pd.Timestamp("2026-07-10T10:00:00Z")
    return pd.DataFrame(
        {
            "assetPortfolioId": ["portfolio-1" for _ in quantities],
            "periodFrom": [
                (start + pd.Timedelta(minutes=15 * idx)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
                for idx, _ in enumerate(quantities)
            ],
            "periodTo": [
                (start + pd.Timedelta(minutes=15 * (idx + 1))).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
                for idx, _ in enumerate(quantities)
            ],
            "quantity": quantities,
            "quantityType": ["Power" for _ in quantities],
        }
    )


def _make_fsp(tmp_path):
    fsp = object.__new__(FSP)
    fsp.cfg = {
        "id": "SUPSI",
        "name": "SUPSI",
        "baselines": {"tmpFolder": str(tmp_path)},
    }
    fsp.main_cfg = {
        "fm": {"granularity": 15},
        "influxDB": {"saveBaselineMeasurement": False},
    }
    fsp.logger = logging.getLogger("test_baseline_value_multiplier")
    fsp.nodes_interface = FakeNodesInterface()
    fsp.portfolios = {"portfolio-1": Portfolio("portfolio-1", {})}
    return fsp


def _install_baseline_builder(fsp, source, generated_dataframe):
    def create_df_baseline_from_db(self, portfolio, adjusted_time, bs_cfg):
        return generated_dataframe

    def create_df_baseline_from_file(self, portfolio, adjusted_time, file_settings):
        return generated_dataframe

    if source == "db":
        fsp.create_df_baseline_from_db = types.MethodType(
            create_df_baseline_from_db, fsp
        )
    else:
        fsp.create_df_baseline_from_file = types.MethodType(
            create_df_baseline_from_file, fsp
        )


def _install_save_capture(fsp, captured):
    def save_portfolio_baseline_to_influx(self, portfolio_id, baseline_dataframe, bs_cfg):
        captured["portfolio_id"] = portfolio_id
        captured["dataframe"] = baseline_dataframe.copy(deep=True)
        return True

    fsp.save_portfolio_baseline_to_influx = types.MethodType(
        save_portfolio_baseline_to_influx, fsp
    )


def _baseline_config(source, value_multiplier=None):
    cfg = {
        "source": source,
        "shiftMinutes": 0,
        "fileSettings": {"profileFile": "unused.csv"},
        "dbSettings": {"strategy": "slot_persistence"},
    }
    if value_multiplier is not None:
        cfg["valueMultiplier"] = value_multiplier
    return cfg


def test_missing_value_multiplier_defaults_to_one():
    assert FSP._parse_baseline_value_multiplier({}) == 1.0


def test_value_multiplier_one_preserves_uploaded_values(tmp_path):
    fsp = _make_fsp(tmp_path)
    original = _baseline_dataframe([11.0, 0.0, 2.5])

    assert fsp.update_portfolio_baseline(
        "portfolio-1", original, value_multiplier=1.0
    )

    assert fsp.nodes_interface.uploaded_dataframe["quantity"].tolist() == [
        11.0,
        0.0,
        2.5,
    ]
    assert original["quantity"].tolist() == [11.0, 0.0, 2.5]


def test_negative_value_multiplier_inverts_positive_values_and_keeps_zero(tmp_path):
    fsp = _make_fsp(tmp_path)
    original = _baseline_dataframe([11.0, 0.0, 2.5])

    assert fsp.update_portfolio_baseline(
        "portfolio-1", original, value_multiplier=-1.0
    )

    assert fsp.nodes_interface.uploaded_dataframe["quantity"].tolist() == [
        -11.0,
        0.0,
        -2.5,
    ]
    assert original["quantity"].tolist() == [11.0, 0.0, 2.5]


@pytest.mark.parametrize("source", ["db", "file"])
def test_update_baselines_uploads_transformed_payload_once_and_preserves_internal_values(
    tmp_path, source, caplog
):
    fsp = _make_fsp(tmp_path)
    generated = _baseline_dataframe([11.0, 0.0, 2.5])
    _install_baseline_builder(fsp, source, generated)
    saved = {}
    _install_save_capture(fsp, saved)

    caplog.set_level(logging.INFO, logger="test_baseline_value_multiplier")
    result = fsp.update_baselines(
        _baseline_config(source, value_multiplier=-1.0), dry_run=False
    )

    assert result is True
    assert fsp.nodes_interface.upload_count == 1
    assert fsp.nodes_interface.upload_endpoint.endswith("BaselineIntervals/import")
    assert fsp.nodes_interface.uploaded_dataframe["quantity"].tolist() == [
        -11.0,
        0.0,
        -2.5,
    ]
    assert generated["quantity"].tolist() == [11.0, 0.0, 2.5]
    assert saved["portfolio_id"] == "portfolio-1"
    assert saved["dataframe"]["quantity"].tolist() == [11.0, 0.0, 2.5]
    assert "Baseline value multiplier: -1.0" in caplog.text
    assert "calculated range=0.000000 to 11.000000 MW" in caplog.text
    assert "upload range=-11.000000 to 0.000000 MW" in caplog.text
