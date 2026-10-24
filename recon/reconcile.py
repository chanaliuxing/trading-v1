# recon/reconcile.py
from __future__ import annotations
from typing import List, Dict, Any
import time


def realtime_compare_and_log(run_id: str, ch_client, shadow_fills: List[Dict[str,Any]], real_fills: List[Dict[str,Any]], slo=None):
    """
    Compare shadow and real fills in real-time.

    Simplified matching: match by (symbol, side, qty) within window.
    Logs differences to ClickHouse exec_compare table.

    Args:
        run_id: Run identifier
        ch_client: ClickHouse client (or None)
        shadow_fills: List of shadow execution fills
        real_fills: List of real broker fills
        slo: SLO monitor for tracking differences
    """
    if not ch_client:
        return

    # Index real fills by (symbol, side, qty)
    real_ix: Dict[tuple, List[Dict[str,Any]]] = {}
    for f in real_fills:
        key = (f.get("symbol"), f.get("side"), int(f.get("qty",0)))
        real_ix.setdefault(key, []).append(f)

    inserts = []
    for s in shadow_fills:
        key = (s.get("symbol"), s.get("side"), int(s.get("qty",0)))
        candidates = real_ix.get(key, [])
        if not candidates:
            continue

        r = candidates.pop(0)
        try:
            shadow_px = float(s.get("price",0.0))
            real_px = float(r.get("price",0.0))
            diff_bps = abs((real_px - shadow_px)/shadow_px)*1e4 if shadow_px>0 else 0.0

            inserts.append([
                run_id, int(time.time()*1000),
                str(s.get("symbol")), str(s.get("side")), int(s.get("qty",0)),
                shadow_px, real_px, diff_bps
            ])

            if slo:
                slo.record_shadow_real_diff_bps(shadow_px, real_px)
        except Exception:
            pass

    if inserts:
        try:
            ch_client.insert("exec_compare", inserts, column_names=[
                "run_id","ts_ms","symbol","side","qty","shadow_px","real_px","diff_bps"
            ])
        except Exception:
            pass  # Don't fail on reconciliation errors


def eod_reconcile_positions(ch_client, run_id: str, internal_positions: Dict[str,Dict[str,float]], broker_positions: Dict[str,Dict[str,float]]):
    """
    End-of-day position reconciliation.

    Compares internal positions vs broker positions and returns discrepancies.

    Args:
        ch_client: ClickHouse client (optional)
        run_id: Run identifier
        internal_positions: Internal position tracking
        broker_positions: Positions from broker API

    Returns:
        Dict of position discrepancies by symbol
    """
    diff = {}
    for sym, pos in internal_positions.items():
        q_int = int(pos.get("qty",0))
        q_real = int((broker_positions.get(sym) or {}).get("qty",0))
        if q_int != q_real:
            diff[sym] = {"internal": q_int, "broker": q_real}

    # Could log to ClickHouse here for audit trail
    return diff
