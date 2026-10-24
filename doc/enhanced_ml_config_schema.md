Enhanced ML Per-Symbol Config Schema
====================================

Location: strategies/configs/enhanced_ml_{SYMBOL}.json

Top-level keys
- autosettings: { enabled: bool, require_approval: bool }
  - Coach (--apply) only updates file when enabled=true; requires --force if require_approval=true.
- symbols: ["TSLA"] or symbol: "TSLA"
- bar_sec: 60 | 300
- ensemble: { mode: "weighted_avg"|"vote"|"max_conf", models: [{name, weight}] }
- sizing: { mode: "fixed"|"confidence_scaled"|"vol_target"|"kelly", ... }
- market_filter: { conf_floor: float, max_spread_bps: float, min_volume: int, max_vix: float, check_after_hours: bool }
- risk: { take_profit_pct: float, stop_loss_pct: float, time_stop_min: int, partial_exit_pct?: float }
- order defaults: { order_type: "MKT"|"LMT", limit_offset_bps: float, lot: int, exec?: { mode: "TWAP"|"POV"|"IOC", horizon_s?, participation? } }
- per_symbol / per_sector: nested overrides with same keys as above
- sector_map: { "TSLA": "Information Technology", ... }
- hedging: { enabled: bool, confidence_threshold: float, hedge_instrument: "SPY", beta: float, per_symbol?: {}, per_sector?: {} }
- adaptive: { enabled: bool, rules: [ { when: {}, overrides: {} } ] }
- disabled: true|false (hard-disable this symbol)

Adaptive.when (supported v1)
- vix_gt: float                # VIX > threshold
- time_lt: "HH:MM"            # local NY time < HH:MM
- time_ge: "HH:MM"            # local NY time ≥ HH:MM
- gap_up_pct_ge: float        # gap% ≥ threshold (e.g., 3.0 for 3%)
- gap_dn_pct_ge: float        # gap% ≤ -threshold
- ret_1m_gt: float            # per-symbol 1m return > threshold (e.g., 1.0 for 1%)
- spy_abs_ret_1m_gt: float    # |SPY| (or QQQ) 1m abs return > threshold (e.g., 0.8 for 0.8%)
- halt: true                  # explicit halt

Adaptive.overrides (supported v1)
- conf_floor: float           # temporary MarketFilter conf threshold
- max_spread_bps: float       # temporary max spread gate
- sizer_scale: float          # multiply target size (0..3)
- halt_for_sec: int           # guardian halt duration
- risk_take_profit_pct: float # per-tick RiskManager TP
- risk_stop_loss_pct: float   # per-tick RiskManager SL
- risk_time_stop_min: int     # per-tick RiskManager time stop
- exec_mode: "TWAP"|"POV"|"IOC" (+ exec_horizon_s / exec_pov)

Notes
- Adaptive overrides are applied per-tick and do not mutate global config.
- For gap/ret/spy signals, features must supply keys: gap_pct, ret_1m, spy_ret_1m (or qqq_ret_1m).
- Coach (tools/strategy_coach.py) can generate suggestions and optionally apply them (with backups).
