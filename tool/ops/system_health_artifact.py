#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def _parse_iso(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except Exception:
        return None


def _age_seconds(ts: datetime | None, now: datetime) -> float | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=ET)
    return max(0.0, (now.astimezone(ET) - ts.astimezone(ET)).total_seconds())


def latest_system_health_transition(
    *,
    audit_dir: Path,
    now_et: datetime | None = None,
    max_age_sec: float = 90.0,
) -> dict[str, Any]:
    """Read the latest SystemHealthController transition from runtime audit JSONL."""
    now = now_et.astimezone(ET) if now_et is not None else datetime.now(ET)
    latest: dict[str, Any] = {}
    latest_ts: datetime | None = None
    for path in sorted(audit_dir.glob("system-*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            continue
        for raw in lines:
            text = raw.strip()
            if not text:
                continue
            try:
                row = json.loads(text)
            except Exception:
                continue
            if not isinstance(row, dict):
                continue
            if str(row.get("component") or "") != "system_health_controller":
                continue
            if str(row.get("stage") or "") != "health_transition":
                continue
            ts = _parse_iso(row.get("ts_et") or (row.get("payload") or {}).get("ts_et"))
            if latest_ts is None or (ts is not None and ts > latest_ts):
                latest = row
                latest_ts = ts

    payload = dict(latest.get("payload") or {}) if latest else {}
    metadata = dict(payload.get("metadata") or {})
    readiness = dict(metadata.get("recovery_readiness") or {})
    blockers = [str(item) for item in (readiness.get("blockers") or [])]
    state = str(latest.get("status") or payload.get("state") or "").strip().lower()
    event_type = str(payload.get("event_type") or "").strip()
    age_sec = _age_seconds(latest_ts, now)
    try:
        max_age_value = float(max_age_sec)
    except Exception:
        max_age_value = 90.0
    stale = bool(latest) and max_age_value > 0 and (age_sec is None or age_sec > max_age_value)
    unhealthy = state not in {"running"} or bool(blockers) or stale
    stale_reason = "system_health_transition_stale" if stale else ""
    return {
        "audit_dir": str(audit_dir),
        "present": bool(latest),
        "state": state or None,
        "event_type": event_type or None,
        "reason": str(latest.get("reason") or payload.get("reason") or ""),
        "ts_et": latest_ts.isoformat() if latest_ts else None,
        "age_sec": age_sec,
        "max_age_sec": max_age_value,
        "stale": stale,
        "stale_reason": stale_reason,
        "platform_recovery_ready": metadata.get("platform_recovery_ready"),
        "recovery_readiness_ready": readiness.get("ready"),
        "recovery_readiness_blockers": blockers,
        "data_availability": readiness.get("data_availability") or {},
        "unhealthy": unhealthy,
        "raw": latest,
    }
