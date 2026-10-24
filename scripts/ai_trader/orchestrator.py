import sys
from pathlib import Path
import subprocess

# Add project root to path to allow importing libs and scripts
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import datetime
import time
import statistics
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
import yaml
import pytz

from scripts.ai_trader.gates.health_gate import HealthGate
from scripts.ai_trader.gates.data_gate import DataGate
from scripts.ai_trader.gates.data_quality_gate import DataQualityGate
from scripts.ai_trader.gates.feature_freshness_gate import FeatureFreshnessGate
from scripts.ai_trader.gates.realtime_feature_freshness_gate import RealtimeFeatureFreshnessGate
from scripts.ai_trader.gates.exec_gate import ExecGate
from scripts.ai_trader.gates.llm_gate import LLMGate
from scripts.ai_trader.gates.realtime_gate import check as realtime_check
from scripts.ai_trader.controllers.run_system_controller import RunSystemController
from scripts.ai_trader.controllers.llm_trader_controller import LLMTraderController
from scripts.ai_trader.deploy.ab_deployer import ABDeployer
from scripts.ai_trader.reports.reporter import write_action_plan
from scripts.monitor_system import SystemMonitor
from scripts.ai_trader.brain.diagnostic_utils import simplify_label_health_for_brain
import json
import glob
from scripts.ai_trader.brain.brain import ControlBrain
from scripts.ai_trader.memory.aggregator import memory_summary
from scripts.ai_trader.learning.pipeline import LearningPipeline
from scripts.ai_trader.constants import MEMORY_LOOKBACK_DAYS, RL_MIN_SHARPE, RL_MAX_MDD, DECISION_FILES
from libs.logger import get_logger
from libs.db.clickhouse import ClickHouseDB
from libs import event_bus
from libs import metrics
from typing import List, Dict, Any, Optional
from libs.db.ch_pool import get_client as _get_ch_client
from libs.skills.runtime import SkillRuntime
from features.registry import get_default_registry


class Orchestrator:
    def __init__(self, cfg_path="scripts/ai_trader/config/ai_trader.yaml"):
        print("[Orchestrator] Loading config...")
        self.cfg_path = Path(cfg_path)
        self.cfg = yaml.safe_load(self.cfg_path.read_text())
        runtime_cfg_path = ((self.cfg.get("paths") or {}).get("runtime_config") or "").strip()
        if not runtime_cfg_path:
            default_ctrl = Path("scripts/ai_trader/config/ai_trader.yaml").resolve()
            if self.cfg_path.resolve() == default_ctrl:
                runtime_cfg_path = "config.yml"
            else:
                runtime_cfg_path = str(self.cfg_path.resolve().parent / "config.yml")
        self.learning = LearningPipeline(control_cfg=self.cfg, runtime_config_path=runtime_cfg_path)
        print("[Orchestrator] Config loaded, creating logger...")
        self.log = get_logger("ai_trader.orchestrator")
        self.ny_tz = pytz.timezone('America/New_York')
        # Initialize runs_dir early for lock cleanup
        self.runs_dir = Path(self.cfg["paths"]["runs_dir"])

        # ✅ Clean up stale locks on startup (handles SIGKILL, crashes, etc.)
        self._cleanup_stale_locks()

        print("[Orchestrator] Creating gates...")
        self.health = HealthGate(self.cfg["paths"]["health_report"])
        self.data = DataGate(self.cfg.get("clickhouse", {}), self.cfg.get("policies", {}).get("gates", {}).get("data_checks", {}))
        dq_cfg = (self.cfg.get("policies", {}).get("gates", {}).get("data_quality_checks", {}))
        self.dq_enabled = bool(dq_cfg.get("enabled", False))
        self.data_quality_gate = DataQualityGate(dq_cfg) if self.dq_enabled else None
        ff_cfg = (self.cfg.get("policies", {}).get("gates", {}).get("features_freshness", {}))
        self.ff_enabled = bool(ff_cfg.get("enabled", False))
        self.feature_freshness_gate = FeatureFreshnessGate(ff_cfg) if self.ff_enabled else None
        # Realtime feature freshness gate (for strategies using RealtimeFeatureEngine)
        rff_cfg = (self.cfg.get("policies", {}).get("gates", {}).get("realtime_feature_freshness", {}))
        self.rff_enabled = bool(rff_cfg.get("enabled", False))
        self.realtime_feature_freshness_gate = RealtimeFeatureFreshnessGate(rff_cfg) if self.rff_enabled else None
        self.exec = ExecGate()
        gates_cfg = self.cfg.get("policies", {}).get("gates", {})
        self.realtime_cfg = (gates_cfg.get("realtime") or {})
        self.llm_gate = LLMGate(
            deep_check=bool(gates_cfg.get("llm_deep_check", False)),
            deep_check_providers=gates_cfg.get("llm_deep_check_providers", None),
        )
        print("[Orchestrator] Gates created, creating RunSystemController...")
        self.rs = RunSystemController(self.cfg.get("commands", {}))
        # Modes (env overrides YAML): default paper; read from .env to avoid hardcoding
        import os as _os
        self.modes = self.cfg.get("modes", {}) or {}
        env_mode = (_os.getenv("ENV_MODE") or _os.getenv("TRADING_MODE") or "").strip().lower()
        if env_mode:
            self.paper_mode = (env_mode == "paper")
        else:
            self.paper_mode = bool(self.modes.get("paper", True))
        # Dynamic skill runtime (used for plan/decision hooks)
        self.skill_runtime = SkillRuntime(
            registry_path="config/skill_registry.json",
            runs_dir=self.cfg["paths"]["runs_dir"],
        )
        # Tools policy
        print("[Orchestrator] Creating LLMTraderController...")
        tools_cfg = (self.cfg.get("policies") or {}).get("tools", {})
        max_cc = None
        try:
            max_cc = int((tools_cfg.get("llm_trader") or {}).get("max_concurrent", 0) or 0)
        except Exception:
            max_cc = None
        self.llm = LLMTraderController(
            self.cfg.get("commands", {}),
            paper=self.paper_mode,
            paper_flag=str(self.modes.get("llm_paper_flag", "")) if self.paper_mode else None,
            runs_dir=self.cfg["paths"]["runs_dir"],
            max_concurrent=max_cc if max_cc and max_cc > 0 else None,
            min_interval_by_provider=((self.cfg.get("providers", {}) or {}).get("intervals", {}) or {}).get("min", {}),
            skill_runtime=self.skill_runtime,
        )
        print("[Orchestrator] Creating ABDeployer...")
        self.ab = ABDeployer(
            registry_path="scripts/ai_trader/registry/champion_challenger.json",
            promote_rules=self.cfg.get("policies", {}).get("ab", {}).get("promote_rules", {}),
            default_w=self.cfg.get("policies", {}).get("ab", {}).get("default_weights", {}),
        )

        self.runs_dir = Path(self.cfg["paths"]["runs_dir"])
        # System monitor: tracks label health for ML training readiness
        try:
            self.system_monitor = SystemMonitor()
        except Exception:
            self.system_monitor = None

        # 🆕 P0 UPGRADE: ML Config Health Check on Startup
        # Check ML configuration consistency on startup to catch config/schema mismatches
        # before training jobs fail. Logs warnings but doesn't block startup.
        print("[Orchestrator] Running ML config health check...")
        try:
            import sys
            _project_root = Path(__file__).parent.parent.parent
            if str(_project_root) not in sys.path:
                sys.path.insert(0, str(_project_root))

            from tools.check_ml_config_health import check_ml_config_health
            
            # Initialize the client to be passed to the health check
            cli = _get_ch_client()
            config_health = check_ml_config_health(output_file=None, cli=cli)

            if not config_health["healthy"]:
                self.log.warning("ml_config_health_check_failed",
                                 issues_count=config_health["issues_count"],
                                 warnings_count=config_health["warnings_count"])

                # Log each issue
                for issue in config_health.get("issues", []):
                    self.log.error("ml_config_issue",
                                   type=issue.get("type"),
                                   severity=issue.get("severity"),
                                   message=issue.get("message"))

                # TODO: In future, auto-fix critical issues here
                # For now, just log and continue
            else:
                self.log.info("ml_config_health_check_passed")
                print("[Orchestrator] ✅ ML config health check passed")

        except Exception as e:
            self.log.error("ml_config_health_check_failed", error=str(e), exc_info=True)
            print(f"[Orchestrator] ⚠️  ML config health check failed: {e}")

        # Policies
        self.fail_fast = bool((self.cfg.get("policies") or {}).get("fail_fast", False))
        # Propose-plan caching policy
        _pp = (self.cfg.get("policies") or {}).get("propose_plan", {})
        self._pp_use_triggers = bool((_pp or {}).get("use_triggers", True))
        # Minimum re-plan intervals per session (sec). More conservative for afterhours/overnight.
        self._pp_min_iv = ((_pp or {}).get("min_interval_sec") or {
            "morning": 900,      # 15m
            "intraday": 1200,    # 20m
            "afterhours": 3600,  # 60m
            "overnight": 7200    # 120m
        })
        # Stickiness window for providers/symbols to reduce plan churn
        try:
            self._pp_stickiness_sec = int((_pp or {}).get("stickiness_window_sec", 900))
        except Exception:
            self._pp_stickiness_sec = 900
        qh = (_pp or {}).get("quiet_hours_et", {}) or {}
        self._pp_quiet_start = str(qh.get("start", "02:00:00"))
        self._pp_quiet_end = str(qh.get("end", "06:00:00"))

        # Premarket scan-slot configuration (optional)
        pm = (self.cfg.get("policies") or {}).get("premarket", {}) or {}
        self._pm_scan_slots = list(pm.get("scan_slots") or [])
        try:
            self._pm_scan_tol = int(pm.get("scan_tolerance_sec", 120) or 120)
        except Exception:
            self._pm_scan_tol = 120
        # Control Brain (LLM) setup
        print("[Orchestrator] Checking brain config...")
        bcfg = self.cfg.get("brain", {})
        self.brain_enabled = bool(bcfg.get("enabled", False))
        if self.brain_enabled:
            print("[Orchestrator] Brain enabled, creating ControlBrain...")
            import os
            prov = os.getenv("LLM_PROVIDER", bcfg.get("provider", "deepseek_nvidia"))
            mod = os.getenv("LLM_MODEL_ML", os.getenv("LLM_MODEL", bcfg.get("model", "deepseek-ai/deepseek-r1")))
            self.brain = ControlBrain(
                runs_dir=self.cfg["paths"]["runs_dir"],
                provider=prov,
                model=mod,
            )
            print(f"[Orchestrator] ControlBrain created successfully (provider={prov}, model={mod})")

        # Validate critical configuration
        self._validate_critical_config()

        print("[Orchestrator] __init__ complete")

    # --- utilities ---
    def _validate_critical_config(self):
        """Validate critical configuration at startup."""
        try:
            from scripts.ai_trader.brain.brain import REQUIRED_COMMANDS, COMMAND_TEMPLATES
            import json

            missing = []
            invalid = []

            commands = self.cfg.get("commands", {})

            for cmd_name in REQUIRED_COMMANDS:
                if cmd_name not in commands:
                    missing.append(cmd_name)
                else:
                    cmd_value = commands[cmd_name]
                    template = COMMAND_TEMPLATES.get(cmd_name, {})
                    required_flags = template.get("required_flags", [])

                    # Validate format
                    for flag in required_flags:
                        if flag not in str(cmd_value):
                            invalid.append({
                                "command": cmd_name,
                                "issue": f"Missing required flag: {flag}",
                                "current": cmd_value,
                                "template": template.get("default")
                            })

            if missing or invalid:
                # Log issues
                self.log.error("missing_required_commands", missing=missing, invalid=invalid)

                # Persist to file
                dstr = datetime.date.today().isoformat()
                issues_file = self.runs_dir / dstr / "config_issues.json"
                issues_file.parent.mkdir(parents=True, exist_ok=True)

                with issues_file.open("w") as f:
                    json.dump({
                        "ts": datetime.datetime.now().isoformat(),
                        "missing_commands": missing,
                        "invalid_commands": invalid,
                        "templates": {cmd: COMMAND_TEMPLATES[cmd]["default"] for cmd in missing}
                    }, f, indent=2, ensure_ascii=False)

                # Trigger immediate Brain review
                invalidate_file = Path("cache/propose_plan.invalidate")
                invalidate_file.parent.mkdir(parents=True, exist_ok=True)
                invalidate_file.touch()

                self.log.warning("config_validation_failed", issues_file=str(issues_file), missing=len(missing), invalid=len(invalid))
            else:
                self.log.info("config_validation_passed", commands_checked=len(REQUIRED_COMMANDS))

        except Exception as e:
            self.log.error("config_validation_error", error=str(e), exc_info=True)

    def _ny_now_iso(self) -> str:
        """Get current time in NY timezone as ISO string."""
        return datetime.datetime.now(self.ny_tz).isoformat()

    def _ny_now(self) -> datetime.datetime:
        return datetime.datetime.now(self.ny_tz)

    def _adjust_health_for_session(self, session: str, health: dict | None) -> dict:
        """Downgrade off-hours live-tick/Kafka warnings to advisory.

        - In premarket (before 09:30 ET), afterhours, overnight sessions we may have no live ticks and Kafka lag.
        - Those are expected and should not force safe_mode/no_new_positions.
        - Keep other health errors intact.
        """
        try:
            h = dict(health or {})
        except Exception:
            return health or {}
        try:
            now = self._ny_now().strftime("%H:%M")
        except Exception:
            now = "00:00"
        is_off_hours = False
        try:
            if session in ("afterhours", "overnight"):
                is_off_hours = True
            elif session == "morning" and now < "09:30":
                is_off_hours = True
        except Exception:
            pass
        if not is_off_hours:
            return h
        # Heuristic: if only warnings about no recent ticks / Kafka lag, mark advisory_ok
        try:
            import json as _J
            s = _J.dumps(h, ensure_ascii=False).lower()
        except Exception:
            s = str(h).lower()
        phrases = ("no recent ticks", "no recent tick", "kafka lag", "kafka_lag")
        if (not bool(h.get("ok", True))) or (str(h.get("status", "")).lower() in ("warning", "degraded")):
            if any(p in s for p in phrases):
                h["ok"] = True
                h["advisory_ok"] = True
                h["off_hours_downgraded"] = True
                # Optional: annotate
                try:
                    notes = h.get("notes") or []
                    if isinstance(notes, list):
                        notes.append("downgraded: off-hours no live ticks/Kafka lag")
                        h["notes"] = notes
                except Exception:
                    pass
        return h

    # --- propose_plan cache helpers ---
    def _cache_dir(self) -> Path:
        d = self.runs_dir / datetime.date.today().isoformat() / "cache"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _ctx_sig_for_plan(self, session: str, ctx: dict) -> str:
        import hashlib, json as _J
        def _b(x):
            try:
                return bool(x)
            except Exception:
                return False
        health_ok = _b(((ctx or {}).get("health") or {}).get("ok")) or _b(ctx.get("data_ok"))
        dq = "has_dq_blacklist" if (ctx.get("dq_blacklist")) else "no_dq"
        news = "has_news" if (ctx.get("news")) else "no_news"
        features = "ff_present" if (ctx.get("features_freshness")) else "ff_absent"
        base = {"session": str(session), "health_ok": health_ok, "dq": dq, "news": news, "ff": features}
        raw = _J.dumps(base, sort_keys=True).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def _compress_diagnostics(self, diagnostics: dict, top_k: int = 32) -> dict:
        """Reduce diagnostics payload size while keeping the most informative parts.

        - Keep only top-K features by |IC| in feature_ic_spearman.
        - Add a small summary of IC distribution for global reasoning.
        """
        if not isinstance(diagnostics, dict):
            return diagnostics
        out = dict(diagnostics)
        try:
            fic = diagnostics.get("feature_ic_spearman") or {}
            if isinstance(fic, dict) and fic:
                items = []
                for k, v in fic.items():
                    try:
                        v_f = float(v)
                    except Exception:
                        continue
                    items.append((k, v_f))
                if items:
                    items.sort(key=lambda kv: abs(kv[1]), reverse=True)
                    if len(items) > top_k:
                        top_items = dict(items[:top_k])
                    else:
                        top_items = dict(items)
                    out["feature_ic_spearman"] = top_items
                    abs_vals = [abs(v) for _, v in items]
                    try:
                        max_abs_ic = max(abs_vals)
                        med_abs_ic = statistics.median(abs_vals)
                    except Exception:
                        max_abs_ic = None
                        med_abs_ic = None
                    out["feature_ic_summary"] = {
                        "n_features": len(items),
                        "max_abs_ic": float(max_abs_ic) if max_abs_ic is not None else None,
                        "median_abs_ic": float(med_abs_ic) if med_abs_ic is not None else None,
                    }
        except Exception:
            return diagnostics
        return out

    def _load_label_profiles_brief(self) -> dict:
        """Load a compact summary of intraday regression label profiles for ML Brain.

        Only intraday_reg_* profiles are exposed to keep context small and aligned with
        day-trading objective.
        """
        try:
            path = Path("label_profiles.yml")
            if not path.exists():
                return {}
            data = yaml.safe_load(path.read_text("utf-8")) or {}
            profiles = data.get("profiles") or {}
            brief: dict = {}
            for name, cfg in profiles.items():
                try:
                    if not isinstance(cfg, dict):
                        continue
                    name_str = str(name)
                    # Only expose intraday regression profiles to Brain
                    if not name_str.startswith("intraday_reg_"):
                        continue
                    task = str((cfg.get("task") or "reg")).lower()
                    if task != "reg":
                        continue
                    obj = cfg.get("objective") or {}
                    brief[name_str] = {
                        "task": task,
                        "horizon_bars": cfg.get("horizon_bars"),
                        "objective_metric": obj.get("metric"),
                        "min_trades": obj.get("min_trades"),
                    }
                except Exception:
                    continue
            return brief
        except Exception:
            return {}

    def _current_label_profile(self) -> str | None:
        """Best-effort read of current label_profile from main alpha config."""
        try:
            cfg_path = Path("alpha_config_enhanced.json")
            if not cfg_path.exists():
                return None
            data = json.loads(cfg_path.read_text("utf-8"))
            val = data.get("label_profile")
            return str(val) if isinstance(val, str) else None
        except Exception:
            return None

    def _in_quiet_hours(self) -> bool:
        now = self._ny_now()
        try:
            s = datetime.datetime.strptime(self._pp_quiet_start, "%H:%M:%S").time()
            e = datetime.datetime.strptime(self._pp_quiet_end, "%H:%M:%S").time()
            t = now.time()
            if s <= e:
                return s <= t <= e
            return t >= s or t <= e
        except Exception:
            return False

    # --- premarket scan slots helpers ---
    def _match_premarket_slot(self) -> str | None:
        """If current NY time is within tolerance of a configured premarket slot, return slot string HH:MM."""
        if not getattr(self, "_pm_scan_slots", None):
            return None
        now = self._ny_now()
        # Only consider before 09:30
        if now.strftime("%H:%M") >= "09:30":
            return None
        try:
            import datetime as _dt
            base = _dt.datetime.combine(now.date(), _dt.time(tzinfo=now.tzinfo))
            cur = base.replace(hour=now.hour, minute=now.minute, second=now.second, microsecond=0)
            for s in self._pm_scan_slots:
                try:
                    hh, mm, ss = [int(x) for x in s.split(":")]
                    slot = base.replace(hour=hh, minute=mm, second=ss)
                    delta = abs((cur - slot).total_seconds())
                    if delta <= int(getattr(self, "_pm_scan_tol", 120)):
                        return f"{hh:02d}:{mm:02d}"
                except Exception:
                    continue
        except Exception:
            return None
        return None

    def _execute_premarket_slot_if_due(self) -> dict | None:
        """If within a configured premarket scan slot, execute one LLM slot and submit/manage orders."""
        slot_hm = self._match_premarket_slot()
        if not slot_hm:
            return None
        slot_id = f"premarket_{slot_hm}"
        try:
            self._run_cmd(f"venv/bin/python llm_trader/scripts/execute_slot.py --slot {slot_id}", step="llm_slot_execute")
        except Exception:
            pass
        try:
            self._run_cmd("venv/bin/python llm_trader/scripts/submit_pending.py", step="llm_submit_pending")
        except Exception:
            pass
        try:
            self._run_cmd("venv/bin/python llm_trader/scripts/manage_orders.py", step="llm_manage_orders")
        except Exception:
            pass
        return {"status": "premarket_slot_executed", "slot": slot_id}

    def _propose_plan_cached(self, session: str, ctx: dict) -> dict:
        import json as _J, time as _t
        cdir = self._cache_dir()
        cfile = cdir / f"propose_plan_{session}.json"
        inv_generic = cdir / "propose_plan.invalidate"
        inv_session = cdir / f"propose_plan_{session}.invalidate"
        ttl = int((self._pp_min_iv or {}).get(session, 1200) or 1200)
        now = int(_t.time())
        ctx_sig = self._ctx_sig_for_plan(session, ctx or {})
        quiet = self._in_quiet_hours()
        if session == "maintenance":
            quiet = False

        cache = None
        if cfile.exists():
            try:
                cache = _J.loads(cfile.read_text("utf-8"))
            except Exception:
                cache = None
        invalidate = (inv_generic.exists() or inv_session.exists() or bool((cache or {}).get("invalidate")))
        if cache and not invalidate:
            try:
                age = now - int(cache.get("ts", 0))
            except Exception:
                age = ttl + 1
            if age < ttl and cache.get("ctx_sig") == ctx_sig:
                return cache.get("plan") or {}
        # Quiet hours without invalidation and no cache → skip heavy call
        if quiet and not invalidate and not cfile.exists():
            return {"session": session, "providers": [], "safe_mode": True, "notes": "quiet_hours_skip"}
        # Clear invalidation markers
        try:
            if inv_session.exists():
                inv_session.unlink(missing_ok=True)
            if inv_generic.exists():
                inv_generic.unlink(missing_ok=True)
        except Exception:
            pass
        # Call Brain via Stage 0 precheck to avoid hallucinated context
        try:
            plan = self.propose_plan_stage0_and_call(session, ctx)
        except Exception:
            plan = self.brain.propose_plan(session, ctx)
        try:
            out = {
                "ts": now,
                "ttl_sec": ttl,
                "ctx_sig": ctx_sig,
                "plan": plan,
                "hits": int((cache or {}).get("hits", 0)),
                "misses": int((cache or {}).get("misses", 0)) + 1,
            }
            cfile.write_text(_J.dumps(out, indent=2), encoding="utf-8")
        except Exception:
            pass
        return plan

    def _plan_stage0(self, session: str, ctx: dict) -> dict:
        """Deterministic pre-check for propose_plan: detect missing critical fields.

        Returns: {missing_fields: [...]} (can include additional future keys)
        """
        missing: list[str] = []
        try:
            if session in ("afterhours", "premarket"):
                ca = ((ctx or {}).get("llm_trader_flags") or {}).get("close_at")
                if not ca or not isinstance(ca, str) or ":" not in ca:
                    missing.append("llm_trader_flags.close_at")
        except Exception:
            pass
        return {"missing_fields": missing}

    def propose_plan_stage0_and_call(self, session: str, ctx: dict) -> dict:
        """Run Stage 0 pre-checks; if missing critical fields, return a safe placeholder plan.

        Otherwise call LLM via brain.propose_plan().
        """
        s0 = self._plan_stage0(session, ctx)
        if s0.get("missing_fields"):
            # Return safe placeholder; avoid calling LLM without close_at etc.
            try:
                missing = ",".join(s0.get("missing_fields", []))
            except Exception:
                missing = "critical_fields"
            return {
                "session": session,
                "providers": [],
                "safe_mode": True,
                "restrictions": ["manage_only", "slow_interval"],
                "notes": f"insufficient_context: missing {missing}",
            }
        # Else call LLM
        return self.brain.propose_plan(session, ctx)

    def _should_run_quiet_session(self, session: str) -> bool:
        """Determine whether to run a plan cycle for low-activity sessions.

        Triggers: explicit invalidation marker OR open positions (need management).
        """
        cdir = self._cache_dir()
        inv_generic = cdir / "propose_plan.invalidate"
        inv_session = cdir / f"propose_plan_{session}.invalidate"
        if inv_generic.exists() or inv_session.exists():
            return True
        # If recent success cooldown exists, skip quiet sessions (reduce cost post-success)
        try:
            import time as _t
            d = self.runs_dir / datetime.date.today().isoformat() / "locks"
            p = d / "success_cooldown_any.lock"
            if p.exists():
                age = _t.time() - p.stat().st_mtime
                if age < 6 * 3600:  # 6 hours cooldown
                    return False
        except Exception:
            pass
        # If we have open positions, we should run to manage/rescue
        try:
            pos = self._load_open_positions()
            if isinstance(pos, dict) and pos:
                return True
        except Exception:
            pass
        return False

    # --- data readiness helpers ---
    def _time_context(self) -> dict:
        try:
            now = self._ny_now()
            wd = now.weekday()  # 0=Mon ... 6=Sun
            # holiday detection
            try:
                from libs.market_calendar import is_holiday as _is_h
                holiday = bool(_is_h(now.date()))
            except Exception:
                holiday = False
            return {
                "ny_date": now.date().isoformat(),
                "ny_time": now.strftime("%H:%M:%S"),
                "weekday": wd,
                "is_weekend": bool(wd >= 5),
                "is_holiday": holiday,
            }
        except Exception:
            return {}
    def _strategy_symbols_from_config(self) -> list[str]:
        try:
            syms = self.learning.collect_symbols()
            if syms:
                return syms
        except Exception:
            pass
        # fallback to local control config optional defaults
        try:
            defaults = (self.cfg.get("default_symbols") or [])
            if isinstance(defaults, list):
                out = [str(x).strip() for x in defaults if str(x).strip()]
                if out:
                    return out
        except Exception:
            pass
        # ultimate safe fallback for plan normalization/tests
        return ["TSLA"]

    def _learning_target_ids(self) -> list[str]:
        try:
            out = self.learning.resolve_targets()
            if out:
                return out
        except Exception:
            pass
        return []

    def _primary_learning_target(self) -> str:
        try:
            sid = self.learning.primary_target()
            if sid:
                return sid
        except Exception:
            pass
        return "unknown"

    def _learning_observability(self, strategy_id: str | None = None) -> dict:
        sid = str(strategy_id or self._primary_learning_target() or "unknown")
        targets = self._learning_target_ids()
        try:
            profile = self.learning.profile_for(sid)
        except Exception:
            profile = "generic"
        try:
            adapter_name = self.learning.adapter_name_for(sid)
        except Exception:
            adapter_name = "generic"
        try:
            pipeline_ver = self.learning.pipeline_version()
        except Exception:
            pipeline_ver = "learning-pipeline-v1"
        return {
            "strategy_id": sid,
            "learning_profile": profile,
            "adapter_name": adapter_name,
            "pipeline_version": pipeline_ver,
            "learning_targets": targets,
        }

    def _decision_context(self, context: dict | None = None) -> dict:
        """Attach modular learning targets to brain decision context.

        Backward compatibility:
        - keep `strategy_id` for older prompts/parsers
        - add `primary_strategy_id` and structured `targets` for new multi-target logic
        """
        base: dict = dict(context or {})
        target_ids = self._learning_target_ids()
        primary = self._primary_learning_target()
        if not target_ids and primary:
            target_ids = [primary]
        base.setdefault("targets", [{"id": sid, "is_primary": bool(sid == primary)} for sid in target_ids])
        base.setdefault("learning_targets", target_ids)
        base.setdefault("primary_strategy_id", primary)
        base.setdefault("strategy_id", primary)
        return base

    def _brain_decide(self, action: str, context: dict | None = None) -> dict:
        return self.brain.decide(action, self._decision_context(context))

    def _apply_params_for_target(
        self,
        hpo_params_path: str,
        strategy_id: str | None = None,
        strategy_config: str | None = None,
        output_path: str | None = None,
    ) -> None:
        sid = str(strategy_id or self._primary_learning_target())
        cmd_override = self.learning.apply_command_for(sid)
        cfg_override = strategy_config or self.learning.apply_strategy_config_for(sid)
        self.rs.apply_params(
            hpo_params_path=hpo_params_path,
            strategy_config=(cfg_override or None),
            output_path=output_path,
            cmd_override=(cmd_override or None),
            strategy_id=sid,
        )

    def _apply_params_with_scope(
        self,
        hpo_params_path: str,
        strategy_id: str | None = None,
        strategy_config: str | None = None,
        output_path: str | None = None,
    ) -> dict:
        """Apply params with configurable writeback scope.

        Scope (policies.learning.writeback_scope):
        - primary (default): apply to single target (existing behavior)
        - all_targets: apply to all resolved learning targets in parallel,
          isolate per-target failures, and return aggregated results.
        """
        learning_cfg = (((self.cfg.get("policies") or {}).get("learning")) or {})
        scope = str(learning_cfg.get("writeback_scope") or "primary").strip().lower()
        if strategy_id:
            scope = "primary"
        if scope != "all_targets":
            sid = str(strategy_id or self._primary_learning_target())
            self._apply_params_for_target(
                hpo_params_path=hpo_params_path,
                strategy_id=sid,
                strategy_config=strategy_config,
                output_path=output_path,
            )
            return {"scope": "primary", "applied": [{"strategy_id": sid}], "failed": []}

        targets = self._learning_target_ids() or [self._primary_learning_target()]
        targets = [str(t).strip() for t in targets if str(t).strip()]
        if not targets:
            sid = self._primary_learning_target()
            self._apply_params_for_target(
                hpo_params_path=hpo_params_path,
                strategy_id=sid,
                strategy_config=strategy_config,
                output_path=output_path,
            )
            return {"scope": "primary_fallback", "applied": [{"strategy_id": sid}], "failed": []}

        try:
            max_workers = int(learning_cfg.get("max_parallel_targets", 2) or 2)
        except Exception:
            max_workers = 2
        max_workers = max(1, min(max_workers, len(targets)))

        applied: list[dict] = []
        failed: list[dict] = []

        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            fut_map = {
                ex.submit(
                    self._apply_params_for_target,
                    hpo_params_path,
                    sid,
                    strategy_config,
                    output_path,
                ): sid
                for sid in targets
            }
            for fut in as_completed(fut_map):
                sid = fut_map[fut]
                try:
                    fut.result()
                    applied.append({"strategy_id": sid})
                except Exception as e:
                    failed.append({"strategy_id": sid, "error": str(e)})

        return {"scope": "all_targets", "applied": applied, "failed": failed}

    def _build_available_columns(self, symbols: list[str]) -> dict:
        """
        Build a minimal AVAILABLE_COLUMNS mapping for ML Brain based on
        known-good core 1m feature columns. This is intentionally conservative
        and focuses on columns we have verified are fully populated for the
        current intraday enhanced ML setup (TSLA/NVDA as of 2025-11-24).

        The Brain uses this as a guardrail when designing new features.
        """
        if not symbols:
            return {}
        # Core base columns with 100% non-null coverage for TSLA/NVDA (last 60d)
        core_cols = [
            "ret_1",
            "ret_5",
            "atr_5",
            "atr_14",
            "atr_20",
            "vol_5m",
            "vol_15m",
            "vol_1h",
            "realized_vol_5m",
            "realized_vol_15m",
            "volume_rank_20",
            "vwap_dist_5m",
            "minute",
            "minutes_from_open",
            "minutes_to_close",
            "is_lunch_time",
        ]
        mapping: Dict[str, List[str]] = {}
        for s in symbols:
            mapping[str(s)] = [f"f.{c}" for c in core_cols]
        return mapping

    def _expected_1m_bars_by_now(self) -> int:
        """Expected 1m bars for today so far, honoring early-close via XNYS schedule.

        Fallback to fixed 09:30–16:00 (390) if pandas_market_calendars unavailable.
        """
        now = self._ny_now()
        # Try XNYS schedule for early close
        try:
            import pandas_market_calendars as pmc  # type: ignore
            cal = pmc.get_calendar('XNYS')
            sched = cal.schedule(start_date=now.date(), end_date=now.date())
            if sched is not None and len(sched) > 0:
                o = sched['market_open'].iloc[0].tz_convert(self.ny_tz)
                c = sched['market_close'].iloc[0].tz_convert(self.ny_tz)
                if now <= o:
                    return 0
                total = int((c - o).total_seconds() // 60)
                if now >= c:
                    return max(0, total)
                sofar = int((now - o).total_seconds() // 60)
                return max(0, min(total, sofar))
        except Exception:
            pass
        # Fallback: RTH 09:30–16:00 (390)
        start = now.replace(hour=9, minute=30, second=0, microsecond=0)
        end = now.replace(hour=16, minute=0, second=0, microsecond=0)
        if now < start:
            return 0
        if now > end:
            return 390
        mins = int((now - start).total_seconds() // 60)
        return max(0, min(390, mins))

    # --- trading calendar helpers ---
    def _is_trading_day(self, d: datetime.date) -> bool:
        """Return True if XNYS is open at any time on date d.

        Uses pandas_market_calendars when available; falls back to Mon–Fri heuristic.
        """
        try:
            import pandas_market_calendars as pmc  # type: ignore
            cal = pmc.get_calendar('XNYS')
            sched = cal.schedule(start_date=d, end_date=d)
            return bool(sched is not None and len(sched) > 0)
        except Exception:
            # Fallback: Mon–Fri as trading days
            return d.weekday() < 5

    def _prev_trading_day(self, n: int = 1) -> datetime.date:
        """Return the previous trading day n steps back (XNYS), with Mon–Fri fallback."""
        cur = datetime.date.today()
        steps = int(max(1, n))
        count = 0
        # Walk back day-by-day until we hit 'steps' trading days
        while count < steps:
            cur = cur - datetime.timedelta(days=1)
            if self._is_trading_day(cur):
                count += 1
        return cur

    def _realtime_check(self, symbols: list[str]) -> dict:
        """Redis-first realtime data liveness for sfs:1s:{symbol} keys."""
        try:
            cfg = self.realtime_cfg or {}
            enabled = bool(cfg.get("enabled", True))
            if not enabled:
                return {"ok": True, "reason": "realtime_disabled"}
            max_age = int(cfg.get("max_age_sec", 90) or 90)
            min_ratio = float(cfg.get("min_ok_ratio", 0.8) or 0.8)
            redis_url = cfg.get("redis_url")
            return realtime_check(symbols, redis_url=redis_url, max_age_sec=max_age, min_ok_ratio=min_ratio)
        except Exception as e:
            return {"ok": True, "reason": f"realtime_check_failed:{e}"}

    def _count_1m_today(self, ch: ClickHouseDB, symbol: str) -> int:
        # prefer configured table if present
        table = None
        try:
            table = (self.cfg.get("policies", {}) or {}).get("gates", {}).get("data_quality_checks", {}).get("table")
        except Exception:
            table = None
        if not table:
            table = "trading.bars_1m_ibkr"
        try:
            rows = ch.query(
                f"SELECT count() AS c FROM {table} WHERE toDate(ts_end)=today() AND symbol={{s:String}}",
                {"s": symbol},
            )
            return int(rows[0].get("c") or 0) if rows else 0
        except Exception:
            return 0

    def _intraday_backfill_block(self) -> Optional[dict]:
        """
        Guard to avoid heavy historical backfill during live/paper trading hours.
        Override with ALLOW_INTRADAY_BACKFILL=1 when you explicitly need it.
        """
        import os as _os

        if _os.getenv("ALLOW_INTRADAY_BACKFILL", "").lower() in ("1", "true", "yes", "on"):
            return None

        trading_mode = _os.getenv("TRADING_MODE", self.cfg.get("trading_mode", "paper")).lower()
        if trading_mode not in {"live", "paper"}:
            return None

        now = datetime.datetime.now(self.ny_tz)
        if now.weekday() >= 5:  # weekend
            return None

        if datetime.time(9, 15) <= now.time() <= datetime.time(16, 15):
            return {"mode": trading_mode, "now": now.isoformat()}

        return None

    def _load_hist_fast(self, symbols: list[str], days: int = 5, bar: str = "1m") -> None:
        """Fire-and-forget backfill with simple idempotency + backoff.

        - Per-symbol lock file: runs/YYYY-MM-DD/locks/backfill_{sym}_{bar}.lock
        - TTL: 15 minutes (skip repeated attempts within TTL)
        """
        if not symbols:
            return
        import time as _t
        dstr = datetime.date.today().isoformat()
        locks_dir = self.runs_dir / dstr / "locks"
        locks_dir.mkdir(parents=True, exist_ok=True)
        ttl_sec = 15 * 60

        def _lock_path(sym: str) -> Path:
            return locks_dir / f"backfill_{sym.upper()}_{bar}.lock"

        to_run: list[str] = []
        now = _t.time()
        for s in symbols:
            lp = _lock_path(s)
            try:
                if lp.exists():
                    age = now - lp.stat().st_mtime
                    if age < ttl_sec:
                        # recently attempted; skip
                        continue
                # touch lock (idempotent marker)
                lp.write_text(str(int(now)), encoding="utf-8")
                to_run.append(s)
            except Exception:
                # best-effort: if lock write fails, still include symbol once
                to_run.append(s)
        if not to_run:
            return
        syms = ",".join([s.upper() for s in to_run])
        cmd = f"python tool/load_hist_multi.py --symbols {syms} --days {int(days)} --bar-size {bar} --fill-mode backfill"
        self._run_cmd(cmd, step="load_hist_multi")

    def _ensure_data_ready(self, symbols: list[str], fast: bool = False) -> dict:
        """Ensure today's 1m bars exist for target symbols.

        - Computes expected bars by now; if below threshold, triggers load_hist_multi backfill.
        - Returns {checked: N, missing: k, threshold: r, attempted: bool}
        """
        try:
            ch = ClickHouseDB()
        except Exception:
            return {"checked": 0, "missing": 0, "attempted": False, "error": "ch_unavailable"}
        exp = self._expected_1m_bars_by_now()
        # before market open → nothing to check
        if exp <= 0:
            return {"checked": 0, "missing": 0, "attempted": False}
        # threshold ratio from config (default 0.85 intra-day)
        try:
            r = float(((self.cfg.get("policies", {}) or {}).get("gates", {}) or {}).get("min_data_ratio_by_now", 0.85))
        except Exception:
            r = 0.85
        min_ok = int(exp * r)
        if not symbols:
            symbols = self._strategy_symbols_from_config()
        missing = []
        for s in symbols:
            c = self._count_1m_today(ch, s)
            if c < min_ok:
                missing.append(s)
        attempted = False
        if missing:
            guard = self._intraday_backfill_block()
            if guard:
                self.log.warning("skip_intraday_backfill", symbols=missing, **guard)
            else:
                try:
                    # intraday: fast path always uses load_hist_multi to minimize latency, with idempotency/backoff
                    self._load_hist_fast(missing, days=5, bar="1m")
                    attempted = True
                except Exception as e:
                    self.log.warning("fast_backfill_failed", err=str(e), symbols=missing, exc_info=True)
        return {"checked": len(symbols), "missing": len(missing), "attempted": attempted, "min_ok": min_ok, "expected": exp}

    def _run_cmd(self, cmd: str, step: str) -> None:
        """Run a command, prefer current Python interpreter for python invocations.

        - If the command starts with `python`/`python3`, replace it with sys.executable
          so we honor the active virtualenv and avoid system-site package contamination.
        - Capture output for diagnostics; raise on non-zero exit.
        """
        import subprocess, shlex, sys
        self.log.info("run_step", step=step, cmd=cmd)
        try:
            parts = shlex.split(cmd)
            if parts and parts[0] in {"python", "python3"}:
                parts[0] = sys.executable
            proc = subprocess.run(parts, capture_output=True, text=True, check=True)
            if proc.stdout:
                self.log.debug("step_stdout", step=step, stdout=proc.stdout[-2000:])
            if proc.stderr:
                self.log.debug("step_stderr", step=step, stderr=proc.stderr[-2000:])
        except subprocess.CalledProcessError as e:
            self.log.error(
                "step_failed",
                step=step,
                returncode=e.returncode,
                cmd=cmd,
                stdout=(e.stdout[-2000:] if e.stdout else None),
                stderr=(e.stderr[-2000:] if e.stderr else None),
            )
            raise

    # --- knowledge helpers ---
    def _get_system_knowledge(self) -> dict:
        """Return a compact 'weapons catalog' of system components for the Brain.

        Includes traits of SFS-5s, FeatureCalculator-1m, StrategyEngine, llm_trader, and gates.
        Best-effort load of key docs for short descriptions.
        """
        knowledge = {
            "components": {
                "sfs_5s": {
                    "role": "ultra_low_latency_5s_bars",
                    "best_for": ["short_momentum", "fast_react"],
                    "tradeoffs": ["higher_noise", "costly_high_freq"]
                },
                "feature_calculator_1m": {
                    "role": "rich_indicators_on_1m",
                    "best_for": ["stable_signals", "mean_reversion", "trend_follow"]
                },
                "strategy_engine": {
                    "role": "predefined_ml_strategies",
                    "strategies": (self._learning_target_ids() or ["unknown"]),
                    "requires": ["features_1m_fresh"]
                },
                "llm_trader": {
                    "role": "flexible_bot_execution",
                    "best_for": ["low_freq_reasoning", "paper/live controlled"],
                    "controls": ["interval_sec", "cycles", "time_gates"]
                },
                "risk": {
                    "pretrade_guard": "AI-driven per-symbol ALLOW/SIZE_DOWN/REJECT with dynamic stops based on ATR/EV/PF/RVOL/VaR tests",
                    "kill_switch": "Session-level BLOCK_NEW/SIZE_DOWN/EXIT_ALL with optional hedging",
                    "hedging": "Overlay beta hedge and options hedge; policy under risk_management.hedge in config.yml",
                    "oco": "Strategy JSON oco controls take_profit_bp/hard_stop_bp/trail_atr_mult",
                    "policy_governance": "Autosettings via config.yml; default: autosettings.enabled=false, require_approval=true",
                },
                "gates": {
                    "data_quality": "bars_1m completeness/outliers/stale",
                    "feature_freshness": "per-symbol latest features recency",
                    "health": "system snapshot",
                },
            }
        }
        # Optionally embed brief doc excerpts (first ~2000 chars) to guide Brain terminology
        def _read_snip(p: str, cap: int = 2000) -> str:
            try:
                fp = Path(p)
                if fp.exists():
                    s = fp.read_text("utf-8")
                    return s[:cap]
            except Exception:
                pass
            return ""
        docs = {
            "GEMINI_md": _read_snip("GEMINI.md"),
            "ARCHITECTURE_quick": _read_snip("ARCHITECTURE_QUICK_REFERENCE.md"),
        }
        knowledge["docs"] = {k: v for k, v in docs.items() if v}
        return knowledge

    # --- Positions awareness/rescue helpers ---
    def _load_open_positions(self) -> dict:
        """Best-effort load of open positions (symbol -> {qty, avg_price}).

        Primary source: ClickHouse `trading.positions` (quantity != 0).
        Returns {} on any error.
        """
        try:
            ch = ClickHouseDB()
        except Exception:
            return {}
        try:
            rows = ch.query(
                "SELECT symbol, quantity, avg_price FROM trading.positions WHERE quantity != 0"
            ) or []
            out = {}
            for r in rows:
                try:
                    sym = str(r.get("symbol") or "").upper()
                    qty = float(r.get("quantity") or 0)
                    avg = float(r.get("avg_price") or 0)
                    if sym and qty != 0:
                        out[sym] = {"qty": qty, "avg_price": avg}
                except Exception:
                    continue
            return out
        except Exception:
            return {}

    def _write_rescue_orders(self, exits: dict, positions: dict) -> str | None:
        """Persist planned exit orders to runs/YYYY-MM-DD/rescue_orders.json for llm_trader to pick up.

        exits schema: {SYMBOL: {"mode": "limit|market", "px": float|null, "tw_s": int|null, "qty": optional float}}
        Only writes for symbols actually in positions.
        Returns path string or None.
        """
        try:
            dstr = datetime.date.today().isoformat()
            out_dir = self.runs_dir / dstr
            out_dir.mkdir(parents=True, exist_ok=True)
            orders = []
            for sym, plan in (exits or {}).items():
                s = str(sym).upper()
                if s not in positions:
                    continue
                qty = float(plan.get("qty") or positions[s]["qty"])
                side = "SELL" if qty > 0 else "BUY"  # support short covers
                px = plan.get("px")
                tw_s = int(plan.get("tw_s") or 900)
                orders.append({
                    "symbol": s,
                    "side": side,
                    "qty": abs(int(qty)),
                    "px": float(px) if px is not None else None,
                    "mode": str(plan.get("mode") or "limit"),
                    "tw_s": tw_s,
                })
            if not orders:
                return None
            fp = out_dir / "rescue_orders.json"
            fp.write_text(json.dumps({"orders": orders, "ts": self._ny_now_iso()} , indent=2), encoding="utf-8")
            return str(fp)
        except Exception:
            return None

    def _default_positions_exit(self, positions: dict) -> dict:
        """Generate a conservative default exit plan when Brain doesn't provide one.

        Long: limit at avg_price*1.01; Short: limit at avg_price*0.99; validity window 90 minutes.
        """
        out = {}
        for sym, p in (positions or {}).items():
            try:
                qty = float(p.get("qty") or 0)
                avg = float(p.get("avg_price") or 0)
                if qty == 0 or avg <= 0:
                    continue
                if qty > 0:
                    px = avg * 1.01
                else:
                    px = avg * 0.99
                out[str(sym).upper()] = {"mode": "limit", "px": px, "tw_s": 5400, "qty": qty}
            except Exception:
                continue
        return out

    def _apply_strategy_controls(self, controls: list, health: dict | None = None, data_gate: dict | None = None, ff: dict | None = None) -> dict | None:
        """Apply AI-requested strategy pause/resume with minimal risk gating and brain approval.

        controls: [{id: '<strategy_id>', action: 'pause'|'resume'}]
        Gate: allow pause always; resume only if health.ok and data_gate.ok and (ff is None or ff.ok)
        Also ask Brain approval via decide('execute_actions') to keep a human-in-the-loop style LLM guardrail.
        """
        if not controls:
            return None
        # Normalize
        todo = []
        for c in controls:
            try:
                sid = str(c.get("id") or "")
                act = str(c.get("action") or "").lower()
                if sid and act in {"pause", "resume"}:
                    todo.append({"id": sid, "action": act})
            except Exception:
                continue
        if not todo:
            return None
        # Risk gate
        can_resume = bool((health or {}).get("ok", True)) and bool((data_gate or {}).get("ok", True))
        if ff is not None and isinstance(ff, dict):
            try:
                can_resume = can_resume and bool(ff.get("ok", True))
            except Exception:
                pass
        pending = []
        for t in todo:
            if t["action"] == "resume" and not can_resume:
                continue
            pending.append({"action": ("resume_strategy" if t["action"] == "resume" else "pause_strategy"), "params": {"id": t["id"]}})
        if not pending:
            return {"applied": [], "denied": [t for t in todo if t["action"] == "resume"], "errors": []}
        # Brain approval
        approve = None
        if self.brain_enabled:
            try:
                dec = self._brain_decide("execute_actions", {"pending": pending})
                if isinstance(dec, dict) and isinstance(dec.get("approve"), list):
                    approve = {it.get("action"): bool(it.get("ok")) for it in dec.get("approve")}
            except Exception:
                approve = None
        def is_ok(action: str) -> bool:
            return True if approve is None else bool(approve.get(action, False))
        # Apply to config.yml
        import yaml as _yaml
        applied, denied, errors = [], [], []
        try:
            cfg_path = Path("config.yml")
            cfg_data = _yaml.safe_load(cfg_path.read_text("utf-8")) or {}
        except Exception as e:
            return {"applied": [], "denied": [t for t in todo], "errors": [str(e)]}
        changed = False
        for t in todo:
            sid, act = t["id"], t["action"]
            act_key = "resume_strategy" if act == "resume" else "pause_strategy"
            if act == "resume" and not can_resume:
                denied.append(t)
                continue
            if not is_ok(act_key):
                denied.append(t)
                continue
            try:
                for s in (cfg_data.get("strategies") or []):
                    if str(s.get("id")) == sid:
                        s["enabled"] = (act == "resume")
                        applied.append(t)
                        changed = True
                        break
                else:
                    errors.append(f"strategy_not_found:{sid}")
            except Exception as e:
                errors.append(str(e))
        if changed:
            try:
                cfg_path.write_text(_yaml.safe_dump(cfg_data, sort_keys=False), encoding="utf-8")
            except Exception as e:
                errors.append(f"write_failed:{e}")
        # Persist log
        try:
            outd = self.runs_dir / datetime.date.today().isoformat()
            outd.mkdir(parents=True, exist_ok=True)
            (outd / "strategy_controls_apply.json").write_text(json.dumps({"applied": applied, "denied": denied, "errors": errors}, indent=2), encoding="utf-8")
        except Exception:
            pass
        return {"applied": applied, "denied": denied, "errors": errors}

    # --- risk policy autosettings ---
    def _risk_policy_whitelist(self) -> set[str]:
        return {
            "risk_management.guardian.dd_intraday_pct",
            "risk_management.guardian.dd_5d_pct",
            "risk_management.guardian.vol_spike_mult",
            "risk_management.hedge.strategy_id",
            "risk_management.hedge.min_notional_usd",
            "risk_management.hedge.auto_execute",
            "risk_management.hedge.mode",
        }

    def _get_cfg_value(self, cfg: dict, dotted: str):
        cur = cfg
        for k in dotted.split('.'):
            if not isinstance(cur, dict) or k not in cur:
                return None
            cur = cur[k]
        return cur

    def _set_cfg_value(self, cfg: dict, dotted: str, value):
        keys = dotted.split('.')
        cur = cfg
        for k in keys[:-1]:
            if k not in cur or not isinstance(cur[k], dict):
                cur[k] = {}
            cur = cur[k]
        cur[keys[-1]] = value

    def _autosettings_enabled(self) -> tuple[bool, bool]:
        """Check autosettings status - always reload from file to catch config updates"""
        try:
            # Reload ai_trader.yaml to catch runtime config updates
            import yaml as _yaml
            cfg = _yaml.safe_load(self.cfg_path.read_text("utf-8")) or {}
            pol = (cfg.get("policies") or {}).get("autosettings", {})
            enabled = bool(pol.get("enabled", False))
            require_approval = bool(pol.get("require_approval", True))
            return enabled, require_approval
        except Exception:
            return False, True

    def _apply_risk_policy(self, request: dict) -> dict:
        """Apply risk policy changes from Brain within a whitelist.

        request schema: { apply: [{key: dotted_key, value: any}], notes: str }
        Governance: config.policies.autosettings.{enabled, require_approval}
        Audit: runs/YYYY-MM-DD/audits/risk_policy_*.json
        Backup: backups/config/config.yml.TIMESTAMP.yaml
        """
        enabled, require_approval = self._autosettings_enabled()
        audit = {"received": request, "enabled": enabled, "require_approval": require_approval, "applied": [], "denied": [], "errors": []}
        if not enabled:
            audit["errors"].append("autosettings_disabled")
            self._persist_audit("risk_policy", audit)
            return audit
        todo = (request or {}).get("apply") or []
        if not isinstance(todo, list) or not todo:
            self._persist_audit("risk_policy", audit)
            return audit
        whitelist = self._risk_policy_whitelist()
        # Load current config.yml
        import yaml as _yaml, time as _time
        try:
            cfg_path = Path("config.yml")
            cfg_data = _yaml.safe_load(cfg_path.read_text("utf-8")) or {}
        except Exception as e:
            audit["errors"].append(f"read_config_failed:{e}")
            self._persist_audit("risk_policy", audit)
            return audit
        changed = False
        for item in todo:
            k = str((item or {}).get("key") or "")
            if not k or k not in whitelist:
                audit["denied"].append({"key": k, "reason": "not_whitelisted"})
                continue
            v = item.get("value")
            try:
                cur = self._get_cfg_value(cfg_data, k)
                if cur != v:
                    self._set_cfg_value(cfg_data, k, v)
                    changed = True
                    audit["applied"].append({"key": k, "from": cur, "to": v})
            except Exception as e:
                audit["errors"].append(f"set_failed:{k}:{e}")
        # Backup and write
        import os as _os
        auto_approve = bool(_os.getenv("AI_TRADER_APPROVE_AUTOS", "").strip())
        if changed and (not require_approval or auto_approve):
            try:
                bdir = Path("backups/config")
                bdir.mkdir(parents=True, exist_ok=True)
                ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
                (bdir / f"config.yml.{ts}.yaml").write_text(_yaml.safe_dump(self.cfg, sort_keys=False), encoding="utf-8")
            except Exception:
                pass
            try:
                Path("config.yml").write_text(_yaml.safe_dump(cfg_data, sort_keys=False), encoding="utf-8")
                self.cfg = cfg_data  # refresh in-memory snapshot
            except Exception as e:
                audit["errors"].append(f"write_failed:{e}")
        elif changed and require_approval and not auto_approve:
            audit["errors"].append("pending_approval")
        self._persist_audit("risk_policy", audit)
        return audit

    def _persist_audit(self, name: str, payload: dict) -> None:
        try:
            outd = self.runs_dir / datetime.date.today().isoformat() / "audits"
            outd.mkdir(parents=True, exist_ok=True)
            ts = datetime.datetime.now().strftime("%H%M%S")
            (outd / f"{name}_{ts}.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception:
            pass

    # --- News need/TTL helpers ---
    def _news_ttl_sec(self, session: str) -> int:
        """Return TTL for news freshness by session.

        Defaults:
          - premarket: 1800s (30m)
          - intraday (RTH): 3600s (60m)
          - afterhours/overnight: 3600s (60m)
        """
        s = (session or "").lower()
        if s in {"morning", "premarket"}:
            return 1800
        if s in {"afterhours", "overnight"}:
            return 3600
        return 3600

    def _news_last_fetch_map(self) -> dict:
        """Load per-symbol last_fetch ISO timestamps from runs/.../news_context.json if present."""
        try:
            dstr = datetime.date.today().isoformat()
            fp = self.runs_dir / dstr / "news_context.json"
            if not fp.exists():
                return {}
            obj = json.loads(fp.read_text("utf-8"))
            m = obj.get("last_fetch") or {}
            return m if isinstance(m, dict) else {}
        except Exception:
            return {}

    def _should_refresh_news(self, symbol: str, session: str, last_map: dict) -> bool:
        try:
            ttl = self._news_ttl_sec(session)
            ts = last_map.get(symbol)
            if not ts:
                return True
            try:
                last = datetime.datetime.fromisoformat(ts.replace("Z","+00:00"))
            except Exception:
                return True
            now = datetime.datetime.now(datetime.timezone.utc)
            age = (now - last).total_seconds()
            return age >= ttl
        except Exception:
            return True

    def _maybe_inject_need_news(self, session: str, plan: dict) -> dict:
        """Auto-mark need_news when inputs suggest it, honoring TTL and caps.

        - Only adds need_news when missing or empty to avoid overriding Brain.
        - Picks symbols from current plan providers; caps to 10 symbols per cycle.
        - Leaves room for Brain-provided news_days/news_limit if already present.
        """
        try:
            if not isinstance(plan, dict):
                return plan
            cur = plan.get("need_news")
            if isinstance(cur, list) and cur:
                return plan  # Brain already requested
            # Gather candidate symbols from plan
            cand: list[str] = []
            for p in (plan.get("providers") or []):
                for s in (p.get("symbols") or []):
                    s = str(s).upper()
                    if s and s not in cand:
                        cand.append(s)
            if not cand:
                return plan
            last = self._news_last_fetch_map()
            need = [s for s in cand if self._should_refresh_news(s, session, last)]
            if not need:
                return plan
            # Cap to 10 symbols per cycle for cost control
            need = need[:10]
            plan["need_news"] = need
            # Provide sensible defaults if not specified
            plan.setdefault("news_days", 1)
            plan.setdefault("news_limit", 3)
            # Audit request intent
            try:
                dstr = datetime.date.today().isoformat()
                outd = self.runs_dir / dstr
                outd.mkdir(parents=True, exist_ok=True)
                audit_path = outd / "news_requests.jsonl"
                evt = {
                    "ts": self._ny_now_iso(),
                    "session": session,
                    "symbols": need,
                    "ttl_sec": self._news_ttl_sec(session),
                    "reason": "ttl_refresh",
                }
                with audit_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(evt) + "\n")
            except Exception:
                pass
            return plan
        except Exception:
            return plan

    def _risk_pretrade_guard(self, plan: dict) -> dict:
        """Run risk pretrade guard via Brain and adjust plan with conservative overrides.

        - Writes risk_limits.json for LLM bots to honor
        - Maps REJECT→per_symbol_override.pause, SIZE_DOWN→restrictions.reduce_sizing
        """
        try:
            from scripts.ai_trader.risk.pretrade_guard import run_pretrade_guard
            # collect all symbols from plan providers
            syms = []
            for p in (plan.get("providers") or []):
                for s in (p.get("symbols") or []):
                    if s not in syms:
                        syms.append(s)
            if not syms:
                return plan
            # Get TTL config (default 1 hour global, 30 min per-symbol)
            rate_limit_cfg = ((self.cfg.get("policies") or {}).get("rate_limiting") or {})
            min_interval_sec = rate_limit_cfg.get("risk_pretrade_min_interval_sec", 3600)
            per_symbol_ttl_sec = rate_limit_cfg.get("risk_pretrade_per_symbol_ttl_sec", 1800)

            # Build thresholds/extras from config
            rp_cfg = ((self.cfg.get("policies") or {}).get("risk_pretrade") or {})
            extras = {}
            try:
                thr = (rp_cfg.get("thresholds") or {})
                extras = {
                    "thresholds": {
                        "max_spread_bps": float(thr.get("max_spread_bps", 50.0) or 50.0),
                        "min_adv_usd": float(thr.get("min_adv_usd", 10_000_000.0) or 10_000_000.0),
                        "var_kupiec_p_min": float(thr.get("var_kupiec_p_min", 0.05) or 0.05),
                        "var_christoffersen_p_min": float(thr.get("var_christoffersen_p_min", 0.05) or 0.05),
                    }
                }
            except Exception:
                extras = {}

            res = run_pretrade_guard(
                self.runs_dir,
                syms,
                extras=extras,
                brain=(self.brain if self.brain_enabled else None),
                min_interval_sec=min_interval_sec,
                per_symbol_ttl_sec=per_symbol_ttl_sec
            )

            # Log cache stats to track efficiency
            stats = res.get("cache_stats", {})
            if stats:
                self.log.info(
                    "risk_pretrade_cache_stats",
                    hits=stats.get("hits", 0),
                    misses=stats.get("misses", 0),
                    to_eval_count=stats.get("to_eval_count", 0),
                    cached_count=stats.get("cached_count", 0),
                    global_invalidated=stats.get("global_invalidated", False)
                )

            decisions = (res or {}).get("decisions") or []
            # If any symbol requires data backfill, attempt quick backfill and re-run for those symbols only
            try:
                to_backfill = [d.get("symbol") for d in decisions if d.get("need_backfill")]
            except Exception:
                to_backfill = []
            if to_backfill:
                try:
                    self._load_hist_fast(to_backfill, days=5, bar="1m")
                except Exception as e:
                    self.log.warning("pretrade_backfill_failed", err=str(e), symbols=to_backfill, exc_info=True)
                # re-run guard for backfilled symbols to refresh decisions and cache
                try:
                    res2 = run_pretrade_guard(
                        self.runs_dir,
                        to_backfill,
                        extras=extras,
                        brain=(self.brain if self.brain_enabled else None),
                        min_interval_sec=0,  # force immediate
                        per_symbol_ttl_sec=0
                    )
                    dec2 = (res2 or {}).get("decisions") or []
                    if dec2:
                        # merge: replace old decisions for those symbols
                        by_sym = {d.get("symbol"): d for d in decisions if d.get("symbol") not in to_backfill}
                        for d in dec2:
                            by_sym[d.get("symbol")] = d
                        decisions = list(by_sym.values())
                    # Invalidate plan cache for next propose_plan call
                    inv = self._cache_dir() / "propose_plan.invalidate"
                    try:
                        inv.touch()
                    except Exception:
                        pass
                except Exception as e:
                    self.log.warning("pretrade_rerun_failed", err=str(e), exc_info=True)
            if not decisions:
                return plan
            # apply conservative mapping
            pso = dict(plan.get("per_symbol_override") or {})
            restrictions = list(plan.get("restrictions") or [])
            reject_syms = set()
            for d in decisions:
                sym = d.get("symbol")
                dec = (d.get("decision") or "").upper()
                if dec in {"REJECT", "EXIT"}:
                    pso[sym] = "pause"
                    reject_syms.add(sym)
                elif dec == "SIZE_DOWN" and "reduce_sizing" not in restrictions:
                    restrictions.append("reduce_sizing")
            # OOF quality gate (policy-driven): if quality below thresholds, pause symbol
            try:
                qpol = ((self.cfg.get("policies") or {}).get("oof_quality") or {})
                auc_thr = float(qpol.get("auc_thr", 0.52) or 0.52)
                ic_thr = float(qpol.get("ic_thr", 0.02) or 0.02)
                cov_thr = float(qpol.get("cov_thr", 0.60) or 0.60)
                d = Path(self.runs_dir) / datetime.date.today().isoformat() / "oof_eval"
                for sym in syms:
                    try:
                        fp = d / f"{sym}.json"
                        if not fp.exists():
                            continue
                        m = json.loads(fp.read_text("utf-8"))
                        auc = float(m.get("auc", 0.0) or 0.0)
                        ic = float(m.get("ic", 0.0) or 0.0)
                        cov = float(m.get("coverage", 0.0) or 0.0)
                        if not (auc >= auc_thr and ic > ic_thr and cov >= cov_thr):
                            # Limit new entries but allow managing existing positions
                            pso[sym] = "manage_only"
                            reject_syms.add(sym)
                    except Exception:
                        continue
            except Exception:
                pass
            plan["per_symbol_override"] = pso
            plan["restrictions"] = restrictions
            # prune rejected symbols to reduce cost
            provs = (plan.get("providers") or [])
            for p in provs:
                sy = p.get("symbols") or []
                p["symbols"] = [s for s in sy if s not in reject_syms]
            return plan
        except Exception:
            return plan

    # --- health helpers ---
    def _health_report_file(self) -> Path:
        try:
            return Path(self.cfg["paths"]["health_report"])  # type: ignore
        except Exception:
            return Path("logs/system_health.json")

    def _maybe_refresh_health(self, force: bool = False) -> None:
        """Refresh docs/daily/system_health_check.py if snapshot missing or stale.

        Controlled by policies.health.*. Best-effort; failures are non-fatal here.
        """
        try:
            pol = (self.cfg.get("policies") or {}).get("health", {})
            max_age_min = int(pol.get("auto_refresh_minutes", 120) or 120)
        except Exception:
            max_age_min = 120
        f = self._health_report_file()
        need = force or (not f.exists())
        if not need:
            try:
                import os, time as _t
                age_s = _t.time() - os.path.getmtime(f)
                if age_s > max_age_min * 60:
                    need = True
            except Exception:
                need = True
        if not need:
            return
        try:
            self._run_cmd("venv/bin/python docs/daily/system_health_check.py", step="health_refresh")
        except Exception as e:
            # Non-fatal; leave gating to HealthGate
            self.log.warning("health_refresh_failed", err=str(e), exc_info=True)

    # --- Sequences ---
    def _persist_decision(self, filename: str, decide_ctx: dict, decision: dict) -> None:
        outd = self.runs_dir / datetime.date.today().isoformat()
        outd.mkdir(parents=True, exist_ok=True)
        (outd / filename).write_text(json.dumps({"input": decide_ctx, "decision": decision}, indent=2), encoding="utf-8")

    def _run_feature_engineering_agent(self, out_dir: Path, best_oos: dict, retry_audit: list) -> dict:
        """
        Run Feature Engineering Agent to suggest feature improvements based on HPO results.

        Args:
            out_dir: Output directory for this run
            best_oos: Best OOS result from HPO
            retry_audit: Audit of all HPO retry rounds

        Returns:
            Feature engineering suggestion dict or None if failed
        """
        try:
            # Load current feature_programs.json
            fp_path = Path("feature_programs.json")
            if not fp_path.exists():
                self.log.warning("feature_programs_not_found", path=str(fp_path))
                return None

            with fp_path.open("r", encoding="utf-8") as f:
                current_feature_programs = json.load(f)

            # Load current alpha_config_enhanced.json HPO space
            config_path = Path("alpha_config_enhanced.json")
            if not config_path.exists():
                self.log.warning("alpha_config_not_found", path=str(config_path))
                return None

            with config_path.open("r", encoding="utf-8") as f:
                full_config = json.load(f)

            grid = full_config.get("hpo", {}).get("grid", {})
            current_alpha_hpo_config = {
                "feature_program_id": grid.get("feature_program_id"),
                "feature_subset": grid.get("feature_subset"),
                "model_scale_mode": grid.get("model_scale_mode")
            }

            # Extract trial results from retry_audit
            recent_trials = []
            for round_data in retry_audit:
                if round_data.get("status") == "completed" and "best" in round_data:
                    best = round_data.get("best", {})
                    trial = {
                        "trial_id": round_data.get("round"),
                        "params": best.get("params", {}),
                        "oos_summary": best.get("oos", {})
                    }
                    recent_trials.append(trial)

            # If no trials from retry_audit, try to load from best_oos
            if not recent_trials and best_oos:
                recent_trials.append({
                    "trial_id": 0,
                    "params": best_oos.get("params", {}),
                    "oos_summary": best_oos.get("oos", {})
                })

            if not recent_trials:
                self.log.warning("no_trials_for_feature_engineering")
                return None

            # Get available features from ClickHouse or config
            available_features = [
                "close", "high", "low", "open", "volume",
                "vwap", "ret_1", "ret_5", "ret_10",
                "atr_5", "atr_14", "atr_20",
                "rsi_14", "bb_upper", "bb_lower", "bb_width",
                "ema_12", "ema_26", "ema_50",
                "vol_avg_20", "spread_bps", "tick_imbalance"
            ]

            # Build context for Feature Engineering Agent
            context = {
                "recent_trials": recent_trials,
                "available_features": available_features,
                "current_feature_programs": current_feature_programs,
                "current_alpha_hpo_config": current_alpha_hpo_config
            }

            # Attach best-run diagnostics (label/feature/prediction stats) if available
            try:
                diag = best_oos.get("diagnostics") if isinstance(best_oos, dict) else None
                if isinstance(diag, dict):
                    context["diagnostics"] = diag
            except Exception:
                pass

            self.log.info("calling_feature_engineering_agent",
                         trials_count=len(recent_trials),
                         current_programs=len(current_feature_programs))

            # Call Brain's suggest_feature_engineering method
            suggestion = self.brain.suggest_feature_engineering(context)

            if not suggestion:
                self.log.warning("feature_engineering_agent_returned_empty")
                return None

            # Save suggestion to file
            suggestion_file = out_dir / "feature_engineering_suggestion.json"
            with suggestion_file.open("w", encoding="utf-8") as f:
                json.dump(suggestion, f, indent=2, ensure_ascii=False)
            try:
                self._mirror_learning_artifact(suggestion_file, artifact_name="feature_engineering_suggestion")
            except Exception:
                pass

            self.log.info("feature_engineering_suggestion_saved",
                         file=str(suggestion_file),
                         analysis_length=len(suggestion.get("analysis", "")))

            # Optional: auto-apply feature engineering patch to configs (AI-controlled)
            try:
                autopatch = self._auto_apply_feature_engineering_patch(
                    suggestion,
                    current_feature_programs=current_feature_programs,
                    full_config=full_config,
                    feature_programs_path=fp_path,
                    alpha_config_path=config_path,
                )
                if autopatch is not None:
                    suggestion["_autopatch"] = autopatch
            except Exception as e:
                self.log.warning("feature_engineering_autopatch_failed", err=str(e), exc_info=True)

            return suggestion

        except Exception as e:
            self.log.error("feature_engineering_agent_error", error=str(e), exc_info=True)
            return None

    def _persist_plan(self, session: str, plan: dict) -> None:
        """Persist the normalized plan for the current session to runs dir and publish to event bus.

        Path: runs/YYYY-MM-DD/brain_plan_{session}.json
        Events: internal.plans (all plans), internal.plans.approved (post-risk-guard plans)
        """
        try:
            outd = self.runs_dir / datetime.date.today().isoformat()
            outd.mkdir(parents=True, exist_ok=True)
            payload = dict(plan or {})
            payload["ts"] = self._ny_now_iso()
            payload["session"] = session

            # Persist to file
            (outd / f"brain_plan_{session}.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

            # Publish to event bus (best-effort)
            # All plans go to internal.plans
            event_bus.publish('plans', payload)

            # Only publish to internal.plans.approved if plan passed risk guard
            # (indicated by presence of 'providers' field - risk guard doesn't remove it)
            if plan.get('providers'):
                event_bus.publish('plans_approved', payload)

        except Exception as e:
            self.log.warning("plan_publish_failed", session=session, err=str(e))

    def _load_planned_provider_symbol_map(self) -> dict:
        """Load union of provider→symbols from all brain_plan_*.json files for today.

        Returns: {provider: set(symbols)}; if a provider has empty symbols list in plan, map to empty set to denote 'any'.
        """
        out: dict[str, set] = {}
        try:
            d = self.runs_dir / datetime.date.today().isoformat()
            if not d.exists():
                return out
            for pf in d.glob("brain_plan_*.json"):
                try:
                    j = json.loads(pf.read_text("utf-8"))
                    for p in (j.get("providers") or []):
                        prov = p.get("name")
                        if not prov:
                            continue
                        syms = list(p.get("symbols") or [])
                        # empty list → denote 'any' via empty set sentinel
                        if prov not in out:
                            out[prov] = set(syms)
                        else:
                            if len(out[prov]) == 0:
                                # already any; keep empty set
                                continue
                            if len(syms) == 0:
                                out[prov] = set()  # upgrade to any
                            else:
                                out[prov].update({s for s in syms})
                except Exception:
                    continue
        except Exception:
            return {}
        return out

    def _meta_matches_plan(self, meta: dict, planned: dict) -> bool:
        """Return True if process meta aligns with planned provider/symbols.

        - If provider not in plan → False
        - If planned symbols empty (meaning 'any') → True
        - If meta symbols intersect planned symbols → True else False
        """
        try:
            prov = meta.get("provider")
            if not prov or prov not in planned:
                return False
            planned_syms = planned.get(prov)
            if planned_syms is None:
                return False
            # empty set denotes 'any symbol'
            if len(planned_syms) == 0:
                return True
            meta_syms = set(meta.get("symbols") or [])
            return bool(meta_syms & planned_syms)
        except Exception:
            return False

    def _check_gate_with_brain(self, gate_name: str, gate_result: dict, all_gates: dict) -> dict | None:
        if gate_result and not gate_result.get("ok"):
            if self.brain_enabled:
                try:
                    decide_ctx = {"gates": all_gates}
                    decision = self._brain_decide("start_morning", decide_ctx)
                    decision = self._validate_start_morning_decision(decision)
                    fname = DECISION_FILES.get(gate_name, DECISION_FILES["generic"])
                    self._persist_decision(fname, decide_ctx, decision)
                    if not (isinstance(decision, dict) and decision.get("proceed")):
                        return {"gate": gate_name, "status": "block", "detail": gate_result, "brain": decision}
                except Exception as e:
                    self.log.warning("gate_brain_decide_failed", gate=gate_name, err=str(e), exc_info=True)
                    return {"gate": gate_name, "status": "block", "detail": gate_result}
            else:
                return {"gate": gate_name, "status": "block", "detail": gate_result}
        return None

    def _latency_stats(self) -> dict:
        """Compute avg/p95 duration from brain_calls.jsonl for today (best-effort)."""
        out = {"avg_sec": None, "p95_sec": None}
        try:
            d = self.runs_dir / datetime.date.today().isoformat() / "brain_calls.jsonl"
            if not d.exists():
                return out
            vals = []
            with d.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        ms = float(obj.get("duration_ms") or 0.0)
                        if ms > 0:
                            vals.append(ms / 1000.0)
                    except Exception:
                        continue
            if not vals:
                return out
            vals.sort()
            import math
            avg = sum(vals) / len(vals)
            p95 = vals[math.floor(0.95 * (len(vals) - 1))]
            out["avg_sec"], out["p95_sec"] = round(avg, 2), round(p95, 2)
        except Exception:
            return out
        return out

    @staticmethod
    def _dq_has_missing(dq_result: dict) -> bool:
        """Heuristic: return True if DQ errors indicate 'no_data' or 'gap_detection'."""
        try:
            failed = dq_result.get("failed") or {}
            for sym, errs in failed.items():
                if not isinstance(errs, list):
                    continue
                for e in errs:
                    se = str(e)
                    if se.startswith("no_data") or se.startswith("gap_detection"):
                        return True
        except Exception:
            return False
        return False

    def _cleanup_stale_locks(self):
        """Clean up stale lock files on startup.

        When auto_loop/scheduler is killed (SIGKILL, crash, etc), lock files may be left behind.
        This prevents the system from deadlocking on restart.
        """
        try:
            import datetime
            locks_dir = self.runs_dir / datetime.date.today().isoformat() / "locks"
            if not locks_dir.exists():
                self.log.debug("no_locks_dir", path=str(locks_dir))
                return

            cleaned = []
            # Locks that should be preserved (long-lived, intentional)
            PRESERVE_LOCKS = {
                "recon_daily.lock",  # 6-hour TTL, intentional
                "ml_pause.lock",     # User-controlled pause
            }

            for lock_file in locks_dir.glob("*.lock"):
                try:
                    lock_name = lock_file.name

                    # Skip intentionally persistent locks
                    if lock_name in PRESERVE_LOCKS:
                        self.log.debug("lock_preserved", lock=lock_name,
                                      reason="intentional_long_lived")
                        continue

                    # Read lock content for audit trail
                    try:
                        lock_content = lock_file.read_text().strip()
                    except Exception:
                        lock_content = "<unreadable>"

                    # Check lock age (safety check: only clean old locks)
                    import time
                    mtime = lock_file.stat().st_mtime
                    age_sec = time.time() - mtime

                    # Only clean locks older than 60 seconds (to avoid race conditions)
                    if age_sec > 60:
                        lock_file.unlink()
                        cleaned.append(lock_name)
                        self.log.info("stale_lock_cleaned", lock=lock_name,
                                     age_sec=int(age_sec), content=lock_content[:50],
                                     reason="startup_cleanup")
                    else:
                        self.log.debug("lock_too_recent", lock=lock_name, age_sec=int(age_sec))

                except Exception as e:
                    self.log.warning("lock_cleanup_failed", lock=lock_file.name, error=str(e))

            if cleaned:
                self.log.info("startup_lock_cleanup_complete", locks_cleaned=len(cleaned),
                             locks=cleaned)
                print(f"[Orchestrator] Cleaned {len(cleaned)} stale locks: {', '.join(cleaned)}")
            else:
                self.log.debug("no_stale_locks_found")

        except Exception as e:
            self.log.warning("lock_cleanup_error", error=str(e), exc_info=True)

    def morning_sequence(self):
        # Optional: feature freshness check for symbols (advisory, not a hard block)
        # Prefer realtime gate (for strategies using RealtimeFeatureEngine)
        # Fallback to ClickHouse gate (legacy)
        features_freshness = None
        if self.rff_enabled and self.realtime_feature_freshness_gate is not None:
            try:
                rff_cfg = (self.cfg.get("policies", {}).get("gates", {}).get("realtime_feature_freshness", {}))
                symbols = rff_cfg.get("symbols_to_check", []) or []
                if symbols:
                    features_freshness = self.realtime_feature_freshness_gate.check(symbols)
                    try:
                        outd = self.runs_dir / datetime.date.today().isoformat()
                        outd.mkdir(parents=True, exist_ok=True)
                        (outd / "features_freshness.json").write_text(json.dumps(features_freshness, indent=2), encoding="utf-8")
                    except Exception:
                        pass
            except Exception as e:
                self.log.warning("realtime_features_freshness_check_failed", err=str(e), exc_info=True)
        elif self.ff_enabled and self.feature_freshness_gate is not None:
            try:
                ff_cfg = (self.cfg.get("policies", {}).get("gates", {}).get("features_freshness", {}))
                symbols = ff_cfg.get("symbols_to_check", []) or []
                if symbols:
                    features_freshness = self.feature_freshness_gate.check(symbols)
                    try:
                        outd = self.runs_dir / datetime.date.today().isoformat()
                        outd.mkdir(parents=True, exist_ok=True)
                        (outd / "features_freshness.json").write_text(json.dumps(features_freshness, indent=2), encoding="utf-8")
                    except Exception:
                        pass
            except Exception as e:
                self.log.warning("features_freshness_check_failed", err=str(e), exc_info=True)
        # Ensure health snapshot is fresh if configured
        try:
            pol = (self.cfg.get("policies") or {}).get("health", {})
            if bool(pol.get("refresh_before_morning", True)):
                self._maybe_refresh_health(force=False)
        except Exception:
            pass
        # Optional: proactive data-quality check for key symbols (previous trading day by default)
        dq_blacklist: List[str] = []
        if self.dq_enabled and self.data_quality_gate is not None:
            try:
                dq_cfg = (self.cfg.get("policies", {}).get("gates", {}).get("data_quality_checks", {}))
                symbols = dq_cfg.get("symbols_to_check", []) or []
                offset = int(dq_cfg.get("check_date_offset_days", 1) or 1)
                wait_sec = int(dq_cfg.get("backfill_wait_sec", 300) or 300)
                # Use previous TRADING day, not simply 'yesterday'
                check_date = self._prev_trading_day(offset)
                if symbols:
                    # If chosen check_date is a non-trading day (defensive), skip DQ
                    if not self._is_trading_day(check_date):
                        self.log.info("dq_skip_non_trading_day", check_date=str(check_date))
                        outd = self.runs_dir / datetime.date.today().isoformat()
                        outd.mkdir(parents=True, exist_ok=True)
                        (outd / "data_quality.json").write_text(json.dumps({
                            "ok": True,
                            "reason": "non_trading_day",
                            "check_date": check_date.isoformat(),
                            "symbols": symbols
                        }, indent=2), encoding="utf-8")
                        # Skip the remainder of DQ block
                        raise StopIteration()
                    # 1) 初检
                    dq_res = self.data_quality_gate.check(symbols, check_date)
                    attempted_backfill = False
                    # 2) 缺数据/有空洞 → 回补 → 等待 → 复检
                    try:
                        if not dq_res.get("ok", True) and self._dq_has_missing(dq_res):
                            self.log.warning("data_quality_missing_detected_trigger_backfill", symbols=symbols, offset_days=offset)
                            attempted_backfill = True
                            self.rs.backfill_yesterday()
                            self.log.info("waiting_for_backfill", seconds=wait_sec)
                            time.sleep(wait_sec)
                            self.log.info("rerun_data_quality_after_backfill")
                            dq_res = self.data_quality_gate.check(symbols, check_date)
                    except Exception as e:
                        self.log.warning("data_quality_backfill_attempt_failed", err=str(e), exc_info=True)
                    # 3) 最终结果 → 黑名单 & 持久化
                    if not dq_res.get("ok", True):
                        dq_blacklist = list((dq_res.get("failed") or {}).keys())
                        self.log.warning("data_quality_blacklist", blacklist=dq_blacklist)
                    try:
                        outd = self.runs_dir / datetime.date.today().isoformat()
                        outd.mkdir(parents=True, exist_ok=True)
                        final = dict(dq_res or {})
                        final["attempted_backfill"] = attempted_backfill
                        (outd / "data_quality.json").write_text(json.dumps(final, indent=2), encoding="utf-8")
                    except Exception:
                        pass
            except StopIteration:
                pass
            except Exception as e:
                self.log.warning("data_quality_check_failed", err=str(e), exc_info=True)
        h = self.health.check()
        h = self._adjust_health_for_session("morning", h)
        if block := self._check_gate_with_brain("health", h, {"health": h}):
            return block
        # Scope symbols used for morning gates
        try:
            syms_for_gate = self._strategy_symbols_from_config()
        except Exception:
            syms_for_gate = []
        # Realtime (Redis-first) gate for premarket
        rt = self._realtime_check(syms_for_gate)
        # If feature freshness checks are disabled, provide explicit advisory context to Brain
        if features_freshness is None:
            features_freshness = {
                "check_enabled": False,
                "reason": "disabled_in_config",
                "expected_disabled": True,
                "advisory_ok": True,
                "realtime_ok": bool(rt.get("ok", True)),
            }
        try:
            req_data = bool(self.cfg.get("policies", {}).get("gates", {}).get("require_data_ok", True))
        except Exception:
            req_data = True
        # In premarket (before 09:30 ET), allow missing live ticks (llm_trader will self-check)
        try:
            if self._ny_now().strftime("%H:%M") < "09:30":
                req_data = False
        except Exception:
            pass
        if not rt.get("ok", True) and req_data:
            return {"gate": "realtime", "status": "block", "detail": rt}
        # CH Data gate (advisory/fallback); still honor require_data_ok for consistency
        d = self.data.check(symbols=syms_for_gate)
        if not d.get("ok"):
            # Try backfill yesterday then recheck once
            self.rs.backfill_yesterday()
            d = self.data.check(symbols=syms_for_gate)
            if block := self._check_gate_with_brain("data", d, {"health": h, "data": d}):
                return block
        # Execution gate: skip in paper mode
        if not self.paper_mode:
            e = self.exec.check()
            if not e.get("ok"):
                if self.brain_enabled:
                    try:
                        decide_ctx = {"gates": {"health": h, "data": d, "exec": e}}
                        decision = self._brain_decide("start_morning", decide_ctx)
                        decision = self._validate_start_morning_decision(decision)
                        outd = self.runs_dir / datetime.date.today().isoformat()
                        outd.mkdir(parents=True, exist_ok=True)
                        (outd / "morning_decision_exec.json").write_text(json.dumps({"input": decide_ctx, "decision": decision}, indent=2), encoding="utf-8")
                        if isinstance(decision, dict) and decision.get("proceed"):
                            pass
                        else:
                            return {"gate": "exec", "status": "block", "detail": e, "brain": decision}
                    except Exception:
                        return {"gate": "exec", "status": "block", "detail": e}
                else:
                    return {"gate": "exec", "status": "block", "detail": e}

        llm_required = bool(self.cfg.get("policies", {}).get("gates", {}).get("require_llm_ok", False))
        if llm_required:
            lg = self.llm_gate.check()
            if not lg.get("ok"):
                return {"gate": "llm", "status": "block", "detail": lg}

        # Premarket scan-slot gating: before 09:30, if not in a configured slot and no triggers, skip heavy LLM plan
        try:
            if self._ny_now().strftime("%H:%M") < "09:30":
                # If we are exactly in a scan slot, execute the LLM slot pipeline first
                slot_res = self._execute_premarket_slot_if_due()
                if not slot_res and (not self._should_run_quiet_session("morning")):
                    try:
                        outd = self.runs_dir / datetime.date.today().isoformat()
                        outd.mkdir(parents=True, exist_ok=True)
                        (outd / "premarket_scan_skip.json").write_text(json.dumps({
                            "ts": self._ny_now_iso(),
                            "reason": "not_in_premarket_scan_slot",
                            "next_slots": getattr(self, "_pm_scan_slots", []),
                        }, indent=2), encoding="utf-8")
                    except Exception:
                        pass
                    return {"status": "skip", "reason": "premarket_not_in_scan_slot"}
        except Exception:
            pass

        # Optionally ask Control Brain for plan (LLM total control; persisted to runs dir)
        if self.brain_enabled:
            try:
                mem = {}
                try:
                    mem = memory_summary("morning", lookback_days=MEMORY_LOOKBACK_DAYS)
                except Exception:
                    mem = {}
                # Best-effort: premarket news analysis
                news_ctx = None
                try:
                    news_ctx = self._get_news_analysis()
                except Exception as e:
                    self.log.warning("news_collect_failed", err=str(e), exc_info=True)
                # Execute pending actions from previous cycle (review buffer)
                try:
                    self._execute_pending_actions()
                except Exception as e:
                    self.log.warning("execute_pending_actions_failed", err=str(e), exc_info=True)
                # Ask if we should proceed under current gate statuses
                try:
                    decide_ctx = {
                        "gates": {
                            "health": h,
                            "data": d,
                            "exec": {"ok": True, "skipped": True} if self.paper_mode else e,
                        }
                    }
                    decision = self._brain_decide("start_morning", decide_ctx)
                    # Persist decision
                    self._persist_decision(DECISION_FILES["generic"], decide_ctx, decision)
                    if isinstance(decision, dict) and decision.get("proceed") is False:
                        return {"gate": "brain", "status": "block", "detail": {"decision": decision}}
                except Exception:
                    pass

                # Positions awareness: load any leftover positions to inform Brain
                positions = {}
                try:
                    positions = self._load_open_positions()
                except Exception:
                    positions = {}
                # Label health: check ML training data readiness
                label_health = None
                try:
                    if self.system_monitor:
                        raw_health = self.system_monitor.check_label_health()
                        # Simplify for Brain consumption
                        label_health = simplify_label_health_for_brain(raw_health)
                except Exception as e:
                    self.log.warning("label_health_check_failed", err=str(e), exc_info=True)
                plan = self._propose_plan_cached(
                    "morning",
                    {
                        "health": h,
                        "data_ok": d.get("ok"),
                        "exec_ok": True,
                        "memory": mem,
                        **({"positions": positions} if positions else {}),
                        **({"news": news_ctx} if news_ctx else {}),
                        "system_knowledge": self._get_system_knowledge(),
                        **({"features_freshness": features_freshness} if features_freshness else {}),
                        **({"dq_blacklist": dq_blacklist} if dq_blacklist else {}),
                        **({"llm_latency": self._latency_stats()}),
                        **({"label_health": label_health} if label_health else {}),
                    },
                )
                # Health on-demand refresh if requested by Brain
                try:
                    need_h = bool(isinstance(plan, dict) and plan.get("need_health"))
                except Exception:
                    need_h = False
                if not need_h:
                    try:
                        import os as _os
                        if _os.getenv("AI_TRADER_FORCE_HEALTH", "").strip():
                            need_h = True
                    except Exception:
                        pass
                if need_h:
                    try:
                        self._maybe_refresh_health(force=True)
                        h = self.health.check()
                        plan = self._propose_plan_cached(
                            "morning",
                            {
                                "health": h,
                                "data_ok": d.get("ok"),
                                "exec_ok": True,
                                "memory": mem,
                                **({"news": news_ctx} if news_ctx else {}),
                                **({"dq_blacklist": dq_blacklist} if dq_blacklist else {}),
                                **({"llm_latency": self._latency_stats()}),
                            },
                        )
                    except Exception as e:
                        self.log.warning("health_on_demand_failed", err=str(e), exc_info=True)
                # Self-evaluate plan and adopt conservative tweaks if needed
                try:
                    evalr = self.brain.evaluate_plan("morning", {
                        "health": h, "data": d, "features_freshness": features_freshness
                    }, plan)
                    if isinstance(evalr, dict):
                        if bool(evalr.get("recommend_safe_mode", False)):
                            plan.setdefault("safe_mode", True)
                            rs = evalr.get("recommended_restrictions") or []
                            if isinstance(rs, list) and rs:
                                cur = list(plan.get("restrictions") or [])
                                plan["restrictions"] = list({*cur, *rs})
                        # If eval says missing context, optionally ask tools on next cycles (we already support need_*)
                        req = evalr.get("required_tools") or []
                        if isinstance(req, list):
                            if "need_news" in req and not plan.get("need_news"):
                                plan["need_news"] = []  # empty triggers no-op; Brain can decide
                            if "need_health" in req and not plan.get("need_health"):
                                plan["need_health"] = True
                except Exception as e:
                    self.log.warning("plan_evaluate_failed", err=str(e), exc_info=True)
                # Apply strategy controls (pause/resume) if present and allowed by gates
                try:
                    sc = plan.get("strategy_controls") if isinstance(plan, dict) else None
                    if sc:
                        self._apply_strategy_controls(sc, health=h, data_gate=d, ff=features_freshness)
                except Exception as e:
                    self.log.warning("strategy_controls_apply_failed", err=str(e), exc_info=True)
                # If Brain requests fresh news for specific symbols, or TTL suggests refresh, fetch and re-ask once
                try:
                    # Auto-inject need_news when missing but TTL suggests refresh for plan symbols
                    try:
                        plan = self._maybe_inject_need_news("morning", plan)
                    except Exception:
                        pass
                    need = []
                    if isinstance(plan, dict):
                        need = list(plan.get("need_news") or [])
                    if not need:
                        import os
                        env_need = os.getenv("AI_TRADER_FORCE_NEWS", "").strip()
                        if env_need:
                            need = [s.strip().upper() for s in env_need.split(',') if s.strip()]
                    if need:
                        limit = None
                        days = None
                        try:
                            limit = int(plan.get("news_limit")) if isinstance(plan, dict) and plan.get("news_limit") is not None else None
                        except Exception:
                            pass
                        try:
                            days = int(plan.get("news_days")) if isinstance(plan, dict) and plan.get("news_days") is not None else None
                        except Exception:
                            pass
                        fresh = self._get_news_analysis_for(need, days=days, limit=limit)
                        if fresh:
                            plan = self._propose_plan_cached(
                                "morning",
                                {
                                    "health": h,
                                    "data_ok": d.get("ok"),
                                    "exec_ok": True,
                                    "memory": mem,
                                    "news": fresh,
                                    "system_knowledge": self._get_system_knowledge(),
                                    "time": self._time_context(),
                                    **({"dq_blacklist": dq_blacklist} if dq_blacklist else {}),
                                    **({"llm_latency": self._latency_stats()}),
                                },
                            )
                except Exception as e:
                    self.log.warning("news_on_demand_failed", err=str(e), exc_info=True)
                plan = self._normalize_or_build_plan("morning", plan, mem)
                # After normalization, for premarket slot windows also submit/manage orders (best-effort)
                try:
                    if self._ny_now().strftime("%H:%M") < "09:30":
                        self._run_cmd("venv/bin/python llm_trader/scripts/submit_pending.py", step="llm_submit_pending")
                        self._run_cmd("venv/bin/python llm_trader/scripts/manage_orders.py", step="llm_manage_orders")
                except Exception:
                    pass
                # If Brain proposed exits for leftover positions, persist rescue orders for llm_trader to execute
                try:
                    exits = plan.get("positions_exit") if isinstance(plan, dict) else None
                    # If positions exist but Brain didn't provide exits, create a conservative default
                    if positions and not isinstance(exits, dict):
                        exits = self._default_positions_exit(positions)
                    if isinstance(exits, dict) and positions:
                        path = self._write_rescue_orders(exits, positions)
                        if path:
                            self.log.info("rescue_orders_written", file=path)
                            # Force manage-only: prohibit new entries while managing exits
                            try:
                                flags = plan.setdefault("llm_trader_flags", {})
                                cur = int(flags.get("forbid_new_before_min", 0) or 0)
                                flags["forbid_new_before_min"] = max(cur, 1440)
                            except Exception:
                                pass
                except Exception as e:
                    self.log.warning("positions_exit_apply_failed", err=str(e), exc_info=True)
                # Ensure data readiness for planned symbols (pre-trade)
                try:
                    p_syms = []
                    for p in (plan.get("providers") or []):
                        for s in (p.get("symbols") or []):
                            if s not in p_syms:
                                p_syms.append(s)
                    dr = self._ensure_data_ready(p_syms, fast=False)
                    if dr.get("missing", 0) > 0:
                        self.log.warning("data_readiness_remediated_morning", **dr)
                except Exception as e:
                    self.log.warning("data_readiness_check_failed_morning", err=str(e), exc_info=True)
                # Ensure providers include any open-position symbols for rescue handling
                try:
                    if positions and isinstance(plan, dict):
                        have = set()
                        for p in (plan.get("providers") or []):
                            for s in (p.get("symbols") or []):
                                have.add(str(s).upper())
                        missing = [s for s in positions.keys() if s not in have]
                        if missing:
                            provs = plan.get("providers") or []
                            if provs:
                                p0 = provs[0]
                                cur = [str(s).upper() for s in (p0.get("symbols") or [])]
                                for ms in missing:
                                    if ms not in cur:
                                        cur.append(ms)
                                p0["symbols"] = cur
                                plan["providers"][0] = p0
                except Exception:
                    pass
                overrides = self._prepare_prompt_overrides(plan)
                # Risk pretrade guard
                plan = self._risk_pretrade_guard(plan)
                self.llm.run_with_plan(plan, prompt_overrides=overrides)
                # Persist plan and reconcile running processes with this plan (best-effort)
                try:
                    self._persist_plan("morning", plan)
                    self._reconcile_processes_with_plan(plan)
                except Exception:
                    pass
                # Export events for morning (best-effort)
                try:
                    from scripts.ai_trader.exporters.events_exporter import export_events as export_ev
                    dstr = datetime.date.today().isoformat()
                    export_ev(str(self.runs_dir), dstr, session="morning")
                except Exception:
                    pass
                return {"gate": "all", "status": "ok", "executed_plan": plan}
            except Exception as e:
                # LLM failure should not block session since gates passed
                pass

        # Fallback: Start premarket session
        self.llm.start_premarket()
        # Export events as well (best-effort)
        try:
            from scripts.ai_trader.exporters.events_exporter import export_events as export_ev
            dstr = datetime.date.today().isoformat()
            export_ev(str(self.runs_dir), dstr, session="morning")
        except Exception:
            pass
        return {"gate": "all", "status": "ok"}

    def intraday_sequence(self, mode="fast"):
        # Try plan-driven if brain enabled
        if self.brain_enabled:
            try:
                # 轻量级数据自愈：盘中优先快速检查今天的核心标的
                try:
                    if getattr(self, "data_healer", None) is not None:
                        syms = self._strategy_symbols_from_config()
                        if syms:
                            self.data_healer.ensure_data_ready(syms, lookback_days=1)
                except Exception as e:
                    self.log.debug("intraday_healer_warning", err=str(e))

                try:
                    pol = (self.cfg.get("policies") or {}).get("health", {})
                    if bool(pol.get("refresh_before_intraday", False)):
                        self._maybe_refresh_health(force=False)
                except Exception:
                    pass
                mem = {}
                try:
                    mem = memory_summary("intraday", lookback_days=MEMORY_LOOKBACK_DAYS)
                except Exception:
                    mem = {}
                # Optional features freshness at intraday (advisory)
                # Prefer realtime gate for strategies using RealtimeFeatureEngine
                ff_ctx = None
                if self.rff_enabled and self.realtime_feature_freshness_gate is not None:
                    try:
                        rff_cfg = (self.cfg.get("policies", {}).get("gates", {}).get("realtime_feature_freshness", {}))
                        symbols = rff_cfg.get("symbols_to_check", []) or []
                        if symbols:
                            ff_ctx = self.realtime_feature_freshness_gate.check(symbols)
                    except Exception:
                        ff_ctx = None
                elif self.ff_enabled and self.feature_freshness_gate is not None:
                    try:
                        ff_cfg = (self.cfg.get("policies", {}).get("gates", {}).get("features_freshness", {}))
                        symbols = ff_cfg.get("symbols_to_check", []) or []
                        if symbols:
                            ff_ctx = self.feature_freshness_gate.check(symbols)
                    except Exception:
                        ff_ctx = None
                else:
                    # Even if check is disabled, pass status so AI brain knows
                    ff_ctx = {"check_enabled": False, "reason": "disabled_in_config", "expected_disabled": True, "advisory_ok": True}

                plan = self.brain.propose_plan(
                    "intraday",
                    {
                        "mode": mode,
                        "memory": mem,
                        "system_knowledge": self._get_system_knowledge(),
                        "time": self._time_context(),
                        "features_freshness": ff_ctx,  # Always pass, even if disabled
                    },
                )
                # Health on-demand refresh if requested during intraday
                try:
                    need_h = bool(isinstance(plan, dict) and plan.get("need_health"))
                except Exception:
                    need_h = False
                if not need_h:
                    try:
                        import os as _os
                        if _os.getenv("AI_TRADER_FORCE_HEALTH", "").strip():
                            need_h = True
                    except Exception:
                        pass
                if need_h:
                    try:
                        self._maybe_refresh_health(force=True)
                        h = self.health.check()
                    except Exception as e:
                        self.log.warning("intraday_health_on_demand_failed", err=str(e), exc_info=True)
                plan = self._propose_plan_cached("intraday", {"mode": mode, "memory": mem, "health": h, "time": self._time_context()})
                # Self-evaluate plan intraday as well (disabled - trust AI brain's initial plan)
                # evaluate_plan tends to be overly conservative, triggering safe mode even when features check is disabled
                # trust the propose_plan output unless there are actual data quality failures
                pass
                # Apply strategy controls intraday if any
                try:
                    sc = plan.get("strategy_controls") if isinstance(plan, dict) else None
                    if sc:
                        self._apply_strategy_controls(sc, health=self.health.check(), data_gate=None, ff=ff_ctx)
                except Exception as e:
                    self.log.warning("strategy_controls_apply_failed_intraday", err=str(e), exc_info=True)
                # On-demand news during intraday if requested or TTL suggests refresh
                try:
                    try:
                        plan = self._maybe_inject_need_news("intraday", plan)
                    except Exception:
                        pass
                    need = []
                    if isinstance(plan, dict):
                        need = list(plan.get("need_news") or [])
                    if need:
                        fresh = self._get_news_analysis_for(need)
                        if fresh:
                            plan = self._propose_plan_cached(
                                "intraday",
                                {
                                    "mode": mode,
                                    "memory": mem,
                                    "news": fresh,
                                    "system_knowledge": self._get_system_knowledge(),
                                },
                            )
                except Exception as e:
                    self.log.warning("intraday_news_on_demand_failed", err=str(e), exc_info=True)
                plan = self._normalize_or_build_plan("intraday", plan, mem)
                # Intraday fast data readiness remediation for plan symbols
                try:
                    p_syms = []
                    for p in (plan.get("providers") or []):
                        for s in (p.get("symbols") or []):
                            if s not in p_syms:
                                p_syms.append(s)
                    dr = self._ensure_data_ready(p_syms, fast=True)
                    if dr.get("missing", 0) > 0:
                        self.log.warning("data_readiness_remediated_intraday", **dr)
                except Exception as e:
                    self.log.warning("data_readiness_check_failed_intraday", err=str(e), exc_info=True)
                overrides = self._prepare_prompt_overrides(plan)
                plan = self._risk_pretrade_guard(plan)
                self.llm.run_with_plan(plan, prompt_overrides=overrides)
                # Execute intraday LLM slot and manage orders (idempotent)
                try:
                    now_hm = self._ny_now().strftime("%H:%M")
                    slot_id = f"intraday_{now_hm}"
                    self._run_cmd(f"venv/bin/python llm_trader/scripts/execute_slot.py --slot {slot_id}", step="llm_slot_execute_intraday")
                    self._run_cmd("venv/bin/python llm_trader/scripts/submit_pending.py", step="llm_submit_pending_intraday")
                    self._run_cmd("venv/bin/python llm_trader/scripts/manage_orders.py", step="llm_manage_orders_intraday")
                    # If close approaching, also trigger EOD slot once in the last 5 minutes
                    if now_hm >= "15:55":
                        eod_slot = f"eod_{now_hm}"
                        self._run_cmd(f"venv/bin/python llm_trader/scripts/execute_slot.py --slot {eod_slot}", step="llm_slot_execute_eod")
                        self._run_cmd("venv/bin/python llm_trader/scripts/submit_pending.py", step="llm_submit_pending_eod")
                        self._run_cmd("venv/bin/python llm_trader/scripts/manage_orders.py", step="llm_manage_orders_eod")
                except Exception:
                    pass
                # Persist plan and reconcile (best-effort)
                try:
                    self._persist_plan("intraday", plan)
                    self._reconcile_processes_with_plan(plan)
                except Exception:
                    pass
                # Also write hourly memory snapshot (best effort)
                snap = self._maybe_write_intraday_memory_snapshot()
                # Process Guardian emergencies if any (best effort)
                try:
                    self._process_guardian_events()
                except Exception as e:
                    self.log.warning("guardian_process_failed", err=str(e), exc_info=True)
                # Apply immune actions emitted by anomaly detector (best effort)
                try:
                    self._process_immune_actions()
                except Exception as e:
                    self.log.warning("immune_actions_process_failed", err=str(e), exc_info=True)
                # Risk kill-switch monitor (best-effort)
                try:
                    from scripts.ai_trader.risk.kill_switch import evaluate_kill_switch
                    ks = evaluate_kill_switch(self.runs_dir, thresholds=(self.cfg.get("risk_management", {}) or {}).get("guardian", {}), brain=(self.brain if self.brain_enabled else None))
                    act = str((ks or {}).get("action") or "")
                    if act in {"EXIT_ALL", "BLOCK_NEW", "SIZE_DOWN"}:
                        # Apply conservative actions via strategy controls/config
                        sc = []
                        restrictions = []
                        if act == "EXIT_ALL" or act == "BLOCK_NEW":
                            sc.append({"id": self._primary_learning_target(), "action": "pause"})
                            restrictions.append("no_new_positions")
                        if act == "SIZE_DOWN":
                            restrictions.append("reduce_sizing")
                        if sc:
                            try:
                                self._apply_strategy_controls(sc, health=self.health.check(), data_gate=None, ff=None)
                            except Exception:
                                pass
                        # Persist soft restrictions for bots (best-effort)
                        try:
                            d = self.runs_dir / datetime.date.today().isoformat()
                            d.mkdir(parents=True, exist_ok=True)
                            import json as _J
                            (d/"risk_soft_restrictions.json").write_text(_J.dumps({"restrictions": restrictions}, indent=2), encoding="utf-8")
                        except Exception:
                            pass
                        # Hedging: use new selector-based options hedging
                        try:
                            import json as _J
                            import asyncio as _aio
                            from pathlib import Path as _P
                            dstr = datetime.date.today().isoformat()
                            outd = self.runs_dir / dstr
                            outd.mkdir(parents=True, exist_ok=True)

                            # Check if options hedging is enabled
                            hcfg = (self.cfg.get("risk_management", {}) or {}).get("hedge", {})
                            opts_enabled = (hcfg.get("options", {}) or {}).get("enabled", True)

                            if opts_enabled:
                                # Use new selector-based approach
                                from scripts.ai_trader.risk.options_selector import (
                                    propose_options_hedge_with_selector,
                                    HedgeIntent,
                                )
                                from scripts.ai_trader.risk.options_hedge import _portfolio_exposure

                                # Get current portfolio exposure
                                exposure = _portfolio_exposure()

                                # Parse intent from brain if available
                                hedge = (ks or {}).get("hedge") if isinstance(ks, dict) else None
                                intent = None
                                if isinstance(hedge, dict):
                                    intent = HedgeIntent(
                                        intent_type=hedge.get("intent_type", "vol_spike"),
                                        instrument=hedge.get("instrument"),
                                        size=hedge.get("size", "medium"),
                                        urgency=hedge.get("urgency", "normal"),
                                        note=f"kill_switch:{act}",
                                    )

                                # Run selector
                                _aio.run(
                                    propose_options_hedge_with_selector(
                                        runs_dir=str(self.runs_dir),
                                        exposure=exposure,
                                        intent=intent,
                                    )
                                )
                            else:
                                # Fallback to legacy approach
                                from scripts.ai_trader.risk.options_hedge import propose_options_hedge
                                propose_options_hedge(self.runs_dir, intent="kill_switch:" + act)
                        except Exception as e:
                            self.log.warning("options_hedge_proposal_failed", err=str(e), exc_info=True)
                        # Execute hedge plan (dry-run by default; set AI_TRADER_EXEC_HEDGE=1 to submit)
                        try:
                            # Read hedge execution policy from config.yml (risk_management.hedge)
                            hcfg = (self.cfg.get("risk_management", {}) or {}).get("hedge", {})
                            mode = str(hcfg.get("mode", "both"))
                            auto_exec = bool(hcfg.get("auto_execute", False))
                            cmd = f"venv/bin/python scripts/ai_trader/portfolio/execute_hedge.py --runs-dir {self.runs_dir} --mode {mode} " + ("--execute" if auto_exec else "")
                            self._run_cmd(cmd, step="execute_hedge")
                        except Exception as e:
                            self.log.warning("execute_hedge_failed", err=str(e), exc_info=True)
                except Exception as e:
                    self.log.warning("risk_kill_switch_failed", err=str(e), exc_info=True)
                # Watchdog for llm_trader tools (best effort)
                try:
                    self._scan_and_recover_tools()
                except Exception as e:
                    self.log.warning("tools_watchdog_failed", err=str(e), exc_info=True)
                # Export events intraday (best-effort)
                try:
                    from scripts.ai_trader.exporters.events_exporter import export_events as export_ev
                    export_ev(str(self.runs_dir), datetime.date.today().isoformat(), session="intraday")
                except Exception:
                    pass
                return {"status": "ok", "mode": mode, "executed_plan": plan, **({"memory_snapshot": snap} if snap else {})}
            except Exception:
                pass
        if mode == "fast":
            self.llm.start_rth_fast()
        else:
            self.llm.start_rth_full()
        snap = self._maybe_write_intraday_memory_snapshot()
        try:
            self._process_guardian_events()
        except Exception as e:
            self.log.warning("guardian_process_failed", err=str(e), exc_info=True)
        try:
            self._scan_and_recover_tools()
        except Exception as e:
            self.log.warning("tools_watchdog_failed", err=str(e), exc_info=True)
        # Export events intraday (best-effort)
        try:
            from scripts.ai_trader.exporters.events_exporter import export_events as export_ev
            export_ev(str(self.runs_dir), datetime.date.today().isoformat(), session="intraday")
        except Exception:
            pass
        return {"status": "ok", "mode": mode, **({"memory_snapshot": snap} if snap else {})}

    def afterhours_sequence(self):
        if self.brain_enabled:
            try:
                # Skip if no triggers and in low-activity session
                if not self._should_run_quiet_session("afterhours"):
                    return {"status": "skip", "reason": "afterhours_quiet_no_triggers"}
                mem = {}
                try:
                    mem = memory_summary("afterhours", lookback_days=MEMORY_LOOKBACK_DAYS)
                except Exception:
                    mem = {}
                _h = self._adjust_health_for_session("afterhours", self.health.check())
                plan = self._propose_plan_cached("afterhours", {"health": _h, "memory": mem, "time": self._time_context()})
                # News TTL-based refresh for plan symbols (single re-ask)
                try:
                    plan = self._maybe_inject_need_news("afterhours", plan)
                    need = list(plan.get("need_news") or []) if isinstance(plan, dict) else []
                    if need:
                        fresh = self._get_news_analysis_for(need)
                        if fresh:
                            _h = self._adjust_health_for_session("afterhours", self.health.check())
                            plan = self._propose_plan_cached("afterhours", {"health": _h, "memory": mem, "news": fresh, "time": self._time_context()})
                except Exception as _e:
                    self.log.warning("afterhours_news_on_demand_failed", err=str(_e), exc_info=True)
                plan = self._normalize_or_build_plan("afterhours", plan, mem)
                overrides = self._prepare_prompt_overrides(plan)
                plan = self._risk_pretrade_guard(plan)
                self.llm.run_with_plan(plan, prompt_overrides=overrides)
                try:
                    self._persist_plan("afterhours", plan)
                    self._reconcile_processes_with_plan(plan)
                except Exception:
                    pass
                return {"status": "ok", "executed_plan": plan}
            except Exception:
                pass
        self.llm.start_afterhours()
        return {"status": "ok"}

    def overnight_sequence(self):
        if self.brain_enabled:
            try:
                # Skip if no triggers and in low-activity session
                if not self._should_run_quiet_session("overnight"):
                    return {"status": "skip", "reason": "overnight_quiet_no_triggers"}
                mem = {}
                try:
                    mem = memory_summary("overnight", lookback_days=MEMORY_LOOKBACK_DAYS)
                except Exception:
                    mem = {}
                _h = self._adjust_health_for_session("overnight", self.health.check())
                plan = self._propose_plan_cached("overnight", {"health": _h, "memory": mem, "time": self._time_context()})
                # News TTL-based refresh for plan symbols (single re-ask)
                try:
                    plan = self._maybe_inject_need_news("overnight", plan)
                    need = list(plan.get("need_news") or []) if isinstance(plan, dict) else []
                    if need:
                        fresh = self._get_news_analysis_for(need)
                        if fresh:
                            _h = self._adjust_health_for_session("overnight", self.health.check())
                            plan = self._propose_plan_cached("overnight", {"health": _h, "memory": mem, "news": fresh, "time": self._time_context()})
                except Exception as _e:
                    self.log.warning("overnight_news_on_demand_failed", err=str(_e), exc_info=True)
                plan = self._normalize_or_build_plan("overnight", plan, mem)
                overrides = self._prepare_prompt_overrides(plan)
                plan = self._risk_pretrade_guard(plan)
                self.llm.run_with_plan(plan, prompt_overrides=overrides)
                try:
                    self._persist_plan("overnight", plan)
                    self._reconcile_processes_with_plan(plan)
                except Exception:
                    pass
                return {"status": "ok", "executed_plan": plan}
            except Exception:
                pass
        self.llm.start_overnight()
        return {"status": "ok"}

    def maintenance_sequence(self):
        """Maintenance cycle: Ask Brain for maintenance tasks (Option ML, etc)."""
        if not self.brain_enabled:
            return {"status": "skipped", "reason": "brain_disabled"}
        
        # 1. Get Health (with Option ML status)
        # Force refresh to ensure latest option_ml status
        h_res = self.health.check(auto_refresh=True)
        h_data = h_res.get("raw", {})
        
        # 2. Context for Brain
        ctx = {
            "health": h_data,
            "time": self._time_context(),
        }
        
        # 3. Ask Brain
        try:
            # Force a prompt override for maintenance to avoid trading hallucinations
            ctx["_prompt_override"] = (
                "You are in MAINTENANCE mode. Do NOT propose any trading actions.\n"
                "Your ONLY goal is to schedule maintenance tasks based on health status.\n"
                "Task: 'option_ml_train'.\n"
                "User explicit request: Run Option ML for full month of December 2025.\n"
                "Return STRICT JSON with this exact schema:\n"
                "{\n"
                "  \"session\": \"maintenance\",\n"
                "  \"maintenance\": [\n"
                "    {\"action\": \"option_ml_train\", \"params\": {\"start\": \"2025-12-01\", \"end\": \"2025-12-31\", \"symbols\": [\"SPY\", \"QQQ\", \"IWM\", \"AAPL\", \"MSFT\", \"NVDA\", \"TSLA\"]}}\n"
                "  ]\n"
                "}\n"
                "Ensure you use the key 'action', NOT 'task'."
            )
            plan = self._propose_plan_cached("maintenance", ctx)
            self.log.info("maintenance_plan_received", plan=plan)
        except Exception as e:
            self.log.error("maintenance_plan_failed", err=str(e))
            return {"status": "error", "error": str(e)}
            
        # 4. Execute Plan
        results = {}
        maint = plan.get("maintenance", [])
        if maint:
            for task in maint:
                action = task.get("action")
                params = task.get("params", {})
                if action == "option_ml_train":
                    try:
                        self.log.info("executing_option_ml_from_brain", params=params)
                        self.rs.option_ml_train(
                            days=params.get("days", 90),
                            start=params.get("start"),
                            end=params.get("end"),
                            symbols=",".join(params.get("symbols", [])) if params.get("symbols") else None,
                            auto_model_selection=True
                        )
                        results["option_ml"] = "success"
                    except Exception as e:
                        self.log.error("option_ml_execution_failed", err=str(e))
                        results["option_ml"] = f"error: {str(e)}"
                else:
                    self.log.warning("unknown_maintenance_task", action=action, params=params)
                    results[str(action)] = "skipped_unknown"
        else:
            self.log.info("no_maintenance_tasks_planned")
        
        return {"status": "ok", "executed": results}

    def evening_learning(self):
        # Check if ML is paused by control plane
        try:
            dstr = datetime.date.today().isoformat()
            lockp = self.runs_dir / dstr / "locks" / "ml_pause.lock"
            if lockp.exists():
                self.log.info("learning_paused_by_lock", lock=str(lockp))
                return {"status": "skipped", "reason": "ml_paused", "lock": str(lockp)}
        except Exception:
            pass
        # STEP 0: 强制数据自愈 (优先确保 features_1m 有数据)
        try:
            if getattr(self, "data_healer", None) is not None:
                # 优先从策略配置推导 universe；缺省时用一组核心标的
                syms = self._strategy_symbols_from_config()
                if not syms:
                    syms = ["SPY", "QQQ", "IWM", "TSLA", "NVDA", "AAPL", "AMD", "AMZN", "MSFT"]
                self.log.info("evening_learning_data_check_start", symbols_count=len(syms))
                ok = self.data_healer.ensure_data_ready(syms, lookback_days=2)
                if not ok:
                    self.log.error(
                        "evening_learning_aborted_data_void",
                        notes="DataHealer failed after max retries",
                    )
                    return {
                        "status": "failed",
                        "reason": "critical_data_void",
                        "action": "check_ibkr_connection_manual",
                    }
                self.log.info("evening_learning_data_check_passed")
        except Exception as e:
            self.log.warning("evening_learning_healer_warning", err=str(e))
        # ✅ OOM/OpenMP hotfix: Set thread/BLAS env vars BEFORE any ML work
        import os
        env_overrides = {
            "OMP_NUM_THREADS": "2",
            "MKL_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "NUMEXPR_MAX_THREADS": "2",
            "OMP_NESTED": "FALSE",
            "OMP_WAIT_POLICY": "PASSIVE",
            # Allow multiple OpenMP runtimes (PyTorch + XGBoost + sklearn) in the same process.
            # Without this, macOS can segfault inside libomp when different wheels ship their own libomp.dylib.
            "KMP_DUPLICATE_LIB_OK": "TRUE",
        }
        for key, val in env_overrides.items():
            os.environ[key] = val
        self.log.info("env_overrides_applied", env=env_overrides)

        actions: Dict[str, Any] = {}
        # Determine if today's data is incomplete; if so, we will not block learning but mark skip
        learn_skipped_today = False
        train_end_date = None
        try:
            dstr = datetime.date.today().isoformat()
            out_dir = self.runs_dir / dstr
            dq_path = out_dir / "data_quality.json"
            if dq_path.exists():
                dqj = json.loads(dq_path.read_text("utf-8"))
                if not bool(dqj.get("ok", True)):
                    learn_skipped_today = True
            else:
                # Best-effort re-check minimal freshness by reusing DataGate
                dres = self.data.check()
                if not bool(dres.get("ok", True)):
                    learn_skipped_today = True

                    # 🆕 P1.1: Auto-trigger data sync when DataGate fails
                    try:
                        from libs.ml.auto_remediation import AutoRemediator
                        from datetime import date as _date, timedelta as _timedelta

                        self.log.warning("data_gate_failed_triggering_auto_sync",
                                       issues=dres.get("issues", []))

                        # Get trading universe symbols
                        symbols = self.cfg.get("trading_universe", ["AAPL", "TSLA"])

                        # Default sync window: last 7 days
                        end_date = _date.today()
                        start_date = end_date - _timedelta(days=7)

                        # Trigger remediation for each symbol
                        remediator = AutoRemediator(dry_run=False)
                        for sym in symbols[:3]:  # Limit to top 3 symbols to avoid long sync
                            try:
                                self.log.info("triggering_auto_sync", symbol=sym)
                                # Note: _remediate_sync expects DataReadinessResult, but we can call sync directly
                                import subprocess
                                import sys as _sys
                                sync_cmd = [
                                    _sys.executable,
                                    "tools/sync_features_active.py",
                                    "--symbols", sym,
                                    "--start", f"{start_date} 09:30:00",
                                    "--end", f"{end_date} 16:00:00",
                                    "--chunk-days", "1"
                                ]
                                result = subprocess.run(sync_cmd, capture_output=True, text=True, timeout=300)
                                if result.returncode == 0:
                                    self.log.info("auto_sync_success", symbol=sym,
                                                 stdout=result.stdout[-200:])
                                else:
                                    self.log.error("auto_sync_failed", symbol=sym,
                                                  stderr=result.stderr[-200:])
                            except Exception as sync_err:
                                self.log.error("auto_sync_exception", symbol=sym,
                                             error=str(sync_err))
                    except Exception as heal_err:
                        self.log.warning("auto_healing_trigger_failed", error=str(heal_err))

            if learn_skipped_today:
                # previous business day (simple: minus 1 day; refine if weekend when needed)
                prev = datetime.date.today() - datetime.timedelta(days=1)
                train_end_date = prev.isoformat()
        except Exception:
            pass
        # Optional: auto-apply last model-space suggestion to HPO config (AI Brain controlled)
        try:
            auto_model_patch = self._auto_apply_model_space_patch()
            if auto_model_patch:
                actions["model_space_autopatch"] = auto_model_patch
        except Exception as e:
            self.log.warning("model_space_autopatch_failed", err=str(e), exc_info=True)

        # A) Self‑Reflection: per‑decision consistency + prompt suggestions
        try:
            # Compute reflection (per-decision consistency)
            out_dir = self.runs_dir / datetime.date.today().isoformat()
            out_dir.mkdir(parents=True, exist_ok=True)
            ref_json = out_dir / 'reflection.json'
            # Run job via Python module
            self._run_cmd(
                f"venv/bin/python -m scripts.ai_trader.reflection.consistency_job --date {datetime.date.today().isoformat()} --runs-dir {self.runs_dir}",
                step="reflection_consistency",
            )
            actions['reflection'] = str(ref_json)
            # Evolve prompts (auto-apply = true)
            sugg_out = out_dir / 'prompt_suggestions.json'
            self._run_cmd(
                f"venv/bin/python scripts/ai_trader/evolvers/prompt_evolver.py --reflection {ref_json} --out {sugg_out} --auto-apply",
                step="prompt_evolver",
            )
            actions['prompt_suggestions'] = str(sugg_out)
        except Exception as e:
            # Distinguish known subprocess failures vs unexpected exceptions
            try:
                import subprocess as _sp
                if isinstance(e, _sp.CalledProcessError):
                    self.log.error("reflection_job_failed", returncode=e.returncode, err=str(e), exc_info=True)
                    actions['reflection_error'] = str(e)
                else:
                    self.log.exception("reflection_unexpected_error")
                    raise
            except Exception:
                # If classification failed, re-raise to surface unknown error
                raise
        # Fail-fast if reflection failed
        if self.fail_fast and actions.get('reflection_error'):
            return {"status": "error", "actions": actions}

        # B) Adaptive Retry Loop for Alpha Optimization
        # Load retry budget configuration
        gate_cfg = {}
        budget_cfg = {}
        try:
            import yaml as _yaml
            cfg_yml = _yaml.safe_load(Path("config.yml").read_text("utf-8"))
            gate_cfg = (cfg_yml.get("policies", {}) or {}).get("hpo_deploy", {})
            budget_cfg = (cfg_yml.get("policies", {}) or {}).get("hpo_budget", {})
        except Exception as e:
            self.log.warning("hpo_config_load_failed", err=str(e))
            gate_cfg = {}
            budget_cfg = {}

        if not self.rs.cmds.get("alpha_loop"):
            self.log.info("alpha_loop_skipped", reason="command_disabled")
            max_rounds = 0
        else:
            max_rounds = int(budget_cfg.get("max_rounds", 4))

        time_budget_min = int(budget_cfg.get("time_budget_min", 60)) if budget_cfg else 60
        seed_sweep = budget_cfg.get("seed_sweep", [42, 1337, 2025]) if budget_cfg else [42, 1337, 2025]
        shrink_factor = float(budget_cfg.get("shrink_factor", 0.15)) if budget_cfg else 0.15
        # Provider fallback order per round; default to pure Optuna to reduce memory pressure
        provider_fallback = budget_cfg.get("provider_fallback", ["optuna"]) or ["optuna"] if budget_cfg else ["optuna"]

        start_time = time.time()
        time_budget_sec = time_budget_min * 60

        retry_audit = []  # Audit each round
        best_oos = None  # Best result across all rounds

        for round_idx in range(max_rounds):
            # Check time budget
            elapsed = time.time() - start_time
            if elapsed >= time_budget_sec:
                retry_audit.append({
                    "round": round_idx,
                    "status": "budget_exhausted",
                    "elapsed_sec": elapsed,
                    "reason": f"time_budget={time_budget_min}min exceeded"
                })
                self.log.info("hpo_retry_timeout", round=round_idx, elapsed_sec=elapsed, budget_sec=time_budget_sec)
                break

            # Select round parameters
            seed = seed_sweep[round_idx % len(seed_sweep)]
            provider = provider_fallback[min(round_idx, len(provider_fallback) - 1)]

            self.log.info("hpo_retry_round_start", round=round_idx, seed=seed, provider=provider, elapsed_sec=elapsed)

            # Run alpha_loop with current configuration using safe wrapper
            # Includes memory guard and graceful degradation
            try:
                from utils.alpha_loop_wrapper import run_alpha_loop_safe, apply_config_degradation
                from utils.degrade_ladder import DegradeLadder

                # ✅ OOM hotfix: FORCE Round-0 pre-degrade BEFORE any execution
                # Read safe baseline from config
                safe_baseline = self.cfg.get("policies", {}).get("hpo_safe_baseline", {})
                should_pre_degrade = False
                pre_degrade_reason = None

                if round_idx == 0:
                    # Always pre-degrade Round 0 to safe baseline
                    should_pre_degrade = True
                    pre_degrade_reason = "round_0_policy"
                    self.log.warning("round_0_pre_degrade_forced", reason=pre_degrade_reason, baseline=safe_baseline)

                # Apply degradation to config for this round
                degrade_ladder = DegradeLadder()
                if should_pre_degrade:
                    # Force Level 1 degradation for Round 0
                    degrade_cfg = degrade_ladder.apply(1, failure_reason=pre_degrade_reason)
                else:
                    degrade_cfg = degrade_ladder.apply(round_idx, failure_reason="hpo_retry")

                # Create degraded config file for this round
                degraded_config_path = f"alpha_config_enhanced_round_{round_idx}.json"
                apply_config_degradation(
                    "alpha_config_enhanced.json",
                    degraded_config_path,
                    degrade_cfg,
                    round_idx=round_idx
                )

                # Determine memory budget (reduce for higher rounds)
                # Read from config.yml policies.hpo_budget.mem_budget_gb with sensible fallback
                try:
                    base_mem_gb = float(((self.cfg.get("policies") or {}).get("hpo_budget") or {}).get("mem_budget_gb", 12.0))
                except Exception:
                    base_mem_gb = 12.0
                # Reduce slightly per round but keep a floor relative to base
                max_mem_gb = base_mem_gb - (round_idx * 1.0)
                max_mem_gb = max(max_mem_gb, max(4.0, base_mem_gb * 0.6))

                result = run_alpha_loop_safe(
                    config_path=degraded_config_path,
                    out_dir="alpha_out/daily",
                    gens=2,
                    k_top=10,
                    provider=provider,
                    round_idx=round_idx,
                    max_mem_gb=max_mem_gb,
                    seed=seed
                )

                self.log.info("alpha_loop_safe_completed", round=round_idx, mem_peak_mb=result["mem_stats"]["peak_rss_mb"])

            except Exception as e:
                self.log.warning("alpha_loop_failed", round=round_idx, err=str(e), exc_info=True)
                retry_audit.append({
                    "round": round_idx,
                    "status": "alpha_loop_failed",
                    "seed": seed,
                    "provider": provider,
                    "error": str(e),
                    "elapsed_sec": time.time() - start_time
                })
                continue

            # Discover best params from this round
            best = self._discover_best_params()
            if not best or not best.get("best_params"):
                self.log.warning("hpo_no_best_params", round=round_idx)
                retry_audit.append({
                    "round": round_idx,
                    "status": "no_best_params",
                    "seed": seed,
                    "provider": provider,
                    "elapsed_sec": time.time() - start_time
                })
                continue

            # Gate check
            oos = best.get("oos") or {}
            diagnostics = best.get("diagnostics")
            gate_res = self._hpo_deploy_gate(oos, gate_cfg)

            retry_audit.append({
                "round": round_idx,
                "seed": seed,
                "provider": provider,
                "elapsed_sec": time.time() - start_time,
                "oos_metrics": gate_res.get("metrics", {}),
                "gate_ok": gate_res.get("ok", False),
                "gate_reasons": gate_res.get("reasons", [])
            })

            self.log.info("hpo_retry_round_complete",
                         round=round_idx,
                         gate_ok=gate_res["ok"],
                         sharpe=gate_res.get("metrics", {}).get("oos_sharpe", 0),
                         reasons=gate_res.get("reasons", []))

            # If passed gate, we're done!
            if gate_res.get("ok"):
                best_oos = {
                    "params": best["best_params"],
                    "oos": oos,
                    "gate": gate_res,
                    "diagnostics": diagnostics,
                }
                retry_audit[-1]["status"] = "success_converged"
                self.log.info("hpo_retry_converged", round=round_idx, total_rounds=round_idx + 1)
                break
            else:
                # Save this round as best if better than previous attempts
                if not best_oos or oos.get("oos_sharpe", 0) > best_oos.get("oos", {}).get("oos_sharpe", 0):
                    best_oos = {
                        "params": best["best_params"],
                        "oos": oos,
                        "gate": gate_res,
                        "diagnostics": diagnostics,
                    }

                retry_audit[-1]["status"] = "gate_failed_retry"

        # Save audit to file
        actions["hpo_retry_audit"] = retry_audit
        try:
            out_dir = self.runs_dir / datetime.date.today().isoformat()
            out_dir.mkdir(parents=True, exist_ok=True)
            audit_path = out_dir / "hpo_retry_audit.json"
            audit_path.write_text(json.dumps(retry_audit, indent=2), encoding="utf-8")
            try:
                self._mirror_learning_artifact(audit_path, artifact_name="hpo_retry_audit")
            except Exception:
                pass
            self.log.info("hpo_retry_audit_saved", path=str(audit_path), total_rounds=len(retry_audit))
        except Exception as e:
            self.log.warning("hpo_retry_audit_save_failed", err=str(e))

        # Final decision: apply params if converged, otherwise skip
        actions["alpha_loop"] = "done"
        best = best_oos  # Use best result from retry loop

        # Publish HPO completion event to event bus
        try:
            from libs.event_bus import publish
            learn_obs = self._learning_observability()
            hpo_event = {
                "event": "hpo_complete",
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "metrics": (best.get("oos") if best else {}),
                "result_path": str(self.runs_dir / datetime.date.today().isoformat() / "hpo_retry_audit.json"),
                "passed_gate": bool(best and best.get("gate", {}).get("ok")),
                "total_rounds": len(retry_audit),
                "best_provider": (best.get("provider") if best else None),
                "strategy_id": learn_obs.get("strategy_id"),
                "learning_profile": learn_obs.get("learning_profile"),
                "adapter_name": learn_obs.get("adapter_name"),
                "pipeline_version": learn_obs.get("pipeline_version"),
                "learning_targets": learn_obs.get("learning_targets"),
            }
            publish('control', hpo_event, key="ai_trader")
            self.log.info("hpo_complete_event_published", passed_gate=hpo_event["passed_gate"])
        except Exception as e:
            self.log.warning("hpo_event_publish_failed", err=str(e))

        if best and best.get("params"):
            # Have params, check gate status
            gate_res = best.get("gate", {})
            actions["hpo_deploy_check"] = gate_res

            if gate_res.get("ok"):
                # Gate passed - proceed with apply
                # 3) Apply params to config (enhanced strategy via apply_extras_params.py)
                params_path = self._write_temp_best_params(best["params"])  # returns file path
                # 3.2) Ask Control Brain for decision and AB weights
                decision = None
                chmp, chall = self._build_ab_inputs(best)
                try:
                    decision_ctx = {
                        "champion": chmp,
                        "challenger": chall,
                        "oos": best.get("oos") or {},
                    }
                    decision = self._brain_decide("hpo_deploy", decision_ctx) if self.brain_enabled else None
                    decision = self._validate_hpo_deploy_decision(decision) if decision is not None else None
                    # Persist decision artifacts
                    outd = self.runs_dir / datetime.date.today().isoformat()
                    outd.mkdir(parents=True, exist_ok=True)
                    hpo_decision_path = outd / "hpo_decision.json"
                    hpo_decision_path.write_text(json.dumps({"input": decision_ctx, "decision": decision}, indent=2), encoding="utf-8")
                    try:
                        self._mirror_learning_artifact(hpo_decision_path, artifact_name="hpo_decision")
                    except Exception:
                        pass
                    actions["hpo_decision"] = decision
                except Exception as e:
                    self.log.warning("hpo_decide_failed", err=str(e), exc_info=True)
                    actions["hpo_decision_error"] = str(e)

                # 3.3) Apply only if policy gate ok AND brain approves (or brain absent)
                brain_approve = True if decision is None else bool(decision.get("apply_params", False))
                if brain_approve:
                    try:
                        # apply_extras_params.py 的固定 flags 已在 commands.apply_params 中给出
                        apply_result = self._apply_params_with_scope(hpo_params_path=params_path)
                        actions["apply_params"] = {
                            "params_file": params_path,
                            "rounds": len(retry_audit),
                            "result": apply_result,
                        }
                        if not (apply_result.get("applied") or []):
                            raise RuntimeError(f"no targets applied: {apply_result.get('failed')}")
                    except Exception as e:
                        self.log.error("apply_params_failed", err=str(e), exc_info=True)
                        actions["apply_params_error"] = str(e)
                else:
                    actions["apply_params_skipped"] = True
                    actions["skip_reason"] = "brain_disapproved"

                # Fail-fast if applying params failed (critical mutation)
                if self.fail_fast and actions.get("apply_params_error"):
                    return {"status": "error", "actions": actions}

                # 4) AB proposal update registry (Champion vs Challenger)
                try:
                    # Use brain weights if provided, else default AB policy
                    if isinstance(decision, dict) and isinstance(decision.get("ab_weights"), dict):
                        plan = {"weights": decision.get("ab_weights"), "reason": "brain_override"}
                    else:
                        plan = self.ab.propose(chmp, chall)
                    reg = {
                        "champion": chmp,
                        "challenger": chall,
                        "weights": plan["weights"],
                        "updated_at": self._ny_now_iso(),
                    }
                    self.ab.save_registry(reg)
                    actions["ab"] = plan
                except Exception as e:
                    self.log.error("ab_update_failed", err=str(e), exc_info=True)
                    actions["ab_error"] = str(e)
            else:
                # Gate failed - generate stop-loss report
                actions["apply_params_skipped"] = True
                actions["skip_reason"] = "hpo_gate_failed"
                stop_loss_report = self._generate_stop_loss_report(retry_audit, best_oos)
                actions["stop_loss_report"] = stop_loss_report
                # Save report to file
                try:
                    out_dir = self.runs_dir / datetime.date.today().isoformat()
                    out_dir.mkdir(parents=True, exist_ok=True)
                    report_path = out_dir / "hpo_stop_loss_report.json"
                    report_path.write_text(json.dumps(stop_loss_report, indent=2), encoding="utf-8")
                    self.log.warning("hpo_failed_to_converge",
                                   total_rounds=len(retry_audit),
                                   best_sharpe=stop_loss_report.get("best_metrics", {}).get("oos_sharpe", 0),
                                   report_path=str(report_path))
                except Exception as e:
                    self.log.warning("stop_loss_report_save_failed", err=str(e))

        # C) Portfolio optimization (weights → per_symbol sizing multipliers)
        try:
            dstr = datetime.date.today().isoformat()
            out_dir = self.runs_dir / dstr
            out_dir.mkdir(parents=True, exist_ok=True)
            self._run_cmd(
                f"venv/bin/python scripts/ai_trader/portfolio/optimizer.py --config config.yml --apply",
                step="portfolio_optimize",
            )
            actions["portfolio_optimize"] = str(out_dir / "portfolio_apply.json")
        except Exception as e:
            self.log.warning("portfolio_optimize_failed", err=str(e), exc_info=True)
            actions["portfolio_optimize_error"] = str(e)

        # D) Portfolio hedging overlay (plan-only)
        try:
            self._run_cmd(
                f"venv/bin/python scripts/ai_trader/portfolio/overlay.py --config config.yml --runs-dir {self.runs_dir}",
                step="portfolio_overlay",
            )
            actions["portfolio_overlay"] = str(self.runs_dir / datetime.date.today().isoformat() / "overlay_hedge.json")
        except Exception as e:
            self.log.warning("portfolio_overlay_failed", err=str(e), exc_info=True)
            actions["portfolio_overlay_error"] = str(e)

        # E) Persist a memory episode to DB (ClickHouse)
        try:
            from scripts.ai_trader.memory.episode_store import store_episode
            mem = store_episode(
                runs_dir=self.cfg["paths"]["runs_dir"],
                session="learn",
                best=best,
                hpo_gate=actions.get("hpo_deploy_check"),
                notes=("; ".join(actions.get("hpo_deploy_check", {}).get("reasons", [])) if actions.get("hpo_deploy_check") else None),
            )
            actions["memory_episode"] = {k: v for k, v in mem.items() if k in ("saved", "save_error")}
        except Exception as e:
            self.log.warning("memory_episode_failed", err=str(e), exc_info=True)
            actions["memory_error"] = str(e)

        # F) Optional RL training + eval (Paper-only; best-effort)
        try:
            self.rs.rl_train()
            actions["rl_train"] = "triggered"
            rl_policy = (self.cfg.get("policies", {}) or {}).get("rl_deploy", {})
            min_sh = rl_policy.get("min_sharpe", RL_MIN_SHARPE)
            max_md = rl_policy.get("max_mdd", RL_MAX_MDD)
            rl_eval = self.rs.rl_eval(min_sharpe=min_sh, max_mdd=max_md)
            actions["rl_eval"] = rl_eval

            # Publish RL evaluation completion event
            try:
                from libs.event_bus import publish
                learn_obs = self._learning_observability()
                rl_event = {
                    "event": "rl_eval_complete",
                    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "metrics": (rl_eval.get("metrics") if isinstance(rl_eval, dict) else {}),
                    "result_path": str(self.runs_dir / datetime.date.today().isoformat() / "rl_eval.json"),
                    "passed_gate": bool(isinstance(rl_eval, dict) and rl_eval.get("gate", {}).get("ok")),
                    "gate_info": (rl_eval.get("gate") if isinstance(rl_eval, dict) else {}),
                    "strategy_id": learn_obs.get("strategy_id"),
                    "learning_profile": learn_obs.get("learning_profile"),
                    "adapter_name": learn_obs.get("adapter_name"),
                    "pipeline_version": learn_obs.get("pipeline_version"),
                    "learning_targets": learn_obs.get("learning_targets"),
                }
                publish('control', rl_event, key="ai_trader")
                self.log.info("rl_eval_complete_event_published", passed_gate=rl_event["passed_gate"])
            except Exception as e:
                self.log.warning("rl_event_publish_failed", err=str(e))
            # If RL gating ok, ensure rl.enabled remains true; otherwise advisor note
            if isinstance(rl_eval, dict) and not rl_eval.get("gate", {}).get("ok", True):
                actions["rl_enable_advice"] = "rl gate failed; consider disabling rl.enabled"
            # RL auto toggle (with brain approve) — highest priority safety/control
            if isinstance(rl_eval, dict) and isinstance(rl_eval.get("gate"), dict):
                target_enable = bool(rl_eval["gate"].get("ok", False))
                # Ask brain approval
                approve = True
                if self.brain_enabled:
                    try:
                        pend = {"pending": [{"action": ("enable_rl" if target_enable else "disable_rl"), "params": {}}]}
                        dec = self._brain_decide("execute_actions", pend)
                        if isinstance(dec, dict) and isinstance(dec.get("approve"), list):
                            amap = {it.get("action"): bool(it.get("ok")) for it in dec.get("approve")}
                            approve = bool(amap.get("enable_rl" if target_enable else "disable_rl", False))
                    except Exception as e:
                        self.log.warning("rl_brain_approve_failed", err=str(e), exc_info=True)
                        approve = True
                if approve:
                    try:
                        # Apply rl.enabled
                        best_params = {"rl.enabled": target_enable}
                        temp = self.runs_dir / datetime.date.today().isoformat() / "rl_toggle.json"
                        temp.parent.mkdir(parents=True, exist_ok=True)
                        temp.write_text(json.dumps(best_params, indent=2), encoding="utf-8")
                        toggle_result = self._apply_params_with_scope(hpo_params_path=str(temp))
                        actions["rl_toggle"] = {"enabled": target_enable, "result": toggle_result}
                        if not (toggle_result.get("applied") or []):
                            raise RuntimeError(f"rl_toggle_apply_failed: {toggle_result.get('failed')}")
                        # Log decision
                        rl_decision_path = self.runs_dir / datetime.date.today().isoformat() / "rl_decision.json"
                        rl_decision_path.write_text(
                            json.dumps({"target": target_enable, "rl_eval": rl_eval}, indent=2), encoding="utf-8"
                        )
                        try:
                            self._mirror_learning_artifact(rl_decision_path, artifact_name="rl_decision")
                        except Exception:
                            pass
                    except Exception as e:
                        self.log.error("rl_toggle_failed", err=str(e), exc_info=True)
                        actions["rl_toggle_error"] = str(e)
        except Exception as e:
            self.log.error("rl_train_or_eval_failed", err=str(e), exc_info=True)
            actions["rl_train_error"] = str(e)
        # RL is best-effort; do not fail-fast here

        # F2) Maintenance (Option ML, etc) via Brain
        try:
            maint_res = self.maintenance_sequence()
            actions["maintenance"] = maint_res
        except Exception as e:
            self.log.error("maintenance_sequence_failed", err=str(e), exc_info=True)
            actions["maintenance_error"] = str(e)

        # G) Market Context → Reflection layers (Diagnosis/Prescription) → Pending actions → Daily report
        try:
            dstr = datetime.date.today().isoformat()
            out_dir = self.runs_dir / dstr
            # context
            self._run_cmd(
                f"venv/bin/python scripts/ai_trader/context/context_analyzer.py --date {dstr} --runs-dir {self.runs_dir}",
                step="context_analyzer",
            )
            # dxp
            self._run_cmd(
                f"venv/bin/python scripts/ai_trader/reflection/analyzer.py --date {dstr} --runs-dir {self.runs_dir}",
                step="reflection_analyzer",
            )
            ctx = json.loads((out_dir/"context.json").read_text("utf-8")) if (out_dir/"context.json").exists() else {}
            dxp = json.loads((out_dir/"reflection_dxp.json").read_text("utf-8")) if (out_dir/"reflection_dxp.json").exists() else {}
            # build pending actions (review buffer)
            from scripts.ai_trader.evolvers.decision_engine import load_promotion_policy, build_pending_actions, write_pending_actions
            pol = load_promotion_policy()
            pen = build_pending_actions(dxp, actions.get("rl_eval") or {}, pol)
            pending_path = write_pending_actions(str(out_dir), pen)
            actions["pending_actions"] = pending_path
            # daily report
            from scripts.ai_trader.reflection.report_builder import build_daily_report
            report_path = build_daily_report(str(out_dir), ctx, dxp, {
                "hpo_gate": actions.get("hpo_deploy_check"),
                "ab": actions.get("ab"),
                "apply_params": actions.get("apply_params"),
            }, actions.get("rl_eval") or {})
            actions["daily_report"] = report_path
            # ML result structured review (best-effort)
            try:
                ml_ctx = {"date": dstr}
                # Attach HPO gate metrics and best OOS if present
                if isinstance(actions.get("hpo_deploy_check"), dict):
                    ml_ctx["hpo_gate"] = actions.get("hpo_deploy_check")
                try:
                    if best:
                        alpha_best: Dict[str, Any] = {
                            "oos": best.get("oos"),
                            "params": best.get("params") or best.get("best_params"),
                        }
                        # Attach rich diagnostics (labels, features, predictions) when available
                        if isinstance(best.get("diagnostics"), dict):
                            alpha_best["diagnostics"] = self._compress_diagnostics(best.get("diagnostics"))
                        # Attach current label_profile used for alpha HPO, if detectable
                        lp_cur = self._current_label_profile()
                        if lp_cur:
                            alpha_best["label_profile"] = lp_cur
                        ml_ctx["alpha_best"] = alpha_best
                except Exception:
                    pass
                # Summarize OOF evaluation per symbol (if exists)
                try:
                    oof_dir = out_dir / "oof_eval"
                    if oof_dir.exists():
                        oof_sum = {}
                        for fp in oof_dir.glob("*.json"):
                            try:
                                j = json.loads(fp.read_text("utf-8"))
                                oof_sum[fp.stem] = {k: j.get(k) for k in ("auc","ic","coverage")}
                            except Exception:
                                continue
                        if oof_sum:
                            ml_ctx["oof_eval"] = oof_sum
                except Exception:
                    pass
                # Budget snapshot (if configured)
                try:
                    ml_ctx["budget"] = ((self.cfg.get("policies") or {}).get("hpo_budget") or {})
                except Exception:
                    pass
                # Label profile catalogue (intraday regression profiles only)
                try:
                    lp_brief = self._load_label_profiles_brief()
                    if lp_brief:
                        ml_ctx["label_profiles"] = lp_brief
                except Exception:
                    pass
                # Attach basic schema and AVAILABLE_COLUMNS so the ML Brain
                # can see concrete f./m5./d. columns and a conservative
                # whitelist of core 1m features instead of guessing.
                try:
                    schemas: Dict[str, Any] = {}
                    cli = _get_ch_client()
                    for tbl in ("trading.features_1m", "trading.features_5m", "trading.features_1d_prev"):
                        try:
                            rows = cli.query(f"DESCRIBE TABLE {tbl}")
                            cols = []
                            for r in rows:
                                name = r.get("name") or r.get("column")
                                if not name:
                                    continue
                                cols.append({"name": name, "type": r.get("type")})
                            if cols:
                                schemas[tbl] = cols
                        except Exception:
                            continue
                    if schemas:
                        ml_ctx["schemas"] = schemas
                except Exception:
                    pass
                try:
                    syms = self._strategy_symbols_from_config()
                    if syms:
                        ml_ctx["available_columns"] = {
                            "by_symbol": self._build_available_columns(syms),
                            "base_alias": "f",
                        }
                except Exception:
                    pass
                # Attach a curated feature registry summary so the Brain can
                # see which logical features (beyond raw columns) are
                # recommended for 1m intraday use.
                try:
                    reg = get_default_registry()
                    ml_ctx["feature_registry"] = {
                        "version": reg.version,
                        "freq": "1m",
                        "features": [
                            {
                                "name": f.name,
                                "base_cols": f.base_cols,
                                "tags": f.tags,
                                "desc": f.desc,
                            }
                            for f in reg.list_for_freq("1m", exposed_only=True)
                        ],
                    }
                except Exception:
                    pass
                if self.brain_enabled:
                    review = self.brain.review_ml(ml_ctx)
                    ml_review_path = out_dir / "ml_review.json"
                    ml_review_path.write_text(json.dumps(review, indent=2), encoding="utf-8")
                    actions["ml_review"] = str(ml_review_path)
                    try:
                        self._mirror_learning_artifact(ml_review_path, artifact_name="ml_review")
                    except Exception:
                        pass

                    # Feature Engineering Agent: suggest feature improvements based on HPO results
                    try:
                        self.log.info("feature_engineering_agent_starting")
                        fe_suggestion = self._run_feature_engineering_agent(out_dir, best_oos, retry_audit)
                        if fe_suggestion:
                            fe_info: Dict[str, Any] = {
                                "suggestion_file": str(out_dir / "feature_engineering_suggestion.json"),
                            }
                            if isinstance(fe_suggestion, dict) and fe_suggestion.get("_autopatch") is not None:
                                fe_info["autopatch"] = fe_suggestion.get("_autopatch")
                            actions["feature_engineering"] = fe_info
                            self.log.info(
                                "feature_engineering_agent_completed",
                                programs_added=len(
                                    fe_suggestion.get("feature_programs_patch", {}).get("add_or_update", {})
                                ),
                                programs_deleted=len(
                                    fe_suggestion.get("feature_programs_patch", {}).get("delete", [])
                                ),
                            )
                    except Exception as fe_err:
                        self.log.warning("feature_engineering_agent_failed", err=str(fe_err), exc_info=True)

            except Exception as e:
                self.log.warning("ml_review_failed", err=str(e), exc_info=True)
            # MLflow logging (best-effort)
            try:
                from scripts.ai_trader.mlflow_utils import init_mlflow, start_run, log_metrics, log_params, log_artifacts, end_run
                mlf = (self.cfg.get("mlflow") or {})
                init_mlflow(mlf.get("tracking_uri"), mlf.get("experiment"), enabled=mlf.get("enabled", True))
                learn_obs = self._learning_observability()
                tags = {
                    "session": "learn",
                    "date": dstr,
                    "strategy_id": learn_obs.get("strategy_id"),
                    "learning_profile": learn_obs.get("learning_profile"),
                    "adapter_name": learn_obs.get("adapter_name"),
                    "pipeline_version": learn_obs.get("pipeline_version"),
                }
                start_run(run_name=f"learn_{dstr}", tags=tags)
                # Metrics
                gate = actions.get("hpo_deploy_check") or {}
                gm = gate.get("metrics") or {}
                rl = actions.get("rl_eval") or {}
                rl_gate = (rl.get("gate") or {})
                # Pending actions metrics
                pending_list = (pen.get("pending") if isinstance(pen, dict) else []) or []
                pa_counts = {"disable_rl": 0, "increase_conf_floor": 0, "reduce_sizing_multiplier": 0}
                for st in pending_list:
                    a = st.get("action")
                    if a in pa_counts:
                        pa_counts[a] += 1
                # Hedging aggregation (best-effort)
                hedg_cnt = 0
                hedg_notional = 0.0
                try:
                    import json as _J
                    hev = out_dir / "hedging_events.jsonl"
                    if hev.exists():
                        with hev.open("r", encoding="utf-8") as f:
                            for line in f:
                                line = line.strip()
                                if not line:
                                    continue
                                try:
                                    obj = _J.loads(line)
                                except Exception:
                                    continue
                                hedg_cnt += 1
                                try:
                                    hedg_notional += abs(float(obj.get("hedge_qty", 0.0)) * float(obj.get("ref_px", 0.0)))
                                except Exception:
                                    continue
                except Exception:
                    pass
                # Guardian aggregation (best-effort)
                guard_cnt = 0
                guard_max_dd = 0.0
                try:
                    import json as _J
                    gev = out_dir / "guardian_events.jsonl"
                    if gev.exists():
                        with gev.open("r", encoding="utf-8") as f:
                            for line in f:
                                line = line.strip()
                                if not line:
                                    continue
                                try:
                                    obj = _J.loads(line)
                                except Exception:
                                    continue
                                guard_cnt += 1
                                try:
                                    dd = float(obj.get("dd_pct", 0.0) or 0.0)
                                    if dd > guard_max_dd:
                                        guard_max_dd = dd
                                except Exception:
                                    continue
                except Exception:
                    pass
                # Overlay aggregation (best-effort)
                overlay_beta = 0.0
                overlay_need = 0.0
                try:
                    ov = out_dir / "overlay_hedge.json"
                    if ov.exists():
                        ovj = json.loads(ov.read_text("utf-8"))
                        overlay_beta = float(ovj.get("beta", 0.0) or 0.0)
                        overlay_need = 1.0 if bool(ovj.get("need_hedge", False)) else 0.0
                except Exception:
                    pass
                # Data Quality aggregation (best-effort)
                dq_ok = 1.0
                dq_failed_count = 0.0
                dq_passed_count = 0.0
                dq_failed_syms: list[str] = []
                try:
                    dq = out_dir / "data_quality.json"
                    if dq.exists():
                        dqj = json.loads(dq.read_text("utf-8"))
                        dq_ok = 1.0 if bool(dqj.get("ok", True)) else 0.0
                        dq_passed_count = float(len(dqj.get("passed") or []))
                        failed = dqj.get("failed") or {}
                        dq_failed_syms = list(failed.keys())
                        dq_failed_count = float(len(dq_failed_syms))
                except Exception:
                    pass
                # Tools (llm_trader) health aggregation (best-effort)
                tools_kill = 0
                tools_restart = 0
                tools_backoff = 0
                tools_running = 0
                try:
                    import json as _J
                    led = out_dir / "tools_ledger.jsonl"
                    if led.exists():
                        with led.open("r", encoding="utf-8") as f:
                            for line in f:
                                line = line.strip()
                                if not line:
                                    continue
                                try:
                                    obj = _J.loads(line)
                                except Exception:
                                    continue
                                ev = str(obj.get("event") or "")
                                if ev == "kill":
                                    tools_kill += 1
                                elif ev == "restart":
                                    tools_restart += 1
                                elif ev == "backoff":
                                    tools_backoff += 1
                    procs_dir = out_dir / "procs"
                    if procs_dir.exists():
                        for pmeta in procs_dir.glob("llm_*.json"):
                            try:
                                meta = _J.loads(pmeta.read_text("utf-8"))
                                if str(meta.get("status") or "") == "running":
                                    tools_running += 1
                            except Exception:
                                continue
                except Exception:
                    pass
                metrics = {
                    "oos_sharpe": gm.get("oos_sharpe", 0.0),
                    "oos_mdd": gm.get("oos_mdd", 0.0),
                    "oos_trades": gm.get("oos_trades", 0.0),
                    "rl_gate_ok": 1.0 if rl_gate.get("ok") else 0.0,
                    "pending_count": len(pending_list),
                    "pending_disable_rl": pa_counts["disable_rl"],
                    "pending_increase_conf_floor": pa_counts["increase_conf_floor"],
                    "pending_reduce_sizing_multiplier": pa_counts["reduce_sizing_multiplier"],
                    "hedging_events": hedg_cnt,
                    "hedging_notional_usd": hedg_notional,
                    "guardian_events": guard_cnt,
                    "guardian_max_dd_pct": guard_max_dd,
                    "overlay_beta": overlay_beta,
                    "overlay_need_hedge": overlay_need,
                    "dq_ok": dq_ok,
                    "dq_failed_count": dq_failed_count,
                    "dq_passed_count": dq_passed_count,
                    "tools_kill": float(tools_kill),
                    "tools_restart": float(tools_restart),
                    "tools_backoff": float(tools_backoff),
                    "tools_running": float(tools_running),
                }
                log_metrics(metrics)
                # Export to ClickHouse (Grafana)
                try:
                    from scripts.ai_trader.exporters.dash_exporter import export_metrics as dash_export
                    dash_export(run_name=f"pending_exec_{today.isoformat()}", session="morning", metrics=metrics, tags=tags)
                except Exception:
                    pass
                # Export to ClickHouse (Grafana)
                try:
                    from scripts.ai_trader.exporters.dash_exporter import export_metrics as dash_export
                    dash_export(run_name=f"learn_{dstr}", session="learn", metrics=metrics, tags=tags)
                except Exception:
                    pass
                # Params (best params count limited + promotion policy criteria)
                best_params = (best or {}).get("best_params") or {}
                bp_small = {k: best_params[k] for k in list(best_params.keys())[:50]}  # cap 50 keys
                # Policy criteria
                crit = ((pol.get("promotion_policy", {}) or {}).get("criteria") or pol.get("criteria") or {})
                pol_params = {f"policy_{k}": v for k, v in crit.items()}
                # DQ failed symbols (top up to 5 for brevity)
                dq_params = {}
                if dq_failed_syms:
                    for i, s in enumerate(dq_failed_syms[:5], start=1):
                        dq_params[f"dq_failed_sym_{i}"] = s
                extra = {}
                if learn_skipped_today:
                    extra["learn_skipped_today"] = True
                    if train_end_date:
                        extra["train_end_date"] = train_end_date
                log_params({**bp_small, **pol_params, **dq_params, **extra})
                # Artifacts
                arts = []
                try:
                    if best and best.get("file"):
                        arts.append(str(best.get("file")))
                except Exception:
                    pass
                for k in ("hpo_decision.json", "context.json", "reflection_dxp.json", "pending_actions.yml", "hedging_events.jsonl", "daily_report.md", "rl_eval.json", "tools_ledger.jsonl", "data_quality.json"):
                    p = out_dir / k
                    if p.exists():
                        arts.append(str(p))
                for k in ("portfolio_weights.json", "portfolio_apply.json", "overlay_hedge.json", "overlay_events.jsonl"):
                    p = out_dir / k
                    if p.exists():
                        arts.append(str(p))
                # Attach ML review if available
                p = out_dir / "ml_review.json"
                if p.exists():
                    arts.append(str(p))
                log_artifacts(arts)
                # Export events to ClickHouse (Grafana)
                try:
                    from scripts.ai_trader.exporters.events_exporter import export_events as export_ev
                    export_ev(str(self.runs_dir), dstr, session="learn")
                except Exception:
                    pass
                end_run()
            except Exception as e:
                self.log.warning("mlflow_logging_failed", err=str(e), exc_info=True)
                pass
        except Exception as e:
            self.log.error("report_block_failed", err=str(e), exc_info=True)
            actions["report_error"] = str(e)
        # Risk policy auto-adjustment (Brain-driven, safe whitelist) with idempotency
        try:
            if self.brain_enabled:
                # Check rate limit
                rate_limiting = self.cfg.get("policies", {}).get("rate_limiting", {})
                min_interval_sec = rate_limiting.get("risk_policy_min_interval_sec", 10800)

                dstr = datetime.date.today().isoformat()
                lock_file = self.runs_dir / dstr / "locks" / "risk_policy_last_run.lock"
                lock_file.parent.mkdir(parents=True, exist_ok=True)

                # Check if within rate limit window
                should_skip_rate_limit = False
                if lock_file.exists():
                    try:
                        mtime = lock_file.stat().st_mtime
                        elapsed = time.time() - mtime
                        if elapsed < min_interval_sec:
                            should_skip_rate_limit = True
                            self.log.info("risk_policy_rate_limited", elapsed_sec=elapsed, min_interval=min_interval_sec)
                    except Exception:
                        pass

                if not should_skip_rate_limit:
                    # Build context with current risk config and facts (if available)
                    rcfg = (self.cfg.get("risk_management") or {})
                    ctx = {"risk_config": rcfg}
                    facts_data = None
                    try:
                        dxp = json.loads((self.runs_dir / dstr / "reflection_dxp.json").read_text("utf-8"))
                        facts_data = dxp.get("facts")
                        ctx["facts"] = facts_data
                    except Exception:
                        pass

                    # Calculate facts signature for idempotency
                    import hashlib
                    sig_input = json.dumps({
                        "risk_config": rcfg,
                        "facts": facts_data,
                        "date": dstr
                    }, sort_keys=True)
                    facts_sig = hashlib.sha256(sig_input.encode()).hexdigest()[:16]

                    # Check if same facts signature exists and is recent
                    sig_file = self.runs_dir / dstr / "locks" / f"risk_policy_sig_{facts_sig}.lock"
                    should_skip_same_facts = False
                    if sig_file.exists():
                        try:
                            sig_mtime = sig_file.stat().st_mtime
                            sig_elapsed = time.time() - sig_mtime
                            if sig_elapsed < min_interval_sec:
                                should_skip_same_facts = True
                                self.log.info("risk_policy_skipped_same_facts", facts_sig=facts_sig, elapsed_sec=sig_elapsed)
                                # Write audit
                                audit_file = self.runs_dir / dstr / "risk_policy_audit.jsonl"
                                audit_entry = {
                                    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                                    "action": "skipped_same_facts",
                                    "facts_sig": facts_sig,
                                    "elapsed_sec": sig_elapsed,
                                    "reason": "no_new_evidence"
                                }
                                with open(audit_file, "a") as f:
                                    f.write(json.dumps(audit_entry) + "\n")
                        except Exception:
                            pass

                    if not should_skip_same_facts:
                        policy = self._brain_decide("risk_policy", ctx)
                        if isinstance(policy, dict) and policy.get("apply"):
                            self._apply_risk_policy(policy)
                            # Touch locks
                            lock_file.touch()
                            sig_file.touch()
                            self.log.info("risk_policy_applied", facts_sig=facts_sig)
        except Exception as e:
            self.log.warning("risk_policy_apply_failed", err=str(e), exc_info=True)

        # Fail-fast if report chain failed
        if self.fail_fast and actions.get("report_error"):
            # Persist progress snapshot for observability
            try:
                dstr = datetime.date.today().isoformat()
                out_dir = self.runs_dir / dstr
                out_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / "evening_status.json").write_text(json.dumps({"status": "error", "actions": actions}, indent=2), encoding="utf-8")
            except Exception:
                pass

        # Director-level ML research plan (best-effort, with downstream autopatch when enabled)
        try:
            dstr = datetime.date.today().isoformat()
            out_dir = self.runs_dir / dstr
            out_dir.mkdir(parents=True, exist_ok=True)
            # Load recent intraday PnL/trade metrics (best-effort, from ClickHouse), with JSON fallback
            recent_intraday_metrics: Dict[str, Any] = self._load_recent_intraday_metrics()
            if not recent_intraday_metrics:
                try:
                    mem_file = out_dir / "intraday_memory_summary.json"
                    if mem_file.exists():
                        recent_intraday_metrics = json.loads(mem_file.read_text("utf-8")) or {}
                except Exception:
                    recent_intraday_metrics = {}
            # Load ML actions summary (if produced earlier in the day)
            branch_summary: Optional[Dict[str, Any]] = None
            summary_fp = out_dir / "ml_branches_summary.json"
            if summary_fp.exists():
                try:
                    branch_summary = json.loads(summary_fp.read_text("utf-8"))
                except Exception:
                    branch_summary = None
            # Derive deltas_by_stage / stage_health from branch_summary (best-effort)
            deltas_by_stage: Dict[str, float] = {}
            stage_health: Dict[str, str] = {}
            try:
                agg = ((branch_summary or {}).get("aggregates") or {}).get("by_stage") or {}
                for stage, stats in agg.items():
                    try:
                        ic_val = float(stats.get("avg_ic", 0.0) or 0.0)
                    except Exception:
                        ic_val = 0.0
                    deltas_by_stage[stage] = ic_val
                    try:
                        ok_rate = float(stats.get("ok_rate", 0.0) or 0.0)
                    except Exception:
                        ok_rate = 0.0
                    if ok_rate >= 0.6:
                        health = "OK"
                    elif ok_rate <= 0.3:
                        health = "WEAK"
                    else:
                        health = "UNKNOWN"
                    stage_health[stage] = health
            except Exception:
                deltas_by_stage = {}
                stage_health = {}
            director_ctx: Dict[str, Any] = {
                "meta": {
                    "as_of": dstr,
                    "tz": "America/New_York",
                },
                "constraints": {
                    "no_overnight": True,
                    "target_daily_pnl_pct": 1.0,
                    "max_intraday_drawdown_pct": 2.0,
                },
                "recent_intraday_metrics": recent_intraday_metrics,
                "branch_summary": branch_summary or {},
                "deltas_by_stage": deltas_by_stage,
                "stage_health": stage_health,
            }
            director_plan: Dict[str, Any] = {}
            director_crit: Optional[Dict[str, Any]] = None
            if self.brain_enabled:
                try:
                    director_plan = self.brain.plan_research(director_ctx)
                except Exception as e:
                    self.log.warning("plan_research_failed", err=str(e), exc_info=True)
                    director_plan = {}
                # Validate Director output with Critic (best-effort; non-blocking safety layer)
                try:
                    if isinstance(director_plan, dict) and director_plan:
                        crit_constraints = {
                            "max_intraday_drawdown_pct": float(
                                (director_ctx.get("constraints") or {}).get("max_intraday_drawdown_pct", 2.0)
                            ),
                            "allowed_paths": ["focus", "horizon_days", "success_criteria.*"],
                        }
                        director_crit = self.brain.validate_agent_output(
                            actor="director",
                            proposal=director_plan,
                            constraints=crit_constraints,
                        )
                except Exception as e:
                    self.log.warning("director_critic_failed", err=str(e), exc_info=True)
                    director_crit = None
                try:
                    plan_fp = out_dir / "ml_research_plan.json"
                    payload = {"plan": director_plan}
                    if director_crit is not None:
                        payload["critic"] = director_crit
                    plan_fp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
                    actions["ml_research_plan"] = str(plan_fp)
                except Exception:
                    pass
            # Model-space suggestion (used by _auto_apply_model_space_patch for next HPO); Execution suggestion when focus==execution
            try:
                focus = str(((director_plan.get("changes") or {}).get("focus"))).lower() if isinstance(director_plan, dict) else ""
                # --- Model Expert ---
                if self.brain_enabled and focus == "model":
                    model_ctx: Dict[str, Any] = {
                        "meta": {"as_of": dstr, "tz": "America/New_York"},
                        "constraints": director_ctx.get("constraints") or {},
                        "hpo_report": {
                            "best_oos": best.get("oos") if best else {},
                            "retry_audit": retry_audit,
                        },
                    }
                    full_cfg: Optional[Dict[str, Any]] = None
                    # Try to load current HPO/model space config (alpha_config_enhanced.json)
                    try:
                        cfg_path = Path("alpha_config_enhanced.json")
                        if cfg_path.exists():
                            full_cfg = json.loads(cfg_path.read_text("utf-8"))
                            hpo_cfg = (full_cfg.get("hpo") or {})
                            current_space: Dict[str, Any] = {}
                            if "grid" in hpo_cfg:
                                current_space["grid_keys"] = sorted(list((hpo_cfg.get("grid") or {}).keys()))
                            if "model_pool" in full_cfg:
                                current_space["model_pool"] = {"enabled": bool((full_cfg.get("model_pool") or {}).get("enabled", False))}
                            if full_cfg.get("label_profile"):
                                current_space["label_profile"] = full_cfg.get("label_profile")
                            model_ctx["current_space"] = current_space
                    except Exception:
                        full_cfg = None
                    try:
                        suggestion = self.brain.plan_model_space(model_ctx)
                    except Exception as e:
                        self.log.warning("plan_model_space_failed", err=str(e), exc_info=True)
                        suggestion = {}
                    # Validate Model Expert output with Critic (best-effort)
                    suggestion_crit: Optional[Dict[str, Any]] = None
                    try:
                        if isinstance(suggestion, dict) and suggestion:
                            crit_constraints = {
                                "max_intraday_drawdown_pct": float(
                                    (director_ctx.get("constraints") or {}).get("max_intraday_drawdown_pct", 2.0)
                                ),
                                "allowed_paths": ["space.grid.*", "hpo.next_job.*"],
                            }
                            suggestion_crit = self.brain.validate_agent_output(
                                actor="model",
                                proposal=suggestion,
                                constraints=crit_constraints,
                            )
                    except Exception as e:
                        self.log.warning("model_critic_failed", err=str(e), exc_info=True)
                        suggestion_crit = None
                    try:
                        sugg_fp = out_dir / "ml_model_space_suggestion.json"
                        payload = {"plan": suggestion}
                        if suggestion_crit is not None:
                            payload["critic"] = suggestion_crit
                        sugg_fp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
                        actions["ml_model_space_suggestion"] = str(sugg_fp)
                    except Exception:
                        pass
                # --- Execution Expert ---
                if self.brain_enabled and focus == "execution":
                    exec_ctx: Dict[str, Any] = {
                        "meta": {"as_of": dstr, "tz": "America/New_York"},
                        "constraints": director_ctx.get("constraints") or {},
                        "recent_intraday_metrics": recent_intraday_metrics,
                    }
                    # Best-effort current gating from alpha_config_enhanced.json
                    try:
                        cfg_path = Path("alpha_config_enhanced.json")
                        current_gating: Dict[str, Any] = {}
                        risk_limits: Dict[str, Any] = {}
                        if cfg_path.exists():
                            cfg_full = json.loads(cfg_path.read_text("utf-8"))
                            risk_cfg = (((cfg_full.get("hpo") or {}).get("strategy_base_extras") or {}).get("risk") or {})
                            if isinstance(risk_cfg, dict):
                                if "time_stop_min" in risk_cfg:
                                    current_gating["time_stop_min"] = risk_cfg.get("time_stop_min")
                            risk_limits["max_intraday_drawdown_pct"] = float(
                                (director_ctx.get("constraints") or {}).get("max_intraday_drawdown_pct", 2.0)
                            )
                        exec_ctx["current_gating"] = current_gating
                        exec_ctx["risk_limits"] = risk_limits
                    except Exception:
                        pass
                    try:
                        exec_plan = self.brain.plan_execution(exec_ctx)
                    except Exception as e:
                        self.log.warning("plan_execution_failed", err=str(e), exc_info=True)
                        exec_plan = {}
                    exec_crit: Optional[Dict[str, Any]] = None
                    try:
                        if isinstance(exec_plan, dict) and exec_plan:
                            crit_constraints = {
                                "max_intraday_drawdown_pct": float(
                                    (director_ctx.get("constraints") or {}).get("max_intraday_drawdown_pct", 2.0)
                                ),
                                "allowed_paths": ["gating.*", "risk.*"],
                            }
                            exec_crit = self.brain.validate_agent_output(
                                actor="execution",
                                proposal=exec_plan,
                                constraints=crit_constraints,
                            )
                    except Exception as e:
                        self.log.warning("execution_critic_failed", err=str(e), exc_info=True)
                        exec_crit = None
                    # Persist execution suggestion and optionally apply limited autopatch
                    try:
                        exec_fp = out_dir / "ml_execution_suggestion.json"
                        payload: Dict[str, Any] = {"plan": exec_plan}
                        if exec_crit is not None:
                            payload["critic"] = exec_crit
                        # Optional: auto-apply safe subset (risk.time_stop_min grid) when Critic passes or absent
                        autopatch_exec: Optional[Dict[str, Any]] = None
                        try:
                            if isinstance(exec_plan, dict) and isinstance(exec_plan.get("changes"), dict):
                                if not (isinstance(exec_crit, dict) and exec_crit.get("valid") is False):
                                    autopatch_exec = self._auto_apply_execution_patch(exec_plan)
                        except Exception as e:
                            self.log.warning("execution_autopatch_failed", err=str(e), exc_info=True)
                            autopatch_exec = None
                        if autopatch_exec is not None:
                            payload["autopatch"] = autopatch_exec
                        exec_fp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
                        actions["ml_execution_suggestion"] = str(exec_fp)
                    except Exception:
                        pass
                # --- Alpha Expert (labels + high-level features) ---
                if self.brain_enabled and focus == "alpha":
                    alpha_ctx: Dict[str, Any] = {
                        "meta": {"as_of": dstr, "tz": "America/New_York"},
                        "constraints": director_ctx.get("constraints") or {},
                        "recent_intraday_metrics": recent_intraday_metrics,
                    }
                    # Load current label_profile and candidate profiles from label_profiles.yml
                    current_label_name: Optional[str] = None
                    try:
                        cfg_path = Path("alpha_config_enhanced.json")
                        if cfg_path.exists():
                            cfg_full = json.loads(cfg_path.read_text("utf-8"))
                            current_label_name = cfg_full.get("label_profile")
                    except Exception:
                        current_label_name = None
                    try:
                        import yaml as _yaml
                        lp_path = Path("label_profiles.yml")
                        if lp_path.exists():
                            data = _yaml.safe_load(lp_path.read_text("utf-8")) or {}
                            profiles = (data.get("profiles") or {}) if isinstance(data, dict) else {}
                            label_candidates: list[Dict[str, Any]] = []
                            current_cfg: Optional[Dict[str, Any]] = None
                            for name, cfg in profiles.items():
                                if not isinstance(cfg, dict):
                                    continue
                                # Prefer intraday_* profiles (avoid overnight by default)
                                if str(name).startswith("overnight_"):
                                    continue
                                entry = {"name": name, "config": cfg}
                                label_candidates.append(entry)
                                if current_label_name and name == current_label_name:
                                    current_cfg = cfg
                            if current_label_name and current_cfg is not None:
                                alpha_ctx["current_label_profile"] = {
                                    "name": current_label_name,
                                    "config": current_cfg,
                                }
                            if label_candidates:
                                alpha_ctx["label_candidates"] = label_candidates
                    except Exception:
                        pass
                    try:
                        alpha_plan = self.brain.plan_alpha(alpha_ctx)
                    except Exception as e:
                        self.log.warning("plan_alpha_failed", err=str(e), exc_info=True)
                        alpha_plan = {}
                    alpha_crit: Optional[Dict[str, Any]] = None
                    try:
                        if isinstance(alpha_plan, dict) and alpha_plan:
                            crit_constraints = {
                                "max_intraday_drawdown_pct": float(
                                    (director_ctx.get("constraints") or {}).get("max_intraday_drawdown_pct", 2.0)
                                ),
                                "allowed_paths": ["label_profile.*", "features.*"],
                            }
                            alpha_crit = self.brain.validate_agent_output(
                                actor="alpha",
                                proposal=alpha_plan,
                                constraints=crit_constraints,
                            )
                    except Exception as e:
                        self.log.warning("alpha_critic_failed", err=str(e), exc_info=True)
                        alpha_crit = None
                    try:
                        alpha_fp = out_dir / "ml_alpha_suggestion.json"
                        payload: Dict[str, Any] = {"plan": alpha_plan}
                        if alpha_crit is not None:
                            payload["critic"] = alpha_crit
                        # Optional: auto-apply label_profile change when Critic passes or absent
                        autopatch_alpha: Optional[Dict[str, Any]] = None
                        try:
                            if isinstance(alpha_plan, dict) and isinstance(alpha_plan.get("changes"), dict):
                                if not (isinstance(alpha_crit, dict) and alpha_crit.get("valid") is False):
                                    autopatch_alpha = self._auto_apply_alpha_patch(alpha_plan)
                        except Exception as e:
                            self.log.warning("alpha_autopatch_failed", err=str(e), exc_info=True)
                            autopatch_alpha = None
                        if autopatch_alpha is not None:
                            payload["autopatch"] = autopatch_alpha
                        alpha_fp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
                        actions["ml_alpha_suggestion"] = str(alpha_fp)
                    except Exception:
                        pass
            except Exception:
                pass
        except Exception:
            pass
        # G) Summarize ML Brain actions effectiveness (best-effort)
        try:
            dstr = datetime.date.today().isoformat()
            out_dir = self.runs_dir / dstr
            out_dir.mkdir(parents=True, exist_ok=True)
            summary_path = out_dir / "ml_branches_summary.json"
            cmd = (
                f"venv/bin/python tools/summarize_ml_actions.py "
                f"--days 7 --runs-dir {self.runs_dir} --output {summary_path}"
            )
            self._run_cmd(cmd, step="ml_actions_summary")
            actions["ml_actions_summary"] = str(summary_path)
        except Exception as e:
            self.log.warning("ml_actions_summary_failed", err=str(e), exc_info=True)
            actions["ml_actions_summary_error"] = str(e)

        # Persist progress snapshot for observability
        try:
            dstr = datetime.date.today().isoformat()
            out_dir = self.runs_dir / dstr
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "evening_status.json").write_text(json.dumps({"status": "ok", "actions": actions}, indent=2), encoding="utf-8")
        except Exception:
            pass
        return {"status": "ok", "actions": actions}

    # --- internal helpers ---
    def _discover_best_params(self):
        """Locate latest gen_summary.json and extract best params + oos metrics.

        Supports structures like alpha_out/*/gen_XX/gen_summary.json or alpha_out/daily.
        """
        # Gather candidates by mtime (newest first)
        patterns = [
            "alpha_out/**/gen_*/gen_summary.json",
            "alpha_out/*/gen_summary.json",
        ]
        files = []
        for p in patterns:
            files.extend(glob.glob(p, recursive=True))
        files = sorted(files, key=lambda f: Path(f).stat().st_mtime, reverse=True)
        for f in files:
            try:
                data = json.loads(Path(f).read_text("utf-8"))
                # Preferred: stage2.best_params & stage2.oos_summary
                stage2 = data.get("stage2") or data.get("best_trial") or {}
                best_params = stage2.get("best_params") or data.get("best_params")
                oos = stage2.get("oos_summary") or data.get("oos_summary") or {}

                diagnostics = None
                if best_params:
                    # Try to locate diagnostics from rows.model_meta where params match best_params
                    rows = data.get("rows") or []
                    try:
                        # Prefer OOS rows, then IS rows
                        for phase in ("OOS", "IS"):
                            if diagnostics is not None:
                                break
                            for r in rows:
                                if str(r.get("phase", "")).upper() != phase:
                                    continue
                                p_str = r.get("params")
                                if not p_str:
                                    continue
                                try:
                                    row_params = json.loads(p_str)
                                except Exception:
                                    continue
                                if row_params != best_params:
                                    continue
                                meta_str = r.get("model_meta")
                                if not meta_str:
                                    continue
                                try:
                                    meta = json.loads(meta_str)
                                    diagnostics = meta.get("diagnostics")
                                except Exception:
                                    diagnostics = None
                                break
                    except Exception:
                        diagnostics = None

                    return {"file": f, "best_params": best_params, "oos": oos, "diagnostics": diagnostics}
            except Exception:
                continue
        return None

    def _write_temp_best_params(self, params: dict) -> str:
        out = self.runs_dir / datetime.date.today().isoformat() / "best_params.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(params, indent=2), encoding="utf-8")
        try:
            self._mirror_learning_artifact(out, artifact_name="best_params")
        except Exception:
            pass
        return str(out)

    def _learning_day_dir(self, dstr: str | None = None) -> Path:
        return self.runs_dir / (dstr or datetime.date.today().isoformat()) / "learning"

    def _learning_strategy_dir(self, strategy_id: str | None = None, dstr: str | None = None) -> Path:
        sid = str(strategy_id or self._primary_learning_target() or "unknown")
        out = self._learning_day_dir(dstr=dstr) / sid
        out.mkdir(parents=True, exist_ok=True)
        return out

    def _register_learning_artifact(
        self,
        artifact_name: str,
        artifact_path: str | Path,
        strategy_id: str | None = None,
        dstr: str | None = None,
    ) -> None:
        day = str(dstr or datetime.date.today().isoformat())
        sid = str(strategy_id or self._primary_learning_target() or "unknown")
        day_root = self.runs_dir / day
        learning_dir = self._learning_day_dir(day)
        learning_dir.mkdir(parents=True, exist_ok=True)
        index_path = learning_dir / "index.json"
        rel_dir = str(Path("learning") / sid)
        ap = Path(artifact_path)
        try:
            rel_path = str(ap.resolve().relative_to(day_root.resolve()))
        except Exception:
            rel_path = str(ap)
        payload = {"date": day, "updated_at": self._ny_now_iso(), "strategies": {}}
        try:
            if index_path.exists():
                old = json.loads(index_path.read_text("utf-8")) or {}
                if isinstance(old, dict):
                    payload.update(old)
        except Exception:
            pass
        strategies = payload.setdefault("strategies", {})
        entry = strategies.setdefault(sid, {"dir": rel_dir, "artifacts": {}})
        entry["dir"] = rel_dir
        artifacts = entry.setdefault("artifacts", {})
        artifacts[str(artifact_name)] = rel_path
        payload["updated_at"] = self._ny_now_iso()
        index_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _mirror_learning_artifact(
        self,
        source_path: str | Path,
        artifact_name: str,
        strategy_id: str | None = None,
        dstr: str | None = None,
        filename: str | None = None,
    ) -> str | None:
        src = Path(source_path)
        if not src.exists():
            return None
        dst_dir = self._learning_strategy_dir(strategy_id=strategy_id, dstr=dstr)
        dst = dst_dir / (filename or src.name)
        try:
            if src.resolve() != dst.resolve():
                shutil.copy2(src, dst)
        except Exception:
            return None
        self._register_learning_artifact(
            artifact_name=artifact_name,
            artifact_path=dst,
            strategy_id=strategy_id,
            dstr=dstr,
        )
        return str(dst)

    def _build_ab_inputs(self, best: dict):
        # Champion from current registry or defaults
        reg_path = Path("scripts/ai_trader/registry/champion_challenger.json")
        if reg_path.exists():
            try:
                reg = json.loads(reg_path.read_text("utf-8"))
                chmp = {
                    "id": reg.get("champion", {}).get("id", "baseline"),
                    "sharpe": float(reg.get("champion", {}).get("sharpe", 1.0)),
                    "mdd": float(reg.get("champion", {}).get("mdd", 0.10)),
                }
            except Exception:
                chmp = {"id": "baseline", "sharpe": 1.0, "mdd": 0.10}
        else:
            chmp = {"id": "baseline", "sharpe": 1.0, "mdd": 0.10}

        # Challenger from latest oos summary
        oos = best.get("oos", {}) or {}
        chall = {
            "id": "candidate",
            "sharpe": float(oos.get("oos_sharpe", oos.get("sharpe", 1.0))),
            "mdd": float(oos.get("oos_avg_max_drawdown", oos.get("max_drawdown", 0.10))),
        }
        return chmp, chall

    # --- Helpers ---
    def write_plan(self, tag="morning", detail=None):
        plan = {"tag": tag, "detail": detail or {}, "ts": self._ny_now_iso()}
        out = self.runs_dir / datetime.date.today().isoformat() / f"{tag}_plan.json"
        write_action_plan(str(out), plan)
        return plan

    def _get_news_analysis(self) -> dict | None:
        """Run googleNews.py and load JSON as a dict keyed by ticker.

        Returns a summary like {ticker: [{event_type, direction, strength_0to3, ...}, ...]}
        """
        import json as _J, shlex, subprocess
        # Run script (best-effort). It writes llm_trader/scripts/news/news.csv and news.csv.json
        cmd = "venv/bin/python llm_trader/scripts/googleNews.py"
        try:
            self._run_cmd(cmd, step="premarket_news")
        except Exception:
            # Non-fatal
            return None
        news_json = Path("llm_trader/scripts/news/news.csv.json")
        if not news_json.exists():
            return None
        try:
            data = _J.loads(news_json.read_text("utf-8"))
        except Exception:
            return None
        by_ticker: dict = {}
        try:
            for it in (data or []):
                t = str(it.get("ticker") or "").upper()
                if not t:
                    continue
                by_ticker.setdefault(t, []).append({k: it.get(k) for k in ("event_type","direction","strength_0to3","forward_looking","signal_quality","time_horizon","key_reasons")})
        except Exception:
            pass
        # Persist into runs dir for auditable context (with last_fetch timestamps)
        try:
            dstr = datetime.date.today().isoformat()
            out = self.runs_dir / dstr / "news_context.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
            merged = {"by_ticker": {}, "source": str(news_json), "last_fetch": {}}
            if out.exists():
                try:
                    cur = _J.loads(out.read_text("utf-8"))
                    if isinstance(cur, dict):
                        merged = cur
                except Exception:
                    pass
            base = (merged.get("by_ticker") or {}) if isinstance(merged, dict) else {}
            base.update(by_ticker or {})
            merged["by_ticker"] = base
            lf = (merged.get("last_fetch") or {}) if isinstance(merged, dict) else {}
            for k in (by_ticker or {}).keys():
                lf[str(k).upper()] = now_iso
            merged["last_fetch"] = lf
            out.write_text(_J.dumps(merged, indent=2), encoding="utf-8")
        except Exception:
            pass
        return by_ticker

    def _get_news_analysis_for(self, symbols: list[str], days: int | None = None, limit: int | None = None) -> dict | None:
        """Run googleNews.py for a subset of symbols using --tickers override.

        Returns same by_ticker dict as _get_news_analysis, and merges into runs_dir/news_context.json.
        """
        import json as _J
        import shlex
        if not symbols:
            return None
        # Build command
        args = ["python", "llm_trader/scripts/googleNews.py", "--tickers", ",".join(symbols)]
        if isinstance(days, int) and days > 0:
            args += ["--days", str(int(days))]
        if isinstance(limit, int) and limit > 0:
            args += ["--limit", str(int(limit))]
        try:
            self._run_cmd(shlex.join(args), step="premarket_news_subset")
        except Exception:
            return None
        news_json = Path("llm_trader/scripts/news/news.csv.json")
        if not news_json.exists():
            return None
        try:
            data = _J.loads(news_json.read_text("utf-8"))
        except Exception:
            return None
        by_ticker: dict = {}
        try:
            for it in (data or []):
                t = str(it.get("ticker") or "").upper()
                if not t:
                    continue
                by_ticker.setdefault(t, []).append({k: it.get(k) for k in ("event_type","direction","strength_0to3","forward_looking","signal_quality","time_horizon","key_reasons")})
        except Exception:
            pass
        # Merge into runs news_context.json (and update last_fetch timestamps)
        try:
            dstr = datetime.date.today().isoformat()
            out = self.runs_dir / dstr / "news_context.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            merged = {"by_ticker": {}, "source": str(news_json), "last_fetch": {}}
            if out.exists():
                try:
                    cur = _J.loads(out.read_text("utf-8"))
                    if isinstance(cur, dict):
                        merged = cur
                except Exception:
                    pass
            base = (merged.get("by_ticker") or {}) if isinstance(merged, dict) else {}
            for k, v in (by_ticker or {}).items():
                base[k] = v
            merged["by_ticker"] = base
            lf = (merged.get("last_fetch") or {}) if isinstance(merged, dict) else {}
            now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
            for k in (by_ticker or {}).keys():
                lf[str(k).upper()] = now_iso
            merged["last_fetch"] = lf
            out.write_text(_J.dumps(merged, indent=2), encoding="utf-8")
        except Exception:
            pass
        return by_ticker

    def _process_guardian_events(self) -> None:
        """Handle latest guardian event intraday with Brain approval and immediate param tweaks.

        Maps emergency into supported actions: disable_rl / increase_conf_floor / reduce_sizing_multiplier.
        """
        dstr = datetime.date.today().isoformat()
        out_dir = self.runs_dir / dstr
        ev = out_dir / "guardian_events.jsonl"
        if not ev.exists():
            return
        last = None
        import json as _J
        for line in ev.read_text("utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                o = _J.loads(line)
                last = o
            except Exception:
                continue
        if not isinstance(last, dict):
            return
        ack = out_dir / "guardian_ack.json"
        last_ts = str(last.get("ts") or "")
        try:
            if ack.exists():
                cur = _J.loads(ack.read_text("utf-8"))
                if cur.get("last_ts") == last_ts:
                    return
        except Exception:
            pass
        reason = str(last.get("reason") or "")
        pending = []
        if reason == "drawdown":
            pending = [
                {"action": "reduce_sizing_multiplier", "params": {"factor": 0.8}},
                {"action": "increase_conf_floor", "params": {"delta": 0.05}},
            ]
        elif reason == "exposure":
            pending = [
                {"action": "reduce_sizing_multiplier", "params": {"factor": 0.7}},
                {"action": "increase_conf_floor", "params": {"delta": 0.05}},
            ]
        elif reason == "trade_burst":
            pending = [
                {"action": "increase_conf_floor", "params": {"delta": 0.1}},
            ]
        if not pending:
            ack.write_text(_J.dumps({"last_ts": last_ts, "skipped": True}, indent=2), encoding="utf-8")
            return
        approve = None
        if self.brain_enabled:
            try:
                dec = self._brain_decide("execute_actions", {"pending": pending})
                if isinstance(dec, dict) and isinstance(dec.get("approve"), list):
                    approve = {it.get("action"): bool(it.get("ok")) for it in dec.get("approve")}
            except Exception as e:
                self.log.warning("guardian_brain_decide_failed", err=str(e), exc_info=True)
        def is_ok(action: str) -> bool:
            return True if approve is None else bool(approve.get(action, False))
        best_params = {}
        for st in pending:
            act = st.get("action")
            params = st.get("params") or {}
            if not is_ok(act):
                continue
            if act == "increase_conf_floor":
                delta = float(params.get("delta", 0.0))
                cur = 0.6
                try:
                    import yaml as _yaml
                    data = _yaml.safe_load(Path("config.yml").read_text("utf-8")) or {}
                    for s in (data.get("strategies") or []):
                        if str(s.get("id")) == self._primary_learning_target():
                            cur = float((((s.get("params") or {}).get("extras") or {}).get("market_filter") or {}).get("conf_floor", 0.6))
                            break
                except Exception:
                    pass
                best_params["market_filter.conf_floor"] = max(0.0, min(1.0, cur + delta))
            elif act == "reduce_sizing_multiplier":
                factor = float(params.get("factor", 0.9))
                best_params["sizing.multiplier"] = max(0.1, 2.0 * factor)
            elif act == "disable_rl":
                best_params["rl.enabled"] = False
        if not best_params:
            ack.write_text(_J.dumps({"last_ts": last_ts, "skipped": True}, indent=2), encoding="utf-8")
            return
        temp = out_dir / "guardian_apply.json"
        temp.write_text(_J.dumps(best_params, indent=2), encoding="utf-8")
        try:
            self._apply_params_for_target(hpo_params_path=str(temp))
            ack.write_text(_J.dumps({"last_ts": last_ts, "applied": best_params}, indent=2), encoding="utf-8")
        except Exception as e:
            self.log.error("guardian_apply_failed", err=str(e), exc_info=True)
            ack.write_text(_J.dumps({"last_ts": last_ts, "apply_error": str(e)}, indent=2), encoding="utf-8")

    def _process_immune_actions(self) -> None:
        """
        Apply protective actions recommended by anomaly_detector (immune_actions.jsonl).
        Supports reduce_sizing_multiplier and pause_strategy (maps to strategy control pause).
        """
        fp = Path("runs/memory/immune_actions.jsonl")
        if not fp.exists():
            return
        try:
            lines = fp.read_text("utf-8").splitlines()
        except Exception:
            return
        if not lines:
            return
        latest = None
        try:
            latest = json.loads(lines[-1])
        except Exception:
            return
        # Dedup by timestamp
        ack = self.runs_dir / "immune_ack.json"
        latest_ts = str(latest.get("ts") or latest.get("timestamp") or "")
        if ack.exists():
            try:
                cur = json.loads(ack.read_text("utf-8"))
                if cur.get("last_ts") == latest_ts:
                    return
            except Exception:
                pass
        action = str(latest.get("action") or "")
        params = latest.get("params") or {}
        reason = latest.get("reason") or ""
        applied: Dict[str, Any] = {"action": action, "ts": latest_ts, "reason": reason}

        # Map pause_strategy to strategy_control pause
        if action == "pause_strategy":
            try:
                sc = [{"id": self._primary_learning_target(), "action": "pause", "reason": reason or "immune_action"}]
                self._apply_strategy_controls(sc, health=self.health.check(), data_gate=None, ff=None)
                applied["strategy_controls"] = sc
            except Exception as exc:
                applied["error"] = str(exc)
        elif action == "reduce_sizing_multiplier":
            factor = float(params.get("factor", 0.9))
            best_params = {"sizing.multiplier": max(0.1, 2.0 * factor)}
            try:
                tmp = self.runs_dir / "immune_apply.json"
                tmp.write_text(json.dumps(best_params, indent=2), encoding="utf-8")
                self._apply_params_for_target(hpo_params_path=str(tmp))
                applied["params"] = best_params
            except Exception as exc:
                applied["error"] = str(exc)
        else:
            applied["skipped"] = True

        try:
            ack.write_text(json.dumps({"last_ts": latest_ts, "applied": applied}, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _scan_and_recover_tools(self) -> None:
        """Watchdog for llm_trader processes: heartbeat + TTL + restart/backoff.

        Reads runs/YYYY-MM-DD/procs/*.json written by LLMTraderController.
        """
        if not bool((self.cfg.get("policies") or {}).get("tools_watchdog", False)):
            return
        import os, signal, time
        dstr = datetime.date.today().isoformat()
        out_dir = self.runs_dir / dstr
        procs_dir = out_dir / "procs"
        if not procs_dir.exists():
            return
        tcfg = ((self.cfg.get("policies") or {}).get("tools") or {}).get("llm_trader", {})
        hb_to = int(tcfg.get("heartbeat_timeout_s", 180))
        max_rt = int(tcfg.get("max_runtime_min", 60)) * 60
        max_rs = int(tcfg.get("max_restarts_per_day", 2))
        meta_ttl_min = int(tcfg.get("metadata_ttl_min", 120))
        ledger = out_dir / "tools_ledger.jsonl"
        planned = self._load_planned_provider_symbol_map()
        def _alive(pid: int) -> bool:
            try:
                os.kill(pid, 0)
                return True
            except Exception:
                return False
        def _killpg(pid: int):
            try:
                pgid = os.getpgid(pid)
                os.killpg(pgid, signal.SIGTERM)
                time.sleep(10)
                if _alive(pid):
                    os.killpg(pgid, signal.SIGKILL)
            except Exception:
                pass
        import json as _J
        now = time.time()
        for meta_path in procs_dir.glob("llm_*.json"):
            try:
                meta = _J.loads(meta_path.read_text("utf-8"))
            except Exception:
                continue
            pid = int(meta.get("pid") or 0)
            started_at = meta.get("started_at")
            try:
                st = datetime.datetime.fromisoformat(started_at.replace("Z","+00:00")).timestamp() if started_at else None
            except Exception:
                st = None
            hb_file = meta.get("heartbeat_file")
            restarts = int(meta.get("restarts") or 0)
            need_kill = False
            reason = None
            in_plan = self._meta_matches_plan(meta, planned) if isinstance(planned, dict) else True
            # stale heartbeat
            if hb_file:
                try:
                    mtime = os.path.getmtime(hb_file)
                    if now - mtime > hb_to:
                        need_kill = True
                        reason = f"heartbeat_stale>{hb_to}s"
                except Exception:
                    # No heartbeat file yet: allow initial grace
                    if st and (now - st) > hb_to * 2:
                        need_kill = True
                        reason = "heartbeat_missing"
            # over max runtime
            if st and (now - st) > max_rt:
                need_kill = True
                reason = (reason or "") + (";" if reason else "") + f"runtime>{max_rt}s"
            # dead process: cleanup or retire
            if pid and not _alive(pid):
                meta["status"] = "exited" if in_plan else "retired"
                meta_path.write_text(_J.dumps(meta, indent=2), encoding="utf-8")
                continue
            # alive but not in plan → retire immediately
            if pid and _alive(pid) and not in_plan:
                _killpg(pid)
                try:
                    evt = {"ts": self._ny_now_iso(), "event": "retire", "pid": pid, "reason": "plan_mismatch"}
                    with ledger.open("a", encoding="utf-8") as f:
                        f.write(_J.dumps(evt)+"\n")
                except Exception:
                    pass
                meta["status"] = "retired"
                meta_path.write_text(_J.dumps(meta, indent=2), encoding="utf-8")
                continue
            if not need_kill:
                continue
            # kill process group
            _killpg(pid)
            # ledger
            try:
                evt = {"ts": self._ny_now_iso(), "event": "kill", "pid": pid, "reason": reason}
                with ledger.open("a", encoding="utf-8") as f:
                    f.write(_J.dumps(evt)+"\n")
            except Exception:
                pass
            # restart if allowed and still in plan
            if restarts < max_rs and meta.get("cmd") and in_plan:
                try:
                    import os
                    env = os.environ.copy()
                    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent.parent)
                    cwd = str(Path(__file__).resolve().parent.parent.parent)
                    proc = subprocess.Popen(meta.get("cmd"), start_new_session=True, cwd=cwd, env=env)
                    meta["pid"] = proc.pid
                    meta["restarts"] = restarts + 1
                    meta["started_at"] = self._ny_now_iso()
                    meta["status"] = "running"
                    meta_path.write_text(_J.dumps(meta, indent=2), encoding="utf-8")
                    evt = {"ts": self._ny_now_iso(), "event": "restart", "pid": proc.pid}
                    with ledger.open("a", encoding="utf-8") as f:
                        f.write(_J.dumps(evt)+"\n")
                except Exception as e:
                    self.log.error("tools_restart_failed", err=str(e), exc_info=True)
                    meta["status"] = "failed_restart"
                    meta_path.write_text(_J.dumps(meta, indent=2), encoding="utf-8")
            else:
                meta["status"] = "backoff" if in_plan else "retired"
                meta_path.write_text(_J.dumps(meta, indent=2), encoding="utf-8")
                try:
                    evt = {"ts": self._ny_now_iso(), "event": ("backoff" if in_plan else "skip_restart"), "pid": pid, **({"reason": "plan_mismatch"} if not in_plan else {})}
                    with ledger.open("a", encoding="utf-8") as f:
                        f.write(_J.dumps(evt)+"\n")
                except Exception:
                    pass

        # Metadata TTL cleanup for retired/exited/backoff/failed_restart
        try:
            import os as _os
            for mp in list(procs_dir.glob("llm_*.json")):
                try:
                    j = _J.loads(mp.read_text("utf-8"))
                    if str(j.get("status")) in {"retired", "exited", "backoff", "failed_restart"}:
                        age_s = now - _os.path.getmtime(mp)
                        if age_s > max(1, meta_ttl_min) * 60:
                            mp.unlink(missing_ok=True)
                except Exception:
                    continue
        except Exception:
            pass

    def _reconcile_processes_with_plan(self, plan: dict) -> None:
        """Proactively stop processes not present in the given plan.

        This complements the watchdog by eliminating drift immediately after a plan change.
        """
        try:
            d = self.runs_dir / datetime.date.today().isoformat()
            procs_dir = d / "procs"
            if not procs_dir.exists():
                return
            ledger = d / "tools_ledger.jsonl"
            # Build planned map from the provided plan only
            pmap: dict[str, set] = {}
            for p in (plan or {}).get("providers", []) or []:
                prov = p.get("name")
                if not prov:
                    continue
                syms = list(p.get("symbols") or [])
                pmap[prov] = set(syms)
            for meta_path in procs_dir.glob("llm_*.json"):
                try:
                    meta = json.loads(meta_path.read_text("utf-8"))
                except Exception:
                    continue
                if self._meta_matches_plan(meta, pmap):
                    continue
                # kill if alive
                pid = int(meta.get("pid") or 0)
                try:
                    import os, signal
                    if pid:
                        pgid = os.getpgid(pid)
                        os.killpg(pgid, signal.SIGTERM)
                except Exception:
                    pass
                try:
                    evt = {"ts": self._ny_now_iso(), "event": "retire", "pid": pid, "reason": "plan_reconcile"}
                    with ledger.open("a", encoding="utf-8") as f:
                        f.write(json.dumps(evt)+"\n")
                except Exception:
                    pass
                try:
                    meta["status"] = "retired"
                    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
                except Exception:
                    pass
        except Exception:
            # best-effort reconcile
            return

    # --- Plan helpers ---
    def _prepare_prompt_overrides(self, plan: dict) -> dict:
        """Extract execution_prompt_id/text from plan and prepare overrides for LLMTraderController.

        Returns: {index: {"PROMPT_ID": str, "PROMPT_FILE": str}}
        """
        out = {}
        providers = (plan or {}).get("providers") or []

        # Check if text overrides are allowed
        allow_text = bool((self.cfg.get("policies", {}).get("prompts", {}).get("allow_text_override", False)))

        # Prepare output directory for prompt files
        out_dir = self.runs_dir / datetime.date.today().isoformat() / "prompts"
        out_dir.mkdir(parents=True, exist_ok=True)

        ts = self._ny_now_iso().replace(":", "-")

        for i, p in enumerate(providers):
            eid = p.get("execution_prompt_id")
            etx = p.get("execution_prompt_text")
            entry = {}

            # Always pass prompt ID if present
            if eid:
                entry["PROMPT_ID"] = str(eid)

            # Only write prompt text to file if allowed
            if etx and allow_text:
                try:
                    fp = out_dir / f"p{i}_{p.get('name', 'prov')}_{ts}.txt"
                    fp.write_text(str(etx), encoding="utf-8")
                    entry["PROMPT_FILE"] = str(fp)
                    self.log.info("prompt_override_saved", index=i, provider=p.get('name'), file=str(fp))
                except Exception as e:
                    self.log.warning("prompt_override_save_failed", index=i, err=str(e))
            elif etx and not allow_text:
                self.log.warning("prompt_text_override_rejected", index=i,
                                reason="allow_text_override=false in config")

            if entry:
                out[i] = entry

        return out

    def _normalize_or_build_plan(self, session: str, plan: dict, mem: dict) -> dict:
        """Normalize AI plan with safe defaults and clamps, without overriding explicit AI choices.

        - Clamp interval_sec to provider-specific minimums to respect LLM latency/cost
        - Ensure bounded sessions by default cycles if missing
        - Provide sensible wait_for defaults per session
        - Fill symbols from memory/news when missing, and filter DQ blacklist
        """
        providers = (plan or {}).get("providers") or []
        safe_mode = bool((plan or {}).get("safe_mode", False))
        restrictions = []
        try:
            restrictions = list((plan or {}).get("restrictions") or [])
        except Exception:
            restrictions = []
        per_symbol_override = {}
        try:
            per_symbol_override = dict((plan or {}).get("per_symbol_override") or {})
        except Exception:
            per_symbol_override = {}
        # Fallback providers from memory only if AI didn't specify any
        if not providers:
            tops = (mem or {}).get("top_providers") or []
            providers = [{"name": t.get("provider")} for t in tops if t.get("provider")]
            if not providers:
                providers = [{"name": "deepseek_nvidia"}]

        # Defaults and clamps (config-driven with sensible fallbacks)
        providers_cfg = (self.cfg.get("providers", {}) or {})
        def_interval_min = ((providers_cfg.get("intervals", {}) or {}).get("min", {})) or {"deepseek_nvidia": 600, "deepseek": 300, "openai": 300, "gemini": 300}
        def_interval = ((providers_cfg.get("defaults", {}) or {}).get("interval_sec", {})) or {"deepseek_nvidia": 600, "deepseek": 300, "openai": 300, "gemini": 300}
        def_wait = {"morning": "premarket", "intraday": "market", "afterhours": "afterhours", "overnight": "overnight"}
        default_cycles = 30

        # Compute time-to-close and last entry cutoff
        now_ny = self._ny_now()
        close_dt = now_ny.replace(hour=16, minute=0, second=0, microsecond=0)
        minutes_to_close = max(0, int((close_dt - now_ny).total_seconds() // 60))

        # Load DQ blacklist and news candidates (best-effort)
        dstr = datetime.date.today().isoformat()
        out_dir = self.runs_dir / dstr
        dq_blacklist = set()
        news_by_ticker = {}
        try:
            dq_path = out_dir / "data_quality.json"
            if dq_path.exists():
                dqj = json.loads(dq_path.read_text("utf-8"))
                dq_blacklist = set((dqj.get("failed") or {}).keys())
        except Exception:
            dq_blacklist = set()
        try:
            news_path = out_dir / "news_context.json"
            if news_path.exists():
                nj = json.loads(news_path.read_text("utf-8"))
                news_by_ticker = nj.get("by_ticker") or {}
        except Exception:
            news_by_ticker = {}

        # derive news-top candidates (bullish, strength>=2, forward, not low-quality)
        def _news_candidates(limit: int = 5) -> list[str]:
            out = []
            for sym, events in (news_by_ticker or {}).items():
                if not isinstance(events, list):
                    continue
                for ev in events:
                    try:
                        if str(ev.get("direction")) != "bullish":
                            continue
                        if int(ev.get("strength_0to3", 0)) < 2:
                            continue
                        if not bool(ev.get("forward_looking", False)):
                            continue
                        sq = str(ev.get("signal_quality") or "")
                        if sq in {"promotional", "price_recap", "rumor"}:
                            continue
                        out.append(sym)
                        break
                    except Exception:
                        continue
            # de-dup and cap
            uniq = []
            for s in out:
                if s not in uniq:
                    uniq.append(s)
            return uniq[:limit]

        news_syms = _news_candidates(limit=5)

        # memory-derived per-provider symbols map
        top_syms = (mem or {}).get("top_symbols") or []
        prov_map: dict[str, list[str]] = {}
        for item in top_syms:
            prov = item.get("provider")
            sym = item.get("symbol")
            if prov and sym:
                prov_map.setdefault(prov, [])
                if sym not in prov_map[prov]:
                    prov_map[prov].append(sym)

        paused_syms = {s for s, v in (per_symbol_override or {}).items() if str(v) == "pause"}
        manage_only_syms = {s for s, v in (per_symbol_override or {}).items() if str(v) == "manage_only"}

        # llm_trader flags with safe-mode overrides (preload for cutoff)
        flags = dict(((plan or {}).get("llm_trader_flags")) or {})
        if safe_mode or ("no_new_positions" in restrictions) or ("manage_only" in restrictions) or manage_only_syms:
            try:
                cur = int(flags.get("forbid_new_before_min", 0) or 0)
            except Exception:
                cur = 0
            flags["forbid_new_before_min"] = max(cur, 1440)

        try:
            forbid_new_before_min = int(flags.get("forbid_new_before_min", 0) or 0)
        except Exception:
            forbid_new_before_min = 0
        last_entry_dt = close_dt - datetime.timedelta(minutes=forbid_new_before_min)
        seconds_until_last_entry = int((last_entry_dt - now_ny).total_seconds())

        interval_clamps = []
        cycles_caps = []

        # If no time remains for new entries, mark plan as manage-only (no new positions)
        if seconds_until_last_entry <= 0:
            try:
                restrictions = list(restrictions or [])
                if "no_new_positions" not in restrictions:
                    restrictions.append("no_new_positions")
            except Exception:
                pass

        for p in providers:
            n = p.get("name")
            base_name = str(n).lower() if n is not None else ""
            # interval clamp + default
            iv = p.get("interval_sec")
            if iv is None:
                iv = def_interval.get(base_name, 300)
            try:
                iv = int(iv)
            except Exception:
                iv = def_interval.get(base_name, 300)
            min_iv = def_interval_min.get(base_name, 300)
            # Safe mode: slow down further if requested
            if safe_mode or ("slow_interval" in restrictions):
                min_iv = max(min_iv, 600)
            if iv < min_iv:
                iv = min_iv
                interval_clamps.append({"provider": n, "interval_sec": iv})
            p["interval_sec"] = iv

            # cycles default to ensure bounded session
            if p.get("cycles") is None:
                p["cycles"] = default_cycles
            # Cap cycles so that only iterations before last-entry cutoff are counted
            try:
                c_cur = int(p.get("cycles") or default_cycles)
            except Exception:
                c_cur = default_cycles
            # If seconds_until_last_entry negative, only allow management (keep at least 1)
            if seconds_until_last_entry < 0:
                c_cap = max(1, 0)
            else:
                # Allow current iteration + floor(remaining/iv)
                c_cap = max(1, 1 + int(seconds_until_last_entry // max(1, iv)))
            if c_cur > c_cap:
                p["cycles"] = c_cap
                cycles_caps.append({"provider": n, "cycles": c_cap})

            # wait_for default
            p.setdefault("wait_for", def_wait.get(session, "market"))

            # symbols filling: prefer AI-provided; else memory per provider; else news; finally empty (no TSLA fallback)
            syms = p.get("symbols") or []
            if not syms:
                # Memory symbols are high-signal but bounded for cost/risk control.
                cand = (prov_map.get(n) or [])[:3]
                if not cand:
                    cand = (self._strategy_symbols_from_config() or [])[:3]
                # merge news suggestions
                for s in news_syms:
                    if s not in cand:
                        cand.append(s)
                syms = cand[:5]
            # filter dq blacklist
            syms = [s for s in syms if s not in dq_blacklist and s not in paused_syms]
            p["symbols"] = syms

        # If exits planned for positions, prevent new entries
        try:
            if isinstance(plan, dict) and isinstance(plan.get("positions_exit"), dict):
                cur = int(flags.get("forbid_new_before_min", 0) or 0)
                flags["forbid_new_before_min"] = max(cur, 1440)
        except Exception:
            pass

        # Apply stickiness to reduce plan churn (preserve symbols across short windows unless triggers)
        try:
            plan = self._apply_stickiness(session, plan, window_sec=self._pp_stickiness_sec)
        except Exception:
            pass

        # Attach computed flags and helpful notes
        
        plan_notes = []
        try:
            if interval_clamps:
                plan_notes.append("interval_clamped_due_to_safe_mode")
            if cycles_caps:
                plan_notes.append("cycles_capped_by_entry_cutoff")
        except Exception:
            pass
        if plan_notes:
            try:
                cur_notes = str(plan.get("notes") or "").strip()
                extra = ",".join(plan_notes)
                plan["notes"] = f"{cur_notes};{extra}" if cur_notes else extra
            except Exception:
                pass
        # Integrate nightly prompt evolution candidate as challenger (safe, ID-based)
        # Controlled by policies.prompts.use_prompt_challenger (default: false)
        use_challenger = self.cfg.get("policies", {}).get("prompts", {}).get("use_prompt_challenger", False)
        max_concurrent = self.cfg.get("policies", {}).get("prompts", {}).get("max_concurrent_providers", 3)

        if use_challenger:
            try:
                ps = out_dir / "prompt_suggestions.json"
                if ps.exists():
                    pj = json.loads(ps.read_text("utf-8"))
                    cand_id = pj.get("applied_prompt_id") or pj.get("recommended_prompt_id") or pj.get("suggested_prompt_id")
                    if isinstance(cand_id, str) and cand_id:
                        # Verify exists in prompt library
                        lib = {}
                        try:
                            libp = Path("prompts/prompt_library.json")
                            if libp.exists():
                                lib = json.loads(libp.read_text("utf-8"))
                        except Exception:
                            lib = {}
                        # Support both old format (dict of prompts) and new format (with meta)
                        prompts = lib.get('prompts', lib) if isinstance(lib, dict) else {}
                        if cand_id in prompts:
                            # Respect max_concurrent_providers limit
                            if len(providers) >= max_concurrent:
                                pass  # Skip adding challenger if at limit
                            else:
                                # Ensure AB weights present (non-destructive)
                                try:
                                    ab = (plan.get("ab") if isinstance(plan, dict) else None) or {}
                                    if not isinstance(ab, dict) or (not {"champion","challenger"} <= set(ab.keys())):
                                        plan["ab"] = {"champion": 0.7, "challenger": 0.3}
                                except Exception:
                                    plan["ab"] = {"champion": 0.7, "challenger": 0.3}
                                # Choose base provider template
                                base_name = providers[0].get("name") if providers else "deepseek_nvidia"
                                # Avoid duplicates
                                have = any(str(p.get("execution_prompt_id")) == cand_id for p in providers)
                                if not have:
                                    base_syms = providers[0].get("symbols") if providers else []
                                    prov_chl = {
                                        "name": base_name,
                                        "execution_prompt_id": cand_id,
                                        "ab_group": "challenger",
                                        "interval_sec": def_interval.get(base_name, 300),
                                        "cycles": default_cycles,
                                        "wait_for": def_wait.get(session, "market"),
                                        "symbols": base_syms,
                                    }
                                    providers.append(prov_chl)
            except Exception:
                pass

        # Persist safe mode status (best-effort)
        try:
            if safe_mode or restrictions or per_symbol_override:
                dstr = datetime.date.today().isoformat()
                out_dir = self.runs_dir / dstr
                out_dir.mkdir(parents=True, exist_ok=True)
                safe = {
                    "ts": self._ny_now_iso(),
                    "enabled": bool(safe_mode),
                    "restrictions": restrictions,
                    "per_symbol_override": per_symbol_override,
                }
                (out_dir / "safe_mode.json").write_text(json.dumps(safe, indent=2), encoding="utf-8")
        except Exception:
            pass

        return {
            "session": session,
            "providers": providers,
            "llm_trader_flags": flags,
        }

    def _apply_stickiness(self, session: str, plan: dict, window_sec: int = 900) -> dict:
        """Reduce plan churn by keeping provider.symbols stable within a window unless triggers. """
        try:
            if not isinstance(plan, dict):
                return plan
            # Skip when triggers demand refresh
            if plan.get("need_news") or plan.get("positions_exit"):
                # Save and return
                self._save_last_plan(session, plan)
                return plan
            cdir = self._cache_dir()
            last_fp = cdir / f"last_plan_{session}.json"
            import json as _J, time as _t
            now = int(_t.time())
            last = None
            if last_fp.exists():
                try:
                    last = _J.loads(last_fp.read_text("utf-8"))
                except Exception:
                    last = None
            # Only apply within window
            if last and isinstance(last, dict):
                try:
                    age = now - int(last.get("ts", 0))
                except Exception:
                    age = window_sec + 1
                if age < window_sec:
                    # Build provider->symbols map
                    mp = {}
                    for p in (last.get("providers") or []):
                        n = str(p.get("name") or "").lower()
                        if not n:
                            continue
                        syms = [str(s).upper() for s in (p.get("symbols") or [])]
                        mp[n] = syms
                    # Apply to current plan providers where symbols missing or oscillating
                    changed = False
                    for p in (plan.get("providers") or []):
                        n = str(p.get("name") or "").lower()
                        if n in mp:
                            cur_syms = [str(s).upper() for s in (p.get("symbols") or [])]
                            # If current is empty or differs significantly, reuse last within window
                            if not cur_syms or len(set(cur_syms) ^ set(mp[n])) >= max(2, len(mp[n])//2):
                                p["symbols"] = mp[n]
                                changed = True
                    if changed:
                        plan["notes"] = ((plan.get("notes") or "") + ";sticky_symbols_applied").strip(";")
            # Save current plan snapshot
            self._save_last_plan(session, plan, ts=now)
            return plan
        except Exception:
            return plan

    def _save_last_plan(self, session: str, plan: dict, ts: int | None = None) -> None:
        try:
            import json as _J, time as _t
            if ts is None:
                ts = int(_t.time())
            snap = {"ts": ts, "providers": plan.get("providers"), "safe_mode": plan.get("safe_mode"), "notes": plan.get("notes")}
            cdir = self._cache_dir()
            (cdir / f"last_plan_{session}.json").write_text(_J.dumps(snap, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _maybe_write_intraday_memory_snapshot(self):
        """Once per hour, compute reflection and store an intraday memory episode.

        Uses runs_dir/YYYY-MM-DD/intraday_memory_HH.json as a guard.
        """
        import datetime as _dt
        now = _dt.datetime.now()
        dstr = now.date().isoformat()
        hstr = now.strftime("%H")
        out_dir = self.runs_dir / dstr
        out_dir.mkdir(parents=True, exist_ok=True)
        guard = out_dir / f"intraday_memory_{hstr}.json"
        if guard.exists():
            return None
        # Run reflection job for today (partial day is OK)
        try:
            import subprocess, shlex, os
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent.parent)
            cwd = str(Path(__file__).resolve().parent.parent.parent)
            subprocess.check_call(shlex.split(
                f"venv/bin/python scripts/ai_trader/reflection/consistency_job.py --date {dstr} --runs-dir {self.runs_dir}"
            ), cwd=cwd, env=env)
            # Store episode (session=intraday)
            from scripts.ai_trader.memory.episode_store import store_episode
            ep = store_episode(
                runs_dir=self.cfg["paths"]["runs_dir"],
                session="intraday",
                best=None,
                hpo_gate=None,
                notes=f"intraday snapshot {hstr}:00",
                d=dstr,
            )
            # Persist small plan file as a marker
            write_action_plan(str(guard), {"ts": now.isoformat(), "episode_saved": bool(ep.get("saved"))})
            return {"file": str(guard), "saved": bool(ep.get("saved"))}
        except Exception as e:
            return {"error": str(e)}

    def _auto_apply_feature_engineering_patch(
        self,
        suggestion: Dict[str, Any],
        current_feature_programs: Dict[str, Any],
        full_config: Dict[str, Any],
        feature_programs_path: Path,
        alpha_config_path: Path,
    ) -> Optional[Dict[str, Any]]:
        """Auto-apply Feature Engineering Agent patch to feature_programs + alpha_config_enhanced.

        Controlled by brain.autopatch_features (default: enabled).
        """
        if not self.brain_enabled:
            return None
        try:
            brain_cfg = (self.cfg.get("brain") or {})
            if brain_cfg.get("autopatch_features") is False:
                return None
        except Exception:
            pass
        fp_patch = (suggestion.get("feature_programs_patch") or {}) if isinstance(suggestion, dict) else {}
        hpo_patch = (suggestion.get("alpha_hpo_patch") or {}) if isinstance(suggestion, dict) else {}
        applied: Dict[str, Any] = {"status": "ok"}

        # 1) Apply feature_programs_patch
        try:
            add_or_update = fp_patch.get("add_or_update") or {}
            delete_list = fp_patch.get("delete") or []
            added = 0
            updated = 0
            deleted = 0
            programs = dict(current_feature_programs or {})
            if add_or_update or delete_list:
                for prog_id, prog_def in add_or_update.items():
                    if prog_id in programs:
                        updated += 1
                    else:
                        added += 1
                    programs[prog_id] = prog_def
                for prog_id in delete_list:
                    if prog_id in programs:
                        del programs[prog_id]
                        deleted += 1
                feature_programs_path.write_text(json.dumps(programs, indent=2, ensure_ascii=False), encoding="utf-8")
                applied["feature_programs"] = {
                    "added": added,
                    "updated": updated,
                    "deleted": deleted,
                    "path": str(feature_programs_path),
                }
        except Exception as e:
            self.log.warning("feature_programs_autopatch_failed", err=str(e), exc_info=True)
            applied["status"] = "error"
            applied["feature_programs_error"] = str(e)

        # 2) Apply alpha_hpo_patch to alpha_config_enhanced.json
        try:
            grid = (full_config.get("hpo") or {}).get("grid") or {}
            changed = False
            fp_choices = hpo_patch.get("feature_program_id_choices")
            fs_choices = hpo_patch.get("feature_subset_choices")
            scale_choices = hpo_patch.get("model_scale_mode_choices")
            if isinstance(fp_choices, list) and "feature_program_id" in grid and isinstance(grid["feature_program_id"], dict):
                grid["feature_program_id"]["values"] = fp_choices
                changed = True
            if isinstance(fs_choices, list) and "feature_subset" in grid and isinstance(grid["feature_subset"], dict):
                grid["feature_subset"]["values"] = fs_choices
                changed = True
            if isinstance(scale_choices, list) and "model_scale_mode" in grid and isinstance(grid["model_scale_mode"], dict):
                grid["model_scale_mode"]["values"] = scale_choices
                changed = True
            if changed:
                # Persist updated config
                alpha_config_path.write_text(json.dumps(full_config, indent=2, ensure_ascii=False), encoding="utf-8")
                applied["alpha_hpo"] = {
                    "feature_program_id_choices": len(fp_choices or []),
                    "feature_subset_choices": len(fs_choices or []),
                    "model_scale_mode_choices": len(scale_choices or []),
                    "path": str(alpha_config_path),
                }
        except Exception as e:
            self.log.warning("alpha_hpo_autopatch_failed", err=str(e), exc_info=True)
            applied["status"] = "error"
            applied["alpha_hpo_error"] = str(e)

        return applied

    def _auto_apply_execution_patch(self, plan: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Auto-apply a limited subset of Execution Expert changes to HPO search space.

        Currently supports mapping gating.time_stop_min/max → hpo.grid['risk.time_stop_min'].low/high.
        Controlled by brain.autopatch_execution (default: enabled).
        """
        if not self.brain_enabled:
            return None
        try:
            brain_cfg = (self.cfg.get("brain") or {})
            if brain_cfg.get("autopatch_execution") is False:
                return None
        except Exception:
            pass
        changes = plan.get("changes") or {}
        if not isinstance(changes, dict) or not changes:
            return None
        base_path = Path("alpha_config_enhanced.json")
        if not base_path.exists():
            return {"status": "base_missing", "base": str(base_path)}
        try:
            full_cfg = json.loads(base_path.read_text("utf-8"))
        except Exception as e:
            return {"status": "read_failed", "base": str(base_path), "error": str(e)}
        hpo_cfg = (full_cfg.get("hpo") or {})
        grid = hpo_cfg.get("grid") or {}
        if "risk.time_stop_min" not in grid or not isinstance(grid["risk.time_stop_min"], dict):
            return {"status": "no_risk_time_stop_grid"}
        grid_entry = grid["risk.time_stop_min"]
        old_low = grid_entry.get("low")
        old_high = grid_entry.get("high")
        # Derive new low/high from gating suggestions if present
        new_low = grid_entry.get("low")
        new_high = grid_entry.get("high")
        if "gating.time_stop_min" in changes:
            try:
                new_low = int(changes["gating.time_stop_min"])
            except Exception:
                pass
        if "gating.time_stop_max" in changes:
            try:
                new_high = int(changes["gating.time_stop_max"])
            except Exception:
                pass
        # If nothing to change, skip
        if new_low == old_low and new_high == old_high:
            return {"status": "no_change", "old_low": old_low, "old_high": old_high}
        # Sanity: ensure bounds and ordering remain reasonable
        try:
            if new_low is not None and new_high is not None:
                if new_low > new_high:
                    new_low, new_high = new_high, new_low
                # Optionally clamp to original broader range if defined
                if isinstance(old_low, int):
                    new_low = max(old_low, new_low)
                if isinstance(old_high, int):
                    new_high = min(old_high, new_high)
        except Exception:
            pass
        # Apply updates
        if new_low is not None:
            grid_entry["low"] = new_low
        if new_high is not None:
            grid_entry["high"] = new_high
        # Persist config
        try:
            base_path.write_text(json.dumps(full_cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            return {
                "status": "write_failed",
                "base": str(base_path),
                "error": str(e),
            }
        return {
            "status": "applied",
            "base": str(base_path),
            "old_low": old_low,
            "old_high": old_high,
            "new_low": new_low,
            "new_high": new_high,
        }

    def _auto_apply_alpha_patch(self, plan: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Auto-apply a limited subset of Alpha Expert changes to alpha_config_enhanced.json.

        Currently supports updating the top-level \"label_profile\" field.
        Controlled by brain.autopatch_alpha (default: enabled).
        """
        if not self.brain_enabled:
            return None
        try:
            brain_cfg = (self.cfg.get("brain") or {})
            if brain_cfg.get("autopatch_alpha") is False:
                return None
        except Exception:
            pass
        changes = plan.get("changes") or {}
        if not isinstance(changes, dict) or not changes:
            return None
        new_label = changes.get("label_profile.name")
        if not new_label:
            return {"status": "no_label_change"}
        base_path = Path("alpha_config_enhanced.json")
        if not base_path.exists():
            return {"status": "base_missing", "base": str(base_path)}
        try:
            cfg_full = json.loads(base_path.read_text("utf-8"))
        except Exception as e:
            return {"status": "read_failed", "base": str(base_path), "error": str(e)}
        old_label = cfg_full.get("label_profile")
        if old_label == new_label:
            return {"status": "no_change", "label_profile": old_label}
        cfg_full["label_profile"] = new_label
        try:
            base_path.write_text(json.dumps(cfg_full, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            return {
                "status": "write_failed",
                "base": str(base_path),
                "error": str(e),
            }
        return {
            "status": "applied",
            "base": str(base_path),
            "old_label_profile": old_label,
            "new_label_profile": new_label,
        }

    def _auto_apply_model_space_patch(self) -> Optional[Dict[str, Any]]:
        """Auto-apply last model-space suggestion to alpha_config_enhanced.json for next HPO.

        - Looks back up to 7 days for ml_model_space_suggestion.json.
        - Respects brain.autopatch_model_space flag (default: enabled).
        - Skips suggestions that Critic marked invalid.
        """
        # Brain must be enabled
        if not self.brain_enabled:
            return None
        # Config toggle (default: True if unspecified)
        try:
            brain_cfg = (self.cfg.get("brain") or {})
            if brain_cfg.get("autopatch_model_space") is False:
                return None
        except Exception:
            pass
        import datetime as _dt
        today = _dt.date.today()
        runs_root = self.runs_dir
        suggestion_fp: Optional[Path] = None
        # Search most recent suggestion in past 7 days
        for offset in range(1, 8):
            d = today - _dt.timedelta(days=offset)
            cand = runs_root / d.isoformat() / "ml_model_space_suggestion.json"
            if cand.exists():
                suggestion_fp = cand
                break
        if suggestion_fp is None:
            return None
        try:
            raw = json.loads(suggestion_fp.read_text("utf-8"))
        except Exception as e:
            return {"status": "suggestion_read_failed", "file": str(suggestion_fp), "error": str(e)}
        if isinstance(raw, dict) and "plan" in raw:
            plan = raw.get("plan") or {}
            critic = raw.get("critic") or {}
        else:
            plan = raw
            critic = {}
        # If Critic exists and says invalid, skip
        if isinstance(critic, dict) and critic.get("valid") is False:
            return {
                "status": "skipped_by_critic",
                "file": str(suggestion_fp),
                "critic": critic,
            }
        if not isinstance(plan, dict) or not isinstance(plan.get("changes"), dict) or not plan.get("changes"):
            return {"status": "no_changes", "file": str(suggestion_fp)}
        base_path = Path("alpha_config_enhanced.json")
        if not base_path.exists():
            return {"status": "base_missing", "base": str(base_path)}
        # Build patch payload compatible with apply_config_patch.py
        patch_payload: Dict[str, Any] = {
            "version": str(plan.get("version") or "v1"),
            "changes": plan.get("changes") or {},
        }
        if plan.get("reasoning_summary"):
            patch_payload["reasoning"] = plan["reasoning_summary"]
        # Write patch file under today's runs directory
        today_dir = runs_root / today.isoformat()
        today_dir.mkdir(parents=True, exist_ok=True)
        patch_path = today_dir / "model_space_patch.json"
        patch_path.write_text(json.dumps(patch_payload, indent=2), encoding="utf-8")
        # Apply patch in-place to alpha_config_enhanced.json
        cmd = (
            f"venv/bin/python apply_config_patch.py "
            f"--base {base_path} --patch {patch_path} --output {base_path} --quiet"
        )
        try:
            self._run_cmd(cmd, step="apply_model_space_patch")
        except Exception as e:
            return {
                "status": "apply_failed",
                "file": str(suggestion_fp),
                "patch": str(patch_path),
                "error": str(e),
            }
        return {
            "status": "applied",
            "file": str(suggestion_fp),
            "patch": str(patch_path),
            "base": str(base_path),
        }

    def _load_recent_intraday_metrics(self, lookback_days: int = 7, account: str = "paper") -> Dict[str, Any]:
        """Best-effort summary of recent intraday performance for the Research Director.

        Uses trading.pnl_daily (and equity_pnl_snapshots when available) to derive:
          - mean/median daily PnL in percent of starting equity
          - average trades per day
          - win_rate over days (fraction of days with positive PnL)
          - max_intraday_drawdown_pct approximated by worst daily loss magnitude
        Returns an empty dict if ClickHouse is unavailable or no data is found.
        """
        try:
            ch = ClickHouseDB()
        except Exception:
            return {}
        try:
            sql = """
            SELECT
              d_local,
              sum(realized + unrealized_eod - fees) AS day_pnl,
              sum(trades) AS trades
            FROM trading.pnl_daily
            WHERE d_local >= today() - INTERVAL %(days)s DAY
              AND account = %(account)s
            GROUP BY d_local
            ORDER BY d_local ASC
            """
            rows = ch.query(sql, {"days": int(lookback_days), "account": account})
        except Exception as e:
            self.log.warning("recent_intraday_metrics_pnl_daily_failed", err=str(e), exc_info=True)
            return {}
        if not rows:
            return {}
        # Optional: use equity_pnl_snapshots to scale PnL into pct-of-equity when available
        equity_by_day: Dict[str, float] = {}
        try:
            eq_sql = """
            SELECT
              date,
              anyFirst(equity) AS equity_start
            FROM trading.equity_pnl_snapshots
            WHERE date >= today() - INTERVAL %(days)s DAY
            GROUP BY date
            """
            eq_rows = ch.query(eq_sql, {"days": int(lookback_days)})
            for r in eq_rows or []:
                try:
                    d = str(r.get("date"))
                    equity_by_day[d] = float(r.get("equity_start") or 0.0)
                except Exception:
                    continue
        except Exception:
            equity_by_day = {}
        import statistics
        daily_pcts: List[float] = []
        trades_per_day: List[float] = []
        for r in rows or []:
            try:
                d = str(r.get("d_local") or r.get("d") or "")
                pnl = float(r.get("day_pnl") or 0.0)
                trades = float(r.get("trades") or 0.0)
            except Exception:
                continue
            base_eq = equity_by_day.get(d)
            if base_eq is None or base_eq <= 0.0:
                base_eq = 100000.0
            try:
                pct = (pnl / base_eq) * 100.0
            except Exception:
                pct = 0.0
            daily_pcts.append(pct)
            trades_per_day.append(trades)
        if not daily_pcts:
            return {}
        try:
            mean_pct = float(statistics.mean(daily_pcts))
        except Exception:
            mean_pct = float(daily_pcts[0])
        try:
            median_pct = float(statistics.median(daily_pcts))
        except Exception:
            median_pct = float(daily_pcts[0])
        try:
            mean_trades = float(statistics.mean(trades_per_day)) if trades_per_day else 0.0
        except Exception:
            mean_trades = 0.0
        try:
            win_rate = float(sum(1 for v in daily_pcts if v > 0.0) / len(daily_pcts))
        except Exception:
            win_rate = 0.0
        try:
            worst = float(min(daily_pcts))
        except Exception:
            worst = 0.0
        max_dd_pct = abs(worst) if worst < 0.0 else 0.0
        return {
            "mean_daily_pnl_pct": mean_pct,
            "median_daily_pnl_pct": median_pct,
            "trades_per_day": mean_trades,
            "win_rate": win_rate,
            "max_intraday_drawdown_pct": max_dd_pct,
        }

    # --- Decision validation (lightweight, no external deps) ---
    def _validate_start_morning_decision(self, d: dict) -> dict:
        try:
            proceed = bool(d.get("proceed"))
            notes = d.get("notes")
            if notes is not None:
                notes = str(notes)[:200]
            return {"proceed": proceed, **({"notes": notes} if notes else {})}
        except Exception:
            return {"proceed": True}

    def _validate_hpo_deploy_decision(self, d: dict) -> dict:
        out = {"apply_params": False}
        try:
            out["apply_params"] = bool(d.get("apply_params"))
        except Exception:
            pass
        try:
            w = d.get("ab_weights") or {}
            chp = float(w.get("champion"))
            chl = float(w.get("challenger"))
            # clamp to [0,1] and normalize if sum>0
            chp = max(0.0, min(1.0, chp))
            chl = max(0.0, min(1.0, chl))
            s = chp + chl
            if s > 0:
                chp, chl = chp / s, chl / s
            out["ab_weights"] = {"champion": chp, "challenger": chl}
        except Exception:
            pass
        if d.get("notes"):
            out["notes"] = str(d.get("notes"))[:200]
        return out

    def _hpo_deploy_gate(self, oos: dict, cfg: dict) -> dict:
        """Check OOS metrics against thresholds before applying params.

        cfg accepts keys: min_oos_sharpe, max_oos_mdd, min_oos_trades, min_coverage,
        min_pf (profit_factor), min_expectancy_usd, min_return.
        OOS dict may carry keys: oos_sharpe|sharpe, oos_avg_max_drawdown|max_drawdown|mdd,
        oos_trades|trades|n, coverage, profit_factor|pf, expectancy, return|total_return.
        """
        # Extract metrics with multiple fallback keys
        try:
            sharpe = float(oos.get("oos_sharpe", oos.get("sharpe", 0.0)))
        except Exception:
            sharpe = 0.0
        try:
            mdd = float(oos.get("oos_avg_max_drawdown", oos.get("max_drawdown", oos.get("mdd", 1.0))))
        except Exception:
            mdd = 1.0

        # trades fallback across common fields
        trades = None
        for k in ("oos_trades", "trades", "n", "num_trades"):
            if k in oos:
                try:
                    trades = int(oos.get(k) or 0)
                except Exception:
                    trades = 0
                break
        if trades is None:
            trades = 0

        # coverage fallback
        try:
            coverage = float(oos.get("coverage", oos.get("oos_coverage", 1.0)))
        except Exception:
            coverage = 1.0

        # NEW: Profit Factor (盈亏比)
        try:
            pf = float(oos.get("profit_factor", oos.get("pf", oos.get("oos_pf", 0.0))))
        except Exception:
            pf = 0.0

        # NEW: Expectancy (期望收益 per trade)
        try:
            expectancy = float(oos.get("expectancy", oos.get("avg_win_per_trade", oos.get("oos_expectancy", 0.0))))
        except Exception:
            expectancy = 0.0

        # NEW: Total Return (总收益率)
        try:
            ret = float(oos.get("return", oos.get("total_return", oos.get("oos_return", 0.0))))
        except Exception:
            ret = 0.0

        reasons = []
        ok = True
        if cfg:
            # Original checks
            min_sh = cfg.get("min_oos_sharpe")
            if min_sh is not None and sharpe < float(min_sh):
                ok = False
                reasons.append(f"oos_sharpe<{min_sh} (got {sharpe:.3f})")

            max_m = cfg.get("max_oos_mdd")
            if max_m is not None and mdd > float(max_m):
                ok = False
                reasons.append(f"oos_mdd>{max_m} (got {mdd:.3f})")

            min_tr = cfg.get("min_oos_trades")
            if min_tr is not None and trades < int(min_tr):
                ok = False
                reasons.append(f"oos_trades<{min_tr} (got {trades})")

            min_cov = cfg.get("min_coverage")
            if min_cov is not None and coverage < float(min_cov):
                ok = False
                reasons.append(f"coverage<{min_cov} (got {coverage:.3f})")

            # NEW: Profit Factor check
            min_pf = cfg.get("min_pf")
            if min_pf is not None and pf < float(min_pf):
                ok = False
                reasons.append(f"profit_factor<{min_pf} (got {pf:.3f})")

            # NEW: Expectancy check (must be > 0 for positive expected value)
            min_exp = cfg.get("min_expectancy_usd")
            if min_exp is not None and expectancy <= float(min_exp):
                ok = False
                reasons.append(f"expectancy<={min_exp} (got {expectancy:.3f})")

            # NEW: Return check (must be > 0 for profitability)
            min_ret = cfg.get("min_return")
            if min_ret is not None and ret <= float(min_ret):
                ok = False
                reasons.append(f"return<={min_ret}% (got {ret:.3f}%)")

        metrics = {
            "oos_sharpe": sharpe,
            "oos_mdd": mdd,
            "oos_trades": trades,
            "coverage": coverage,
            "profit_factor": pf,
            "expectancy": expectancy,
            "return": ret
        }

        return {"ok": ok, "metrics": metrics, "reasons": reasons}

    def _generate_stop_loss_report(self, retry_audit: list, best_oos: dict) -> dict:
        """Generate stop-loss report when HPO fails to converge after max rounds.

        Args:
            retry_audit: List of audit dicts from each retry round
            best_oos: Best result across all rounds (even if didn't pass gate)

        Returns:
            dict with failure analysis, trends, recommendations, and conservative actions
        """
        report = {
            "status": "failed_to_converge",
            "total_rounds": len(retry_audit),
            "best_metrics": best_oos.get("oos", {}) if best_oos else {},
            "failure_breakdown": {},
            "performance_trend": [],
            "recommendations": [],
            "pending_actions": []
        }

        # Analyze failure reasons
        reasons_count = {}
        for audit in retry_audit:
            for reason in audit.get("gate_reasons", []):
                reasons_count[reason] = reasons_count.get(reason, 0) + 1

        report["failure_breakdown"] = reasons_count

        # Analyze performance trend (is it improving?)
        sharpe_trend = [
            a.get("oos_metrics", {}).get("oos_sharpe", 0)
            for a in retry_audit
            if a.get("oos_metrics")
        ]
        report["performance_trend"] = sharpe_trend

        improving = len(sharpe_trend) > 1 and sharpe_trend[-1] > sharpe_trend[0]

        # Generate recommendations based on failure patterns
        reasons_str = str(reasons_count)

        if "coverage" in reasons_str:
            report["recommendations"].append(
                "数据覆盖率不足: 缩小窗口或延长训练周期, 或等待数据回补完成"
            )

        if "oos_sharpe" in reasons_str:
            if improving:
                report["recommendations"].append(
                    "Sharpe在改善但未达标: 增加max_rounds或降低阈值"
                )
            else:
                report["recommendations"].append(
                    "Sharpe持续偏低: 检查特征工程或模型架构"
                )

        if "oos_trades" in reasons_str:
            report["recommendations"].append(
                "交易次数不足: 降低conf_floor或扩大symbol池"
            )

        if "profit_factor" in reasons_str:
            report["recommendations"].append(
                "盈亏比不足: 优化止损策略或entry timing"
            )

        if "expectancy" in reasons_str or "return" in reasons_str:
            report["recommendations"].append(
                "负期望或负收益: 严重问题，建议暂停自动部署，人工审查策略逻辑"
            )

        # Generate conservative pending_actions
        # These will be reviewed by brain or human before applying
        try:
            import yaml as _yaml
            cfg_data = _yaml.safe_load(Path("config.yml").read_text("utf-8"))
            extras = {}
            for s in (cfg_data.get("strategies") or []):
                if str(s.get("id")) == self._primary_learning_target():
                    extras = (((s.get("params") or {}).get("extras")) or {})
                    break

            current_conf_floor = float(((extras.get("market_filter") or {}).get("conf_floor", 0.6)) or 0.6)
            current_sizing_mult = float(((extras.get("sizing") or {}).get("multiplier", 2.0)) or 2.0)

            # Conservative actions: tighten entry criteria, reduce sizing
            report["pending_actions"] = [
                {
                    "action": "increase_conf_floor",
                    "params": {"delta": 0.1},
                    "from": current_conf_floor,
                    "to": min(1.0, current_conf_floor + 0.1),
                    "reason": "提高入场门槛以降低交易频率"
                },
                {
                    "action": "reduce_sizing_multiplier",
                    "params": {"factor": 0.75},
                    "from": current_sizing_mult,
                    "to": current_sizing_mult * 0.75,
                    "reason": "降低仓位规模以控制风险"
                }
            ]

            # If seriously underperforming (negative expectancy/return), suggest disabling
            if any("expectancy" in r or "return" in r for r in reasons_count.keys()):
                report["pending_actions"].append({
                    "action": "disable_strategy",
                    "reason": "负期望值，建议暂停策略"
                })

        except Exception as e:
            self.log.warning("generate_pending_actions_failed", err=str(e))

        return report

    def _execute_pending_actions(self):
        import yaml as _yaml
        from datetime import timedelta as _td
        today = datetime.date.today()
        # Find pending file (prefer yesterday, then today)
        path = None
        for d in (today - _td(days=1), today):
            p = self.runs_dir / d.isoformat() / "pending_actions.yml"
            if p.exists():
                path = p
                break
        if not path:
            return None
        data = _yaml.safe_load(path.read_text("utf-8")) or {}
        pending = data.get("pending") or []
        if not pending:
            return None
        # Ask brain to approve
        approve = None
        try:
            dec = self._brain_decide("execute_actions", {"pending": pending}) if self.brain_enabled else None
            if isinstance(dec, dict) and isinstance(dec.get("approve"), list):
                approve = {it.get("action"): bool(it.get("ok")) for it in dec.get("approve")}
        except Exception:
            approve = None

        # Load current config
        try:
            cfg_data = _yaml.safe_load(Path("config.yml").read_text("utf-8"))
        except Exception:
            cfg_data = {}
        extras = {}
        try:
            for s in (cfg_data.get("strategies") or []):
                if str(s.get("id")) == self._primary_learning_target():
                    extras = (((s.get("params") or {}).get("extras")) or {})
                    break
        except Exception:
            extras = {}

        def is_ok(action: str) -> bool:
            return True if approve is None else bool(approve.get(action, False))

        best_params = {}
        supported_actions = {"disable_rl", "increase_conf_floor", "reduce_sizing_multiplier", "label_recompute", "feature_rebuild"}
        denied_actions = []
        unsupported = 0
        label_recompute_results = []
        feature_rebuild_results = []
        for step in pending:
            act = step.get("action")
            params = step.get("params") or {}
            if act not in supported_actions:
                unsupported += 1
                continue
            if not is_ok(act):
                denied_actions.append(act)
                continue
            if act == "disable_rl":
                best_params["rl.enabled"] = False
            elif act == "increase_conf_floor":
                delta = float(params.get("delta", 0.0))
                cur = float(((extras.get("market_filter") or {}).get("conf_floor", 0.6)) or 0.6)
                newv = max(0.0, min(1.0, cur + delta))
                best_params["market_filter.conf_floor"] = round(newv, 4)
            elif act == "reduce_sizing_multiplier":
                factor = float(params.get("factor", 0.9))
                cur = float(((extras.get("sizing") or {}).get("multiplier", 2.0)) or 2.0)
                newv = max(0.1, cur * factor)
                best_params["sizing.multiplier"] = round(newv, 4)
            elif act == "label_recompute":
                # Execute label recomputation via DataHealer
                symbols = params.get("symbols", [])
                horizons = params.get("horizons", [30])
                if symbols and self.data_healer:
                    try:
                        self.log.info("label_recompute_requested", symbols=symbols, horizons=horizons)
                        heal_result = self.data_healer.ensure_labels_ready(
                            symbols=symbols,
                            horizons=horizons,
                            lookback_days=params.get("lookback_days", 90),
                            batch_size=params.get("batch_size", 5)
                        )
                        label_recompute_results.append({
                            "symbols": symbols,
                            "result": heal_result
                        })
                        self.log.info("label_recompute_completed",
                                      symbols=symbols,
                                      status=heal_result.get("status"),
                                      generated_rows=heal_result.get("generated_rows", 0))
                    except Exception as e:
                        self.log.error("label_recompute_failed", symbols=symbols, err=str(e), exc_info=True)
                        label_recompute_results.append({
                            "symbols": symbols,
                            "result": {"status": "error", "error": str(e)}
                        })
            elif act == "feature_rebuild":
                # Execute feature rebuild via DataHealer
                symbols = params.get("symbols", [])
                start_date = params.get("start_date", "")
                end_date = params.get("end_date", "")
                dry_run = bool(params.get("dry_run", False))

                if symbols and start_date and end_date and self.data_healer:
                    try:
                        self.log.info("feature_rebuild_requested",
                                      symbols=symbols,
                                      start_date=start_date,
                                      end_date=end_date,
                                      dry_run=dry_run)

                        rebuild_result = self.data_healer.ensure_features_ready(
                            symbols=symbols,
                            start_date=start_date,
                            end_date=end_date,
                            dry_run=dry_run
                        )

                        feature_rebuild_results.append({
                            "symbols": symbols,
                            "date_range": f"{start_date} to {end_date}",
                            "result": rebuild_result
                        })

                        self.log.info("feature_rebuild_completed",
                                      symbols=symbols,
                                      status=rebuild_result.get("status"),
                                      rows_written=rebuild_result.get("rows_written", 0))
                    except Exception as e:
                        self.log.error("feature_rebuild_failed",
                                       symbols=symbols,
                                       err=str(e),
                                       exc_info=True)
                        feature_rebuild_results.append({
                            "symbols": symbols,
                            "date_range": f"{start_date} to {end_date}",
                            "result": {"status": "error", "error": str(e)}
                        })
                else:
                    self.log.warning("feature_rebuild_skipped_invalid_params",
                                     symbols=symbols,
                                     start_date=start_date,
                                     end_date=end_date,
                                     has_data_healer=bool(self.data_healer))

        # Log label recompute results
        if label_recompute_results:
            label_log_path = self.runs_dir / today.isoformat() / "label_recompute_log.json"
            label_log_path.parent.mkdir(parents=True, exist_ok=True)
            label_log_path.write_text(json.dumps({
                "ts": self._ny_now_iso(),
                "results": label_recompute_results
            }, indent=2), encoding="utf-8")
            self.log.info("label_recompute_log_saved", path=str(label_log_path))

        # Log feature rebuild results
        if feature_rebuild_results:
            feature_rebuild_log_path = self.runs_dir / today.isoformat() / "feature_rebuild_log.json"
            feature_rebuild_log_path.parent.mkdir(parents=True, exist_ok=True)
            feature_rebuild_log_path.write_text(json.dumps({
                "ts": self._ny_now_iso(),
                "results": feature_rebuild_results
            }, indent=2), encoding="utf-8")
            self.log.info("feature_rebuild_log_saved", path=str(feature_rebuild_log_path))

        if not best_params:
            # If only label_recompute or feature_rebuild was executed, return the log path
            if label_recompute_results or feature_rebuild_results:
                return str(label_log_path)
            return None
        # Apply via existing writer
        temp = self.runs_dir / today.isoformat() / "pending_apply.json"
        temp.parent.mkdir(parents=True, exist_ok=True)
        temp.write_text(json.dumps(best_params, indent=2), encoding="utf-8")
        try:
            self._apply_params_for_target(hpo_params_path=str(temp))
            log = {
                "ts": self._ny_now_iso(),
                "source": str(path),
                "applied": best_params,
            }
            (self.runs_dir / today.isoformat() / "decision_log.yaml").write_text(_yaml.safe_dump(log, sort_keys=False), encoding="utf-8")
            # MLflow logging for pending execution (best-effort)
            try:
                from scripts.ai_trader.mlflow_utils import init_mlflow, start_run, log_metrics, log_params, log_artifacts, end_run
                mlf = (self.cfg.get("mlflow") or {})
                init_mlflow(mlf.get("tracking_uri"), mlf.get("experiment"), enabled=mlf.get("enabled", True))
                learn_obs = self._learning_observability()
                tags = {
                    "session": "morning",
                    "date": today.isoformat(),
                    "strategy_id": learn_obs.get("strategy_id"),
                    "learning_profile": learn_obs.get("learning_profile"),
                    "adapter_name": learn_obs.get("adapter_name"),
                    "pipeline_version": learn_obs.get("pipeline_version"),
                }
                start_run(run_name=f"pending_exec_{today.isoformat()}", tags=tags)
                # metrics: counts of pending by type and applied keys
                pa_counts = {"disable_rl": 0, "increase_conf_floor": 0, "reduce_sizing_multiplier": 0}
                for st in pending:
                    a = st.get("action")
                    if a in pa_counts and (approve is None or approve.get(a, False)):
                        pa_counts[a] += 1
                metrics = {
                    "pending_total": len(pending),
                    "applied_keys": len(best_params),
                    "applied_disable_rl": pa_counts["disable_rl"],
                    "applied_increase_conf_floor": pa_counts["increase_conf_floor"],
                    "applied_reduce_sizing_multiplier": pa_counts["reduce_sizing_multiplier"],
                    "pending_denied": len(denied_actions),
                    "pending_unsupported": int(unsupported),
                }
                log_metrics(metrics)
                lp = dict(best_params)
                if denied_actions:
                    lp["denied_actions"] = ",".join(denied_actions[:10])
                log_params(lp)
                arts = [str(path), str(temp), str(self.runs_dir / today.isoformat() / "decision_log.yaml")]
                log_artifacts(arts)
                end_run()
            except Exception:
                pass
        except Exception:
            pass
        return str(temp)
from libs.db.clickhouse import ClickHouseDB  # data readiness checks
