# AI Research Factory Promotion Gate（CN）

## 目标
把 `completed/passed` 转换为显式晋级门，决定 `research_only/paper_candidate/shadow_candidate`。

## 输入
- `candidate_results`（实验执行结果）
- `portfolio_selection.json`

## 输出
- `promotion_gate.json`

## 字段
- `experiment_id`
- `gate_status`
- `promotion_target`
- `gate_reasons`

## 硬约束
- `robustness.ok != true` 不得进入 `paper_candidate/shadow_candidate`。
- `status in {blocked,deferred,failed,error}` 不得晋级。
- 不在组合入选集合内不得晋级。

