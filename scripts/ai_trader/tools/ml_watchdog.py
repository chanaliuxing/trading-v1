#!/usr/bin/env python3
from __future__ import annotations

"""
ML Watchdog (process + log activity)
------------------------------------
Monitors an ML process (e.g., alpha_loop) for stalls or memory blowups.

Features:
- Track process RSS (MB) against a limit; on breach → signal and optional restart
- Track log file growth; if unchanged for N minutes → consider stuck → signal/restart
- Works on macOS/Linux; uses psutil if available, else falls back to `ps` parsing

Usage examples:
  # Watch an alpha_loop run by PID, detect stall if log silent for 5 min
  PYTHONPATH=$PWD venv/bin/python -m scripts.ai_trader.tools.ml_watchdog \
    --pid 12345 \
    --log-file alpha_out/daily/run.log \
    --stuck-mins 5 --rss-limit-mb 9000

  # Watch by command pattern (pgrep -f), and auto-restart with a safe wrapper
  PYTHONPATH=$PWD venv/bin/python -m scripts.ai_trader.tools.ml_watchdog \
    --match "alpha_loop.py --config alpha_config_enhanced.json" \
    --log-file alpha_out/daily/run.log \
    --rss-limit-mb 10000 --stuck-mins 6 \
    --restart-cmd "venv/bin/python alpha_loop.py --config alpha_config_enhanced.json --gens 1 --provider optuna --out_dir alpha_out/daily"
"""

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional


def _now_str() -> str:
    import datetime as _dt
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _find_pid_by_match(pattern: str) -> Optional[int]:
    try:
        out = subprocess.check_output(["pgrep", "-f", pattern], text=True)
        for line in out.splitlines():
            try:
                pid = int(line.strip())
                if pid != 0:
                    return pid
            except Exception:
                continue
    except Exception:
        return None
    return None


def _rss_mb(pid: int) -> float:
    try:
        import psutil  # type: ignore
        p = psutil.Process(pid)
        return float(p.memory_info().rss) / 1024.0 / 1024.0
    except Exception:
        pass
    # Fallback to ps (portable on macOS/Linux)
    try:
        out = subprocess.check_output(["ps", "-o", "rss=", "-p", str(pid)], text=True)
        kb = float(out.strip() or 0.0)
        return kb / 1024.0
    except Exception:
        return 0.0


def _send_signal(pid: int, sig: str = "TERM") -> None:
    sigmap = {
        "TERM": signal.SIGTERM,
        "INT": signal.SIGINT,
        "KILL": signal.SIGKILL,
        "HUP": signal.SIGHUP,
    }
    s = sigmap.get(sig.upper(), signal.SIGTERM)
    try:
        os.kill(pid, s)
    except ProcessLookupError:
        pass


def _is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except Exception:
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="ML watchdog for alpha_loop/auto_loop")
    ap.add_argument("--pid", type=int, default=0, help="PID of target process")
    ap.add_argument("--match", type=str, default="", help="pgrep -f pattern to find PID if --pid not set")
    ap.add_argument("--log-file", type=str, default="", help="Log file to monitor for growth (optional)")
    ap.add_argument("--interval-sec", type=int, default=60, help="Watch interval seconds")
    ap.add_argument("--stuck-mins", type=int, default=10, help="Minutes of no log growth → considered stuck")
    ap.add_argument("--rss-limit-mb", type=float, default=12000.0, help="RSS MB limit to trigger action")
    ap.add_argument("--signal", type=str, default="TERM", help="Signal to send on breach: TERM|INT|HUP|KILL")
    ap.add_argument("--restart-cmd", type=str, default="", help="Shell command to restart process after KILL (optional)")
    ap.add_argument("--cooldown-sec", type=int, default=120, help="Cooldown between actions seconds")
    ap.add_argument("--max-restarts", type=int, default=3, help="Max restarts in one watchdog session")

    args = ap.parse_args()

    log_path = Path(args.log_file) if args.log_file else None
    last_log_mtime = 0.0
    last_log_size = -1
    last_change_ts = time.time()
    restarts = 0

    print(f"[{_now_str()}] [watchdog] started; interval={args.interval_sec}s, stuck={args.stuck_mins}m, rss_limit={args.rss_limit_mb}MB")
    while True:
        # Resolve PID
        pid = args.pid
        if pid <= 0 and args.match:
            pid = _find_pid_by_match(args.match) or 0
        if pid <= 0:
            print(f"[{_now_str()}] [watchdog] no PID yet (match='{args.match}'), sleeping…")
            time.sleep(args.interval_sec)
            continue

        # Check running
        if not _is_running(pid):
            print(f"[{_now_str()}] [watchdog] pid {pid} not running")
            # restart if requested
            if args.restart_cmd and restarts < args.max_restarts:
                try:
                    print(f"[{_now_str()}] [watchdog] restarting: {args.restart_cmd}")
                    subprocess.Popen(args.restart_cmd, shell=True, cwd=os.getcwd(), env=os.environ.copy())
                    restarts += 1
                except Exception as e:
                    print(f"[{_now_str()}] [watchdog] restart failed: {e}")
            time.sleep(args.interval_sec)
            continue

        # Check log growth
        stuck = False
        if log_path and log_path.exists():
            st = log_path.stat()
            if st.st_size != last_log_size or st.st_mtime != last_log_mtime:
                last_log_size = st.st_size
                last_log_mtime = st.st_mtime
                last_change_ts = time.time()
            else:
                silence = (time.time() - last_change_ts) / 60.0
                if silence >= args.stuck_mins:
                    stuck = True
        elif log_path:
            # If expecting a log but not yet created, treat as silence window
            silence = (time.time() - last_change_ts) / 60.0
            if silence >= args.stuck_mins:
                stuck = True

        # Check RSS
        rss = _rss_mb(pid)
        rss_breach = rss >= args.rss_limit_mb

        if stuck or rss_breach:
            reason = []
            if stuck:
                reason.append("stuck")
            if rss_breach:
                reason.append(f"rss={rss:.0f}MB >= {args.rss_limit_mb}")
            print(f"[{_now_str()}] [watchdog] breach: pid={pid} {' & '.join(reason)}; sending {args.signal}")
            _send_signal(pid, args.signal)
            time.sleep(args.cooldown_sec)
            # If still alive and restart allowed, escalate to KILL then restart
            if _is_running(pid) and args.restart_cmd:
                print(f"[{_now_str()}] [watchdog] still alive; sending KILL and restarting")
                _send_signal(pid, "KILL")
                time.sleep(1)
                if restarts < args.max_restarts:
                    try:
                        subprocess.Popen(args.restart_cmd, shell=True, cwd=os.getcwd(), env=os.environ.copy())
                        restarts += 1
                        last_change_ts = time.time()
                        last_log_mtime = 0.0
                        last_log_size = -1
                    except Exception as e:
                        print(f"[{_now_str()}] [watchdog] restart failed: {e}")

        time.sleep(args.interval_sec)

    return 0


if __name__ == "__main__":
    sys.exit(main())

