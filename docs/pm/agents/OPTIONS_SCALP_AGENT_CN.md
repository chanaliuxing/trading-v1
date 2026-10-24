# Options Scalp Agent

## 1. 角色定位

Options Scalp Agent 负责期权削头皮策略研究，第一期聚焦单腿日内策略。

## 2. 职责

- 从 `options_kernel` 定义注册表中挑选策略
- 根据 ML 建议调整信号、合约选择、风控参数
- 生成期权 `ExperimentSpec`
- 调用期权 replay adapter
- 输出结构化实验报告

## 3. 输入

- `AgentTask`
- Data Readiness report
- ML search space patch
- `options_kernel` definitions / policy registry

## 4. 输出

- 多个期权 `ExperimentSpec`
- 一份 `AgentReport`

## 5. 研究边界

第一期只允许：

- `SPY`, `QQQ`
- 单腿 `0DTE/1DTE` 日内方向性策略
- 已注册 `definition_id`

## 6. 关键约束

- 不做多腿 spread
- 不做组合保证金模拟
- 不允许没有 replay 审计结果就算成功

## 7. MVP 任务

- 支持从现有 `definition_id` 生成实验
- 支持 policy override
- 通过 `options_single_leg_replay` adapter 输出统一结果

## 8. 验收标准

- 至少支持 `SPY`, `QQQ` 单腿日内策略实验
- 结果能与股票实验进入同一个评估器
