import json
from pathlib import Path

from strategies.stocks_kernel.policies.agent_paper_bridge import build_agent_paper_bridge_policy
from strategies.stocks_kernel.types import FeatureContext, GateEvaluationReport, HistoryBundle, MarketSnapshot


def _ctx(ts_epoch_s: float = 1_778_629_000.0) -> FeatureContext:
    return FeatureContext(
        symbol="NVDA",
        snapshot=MarketSnapshot(symbol="NVDA", ts_epoch_s=ts_epoch_s, last_price=220.0, session="POST"),
        history=HistoryBundle(symbol="NVDA"),
        values={},
    )


def _gate(allowed: bool = True) -> GateEvaluationReport:
    return GateEvaluationReport(allowed=allowed, decisions=())


def test_agent_paper_bridge_builds_entry_once_from_signal_file(tmp_path: Path) -> None:
    signal_path = tmp_path / "agent_paper_signal.jsonl"
    signal_path.write_text(
        json.dumps(
            {
                "signal_id": "sig-1",
                "symbol": "NVDA",
                "action": "enter",
                "side": "LONG",
                "qty": 1,
                "ts_epoch_s": 1_778_629_000.0,
                "max_age_sec": 120,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    policy = build_agent_paper_bridge_policy(
        policy_params={
            "signal_path": str(signal_path),
            "state_path": str(tmp_path / "agent_paper_state.json"),
            "symbol": "NVDA",
            "max_qty": 1,
        },
        extras={},
    )
    signal = policy.build_signal(symbol="NVDA", feature_context=_ctx(), gate_report=_gate())
    plans = policy.build_action_plans(signal=signal, feature_context=_ctx())

    assert signal.action == "enter"
    assert signal.side == "LONG"
    assert len(plans) == 1
    assert plans[0].action_type == "ENTRY"
    assert plans[0].symbol == "NVDA"
    assert plans[0].qty == 1
    assert plans[0].payload["absolute_signal_qty"] is True
    assert plans[0].payload["skip_trade_budget_sizing"] is True
    assert policy.build_entry_plan(signal=signal, feature_context=_ctx()) is None
    assert policy.build_action_plans(signal=signal, feature_context=_ctx()) == []


def test_agent_paper_bridge_ignores_stale_signal(tmp_path: Path) -> None:
    signal_path = tmp_path / "agent_paper_signal.jsonl"
    signal_path.write_text(
        json.dumps(
            {
                "signal_id": "sig-stale",
                "symbol": "NVDA",
                "action": "enter",
                "side": "LONG",
                "qty": 1,
                "ts_epoch_s": 1_000.0,
                "max_age_sec": 1,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    policy = build_agent_paper_bridge_policy(
        policy_params={
            "signal_path": str(signal_path),
            "state_path": str(tmp_path / "agent_paper_state.json"),
            "symbol": "NVDA",
        },
        extras={},
    )
    signal = policy.build_signal(symbol="NVDA", feature_context=_ctx(), gate_report=_gate())

    assert signal.action == "hold"
    assert signal.payload["reason"] == "agent_signal_missing_or_stale"


def test_agent_paper_bridge_allows_larger_flatten_qty_without_raising_entry_cap(tmp_path: Path) -> None:
    signal_path = tmp_path / "agent_paper_signal.jsonl"
    signal_path.write_text(
        json.dumps(
            {
                "signal_id": "sig-flatten",
                "symbol": "NVDA",
                "action": "flatten",
                "qty": 2,
                "ts_epoch_s": 1_778_629_000.0,
                "max_age_sec": 120,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    policy = build_agent_paper_bridge_policy(
        policy_params={
            "signal_path": str(signal_path),
            "state_path": str(tmp_path / "agent_paper_state.json"),
            "symbol": "NVDA",
            "max_qty": 1,
            "max_flatten_qty": 100,
        },
        extras={},
    )
    signal = policy.build_signal(symbol="NVDA", feature_context=_ctx(), gate_report=_gate())
    plans = policy.build_action_plans(signal=signal, feature_context=_ctx())

    assert signal.action == "flatten"
    assert len(plans) == 1
    assert plans[0].action_type == "FLATTEN"
    assert plans[0].qty == 2
    assert plans[0].reduce_only is True


def test_agent_paper_bridge_persists_emitted_signal_ids(tmp_path: Path) -> None:
    signal_path = tmp_path / "agent_paper_signal.jsonl"
    state_path = tmp_path / "agent_paper_state.json"
    signal_path.write_text(
        json.dumps(
            {
                "signal_id": "sig-persisted",
                "symbol": "NVDA",
                "action": "enter",
                "side": "LONG",
                "qty": 1,
                "ts_epoch_s": 1_778_629_000.0,
                "max_age_sec": 120,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    params = {"signal_path": str(signal_path), "state_path": str(state_path), "symbol": "NVDA", "max_qty": 1}
    policy = build_agent_paper_bridge_policy(policy_params=params, extras={})
    signal = policy.build_signal(symbol="NVDA", feature_context=_ctx(), gate_report=_gate())

    assert len(policy.build_action_plans(signal=signal, feature_context=_ctx())) == 1

    reloaded = build_agent_paper_bridge_policy(policy_params=params, extras={})
    reloaded_signal = reloaded.build_signal(symbol="NVDA", feature_context=_ctx(), gate_report=_gate())
    assert reloaded.build_action_plans(signal=reloaded_signal, feature_context=_ctx()) == []
    assert reloaded.build_entry_plan(signal=reloaded_signal, feature_context=_ctx()) is None
