from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")


def build_option_trigger_ledger(
    *,
    repo_root: str | Path,
    generated_at: datetime,
    required_strategy_ids: list[str],
    max_age_sec: float = 300.0,
) -> dict[str, Any]:
    root = Path(repo_root)
    required = {str(item or "").strip() for item in required_strategy_ids if str(item or "").strip()}
    handoff_path = root / "runtime/state/qqq_stock_option_handoff.jsonl"
    selection_dir = root / "runtime/readiness/contract_selection"
    lifecycle_path = root / "runtime/state/option_trigger_lifecycle.jsonl"
    handoffs = _read_jsonl(handoff_path)
    lifecycle_events = _read_jsonl(lifecycle_path)
    selections = _read_jsonl(selection_dir / f"contract_selection_{generated_at.astimezone(ET).strftime('%Y%m%d')}.jsonl")
    if not selections:
        latest = _read_json(selection_dir / "contract_selection_latest.json")
        selections = [latest] if latest else []

    recent_handoffs: list[dict[str, Any]] = []
    for row in handoffs:
        consumer = str(row.get("consumer_strategy_id") or "").strip()
        if required and consumer not in required:
            continue
        ts = _parse_dt(row.get("ts"))
        age_sec = _age_sec(ts, generated_at)
        terminal = bool(row.get("terminal")) or str(row.get("status") or "").strip().lower() in {
            "filled",
            "rejected",
            "expired",
            "cancelled",
            "risk_blocked",
        }
        if age_sec is None:
            continue
        if age_sec > float(max_age_sec) and terminal:
            continue
        item = dict(row)
        item["age_sec"] = age_sec
        item["stale_unresolved"] = bool(age_sec > float(max_age_sec) and not terminal)
        recent_handoffs.append(item)

    triggers: list[dict[str, Any]] = []
    active_count = 0
    rejected_count = 0
    selected_count = 0
    expired_count = 0
    for handoff in recent_handoffs:
        consumer = str(handoff.get("consumer_strategy_id") or "").strip()
        underlying = str(handoff.get("underlying") or "").strip().upper()
        handoff_ts = _parse_dt(handoff.get("ts"))
        verdict = _latest_selection_for_strategy(selections, strategy_id=consumer, after_ts=handoff_ts)
        lifecycle = _latest_lifecycle_event(lifecycle_events, trigger=handoff, after_ts=handoff_ts)
        status = "received"
        reason = str(handoff.get("reason") or "")
        terminal = bool(handoff.get("terminal"))
        if bool(handoff.get("stale_unresolved")):
            reason = "trigger_expired_without_terminal_lifecycle"
            appended = _append_expired_lifecycle_event_once(
                lifecycle_path=lifecycle_path,
                trigger=handoff,
                generated_at=generated_at,
                reason=reason,
                existing_events=lifecycle_events,
            )
            if appended:
                status = "expired"
                terminal = True
                expired_count += 1
                lifecycle = appended
            else:
                status = "expiry_lifecycle_write_failed"
                reason = "trigger_expiry_lifecycle_write_failed"
                terminal = False
        if verdict and not bool(handoff.get("stale_unresolved")):
            verdict_status = str(verdict.get("status") or "").strip().lower()
            verdict_reason = str(verdict.get("reason") or "")
            if verdict_status == "selected":
                status = "selected"
                reason = verdict_reason or "contract_selected"
                terminal = False
                selected_count += 1
            elif verdict_status in {"rejected", "expired"}:
                status = "rejected" if verdict_status == "rejected" else "expired"
                reason = verdict_reason or status
                terminal = True
                if status == "rejected":
                    rejected_count += 1
                else:
                    expired_count += 1
            else:
                status = verdict_status or "received"
                reason = verdict_reason or reason
        if lifecycle:
            lifecycle_status = str(lifecycle.get("status") or "").strip().lower()
            if lifecycle_status:
                status = lifecycle_status
                reason = str(lifecycle.get("reason") or "") or reason or lifecycle_status
                terminal = lifecycle_status in {"filled", "rejected", "expired", "cancelled", "risk_blocked"}
            if status == "filled":
                selected_count += 0
        if not terminal:
            active_count += 1
        triggers.append(
            {
                "trigger_key": str(handoff.get("trigger_key") or handoff.get("trigger_id") or ""),
                "trigger_id": str(handoff.get("trigger_id") or ""),
                "source_strategy_id": str(handoff.get("source_strategy_id") or ""),
                "consumer_strategy_id": consumer,
                "underlying": underlying,
                "signal_family": str(handoff.get("signal_family") or ""),
                "status": status,
                "reason": reason,
                "terminal": terminal,
                "age_sec": handoff.get("age_sec"),
                "stale_unresolved": bool(handoff.get("stale_unresolved")),
                "handoff_ts": str(handoff.get("ts") or ""),
                "selection_generated_at": str((verdict or {}).get("generated_at") or ""),
                "lifecycle_status": str((lifecycle or {}).get("status") or ""),
                "lifecycle_generated_at": str((lifecycle or {}).get("generated_at") or (lifecycle or {}).get("ts") or ""),
                "order_id": str((lifecycle or {}).get("order_id") or ""),
                "broker_order_id": str((lifecycle or {}).get("broker_order_id") or ""),
                "exec_id": str((lifecycle or {}).get("exec_id") or ""),
            }
        )

    clean = active_count <= 0 and rejected_count <= 0
    return {
        "schema_version": 1,
        "artifact_type": "option_trigger_ledger",
        "generated_at": generated_at.isoformat(),
        "source_process_pid": os.getpid(),
        "enabled": True,
        "go_no_go": "go" if clean else "no_go",
        "required_strategy_ids": sorted(required),
        "recent_trigger_count": len(triggers),
        "active_trigger_count": active_count,
        "selected_trigger_count": selected_count,
        "rejected_trigger_count": rejected_count,
        "expired_trigger_count": expired_count,
        "triggers": triggers[-100:],
        "blockers": _blockers(active_count=active_count, rejected_count=rejected_count),
        "clear_conditions": [] if clean else ["select_reject_expire_or_adjudicate_option_triggers"],
        "source_paths": {
            "handoff": str(handoff_path),
            "contract_selection_dir": str(selection_dir),
            "lifecycle": str(lifecycle_path),
        },
    }


def disabled_option_trigger_ledger_status(*, generated_at: datetime) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "artifact_type": "option_trigger_ledger",
        "generated_at": generated_at.isoformat(),
        "source_process_pid": os.getpid(),
        "enabled": False,
        "go_no_go": "go",
        "required_strategy_ids": [],
        "recent_trigger_count": 0,
        "active_trigger_count": 0,
        "selected_trigger_count": 0,
        "rejected_trigger_count": 0,
        "expired_trigger_count": 0,
        "triggers": [],
        "blockers": [],
        "clear_conditions": [],
    }


def failed_option_trigger_ledger_status(*, generated_at: datetime, error: str) -> dict[str, Any]:
    status = disabled_option_trigger_ledger_status(generated_at=generated_at)
    status.update(
        {
            "enabled": True,
            "go_no_go": "no_go",
            "lookup_failed": True,
            "blockers": ["option_trigger_ledger_lookup_failed"],
            "clear_conditions": ["restore_option_trigger_ledger_builder"],
            "error": error,
        }
    )
    return status


def write_option_trigger_ledger_artifact(*, repo_root: str | Path, status: dict[str, Any]) -> Path:
    path = Path(repo_root) / "runtime/readiness/option_trigger_ledger/option_trigger_ledger_latest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _latest_selection_for_strategy(rows: list[dict[str, Any]], *, strategy_id: str, after_ts: datetime | None) -> dict[str, Any] | None:
    matches: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("strategy_id") or "").strip() != strategy_id:
            continue
        ts = _parse_dt(row.get("generated_at"))
        if after_ts is not None and ts is not None and ts < after_ts:
            continue
        matches.append(row)
    matches.sort(key=lambda item: _parse_dt(item.get("generated_at")) or datetime.min.replace(tzinfo=ET))
    return matches[-1] if matches else None


def _latest_lifecycle_event(
    rows: list[dict[str, Any]],
    *,
    trigger: dict[str, Any],
    after_ts: datetime | None,
) -> dict[str, Any] | None:
    trigger_keys = {
        str(trigger.get("trigger_key") or "").strip(),
        str(trigger.get("trigger_id") or "").strip(),
    }
    trigger_keys.discard("")
    consumer = str(trigger.get("consumer_strategy_id") or "").strip()
    matches: list[dict[str, Any]] = []
    for row in rows:
        row_keys = {
            str(row.get("trigger_key") or "").strip(),
            str(row.get("trigger_id") or "").strip(),
        }
        row_keys.discard("")
        if trigger_keys and not (trigger_keys & row_keys):
            continue
        if consumer and str(row.get("consumer_strategy_id") or row.get("strategy_id") or "").strip() not in {"", consumer}:
            continue
        ts = _parse_dt(row.get("generated_at") or row.get("ts"))
        if after_ts is not None and ts is not None and ts < after_ts:
            continue
        matches.append(row)
    matches.sort(key=lambda item: _parse_dt(item.get("generated_at") or item.get("ts")) or datetime.min.replace(tzinfo=ET))
    return matches[-1] if matches else None


def _append_expired_lifecycle_event_once(
    *,
    lifecycle_path: Path,
    trigger: dict[str, Any],
    generated_at: datetime,
    reason: str,
    existing_events: list[dict[str, Any]],
) -> dict[str, Any] | None:
    trigger_key = str(trigger.get("trigger_key") or trigger.get("trigger_id") or "").strip()
    trigger_id = str(trigger.get("trigger_id") or trigger_key).strip()
    consumer = str(trigger.get("consumer_strategy_id") or "").strip()
    for row in existing_events or []:
        if str(row.get("status") or "").strip().lower() != "expired":
            continue
        row_key = str(row.get("trigger_key") or row.get("trigger_id") or "").strip()
        row_consumer = str(row.get("consumer_strategy_id") or row.get("strategy_id") or "").strip()
        if row_key == trigger_key and (not consumer or row_consumer in {"", consumer}):
            return row
    event = {
        "artifact_type": "option_trigger_lifecycle_event",
        "generated_at": generated_at.isoformat(),
        "trigger_key": trigger_key,
        "trigger_id": trigger_id,
        "consumer_strategy_id": consumer,
        "source_strategy_id": str(trigger.get("source_strategy_id") or ""),
        "underlying": str(trigger.get("underlying") or "").strip().upper(),
        "status": "expired",
        "reason": str(reason or "trigger_expired_without_terminal_lifecycle"),
        "terminal": True,
    }
    try:
        lifecycle_path.parent.mkdir(parents=True, exist_ok=True)
        with lifecycle_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        existing_events.append(event)
    except Exception:
        return None
    return event


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return rows
    for line in lines:
        try:
            value = json.loads(line)
        except Exception:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ET)
        return parsed.astimezone(ET)
    except Exception:
        return None


def _age_sec(ts: datetime | None, now: datetime) -> float | None:
    if ts is None:
        return None
    now_et = now.astimezone(ET) if now.tzinfo else now.replace(tzinfo=ET)
    return max(0.0, (now_et - ts.astimezone(ET)).total_seconds())


def _blockers(*, active_count: int, rejected_count: int) -> list[str]:
    blockers: list[str] = []
    if active_count > 0:
        blockers.append("option_trigger_active_without_terminal_verdict")
    if rejected_count > 0:
        blockers.append("option_trigger_rejected_contract_selection")
    return blockers
