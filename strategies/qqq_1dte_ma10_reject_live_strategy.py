from __future__ import annotations

from strategies.ma_1dte_option_live_base import MA1DTEMA10RejectLiveStrategyBase, _safe_float


class QQQ1DTEMA10RejectLiveStrategy(MA1DTEMA10RejectLiveStrategyBase):
    """
    Thin QQQ wrapper around the shared 1DTE MA option live runtime.
    """

    def _strategy_slug(self) -> str:
        return "qqq_1dte_ma10_reject"

    def _default_underlying(self) -> str:
        return "QQQ"

    def _default_risk_symbol(self) -> str:
        return "OPT:QQQ:1DTE"

    def _default_live_parity_profile_id(self) -> str:
        return "qqq_option_ma_ext_v1"

    def _default_live_parity_snapshot_root(self) -> str:
        return "runtime/state/decision_snapshots"

    def _default_replay_strategy_id(self) -> str:
        return "ma10_reject_0324"
