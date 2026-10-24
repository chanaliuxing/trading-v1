"""
Debug script to understand why backtest generates 0 trades.
Adds verbose logging to see signal generation details.
"""

import json
import asyncio
from backtest_ml_engine import BacktestEngine, BacktestConfig

# Load the relaxed config
with open("strategies/configs/tsla_v7_2_relaxed.json", "r") as f:
    relaxed_config = json.load(f)

# Make it ULTRA relaxed - disable all filters
relaxed_config["A1"]["thr_sigma"] = 0.001  # Very low threshold
relaxed_config["A1"]["rvol_lo"] = 0.1      # Very low RVOL requirement
relaxed_config["A1"]["rvol_hi"] = 10.0     # Very high RVOL max
relaxed_config["A2"]["grid_g"] = 0.1       # Very tight grid
relaxed_config["A2"]["oneway_thr"] = 10.0  # No oneway filter
relaxed_config["B1"]["rvol_min"] = 0.1     # Very low RVOL requirement
relaxed_config["edge_floor"] = -10.0       # Allow massive negative edge
relaxed_config["dd_limits"]["soft_pct"] = 0.99  # No drawdown limits
relaxed_config["dd_limits"]["hard_pct"] = 0.99

# Wrap config
strategy_params = {
    "tsla_v7_2_config": relaxed_config
}

# Test just 1 day first
config = BacktestConfig(
    symbols=["TSLA"],
    start="2025-10-10 09:30:00",
    end="2025-10-10 16:00:00",
    timeframe="1m",
    features_table="trading.features_1m",
    strategy_path="strategies.tsla_v7_2.TSLASimplifiedV7_2",
    strategy_params=strategy_params,
    initial_cash=100000,
    commission_per_share=0.0035,
    commission_min=1.0,
    slippage_bps=1.0,
    fill_ref="open",
)

print("=" * 80)
print("DEBUG: Ultra-Relaxed 1-Day TSLA Backtest")
print("=" * 80)
print(f"Date: 2025-10-10")
print(f"Ultra-relaxed settings:")
print(f"  A1_thr_sigma: {relaxed_config['A1']['thr_sigma']}")
print(f"  A1_rvol_lo: {relaxed_config['A1']['rvol_lo']}")
print(f"  A2_grid_g: {relaxed_config['A2']['grid_g']}")
print(f"  edge_floor: {relaxed_config['edge_floor']}")
print("=" * 80)

# Run backtest
engine = BacktestEngine(config)
results = engine.run()

# Print results
metrics = results.get('metrics', {})
trades = results.get('trades', [])

print("\nRESULTS:")
print(f"Total Trades: {metrics.get('trades', 0)}")
print(f"Final NAV: ${results.get('final_nav', 0):,.2f}")

if trades:
    print(f"\n✓ SUCCESS! Generated {len(trades)} trades")
    for i, trade in enumerate(trades, 1):
        print(f"\nTrade {i}:")
        print(f"  Side: {trade.get('side')}")
        print(f"  Qty: {trade.get('qty')}")
        print(f"  Price: ${trade.get('price', 0):.2f}")
else:
    print("\n✗ Still 0 trades even with ultra-relaxed settings")
    print("\nThis suggests the issue is deeper - possibly:")
    print("1. Strategy never calls on_features() - data loading issue")
    print("2. Strategy exits early in on_features() before checking signals")
    print("3. Risk multipliers are 0.0 (blocking all entries)")

# Check data was loaded
print(f"\nData loaded: {len(engine._data) if hasattr(engine, '_data') else 'Unknown'} bars")
print(f"Equity curve points: {len(results.get('equity_curve', []))}")

# Save for inspection
with open("results/debug_ultra_relaxed.json", "w") as f:
    json.dump(results, f, indent=2)
print("\nResults saved to: results/debug_ultra_relaxed.json")
