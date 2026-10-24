# AI Research Factory AgentTask Schema

## 1. 目标
把 `policy -> AgentTask -> ExperimentSpec` 显式化，避免 agent 直接自由解释 policy。

## 2. 文件布局
- `agent_tasks/tasks.json`
- `agent_tasks/stocks_scalp.json`
- `agent_tasks/options_scalp.json`
- `agent_tasks/ml_brain.json`
- `agent_tasks/evaluator.json`

## 3. AgentTask 最小字段
- `task_id`
- `cycle_id`
- `agent_role`
- `task_type`
- `research_intent`
- `priority`
- `budget_cost`
- `input_refs`
- `task_patch`
- `derived_from`
- `blocked_by`

## 4. 追溯约束
- 每个 `ExperimentResult` 必须带 `task_id`。
- 每个 `task_id` 必须可映射到 `derived_from`（policy/recommendation 来源）。
- 同一 task 输入应生成稳定 `ExperimentSpec` 与稳定 `experiment_id`。

