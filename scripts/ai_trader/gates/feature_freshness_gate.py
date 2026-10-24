from __future__ import annotations

"""
Feature Freshness Gate

Checks recency of per-symbol features in ClickHouse and classifies into
  - ok:   within stale threshold
  - stale: between stale and dead thresholds
  - dead: missing or beyond dead threshold

Config (policies.gates.features_freshness):
  enabled: true
  table: "trading.features_1m"
  symbols_to_check: ["AAPL", "TSLA", ...]
  threshold_stale_sec: 600
  threshold_dead_sec: 3600

Best-effort: if ClickHouse is unavailable, returns ok=True with empty lists.
"""

import time
from typing import Any, Dict, List, Optional

from libs.logger import get_logger

try:
    from libs.db.clickhouse import ClickHouseDB
except Exception:  # pragma: no cover
    ClickHouseDB = None  # type: ignore


class FeatureFreshnessGate:
    def __init__(self, config: Dict[str, Any]):
        self.config = config or {}
        self.log = get_logger("ai_trader.feature_freshness_gate")
        self.table = str(self.config.get("table", "trading.features_1m"))
        self.th_stale = int(self.config.get("threshold_stale_sec", 600) or 600)
        self.th_dead = int(self.config.get("threshold_dead_sec", 3600) or 3600)
        try:
            self.client = ClickHouseDB() if ClickHouseDB is not None else None
        except Exception:
            self.client = None

    def _latest_ts(self, symbol: str) -> Optional[float]:
        if self.client is None:
            return None

        # Use FINAL for ReplacingMergeTree tables (e.g., sfs.sfs_5s)
        # For other tables, FINAL is harmless but may have slight overhead
        final_clause = "FINAL" if "sfs" in self.table.lower() else ""

        sql = f"""
        SELECT max(toUnixTimestamp(ts)) AS max_ts
        FROM {self.table} {final_clause}
        WHERE symbol = {{s:String}} AND toDate(ts)=today()
        """
        try:
            rows = self.client.query(sql, {"s": symbol})  # type: ignore
            if not rows:
                return None
            v = rows[0].get("max_ts")
            return float(v) if v is not None else None
        except Exception as e:
            self.log.warning("ff_query_failed", symbol=symbol, table=self.table, err=str(e))
            return None

    def check(self, symbols: List[str]) -> Dict[str, Any]:
        now = time.time()
        res: Dict[str, Any] = {"ok": True, "ok_symbols": [], "stale": {}, "dead": {}}
        if not symbols:
            return res
        if self.client is None:
            self.log.warning("ClickHouse client unavailable; skipping feature freshness")
            return res
        for s in symbols:
            latest = self._latest_ts(s)
            if latest is None:
                res["ok"] = False
                res["dead"][s] = {"age_sec": None}
                continue
            age = now - latest
            if age <= self.th_stale:
                res["ok_symbols"].append(s)
            elif age <= self.th_dead:
                res["ok"] = False
                res["stale"][s] = {"age_sec": int(age)}
            else:
                res["ok"] = False
                res["dead"][s] = {"age_sec": int(age)}
        if not res["ok"]:
            self.log.warning("feature_freshness_not_ok", stale=len(res["stale"]), dead=len(res["dead"]))
        else:
            self.log.info("feature_freshness_ok", checked=len(symbols))
        return res

