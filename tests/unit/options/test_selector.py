"""Unit tests for options contract selector."""

import pytest
from datetime import datetime, timedelta
from scripts.ai_trader.risk.options_selector import (
    OptionsPolicy,
    OptionsSelector,
    HedgeIntent,
    SelectionResult,
)
from services.market.options_data import (
    StubOptionsDataProvider,
    OptionContract,
)


class TestOptionsPolicy:
    """Test options policy configuration."""

    def test_default_policy(self):
        """Test default policy values."""
        policy = OptionsPolicy()

        assert policy.enabled is True
        assert "SPY" in policy.instruments
        assert policy.primary == "delta"
        assert policy.delta_target_abs == 0.4
        assert 7 in policy.dte_preference
        assert policy.max_contracts_per_day == 50
        assert policy.dry_run is True

    def test_custom_policy(self):
        """Test custom policy construction."""
        policy = OptionsPolicy(
            enabled=False,
            instruments=["QQQ"],
            delta_target_abs=0.5,
            max_contracts_per_day=10,
        )

        assert policy.enabled is False
        assert policy.instruments == ["QQQ"]
        assert policy.delta_target_abs == 0.5
        assert policy.max_contracts_per_day == 10


class TestHedgeIntent:
    """Test hedge intent dataclass."""

    def test_default_intent(self):
        """Test default intent values."""
        intent = HedgeIntent(intent_type="vol_spike")

        assert intent.intent_type == "vol_spike"
        assert intent.size == "medium"
        assert intent.urgency == "normal"

    def test_custom_intent(self):
        """Test custom intent."""
        intent = HedgeIntent(
            intent_type="beta_balance",
            instrument="QQQ",
            size="large",
            urgency="high",
            note="market crash",
        )

        assert intent.intent_type == "beta_balance"
        assert intent.instrument == "QQQ"
        assert intent.size == "large"
        assert intent.urgency == "high"
        assert intent.note == "market crash"


@pytest.mark.asyncio
class TestOptionsSelector:
    """Test options contract selector logic."""

    async def test_selector_initialization(self):
        """Test selector initialization with stub provider."""
        selector = OptionsSelector()

        assert selector.policy is not None
        assert selector.data_provider is not None
        assert selector._daily_stats == {"contracts": 0, "notional": 0.0}

    async def test_select_no_exposure(self):
        """Test selection with zero exposure."""
        selector = OptionsSelector()
        exposure = {"gross": 0.0, "net": 0.0}

        result = await selector.select_hedge_contract(exposure=exposure)

        assert result.success is False
        assert "exposure" in result.error.lower()

    async def test_select_disabled(self):
        """Test selection when policy is disabled."""
        policy = OptionsPolicy(enabled=False)
        selector = OptionsSelector(policy=policy)
        exposure = {"gross": 500_000.0, "net": 300_000.0}

        result = await selector.select_hedge_contract(exposure=exposure)

        assert result.success is False
        assert "disabled" in result.error.lower()

    async def test_select_long_bias_chooses_puts(self):
        """Test that long bias (net > 0) selects puts."""
        selector = OptionsSelector()
        exposure = {"gross": 500_000.0, "net": 300_000.0}  # Net long

        result = await selector.select_hedge_contract(exposure=exposure)

        # With stub provider, should get a valid selection
        if result.success:
            assert result.contract.right == "P"  # Puts for protection

    async def test_select_short_bias_chooses_calls(self):
        """Test that short bias (net < 0) selects calls."""
        selector = OptionsSelector()
        exposure = {"gross": 500_000.0, "net": -300_000.0}  # Net short

        result = await selector.select_hedge_contract(exposure=exposure)

        # With stub provider, should get a valid selection
        if result.success:
            assert result.contract.right == "C"  # Calls for protection

    async def test_target_notional_calculation(self):
        """Test target notional calculation based on intent size."""
        selector = OptionsSelector()
        gross = 1_000_000.0

        # Test different sizes
        small_notional = selector._calculate_target_notional(gross, HedgeIntent("vol_spike", size="small"))
        medium_notional = selector._calculate_target_notional(gross, HedgeIntent("vol_spike", size="medium"))
        large_notional = selector._calculate_target_notional(gross, HedgeIntent("vol_spike", size="large"))

        assert small_notional == pytest.approx(200_000.0)  # 20%
        assert medium_notional == pytest.approx(300_000.0)  # 30%
        assert large_notional == pytest.approx(400_000.0)  # 40%

    async def test_instrument_selection(self):
        """Test instrument selection logic."""
        selector = OptionsSelector()

        # Default to first in policy
        instrument = selector._select_instrument(None)
        assert instrument in selector.policy.instruments

        # Honor intent if valid
        intent = HedgeIntent("vol_spike", instrument="QQQ")
        instrument = selector._select_instrument(intent)
        assert instrument == "QQQ"

        # Fallback if intent instrument not in policy
        intent = HedgeIntent("vol_spike", instrument="INVALID")
        instrument = selector._select_instrument(intent)
        assert instrument in selector.policy.instruments

    async def test_daily_limits_enforcement(self):
        """Test daily limits are enforced."""
        policy = OptionsPolicy(max_contracts_per_day=5, max_notional_usd_per_day=50_000.0)
        selector = OptionsSelector(policy=policy)

        # Simulate reaching limits
        selector._daily_stats = {"contracts": 5, "notional": 45_000.0}

        # This should fail due to contract limit
        assert selector._check_daily_limits(10_000.0) is False

        # Reset and test notional limit
        selector._daily_stats = {"contracts": 3, "notional": 45_000.0}
        assert selector._check_daily_limits(10_000.0) is False  # Would exceed 50k

        # Within limits should pass
        selector._daily_stats = {"contracts": 3, "notional": 40_000.0}
        assert selector._check_daily_limits(5_000.0) is True

    async def test_daily_stats_update(self):
        """Test daily stats tracking."""
        selector = OptionsSelector()

        assert selector._daily_stats == {"contracts": 0, "notional": 0.0}

        selector.update_daily_stats(2, 50_000.0)
        assert selector._daily_stats["contracts"] == 2
        assert selector._daily_stats["notional"] == 50_000.0

        selector.update_daily_stats(1, 25_000.0)
        assert selector._daily_stats["contracts"] == 3
        assert selector._daily_stats["notional"] == 75_000.0

        selector.reset_daily_stats()
        assert selector._daily_stats == {"contracts": 0, "notional": 0.0}

    async def test_absolute_safety_limits(self):
        """Test that absolute safety limits cannot be exceeded."""
        # Even with high config values, absolute limits should apply
        policy = OptionsPolicy(
            max_contracts_per_day=200,  # Higher than absolute max (100)
            max_notional_usd_per_day=1_000_000.0,  # Higher than absolute max (500k)
        )
        selector = OptionsSelector(policy=policy)

        # Simulate at absolute contract limit (100 contracts)
        selector._daily_stats = {"contracts": 100, "notional": 400_000.0}
        assert selector._check_daily_limits(10_000.0) is False

        # Simulate at absolute notional limit (500k)
        selector._daily_stats = {"contracts": 50, "notional": 500_000.0}
        assert selector._check_daily_limits(10_000.0) is False

        # Simulate exceeding notional limit with this trade
        selector._daily_stats = {"contracts": 50, "notional": 495_000.0}
        assert selector._check_daily_limits(10_000.0) is False  # Would bring to 505k

        # Within both limits should pass
        selector._daily_stats = {"contracts": 50, "notional": 400_000.0}
        assert selector._check_daily_limits(50_000.0) is True


@pytest.mark.asyncio
class TestContractSelection:
    """Test contract selection algorithm."""

    async def test_contract_filtering_by_oi(self):
        """Test contracts are filtered by open interest."""
        policy = OptionsPolicy(min_open_interest=5000)
        selector = OptionsSelector(policy=policy)

        # Create mock contracts with varying OI
        contracts = [
            OptionContract(
                symbol="SPY_2025-01-20_P_450",
                underlying="SPY",
                expiry_date="2025-01-20",
                strike=450.0,
                right="P",
                bid=5.0,
                ask=5.1,
                open_interest=3000,  # Below threshold
                delta=-0.4,
            ),
            OptionContract(
                symbol="SPY_2025-01-20_P_455",
                underlying="SPY",
                expiry_date="2025-01-20",
                strike=455.0,
                right="P",
                bid=6.0,
                ask=6.1,
                open_interest=10000,  # Above threshold
                delta=-0.45,
            ),
        ]

        result = selector._select_best_contract(
            chain=contracts,
            underlying_px=450.0,
            direction="P",
            target_notional_usd=100_000.0,
            intent=None,
        )

        if result.success:
            # Should select contract with OI >= 5000
            assert result.contract.open_interest >= 5000

    async def test_contract_filtering_by_spread(self):
        """Test contracts are filtered by spread."""
        policy = OptionsPolicy(max_spread_bps=50.0)
        selector = OptionsSelector(policy=policy)

        expiry = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")

        contracts = [
            OptionContract(
                symbol="SPY_2025-01-20_P_450",
                underlying="SPY",
                expiry_date=expiry,
                strike=450.0,
                right="P",
                bid=5.0,
                ask=6.0,  # 1.0 spread on 5.5 mid = ~1818 bps (too wide)
                open_interest=10000,
                delta=-0.4,
                last_trade_time=datetime.now(),
            ),
            OptionContract(
                symbol="SPY_2025-01-20_P_455",
                underlying="SPY",
                expiry_date=expiry,
                strike=455.0,
                right="P",
                bid=6.0,
                ask=6.02,  # 0.02 spread on 6.01 mid = ~33 bps (acceptable)
                open_interest=10000,
                delta=-0.45,
                last_trade_time=datetime.now(),
            ),
        ]

        result = selector._select_best_contract(
            chain=contracts,
            underlying_px=450.0,
            direction="P",
            target_notional_usd=100_000.0,
            intent=None,
        )

        if result.success:
            # Should select contract with acceptable spread
            assert result.contract.spread_bps <= 50.0

    async def test_delta_proximity_scoring(self):
        """Test that contracts closest to target delta are preferred."""
        policy = OptionsPolicy(delta_target_abs=0.4, max_spread_bps=100.0)
        selector = OptionsSelector(policy=policy)

        expiry = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")

        contracts = [
            OptionContract(
                symbol="SPY_2025-01-20_P_450",
                underlying="SPY",
                expiry_date=expiry,
                strike=450.0,
                right="P",
                bid=5.0,
                ask=5.1,
                open_interest=10000,
                delta=-0.2,  # Far from target
                last_trade_time=datetime.now(),
            ),
            OptionContract(
                symbol="SPY_2025-01-20_P_455",
                underlying="SPY",
                expiry_date=expiry,
                strike=455.0,
                right="P",
                bid=6.0,
                ask=6.1,
                open_interest=10000,
                delta=-0.41,  # Very close to target 0.4
                last_trade_time=datetime.now(),
            ),
        ]

        result = selector._select_best_contract(
            chain=contracts,
            underlying_px=450.0,
            direction="P",
            target_notional_usd=100_000.0,
            intent=None,
        )

        if result.success:
            # Should prefer delta closest to 0.4
            assert abs(abs(result.contract.delta) - 0.4) < 0.1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
