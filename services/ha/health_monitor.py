"""
Health Monitor & Circuit Breaker
Based on doc/76.md specification

Unified health monitoring and automatic degradation/circuit breaking.

Features:
- Service health tracking (OK/WARN/ERROR)
- Heartbeat monitoring
- Automatic degradation (reduce, don't increase)
- Circuit breaker integration
- StateBus integration for cross-process coordination
"""
import asyncio
import time
from typing import Dict, Optional, List, Callable, Any
from enum import Enum
from dataclasses import dataclass

from libs.logger import get_logger

logger = get_logger(__name__)


class HealthState(Enum):
    """Service health state"""
    OK = 0
    WARN = 1
    ERROR = 2


@dataclass
class HealthStatus:
    """Health status for a service"""
    service: str
    state: HealthState
    last_heartbeat: float
    restart_count: int
    error_msg: Optional[str] = None
    degraded_since: Optional[float] = None


@dataclass
class CircuitBreakerConfig:
    """Circuit breaker configuration"""
    # Failure thresholds
    error_threshold: int = 5  # Errors in window
    error_window_s: int = 300  # 5 minutes

    # Timeout thresholds
    timeout_threshold: int = 3
    timeout_window_s: int = 60

    # Reset
    success_threshold: int = 3  # Consecutive successes to close
    half_open_timeout_s: int = 30


class CircuitBreakerState(Enum):
    """Circuit breaker state"""
    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Failing, reject requests
    HALF_OPEN = "half_open"  # Testing recovery


class CircuitBreaker:
    """
    Circuit breaker for service protection

    Prevents cascading failures by opening circuit on repeated errors.
    """

    def __init__(
        self,
        name: str,
        config: Optional[CircuitBreakerConfig] = None
    ):
        """
        Initialize circuit breaker

        Args:
            name: Breaker name
            config: Configuration
        """
        self.name = name
        self.config = config or CircuitBreakerConfig()

        # State
        self.state = CircuitBreakerState.CLOSED
        self.error_count = 0
        self.timeout_count = 0
        self.success_count = 0
        self.last_error_time = 0.0
        self.last_timeout_time = 0.0
        self.opened_at = 0.0

        # History (for windowing)
        self.error_history: List[float] = []
        self.timeout_history: List[float] = []

        logger.info(
            "CircuitBreaker initialized",
            name=name,
            error_threshold=self.config.error_threshold,
            timeout_threshold=self.config.timeout_threshold
        )

    def _clean_history(self, history: List[float], window_s: int) -> None:
        """Clean old entries from history"""
        now = time.time()
        cutoff = now - window_s

        # Remove old entries
        while history and history[0] < cutoff:
            history.pop(0)

    def record_success(self) -> None:
        """Record successful operation"""
        if self.state == CircuitBreakerState.HALF_OPEN:
            self.success_count += 1

            if self.success_count >= self.config.success_threshold:
                # Close circuit
                self.state = CircuitBreakerState.CLOSED
                self.error_count = 0
                self.timeout_count = 0
                self.success_count = 0
                self.error_history.clear()
                self.timeout_history.clear()

                logger.info("CircuitBreaker closed", name=self.name)

    def record_error(self) -> None:
        """Record error"""
        now = time.time()

        self.error_history.append(now)
        self._clean_history(self.error_history, self.config.error_window_s)

        self.error_count = len(self.error_history)
        self.last_error_time = now

        # Check threshold
        if self.error_count >= self.config.error_threshold:
            self._open_circuit()

    def record_timeout(self) -> None:
        """Record timeout"""
        now = time.time()

        self.timeout_history.append(now)
        self._clean_history(self.timeout_history, self.config.timeout_window_s)

        self.timeout_count = len(self.timeout_history)
        self.last_timeout_time = now

        # Check threshold
        if self.timeout_count >= self.config.timeout_threshold:
            self._open_circuit()

    def _open_circuit(self) -> None:
        """Open circuit"""
        if self.state != CircuitBreakerState.OPEN:
            self.state = CircuitBreakerState.OPEN
            self.opened_at = time.time()

            logger.error(
                "CircuitBreaker opened",
                name=self.name,
                error_count=self.error_count,
                timeout_count=self.timeout_count
            )

    def can_proceed(self) -> bool:
        """
        Check if operation can proceed

        Returns:
            True if circuit allows operation
        """
        if self.state == CircuitBreakerState.CLOSED:
            return True

        if self.state == CircuitBreakerState.OPEN:
            # Check if we should try half-open
            elapsed = time.time() - self.opened_at
            if elapsed >= self.config.half_open_timeout_s:
                self.state = CircuitBreakerState.HALF_OPEN
                self.success_count = 0

                logger.warning("CircuitBreaker half-open", name=self.name)
                return True

            return False

        # HALF_OPEN: allow request (will test recovery)
        return True


class HealthMonitor:
    """
    Health monitor for all services

    Tracks service health, heartbeats, and coordinates degradation.
    """

    def __init__(
        self,
        state_bus: Any,
        heartbeat_timeout_s: int = 10
    ):
        """
        Initialize health monitor

        Args:
            state_bus: StateBus for coordination
            heartbeat_timeout_s: Heartbeat timeout
        """
        self.bus = state_bus
        self.heartbeat_timeout_s = heartbeat_timeout_s

        # Service health
        self.services: Dict[str, HealthStatus] = {}

        # Circuit breakers
        self.breakers: Dict[str, CircuitBreaker] = {}

        # System state
        self.system_degraded = False
        self.pause_new_orders = False

        logger.info(
            "HealthMonitor initialized",
            heartbeat_timeout_s=heartbeat_timeout_s
        )

    def register_service(self, service: str) -> None:
        """
        Register service for monitoring

        Args:
            service: Service name
        """
        self.services[service] = HealthStatus(
            service=service,
            state=HealthState.OK,
            last_heartbeat=time.time(),
            restart_count=0
        )

        # Create circuit breaker
        self.breakers[service] = CircuitBreaker(name=service)

        logger.info("Service registered", service=service)

    async def heartbeat(self, service: str) -> None:
        """
        Record service heartbeat

        Args:
            service: Service name
        """
        if service not in self.services:
            self.register_service(service)

        status = self.services[service]
        status.last_heartbeat = time.time()

        # Write to StateBus
        if hasattr(self.bus, 'set'):
            await self.bus.set(f"HEARTBEAT::{service}", str(status.last_heartbeat))

    async def set_health(
        self,
        service: str,
        state: HealthState,
        error_msg: Optional[str] = None
    ) -> None:
        """
        Set service health state

        Args:
            service: Service name
            state: Health state
            error_msg: Optional error message
        """
        if service not in self.services:
            self.register_service(service)

        status = self.services[service]
        old_state = status.state
        status.state = state
        status.error_msg = error_msg

        if state != HealthState.OK and status.degraded_since is None:
            status.degraded_since = time.time()
        elif state == HealthState.OK:
            status.degraded_since = None

        # Write to StateBus
        if hasattr(self.bus, 'set'):
            await self.bus.set(f"HEALTH::{service}", state.name)

        logger.info(
            "Health state changed",
            service=service,
            old_state=old_state.name,
            new_state=state.name,
            error_msg=error_msg
        )

        # Check system degradation
        await self._check_system_degradation()

    async def record_restart(self, service: str) -> None:
        """
        Record service restart

        Args:
            service: Service name
        """
        if service not in self.services:
            self.register_service(service)

        status = self.services[service]
        status.restart_count += 1

        logger.warning(
            "Service restart recorded",
            service=service,
            restart_count=status.restart_count
        )

        # Check for flapping
        if status.restart_count >= 5:
            await self.set_health(service, HealthState.ERROR, "Excessive restarts")

    def get_breaker(self, service: str) -> Optional[CircuitBreaker]:
        """
        Get circuit breaker for service

        Args:
            service: Service name

        Returns:
            CircuitBreaker or None
        """
        return self.breakers.get(service)

    async def _check_system_degradation(self) -> None:
        """Check if system should degrade"""
        # Count services in bad health
        error_count = sum(
            1 for s in self.services.values()
            if s.state == HealthState.ERROR
        )

        warn_count = sum(
            1 for s in self.services.values()
            if s.state == HealthState.WARN
        )

        # System degradation rules
        if error_count >= 2 or warn_count >= 3:
            if not self.system_degraded:
                self.system_degraded = True

                logger.error(
                    "System degraded",
                    error_count=error_count,
                    warn_count=warn_count
                )

                # Pause new orders
                await self._pause_new_orders()

        # Recovery
        elif error_count == 0 and warn_count <= 1:
            if self.system_degraded:
                self.system_degraded = False

                logger.info("System recovered from degradation")

                # Resume orders (with caution)
                await self._resume_new_orders()

    async def _pause_new_orders(self) -> None:
        """Pause new orders system-wide"""
        self.pause_new_orders = True

        if hasattr(self.bus, 'set'):
            await self.bus.set("SYSTEM::PAUSE_NEW_ORDERS", "true")

        logger.critical("New orders paused system-wide")

    async def _resume_new_orders(self) -> None:
        """Resume new orders"""
        self.pause_new_orders = False

        if hasattr(self.bus, 'set'):
            await self.bus.set("SYSTEM::PAUSE_NEW_ORDERS", "false")

        logger.info("New orders resumed")

    async def check_heartbeats(self) -> None:
        """Check all service heartbeats"""
        now = time.time()

        for service, status in self.services.items():
            age = now - status.last_heartbeat

            if age > self.heartbeat_timeout_s:
                # Heartbeat timeout
                if status.state != HealthState.ERROR:
                    await self.set_health(
                        service,
                        HealthState.ERROR,
                        f"Heartbeat timeout ({age:.1f}s)"
                    )

    async def monitor_loop(self, interval_s: int = 5) -> None:
        """
        Main monitoring loop

        Args:
            interval_s: Check interval
        """
        logger.info("HealthMonitor loop started", interval_s=interval_s)

        while True:
            try:
                await self.check_heartbeats()
            except Exception as e:
                logger.error("Monitor loop error", error=str(e), exc_info=True)

            await asyncio.sleep(interval_s)

    def get_status_summary(self) -> Dict[str, Any]:
        """
        Get health status summary

        Returns:
            Status summary dict
        """
        return {
            "system_degraded": self.system_degraded,
            "pause_new_orders": self.pause_new_orders,
            "services": {
                name: {
                    "state": status.state.name,
                    "last_heartbeat_age_s": time.time() - status.last_heartbeat,
                    "restart_count": status.restart_count,
                    "error_msg": status.error_msg
                }
                for name, status in self.services.items()
            },
            "breakers": {
                name: {
                    "state": breaker.state.name,
                    "error_count": breaker.error_count,
                    "timeout_count": breaker.timeout_count
                }
                for name, breaker in self.breakers.items()
            }
        }
