"""Prefect-based evening_learning flow (optional).

Enables robust, retryable DAG execution with task-level observability.

Usage (programmatic):
  from scripts.ai_trader.flows.prefect_evening import evening_learning_flow
  evening_learning_flow()

To develop locally with Prefect UI and agents, install prefect:
  pip install prefect
  prefect server start &
  prefect agent start -q 'default' &

Notes:
  - This flow reuses existing Orchestrator helpers and controllers where possible.
  - It writes artifacts to runs/ai_trader/YYYY-MM-DD similar to the Orchestrator path.
"""

from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo
import json
import shlex
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

from prefect import flow, task

NY_TZ = ZoneInfo("America/New_York")

from scripts.ai_trader.orchestrator import Orchestrator


def _today_str() -> str:
    return datetime.date.today().isoformat()


def _run(cmd: str) -> None:
    subprocess.check_call(shlex.split(cmd))


@task(name="reflection_and_evolution", retries=2, retry_delay_seconds=10)
def t_reflection_and_evolution(dstr: Optional[str] = None) -> Dict[str, Any]:
    """Run reflection consistency job and prompt evolver, return artifact paths."""
    d = dstr or _today_str()
    o = Orchestrator()
    out_dir = o.runs_dir / d
    out_dir.mkdir(parents=True, exist_ok=True)
    ref_json = out_dir / "reflection.json"
    sugg_out = out_dir / "prompt_suggestions.json"
    _run(
        f"python scripts/ai_trader/reflection/consistency_job.py --date {d} --runs-dir {o.runs_dir}"
    )
    _run(
        f"python scripts/ai_trader/evolvers/prompt_evolver.py --reflection {ref_json} --out {sugg_out} --auto-apply"
    )
    return {"reflection": str(ref_json), "prompt_suggestions": str(sugg_out)}


@task(name="alpha_loop", retries=1)
def t_alpha_loop() -> str:
    Orchestrator().rs.alpha_loop()
    return "done"


@task(name="discover_best_params", retries=1)
def t_discover_best_params() -> Optional[Dict[str, Any]]:
    o = Orchestrator()
    return o._discover_best_params()


@task(name="hpo_gate", retries=1)
def t_hpo_gate(oos: Dict[str, Any]) -> Dict[str, Any]:
    o = Orchestrator()
    gate_cfg = (o.cfg.get("policies", {}) or {}).get("hpo_deploy", {})
    return o._hpo_deploy_gate(oos or {}, gate_cfg)


@task(name="brain_hpo_decision", retries=2, retry_delay_seconds=5)
def t_brain_hpo_decision(best: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    o = Orchestrator()
    if not o.brain_enabled:
        return None
    chmp, chall = o._build_ab_inputs(best)
    ctx = {"champion": chmp, "challenger": chall, "oos": best.get("oos") or {}}
    d = o._brain_decide("hpo_deploy", ctx)
    return o._validate_hpo_deploy_decision(d) if d else None


@task(name="write_temp_best_params")
def t_write_temp_best_params(best_params: Dict[str, Any]) -> str:
    o = Orchestrator()
    return o._write_temp_best_params(best_params)


@task(name="apply_params", retries=1)
def t_apply_params(params_path: str) -> Dict[str, Any]:
    res = Orchestrator()._apply_params_with_scope(hpo_params_path=params_path)
    return {"params_file": params_path, "result": res}


@task(name="ab_update", retries=1)
def t_ab_update(best: Dict[str, Any], decision: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    o = Orchestrator()
    chmp, chall = o._build_ab_inputs(best)
    if isinstance(decision, dict) and isinstance(decision.get("ab_weights"), dict):
        plan = {"weights": decision.get("ab_weights"), "reason": "brain_override"}
    else:
        plan = o.ab.propose(chmp, chall)
    reg = {
        "champion": chmp,
        "challenger": chall,
        "weights": plan["weights"],
        "updated_at": datetime.datetime.now(NY_TZ).isoformat(),
    }
    o.ab.save_registry(reg)
    return plan


@task(name="store_memory_episode", retries=1)
def t_store_memory_episode(best: Optional[Dict[str, Any]], hpo_gate: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    from scripts.ai_trader.memory.episode_store import store_episode
    o = Orchestrator()
    mem = store_episode(
        runs_dir=o.cfg["paths"]["runs_dir"],
        session="learn",
        best=best,
        hpo_gate=hpo_gate,
        notes=("; ".join(hpo_gate.get("reasons", [])) if hpo_gate else None),
    )
    return {k: v for k, v in mem.items() if k in ("saved", "save_error")}


@task(name="rl_train", retries=0)
def t_rl_train() -> str:
    Orchestrator().rs.rl_train()
    return "triggered"


@task(name="rl_eval", retries=0)
def t_rl_eval() -> Dict[str, Any]:
    o = Orchestrator()
    rl_policy = (o.cfg.get("policies", {}) or {}).get("rl_deploy", {})
    min_sh = rl_policy.get("min_sharpe")
    max_md = rl_policy.get("max_mdd")
    return o.rs.rl_eval(min_sharpe=min_sh, max_mdd=max_md) or {}


@task(name="rl_toggle_if_needed", retries=0)
def t_rl_toggle_if_needed(rl_eval: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    o = Orchestrator()
    g = (rl_eval or {}).get("gate") or {}
    if not isinstance(g, dict):
        return None
    target_enable = bool(g.get("ok", False))
    approve = True
    if o.brain_enabled:
        try:
            pend = {"pending": [{"action": ("enable_rl" if target_enable else "disable_rl"), "params": {}}]}
            dec = o._brain_decide("execute_actions", pend)
            if isinstance(dec, dict) and isinstance(dec.get("approve"), list):
                amap = {it.get("action"): bool(it.get("ok")) for it in dec.get("approve")}
                approve = bool(amap.get("enable_rl" if target_enable else "disable_rl", False))
        except Exception:
            approve = True
    if not approve:
        return {"skipped": True}
    # Apply rl.enabled
    best_params = {"rl.enabled": target_enable}
    temp = o.runs_dir / datetime.date.today().isoformat() / "rl_toggle.json"
    temp.parent.mkdir(parents=True, exist_ok=True)
    temp.write_text(json.dumps(best_params, indent=2), encoding="utf-8")
    apply_res = o._apply_params_with_scope(hpo_params_path=str(temp))
    (o.runs_dir / datetime.date.today().isoformat() / "rl_decision.json").write_text(
        json.dumps({"target": target_enable, "rl_eval": rl_eval}, indent=2), encoding="utf-8"
    )
    return {"enabled": target_enable, "result": apply_res}


@task(name="build_daily_report", retries=1)
def t_build_daily_report(dstr: Optional[str] = None) -> str:
    d = dstr or _today_str()
    o = Orchestrator()
    out_dir = o.runs_dir / d
    out_dir.mkdir(parents=True, exist_ok=True)
    _run(
        f"python scripts/ai_trader/context/context_analyzer.py --date {d} --runs-dir {o.runs_dir}"
    )
    _run(
        f"python scripts/ai_trader/reflection/analyzer.py --date {d} --runs-dir {o.runs_dir}"
    )
    ctx = json.loads((out_dir / "context.json").read_text("utf-8")) if (out_dir / "context.json").exists() else {}
    dxp = json.loads((out_dir / "reflection_dxp.json").read_text("utf-8")) if (out_dir / "reflection_dxp.json").exists() else {}
    from scripts.ai_trader.reflection.report_builder import build_daily_report
    p = build_daily_report(str(out_dir), ctx, dxp, {}, {})
    return p


@task(name="portfolio_optimize", retries=1)
def t_portfolio_optimize() -> Dict[str, Any]:
    o = Orchestrator()
    import subprocess, shlex
    subprocess.check_call(shlex.split("python scripts/ai_trader/portfolio/optimizer.py --config config.yml --apply"))
    d = Path(o.runs_dir) / _today_str()
    return {"apply": str(d / "portfolio_apply.json")}


@task(name="portfolio_overlay", retries=1)
def t_portfolio_overlay() -> Dict[str, Any]:
    o = Orchestrator()
    import subprocess, shlex
    subprocess.check_call(shlex.split(f"python scripts/ai_trader/portfolio/overlay.py --config config.yml --runs-dir {o.runs_dir}"))
    d = Path(o.runs_dir) / _today_str()
    return {"overlay": str(d / "overlay_hedge.json")}


@flow(name="Evening Learning Flow")
def evening_learning_flow(run_date: Optional[str] = None) -> Dict[str, Any]:
    d = run_date or _today_str()

    # 1. 并行早期环节
    ref_f = t_reflection_and_evolution.submit(d)
    al_f = t_alpha_loop.submit()

    # 2. HPO 路径
    best_f = t_discover_best_params.submit(wait_for=[al_f])
    best = best_f.result()

    hpo_gate = None
    decision = None
    apply_res = None
    ab_plan = None

    if best and best.get("best_params"):
        hpo_gate_f = t_hpo_gate.submit(best.get("oos") or {})
        hpo_gate = hpo_gate_f.result()
        decision_f = t_brain_hpo_decision.submit(best)
        decision = decision_f.result()
        if (hpo_gate or {}).get("ok") and (decision is None or decision.get("apply_params")):
            params_path_f = t_write_temp_best_params.submit(best["best_params"])
            apply_res_f = t_apply_params.submit(params_path_f)
            apply_res = apply_res_f.result()
            # 更新 AB 注册依赖参数写回成功
            ab_plan_f = t_ab_update.submit(best, decision, wait_for=[apply_res_f])
            ab_plan = ab_plan_f.result()

    # 3. 记忆/训练/评估/日报
    port_f = t_portfolio_optimize.submit()
    over_f = t_portfolio_overlay.submit()
    mem_f = t_store_memory_episode.submit(best, hpo_gate)
    rl_train_f = t_rl_train.submit()
    rl_eval_f = t_rl_eval.submit(wait_for=[rl_train_f])
    rl_eval = rl_eval_f.result()
    rl_toggle_f = t_rl_toggle_if_needed.submit(rl_eval)
    report_f = t_build_daily_report.submit(d, wait_for=[ref_f])

    return {
        "status": "ok",
        "artifacts": ref_f.result(),
        "hpo_gate": hpo_gate,
        "decision": decision,
        "apply_params": apply_res,
        "ab": ab_plan,
        "portfolio": port_f.result(),
        "overlay": over_f.result(),
        "memory": mem_f.result(),
        "rl_eval": rl_eval,
        "rl_toggle": rl_toggle_f.result(),
        "report": report_f.result(),
    }


if __name__ == "__main__":
    print(json.dumps(evening_learning_flow(), indent=2))
