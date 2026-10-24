from __future__ import annotations

"""
Lightweight training window readiness checker.
- Aligns with research/blueprint: bars/features/labels coverage + freshness
- Provides simple recommendations for shrink window or sync active table
"""

from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Any

try:
    from scripts.ai_trader.research.readiness import check_training_window
except Exception:
    check_training_window = None  # type: ignore


@dataclass
class ReadinessRecommendation:
    status: str  # ready | needs_window_adjust | needs_sync | missing | error
    reason: str
    suggested_start: Optional[str] = None
    suggested_end: Optional[str] = None
    coverage: Dict[str, Dict] = None

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class CoverageReport:
    table_name: str
    total_bars: int
    expected_bars: int
    utilization: float
    latest_ts: Optional[str] = None


@dataclass
class DataReadinessResult:
    status: str  # ready | needs_window_adjust | needs_sync | not_ready
    reason: str
    bars_coverage: CoverageReport
    features_coverage: CoverageReport
    labels_coverage: CoverageReport
    suggested_start: Optional[date] = None
    suggested_end: Optional[date] = None
    action: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class DataReadinessChecker:
    def __init__(self, min_utilization: float = 0.3, clamp_days_1: int = 30, clamp_days_2: int = 45):
        self.min_utilization = min_utilization
        self.clamp_days_1 = clamp_days_1
        self.clamp_days_2 = clamp_days_2

    def check(
        self,
        symbols: List[str],
        start: str,
        end: str,
        features_table: str = "trading.features_1m",
        labels_table: str = "trading.features_1m_with_labels",
    ) -> ReadinessRecommendation:
        if not check_training_window:
            return ReadinessRecommendation(status="ready", reason="no_checker_available", coverage={})

        effective_features_table = features_table if features_table is not None else "trading.features_1m"
        effective_labels_table = labels_table if labels_table is not None else "trading.features_1m_with_labels"
        result = check_training_window(
            symbols=symbols,
            start=start,
            end=end,
            features_table=effective_features_table,
            labels_table=effective_labels_table,
            horizon_min=30,
        )
        cov = result.coverage or {}
        total_rows = 0
        expected_rows = 0
        for v in cov.values():
            try:
                if v and "rows" in v:
                    total_rows += int(v["rows"] or 0)
            except Exception:
                continue
        # Heuristic: 6.5 RTH hours * 60 minutes ≈ 390 bars/day
        try:
            start_dt = datetime.fromisoformat(start.replace(" ", "T"))
            end_dt = datetime.fromisoformat(end.replace(" ", "T"))
            days = max(1, (end_dt - start_dt).days + 1)
            expected_rows = days * 390 * len(symbols)
        except Exception:
            expected_rows = None

        utilization = (total_rows / expected_rows) if expected_rows else None

        if result.status == "missing" or (utilization is not None and utilization < self.min_utilization):
            # Suggest clamping to recent window with data (defaults to last full day)
            try:
                anchor = datetime.now(timezone.utc).date()
                end_clamped = datetime.combine(anchor, datetime.min.time()).replace(tzinfo=timezone.utc)
                # Prefer a 30d clamp, fallback to 45d
                start_clamped = end_clamped - timedelta(days=self.clamp_days_1)
                return ReadinessRecommendation(
                    status="needs_window_adjust",
                    reason="low_utilization",
                    suggested_start=start_clamped.isoformat(),
                    suggested_end=end_clamped.isoformat(),
                    coverage=cov,
                )
            except Exception:
                return ReadinessRecommendation(
                    status="needs_window_adjust",
                    reason="low_utilization",
                    suggested_start=None,
                    suggested_end=None,
                    coverage=cov,
                )

        # ready or just ok
        return ReadinessRecommendation(
            status="ready",
            reason=result.reason,
            suggested_start=start,
            suggested_end=end,
            coverage=cov,
        )

    def check_training_window(
        self,
        symbol: str,
        start: date | str,
        end: date | str,
        horizon: str = "30m",
        min_utilization: float = 0.5,
        features_table: Optional[str] = "trading.features_1m",
    ) -> DataReadinessResult:
        """Compatibility API used by integration tests and adaptive window logic."""
        del horizon  # reserved for future finer-grained checks

        def _to_date(x: date | str) -> date:
            if isinstance(x, date):
                return x
            sx = str(x).replace("T", " ").strip()
            return datetime.fromisoformat(sx).date()

        start_d = _to_date(start)
        end_d = _to_date(end)
        start_s = f"{start_d.isoformat()} 09:30:00"
        end_s = f"{end_d.isoformat()} 16:00:00"
        days = max(1, (end_d - start_d).days + 1)
        expected = 390 * days

        bars_rows = 0
        bars_latest = None
        feat_rows = 0
        feat_latest = None
        labels_rows = expected
        labels_latest = None
        table = str(features_table or "trading.features_1m")

        if check_training_window:
            out = check_training_window(
                symbols=[symbol],
                start=start_s,
                end=end_s,
                features_table=table,
                labels_table="trading.features_1m_with_labels",
                horizon_min=30,
            )
            cov = out.coverage or {}
            b = cov.get(f"{symbol}_bars")
            f = cov.get(f"{symbol}_features")
            l = cov.get(f"{symbol}_labels")
            try:
                bars_rows = int((getattr(b, "rows", None) if b is not None else 0) or 0)
                bars_latest = getattr(b, "latest_ts", None) if b is not None else None
            except Exception:
                pass
            try:
                feat_rows = int((getattr(f, "rows", None) if f is not None else 0) or 0)
                feat_latest = getattr(f, "latest_ts", None) if f is not None else None
            except Exception:
                pass
            # For non-label tables, labels check is considered skipped/pass.
            if "with_labels" in table:
                try:
                    labels_rows = int((getattr(l, "rows", None) if l is not None else 0) or 0)
                    labels_latest = getattr(l, "latest_ts", None) if l is not None else None
                except Exception:
                    labels_rows = 0

        bars_cov = CoverageReport(
            table_name="trading.bars_1m_ibkr",
            total_bars=bars_rows,
            expected_bars=expected,
            utilization=float(bars_rows / expected) if expected > 0 else 0.0,
            latest_ts=bars_latest,
        )
        feat_cov = CoverageReport(
            table_name=table,
            total_bars=feat_rows,
            expected_bars=expected,
            utilization=float(feat_rows / expected) if expected > 0 else 0.0,
            latest_ts=feat_latest,
        )
        labels_cov = CoverageReport(
            table_name=("trading.features_1m_with_labels" if "with_labels" in table else "labels_check_skipped"),
            total_bars=labels_rows,
            expected_bars=expected,
            utilization=float(labels_rows / expected) if expected > 0 else 1.0,
            latest_ts=labels_latest,
        )

        status = "ready"
        reason = "ok"
        action = None
        suggested_start = None
        suggested_end = None

        if bars_cov.total_bars <= 0:
            status = "not_ready"
            reason = "no_bars_found"
        elif feat_cov.total_bars <= 0:
            status = "needs_sync"
            reason = "features_missing_or_outdated"
            action = "sync_features_active"
        else:
            util_floor = float(min_utilization or self.min_utilization)
            if bars_cov.utilization < util_floor:
                status = "needs_window_adjust"
                reason = "low_utilization"
                suggested_end = end_d
                suggested_start = max(start_d, end_d - timedelta(days=self.clamp_days_1))

        return DataReadinessResult(
            status=status,
            reason=reason,
            bars_coverage=bars_cov,
            features_coverage=feat_cov,
            labels_coverage=labels_cov,
            suggested_start=suggested_start,
            suggested_end=suggested_end,
            action=action,
            metadata={"symbol": symbol, "features_table": table},
        )
