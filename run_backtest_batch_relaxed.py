#!/usr/bin/env python3
"""
Batch Backtest Runner with Relaxed Parameters
"""
import argparse
import json
from datetime import timedelta
from pathlib import Path
from typing import Dict

import pandas as pd

from backtest_ml_engine import BacktestEngine, BacktestConfig
from llm_trader.llm_trader.ny_time import now_ny
from libs.logger import get_logger
from libs.db.ch_pool import get_client

logger = get_logger(__name__)


class BatchBacktester:
    """Run backtests with relaxed parameters."""

    def __init__(self, start_date: str, end_date: str, starting_cash: float = 100000.0):
        self.start_date = start_date
        self.end_date = end_date
        self.starting_cash = starting_cash
        self.results = {}
        self.ch = get_client()  # Use global connection pool

    def load_strategy_configs(self) -> Dict[str, dict]:
        """Load all relaxed strategy configs."""
        configs = {}
        config_dir = Path("strategies/configs")

        for config_file in config_dir.glob("*_v7_2_relaxed.json"):
            symbol = config_file.stem.replace("_v7_2_relaxed", "").upper()

            with open(config_file, 'r') as f:
                config_data = json.load(f)
                configs[symbol] = config_data

        logger.info("Relaxed strategy configs loaded", count=len(configs), symbols=list(configs.keys()))
        return configs

    def load_features_from_clickhouse(self, symbol: str) -> pd.DataFrame:
        """Load features for a symbol from ClickHouse."""
        query = f"""
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
            FROM features_1m
            WHERE symbol = '{symbol}'
              AND ts >= '{self.start_date} 09:30:00'
              AND ts <= '{self.end_date} 16:00:00'
            ORDER BY ts
        """

        logger.info("Loading features from ClickHouse", symbol=symbol)

        try:
            result = self.ch.query(query)
            df = pd.DataFrame(result)

            if df.empty:
                logger.warning("No features found", symbol=symbol)
                return pd.DataFrame()

            logger.info("Features loaded", symbol=symbol, rows=len(df))
            return df

        except Exception as e:
            logger.error("Failed to load features", symbol=symbol, error=str(e))
            return pd.DataFrame()

    def run_backtest(self, symbol: str, strategy_params: dict) -> dict:
        """Run backtest for a single symbol."""
        logger.info("Starting backtest", symbol=symbol, start=self.start_date, end=self.end_date)

        # Load features from ClickHouse
        df = self.load_features_from_clickhouse(symbol)
        if df.empty:
            return {
                "symbol": symbol,
                "success": False,
                "error": "No features data available",
                "final_nav": 0,
                "realized_pnl": 0,
                "trades": 0,
            }

        # Create backtest configuration
        # Wrap strategy params in the expected key for the strategy
        wrapped_params = {"tsla_v7_2_config": strategy_params}

        config = BacktestConfig(
            symbols=[symbol],
            start=f"{self.start_date} 09:30:00",
            end=f"{self.end_date} 16:00:00",
            timeframe="1m",
            features_table="trading.features_1m",
            strategy_path="strategies.tsla_v7_2.TSLASimplifiedV7_2",
            strategy_params=wrapped_params,
            initial_cash=self.starting_cash,
            commission_per_share=strategy_params.get("commission_per_share", 0.0035),
            commission_min=1.0,
            slippage_bps=1.0,
            fill_ref="open",
        )

        try:
            engine = BacktestEngine(config)
            result = engine.run(df=df)

            # Extract key metrics
            metrics = result.get("metrics", {})
            final_snapshot = result.get("final_snapshot", {})

            realized_pnl = final_snapshot.get("equity", self.starting_cash) - self.starting_cash

            summary = {
                "symbol": symbol,
                "success": True,
                "final_nav": final_snapshot.get("equity", self.starting_cash),
                "cash": final_snapshot.get("cash", self.starting_cash),
                "realized_pnl": realized_pnl,
                "total_return": metrics.get("total_return", 0),
                "sharpe": metrics.get("sharpe", 0),
                "max_drawdown": metrics.get("max_drawdown", 0),
                "trades": metrics.get("trades", 0),
                "win_rate": metrics.get("win_rate", 0),
                "profit_factor": metrics.get("profit_factor", 0),
                "return_pct": metrics.get("total_return", 0) * 100,
            }

            logger.info("Backtest completed", symbol=symbol,
                       pnl=summary["realized_pnl"],
                       trades=summary["trades"],
                       win_rate=summary["win_rate"])

            return summary

        except Exception as e:
            logger.error("Backtest failed", symbol=symbol, error=str(e))
            return {
                "symbol": symbol,
                "success": False,
                "error": str(e),
                "final_nav": 0,
                "realized_pnl": 0,
                "trades": 0,
            }

    def run_all(self) -> Dict[str, dict]:
        """Run backtests for all strategies."""
        configs = self.load_strategy_configs()

        logger.info("="*80)
        logger.info("Starting Batch Backtests (RELAXED PARAMETERS)")
        logger.info("="*80)
        logger.info("Configuration",
                   start=self.start_date,
                   end=self.end_date,
                   starting_cash=self.starting_cash,
                   strategies=len(configs))

        for symbol, strategy_params in configs.items():
            result = self.run_backtest(symbol, strategy_params)
            self.results[symbol] = result

        return self.results

    def print_summary(self):
        """Print summary of all backtest results."""
        print()
        print("="*100)
        print("BATCH BACKTEST RESULTS SUMMARY (RELAXED PARAMETERS)")
        print("="*100)
        print(f"Date Range: {self.start_date} to {self.end_date}")
        print(f"Starting Cash: ${self.starting_cash:,.2f}")
        print("="*100)
        print()

        print(f"{'Symbol':<8} {'Success':<10} {'Final NAV':<15} {'P&L':<15} {'Return %':<12} {'Trades':<8} {'Win Rate':<10} {'PF':<8}")
        print("-"*100)

        total_pnl = 0
        successful = 0
        failed = 0
        total_trades = 0

        for symbol in sorted(self.results.keys()):
            result = self.results[symbol]

            if result["success"]:
                successful += 1
                total_pnl += result["realized_pnl"]
                total_trades += result["trades"]

                print(f"{symbol:<8} {'✓':<10} "
                      f"${result['final_nav']:>13,.2f} "
                      f"${result['realized_pnl']:>13,.2f} "
                      f"{result['return_pct']:>10.2f}% "
                      f"{result['trades']:<8} "
                      f"{result['win_rate']:>8.2%} "
                      f"{result['profit_factor']:<8.2f}")
            else:
                failed += 1
                print(f"{symbol:<8} {'✗ FAILED':<10} {result.get('error', 'Unknown error')}")

        print("-"*100)
        print(f"\nTotal P&L: ${total_pnl:,.2f}")
        print(f"Total Trades: {total_trades}")
        print(f"Successful: {successful}/{len(self.results)}")
        print(f"Failed: {failed}/{len(self.results)}")
        print("="*100)


def main():
    parser = argparse.ArgumentParser(description="Run backtests with relaxed parameters")
    parser.add_argument("--start", type=str, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, help="End date (YYYY-MM-DD)")
    parser.add_argument("--days", type=int, help="Number of days to backtest")
    parser.add_argument("--cash", type=float, default=100000.0, help="Starting cash")
    parser.add_argument("--output", type=str, help="Output JSON file")

    args = parser.parse_args()

    if args.days:
        end_date = now_ny().strftime("%Y-%m-%d")
        start_date = (now_ny() - timedelta(days=args.days)).strftime("%Y-%m-%d")
    elif args.start and args.end:
        start_date = args.start
        end_date = args.end
    else:
        end_date = now_ny().strftime("%Y-%m-%d")
        start_date = (now_ny() - timedelta(days=30)).strftime("%Y-%m-%d")

    backtester = BatchBacktester(start_date, end_date, args.cash)
    results = backtester.run_all()
    backtester.print_summary()

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, 'w') as f:
            json.dump(results, f, indent=2, default=str)
        print(f"\nResults saved to: {output_path}")

    return 0


if __name__ == "__main__":
    exit(main())
