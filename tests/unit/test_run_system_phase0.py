import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import run_system
from run_system import RealTradingSystem
from services.reconciliation.contracts import ReconcilerComponentResult


def _lock_helper_script() -> str:
    return textwrap.dedent(
        """
        import os
        import pathlib
        import run_system
        import sys
        import time

        lock_path = pathlib.Path(os.environ["RUN_SYSTEM_TEST_LOCK_PATH"])
        run_system._run_system_lock_path = lambda: lock_path
        acquired = run_system._acquire_run_system_lock()
        print("acquired=1" if acquired else "acquired=0", flush=True)
        if acquired and os.environ.get("RUN_SYSTEM_TEST_HOLD") == "1":
            time.sleep(float(os.environ.get("RUN_SYSTEM_TEST_SLEEP", "2")))
            run_system._release_run_system_lock()
        sys.exit(0 if acquired else 2)
        """
    )


def _run_lock_helper(lock_path: Path, *, hold: bool = False, sleep_sec: float = 2.0) -> subprocess.Popen:
    env = os.environ.copy()
    env["RUN_SYSTEM_TEST_LOCK_PATH"] = str(lock_path)
    if hold:
        env["RUN_SYSTEM_TEST_HOLD"] = "1"
        env["RUN_SYSTEM_TEST_SLEEP"] = str(sleep_sec)
    return subprocess.Popen(
        [sys.executable, "-c", _lock_helper_script()],
        cwd=str(Path(__file__).resolve().parents[2]),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _read_lock_helper_acquired_line(proc: subprocess.Popen, timeout_sec: float = 5.0) -> str:
    assert proc.stdout is not None
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                break
            time.sleep(0.01)
            continue
        stripped = line.strip()
        if stripped.startswith("acquired="):
            return stripped
    raise AssertionError("lock helper did not emit acquired marker")


def test_phase0_strategy_admission_provider_allows_only_live_baseline() -> None:
    system = RealTradingSystem()
    system._phase0_live_strategy_ids = {"qqq_intraday_001"}
    system._phase0_blocked_strategy_ids = {"stocks_kernel_shadow_spy_001"}

    live = system._phase0_strategy_admission_provider(
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        intent_kind="entry",
    )
    shadow = system._phase0_strategy_admission_provider(
        strategy_id="stocks_kernel_shadow_spy_001",
        symbol="SPY",
        intent_kind="entry",
    )
    unknown = system._phase0_strategy_admission_provider(
        strategy_id="new_live_strategy_001",
        symbol="QQQ",
        intent_kind="entry",
    )

    assert live == {"allowed": True, "reason": "phase0_live_strategy_admitted"}
    assert shadow["allowed"] is False
    assert shadow["reason"] == "phase0_forbidden_strategy_not_live_admitted"
    assert unknown["allowed"] is False
    assert unknown["reason"] == "phase0_strategy_not_in_live_baseline"


def test_run_system_lock_blocks_concurrent_process_and_recovers_after_exit(tmp_path: Path) -> None:
    lock_path = tmp_path / "run_system.lock"
    holder = _run_lock_helper(lock_path, hold=True, sleep_sec=10.0)
    try:
        assert _read_lock_helper_acquired_line(holder) == "acquired=1"

        blocked = _run_lock_helper(lock_path)
        blocked_stdout, blocked_stderr = blocked.communicate(timeout=5)
        assert blocked.returncode == 2, blocked_stderr
        assert "acquired=0" in blocked_stdout
    finally:
        if holder.poll() is None:
            holder.terminate()
        holder_stdout, holder_stderr = holder.communicate(timeout=5)
        assert holder.returncode in (0, -15), holder_stderr

    assert lock_path.exists()

    successor = _run_lock_helper(lock_path)
    successor_stdout, successor_stderr = successor.communicate(timeout=5)
    assert successor.returncode == 0, successor_stderr
    assert "acquired=1" in successor_stdout
    assert lock_path.exists()


def test_run_system_lock_dirty_file_is_overwritten_without_unlink(tmp_path: Path, monkeypatch) -> None:
    lock_path = tmp_path / "run_system.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("", encoding="utf-8")
    original_inode = lock_path.stat().st_ino

    monkeypatch.setattr(run_system, "_run_system_lock_path", lambda: lock_path)
    assert run_system._acquire_run_system_lock() is True
    try:
        assert lock_path.read_text(encoding="utf-8") == str(os.getpid())
    finally:
        run_system._release_run_system_lock()

    assert lock_path.exists()
    assert lock_path.stat().st_ino == original_inode

    lock_path.write_text("not-a-pid\npartial", encoding="utf-8")
    dirty_inode = lock_path.stat().st_ino
    assert run_system._acquire_run_system_lock() is True
    try:
        assert lock_path.read_text(encoding="utf-8") == str(os.getpid())
    finally:
        run_system._release_run_system_lock()

    assert lock_path.exists()
    assert lock_path.stat().st_ino == dirty_inode


def test_ib_startup_status_snapshot_requires_master_truth_when_enabled() -> None:
    class _FakeManager:
        @staticmethod
        def market_connected() -> bool:
            return True

        @staticmethod
        def exec_connected() -> bool:
            return True

        @staticmethod
        def master_connected() -> bool:
            return True

        @staticmethod
        def master_truth_complete() -> bool:
            return False

    snapshot = RealTradingSystem._ib_startup_status_snapshot(
        _FakeManager(),
        exec_enabled=True,
        master_enabled=True,
    )

    assert snapshot == {
        "market_ready": True,
        "exec_ready": True,
        "master_ready": True,
        "master_truth_complete": False,
        "ready": False,
    }


def test_ib_startup_status_snapshot_requires_master_truth_in_shadow_mode() -> None:
    class _FakeManager:
        @staticmethod
        def market_connected() -> bool:
            return True

        @staticmethod
        def exec_connected() -> bool:
            return False

        @staticmethod
        def master_connected() -> bool:
            return True

        @staticmethod
        def master_truth_complete() -> bool:
            return False

    snapshot = RealTradingSystem._ib_startup_status_snapshot(
        _FakeManager(),
        exec_enabled=False,
        master_enabled=True,
    )

    assert snapshot == {
        "market_ready": True,
        "exec_ready": True,
        "master_ready": True,
        "master_truth_complete": False,
        "ready": False,
    }


def test_run_system_startup_wait_until_ready_uses_authorized_recovery_initiator() -> None:
    source = Path(run_system.__file__).read_text(encoding="utf-8")
    assert 'initiator="run_system.start"' in source


def test_runtime_reconciliation_resume_metadata_reflects_dirty_truth() -> None:
    result = ReconcilerComponentResult(
        component="startup",
        ok=False,
        reason="startup_reconcile_diverged",
        metadata={
            "ghosts_found": 0,
            "orphans_found": 0,
            "mismatches": 0,
            "position_mismatches": 1,
            "order_truth": {"ghosts_found": 0, "orphans_found": 0, "mismatches": 0},
            "position": {"mismatch_count": 1},
        },
    )

    metadata = RealTradingSystem._runtime_reconciliation_resume_metadata(result)

    assert metadata["order_truth_clean"] is True
    assert metadata["position_ledger_clean"] is False
    assert metadata["position_mismatches"] == 1
