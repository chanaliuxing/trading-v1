AI-Driven Dynamic Strategy: Plan & TODOs
=======================================

This plan sequences concrete, low-risk steps to let strategies adapt pre‑market and intraday, support per‑symbol hot‑reload, add daily replay, and perform post‑market ML optimization. Each item has clear acceptance criteria and run/verify commands.

Phase 0 — Safety Nets (DONE / VERIFY)
- [x] NY‑native time refactor completed; tests added
  - Files: llm_trader/llm_trader/time_manager.py, tests under tests/unit/
  - Verify: pytest tests -k 'time_manager_behavior or ny_time_utils' -q
- [x] LLM Trader realtime readiness (Redis 1s) checker
  - File: tools/check_llm_trader_realtime.py
  - Run: python tools/check_llm_trader_realtime.py --json
- [x] Strategy data freshness (ClickHouse) per enabled strategy/symbol
  - File: tools/check_clickhouse_strategy_freshness.py
  - Run: python tools/check_clickhouse_strategy_freshness.py --stale-sec 180 --json
- [x] Integrated into preflight
  - File: scripts/check_env.sh (prints OK/FAIL + JSON)
  - Run: make verify or bash scripts/check_env.sh

Phase 1 — Strategy Hot‑Reload + Per‑Symbol Controls (DONE / EXTEND)
- [x] Per‑symbol JSON loader with TTL/mtime cache
  - File: strategies/enhanced_model_config_loader.py
  - Accepts: strategies/configs/enhanced_ml_{SYMBOL}.json
- [x] Strategy supports per‑symbol disable + overrides (hot‑reload)
  - File: strategies/enhanced_model_signal_strategy.py
  - Feature: {"disabled": true} blocks new decisions immediately (logged)
- [ ] Add schema doc + examples for per‑symbol JSON
  - Output: doc/enhanced_ml_config_schema.md with examples for TSLA/AAPL

Phase 2 — Intraday Adaptive Engine (DONE v1 / EXTEND)
- [x] AdaptivePolicyEngine (rule‑based) with hot‑reload
  - File: strategies/enhanced_model_signal_strategy.py
  - Supports when: vix_gt, time_lt, time_ge, halt
  - Supports overrides: conf_floor, sizer_scale, halt_for_sec
- [ ] Extend conditions (gap/ret/sigma/index volatility)
  - Add gap_up_pct_ge, gap_dn_pct_ge, ret_1m_gt, spy_abs_ret_1m_gt
  - Acceptance: rules evaluated per‑tick; unit tests for each comparator
- [ ] Extend overrides (risk/order/exec)
  - risk.{stop_loss_pct,take_profit_pct,time_stop_min}, market_filter.max_spread_bps, order.exec={TWAP,POV,IOC}
  - Acceptance: overrides applied only for current tick; no global mutation
- [ ] Config samples for adaptive rules per symbol
  - Files: strategies/configs/enhanced_ml_TSLA.json, ..._AAPL.json

Phase 3 — Strategy Coach (Premarket + Intraday Suggestions)
- [x] Implement coach script to generate/update per‑symbol adaptive rules
  - File: tools/strategy_coach.py
  - Inputs: VIX, index (SPY/QQQ) 1m/5m returns, symbol gap/ATR/spread, data health
  - Outputs: Patch to enhanced_ml_{SYMBOL}.json with adaptive.rules (with audit logs)
- [x] Guardrails & approvals
  - Feature flags: autosettings.enabled, autosettings.require_approval
  - Write suggestions to runs/ai_trader/YYYY-MM-DD/suggestions/ and apply only when approved
- [x] Dry‑run mode + report
  - --dry-run prints proposed diffs per symbol

- [x] Sector-level coordination (median spread/|ret_1| triggers sector rules)
- [x] Health gating for apply (stale symbols skipped; flow script checks LLMT/CH overall)
- [x] Premarket flow script (checks + report + optional apply)
  - File: tools/premarket_coach_flow.py

Phase 4 — Daily Replay & Report
- [x] Build daily replay summarizer per strategy/symbol
  - File: tools/daily_replay.py
  - KPIs: win rate, PF, trades, DD, exposure %, avg hold, costs, slippage/latency proxies
  - Save: runs/ai_trader/{day}/replay_{strategy}.json and _summary.md
- [ ] Hook into automation
  - Crontab or make target: run after market close and before HPO pipeline

Phase 5 — Post‑Market ML Optimization (HPO)
- [x] Define HPO space for enhanced strategy
  - Map sizing, risk, market_filter params → searchable ranges per symbol
  - Doc: doc/enhanced_ml_hpo_space.md
- [x] Nightly HPO run on recent N days (WF/OOS split)
  - Script: tools/nightly_hpo.py using alpha_loop.py/backtest_ml_engine.py
  - Save best params with metrics + SHAP/feature importances if applicable
- [ ] Safe apply + audit
  - Write to strategies/configs/enhanced_ml_{SYMBOL}.json (gated by autosettings.require_approval)
  - Keep backups in strategies/configs/*.backup_YYYYMMDD_HHMMSS

Phase 6 — Multi‑Strategy Coordination
- [x] Global volatility gate
  - Single place to raise conf_floor/reduce sizer_scale across enabled strategies on high VIX or index 1m shock
- [ ] Group/sector actions
  - Use sector_map to apply coherent rules to correlated symbols
- [ ] Circuit breaker
  - If aggregate DD/trades/min crosses threshold → halt_for_sec across affected group

Phase 7 — Observability & Dashboards
- [ ] Grafana panels
  - Redis 1s readiness per symbol; CH freshness per symbol/table; adaptive actions/halts timeline; decisions volume; guardian trips
- [ ] Logs & audit
  - Append JSONL to runs/ai_trader/{day}/(adaptive_events|guardian_events|hedging_events).jsonl

Phase 8 — Rollout & Risk Management
- [ ] Feature flags
  - autosettings.enabled, autosettings.require_approval, adaptive.enabled
- [ ] Staged rollout
  - 1–2 symbols (shadow) → 5–10 symbols → full universe
- [ ] Rollback
  - One‑click revert to previous JSON backups; disable adaptive.enabled

Ownership & Checkpoints
- Owner: Strategy/ML
  - Phases 2–6
- Owner: Platform/Infra
  - Phases 0–1, 7–8, integration into CI/automation
- Weekly checkpoint
  - Demo adaptive rules live changes; replay report; HPO suggestions vs prior day

Quick Commands
- Preflight: bash scripts/check_env.sh
- LLM Trader readiness: python tools/check_llm_trader_realtime.py --json
- Strategy CH freshness: python tools/check_clickhouse_strategy_freshness.py --json
- Watch dynamic health: python tools/watch_strategy_data_health.py --check-redis --interval 30

Acceptance Criteria (MVP)
- Adaptive rules can tighten/loosen conf_floor and size in <60s without restart
- Per‑symbol disable/临停热生效并可审计回溯
- Replay JSON/summary generated daily，含关键KPI
- HPO nightly完成一次参数建议输出（不强制自动应用）

Notes
- 现已具备：NY 时间母语化、自适应 v1、per‑symbol disabled、Redis/CH 健康检查、预检整合
- 后续扩展以“轻量规则 + 可审计热更新”为先；ML 优化逐步灰度纳管
