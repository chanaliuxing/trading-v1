# AI Research Factory Director 执行流

## 1. 主流程
1. Director 读取上一轮 bundle（含 `next_cycle_policy` / diagnostics）。
2. Director 编译并落盘 `AgentTask`。
3. 执行器仅在 `agent_tasks/tasks.json` 存在时允许执行。
4. Agent 只消费 `AgentTask`，生成 `ExperimentSpec`。
5. Execute 后写入 `ExperimentResult`（含 `task_id`）。
6. 产出审计索引与日报 bundle，供下一轮复用。

## 2. 新增产物
- `task_manifest.json`
- `task_execution_log.jsonl`
- `task_outcome_index.json`

## 3. 审计链路
- `derived_from -> task_id`：通过 `task_manifest.json` 与 role task 文件追溯。
- `task_id -> experiment_id -> result`：通过 `task_outcome_index.json` 与 `experiments/*.json` 追溯。

## 4. 执行硬门
- 没有 task 文件，不执行实验（No task file, no execution）。

