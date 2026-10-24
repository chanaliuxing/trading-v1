# AI Research Factory Daily Report Schema

## 1. 目录结构
每轮输出目录：`runs/ai_trader/research/cycles/<cycle_id>/`

必备文件：
- `cycle_spec.json`
- `agent_reports/plan.json`
- `agent_reports/data_readiness.json`
- `agent_reports/execution_reports.json`
- `experiments/*.json`
- `leaderboard.json`
- `promotion_decision.json`
- `portfolio_evaluation.json`
- `blocked_summary.json`
- `quality_gate_breakdown.json`
- `next_cycle_recommendations.json`
- `next_cycle_policy.json`
- `evaluation_bundle.json`

## 2. 消费者映射
- Director/Planner：`next_cycle_policy.json`、`data_readiness.json`、`promotion_decision.json`
- Evaluator 回放/审计：`leaderboard.json`、`quality_gate_breakdown.json`
- 收敛状态管理：`candidate_pool.json`、`lineage_index.json`（位于 `runs/.../state/`）

## 3. 合规约束
- 每个实验必须有终态：`completed | blocked | failed`。
- 没有结构化失败原因（`quality_gate.reasons`、`blocked_by`、`robustness.failure_type`）视为不合格产物。

