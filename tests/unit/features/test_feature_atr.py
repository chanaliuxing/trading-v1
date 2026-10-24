import pandas as pd

from libs.features.feature_ops import op_ta_atr


def test_op_ta_atr_basic():
    df = pd.DataFrame(
        {
            "high": [10, 10.5, 11, 11.2],
            "low": [9, 9.7, 10, 10.8],
            "close": [9.5, 10, 10.8, 11.0],
        }
    )
    out = op_ta_atr(df, window_bars=14, per_symbol=False)
    assert len(out) == 4
    assert round(out.iloc[0], 6) == 1.0
