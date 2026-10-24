import time
import datetime
import sys as _sys
from pathlib import Path
import os

# Ensure project root is on sys.path so that "scripts.*" and "libs.*" imports work
_THIS_FILE = Path(__file__).resolve()
_PROJECT_ROOT = _THIS_FILE.parent.parent.parent
if str(_PROJECT_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_PROJECT_ROOT))

from scripts.ai_trader.orchestrator import Orchestrator
from libs.market_calendar import is_holiday
from libs.config import config as _cfg
from libs.skills.runtime import append_skill_flow_event
import subprocess
import signal


def _get_project_env():
    """Get environment dict with PYTHONPATH set to project root.

    Also sets OpenMP/PyTorch threading limits to prevent LSTM crashes.
    """
    project_root = Path(__file__).resolve().parent.parent.parent
    env = os.environ.copy()
    env["PYTHONPATH"] = str(project_root)
    # Fix PyTorch LSTM OpenMP threading crashes
    env["OMP_NUM_THREADS"] = "4"
    env["MKL_NUM_THREADS"] = "4"
    env["PYTORCH_NUM_THREADS"] = "4"
    env["OMP_NESTED"] = "FALSE"
    env["OMP_WAIT_POLICY"] = "PASSIVE"
    return env, str(project_root)


def _reap_zombies(signum, frame):
    """Signal handler to reap zombie processes asynchronously."""
    try:
        while True:
            # WNOHANG: return immediately if no child has exited
            pid, status = os.waitpid(-1, os.WNOHANG)
            if pid == 0:
                break
    except ChildProcessError:
        pass
    except Exception:
        pass


def _ensure_ml_scheduler_running():
    """Ensure ml_scheduler is running; respawn if pid missing or dead."""
    try:
        from pathlib import Path as _P
        import os as _os
        import signal as _sig
        import datetime as _dt
        # Resolve pid file
        dstr = _dt.date.today().isoformat()
        runs = Path(Orchestrator().runs_dir)
        pf = runs / dstr / "pids" / "ml_scheduler.pid"
        alive = False
        if pf.exists():
            try:
                pid = int((pf.read_text("utf-8")).strip())
                # Check liveness
                _os.kill(pid, 0)
                alive = True
            except Exception:
                alive = False
        if not alive:
            env, cwd = _get_project_env()
            subprocess.Popen([_sys.executable, "scripts/ai_trader/ml_scheduler.py"], start_new_session=True, cwd=cwd, env=env)
            print("[auto_loop] ml_scheduler (re)started")
    except Exception as e:
        print(f"[auto_loop] ensure ml_scheduler failed: {e}")


def _ensure_skill_approver_running():
    """Ensure skill_approver daemon is running; respawn if pid missing or dead."""
    try:
        from pathlib import Path as _P
        import os as _os
        import signal as _sig
        import datetime as _dt
        # Resolve pid file
        dstr = _dt.date.today().isoformat()
        runs = Path(Orchestrator().runs_dir)
        # Ensure pids dir exists
        (runs / dstr / "pids").mkdir(parents=True, exist_ok=True)
        pf = runs / dstr / "pids" / "skill_approver.pid"
        alive = False
        if pf.exists():
            try:
                pid = int((pf.read_text("utf-8")).strip())
                # Check liveness
                _os.kill(pid, 0)
                alive = True
            except Exception:
                alive = False
        if not alive:
            env, cwd = _get_project_env()
            # Start with auto-register enabled to fully automate the pipeline
            # Note: We do NOT enable --enable-on-pass by default for safety,
            # unless/until backtest gates are added.
            cmd = [_sys.executable, "scripts/ai_trader/evolvers/skill_approver.py", "--auto-register"]
            # We need to capture the PID, so we can't just fire-and-forget without tracking
            # But for simplicity here, we rely on skill_approver.py to write its own PID if needed,
            # OR we write it here. Let's write it here to be safe.
            proc = subprocess.Popen(cmd, start_new_session=True, cwd=cwd, env=env)
            pf.write_text(str(proc.pid), encoding="utf-8")
            print(f"[auto_loop] skill_approver (re)started (pid={proc.pid})")
    except Exception as e:
        print(f"[auto_loop] ensure skill_approver failed: {e}")


def _ensure_skill_approver_running():
    """Ensure skill_approver daemon is running; respawn if pid missing or dead."""
    try:
        from pathlib import Path as _P
        import os as _os
        import signal as _sig
        import datetime as _dt

        dstr = _dt.date.today().isoformat()
        runs = Path(Orchestrator().runs_dir)
        pf = runs / dstr / "pids" / "skill_approver.pid"
        alive = False
        if pf.exists():
            try:
                pid = int((pf.read_text("utf-8")).strip())
                _os.kill(pid, 0)
                alive = True
            except Exception:
                alive = False
        if not alive:
            env, cwd = _get_project_env()
            proc = subprocess.Popen(
                [
                    _sys.executable,
                    "scripts/ai_trader/evolvers/skill_approver.py",
                    "--auto-register",
                    "--enable-on-pass",
                    "--require-backtest",
                    "--min-backtest-ev",
                    "0.0",
                    "--min-backtest-sharpe",
                    "0.2",
                ],
                start_new_session=True,
                cwd=cwd,
                env=env,
            )
            pf.parent.mkdir(parents=True, exist_ok=True)
            pf.write_text(str(proc.pid))
            print("[auto_loop] skill_approver (re)started")
    except Exception as e:
        print(f"[auto_loop] ensure skill_approver failed: {e}")


def _ny_now():
    import pytz
    tz = pytz.timezone('America/New_York')
    return datetime.datetime.now(tz)


def _route_session(now: datetime.datetime) -> str:
    wd = now.weekday()  # 0=Mon..6=Sun
    if wd >= 5 or is_holiday(now.date()):
        return "weekend"
    hm = now.strftime("%H:%M")
    if "07:00" <= hm < "09:30":
        return "morning"
    if "09:30" <= hm < "16:00":
        return "intraday"
    if "16:00" <= hm < "20:00":
        return "afterhours"
    return "overnight"


def run_continuous():
    print("[auto_loop] Starting AI brain...")
    # Register signal handler for zombie reaping
    signal.signal(signal.SIGCHLD, _reap_zombies)
    
    print(f"[auto_loop] Current time: {_ny_now().strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"[auto_loop] Session will be: {_route_session(_ny_now())}")
    print("[auto_loop] Creating Orchestrator instance...")
    import sys
    sys.stdout.flush()
    o = Orchestrator()

    # Cleanup stale process locks from previous crashes to avoid startup blocks
    try:
        import datetime as _dt
        from pathlib import Path as _P
        dstr = _dt.date.today().isoformat()
        locks_dir = _P(o.runs_dir) / dstr / "locks"
        pids_dir = _P(o.runs_dir) / dstr / "pids"
        to_remove = [
            "control_consumer.lock",
            "ml_slot.lock",
            "weekend_supervisor.lock",
        ]
        if locks_dir.exists():
            for name in to_remove:
                fp = locks_dir / name
                if fp.exists():
                    try:
                        fp.unlink()
                        print(f"[auto_loop] removed stale lock: {fp}")
                    except Exception:
                        pass
        # Clear stale pid files
        if pids_dir.exists():
            for fp in pids_dir.glob("*.pid"):
                try:
                    fp.unlink()
                    print(f"[auto_loop] removed stale pid: {fp}")
                except Exception:
                    pass
    except Exception as e:
        print(f"[auto_loop] lock cleanup skipped due to error: {e}")
    print("[auto_loop] Orchestrator initialized, entering main loop...")
    sys.stdout.flush()
    # Ensure the control-event consumer is running (idempotent via internal lock)
    try:
        env, cwd = _get_project_env()
        subprocess.Popen([_sys.executable, "scripts/ai_trader/control_event_consumer.py"],
                        start_new_session=True, cwd=cwd, env=env)
        # Ensure ML scheduler is running to process queue_job actions
        subprocess.Popen([_sys.executable, "scripts/ai_trader/ml_scheduler.py"],
                        start_new_session=True, cwd=cwd, env=env)
        # Ensure skill approver daemon is running for queued skills
        _ensure_skill_approver_running()
    except Exception:
        pass
    print("[auto_loop] Entering while loop...")
    sys.stdout.flush()
    while True:
        try:
            print("[auto_loop] Loop iteration starting...")
            sys.stdout.flush()
            print("[auto_loop] Getting NY time...")
            sys.stdout.flush()
            now = _ny_now()
            try:
                if hasattr(o, "skill_runtime"):
                    o.skill_runtime.refresh()
            except Exception:
                pass
            sess = _route_session(now)
            print(f"[auto_loop] {now.strftime('%Y-%m-%d %H:%M:%S %Z')} - Session: {sess} (weekday={now.weekday()})")
            sys.stdout.flush()
            if sess == "weekend":
                # Weekend: ensure weekend supervisor is running; do not start trading
                print(f"[auto_loop] WEEKEND DETECTED - Not starting trading.")
                sys.stdout.flush()
                try:
                    # Launch a supervisor loop if not already running (pidfile-based)
                    env, cwd = _get_project_env()
                    subprocess.Popen([_sys.executable, "scripts/ai_trader/weekend_supervisor.py", "--interval-sec", "300"],
                                    start_new_session=True, cwd=cwd, env=env)
                except Exception:
                    pass
                # Also schedule OOF generation flow periodically on weekends
                _maybe_run_oof_flow(o, now, apply=False)
                # Ensure ml_scheduler kept alive even on weekend
                _ensure_ml_scheduler_running()
                _ensure_skill_approver_running()
                time.sleep(_get_sleep_secs("weekend"))
                continue
            if sess == "morning":
                print(f"[auto_loop] Starting MORNING sequence...")
                o.write_plan("morning", o.morning_sequence())
            elif sess == "intraday":
                print(f"[auto_loop] Starting INTRADAY sequence...")
                o.write_plan("intraday", o.intraday_sequence(mode="slow"))
            elif sess == "afterhours":
                print(f"[auto_loop] Starting AFTERHOURS sequence...")
                o.write_plan("afterhours", o.afterhours_sequence())
                # Trigger evening learning once at 16:20 ET (daily lock)
                if _should_trigger_evening_learning(o, now):
                    _trigger_evening_learning(o, now)
                # Trigger ML backfill at ~16:20 ET (daily lock)
                if _should_trigger_ml_backfill(o, now):
                    _trigger_ml_backfill(o, now)
                # Active vs bars completeness check (~16:40 ET)
                if _should_run_active_dq(o, now):
                    _run_active_dq(o, now)
                # Labels recompute (~16:30 ET)
                if _should_trigger_labels_recompute(o, now):
                    _trigger_labels_recompute(o, now)
                if _should_trigger_research_cycle(o, now):
                    _trigger_research_cycle(o, now)
                if _should_trigger_sleep_loop(o, now):
                    _trigger_sleep_loop(o, now)

                # Run OOF flow once per TTL window
                _maybe_run_oof_flow(o, now, apply=False)
                # Trigger recon-all once per day after configured time (default 16:10 ET)
                if _should_trigger_recon(o, now):
                    _trigger_recon_all(now)
            else:
                print(f"[auto_loop] Starting OVERNIGHT sequence...")
                o.write_plan("overnight", o.overnight_sequence())
                # Prefer training overnight; safe to apply in paper if enabled
                _maybe_run_oof_flow(o, now, apply=False)
                # If recon not yet run (missed afterhours), still trigger during overnight
                if _should_trigger_recon(o, now):
                    _trigger_recon_all(now)
                # Ensure ml_scheduler kept alive
                _ensure_ml_scheduler_running()
                _ensure_skill_approver_running()
                if _should_trigger_research_cycle(o, now):
                    _trigger_research_cycle(o, now)
                if _should_trigger_sleep_loop(o, now):
                    _trigger_sleep_loop(o, now)
        except Exception as e:
            # Never let one bad iteration kill the loop
            try:
                import traceback
                print(f"[auto_loop] ERROR in loop: {e}")
                traceback.print_exc()
            except Exception:
                pass
        finally:
            # Sleep based on session (fallback to weekend if sess missing)
            try:
                time.sleep(_get_sleep_secs(sess))
            except Exception:
                time.sleep(_get_sleep_secs("weekend"))


def _should_trigger_evening_learning(o: Orchestrator, now: datetime.datetime) -> bool:
    """Check if we should trigger evening learning at 16:20 ET.

    Rules:
    1. Must be >= earliest_afterhours_start_et (default 16:20:00)
    2. Must not have run today (daily lock)
    3. Respects min_interval_sec TTL (fallback)

    Returns:
        True if should trigger, False otherwise
    """
    try:
        # Get learning config
        from libs.config import config as _cfg
        learning_cfg = _cfg.get("policies", {}).get("learning", {})
        earliest_time_str = learning_cfg.get("earliest_afterhours_start_et", "16:20:00")
        min_interval_sec = learning_cfg.get("min_interval_sec", 3600)

        # Parse earliest time
        import datetime as dt
        earliest_hour, earliest_min, earliest_sec = map(int, earliest_time_str.split(":"))
        earliest_time = dt.time(earliest_hour, earliest_min, earliest_sec)

        # Check if current time >= earliest time
        if now.time() < earliest_time:
            return False

        # Check daily lock
        runs = Path(o.runs_dir) if hasattr(o, "runs_dir") else Path("runs/ai_trader")
        dstr = now.date().isoformat()
        locks = runs / dstr / "locks"
        locks.mkdir(parents=True, exist_ok=True)
        lock = locks / "learn_afterhours.lock"

        ts = int(now.timestamp())

        if lock.exists():
            # Check TTL (fallback)
            age = ts - int(lock.stat().st_mtime)
            if age < min_interval_sec:
                return False

        # All checks passed - should trigger
        return True
    except Exception as e:
        print(f"[auto_loop] Error checking evening learning trigger: {e}")
        return False


def _trigger_evening_learning(o: Orchestrator, now: datetime.datetime) -> None:
    """Trigger evening learning once per day at 16:20 ET.

    Steps:
    1. Call o.evening_learning()
    2. Save result to runs/ai_trader/YYYY-MM-DD/learn_afterhours.json
    3. Write lock to runs/ai_trader/YYYY-MM-DD/locks/learn_afterhours.lock
    """
    import json

    try:
        runs = Path(o.runs_dir) if hasattr(o, "runs_dir") else Path("runs/ai_trader")
        dstr = now.date().isoformat()
        day_dir = runs / dstr
        day_dir.mkdir(parents=True, exist_ok=True)
        locks = day_dir / "locks"
        locks.mkdir(parents=True, exist_ok=True)

        print(f"[auto_loop] Triggering evening learning at {now.strftime('%H:%M:%S')} ET...")

        # Call evening_learning (HPO, RL, prompt evolution)
        result = o.evening_learning()

        # Save result
        result_file = day_dir / "learn_afterhours.json"
        with open(result_file, "w") as f:
            json.dump({
                "ts": now.isoformat(),
                "status": "completed",
                "result": result
            }, f, indent=2)

        print(f"[auto_loop] Evening learning completed. Result saved to {result_file}")

        # Write lock (daily lock + TTL fallback)
        lock = locks / "learn_afterhours.lock"
        lock.write_text(str(int(now.timestamp())))

        print(f"[auto_loop] Lock written to {lock}")

    except Exception as e:
        print(f"[auto_loop] Error triggering evening learning: {e}")
        import traceback
        traceback.print_exc()


def _should_trigger_recon(o: Orchestrator, now: datetime.datetime) -> bool:
    """Check whether daily recon-all should run (time-gated, daily lock).

    Config in config.yml:
      policies.recon.enabled (default True)
      policies.recon.run_time_et (default "16:10:00")
      policies.recon.min_interval_sec (default 21600 = 6 hours)

    Returns:
        True if recon should be triggered, False otherwise
    """
    try:
        pol = _cfg.get("policies", {}) or {}
        rc = pol.get("recon", {}) or {}

        # Check if recon is enabled
        if not bool(rc.get("enabled", True)):
            print("[auto_loop] Recon disabled in config")
            return False

        # Check time gate
        rt = str(rc.get("run_time_et", "16:10:00"))
        hh, mm, ss = [int(x) for x in rt.split(":")]
        target = datetime.time(hh, mm, ss)
        if now.time() < target:
            # Before configured run time, don't trigger
            return False

        # Check interval lock
        min_iv = int(rc.get("min_interval_sec", 6 * 3600) or 21600)
        runs = Path(o.runs_dir) if hasattr(o, "runs_dir") else Path("runs/ai_trader")
        dstr = now.date().isoformat()
        locks = runs / dstr / "locks"
        locks.mkdir(parents=True, exist_ok=True)
        lock = locks / "recon_daily.lock"
        ts = int(now.timestamp())

        if lock.exists():
            age = ts - int(lock.stat().st_mtime)
            if age < min_iv:
                # Lock exists and not expired
                print(f"[auto_loop] Recon skipped: lock age={age}s, min_interval={min_iv}s")
                return False
            else:
                # Lock expired
                print(f"[auto_loop] Recon lock expired: age={age}s > min_interval={min_iv}s")
                return True
        else:
            # No lock, can trigger
            print(f"[auto_loop] Recon lock not found: {lock}")
            return True

    except Exception as e:
        print(f"[auto_loop] Error checking recon trigger: {e}")
        import traceback
        traceback.print_exc()
        return False


def _trigger_recon_all(now: datetime.datetime) -> None:
    """Fire-and-forget `make recon-all` using project venv env and cwd.

    IMPORTANT: Write lock file BEFORE launching recon to prevent race conditions.
    """
    try:
        # Write lock FIRST to prevent duplicate triggers
        dstr = now.date().isoformat()
        day_dir = Path("runs/ai_trader") / dstr
        lock_dir = day_dir / "locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_file = lock_dir / "recon_daily.lock"

        ts = int(now.timestamp())
        lock_file.write_text(str(ts))
        print(f"[auto_loop] Recon lock written: {lock_file} (ts={ts})")

        # Then trigger recon-all
        env, cwd = _get_project_env()
        print("[auto_loop] Triggering recon-all (Makefile)...")
        subprocess.Popen(["make", "recon-all"], start_new_session=True, cwd=cwd, env=env)

    except Exception as e:
        print(f"[auto_loop] Error launching recon-all: {e}")
        import traceback
        traceback.print_exc()

def _maybe_run_oof_flow(o: Orchestrator, now: datetime.datetime, apply: bool = False) -> None:
    """Run nightly OOF flow once per TTL in off-hours.

    - TTL lock at runs/YYYY-MM-DD/locks/nightly_oof.lock (15 min)
    - Symbol set from current strategy config (enabled symbols); fallback to universe
    - Range: last 30 calendar days → now
    """
    try:
        runs = Path(o.runs_dir) if hasattr(o, "runs_dir") else Path("runs/ai_trader")
        dstr = now.date().isoformat()
        locks = runs / dstr / "locks"
        locks.mkdir(parents=True, exist_ok=True)
        lock = locks / "nightly_oof.lock"
        ttl = 15 * 60  # 15 minutes
        ts = int(now.timestamp())
        if lock.exists():
            age = ts - int(lock.stat().st_mtime)
            if age < ttl:
                return
        # Touch lock
        lock.write_text(str(ts))

        # Symbols from config
        try:
            syms = o._strategy_symbols_from_config()
        except Exception:
            syms = []
        if not syms:
            # fallback: use universe from config.yml if present
            try:
                from libs.config import config as _cfg
                syms = [str(s) for s in (_cfg.universe or [])]
            except Exception:
                syms = []
        if not syms:
            return

        # Compute default window (last 30 calendar days)
        default_window_days = 30

        # Check for extend_window hints from ML brain (P1)
        try:
            hints_file = runs / dstr / "hpo_hints.json"
            if hints_file.exists():
                import json
                hints = json.loads(hints_file.read_text("utf-8"))
                suggested_window = hints.get("window_days")
                if suggested_window and isinstance(suggested_window, int):
                    # Validate against whitelist
                    if suggested_window in [21, 30, 45, 60]:
                        print(f"[auto_loop] OOF: Using hinted window_days={suggested_window} (from ML brain)")
                        default_window_days = suggested_window
                    else:
                        print(f"[auto_loop] OOF: Ignoring invalid window_days={suggested_window} (not in whitelist)")
        except Exception as e:
            print(f"[auto_loop] OOF: Failed to read hpo_hints.json: {e}")

        start_dt = now - datetime.timedelta(days=default_window_days)
        start = start_dt.strftime('%Y-%m-%d 09:30:00')
        end = now.strftime('%Y-%m-%d %H:%M:%S')
        cmd = [
            _sys.executable, "tools/nightly_oof_flow.py",
            "--symbols", ",".join(syms),
            "--start", start, "--end", end,
        ]
        if apply:
            cmd.append("--apply")
        print(f"[auto_loop] Trigger OOF flow: {' '.join(cmd)}")
        env, cwd = _get_project_env()
        subprocess.Popen(cmd, start_new_session=True, cwd=cwd, env=env)

        # Clear hints after OOF is triggered (P1)
        try:
            hints_file = runs / dstr / "hpo_hints.json"
            if hints_file.exists():
                # Archive instead of delete for audit trail
                archive = hints_file.with_suffix('.json.used')
                hints_file.rename(archive)
                print(f"[auto_loop] OOF: Archived hpo_hints.json → {archive.name}")
        except Exception as e:
            print(f"[auto_loop] OOF: Failed to archive hpo_hints.json: {e}")
    except Exception as e:
        print(f"[auto_loop] OOF flow trigger failed: {e}")


def _get_sleep_secs(sess: str) -> int:
    """Compute per-session sleep seconds from config with sane defaults.

    Defaults (if config missing):
      weekend: 600s; morning: 600s; intraday: 300s; afterhours: 900s; overnight: 900s
    """
    try:
        pol = _cfg.get("policies", {}) or {}
        mp = (pol.get("loop_sleep_sec") or {})
        if sess in mp:
            return int(mp.get(sess) or 0) or _default_sleep(sess)
        return _default_sleep(sess)
    except Exception:
        return _default_sleep(sess)


def _default_sleep(sess: str) -> int:
    defaults = {
        "weekend": 600,
        "morning": 600,
        "intraday": 300,
        "afterhours": 900,
        "overnight": 900,
    }
    return defaults.get(sess, 600)


def _should_trigger_ml_backfill(o: Orchestrator, now: datetime.datetime) -> bool:
    """Check whether to trigger daily ML data backfill around 16:20 ET.

    Config in config.yml (optional):
      policies.backfill_ml.enabled: true
      policies.backfill_ml.run_time_et: "16:20:00"
      policies.backfill_ml.min_interval_sec: 21600

    Returns True if backfill should run (time reached and TTL passed).
    """
    try:
        pol = _cfg.get("policies", {}) or {}
        bc = pol.get("backfill_ml", {}) or {}
        if not bool(bc.get("enabled", True)):
            return False
        rt = str(bc.get("run_time_et", "16:20:00"))
        hh, mm, ss = [int(x) for x in rt.split(":")]
        target = datetime.time(hh, mm, ss)
        if now.time() < target:
            return False
        min_iv = int(bc.get("min_interval_sec", 6 * 3600) or 21600)
        runs = Path(o.runs_dir) if hasattr(o, "runs_dir") else Path("runs/ai_trader")
        dstr = now.date().isoformat()
        locks = runs / dstr / "locks"
        locks.mkdir(parents=True, exist_ok=True)
        lock = locks / "ml_backfill_daily.lock"
        ts = int(now.timestamp())
        if lock.exists():
            age = ts - int(lock.stat().st_mtime)
            if age < min_iv:
                return False
        return True
    except Exception as e:
        print(f"[auto_loop] Error checking ml backfill trigger: {e}")
        return False


def _trigger_ml_backfill(o: Orchestrator, now: datetime.datetime) -> None:
    """Trigger ML data backfill (1m) for today's date and active symbols.

    Uses docs/daily/trigger_backfill.py to emit Kafka requests.
    """
    try:
        # Determine symbol list
        try:
            syms = o._strategy_symbols_from_config()
        except Exception:
            syms = []
        if not syms:
            from libs.config import config as _cfg
            syms = [str(s) for s in (_cfg.universe or [])]
        if not syms:
            print("[auto_loop] ML backfill: no symbols, skipping")
            return

        date_str = now.strftime('%Y-%m-%d')
        cmd = [
            _sys.executable, "docs/daily/trigger_backfill.py",
            "--date", date_str,
            "--bar-size", "1m",
            "--symbols", *syms
        ]
        print("[auto_loop] ML backfill:", " ".join(cmd))
        env, cwd = _get_project_env()
        subprocess.Popen(cmd, start_new_session=True, cwd=cwd, env=env)

        # Write lock
        runs = Path(o.runs_dir) if hasattr(o, "runs_dir") else Path("runs/ai_trader")
        dstr = now.date().isoformat()
        locks = runs / dstr / "locks"
        locks.mkdir(parents=True, exist_ok=True)
        (locks / "ml_backfill_daily.lock").write_text(str(int(now.timestamp())))
    except Exception as e:
        print(f"[auto_loop] ML backfill trigger failed: {e}")


def _should_run_active_dq(o: Orchestrator, now: datetime.datetime) -> bool:
    """Should run active vs bars completeness check (~16:40 ET)?"""
    try:
        pol = _cfg.get("policies", {}) or {}
        dq = (pol.get("dq") or {})
        checks = (dq.get("checks") or {})
        if not bool(checks.get("active_vs_bars", True)):
            return False
        # Default check time
        rt = str(checks.get("active_dq_et", "16:40:00"))
        hh, mm, ss = [int(x) for x in rt.split(":")]
        target = datetime.time(hh, mm, ss)
        if now.time() < target:
            return False
        # Daily lock
        runs = Path(o.runs_dir) if hasattr(o, "runs_dir") else Path("runs/ai_trader")
        dstr = now.date().isoformat()
        locks = runs / dstr / "locks"
        locks.mkdir(parents=True, exist_ok=True)
        lock = locks / "active_dq_daily.lock"
        ts = int(now.timestamp())
        if lock.exists():
            age = ts - int(lock.stat().st_mtime)
            if age < 6 * 3600:
                return False
        return True
    except Exception as e:
        print(f"[auto_loop] Error checking active DQ trigger: {e}")
        return False


def _run_active_dq(o: Orchestrator, now: datetime.datetime) -> None:
    """Run active vs bars completeness check; if below threshold, retrigger backfill."""
    try:
        from libs.config import config as _cfg2
        min_cov = float(((_cfg2.get("policies") or {}).get("dq") or {}).get("min_active_1m_coverage_pct", 0.98))
        # Build command
        syms = []
        try:
            syms = o._strategy_symbols_from_config()
        except Exception:
            pass
        sym_arg = ",".join(syms) if syms else ""
        date_str = now.strftime('%Y-%m-%d')
        cmd = [_sys.executable, "tools/check_active_completeness.py", "--date", date_str, "--min-coverage", str(min_cov)]
        if sym_arg:
            cmd += ["--symbols", sym_arg]
        print("[auto_loop] Active DQ:", " ".join(cmd))
        env, cwd = _get_project_env()
        p = subprocess.run(cmd, cwd=cwd, env=env, text=True, capture_output=True)
        # Write JSON output for audit
        try:
            runs = Path(o.runs_dir) if hasattr(o, "runs_dir") else Path("runs/ai_trader")
            dstr = now.date().isoformat()
            d = runs / dstr
            d.mkdir(parents=True, exist_ok=True)
            (d / "active_dq.json").write_text(p.stdout or "{}", encoding="utf-8")
        except Exception:
            pass
        # If non-zero (bad coverage), retrigger ML backfill once more
        if p.returncode != 0:
            print("[auto_loop] Active DQ: coverage below threshold, triggering additional backfill")
            _trigger_ml_backfill(o, now)
        # Lock
        runs = Path(o.runs_dir) if hasattr(o, "runs_dir") else Path("runs/ai_trader")
        dstr = now.date().isoformat()
        (runs / dstr / "locks").mkdir(parents=True, exist_ok=True)
        (runs / dstr / "locks" / "active_dq_daily.lock").write_text(str(int(now.timestamp())))
    except Exception as e:
        print(f"[auto_loop] Active DQ run failed: {e}")


def _should_trigger_labels_recompute(o: Orchestrator, now: datetime.datetime) -> bool:
    """Check whether to trigger labels recompute (~16:30 ET)."""
    try:
        pol = _cfg.get("policies", {}) or {}
        dq = (pol.get("dq") or {})
        checks = (dq.get("checks") or {})
        rt = str(checks.get("labels_recompute_et", "16:30:00"))
        hh, mm, ss = [int(x) for x in rt.split(":")]
        target = datetime.time(hh, mm, ss)
        if now.time() < target:
            return False
        runs = Path(o.runs_dir) if hasattr(o, "runs_dir") else Path("runs/ai_trader")
        dstr = now.date().isoformat()
        locks = runs / dstr / "locks"
        locks.mkdir(parents=True, exist_ok=True)
        lock = locks / "labels_recompute_daily.lock"
        ts = int(now.timestamp())
        if lock.exists():
            age = ts - int(lock.stat().st_mtime)
            if age < 6 * 3600:
                return False
        return True
    except Exception as e:
        print(f"[auto_loop] Error checking labels recompute trigger: {e}")
        return False


def _trigger_labels_recompute(o: Orchestrator, now: datetime.datetime) -> None:
    """Recompute labels over today's session for active symbols using active table."""
    try:
        # Determine symbols
        try:
            syms = o._strategy_symbols_from_config()
        except Exception:
            syms = []
        if not syms:
            from libs.config import config as _cfg2
            syms = [str(s) for s in (_cfg2.universe or [])]
        if not syms:
            print("[auto_loop] Labels recompute: no symbols, skipping")
            return

        date_str = now.strftime('%Y-%m-%d')
        start = f"{date_str} 09:30:00"
        end = f"{date_str} 16:00:00"
        # Parameters from ML policy store (fallbacks)
        try:
            from tools.ml_policy_store import load_policies
            pol = load_policies()
            horizon_min = int(pol.get("horizon_min", 5))
            label_bps = float(pol.get("label_bps", 10.0))
        except Exception:
            horizon_min = 5
            label_bps = 10.0

        cmd = [
            _sys.executable, "tools/recompute_labels_1m.py",
            "--symbols", ",".join(syms),
            "--start", start, "--end", end,
            "--horizon-min", str(horizon_min),
            "--label-bps", str(label_bps)
        ]
        print("[auto_loop] Labels recompute:", " ".join(cmd))
        env, cwd = _get_project_env()
        subprocess.Popen(cmd, start_new_session=True, cwd=cwd, env=env)

        runs = Path(o.runs_dir) if hasattr(o, "runs_dir") else Path("runs/ai_trader")
        dstr = now.date().isoformat()
        (runs / dstr / "locks").mkdir(parents=True, exist_ok=True)
        (runs / dstr / "locks" / "labels_recompute_daily.lock").write_text(str(int(now.timestamp())))
    except Exception as e:
        print(f"[auto_loop] Labels recompute failed: {e}")


def _should_trigger_research_cycle(o: Orchestrator, now: datetime.datetime) -> bool:
    """Daily lock for research cycle."""
    try:
        pol = (_cfg.get("policies") or {}).get("research_cycle", {}) or {}
        run_time = str(pol.get("run_time_et", "16:45:00"))
        hh, mm, ss = [int(x) for x in run_time.split(":")]
        target = datetime.time(hh, mm, ss)
        # Allow overnight catch-up even if past midnight
        if now.time() < target and _route_session(now) != "overnight":
            return False
        min_iv = int(pol.get("min_interval_sec", 6 * 3600) or 21600)
        runs = Path(o.runs_dir) if hasattr(o, "runs_dir") else Path("runs/ai_trader")
        dstr = now.date().isoformat()
        locks = runs / dstr / "locks"
        locks.mkdir(parents=True, exist_ok=True)
        lock = locks / "research_cycle.lock"
        ts = int(now.timestamp())
        if lock.exists():
            age = ts - int(lock.stat().st_mtime)
            if age < min_iv:
                return False
        return True
    except Exception as e:
        print(f"[auto_loop] Error checking research cycle trigger: {e}")
        return False


def _trigger_research_cycle(o: Orchestrator, now: datetime.datetime) -> None:
    """Fire-and-forget research cycle run (afterhours/overnight)."""
    try:
        runs = Path(o.runs_dir) if hasattr(o, "runs_dir") else Path("runs/ai_trader")
        dstr = now.date().isoformat()
        locks = runs / dstr / "locks"
        locks.mkdir(parents=True, exist_ok=True)
        lock = locks / "research_cycle.lock"
        lock.write_text(str(int(now.timestamp())))

        env, cwd = _get_project_env()
        cmd = [_sys.executable, "scripts/ai_trader/run_research_cycle.py"]
        subprocess.Popen(cmd, start_new_session=True, cwd=cwd, env=env)
        append_skill_flow_event(
            {"event": "research_cycle_triggered", "ts": now.isoformat(), "session": _route_session(now)},
            runs_dir=runs,
        )
    except Exception as e:
        print(f"[auto_loop] Error launching research cycle: {e}")


def _should_trigger_sleep_loop(o: Orchestrator, now: datetime.datetime) -> bool:
    """Daily lock for memory sleep loop (EOD + 30m)."""
    try:
        pol = (_cfg.get("policies") or {}).get("sleep_loop", {}) or {}
        run_time = str(pol.get("run_time_et", "16:30:00"))
        hh, mm, ss = [int(x) for x in run_time.split(":")]
        target = datetime.time(hh, mm, ss)
        if now.time() < target and _route_session(now) != "overnight":
            return False
        min_iv = int(pol.get("min_interval_sec", 20 * 3600) or 72000)
        runs = Path(o.runs_dir) if hasattr(o, "runs_dir") else Path("runs/ai_trader")
        dstr = now.date().isoformat()
        locks = runs / dstr / "locks"
        locks.mkdir(parents=True, exist_ok=True)
        lock = locks / "sleep_loop.lock"
        ts = int(now.timestamp())
        if lock.exists():
            age = ts - int(lock.stat().st_mtime)
            if age < min_iv:
                return False
        return True
    except Exception as e:
        print(f"[auto_loop] Error checking sleep loop trigger: {e}")
        return False


def _trigger_sleep_loop(o: Orchestrator, now: datetime.datetime) -> None:
    """Launch nightly sleep loop with daily lock."""
    try:
        runs = Path(o.runs_dir) if hasattr(o, "runs_dir") else Path("runs/ai_trader")
        dstr = now.date().isoformat()
        locks = runs / dstr / "locks"
        locks.mkdir(parents=True, exist_ok=True)
        lock = locks / "sleep_loop.lock"
        lock.write_text(str(int(now.timestamp())))

        env, cwd = _get_project_env()
        cmd = [_sys.executable, "scripts/ai_trader/memory/sleep_loop.py"]
        subprocess.Popen(cmd, start_new_session=True, cwd=cwd, env=env)
        append_skill_flow_event(
            {"event": "sleep_loop_triggered", "ts": now.isoformat(), "session": _route_session(now)},
            runs_dir=runs,
        )
    except Exception as e:
        print(f"[auto_loop] Error launching sleep loop: {e}")


if __name__ == "__main__":
    run_continuous()
