#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategies.stocks_kernel.runtime.matrix import MatrixScenario, run_regression_matrix
from strategies.stocks_kernel.runtime.replay import ReplayFrame
from strategies.stocks_kernel.strategy import ComposableStocksKernelStrategy
from strategies.stocks_kernel.types import HistoryBar


class _Logger:
    def info(self, *args, **kwargs):
        return None

    def warning(self, *args, **kwargs):
        return None

    def error(self, *args, **kwargs):
        return None


class _Engine:
    async def subscribe_features(self, strategy_id, symbols, bar_sec):
        return None

    async def subscribe_tick_data(self, strategy_id, symbols):
        return {symbol: True for symbol in symbols}


class _SystemHealth:
    async def subscribe(self, strategy_id: str, strategy) -> None:
        _ = strategy_id, strategy
        return None


async def _build_strategy(*, config: dict, strategy_id: str):
    engine = _Engine()
    ctx = SimpleNamespace(
        log=_Logger(),
        metrics=SimpleNamespace(),
        engine=engine,
        system_health=_SystemHealth(),
        strategy_id=strategy_id,
        strategy_run_id=f"{strategy_id}-matrix",
        subscribe_features=lambda symbols, bar_sec=5: engine.subscribe_features(strategy_id, symbols, bar_sec),
        subscribe_tick_data=lambda symbols: engine.subscribe_tick_data(strategy_id, symbols),
    )
    params = SimpleNamespace(
        strategy_id=strategy_id,
        ttl_ms=int(config.get("ttl_ms") or 2000),
        extras=dict(config.get("extras") or {}),
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    return strategy


async def _run(*, config: dict, scenarios_payload: list[dict]) -> dict:
    def _normalize_features(payload: dict) -> dict:
        features = dict(payload)
        if "bars_1m" in features:
            features["bars_1m"] = [
                HistoryBar(
                    ts_epoch_s=float(item["ts_epoch_s"]),
                    open=float(item["open"]),
                    high=float(item["high"]),
                    low=float(item["low"]),
                    close=float(item["close"]),
                    volume=float(item["volume"]),
                )
                if isinstance(item, dict)
                else item
                for item in list(features.get("bars_1m") or [])
            ]
        return features

    def _default_matrix_scenarios() -> list[dict]:
        bars = [
            {"ts_epoch_s": float(i), "open": 100 + i, "high": 101 + i, "low": 99 + i, "close": 100 + i, "volume": 10 * i}
            for i in range(1, 16)
        ]
        return [
            {
                "name": "entry_action_set_ready",
                "required_symbols": ["QQQ"],
                "required_stages": ["signal_evaluated", "entry_plan_built"],
                "frames": [
                    {
                        "symbol": "QQQ",
                        "features": {
                            "ts_epoch_s": 100.0,
                            "now_epoch_s": 101.0,
                            "last_price": 500.0,
                            "session": "RTH",
                            "vwap": 499.0,
                            "planned_qty": 2,
                            "trigger_id": "matrix-entry-1",
                            "bars_1m": bars,
                        },
                    }
                ],
            },
            {
                "name": "session_flatten_path",
                "required_symbols": ["QQQ"],
                "required_stages": ["signal_evaluated"],
                "frames": [
                    {
                        "symbol": "QQQ",
                        "features": {
                            "ts_epoch_s": 200.0,
                            "now_epoch_s": 201.0,
                            "last_price": 480.0,
                            "session": "RTH",
                            "vwap": 479.0,
                            "planned_qty": 1,
                            "trigger_id": "matrix-flat-1",
                            "bars_1m": bars,
                        },
                    }
                ],
            },
            {
                "name": "same_symbol_lane_scope",
                "required_symbols": ["QQQ"],
                "required_stages": ["signal_evaluated"],
                "frames": [
                    {
                        "symbol": "QQQ",
                        "features": {
                            "ts_epoch_s": 300.0,
                            "now_epoch_s": 301.0,
                            "last_price": 470.0,
                            "session": "RTH",
                            "vwap": 469.0,
                            "planned_qty": 1,
                            "trigger_id": "matrix-lane-1",
                            "bars_1m": bars,
                        },
                    }
                ],
            },
        ]

    scenarios: list[MatrixScenario] = []
    base_strategy_id = str(config.get("strategy_id") or "stocks_kernel_matrix")
    payload_rows = list(scenarios_payload or [])
    if not payload_rows:
        payload_rows = _default_matrix_scenarios()
    for idx, payload in enumerate(payload_rows):
        scenario_name = str(payload.get("name") or f"scenario_{idx + 1}")
        scenario_strategy_id = str(payload.get("strategy_id") or f"{base_strategy_id}_{idx + 1}")
        frames = tuple(
            ReplayFrame(symbol=str(item["symbol"]), features=_normalize_features(dict(item["features"])))
            for item in list(payload.get("frames") or [])
        )
        required_symbols = tuple(str(symbol) for symbol in list(payload.get("required_symbols") or []))
        required_stages = tuple(str(stage) for stage in list(payload.get("required_stages") or []))
        scenarios.append(
            MatrixScenario(
                name=scenario_name,
                strategy_factory=lambda sid=scenario_strategy_id: _build_strategy(config=config, strategy_id=sid),
                frames=frames,
                required_symbols=required_symbols,
                required_stages=required_stages,
            )
        )
    report = await run_regression_matrix(scenarios=tuple(scenarios))
    payload = asdict(report)
    scenario_count = int(payload.get("scenario_count") or 0)
    passed_count = int(payload.get("passed_count") or 0)
    fallback_used = not bool(scenarios_payload)
    min_required = int(config.get("matrix_min_scenarios") or (3 if fallback_used else 0))
    payload["pass_rate"] = (float(passed_count) / float(scenario_count)) if scenario_count > 0 else 0.0
    payload["coverage"] = {
        "scenario_names": [str(item.get("name") or "") for item in list(payload.get("results") or [])],
        "minimum_required_scenarios": min_required,
        "coverage_ok": scenario_count >= min_required,
    }
    if min_required > 0 and not payload["coverage"]["coverage_ok"]:
        payload["passed"] = False
        payload["reason"] = "matrix_insufficient_coverage"
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Run stocks kernel regression matrix")
    parser.add_argument("--config", required=True)
    parser.add_argument("--scenarios", required=True)
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    config = json.loads(Path(args.config).expanduser().read_text(encoding="utf-8"))
    scenarios = json.loads(Path(args.scenarios).expanduser().read_text(encoding="utf-8"))
    payload = asyncio.run(_run(config=config, scenarios_payload=list(scenarios)))
    if args.output:
        out = Path(args.output).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if bool(payload.get("passed")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
