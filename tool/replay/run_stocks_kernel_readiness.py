#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategies.stocks_kernel.runtime.readiness import generate_production_readiness_report
from strategies.stocks_kernel.strategy import ComposableStocksKernelStrategy
from strategies.stocks_kernel.types import PositionLot


class _Logger:
    def info(self, *args, **kwargs):
        return None

    def warning(self, *args, **kwargs):
        return None

    def error(self, *args, **kwargs):
        return None


class _Engine:
    def __init__(self, *, broker_positions: list[dict[str, Any]] | None = None, broker_open_orders: list[dict[str, Any]] | None = None) -> None:
        self.broker_adapter = _BrokerSnapshotAdapter(
            positions=list(broker_positions or []),
            open_orders=list(broker_open_orders or []),
        )

    async def subscribe_features(self, strategy_id, symbols, bar_sec):
        return None

    async def subscribe_tick_data(self, strategy_id, symbols):
        return {symbol: True for symbol in symbols}

    async def history_features(self, symbol, resolution, limit, fields=None):
        return []


class _SystemHealth:
    async def subscribe(self, strategy_id: str, strategy: Any) -> None:
        _ = strategy_id, strategy
        return None


def _position_lot(payload: dict[str, Any]) -> PositionLot:
    return PositionLot(
        position_lot_id=str(payload["position_lot_id"]),
        strategy_id=str(payload["strategy_id"]),
        symbol=str(payload["symbol"]),
        side=str(payload["side"]),
        opened_by_decision_id=str(payload["opened_by_decision_id"]),
        opened_by_trigger_id=payload.get("opened_by_trigger_id"),
        entry_order_ref=str(payload["entry_order_ref"]),
        live_qty=float(payload.get("live_qty") or 0.0),
        filled_qty=float(payload.get("filled_qty") or 0.0),
        avg_entry_price=float(payload["avg_entry_price"]) if payload.get("avg_entry_price") is not None else None,
        state=str(payload.get("state") or "live"),
        lane_id=payload.get("lane_id"),
        ownership_scope=str(payload.get("ownership_scope") or "strategy"),
        flatten_authority=str(payload.get("flatten_authority") or "strategy"),
        protection_authority=str(payload.get("protection_authority") or "strategy"),
        adopt_authority=str(payload.get("adopt_authority") or "strategy"),
        extras=dict(payload.get("extras") or {}),
    )


class _BrokerSnapshotAdapter:
    def __init__(self, *, positions: list[dict[str, Any]], open_orders: list[dict[str, Any]]) -> None:
        self._positions = list(positions or [])
        self._open_orders = list(open_orders or [])

    async def position_qty(self, symbol: str) -> float:
        symbol_u = str(symbol or "").strip().upper()
        total = 0.0
        for row in self._positions:
            if str(row.get("symbol", "") or "").strip().upper() != symbol_u:
                continue
            qty = row.get("pos_qty", row.get("qty", 0.0))
            try:
                total += float(qty or 0.0)
            except Exception:
                continue
        return float(total)

    async def open_orders(self, *, force_refresh: bool = False):
        _ = force_refresh
        rows: list[Any] = []
        for row in self._open_orders:
            payload = dict(row or {})
            rows.append(
                SimpleNamespace(
                    symbol=str(payload.get("symbol", "") or "").strip().upper(),
                    order_ref=str(payload.get("order_ref", "") or ""),
                    status=str(payload.get("status", payload.get("state", "")) or ""),
                    strategy_id=str(payload.get("strategy_id", "") or ""),
                    broker_order_id=payload.get("broker_order_id"),
                    order_id=payload.get("order_id"),
                )
            )
        return rows


async def _run(*, config: dict[str, Any], runtime_state: dict[str, Any]) -> dict[str, Any]:
    strategy_id = str(config.get("strategy_id") or "stocks_kernel_readiness")
    engine = _Engine(
        broker_positions=list(runtime_state.get("broker_positions") or []),
        broker_open_orders=list(runtime_state.get("broker_open_orders") or []),
    )
    ctx = SimpleNamespace(
        log=_Logger(),
        metrics=SimpleNamespace(),
        engine=engine,
        system_health=_SystemHealth(),
        strategy_id=strategy_id,
        strategy_run_id=f"{strategy_id}-readiness",
        subscribe_features=lambda symbols, bar_sec=5: engine.subscribe_features(strategy_id, symbols, bar_sec),
        subscribe_tick_data=lambda symbols: engine.subscribe_tick_data(strategy_id, symbols),
        history=lambda symbol, resolution, limit: engine.history_features(symbol, resolution, limit),
    )
    params = SimpleNamespace(
        strategy_id=strategy_id,
        ttl_ms=int(config.get("ttl_ms") or 2000),
        extras=dict(config.get("extras") or {}),
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()

    for symbol, reason in dict(runtime_state.get("kill_switches") or {}).items():
        strategy._symbol_kill_switch[str(symbol).strip().upper()] = str(reason)
    for symbol, reason in dict(runtime_state.get("entry_freezes") or {}).items():
        if strategy.order_lifecycle_module is not None:
            strategy.order_lifecycle_module.mark_sync_issue(symbol=str(symbol).strip().upper(), reason=str(reason))
    for payload in list(runtime_state.get("lots") or []):
        strategy.execution.ledger.open_lot(_position_lot(dict(payload)))

    report = await generate_production_readiness_report(strategy=strategy)
    return asdict(report)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run stocks kernel production readiness report")
    parser.add_argument("--config", required=True)
    parser.add_argument("--runtime-state", default="")
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    config = json.loads(Path(args.config).expanduser().read_text(encoding="utf-8"))
    runtime_state = {}
    if args.runtime_state:
        runtime_state = json.loads(Path(args.runtime_state).expanduser().read_text(encoding="utf-8"))
    payload = asyncio.run(_run(config=config, runtime_state=runtime_state))
    if args.output:
        out = Path(args.output).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if bool(payload.get("ready")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
