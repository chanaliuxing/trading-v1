# AI Research Factory 收敛闭环（Convergence Loop）

## 1. 目标
把上一轮 `daily report bundle` 直接转成下一轮研究输入，形成最小自动收敛闭环：

`Bundle -> next_cycle_policy -> Planner Patch -> Next ExperimentSpec`

## 2. 输入文件（上一轮）
- `blocked_summary.json`
- `quality_gate_breakdown.json`
- `next_cycle_recommendations.json`
- `promotion_decision.json`
- `portfolio_evaluation.json`

## 3. 编译产物（策略层）
- `next_cycle_policy.json`

关键字段：
- `suppressions.markets/symbols`
- `retest_experiments`
- `promote_candidates`
- `robustness_adjustments`
- `budget_allocation`

## 4. Planner 落地规则
- `blocked_by_data`：下轮默认抑制对应 `market/symbol`，`research_intent=retest_data`。
- `blocked_by_robustness`：下轮生成 `retest_robustness`，优先 `increase_folds/reduce_complexity/window_expand`。
- `winners`：下轮生成 `lineage_expand/promote_candidate`。
- 同时保留跨资产最小 seed，但必须通过 readiness 前置守门。

## 5. 验收口径
1. 上一轮 bundle 自动生成 `next_cycle_policy.json`。
2. `agent.py` 消费 policy 后，下一轮 `ExperimentSpec` 与上一轮有可观测差异。
3. 至少一类 blocked case 被抑制，至少一类 passed case 被延伸。

