# options_quotes_1m Gap Detection And Backfill Skeleton

## Purpose

This utility detects dates where underlying 1m bars exist but `trading.options_quotes_1m` is missing or sparse, then optionally runs a backfill skeleton.

Script:

- `scripts/options/detect_and_backfill_options_quotes_1m.py`

## Modes

### `detect`

Detection only. No data is written.

Example:

```bash
./venv/bin/python scripts/options/detect_and_backfill_options_quotes_1m.py \
  --underlying QQQ \
  --start 2026-03-23 \
  --end 2026-03-25 \
  --mode detect \
  --report-json backtest_results/options_quotes_gap_report_2026-03-23_2026-03-24.json
```

### `synth`

For each missing day, invoke the existing synthetic backfill script:

- `scripts/options/backfill_options_quotes_1m_synth.py`

This is research-only fallback, not real quote recovery.

### `csv`

For each missing day, look for a normalized CSV file and insert it into `trading.options_quotes_1m`.

Expected file naming:

- `CSV_DIR/QQQ_options_quotes_1m_2026-03-23.csv`

Expected columns:

- `ts`
- `underlying`
- `expiry`
- `strike`
- `right`
- `dte`
- `bid`
- `ask`
- `mid`
- `last`
- `volume`
- `open_interest`
- `iv`
- `delta`
- `gamma`
- `theta`
- `vega`
- `spread_bps`
- `contracts_mult`
- `source`
- `created_at`

## Detection Rules

For each day in the requested range:

- `no_bars`: no underlying 1m bars
- `missing_quotes`: bars exist but quote rows are zero
- `sparse_quotes`: bars exist but quote minute coverage is under 30 unique minutes
- `ok`: bars and quotes both present

## Example Result

For `QQQ`, `2026-03-23` to `2026-03-24`:

- `2026-03-23`: `missing_quotes`
- `2026-03-24`: `ok`

## Notes

- `--end` is exclusive.
- The script is intentionally conservative and explicit. It does not guess a vendor feed.
- For production-quality replay, prefer real quote backfill over synthetic generation.
