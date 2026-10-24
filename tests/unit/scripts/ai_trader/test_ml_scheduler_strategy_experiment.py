from __future__ import annotations

from scripts.ai_trader import ml_scheduler


def test_normalize_strategy_experiment_preserves_engine_backtest_config() -> None:
    params = {
        "strategy_experiment": {
            "cycle_id": "2026-04-11",
            "market": "stocks",
            "strategy_family": "intraday_scalp",
            "engine": {
                "adapter": "stocks_kernel_backtest",
                "mode": "backtest",
                "backtest": {
                    "strategy_path": "strategies/stocks_kernel_strategy.py",
                    "strategy_class": "StocksKernelStrategy",
                    "csv": "tests/fixtures/stocks_kernel_minute.csv",
                    "slippage_bps": 3.0,
                },
            },
            "strategy_ref": {"template_id": "qqq_intraday_core"},
            "module_selection": {
                "features": ["vwap", "atr_eff"],
                "gates": ["session_window", "data_integrity"],
            },
            "data_window": {
                "start": "2026-04-01 09:30:00",
                "end": "2026-04-10 16:00:00",
            },
            "universe": {"symbols": ["QQQ"]},
        }
    }

    spec = ml_scheduler._normalize_strategy_experiment_spec("job-test-001", params)

    assert spec["engine"]["adapter"] == "stocks_kernel_backtest"
    assert spec["engine"]["mode"] == "backtest"
    assert spec["engine"]["backtest"]["strategy_path"] == "strategies/stocks_kernel_strategy.py"
    assert spec["engine"]["backtest"]["strategy_class"] == "StocksKernelStrategy"
    assert spec["engine"]["backtest"]["csv"] == "tests/fixtures/stocks_kernel_minute.csv"
    assert spec["engine"]["backtest"]["slippage_bps"] == 3.0


def test_build_strategy_result_infers_success_from_completed_status_without_ok() -> None:
    spec = {
        "experiment_id": "exp-1",
        "cycle_id": "2026-04-11",
        "market": "options",
        "strategy_family": "seed",
        "engine": {"adapter": "options_single_leg_replay", "mode": "replay"},
        "acceptance": {"min_trades": 1, "max_drawdown_pct": 50.0},
    }
    validation = {"ok": True, "issues": [], "spec": spec}
    run_result = {
        "status": "completed",
        "summary_metrics": {"trades": 3, "max_drawdown_pct": 1.0},
        "artifacts": [],
    }
    robustness_result = {"ok": True, "status": "passed", "metrics": {}, "artifacts": []}

    out = ml_scheduler._build_strategy_experiment_result(
        spec=spec,
        validation=validation,
        run_result=run_result,
        robustness_result=robustness_result,
        job_id="job-1",
    )

    assert out["ok"] is True
    assert out["status"] == "completed"
    assert "strategy_runner_failed" not in (out.get("notes") or [])


def test_build_strategy_result_prefers_run_notes_over_generic_failure_label() -> None:
    spec = {
        "experiment_id": "exp-2",
        "cycle_id": "2026-04-11",
        "market": "stocks",
        "strategy_family": "seed",
        "engine": {"adapter": "stocks_kernel_backtest", "mode": "backtest"},
        "acceptance": {"min_trades": 1, "max_drawdown_pct": 50.0},
    }
    validation = {"ok": True, "issues": [], "spec": spec}
    run_result = {
        "status": "blocked",
        "ok": False,
        "summary_metrics": {"trades": 0, "max_drawdown_pct": 0.0},
        "notes": ["no_runner_or_engine_configured"],
        "artifacts": [],
    }
    robustness_result = {"ok": False, "status": "failed", "metrics": {}, "artifacts": []}

    out = ml_scheduler._build_strategy_experiment_result(
        spec=spec,
        validation=validation,
        run_result=run_result,
        robustness_result=robustness_result,
        job_id="job-2",
    )

    assert out["ok"] is False
    assert out["status"] == "blocked"
    assert "no_runner_or_engine_configured" in (out.get("notes") or [])
    assert "strategy_runner_failed" not in (out.get("notes") or [])
