# AI Research Factory Portfolio Selection（CN）

## 目标
把单实验晋级改为组合级候选选择，输出可执行的组合选择结果。

## 输入
- `leaderboard.json`
- `promotion_decision.json`
- `portfolio_evaluation.json`
- `runs/ai_trader/research/state/candidate_pool.json`

## 输出
- `portfolio_selection.json`

## 关键字段
- `selected_candidates`
- `rejected_candidates`
- `allocation_plan`
- `selection_reason`
- `correlation_constraints`
- `risk_budget`

## 规则（MVP）
- 只允许质量门通过候选优先入选。
- 组合最多选 `2` 个候选。
- 超相关（`pair_correlation > 0.75`）候选拒绝，记录到 `rejected_candidates`。
- 必须保留至少一个“因相关性/风险被拒”样例。

