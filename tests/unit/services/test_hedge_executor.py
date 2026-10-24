"""
Unit tests for HedgeExecutor.

Tests cover:
1. Contract selection and quantity calculation
2. Daily limit enforcement
3. Failure tracking and cooldown
4. Dry-run vs real execution
5. Instrument whitelist validation
"""

import pytest
from datetime import datetime
from unittest.mock import Mock, AsyncMock, patch
from zoneinfo import ZoneInfo

from services.risk.hedge_executor import HedgeExecutor, HedgeResult
from scripts.ai_trader.risk.options_selector import (
    OptionsSelector,
    OptionsPolicy,
    SelectionResult,
    SelectedContract,
)
from services.market.options_data import StubOptionsDataProvider

ET = ZoneInfo("America/New_York")


@pytest.fixture
def stub_provider():
    """Create stub options data provider."""
    return StubOptionsDataProvider()


@pytest.fixture
def options_selector(stub_provider):
    """Create OptionsSelector with stub provider."""
    policy = OptionsPolicy(
        enabled=True,
        instruments=["SPY", "QQQ"],
        dry_run=True,
    )
    return OptionsSelector(policy=policy, data_provider=stub_provider)


@pytest.fixture
def executor(options_selector):
    """Create HedgeExecutor with mocked config."""
    with patch('services.risk.hedge_executor.config') as mock_config:
        mock_config.get.return_value = {
            "enabled": True,
            "instruments": ["SPY", "QQQ", "IWM", "DIA"],
            "max_notional_usd_per_day": 100000.0,
            "max_contracts_per_day": 50,
            "slippage_bps": 10.0,
            "tif": "DAY",
            "dry_run": True,
            "allow_linear_fallback": False,
        }
        executor = HedgeExecutor(options_selector=options_selector)
        return executor


class TestHedgeExecutorBasics:
    """Test basic executor functionality."""

    def test_init(self, executor):
        """Test executor initialization."""
        assert executor.enabled is True
        assert executor.dry_run is True
        assert executor.whitelist == ["SPY", "QQQ", "IWM", "DIA"]
        assert executor.max_notional_per_day == 100000.0
        assert executor.max_contracts_per_day == 50
        assert executor.consecutive_failures == 0

    def test_determine_action(self, executor):
        """Test action determination from delta gap."""
        # Need to add protection (negative gap)
        assert executor._determine_action(-15000) == "add"

        # Need to reduce protection (positive gap)
        assert executor._determine_action(15000) == "reduce"

        # Within tolerance (hold)
        assert executor._determine_action(500) == "hold"
        assert executor._determine_action(-500) == "hold"

    def test_daily_reset(self, executor):
        """Test daily stats reset."""
        from datetime import timedelta

        # Set up stats from yesterday
        today = datetime.now(ET).date()
        yesterday = today - timedelta(days=1)
        executor.daily_stats = {
            "date": yesterday,
            "contracts": 10,
            "notional": 50000.0,
        }

        # Check reset
        executor._check_daily_reset()

        # Should reset to today with zero stats
        assert executor.daily_stats["date"] == today
        assert executor.daily_stats["contracts"] == 0
        assert executor.daily_stats["notional"] == 0.0


class TestDailyLimits:
    """Test daily limit enforcement."""

    def test_check_daily_limits_contracts(self, executor):
        """Test contract limit enforcement."""
        # Set stats near limit
        executor.daily_stats["contracts"] = 49
        executor.daily_stats["notional"] = 0.0

        # Should pass (49 + 1 = 50, at limit)
        assert executor._check_daily_limits(add_contracts=1, add_notional=0.0) is True

        # Set stats at limit
        executor.daily_stats["contracts"] = 50

        # Should fail (would exceed)
        assert executor._check_daily_limits(add_contracts=1, add_notional=0.0) is False

    def test_check_daily_limits_notional(self, executor):
        """Test notional limit enforcement."""
        # Set stats near limit
        executor.daily_stats["contracts"] = 0
        executor.daily_stats["notional"] = 90000.0

        # Should pass (90k + 9k = 99k, under 100k limit)
        assert executor._check_daily_limits(add_contracts=0, add_notional=9000) is True

        # Should fail (90k + 11k = 101k, over limit)
        assert executor._check_daily_limits(add_contracts=0, add_notional=11000) is False

    @pytest.mark.asyncio
    async def test_execute_rejects_on_limit_exceeded(self, executor):
        """Test that execute rejects when limits exceeded."""
        # Set stats at contract limit
        executor.daily_stats["contracts"] = 50
        executor.daily_stats["notional"] = 0.0

        exposure = {"net": 100000, "gross": 200000}
        result = await executor.execute(
            delta_gap_usd=-15000,
            exposure=exposure,
        )

        assert result.success is False
        assert result.action == "reject"
        assert "Daily limits exceeded" in result.reason


class TestFailureTracking:
    """Test failure tracking and cooldown."""

    def test_record_failure(self, executor):
        """Test failure recording."""
        assert executor.consecutive_failures == 0

        executor._record_failure()
        assert executor.consecutive_failures == 1
        assert executor.last_failure_ts > 0

        executor._record_failure()
        assert executor.consecutive_failures == 2

    def test_in_failure_cooldown(self, executor):
        """Test cooldown detection."""
        # No failures = no cooldown
        assert executor._in_failure_cooldown() is False

        # Record failure
        executor._record_failure()

        # Should be in cooldown
        assert executor._in_failure_cooldown() is True

    def test_reset_failures(self, executor):
        """Test manual failure reset."""
        executor._record_failure()
        executor._record_failure()
        assert executor.consecutive_failures == 2

        executor.reset_failures()
        assert executor.consecutive_failures == 0
        assert executor.last_failure_ts == 0.0

    @pytest.mark.asyncio
    async def test_execute_skips_during_cooldown(self, executor):
        """Test that execute skips when in cooldown."""
        # Record failure to trigger cooldown
        executor._record_failure()

        exposure = {"net": 100000, "gross": 200000}
        result = await executor.execute(
            delta_gap_usd=-15000,
            exposure=exposure,
        )

        assert result.success is False
        assert result.action == "hold"
        assert "cooldown" in result.reason.lower()


class TestWhitelist:
    """Test instrument whitelist validation."""

    @pytest.mark.asyncio
    async def test_whitelist_rejection(self, executor, options_selector):
        """Test rejection of non-whitelisted instruments."""
        # Mock contract selection to return non-whitelisted instrument
        mock_contract = SelectedContract(
            occ_symbol="AAPL  251219P00200000",
            underlying="AAPL",  # Not in whitelist
            expiry_date="2025-12-19",
            strike=200.0,
            right="P",
            delta=-0.3,
            open_interest=5000,
            spread_bps=20.0,
            mid_price=5.0,
            limit_price=5.05,
            quantity=1,
            notional_usd=15000,
            tif="DAY",
            rationale="Test contract",
        )

        mock_selection = SelectionResult(
            success=True,
            contract=mock_contract,
            candidates_evaluated=10,
        )

        with patch.object(options_selector, 'select_hedge_contract', return_value=mock_selection):
            exposure = {"net": 100000, "gross": 200000}
            result = await executor.execute(
                delta_gap_usd=-15000,
                exposure=exposure,
            )

            assert result.success is False
            assert result.action == "reject"
            assert "whitelist" in result.reason.lower()


class TestDryRunVsRealExecution:
    """Test dry-run vs real execution behavior."""

    @pytest.mark.asyncio
    async def test_dry_run_execution(self, executor, options_selector):
        """Test dry-run execution logs but doesn't submit orders."""
        # Mock successful contract selection
        mock_contract = SelectedContract(
            occ_symbol="SPY   251219P00650000",
            underlying="SPY",
            expiry_date="2025-12-19",
            strike=650.0,
            right="P",
            delta=-0.3,
            open_interest=10000,
            spread_bps=15.0,
            mid_price=10.0,
            limit_price=10.1,
            quantity=1,
            notional_usd=15000,
            tif="DAY",
            rationale="Test hedge contract",
        )

        mock_selection = SelectionResult(
            success=True,
            contract=mock_contract,
            candidates_evaluated=10,
        )

        with patch.object(options_selector, 'select_hedge_contract', return_value=mock_selection):
            with patch.object(executor, '_get_underlying_price', return_value=650.0):
                exposure = {"net": 100000, "gross": 200000}
                result = await executor.execute(
                    delta_gap_usd=-15000,
                    exposure=exposure,
                )

                assert result.success is True
                assert result.action == "add"
                assert result.contract is not None
                assert result.qty == 1
                assert result.notional_usd == 15000

                # Check daily stats updated (even in dry-run for testing)
                assert executor.daily_stats["contracts"] == 1
                assert executor.daily_stats["notional"] == 15000

    @pytest.mark.asyncio
    async def test_real_execution_without_router(self, executor, options_selector):
        """Test real execution fails gracefully without order router."""
        # Set executor to real mode (not dry-run)
        executor.dry_run = False
        executor.order_router = None

        # Mock successful contract selection
        mock_contract = SelectedContract(
            occ_symbol="SPY   251219P00650000",
            underlying="SPY",
            expiry_date="2025-12-19",
            strike=650.0,
            right="P",
            delta=-0.3,
            open_interest=10000,
            spread_bps=15.0,
            mid_price=10.0,
            limit_price=10.1,
            quantity=1,
            notional_usd=15000,
            tif="DAY",
            rationale="Test hedge contract",
        )

        mock_selection = SelectionResult(
            success=True,
            contract=mock_contract,
            candidates_evaluated=10,
        )

        with patch.object(options_selector, 'select_hedge_contract', return_value=mock_selection):
            with patch.object(executor, '_get_underlying_price', return_value=650.0):
                exposure = {"net": 100000, "gross": 200000}
                result = await executor.execute(
                    delta_gap_usd=-15000,
                    exposure=exposure,
                )

                assert result.success is False
                assert result.action == "reject"
                assert "router not available" in result.reason.lower()

                # Should record failure
                assert executor.consecutive_failures == 1


class TestLimitPriceCalculation:
    """Test limit price calculation with slippage."""

    def test_calculate_limit_price_buy(self, executor):
        """Test limit price for buying (add protection)."""
        mid_price = 10.0
        # slippage_bps = 10 = 0.1%
        limit = executor._calculate_limit_price(mid_price, side="buy")

        # Should pay up: mid * (1 + 0.001) = 10.01
        assert limit == pytest.approx(10.01, rel=1e-4)

    def test_calculate_limit_price_sell(self, executor):
        """Test limit price for selling (reduce protection)."""
        mid_price = 10.0
        # slippage_bps = 10 = 0.1%
        limit = executor._calculate_limit_price(mid_price, side="sell")

        # Should accept lower: mid * (1 - 0.001) = 9.99
        assert limit == pytest.approx(9.99, rel=1e-4)


class TestContractSelectionFailure:
    """Test handling of contract selection failures."""

    @pytest.mark.asyncio
    async def test_selection_failure(self, executor, options_selector):
        """Test handling when no suitable contract found."""
        # Mock failed selection
        mock_selection = SelectionResult(
            success=False,
            error="No contracts meet criteria",
            candidates_evaluated=5,
        )

        with patch.object(options_selector, 'select_hedge_contract', return_value=mock_selection):
            exposure = {"net": 100000, "gross": 200000}
            result = await executor.execute(
                delta_gap_usd=-15000,
                exposure=exposure,
            )

            assert result.success is False
            assert result.action == "reject"
            assert "selection failed" in result.reason.lower()

            # Should record failure
            assert executor.consecutive_failures == 1


class TestDisabledExecutor:
    """Test executor when disabled."""

    @pytest.mark.asyncio
    async def test_disabled_executor(self, options_selector):
        """Test that disabled executor rejects all requests."""
        with patch('services.risk.hedge_executor.config') as mock_config:
            mock_config.get.return_value = {
                "enabled": False,  # Disabled
                "dry_run": True,
            }
            executor = HedgeExecutor(options_selector=options_selector)

            exposure = {"net": 100000, "gross": 200000}
            result = await executor.execute(
                delta_gap_usd=-15000,
                exposure=exposure,
            )

            assert result.success is False
            assert result.action == "reject"
            assert "disabled" in result.reason.lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
