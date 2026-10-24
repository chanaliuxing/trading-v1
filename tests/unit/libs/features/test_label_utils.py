
import pandas as pd
import numpy as np
import pytest
from libs.features.label_utils import _forward_match_strict, compute_label

def test_forward_match_strict_basic_alignment():
    """
    Tests if _forward_match_strict correctly aligns future prices (px_to)
    for a simple, regular time series with multiple symbols.
    """
    # 1. Construct a simple DataFrame
    data = {
        'ts': pd.to_datetime([
            '2025-01-01 09:30:00', '2025-01-01 09:31:00', '2025-01-01 09:32:00', '2025-01-01 09:33:00',
            '2025-01-01 09:30:00', '2025-01-01 09:31:00', '2025-01-01 09:32:00', '2025-01-01 09:33:00',
        ]),
        'symbol': ['A', 'A', 'A', 'A', 'B', 'B', 'B', 'B'],
        'close': [100, 101, 102, 103, 200, 201, 202, 203]
    }
    df = pd.DataFrame(data)
    horizon_sec = 120  # 2 minutes

    # 2. Call the function
    result_px_to = _forward_match_strict(
        df,
        horizon_sec=horizon_sec,
        ts_col="ts",
        symbol_col="symbol",
        to_col="close",
        max_slip_sec=0
    )

    # 3. Manually calculate the expected result
    # For symbol 'A':
    # 09:30 -> 09:32 (102)
    # 09:31 -> 09:33 (103)
    # 09:32 -> 09:34 (NaN)
    # 09:33 -> 09:35 (NaN)
    # For symbol 'B':
    # 09:30 -> 09:32 (202)
    # 09:31 -> 09:33 (203)
    # 09:32 -> 09:34 (NaN)
    # 09:33 -> 09:35 (NaN)
    expected_data = [102.0, 103.0, np.nan, np.nan, 202.0, 203.0, np.nan, np.nan]
    expected_px_to = pd.Series(expected_data, index=df.index, dtype=np.float64)

    # 4. Assert correctness
    pd.testing.assert_series_equal(result_px_to, expected_px_to, check_names=False)


def test_compute_label_regression_profile_matches_profile_config():
    """
    Tests if compute_label correctly calculates regression labels (log-return with clipping)
    based on a given policy configuration.
    """
    # 1. Construct DataFrame
    data = {
        'ts': pd.to_datetime([
            '2025-01-01 09:30:00', '2025-01-01 09:31:00', '2025-01-01 09:32:00',
        ]),
        'symbol': ['A', 'A', 'A'],
        'close': [100, 101, 105]  # Large jump at the end to test clipping
    }
    df = pd.DataFrame(data)

    # 2. Define policy
    policy = {
        "task": "regression",
        "label": {
            "horizon_sec": 60,
            "method": "logret",
            "from_col": "close",
            "to_col": "close",
            "clip_abs": 0.02  # Clip absolute log-return at 2%
        }
    }

    # 3. Call the function
    result_y = compute_label(df, policy)

    # 4. Manually calculate the expected result
    # 09:30 -> 09:31: log(101/100) = 0.00995
    # 09:31 -> 09:32: log(105/101) = 0.0388 (will be clipped to 0.02)
    # 09:32 -> 09:33: NaN
    expected_data = [np.log(101/100), 0.02, np.nan]
    expected_y = pd.Series(expected_data, index=df.index, dtype=np.float64)

    # 5. Assert correctness
    pd.testing.assert_series_equal(result_y, expected_y, check_names=False)


def test_compute_label_binary_bins_balance():
    """
    Tests if compute_label for a binary task produces a balanced number of
    positive labels, preventing extreme class imbalance from bad binning thresholds.
    """
    # 1. Create a synthetic DataFrame with a known return distribution
    np.random.seed(42)
    size = 1000
    # Create returns centered around 0, with a std dev of 0.001
    log_returns = np.random.normal(loc=0, scale=0.001, size=size)
    
    # Create a price series from the log returns
    initial_price = 100
    prices = initial_price * np.exp(np.cumsum(log_returns))
    
    df = pd.DataFrame({
        'ts': pd.to_datetime(pd.date_range('2025-01-01', periods=size, freq='1s')),
        'symbol': 'A',
        'close': prices
    })

    # 2. Define policy with bins that should capture ~25% positive class
    # Based on normal distribution, ~68% is within 1 std dev.
    # We set the positive threshold near the 75th percentile.
    # For N(0, 0.001), p75 is approx 0.00067
    policy = {
        "task": "binary",
        "label": {
            "horizon_sec": 1,
            "method": "logret",
            "from_col": "close",
            "to_col": "close",
            "cls_bins": [-np.inf, -0.0003, 0.0003, np.inf]
        }
    }

    # 3. Call the function
    result_y = compute_label(df, policy)

    # 4. Check the balance
    # We expect roughly 25-40% of non-NaN labels to be positive (1)
    # For N(0,1), P(X > 0.3/1.0) = P(Z > 0.3) ~= 38%
    # A range of 20-45% seems reasonable to account for randomness.
    valid_labels = result_y.dropna()
    pos_frac = (valid_labels == 1).mean()

    assert 0.20 < pos_frac < 0.45, f"Positive label fraction {pos_frac} is outside the expected range (0.20, 0.45)"


def test_compute_label_handles_missing_prices_and_slip():
    """
    Tests that `compute_label` correctly uses `max_slip_sec` to find the next
    available price when the target timestamp is missing, preventing NaNs.
    """
    # 1. Construct a DataFrame with a gap in the time series
    data = {
        'ts': pd.to_datetime([
            '2025-01-01 09:30:00',  # Target for this is 09:31:00, which is missing
            '2025-01-01 09:31:30',  # The next available price
            '2025-01-01 09:32:00',
        ]),
        'symbol': ['A', 'A', 'A'],
        'close': [100, 102, 103]
    }
    df = pd.DataFrame(data).set_index(pd.Index([0, 1, 2]))

    # 2. Define policy with a `max_slip_sec` that can bridge the gap
    policy = {
        "task": "regression",
        "label": {
            "horizon_sec": 60,  # Look forward 60 seconds
            "method": "logret",
            "max_slip_sec": 30  # Allow slipping up to 30 seconds
        }
    }

    # 3. Call the function
    result_y = compute_label(df, policy)

    # 4. Manually calculate the expected result
    # For the first row at 09:30:00, the target is 09:31:00.
    # This is missing, but 09:31:30 is within the 30s slip.
    # So, the label should be log(102/100).
    # The other rows should result in NaN as there's no future data.
    expected_data = [np.log(102/100), np.nan, np.nan]
    expected_y = pd.Series(expected_data, index=df.index, dtype=np.float64)

    # 5. Assert correctness
    pd.testing.assert_series_equal(result_y, expected_y, check_names=False)
