"""
Dry-run strategy loading contracts for startup smoke checks.

These classes deliberately avoid external services.  They let startup smoke use
the same StrategyEngine.load_strategy path as production while preventing IB,
ClickHouse, PostgreSQL, Kafka, Redis, and the real decision pipeline from being
touched during validation.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from libs.strategy.base import DecisionEnvelope, OrderUpdate, PositionSnapshot, StrategyContext


class NoopMarketDataFacade:
    async def bars_async(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    def bars(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    async def latest_bar_async(self, *args: Any, **kwargs: Any) -> Optional[dict[str, Any]]:
        return None

    def latest_bar(self, *args: Any, **kwargs: Any) -> Optional[dict[str, Any]]:
        return None

    async def health_check(self) -> dict[str, Any]:
        return {"ok": True, "dry_run": True}


@dataclass
class NoopDecisionResult:
    status: str = "DRY_RUN"
    reasons: list[str] = None  # type: ignore[assignment]
    plan: Any = None

    def __post_init__(self) -> None:
        if self.reasons is None:
            self.reasons = ["dry_run_no_submission"]


class NoopDecisionPipeline:
    shadow_mode = True

    async def process_decision(self, envelope: DecisionEnvelope) -> NoopDecisionResult:
        return NoopDecisionResult()


class NoopEventBus:
    async def publish(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def subscribe(self, *args: Any, **kwargs: Any) -> None:
        return None


class NoopHealthController:
    def __init__(self) -> None:
        self.state = "running"
        self.contexts: dict[str, StrategyContext] = {}
        self.strategies: dict[str, Any] = {}
        self.events: list[dict[str, Any]] = []

    def attach_context(self, ctx: StrategyContext) -> None:
        ctx.system_health = self
        self.contexts[str(ctx.strategy_id)] = ctx

    async def register_strategy(self, strategy_id: str, strategy: Any) -> None:
        self.strategies[str(strategy_id)] = strategy

    async def assert_strategy_admitted(self, strategy_id: str) -> None:
        if str(strategy_id) not in self.strategies:
            raise RuntimeError(f"dry_run_strategy_not_registered:{strategy_id}")

    async def subscribe(self, strategy_id: str, strategy: Any) -> None:
        self.strategies.setdefault(str(strategy_id), strategy)

    async def publish(self, event: dict[str, Any]) -> None:
        self.events.append(dict(event or {}))


class NoopSubmitIntent:
    async def __call__(self, envelope: DecisionEnvelope) -> NoopDecisionResult:
        return NoopDecisionResult()


class DryRunEngine:
    def __init__(self) -> None:
        self._cadences: dict[str, int] = {}
        self.subscriptions: list[dict[str, Any]] = []
        self.submitted: list[DecisionEnvelope] = []
        self.emitted_features: list[dict[str, Any]] = []
        self.decision_pipeline = NoopDecisionPipeline()
        self.market_data_service = None
        self.order_truth_service = None
        self.broker_adapter = None

    async def emit_features(self, strategy_id: str, symbol: str, features: Dict[str, Any]) -> None:
        self.emitted_features.append(
            {"strategy_id": str(strategy_id), "symbol": str(symbol), "features": dict(features or {})}
        )

    async def subscribe_features(self, strategy_id: str, symbols: List[str], bar_sec: int) -> None:
        self.subscriptions.append(
            {
                "kind": "features",
                "strategy_id": str(strategy_id),
                "symbols": [str(s).upper() for s in symbols or []],
                "bar_sec": int(bar_sec),
            }
        )

    async def history_features(
        self,
        symbol: str,
        resolution: str,
        limit: int,
        fields: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        return []

    async def portfolio_view(self, strategy_id: str) -> Dict[str, Any]:
        return {"cash": 0.0, "equity": 0.0, "positions": [], "dry_run": True}

    async def position_view(self, strategy_id: str, symbol: str) -> PositionSnapshot:
        return PositionSnapshot(
            strategy_id=str(strategy_id),
            symbol=str(symbol).upper(),
            pos_qty=0.0,
            avg_px=None,
            ts=datetime.utcnow(),
        )

    async def open_orders_view(self, strategy_id: str) -> List[OrderUpdate]:
        return []

    async def submit_intent(self, strategy_id: str, envelope: DecisionEnvelope) -> NoopDecisionResult:
        self.submitted.append(envelope)
        return NoopDecisionResult()

    async def set_cadence(self, strategy_id: str, seconds: int) -> None:
        self._cadences[str(strategy_id)] = max(1, int(seconds))

    async def subscribe_tick_data(self, strategy_id: str, symbols: List[str]) -> Dict[str, bool]:
        self.subscriptions.append(
            {
                "kind": "ticks",
                "strategy_id": str(strategy_id),
                "symbols": [str(s).upper() for s in symbols or []],
            }
        )
        return {str(symbol).upper(): True for symbol in symbols or []}


class DryRunStrategyContext(StrategyContext):
    async def enable_realtime_feature_engine(
        self,
        symbols: List[str],
        spec: Optional[Dict[str, Any]] = None,
        emit_interval_sec: int = 1,
        warmup_bars: int = 60,
    ) -> None:
        self._rfe_enabled = True
        self._rfe_warm = {str(symbol).upper(): True for symbol in symbols or []}

    async def disable_realtime_feature_engine(self) -> None:
        self._rfe_enabled = False
        self._rfe_poll_tasks.clear()

    def get_realtime_features(self, symbol: str) -> tuple[dict[str, Any], int, bool]:
        return {}, 0, True
