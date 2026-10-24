#!/usr/bin/env python3
from __future__ import annotations
"""
Force-reset the ML slot lock safely.

Behavior:
- If there are jobs marked as running in the queue, it will refuse to release the lock (to avoid corrupting a live run).
- If no running jobs are detected, it removes ml_slot.lock and cleans stale locks.
- With --kill, it will also terminate an existing ml_scheduler process (PID file) before cleanup.

Usage:
  python tools/reset_ml_slot.py         # safe release if no running jobs
  python tools/reset_ml_slot.py --kill  # also kill existing scheduler and clean locks
"""
import argparse
import os
import signal
from pathlib import Path


def main() -> int:
    from scripts.ai_trader.ml_scheduler import (
        _queue_running_jobs,
        _slot_lock_file,
        _release_slot,
        _scheduler_pid_file,
        _runs_dir,
    )
    parser = argparse.ArgumentParser()
    parser.add_argument("--kill", action="store_true", help="Kill existing ml_scheduler (from PID file) before cleanup")
    args = parser.parse_args()

    if args.kill:
        pf = _scheduler_pid_file()
        if pf.exists():
            try:
                pid = int(pf.read_text().strip())
                # Best-effort: SIGTERM, then SIGKILL if still alive
                try:
                    os.kill(pid, signal.SIGTERM)
                except Exception:
                    pass
                # Try SIGKILL
                try:
                    os.kill(pid, signal.SIGKILL)
                except Exception:
                    pass
                print(f"Killed scheduler pid={pid}")
            except Exception as e:
                print(f"Failed to kill scheduler: {e}")
        else:
            print("No scheduler PID file; skipping kill")

    running = _queue_running_jobs()
    if running:
        print(f"Refusing to release slot: {len(running)} job(s) marked as running in queue")
        return 2

    # Release slot (removes lock if present)
    _release_slot()
    lf = _slot_lock_file()
    if lf.exists():
        try:
            lf.unlink()
        except Exception:
            pass
    print(f"Slot released; lock exists? {lf.exists()} (path={lf})")

    # Clean dated locks for today as extra safety
    try:
        locks_dir = _runs_dir() / "locks"
        for p in locks_dir.glob("*.lock"):
            try:
                p.unlink()
            except Exception:
                pass
        print(f"Cleaned locks in {locks_dir}")
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

