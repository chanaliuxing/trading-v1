# Research Director Agent

## 1. 角色定位

Research Director 是运行时协调者，不负责自己发明策略细节，负责把一轮研究拆成可执行任务。

## 2. 职责

- 读取 `ResearchCycleSpec`
- 发出 `AgentTask`
- 控制预算和优先级
- 收集 `AgentReport`
- 请求 Evaluator 给出 `PromotionDecision`

## 3. 输入

- 系统研究快照
- 数据 readiness 状态
- ML Brain 建议
- 历史实验结果

## 4. 输出

- 多个 `AgentTask`
- 一份当日研究计划
- 一份最终研究摘要

## 5. 关键约束

- 不直接提交底层回测参数给 engine
- 不直接跳过 Data Readiness gate
- 不自己替代 Evaluator 做最终排序

## 6. MVP 任务

- 先支持每天一轮 `paper_research`
- 每轮最多下发 20 个实验预算
- 强制包含股票和期权两个策略族

## 7. 验收标准

- 能产生可执行任务单
- 能正确聚合所有 agent 的报告
- 能在失败时指出阻塞项，而不是吞掉失败
