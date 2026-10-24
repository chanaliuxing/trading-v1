#!/usr/bin/env python3
from __future__ import annotations
"""
Weekend Learning Supervisor

Goals:
- Ensure a single learning pipeline is running (scripts/ai_trader/cli.py learn)
- If it exits unexpectedly, capture last logs and restart
- HPO supports resume via storage/resume_state; RL training supports --resume (added)

Usage (cron or long-running):
  PYTHONPATH=$(pwd) python scripts/ai_trader/weekend_supervisor.py --interval-sec 300
"""
import argparse
import fcntl
import os
import signal
import subprocess
import sys
import time
from datetime import date
from pathlib import Path


def _off_day() -> bool:
    import pytz, datetime as dt
    from libs.market_calendar import is_holiday
    tz = pytz.timezone('America/New_York')
    now = dt.datetime.now(tz)
    return now.weekday() >= 5 or is_holiday(now.date())


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _pidfile() -> Path:
    return Path("runs/ai_trader") / date.today().isoformat() / "pids" / "weekend_learn.pid"


def _learn_ttl_lock() -> Path:
    return Path("runs/ai_trader") / date.today().isoformat() / "locks" / "learn_last_run.lock"


def _ensure_dirs(p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)


def _should_skip_learn(ttl_sec: int = 3600) -> bool:
    """Check if we should skip launching learn based on TTL lock.

    Returns True if learn was launched recently (within ttl_sec).
    """
    lock_file = _learn_ttl_lock()
    if not lock_file.exists():
        return False

    try:
        mtime = lock_file.stat().st_mtime
        elapsed = time.time() - mtime
        if elapsed < ttl_sec:
            return True  # Too soon, skip
    except Exception:
        pass

    return False


def _touch_learn_lock() -> None:
    """Touch the learn TTL lock to record launch time."""
    lock_file = _learn_ttl_lock()
    _ensure_dirs(lock_file)
    lock_file.touch()


def _launch_learning() -> int:
    """Launch learning with proper environment (PYTHONPATH, cwd).

    Includes OpenMP/PyTorch threading limits to prevent LSTM crashes.
    """
    import os
    project_root = Path(__file__).resolve().parent.parent.parent
    env = os.environ.copy()
    env["PYTHONPATH"] = str(project_root)
    # Fix PyTorch LSTM OpenMP threading crashes
    env["OMP_NUM_THREADS"] = "4"
    env["MKL_NUM_THREADS"] = "4"
    env["PYTORCH_NUM_THREADS"] = "4"
    env["OMP_NESTED"] = "FALSE"
    env["OMP_WAIT_POLICY"] = "PASSIVE"
    cmd = [sys.executable, "scripts/ai_trader/cli.py", "learn"]
    proc = subprocess.Popen(cmd, start_new_session=True, cwd=str(project_root), env=env)
    return proc.pid


_lock_handle = None

def _acquire_lock() -> bool:
    """Ensure single instance using an exclusive lock file (non-blocking)."""
    global _lock_handle
    try:
        lock_path = Path("runs/ai_trader") / date.today().isoformat() / "locks" / "weekend_supervisor.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        _lock_handle = open(lock_path, "w")
        fcntl.flock(_lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_handle.write(str(os.getpid()))
        _lock_handle.flush()
        return True
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval-sec", type=int, default=300)
    args = ap.parse_args()
    # Single-instance guard
    if not _acquire_lock():
        return
    while True:
        # Reap any zombie children to prevent accumulation
        try:
            while True:
                pid, status = os.waitpid(-1, os.WNOHANG)
                if pid == 0:
                    break
        except ChildProcessError:
            pass  # No children to reap

        if not _off_day():
            # Not weekend → exit supervisor loop
            time.sleep(args.interval_sec)
            continue
        pf = _pidfile()
        _ensure_dirs(pf)
        pid = None
        if pf.exists():
            try:
                pid = int(pf.read_text("utf-8").strip())
            except Exception:
                pid = None
        if pid and _pid_alive(pid):
            time.sleep(args.interval_sec)
            continue

        # Check TTL lock before launching (prevent rapid restarts)
        LEARN_MIN_INTERVAL_SEC = 3600  # 1 hour minimum between learn runs
        if _should_skip_learn(ttl_sec=LEARN_MIN_INTERVAL_SEC):
            time.sleep(args.interval_sec)
            continue

        # Either no pid or dead → launch a new learning session
        new_pid = _launch_learning()
        pf.write_text(str(new_pid), encoding="utf-8")
        _touch_learn_lock()  # Record launch time
        # Give it a grace period before next check
        time.sleep(args.interval_sec)


if __name__ == "__main__":
    main()
