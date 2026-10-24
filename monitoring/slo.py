# monitoring/slo.py
from __future__ import annotations
from collections import deque, defaultdict
from dataclasses import dataclass, field
from typing import Deque, Dict, Any, Optional, Tuple
import time
import math

from obs.audit import log_event


@dataclass
class SLOThresholds:
    max_data_lag_ms: int = 5_000          # Data latency threshold (5s)
    max_order_error_rate: float = 0.02    # Order failure rate (2%)
    max_shadow_real_diff_bps: float = 5.0 # Shadow vs real execution diff
    window_sec: int = 300                 # 5-minute rolling window


@dataclass
class SLOState:
    latencies: Deque[Tuple[int,int]] = field(default_factory=deque)  # (ts, lag_ms)
    order_attempts: Deque[int] = field(default_factory=deque)        # ts
    order_failures: Deque[int] = field(default_factory=deque)        # ts
    slippages_bps: Deque[Tuple[int,float]] = field(default_factory=deque)   # (ts, bps)
    shadow_real_bps: Deque[Tuple[int,float]] = field(default_factory=deque) # (ts, bps)
    last_flush_ts: int = 0


class SLOMonitor:
    """
    Service Level Objective monitoring for production trading.

    Tracks:
    - Data feed latency
    - Order execution error rate
    - Slippage from reference prices
    - Shadow vs real execution differences
    """
    def __init__(self, thresholds: SLOThresholds, alert_cb=None, ch_client=None, run_id: str = "run"):
        self.t = thresholds
        self.s = SLOState()
        self.alert = alert_cb or (lambda title, text: None)
        self.ch = ch_client
        self.run_id = run_id

    def _now_ms(self) -> int:
        return int(time.time()*1000)

    def _trim(self, q: Deque, now_ms: int):
        """Remove entries outside the time window."""
        cutoff = now_ms - self.t.window_sec*1000
        while q and (q[0][0] if isinstance(q[0], tuple) else q[0]) < cutoff:
            q.popleft()

    # ---- Record metrics ----
    def record_data_point_latency(self, market_ts_ms: int, recv_ts_ms: Optional[int] = None):
        """Record latency between market timestamp and receipt time."""
        now = recv_ts_ms or self._now_ms()
        lag = max(0, now - market_ts_ms)
        self.s.latencies.append((now, lag))
        self._trim(self.s.latencies, now)

    def record_order_attempt(self):
        """Record an order submission attempt."""
        now = self._now_ms()
        self.s.order_attempts.append(now)
        self._trim(self.s.order_attempts, now)

    def record_order_failure(self):
        """Record an order failure (reject, timeout, etc)."""
        now = self._now_ms()
        self.s.order_failures.append(now)
        self._trim(self.s.order_failures, now)

    def record_slippage_bps(self, ref_px: float, fill_px: float, side: str):
        """Record slippage in basis points."""
        if ref_px <= 0 or fill_px <= 0:
            return
        # Buy: fill>=ref is negative slippage; Sell: fill<=ref is negative
        bps = abs((fill_px - ref_px)/ref_px)*1e4
        now = self._now_ms()
        self.s.slippages_bps.append((now, bps))
        self._trim(self.s.slippages_bps, now)

    def record_shadow_real_diff_bps(self, shadow_fill: float, real_fill: float):
        """Record difference between shadow and real execution."""
        if shadow_fill <= 0 or real_fill <= 0:
            return
        bps = abs((real_fill - shadow_fill)/shadow_fill)*1e4
        now = self._now_ms()
        self.s.shadow_real_bps.append((now, bps))
        self._trim(self.s.shadow_real_bps, now)

    # ---- Compute & alert ----
    def compute_window_metrics(self) -> Dict[str, float]:
        """Compute average metrics over the rolling window."""
        now = self._now_ms()

        def avg(q: Deque[Tuple[int,float]]) -> float:
            if not q:
                return 0.0
            return float(sum(v for _, v in q)/len(q))

        lag_avg = avg(self.s.latencies)
        err_rate = (len(self.s.order_failures)/max(1,len(self.s.order_attempts)))
        slip_avg = avg(self.s.slippages_bps)
        diff_avg = avg(self.s.shadow_real_bps)

        return {
            "data_lag_ms_avg": lag_avg,
            "order_error_rate": err_rate,
            "slippage_bps_avg": slip_avg,
            "shadow_real_diff_bps_avg": diff_avg,
            "window_sec": self.t.window_sec
        }

    def evaluate_and_alert(self):
        """Evaluate SLOs and trigger alerts if breached."""
        m = self.compute_window_metrics()
        alerts = []

        if m["data_lag_ms_avg"] > self.t.max_data_lag_ms:
            alerts.append(f"DataLag {m['data_lag_ms_avg']:.0f}ms > {self.t.max_data_lag_ms}ms")
        if m["order_error_rate"] > self.t.max_order_error_rate:
            alerts.append(f"OrderError {m['order_error_rate']:.3f} > {self.t.max_order_error_rate}")
        if m["shadow_real_diff_bps_avg"] > self.t.max_shadow_real_diff_bps:
            alerts.append(f"ShadowRealDiff {m['shadow_real_diff_bps_avg']:.1f}bps > {self.t.max_shadow_real_diff_bps}bps")

        for msg in alerts:
            self.alert("SLO Breach", f"run={self.run_id} {msg}")
            try:
                metric, value = msg.split(" ", 1)
            except ValueError:
                metric, value = "metric", msg
            log_event(
                "slo_breach",
                component="SLO",
                run_id=self.run_id,
                metric=metric,
                detail=value,
                window_sec=self.t.window_sec,
            )

    # ---- ClickHouse export ----
    def flush_to_clickhouse(self):
        """Flush metrics to ClickHouse for persistence and visualization."""
        if not self.ch:
            return

        m = self.compute_window_metrics()
        row = [[self.run_id, int(self._now_ms()),
                float(m["data_lag_ms_avg"]), float(m["order_error_rate"]),
                float(m["slippage_bps_avg"]), float(m["shadow_real_diff_bps_avg"]),
                int(m["window_sec"])]]

        try:
            self.ch.insert("live_metrics", row, column_names=[
                "run_id","ts_ms","data_lag_ms_avg","order_error_rate",
                "slippage_bps_avg","shadow_real_diff_bps_avg","window_sec"
            ])
        except Exception as e:
            pass  # Don't fail on metrics recording errors
