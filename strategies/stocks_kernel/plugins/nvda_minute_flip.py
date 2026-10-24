from __future__ import annotations

from typing import Any, Dict, Optional

from libs.strategy.base import OrderStyle
from strategies.stocks_kernel.plugin import BaseStockPlugin, StockStrategyManifest


class NVDAMinuteFlipStockPlugin(BaseStockPlugin):
    """Execution-surface plugin for the NVDA minute-flip test strategy."""

    component_name = "nvda_minute_flip_stock_plugin"

    def __init__(self, *, limit_offset_bp: float = 20.0, order_type: str = "LMT") -> None:
        self.limit_offset_bp = abs(float(limit_offset_bp or 0.0))
        self.order_type = str(order_type or "LMT").strip().upper() or "LMT"
        self._last_price_by_symbol: dict[str, float] = {}

    def production_manifest(self) -> StockStrategyManifest:
        return StockStrategyManifest(
            strategy_key="nvda_minute_flip",
            symbols=("NVDA",),
            template_id="offhours_equity",
            order_namespace="nvda_flip",
            data_profile="equity_offhours_tick_only",
            policy_id="nvda_minute_flip_policy",
            recovery_policy_id="stocks_kernel_close_only",
            data_requirements=(
                {"dataset": "ticks", "symbols": ["NVDA"], "ttl_sec": 30, "rth": False},
                {"dataset": "bars_5s", "symbols": ["NVDA"], "ttl_sec": 90, "rth": False},
            ),
            policy_registry_specs=(
                {
                    "policy_id": "nvda_minute_flip_policy",
                    "mode": "factory",
                    "module": "strategies.stocks_kernel.policies.nvda_minute_flip",
                    "attr": "build_nvda_minute_flip_policy",
                },
            ),
            execution_profile={
                "protection_enabled": True,
                "quarantine_on_unknown_position": True,
                "offhours_outside_rth": True,
                "overnight_outside_rth": True,
            },
        )

    async def on_init(self, *, strategy: Any, config: Any) -> Optional[Dict[str, Any]]:
        return {
            "strategy_id": str(getattr(strategy, "strategy_id", "") or ""),
            "component": self.component_name,
            "order_type": self.order_type,
            "limit_offset_bp": float(self.limit_offset_bp),
            "dry_run": bool(getattr(config, "dry_run", True)),
        }

    async def on_features(self, *, strategy: Any, symbol: str, features: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        updated = dict(features or {})
        symbol_u = str(symbol or "").strip().upper()
        price = self._extract_price(updated)
        if symbol_u and price > 0.0:
            self._last_price_by_symbol[symbol_u] = price
        updated.setdefault("stock_plugin_component", self.component_name)
        updated.setdefault("nvda_minute_flip_seen", True)
        return updated

    async def on_intent(self, *, strategy: Any, envelope: Any, symbol: str, side: str) -> Any:
        symbol_u = str(symbol or "").strip().upper()
        extras = getattr(envelope, "extras", None)
        if isinstance(extras, dict):
            extras.setdefault("stock_plugin_component", self.component_name)
            extras.setdefault("test_strategy", True)
            extras.setdefault("paper_test", True)
            extras.setdefault("allow_outside_rth", True)
            extras.setdefault("force_order_style", self.order_type in {"LMT", "STP_LMT"})
        if self.order_type == "LMT":
            ref_px = self._last_price_by_symbol.get(symbol_u, 0.0)
            if ref_px > 0.0:
                side_u = str(side or "").strip().upper()
                direction = 1.0 if side_u == "LONG" else -1.0
                limit_px = ref_px * (1.0 + direction * self.limit_offset_bp / 10_000.0)
                envelope.order_style = OrderStyle(kind="LMT", limit_px=round(limit_px, 2))
        return envelope

    @staticmethod
    def _extract_price(features: Dict[str, Any]) -> float:
        for key in ("last_price", "last", "mid_last", "close", "minute_close", "price"):
            try:
                value = float(features.get(key) or 0.0)
            except Exception:
                value = 0.0
            if value > 0.0:
                return value
        return 0.0
