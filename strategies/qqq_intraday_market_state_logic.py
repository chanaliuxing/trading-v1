from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo
from typing import Any

ET = ZoneInfo("America/New_York")


def coerce_et_datetime(value: Any) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=ET)
        return value.astimezone(ET)
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=ET)
    return parsed.astimezone(ET)


def minute_history_map(*, rows: list[dict[str, Any]] | None, field: str) -> dict[dt.datetime, float]:
    values: dict[dt.datetime, float] = {}
    for row in list(rows or []):
        if not isinstance(row, dict):
            continue
        minute = coerce_et_datetime(row.get("minute"))
        try:
            value = float(row.get(field))
        except (TypeError, ValueError):
            continue
        if minute is None:
            continue
        values[minute.replace(second=0, microsecond=0)] = float(value)
    return values


def history_value(*, rows: list[dict[str, Any]] | None, minute_ts: dt.datetime, field: str) -> float | None:
    minute_iso = minute_ts.isoformat()
    for row in reversed(list(rows or [])):
        if not isinstance(row, dict):
            continue
        if str(row.get("minute") or "") != minute_iso:
            continue
        try:
            return float(row.get(field))
        except (TypeError, ValueError):
            return None
    return None


def upsert_history_row(*, rows: list[dict[str, Any]] | None, minute_iso: str, field: str, value: float, keep: int = 12) -> list[dict[str, Any]]:
    history: list[dict[str, Any]] = [row for row in list(rows or []) if isinstance(row, dict)]
    replaced = False
    for row in history:
        if str(row.get("minute") or "") == minute_iso:
            row[field] = float(value)
            replaced = True
            break
    if not replaced:
        history.append({"minute": minute_iso, field: float(value)})
    history.sort(key=lambda row: str(row.get("minute") or ""))
    return history[-max(1, int(keep)):]


def upsert_live_sample(*, rows: list[dict[str, Any]] | None, ts_iso: str, close_now: float, keep: int = 180) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = [row for row in list(rows or []) if isinstance(row, dict)]
    replaced = False
    for row in samples:
        if str(row.get("ts") or "") == ts_iso:
            row["close"] = float(close_now)
            replaced = True
            break
    if not replaced:
        samples.append({"ts": ts_iso, "close": float(close_now)})
    samples.sort(key=lambda row: str(row.get("ts") or ""))
    return samples[-max(1, int(keep)):]


def live_samples(*, rows: list[dict[str, Any]] | None) -> list[tuple[dt.datetime, float]]:
    samples: list[tuple[dt.datetime, float]] = []
    for row in list(rows or []):
        if not isinstance(row, dict):
            continue
        row_dt = coerce_et_datetime(row.get("ts"))
        try:
            row_value = float(row.get("close"))
        except (TypeError, ValueError):
            continue
        if row_dt is None:
            continue
        samples.append((row_dt, float(row_value)))
    return samples


def live_value_at_or_before(*, rows: list[dict[str, Any]] | None, target_ts: dt.datetime) -> float | None:
    best_dt: dt.datetime | None = None
    best_value: float | None = None
    for row_dt, row_value in live_samples(rows=rows):
        if row_dt > target_ts:
            continue
        if best_dt is None or row_dt > best_dt:
            best_dt = row_dt
            best_value = row_value
    return best_value


def session_pos_day_from_history(*, rows: list[dict[str, Any]] | None, eval_now: dt.datetime, current_close: float) -> float | None:
    session_start = eval_now.replace(hour=9, minute=30, second=0, microsecond=0)
    if eval_now <= session_start:
        return None
    prices: list[float] = []
    for row in list(rows or []):
        if not isinstance(row, dict):
            continue
        minute = coerce_et_datetime(row.get("minute"))
        try:
            close = float(row.get("close"))
        except (TypeError, ValueError):
            continue
        if minute is None:
            continue
        if minute.date() != eval_now.date():
            continue
        if minute < session_start or minute > eval_now:
            continue
        prices.append(float(close))
    if not prices:
        return None
    prices.append(float(current_close))
    p_min = min(prices)
    p_max = max(prices)
    if p_max - p_min <= 1e-9:
        return 0.5
    return float((float(current_close) - p_min) / max(p_max - p_min, 1e-9))
