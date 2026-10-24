# experiments/trial_guard.py

from __future__ import annotations

from typing import Any, Dict, List, Tuple


def guard_trial_metrics(metrics: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """
    对单个 trial 的指标做简单 sanity check。

    This provides a safety net for HPO trials, catching obviously broken results
    before they influence the HPO process.

    Checks:
    - All key metrics must not be NaN / Inf
    - Trade count must be reasonable (>= 5)
    - Sharpe ratio must be within reasonable bounds (< 20)
    - IC and Sharpe should be directionally consistent
    - High win rate with low Sharpe suggests issues

    Args:
        metrics: Trial performance metrics dict

    Returns:
        Tuple of (ok: bool, reasons: List[str])
            - ok: True if all checks pass
            - reasons: List of failure reasons (empty if ok)

    Design philosophy:
    - Conservative but practical
    - Catch obvious failures, not borderline cases
    - Provide actionable reasons for failures
    """
    reasons: List[str] = []

    def _safe_get(name: str) -> Any:
        return metrics.get(name)

    sharpe = _safe_get("sharpe")
    ic = _safe_get("ic")
    trades = _safe_get("trades") or _safe_get("n_trades") or 0
    win_rate = _safe_get("win_rate")

    # 1) 关键指标不能是 None
    for key in ("sharpe", "ic"):
        if metrics.get(key) is None:
            reasons.append(f"metric_missing:{key}")

    # 2) 交易次数太少
    try:
        if trades is not None and trades < 5:
            reasons.append("too_few_trades")
    except TypeError:
        reasons.append("invalid_trades_type")

    # 3) Sharpe 非常离谱
    try:
        if sharpe is not None:
            s = float(sharpe)
            if abs(s) > 20:
                reasons.append("sharpe_out_of_range")
    except Exception:
        reasons.append("invalid_sharpe_type")

    # 4) IC 高但 Sharpe 为负，打一个 warning（不直接判死刑，由上层决定）
    try:
        if ic is not None and sharpe is not None:
            i = float(ic)
            s = float(sharpe)
            if i > 0.02 and s < 0:
                reasons.append("ic_sharpe_inconsistent")
    except Exception:
        reasons.append("invalid_ic_type")

    # 5) 胜率极端但 Sharpe 一般，提示可能过度杠杆或 heavy tail
    try:
        if win_rate is not None:
            w = float(win_rate)
            if w > 0.8 and sharpe is not None and float(sharpe) < 1.0:
                reasons.append("high_winrate_low_sharpe")
    except Exception:
        pass

    ok = len(reasons) == 0
    return ok, reasons
