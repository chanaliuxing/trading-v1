# AI Research Factory PRD

## 1. 文档目标

本文件固化一个统一的研究工厂需求：

- 复用现有 ML AI Brain 能力，而不是推倒重来。
- 每天自动运行，拆成多个子 agent 并行研究。
- 通过可插拔行情、特征、策略模块组装实验。
- 用统一实验协议驱动股票和期权回测。
- 最后产出可比较、可复盘、可晋级的实验结果。

## 2. 问题定义

当前系统已经有三类能力，但没有形成一个统一闭环：

1. ML Brain
   - 现有 `scripts/ai_trader/brain/brain.py` 已经能做 `review_ml`。
   - `scripts/ai_trader/orchestrator.py` 已经有 `evening_learning()` 夜间学习流程。

2. Strategy Research
   - `scripts/ai_trader/research/agent.py` 能提出实验建议。
   - `scripts/ai_trader/research/orchestrator.py` 能把 `strategy_experiment` 映射成 job type。

3. Backtest / Replay
   - 股票通用回测在 `services/backtest/`。
   - 股票策略模块化骨架在 `strategies/stocks_kernel/`。
   - 期权单腿 replay 在 `services/option_replay/`。
   - 期权定义驱动策略骨架在 `strategies/options_kernel/`。

当前核心缺口：

- 没有统一的 `ExperimentSpec` / `ExperimentResult`。
- `strategy_experiment` 已映射到调度器，但执行闭环未补齐。
- 股票和期权回测能力分裂，无法统一比较。
- `research agent` 仍是单次 LLM 规划，不是日常多子 agent 研究工厂。
- `ml_scheduler` 仍是单槽模型调度，不适合作为多策略研究调度器。

## 2.1 诊断原则修正

本项目当前的很多问题，不应再写成“没有某能力”，而应区分成两类：

1. 已有子系统能力，但未统一接入研究工厂总线
   - 例如 walk-forward、leakage、drift、cost sim、sizing、memory、evolution、live parity。

2. 确实缺失的统一基础设施
   - 例如 `strategy_experiment` 执行闭环。
   - 例如跨股票/期权统一评分。
   - 例如研究层 portfolio evaluation。

本 PRD 后续一律使用这套表述，避免把“未解耦、未协议化”误写成“完全不存在”。

## 3. 目标

### 3.1 业务目标

- 每个交易日自动启动一轮研究。
- 同时研究股票削头皮与期权削头皮策略。
- 每个子 agent 专注一种职责，避免一个 agent 做所有事情。
- 每轮都能输出可比较的实验榜单。
- 保留 ML Brain 作为特征、模型、HPO 研究角色。

### 3.2 工程目标

- 用统一协议衔接 Research、ML、Backtest、Evaluation。
- 把已有的 robustness、execution、sizing、memory、evolution 能力接入统一总线。
- 先做最小可跑，再扩到更复杂策略。
- 尽量复用现有模块，不新增第二套平行系统。

## 4. 非目标

第一期不做：

- 多腿期权组合回测。
- 保证金、指派、行权、组合保证金的完整模拟。
- 自动改代码并直接上线真实策略。
- 让 LLM 自由发明未注册的特征或策略模块。
- 在第一期同时支持股票、期权、期货、加密等所有资产类别。

## 5. 第一性原理

研究工厂最小闭环只需要五件事：

1. 明确研究对象
   - 今天研究什么市场、什么策略族、什么模板。

2. 明确可变项
   - 可切换哪些特征、门控、参数、合约选择规则。

3. 明确运行器
   - 由哪个 backtest adapter 执行实验。

4. 明确结果
   - 每个实验都必须输出标准化指标和工件路径。

5. 明确决策
   - 哪些策略继续，哪些淘汰，哪些进入下一轮细化。

如果这五件事不能标准化，多 agent 只会制造噪音，不会增加产出。

进一步说，当前系统最大问题不是“没有零件”，而是：

- robustness 在 ML 子系统里
- execution reality 在回测与 replay 子系统里
- sizing 在 risk / strategy 子系统里
- memory / evolution 在 memory / evolution 子系统里
- live parity 在 live proof / parity 子系统里

这些零件没有通过统一协议接进研究主路径，所以还不是同一台机器。

## 6. MVP 范围

### 6.1 研究范围

- 股票
  - 标的：`SPY`, `QQQ`
  - 策略：单标的日内削头皮
  - 载体：`strategies/stocks_kernel/`

- 期权
  - 标的：`SPY`, `QQQ`
  - 策略：单腿 `0DTE/1DTE` 日内方向性策略
  - 载体：`strategies/options_kernel/` + `services/option_replay/`

### 6.2 研究动作

- 选择模板或定义
- 选择可插拔特征集合
- 选择可插拔 gate / risk / contract policy
- 生成实验配置
- 跑回测
- 汇总结果并排序

### 6.3 每日产出

- 一份研究计划
- 一组实验执行记录
- 一份统一排名结果
- 一份推荐进入下一轮的候选清单

## 7. 角色设计

运行时采用 `1 个 Director + 5 个执行子 agent`：

1. Research Director Agent
   - 负责拆任务、分配预算、收敛研究方向。

2. Data Readiness Agent
   - 负责数据可用性、覆盖率、回测输入就绪检查。

3. ML Brain Agent
   - 负责特征、模型、HPO、label 与信号质量研究。

4. Stocks Scalp Agent
   - 负责股票削头皮策略组装与回测。

5. Options Scalp Agent
   - 负责期权削头皮策略组装与 replay。

6. Evaluator Agent
   - 负责统一打分、去重、排序、晋级建议。

## 8. 成功标准

### 8.1 MVP 成功标准

- 每天可以自动跑完一轮研究。
- 至少能并行提出并执行股票和期权实验。
- 所有实验都产出统一结果结构。
- ML Brain 的特征建议可以进入策略实验搜索空间。
- 当天结果可以被 Evaluator 统一排序。

### 8.2 工程验收标准

- `strategy_experiment` 有真实执行器。
- 股票与期权都存在 `backtest adapter`。
- 统一结果结构落盘到稳定位置。
- 子 agent 之间只通过协议交互，不直接耦合内部实现。
- robustness、execution reality、sizing、lineage、portfolio evaluation 都有明确协议位置。

## 9. 现状到目标的差距

### 9.1 已有基础

- ML review 能力已经存在。
- 夜间学习主流程已经存在。
- 股票 kernel 和期权 kernel 都已有较强模块化基础。
- 期权 replay 对单腿日内策略已经有较完整闭环。
- ML 子系统里已有 walk-forward、leakage / drift 检查与 OOS 评估基础。
- 股票与期权侧已经各自存在 cost / fill / audit 相关能力。
- memory / evolution / live parity 能力已有局部实现。

### 9.2 必补缺口

- `experiment_api.py` 现在还是 JSONL stub，不是正式实验协议层。
- `ml_scheduler.py` 是单槽执行器，不是研究调度器。
- 股票 kernel replay 目前偏逻辑 replay，缺交易账本和 PnL 标准输出。
- 期权 replay 现在偏单腿 engine，还没有统一接入 research layer。
- `research/agent.py` 还没有直接使用统一子 agent 执行模型。
- robustness 还不是 `Experiment -> Robustness -> Evaluator` 的强制主路径。
- execution reality 还没有统一注入 `ExperimentResult`。
- sizing 还不是实验协议中的一等对象。
- memory / evolution 还没有沉淀为策略谱系对象。
- 研究层还缺 portfolio evaluation。

## 10. 最小实施顺序

### Phase 0: 文档与协议

- 固化需求
- 固化接口
- 固化 agent 分工

### Phase 1: 统一实验协议

- 定义 `ExperimentSpec`
- 定义 `ExperimentResult`
- 定义 `AgentTask` / `AgentReport`
- 定义 `RobustnessAdapter`
- 定义 `ExecutionRealityAdapter`
- 定义 `SizingPolicy`
- 定义 `LineageRecord`
- 定义 `PortfolioEvaluation`

### Phase 2: 补齐执行器

- 实现 `strategy_experiment` 执行器
- 接入股票 adapter
- 接入期权 adapter

### Phase 3: 建立评估器

- 统一结果归档
- 统一指标计算
- 统一排名与晋级逻辑

### Phase 4: 自动日跑

- 由 Director 发起每日研究
- 各 agent 产出结果
- Evaluator 汇总后写日报

### Phase 5: 研究总线增强

- 把 robustness 前置成强制门槛
- 把 execution reality 纳入统一评分
- 把 sizing 固化到实验协议
- 把 lineage / portfolio evaluation 接入晋级逻辑

## 11. 核心决策

### 决策 1

保留现有 ML Brain，不另造一个新的 machine learning agent。

### 决策 2

股票与期权回测不强行先统一成一个 engine，而是先统一成同一个 adapter 协议。

### 决策 3

第一期只允许从注册表里挑模块，不允许 agent 发明未注册模块直接执行。

### 决策 4

第一期先做研究闭环，不做自动实盘上线闭环。

### 决策 5

凡是仓库里已经存在的 robustness / cost / sizing / memory / evolution / parity 能力，优先接总线，不重复造轮子。

## 12. 统一总线升级结论

下一阶段的目标不是“继续增加 agent 数量”，而是依次接入五层统一总线：

1. `Robustness Layer`
2. `Execution Reality Layer`
3. `Sizing Policy Layer`
4. `Lineage / Evolution Layer`
5. `Portfolio Evaluation Layer`

## 13. 这份 PRD 对开发的要求

后续所有开发任务必须回答四个问题：

1. 属于哪个 agent 的职责。
2. 输入和输出协议是什么。
3. 依赖哪个 adapter 或 registry。
4. 验收结果如何进入统一 `ExperimentResult`。
