#!/usr/bin/env python3
"""
LLM Trading Race - Run multiple LLM providers in parallel and compare performance

This script runs multiple LLM trading bots concurrently, each with the same initial
capital and symbols, and compares their performance.
"""

import asyncio
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent))

from llm_trader.llm_trader_bot import LLMTraderBot
from llm_trader.utils.config import Config
from datetime import datetime
from zoneinfo import ZoneInfo
import json


async def run_bot_instance(
    provider_name: str,
    symbols: list,
    config: Config,
    initial_cash: float,
    interval: int,
    max_cycles: int,
) -> dict:
    """Run a single bot instance and return results

    Args:
        provider_name: LLM provider name
        symbols: List of symbols to trade
        config: Configuration
        initial_cash: Initial cash
        interval: Seconds between cycles
        max_cycles: Max number of cycles

    Returns:
        Dict with final results
    """
    print(f"\n[{provider_name.upper()}] Starting bot...")

    try:
        bot = LLMTraderBot(
            provider_name=provider_name,
            symbols=symbols,
            config=config,
            initial_cash=initial_cash,
            dry_run=True,  # Always simulate in race mode
        )

        await bot.run(interval=interval, max_cycles=max_cycles)

        # Get final state
        final_state = bot.portfolio.get_detailed_state()

        return {
            "provider": provider_name,
            "status": "completed",
            "final_equity": final_state["total_equity"],
            "total_pnl": final_state["total_pnl"],
            "total_pnl_pct": final_state["total_pnl_pct"],
            "num_trades": len(bot.portfolio.trade_history),
            "num_positions": final_state["num_positions"],
            "cycle_count": bot.cycle_count,
        }

    except Exception as e:
        print(f"\n[{provider_name.upper()}] ERROR: {e}")
        import traceback
        traceback.print_exc()

        return {
            "provider": provider_name,
            "status": "failed",
            "error": str(e),
        }


async def run_llm_race(
    providers: list,
    symbols: list,
    initial_cash: float = 50000.0,
    interval: int = 60,
    max_cycles: int = 10,
):
    """Run LLM trading race with multiple providers

    Args:
        providers: List of provider names to race
        symbols: List of symbols to trade
        initial_cash: Initial cash for each bot
        interval: Seconds between cycles
        max_cycles: Max number of cycles
    """
    print("\n" + "="*100)
    print(" "*30 + "🏁 LLM TRADING RACE 🏁")
    print("="*100)
    print(f"\nRace Configuration:")
    print(f"  Providers: {', '.join(providers)} ({len(providers)} LLMs)")
    print(f"  Symbols: {', '.join(symbols)}")
    print(f"  Initial Cash per LLM: ${initial_cash:,.2f}")
    print(f"  Total Capital (all LLMs): ${initial_cash * len(providers):,.2f}")
    print(f"  Interval: {interval}s")
    print(f"  Max Cycles: {max_cycles}")
    print(f"  Mode: DRY RUN (Simulation)")
    print(f"\n  ⚠️  Each LLM has INDEPENDENT capital - they don't share funds!")
    print("="*100 + "\n")

    # Load config
    config = Config()

    # Start all bots concurrently
    tasks = []
    for provider in providers:
        task = run_bot_instance(
            provider_name=provider,
            symbols=symbols,
            config=config,
            initial_cash=initial_cash,
            interval=interval,
            max_cycles=max_cycles,
        )
        tasks.append(task)

    # Wait for all to complete
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Process results
    successful_results = []
    for result in results:
        if isinstance(result, dict) and result.get("status") == "completed":
            successful_results.append(result)

    # Sort by P&L
    successful_results.sort(key=lambda x: x["total_pnl"], reverse=True)

    # Print results
    print("\n" + "="*100)
    print(" "*35 + "🏆 RACE RESULTS 🏆")
    print("="*100 + "\n")

    for i, result in enumerate(successful_results, 1):
        medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
        print(f"{medal} {result['provider'].upper():20s} | "
              f"P&L: ${result['total_pnl']:>10,.2f} ({result['total_pnl_pct']:>6.2f}%) | "
              f"Equity: ${result['final_equity']:>10,.2f} | "
              f"Trades: {result['num_trades']:>3d} | "
              f"Cycles: {result['cycle_count']:>3d}")

    print("\n" + "="*100 + "\n")

    # Save results
    # Use ET time for consistency
    et_now = datetime.now(ZoneInfo("America/New_York"))
    timestamp = et_now.strftime("%Y%m%d_%H%M%S")
    results_file = f"results/race_results_{timestamp}.json"

    race_summary = {
        "timestamp": timestamp,
        "config": {
            "providers": providers,
            "symbols": symbols,
            "initial_cash": initial_cash,
            "interval": interval,
            "max_cycles": max_cycles,
        },
        "results": successful_results,
    }

    Path("results").mkdir(exist_ok=True)
    with open(results_file, "w") as f:
        json.dump(race_summary, f, indent=2)

    print(f"Results saved to {results_file}\n")


def main():
    """Main entry point"""
    import argparse

    parser = argparse.ArgumentParser(description="LLM Trading Race")
    parser.add_argument(
        "--providers",
        nargs="+",
        default=["openai", "gemini", "deepseek", "deepseek_nvidia"],
        choices=["openai", "gemini", "deepseek", "deepseek_nvidia"],
        help="LLM providers to race",
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=["AAPL", "TSLA", "NVDA", "MSFT", "META"],
        help="Symbols to trade",
    )
    parser.add_argument(
        "--cash",
        type=float,
        default=50000.0,
        help="Initial cash per bot",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help="Seconds between trading cycles",
    )
    parser.add_argument(
        "--cycles",
        type=int,
        default=10,
        help="Max number of trading cycles",
    )

    args = parser.parse_args()

    asyncio.run(
        run_llm_race(
            providers=args.providers,
            symbols=args.symbols,
            initial_cash=args.cash,
            interval=args.interval,
            max_cycles=args.cycles,
        )
    )


if __name__ == "__main__":
    main()
