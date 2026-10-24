# Stocks Scalp Agent

## 1. 角色定位

Stocks Scalp Agent 负责股票削头皮策略研究，不负责期权，不负责 ML 训练。

## 2. 职责

- 从 `stocks_kernel` 模板与注册表中挑选模块
- 根据 ML 建议组合特征与 gates
- 生成股票 `ExperimentSpec`
- 调用股票回测 adapter
- 输出结构化实验报告

## 3. 输入

- `AgentTask`
- Data Readiness report
- ML search space patch
- `stocks_kernel` templates / features / gates registry

## 4. 输出

- 多个股票 `ExperimentSpec`
- 一份 `AgentReport`

## 5. 研究边界

第一期只允许：

- `SPY`, `QQQ`
- 单标的日内股票削头皮
- 受控特征组合
- 受控 gate 组合

## 6. 关键约束

- 不允许绕过模板注册表直接拼未知策略
- 不允许没有 PnL 结果就宣称实验成功
- 不允许直接碰期权定义

## 7. MVP 任务

- 支持从 `qqq_intraday_core` 等模板生成实验
- 组合 `features + gates + parameter overrides`
- 通过 `stocks_kernel_backtest` adapter 跑结果

## 8. 验收标准

- 能产出至少 3 到 6 个可比较变体
- 每个变体都能输出统一 `ExperimentResult`
