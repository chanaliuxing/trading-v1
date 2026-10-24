from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime
from threading import RLock
from typing import Any, Deque, Dict, List, Optional
import os


# Memory cache is intentionally bounded by both snapshot count and age.
# This prevents option-chain churn from growing process memory over time.
_MAX_SNAPSHOTS_PER_UNDERLYING = max(2, int(os.getenv("OPTIONS_QUOTE_CACHE_MAX_SNAPSHOTS", "6") or 6))
_MAX_ROWS_PER_SNAPSHOT = max(16, int(os.getenv("OPTIONS_QUOTE_CACHE_MAX_ROWS_PER_SNAPSHOT", "512") or 512))
_MAX_SNAPSHOT_AGE_SEC = max(60, int(os.getenv("OPTIONS_QUOTE_CACHE_MAX_AGE_SEC", "240") or 240))

_LOCK = RLock()
_STORE: Dict[str, Deque[Dict[str, Any]]] = defaultdict(
    lambda: deque(maxlen=_MAX_SNAPSHOTS_PER_UNDERLYING)
)


def _prune_underlying_locked(underlying: str, now_ts: datetime) -> None:
    buf = _STORE.get(underlying)
    if not buf:
        return
    keep: Deque[Dict[str, Any]] = deque(maxlen=_MAX_SNAPSHOTS_PER_UNDERLYING)
    for snap in buf:
        ts = snap.get("ts")
        if not isinstance(ts, datetime):
            continue
        if (now_ts - ts).total_seconds() <= _MAX_SNAPSHOT_AGE_SEC:
            keep.append(snap)
    _STORE[underlying] = keep


def update_from_ingest_rows(rows: List[tuple]) -> None:
    """
    Update in-process cache using rows prepared for trading.options_quotes_1m insert.

    Row layout (from tools/ingest_options_quotes.py):
      0 ts, 1 underlying, 2 expiry(date), 3 strike, 4 right, 5 dte, 6 bid, 7 ask,
      8 mid, 9 last, 10 volume, 11 open_interest, 12 iv, 13 delta, 14 gamma,
      15 theta, 16 vega, 17 spread_bps, 18 contracts_mult, 19 source, 20 created_at.
    """
    if not rows:
        return

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
        }
        dedupe_key = f"{payload['ts']}|{payload['expiry']}|{payload['right']}|{payload['strike']}"
        grouped[underlying][dedupe_key] = payload

    if not grouped:
        return

    first_ts = rows[0][0] if isinstance(rows[0][0], datetime) else None
    now_ts = datetime.now(first_ts.tzinfo) if isinstance(first_ts, datetime) and first_ts.tzinfo else datetime.now()

    with _LOCK:
        for underlying, dedup_map in grouped.items():
            _prune_underlying_locked(underlying, now_ts)
            snap_rows = list(dedup_map.values())
            snap_rows.sort(
                key=lambda r: (
                    r.get("created_at") if isinstance(r.get("created_at"), datetime) else datetime.min,
                    r.get("strike", 0.0),
                ),
                reverse=True,
            )
            if len(snap_rows) > _MAX_ROWS_PER_SNAPSHOT:
                snap_rows = snap_rows[:_MAX_ROWS_PER_SNAPSHOT]
            if not snap_rows:
                continue

            snap_ts = snap_rows[0]["ts"]
            buf = _STORE[underlying]
            replaced = False
            for idx in range(len(buf)):
                if buf[idx].get("ts") == snap_ts:
                    buf[idx] = {"ts": snap_ts, "rows": snap_rows}
                    replaced = True
                    break
            if not replaced:
                buf.append({"ts": snap_ts, "rows": snap_rows})


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
    symbol = str(underlying or "").strip().upper()
    if not symbol:
        return []
    right_norm = str(right or "").strip().upper()
    expiry_norm = str(expiry or "").strip()
    out: List[Dict[str, Any]] = []

    with _LOCK:
        now_ts = datetime.now(ts_now.tzinfo) if getattr(ts_now, "tzinfo", None) else datetime.now()
        _prune_underlying_locked(symbol, now_ts)
        snapshots = list(_STORE.get(symbol, []))

    for snap in snapshots:
        snap_rows = list(snap.get("rows") or [])
        for row in snap_rows:
            ts = row.get("ts")
            if not isinstance(ts, datetime):
                continue
            if ts < ts_lb or ts > ts_now:
                continue
            if right_norm and str(row.get("right") or "").upper() != right_norm:
                continue
            dte = int(row.get("dte") or 0)
            if dte < dte_min or dte > dte_max:
                continue
            if expiry_norm and str(row.get("expiry") or "") != expiry_norm:
                continue
            if strike is not None and abs(float(row.get("strike") or 0.0) - float(strike)) > 1e-9:
                continue
            out.append(row)

    out.sort(
        key=lambda r: (
            r.get("ts") if isinstance(r.get("ts"), datetime) else datetime.min,
            r.get("created_at") if isinstance(r.get("created_at"), datetime) else datetime.min,
        ),
        reverse=True,
    )
    return out
