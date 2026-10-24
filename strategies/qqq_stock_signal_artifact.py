from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from threading import RLock
from typing import Any, Dict, Optional


_LOCK = RLock()
_LATEST: dict[tuple[str, str, str], "StockEntrySignalArtifact"] = {}
_SIGNALS_BY_TRIGGER: dict[str, "StockEntrySignalArtifact"] = {}
_HANDOFF: dict[str, "StockOptionSignalHandoffRecord"] = {}
_HANDOFF_LOADED_PATH = ""
_REDIS_CLIENT: Any = None
_REDIS_DISABLED = False
_REDIS_RETRY_AT_MONO = 0.0
_REDIS_ERROR_STREAK = 0
_REDIS_CONNECT_ATTEMPTS = 0
_REDIS_CONNECT_SUCCESSES = 0
_REDIS_LAST_ERROR = ""
_LAST_WRITE_OK: Optional[bool] = None
_LAST_WRITE_ERROR = ""
_LAST_WRITE_TS: Optional[datetime] = None

_FORBIDDEN_PAYLOAD_KEYS = {
    "order_ref",
    "position_lot_id",
    "target_qty",
    "filled_qty",
    "broker_order_id",
    "execution_profile_id",
}


@dataclass(frozen=True)
class StockEntrySignalArtifact:
    signal_family: str
    underlying: str
    strategy_id: str
    trigger_ts: datetime
    minute: datetime
    direction: str
    option_side: str
    instrument: str
    entry_allowed: bool
    trigger_id: str = ""
    decision_id: str = ""
    trace_id: str = ""
    payload: Dict[str, Any] | None = None

    def to_wire(self) -> Dict[str, Any]:
        return {
            "signal_family": self.signal_family,
            "underlying": self.underlying,
            "strategy_id": self.strategy_id,
            "trigger_ts": self.trigger_ts.isoformat(),
            "minute": self.minute.isoformat(),
            "direction": self.direction,
            "option_side": self.option_side,
            "instrument": self.instrument,
            "entry_allowed": bool(self.entry_allowed),
            "trigger_id": self.trigger_id,
            "decision_id": self.decision_id,
            "trace_id": self.trace_id,
            "payload": dict(self.payload or {}),
        }

    @classmethod
    def from_wire(cls, value: Dict[str, Any]) -> Optional["StockEntrySignalArtifact"]:
        if not isinstance(value, dict):
            return None
        try:
            trigger_ts = datetime.fromisoformat(str(value.get("trigger_ts")))
            minute_raw = value.get("minute")
            minute = (
                datetime.fromisoformat(str(minute_raw))
                if minute_raw
                else trigger_ts.replace(second=0, microsecond=0)
            )
        except Exception:
            return None
        return cls(
            signal_family=_norm_family(str(value.get("signal_family") or "")),
            underlying=_norm_underlying(str(value.get("underlying") or "")),
            strategy_id=str(value.get("strategy_id") or "").strip(),
            trigger_ts=trigger_ts,
            minute=minute,
            direction=str(value.get("direction") or "").strip().upper(),
            option_side=str(value.get("option_side") or "").strip().upper(),
            instrument=str(value.get("instrument") or "").strip().upper(),
            entry_allowed=bool(value.get("entry_allowed")),
            trigger_id=str(value.get("trigger_id") or "").strip(),
            decision_id=str(value.get("decision_id") or "").strip(),
            trace_id=str(value.get("trace_id") or "").strip(),
            payload=dict(value.get("payload") or {}),
        )


@dataclass(frozen=True)
class StockOptionSignalHandoffRecord:
    trigger_key: str
    trigger_id: str
    source_strategy_id: str
    consumer_strategy_id: str
    underlying: str
    signal_family: str
    status: str
    reason: str
    ts: datetime
    terminal: bool = False
    payload: Dict[str, Any] | None = None

    def to_wire(self) -> Dict[str, Any]:
        return {
            "trigger_key": self.trigger_key,
            "trigger_id": self.trigger_id,
            "source_strategy_id": self.source_strategy_id,
            "consumer_strategy_id": self.consumer_strategy_id,
            "underlying": self.underlying,
            "signal_family": self.signal_family,
            "status": self.status,
            "reason": self.reason,
            "ts": self.ts.isoformat(),
            "terminal": bool(self.terminal),
            "payload": dict(self.payload or {}),
        }

    @classmethod
    def from_wire(cls, value: Dict[str, Any]) -> Optional["StockOptionSignalHandoffRecord"]:
        if not isinstance(value, dict):
            return None
        try:
            ts = datetime.fromisoformat(str(value.get("ts")))
        except Exception:
            return None
        return cls(
            trigger_key=str(value.get("trigger_key") or "").strip(),
            trigger_id=str(value.get("trigger_id") or "").strip(),
            source_strategy_id=str(value.get("source_strategy_id") or "").strip(),
            consumer_strategy_id=str(value.get("consumer_strategy_id") or "").strip(),
            underlying=_norm_underlying(str(value.get("underlying") or "")),
            signal_family=_norm_family(str(value.get("signal_family") or "")),
            status=str(value.get("status") or "").strip(),
            reason=str(value.get("reason") or "").strip(),
            ts=ts,
            terminal=bool(value.get("terminal")),
            payload=dict(value.get("payload") or {}),
        )


def _norm_underlying(underlying: str) -> str:
    return str(underlying or "").strip().upper()


def _norm_family(signal_family: str) -> str:
    return str(signal_family or "").strip().lower()


def _direction_to_option_side(direction: str) -> str:
    d = str(direction or "").strip().upper()
    if d == "LONG":
        return "CALL"
    if d == "SHORT":
        return "PUT"
    return ""


def _backend() -> str:
    mode = str(os.getenv("QQQ_STOCK_SIGNAL_BACKEND", "auto")).strip().lower()
    if mode not in {"auto", "redis", "memory", "off"}:
        return "auto"
    return mode


def _redis_enabled() -> bool:
    mode = _backend()
    return mode in {"auto", "redis"}


def _ttl_sec() -> int:
    return max(5, int(os.getenv("QQQ_STOCK_SIGNAL_TTL_SEC", "30") or 30))


def _redis_ns() -> str:
    return str(os.getenv("QQQ_STOCK_SIGNAL_NS", "signal:qqq:stock:v1")).strip() or "signal:qqq:stock:v1"


def _redis_key(signal_family: str, underlying: str) -> str:
    return f"{_redis_ns()}:{_norm_family(signal_family)}:{_norm_underlying(underlying)}"


def _handoff_key(artifact: StockEntrySignalArtifact | None = None, *, trigger_key: str = "") -> str:
    explicit = str(trigger_key or "").strip()
    if explicit:
        return explicit
    if artifact is None:
        return ""
    if artifact.trigger_id:
        return str(artifact.trigger_id)
    return "|".join(
        [
            _norm_family(artifact.signal_family),
            _norm_underlying(artifact.underlying),
            str(artifact.strategy_id or "").strip(),
            artifact.trigger_ts.isoformat(),
            str(artifact.direction or "").strip().upper(),
        ]
    )


def _handoff_path() -> str:
    return str(os.getenv("QQQ_STOCK_OPTION_HANDOFF_PATH", "runtime/state/qqq_stock_option_handoff.jsonl")).strip()


def _append_handoff_file(record: "StockOptionSignalHandoffRecord") -> None:
    path_s = _handoff_path()
    if not path_s:
        return
    try:
        from pathlib import Path

        path = Path(path_s)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record.to_wire(), separators=(",", ":"), ensure_ascii=False) + "\n")
    except Exception:
        return


def _ensure_handoff_loaded() -> None:
    global _HANDOFF_LOADED_PATH
    path_s = _handoff_path()
    if not path_s or _HANDOFF_LOADED_PATH == path_s:
        return
    try:
        from pathlib import Path

        path = Path(path_s)
        if not path.exists():
            _HANDOFF_LOADED_PATH = path_s
            return
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        _HANDOFF_LOADED_PATH = path_s
        return
    with _LOCK:
        for line in lines:
            try:
                raw = json.loads(line)
            except Exception:
                continue
            record = StockOptionSignalHandoffRecord.from_wire(raw)
            if record is None or not record.trigger_key:
                continue
            payload = dict(record.payload or {})
            artifact = StockEntrySignalArtifact.from_wire(dict(payload.get("signal_artifact") or {}))
            if artifact is not None:
                _SIGNALS_BY_TRIGGER[record.trigger_key] = artifact
                _LATEST[(artifact.signal_family, artifact.underlying, artifact.strategy_id)] = artifact
            _HANDOFF[record.trigger_key] = record
        _HANDOFF_LOADED_PATH = path_s


def _redis_client() -> Any:
    global _REDIS_CLIENT, _REDIS_DISABLED, _REDIS_RETRY_AT_MONO, _REDIS_ERROR_STREAK
    global _REDIS_CONNECT_ATTEMPTS, _REDIS_CONNECT_SUCCESSES, _REDIS_LAST_ERROR
    if not _redis_enabled():
        return None
    now_mono = time.monotonic()
    if _REDIS_DISABLED:
        if now_mono < _REDIS_RETRY_AT_MONO:
            return None
        _REDIS_DISABLED = False
    if _REDIS_CLIENT is not None:
        return _REDIS_CLIENT
    try:
        _REDIS_CONNECT_ATTEMPTS += 1
        _REDIS_CLIENT = _build_redis_client()
        _REDIS_CONNECT_SUCCESSES += 1
        _REDIS_ERROR_STREAK = 0
        _REDIS_LAST_ERROR = ""
        return _REDIS_CLIENT
    except Exception as exc:
        _REDIS_CLIENT = None
        _REDIS_ERROR_STREAK += 1
        backoff = min(30.0, 1.5 * (2 ** max(0, _REDIS_ERROR_STREAK - 1)))
        _REDIS_RETRY_AT_MONO = now_mono + backoff
        _REDIS_DISABLED = True
        _REDIS_LAST_ERROR = str(exc)
        return None


def _build_redis_client() -> Any:
    from libs.db.redis_client import RedisClient

    return RedisClient()


def _write_redis(artifact: StockEntrySignalArtifact) -> bool:
    global _REDIS_CLIENT, _REDIS_DISABLED, _REDIS_RETRY_AT_MONO, _REDIS_ERROR_STREAK, _REDIS_LAST_ERROR
    global _LAST_WRITE_OK, _LAST_WRITE_ERROR, _LAST_WRITE_TS
    client = _redis_client()
    if client is None:
        _LAST_WRITE_OK = False
        _LAST_WRITE_ERROR = "redis_client_unavailable"
        _LAST_WRITE_TS = datetime.now().astimezone()
        return False
    try:
        key = _redis_key(artifact.signal_family, artifact.underlying)
        client.hset(key, artifact.strategy_id, json.dumps(artifact.to_wire(), separators=(",", ":"), ensure_ascii=False))
        client.expire(key, _ttl_sec())
        _REDIS_ERROR_STREAK = 0
        _REDIS_LAST_ERROR = ""
        _LAST_WRITE_OK = True
        _LAST_WRITE_ERROR = ""
        _LAST_WRITE_TS = datetime.now().astimezone()
        return True
    except Exception as exc:
        _REDIS_CLIENT = None
        _REDIS_ERROR_STREAK += 1
        _REDIS_DISABLED = True
        _REDIS_RETRY_AT_MONO = time.monotonic() + min(30.0, 1.5 * (2 ** max(0, _REDIS_ERROR_STREAK - 1)))
        _REDIS_LAST_ERROR = str(exc)
        _LAST_WRITE_OK = False
        _LAST_WRITE_ERROR = str(exc)
        _LAST_WRITE_TS = datetime.now().astimezone()
        return False


def _read_redis(*, underlying: str, signal_family: str, source_strategy_id: str = "") -> Optional[StockEntrySignalArtifact]:
    global _REDIS_CLIENT, _REDIS_DISABLED, _REDIS_RETRY_AT_MONO, _REDIS_ERROR_STREAK, _REDIS_LAST_ERROR
    client = _redis_client()
    if client is None:
        return None
    key = _redis_key(signal_family, underlying)
    try:
        if str(source_strategy_id or "").strip():
            raw = client.hget(key, str(source_strategy_id).strip())
            if not raw:
                return None
            payload = json.loads(raw)
            return StockEntrySignalArtifact.from_wire(payload)

        raw_map = client.hgetall(key) or {}
        candidates = []
        for raw in raw_map.values():
            try:
                payload = json.loads(raw)
            except Exception:
                continue
            artifact = StockEntrySignalArtifact.from_wire(payload)
            if artifact is not None:
                candidates.append(artifact)
        if not candidates:
            return None
        return max(candidates, key=lambda row: row.trigger_ts)
    except Exception as exc:
        _REDIS_CLIENT = None
        _REDIS_ERROR_STREAK += 1
        _REDIS_DISABLED = True
        _REDIS_RETRY_AT_MONO = time.monotonic() + min(30.0, 1.5 * (2 ** max(0, _REDIS_ERROR_STREAK - 1)))
        _REDIS_LAST_ERROR = str(exc)
        return None


def signal_plane_health() -> Dict[str, Any]:
    mode = _backend()
    redis_ok = False
    using_redis = False
    retry_in_sec = 0.0
    if _REDIS_DISABLED:
        retry_in_sec = max(0.0, _REDIS_RETRY_AT_MONO - time.monotonic())
    if mode in {"auto", "redis"}:
        client = _redis_client()
        if client is not None:
            using_redis = True
            try:
                redis_ok = bool(client.health_check())
            except Exception:
                redis_ok = False
    if mode == "off":
        state = "off"
    elif mode == "memory":
        state = "healthy_memory"
    elif using_redis and redis_ok:
        state = "healthy_redis"
    elif mode == "redis":
        state = "degraded_no_redis"
    else:
        state = "degraded_fallback_memory"
    return {
        "backend_config": mode,
        "state": state,
        "using_redis": using_redis,
        "redis_ok": redis_ok,
        "fallback_memory_enabled": mode in {"auto", "memory"},
        "error_streak": int(_REDIS_ERROR_STREAK),
        "retry_in_sec": float(retry_in_sec),
        "connect_attempts": int(_REDIS_CONNECT_ATTEMPTS),
        "connect_successes": int(_REDIS_CONNECT_SUCCESSES),
        "last_error": str(_REDIS_LAST_ERROR or ""),
        "last_write_ok": _LAST_WRITE_OK,
        "last_write_error": str(_LAST_WRITE_ERROR or ""),
        "last_write_ts": _LAST_WRITE_TS.isoformat() if hasattr(_LAST_WRITE_TS, "isoformat") else "",
        "memory_entries": int(len(_LATEST)),
    }


def publish_stock_entry_signal(
    *,
    underlying: str,
    strategy_id: str,
    trigger_ts: datetime,
    direction: str,
    instrument: str,
    entry_allowed: bool,
    signal_family: str = "qqq_intraday_entry",
    trigger_id: str = "",
    decision_id: str = "",
    trace_id: str = "",
    payload: Optional[Dict[str, Any]] = None,
) -> StockEntrySignalArtifact:
    minute = trigger_ts.replace(second=0, microsecond=0)
    normalized_direction = str(direction or "").strip().upper()
    safe_payload = {
        str(k): v
        for k, v in dict(payload or {}).items()
        if str(k) not in _FORBIDDEN_PAYLOAD_KEYS
    }
    artifact = StockEntrySignalArtifact(
        signal_family=_norm_family(signal_family),
        underlying=_norm_underlying(underlying),
        strategy_id=str(strategy_id or "").strip(),
        trigger_ts=trigger_ts,
        minute=minute,
        direction=normalized_direction,
        option_side=_direction_to_option_side(normalized_direction),
        instrument=str(instrument or "").strip().upper(),
        entry_allowed=bool(entry_allowed),
        trigger_id=str(trigger_id or "").strip(),
        decision_id=str(decision_id or "").strip(),
        trace_id=str(trace_id or "").strip(),
        payload=safe_payload,
    )
    key = (artifact.signal_family, artifact.underlying, artifact.strategy_id)
    trigger_key = _handoff_key(artifact)
    with _LOCK:
        _LATEST[key] = artifact
        if trigger_key:
            _SIGNALS_BY_TRIGGER[trigger_key] = artifact
    _write_redis(artifact)
    record_stock_option_signal_handoff(
        artifact=artifact,
        status="published",
        reason="stock_signal_published",
        terminal=False,
        ts=trigger_ts,
        payload={
            "entry_allowed": bool(entry_allowed),
            "instrument": artifact.instrument,
            "signal_artifact": artifact.to_wire(),
        },
    )
    return artifact


def record_stock_option_signal_handoff(
    *,
    artifact: Optional[StockEntrySignalArtifact] = None,
    trigger_key: str = "",
    trigger_id: str = "",
    source_strategy_id: str = "",
    consumer_strategy_id: str = "",
    underlying: str = "",
    signal_family: str = "qqq_intraday_entry",
    status: str,
    reason: str = "",
    terminal: bool = False,
    ts: Optional[datetime] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> StockOptionSignalHandoffRecord:
    now = ts or datetime.now().astimezone()
    key = _handoff_key(artifact, trigger_key=trigger_key) or str(trigger_id or "").strip()
    if not key:
        key = f"{_norm_family(signal_family)}:{_norm_underlying(underlying)}:{now.isoformat()}"
    record = StockOptionSignalHandoffRecord(
        trigger_key=key,
        trigger_id=str(trigger_id or (artifact.trigger_id if artifact else "") or "").strip(),
        source_strategy_id=str(source_strategy_id or (artifact.strategy_id if artifact else "") or "").strip(),
        consumer_strategy_id=str(consumer_strategy_id or "").strip(),
        underlying=_norm_underlying(underlying or (artifact.underlying if artifact else "")),
        signal_family=_norm_family(signal_family or (artifact.signal_family if artifact else "qqq_intraday_entry")),
        status=str(status or "unknown").strip(),
        reason=str(reason or "").strip(),
        ts=now,
        terminal=bool(terminal),
        payload=dict(payload or {}),
    )
    with _LOCK:
        _HANDOFF[key] = record
    _append_handoff_file(record)
    return record


def get_stock_option_signal_handoff_records() -> list[StockOptionSignalHandoffRecord]:
    _ensure_handoff_loaded()
    with _LOCK:
        return list(_HANDOFF.values())


def get_pending_stock_entry_signal(
    *,
    underlying: str,
    signal_family: str = "qqq_intraday_entry",
    source_strategy_id: str = "",
) -> Optional[StockEntrySignalArtifact]:
    _ensure_handoff_loaded()
    family = _norm_family(signal_family)
    underlying_n = _norm_underlying(underlying)
    expected = str(source_strategy_id or "").strip()
    with _LOCK:
        candidates: list[StockEntrySignalArtifact] = []
        for trigger_key, artifact in _SIGNALS_BY_TRIGGER.items():
            if artifact.signal_family != family or artifact.underlying != underlying_n:
                continue
            if expected and artifact.strategy_id != expected:
                continue
            handoff = _HANDOFF.get(trigger_key)
            if handoff is not None and bool(handoff.terminal):
                continue
            candidates.append(artifact)
    if not candidates:
        return None
    return min(candidates, key=lambda row: row.trigger_ts)


def _read_memory(*, underlying: str, signal_family: str, source_strategy_id: str = "") -> Optional[StockEntrySignalArtifact]:
    family = _norm_family(signal_family)
    underlying_n = _norm_underlying(underlying)
    expected = str(source_strategy_id or "").strip()
    with _LOCK:
        if expected:
            return _LATEST.get((family, underlying_n, expected))
        candidates = [
            row
            for (f, u, _sid), row in _LATEST.items()
            if f == family and u == underlying_n
        ]
    if not candidates:
        return None
    return max(candidates, key=lambda row: row.trigger_ts)


def get_latest_stock_entry_signal(
    *,
    underlying: str,
    signal_family: str = "qqq_intraday_entry",
    source_strategy_id: str = "",
) -> Optional[StockEntrySignalArtifact]:
    _ensure_handoff_loaded()
    mode = _backend()
    if mode == "off":
        return None
    if mode == "redis":
        return _read_redis(
            underlying=underlying,
            signal_family=signal_family,
            source_strategy_id=source_strategy_id,
        )
    if mode == "memory":
        return _read_memory(
            underlying=underlying,
            signal_family=signal_family,
            source_strategy_id=source_strategy_id,
        )
    redis_artifact = _read_redis(
        underlying=underlying,
        signal_family=signal_family,
        source_strategy_id=source_strategy_id,
    )
    memory_artifact = _read_memory(
        underlying=underlying,
        signal_family=signal_family,
        source_strategy_id=source_strategy_id,
    )
    if redis_artifact is None:
        return memory_artifact
    if memory_artifact is None:
        return redis_artifact
    return redis_artifact if redis_artifact.trigger_ts >= memory_artifact.trigger_ts else memory_artifact


def clear_stock_entry_signal_artifacts(
    *,
    clear_remote: bool = False,
    forget_loaded: bool = False,
    underlying: str = "QQQ",
    signal_family: str = "qqq_intraday_entry",
    source_strategy_id: str = "",
) -> None:
    global _HANDOFF_LOADED_PATH
    with _LOCK:
        _LATEST.clear()
        _SIGNALS_BY_TRIGGER.clear()
        _HANDOFF.clear()
        _HANDOFF_LOADED_PATH = "" if forget_loaded else _handoff_path()
    if not clear_remote:
        return
    client = _redis_client()
    if client is None:
        return
    try:
        key = _redis_key(signal_family, underlying)
        sid = str(source_strategy_id or "").strip()
        if sid:
            if hasattr(client, "hdel"):
                client.hdel(key, sid)
            return
        client.delete(key)
    except Exception:
        return
