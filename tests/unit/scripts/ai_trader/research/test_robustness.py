from __future__ import annotations

from scripts.ai_trader.research.robustness import (
    DefaultRobustnessAdapter,
    attach_robustness_to_result,
    normalize_robustness_result,
    run_default_robustness,
)


def _good_spec() -> dict[str, object]:
    return {
        "experiment_id": "exp-good-001",
        "cycle_id": "cycle-001",
        "robustness": {
            "required_checks": ["walk_forward", "leakage", "regime_split", "cross_symbol"],
            "leakage_score_max": 0.05,
            "regime_stability_min": 0.7,
            "symbol_transfer_min": 0.6,
        },
    }


def _good_raw_result() -> dict[str, object]:
    return {
        "robustness": {
            "wf_pass": True,
            "embargo_pass": True,
            "leakage_score": 0.01,
            "regime_stability": 0.82,
            "symbol_transfer_score": 0.71,
            "notes": ["walk-forward passed on all folds"],
        }
    }


def _blocked_raw_result() -> dict[str, object]:
    return {
        "status": "blocked",
        "ok": False,
        "error": "validation_failed",
        "metrics": {},
    }


def test_default_robustness_adapter_normalizes_and_attaches_result() -> None:
    adapter = DefaultRobustnessAdapter()
    spec = _good_spec()

    validation = adapter.validate_inputs(spec)
    assert validation["ok"] is True
    assert validation["required_checks"] == ["walk_forward", "leakage", "regime_split", "cross_symbol"]

    normalized = adapter.run(spec, _good_raw_result())

    assert normalized["ok"] is True
    assert normalized["status"] == "ok"
    assert normalized["wf_pass"] is True
    assert normalized["embargo_pass"] is True
    assert set(normalized["passed_checks"]) == {"walk_forward", "leakage", "regime_split", "cross_symbol"}
    assert normalized["failed_checks"] == []

    experiment_result = attach_robustness_to_result({"experiment_id": "exp-good-001"}, normalized)
    assert experiment_result["robustness"]["ok"] is True
    assert experiment_result["robustness"]["leakage_score"] == 0.01


def test_normalize_robustness_result_fails_when_signals_break_thresholds() -> None:
    spec = _good_spec()
    raw = {
        "robustness": {
            "wf_pass": False,
            "embargo_pass": False,
            "leakage_score": 0.40,
            "regime_stability": 0.20,
            "symbol_transfer_score": 0.10,
        }
    }

    normalized = normalize_robustness_result(spec, raw)

    assert normalized.ok is False
    assert normalized.status == "fail"
    assert "walk_forward" in normalized.failed_checks
    assert "embargo" in normalized.failed_checks
    assert "leakage_score_too_high" in normalized.issues
    assert "regime_stability_too_low" in normalized.issues
    assert "symbol_transfer_score_too_low" in normalized.issues
    assert normalized.failure_type == "threshold_not_met"
    assert normalized.next_action is not None


def test_default_robustness_runner_populates_completed_result() -> None:
    spec = _good_spec()
    raw = {
        "status": "completed",
        "ok": True,
        "summary_metrics": {"trades": 10, "net_pnl": 42.0},
    }

    result = run_default_robustness(spec, raw)

    assert result["ok"] is True
    assert result["status"] == "ok"
    assert result["wf_pass"] is True
    assert result["embargo_pass"] is True
    assert result["leakage_score"] is not None
    assert result["regime_stability"] is not None
    assert result["symbol_transfer_score"] is not None
    assert "walk_forward" in result["passed_checks"]
    assert "leakage" in result["passed_checks"]


def test_default_robustness_runner_populates_blocked_result() -> None:
    spec = _good_spec()
    raw = _blocked_raw_result()

    result = run_default_robustness(spec, raw)

    assert result["ok"] is False
    assert result["status"] == "fail"
    assert result["wf_pass"] is False
    assert result["embargo_pass"] is False
    assert result["leakage_score"] is not None
    assert result["regime_stability"] is not None
    assert result["symbol_transfer_score"] is not None
    assert "walk_forward_failed" in result["issues"]
    assert "embargo_failed" in result["issues"]
    assert result["failure_type"] in {"data_insufficient", "runtime_error", "unknown_failure", "threshold_not_met"}
    assert result["next_action"] is not None
