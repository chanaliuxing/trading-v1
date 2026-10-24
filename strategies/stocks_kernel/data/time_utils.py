from __future__ import annotations

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")


def coerce_epoch_seconds(value: Any, *, default: float = 0.0) -> float:
    if value is None:
        return float(default)
    if isinstance(value, datetime):
        return float(value.timestamp())
    if isinstance(value, date):
        return float(datetime.combine(value, datetime.min.time()).timestamp())
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def coerce_feature_timestamp_seconds(value: Any, *, default: float = 0.0) -> float:
    if value is None:
        return float(default)
    if isinstance(value, (datetime, date)):
        return coerce_epoch_seconds(value, default=default)
    raw = value
    if isinstance(raw, str):
        parsed_dt = None
        try:
            parsed_dt = datetime.fromisoformat(raw.strip())
        except ValueError:
            parsed_dt = None
        if parsed_dt is not None:
            return coerce_epoch_seconds(parsed_dt, default=default)
        try:
            raw = float(raw.strip())
        except (TypeError, ValueError):
            return float(default)
    try:
        numeric = float(raw)
    except (TypeError, ValueError):
        return float(default)
    if numeric > 1e14:
        return numeric / 1_000_000.0
    if numeric > 1e11:
        return numeric / 1_000.0
    return numeric


def resolve_session_label(value: Any) -> str | None:
    raw = str(value or "").strip().upper()
    if not raw:
        return None
    aliases = {
        "PREMARKET": "PRE",
        "PRE": "PRE",
        "RTH": "RTH",
        "REGULAR": "RTH",
        "AFTERHOURS": "POST",
        "POST": "POST",
        "OVERNIGHT": "OVERNIGHT",
    }
    return aliases.get(raw, raw or None)


def session_label_from_epoch_seconds(ts_epoch_s: float) -> str | None:
    if ts_epoch_s <= 0:
        return None
    now = datetime.fromtimestamp(float(ts_epoch_s), tz=ET)
    time_decimal = now.hour + now.minute / 60.0 + now.second / 3600.0
    if time_decimal >= 20.0 or time_decimal < 3.833333:
        return "OVERNIGHT"
    if 4.0 <= time_decimal < 9.5:
        return "PRE"
    if 9.5 <= time_decimal < 16.0:
        return "RTH"
    if 16.0 <= time_decimal < 20.0:
        return "POST"
    return None
