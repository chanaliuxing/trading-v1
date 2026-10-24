from __future__ import annotations

from strategies.ma_1dte_option_live_base import MA1DTEMA10RejectLiveStrategyBase


class SPY1DTEMA10RejectLiveStrategy(MA1DTEMA10RejectLiveStrategyBase):
    """
    SPY live delegate for the MA option kernel path.

    Reuses the same execution/position-management implementation as QQQ while
    keeping SPY defaults, symbols, and runtime state isolated.
    """

    def _strategy_slug(self) -> str:
        return "spy_1dte_ma10_reject"

    def _default_underlying(self) -> str:
        return "SPY"

    def _default_risk_symbol(self) -> str:
        return "OPT:SPY:1DTE"

    def _default_live_parity_profile_id(self) -> str:
        return "spy_option_ma_ext_v1"

    def _default_live_parity_snapshot_root(self) -> str:
        return "runtime/state/decision_snapshots_live_v2"

    def _default_replay_strategy_id(self) -> str:
        return "ma_cross_stack_cont_v2_dual_exp"
