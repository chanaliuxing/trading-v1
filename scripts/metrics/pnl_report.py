#!/usr/bin/env python3
"""
PnL Baseline Report Generator

Aggregates trading PnL, calculates Sharpe ratios across multiple windows,
compares against SPY/QQQ benchmark, and outputs to CSV/ClickHouse.

Usage:
    python scripts/metrics/pnl_report.py --sink csv --days 252
    python scripts/metrics/pnl_report.py --sink ch --days 60
"""
import argparse
import asyncio
import os
import sys
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from zoneinfo import ZoneInfo

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from libs.db.clickhouse import ClickHouseDB
from libs.logger import get_logger

logger = get_logger(__name__)

EASTERN = ZoneInfo("America/New_York")


class PnLReportGenerator:
    """Generate PnL baseline reports with Sharpe comparison against benchmarks."""

    def __init__(self, days: int = 252, benchmark_symbol: str = "SPY"):
        self.days = days
        self.benchmark_symbol = benchmark_symbol
        self.ch_client = None

    async def initialize(self):
        """Initialize ClickHouse connection."""
        self.ch_client = ClickHouseDB()
        logger.info("pnl_report_initialized", days=self.days, benchmark=self.benchmark_symbol)

    async def fetch_trading_pnl(self) -> pd.DataFrame:
        """
        Fetch daily trading PnL from ClickHouse.

        Tries multiple sources in order:
        1. trading.pnl_daily (primary source - aggregated daily PnL)
        2. trading.fills_live (fallback - calculated from live fills)

        Returns empty DataFrame if no data found.
        """
        end_date = datetime.now(EASTERN).date()
        start_date = end_date - timedelta(days=self.days)

        # Try pnl_daily table first (primary source)
        query_pnl_daily = f"""
        SELECT
            d_local as date,
            sum(realized + unrealized_eod) as pnl,
            sum(fees) as fees,
            sum(trades) as num_trades
        FROM trading.pnl_daily
        WHERE d_local >= '{start_date}' AND d_local <= '{end_date}'
        GROUP BY d_local
        ORDER BY d_local
        """

        try:
            df = self.ch_client.query(query_pnl_daily)
            if len(df) > 0:
                logger.info("pnl_fetched", source="pnl_daily", rows=len(df))
                return pd.DataFrame(df)
        except Exception as e:
            logger.debug("pnl_daily_query_failed", error=str(e))

        # Fallback to fills_live table (for production systems with real fills)
        query_fills = f"""
        SELECT
            toDate(ts) as date,
            sum(qty * price * if(side = 'SELL', -1, 1)) as pnl,
            0 as fees,
            count(*) as num_trades
        FROM trading.fills_live
        WHERE date >= '{start_date}' AND date <= '{end_date}'
        GROUP BY date
        ORDER BY date
        """

        try:
            df = self.ch_client.query(query_fills)
            if len(df) > 0:
                logger.info("pnl_fetched", source="fills_live", rows=len(df))
                return pd.DataFrame(df)
        except Exception as e:
            logger.debug("fills_live_not_available", error=str(e))

        # Final fallback: empty dataframe
        logger.warning("no_pnl_data_found", days=self.days)
        return pd.DataFrame(columns=["date", "pnl", "fees", "num_trades"])

    async def fetch_benchmark_returns(self) -> pd.DataFrame:
        """Fetch benchmark (SPY) daily returns from ClickHouse."""
        end_date = datetime.now(EASTERN).date()
        start_date = end_date - timedelta(days=self.days + 1)  # Extra day for return calculation

        query = f"""
        SELECT
            toDate(ts_end) as date,
            argMax(close, ts_end) as close
        FROM trading.bars_1d_ibkr
        WHERE symbol = '{self.benchmark_symbol}'
          AND toDate(ts_end) >= '{start_date}' AND toDate(ts_end) <= '{end_date}'
        GROUP BY date
        ORDER BY date
        """

        try:
            result = self.ch_client.query(query)
            df = pd.DataFrame(result) if isinstance(result, list) else result

            if len(df) > 0:
                # Calculate returns
                df["benchmark_ret"] = df["close"].pct_change()
                df = df[["date", "benchmark_ret"]].dropna()
                logger.info("benchmark_fetched", symbol=self.benchmark_symbol, rows=len(df))
                return df
            else:
                logger.warning("no_benchmark_data", symbol=self.benchmark_symbol)
                return pd.DataFrame(columns=["date", "benchmark_ret"])
        except Exception as e:
            logger.error("benchmark_fetch_failed", symbol=self.benchmark_symbol, error=str(e))
            return pd.DataFrame(columns=["date", "benchmark_ret"])

    def calculate_sharpe(self, returns: pd.Series, window: int, annualize_factor: float = 252) -> float:
        """
        Calculate Sharpe ratio for a returns series.

        Args:
            returns: Daily returns series
            window: Number of days to use (uses last N days)
            annualize_factor: Scaling factor (252 for daily data)
        """
        if len(returns) < window:
            return np.nan

        recent_returns = returns.tail(window)
        mean_ret = recent_returns.mean()
        std_ret = recent_returns.std()

        if std_ret == 0 or np.isnan(std_ret):
            return np.nan

        sharpe = (mean_ret / std_ret) * np.sqrt(annualize_factor)
        return sharpe

    def calculate_expectancy(self, wins: pd.Series, losses: pd.Series) -> float:
        """Calculate expectancy (EV) per trade."""
        num_wins = len(wins[wins > 0])
        num_losses = len(losses[losses < 0])
        total_trades = num_wins + num_losses

        if total_trades == 0:
            return 0.0

        win_rate = num_wins / total_trades
        avg_win = wins[wins > 0].mean() if num_wins > 0 else 0
        avg_loss = abs(losses[losses < 0].mean()) if num_losses > 0 else 0

        ev = (win_rate * avg_win) - ((1 - win_rate) * avg_loss)
        return ev

    async def generate_report(self) -> pd.DataFrame:
        """Generate comprehensive PnL baseline report."""
        # Fetch data
        pnl_df = await self.fetch_trading_pnl()
        bench_df = await self.fetch_benchmark_returns()

        if len(pnl_df) == 0:
            logger.warning("no_pnl_data_for_report")
            return pd.DataFrame()

        # Merge benchmark
        df = pnl_df.merge(bench_df, on="date", how="left")
        df["benchmark_ret"] = df["benchmark_ret"].fillna(0)

        # Calculate cumulative PnL
        df["pnl_cum"] = df["pnl"].cumsum()

        # Calculate returns (assuming some starting capital - use cumulative PnL as proxy)
        # More accurate: use actual capital from positions table
        capital = 100000  # Default starting capital
        df["ret"] = df["pnl"] / capital

        # Calculate win/loss stats
        df["wins"] = (df["pnl"] > 0).astype(int)
        df["losses"] = (df["pnl"] < 0).astype(int)

        # Calculate rolling Sharpe ratios (20/60/252 days)
        sharpe_windows = [20, 60, 252]
        for window in sharpe_windows:
            df[f"sharpe_{window}"] = np.nan
            df[f"benchmark_sharpe_{window}"] = np.nan
            df[f"edge_sharpe_{window}"] = np.nan

            # Calculate for each row (expanding window up to max size)
            for idx in range(len(df)):
                if idx + 1 >= min(window, 20):  # Minimum 20 days for meaningful Sharpe
                    start_idx = max(0, idx + 1 - window)

                    # Strategy Sharpe
                    strategy_sharpe = self.calculate_sharpe(
                        df["ret"].iloc[start_idx:idx+1],
                        min(window, idx + 1)
                    )
                    df.loc[idx, f"sharpe_{window}"] = strategy_sharpe

                    # Benchmark Sharpe
                    bench_sharpe = self.calculate_sharpe(
                        df["benchmark_ret"].iloc[start_idx:idx+1],
                        min(window, idx + 1)
                    )
                    df.loc[idx, f"benchmark_sharpe_{window}"] = bench_sharpe

                    # Edge (difference)
                    if not np.isnan(strategy_sharpe) and not np.isnan(bench_sharpe):
                        df.loc[idx, f"edge_sharpe_{window}"] = strategy_sharpe - bench_sharpe

        # Calculate edge returns (strategy vs benchmark)
        df["edge_ret"] = df["ret"] - df["benchmark_ret"]

        # Calculate cumulative win/loss counts
        df["wins_cum"] = df["wins"].cumsum()
        df["losses_cum"] = df["losses"].cumsum()

        logger.info("report_generated", rows=len(df), days=self.days)
        return df

    async def save_to_csv(self, df: pd.DataFrame, output_path: str = "reports/pnl_baseline_daily.csv"):
        """Save report to CSV file."""
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(output_path, index=False)
        logger.info("report_saved_csv", path=output_path, rows=len(df))

    async def save_to_clickhouse(self, df: pd.DataFrame):
        """Save report to ClickHouse trading.pnl_baseline table."""
        if len(df) == 0:
            logger.warning("no_data_to_save_ch")
            return

        # Ensure table exists
        create_table_sql = """
        CREATE TABLE IF NOT EXISTS trading.pnl_baseline (
            date Date,
            pnl Float64,
            pnl_cum Float64,
            ret Float64,
            sharpe_20 Float64,
            sharpe_60 Float64,
            sharpe_252 Float64,
            benchmark_ret Float64,
            benchmark_sharpe_20 Float64,
            benchmark_sharpe_60 Float64,
            benchmark_sharpe_252 Float64,
            edge_ret Float64,
            edge_sharpe_20 Float64,
            edge_sharpe_60 Float64,
            edge_sharpe_252 Float64,
            wins UInt32,
            losses UInt32,
            wins_cum UInt32,
            losses_cum UInt32,
            fees Float64,
            num_trades UInt32
        ) ENGINE = ReplacingMergeTree()
        ORDER BY date
        """

        # Table should already exist from create_pnl_tables.sql
        # If not, run: docker exec trading_clickhouse clickhouse-client --multiquery < scripts/metrics/create_pnl_tables.sql
        logger.debug("assuming_table_exists", table="trading.pnl_baseline")

        # Prepare data for insertion
        insert_df = df[[
            "date", "pnl", "pnl_cum", "ret",
            "sharpe_20", "sharpe_60", "sharpe_252",
            "benchmark_ret",
            "benchmark_sharpe_20", "benchmark_sharpe_60", "benchmark_sharpe_252",
            "edge_ret",
            "edge_sharpe_20", "edge_sharpe_60", "edge_sharpe_252",
            "wins", "losses", "wins_cum", "losses_cum",
            "fees", "num_trades"
        ]].copy()

        # Replace NaN with 0 for numeric columns
        numeric_cols = insert_df.select_dtypes(include=[np.number]).columns
        insert_df[numeric_cols] = insert_df[numeric_cols].fillna(0)

        # Insert data
        self.ch_client.insert_df("trading.pnl_baseline", insert_df)
        logger.info("report_saved_clickhouse", table="trading.pnl_baseline", rows=len(insert_df))


    async def _save_to_jsonl_for_health_snapshot(self, df: pd.DataFrame):
        """
        Save daily PnL data to pnl.jsonl for consumption by health_snapshot.py.
        """
        if len(df) == 0:
            logger.warning("no_data_to_save_jsonl", reason="empty_dataframe")
            return

        today_dir = Path("runs/ai_trader") / datetime.now().strftime("%Y-%m-%d")
        today_dir.mkdir(parents=True, exist_ok=True)
        output_path = today_dir / "pnl.jsonl"

        records = []
        for _, row in df.iterrows():
            record = {"return": row["ret"]}
            if "benchmark_ret" in row and pd.notna(row["benchmark_ret"]):
                record["bench_return"] = row["benchmark_ret"]
            records.append(json.dumps(record))

        with open(output_path, "w") as f:
            f.write("\n".join(records))

        logger.info("pnl_jsonl_saved", path=str(output_path), rows=len(records))


async def main():
    parser = argparse.ArgumentParser(description="Generate PnL baseline report")
    parser.add_argument("--days", type=int, default=252, help="Number of days to analyze")
    parser.add_argument("--benchmark", type=str, default="SPY", help="Benchmark symbol (SPY/QQQ)")
    parser.add_argument("--sink", type=str, choices=["csv", "ch", "both"], default="csv",
                       help="Output sink: csv, ch (ClickHouse), or both")
    parser.add_argument("--output", type=str, default="reports/pnl_baseline_daily.csv",
                       help="CSV output path")

    args = parser.parse_args()

    # Initialize generator
    generator = PnLReportGenerator(days=args.days, benchmark_symbol=args.benchmark)
    await generator.initialize()

    # Generate report
    df = await generator.generate_report()

    if len(df) == 0:
        logger.error("no_data_generated")
        sys.exit(1)

    # Save to sinks
    if args.sink in ["csv", "both"]:
        await generator.save_to_csv(df, args.output)

    if args.sink in ["ch", "both"]:
        await generator.save_to_clickhouse(df)

    # Save pnl.jsonl for health_snapshot.py
    await generator._save_to_jsonl_for_health_snapshot(df)

    # Print summary
    latest = df.iloc[-1]
    print("\n" + "="*80)
    print("PNL BASELINE REPORT SUMMARY")
    print("="*80)
    print(f"Period: {df['date'].min()} to {df['date'].max()} ({len(df)} days)")
    print(f"Total PnL: ${latest['pnl_cum']:.2f}")
    print(f"Total Trades: {latest['num_trades']}")
    print(f"Win Rate: {latest['wins_cum']/(latest['wins_cum']+latest['losses_cum'])*100:.1f}%")
    print(f"\nLatest Sharpe Ratios:")
    print(f"  20-day:  Strategy={latest['sharpe_20']:.3f}  Benchmark={latest['benchmark_sharpe_20']:.3f}  Edge={latest['edge_sharpe_20']:.3f}")
    print(f"  60-day:  Strategy={latest['sharpe_60']:.3f}  Benchmark={latest['benchmark_sharpe_60']:.3f}  Edge={latest['edge_sharpe_60']:.3f}")
    print(f"  252-day: Strategy={latest['sharpe_252']:.3f}  Benchmark={latest['benchmark_sharpe_252']:.3f}  Edge={latest['edge_sharpe_252']:.3f}")
    print("="*80 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
