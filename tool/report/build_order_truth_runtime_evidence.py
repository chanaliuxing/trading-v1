#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from tool.ops.qqq_live_strategy_aliases import (
    LEGACY_QQQ_LIVE_STRATEGY_ID,
    QQQ_LIVE_STRATEGY_ALIASES,
    matched_qqq_live_strategy_ids,
)

DEFAULT_STRATEGY_ID = LEGACY_QQQ_LIVE_STRATEGY_ID
LEGACY_OPEN_ORDER_BLOCK_REASON = "legacy_open_orders_block"
LEGACY_OPEN_ORDER_BLOCK_STAGE = "entry_blocked_legacy_open_orders"


def _normalize_strategy_ids(strategy_id: str | list[str] | tuple[str, ...] | None) -> list[str]:
    if strategy_id is None:
        return list(QQQ_LIVE_STRATEGY_ALIASES)
    if isinstance(strategy_id, str):
        value = str(strategy_id).strip()
        return [value] if value else list(QQQ_LIVE_STRATEGY_ALIASES)
    normalized = [str(item).strip() for item in strategy_id if str(item).strip()]
    return normalized or list(QQQ_LIVE_STRATEGY_ALIASES)


def _iter_strategy_rows(audit_dir: Path, strategy_ids: list[str]) -> tuple[int, list[dict[str, Any]], list[str]]:
    rows_scanned = 0
    matched: list[dict[str, Any]] = []
    matched_strategy_ids: set[str] = set()
    expected_strategy_ids = set(strategy_ids)
    for path in sorted(audit_dir.glob("strategy-*.jsonl")):
        try:
            with path.open("r", encoding="utf-8") as handle:
                for raw_line in handle:
                    raw_line = raw_line.strip()
                    if not raw_line:
                        continue
                    rows_scanned += 1
                    try:
                        row = json.loads(raw_line)
                    except Exception:
                        continue
                    if not isinstance(row, dict):
                        continue
                    row_strategy_id = str(row.get("strategy_id") or "").strip()
                    if row_strategy_id not in expected_strategy_ids:
                        continue
                    matched.append(row)
                    matched_strategy_ids.add(row_strategy_id)
        except Exception:
            continue
    return rows_scanned, matched, matched_qqq_live_strategy_ids(matched_strategy_ids)


def _parse_iso(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except Exception:
        return None


def build_order_truth_runtime_evidence(
    *,
    audit_root: Path,
    date: str,
    strategy_id: str | list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    audit_dir = audit_root / date
    strategy_ids_checked = _normalize_strategy_ids(strategy_id)
    rows_scanned, strategy_rows, matched_strategy_ids = _iter_strategy_rows(audit_dir, strategy_ids_checked)

    legacy_open_orders_block_count = 0
    blocked_reason_counts: Counter[str] = Counter()
    legitimate_block_reason_counts: Counter[str] = Counter()
    position_sync_flat_count = 0
    latest_legacy_block_ts: datetime | None = None

    for row in strategy_rows:
        stage = str(row.get("stage") or "").strip()
        reason = str(row.get("reason") or "").strip()
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}

        if stage == LEGACY_OPEN_ORDER_BLOCK_STAGE or reason == LEGACY_OPEN_ORDER_BLOCK_REASON:
            legacy_open_orders_block_count += 1
            row_ts = _parse_iso(row.get("ts") or row.get("ts_now_et"))
            if row_ts is not None and (latest_legacy_block_ts is None or row_ts > latest_legacy_block_ts):
                latest_legacy_block_ts = row_ts

        if stage == "gate_blocked" and reason:
            blocked_reason_counts[reason] += 1
            if reason != LEGACY_OPEN_ORDER_BLOCK_REASON:
                legitimate_block_reason_counts[reason] += 1

        if stage == "position_sync_qty_resolution":
            qty_resolved = payload.get("qty_resolved") if isinstance(payload, dict) else None
            if isinstance(qty_resolved, dict):
                try:
                    if all(abs(float(v or 0.0)) < 1e-9 for v in qty_resolved.values()):
                        position_sync_flat_count += 1
                except Exception:
                    pass

    post_legacy_strategy_rows = 0
    post_legacy_flat_count = 0
    if latest_legacy_block_ts is not None:
        for row in strategy_rows:
            row_ts = _parse_iso(row.get("ts") or row.get("ts_now_et"))
            if row_ts is None or row_ts <= latest_legacy_block_ts:
                continue
            post_legacy_strategy_rows += 1
            if str(row.get("stage") or "").strip() != "position_sync_qty_resolution":
                continue
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            qty_resolved = payload.get("qty_resolved") if isinstance(payload, dict) else None
            if isinstance(qty_resolved, dict):
                try:
                    if all(abs(float(v or 0.0)) < 1e-9 for v in qty_resolved.values()):
                        post_legacy_flat_count += 1
                except Exception:
                    pass

    legacy_block_recovered = (
        latest_legacy_block_ts is not None
        and post_legacy_strategy_rows > 0
        and post_legacy_flat_count > 0
    )
    false_block_removed = bool(strategy_rows) and (
        legacy_open_orders_block_count == 0 or legacy_block_recovered
    )
    legitimate_block_observed = bool(legitimate_block_reason_counts)
    ok = false_block_removed and bool(strategy_rows)

    if not strategy_rows:
        recommendation = "HOLD"
        reasons = ["strategy_runtime_evidence_missing"]
    elif legacy_open_orders_block_count > 0 and not legacy_block_recovered:
        recommendation = "HOLD"
        reasons = ["legacy_open_orders_block_still_present"]
    elif legitimate_block_observed:
        recommendation = "PASS"
        reasons = []
    else:
        recommendation = "CONTINUE"
        reasons = ["legitimate_runtime_block_not_observed_yet"]

    return {
        "date": date,
        "strategy_id": DEFAULT_STRATEGY_ID,
        "strategy_ids_checked": strategy_ids_checked,
        "matched_strategy_ids": matched_strategy_ids,
        "audit_dir": str(audit_dir),
        "audit_dir_exists": audit_dir.exists(),
        "rows_scanned": rows_scanned,
        "strategy_row_count": len(strategy_rows),
        "false_block_removed": false_block_removed,
        "legacy_open_orders_block_count": legacy_open_orders_block_count,
        "latest_legacy_block_ts": latest_legacy_block_ts.isoformat() if latest_legacy_block_ts else None,
        "legacy_block_recovered": legacy_block_recovered,
        "post_legacy_strategy_row_count": post_legacy_strategy_rows,
        "post_legacy_flat_count": post_legacy_flat_count,
        "blocked_reason_counts": dict(sorted(blocked_reason_counts.items())),
        "legitimate_block_reason_counts": dict(sorted(legitimate_block_reason_counts.items())),
        "legitimate_block_observed": legitimate_block_observed,
        "position_sync_flat_count": position_sync_flat_count,
        "ok": ok,
        "recommendation": recommendation,
        "reasons": reasons,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build runtime evidence for order-truth gating behavior")
    parser.add_argument("--root", default="runtime/audit")
    parser.add_argument("--date", required=True)
    parser.add_argument("--strategy-id", action="append", default=[])
    parser.add_argument("--output", default="")
    args = parser.parse_args(argv)

    payload = build_order_truth_runtime_evidence(
        audit_root=Path(args.root).expanduser(),
        date=str(args.date),
        strategy_id=(args.strategy_id or None),
    )
    if args.output:
        output_path = Path(args.output).expanduser()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload.get("recommendation") in {"PASS", "CONTINUE"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
