from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.ai_trader.research import experiment_api


@pytest.fixture()
def temp_root(tmp_path, monkeypatch):
    research_root = tmp_path / "runs" / "ai_trader" / "research" / "experiments"
    log_root = tmp_path / "runs" / "ai_trader" / "experiments.jsonl"
    monkeypatch.setattr(experiment_api, "EXPERIMENTS_ROOT", research_root)
    monkeypatch.setattr(experiment_api, "EXP_LOG", log_root)
    return research_root


def test_stable_experiment_id_is_deterministic(temp_root):
    spec = {
        "cycle_id": "2026-04-04-nightly",
        "market": "stocks",
        "strategy_family": "intraday_scalp",
        "universe": {"symbols": ["QQQ"]},
        "strategy_ref": {"template_id": "qqq_intraday_core"},
        "parameters": {"entry_threshold": 0.6},
    }

    exp_id_a = experiment_api.stable_experiment_id(spec)
    exp_id_b = experiment_api.stable_experiment_id(dict(spec))

    assert exp_id_a == exp_id_b
    assert exp_id_a.startswith("exp-2026-04-04-nightly-stocks-intraday-scalp-qqq-")


def test_create_start_complete_and_fail_persist_state(temp_root):
    spec = {
        "cycle_id": "2026-04-04-nightly",
        "market": "options",
        "strategy_family": "single_leg",
        "lineage": {"family_id": "qqq-option-family", "mutation_type": "seed"},
        "universe": {"symbols": ["QQQ"]},
        "strategy_ref": {"definition_id": "QQQ_INTRADAY_OPTION_PARITY_V1"},
        "parameters": {"delta_min": 0.2},
    }

    exp_id = experiment_api.create_experiment(spec)
    loaded = experiment_api.load_experiment(exp_id, cycle_id="2026-04-04-nightly")
    assert loaded["spec"]["experiment_id"] == exp_id
    assert loaded["state"]["status"] == "created"
    assert loaded["lineage"]["experiment_id"] == exp_id
    assert loaded["paths"]["artifact_paths"]["trade_log"].endswith("trade_log.jsonl")

    running = experiment_api.start_experiment(exp_id)
    assert running["status"] == "running"

    result_state = experiment_api.complete_experiment(
        exp_id,
        status="completed",
        metrics={"sharpe": 1.25, "trades": 17},
        artifacts={"report": str(Path("dummy/report.json"))},
    )
    assert result_state["status"] == "completed"
    assert result_state["summary_metrics"]["sharpe"] == 1.25

    loaded_after = experiment_api.load_experiment(exp_id, cycle_id="2026-04-04-nightly")
    assert loaded_after["result"]["status"] == "completed"
    assert loaded_after["result"]["summary_metrics"]["trades"] == 17
    assert loaded_after["state"]["completed_at"] is not None

    failed = experiment_api.fail_experiment(
        exp_id,
        error="engine_timeout",
        metrics={"trades": 0},
    )
    assert failed["status"] == "failed"
    assert failed["error"] == "engine_timeout"
    loaded_failed = experiment_api.load_experiment(exp_id, cycle_id="2026-04-04-nightly")
    assert loaded_failed["state"]["status"] == "failed"
    assert loaded_failed["result"]["errors"] == ["engine_timeout"]


def test_artifact_paths_are_stable(temp_root):
    exp_id = "exp-foo"
    cycle_id = "2026-04-04-nightly"
    paths = experiment_api.artifact_paths(exp_id, cycle_id)

    assert paths["trade_log"].endswith("trade_log.jsonl")
    assert paths["equity_curve"].endswith("equity_curve.csv")
    assert paths["report"].endswith("report.json")
    assert Path(paths["robustness"]).parent.name == "artifacts"


def test_compare_variants_logs_global_event(temp_root):
    exp_id = experiment_api.create_experiment({
        "cycle_id": "2026-04-04-nightly",
        "market": "stocks",
        "strategy_family": "intraday_scalp",
    })

    experiment_api.compare_variants(exp_id, {"a": 1}, {"b": 2})

    assert experiment_api.EXP_LOG.exists()
    rows = [json.loads(line) for line in experiment_api.EXP_LOG.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert any(row["event"] == "experiment_compare" and row["experiment_id"] == exp_id for row in rows)
