#!/usr/bin/env python3
"""
Load historical data for all strategies in strategies/configs folder.

This script:
1. Reads all strategy config JSON files
2. Extracts symbols and data requirements
3. Calls load_hist_multi.py to fetch the required historical data
"""
import json
import os
import sys
import subprocess
from pathlib import Path
from typing import Set, Dict, Any

# Project root
PROJECT_ROOT = Path(__file__).parent.parent
CONFIGS_DIR = PROJECT_ROOT / "strategies" / "configs"
LOAD_HIST_SCRIPT = PROJECT_ROOT / "tool" / "load_hist_multi.py"


def load_strategy_configs() -> Dict[str, Dict[str, Any]]:
    """Load all strategy configuration files."""
    configs = {}

    for config_file in CONFIGS_DIR.glob("*.json"):
        try:
            with open(config_file, 'r') as f:
                config = json.load(f)
                symbol = config.get("symbol")
                if symbol:
                    configs[symbol] = config
                    print(f"✓ Loaded config: {config_file.name} (symbol: {symbol})")
        except Exception as e:
            print(f"✗ Error loading {config_file.name}: {e}")

    return configs


def extract_symbols_and_requirements(configs: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Extract symbols and their data requirements from configs."""
    symbols = set()
    additional_symbols = set()  # For VIX, SPY, QQQ, etc.
    max_days = 0
    bar_sizes = set()

    for symbol, config in configs.items():
        # Main symbol
        symbols.add(symbol)

        # Get max lookback period
        windows = config.get("windows", {})
        baseline_days = windows.get("baseline_days", 180)
        max_days = max(max_days, baseline_days)

        # Extract additional symbols needed
        regime = config.get("regime", {})
        vix_symbol = regime.get("vix_symbol")
        if vix_symbol:
            additional_symbols.add(vix_symbol)

        market_regime_symbols = config.get("market_regime_symbols", [])
        additional_symbols.update(market_regime_symbols)

        # Check data_limits for bar sizes needed
        data_limits = config.get("data_limits", {})
        if "bars_1m" in data_limits:
            bar_sizes.add("1m")
        if "bars_5m" in data_limits or "bars_5m_120" in data_limits:
            bar_sizes.add("5m")

    # Default to common bar sizes if none specified
    if not bar_sizes:
        bar_sizes = {"1m", "5m"}

    return {
        "symbols": sorted(symbols),
        "additional_symbols": sorted(additional_symbols),
        "max_days": max_days,
        "bar_sizes": sorted(bar_sizes)
    }


def run_data_load(symbols: list, days: int, bar_size: str, fill_mode: str = "backfill",
                  chunk_days: int = None, use_rth: bool = True):
    """Run load_hist_multi.py for the given parameters."""
    cmd = [
        sys.executable,
        str(LOAD_HIST_SCRIPT),
        "--symbols", *symbols,
        "--days", str(days),
        "--bar-size", bar_size,
        "--fill-mode", fill_mode,
    ]

    if chunk_days:
        cmd.extend(["--chunk-days", str(chunk_days)])

    if use_rth:
        cmd.append("--use-rth")
    else:
        cmd.append("--no-rth")

    print(f"\n{'='*80}")
    print(f"Running: {' '.join(cmd)}")
    print(f"{'='*80}\n")

    try:
        result = subprocess.run(cmd, check=True)
        return result.returncode == 0
    except subprocess.CalledProcessError as e:
        print(f"✗ Error running command: {e}")
        return False


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Load historical data for all strategies",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Load all data with backfill mode
  python tool/load_strategies_data.py --fill-mode backfill

  # Load only main strategy symbols (skip VIX, SPY, QQQ)
  python tool/load_strategies_data.py --skip-additional

  # Load with custom days and forward mode
  python tool/load_strategies_data.py --days 90 --fill-mode forward

  # Load specific bar size only
  python tool/load_strategies_data.py --bar-size 1m
        """
    )

    parser.add_argument("--days", type=int, default=None,
                       help="Days to fetch (default: use max from configs, usually 180)")
    parser.add_argument("--fill-mode", choices=["forward", "backfill"], default="backfill",
                       help="forward: only after last DB ts; backfill: fetch full window")
    parser.add_argument("--bar-size", type=str, default=None,
                       help="Specific bar size to load (e.g., '1m', '5m'). If not set, loads all required sizes.")
    parser.add_argument("--chunk-days", type=int, default=5,
                       help="Days per request chunk (default: 5)")
    parser.add_argument("--skip-additional", action="store_true",
                       help="Skip loading additional symbols (VIX, SPY, QQQ)")
    parser.add_argument("--no-rth", action="store_true",
                       help="Include all hours (not just regular trading hours)")
    parser.add_argument("--dry-run", action="store_true",
                       help="Show what would be loaded without actually loading")

    args = parser.parse_args()

    print("="*80)
    print("Loading Strategy Configurations")
    print("="*80)

    configs = load_strategy_configs()

    if not configs:
        print("✗ No strategy configs found!")
        return 1

    print(f"\n✓ Loaded {len(configs)} strategy configs\n")

    requirements = extract_symbols_and_requirements(configs)

    # Use command line args or defaults from configs
    days = args.days or requirements["max_days"]
    bar_sizes = [args.bar_size] if args.bar_size else requirements["bar_sizes"]

    print("="*80)
    print("Data Requirements Summary")
    print("="*80)
    print(f"Main symbols: {', '.join(requirements['symbols'])}")
    if not args.skip_additional and requirements['additional_symbols']:
        print(f"Additional symbols: {', '.join(requirements['additional_symbols'])}")
    print(f"Days to fetch: {days}")
    print(f"Bar sizes: {', '.join(bar_sizes)}")
    print(f"Fill mode: {args.fill_mode}")
    print(f"Use RTH: {not args.no_rth}")
    print("="*80)

    if args.dry_run:
        print("\n[DRY RUN] Would execute the following commands:\n")
        for bar_size in bar_sizes:
            all_symbols = requirements['symbols'].copy()
            if not args.skip_additional:
                all_symbols.extend(requirements['additional_symbols'])
            print(f"  Load {bar_size} data for: {', '.join(all_symbols)}")
        return 0

    # Confirm before proceeding
    response = input("\nProceed with data loading? (y/N): ")
    if response.lower() not in ['y', 'yes']:
        print("Aborted.")
        return 0

    # Load data for each bar size
    success_count = 0
    total_count = 0

    for bar_size in bar_sizes:
        all_symbols = requirements['symbols'].copy()

        # Add additional symbols if not skipped
        if not args.skip_additional:
            all_symbols.extend(requirements['additional_symbols'])

        # Remove duplicates
        all_symbols = list(dict.fromkeys(all_symbols))

        total_count += 1
        if run_data_load(
            symbols=all_symbols,
            days=days,
            bar_size=bar_size,
            fill_mode=args.fill_mode,
            chunk_days=args.chunk_days,
            use_rth=not args.no_rth
        ):
            success_count += 1

    print("\n" + "="*80)
    print(f"Summary: {success_count}/{total_count} loads completed successfully")
    print("="*80)

    return 0 if success_count == total_count else 1


if __name__ == "__main__":
    sys.exit(main())
