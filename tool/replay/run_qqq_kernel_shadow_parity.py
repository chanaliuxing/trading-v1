#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_ROOT = ROOT / "reports" / "qqq_kernel_shadow_parity"

PARITY_FIELDS = (
    "signal_authority",
    "intent",
    "order_plan",
    "block_reason",
    "exec_symbol",
    "decision_ts_delta_sec",
)

TIMING_DIFF_TOLERANCE_SEC = 30.0


def _minute_key_value(key: str) -> int | None:
    text = str(key or "").strip()
    if not text.startswith("minute:"):
        return None
    digits = "".join(ch for ch in text[len("minute:") :] if ch.isdigit())
    if len(digits) < 12:
        return None
    try:
        return int(digits[:12])
    except Exception:
        return None


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except Exception:
        return None


def _load_json(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".jsonl":
        rows: list[dict[str, Any]] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            payload = json.loads(stripped)
            if isinstance(payload, dict):
                rows.append(payload)
        return rows
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        rows: list[dict[str, Any]] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            payload = json.loads(stripped)
            if isinstance(payload, dict):
                rows.append(payload)
        return rows


def _records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [_as_dict(item) for item in payload]
    data = _as_dict(payload)
    for key in ("records", "rows", "samples", "events", "decisions"):
        value = data.get(key)
        if isinstance(value, list):
            return [_as_dict(item) for item in value]
    return []


def _ts_epoch_s(record: dict[str, Any]) -> float | None:
    for key in ("decision_ts_epoch_s", "ts_epoch_s", "timestamp_epoch_s"):
        try:
            value = float(record.get(key))
        except Exception:
            continue
        if value > 0:
            return value
    return None


def _decision_key(record: dict[str, Any], idx: int) -> str:
    minute = str(record.get("minute") or record.get("bar_id") or "").strip()
    minute_digits = "".join(ch for ch in minute if ch.isdigit())
    if len(minute_digits) >= 12:
        minute = minute_digits[:12]
    elif len(minute_digits) >= 8:
        minute = minute_digits
    ts_value = _ts_epoch_s(record)
    if (not minute) and ts_value is not None:
        minute = datetime.utcfromtimestamp(float(ts_value)).strftime("%Y%m%d%H%M")
    if minute:
        return f"minute:{minute}"
    decision_id = str(record.get("decision_id") or record.get("id") or "").strip()
    if decision_id:
        return f"decision:{decision_id}"
    if ts_value is not None:
        return f"ts:{int(ts_value // 60)}:idx:{idx}"
    return f"idx:{idx}"


def _side(record: dict[str, Any]) -> Any:
    if "side" in record:
        return record.get("side")
    if "direction" in record:
        direction = str(record.get("direction") or "").strip().upper()
        if direction in {"LONG", "SHORT", "FLAT"}:
            return direction
    target_qty = record.get("target_qty")
    try:
        qty = float(target_qty)
    except Exception:
        return None
    if qty > 0:
        return "LONG"
    if qty < 0:
        return "SHORT"
    return "FLAT"


def _canonical_side(value: Any) -> str | None:
    text = str(value or "").strip().upper()
    if text in {"", "NA", "NONE", "NULL", "FLAT"}:
        return "FLAT"
    if text in {"BUY", "LONG"}:
        return "LONG"
    if text in {"SELL", "SHORT"}:
        return "SHORT"
    return text or None


def _canonical_symbol(value: Any) -> str | None:
    text = str(value or "").strip().upper()
    return text or None


def _is_flat_action(side: Any, qty: Any = None, exec_symbol: Any = None) -> bool:
    canonical_side = _canonical_side(side)
    canonical_qty = _as_float(qty)
    canonical_symbol = _canonical_symbol(exec_symbol)
    if canonical_side == "FLAT":
        return True
    if canonical_qty is not None and abs(canonical_qty) <= 0:
        return True
    return canonical_symbol is None and canonical_side in {None, "FLAT"}


def _canonical_signal_authority(value: dict[str, Any]) -> dict[str, Any]:
    payload = dict(value)
    payload["side"] = _canonical_side(payload.get("side"))
    if payload.get("allow") is False and payload.get("side") is None:
        payload["side"] = "FLAT"
    return payload


def _canonical_intent(value: dict[str, Any]) -> dict[str, Any]:
    payload = dict(value)
    payload["symbol"] = _canonical_symbol(payload.get("symbol")) or ""
    payload["side"] = _canonical_side(payload.get("side"))
    qty = _as_float(payload.get("target_qty"))
    if _is_flat_action(payload.get("side"), qty, payload.get("symbol")):
        payload["side"] = "FLAT"
        payload["target_qty"] = 0.0
    else:
        payload["target_qty"] = qty if qty is not None else payload.get("target_qty")
    return payload


def _canonical_order_plan(value: dict[str, Any]) -> dict[str, Any]:
    payload = dict(value)
    payload["exec_symbol"] = _canonical_symbol(payload.get("exec_symbol"))
    payload["side"] = _canonical_side(payload.get("side"))
    qty = _as_float(payload.get("qty"))
    order_type = str(payload.get("order_type") or "").strip().upper() or None
    if _is_flat_action(payload.get("side"), qty, payload.get("exec_symbol")):
        payload["side"] = "FLAT"
        payload["qty"] = 0.0
        payload["order_type"] = "DRY_RUN_SHADOW"
    else:
        payload["qty"] = qty if qty is not None else payload.get("qty")
        payload["order_type"] = order_type
    return payload


def _canonical_record_fields(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "signal_authority": _canonical_signal_authority(record.get("signal_authority") or {}),
        "intent": _canonical_intent(record.get("intent") or {}),
        "order_plan": _canonical_order_plan(record.get("order_plan") or {}),
        "block_reason": record.get("block_reason") or None,
        "exec_symbol": _canonical_symbol(record.get("exec_symbol")),
    }


def _is_non_tradable_flat_record(record: dict[str, Any] | None) -> bool:
    if record is None:
        return False
    fields = _canonical_record_fields(record)
    return (
        fields["signal_authority"].get("allow") is False
        and _is_flat_action(
            fields["intent"].get("side"),
            fields["intent"].get("target_qty"),
            fields["order_plan"].get("exec_symbol"),
        )
        and _is_flat_action(
            fields["order_plan"].get("side"),
            fields["order_plan"].get("qty"),
            fields["order_plan"].get("exec_symbol"),
        )
    )


def _signal_authority(record: dict[str, Any]) -> dict[str, Any]:
    nested = record.get("signal_authority")
    if isinstance(nested, dict):
        return {
            "allow": nested.get("allow"),
            "side": nested.get("side"),
            "reason": nested.get("reason"),
        }
    return {
        "allow": record.get("allow", bool(record.get("direction") or record.get("exec_symbol"))),
        "side": _side(record),
        "reason": record.get("reason") or record.get("block_reason") or _as_dict(record.get("signal_state")).get("mode"),
    }


def _intent(record: dict[str, Any]) -> dict[str, Any]:
    nested = record.get("intent")
    if isinstance(nested, dict):
        return {
            "symbol": nested.get("symbol"),
            "side": nested.get("side") or _side(nested),
            "target_qty": nested.get("target_qty"),
        }
    return {
        "symbol": record.get("symbol") or record.get("exec_symbol"),
        "side": _side(record),
        "target_qty": record.get("target_qty"),
    }


def _order_plan(record: dict[str, Any]) -> dict[str, Any]:
    nested = record.get("order_plan")
    if isinstance(nested, dict):
        return {
            "exec_symbol": nested.get("exec_symbol") or nested.get("symbol"),
            "side": nested.get("side"),
            "qty": nested.get("qty"),
            "order_type": nested.get("order_type"),
        }
    return {
        "exec_symbol": record.get("exec_symbol"),
        "side": record.get("order_side") or record.get("side") or record.get("direction"),
        "qty": record.get("qty"),
        "order_type": record.get("order_type"),
    }


def normalize_record(record: dict[str, Any], *, idx: int) -> dict[str, Any]:
    ts_value = _ts_epoch_s(record)
    out = {
        "key": _decision_key(record, idx),
        "decision_id": str(record.get("decision_id") or "").strip() or None,
        "decision_ts_epoch_s": ts_value,
        "signal_authority": _signal_authority(record),
        "intent": _intent(record),
        "order_plan": _order_plan(record),
        "block_reason": record.get("block_reason") or record.get("reason"),
        "exec_symbol": record.get("exec_symbol") or _order_plan(record).get("exec_symbol"),
    }
    out.update(_canonical_record_fields(out))
    return out


def _classify_field_diff(
    *,
    left: dict[str, Any] | None,
    right: dict[str, Any] | None,
    field: str,
    actual: Any,
    expected: Any,
) -> str:
    if field == "__presence__":
        return "true_decision_diff"
    if field == "decision_ts_delta_sec":
        return "timing_only"
    left_blocked = _is_non_tradable_flat_record(left)
    right_blocked = _is_non_tradable_flat_record(right)
    if field in {"signal_authority", "block_reason"} and left_blocked and right_blocked:
        return "semantic_equivalent"
    if field in {"intent", "order_plan", "exec_symbol"} and left_blocked and right_blocked:
        return "semantic_equivalent"
    return "true_decision_diff"


def _field_diff(left: dict[str, Any] | None, right: dict[str, Any] | None, field: str) -> dict[str, Any] | None:
    left_value = left.get(field) if left is not None else None
    right_value = right.get(field) if right is not None else None
    if field == "decision_ts_delta_sec":
        left_ts = left.get("decision_ts_epoch_s") if left is not None else None
        right_ts = right.get("decision_ts_epoch_s") if right is not None else None
        if left_ts is None or right_ts is None:
            return None
        delta = round(abs(float(left_ts) - float(right_ts)), 6)
        if delta <= TIMING_DIFF_TOLERANCE_SEC:
            return None
        return {
            "field": field,
            "expected": 0.0,
            "actual": delta,
            "classification": _classify_field_diff(
                left=left,
                right=right,
                field=field,
                actual=delta,
                expected=0.0,
            ),
        }
    if left_value == right_value:
        return None
    return {
        "field": field,
        "expected": left_value,
        "actual": right_value,
        "classification": _classify_field_diff(
            left=left,
            right=right,
            field=field,
            actual=right_value,
            expected=left_value,
        ),
    }


def _row_classification(diffs: list[dict[str, Any]]) -> str:
    classes = {str(diff.get("classification") or "true_decision_diff") for diff in diffs}
    if "true_decision_diff" in classes:
        return "true_decision_diff"
    if classes == {"timing_only"}:
        return "timing_only"
    return "semantic_equivalent"


def build_qqq_kernel_shadow_parity_report(
    *,
    legacy_records: list[dict[str, Any]],
    kernel_records: list[dict[str, Any]],
    legacy_strategy_id: str,
    kernel_strategy_id: str,
    legacy_mode: str,
    day: str,
) -> dict[str, Any]:
    left_by_key = {
        item["key"]: item
        for idx, record in enumerate(legacy_records)
        for item in (normalize_record(record, idx=idx),)
    }
    right_by_key = {
        item["key"]: item
        for idx, record in enumerate(kernel_records)
        for item in (normalize_record(record, idx=idx),)
    }
    left_minute_values = sorted(
        value for value in (_minute_key_value(key) for key in left_by_key) if value is not None
    )
    right_minute_values = sorted(
        value for value in (_minute_key_value(key) for key in right_by_key) if value is not None
    )
    compare_minute_floor: int | None = None
    compare_minute_ceiling: int | None = None
    if left_minute_values and right_minute_values:
        compare_minute_floor = max(left_minute_values[0], right_minute_values[0])
        compare_minute_ceiling = min(left_minute_values[-1], right_minute_values[-1])
    rows: list[dict[str, Any]] = []
    diff_count_by_field = {field: 0 for field in PARITY_FIELDS}
    diff_count_by_classification = {
        "semantic_equivalent": 0,
        "timing_only": 0,
        "true_decision_diff": 0,
    }
    ignored_out_of_window_key_count = 0
    for key in sorted(set(left_by_key) | set(right_by_key)):
        minute_value = _minute_key_value(key)
        if (
            minute_value is not None
            and compare_minute_floor is not None
            and compare_minute_ceiling is not None
            and (minute_value < compare_minute_floor or minute_value > compare_minute_ceiling)
        ):
            ignored_out_of_window_key_count += 1
            continue
        left = left_by_key.get(key)
        right = right_by_key.get(key)
        diffs = []
        if left is None or right is None:
            diffs.append(
                {
                    "field": "__presence__",
                    "expected": left is not None,
                    "actual": right is not None,
                    "classification": "true_decision_diff",
                }
            )
        else:
            for field in PARITY_FIELDS:
                diff = _field_diff(left, right, field)
                if diff is not None:
                    diffs.append(diff)
                    diff_count_by_field[field] += 1
        classification = _row_classification(diffs) if diffs else "match"
        if classification in diff_count_by_classification:
            diff_count_by_classification[classification] += 1
        blocking = classification == "true_decision_diff"
        rows.append(
            {
                "key": key,
                "matched": not diffs,
                "blocking": blocking,
                "diff_classification": classification,
                "diffs": diffs,
                "legacy": left,
                "kernel": right,
            }
        )
    total = len(rows)
    diff_total = sum(1 for row in rows if bool(row.get("blocking")))
    return {
        "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "day": str(day),
        "legacy_strategy_id": str(legacy_strategy_id),
        "kernel_strategy_id": str(kernel_strategy_id),
        "legacy_mode": str(legacy_mode),
        "legacy_count": len(legacy_records),
        "kernel_count": len(kernel_records),
        "comparison_count": total,
        "diff_total": diff_total,
        "match_ratio": ((total - diff_total) / total) if total else 1.0,
        "ignored_out_of_window_key_count": ignored_out_of_window_key_count,
        "comparison_window": {
            "minute_floor": str(compare_minute_floor) if compare_minute_floor is not None else None,
            "minute_ceiling": str(compare_minute_ceiling) if compare_minute_ceiling is not None else None,
        },
        "diff_count_by_field": diff_count_by_field,
        "diff_count_by_classification": diff_count_by_classification,
        "metrics": {
            "qqq_kernel_shadow_parity_diff_total": diff_total,
        },
        "recommendation": "PASS" if diff_total == 0 else "HOLD",
        "rows": rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build QQQ kernel shadow parity report")
    parser.add_argument("--legacy", required=True, help="Legacy monolith baseline JSON")
    parser.add_argument("--kernel", required=True, help="Kernel shadow JSON")
    parser.add_argument("--legacy-strategy-id", default="qqq_intraday_001")
    parser.add_argument("--kernel-strategy-id", default="qqq_kernel_shadow_legacy_on_001")
    parser.add_argument("--legacy-mode", default="legacy_on")
    parser.add_argument("--day", default=datetime.now().strftime("%Y%m%d"))
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument("--output", default="")
    parser.add_argument("--fail-on-diff", action="store_true")
    args = parser.parse_args(argv)

    legacy_records = _records(_load_json(Path(args.legacy)))
    kernel_records = _records(_load_json(Path(args.kernel)))
    report = build_qqq_kernel_shadow_parity_report(
        legacy_records=legacy_records,
        kernel_records=kernel_records,
        legacy_strategy_id=args.legacy_strategy_id,
        kernel_strategy_id=args.kernel_strategy_id,
        legacy_mode=args.legacy_mode,
        day=args.day,
    )
    output = Path(args.output) if str(args.output or "").strip() else Path(args.output_root) / f"{args.day}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"output": str(output), "recommendation": report["recommendation"], "diff_total": report["diff_total"]}, ensure_ascii=False, sort_keys=True))
    if args.fail_on_diff and int(report["diff_total"] or 0) > 0:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
