from dataclasses import dataclass
import numpy as np
import pandas as pd


@dataclass
class Metrics:
    expectancy: float
    sharpe: float
    sortino: float
    mdd: float
    hit: float
    pf: float


def _max_drawdown(returns: pd.Series) -> float:
    equity = (1.0 + returns).cumprod()
    peak = equity.cummax()
    dd = (equity / peak - 1.0).min()
    return float(dd)


def _sharpe(returns: pd.Series) -> float:
    r = returns.values
    return float(np.mean(r) / (np.std(r) + 1e-12))


def _sortino(returns: pd.Series) -> float:
    r = returns.values
    downside = r[r < 0]
    denom = np.std(downside) + 1e-12
    return float(np.mean(r) / denom)


def _expectancy(returns: pd.Series) -> float:
    wins = returns[returns > 0]
    losses = returns[returns <= 0]
    p = len(wins) / max(1, len(returns))
    ew = float(wins.mean()) if len(wins) else 0.0
    el = abs(float(losses.mean())) if len(losses) else 0.0
    return float(p * ew - (1 - p) * el)


def _profit_factor(returns: pd.Series) -> float:
    gw = float(returns[returns > 0].sum())
    gl = abs(float(returns[returns <= 0].sum()))
    return float((gw + 1e-12) / (gl + 1e-12))


def build_score(df: pd.DataFrame, ret_col: str = "ret_net") -> Metrics:
    r = df[ret_col].astype(float)
    return Metrics(
        expectancy=_expectancy(r),
        sharpe=_sharpe(r),
        sortino=_sortino(r),
        mdd=_max_drawdown(r),
        hit=float((r > 0).mean()),
        pf=_profit_factor(r),
    )

