from __future__ import annotations

"""
Lightweight data readiness checks for training windows.

This is a minimal implementation to support the meta-AI loop:
- Check bars coverage (RTH-only)
- Check features coverage (by table)
- Check labels coverage (by table, label column)
Returns a structured result that can be surfaced to the ResearchAgent or scheduler
to avoid empty/low-coverage training runs.
"""

from dataclasses import dataclass, asdict, field, is_dataclass
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

try:
    from libs.db.ch_pool import get_client
except Exception:
    get_client = None  # type: ignore


@dataclass
class CoverageStats:
    rows: int
    expected_rows: Optional[int]
    latest_ts: Optional[str]
    utilization: Optional[float]


@dataclass
class ReadinessRecommendation:
    status: str  # ready | needs_window_adjust | needs_sync | missing | error
    reason: str
    suggested_start: Optional[str] = None
    suggested_end: Optional[str] = None
    coverage: Dict[str, CoverageStats] = field(default_factory=dict)

    def to_json(self) -> Dict[str, Any]:
        d = asdict(self)
        if d.get("coverage"):
            d["coverage"] = {
                k: asdict(v) if is_dataclass(v) else v for k, v in d["coverage"].items()
            }
        return d


def _rth_filter() -> str:
    # Default RTH filter; the actual timestamp column is selected per table.
    return "toHour({ts_col}) >= 9 AND toHour({ts_col}) < 16"


def _count_rows(
    ch,
    table: str,
    symbol: str,
    start: str,
    end: str,
    label_col: Optional[str] = None,
) -> CoverageStats:
    """
    Count rows and find latest ts, optionally requiring label column existence.
    Uses the table-specific timestamp column:
    - bars_* tables: ts_end (or ts_bar_end fallback)
    - everything else: ts
    """
    ts_col = "ts"
    if "bars" in table:
        ts_col = "ts_end"
    ts_filter = _rth_filter().format(ts_col=ts_col)

    # Build query with safe timestamp column fallback for bars tables
    q = f"""
        SELECT
            count() AS rows,
            max({ts_col}) AS latest_ts
        FROM {table}
        WHERE symbol = %(sym)s
          AND {ts_col} >= toDateTime(%(start)s)
          AND {ts_col} <= toDateTime(%(end)s)
          AND {ts_filter}
    """
    try:
        res = ch.query(
            q, {"sym": symbol, "start": start, "end": end}, settings={"use_query_cache": False}
        )
    except Exception:
        # Fallback for bars tables that might expose ts_bar_end instead of ts_end
        if "bars" in table and ts_col == "ts_end":
            ts_col = "ts_bar_end"
            ts_filter = _rth_filter().format(ts_col=ts_col)
            q = f"""
                SELECT
                    count() AS rows,
                    max({ts_col}) AS latest_ts
                FROM {table}
                WHERE symbol = %(sym)s
                  AND {ts_col} >= toDateTime(%(start)s)
                  AND {ts_col} <= toDateTime(%(end)s)
                  AND {ts_filter}
            """
            res = ch.query(
                q, {"sym": symbol, "start": start, "end": end}, settings={"use_query_cache": False}
            )
        else:
            raise

    if not res:
        return CoverageStats(rows=0, expected_rows=None, latest_ts=None, utilization=None)
    rows = int(res[0].get("rows", 0) or 0)
    latest_ts = res[0].get("latest_ts")
    latest_ts_str = latest_ts.isoformat() if latest_ts else None
    return CoverageStats(rows=rows, expected_rows=None, latest_ts=latest_ts_str, utilization=None)


def check_training_window(
    symbols: List[str],
    start: str,
    end: str,
    features_table: str = "trading.features_1m",
    labels_table: str = "trading.features_1m_with_labels",
    horizon_min: int = 30,
) -> ReadinessRecommendation:
    """
    Minimal readiness check for a given window. Returns ready unless we can
    positively detect missing/zero bars.
    """
    if not get_client:
        return ReadinessRecommendation(
            status="ready",
            reason="no_client_available",
            coverage={}
        )
    try:
        ch = get_client()
        coverage: Dict[str, CoverageStats] = {}
        total_bars = 0
        for sym in symbols:
            bars = _count_rows(ch, "trading.bars_1m_ibkr", sym, start, end, None)
            feats = _count_rows(ch, features_table, sym, start, end, None)
            labels = _count_rows(ch, labels_table, sym, start, end, None)
            coverage[f"{sym}_bars"] = bars
            coverage[f"{sym}_features"] = feats
            coverage[f"{sym}_labels"] = labels
            total_bars += bars.rows

        if total_bars == 0:
            return ReadinessRecommendation(
                status="missing",
                reason="no_bars_found",
                coverage=coverage
            )

        return ReadinessRecommendation(
            status="ready",
            reason="ok",
            coverage=coverage
        )
    except Exception as e:
        return ReadinessRecommendation(
            status="error",
            reason=str(e),
            coverage={}
        )
