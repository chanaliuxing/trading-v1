#!/usr/bin/env python3
"""
Run full 30-day backtest for TSLA V7.2 strategy
"""
import json
from backtest_ml_engine import BacktestEngine, BacktestConfig
from tools.training_gates import enforce_training_gates

# Load backtest-only config
with open("strategies/configs/tsla_backtest_only.json", "r") as f:
    backtest_config = json.load(f)

backtest_config["log_signals"] = False  # Disable verbose logging for full run

strategy_params = {
    "tsla_v7_2_config": backtest_config
}

# Full 30-day backtest: Sep 15 - Oct 14, 2025
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

print("=" * 80)
print("Running 30-day backtest: Sep 15 - Oct 14, 2025")
print("=" * 80)

# Guard: ensure data/labels are healthy before running long backtest
gate = enforce_training_gates(
    symbols=["TSLA"],
    horizons=[30],
    window_days=30,
    features_table="trading.features_1m",
    labels_table="trading.features_1m_with_labels",
    data_quality_days=3,
    auto_heal=True,
    heal_lookback_days=2,
)
if not gate.get("ready"):
    raise SystemExit(f"[full-backtest] abort: gates blocked {gate}")

engine = BacktestEngine(config)
results = engine.run()

print("\n" + "=" * 80)
print("BACKTEST RESULTS")
print("=" * 80)

metrics = results['metrics']
print(f"\n📊 Performance Metrics:")
print(f"  Total Return:     {metrics['total_return']:>8.2%}")
print(f"  Sharpe Ratio:     {metrics['sharpe']:>8.2f}")
print(f"  Max Drawdown:     {metrics['max_drawdown']:>8.2%}")
print(f"  Closed Trades:    {metrics['trades']:>8}")
print(f"  Win Rate:         {metrics['win_rate']:>8.1%}")
print(f"  Profit Factor:    {metrics['profit_factor']:>8.2f}")

# Final portfolio state
snap = results['final_snapshot']
print(f"\n💰 Final Portfolio:")
print(f"  Cash:             ${snap['cash']:>12,.2f}")
print(f"  Equity:           ${snap['equity']:>12,.2f}")
print(f"  Realized PnL:     ${snap['realized_pnl']:>12,.2f}")
print(f"  Open Positions:   {len(snap['positions'])}")

# Trade ledger
ledger = engine.sim.get_trade_ledger()
if ledger:
    print(f"\n📝 Trade Ledger (last 10 trades):")
    for trade in ledger[-10:]:
        pnl = trade.get('pnl', 0.0)
        sign = "✅" if pnl > 0 else "❌"
        print(f"  {sign} {trade.get('side', 'UNKNOWN'):5s} {trade.get('qty', 0):3.0f} @ ${trade.get('exit_price', 0):7.2f} | PnL: ${pnl:8.2f}")

# Save results to JSON
output_file = "backtest_results_30day.json"
with open(output_file, "w") as f:
    json.dump({
        "config": {
            "symbols": config.symbols,
            "start": config.start,
            "end": config.end,
            "initial_cash": config.initial_cash,
        },
        "metrics": metrics,
        "final_snapshot": snap,
        "trade_count": len(ledger),
        "equity_curve_points": len(results['equity_curve'])
    }, f, indent=2)

print(f"\n💾 Results saved to: {output_file}")
print("=" * 80)
