from __future__ import annotations

"""
Daily plan slot executor (P0 minimal)

Reads daily_plan.json and, for the given slot, creates/cancels pending orders
using PendingOrdersManager. No broker submission in P0.
"""
from typing import Dict, Any, List
from datetime import datetime, timezone
from pathlib import Path
import hashlib

from .pending_orders_manager import PendingOrdersManager, PendingOrder
from .state_store import load_plan, save_summary, load_summary
from .plan_types import approx_price_from_rule, parse_qty_rule


def _slot_window(slot_id: str) -> str:
    if slot_id.startswith("premarket"):
        return "premarket"
    if slot_id.startswith("eod"):
        return "eod"
    return "intraday"


def _idempotency_key(symbol: str, side: str, limit_price: float, slot_id: str) -> str:
    raw = f"{symbol}:{side}:{limit_price:.4f}:{slot_id}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _exists_duplicate(pom: PendingOrdersManager, symbol: str, side: str, limit_price: float) -> bool:
    for o in pom.get_all_orders(symbol):
        if o.action.upper() == side.upper() and abs(o.limit_price - limit_price) < 1e-6:
            return True
    return False


def execute_slot(slot_id: str, base_dir: str | None = None) -> Dict[str, Any]:
    plan = load_plan(base_dir)
    if not plan:
        return {"status": "no_plan"}
    directives: Dict[str, Any] = plan.get("directives") or {}
    window = _slot_window(slot_id)
    now = datetime.now(timezone.utc).isoformat()

    pom = PendingOrdersManager()
    pom.load_orders()

    new_count = 0
    skipped_dup = 0
    for sym, d in directives.items():
        # Choose directives list based on window
        lst: List[Dict[str, Any]] = []
        if window == "premarket":
            lst = d.get("entry", [])
        elif window == "intraday":
            lst = d.get("intraday", [])
        elif window == "eod":
            lst = d.get("exit", [])
        for ed in lst:
            try:
                side = str(ed.get("side") or "").upper()
                if side not in ("BUY", "SELL"):
                    continue
                # Price
                lp = ed.get("limit_price")
                if lp is None:
                    lp = approx_price_from_rule(ed.get("px_rule"))
                if lp is None:
                    continue
                # Dedupe
                if _exists_duplicate(pom, sym, side, float(lp)):
                    skipped_dup += 1
                    continue
                qty = parse_qty_rule(str(ed.get("qty_rule") or "fixed(100)"), float(lp))
                ttl = int(ed.get("ttl_sec", 600))
                oco = ed.get("oco") or {}
                po = PendingOrder(
                    order_id=_idempotency_key(sym, side, float(lp), slot_id),
                    symbol=sym,
                    action=side,
                    quantity=int(qty),
                    limit_price=float(lp),
                    confidence=1.0,
                    created_at=now,
                    expires_at=(datetime.now(timezone.utc)).isoformat(),
                    take_profit_bps=oco.get("tp_bps"),
                    stop_loss_bps=oco.get("sl_bps"),
                    reasoning=f"slot={slot_id}",
                )
                # set expires_at properly
                from datetime import timedelta
                po.expires_at = (datetime.now(timezone.utc) + timedelta(seconds=ttl)).isoformat()
                if pom.add_order(po):
                    new_count += 1
            except Exception:
                continue

    # Update summary
    s = load_summary(base_dir)
    s.setdefault("slots", {})
    s["slots"][slot_id] = {"ts": now, "new_orders": new_count, "skipped_duplicates": skipped_dup}
    save_summary(s, base_dir)

    return {"status": "ok", "new_orders": new_count, "skipped_duplicates": skipped_dup}

