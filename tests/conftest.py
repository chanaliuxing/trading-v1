import os
import sys
from pathlib import Path

import pytest


def _ensure_project_root_on_syspath() -> None:
    # Pytest rootdir is often `tests/`, which makes imports like `import libs.*` fail
    # unless the project root is on sys.path. Keep this lightweight and deterministic.
    project_root = Path(__file__).resolve().parents[1]
    pr = str(project_root)
    if pr not in sys.path:
        sys.path.insert(0, pr)


_ensure_project_root_on_syspath()

_TEST_LOG_DIR = Path(__file__).resolve().parents[1] / "logs"
_TEST_LOG_DIR.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("AUDIT_LOG_PATH", str(_TEST_LOG_DIR / "pytest_trader.log"))


_LIVE_IBKR_HOSTS = {"127.0.0.1", "localhost", "::1"}
_LIVE_IBKR_PORTS = {4001, 4002}


class TestSafetyError(AssertionError):
    """Raised when a unit test attempts to touch a live broker endpoint."""


def _is_live_ibkr_endpoint(host: object, port: object) -> bool:
    host_s = str(host or "").strip().lower()
    if host_s in {"", "none"}:
        host_s = "127.0.0.1"
    try:
        port_i = int(port)
    except Exception:
        return False
    return host_s in _LIVE_IBKR_HOSTS and port_i in _LIVE_IBKR_PORTS


def assert_no_live_ibkr_endpoint(host: object, port: object) -> None:
    if _is_live_ibkr_endpoint(host, port):
        raise TestSafetyError(
            "Unit tests must not connect to live IBKR. Use FakeIBAdapter or "
            "mark integration test with explicit opt-in."
        )


@pytest.fixture(autouse=True)
def _block_live_ibkr_in_unit_tests(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest):
    """Fail fast if a unit test attempts to connect to local TWS/Gateway."""

    allow_live = os.getenv("TRADING_TEST_ALLOW_LIVE_IBKR") == "1"
    live_marker = request.node.get_closest_marker("live_ibkr")
    if live_marker is not None and not allow_live:
        raise TestSafetyError(
            "live_ibkr tests require TRADING_TEST_ALLOW_LIVE_IBKR=1 and must not run in unit defaults."
        )
    if allow_live:
        return

    try:
        from ib_insync import IB
    except Exception:
        return

    async def _blocked_connect_async(self, host="127.0.0.1", port=4002, clientId=0, *args, **kwargs):
        assert_no_live_ibkr_endpoint(host, port)
        raise TestSafetyError(
            "Unit tests must use fakes for non-local/non-standard IBKR endpoints as well."
        )

    def _blocked_connect(self, host="127.0.0.1", port=4002, clientId=0, *args, **kwargs):
        assert_no_live_ibkr_endpoint(host, port)
        raise TestSafetyError(
            "Unit tests must use fakes for non-local/non-standard IBKR endpoints as well."
        )

    monkeypatch.setattr(IB, "connectAsync", _blocked_connect_async, raising=False)
    monkeypatch.setattr(IB, "connect", _blocked_connect, raising=False)
