from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Mapping


_LIFECYCLE_CACHE: Dict[str, Dict[str, Any]] = {}


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _parse_ts(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _load_state(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if isinstance(payload, dict):
        return payload
    return {}


def _build_lifecycle_status(*, level: str, silenced: bool) -> tuple[bool, str]:
    if level == "none":
        return False, "inactive"
    if silenced:
        return False, "silenced"
    return True, "active"


def apply_notice_lifecycle_engine(
    *,
    notice: Mapping[str, Any],
    state_path: str | Path,
    now_iso: str | None = None,
    silence_seconds: int = 3600,
    use_cache: bool = False,
) -> Dict[str, Any]:
    state_file = Path(state_path)
    now_dt = _parse_ts(now_iso) or datetime.now(timezone.utc)
    now_text = now_dt.isoformat()
    level = str(_as_dict(notice).get("level") or "none")
    throttle_key = str(_as_dict(notice).get("throttle_key") or "")

    cache_key = f"{state_file.resolve()}|{level}|{throttle_key}"
    if use_cache and cache_key in _LIFECYCLE_CACHE:
        return _as_dict(_LIFECYCLE_CACHE[cache_key])

    state_status = "ok"
    prev_state = _load_state(state_file)
    if state_file.exists() and not prev_state:
        state_status = "degraded"

    prev_first_seen = str(prev_state.get("first_seen") or "")
    prev_last_level = str(prev_state.get("last_level") or "")
    prev_last_throttle_key = str(prev_state.get("last_throttle_key") or "")
    prev_silenced_until_dt = _parse_ts(prev_state.get("silenced_until"))

    changed = (level != prev_last_level) or (throttle_key != prev_last_throttle_key)
    first_seen = now_text if changed or not prev_first_seen else prev_first_seen

    silenced = False
    silenced_until_text = ""
    if level != "none":
        if changed:
            silenced = False
            silenced_until_text = (now_dt + timedelta(seconds=max(1, int(silence_seconds)))).isoformat()
        else:
            if prev_silenced_until_dt and now_dt < prev_silenced_until_dt:
                silenced = True
                silenced_until_text = prev_silenced_until_dt.isoformat()
            else:
                silenced = False
                silenced_until_text = (now_dt + timedelta(seconds=max(1, int(silence_seconds)))).isoformat()

    active, lifecycle_status = _build_lifecycle_status(level=level, silenced=silenced)
    lifecycle = {
        "active": bool(active),
        "silenced": bool(silenced),
        "lifecycle_status": lifecycle_status,
        "first_seen": first_seen,
        "last_seen": now_text,
        "silenced_until": silenced_until_text,
        "last_level": level,
        "last_throttle_key": throttle_key,
    }

    persist_state = {
        "first_seen": first_seen,
        "last_seen": now_text,
        "last_level": level,
        "last_throttle_key": throttle_key,
        "silenced_until": silenced_until_text,
    }
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps(persist_state, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        state_status = "degraded"

    result = {
        "state_status": state_status,
        "lifecycle": lifecycle,
    }
    if use_cache:
        _LIFECYCLE_CACHE[cache_key] = result
    return result


def apply_notice_lifecycle_read(
    *,
    notice: Mapping[str, Any],
    state_path: str | Path,
    now_iso: str | None = None,
    silence_seconds: int = 3600,
    use_cache: bool = False,
) -> Dict[str, Any]:
    result = apply_notice_lifecycle_engine(
        notice=notice,
        state_path=state_path,
        now_iso=now_iso,
        silence_seconds=silence_seconds,
        use_cache=use_cache,
    )
    lifecycle = _as_dict(result.get("lifecycle"))
    next_notice = dict(_as_dict(notice))
    next_notice["silenced"] = bool(lifecycle.get("silenced", False))
    next_notice["active"] = bool(lifecycle.get("active", False))
    next_notice["lifecycle_status"] = str(lifecycle.get("lifecycle_status") or "inactive")
    next_notice["highlight"] = bool(next_notice["active"] and str(next_notice.get("level") or "none") in {"info", "warn", "high"})
    next_notice["lifecycle_state_path"] = str(Path(state_path))
    return {
        "notice": next_notice,
        "lifecycle": lifecycle,
        "state_status": str(result.get("state_status") or "ok"),
    }

