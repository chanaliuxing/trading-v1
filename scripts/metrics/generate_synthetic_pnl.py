#!/usr/bin/env python3
"""
Generate synthetic PnL data for testing the Performance Guard system.

This script creates realistic-looking trading PnL data when you don't have
actual trading history yet. Useful for:
- Testing the PnL baseline system
- Testing the PerformanceGuard service
- Demonstrating the Grafana dashboard

Usage:
    # Generate 252 days of synthetic data (1 year)
    venv/bin/python scripts/metrics/generate_synthetic_pnl.py --days 252

    # Generate with specific Sharpe ratio
    venv/bin/python scripts/metrics/generate_synthetic_pnl.py --days 100 --sharpe 0.8

    # Generate losing strategy (Sharpe < 0)
    venv/bin/python scripts/metrics/generate_synthetic_pnl.py --days 60 --sharpe -0.2
"""

import asyncio
import sys
from datetime import datetime, date, timedelta
from pathlib import Path
import argparse
import numpy as np
import pandas as pd
from zoneinfo import ZoneInfo

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from libs.db.ch_pool import get_async_client
from libs.logger import get_logger

logger = get_logger(__name__)

EASTERN = ZoneInfo("America/New_York")


class SyntheticPnLGenerator:
    """Generates realistic synthetic PnL data."""

    def __init__(
        self,
        days: int = 252,
        starting_capital: float = 100000.0,
        target_sharpe: float = 0.5,
        daily_vol: float = 0.015,
        win_rate: float = 0.55,
        avg_trades_per_day: int = 10,
    ):
        self.days = days
        self.starting_capital = starting_capital
        self.target_sharpe = target_sharpe
        self.daily_vol = daily_vol
        self.win_rate = win_rate
        self.avg_trades_per_day = avg_trades_per_day

    def generate(self) -> pd.DataFrame:
        """Generate synthetic daily PnL data."""

        # Calculate target daily return to achieve desired Sharpe
        # Sharpe = mean(returns) / std(returns) * sqrt(252)
        # => mean(returns) = Sharpe * std(returns) / sqrt(252)
        target_daily_return = self.target_sharpe * self.daily_vol / np.sqrt(252)

        # Generate dates (exclude weekends)
        end_date = datetime.now(EASTERN).date()
        dates = []
        current_date = end_date - timedelta(days=self.days * 2)  # Go back further to account for weekends

        while len(dates) < self.days:
            if current_date.weekday() < 5:  # Monday = 0, Friday = 4
                dates.append(current_date)
            current_date += timedelta(days=1)

        dates = dates[-self.days:]  # Keep only requested number of days

        # Generate returns with target mean and vol
        returns = np.random.normal(
            loc=target_daily_return,
            scale=self.daily_vol,
            size=len(dates)
        )

        # Generate PnL from returns
        pnl = returns * self.starting_capital
        pnl_cum = np.cumsum(pnl)

        # Generate win/loss counts
        wins = []
        losses = []
        num_trades_list = []

        for ret in returns:
            num_trades = max(1, int(np.random.poisson(self.avg_trades_per_day)))
            num_wins = int(np.random.binomial(num_trades, self.win_rate))
            num_losses = num_trades - num_wins

            wins.append(num_wins)
            losses.append(num_losses)
            num_trades_list.append(num_trades)

        # Estimate fees (assume $0.005 per share, 100 shares per trade)
        fees = [n * 100 * 0.005 for n in num_trades_list]

        # Create DataFrame
        df = pd.DataFrame({
            "date": dates,
            "pnl": pnl,
            "pnl_cum": pnl_cum,
            "ret": returns,
            "wins": wins,
            "losses": losses,
            "fees": fees,
            "num_trades": num_trades_list,
        })

        # Add cumulative win/loss counts
        df["wins_cum"] = df["wins"].cumsum()
        df["losses_cum"] = df["losses"].cumsum()

        return df

    async def save_to_clickhouse(self, df: pd.DataFrame):
        """Save synthetic data to ClickHouse trading.pnl_daily table."""

        ch_client = await get_async_client()

        # Prepare data for insertion - column-oriented format
        data = [
            df["date"].tolist(),  # d_local
            ["synthetic"] * len(df),  # account
            ["_PORTFOLIO"] * len(df),  # symbol
            df["pnl"].tolist(),  # realized
            [0.0] * len(df),  # unrealized_eod
            df["fees"].tolist(),  # fees
            df["num_trades"].tolist(),  # trades
            [0] * len(df),  # turn_qty
            [0] * len(df),  # pos_qty_eod
            [0.0] * len(df),  # mtm_close
            [1] * len(df),  # ver
        ]

        # Insert into ClickHouse
        await ch_client.insert(
            "trading.pnl_daily",
            data,
            column_names=[
                "d_local", "account", "symbol", "realized", "unrealized_eod",
                "fees", "trades", "turn_qty", "pos_qty_eod", "mtm_close", "ver"
            ],
            column_oriented=True,
            settings={'max_partitions_per_insert_block': 1000} # Increased to handle more partitions
        )

        logger.info(
            "synthetic_pnl_saved",
            rows=len(df),
            table="trading.pnl_daily",
            date_range=f"{df['date'].min()} to {df['date'].max()}"
        )

        print(f"✅ Saved {len(df)} rows to trading.pnl_daily")
        print(f"📅 Date range: {df['date'].min()} to {df['date'].max()}")
        print(f"💰 Total PnL: ${df['pnl'].sum():,.2f}")
        print(f"📊 Final capital: ${self.starting_capital + df['pnl_cum'].iloc[-1]:,.2f}")

        # Calculate actual Sharpe for verification
        actual_sharpe = df["ret"].mean() / df["ret"].std() * np.sqrt(252)
        print(f"📈 Realized Sharpe ratio: {actual_sharpe:.3f} (target: {self.target_sharpe:.3f})")


async def main():
    """Main entry point."""

    parser = argparse.ArgumentParser(
        description="Generate synthetic PnL data for testing",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--days",
        type=int,
        default=252,
        help="Number of trading days to generate (default: 252 = 1 year)"
    )

    parser.add_argument(
        "--sharpe",
        type=float,
        default=0.5,
        help="Target Sharpe ratio (default: 0.5)"
    )

    parser.add_argument(
        "--capital",
        type=float,
        default=100000.0,
        help="Starting capital (default: 100000)"
    )

    parser.add_argument(
        "--vol",
        type=float,
        default=0.015,
        help="Daily volatility (default: 0.015 = 1.5%%)"
    )

    parser.add_argument(
        "--winrate",
        type=float,
        default=0.55,
        help="Win rate (default: 0.55 = 55%%)"
    )

    parser.add_argument(
        "--csv",
        type=str,
        help="Optional: save to CSV file (in addition to ClickHouse)"
    )

    args = parser.parse_args()

    # Validate inputs
    if args.days < 20:
        print("❌ Error: Need at least 20 days for meaningful Sharpe calculation", file=sys.stderr)
        sys.exit(1)

    if not (0.0 <= args.winrate <= 1.0):
        print("❌ Error: Win rate must be between 0.0 and 1.0", file=sys.stderr)
        sys.exit(1)

    # Generate data
    print(f"🎲 Generating {args.days} days of synthetic PnL data...")
    print(f"   Target Sharpe: {args.sharpe:.2f}")
    print(f"   Starting capital: ${args.capital:,.2f}")
    print(f"   Daily volatility: {args.vol*100:.1f}%")
    print(f"   Win rate: {args.winrate*100:.1f}%")
    print()

    generator = SyntheticPnLGenerator(
        days=args.days,
        starting_capital=args.capital,
        target_sharpe=args.sharpe,
        daily_vol=args.vol,
        win_rate=args.winrate,
    )

    df = generator.generate()

    # Save to ClickHouse
    await generator.save_to_clickhouse(df)

    # Optionally save to CSV
    if args.csv:
        csv_path = Path(args.csv)
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(csv_path, index=False)
        print(f"💾 Also saved to CSV: {csv_path}")

    print()
    print("✅ Synthetic data generation complete!")
    print()
    print("📋 Next steps:")
    print("   1. Run PnL report: venv/bin/python scripts/metrics/pnl_report.py --sink ch --days", args.days)
    print("   2. Start system: venv/bin/python run_system.py")
    print("   3. View dashboard: Import dashboards/pnl_baseline_dashboard.json to Grafana")


if __name__ == "__main__":
    asyncio.run(main())
