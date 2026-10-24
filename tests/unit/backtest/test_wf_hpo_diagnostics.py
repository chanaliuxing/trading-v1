
import pandas as pd
import numpy as np
import pytest
from wf_hpo import WalkForwardHPO, Fold

@pytest.fixture
def minimal_hpo_config():
    """Provides a minimal, valid config for instantiating WalkForwardHPO."""
    return {
        "symbols": ["A"],
        "strategy_path": "strategies.enhanced_model_signal_strategy.EnhancedModelSignalStrategy",
        "grid": {},
        "folds": [{"fold_id": 1, "is_start": "2025-01-01 00:00:00", "is_end": "2025-01-01 23:59:59", "oos_start": "2025-01-02 00:00:00", "oos_end": "2025-01-02 23:59:59"}],
        "objective": {"metric": "sharpe"},
        "strategy_base_extras": {},
        "parallel": {"workers": 1},
        "model_target_col": "y",
        "model_task": "reg",
        "dataset_df": None, # To be filled in by the test
    }

def test_label_stats_matches_manual_computation(minimal_hpo_config):
    """
    Tests that the generated label_stats in diagnostics match manual computation.
    This test now provides an in-memory dataframe to avoid ClickHouse connection.
    """
    # 1. Create a sample DataFrame covering both IS and OOS periods
    y_series = pd.Series([0.01, -0.02, 0.03, 0.0, -0.015, 0.005, np.nan])
    full_df = pd.DataFrame({
        'ts': pd.to_datetime([
            '2025-01-01 09:30:00', '2025-01-01 09:31:00', '2025-01-01 09:32:00',
            '2025-01-01 09:33:00', '2025-01-01 09:34:00', '2025-01-01 09:35:00',
            '2025-01-01 09:36:00',
            # Add one row for the OOS period to prevent slicing errors
            '2025-01-02 09:30:00'
        ]),
        'symbol': 'A',
        'close': np.random.rand(len(y_series) + 1) * 100 + 1000,
        'y': pd.concat([y_series, pd.Series([0.0])], ignore_index=True)
    })
    minimal_hpo_config["dataset_df"] = full_df

    # 2. Instantiate WalkForwardHPO and call _prepare_model_data
    wfhpo = WalkForwardHPO(minimal_hpo_config)
    fold = Fold(fold_id=1, is_start="2025-01-01 00:00:00", is_end="2025-01-01 23:59:59", oos_start="2025-01-02 00:00:00", oos_end="2025-01-02 23:59:59")
    params = {"model_type": "xgb", "feature_subset": ["close"]} # Minimal params
    
    # The df_is argument is no longer needed as it will be sliced from self.dataset_df
    _, _, meta = wfhpo._prepare_model_data(fold, params)

    # 3. Extract diagnostics
    assert meta is not None
    assert "diagnostics" in meta
    assert "label_stats" in meta["diagnostics"]
    label_stats = meta["diagnostics"]["label_stats"]

    # 4. Manually compute stats on the valid (non-NaN) part of the series
    y_valid = y_series.dropna()
    
    assert label_stats["n"] == len(y_valid)
    assert label_stats["mean"] == pytest.approx(y_valid.mean())
    assert label_stats["std"] == pytest.approx(y_valid.std())
    assert label_stats["min"] == pytest.approx(y_valid.min())
    assert label_stats["p25"] == pytest.approx(y_valid.quantile(0.25))
    assert label_stats["p50"] == pytest.approx(y_valid.quantile(0.50))
    assert label_stats["p75"] == pytest.approx(y_valid.quantile(0.75))
    assert label_stats["max"] == pytest.approx(y_valid.max())
    assert label_stats["pos_frac"] == pytest.approx((y_valid > 0).mean())
    assert label_stats["neg_frac"] == pytest.approx((y_valid < 0).mean())
    assert label_stats["zero_frac"] == pytest.approx((y_valid == 0).mean())


def test_feature_ic_spearman_nontrivial_when_features_correlated(minimal_hpo_config):
    """
    Tests that feature_ic_spearman is high for correlated features and low for noise.
    """
    # 1. Create a DataFrame with a correlated feature and a noise feature
    np.random.seed(42)
    size = 100
    y_series = pd.Series(np.random.randn(size))
    feature_corr = y_series * 2 + np.random.normal(0, 0.1, size)
    feature_noise = np.random.randn(size)

    full_df = pd.DataFrame({
        'ts': pd.to_datetime(pd.date_range('2025-01-01', periods=size, freq='1min')),
        'symbol': 'A',
        'y': y_series,
        'feature_corr': feature_corr,
        'feature_noise': feature_noise,
    })
    # Add a dummy row for OOS to prevent slicing errors
    oos_row = pd.DataFrame({'ts': [pd.to_datetime('2025-01-02 09:30:00')], 'symbol': ['A'], 'y': [0], 'feature_corr': [0], 'feature_noise': [0]})
    full_df = pd.concat([full_df, oos_row], ignore_index=True)
    minimal_hpo_config["dataset_df"] = full_df

    # 2. Instantiate HPO and run data preparation
    wfhpo = WalkForwardHPO(minimal_hpo_config)
    fold = Fold(fold_id=1, is_start="2025-01-01 00:00:00", is_end="2025-01-01 23:59:59", oos_start="2025-01-02 00:00:00", oos_end="2025-01-02 23:59:59")
    params = {"model_type": "xgb", "feature_subset": ["feature_corr", "feature_noise"]}
    
    _, _, meta = wfhpo._prepare_model_data(fold, params)

    # 3. Extract IC diagnostics
    assert "diagnostics" in meta
    assert "feature_ic_spearman" in meta["diagnostics"]
    ic_stats = meta["diagnostics"]["feature_ic_spearman"]

    # 4. Assert that the correlated feature has high IC and noise has low IC
    assert "feature_corr" in ic_stats
    assert "feature_noise" in ic_stats
    assert ic_stats["feature_corr"] > 0.8
    assert abs(ic_stats["feature_noise"]) < 0.3


def test_diagnostics_present_in_meta_for_regression_task(minimal_hpo_config):
    """
    Ensures all standard diagnostic keys are present in the metadata for a
    typical regression task.
    """
    # 1. Create a simple valid DataFrame
    y_series = pd.Series([0.01, -0.02, 0.03, 0.0])
    full_df = pd.DataFrame({
        'ts': pd.to_datetime(pd.date_range('2025-01-01', periods=len(y_series), freq='1min')),
        'symbol': 'A',
        'close': np.random.rand(len(y_series)) * 100 + 1000,
        'y': y_series,
    })
    oos_row = pd.DataFrame({'ts': [pd.to_datetime('2025-01-02 09:30:00')], 'symbol': ['A'], 'close': [1000], 'y': [0]})
    full_df = pd.concat([full_df, oos_row], ignore_index=True)
    minimal_hpo_config["dataset_df"] = full_df

    # 2. Instantiate HPO and run data preparation
    wfhpo = WalkForwardHPO(minimal_hpo_config)
    fold = Fold(fold_id=1, is_start="2025-01-01 00:00:00", is_end="2025-01-01 23:59:59", oos_start="2025-01-02 00:00:00", oos_end="2025-01-02 23:59:59")
    params = {"model_type": "xgb", "feature_subset": ["close"]}
    
    _, _, meta = wfhpo._prepare_model_data(fold, params)

    # 3. Assert all expected diagnostic keys are present
    assert "diagnostics" in meta
    diagnostics = meta["diagnostics"]
    
    expected_keys = [
        "label_stats",
        "feature_ic_spearman",
        "pred_stats",
        "signal_stats",
        "conf_stats"
    ]
    
    for key in expected_keys:
        assert key in diagnostics, f"Expected diagnostic key '{key}' not found in metadata"
