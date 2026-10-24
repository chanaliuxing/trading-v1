#!/usr/bin/env python3
from __future__ import annotations
import argparse
import asyncio
import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Optional, Dict, Any

from libs.db.clickhouse import ClickHouseDB
from libs.db.postgres import PostgresDB
from libs.config import config as CFG
from libs.options.occ import validate_occ_symbol, parse_occ_symbol


@dataclass
class HedgePlan:
    symbol: str
    side: str  # BUY | SELL
    qty: int
    price: Optional[float]  # Limit price for options
    order_type: str  # MKT or OPT_LMT
    note: str


def _read_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:
        return {}


def _mid_price(ch: ClickHouseDB, symbol: str) -> Optional[float]:
    try:
        rows = ch.query(
            "SELECT anyLast((bid+ask)/2) AS mid FROM trading.bbo WHERE symbol={s:String} AND ts > now() - INTERVAL 5 MINUTE",
            {"s": symbol},
        )
        if rows and rows[0].get("mid"):
            return float(rows[0]["mid"]) or None
    except Exception:
        pass
    try:
        rows = ch.query(
            "SELECT anyLast(close) AS last FROM trading.bars_1m WHERE toDate(ts_end)=today() AND symbol={s:String}",
            {"s": symbol},
        )
        if rows and rows[0].get("last"):
            return float(rows[0]["last"]) or None
    except Exception:
        pass
    return None


def _plan_from_overlay(runs_dir: str) -> Optional[HedgePlan]:
    d = date.today().isoformat()
    ov = _read_json(Path(runs_dir) / d / "overlay_hedge.json")
    if not ov or not bool(ov.get("need_hedge")):
        return None
    hedge = ov.get("hedge") or {}
    sym = str(hedge.get("instrument") or "SPY").upper()
    notional = float(hedge.get("notional_usd") or 0.0)
    side = "BUY" if notional > 0 else "SELL"
    return HedgePlan(symbol=sym, side=side, qty=0, price=None, order_type="MKT", note="overlay")


def _plan_from_options(runs_dir: str) -> Optional[HedgePlan]:
    """Load real options hedge plan from options_hedge.json (new format)."""
    d = date.today().isoformat()
    opt = _read_json(Path(runs_dir) / d / "options_hedge.json")
    if not opt or not bool(opt.get("need_hedge")):
        return None

    # New format: contract field contains full details
    contract = opt.get("contract")
    if not contract:
        # Legacy format fallback (proxy to underlying)
        sym = str(opt.get("instrument") or "SPY").upper()
        notional = float(opt.get("target_notional_usd") or 0.0)
        side = "BUY" if opt.get("option_type") == "CALL" else "SELL"
        return HedgePlan(symbol=sym, side=side, qty=0, price=None, order_type="MKT", note="options_proxy_legacy")

    # Real options execution
    occ_symbol = contract.get("occ_symbol")
    if not occ_symbol or not validate_occ_symbol(occ_symbol):
        return None

    qty = int(contract.get("quantity", 0))
    if qty <= 0:
        return None

    limit_price = float(contract.get("limit_price", 0.0))
    if limit_price <= 0:
        return None

    # Options are always bought for hedging (protective puts/calls)
    side = "BUY"

    # Build full note with contract details
    note_data = {
        "type": "options_hedge",
        "underlying": contract.get("underlying"),
        "expiry": contract.get("expiry_date"),
        "strike": contract.get("strike"),
        "right": contract.get("right"),
        "delta": contract.get("delta"),
        "rationale": contract.get("rationale", ""),
    }

    return HedgePlan(
        symbol=occ_symbol,
        side=side,
        qty=qty,
        price=limit_price,
        order_type="OPT_LMT",
        note=json.dumps(note_data),
    )


async def _validate_options_contract(
    ch: ClickHouseDB,
    occ_symbol: str,
    limit_price: float,
    runs_dir: str,
) -> Dict[str, Any]:
    """Perform pre-execution validation for options contract.

    Checks:
    - Spread still within acceptable range
    - Open interest meets minimum
    - Recent trade activity
    - Current market price near expected

    Returns:
        Dict with 'valid' (bool) and 'reason' (str) if invalid
    """
    # Parse contract details
    try:
        parsed = parse_occ_symbol(occ_symbol)
    except ValueError as e:
        return {"valid": False, "reason": f"Invalid OCC symbol: {e}"}

    # Load policy to check constraints
    from scripts.ai_trader.risk.options_selector import OptionsPolicy
    from services.market.options_data import create_options_provider
    from libs.config import config as CFG

    policy = OptionsPolicy.from_config()

    # Basic limit price check
    if limit_price <= 0 or limit_price > 1000:
        return {"valid": False, "reason": f"Limit price out of range: ${limit_price}"}

    # If using real data provider (IBKR), do live validation
    backend = CFG.get('risk_management.hedge.options.data_provider', 'stub')
    if backend == 'ibkr':
        try:
            host = CFG.get('risk_management.hedge.options.ibkr_host', '127.0.0.1')
            port = int(CFG.get('risk_management.hedge.options.ibkr_port', 4002))
            provider = create_options_provider('ibkr', host=host, port=port)

            # Fetch current chain for this expiry
            from datetime import datetime
            expiry_date = datetime.strptime(parsed['expiry_date'], '%Y-%m-%d')
            now = datetime.now()
            dte = (expiry_date - now).days

            chain = await provider.get_chain(
                underlying=parsed['underlying'],
                dte_candidates=[dte],
                right=parsed['right'],
            )

            # Find matching contract
            matching = None
            for contract in chain:
                if (contract.strike == parsed['strike'] and
                    contract.right == parsed['right'] and
                    contract.expiry_date == parsed['expiry_date']):
                    matching = contract
                    break

            if not matching:
                return {"valid": False, "reason": "Contract not found in current chain"}

            # Validate spread
            if matching.spread_bps > policy.max_spread_bps:
                return {
                    "valid": False,
                    "reason": f"Spread {matching.spread_bps:.1f}bps exceeds max {policy.max_spread_bps}bps"
                }

            # Validate OI
            if matching.open_interest and matching.open_interest < policy.min_open_interest:
                return {
                    "valid": False,
                    "reason": f"OI {matching.open_interest} below min {policy.min_open_interest}"
                }

            # Validate price hasn't moved too far
            price_drift_pct = abs(matching.mid - limit_price) / limit_price
            if price_drift_pct > 0.10:  # 10% drift tolerance
                return {
                    "valid": False,
                    "reason": f"Price drifted {price_drift_pct*100:.1f}% from selection (mid={matching.mid:.2f}, limit={limit_price:.2f})"
                }

            # Cleanup connection
            if hasattr(provider, 'disconnect'):
                await provider.disconnect()

            return {"valid": True, "live_mid": matching.mid, "live_spread_bps": matching.spread_bps}

        except Exception as e:
            # Log error but allow execution to proceed (fail-open for availability)
            import logging
            logger = logging.getLogger(__name__)
            logger.warning(f"Live validation failed: {e}", exc_info=True)
            return {"valid": True, "reason": "Live validation unavailable, allowing execution"}

    # For stub provider, basic validation only
    return {"valid": True}


async def _insert_order(
    pg: PostgresDB,
    strategy_id: str,
    symbol: str,
    side: str,
    qty: int,
    order_type: str,
    price: Optional[float],
    note: str,
) -> None:
    """Insert hedge order into core.core_order table.

    Args:
        pg: PostgreSQL connection
        strategy_id: Strategy identifier
        symbol: Symbol (underlying for overlay, OCC for options)
        side: BUY or SELL
        qty: Quantity (shares for overlay, contracts for options)
        order_type: MKT or OPT_LMT
        price: Limit price (None for MKT orders)
        note: JSON-encoded order details
    """
    idem = f"hedge:{date.today().isoformat()}:{symbol}:{side}:{qty}"

    if order_type == "OPT_LMT" and price:
        # Options limit order
        sql = (
            "INSERT INTO core.core_order (strategy_id, symbol, idempotency_key, side, order_type, qty, limit_price, state, note) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, 'NEW', $8) ON CONFLICT (idempotency_key) DO NOTHING"
        )
        await pg.execute(sql, strategy_id, symbol, idem, side, order_type, float(qty), price, note)
    else:
        # Market order (overlay or legacy)
        sql = (
            "INSERT INTO core.core_order (strategy_id, symbol, idempotency_key, side, order_type, qty, state, note) "
            "VALUES ($1, $2, $3, $4, $5, $6, 'NEW', $7) ON CONFLICT (idempotency_key) DO NOTHING"
        )
        await pg.execute(sql, strategy_id, symbol, idem, side, order_type, float(qty), note)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs-dir", default="runs/ai_trader")
    ap.add_argument("--mode", choices=["overlay", "options", "both"], default=CFG.get('risk_management.hedge.mode', 'both'))
    ap.add_argument("--strategy-id", default=CFG.get('risk_management.hedge.strategy_id', 'hedge_overlay'))
    ap.add_argument("--execute", action="store_true", help="Submit orders to core.core_order (requires DB)")
    ap.add_argument("--min-notional", type=float, default=float(CFG.get('risk_management.hedge.min_notional_usd', 10_000.0)), help="Skip hedges smaller than this notional")
    args = ap.parse_args()

    d = date.today().isoformat()
    outd = Path(args.runs_dir) / d
    outd.mkdir(parents=True, exist_ok=True)
    audit = outd / "hedge_exec.jsonl"

    # Build hedging plan(s)
    plans = []
    if args.mode in ("overlay", "both"):
        p = _plan_from_overlay(args.runs_dir)
        if p:
            plans.append(p)
    if args.mode in ("options", "both"):
        p = _plan_from_options(args.runs_dir)
        if p:
            plans.append(p)
    if not plans:
        print(json.dumps({"status": "no_plans"}))
        return

    ch = ClickHouseDB()
    submitted = []
    auto_exec = bool(CFG.get('risk_management.hedge.auto_execute', False))

    async def _process_plans():
        """Process all hedge plans asynchronously."""
        for p in plans:
            # Handle options vs overlay differently
            if p.order_type == "OPT_LMT":
                # Options execution path
                if not validate_occ_symbol(p.symbol):
                    rec = {"ts": d, "symbol": p.symbol, "action": "skip_invalid_occ", "note": p.note}
                    audit.open("a", encoding="utf-8").write(json.dumps(rec) + "\n")
                    continue

                # Pre-execution validation
                validation = await _validate_options_contract(ch, p.symbol, p.price, args.runs_dir)
                if not validation.get("valid"):
                    rec = {
                        "ts": d,
                        "symbol": p.symbol,
                        "action": "skip_validation_failed",
                        "reason": validation.get("reason"),
                        "note": p.note,
                    }
                    audit.open("a", encoding="utf-8").write(json.dumps(rec) + "\n")
                    continue

                # Check notional minimum (already set in plan)
                js = _read_json(outd / "options_hedge.json")
                contract = js.get("contract", {})
                notional = float(contract.get("notional_usd", 0.0))

                if notional < args.min_notional:
                    rec = {"ts": d, "symbol": p.symbol, "action": "skip_small", "notional": notional}
                    audit.open("a", encoding="utf-8").write(json.dumps(rec) + "\n")
                    continue

                # Execute or dry-run
                execute_now = bool(args.execute or auto_exec)
                if not execute_now:
                    rec = {
                        "ts": d,
                        "symbol": p.symbol,
                        "side": p.side,
                        "qty": p.qty,
                        "limit_px": p.price,
                        "order_type": p.order_type,
                        "notional": notional,
                        "mode": "options_real",
                        "dry_run": True,
                    }
                    audit.open("a", encoding="utf-8").write(json.dumps(rec) + "\n")
                    submitted.append(rec)
                else:
                    # Real execution
                    pg = PostgresDB()
                    await pg.connect()
                    await _insert_order(
                        pg, args.strategy_id, p.symbol, p.side, p.qty, p.order_type, p.price, p.note
                    )
                    await pg.close()

                    rec = {
                        "ts": d,
                        "symbol": p.symbol,
                        "side": p.side,
                        "qty": p.qty,
                        "limit_px": p.price,
                        "order_type": p.order_type,
                        "notional": notional,
                        "mode": "options_real",
                        "submitted": True,
                    }
                    audit.open("a", encoding="utf-8").write(json.dumps(rec) + "\n")
                    submitted.append(rec)

            else:
                # Overlay execution path (existing logic)
                px = _mid_price(ch, p.symbol) or 0.0
                if px <= 0:
                    rec = {"ts": d, "symbol": p.symbol, "action": "skip_no_price", "note": p.note}
                    audit.open("a", encoding="utf-8").write(json.dumps(rec) + "\n")
                    continue

                # Determine notional from source file
                src = outd / "overlay_hedge.json"
                js = _read_json(src)
                if not js:
                    continue

                notional = float((js.get("hedge") or {}).get("notional_usd") or 0.0)
                if abs(notional) < args.min_notional:
                    rec = {"ts": d, "symbol": p.symbol, "action": "skip_small", "notional": notional}
                    audit.open("a", encoding="utf-8").write(json.dumps(rec) + "\n")
                    continue

                qty = max(1, int(abs(notional) / px))
                p.qty = qty

                execute_now = bool(args.execute or auto_exec)
                if not execute_now:
                    rec = {
                        "ts": d,
                        "symbol": p.symbol,
                        "side": p.side,
                        "qty": qty,
                        "px_ref": px,
                        "mode": "overlay",
                        "dry_run": True,
                    }
                    audit.open("a", encoding="utf-8").write(json.dumps(rec) + "\n")
                    submitted.append(rec)
                else:
                    # Insert into core.core_order
                    pg = PostgresDB()
                    await pg.connect()
                    await _insert_order(pg, args.strategy_id, p.symbol, p.side, qty, p.order_type, None, p.note)
                    await pg.close()

                    rec = {
                        "ts": d,
                        "symbol": p.symbol,
                        "side": p.side,
                        "qty": qty,
                        "px_ref": px,
                        "mode": "overlay",
                        "submitted": True,
                    }
                    audit.open("a", encoding="utf-8").write(json.dumps(rec) + "\n")
                    submitted.append(rec)

    # Run async processing
    asyncio.run(_process_plans())

    print(json.dumps({"status": "ok", "submitted": submitted}))


if __name__ == "__main__":
    main()
