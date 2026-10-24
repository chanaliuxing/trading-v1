#!/usr/bin/env python3
"""
Quick test script for HedgingService calculation logic.

Tests:
1. SPY trend score (ATR-based)
2. VIX percentile and carry
3. Target hedge ratio calculation
"""

import sys
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from services.risk.hedging_service import HedgingService
from libs.logger import get_logger

logger = get_logger(__name__)


async def test_trend_calculation():
    """Test SPY trend score calculation."""
    logger.info("=" * 60)
    logger.info("Testing SPY Trend Score (ATR-based)")
    logger.info("=" * 60)

    hs = HedgingService()
    score = await hs._get_spy_trend_score_atr()

    logger.info(
        "SPY Trend Score",
        score=f"{score:.3f}",
        interpretation=(
            "Strong Uptrend" if score > 1.5
            else "Uptrend" if score > 0.5
            else "Neutral" if abs(score) <= 0.5
            else "Downtrend" if score < -0.5
            else "Strong Downtrend"
        )
    )

    return score


async def test_vix_factors():
    """Test VIX percentile and carry calculation."""
    logger.info("\n" + "=" * 60)
    logger.info("Testing VIX Factors")
    logger.info("=" * 60)

    hs = HedgingService()
    vix_pct, carry = await hs._get_vix_and_carry_factors()

    logger.info(
        "VIX Factors",
        vix_percentile=f"{vix_pct:.3f}",
        carry=f"{carry:.3f}",
        vix_interpretation=(
            "Very High" if vix_pct > 0.8
            else "High" if vix_pct > 0.6
            else "Normal" if 0.4 <= vix_pct <= 0.6
            else "Low" if vix_pct > 0.2
            else "Very Low"
        ),
        carry_interpretation=(
            "Backwardation (cheap options)" if carry > 0.1
            else "Flat" if abs(carry) <= 0.1
            else "Contango (expensive options)"
        )
    )

    return vix_pct, carry


async def test_target_ratio_calculation():
    """Test target hedge ratio calculation with different scenarios."""
    logger.info("\n" + "=" * 60)
    logger.info("Testing Target Hedge Ratio Calculation")
    logger.info("=" * 60)

    hs = HedgingService()

    # Get current market state
    trend_score = await hs._get_spy_trend_score_atr()
    vix_pct, carry = await hs._get_vix_and_carry_factors()

    # Test scenarios
    scenarios = [
        ("Low Exposure (20%)", 0.2),
        ("Medium Exposure (50%)", 0.5),
        ("High Exposure (80%)", 0.8),
        ("Max Exposure (100%)", 1.0),
    ]

    logger.info(f"\nCurrent Market State:")
    logger.info(f"  Trend Score: {trend_score:.3f}")
    logger.info(f"  VIX Percentile: {vix_pct:.3f}")
    logger.info(f"  Carry: {carry:.3f}")
    logger.info("")

    results = []
    for name, exposure_ratio in scenarios:
        ratio = hs._calc_target_ratio(
            exposure_ratio=exposure_ratio,
            trend_score=trend_score,
            vix_pct=vix_pct,
            carry=carry,
        )
        results.append((name, exposure_ratio, ratio))
        logger.info(
            f"{name:25} → Target Hedge Ratio: {ratio:.3f} "
            f"({ratio * 100:.1f}% of net delta)"
        )

    return results


async def test_portfolio_exposure():
    """Test portfolio exposure calculation (if data available)."""
    logger.info("\n" + "=" * 60)
    logger.info("Testing Portfolio Exposure Calculation")
    logger.info("=" * 60)

    hs = HedgingService()
    exposure = await hs._get_portfolio_exposure()

    net = exposure.get("net", 0.0)
    gross = exposure.get("gross", 0.0)

    if gross > 0:
        logger.info(
            "Portfolio Exposure",
            net_delta=f"${net:,.0f}",
            gross_delta=f"${gross:,.0f}",
            net_to_gross=f"{abs(net) / gross:.2%}" if gross > 0 else "N/A",
            directional_bias="Long" if net > 0 else "Short" if net < 0 else "Neutral",
        )
    else:
        logger.warning("No portfolio exposure found (strategies not running yet)")
        logger.info("This is normal if system hasn't started trading")

    return exposure


async def test_full_cycle():
    """Test a full hedging decision cycle."""
    logger.info("\n" + "=" * 60)
    logger.info("Testing Full Hedging Decision Cycle")
    logger.info("=" * 60)

    hs = HedgingService()

    # Get portfolio exposure
    exposure = await hs._get_portfolio_exposure()
    net_delta = exposure.get("net", 0.0)
    gross_delta = exposure.get("gross", 0.0)

    if gross_delta <= 0:
        logger.warning("Skipping full cycle test (no portfolio exposure)")
        logger.info("Run this after strategies start trading to see real decisions")
        return

    # Calculate exposure ratio
    exposure_ratio = min(1.0, abs(net_delta) / hs.max_net_delta)

    # Get market factors
    trend_score = await hs._get_spy_trend_score_atr()
    vix_pct, carry = await hs._get_vix_and_carry_factors()

    # Calculate target ratio
    target_ratio = hs._calc_target_ratio(
        exposure_ratio=exposure_ratio,
        trend_score=trend_score,
        vix_pct=vix_pct,
        carry=carry,
    )

    # Calculate delta gap
    target_hedge_delta = -1.0 * net_delta * target_ratio
    current_hedge_delta = await hs._get_current_hedge_delta()
    delta_gap = target_hedge_delta - current_hedge_delta

    logger.info(
        "🛡️  FULL CYCLE SIMULATION",
        net_delta=f"${net_delta:,.0f}",
        exposure_ratio=f"{exposure_ratio:.2%}",
        trend_score=f"{trend_score:.3f}",
        vix_pct=f"{vix_pct:.2%}",
        carry=f"{carry:.3f}",
        target_ratio=f"{target_ratio:.2%}",
        target_hedge_delta=f"${target_hedge_delta:,.0f}",
        current_hedge_delta=f"${current_hedge_delta:,.0f}",
        delta_gap=f"${delta_gap:,.0f}",
    )

    if abs(delta_gap) >= hs.params["min_gap_usd"]:
        logger.info(f"   ✅ Would trigger hedge adjustment (gap > ${hs.params['min_gap_usd']:,.0f})")
    else:
        logger.info(f"   ⏭️  Would skip adjustment (gap < ${hs.params['min_gap_usd']:,.0f})")


async def main():
    """Run all tests."""
    logger.info("🧪 HedgingService Logic Test Suite")
    logger.info("")

    try:
        # Test individual components
        await test_trend_calculation()
        await test_vix_factors()
        await test_target_ratio_calculation()
        await test_portfolio_exposure()

        # Test full cycle
        await test_full_cycle()

        logger.info("\n" + "=" * 60)
        logger.info("✅ All tests completed successfully!")
        logger.info("=" * 60)
        logger.info("\nNext steps:")
        logger.info("1. Review START_HEDGING_MVP.md for deployment instructions")
        logger.info("2. Enable hedging in ai_trader.yaml (set enabled: true)")
        logger.info("3. Start run_system.py and monitor logs")

    except Exception as e:
        logger.error("Test suite failed", error=str(e), exc_info=True)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
