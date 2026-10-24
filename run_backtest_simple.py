#!/usr/bin/env python3
"""
Simple Backtest Runner for TSLA V7.2 Strategy

Usage:
    python run_backtest_simple.py --symbol TSLA --days 30
    python run_backtest_simple.py --symbol NVDA --start 2025-04-01 --end 2025-10-01
"""
import argparse
import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
import pandas as pd

# Use the newer backtest engine
from services.backtest.backtest_engine import BacktestEngine, BacktestConfig
from llm_trader.llm_trader.ny_time import now_ny
from tools.training_gates import enforce_training_gates
from libs.db.ch_pool import get_client


def main():
    parser = argparse.ArgumentParser(description="Run backtest for V7.2 strategy")
    parser.add_argument("--symbol", type=str, required=True, help="Symbol to backtest (e.g., TSLA)")
    parser.add_argument("--start", type=str, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, help="End date (YYYY-MM-DD)")
    parser.add_argument("--days", type=int, help="Number of days to backtest (from today backwards)")
    parser.add_argument("--config", type=str, help="Path to strategy config JSON file")
    parser.add_argument("--cash", type=float, default=100000.0, help="Starting cash (default: 100000)")
    parser.add_argument("--commission", type=float, default=0.0035, help="Commission per share (default: 0.0035)")
    parser.add_argument("--slippage-bps", type=float, default=1.0, help="Slippage in bps (default: 1.0)")
    parser.add_argument("--output", type=str, help="Output JSON file for results")

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

    print(f"="*80)
    print(f"Running Backtest for {args.symbol}")
    print(f"="*80)
    print(f"Date Range: {start_date} to {end_date}")
    print(f"Starting Cash: ${args.cash:,.2f}")
    print(f"Commission: ${args.commission} per share")
    print(f"Slippage: {args.slippage_bps} bps")
    print(f"="*80)

    # Data/label gates with auto-heal to match nightly flows
    try:
        window_days = max(1, (datetime.fromisoformat(end_date) - datetime.fromisoformat(start_date)).days)
    except Exception:
        window_days = args.days or 30
    gate = enforce_training_gates(
        symbols=[args.symbol],
        horizons=[30],
        window_days=window_days,
        features_table="trading.features_1m",
        labels_table="trading.features_1m_with_labels",
        data_quality_days=3,
        auto_heal=True,
        heal_lookback_days=2,
    )
    if not gate.get("ready"):
        print(f"[backtest] abort: gates blocked {gate}")
        raise SystemExit(1)

    def load_features_from_clickhouse(symbol: str, start: str, end: str) -> pd.DataFrame:
        """Load 1m features for the symbol and window; backtest engine needs DataFrame."""
        q = """
        SELECT
            ts,
            symbol,
            open,
            high,
            low,
            close,
            vwap,
            volume,
            trades,
            ema_10,
            ema_20,
            ema_50,
            atr_5,
            atr_14,
            rsi_14,
            bb_up_20,
            bb_dn_20,
            bb_mid_20,
            z_price_20,
            z_vol_20,
            vol_ema_20,
            vol_spike,
            slope_20,
            r2_20,
            spread_bps
        FROM trading.features_1m
        WHERE symbol = %(sym)s
          AND ts >= %(start)s
          AND ts <= %(end)s
        ORDER BY ts
        """
        try:
            ch = get_client()
            rows = ch.query(
                q,
                {
                    "sym": symbol,
                    "start": f"{start} 09:30:00",
                    "end": f"{end} 16:00:00",
                },
                settings={"use_query_cache": False},
            )
            return pd.DataFrame(rows)
        except Exception as e:
            print(f"[backtest] failed to load features: {e}")
            return pd.DataFrame()

    # Load strategy config if provided
    if args.config:
        config_path = Path(args.config)
    else:
        # Auto-detect config based on symbol
        config_path = Path(f"strategies/configs/{args.symbol.lower()}_v7_2.json")

    strategy_params = {}
    if config_path.exists():
        print(f"Loading strategy config from: {config_path}")
        with open(config_path, 'r') as f:
            strategy_params = json.load(f)
        # Override symbol in config
        strategy_params["symbol"] = args.symbol
    else:
        print(f"Warning: Config file not found at {config_path}, using minimal params")
        strategy_params = {"symbol": args.symbol}

    df = load_features_from_clickhouse(args.symbol, start_date, end_date)
    if df.empty:
        print(f"[backtest] abort: no features data found for {args.symbol} in range {start_date} to {end_date}")
        raise SystemExit(1)

    # Create backtest configuration
    config = BacktestConfig(
        symbols=[args.symbol],
        start=f"{start_date} 09:30:00",
        end=f"{end_date} 16:00:00",
        data=df,  # Pass DataFrame directly to engine
        strategy_path="strategies/tsla_v7_2.py",
        strategy_class="TSLASimplifiedV7_2",
        strategy_params=strategy_params,
        starting_cash=args.cash,
        commission_per_share=args.commission,
        slippage_bps=args.slippage_bps,
        fill_ref="open",
    )

    print()
    print("Note: features are loaded from ClickHouse table trading.features_1m for the requested window.")
    print("Ensure the table is populated; otherwise the backtest will abort.")
    print()

    try:
        # Create and run backtest engine
        engine = BacktestEngine(config)

        print("Starting backtest...")
        result = asyncio.run(engine.run())

        # Display results
        print()
        print("="*80)
        print("BACKTEST RESULTS")
        print("="*80)

        metrics = result.get("metrics", {})
        print(f"Final NAV: ${metrics.get('final_nav', 0):,.2f}")
        print(f"Cash: ${metrics.get('cash', 0):,.2f}")
        print(f"Total Fees: ${metrics.get('total_fee', 0):,.2f}")
        print(f"Realized P&L: ${metrics.get('realized_pnl', 0):,.2f}")
        print(f"Total Trades: {metrics.get('trades', 0)}")
        print(f"Win Rate: {metrics.get('win_rate', 0):.2%}")
        print(f"Profit Factor: {metrics.get('profit_factor', 0):.2f}")
        print(f"Bars Processed: {result.get('bars_processed', 0)}")

        print("="*80)

        # Save results if output file specified
        if args.output:
            output_path = Path(args.output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, 'w') as f:
                json.dump(result, f, indent=2, default=str)
            print(f"Results saved to: {output_path}")

        # Print ledger summary
        ledger = result.get("ledger", [])
        if ledger:
            print()
            print("Recent Trades (last 10):")
            print(f"{'Time':<20} {'Symbol':<8} {'Side':<6} {'Qty':<8} {'Price':<10} {'P&L':<10}")
            print("-"*80)
            for trade in ledger[-10:]:
                print(f"{str(trade.get('ts', '')):<20} "
                      f"{trade.get('symbol', ''):<8} "
                      f"{trade.get('side', ''):<6} "
                      f"{trade.get('qty', 0):<8.0f} "
                      f"${trade.get('price', 0):<9.2f} "
                      f"${trade.get('realized_pnl', 0):>9.2f}")

        return 0

    except Exception as e:
        print()
        print(f"❌ Error running backtest: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    exit(main())
