#!/usr/bin/env python3
"""
Backtest Runner for RevE Intraday Strategy

This script runs backtests for the RevEIntradayStrategyV2 mean-reversion strategy.
The strategy requires:
  - 1-minute features data (features_1m) for primary signal generation
  - 5-minute features data (features_5m) for ATR and additional confirmation
  - ORB (Opening Range Break) data from market open (09:30-09:45 ET)

Usage:
    # Backtest single symbol for 30 days
    python run_backtest_reve.py --symbols TSLA --days 30

    # Backtest multiple symbols with date range
    python run_backtest_reve.py --symbols TSLA NVDA AAPL --start 2025-04-01 --end 2025-10-01

    # Backtest all 7 symbols with custom capital
    python run_backtest_reve.py --symbols TSLA NVDA AAPL AMD META QQQ SPY --days 90 --cash 200000

    # Save results to file
    python run_backtest_reve.py --symbols TSLA --days 60 --output results/reve_tsla_60d.json

Data Requirements:
    Before running this backtest, ensure you have:
    1. 1-minute bars: trading.bars_1m_ibkr
    2. 5-minute bars: trading.bars_5m_ibkr
    3. 1-minute features: trading.features_1m (with atr_14, ema_20, etc.)
    4. 5-minute features: trading.features_5m (with atr_14)

    To backfill data:
    python tool/load_hist_multi.py --symbols TSLA NVDA --days 180 --bar-size 1m --fill-mode backfill
    python tool/load_hist_multi.py --symbols TSLA NVDA --days 180 --bar-size 5m --fill-mode backfill

    To compute features (if needed):
    python services/market/features/feature_calculator.py --symbol TSLA --start 2025-04-01 --end 2025-10-01
"""
import argparse
import asyncio
import json
import sys
from datetime import timedelta
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from services.backtest.backtest_engine import BacktestEngine, BacktestConfig
from llm_trader.llm_trader.ny_time import now_ny
from libs.db.clickhouse import ClickHouseDB
import pandas as pd


def load_features_from_clickhouse(symbols, start_date, end_date):
    """
    Load 1-minute features data from ClickHouse for backtest.

    Returns:
        DataFrame with columns: ts, symbol, open, high, low, close, volume, vwap, atr_14, etc.
    """
    print()
    print("Loading data from ClickHouse...")
    print(f"  Table: trading.features_1m")
    print(f"  Symbols: {', '.join(symbols)}")
    print(f"  Date range: {start_date} to {end_date}")
    print()

    try:
        ch = ClickHouseDB()
    except Exception as e:
        print(f"❌ Failed to connect to ClickHouse: {e}")
        return None

    # Build symbol list for query
    symbol_list = "', '".join(symbols)

    # Query to get 1-minute features
    # Include all columns the strategy might need
    query = f"""
    SELECT
        ts,
        symbol,
        close as open,  -- Use close as open (for simplicity, could improve this)
        close as high,
        close as low,
        close,
        volume,
        vwap,
        atr_14,
        ema_20,
        rsi_14,
        z_price_20,
        z_vol_20,
        ret_1,
        ret_5,
        sma_20,
        std_20,
        bb_pct_20
    FROM trading.features_1m FINAL
    WHERE symbol IN ('{symbol_list}')
      AND ts >= '{start_date} 09:30:00'
      AND ts <= '{end_date} 16:00:00'
    ORDER BY ts, symbol
    """

    try:
        rows = ch.query(query)
        if not rows:
            print("❌ No data found in ClickHouse!")
            return None

        # Convert to DataFrame
        df = pd.DataFrame(rows)

        # Ensure ts column is datetime (if it's already datetime, this is a no-op)
        if not pd.api.types.is_datetime64_any_dtype(df['ts']):
            df['ts'] = pd.to_datetime(df['ts'])

        print(f"✓ Loaded {len(df):,} rows")
        print(f"  Date range: {df['ts'].min()} to {df['ts'].max()}")
        print(f"  Symbols: {df['symbol'].unique().tolist()}")
        print()

        return df

    except Exception as e:
        print(f"❌ Failed to load data: {e}")
        import traceback
        traceback.print_exc()
        return None


def main():
    parser = argparse.ArgumentParser(
        description="Run backtest for RevE Intraday Strategy",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )

    # Symbol selection
    parser.add_argument(
        "--symbols",
        type=str,
        nargs="+",
        required=True,
        help="Symbols to backtest (e.g., TSLA NVDA AAPL)"
    )

    # Date range options
    parser.add_argument(
        "--start",
        type=str,
        help="Start date (YYYY-MM-DD)"
    )
    parser.add_argument(
        "--end",
        type=str,
        help="End date (YYYY-MM-DD)"
    )
    parser.add_argument(
        "--days",
        type=int,
        help="Number of days to backtest (from today backwards)"
    )

    # Strategy parameters
    parser.add_argument(
        "--config",
        type=str,
        help="Path to strategy config JSON file (optional)"
    )

    # Execution parameters
    parser.add_argument(
        "--cash",
        type=float,
        default=100000.0,
        help="Starting cash (default: 100000)"
    )
    parser.add_argument(
        "--commission",
        type=float,
        default=0.0035,
        help="Commission per share (default: 0.0035)"
    )
    parser.add_argument(
        "--slippage-bps",
        type=float,
        default=1.0,
        help="Slippage in basis points (default: 1.0)"
    )

    # Output options
    parser.add_argument(
        "--output",
        type=str,
        help="Output JSON file for results"
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose logging"
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Skip confirmation prompt"
    )

    args = parser.parse_args()

    # Determine date range
    if args.days:
        end_date = now_ny().strftime("%Y-%m-%d")
        start_date = (now_ny() - timedelta(days=args.days)).strftime("%Y-%m-%d")
    elif args.start and args.end:
        start_date = args.start
        end_date = args.end
    else:
        # Default: last 30 days
        end_date = now_ny().strftime("%Y-%m-%d")
        start_date = (now_ny() - timedelta(days=30)).strftime("%Y-%m-%d")

    # Banner
    print("=" * 80)
    print("RevE Intraday Strategy Backtest")
    print("=" * 80)
    print(f"Symbols: {', '.join(args.symbols)}")
    print(f"Date Range: {start_date} to {end_date}")
    print(f"Starting Cash: ${args.cash:,.2f}")
    print(f"Commission: ${args.commission} per share")
    print(f"Slippage: {args.slippage_bps} bps")
    print("=" * 80)

    # Load strategy config if provided
    strategy_params = {}
    if args.config:
        config_path = Path(args.config)
        if config_path.exists():
            print(f"Loading strategy config from: {config_path}")
            with open(config_path, 'r') as f:
                strategy_params = json.load(f)
        else:
            print(f"Warning: Config file not found at {config_path}")

    # Build strategy params with defaults
    # These match the RevEIntradayStrategyV2 params.extras structure
    if "extras" not in strategy_params:
        strategy_params["extras"] = {}

    strategy_params["extras"]["symbols"] = args.symbols

    # Set backtest mode flag (skips live-trading specific logic like ORB backfill)
    strategy_params["extras"]["_backtest_mode"] = True

    # Set reasonable defaults if not in config
    if "debug" not in strategy_params["extras"]:
        strategy_params["extras"]["debug"] = args.verbose

    if "schedule" not in strategy_params["extras"]:
        # Default: run all day for backtest (no time restrictions)
        strategy_params["extras"]["schedule"] = {
            "start": "06:30",  # 09:30 ET
            "stop": "13:00"    # 16:00 ET
        }

    # TTL for decision intents (default 60 seconds)
    if "ttl_ms" not in strategy_params:
        strategy_params["ttl_ms"] = 60000

    print()
    print("Strategy Configuration:")
    print(f"  Symbols: {strategy_params['extras']['symbols']}")
    print(f"  Schedule: {strategy_params['extras']['schedule']['start']} - {strategy_params['extras']['schedule']['stop']} PT")
    print(f"  Debug: {strategy_params['extras']['debug']}")
    print(f"  TTL: {strategy_params['ttl_ms']}ms")
    print()

    # Load data from ClickHouse
    print()
    print("=" * 80)
    data_df = load_features_from_clickhouse(args.symbols, start_date, end_date)
    if data_df is None or len(data_df) == 0:
        print("❌ No data available for backtest!")
        print("=" * 80)
        return 1

    # Create backtest configuration
    config = BacktestConfig(
        symbols=args.symbols,
        start=f"{start_date} 09:30:00",  # Market open in ET
        end=f"{end_date} 16:00:00",      # Market close in ET
        data=data_df,  # Use ClickHouse data
        csv=None,
        strategy_path="strategies/reve_intraday_strategy.py",
        strategy_class="RevEIntradayStrategyV2",
        strategy_params=strategy_params,
        starting_cash=args.cash,
        commission_per_share=args.commission,
        slippage_bps=args.slippage_bps,
        fill_ref="close",  # Use bar close price for fills (more realistic)
    )

    # Data requirements warning
    print("⚠️  DATA REQUIREMENTS")
    print("-" * 80)
    print("This backtest requires pre-computed features in ClickHouse:")
    print()
    print("  1. trading.bars_1m_ibkr    - 1-minute OHLCV bars")
    print("  2. trading.bars_5m_ibkr    - 5-minute OHLCV bars")
    print("  3. trading.features_1m     - 1-minute features (atr_14, ema_20, vwap, etc.)")
    print("  4. trading.features_5m     - 5-minute features (atr_14)")
    print()
    print("To backfill bars (if missing):")
    for symbol in args.symbols:
        print(f"  python tool/load_hist_multi.py --symbols {symbol} --days 180 --bar-size 1m")
        print(f"  python tool/load_hist_multi.py --symbols {symbol} --days 180 --bar-size 5m")
    print()
    print("To compute features (if missing):")
    for symbol in args.symbols:
        print(f"  python services/market/features/feature_calculator.py --symbol {symbol} --start {start_date} --end {end_date}")
    print()
    print("-" * 80)
    print()

    # Confirm before proceeding
    if not args.yes:
        try:
            proceed = input("Proceed with backtest? [Y/n]: ").strip().lower()
            if proceed and proceed not in ['y', 'yes']:
                print("Backtest cancelled.")
                return 0
        except (EOFError, KeyboardInterrupt):
            print("\nBacktest cancelled.")
            return 0

    try:
        # Create and run backtest engine
        print()
        print("Initializing backtest engine...")
        engine = BacktestEngine(config)

        print("Starting backtest...")
        print()
        result = asyncio.run(engine.run())

        # Display results
        print()
        print("=" * 80)
        print("BACKTEST RESULTS")
        print("=" * 80)

        metrics = result.get("metrics", {})
        print()
        print("Performance Metrics:")
        print(f"  Final NAV:        ${metrics.get('final_nav', 0):>12,.2f}")
        print(f"  Starting Cash:    ${args.cash:>12,.2f}")
        print(f"  P&L:              ${metrics.get('final_nav', 0) - args.cash:>12,.2f}")
        print(f"  Return:           {((metrics.get('final_nav', 0) / args.cash) - 1) * 100:>12.2f}%")
        print()
        print(f"  Cash Remaining:   ${metrics.get('cash', 0):>12,.2f}")
        print(f"  Total Fees:       ${metrics.get('total_fee', 0):>12,.2f}")
        print(f"  Realized P&L:     ${metrics.get('realized_pnl', 0):>12,.2f}")
        print()
        print(f"  Total Trades:     {metrics.get('trades', 0):>12}")
        print(f"  Win Rate:         {metrics.get('win_rate', 0):>11.2%}")
        print(f"  Profit Factor:    {metrics.get('profit_factor', 0):>12.2f}")
        print(f"  Bars Processed:   {result.get('bars_processed', 0):>12,}")
        print()

        # Calculate additional metrics
        if metrics.get('trades', 0) > 0:
            avg_pnl_per_trade = metrics.get('realized_pnl', 0) / metrics.get('trades', 0)
            print(f"  Avg P&L/Trade:    ${avg_pnl_per_trade:>12,.2f}")

        print("=" * 80)

        # Save results if output file specified
        if args.output:
            output_path = Path(args.output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, 'w') as f:
                json.dump(result, f, indent=2, default=str)
            print(f"✓ Results saved to: {output_path}")
            print()

        # Print ledger summary
        ledger = result.get("ledger", [])
        if ledger:
            print()
            print("Trade History (last 20):")
            print(f"{'Time':<20} {'Symbol':<8} {'Side':<6} {'Qty':<8} {'Price':<10} {'P&L':<12}")
            print("-" * 80)
            for trade in ledger[-20:]:
                pnl = trade.get('realized_pnl', 0)
                pnl_str = f"${pnl:>10,.2f}" if pnl != 0 else ""
                print(f"{str(trade.get('ts', '')):<20} "
                      f"{trade.get('symbol', ''):<8} "
                      f"{trade.get('side', ''):<6} "
                      f"{trade.get('qty', 0):<8.0f} "
                      f"${trade.get('price', 0):<9.2f} "
                      f"{pnl_str:<12}")
            print()

        # Symbol-level breakdown if multiple symbols
        if len(args.symbols) > 1:
            print()
            print("Per-Symbol Breakdown:")
            print(f"{'Symbol':<8} {'Trades':<10} {'P&L':<15}")
            print("-" * 40)
            symbol_stats = {}
            for trade in ledger:
                symbol = trade.get('symbol', '')
                if symbol not in symbol_stats:
                    symbol_stats[symbol] = {'trades': 0, 'pnl': 0.0}
                if trade.get('side') == 'SELL':  # Count exits
                    symbol_stats[symbol]['trades'] += 1
                symbol_stats[symbol]['pnl'] += trade.get('realized_pnl', 0)

            for symbol in sorted(symbol_stats.keys()):
                stats = symbol_stats[symbol]
                print(f"{symbol:<8} {stats['trades']:<10} ${stats['pnl']:>12,.2f}")
            print()

        print("=" * 80)
        print("Backtest completed successfully!")
        print("=" * 80)
        return 0

    except Exception as e:
        print()
        print("=" * 80)
        print("❌ ERROR")
        print("=" * 80)
        print(f"Backtest failed: {e}")
        print()
        import traceback
        traceback.print_exc()
        print()
        print("=" * 80)
        return 1


if __name__ == "__main__":
    exit(main())
