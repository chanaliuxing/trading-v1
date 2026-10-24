# metrics_extra.py
from __future__ import annotations
import numpy as np
from math import erf, sqrt, log


def probabilistic_sharpe_ratio(sharpe: float, n: int, ref: float = 0.0) -> float:
    """
    PSR (Bailey approximation): P(SR > ref)
    Z = (SR - ref) * sqrt(n - 1); PSR = Phi(Z)

    Args:
        sharpe: Observed Sharpe ratio
        n: Number of independent samples (use trades or days, not bars)
        ref: Reference Sharpe ratio (typically 0 or 0.5)

    Returns:
        Probability that true Sharpe > ref (0 to 1)
    """
    if n <= 1:
        return 0.0
    z = (sharpe - ref) * np.sqrt(max(1, n - 1))
    return 0.5 * (1.0 + erf(z / sqrt(2)))


def deflated_sharpe(sharpe: float, n: int, trials: int) -> float:
    """
    DSR: Sharpe ratio deflated for multiple testing.
    Conservative approximation: SR / (1 + log(trials))
    More rigorous version uses Extreme Value Theory (omitted for brevity).

    Args:
        sharpe: Observed Sharpe ratio
        n: Number of independent samples
        trials: Number of trials tested (unique parameter combinations)

    Returns:
        Deflated Sharpe ratio accounting for multiple comparisons
    """
    if trials <= 1:
        return sharpe
    return sharpe / (1.0 + log(max(2, trials)))


def pbo_fraction(oos_sharpes: list[float]) -> float:
    """
    Probability of Backtest Overfitting (simplified proxy).
    Measures fraction of OOS folds with Sharpe < 0.

    Args:
        oos_sharpes: List of OOS Sharpe ratios across folds

    Returns:
        Fraction of negative OOS Sharpe values (0 to 1)
    """
    if not oos_sharpes:
        return 0.0
    arr = np.array(oos_sharpes, dtype=float)
    return float(np.mean(arr < 0.0))
