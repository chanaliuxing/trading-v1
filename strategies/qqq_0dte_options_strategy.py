# -*- coding: utf-8 -*-
"""
QQQ 0DTE Options Strategy (Backtest-first)
- Reuses QQQ signal layer concepts (QQQ + VWAP + DRIFT + gates)
- Trend filter (RVOL/ATR expansion)
- Entry variants: confirm or confirm+expansion
- Executes via synthetic 0DTE option pricing for backtests
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Any, Dict, Optional, Deque, List, Tuple
from collections import deque
from zoneinfo import ZoneInfo
import math

from libs.strategy import StrategyAlgorithm

ET = ZoneInfo("America/New_York")


@dataclass
class OptionPosition:
    side: str  # "CALL" or "PUT"
    qty: int
    entry_premium: float
    entry_underlying: float
    entry_time: datetime
    strike: float
    entry_iv: Optional[float] = None
    tp1_done: bool = False
    runner_qty: int = 0


class QQQ0DTEOptionsStrategy(StrategyAlgorithm):
    async def initialize(self) -> None:
        extras = self.params.extras or {}
        self.symbol = str(extras.get("symbol", "QQQ")).upper()
        self._bars: Deque[Dict[str, Any]] = deque(maxlen=600)
        self._trs: Deque[float] = deque(maxlen=200)
        self._vols: Deque[float] = deque(maxlen=200)
        self._position: Optional[OptionPosition] = None
        self._trades: List[Dict[str, Any]] = []
        self._day: Optional[str] = None
        self._trades_today: int = 0
        self._last_eval_minute: Optional[datetime] = None
        self._pending_breakout: Optional[Dict[str, Any]] = None
        self._first_trade_exit_reason: Optional[str] = None
        self._option_quotes_cache: Optional[Dict[Tuple[datetime, str], List[Dict[str, Any]]]] = None
        self._iv_hist_call: Deque[Tuple[datetime, float]] = deque(maxlen=240)
        self._iv_hist_put: Deque[Tuple[datetime, float]] = deque(maxlen=240)

    async def on_features(self, symbol: str, features: Dict[str, Any]) -> None:
        if symbol.upper() != self.symbol:
            return

        ts = features.get("ts")
        if hasattr(ts, "to_pydatetime"):
            now = ts.to_pydatetime().astimezone(ET)
        elif isinstance(ts, datetime):
            now = ts.astimezone(ET)
        else:
            now = datetime.fromtimestamp(int(ts) / 1000.0, tz=ET)

        self._daily_reset_if_needed(now)

        bar = {
            "ts": now,
            "open": float(features.get("open", 0.0) or 0.0),
            "high": float(features.get("high", 0.0) or 0.0),
            "low": float(features.get("low", 0.0) or 0.0),
            "close": float(features.get("close", 0.0) or 0.0),
            "vwap": float(features.get("vwap", 0.0) or 0.0),
            "volume": float(features.get("volume", 0.0) or 0.0),
            "atr_1m": float(features.get("atr_14", 0.0) or 0.0),
            "atr_d": float(features.get("atr_d", 0.0) or 0.0),
        }

        prev_close = self._bars[-1]["close"] if self._bars else bar["close"]
        tr = max(
            abs(bar["high"] - bar["low"]),
            abs(bar["high"] - prev_close),
            abs(bar["low"] - prev_close),
        )
        if math.isfinite(tr):
            self._trs.append(float(tr))
        if math.isfinite(bar["volume"]):
            self._vols.append(float(bar["volume"]))

        atr_1m = None
        if len(self._trs) >= 14:
            atr_1m = sum(list(self._trs)[-14:]) / 14.0
        bar["atr_1m"] = float(atr_1m) if atr_1m is not None else bar["atr_1m"]

        vol_window = int((self.params.extras or {}).get("rvol_window", 20))
        vol_ma = None
        if len(self._vols) >= max(5, min(vol_window, len(self._vols))):
            window = list(self._vols)[-vol_window:]
            vol_ma = sum(window) / max(1, len(window))
        bar["vol_ma"] = float(vol_ma) if vol_ma is not None else None
        bar["rvol"] = float(bar["volume"]) / float(vol_ma) if vol_ma and vol_ma > 0 else None

        atr_ma_window = int((self.params.extras or {}).get("atr_expansion_window", 20))
        atr_ma = None
        if len(self._trs) >= max(5, atr_ma_window):
            atr_series = list(self._trs)[-atr_ma_window:]
            atr_ma = sum(atr_series) / max(1, len(atr_series))
        bar["atr_1m_ma"] = float(atr_ma) if atr_ma is not None else None

        self._bars.append(bar)
        self._update_iv_history(now, bar)

        if self._position is not None:
            self._manage_position(now, bar)
            return

        minute_ts = now.replace(second=0, microsecond=0)
        if self._last_eval_minute == minute_ts:
            return
        self._last_eval_minute = minute_ts

        self._maybe_enter(now, bar)

    def _daily_reset_if_needed(self, now: datetime) -> None:
        day = now.strftime("%Y-%m-%d")
        if self._day != day:
            self._day = day
            self._trades_today = 0
            self._position = None
            self._last_eval_minute = None
            self._pending_breakout = None
            self._first_trade_exit_reason = None
            self._trs.clear()
            self._vols.clear()
            self._iv_hist_call.clear()
            self._iv_hist_put.clear()

    # ---------------------- Signal Layer ----------------------
    def _maybe_enter(self, now: datetime, bar: Dict[str, Any]) -> None:
        extras = self.params.extras or {}
        confirm_bar_close_loc_min = float(extras.get("confirm_bar_close_loc_min", 0.55))

        gate_start = _parse_hms(extras.get("ab_time_start", "09:30:00"), time(9, 30))
        gate_end = _parse_hms(extras.get("ab_time_end", "14:00:00"), time(14, 0))
        if now.time() < gate_start or now.time() > gate_end:
            self._pending_breakout = None
            return
        no_new_after = _parse_hms(extras.get("no_new_entry_after", "23:59:59"), time(23, 59, 59))
        if now.time() >= no_new_after:
            self._pending_breakout = None
            return
        opt_start = _parse_hms(extras.get("opt_time_start", "09:45:00"), time(9, 45))
        opt_end = _parse_hms(extras.get("opt_time_end", "11:00:00"), time(11, 0))
        if now.time() < opt_start or now.time() > opt_end:
            self._pending_breakout = None
            return

        max_trades = int(extras.get("max_trades_per_day", 1))
        if self._trades_today >= max_trades:
            self._pending_breakout = None
            return
        reserve_after_raw = extras.get("reserve_trade_after_time")
        reserve_count = int(extras.get("reserve_trade_count", 0))
        if reserve_after_raw and reserve_count > 0 and max_trades > reserve_count:
            reserve_after = _parse_hms(reserve_after_raw, time(10, 45))
            early_cap = max_trades - reserve_count
            if now.time() < reserve_after and self._trades_today >= early_cap:
                self._pending_breakout = None
                return
        allowed_second = extras.get("second_trade_allowed_first_exit_reasons")
        if allowed_second is not None and self._trades_today >= 1:
            guard_bear_only = bool(extras.get("second_trade_guard_bear_only", False))
            if guard_bear_only and (not self._is_bear_dominant(bar, extras)):
                allowed_second = None
            if allowed_second is None:
                pass
            else:
                allowed_set = set()
                if isinstance(allowed_second, str):
                    allowed_set = {x.strip() for x in allowed_second.split(",") if x.strip()}
                elif isinstance(allowed_second, (list, tuple, set)):
                    allowed_set = {str(x).strip() for x in allowed_second if str(x).strip()}
                if allowed_set and self._first_trade_exit_reason not in allowed_set:
                    self._pending_breakout = None
                    return
        if bool(extras.get("second_trade_block_if_first_tp2", False)):
            if self._trades_today >= 1 and self._first_trade_exit_reason == "TP2":
                self._pending_breakout = None
                return
        if bool(extras.get("second_trade_block_if_first_hard_stop", False)):
            if self._trades_today >= 1 and self._first_trade_exit_reason == "HARD_STOP":
                self._pending_breakout = None
                return
        block_after_rp_until_raw = extras.get("second_trade_block_after_runnerprotect_until")
        if self._trades_today >= 1 and self._first_trade_exit_reason == "RUNNER_PROTECT" and block_after_rp_until_raw:
            block_until = _parse_hms(block_after_rp_until_raw, time(12, 0))
            if now.time() <= block_until:
                self._pending_breakout = None
                return

        if len(self._bars) < 6:
            return

        close = bar["close"]
        vwap = bar["vwap"]
        if close <= 0 or vwap <= 0:
            return

        trend_ok = self._trend_filter_ok(bar, extras)

        entry_variant = str(extras.get("entry_variant", "confirm")).lower().strip()

        if self._pending_breakout is not None:
            self._pending_breakout["bars_waited"] += 1
            stage = self._pending_breakout.get("stage", "await_confirm")
            direction = self._pending_breakout["direction"]
            level = self._pending_breakout["level"]

            if stage == "await_confirm":
                if direction == "LONG" and close > level:
                    confirm_loc_min = self._effective_confirm_loc_min(
                        direction="LONG",
                        bar=bar,
                        extras=extras,
                        trend_ok=trend_ok,
                        base=confirm_bar_close_loc_min,
                    )
                    rng = float(bar["high"] - bar["low"])
                    close_loc_ok = True
                    if self._should_enforce_close_loc(now, extras) and rng > 0:
                        close_loc_ok = ((float(bar["close"]) - float(bar["low"])) / rng) >= confirm_loc_min
                    if not close_loc_ok:
                        self._pending_breakout = None
                        return
                    confirm_bar_max_range_atr = float(extras.get("confirm_bar_max_range_atr", 0.0))
                    confirm_bar_min_body_ratio = float(extras.get("confirm_bar_min_body_ratio", 0.0))
                    atr_1m_confirm = float(bar.get("atr_1m") or 0.0)
                    if atr_1m_confirm <= 0:
                        atr_d_confirm = self._resolve_atr_d(bar, extras)
                        atr_1m_confirm = atr_d_confirm / max(1.0, float(extras.get("atr_d_mult_from_atr_1m", 40.0)))
                    if confirm_bar_max_range_atr > 0 and atr_1m_confirm > 0:
                        if (rng / atr_1m_confirm) > confirm_bar_max_range_atr:
                            self._pending_breakout = None
                            return
                    if confirm_bar_min_body_ratio > 0 and rng > 0:
                        body = abs(float(bar["close"]) - float(bar["open"]))
                        if (body / rng) < confirm_bar_min_body_ratio:
                            self._pending_breakout = None
                            return
                    if entry_variant == "confirm":
                        if trend_ok:
                            self._enter_with_gates(now, bar, direction, level)
                        self._pending_breakout = None
                        return
                    if entry_variant == "confirm_pullback":
                        self._pending_breakout = {
                            "stage": "await_pullback",
                            "direction": direction,
                            "level": level,
                            "bars_waited": 0,
                            "pullback_seen": False,
                        }
                        return
                    if entry_variant == "confirm_dualpath":
                        self._pending_breakout = {
                            "stage": "await_dualpath",
                            "direction": direction,
                            "level": level,
                            "bars_waited": 0,
                            "pullback_seen": False,
                            "vwap_break_count": 0,
                        }
                        return
                    self._pending_breakout = {
                        "stage": "await_expansion",
                        "direction": direction,
                        "level": level,
                        "bars_waited": 0,
                        "bar1_close": close,
                        "bar1_high": bar["high"],
                        "bar1_low": bar["low"],
                    }
                    return
                if direction == "SHORT" and close < level:
                    confirm_loc_min = self._effective_confirm_loc_min(
                        direction="SHORT",
                        bar=bar,
                        extras=extras,
                        trend_ok=trend_ok,
                        base=confirm_bar_close_loc_min,
                    )
                    rng = float(bar["high"] - bar["low"])
                    close_loc_ok = True
                    if self._should_enforce_close_loc(now, extras) and rng > 0:
                        close_loc_ok = ((float(bar["high"]) - float(bar["close"])) / rng) >= confirm_loc_min
                    if not close_loc_ok:
                        self._pending_breakout = None
                        return
                    confirm_bar_max_range_atr = float(extras.get("confirm_bar_max_range_atr", 0.0))
                    confirm_bar_min_body_ratio = float(extras.get("confirm_bar_min_body_ratio", 0.0))
                    atr_1m_confirm = float(bar.get("atr_1m") or 0.0)
                    if atr_1m_confirm <= 0:
                        atr_d_confirm = self._resolve_atr_d(bar, extras)
                        atr_1m_confirm = atr_d_confirm / max(1.0, float(extras.get("atr_d_mult_from_atr_1m", 40.0)))
                    if confirm_bar_max_range_atr > 0 and atr_1m_confirm > 0:
                        if (rng / atr_1m_confirm) > confirm_bar_max_range_atr:
                            self._pending_breakout = None
                            return
                    if confirm_bar_min_body_ratio > 0 and rng > 0:
                        body = abs(float(bar["close"]) - float(bar["open"]))
                        if (body / rng) < confirm_bar_min_body_ratio:
                            self._pending_breakout = None
                            return
                    if entry_variant == "confirm":
                        if trend_ok:
                            self._enter_with_gates(now, bar, direction, level)
                        self._pending_breakout = None
                        return
                    if entry_variant == "confirm_put_pullback_fail":
                        self._pending_breakout = {
                            "stage": "await_put_pullback_fail",
                            "direction": direction,
                            "level": level,
                            "bars_waited": 0,
                            "pullback_seen": False,
                        }
                        return
                    if entry_variant == "confirm_pullback":
                        self._pending_breakout = {
                            "stage": "await_pullback",
                            "direction": direction,
                            "level": level,
                            "bars_waited": 0,
                            "pullback_seen": False,
                        }
                        return
                    self._pending_breakout = {
                        "stage": "await_expansion",
                        "direction": direction,
                        "level": level,
                        "bars_waited": 0,
                        "bar1_close": close,
                        "bar1_high": bar["high"],
                        "bar1_low": bar["low"],
                    }
                    return

                if self._pending_breakout["bars_waited"] > 1:
                    self._pending_breakout = None
                return

            if stage == "await_dualpath":
                pb_window = int(extras.get("pb_window", 4))
                pb_dev_atr = float(extras.get("pb_dev_atr", 0.10))
                delayed_entry_mom_atr = float(extras.get("delayed_entry_mom_atr", 0.10))
                vwap_break_bars = int(extras.get("pb_vwap_break_bars", 2))
                atr_d = self._resolve_atr_d(bar, extras)
                if atr_d <= 0:
                    atr_d = float(bar.get("atr_1m") or 0.0) * float(extras.get("atr_d_mult_from_atr_1m", 40.0))
                if atr_d <= 0:
                    self._pending_breakout = None
                    return

                close_now = float(bar["close"])
                vwap_now = float(bar["vwap"])
                dev_to_level = abs(close_now - float(level)) / atr_d
                dev_to_vwap = abs(close_now - vwap_now) / atr_d if vwap_now > 0 else 999.0

                # Structure break guard while waiting for pullback.
                if direction == "LONG":
                    if vwap_now > 0 and close_now < vwap_now:
                        self._pending_breakout["vwap_break_count"] = int(self._pending_breakout.get("vwap_break_count", 0)) + 1
                    else:
                        self._pending_breakout["vwap_break_count"] = 0
                else:
                    if vwap_now > 0 and close_now > vwap_now:
                        self._pending_breakout["vwap_break_count"] = int(self._pending_breakout.get("vwap_break_count", 0)) + 1
                    else:
                        self._pending_breakout["vwap_break_count"] = 0
                if int(self._pending_breakout.get("vwap_break_count", 0)) >= max(1, vwap_break_bars):
                    self._pending_breakout = None
                    return

                # Path A: prefer pullback.
                pullback_candidate = (dev_to_level <= pb_dev_atr) or (dev_to_vwap <= pb_dev_atr)
                if pullback_candidate:
                    self._pending_breakout["pullback_seen"] = True

                pullback_seen = bool(self._pending_breakout.get("pullback_seen", False))
                if pullback_seen:
                    prev_bar = self._bars[-2]
                    if direction == "LONG":
                        trigger = close_now > float(prev_bar["high"])
                    else:
                        trigger = close_now < float(prev_bar["low"])
                    if trigger:
                        if trend_ok:
                            self._enter_with_gates(now, bar, direction, level)
                        self._pending_breakout = None
                        return

                # Path B: delayed confirm fallback if no pullback within window.
                if int(self._pending_breakout.get("bars_waited", 0)) >= max(1, pb_window):
                    atr_1m = float(bar.get("atr_1m") or 0.0)
                    if atr_1m <= 0 or len(self._bars) < 4:
                        self._pending_breakout = None
                        return
                    v3m_signed = close_now - float(self._bars[-4]["close"])
                    mom_px = delayed_entry_mom_atr * atr_1m
                    if direction == "LONG":
                        stronger_mom_ok = v3m_signed >= mom_px
                        side_ok = (vwap_now > 0 and close_now > vwap_now)
                    else:
                        stronger_mom_ok = v3m_signed <= -mom_px
                        side_ok = (vwap_now > 0 and close_now < vwap_now)
                    if stronger_mom_ok and side_ok:
                        if trend_ok:
                            self._enter_with_gates(now, bar, direction, level)
                    self._pending_breakout = None
                    return

                # Safety timeout
                if int(self._pending_breakout.get("bars_waited", 0)) > max(2, pb_window + 2):
                    self._pending_breakout = None
                return

            if stage == "await_pullback":
                atr_1m = float(bar.get("atr_1m") or 0.0)
                tol_mult = float(extras.get("pullback_tol_atr_mult", 0.12))
                max_bars = int(extras.get("pullback_max_bars", 3))
                tol = max(0.03, tol_mult * atr_1m) if atr_1m > 0 else 0.05
                pullback_seen = bool(self._pending_breakout.get("pullback_seen", False))

                if direction == "LONG":
                    touched = float(bar["low"]) <= float(level + tol)
                    not_broken = float(bar["low"]) >= float(level - tol)
                    if touched and not_broken:
                        self._pending_breakout["pullback_seen"] = True
                        pullback_seen = True
                    trigger = pullback_seen and float(bar["close"]) > float(level) and float(bar["close"]) >= float(bar["open"])
                else:
                    touched = float(bar["high"]) >= float(level - tol)
                    not_broken = float(bar["high"]) <= float(level + tol)
                    if touched and not_broken:
                        self._pending_breakout["pullback_seen"] = True
                        pullback_seen = True
                    trigger = pullback_seen and float(bar["close"]) < float(level) and float(bar["close"]) <= float(bar["open"])

                if trigger:
                    if trend_ok:
                        self._enter_with_gates(now, bar, direction, level)
                    self._pending_breakout = None
                    return

                if self._pending_breakout["bars_waited"] > max_bars:
                    self._pending_breakout = None
                return

            if stage == "await_put_pullback_fail":
                # Dedicated path for bearish sessions:
                # wait for a pullback toward VWAP, then enter on re-weakening.
                if direction != "SHORT":
                    self._pending_breakout = None
                    return

                max_bars = int(extras.get("put_pullback_window", 4))
                pullback_seen = bool(self._pending_breakout.get("pullback_seen", False))
                atr_d = self._resolve_atr_d(bar, extras)
                vwap_now = float(bar.get("vwap") or 0.0)
                close_now = float(bar.get("close") or 0.0)
                prev_low = float(self._bars[-2].get("low") or close_now) if len(self._bars) >= 2 else close_now
                pullback_dev = float(extras.get("put_pullback_vwap_dev_atr", 0.10))
                invalidate_dev = float(extras.get("put_pullback_invalidate_dev_atr", 0.15))

                if atr_d > 0 and vwap_now > 0:
                    dev_vwap = abs(close_now - vwap_now) / atr_d
                else:
                    dev_vwap = 999.0

                if dev_vwap <= pullback_dev:
                    self._pending_breakout["pullback_seen"] = True
                    pullback_seen = True

                if atr_d > 0 and vwap_now > 0 and close_now > (vwap_now + invalidate_dev * atr_d):
                    self._pending_breakout = None
                    return

                trigger = pullback_seen and (close_now < prev_low) and (vwap_now > 0 and close_now < vwap_now)
                if trigger:
                    if trend_ok:
                        self._enter_with_gates(now, bar, "SHORT", level)
                    self._pending_breakout = None
                    return

                if int(self._pending_breakout.get("bars_waited", 0)) > max_bars:
                    self._pending_breakout = None
                return

            if stage == "await_expansion":
                atr_1m = float(bar.get("atr_1m") or 0.0)
                exp_atr_mult = float(extras.get("expansion_atr_mult", 0.20))
                exp_rvol = float(extras.get("expansion_rvol", 2.0))

                cond_atr = False
                if atr_1m > 0:
                    cond_atr = abs(close - float(self._pending_breakout.get("bar1_close", close))) >= exp_atr_mult * atr_1m

                cond_break = False
                if direction == "LONG":
                    cond_break = close > float(self._pending_breakout.get("bar1_high", close))
                else:
                    cond_break = close < float(self._pending_breakout.get("bar1_low", close))

                cond_rvol = bar.get("rvol") is not None and float(bar["rvol"]) >= exp_rvol

                if (cond_atr or cond_break or cond_rvol):
                    if trend_ok:
                        self._enter_with_gates(now, bar, direction, level)
                    self._pending_breakout = None
                    return

                if self._pending_breakout["bars_waited"] > 1:
                    self._pending_breakout = None
                return

        prev = self._bars[-2]
        if close > prev["high"]:
            direction = "LONG"
        elif close < prev["low"]:
            direction = "SHORT"
        else:
            return

        if direction == "LONG" and not (close > vwap):
            return
        if direction == "SHORT" and not (close < vwap):
            return

        level = prev["high"] if direction == "LONG" else prev["low"]
        self._pending_breakout = {
            "stage": "await_confirm",
            "direction": direction,
            "level": level,
            "bars_waited": 0,
        }

    @staticmethod
    def _percentile(values: List[float], q: float) -> float:
        if not values:
            return 0.0
        q = max(0.0, min(1.0, float(q)))
        arr = sorted(float(v) for v in values if math.isfinite(float(v)))
        if not arr:
            return 0.0
        if len(arr) == 1:
            return arr[0]
        idx = q * (len(arr) - 1)
        lo = int(math.floor(idx))
        hi = int(math.ceil(idx))
        if lo == hi:
            return arr[lo]
        frac = idx - lo
        return arr[lo] * (1.0 - frac) + arr[hi] * frac

    def _trend_filter_ok(self, bar: Dict[str, Any], extras: Dict[str, Any]) -> bool:
        mode = str(extras.get("trend_filter_mode", "fixed")).strip().lower()
        rvol = bar.get("rvol")
        atr_1m = float(bar.get("atr_1m") or 0.0)
        atr_1m_ma = float(bar.get("atr_1m_ma") or 0.0)

        if mode != "relative":
            rvol_min = float(extras.get("rvol_min", 1.5))
            atr_expansion_mult = float(extras.get("atr_expansion_mult", 1.2))
            rvol_ok = rvol is not None and float(rvol) >= rvol_min
            atr_ok = atr_1m_ma > 0 and atr_1m >= atr_expansion_mult * atr_1m_ma
            base_ok = bool(rvol_ok or atr_ok)
            if base_ok:
                return True
            if bool(extras.get("trend_alt_dev_vwap_enabled", False)):
                atr_d = self._resolve_atr_d(bar, extras)
                vwap = float(bar.get("vwap") or 0.0)
                close = float(bar.get("close") or 0.0)
                if atr_d > 0 and vwap > 0:
                    dev = abs(close - vwap) / atr_d
                    if dev >= float(extras.get("trend_alt_dev_vwap_atr_d_min", 0.35)):
                        return True
            return False

        lookback = int(extras.get("trend_rel_lookback", 60))
        min_samples = int(extras.get("trend_rel_min_samples", 20))
        rvol_q = float(extras.get("trend_rel_rvol_q", 0.65))
        atr_ratio_q = float(extras.get("trend_rel_atr_ratio_q", 0.65))

        hist = list(self._bars)[-max(1, lookback):]
        rvol_hist = [float(b.get("rvol")) for b in hist if b.get("rvol") is not None]
        atr_ratio_hist = []
        for b in hist:
            a = float(b.get("atr_1m") or 0.0)
            m = float(b.get("atr_1m_ma") or 0.0)
            if m > 0:
                atr_ratio_hist.append(a / m)

        if len(rvol_hist) < min_samples or len(atr_ratio_hist) < min_samples:
            rvol_min = float(extras.get("rvol_min", 1.5))
            atr_expansion_mult = float(extras.get("atr_expansion_mult", 1.2))
            rvol_ok = rvol is not None and float(rvol) >= rvol_min
            atr_ok = atr_1m_ma > 0 and atr_1m >= atr_expansion_mult * atr_1m_ma
            base_ok = bool(rvol_ok or atr_ok)
            if base_ok:
                return True
            if bool(extras.get("trend_alt_dev_vwap_enabled", False)):
                atr_d = self._resolve_atr_d(bar, extras)
                vwap = float(bar.get("vwap") or 0.0)
                close = float(bar.get("close") or 0.0)
                if atr_d > 0 and vwap > 0:
                    dev = abs(close - vwap) / atr_d
                    if dev >= float(extras.get("trend_alt_dev_vwap_atr_d_min", 0.35)):
                        return True
            return False

        rvol_thr = self._percentile(rvol_hist, rvol_q)
        atr_ratio_thr = self._percentile(atr_ratio_hist, atr_ratio_q)
        cur_atr_ratio = (atr_1m / atr_1m_ma) if atr_1m_ma > 0 else 0.0

        rvol_ok = rvol is not None and float(rvol) >= rvol_thr
        atr_ok = cur_atr_ratio >= atr_ratio_thr
        return bool(rvol_ok or atr_ok)

    def _effective_confirm_loc_min(
        self,
        *,
        direction: str,
        bar: Dict[str, Any],
        extras: Dict[str, Any],
        trend_ok: bool,
        base: float,
    ) -> float:
        if not bool(extras.get("confirm_loc_adaptive_enabled", False)):
            return float(base)

        close = float(bar.get("close") or 0.0)
        vwap = float(bar.get("vwap") or 0.0)
        side_ok = (direction == "LONG" and close > vwap) or (direction == "SHORT" and close < vwap)
        if not side_ok:
            return float(base)

        atr_1m = float(bar.get("atr_1m") or 0.0)
        atr_1m_ma = float(bar.get("atr_1m_ma") or 0.0)
        atr_ratio = (atr_1m / atr_1m_ma) if atr_1m_ma > 0 else None
        rvol = bar.get("rvol")

        slow_atr_ratio_max = float(extras.get("confirm_loc_adaptive_atr_ratio_max", 1.10))
        slow_rvol_max = float(extras.get("confirm_loc_adaptive_rvol_max", 1.60))
        relaxed = float(extras.get("confirm_bar_close_loc_min_relaxed", max(0.35, float(base) - 0.10)))

        slow_regime = False
        if atr_ratio is not None and atr_ratio <= slow_atr_ratio_max:
            slow_regime = True
        if rvol is not None and float(rvol) <= slow_rvol_max:
            slow_regime = True

        if trend_ok and slow_regime:
            return min(float(base), float(relaxed))
        return float(base)

    def _should_enforce_close_loc(self, now: datetime, extras: Dict[str, Any]) -> bool:
        if not bool(extras.get("close_loc_window_only", False)):
            return True
        start = _parse_hms(extras.get("close_loc_window_start", "09:45:00"), time(9, 45))
        end = _parse_hms(extras.get("close_loc_window_end", "10:00:00"), time(10, 0))
        nt = now.time()
        return start <= nt <= end

    def _effective_deadzone(self, now: datetime, bar: Dict[str, Any], extras: Dict[str, Any]) -> float:
        atr_1m = float(bar.get("atr_1m") or 0.0)
        base_px = float(extras.get("momentum_deadzone", 0.05))
        mult = float(extras.get("momentum_deadzone_atr_mult", 0.25))
        if bool(extras.get("momentum_deadzone_schedule_enabled", False)):
            split = _parse_hms(extras.get("momentum_deadzone_split_time", "10:15:00"), time(10, 15))
            if now.time() <= split:
                mult = float(extras.get("momentum_deadzone_atr_mult_early", mult))
            else:
                mult = float(extras.get("momentum_deadzone_atr_mult_late", mult))
        return max(base_px, mult * atr_1m)

    def _is_bear_dominant(self, bar: Dict[str, Any], extras: Dict[str, Any]) -> bool:
        if len(self._bars) < 12:
            return False
        close = float(bar.get("close") or 0.0)
        vwap = float(bar.get("vwap") or 0.0)
        if not (close > 0 and vwap > 0 and close < vwap):
            return False
        slope_short = close - float(self._bars[-4].get("close") or close)
        slope_mid = close - float(self._bars[-11].get("close") or close)
        if not (slope_short < 0 and slope_mid < 0):
            return False
        if bool(extras.get("bear_bias_require_vwap_slope", False)):
            vwap_prev = float(self._bars[-4].get("vwap") or vwap)
            if not (vwap - vwap_prev < 0):
                return False
        return True

    def _enter_with_gates(
        self,
        now: datetime,
        bar: Dict[str, Any],
        direction: str,
        breakout_level: Optional[float] = None,
    ) -> None:
        extras = self.params.extras or {}
        if len(self._bars) < 4:
            return

        close = bar["close"]
        v3m_signed = close - self._bars[-4]["close"]

        if bool(extras.get("bear_dominant_put_only", False)):
            if direction == "LONG" and self._is_bear_dominant(bar, extras):
                return

        deadzone_px = self._effective_deadzone(now, bar, extras)
        if direction == "LONG" and v3m_signed <= deadzone_px:
            return
        if direction == "SHORT" and v3m_signed >= -deadzone_px:
            return

        if bool(extras.get("anti_chase_enabled", False)) and breakout_level is not None:
            atr_d = self._resolve_atr_d(bar, extras)
            if atr_d > 0:
                max_ext_atr = float(extras.get("anti_chase_max_extension_atr", 0.12))
                ext_signed = (
                    (float(close) - float(breakout_level)) / atr_d
                    if direction == "LONG"
                    else (float(breakout_level) - float(close)) / atr_d
                )
                if ext_signed > max_ext_atr:
                    return

        atr_rank = extras.get("atr_rank_today")
        atr_rank_skip_max = float(extras.get("atr_rank_skip_max", 0.15))
        if atr_rank is not None:
            try:
                atr_rank = float(atr_rank)
                if atr_rank <= atr_rank_skip_max:
                    return
            except Exception:
                pass

        early_override_window_start = _parse_hms(extras.get("early_override_window_start", "09:39:00"), time(9, 39))
        early_override_window_end = _parse_hms(extras.get("early_override_window_end", "09:45:00"), time(9, 45))
        if now.time() < time(9, 45):
            early_override = self._early_override_ready(now, extras)
            in_window = early_override_window_start <= now.time() <= early_override_window_end
            if not (in_window and early_override):
                return

        delayed_confirm_end = _parse_hms(extras.get("delayed_confirm_end", "10:00:00"), time(10, 0))
        if time(9, 45) <= now.time() <= delayed_confirm_end:
            atr_d = self._resolve_atr_d(bar, extras)
            if atr_d <= 0:
                return
            if len(self._bars) < 6:
                return
            close_now = bar["close"]
            close_5m_ago = self._bars[-6]["close"]
            move5m = abs(close_now - close_5m_ago) / atr_d
            if move5m < float(extras.get("delayed_confirm_move5m_min", 0.155)):
                return

        if bool(extras.get("entry_commonality_gate_enabled", False)) and (
            not bool(extras.get("entry_commonality_only_second_trade", False)) or self._trades_today >= 1
        ):
            atr_d = self._resolve_atr_d(bar, extras)
            if atr_d <= 0:
                return
            if len(self._bars) < 4:
                return
            atr_1m = float(bar.get("atr_1m") or 0.0)
            atr_1m_ma = float(bar.get("atr_1m_ma") or 0.0)
            atr_exp = (atr_1m / atr_1m_ma) if atr_1m_ma > 0 else 0.0
            vwap = float(bar.get("vwap") or 0.0)
            dist_vwap_atr = abs(float(close) - vwap) / atr_d if vwap > 0 else 999.0
            mom3_signed_atr = ((float(close) - float(self._bars[-4]["close"])) / atr_d) * (1.0 if direction == "LONG" else -1.0)

            cond_atr = atr_exp >= float(extras.get("entry_commonality_min_atr_exp", 1.05))
            cond_vwap = dist_vwap_atr <= float(extras.get("entry_commonality_max_dist_vwap_atr", 0.03))
            cond_pullback = mom3_signed_atr <= float(extras.get("entry_commonality_max_mom3_signed_atr", 0.00))
            min_atr_exp_raw = extras.get("entry_commonality_min_atr_exp", None)
            max_atr_exp_raw = extras.get("entry_commonality_max_atr_exp", None)
            min_dist_raw = extras.get("entry_commonality_min_dist_vwap_atr", None)
            max_dist_raw = extras.get("entry_commonality_max_dist_vwap_atr", None)
            min_mom_raw = extras.get("entry_commonality_min_mom3_signed_atr", None)
            max_mom_raw = extras.get("entry_commonality_max_mom3_signed_atr", None)

            checks = []
            if min_atr_exp_raw is not None:
                checks.append(atr_exp >= float(min_atr_exp_raw))
            if max_atr_exp_raw is not None:
                checks.append(atr_exp <= float(max_atr_exp_raw))
            if min_dist_raw is not None:
                checks.append(dist_vwap_atr >= float(min_dist_raw))
            if max_dist_raw is not None:
                checks.append(dist_vwap_atr <= float(max_dist_raw))
            if min_mom_raw is not None:
                checks.append(mom3_signed_atr >= float(min_mom_raw))
            if max_mom_raw is not None:
                checks.append(mom3_signed_atr <= float(max_mom_raw))

            if checks:
                min_score = int(extras.get("entry_commonality_min_score", len(checks)))
                score = sum(1 for c in checks if bool(c))
                if score < min_score:
                    return
            else:
                # Backward-compatible fallback for old three-condition configs.
                min_score = int(extras.get("entry_commonality_min_score", 2))
                score = int(cond_atr) + int(cond_vwap) + int(cond_pullback)
                if score < min_score:
                    return

        side = "CALL" if direction == "LONG" else "PUT"
        if bool(extras.get("iv_gate_enabled", False)):
            iv_now, iv_rank, iv_chg3 = self._iv_state(side, now)
            iv_rank_max = float(extras.get("iv_rank_max", 0.90))
            iv_chg3m_min = float(extras.get("iv_3m_change_min", -0.02))
            if iv_now is not None:
                if iv_rank is not None and iv_rank > iv_rank_max:
                    return
                if iv_chg3 is not None and iv_chg3 < iv_chg3m_min:
                    return

        if bool(extras.get("iv_gate_v2_enabled", False)):
            iv_now, _, _ = self._iv_state(side, now)
            iv_chg5 = self._iv_change(side, now, int(extras.get("iv_chg_minutes", 5)))
            iv_z30 = self._iv_zscore(side, int(extras.get("iv_z_window", 30)))
            iv_chg5_min = float(extras.get("iv_chg5m_min", -0.02))
            iv_z30_max = float(extras.get("iv_z30_max", 2.0))
            # CALLs are usually more vulnerable to IV crush than PUTs.
            call_penalty = float(extras.get("iv_gate_call_extra_penalty", 0.0)) if side == "CALL" else 0.0
            if iv_now is not None:
                if iv_chg5 is not None and iv_chg5 < (iv_chg5_min - call_penalty):
                    return
                if iv_z30 is not None and iv_z30 > iv_z30_max:
                    return

        if bool(extras.get("theta_entry_gate_enabled", False)):
            quote = self._lookup_option_quote(now, side, float(bar.get("close") or 0.0), None)
            if quote is not None:
                premium = float(quote.get("mid") or 0.0)
                delta = abs(float(quote.get("delta") or 0.0))
                theta = abs(float(quote.get("theta") or 0.0))
                if premium > 0 and delta > 0:
                    n_min = float(extras.get("theta_entry_horizon_min", 8.0))
                    theta_per_min = theta / 390.0
                    s_now = float(bar.get("close") or 0.0)
                    d_s_min = theta_per_min * n_min * s_now / max(1e-8, delta * premium)
                    atr_d = self._resolve_atr_d(bar, extras)
                    if atr_d > 0:
                        d_s_min_atr = d_s_min / atr_d
                        if d_s_min_atr > float(extras.get("theta_entry_max_move_atr", 0.25)):
                            return
        self._enter_position(now, bar, side)

    def _early_override_ready(self, now: datetime, extras: Dict[str, Any]) -> bool:
        if now.time() < time(9, 39):
            return False

        atr_d = self._resolve_atr_d(self._bars[-1], extras)
        if atr_d <= 0:
            return False

        open_0930 = self._price_at_time(time(9, 30))
        px_0938 = self._price_at_time(time(9, 38))
        if open_0930 is None or px_0938 is None:
            return False

        early_move_mult = float(extras.get("early_override_move_mult", 0.70))
        early_vol_min = float(extras.get("early_override_vol_min", 1.50))

        early_move = abs(px_0938 - open_0930)
        if early_move < early_move_mult * atr_d:
            return False

        side_sign = None
        vols = []
        vol_0938 = None
        for b in self._bars:
            bt = b["ts"].time()
            if time(9, 31) <= bt <= time(9, 39):
                c = b["close"]
                v = b["vwap"]
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
                    vol_0938 = b["volume"]
                else:
                    vols.append(b["volume"])

        if vol_0938 is None or len(vols) < 7:
            return False
        base_vol = sum(vols) / max(1, len(vols))
        if base_vol <= 0:
            return False
        if (vol_0938 / base_vol) < early_vol_min:
            return False

        return True

    def _price_at_time(self, target: time) -> Optional[float]:
        for b in self._bars:
            if b["ts"].time() == target:
                return b["close"]
        return None

    # ---------------------- Execution Layer ----------------------
    def _enter_position(self, now: datetime, bar: Dict[str, Any], side: str) -> None:
        extras = self.params.extras or {}
        contracts = int(extras.get("contracts_per_trade", 3))
        if contracts <= 0:
            return

        premium, strike, quote_meta = self._estimate_0dte_premium(bar, side, now, extras)
        if premium <= 0:
            return

        self._position = OptionPosition(
            side=side,
            qty=contracts,
            entry_premium=premium,
            entry_underlying=bar["close"],
            entry_time=now,
            strike=strike,
            entry_iv=(None if quote_meta is None else quote_meta.get("iv")),
            tp1_done=False,
            runner_qty=max(0, contracts - 2),
        )
        self._trades_today += 1
        self._trades.append({
            "event": "ENTRY",
            "ts": now.isoformat(),
            "side": side,
            "qty": contracts,
            "premium": premium,
            "underlying": bar["close"],
            "strike": strike,
        })

    def _manage_position(self, now: datetime, bar: Dict[str, Any]) -> None:
        pos = self._position
        if pos is None:
            return

        extras = self.params.extras or {}
        premium, _, quote_meta = self._estimate_0dte_premium(bar, pos.side, now, extras, strike=pos.strike)
        if premium <= 0:
            return

        held_min = (now - pos.entry_time).total_seconds() / 60.0
        tp1_mult = float(extras.get("tp1_mult", 1.30))
        tp2_mult = float(extras.get("tp2_mult", 1.80))
        stop_mult = float(extras.get("stop_loss_mult", 0.65))
        vwap = bar["vwap"]

        if pos.tp1_done and pos.qty == 1:
            if premium >= pos.entry_premium * tp2_mult:
                self._exit_all(now, premium, bar, reason="TP2")
                return

        if (not pos.tp1_done) and premium >= pos.entry_premium * tp1_mult:
            tp1_sell_qty = int(extras.get("tp1_sell_qty", 2))
            if tp1_sell_qty <= 0:
                tp1_sell_qty = 1
            sell_qty = min(tp1_sell_qty, pos.qty)
            pos.qty -= sell_qty
            pos.tp1_done = True
            pos.runner_qty = max(1, pos.qty)
            self._trades.append({
                "event": "TP1",
                "ts": now.isoformat(),
                "side": pos.side,
                "qty": sell_qty,
                "premium": premium,
                "underlying": bar["close"],
                "strike": pos.strike,
            })
            if pos.qty <= 0:
                self._position = None
            return

        if pos.tp1_done and pos.qty > 0:
            floor = pos.entry_premium * tp1_mult
            if premium >= pos.entry_premium * 1.60:
                floor = max(floor, pos.entry_premium * 1.40)
            if premium >= pos.entry_premium * 2.00:
                floor = max(floor, pos.entry_premium * 1.70)
            if premium <= floor:
                self._exit_all(now, premium, bar, reason="RUNNER_PROTECT")
                return

        if (not pos.tp1_done) and premium <= pos.entry_premium * stop_mult:
            self._exit_all(now, premium, bar, reason="HARD_STOP")
            return

        if bool(extras.get("theta_edge_gate_enabled", False)):
            theta_gate_min_hold = float(extras.get("theta_edge_gate_min_hold_min", 3.0))
            theta_gate_mult = float(extras.get("theta_edge_cost_mult", 1.0))
            use_chain_greeks = quote_meta is not None and str(quote_meta.get("source", "")) == "chain"
            if held_min >= theta_gate_min_hold and use_chain_greeks and len(self._bars) >= 2:
                d_s = float(bar["close"] - self._bars[-2]["close"])
                d_iv = 0.0
                iv_now = quote_meta.get("iv")
                if iv_now is not None and pos.entry_iv is not None:
                    d_iv = float(iv_now - pos.entry_iv)
                delta = float(quote_meta.get("delta") or 0.0)
                gamma = float(quote_meta.get("gamma") or 0.0)
                vega = float(quote_meta.get("vega") or 0.0)
                theta = float(quote_meta.get("theta") or 0.0)
                dt_day = 1.0 / 390.0
                expected_gain = (delta * d_s) + (0.5 * gamma * d_s * d_s) + (vega * d_iv) + (theta * dt_day)
                theta_cost = abs(theta) * dt_day
                if expected_gain <= theta_gate_mult * theta_cost:
                    self._exit_all(now, premium, bar, reason="THETA_EDGE_EXIT")
                    return

        if bool(extras.get("iv_stop_enabled", False)):
            use_chain_greeks = quote_meta is not None and str(quote_meta.get("source", "")) == "chain"
            if use_chain_greeks and pos.entry_iv is not None:
                iv_now = quote_meta.get("iv")
                if iv_now is not None:
                    iv_drop = float(pos.entry_iv - float(iv_now))
                    iv_drop_thresh = float(extras.get("iv_stop_drop_threshold", 0.03))
                    iv_stop_max_opt_return = float(extras.get("iv_stop_max_opt_return", 0.05))
                    opt_return_now = (premium - pos.entry_premium) / pos.entry_premium if pos.entry_premium > 0 else 0.0
                    if iv_drop >= iv_drop_thresh and opt_return_now <= iv_stop_max_opt_return:
                        self._exit_all(now, premium, bar, reason="IV_STOP_EXIT")
                        return

        opt_return = (premium - pos.entry_premium) / pos.entry_premium if pos.entry_premium > 0 else 0.0
        atr_d = self._resolve_atr_d(bar, extras)
        move = abs(bar["close"] - pos.entry_underlying) / atr_d if atr_d > 0 else 0.0

        fast_fail_min_hold = float(extras.get("fast_fail_min_hold_min", 2.0))
        fast_fail_opt_return = float(extras.get("fast_fail_opt_return_max", -0.08))
        fast_fail_require_underlying_confirm = bool(extras.get("fast_fail_require_underlying_confirm", False))
        fast_fail_underlying_move_atr = float(extras.get("fast_fail_underlying_move_atr", 0.08))
        if held_min >= fast_fail_min_hold and opt_return <= fast_fail_opt_return:
            if fast_fail_require_underlying_confirm:
                if pos.side == "CALL":
                    underlying_against = (pos.entry_underlying - bar["close"]) / atr_d if atr_d > 0 else 0.0
                else:
                    underlying_against = (bar["close"] - pos.entry_underlying) / atr_d if atr_d > 0 else 0.0
                if underlying_against < fast_fail_underlying_move_atr:
                    pass
                else:
                    self._exit_all(now, premium, bar, reason="FAST_FAIL_2MIN")
                    return
            else:
                self._exit_all(now, premium, bar, reason="FAST_FAIL_2MIN")
                return

        early_no_move_min_hold = float(extras.get("early_no_move_min_hold_min", 2.0))
        early_no_move_atr = float(extras.get("early_no_move_underlying_move_atr", 0.08))
        early_no_move_min_ret = float(extras.get("early_no_move_min_opt_return", 0.05))
        early_no_move_only_if_non_profit = bool(extras.get("early_no_move_only_if_non_profit", False))
        if held_min >= early_no_move_min_hold:
            if move < early_no_move_atr and opt_return < early_no_move_min_ret:
                if early_no_move_only_if_non_profit and opt_return > 0.0:
                    pass
                else:
                    self._exit_all(now, premium, bar, reason="EARLY_NO_MOVE_EXIT")
                    return

        no_move_min_hold = float(extras.get("no_move_min_hold_min", 5.0))
        no_move_atr = float(extras.get("no_move_underlying_move_atr", 0.15))
        no_move_min_ret = float(extras.get("no_move_min_opt_return", 0.10))
        late_no_move_only_pre_tp1 = bool(extras.get("late_no_move_only_pre_tp1", False))
        allow_late_no_move = (not late_no_move_only_pre_tp1) or (not pos.tp1_done)
        if held_min >= no_move_min_hold:
            if allow_late_no_move and move < no_move_atr and opt_return < no_move_min_ret:
                self._exit_all(now, premium, bar, reason="LATE_NO_MOVE_EXIT")
                return

        vwap_exit_only_pre_tp1 = bool(extras.get("vwap_exit_only_pre_tp1", False))
        allow_vwap_exit = (not vwap_exit_only_pre_tp1) or (not pos.tp1_done)
        if allow_vwap_exit:
            vwap_exit_minutes = float(extras.get("vwap_exit_minutes", 7.0))
            if held_min >= vwap_exit_minutes:
                if vwap > 0:
                    if pos.side == "CALL" and bar["close"] < vwap:
                        self._exit_all(now, premium, bar, reason="VWAP_EXIT")
                        return
                    if pos.side == "PUT" and bar["close"] > vwap:
                        self._exit_all(now, premium, bar, reason="VWAP_EXIT")
                        return

    def _exit_all(self, now: datetime, premium: float, bar: Dict[str, Any], reason: str) -> None:
        pos = self._position
        if pos is None:
            return
        self._trades.append({
            "event": "EXIT",
            "ts": now.isoformat(),
            "side": pos.side,
            "qty": pos.qty,
            "premium": premium,
            "underlying": bar["close"],
            "strike": pos.strike,
            "reason": reason,
        })
        if self._first_trade_exit_reason is None:
            self._first_trade_exit_reason = reason
        self._position = None

    # ---------------------- Pricing Helpers ----------------------
    def _estimate_0dte_premium(
        self,
        bar: Dict[str, Any],
        side: str,
        now: datetime,
        extras: Dict[str, Any],
        *,
        strike: Optional[float] = None,
    ) -> tuple[float, float, Optional[Dict[str, Any]]]:
        spot = bar["close"]
        if spot <= 0:
            return 0.0, 0.0, None

        strike_step = float(extras.get("strike_step", 1.0))
        if strike is None:
            strike = round(spot / strike_step) * strike_step

        quote = self._lookup_option_quote(now, side, spot, strike)
        if quote is not None:
            mid = float(quote.get("mid") or 0.0)
            q_strike = float(quote.get("strike") or strike)
            if mid > 0:
                q = dict(quote)
                q["source"] = "chain"
                return mid, q_strike, q

        expiry = datetime(now.year, now.month, now.day, 16, 0, tzinfo=ET)
        t_sec = max(60.0, (expiry - now).total_seconds())
        t_years = t_sec / (365.0 * 24.0 * 3600.0)

        iv = float(extras.get("iv_proxy", 0.35))
        r = float(extras.get("r_rate", 0.01))
        q = float(extras.get("q_div", 0.0))
        right = "C" if side == "CALL" else "P"

        premium = _bs_price(spot, strike, t_years, r, q, iv, right)
        if not math.isfinite(premium) or premium <= 0:
            return 0.0, strike, None
        return float(premium), float(strike), {"iv": iv, "source": "model"}

    def _resolve_atr_d(self, bar: Dict[str, Any], extras: Dict[str, Any]) -> float:
        atr_d = float(bar.get("atr_d", 0.0) or 0.0)
        if atr_d > 0:
            return atr_d
        atr_1m = float(bar.get("atr_1m", 0.0) or 0.0)
        mult = float(extras.get("atr_d_mult_from_atr_1m", 40.0))
        return atr_1m * mult

    def get_trades(self) -> List[Dict[str, Any]]:
        return list(self._trades)

    def set_option_quotes_cache(self, cache: Dict[Tuple[datetime, str], List[Dict[str, Any]]]) -> None:
        self._option_quotes_cache = cache

    def _lookup_option_quote(
        self,
        now: datetime,
        side: str,
        spot: float,
        strike: Optional[float],
    ) -> Optional[Dict[str, Any]]:
        if not self._option_quotes_cache:
            return None
        right = "C" if side == "CALL" else "P"
        key = (now.replace(second=0, microsecond=0), right)
        rows = self._option_quotes_cache.get(key)
        if not rows:
            return None
        target = float(strike if strike is not None else spot)
        best = min(rows, key=lambda r: abs(float(r.get("strike") or target) - target))
        return best

    def _update_iv_history(self, now: datetime, bar: Dict[str, Any]) -> None:
        if not self._option_quotes_cache:
            return
        spot = float(bar.get("close") or 0.0)
        if spot <= 0:
            return
        call_q = self._lookup_option_quote(now, "CALL", spot, None)
        put_q = self._lookup_option_quote(now, "PUT", spot, None)
        if call_q is not None and call_q.get("iv") is not None:
            self._iv_hist_call.append((now, float(call_q["iv"])))
        if put_q is not None and put_q.get("iv") is not None:
            self._iv_hist_put.append((now, float(put_q["iv"])))

    def _iv_state(self, side: str, now: datetime) -> tuple[Optional[float], Optional[float], Optional[float]]:
        hist = self._iv_hist_call if side == "CALL" else self._iv_hist_put
        if not hist:
            return None, None, None
        iv_now = float(hist[-1][1])
        # intraday rank on recent window
        vals = [float(v) for _, v in hist]
        lo = min(vals)
        hi = max(vals)
        iv_rank = 0.5 if hi <= lo else (iv_now - lo) / (hi - lo)
        # 3-minute change
        cutoff = now.timestamp() - 180.0
        iv_prev = None
        for ts, v in reversed(hist):
            if ts.timestamp() <= cutoff:
                iv_prev = float(v)
                break
        iv_chg3 = None if iv_prev is None else (iv_now - iv_prev)
        return iv_now, iv_rank, iv_chg3

    def _iv_change(self, side: str, now: datetime, lookback_min: int) -> Optional[float]:
        hist = self._iv_hist_call if side == "CALL" else self._iv_hist_put
        if not hist:
            return None
        iv_now = float(hist[-1][1])
        cutoff = now.timestamp() - float(max(1, lookback_min) * 60)
        iv_prev = None
        for ts, v in reversed(hist):
            if ts.timestamp() <= cutoff:
                iv_prev = float(v)
                break
        return None if iv_prev is None else (iv_now - iv_prev)

    def _iv_zscore(self, side: str, window: int) -> Optional[float]:
        hist = self._iv_hist_call if side == "CALL" else self._iv_hist_put
        n = max(5, int(window))
        if len(hist) < n:
            return None
        vals = [float(v) for _, v in list(hist)[-n:]]
        mean = sum(vals) / len(vals)
        var = sum((x - mean) * (x - mean) for x in vals) / max(1, len(vals) - 1)
        std = math.sqrt(max(1e-12, var))
        return (vals[-1] - mean) / std


# ---------------------- Utilities ----------------------

def _parse_hms(raw: Any, default: time) -> time:
    try:
        val = str(raw or "").strip()
        if not val:
            return default
        return time.fromisoformat(val)
    except Exception:
        return default


def _bs_price(spot: float, strike: float, t_years: float, r: float, q: float, sigma: float, right: str) -> float:
    if spot <= 0 or strike <= 0 or t_years <= 0 or sigma <= 0:
        return float("nan")
    sqrt_t = math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (r - q + 0.5 * sigma * sigma) * t_years) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    disc_q = math.exp(-q * t_years)
    disc_r = math.exp(-r * t_years)
    if right == "C":
        return disc_q * spot * _norm_cdf(d1) - disc_r * strike * _norm_cdf(d2)
    return disc_r * strike * _norm_cdf(-d2) - disc_q * spot * _norm_cdf(-d1)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
