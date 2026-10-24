# AI Research Factory Agent Docs

本目录是统一研究工厂的角色说明。

## 角色列表

- `RESEARCH_DIRECTOR_AGENT_CN.md`
  - 顶层协调角色，负责拆任务和收敛结论。

- `DATA_READINESS_AGENT_CN.md`
  - 数据与回测输入守门员。

- `ML_BRAIN_AGENT_CN.md`
  - 现有 ML AI Brain 的统一接入角色。

- `STOCKS_SCALP_AGENT_CN.md`
  - 股票削头皮策略研究角色。

- `OPTIONS_SCALP_AGENT_CN.md`
  - 期权削头皮策略研究角色。

- `EVALUATOR_AGENT_CN.md`
  - 统一评估与晋级角色。

## 使用原则

- 每个 agent 只负责自己边界内的产出。
- 所有 agent 只能通过结构化协议交互。
- 不允许直接越权修改其他 agent 的核心职责。
