Enhanced ML Strategy HPO Space (Draft)
======================================

Purpose
- Define parameter ranges for nightly optimization (per symbol) using WF/OOS backtesting.

Targets
- sizing:
  - fixed.base_qty: [50, 100, 150, 200]
  - fixed.max_qty: [500, 1000, 1500]
  - confidence_scaled.multiplier: [1.0, 1.5, 2.0, 2.5]
- market_filter:
  - conf_floor: [0.5, 0.55, 0.6, 0.65, 0.7]
  - max_spread_bps: [8, 10, 12]
- risk:
  - take_profit_pct: [0.01, 0.015, 0.02, 0.025]
  - stop_loss_pct: [0.008, 0.01, 0.012, 0.015]
  - time_stop_min: [30, 45, 60, 90]

Scoring
- Maximize risk-adjusted return with constraints:
  - Min PF ≥ 1.2
  - Max intraday DD ≤ 3%
  - Min trades ≥ 10/day

Workflow
1) Select last N trading days (e.g., 20 days), split WF/OOS = 70/30.
2) Random search K configs (e.g., 50) per symbol.
3) Backtest with `backtest_ml_engine.py` (pass features DataFrame from ClickHouse)
4) Rank by composite score; keep Top‑K suggestions per symbol.
5) Save to runs/ai_trader/{day}/hpo_suggestions/{symbol}.json
6) (Optional) Apply via autosettings when approved.

Notes
- This draft uses discrete grids for simplicity; integrate Optuna/Bayes later.
- Keep per-symbol ranges in `strategies/configs/enhanced_ml_{SYMBOL}.json` if customization is needed.
