"""Simple strategy that consumes `ml_signal` and submits intents."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

import pandas as pd

from obs.audit import REASONS, log_event
from libs.dal.sfs_facade import SFS1sReader
from libs.strategy.base import (
    DecisionEnvelope,
    Freshness,
    OrderStyle,
    StrategyAlgorithm,
)
from libs.strategy.registry import register_strategy
from services.strategy.strategy_base import make_decision_id


@register_strategy("model_signal")
class ModelSignalStrategy(StrategyAlgorithm):
    """Translate model-driven signals into intents."""

    async def initialize(self) -> None:
        cfg: Dict[str, Any] = self.params.extras or {}
        self.symbol: str = cfg.get("symbol", "")
        if not self.symbol:
            raise ValueError("ModelSignalStrategy requires `symbol` in extras")
        self.lot: int = int(cfg.get("lot", 100))
        self.max_qty: int = int(cfg.get("max_qty", 1000))
        self.conf_floor: float = float(cfg.get("conf_floor", 0.0))
        self.scale: float = float(cfg.get("scale", 1.0))
        self.order_type: str = str(cfg.get("order_type", "MKT")).upper()
        self.limit_offset_bps: float = float(cfg.get("limit_offset_bps", 3.0))
        # Optional SFS-1s micro-structure confirmation
        self.use_sfs: bool = bool(cfg.get("use_sfs_1s", False))
        if self.use_sfs:
            self.sfs_reader = SFS1sReader()
            self.sfs_lookback: int = int(cfg.get("sfs_lookback", 20))
            self.sfs_confirm_up: int = int(cfg.get("sfs_confirm_up", 6))
            self.sfs_confirm_down: int = int(cfg.get("sfs_confirm_down", 6))

    def _target_from_signal(self, signal: float, conf: float) -> float:
        if conf < self.conf_floor:
            return 0.0
        raw = float(signal) * self.max_qty * self.scale
        if abs(raw) < self.lot:
            return 0.0
        # Round to lot size
        qty = round(raw / self.lot) * self.lot
        return qty

    @staticmethod
    def _ts_us(features: Dict[str, Any]) -> int:
        ts = features.get("ts")
        if isinstance(ts, pd.Timestamp):
            return int(ts.value // 1000)
        if isinstance(ts, datetime):
            return int(ts.timestamp() * 1_000_000)
        if ts is None:
            return 0
        try:
            return int(ts)
        except Exception:
            return 0

    async def on_features(self, symbol: str, features: Dict[str, Any]) -> None:
        if symbol != self.symbol:
            return

        sig = float(features.get("ml_signal", 0.0) or 0.0)
        conf = float(features.get("ml_conf", 0.0) or 0.0)
        pred = float(features.get("ml_pred", 0.0) or 0.0)

        if conf < self.conf_floor:
            log_event(
                "order_skipped",
                component="Strategy",
                strategy_id=self.ctx.strategy_id,
                symbol=symbol,
                sig=sig,
                conf=conf,
                reason="low_conf",
                reason_detail=REASONS["low_conf"],
            )
            return

        if abs(sig) < 1e-9:
            log_event(
                "order_skipped",
                component="Strategy",
                strategy_id=self.ctx.strategy_id,
                symbol=symbol,
                sig=sig,
                conf=conf,
                reason="zero_signal",
                reason_detail=REASONS["zero_signal"],
            )
            return

        target_qty = self._target_from_signal(sig, conf)
        if target_qty == 0.0:
            log_event(
                "order_skipped",
                component="Strategy",
                strategy_id=self.ctx.strategy_id,
                symbol=symbol,
                sig=sig,
                conf=conf,
                reason="zero_signal",
                reason_detail=REASONS["zero_signal"],
                msg="below_lot_size",
            )
            return

        mid = float(features.get("close") or features.get("open") or 0.0)
        if mid <= 0:
            mid = float(features.get("ref_px", 0.0) or 0.0)
        if mid <= 0:
            log_event(
                "order_skipped",
                component="Strategy",
                strategy_id=self.ctx.strategy_id,
                symbol=symbol,
                sig=sig,
                conf=conf,
                reason="market_closed",
                reason_detail=REASONS["market_closed"],
            )
            return

        # Optional micro-structure confirmation (Redis 1s)
        if self.use_sfs:
            try:
                sub = await self.sfs_reader.last_n(self.symbol, max(10, self.sfs_lookback))
                closes = [float(x.get("close") or 0) for x in sub if x]
                if len(closes) >= 5:
                    # newest first in Redis
                    ups = sum(1 for i in range(min(10, len(closes) - 1)) if closes[i] > closes[i + 1])
                    downs = 10 - ups
                    if target_qty > 0 and ups < self.sfs_confirm_up:
                        log_event(
                            "order_skipped",
                            component="Strategy",
                            strategy_id=self.ctx.strategy_id,
                            symbol=symbol,
                            sig=sig,
                            conf=conf,
                            reason="micro_confirm_fail",
                            reason_detail="sfs_up10_lt_threshold",
                        )
                        return
                    if target_qty < 0 and downs < self.sfs_confirm_down:
                        log_event(
                            "order_skipped",
                            component="Strategy",
                            strategy_id=self.ctx.strategy_id,
                            symbol=symbol,
                            sig=sig,
                            conf=conf,
                            reason="micro_confirm_fail",
                            reason_detail="sfs_down10_lt_threshold",
                        )
                        return
            except Exception:
                # Non-fatal: if SFS unavailable, proceed without micro confirm
                pass

        order_style = OrderStyle(kind="MKT")
        if self.order_type == "LMT":
            offset = mid * (self.limit_offset_bps / 10_000.0)
            limit_px = mid - offset if target_qty > 0 else mid + offset
            order_style = OrderStyle(kind="LMT", limit_px=limit_px)

        ts_us = self._ts_us(features)
        if not ts_us:
            ts_us = self.ctx.now_us()

        side = "BUY" if target_qty > 0 else "SELL"
        log_event(
            "decision_made",
            component="Strategy",
            strategy_id=self.ctx.strategy_id,
            symbol=self.symbol,
            side=side,
            qty=abs(target_qty),
            sig=sig,
            conf=conf,
            order_type=order_style.kind,
        )

        envelope = DecisionEnvelope(
            decision_id=make_decision_id(),
            strategy_id=self.ctx.strategy_id,
            symbol=self.symbol,
            ts_bar_end_us=int(ts_us),
            target_qty=target_qty,
            order_style=order_style,
            freshness=Freshness(ttl_ms=self.params.ttl_ms, dlb_ms=500),
            extras={
                "ml_signal": sig,
                "ml_conf": conf,
                "ml_pred": pred,
                "ref_px": mid,
            },
        )

        await self.ctx.submit_intent(envelope)
