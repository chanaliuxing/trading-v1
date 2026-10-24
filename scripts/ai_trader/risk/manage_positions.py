#!/usr/bin/env python3
"""View and manage open option positions.

This script provides position management functionality:
- View all open positions
- View positions by underlying
- Check expiring positions
- Update market data (prices, Greeks)
- Force close position (submit closing order)
- View position P&L

Usage:
    # List all positions
    python scripts/ai_trader/risk/manage_positions.py --list

    # Show summary
    python scripts/ai_trader/risk/manage_positions.py --summary

    # Check expiring positions
    python scripts/ai_trader/risk/manage_positions.py --expiring --days 7

    # Update market data from IBKR
    python scripts/ai_trader/risk/manage_positions.py --update-data

    # View specific position
    python scripts/ai_trader/risk/manage_positions.py --position "SPY   251116P00450000"

    # Force close position (submit market order)
    python scripts/ai_trader/risk/manage_positions.py --close "SPY   251116P00450000" --confirm
"""

from __future__ import annotations
import asyncio
import argparse
import json
from pathlib import Path
from typing import Optional

from scripts.ai_trader.risk.options_positions import get_position_tracker


async def list_positions(runs_dir: str, underlying: Optional[str] = None):
    """List all open positions.

    Args:
        runs_dir: Base runs directory
        underlying: Filter by underlying symbol (optional)
    """
    tracker = get_position_tracker(runs_dir)
    positions = tracker.load_positions()

    if not positions:
        print("ℹ️ No open positions")
        return

    # Filter if requested
    if underlying:
        positions = {k: v for k, v in positions.items() if v.underlying == underlying}
        if not positions:
            print(f"ℹ️ No open positions for {underlying}")
            return

    print(f"\n📋 Open Positions ({len(positions)})\n")
    print(
        f"{'Symbol':<25} {'Underlying':<10} {'Expiry':<12} {'Strike':<8} "
        f"{'Type':<4} {'Qty':<5} {'Entry':<8} {'Current':<8} {'P&L':<10} {'DTE':<5} {'Delta':<7}"
    )
    print("-" * 120)

    for symbol, pos in positions.items():
        pnl_str = f"${pos.pnl_unrealized:,.0f}" if pos.pnl_unrealized else "N/A"
        current_str = f"${pos.current_price:.2f}" if pos.current_price else "N/A"
        delta_str = f"{pos.delta:.3f}" if pos.delta else "N/A"

        print(
            f"{symbol:<25} "
            f"{pos.underlying:<10} "
            f"{pos.expiry_date:<12} "
            f"{pos.strike:<8.0f} "
            f"{pos.right:<4} "
            f"{pos.quantity:<5} "
            f"${pos.avg_entry_price:<7.2f} "
            f"{current_str:<8} "
            f"{pnl_str:<10} "
            f"{pos.dte:<5} "
            f"{delta_str:<7}"
        )


async def show_summary(runs_dir: str):
    """Show portfolio summary.

    Args:
        runs_dir: Base runs directory
    """
    tracker = get_position_tracker(runs_dir)
    summary = tracker.get_summary()

    print("\n" + "=" * 80)
    print("Portfolio Summary (期权组合汇总)")
    print("=" * 80)

    print(f"\n📊 Position Counts:")
    print(f"   Total Positions:     {summary['total_positions']}")
    print(f"   Expiring Soon (≤3d): {summary['expiring_soon']}")

    print(f"\n💰 Notional & P&L:")
    print(f"   Total Notional:      ${summary['total_notional']:,.0f}")
    print(f"   Unrealized P&L:      ${summary['total_pnl']:,.0f}")

    print(f"\n📈 Portfolio Greeks:")
    print(f"   Delta:               {summary['total_delta']:,.0f}")
    print(f"   Gamma:               {summary['total_gamma']:,.0f}")
    print(f"   Vega:                {summary['total_vega']:,.0f}")
    print(f"   Theta:               {summary['total_theta']:,.0f}")

    print("=" * 80 + "\n")


async def show_expiring(runs_dir: str, days: int = 3):
    """Show positions expiring soon.

    Args:
        runs_dir: Base runs directory
        days: DTE threshold (default: 3)
    """
    tracker = get_position_tracker(runs_dir)
    expiring = tracker.get_expiring_soon(days=days)

    if not expiring:
        print(f"✅ No positions expiring within {days} days")
        return

    print(f"\n⏰ Positions Expiring Within {days} Days ({len(expiring)})\n")
    print(f"{'Symbol':<25} {'Underlying':<10} {'Expiry':<12} {'DTE':<5} {'Qty':<5} {'Notional':<12} {'P&L':<10}")
    print("-" * 100)

    for pos in expiring:
        pnl_str = f"${pos.pnl_unrealized:,.0f}" if pos.pnl_unrealized else "N/A"
        print(
            f"{pos.occ_symbol:<25} "
            f"{pos.underlying:<10} "
            f"{pos.expiry_date:<12} "
            f"{pos.dte:<5} "
            f"{pos.quantity:<5} "
            f"${pos.notional:<11,.0f} "
            f"{pnl_str:<10}"
        )

    print(f"\n⚠️ Action Required: Consider rolling or closing these positions")


async def update_market_data(runs_dir: str):
    """Update market data from IBKR.

    Args:
        runs_dir: Base runs directory
    """
    print("🔄 Updating market data from IBKR...")

    tracker = get_position_tracker(runs_dir)

    try:
        await tracker.update_market_data()
        print("✅ Market data updated successfully")

        # Show updated summary
        await show_summary(runs_dir)

    except Exception as e:
        print(f"❌ Failed to update market data: {e}")
        import traceback
        traceback.print_exc()


async def show_position_detail(runs_dir: str, occ_symbol: str):
    """Show detailed info for specific position.

    Args:
        runs_dir: Base runs directory
        occ_symbol: OCC symbol
    """
    tracker = get_position_tracker(runs_dir)
    positions = tracker.load_positions()

    if occ_symbol not in positions:
        print(f"❌ Position not found: {occ_symbol}")
        return

    pos = positions[occ_symbol]
    pos_dict = pos.to_dict()

    print("\n" + "=" * 80)
    print(f"Position Detail: {occ_symbol}")
    print("=" * 80)

    print(f"\n📋 Contract Info:")
    print(f"   Description:   {pos.description}")
    print(f"   Underlying:    {pos.underlying}")
    print(f"   Expiry:        {pos.expiry_date} (DTE: {pos.dte})")
    print(f"   Strike:        ${pos.strike:.2f}")
    print(f"   Type:          {pos.right} ({'PUT' if pos.right == 'P' else 'CALL'})")

    print(f"\n💼 Position:")
    print(f"   Quantity:      {pos.quantity} contracts")
    print(f"   Entry Price:   ${pos.avg_entry_price:.2f}")
    print(f"   Current Price: ${pos.current_price:.2f}" if pos.current_price else "   Current Price: N/A")
    print(f"   Entry Date:    {pos.entry_date}")

    print(f"\n💰 P&L:")
    print(f"   Notional:      ${pos.notional:,.0f}")
    if pos.pnl_unrealized is not None:
        print(f"   Unrealized:    ${pos.pnl_unrealized:,.0f}")
        pct = (pos.pnl_unrealized / (pos.quantity * 100 * pos.avg_entry_price)) * 100
        print(f"   Return:        {pct:+.2f}%")
    else:
        print(f"   Unrealized:    N/A")

    print(f"\n📊 Greeks:")
    print(f"   Delta:         {pos.delta:.4f}" if pos.delta else "   Delta:         N/A")
    print(f"   Gamma:         {pos.gamma:.4f}" if pos.gamma else "   Gamma:         N/A")
    print(f"   Vega:          {pos.vega:.4f}" if pos.vega else "   Vega:          N/A")
    print(f"   Theta:         {pos.theta:.4f}" if pos.theta else "   Theta:         N/A")

    print(f"\n⏰ Status:")
    if pos.is_expiring_soon:
        print(f"   ⚠️ EXPIRING SOON ({pos.dte} days)")
    else:
        print(f"   ✅ OK ({pos.dte} days to expiry)")

    print(f"\n🕐 Last Updated: {pos.last_updated or 'Never'}")
    print("=" * 80 + "\n")


async def close_position(runs_dir: str, occ_symbol: str, confirm: bool = False):
    """Close position by submitting closing order.

    Args:
        runs_dir: Base runs directory
        occ_symbol: OCC symbol to close
        confirm: Confirmation flag
    """
    if not confirm:
        print("❌ --confirm flag required for closing positions")
        return

    tracker = get_position_tracker(runs_dir)
    positions = tracker.load_positions()

    if occ_symbol not in positions:
        print(f"❌ Position not found: {occ_symbol}")
        return

    pos = positions[occ_symbol]

    print(f"\n⚠️ CLOSING POSITION: {occ_symbol}")
    print(f"   Quantity: {pos.quantity} contracts")
    print(f"   Side: {'SELL' if pos.quantity > 0 else 'BUY'} TO CLOSE")

    confirm_input = input("\nType 'CLOSE' to proceed: ")
    if confirm_input != "CLOSE":
        print("❌ Aborted")
        return

    # TODO: Submit closing order to IBKR
    # This requires integration with execute_hedge.py or direct IBKR order submission

    print("\n❌ Feature not yet implemented")
    print("   Manual closing required via IBKR TWS/Gateway")
    print("   After closing in IBKR, update position manually:")
    print(f"   tracker.add_position('{occ_symbol}', -{pos.quantity}, current_price, 0)")


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Manage open option positions",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--runs-dir",
        default="runs/ai_trader",
        help="Runs directory (default: runs/ai_trader)",
    )

    parser.add_argument(
        "--list",
        action="store_true",
        help="List all open positions",
    )

    parser.add_argument(
        "--summary",
        action="store_true",
        help="Show portfolio summary",
    )

    parser.add_argument(
        "--expiring",
        action="store_true",
        help="Show expiring positions",
    )

    parser.add_argument(
        "--days",
        type=int,
        default=3,
        help="DTE threshold for expiring (default: 3)",
    )

    parser.add_argument(
        "--underlying",
        help="Filter by underlying symbol",
    )

    parser.add_argument(
        "--update-data",
        action="store_true",
        help="Update market data from IBKR",
    )

    parser.add_argument(
        "--position",
        help="Show detail for specific position (OCC symbol)",
    )

    parser.add_argument(
        "--close",
        help="Close specific position (OCC symbol)",
    )

    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Confirm dangerous operations",
    )

    args = parser.parse_args()

    if args.list:
        await list_positions(args.runs_dir, args.underlying)

    elif args.summary:
        await show_summary(args.runs_dir)

    elif args.expiring:
        await show_expiring(args.runs_dir, args.days)

    elif args.update_data:
        await update_market_data(args.runs_dir)

    elif args.position:
        await show_position_detail(args.runs_dir, args.position)

    elif args.close:
        await close_position(args.runs_dir, args.close, args.confirm)

    else:
        # Default: show summary
        await show_summary(args.runs_dir)


if __name__ == "__main__":
    asyncio.run(main())
