import pandas as pd

from scripts.strategy_evaluator import _activation_case_2assets, _strategy7_daily_power_proxy_from_bid_records


def test_activation_case_bucketing():
    df = pd.DataFrame(
        {
            "kw_ECM96.2": [0.0, 1.0, 0.0, 1.0],
            "kw_ECM97.3": [0.0, 0.0, 2.0, 3.0],
        }
    )
    got = _activation_case_2assets(df, a_col="kw_ECM96.2", b_col="kw_ECM97.3").tolist()
    assert got == ["none", "only ECM96.2", "only ECM97.3", "ECM96.2 and ECM97.3"]


def test_strategy7_daily_proxy_empty_dir(tmp_path):
    out = _strategy7_daily_power_proxy_from_bid_records(str(tmp_path))
    assert list(out.columns) == ["date", "strategy_7_kw"]
    assert out.empty
