from __future__ import annotations

import asyncio
import contextlib
import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable, Dict, Optional, Tuple

from zoneinfo import ZoneInfo

from ib_insync import IB

from libs.config import config
from libs.logger import get_logger
from services.metrics.exporter import inc_ibkr_disconnect
from services.metrics.exporter import inc_ibkr_probe_failure
from services.metrics.exporter import mark_ibkr_probe_ok
from services.metrics.exporter import set_ibkr_connection_up
from services.exec.adapters.ib_adapter import IBInsyncAdapter
from services.ibkr.client_id_registry import get_client_id_registry
from services.ibkr.ib_gateway_circuit import get_ib_gateway_connection_circuit
from services.ibkr.ib_gateway_recovery_coordinator import get_ib_gateway_recovery_coordinator
from services.ibkr.ib_request_governor import IBRequestDecision, IBRequestGovernor

logger = get_logger(__name__)
ET = ZoneInfo("America/New_York")


def _format_ib_exception(exc: BaseException) -> str:
    text = str(exc).strip()
    if text:
        return f"{type(exc).__name__}: {text}"
    return f"{type(exc).__name__}: {exc!r}"


@dataclass(frozen=True)
class IBHandle:
    role: str
    client_id: int
    epoch: int
    ib: IB


def _session_phase_et_static(now_ts: Optional[float] = None) -> str:
    ts = datetime.fromtimestamp(now_ts or time.time(), ET).astimezone(ET)
    t = ts.time()
    if 4 <= t.hour < 9 or (t.hour == 9 and t.minute < 30):
        return "PREMARKET"
    if 9 < t.hour < 16 or (t.hour == 9 and t.minute >= 30):
        return "RTH"
    if 16 <= t.hour < 20:
        return "AFTERHOURS"
    return "OFFHOURS"


class IBReconnectGatekeeper:
    """Serializes and rate-limits all IB reconnection attempts.

    When TWS/Gateway restarts, multiple components (exec adapter, market adapter,
    market data service) detect the disconnect independently and race to reconnect.
    This gatekeeper ensures only one component connects at a time, prevents attempts
    when the port is not yet listening, and applies shared exponential backoff.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        # Global single-flight slot for the real IB API handshake attempt.
        # Acquire callers hold this slot until report_success/report_failure.
        self._attempt_slot = asyncio.Lock()
        self._attempt_owner: Optional[str] = None
        self._attempt_started_ts: float = 0.0
        self._attempt_stale_sec: float = float(os.getenv("IB_RECONNECT_ATTEMPT_STALE_SEC", "90"))
        self._last_attempt_ts: float = 0.0
        self._last_success_ts: float = 0.0
        self._consecutive_failures: int = 0
        self._readiness_failures: int = 0
        self._attempt_timestamps: list[float] = []
        self._max_attempts_per_window: int = int(os.getenv("IB_RECONNECT_MAX_ATTEMPTS_PER_WINDOW", "3"))
        self._window_sec: float = float(os.getenv("IB_RECONNECT_WINDOW_SEC", "10"))

    async def tcp_probe(self, host: str, port: int, timeout: float = 2.0) -> bool:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=timeout,
            )
            writer.close()
            await writer.wait_closed()
            return True
        except (OSError, asyncio.TimeoutError, ConnectionRefusedError):
            return False

    @staticmethod
    def _compute_backoff_for_failures(failures: int) -> float:
        phase = _session_phase_et_static()
        f = max(0, failures)
        if phase == "RTH":
            return min(15.0, 2.0 * (2 ** min(f, 3)))
        elif phase == "PREMARKET":
            return min(30.0, 3.0 * (2 ** min(f, 3)))
        else:
            return min(120.0, 5.0 * (2 ** min(f, 4)))

    def _compute_backoff(self) -> float:
        return self._compute_backoff_for_failures(
            max(self._consecutive_failures, self._readiness_failures)
        )

    def current_backoff_sec(self) -> float:
        return self._compute_backoff()

    def gateway_circuit_snapshot(self) -> Dict[str, object]:
        return get_ib_gateway_connection_circuit().snapshot().as_dict()

    def gateway_circuit_retry_after_sec(self) -> float:
        return float(get_ib_gateway_connection_circuit().snapshot().retry_after_sec)

    def _gateway_circuit_allows(self, *, caller: str, host: str, port: int) -> bool:
        circuit = get_ib_gateway_connection_circuit()
        state = circuit.check_allowed()
        if not state.open:
            return True
        logger.warning(
            "ib_gateway_connection_circuit_open",
            caller=caller,
            host=host,
            port=port,
            reason=state.reason,
            source=state.source,
            retry_after_sec=round(state.retry_after_sec, 2),
            consecutive_failures=state.consecutive_failures,
            state_path=state.path,
        )
        return False

    def _record_gateway_circuit_failure(self, *, caller: str, host: str, port: int, error: str) -> None:
        if not get_ib_gateway_connection_circuit().is_gateway_failure(error):
            return
        state = get_ib_gateway_connection_circuit().record_failure(
            reason=str(error or "ib_gateway_connection_failure"),
            source=caller,
            metadata={"host": host, "port": int(port)},
        )
        logger.warning(
            "ib_gateway_connection_circuit_armed",
            caller=caller,
            host=host,
            port=port,
            reason=state.reason,
            retry_after_sec=round(state.retry_after_sec, 2),
            consecutive_failures=state.consecutive_failures,
            state_path=state.path,
        )

    def _record_gateway_circuit_success(self, *, caller: str) -> None:
        state = get_ib_gateway_connection_circuit().snapshot()
        if not state.open and state.consecutive_failures <= 0:
            return
        cleared = get_ib_gateway_connection_circuit().record_success(source=caller)
        logger.info(
            "ib_gateway_connection_circuit_closed",
            caller=caller,
            state_path=cleared.path,
        )

    @staticmethod
    def _is_port_unready_error(error: str) -> bool:
        err_l = str(error or "").lower()
        return any(
            tok in err_l
            for tok in (
                "connectionrefusederror",
                "connection refused",
                "connect call failed",
                "errno 61",
                "api port",
            )
        )

    def _check_rate_limit(self) -> bool:
        now = time.time()
        self._attempt_timestamps = [
            ts for ts in self._attempt_timestamps
            if now - ts < self._window_sec
        ]
        if len(self._attempt_timestamps) >= self._max_attempts_per_window:
            return False
        self._attempt_timestamps.append(now)
        return True

    async def probe_ready(self, *, caller: str, host: str, port: int) -> bool:
        """Serialize readiness probes without consuming reconnect attempt budget."""
        if not self._gateway_circuit_allows(caller=f"{caller}:probe_ready", host=host, port=port):
            return False
        async with self._lock:
            ready = await self.tcp_probe(host, port)
            if ready:
                previous_failures = self._readiness_failures
                self._readiness_failures = 0
                if previous_failures > 0:
                    logger.info(
                        "reconnect_gatekeeper_probe_reset",
                        caller=caller,
                        previous_failures=previous_failures,
                    )
            else:
                self._readiness_failures += 1
                logger.info(
                    "reconnect_gatekeeper_probe_not_ready",
                    caller=caller,
                    host=host,
                    port=port,
                    readiness_failures=self._readiness_failures,
                    next_backoff_sec=round(self.current_backoff_sec(), 1),
                )
            if ready:
                logger.info(
                    "reconnect_gatekeeper_probe_ready",
                    caller=caller,
                    host=host,
                    port=port,
                )
            return ready

    async def acquire(self, *, caller: str, host: str, port: int) -> bool:
        """Reserve reconnect budget and a global single-flight connect slot.

        This path intentionally does not issue a raw TCP probe before the real
        IB API handshake. In practice IB Gateway can expose the socket before the
        API handshake path is healthy, and an extra pre-connect open/close here
        adds an additional transient socket just before the real client tries to
        negotiate the API session. `probe_ready()` remains available for passive
        readiness loops that want to wait for the port without spending attempt
        budget.

        Important: on successful return, caller now owns the global connect slot
        and must call report_success/report_failure to release it.
        """
        # Compute shared backoff snapshot first; do not occupy global slot while sleeping.
        if not self._gateway_circuit_allows(caller=caller, host=host, port=port):
            return False
        now = time.time()
        backoff = self._compute_backoff()
        elapsed = now - self._last_attempt_ts
        if self._last_attempt_ts > 0 and elapsed < backoff:
            if self._readiness_failures > 0:
                try:
                    ready = await self.tcp_probe(host, port, timeout=0.5)
                except Exception:
                    ready = False
                if ready:
                    self.clear_failure_backoff(caller=caller, reason="ib_port_ready_before_backoff_sleep")
                    backoff = self._compute_backoff()
                    elapsed = now - self._last_attempt_ts
                    if elapsed >= backoff:
                        backoff = 0.0
            wait = backoff - elapsed
            if wait > 0:
                logger.info(
                    "reconnect_gatekeeper_backoff",
                    caller=caller,
                    wait_sec=round(wait, 1),
                    consecutive_failures=self._consecutive_failures,
                    readiness_failures=self._readiness_failures,
                    phase=_session_phase_et_static(),
                )
                await asyncio.sleep(wait)

        async with self._lock:
            # Reclaim ghost/stale slot before deciding denial.
            if self._attempt_slot.locked():
                active_age = (
                    max(0.0, time.time() - self._attempt_started_ts)
                    if self._attempt_started_ts > 0
                    else 0.0
                )
                if self._attempt_started_ts <= 0 or active_age >= self._attempt_stale_sec:
                    logger.warning(
                        "reconnect_gatekeeper_stale_attempt_slot_forced_release",
                        caller=caller,
                        active_owner=self._attempt_owner,
                        active_age_sec=round(active_age, 3),
                        stale_sec=self._attempt_stale_sec,
                    )
                    self._release_attempt_slot(caller=caller, force=True)
                else:
                    logger.info(
                        "reconnect_gatekeeper_wait_active_attempt",
                        caller=caller,
                        active_owner=self._attempt_owner,
                        active_age_sec=round(active_age, 3),
                    )
                    return False

            if not self._check_rate_limit():
                logger.warning(
                    "reconnect_gatekeeper_rate_limited",
                    caller=caller,
                    window_sec=self._window_sec,
                    max_attempts=self._max_attempts_per_window,
                )
                return False

            self._last_attempt_ts = time.time()
            await self._attempt_slot.acquire()
            self._attempt_owner = caller
            self._attempt_started_ts = time.time()
            logger.info(
                "reconnect_gatekeeper_proceed",
                caller=caller,
                host=host,
                port=port,
                consecutive_failures=self._consecutive_failures,
                readiness_failures=self._readiness_failures,
            )
            return True

    def _release_attempt_slot(self, *, caller: str, force: bool = False) -> None:
        if not self._attempt_slot.locked():
            return
        previous_owner = self._attempt_owner
        if previous_owner is not None and previous_owner != caller and not force:
            logger.warning(
                "reconnect_gatekeeper_slot_release_ignored_owner_mismatch",
                caller=caller,
                previous_owner=previous_owner,
            )
            return
        held_for_sec = (
            max(0.0, time.time() - self._attempt_started_ts)
            if self._attempt_started_ts > 0
            else None
        )
        self._attempt_owner = None
        self._attempt_started_ts = 0.0
        self._attempt_slot.release()
        logger.info(
            "reconnect_gatekeeper_slot_released",
            caller=caller,
            previous_owner=previous_owner,
            held_for_sec=(round(held_for_sec, 3) if held_for_sec is not None else None),
        )
        if previous_owner is not None and previous_owner != caller:
            logger.warning(
                "reconnect_gatekeeper_slot_owner_mismatch",
                caller=caller,
                previous_owner=previous_owner,
            )

    def report_success(self, *, caller: str) -> None:
        self._release_attempt_slot(caller=caller)
        self._record_gateway_circuit_success(caller=caller)
        prev_failures = self._consecutive_failures
        prev_readiness_failures = self._readiness_failures
        self._consecutive_failures = 0
        self._readiness_failures = 0
        self._last_success_ts = time.time()
        self._attempt_timestamps.clear()
        if prev_failures > 0 or prev_readiness_failures > 0:
            logger.info(
                "reconnect_gatekeeper_reset",
                caller=caller,
                previous_failures=prev_failures,
                previous_readiness_failures=prev_readiness_failures,
            )

    def clear_failure_backoff(self, *, caller: str, reason: str) -> None:
        previous_failures = self._consecutive_failures
        previous_readiness_failures = self._readiness_failures
        self._consecutive_failures = 0
        self._readiness_failures = 0
        self._attempt_timestamps.clear()
        logger.info(
            "reconnect_gatekeeper_failure_backoff_cleared",
            caller=caller,
            reason=str(reason or "")[:200],
            previous_failures=previous_failures,
            previous_readiness_failures=previous_readiness_failures,
        )
        self._record_gateway_circuit_success(caller=caller)

    def report_failure(self, *, caller: str, error: str, host: Optional[str] = None, port: Optional[int] = None) -> None:
        self._release_attempt_slot(caller=caller)
        h, p = _default_ib_host_port()
        self._record_gateway_circuit_failure(
            caller=caller,
            host=host or h,
            port=int(port or p),
            error=error,
        )
        err_l = str(error or "").lower()
        if self._is_port_unready_error(error):
            self._readiness_failures += 1
            logger.warning(
                "reconnect_gatekeeper_port_not_ready_failure",
                caller=caller,
                error=error[:200],
                consecutive_failures=self._consecutive_failures,
                readiness_failures=self._readiness_failures,
                next_backoff_sec=round(self._compute_backoff(), 1),
            )
            return
        severe_tokens = (
            "apistart",
            "timeouterror",
            "timed out",
            "timeout",
            "1100",
        )
        severe = any(tok in err_l for tok in severe_tokens)
        self._consecutive_failures += 2 if severe else 1
        self._readiness_failures = 0
        logger.warning(
            "reconnect_gatekeeper_failure",
            caller=caller,
            error=error[:200],
            consecutive_failures=self._consecutive_failures,
            severe_backoff=bool(severe),
            next_backoff_sec=round(self._compute_backoff(), 1),
        )

    def report_aborted(self, *, caller: str, reason: str) -> None:
        self._release_attempt_slot(caller=caller)
        logger.warning(
            "reconnect_gatekeeper_aborted",
            caller=caller,
            reason=reason[:200],
            consecutive_failures=self._consecutive_failures,
            readiness_failures=self._readiness_failures,
        )


def _default_ib_host_port() -> tuple[str, int]:
    ib_cfg = config.ib_config
    return ib_cfg.get("host", "127.0.0.1"), int(ib_cfg.get("port", 4002))


def _base_client_id() -> int:
    return int(config.ib_config.get("client_id", 7))


def _default_exec_client_id() -> int:
    raw = os.getenv("IB_EXEC_CLIENT_ID", "").strip()
    if raw:
        try:
            return int(raw)
        except Exception:
            logger.warning("ib_exec_client_id_env_invalid", value=raw)
    return int(get_client_id_registry().resolve("exec"))


def _default_market_client_id() -> int:
    raw = os.getenv("IB_MARKET_CLIENT_ID", "").strip()
    if raw:
        try:
            return int(raw)
        except Exception:
            logger.warning("ib_market_client_id_env_invalid", value=raw)
    return int(get_client_id_registry().resolve("market"))


def _default_master_client_id() -> int:
    raw = os.getenv("IB_MASTER_CLIENT_ID", "").strip()
    if raw:
        try:
            return int(raw)
        except Exception:
            logger.warning("ib_master_client_id_env_invalid", value=raw)
    return int(get_client_id_registry().resolve("master"))


def _market_data_type_for_now(now: Optional[datetime] = None) -> int:
    """Choose IB market data type based on ET session.

    Defaults:
    - RTH: live (1)
    - non-RTH: delayed-frozen (4)

    Both values can be overridden via env for controlled experiments.
    """
    ts = (now or datetime.now(ET)).astimezone(ET)
    is_rth = 9 <= ts.hour < 16 and not (ts.hour == 9 and ts.minute < 30)
    env_key = "IB_MARKET_DATA_TYPE_RTH" if is_rth else "IB_MARKET_DATA_TYPE_OFFHOURS"
    default_value = "1" if is_rth else "4"
    try:
        return int(os.getenv(env_key, default_value))
    except Exception:
        return int(default_value)


def _market_data_type_mode(market_data_type: int) -> str:
    return {
        1: "live",
        2: "frozen",
        3: "delayed",
        4: "delayed-frozen",
    }.get(int(market_data_type), f"type-{market_data_type}")


def _try_set_delayed_frozen_market_data(ib: IB) -> None:
    """Best-effort: keep shared market session on the intended market data type.

    Shared market sessions are reused by stock L1 subscriptions, option chain scans,
    and underlying price lookups. When we switched to a shared IB session, the old
    adapter-local market-data-type call could be skipped. Calling this more than
    once is harmless and keeps the shared session aligned with the current ET phase.
    """
    market_data_type = _market_data_type_for_now()
    try:
        ib.reqMarketDataType(market_data_type)
        logger.info(
            "ib_market_data_type_set",
            market_data_type=market_data_type,
            mode=_market_data_type_mode(market_data_type),
        )
    except Exception as exc:
        logger.warning("ib_market_data_type_set_failed", market_data_type=market_data_type, error=str(exc))


async def _connect_ib_with_retry(
    ib: IB,
    host: str,
    port: int,
    client_id: int,
    *,
    ib_factory: Optional[Callable[[], IB]] = None,
    timeout_client_id_fallbacks: int = 0,
    max_attempts: int = 5,
    connect_timeout_sec: float = 30.0,
) -> tuple[IB, int]:
    """Connect an IB instance with collision-aware retries.

    On connect/probe timeout, ib_insync can leave the client in a poisoned half-open
    state that keeps failing until the process is restarted. Rebuilding the IB object
    between timeout retries avoids carrying that bad state forward.
    """
    max_attempts = max(1, int(max_attempts))
    connect_timeout_sec = max(1.0, float(connect_timeout_sec))
    attempt = 1
    current_id = int(client_id)
    current_ib = ib
    connect_flow_started_at = time.time()
    timeout_client_id_switches = 0

    async def _disconnect_partial_session(reason: str) -> None:
        connected_before = bool(current_ib.isConnected())
        try:
            current_ib.disconnect()
            logger.warning(
                "ib_partial_session_disconnected",
                host=host,
                port=port,
                client_id=current_id,
                reason=reason,
                connected_before=connected_before,
            )
        except Exception as disconnect_exc:
            logger.warning(
                "ib_partial_session_disconnect_failed",
                host=host,
                port=port,
                client_id=current_id,
                reason=reason,
                error=str(disconnect_exc),
            )

    api_probe_timeout_sec = float(os.getenv("IB_API_READY_TIMEOUT_SEC", "10"))
    api_probe_attempts = max(1, int(os.getenv("IB_API_READY_ATTEMPTS", "3")))

    async def _probe_api_ready() -> None:
        # reqCurrentTimeAsync confirms the API handshake finished; on busy
        # gateways the first probe can lag even after connectAsync succeeds.
        last_exc: Exception | None = None
        for probe_attempt in range(1, api_probe_attempts + 1):
            try:
                probe_started_at = time.time()
                logger.info(
                    "ib_api_ready_probe_begin",
                    host=host,
                    port=port,
                    client_id=current_id,
                    probe_attempt=probe_attempt,
                    max_probe_attempts=api_probe_attempts,
                    timeout_sec=api_probe_timeout_sec,
                )
                await asyncio.wait_for(current_ib.reqCurrentTimeAsync(), timeout=api_probe_timeout_sec)
                logger.info(
                    "ib_api_ready_probe_ok",
                    host=host,
                    port=port,
                    client_id=current_id,
                    probe_attempt=probe_attempt,
                    latency_ms=round((time.time() - probe_started_at) * 1000.0, 1),
                )
                return
            except Exception as exc:
                last_exc = exc
                if probe_attempt >= api_probe_attempts:
                    logger.warning(
                        "ib_api_ready_probe_failed_final",
                        host=host,
                        port=port,
                        client_id=current_id,
                        probe_attempt=probe_attempt,
                        max_probe_attempts=api_probe_attempts,
                        timeout_sec=api_probe_timeout_sec,
                        error=_format_ib_exception(exc),
                    )
                    break
                logger.warning(
                    "ib_api_ready_probe_retry",
                    host=host,
                    port=port,
                    client_id=current_id,
                    attempt=probe_attempt,
                    max_attempts=api_probe_attempts,
                    timeout_sec=api_probe_timeout_sec,
                    error=_format_ib_exception(exc),
                )
                await asyncio.sleep(min(1.5 * probe_attempt, 5.0))
        assert last_exc is not None
        raise last_exc

    while True:
        try:
            # Timeout is caller-controlled: market startup uses a shorter timeout
            # to avoid wedging IB Gateway with long repeated handshakes.
            connect_started_at = time.time()
            logger.info(
                "ib_connect_attempt_begin",
                host=host,
                port=port,
                client_id=current_id,
                attempt=attempt,
                max_attempts=max_attempts,
                ib_id=id(current_ib),
                elapsed_since_flow_start_sec=round(connect_started_at - connect_flow_started_at, 3),
            )
            await current_ib.connectAsync(host, port, clientId=current_id, timeout=connect_timeout_sec)
            logger.info(
                "ib_connect_async_ok",
                host=host,
                port=port,
                client_id=current_id,
                attempt=attempt,
                max_attempts=max_attempts,
                ib_id=id(current_ib),
                latency_ms=round((time.time() - connect_started_at) * 1000.0, 1),
                connected=bool(current_ib.isConnected()),
            )
            await _probe_api_ready()
            logger.info(
                "ib_connect_flow_ok",
                host=host,
                port=port,
                client_id=current_id,
                attempt=attempt,
                max_attempts=max_attempts,
                total_latency_ms=round((time.time() - connect_flow_started_at) * 1000.0, 1),
                ib_id=id(current_ib),
            )
            return current_ib, current_id
        except asyncio.CancelledError:
            logger.warning(
                "ib_connect_attempt_cancelled",
                host=host,
                port=port,
                client_id=current_id,
                attempt=attempt,
                max_attempts=max_attempts,
                ib_id=id(current_ib),
                total_elapsed_ms=round((time.time() - connect_flow_started_at) * 1000.0, 1),
            )
            await _disconnect_partial_session("connect_cancelled")
            raise
        except Exception as exc:
            exc_text = _format_ib_exception(exc)
            msg = exc_text.lower()
            # Handle both "Client ID in use" and "Timeout" as retryable conditions
            is_in_use = "client id is already in use" in msg or "already in use" in msg
            is_timeout = isinstance(exc, (TimeoutError, asyncio.TimeoutError))
            logger.warning(
                "ib_connect_attempt_failed",
                host=host,
                port=port,
                client_id=current_id,
                attempt=attempt,
                max_attempts=max_attempts,
                ib_id=id(current_ib),
                is_timeout=is_timeout,
                is_in_use=is_in_use,
                connected=bool(current_ib.isConnected()),
                error=exc_text,
                total_elapsed_ms=round((time.time() - connect_flow_started_at) * 1000.0, 1),
            )

            await _disconnect_partial_session("connect_or_probe_failed")
            
            if (is_in_use or is_timeout) and attempt < max_attempts:
                if is_timeout:
                    logger.warning(
                        "ib_connect_timeout_retry",
                        attempt=attempt,
                        max_attempts=max_attempts,
                        timeout=connect_timeout_sec,
                    )
                    if timeout_client_id_switches < max(0, int(timeout_client_id_fallbacks)):
                        old_client_id = current_id
                        current_id = int(current_id) + 1
                        timeout_client_id_switches += 1
                        logger.warning(
                            "ib_connect_timeout_client_id_fallback",
                            old_client_id=old_client_id,
                            new_client_id=current_id,
                            switches=timeout_client_id_switches,
                            max_switches=max(0, int(timeout_client_id_fallbacks)),
                            attempt=attempt,
                            max_attempts=max_attempts,
                        )
                    if ib_factory is not None:
                        old_ib = current_ib
                        current_ib = ib_factory()
                        logger.warning(
                            "ib_connect_timeout_recreate_client",
                            attempt=attempt,
                            max_attempts=max_attempts,
                            old_ib_id=id(old_ib),
                            new_ib_id=id(current_ib),
                        )
                else:
                    logger.warning(
                        "ib_market_client_id_in_use_retry_same_id",
                        client_id=current_id,
                        attempt=attempt,
                        max_attempts=max_attempts,
                        error=exc_text,
                    )
                attempt += 1
                await asyncio.sleep(1.0 * attempt)
                continue

            raise


class IBConnectionManager:
    """Centralized owner for IB sessions (master + market + exec)."""

    def __init__(self) -> None:
        self._gatekeeper = IBReconnectGatekeeper()
        self._market_ib: Optional[IB] = None
        self._market_lock = asyncio.Lock()
        self._market_client_id: Optional[int] = None
        self._market_events_bound = False
        self._market_bound_ib: Optional[IB] = None
        self._market_error_handler = None
        self._market_disconnected_handler = None
        self._market_last_error_code: Optional[int] = None
        self._market_last_error_msg: str = ""
        self._market_last_error_ts: float = 0.0
        self._market_degraded = False
        self._market_revalidate_required = False
        self._market_revalidate_reason: Optional[str] = None
        self._market_last_revalidate_ok_ts: float = 0.0
        self._market_req_error_codes: Dict[int, Tuple[int, float]] = {}
        self._market_noise_minute: Optional[str] = None
        self._market_noise_counts: Dict[str, int] = {}
        self._market_disconnect_count: int = 0
        self._market_last_disconnect_ts: float = 0.0
        self._market_last_disconnect_reason: Optional[str] = None
        self._market_last_disconnect_source: Optional[str] = None
        self._market_last_connectivity_error_code: Optional[int] = None
        self._market_last_connectivity_error_msg: str = ""
        self._market_last_connectivity_error_ts: float = 0.0
        self._market_last_probe_ok_ts: float = 0.0
        self._market_last_probe_latency_ms: Optional[float] = None
        self._market_probe_task: Optional[asyncio.Task] = None
        self._market_connect_timeout_streak: int = 0
        self._market_last_connect_error: Optional[str] = None
        self._market_last_connect_error_ts: float = 0.0
        self._market_last_connect_error_client_id: Optional[int] = None
        self._market_connection_epoch: int = 0
        self._market_probe_interval_sec: float = float(os.getenv("IB_MARKET_PROBE_INTERVAL_SEC", "30"))
        self._market_probe_interval_offhours_sec: float = float(
            os.getenv("IB_MARKET_PROBE_INTERVAL_OFFHOURS_SEC", "120")
        )
        self._market_probe_timeout_sec: float = float(os.getenv("IB_MARKET_PROBE_TIMEOUT_SEC", "5"))
        self._offhours_connectivity_log_cooldown_sec: float = float(
            os.getenv("IB_OFFHOURS_CONNECTIVITY_LOG_COOLDOWN_SEC", "300")
        )
        self._offhours_probe_fail_log_cooldown_sec: float = float(
            os.getenv("IB_OFFHOURS_PROBE_FAIL_LOG_COOLDOWN_SEC", "300")
        )
        self._last_offhours_session_state_log_ts: float = 0.0
        self._last_offhours_disconnected_log_ts: float = 0.0
        self._last_offhours_probe_fail_log_ts: float = 0.0
        self._request_governor = IBRequestGovernor(
            market_health_snapshot=self.get_market_health_snapshot,
            recovery_quiet_active=lambda: self._recovery_short_circuit_active() or self.runtime_recovery_active(),
        )

        self._master_ib: Optional[IB] = None
        self._master_lock = asyncio.Lock()
        self._master_client_id: Optional[int] = None
        self._master_connection_epoch: int = 0
        self._master_events_bound = False
        self._master_bound_ib: Optional[IB] = None
        self._master_disconnected_handler = None
        self._master_truth_complete: bool = False
        self._master_truth_last_snapshot_ts: float = 0.0
        self._master_truth_open_orders_count: int = 0

        self._exec_adapter: Optional[IBInsyncAdapter] = None
        self._exec_lock = asyncio.Lock()
        self._exec_connection_epoch: int = 0
        self._exec_last_connect_generation: int = 0
        self._exec_last_disconnect_generation: int = 0

        self._runtime_recovery_task: Optional[asyncio.Task] = None
        self._runtime_recovery_event: Optional[asyncio.Event] = None
        self._runtime_recovery_active: bool = False
        self._runtime_recovery_last_reason: str = ""
        self._runtime_recovery_last_source: str = ""
        self._runtime_recovery_pending_health_event: Optional[Dict[str, object]] = None
        self._recovery_short_circuit_until_ts: float = 0.0
        self._recovery_short_circuit_reason: str = ""
        self._recovery_short_circuit_source: str = ""
        self._recovery_short_circuit_rth_sec: float = float(
            os.getenv("IB_RECOVERY_SHORT_CIRCUIT_RTH_SEC", "8")
        )
        self._recovery_short_circuit_offhours_sec: float = float(
            os.getenv("IB_RECOVERY_SHORT_CIRCUIT_OFFHOURS_SEC", "20")
        )
        self._wait_short_circuit_log_cooldown_sec: float = float(
            os.getenv("IB_WAIT_SHORT_CIRCUIT_LOG_COOLDOWN_SEC", "10")
        )
        self._last_wait_short_circuit_log_ts: float = 0.0
        self._api_readiness_probe_lock = asyncio.Lock()
        self._api_readiness_probe_client_id: int = int(os.getenv("IB_API_READINESS_PROBE_CLIENT_ID", "97"))
        self._api_readiness_probe_timeout_sec: float = float(
            os.getenv("IB_API_READINESS_PROBE_TIMEOUT_SEC", "5")
        )
        self._api_readiness_probe_ok_ttl_sec: float = float(
            os.getenv("IB_API_READINESS_PROBE_OK_TTL_SEC", "15")
        )
        self._pre_connect_api_probe_enabled: bool = os.getenv(
            "IB_PRE_CONNECT_API_PROBE_ENABLED",
            "0",
        ).strip().lower() in {"1", "true", "yes", "on"}
        self._operator_recovery_proof_ok_ttl_sec: float = float(
            os.getenv("IB_OPERATOR_RECOVERY_PROOF_OK_TTL_SEC", "120")
        )
        self._api_readiness_probe_failure_cooldown_sec: float = float(
            os.getenv("IB_API_READINESS_PROBE_FAILURE_COOLDOWN_SEC", "60")
        )
        self._api_readiness_probe_client_id_fallbacks: int = int(
            os.getenv("IB_API_READINESS_PROBE_CLIENT_ID_FALLBACKS", "3")
        )
        self._api_readiness_probe_last_ok_ts: float = 0.0
        self._api_readiness_probe_next_allowed_ts: float = 0.0
        self._api_readiness_probe_failure_streak: int = 0
        self._api_readiness_probe_first_failure_ts: float = 0.0
        self._api_readiness_probe_unhealthy_threshold: int = int(
            os.getenv("IB_API_READINESS_UNHEALTHY_THRESHOLD", "5")
        )
        self._api_readiness_probe_unhealthy_min_duration_sec: float = float(
            os.getenv("IB_API_READINESS_UNHEALTHY_MIN_DURATION_SEC", "600")
        )
        self._api_readiness_probe_unhealthy_emit_cooldown_sec: float = float(
            os.getenv("IB_API_READINESS_UNHEALTHY_EMIT_COOLDOWN_SEC", "300")
        )
        self._api_readiness_probe_last_unhealthy_emit_ts: float = 0.0
        self._gateway_circuit_recovery_probe_sleep_sec: float = float(
            os.getenv("IB_GATEWAY_CIRCUIT_RECOVERY_PROBE_SLEEP_SEC", "2")
        )
        self._health_event_listeners: Dict[str, Callable[[Dict[str, object]], Awaitable[None] | None]] = {}

    @property
    def reconnect_gatekeeper(self) -> IBReconnectGatekeeper:
        return self._gatekeeper

    def register_health_event_listener(
        self,
        listener_id: str,
        callback: Callable[[Dict[str, object]], Awaitable[None] | None],
    ) -> None:
        self._health_event_listeners[str(listener_id)] = callback

    def unregister_health_event_listener(self, listener_id: str) -> None:
        self._health_event_listeners.pop(str(listener_id), None)

    async def _emit_health_event(self, payload: Dict[str, object]) -> None:
        listeners = list(self._health_event_listeners.items())
        for listener_id, callback in listeners:
            try:
                result = callback(dict(payload))
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                logger.warning(
                    "ib_health_event_listener_failed",
                    listener_id=listener_id,
                    event_type=payload.get("event_type"),
                    error=str(exc),
                )

    def _emit_health_event_background(self, payload: Dict[str, object]) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning(
                "ib_health_event_emit_skipped_no_loop",
                event_type=payload.get("event_type"),
                reason=payload.get("reason"),
            )
            return
        loop.create_task(self._emit_health_event(dict(payload)))

    def market_connected(self) -> bool:
        return bool(self._market_ib and self._market_ib.isConnected())

    def exec_connected(self) -> bool:
        return bool(self._exec_adapter and self._exec_adapter.ib and self._exec_adapter.ib.isConnected())

    def master_connected(self) -> bool:
        return bool(self._master_ib and self._master_ib.isConnected())

    def master_truth_complete(self) -> bool:
        return bool(self._master_truth_complete)

    def exec_connection_epoch(self) -> int:
        return int(self._exec_connection_epoch)

    def market_connection_epoch(self) -> int:
        return int(self._market_connection_epoch)

    def master_connection_epoch(self) -> int:
        return int(self._master_connection_epoch)

    def trading_enabled(self, *, symbol_has_unresolved_quarantine: bool) -> bool:
        return bool(
            self.exec_connected()
            and self.market_connected()
            and self.master_truth_complete()
            and (not bool(symbol_has_unresolved_quarantine))
        )

    def runtime_recovery_active(self) -> bool:
        return bool(self._runtime_recovery_active)

    def _recovery_short_circuit_remaining_sec(self, *, now_ts: Optional[float] = None) -> float:
        now = float(now_ts or time.time())
        return max(0.0, float(self._recovery_short_circuit_until_ts) - now)

    def _recovery_short_circuit_active(self, *, now_ts: Optional[float] = None) -> bool:
        return self._recovery_short_circuit_remaining_sec(now_ts=now_ts) > 0.0

    def _arm_recovery_short_circuit(
        self,
        *,
        reason: str,
        source: str,
        min_delay_sec: Optional[float] = None,
    ) -> None:
        phase = self._session_phase_et()
        base_delay = (
            float(self._recovery_short_circuit_rth_sec)
            if phase == "RTH"
            else float(self._recovery_short_circuit_offhours_sec)
        )
        delay_sec = max(1.0, float(min_delay_sec) if min_delay_sec is not None else base_delay)
        now = time.time()
        new_until = now + delay_sec
        if new_until <= float(self._recovery_short_circuit_until_ts):
            return
        previous_until = float(self._recovery_short_circuit_until_ts)
        self._recovery_short_circuit_until_ts = new_until
        self._recovery_short_circuit_reason = str(reason or "")[:240]
        self._recovery_short_circuit_source = str(source or "")[:120]
        logger.warning(
            "ib_recovery_short_circuit_armed",
            source=source,
            reason=str(reason or "")[:240],
            phase=phase,
            delay_sec=round(delay_sec, 2),
            previous_remaining_sec=round(max(0.0, previous_until - now), 2),
            remaining_sec=round(max(0.0, new_until - now), 2),
        )

    def _clear_recovery_short_circuit(self, *, reason: str, source: str) -> None:
        previous_remaining = self._recovery_short_circuit_remaining_sec()
        self._recovery_short_circuit_until_ts = 0.0
        self._recovery_short_circuit_reason = ""
        self._recovery_short_circuit_source = ""
        logger.info(
            "ib_recovery_short_circuit_cleared",
            source=source,
            reason=str(reason or "")[:240],
            previous_remaining_sec=round(previous_remaining, 2),
        )

    @staticmethod
    def _is_upstream_outage_error(exc: Exception) -> bool:
        msg = _format_ib_exception(exc).lower()
        tokens = (
            "connectionrefusederror",
            "connect call failed",
            "api connection failed",
            "apistart",
            "timeouterror",
            "timed out",
            "timeout",
            "connect_or_probe_failed",
            "1100",
        )
        return any(tok in msg for tok in tokens)

    @staticmethod
    def _is_port_unready_error_text(text: str) -> bool:
        return IBReconnectGatekeeper._is_port_unready_error(text)

    @staticmethod
    def _is_api_handshake_unready_error_text(text: str) -> bool:
        msg = str(text or "").lower()
        tokens = (
            "apistart",
            "timeouterror",
            "timed out",
            "timeout",
            "api ready",
            "api handshake",
        )
        return any(tok in msg for tok in tokens)

    async def _probe_api_handshake_ready(self, *, caller: str, host: str, port: int) -> bool:
        """Disposable IB API readiness probe used only during recovery short-circuit.

        A raw TCP listener is not enough after IB Gateway restarts or freezes: the
        socket can be visible while the API handshake still times out. This probe
        confirms both connectAsync and a lightweight reqCurrentTime round trip
        before wait_until_ready releases the real market/exec/master sessions.
        """
        async with self._api_readiness_probe_lock:
            now = time.time()
            cooldown_remaining = max(0.0, float(self._api_readiness_probe_next_allowed_ts) - now)
            if cooldown_remaining > 0.0:
                logger.info(
                    "ib_api_handshake_probe_skipped_cooldown",
                    caller=caller,
                    host=host,
                    port=port,
                    cooldown_remaining_sec=round(cooldown_remaining, 2),
                )
                return False
            recovery_decision = get_ib_gateway_recovery_coordinator().probe_decision(
                caller=caller,
                host=host,
                port=port,
            )
            if not recovery_decision.allowed:
                retry_after = max(
                    1.0,
                    float(recovery_decision.retry_after_sec or 0.0),
                    float(self._api_readiness_probe_failure_cooldown_sec),
                )
                self._api_readiness_probe_next_allowed_ts = time.time() + retry_after
                logger.warning(
                    "ib_api_handshake_probe_blocked_by_recovery_coordinator",
                    caller=caller,
                    host=host,
                    port=port,
                    reason=recovery_decision.reason,
                    source=recovery_decision.source,
                    retry_after_sec=round(retry_after, 2),
                    circuit_open=bool(recovery_decision.circuit_open),
                    manual_required=bool(recovery_decision.manual_required),
                )
                return False
            base_client_id = int(self._api_readiness_probe_client_id)
            fallback_count = max(0, int(getattr(self, "_api_readiness_probe_client_id_fallbacks", 0)))
            timeout_sec = max(1.0, float(self._api_readiness_probe_timeout_sec))
            started_at = time.time()
            last_error_text = ""
            for offset in range(fallback_count + 1):
                probe_ib = IB()
                client_id = base_client_id + offset
                try:
                    await probe_ib.connectAsync(host, port, clientId=client_id, timeout=timeout_sec)
                    await asyncio.wait_for(probe_ib.reqCurrentTimeAsync(), timeout=timeout_sec)
                    self._api_readiness_probe_last_ok_ts = time.time()
                    self._api_readiness_probe_next_allowed_ts = 0.0
                    self._api_readiness_probe_failure_streak = 0
                    self._api_readiness_probe_first_failure_ts = 0.0
                    get_ib_gateway_recovery_coordinator().record_probe_success(
                        source=f"{caller}:api_handshake_probe"
                    )
                    logger.info(
                        "ib_api_handshake_probe_ready",
                        caller=caller,
                        host=host,
                        port=port,
                        client_id=client_id,
                        base_client_id=base_client_id,
                        latency_ms=round((time.time() - started_at) * 1000.0, 1),
                    )
                    return True
                except Exception as exc:
                    error_text = _format_ib_exception(exc)
                    last_error_text = error_text
                    in_use = "client id is already in use" in error_text.lower() or "already in use" in error_text.lower()
                    if in_use and offset < fallback_count:
                        logger.warning(
                            "ib_api_handshake_probe_client_id_in_use_retry",
                            caller=caller,
                            host=host,
                            port=port,
                            client_id=client_id,
                            next_client_id=client_id + 1,
                            error=error_text,
                        )
                        continue
                    break
                finally:
                    with contextlib.suppress(Exception):
                        probe_ib.disconnect()

            now = time.time()
            if self._api_readiness_probe_failure_streak <= 0:
                self._api_readiness_probe_first_failure_ts = now
            self._api_readiness_probe_failure_streak += 1
            self._api_readiness_probe_next_allowed_ts = time.time() + max(
                1.0,
                float(self._api_readiness_probe_failure_cooldown_sec),
            )
            logger.warning(
                "ib_api_handshake_probe_not_ready",
                caller=caller,
                host=host,
                port=port,
                client_id=base_client_id,
                timeout_sec=timeout_sec,
                error=last_error_text,
                latency_ms=round((time.time() - started_at) * 1000.0, 1),
            )
            get_ib_gateway_recovery_coordinator().record_probe_failure(
                reason=last_error_text or "ib_api_handshake_not_ready",
                source=caller,
                host=host,
                port=port,
                metadata={"probe": "api_handshake"},
            )
            self._maybe_emit_gateway_api_unhealthy(
                caller=caller,
                host=host,
                port=port,
                error=last_error_text,
            )
            return False

    def _maybe_emit_gateway_api_unhealthy(
        self,
        *,
        caller: str,
        host: str,
        port: int,
        error: str,
    ) -> None:
        if self._is_port_unready_error_text(error):
            logger.warning(
                "ib_gateway_api_unhealthy_deferred_port_unready",
                caller=caller,
                host=host,
                port=port,
                error=str(error or "")[:240],
                failure_streak=int(self._api_readiness_probe_failure_streak),
            )
            return
        threshold = max(1, int(self._api_readiness_probe_unhealthy_threshold))
        if int(self._api_readiness_probe_failure_streak) < threshold:
            return
        now = time.time()
        min_duration = max(0.0, float(self._api_readiness_probe_unhealthy_min_duration_sec))
        first_failure_ts = float(self._api_readiness_probe_first_failure_ts or now)
        duration_sec = max(0.0, now - first_failure_ts)
        if duration_sec < min_duration:
            logger.warning(
                "ib_gateway_api_unhealthy_deferred",
                caller=caller,
                host=host,
                port=port,
                failure_streak=int(self._api_readiness_probe_failure_streak),
                threshold=threshold,
                duration_sec=round(duration_sec, 2),
                min_duration_sec=round(min_duration, 2),
            )
            return
        cooldown = max(1.0, float(self._api_readiness_probe_unhealthy_emit_cooldown_sec))
        if (
            self._api_readiness_probe_last_unhealthy_emit_ts > 0.0
            and (now - self._api_readiness_probe_last_unhealthy_emit_ts) < cooldown
        ):
            return
        self._api_readiness_probe_last_unhealthy_emit_ts = now
        payload = {
            "event_type": "ib_gateway_api_unhealthy",
            "reason": "gateway_api_half_open",
            "source": str(caller or "ib_api_readiness_probe"),
            "detail": str(error or "TimeoutError: ib_api_handshake_not_ready")[:240],
            "action": "manual_gateway_api_recovery_required",
            "host": host,
            "port": int(port),
            "failure_streak": int(self._api_readiness_probe_failure_streak),
            "threshold": threshold,
            "duration_sec": round(duration_sec, 2),
            "min_duration_sec": round(min_duration, 2),
        }
        logger.error("ib_gateway_api_unhealthy", **payload)
        get_ib_gateway_recovery_coordinator().mark_manual_required(
            reason="manual_gateway_api_recovery_required",
            source=str(caller or "ib_api_readiness_probe"),
            host=host,
            port=port,
            metadata={
                "gateway_reason": "gateway_api_half_open",
                "failure_streak": int(self._api_readiness_probe_failure_streak),
                "duration_sec": round(duration_sec, 2),
                "error": str(error or "")[:240],
            },
        )
        self._emit_health_event_background(payload)

    def _api_readiness_probe_recently_ok(self) -> bool:
        return (
            self._api_readiness_probe_last_ok_ts > 0.0
            and (time.time() - self._api_readiness_probe_last_ok_ts)
            <= max(1.0, float(self._api_readiness_probe_ok_ttl_sec))
        )

    def _operator_recovery_proof_recently_ok(self) -> bool:
        try:
            return get_ib_gateway_connection_circuit().recent_recovery_proof_succeeded(
                max_age_sec=max(1.0, float(self._operator_recovery_proof_ok_ttl_sec))
            )
        except Exception as exc:
            logger.warning("ib_operator_recovery_proof_recent_check_failed", error=str(exc))
            return False

    async def _ensure_api_ready_before_real_connect(
        self,
        *,
        caller: str,
        host: str,
        port: int,
    ) -> None:
        if not self._pre_connect_api_probe_enabled:
            logger.info(
                "ib_api_pre_connect_probe_disabled",
                caller=caller,
                host=host,
                port=port,
            )
            return
        if self._api_readiness_probe_recently_ok():
            return
        if self._operator_recovery_proof_recently_ok():
            self._api_readiness_probe_last_ok_ts = time.time()
            logger.info(
                "ib_api_pre_connect_probe_skipped_recent_operator_proof",
                caller=caller,
                host=host,
                port=port,
                ttl_sec=round(max(1.0, float(self._operator_recovery_proof_ok_ttl_sec)), 2),
            )
            return
        api_ready = await self._probe_api_handshake_ready(
            caller=f"{caller}:pre_connect_api_probe",
            host=host,
            port=port,
        )
        if api_ready:
            get_ib_gateway_recovery_coordinator().record_probe_success(source=f"{caller}:pre_connect_api_probe")
            return
        reason = "TimeoutError: ib_api_handshake_not_ready"
        self._arm_recovery_short_circuit(
            reason=reason,
            source=f"{caller}:pre_connect_api_probe",
            min_delay_sec=max(
                float(self._api_readiness_probe_failure_cooldown_sec),
                float(self._recovery_short_circuit_rth_sec),
                float(self._recovery_short_circuit_offhours_sec),
            ),
        )
        self._gatekeeper.report_aborted(caller=caller, reason=reason)
        raise TimeoutError("ib_api_handshake_not_ready")

    def _clear_stale_pre_connect_probe_manual_circuit_if_applicable(
        self,
        *,
        caller: str,
        host: str,
        port: int,
        circuit_state: object,
    ) -> bool:
        if not (
            bool(getattr(circuit_state, "manual_required", False))
            and not self._pre_connect_api_probe_enabled
            and ":pre_connect_api_probe" in str(getattr(circuit_state, "source", "") or "")
        ):
            return False
        cleared = get_ib_gateway_connection_circuit().record_success(
            source=f"{caller}:clear_stale_pre_connect_probe_manual_circuit"
        )
        logger.warning(
            "ib_gateway_stale_pre_connect_probe_manual_circuit_cleared",
            caller=caller,
            host=host,
            port=port,
            previous_reason=getattr(circuit_state, "reason", ""),
            previous_source=getattr(circuit_state, "source", ""),
            state_path=cleared.path,
        )
        return True

    async def _ensure_recovery_short_circuit_allows_connect(
        self,
        *,
        caller: str,
        host: str,
        port: int,
    ) -> None:
        remaining = self._recovery_short_circuit_remaining_sec()
        circuit_state = get_ib_gateway_connection_circuit().check_allowed()
        if circuit_state.open:
            if self._clear_stale_pre_connect_probe_manual_circuit_if_applicable(
                caller=caller,
                host=host,
                port=port,
                circuit_state=circuit_state,
            ):
                circuit_state = get_ib_gateway_connection_circuit().check_allowed()
                if not circuit_state.open:
                    return
            logger.warning(
                "ib_gateway_connection_circuit_connect_blocked",
                caller=caller,
                host=host,
                port=port,
                reason=circuit_state.reason,
                source=circuit_state.source,
                retry_after_sec=round(circuit_state.retry_after_sec, 2),
                consecutive_failures=circuit_state.consecutive_failures,
            )
            raise ConnectionError(f"ib_gateway_connection_circuit_open:{circuit_state.reason}")
        if remaining <= 0.0:
            return

        reason = self._recovery_short_circuit_reason
        logger.info(
            "ib_recovery_short_circuit_connect_blocked",
            caller=caller,
            host=host,
            port=port,
            remaining_sec=round(remaining, 2),
            reason=reason,
        )
        raise ConnectionError(f"ib_recovery_short_circuit_active:{reason}")

    def _ensure_runtime_recovery_task(self) -> None:
        task = self._runtime_recovery_task
        if task is not None and not task.done():
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._runtime_recovery_event = asyncio.Event()
        self._runtime_recovery_task = asyncio.create_task(
            self._runtime_recovery_loop(),
            name="ib_runtime_recovery_loop",
        )
        logger.info("ib_runtime_recovery_loop_started")

    def _trigger_runtime_recovery(
        self,
        *,
        reason: str,
        source: str,
        health_event: Optional[Dict[str, object]] = None,
    ) -> None:
        self._runtime_recovery_last_reason = str(reason or "unknown")
        self._runtime_recovery_last_source = str(source or "unknown")
        if health_event is not None:
            self._runtime_recovery_pending_health_event = dict(health_event)
        reason_l = self._runtime_recovery_last_reason.lower()
        if (
            "1100" in reason_l
            or "ib_connectivity_lost" in reason_l
            or "tcp_timeout" in reason_l
            or "connectionrefused" in reason_l
            or "apistart" in reason_l
            or "timeouterror" in reason_l
            or "timed out" in reason_l
            or "timeout" in reason_l
        ):
            self._arm_recovery_short_circuit(
                reason=self._runtime_recovery_last_reason,
                source=self._runtime_recovery_last_source,
            )
        self._ensure_runtime_recovery_task()
        if self._runtime_recovery_event is not None:
            self._runtime_recovery_event.set()
        logger.warning(
            "ib_runtime_recovery_triggered",
            reason=self._runtime_recovery_last_reason,
            source=self._runtime_recovery_last_source,
            market_ready=self.market_connected(),
            exec_ready=self.exec_connected(),
            master_ready=self.master_connected(),
            master_truth_complete=self.master_truth_complete(),
        )

    async def _runtime_recovery_loop(self) -> None:
        event = self._runtime_recovery_event
        if event is None:
            return
        while True:
            try:
                await event.wait()
                event.clear()
                self._runtime_recovery_active = True
                reason = self._runtime_recovery_last_reason
                source = self._runtime_recovery_last_source
                pending_health_event = self._runtime_recovery_pending_health_event
                self._runtime_recovery_pending_health_event = None
                logger.info(
                    "ib_runtime_recovery_cycle_begin",
                    reason=reason,
                    source=source,
                )
                if pending_health_event is not None:
                    await self._emit_health_event(pending_health_event)
                attempts = 0
                while True:
                    attempts += 1
                    market_ready, exec_ready = await self.wait_until_ready(
                        require_exec=True,
                        require_master=True,
                        timeout_sec=float(os.getenv("IB_RUNTIME_RECOVERY_GATE_SEC", "35")),
                        initiator="manager_runtime_recovery",
                    )
                    master_ready = self.master_connected()
                    master_truth_ready = self.master_truth_complete()
                    fully_ready = bool(market_ready and exec_ready and master_ready and master_truth_ready)
                    if fully_ready:
                        logger.info(
                            "ib_runtime_recovery_cycle_complete",
                            reason=reason,
                            source=source,
                            attempts=attempts,
                            market_ready=market_ready,
                            exec_ready=exec_ready,
                            master_ready=master_ready,
                            master_truth_complete=master_truth_ready,
                        )
                        await self._emit_health_event(
                            {
                                "event_type": "broker_api_ready",
                                "reason": "ib_runtime_recovery_api_ready",
                                "source": "ib_connection_manager.runtime_recovery",
                                "detail": f"reason={reason};source={source};attempts={attempts}",
                                "resume_ready": False,
                                "broker_connected": True,
                                "broker_truth_complete": False,
                                "order_truth_clean": False,
                                "position_ledger_clean": False,
                                "strategies_paused_ack": True,
                                "market_ready": market_ready,
                                "exec_ready": exec_ready,
                                "master_ready": master_ready,
                                "master_truth_complete": master_truth_ready,
                                "requires_runtime_reconciler": True,
                            }
                        )
                        break
                    logger.warning(
                        "ib_runtime_recovery_cycle_retry",
                        reason=reason,
                        source=source,
                        attempts=attempts,
                        market_ready=market_ready,
                        exec_ready=exec_ready,
                        master_ready=master_ready,
                        master_truth_complete=master_truth_ready,
                    )
                    await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.error("ib_runtime_recovery_cycle_failed", error=str(exc), exc_info=True)
                await asyncio.sleep(1.0)
            finally:
                self._runtime_recovery_active = False

    @staticmethod
    def _session_phase_et(now_ts: Optional[float] = None) -> str:
        return _session_phase_et_static(now_ts)

    def _is_rth_et(self, *, now_ts: Optional[float] = None) -> bool:
        return self._session_phase_et(now_ts=now_ts) == "RTH"

    def _should_emit_offhours_session_state_log(
        self,
        *,
        now_ts: Optional[float] = None,
        error_code: Optional[int] = None,
    ) -> bool:
        now = float(now_ts or time.time())
        if self._is_rth_et(now_ts=now):
            return True
        # Keep recovery visible even during off-hours storms.
        if int(error_code or 0) == 1102:
            return True
        if (now - self._last_offhours_session_state_log_ts) < self._offhours_connectivity_log_cooldown_sec:
            return False
        self._last_offhours_session_state_log_ts = now
        return True

    def _should_emit_offhours_disconnected_log(self, *, now_ts: Optional[float] = None) -> bool:
        now = float(now_ts or time.time())
        if self._is_rth_et(now_ts=now):
            return True
        if (now - self._last_offhours_disconnected_log_ts) < self._offhours_connectivity_log_cooldown_sec:
            return False
        self._last_offhours_disconnected_log_ts = now
        return True

    def _should_emit_offhours_probe_failure_log(self, *, now_ts: Optional[float] = None) -> bool:
        now = float(now_ts or time.time())
        if self._is_rth_et(now_ts=now):
            return True
        if (now - self._last_offhours_probe_fail_log_ts) < self._offhours_probe_fail_log_cooldown_sec:
            return False
        self._last_offhours_probe_fail_log_ts = now
        return True

    def _market_probe_sleep_interval_sec(self, *, now_ts: Optional[float] = None) -> float:
        base = float(self._market_probe_interval_sec)
        if self._is_rth_et(now_ts=now_ts):
            return base
        return max(base, float(self._market_probe_interval_offhours_sec))

    @staticmethod
    def _is_retryable_connect_exc(exc: Exception) -> tuple[bool, bool]:
        msg = str(exc or "").lower()
        is_timeout = isinstance(exc, (TimeoutError, asyncio.TimeoutError)) or "timeout" in msg
        is_in_use = "client id is already in use" in msg or "already in use" in msg
        return is_timeout, is_in_use

    def _advance_market_client_id_after_failure(
        self,
        *,
        client_id: int,
        exc: Exception,
        host: str,
        port: int,
        started_at: float,
    ) -> None:
        is_timeout, is_in_use = self._is_retryable_connect_exc(exc)
        self._market_last_connect_error = str(exc)
        self._market_last_connect_error_ts = time.time()
        self._market_last_connect_error_client_id = int(client_id)
        if is_timeout:
            self._market_connect_timeout_streak += 1
            logger.warning(
                "ib_market_connect_timeout_keep_client_id",
                host=host,
                port=port,
                client_id=int(client_id),
                timeout_streak=self._market_connect_timeout_streak,
                latency_ms=round((time.time() - started_at) * 1000.0, 1),
                error=str(exc),
            )
        elif is_in_use:
            self._market_connect_timeout_streak = 0
            logger.warning(
                "ib_market_connect_conflict_keep_client_id",
                host=host,
                port=port,
                client_id=int(client_id),
                latency_ms=round((time.time() - started_at) * 1000.0, 1),
                error=str(exc),
            )
        else:
            self._market_connect_timeout_streak = 0

    @staticmethod
    def _classify_disconnect_reason(
        *,
        error_code: Optional[int],
        error_msg: str,
        error_age_sec: Optional[float],
    ) -> tuple[str, str]:
        code = int(error_code) if error_code is not None else None
        msg = str(error_msg or "").lower()
        age = float(error_age_sec) if error_age_sec is not None else None

        if code == 1100:
            return "ib_connectivity_lost_1100", "ib_gateway_or_network"
        if code == 1300:
            return "ib_socket_port_reset_1300", "ib_gateway_reset"
        if code == 326:
            return "ib_client_id_conflict_326", "session_conflict"
        if "connection reset by peer" in msg or "peer closed connection" in msg:
            return "tcp_peer_closed", "network"
        if "timed out" in msg or "timeout" in msg:
            return "tcp_timeout", "network"
        if age is not None and age > 60:
            return "stale_or_unknown_error_context", "unknown"
        return "unknown_disconnect", "unknown"

    def _flush_market_noise_logs(self, now_ts: Optional[float] = None) -> None:
        current_minute = datetime.fromtimestamp(now_ts or time.time(), ET).strftime("%Y-%m-%d %H:%M")
        if self._market_noise_minute and self._market_noise_minute != current_minute and self._market_noise_counts:
            logger.info(
                "ib_market_option_noise_summary",
                minute=self._market_noise_minute,
                counts=dict(self._market_noise_counts),
            )
            self._market_noise_counts = {}
        self._market_noise_minute = current_minute

    def _record_market_noise(self, *, error_code: int) -> None:
        self._flush_market_noise_logs()
        key = str(int(error_code))
        self._market_noise_counts[key] = int(self._market_noise_counts.get(key, 0)) + 1

    def _remember_market_req_error(self, req_id: int, error_code: int) -> None:
        if req_id < 0:
            return
        self._market_req_error_codes[int(req_id)] = (int(error_code), time.time())
        now_ts = time.time()
        for k, (_code, ts) in list(self._market_req_error_codes.items()):
            if now_ts - ts > 300:
                self._market_req_error_codes.pop(k, None)

    def was_market_req_rejected(self, req_id: Optional[int], *, error_codes: tuple[int, ...] = (354,)) -> bool:
        if req_id is None:
            return False
        data = self._market_req_error_codes.get(int(req_id))
        if not data:
            return False
        code, ts = data
        if time.time() - ts > 300:
            self._market_req_error_codes.pop(int(req_id), None)
            return False
        return int(code) in set(int(x) for x in error_codes)

    def _remember_market_connectivity_error(self, error_code: Optional[int], error_msg: str) -> None:
        try:
            code = int(error_code) if error_code is not None else None
        except Exception:
            code = None
        if code not in {1100, 1300, 326}:
            return
        self._market_last_connectivity_error_code = code
        self._market_last_connectivity_error_msg = str(error_msg or "")
        self._market_last_connectivity_error_ts = time.time()

    def _resolve_market_disconnect_context(self) -> tuple[Optional[int], str, Optional[float], str]:
        now = time.time()
        if self._market_last_connectivity_error_ts:
            age = now - self._market_last_connectivity_error_ts
            if age <= 300:
                return (
                    self._market_last_connectivity_error_code,
                    self._market_last_connectivity_error_msg,
                    age,
                    "recent_connectivity_error",
                )
        error_age_sec = (now - self._market_last_error_ts) if self._market_last_error_ts else None
        return self._market_last_error_code, self._market_last_error_msg, error_age_sec, "last_error_event"

    def market_revalidation_required(self) -> bool:
        return bool(self._market_revalidate_required)

    def mark_market_revalidated(self, *, source: str, ok: bool, detail: Optional[str] = None) -> None:
        if ok:
            self._market_degraded = False
            self._market_revalidate_required = False
            self._market_revalidate_reason = None
            self._market_last_revalidate_ok_ts = time.time()
            logger.info(
                "ib_market_revalidation_ok",
                source=source,
                detail=detail,
            )
            return
        logger.warning(
            "ib_market_revalidation_failed",
            source=source,
            detail=detail,
            degraded=bool(self._market_degraded),
            revalidate_required=bool(self._market_revalidate_required),
        )

    def allow_market_request(
        self,
        *,
        kind: str,
        key: str,
        requester: str,
        diagnostic: bool = False,
        allow_during_revalidation: bool = False,
    ) -> IBRequestDecision:
        return self._request_governor.allow(
            kind=kind,
            key=key,
            requester=requester,
            diagnostic=diagnostic,
            allow_during_revalidation=allow_during_revalidation,
        )

    def record_market_request_result(
        self,
        *,
        kind: str,
        key: str,
        ok: bool,
        error_code: Optional[int] = None,
        reason: Optional[str] = None,
    ) -> None:
        self._request_governor.record_result(
            kind=kind,
            key=key,
            ok=ok,
            error_code=error_code,
            reason=reason,
        )

    def get_market_health_snapshot(self) -> Dict[str, Optional[object]]:
        return {
            "connected": bool(self._market_ib and self._market_ib.isConnected()),
            "degraded": bool(self._market_degraded),
            "revalidate_required": bool(self._market_revalidate_required),
            "revalidate_reason": self._market_revalidate_reason,
            "last_revalidate_ok_ts": self._market_last_revalidate_ok_ts or None,
            "last_error_code": self._market_last_error_code,
            "last_error_msg": self._market_last_error_msg or None,
            "last_error_ts": self._market_last_error_ts or None,
            "disconnect_count": int(self._market_disconnect_count),
            "last_disconnect_ts": self._market_last_disconnect_ts or None,
            "last_disconnect_reason": self._market_last_disconnect_reason,
            "last_disconnect_source": self._market_last_disconnect_source,
            "last_probe_ok_ts": self._market_last_probe_ok_ts or None,
            "last_probe_latency_ms": self._market_last_probe_latency_ms,
        }

    def _handle_market_error_event(self, req_id: int, error_code: int, error_string: str, contract, *, source_ib: Optional[IB] = None) -> None:
        if source_ib is not None and source_ib is not self._market_ib:
            logger.info(
                "ib_market_stale_error_event_ignored",
                req_id=req_id,
                error_code=error_code,
                source_ib_id=id(source_ib),
                active_ib_id=(id(self._market_ib) if self._market_ib is not None else None),
            )
            return
        try:
            self._market_last_error_code = int(error_code)
        except Exception:
            self._market_last_error_code = None
        self._market_last_error_ts = time.time()
        self._market_last_error_msg = str(error_string or "")
        if req_id is not None:
            self._remember_market_req_error(int(req_id), int(error_code))
        self._remember_market_connectivity_error(self._market_last_error_code, self._market_last_error_msg)

        if self._market_last_error_code in {1100, 1101, 1102}:
            self._market_degraded = self._market_last_error_code == 1100
            self._market_revalidate_required = True
            self._market_revalidate_reason = f"IB_{self._market_last_error_code}"
            log_fn = logger.error if self._market_last_error_code == 1100 else logger.warning
            if self._should_emit_offhours_session_state_log(error_code=self._market_last_error_code):
                log_fn(
                    "ib_market_session_state_change",
                    error_code=self._market_last_error_code,
                    error_msg=(self._market_last_error_msg or "")[:300],
                    degraded=bool(self._market_degraded),
                    revalidate_required=True,
                )
            return

        # Benign farm/status chatter from IBKR (especially off-hours) can be noisy
        # and does not represent actionable reconnect failures.
        if self._market_last_error_code in {2104, 2106, 2107, 2108, 2119, 2157, 2158}:
            return
        if self._market_last_error_code == 200 and "No security definition has been found" in self._market_last_error_msg:
            return

        contract_payload = None
        sec_type = None
        if contract is not None:
            try:
                sec_type = getattr(contract, "secType", None)
                contract_payload = {
                    "sec_type": sec_type,
                    "symbol": getattr(contract, "symbol", None),
                    "local_symbol": getattr(contract, "localSymbol", None),
                    "exchange": getattr(contract, "exchange", None),
                    "currency": getattr(contract, "currency", None),
                    "expiry": getattr(contract, "lastTradeDateOrContractMonth", None),
                    "right": getattr(contract, "right", None),
                    "strike": getattr(contract, "strike", None),
                    "multiplier": getattr(contract, "multiplier", None),
                    "trading_class": getattr(contract, "tradingClass", None),
                    "con_id": getattr(contract, "conId", None),
                }
            except Exception:
                contract_payload = None

        if sec_type == "OPT" and self._market_last_error_code in {300, 354}:
            self._record_market_noise(error_code=self._market_last_error_code)
            logger.info(
                "ib_market_option_request_event",
                req_id=req_id,
                error_code=error_code,
                error_msg=(self._market_last_error_msg or "")[:300],
                contract=contract_payload,
            )
            return
        if (
            req_id is not None
            and int(req_id) >= 0
            and contract_payload is None
            and self._market_last_error_code in {300, 354}
        ):
            # Some rejected snapshot/subscription requests surface without contract payload.
            # Treat as request lifecycle noise to avoid false alarms.
            self._record_market_noise(error_code=self._market_last_error_code)
            logger.info(
                "ib_market_request_event_unresolved_contract",
                req_id=req_id,
                error_code=error_code,
                error_msg=(self._market_last_error_msg or "")[:300],
            )
            return
        if (
            req_id is not None
            and int(req_id) >= 0
            and self._market_last_error_code == 300
            and self.was_market_req_rejected(int(req_id), error_codes=(354,))
        ):
            # Follow-up cancel/no-EId after an already rejected market data request.
            self._record_market_noise(error_code=self._market_last_error_code)
            logger.info(
                "ib_market_request_event_cancel_after_reject",
                req_id=req_id,
                error_code=error_code,
                error_msg=(self._market_last_error_msg or "")[:300],
            )
            return
        if (
            sec_type == "IND"
            and str(getattr(contract, "symbol", "")).upper() == "VIX"
            and self._market_last_error_code == 354
        ):
            logger.info(
                "ib_market_vix_subscription_event",
                req_id=req_id,
                error_code=error_code,
                error_msg=(self._market_last_error_msg or "")[:300],
                contract=contract_payload,
            )
            return

        logger.warning(
            "ib_market_error_event",
            req_id=req_id,
            error_code=error_code,
            error_msg=(self._market_last_error_msg or "")[:300],
            contract=contract_payload,
        )

    def _handle_market_disconnected(self, *, source_ib: Optional[IB] = None) -> None:
        if source_ib is not None and source_ib is not self._market_ib:
            logger.warning(
                "ib_market_stale_disconnect_ignored",
                source_ib_id=id(source_ib),
                active_ib_id=(id(self._market_ib) if self._market_ib is not None else None),
                active_client_id=self._market_client_id,
            )
            return
        error_code, error_msg, error_age_sec, disconnect_source = self._resolve_market_disconnect_context()
        reason_code, cause_type = self._classify_disconnect_reason(
            error_code=error_code,
            error_msg=error_msg,
            error_age_sec=error_age_sec,
        )
        self._market_disconnect_count += 1
        self._market_last_disconnect_ts = time.time()
        self._market_last_disconnect_reason = reason_code
        self._market_last_disconnect_source = disconnect_source
        self._market_degraded = True
        self._market_revalidate_required = True
        self._market_revalidate_reason = "DISCONNECTED_EVENT"
        disconnected_ib = self._market_ib
        disconnected_ib_id = id(disconnected_ib) if disconnected_ib is not None else None
        disconnected_client_id = self._market_client_id
        self._market_ib = None
        self._market_events_bound = False
        set_ibkr_connection_up("market", False)
        inc_ibkr_disconnect("market", reason_code, cause_type)
        logger.warning(
            "ib_market_shared_session_invalidated",
            disconnect_reason=reason_code,
            disconnect_cause=cause_type,
            disconnect_count=int(self._market_disconnect_count),
            client_id=disconnected_client_id,
            ib_id=disconnected_ib_id,
            disconnect_source=disconnect_source,
        )
        if self._should_emit_offhours_disconnected_log():
            logger.error(
                "ib_market_disconnected",
                disconnect_reason=reason_code,
                disconnect_cause=cause_type,
                disconnect_count=int(self._market_disconnect_count),
                disconnect_source=disconnect_source,
                last_error_code=error_code,
                last_error_msg=(error_msg or "")[:300],
                last_error_age_sec=error_age_sec,
            )
        self._trigger_runtime_recovery(
            reason=reason_code,
            source="market_disconnect_event",
            health_event={
                "event_type": "ib_disconnected",
                "reason": "ib_market_disconnected",
                "source": "market_disconnect_event",
                "detail": reason_code,
                "client_id": disconnected_client_id,
                "disconnect_reason": reason_code,
                "disconnect_cause": cause_type,
            },
        )

    def _unbind_market_events(self, ib: Optional[IB] = None) -> None:
        target_ib = ib or self._market_bound_ib
        if not target_ib or not self._market_events_bound:
            return
        if self._market_error_handler is not None:
            try:
                target_ib.errorEvent -= self._market_error_handler
            except Exception:
                pass
        if self._market_disconnected_handler is not None and hasattr(target_ib, "disconnectedEvent"):
            try:
                target_ib.disconnectedEvent -= self._market_disconnected_handler
            except Exception:
                pass
        self._market_events_bound = False
        self._market_bound_ib = None
        self._market_error_handler = None
        self._market_disconnected_handler = None

    def _bind_market_events(self) -> None:
        if not self._market_ib or self._market_events_bound:
            return
        bound_ib = self._market_ib

        def _on_error(req_id: int, error_code: int, error_string: str, contract) -> None:
            self._handle_market_error_event(req_id, error_code, error_string, contract, source_ib=bound_ib)

        def _on_disconnected() -> None:
            self._handle_market_disconnected(source_ib=bound_ib)

        self._market_ib.errorEvent += _on_error
        if hasattr(self._market_ib, "disconnectedEvent"):
            self._market_ib.disconnectedEvent += _on_disconnected
        self._market_bound_ib = bound_ib
        self._market_error_handler = _on_error
        self._market_disconnected_handler = _on_disconnected
        self._market_events_bound = True

    def _unbind_master_events(self, ib: Optional[IB] = None) -> None:
        target_ib = ib or self._master_bound_ib
        if not target_ib or not self._master_events_bound:
            return
        if self._master_disconnected_handler is not None and hasattr(target_ib, "disconnectedEvent"):
            try:
                target_ib.disconnectedEvent -= self._master_disconnected_handler
            except Exception:
                pass
        self._master_events_bound = False
        self._master_bound_ib = None
        self._master_disconnected_handler = None

    def _handle_master_disconnected(self, *, source_ib: Optional[IB] = None) -> None:
        if source_ib is not None and source_ib is not self._master_ib:
            logger.warning(
                "ib_master_stale_disconnect_ignored",
                source_ib_id=id(source_ib),
                active_ib_id=(id(self._master_ib) if self._master_ib is not None else None),
                active_client_id=self._master_client_id,
            )
            return
        disconnected_ib = self._master_ib
        disconnected_ib_id = id(disconnected_ib) if disconnected_ib is not None else None
        disconnected_client_id = self._master_client_id
        self._master_ib = None
        self._master_events_bound = False
        self._master_truth_complete = False
        self._master_truth_last_snapshot_ts = 0.0
        self._master_truth_open_orders_count = 0
        set_ibkr_connection_up("master", False)
        logger.error(
            "ib_master_disconnected",
            client_id=disconnected_client_id,
            ib_id=disconnected_ib_id,
        )
        self._trigger_runtime_recovery(
            reason="master_disconnected",
            source="master_disconnect_event",
            health_event={
                "event_type": "ib_disconnected",
                "reason": "ib_master_disconnected",
                "source": "master_disconnect_event",
                "detail": "master_disconnected",
                "client_id": disconnected_client_id,
            },
        )

    def _bind_master_events(self) -> None:
        if not self._master_ib or self._master_events_bound:
            return
        bound_ib = self._master_ib

        def _on_disconnected() -> None:
            self._handle_master_disconnected(source_ib=bound_ib)

        if hasattr(self._master_ib, "disconnectedEvent"):
            self._master_ib.disconnectedEvent += _on_disconnected
        self._master_bound_ib = bound_ib
        self._master_disconnected_handler = _on_disconnected
        self._master_events_bound = True

    async def _probe_market_connection(self, *, source: str) -> None:
        if not self._market_ib or not self._market_ib.isConnected():
            set_ibkr_connection_up("market", False)
            raise ConnectionError("market_ib_not_connected")
        probe_attempts = max(1, int(os.getenv("IB_MARKET_PROBE_ATTEMPTS", "3")))
        start = time.perf_counter_ns()
        last_exc: Exception | None = None
        for attempt in range(1, probe_attempts + 1):
            try:
                await asyncio.wait_for(
                    self._market_ib.reqCurrentTimeAsync(),
                    timeout=self._market_probe_timeout_sec,
                )
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                if attempt >= probe_attempts:
                    break
                logger.warning(
                    "ib_market_probe_retry",
                    source=source,
                    attempt=attempt,
                    max_attempts=probe_attempts,
                    timeout_sec=self._market_probe_timeout_sec,
                    error=str(exc),
                )
                await asyncio.sleep(min(1.0 * attempt, 3.0))
        if last_exc is not None:
            raise last_exc
        latency_ms = (time.perf_counter_ns() - start) / 1_000_000
        self._market_last_probe_ok_ts = time.time()
        self._market_last_probe_latency_ms = float(latency_ms)
        set_ibkr_connection_up("market", True)
        mark_ibkr_probe_ok("market", latency_ms)
        logger.info(
            "ib_market_probe_ok",
            source=source,
            latency_ms=round(latency_ms, 2),
            connected=True,
        )

    async def _market_probe_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._market_probe_sleep_interval_sec())
                async with self._market_lock:
                    if not self._market_ib:
                        continue
                    try:
                        await self._probe_market_connection(source="background")
                    except Exception as exc:
                        code, msg, age, _ctx = self._resolve_market_disconnect_context()
                        reason_code, _cause = self._classify_disconnect_reason(
                            error_code=code,
                            error_msg=msg or str(exc),
                            error_age_sec=age,
                        )
                        set_ibkr_connection_up("market", False)
                        inc_ibkr_probe_failure("market", reason_code)
                        if self._should_emit_offhours_probe_failure_log():
                            logger.warning(
                                "ib_market_probe_failed",
                                error=str(exc),
                                reason=reason_code,
                                last_error_code=code,
                                last_probe_ok_age_sec=(
                                    time.time() - self._market_last_probe_ok_ts
                                    if self._market_last_probe_ok_ts
                                    else None
                                ),
                            )
        except asyncio.CancelledError:
            return

    def _ensure_market_probe_task(self) -> None:
        if self._market_probe_task and not self._market_probe_task.done():
            return
        self._market_probe_task = asyncio.create_task(
            self._market_probe_loop(),
            name="ib_market_probe",
        )

    async def get_market_ib(self, *, host: Optional[str] = None, port: Optional[int] = None) -> IB:
        """Shared IB connection for market data + option chain + VIX ingestor."""
        self._ensure_runtime_recovery_task()
        h, p = _default_ib_host_port()
        host = host or h
        port = int(port or p)
        started_at = time.time()
        logger.info(
            "ib_market_get_begin",
            host=host,
            port=port,
            has_existing_ib=bool(self._market_ib),
            existing_connected=bool(self._market_ib and self._market_ib.isConnected()),
            current_client_id=self._market_client_id,
        )

        async with self._market_lock:
            if self._market_ib and self._market_ib.isConnected():
                logger.info(
                    "ib_market_get_reuse_connected",
                    host=host,
                    port=port,
                    client_id=self._market_client_id,
                    ib_id=id(self._market_ib),
                    latency_ms=round((time.time() - started_at) * 1000.0, 1),
                )
                return self._market_ib

            await self._ensure_recovery_short_circuit_allows_connect(
                caller="market",
                host=host,
                port=port,
            )
            await self._ensure_api_ready_before_real_connect(
                caller="market",
                host=host,
                port=port,
            )

            if self._market_ib is not None:
                stale_ib = self._market_ib
                self._unbind_market_events(stale_ib)
                try:
                    stale_ib.disconnect()
                    logger.warning(
                        "ib_market_stale_client_discarded",
                        host=host,
                        port=port,
                        client_id=self._market_client_id,
                        ib_id=id(stale_ib),
                    )
                except Exception as discard_exc:
                    logger.warning(
                        "ib_market_stale_client_discard_failed",
                        host=host,
                        port=port,
                        client_id=self._market_client_id,
                        ib_id=id(stale_ib),
                        error=str(discard_exc),
                    )
                self._market_ib = None
                self._market_events_bound = False

            logger.info(
                "ib_market_gatekeeper_acquire_begin",
                host=host,
                port=port,
                current_client_id=self._market_client_id or _default_market_client_id(),
            )
            allowed = await self._gatekeeper.acquire(caller="market", host=host, port=port)
            if not allowed:
                logger.warning(
                    "ib_market_gatekeeper_denied",
                    host=host,
                    port=port,
                    client_id=self._market_client_id or _default_market_client_id(),
                    latency_ms=round((time.time() - started_at) * 1000.0, 1),
                )
                raise ConnectionError("reconnect_gatekeeper_denied:market")

            gate_acquired = True
            seed_ib: Optional[IB] = None
            client_id: Optional[int] = None
            connected_ib: Optional[IB] = None
            try:
                seed_ib = self._market_ib or IB()
                client_id = self._market_client_id or _default_market_client_id()
                # With global single-flight gate, keep each lease short:
                # one handshake per lease, then release and let callers retry.
                max_attempts = int(os.getenv("IB_MARKET_CONNECT_MAX_ATTEMPTS", "3"))
                connect_timeout_sec = float(os.getenv("IB_MARKET_CONNECT_TIMEOUT_SEC", "15"))
                logger.info(
                    "ib_market_connect_flow_begin",
                    host=host,
                    port=port,
                    client_id=client_id,
                    seed_ib_id=id(seed_ib),
                    persisted_client_id=self._market_client_id,
                    timeout_streak=self._market_connect_timeout_streak,
                    max_attempts=max_attempts,
                    connect_timeout_sec=connect_timeout_sec,
                )
                connected_ib, used = await _connect_ib_with_retry(
                    seed_ib,
                    host,
                    port,
                    client_id,
                    ib_factory=IB,
                    timeout_client_id_fallbacks=int(os.getenv("IB_MARKET_TIMEOUT_CLIENT_ID_FALLBACKS", "2")),
                    max_attempts=max_attempts,
                    connect_timeout_sec=connect_timeout_sec,
                )
            except asyncio.CancelledError:
                working_ib = connected_ib or seed_ib
                if working_ib is not None:
                    try:
                        if working_ib.isConnected():
                            working_ib.disconnect()
                    except Exception as disconnect_exc:
                        logger.warning(
                            "ib_market_connect_cancel_disconnect_failed",
                            host=host,
                            port=port,
                            client_id=client_id,
                            ib_id=id(working_ib),
                            error=str(disconnect_exc),
                        )
                logger.warning(
                    "ib_market_connect_flow_cancelled",
                    host=host,
                    port=port,
                    client_id=client_id,
                    timeout_streak=self._market_connect_timeout_streak,
                    latency_ms=round((time.time() - started_at) * 1000.0, 1),
                )
                self._gatekeeper.report_aborted(caller="market", reason="connect_flow_cancelled")
                gate_acquired = False
                raise
            except Exception as exc:
                exc_text = _format_ib_exception(exc)
                self._advance_market_client_id_after_failure(
                    client_id=int(client_id),
                    exc=exc,
                    host=host,
                    port=port,
                    started_at=started_at,
                )
                logger.warning(
                    "ib_market_connect_flow_failed",
                    host=host,
                    port=port,
                    client_id=int(client_id),
                    timeout_streak=self._market_connect_timeout_streak,
                    error=exc_text,
                    latency_ms=round((time.time() - started_at) * 1000.0, 1),
                )
                self._gatekeeper.report_failure(caller="market", error=exc_text, host=host, port=port)
                gate_acquired = False
                raise

            if connected_ib is not self._market_ib:
                if self._market_ib is not None:
                    self._unbind_market_events(self._market_ib)
                self._market_ib = connected_ib
                self._market_events_bound = False
            self._bind_market_events()
            self._market_client_id = used
            self._market_connect_timeout_streak = 0
            self._market_degraded = False
            self._market_last_connectivity_error_code = None
            self._market_last_connectivity_error_msg = ""
            self._market_last_connectivity_error_ts = 0.0
            self._market_last_connect_error = None
            self._market_last_connect_error_ts = 0.0
            self._market_last_connect_error_client_id = None
            self._market_connection_epoch += 1
            _try_set_delayed_frozen_market_data(self._market_ib)
            set_ibkr_connection_up("market", True)
            await self._probe_market_connection(source="connect")
            self._ensure_market_probe_task()
            self._gatekeeper.report_success(caller="market")
            gate_acquired = False
            logger.info(
                "ib_market_connected",
                host=host,
                port=port,
                client_id=used,
                ib_id=id(self._market_ib),
                latency_ms=round((time.time() - started_at) * 1000.0, 1),
            )
            return self._market_ib

    async def get_master_ib(self, *, host: Optional[str] = None, port: Optional[int] = None) -> IB:
        """Read-only account-global truth session."""
        self._ensure_runtime_recovery_task()
        h, p = _default_ib_host_port()
        host = host or h
        port = int(port or p)
        started_at = time.time()
        async with self._master_lock:
            if self._master_ib and self._master_ib.isConnected():
                return self._master_ib

            await self._ensure_recovery_short_circuit_allows_connect(
                caller="master",
                host=host,
                port=port,
            )
            await self._ensure_api_ready_before_real_connect(
                caller="master",
                host=host,
                port=port,
            )

            logger.info(
                "ib_master_gatekeeper_acquire_begin",
                host=host,
                port=port,
                current_client_id=self._master_client_id or _default_master_client_id(),
            )
            allowed = await self._gatekeeper.acquire(caller="master", host=host, port=port)
            if not allowed:
                logger.warning(
                    "ib_master_gatekeeper_denied",
                    host=host,
                    port=port,
                    client_id=self._master_client_id or _default_master_client_id(),
                    latency_ms=round((time.time() - started_at) * 1000.0, 1),
                )
                raise ConnectionError("reconnect_gatekeeper_denied:master")

            gate_acquired = True
            seed_ib: Optional[IB] = None
            connected_ib: Optional[IB] = None
            client_id = self._master_client_id or _default_master_client_id()
            connect_done = False
            try:
                if self._master_ib is None:
                    self._master_ib = IB()
                seed_ib = self._master_ib
                connected_ib, used = await _connect_ib_with_retry(
                    seed_ib,
                    host,
                    port,
                    client_id,
                    ib_factory=IB,
                    timeout_client_id_fallbacks=int(os.getenv("IB_MASTER_TIMEOUT_CLIENT_ID_FALLBACKS", "2")),
                    max_attempts=int(os.getenv("IB_MASTER_CONNECT_MAX_ATTEMPTS", "3")),
                    connect_timeout_sec=float(os.getenv("IB_MASTER_CONNECT_TIMEOUT_SEC", "15")),
                )
                connect_done = True
            except asyncio.CancelledError:
                working_ib = connected_ib or seed_ib
                if working_ib is not None:
                    try:
                        if working_ib.isConnected():
                            working_ib.disconnect()
                    except Exception as disconnect_exc:
                        logger.warning(
                            "ib_master_connect_cancel_disconnect_failed",
                            host=host,
                            port=port,
                            client_id=client_id,
                            ib_id=id(working_ib),
                            error=str(disconnect_exc),
                        )
                self._gatekeeper.report_aborted(caller="master", reason="connect_flow_cancelled")
                gate_acquired = False
                raise
            except Exception as exc:
                exc_text = _format_ib_exception(exc)
                logger.warning(
                    "ib_master_connect_flow_failed",
                    host=host,
                    port=port,
                    client_id=int(client_id),
                    error=exc_text,
                    latency_ms=round((time.time() - started_at) * 1000.0, 1),
                )
                self._gatekeeper.report_failure(caller="master", error=exc_text, host=host, port=port)
                gate_acquired = False
                raise
            finally:
                if gate_acquired and not connect_done:
                    self._gatekeeper.report_aborted(caller="master", reason="connect_flow_exit_without_result")

            if connected_ib is not self._master_ib:
                if self._master_ib is not None:
                    self._unbind_master_events(self._master_ib)
                self._master_ib = connected_ib
            self._master_client_id = int(used)
            self._master_connection_epoch += 1
            self._master_truth_complete = False
            self._bind_master_events()
            set_ibkr_connection_up("master", True)
            self._gatekeeper.report_success(caller="master")
            logger.info(
                "ib_master_connected",
                host=host,
                port=port,
                client_id=self._master_client_id,
                epoch=self._master_connection_epoch,
                latency_ms=round((time.time() - started_at) * 1000.0, 1),
            )
            return self._master_ib

    async def refresh_master_truth_snapshot(self, *, force: bool = False) -> int:
        if not force and self._master_truth_complete and self._master_truth_last_snapshot_ts > 0:
            return int(self._master_truth_open_orders_count)
        ib = await self.get_master_ib()
        open_count = 0
        if hasattr(ib, "reqAllOpenOrdersAsync"):
            await ib.reqAllOpenOrdersAsync()
        elif hasattr(ib, "reqOpenOrdersAsync"):
            await ib.reqOpenOrdersAsync()
        else:
            ib.reqAllOpenOrders()
            await asyncio.sleep(0.2)
        await asyncio.sleep(0.2)
        try:
            open_count = len(list(ib.openTrades() or []))
        except Exception:
            open_count = 0
        self._master_truth_open_orders_count = int(open_count)
        self._master_truth_complete = True
        self._master_truth_last_snapshot_ts = time.time()
        logger.info(
            "ib_master_truth_snapshot_complete",
            open_orders=open_count,
            epoch=self._master_connection_epoch,
            client_id=self._master_client_id,
        )
        return int(open_count)

    async def get_market_ib_handle(self, *, host: Optional[str] = None, port: Optional[int] = None) -> IBHandle:
        ib = await self.get_market_ib(host=host, port=port)
        return IBHandle(role="market", client_id=int(self._market_client_id or 0), epoch=int(self._market_connection_epoch), ib=ib)

    async def get_master_ib_handle(self, *, host: Optional[str] = None, port: Optional[int] = None) -> IBHandle:
        ib = await self.get_master_ib(host=host, port=port)
        return IBHandle(role="master", client_id=int(self._master_client_id or 0), epoch=int(self._master_connection_epoch), ib=ib)

    async def get_exec_adapter(
        self,
        *,
        host: Optional[str] = None,
        port: Optional[int] = None,
        account: Optional[str] = None,
    ) -> IBInsyncAdapter:
        """Shared IB adapter for order placement, fills, and account summary."""
        self._ensure_runtime_recovery_task()
        h, p = _default_ib_host_port()
        host = host or h
        port = int(port or p)

        async with self._exec_lock:
            if self._exec_adapter and self._exec_adapter.ib and self._exec_adapter.ib.isConnected():
                return self._exec_adapter

            await self._ensure_recovery_short_circuit_allows_connect(
                caller="exec",
                host=host,
                port=port,
            )
            await self._ensure_api_ready_before_real_connect(
                caller="exec",
                host=host,
                port=port,
            )

            if self._exec_adapter is None:
                # Reuse legacy global adapter singleton when it already exists so
                # we do not spawn a second execution-session factory.
                reused_global = False
                try:
                    from services.exec.adapters.ib_adapter import peek_global_ib_adapter

                    existing = peek_global_ib_adapter()
                    if existing is not None:
                        self._exec_adapter = existing
                        reused_global = True
                except Exception:
                    reused_global = False

                if self._exec_adapter is None:
                    client_id = _default_exec_client_id()
                    self._exec_adapter = IBInsyncAdapter(host=host, port=port, client_id=client_id, account=account)
                elif reused_global:
                    logger.info(
                        "ib_exec_reused_global_adapter",
                        host=host,
                        port=port,
                        client_id=getattr(self._exec_adapter, "client_id", None),
                    )
            self._exec_adapter.host = host
            self._exec_adapter.port = port
            self._exec_adapter.account = account or self._exec_adapter.account

            self._exec_adapter._reconnect_gatekeeper = self._gatekeeper
            self._exec_adapter.configure_managed_mode(
                enabled=True,
                on_connected_callback=self._on_exec_adapter_connected,
                on_disconnected_callback=self._on_exec_adapter_disconnected,
            )

            await self._exec_adapter.start()

            if not (self._exec_adapter.ib and self._exec_adapter.ib.isConnected()):
                logger.warning(
                    "ib_exec_start_degraded",
                    host=host,
                    port=port,
                    client_id=self._exec_adapter.client_id,
                )
                raise ConnectionError("ib_exec_not_connected")

            logger.info("ib_exec_connected", host=host, port=port, client_id=self._exec_adapter.client_id)
            return self._exec_adapter

    async def _on_exec_adapter_connected(self, event: Dict[str, Any]) -> None:
        generation = int((event or {}).get("generation") or 0)
        if generation <= 0:
            generation = self._exec_last_connect_generation + 1
        if generation <= self._exec_last_connect_generation:
            return
        self._exec_last_connect_generation = generation
        self._exec_connection_epoch += 1
        logger.info(
            "ib_exec_connection_epoch_advanced",
            epoch=self._exec_connection_epoch,
            generation=generation,
            client_id=(event or {}).get("client_id"),
            source=(event or {}).get("source"),
        )
        await self._emit_health_event(
            {
                "event_type": "ib_connected",
                "reason": "ib_exec_connected",
                "source": str((event or {}).get("source") or "ib_exec_adapter"),
                "detail": f"generation={generation}",
                "generation": generation,
                "epoch": self._exec_connection_epoch,
                "client_id": (event or {}).get("client_id"),
            }
        )

    async def _on_exec_adapter_disconnected(self, event: Dict[str, Any]) -> None:
        generation = int((event or {}).get("generation") or 0)
        if generation <= 0:
            generation = self._exec_last_disconnect_generation + 1
        if generation <= self._exec_last_disconnect_generation:
            return
        self._exec_last_disconnect_generation = generation
        await self._emit_health_event(
            {
                "event_type": "ib_disconnected",
                "reason": "ib_exec_disconnected",
                "source": str((event or {}).get("source") or "ib_exec_adapter"),
                "detail": f"generation={generation}",
                "generation": generation,
                "epoch": self._exec_connection_epoch,
                "client_id": (event or {}).get("client_id"),
            }
        )
        self._trigger_runtime_recovery(reason="exec_disconnected", source="exec_disconnect_event")

    async def get_exec_ib_handle(
        self,
        *,
        host: Optional[str] = None,
        port: Optional[int] = None,
        account: Optional[str] = None,
    ) -> IBHandle:
        adapter = await self.get_exec_adapter(host=host, port=port, account=account)
        return IBHandle(
            role="exec",
            client_id=int(getattr(adapter, "client_id", 0) or 0),
            epoch=int(self._exec_connection_epoch),
            ib=adapter.ib,
        )

    async def wait_until_ready(
        self,
        *,
        host: Optional[str] = None,
        port: Optional[int] = None,
        account: Optional[str] = None,
        require_exec: bool = True,
        require_master: bool = False,
        timeout_sec: float = 25.0,
        initiator: str = "external",
    ) -> tuple[bool, bool]:
        """Wait until canonical sessions are connected and master truth is ready."""
        self._ensure_runtime_recovery_task()
        h, p = _default_ib_host_port()
        host = host or h
        port = int(port or p)
        deadline = time.time() + max(0.0, float(timeout_sec))
        market_ready = self.market_connected()
        exec_ready = self.exec_connected() if require_exec else True
        master_ready = self.master_connected() if require_master else True
        last_market_error: Optional[str] = None
        last_exec_error: Optional[str] = None
        last_master_error: Optional[str] = None

        while time.time() < deadline:
            now = time.time()
            circuit_state = get_ib_gateway_connection_circuit().check_allowed(now_ts=now)
            if circuit_state.open:
                if self._clear_stale_pre_connect_probe_manual_circuit_if_applicable(
                    caller=initiator,
                    host=host,
                    port=port,
                    circuit_state=circuit_state,
                ):
                    continue
                if (now - self._last_wait_short_circuit_log_ts) >= self._wait_short_circuit_log_cooldown_sec:
                    self._last_wait_short_circuit_log_ts = now
                    logger.warning(
                        "ib_wait_until_ready_gateway_circuit_open",
                        initiator=initiator,
                        remaining_sec=round(circuit_state.remaining_sec, 2),
                        reason=circuit_state.reason,
                        source=circuit_state.source,
                        consecutive_failures=circuit_state.consecutive_failures,
                        manual_required=bool(getattr(circuit_state, "manual_required", False)),
                    )
                if bool(getattr(circuit_state, "manual_required", False)):
                    await asyncio.sleep(
                        min(
                            max(1.0, float(timeout_sec)),
                            max(1.0, float(circuit_state.remaining_sec or 0.0)),
                        )
                    )
                    return market_ready, exec_ready
                api_ready = await self._probe_api_handshake_ready(
                    caller=f"{initiator}:circuit_recovery_probe",
                    host=host,
                    port=port,
                )
                if api_ready:
                    self._gatekeeper.clear_failure_backoff(
                        caller=f"{initiator}:circuit_recovery_probe",
                        reason="gateway_circuit_recovery_probe_ready",
                    )
                    self._clear_recovery_short_circuit(
                        reason="gateway_circuit_recovery_probe_ready",
                        source=f"{initiator}:circuit_recovery_probe",
                    )
                    logger.info(
                        "ib_wait_until_ready_gateway_circuit_cleared_by_probe",
                        initiator=initiator,
                        host=host,
                        port=port,
                        reason=circuit_state.reason,
                        source=circuit_state.source,
                    )
                    continue
                # Do not sleep the full circuit cooldown here. The durable
                # recovery-probe lease is the authority that prevents IB storms;
                # this loop must keep checking at a short cadence so an
                # externally restarted Gateway can be discovered promptly.
                await asyncio.sleep(
                    min(
                        max(1.0, float(self._gateway_circuit_recovery_probe_sleep_sec)),
                        max(0.0, deadline - time.time()),
                    )
                )
                continue
            short_circuit_remaining = self._recovery_short_circuit_remaining_sec(now_ts=now)
            if short_circuit_remaining > 0.0:
                if (now - self._last_wait_short_circuit_log_ts) >= self._wait_short_circuit_log_cooldown_sec:
                    self._last_wait_short_circuit_log_ts = now
                    logger.info(
                        "ib_wait_until_ready_short_circuit",
                        initiator=initiator,
                        remaining_sec=round(short_circuit_remaining, 2),
                        reason=self._recovery_short_circuit_reason,
                        source=self._recovery_short_circuit_source,
                    )
                api_ready = await self._probe_api_handshake_ready(
                    caller=f"{initiator}:circuit_recovery_probe",
                    host=host,
                    port=port,
                )
                if api_ready:
                    self._gatekeeper.clear_failure_backoff(
                        caller=f"{initiator}:circuit_recovery_probe",
                        reason="local_recovery_short_circuit_probe_ready",
                    )
                    self._clear_recovery_short_circuit(
                        reason="local_recovery_short_circuit_probe_ready",
                        source=f"{initiator}:circuit_recovery_probe",
                    )
                    logger.info(
                        "ib_wait_until_ready_short_circuit_cleared_by_probe",
                        initiator=initiator,
                        host=host,
                        port=port,
                    )
                    continue
                await asyncio.sleep(
                    min(
                        max(1.0, float(self._gateway_circuit_recovery_probe_sleep_sec)),
                        max(0.0, deadline - time.time()),
                    )
                )
                continue
            market_ready = self.market_connected()
            exec_ready = self.exec_connected() if require_exec else True
            master_ready = self.master_connected() if require_master else True
            if require_master and master_ready and (not self._master_truth_complete):
                try:
                    await self.refresh_master_truth_snapshot(force=True)
                except Exception as exc:
                    last_master_error = str(exc)
            if market_ready and exec_ready and (master_ready if require_master else True) and (
                self._master_truth_complete if require_master else True
            ):
                return market_ready, exec_ready

            manager_initiated = initiator == "manager_runtime_recovery"
            passive_wait_only = self._runtime_recovery_active and (not manager_initiated)
            if passive_wait_only:
                await asyncio.sleep(1.0)
                continue

            if not market_ready:
                try:
                    await self.get_market_ib(host=host, port=port)
                except Exception as exc:
                    exc_text = _format_ib_exception(exc)
                    last_market_error = exc_text
                    if self._is_upstream_outage_error(exc):
                        self._arm_recovery_short_circuit(
                            reason=exc_text,
                            source="ib_wait_until_ready_market_retry",
                        )
                    logger.warning(
                        "ib_wait_until_ready_market_retry",
                        host=host,
                        port=port,
                        error=exc_text,
                        active_client_id=self._market_client_id,
                        timeout_streak=self._market_connect_timeout_streak,
                    )
                await asyncio.sleep(1.0)
                continue

            if require_exec and not exec_ready:
                try:
                    await self.get_exec_adapter(host=host, port=port, account=account)
                except Exception as exc:
                    exc_text = _format_ib_exception(exc)
                    last_exec_error = exc_text
                    if self._is_upstream_outage_error(exc):
                        self._arm_recovery_short_circuit(
                            reason=exc_text,
                            source="ib_wait_until_ready_exec_retry",
                        )
                    logger.warning(
                        "ib_wait_until_ready_exec_retry",
                        host=host,
                        port=port,
                        error=exc_text,
                        active_client_id=getattr(self._exec_adapter, "client_id", None),
                    )
                await asyncio.sleep(1.0)
                continue

            if require_master and not master_ready:
                try:
                    await self.get_master_ib(host=host, port=port)
                except Exception as exc:
                    exc_text = _format_ib_exception(exc)
                    last_master_error = exc_text
                    if self._is_upstream_outage_error(exc):
                        self._arm_recovery_short_circuit(
                            reason=exc_text,
                            source="ib_wait_until_ready_master_retry",
                        )
                    logger.warning(
                        "ib_wait_until_ready_master_retry",
                        host=host,
                        port=port,
                        error=exc_text,
                        active_client_id=self._master_client_id,
                    )

            await asyncio.sleep(1.0)

        final_market_ready = self.market_connected()
        final_exec_ready = self.exec_connected() if require_exec else True
        final_master_ready = self.master_connected() if require_master else True
        final_master_truth_complete = self._master_truth_complete if require_master else True
        if final_market_ready and final_exec_ready and final_master_ready and final_master_truth_complete:
            return final_market_ready, final_exec_ready

        logger.error(
            "ib_wait_until_ready_timeout",
            host=host,
            port=port,
            market_ready=final_market_ready,
            exec_ready=final_exec_ready,
            master_ready=final_master_ready,
            master_truth_complete=final_master_truth_complete,
            last_market_error=last_market_error,
            last_exec_error=last_exec_error,
            last_master_error=last_master_error,
            market_client_id=self._market_client_id,
            market_timeout_streak=self._market_connect_timeout_streak,
            exec_client_id=getattr(self._exec_adapter, "client_id", None),
            master_client_id=self._master_client_id,
            timeout_sec=float(timeout_sec),
        )
        return final_market_ready, final_exec_ready

    async def shutdown(self) -> None:
        """Best-effort shutdown of shared sessions."""
        if self._runtime_recovery_task:
            task = self._runtime_recovery_task
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            self._runtime_recovery_task = None
            self._runtime_recovery_event = None
            self._runtime_recovery_active = False
        async with self._exec_lock:
            if self._exec_adapter:
                try:
                    await self._exec_adapter.stop(dispose=True)
                except Exception as exc:
                    logger.warning("ib_exec_shutdown_error", error=str(exc))
                self._exec_adapter = None

        async with self._market_lock:
            self._flush_market_noise_logs()
            if self._market_probe_task:
                self._market_probe_task.cancel()
                self._market_probe_task = None
            if self._market_ib:
                self._unbind_market_events(self._market_ib)
                try:
                    if self._market_ib.isConnected():
                        self._market_ib.disconnect()
                except Exception as exc:
                    logger.warning("ib_market_shutdown_error", error=str(exc))
                self._market_ib = None
                self._market_client_id = None
                self._market_events_bound = False
                set_ibkr_connection_up("market", False)

        async with self._master_lock:
            if self._master_ib:
                self._unbind_master_events(self._master_ib)
                try:
                    if self._master_ib.isConnected():
                        self._master_ib.disconnect()
                except Exception as exc:
                    logger.warning("ib_master_shutdown_error", error=str(exc))
                self._master_ib = None
                self._master_client_id = None
                self._master_truth_complete = False
                self._master_truth_last_snapshot_ts = 0.0
                self._master_truth_open_orders_count = 0
                set_ibkr_connection_up("master", False)
        await asyncio.sleep(0.05)


_MANAGER = IBConnectionManager()


def get_ib_connection_manager() -> IBConnectionManager:
    return _MANAGER
