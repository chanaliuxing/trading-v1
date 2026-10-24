"""
System Manager
Based on doc/76.md specification

Orchestrates service startup, shutdown, and recovery.

Features:
- Ordered startup/shutdown
- Crash recovery with exponential backoff
- Heartbeat registration
- Configuration hot reload
- Graceful degradation
"""
import asyncio
import time
import signal
from typing import Dict, List, Optional, Callable, Any
from dataclasses import dataclass
from enum import Enum

from services.ha.health_monitor import HealthMonitor, HealthState
from libs.logger import get_logger

logger = get_logger(__name__)


class ServiceState(Enum):
    """Service state"""
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    FAILED = "failed"


@dataclass
class ServiceConfig:
    """Service configuration"""
    name: str
    start_fn: Callable
    stop_fn: Optional[Callable] = None
    health_check_fn: Optional[Callable] = None
    restart_on_failure: bool = True
    max_restarts: int = 5
    restart_window_s: int = 300  # 5 minutes


class ManagedService:
    """Managed service wrapper"""

    def __init__(self, config: ServiceConfig):
        """
        Initialize managed service

        Args:
            config: Service configuration
        """
        self.config = config
        self.state = ServiceState.STOPPED
        self.restart_count = 0
        self.restart_times: List[float] = []
        self.task: Optional[asyncio.Task] = None
        self.backoff_s = 1.0

    def record_restart(self) -> None:
        """Record restart time"""
        now = time.time()
        self.restart_times.append(now)

        # Clean old restarts outside window
        cutoff = now - self.config.restart_window_s
        self.restart_times = [t for t in self.restart_times if t > cutoff]

        self.restart_count = len(self.restart_times)

    def should_restart(self) -> bool:
        """Check if service should restart"""
        if not self.config.restart_on_failure:
            return False

        if self.restart_count >= self.config.max_restarts:
            logger.error(
                "Max restarts exceeded",
                service=self.config.name,
                restart_count=self.restart_count
            )
            return False

        return True

    def get_backoff(self) -> float:
        """Get backoff delay (exponential with cap)"""
        delay = min(self.backoff_s, 60.0)
        self.backoff_s = min(self.backoff_s * 2, 60.0)
        return delay

    def reset_backoff(self) -> None:
        """Reset backoff on successful start"""
        self.backoff_s = 1.0


class SystemManager:
    """
    System manager for orchestrating services

    Manages startup, shutdown, health, and recovery.
    """

    def __init__(
        self,
        health_monitor: HealthMonitor,
        state_bus: Any
    ):
        """
        Initialize system manager

        Args:
            health_monitor: Health monitor
            state_bus: StateBus for coordination
        """
        self.health = health_monitor
        self.bus = state_bus

        # Services
        self.services: Dict[str, ManagedService] = {}
        self.startup_order: List[str] = []
        self.shutdown_order: List[str] = []

        # State
        self.running = False
        self.shutting_down = False

        # Tasks
        self.heartbeat_task: Optional[asyncio.Task] = None
        self.watchdog_task: Optional[asyncio.Task] = None

        logger.info("SystemManager initialized")

    def register_service(
        self,
        name: str,
        start_fn: Callable,
        stop_fn: Optional[Callable] = None,
        health_check_fn: Optional[Callable] = None,
        **kwargs
    ) -> None:
        """
        Register service

        Args:
            name: Service name
            start_fn: Async function to start service
            stop_fn: Optional async function to stop service
            health_check_fn: Optional health check function
            **kwargs: Additional ServiceConfig parameters
        """
        config = ServiceConfig(
            name=name,
            start_fn=start_fn,
            stop_fn=stop_fn,
            health_check_fn=health_check_fn,
            **kwargs
        )

        self.services[name] = ManagedService(config)
        self.health.register_service(name)

        logger.info("Service registered", service=name)

    def set_startup_order(self, order: List[str]) -> None:
        """
        Set startup order

        Args:
            order: List of service names in startup order
        """
        self.startup_order = order
        self.shutdown_order = list(reversed(order))

        logger.info("Startup order set", order=order)

    async def start_service(self, name: str) -> bool:
        """
        Start service

        Args:
            name: Service name

        Returns:
            True if started successfully
        """
        if name not in self.services:
            logger.error("Service not registered", service=name)
            return False

        svc = self.services[name]

        if svc.state == ServiceState.RUNNING:
            logger.warning("Service already running", service=name)
            return True

        svc.state = ServiceState.STARTING

        logger.info("Starting service", service=name)

        try:
            # Start service
            svc.task = asyncio.create_task(svc.config.start_fn())
            svc.state = ServiceState.RUNNING
            svc.reset_backoff()

            await self.health.set_health(name, HealthState.OK)

            logger.info("Service started", service=name)

            return True

        except Exception as e:
            logger.error(
                "Service start failed",
                service=name,
                error=str(e),
                exc_info=True
            )

            svc.state = ServiceState.FAILED
            await self.health.set_health(name, HealthState.ERROR, str(e))

            return False

    async def stop_service(self, name: str) -> None:
        """
        Stop service

        Args:
            name: Service name
        """
        if name not in self.services:
            logger.error("Service not registered", service=name)
            return

        svc = self.services[name]

        if svc.state == ServiceState.STOPPED:
            logger.warning("Service already stopped", service=name)
            return

        svc.state = ServiceState.STOPPING

        logger.info("Stopping service", service=name)

        try:
            # Call stop function
            if svc.config.stop_fn:
                await svc.config.stop_fn()

            # Cancel task
            if svc.task and not svc.task.done():
                svc.task.cancel()
                try:
                    await svc.task
                except asyncio.CancelledError:
                    pass

            svc.state = ServiceState.STOPPED
            await self.health.set_health(name, HealthState.OK)

            logger.info("Service stopped", service=name)

        except Exception as e:
            logger.error(
                "Service stop failed",
                service=name,
                error=str(e),
                exc_info=True
            )

    async def restart_service(self, name: str) -> None:
        """
        Restart service

        Args:
            name: Service name
        """
        logger.warning("Restarting service", service=name)

        svc = self.services[name]

        # Record restart
        svc.record_restart()
        await self.health.record_restart(name)

        # Check if should restart
        if not svc.should_restart():
            logger.error("Service restart blocked", service=name)
            return

        # Get backoff delay
        delay = svc.get_backoff()
        logger.info("Restart backoff", service=name, delay_s=delay)

        await asyncio.sleep(delay)

        # Stop then start
        await self.stop_service(name)
        await self.start_service(name)

    async def start_all(self) -> None:
        """Start all services in order"""
        logger.info("Starting all services", order=self.startup_order)

        self.running = True

        for name in self.startup_order:
            if not await self.start_service(name):
                logger.error("Failed to start service, aborting", service=name)
                await self.stop_all()
                return

            # Brief delay between starts
            await asyncio.sleep(1.0)

        logger.info("All services started")

        # Start background tasks
        self.heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        self.watchdog_task = asyncio.create_task(self._watchdog_loop())

    async def stop_all(self) -> None:
        """Stop all services in reverse order"""
        logger.info("Stopping all services", order=self.shutdown_order)

        self.shutting_down = True

        # Stop background tasks
        if self.heartbeat_task:
            self.heartbeat_task.cancel()
        if self.watchdog_task:
            self.watchdog_task.cancel()

        for name in self.shutdown_order:
            await self.stop_service(name)

            # Brief delay between stops
            await asyncio.sleep(0.5)

        self.running = False

        logger.info("All services stopped")

    async def _heartbeat_loop(self, interval_s: int = 2) -> None:
        """
        Heartbeat loop

        Args:
            interval_s: Heartbeat interval
        """
        logger.info("Heartbeat loop started", interval_s=interval_s)

        while self.running and not self.shutting_down:
            try:
                for name, svc in self.services.items():
                    if svc.state == ServiceState.RUNNING:
                        await self.health.heartbeat(name)

            except Exception as e:
                logger.error("Heartbeat error", error=str(e))

            await asyncio.sleep(interval_s)

    async def _watchdog_loop(self, interval_s: int = 10) -> None:
        """
        Watchdog loop (monitor and restart failed services)

        Args:
            interval_s: Check interval
        """
        logger.info("Watchdog loop started", interval_s=interval_s)

        while self.running and not self.shutting_down:
            try:
                for name, svc in self.services.items():
                    # Check if task died
                    if svc.state == ServiceState.RUNNING:
                        if svc.task and svc.task.done():
                            # Task died
                            logger.error("Service task died", service=name)

                            svc.state = ServiceState.FAILED
                            await self.health.set_health(
                                name,
                                HealthState.ERROR,
                                "Task died"
                            )

                            # Restart
                            if svc.config.restart_on_failure:
                                await self.restart_service(name)

                    # Run health check
                    if svc.config.health_check_fn:
                        try:
                            healthy = await svc.config.health_check_fn()

                            if not healthy:
                                logger.warning("Health check failed", service=name)

                                await self.health.set_health(
                                    name,
                                    HealthState.WARN,
                                    "Health check failed"
                                )

                        except Exception as e:
                            logger.error(
                                "Health check error",
                                service=name,
                                error=str(e)
                            )

            except Exception as e:
                logger.error("Watchdog error", error=str(e))

            await asyncio.sleep(interval_s)

    def setup_signal_handlers(self) -> None:
        """Setup signal handlers for graceful shutdown"""

        def signal_handler(sig, frame):
            logger.info("Signal received", signal=sig)
            asyncio.create_task(self.stop_all())

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        logger.info("Signal handlers registered")

    async def run(self) -> None:
        """Run system manager"""
        logger.info("SystemManager starting")

        self.setup_signal_handlers()

        await self.start_all()

        # Wait for shutdown
        while self.running:
            await asyncio.sleep(1.0)

        logger.info("SystemManager stopped")
