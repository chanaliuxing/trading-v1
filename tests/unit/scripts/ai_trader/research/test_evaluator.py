from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pandas as pd

from services.backtest.stocks_kernel_adapter import StocksKernelBacktestAdapter
from services.option_replay.core.types import ContractQuote
from services.option_replay.research_adapter import OptionReplayResearchAdapter
from scripts.ai_trader.research.evaluator import (
    build_portfolio_evaluation,
    build_promotion_decision,
    compute_complexity_penalty,
    compute_quality_gates,
    evaluate_candidates,
    normalize_costs,
    persist_evaluation_bundle,
    score_experiment,
)
from scripts.ai_trader.research.robustness import normalize_robustness_result


def _spec() -> dict[str, object]:
    return {
        "experiment_id": "exp-good-001",
        "cycle_id": "cycle-001",
        "robustness": {
            "required_checks": ["walk_forward", "leakage", "regime_split", "cross_symbol"],
            "leakage_score_max": 0.05,
            "regime_stability_min": 0.7,
            "symbol_transfer_min": 0.6,
        },
        "module_selection": {
            "features": ["vwap", "atr_eff", "vol_surge"],
            "gates": ["session_window", "risk_budget"],
        },
        "parameters": {
            "entry_threshold": 0.6,
            "max_hold_minutes": 12,
            "risk_per_trade_bps": 15,
        },
        "execution_reality": {
            "cost_model_id": "default_cost_v1",
            "fill_model_id": "paper_fill_v1",
        },
        "acceptance": {
            "min_trades": 20,
            "max_drawdown_pct": 3.0,
        },
    }


def _good_result() -> dict[str, object]:
    robustness = normalize_robustness_result(
        _spec(),
        {
            "robustness": {
                "wf_pass": True,
                "embargo_pass": True,
                "leakage_score": 0.01,
                "regime_stability": 0.82,
                "symbol_transfer_score": 0.71,
            }
        },
    ).to_dict()
    return {
        "experiment_id": "exp-good-001",
        "summary_metrics": {
            "trades": 40,
            "win_rate": 0.62,
            "net_pnl": 1800.0,
            "sharpe": 1.9,
            "max_drawdown_pct": 2.5,
        },
        "robustness": robustness,
        "execution_reality": {
            "modeled_slippage_bps": 5.0,
            "spread_cost_bps": 3.0,
            "partial_fill_rate": 0.1,
            "latency_bucket": "sub_2s",
        },
        "portfolio_metrics": {
            "pairwise_correlation": 0.18,
            "drawdown_overlap_score": 0.25,
            "capacity_limited": False,
            "capacity_usd": 10000.0,
        },
    }


def _bad_result() -> dict[str, object]:
    return {
        "experiment_id": "exp-bad-001",
        "summary_metrics": {
            "trades": 8,
            "win_rate": 0.4,
            "net_pnl": -400.0,
            "sharpe": -0.2,
            "max_drawdown_pct": 5.0,
        },
        "robustness": {
            "ok": False,
            "wf_pass": False,
            "embargo_pass": False,
            "leakage_score": 0.40,
            "regime_stability": 0.20,
            "symbol_transfer_score": 0.10,
            "passed_checks": [],
            "failed_checks": ["walk_forward", "leakage", "regime_split", "cross_symbol"],
        },
        "execution_reality": {
            "modeled_slippage_bps": 15.0,
            "spread_cost_bps": 8.0,
            "partial_fill_rate": 0.4,
            "latency_bucket": "slow",
        },
        "portfolio_metrics": {
            "pairwise_correlation": 0.42,
            "drawdown_overlap_score": 0.55,
            "capacity_limited": True,
        },
    }


def _stocks_spec(tmp_path: Path) -> dict[str, object]:
    artifact_root = tmp_path / "stocks" / "exp-stocks-qqq-001"
    return {
        "experiment_id": "exp-stocks-qqq-001",
        "cycle_id": "cycle-001",
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
        "execution_reality": {
            "cost_model_id": "default_cost_v1",
            "fill_model_id": "paper_fill_v1",
            "latency_model_id": "baseline_latency_v1",
            "modeled_slippage_bps": 7.5,
            "spread_cost_bps": 2.0,
            "fill_ratio": 0.95,
            "partial_fill_rate": 0.05,
            "latency_bucket": "sub_2s",
        },
        "acceptance": {
            "min_trades": 2,
            "max_drawdown_pct": 5.0,
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
        "robustness": {
            "required_checks": ["walk_forward", "leakage", "regime_split", "cross_symbol"],
            "leakage_score_max": 0.05,
            "regime_stability_min": 0.7,
            "symbol_transfer_min": 0.6,
        },
        "artifact_root": str(artifact_root),
    }


def _stock_runner_factory():
    def _runner(normalized_spec: dict[str, object]) -> dict[str, object]:
        artifact_root = Path(str(normalized_spec["artifact_root"]))
        artifact_root.mkdir(parents=True, exist_ok=True)
        (artifact_root / "trades.jsonl").write_text(
            "\n".join(
                [
                    json.dumps({"ts": "2026-03-24T09:31:00", "realized_pnl": 400.0, "qty": 100}),
                    json.dumps({"ts": "2026-03-24T10:05:00", "realized_pnl": -120.0, "qty": 100}),
                    json.dumps({"ts": "2026-03-24T13:20:00", "realized_pnl": 860.0, "qty": 100}),
                ]
            ),
            encoding="utf-8",
        )
        (artifact_root / "equity_curve.csv").write_text(
            "ts,equity\n"
            "2026-03-24T09:30:00,100000.0\n"
            "2026-03-24T10:00:00,100600.0\n"
            "2026-03-24T13:30:00,101140.0\n",
            encoding="utf-8",
        )
        (artifact_root / "report.json").write_text(
            json.dumps(
                {
                    "experiment_id": normalized_spec["experiment_id"],
                    "adapter": "stocks_kernel_backtest",
                    "status": "completed",
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        (artifact_root / "raw_result.json").write_text(
            json.dumps({"run_mode": "fixture_backed"}, indent=2),
            encoding="utf-8",
        )
        return {
            "status": "completed",
            "metrics": {
                "final_nav": 101140.0,
                "cash": 101140.0,
                "realized_pnl": 1140.0,
                "trades": 3,
                "win_rate": 0.6666667,
                "profit_factor": 1.9,
                "total_fee": 4.5,
            },
            "ledger": [
                {"qty": 100, "realized_pnl": 400.0, "fee": 1.5},
                {"qty": 100, "realized_pnl": -120.0, "fee": 1.5},
                {"qty": 100, "realized_pnl": 860.0, "fee": 1.5},
            ],
            "snapshot": {"equity": 101140.0, "cash": 101140.0, "positions": {}},
            "bars_processed": 63,
            "equity_curve": [
                {"ts": 1, "equity": 100000.0},
                {"ts": 2, "equity": 100600.0},
                {"ts": 3, "equity": 101140.0},
            ],
            "trades": [
                {"realized_pnl": 400.0},
                {"realized_pnl": -120.0},
                {"realized_pnl": 860.0},
            ],
        }

    return _runner


def _option_quote(ts: datetime, *, spot: float, bid: float, ask: float, dte: int = 1) -> ContractQuote:
    return ContractQuote(
        ts=ts,
        expiry="2026-03-25",
        right="C",
        strike=round(spot),
        dte=dte,
        bid=bid,
        ask=ask,
        mid=(bid + ask) / 2.0,
        iv=0.24,
        delta=0.52,
        gamma=0.01,
        theta=-0.02,
        vega=0.12,
        open_interest=500,
    )


def _build_option_bars() -> pd.DataFrame:
    ts = pd.date_range("2026-03-24 09:30:00", periods=120, freq="1min")
    close = []
    for i in range(len(ts)):
        if i < 10:
            close.append(500.0 + 0.02 * i)
        elif i == 10:
            close.append(501.25)
        elif i == 11:
            close.append(501.52)
        elif i < 20:
            close.append(501.70 + 0.03 * (i - 11))
        else:
            close.append(501.0 + 0.01 * (i - 20))
    close = pd.Series(close, dtype=float)
    return pd.DataFrame(
        {
            "ts": ts,
            "symbol": ["QQQ"] * len(ts),
            "open": close.shift(1).fillna(close.iloc[0]),
            "high": close + 0.18,
            "low": close - 0.16,
            "close": close,
            "vwap": close - 0.05,
            "volume": [1500.0] * len(ts),
        }
    )


def _build_option_cache(bars: pd.DataFrame) -> dict[tuple[datetime, str], list[ContractQuote]]:
    cache: dict[tuple[datetime, str], list[ContractQuote]] = {}
    for i, row in bars.iterrows():
        ts = pd.Timestamp(row["ts"]).to_pydatetime().replace(second=0, microsecond=0)
        spot = float(row["close"])
        bid = 4.80 + 0.01 * min(i, 20)
        ask = bid + 0.40
        cache[(ts, "C")] = [_option_quote(ts, spot=spot, bid=bid, ask=ask)]
    return cache


def test_quality_gates_costs_and_scoring_are_consistent() -> None:
    spec = _spec()
    result = _good_result()

    gates = compute_quality_gates(result, spec)
    costs = normalize_costs(result, spec)
    complexity = compute_complexity_penalty(spec)
    scored = score_experiment(result, spec)

    assert gates.passed is True
    assert gates.metrics["trades"] == 40
    assert costs["effective_cost_bps"] > 0
    assert costs["estimated_cost_penalty"] > costs["effective_cost_bps"]
    assert complexity > 0
    assert scored["quality_gate"]["passed"] is True
    assert scored["score"] > 0


def test_evaluate_candidates_builds_decision_and_portfolio_summary() -> None:
    spec = _spec()
    leaderboard_input = [_good_result(), _bad_result()]

    output = evaluate_candidates(
        leaderboard_input,
        specs_by_experiment_id={spec["experiment_id"]: spec},
        cycle_id="cycle-001",
        top_k=1,
    )

    leaderboard = output["leaderboard"]
    decision = output["promotion_decision"]
    portfolio = output["portfolio_evaluation"]
    breakdown = output["quality_gate_breakdown"]
    blocked_summary = output["blocked_summary"]
    next_cycle = output["next_cycle_recommendations"]

    assert leaderboard[0]["experiment_id"] == "exp-good-001"
    assert leaderboard[0]["score"] >= leaderboard[1]["score"]
    assert decision["winners"][0]["experiment_id"] == "exp-good-001"
    assert decision["winners"][0]["tier"] == "candidate"
    assert isinstance(decision["blocked_by_data"], list)
    assert isinstance(decision["blocked_by_robustness"], list)
    assert isinstance(decision["not_evaluable"], list)
    assert portfolio["candidate_set"] == ["exp-good-001", "exp-bad-001"]
    assert 0.0 <= portfolio["diversification_score"] <= 1.0
    assert portfolio["capacity_fit"] == "constrained"

    portfolio_eval = build_portfolio_evaluation(leaderboard_input, leaderboard=leaderboard, cycle_id="cycle-001")
    assert portfolio_eval.pairwise_correlation == 0.3
    assert portfolio_eval.marginal_risk_contribution["exp-good-001"] < portfolio_eval.marginal_risk_contribution["exp-bad-001"]
    assert breakdown["total"] == 2
    assert blocked_summary["blocked_total"] >= 1
    assert next_cycle["actions"]

    promotion = build_promotion_decision(leaderboard, cycle_id="cycle-001", top_k=1)
    assert promotion.winners[0]["experiment_id"] == "exp-good-001"


def test_real_stock_and_option_results_generate_persisted_leaderboard_and_block_bad_robustness(tmp_path: Path) -> None:
    stock_adapter = StocksKernelBacktestAdapter(
        runner=_stock_runner_factory(),
        artifacts_root=tmp_path / "stock_artifacts",
    )
    stock_spec = _stocks_spec(tmp_path)
    stock_result = stock_adapter.summarize(stock_adapter.run(stock_spec))
    stock_result["robustness"] = {
        "ok": True,
        "wf_pass": True,
        "embargo_pass": True,
        "leakage_score": 0.01,
        "regime_stability": 0.82,
        "symbol_transfer_score": 0.71,
        "passed_checks": ["walk_forward", "leakage", "regime_split", "cross_symbol"],
        "failed_checks": [],
    }

    option_adapter = OptionReplayResearchAdapter(output_root=tmp_path / "options")
    option_bars = _build_option_bars()
    option_spec = {
        "experiment_id": "exp-options-spy-001",
        "cycle_id": "cycle-001",
        "strategy_ref": {"definition_id": "QQQ_INTRADAY_OPTION_PARITY_V1"},
        "universe": {"underlying": "QQQ"},
        "inputs": {
            "bars": option_bars,
            "option_cache": _build_option_cache(option_bars),
        },
        "acceptance": {
            "min_trades": 1,
            "max_drawdown_pct": 0.60,
        },
        "robustness": {
            "wf_pass": True,
        },
        "sizing": {
            "risk_per_trade_bps": 15,
        },
    }
    option_result = option_adapter.run(option_spec)

    bad_result = {
        "experiment_id": "exp-bad-robustness-001",
        "cycle_id": "cycle-001",
        "summary_metrics": {
            "trades": 24,
            "win_rate": 0.71,
            "net_pnl": 3200.0,
            "sharpe": 2.4,
            "max_drawdown_pct": 1.2,
        },
        "robustness": {
            "ok": False,
            "wf_pass": False,
            "embargo_pass": False,
            "leakage_score": 0.30,
            "regime_stability": 0.20,
            "symbol_transfer_score": 0.10,
            "passed_checks": [],
            "failed_checks": ["walk_forward", "leakage", "regime_split", "cross_symbol"],
        },
        "execution_reality": {
            "modeled_slippage_bps": 18.0,
            "spread_cost_bps": 9.0,
            "partial_fill_rate": 0.5,
            "latency_bucket": "slow",
        },
        "portfolio_metrics": {
            "pairwise_correlation": 0.56,
            "drawdown_overlap_score": 0.62,
            "capacity_limited": True,
        },
    }

    bundle = persist_evaluation_bundle(
        [stock_result, option_result, bad_result],
        output_dir=tmp_path / "evaluation",
        specs_by_experiment_id={
            stock_spec["experiment_id"]: stock_spec,
            option_spec["experiment_id"]: option_spec,
        },
        cycle_id="cycle-001",
        top_k=1,
    )

    leaderboard = bundle["leaderboard"]
    promotion = bundle["promotion_decision"]
    portfolio = bundle["portfolio_evaluation"]

    assert Path(bundle["artifacts"]["leaderboard_json"]).exists()
    assert Path(bundle["artifacts"]["promotion_decision_json"]).exists()
    assert Path(bundle["artifacts"]["portfolio_evaluation_json"]).exists()
    assert Path(bundle["artifacts"]["blocked_summary_json"]).exists()
    assert Path(bundle["artifacts"]["quality_gate_breakdown_json"]).exists()
    assert Path(bundle["artifacts"]["next_cycle_recommendations_json"]).exists()
    assert leaderboard[0]["experiment_id"] in {"exp-stocks-qqq-001", "exp-options-spy-001"}
    assert promotion["winners"]
    assert all(item["experiment_id"] != "exp-bad-robustness-001" for item in promotion["winners"])
    assert any(item["experiment_id"] == "exp-bad-robustness-001" for item in promotion["retest_queue"])
    assert portfolio["candidate_set"] == ["exp-stocks-qqq-001", "exp-options-spy-001", "exp-bad-robustness-001"]
    assert 0.0 <= portfolio["diversification_score"] <= 1.0

    with Path(bundle["artifacts"]["leaderboard_json"]).open("r", encoding="utf-8") as fh:
        leaderboard_json = json.load(fh)
    assert leaderboard_json[0]["experiment_id"] == leaderboard[0]["experiment_id"]
