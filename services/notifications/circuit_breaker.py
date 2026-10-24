from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class CircuitBreakerState:
    state: str = "CLOSED"
    failures: int = 0
    opened_at_mono: float = 0.0
    half_open_calls: int = 0


class CircuitBreaker:
    """Simple CLOSED/OPEN/HALF_OPEN circuit breaker."""

    def __init__(
        self,
        failure_threshold: int = 5,
        open_timeout_sec: float = 60.0,
        half_open_max_calls: int = 1,
    ):
        self.failure_threshold = max(1, int(failure_threshold))
        self.open_timeout_sec = max(1.0, float(open_timeout_sec))
        self.half_open_max_calls = max(1, int(half_open_max_calls))
        self._state = CircuitBreakerState()

    @property
    def state(self) -> str:
        return self._state.state

    def allow_request(self) -> bool:
        now = time.monotonic()
        if self._state.state == "OPEN":
            if (now - self._state.opened_at_mono) >= self.open_timeout_sec:
                self._state.state = "HALF_OPEN"
                self._state.half_open_calls = 0
            else:
                return False

        if self._state.state == "HALF_OPEN":
            if self._state.half_open_calls >= self.half_open_max_calls:
                return False
            self._state.half_open_calls += 1
            return True

        return True

    def on_success(self) -> None:
        self._state.state = "CLOSED"
        self._state.failures = 0
        self._state.half_open_calls = 0

    def on_failure(self) -> None:
        if self._state.state == "HALF_OPEN":
            self._state.state = "OPEN"
            self._state.opened_at_mono = time.monotonic()
            self._state.failures = self.failure_threshold
            self._state.half_open_calls = 0
            return

        self._state.failures += 1
        if self._state.failures >= self.failure_threshold:
            self._state.state = "OPEN"
            self._state.opened_at_mono = time.monotonic()
            self._state.half_open_calls = 0
