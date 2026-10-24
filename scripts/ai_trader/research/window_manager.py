from __future__ import annotations

"""
Adaptive window helper for training/diagnosis.
Clamps requested window to available data coverage based on readiness results.
"""

from datetime import datetime, timedelta, timezone
from typing import Tuple, Optional, Dict, Any


class AdaptiveWindowManager:
    def __init__(self, clamp_30d: int = 30, clamp_45d: int = 45, min_utilization: float = 0.3):
        self.clamp_30d = clamp_30d
        self.clamp_45d = clamp_45d
        self.min_utilization = min_utilization

    def adjust(self, requested_start: str, requested_end: str, coverage: Dict[str, Any] | None) -> Tuple[str, str, str]:
        """
        Decide whether to shrink the window based on utilization. coverage can be the readiness_probe coverage.
        Returns (start_iso, end_iso, reason).
        """
        utilization = None
        if coverage:
            rows = 0
            exp_rows = 0
            try:
                for v in coverage.values():
                    if not v:
                        continue
                    rows += int(v.get("rows", 0) or 0)
                # rough expected rows fallback
                start_dt = datetime.fromisoformat(requested_start.replace(" ", "T"))
                end_dt = datetime.fromisoformat(requested_end.replace(" ", "T"))
                days = max(1, (end_dt - start_dt).days + 1)
                exp_rows = days * 390  # one symbol; upstream can divide/average
                utilization = rows / exp_rows if exp_rows else None
            except Exception:
                utilization = None

        if utilization is not None and utilization < self.min_utilization:
            anchor = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            new_end = anchor.isoformat()
            new_start = (anchor - timedelta(days=self.clamp_30d)).isoformat()
            return new_start, new_end, "window_shrunk_to_30d"
        return requested_start, requested_end, "window_ok"
