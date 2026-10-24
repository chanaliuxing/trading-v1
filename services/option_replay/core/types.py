"""Core type definitions for the reusable option replay engine."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import pandas as pd


@dataclass
class ReplayConfig:
    starting_cash: float = 100_000.0
    market_data_mode: str = "raw"
    market_data_session: str = "rth"
    indicator_profile: str = "standard_close_rth_sma"
    indicator_price_source: str = "close"
    indicator_ma_mode: str = "sma"
    clean_data_max_price_deviation_pct: float = 0.35
    clean_data_max_bar_range_pct: float = 0.20
    risk_per_trade_pct_a: float = 0.004
    risk_per_trade_pct_b: float = 0.002
    max_loss_pct_floor: float = 0.35
    max_loss_pct_cap: float = 0.85
    contract_multiplier: int = 100
    commission_per_contract: float = 0.65
    entry_start_am: str = "09:40"
    entry_end_am: str = "12:00"
    entry_start_pm: str = "13:30"
    entry_end_pm: str = "15:00"
    final_flatten: str = "15:50"
    fixed_contracts_per_trade: int = 4
    volume_mult_min: float = 1.10
    lower_wick_min_frac: float = 0.30
    upper_wick_min_frac: float = 0.30
    close_strength_min: float = 0.60
    breakout_buffer: float = 0.00
    confirm_window_bars: int = 2
    # Default trading profile does not use bar-3 decay.
    confirm_decay_from_bar: int = 99
    confirm_decay_max_distance_atr: float = 0.30
    enable_fresh_momentum_branch: bool = True
    momentum_max_bars_after_cross: int = 3
    momentum_body_min_frac: float = 0.55
    momentum_volume_mult_min: float = 1.20
    # Replay-only fresh-momentum confirm quality gates (default OFF).
    fresh_momentum_confirm_quality_enabled: bool = False
    fresh_momentum_confirm_require_close_break: bool = False
    fresh_momentum_confirm_min_body_frac: float = 0.45
    fresh_momentum_confirm_min_dir_close_strength: float = 0.65
    # Replay-only: scenario filter for fresh-momentum fake breakout failures.
    fresh_momentum_fakeout_filter_enabled: bool = False
    fresh_momentum_fakeout_min_break_atr: float = 0.10
    fresh_momentum_fakeout_min_close_break_atr: float = 0.02
    fresh_momentum_fakeout_max_body_frac: float = 0.35
    fresh_momentum_fakeout_min_adverse_wick_frac: float = 0.30
    fresh_momentum_fakeout_min_vol_vs_setup: float = 1.00
    # Trend-continuation breakout branch (experimental, default OFF).
    enable_trend_continuation_branch: bool = False
    trend_cont_breakout_lookback: int = 2
    trend_cont_body_min_frac: float = 0.50
    trend_cont_volume_mult_min: float = 0.80
    trend_cont_put_close_pos_max: float = 0.35
    trend_cont_call_close_pos_min: float = 0.65
    trend_cont_put_require_recent_cross: bool = False
    trend_cont_put_recent_cross_max_bars: int = 8
    trend_cont_put_consecutive_bars_below_ma10: int = 0
    trend_cont_put_consecutive_bars_below_ma200: int = 0
    trend_cont_put_max_high_over_ma10_atr: float = 0.0
    momentum_chase_profile: str = "neutral"
    momentum_entry_first_try_limit_bump_pct: float = 0.03
    momentum_entry_second_try_limit_bump_pct: float = 0.08
    revalidate_pending_before_fill: bool = True
    put_close_pos_max_for_entry: float = 0.80
    ma10_reject_ext_atr_max: float = 2.20
    enable_entry_quality_gate: bool = False
    quality_max_dist_ma10_atr: float = 0.80
    quality_max_body_frac: float = 0.45
    quality_min_dir_close_strength: float = 0.88
    # Additive branch mined from daily-best analysis; default OFF to preserve ext22 baseline behavior.
    enable_daily_best_additive_branch: bool = False
    daily_best_max_bars_after_cross: int = 8
    daily_best_volume_mult_min: float = 1.05
    daily_best_breakout_lookback: int = 3
    daily_best_max_dist_ma10_atr: float = 1.20
    daily_best_min_dist_vwap_atr: float = 0.0
    daily_best_max_body_frac: float = 0.90
    daily_best_dir_close_strength_min: float = 0.70
    daily_best_call_only: bool = False
    delta_min: float = 0.45
    delta_max: float = 0.55
    delta_fallback_min: float = 0.40
    delta_fallback_max: float = 0.60
    # Replay-only conditional low-delta branch (keeps global delta unchanged).
    low_delta_experiment_enabled: bool = False
    low_delta_signal_kinds_csv: str = "oracle0327_fallback_put"
    low_delta_require_trend_regime: bool = True
    low_delta_delta_min: float = 0.30
    low_delta_delta_max: float = 0.40
    low_delta_no_fallback: bool = True
    low_delta_require_5m_trend_gate: bool = True
    low_delta_5m_gate_allow_ma200_missing: bool = False
    low_delta_min_vol_ratio: float = 1.00
    # Replay-only branch side-effect guard (default OFF).
    branch_guard_enabled: bool = False
    branch_guard_signal_kinds_csv: str = "oracle0327_fallback_put"
    branch_guard_daily_drawdown_pct: float = 0.0
    branch_guard_max_entries_per_day: int = 0
    # Replay-only per-trade risk controls on selected branch signal kinds.
    branch_risk_control_enabled: bool = False
    branch_risk_signal_kinds_csv: str = "oracle0327_fallback_put"
    branch_contract_scale: float = 1.0
    branch_max_premium_expansion_pct: float = 0.0
    branch_softstop_min_hold_bars: int = 0
    branch_softstop_min_fav_r: float = 0.0
    branch_max_spread_pct: float = 0.0
    # Replay-only branch quality scoring gate.
    branch_quality_score_enabled: bool = False
    branch_quality_min_score: int = 0
    branch_quality_min_body_frac: float = 0.0
    branch_quality_max_adverse_wick_frac: float = 1.0
    branch_quality_min_vol_ratio: float = 0.0
    branch_quality_max_dist_ma10_atr: float = 0.0
    branch_quality_max_close_pos_put: float = 1.0
    branch_quality_min_close_pos_call: float = 0.0
    branch_quality_early_window_start: str = "09:40"
    branch_quality_early_window_end: str = "10:00"
    branch_quality_early_min_score_boost: int = 0
    # Replay-only branch risk-normalized sizing by structure risk.
    branch_risk_norm_enabled: bool = False
    branch_risk_norm_ref_r_atr: float = 0.8
    branch_risk_norm_min_scale: float = 0.5
    # Replay-only branch pre-stop reduction on reversal bar.
    branch_prestop_enabled: bool = False
    branch_prestop_hold_bars_min: int = 0
    branch_prestop_hold_bars_max: int = 0
    branch_prestop_body_frac_min: float = 0.0
    branch_prestop_close_pos_put_min: float = 1.0
    branch_prestop_close_pos_call_max: float = 0.0
    branch_prestop_high_over_ma10_atr_min: float = 0.0
    branch_prestop_low_under_ma10_atr_min: float = 0.0
    # Replay-only branch stale quote force-flatten guard.
    branch_quote_missing_force_exit_bars: int = 0
    # Replay-only oracle fail-fast exit (targeted early failure pattern).
    oracle_failfast_enabled: bool = False
    oracle_failfast_signal_kinds_csv: str = "oracle0327_fallback_put"
    oracle_failfast_eval_bars_max: int = 4
    oracle_failfast_min_adverse_r: float = 0.75
    oracle_failfast_max_fav_r: float = 0.15
    oracle_failfast_min_close_pos_put: float = 0.70
    oracle_failfast_max_close_pos_call: float = 0.30
    dte_preferred: int = 1
    dte_fallback_max: int = 2
    allow_aggressive_same_day: bool = True
    max_spread_pct: float = 0.12
    min_open_interest: int = 100
    iv_high_percentile: float = 0.80
    iv_fade_skip_abs: float = 0.015
    adaptive_entry_improve_pct: float = 0.20
    adaptive_entry_chase_pct: float = 0.35
    adaptive_exit_improve_pct: float = 0.10
    urgent_entry_improve_pct: float = 0.05
    urgent_entry_chase_pct: float = 0.60
    stop_exit_improve_pct: float = 0.00
    quote_staleness_min: int = 2
    chain_lookup_back_min: int = 2
    pending_entry_timeout_bars: int = 2
    continuation_pending_entry_timeout_bars: int = 0
    continuation_dynamic_reprice_to_ask: bool = False
    continuation_reprice_profile: str = ""
    continuation_max_premium_expansion_pct: float = 0.0
    # Replay-only: conditional relaxation for continuation premium expansion.
    continuation_relaxed_premium_expansion_pct: float = 0.0
    continuation_relax_require_5m_trend_gate: bool = False
    continuation_relax_5m_gate_allow_ma200_missing: bool = False
    continuation_relax_min_vol_ratio: float = 0.0
    # Replay-only: pre-entry structural filter for 0DTE execution research.
    prefilter_enabled: bool = False
    prefilter_signal_kinds_csv: str = "trend_continuation_put,fresh_momentum"
    prefilter_require_5m_trend_align: bool = True
    prefilter_5m_allow_ma200_missing: bool = True
    prefilter_min_vol_ratio: float = 1.0
    prefilter_put_close_pos_min: float = 0.10
    prefilter_put_close_pos_max: float = 0.75
    prefilter_call_close_pos_min: float = 0.25
    prefilter_call_close_pos_max: float = 0.90
    # V2 commonality replay-only tuning knobs.
    v2_max_dist_ma10_atr: float = 1.8
    v2_put_close_pos_max: float = 1.0
    v2_min_vol_ratio: float = 0.0
    v2_max_ma10_slope: float = 999.0
    layered_softstop_min_fav_r: float = 0.20
    layered_softstop_min_hold_bars: int = 5
    layered_softstop_signal_kind: str = "trend_continuation_put"
    allow_partial_fills: bool = False
    max_fill_contracts_per_event: int = 0

    @classmethod
    def from_dict(cls, overrides: Optional[Dict[str, object]] = None) -> "ReplayConfig":
        base = cls()
        if not overrides:
            return base
        data = asdict(base)
        data.update(overrides)
        return cls(**data)


@dataclass
class ContractQuote:
    ts: datetime
    expiry: str
    right: str
    strike: float
    dte: int
    bid: float
    ask: float
    mid: float
    iv: float
    delta: float
    gamma: float = 0.0
    theta: float = 0.0
    vega: float = 0.0
    open_interest: int = 0

    @property
    def spread(self) -> float:
        return max(0.0, self.ask - self.bid)

    @property
    def spread_pct(self) -> float:
        return self.spread / self.mid if self.mid > 0 else 1.0


@dataclass
class PendingSetup:
    side: str
    regime: str
    grade: str
    setup_ts: datetime
    setup_idx: int
    reject_number: Optional[int]
    high: float
    low: float
    atr: float
    volume_ratio: float
    strong_confirm_only: bool = False
    signal_kind: str = "ma10_reject"


@dataclass
class StrategySignal:
    side: str
    regime: str
    grade: str
    high: float
    low: float
    atr: float
    cross_idx: Optional[int] = None
    signal_kind: str = "ma10_reject"


@dataclass
class PendingEntry:
    order_id: str
    side: str
    regime: str
    grade: str
    created_ts: datetime
    trigger_ts: datetime
    trigger_idx: int
    expiry: str
    strike: float
    trigger_underlying: float
    limit_price: float
    entry_delta: float
    structure_stop: float
    r_per_share: float
    target_1r: float
    target_1p2r: float
    aggressive_contract: bool = False
    iv_high: bool = False
    contracts: int = 0
    attempt_count: int = 0
    filled_contracts: int = 0
    signal_kind: str = "ma10_reject"


@dataclass
class PositionState:
    entry_order_id: str
    side: str
    regime: str
    grade: str
    entry_ts: datetime
    entry_idx: int
    expiry: str
    strike: float
    entry_underlying: float
    entry_option: float
    entry_delta: float
    contracts: int
    remaining: int
    structure_stop: float
    r_per_share: float
    target_1r: float
    target_1p2r: float
    aggressive_contract: bool = False
    iv_high: bool = False
    signal_kind: str = "ma10_reject"
    stop_order_id: Optional[str] = None
    tp_order_id: Optional[str] = None
    best_fav_move_r: float = 0.0
    best_fav_idx: int = 0
    partial_taken: bool = False
    branch_prestop_reduced: bool = False
    quote_missing_bars: int = 0
    exit_reasons: List[str] = field(default_factory=list)
    last_quote: Optional[ContractQuote] = None


class OptionReplayStrategy(ABC):
    """Minimal strategy interface for the isolated single-leg intraday replay engine."""

    def on_new_day(self) -> None:
        """Reset per-day strategy state if needed."""

    @abstractmethod
    def detect_signal(self, df: pd.DataFrame, idx: int, row: pd.Series, cfg: ReplayConfig) -> Optional[StrategySignal]:
        """Return a candidate setup signal for the current bar, or None."""

    @abstractmethod
    def confirm_signal(
        self,
        df: pd.DataFrame,
        idx: int,
        row: pd.Series,
        pending: PendingSetup,
        cfg: ReplayConfig,
    ) -> bool:
        """Return True if the current bar confirms entry for the pending setup."""

    @abstractmethod
    def build_pending_setup(self, signal: StrategySignal, ts: datetime, idx: int, row: pd.Series) -> PendingSetup:
        """Convert a strategy signal into the engine's pending setup object."""

    @abstractmethod
    def build_position_targets(
        self,
        df: pd.DataFrame,
        idx: int,
        trigger_row: pd.Series,
        setup: PendingSetup,
        trigger_price: float,
    ) -> Tuple[float, float, float]:
        """Return structure_stop, target_1r, target_1p2r for a filled position."""

    @abstractmethod
    def manage_position(
        self,
        engine: "object",
        idx: int,
        row: pd.Series,
    ) -> bool:
        """Manage the active position; return True if an exit occurred."""


__all__ = [
    "ContractQuote",
    "OptionReplayStrategy",
    "PendingEntry",
    "PendingSetup",
    "PositionState",
    "ReplayConfig",
    "StrategySignal",
]
