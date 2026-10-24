# Data Readiness Agent

## 1. 角色定位

Data Readiness Agent 是所有研究实验的前置守门员。

## 2. 职责

- 检查股票 bars 是否可用
- 检查 features 是否就绪
- 检查期权 quotes 是否覆盖
- 检查实验窗口是否可回测
- 给出可跑 / 降级 / 阻塞结论

## 3. 输入

- `ResearchCycleSpec`
- 标的列表
- 时间窗口
- 市场类型

## 4. 输出

```json
{
  "agent_role": "data_readiness",
  "status": "completed",
  "market_status": {
    "stocks": "ready",
    "options": "degraded"
  },
  "blocking_issues": [],
  "warnings": [
    "options quotes coverage under threshold for QQQ on 2 sessions"
  ]
}
```

## 5. 与现有系统的关系

- 复用现有 data healer / data check / research state 数据源。
- 不重新发明第二套健康检查框架。

## 6. 关键约束

- 没有 readiness，不允许其他 agent 直接跑实验。
- 只能报告事实和等级，不负责修改策略。

## 7. MVP 任务

- 股票：检查 `SPY`, `QQQ` 的回测窗口完整性
- 期权：检查 `options_quotes_1m` 覆盖率
- 输出统一 readiness report

## 8. 验收标准

- 能对股票和期权分别给出 ready / degraded / blocked
- 能明确说明阻塞来自数据缺失还是数据质量不足
