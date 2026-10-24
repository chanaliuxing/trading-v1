from __future__ import annotations
import json
from pathlib import Path

from scripts.ai_trader.brain.policy.validators import build_ml_stage0, validate_llm_review_output
from scripts.ai_trader.brain.policy.memory import summarize_actions, summarize_actions_by_symbol


def test_build_ml_stage0_large_window_blocks_extend(tmp_path: Path):
    ctx = {
        "oof_evaluations": {
            "TSLA": {
                "n_bars": 30000,
                "fusion": {"ic": 0.005, "auc": 0.502},
            }
        },
        "runs_dir": str(tmp_path),
    }
    s0 = build_ml_stage0(ctx)
    assert s0["derived_flags"]["large_window_toxicity"] is True
    assert "extend_window" not in s0["allowed_actions"], "extend_window must be removed under toxicity"


def test_build_ml_stage0_no_edge_longrun_flag(tmp_path: Path):
    ctx = {
        "ml_health_recent": {
            "fusion_ic_mean": 0.0,
            "fusion_auc_mean": 0.501,
            "no_edge_simple": True,
        },
        "runs_dir": str(tmp_path),
    }
    s0 = build_ml_stage0(ctx)
    assert s0["derived_flags"].get("no_edge_longrun") is True
    # In long-run no-edge regime without hard data-quality issues,
    # backfill should be removed from allowed_actions to push Brain
    # toward experiment-style actions instead of repeated backfill.
    assert "backfill" not in s0["allowed_actions"]


def test_validate_llm_review_output_numbers_and_actions():
    allowed_actions = ["reduce_window", "queue_job"]
    allowed_numbers = [0.86, 30.0, 60.0]
    # Valid
    out_ok = {
        "next_actions": [{"action": "reduce_window", "confidence": 0.86, "params": {"window_days": 60}}],
        "numbers_used": [0.86, 60.0],
    }
    ok, reason = validate_llm_review_output(out_ok, allowed_actions, allowed_numbers)
    assert ok, reason
    # Illegal action
    out_bad_action = {
        "next_actions": [{"action": "extend_window", "confidence": 0.8, "params": {"window_days": 60}}]
    }
    ok, reason = validate_llm_review_output(out_bad_action, allowed_actions, allowed_numbers)
    assert not ok and "action_not_allowed" in reason
    # Fabricated number
    out_bad_num = {
        "next_actions": [{"action": "reduce_window", "confidence": 0.86, "params": {"window_days": 45}}],
        "numbers_used": [0.86, 45.0],
    }
    ok, reason = validate_llm_review_output(out_bad_num, allowed_actions, allowed_numbers)
    assert not ok and "fabricated_number_detected" in reason


def test_stage0_carries_feature_build_status():
    ctx = {
        "runs_dir": "/tmp",
        "feature_build_status": {"ok": True, "version": "v3_intraday", "path": "/tmp/status.json"},
    }
    s0 = build_ml_stage0(ctx)
    assert "feature_build_status" in s0
    assert s0["feature_build_status"].get("ok") is True


def test_memory_summarize_actions(tmp_path: Path, monkeypatch):
    runs = tmp_path
    dstr = "2025-11-07"
    day_dir = runs / dstr
    day_dir.mkdir(parents=True)
    audit = day_dir / "ml_actions.jsonl"
    lines = [
        {"ts": f"{dstr}T01:00:00", "action": "queue_job", "status": "ok"},
        {"ts": f"{dstr}T02:00:00", "action": "queue_job", "status": "failed"},
        {"ts": f"{dstr}T03:00:00", "action": "reduce_window", "status": "ok", "params": {"symbols": ["TSLA"]}},
        {"ts": f"{dstr}T04:00:00", "action": "reduce_window", "status": "skipped", "params": {"symbol": "AAPL"}},
        {"ts": f"{dstr}T05:00:00", "action": "reduce_window", "status": "failed", "params": {"symbols": ["TSLA","AAPL"]}},
    ]
    with audit.open("w", encoding="utf-8") as f:
        for j in lines:
            f.write(json.dumps(j) + "\n")
    by_action = summarize_actions(str(runs), days=7)
    assert by_action.get("queue_job", {}).get("attempts", 0) == 2
    assert by_action.get("reduce_window", {}).get("attempts", 2) == 2
    by_sym = summarize_actions_by_symbol(str(runs), days=7)
    rw = by_sym.get("reduce_window", {})
    # TSLA appears in two attempts (ok + failed)
    assert rw.get("TSLA", {}).get("attempts", 0) == 2
    # AAPL appears once (skipped ignored) + one fail
    assert rw.get("AAPL", {}).get("attempts", 0) == 1
