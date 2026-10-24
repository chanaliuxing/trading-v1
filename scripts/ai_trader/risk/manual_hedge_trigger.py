#!/usr/bin/env python3
"""Manually trigger an options hedge.

This script allows operators to manually trigger hedge contract selection and execution
bypassing the normal kill-switch flow. Useful for:
- Testing hedge selection algorithm
- Emergency hedging outside normal triggers
- Recovering from failed automatic hedges

Usage:
    # Dry-run selection only (no execution)
    python scripts/ai_trader/risk/manual_hedge_trigger.py \
        --runs-dir runs/ai_trader \
        --dry-run

    # Execute hedge (submit order to IBKR)
    python scripts/ai_trader/risk/manual_hedge_trigger.py \
        --runs-dir runs/ai_trader \
        --intent-type drawdown_protect \
        --size medium \
        --urgency high \
        --execute

    # Force specific underlying and size
    python scripts/ai_trader/risk/manual_hedge_trigger.py \
        --runs-dir runs/ai_trader \
        --intent-type beta_balance \
        --instrument SPY \
        --size large \
        --execute

Safety:
- Respects daily limits (cannot bypass)
- Requires explicit --execute flag for real orders
- Validates current exposure before proceeding
- Logs all actions to hedging_events.jsonl
"""

from __future__ import annotations
import asyncio
import argparse
import json
from pathlib import Path
from typing import Optional

from scripts.ai_trader.risk.options_selector import propose_options_hedge_with_selector, HedgeIntent
from scripts.ai_trader.risk.options_hedge import _portfolio_exposure
import subprocess
import sys
import os


async def manual_trigger_hedge(
    runs_dir: str,
    intent_type: str,
    instrument: Optional[str] = None,
    size: str = "medium",
    urgency: str = "normal",
    dry_run: bool = True,
    execute: bool = False,
):
    """Manually trigger hedge selection and optionally execute.

    Args:
        runs_dir: Base runs directory
        intent_type: One of: vol_spike, beta_balance, drawdown_protect
        instrument: Optional underlying (SPY, QQQ, or None for auto)
        size: small, medium, or large
        urgency: normal or high
        dry_run: If True, only select contract without execution
        execute: If True, submit order to IBKR (requires dry_run=False)
    """
    print("=" * 80)
    print("Manual Hedge Trigger")
    print("=" * 80)

    # Validate inputs
    if intent_type not in ["vol_spike", "beta_balance", "drawdown_protect"]:
        print(f"❌ Invalid intent_type: {intent_type}")
        print("   Valid options: vol_spike, beta_balance, drawdown_protect")
        return

    if size not in ["small", "medium", "large"]:
        print(f"❌ Invalid size: {size}")
        print("   Valid options: small, medium, large")
        return

    if urgency not in ["normal", "high"]:
        print(f"❌ Invalid urgency: {urgency}")
        print("   Valid options: normal, high")
        return

    if execute and dry_run:
        print("⚠️ Warning: --execute requires dry_run=False")
        dry_run = False

    # Get current exposure
    print("\n📊 Fetching current portfolio exposure...")
    try:
        exposure = _portfolio_exposure()
        print(f"   Gross: ${exposure['gross']:,.0f}")
        print(f"   Net:   ${exposure['net']:,.0f}")
        print(f"   Long:  ${exposure['long']:,.0f}")
        print(f"   Short: ${exposure['short']:,.0f}")
    except Exception as e:
        print(f"❌ Failed to fetch exposure: {e}")
        return

    # Build intent
    intent = HedgeIntent(
        intent_type=intent_type,
        instrument=instrument,
        size=size,
        urgency=urgency,
    )

    print(f"\n🎯 Hedge Intent:")
    print(f"   Type: {intent_type}")
    print(f"   Instrument: {instrument or 'auto'}")
    print(f"   Size: {size}")
    print(f"   Urgency: {urgency}")

    # Select contract
    print(f"\n🔍 Selecting contract...")
    try:
        result = await propose_options_hedge_with_selector(
            runs_dir=runs_dir,
            exposure=exposure,
            intent=intent,
        )

        print(f"\n✅ Selection complete")

        # Check result
        hedge_file = Path(runs_dir) / "latest/options_hedge.json"
        if not hedge_file.exists():
            print(f"❌ No hedge plan generated")
            print(f"   Check: {runs_dir}/*/hedging_events.jsonl for rejection reasons")
            return

        with hedge_file.open("r") as f:
            plan = json.load(f)

        print(f"\n📋 Selected Contract:")
        print(f"   Symbol:     {plan.get('occ_symbol', 'N/A')}")
        print(f"   Side:       {plan.get('side', 'N/A')}")
        print(f"   Quantity:   {plan.get('quantity', 0)} contracts")
        print(f"   Limit Price: ${plan.get('limit_price', 0):.2f}")
        print(f"   Notional:   ${plan.get('notional', 0):,.0f}")
        print(f"   Delta:      {plan.get('delta', 0):.3f}")
        print(f"   Spread:     {plan.get('spread_bps', 0):.1f} bps")
        print(f"   OI:         {plan.get('open_interest', 0):,}")

        if dry_run or not execute:
            print(f"\n🔒 Dry-run mode - no order submitted")
            print(f"   To execute, add --execute flag")
            return

        # Execute via subprocess (execute_hedge.py doesn't export a function)
        print(f"\n🚀 Executing hedge order...")
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/ai_trader/portfolio/execute_hedge.py",
                    "--runs-dir", runs_dir,
                    "--mode", "options",
                    "--execute"
                ],
                cwd=".",
                env={**os.environ, "PYTHONPATH": "."},
                capture_output=True,
                text=True,
                timeout=60
            )

            if result.returncode == 0:
                print(f"✅ Order submitted to IBKR")
                print(f"   Check core.core_order table for order status")
                if result.stdout:
                    print(f"\nOutput:\n{result.stdout}")
            else:
                print(f"❌ Execution failed (exit code: {result.returncode})")
                if result.stderr:
                    print(f"Error:\n{result.stderr}")

        except subprocess.TimeoutExpired:
            print(f"❌ Execution timed out after 60 seconds")
        except Exception as e:
            print(f"❌ Execution failed: {e}")
            import traceback
            traceback.print_exc()

    except Exception as e:
        print(f"❌ Selection failed: {e}")
        import traceback
        traceback.print_exc()


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Manually trigger options hedge",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Dry-run selection only
  python manual_hedge_trigger.py --dry-run

  # Execute small hedge for vol spike
  python manual_hedge_trigger.py --intent-type vol_spike --size small --execute

  # Execute large hedge for drawdown protection on SPY
  python manual_hedge_trigger.py --intent-type drawdown_protect --instrument SPY --size large --urgency high --execute
        """,
    )

    parser.add_argument(
        "--runs-dir",
        default="runs/ai_trader",
        help="Runs directory (default: runs/ai_trader)",
    )

    parser.add_argument(
        "--intent-type",
        default="drawdown_protect",
        choices=["vol_spike", "beta_balance", "drawdown_protect"],
        help="Hedge intent type (default: drawdown_protect)",
    )

    parser.add_argument(
        "--instrument",
        default=None,
        help="Force specific underlying (SPY, QQQ, etc.). Default: auto-select",
    )

    parser.add_argument(
        "--size",
        default="medium",
        choices=["small", "medium", "large"],
        help="Hedge size (default: medium)",
    )

    parser.add_argument(
        "--urgency",
        default="normal",
        choices=["normal", "high"],
        help="Urgency level (default: normal)",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only select contract, do not execute",
    )

    parser.add_argument(
        "--execute",
        action="store_true",
        help="Submit order to IBKR (DANGEROUS: real execution)",
    )

    args = parser.parse_args()

    if args.execute and args.dry_run:
        print("⚠️ Conflicting flags: --execute and --dry-run")
        print("   Using --execute (will submit real order)")
        args.dry_run = False

    if args.execute:
        confirm = input("\n⚠️ CAUTION: This will submit a REAL order to IBKR. Continue? (yes/no): ")
        if confirm.lower() != "yes":
            print("❌ Aborted")
            return

    await manual_trigger_hedge(
        runs_dir=args.runs_dir,
        intent_type=args.intent_type,
        instrument=args.instrument,
        size=args.size,
        urgency=args.urgency,
        dry_run=args.dry_run,
        execute=args.execute,
    )


if __name__ == "__main__":
    asyncio.run(main())
