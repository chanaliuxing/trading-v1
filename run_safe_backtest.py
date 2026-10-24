"""
Safe backtest runner - uses BACKTEST-ONLY config, does not modify live trading configs.
"""

import json
from backtest_ml_engine import BacktestEngine, BacktestConfig

print("=" * 80)
print("SAFE BACKTEST MODE - Using backtest-only configuration")
print("This will NOT affect your live trading configs!")
print("=" * 80)

# Load backtest-only config
with open("strategies/configs/tsla_backtest_only.json", "r") as f:
    backtest_config = json.load(f)

print(f"\nConfig: {backtest_config['description']}")

# Wrap config
strategy_params = {
    "tsla_v7_2_config": backtest_config
}

# Run 30-day backtest using features_1m (has historical data)
# Note: bars_1m_ibkr only has today's data
config = BacktestConfig(
    symbols=["TSLA"],
    start="2025-09-15 09:30:00",
    end="2025-10-14 16:00:00",
    timeframe="1m",
    features_table="trading.features_1m",  # Historical data
    strategy_path="strategies.tsla_v7_2.TSLASimplifiedV7_2",
    strategy_params=strategy_params,
    initial_cash=100000,
    commission_per_share=0.0035,
    commission_min=1.0,
    slippage_bps=1.0,
    fill_ref="open",
)

print(f"\nBacktest Period: {config.start} to {config.end}")
print(f"Initial Cash: ${config.initial_cash:,.2f}")
print("\nRelaxed Settings:")
print(f"  A1_thr_sigma: {backtest_config['A1']['thr_sigma']}")
print(f"  A1_rvol_lo: {backtest_config['A1']['rvol_lo']}")
print(f"  A2_grid_g: {backtest_config['A2']['grid_g']}")
print(f"  B1_rvol_min: {backtest_config['B1']['rvol_min']}")
print(f"  edge_floor: {backtest_config['edge_floor']}")
print(f"  market_mult: {backtest_config['initial_state']['market_mult']} (fixed)")
print("\nRunning backtest...")
print("=" * 80)

# Run backtest
engine = BacktestEngine(config)
results = engine.run()

# Save results
output_file = "results/tsla_30day_safe_backtest.json"
with open(output_file, 'w') as f:
    json.dump(results, f, indent=2)

# Print summary
metrics = results.get('metrics', {})
trades = results.get('trades', [])

print("\n" + "=" * 80)
print("BACKTEST RESULTS")
print("=" * 80)
print(f"Total Trades: {metrics.get('trades', 0)}")
print(f"Win Rate: {metrics.get('win_rate', 0):.1%}")
print(f"Total Return: {metrics.get('total_return', 0):.2%}")
print(f"Sharpe Ratio: {metrics.get('sharpe', 0):.2f}")
print(f"Max Drawdown: {metrics.get('max_drawdown', 0):.2%}")
print(f"Profit Factor: {metrics.get('profit_factor', 'N/A')}")
print(f"Final NAV: ${results.get('final_nav', 0):,.2f}")
print("=" * 80)

if trades and len(trades) > 0:
    print(f"\n✓ SUCCESS! Generated {len(trades)} trades")
    print(f"\nResults saved to: {output_file}")

    # Show first few trades
    print("\nFirst 5 trades:")
    for i, trade in enumerate(trades[:5], 1):
        print(f"  {i}. {trade.get('side')} {trade.get('qty')} @ ${trade.get('price', 0):.2f}")

    print("\nTo visualize results, run:")
    print(f"python plot_backtest.py {output_file} TSLA --start 2025-09-15 --end 2025-10-14 --output results/tsla_backtest_plot.png")
else:
    print(f"\n⚠ No trades generated. Results saved to: {output_file}")
    print("\nDebugging info:")
    print(f"  Data bars loaded: {len(engine._data) if hasattr(engine, '_data') else 'Unknown'}")
    print(f"  Equity curve points: {len(results.get('equity_curve', []))}")

print("\n" + "=" * 80)
print("Live trading configs were NOT modified - safe to trade tomorrow!")
print("=" * 80)
