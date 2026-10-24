from __future__ import annotations

import json
import os
from collections import defaultdict, deque
from datetime import datetime
from threading import RLock
from typing import Any, Deque, Dict, List, Optional


_BACKEND = str(os.getenv("OPTIONS_QUOTE_LIVE_STATE_BACKEND", "off")).strip().lower()
_MAX_SNAPSHOTS = max(4, int(os.getenv("OPTIONS_QUOTE_LIVE_STATE_MAX_SNAPSHOTS", "64") or 64))
_SNAPSHOT_TTL_SEC = max(60, int(os.getenv("OPTIONS_QUOTE_LIVE_STATE_TTL_SEC", "900") or 900))
_REDIS_NS = str(os.getenv("OPTIONS_QUOTE_LIVE_STATE_NS", "options:quote:state:v1")).strip() or "options:quote:state:v1"

_LOCK = RLock()
_MEMORY: Dict[str, Deque[Dict[str, Any]]] = defaultdict(lambda: deque(maxlen=_MAX_SNAPSHOTS))
_REDIS_CLIENT: Any = None
_REDIS_DISABLED = False


def _backend() -> str:
    return _BACKEND


def _expiry_norm(value: Any) -> str:
    return str(value or "").strip()


def _source_tag() -> str:
    if _backend() == "redis":
        return "live_state"
    if _backend() == "memory":
        return "live_state_memory"
    return "unknown"


def _redis_client() -> Any:
    global _REDIS_CLIENT, _REDIS_DISABLED
    if _REDIS_DISABLED:
        return None
    if _REDIS_CLIENT is not None:
        return _REDIS_CLIENT
    try:
        from libs.db.redis_client import RedisClient

        _REDIS_CLIENT = RedisClient()
        return _REDIS_CLIENT
    except Exception:
        _REDIS_DISABLED = True
        return None


def _redis_key(underlying: str) -> str:
    return f"{_REDIS_NS}:{str(underlying or '').strip().upper()}"


def _prune_snapshots(items: List[Dict[str, Any]], now_ts: datetime) -> List[Dict[str, Any]]:
    keep: List[Dict[str, Any]] = []
    for snap in items:
        ts = snap.get("ts")
        if not isinstance(ts, datetime):
            continue
        if (now_ts - ts).total_seconds() <= _SNAPSHOT_TTL_SEC:
            keep.append(snap)
    keep.sort(key=lambda x: x.get("ts") if isinstance(x.get("ts"), datetime) else datetime.min, reverse=True)
    return keep[:_MAX_SNAPSHOTS]


def _rows_from_tuples(rows: List[tuple]) -> Dict[str, Dict[str, Any]]:
    grouped: Dict[str, Dict[str, Any]] = defaultdict(dict)
    for row in rows:
        if len(row) < 21:
            continue
        ts = row[0] if isinstance(row[0], datetime) else None
        if ts is None:
            continue
        underlying = str(row[1] or "").strip().upper()
        if not underlying:
            continue
        payload = {
            "ts": ts,
            "underlying": underlying,
            "expiry": row[2],
            "strike": float(row[3] or 0.0),
            "right": str(row[4] or ""),
            "dte": int(row[5] or 0),
            "bid": float(row[6] or 0.0),
            "ask": float(row[7] or 0.0),
            "mid": float(row[8] or 0.0),
            "iv": float(row[12] or 0.0),
            "delta": float(row[13] or 0.0),
            "gamma": float(row[14] or 0.0),
            "theta": float(row[15] or 0.0),
            "vega": float(row[16] or 0.0),
            "open_interest": int(row[11] or 0),
            "created_at": row[20] if isinstance(row[20], datetime) else ts,
            "quote_source": _source_tag(),
        }
        dedupe_key = f"{payload['ts']}|{payload['expiry']}|{payload['right']}|{payload['strike']}"
        grouped[underlying][dedupe_key] = payload
    return grouped


def update_from_ingest_rows(rows: List[tuple]) -> None:
    if not rows:
        return
    backend = _backend()
    if backend not in {"memory", "redis"}:
        return
    grouped = _rows_from_tuples(rows)
    if not grouped:
        return
    ts0 = rows[0][0] if isinstance(rows[0][0], datetime) else datetime.now()
    now_ts = datetime.now(ts0.tzinfo) if getattr(ts0, "tzinfo", None) else datetime.now()
    if backend == "memory":
        with _LOCK:
            for underlying, dedupe_map in grouped.items():
                snapshots = list(_MEMORY.get(underlying, deque()))
                snapshots = _prune_snapshots(snapshots, now_ts)
                snap_rows = list(dedupe_map.values())
                if not snap_rows:
                    continue
                snap_ts = snap_rows[0]["ts"]
                replaced = False
                for idx in range(len(snapshots)):
                    if snapshots[idx].get("ts") == snap_ts:
                        snapshots[idx] = {"ts": snap_ts, "rows": snap_rows}
                        replaced = True
                        break
                if not replaced:
                    snapshots.append({"ts": snap_ts, "rows": snap_rows})
                _MEMORY[underlying] = deque(snapshots, maxlen=_MAX_SNAPSHOTS)
        return

    client = _redis_client()
    if client is None:
        return
    for underlying, dedupe_map in grouped.items():
        key = _redis_key(underlying)
        snap_rows = list(dedupe_map.values())
        if not snap_rows:
            continue
        snap_ts = snap_rows[0]["ts"]
        field = snap_ts.isoformat()
        payload = {
            "ts": snap_ts.isoformat(),
            "rows": [
                {
                    **row,
                    "ts": row["ts"].isoformat() if isinstance(row.get("ts"), datetime) else row.get("ts"),
                    "created_at": row["created_at"].isoformat() if isinstance(row.get("created_at"), datetime) else row.get("created_at"),
                }
                for row in snap_rows
            ],
        }
        try:
            client.hset(key, field, json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
            client.expire(key, _SNAPSHOT_TTL_SEC)
        except Exception:
            continue


def _decode_redis_snapshots(raw_map: Dict[str, str]) -> List[Dict[str, Any]]:
    snapshots: List[Dict[str, Any]] = []
    for value in (raw_map or {}).values():
        try:
            payload = json.loads(value)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        try:
            ts = datetime.fromisoformat(str(payload.get("ts")))
        except Exception:
            continue
        rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
        decoded_rows = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            out = dict(row)
            for key in ("ts", "created_at"):
                if isinstance(out.get(key), str):
                    try:
                        out[key] = datetime.fromisoformat(out[key])
                    except Exception:
                        pass
            out["quote_source"] = str(out.get("quote_source") or _source_tag())
            decoded_rows.append(out)
        snapshots.append({"ts": ts, "rows": decoded_rows})
    return snapshots


def query_rows(
    *,
    underlying: str,
    right: Optional[str],
    dte_min: int,
    dte_max: int,
    ts_lb: datetime,
    ts_now: datetime,
    expiry: Any = None,
    strike: Optional[float] = None,
) -> List[Dict[str, Any]]:
    backend = _backend()
    if backend not in {"memory", "redis"}:
        return []
    symbol = str(underlying or "").strip().upper()
    if not symbol:
        return []
    snapshots: List[Dict[str, Any]] = []
    if backend == "memory":
        with _LOCK:
            snapshots = list(_MEMORY.get(symbol, deque()))
    else:
        client = _redis_client()
        if client is None:
            return []
        try:
            snapshots = _decode_redis_snapshots(client.hgetall(_redis_key(symbol)))
        except Exception:
            return []
    right_norm = str(right or "").strip().upper()
    expiry_norm = _expiry_norm(expiry)
    out: List[Dict[str, Any]] = []
    for snap in snapshots:
        for row in list(snap.get("rows") or []):
            ts = row.get("ts")
            if not isinstance(ts, datetime):
                continue
            if ts < ts_lb or ts > ts_now:
                continue
            if right_norm and str(row.get("right") or "").upper() != right_norm:
                continue
            dte = int(row.get("dte") or 0)
            if dte < int(dte_min) or dte > int(dte_max):
                continue
            if expiry_norm and _expiry_norm(row.get("expiry")) != expiry_norm:
                continue
            if strike is not None and abs(float(row.get("strike") or 0.0) - float(strike)) > 1e-9:
                continue
            out.append(dict(row))
    out.sort(
        key=lambda r: (
            r.get("ts") if isinstance(r.get("ts"), datetime) else datetime.min,
            r.get("created_at") if isinstance(r.get("created_at"), datetime) else datetime.min,
        ),
        reverse=True,
    )
    return out
