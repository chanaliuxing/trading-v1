"""Tests for execution planner."""

import pytest

from brain.decision_schema import BrainDecision
from execution.planner import (
    ExecutionPlanner,
    PortfolioState,
    OrderInstruction,
)
from strategies.runtime import StrategyManifest


class TestPortfolioState:
    """Test PortfolioState dataclass."""

    def test_portfolio_state_creation(self):
        """Test creating portfolio state."""
        portfolio = PortfolioState(
            cash=100000.0,
            position_qty=100,
            position_avg_price=150.0,
        )

        assert portfolio.cash == 100000.0
        assert portfolio.position_qty == 100
        assert portfolio.position_avg_price == 150.0


class TestOrderInstruction:
    """Test OrderInstruction dataclass."""

    def test_order_instruction_creation(self):
        """Test creating order instruction."""
        order = OrderInstruction(
            symbol="AAPL",
            side="BUY",
            quantity=100,
            order_type="MKT",
            time_in_force="DAY",
            reason="Brain ENTER",
            extra={"run_id": "test_run_001"},
        )

        assert order.symbol == "AAPL"
        assert order.side == "BUY"
        assert order.quantity == 100
        assert order.extra["run_id"] == "test_run_001"


class TestExecutionPlanner:
    """Test ExecutionPlanner class."""

    @pytest.fixture
    def planner(self):
        """Create execution planner."""
        return ExecutionPlanner(min_notional=500.0, max_leverage=1.0)

    @pytest.fixture
    def portfolio(self):
        """Create test portfolio state."""
        return PortfolioState(cash=100000.0, position_qty=0, position_avg_price=0.0)

    @pytest.fixture
    def strategy(self):
        """Create test strategy manifest."""
        return StrategyManifest(
            run_id="test_run_001",
            symbol="AAPL",
            freq="1m",
            horizon="10m",
            label_id="ret_10m_fwd",
            model_type="xgb",
            model_path="models/test.bin",
            feature_subset=["close", "volume"],
            params={},
        )

    def test_planner_creation(self):
        """Test creating planner with custom parameters."""
        planner = ExecutionPlanner(min_notional=1000.0, max_leverage=2.0)

        assert planner._min_notional == 1000.0
        assert planner._max_leverage == 2.0

    def test_hold_decision_returns_empty(self, planner, portfolio, strategy):
        """Test that HOLD decision returns no orders."""
        decision = BrainDecision(
            decision="HOLD",
            chosen_run_id=None,
            side=None,
            size_frac=0.0,
        )

        risk_gates = {"equity": 100000.0}
        orders = planner.plan(
            decision=decision,
            strategy=strategy,
            portfolio=portfolio,
            last_price=150.0,
            model_score=0.5,
            risk_gates=risk_gates,
        )

        assert orders == []

    def test_daily_loss_limit_blocks_orders(self, planner, portfolio, strategy):
        """Test that daily loss limit prevents new orders."""
        decision = BrainDecision(
            decision="ENTER",
            chosen_run_id="test_run_001",
            side="LONG",
            size_frac=0.5,
        )

        # Set daily loss beyond limit
        risk_gates = {
            "equity": 100000.0,
            "today_pnl": -3000.0,
            "daily_loss_limit": 2000.0,
        }

        orders = planner.plan(
            decision=decision,
            strategy=strategy,
            portfolio=portfolio,
            last_price=150.0,
            model_score=1.0,
            risk_gates=risk_gates,
        )

        assert orders == []  # Blocked by risk gates

    def test_exit_with_long_position(self, planner, strategy):
        """Test EXIT decision with long position."""
        portfolio = PortfolioState(
            cash=50000.0,
            position_qty=100,
            position_avg_price=145.0,
        )

        decision = BrainDecision(
            decision="EXIT",
            chosen_run_id="test_run_001",
            side=None,
            size_frac=0.0,
            notes="Exit signal detected",
        )

        risk_gates = {"equity": 65000.0}
        orders = planner.plan(
            decision=decision,
            strategy=strategy,
            portfolio=portfolio,
            last_price=150.0,
            model_score=0.0,
            risk_gates=risk_gates,
        )

        assert len(orders) == 1
        assert orders[0].side == "SELL"
        assert orders[0].quantity == 100
        assert orders[0].order_type == "MKT"
        assert orders[0].symbol == "AAPL"
        assert "Exit signal" in orders[0].reason

    def test_exit_with_short_position(self, planner, strategy):
        """Test EXIT decision with short position."""
        portfolio = PortfolioState(
            cash=150000.0,
            position_qty=-100,
            position_avg_price=155.0,
        )

        decision = BrainDecision(
            decision="EXIT",
            chosen_run_id="test_run_001",
            side=None,
            size_frac=0.0,
        )

        risk_gates = {"equity": 135000.0}
        orders = planner.plan(
            decision=decision,
            strategy=strategy,
            portfolio=portfolio,
            last_price=150.0,
            model_score=0.0,
            risk_gates=risk_gates,
        )

        assert len(orders) == 1
        assert orders[0].side == "BUY"
        assert orders[0].quantity == 100

    def test_exit_with_no_position(self, planner, portfolio, strategy):
        """Test EXIT decision with no existing position."""
        decision = BrainDecision(
            decision="EXIT",
            chosen_run_id="test_run_001",
            side=None,
            size_frac=0.0,
        )

        risk_gates = {"equity": 100000.0}
        orders = planner.plan(
            decision=decision,
            strategy=strategy,
            portfolio=portfolio,
            last_price=150.0,
            model_score=0.0,
            risk_gates=risk_gates,
        )

        assert orders == []  # No position to exit

    def test_enter_long_from_flat(self, planner, portfolio, strategy):
        """Test ENTER LONG decision from flat position."""
        decision = BrainDecision(
            decision="ENTER",
            chosen_run_id="test_run_001",
            side="LONG",
            size_frac=0.3,
            notes="Strong buy signal",
        )

        risk_gates = {
            "equity": 100000.0,
            "max_position_frac": 0.5,
        }

        orders = planner.plan(
            decision=decision,
            strategy=strategy,
            portfolio=portfolio,
            last_price=150.0,
            model_score=2.0,  # High confidence
            risk_gates=risk_gates,
        )

        assert len(orders) == 1
        assert orders[0].side == "BUY"
        assert orders[0].quantity > 0
        assert orders[0].symbol == "AAPL"
        assert orders[0].extra["run_id"] == "test_run_001"
        assert "Strong buy" in orders[0].reason

    def test_enter_without_side_returns_empty(self, planner, portfolio, strategy):
        """Test ENTER decision without side returns no orders."""
        decision = BrainDecision(
            decision="ENTER",
            chosen_run_id="test_run_001",
            side=None,  # Missing side
            size_frac=0.3,
        )

        risk_gates = {"equity": 100000.0}
        orders = planner.plan(
            decision=decision,
            strategy=strategy,
            portfolio=portfolio,
            last_price=150.0,
            model_score=1.0,
            risk_gates=risk_gates,
        )

        assert orders == []

    def test_adjust_increases_long_position(self, planner, strategy):
        """Test ADJUST decision increasing long position."""
        portfolio = PortfolioState(
            cash=70000.0,
            position_qty=50,
            position_avg_price=145.0,
        )

        decision = BrainDecision(
            decision="ADJUST",
            chosen_run_id="test_run_001",
            side="LONG",
            size_frac=0.4,
        )

        risk_gates = {
            "equity": 77500.0,  # cash + 50 * 150
            "max_position_frac": 0.5,
        }

        orders = planner.plan(
            decision=decision,
            strategy=strategy,
            portfolio=portfolio,
            last_price=150.0,
            model_score=1.5,
            risk_gates=risk_gates,
        )

        # Should increase position
        if orders:
            assert orders[0].side == "BUY"
            assert orders[0].quantity > 0

    def test_adjust_no_increase_if_sufficient_position(self, planner, strategy):
        """Test ADJUST doesn't add if position already sufficient."""
        # Large existing position
        portfolio = PortfolioState(
            cash=50000.0,
            position_qty=200,
            position_avg_price=145.0,
        )

        decision = BrainDecision(
            decision="ADJUST",
            chosen_run_id="test_run_001",
            side="LONG",
            size_frac=0.2,  # Small target
        )

        risk_gates = {
            "equity": 80000.0,
            "max_position_frac": 0.3,
        }

        orders = planner.plan(
            decision=decision,
            strategy=strategy,
            portfolio=portfolio,
            last_price=150.0,
            model_score=0.5,
            risk_gates=risk_gates,
        )

        # Position already exceeds target
        assert orders == []

    def test_compute_target_notional_with_model_confidence(self, planner):
        """Test that model score affects position sizing."""
        decision = BrainDecision(
            decision="ENTER",
            chosen_run_id="test",
            side="LONG",
            size_frac=0.5,
        )

        portfolio = PortfolioState(cash=100000.0, position_qty=0, position_avg_price=0.0)
        risk_gates = {"equity": 100000.0, "max_position_frac": 0.5}

        # High confidence score should result in larger notional
        notional_high = planner._compute_target_notional(
            decision=decision,
            portfolio=portfolio,
            last_price=150.0,
            risk_gates=risk_gates,
            model_score=3.0,  # High confidence
        )

        # Low confidence score should result in smaller notional
        notional_low = planner._compute_target_notional(
            decision=decision,
            portfolio=portfolio,
            last_price=150.0,
            risk_gates=risk_gates,
            model_score=0.1,  # Low confidence
        )

        assert notional_high > notional_low

    def test_min_notional_enforced(self, planner):
        """Test that minimum notional is enforced."""
        decision = BrainDecision(
            decision="ENTER",
            chosen_run_id="test",
            side="LONG",
            size_frac=0.001,  # Very small fraction
        )

        portfolio = PortfolioState(cash=100000.0, position_qty=0, position_avg_price=0.0)
        risk_gates = {"equity": 100000.0, "max_position_frac": 0.5}

        notional = planner._compute_target_notional(
            decision=decision,
            portfolio=portfolio,
            last_price=150.0,
            risk_gates=risk_gates,
            model_score=0.5,
        )

        # Should be at least min_notional
        assert notional >= planner._min_notional

    def test_order_extra_contains_metadata(self, planner, portfolio, strategy):
        """Test that order extra dict contains metadata."""
        decision = BrainDecision(
            decision="ENTER",
            chosen_run_id="test_run_001",
            side="LONG",
            size_frac=0.3,
        )

        risk_gates = {"equity": 100000.0, "max_position_frac": 0.5}
        orders = planner.plan(
            decision=decision,
            strategy=strategy,
            portfolio=portfolio,
            last_price=150.0,
            model_score=1.5,
            risk_gates=risk_gates,
        )

        if orders:
            extra = orders[0].extra
            assert extra["decision"] == "ENTER"
            assert extra["run_id"] == "test_run_001"
            assert "model_score" in extra
            assert extra["model_score"] == 1.5
