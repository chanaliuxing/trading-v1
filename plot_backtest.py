"""
Backtest Results Visualization Tool

Usage:
    python plot_backtest.py results/batch_backtest_5d_ultra_relaxed.json TSLA
    python plot_backtest.py results/single_symbol_backtest.json TSLA --start "2025-10-10" --end "2025-10-14"
"""

import json
import sys
import argparse
from datetime import datetime, timedelta
from typing import List, Dict, Any
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.patches import Rectangle
import numpy as np

try:
    import clickhouse_connect
except ImportError:
    print("Installing clickhouse_connect...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "clickhouse-connect"])
    import clickhouse_connect


def load_backtest_results(filepath: str, symbol: str) -> Dict[str, Any]:
    """Load backtest results from JSON file."""
    with open(filepath, 'r') as f:
        data = json.load(f)

    # Handle both single-symbol and multi-symbol result formats
    if symbol in data:
        return data[symbol]
    elif "metrics" in data:  # Single symbol format
        return data
    else:
        raise ValueError(f"Symbol {symbol} not found in backtest results")


def fetch_price_data(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    """Fetch 1-minute price data from ClickHouse."""
    client = clickhouse_connect.get_client(
        host='localhost',
        port=8123,
        username='default',
        password=''
    )

    # Convert dates to datetime
    start_dt = pd.to_datetime(start_date)
    end_dt = pd.to_datetime(end_date) + timedelta(days=1)  # Include end date

    query = f"""
    SELECT
        ts,
        open,
        high,
        low,
        close,
        volume
    FROM trading.features_1m
    WHERE symbol = '{symbol}'
        AND ts >= '{start_dt.strftime('%Y-%m-%d %H:%M:%S')}'
        AND ts <= '{end_dt.strftime('%Y-%m-%d %H:%M:%S')}'
    ORDER BY ts
    """

    result = client.query(query)
    df = pd.DataFrame(result.result_rows, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
    df['ts'] = pd.to_datetime(df['ts'])

    return df


def plot_backtest_results(results: Dict[str, Any], price_data: pd.DataFrame, symbol: str):
    """Create comprehensive backtest visualization."""

    trades = results.get('trades', [])
    equity_curve = results.get('equity_curve', [])
    metrics = results.get('metrics', {})

    # Create figure with 3 subplots
    fig = plt.figure(figsize=(16, 12))
    gs = fig.add_gridspec(4, 1, height_ratios=[3, 1, 1, 1], hspace=0.3)

    ax_price = fig.add_subplot(gs[0])
    ax_volume = fig.add_subplot(gs[1], sharex=ax_price)
    ax_equity = fig.add_subplot(gs[2], sharex=ax_price)
    ax_pnl = fig.add_subplot(gs[3], sharex=ax_price)

    # Plot 1: Price Chart with Trade Markers
    ax_price.plot(price_data['ts'], price_data['close'], linewidth=1, color='black', alpha=0.6, label='Close Price')

    # Add high/low envelope
    ax_price.fill_between(price_data['ts'], price_data['low'], price_data['high'],
                          alpha=0.1, color='blue', label='High/Low Range')

    # Plot trades
    if trades:
        entries = []
        exits = []
        pnls = []

        for trade in trades:
            entry_time = pd.to_datetime(trade['entry_time'])
            exit_time = pd.to_datetime(trade['exit_time']) if trade['exit_time'] else None
            entry_price = trade['entry_price']
            exit_price = trade['exit_price'] if trade['exit_price'] else None
            pnl = trade['realized_pnl']
            side = trade['side']

            # Entry marker
            marker_color = 'green' if side == 'long' else 'red'
            marker = '^' if side == 'long' else 'v'
            ax_price.scatter(entry_time, entry_price, color=marker_color, marker=marker,
                           s=100, zorder=5, edgecolors='black', linewidth=0.5)

            # Exit marker
            if exit_time and exit_price:
                exit_marker = 'v' if side == 'long' else '^'
                exit_color = 'darkgreen' if pnl > 0 else 'darkred'
                ax_price.scatter(exit_time, exit_price, color=exit_color, marker=exit_marker,
                               s=100, zorder=5, edgecolors='black', linewidth=0.5)

                # Draw line connecting entry to exit
                ax_price.plot([entry_time, exit_time], [entry_price, exit_price],
                            color=exit_color, alpha=0.3, linewidth=1, linestyle='--')

                # Add P&L annotation
                mid_time = entry_time + (exit_time - entry_time) / 2
                mid_price = (entry_price + exit_price) / 2
                ax_price.annotate(f'${pnl:.2f}', xy=(mid_time, mid_price),
                                xytext=(0, 10), textcoords='offset points',
                                fontsize=8, color=exit_color, weight='bold',
                                bbox=dict(boxstyle='round,pad=0.3', facecolor='white',
                                        edgecolor=exit_color, alpha=0.7))

    ax_price.set_ylabel('Price ($)', fontsize=12)
    ax_price.set_title(f'{symbol} Backtest Results - Price Chart with Trades', fontsize=14, weight='bold')
    ax_price.grid(True, alpha=0.3)
    ax_price.legend(loc='upper left')

    # Plot 2: Volume
    colors = ['green' if price_data.iloc[i]['close'] >= price_data.iloc[i]['open'] else 'red'
              for i in range(len(price_data))]
    ax_volume.bar(price_data['ts'], price_data['volume'], color=colors, alpha=0.5, width=0.0006)
    ax_volume.set_ylabel('Volume', fontsize=12)
    ax_volume.set_title('Volume', fontsize=12)
    ax_volume.grid(True, alpha=0.3, axis='y')

    # Plot 3: Equity Curve
    if equity_curve:
        eq_times = [pd.to_datetime(ts, unit='ms') for ts, _ in equity_curve]
        eq_values = [val for _, val in equity_curve]

        ax_equity.plot(eq_times, eq_values, linewidth=2, color='blue', label='NAV')
        ax_equity.axhline(y=eq_values[0], color='gray', linestyle='--', alpha=0.5, label='Initial Capital')
        ax_equity.fill_between(eq_times, eq_values[0], eq_values,
                              where=[v >= eq_values[0] for v in eq_values],
                              alpha=0.2, color='green', label='Profit')
        ax_equity.fill_between(eq_times, eq_values[0], eq_values,
                              where=[v < eq_values[0] for v in eq_values],
                              alpha=0.2, color='red', label='Loss')

        ax_equity.set_ylabel('NAV ($)', fontsize=12)
        ax_equity.set_title('Equity Curve', fontsize=12)
        ax_equity.grid(True, alpha=0.3)
        ax_equity.legend(loc='upper left')
    else:
        ax_equity.text(0.5, 0.5, 'No equity curve data',
                      ha='center', va='center', transform=ax_equity.transAxes)

    # Plot 4: Cumulative P&L from Trades
    if trades:
        trade_times = []
        cumulative_pnl = []
        running_pnl = 0

        for trade in sorted(trades, key=lambda t: t['exit_time'] if t['exit_time'] else '9999'):
            if trade['exit_time']:
                trade_times.append(pd.to_datetime(trade['exit_time']))
                running_pnl += trade['realized_pnl']
                cumulative_pnl.append(running_pnl)

        if trade_times:
            ax_pnl.plot(trade_times, cumulative_pnl, linewidth=2, color='purple', marker='o', markersize=4)
            ax_pnl.axhline(y=0, color='gray', linestyle='--', alpha=0.5)
            ax_pnl.fill_between(trade_times, 0, cumulative_pnl,
                               where=[p >= 0 for p in cumulative_pnl],
                               alpha=0.2, color='green')
            ax_pnl.fill_between(trade_times, 0, cumulative_pnl,
                               where=[p < 0 for p in cumulative_pnl],
                               alpha=0.2, color='red')

            ax_pnl.set_ylabel('Cumulative P&L ($)', fontsize=12)
            ax_pnl.set_title('Realized P&L from Closed Trades', fontsize=12)
            ax_pnl.grid(True, alpha=0.3)
        else:
            ax_pnl.text(0.5, 0.5, 'No closed trades',
                       ha='center', va='center', transform=ax_pnl.transAxes)
    else:
        ax_pnl.text(0.5, 0.5, 'No trades executed',
                   ha='center', va='center', transform=ax_pnl.transAxes)

    ax_pnl.set_xlabel('Time', fontsize=12)

    # Format x-axis
    ax_pnl.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d %H:%M'))
    plt.setp(ax_price.get_xticklabels(), visible=False)
    plt.setp(ax_volume.get_xticklabels(), visible=False)
    plt.setp(ax_equity.get_xticklabels(), visible=False)
    plt.xticks(rotation=45, ha='right')

    # Add metrics summary as text box
    metrics_text = f"""Backtest Metrics:
    Total Return: {metrics.get('return_pct', 0):.2f}%
    Sharpe Ratio: {metrics.get('sharpe', 0):.2f}
    Max Drawdown: {metrics.get('max_drawdown', 0):.2f}%
    Total Trades: {metrics.get('trades', 0)}
    Win Rate: {metrics.get('win_rate', 0):.2f}%
    Profit Factor: {metrics.get('profit_factor', 'N/A')}
    Final NAV: ${metrics.get('final_nav', 0):,.2f}
    """

    fig.text(0.02, 0.98, metrics_text, transform=fig.transFigure,
            fontsize=10, verticalalignment='top', family='monospace',
            bbox=dict(boxstyle='round', facecolor='wheat', alpha=0.5))

    plt.tight_layout()
    return fig


def main():
    parser = argparse.ArgumentParser(description='Visualize backtest results')
    parser.add_argument('results_file', help='Path to backtest results JSON file')
    parser.add_argument('symbol', help='Symbol to plot (e.g., TSLA)')
    parser.add_argument('--start', help='Start date (YYYY-MM-DD), auto-detected if not provided')
    parser.add_argument('--end', help='End date (YYYY-MM-DD), auto-detected if not provided')
    parser.add_argument('--output', help='Output file path (default: show plot)')

    args = parser.parse_args()

    # Load backtest results
    print(f"Loading backtest results from {args.results_file}...")
    results = load_backtest_results(args.results_file, args.symbol)

    if not results.get('success', False):
        print(f"Backtest for {args.symbol} was not successful. Cannot plot.")
        return

    # Auto-detect date range from trades or equity curve
    start_date = args.start
    end_date = args.end

    if not start_date or not end_date:
        trades = results.get('trades', [])
        equity_curve = results.get('equity_curve', [])

        if trades and trades[0].get('entry_time'):
            all_times = [pd.to_datetime(t['entry_time']) for t in trades]
            if trades[-1].get('exit_time'):
                all_times.append(pd.to_datetime(trades[-1]['exit_time']))
            start_date = start_date or all_times[0].strftime('%Y-%m-%d')
            end_date = end_date or all_times[-1].strftime('%Y-%m-%d')
        elif equity_curve:
            eq_times = [pd.to_datetime(ts, unit='ms') for ts, _ in equity_curve]
            start_date = start_date or eq_times[0].strftime('%Y-%m-%d')
            end_date = end_date or eq_times[-1].strftime('%Y-%m-%d')
        else:
            print("No trades or equity curve found. Please specify --start and --end dates.")
            return

    print(f"Fetching price data for {args.symbol} from {start_date} to {end_date}...")
    price_data = fetch_price_data(args.symbol, start_date, end_date)

    if price_data.empty:
        print(f"No price data found for {args.symbol} in specified date range.")
        return

    print(f"Plotting backtest results for {args.symbol}...")
    fig = plot_backtest_results(results, price_data, args.symbol)

    if args.output:
        print(f"Saving plot to {args.output}...")
        fig.savefig(args.output, dpi=150, bbox_inches='tight')
        print("Done!")
    else:
        print("Displaying plot...")
        plt.show()


if __name__ == '__main__':
    main()
