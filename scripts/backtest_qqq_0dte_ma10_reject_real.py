#!/usr/bin/env python3
"""QQQ 0DTE MA10 Reject backtest with real option chain quotes.

- Signal layer: MA10 reject + 5m regime + vol gate (same family as SPY v2)
- Execution layer: real quotes from trading.options_quotes_1m (dte 0/1)
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from libs.db.ch_pool import get_client


def load_1m(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    q = """
    SELECT ts_start AS ts, symbol, open, high, low, close, vwap, volume
    FROM trading.bars_1m_ibkr FINAL
    WHERE symbol = %(s)s
      AND is_rth = 1
      AND ts_start >= %(start)s
      AND ts_start < %(end)s
    ORDER BY ts_start
    """
    ch = get_client()
    rows = ch.query(
        q,
        {"s": symbol, "start": f"{start_date} 00:00:00", "end": f"{end_date} 00:00:00"},
        settings={"use_query_cache": False},
    )
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"]).dt.tz_localize(None)
    return df.sort_values("ts").reset_index(drop=True)


def load_option_cache(underlying: str, start_date: str, end_date: str) -> Dict[Tuple[datetime, str], List[Dict[str, Any]]]:
    q = """
    SELECT ts, expiry, strike, right, dte, bid, ask, mid, iv, delta, gamma, theta, vega
    FROM trading.options_quotes_1m
    WHERE underlying = %(u)s
      AND ts >= %(start)s
      AND ts < %(end)s
      AND dte IN (0, 1)
      AND mid > 0
    ORDER BY ts
    """
    ch = get_client()
    rows = ch.query(
        q,
        {"u": underlying, "start": f"{start_date} 00:00:00", "end": f"{end_date} 00:00:00"},
        settings={"use_query_cache": False},
    )
    cache: Dict[Tuple[datetime, str], List[Dict[str, Any]]] = {}
    for r in rows:
        ts = pd.Timestamp(r["ts"]).to_pydatetime().replace(second=0, microsecond=0, tzinfo=None)
        right = str(r["right"])
        key = (ts, right)
        cache.setdefault(key, []).append(
            {
                "strike": float(r.get("strike") or 0.0),
                "dte": int(r.get("dte") or 0),
                "bid": float(r.get("bid") or 0.0),
                "ask": float(r.get("ask") or 0.0),
                "mid": float(r.get("mid") or 0.0),
                "iv": float(r.get("iv") or 0.0),
                "delta": float(r.get("delta") or 0.0),
                "gamma": float(r.get("gamma") or 0.0),
                "theta": float(r.get("theta") or 0.0),
                "vega": float(r.get("vega") or 0.0),
            }
        )
    return cache


def add_features(df: pd.DataFrame, vol_lookback_days: int = 20) -> pd.DataFrame:
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
    out["ma10_1m"] = out["close"].rolling(10, min_periods=10).mean()

    rng = (out["high"] - out["low"]).replace(0, np.nan)
    out["upper_wick_ratio"] = ((out["high"] - out[["open", "close"]].max(axis=1)) / rng).fillna(0.0)
    out["lower_wick_ratio"] = ((out[["open", "close"]].min(axis=1) - out["low"]) / rng).fillna(0.0)
    out["close_low_ratio"] = ((out["close"] - out["low"]) / rng).fillna(0.5)
    out["close_high_ratio"] = ((out["high"] - out["close"]) / rng).fillna(0.5)
    out["lower_wick_q80"] = out["lower_wick_ratio"].rolling(120, min_periods=30).quantile(0.8).fillna(0.5)
    out["upper_wick_q80"] = out["upper_wick_ratio"].rolling(120, min_periods=30).quantile(0.8).fillna(0.5)

    out["bar5"] = out["ts"].dt.floor("5min")
    agg = (
        out.groupby(["date", "bar5"], as_index=False)
        .agg(open_5m=("open", "first"), high_5m=("high", "max"), low_5m=("low", "min"), close_5m=("close", "last"), volume_5m=("volume", "sum"))
        .sort_values(["date", "bar5"])
    )
    for col, win in [("ma10_5m", 10), ("ma20_5m", 20), ("ma200_5m", 200)]:
        agg[col] = agg.groupby("date")["close_5m"].transform(lambda s: s.rolling(win, min_periods=win).mean())
    agg["slope_ma10_5m"] = agg.groupby("date")["ma10_5m"].diff()

    out["cum_pv_day"] = (out["close"] * out["volume"]).groupby(out["date"]).cumsum()
    out["cum_vol_day"] = out["volume"].groupby(out["date"]).cumsum().replace(0, np.nan)
    out["vwap_day"] = (out["cum_pv_day"] / out["cum_vol_day"]).fillna(out["close"])

    out = out.merge(
        agg[["date", "bar5", "close_5m", "ma10_5m", "ma20_5m", "ma200_5m", "slope_ma10_5m"]],
        on=["date", "bar5"],
        how="left",
    )

    out["atr_5m_proxy"] = out.groupby("date")["close_5m"].transform(lambda s: s.diff().abs().rolling(14, min_periods=5).mean())
    out["dist_ma200"] = (out["close_5m"] - out["ma200_5m"]).abs() / (out["atr_5m_proxy"].replace(0, np.nan))
    out["near_ma200_th"] = out["dist_ma200"].rolling(2000, min_periods=200).quantile(0.30)
    out["is_near_ma200"] = out["dist_ma200"] <= out["near_ma200_th"]

    vol_scores: List[float] = []
    minute_hist: Dict[int, List[Tuple[datetime.date, float]]] = {}
    for r in out.itertuples(index=False):
        m = int(r.minute_of_day)
        d = r.date
        x = float(r.atr_1m) if pd.notna(r.atr_1m) else np.nan
        hist = minute_hist.get(m, [])
        vals = [v for dd, v in hist if (d - dd).days <= vol_lookback_days]
        if not vals or not np.isfinite(x):
            score = 0.5
        else:
            arr = np.asarray(vals)
            score = float((arr <= x).sum() / len(arr))
        vol_scores.append(score)
        if np.isfinite(x):
            hist.append((d, x))
            minute_hist[m] = hist
    out["vol_score"] = vol_scores
    return out


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
    best_fav_move: float = 0.0
    best_fav_idx: int = 0
    worst_adverse_move: float = 0.0


class Backtester:
    def __init__(self, df: pd.DataFrame, option_cache: Dict[Tuple[datetime, str], List[Dict[str, Any]]], cfg: Dict[str, Any]):
        self.df = df.reset_index(drop=True)
        self.option_cache = option_cache
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

    def _lookup_chain_candidates(self, ts: datetime, side: str) -> List[Dict[str, Any]]:
        right = "C" if side == "CALL" else "P"
        key = (ts.replace(second=0, microsecond=0), right)
        rows = self.option_cache.get(key, [])
        if not rows:
            return []
        # Prefer true 0DTE rows; fallback to 1DTE.
        rows0 = [r for r in rows if int(r.get("dte", 99)) == 0 and float(r.get("mid", 0.0)) > 0]
        if rows0:
            return rows0
        return [r for r in rows if int(r.get("dte", 99)) <= 1 and float(r.get("mid", 0.0)) > 0]

    def _entry_contract(self, ts: datetime, side: str, spot: float, target_abs_delta: float) -> Optional[Dict[str, Any]]:
        rows = self._lookup_chain_candidates(ts, side)
        if not rows:
            return None
        # choose closest by |abs(delta)-target| then closest strike to spot
        best = min(
            rows,
            key=lambda r: (
                abs(abs(float(r.get("delta", 0.0))) - target_abs_delta),
                abs(float(r.get("strike", 0.0)) - spot),
            ),
        )
        return best

    def _mtm_premium(self, ts: datetime, side: str, strike: float) -> Optional[float]:
        rows = self._lookup_chain_candidates(ts, side)
        if not rows:
            return None
        best = min(rows, key=lambda r: abs(float(r.get("strike", 0.0)) - strike))
        mid = float(best.get("mid", 0.0))
        return mid if mid > 0 else None

    def _regime(self, row: Dict[str, Any]) -> str:
        c5, m10, m20, s10, vwap = row.get("close_5m"), row.get("ma10_5m"), row.get("ma20_5m"), row.get("slope_ma10_5m"), row.get("vwap_day")
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

    def _on_new_day(self, d: datetime.date) -> None:
        self.current_day = d
        self.trades_today = 0
        self.daily_realized = 0.0
        self.block_new_entry = False
        self.pending_reject = None
        self.daily.setdefault(
            d,
            {"date": str(d), "trades": 0, "wins": 0, "losses": 0, "gross_profit": 0.0, "gross_loss": 0.0, "day_pnl": 0.0, "cum_pnl": self.equity - float(self.cfg["starting_cash"])},
        )

    def _book(self, pnl: float) -> None:
        self.equity += pnl
        self.daily_realized += pnl
        rec = self.daily[self.current_day]
        rec["day_pnl"] += pnl
        rec["cum_pnl"] = self.equity - float(self.cfg["starting_cash"])
        rec["trades"] += 1
        if pnl >= 0:
            rec["wins"] += 1
            rec["gross_profit"] += pnl
        else:
            rec["losses"] += 1
            rec["gross_loss"] += pnl
        if self.daily_realized <= -float(self.cfg["starting_cash"]) * float(self.cfg["daily_stop_pct"]):
            self.block_new_entry = True

    def _close_qty_fifo(self, row: Dict[str, Any], qty_to_close: int, reason: str) -> None:
        assert self.pos is not None
        px = self._mtm_premium(row["ts"], self.pos.side, self.pos.strike)
        if px is None:
            # if missing quote this minute, skip closing (conservative)
            return
        remain = qty_to_close
        while remain > 0 and self.pos.legs:
            leg = self.pos.legs[0]
            q = min(remain, leg.qty)
            pnl = (px - leg.entry_premium) * q * 100.0
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
                    "qty": q,
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
            self._book(pnl)
            leg.qty -= q
            remain -= q
            if leg.qty <= 0:
                self.pos.legs.pop(0)

    def _close_all(self, row: Dict[str, Any], reason: str) -> None:
        if self.pos is None:
            return
        total = sum(l.qty for l in self.pos.legs)
        if total > 0:
            self._close_qty_fifo(row, total, reason)
        if self.pos and sum(l.qty for l in self.pos.legs) == 0:
            self.pos = None

    def _maybe_open(self, i: int, row: Dict[str, Any]) -> None:
        t = row["ts"].time()
        if t < datetime.strptime(self.cfg["entry_start"], "%H:%M").time() or t > datetime.strptime(self.cfg["entry_end"], "%H:%M").time():
            self.pending_reject = None
            return
        if self.block_new_entry or self.trades_today >= int(self.cfg["max_trades_per_day"]):
            self.pending_reject = None
            return

        regime = self._regime(row)
        if regime == "CHOP":
            self.pending_reject = None
            return

        ma10_1m = row.get("ma10_1m")
        if not pd.notna(ma10_1m):
            return

        setup = "A+" if bool(row.get("is_near_ma200", False)) else "A"
        vol_score = float(row.get("vol_score", 0.5))
        if setup == "A" and vol_score < float(self.cfg["vol_gate_threshold"]):
            return

        # Confirm entry from pending reject
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

        high, low, close = float(row["high"]), float(row["low"]), float(row["close"])
        upper_wick_ratio = float(row.get("upper_wick_ratio", 0.0))
        lower_wick_ratio = float(row.get("lower_wick_ratio", 0.0))
        close_low_ratio = float(row.get("close_low_ratio", 0.5))
        close_high_ratio = float(row.get("close_high_ratio", 0.5))

        if regime == "BEAR" and high >= ma10_1m:
            conds = [
                close < ma10_1m,
                upper_wick_ratio >= float(self.cfg["reject_wick_ratio_min"]),
                close_low_ratio <= float(self.cfg["reject_close_low_ratio_max"]),
            ]
            if sum(bool(x) for x in conds) >= 2:
                self.pending_reject = {"idx": i, "side": "PUT", "reject_low": low, "reject_high": high, "reason": f"{setup}|BEAR|touch_ma10_reject"}

        if regime == "BULL" and low <= ma10_1m:
            conds = [
                close > ma10_1m,
                lower_wick_ratio >= float(self.cfg["reject_wick_ratio_min"]),
                close_high_ratio <= float(self.cfg["reject_close_low_ratio_max"]),
            ]
            if sum(bool(x) for x in conds) >= 2:
                self.pending_reject = {"idx": i, "side": "CALL", "reject_low": low, "reject_high": high, "reason": f"{setup}|BULL|touch_ma10_reject"}

    def _open_position(self, i: int, row: Dict[str, Any], side: str, regime: str, setup: str, pr: Dict[str, Any]) -> None:
        spot = float(row["close"])
        vol_score = float(row.get("vol_score", 0.5))
        delta_target = self._target_delta(vol_score)
        quote = self._entry_contract(row["ts"], side, spot, delta_target)
        if not quote:
            return
        prem = float(quote["mid"])
        strike = float(quote["strike"])
        if prem <= 0:
            return

        risk_usd = float(self.cfg["starting_cash"]) * float(self.cfg["risk_per_trade_pct"])
        total = max(1, int(risk_usd // (prem * 100.0)))
        probe = max(1, int(round(total * 0.7)))
        add = max(0, total - probe)

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
            legs=[Leg(qty=probe, entry_premium=prem, entry_ts=row["ts"])],
        )
        self.pos._add_qty = add  # type: ignore[attr-defined]
        self.trades_today += 1

    def _manage_position(self, i: int, row: Dict[str, Any]) -> None:
        assert self.pos is not None

        add_qty = int(getattr(self.pos, "_add_qty", 0))
        if add_qty > 0:
            if self.pos.side == "PUT" and float(row["low"]) < (self.pos.entry_under - 0.2 * self.pos.risk_dist):
                px = self._mtm_premium(row["ts"], self.pos.side, self.pos.strike)
                if px is not None:
                    self.pos.legs.append(Leg(qty=add_qty, entry_premium=px, entry_ts=row["ts"]))
                    self.pos._add_qty = 0  # type: ignore[attr-defined]
            if self.pos.side == "CALL" and float(row["high"]) > (self.pos.entry_under + 0.2 * self.pos.risk_dist):
                px = self._mtm_premium(row["ts"], self.pos.side, self.pos.strike)
                if px is not None:
                    self.pos.legs.append(Leg(qty=add_qty, entry_premium=px, entry_ts=row["ts"]))
                    self.pos._add_qty = 0  # type: ignore[attr-defined]

        if self.pos.side == "PUT":
            fav, adv = self.pos.entry_under - float(row["low"]), float(row["high"]) - self.pos.entry_under
        else:
            fav, adv = float(row["high"]) - self.pos.entry_under, self.pos.entry_under - float(row["low"])
        if fav > self.pos.best_fav_move:
            self.pos.best_fav_move, self.pos.best_fav_idx = fav, i
        if adv > self.pos.worst_adverse_move:
            self.pos.worst_adverse_move = adv

        ma10 = row.get("ma10_1m")
        close = float(row["close"])
        if pd.notna(ma10):
            ma10 = float(ma10)
            if self.pos.side == "PUT" and close > ma10 and close > self.pos.reject_ref:
                self._close_all(row, "STRUCT_STOP")
                return
            if self.pos.side == "CALL" and close < ma10 and close < self.pos.reject_ref:
                self._close_all(row, "STRUCT_STOP")
                return

        if not self.pos.tp1_done:
            hit = (close <= self.pos.tp1_level) if self.pos.side == "PUT" else (close >= self.pos.tp1_level)
            if hit:
                total = sum(l.qty for l in self.pos.legs)
                q = max(1, total // 2)
                self._close_qty_fifo(row, q, "TP1_LOCK")
                if self.pos is None:
                    return
                self.pos.tp1_done = True

        if self.pos and self.pos.tp1_done and pd.notna(ma10):
            if self.pos.side == "PUT" and close > ma10:
                self._close_all(row, "TP2_TRAIL_MA10")
                return
            if self.pos.side == "CALL" and close < ma10:
                self._close_all(row, "TP2_TRAIL_MA10")
                return

        if self.pos and self.pos.tp1_done:
            if self.pos.side == "PUT" and float(row.get("lower_wick_ratio", 0.0)) >= float(row.get("lower_wick_q80", 0.7)) and close > float(row["open"]):
                self._close_all(row, "WICK_EXHAUST_EXIT")
                return
            if self.pos.side == "CALL" and float(row.get("upper_wick_ratio", 0.0)) >= float(row.get("upper_wick_q80", 0.7)) and close < float(row["open"]):
                self._close_all(row, "WICK_EXHAUST_EXIT")
                return

        if self.pos:
            hold_bars = i - self.pos.entry_idx
            stall_bars = i - self.pos.best_fav_idx
            if hold_bars >= int(self.cfg["time_stop_bars"]) and self.pos.best_fav_move < (0.35 * self.pos.risk_dist):
                self._close_all(row, "TIME_STOP_NO_EXPANSION")
                return
            if stall_bars >= int(self.cfg["time_stop_bars"]):
                self._close_all(row, "TIME_STOP_STALL")
                return

    def run(self) -> Dict[str, Any]:
        for i, r in enumerate(self.df.itertuples(index=False)):
            row = r._asdict()
            d = row["date"]
            if self.current_day != d:
                self._on_new_day(d)

            if self.pos is not None:
                self._manage_position(i, row)
            if self.pos is None:
                self._maybe_open(i, row)

            self.equity_curve.append({"ts": row["ts"].isoformat(), "equity": self.equity})
            self.peak = max(self.peak, self.equity)
            dd = (self.peak - self.equity) / self.peak if self.peak > 0 else 0.0
            self.max_dd = max(self.max_dd, dd)

        if self.pos is not None:
            self._close_all(self.df.iloc[-1].to_dict(), "EOD_FORCE_EXIT")

        tr = pd.DataFrame(self.trades)
        dy = pd.DataFrame(self.daily.values()).sort_values("date") if self.daily else pd.DataFrame()
        total = float(tr["pnl"].sum()) if not tr.empty else 0.0
        wins = int((tr["pnl"] > 0).sum()) if not tr.empty else 0
        losses = int((tr["pnl"] < 0).sum()) if not tr.empty else 0
        summary = {
            "start": str(self.df["date"].min()) if not self.df.empty else None,
            "end": str(self.df["date"].max()) if not self.df.empty else None,
            "n_trades": int(len(tr)),
            "wins": wins,
            "losses": losses,
            "win_rate": (wins / len(tr)) if len(tr) > 0 else 0.0,
            "realized_pnl": total,
            "return_pct": total / float(self.cfg["starting_cash"]),
            "max_drawdown_pct": self.max_dd,
            "avg_trade_pnl": (total / len(tr)) if len(tr) > 0 else 0.0,
            "daily_avg_pnl": float(dy["day_pnl"].mean()) if not dy.empty else 0.0,
        }
        return {"summary": summary, "trades": tr, "daily": dy, "equity_curve": pd.DataFrame(self.equity_curve)}


def run_backtest(start: str, end: str, out_dir: Path) -> Dict[str, Any]:
    bars = load_1m("QQQ", start, end)
    if bars.empty:
        raise RuntimeError("No QQQ bars found")
    quotes = load_option_cache("QQQ", start, end)
    if not quotes:
        raise RuntimeError("No QQQ option quotes found in selected range")

    df = add_features(bars)

    cfg = {
        "starting_cash": 100000.0,
        "risk_per_trade_pct": 0.005,
        "daily_stop_pct": 0.015,
        "max_trades_per_day": 1,
        "entry_start": "09:40",
        "entry_end": "14:30",
        "vol_gate_threshold": 0.6,
        "reject_wick_ratio_min": 0.35,
        "reject_close_low_ratio_max": 0.30,
        "time_stop_bars": 8,
    }

    bt = Backtester(df, quotes, cfg)
    res = bt.run()

    out_dir.mkdir(parents=True, exist_ok=True)
    res["trades"].to_csv(out_dir / "trades.csv", index=False)
    res["daily"].to_csv(out_dir / "daily.csv", index=False)
    res["equity_curve"].to_csv(out_dir / "equity_curve.csv", index=False)

    payload = {
        "config": cfg,
        "summary": res["summary"],
        "notes": {
            "underlying_data": "trading.bars_1m_ibkr FINAL (QQQ, RTH)",
            "options_data": "trading.options_quotes_1m (QQQ, dte 0/1, real chain)",
        },
    }
    (out_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
    return payload


def main() -> None:
    ap = argparse.ArgumentParser(description="QQQ 0DTE MA10 Reject real-chain backtest")
    ap.add_argument("--start", default="2026-02-03")
    ap.add_argument("--end", default="2026-03-05")
    ap.add_argument("--out-dir", default="backtest_results/qqq_0dte_ma10_reject_real_v1")
    args = ap.parse_args()

    payload = run_backtest(args.start, args.end, Path(args.out_dir))
    s = payload["summary"]
    print("=== QQQ 0DTE MA10 Reject (Real Chain) ===")
    print(f"Range: {s['start']} -> {s['end']}")
    print(f"Trades: {s['n_trades']} (win rate {s['win_rate']:.2%})")
    print(f"Realized PnL: {s['realized_pnl']:.2f}")
    print(f"Return: {s['return_pct']:.2%}")
    print(f"MDD: {s['max_drawdown_pct']:.2%}")


if __name__ == "__main__":
    main()
