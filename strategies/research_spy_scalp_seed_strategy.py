from __future__ import annotations

from types import SimpleNamespace
from typing import Any


class ResearchSpyScalpSeedStrategy:
    """Minimal deterministic SPY scalp seed strategy for research backtests.

    Uses short-horizon price impulse on minute bars:
    - Buy 1 share when close > open by threshold and currently flat.
    - Exit position when close < open by threshold.
    """

    def __init__(self, *args: Any, **kwargs: Any):
        # New runtime style: (ctx, StrategyParams)
        if len(args) >= 2 and hasattr(args[0], "submit_intent"):
            self.ctx = args[0]
            self.strategy_id = str(getattr(self.ctx, "strategy_id", "research_spy_scalp_seed"))
            self.engine = self.ctx
            raw_params = args[1]
            extras = dict(getattr(raw_params, "extras", {}) or {})
        else:
            # Legacy style: (strategy_id, engine, params)
            strategy_id = args[0] if args else kwargs.get("strategy_id")
            engine = args[1] if len(args) > 1 else kwargs.get("engine")
            params = args[2] if len(args) > 2 else kwargs.get("params")
            self.ctx = None
            self.strategy_id = str(strategy_id or "research_spy_scalp_seed")
            self.engine = engine
            extras = dict((params or {}).get("extras") or {})
        self.impulse_bps = float(extras.get("impulse_bps", 5.0))
        self.position_qty = 0

    async def on_features(self, symbol: str, feat: dict[str, Any]) -> None:
        open_px = float(feat.get("open") or 0.0)
        close_px = float(feat.get("close") or open_px or 0.0)
        if open_px <= 0 or close_px <= 0:
            return

        impulse_bps = ((close_px - open_px) / open_px) * 10000.0
        ts = feat.get("ts")
        ts_us = 0
        if ts is not None:
            try:
                ts_us = int(getattr(ts, "timestamp")() * 1_000_000)
            except Exception:
                ts_us = int(float(ts)) * 1000

        if self.position_qty == 0 and impulse_bps >= self.impulse_bps:
            self.position_qty = 1
            decision = SimpleNamespace(
                symbol=symbol,
                side="BUY",
                target_qty=1,
                order_style=None,
                ts_bar_end_us=ts_us,
            )
            if self.ctx is not None:
                await self.ctx.submit_intent(decision)
            else:
                await self.engine.submit_intent(self.strategy_id, decision)
            return

        if self.position_qty > 0 and impulse_bps <= -self.impulse_bps:
            self.position_qty = 0
            decision = SimpleNamespace(
                symbol=symbol,
                side="SELL",
                target_qty=1,
                order_style=None,
                ts_bar_end_us=ts_us,
            )
            if self.ctx is not None:
                await self.ctx.submit_intent(decision)
            else:
                await self.engine.submit_intent(self.strategy_id, decision)
