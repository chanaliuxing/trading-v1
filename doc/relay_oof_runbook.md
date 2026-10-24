# Relay OOF Runbook

Operational checklist for the LSTM → XGBoost relay upgrade. Follow these steps when (1) setting up the pipeline, (2) running regular backfills, and (3) responding to alerts.

## 1. One-Time Initialization
- Execute the ClickHouse DDL at `db/clickhouse/oof_lstm_schema.sql` to create `trading.oof_lstm_h5`, the `features_1m_with_oof` view, and the `trading.runs` experiment log.
- Confirm `features_1m_with_oof` returns rows with the expected `lstm_oof_h5` columns:  
  `SELECT * FROM trading.features_1m_with_oof WHERE symbol='TSLA' LIMIT 5;`

## 2. Daily / Hourly Batch Generation
- Produce strict OOF predictions per symbol (adjust window arguments as needed):
  ```bash
  python -m pipelines.lstm_oof_to_clickhouse \
    --symbol TSLA \
    --start 2024-01-01 --end 2025-10-01 \
    --seq_len 60 --folds 5 --embargo_min 10 --epochs 3
  ```
- The script deletes any previous rows for the same `run_id` and inserts the new batch into `trading.oof_lstm_h5`.
- Verify freshness and coverage after each run:
  ```bash
  python -m scripts.relay_health --symbol TSLA --coverage_window "6 HOURS"
  ```

## 3. Training and Evaluation
- Train the XGBoost stage, optionally toggling relay features:
  ```bash
  python -m pipelines.train_xgb_with_oof \
    --symbol TSLA \
    --start 2024-06-01 --end 2025-10-01 \
    --use_relay 1 --rounds 300 --splits 5
  ```
- For automated HPO, run `alpha_loop.py` with a config that enables the relay space. The Optuna integration calls the OOF pipeline whenever `use_relay=1` and records results in `trading.runs`.

## 4. Monitoring and SLOs
- Import the Grafana dashboard JSON at `dashboards/relay_oof_overview.json`.
- Key widgets:
  - OOF coverage percentage over time (`features_1m_with_oof`).
  - Freshness lag in minutes (`oof_lstm_h5`).
  - Latest run_id per symbol and per-run row counts.
  - Relay vs. Base AUC trend from `trading.runs`.
- Alert thresholds:
  - Coverage < 95 % over the last 1 h.
  - OOF lag > 30 m.
  - Sharp rise in order rejections after enabling relay.

## 5. Rollback and Recovery
- Roll back a bad OOF batch:
  ```sql
  ALTER TABLE trading.oof_lstm_h5 DELETE WHERE run_id = 'oof_20240101_bad';
  ```
  The `features_1m_with_oof` view automatically falls back to the previous version.
- Disable relay features in production by flipping the strategy/config flag (e.g. `use_relay=false`).
- Re-run the OOF job once root cause is fixed, confirm coverage, then re-enable relay.

## 6. Troubleshooting Guide
- **No OOF values in view:** `SELECT COUNT(*) FROM trading.features_1m_with_oof WHERE lstm_oof_h5 IS NOT NULL;`  
  - If zero, ensure the latest insert completed and that timestamps align.
- **Stale data:** inspect `updated_at` from `trading.oof_lstm_h5` via `scripts.relay_health`.
- **Potential leakage:** confirm the batch job ran with `embargo_min > 0` and that only validation folds are written (the pipeline enforces this by design).
- **Performance issues:** reduce `folds`, `epochs`, or `seq_len`, or move the job onto a GPU node.

## 7. On-Call Quick Checks
1. Run `python -m scripts.relay_health --coverage_window "1 HOUR"` to inspect coverage and lag.
2. In Grafana, open “Relay OOF Overview” and review:
   - Last OOF version (`run_id`) and insert timestamp.
   - Coverage heatmap and null-count table.
   - Relay vs. Base AUC trend for the impacted symbol.
3. If coverage falls below threshold, execute the rollback command above, notify trading, and kick off a fresh OOF job.
4. Record incident notes (run_id, git commit, config hash, mitigation steps) in the ops log.

## 8. References
- `pipelines/lstm_oof_to_clickhouse.py`
- `pipelines/train_xgb_with_oof.py`
- `alpha_loop.py` relay integration
- `scripts/relay_health.py`
- `db/clickhouse/oof_lstm_schema.sql`
