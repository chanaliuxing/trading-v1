# AI Research Factory Integration Wave 2

## 1. 目标

本轮目标不是继续补说明，而是拿到 3 个真实证据：

1. 一个真实股票实验结果
2. 一个真实期权实验结果
3. 一个真实统一 leaderboard / promotion_decision / portfolio_evaluation

## 2. 统一要求

- 所有改动必须附测试
- 所有改动必须附最小真实输入样例
- 所有改动必须附实际产物路径或 JSON 片段
- 不允许只说“已支持”，必须给落盘证据
- 必须至少提供一个 `completed` 和一个 `failed/blocked` 的 `ExperimentResult`

## 3. Agent Ownership

### Agent A: Scheduler / Protocol

Ownership:
- `scripts/ai_trader/ml_scheduler.py`
- `scripts/ai_trader/research/experiment_api.py`
- `scripts/ai_trader/research/contracts.py`
- `tests/test_ai_trader_tools_and_scheduler.py`

任务：
- 在 scheduler 主路径显式接入 `stable_experiment_id()`
- 统一 `created / blocked / failed / completed`
- 定义重跑同一 spec 的 experiment_id 策略

### Agent B: Robustness

Ownership:
- `scripts/ai_trader/research/robustness.py`
- `scripts/ai_trader/ml_scheduler.py`
- 可新增对应测试

任务：
- 提供默认 robustness runner
- `completed` 实验不得只产出 skipped robustness
- 至少产出 `wf_pass / embargo_pass / leakage_score / regime_stability`

### Agent C: Stocks E2E

Ownership:
- `services/backtest/stocks_kernel_adapter.py`
- 可新增最小 E2E 支撑测试

任务：
- 跑 1 个真实股票实验
- 输出真实 trade log / equity / result
- 确认 execution_reality 不是空壳

### Agent D: Options E2E

Ownership:
- `services/option_replay/research_adapter.py`
- 可新增最小 E2E 支撑测试

任务：
- 跑 1 个真实期权实验
- 输出真实 fills / orders / selection audit
- 确认 execution_reality 带实际字段

### Agent E: Evaluator / Leaderboard

Ownership:
- `scripts/ai_trader/research/evaluator.py`
- 可新增测试

任务：
- 用 1 个股票 + 1 个期权真实结果生成真实 leaderboard
- 输出 `promotion_decision`
- 输出 `portfolio_evaluation`

## 4. 优先级

1. 默认 robustness 总线
2. stable_experiment_id 接线
3. 真实股票实验
4. 真实期权实验
5. 真实 leaderboard

## 5. 交付清单

- 修改文件列表
- 测试命令
- 测试结果
- 真实产物目录
- 一个 `completed` 样例
- 一个 `failed/blocked` 样例
