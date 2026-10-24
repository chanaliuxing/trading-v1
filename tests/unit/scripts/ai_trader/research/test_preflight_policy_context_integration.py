from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from scripts.ai_trader.research.orchestrator import ExperimentOrchestrator


def _specs(cycle_id: str) -> list[dict]:
    return [
        {
            "experiment_id": f"{cycle_id}-stocks-001",
            "cycle_id": cycle_id,
            "market": "stocks",
            "strategy_family": "stocks_scalp",
            "engine": {"adapter": "stocks_kernel_backtest", "mode": "backtest"},
            "universe": {"symbols": ["QQQ"]},
            "strategy_ref": {"template_id": "qqq_intraday_core"},
            "acceptance": {"min_trades": 1, "max_drawdown_pct": 20.0},
        },
        {
            "experiment_id": f"{cycle_id}-options-001",
            "cycle_id": cycle_id,
            "market": "options",
            "strategy_family": "options_scalp",
            "engine": {"adapter": "options_single_leg_replay", "mode": "replay"},
            "universe": {"underlying": "SPY", "symbols": ["SPY"]},
            "strategy_ref": {"definition_id": "SPY_INTRADAY_MOMENTUM_OPTION_PAPER_V1"},
            "acceptance": {"min_trades": 1, "max_drawdown_pct": 40.0},
        },
    ]


def _fake_scheduler():
    async def _fake_execute_job(job):
        spec = dict(job.get("params") or {})
        exp_id = str(spec.get("experiment_id"))
        market = str(spec.get("market") or "")
        return {
            "ok": True,
            "status": "completed",
            "experiment_result": {
                "experiment_id": exp_id,
                "cycle_id": str(spec.get("cycle_id")),
                "status": "completed",
                "market": market,
                "strategy_family": str(spec.get("strategy_family") or f"{market}_family"),
                "summary_metrics": {
                    "trades": 10,
                    "win_rate": 0.61,
                    "net_pnl": 420.0,
                    "sharpe": 1.3,
                    "max_drawdown_pct": 3.0,
                },
                "quality_flags": {"data_ok": True, "enough_trades": True, "passed_acceptance": True},
                "robustness": {
                    "ok": True,
                    "wf_pass": True,
                    "embargo_pass": True,
                    "leakage_score": 0.02,
                    "regime_stability": 0.8,
                    "symbol_transfer_score": 0.7,
                },
                "execution_reality": {"modeled_slippage_bps": 5.0, "fill_ratio": 0.96, "latency_bucket": "sub_2s"},
                "sizing": {},
                "lineage": {},
                "artifacts": {},
                "notes": [],
            },
        }

    return SimpleNamespace(execute_job=_fake_execute_job)


def _run_cycle(monkeypatch, tmp_path: Path, cycle_id: str) -> dict:
    from scripts.ai_trader.research import orchestrator as orch_mod

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(orch_mod, "RESEARCH_CYCLES_ROOT", tmp_path / "cycles")
    monkeypatch.setattr(orch_mod, "ml_scheduler", _fake_scheduler())

    orchestrator = ExperimentOrchestrator()
    return asyncio.run(
        orchestrator.execute_experiment_specs(
            cycle_id=cycle_id,
            experiment_specs=_specs(cycle_id),
            agent_tasks=[],
            agent_plan={"experiments": []},
            state_snapshot={"timestamp": "2026-04-10T00:00:00+00:00"},
            policy_snapshot={"version": "test-v1"},
        )
    )


def test_orchestrator_preflight_report_persists_policy_compatibility(monkeypatch, tmp_path: Path) -> None:
    state_root = tmp_path / "runs/ai_trader/research/state"
    state_root.mkdir(parents=True, exist_ok=True)
    (state_root / "preflight-policy.json").write_text(
        json.dumps({"policy_id": "preflight-policy", "version": "v3", "digest": "digest-v3"}, ensure_ascii=False),
        encoding="utf-8",
    )
    (state_root / "preflight_release_history.json").write_text(
        json.dumps(
            {
                "history": [
                    {"preflight_policy": {"policy_id": "preflight-policy", "version": "v1", "digest": "digest-v1"}},
                    {"preflight_policy": {"policy_id": "preflight-policy", "version": "v2", "digest": "digest-v2"}},
                    {"preflight_policy": {"policy_id": "preflight-policy", "version": "v3", "digest": "digest-v3"}},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = _run_cycle(monkeypatch, tmp_path, "2026-04-10-policy-known")
    preflight_path = Path(result["artifacts"]["preflight_check_json"])
    summary_path = Path(result["artifacts"]["policy_compatibility_summary_json"])
    human_ops_path = Path(result["artifacts"]["human_ops_summary_json"])
    preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    human_ops = json.loads(human_ops_path.read_text(encoding="utf-8"))
    compat = dict(preflight.get("policy_compatibility") or {})

    assert preflight_path.exists()
    assert summary_path.exists()
    assert human_ops_path.exists()
    assert compat
    assert compat.get("policy_state") == "known"
    assert summary.get("current_cycle", {}).get("policy_state") == "known"
    assert human_ops.get("policy_compatibility_observability", {}).get("status") == "ok"


def test_orchestrator_preflight_policy_context_missing_or_corrupt_history_fallback(monkeypatch, tmp_path: Path) -> None:
    state_root = tmp_path / "runs/ai_trader/research/state"
    state_root.mkdir(parents=True, exist_ok=True)
    (state_root / "preflight-policy.json").write_text(
        json.dumps({"policy_id": "preflight-policy", "version": "v3", "digest": "digest-v3"}, ensure_ascii=False),
        encoding="utf-8",
    )

    # Case 1: corrupt history file should degrade to current_only, never crash.
    (state_root / "preflight_release_history.json").write_text("{not-json", encoding="utf-8")
    corrupt_result = _run_cycle(monkeypatch, tmp_path, "2026-04-10-policy-corrupt-history")
    corrupt_preflight = json.loads(Path(corrupt_result["artifacts"]["preflight_check_json"]).read_text(encoding="utf-8"))
    corrupt_summary = json.loads(Path(corrupt_result["artifacts"]["policy_compatibility_summary_json"]).read_text(encoding="utf-8"))
    corrupt_compat = dict(corrupt_preflight.get("policy_compatibility") or {})
    assert corrupt_compat.get("policy_state") == "current_only"
    assert "no_policy_history_available" in list(corrupt_compat.get("advisories") or [])
    assert int(corrupt_summary.get("summary", {}).get("degraded_count") or 0) >= 1

    # Case 2: missing history file should keep the same fallback behavior.
    (state_root / "preflight_release_history.json").unlink(missing_ok=True)
    missing_result = _run_cycle(monkeypatch, tmp_path, "2026-04-10-policy-missing-history")
    missing_preflight = json.loads(Path(missing_result["artifacts"]["preflight_check_json"]).read_text(encoding="utf-8"))
    missing_compat = dict(missing_preflight.get("policy_compatibility") or {})
    assert missing_compat.get("policy_state") == "current_only"
    assert "no_policy_history_available" in list(missing_compat.get("advisories") or [])
