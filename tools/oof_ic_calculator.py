"""
OOF IC Calculator - Compute Information Coefficient for Walk-Forward Evaluation

This module provides utilities to calculate Spearman rank IC (Information Coefficient)
for out-of-sample (OOS) predictions and integrate with oos_summary.

Usage:
    from tools.oof_ic_calculator import compute_fold_ic, aggregate_oof_summaries_with_ic
"""

from typing import Dict, Any, List, Optional
import numpy as np
import pandas as pd
from scipy.stats import spearmanr


def compute_fold_ic(
    df: pd.DataFrame,
    pred_col: str = "y_pred",
    label_col: str = "y_true",
    min_samples: int = 5
) -> float:
    """
    Compute Spearman rank IC for a single OOS fold/slice.

    Parameters:
    -----------
    df : pd.DataFrame
        DataFrame containing predictions and actual returns
    pred_col : str
        Column name for predictions (default: "y_pred")
    label_col : str
        Column name for actual future returns (default: "y_true")
    min_samples : int
        Minimum number of valid samples required (default: 5)

    Returns:
    --------
    float
        Spearman IC (correlation coefficient), or 0.0 if insufficient data

    Example:
    --------
    >>> df = pd.DataFrame({
    ...     'y_pred': [0.02, 0.01, -0.01, 0.03],
    ...     'y_true': [0.015, 0.012, -0.008, 0.025]
    ... })
    >>> ic = compute_fold_ic(df)
    >>> print(f"IC: {ic:.4f}")
    """
    if df is None or df.empty:
        return 0.0

    # Extract predictions and actual returns
    s_pred = df[pred_col].astype(float)
    s_ret = df[label_col].astype(float)

    # Filter out NaN/inf values
    mask = np.isfinite(s_pred) & np.isfinite(s_ret)
    valid_count = mask.sum()

    if valid_count < min_samples:
        return 0.0

    try:
        ic, _ = spearmanr(s_pred[mask], s_ret[mask])
        if not np.isfinite(ic):
            return 0.0
        return float(ic)
    except Exception:
        return 0.0


def aggregate_oof_summaries_with_ic(
    oof_folds: List[Dict[str, Any]],
    pred_col: str = "y_pred",
    label_col: str = "y_true"
) -> Dict[str, float]:
    """
    Aggregate OOF fold results and compute average IC across all folds.

    This function should be called at the end of walk-forward evaluation to
    produce the final oos_summary with IC included.

    Parameters:
    -----------
    oof_folds : List[Dict[str, Any]]
        List of fold results. Each fold dict should contain:
        - 'sharpe': Sharpe ratio for this fold
        - 'profit_factor': Profit factor
        - 'max_drawdown': Maximum drawdown
        - 'win_rate': Win rate
        - 'total_return': Total return
        - 'trades': Number of trades
        - 'df_oos': DataFrame with predictions and actuals (for IC calculation)

    pred_col : str
        Column name for predictions in df_oos (default: "y_pred")
    label_col : str
        Column name for actual returns in df_oos (default: "y_true")

    Returns:
    --------
    Dict[str, float]
        Aggregated OOS summary with all standard metrics plus oos_avg_ic

    Example:
    --------
    >>> fold1 = {
    ...     'sharpe': 1.5,
    ...     'profit_factor': 1.8,
    ...     'max_drawdown': -0.12,
    ...     'win_rate': 0.62,
    ...     'total_return': 0.25,
    ...     'trades': 100,
    ...     'df_oos': df1  # DataFrame with predictions
    ... }
    >>> fold2 = {...}  # Similar structure
    >>> oos_summary = aggregate_oof_summaries_with_ic([fold1, fold2])
    >>> print(oos_summary)
    {
        'oos_avg_sharpe': 1.4,
        'oos_avg_profit_factor': 1.7,
        'oos_avg_ic': 0.032,  # ⭐ This is the new field
        ...
    }
    """
    if not oof_folds:
        return {
            "oos_avg_sharpe": 0.0,
            "oos_avg_profit_factor": 0.0,
            "oos_avg_max_drawdown": 0.0,
            "oos_avg_win_rate": 0.0,
            "oos_avg_total_return": 0.0,
            "oos_trades": 0,
            "oos_avg_ic": 0.0,
        }

    # Collect metrics from each fold
    sharpe_vals = []
    pf_vals = []
    mdd_vals = []
    win_vals = []
    ret_vals = []
    trades_vals = []
    ic_vals = []

    for fold in oof_folds:
        sharpe_vals.append(float(fold.get("sharpe", 0.0)))
        pf_vals.append(float(fold.get("profit_factor", 0.0)))
        mdd_vals.append(float(fold.get("max_drawdown", 0.0)))
        win_vals.append(float(fold.get("win_rate", 0.0)))
        ret_vals.append(float(fold.get("total_return", 0.0)))
        trades_vals.append(float(fold.get("trades", 0)))

        # Compute IC for this fold if df_oos is available
        df_oos = fold.get("df_oos")
        if df_oos is not None and not df_oos.empty:
            ic_val = compute_fold_ic(df_oos, pred_col, label_col)
            ic_vals.append(ic_val)

    def _safe_mean(xs: List[float]) -> float:
        """Compute mean, filtering out non-finite values"""
        xs_clean = [x for x in xs if np.isfinite(x)]
        return float(sum(xs_clean) / len(xs_clean)) if xs_clean else 0.0

    # Aggregate all metrics
    oos_summary = {
        "oos_avg_sharpe": _safe_mean(sharpe_vals),
        "oos_avg_profit_factor": _safe_mean(pf_vals),
        "oos_avg_max_drawdown": _safe_mean(mdd_vals),
        "oos_avg_win_rate": _safe_mean(win_vals),
        "oos_avg_total_return": _safe_mean(ret_vals),
        "oos_trades": int(sum(trades_vals)),
        # ⭐ The key addition: average IC across all OOS folds
        "oos_avg_ic": _safe_mean(ic_vals),
    }

    return oos_summary


# ============================================================================
# Integration example for existing walk-forward code
# ============================================================================

def example_integration():
    """
    Example showing how to integrate IC calculation into existing walk-forward code.

    BEFORE (in your walk_forward.py or similar):
    ```python
    def run_walk_forward(...):
        ...
        oos_folds = []
        for fold in folds:
            # Run backtest for this fold
            result = run_backtest(fold)
            oos_folds.append({
                'sharpe': result.sharpe,
                'profit_factor': result.pf,
                ...
            })

        # Aggregate (old way - no IC)
        oos_summary = {
            'oos_avg_sharpe': mean([f['sharpe'] for f in oos_folds]),
            ...
        }
        return {'oos_summary': oos_summary, ...}
    ```

    AFTER (with IC):
    ```python
    from tools.oof_ic_calculator import aggregate_oof_summaries_with_ic

    def run_walk_forward(...):
        ...
        oos_folds = []
        for fold in folds:
            # Run backtest for this fold
            result = run_backtest(fold)

            # ⭐ KEY: Save the OOS predictions DataFrame
            df_oos = result.get_oos_predictions_df()  # Must have 'y_pred' and 'y_true' columns

            oos_folds.append({
                'sharpe': result.sharpe,
                'profit_factor': result.pf,
                'max_drawdown': result.max_dd,
                'win_rate': result.win_rate,
                'total_return': result.total_return,
                'trades': result.num_trades,
                'df_oos': df_oos,  # ⭐ Add this
            })

        # ⭐ Use the new aggregation function
        oos_summary = aggregate_oof_summaries_with_ic(oos_folds)

        return {'oos_summary': oos_summary, ...}
    ```
    """
    pass


if __name__ == "__main__":
    # Simple test
    import pandas as pd

    print("Testing OOF IC Calculator...")

    # Create sample data
    np.random.seed(42)
    n = 100

    # Simulated predictions with some correlation to actual returns
    actual_returns = np.random.randn(n) * 0.02
    predictions = actual_returns + np.random.randn(n) * 0.01  # Add noise

    df_test = pd.DataFrame({
        'y_pred': predictions,
        'y_true': actual_returns
    })

    # Test single fold IC
    ic = compute_fold_ic(df_test)
    print(f"Single fold IC: {ic:.4f}")

    # Test aggregation
    fold1 = {
        'sharpe': 1.5,
        'profit_factor': 1.8,
        'max_drawdown': -0.12,
        'win_rate': 0.62,
        'total_return': 0.25,
        'trades': 100,
        'df_oos': df_test
    }

    fold2 = {
        'sharpe': 1.3,
        'profit_factor': 1.6,
        'max_drawdown': -0.15,
        'win_rate': 0.58,
        'total_return': 0.20,
        'trades': 95,
        'df_oos': df_test
    }

    oos_summary = aggregate_oof_summaries_with_ic([fold1, fold2])

    print("\nAggregated OOS Summary:")
    for key, value in oos_summary.items():
        if 'ic' in key:
            print(f"  {key}: {value:.4f} ⭐")
        elif isinstance(value, float):
            print(f"  {key}: {value:.4f}")
        else:
            print(f"  {key}: {value}")

    print("\n✅ OOF IC Calculator ready to use!")
    print("\nNext steps:")
    print("1. Import this module in your walk-forward code")
    print("2. Ensure each OOS fold includes 'df_oos' with predictions")
    print("3. Use aggregate_oof_summaries_with_ic() to get oos_summary with IC")
