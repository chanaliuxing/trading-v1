# QQQ Option Replay 2026-03-24

## Purpose

This document records the successful replay run for the `ma10_reject_0324` option strategy on `QQQ` for trading day `2026-03-24`.

## Command

Use the next calendar day as `--end`, because the replay loader treats `end` as an exclusive upper bound.

```bash
./venv/bin/python scripts/backtest_option_replay.py \
  --start 2026-03-24 \
  --end 2026-03-25 \
  --strategy ma10_reject_0324 \
  --out-dir backtest_results/qqq_option_replay_today
```

## Result

Run status:

- success

Summary:

- trading day: `2026-03-24`
- strategy: `ma10_reject_0324`
- symbol: `QQQ`
- trades: `0`
- realized pnl: `0.00`
- win rate: `0.00%`
- skipped signals: `0`
- final cash: `100000.00`
- max drawdown: `0.00%`

Interpretation:

- the replay did load valid `QQQ` 1m bars and option quotes for the day
- there were no qualifying entries under the current strategy rules and execution model
- `0` trades here means "no signal confirmed and filled", not "data missing"

## Output Files

Output directory:

- `backtest_results/qqq_option_replay_today`

Files produced:

- `summary.json`
- `trades.csv`
- `skips.csv`
- `entry_audit.csv`
- `selection_audit.csv`
- `selection_candidates.csv`
- `bracket_audit.csv`
- `orders.csv`
- `fills.csv`
- `cash_ledger.csv`
- `equity_curve.csv`

## Important Note

Do not run the same day with:

```bash
--start 2026-03-24 --end 2026-03-24
```

That returns no bars, because the loader query uses:

- `ts_start >= start`
- `ts_start < end`

So for a single-day replay, `end` must be the next day.
