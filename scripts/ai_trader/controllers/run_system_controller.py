import subprocess
import shlex
import sys
import datetime
import os
from pathlib import Path


def _get_project_env():
    """Get environment with PYTHONPATH set to project root.

    Also sets OpenMP/PyTorch threading limits to prevent ML training crashes.
    Also sets ClickHouse connection to use native protocol port 9000.
    """
    project_root = Path(__file__).resolve().parent.parent.parent.parent
    env = os.environ.copy()
    env["PYTHONPATH"] = str(project_root)
    # Fix PyTorch LSTM/XGBoost/OpenMP threading crashes
    env["OMP_NUM_THREADS"] = "4"
    env["MKL_NUM_THREADS"] = "4"
    env["PYTORCH_NUM_THREADS"] = "4"
    env["OMP_NESTED"] = "FALSE"
    env["OMP_WAIT_POLICY"] = "PASSIVE"
    # Allow multiple OpenMP runtimes (PyTorch + XGBoost + sklearn) without segfault
    env["KMP_DUPLICATE_LIB_OK"] = "TRUE"
    # ClickHouse connection - use native protocol port 9000 (not HTTP port 8123)
    env.setdefault("CH_HOST", "localhost")
    env.setdefault("CH_PORT", "9000")
    env.setdefault("CH_DB", "trading")
    env.setdefault("CH_USER", "default")
    env.setdefault("CH_PASSWORD", "")
    return env, str(project_root)


class RunSystemController:
    """Controls run_system data ops and HPO/parameter flow via existing scripts."""

    def __init__(self, cmds: dict):
        self.cmds = cmds or {}

    def backfill_yesterday(self, symbols=None):
        cmd = self.cmds.get("backfill_yesterday")
        if not cmd:
            return
        date = (datetime.datetime.now() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        args = f" --date {date}"
        if symbols:
            arg_syms = " ".join(symbols)
            args += f" --symbols {arg_syms}"
        full = cmd + args
        parts = shlex.split(full)
        if parts and parts[0] in {"python", "python3"}:
            parts[0] = sys.executable
        env, cwd = _get_project_env()
        subprocess.check_call(parts, cwd=cwd, env=env)

    def alpha_loop(self):
        cmd = self.cmds.get("alpha_loop")
        if cmd:
            parts = shlex.split(cmd)
            if parts and parts[0] in {"python", "python3"}:
                parts[0] = sys.executable
            env, cwd = _get_project_env()
            subprocess.check_call(parts, cwd=cwd, env=env)

    def apply_params(
        self,
        hpo_params_path=None,
        strategy_config=None,
        output_path=None,
        cmd_override: str | None = None,
        strategy_id: str | None = None,
    ):
        cmd = cmd_override or self.cmds.get("apply_params")
        if not cmd:
            return
        parts = shlex.split(str(cmd))
        # Route to target strategy when command supports --strategy-id.
        if strategy_id:
            sid = str(strategy_id).strip()
            if sid:
                has_flag = "--strategy-id" in parts
                likely_apply_extras = any("apply_extras_params.py" in p for p in parts)
                if has_flag:
                    try:
                        idx = parts.index("--strategy-id")
                        if idx + 1 < len(parts):
                            parts[idx + 1] = sid
                        else:
                            parts.append(sid)
                    except Exception:
                        parts += ["--strategy-id", sid]
                elif likely_apply_extras:
                    parts += ["--strategy-id", sid]
        if hpo_params_path:
            parts += ["--hpo-params", hpo_params_path]
        if strategy_config:
            parts += ["--strategy-config", strategy_config]
        if output_path:
            parts += ["--output", output_path]
        if parts and parts[0] in {"python", "python3"}:
            parts[0] = sys.executable
        env, cwd = _get_project_env()
        subprocess.check_call(parts, cwd=cwd, env=env)

    def option_ml_train(self, days: int | None = None, start: str | None = None, end: str | None = None, symbols: str | None = None, auto_model_selection: bool = False):
        cmd = self.cmds.get("option_ml_train")
        if not cmd:
            return
        parts = shlex.split(cmd)
        if start and end:
            parts += ["--start", str(start), "--end", str(end)]
        elif days:
            parts += ["--days", str(days)]
        
        if symbols:
            parts += ["--symbols", str(symbols)]
        if auto_model_selection:
            parts += ["--auto-model-selection"]
        if parts and parts[0] in {"python", "python3"}:
            parts[0] = sys.executable
        env, cwd = _get_project_env()
        subprocess.check_call(parts, cwd=cwd, env=env)

    def rl_train(self):
        cmd = self.cmds.get("rl_train")
        if not cmd:
            return
        import shlex, subprocess
        try:
            parts = shlex.split(cmd)
            if parts and parts[0] in {"python", "python3"}:
                parts[0] = sys.executable
            env, cwd = _get_project_env()
            subprocess.check_call(parts, cwd=cwd, env=env)
        except Exception:
            # Best-effort; training may be skipped if deps missing
            pass

    def rl_eval(self, min_sharpe: float | None = None, max_mdd: float | None = None):
        cmd = self.cmds.get("rl_eval")
        if not cmd:
            return None
        import shlex, subprocess, json
        try:
            parts = shlex.split(cmd)
            if parts and parts[0] in {"python", "python3"}:
                parts[0] = sys.executable
            if min_sharpe is not None:
                parts += ["--min_sharpe", str(min_sharpe)]
            if max_mdd is not None:
                parts += ["--max_mdd", str(max_mdd)]
            env, cwd = _get_project_env()
            out = subprocess.check_output(parts, text=True, cwd=cwd, env=env)
            # Try to parse last JSON printed
            try:
                return json.loads(out.strip())
            except Exception:
                return {"raw": out[:500]}
        except Exception as e:
            return {"error": str(e)}
