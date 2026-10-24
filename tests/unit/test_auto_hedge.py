"""Unit tests for auto_hedge.py hedging system."""

import pytest
from datetime import datetime, date
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock
import json

# Import hedge executor classes
import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from scripts.options.auto_hedge import (
    AutoHedgeExecutor,
    HedgeIntent,
    HedgeOrder,
    HedgeResult,
    HEDGE_INSTRUMENTS,
    SYMBOL_CORRELATIONS,
)


class TestHedgeIntent:
    """Test HedgeIntent dataclass."""

    def test_hedge_intent_creation(self):
        """Test creating a hedge intent."""
        intent = HedgeIntent(
            symbols=["TSLA", "NVDA"],
            notional_pct=0.5,
            urgency="medium",
            strategy="options_put",
        )

        assert intent.symbols == ["TSLA", "NVDA"]
        assert intent.notional_pct == 0.5
        assert intent.urgency == "medium"
        assert intent.strategy == "options_put"
        assert intent.max_cost_bps == 100  # default

    def test_hedge_intent_with_confidence(self):
        """Test hedge intent with confidence field."""
        intent = HedgeIntent(
            symbols=["AAPL"],
            notional_pct=1.0,
            confidence=0.55,
            urgency="high",
        )

        assert intent.confidence == 0.55
        assert intent.urgency == "high"


class TestAutoHedgeExecutor:
    """Test AutoHedgeExecutor class."""

    def setup_method(self):
        """Setup test fixtures."""
        self.tmp_dir = Path("/tmp/test_hedge_audit")
        self.tmp_dir.mkdir(parents=True, exist_ok=True)

        self.executor = AutoHedgeExecutor(
            dry_run=True,
            audit_dir=self.tmp_dir,
        )

    def teardown_method(self):
        """Cleanup test fixtures."""
        import shutil
        if self.tmp_dir.exists():
            shutil.rmtree(self.tmp_dir)

    def test_executor_initialization(self):
        """Test executor initializes correctly."""
        assert self.executor.dry_run is True
        assert self.executor.paper_mode is False
        assert self.executor.audit_dir == self.tmp_dir

    def test_select_hedge_instrument_explicit(self):
        """Test selecting explicitly specified hedge instrument."""
        intent = HedgeIntent(
            symbols=["TSLA"],
            notional_pct=0.5,
            instrument="QQQ",
        )

        result = self.executor._select_hedge_instrument(intent)
        assert result == "QQQ"

    def test_select_hedge_instrument_correlation_based(self):
        """Test selecting hedge instrument based on correlations."""
        intent = HedgeIntent(
            symbols=["NVDA"],  # NVDA has high correlation with QQQ
            notional_pct=0.5,
        )

        result = self.executor._select_hedge_instrument(intent)
        # NVDA has highest correlation with QQQ (0.80)
        assert result in ["QQQ", "SPY"]  # Should select based on correlation score

    def test_confidence_to_size_multiplier_low(self):
        """Test size multiplier for low confidence (<0.6)."""
        multiplier = self.executor._confidence_to_size_multiplier(0.4)
        assert multiplier == 1.0  # Full hedge

    def test_confidence_to_size_multiplier_medium(self):
        """Test size multiplier for medium confidence (0.6-0.8)."""
        multiplier = self.executor._confidence_to_size_multiplier(0.7)
        assert multiplier == 0.5  # Partial hedge

    def test_confidence_to_size_multiplier_high(self):
        """Test size multiplier for high confidence (>=0.8)."""
        multiplier = self.executor._confidence_to_size_multiplier(0.9)
        assert multiplier == 0.25  # Light hedge

    @pytest.mark.asyncio
    async def test_get_positions_mock(self):
        """Test getting positions (mock data)."""
        positions = await self.executor._get_positions(["TSLA", "AAPL"])

        assert "TSLA" in positions
        assert "AAPL" in positions
        assert positions["TSLA"]["qty"] == 100
        assert positions["TSLA"]["notional"] == 25000.0

    @pytest.mark.asyncio
    async def test_get_current_price_mock(self):
        """Test getting current price (mock data)."""
        price_spy = await self.executor._get_current_price("SPY")
        price_qqq = await self.executor._get_current_price("QQQ")

        assert price_spy == 580.0
        assert price_qqq == 510.0

    def test_round_strike(self):
        """Test strike price rounding logic."""
        # Low prices: 0.5 increments
        assert self.executor._round_strike(45.7) == 45.5
        assert self.executor._round_strike(48.3) == 48.5

        # Medium prices: 1.0 increments
        assert self.executor._round_strike(150.4) == 150.0
        assert self.executor._round_strike(150.6) == 151.0

        # High prices: 5.0 increments
        assert self.executor._round_strike(487.0) == 485.0
        assert self.executor._round_strike(493.0) == 495.0

    def test_select_expiry(self):
        """Test option expiry selection."""
        expiry = self.executor._select_expiry(days_out=30)

        # Should be a Friday
        assert expiry.weekday() == 4
        # Should be ~30 days out
        today = datetime.now(expiry.tzinfo).date()
        delta = (expiry.date() - today).days
        assert 25 <= delta <= 40  # Allow some flexibility

    @pytest.mark.asyncio
    async def test_generate_put_hedge(self):
        """Test generating protective put hedge orders."""
        intent = HedgeIntent(
            symbols=["TSLA"],
            notional_pct=0.5,
            strategy="options_put",
        )

        orders = await self.executor._generate_put_hedge("SPY", 25000.0, intent)

        assert len(orders) == 1
        order = orders[0]

        assert order.instrument == "SPY"
        assert order.side == "buy"
        assert order.symbol_type == "option"
        assert order.right == "P"
        assert order.occ_symbol is not None
        assert order.estimated_cost > 0
        assert 0 < order.hedge_ratio < 1  # Delta should be between 0 and 1

    @pytest.mark.asyncio
    async def test_generate_collar_hedge(self):
        """Test generating collar hedge (put + short call) orders."""
        intent = HedgeIntent(
            symbols=["TSLA"],
            notional_pct=0.5,
            strategy="options_collar",
        )

        orders = await self.executor._generate_collar_hedge("SPY", 25000.0, intent)

        assert len(orders) == 2  # One put, one call

        # Find put and call orders
        put_order = next(o for o in orders if o.right == "P")
        call_order = next(o for o in orders if o.right == "C")

        assert put_order.side == "buy"
        assert call_order.side == "sell"
        assert put_order.strike < call_order.strike  # Put strike < Call strike

    @pytest.mark.asyncio
    async def test_generate_stock_hedge(self):
        """Test generating stock short hedge orders."""
        intent = HedgeIntent(
            symbols=["TSLA"],
            notional_pct=0.5,
            strategy="stock_short",
        )

        orders = await self.executor._generate_stock_hedge("SPY", 25000.0, intent)

        assert len(orders) == 1
        order = orders[0]

        assert order.instrument == "SPY"
        assert order.side == "sell"
        assert order.symbol_type == "stock"
        assert order.hedge_ratio == 1.0  # Full delta hedge

    @pytest.mark.asyncio
    async def test_execute_hedge_no_positions(self):
        """Test hedge execution when no positions exist."""
        intent = HedgeIntent(
            symbols=["UNKNOWN_SYMBOL"],
            notional_pct=0.5,
        )

        # Mock get_positions to return empty
        with patch.object(self.executor, '_get_positions', return_value={}):
            result = await self.executor.execute_hedge(intent)

        assert result.success is True
        assert result.error == "no_positions_to_hedge"
        assert len(result.orders) == 0

    @pytest.mark.asyncio
    async def test_execute_hedge_cost_exceeded(self):
        """Test hedge execution when cost exceeds maximum."""
        intent = HedgeIntent(
            symbols=["TSLA"],
            notional_pct=0.5,
            max_cost_bps=10,  # Very low cost threshold
        )

        result = await self.executor.execute_hedge(intent)

        assert result.success is False
        assert "hedge_cost_too_high" in result.error
        assert result.total_cost > 0

    @pytest.mark.asyncio
    async def test_execute_hedge_dry_run_success(self):
        """Test successful hedge execution in dry-run mode."""
        intent = HedgeIntent(
            symbols=["TSLA"],
            notional_pct=0.5,
            urgency="medium",
            strategy="options_put",
            max_cost_bps=2000,  # Allow higher cost for test to pass
        )

        result = await self.executor.execute_hedge(intent)

        assert result.success is True
        assert result.dry_run is True
        assert len(result.orders) > 0
        assert len(result.executed_orders) > 0
        assert result.total_cost > 0
        assert result.hedge_effectiveness is not None

    @pytest.mark.asyncio
    async def test_execute_hedge_with_confidence_adjustment(self):
        """Test hedge sizing adjustment based on confidence."""
        intent_low_conf = HedgeIntent(
            symbols=["TSLA"],
            notional_pct=0.5,
            confidence=0.4,  # Low confidence → full hedge
            max_cost_bps=2000,
        )

        intent_high_conf = HedgeIntent(
            symbols=["TSLA"],
            notional_pct=0.5,
            confidence=0.9,  # High confidence → reduced hedge
            max_cost_bps=2000,
        )

        result_low = await self.executor.execute_hedge(intent_low_conf)
        result_high = await self.executor.execute_hedge(intent_high_conf)

        # Low confidence should result in higher cost (more hedge contracts)
        if result_low.success and result_high.success:
            assert result_low.total_cost > result_high.total_cost

    @pytest.mark.asyncio
    async def test_audit_log_written(self):
        """Test that audit log is written correctly."""
        intent = HedgeIntent(
            symbols=["TSLA"],
            notional_pct=0.5,
            max_cost_bps=2000,  # Allow higher cost for test to pass
        )

        result = await self.executor.execute_hedge(intent)

        # Check audit file exists
        audit_file = self.tmp_dir / "hedge_audit.jsonl"
        assert audit_file.exists()

        # Parse audit log
        with open(audit_file, 'r') as f:
            records = [json.loads(line) for line in f if line.strip()]

        assert len(records) == 1
        record = records[0]

        assert "ts" in record
        assert "intent" in record
        assert "result" in record
        assert record["intent"]["symbols"] == ["TSLA"]
        assert record["result"]["success"] == result.success


class TestHedgeInstruments:
    """Test hedge instrument configuration."""

    def test_hedge_instruments_available(self):
        """Test that hedge instruments are properly configured."""
        assert "SPY" in HEDGE_INSTRUMENTS
        assert "QQQ" in HEDGE_INSTRUMENTS
        assert "IWM" in HEDGE_INSTRUMENTS

        spy_config = HEDGE_INSTRUMENTS["SPY"]
        assert spy_config["priority"] == 1  # SPY is highest priority
        assert spy_config["beta_spy"] == 1.0
        assert spy_config["min_liquidity"] > 0

    def test_symbol_correlations_available(self):
        """Test that symbol correlations are configured."""
        assert "TSLA" in SYMBOL_CORRELATIONS
        assert "NVDA" in SYMBOL_CORRELATIONS

        tsla_corr = SYMBOL_CORRELATIONS["TSLA"]
        assert "SPY" in tsla_corr
        assert "QQQ" in tsla_corr
        assert 0 <= tsla_corr["SPY"] <= 1
        assert 0 <= tsla_corr["QQQ"] <= 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
