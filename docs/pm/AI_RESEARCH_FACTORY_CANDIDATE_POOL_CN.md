# AI Research Factory Candidate Pool 设计

## 1. 目标
把 `promotion/retest/reject` 结果沉淀为可复用状态，不依赖 prompt 记忆。

## 2. 文件
- `runs/ai_trader/research/state/candidate_pool.json`
- `runs/ai_trader/research/state/lineage_index.json`

## 3. candidate_pool.json
字段：
- `experiment_id`
- `status`：`promoted | retest`
- `tier`
- `score`
- `market`
- `strategy_family`
- `promoted_at_cycle`
- `retest_reason`
- `updated_at`

## 4. lineage_index.json
字段：
- `family_id`
- `experiment_id`
- `parent_experiment_id`
- `mutation_type`
- `status`：`promoted | retest | rejected`
- `promoted_at_cycle`
- `retest_reason`
- `rejected_reason`

## 5. 使用方式
- Planner 可从 `candidate_pool` 抽取下轮 `promote_candidate/lineage_expand` 种子。
- Orchestrator/Evaluator 可用 `lineage_index` 追踪谱系晋级与淘汰。

