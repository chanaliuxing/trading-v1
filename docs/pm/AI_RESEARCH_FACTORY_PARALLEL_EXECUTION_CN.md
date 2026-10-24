# AI Research Factory Parallel Execution

## 1. 目标

本文件用于并行开发编排，不再讨论方向，只定义：

- 哪 5 个开发 agent 并行开工
- 每个 agent 的文件 ownership
- 禁止改动范围
- 合并顺序
- 最小验收口

## 2. 全局规则

### 2.1 唯一协议源

- 统一协议源：`docs/pm/AI_RESEARCH_FACTORY_INTERFACES_CN.md`
- 统一协调源：`docs/pm/AI_RESEARCH_FACTORY_COORDINATION_CN.md`

### 2.2 并行规则

- 每个 agent 只能改自己 ownership 范围内的文件。
- 任何 agent 不允许重命名其他 agent 正在依赖的文件。
- 任何 agent 不能通过改 prompt 规避协议要求。
- 所有新增能力必须带最小测试。

### 2.3 统一验收底线

- 必须有代码
- 必须有测试
- 必须有失败路径
- 必须有样例产物或落盘证据
- 必须可被下游消费

## 3. Agent 编排

### Agent 1: Protocol / Persistence

目标：

- 冻结 `ExperimentSpec` / `ExperimentResult` / `LineageRecord` / 落盘规则
- 固定 `experiment_id` 生成逻辑
- 升级研究实验持久化层

Ownership:

- `scripts/ai_trader/research/experiment_api.py`
- 可新增：`scripts/ai_trader/research/contracts.py`
- 可新增：`tests/unit/scripts/ai_trader/research/test_experiment_api.py`

禁止改动：

- `scripts/ai_trader/ml_scheduler.py`
- `services/backtest/`
- `services/option_replay/`

交付：

- 稳定的 `create / persist / complete / fail` 接口
- 标准化实验目录与产物路径
- 失败态结果也可落盘

### Agent 2: Scheduler / Orchestrator

目标：

- 打通 `strategy_experiment`
- 串起 `validate -> run -> summarize -> robustness -> persist`
- 异常也能写失败态 `ExperimentResult`

Ownership:

- `scripts/ai_trader/ml_scheduler.py`
- `scripts/ai_trader/research/orchestrator.py`
- 可新增：`tests/test_ai_trader_tools_and_scheduler.py`

禁止改动：

- `services/backtest/`
- `services/option_replay/`
- `strategies/stocks_kernel/`
- `strategies/options_kernel/`

依赖：

- 依赖 Agent 1 的协议层
- 依赖 Agent 5 的 `RobustnessAdapter` 最小接口

### Agent 3: Stocks Adapter

目标：

- 实现 `stocks_kernel_backtest`
- 组装 `template_id + module_selection + parameters`
- 输出统一交易账本、指标和 execution reality

Ownership:

- 可新增：`services/backtest/stocks_kernel_adapter.py`
- 可新增：`tests/unit/backtest/test_stocks_kernel_adapter.py`

禁止改动：

- `scripts/ai_trader/ml_scheduler.py`
- `services/option_replay/`

依赖：

- 依赖 Agent 1 的 `ExperimentSpec` / `ExperimentResult`
- 输出给 Agent 2 和 Agent 5 消费

### Agent 4: Options Adapter

目标：

- 实现 `options_single_leg_replay`
- 映射 `definition_id + policy overrides`
- 把 execution audit 归一为统一结果结构

Ownership:

- 可新增：`services/option_replay/research_adapter.py`
- 可新增：`tests/unit/backtest/test_option_replay_research_adapter.py`

禁止改动：

- `scripts/ai_trader/ml_scheduler.py`
- `services/backtest/`

依赖：

- 依赖 Agent 1 的 `ExperimentSpec` / `ExperimentResult`
- 输出给 Agent 2 和 Agent 5 消费

### Agent 5: Robustness / Evaluator

目标：

- 实现 `RobustnessAdapter` 最小版
- 统一质量门槛、complexity penalty、cost normalization
- 实现 MVP `PromotionDecision` 和 `PortfolioEvaluation`

Ownership:

- 可新增：`scripts/ai_trader/research/robustness.py`
- 可新增：`scripts/ai_trader/research/evaluator.py`
- 可新增：`tests/unit/scripts/ai_trader/research/test_robustness.py`
- 可新增：`tests/unit/scripts/ai_trader/research/test_evaluator.py`

禁止改动：

- `services/backtest/`
- `services/option_replay/`
- `strategies/stocks_kernel/`
- `strategies/options_kernel/`

依赖：

- 依赖 Agent 1 的结果结构
- 消费 Agent 3 / 4 的 adapter 输出

## 4. 合并顺序

固定合并顺序：

1. Agent 1: Protocol / Persistence
2. Agent 5: Robustness / Evaluator
3. Agent 3: Stocks Adapter
4. Agent 4: Options Adapter
5. Agent 2: Scheduler / Orchestrator

理由：

- 先有协议，后有评估标准。
- 先有 adapter 输出结构，后把总线串起来。
- Scheduler 最后接，减少返工。

## 5. 最小里程碑

### M1

- `ExperimentResult` 可稳定落盘
- 失败态实验也能落盘

### M2

- `RobustnessAdapter` 最小版可运行
- 每个实验都带 `robustness`

### M3

- 股票 adapter 可产出标准化结果
- 期权 adapter 可产出标准化结果

### M4

- `strategy_experiment` 真正闭环
- 股票和期权实验能进入同一轮 leaderboard

## 6. 当前 PM 命令口径

发给开发 agent 的统一要求：

> 以接口文档为唯一协议源；每个 agent 只能修改自己 ownership 文件；所有变更必须附测试、样例产物和失败路径；优先保证 `RobustnessAdapter` 和 `strategy_experiment` 闭环。
