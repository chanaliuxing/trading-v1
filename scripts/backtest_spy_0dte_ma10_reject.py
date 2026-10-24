#!/usr/bin/env python3
"""SPY 0DTE MA10 Reject trend-pullback backtest (signal+synthetic option execution).

Uses real 1m SPY bars from ClickHouse and a delta-driven 0DTE pricing layer.
Outputs trade log + daily report + summary with MDD.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# Ensure project root for local imports.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from libs.db.ch_pool import get_client


# ---------------- Pricing ---------------- #

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs_price_delta(spot: float, strike: float, tau_years: float, sigma: float, side: str) -> Tuple[float, float]:
    if spot <= 0 or strike <= 0 or tau_years <= 0 or sigma <= 0:
        return 0.0, 0.0
    sqrt_t = math.sqrt(tau_years)
    d1 = (math.log(spot / strike) + 0.5 * sigma * sigma * tau_years) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    if side == "CALL":
        return spot * _norm_cdf(d1) - strike * _norm_cdf(d2), _norm_cdf(d1)
    return strike * _norm_cdf(-d2) - spot * _norm_cdf(-d1), -_norm_cdf(-d1)


def _pick_strike_by_delta(spot: float, sigma: float, tau_years: float, side: str, target_abs_delta: float, step: float = 1.0) -> float:
    # Restrict to a practical near-ATM range for 0DTE.
    lo = max(step, math.floor((spot * 0.85) / step) * step)
    hi = math.ceil((spot * 1.15) / step) * step
    grid = np.arange(lo, hi + step, step)
    best_k = float(round(spot / step) * step)
    best_err = 1e9
    for k in grid:
        _, dlt = _bs_price_delta(spot, float(k), tau_years, sigma, side)
        err = abs(abs(dlt) - target_abs_delta)
        if err < best_err:
            best_err = err
            best_k = float(k)
    return best_k


# ---------------- Data ---------------- #

def load_spy_1m(start_date: str, end_date: str) -> pd.DataFrame:
    q = """
    SELECT
      ts_start AS ts,
      symbol,
      open,
      high,
      low,
      close,
      vwap,
      volume
    FROM trading.bars_1m_ibkr FINAL
    WHERE symbol = 'SPY'
      AND is_rth = 1
      AND ts_start >= %(start)s
      AND ts_start < %(end)s
    ORDER BY ts_start
    """
    ch = get_client()
    rows = ch.query(
        q,
        {
            "start": f"{start_date} 00:00:00",
            "end": f"{end_date} 00:00:00",
        },
        settings={"use_query_cache": False},
    )
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"]).dt.tz_localize(None)
    df = df.sort_values("ts").reset_index(drop=True)
    return df


def add_features(df: pd.DataFrame, vol_lookback_days: int = 20) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    out["date"] = out["ts"].dt.date
    out["minute_of_day"] = out["ts"].dt.hour * 60 + out["ts"].dt.minute

    prev_close = out["close"].shift(1)
    tr = pd.concat(
        [
            (out["high"] - out["low"]).abs(),
            (out["high"] - prev_close).abs(),
            (out["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    out["atr_1m"] = tr.rolling(14, min_periods=14).mean()
    ret = out["close"].pct_change().fillna(0.0)
    out["rv_30m"] = ret.rolling(30, min_periods=30).std()
    out["iv_proxy"] = (out["rv_30m"] * math.sqrt(252.0 * 390.0)).clip(0.08, 1.2)

    out["ma10_1m"] = out["close"].rolling(10, min_periods=10).mean()

    rng = (out["high"] - out["low"]).replace(0, np.nan)
    out["upper_wick_ratio"] = ((out["high"] - out[["open", "close"]].max(axis=1)) / rng).fillna(0.0)
    out["lower_wick_ratio"] = ((out[["open", "close"]].min(axis=1) - out["low"]) / rng).fillna(0.0)
    out["close_low_ratio"] = ((out["close"] - out["low"]) / rng).fillna(0.5)
    out["close_high_ratio"] = ((out["high"] - out["close"]) / rng).fillna(0.5)
    out["lower_wick_q80"] = out["lower_wick_ratio"].rolling(120, min_periods=30).quantile(0.8).fillna(0.5)
    out["upper_wick_q80"] = out["upper_wick_ratio"].rolling(120, min_periods=30).quantile(0.8).fillna(0.5)

    # Build 5m bars by day to avoid cross-day bleeding.
    g = out.copy()
    g["bar5"] = g["ts"].dt.floor("5min")
    out["bar5"] = out["ts"].dt.floor("5min")
    agg = (
        g.groupby(["date", "bar5"], as_index=False)
        .agg(
            open_5m=("open", "first"),
            high_5m=("high", "max"),
            low_5m=("low", "min"),
            close_5m=("close", "last"),
            volume_5m=("volume", "sum"),
        )
        .sort_values(["date", "bar5"])
    )
    # day vwap from 1m cumulative (more stable than 5m sampled vwap)
    out["cum_pv_day"] = (out["close"] * out["volume"]).groupby(out["date"]).cumsum()
    out["cum_vol_day"] = out["volume"].groupby(out["date"]).cumsum().replace(0, np.nan)
    out["vwap_day"] = (out["cum_pv_day"] / out["cum_vol_day"]).fillna(out["close"])

    for col, win in [("ma10_5m", 10), ("ma20_5m", 20), ("ma200_5m", 200)]:
        agg[col] = agg.groupby("date")["close_5m"].transform(lambda s: s.rolling(win, min_periods=win).mean())
    agg["slope_ma10_5m"] = agg.groupby("date")["ma10_5m"].diff()

    # Merge 5m regime info back to 1m rows by bar5 bucket.
    out = out.merge(
        agg[["date", "bar5", "close_5m", "ma10_5m", "ma20_5m", "ma200_5m", "slope_ma10_5m"]],
        on=["date", "bar5"],
        how="left",
    )

    # MA200 distance (normalized), and dynamic proximity threshold (P30)
    out["atr_5m_proxy"] = out.groupby("date")["close_5m"].transform(lambda s: s.diff().abs().rolling(14, min_periods=5).mean())
    out["dist_ma200"] = (out["close_5m"] - out["ma200_5m"]).abs() / (out["atr_5m_proxy"].replace(0, np.nan))
    out["dist_ma200"] = out["dist_ma200"].replace([np.inf, -np.inf], np.nan)
    out["near_ma200_th"] = out["dist_ma200"].rolling(2000, min_periods=200).quantile(0.30)
    out["is_near_ma200"] = out["dist_ma200"] <= out["near_ma200_th"]

    # Vol score = percentile of current ATR among same minute across previous N days.
    vol_scores: List[float] = []
    minute_hist: Dict[int, List[Tuple[datetime.date, float]]] = {}
    for r in out.itertuples(index=False):
        m = int(r.minute_of_day)
        d = r.date
        x = float(r.atr_1m) if pd.notna(r.atr_1m) else np.nan
        hist = minute_hist.get(m, [])
        window_vals = [v for dd, v in hist if (d - dd).days <= vol_lookback_days]
        if not window_vals or not np.isfinite(x):
            score = 0.5
        else:
            arr = np.asarray(window_vals)
            score = float((arr <= x).sum() / len(arr))
        vol_scores.append(score)
        if np.isfinite(x):
            hist.append((d, x))
            minute_hist[m] = hist
    out["vol_score"] = vol_scores

    return out


# ---------------- Backtest ---------------- #

@dataclass
class Leg:
    qty: int
    entry_premium: float
    entry_ts: datetime


@dataclass
class Position:
    side: str
    setup: str
    regime: str
    strike: float
    delta_target: float
    risk_dist: float
    struct_stop: float
    tp1_level: float
    entry_under: float
    entry_ts: datetime
    entry_idx: int
    reject_ref: float
    entry_reason: str
    legs: List[Leg]
    tp1_done: bool = False
    runner_reached_100: bool = False
    best_fav_move: float = 0.0
    best_fav_idx: int = 0
    worst_adverse_move: float = 0.0


class Backtester:
    def __init__(self, df: pd.DataFrame, cfg: Dict[str, Any]):
        self.df = df.reset_index(drop=True)
        self.cfg = cfg
        self.trades: List[Dict[str, Any]] = []
        self.daily: Dict[datetime.date, Dict[str, Any]] = {}
        self.equity_curve: List[Dict[str, Any]] = []
        self.equity = float(cfg["starting_cash"])
        self.peak = self.equity
        self.max_dd = 0.0

        self.pos: Optional[Position] = None
        self.pending_reject: Optional[Dict[str, Any]] = None
        self.current_day: Optional[datetime.date] = None
        self.trades_today = 0
        self.daily_realized = 0.0
        self.block_new_entry = False

    def run(self) -> Dict[str, Any]:
        for i, r in enumerate(self.df.itertuples(index=False)):
            ts = r.ts
            d = r.date
            if self.current_day != d:
                self._on_new_day(d)

            row = r._asdict()
            if self.pos is not None:
                self._manage_position(i, row)

            if self.pos is None:
                self._maybe_open(i, row)

            self.equity_curve.append({"ts": ts.isoformat(), "equity": self.equity})
            self.peak = max(self.peak, self.equity)
            dd = (self.peak - self.equity) / self.peak if self.peak > 0 else 0.0
            self.max_dd = max(self.max_dd, dd)

        # Force exit EOD if still open.
        if self.pos is not None:
            last = self.df.iloc[-1].to_dict()
            self._close_all(len(self.df) - 1, last, "EOD_FORCE_EXIT")

        trades_df = pd.DataFrame(self.trades)
        daily_df = pd.DataFrame(self.daily.values()).sort_values("date") if self.daily else pd.DataFrame()

        total_realized = float(trades_df["pnl"].sum()) if not trades_df.empty else 0.0
        wins = int((trades_df["pnl"] > 0).sum()) if not trades_df.empty else 0
        losses = int((trades_df["pnl"] < 0).sum()) if not trades_df.empty else 0
        summary = {
            "start": str(self.df["date"].min()) if not self.df.empty else None,
            "end": str(self.df["date"].max()) if not self.df.empty else None,
            "n_trades": int(len(trades_df)),
            "wins": wins,
            "losses": losses,
            "win_rate": (wins / len(trades_df)) if len(trades_df) > 0 else 0.0,
            "realized_pnl": total_realized,
            "return_pct": total_realized / float(self.cfg["starting_cash"]),
            "max_drawdown_pct": self.max_dd,
            "avg_trade_pnl": (total_realized / len(trades_df)) if len(trades_df) > 0 else 0.0,
            "daily_avg_pnl": float(daily_df["day_pnl"].mean()) if not daily_df.empty else 0.0,
        }
        return {
            "summary": summary,
            "trades": trades_df,
            "daily": daily_df,
            "equity_curve": pd.DataFrame(self.equity_curve),
        }

    def _on_new_day(self, d: datetime.date) -> None:
        self.current_day = d
        self.trades_today = 0
        self.daily_realized = 0.0
        self.block_new_entry = False
        self.pending_reject = None
        self.daily.setdefault(
            d,
            {
                "date": str(d),
                "trades": 0,
                "wins": 0,
                "losses": 0,
                "gross_profit": 0.0,
                "gross_loss": 0.0,
                "day_pnl": 0.0,
                "cum_pnl": self.equity - float(self.cfg["starting_cash"]),
            },
        )

    def _regime(self, row: Dict[str, Any]) -> str:
        c5 = row.get("close_5m")
        m10 = row.get("ma10_5m")
        m20 = row.get("ma20_5m")
        s10 = row.get("slope_ma10_5m")
        vwap = row.get("vwap_day")
        if pd.notna(c5) and pd.notna(m10) and pd.notna(m20) and pd.notna(s10) and pd.notna(vwap):
            if (m20 > m10) and (s10 < 0) and (c5 < vwap):
                return "BEAR"
            if (m20 < m10) and (s10 > 0) and (c5 > vwap):
                return "BULL"
        return "CHOP"

    def _target_delta(self, vol_score: float) -> float:
        if vol_score >= 0.80:
            return 0.30
        if vol_score >= 0.60:
            return 0.35
        return 0.45

    def _maybe_open(self, i: int, row: Dict[str, Any]) -> None:
        t = row["ts"].time()
        if t < datetime.strptime(self.cfg["entry_start"], "%H:%M").time() or t > datetime.strptime(self.cfg["entry_end"], "%H:%M").time():
            self.pending_reject = None
            return
        if self.block_new_entry:
            self.pending_reject = None
            return
        if self.trades_today >= int(self.cfg["max_trades_per_day"]):
            self.pending_reject = None
            return

        regime = self._regime(row)
        if regime == "CHOP":
            self.pending_reject = None
            return

        ma10_1m = row.get("ma10_1m")
        if not pd.notna(ma10_1m):
            return

        is_a_plus = bool(row.get("is_near_ma200", False))
        setup = "A+" if is_a_plus else "A"

        vol_score = float(row.get("vol_score", 0.5))
        if (setup == "A") and (vol_score < float(self.cfg["vol_gate_threshold"])):
            return

        # Confirm pending reject on next bar.
        if self.pending_reject is not None:
            pr = self.pending_reject
            if i == pr["idx"] + 1:
                if pr["side"] == "PUT" and row["close"] < pr["reject_low"]:
                    self._open_position(i, row, "PUT", regime, setup, pr)
                    self.pending_reject = None
                    return
                if pr["side"] == "CALL" and row["close"] > pr["reject_high"]:
                    self._open_position(i, row, "CALL", regime, setup, pr)
                    self.pending_reject = None
                    return
            if i > pr["idx"] + 1:
                self.pending_reject = None

        # Trigger touch -> reject candidate
        high, low, close, open_ = float(row["high"]), float(row["low"]), float(row["close"]), float(row["open"])
        upper_wick_ratio = float(row.get("upper_wick_ratio", 0.0))
        lower_wick_ratio = float(row.get("lower_wick_ratio", 0.0))
        close_low_ratio = float(row.get("close_low_ratio", 0.5))
        close_high_ratio = float(row.get("close_high_ratio", 0.5))

        if regime == "BEAR":
            touch = high >= ma10_1m
            if touch:
                conds = [
                    close < ma10_1m,
                    upper_wick_ratio >= float(self.cfg["reject_wick_ratio_min"]),
                    close_low_ratio <= float(self.cfg["reject_close_low_ratio_max"]),
                ]
                if sum(bool(x) for x in conds) >= 2:
                    self.pending_reject = {
                        "idx": i,
                        "side": "PUT",
                        "reject_low": low,
                        "reject_high": high,
                        "reason": f"{setup}|BEAR|touch_ma10_reject",
                    }

        if regime == "BULL":
            touch = low <= ma10_1m
            if touch:
                conds = [
                    close > ma10_1m,
                    lower_wick_ratio >= float(self.cfg["reject_wick_ratio_min"]),
                    close_high_ratio <= float(self.cfg["reject_close_low_ratio_max"]),
                ]
                if sum(bool(x) for x in conds) >= 2:
                    self.pending_reject = {
                        "idx": i,
                        "side": "CALL",
                        "reject_low": low,
                        "reject_high": high,
                        "reason": f"{setup}|BULL|touch_ma10_reject",
                    }

    def _open_position(self, i: int, row: Dict[str, Any], side: str, regime: str, setup: str, pr: Dict[str, Any]) -> None:
        spot = float(row["close"])
        vol_score = float(row.get("vol_score", 0.5))
        delta_target = self._target_delta(vol_score)
        iv = float(row.get("iv_proxy", 0.25))
        minutes_to_close = max(1.0, ((16 * 60) - (row["ts"].hour * 60 + row["ts"].minute)))
        tau = minutes_to_close / (252.0 * 390.0)
        strike = _pick_strike_by_delta(spot, iv, tau, side, delta_target, float(self.cfg["strike_step"]))
        prem, _ = _bs_price_delta(spot, strike, tau, iv, side)
        if prem <= 0:
            return

        total_contracts = int(self.cfg.get("contracts_per_trade", 3))
        if total_contracts <= 0:
            return
        max_capital_use_pct = float(self.cfg.get("max_capital_use_pct", 1.0))
        budget = max(0.0, float(self.equity) * max(0.0, min(1.0, max_capital_use_pct)))
        affordable_contracts = int(budget // (prem * 100.0)) if prem > 0 else 0
        total_contracts = min(total_contracts, affordable_contracts)
        if total_contracts <= 0:
            return
        probe_qty = total_contracts
        add_qty = 0

        struct_stop = float(pr["reject_high"] if side == "PUT" else pr["reject_low"])
        risk_dist = abs(struct_stop - spot)
        risk_dist = max(risk_dist, max(0.05, float(row.get("atr_1m", 0.2)) * 0.6))
        tp1_level = spot - 1.2 * risk_dist if side == "PUT" else spot + 1.2 * risk_dist

        self.pos = Position(
            side=side,
            setup=setup,
            regime=regime,
            strike=strike,
            delta_target=delta_target,
            risk_dist=risk_dist,
            struct_stop=struct_stop,
            tp1_level=tp1_level,
            entry_under=spot,
            entry_ts=row["ts"],
            entry_idx=i,
            reject_ref=struct_stop,
            entry_reason=pr["reason"],
            legs=[Leg(qty=probe_qty, entry_premium=prem, entry_ts=row["ts"])],
            tp1_done=False,
            runner_reached_100=False,
            best_fav_move=0.0,
            best_fav_idx=i,
            worst_adverse_move=0.0,
        )
        self.pos._add_qty = add_qty  # type: ignore[attr-defined]
        self.trades_today += 1

    def _current_option_premium(self, row: Dict[str, Any], side: str, strike: float) -> float:
        spot = float(row["close"])
        iv = float(row.get("iv_proxy", 0.25))
        minutes_to_close = max(1.0, ((16 * 60) - (row["ts"].hour * 60 + row["ts"].minute)))
        tau = minutes_to_close / (252.0 * 390.0)
        prem, _ = _bs_price_delta(spot, strike, tau, iv, side)
        return max(0.01, float(prem))

    def _close_qty_fifo(self, row: Dict[str, Any], qty_to_close: int, reason: str) -> float:
        assert self.pos is not None
        px = self._current_option_premium(row, self.pos.side, self.pos.strike)
        remaining = qty_to_close
        realized = 0.0
        while remaining > 0 and self.pos.legs:
            leg = self.pos.legs[0]
            close_qty = min(remaining, leg.qty)
            pnl = (px - leg.entry_premium) * close_qty * 100.0
            realized += pnl

            # Trade log as completed round-trip block.
            self.trades.append(
                {
                    "date": str(row["date"]),
                    "side": self.pos.side,
                    "setup": self.pos.setup,
                    "regime": self.pos.regime,
                    "entry_time": leg.entry_ts.isoformat(),
                    "exit_time": row["ts"].isoformat(),
                    "entry_reason": self.pos.entry_reason,
                    "exit_reason": reason,
                    "strike": self.pos.strike,
                    "delta_target": self.pos.delta_target,
                    "qty": close_qty,
                    "entry_premium": leg.entry_premium,
                    "exit_premium": px,
                    "entry_under": self.pos.entry_under,
                    "exit_under": float(row["close"]),
                    "pnl": pnl,
                    "mae_pts": self.pos.worst_adverse_move,
                    "mfe_pts": self.pos.best_fav_move,
                    "hold_minutes": (row["ts"] - leg.entry_ts).total_seconds() / 60.0,
                }
            )

            leg.qty -= close_qty
            remaining -= close_qty
            if leg.qty <= 0:
                self.pos.legs.pop(0)

        self._book_realized(realized)
        return realized

    def _book_realized(self, pnl: float) -> None:
        self.equity += pnl
        self.daily_realized += pnl
        rec = self.daily[self.current_day]
        rec["day_pnl"] += pnl
        rec["cum_pnl"] = self.equity - float(self.cfg["starting_cash"])
        if pnl >= 0:
            rec["gross_profit"] += pnl
            rec["wins"] += 1
        else:
            rec["gross_loss"] += pnl
            rec["losses"] += 1
        rec["trades"] += 1

        if self.daily_realized <= -float(self.cfg["starting_cash"]) * float(self.cfg["daily_stop_pct"]):
            self.block_new_entry = True

    def _close_all(self, i: int, row: Dict[str, Any], reason: str) -> None:
        if self.pos is None:
            return
        total_qty = sum(l.qty for l in self.pos.legs)
        if total_qty > 0:
            self._close_qty_fifo(row, total_qty, reason)
        self.pos = None

    def _manage_position(self, i: int, row: Dict[str, Any]) -> None:
        assert self.pos is not None

        total_qty = sum(l.qty for l in self.pos.legs)
        if total_qty <= 0:
            self.pos = None
            return
        px = self._current_option_premium(row, self.pos.side, self.pos.strike)
        avg_entry = sum(l.qty * l.entry_premium for l in self.pos.legs) / max(1, total_qty)
        opt_return = (px / avg_entry) - 1.0 if avg_entry > 0 else 0.0

        # Update MFE/MAE on underlying points.
        if self.pos.side == "PUT":
            fav = self.pos.entry_under - float(row["low"])
            adv = float(row["high"]) - self.pos.entry_under
        else:
            fav = float(row["high"]) - self.pos.entry_under
            adv = self.pos.entry_under - float(row["low"])
        if fav > self.pos.best_fav_move:
            self.pos.best_fav_move = fav
            self.pos.best_fav_idx = i
        if adv > self.pos.worst_adverse_move:
            self.pos.worst_adverse_move = adv

        # Hard structure stop.
        ma10 = row.get("ma10_1m")
        close = float(row["close"])
        if pd.notna(ma10):
            ma10 = float(ma10)
            if self.pos.side == "PUT":
                if close > ma10 and close > self.pos.reject_ref:
                    self._close_all(i, row, "STRUCT_STOP")
                    return
            else:
                if close < ma10 and close < self.pos.reject_ref:
                    self._close_all(i, row, "STRUCT_STOP")
                    return

        # Requested options PnL ladder:
        # +30%: close 2 contracts; runner follows trend
        # +100%: close runner immediately
        # (fallback) if runner once reached +100% and drops to +50%, force exit
        if not self.pos.tp1_done:
            tp1_return = float(self.cfg.get("tp1_return_pct", 0.30))
            tp1_close_qty = int(self.cfg.get("tp1_close_qty", 2))
            if opt_return >= tp1_return:
                close_qty = min(tp1_close_qty, total_qty)
                self._close_qty_fifo(row, close_qty, "TP1_30PCT_LOCK")
                self.pos.tp1_done = True
                if sum(l.qty for l in self.pos.legs) <= 0:
                    self.pos = None
                    return

        if self.pos and self.pos.tp1_done:
            runner_tp = float(self.cfg.get("runner_take_profit_pct", 1.00))
            runner_floor = float(self.cfg.get("runner_drop_floor_pct", 0.50))
            if opt_return >= runner_tp:
                self.pos.runner_reached_100 = True
                self._close_all(i, row, "RUNNER_TP_100PCT")
                return
            if self.pos.runner_reached_100 and opt_return <= runner_floor:
                self._close_all(i, row, "RUNNER_DROP_TO_50PCT")
                return

        # Keep MA10 trailing as a secondary risk-off for runner.
        if self.pos and self.pos.tp1_done and pd.notna(ma10):
            if self.pos.side == "PUT" and close > ma10:
                self._close_all(i, row, "TP2_TRAIL_MA10")
                return
            if self.pos.side == "CALL" and close < ma10:
                self._close_all(i, row, "TP2_TRAIL_MA10")
                return

        # Wick exhaustion rule.
        if self.pos and self.pos.tp1_done:
            if self.pos.side == "PUT":
                if float(row.get("lower_wick_ratio", 0.0)) >= float(row.get("lower_wick_q80", 0.7)) and close > float(row["open"]):
                    self._close_all(i, row, "WICK_EXHAUST_EXIT")
                    return
            else:
                if float(row.get("upper_wick_ratio", 0.0)) >= float(row.get("upper_wick_q80", 0.7)) and close < float(row["open"]):
                    self._close_all(i, row, "WICK_EXHAUST_EXIT")
                    return

        # Time stop: no expansion.
        if self.pos:
            hold_bars = i - self.pos.entry_idx
            stall_bars = i - self.pos.best_fav_idx
            if hold_bars >= int(self.cfg["time_stop_bars"]) and self.pos.best_fav_move < (0.35 * self.pos.risk_dist):
                self._close_all(i, row, "TIME_STOP_NO_EXPANSION")
                return
            if stall_bars >= int(self.cfg["time_stop_bars"]):
                self._close_all(i, row, "TIME_STOP_STALL")
                return


def run_backtest(start: str, end: str, out_dir: Path, cfg_override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    raw = load_spy_1m(start, end)
    if raw.empty:
        raise RuntimeError("No SPY 1m data found for the selected range.")

    df = add_features(raw)

    cfg = {
        "starting_cash": 100000.0,
        "risk_per_trade_pct": 0.005,
        "daily_stop_pct": 0.015,
        "max_trades_per_day": 2,
        "contracts_per_trade": 3,
        "max_capital_use_pct": 1.0,
        "tp1_return_pct": 0.30,
        "tp1_close_qty": 2,
        "runner_take_profit_pct": 1.00,
        "runner_drop_floor_pct": 0.50,
        "entry_start": "09:40",
        "entry_end": "15:20",
        "vol_gate_threshold": 0.50,
        "reject_wick_ratio_min": 0.35,
        "reject_close_low_ratio_max": 0.35,
        "time_stop_bars": 8,
        "strike_step": 1.0,
    }
    if cfg_override:
        cfg.update(cfg_override)

    bt = Backtester(df, cfg)
    result = bt.run()

    out_dir.mkdir(parents=True, exist_ok=True)
    result["trades"].to_csv(out_dir / "trades.csv", index=False)
    result["daily"].to_csv(out_dir / "daily.csv", index=False)
    result["equity_curve"].to_csv(out_dir / "equity_curve.csv", index=False)

    summary_payload = {
        "config": cfg,
        "summary": result["summary"],
        "notes": {
            "data": "SPY 1m real bars from trading.bars_1m_ibkr FINAL",
            "options_layer": "0DTE synthetic pricing (delta-target + BS) due to no SPY options_quotes_1m in DB",
        },
    }
    (out_dir / "summary.json").write_text(json.dumps(summary_payload, ensure_ascii=True, indent=2), encoding="utf-8")

    return summary_payload


def main() -> None:
    ap = argparse.ArgumentParser(description="Backtest SPY 0DTE MA10 Reject strategy")
    ap.add_argument("--start", default="2026-01-01")
    ap.add_argument("--end", default="2026-03-05")
    ap.add_argument("--out-dir", default="backtest_results/spy_0dte_ma10_reject_v1")
    ap.add_argument("--config-json", default=None, help="Optional JSON file to override strategy config")
    args = ap.parse_args()

    cfg_override = None
    if args.config_json:
        cfg_override = json.loads(Path(args.config_json).read_text(encoding="utf-8"))

    payload = run_backtest(args.start, args.end, Path(args.out_dir), cfg_override=cfg_override)
    s = payload["summary"]
    print("=== SPY 0DTE MA10 Reject Backtest ===")
    print(f"Range: {s['start']} -> {s['end']}")
    print(f"Trades: {s['n_trades']} (win rate {s['win_rate']:.2%})")
    print(f"Realized PnL: {s['realized_pnl']:.2f}")
    print(f"Return: {s['return_pct']:.2%}")
    print(f"MDD: {s['max_drawdown_pct']:.2%}")


if __name__ == "__main__":
    main()
