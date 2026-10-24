"""Core engine package for reusable option replay components."""

from .engine import SpyOptionReplayBacktester
from .market_data import build_features, clean_intraday_bars, is_in_entry_window, load_option_quotes, load_spy_bars
from .market_data import infer_warmup_start_date, load_option_quotes_for_replay, resolve_indicator_profile, synth_option_quotes_from_bars
from .selector import (
    compute_contracts,
    describe_selection_snapshot,
    entry_fill_price,
    exit_fill_price,
    explain_contract_rejection,
    infer_iv_context,
    rank_contract_candidates,
    select_contract,
)
from .types import ContractQuote, OptionReplayStrategy, PendingEntry, PendingSetup, PositionState, ReplayConfig, StrategySignal
from .accounting import apply_entry_fill, apply_exit_fill, record_cash_ledger
from .audit import record_fill, record_order
from .calibration import (
    CalibrationConfig,
    build_calibration_report,
    build_replay_fill_comparison_df,
    load_live_fills_csv,
    match_live_to_replay_fills,
)
from services.option_replay.strategies.ma10_reject_0324 import (
    MA10Reject0324Strategy,
    aggressive_entry_allowed,
    classify_signal_regime,
    detect_setup,
)
from services.option_replay.strategies.orb_opening_breakout import OpeningRangeBreakoutStrategy
from services.option_replay.strategies.vwap_reclaim_continuation import VWAPReclaimContinuationStrategy
from services.option_replay.strategies import STRATEGY_REGISTRY, get_strategy, list_strategies

__all__ = [
    "ContractQuote",
    "CalibrationConfig",
    "MA10Reject0324Strategy",
    "OpeningRangeBreakoutStrategy",
    "VWAPReclaimContinuationStrategy",
    "OptionReplayStrategy",
    "PendingEntry",
    "PendingSetup",
    "PositionState",
    "ReplayConfig",
    "SpyOptionReplayBacktester",
    "StrategySignal",
    "aggressive_entry_allowed",
    "apply_entry_fill",
    "apply_exit_fill",
    "build_features",
    "clean_intraday_bars",
    "build_calibration_report",
    "build_replay_fill_comparison_df",
    "classify_signal_regime",
    "compute_contracts",
    "describe_selection_snapshot",
    "detect_setup",
    "entry_fill_price",
    "exit_fill_price",
    "explain_contract_rejection",
    "infer_iv_context",
    "infer_warmup_start_date",
    "is_in_entry_window",
    "get_strategy",
    "list_strategies",
    "load_option_quotes",
    "load_option_quotes_for_replay",
    "load_live_fills_csv",
    "load_spy_bars",
    "match_live_to_replay_fills",
    "rank_contract_candidates",
    "resolve_indicator_profile",
    "record_cash_ledger",
    "record_fill",
    "record_order",
    "select_contract",
    "synth_option_quotes_from_bars",
    "STRATEGY_REGISTRY",
]
