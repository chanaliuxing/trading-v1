"""
Prometheus Metrics for AI Trading System

Core metrics:
1. Brain call frequency/latency/success rate
2. Nightly task completion
3. Prompt A/B contribution
"""
try:
    from prometheus_client import Counter, Histogram, Gauge, Info
    METRICS_ENABLED = True
except ImportError:
    METRICS_ENABLED = False
    # Provide no-op implementations
    class Counter:
        def __init__(self, *args, **kwargs): pass
        def labels(self, *args, **kwargs): return self
        def inc(self, *args, **kwargs): pass

    class Histogram:
        def __init__(self, *args, **kwargs): pass
        def labels(self, *args, **kwargs): return self
        def observe(self, *args, **kwargs): pass

    class Gauge:
        def __init__(self, *args, **kwargs): pass
        def labels(self, *args, **kwargs): return self
        def set(self, *args, **kwargs): pass
        def inc(self, *args, **kwargs): pass
        def dec(self, *args, **kwargs): pass

    class Info:
        def __init__(self, *args, **kwargs): pass
        def info(self, *args, **kwargs): pass


# =============================================================================
# Brain Metrics
# =============================================================================

brain_call_total = Counter(
    'ai_brain_call_total',
    'Total number of AI Brain calls',
    ['session', 'status']  # session: morning/intraday/afterhours/overnight, status: success/error
)

brain_call_duration_seconds = Histogram(
    'ai_brain_call_duration_seconds',
    'Duration of AI Brain calls',
    ['session'],
    buckets=[1, 5, 10, 30, 60, 120, 300, 600]  # 1s to 10min
)

brain_plan_providers_count = Gauge(
    'ai_brain_plan_providers_count',
    'Number of providers in current brain plan',
    ['session']
)

brain_plan_symbols_count = Gauge(
    'ai_brain_plan_symbols_count',
    'Number of unique symbols in current brain plan',
    ['session']
)

brain_risk_rejections_total = Counter(
    'ai_brain_risk_rejections_total',
    'Total number of brain plans rejected by risk guard',
    ['session', 'reason']
)

# =============================================================================
# Nightly Task Metrics
# =============================================================================

nightly_task_completion = Gauge(
    'ai_nightly_task_completion',
    'Status of nightly tasks (1=completed, 0=failed/pending)',
    ['task']  # task: alpha_loop, rl_train, hpo_sweep, reflection, prompt_evolver
)

nightly_task_duration_seconds = Histogram(
    'ai_nightly_task_duration_seconds',
    'Duration of nightly tasks',
    ['task'],
    buckets=[60, 300, 600, 1800, 3600, 7200, 14400]  # 1min to 4hr
)

nightly_task_last_run_timestamp = Gauge(
    'ai_nightly_task_last_run_timestamp',
    'Unix timestamp of last successful run',
    ['task']
)

# =============================================================================
# Prompt A/B Testing Metrics
# =============================================================================

prompt_ab_decisions_total = Counter(
    'ai_prompt_ab_decisions_total',
    'Total number of decisions by prompt variant',
    ['prompt_id', 'ab_group', 'session']  # ab_group: champion/challenger
)

prompt_ab_performance_sharpe = Gauge(
    'ai_prompt_ab_performance_sharpe',
    'Sharpe ratio by prompt variant',
    ['prompt_id', 'ab_group']
)

prompt_ab_performance_pnl = Gauge(
    'ai_prompt_ab_performance_pnl',
    'Cumulative P&L by prompt variant',
    ['prompt_id', 'ab_group']
)

prompt_ab_weight = Gauge(
    'ai_prompt_ab_weight',
    'Current AB test weight allocation',
    ['ab_group']  # champion/challenger
)

# =============================================================================
# Strategy & Execution Metrics
# =============================================================================

strategy_decisions_total = Counter(
    'ai_strategy_decisions_total',
    'Total number of strategy decisions',
    ['strategy_id', 'provider', 'symbol', 'action']  # action: BUY/SELL/HOLD
)

strategy_pnl_total = Gauge(
    'ai_strategy_pnl_total',
    'Cumulative P&L by strategy',
    ['strategy_id', 'symbol']
)

provider_calls_total = Counter(
    'ai_provider_calls_total',
    'Total number of provider API calls',
    ['provider', 'status']  # status: success/error/budget_exceeded
)

provider_cost_usd = Gauge(
    'ai_provider_cost_usd',
    'Cumulative cost per provider',
    ['provider']
)

# =============================================================================
# Gate Metrics
# =============================================================================

gate_check_total = Counter(
    'ai_gate_check_total',
    'Total number of gate checks',
    ['gate', 'result']  # gate: data/health/llm/exec, result: pass/fail
)

gate_check_duration_seconds = Histogram(
    'ai_gate_check_duration_seconds',
    'Duration of gate checks',
    ['gate'],
    buckets=[0.1, 0.5, 1, 5, 10, 30]
)

# =============================================================================
# HPO & RL Metrics
# =============================================================================

hpo_deploy_total = Counter(
    'ai_hpo_deploy_total',
    'Total number of HPO deployments',
    ['strategy_id', 'status']  # status: deployed/rejected_oos_gate
)

hpo_oos_sharpe = Gauge(
    'ai_hpo_oos_sharpe',
    'Out-of-sample Sharpe from latest HPO',
    ['strategy_id']
)

rl_train_episodes_total = Counter(
    'ai_rl_train_episodes_total',
    'Total number of RL training episodes',
    ['symbol']
)

rl_train_reward_avg = Gauge(
    'ai_rl_train_reward_avg',
    'Average episode reward in RL training',
    ['symbol']
)

# =============================================================================
# System Health Metrics
# =============================================================================

system_health_score = Gauge(
    'ai_system_health_score',
    'Overall system health score (0-1)',
    []
)

autosettings_applied_total = Counter(
    'ai_autosettings_applied_total',
    'Total number of autosettings applied',
    ['key', 'status']  # status: applied/denied/pending_approval
)

# =============================================================================
# Memory Metrics
# =============================================================================

memory_fetch_total = Counter(
    'ai_memory_fetch_total',
    'Memory retrieval attempts and results',
    ['memory_type', 'result']  # result: hit/miss/invalid
)

memory_records_available = Gauge(
    'ai_memory_records_available',
    'Available memory records per tier',
    ['memory_type', 'tier']
)

# =============================================================================
# Research & Evolution Metrics
# =============================================================================

research_cycle_total = Counter(
    'ai_research_cycle_total',
    'Total number of research/self-evolution cycles executed',
    ['status', 'reason']  # status: success/error/skip
)

research_cycle_duration_seconds = Histogram(
    'ai_research_cycle_duration_seconds',
    'Duration of research/self-evolution cycles',
    ['reason'],
    buckets=[10, 30, 60, 120, 300, 600, 1800]
)

auto_evolution_outcome = Counter(
    'ai_auto_evolution_outcome_total',
    'Auto-evolution outcomes',
    ['status']  # status: success/fail/skip
)

auto_evolution_profit_delta = Gauge(
    'ai_auto_evolution_profit_delta',
    'Estimated PnL impact (delta EV) from recent auto-evolution actions',
    []
)

strategy_council_adoption_rate = Gauge(
    'ai_strategy_council_adoption_rate',
    'Adoption rate of strategy council proposals (accepted/total)',
    []
)

strategy_council_hedge_effect = Gauge(
    'ai_strategy_council_hedge_var_reduction',
    'Average hedge variance reduction from council plans',
    []
)

# =============================================================================
# Observability / Health Metrics
# =============================================================================

sleep_loop_total = Counter(
    'ai_sleep_loop_total',
    'Nightly sleep loop runs',
    ['status']  # status: success/fail/skip
)

vector_store_health = Gauge(
    'ai_vector_store_health',
    'Vector store availability (1=ok,0=down)',
    ['backend']
)

vector_store_size = Gauge(
    'ai_vector_store_size',
    'Vector store document count by backend',
    ['backend']
)

# =============================================================================
# Hedge Execution Metrics
# =============================================================================

hedge_executions_total = Counter(
    'ai_hedge_executions_total',
    'Total number of hedge executions',
    ['strategy', 'status', 'dry_run']  # strategy: options_put/options_collar/stock_short, status: success/failure
)

hedge_cost_usd = Histogram(
    'ai_hedge_cost_usd',
    'Hedge execution cost in USD',
    ['strategy'],
    buckets=[10, 50, 100, 250, 500, 1000, 2500, 5000, 10000]  # $10 to $10k
)

hedge_effectiveness = Gauge(
    'ai_hedge_effectiveness',
    'Current hedge effectiveness (correlation-based, 0-1)',
    ['strategy']
)

hedge_failure_reason_total = Counter(
    'ai_hedge_failure_reason_total',
    'Hedge execution failures by reason',
    ['reason']  # cost_exceeded, no_positions, timeout, integration_pending
)

tool_call_total = Counter(
    'ai_tool_call_total',
    'Tool call attempts',
    ['kind', 'status']  # kind: ml_job/brain_call/unknown, status: success/fail
)

# =============================================================================
# Options Hedging Metrics
# =============================================================================

hedging_selection_total = Counter(
    'ai_hedging_selection_total',
    'Total number of hedge contract selections',
    ['mode', 'status']  # mode: options/stock, status: success/no_need/rejected/error
)

hedging_execution_total = Counter(
    'ai_hedging_execution_total',
    'Total number of hedge executions',
    ['mode', 'status']  # mode: options/stock, status: submitted/rejected/dry_run/error
)

hedging_selection_duration_seconds = Histogram(
    'ai_hedging_selection_duration_seconds',
    'Duration of contract selection process',
    ['mode'],
    buckets=[0.1, 0.5, 1, 2, 5, 10, 30]
)

hedging_daily_contracts = Gauge(
    'ai_hedging_daily_contracts',
    'Number of option contracts executed today',
    []
)

hedging_daily_notional = Gauge(
    'ai_hedging_daily_notional_usd',
    'Total notional USD executed today',
    []
)

hedging_positions_open = Gauge(
    'ai_hedging_positions_open',
    'Number of open option positions',
    ['underlying']
)

hedging_positions_expiring_soon = Gauge(
    'ai_hedging_positions_expiring_soon',
    'Number of positions expiring within 3 days',
    []
)

hedging_portfolio_greeks = Gauge(
    'ai_hedging_portfolio_greeks',
    'Portfolio-level Greek exposures',
    ['greek']  # greek: delta/gamma/vega/theta
)

hedging_portfolio_pnl = Gauge(
    'ai_hedging_portfolio_pnl_usd',
    'Unrealized P&L from option positions',
    []
)

hedging_contract_quality = Histogram(
    'ai_hedging_contract_quality',
    'Quality metrics of selected contracts',
    ['metric'],  # metric: spread_bps/open_interest/delta_error
    buckets=[0, 5, 10, 20, 50, 100, 500, 1000, 5000, 10000]
)

hedging_rejection_total = Counter(
    'ai_hedging_rejection_total',
    'Total number of hedge rejections by reason',
    ['reason']  # reason: daily_limit/spread_too_wide/low_oi/validation_failed/no_contracts
)

# =============================================================================
# Utility Functions
# =============================================================================

def is_enabled() -> bool:
    """Check if metrics are enabled."""
    return METRICS_ENABLED
