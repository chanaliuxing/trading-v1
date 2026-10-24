# -*- coding: utf-8 -*-
"""
QQQ 0DTE long options strategy (live-capable, pipeline-compatible).

Design goals:
- Runs alongside existing stock/ETF strategies without symbol collisions.
- Uses internal option symbols: OPT:QQQ:YYYYMMDD:STRIKE:C|P
- Orders: MKT entry; cancel if not filled within 60s (kernel-level timeout).
- Exits are rule-based (TP1/runner/TP2/floor/stop/no-move/vwap) using option mid marks.

IMPORTANT:
- This strategy is not enabled by default. It must be explicitly configured in config.yml.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any, Dict, Optional, Deque, Tuple, List
from collections import deque
from zoneinfo import ZoneInfo
from pathlib import Path
import json
import uuid
import math
import os

from libs.strategy.base import StrategyAlgorithm, DecisionEnvelope, Freshness, OrderStyle
from libs.options.internal_symbol import format_internal_opt_symbol, parse_internal_opt_symbol
from services.market.options_quote_store import (
    get_quote_for_opt_symbol,
    latest_quote_status,
    query_option_rows,
    quote_minute_coverage,
)
from services.production.strategy_tradability import write_strategy_tradability_heartbeat
from services.strategy.strategy_base import make_decision_id

ET = ZoneInfo("America/New_York")


@dataclass
class _PendingEntry:
    direction: str  # LONG/SHORT
    level: float
    stage: str  # await_confirm
    created_at: datetime
    bars_waited: int = 0


@dataclass
class _PositionState:
    opt_symbol: str
    side: str  # CALL/PUT
    entry_time: datetime
    entry_underlying: float
    position_id: str = ""
    entry_decision_id: str = ""
    entry_trigger_id: str = ""
    entry_snapshot_id: str = ""
    entry_close: float = 0.0
    entry_vwap: float = 0.0
    entry_atr_1m: float = 0.0
    trace_id: str = ""
    tp1_done: bool = False
    filled: bool = False
    last_exit_reason: Optional[str] = None
    exit_attempts: int = 0
    early_fail_mode: str = "hard"
    early_fail_decided: bool = False
    early_fail_triggered: bool = False
    entry_counter_vwap: bool = False
    entry_first1_favorable_move: float = 0.0
    entry_first1_giveback_ratio: float = 1.0


@dataclass
class _MinuteBuilder:
    minute_start: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap_num: float
    last_vwap: float
    atr_d: float = 0.0

    def ingest(self, bar: Dict[str, Any]) -> None:
        self.high = max(self.high, float(bar.get("high") or self.close))
        self.low = min(self.low, float(bar.get("low") or self.close))
        self.close = float(bar.get("close") or self.close)
        vol = float(bar.get("volume") or 0.0)
        self.volume += vol
        vwap = float(bar.get("vwap") or 0.0)
        if vol > 0 and vwap > 0:
            self.vwap_num += vwap * vol
            self.last_vwap = vwap
        elif vwap > 0:
            self.last_vwap = vwap
        atr_d = float(bar.get("atr_d") or 0.0)
        if atr_d > 0:
            self.atr_d = atr_d

    def finalize(self) -> Dict[str, Any]:
        vwap = (self.vwap_num / self.volume) if self.volume > 0 else self.last_vwap
        return {
            "ts": self.minute_start + timedelta(minutes=1),
            "open": float(self.open),
            "high": float(self.high),
            "low": float(self.low),
            "close": float(self.close),
            "vwap": float(vwap or self.close),
            "volume": float(self.volume),
            "atr_d": float(self.atr_d or 0.0),
            "atr_1m": 0.0,
            "atr_1m_ma": 0.0,
            "vol_ma": None,
            "rvol": None,
        }


def _parse_hms(raw: Any, default: time) -> time:
    try:
        s = str(raw or "").strip()
        if not s:
            return default
        return time.fromisoformat(s)
    except Exception:
        return default


def _minute_floor(dt: datetime) -> datetime:
    return dt.astimezone(ET).replace(second=0, microsecond=0)


def _order_status_is_working(status: Any) -> bool:
    raw = str(status or "").strip().upper().replace(" ", "")
    if not raw:
        return True
    return raw not in {"FILLED", "CANCELLED", "APICANCELLED", "INACTIVE"}


class QQQ0DTEOptionsLiveStrategy(StrategyAlgorithm):
    def _order_belongs_to_strategy(self, order: Any) -> bool:
        ref = str(getattr(order, "order_ref", "") or "").strip()
        if ref:
            parts = [p.strip() for p in ref.split(":") if str(p).strip()]
            if str(self.strategy_id) in parts:
                return True
        return str(getattr(order, "strategy_id", "") or "").strip() == str(self.strategy_id)

    def _option_order_matches_scope(self, order: Any, *, opt_symbol: Optional[str] = None) -> bool:
        symbol = str(getattr(order, "symbol", "") or "").strip().upper()
        if not symbol.startswith("OPT:"):
            return False
        if opt_symbol is not None:
            return symbol == str(opt_symbol).strip().upper()
        try:
            return str(parse_internal_opt_symbol(symbol).underlying).upper() == self.underlying
        except Exception:
            return False

    def _has_strategy_owned_open_option_orders(self, orders: List[Any], *, opt_symbol: Optional[str] = None) -> bool:
        for order in orders or []:
            if not self._option_order_matches_scope(order, opt_symbol=opt_symbol):
                continue
            if not self._order_belongs_to_strategy(order):
                continue
            status = getattr(order, "status", "")
            if _order_status_is_working(status):
                return True
        return False

    def _strategy_owned_qty_cap(self, *, opt_symbol: Optional[str], account_qty: float) -> float:
        qty = max(0.0, float(account_qty or 0.0))
        if qty <= 0.0 or self._pos is None:
            return qty
        if opt_symbol is not None and str(getattr(self._pos, "opt_symbol", "") or "") != str(opt_symbol):
            return qty
        initial_contracts = int(getattr(self._pos, "initial_contracts", 0) or 0)
        if initial_contracts <= 0:
            return qty
        owned_cap = float(initial_contracts)
        if bool(getattr(self._pos, "partial_taken", False)) or bool(getattr(self._pos, "tp1_done", False)):
            owned_cap = max(0.0, float(initial_contracts - 2))
        return max(0.0, min(qty, owned_cap))

    def _resolve_local_audit_writer(self):
        engine = getattr(self.ctx, "engine", None)
        candidates = [
            getattr(engine, "audit_writer", None),
            getattr(getattr(engine, "_strategy_engine", None), "audit_writer", None),
        ]
        for writer in candidates:
            if writer is not None and hasattr(writer, "emit"):
                return writer
        return None

    def _make_audit_ids(self) -> tuple[str, str, str]:
        decision_id = str(make_decision_id() or "").strip() or str(uuid.uuid4())
        trigger_id = decision_id
        trace_id = decision_id
        return trigger_id, decision_id, trace_id

    def _prepare_entry_intent(
        self,
        *,
        now: datetime,
        side: str,
        spot: float,
        entry_bar: Dict[str, Any],
        quote: Dict[str, Any],
        trigger_id: str,
        decision_id: str,
        trace_id: str,
    ) -> tuple[bool, Dict[str, Any]]:
        return True, {}

    def _skip_entry_intent_submit(
        self,
        *,
        now: datetime,
        side: str,
        env: DecisionEnvelope,
        quote: Dict[str, Any],
        trigger_id: str,
        decision_id: str,
        trace_id: str,
    ) -> bool:
        return False

    def _emit_local_audit(
        self,
        *,
        stage: str,
        severity: str = "info",
        status: Optional[str] = None,
        reason: Optional[str] = None,
        symbol: Optional[str] = None,
        decision_id: Optional[str] = None,
        trigger_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        writer = self._resolve_local_audit_writer()
        if writer is None:
            return
        strategy_run_id = str(getattr(self.ctx, "strategy_run_id", "") or "").strip() or None
        try:
            writer.emit(
                domain="strategy",
                component="qqq_0dte_opt_live_strategy",
                stage=stage,
                severity=severity,
                strategy_id=self.strategy_id,
                strategy_run_id=strategy_run_id,
                trigger_id=str(trigger_id or "").strip() or None,
                decision_id=str(decision_id or "").strip() or None,
                trace_id=str(trace_id or "").strip() or None,
                symbol=symbol or getattr(self, "underlying", None),
                status=status,
                reason=reason,
                payload=payload,
            )
        except Exception as exc:
            self.ctx.log.warning(
                "qqq_0dte_local_audit_emit_failed",
                strategy_id=str(self.strategy_id),
                stage=stage,
                error=str(exc),
            )

    async def initialize(self) -> None:
        extras = self.params.extras or {}
        self.underlying = str(extras.get("underlying", "QQQ")).strip().upper()
        self.risk_symbol = str(extras.get("risk_symbol", f"OPT:{self.underlying}:0DTE")).strip().upper()
        self._source_bar_sec = max(5, int(extras.get("realtime_bar_sec", 5) or 5))
        self._rfe_primary_enabled = bool(extras.get("rfe_primary_enabled", True))
        self._rfe_primary_grace_sec = max(0, int(extras.get("rfe_primary_grace_sec", 75)))

        self._bars: Deque[Dict[str, Any]] = deque(maxlen=600)
        self._trs: Deque[float] = deque(maxlen=200)
        self._vols: Deque[float] = deque(maxlen=200)
        self._minute_builder: Optional[_MinuteBuilder] = None
        self._pending: Optional[_PendingEntry] = None
        self._pos: Optional[_PositionState] = None
        self._day: Optional[str] = None
        self._trades_filled_today: int = 0
        self._first_trade_exit_reason: Optional[str] = None
        self._last_entry_reject_log_key: Optional[Tuple[str, str, str]] = None
        self._last_mark_missing_log_ts: Optional[datetime] = None
        self._quote_hb_pause_new_entries: bool = False
        self._quote_hb_fail_streak: int = 0
        self._quote_hb_fresh_streak: int = 0
        self._quote_hb_last_log_key: Optional[Tuple[str, str]] = None
        self._last_entry_eval_minute: Optional[datetime] = None
        self._entry_block_minute_key: Optional[str] = None
        self._entry_block_minute_counts: Dict[str, int] = {}
        self._entry_block_day_counts: Dict[str, int] = {}
        self._quote_health_checked: bool = False
        self._quote_health_block_day: bool = False
        self._quote_health_cov_pct: float = 0.0
        self._quote_health_recovery_last_log_key: Optional[Tuple[str, str]] = None
        self._last_pos_diag_log_ts: Optional[datetime] = None
        self._last_minute_heartbeat_log_ts: Optional[datetime] = None
        self._tradability_heartbeat_task: Optional[asyncio.Task] = None
        self._last_signal_gate_reason: str = "UNKNOWN"
        self._last_completed_minute_ts: Optional[datetime] = None
        self._boot_wall_clock = datetime.now(ET)
        self._data_mode = "rfe_pending" if self._rfe_primary_enabled else "bar5s_fallback"
        self._data_mode_locked = not self._rfe_primary_enabled
        self._last_data_mode_log_key: Optional[Tuple[str, str]] = None

        self.ctx.log.info(
            "qqq_0dte_strategy_boot",
            strategy_id=str(self.strategy_id),
            exit_logic_version="2026-03-11-lmt-exit-diag-v1",
            no_new_entry_after=str(extras.get("no_new_entry_after", "14:50:00")),
            force_flatten_time=str(extras.get("force_flatten_time", "15:58:00")),
            tp1_mult=float(extras.get("tp1_mult", 1.20)),
            tp2_mult=float(extras.get("tp2_mult", 2.50)),
            stop_loss_mult=float(extras.get("stop_loss_mult", 0.65)),
            no_move_min_hold_min=float(extras.get("no_move_min_hold_min", 5.0)),
            no_move_min_opt_return=float(extras.get("no_move_min_opt_return", 0.10)),
            vwap_exit_minutes=float(extras.get("vwap_exit_minutes", 12.0)),
            exit_degraded_nbbo_max_age_sec=float(extras.get("exit_degraded_nbbo_max_age_sec", 5.0)),
            exit_quote_lookback_min=int(extras.get("exit_quote_lookback_min", 3)),
            exit_tif=str(extras.get("exit_tif", "IOC")).upper(),
            source_bar_sec=int(self._source_bar_sec),
            rfe_primary_enabled=bool(self._rfe_primary_enabled),
            rfe_primary_grace_sec=int(self._rfe_primary_grace_sec),
            data_mode=str(self._data_mode),
        )

        if self._rfe_primary_enabled:
            rfe_spec = dict(extras.get("realtime_feature_engine") or {})
            rfe_spec.setdefault("emit_on", "1m")
            rfe_spec.setdefault("sink", {"enabled": False})
            try:
                await self.ctx.enable_realtime_feature_engine([self.underlying], spec=rfe_spec)
                self._log_data_mode(self._boot_wall_clock, "RFE_PENDING", "redis_primary_waiting_for_first_emit")
            except Exception as exc:
                self._data_mode = "bar5s_fallback"
                self._data_mode_locked = True
                self.ctx.log.warning(
                    "qqq_0dte_rfe_enable_failed",
                    strategy_id=str(self.strategy_id),
                    underlying=self.underlying,
                    error=str(exc),
                )
                self._log_data_mode(self._boot_wall_clock, "BAR5S_FALLBACK", "rfe_enable_failed")

        # Keep 5s subscription as the only live fallback path; do not rely on CH 1m.
        try:
            await self.ctx.subscribe_features([self.underlying], bar_sec=self._source_bar_sec)
        except Exception:
            pass
        if "PYTEST_CURRENT_TEST" not in os.environ:
            self._tradability_heartbeat_task = asyncio.create_task(
                self._run_tradability_heartbeat_loop(),
                name=f"option_tradability_heartbeat:{self.strategy_id}",
            )

    async def on_features(self, symbol: str, features: Dict[str, Any]) -> None:
        if str(symbol or "").strip().upper() != self.underlying:
            return

        now = self._parse_ts(features)
        self._daily_reset(now)
        wall_now = datetime.now(ET)
        source = self._classify_feature_source(features)

        if source == "rfe_1m":
            if self._data_mode == "bar5s_fallback":
                self._log_data_mode(now, "BAR5S_FALLBACK", "ignoring_rfe_after_intraday_fallback")
                return
            self._data_mode = "rfe_primary"
            self._data_mode_locked = True
            self._log_data_mode(now, "RFE_PRIMARY", "first_rfe_emit")
            bar = self._features_to_bar(now, features)
            await self._process_completed_minute_bar(now, bar, wall_now=wall_now)
            return

        if self._source_bar_sec < 60:
            if self._data_mode == "rfe_pending":
                grace_deadline = self._boot_wall_clock + timedelta(seconds=self._rfe_primary_grace_sec)
                if wall_now < grace_deadline:
                    return
                self._data_mode = "bar5s_fallback"
                self._data_mode_locked = True
                self._log_data_mode(now, "BAR5S_FALLBACK", "rfe_grace_elapsed")
            if self._data_mode == "rfe_primary":
                return
            completed = self._ingest_subminute_bar(now, features)
            if completed is None:
                return
            minute_ts, minute_bar = completed
            await self._process_completed_minute_bar(minute_ts, minute_bar, wall_now=wall_now)
            return

        bar = self._features_to_bar(now, features)
        await self._process_completed_minute_bar(now, bar, wall_now=wall_now)

    async def _process_completed_minute_bar(
        self,
        now: datetime,
        bar: Dict[str, Any],
        *,
        wall_now: Optional[datetime] = None,
    ) -> None:
        wall_now = wall_now or datetime.now(ET)
        minute_key = now.replace(second=0, microsecond=0)
        if self._last_completed_minute_ts == minute_key:
            return
        self._last_completed_minute_ts = minute_key
        self._enrich_bar(bar)
        self._bars.append(bar)

        if await self._sync_and_manage_position(now, bar):
            return

        if self._last_entry_eval_minute == minute_key:
            return
        self._last_entry_eval_minute = minute_key
        self._log_minute_heartbeat(now, bar)
        await self._maybe_enter(now, bar, wall_now=wall_now)

    # -------------------- Parsing --------------------
    def _parse_ts(self, features: Dict[str, Any]) -> datetime:
        ts = features.get("ts")
        if hasattr(ts, "to_pydatetime"):
            return ts.to_pydatetime().astimezone(ET)
        if isinstance(ts, datetime):
            return ts.astimezone(ET)
        try:
            raw = float(ts)
            if raw > 100_000_000_000_000:
                return datetime.fromtimestamp(raw / 1_000_000.0, tz=ET)
            if raw > 100_000_000_000:
                return datetime.fromtimestamp(raw / 1000.0, tz=ET)
            return datetime.fromtimestamp(raw, tz=ET)
        except Exception:
            return datetime.now(ET)

    def _classify_feature_source(self, features: Dict[str, Any]) -> str:
        ts = features.get("ts")
        try:
            raw = float(ts)
            if raw > 1_000_000_000_000_00:
                return "rfe_1m"
        except Exception:
            pass
        if any(str(k).endswith("_1m") for k in features.keys()):
            return "rfe_1m"
        return "subminute"

    def _log_data_mode(self, now: datetime, mode: str, reason: str) -> None:
        minute_key = now.strftime("%Y-%m-%d %H:%M")
        log_key = (minute_key, mode)
        if self._last_data_mode_log_key == log_key:
            return
        self._last_data_mode_log_key = log_key
        self.ctx.log.info(
            "qqq_0dte_data_mode",
            ts=now.strftime("%Y-%m-%d %H:%M:%S"),
            strategy_id=str(self.strategy_id),
            underlying=self.underlying,
            mode=str(mode),
            reason=str(reason),
            source_bar_sec=int(self._source_bar_sec),
            rfe_primary_enabled=bool(self._rfe_primary_enabled),
        )
        self._emit_local_audit(
            stage="data_mode",
            status=str(mode),
            reason=str(reason),
            symbol=self.underlying,
            payload={
                "underlying": self.underlying,
                "mode": str(mode),
                "source_bar_sec": int(self._source_bar_sec),
                "rfe_primary_enabled": bool(self._rfe_primary_enabled),
            },
        )

    def _features_to_bar(self, now: datetime, features: Dict[str, Any]) -> Dict[str, Any]:
        def f(key: str) -> float:
            try:
                return float(features.get(key, 0.0) or 0.0)
            except Exception:
                return 0.0

        def f_any(keys: Tuple[str, ...], default: float = 0.0) -> float:
            for key in keys:
                try:
                    raw = features.get(key, None)
                    if raw is None:
                        continue
                    val = float(raw or 0.0)
                    if math.isfinite(val):
                        return val
                except Exception:
                    continue
            return float(default)

        close = f("close")
        vwap = f_any(("vwap", "vwap_1m"), default=close)
        if vwap <= 0.0 and close > 0.0:
            vwap = close
        volume = f_any(("volume", "vol", "bar_volume", "total_volume"), default=0.0)

        return {
            "ts": now,
            "open": f("open"),
            "high": f("high"),
            "low": f("low"),
            "close": close,
            "vwap": vwap,
            "volume": volume,
            "atr_d": f("atr_d"),
            "atr_1m": 0.0,
            "atr_1m_ma": 0.0,
            "vol_ma": None,
            "rvol": None,
        }

    def _subminute_parent_minute(self, now: datetime) -> datetime:
        # Poll-based 5s bars arrive as end timestamps; assign the boundary bar
        # (e.g. 09:31:00) to the minute that just completed.
        return _minute_floor(now - timedelta(microseconds=1))

    def _new_minute_builder(self, minute_start: datetime, bar: Dict[str, Any]) -> _MinuteBuilder:
        close = float(bar.get("close") or 0.0)
        vol = float(bar.get("volume") or 0.0)
        vwap = float(bar.get("vwap") or close)
        return _MinuteBuilder(
            minute_start=minute_start,
            open=float(bar.get("open") or close),
            high=float(bar.get("high") or close),
            low=float(bar.get("low") or close),
            close=close,
            volume=vol,
            vwap_num=(vwap * vol) if (vol > 0 and vwap > 0) else 0.0,
            last_vwap=vwap if vwap > 0 else close,
            atr_d=float(bar.get("atr_d") or 0.0),
        )

    def _ingest_subminute_bar(
        self,
        now: datetime,
        features: Dict[str, Any],
    ) -> Optional[Tuple[datetime, Dict[str, Any]]]:
        subbar = self._features_to_bar(now, features)
        minute_start = self._subminute_parent_minute(now)

        if self._minute_builder is None:
            self._minute_builder = self._new_minute_builder(minute_start, subbar)
            return None

        if minute_start == self._minute_builder.minute_start:
            self._minute_builder.ingest(subbar)
            return None

        completed = self._minute_builder.finalize()
        completed_ts = completed["ts"]
        self._minute_builder = self._new_minute_builder(minute_start, subbar)
        return completed_ts, completed

    def _enrich_bar(self, bar: Dict[str, Any]) -> None:
        # Maintain minimal 1m ATR + RVOL/ATR-expansion inputs (no external deps).
        close = float(bar.get("close") or 0.0)
        high = float(bar.get("high") or close)
        low = float(bar.get("low") or close)
        prev_close = float(self._bars[-1].get("close") or close) if self._bars else close
        tr = max(abs(high - low), abs(high - prev_close), abs(low - prev_close))
        if math.isfinite(tr):
            self._trs.append(float(tr))
        vol = float(bar.get("volume") or 0.0)
        if math.isfinite(vol):
            self._vols.append(float(vol))

        if len(self._trs) >= 14:
            bar["atr_1m"] = float(sum(list(self._trs)[-14:]) / 14.0)

        extras = self.params.extras or {}
        rvol_window = int(extras.get("rvol_window", 20))
        if len(self._vols) >= max(5, min(rvol_window, len(self._vols))):
            window = list(self._vols)[-rvol_window:]
            vol_ma = sum(window) / max(1, len(window))
            bar["vol_ma"] = float(vol_ma)
            bar["rvol"] = (float(vol) / float(vol_ma)) if vol_ma > 0 else None

        atr_ma_window = int(extras.get("atr_expansion_window", 20))
        if len(self._trs) >= max(5, atr_ma_window):
            atr_series = list(self._trs)[-atr_ma_window:]
            bar["atr_1m_ma"] = float(sum(atr_series) / max(1, len(atr_series)))

    def _daily_reset(self, now: datetime) -> None:
        day = now.strftime("%Y-%m-%d")
        if self._day != day:
            self._day = day
            self._trades_filled_today = 0
            self._first_trade_exit_reason = None
            self._pending = None
            self._pos = None
            self._trs.clear()
            self._vols.clear()
            self._bars.clear()
            self._last_entry_reject_log_key = None
            self._last_mark_missing_log_ts = None
            self._quote_hb_pause_new_entries = False
            self._quote_hb_fail_streak = 0
            self._quote_hb_fresh_streak = 0
            self._quote_hb_last_log_key = None
            self._last_entry_eval_minute = None
            self._entry_block_minute_key = None
            self._entry_block_minute_counts = {}
            self._entry_block_day_counts = {}
            self._quote_health_checked = False
            self._quote_health_block_day = False
            self._quote_health_cov_pct = 0.0
            self._quote_health_recovery_last_log_key = None
            self._last_signal_gate_reason = "UNKNOWN"
            self._minute_builder = None
            self._last_completed_minute_ts = None
            self._data_mode = "rfe_pending" if self._rfe_primary_enabled else "bar5s_fallback"
            self._data_mode_locked = not self._rfe_primary_enabled
            self._boot_wall_clock = datetime.now(ET)
            self._last_data_mode_log_key = None

    async def on_shutdown(self, reason: str) -> None:
        task = getattr(self, "_tradability_heartbeat_task", None)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            self._tradability_heartbeat_task = None
        try:
            await self.ctx.disable_realtime_feature_engine()
        except Exception:
            pass

    async def _run_tradability_heartbeat_loop(self) -> None:
        interval_sec = max(10.0, float((self.params.extras or {}).get("tradability_heartbeat_interval_sec", 30.0)))
        while True:
            self._emit_tradability_heartbeat(datetime.now(ET), source="option_idle_heartbeat")
            await asyncio.sleep(interval_sec)

    def _tradability_blockers(self, now: datetime, bar: Optional[Dict[str, Any]] = None) -> tuple[List[str], Dict[str, Any]]:
        extras = self.params.extras or {}
        quote_status = self._latest_quote_status(now)
        quote_source = str((quote_status or {}).get("source") or "unknown")
        quote_age_sec = None if quote_status is None else float(quote_status.get("age_sec") or 0.0)
        entry_start = _parse_hms(extras.get("ab_time_start", "09:40:00"), time(9, 40))
        entry_end = _parse_hms(
            extras.get("no_new_entry_after", extras.get("ab_time_end", "15:00:00")),
            time(15, 0),
        )
        in_entry_window = entry_start <= now.time() < entry_end
        quote_max_age_sec = max(10.0, float(extras.get("quote_hb_max_age_sec", 120.0)))
        min_warmup_bars = max(1, int(extras.get("tradability_min_warmup_bars", extras.get("min_warmup_bars", 210))))
        blockers: List[str] = []
        data_blockers: List[str] = []
        if self._quote_health_block_day:
            blockers.append("quote_health_block_day")
            data_blockers.append("quote_health_block_day")
        warmup_ready = len(self._bars) >= min_warmup_bars
        quote_fresh = quote_status is not None and quote_age_sec is not None and quote_age_sec <= quote_max_age_sec
        if not warmup_ready:
            data_blockers.append("insufficient_warmup")
        if quote_status is None:
            data_blockers.append("quote_missing")
        elif not quote_fresh:
            data_blockers.append("quote_stale")
        if in_entry_window and not warmup_ready:
            blockers.append("insufficient_warmup")
        if in_entry_window and quote_status is None:
            blockers.append("quote_missing")
        elif in_entry_window and quote_age_sec is not None and quote_age_sec > quote_max_age_sec:
            blockers.append("quote_stale")
        if in_entry_window and self._quote_hb_pause_new_entries:
            blockers.append("quote_heartbeat_paused")
            data_blockers.append("quote_heartbeat_paused")
        idle_ok = not bool(in_entry_window)
        data_health_ok = not bool(data_blockers)
        tradable_for_entry = bool(in_entry_window and not blockers and data_health_ok)
        if tradable_for_entry:
            readiness_state = "entry_ready"
        elif idle_ok:
            readiness_state = "idle_not_in_entry_window"
        else:
            readiness_state = "entry_blocked"
        details = {
            "minute": _minute_floor(now).strftime("%Y-%m-%d %H:%M"),
            "bars": int(len(self._bars)),
            "min_warmup_bars": int(min_warmup_bars),
            "warmup_ready": bool(warmup_ready),
            "has_pending": bool(self._pending is not None),
            "has_position": bool(self._pos is not None),
            "trades_filled_today": int(self._trades_filled_today),
            "quote_health_block_day": bool(self._quote_health_block_day),
            "quote_heartbeat_paused": bool(self._quote_hb_pause_new_entries),
            "quote_source": quote_source,
            "quote_age_sec": quote_age_sec,
            "quote_max_age_sec": float(quote_max_age_sec),
            "quote_fresh": bool(quote_fresh),
            "in_entry_window": bool(in_entry_window),
            "idle_ok": bool(idle_ok),
            "tradable_for_entry": bool(tradable_for_entry),
            "data_health_ok": bool(data_health_ok),
            "data_blockers": list(dict.fromkeys(data_blockers)),
            "readiness_state": readiness_state,
            "close": float((bar or {}).get("close") or 0.0),
            "vwap": float((bar or {}).get("vwap") or 0.0),
        }
        return blockers, details

    def _emit_tradability_heartbeat(
        self,
        now: datetime,
        *,
        source: str,
        bar: Optional[Dict[str, Any]] = None,
    ) -> tuple[List[str], Dict[str, Any]]:
        blockers, details = self._tradability_blockers(now, bar=bar)
        tradable_for_entry = bool(details.get("tradable_for_entry"))
        heartbeat_blockers = list(blockers)
        if not tradable_for_entry and not heartbeat_blockers:
            heartbeat_blockers.append(str(details.get("readiness_state") or "not_entry_ready"))
        write_strategy_tradability_heartbeat(
            strategy_id=str(self.strategy_id),
            tradable=tradable_for_entry,
            blockers=heartbeat_blockers,
            source=source,
            details={
                **details,
                "tradable": tradable_for_entry,
                "symbol": str(self.underlying),
                "stage": "minute_heartbeat" if source == "option_minute_heartbeat" else "idle_heartbeat",
                "reason": "HEARTBEAT" if tradable_for_entry else ",".join(heartbeat_blockers),
                "trace_id": f"{self.strategy_id}:{_minute_floor(now).strftime('%Y%m%d%H%M')}:heartbeat",
            },
        )
        return blockers, details

    # -------------------- Entry --------------------
    async def _maybe_enter(self, now: datetime, bar: Dict[str, Any], wall_now: Optional[datetime] = None) -> None:
        extras = self.params.extras or {}
        wall_now = wall_now or datetime.now(ET)
        self._flush_entry_block_logs(now)
        self._update_quote_data_health_gate(now)
        self._maybe_release_quote_data_health_gate(now)

        gate_start = _parse_hms(extras.get("ab_time_start", "09:30:00"), time(9, 30))
        gate_end = _parse_hms(extras.get("ab_time_end", "15:59:00"), time(15, 59))
        if now.time() < gate_start or now.time() > gate_end:
            self._pending = None
            self._record_entry_block(now, "OUTSIDE_AB_WINDOW")
            return

        no_new_after = _parse_hms(extras.get("no_new_entry_after", "14:50:00"), time(14, 50))
        if now.time() >= no_new_after:
            self._pending = None
            self._record_entry_block(now, "AFTER_NO_NEW_ENTRY")
            return

        max_trades = int(extras.get("max_trades_per_day", 2))
        if self._trades_filled_today >= max_trades:
            self._pending = None
            self._record_entry_block(now, "MAX_TRADES_REACHED")
            return
        if self._quote_health_block_day:
            self._pending = None
            self._record_entry_block(now, "DATA_HEALTH_BLOCK_DAY")
            return
        if not self._quote_heartbeat_allows_entry(now):
            self._pending = None
            self._record_entry_block(now, "QUOTE_HEARTBEAT_PAUSED")
            return

        # Second trade gate: only allow trade #2 if trade #1 ended in "bad exit"
        # (no hindsight; uses realized exit reason from trade #1).
        if self._trades_filled_today >= 1 and max_trades >= 2:
            enabled = bool(extras.get("second_trade_only_if_first_bad_exit", True))
            if enabled:
                allow_list = extras.get("second_trade_first_exit_allow", ["HARD_STOP", "LATE_NO_MOVE_EXIT"])
                allow = {str(x).strip().upper() for x in (allow_list or []) if str(x).strip()}
                if allow and str(self._first_trade_exit_reason or "").upper() not in allow:
                    self._pending = None
                    self._record_entry_block(now, "SECOND_TRADE_GATE")
                    return

        # Reserve second trade
        reserve_after_raw = extras.get("reserve_trade_after_time", "10:30:00")
        reserve_count = int(extras.get("reserve_trade_count", 1))
        if reserve_after_raw and reserve_count > 0 and max_trades > reserve_count:
            reserve_after = _parse_hms(reserve_after_raw, time(10, 30))
            early_cap = max_trades - reserve_count
            if now.time() < reserve_after and self._trades_filled_today >= early_cap:
                self._pending = None
                self._record_entry_block(now, "RESERVE_GATE")
                return

        if len(self._bars) < 3:
            self._record_entry_block(now, "INSUFFICIENT_BARS")
            return

        close = float(bar["close"] or 0.0)
        vwap = float(bar["vwap"] or 0.0)
        if close <= 0 or vwap <= 0:
            self._record_entry_block(now, "INVALID_BAR_PRICE")
            return

        prev = self._bars[-2]
        prev_high = float(prev["high"] or 0.0)
        prev_low = float(prev["low"] or 0.0)

        confirm_loc_min = float(extras.get("confirm_bar_close_loc_min", 0.55))
        spread_max_pct = float(extras.get("spread_max_pct", 0.15))

        # 0DTE trend filter: RVOL > threshold OR ATR expansion (prevents theta chop entries)
        rvol_min = float(extras.get("rvol_min", 1.5))
        atr_expansion_mult = float(extras.get("atr_expansion_mult", 1.2))
        rvol = bar.get("rvol")
        rvol_ok = rvol is not None and float(rvol) >= rvol_min
        atr_1m = float(bar.get("atr_1m") or 0.0)
        atr_1m_ma = float(bar.get("atr_1m_ma") or 0.0)
        atr_ok = atr_1m_ma > 0 and atr_1m >= atr_expansion_mult * atr_1m_ma
        trend_ok = bool(rvol_ok or atr_ok)

        # Stage machine: breakout -> confirm -> entry
        if self._pending is not None:
            self._pending.bars_waited += 1
            direction = self._pending.direction
            level = self._pending.level
            if direction == "LONG":
                if close > level and close > vwap and self._close_loc(bar) >= confirm_loc_min:
                    if trend_ok and self._enter_with_gates(now, bar, direction="LONG", breakout_level=level):
                        trigger_id, decision_id, trace_id = self._make_audit_ids()
                        self.ctx.log.info(
                            "qqq_0dte_confirm_pass",
                            ts=now.strftime("%Y-%m-%d %H:%M:%S"),
                            strategy_id=str(self.strategy_id),
                            direction="LONG",
                            close=float(close),
                            vwap=float(vwap),
                            level=float(level),
                            trend_ok=bool(trend_ok),
                        )
                        self._emit_local_audit(
                            stage="entry_confirmed",
                            status="confirmed",
                            reason="CONFIRM_PASS",
                            symbol=self.underlying,
                            decision_id=decision_id,
                            trigger_id=trigger_id,
                            trace_id=trace_id,
                            payload={
                                "direction": "LONG",
                                "close": float(close),
                                "vwap": float(vwap),
                                "level": float(level),
                                "trend_ok": bool(trend_ok),
                                "path": "A",
                            },
                        )
                        await self._submit_entry(
                            now,
                            side="CALL",
                            spot=close,
                            entry_bar=bar,
                            spread_max_pct=spread_max_pct,
                            wall_now=wall_now,
                            audit_ids=(trigger_id, decision_id, trace_id),
                        )
                    else:
                        self._record_entry_block(
                            now,
                            "TREND_FILTER_FAIL"
                            if not trend_ok
                            else f"SIGNAL_GATE_FAIL_{self._last_signal_gate_reason}",
                        )
                else:
                    self._record_entry_block(now, "CONFIRM_NOT_MET")
                self._pending = None
            else:
                if close < level and close < vwap and self._close_loc(bar) <= (1.0 - confirm_loc_min):
                    if trend_ok and self._enter_with_gates(now, bar, direction="SHORT", breakout_level=level):
                        trigger_id, decision_id, trace_id = self._make_audit_ids()
                        self.ctx.log.info(
                            "qqq_0dte_confirm_pass",
                            ts=now.strftime("%Y-%m-%d %H:%M:%S"),
                            strategy_id=str(self.strategy_id),
                            direction="SHORT",
                            close=float(close),
                            vwap=float(vwap),
                            level=float(level),
                            trend_ok=bool(trend_ok),
                        )
                        self._emit_local_audit(
                            stage="entry_confirmed",
                            status="confirmed",
                            reason="CONFIRM_PASS",
                            symbol=self.underlying,
                            decision_id=decision_id,
                            trigger_id=trigger_id,
                            trace_id=trace_id,
                            payload={
                                "direction": "SHORT",
                                "close": float(close),
                                "vwap": float(vwap),
                                "level": float(level),
                                "trend_ok": bool(trend_ok),
                                "path": "A",
                            },
                        )
                        await self._submit_entry(
                            now,
                            side="PUT",
                            spot=close,
                            entry_bar=bar,
                            spread_max_pct=spread_max_pct,
                            wall_now=wall_now,
                            audit_ids=(trigger_id, decision_id, trace_id),
                        )
                    else:
                        self._record_entry_block(
                            now,
                            "TREND_FILTER_FAIL"
                            if not trend_ok
                            else f"SIGNAL_GATE_FAIL_{self._last_signal_gate_reason}",
                        )
                else:
                    self._record_entry_block(now, "CONFIRM_NOT_MET")
                self._pending = None
            return

        # Path B: pullback-failure entry (supplement, not replacement for Path A).
        # Logic mirrors offline A+B replay assumptions:
        # - LONG: recent downside extension from VWAP, then reclaim above prev high with positive 1m momentum
        # - SHORT: recent upside extension from VWAP, then lose prev low with negative 1m momentum
        path_b_enabled = bool(extras.get("path_b_enabled", True))
        path_b_start = _parse_hms(extras.get("path_b_start", "10:00:00"), time(10, 0))
        path_b_dist_thr = float(extras.get("path_b_dist_atr_threshold", 0.15))
        if path_b_enabled and now.time() >= path_b_start and len(self._bars) >= 6:
            hist = list(self._bars)[-6:-1]  # last 5 completed bars before current bar
            dist_vals = []
            for hb in hist:
                hb_close = float(hb.get("close") or 0.0)
                hb_vwap = float(hb.get("vwap") or 0.0)
                hb_atr_d = self._resolve_atr_d(hb)
                if hb_close > 0 and hb_vwap > 0 and hb_atr_d > 0:
                    dist_vals.append((hb_close - hb_vwap) / hb_atr_d)
            if dist_vals:
                dist_roll_min = float(min(dist_vals))
                dist_roll_max = float(max(dist_vals))
                mom1_atr = (close - float(prev.get("close") or close)) / max(1e-9, atr_1m)
                long_b = (dist_roll_min <= -path_b_dist_thr) and (close > prev_high) and (mom1_atr > 0.0)
                short_b = (dist_roll_max >= path_b_dist_thr) and (close < prev_low) and (mom1_atr < 0.0)
                if long_b:
                    trigger_id, decision_id, trace_id = self._make_audit_ids()
                    self.ctx.log.info(
                        "qqq_0dte_path_b_trigger",
                        ts=now.strftime("%Y-%m-%d %H:%M:%S"),
                        strategy_id=str(self.strategy_id),
                        direction="LONG",
                        dist_roll_min=float(dist_roll_min),
                        mom1_atr=float(mom1_atr),
                    )
                    self._emit_local_audit(
                        stage="entry_confirmed",
                        status="confirmed",
                        reason="PATH_B_TRIGGER",
                        symbol=self.underlying,
                        decision_id=decision_id,
                        trigger_id=trigger_id,
                        trace_id=trace_id,
                        payload={
                            "direction": "LONG",
                            "dist_roll_min": float(dist_roll_min),
                            "mom1_atr": float(mom1_atr),
                            "path": "B",
                        },
                    )
                    await self._submit_entry(
                        now,
                        side="CALL",
                        spot=close,
                        entry_bar=bar,
                        spread_max_pct=spread_max_pct,
                        wall_now=wall_now,
                        audit_ids=(trigger_id, decision_id, trace_id),
                    )
                    return
                if short_b:
                    trigger_id, decision_id, trace_id = self._make_audit_ids()
                    self.ctx.log.info(
                        "qqq_0dte_path_b_trigger",
                        ts=now.strftime("%Y-%m-%d %H:%M:%S"),
                        strategy_id=str(self.strategy_id),
                        direction="SHORT",
                        dist_roll_max=float(dist_roll_max),
                        mom1_atr=float(mom1_atr),
                    )
                    self._emit_local_audit(
                        stage="entry_confirmed",
                        status="confirmed",
                        reason="PATH_B_TRIGGER",
                        symbol=self.underlying,
                        decision_id=decision_id,
                        trigger_id=trigger_id,
                        trace_id=trace_id,
                        payload={
                            "direction": "SHORT",
                            "dist_roll_max": float(dist_roll_max),
                            "mom1_atr": float(mom1_atr),
                            "path": "B",
                        },
                    )
                    await self._submit_entry(
                        now,
                        side="PUT",
                        spot=close,
                        entry_bar=bar,
                        spread_max_pct=spread_max_pct,
                        wall_now=wall_now,
                        audit_ids=(trigger_id, decision_id, trace_id),
                    )
                    return

        # Path A: breakout detection
        if close > prev_high and close > vwap:
            self.ctx.log.info(
                "qqq_0dte_pending_breakout",
                ts=now.strftime("%Y-%m-%d %H:%M:%S"),
                strategy_id=str(self.strategy_id),
                direction="LONG",
                close=float(close),
                vwap=float(vwap),
                prev_high=float(prev_high),
            )
            self._pending = _PendingEntry(direction="LONG", level=prev_high, stage="await_confirm", created_at=now, bars_waited=0)
            return
        if close < prev_low and close < vwap:
            self.ctx.log.info(
                "qqq_0dte_pending_breakout",
                ts=now.strftime("%Y-%m-%d %H:%M:%S"),
                strategy_id=str(self.strategy_id),
                direction="SHORT",
                close=float(close),
                vwap=float(vwap),
                prev_low=float(prev_low),
            )
            self._pending = _PendingEntry(direction="SHORT", level=prev_low, stage="await_confirm", created_at=now, bars_waited=0)
            return
        self._record_entry_block(now, "NO_BREAKOUT")

    def _close_loc(self, bar: Dict[str, Any]) -> float:
        hi = float(bar.get("high") or 0.0)
        lo = float(bar.get("low") or 0.0)
        cl = float(bar.get("close") or 0.0)
        rng = max(1e-9, hi - lo)
        return (cl - lo) / rng

    def _quote_heartbeat_allows_entry(self, now: datetime) -> bool:
        extras = self.params.extras or {}
        enabled = bool(extras.get("quote_hb_gate_enabled", True))
        if not enabled:
            return True

        start = _parse_hms(extras.get("quote_hb_check_start", "09:45:00"), time(9, 45))
        end = _parse_hms(
            extras.get("quote_hb_check_end", extras.get("no_new_entry_after", "14:50:00")),
            time(14, 50),
        )
        if now.time() < start or now.time() >= end:
            return True

        warmup_min = max(0, int(extras.get("quote_hb_warmup_min", 5)))
        warmup_end = datetime.combine(now.date(), start, tzinfo=ET) + timedelta(minutes=warmup_min)
        if now < warmup_end:
            return True

        max_age_sec = max(10.0, float(extras.get("quote_hb_max_age_sec", 120.0)))
        fail_to_pause = max(1, int(extras.get("quote_hb_fail_streak_to_pause", 4)))
        resume_streak = max(1, int(extras.get("quote_hb_resume_fresh_streak", 2)))

        status = self._latest_quote_status(now)
        age_sec = None if status is None else float(status.get("age_sec") or 0.0)
        source = str((status or {}).get("source") or "unknown")
        fresh = (age_sec is not None) and (age_sec <= max_age_sec)

        if fresh:
            self._quote_hb_fail_streak = 0
            self._quote_hb_fresh_streak += 1
            if self._quote_hb_pause_new_entries and self._quote_hb_fresh_streak >= resume_streak:
                self._quote_hb_pause_new_entries = False
                self._log_quote_hb(
                    now,
                    "RESUME",
                    f"fresh_streak={self._quote_hb_fresh_streak}, age_sec={int(age_sec or 0)}",
                    quote_source=source,
                    quote_age_sec=age_sec,
                )
            return True

        self._quote_hb_fresh_streak = 0
        self._quote_hb_fail_streak += 1
        if (not self._quote_hb_pause_new_entries) and self._quote_hb_fail_streak >= fail_to_pause:
            self._quote_hb_pause_new_entries = True
            self._log_quote_hb(
                now,
                "PAUSE",
                f"fail_streak={self._quote_hb_fail_streak}, age_sec={None if age_sec is None else int(age_sec)}",
                quote_source=source,
                quote_age_sec=age_sec,
            )
        return not self._quote_hb_pause_new_entries

    def _latest_quote_status(self, now: datetime) -> Optional[Dict[str, Any]]:
        try:
            return latest_quote_status(
                underlying=self.underlying,
                now=now,
                day_start=datetime.combine(now.date(), time(0, 0), tzinfo=ET),
                day_end=datetime.combine(now.date() + timedelta(days=1), time(0, 0), tzinfo=ET),
                dte_min=0,
                dte_max=0,
                expiry=now.strftime("%Y-%m-%d"),
            )
        except Exception:
            return None

    def _latest_quote_age_sec(self, now: datetime) -> Optional[float]:
        status = self._latest_quote_status(now)
        if not status:
            return None
        return float(status.get("age_sec") or 0.0)

    def _log_quote_hb(
        self,
        now: datetime,
        state: str,
        reason: str,
        *,
        quote_source: Optional[str] = None,
        quote_age_sec: Optional[float] = None,
    ) -> None:
        minute_key = now.strftime("%Y-%m-%d %H:%M")
        log_key = (minute_key, state)
        if log_key == self._quote_hb_last_log_key:
            return
        self._quote_hb_last_log_key = log_key
        self.ctx.log.warning(
            "qqq_0dte_quote_heartbeat_state",
            ts=now.strftime("%Y-%m-%d %H:%M:%S"),
            state=state,
            reason=reason,
            pause_new_entries=bool(self._quote_hb_pause_new_entries),
            fail_streak=int(self._quote_hb_fail_streak),
            fresh_streak=int(self._quote_hb_fresh_streak),
            underlying=self.underlying,
            quote_source=str(quote_source or "unknown"),
            quote_age_sec=None if quote_age_sec is None else float(quote_age_sec),
        )
        self._emit_local_audit(
            stage="quote_heartbeat_state",
            status=str(state).lower(),
            reason=str(reason),
            symbol=self.underlying,
            payload={
                "pause_new_entries": bool(self._quote_hb_pause_new_entries),
                "fail_streak": int(self._quote_hb_fail_streak),
                "fresh_streak": int(self._quote_hb_fresh_streak),
                "quote_source": str(quote_source or "unknown"),
                "quote_age_sec": None if quote_age_sec is None else float(quote_age_sec),
            },
        )

    def _record_entry_block(self, now: datetime, reason: str) -> None:
        minute_key = now.strftime("%Y-%m-%d %H:%M")
        if self._entry_block_minute_key != minute_key:
            self._entry_block_minute_key = minute_key
            self._entry_block_minute_counts = {}
        k = str(reason or "UNKNOWN").upper()
        first_seen_this_minute = int(self._entry_block_minute_counts.get(k, 0)) == 0
        self._entry_block_minute_counts[k] = int(self._entry_block_minute_counts.get(k, 0)) + 1
        self._entry_block_day_counts[k] = int(self._entry_block_day_counts.get(k, 0)) + 1
        if first_seen_this_minute:
            quote_status = self._latest_quote_status(now)
            quote_source = str((quote_status or {}).get("source") or "unknown")
            quote_age_sec = None if quote_status is None else float(quote_status.get("age_sec") or 0.0)
            self.ctx.log.info(
                "qqq_0dte_entry_block",
                ts=now.strftime("%Y-%m-%d %H:%M:%S"),
                minute=minute_key,
                reason=k,
                minute_count=int(self._entry_block_minute_counts.get(k, 0)),
                day_count=int(self._entry_block_day_counts.get(k, 0)),
                strategy_id=str(self.strategy_id),
                underlying=self.underlying,
                quote_source=quote_source,
                quote_age_sec=quote_age_sec,
            )
            self._emit_local_audit(
                stage="gate_blocked",
                status="blocked",
                reason=k,
                symbol=self.underlying,
                payload={
                    "minute": minute_key,
                    "minute_count": int(self._entry_block_minute_counts.get(k, 0)),
                    "day_count": int(self._entry_block_day_counts.get(k, 0)),
                    "quote_source": quote_source,
                    "quote_age_sec": quote_age_sec,
                },
            )

    def _flush_entry_block_logs(self, now: datetime) -> None:
        if not self._entry_block_minute_key or not self._entry_block_minute_counts:
            return
        current_key = now.strftime("%Y-%m-%d %H:%M")
        if current_key == self._entry_block_minute_key:
            return
        self.ctx.log.info(
            "qqq_0dte_entry_block_summary",
            minute=self._entry_block_minute_key,
            counts=dict(self._entry_block_minute_counts),
            day_totals=dict(self._entry_block_day_counts),
            strategy_id=str(self.strategy_id),
            underlying=self.underlying,
        )
        self._entry_block_minute_key = None
        self._entry_block_minute_counts = {}

    def _log_minute_heartbeat(self, now: datetime, bar: Dict[str, Any]) -> None:
        now_min = _minute_floor(now)
        if self._last_minute_heartbeat_log_ts == now_min:
            return
        self._last_minute_heartbeat_log_ts = now_min
        extras = self.params.extras or {}
        quote_status = self._latest_quote_status(now)
        quote_source = str((quote_status or {}).get("source") or "unknown")
        quote_age_sec = None if quote_status is None else float(quote_status.get("age_sec") or 0.0)
        entry_start = _parse_hms(extras.get("ab_time_start", "09:40:00"), time(9, 40))
        entry_end = _parse_hms(
            extras.get("no_new_entry_after", extras.get("ab_time_end", "15:00:00")),
            time(15, 0),
        )
        in_entry_window = entry_start <= now.time() < entry_end
        quote_max_age_sec = max(10.0, float(extras.get("quote_hb_max_age_sec", 120.0)))
        min_warmup_bars = max(1, int(extras.get("tradability_min_warmup_bars", extras.get("min_warmup_bars", 210))))
        blockers: List[str] = []
        if self._quote_health_block_day:
            blockers.append("quote_health_block_day")
        data_blockers: List[str] = []
        warmup_ready = len(self._bars) >= min_warmup_bars
        quote_fresh = quote_status is not None and quote_age_sec is not None and quote_age_sec <= quote_max_age_sec
        if self._quote_health_block_day:
            data_blockers.append("quote_health_block_day")
        if not warmup_ready:
            data_blockers.append("insufficient_warmup")
        if quote_status is None:
            data_blockers.append("quote_missing")
        elif not quote_fresh:
            data_blockers.append("quote_stale")
        if in_entry_window and not warmup_ready:
            blockers.append("insufficient_warmup")
        if in_entry_window and quote_status is None:
            blockers.append("quote_missing")
        elif in_entry_window and quote_age_sec is not None and quote_age_sec > quote_max_age_sec:
            blockers.append("quote_stale")
        if in_entry_window and self._quote_hb_pause_new_entries:
            blockers.append("quote_heartbeat_paused")
            data_blockers.append("quote_heartbeat_paused")
        idle_ok = not bool(in_entry_window)
        data_health_ok = not bool(data_blockers)
        tradable_for_entry = bool(in_entry_window and not blockers and data_health_ok)
        if tradable_for_entry:
            readiness_state = "entry_ready"
        elif idle_ok:
            readiness_state = "idle_not_in_entry_window"
        else:
            readiness_state = "entry_blocked"
        heartbeat_details = {
            "minute": now_min.strftime("%Y-%m-%d %H:%M"),
            "bars": int(len(self._bars)),
            "min_warmup_bars": int(min_warmup_bars),
            "warmup_ready": bool(warmup_ready),
            "has_pending": bool(self._pending is not None),
            "has_position": bool(self._pos is not None),
            "trades_filled_today": int(self._trades_filled_today),
            "quote_health_block_day": bool(self._quote_health_block_day),
            "quote_heartbeat_paused": bool(self._quote_hb_pause_new_entries),
            "quote_source": quote_source,
            "quote_age_sec": quote_age_sec,
            "quote_max_age_sec": float(quote_max_age_sec),
            "quote_fresh": bool(quote_fresh),
            "in_entry_window": bool(in_entry_window),
            "idle_ok": bool(idle_ok),
            "tradable_for_entry": bool(tradable_for_entry),
            "data_health_ok": bool(data_health_ok),
            "data_blockers": list(dict.fromkeys(data_blockers)),
            "readiness_state": readiness_state,
            "close": float(bar.get("close") or 0.0),
            "vwap": float(bar.get("vwap") or 0.0),
        }
        self.ctx.log.info(
            "qqq_0dte_minute_heartbeat",
            ts=now_min.strftime("%Y-%m-%d %H:%M:%S"),
            strategy_id=str(self.strategy_id),
            underlying=self.underlying,
            bars=int(len(self._bars)),
            has_pending=bool(self._pending is not None),
            has_position=bool(self._pos is not None),
            trades_filled_today=int(self._trades_filled_today),
            quote_health_block_day=bool(self._quote_health_block_day),
            close=float(bar.get("close") or 0.0),
            vwap=float(bar.get("vwap") or 0.0),
            quote_source=quote_source,
            quote_age_sec=quote_age_sec,
            tradable=tradable_for_entry,
            blockers=list(blockers),
        )
        self._emit_local_audit(
            stage="minute_heartbeat",
            status="alive",
            reason="HEARTBEAT",
            symbol=self.underlying,
            payload={**heartbeat_details, "tradable": tradable_for_entry, "blockers": list(blockers)},
        )
        heartbeat_blockers = list(blockers)
        if not tradable_for_entry and not heartbeat_blockers:
            heartbeat_blockers.append(str(readiness_state or "not_entry_ready"))
        write_strategy_tradability_heartbeat(
            strategy_id=str(self.strategy_id),
            tradable=tradable_for_entry,
            blockers=heartbeat_blockers,
            source="option_minute_heartbeat",
            details={
                **heartbeat_details,
                "tradable": tradable_for_entry,
                "symbol": str(self.underlying),
                "stage": "minute_heartbeat",
                "reason": "HEARTBEAT" if tradable_for_entry else ",".join(heartbeat_blockers),
                "trace_id": f"{self.strategy_id}:{now_min.strftime('%Y%m%d%H%M')}:heartbeat",
            },
        )

    def _log_quote_health_recovery_check(
        self,
        now: datetime,
        *,
        state: str,
        window_min: int,
        covered: int,
        nbbo: int,
        expected: int,
        coverage: float,
        nbbo_coverage: float,
        last_age_sec: Optional[float],
        reason: str,
    ) -> None:
        minute_key = now.strftime("%Y-%m-%d %H:%M")
        log_key = (minute_key, state)
        if self._quote_health_recovery_last_log_key == log_key:
            return
        self._quote_health_recovery_last_log_key = log_key
        self.ctx.log.info(
            "qqq_0dte_quote_data_health_recovery_check",
            ts=now.strftime("%Y-%m-%d %H:%M:%S"),
            strategy_id=str(self.strategy_id),
            underlying=self.underlying,
            state=str(state),
            reason=str(reason),
            window_min=int(window_min),
            covered_minutes=int(covered),
            nbbo_minutes=int(nbbo),
            expected_minutes=int(expected),
            coverage_pct=round(coverage * 100.0, 2),
            nbbo_coverage_pct=round(nbbo_coverage * 100.0, 2),
            last_quote_age_sec=None if last_age_sec is None else float(last_age_sec),
            quote_source=str((self._latest_quote_status(now) or {}).get("source") or "unknown"),
        )

    def _query_quote_minute_coverage(self, window_start: datetime, window_end: datetime) -> Dict[str, int]:
        return quote_minute_coverage(
            underlying=self.underlying,
            window_start=window_start,
            window_end=window_end,
            dte_min=0,
            dte_max=0,
            expiry=window_start.strftime("%Y-%m-%d"),
        )

    def _resolve_trader_log_path(self) -> Optional[Path]:
        candidates = [
            Path("./logs/trader.log"),
            Path("/workspace/grafana-stack/logs/trader.log"),
        ]
        for candidate in candidates:
            try:
                if not candidate.exists():
                    continue
                if candidate.stat().st_size <= 512:
                    content = candidate.read_text(encoding="utf-8", errors="ignore").strip()
                    if content.startswith("/") and content.endswith(".log"):
                        pointed = Path(content)
                        if pointed.exists():
                            return pointed
                return candidate
            except Exception:
                continue
        return None

    def _iter_recent_log_lines(self, log_path: Path, max_bytes: int) -> Deque[str]:
        lines: Deque[str] = deque()
        try:
            size = int(log_path.stat().st_size)
            read_bytes = max(1024, min(max_bytes, size))
            with log_path.open("rb") as fh:
                if size > read_bytes:
                    fh.seek(size - read_bytes)
                    _ = fh.readline()  # drop partial line at chunk boundary
                blob = fh.read().decode("utf-8", errors="ignore")
            for line in blob.splitlines():
                lines.append(line)
        except Exception:
            return deque()
        return lines

    def _restore_adopted_position_state(self, now: datetime, opt_symbol: str, qty: float) -> Dict[str, Any]:
        extras = self.params.extras or {}
        contracts = max(1, int(extras.get("contracts", extras.get("contracts_per_trade", 3)) or 3))
        restored: Dict[str, Any] = {
            "trace_id": "adopted",
            "tp1_done": bool(qty < contracts),
            "entry_time": now,
            "source": "heuristic_qty",
        }

        log_path = self._resolve_trader_log_path()
        if log_path is None:
            return restored

        try:
            max_scan_bytes = int(extras.get("quote_health_restore_log_max_bytes", 2_000_000))
            candidates: List[Dict[str, Any]] = []
            for line in self._iter_recent_log_lines(log_path, max_scan_bytes):
                if "qqq_0dte_position_diag" not in line:
                    continue
                try:
                    payload = json.loads(line)
                except Exception:
                    continue
                if payload.get("event") != "qqq_0dte_position_diag":
                    continue
                if str(payload.get("strategy_id") or "") != str(self.strategy_id):
                    continue
                if str(payload.get("symbol") or "") != str(opt_symbol):
                    continue
                ts_str = str(payload.get("timestamp") or "")
                if not ts_str.startswith(now.strftime("%Y-%m-%d")):
                    continue
                candidates.append(payload)
            if not candidates:
                return restored
            candidates.sort(key=lambda x: str(x.get("timestamp") or ""))
            latest = candidates[-1]
            trace_id = str(latest.get("trace_id") or "").strip()
            held_min = float(latest.get("held_min", 0.0) or 0.0)
            entry_time = now - timedelta(minutes=max(0.0, held_min))
            restored.update(
                {
                    "trace_id": trace_id or "adopted",
                    "tp1_done": bool(latest.get("tp1_done", restored["tp1_done"])),
                    "entry_time": entry_time,
                    "source": "position_diag_log",
                }
            )
        except Exception:
            return restored
        return restored

    def _restore_quote_data_health_gate(self, now: datetime, *, threshold: float, window_end: time) -> bool:
        day = now.strftime("%Y-%m-%d")
        threshold_pct = float(threshold) * 100.0
        state = self.state.get("quote_health_gate")
        if isinstance(state, dict) and state.get("day") == day:
            saved_threshold_pct = state.get("threshold_pct")
            if saved_threshold_pct is not None:
                try:
                    if abs(float(saved_threshold_pct) - threshold_pct) > 0.01:
                        self.ctx.log.warning(
                            "qqq_0dte_quote_data_health_gate_restore_threshold_mismatch",
                            ts=now.strftime("%Y-%m-%d %H:%M:%S"),
                            restored_from="memory_state",
                            saved_threshold_pct=float(saved_threshold_pct),
                            current_threshold_pct=round(threshold_pct, 2),
                            strategy_id=str(self.strategy_id),
                            underlying=self.underlying,
                        )
                        return False
                except Exception:
                    return False
            self._quote_health_checked = True
            self._quote_health_cov_pct = float(state.get("coverage", 0.0) or 0.0)
            self._quote_health_block_day = bool(state.get("block_day", False))
            self.ctx.log.info(
                "qqq_0dte_quote_data_health_gate_restored",
                ts=now.strftime("%Y-%m-%d %H:%M:%S"),
                restored_from="memory_state",
                coverage_pct=round(self._quote_health_cov_pct * 100.0, 2),
                block_day=bool(self._quote_health_block_day),
                strategy_id=str(self.strategy_id),
                underlying=self.underlying,
            )
            return True

        log_path = self._resolve_trader_log_path()
        if log_path is None:
            return False

        earliest: Optional[Dict[str, Any]] = None
        latest_recovery: Optional[Dict[str, Any]] = None
        try:
            extras = self.params.extras or {}
            max_scan_bytes = int(extras.get("quote_health_restore_log_max_bytes", 2_000_000))
            rows = []
            for line in self._iter_recent_log_lines(log_path, max_scan_bytes):
                if "qqq_0dte_quote_data_health_gate" not in line and "qqq_0dte_quote_data_health_gate_recovered" not in line:
                    continue
                try:
                    payload = json.loads(line)
                except Exception:
                    continue
                if payload.get("event") not in {"qqq_0dte_quote_data_health_gate", "qqq_0dte_quote_data_health_gate_recovered"}:
                    continue
                if str(payload.get("strategy_id") or "") != str(self.strategy_id):
                    continue
                if str(payload.get("underlying") or "").upper() != self.underlying:
                    continue
                ts_str = str(payload.get("timestamp") or "")
                if not ts_str.startswith(day):
                    continue
                if payload.get("event") == "qqq_0dte_quote_data_health_gate":
                    rows.append(payload)
                else:
                    if latest_recovery is None or str(payload.get("timestamp") or "") > str(latest_recovery.get("timestamp") or ""):
                        latest_recovery = payload
            if rows:
                rows.sort(key=lambda x: str(x.get("timestamp") or ""))
                earliest = rows[0]
        except Exception:
            return False

        if not earliest:
            return False

        source_threshold_pct = earliest.get("threshold_pct")
        if source_threshold_pct is not None:
            try:
                if abs(float(source_threshold_pct) - threshold_pct) > 0.01:
                    self.ctx.log.warning(
                        "qqq_0dte_quote_data_health_gate_restore_threshold_mismatch",
                        ts=now.strftime("%Y-%m-%d %H:%M:%S"),
                        restored_from="trader_log",
                        source_log_path=str(log_path),
                        source_threshold_pct=float(source_threshold_pct),
                        current_threshold_pct=round(threshold_pct, 2),
                        strategy_id=str(self.strategy_id),
                        underlying=self.underlying,
                    )
                    return False
            except Exception:
                return False

        source_ts = str(earliest.get("ts") or "")
        try:
            source_dt = datetime.fromisoformat(source_ts.replace(" ", "T"))
            if source_dt.tzinfo is None:
                source_dt = source_dt.replace(tzinfo=ET)
            latest_expected = datetime.combine(now.date(), window_end, tzinfo=ET) + timedelta(minutes=5)
            if source_dt > latest_expected:
                self.ctx.log.warning(
                    "qqq_0dte_quote_data_health_gate_restore_uncertain",
                    ts=now.strftime("%Y-%m-%d %H:%M:%S"),
                    restored_from="trader_log",
                    source_log_path=str(log_path),
                    source_ts=source_ts,
                    latest_expected_ts=latest_expected.strftime("%Y-%m-%d %H:%M:%S"),
                    strategy_id=str(self.strategy_id),
                    underlying=self.underlying,
                )
                return False
        except Exception:
            return False

        self._quote_health_checked = True
        self._quote_health_cov_pct = float(earliest.get("coverage_pct", 0.0) or 0.0) / 100.0
        self._quote_health_block_day = bool(earliest.get("block_day", False))
        restored_from = "trader_log"
        source_timestamp = str(earliest.get("timestamp") or "")
        if latest_recovery is not None:
            recovery_threshold_pct = latest_recovery.get("threshold_pct")
            if recovery_threshold_pct is not None:
                try:
                    if abs(float(recovery_threshold_pct) - threshold_pct) > 0.01:
                        latest_recovery = None
                    else:
                        self._quote_health_cov_pct = float(latest_recovery.get("coverage_pct", 0.0) or 0.0) / 100.0
                        self._quote_health_block_day = False
                        restored_from = "trader_log_recovery"
                        source_timestamp = str(latest_recovery.get("timestamp") or "")
                except Exception:
                    latest_recovery = None
        self.state["quote_health_gate"] = {
            "day": day,
            "coverage": self._quote_health_cov_pct,
            "block_day": self._quote_health_block_day,
            "threshold_pct": round(threshold_pct, 2),
            "restored_from": restored_from,
        }
        self.ctx.log.info(
            "qqq_0dte_quote_data_health_gate_restored",
            ts=now.strftime("%Y-%m-%d %H:%M:%S"),
            restored_from=restored_from,
            source_log_path=str(log_path),
            source_timestamp=source_timestamp,
            coverage_pct=round(self._quote_health_cov_pct * 100.0, 2),
            block_day=bool(self._quote_health_block_day),
            strategy_id=str(self.strategy_id),
            underlying=self.underlying,
        )
        return True

    def _update_quote_data_health_gate(self, now: datetime) -> None:
        """
        Day-level data health gate:
        After 10:00 ET, check 09:45-10:00 0DTE quote minute coverage.
        If below threshold, block new entries for the rest of day.
        """
        if self._quote_health_checked:
            return
        extras = self.params.extras or {}
        start = _parse_hms(extras.get("quote_health_window_start", "09:45:00"), time(9, 45))
        end = _parse_hms(extras.get("quote_health_window_end", "10:00:00"), time(10, 0))
        if now.time() < end:
            return
        threshold = float(extras.get("quote_health_min_coverage", 0.70))
        if self._restore_quote_data_health_gate(now, threshold=threshold, window_end=end):
            return

        dt_start = datetime.combine(now.date(), start, tzinfo=ET)
        dt_end = datetime.combine(now.date(), end, tzinfo=ET)
        expected = max(1, int((dt_end - dt_start).total_seconds() // 60))
        counts = self._query_quote_minute_coverage(dt_start, dt_end)
        covered = int(counts.get("covered_minutes", 0))
        coverage = float(covered) / float(expected)
        self._quote_health_checked = True
        self._quote_health_cov_pct = coverage
        self._quote_health_block_day = bool(coverage < threshold)
        self.state["quote_health_gate"] = {
            "day": now.strftime("%Y-%m-%d"),
            "coverage": coverage,
            "block_day": self._quote_health_block_day,
            "threshold_pct": round(threshold * 100.0, 2),
            "restored_from": "query",
        }
        self.ctx.log.warning(
            "qqq_0dte_quote_data_health_gate",
            ts=now.strftime("%Y-%m-%d %H:%M:%S"),
            covered_minutes=covered,
            expected_minutes=expected,
            coverage_pct=round(coverage * 100.0, 2),
            threshold_pct=round(threshold * 100.0, 2),
            block_day=bool(self._quote_health_block_day),
            strategy_id=str(self.strategy_id),
            underlying=self.underlying,
        )
        self._emit_local_audit(
            stage="quote_health_gate",
            severity="warning" if self._quote_health_block_day else "info",
            status="blocked" if self._quote_health_block_day else "ok",
            reason="DATA_HEALTH_BLOCK_DAY" if self._quote_health_block_day else "DATA_HEALTH_OK",
            symbol=self.underlying,
            payload={
                "covered_minutes": int(covered),
                "expected_minutes": int(expected),
                "coverage_pct": round(coverage * 100.0, 2),
                "threshold_pct": round(threshold * 100.0, 2),
            },
        )

    def _maybe_release_quote_data_health_gate(self, now: datetime) -> None:
        if not self._quote_health_block_day:
            return
        extras = self.params.extras or {}
        if not bool(extras.get("quote_health_recovery_enabled", True)):
            return

        no_new_after = _parse_hms(extras.get("no_new_entry_after", "14:50:00"), time(14, 50))
        if now.time() >= no_new_after:
            return

        recovery_window_min = max(3, int(extras.get("quote_health_recovery_window_min", 10)))
        recovery_min_coverage = float(extras.get("quote_health_recovery_min_coverage", 0.90))
        recovery_min_nbbo_coverage = float(
            extras.get("quote_health_recovery_min_nbbo_coverage", recovery_min_coverage)
        )
        recovery_max_age_sec = max(10.0, float(extras.get("quote_health_recovery_max_last_age_sec", 120.0)))

        # Use only fully completed minutes. Strategy evaluation often runs before the
        # current minute's option quotes have been ingested, so including the in-flight
        # minute would systematically undercount coverage by one.
        window_end = _minute_floor(now)
        window_start = window_end - timedelta(minutes=recovery_window_min)
        expected = max(1, recovery_window_min)
        counts = self._query_quote_minute_coverage(window_start, window_end)
        covered = int(counts.get("covered_minutes", 0))
        nbbo = int(counts.get("nbbo_minutes", 0))
        coverage = float(covered) / float(expected)
        nbbo_coverage = float(nbbo) / float(expected)
        last_age_sec = self._latest_quote_age_sec(now)

        if coverage < recovery_min_coverage:
            self._log_quote_health_recovery_check(
                now,
                state="BLOCKED",
                window_min=recovery_window_min,
                covered=covered,
                nbbo=nbbo,
                expected=expected,
                coverage=coverage,
                nbbo_coverage=nbbo_coverage,
                last_age_sec=last_age_sec,
                reason="COVERAGE_LOW",
            )
            return
        if nbbo_coverage < recovery_min_nbbo_coverage:
            self._log_quote_health_recovery_check(
                now,
                state="BLOCKED",
                window_min=recovery_window_min,
                covered=covered,
                nbbo=nbbo,
                expected=expected,
                coverage=coverage,
                nbbo_coverage=nbbo_coverage,
                last_age_sec=last_age_sec,
                reason="NBBO_COVERAGE_LOW",
            )
            return
        if last_age_sec is None:
            self._log_quote_health_recovery_check(
                now,
                state="BLOCKED",
                window_min=recovery_window_min,
                covered=covered,
                nbbo=nbbo,
                expected=expected,
                coverage=coverage,
                nbbo_coverage=nbbo_coverage,
                last_age_sec=last_age_sec,
                reason="LAST_QUOTE_MISSING",
            )
            return
        if float(last_age_sec) > recovery_max_age_sec:
            self._log_quote_health_recovery_check(
                now,
                state="BLOCKED",
                window_min=recovery_window_min,
                covered=covered,
                nbbo=nbbo,
                expected=expected,
                coverage=coverage,
                nbbo_coverage=nbbo_coverage,
                last_age_sec=last_age_sec,
                reason="LAST_QUOTE_STALE",
            )
            return

        self._quote_health_block_day = False
        self._quote_health_cov_pct = max(self._quote_health_cov_pct, coverage)
        self.state["quote_health_gate"] = {
            "day": now.strftime("%Y-%m-%d"),
            "coverage": self._quote_health_cov_pct,
            "block_day": False,
            "threshold_pct": round(float(extras.get("quote_health_min_coverage", 0.70)) * 100.0, 2),
            "restored_from": "recovery_gate",
            "recovered_at": now.isoformat(),
        }
        self._log_quote_health_recovery_check(
            now,
            state="RECOVERED",
            window_min=recovery_window_min,
            covered=covered,
            nbbo=nbbo,
            expected=expected,
            coverage=coverage,
            nbbo_coverage=nbbo_coverage,
            last_age_sec=last_age_sec,
            reason="RECOVERY_GATE_PASS",
        )
        self.ctx.log.warning(
            "qqq_0dte_quote_data_health_gate_recovered",
            ts=now.strftime("%Y-%m-%d %H:%M:%S"),
            window_min=int(recovery_window_min),
            covered_minutes=int(covered),
            nbbo_minutes=int(nbbo),
            coverage_pct=round(coverage * 100.0, 2),
            nbbo_coverage_pct=round(nbbo_coverage * 100.0, 2),
            last_quote_age_sec=float(last_age_sec),
            threshold_pct=round(float(extras.get("quote_health_min_coverage", 0.70)) * 100.0, 2),
            strategy_id=str(self.strategy_id),
            underlying=self.underlying,
        )
        self._emit_local_audit(
            stage="quote_health_recovered",
            status="recovered",
            reason="RECOVERY_GATE_PASS",
            symbol=self.underlying,
            payload={
                "window_min": int(recovery_window_min),
                "covered_minutes": int(covered),
                "nbbo_minutes": int(nbbo),
                "coverage_pct": round(coverage * 100.0, 2),
                "nbbo_coverage_pct": round(nbbo_coverage * 100.0, 2),
                "last_quote_age_sec": float(last_age_sec),
            },
        )

    def _enter_with_gates(self, now: datetime, bar: Dict[str, Any], *, direction: str, breakout_level: Optional[float]) -> bool:
        """
        Keep entry signal layer compatible with the existing QQQ logic:
        - momentum deadzone (ATR scaled)
        - anti-chase (extension cap)
        - atr_rank_skip (optional)
        - early_override (pre-09:45)
        - delayed_confirm (09:45..end)
        """
        extras = self.params.extras or {}
        self._last_signal_gate_reason = "UNKNOWN"
        if len(self._bars) < 6:
            self._last_signal_gate_reason = "INSUFFICIENT_BARS"
            return False

        close = float(bar.get("close") or 0.0)
        atr_1m = float(bar.get("atr_1m") or 0.0)

        # 3m signed momentum (same definition as backtest strategy: close - close[-4])
        v3m_signed = close - float(self._bars[-4].get("close") or close)
        deadzone_atr_mult = float(extras.get("momentum_deadzone_atr_mult", 0.25))
        deadzone_px = max(float(extras.get("momentum_deadzone", 0.05)), deadzone_atr_mult * atr_1m)
        if direction == "LONG" and v3m_signed <= deadzone_px:
            self._last_signal_gate_reason = "MOMENTUM_DEADZONE"
            return False
        if direction == "SHORT" and v3m_signed >= -deadzone_px:
            self._last_signal_gate_reason = "MOMENTUM_DEADZONE"
            return False

        # Anti-chase: avoid late extension entries (ATR_D normalized)
        if bool(extras.get("anti_chase_enabled", False)) and breakout_level is not None:
            atr_d = self._resolve_atr_d(bar)
            if atr_d > 0:
                max_ext_atr = float(extras.get("anti_chase_max_extension_atr", 0.12))
                ext_signed = (
                    (close - float(breakout_level)) / atr_d
                    if direction == "LONG"
                    else (float(breakout_level) - close) / atr_d
                )
                if ext_signed > max_ext_atr:
                    self._last_signal_gate_reason = "ANTI_CHASE"
                    return False

        # ATR-rank skip (optional, if upstream computed)
        atr_rank = extras.get("atr_rank_today")
        atr_rank_skip_max = float(extras.get("atr_rank_skip_max", 0.15))
        if atr_rank is not None:
            try:
                if float(atr_rank) <= atr_rank_skip_max:
                    self._last_signal_gate_reason = "ATR_RANK_SKIP"
                    return False
            except Exception:
                pass

        # Early override: before 09:45 only enter if early strength confirmed
        # Default-on for backward-compatible signal behavior; config can disable explicitly.
        early_override_enabled = bool(extras.get("early_override_enabled", True))
        if early_override_enabled:
            early_override_window_start = _parse_hms(extras.get("early_override_window_start", "09:39:00"), time(9, 39))
            early_override_window_end = _parse_hms(extras.get("early_override_window_end", "09:45:00"), time(9, 45))
            if now.time() < time(9, 45):
                in_window = early_override_window_start <= now.time() <= early_override_window_end
                if not (in_window and self._early_override_ready(now)):
                    self._last_signal_gate_reason = "EARLY_OVERRIDE"
                    return False

        # Delayed confirm gate: between 09:45..delayed_confirm_end require 5m move
        delayed_confirm_end = _parse_hms(extras.get("delayed_confirm_end", "10:00:00"), time(10, 0))
        if time(9, 45) <= now.time() <= delayed_confirm_end:
            atr_d = self._resolve_atr_d(bar)
            if atr_d <= 0:
                self._last_signal_gate_reason = "DELAYED_CONFIRM_NO_ATR_D"
                return False
            close_5m_ago = float(self._bars[-6].get("close") or close)
            move5m = abs(close - close_5m_ago) / atr_d
            if move5m < float(extras.get("delayed_confirm_move5m_min", 0.155)):
                self._last_signal_gate_reason = "DELAYED_CONFIRM_MOVE5M"
                return False

        self._last_signal_gate_reason = "PASS"
        return True

    def _early_override_ready(self, now: datetime) -> bool:
        extras = self.params.extras or {}
        if now.time() < time(9, 39):
            return False
        if not self._bars:
            return False

        atr_d = self._resolve_atr_d(self._bars[-1])
        if atr_d <= 0:
            return False

        open_0930 = self._price_at_time(time(9, 30))
        px_0938 = self._price_at_time(time(9, 38))
        if open_0930 is None or px_0938 is None:
            return False

        early_move_mult = float(extras.get("early_override_move_mult", 0.16))
        early_vol_min = float(extras.get("early_override_vol_min", 0.60))

        early_move = abs(float(px_0938) - float(open_0930))
        if early_move < early_move_mult * atr_d:
            return False

        side_sign = None
        vols = []
        vol_0939 = None
        for b in self._bars:
            bt = b["ts"].time()
            if time(9, 31) <= bt <= time(9, 39):
                c = float(b.get("close") or 0.0)
                v = float(b.get("vwap") or 0.0)
                if c <= 0 or v <= 0:
                    return False
                s = 1 if c > v else (-1 if c < v else 0)
                if s == 0:
                    return False
                if side_sign is None:
                    side_sign = s
                elif side_sign != s:
                    return False
                if bt == time(9, 39):
                    vol_0939 = float(b.get("volume") or 0.0)
                else:
                    vols.append(float(b.get("volume") or 0.0))

        if vol_0939 is None or len(vols) < 7:
            return False
        base_vol = sum(vols) / max(1, len(vols))
        if base_vol <= 0:
            return False
        if (float(vol_0939) / base_vol) < early_vol_min:
            return False

        return True

    def _price_at_time(self, target: time) -> Optional[float]:
        for b in self._bars:
            if b["ts"].time() == target:
                return float(b.get("close") or 0.0)
        return None

    def _is_otm_disallowed_tail_entry(self, now: datetime, *, side: str, spot: float, strike: float) -> bool:
        extras = self.params.extras or {}
        tail_gate_after = _parse_hms(
            extras.get("tail_atm_itm_only_after", "14:50:00"),
            time(14, 50),
        )
        if now.time() < tail_gate_after:
            return False
        if side == "PUT":
            return float(strike) < float(spot)
        return float(strike) > float(spot)

    async def _submit_entry(
        self,
        now: datetime,
        *,
        side: str,
        spot: float,
        entry_bar: Dict[str, Any],
        spread_max_pct: float,
        wall_now: Optional[datetime] = None,
        audit_ids: Optional[Tuple[str, str, str]] = None,
    ) -> None:
        extras = self.params.extras or {}
        contracts = int(extras.get("contracts", extras.get("contracts_per_trade", 3)))
        if contracts <= 0:
            return

        q, reject_reason = self._select_0dte_quote(
            now, side=side, spot=spot, spread_max_pct=spread_max_pct, now_for_quote_age=(wall_now or now)
        )
        if q is None:
            log_key = (now.strftime("%Y-%m-%d %H:%M"), side, str(reject_reason or "UNKNOWN"))
            if log_key != self._last_entry_reject_log_key:
                self._last_entry_reject_log_key = log_key
                self.ctx.log.warning(
                    "qqq_0dte_entry_rejected_quote_unavailable",
                    ts=now.strftime("%Y-%m-%d %H:%M:%S"),
                    side=side,
                    reason=str(reject_reason or "UNKNOWN"),
                    underlying=self.underlying,
                )
                self._emit_local_audit(
                    stage="entry_intent_rejected",
                    severity="warning",
                    status="rejected",
                    reason=str(reject_reason or "UNKNOWN"),
                    symbol=self.underlying,
                    payload={
                        "side": str(side),
                        "spot": float(spot),
                    },
                )
            return

        opt_symbol = q["opt_symbol"]
        strike = float(q.get("strike") or 0.0)
        if self._is_otm_disallowed_tail_entry(now, side=side, spot=spot, strike=strike):
            self.ctx.log.warning(
                "qqq_0dte_entry_rejected_tail_otm_disallowed",
                ts=now.strftime("%Y-%m-%d %H:%M:%S"),
                side=side,
                underlying=self.underlying,
                spot=float(spot),
                strike=float(strike),
                tail_after=str((_parse_hms((self.params.extras or {}).get("tail_atm_itm_only_after", "14:50:00"), time(14, 50))).strftime("%H:%M:%S")),
            )
            self._emit_local_audit(
                stage="entry_intent_rejected",
                severity="warning",
                status="rejected",
                reason="TAIL_OTM_DISALLOWED",
                symbol=str(opt_symbol),
                payload={
                    "side": str(side),
                    "spot": float(spot),
                    "strike": float(strike),
                },
            )
            return

        trigger_id, decision_id, trace_id = audit_ids or self._make_audit_ids()
        self._emit_local_audit(
            stage="contract_selected",
            status="selected",
            reason="QUOTE_OK",
            symbol=str(opt_symbol),
            decision_id=decision_id,
            trigger_id=trigger_id,
            trace_id=trace_id,
            payload={
                "side": str(side),
                "spot": float(spot),
                "strike": float(strike),
            },
        )
        self._emit_local_audit(
            stage="quote_snapshot",
            status="ready",
            reason="NBBO_OK",
            symbol=str(opt_symbol),
            decision_id=decision_id,
            trigger_id=trigger_id,
            trace_id=trace_id,
            payload={
                "side": str(side),
                "bid": float(q["bid"]),
                "ask": float(q["ask"]),
                "mid": float(q["mid"]),
                "last": float(q.get("last") or 0.0),
                "quote_age_sec": float(q.get("quote_age_sec") or 0.0),
                "quote_source": str(q.get("quote_source") or "unknown"),
            },
        )
        self.ctx.log.warning(
            "qqq_0dte_entry_submit",
            ts=now.strftime("%Y-%m-%d %H:%M:%S"),
            strategy_id=str(self.strategy_id),
            trace_id=trace_id,
            decision_id=decision_id,
            side=str(side),
            symbol=str(opt_symbol),
            contracts=int(contracts),
            spot=float(spot),
            mid=float(q["mid"]),
            bid=float(q["bid"]),
            ask=float(q["ask"]),
            quote_age_sec=float(q.get("quote_age_sec") or 0.0),
            quote_source=str(q.get("quote_source") or "unknown"),
        )
        self._emit_local_audit(
            stage="entry_intent_requested",
            severity="warning",
            status="requested",
            reason="ENTRY_SIGNAL_READY",
            symbol=str(opt_symbol),
            decision_id=decision_id,
            trigger_id=trigger_id,
            trace_id=trace_id,
            payload={
                "side": str(side),
                "contracts": int(contracts),
                "spot": float(spot),
                "mid": float(q["mid"]),
                "bid": float(q["bid"]),
                "ask": float(q["ask"]),
                "quote_age_sec": float(q.get("quote_age_sec") or 0.0),
                "quote_source": str(q.get("quote_source") or "unknown"),
            },
        )
        proceed, extra_entry_extras = self._prepare_entry_intent(
            now=now,
            side=side,
            spot=float(spot),
            entry_bar=entry_bar,
            quote=q,
            trigger_id=trigger_id,
            decision_id=decision_id,
            trace_id=trace_id,
        )
        if not proceed:
            return
        env = DecisionEnvelope(
            decision_id=decision_id,
            strategy_id=str(self.strategy_id),
            symbol=opt_symbol,
            ts_bar_end_us=int(now.timestamp() * 1_000_000),
            target_qty=float(contracts),
            freshness=Freshness(dlb_ms=30_000, ttl_ms=int(self.params.ttl_ms)),
            signal_score=float(extras.get("entry_signal_score", 3.0)),
            order_style=OrderStyle(kind="MKT"),
            extras={
                "instrument_type": "OPTION",
                "underlying": self.underlying,
                "bars_symbol": self.underlying,
                "risk_symbol": self.risk_symbol,
                "contracts_mult": 100,
                "force_order_style": True,
                "disable_oco": True,
                "entry_timeout_sec": float(extras.get("entry_timeout_sec", 60.0)),
                "ref_px": float(q["mid"]),
                "ref_px_source": "options_quotes_1m",
                "opt_bid_px": float(q["bid"]),
                "opt_ask_px": float(q["ask"]),
                "opt_mid_px": float(q["mid"]),
                "opt_last_px": float(q.get("last") or 0.0),
                "opt_quote_age_sec": float(q.get("quote_age_sec") or 0.0),
                "opt_quote_source": str(q.get("quote_source") or "unknown"),
                "opt_tick_size": 0.01,
                "opt_lot_size": 1.0,
                "order_ref": f"opt0dte:{self.strategy_id}:{decision_id}"[:45],
                "allocation_id": trace_id,
                "trace_id": trace_id,
                "trigger_id": trigger_id,
                "allow_same_option_symbol_cross_strategy": bool(extras.get("allow_same_option_symbol_cross_strategy", False)),
                "shared_cap_group_id": extras.get("shared_cap_group_id"),
                "shared_cap_group_limit_usd": extras.get("shared_cap_group_limit_usd"),
                "shared_cap_group_strategy_ids": extras.get("shared_cap_group_strategy_ids"),
                **dict(extra_entry_extras or {}),
            },
        )

        if self._skip_entry_intent_submit(
            now=now,
            side=side,
            env=env,
            quote=q,
            trigger_id=trigger_id,
            decision_id=decision_id,
            trace_id=trace_id,
        ):
            return

        try:
            await self.ctx.submit_intent(env)
            self._emit_local_audit(
                stage="entry_intent_submitted",
                status="submitted",
                reason="SUBMIT_INTENT_OK",
                symbol=str(opt_symbol),
                decision_id=decision_id,
                trigger_id=trigger_id,
                trace_id=trace_id,
                payload={
                    "side": str(side),
                    "contracts": int(contracts),
                    "order_ref": str(env.extras.get("order_ref") or ""),
                },
            )
        except Exception as exc:
            self._emit_local_audit(
                stage="entry_intent_submit_failed",
                severity="warning",
                status="failed",
                reason="SUBMIT_INTENT_EXCEPTION",
                symbol=str(opt_symbol),
                decision_id=decision_id,
                trigger_id=trigger_id,
                trace_id=trace_id,
                payload={
                    "side": str(side),
                    "contracts": int(contracts),
                    "error": str(exc),
                },
            )
            raise
        # Track pending/active intent; we confirm fills via ctx.position/open_orders.
        self._pos = _PositionState(
            opt_symbol=opt_symbol,
            side=side,
            entry_time=now,
            position_id=str((env.extras or {}).get("position_id") or ""),
            entry_decision_id=str(decision_id),
            entry_trigger_id=str(trigger_id),
            entry_snapshot_id=str((env.extras or {}).get("decision_snapshot_id") or ""),
            entry_underlying=float(spot),
            entry_close=float(spot),
            entry_vwap=float(entry_bar.get("vwap") or spot),
            entry_atr_1m=float(entry_bar.get("atr_1m") or 0.0),
            trace_id=trace_id,
            tp1_done=False,
            filled=False,
            last_exit_reason=None,
            early_fail_mode="hard",
            early_fail_decided=False,
            early_fail_triggered=False,
            entry_counter_vwap=(
                bool(float(spot) < float(entry_bar.get("vwap") or spot))
                if side == "CALL"
                else bool(float(spot) > float(entry_bar.get("vwap") or spot))
            ),
        )

    # -------------------- Quote selection --------------------
    def _select_0dte_quote(
        self,
        now: datetime,
        *,
        side: str,
        spot: float,
        spread_max_pct: float,
        now_for_quote_age: Optional[datetime] = None,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        extras = self.params.extras or {}
        expiry = now.strftime("%Y-%m-%d")
        expiry_ymd = now.strftime("%Y%m%d")
        right = "C" if side == "CALL" else "P"
        ts = _minute_floor(now)
        lookback_min = max(1, int(extras.get("entry_quote_lookback_min", 2)))
        quote_max_age_sec = max(1.0, float(extras.get("entry_quote_max_age_sec", 90.0)))
        ts_lb = ts - timedelta(minutes=lookback_min)
        allow_ch_fallback = bool(extras.get("entry_quote_allow_ch_fallback", False))
        rows = query_option_rows(
            underlying=self.underlying,
            right=right,
            dte_min=0,
            dte_max=0,
            expiry=expiry,
            ts_lb=ts_lb,
            ts_now=ts,
            limit=128,
            fallback_to_ch=allow_ch_fallback,
        )
        if not rows:
            return None, "NO_QUOTES"

        ref_now = now_for_quote_age or now

        def _quote_age_seconds(row: Dict[str, Any]) -> float:
            try:
                created_at = row.get("created_at")
                if isinstance(created_at, datetime):
                    return max(0.0, (ref_now - created_at.astimezone(ET)).total_seconds())
            except Exception:
                pass
            try:
                row_ts = row.get("ts")
                if isinstance(row_ts, datetime):
                    return max(0.0, (ref_now - row_ts.astimezone(ET)).total_seconds())
            except Exception:
                pass
            return 9_999.0

        invalid_price = 0
        stale = 0
        wide = 0
        allow_stale_entry_quotes = bool(extras.get("allow_stale_entry_quotes", False))
        valid_rows = []
        for r in rows:
            bid = float(r.get("bid") or 0.0)
            ask = float(r.get("ask") or 0.0)
            mid = float(r.get("mid") or 0.0)
            if bid <= 0 or ask <= 0 or mid <= 0:
                invalid_price += 1
                continue
            age_sec = _quote_age_seconds(r)
            if (not allow_stale_entry_quotes) and age_sec > quote_max_age_sec:
                stale += 1
                continue
            spread_pct = (ask - bid) / max(1e-9, mid)
            if spread_pct > float(spread_max_pct):
                wide += 1
                continue
            rr = dict(r)
            rr["quote_age_sec"] = float(age_sec)
            valid_rows.append(rr)

        if not valid_rows:
            if stale > 0:
                return None, "STALE_QUOTE"
            if wide > 0:
                return None, "WIDE_SPREAD"
            if invalid_price > 0:
                return None, "INVALID_PRICE"
            return None, "NO_VALID_QUOTE"

        best = min(valid_rows, key=lambda r: abs(float(r.get("strike") or spot) - float(spot)))
        bid = float(best.get("bid") or 0.0)
        ask = float(best.get("ask") or 0.0)
        mid = float(best.get("mid") or 0.0)

        strike = float(best.get("strike") or 0.0)
        opt_symbol = format_internal_opt_symbol(
            underlying=self.underlying,
            expiry_ymd=expiry_ymd,
            strike=strike,
            right=right,
        )

        quote_age_sec = float(best.get("quote_age_sec") or 0.0)

        return ({
            "opt_symbol": opt_symbol,
            "strike": strike,
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "last": float(best.get("last") or mid),
            "quote_age_sec": float(quote_age_sec),
            "quote_source": str(best.get("quote_source") or "unknown"),
        }, None)

    def _resolve_atr_d(self, bar: Dict[str, Any]) -> float:
        atr_d = float(bar.get("atr_d") or 0.0)
        if atr_d > 0:
            return atr_d
        extras = self.params.extras or {}
        atr_1m = float(bar.get("atr_1m") or 0.0)
        mult = float(extras.get("atr_d_mult_from_atr_1m", 40.0))
        return atr_1m * mult

    def _bars_after_entry(self, entry_time: datetime) -> List[Dict[str, Any]]:
        entry_minute = _minute_floor(entry_time)
        out: List[Dict[str, Any]] = []
        for row in self._bars:
            ts = row.get("ts")
            if isinstance(ts, datetime) and ts.astimezone(ET) > entry_minute:
                out.append(row)
        return out

    def _favorable_move_for_bar(self, side: str, entry_close: float, bar: Dict[str, Any]) -> float:
        if side == "CALL":
            return max(0.0, float(bar.get("high") or entry_close) - float(entry_close))
        return max(0.0, float(entry_close) - float(bar.get("low") or entry_close))

    def _giveback_ratio_for_bar(self, side: str, entry_close: float, bar: Dict[str, Any]) -> float:
        favorable_move = self._favorable_move_for_bar(side, entry_close, bar)
        if favorable_move <= 0:
            return 1.0
        close_px = float(bar.get("close") or entry_close)
        if side == "CALL":
            realized = max(0.0, close_px - float(entry_close))
        else:
            realized = max(0.0, float(entry_close) - close_px)
        giveback = max(0.0, favorable_move - realized)
        return float(giveback / max(1e-9, favorable_move))

    def _emit_early_fail_mode_log(self, now: datetime, first_bar: Dict[str, Any], atr_1m: float) -> None:
        if self._pos is None:
            return
        self.ctx.log.info(
            "entry_early_fail_mode",
            ts=now.strftime("%Y-%m-%d %H:%M:%S"),
            strategy_id=str(self.strategy_id),
            trace_id=str(self._pos.trace_id or ""),
            symbol=str(self._pos.opt_symbol),
            early_fail_mode=str(self._pos.early_fail_mode),
            entry_counter_vwap=bool(self._pos.entry_counter_vwap),
            entry_first1_favorable_move=float(self._pos.entry_first1_favorable_move),
            entry_first1_giveback_ratio=float(self._pos.entry_first1_giveback_ratio),
            first1_atr_1m=float(atr_1m),
            first1_ts=str(first_bar.get("ts")),
        )

    # -------------------- Position / exits --------------------
    async def _sync_and_manage_position(self, now: datetime, bar: Dict[str, Any]) -> bool:
        extras = self.params.extras or {}

        if self._pos is None:
            # Warm restart recovery: if we already hold an OPT: position, adopt it.
            try:
                pf = await self.ctx.portfolio()
                for p in (pf.get("positions") or []):
                    sym = str((p or {}).get("symbol") or "")
                    qty = float((p or {}).get("pos_qty") or 0.0)
                    if qty <= 0 or not sym.upper().startswith("OPT:"):
                        continue
                    try:
                        parts = parse_internal_opt_symbol(sym)
                    except Exception:
                        continue
                    if str(parts.underlying).upper() != self.underlying:
                        continue
                    adopted_state = self._restore_adopted_position_state(now, sym, qty)
                    if str(adopted_state.get("source") or "") == "heuristic_qty":
                        continue
                    side = "CALL" if parts.right == "C" else "PUT"
                    self._pos = _PositionState(
                        opt_symbol=sym,
                        side=side,
                        entry_time=adopted_state.get("entry_time", now),
                        entry_underlying=float(bar.get("close") or 0.0),
                        entry_close=float(bar.get("close") or 0.0),
                        entry_vwap=float(bar.get("vwap") or bar.get("close") or 0.0),
                        trace_id=str(adopted_state.get("trace_id") or "adopted"),
                        tp1_done=bool(adopted_state.get("tp1_done", False)),
                    )
                    self.ctx.log.warning(
                        "qqq_0dte_position_adopted",
                        ts=now.strftime("%Y-%m-%d %H:%M:%S"),
                        strategy_id=str(self.strategy_id),
                        symbol=str(sym),
                        qty=float(qty),
                        restored_from=str(adopted_state.get("source") or "unknown"),
                        restored_trace_id=str(adopted_state.get("trace_id") or "adopted"),
                        restored_tp1_done=bool(adopted_state.get("tp1_done", False)),
                    )
                    self._emit_local_audit(
                        stage="position_adopted",
                        status="adopted",
                        reason="WARM_RESTART_POSITION_FOUND",
                        symbol=str(sym),
                        trace_id=str(adopted_state.get("trace_id") or "adopted"),
                        payload={
                            "qty": float(qty),
                            "restored_from": str(adopted_state.get("source") or "unknown"),
                            "restored_trace_id": str(adopted_state.get("trace_id") or "adopted"),
                            "restored_tp1_done": bool(adopted_state.get("tp1_done", False)),
                        },
                    )
                    self._emit_local_audit(
                        stage="runner_state_restored",
                        status="restored",
                        reason=str(adopted_state.get("source")),
                        symbol=str(sym),
                        trace_id=str(adopted_state.get("trace_id") or "adopted"),
                        payload={
                            "tp1_done": bool(adopted_state.get("tp1_done", False)),
                            "entry_time": adopted_state.get("entry_time"),
                        },
                    )
                    break
            except Exception:
                pass
            # If no tracked position, still avoid duplicate entries if there are open option orders.
            try:
                oos = await self.ctx.open_orders()
                if self._has_strategy_owned_open_option_orders(oos):
                    self.ctx.log.info(
                        "qqq_0dte_entry_paused_open_order",
                        ts=now.strftime("%Y-%m-%d %H:%M:%S"),
                        strategy_id=str(self.strategy_id),
                    )
                    return True
            except Exception:
                pass
            return False

        opt_symbol = self._pos.opt_symbol
        ps = await self.ctx.position(opt_symbol)
        qty = self._strategy_owned_qty_cap(
            opt_symbol=opt_symbol,
            account_qty=float(getattr(ps, "pos_qty", 0.0) or 0.0),
        )
        if qty <= 0:
            # Pending entry: keep state as long as there is a working order for this contract.
            try:
                oos = await self.ctx.open_orders()
                if self._has_strategy_owned_open_option_orders(oos, opt_symbol=opt_symbol):
                    return True
            except Exception:
                pass
            # Entry timeout cancel path (no fills) lands here as well.
            if self._pos.filled:
                self.ctx.log.warning(
                    "qqq_0dte_position_disappeared",
                    ts=now.strftime("%Y-%m-%d %H:%M:%S"),
                    strategy_id=str(self.strategy_id),
                    trace_id=str(self._pos.trace_id or ""),
                    symbol=opt_symbol,
                    last_exit_reason=str(self._pos.last_exit_reason or ""),
                    exit_attempts=int(self._pos.exit_attempts or 0),
                )
                if self._first_trade_exit_reason is None:
                    self._first_trade_exit_reason = str(self._pos.last_exit_reason or "MANUAL_FLATTEN").upper()
            self._pos = None
            return False
        if not self._pos.filled:
            self._pos.filled = True
            self._trades_filled_today += 1
            self.ctx.log.warning(
                "qqq_0dte_entry_fill_detected",
                ts=now.strftime("%Y-%m-%d %H:%M:%S"),
                strategy_id=str(self.strategy_id),
                trace_id=str(self._pos.trace_id or ""),
                symbol=str(opt_symbol),
                qty=float(qty),
                avg_px=float(getattr(ps, "avg_px", 0.0) or 0.0),
                trades_filled_today=int(self._trades_filled_today),
            )
            self._emit_local_audit(
                stage="entry_confirmed",
                status="filled",
                reason="POSITION_DETECTED",
                symbol=str(opt_symbol),
                trace_id=str(self._pos.trace_id or ""),
                payload={
                    "qty": float(qty),
                    "avg_px": float(getattr(ps, "avg_px", 0.0) or 0.0),
                    "trades_filled_today": int(self._trades_filled_today),
                },
            )

        entry_premium = float(getattr(ps, "avg_px", 0.0) or 0.0)
        if entry_premium <= 0:
            return True

        force_flatten_time = _parse_hms(extras.get("force_flatten_time", "15:58:00"), time(15, 58))
        if now.time() >= force_flatten_time:
            self.ctx.log.info(
                "qqq_0dte_EOD_FORCE_FLATTEN",
                strategy_id=str(self.strategy_id),
                symbol=opt_symbol,
                qty=float(qty),
                ts=now.strftime("%Y-%m-%d %H:%M:%S"),
            )
            await self._submit_exit(
                now,
                opt_symbol,
                sell_qty=int(round(qty)),
                reason="EOD_FORCE_FLATTEN",
                current_qty=qty,
            )
            return True

        mark = self._get_mark_for_contract(now, opt_symbol)
        if mark is None or mark <= 0:
            now_min = _minute_floor(now)
            if self._last_mark_missing_log_ts != now_min:
                self._last_mark_missing_log_ts = now_min
                self.ctx.log.warning(
                    "qqq_0dte_mark_missing",
                    ts=now.strftime("%Y-%m-%d %H:%M:%S"),
                    symbol=opt_symbol,
                    underlying=self.underlying,
                )
            return True

        held_min = max(0.0, (now - self._pos.entry_time).total_seconds() / 60.0)
        opt_return = (mark - entry_premium) / entry_premium

        now_min = _minute_floor(now)
        if self._last_pos_diag_log_ts != now_min:
            self._last_pos_diag_log_ts = now_min
            self.ctx.log.info(
                "qqq_0dte_position_diag",
                ts=now.strftime("%Y-%m-%d %H:%M:%S"),
                strategy_id=str(self.strategy_id),
                trace_id=str(self._pos.trace_id or ""),
                symbol=opt_symbol,
                side=str(self._pos.side),
                qty=float(qty),
                avg_px=float(entry_premium),
                mark=float(mark),
                opt_return=float(opt_return),
                held_min=float(round(held_min, 2)),
                tp1_done=bool(self._pos.tp1_done),
                last_exit_reason=str(self._pos.last_exit_reason or ""),
                exit_attempts=int(self._pos.exit_attempts or 0),
            )

        tp1_mult = float(extras.get("tp1_mult", 1.20))
        tp2_mult = float(extras.get("tp2_mult", 2.50))
        stop_mult = float(extras.get("stop_loss_mult", 0.65))

        # TP2 (runner)
        if self._pos.tp1_done and mark >= entry_premium * tp2_mult:
            self._log_exit_signal(
                now,
                reason="TP2",
                opt_symbol=opt_symbol,
                sell_qty=int(round(qty)),
                mark=float(mark),
                entry_premium=float(entry_premium),
                held_min=float(held_min),
                threshold_px=float(entry_premium * tp2_mult),
            )
            await self._submit_exit(now, opt_symbol, sell_qty=int(round(qty)), reason="TP2", current_qty=qty)
            return True

        # TP1 sell 2
        if (not self._pos.tp1_done) and mark >= entry_premium * tp1_mult:
            sell_qty = min(2, int(round(qty)))
            self._log_exit_signal(
                now,
                reason="TP1",
                opt_symbol=opt_symbol,
                sell_qty=int(sell_qty),
                mark=float(mark),
                entry_premium=float(entry_premium),
                held_min=float(held_min),
                threshold_px=float(entry_premium * tp1_mult),
            )
            submitted_qty = await self._submit_exit(now, opt_symbol, sell_qty=sell_qty, reason="TP1", current_qty=qty)
            if submitted_qty > 0:
                self._pos.tp1_done = True
            return True

        # Runner protect: floor at TP1
        if self._pos.tp1_done:
            floor = entry_premium * tp1_mult
            if mark >= entry_premium * 1.60:
                floor = max(floor, entry_premium * 1.40)
            if mark >= entry_premium * 2.00:
                floor = max(floor, entry_premium * 1.70)
            if mark <= floor:
                self._log_exit_signal(
                    now,
                    reason="RUNNER_PROTECT",
                    opt_symbol=opt_symbol,
                    sell_qty=int(round(qty)),
                    mark=float(mark),
                    entry_premium=float(entry_premium),
                    held_min=float(held_min),
                    threshold_px=float(floor),
                )
                await self._submit_exit(
                    now,
                    opt_symbol,
                    sell_qty=int(round(qty)),
                    reason="RUNNER_PROTECT",
                    current_qty=qty,
                )
                return True

        # Hard stop
        if mark <= entry_premium * stop_mult:
            self._log_exit_signal(
                now,
                reason="HARD_STOP",
                opt_symbol=opt_symbol,
                sell_qty=int(round(qty)),
                mark=float(mark),
                entry_premium=float(entry_premium),
                held_min=float(held_min),
                threshold_px=float(entry_premium * stop_mult),
            )
            await self._submit_exit(now, opt_symbol, sell_qty=int(round(qty)), reason="HARD_STOP", current_qty=qty)
            return True

        # Early continuation fail applies only before TP1 and only across the
        # first two completed 1m bars after entry.
        if not self._pos.tp1_done and not self._pos.early_fail_triggered:
            extras = self.params.extras or {}
            enable_early_fail = bool(extras.get("enable_early_cont_fail", True))
            early_fail_bars = max(1, int(extras.get("early_cont_fail_bars", 2)))
            soft_move_atr = float(extras.get("early_cont_exempt_first1_move_atr1", 0.08))
            soft_giveback_max = float(extras.get("early_cont_exempt_first1_max_giveback", 0.50))
            if enable_early_fail:
                entry_bars = self._bars_after_entry(self._pos.entry_time)
                first_bar = entry_bars[0] if len(entry_bars) >= 1 else None
                if first_bar is not None and not self._pos.early_fail_decided:
                    atr_1m_entry = float(getattr(self._pos, "entry_atr_1m", 0.0) or 0.0)
                    favorable_move = self._favorable_move_for_bar(
                        self._pos.side,
                        float(self._pos.entry_underlying),
                        first_bar,
                    )
                    giveback_ratio = self._giveback_ratio_for_bar(
                        self._pos.side,
                        float(self._pos.entry_underlying),
                        first_bar,
                    )
                    self._pos.entry_first1_favorable_move = float(favorable_move)
                    self._pos.entry_first1_giveback_ratio = float(giveback_ratio)
                    qualifies_soft = (
                        bool(self._pos.entry_counter_vwap)
                        and atr_1m_entry > 0
                        and favorable_move >= soft_move_atr * atr_1m_entry
                        and giveback_ratio <= soft_giveback_max
                    )
                    self._pos.early_fail_mode = "soft_exempt" if qualifies_soft else "hard"
                    self._pos.early_fail_decided = True
                    self._emit_early_fail_mode_log(now, first_bar, atr_1m_entry)

                if len(entry_bars) >= early_fail_bars:
                    first_two = entry_bars[:early_fail_bars]
                    entry_ref = float(self._pos.entry_underlying)
                    should_fail = False
                    threshold_px = float(entry_ref)
                    if self._pos.early_fail_mode == "soft_exempt":
                        atr_1m_entry = float(getattr(self._pos, "entry_atr_1m", 0.0) or 0.0)
                        favorable_move = max(
                            self._favorable_move_for_bar(self._pos.side, entry_ref, first_two[0]),
                            self._favorable_move_for_bar(self._pos.side, entry_ref, first_two[1]),
                        )
                        threshold_px = float(entry_ref + (soft_move_atr * atr_1m_entry if self._pos.side == "CALL" else -(soft_move_atr * atr_1m_entry)))
                        should_fail = atr_1m_entry > 0 and favorable_move < soft_move_atr * atr_1m_entry
                    else:
                        if self._pos.side == "CALL":
                            should_fail = max(float(b.get("high") or entry_ref) for b in first_two) <= entry_ref
                        else:
                            should_fail = min(float(b.get("low") or entry_ref) for b in first_two) >= entry_ref
                    if should_fail:
                        self._pos.early_fail_triggered = True
                        self.ctx.log.warning(
                            "early_cont_fail_triggered",
                            ts=now.strftime("%Y-%m-%d %H:%M:%S"),
                            strategy_id=str(self.strategy_id),
                            trace_id=str(self._pos.trace_id or ""),
                            symbol=str(opt_symbol),
                            side=str(self._pos.side),
                            early_fail_mode=str(self._pos.early_fail_mode),
                            entry_counter_vwap=bool(self._pos.entry_counter_vwap),
                            entry_first1_favorable_move=float(self._pos.entry_first1_favorable_move),
                            entry_first1_giveback_ratio=float(self._pos.entry_first1_giveback_ratio),
                            held_min=float(round(held_min, 2)),
                        )
                        self._log_exit_signal(
                            now,
                            reason="EARLY_CONT_FAIL",
                            opt_symbol=opt_symbol,
                            sell_qty=int(round(qty)),
                            mark=float(mark),
                            entry_premium=float(entry_premium),
                            held_min=float(held_min),
                            threshold_px=float(threshold_px),
                        )
                        await self._submit_exit(
                            now,
                            opt_symbol,
                            sell_qty=int(round(qty)),
                            reason="EARLY_CONT_FAIL",
                            current_qty=qty,
                        )
                        return True

        # Late no-move
        no_move_min_hold = float(extras.get("no_move_min_hold_min", 5.0))
        no_move_atr = float(extras.get("no_move_underlying_move_atr", 0.15))
        no_move_min_ret = float(extras.get("no_move_min_opt_return", 0.10))
        atr_d = self._resolve_atr_d(bar)
        if held_min >= no_move_min_hold and atr_d > 0:
            move = abs(float(bar["close"]) - float(self._pos.entry_underlying)) / atr_d
            if move < no_move_atr and opt_return < no_move_min_ret:
                self._log_exit_signal(
                    now,
                    reason="LATE_NO_MOVE_EXIT",
                    opt_symbol=opt_symbol,
                    sell_qty=int(round(qty)),
                    mark=float(mark),
                    entry_premium=float(entry_premium),
                    held_min=float(held_min),
                    threshold_px=float(entry_premium * (1.0 + no_move_min_ret)),
                )
                await self._submit_exit(
                    now,
                    opt_symbol,
                    sell_qty=int(round(qty)),
                    reason="LATE_NO_MOVE_EXIT",
                    current_qty=qty,
                )
                return True

        # VWAP exit only pre-TP1
        vwap_exit_only_pre_tp1 = bool(extras.get("vwap_exit_only_pre_tp1", True))
        if (not vwap_exit_only_pre_tp1) or (not self._pos.tp1_done):
            vwap_exit_minutes = float(extras.get("vwap_exit_minutes", 12.0))
            vwap = float(bar.get("vwap") or 0.0)
            if held_min >= vwap_exit_minutes and vwap > 0:
                if self._pos.side == "CALL" and float(bar["close"]) < vwap:
                    self._log_exit_signal(
                        now,
                        reason="VWAP_EXIT",
                        opt_symbol=opt_symbol,
                        sell_qty=int(round(qty)),
                        mark=float(mark),
                        entry_premium=float(entry_premium),
                        held_min=float(held_min),
                        threshold_px=float(vwap),
                    )
                    await self._submit_exit(
                        now,
                        opt_symbol,
                        sell_qty=int(round(qty)),
                        reason="VWAP_EXIT",
                        current_qty=qty,
                    )
                    return True
                if self._pos.side == "PUT" and float(bar["close"]) > vwap:
                    self._log_exit_signal(
                        now,
                        reason="VWAP_EXIT",
                        opt_symbol=opt_symbol,
                        sell_qty=int(round(qty)),
                        mark=float(mark),
                        entry_premium=float(entry_premium),
                        held_min=float(held_min),
                        threshold_px=float(vwap),
                    )
                    await self._submit_exit(
                        now,
                        opt_symbol,
                        sell_qty=int(round(qty)),
                        reason="VWAP_EXIT",
                        current_qty=qty,
                    )
                    return True

        return True

    def _get_quote_for_contract(
        self,
        now: datetime,
        opt_symbol: str,
        *,
        lookback_min: int,
        max_age_sec: float,
        fallback_to_ch: bool = True,
    ) -> Optional[Dict[str, Any]]:
        return get_quote_for_opt_symbol(
            now=now,
            opt_symbol=opt_symbol,
            lookback_min=lookback_min,
            max_age_sec=max_age_sec,
            dte_min=0,
            dte_max=0,
            fallback_to_ch=fallback_to_ch,
        )

    def _get_mark_for_contract(self, now: datetime, opt_symbol: str) -> Optional[float]:
        extras = self.params.extras or {}
        lookback_min = max(1, int(extras.get("mark_quote_lookback_min", 2)))
        quote_max_age_sec = max(
            1.0,
            float(extras.get("mark_quote_max_age_sec", extras.get("entry_quote_max_age_sec", 90.0))),
        )
        mark_allow_ch_fallback = bool(extras.get("mark_quote_allow_ch_fallback", True))
        q = self._get_quote_for_contract(
            now,
            opt_symbol,
            lookback_min=lookback_min,
            max_age_sec=quote_max_age_sec,
            fallback_to_ch=mark_allow_ch_fallback,
        )
        if q is None:
            return None
        mid = float(q.get("mid") or 0.0)
        if mid > 0:
            return mid
        bid = float(q.get("bid") or 0.0)
        ask = float(q.get("ask") or 0.0)
        if bid > 0 and ask > 0:
            return (bid + ask) / 2.0
        return None

    def _log_exit_signal(
        self,
        now: datetime,
        *,
        reason: str,
        opt_symbol: str,
        sell_qty: int,
        mark: float,
        entry_premium: float,
        held_min: float,
        threshold_px: float,
    ) -> None:
        self.ctx.log.warning(
            "qqq_0dte_exit_signal",
            ts=now.strftime("%Y-%m-%d %H:%M:%S"),
            strategy_id=str(self.strategy_id),
            trace_id=str((self._pos.trace_id if self._pos is not None else "") or ""),
            symbol=opt_symbol,
            reason=str(reason),
            sell_qty=int(sell_qty),
            mark=float(mark),
            entry_premium=float(entry_premium),
            threshold_px=float(threshold_px),
            opt_return=float((mark - entry_premium) / entry_premium) if entry_premium > 0 else 0.0,
            held_min=float(round(held_min, 2)),
            tp1_done=bool(self._pos.tp1_done) if self._pos is not None else False,
            last_exit_reason=str((self._pos.last_exit_reason if self._pos is not None else "") or ""),
            exit_attempts=int((self._pos.exit_attempts if self._pos is not None else 0) or 0),
        )
        self._emit_local_audit(
            stage="exit_signal",
            severity="warning",
            status="triggered",
            reason=str(reason),
            symbol=str(opt_symbol),
            trace_id=str((self._pos.trace_id if self._pos is not None else "") or ""),
            payload={
                "sell_qty": int(sell_qty),
                "mark": float(mark),
                "entry_premium": float(entry_premium),
                "threshold_px": float(threshold_px),
                "held_min": float(round(held_min, 2)),
                "tp1_done": bool(self._pos.tp1_done) if self._pos is not None else False,
                "position_id": str((self._pos.position_id if self._pos is not None else "") or ""),
                "entry_snapshot_id": str((self._pos.entry_snapshot_id if self._pos is not None else "") or ""),
            },
        )

    async def _get_working_exit_state(self, opt_symbol: str) -> Tuple[int, List[Dict[str, Any]]]:
        try:
            open_orders = await self.ctx.open_orders()
        except Exception:
            return 0, []

        working_qty = 0
        sample: List[Dict[str, Any]] = []
        for order in open_orders or []:
            symbol = str(getattr(order, "symbol", "") or "")
            side = str(getattr(order, "side", "") or "").upper()
            status = getattr(order, "status", "")
            if symbol != opt_symbol or side != "SELL" or not _order_status_is_working(status):
                continue
            if not self._order_belongs_to_strategy(order):
                continue
            qty = getattr(order, "remaining", None)
            if qty is None:
                qty = getattr(order, "qty", None)
            if qty is None:
                qty = getattr(order, "order_qty", None)
            try:
                qty_i = max(0, int(round(float(qty or 0.0))))
            except Exception:
                qty_i = 0
            working_qty += qty_i
            sample.append(
                {
                    "order_id": getattr(order, "order_id", None),
                    "status": str(status or ""),
                    "qty": qty_i,
                    "order_type": str(getattr(order, "order_type", "") or ""),
                    "order_ref": str(getattr(order, "order_ref", "") or ""),
                }
            )
        return int(working_qty), sample[:4]

    async def _submit_exit(
        self,
        now: datetime,
        opt_symbol: str,
        *,
        sell_qty: int,
        reason: str,
        current_qty: Optional[float] = None,
    ) -> int:
        if sell_qty <= 0:
            return 0

        working_exit_qty, working_sample = await self._get_working_exit_state(opt_symbol)
        position_qty = float(current_qty if current_qty is not None else sell_qty)
        position_qty = self._strategy_owned_qty_cap(opt_symbol=opt_symbol, account_qty=position_qty)
        available_qty = max(0, int(math.floor(max(0.0, position_qty - float(working_exit_qty)) + 1e-9)))
        submit_qty = min(int(sell_qty), int(available_qty))
        if submit_qty <= 0:
            self.ctx.log.warning(
                "qqq_0dte_exit_guard_blocked",
                ts=now.strftime("%Y-%m-%d %H:%M:%S"),
                strategy_id=str(self.strategy_id),
                trace_id=str((self._pos.trace_id if self._pos is not None else "") or ""),
                symbol=str(opt_symbol),
                reason=str(reason),
                requested_sell_qty=int(sell_qty),
                position_qty=float(position_qty),
                working_exit_qty=int(working_exit_qty),
                working_exit_orders=working_sample,
            )
            self._emit_local_audit(
                stage="exit_guard_blocked",
                severity="warning",
                status="blocked",
                reason=str(reason),
                symbol=str(opt_symbol),
                trace_id=str((self._pos.trace_id if self._pos is not None else "") or ""),
                payload={
                    "requested_sell_qty": int(sell_qty),
                    "position_qty": float(position_qty),
                    "working_exit_qty": int(working_exit_qty),
                    "working_exit_orders": working_sample,
                    "position_id": str((self._pos.position_id if self._pos is not None else "") or ""),
                },
            )
            return 0
        if submit_qty < int(sell_qty):
            self.ctx.log.warning(
                "qqq_0dte_exit_guard_partial_allow",
                ts=now.strftime("%Y-%m-%d %H:%M:%S"),
                strategy_id=str(self.strategy_id),
                trace_id=str((self._pos.trace_id if self._pos is not None else "") or ""),
                symbol=str(opt_symbol),
                reason=str(reason),
                requested_sell_qty=int(sell_qty),
                adjusted_sell_qty=int(submit_qty),
                position_qty=float(position_qty),
                working_exit_qty=int(working_exit_qty),
                working_exit_orders=working_sample,
            )
            self._emit_local_audit(
                stage="exit_guard_partial_allow",
                severity="warning",
                status="adjusted",
                reason=str(reason),
                symbol=str(opt_symbol),
                trace_id=str((self._pos.trace_id if self._pos is not None else "") or ""),
                payload={
                    "requested_sell_qty": int(sell_qty),
                    "adjusted_sell_qty": int(submit_qty),
                    "position_qty": float(position_qty),
                    "working_exit_qty": int(working_exit_qty),
                    "working_exit_orders": working_sample,
                    "position_id": str((self._pos.position_id if self._pos is not None else "") or ""),
                },
            )

        sell_qty = int(submit_qty)
        if self._pos is not None and str(self._pos.opt_symbol) == str(opt_symbol):
            self._pos.last_exit_reason = str(reason)
            self._pos.exit_attempts = int(self._pos.exit_attempts or 0) + 1
        extras = self.params.extras or {}
        max_age_sec = max(1.0, float(extras.get("exit_degraded_nbbo_max_age_sec", 5.0)))
        q = self._get_quote_for_contract(
            now,
            opt_symbol,
            lookback_min=max(1, int(extras.get("exit_quote_lookback_min", 3))),
            max_age_sec=max(300.0, max_age_sec),
        )
        tick = max(0.01, float(extras.get("opt_tick_size", 0.01)))

        quote_age = float(q.get("age_sec") or 9_999.0) if q else 9_999.0
        bid = float(q.get("bid") or 0.0) if q else 0.0
        ask = float(q.get("ask") or 0.0) if q else 0.0
        mid = float(q.get("mid") or 0.0) if q else 0.0
        if mid <= 0 and bid > 0 and ask > 0:
            mid = (bid + ask) / 2.0

        degraded = (bid > 0 and ask > 0 and quote_age <= max_age_sec)
        limit_px: float
        mode: str
        if degraded:
            spread = max(0.0, ask - bid)
            # Marketable sell LMT: keep a small cushion below bid to reduce no-fill risk.
            limit_px = max(0.01, bid - max(tick, spread * 0.10))
            mode = "EXIT_DEGRADED"
        else:
            # Emergency protection ladder: tolerate stale/missing NBBO but control slippage by LMT.
            steps = extras.get("exit_emergency_ladder", [0.98, 0.95, 0.92, 0.88])
            ladder = [float(x) for x in (steps or [0.98, 0.95, 0.92, 0.88])]
            if not ladder:
                ladder = [0.95]
            idx = min(len(ladder) - 1, max(0, int((self._pos.exit_attempts - 1) if self._pos else 0)))
            mult = float(ladder[idx])
            base = mid if mid > 0 else (bid if bid > 0 else (ask if ask > 0 else 0.01))
            limit_px = max(0.01, base * mult)
            mode = "EXIT_EMERGENCY"

        # tick alignment (avoids reglite tick_size reject)
        limit_px = round(round(limit_px / tick) * tick, 10)
        tif = str(extras.get("exit_tif", "IOC")).upper()
        if tif not in {"IOC", "DAY", "GTC"}:
            tif = "IOC"

        self.ctx.log.warning(
            "qqq_0dte_exit_mode",
            ts=now.strftime("%Y-%m-%d %H:%M:%S"),
            strategy_id=str(self.strategy_id),
            trace_id=str((self._pos.trace_id if self._pos is not None else "") or ""),
            symbol=opt_symbol,
            reason=str(reason),
            mode=mode,
            sell_qty=int(sell_qty),
            quote_age_sec=float(quote_age),
            bid=float(bid),
            ask=float(ask),
            mid=float(mid),
            limit_px=float(limit_px),
            tif=tif,
        )

        trigger_id = str((self._pos.trace_id if self._pos is not None else "") or "")
        decision_id = str(make_decision_id() or "").strip() or str(uuid.uuid4())
        self.ctx.log.warning(
            "qqq_0dte_exit_submit",
            ts=now.strftime("%Y-%m-%d %H:%M:%S"),
            strategy_id=str(self.strategy_id),
            trace_id=str((self._pos.trace_id if self._pos is not None else "") or ""),
            decision_id=decision_id,
            symbol=str(opt_symbol),
            reason=str(reason),
            sell_qty=int(sell_qty),
            mode=str(mode),
            limit_px=float(limit_px),
            tif=str(tif),
        )
        self._emit_local_audit(
            stage="exit_submit_requested",
            severity="warning",
            status="requested",
            reason=str(reason),
            symbol=str(opt_symbol),
            decision_id=decision_id,
            trigger_id=trigger_id or None,
            trace_id=str((self._pos.trace_id if self._pos is not None else "") or ""),
            payload={
                "sell_qty": int(sell_qty),
                "mode": str(mode),
                "limit_px": float(limit_px),
                "tif": str(tif),
                "quote_age_sec": float(quote_age),
                "position_id": str((self._pos.position_id if self._pos is not None else "") or ""),
            },
        )
        env = DecisionEnvelope(
            decision_id=decision_id,
            strategy_id=str(self.strategy_id),
            symbol=opt_symbol,
            ts_bar_end_us=int(now.timestamp() * 1_000_000),
            target_qty=-float(sell_qty),
            freshness=Freshness(dlb_ms=30_000, ttl_ms=int(self.params.ttl_ms)),
            signal_score=1.0,
            order_style=OrderStyle(kind="LMT", limit_px=float(limit_px)),
            extras={
                "instrument_type": "OPTION",
                "underlying": self.underlying,
                "bars_symbol": self.underlying,
                "risk_symbol": self.risk_symbol,
                "contracts_mult": 100,
                "force_order_style": True,
                "disable_oco": True,
                "close_only": True,
                "position_qty_hint": float(sell_qty),
                "order_ref": f"opt0dte_exit:{self.strategy_id}:{decision_id}"[:45],
                "allocation_id": str((self._pos.trace_id if self._pos is not None else "") or ""),
                "exit_reason": str(reason),
                "position_id": str((self._pos.position_id if self._pos is not None else "") or ""),
                "entry_snapshot_id": str((self._pos.entry_snapshot_id if self._pos is not None else "") or ""),
                "entry_decision_id": str((self._pos.entry_decision_id if self._pos is not None else "") or ""),
                "tif": tif,
                "ref_px": float(mid if mid > 0 else limit_px),
                "ref_px_source": "options_quotes_1m_exit",
                "opt_bid_px": float(bid),
                "opt_ask_px": float(ask),
                "opt_mid_px": float(mid if mid > 0 else limit_px),
                "opt_quote_age_sec": float(quote_age),
                "exit_exec_mode": mode,
                "protective_stop": True,
                "trace_id": str((self._pos.trace_id if self._pos is not None else "") or ""),
                "trigger_id": trigger_id or decision_id,
                "allow_same_option_symbol_cross_strategy": bool(extras.get("allow_same_option_symbol_cross_strategy", False)),
                "shared_cap_group_id": extras.get("shared_cap_group_id"),
                "shared_cap_group_limit_usd": extras.get("shared_cap_group_limit_usd"),
                "shared_cap_group_strategy_ids": extras.get("shared_cap_group_strategy_ids"),
            },
        )
        try:
            await self.ctx.submit_intent(env)
            self._emit_local_audit(
                stage="exit_submitted",
                status="submitted",
                reason=str(reason),
                symbol=str(opt_symbol),
                decision_id=decision_id,
                trigger_id=trigger_id or None,
                trace_id=str((self._pos.trace_id if self._pos is not None else "") or ""),
                payload={
                    "sell_qty": int(sell_qty),
                    "mode": str(mode),
                    "limit_px": float(limit_px),
                    "position_id": str((self._pos.position_id if self._pos is not None else "") or ""),
                },
            )
            return int(sell_qty)
        except Exception as exc:
            self._emit_local_audit(
                stage="exit_submit_failed",
                severity="warning",
                status="failed",
                reason=str(reason),
                symbol=str(opt_symbol),
                decision_id=decision_id,
                trigger_id=trigger_id or None,
                trace_id=str((self._pos.trace_id if self._pos is not None else "") or ""),
                payload={
                    "sell_qty": int(sell_qty),
                    "mode": str(mode),
                    "error": str(exc),
                },
            )
            raise
