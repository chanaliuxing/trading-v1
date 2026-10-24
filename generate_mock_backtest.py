"""
Generate mock backtest results with trades for visualization testing.
"""

import json
from datetime import datetime, timedelta

# Create mock backtest results with 3 trades
mock_results = {
    "success": True,
    "final_nav": 102450.75,
    "cash": 2450.75,
    "realized_pnl": 2450.75,
    "metrics": {
        "total_return": 0.024507,
        "return_pct": 2.45,
        "sharpe": 1.85,
        "max_drawdown": 0.0125,
        "trades": 3,
        "win_rate": 0.6667,
        "profit_factor": 3.5
    },
    "trades": [
        {
            "ts": int(datetime(2025, 10, 10, 10, 15).timestamp() * 1000),
            "symbol": "TSLA",
            "side": "BUY",
            "qty": 40,
            "price": 243.50,
            "commission": 1.40,
            "entry_time": "2025-10-10 10:15:00",
            "exit_time": "2025-10-10 11:30:00",
            "entry_price": 243.50,
            "exit_price": 246.25,
            "realized_pnl": 108.60,
            "signal_type": "A1_VWAP"
        },
        {
            "ts": int(datetime(2025, 10, 11, 14, 20).timestamp() * 1000),
            "symbol": "TSLA",
            "side": "BUY",
            "qty": 40,
            "price": 241.75,
            "commission": 1.40,
            "entry_time": "2025-10-11 14:20:00",
            "exit_time": "2025-10-11 15:10:00",
            "entry_price": 241.75,
            "exit_price": 239.80,
            "realized_pnl": -79.40,
            "signal_type": "A2_GRID"
        },
        {
            "ts": int(datetime(2025, 10, 14, 9, 45).timestamp() * 1000),
            "symbol": "TSLA",
            "side": "BUY",
            "qty": 40,
            "price": 244.20,
            "commission": 1.40,
            "entry_time": "2025-10-14 09:45:00",
            "exit_time": "2025-10-14 13:25:00",
            "entry_price": 244.20,
            "exit_price": 250.15,
            "realized_pnl": 235.60,
            "signal_type": "B1_ORB"
        }
    ],
    "equity_curve": []
}

# Generate equity curve (every 30 minutes from Oct 10-14)
start_date = datetime(2025, 10, 10, 9, 30)
end_date = datetime(2025, 10, 14, 16, 0)
current_time = start_date
equity = 100000.0

# Simple equity curve progression
equity_progression = [
    (datetime(2025, 10, 10, 9, 30), 100000),
    (datetime(2025, 10, 10, 10, 15), 100000),  # Trade 1 entry
    (datetime(2025, 10, 10, 11, 30), 100108.60),  # Trade 1 exit +108.60
    (datetime(2025, 10, 10, 16, 0), 100108.60),
    (datetime(2025, 10, 11, 9, 30), 100108.60),
    (datetime(2025, 10, 11, 14, 20), 100108.60),  # Trade 2 entry
    (datetime(2025, 10, 11, 15, 10), 100029.20),  # Trade 2 exit -79.40
    (datetime(2025, 10, 11, 16, 0), 100029.20),
    (datetime(2025, 10, 14, 9, 30), 100029.20),
    (datetime(2025, 10, 14, 9, 45), 100029.20),  # Trade 3 entry
    (datetime(2025, 10, 14, 13, 25), 100264.80),  # Trade 3 exit +235.60
    (datetime(2025, 10, 14, 16, 0), 100264.80),
]

# Generate equity curve points every 5 minutes
current_equity = 100000.0
current_idx = 0
current_time = start_date

while current_time <= end_date:
    # Find the appropriate equity value
    while current_idx < len(equity_progression) - 1 and current_time >= equity_progression[current_idx + 1][0]:
        current_idx += 1

    equity_val = equity_progression[current_idx][1]
    mock_results["equity_curve"].append([int(current_time.timestamp() * 1000), equity_val])

    current_time += timedelta(minutes=5)

# Save to file
output_file = "results/mock_backtest_with_trades.json"
with open(output_file, 'w') as f:
    json.dump(mock_results, f, indent=2)

print(f"Mock backtest results saved to: {output_file}")
print(f"Total trades: {len(mock_results['trades'])}")
print(f"Win rate: {mock_results['metrics']['win_rate']:.1%}")
print(f"Total return: {mock_results['metrics']['return_pct']:.2f}%")
