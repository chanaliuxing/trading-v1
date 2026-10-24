from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple

from libs.logger import get_logger


logger = get_logger(__name__)


@dataclass(frozen=True)
class IBRequestDecision:
    allowed: bool
    reason: str = "allowed"
    cooldown_remaining_sec: float = 0.0


class IBRequestGovernor:
    """Central policy for expensive IB market-data API requests.

    The reconnect gatekeeper serializes connection attempts. This governor handles
    the separate problem: business modules that already hold a valid shared IB
    object can still overload Gateway with qualify/secdef/market-data requests.
    """

    def __init__(
        self,
        *,
        market_health_snapshot: Callable[[], Dict[str, object]],
        recovery_quiet_active: Callable[[], bool],
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        self._market_health_snapshot = market_health_snapshot
        self._recovery_quiet_active = recovery_quiet_active
        self._now_fn = now_fn
        self._request_circuit_sec = float(os.getenv("IB_REQUEST_GOVERNOR_CIRCUIT_SEC", "300"))
        self._request_error_threshold = max(1, int(os.getenv("IB_REQUEST_GOVERNOR_ERROR_THRESHOLD", "3")))
        self._warn_cooldown_sec = float(os.getenv("IB_REQUEST_GOVERNOR_WARN_COOLDOWN_SEC", "60"))
        self._circuits: Dict[Tuple[str, str], Tuple[float, str]] = {}
        self._error_streaks: Dict[Tuple[str, str], int] = {}
        self._last_log_ts: Dict[Tuple[str, str, str], float] = {}

    def allow(
        self,
        *,
        kind: str,
        key: str,
        requester: str,
        diagnostic: bool = False,
        allow_during_revalidation: bool = False,
    ) -> IBRequestDecision:
        kind_s = str(kind or "unknown").strip() or "unknown"
        key_s = str(key or "*").strip() or "*"
        requester_s = str(requester or "unknown").strip() or "unknown"
        now_ts = float(self._now_fn())
        circuit_key = (kind_s, key_s)

        circuit = self._circuits.get(circuit_key)
        if circuit:
            until_ts, reason = circuit
            if until_ts > now_ts:
                remaining = until_ts - now_ts
                self._log_block(
                    requester=requester_s,
                    kind=kind_s,
                    key=key_s,
                    reason=f"circuit_open:{reason}",
                    remaining=remaining,
                    now_ts=now_ts,
                )
                return IBRequestDecision(False, f"circuit_open:{reason}", remaining)
            self._circuits.pop(circuit_key, None)
            self._error_streaks.pop(circuit_key, None)

        if self._recovery_quiet_active() and not diagnostic:
            self._log_block(
                requester=requester_s,
                kind=kind_s,
                key=key_s,
                reason="recovery_quiet_period",
                remaining=0.0,
                now_ts=now_ts,
            )
            return IBRequestDecision(False, "recovery_quiet_period", 0.0)

        snap = self._market_health_snapshot()
        market_blocked = bool(snap.get("degraded")) or bool(snap.get("revalidate_required"))
        if market_blocked and not (diagnostic or allow_during_revalidation):
            reason = "market_revalidation_required" if snap.get("revalidate_required") else "market_degraded"
            self._log_block(
                requester=requester_s,
                kind=kind_s,
                key=key_s,
                reason=reason,
                remaining=0.0,
                now_ts=now_ts,
            )
            return IBRequestDecision(False, reason, 0.0)

        return IBRequestDecision(True)

    def record_result(
        self,
        *,
        kind: str,
        key: str,
        ok: bool,
        error_code: Optional[int] = None,
        reason: Optional[str] = None,
    ) -> None:
        kind_s = str(kind or "unknown").strip() or "unknown"
        key_s = str(key or "*").strip() or "*"
        circuit_key = (kind_s, key_s)
        if ok:
            self._error_streaks.pop(circuit_key, None)
            return

        count = int(self._error_streaks.get(circuit_key) or 0) + 1
        self._error_streaks[circuit_key] = count
        if count < self._request_error_threshold:
            return

        reason_s = str(reason or (f"error_{error_code}" if error_code is not None else "request_failed"))
        until_ts = float(self._now_fn()) + max(1.0, self._request_circuit_sec)
        self._circuits[circuit_key] = (until_ts, reason_s)
        logger.warning(
            "ib_request_governor_circuit_opened",
            kind=kind_s,
            key=key_s,
            reason=reason_s,
            error_streak=count,
            cooldown_sec=self._request_circuit_sec,
        )

    def _log_block(
        self,
        *,
        requester: str,
        kind: str,
        key: str,
        reason: str,
        remaining: float,
        now_ts: float,
    ) -> None:
        log_key = (requester, kind, key)
        last_ts = float(self._last_log_ts.get(log_key) or 0.0)
        if now_ts - last_ts < self._warn_cooldown_sec:
            return
        self._last_log_ts[log_key] = now_ts
        logger.warning(
            "ib_request_governor_blocked",
            requester=requester,
            kind=kind,
            key=key,
            reason=reason,
            cooldown_remaining_sec=round(float(remaining), 2),
        )
