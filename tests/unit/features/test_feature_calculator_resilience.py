import asyncio
import pytest

import services.market.features.feature_calculator as fc_mod
from services.market.features.feature_calculator import FeatureCalculator


@pytest.mark.asyncio
async def test_execute_ch_command_with_retry_recovers_after_timeout(monkeypatch):
    calc = FeatureCalculator(resolution="1m")
    calc.ch_retry_max_attempts = 3
    calc.ch_retry_backoff_sec = 0.0
    calc.ch_retry_backoff_max_sec = 0.0
    calc.ch_send_receive_timeout_sec = 9.5
    calc._ch_semaphore = asyncio.Semaphore(1)

    calls = []

    async def _fake_execute(sql: str, send_receive_timeout_sec=None):
        calls.append((sql, send_receive_timeout_sec))
        if len(calls) == 1:
            raise RuntimeError("HTTPConnectionPool(host='localhost', port=8123): Read timed out")

    monkeypatch.setattr(fc_mod, "_execute_ch_command_async", _fake_execute)

    await calc._execute_ch_command_with_retry(
        "SELECT 1",
        context={"resolution": "1m", "batch": "1/1", "symbols": ["QQQ"]},
    )

    assert len(calls) == 2
    assert calls[0][1] == pytest.approx(9.5)
    assert calls[1][1] == pytest.approx(9.5)


@pytest.mark.asyncio
async def test_execute_ch_command_with_retry_does_not_retry_non_retryable(monkeypatch):
    calc = FeatureCalculator(resolution="1m")
    calc.ch_retry_max_attempts = 3
    calc.ch_retry_backoff_sec = 0.0
    calc.ch_retry_backoff_max_sec = 0.0
    calc._ch_semaphore = asyncio.Semaphore(1)

    calls = []

    async def _fake_execute(sql: str, send_receive_timeout_sec=None):
        calls.append((sql, send_receive_timeout_sec))
        raise RuntimeError("Syntax error: unexpected token")

    monkeypatch.setattr(fc_mod, "_execute_ch_command_async", _fake_execute)

    with pytest.raises(RuntimeError, match="Syntax error"):
        await calc._execute_ch_command_with_retry(
            "BAD SQL",
            context={"resolution": "1m", "batch": "1/1", "symbols": ["QQQ"]},
        )

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_compute_features_continues_when_single_batch_fails(monkeypatch):
    calc = FeatureCalculator(resolution="1m")
    calc.symbols = ["A", "B", "C"]
    calc.batch_size = 1

    monkeypatch.setattr(
        calc,
        "_build_feature_sql",
        lambda symbols_batch=None: f"SQL-{symbols_batch[0] if symbols_batch else 'ALL'}",
    )

    seen_batches = []

    async def _fake_exec_with_retry(sql: str, *, context):
        symbol = context["symbols"][0]
        seen_batches.append(symbol)
        if symbol == "B":
            raise RuntimeError("HTTPConnectionPool(host='localhost', port=8123): Read timed out")

    monkeypatch.setattr(calc, "_execute_ch_command_with_retry", _fake_exec_with_retry)

    await calc._compute_features()

    assert seen_batches == ["A", "B", "C"]


# ---------------------------------------------------------------------------
# connect_timeout override
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_execute_ch_command_async_uses_elevated_connect_timeout(monkeypatch):
    """_execute_ch_command_async must pass connect_timeout=5.0 to get_client."""
    captured_kwargs = {}

    class _FakeClient:
        def command(self, sql, **_):
            pass

    def _fake_get_client(**kwargs):
        captured_kwargs.update(kwargs)
        return _FakeClient()

    monkeypatch.setattr(fc_mod.ch_pool, "get_client", _fake_get_client)

    await fc_mod._execute_ch_command_async("SELECT 1", send_receive_timeout_sec=8.0)

    assert captured_kwargs.get("connect_timeout") == pytest.approx(5.0)
    assert captured_kwargs.get("send_receive_timeout") == pytest.approx(8.0)


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_circuit_breaker_opens_after_threshold_failures(monkeypatch):
    """After circuit_breaker_threshold consecutive failures, _circuit_open becomes True."""
    calc = FeatureCalculator(resolution="1m")
    calc._circuit_breaker_threshold = 3
    calc._circuit_breaker_probe_interval_sec = 0.0
    calc.poll_interval_sec = 0.0
    calc._running = True

    call_count = 0

    async def _always_fail():
        nonlocal call_count
        call_count += 1
        return False

    async def _fake_health_check():
        # Stop the loop after circuit opens so the test terminates.
        calc._running = False
        return False

    monkeypatch.setattr(calc, "_compute_features", _always_fail)
    monkeypatch.setattr(calc, "_ch_health_check", _fake_health_check)

    await calc._compute_loop()

    assert calc._circuit_open is True
    # Must have run at least threshold times before opening
    assert call_count >= calc._circuit_breaker_threshold


@pytest.mark.asyncio
async def test_circuit_breaker_closes_when_ch_recovers(monkeypatch):
    """Once health check succeeds, circuit closes and computation resumes."""
    calc = FeatureCalculator(resolution="1m")
    calc._circuit_breaker_threshold = 2
    calc._circuit_breaker_probe_interval_sec = 0.0
    calc.poll_interval_sec = 0.0
    calc._circuit_open = True          # start already OPEN
    calc._compute_failure_streak = 5
    calc._running = True

    resumed = []

    async def _fake_health_check():
        return True

    async def _succeeds():
        resumed.append(True)
        calc._running = False          # stop after one successful run
        return True

    monkeypatch.setattr(calc, "_ch_health_check", _fake_health_check)
    monkeypatch.setattr(calc, "_compute_features", _succeeds)

    await calc._compute_loop()

    assert calc._circuit_open is False
    assert calc._compute_failure_streak == 0
    assert resumed == [True]


# ---------------------------------------------------------------------------
# Jitter: sleep is bounded and non-zero on failure
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_backoff_sleep_has_jitter_and_floor(monkeypatch):
    """Sleep on failure must be >= poll_interval_sec and vary across calls."""
    calc = FeatureCalculator(resolution="1m")
    calc._circuit_breaker_threshold = 100   # don't open
    calc._circuit_breaker_probe_interval_sec = 0.0
    calc.poll_interval_sec = 1.0
    calc._max_backoff_multiplier = 12
    calc._running = True

    fail_count = 0
    sleep_values = []

    async def _always_fail():
        nonlocal fail_count
        fail_count += 1
        if fail_count >= 5:
            calc._running = False
        return False

    async def _fake_sleep(secs):
        sleep_values.append(secs)

    monkeypatch.setattr(calc, "_compute_features", _always_fail)
    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    await calc._compute_loop()

    # Every sleep must be at least poll_interval_sec
    assert all(s >= calc.poll_interval_sec for s in sleep_values), sleep_values
    # With jitter, not all sleeps should be identical (probabilistic; >=2 values)
    if len(sleep_values) >= 2:
        assert not all(s == sleep_values[0] for s in sleep_values), (
            "All sleep values identical — jitter not applied"
        )
