#!/usr/bin/env python3
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List


def _pgrep(pattern: str) -> List[int]:
    """Return list of PIDs matching the pattern via pgrep -f."""
    try:
        proc = subprocess.run(
            ["pgrep", "-f", pattern],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    pids: List[int] = []
    for line in proc.stdout.split():
        try:
            pid = int(line.strip())
            if pid > 0 and pid != os.getpid():
                pids.append(pid)
        except Exception:
            continue
    return pids


def _kill_pids(pids: List[int], sig: int) -> None:
    for pid in pids:
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            continue
        except Exception:
            continue


def _still_alive(pids: List[int]) -> List[int]:
    alive: List[int] = []
    for pid in pids:
        try:
            os.kill(pid, 0)
            alive.append(pid)
        except ProcessLookupError:
            continue
        except Exception:
            continue
    return alive


def main() -> None:
    project_root = Path(__file__).resolve().parent.parent.parent

    patterns = [
        # Long-running orchestrator loop
        "scripts/ai_trader/orchestrator.py",
        "scripts/ai_trader/auto_loop.py",
        "scripts/ai_trader/cli.py",
        # Control/event consumers
        "scripts/ai_trader/control_event_consumer.py",
        # ML job scheduler
        "scripts/ai_trader/ml_scheduler.py",
        # Weekend supervisor
        "scripts/ai_trader/weekend_supervisor.py",
        # Heavy ML/HPO jobs spawned by Brain
        "alpha_loop.py",
        "tools/nightly_oof_flow.py",
        "hpo_cli_upgraded.py",
    ]

    all_pids: Dict[str, List[int]] = {}
    for pattern in patterns:
        pids = _pgrep(pattern)
        if pids:
            all_pids[pattern] = pids

    if not all_pids:
        print("No AI brain processes found matching known patterns.")
        return

    print("Found AI brain related processes:")
    for pattern, pids in all_pids.items():
        print(f"  {pattern}: {pids}")

    # First try graceful shutdown with SIGTERM
    term_pids: List[int] = sorted({pid for plist in all_pids.values() for pid in plist})
    if term_pids:
        print(f"\nSending SIGTERM to PIDs: {term_pids}")
        _kill_pids(term_pids, signal.SIGTERM)
        time.sleep(5.0)

    # Check which PIDs are still alive and force kill if needed
    remaining = _still_alive(term_pids)
    if remaining:
        print(f"\nPIDs still alive after SIGTERM, sending SIGKILL: {remaining}")
        _kill_pids(remaining, signal.SIGKILL)
        time.sleep(1.0)
        remaining = _still_alive(remaining)

    if remaining:
        print(f"\nWarning: some PIDs still appear alive: {remaining}")
    else:
        print("\nAll AI brain related processes have been stopped.")


if __name__ == "__main__":
    main()
