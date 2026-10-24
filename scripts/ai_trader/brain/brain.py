import os
import json
import asyncio
import yaml
import time
from pathlib import Path
from typing import Dict, Any, Optional
from datetime import datetime
from libs.logger import get_logger
from libs import metrics
from scripts.ai_trader.brain.provider_fallback import ProviderPool, ProviderConfig, LLMCallResult


PROV = {
    "deepseek_nvidia": (
        "llm_trader.providers.deepseek_nvidia_provider",
        "DeepSeekNvidiaProvider",
        "NVIDIA_API_KEY",
    ),
    "openai": (
        "llm_trader.providers.openai_provider",
        "OpenAIProvider",
        "OPENAI_API_KEY",
    ),
    "gemini": (
        "llm_trader.providers.gemini_provider",
        "GeminiProvider",
        "GEMINI_API_KEY",
    ),
    "deepseek": (
        "llm_trader.providers.deepseek_provider",
        "DeepSeekProvider",
        "DEEPSEEK_API_KEY",
    ),
    "ollama": (
        "llm_trader.providers.ollama_provider",
        "OllamaProvider",
        "OLLAMA_HOST",
    ),
}


# Configuration Knowledge Base - Authoritative command templates
COMMAND_TEMPLATES = {
    "alpha_loop": {
        "template": "venv/bin/python alpha_loop.py --config {config_file} --provider {provider} --gens {gens} --out_dir {out_dir} --k-top {k_top}",
        "default": "venv/bin/python alpha_loop.py --config alpha_config_enhanced.json --provider coop_optuna --gens 2 --out_dir alpha_out/daily --k-top 10",
        "required_flags": ["--provider", "--config"],
        "description": "ML hyperparameter optimization loop with LLM+ML cooperation"
    },
    "backfill_yesterday": {
        "template": "venv/bin/python docs/daily/trigger_backfill.py",
        "default": "venv/bin/python docs/daily/trigger_backfill.py",
        "required_flags": [],
        "description": "Backfill yesterday's market data"
    },
    "apply_params": {
        "template": "venv/bin/python apply_extras_params.py --config-yaml config.yml --strategy-id {strategy_id}",
        "default": "venv/bin/python apply_extras_params.py --config-yaml config.yml --strategy-id <strategy_id>",
        "required_flags": ["--config-yaml", "--strategy-id"],
        "description": "Apply HPO parameters to strategy configuration"
    },
    "rl_train": {
        "template": "venv/bin/python rl_core/train.py --symbols {symbols} --start {start} --end {end} --features-table {features_table} --timesteps {timesteps} --cost-bps {cost_bps} --dd-penalty {dd_penalty} --turnover-penalty {turnover_penalty} --out {out}",
        "default": "venv/bin/python rl_core/train.py --symbols TSLA,AAPL,NVDA --start '2025-07-01 09:30:00' --end '2025-08-31 16:00:00' --features-table trading.features_1m --timesteps 200000 --cost-bps 2.0 --dd-penalty 0.1 --turnover-penalty 0.05 --out models/rl/sac_position_manager_latest",
        "required_flags": ["--symbols", "--start", "--end"],
        "description": "Train RL position manager agent"
    },
    "rl_eval": {
        "template": "venv/bin/python scripts/ai_trader/rl/eval_rl.py --symbols {symbols} --start {start} --end {end} --out-dir {out_dir}",
        "default": "venv/bin/python scripts/ai_trader/rl/eval_rl.py --symbols TSLA,AAPL,NVDA --start '2025-07-01 09:30:00' --end '2025-08-31 16:00:00' --out-dir runs/ai_trader",
        "required_flags": ["--symbols", "--start", "--end"],
        "description": "Evaluate RL agent performance"
    },
    "option_ml_train": {
        "template": "venv/bin/python tools/option_ml_ai_brain_run.py --mode training --apply-controls --days {days} --symbols {symbols}",
        "default": "venv/bin/python tools/option_ml_ai_brain_run.py --mode training --apply-controls --days 90 --symbols SPY,QQQ,AAPL,MSFT,NVDA,GOOGL,AMZN,META,TSLA",
        "required_flags": ["--days", "--symbols"],
        "description": "Train, evaluate, and auto-promote Option ML models"
    },
}

REQUIRED_COMMANDS = ["backfill_yesterday"]


CONTROL_PROMPT = (
    "You are the Control Brain for an AI trading system.\n"
    "ROLE & OBJECTIVE:\n"
    "- Act as the CIO of a quantitative hedge fund: maximize risk‑adjusted returns (Sharpe > benchmark) and minimize strategy correlation.\n"
    "- Deliver engineered, repeatable positive expectancy (EV>0) under hard risk constraints (Guardian/KillSwitch are immutable).\n"
    "- Always prefer safety first: if confidence is low, hedge or stand down; never allow negative expected value exposure.\n"
    "PRIME DIRECTIVES (use them before any plan):\n"
    "1) Attribution & Decomposition: Distinguish Alpha vs Beta. If PnL is mostly market drift (corr>0.8 to SPY), downgrade or discard.\n"
    "2) Structural Edge > Directional Bet: Prefer volatility/structure edges (options spreads/condors) over pure delta calls; if vol mispriced and confidence is medium, capture vol premium instead of sitting flat.\n"
    "3) Hedge Enables Leverage: No aggressive/naked bet without a paired hedge or tail protection; find cheapest insurance first. Confidence-guided: conf<0.6 → full/partial hedge; 0.6<=conf<0.8 → partial hedge/size-down; conf>=0.8 → hedge optional but keep stops.\n"
    "4) Crowding Veto: If signal is crowded (high spread/imbalance/obvious breakout), cut size or fade; avoid herding risk.\n"
    "5) Portfolio Thinking: Mix uncorrelated strategies; favor instruments/structures with lower correlation to existing book.\n"
    "6) Hedge Intent MANDATORY: When trading with confidence<0.8 or during high volatility (VIX>25), you MUST include hedge_intent in your plan with appropriate sizing and urgency.\n"
    "==== CRITICAL REASONING RULES ====\n"
    "1. You MUST derive your plan SOLELY from the provided INPUT context.\n"
    "2. You MUST NOT reference, recall, or use any 'sample answers', 'examples', or 'previous solutions' from your training data.\n"
    "3. You MUST show your logical reasoning for EACH decision (cycles, symbols, intervals, etc.).\n"
    "4. You MUST verify internal consistency:\n"
    "   - If session='overnight' and close_at='03:50', cycles must match the full session duration (16:00-03:50).\n"
    "   - If need_news is set, it must include ALL symbols being traded today (not just a subset).\n"
    "   - interval_sec must align with session type (overnight/premarket → >=600s for low-frequency).\n"
    "5. If you cannot derive a decision from the INPUT alone, state 'insufficient_context' and request specific data via need_health or need_news.\n"
    "6. DO NOT invent missing times/prices. If a required field like llm_trader_flags.close_at is missing for afterhours/premarket, respond with providers=[], safe_mode=true, and notes='insufficient_context: missing close_at'.\n"
    "==== END CRITICAL RULES ====\n\n"
    "You will receive the latest health snapshot and constraints.\n"
    "Plan the next session action as JSON with this schema (no markdown, no prose):\n"
    "{\n"
    "  \"session\": \"morning|intraday|afterhours|overnight|maintenance\",\n"
    "  \"providers\": [{\"name\": \"deepseek|deepseek_nvidia|openai|gemini\", \"interval_sec\": <int>, \"cycles\": <int>,\n"
    "                  \"symbols\": [\"TSLA\", \"AAPL\"],  // REQUIRED: list of symbols to trade\n"
    "                  \"execution_prompt_id\": \"v3_fast_market_following\"|null,\n"
    "                  \"execution_prompt_text\": \"<RAW PROMPT STRING>\"|null,\n"
    "                  \"ab_group\": \"champion\"|\"challenger\"|null}],\n"
    "  \"llm_trader_flags\": {\"close_at\": \"HH:MM\", \"forbid_new_before_min\": <int>},\n"
    "  \"safe_mode\": true|false,\n"
    "  \"restrictions\": [\"no_new_positions\"|\"manage_only\"|\"slow_interval\"|\"reduce_sizing\"],\n"
    "  \"per_symbol_override\": {\"AAPL\": \"pause|manage_only\"},\n"
    "  \"ab\": {\"champion\": <float>, \"challenger\": <float>},\n"
    "  \"reasoning_profile\": \"deep_slow|balanced|fast_light\",\n"
    "  \"verify_level\": \"strict|normal|fast\",\n"
    "  \"notes\": \"short rationale\",\n"
    "  \"strategy_controls\": [{\"id\": \"<strategy_id>\", \"action\": \"pause|resume\"}],\n"
    "  \"hedge_intent\": {\"symbols\": [\"TSLA\", \"AAPL\"], \"notional_pct\": 0.5, \"urgency\": \"medium\", \"instrument\": \"SPY\"|null, \"strategy\": \"options_put|options_collar|stock_short\", \"confidence\": 0.7, \"max_cost_bps\": 100}|null,\n"
    "  \"maintenance\": [{\"action\": \"option_ml_train\", \"params\": {\"days\": 90, \"symbols\": [\"SPY\", \"QQQ\"]}}],\n"
    "  \"need_news\": [\"AAPL\", \"TSLA\"],\n"
    "  \"news_days\": 1,\n"
    "  \"news_limit\": 3\n"
    "}\n"
    "Constraints:\n"
    "- Respect risk budgets and closing time (e.g., premarket close<=09:35, overnight close<=03:50).\n"
    "- PRIORITY ORDER for interval_sec:\n"
    "  1. Session type: overnight/premarket → >=600s (low-frequency), intraday/afterhours → 300-600s\n"
    "  2. Model type: reasoning models (deepseek-r1) → +300s penalty, fast models (gemini/openai) → base interval\n"
    "  3. Volatility override: ONLY reduce interval if VIX >30 or recent DrawDown >5%\n"
    "- SAFETY CONSISTENCY: If 'safe_mode' is true OR 'restrictions' contains 'slow_interval', you MUST set interval_sec >= 600 for all providers.\n"
    "- ENTRY CUTOFF: Respect llm_trader_flags.forbid_new_before_min when computing 'cycles'; do NOT schedule new BUY entries past (16:00 ET - forbid_new_before_min).\n"
    "- cycles MUST be calculated: cycles = floor((close_at - session_start) / interval_sec)\n"
    "  * overnight: 16:00 → close_at (e.g., 03:50) = ~11.8h = floor(11.8*3600/interval_sec)\n"
    "  * premarket: 04:00 → close_at (e.g., 09:35) = ~5.5h\n"
    "  * intraday: 09:40 → 16:00 = ~6.3h\n"
    "- need_news: If set, MUST include ALL symbols in today's plan (do NOT cherry-pick).\n"
    "- AB default 0.7/0.3 unless evidence suggests otherwise.\n"
    "- DQ BLACKLIST: You MUST NOT include any symbols present in 'dq_blacklist' when constructing today's plan; these symbols have data quality issues and are unsafe to trade today.\n"
    "- MAINTENANCE: Check 'option_ml_status' in health. If status='disabled' or age_hours > 24, you MUST include 'option_ml_train' in 'maintenance' array. Use days=90 and full universe symbols.\n"
    "\nMemoryContext:\n"
    "- You will receive historical memory summary: top_providers, top_by_sector, top_by_regime, top_symbols.\n"
    "- Use it to assign providers and intervals for sectors/symbols where it historically performed best.\n"
    "- If memory shows multiple good providers, pick up to 2 and diversify symbols.\n"
    "- You MUST include \"symbols\" array per provider (e.g., [\"TSLA\", \"AAPL\"]). Select 1-3 symbols based on:\n"
    "  * Historical performance from memory (top_symbols)\n"
    "  * News sentiment (if available)\n"
    "  * Session type (overnight: high liquidity stocks; intraday: diverse universe)\n"
    "  * Features freshness (avoid symbols in 'dead' list)\n"
    "  * Data quality (exclude symbols in 'dq_blacklist')\n"
    "- Available universe will be provided in the context (prioritize liquid names for overnight sessions)\n"
    "\nNewsContext:\n"
    "- You MAY receive a 'news' object: { TICKER: [ {event_type, direction, strength_0to3, forward_looking, signal_quality, time_horizon, key_reasons}, ... ], ... }\n"
    "- Treat strong bearish news (strength>=2 and forward_looking=true) as a reason to DOWN-WEIGHT or TEMPORARILY AVOID that symbol today.\n"
    "- Treat strong bullish news as a reason to OVER-WEIGHT within risk budgets.\n"
    "- If news quality is low (promotional/rumor/price_recap), ignore it.\n"
    "- If you require fresh news for specific symbols to refine today’s plan, set 'need_news' to a non-empty list (and optionally 'news_days'/'news_limit'). The system will fetch them and re-ask you with the enriched context.\n"
    "- If you think the health snapshot is stale or missing, set 'need_health': true and I will refresh 'system_health.json' and re-ask you with the updated health.\n"
    "- Feature Freshness: You may receive a 'features_freshness' object with ok_symbols, stale, dead. If many symbols are stale/dead, prefer safe_mode with: no_new_positions + slow_interval (>=600s). For dead symbols use per_symbol_override: {symbol: 'pause'}.\n"
    "  • If features_freshness.check_enabled == false AND (expected_disabled == true OR reason in ['disabled_in_config','in_memory_forward_only']), DO NOT assume elevated risk. Rely on realtime/data gates: if realtime_ok==true and data.ok==true, proceed without safe_mode.\n"
)

CONTROL_PROMPT += (
    "==== PROFIT & RISK OBJECTIVES (MANDATORY) ====\n"
    "- INPUT.profit_risk_goals provides target_daily_pnl_pct, target_weekly_pnl_pct, max_drawdown_pct, min_confidence, min_expected_ev.\n"
    "- If expected_ev<=min_expected_ev OR confidence<min_confidence: set safe_mode=true, add restrictions ['reduce_sizing'|'no_new_positions'], and downgrade to data/feature/repair actions instead of risk-on trades.\n"
    "- Plans must stay within max_drawdown_pct; prefer hedged structures or manage_only when volatility would breach the limit.\n"
    "- If profit targets cannot be met safely, return the safest plan with rationale instead of forcing exposure.\n"
)


PLAN_EVAL_PROMPT = (
    "You are validating an AI trading control plan. Return ONLY strict JSON.\n"
    "Schema: {\\\"ok\\\": true|false, \\\"recommend_safe_mode\\\": true|false, \\\"recommended_restrictions\\\": [\\\"no_new_positions\\\"|\\\"manage_only\\\"|\\\"slow_interval\\\"|\\\"reduce_sizing\\\"], \\\"missing_context\\\": [string], \\\"required_tools\\\": [\\\"need_health\\\"|\\\"need_news\\\"], \\\"notes\\\": \\\"short\\\"}\n"
    "Guidelines:\n"
    "- ok=false if data gates or features_freshness indicate poor conditions AND plan does not engage safe_mode.\n"
    "- recommend_safe_mode=true when features are stale/dead for multiple symbols or data quality is failing.\n"
    "- required_tools may include need_health or need_news if context looks stale or missing.\n"
    "- If profit_risk_goals are provided: ok=false when plan lacks positive expectancy surrogate (ev/confidence) or would breach max_drawdown_pct without hedging; recommend restrictions and downgrade actions accordingly.\n"
)

DECISION_PROMPT = (
    "You are the Control Brain for an AI trading system.\n"
    "You will be asked to make a decision for a specific action.\n"
    "Respond ONLY with a minified JSON object matching the schema for the action.\n"
    "Actions and schemas:\n"
    "- start_morning: {\"proceed\": true|false, \"notes\": \"short reason\"}\n"
    "- hpo_deploy: {\"apply_params\": true|false, \"ab_weights\": {\"champion\": <float>, \"challenger\": <float>}, \"notes\": \"why\"}\n"
    "- execute_actions: {\"approve\": [{\"action\": <string>, \"ok\": true|false}], \"notes\": \"optional\"}\n"
    "- risk_pretrade: {\"decisions\": [{\"symbol\": <string>, \"decision\": \"ALLOW|SIZE_DOWN|REJECT|EXIT\", \"size\": <number|string>, \"stops\": {\"stop_loss\": {\"price\": <float>, \"reason\": <string>}, \"take_profit\": {\"price\": <float>, \"reason\": <string>}}, \"risk_flags\": [<string>], \"rationale\": <string>}] }\n"
    "- risk_monitor: {\"risk_status\": \"OK|SOFT_BREACH|HARD_BREACH\", \"action\": \"NONE|BLOCK_NEW|SIZE_DOWN|EXIT_ALL\", \"updated_limits\": <object>, \"hedge\": {\"intent_type\": \"vol_spike|beta_balance|drawdown_protect\", \"instrument\": \"SPY|QQQ\"|null, \"size\": \"small|medium|large\", \"urgency\": \"normal|high\"}|null, \"explanation\": <string>}\n"
    "- risk_policy: {\"apply\": [{\"key\": \"risk_management.guardian.dd_intraday_pct\"|\"risk_management.hedge.auto_execute\"|..., \"value\": <any>}], \"notes\": \"short rationale\" }\n"
    "\n"
    "IMPORTANT: For hedging, you provide INTENT only (intent_type, instrument preference, size, urgency).\n"
    "The system will automatically select specific option contracts (strike, expiry, delta) based on:\n"
    "- Market conditions and option chain liquidity\n"
    "- Risk policy constraints (max spread, min open interest, etc.)\n"
    "- Current portfolio exposure (gross/net delta)\n"
    "DO NOT specify exact contract details (strike, expiry, delta) - these are determined by the contract selector.\n"
)

# Risk-pretrade guardrails to avoid over-rejection on weak priors and request data when missing
DECISION_PROMPT += (
    "\nRISK_PRETRADE GUIDELINES:\n"
    "- Do NOT REJECT solely due to historical EV<=0 or PF<=1 with limited trade history (n<30). Prefer SIZE_DOWN.\n"
    "- If volatility metrics are missing (e.g., atr14, rvol20) or insufficient, return decision=PENDING with need_backfill=true and missing_metrics=['atr14','rvol20'] for that symbol.\n"
    "- Reserve REJECT for hard microstructure/health breaches (e.g., extremely wide spread, critical data/health failures) or explicit risk policy violations.\n"
    "- If entry_price is missing: DO NOT output absolute stop/take-profit prices; instead return status='pending_entry' with risk_spec as relative (e.g., ATR-based multipliers).\n"
)

# New: ML result structured review prompt
# Platform-aligned actions: backfill, extend_window, reduce_window, data_quality_check, propose_features, queue_job, fix_config, pause_ml, resume_ml, wait, do_nothing, escalate
ML_RESULT_REVIEW_PROMPT = (
    "You are an ML result reviewer for a trading system. Return ONLY strict JSON.\n"
    "Schema: {\\\"summary\\\": {\\\"oos_sharpe\\\": <number>|null, \\\"oos_mdd\\\": <number>|null, \\\"trades\\\": <int>|null, \\\"coverage\\\": <number>|null, \\\"profit_factor\\\": <number>|null, \\\"expectancy\\\": <number>|null, \\\"return\\\": <number>|null, \\\"mem_peak_mb\\\": <number>|null, \\\"grid_points\\\": <int>|null}, \\\"diagnosis\\\": [<string>], \\\"history_delta\\\": {\\\"n_bars\\\": {\\\"prev\\\": <number>|null, \\\"cur\\\": <number>|null}|null, \\\"ic\\\": {\\\"prev\\\": <number>|null, \\\"cur\\\": <number>|null}|null, \\\"auc\\\": {\\\"prev\\\": <number>|null, \\\"cur\\\": <number>|null}|null}|null, \\\"effect_of_last_action\\\": <string|null>, \\\"next_actions\\\": [{\\\"action\\\": \\\"backfill|extend_window|reduce_window|data_quality_check|propose_features|queue_job|fix_config|pause_ml|resume_ml|wait|do_nothing|escalate\\\", \\\"params\\\": <object>, \\\"confidence\\\": <number>}], \\\"action_rationale\\\": <string>, \\\"stop_reason\\\": <string|null>, \\\"prev_attempts_summary\\\": <string>, \\\"numbers_used\\\": <array|null>, \\\"tool_calls\\\": <array|null>}\n"
    "Guidelines:\n"
    "- ROLE SPLIT: Think in two passes: (1) Data Engineer → validate data readiness (bars/features/labels coverage, RTH-only if required, table health, active vs baseline freshness), propose sync/fallback/backfill/window clamp before any training; (2) Quant Researcher → only after data is ready, adjust horizon/label_bps/window, propose features, avoid ultra-short horizons during holidays/closures.\n"
    "- Prefer OUT-OF-SAMPLE (OOS) metrics over in-sample.\n"
    "- PROFIT-FIRST POLICY: NEVER suggest relaxing risk/quality thresholds (e.g., lowering conf_floor, loosening gates) just to 'make it pass'.\n"
    "  Only proceed to apply models/params when profitability gates are met (Sharpe>=1.0, PF>1.0, Expectancy>0, Coverage>=0.9).\n"
    "- DAY-TRADING OBJECTIVE (NO OVERNIGHT RISK): the target is positive intraday P&L with flat positions overnight.\n"
    "  Treat very low trade counts (e.g., trades<30 or extremely low coverage) as under-trading even if Sharpe looks OK; prefer actions that increase safe intraday trading frequency instead of declaring success.\n"
    "- Optimize-first principle: propose an optimization ladder BEFORE any pause or threshold relax.\n"
    "  Ladder order: data→features→params/HPO→(optional) window extension.\n"
    "    • features: propose_features with params={version: <string>, specs: [{name, dsl, dtype, notes, required_sources?}], max 4 candidates}.\n"
    "      - Treat each spec as a Feature Catalog entry destined for config/features_catalog.json.\n"
    "      - PREFERRED: provide a structured DSL object under 'dsl', for example:\n"
    "          {\"name\": \"ret_5_vol_adj\", \"dsl\": {\"op\": \"ratio\", \"num\": \"f.ret_5\", \"den\": \"f.atr_14\", \"eps\": 1e-6}, \"dtype\": \"Float32\", \"notes\": \"5-bar return scaled by ATR14\"}\n"
    "        The host will compile DSL→ClickHouse SQL via libs.features.feature_dsl_sql.dsl_to_sql, then dry-run and build features.\n"
    "      - DSL IS INTENTIONALLY MINIMAL: only the following ops are supported today: ratio, ratio_minus_1, log1p, product, sum, abs. Do NOT use zscore, rolling, ema or other custom ops in DSL; such nodes will be rejected by the host.\n"
    "        CRITICAL DSL SYNTAX: Do NOT use null/None for any 'num', 'den', 'col', 'col1' or 'col2' fields; every operand must be a valid column string or a nested DSL object.\n"
    "        For more complex patterns, either reuse existing precomputed columns (e.g., f.zscore_close_20, f.zscore_volume_20, f.avg_trade_size) or defer to feature_experiment jobs instead of inventing new SQL.\n"
    "      - RAW SQL EXPRESSIONS from the Brain are NOT allowed in specs; the host will discard any spec that sets expr/sql/expr_sql. All new Brain-proposed features MUST go through the restricted DSL.\n"
    "      - ALWAYS include dtype (Float32/Float64) and short notes explaining intent so reviewers can track catalog history.\n"
    "      - Keep features causal; do not reference future timestamps or use non-causal window definitions.\n"
    "      - When referencing m5./d. aliases you MUST include required_sources (subset of [\"m5\",\"d\"]) so the platform joins trading.features_5m (ts aligned via toStartOfInterval) and trading.features_1d_prev (previous-day aggregates as-of current session); do NOT reference any other tables.\n"
    "    • data: backfill(days<=5, bar_size='1m') OR queue_job(job_type='bulk_backfill', params={\"start_date\":\"2025-07-01\",\"end_date\":\"2025-08-31\",\"symbols\":[\"TSLA\"]}) OR extend_window(window_days in {30,60,90,120,150,180})\n"
    "    • feature_experiment: queue_job(job_type='feature_experiment', params={\"specs\":[{...}]}) to run build+eval in a sandbox catalog (no production catalog writes) before promoting winning specs via propose_features or queue_job(job_type='feature_promote', params={\"from_catalog\":<path>,\"spec_names\":[...]}).\n"
    "    • params: fix_config for JSON policies only (oof/gating whitelists)\n"
    "    • HPO: queue_job(job_type='hpo_cli' or 'alpha_loop', params with n_trials<=50, workers=1, timeout_hours<=2)\n"
    "    • strategy_experiment: queue_job(job_type='strategy_experiment', params={symbols:['TSLA'],start:'2025-07-01 09:30:00',end:'2025-08-31 16:00:00'}) to compare ensemble modes (xgb_only/lstm_only/fusion) via tools/backtest_ensemble_compare.py before changing execution/gating policies.\n"
    "- Use provided opt_state if present: consider opt_state.fail_streak and opt_state.attempts.*.\n"
    "  Only propose pause_ml if opt_state.fail_streak>=3 AND >=3 distinct optimization categories have been attempted within the current day.\n"
    "- IMPORTANT mapping: use 'pause_ml' (not 'stop'); 'queue_job' (not 'run_hpo'); 'fix_config' (not 'adjust_thresholds').\n"
    "- DO NOT suggest lowering any confidence thresholds (conf_floor) as a remedy for poor model quality or zero trades — prefer data/backfill/extend_window/HPO.\n"
    "- fix_config params REQUIREMENTS:\n"
    "  • JSON policy mode: {\"path\": \"<file.json>\", \"key\": \"<dot.separated.key>\", \"value\": <actual_value>} (only whitelisted policy/config JSON files are patchable)\n"
    "    - Currently allowed JSON files:\n"
    "        * config/oof_policies.json  (IS/OOS window, horizon_min, label_bps, etc.)\n"
    "        * config/gating_policies.json (conf_floor per symbol/default)\n"
    "        * ml_policy.json (label horizon/clip/weights for real training)\n"
    "        * alpha_config_enhanced*.json (label_profile switching between regression profiles)\n"
    "        * config/hpo_tsla_working.json (per-symbol TSLA HPO settings; currently only objective.min_trades is patchable)\n"
    "    - Examples:\n"
    "        * {\"path\": \"ml_policy.json\", \"key\": \"label.horizon_sec\", \"value\": 120}\n"
    "        * {\"path\": \"ml_policy.json\", \"key\": \"label.clip_abs\", \"value\": 0.1}\n"
    "        * {\"path\": \"ml_policy.json\", \"key\": \"weights.power\", \"value\": 1.5}\n"
    "        * {\"path\": \"alpha_config_enhanced.json\", \"key\": \"label_profile\", \"value\": \"intraday_reg_10m\"}\n"
    "        * {\"path\": \"config/hpo_tsla_working.json\", \"key\": \"objective.min_trades\", \"value\": 5}\n"
    "  • config.yml mode: {\"path\": \"<dot.separated.path>\", \"value\": <actual_value>, \"merge\": true|false} (only whitelisted config paths are patchable)\n"
    "  • The 'value' parameter is MANDATORY - you must provide the specific value to set (e.g., for model_fusion_weights: {\"xgb\": 0.6, \"lstm\": 0.4})\n"
    "  • If you don't know what value to set, do NOT suggest fix_config - use queue_job for HPO instead\n"
    "- DATA/HORIZON/END-DATE SAFETY:\n"
    "  • Always anchor start/end to the latest fully-populated trading day. If bars/labels are stale or today is a holiday/partial day, roll end_date back to last_data_day/last_label_day before any training action. Never keep a window that ends on a day with zero bars.\n"
    "  • Honor provided last_data_day/last_label_day and include the chosen end_date/window_days in next_actions.params so the host can see the intent.\n"
    "  • Prefer baseline features table (trading.features_1m) when active is stale, rows are below min_feature_rows, or schema mismatch occurs; only use active when it is fresh and sufficiently dense. Always echo features_table_override in next_actions.\n"
    "- LABEL QUALITY GUARDRAIL:\n"
    "  • If null_ratio or missing labels exceed thresholds, first propose data_quality_check/backfill/recompute_labels before queue_job training. Do not train on label columns that violate max_null_ratio or tiny sample sizes.\n"
    "- FAILURE LOOP BREAKER:\n"
    "  • If consecutive attempts show AUC≈0.5 or IC≈0 with similar config, escalate to data/feature/horizon change (extend_window/reduce_window/change_horizon or pause_ml) instead of repeating the same plan. Summarize the fail streak in prev_attempts_summary.\n"
    "- REQUIRED ECHO FIELDS:\n"
    "  • For any queue_job/backfill/extend_window action, include: {\"end\": <ISO>, \"window_days\": <int>, \"features_table_override\": <string>} when applicable, so downstream can enforce the selection.\n"
    "- IMMUNE & MUTABLE CONTEXT:\n"
    "  • If snapshot.immune_cases contains components with repeated issues, prioritize actions that address them (data backfill, table sync, patch_experiment) before re-running training.\n"
    "  • Use snapshot.mutable_targets health (lag_hours, error_rate, immune_cases_7d) to choose infra_experiments; high lag/error or immune cases → schedule tool/data/module patch experiments ahead of repeated HPO.\n"
    "\n"
    "HISTORY & EXPLANATION REQUIREMENTS:\n"
    "- Compare with previous review when possible: report deltas for n_bars, IC, AUC in 'history_delta'.\n"
    "  • Use INPUT.stage0.recent_actions to identify the last action; if previous metrics are unavailable, set prev=null and describe qualitatively in 'effect_of_last_action'.\n"
    "- In 'effect_of_last_action', state whether the last action (e.g., reduce_window) improved metrics or not (e.g., 'IC unchanged; AUC flat'), and whether to move to the next ladder rung.\n"
    "- Provide 'action_rationale' explaining why the chosen action beats alternatives right now, referencing concrete INPUT numbers (quote them and echo into numbers_used) AND the recent_actions history (including any failure reasons).\n"
    "- When toxicity was present in the prior review but INPUT.stage0.derived_flags.large_window_toxicity==false now, explicitly note 'toxicity removed'.\n"
    "- If INPUT.stage0.derived_flags.feature_loop_blocked==true, you MUST NOT propose 'propose_features' actions in this review; instead, focus on data/backfill/extend_window/HPO or pause_ml as appropriate.\n"
    "\n"
    "TOOL CALLS (OPTIONAL, FOR RESEARCH MODE):\n"
    "- You SHOULD use diagnostic tools before committing new feature expressions or complex schema-dependent actions by populating the top-level field tool_calls:\n"
    "  • tool_calls: [{\"tool\": \"inspect_table_schema|dry_run_feature_expr\", \"args\": <object>}]\n"
    "- Available tools:\n"
    "  • inspect_table_schema: {\"tool\": \"inspect_table_schema\", \"args\": {\"table\": \"trading.features_1m\"}}\n"
    "      - Use this to inspect valid column names/types; never invent columns not present in schemas.\n"
    "  • dry_run_feature_expr: {\"tool\": \"dry_run_feature_expr\", \"args\": {\"expr\": <sql_expr>, \"sources\": [\"m5\"|\"d\"], \"symbol\": \"TSLA\", \"limit\": 10}}\n"
    "      - Use this to test a proposed feature expression against a small sample before committing it via propose_features.\n"
    "- HARD RULE (FOR FEATURES): when introducing any NEW feature expression (DSL-compiled or raw SQL) that has not been previously validated, you MUST first call dry_run_feature_expr on that expression.\n"
    "- If INPUT.tool_results shows dry_run_feature_expr.ok==false for an expression, you MUST NOT include that expression in propose_features until you have fixed the error and a subsequent dry_run passes.\n"
    "- If INPUT.tool_results shows dry_run_feature_expr.ok==true but rows_preview values are all NULL (or INPUT.feature_build_status / logs indicate feature_spec_dry_run_all_null for that spec), you MUST treat this as an upstream data/column issue (e.g., the source column such as atr_5 or spy_ret_5 has no data in the current window), not as a generic syntax error.\n"
    "  • In that case, you MUST avoid repeatedly proposing new features that depend on the same dead/missing column; instead, either (a) switch to existing populated base columns (e.g., atr_14 instead of atr_5), or (b) focus your next action on data/pipeline remedies (backfill/feature_recompute/data_quality_check) if those columns are expected to exist.\n"
    "- If INPUT.stage0.recent_actions includes entries with action='propose_features' and status='failed' and a reason starting with 'unsupported_dsl_ops' or 'no_valid_specs', you MUST adjust your DSL/spec usage (e.g., stop using those ops or fix missing columns) instead of repeating the same failing pattern.\n"
    "- The host will execute these tools and surface results in INPUT.tool_results for the NEXT review_ml call; you must read those results and refine your feature proposals accordingly instead of repeating failing expressions.\n"
    "\n"
    "RECENCY & INFRASTRUCTURE AWARENESS (AVOID STALE ESCALATIONS):\n"
    "- INPUT.feature_build_status always reflects the MOST RECENT feature_build run; older failures are preserved only in logs / previous reviews.\n"
    "- When diagnosing infrastructure or DSL issues (e.g., ClickHouse code 47, UNKNOWN_IDENTIFIER/UNKNOWN_FUNCTION), you MUST:\n"
    "  • First, check the current INPUT.feature_build_status.ok flag and error_code/error_message.\n"
    "  • If INPUT.feature_build_status.ok==true or error_code is absent/empty, you MUST treat earlier feature_build failures as historical context only and MUST NOT escalate or block on them.\n"
    "  • Only when the latest INPUT.feature_build_status.ok==false AND error_code clearly points to a CURRENT ClickHouse/DSL problem may you consider 'escalate' as a next_action.\n"
    "- You MUST NOT repeatedly propose 'escalate' solely based on stale errors mentioned in previous reviews or in long-run counters (e.g., \"892 feature builds failed historically\") when the current feature_build_status shows success.\n"
    "- ALWAYS distinguish between:\n"
    "  • current_state: INPUT.feature_build_status, INPUT.data_quality, INPUT.gateway_stats for the latest jobs;\n"
    "  • history: older failures or escalations described in INPUT.history_delta, prev_attempts_summary, or logs.\n"
    "  In your diagnosis and next_actions, you MUST base hard blocking decisions (pause_ml/escalate) on current_state, using history only as additional color.\n"
    "\n"
    "DIAGNOSTICS (LABELS / FEATURES / CONFIDENCE):\n"
    "- INPUT.alpha_best.diagnostics contains rich statistics you MUST use when available:\n"
    "  • label_stats: {n, mean, std, p01,p05,p25,p50,p75,p95,p99,min,max,pos_frac,neg_frac,zero_frac}\n"
    "  • feature_ic_spearman: {<feature_name>: IC}\n"
    "  • pred_stats: distribution of model predictions\n"
    "  • signal_stats: {pos_frac,neg_frac,zero_frac,avg_abs}\n"
    "  • conf_stats: {mean,p25,p50,p75,frac_ge_0_2,frac_ge_0_5}\n"
    "- Use these diagnostics as follows (examples, not exhaustive):\n"
    "  • If label_stats.n==0 OR std≈0 OR zero_frac>0.95: label generation is broken or degenerate.\n"
    "    → Prefer queue_job(job_type='label_diagnostics', params={symbols:['TSLA']}) to inspect label health, then data_quality_check/backfill/extend_window or fix_config on ml_policy/alpha_config label_profile (NOT lowering thresholds).\n"
    "  • If pos_frac<0.02 OR neg_frac<0.02: labels are extremely imbalanced.\n"
    "    → Consider adjusting label horizon/clip via fix_config on ml_policy or switching to a different intraday_reg_* label_profile.\n"
    "  • If all |IC| in feature_ic_spearman are <0.01: model layer has almost no predictive power.\n"
    "    → Prefer propose_features (structural feature changes) or queue_job(HPO) over gating tweaks.\n"
    "  • If some features have |IC|>=0.03 but OOS Sharpe<=0 or PF<=1.0: model layer is OK but strategy/thresholds may be suboptimal.\n"
    "    → Prefer HPO/alpha_loop or modest label/profile adjustments; DO NOT relax conf_floor.\n"
    "  • If signal_stats.zero_frac>0.98: model rarely produces non-zero signals.\n"
    "    → Prefer HPO on decision/thresholds or label horizon/profile, NOT lowering conf_floor.\n"
    "  • If conf_stats.mean<0.05 and frac_ge_0_2≈0: model is extremely uncertain.\n"
    "    → Treat as weak model quality; prefer feature/HPO/label changes, not gating.\n"
    "\n"
    "HPO / MODEL PARAMETER INSPECTION (XGBOOST DEADLOCK CHECK):\n"
    "- INPUT.metrics.best_params (when job_type=='hpo_cli') and INPUT.hpo_last_best_params (for any job_type) expose the last accepted model hyperparameters.\n"
    "- You MUST inspect these whenever you diagnose long-run 'no edge' or many HPO attempts with AUC≈0.5 and IC≈0, especially for XGBoost models.\n"
    "- For XGBoost-style configs (keys like learning_rate/eta, max_depth, min_child_weight, subsample, colsample_bytree, n_estimators/num_boost_round):\n"
    "  • Treat extremely small learning_rate (e.g., <0.01) combined with very few boosting rounds as a likely cause of underfitting / constant predictions.\n"
    "  • Treat very large min_child_weight (e.g., >10) or very small max_depth (e.g., ≤2) as overly conservative settings that can suppress any edge.\n"
    "  • If best_params show such patterns AND many HPO attempts already exist with no improvement in IC/AUC, you MUST call this out explicitly as a model-parameter deadlock and avoid blindly scheduling more HPO on the same space.\n"
    "- In these regimes, prefer:\n"
    "  • either: running a fresh, targeted hpo_cli over the existing optuna_space.model_type choices (e.g., including lgbm/ensemble) instead of blindly repeating the same XGBoost-only configuration,\n"
    "  • or: escalating to human/operator review when you suspect the allowed model family set itself is too restrictive; you MUST NOT attempt fix_config on optuna_space.* or model_pool.* keys (these are outside your patching scope).\n"
    "- When you attribute lack of edge to model hyperparameters, your diagnosis MUST quote the concrete parameter values from best_params/hpo_last_best_params and explain why they are likely to cause underfitting or degenerate predictions.\n"
    "\n"
    "MODEL FAMILY & MODEL POOL AWARENESS:\n"
    "- INPUT.hpo_config and INPUT.model_space_summary describe the available model families and whether a model_pool/ensemble is configured:\n"
    "  • INPUT.model_space_summary.model_type_choices (when present) lists allowed model_type values in optuna_space (e.g., ['xgb','lgbm','lasso','ridge','ensemble']).\n"
    "  • INPUT.hpo_config.model_pool (when present) describes candidate base models and ensemble templates; INPUT.model_space_summary.has_model_pool==true indicates this is wired into the HPO stack.\n"
    "- You MUST use this information when reasoning about HPO and model quality:\n"
    "  • If long-run no_edge_longrun==true and hpo_last_best_params.model_type appears to be stuck on a single family (e.g., 'xgb') while model_type_choices includes other families (e.g., 'lgbm','ensemble','lasso'), you should question whether additional HPO on the same family is worthwhile.\n"
    "  • In such regimes, prefer queue_job(job_type='hpo_cli') framed as \"explore non-xgb model_type choices within the existing optuna_space\" rather than another generic HPO on the same narrow model.\n"
    "  • If INPUT.hpo_config.model_pool.enabled is true or INPUT.model_space_summary.has_model_pool==true, explicitly acknowledge that a layered ensemble is available and treat it as a first-class option when proposing further HPO/alpha_loop experiments.\n"
    "- HARD BOUNDARY: You MUST NOT propose fix_config actions that attempt to edit model_pool.* or optuna_space.* keys; those configuration surfaces are managed by operators. Your role is to decide WHEN to exploit the existing model_family/model_pool space via queue_job(HPO/alpha_loop), and WHEN to pause or escalate if that space appears exhausted.\n"
    "\n"
    "SIMILAR EPISODES (VECTOR RECALL):\n"
    "- INPUT.similar_episodes (when present) contains a small list of historically similar ML episodes retrieved via vector search over past jobs.\n"
    "- Each entry includes: date, symbol, job_type, horizon_min, window_days, label_bps, auc, ic, coverage, action, notes, similarity.\n"
    "- You MUST interpret these episodes as concrete prior experiments under similar label/horizon/window configurations and use them to guide your next action:\n"
    "  • If most similar_episodes for the current (horizon_min,label_bps,window_days) cluster show AUC≈0.5 and |IC|≈0, you should avoid repeating the same expensive actions (e.g., repeated HPO/backfill/feature proposals) and instead move toward pause_ml or task changes when INPUT.stage0.derived_flags.no_edge_confirmed==true.\n"
    "  • If some similar_episodes show clearly better edge (e.g., AUC>=0.52 and |IC|>=0.02) for nearby label/horizon choices, you may prioritize queue_job(O O F or HPO) for those label_grid points instead of blindly exploring new ones.\n"
    "- In your diagnosis and action_rationale, explicitly mention whether similar_episodes support or contradict your chosen next_action.\n"
    "\n"
    "SCHEMA AWARENESS FOR FEATURES:\n"
    "- INPUT.stage0.schemas may contain table schemas from ClickHouse for:\n"
    "  • trading.features_1m (base intraday features table, alias 'f')\n"
    "  • trading.features_5m (5-minute features table, alias 'm5')\n"
    "- When proposing new features or fixing existing ones:\n"
    "  • ONLY reference columns that appear in these schemas (e.g., f.close, f.atr_14, m5.vwap).\n"
    "  • DO NOT invent new column names or aliases that are not present in the schema.\n"
    "  • TICK-LEVEL DATA IS NOT AVAILABLE: You MUST NOT use tick-level microstructure columns such as tick_imbalance or any column whose name starts with 'tick_'; treat them as unavailable.\n"
    "  • For m5 features, always join on m5.symbol=f.symbol AND m5.ts = toStartOfInterval(f.ts, INTERVAL 5 MINUTE).\n"
    "- If you are unsure whether a column exists, you must assume it does NOT exist and avoid using it.\n"
    "- Prefer simple, ClickHouse-compatible window syntax that avoids unsupported constructs (no standard SQL 'ROWS BETWEEN', no movingAverage()).\n"
    "\n"
    "OOF/HPO/ALPHA CONTEXT (SINGLE JOB REVIEW):\n"
    "- Sometimes INPUT will contain a single job description under INPUT.job. When present, use INPUT.job.job_type and INPUT.job.metrics as the primary ML context.\n"
    "- If INPUT.job.job_type=='oof':\n"
    "  • Treat metrics.ic/metrics.auc/metrics.coverage as pure MODEL-LAYER metrics. Do NOT invent strategy trades or Sharpe from these; they are not full backtest results.\n"
    "  • If trades is null or 0 and only OOF metrics are available, focus your diagnosis on IC/AUC/coverage and error_code (if any), not on conf_floor/gating explanations.\n"
    "  • If metrics.error_code exists (e.g., 'oof_cli_missing_start_end','oof_clickhouse_unknown_table','oof_clickhouse_connection_refused','oof_cli_failed'), treat this as infra/config issue and prefer data_quality_check/backfill or human investigation over repeated HPO/OOF runs.\n"
    "  • NEVER trigger alpha_loop directly from a single bad OOF result; first fix data/labels/features and, if needed, run a targeted hpo_cli job to test improvements.\n"
    "- If INPUT.job.job_type=='hpo_cli':\n"
    "  • Use metrics.best_params and any top-level sharpe/sortino/win_rate/trades/profit_factor/mdd fields (extracted from best_params) as the primary evaluation of the HPO run.\n"
    "  • If trades==0 across folds but IC/AUC are reasonable, consider min_trades constraints or time-window design as possible causes; prefer fix_config on config/hpo_tsla_working.json.objective.min_trades or window adjustments instead of blindly repeating identical HPO jobs.\n"
    "  • DO NOT try to alter min_trades via queue_job params; when you want to change it, use fix_config on config/hpo_tsla_working.json as per the whitelist.\n"
    "  • Only suggest queue_job(job_type='alpha_loop', ...) when BOTH of the following hold:\n"
    "      - model-layer diagnostics (OOF IC/AUC/coverage or recent OOF jobs) indicate a real predictive edge (e.g., |IC|>=0.02 and AUC>=0.52) for at least one key symbol;\n"
    "      - this hpo_cli run shows non-toxic profitability metrics (e.g., sortino>0 OR sharpe>0 AND mdd within acceptable bounds,  trades>=min_trades).\n"
    "    Otherwise, stay on the ladder: prefer further data/label/feature/HPO adjustments instead of escalating to alpha_loop.\n"
    "- If INPUT.job.job_type=='alpha_loop':\n"
    "  • Treat metrics/oos_summary as full STRATEGY-LAYER results (Sharpe, Sortino, max_drawdown, win_rate, trades, profit_factor, expectancy, etc.).\n"
    "  • Apply PROFIT-FIRST gates strictly: do NOT declare success or recommend apply/rollout when Sortino<=0 OR Sharpe<=0 OR profit_factor<=1.0 OR trades too low OR max_drawdown too large.\n"
    "  • If a previous hpo_cli run motivated this alpha_loop job but the alpha_loop metrics are worse than the champion or worse than the hpo_cli expectations, you must treat this as a failed escalation and move the next action back down the ladder (e.g., refine features/labels or adjust HPO config), not schedule another alpha_loop immediately.\n"
    "\n"
    "N_TRIALS GUIDELINES (HPO):\n"
    "- Default n_trials in [40,50] when prior HPO attempts <3 or unknown.\n"
    "- Reduce to ~24 ONLY if: prior HPO history in INPUT.stage0.recent_actions suggests repeatedly low effectiveness, OR time budget constraints are explicit in INPUT; state the reason in 'action_rationale'.\n"
    "- Always include n_trials in numbers_used and justify deviations from default.\n"
    "\n"
    "AUTO-ML & ARCHITECTURE CONTEXT:\n"
    "- The ML stack supports advanced AutoML with stacking and MODEL_POOL-based ensembles driven by walk-forward HPO (wf_hpo.py) and the upgraded HPO CLI (hpo_cli_upgraded.py).\n"
    "- When you schedule HPO via queue_job(job_type='hpo_cli'), you are invoking WalkForwardHPO (runner='wf_hpo:evaluate_trial') over ClickHouse feature tables (e.g., trading.features_1m, features_5s) and label profiles defined in label_profiles.yml.\n"
    "- Use INPUT metrics and artifacts to compare model families and architectures:\n"
    "  • Simple single models (e.g., xgb, lgbm, linear)\n"
    "  • Stacking setups (base models + meta model)\n"
    "  • MODEL_POOL ensembles (use_model_pool=true with model_pool_cfg specifying candidates/templates)\n"
    "- Prefer narrowing search to architectures that consistently deliver better OOS Sharpe/profit_factor/expectancy rather than repeatedly exploring clearly underperforming families.\n"
    "- When multiple models show complementary strengths across regimes (e.g., different volatility regimes or symbols), you should favor actions that keep or strengthen their cooperation (via stacking/model_pool/ensemble policies) instead of dropping them prematurely.\n"
    "\n"
    "ML GATEWAY / EXECUTION-LAYER FILTERS:\n"
    "- INPUT.gateway_stats (when present) summarizes how often the ML Gateway blocked decisions vs allowed them for each symbol on the current date, with block_codes such as 'outlier_drift' or 'regime_filter'.\n"
    "- Use gateway_stats to distinguish between:\n"
    "  • models that truly have no edge (AUC≈0.5/IC≈0 across OOF runs), and\n"
    "  • models whose raw predictions may be fine but whose outputs are frequently blocked by execution-layer filters (e.g., extreme volatility regimes or outlier feature patterns).\n"
    "- When diagnosing zero trades or low trade rates, you MUST consider gateway_stats alongside oof_evaluations and gating_policies:\n"
    "  • If block_ratio is high and block_codes are dominated by regime_filter/outlier_drift, then the root cause of zero trades is Gateway safety, not lack of model signal; further data/label/feature/HPO changes may be less effective than revisiting regime thresholds (via operator-approved config changes) or adjusting the trading task.\n"
    "  • If block_ratio is low but model-layer metrics are near-random, treat it as a pure model/no-edge issue and focus on data/label/feature/model experiments instead of blaming Gateway.\n"
    "\n"
    "MULTI-FREQUENCY & PIPELINE CONTEXT:\n"
    "- Features, labels and losses come from a unified pipeline built on ClickHouse (bars_5s, features_5s, features_1m, etc.) and label_profiles.yml (which defines horizon_bars, clipping, winsorization and objective metric such as sharpe/sortino/calmar).\n"
    "- If INPUT.data_availability indicates short history or limited coverage for key symbols, prioritize data_quality_check/backfill/extend_window before concluding that the model class is hopeless.\n"
    "- Use propose_features for structural feature ideas (e.g., cross-frequency momentum, volatility-adjusted returns, liquidity/microstructure signals) expressed as Feature Catalog specs (config/features_catalog.json via the approved DSL, including optional m5./d. aliases when required_sources is set); keep each proposal small (<=4 specs) and incremental.\n"
    "- Always enforce causality: features and labels must not peek into the future and must behave identically in training, backtest and live trading.\n"
    "\n"
    "TRADING STYLE & LABEL PROFILE ALIGNMENT:\n"
    "- The default research target is intraday, flat-overnight day trading. When INPUT mentions label_profile or objective settings, treat intraday_reg_* profiles as first-class and avoid switching to overnight_* profiles unless explicitly requested.\n"
    "- When models have good metrics but trade too rarely, consider whether the label horizon or decision thresholds are misaligned with intraday trading; however, do NOT directly lower conf_floor via fix_config (profit-first guard). Prefer data/feature/HPO changes that legitimately raise signal quality and mean_conf.\n"
    "- If INPUT.alpha_best.diagnostics.label_stats exists and shows n large but std≈0 and zero_frac>0.9 (labels concentrated near 0) while trades and coverage are extremely low, treat this as a likely horizon mismatch (label too short) and consider switching to a LONGER intraday_reg_* horizon via fix_config on alpha_config_enhanced*.json.label_profile.\n"
    "  • When zero_frac≈1 is induced by excessive label_bps on short horizons, prefer queue_job(job_type='label_recompute', params={symbols:['TSLA'], start:'2025-11-20 09:30:00', end:'2025-11-20 16:00:00', horizon_min:5, label_bps:5}) to regenerate labels via tools/recompute_labels_1m.py before running another ML attempt.\n"
    "- If label_stats.std is extremely large together with very poor Sharpe/profit_factor and deep drawdowns, and trades remain very low, consider switching to a SHORTER intraday_reg_* horizon (e.g., 5m/10m) instead of extending the history window.\n"
    "- If INPUT.label_profiles is present, treat it as the available regression label profile catalogue (name→{task,horizon_bars,objective_metric,min_trades}) and only propose label_profile values taken from this map.\n"
    "- IMPORTANT: INPUT.experiment_policy.label_grid entries (e.g., h10_bps3) are NOT label_profile names. They define (horizon_min,label_bps) combinations for OOF experiments only. You MUST NOT set alpha_config_enhanced*.json.label_profile to any label_grid.name; allowed label_profile values are ONLY those in INPUT.label_profiles.\n"
    "- Label profile switching is a relatively rare action: only propose fix_config on alpha_config_enhanced*.json.label_profile when multiple rounds already show consistently bad trading metrics AND diagnostics clearly point to horizon mismatch rather than data corruption or feature collapse.\n"
    "- Feature family toggling is another lever: when INPUT.stage0.derived_flags.no_edge_longrun==true but label/horizon experiments already cover INPUT.experiment_policy.label_grid, you may try fix_config on config/feature_families.json.families.<name>.enabled to isolate contributions from 'price', 'volume', 'volatility', or 'time' families.\n"
    "- If INPUT.microstructure_health.tick_imbalance_has_signal==false (or INPUT.microstructure_health.symbols[*].nonzero_frac≈0), treat microstructure/tick_imbalance-based features as currently uninformative and avoid spending further propose_features budget on them; instead focus on better-populated sources such as price/volume/ATR/VWAP.\n"
    "\n"
    "HARD RULES (NON-NEGOTIABLE):\n"
    "- If INPUT.stage0.allowed_actions exists, you MUST choose actions ONLY from that list.\n"
    "- If INPUT.stage0.derived_flags.large_window_toxicity==true (e.g., n_bars≥25000 & IC<0.02 or AUC≈0.5): prefer reduce_window (window_days in {30,60}); DO NOT use extend_window.\n"
    "- If INPUT.stage0.derived_flags.data_quality_red_flag==true: prefer data_quality_check/backfill before any window/params changes.\n"
    "- If INPUT.stage0.derived_flags.no_edge_longrun==true (fusion_auc≈0.5 & fusion_ic≈0 over recent window): treat this as 'no edge / research mode'.\n"
    "- In research mode you should PREFER label/feature/HPO experiments (queue_job on OOF/HPO, fix_config on oof_policies/ml_policy, propose_features) over repeated backfill/extend_window tweaks.\n"
    "- When INPUT.stage0.derived_flags.no_edge_longrun==true AND INPUT.experiments_summary.total_experiments is small (e.g., <10), you should actively schedule OOF experiments over the safe grid provided in INPUT.experiment_policy.label_grid.\n"
    "  • For each experiment, use queue_job with job_type='oof' and params including symbols, window_days (from INPUT.experiment_policy.window_days), horizon_min, label_bps drawn from the grid.\n"
    "  • Walk the grid over multiple review cycles rather than trying all combinations at once (platform executes one action per cycle).\n"
    "- If INPUT.experiments_summary.no_edge_confirmed==true (many OOF experiments with no positive AUC/IC across label/horizon choices), you MUST avoid scheduling more expensive HPO/backfill loops and instead either (a) pause_ml, or (b) change the prediction task (label/horizon/frequency) and clearly explain that current configuration has no tradable edge.\n"
    "- Provide prev_attempts_summary briefly to explain why you avoid repeating failed actions.\n"
    "- If INPUT.stage0.allowed_numbers exists and you reference any numeric literals, mirror them in numbers_used (rounded).\n"
    "- ACTION LIMIT: Return at most ONE next_action (the highest-confidence). Platform executes one action per review cycle.\n"
    "\n"
    "**ZERO TRADES DIAGNOSTIC PROTOCOL** (CRITICAL - CHECK FIRST):\n"
    "If INPUT contains backtest_results with trades=0 for ALL modes (xgb_only, lstm_only, fusion):\n"
    "1. FIRST check if oof_evaluations and gating_policies exist in INPUT\n"
    "2. For each symbol in oof_evaluations:\n"
    "   a. Get mean_conf from oof_evaluations[symbol].fusion (or xgb/lstm if fusion missing)\n"
    "   b. Get conf_floor from gating_policies.symbols[symbol].conf_floor (or gating_policies.defaults.conf_floor)\n"
    "   c. Also check strategy_configs[symbol].conf_floor if present (this OVERRIDES gating_policies!)\n"
    "   d. Compare: if mean_conf < conf_floor → this is ROOT CAUSE of 0 trades\n"
    "3. If ROOT CAUSE is config (mean_conf < conf_floor):\n"
    "   - DO NOT lower conf_floor. This is disallowed by profit-first policy.\n"
    "   - Instead, propose data remedies (backfill 1m, extend_window 21/30/45/60) and HPO (limited trials) to improve mean_conf legitimately.\n"
    "   - Only after gates pass on profitability (Sharpe/PF/Expectancy) may you consider small threshold tuning — but do not suggest it in this review.\n"
    "4. Only if NO config issue found, then consider model quality problems\n"
    "\n"
    "Example fix_config for zero trades due to high thresholds:\n"
    "  {\"action\": \"fix_config\", \"params\": {\"path\": \"config/gating_policies.json\", \"key\": \"symbols.AAPL.conf_floor\", \"value\": 0.10}, \"confidence\": 0.95}\n"
    "\n"
    "PHASE 4: DIAGNOSIS PROTOCOL UNDERSTANDING (OOF FAILURE ANALYSIS):\n"
    "- INPUT may contain 'oof_diagnosis' (when OOF training fails) which follows a standardized Diagnosis protocol:\n"
    "  • diagnosis_id: unique identifier for this diagnosis\n"
    "  • timestamp: when diagnosis was created\n"
    "  • target_component: what was diagnosed (e.g., 'training', 'labels', 'features')\n"
    "  • issues: list of Issue objects with {issue_id, component, issue_type, severity, title, description, affected_symbols, metrics}\n"
    "  • root_causes: list of RootCause objects with {type, confidence, evidence, description}\n"
    "  • primary_root_cause: the main RootCause identified (highest priority)\n"
    "  • recommended_actions: list of Action objects with {action_id, action_type, priority, title, description, params, is_safe_auto_execute, requires_approval}\n"
    "  • summary: human-readable diagnosis summary\n"
    "  • confidence: overall diagnosis confidence (0.0-1.0)\n"
    "\n"
    "- INPUT may also contain 'heal_result' (when auto-healing was attempted):\n"
    "  • status: 'fixed' (all actions succeeded), 'partial' (some succeeded), 'failed' (all failed)\n"
    "  • actions: list of ActionResult objects with {action_type, success, duration_sec, rows_affected, error}\n"
    "  • success_count, total_count: success metrics\n"
    "\n"
    "DECISION RULES FOR DIAGNOSIS:\n"
    "1. When INPUT.oof_diagnosis.primary_root_cause.type == 'label_column_missing':\n"
    "   → The config references a label column that doesn't exist in the database\n"
    "   → DO NOT suggest training/HPO until this is fixed\n"
    "   → If heal_result.status == 'fixed', acknowledge the fix and suggest retry OOF\n"
    "   → If heal_result.status == 'failed' or no heal_result, recommend manual label generation or config fix\n"
    "   → Example: queue_job(job_type='oof', params={symbol:'TSLA', horizon_min:120}) after successful label generation\n"
    "\n"
    "2. When INPUT.oof_diagnosis.primary_root_cause.type == 'training_data_insufficient':\n"
    "   → Only a small fraction of available data was used for training (low utilization)\n"
    "   → Check metrics.utilization_rate (e.g., 0.25 = only 25% of bars used)\n"
    "   → Likely causes: excessive filtering, missing features, or NULL-heavy columns\n"
    "   → Recommend: data_quality_check, backfill, or extend_window\n"
    "   → DO NOT suggest HPO/feature changes until data utilization improves\n"
    "\n"
    "3. When INPUT.oof_diagnosis.primary_root_cause.type == 'label_quality_poor':\n"
    "   → Labels exist but have quality issues (high NULL rate, low std, imbalanced)\n"
    "   → Check oof_diagnosis.issues for specific problems (e.g., 'zero_rate_too_high', 'std_too_low')\n"
    "   → If heal_result shows successful label regeneration, suggest retry OOF\n"
    "   → Otherwise, recommend testing longer horizons: queue_job(job_type='label_diagnostics', params={symbols:['TSLA'], horizons:[30,60,120]})\n"
    "   → Consider fix_config to switch label horizon if diagnosis suggests it\n"
    "\n"
    "4. When INPUT.oof_diagnosis.primary_root_cause.type in ['data_pipeline_failed', 'feature_stale']:\n"
    "   → Upstream data pipeline has not updated (bars or features are stale)\n"
    "   → Check age_hours in diagnosis.issues[].metrics\n"
    "   → If heal_result.status == 'fixed' (backfill/rebuild completed), acknowledge and suggest retry\n"
    "   → If heal_result.status == 'failed', escalate to human (data pipeline issue)\n"
    "\n"
    "5. When INPUT.heal_result.status == 'fixed':\n"
    "   → Auto-healing succeeded, underlying issues are resolved\n"
    "   → Acknowledge the successful fix in your diagnosis\n"
    "   → Suggest retry of the failed job (e.g., queue_job with same params)\n"
    "   → Example: 'Auto-healing successfully generated missing label column. Retry OOF training.'\n"
    "\n"
    "6. When INPUT.heal_result.status == 'partial':\n"
    "   → Some auto-healing actions succeeded, but not all\n"
    "   → Check heal_result.actions to see which succeeded/failed\n"
    "   → Acknowledge partial success and recommend manual intervention for failed actions\n"
    "   → May still suggest retry if critical actions (e.g., label generation) succeeded\n"
    "\n"
    "7. When INPUT.heal_result.status == 'failed':\n"
    "   → All auto-healing attempts failed\n"
    "   → Check heal_result.actions[].error for failure reasons\n"
    "   → DO NOT suggest retry without manual intervention\n"
    "   → Recommend escalate or specific manual fixes based on error messages\n"
    "\n"
    "8. When NO oof_diagnosis present:\n"
    "   → Normal OOF completion (no diagnostic analysis ran)\n"
    "   → Follow standard review logic based on metrics (AUC, IC, Sharpe, etc.)\n"
    "\n"
    "IMPORTANT DIAGNOSIS INTERPRETATION:\n"
    "- oof_diagnosis.confidence indicates how certain the diagnosis is (0.0-1.0)\n"
    "  • confidence >= 0.9: Very certain, follow recommended_actions closely\n"
    "  • confidence < 0.5: Uncertain, consider multiple hypotheses\n"
    "\n"
    "- recommended_actions are ordered by priority (priority=1 is highest)\n"
    "  • is_safe_auto_execute=true means DataHealer can run it automatically\n"
    "  • requires_approval=true means human must approve (not auto-executed)\n"
    "\n"
    "- When multiple root_causes exist, primary_root_cause is the most critical\n"
    "  • Focus on fixing primary_root_cause first before addressing secondary issues\n"
    "\n"
    "- Evidence in root_causes.evidence contains diagnostic metrics and queries\n"
    "  • Use this to understand WHY the diagnosis was made\n"
    "  • Quote specific metrics in your action_rationale\n"
    "\n"
    "EXAMPLE DIAGNOSIS-BASED DECISION:\n"
    "INPUT.oof_diagnosis = {\n"
    "  \"primary_root_cause\": {\"type\": \"label_column_missing\", \"confidence\": 1.0},\n"
    "  \"summary\": \"Label column label_h5 missing for TSLA.\",\n"
    "  \"heal_result\": {\"status\": \"fixed\", \"success_count\": 1}\n"
    "}\n"
    "→ Your response:\n"
    "  diagnosis: [\"Label column label_h5 was missing but auto-healing successfully generated it\"]\n"
    "  next_actions: [{\"action\": \"queue_job\", \"params\": {\"job_type\": \"oof\", \"symbols\": [\"TSLA\"], \"horizon_min\": 5}, \"confidence\": 0.95}]\n"
    "  action_rationale: \"Auto-healing fixed label_column_missing issue (confidence=1.0). Retry OOF training with newly generated labels.\"\n"

)

# Feature Engineering Agent Prompt
# 这个prompt让AI Brain能够智能地修改特征工程配置,而不是直接写Python代码
FEATURE_ENGINEERING_PROMPT = (
    "You are a professional Feature Engineering Agent for multi-asset high-frequency/intraday trading strategies.\n"
    "\n"
    "==== YOUR ROLE ====\n"
    "You control feature engineering through a Feature Program DSL (Domain-Specific Language), not by writing Python code.\n"
    "- All feature programs are defined in feature_programs.json\n"
    "- Each program is a sequence of 'steps' using predefined operators\n"
    "- Your changes affect training (HPO), backtesting, AND live trading simultaneously\n"
    "- Therefore, features MUST be stable, causal (no future leakage), and generalizable\n"
    "\n"
    "==== AVAILABLE OPERATORS (op) ====\n"
    "You can ONLY use these operators. Do NOT invent new ones.\n"
    "\n"
    "Basic Ratios:\n"
    "  - ratio: (num / den)\n"
    "  - ratio_minus_1: (num / den - 1)\n"
    "\n"
    "Standard Transforms:\n"
    "  - log1p: log(1 + x)\n"
    "  - abs: |x|\n"
    "  - clip: clip to [min, max]\n"
    "  - power: x^p\n"
    "  - sign: sign(x)\n"
    "\n"
    "Time Series Rolling (look-back ONLY, no future peeking):\n"
    "  - rolling_sum: rolling sum over window_bars\n"
    "  - rolling_mean: rolling average\n"
    "  - rolling_std: rolling standard deviation\n"
    "  - rolling_max: rolling maximum\n"
    "  - rolling_min: rolling minimum\n"
    "  - rolling_zscore: (x - rolling_mean) / rolling_std\n"
    "\n"
    "Lag/Diff:\n"
    "  - lag: shift backward n bars\n"
    "  - diff: n-order difference\n"
    "\n"
    "Smoothing:\n"
    "  - ema: exponential moving average (supports per_symbol)\n"
    "\n"
    "Interaction Features:\n"
    "  - product: col1 * col2\n"
    "  - sum: col1 + col2\n"
    "\n"
    "Microstructure/Liquidity:\n"
    "  - price_impact_hl: price impact using high/low vs previous close\n"
    "  - effective_spread: (high - low) / mid\n"
    "  - amihud_illiquidity: |ret| / (volume * close)\n"
    "\n"
    "==== MULTI-FREQUENCY CONTEXT ====\n"
    "Feature catalogs can now mix multiple resampled sources as long as you stay causal:\n"
    "  - f.<col>: 1m baseline from trading.features_1m (default).\n"
    "  - m5.<col>: 5-minute buckets from trading.features_5m (ts = toStartOfInterval(f.ts, 5m)).\n"
    "  - d.<col>: previous-day aggregates from trading.features_1d_prev (ts = toStartOfDay(f.ts), values computed via tools/feature_resample.py).\n"
    "Whenever a spec references m5./d. aliases, include \"required_sources\": [\"m5\", \"d\"] subset so the platform joins those tables automatically.\n"
    "Never reference other tables or future timestamps; daily aggregates are already shifted to be valid for the next session.\n"
    "\n"
    "==== STEP STRUCTURE ====\n"
    "Each step in a program has these fields (do NOT invent new keys):\n"
    "\n"
    "Common fields:\n"
    "  - name: new feature column name (string, must be unique and valid)\n"
    "  - op: operator name from the list above\n"
    "  - per_symbol: optional bool (default true), whether to group by symbol for rolling/lag ops\n"
    "\n"
    "Operator-specific fields:\n"
    "  ratio/ratio_minus_1:\n"
    "    - num: numerator column\n"
    "    - den: denominator column\n"
    "    - eps: optional, prevent division by zero (default 1e-6)\n"
    "\n"
    "  log1p/abs/clip/power/sign:\n"
    "    - col: column name\n"
    "    - min/max/p: depending on operator\n"
    "\n"
    "  rolling_*:\n"
    "    - col: column name\n"
    "    - window_bars: window length (int)\n"
    "    - min_periods: optional, minimum valid samples\n"
    "\n"
    "  lag/diff:\n"
    "    - col: column name\n"
    "    - lags: lag/diff steps (int)\n"
    "\n"
    "  ema:\n"
    "    - col: column name\n"
    "    - span: EMA span (int)\n"
    "\n"
    "  product/sum:\n"
    "    - col1: first column\n"
    "    - col2: second column\n"
    "\n"
    "  price_impact_hl/effective_spread/amihud_illiquidity:\n"
    "    - Use existing columns (high/low/close/ret_1/volume), minimal extra params needed\n"
    "\n"
    "==== CAUSALITY & NO FUTURE LEAKAGE ====\n"
    "CRITICAL RULES:\n"
    "1. You can ONLY use rolling/lag/diff operators that \"look backward\"\n"
    "2. NEVER use future y/target or future returns\n"
    "3. NO \"peek into future\" features allowed\n"
    "4. Features must work identically in training, backtest, AND live trading\n"
    "\n"
    "==== HPO CONFIGURATION ====\n"
    "The training system's HPO space is defined in alpha_config_enhanced.json:\n"
    "  - feature_program_id: which program to use\n"
    "  - feature_subset: select a subset of generated features\n"
    "  - model_scale_mode: ['none', 'zscore', 'robust']\n"
    "\n"
    "==== YOUR TASKS ====\n"
    "Given recent HPO/backtest results, you should:\n"
    "\n"
    "1. ANALYZE current feature programs' performance\n"
    "   - Which feature_program_id / feature_subset / model_scale_mode combos perform best on OOS metrics?\n"
    "   - Which show overfitting or poor generalization?\n"
    "\n"
    "2. INCREMENTALLY modify feature_programs.json:\n"
    "   - Add or update a few programs (not wholesale replacement)\n"
    "   - Each program should have 6-10 steps max (avoid over-complexity)\n"
    "   - Each step:\n"
    "     * Use ONLY columns from available_features list provided\n"
    "     * Use ONLY operators listed above\n"
    "     * Maintain causality (no future peeking)\n"
    "\n"
    "3. UPDATE HPO space in alpha_config_enhanced.json:\n"
    "   - Recommend new feature_program_id candidates\n"
    "   - Recommend feature_subset combinations (arrays of column names)\n"
    "     * Subsets must be from: available_features + your newly created feature names\n"
    "   - Recommend model_scale_mode candidates (typically 'zscore' or 'robust')\n"
    "\n"
    "4. AVOID:\n"
    "   - Inventing non-existent column names\n"
    "   - Inventing non-existent operators\n"
    "   - Creating 20+ step mega-programs\n"
    "   - Including target/y columns in feature_subset\n"
    "\n"
    "==== OUTPUT FORMAT (STRICT JSON) ====\n"
    "Return ONLY a JSON object (no markdown, no extra text):\n"
    "\n"
    "{\n"
    "  \"analysis\": \"Brief explanation of your feature engineering changes and rationale (can be in Chinese)\",\n"
    "  \"feature_programs_patch\": {\n"
    "    \"add_or_update\": {\n"
    "      \"<program_id_1>\": {\n"
    "        \"description\": \"...\",\n"
    "        \"steps\": [\n"
    "          { \"name\": \"...\", \"op\": \"...\", ... },\n"
    "          ...\n"
    "        ]\n"
    "      },\n"
    "      \"<program_id_2>\": { ... }\n"
    "    },\n"
    "    \"delete\": [\"<program_id_to_delete_1>\", \"<program_id_to_delete_2>\"]\n"
    "  },\n"
    "  \"alpha_hpo_patch\": {\n"
    "    \"feature_program_id_choices\": [\"none\", \"baseline_v1\", \"momentum_v2\", ...],\n"
    "    \"feature_subset_choices\": [\n"
    "      [\"ret_1\", \"atr_14\", \"volume\"],\n"
    "      [\"ret_1\", \"vwap\", \"atr_14\", \"volume\"],\n"
    "      [\"feat_vwap_dev\", \"feat_ret1m_over_atr\", \"feat_log_volume\"],\n"
    "      ...\n"
    "    ],\n"
    "    \"model_scale_mode_choices\": [\"zscore\", \"robust\"]\n"
    "  }\n"
    "}\n"
    "\n"
    "Requirements:\n"
    "- 'analysis': string explaining your thinking\n"
    "- 'feature_programs_patch.add_or_update': only programs you want to add/modify\n"
    "- 'feature_programs_patch.delete': array of program_ids to remove (empty array [] if none)\n"
    "- 'alpha_hpo_patch': must have all three fields\n"
    "  * feature_program_id_choices: array of program_ids\n"
    "  * feature_subset_choices: array of arrays (each inner array is a feature subset)\n"
    "  * model_scale_mode_choices: array of scaling modes\n"
    "- Must be valid JSON (no comments, no trailing commas)\n"
    "CRITICAL DSL SYNTAX: Do NOT use null/None for any operator arguments (num, den, col, col1, col2, etc.); every operand must be a valid column name present in available_features.\n"
)


FEATURE_CATALOG_PROMPT = (
    "You are a feature catalog designer for an intraday ML trading system. Return ONLY strict JSON.\n"
    "Your task is to propose a SMALL set of new feature specs for the ClickHouse feature catalog (config/features_catalog.json).\n"
    "You MUST obey the allowed DSL ops and schemas provided in INPUT.\n"
    "\n"
    "==== OUTPUT SCHEMA ====\n"
    "Return a JSON object of the form:\n"
    "{\\\"version\\\": <string>, \\\"specs\\\": [{\\\"name\\\": <string>, \\\"dsl\\\": <object>, "
    "\\\"dtype\\\": \\\"Float32\\\"|\\\"Float64\\\", \\\"notes\\\": <string>, \\\"required_sources\\\"?: [\\\"m5\\\"|\\\"d\\\"]}]}\n"
    "\n"
    "==== DSL RULES (CRITICAL) ====\n"
    "- The SQL-side DSL is intentionally minimal: only the following ops are allowed today: ratio, ratio_minus_1, log1p, product, sum, abs.\n"
    "- NEVER use null or None for any operand (num, den, col, col1, col2, etc.). Every operand must be a valid column string or a nested DSL object.\n"
    "- If a denominator is missing, choose a safe fallback column (e.g., f.atr_14) or a constant (e.g., 1.0); do NOT set it to null.\n"
    "- Example BAD: {\"op\": \"ratio\", \"num\": \"f.ret_5\", \"den\": null}.\n"
    "- Example GOOD: {\"op\": \"ratio\", \"num\": \"f.ret_5\", \"den\": \"f.atr_14\", \"eps\": 1e-6}.\n"
    "- Do NOT invent new ops or direct SQL functions in DSL; if you need a more complex pattern, fall back to a raw expr using only f./m5./d. columns and ClickHouse-safe functions.\n"
    "\n"
    "==== CONTEXT ====\n"
    "- INPUT.schemas: may contain ClickHouse table schemas for trading.features_1m (alias f), "
    "trading.features_5m (alias m5), trading.features_1d_prev (alias d). Treat the column names from these schemas as your AVAILABLE_COLUMNS; you MUST NOT reference any f./m5./d. columns outside of these lists.\n"
    "- INPUT.data_gaps / INPUT.data_quality_snapshot / INPUT.ml_health_recent: summarise data coverage and model health.\n"
    "- INPUT.stage0.recent_actions: includes recent propose_features attempts and failure reasons "
    "(e.g. unsupported_dsl_ops, no_valid_specs).\n"
    "\n"
    "==== ALLOWED DSL OPS (for dsl specs) ====\n"
    "You may use ONLY this minimal DSL when you choose to emit a dsl object instead of raw SQL:\n"
    "- ratio: {op: 'ratio', num: 'f.ret_5', den: 'f.atr_14', eps: 1e-6} -> (num) / nullIf(den, eps)\n"
    "- ratio_minus_1: {op: 'ratio_minus_1', num: 'f.ret_5', den: 'f.atr_14', eps: 1e-6} -> (num / nullIf(den, eps)) - 1\n"
    "- log1p: {op: 'log1p', col: 'f.volume'} -> log1p(col)\n"
    "- product: {op: 'product', col1: 'f.z_vol_20', col2: 'm5.tick_imbalance'} -> col1 * col2\n"
    "- sum: {op: 'sum', col1: 'f.ret_1', col2: 'd.vwap_day'} -> col1 + col2\n"
    "- abs: {op: 'abs', col: 'm5.tick_imbalance'} -> abs(col)\n"
    "Do NOT use other DSL ops such as zscore, rolling, ema, delta, movingAverage; the host will reject them as unsupported_dsl_ops.\n"
    "CRITICAL DSL SYNTAX: Do NOT set num/den/col/col1/col2 to null or None; every operand must be a valid column string or a nested DSL object.\n"
    "\n"
    "==== SQL EXPRESSIONS (expr) ====\n"
    "If you prefer, you MAY provide raw ClickHouse SQL expressions in 'expr' instead of DSL, but you MUST obey these rules:\n"
    "- Use only aliases f., m5., d. and only columns that exist in the corresponding INPUT.schemas tables.\n"
    "- Do NOT use deprecated/unsafe functions like runningDifference, neighbor, movingAverage, linearRegressionSlope.\n"
    "- Avoid generic 'ROWS BETWEEN' window syntax; prefer simple expressions built from existing precomputed stats "
    "(e.g. f.zscore_close_20, f.zscore_volume_20, m5.tick_imbalance, f.avg_trade_size).\n"
    "\n"
    "==== DESIGN PRINCIPLES ====\n"
    "- Propose at most 3-4 new specs per call; keep them simple and interpretable.\n"
    "- Leverage existing columns that already encode volatility/liquidity/microstructure instead of recomputing complex windows.\n"
    "- Prefer features that target known weaknesses in INPUT.ml_health_recent (e.g. low IC, AUC≈0.5, low coverage).\n"
    "- Read INPUT.stage0.recent_actions: if earlier propose_features attempts failed with unsupported_dsl_ops or no_valid_specs, "
    "avoid repeating the same ops or invalid column references.\n"
)


class ControlBrain:
    def __init__(self, runs_dir: str, provider: str = "deepseek_nvidia", model: str = "deepseek-ai/deepseek-r1"):
        self.runs_dir = Path(runs_dir)
        self.provider = provider  # Keep for backward compatibility
        self.model = model
        self._prov = None  # Keep for backward compatibility
        self.log = get_logger("ai_trader.brain")
        # tracing: write every call I/O to brain_calls.jsonl (best-effort)
        self.trace_enabled: bool = True
        # Load trading universe from root config.yml
        self.trading_universe = self._load_trading_universe()

        # Initialize Provider Pool with fallback support (configurable via env)
        self.pool = ProviderPool(
            configs=self._build_provider_configs(),
            runs_dir=str(self.runs_dir),
            log_failures=True
        )
        # Optional data readiness checker (best-effort)
        try:
            from libs.ml.data_readiness_checker import DataReadinessChecker  # type: ignore
            self.data_readiness_checker = DataReadinessChecker()
        except Exception:
            self.data_readiness_checker = None
        # Lazy window manager for shrink decisions
        try:
            from scripts.ai_trader.research.window_manager import AdaptiveWindowManager  # type: ignore
            self.window_manager = AdaptiveWindowManager()
        except Exception:
            self.window_manager = None

    def _load_prompt_file(self, name: str, default: str) -> str:
        """Load a prompt template from prompts/ with a safe fallback."""
        try:
            p = Path("prompts") / name
            if p.exists():
                return p.read_text(encoding="utf-8")
        except Exception:
            pass
        return default

    def _load_trading_universe(self) -> list:
        """Load trading universe from root config.yml"""
        try:
            config_path = Path("config.yml")
            if not config_path.exists():
                self.log.warning("config.yml not found, using default universe")
                return ["TSLA", "AAPL", "NVDA", "MSFT"]

            with config_path.open("r", encoding="utf-8") as f:
                config = yaml.safe_load(f)

            universe = config.get("universe", [])
            if not universe:
                self.log.warning("universe empty in config.yml, using default")
                return ["TSLA", "AAPL", "NVDA", "MSFT"]

            self.log.info("trading_universe_loaded", symbols=universe)
            return universe
        except Exception as e:
            self.log.error("failed_to_load_universe", error=str(e))
            return ["TSLA", "AAPL", "NVDA", "MSFT"]

    def _build_provider_configs(self) -> list:
        """
        Build ProviderConfig list from env, with sensible defaults.
        Env format (comma-separated entries):
          LLM_PROVIDERS="ollama:qwen2.5vl:latest@120,ollama:my-deepseek-uncensored:latest@300,deepseek_nvidia@300,gemini@30,openai@45"
        Entry syntax: provider[:model][@timeout_seconds]
        You can override per operation by setting:
          LLM_PROVIDERS_REVIEW, LLM_PROVIDERS_DECISION, LLM_PROVIDERS_HPO, etc.
        """
        import os
        default_ollama_timeout = int(os.getenv("OLLAMA_DEFAULT_TIMEOUT", 300))
        default_chain = [
            ("ollama", "qwen2.5vl:latest", 300),
            ("ollama", "my-deepseek-uncensored:latest", 300),
            ("ollama", "qwen2.5:7b-instruct", 300),
            ("ollama", "deepseek-r1:1.5b", 300),
            ("deepseek_nvidia", None, 300),
            ("gemini", None, 60),
            ("openai", None, 60),
        ]

        # Operation-specific chain if present
        op_key = os.getenv("LLM_PROVIDERS_REVIEW") if getattr(self, "current_operation", None) == "review_ml" else None
        raw = op_key or os.getenv("LLM_PROVIDERS")
        entries = []
        if raw:
            for part in raw.split(","):
                part = part.strip()
                if not part:
                    continue
                # provider[:model][@timeout]
                timeout = None
                model = None
                if "@" in part:
                    part, t = part.split("@", 1)
                    try:
                        timeout = int(t)
                    except Exception:
                        timeout = None
                if ":" in part:
                    provider, model = part.split(":", 1)
                else:
                    provider = part
                entries.append((provider, model, timeout))
        else:
            entries = default_chain
            # Inject configured primary model at the top
            if self.provider and self.model:
                entries.insert(0, (self.provider, self.model, 300))

        configs = []
        for idx, (provider, model, timeout) in enumerate(entries, start=1):
            # fallback defaults if not provided
            if model is None and provider == "ollama":
                model = os.getenv("OLLAMA_DEFAULT_MODEL", "qwen2.5vl:latest")
            if timeout is None:
                # default timeouts similar to previous tuning
                if provider == "ollama":
                    timeout = default_ollama_timeout
                elif provider == "deepseek_nvidia":
                    timeout = 300
                elif provider == "openai":
                    timeout = 45
                else:
                    timeout = 30
            configs.append(
                ProviderConfig(
                    name=provider,
                    model=model,
                    priority=idx,
                    timeout=timeout,
                    max_retries=1
                )
            )
        return configs

    # Best-effort readiness hook (used by callers before heavy jobs)
    def check_data_readiness(self, symbols: list, start: str, end: str) -> dict | None:
        if not getattr(self, "data_readiness_checker", None):
            return None
        try:
            rec = self.data_readiness_checker.check(
                symbols=symbols,
                start=start,
                end=end,
                features_table="trading.features_1m_active",
                labels_table="trading.features_1m_with_labels",
            )
            return rec.to_dict()
        except Exception:
            return None

    def adjust_window(self, start: str, end: str, coverage: dict | None) -> tuple[str, str, str]:
        """
        Use adaptive window manager to shrink window when utilization is low.
        """
        if not getattr(self, "window_manager", None):
            return start, end, "window_ok"
        try:
            return self.window_manager.adjust(start, end, coverage)
        except Exception:
            return start, end, "window_ok"

    def _ensure_provider(self):
        if self._prov is not None:
            return self._prov
        mod_name, cls_name, envk = PROV[self.provider]
        api_key = os.getenv(envk)
        if not api_key:
            raise RuntimeError(f"Missing API key for {self.provider}: env {envk}")
        mod = __import__(mod_name, fromlist=[cls_name])
        cls = getattr(mod, cls_name)
        self._prov = cls(api_key, self.model)
        return self._prov

    async def _ask(self, prompt: str, timeout: float = 300.0, operation: str = "unknown") -> LLMCallResult:
        """
        Call LLM with automatic provider fallback and return LLMCallResult with error tracking
        """
        try:
            # Use ProviderPool with automatic fallback
            result = await self.pool.call_with_fallback(prompt, operation)

            if result.success:
                self.log.info(
                    "llm_call_success",
                    operation=operation,
                    provider=result.provider_used,
                    response_len=len(result.raw),
                    fallback_used=result.fallback_used
                )
            else:
                self.log.error(
                    "llm_call_failed",
                    operation=operation,
                    error_type=result.error_type,
                    error_message=result.error_message,
                    providers_attempted=result.providers_attempted
                )

            return result

        except Exception as e:
            # Unexpected error (should not happen with new LLMCallResult)
            self.log.error("llm_call_unexpected_error", operation=operation, error=str(e), exc_info=True)
            return LLMCallResult(
                data={"error": str(e)},
                raw="",
                provider_used="none",
                success=False,
                error_type="unexpected_error",
                error_message=str(e),
                fallback_used=False,
                providers_attempted=[]
            )

    def _trace_write(self, record: Dict[str, Any]) -> None:
        if not self.trace_enabled:
            return
        try:
            d = self.runs_dir / datetime.now().date().isoformat()
            d.mkdir(parents=True, exist_ok=True)
            fp = d / "brain_calls.jsonl"
            with fp.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _recent_memory_block(self, days: int = 7, limit: int = 3) -> str:
        """
        Force-read recent daily summaries and plan_vs_outcome memories for prompt injection.
        Skips malformed entries to avoid polluting prompts.
        """
        try:
            from scripts.ai_trader.memory.memory_fabric import load_recent_memories  # type: ignore
        except Exception:
            return ""

        import json as _json

        try:
            daily = load_recent_memories(
                "daily_summary",
                limit=limit,
                days=days,
                required_keys=("summary", "lessons_learned"),
                mark=True,
            )
        except Exception as exc:
            self.log.warning("memory_daily_summary_fetch_failed", error=str(exc))
            daily = []

        outcomes: list[dict] = []
        try:
            outcomes = load_recent_memories(
                "plan_vs_outcome",
                limit=limit,
                days=days,
                required_keys=("plan_id", "ev_plan", "ev_realized", "mdd", "timestamp"),
                mark=True,
            )
        except Exception:
            try:
                from ai.plan_outcome_logger import load_recent_plan_outcomes  # type: ignore

                outcomes = load_recent_plan_outcomes(limit=limit, days=days, mark_accessed=True)
            except Exception as exc:
                self.log.warning("plan_vs_outcome_fetch_failed", error=str(exc))
                outcomes = []

        if not daily and not outcomes:
            return ""

        block_parts: list[str] = []
        if daily:
            compact = [
                {
                    "date": d.get("date"),
                    "summary": d.get("summary"),
                    "lessons": d.get("lessons_learned") or d.get("lessons") or [],
                }
                for d in daily
            ]
            block_parts.append("RECENT DAILY SUMMARIES (last {}d):\n{}".format(days, _json.dumps(compact, indent=2)))

        if outcomes:
            compact_outcomes = [
                {
                    "plan_id": o.get("plan_id"),
                    "ev_plan": o.get("ev_plan"),
                    "ev_realized": o.get("ev_realized"),
                    "mdd": o.get("mdd"),
                    "hedge_effect": o.get("hedge_effect"),
                    "issues": o.get("issues"),
                    "lessons": o.get("lessons"),
                    "ts": o.get("timestamp"),
                }
                for o in outcomes
            ]
            block_parts.append("RECENT PLAN_VS_OUTCOME (last {}d):\n{}".format(days, _json.dumps(compact_outcomes, indent=2)))

        block_parts.append("INSTRUCTION: apply lessons above and avoid repeating prior issues before proposing plan.")
        return "\n\n".join(block_parts) + "\n"

    def _profit_risk_goals(self, context: Dict[str, Any]) -> Dict[str, float]:
        """
        Derive profit/risk goals for prompt injection with safe defaults.
        """
        goals = (context or {}).get("profit_risk_goals") or {}
        # Optional: load live goals from runs/ai_trader/profit_risk_goals.json if present
        try:
            live_fp = Path("runs/ai_trader/profit_risk_goals.json")
            if live_fp.exists():
                live = json.loads(live_fp.read_text())
                if isinstance(live, dict):
                    goals = {**goals, **live}
        except Exception:
            pass
        def _num(val: Any, default: float) -> float:
            try:
                return float(val)
            except Exception:
                return default

        return {
            "target_daily_pnl_pct": _num(goals.get("target_daily_pnl_pct") or (context or {}).get("target_daily_pnl_pct"), 0.6),
            "target_weekly_pnl_pct": _num(goals.get("target_weekly_pnl_pct") or (context or {}).get("target_weekly_pnl_pct"), 3.0),
            "max_drawdown_pct": _num(goals.get("max_drawdown_pct") or (context or {}).get("max_drawdown_pct"), 3.0),
            "min_confidence": _num(goals.get("min_confidence") or (context or {}).get("min_confidence"), 0.6),
            "min_expected_ev": _num(goals.get("min_expected_ev") or (context or {}).get("min_expected_ev"), 0.0),
        }

    def plan_model_space(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Model & space expert: propose HPO/model_space adjustments.

        Context is expected to include:
          - current_space: simplified view of current HPO/model space
          - hpo_report: summary of recent HPO/OOS results
          - constraints: same as plan_research
        """
        import json as _J
        payload = context or {}
        default_intro = (
            "You are the Model & Space Expert for an intraday trading ML system.\n"
            "Given INPUT, propose adjustments to the HPO/model search space "
            "for the next research cycle. You MUST return ONLY strict JSON of the form "
            "{\"version\":\"v1\",\"actor\":\"model\",\"branch_id\":...,\"mode\":...,"
            "\"reasoning_summary\":...,\"changes\":{...}}.\n"
        )
        system_intro = self._load_prompt_file("model_expert.md", default_intro)
        prompt = system_intro + "\n\nINPUT:\n" + _J.dumps(payload)
        t0 = datetime.now()
        result = asyncio.run(self._ask(prompt, operation="plan_model_space", timeout=180.0))
        dur_ms = int((datetime.now() - t0).total_seconds() * 1000)
        data = result.data if isinstance(result.data, dict) else {}
        self._trace_write(
            {
                "ts": datetime.now().isoformat(),
                "kind": "plan_model_space",
                "provider": result.provider_used,
                "model": self.model,
                "input_keys": list(payload.keys()),
                "output": data,
                "duration_ms": dur_ms,
                "success": 1 if result.success else 0,
                "error_type": result.error_type,
                "error_message": result.error_message,
                "fallback_used": 1 if result.fallback_used else 0,
                "providers_attempted": result.providers_attempted,
            }
        )
        return data if isinstance(data, dict) else {}

    def plan_alpha(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Alpha expert: propose label_profile and high-level feature tag adjustments.

        Context is expected to include:
          - recent_intraday_metrics
          - current_label_profile: {name, config}
          - label_candidates: optional list of alternative profiles
          - label_history / feature summaries (optional)
        """
        import json as _J
        payload = context or {}
        default_intro = (
            "You are the Alpha Expert for an intraday trading ML system.\n"
            "Given INPUT, propose safe adjustments to label_profile and high-level feature tags "
            "to improve intraday profitability, without touching execution or risk. "
            "You MUST return ONLY strict JSON of the form "
            "{\"version\":\"v1\",\"actor\":\"alpha\",\"branch_id\":...,\"mode\":...,"
            "\"reasoning_summary\":...,\"changes\":{...}}.\n"
        )
        system_intro = self._load_prompt_file("alpha_expert.md", default_intro)
        prompt = system_intro + "\n\nINPUT:\n" + _J.dumps(payload)
        t0 = datetime.now()
        result = asyncio.run(self._ask(prompt, operation="plan_alpha", timeout=120.0))
        dur_ms = int((datetime.now() - t0).total_seconds() * 1000)
        data = result.data if isinstance(result.data, dict) else {}
        self._trace_write(
            {
                "ts": datetime.now().isoformat(),
                "kind": "plan_alpha",
                "provider": result.provider_used,
                "model": self.model,
                "input_keys": list(payload.keys()),
                "output": data,
                "duration_ms": dur_ms,
                "success": 1 if result.success else 0,
                "error_type": result.error_type,
                "error_message": result.error_message,
                "fallback_used": 1 if result.fallback_used else 0,
                "providers_attempted": result.providers_attempted,
            }
        )
        return data if isinstance(data, dict) else {}

    def plan_execution(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Execution & gating expert: propose execution/risk threshold adjustments.

        Context is expected to include:
          - recent_intraday_metrics
          - current_gating: current execution/gating parameters
          - risk_limits: e.g. max_intraday_drawdown_pct
          - time_distribution: optional intraday trades/PnL by bucket
        """
        import json as _J
        payload = context or {}
        default_intro = (
            "You are the Execution & Gating Expert for an intraday ML trading system.\n"
            "Given INPUT, propose small, risk-respecting adjustments to execution and gating "
            "parameters. You MUST return ONLY strict JSON of the form "
            "{\"version\":\"v1\",\"actor\":\"execution\",\"branch_id\":...,\"mode\":...,"
            "\"reasoning_summary\":...,\"changes\":{...}}.\n"
        )
        system_intro = self._load_prompt_file("execution_expert.md", default_intro)
        prompt = system_intro + "\n\nINPUT:\n" + _J.dumps(payload)
        t0 = datetime.now()
        result = asyncio.run(self._ask(prompt, operation="plan_execution", timeout=120.0))
        dur_ms = int((datetime.now() - t0).total_seconds() * 1000)
        data = result.data if isinstance(result.data, dict) else {}
        self._trace_write(
            {
                "ts": datetime.now().isoformat(),
                "kind": "plan_execution",
                "provider": result.provider_used,
                "model": self.model,
                "input_keys": list(payload.keys()),
                "output": data,
                "duration_ms": dur_ms,
                "success": 1 if result.success else 0,
                "error_type": result.error_type,
                "error_message": result.error_message,
                "fallback_used": 1 if result.fallback_used else 0,
                "providers_attempted": result.providers_attempted,
            }
        )
        return data if isinstance(data, dict) else {}

    def plan_research(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """High-level research planner using the Research Director prompt.

        Context is expected to include:
          - recent_intraday_metrics: dict with mean_daily_pnl_pct, trades_per_day, win_rate, max_intraday_drawdown_pct
          - deltas_by_stage: optional dict summarizing recent alpha/model/execution contributions
          - stage_health: optional coarse status per stage
          - branch_summary: optional aggregated summary from ml_branches_summary.json
        """
        import json as _J
        payload = context or {}
        default_intro = (
            "You are the Research Director for an intraday trading ML system.\n"
            "Use the provided INPUT to choose which stage to focus on next "
            "(alpha/model/execution) and define realistic intraday success criteria. "
            "Return ONLY strict JSON.\n"
        )
        system_intro = self._load_prompt_file("brain_director.md", default_intro)
        prompt = system_intro + "\n\nINPUT:\n" + _J.dumps(payload)
        t0 = datetime.now()
        result = asyncio.run(self._ask(prompt, operation="plan_research", timeout=120.0))
        dur_ms = int((datetime.now() - t0).total_seconds() * 1000)
        data = result.data if isinstance(result.data, dict) else {}
        self._trace_write(
            {
                "ts": datetime.now().isoformat(),
                "kind": "plan_research",
                "provider": result.provider_used,
                "model": self.model,
                "input_keys": list(payload.keys()),
                "output": data,
                "duration_ms": dur_ms,
                "success": 1 if result.success else 0,
                "error_type": result.error_type,
                "error_message": result.error_message,
                "fallback_used": 1 if result.fallback_used else 0,
                "providers_attempted": result.providers_attempted,
            }
        )
        return data if isinstance(data, dict) else {}

    def validate_agent_output(self, actor: str, proposal: Dict[str, Any], constraints: Dict[str, Any]) -> Dict[str, Any]:
        """Validate an agent proposal using the Critic / Referee prompt.

        Returns a dict with at least {valid: bool, violations: [...]}. Best-effort; on failure returns {}.
        """
        import json as _J
        payload = {
            "actor": actor,
            "proposal": proposal or {},
            "constraints": constraints or {},
        }
        default_intro = (
            "You are the Critic / Referee for an intraday trading AI Brain.\n"
            "You statically validate JSON outputs from agents against a fixed constitution and schema.\n"
            "Return ONLY strict JSON of the form {\"valid\": bool, \"violations\": [...] }.\n"
        )
        system_intro = self._load_prompt_file("critic_referee.md", default_intro)
        prompt = system_intro + "\n\nINPUT:\n" + _J.dumps(payload)
        t0 = datetime.now()
        result = asyncio.run(self._ask(prompt, operation="critic_validate", timeout=90.0))
        dur_ms = int((datetime.now() - t0).total_seconds() * 1000)
        data = result.data if isinstance(result.data, dict) else {}
        self._trace_write(
            {
                "ts": datetime.now().isoformat(),
                "kind": "critic_validate",
                "actor": actor,
                "provider": result.provider_used,
                "model": self.model,
                "input_keys": list(payload.keys()),
                "output": data,
                "duration_ms": dur_ms,
                "success": 1 if result.success else 0,
                "error_type": result.error_type,
                "error_message": result.error_message,
                "fallback_used": 1 if result.fallback_used else 0,
                "providers_attempted": result.providers_attempted,
            }
        )
        return data if isinstance(data, dict) else {}

    def _calc_session_duration(self, session: str, close_at: str) -> int:
        """Calculate session duration in minutes.

        Args:
            session: Session type (overnight/premarket/intraday/afterhours)
            close_at: Close time in HH:MM format

        Returns:
            Duration in minutes
        """
        # Session start times
        session_starts = {
            "overnight": "16:00",
            "premarket": "04:00",
            "intraday": "09:40",
            "afterhours": "16:05"
        }

        start = session_starts.get(session, "09:30")

        # Parse HH:MM
        try:
            start_h, start_m = map(int, start.split(":"))
            end_h, end_m = map(int, close_at.split(":"))
        except (ValueError, AttributeError):
            return 0

        start_minutes = start_h * 60 + start_m
        end_minutes = end_h * 60 + end_m

        # Handle overnight wraparound
        if session == "overnight" and end_minutes < start_minutes:
            end_minutes += 24 * 60

        return end_minutes - start_minutes

    def _validate_plan_consistency(self, plan: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        """Validate consistency of the proposed plan."""
        issues = []
        session = plan.get("session")
        providers = plan.get("providers") or []
        
        # Check 1: cycles vs time
        lflags = plan.get("llm_trader_flags", {})
        close_at = lflags.get("close_at")
        
        if session and close_at:
            try:
                expected_cycles = self._calc_cycles(session, close_at, 300) # placeholder
            except Exception:
                expected_cycles = 0
                
            if expected_cycles > 0:
                for prov in providers:
                    interval_sec = prov.get("interval_sec", 300)
                    cycles = prov.get("cycles", 0)
                    expected_cycles = self._calc_cycles(session, close_at, interval_sec)
                    
                    # Allow 20% tolerance
                    if abs(cycles - expected_cycles) > expected_cycles * 0.2:
                        issues.append({
                            "type": "cycles_mismatch",
                            "provider": prov.get("name"),
                            "actual_cycles": cycles,
                            "expected_cycles": expected_cycles,
                            "reason": f"Session {session} with close_at {close_at} should have ~{expected_cycles} cycles at {interval_sec}s interval"
                        })

        # Check 2: need_news vs symbols coverage
        need_news = set(plan.get("need_news") or [])
        all_symbols = set()
        for prov in providers:
            all_symbols.update(prov.get("symbols", []))

        if need_news and need_news != all_symbols:
            missing = all_symbols - need_news
            if missing:
                issues.append({
                    "type": "incomplete_news_coverage",
                    "missing_symbols": list(missing),
                    "reason": f"need_news missing symbols that are being traded: {missing}"
                })

        # Check 3: interval vs session type
        for prov in providers:
            interval = prov.get("interval_sec", 300)
            if session in ("overnight", "premarket") and interval < 600:
                issues.append({
                    "type": "interval_too_high_frequency",
                    "provider": prov.get("name"),
                    "session": session,
                    "interval_sec": interval,
                    "reason": f"{session} sessions should use >=600s intervals for low-frequency trading"
                })

        return {
            "ok": len(issues) == 0,
            "issues": issues
        }

    def propose_plan(self, session: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """Ask LLM for a Control Plan for the given session.

        Context may include: health snapshot, gates status, registry weights, etc.
        """
        # Inject trading universe into context
        enriched_context = {
            "session": session,
            "trading_universe": self.trading_universe,
            **(context or {})
        }
        profit_risk_goals = self._profit_risk_goals(context or {})
        enriched_context["profit_risk_goals"] = profit_risk_goals
        # Optional Critic feedback
        critic_feedback = {}
        if bool(os.getenv("CRITIC_ENABLED", "1") not in ("0", "false", "False")):
            try:
                from libs.critic.critic_client import CriticClient

                critic = CriticClient()
                critic_feedback = critic.check_plan(
                    {
                        "regime": enriched_context.get("regime"),
                        "strategy": enriched_context.get("strategy"),
                    }
                )
                enriched_context["critic_feedback"] = critic_feedback
                if critic_feedback.get("should_block"):
                    return {
                        "status": "blocked_by_critic",
                        "reason": "critic_should_block",
                        "critic_feedback": critic_feedback,
                        "session": session,
                    }
            except Exception as exc:  # pragma: no cover - defensive
                self.log.warning("critic_feedback_failed", error=str(exc))

        # 🆕 P0.2: Inject semantic memories into context
        memories_section = ""
        try:
            from scripts.ai_trader.memory.memory_fabric import query_similar_memories, mark_accessed_by_doc_id

            # Build memory query from session and context
            query_text = f"{session} session market conditions trading lessons"
            if enriched_context.get("regime"):
                query_text += f" {enriched_context['regime']}"

            # Retrieve top 3 similar memories
            memories = query_similar_memories(query_text, top_k=3)

            if memories:
                import json as _json
                memories_formatted = []
                for mem in memories:
                    try:
                        content = _json.loads(mem['text'])
                        if mark_accessed_by_doc_id and mem.get("id"):
                            mark_accessed_by_doc_id(mem.get("id"))
                        memories_formatted.append({
                            "lesson": content.get("lesson", content.get("summary", "")),
                            "relevance": f"{mem['score']:.2f}",
                            "context": {k: v for k, v in content.items() if k not in ("lesson", "summary")}
                        })
                    except Exception:
                        pass

                if memories_formatted:
                    memories_section = "\n\nHISTORICAL LESSONS (from past experiences):\n" + \
                                     _json.dumps(memories_formatted, indent=2) + "\n" + \
                                     "INSTRUCTION: Consider these lessons when proposing the plan.\n"
        except Exception as exc:
            self.log.warning("semantic_memory_retrieval_failed", error=str(exc))

        lessons_block = self._recent_memory_block()

        profit_risk_block = (
            "PROFIT/RISK OBJECTIVES:\n"
            f"- target_daily_pnl_pct={profit_risk_goals['target_daily_pnl_pct']}%, "
            f"target_weekly_pnl_pct={profit_risk_goals['target_weekly_pnl_pct']}%, "
            f"max_drawdown_pct={profit_risk_goals['max_drawdown_pct']}%\n"
            f"- min_confidence={profit_risk_goals['min_confidence']}, min_expected_ev={profit_risk_goals['min_expected_ev']} "
            "(downgrade to safe_mode + data/feature/repair actions if unmet; prefer hedged structures when volatility is high.)\n"
        )

        if enriched_context.get("_prompt_override"):
            prompt = enriched_context["_prompt_override"] + "\n\nINPUT:\n" + json.dumps(enriched_context, indent=2)
        else:
            prompt = CONTROL_PROMPT + "\n" + profit_risk_block + \
                "IMPORTANT: You MUST address critic_feedback. If critic_feedback.should_block is true, propose a safe/no-trade variant or defer.\n" + \
                "critic_feedback:\n" + json.dumps(critic_feedback, indent=2) + "\n" + \
                memories_section + lessons_block + \
                "INPUT:\n" + json.dumps(enriched_context, indent=2)
        # Save prompt for debugging
        try:
            from datetime import date
            debug_dir = self.runs_dir / date.today().isoformat()
            debug_dir.mkdir(parents=True, exist_ok=True)
            prompt_file = debug_dir / f"brain_prompt_{session}_{datetime.now().strftime('%H%M%S')}.txt"
            prompt_file.write_text(prompt, encoding="utf-8")
        except Exception:
            pass
        t0 = datetime.now()
        result = asyncio.run(self._ask(prompt, operation="propose_plan"))
        dur_ms = int((datetime.now() - t0).total_seconds() * 1000)
        plan = result.data
        raw_response = result.raw

        # Self-validation step
        validation_result = self._validate_plan_consistency(plan, enriched_context)
        if not validation_result["ok"]:
            # Log validation failure
            self.log.warning("plan_validation_failed",
                           session=session,
                           issues=validation_result["issues"])

            # Re-ask LLM with validation feedback
            retry_prompt = (
                f"Your previous plan has consistency issues:\n"
                f"{json.dumps(validation_result['issues'], indent=2)}\n\n"
                f"Please revise the plan to fix these issues. Original INPUT:\n"
                f"{json.dumps(enriched_context, indent=2)}"
            )

            try:
                retry_result = asyncio.run(self._ask(retry_prompt, operation="propose_plan_retry"))
                if retry_result.success:
                    plan = retry_result.data
                    self.log.info("plan_validation_retry_success", session=session)
                else:
                    self.log.error("plan_validation_retry_failed",
                                 session=session,
                                 error=retry_result.error_message)
                    # Keep original plan despite validation issues
            except Exception as e:
                self.log.error("plan_validation_retry_error",
                             session=session,
                             error=str(e))
                # Keep original plan despite validation issues

        # Save raw response for debugging
        try:
            from datetime import date
            debug_dir = self.runs_dir / date.today().isoformat()
            debug_dir.mkdir(parents=True, exist_ok=True)
            raw_file = debug_dir / f"brain_raw_response_{session}_{datetime.now().strftime('%H%M%S')}.txt"
            raw_file.write_text(raw_response, encoding="utf-8")
        except Exception:
            pass
        # Save
        out = self.runs_dir / Path(str(Path().cwd())) / "tmp"
        # Better path: runs_dir/YYYY-MM-DD/brain_plan_<session>.json
        d = self.runs_dir / (context.get("date") or "")
        if not d.name:
            from datetime import date
            d = self.runs_dir / date.today().isoformat()
        d.mkdir(parents=True, exist_ok=True)
        fp = d / f"brain_plan_{session}.json"
        fp.write_text(json.dumps(plan, indent=2), encoding="utf-8")
        # Trace call (best-effort) - now with error tracking
        self._trace_write({
            "ts": datetime.now().isoformat(),
            "kind": "propose_plan",
            "session": session,
            "provider": result.provider_used,
            "model": self.model,
            "input_keys": list(enriched_context.keys()),
            "output": plan,
            "duration_ms": dur_ms,
            "trading_universe_count": len(self.trading_universe),
            # Error tracking fields
            "success": 1 if result.success else 0,
            "error_type": result.error_type,
            "error_message": result.error_message,
            "fallback_used": 1 if result.fallback_used else 0,
            "providers_attempted": result.providers_attempted,
        })
        return plan

    def decide(self, action: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """Ask LLM to make a control decision for a given action.

        action: 'start_morning' | 'hpo_deploy' | 'risk_policy'
        context: dict with relevant metrics and statuses
        """
        prompt = DECISION_PROMPT + "\nINPUT:\n" + json.dumps({"action": action, **(context or {})})

        # Save prompt for debugging
        try:
            from datetime import date
            debug_dir = self.runs_dir / date.today().isoformat()
            debug_dir.mkdir(parents=True, exist_ok=True)
            ts_str = datetime.now().strftime('%H%M%S')
            prompt_file = debug_dir / f"brain_prompt_{action}_{ts_str}.txt"
            prompt_file.write_text(prompt, encoding="utf-8")
        except Exception as e:
            # Log error instead of silently passing
            import logging
            logger = logging.getLogger(__name__)
            logger.warning(f"Failed to save prompt: {e}")

        t0 = datetime.now()
        t0_perf = time.time()  # For metrics
        result = asyncio.run(self._ask(prompt, operation=f"decide_{action}"))
        dur_ms = int((datetime.now() - t0).total_seconds() * 1000)
        dur_sec = time.time() - t0_perf
        data = result.data
        raw_response = result.raw

        # Track metrics
        session = action  # action is like 'start_morning', 'hpo_deploy', etc.
        status = 'success' if result.success else 'error'
        metrics.brain_call_total.labels(session=session, status=status).inc()
        metrics.brain_call_duration_seconds.labels(session=session).observe(dur_sec)

        # Save raw response for debugging
        try:
            from datetime import date
            debug_dir = self.runs_dir / date.today().isoformat()
            debug_dir.mkdir(parents=True, exist_ok=True)
            ts_str = datetime.now().strftime('%H%M%S')
            raw_file = debug_dir / f"brain_raw_response_{action}_{ts_str}.txt"
            raw_file.write_text(raw_response, encoding="utf-8")
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.warning(f"Failed to save raw response: {e}")

        # Trace call (best-effort) - now with error tracking
        self._trace_write({
            "ts": datetime.now().isoformat(),
            "kind": "decide",
            "action": action,
            "provider": result.provider_used,
            "model": self.model,
            "input_keys": list((context or {}).keys()),
            "output": data,
            "duration_ms": dur_ms,
            # Error tracking fields
            "success": 1 if result.success else 0,
            "error_type": result.error_type,
            "error_message": result.error_message,
            "fallback_used": 1 if result.fallback_used else 0,
            "providers_attempted": result.providers_attempted,
        })
        # No file persistence here; caller may choose to persist
        return data

    def evaluate_plan(self, session: str, context: Dict[str, Any], plan: Dict[str, Any]) -> Dict[str, Any]:
        goals = self._profit_risk_goals(context or {})
        payload = {
            "session": session,
            "context_keys": list((context or {}).keys()),
            "plan": plan,
            "profit_risk_goals": goals,
        }
        prompt = PLAN_EVAL_PROMPT + "\nINPUT:\n" + json.dumps(payload)
        t0 = datetime.now()
        result = asyncio.run(self._ask(prompt, operation="evaluate_plan"))
        dur_ms = int((datetime.now() - t0).total_seconds() * 1000)
        data = result.data
        self._trace_write({
            "ts": datetime.now().isoformat(),
            "kind": "evaluate",
            "session": session,
            "provider": result.provider_used,
            "model": self.model,
            "output": data,
            "duration_ms": dur_ms,
            # Error tracking fields
            "success": 1 if result.success else 0,
            "error_type": result.error_type,
            "error_message": result.error_message,
            "fallback_used": 1 if result.fallback_used else 0,
            "providers_attempted": result.providers_attempted,
        })
        return data if isinstance(data, dict) else {}

    def record_plan_outcome(self, plan_id: str, exec_result: Dict[str, Any], summary: str = "brain_execution") -> Optional[Dict[str, Any]]:
        """
        Persist plan_vs_outcome after orchestrator/brain executes a plan.
        Expects exec_result to contain ev_plan/ev_realized/mdd and optional hedge metrics.
        """
        try:
            from ai.plan_outcome_logger import log_plan_vs_outcome  # type: ignore
        except Exception as exc:  # pragma: no cover - defensive
            self.log.warning("plan_outcome_logger_unavailable", error=str(exc))
            return None

        def _safe(val: Any, default: float = 0.0) -> float:
            try:
                return float(val)
            except Exception:
                return default

        ev_plan = _safe(exec_result.get("ev_plan") or exec_result.get("ev_expected"))
        ev_realized = _safe(exec_result.get("ev_realized") or exec_result.get("ev") or exec_result.get("pnl"))
        mdd = _safe(exec_result.get("mdd") or exec_result.get("max_drawdown"))
        hedge_effect = exec_result.get("hedge_effect") or {
            "hedge_pnl": exec_result.get("hedge_pnl"),
            "hedge_var_reduction": exec_result.get("hedge_var_reduction"),
        }
        issues = exec_result.get("issues") or []
        lessons = exec_result.get("lessons") or exec_result.get("notes") or []
        extra = {k: v for k, v in exec_result.items() if k not in {"ev_plan", "ev_expected", "ev_realized", "ev", "pnl", "mdd", "max_drawdown", "hedge_effect", "hedge_pnl", "hedge_var_reduction", "issues", "lessons", "notes"}}

        return log_plan_vs_outcome(
            plan_id=plan_id,
            summary=summary,
            ev_plan=ev_plan,
            ev_realized=ev_realized,
            mdd=mdd,
            hedge_effect=hedge_effect,
            issues=issues,
            lessons=lessons if isinstance(lessons, list) else [lessons],
            extra=extra,
        )

    def review_ml(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Review nightly ML results and suggest next actions.

        Returns a dict per ML_RESULT_REVIEW_PROMPT schema. Best-effort; on failure returns {}.
        """
        import json as _J

        # Build an optional AVAILABLE_COLUMNS block from INPUT.schemas so the
        # Brain can see which f./m5./d. columns actually exist instead of
        # guessing. This mirrors the alpha_expert guidance.
        available_block = ""
        try:
            schemas = context.get("schemas") or {}
            alias_for_table = {
                "trading.features_1m": "f",
                "trading.features_5m": "m5",
                "trading.features_1d_prev": "d",
            }
            cols: list[str] = []
            for tbl, alias in alias_for_table.items():
                table_cols = schemas.get(tbl) or []
                for c in table_cols:
                    name = c.get("name") or c.get("column")
                    if name:
                        cols.append(f"{alias}.{name}")
            if cols:
                cols = sorted(set(cols))
                max_cols = 120
                shown = cols[:max_cols]
                if len(cols) > max_cols:
                    shown.append("... (truncated)")
                available_block = (
                    "CURRENTLY AVAILABLE COLUMNS IN DB:\n"
                    + ", ".join(shown)
                    + "\n\nCRITICAL RULE: You can ONLY use columns listed above as inputs. "
                      "Do NOT invent column names. If you need a composite feature, "
                      "define it from scratch using base columns from this list.\n\n"
                )
        except Exception:
            available_block = ""

        prompt = ML_RESULT_REVIEW_PROMPT + "\n" + available_block + "INPUT:\n" + _J.dumps(context)
        t0 = datetime.now()
        result = asyncio.run(self._ask(prompt, operation="review_ml"))
        dur_ms = int((datetime.now() - t0).total_seconds() * 1000)
        data = result.data if isinstance(result.data, dict) else {}
        # Trace call
        self._trace_write({
            "ts": datetime.now().isoformat(),
            "kind": "review_ml",
            "provider": result.provider_used,
            "model": self.model,
            "output": data,
            "duration_ms": dur_ms,
            "success": 1 if result.success else 0,
            "error_type": result.error_type,
            "error_message": result.error_message,
            "fallback_used": 1 if result.fallback_used else 0,
            "providers_attempted": result.providers_attempted,
        })
        return data

    async def review_ml_async(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Async version of review_ml() for use within async event loops.

        Returns a dict per ML_RESULT_REVIEW_PROMPT schema. Best-effort; on failure returns {}.
        """
        import json as _J

        available_block = ""
        try:
            schemas = context.get("schemas") or {}
            alias_for_table = {
                "trading.features_1m": "f",
                "trading.features_5m": "m5",
                "trading.features_1d_prev": "d",
            }
            cols: list[str] = []
            for tbl, alias in alias_for_table.items():
                table_cols = schemas.get(tbl) or []
                for c in table_cols:
                    name = c.get("name") or c.get("column")
                    if name:
                        cols.append(f"{alias}.{name}")
            if cols:
                cols = sorted(set(cols))
                max_cols = 120
                shown = cols[:max_cols]
                if len(cols) > max_cols:
                    shown.append("... (truncated)")
                available_block = (
                    "CURRENTLY AVAILABLE COLUMNS IN DB:\n"
                    + ", ".join(shown)
                    + "\n\nCRITICAL RULE: You can ONLY use columns listed above as inputs. "
                      "Do NOT invent column names. If you need a composite feature, "
                      "define it from scratch using base columns from this list.\n\n"
                )
        except Exception:
            available_block = ""

        prompt = ML_RESULT_REVIEW_PROMPT + "\n" + available_block + "INPUT:\n" + _J.dumps(context)
        t0 = datetime.now()
        result = await self._ask(prompt, operation="review_ml")
        dur_ms = int((datetime.now() - t0).total_seconds() * 1000)
        data = result.data if isinstance(result.data, dict) else {}
        # Trace call
        self._trace_write({
            "ts": datetime.now().isoformat(),
            "kind": "review_ml",
            "provider": result.provider_used,
            "model": self.model,
            "output": data,
            "duration_ms": dur_ms,
            "success": 1 if result.success else 0,
            "error_type": result.error_type,
            "error_message": result.error_message,
            "fallback_used": 1 if result.fallback_used else 0,
            "providers_attempted": result.providers_attempted,
        })
        return data

    def print_provider_stats(self) -> None:
        """Print provider pool statistics (calls, successes, failures, fallbacks)"""
        self.pool.print_stats()

    def get_provider_stats(self) -> Dict[str, Any]:
        """Get provider pool statistics as dict"""
        return self.pool.get_stats()

    def design_feature_specs(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Design a small set of feature catalog specs using a focused prompt.

        Intended for offline tools; live ML loop should call the async variant.
        """
        import json as _J
        prompt = FEATURE_CATALOG_PROMPT + "\nINPUT:\n" + _J.dumps(context)
        t0 = datetime.now()
        result = asyncio.run(self._ask(prompt, operation="design_feature_specs"))
        dur_ms = int((datetime.now() - t0).total_seconds() * 1000)
        data = result.data if isinstance(result.data, dict) else {}
        self._trace_write({
            "ts": datetime.now().isoformat(),
            "kind": "design_feature_specs",
            "provider": result.provider_used,
            "model": self.model,
            "output": data,
            "duration_ms": dur_ms,
            "success": 1 if result.success else 0,
            "error_type": result.error_type,
            "error_message": result.error_message,
            "fallback_used": 1 if result.fallback_used else 0,
            "providers_attempted": result.providers_attempted,
        })
        return data if isinstance(data, dict) else {}

    async def design_feature_specs_async(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Async version of design_feature_specs() for use from the ML loop.

        INPUT context should be focused (schemas/health/gaps/recent_actions), not full ml_ctx.
        """
        import json as _J
        prompt = FEATURE_CATALOG_PROMPT + "\nINPUT:\n" + _J.dumps(context)
        t0 = datetime.now()
        result = await self._ask(prompt, operation="design_feature_specs")
        dur_ms = int((datetime.now() - t0).total_seconds() * 1000)
        data = result.data if isinstance(result.data, dict) else {}
        self._trace_write({
            "ts": datetime.now().isoformat(),
            "kind": "design_feature_specs",
            "provider": result.provider_used,
            "model": self.model,
            "output": data,
            "duration_ms": dur_ms,
            "success": 1 if result.success else 0,
            "error_type": result.error_type,
            "error_message": result.error_message,
            "fallback_used": 1 if result.fallback_used else 0,
            "providers_attempted": result.providers_attempted,
        })
        return data if isinstance(data, dict) else {}

    def suggest_feature_engineering(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        让AI Brain根据HPO/回测结果智能修改特征工程配置

        Args:
            context: 包含以下信息的字典:
                - recent_trials: 最近的HPO试验结果 (包含trial_id, params, oos_summary等)
                - available_features: 当前可用的原始特征列名
                - current_feature_programs: 当前的feature_programs.json内容
                - current_alpha_hpo_config: 当前alpha_config_enhanced.json中的特征相关HPO空间

        Returns:
            包含特征工程改进建议的字典:
                - analysis: 分析说明
                - feature_programs_patch: 对feature_programs.json的修改建议
                - alpha_hpo_patch: 对alpha_config_enhanced.json的修改建议
        """
        import json as _J

        # 构建完整的prompt
        prompt = FEATURE_ENGINEERING_PROMPT + "\n\nINPUT CONTEXT:\n" + _J.dumps(context, indent=2)

        # 调用LLM
        t0 = datetime.now()
        result = asyncio.run(self._ask(prompt, operation="suggest_feature_engineering", timeout=300.0))
        dur_ms = int((datetime.now() - t0).total_seconds() * 1000)

        data = result.data if isinstance(result.data, dict) else {}

        # Trace调用
        self._trace_write({
            "ts": datetime.now().isoformat(),
            "kind": "suggest_feature_engineering",
            "provider": result.provider_used,
            "model": self.model,
            "output": data,
            "duration_ms": dur_ms,
            "success": 1 if result.success else 0,
            "error_type": result.error_type,
            "error_message": result.error_message,
            "fallback_used": 1 if result.fallback_used else 0,
            "providers_attempted": result.providers_attempted,
        })

        # 保存建议到文件
        try:
            from datetime import date
            debug_dir = self.runs_dir / date.today().isoformat()
            debug_dir.mkdir(parents=True, exist_ok=True)
            ts_str = datetime.now().strftime('%H%M%S')
            suggestion_file = debug_dir / f"feature_engineering_suggestion_{ts_str}.json"
            suggestion_file.write_text(_J.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            self.log.info("feature_engineering_suggestion_saved",
                         file=str(suggestion_file),
                         programs_to_add=len(data.get("feature_programs_patch", {}).get("add_or_update", {})),
                         programs_to_delete=len(data.get("feature_programs_patch", {}).get("delete", [])))
        except Exception as e:
            self.log.warning("failed_to_save_suggestion", error=str(e))

        return data

    async def suggest_feature_engineering_async(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        异步版本的suggest_feature_engineering(),用于在异步事件循环中调用

        Args:
            context: 同suggest_feature_engineering()

        Returns:
            同suggest_feature_engineering()
        """
        import json as _J

        # 构建完整的prompt
        prompt = FEATURE_ENGINEERING_PROMPT + "\n\nINPUT CONTEXT:\n" + _J.dumps(context, indent=2)

        # 调用LLM
        t0 = datetime.now()
        result = await self._ask(prompt, operation="suggest_feature_engineering", timeout=300.0)
        dur_ms = int((datetime.now() - t0).total_seconds() * 1000)

        data = result.data if isinstance(result.data, dict) else {}

        # Trace调用
        self._trace_write({
            "ts": datetime.now().isoformat(),
            "kind": "suggest_feature_engineering",
            "provider": result.provider_used,
            "model": self.model,
            "output": data,
            "duration_ms": dur_ms,
            "success": 1 if result.success else 0,
            "error_type": result.error_type,
            "error_message": result.error_message,
            "fallback_used": 1 if result.fallback_used else 0,
            "providers_attempted": result.providers_attempted,
        })

        # 保存建议到文件
        try:
            from datetime import date
            debug_dir = self.runs_dir / date.today().isoformat()
            debug_dir.mkdir(parents=True, exist_ok=True)
            ts_str = datetime.now().strftime('%H%M%S')
            suggestion_file = debug_dir / f"feature_engineering_suggestion_{ts_str}.json"
            suggestion_file.write_text(_J.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            self.log.info("feature_engineering_suggestion_saved",
                         file=str(suggestion_file),
                         programs_to_add=len(data.get("feature_programs_patch", {}).get("add_or_update", {})),
                         programs_to_delete=len(data.get("feature_programs_patch", {}).get("delete", [])))
        except Exception as e:
            self.log.warning("failed_to_save_suggestion", error=str(e))

        return data
