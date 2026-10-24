from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List

from strategies.options_kernel.types import KernelBarContext, PluginRuntimeState
from strategies.qqq_intraday_gate_audit_logic import trader_reason_code

from .engine import QQQIntradayRuleEngine
from .recipe import load_option_entry_recipe, load_stock_entry_recipe


@dataclass(frozen=True)
class ReplaySample:
    name: str
    now: datetime
    spot: float
    features: Dict[str, Any] = field(default_factory=dict)
    extras: Dict[str, Any] = field(default_factory=dict)
    expected_allowed: bool = False
    expected_stock_side: str = "NONE"
    expected_reason: str = ""


@dataclass(frozen=True)
class ReplayResult:
    name: str
    allowed: bool
    stock_side: str
    option_side: str | None
    reason: str
    trader_reason_code: str
    expected_allowed: bool
    expected_stock_side: str
    expected_reason: str


def _runtime(sample: ReplaySample) -> PluginRuntimeState:
    features = dict(sample.features or {})
    return PluginRuntimeState(
        strategy_id="qqq_intraday_option_v1_001",
        kernel_mode="pure_plugin",
        symbol="QQQ",
        now=sample.now,
        wall_now=sample.now,
        spot=float(sample.spot),
        bar=KernelBarContext(
            ts=sample.now,
            symbol="QQQ",
            open=float(sample.spot),
            high=float(sample.spot) + 0.5,
            low=float(sample.spot) - 0.5,
            close=float(sample.spot),
            volume=float(features.get("volume", 1200.0)),
            vwap=float(features.get("vwap", sample.spot)),
            atr=float(features.get("atr_eff", 1.0)),
        ),
        features=features,
        payload={},
    )


def run_replay_samples(samples: Iterable[ReplaySample]) -> List[ReplayResult]:
    engine = QQQIntradayRuleEngine()
    results: list[ReplayResult] = []
    for sample in samples:
        runtime = _runtime(sample)
        extras = dict(sample.extras or {})
        stock_eval = engine.evaluate(runtime=runtime, extras=extras, recipe=load_stock_entry_recipe(extras))
        option_eval = engine.evaluate(runtime=runtime, extras=extras, recipe=load_option_entry_recipe(extras))
        reason = str(option_eval.translation.reason)
        results.append(
            ReplayResult(
                name=sample.name,
                allowed=bool(option_eval.translation.allowed),
                stock_side=str(option_eval.translation.stock_side),
                option_side=option_eval.translation.side,
                reason=reason,
                trader_reason_code=trader_reason_code(reason),
                expected_allowed=bool(sample.expected_allowed),
                expected_stock_side=str(sample.expected_stock_side),
                expected_reason=str(sample.expected_reason),
            )
        )
        # shared upstream parity: only translator differs between stock/option recipes
        if bool(stock_eval.translation.allowed) != bool(option_eval.translation.allowed):
            raise AssertionError(f"shared upstream parity mismatch for sample={sample.name}: stock_allowed={stock_eval.translation.allowed} option_allowed={option_eval.translation.allowed}")
        if str(stock_eval.translation.stock_side) != str(option_eval.translation.stock_side):
            raise AssertionError(f"shared upstream side mismatch for sample={sample.name}: stock_side={stock_eval.translation.stock_side} option_side={option_eval.translation.stock_side}")
    return results
