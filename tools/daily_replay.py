#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Daily Replay Summarizer (MVP)

Summarizes per-strategy and per-symbol activity for a trading day using
PostgreSQL core tables. Uses an approximate PnL (net cash flow) if positions
are not fully closed.

Outputs JSON + optional Markdown summary.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass, asdict
from datetime import timedelta
from pathlib import Path
from typing import Dict, Any, List, Optional

from libs.db.postgres import PostgresDB
from llm_trader.llm_trader.ny_time import now_ny


@dataclass
class SymbolReplay:
    symbol: str
    trades: int
    qty_buy: float
    qty_sell: float
    notional_buy: float
    notional_sell: float
    net_cash_flow: float  # approx PnL if flat


@dataclass
class StrategyReplay:
    strategy_id: str
    symbols: List[SymbolReplay]
    total_trades: int
    total_notional: float
    net_cash_flow: float


async def fetch_fills(db: PostgresDB, strategy_id: str, start_ts: str, end_ts: str) -> List[Dict[str, Any]]:
    sql = (
        "SELECT order_id, symbol, qty, price, commission_usd, ts "
        "FROM core.core_fill WHERE strategy_id = $1 AND ts >= $2 AND ts < $3 "
        "ORDER BY ts ASC"
    )
    return await db.fetchall(sql, strategy_id, start_ts, end_ts)


def summarize_symbol(rows: List[Dict[str, Any]], symbol: str) -> SymbolReplay:
    trades = 0
    qty_buy = qty_sell = 0.0
    notional_buy = notional_sell = 0.0
    for r in rows:
        q = float(r.get("qty") or 0.0)
        px = float(r.get("price") or 0.0)
        if q > 0:
            qty_buy += q
            notional_buy += q * px
        elif q < 0:
            qty_sell += abs(q)
            notional_sell += abs(q) * px
        trades += 1
    # Approx net cash flow (positive if net cash received)
    net_cash = notional_sell - notional_buy
    return SymbolReplay(symbol=symbol,
                        trades=trades,
                        qty_buy=qty_buy,
                        qty_sell=qty_sell,
                        notional_buy=notional_buy,
                        notional_sell=notional_sell,
                        net_cash_flow=net_cash)


def fifo_closed_pnl(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Compute closed PnL via FIFO pairing for a single symbol.

    Assumes qty>0 is buy, qty<0 is sell; price is fill price; commission_usd optional.
    Returns dict with closed_trades, realized_pnl, realized_pnl_net, win_rate, profit_factor.
    """
    # Build lots queue
    lots: List[Dict[str, float]] = []  # each: {qty, px}
    realized = 0.0
    realized_net = 0.0
    wins = 0
    losses = 0
    gross_win = 0.0
    gross_loss = 0.0
    closed_trades = 0

    for r in rows:
        q = float(r.get("qty") or 0.0)
        px = float(r.get("price") or 0.0)
        fee = float(r.get("commission_usd") or 0.0)
        if q > 0:  # buy
            lots.append({"qty": q, "px": px})
            realized_net -= fee
        elif q < 0:
            sell_qty = -q
            realized_net -= fee
            while sell_qty > 1e-9 and lots:
                lot = lots[0]
                match_qty = min(lot["qty"], sell_qty)
                pnl = (px - lot["px"]) * match_qty
                realized += pnl
                realized_net += pnl
                # Track trade outcome approximated per matched slice
                if pnl > 0:
                    gross_win += pnl
                    wins += 1
                elif pnl < 0:
                    gross_loss += pnl
                    losses += 1
                closed_trades += 1
                lot["qty"] -= match_qty
                sell_qty -= match_qty
                if lot["qty"] <= 1e-9:
                    lots.pop(0)
    total_closed = wins + losses
    win_rate = (wins / total_closed) if total_closed > 0 else 0.0
    pf = (gross_win / abs(gross_loss)) if gross_loss < 0 else None
    return {
        "closed_trades": total_closed,
        "realized_pnl": realized,
        "realized_pnl_net": realized_net,
        "win_rate": win_rate,
        "profit_factor": pf,
    }


async def replay_for_strategy(db: PostgresDB, strategy_id: str, day: str) -> StrategyReplay:
    ny0 = now_ny().replace(hour=0, minute=0, second=0, microsecond=0)
    if day:
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo as _ZI
        y, m, d = [int(x) for x in day.split("-")]
        ny0 = _dt(y, m, d, 0, 0, 0, tzinfo=_ZI("America/New_York"))
    ny1 = ny0 + timedelta(days=1)
    rows = await fetch_fills(db, strategy_id, ny0.isoformat(), ny1.isoformat())
    by_sym: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        by_sym.setdefault(r.get("symbol") or "?", []).append(r)
    symbols = []
    for s in sorted(by_sym.keys()):
        sr = summarize_symbol(by_sym[s], s)
        # Compute FIFO PnL metrics
        fifo = fifo_closed_pnl(by_sym[s])
        # Attach metrics for later JSON output (kept separate but added in payload below)
        sr._fifo = fifo  # type: ignore
        symbols.append(sr)
    total_trades = sum(s.trades for s in symbols)
    total_notional = sum(s.notional_buy + s.notional_sell for s in symbols)
    net_cash_flow = sum(s.net_cash_flow for s in symbols)
    return StrategyReplay(strategy_id=strategy_id,
                          symbols=symbols,
                          total_trades=total_trades,
                          total_notional=total_notional,
                          net_cash_flow=net_cash_flow)


def save_outputs(payload: Dict[str, Any], out_json: Optional[str], out_md: Optional[str]) -> None:
    if out_json:
        Path(out_json).parent.mkdir(parents=True, exist_ok=True)
        Path(out_json).write_text(json.dumps(payload, indent=2))
    if out_md:
        lines = ["# Daily Replay Summary", ""]
        for s in payload.get("strategies", []):
            lines.append(f"## {s['strategy_id']}")
            lines.append(f"- Total Trades: {s['total_trades']}")
            lines.append(f"- Total Notional: ${s['total_notional']:.2f}")
            lines.append(f"- Net Cash Flow (approx): ${s['net_cash_flow']:.2f}")
            lines.append("")
            lines.append("Symbol | Trades | BuyNotional | SellNotional | NetCash | Closed | WinRate | PF | RealizedNet")
            lines.append("------ | ------:| ----------: | -----------: | ------: | -----: | ------: | --:| ---------:")
            for sym in s.get("symbols", []):
                fifo = sym.get("fifo", {})
                lines.append(
                    f"{sym['symbol']} | {sym['trades']} | ${sym['notional_buy']:.2f} | ${sym['notional_sell']:.2f} | ${sym['net_cash_flow']:.2f} | "
                    f"{fifo.get('closed_trades','-')} | {fifo.get('win_rate','-')} | {fifo.get('profit_factor','-')} | ${fifo.get('realized_pnl_net','-')}")
            lines.append("")
        Path(out_md).parent.mkdir(parents=True, exist_ok=True)
        Path(out_md).write_text("\n".join(lines))


async def main_async(args):
    db = PostgresDB()
    await db.connect()
    strategies = args.strategies or []
    results = []
    for sid in strategies:
        rep = await replay_for_strategy(db, sid, args.day)
        symbols = []
        for s in rep.symbols:
            row = asdict(s)
            fifo = getattr(s, "_fifo", {})  # type: ignore
            row["fifo"] = fifo
            symbols.append(row)
        results.append({
            "strategy_id": rep.strategy_id,
            "symbols": symbols,
            "total_trades": rep.total_trades,
            "total_notional": rep.total_notional,
            "net_cash_flow": rep.net_cash_flow,
        })
    await db.close()
    payload = {"ok": True, "day": args.day or now_ny().date().isoformat(), "strategies": results}
    if args.json:
        print(json.dumps(payload, indent=2))
    save_outputs(payload, args.out_json, args.out_md)


def main():
    ap = argparse.ArgumentParser(description="Daily replay summarizer (MVP)")
    ap.add_argument("--strategies", nargs="+", help="Strategy IDs to summarize")
    ap.add_argument("--day", default=None, help="YYYY-MM-DD (NY)")
    ap.add_argument("--json", action="store_true", help="Print JSON to stdout")
    ap.add_argument("--out-json", default=None)
    ap.add_argument("--out-md", default=None)
    args = ap.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
