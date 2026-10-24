#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Replay QQQIntradayStrategy with live-like engine lifecycle (offline, non-live).

Goals:
- Import and run strategies.qqq_intraday_strategy.QQQIntradayStrategy directly.
- Feed historical market data in 1-second steps (expanded from 1m bars).
- Simulate intent -> order submit -> fill -> position update lifecycle.
- Emit reconciliation logs with signal/decision/order/fill timestamps and prices.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import math
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
import types
from typing import Any, Dict, Optional

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from libs.db.ch_pool import get_client
from libs.strategy import OrderUpdate, PositionSnapshot, StrategyParams
import strategies.qqq_intraday_strategy as qmod
from strategies.qqq_intraday_strategy import ET


@dataclass
class Bar1m:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class OrderRec:
    order_id: str
    strategy_id: str
    symbol: str
    qty: int
    order_type: str
    order_ref: str
    reason: str
    close_only: bool
    stop_px: Optional[float]
    submit_ts: datetime
    submit_price: float
    state: str = "SUBMITTED"
    fill_ts: Optional[datetime] = None
    fill_price: Optional[float] = None


@dataclass
class ReplayPolicy:
    profile: str = "baseline"
    day_stop_after_n_hard_stops: int = 2
    anti_choptop_enable: bool = True
    anti_choptop_apply_to: str = "drift"
    anti_choptop_window_start: time = time(9, 45, 0)
    anti_choptop_window_end: time = time(12, 30, 0)
    anti_choptop_ttl_sec: int = 180
    anti_choptop_rank_high: float = 0.93
    anti_choptop_rank_low: float = 0.07
    anti_choptop_sign_flip_5m_min: int = 1
    anti_choptop_dev_atr_d_min: float = 1.00
    entry_check_on_new_minute_only: bool = False
    signal_ttl_sec: int = 0
    max_chase_atr_d: float = 0.0
    drift_max_dev_vwap_atr_d: float = 0.0
    drift_max_dev_window_start: time = time(9, 45, 0)
    drift_max_dev_window_end: time = time(14, 0, 0)
    drift_max_dev_sign_flip_5m_min: int = 0
    drift_max_dev_rank_high: float = 0.0
    history_ready_observe_only: bool = False
    history_ready_seed_fallback: bool = False
    entry_dev_vwap_max_atr_d: float = 0.0
    entry_dev_anchor_max_atr_d: float = 0.0
    logic_retreat_confirm_sec: int = 0
    overext_pullback_reentry_enable: bool = False
    overext_reentry_dev_vwap_atr: float = 0.0
    overext_reentry_dev_anchor_atr: float = 0.0
    overext_mark_ttl_sec: int = 180
    momentum_decay_veto_enable: bool = False
    momentum_decay_ratio: float = 0.85
    momentum_decay_lookback: int = 3
    stop_hit_count: int = 0
    block_until: dict[str, datetime] = field(default_factory=dict)
    recent_qqq_minute_closes: list[tuple[datetime, float]] = field(default_factory=list)
    last_gate: Optional[dict[str, Any]] = None
    overext_wait_direction: Optional[str] = None
    overext_wait_mark_ts: Optional[datetime] = None

    @classmethod
    def from_profile(cls, profile: str) -> "ReplayPolicy":
        p = cls(profile=profile)
        if profile == "baseline":
            p.anti_choptop_enable = False
        elif profile == "patch_0227":
            pass
        else:
            raise ValueError(f"unknown --profile: {profile}")
        return p


class ReplayClock:
    def __init__(self) -> None:
        self.now_et: datetime = datetime.now(ET)

    def set(self, ts: datetime) -> None:
        self.now_et = ts.astimezone(ET)

    def now(self, tz=None):
        if tz is None:
            return self.now_et.replace(tzinfo=None)
        return self.now_et.astimezone(tz)


class ReplayDateTime:
    _clock: ReplayClock
    _real = datetime

    @classmethod
    def bind_clock(cls, clock: ReplayClock) -> None:
        cls._clock = clock

    @classmethod
    def now(cls, tz=None):
        return cls._clock.now(tz)

    @classmethod
    def fromtimestamp(cls, ts, tz=None):
        return cls._real.fromtimestamp(ts, tz=tz)

    @classmethod
    def strptime(cls, s, fmt):
        return cls._real.strptime(s, fmt)

    @classmethod
    def fromisoformat(cls, s):
        return cls._real.fromisoformat(s)


class ReplayBroker:
    def __init__(self, strategy_id: str, fill_delay_sec: int, slippage_bps: float, policy: ReplayPolicy) -> None:
        self.strategy_id = strategy_id
        self.fill_delay_sec = max(0, int(fill_delay_sec))
        self.slippage_bps = float(slippage_bps)
        self.policy = policy
        self.last_price: Dict[str, float] = {}
        self.open_orders: list[OrderRec] = []
        self.order_seq = 0
        self.positions: Dict[str, PositionSnapshot] = {}
        self.lifecycle: list[dict[str, Any]] = []

    def update_price(self, symbol: str, px: float) -> None:
        self.last_price[symbol] = float(px)

    def _update_recent_qqq_minute_close(self, now: datetime) -> None:
        if "QQQ" not in self.last_price:
            return
        if now.second != 59:
            return
        px = float(self.last_price.get("QQQ", 0.0) or 0.0)
        if px <= 0:
            return
        bucket = now.replace(second=0, microsecond=0)
        if self.policy.recent_qqq_minute_closes and self.policy.recent_qqq_minute_closes[-1][0] == bucket:
            self.policy.recent_qqq_minute_closes[-1] = (bucket, px)
        else:
            self.policy.recent_qqq_minute_closes.append((bucket, px))
        if len(self.policy.recent_qqq_minute_closes) > 30:
            self.policy.recent_qqq_minute_closes = self.policy.recent_qqq_minute_closes[-30:]

    @staticmethod
    def _rank_percentile(vals: list[float], x: float) -> float:
        if not vals:
            return 0.5
        n = len(vals)
        le = sum(1 for v in vals if v <= x)
        return float(le) / float(n)

    @staticmethod
    def _count_sign_flip(vals: list[float]) -> int:
        if len(vals) < 3:
            return 0
        flips = 0
        prev = 0
        for i in range(1, len(vals)):
            d = vals[i] - vals[i - 1]
            s = 1 if d > 0 else (-1 if d < 0 else 0)
            if s == 0:
                continue
            if prev != 0 and s != prev:
                flips += 1
            prev = s
        return flips

    def _entry_block_reason(self, now: datetime, rec: OrderRec) -> Optional[str]:
        is_entry = ":ENTRY:" in str(rec.order_ref)
        if self.policy.profile == "baseline":
            gate = self.policy.last_gate or {}
            if is_entry:
                if self.policy.history_ready_observe_only:
                    history_ready = gate.get("history_ready")
                    if history_ready is False:
                        return "observe_only_history_not_ready"
                    if self.policy.history_ready_seed_fallback:
                        # Optional fallback for schemas without explicit history_ready.
                        seed_ready = gate.get("seed_ready")
                        if seed_ready is False:
                            return "observe_only_seed_not_ready"
                if float(self.policy.entry_dev_vwap_max_atr_d) > 0 or float(self.policy.entry_dev_anchor_max_atr_d) > 0:
                    sig_qqq = float(gate.get("qqq_close_now", 0.0) or 0.0)
                    sig_vwap = float(gate.get("vwap_now", 0.0) or 0.0)
                    sig_anchor = float(gate.get("openclose_base", 0.0) or 0.0)
                    atr_eff = float(gate.get("atr_eff", 0.0) or 0.0)
                    if sig_qqq > 0 and atr_eff > 0:
                        direction = str(gate.get("direction", "")).upper()
                        if float(self.policy.entry_dev_vwap_max_atr_d) > 0 and sig_vwap > 0:
                            dev_vwap_atr = abs(sig_qqq - sig_vwap) / max(1e-9, atr_eff)
                            if dev_vwap_atr > float(self.policy.entry_dev_vwap_max_atr_d):
                                if self.policy.overext_pullback_reentry_enable:
                                    self.policy.overext_wait_direction = direction or None
                                    self.policy.overext_wait_mark_ts = now
                                return "entry_dev_vwap_too_far"
                        if float(self.policy.entry_dev_anchor_max_atr_d) > 0 and sig_anchor > 0:
                            dev_anchor_atr = abs(sig_qqq - sig_anchor) / max(1e-9, atr_eff)
                            if dev_anchor_atr > float(self.policy.entry_dev_anchor_max_atr_d):
                                if self.policy.overext_pullback_reentry_enable:
                                    self.policy.overext_wait_direction = direction or None
                                    self.policy.overext_wait_mark_ts = now
                                return "entry_dev_anchor_too_far"
                        if self.policy.overext_pullback_reentry_enable and self.policy.overext_wait_direction:
                            marked_dir = str(self.policy.overext_wait_direction or "")
                            mark_ts = self.policy.overext_wait_mark_ts
                            same_dir = bool(marked_dir) and bool(direction) and (marked_dir == direction)
                            ttl_ok = True
                            if mark_ts is not None:
                                ttl_ok = (now - mark_ts).total_seconds() <= max(1, int(self.policy.overext_mark_ttl_sec))
                            if (not same_dir) or (not ttl_ok):
                                self.policy.overext_wait_direction = None
                                self.policy.overext_wait_mark_ts = None
                            else:
                                if sig_vwap > 0 and float(self.policy.overext_reentry_dev_vwap_atr) > 0:
                                    dev_vwap_atr = abs(sig_qqq - sig_vwap) / max(1e-9, atr_eff)
                                    if dev_vwap_atr > float(self.policy.overext_reentry_dev_vwap_atr):
                                        return "overext_wait_pullback_vwap"
                                if sig_anchor > 0 and float(self.policy.overext_reentry_dev_anchor_atr) > 0:
                                    dev_anchor_atr = abs(sig_qqq - sig_anchor) / max(1e-9, atr_eff)
                                    if dev_anchor_atr > float(self.policy.overext_reentry_dev_anchor_atr):
                                        return "overext_wait_pullback_anchor"
                                self.policy.overext_wait_direction = None
                                self.policy.overext_wait_mark_ts = None
                if self.policy.momentum_decay_veto_enable:
                    direction = str(gate.get("direction", "")).upper()
                    if direction in {"LONG", "SHORT"} and len(self.policy.recent_qqq_minute_closes) >= (self.policy.momentum_decay_lookback + 1):
                        vals = [x[1] for x in self.policy.recent_qqq_minute_closes[-(self.policy.momentum_decay_lookback + 1):]]
                        moves: list[float] = []
                        for i in range(1, len(vals)):
                            dlt = float(vals[i] - vals[i - 1])
                            moves.append(dlt if direction == "LONG" else -dlt)
                        if len(moves) >= 3 and all(m > 0 for m in moves):
                            m1, m2, m3 = moves[-3], moves[-2], moves[-1]
                            ratio = max(0.1, min(0.99, float(self.policy.momentum_decay_ratio)))
                            if m2 < (m1 * ratio) and m3 < (m2 * ratio):
                                return "momentum_decay_veto"
                if float(self.policy.drift_max_dev_vwap_atr_d) > 0:
                    tt = now.timetz().replace(tzinfo=None)
                    if self.policy.drift_max_dev_window_start <= tt <= self.policy.drift_max_dev_window_end:
                        mode = str(gate.get("mode", "")).upper()
                        if mode == "DRIFT":
                            sig_qqq = float(gate.get("qqq_close_now", 0.0) or 0.0)
                            sig_vwap = float(gate.get("vwap_now", 0.0) or 0.0)
                            atr_eff = float(gate.get("atr_eff", 0.0) or 0.0)
                            if sig_qqq > 0 and sig_vwap > 0 and atr_eff > 0:
                                dev_atr = abs(sig_qqq - sig_vwap) / max(1e-9, atr_eff)
                                vals15 = [x[1] for x in self.policy.recent_qqq_minute_closes[-15:]]
                                vals15.append(sig_qqq)
                                rank15 = self._rank_percentile(vals15, sig_qqq)
                                vals6 = [x[1] for x in self.policy.recent_qqq_minute_closes[-6:]]
                                vals6.append(sig_qqq)
                                flips5m = self._count_sign_flip(vals6)
                                rank_ok = True
                                if float(self.policy.drift_max_dev_rank_high) > 0:
                                    rank_ok = rank15 >= float(self.policy.drift_max_dev_rank_high)
                                flips_ok = flips5m >= int(self.policy.drift_max_dev_sign_flip_5m_min)
                                if dev_atr > float(self.policy.drift_max_dev_vwap_atr_d) and rank_ok and flips_ok:
                                    return "drift_dev_vwap_atr_d"
                sig_ts = gate.get("ts_now_et") or gate.get("ts_et")
                if sig_ts and int(self.policy.signal_ttl_sec) > 0:
                    try:
                        sig_dt = datetime.fromisoformat(str(sig_ts)) if "T" in str(sig_ts) else datetime.strptime(str(sig_ts), "%Y-%m-%d %H:%M:%S").replace(tzinfo=ET)
                        if (now - sig_dt).total_seconds() > float(self.policy.signal_ttl_sec):
                            return "signal_ttl_expired"
                    except Exception:
                        pass
                if float(self.policy.max_chase_atr_d) > 0:
                    sig_px = float(gate.get("qqq_close_now", 0.0) or 0.0)
                    atr_eff = float(gate.get("atr_eff", 0.0) or 0.0)
                    qqq_px = float(self.last_price.get("QQQ", 0.0) or 0.0)
                    if sig_px > 0 and atr_eff > 0 and qqq_px > 0:
                        chase = abs(qqq_px - sig_px) / max(1e-9, atr_eff)
                        if chase > float(self.policy.max_chase_atr_d):
                            return "max_chase_atr_d"
            return None
        if not is_entry:
            return None
        if self.policy.stop_hit_count >= int(self.policy.day_stop_after_n_hard_stops):
            return "day_stop_after_hard_stops"
        gate = self.policy.last_gate or {}
        direction = str(gate.get("direction", "")).upper()
        if not direction:
            direction = "LONG" if rec.symbol == "TQQQ" else ("SHORT" if rec.symbol == "SQQQ" else "")
        until = self.policy.block_until.get(direction)
        if until is not None and now < until:
            return f"anti_choptop_ttl_{direction.lower()}"
        if not self.policy.anti_choptop_enable:
            return None
        tt = now.timetz().replace(tzinfo=None)
        if tt < self.policy.anti_choptop_window_start or tt > self.policy.anti_choptop_window_end:
            return None
        apply_to = str(self.policy.anti_choptop_apply_to or "drift").lower()
        mode = str(gate.get("mode", "")).lower()
        if apply_to == "drift" and mode != "drift":
            return None
        qqq = float(self.last_price.get("QQQ", 0.0) or 0.0)
        vwap = float(gate.get("vwap_now", 0.0) or 0.0)
        atr = float(gate.get("atr_eff", 0.0) or 0.0)
        if qqq <= 0 or vwap <= 0 or atr <= 0:
            return None
        vals = [x[1] for x in self.policy.recent_qqq_minute_closes[-15:]]
        vals.append(qqq)
        rank = self._rank_percentile(vals, qqq)
        flips = self._count_sign_flip([x[1] for x in self.policy.recent_qqq_minute_closes[-6:]] + [qqq])
        dev_atr = abs(qqq - vwap) / max(1e-9, atr)
        trig = False
        if direction == "LONG":
            trig = rank >= float(self.policy.anti_choptop_rank_high)
        elif direction == "SHORT":
            trig = rank <= float(self.policy.anti_choptop_rank_low)
        trig = trig and flips >= int(self.policy.anti_choptop_sign_flip_5m_min) and dev_atr >= float(self.policy.anti_choptop_dev_atr_d_min)
        if not trig:
            return None
        self.policy.block_until[direction] = now + timedelta(seconds=int(self.policy.anti_choptop_ttl_sec))
        return f"anti_choptop_block_{direction.lower()}_ttl"

    def _next_order_id(self) -> str:
        self.order_seq += 1
        return f"replay_{self.order_seq}"

    async def submit_intent(self, envelope, now: datetime) -> None:
        symbol = str(envelope.symbol)
        qty = int(round(float(getattr(envelope, "target_qty", 0.0) or 0.0)))
        if qty == 0:
            return
        style = getattr(envelope, "order_style", None)
        order_type = str(getattr(style, "kind", "MKT") or "MKT").upper()
        extras = getattr(envelope, "extras", {}) or {}
        order_ref = str(extras.get("order_ref", ""))
        reason = str(extras.get("reason", ""))
        close_only = bool(extras.get("close_only", False))
        stop_px = getattr(style, "stop_px", None)
        submit_price = float(self.last_price.get(symbol, 0.0) or 0.0)

        rec = OrderRec(
            order_id=self._next_order_id(),
            strategy_id=self.strategy_id,
            symbol=symbol,
            qty=qty,
            order_type=order_type,
            order_ref=order_ref,
            reason=reason,
            close_only=close_only,
            stop_px=float(stop_px) if stop_px is not None else None,
            submit_ts=now,
            submit_price=submit_price,
        )
        block_reason = self._entry_block_reason(now, rec)
        if block_reason:
            self.lifecycle.append(
                {
                    "kind": "blocked",
                    "ts": now.isoformat(),
                    "symbol": symbol,
                    "qty": qty,
                    "order_type": order_type,
                    "order_ref": order_ref,
                    "reason": block_reason,
                }
            )
            return
        self.open_orders.append(rec)
        self.lifecycle.append(
            {
                "kind": "submit",
                "ts": now.isoformat(),
                "order_id": rec.order_id,
                "symbol": symbol,
                "qty": qty,
                "order_type": order_type,
                "order_ref": order_ref,
                "reason": reason,
                "submit_price": submit_price,
            }
        )
        if str(order_ref).endswith(":FLATTEN") and str(reason) == "stop_hit":
            self.policy.stop_hit_count += 1

    def _position(self, symbol: str) -> PositionSnapshot:
        p = self.positions.get(symbol)
        if p is None:
            p = PositionSnapshot(strategy_id=self.strategy_id, symbol=symbol, pos_qty=0.0, avg_px=None, realized_pnl_usd=0.0)
            self.positions[symbol] = p
        return p

    def _apply_fill(self, symbol: str, qty: int, px: float, close_only: bool) -> None:
        p = self._position(symbol)
        cur_qty = float(p.pos_qty or 0.0)
        avg = float(p.avg_px or 0.0)
        fill_qty = int(qty)
        if close_only and fill_qty < 0:
            fill_qty = -int(min(abs(fill_qty), max(0.0, cur_qty)))
            if fill_qty == 0:
                return
        if fill_qty > 0:
            new_qty = cur_qty + fill_qty
            if new_qty > 0:
                p.avg_px = ((avg * cur_qty) + (px * fill_qty)) / new_qty if cur_qty > 0 else px
            p.pos_qty = new_qty
            return
        sell_qty = abs(fill_qty)
        if cur_qty > 0:
            close_qty = min(cur_qty, sell_qty)
            p.realized_pnl_usd += (px - avg) * close_qty
            remain = cur_qty - close_qty
            p.pos_qty = remain
            p.avg_px = avg if remain > 0 else None
            overflow = sell_qty - close_qty
            if overflow > 0:
                p.pos_qty = -float(overflow)
                p.avg_px = px
            return
        p.pos_qty = cur_qty - sell_qty
        p.avg_px = px if p.pos_qty != 0 else None

    def _fill_price(self, symbol: str, qty: int) -> float:
        base = float(self.last_price.get(symbol, 0.0) or 0.0)
        if base <= 0:
            return 0.0
        slip = base * (self.slippage_bps / 10000.0)
        return base + slip if qty > 0 else base - slip

    def _should_fill_mkt(self, rec: OrderRec, now: datetime) -> bool:
        return now >= (rec.submit_ts + timedelta(seconds=self.fill_delay_sec))

    def _should_trigger_stop(self, rec: OrderRec) -> bool:
        if rec.order_type not in {"STP", "STOP", "STP_LMT", "STOP_LIMIT"}:
            return False
        if rec.stop_px is None:
            return False
        px = float(self.last_price.get(rec.symbol, 0.0) or 0.0)
        if px <= 0:
            return False
        if rec.qty < 0:
            return px <= float(rec.stop_px)
        return px >= float(rec.stop_px)

    async def process_orders(self, now: datetime, strategy: Any) -> None:
        self._update_recent_qqq_minute_close(now)
        filled: list[OrderRec] = []
        for rec in self.open_orders:
            if rec.state != "SUBMITTED":
                continue
            do_fill = False
            if rec.order_type == "MKT":
                do_fill = self._should_fill_mkt(rec, now)
            else:
                do_fill = self._should_trigger_stop(rec)
            if not do_fill:
                continue
            px = self._fill_price(rec.symbol, rec.qty)
            if px <= 0:
                continue
            rec.state = "FILLED"
            rec.fill_ts = now
            rec.fill_price = px
            self._apply_fill(rec.symbol, rec.qty, px, rec.close_only)
            self.lifecycle.append(
                {
                    "kind": "fill",
                    "ts": now.isoformat(),
                    "order_id": rec.order_id,
                    "symbol": rec.symbol,
                    "qty": rec.qty,
                    "order_type": rec.order_type,
                    "order_ref": rec.order_ref,
                    "reason": rec.reason,
                    "fill_price": px,
                }
            )
            upd = OrderUpdate(
                order_id=rec.order_id,
                strategy_id=self.strategy_id,
                symbol=rec.symbol,
                state="FILLED",
                order_type=rec.order_type,
                order_ref=rec.order_ref,
                note=rec.reason,
                stop_px=rec.stop_px,
                ts=now,
            )
            await strategy.on_order_update(upd)
            filled.append(rec)
        if filled:
            filled_ids = {x.order_id for x in filled}
            self.open_orders = [o for o in self.open_orders if o.order_id not in filled_ids]


class ReplayContext:
    def __init__(self, strategy_id: str, broker: ReplayBroker) -> None:
        self.strategy_id = strategy_id
        self.broker = broker
        self._broker_adapter = _ReplayBrokerAdapter(broker)
        self.log = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None, error=lambda *a, **k: None, debug=lambda *a, **k: None)
        self.metrics = None
        self.engine = SimpleNamespace(
            market_data_service=None,
            broker_adapter=self._broker_adapter,
            decision_pipeline=SimpleNamespace(replay_stub=True),
        )
        self._now = datetime.now(ET)

    def set_now(self, now: datetime) -> None:
        self._now = now

    async def set_cadence(self, seconds: int) -> None:
        return None

    async def subscribe_features(self, symbols, bar_sec: int = 60) -> None:
        return None

    async def open_orders(self):
        out = []
        for rec in self.broker.open_orders:
            out.append(
                OrderUpdate(
                    order_id=rec.order_id,
                    strategy_id=self.strategy_id,
                    symbol=rec.symbol,
                    state=rec.state,
                    order_type=rec.order_type,
                    order_ref=rec.order_ref,
                    note=rec.reason,
                    stop_px=rec.stop_px,
                    ts=rec.submit_ts,
                )
            )
        return out

    async def position(self, symbol: str):
        p = self.broker.positions.get(symbol)
        if p is None:
            return PositionSnapshot(
                strategy_id=self.strategy_id,
                symbol=symbol,
                pos_qty=0.0,
                avg_px=0.0,
                realized_pnl_usd=0.0,
                ts=self._now,
            )
        return PositionSnapshot(
            strategy_id=self.strategy_id,
            symbol=symbol,
            pos_qty=float(p.pos_qty),
            avg_px=p.avg_px,
            realized_pnl_usd=float(p.realized_pnl_usd),
            ts=self._now,
        )

    async def submit_intent(self, envelope) -> None:
        await self.broker.submit_intent(envelope, self._now)


class _ReplayBrokerAdapter:
    """Replay-only broker adapter so strategy cleanup can cancel orphan STOP intents."""

    def __init__(self, broker: ReplayBroker) -> None:
        self._broker = broker

    async def open_orders(self):
        out = []
        for rec in self._broker.open_orders:
            if rec.state != "SUBMITTED":
                continue
            bid: Optional[int] = None
            oid = str(rec.order_id or "")
            if oid.startswith("replay_"):
                try:
                    bid = int(oid.split("_", 1)[1])
                except Exception:
                    bid = None
            out.append(
                SimpleNamespace(
                    strategy_id=rec.strategy_id,
                    symbol=rec.symbol,
                    state=rec.state,
                    order_type=rec.order_type,
                    order_ref=rec.order_ref,
                    broker_order_id=bid,
                    order_id=bid,
                    id=bid,
                )
            )
        return out

    async def cancel(self, order_id: int):
        oid = str(order_id)
        candidates = {oid, f"replay_{oid}"}
        for rec in list(self._broker.open_orders):
            if rec.order_id not in candidates:
                continue
            if rec.state != "SUBMITTED":
                return False
            rec.state = "CANCELLED"
            self._broker.lifecycle.append(
                {
                    "kind": "cancel",
                    "ts": datetime.now(ET).isoformat(),
                    "order_id": rec.order_id,
                    "symbol": rec.symbol,
                    "order_type": rec.order_type,
                    "order_ref": rec.order_ref,
                    "reason": "replay_cancel",
                }
            )
            self._broker.open_orders = [o for o in self._broker.open_orders if o.order_id != rec.order_id]
            return True
        return False


def _load_strategy_extras(config_path: Path, strategy_id: str) -> dict[str, Any]:
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    for s in (raw.get("strategies") or []):
        if str(s.get("id")) == strategy_id:
            return dict(((s.get("params") or {}).get("extras") or {}))
    raise ValueError(f"strategy id not found: {strategy_id}")


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _parse_hms(s: str, default: time) -> time:
    try:
        return datetime.strptime(str(s), "%H:%M:%S").time()
    except Exception:
        return default


def _load_bars_1m(day: str, symbol: str) -> list[Bar1m]:
    client = get_client()
    rows = client.query(
        """
        SELECT ts_start, open, high, low, close, volume
        FROM bars_1m_ibkr
        WHERE symbol = %(symbol)s
          AND (
            d = toDate(%(day)s)
            OR (d = toDate('1970-01-01') AND toDate(ts_start) = toDate(%(day)s))
          )
        ORDER BY ts_start ASC
        """,
        {"day": day, "symbol": symbol},
    )
    if not rows:
        rows = client.query(
            """
            SELECT
              toStartOfMinute(ts_start, 'America/New_York') AS ts_start,
              argMin(open, ts_start) AS open,
              max(high) AS high,
              min(low) AS low,
              argMax(close, ts_start) AS close,
              sum(volume) AS volume
            FROM bars_5s_current
            WHERE symbol = %(symbol)s
              AND toDate(ts_start, 'America/New_York') = toDate(%(day)s)
              AND ts_start >= toDateTime(concat(%(day)s, ' 09:30:00'), 'America/New_York')
              AND ts_start < toDateTime(concat(%(day)s, ' 16:00:00'), 'America/New_York')
            GROUP BY ts_start
            ORDER BY ts_start ASC
            """,
            {"day": day, "symbol": symbol},
        )
    out: list[Bar1m] = []
    for r in rows:
        ts = r.get("ts_start")
        if not isinstance(ts, datetime):
            ts = datetime.fromisoformat(str(ts))
        ts = ts.replace(tzinfo=ET) if ts.tzinfo is None else ts.astimezone(ET)
        out.append(
            Bar1m(
                ts=ts,
                open=float(r.get("open", 0.0) or 0.0),
                high=float(r.get("high", 0.0) or 0.0),
                low=float(r.get("low", 0.0) or 0.0),
                close=float(r.get("close", 0.0) or 0.0),
                volume=float(r.get("volume", 0.0) or 0.0),
            )
        )
    return [b for b in out if b.close > 0]


def _list_days_with_data(start_day: str, end_day: str, symbol: str = "QQQ") -> list[str]:
    client = get_client()
    rows = client.query(
        """
        SELECT DISTINCT if(d = toDate('1970-01-01'), toDate(ts_start), d) AS dd
        FROM bars_1m_ibkr
        WHERE symbol = %(symbol)s
          AND if(d = toDate('1970-01-01'), toDate(ts_start), d) >= toDate(%(start)s)
          AND if(d = toDate('1970-01-01'), toDate(ts_start), d) <= toDate(%(end)s)
        ORDER BY dd ASC
        """,
        {"start": start_day, "end": end_day, "symbol": symbol},
    )
    out: list[str] = []
    for r in rows:
        d = r.get("dd")
        if isinstance(d, date):
            out.append(d.isoformat())
        else:
            out.append(str(d))
    return out


def _expand_1m_to_1s(
    bars: list[Bar1m],
    *,
    model: str = "linear",
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for b in bars:
        vol_per_sec = float(b.volume) / 60.0
        if model == "linear":
            for i in range(60):
                ts = b.ts + timedelta(seconds=i)
                ratio = float(i + 1) / 60.0
                px = float(b.open + (b.close - b.open) * ratio)
                out.append({"ts": int(ts.timestamp() * 1000), "close": px, "volume": vol_per_sec})
            continue

        # Replay-only intrabar path (60s split into 3 x 20s segments):
        # open -> pivot1 -> pivot2 -> close
        if model == "ohlc_hl":
            piv1, piv2 = float(b.high), float(b.low)
        elif model == "ohlc_lh":
            piv1, piv2 = float(b.low), float(b.high)
        elif model == "adaptive":
            if float(b.close) >= float(b.open):
                piv1, piv2 = float(b.low), float(b.high)
            else:
                piv1, piv2 = float(b.high), float(b.low)
        else:
            raise ValueError(f"unknown intrabar model: {model}")

        knots = [float(b.open), piv1, piv2, float(b.close)]
        for i in range(60):
            ts = b.ts + timedelta(seconds=i)
            sec = i + 1
            if sec <= 20:
                frac = sec / 20.0
                px = knots[0] + (knots[1] - knots[0]) * frac
            elif sec <= 40:
                frac = (sec - 20) / 20.0
                px = knots[1] + (knots[2] - knots[1]) * frac
            else:
                frac = (sec - 40) / 20.0
                px = knots[2] + (knots[3] - knots[2]) * frac
            out.append({"ts": int(ts.timestamp() * 1000), "close": float(px), "volume": vol_per_sec})
    return out


def _rth_filter(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        ts = datetime.fromtimestamp(int(r["ts"]) / 1000.0, tz=ET)
        if time(9, 30) <= ts.time() <= time(16, 0):
            out.append(r)
    return out


def _load_diag(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            rows.append(json.loads(ln))
        except Exception:
            continue
    return rows


def _build_reconcile(diag_rows: list[dict[str, Any]], lifecycle: list[dict[str, Any]]) -> list[dict[str, Any]]:
    gates = [x for x in diag_rows if x.get("event") == "entry_gate"]
    entry_gates = [x for x in gates if x.get("decision") == "ENTRY_ALLOWED"]
    submits = [x for x in lifecycle if x.get("kind") == "submit" and str(x.get("order_ref", "")).endswith(":ENTRY")]
    fills_by_order = {x.get("order_id"): x for x in lifecycle if x.get("kind") == "fill"}

    out: list[dict[str, Any]] = []
    for s in submits:
        submit_ts = datetime.fromisoformat(str(s["ts"]))
        best_gate = None
        best_dt = None
        for g in entry_gates:
            gt = g.get("ts_now_et") or g.get("ts_et")
            if not gt:
                continue
            gts = datetime.fromisoformat(str(gt)) if "T" in str(gt) else datetime.strptime(str(gt), "%Y-%m-%d %H:%M:%S").replace(tzinfo=ET)
            if gts > submit_ts:
                continue
            delta = (submit_ts - gts).total_seconds()
            if delta > 300:
                continue
            if best_dt is None or delta < best_dt:
                best_dt = delta
                best_gate = g
        f = fills_by_order.get(s.get("order_id"))
        out.append(
            {
                "order_id": s.get("order_id"),
                "symbol": s.get("symbol"),
                "signal_ts": (best_gate or {}).get("ts_now_et") or (best_gate or {}).get("ts_et"),
                "decision_ts": (best_gate or {}).get("ts_now_et") or (best_gate or {}).get("ts_et"),
                "order_submit_ts": s.get("ts"),
                "fill_ts": (f or {}).get("ts"),
                "signal_price": (best_gate or {}).get("qqq_close_now"),
                "submit_price": s.get("submit_price"),
                "fill_price": (f or {}).get("fill_price"),
                "rank_15m": None,
                "dev_vwap_atr": None,
                "sign_flip_5m": None,
                "atr_rank": None,
                "move5m": None,
                "allow_reason": (best_gate or {}).get("reason"),
                "block_reason": None,
                "mode": (best_gate or {}).get("mode"),
                "direction": (best_gate or {}).get("direction"),
                "v3m_signed": (best_gate or {}).get("v3m_signed"),
                "vol_surge_ratio": (best_gate or {}).get("vol_surge_ratio"),
            }
        )
    return out


def _build_trades_from_lifecycle(day: str, lifecycle: list[dict[str, Any]]) -> list[dict[str, Any]]:
    fills = [x for x in lifecycle if x.get("kind") == "fill"]
    fills.sort(key=lambda x: str(x.get("ts", "")))
    out: list[dict[str, Any]] = []
    st: dict[str, dict[str, Any]] = {}
    for f in fills:
        sym = str(f.get("symbol", ""))
        qty = int(f.get("qty", 0) or 0)
        px = float(f.get("fill_price", 0.0) or 0.0)
        ts = str(f.get("ts", ""))
        reason = str(f.get("reason", ""))
        if qty == 0 or px <= 0:
            continue
        s = st.setdefault(sym, {"qty": 0.0, "avg": 0.0, "entry_ts": "", "entry_price": 0.0, "pnl": 0.0, "entry_order_id": "", "exit_order_id": ""})
        cur_qty = float(s["qty"])
        cur_avg = float(s["avg"])
        if qty > 0:
            new_qty = cur_qty + qty
            new_avg = ((cur_avg * cur_qty) + (px * qty)) / new_qty if cur_qty > 0 else px
            if cur_qty <= 0:
                s["entry_ts"] = ts
                s["entry_price"] = px
                s["pnl"] = 0.0
                s["entry_order_id"] = str(f.get("order_id", ""))
            s["qty"] = new_qty
            s["avg"] = new_avg
            continue
        sell_qty = abs(qty)
        if cur_qty <= 0:
            continue
        close_qty = min(cur_qty, sell_qty)
        s["pnl"] = float(s["pnl"]) + (px - cur_avg) * close_qty
        remain = cur_qty - close_qty
        s["qty"] = remain
        s["exit_order_id"] = str(f.get("order_id", ""))
        if remain <= 1e-9:
            out.append(
                {
                    "day": day,
                    "symbol": sym,
                    "entry_ts": str(s.get("entry_ts", "")),
                    "exit_ts": ts,
                    "entry_price": float(s.get("entry_price", 0.0) or 0.0),
                    "exit_price": px,
                    "pnl_usd": float(s.get("pnl", 0.0) or 0.0),
                    "exit_reason": reason,
                    "entry_order_id": str(s.get("entry_order_id", "")),
                    "exit_order_id": str(s.get("exit_order_id", "")),
                }
            )
            s["qty"] = 0.0
            s["avg"] = 0.0
            s["entry_ts"] = ""
            s["entry_price"] = 0.0
            s["pnl"] = 0.0
    return out


def _compute_mdd(daily_pnls: list[float]) -> float:
    peak = 0.0
    eq = 0.0
    mdd = 0.0
    for p in daily_pnls:
        eq += float(p)
        peak = max(peak, eq)
        mdd = min(mdd, eq - peak)
    return float(mdd)


async def run_day(args) -> dict[str, Any]:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    diag_path = out_dir / f"replay_diag_{args.day}.jsonl"
    if diag_path.exists():
        diag_path.unlink()
    semantics_audit_path = out_dir / f"replay_exit_semantics_audit_{args.day}.jsonl"
    if semantics_audit_path.exists():
        semantics_audit_path.unlink()

    extras = _load_strategy_extras(Path(args.config), args.strategy_id)
    if args.overrides_json:
        overrides = json.loads(args.overrides_json)
        if not isinstance(overrides, dict):
            raise ValueError("--overrides-json must be a JSON object")
        extras.update(overrides)
    extras["enable_observability_file"] = False
    extras["diag_log_path"] = str(diag_path)
    extras["fail_fast_enabled"] = False
    extras["submit_exchange_stop_intent"] = True
    params = StrategyParams(cadence_s=1, ttl_ms=2000, extras=extras)

    policy = ReplayPolicy.from_profile(args.profile)
    policy.entry_check_on_new_minute_only = bool(args.entry_check_on_new_minute_only)
    policy.signal_ttl_sec = max(0, int(args.signal_ttl_sec))
    policy.max_chase_atr_d = max(0.0, float(args.max_chase_atr_d))
    policy.drift_max_dev_vwap_atr_d = max(0.0, float(args.drift_max_dev_vwap_atr_d))
    policy.drift_max_dev_window_start = _parse_hms(str(args.drift_max_dev_window_start), time(9, 45, 0))
    policy.drift_max_dev_window_end = _parse_hms(str(args.drift_max_dev_window_end), time(14, 0, 0))
    policy.drift_max_dev_sign_flip_5m_min = max(0, int(args.drift_max_dev_sign_flip_5m_min))
    policy.drift_max_dev_rank_high = max(0.0, min(1.0, float(args.drift_max_dev_rank_high)))
    policy.history_ready_observe_only = bool(args.history_ready_observe_only)
    policy.history_ready_seed_fallback = bool(args.history_ready_seed_fallback)
    policy.entry_dev_vwap_max_atr_d = max(0.0, float(args.entry_dev_vwap_max_atr_d))
    policy.entry_dev_anchor_max_atr_d = max(0.0, float(args.entry_dev_anchor_max_atr_d))
    policy.logic_retreat_confirm_sec = max(0, int(args.logic_retreat_confirm_sec))
    policy.overext_pullback_reentry_enable = bool(args.overext_pullback_reentry_enable)
    policy.overext_reentry_dev_vwap_atr = max(0.0, float(args.overext_reentry_dev_vwap_atr))
    policy.overext_reentry_dev_anchor_atr = max(0.0, float(args.overext_reentry_dev_anchor_atr))
    policy.overext_mark_ttl_sec = max(1, int(args.overext_mark_ttl_sec))
    policy.momentum_decay_veto_enable = bool(args.momentum_decay_veto_enable)
    policy.momentum_decay_ratio = max(0.1, min(0.99, float(args.momentum_decay_ratio)))
    policy.momentum_decay_lookback = max(2, int(args.momentum_decay_lookback))
    broker = ReplayBroker(strategy_id=args.strategy_id, fill_delay_sec=args.fill_delay_sec, slippage_bps=args.slippage_bps, policy=policy)
    ctx = ReplayContext(args.strategy_id, broker)
    strategy_module_name = str(getattr(args, "strategy_module", "") or "strategies.qqq_intraday_strategy")
    strategy_class_name = str(getattr(args, "strategy_class", "") or "QQQIntradayStrategy")
    qmod_local = importlib.import_module(strategy_module_name)
    strategy_cls = getattr(qmod_local, strategy_class_name)
    strat = strategy_cls(ctx, params)
    strat._running = True
    strat._startup_positions_ready = True
    strat._all_day_test_mode = lambda: False  # type: ignore[method-assign]
    strat._diag_path = diag_path
    orig_append_diag = strat._append_diag_event

    def _append_diag_and_capture(event: str, payload: Dict[str, Any]) -> None:
        if event == "entry_gate":
            broker.policy.last_gate = dict(payload or {})
        orig_append_diag(event, payload)

    strat._append_diag_event = _append_diag_and_capture  # type: ignore[method-assign]

    semantics_audit_rows: list[dict[str, Any]] = []

    def _sem_state() -> dict[str, Any]:
        return {
            "symbol": getattr(strat, "_exec_symbol", None),
            "side": getattr(strat, "_side", None),
            "entry_px": qmod_local._safe_float(getattr(strat, "_entry_px", None)),
            "target_px": qmod_local._safe_float(getattr(strat, "_trade_target_px", None)),
            "stop_px": qmod_local._safe_float(getattr(strat, "_stop_px", None)),
            "tp1_hit": bool(getattr(strat, "_tp1_hit", False)),
            "trail_high_px": qmod_local._safe_float(getattr(strat, "_trail_high_px", None)),
            "trail_floor_px": qmod_local._safe_float(getattr(strat, "_trail_floor_px", None)),
            "entry_atr_used": qmod_local._safe_float(getattr(strat, "_entry_atr_used", None)),
        }

    def _audit_sem(event: str, **payload: Any) -> None:
        now_ts = getattr(ctx, "_now", datetime.now(ET))
        row = {"ts": now_ts.isoformat(), "event": event}
        row.update(_sem_state())
        row.update(payload)
        semantics_audit_rows.append(row)

    orig_submit_tp1_partial_exit = strat._submit_tp1_partial_exit

    async def _submit_tp1_partial_exit_audited(self, qty: int) -> bool:  # type: ignore[override]
        _audit_sem("tp1_submit_attempt", qty=int(qty), tp1_fill_basis="replay_market_next_tick")
        ok = await orig_submit_tp1_partial_exit(qty)
        _audit_sem("tp1_submit_result", qty=int(qty), ok=bool(ok), trail_armed_time=getattr(ctx, "_now", datetime.now(ET)).isoformat() if ok else None)
        return bool(ok)

    strat._submit_tp1_partial_exit = types.MethodType(_submit_tp1_partial_exit_audited, strat)  # type: ignore[method-assign]

    orig_flatten = strat._flatten

    async def _flatten_audited(self, reason: str) -> None:  # type: ignore[override]
        _audit_sem("flatten_submit_attempt", flatten_reason=str(reason))
        await orig_flatten(reason)
        _audit_sem("flatten_submit_done", flatten_reason=str(reason))

    strat._flatten = types.MethodType(_flatten_audited, strat)  # type: ignore[method-assign]

    # Replay-only switch:
    # - realtime: current live implementation (QQQ last price vs VWAP, second-level)
    # - minute: legacy style (QQQ 1m close vs VWAP, minute-gated)
    if str(getattr(args, "logic_retreat_mode", "realtime")).lower() == "minute":
        async def _maybe_logic_retreat_minute(self, now: datetime) -> None:  # type: ignore[override]
            if not self._exec_symbol or not self._side:
                return
            min_hold_sec = float((self.params.extras or {}).get("logic_retreat_min_hold_sec", 420.0))
            if self._entry_time_et is not None and min_hold_sec > 0:
                held_sec = (now - self._entry_time_et).total_seconds()
                if held_sec < min_hold_sec:
                    return
            minute_ts = qmod_local._floor_minute(now)
            if self._last_logic_retreat_minute == minute_ts:
                return
            self._last_logic_retreat_minute = minute_ts
            vwap = self._vwap()
            if vwap <= 0:
                return
            close_now = self._minute_close_for_gate(minute_ts, now)
            if close_now is None:
                return
            wrong_side = (self._side == "LONG" and float(close_now) < float(vwap)) or (self._side == "SHORT" and float(close_now) > float(vwap))
            if wrong_side:
                await self._flatten("logic_retreat")

        strat._maybe_logic_retreat = types.MethodType(_maybe_logic_retreat_minute, strat)  # type: ignore[method-assign]
    elif int(policy.logic_retreat_confirm_sec) > 0:
        async def _maybe_logic_retreat_realtime_confirm(self, now: datetime) -> None:  # type: ignore[override]
            if not self._exec_symbol or not self._side:
                self._logic_retreat_wrong_since = None
                return
            min_hold_sec = float((self.params.extras or {}).get("logic_retreat_min_hold_sec", 420.0))
            if self._entry_time_et is not None and min_hold_sec > 0:
                held_sec = (now - self._entry_time_et).total_seconds()
                if held_sec < min_hold_sec:
                    self._logic_retreat_wrong_since = None
                    return
            vwap = self._vwap()
            if vwap <= 0:
                self._logic_retreat_wrong_since = None
                return
            qqq_now = self._last_price("QQQ")
            if qqq_now is None:
                return
            wrong_side = (self._side == "LONG" and float(qqq_now) < float(vwap)) or (self._side == "SHORT" and float(qqq_now) > float(vwap))
            if not wrong_side:
                self._logic_retreat_wrong_since = None
                return
            since = getattr(self, "_logic_retreat_wrong_since", None)
            if since is None:
                self._logic_retreat_wrong_since = now
                return
            if (now - since).total_seconds() >= float(policy.logic_retreat_confirm_sec):
                self._logic_retreat_wrong_since = None
                await self._flatten("logic_retreat")

        strat._logic_retreat_wrong_since = None
        strat._maybe_logic_retreat = types.MethodType(_maybe_logic_retreat_realtime_confirm, strat)  # type: ignore[method-assign]

    qqq_daily_atr_path = extras.get("qqq_daily_atr_path")
    if qqq_daily_atr_path:
        strat._qqq_daily_atr_path = Path(str(qqq_daily_atr_path)).expanduser()
    atr_seed_path = extras.get("atr_seed_path")
    if atr_seed_path:
        strat._atr_seed_path = Path(str(atr_seed_path)).expanduser()

    clock = ReplayClock()
    real_datetime = qmod_local.datetime
    ReplayDateTime.bind_clock(clock)
    qmod_local.datetime = ReplayDateTime  # type: ignore[assignment]

    try:
        qqq_1m = _load_bars_1m(args.day, "QQQ")
        tqqq_1m = _load_bars_1m(args.day, "TQQQ")
        sqqq_1m = _load_bars_1m(args.day, "SQQQ")
        if not qqq_1m or not tqqq_1m or not sqqq_1m:
            raise RuntimeError(f"missing bars for {args.day}: qqq={len(qqq_1m)} tqqq={len(tqqq_1m)} sqqq={len(sqqq_1m)}")

        replay_exit_mode = str(getattr(args, "replay_exit_semantics", "default")).lower()
        if replay_exit_mode in {"ohlc", "phased"}:
            exec_bar_map: dict[str, dict[datetime, Bar1m]] = {"TQQQ": {}, "SQQQ": {}}
            for b in tqqq_1m:
                exec_bar_map["TQQQ"][b.ts.replace(second=0, microsecond=0)] = b
            for b in sqqq_1m:
                exec_bar_map["SQQQ"][b.ts.replace(second=0, microsecond=0)] = b

            async def _manage_position_ohlc_exit(self, now: datetime) -> None:  # type: ignore[override]
                if not self._is_traded or not self._exec_symbol or not self._side or self._entry_px is None:
                    return
                if (not self._all_day_test_mode()) and (not self._is_regular_session(now)):
                    return

                live_qty_signed = float(await self._current_exec_qty())
                if abs(live_qty_signed) <= 1e-6:
                    await self._sync_position_state(force=True)
                    return

                minute_ts = now.replace(second=0, microsecond=0)
                bar = exec_bar_map.get(self._exec_symbol, {}).get(minute_ts)
                if bar is None:
                    return
                bar_low = float(bar.low)
                bar_high = float(bar.high)
                if bar_low <= 0 or bar_high <= 0:
                    return

                # 1) Hard stop (pre-TP1): risk-first on intrabar low
                hard_stop_delay_min = float((self.params.extras or {}).get("hard_stop_delay_min", 3.0))
                enable_hard_stop = bool((self.params.extras or {}).get("enable_hard_stop", True))
                if enable_hard_stop and (not self._tp1_hit) and self._entry_time_et is not None:
                    held_min = (now - self._entry_time_et).total_seconds() / 60.0
                    if held_min >= max(0.0, hard_stop_delay_min):
                        if self._stop_px is not None and float(self._stop_px) > 0 and bar_low <= float(self._stop_px):
                            await self._flatten("stop_hit")
                            return

                # 2) Optional exchange-stop line check (also pre-TP1)
                if (not self._tp1_hit) and bool((self.params.extras or {}).get("enable_exchange_stop_check", False)):
                    if self._stop_px is not None and bar_low <= float(self._stop_px):
                        await self._flatten("stop_hit")
                        return

                # 3) TP1 + trailing with intrabar sequence low -> high -> low
                target_px = qmod_local._safe_float(self._trade_target_px)
                if target_px is not None and target_px > 0:
                    if not self._tp1_hit:
                        if bar_high >= float(target_px):
                            live_qty = int(max(0.0, await self._current_exec_qty()))
                            if live_qty <= 1:
                                await self._flatten("target_hit")
                                return
                            tp1_qty = max(1, int(math.floor(live_qty * 0.5)))
                            if tp1_qty >= live_qty:
                                tp1_qty = max(1, live_qty - 1)
                            submitted = await self._submit_tp1_partial_exit(tp1_qty)
                            if submitted:
                                atr_entry = float(self._entry_atr_used or 0.0)
                                self._tp1_hit = True
                                self._trail_high_px = float(bar_high)
                                self._replay_tp1_hit_bar_ts = minute_ts
                                if atr_entry > 0:
                                    self._trail_floor_px = round(float(self._entry_px) + (0.2 * atr_entry), 2)
                                self._persist_runtime_state(reason="tp1_hit")
                            return
                    else:
                        atr_entry = float(self._entry_atr_used or 0.0)
                        if atr_entry > 0:
                            self._trail_high_px = max(float(self._trail_high_px or bar_high), float(bar_high))
                            trail_line = float(self._trail_high_px) - (0.5 * atr_entry)
                            floor_line = float(self._trail_floor_px or (float(self._entry_px) + (0.2 * atr_entry)))
                            effective_stop = max(floor_line, trail_line)
                            # For the same bar where TP1 is hit, avoid reusing pre-TP1 global low.
                            # Approximate post-TP1 check using close as the post-high segment proxy.
                            tp1_bar_ts = getattr(self, "_replay_tp1_hit_bar_ts", None)
                            post_tp1_low = float(bar.close) if tp1_bar_ts == minute_ts else float(bar_low)
                            if post_tp1_low <= effective_stop:
                                await self._flatten("tp1_then_trailing_stop")
                                return
                        if now.time() >= qmod_local.EOD_FLATTEN_TIME:
                            await self._flatten("tp1_then_eod")
                            return

                # Keep remaining management behavior unchanged (breakeven / widen / retreat / add)
                if not self._breakeven_done:
                    last_px = self._last_price(self._exec_symbol)
                    if last_px and last_px > 0:
                        unrealized = float(last_px) - float(self._entry_px)
                        if bool((self.params.extras or {}).get("enable_breakeven_shield", False)) and unrealized >= 1.0:
                            new_stop = round(float(self._entry_px) + 0.20, 2)
                            if new_stop < float(last_px) - 0.05:
                                self._stop_px = new_stop
                                self._breakeven_done = True
                                logger.info("qqq_breakeven_shield_armed", strategy_id=self.strategy_id, stop_px=new_stop, price=last_px)
                                self._persist_runtime_state(reason="breakeven_shield")
                                await self._submit_protective_stop_intent("breakeven_shield")
                                return

                if not self._breakeven_done:
                    if bool((self.params.extras or {}).get("enable_dynamic_stop_widen", False)):
                        await self._maybe_update_stop_by_atr(now)

                if not self._tp1_hit:
                    await self._maybe_logic_retreat(now)
                await self._maybe_add_position(now=now)

            async def _manage_position_phased_exit(self, now: datetime) -> None:  # type: ignore[override]
                if not self._is_traded or not self._exec_symbol or not self._side or self._entry_px is None:
                    return
                if (not self._all_day_test_mode()) and (not self._is_regular_session(now)):
                    return

                live_qty_signed = float(await self._current_exec_qty())
                if abs(live_qty_signed) <= 1e-6:
                    await self._sync_position_state(force=True)
                    return

                curr_px = self._last_price(self._exec_symbol)
                if curr_px is None or float(curr_px) <= 0:
                    return
                curr_px_f = float(curr_px)
                prev_px = float(getattr(self, "_replay_phase_prev_px", curr_px_f))
                self._replay_phase_prev_px = curr_px_f
                if prev_px <= 0:
                    return

                def _hit_tau_up(a: float, b: float, lv: float) -> Optional[float]:
                    if b <= a:
                        return None
                    if not (a <= lv <= b):
                        return None
                    span = b - a
                    if span <= 1e-9:
                        return None
                    return (lv - a) / span

                def _hit_tau_down(a: float, b: float, lv: float) -> Optional[float]:
                    if b >= a:
                        return None
                    if not (b <= lv <= a):
                        return None
                    span = a - b
                    if span <= 1e-9:
                        return None
                    return (a - lv) / span

                # 1) Pre-TP1 hard stop: earliest-hit on current 1s segment.
                hard_stop_delay_min = float((self.params.extras or {}).get("hard_stop_delay_min", 3.0))
                enable_hard_stop = bool((self.params.extras or {}).get("enable_hard_stop", True))
                allow_stop = False
                if enable_hard_stop and (not self._tp1_hit) and self._entry_time_et is not None:
                    held_min = (now - self._entry_time_et).total_seconds() / 60.0
                    allow_stop = held_min >= max(0.0, hard_stop_delay_min)

                stop_px = float(self._stop_px) if (self._stop_px is not None and float(self._stop_px) > 0) else None
                target_px = qmod_local._safe_float(self._trade_target_px)

                # 2) Stateful multi-event intrabar simulation on current 1s segment.
                # Sequence: find earliest event -> update state -> continue on remaining segment.
                segment_start = 0.0
                segment_eps = 1e-6
                exchange_stop_check = bool((self.params.extras or {}).get("enable_exchange_stop_check", False))

                while segment_start < (1.0 - segment_eps):
                    px_a = prev_px + (curr_px_f - prev_px) * segment_start
                    px_b = curr_px_f
                    if abs(px_b - px_a) <= 1e-9:
                        break

                    candidates: list[tuple[float, int, str, float]] = []

                    if not self._tp1_hit:
                        stop_active = stop_px is not None and (allow_stop or exchange_stop_check)
                        if stop_active and stop_px is not None:
                            tau = _hit_tau_down(px_a, px_b, stop_px)
                            if tau is not None:
                                t_abs = segment_start + tau * (1.0 - segment_start)
                                candidates.append((t_abs, 0, "stop_hit", float(stop_px)))
                        if target_px is not None and target_px > 0:
                            tau = _hit_tau_up(px_a, px_b, float(target_px))
                            if tau is not None:
                                t_abs = segment_start + tau * (1.0 - segment_start)
                                candidates.append((t_abs, 1, "tp1_hit", float(target_px)))
                    else:
                        atr_entry = float(self._entry_atr_used or 0.0)
                        armed_at = getattr(self, "_replay_trail_armed_at", None)
                        if armed_at is not None and now < armed_at:
                            break
                        if atr_entry > 0:
                            self._trail_high_px = max(float(self._trail_high_px or px_a), px_a)
                            trail_line = float(self._trail_high_px) - (0.5 * atr_entry)
                            floor_line = float(self._trail_floor_px or (float(self._entry_px) + (0.2 * atr_entry)))
                            effective_stop = max(floor_line, trail_line)
                            tau = _hit_tau_down(px_a, px_b, effective_stop)
                            if tau is not None:
                                t_abs = segment_start + tau * (1.0 - segment_start)
                                candidates.append((t_abs, 2, "tp1_then_trailing_stop", float(effective_stop)))

                    if not candidates:
                        if self._tp1_hit:
                            self._trail_high_px = max(float(self._trail_high_px or px_a), px_a, px_b)
                        break

                    candidates.sort(key=lambda x: (x[0], x[1]))
                    event_t, _, event_kind, event_px = candidates[0]
                    _audit_sem(
                        "intrabar_event_selected",
                        event_kind=event_kind,
                        event_t=float(event_t),
                        event_px=float(event_px),
                        segment_start=float(segment_start),
                        px_a=float(px_a),
                        px_b=float(px_b),
                    )

                    if event_kind == "stop_hit":
                        await self._flatten("stop_hit")
                        return

                    if event_kind == "tp1_hit":
                        live_qty = int(max(0.0, await self._current_exec_qty()))
                        if live_qty <= 1:
                            await self._flatten("target_hit")
                            return
                        tp1_qty = max(1, int(math.floor(live_qty * 0.5)))
                        if tp1_qty >= live_qty:
                            tp1_qty = max(1, live_qty - 1)
                        submitted = await self._submit_tp1_partial_exit(tp1_qty)
                        if submitted:
                            atr_entry = float(self._entry_atr_used or 0.0)
                            self._tp1_hit = True
                            anchor_mode = str((self.params.extras or {}).get("replay_tp1_anchor_mode", "extreme")).lower()
                            if anchor_mode == "tp1_fill":
                                # Replay-only: anchor trailing from TP1 hit/submit price semantics.
                                self._trail_high_px = float(event_px)
                            else:
                                self._trail_high_px = max(float(self._trail_high_px or px_a), px_a, event_px)
                            if atr_entry > 0:
                                self._trail_floor_px = round(float(self._entry_px) + (0.2 * atr_entry), 2)
                            arm_delay_sec = max(0.0, float((self.params.extras or {}).get("replay_trail_arm_delay_sec", 0.0)))
                            self._replay_trail_armed_at = now + timedelta(seconds=arm_delay_sec)
                            self._persist_runtime_state(reason="tp1_hit")
                        segment_start = min(1.0, event_t + segment_eps)
                        continue

                    if event_kind == "tp1_then_trailing_stop":
                        await self._flatten("tp1_then_trailing_stop")
                        return

                if self._tp1_hit and now.time() >= qmod_local.EOD_FLATTEN_TIME:
                    _audit_sem("tp1_eod_flatten_check", eod_time=str(now.time()))
                    await self._flatten("tp1_then_eod")
                    return

                # Keep other behavior unchanged.
                if not self._tp1_hit:
                    # Replay-only pre-TP1 retreat gating for ablation:
                    # - normal: keep existing behavior
                    # - stop_priority: once hard stop is armed, suppress logic_retreat and let stop dominate
                    # - disabled: suppress all pre-TP1 logic_retreat
                    retreat_mode = str((self.params.extras or {}).get("replay_pre_tp1_logic_retreat_mode", "normal")).lower()
                    allow_retreat = True
                    if retreat_mode == "disabled":
                        allow_retreat = False
                    elif retreat_mode == "stop_priority":
                        hard_stop_delay_min = float((self.params.extras or {}).get("hard_stop_delay_min", 3.0))
                        enable_hard_stop = bool((self.params.extras or {}).get("enable_hard_stop", True))
                        stop_valid = self._stop_px is not None and float(self._stop_px) > 0
                        if enable_hard_stop and stop_valid and self._entry_time_et is not None:
                            held_min = (now - self._entry_time_et).total_seconds() / 60.0
                            if held_min >= max(0.0, hard_stop_delay_min):
                                allow_retreat = False
                    if allow_retreat:
                        await self._maybe_logic_retreat(now)
                await self._maybe_add_position(now=now)

            if replay_exit_mode == "ohlc":
                strat._manage_position = types.MethodType(_manage_position_ohlc_exit, strat)  # type: ignore[method-assign]
            else:
                strat._manage_position = types.MethodType(_manage_position_phased_exit, strat)  # type: ignore[method-assign]

        qqq_1s = _rth_filter(_expand_1m_to_1s(qqq_1m, model=str(args.intrabar_model)))
        tqqq_1s = _rth_filter(_expand_1m_to_1s(tqqq_1m, model=str(args.intrabar_model)))
        sqqq_1s = _rth_filter(_expand_1m_to_1s(sqqq_1m, model=str(args.intrabar_model)))
        n = min(len(qqq_1s), len(tqqq_1s), len(sqqq_1s))
        qqq_1s = qqq_1s[:n]
        tqqq_1s = tqqq_1s[:n]
        sqqq_1s = sqqq_1s[:n]

        for i in range(n):
            ts = datetime.fromtimestamp(int(qqq_1s[i]["ts"]) / 1000.0, tz=ET)
            clock.set(ts)
            ctx.set_now(ts)
            strat._daily_reset_if_needed(ts)

            await strat._ingest_1s("QQQ", qqq_1s[i], now_hint=ts)
            await strat._ingest_1s("TQQQ", tqqq_1s[i], now_hint=ts)
            await strat._ingest_1s("SQQQ", sqqq_1s[i], now_hint=ts)
            broker.update_price("QQQ", float(qqq_1s[i]["close"]))
            broker.update_price("TQQQ", float(tqqq_1s[i]["close"]))
            broker.update_price("SQQQ", float(sqqq_1s[i]["close"]))

            await broker.process_orders(ts, strat)
            strat._maybe_lock_seed(ts)
            strat._refresh_atr_pre_by_schedule(ts)
            strat._update_crosscount(ts)
            await strat._lock_amp_params(ts)
            if (not policy.entry_check_on_new_minute_only) or (ts.second == 0):
                await strat._maybe_enter(ts)
            await strat._manage_position(ts)
            await strat._force_flatten_1559(ts)
            await strat._sync_position_state(force=True)
            await broker.process_orders(ts, strat)

        diag_rows = _load_diag(diag_path)
        reconcile_rows = _build_reconcile(diag_rows, broker.lifecycle)

        recon_path = out_dir / f"replay_reconcile_{args.day}.csv"
        with recon_path.open("w", encoding="utf-8") as f:
            headers = [
                "order_id", "symbol", "signal_ts", "decision_ts", "order_submit_ts", "fill_ts",
                "signal_price", "submit_price", "fill_price",
                "rank_15m", "dev_vwap_atr", "sign_flip_5m", "atr_rank", "move5m",
                "allow_reason", "block_reason", "mode", "direction", "v3m_signed", "vol_surge_ratio",
            ]
            f.write(",".join(headers) + "\n")
            for r in reconcile_rows:
                vals = [str(r.get(h, "")) for h in headers]
                f.write(",".join(vals) + "\n")

        lifecycle_path = out_dir / f"replay_lifecycle_{args.day}.jsonl"
        with lifecycle_path.open("w", encoding="utf-8") as f:
            for row in broker.lifecycle:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        with semantics_audit_path.open("w", encoding="utf-8") as f:
            for row in semantics_audit_rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

        trades = _build_trades_from_lifecycle(args.day, broker.lifecycle)
        daily_pnl = float(sum(float(x.get("pnl_usd", 0.0) or 0.0) for x in trades))
        wins = sum(1 for x in trades if float(x.get("pnl_usd", 0.0) or 0.0) > 0)
        losses = sum(1 for x in trades if float(x.get("pnl_usd", 0.0) or 0.0) < 0)
        summary = {
            "day": args.day,
            "diag_path": str(diag_path),
            "reconcile_csv": str(recon_path),
            "lifecycle_jsonl": str(lifecycle_path),
            "exit_semantics_audit_jsonl": str(semantics_audit_path),
            "entry_gate_total": sum(1 for x in diag_rows if x.get("event") == "entry_gate"),
            "entry_allowed": sum(1 for x in diag_rows if x.get("event") == "entry_gate" and x.get("decision") == "ENTRY_ALLOWED"),
            "orders_submitted": sum(1 for x in broker.lifecycle if x.get("kind") == "submit"),
            "orders_filled": sum(1 for x in broker.lifecycle if x.get("kind") == "fill"),
            "orders_blocked": sum(1 for x in broker.lifecycle if x.get("kind") == "blocked"),
            "stop_hit_count": int(broker.policy.stop_hit_count),
            "trades": len(trades),
            "wins": wins,
            "losses": losses,
            "daily_pnl_usd": daily_pnl,
            "exit_reasons": {
                k: sum(1 for x in trades if str(x.get("exit_reason", "")) == k)
                for k in sorted({str(x.get("exit_reason", "")) for x in trades if str(x.get("exit_reason", ""))})
            },
        }
        (out_dir / f"replay_summary_{args.day}.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        (out_dir / f"replay_trades_{args.day}.json").write_text(json.dumps(trades, ensure_ascii=False, indent=2), encoding="utf-8")
        return summary
    finally:
        qmod_local.datetime = real_datetime  # type: ignore[assignment]


async def main():
    ap = argparse.ArgumentParser(description="Replay QQQ live strategy with simulated execution lifecycle.")
    ap.add_argument("--day", default="", help="ET day, e.g. 2026-02-27")
    ap.add_argument("--start-day", default="", help="ET start day for range run")
    ap.add_argument("--end-day", default="", help="ET end day for range run")
    ap.add_argument("--strategy-id", default="qqq_intraday_001")
    ap.add_argument("--strategy-module", default="strategies.qqq_intraday_strategy")
    ap.add_argument("--strategy-class", default="QQQIntradayStrategy")
    ap.add_argument("--config", default="config.yml")
    ap.add_argument("--fill-delay-sec", type=int, default=1)
    ap.add_argument("--slippage-bps", type=float, default=1.0)
    ap.add_argument("--profile", default="baseline", choices=["baseline", "patch_0227"])
    ap.add_argument("--entry-check-on-new-minute-only", action="store_true")
    ap.add_argument("--signal-ttl-sec", type=int, default=0)
    ap.add_argument("--max-chase-atr-d", type=float, default=0.0)
    ap.add_argument("--drift-max-dev-vwap-atr-d", type=float, default=0.0)
    ap.add_argument("--drift-max-dev-window-start", default="09:45:00")
    ap.add_argument("--drift-max-dev-window-end", default="14:00:00")
    ap.add_argument("--drift-max-dev-sign-flip-5m-min", type=int, default=0)
    ap.add_argument("--drift-max-dev-rank-high", type=float, default=0.0)
    ap.add_argument("--history-ready-observe-only", action="store_true")
    ap.add_argument("--history-ready-seed-fallback", action="store_true")
    ap.add_argument("--entry-dev-vwap-max-atr-d", type=float, default=0.0)
    ap.add_argument("--entry-dev-anchor-max-atr-d", type=float, default=0.0)
    ap.add_argument("--logic-retreat-confirm-sec", type=int, default=0)
    ap.add_argument("--overext-pullback-reentry-enable", action="store_true")
    ap.add_argument("--overext-reentry-dev-vwap-atr", type=float, default=0.0)
    ap.add_argument("--overext-reentry-dev-anchor-atr", type=float, default=0.0)
    ap.add_argument("--overext-mark-ttl-sec", type=int, default=180)
    ap.add_argument("--momentum-decay-veto-enable", action="store_true")
    ap.add_argument("--momentum-decay-ratio", type=float, default=0.85)
    ap.add_argument("--momentum-decay-lookback", type=int, default=3)
    ap.add_argument("--logic-retreat-mode", default="realtime", choices=["realtime", "minute"])
    ap.add_argument(
        "--intrabar-model",
        default="linear",
        choices=["linear", "ohlc_hl", "ohlc_lh", "adaptive"],
        help="Replay-only 1m->1s expansion model.",
    )
    ap.add_argument(
        "--replay-exit-semantics",
        default="default",
        choices=["default", "ohlc", "phased"],
        help="Replay-only exit semantics switch. 'ohlc' enables bar-extrema-aware checks; 'phased' uses phase-ordered intrabar path checks.",
    )
    ap.add_argument("--overrides-json", default="", help="JSON object to override strategy extras for replay only")
    ap.add_argument("--out-dir", default="runtime/state")
    args = ap.parse_args()

    if args.day:
        _ = _parse_date(args.day)  # validate
        summary = await run_day(args)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return
    if not args.start_day or not args.end_day:
        raise ValueError("provide either --day OR --start-day + --end-day")
    _ = _parse_date(args.start_day)
    _ = _parse_date(args.end_day)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    days = _list_days_with_data(args.start_day, args.end_day, symbol="QQQ")
    if not days:
        raise RuntimeError(f"no QQQ data days between {args.start_day} and {args.end_day}")

    all_summaries: list[dict[str, Any]] = []
    all_trades: list[dict[str, Any]] = []
    for d in days:
        args.day = d
        day_dir = out_dir / args.profile / d
        args.out_dir = str(day_dir)
        s = await run_day(args)
        all_summaries.append(s)
        tpath = day_dir / f"replay_trades_{d}.json"
        if tpath.exists():
            try:
                rows = json.loads(tpath.read_text(encoding="utf-8"))
                if isinstance(rows, list):
                    all_trades.extend(rows)
            except Exception:
                pass

    daily_rows = sorted(all_summaries, key=lambda x: str(x.get("day", "")))
    daily_pnls = [float(x.get("daily_pnl_usd", 0.0) or 0.0) for x in daily_rows]
    total_pnl = float(sum(daily_pnls))
    total_trades = int(sum(int(x.get("trades", 0) or 0) for x in daily_rows))
    total_wins = int(sum(int(x.get("wins", 0) or 0) for x in daily_rows))
    total_losses = int(sum(int(x.get("losses", 0) or 0) for x in daily_rows))
    win_rate = (float(total_wins) / float(total_wins + total_losses)) if (total_wins + total_losses) > 0 else 0.0
    mdd = _compute_mdd(daily_pnls)

    extras = _load_strategy_extras(Path(args.config), args.strategy_id)
    init_cap = float(extras.get("notional_usd", 10000.0) or 10000.0)
    total_days = len(days)
    years = max(1e-9, float(total_days) / 252.0)
    ann_return = (pow(max(1e-9, 1.0 + (total_pnl / init_cap)), 1.0 / years) - 1.0) if init_cap > 0 else 0.0

    exit_reason_counts: dict[str, int] = {}
    for tr in all_trades:
        k = str(tr.get("exit_reason", ""))
        if not k:
            continue
        exit_reason_counts[k] = exit_reason_counts.get(k, 0) + 1

    report = {
        "profile": args.profile,
        "range_start": args.start_day,
        "range_end": args.end_day,
        "trading_days_with_data": total_days,
        "trade_days": sum(1 for x in daily_rows if int(x.get("trades", 0) or 0) > 0),
        "total_trades": total_trades,
        "wins": total_wins,
        "losses": total_losses,
        "win_rate": win_rate,
        "total_pnl_usd": total_pnl,
        "return_on_notional": (total_pnl / init_cap) if init_cap > 0 else 0.0,
        "annualized_return": ann_return,
        "mdd_usd": mdd,
        "exit_reason_counts": exit_reason_counts,
    }

    (out_dir / f"replay_report_{args.profile}_{args.start_day}_{args.end_day}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / f"replay_daily_{args.profile}_{args.start_day}_{args.end_day}.json").write_text(
        json.dumps(daily_rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / f"replay_trades_{args.profile}_{args.start_day}_{args.end_day}.json").write_text(
        json.dumps(all_trades, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
