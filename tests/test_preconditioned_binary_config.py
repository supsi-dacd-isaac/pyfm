import json
import logging
import os
import sys


sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from classes.bidding_strategy import StrategyManager


CONFIG_PATH = os.path.join(
    os.path.dirname(__file__), "..", "conf", "test_fm01_aem.json"
)

TARGET_BINARY_HP_ASSETS = ["ECM62.10", "ECM68.3", "ECM162.1"]
LEGACY_ECM62_ASSETS = ["ECM62.1", "ECM62.2", "ECM62.3"]


def _load_config():
    with open(CONFIG_PATH, "r") as handle:
        return json.load(handle)


def test_ecm62_10_asset_mapping_contract():
    cfg = _load_config()
    asset = cfg["asset_mapping"]["ECM62.10"]

    assert asset["device_name_tag"] == "ECM62.10"
    assert asset["pod"] == "ECM62"
    assert asset["field"] == "active_power"
    assert asset["type"] == "heat_pump"
    assert asset["rabbitCommandSection"] == "simulatedAssetCommands"
    assert asset["capacity_kw"] == 36.0
    assert asset["nominal_power_w"] == 36000
    assert asset["modulation_type"] == "discrete"
    assert asset["discrete_states_kw"] == [0.0, 36.0]


def test_existing_binary_simulated_hp_contracts_are_unchanged():
    cfg = _load_config()

    assert cfg["asset_mapping"]["ECM68.3"]["type"] == "heat_pump"
    assert cfg["asset_mapping"]["ECM68.3"]["modulation_type"] == "discrete"
    assert cfg["asset_mapping"]["ECM68.3"]["discrete_states_kw"] == [0.0, 8.4]

    assert cfg["asset_mapping"]["ECM162.1"]["type"] == "heat_pump"
    assert cfg["asset_mapping"]["ECM162.1"]["modulation_type"] == "discrete"
    assert cfg["asset_mapping"]["ECM162.1"]["discrete_states_kw"] == [0.0, 6.0]


def test_simulated_fsp_portfolio_contains_only_target_binary_hps():
    cfg = _load_config()
    portfolio_assets = cfg["fm"]["actors"]["fsps"]["supsi02"]["assets"]

    assert portfolio_assets == TARGET_BINARY_HP_ASSETS
    for asset_id in LEGACY_ECM62_ASSETS:
        assert asset_id not in portfolio_assets


def test_strategy_12_targets_only_target_binary_hps():
    cfg = _load_config()
    strategy = cfg["bidding_strategies"]["strategy_12"]

    assert strategy["asset_types"] == ["heat_pump"]
    assert strategy["assets_filter"] == TARGET_BINARY_HP_ASSETS
    # Step 2 promoted the strategy to the live preconditioned_binary method.
    assert strategy["flexibility_method"] == "preconditioned_binary"
    assert "intended_flexibility_method" not in strategy
    assert strategy["nominal_portfolio_flexibility_mw"] == 0.0504

    for asset_id in LEGACY_ECM62_ASSETS:
        assert asset_id not in strategy["assets_filter"]


def test_strategy_12_preconditioned_binary_settings_block():
    cfg = _load_config()
    settings = cfg["bidding_strategies"]["strategy_12"][
        "preconditionedBinarySettings"
    ]

    assert settings["maxCurrentMeasurementAgeMinutes"] == 30
    assert settings["minSamples"] == 2
    assert settings["requireLatestOn"] is True
    assert settings["minOnRatio"] == 0.8
    assert settings["stateToleranceW"] == 100
    assert settings["missingMeasurementPolicy"] == "skip_asset"


def test_strategy_12_loads_without_using_legacy_ecm62_members():
    cfg = _load_config()
    manager = StrategyManager(cfg, logging.getLogger("test_strategy_12_config"))
    strategy = manager.get_strategy("strategy_12")

    assert strategy is not None
    assert strategy.allowed_assets == TARGET_BINARY_HP_ASSETS
    assert strategy.hp_assets == TARGET_BINARY_HP_ASSETS
    assert strategy.ev_assets == []
    for asset_id in LEGACY_ECM62_ASSETS:
        assert not strategy.is_asset_allowed(asset_id)


def test_strategy_12_evening_only_schedule():
    cfg = _load_config()
    strategy = cfg["bidding_strategies"]["strategy_12"]
    slots = {slot["name"]: slot for slot in strategy["time_slots"]}

    # Pre-conditioning slot remains non-bidding and does not trigger control.
    preconditioning = slots["Pre-conditioning (configuration only)"]
    assert preconditioning["start"] == "14:00"
    assert preconditioning["end"] == "17:00"
    assert preconditioning["flexibility_mw"] == 0.0
    assert preconditioning["is_preheat_period"] is True
    assert "does not trigger" in preconditioning["control_note"]

    # Evening slot now carries the live nominal portfolio target (50.4 kW).
    evening = slots["Evening Peak Flex (preconditioned_binary)"]
    assert evening["start"] == "17:00"
    assert evening["end"] == "20:00"
    assert evening["flexibility_mw"] == 0.0504
    assert evening["target_flexibility_mw"] == 0.0504

    # Off-window remains non-bidding.
    off_window = slots["Off-window (no bid)"]
    assert off_window["flexibility_mw"] == 0.0

