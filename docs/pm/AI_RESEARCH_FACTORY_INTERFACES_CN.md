# AI Research Factory Interfaces

## 1. 设计原则

- 先统一协议，不先统一实现。
- 先统一输入输出，不先统一数据库表。
- 先支持股票和期权两个 adapter，不追求一个 backtest engine 包打天下。
- 所有 agent 只能交换结构化对象。
- 已有子系统能力优先接总线，不重复发明第二套 robustness / cost / sizing / memory。

## 2. 核心对象

### 2.1 ResearchCycleSpec

定义一次每日研究的顶层边界。

```json
{
  "cycle_id": "2026-04-04-nightly",
  "run_mode": "paper_research",
  "markets": ["stocks", "options"],
  "symbols": ["SPY", "QQQ"],
  "date_range": {
    "start": "2026-03-01",
    "end": "2026-04-03"
  },
  "budget": {
    "max_experiments": 20,
    "max_parallel_agents": 5,
    "max_runtime_minutes": 120
  },
  "objectives": [
    "improve_intraday_scalp_sharpe",
    "reduce_drawdown",
    "increase_trade_quality"
  ]
}
```

### 2.2 AgentTask

Director 下发给子 agent 的最小任务单。

```json
{
  "task_id": "task-stocks-001",
  "cycle_id": "2026-04-04-nightly",
  "agent_role": "stocks_scalp",
  "goal": "search intraday scalp variants for QQQ",
  "constraints": {
    "max_variants": 6,
    "max_runtime_minutes": 25
  },
  "input_refs": {
    "research_context": "runs/ai_trader/research/context/2026-04-04.json",
    "ml_recommendation": "runs/ai_trader/research/ml_review/2026-04-04.json"
  },
  "search_space": {
    "templates": ["qqq_intraday_core"],
    "features": ["vwap", "atr_eff", "vol_surge"],
    "gates": ["session_window", "data_integrity", "risk_budget"]
  }
}
```

### 2.3 ExperimentSpec

统一实验输入协议。所有回测器都必须吃这个对象，哪怕只用其中一部分字段。

```json
{
  "experiment_id": "exp-stocks-qqq-001",
  "cycle_id": "2026-04-04-nightly",
  "market": "stocks",
  "strategy_family": "intraday_scalp",
  "engine": {
    "adapter": "stocks_kernel_backtest",
    "mode": "backtest"
  },
  "universe": {
    "symbols": ["QQQ"]
  },
  "strategy_ref": {
    "template_id": "qqq_intraday_core",
    "definition_id": null
  },
  "module_selection": {
    "features": ["vwap", "atr_eff", "vol_surge"],
    "gates": ["session_window", "risk_budget"],
    "risk_policy": "default_intraday",
    "execution_policy": "paper_fill_v1"
  },
  "parameters": {
    "entry_threshold": 0.6,
    "max_hold_minutes": 12,
    "risk_per_trade_bps": 15
  },
  "data_window": {
    "start": "2026-03-01 09:30:00",
    "end": "2026-04-03 16:00:00",
    "session": "rth"
  },
  "labels_and_ml": {
    "label_profile": "intraday_h5",
    "model_hint": "xgb_intraday_v3",
    "feature_patch_id": null
  },
  "robustness": {
    "required_checks": ["walk_forward", "leakage", "regime_split", "cross_symbol"],
    "min_walk_forward_folds": 3
  },
  "execution_reality": {
    "cost_model_id": "default_cost_v1",
    "fill_model_id": "paper_fill_v1",
    "latency_model_id": "baseline_latency_v1"
  },
  "sizing": {
    "sizing_policy_id": "risk_budget_v1",
    "capital_quota_policy_id": "default_quota_v1",
    "de_risk_on_drawdown": true
  },
  "lineage": {
    "family_id": "qqq-intraday-scalp",
    "parent_experiment_id": null,
    "mutation_type": "seed"
  },
  "acceptance": {
    "min_trades": 20,
    "max_drawdown_pct": 3.0
  }
}
```

### 2.4 BacktestAdapter Contract

所有 adapter 都必须实现相同行为。

```python
class BacktestAdapter(Protocol):
    adapter_name: str

    def validate(self, spec: dict) -> dict:
        ...

    def run(self, spec: dict) -> dict:
        ...

    def summarize(self, raw_result: dict) -> dict:
        ...
```

约束：

- `validate()` 负责拒绝非法实验配置。
- `run()` 负责调用真实 engine。
- `summarize()` 负责转成标准结果结构。

### 2.5 RobustnessAdapter

已有 ML robustness 能力的统一接入协议。

```python
class RobustnessAdapter(Protocol):
    adapter_name: str

    def validate_inputs(self, spec: dict) -> dict:
        ...

    def run(self, spec: dict, raw_result: dict) -> dict:
        ...
```

输出示例：

```json
{
  "wf_pass": true,
  "embargo_pass": true,
  "leakage_score": 0.01,
  "regime_stability": 0.72,
  "symbol_transfer_score": 0.65
}
```

### 2.6 ExecutionRealityAdapter

统一把股票 cost sim、期权 replay execution audit 归一化。

```python
class ExecutionRealityAdapter(Protocol):
    adapter_name: str

    def run(self, spec: dict, raw_result: dict) -> dict:
        ...
```

输出示例：

```json
{
  "modeled_slippage_bps": 8.2,
  "spread_cost_bps": 6.5,
  "fill_ratio": 0.94,
  "partial_fill_rate": 0.18,
  "latency_bucket": "sub_2s"
}
```

### 2.7 SizingPolicy

sizing 必须从“参数提示”升级为一等协议对象。

```python
class SizingPolicy(Protocol):
    policy_id: str

    def size_order(self, spec: dict, context: dict) -> dict:
        ...
```

最低要求字段：

- `sizing_policy_id`
- `capital_quota_policy_id`
- `de_risk_on_drawdown`
- `volatility_scale_rule`

### 2.8 LineageRecord

memory / evolution 进入研究总线后的统一谱系对象。

```json
{
  "family_id": "qqq-intraday-scalp",
  "experiment_id": "exp-stocks-qqq-001",
  "parent_experiment_id": "exp-stocks-qqq-000",
  "mutation_type": "gate_override",
  "promoted_from": null,
  "rejected_reason": null
}
```

### 2.9 ExperimentResult

所有实验必须产出统一结果，不允许只返回 engine 私有结构。

```json
{
  "experiment_id": "exp-stocks-qqq-001",
  "cycle_id": "2026-04-04-nightly",
  "status": "completed",
  "market": "stocks",
  "strategy_family": "intraday_scalp",
  "adapter": "stocks_kernel_backtest",
  "summary_metrics": {
    "trades": 38,
    "win_rate": 0.55,
    "net_pnl": 1245.5,
    "sharpe": 1.42,
    "max_drawdown_pct": 2.1,
    "profit_factor": 1.35
  },
  "quality_flags": {
    "data_ok": true,
    "enough_trades": true,
    "passed_acceptance": true
  },
  "robustness": {
    "wf_pass": true,
    "embargo_pass": true,
    "leakage_score": 0.01,
    "regime_stability": 0.72,
    "symbol_transfer_score": 0.65
  },
  "execution_reality": {
    "modeled_slippage_bps": 8.2,
    "spread_cost_bps": 6.5,
    "fill_ratio": 0.94,
    "partial_fill_rate": 0.18,
    "latency_bucket": "sub_2s"
  },
  "sizing": {
    "sizing_policy_id": "risk_budget_v1",
    "capital_allocated_usd": 15000.0
  },
  "lineage": {
    "family_id": "qqq-intraday-scalp",
    "parent_experiment_id": null,
    "mutation_type": "seed"
  },
  "artifacts": {
    "trade_log": "runs/ai_trader/experiments/exp-stocks-qqq-001/trades.parquet",
    "equity_curve": "runs/ai_trader/experiments/exp-stocks-qqq-001/equity.csv",
    "report": "runs/ai_trader/experiments/exp-stocks-qqq-001/report.json"
  },
  "notes": [
    "entry quality improved by vol_surge filter"
  ]
}
```

### 2.10 AgentReport

每个子 agent 回传给 Director 的标准格式。

```json
{
  "agent_role": "stocks_scalp",
  "cycle_id": "2026-04-04-nightly",
  "task_id": "task-stocks-001",
  "status": "completed",
  "experiments": [
    "exp-stocks-qqq-001",
    "exp-stocks-qqq-002"
  ],
  "top_candidates": [
    {
      "experiment_id": "exp-stocks-qqq-001",
      "reason": "best sharpe with acceptable drawdown"
    }
  ],
  "blocked_by": [],
  "next_actions": [
    "tighten entry threshold around 0.55 to 0.65"
  ]
}
```

### 2.11 PromotionDecision

Evaluator 给 Director 的决策对象。

```json
{
  "cycle_id": "2026-04-04-nightly",
  "decision_id": "promote-2026-04-04-01",
  "winners": [
    {
      "experiment_id": "exp-options-spy-003",
      "tier": "candidate"
    }
  ],
  "retest_queue": [
    {
      "experiment_id": "exp-stocks-qqq-001",
      "reason": "needs wider date window"
    }
  ],
  "rejected": [
    {
      "experiment_id": "exp-stocks-spy-002",
      "reason": "trade count too low"
    }
  ]
}
```

### 2.12 PortfolioEvaluation

不是只评单实验，而是评候选组合。

```json
{
  "cycle_id": "2026-04-04-nightly",
  "candidate_set": ["exp-stocks-qqq-001", "exp-options-spy-003"],
  "pairwise_correlation": 0.31,
  "marginal_risk_contribution": {
    "exp-stocks-qqq-001": 0.42,
    "exp-options-spy-003": 0.58
  },
  "capacity_fit": "ok",
  "drawdown_overlap_score": 0.28,
  "diversification_score": 0.74
}
```

## 3. MVP Adapter 列表

### 3.1 `stocks_kernel_backtest`

绑定：

- `strategies/stocks_kernel/`
- `services/backtest/`

责任：

- 把 `template_id + module_selection + parameters` 组装成股票实验。
- 输出标准交易账本和 PnL 汇总。

当前缺口：

- `stocks_kernel/runtime/replay.py` 还不是完整研究回测器。
- 需要补一个 adapter，把 kernel 逻辑与 `services/backtest` 结果归一化。
- 需要把已有 execution quality / cost 相关能力注入统一 `ExecutionRealityAdapter`。

### 3.2 `options_single_leg_replay`

绑定：

- `strategies/options_kernel/`
- `services/option_replay/`

责任：

- 把 `definition_id + policy overrides + contract rules` 组装成期权实验。
- 输出标准化结果结构。

当前约束：

- 第一阶段只支持单腿日内期权策略。
- 第一阶段先接 execution audit，不强求先支持所有组合策略。

## 4. 与现有代码的映射

### 4.1 Research Layer

- 当前入口：`scripts/ai_trader/research/agent.py`
- 当前编排：`scripts/ai_trader/research/orchestrator.py`
- 当前实验 stub：`scripts/ai_trader/research/experiment_api.py`

要求：

- `experiment_api.py` 从 JSONL stub 升级为正式协议落盘层。
- 研究层必须把 robustness / execution reality / lineage 一并落盘。

### 4.2 Scheduler Layer

- 当前入口：`scripts/ai_trader/ml_scheduler.py`

要求：

- 保留现有 ML job 调度逻辑。
- 新增对 `strategy_experiment` 的正式执行支持。
- 不要求第一期就改成高并发 scheduler，但至少要支持多 agent 任务顺序执行和结果归档。
- 允许先顺序执行，但结果结构必须完整。

### 4.3 ML Brain

- 当前入口：`scripts/ai_trader/brain/brain.py`
- 当前场景：`review_ml`, `evening_learning()`

要求：

- 继续产出特征、label、HPO 建议。
- 输出必须转成 `AgentReport`、`search_space patch` 或 `robustness evidence`，不能直接越权改策略执行逻辑。

## 5. 协议边界

### 5.1 子 agent 可以做什么

- 选择已注册模板或定义。
- 选择已注册特征、gates、risk policy。
- 调整受控参数搜索空间。
- 提交 `ExperimentSpec`。
- 提交 lineage、robustness、execution reality 所需结构化字段。

### 5.2 子 agent 不可以做什么

- 直接修改生产执行逻辑。
- 绕过 registry 引用未注册模块。
- 返回只有自然语言没有结构化结果的报告。
- 在没有数据 readiness 的情况下强行跑实验。

## 6. 最小落地要求

如果某个模块想接入研究工厂，必须先满足：

1. 有稳定的 `strategy_ref`。
2. 有 adapter。
3. 能生成 `ExperimentResult`。
4. 能被 Evaluator 消费。
5. 若已有 robustness / cost / memory / evolution 能力，必须暴露成统一协议字段。
