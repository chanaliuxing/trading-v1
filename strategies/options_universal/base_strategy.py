from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from strategies.qqq_0dte_opt_live_strategy import QQQ0DTEOptionsLiveStrategy

from .plugin import DefaultPassThroughPlugin, OptionStrategyPlugin
from .types import OptionBarContext, OptionQuoteRequest


class UniversalOptionLiveStrategy(QQQ0DTEOptionsLiveStrategy):
    """
    Universal option strategy kernel.

    Shared responsibilities:
    - market data subscription / heartbeat / runtime wiring
    - intent submission / order refs / risk extras
    - base-level option position adoption and sync safeguards

    Pluggable responsibilities:
    - entry signal logic
    - contract selection override
    - position management / exits
    """

    def build_plugin(self) -> OptionStrategyPlugin:
        return DefaultPassThroughPlugin()

    async def initialize(self) -> None:
        extras = self.params.extras or {}
        self._plugin = self.build_plugin()
        self._plugin.apply_default_extras(extras)
        self.params.extras = extras
        await self.subscribe_system_health()
        await super().initialize()
        await self._plugin.on_post_initialize(self)

    def _emit_local_audit(
        self,
        *,
        stage: str,
        severity: str = "info",
        status: Optional[str] = None,
        reason: Optional[str] = None,
        symbol: Optional[str] = None,
        decision_id: Optional[str] = None,
        trigger_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        writer = self._resolve_local_audit_writer()
        if writer is None:
            return
        strategy_run_id = str(getattr(self.ctx, "strategy_run_id", "") or "").strip() or None
        try:
            writer.emit(
                domain="strategy",
                component=str(getattr(self._plugin, "component_name", "option_universal_strategy")),
                stage=stage,
                severity=severity,
                strategy_id=self.strategy_id,
                strategy_run_id=strategy_run_id,
                trigger_id=str(trigger_id or "").strip() or None,
                decision_id=str(decision_id or "").strip() or None,
                trace_id=str(trace_id or "").strip() or None,
                symbol=symbol or getattr(self, "underlying", None),
                status=status,
                reason=reason,
                payload=payload,
            )
        except Exception as exc:
            self.ctx.log.warning(
                "option_universal_local_audit_emit_failed",
                strategy_id=str(self.strategy_id),
                stage=stage,
                error=str(exc),
            )

    async def _maybe_enter(self, now: datetime, bar: Dict[str, Any], wall_now: Optional[datetime] = None) -> None:
        handled = await self._plugin.maybe_enter(
            self,
            OptionBarContext(now=now, bar=bar, wall_now=wall_now),
        )
        if handled:
            return
        await super()._maybe_enter(now, bar, wall_now=wall_now)

    async def _sync_and_manage_position(self, now: datetime, bar: Dict[str, Any]) -> bool:
        handled = await self._plugin.sync_and_manage_position(
            self,
            OptionBarContext(now=now, bar=bar),
        )
        if handled:
            return True
        return await super()._sync_and_manage_position(now, bar)

    def _select_0dte_quote(
        self,
        now: datetime,
        *,
        side: str,
        spot: float,
        spread_max_pct: float,
        now_for_quote_age: Optional[datetime] = None,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        selected = self._plugin.select_quote(
            self,
            OptionQuoteRequest(
                now=now,
                side=side,
                spot=spot,
                spread_max_pct=spread_max_pct,
                now_for_quote_age=now_for_quote_age,
            ),
        )
        if selected is not None:
            return selected.quote, selected.reject_reason
        return super()._select_0dte_quote(
            now,
            side=side,
            spot=spot,
            spread_max_pct=spread_max_pct,
            now_for_quote_age=now_for_quote_age,
        )
