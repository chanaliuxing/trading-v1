from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Mapping


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


def sync_positions_snapshot(
    *,
    cycle_id: str,
    broker_snapshot: Mapping[str, Any],
    live_candidates_dir: str | Path,
    output_dir: str | Path,
) -> Dict[str, Any]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    live_dir = Path(live_candidates_dir)
    positions = [_as_dict(x) for x in _as_list(_as_dict(broker_snapshot).get("positions"))]
    exposure = sum(abs(float(x.get("qty") or 0.0) * float(x.get("avg_price") or 0.0)) for x in positions)
    payload = {"cycle_id": cycle_id, "positions": positions, "exposure": round(exposure, 4)}
    pos_path = out / "positions_snapshot.json"
    pos_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    expected_symbols = set()
    for cand in sorted(live_dir.iterdir()) if live_dir.exists() else []:
        if not cand.is_dir():
            continue
        manifest = cand / "live_manifest.json"
        if not manifest.exists():
            continue
        obj = _as_dict(json.loads(manifest.read_text(encoding="utf-8")))
        for sym in _as_list(obj.get("symbols")):
            text = str(sym).upper().strip()
            if text:
                expected_symbols.add(text)
    mismatches = []
    for row in positions:
        sym = str(row.get("symbol") or "").upper()
        if sym and sym not in expected_symbols:
            mismatches.append({"type": "stale_position", "symbol": sym, "reason": "not_in_candidate_universe"})
    reconcile = {
        "cycle_id": cycle_id,
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
    }
    reconcile_path = out / "position_reconcile_report.json"
    reconcile_path.write_text(json.dumps(reconcile, indent=2, ensure_ascii=False), encoding="utf-8")
    return {
        "positions_snapshot_json": str(pos_path),
        "position_reconcile_report_json": str(reconcile_path),
        "positions_snapshot": payload,
        "position_reconcile_report": reconcile,
    }

