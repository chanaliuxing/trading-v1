"""Audit and result helpers for the option replay engine."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any, Dict, List

import pandas as pd


def record_skip(engine: Any, ts: datetime, reason: str, **extra: Any) -> None:
    payload = {"ts": ts.isoformat(), "reason": reason}
    payload.update(extra)
    engine.skips.append(payload)


def record_entry_audit(engine: Any, ts: datetime, status: str, **extra: Any) -> None:
    payload = {"ts": ts.isoformat(), "status": status}
    payload.update(extra)
    engine.entry_audit.append(payload)


def record_selection_audit(engine: Any, ts: datetime, **extra: Any) -> None:
    payload = {"ts": ts.isoformat()}
    payload.update(extra)
    engine.selection_audit.append(payload)


def record_bracket_audit(engine: Any, ts: datetime, event: str, **extra: Any) -> None:
    payload = {"ts": ts.isoformat(), "event": event}
    payload.update(extra)
    engine.bracket_audit.append(payload)


def record_order(engine: Any, ts: datetime, order_id: str, status: str, **extra: Any) -> None:
    payload = {"ts": ts.isoformat(), "order_id": order_id, "status": status}
    payload.update(extra)
    engine.orders.append(payload)


def record_fill(engine: Any, ts: datetime, order_id: str, fill_price: float, qty: int, **extra: Any) -> None:
    payload = {
        "ts": ts.isoformat(),
        "fill_id": f"fill_{len(engine.fills) + 1}",
        "order_id": order_id,
        "fill_price": float(fill_price),
        "qty": int(qty),
    }
    payload.update(extra)
    engine.fills.append(payload)


def current_nav(engine: Any) -> float:
    nav = engine.cash
    if engine.position is not None and engine.position.last_quote is not None:
        nav += engine.position.remaining * engine.position.last_quote.mid * engine.cfg.contract_multiplier
    return nav


def _build_selection_candidates_df(selection_audit_rows: List[Dict[str, Any]]) -> pd.DataFrame:
    rows: List[Dict[str, Any]] = []
    for selection_idx, row in enumerate(selection_audit_rows, start=1):
        ts = row.get("ts")
        base = {
            "selection_id": selection_idx,
            "ts": ts,
            "side": row.get("side"),
            "grade": row.get("grade"),
            "regime": row.get("regime"),
            "right": row.get("right"),
            "spot": row.get("spot"),
            "selected": row.get("selected"),
            "rejection_reason": row.get("rejection_reason"),
        }
        candidates = row.get("top_candidates") or []
        if not isinstance(candidates, list):
            candidates = []
        for rank, candidate in enumerate(candidates, start=1):
            candidate_row = dict(base)
            candidate_row.update(
                {
                    "candidate_rank": rank,
                    "candidate_expiry": candidate.get("expiry"),
                    "candidate_strike": candidate.get("strike"),
                    "candidate_dte": candidate.get("dte"),
                    "candidate_delta": candidate.get("delta"),
                    "candidate_bid": candidate.get("bid"),
                    "candidate_ask": candidate.get("ask"),
                    "candidate_mid": candidate.get("mid"),
                    "candidate_spread_pct": candidate.get("spread_pct"),
                    "candidate_open_interest": candidate.get("open_interest"),
                }
            )
            rows.append(candidate_row)
    return pd.DataFrame(rows)


def build_run_result(engine: Any) -> Dict[str, Any]:
    trades_df = pd.DataFrame(engine.trades)
    equity_df = pd.DataFrame(engine.equity_curve)
    selection_audit_df = pd.DataFrame(engine.selection_audit)
    selection_candidates_df = _build_selection_candidates_df(engine.selection_audit)
    if engine.trade_start_date is not None and not engine.df.empty:
        trade_df = engine.df[engine.df["date"] >= engine.trade_start_date]
    else:
        trade_df = engine.df
    exit_legs = int(len(trades_df))
    entry_count = exit_legs
    round_trip_win_rate = float((trades_df["pnl"] > 0).mean()) if not trades_df.empty else 0.0
    avg_pnl_per_entry = float(trades_df["pnl"].mean()) if not trades_df.empty else 0.0
    avg_legs_per_entry = 1.0 if entry_count > 0 else 0.0
    if not trades_df.empty and "entry_order_id" in trades_df.columns:
        grouped = (
            trades_df.groupby("entry_order_id", dropna=False)
            .agg(
                entry_pnl=("pnl", "sum"),
                exit_legs=("pnl", "size"),
            )
            .reset_index()
        )
        entry_count = int(len(grouped))
        round_trip_win_rate = float((grouped["entry_pnl"] > 0).mean()) if not grouped.empty else 0.0
        avg_pnl_per_entry = float(grouped["entry_pnl"].mean()) if not grouped.empty else 0.0
        avg_legs_per_entry = float(grouped["exit_legs"].mean()) if not grouped.empty else 0.0

    summary = {
        "start": str(trade_df["date"].min()) if not trade_df.empty else None,
        "end": str(trade_df["date"].max()) if not trade_df.empty else None,
        "trades": exit_legs,
        "exit_legs": exit_legs,
        "entry_count": entry_count,
        "realized_pnl": float(trades_df["pnl"].sum()) if not trades_df.empty else 0.0,
        "win_rate": float((trades_df["pnl"] > 0).mean()) if not trades_df.empty else 0.0,
        "round_trip_win_rate": round_trip_win_rate,
        "avg_pnl_per_entry": avg_pnl_per_entry,
        "avg_legs_per_entry": avg_legs_per_entry,
        "skipped_signals": int(len(engine.skips)),
        "final_cash": float(engine.cash),
    }
    if not equity_df.empty:
        peak = equity_df["equity"].cummax()
        drawdown = ((peak - equity_df["equity"]) / peak.replace(0, pd.NA)).fillna(0.0)
        summary["max_drawdown_pct"] = float(drawdown.max())
    else:
        summary["max_drawdown_pct"] = 0.0
    return {
        "summary": summary,
        "trades": trades_df,
        "skips": pd.DataFrame(engine.skips),
        "entry_audit": pd.DataFrame(engine.entry_audit),
        "selection_audit": selection_audit_df,
        "selection_candidates": selection_candidates_df,
        "bracket_audit": pd.DataFrame(engine.bracket_audit),
        "orders": pd.DataFrame(engine.orders),
        "fills": pd.DataFrame(engine.fills),
        "cash_ledger": pd.DataFrame(engine.cash_ledger),
        "equity_curve": equity_df,
        "config": asdict(engine.cfg),
    }


__all__ = [
    "build_run_result",
    "current_nav",
    "record_bracket_audit",
    "record_fill",
    "record_entry_audit",
    "record_order",
    "record_selection_audit",
    "record_skip",
]
