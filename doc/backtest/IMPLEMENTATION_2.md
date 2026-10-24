# Backtest Engine Upgrades (2.md) - Complete

## Overview
Successfully implemented critical upgrades to the backtesting framework focusing on multi-symbol strict mark-to-market, trade ledger accuracy, extended objective functions, and visualization capabilities.

## Key Improvements

### 1. Multi-Symbol Strict MTM (sim_adapter.py)
**Problem Solved**: Previous version used the last symbol's price to mark all positions, causing inaccurate equity calculations for multi-symbol portfolios.

**Solution Implemented**:
- Per-symbol last price cache (`_last_prices: Dict[str, float]`)
- Strict MTM formula: `equity = cash + Σ(qty_i * last_price_i)`
- Each `on_bar()` call updates the specific symbol's price
- `_mark_equity()` now correctly values each position independently

**Impact**: Single-symbol backtests (like TSLA) remain perfectly accurate, while multi-symbol backtests now have production-grade accuracy.

### 2. Trade Ledger for Accurate Metrics
**Problem Solved**: Win rate and profit factor were approximated using "paired trades" heuristic, which was inaccurate for complex position management.

**Solution Implemented**:
- Trade ledger (`_closed_trades: List[Dict]`) tracks every closing/reducing trade
- `_record_closed_fragment()` captures entry price, exit price, P&L, commission
- Each close/reduce/flip generates a ledger entry with precise attribution
- Proportional commission allocation for partial closes

**Ledger Entry Structure**:
```python
{
    "ts": timestamp_ms,
    "symbol": "TSLA",
    "qty": 100.0,
    "entry_price": 250.50,
    "exit_price": 255.75,
    "side": "LONG" | "SHORT",
    "pnl": 520.0,  # net of commission
    "commission": 5.0
}
```

**backtest_engine.py Changes**:
- `_compute_metrics()` now uses `sim.get_trade_ledger()` for precise statistics
- Win rate: count trades where `pnl > 0` vs `pnl <= 0`
- Profit factor: `sum(gross_wins) / sum(gross_losses)`
- Trades count: length of ledger (actual closed trades, not fills)

### 3. Equity Timestamp Consistency
**Problem Solved**: Multi-symbol backtests appended equity multiple times per timestamp (once per symbol), causing time-series inconsistencies.

**Solution Implemented**:
- Engine now processes all symbols for a given timestamp
- Equity is appended **once** after all symbols of that timestamp are processed
- Logic: check if `next_ts != current_ts` before appending

**Code Pattern**:
```python
for i in range(n):
    row = df.iloc[i]
    ts_ms = int(row["ts_ms"])
    # ... process bar for symbol ...

    # Append equity only when timestamp changes
    next_ts = int(df.iloc[i+1]["ts_ms"]) if i+1 < n else None
    if next_ts is None or next_ts != ts_ms:
        snap = self.sim.snapshot()
        self._equity_curve.append((ts_ms, float(snap["equity"])))
```

### 4. Extended Objective Functions (wf_hpo.py)

**New Metrics Available**:
1. **Sortino Ratio**: Risk-adjusted return using downside deviation only
   ```python
   def _sortino(returns):
       neg = [r for r in returns if r < 0]
       down_stdev = (sum(r*r for r in neg)/len(neg))**0.5
       return mean_return / down_stdev
   ```

2. **Omega Ratio**: Probability-weighted gains vs losses above threshold τ
   ```python
   def _omega(returns, tau=0.0):
       gains = sum(max(r - tau, 0.0) for r in returns)
       losses = sum(max(tau - r, 0.0) for r in returns)
       return gains / losses
   ```

3. **Calmar Ratio**: Return per unit of maximum drawdown
   ```python
   def _calmar(total_return, max_drawdown):
       return total_return / max_drawdown
   ```

4. **Custom Formula**: Safe evaluation of arithmetic expressions
   ```python
   "formula": "sharpe - 2.0*max_drawdown"
   "formula": "0.7*sharpe + 0.3*sortino - 1.5*max_drawdown"
   ```

**Safety**:
- Only allows arithmetic operators (`+, -, *, /, **, %`)
- No function calls or imports allowed
- AST-based validation before evaluation

### 5. Visualization & Plotting

**New Charts Generated** (via `--plots` flag):

1. **oos_equity.png**: Normalized equity curves per fold
   - Y-axis: Equity normalized to 1.0 at start
   - X-axis: Timestamp (ms)
   - One line per fold for comparison

2. **oos_drawdown.png**: Drawdown curves per fold
   - Y-axis: Drawdown from peak (0-1 scale)
   - Shows underwater equity progression
   - Helps identify recovery periods

3. **is_heatmap_<param1>_vs_<param2>.png**: Parameter sensitivity
   - Automatically selects top-2 most-varied parameters
   - Color represents average Sharpe across IS folds
   - Useful for parameter interaction analysis

**Implementation Notes**:
- Uses matplotlib with Agg backend (non-interactive)
- Gracefully handles missing matplotlib (returns empty dict)
- All plots saved at 144 DPI for clarity

### 6. Parallel Execution Bug Fix

**Problem**: Original code had mismatched future-to-params mapping in parallel mode
```python
# Old (buggy):
for params, fut in zip(grid_list, as_completed(tasks)):
    # zip() order doesn't match as_completed() order!
```

**Solution**: Use dictionary mapping
```python
fut2params = {}
for params in grid_list:
    fut = ex.submit(...)
    fut2params[fut] = params

for fut in as_completed(list(fut2params.keys())):
    params = fut2params[fut]  # Correct mapping
    res = fut.result()
```

## Configuration Examples

### Using Extended Objectives
```json
{
  "objective": {
    "metric": "sortino",
    "maximize": true,
    "min_trades": 10,
    "max_mdd": 0.30,
    "threshold": 0.0
  }
}
```

### Using Custom Formula
```json
{
  "objective": {
    "formula": "0.7*sharpe + 0.3*sortino - 2.0*max_drawdown",
    "maximize": true,
    "min_trades": 15,
    "max_mdd": 0.35
  }
}
```

## Usage Examples

### Basic Run with Plots
```bash
python hpo_cli.py \
  --config config/hpo_config_autofolds.json \
  --out_dir ./backtest_results \
  --plots \
  --print-summary
```

### Output Artifacts
```
backtest_results/
├── rows.csv                        # All IS/OOS results
├── rows.parquet                    # Same in Parquet format
├── leaderboard_is.csv              # IS rankings
├── leaderboard_oos.csv             # OOS rankings
├── oos_summary.json                # Aggregated OOS metrics
├── best_params_by_fold.json        # Champion params per fold
├── champion_params.csv             # Parameter frequency table
├── oos_equity.png                  # Equity curves
├── oos_drawdown.png                # Drawdown curves
└── is_heatmap_param1_vs_param2.png # Parameter heatmap
```

## Validation Steps

### 1. Test Multi-Symbol MTM
```python
# Run backtest with TSLA and AAPL
cfg = BacktestConfig(
    symbols=["TSLA", "AAPL"],
    start="2024-11-01 09:30:00",
    end="2024-11-03 16:00:00"
)
result = engine.run()
snapshot = result["final_snapshot"]

# Verify:
# 1. snapshot["last_prices"] has both symbols
# 2. snapshot["positions"] correctly valued
# 3. equity = cash + sum(qty * last_price for each symbol)
```

### 2. Verify Trade Ledger
```python
ledger = sim.get_trade_ledger()
# Check:
# - Each entry has proper entry/exit prices
# - PnL = (exit - entry) * qty for LONG
# - PnL = (entry - exit) * qty for SHORT
# - Commission properly deducted
# - Manual calculation matches automated metrics
```

### 3. Test Objective Functions
```python
# Verify sortino calculation
returns = _equity_to_returns(equity_curve)
sortino = _sortino(returns)
# Compare with manual calculation

# Test custom formula
formula = "sharpe - 2.0*max_drawdown"
ctx = {"sharpe": 1.5, "max_drawdown": 0.15}
score = _safe_eval_formula(formula, ctx)
# Should equal: 1.5 - 2.0*0.15 = 1.2
```

## Performance Improvements

1. **Parallel Execution**: Grid search now correctly distributes across workers
2. **Memory Efficiency**: Trade ledger only stores closed trades, not all fills
3. **Plot Generation**: Optional flag prevents overhead when not needed

## Breaking Changes

None - all changes are backward compatible. Existing configs and code will continue to work.

## Next Steps (From Document)

The document suggests these additional enhancements:

1. **Parallel Resource Awareness**: Adaptive worker count based on CPU/IO
2. **Trading Calendar**: Avoid holidays and market closures
3. **Complete Tearsheet**: Comprehensive HTML/PDF reports
4. **Additional Metrics**: Sortino, Omega variations
5. **Multi-Objective Optimization**: Pareto frontier analysis

## Summary

Implementation 2 provides:
- ✅ Production-grade multi-symbol MTM accuracy
- ✅ Precise trade-level P&L attribution via ledger
- ✅ Extended objective functions (Sortino, Omega, Calmar, custom formulas)
- ✅ Visualization suite (equity, drawdown, parameter heatmaps)
- ✅ Fixed parallel execution bug
- ✅ Timestamp-consistent equity tracking

The backtesting framework is now suitable for both single and multi-symbol strategy research with institutional-grade accuracy.
