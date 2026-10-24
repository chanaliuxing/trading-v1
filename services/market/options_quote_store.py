from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from libs.db.ch_pool import get_client
from libs.options.internal_symbol import parse_internal_opt_symbol
from services.market.options_quote_live_state import query_rows as query_live_state_rows
from services.market.options_quote_memory_cache import query_rows as query_memory_rows

QUOTE_STORE_HOTPATH_CONTRACT_VERSION = "2026-04-06.v1"
HOTPATH_QUOTE_REQUIRED_KEYS = ("bid", "ask", "mid", "age_sec", "quote_source")


def _normalize_expiry(value: Any) -> str:
    if isinstance(value, date):
        return value.isoformat()
    return str(value or "").strip()


def _with_quote_source(rows: List[Dict[str, Any]], *, source: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in rows or []:
        payload = dict(row)
        payload["quote_source"] = str(payload.get("quote_source") or source)
        out.append(payload)
    return out


def _dedupe_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    deduped: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        ts = row.get("ts")
        key = "|".join(
            [
                str(ts),
                _normalize_expiry(row.get("expiry")),
                str(row.get("right") or ""),
                str(float(row.get("strike") or 0.0)),
            ]
        )
        existing = deduped.get(key)
        if existing is None:
            deduped[key] = row
            continue
        row_src = row.get("created_at") or row.get("ts")
        existing_src = existing.get("created_at") or existing.get("ts")
        if isinstance(row_src, datetime) and isinstance(existing_src, datetime) and row_src >= existing_src:
            deduped[key] = row
    ordered = list(deduped.values())
    ordered.sort(
        key=lambda row: (
            row.get("ts") if isinstance(row.get("ts"), datetime) else datetime.min,
            row.get("created_at") if isinstance(row.get("created_at"), datetime) else datetime.min,
        ),
        reverse=True,
    )
    return ordered


def query_option_rows(
    *,
    underlying: str,
    ts_lb: datetime,
    ts_now: datetime,
    dte_min: int,
    dte_max: int,
    right: Optional[str] = None,
    expiry: Any = None,
    strike: Optional[float] = None,
    limit: Optional[int] = None,
    fallback_to_ch: bool = True,
) -> List[Dict[str, Any]]:
    expiry_norm = _normalize_expiry(expiry)
    live_rows = query_live_state_rows(
        underlying=underlying,
        right=right,
        dte_min=dte_min,
        dte_max=dte_max,
        ts_lb=ts_lb,
        ts_now=ts_now,
        expiry=expiry_norm or None,
        strike=strike,
    )
    memory_rows = query_memory_rows(
        underlying=underlying,
        right=right,
        dte_min=dte_min,
        dte_max=dte_max,
        ts_lb=ts_lb,
        ts_now=ts_now,
        expiry=expiry_norm or None,
        strike=strike,
    )
    live_rows = _with_quote_source(live_rows, source="live_state")
    memory_rows = _with_quote_source(memory_rows, source="memory")
    mem_and_live = _dedupe_rows(live_rows + memory_rows)
    if not fallback_to_ch:
        if limit is not None:
            return mem_and_live[: max(0, int(limit))]
        return mem_and_live

    ch = get_client()
    rows = ch.query(
        """
        SELECT
          ts,
          created_at,
          expiry,
          strike,
          right,
          dte,
          bid,
          ask,
          mid,
          iv,
          delta,
          gamma,
          theta,
          vega,
          open_interest
        FROM trading.options_quotes_1m
        WHERE underlying = {u:String}
          AND dte >= {dte_min:Int32}
          AND dte <= {dte_max:Int32}
          AND ts >= {ts_lb:DateTime64(3, 'America/New_York')}
          AND ts <= {ts_now:DateTime64(3, 'America/New_York')}
          AND ({right_filter:UInt8} = 0 OR right = {r:String})
          AND ({expiry_filter:UInt8} = 0 OR expiry = toDate({expiry:String}))
          AND ({strike_filter:UInt8} = 0 OR strike = {strike:Float64})
        ORDER BY ts DESC
        """,
        parameters={
            "u": underlying,
            "dte_min": int(dte_min),
            "dte_max": int(dte_max),
            "ts_lb": ts_lb,
            "ts_now": ts_now,
            "right_filter": 1 if right else 0,
            "r": str(right or ""),
            "expiry_filter": 1 if expiry_norm else 0,
            "expiry": expiry_norm or "1970-01-01",
            "strike_filter": 1 if strike is not None else 0,
            "strike": float(strike or 0.0),
        },
    )
    ch_rows = _with_quote_source(list(rows or []), source="clickhouse")
    merged = _dedupe_rows(mem_and_live + ch_rows)
    if limit is not None:
        return merged[: max(0, int(limit))]
    return merged


def latest_quote_age_sec(
    *,
    underlying: str,
    now: datetime,
    day_start: datetime,
    day_end: datetime,
    dte_min: int,
    dte_max: int,
    expiry: Any = None,
) -> Optional[float]:
    status = latest_quote_status(
        underlying=underlying,
        now=now,
        day_start=day_start,
        day_end=day_end,
        dte_min=dte_min,
        dte_max=dte_max,
        expiry=expiry,
    )
    if not status:
        return None
    return float(status["age_sec"])


def latest_quote_status(
    *,
    underlying: str,
    now: datetime,
    day_start: datetime,
    day_end: datetime,
    dte_min: int,
    dte_max: int,
    expiry: Any = None,
) -> Optional[Dict[str, Any]]:
    memory_rows = query_memory_rows(
        underlying=underlying,
        right=None,
        dte_min=dte_min,
        dte_max=dte_max,
        ts_lb=day_start,
        ts_now=now,
        expiry=_normalize_expiry(expiry) or None,
    )
    live_rows = query_live_state_rows(
        underlying=underlying,
        right=None,
        dte_min=dte_min,
        dte_max=dte_max,
        ts_lb=day_start,
        ts_now=now,
        expiry=_normalize_expiry(expiry) or None,
    )
    latest_src: Optional[datetime] = None
    memory_src: Optional[datetime] = None
    live_src: Optional[datetime] = None
    for row in live_rows:
        src = row.get("created_at") or row.get("ts")
        if isinstance(src, datetime) and (live_src is None or src > live_src):
            live_src = src
    for row in memory_rows:
        src = row.get("created_at") or row.get("ts")
        if isinstance(src, datetime) and (memory_src is None or src > memory_src):
            memory_src = src
    ch = get_client()
    rows = ch.query(
        """
        SELECT
          count() AS row_count,
          max(created_at) AS last_created_at,
          max(ts) AS last_ts
        FROM trading.options_quotes_1m
        WHERE underlying = {u:String}
          AND dte >= {dte_min:Int32}
          AND dte <= {dte_max:Int32}
          AND ts >= {day_start:DateTime64(3, 'America/New_York')}
          AND ts < {day_end:DateTime64(3, 'America/New_York')}
          AND ({expiry_filter:UInt8} = 0 OR expiry = toDate({expiry:String}))
        """,
        parameters={
            "u": underlying,
            "dte_min": int(dte_min),
            "dte_max": int(dte_max),
            "day_start": day_start,
            "day_end": day_end,
            "expiry_filter": 1 if expiry else 0,
            "expiry": _normalize_expiry(expiry) or "1970-01-01",
        },
    )
    row = rows[0] if rows else {}
    try:
        row_count = int(row.get("row_count") or 0)
    except Exception:
        row_count = 0
    db_src = (row.get("last_created_at") or row.get("last_ts")) if row_count > 0 else None
    latest_src = memory_src
    source = "memory"
    if isinstance(live_src, datetime) and (latest_src is None or live_src > latest_src):
        latest_src = live_src
        source = "live_state"
    if isinstance(db_src, datetime) and (latest_src is None or db_src > latest_src):
        latest_src = db_src
        source = "clickhouse"
    if not isinstance(latest_src, datetime):
        return None
    src = latest_src.astimezone(now.tzinfo) if latest_src.tzinfo and now.tzinfo else latest_src
    return {
        "age_sec": max(0.0, (now - src).total_seconds()),
        "source": source,
        "quote_source": source,
        "memory_src": memory_src,
        "live_state_src": live_src,
        "clickhouse_src": db_src if isinstance(db_src, datetime) else None,
        "selected_src": latest_src,
    }


def quote_minute_coverage(
    *,
    underlying: str,
    window_start: datetime,
    window_end: datetime,
    dte_min: int,
    dte_max: int,
    expiry: Any = None,
) -> Dict[str, int]:
    rows = query_memory_rows(
        underlying=underlying,
        right=None,
        ts_lb=window_start,
        ts_now=window_end,
        dte_min=dte_min,
        dte_max=dte_max,
        expiry=_normalize_expiry(expiry) or None,
    )
    live_rows = query_live_state_rows(
        underlying=underlying,
        right=None,
        ts_lb=window_start,
        ts_now=window_end,
        dte_min=dte_min,
        dte_max=dte_max,
        expiry=_normalize_expiry(expiry) or None,
    )
    rows = _dedupe_rows(list(rows) + list(live_rows))
    covered = set()
    nbbo = set()
    for row in rows:
        ts = row.get("ts")
        if not isinstance(ts, datetime):
            continue
        minute = ts.replace(second=0, microsecond=0)
        covered.add(minute)
        if float(row.get("bid") or 0.0) > 0.0 and float(row.get("ask") or 0.0) > 0.0:
            nbbo.add(minute)

    expected_minutes = max(1, int((window_end - window_start).total_seconds() // 60))
    if len(covered) >= expected_minutes:
        return {
            "covered_minutes": len(covered),
            "nbbo_minutes": len(nbbo),
        }

    # Memory cache can be intentionally shallow (few snapshots). When in-memory
    # coverage is below the requested window, augment from ClickHouse aggregate.
    ch = get_client()
    db_rows = ch.query(
        """
        SELECT
          countDistinct(toStartOfMinute(ts)) AS covered_minutes,
          countDistinctIf(toStartOfMinute(ts), bid > 0 AND ask > 0) AS nbbo_minutes
        FROM trading.options_quotes_1m
        WHERE underlying = {u:String}
          AND dte >= {dte_min:Int32}
          AND dte <= {dte_max:Int32}
          AND ts >= {ts_start:DateTime64(3, 'America/New_York')}
          AND ts < {ts_end:DateTime64(3, 'America/New_York')}
          AND ({expiry_filter:UInt8} = 0 OR expiry = toDate({expiry:String}))
        """,
        parameters={
            "u": underlying,
            "dte_min": int(dte_min),
            "dte_max": int(dte_max),
            "ts_start": window_start,
            "ts_end": window_end,
            "expiry_filter": 1 if expiry else 0,
            "expiry": _normalize_expiry(expiry) or "1970-01-01",
        },
    )
    row = db_rows[0] if db_rows else {}
    db_covered = int(row.get("covered_minutes", 0) or 0)
    db_nbbo = int(row.get("nbbo_minutes", 0) or 0)
    return {
        "covered_minutes": max(len(covered), db_covered),
        "nbbo_minutes": max(len(nbbo), db_nbbo),
    }


def get_quote_for_opt_symbol(
    *,
    now: datetime,
    opt_symbol: str,
    lookback_min: int,
    max_age_sec: float,
    dte_min: int,
    dte_max: int,
    fallback_to_ch: bool = True,
) -> Optional[Dict[str, float]]:
    parts = parse_internal_opt_symbol(opt_symbol)
    expiry = f"{parts.expiry_ymd[0:4]}-{parts.expiry_ymd[4:6]}-{parts.expiry_ymd[6:8]}"
    ts_now = now.replace(second=0, microsecond=0)
    ts_lb = ts_now - timedelta(minutes=max(1, int(lookback_min)))
    rows = query_option_rows(
        underlying=parts.underlying,
        right=parts.right,
        expiry=expiry,
        strike=float(parts.strike),
        ts_lb=ts_lb,
        ts_now=ts_now,
        dte_min=dte_min,
        dte_max=dte_max,
        limit=8,
        fallback_to_ch=fallback_to_ch,
    )
    for row in rows:
        src = row.get("created_at") or row.get("ts")
        if isinstance(src, datetime):
            age_dt = src.astimezone(now.tzinfo) if src.tzinfo and now.tzinfo else src
            age_sec = max(0.0, (now - age_dt).total_seconds())
        else:
            age_sec = 9_999.0
        if age_sec > max_age_sec:
            continue
        bid = float(row.get("bid") or 0.0)
        ask = float(row.get("ask") or 0.0)
        mid = float(row.get("mid") or 0.0)
        if bid <= 0.0 and ask <= 0.0 and mid <= 0.0:
            continue
        if mid <= 0.0 and bid > 0.0 and ask > 0.0:
            mid = (bid + ask) / 2.0
        return {
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "age_sec": age_sec,
            "quote_source": str(row.get("quote_source") or "unknown"),
        }
    return None


def quote_store_hotpath_contract() -> Dict[str, Any]:
    return {
        "version": QUOTE_STORE_HOTPATH_CONTRACT_VERSION,
        "entry_path": {
            "reader": "query_option_rows",
            "default_fallback_to_ch": False,
            "required_result_keys": ["quote_source"],
        },
        "mark_path": {
            "reader": "get_quote_for_opt_symbol",
            "default_fallback_to_ch": True,
            "required_result_keys": list(HOTPATH_QUOTE_REQUIRED_KEYS),
        },
        "health_path": {
            "reader": "latest_quote_status",
            "required_result_keys": ["age_sec", "source", "quote_source", "selected_src"],
        },
        "replay_audit_path": {
            "reader": "query_option_rows",
            "require_explicit_fallback_flag": True,
        },
    }


def validate_hotpath_quote_payload(payload: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(payload, dict):
        return False
    return all(k in payload for k in HOTPATH_QUOTE_REQUIRED_KEYS)
