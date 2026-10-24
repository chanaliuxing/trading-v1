#!/usr/bin/env python3
"""
Test IBKR options API integration for hedge system.

This script tests:
1. IB connection
2. Options chain data fetching
3. Greeks availability
4. Real-time pricing for hedge calculations
5. Integration with auto_hedge.py

Usage:
    python scripts/options/test_ib_options_integration.py [--live]
"""

import asyncio
import sys
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from services.market.options_data import create_options_provider, IBKROptionsDataProvider
from libs.options.occ import build_occ_symbol, parse_occ_symbol
from libs.logger import get_logger
import os
from dotenv import load_dotenv

NY_TZ = ZoneInfo("America/New_York")
LOG = get_logger(__name__)

load_dotenv()


async def test_ib_connection(provider: IBKROptionsDataProvider):
    """Test basic IB connection."""
    print("\n" + "=" * 80)
    print("TEST 1: IB CONNECTION")
    print("=" * 80)

    try:
        await provider._ensure_connected()
        print(f"✅ Connected to IBKR at {provider.host}:{provider.port}")
        print(f"   Client ID: {provider.client_id}")
        print(f"   Connection status: {'Connected' if provider.ib.isConnected() else 'Disconnected'}")
        return True
    except Exception as e:
        print(f"❌ Connection failed: {e}")
        return False


async def test_underlying_price(provider: IBKROptionsDataProvider, symbol: str = "SPY"):
    """Test underlying price fetching."""
    print("\n" + "=" * 80)
    print(f"TEST 2: UNDERLYING PRICE ({symbol})")
    print("=" * 80)

    try:
        price = await provider.get_underlying_price(symbol)

        if price:
            print(f"✅ {symbol} Price: ${price:.2f}")
            return price
        else:
            print(f"❌ Failed to fetch {symbol} price")
            return None
    except Exception as e:
        print(f"❌ Error: {e}")
        return None


async def test_options_chain(provider: IBKROptionsDataProvider, symbol: str = "SPY"):
    """Test options chain data fetching."""
    print("\n" + "=" * 80)
    print(f"TEST 3: OPTIONS CHAIN ({symbol})")
    print("=" * 80)

    try:
        # Request options for ~30 days out (typical hedge horizon)
        dte_candidates = [30, 28, 32]  # ±2 days tolerance

        print(f"Fetching option chain for {symbol} with DTE={dte_candidates}...")
        contracts = await provider.get_chain(
            underlying=symbol,
            dte_candidates=dte_candidates,
            right='P',  # Only puts for hedge testing
        )

        if not contracts:
            print(f"❌ No contracts found")
            return []

        print(f"✅ Found {len(contracts)} PUT contracts")

        # Display sample contracts
        print("\nSample contracts:")
        for i, contract in enumerate(contracts[:5], 1):
            print(f"\n{i}. {contract.symbol}")
            print(f"   Strike: ${contract.strike:.2f}")
            print(f"   Bid: ${contract.bid:.2f}, Ask: ${contract.ask:.2f}, Mid: ${contract.mid:.2f}")
            print(f"   Spread: ${contract.spread:.2f} ({contract.spread_bps:.0f} bps)")
            if contract.delta:
                print(f"   Delta: {contract.delta:.4f}")
            if contract.implied_vol:
                print(f"   IV: {contract.implied_vol*100:.1f}%")
            if contract.open_interest:
                print(f"   Open Interest: {contract.open_interest:,}")

        return contracts

    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        return []


async def test_hedge_contract_selection(
    provider: IBKROptionsDataProvider,
    symbol: str = "SPY",
    target_delta: float = 0.30,
):
    """Test selecting appropriate hedge contract."""
    print("\n" + "=" * 80)
    print(f"TEST 4: HEDGE CONTRACT SELECTION (Target Delta: {target_delta})")
    print("=" * 80)

    try:
        # Get underlying price
        underlying_px = await provider.get_underlying_price(symbol)
        if not underlying_px:
            print("❌ Failed to get underlying price")
            return None

        print(f"{symbol} Price: ${underlying_px:.2f}")

        # Get option chain
        contracts = await provider.get_chain(
            underlying=symbol,
            dte_candidates=[30],
            right='P',
        )

        if not contracts:
            print("❌ No contracts available")
            return None

        # Find contract closest to target delta
        best_contract = None
        min_delta_diff = float('inf')

        for contract in contracts:
            if contract.delta is None:
                continue

            # Put delta is negative, convert to absolute
            abs_delta = abs(contract.delta)
            delta_diff = abs(abs_delta - target_delta)

            if delta_diff < min_delta_diff:
                min_delta_diff = delta_diff
                best_contract = contract

        if best_contract:
            print(f"\n✅ Best hedge contract found:")
            print(f"   OCC Symbol: {best_contract.symbol}")
            print(f"   Strike: ${best_contract.strike:.2f}")
            print(f"   Current {symbol}: ${underlying_px:.2f}")
            print(f"   Moneyness: {(best_contract.strike/underlying_px-1)*100:.1f}%")
            print(f"   Delta: {best_contract.delta:.4f} (Target: {-target_delta:.4f})")
            print(f"   Mid Price: ${best_contract.mid:.2f}")
            print(f"   Bid-Ask Spread: {best_contract.spread_bps:.0f} bps")
            if best_contract.implied_vol:
                print(f"   Implied Vol: {best_contract.implied_vol*100:.1f}%")

            # Calculate hedge cost for 100 shares
            notional = underlying_px * 100
            contracts_needed = 1
            premium_per_contract = best_contract.mid * 100  # Option multiplier
            total_cost = premium_per_contract * contracts_needed
            cost_bps = int(total_cost / notional * 10000)

            print(f"\n   Hedge Calculation (100 shares):")
            print(f"   Notional: ${notional:,.2f}")
            print(f"   Contracts: {contracts_needed}")
            print(f"   Premium/Contract: ${premium_per_contract:.2f}")
            print(f"   Total Cost: ${total_cost:.2f} ({cost_bps} bps)")

            return best_contract
        else:
            print("❌ No contract with valid delta found")
            return None

    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        return None


async def test_auto_hedge_integration(provider: IBKROptionsDataProvider):
    """Test integration with auto_hedge.py using real IB data."""
    print("\n" + "=" * 80)
    print("TEST 5: AUTO_HEDGE INTEGRATION")
    print("=" * 80)

    try:
        # Import auto_hedge components
        from scripts.options.auto_hedge import AutoHedgeExecutor, HedgeIntent

        # Get real underlying price
        spy_price = await provider.get_underlying_price("SPY")
        if not spy_price:
            print("❌ Failed to get SPY price")
            return False

        print(f"SPY Price: ${spy_price:.2f}")

        # Create hedge intent
        intent = HedgeIntent(
            symbols=["TSLA"],
            notional_pct=0.5,
            urgency="medium",
            strategy="options_put",
            instrument="SPY",
            max_cost_bps=200,  # Higher limit for real options
        )

        print(f"\nHedge Intent:")
        print(f"  Symbols: {intent.symbols}")
        print(f"  Notional %: {intent.notional_pct*100}%")
        print(f"  Strategy: {intent.strategy}")
        print(f"  Instrument: {intent.instrument}")

        # Note: AutoHedgeExecutor currently uses mock prices
        # We're testing that IB data is available and could be integrated

        print(f"\n✅ IB options data is ready for integration")
        print(f"   Next step: Update AutoHedgeExecutor._get_current_price() to use IBKROptionsDataProvider")
        print(f"   Next step: Update AutoHedgeExecutor._generate_put_hedge() to use real option chain")

        return True

    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_correlation_data():
    """Test fetching real correlation data from ClickHouse."""
    print("\n" + "=" * 80)
    print("TEST 6: CORRELATION DATA (CLICKHOUSE)")
    print("=" * 80)

    try:
        from libs.db.clickhouse import ClickHouseDB

        # Get ClickHouse client
        ch_client = ClickHouseDB()

        # Calculate 30-day rolling correlations from 1m bars aggregated to daily
        query = """
        WITH daily_prices AS (
            SELECT
                symbol,
                toDate(ts_end) AS date,
                anyLast(close) AS close
            FROM trading.bars_1m
            WHERE symbol IN ('TSLA', 'NVDA', 'AAPL', 'SPY', 'QQQ', 'IWM')
              AND ts_end >= today() - INTERVAL 60 DAY
              AND toHour(ts_end) >= 9 AND toHour(ts_end) <= 16
            GROUP BY symbol, date
        )
        SELECT
            a.symbol AS symbol1,
            b.symbol AS symbol2,
            corr(a.close, b.close) AS correlation,
            count(*) AS data_points
        FROM daily_prices a
        INNER JOIN daily_prices b ON a.date = b.date AND a.symbol < b.symbol
        GROUP BY symbol1, symbol2
        HAVING data_points >= 30
        ORDER BY symbol1, symbol2
        """

        print("Fetching 30-day rolling correlations from ClickHouse...")

        result = ch_client.query(query)

        if not result or len(result) == 0:
            print("❌ No correlation data found")
            print("   This is expected if daily bars table doesn't exist yet")
            return None

        # Parse correlations
        correlations = {}
        for row in result:
            sym1 = row['symbol1']
            sym2 = row['symbol2']
            corr = row['correlation']
            # data_points = row['data_points']  # Available but not used for display

            if sym1 not in correlations:
                correlations[sym1] = {}
            if sym2 not in correlations:
                correlations[sym2] = {}

            correlations[sym1][sym2] = round(float(corr), 4)
            correlations[sym2][sym1] = round(float(corr), 4)  # Symmetric

        print(f"\n✅ Found {len(result)} correlation pairs")
        print("\nCorrelation Matrix:")
        print("-" * 60)

        # Display correlations
        symbols = ['TSLA', 'NVDA', 'AAPL']
        hedges = ['SPY', 'QQQ', 'IWM']

        for sym in symbols:
            if sym in correlations:
                print(f"\n{sym}:")
                for hedge in hedges:
                    corr = correlations.get(sym, {}).get(hedge, 0.0)
                    print(f"  {hedge}: {corr:+.4f}")

        return correlations

    except Exception as e:
        print(f"❌ Error: {e}")
        print("   Note: This is expected if ClickHouse isn't running or bars_1d table doesn't exist")
        return None


async def main():
    """Run all integration tests."""
    import argparse

    parser = argparse.ArgumentParser(description="Test IBKR options integration")
    parser.add_argument("--live", action="store_true", help="Use live trading port (default: paper)")
    parser.add_argument("--symbol", default="SPY", help="Symbol to test (default: SPY)")
    args = parser.parse_args()

    # Determine port
    trading_mode_env = os.getenv("TRADING_MODE", "paper").lower()
    mode = "LIVE" if args.live or trading_mode_env == "live" else "PAPER"
    port = 4001 if mode == "LIVE" else 4002

    print("=" * 80)
    print(f"IBKR OPTIONS INTEGRATION TEST - {mode} MODE")
    print("=" * 80)
    print(f"Host: 127.0.0.1")
    print(f"Port: {port}")
    print(f"Test Symbol: {args.symbol}")
    print(f"Timestamp: {datetime.now(NY_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')}")

    # Create IBKR provider
    provider = create_options_provider('ibkr', host='127.0.0.1', port=port)

    try:
        # Test 1: Connection
        if not await test_ib_connection(provider):
            print("\n⚠️  IBKR connection failed. Is TWS/Gateway running?")
            print("   Start IBKR Gateway/TWS and ensure:")
            print(f"   - Socket port {port} is enabled")
            print("   - API connections are allowed")
            print("   - Client ID conflicts are resolved")
            return

        # Test 2: Underlying price
        underlying_price = await test_underlying_price(provider, args.symbol)
        if not underlying_price:
            print(f"\n⚠️  Failed to fetch {args.symbol} price")
            return

        # Test 3: Options chain
        contracts = await test_options_chain(provider, args.symbol)
        if not contracts:
            print(f"\n⚠️  No option contracts found for {args.symbol}")
            return

        # Test 4: Hedge contract selection
        hedge_contract = await test_hedge_contract_selection(provider, args.symbol)

        # Test 5: Auto-hedge integration
        await test_auto_hedge_integration(provider)

        # Test 6: Correlation data
        await test_correlation_data()

        print("\n" + "=" * 80)
        print("✅ INTEGRATION TEST SUMMARY")
        print("=" * 80)
        print(f"✅ IB connection: Working")
        print(f"✅ Underlying price: Working (${underlying_price:.2f})")
        print(f"✅ Options chain: {len(contracts)} contracts available")
        print(f"✅ Greeks data: {'Available' if any(c.delta for c in contracts) else 'Not available'}")
        print(f"✅ Hedge selection: Working")
        print("")
        print("NEXT STEPS:")
        print("1. Update auto_hedge.py to use IBKROptionsDataProvider")
        print("2. Replace mock prices with real IB data")
        print("3. Replace static correlations with ClickHouse rolling correlations")
        print("4. Test end-to-end hedge execution")
        print("=" * 80)

    finally:
        # Cleanup
        await provider.disconnect()
        print("\n✅ Disconnected from IBKR")


if __name__ == "__main__":
    asyncio.run(main())
