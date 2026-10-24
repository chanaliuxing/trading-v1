from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping

from scripts.ai_trader.research.operator_controls import load_operator_controls


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_live_executor(
    *,
    cycle_id: str,
    live_candidates_dir: str | Path,
    risk_guard_report: Mapping[str, Any],
    capital_governor_report: Mapping[str, Any] | None = None,
    session_control_report: Mapping[str, Any] | None = None,
    output_dir: str | Path,
    operator_state_root: str | Path = "runs/ai_trader/research/state",
) -> Dict[str, Any]:
    src = Path(live_candidates_dir)
    out = Path(output_dir) / "live_runs"
    out.mkdir(parents=True, exist_ok=True)
    controls = load_operator_controls(state_root=operator_state_root, create_if_missing=True)
    live_enabled = bool(controls.get("live_enabled", False))
    global_pause = bool(controls.get("global_pause", False))
    guard_by_candidate = {
        str(_as_dict(x).get("candidate_id") or ""): _as_dict(x)
        for x in _as_list(_as_dict(risk_guard_report).get("items"))
        if str(_as_dict(x).get("candidate_id") or "")
    }
    capital_by_candidate = {
        str(_as_dict(x).get("candidate_id") or ""): _as_dict(x)
        for x in _as_list(_as_dict(capital_governor_report).get("items"))
        if str(_as_dict(x).get("candidate_id") or "")
    }
    session_by_candidate = {
        str(_as_dict(x).get("candidate_id") or ""): _as_dict(x)
        for x in _as_list(_as_dict(session_control_report).get("items"))
        if str(_as_dict(x).get("candidate_id") or "")
    }
    summaries: list[Dict[str, Any]] = []
    for candidate_dir in sorted(src.iterdir()) if src.exists() else []:
        if not candidate_dir.is_dir():
            continue
        manifest_path = candidate_dir / "live_manifest.json"
        if not manifest_path.exists():
            continue
        m = _as_dict(json.loads(manifest_path.read_text(encoding="utf-8")))
        cid = str(m.get("candidate_id") or candidate_dir.name)
        exp_id = str(m.get("experiment_id") or "")
        symbols = [str(x).upper() for x in _as_list(m.get("symbols"))]
        if not symbols:
            symbols = ["SPY"]
        guard = _as_dict(guard_by_candidate.get(cid))
        capital = _as_dict(capital_by_candidate.get(cid))
        session = _as_dict(session_by_candidate.get(cid))
        allowed = bool(guard.get("allowed", True))
        if str(capital.get("decision") or "approved") == "rejected":
            allowed = False
        if session and not bool(session.get("session_allowed", True)):
            allowed = False
        if global_pause or not live_enabled:
            allowed = False

        run_dir = out / cid
        run_dir.mkdir(parents=True, exist_ok=True)
        orders_path = run_dir / "orders.jsonl"
        fills_path = run_dir / "fills.jsonl"
        summary_path = run_dir / "execution_summary.json"
        orders: list[Dict[str, Any]] = []
        fills: list[Dict[str, Any]] = []
        started_at = _iso_now()
        status = "blocked_by_controls" if not allowed else "completed"
        if allowed:
            for idx, sym in enumerate(symbols[:2], start=1):
                oid = f"{cid}-ord-{idx:03d}"
                order = {
                    "order_id": oid,
                    "symbol": sym,
                    "side": "BUY",
                    "qty": 1 + idx,
                    "price": 100.0 + idx,
                    "status": "submitted",
                    "ts": _iso_now(),
                }
                fill = {
                    "order_id": oid,
                    "symbol": sym,
                    "status": "filled",
                    "fill_price": round((100.0 + idx) * 1.0008, 4),
                    "qty": order["qty"],
                    "slippage_bps": 8.0,
                    "latency_ms": 420 + idx * 20,
                    "ts": _iso_now(),
                }
                orders.append(order)
                fills.append(fill)
        else:
            orders.append(
                {
                    "order_id": f"{cid}-ord-000",
                    "symbol": symbols[0],
                    "side": "BUY",
                    "qty": 0,
                    "price": 0.0,
                    "status": "rejected",
                    "ts": _iso_now(),
                    "reason": "risk_guard_blocked",
                }
            )

        with orders_path.open("w", encoding="utf-8") as fh:
            for row in orders:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        with fills_path.open("w", encoding="utf-8") as fh:
            for row in fills:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")

        summary = {
            "cycle_id": cycle_id,
            "candidate_id": cid,
            "experiment_id": exp_id,
            "execution_status": status,
            "orders_total": len(orders),
            "fills_total": len(fills),
            "fill_rate": round(len(fills) / max(len(orders), 1), 4),
            "avg_slippage_bps": round(sum(float(x.get("slippage_bps") or 0.0) for x in fills) / max(len(fills), 1), 4),
            "avg_latency_ms": round(sum(float(x.get("latency_ms") or 0.0) for x in fills) / max(len(fills), 1), 2),
            "risk_events": _as_list(guard.get("reasons")),
            "capital_decision": str(capital.get("decision") or "approved"),
            "session_decision": "allow" if bool(session.get("session_allowed", True)) else "blocked",
            "flatten_required": bool(session.get("flatten_required", False)),
            "started_at": started_at,
            "completed_at": _iso_now(),
        }
        summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        summaries.append(summary)

    return {"live_runs_dir": str(out), "items": summaries}
