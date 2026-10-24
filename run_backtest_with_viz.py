"""
Run backtest with relaxed config and generate visualization.
"""

import json
import subprocess
import sys
from backtest_ml_engine import BacktestEngine, BacktestConfig

# Load the relaxed config
with open("strategies/configs/tsla_v7_2_relaxed.json", "r") as f:
    relaxed_config = json.load(f)

# Wrap the entire config as tsla_v7_2_config
strategy_params = {
    "tsla_v7_2_config": relaxed_config
}

# Create backtest config
config = BacktestConfig(
    symbols=["TSLA"],
    start="2025-09-15 09:30:00",
    end="2025-10-14 16:00:00",
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

print("=" * 60)
print("Running TSLA Backtest with Relaxed Configuration")
print("=" * 60)
print(f"Period: {config.start} to {config.end}")
print(f"Initial Cash: ${config.initial_cash:,.2f}")
print(f"Config: Using tsla_v7_2_relaxed.json")
print("=" * 60)

# Run backtest
engine = BacktestEngine(config)
results = engine.run()

# Save results
output_file = "results/tsla_30day_relaxed.json"
with open(output_file, 'w') as f:
    json.dump(results, f, indent=2)

# Print summary
metrics = results.get('metrics', {})
trades = results.get('trades', [])

print("\n" + "=" * 60)
print("BACKTEST RESULTS")
print("=" * 60)
print(f"Total Trades: {metrics.get('trades', 0)}")
print(f"Win Rate: {metrics.get('win_rate', 0):.1%}")
print(f"Total Return: {metrics.get('total_return', 0):.2%}")
print(f"Sharpe Ratio: {metrics.get('sharpe', 0):.2f}")
print(f"Max Drawdown: {metrics.get('max_drawdown', 0):.2%}")
print(f"Profit Factor: {metrics.get('profit_factor', 'N/A')}")
print(f"Final NAV: ${results.get('final_nav', 0):,.2f}")
print("=" * 60)

if trades and len(trades) > 0:
    print(f"\nResults saved to: {output_file}")
    print("\nGenerating visualization...")

    # Generate plot
    plot_cmd = [
        "python3", "plot_backtest.py",
        output_file, "TSLA",
        "--start", "2025-09-15",
        "--end", "2025-10-14",
        "--output", "results/tsla_30day_relaxed_plot.png"
    ]

    result = subprocess.run(plot_cmd, capture_output=True, text=True)

    if result.returncode == 0:
        print("✓ Plot saved to: results/tsla_30day_relaxed_plot.png")
    else:
        print(f"✗ Plot generation failed: {result.stderr}")
else:
    print(f"\n⚠ No trades generated. Results saved to: {output_file}")
    print("\nPossible reasons:")
    print("- Market conditions didn't meet relaxed thresholds")
    print("- No data available for this period")
    print("- Cost gate filtering out all signals")

print("\n" + "=" * 60)
