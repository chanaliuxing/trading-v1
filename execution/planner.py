# execution/planner.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional
import math

from brain.decision_schema import BrainDecision
from strategies.runtime import StrategyManifest


@dataclass
class OrderInstruction:
    """
    标准的订单指令结构，交给 broker 执行。

    Attributes:
        symbol: Trading symbol
        side: Order side (BUY/SELL)
        quantity: Number of shares
        order_type: Order type (MKT/LMT)
        time_in_force: Time in force (DAY/GTC)
        reason: Human-readable reason for the order
        extra: Additional metadata dict
    """
    symbol: str
    side: str           # "BUY" / "SELL"
    quantity: int
    order_type: str     # "MKT" / "LMT"
    time_in_force: str  # "DAY" / "GTC"
    reason: str
    extra: Dict[str, Any]


@dataclass
class PortfolioState:
    """
    简化版的投资组合状态。
    以后可以从你的 IBKR 状态查询模块中构建。

    Attributes:
        cash: Available cash
        position_qty: Current position quantity (positive=long, negative=short)
        position_avg_price: Average entry price for current position
    """
    cash: float
    position_qty: int
    position_avg_price: float
    # 可以扩展：today_pnl, total_equity, margin_used ...


class ExecutionPlanner:
    """
    把 BrainDecision + 模型信号 + 风控信息 → 订单指令。

    This class translates high-level trading decisions from the AI brain
    into concrete order instructions, with risk management and position sizing.
    """

    def __init__(self, *, min_notional: float = 500.0, max_leverage: float = 1.0) -> None:
        """
        Initialize execution planner.

        Args:
            min_notional: Minimum order notional value in USD
            max_leverage: Maximum leverage allowed (1.0 = no leverage)
        """
        self._min_notional = min_notional
        self._max_leverage = max_leverage

    def plan(
        self,
        *,
        decision: BrainDecision,
        strategy: StrategyManifest,
        portfolio: PortfolioState,
        last_price: float,
        model_score: float,
        risk_gates: Dict[str, Any],
    ) -> List[OrderInstruction]:
        """
        返回一组 OrderInstruction（可能为空列表）。

        Args:
            decision: Brain decision (HOLD/ENTER/EXIT/ADJUST)
            strategy: Strategy manifest with run_id and parameters
            portfolio: Current portfolio state
            last_price: Current market price
            model_score: Model prediction score
            risk_gates: Risk management parameters

        Returns:
            List of order instructions (may be empty)
        """
        # 1) 总体风控 gating
        if not self._passes_global_risk(risk_gates):
            return []

        # 2) 根据决策类型分支
        if decision.decision == "HOLD":
            return []

        if decision.decision == "EXIT":
            return self._plan_exit(strategy, portfolio, last_price, risk_gates, decision)

        if decision.decision in {"ENTER", "ADJUST"}:
            return self._plan_enter_or_adjust(
                decision=decision,
                strategy=strategy,
                portfolio=portfolio,
                last_price=last_price,
                model_score=model_score,
                risk_gates=risk_gates,
            )

        # 理论上不会到这里
        return []

    # ---------- 风控 & sizing 逻辑 ----------

    def _passes_global_risk(self, risk_gates: Dict[str, Any]) -> bool:
        """
        示例风控：
        - 日内回撤超过 daily_loss_limit → 禁止新开仓

        Args:
            risk_gates: Risk parameters dict

        Returns:
            True if passes all risk checks, False otherwise
        """
        daily_pnl = risk_gates.get("today_pnl", 0.0)
        loss_limit = risk_gates.get("daily_loss_limit", None)
        if loss_limit is not None and daily_pnl < -abs(loss_limit):
            return False
        return True

    def _compute_target_notional(
        self,
        *,
        decision: BrainDecision,
        portfolio: PortfolioState,
        last_price: float,
        risk_gates: Dict[str, Any],
        model_score: float,
    ) -> float:
        """
        用 size_frac + model_score + risk_gates 计算目标名义价值（美元）。

        Args:
            decision: Brain decision with size_frac
            portfolio: Current portfolio state
            last_price: Current market price
            risk_gates: Risk parameters
            model_score: Model prediction score

        Returns:
            Target notional value in USD
        """
        equity = risk_gates.get("equity", portfolio.cash)
        max_pos_frac = risk_gates.get("max_position_frac", 0.3)
        max_notional = equity * max_pos_frac * self._max_leverage

        # 基础仓位：决策传来的 fraction * max_notional
        base = max_notional * float(decision.size_frac)

        # 根据 model_score 做一点动态调节（例如 sigmoid 后放大/缩小）
        conf = 1.0 / (1.0 + math.exp(-abs(model_score)))
        notional = base * (0.5 + 0.5 * conf)  # 置信度越高，越接近 base 的 1.0 倍

        # 不要低于 min_notional
        notional = max(notional, self._min_notional)

        # 全局限制
        return min(notional, max_notional)

    # ---------- plan 分支 ----------

    def _plan_exit(
        self,
        strategy: StrategyManifest,
        portfolio: PortfolioState,
        last_price: float,
        risk_gates: Dict[str, Any],
        decision: BrainDecision,
    ) -> List[OrderInstruction]:
        """
        EXIT：简单版 → 平掉当前持仓。

        Args:
            strategy: Strategy manifest
            portfolio: Current portfolio state
            last_price: Current market price
            risk_gates: Risk parameters
            decision: Brain decision

        Returns:
            List of exit order instructions
        """
        if portfolio.position_qty == 0:
            return []

        side = "SELL" if portfolio.position_qty > 0 else "BUY"
        qty = abs(portfolio.position_qty)

        return [
            OrderInstruction(
                symbol=strategy.symbol,
                side=side,
                quantity=qty,
                order_type="MKT",
                time_in_force="DAY",
                reason=decision.notes or "Brain EXIT",
                extra={
                    "decision": decision.decision,
                    "run_id": strategy.run_id,
                },
            )
        ]

    def _plan_enter_or_adjust(
        self,
        *,
        decision: BrainDecision,
        strategy: StrategyManifest,
        portfolio: PortfolioState,
        last_price: float,
        model_score: float,
        risk_gates: Dict[str, Any],
    ) -> List[OrderInstruction]:
        """
        ENTER / ADJUST：
        - 先算目标名义
        - 换算成目标持仓数量
        - 用 (target_qty - current_qty) 作为下单数量

        Args:
            decision: Brain decision
            strategy: Strategy manifest
            portfolio: Current portfolio state
            last_price: Current market price
            model_score: Model prediction score
            risk_gates: Risk parameters

        Returns:
            List of entry/adjustment order instructions
        """
        if not decision.side:
            # side 必须指定
            return []

        target_notional = self._compute_target_notional(
            decision=decision,
            portfolio=portfolio,
            last_price=last_price,
            risk_gates=risk_gates,
            model_score=model_score,
        )
        target_qty = int(target_notional // last_price)
        if target_qty <= 0:
            return []

        # 当前仓位方向
        current_qty = portfolio.position_qty
        if decision.side == "LONG":
            delta = target_qty - max(current_qty, 0)
            side = "BUY"
        else:  # SHORT
            delta = target_qty - max(-current_qty, 0)
            side = "SELL"

        if delta <= 0:
            # 已经有足够仓位，不需要加仓
            return []

        return [
            OrderInstruction(
                symbol=strategy.symbol,
                side=side,
                quantity=delta,
                order_type="MKT",
                time_in_force="DAY",
                reason=decision.notes or f"Brain {decision.decision}",
                extra={
                    "decision": decision.decision,
                    "run_id": strategy.run_id,
                    "model_score": model_score,
                },
            )
        ]
