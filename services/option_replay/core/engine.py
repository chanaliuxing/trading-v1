"""Isolated replay backtester for the 2026-03-24 intraday option strategy.

This module is intentionally self-contained and does not modify or reuse the
shared research backtest engines. It is symbol-agnostic and currently defaults
to QQQ in the companion CLI.

- underlying 1m bars for signal generation
- derived 5m bars for context only
- real option chain snapshots from trading.options_quotes_1m
- conservative fill assumptions to approximate IBKR Adaptive/Bracket behavior
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any, Dict, List, Optional, Tuple

import math
import pandas as pd

from services.option_replay.core.audit import build_run_result, current_nav, record_entry_audit, record_skip
from services.option_replay.core.execution import (
    chain_for,
    chain_for_recent,
    close_position,
    create_pending_entry,
    fallback_exit_quote,
    mark_quote,
    try_fill_pending_entry,
)
from services.option_replay.core.market_data import (
    build_features,
    is_in_entry_window,
    load_option_quotes,
    load_spy_bars,
)
from services.option_replay.core.selector import (
    compute_contracts,
    entry_fill_price,
    exit_fill_price,
    explain_contract_rejection,
    infer_iv_context,
    select_contract,
)
from services.option_replay.core.types import (
    ContractQuote,
    OptionReplayStrategy,
    PendingEntry,
    PendingSetup,
    PositionState,
    ReplayConfig,
    StrategySignal,
)
from services.option_replay.strategies.ma10_reject_0324 import MA10Reject0324Strategy

ET = "America/New_York"


def _parse_hhmm(value: str) -> time:
    return datetime.strptime(value, "%H:%M").time()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        out = float(value)
        if math.isnan(out) or math.isinf(out):
            return default
        return out
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


class SpyOptionReplayBacktester:
    """Self-contained replay engine for the 0324 intraday options strategy."""

    def __init__(
        self,
        bars: pd.DataFrame,
        option_cache: Dict[Tuple[datetime, str], List[ContractQuote]],
        config: ReplayConfig | Dict[str, Any] | None = None,
        strategy: Optional[OptionReplayStrategy] = None,
        trade_start_date: Optional[str] = None,
    ) -> None:
        self.cfg = config if isinstance(config, ReplayConfig) else ReplayConfig.from_dict(config)
        self.df = build_features(bars, self.cfg).reset_index(drop=True)
        self.option_cache = option_cache
        self.strategy = strategy or MA10Reject0324Strategy()
        self.trade_start_date: Optional[date] = (
            datetime.strptime(trade_start_date, "%Y-%m-%d").date() if trade_start_date else None
        )
        self.cash = float(self.cfg.starting_cash)
        self.position: Optional[PositionState] = None
        self.pending_entry: Optional[PendingEntry] = None
        self.pending: Optional[PendingSetup] = None
        self.cross_reject_counts: Dict[int, int] = {}
        self.current_day: Optional[date] = None
        self.day_start_nav: float = float(self.cfg.starting_cash)
        self.day_branch_fill_counts: Dict[str, int] = {}
        self.last_quote_by_contract: Dict[Tuple[str, float, str], ContractQuote] = {}
        self.trades: List[Dict[str, Any]] = []
        self.skips: List[Dict[str, Any]] = []
        self.entry_audit: List[Dict[str, Any]] = []
        self.selection_audit: List[Dict[str, Any]] = []
        self.bracket_audit: List[Dict[str, Any]] = []
        self.orders: List[Dict[str, Any]] = []
        self.fills: List[Dict[str, Any]] = []
        self.cash_ledger: List[Dict[str, Any]] = []
        self.equity_curve: List[Dict[str, Any]] = []
        self._order_seq = 0

    def _chain_for(self, ts: datetime, right: str) -> List[ContractQuote]:
        return chain_for(self, ts, right)

    def _allocate_order_id(self, prefix: str) -> str:
        self._order_seq += 1
        return f"{prefix}_{self._order_seq}"

    def _chain_for_recent(self, ts: datetime, right: str) -> List[ContractQuote]:
        return chain_for_recent(self, ts, right)

    def _mark_quote(self, ts: datetime, expiry: str, strike: float, right: str) -> Optional[ContractQuote]:
        return mark_quote(self, ts, expiry, strike, right)

    def _reset_day(self, day: date) -> None:
        self.day_start_nav = float(self._current_nav())
        self.current_day = day
        self.pending = None
        self.pending_entry = None
        self.cross_reject_counts = {}
        self.day_branch_fill_counts = {}
        self.strategy.on_new_day()

    def _record_skip(self, ts: datetime, reason: str, **extra: Any) -> None:
        record_skip(self, ts, reason, **extra)

    def _record_entry_audit(self, ts: datetime, status: str, **extra: Any) -> None:
        record_entry_audit(self, ts, status, **extra)

    def _create_pending_entry(
        self,
        idx: int,
        trigger_row: pd.Series,
        setup: PendingSetup,
    ) -> None:
        create_pending_entry(self, idx, trigger_row, setup)

    def _try_fill_pending_entry(self, idx: int, row: pd.Series) -> None:
        try_fill_pending_entry(self, idx, row)

    def _fallback_exit_quote(self, ts: datetime, side: str) -> Optional[ContractQuote]:
        return fallback_exit_quote(self, ts, side)

    def _close_position(
        self,
        ts: datetime,
        quote: ContractQuote,
        underlying_px: float,
        qty: int,
        reason: str,
    ) -> None:
        close_position(self, ts, quote, underlying_px, qty, reason)

    def _manage_position(self, idx: int, row: pd.Series) -> None:
        if self.position is None:
            return
        self.strategy.manage_position(self, idx, row)

    def _maybe_schedule_setup(self, idx: int, row: pd.Series) -> None:
        ts = pd.Timestamp(row["ts"]).to_pydatetime().replace(second=0, microsecond=0)
        signal = self.strategy.detect_signal(self.df, idx, row, self.cfg)
        if signal is None:
            return
        self.pending = self.strategy.build_pending_setup(signal, ts, idx, row)

    def _maybe_trigger_entry(self, idx: int, row: pd.Series) -> None:
        if self.pending is None:
            return
        if self.pending_entry is not None:
            return
        ts = pd.Timestamp(row["ts"]).to_pydatetime().replace(second=0, microsecond=0)
        bars_since_setup = idx - self.pending.setup_idx
        if bars_since_setup <= 0:
            return
        confirm_window = max(1, int(self.cfg.confirm_window_bars))
        if bars_since_setup > confirm_window:
            self.pending = None
            return
        if not is_in_entry_window(ts, self.cfg):
            self.pending = None
            return

        close_px = _safe_float(row.get("close"))
        high_px = _safe_float(row.get("high"))
        low_px = _safe_float(row.get("low"))
        ma10 = _safe_float(row.get("ma10"))

        # Pending setup remains valid only while structure stays aligned.
        if self.pending.side == "CALL":
            if low_px < self.pending.low - 1e-9:
                self.pending = None
                return
            if ma10 > 0 and close_px <= ma10:
                self.pending = None
                return
        else:
            if high_px > self.pending.high + 1e-9:
                self.pending = None
                return
            if ma10 > 0 and close_px >= ma10:
                self.pending = None
                return

        if not self.strategy.confirm_signal(self.df, idx, row, self.pending, self.cfg):
            return

        decay_from = max(1, int(self.cfg.confirm_decay_from_bar))
        if bars_since_setup >= decay_from and self.pending.atr > 0:
            max_distance = float(self.cfg.confirm_decay_max_distance_atr) * self.pending.atr
            if self.pending.side == "CALL":
                if (close_px - self.pending.high) > max_distance:
                    self.pending = None
                    return
            else:
                if (self.pending.low - close_px) > max_distance:
                    self.pending = None
                    return

        self._create_pending_entry(idx, row, self.pending)
        self.pending = None

    def _current_nav(self) -> float:
        return current_nav(self)

    def run(self) -> Dict[str, Any]:
        for idx, row in self.df.iterrows():
            ts = pd.Timestamp(row["ts"]).to_pydatetime().replace(second=0, microsecond=0)
            if self.current_day != ts.date():
                self._reset_day(ts.date())
            if self.trade_start_date is not None and ts.date() < self.trade_start_date:
                # Warmup bars: update feature state only, no trading actions.
                self.equity_curve.append({"ts": ts.isoformat(), "equity": self._current_nav()})
                continue
            if self.position is not None:
                self._manage_position(idx, row)
            if self.position is None and self.pending_entry is not None:
                self._try_fill_pending_entry(idx, row)
            if self.position is None:
                self._maybe_trigger_entry(idx, row)
            if self.position is None and self.pending_entry is None and is_in_entry_window(ts, self.cfg):
                self._maybe_schedule_setup(idx, row)
            self.equity_curve.append({"ts": ts.isoformat(), "equity": self._current_nav()})

        if self.pending_entry is not None:
            self._record_skip(
                pd.Timestamp(self.df.iloc[-1]["ts"]).to_pydatetime().replace(second=0, microsecond=0),
                "entry_unresolved_at_end",
                side=self.pending_entry.side,
                grade=self.pending_entry.grade,
            )
            self.pending_entry = None
        if self.position is not None and self.position.last_quote is not None:
            last_ts = pd.Timestamp(self.df.iloc[-1]["ts"]).to_pydatetime().replace(second=0, microsecond=0)
            self._close_position(last_ts, self.position.last_quote, _safe_float(self.df.iloc[-1]["close"]), self.position.remaining, "FINAL_BAR_FLATTEN")

        return build_run_result(self)
