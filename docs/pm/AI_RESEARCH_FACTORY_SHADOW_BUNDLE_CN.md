# AI Research Factory Shadow Bundle（CN）

## 目标
为晋级候选生成最小可执行 shadow/paper 入口包，不接实盘执行。

## 输入
- `promotion_gate.json`
- `allocation_plan.json`
- `candidate_results`

## 输出目录
- `shadow_candidates/<experiment_id>/`

## 每个候选必备文件
- `candidate_manifest.json`
- `experiment_ref.json`
- `allocation_ref.json`
- `execution_constraints.json`

## 约束
- 仅 `promotion_target in {paper_candidate, shadow_candidate}` 才生成 bundle。
- bundle 必须包含可消费的实验引用、资金约束、执行约束。

