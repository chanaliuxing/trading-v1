"""Scarcity controller: expand watchlist instead of lowering thresholds."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

from .params_store import ParamSet


@dataclass
class ScarcityDecision:
    watchlist: List[str]
    lane2_weight_boost: float
    wall_dist_floor: float


class ScarcityController:
    def __init__(self, log_path: str):
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def record_signal(self, ts_iso: str) -> None:
        try:
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts": ts_iso}) + "\n")
        except Exception:
            return

    def signals_last_10d(self) -> int:
        if not self.log_path.exists():
            return 0
        cutoff = time.time() - 10 * 86400
        count = 0
        try:
            for line in self.log_path.read_text("utf-8").splitlines():
                try:
                    row = json.loads(line)
                    ts = row.get("ts")
                    if not ts:
                        continue
                    if _iso_to_epoch(ts) >= cutoff:
                        count += 1
                except Exception:
                    continue
        except Exception:
            return 0
        return count

    def decide(self, params: ParamSet, state: str) -> ScarcityDecision:
        count = self.signals_last_10d()
        if count >= params.scarcity_target_signals_10d:
            return ScarcityDecision(
                watchlist=params.watchlist_base,
                lane2_weight_boost=0.0,
                wall_dist_floor=params.wall_dist_hi,
            )

        watchlist = params.watchlist_extended or params.watchlist_base
        wall_floor = params.wall_dist_hi
        lane2_boost = params.lane2_weight_boost

        if state in ("TREND_OK", "CAUTION"):
            wall_floor = max(params.wall_dist_lo, wall_floor)

        return ScarcityDecision(
            watchlist=watchlist,
            lane2_weight_boost=lane2_boost,
            wall_dist_floor=wall_floor,
        )


def _iso_to_epoch(ts_iso: str) -> float:
    try:
        return time.mktime(time.strptime(ts_iso[:19], "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        return 0.0
