from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from services.backtest.stocks_kernel_adapter import StocksKernelBacktestAdapter


def _valid_spec() -> dict:
    return {
        "experiment_id": "exp-stocks-qqq-001",
        "cycle_id": "2026-04-04-nightly",
        "market": "stocks",
        "strategy_family": "intraday_scalp",
        "engine": {
            "adapter": "stocks_kernel_backtest",
            "mode": "backtest",
        },
        "universe": {
            "symbols": ["QQQ"],
        },
        "strategy_ref": {
            "template_id": "qqq_intraday_core",
            "definition_id": None,
        },
        "module_selection": {
            "features": ["vwap", "atr_eff", "vol_surge"],
            "gates": ["session_window", "risk_budget"],
            "risk_policy": "default_intraday",
            "execution_policy": "paper_fill_v1",
        },
        "parameters": {
            "entry_threshold": 0.6,
            "max_hold_minutes": 12,
            "risk_per_trade_bps": 15,
        },
        "data_window": {
            "start": "2026-03-01 09:30:00",
            "end": "2026-04-03 16:00:00",
            "session": "rth",
        },
        "labels_and_ml": {
            "label_profile": "intraday_h5",
            "model_hint": "xgb_intraday_v3",
        },
        "robustness": {
            "required_checks": ["walk_forward", "leakage"],
            "min_walk_forward_folds": 3,
        },
        "execution_reality": {
            "cost_model_id": "default_cost_v1",
            "fill_model_id": "paper_fill_v1",
            "latency_model_id": "baseline_latency_v1",
            "modeled_slippage_bps": 7.5,
            "spread_cost_bps": 2.0,
        },
        "sizing": {
            "sizing_policy_id": "risk_budget_v1",
            "capital_quota_policy_id": "default_quota_v1",
            "de_risk_on_drawdown": True,
        },
        "lineage": {
            "family_id": "qqq-intraday-scalp",
            "parent_experiment_id": None,
            "mutation_type": "seed",
        },
        "acceptance": {
            "min_trades": 2,
            "max_drawdown_pct": 5.0,
        },
    }


def test_validate_accepts_minimal_valid_spec():
    adapter = StocksKernelBacktestAdapter()

    validation = adapter.validate(_valid_spec())

    assert validation["ok"] is True
    assert validation["issues"] == []
    assert validation["normalized_spec"]["engine"]["adapter"] == "stocks_kernel_backtest"
    assert validation["normalized_spec"]["universe"]["symbols"] == ["QQQ"]


@pytest.mark.parametrize(
    "mutator, expected_issue",
    [
        (lambda spec: spec.pop("experiment_id"), "experiment_id_is_required"),
        (lambda spec: spec["engine"].__setitem__("adapter", "wrong_adapter"), "engine.adapter_must_be_stocks_kernel_backtest"),
        (lambda spec: spec["universe"].__setitem__("symbols", []), "universe.symbols_is_required"),
        (lambda spec: spec["strategy_ref"].__setitem__("template_id", "unknown_template"), "unknown_strategy_template:unknown_template"),
        (lambda spec: spec["data_window"].pop("start"), "data_window.start_and_end_are_required"),
    ],
)
def test_validate_rejects_invalid_spec(mutator, expected_issue):
    adapter = StocksKernelBacktestAdapter()
    spec = _valid_spec()
    mutator(spec)

    validation = adapter.validate(spec)

    assert validation["ok"] is False
    assert expected_issue in validation["issues"]


def test_run_uses_injected_runner_and_summarizes_results():
    captured = {}

    def runner(normalized_spec: dict) -> dict:
        captured["spec"] = normalized_spec
        return {
            "status": "completed",
            "metrics": {
                "final_nav": 101500.0,
                "cash": 101500.0,
                "realized_pnl": 1500.0,
                "trades": 3,
                "win_rate": 0.6666667,
                "profit_factor": 1.8,
                "total_fee": 4.5,
            },
            "ledger": [
                {"qty": 100, "realized_pnl": 500.0, "fee": 1.5},
                {"qty": 100, "realized_pnl": -200.0, "fee": 1.5},
                {"qty": 100, "realized_pnl": 1200.0, "fee": 1.5},
            ],
            "snapshot": {"equity": 101500.0, "cash": 101500.0, "positions": {}},
            "bars_processed": 42,
            "equity_curve": [
                {"ts": 1, "equity": 100000.0},
                {"ts": 2, "equity": 101000.0},
                {"ts": 3, "equity": 99000.0},
                {"ts": 4, "equity": 101500.0},
            ],
            "trades": [
                {"realized_pnl": 500.0},
                {"realized_pnl": -200.0},
                {"realized_pnl": 1200.0},
            ],
        }

    adapter = StocksKernelBacktestAdapter(runner=runner, artifacts_root="runs/test_artifacts")

    raw_result = adapter.run(_valid_spec())
    summary = adapter.summarize(raw_result)

    assert captured["spec"]["engine"]["adapter"] == "stocks_kernel_backtest"
    assert raw_result["source"] == "runner"
    assert summary["experiment_id"] == "exp-stocks-qqq-001"
    assert summary["status"] == "completed"
    assert summary["ok"] is True
    assert summary["summary_metrics"]["trades"] == 3
    assert summary["summary_metrics"]["win_rate"] == pytest.approx(2 / 3, rel=1e-6)
    assert summary["summary_metrics"]["net_pnl"] == pytest.approx(1500.0, rel=1e-6)
    assert summary["summary_metrics"]["max_drawdown_pct"] > 0.0
    assert summary["quality_flags"]["data_ok"] is True
    assert summary["quality_flags"]["enough_trades"] is True
    assert summary["quality_flags"]["passed_acceptance"] is True
    assert summary["execution_reality"]["cost_model_id"] == "default_cost_v1"
    assert summary["execution_reality"]["modeled_slippage_bps"] == pytest.approx(7.5, rel=1e-6)
    assert summary["artifacts"]["trade_log"].endswith("trades.jsonl")


def test_run_without_runner_returns_placeholder_summary():
    adapter = StocksKernelBacktestAdapter(artifacts_root="runs/test_artifacts")

    raw_result = adapter.run(_valid_spec())
    summary = adapter.summarize(raw_result)

    assert raw_result["status"] == "blocked"
    assert raw_result["source"] == "placeholder"
    assert summary["ok"] is False
    assert summary["quality_flags"]["data_ok"] is False
    assert summary["quality_flags"]["enough_trades"] is False
    assert summary["quality_flags"]["passed_acceptance"] is False
    assert summary["execution_reality"]["latency_bucket"] == "unknown"
    assert summary["summary_metrics"]["trades"] == 0


def test_fixture_backed_experiment_writes_artifacts_and_result(tmp_path: Path):
    artifact_root = tmp_path / "stocks_e2e"
    spec = _valid_spec()
    spec["experiment_id"] = "exp-stocks-fixture-e2e"
    spec["artifact_root"] = str(artifact_root / spec["experiment_id"])

    def runner(normalized_spec: dict) -> dict:
        assert normalized_spec["experiment_id"] == "exp-stocks-fixture-e2e"
        assert normalized_spec["engine"]["adapter"] == "stocks_kernel_backtest"
        return {
            "status": "completed",
            "metrics": {
                "final_nav": 100900.0,
                "cash": 100900.0,
                "realized_pnl": 900.0,
                "trades": 2,
                "win_rate": 0.5,
                "profit_factor": 1.25,
                "total_fee": 3.0,
            },
            "ledger": [
                {"ts": 1, "symbol": "QQQ", "qty": 100, "price": 500.0, "realized_pnl": 250.0, "fee": 1.5},
                {"ts": 2, "symbol": "QQQ", "qty": 100, "price": 501.0, "realized_pnl": 650.0, "fee": 1.5},
            ],
            "equity_curve": [
                {"ts": 1, "equity": 100000.0},
                {"ts": 2, "equity": 100250.0},
                {"ts": 3, "equity": 100900.0},
            ],
            "trades": [
                {"ts": 1, "symbol": "QQQ", "realized_pnl": 250.0},
                {"ts": 2, "symbol": "QQQ", "realized_pnl": 650.0},
            ],
            "snapshot": {"equity": 100900.0, "cash": 100900.0, "positions": {}},
            "bars_processed": 3,
        }

    adapter = StocksKernelBacktestAdapter(runner=runner, artifacts_root=artifact_root)
    raw_result = adapter.run(spec)
    summary = adapter.summarize(raw_result)

    assert summary["status"] == "completed"
    assert summary["quality_flags"]["data_ok"] is True
    assert summary["quality_flags"]["passed_acceptance"] is True
    assert summary["execution_reality"]["cost_model_id"] == "default_cost_v1"
    assert summary["execution_reality"]["fill_model_id"] == "paper_fill_v1"
    assert summary["execution_reality"]["modeled_slippage_bps"] == pytest.approx(7.5, rel=1e-6)
    assert summary["artifacts"]["artifact_root"].startswith(str(artifact_root))

    result_json = Path(summary["artifacts"]["result_json"])
    raw_result_json = Path(summary["artifacts"]["raw_result"])
    trade_log = Path(summary["artifacts"]["trade_log"])
    equity_curve = Path(summary["artifacts"]["equity_curve"])
    report_json = Path(summary["artifacts"]["report"])

    for path in (result_json, raw_result_json, trade_log, equity_curve, report_json):
        assert path.exists(), path

    payload = json.loads(result_json.read_text(encoding="utf-8"))
    assert payload["experiment_id"] == "exp-stocks-fixture-e2e"
    assert payload["summary_metrics"]["trades"] == 2
    assert payload["execution_reality"]["fill_ratio"] > 0.0
    assert payload["artifacts"]["result_json"] == str(result_json)


def test_adapter_builds_real_stocks_kernel_runtime_extras_and_symbols():
    adapter = StocksKernelBacktestAdapter()
    spec = _valid_spec()
    spec["experiment_id"] = "exp-stocks-spy-001"
    spec["universe"]["symbols"] = ["SPY"]
    spec["strategy_ref"]["template_id"] = "spy_intraday_core"
    spec["module_selection"]["features"] = ["vwap", "atr_eff", "v3m"]
    spec["module_selection"]["gates"] = ["session_window", "freshness", "seed_ready"]
    spec["parameters"] = {
        "policy_id": "spy_intraday_policy_v1",
        "confirm_symbol": "RSP",
        "long_exec_symbol": "SPXL",
        "short_exec_symbol": "SPXS",
        "backtest_exec_proxy_mode": "signal_clone",
        "entry_model": "v47_fast_drift",
        "confirm_feature_prefix": "rsp",
    }

    normalized = adapter.validate(spec)["normalized_spec"]
    runtime_extras = adapter._build_strategy_runtime_extras(normalized)
    data_symbols = adapter._required_data_symbols(normalized)

    assert runtime_extras["strategy_template"] == "spy_intraday_core"
    assert runtime_extras["policy_id"] == "spy_intraday_policy_v1"
    assert runtime_extras["dry_run"] is False
    assert runtime_extras["startup_preflight_enabled"] is False
    assert runtime_extras["feature_set"] == ["vwap", "atr_eff", "v3m"]
    assert runtime_extras["gate_pipeline"] == ["session_window", "freshness", "seed_ready"]
    assert runtime_extras["universe"][0]["signal_symbol"] == "SPY"
    assert runtime_extras["universe"][0]["confirm_symbol"] == "RSP"
    assert runtime_extras["universe"][0]["long_exec_symbol"] == "SPXL"
    assert runtime_extras["universe"][0]["short_exec_symbol"] == "SPXS"
    assert runtime_extras["policy_params"]["entry_model"] == "v47_fast_drift"
    assert runtime_extras["policy_params"]["confirm_feature_prefix"] == "rsp"
    assert "backtest_exec_proxy_mode" not in runtime_extras["policy_params"]
    assert data_symbols == ["SPY", "RSP", "SPXL", "SPXS"]
