# AI Brain 自迭代 Gap & 改进计划（2025-12-09）

目标：让 Brain 具备「自我迭代→自编程→自部署」闭环，持续赚钱。基于最新代码现状（已读 sleep_loop/vector_store/brain multi-turn/DSL 强制等）与蓝皮书差距梳理如下。

## 当前进展（已具备的基座）
- 记忆：`memory_fabric` + `vector_store`（Chroma/FAISS/BGE-M3），STS/MTS/LTS 写入时同步向量存储；plan 前检索记忆注入 prompt（`brain.py`）。
- 睡眠：`memory/sleep_loop.py` 已做日终事件采集→LLM 200 字总结→STS + `daily_summaries.jsonl`，并触发 consolidate；可选 SkillSynthesizer 生成候选技能。
- 多轮/安全：Brain review 多轮工具调用（schema/dry_run），`propose_features` 强制 DSL→SQL，DataReadinessChecker/ActiveTableManager 在训练前护栏。

## 关键 Gap（阻碍“自我迭代赚钱”）
- 复盘闭环不足：缺少「计划 vs 实际」事后复盘、偏差记录与教训写入记忆（当前仅日总结，未做计划对账/critic 复盘）。
- 记忆治理不足：记忆 schema 松散，未统计命中率/新鲜度，未在 orchestrator/research 回路强制使用 lessons。
- 免疫/预测防御缺失：anomaly_detector 存在但未接入调度；无免疫案例自动生成/行动（reduce_sizing/pause）。
- Sim-to-Real 断点：自编程/skill_synth 产物未走编译/测试/白名单 auto-merge 流水；无收益目标/风险门槛驱动的自动上线。
- 守护化/解耦：research/self-evolution 仍耦合主进程，缺少独立守护 + 事件总线；工具调用协议也未覆盖 jobs/experiments。
- 可观测性缺失：无记忆/向量库健康、sleep_loop 成功率、tool_call 成功率、auto-evolution 成功率的仪表盘。

## 改进计划（逐步逼近“自我迭代盈利”）
1) 复盘与记忆强化（本周内）
   - 在 orchestrator/brain 回合结束后写入 `plan_vs_outcome` 记录（计划摘要、执行结果、偏差、教训），并写入 STS+vector store。
   - 在 plan 生成前强制读取最近的 `daily_summaries` 和 `plan_vs_outcome`（命中率计数，mark_accessed）并注入 prompt。
   - 为记忆定义最小 schema：`{type, summary, lesson, context, date, source}`，缺少字段时丢弃或补默认。

2) 工具链与免疫（1 周）
   - 完成 `tools_ml.py`（list_recent_jobs/get_job_details/list_valid_experiment_ids），接入 multi-turn tool_calls + prompt。
   - 接入 `anomaly_detector` 输出到调度：异常→记录免疫案例→触发 reduce_sizing/pause +事件；免疫案例写入记忆。
   - 扩展多轮 tool schema：必填字段、错误分级（retryable vs fatal），覆盖 jobs/experiments/DSL。

3) 自编程→安全上线（2 周）
   - 为 SkillSynthesizer/policy patch/code patch 建立流水：编译/单测/静态检查→白名单 auto-merge（非核心模块）→其余生成待审 PR；失败/成功写事件+记忆。
   - 在 code_evolver/git 流程加覆盖率/收益门槛：未达阈值则只产物待审，不 auto-merge。

4) 守护化与收益驱动（3 周）
   - 独立 research/self-evolution 守护进程（事件驱动），与交易主进程解耦；通过事件总线收集 metrics/状态/记忆。
   - 在 plan/review prompt 注入收益目标与风险约束（每日/周度目标、最大回撤），把“赚钱”作为 gate：无正期望/低置信度则降级为数据/特征/修复动作。

5) 可观测性与治理（并行推进）
   - 看板：sleep_loop 成功率、daily_summary 产出量、vector_store 命中率/大小、tool_call 成功率、auto-evolution 成功率/收益影响。
   - 审计：所有自动上线动作记录原因/证据/覆盖率/收益预估，支持回滚。

## 策略自迭代与多 LLM 策略委员会（对冲与盈利导向）
- 目标：多 LLM 本地“开会”产出策略→互评→合成最终方案，兼顾正期望、风控、对冲；先 paper trading 验证，通过后通知实盘切换。
- 行动项：
  1) 多 LLM 策略委员会（本周起草，下周接入）
     - 新建 `ai/strategy_council.py`（或 brain 子模块）：使用现有 `ProviderPool`（本地 ollama/qwen/deepseek）并行生成策略提案（标的、方向、信心、对冲建议、预期 EV、风控参数）。
     - 第二轮交叉点评：每个 LLM 读取其他提案，给出改进/质疑；记录评分与“信心/赔率/相关性”标签。
     - 聚合器生成最终草案：以 EV>0 且风控满足（止损/头寸上限/回撤阈值）的提案保留，并标注对冲对（多股/ETF/期权、或 IBKR 跨资产 FX/期指）。
  2) 对冲与风险算法（下周落地）
     - 在策略输出 schema 中强制包含 `hedge_plan`：{hedge_type: (pair/ratio/vega-neutral)、instrument: (ETF/期指/期权)、target_beta/vega/FX_exposure、trigger_rules}。
     - 采用简单基线：最小方差/目标 beta 对冲；若信心<阈值或波动>阈值则自动启用对冲或减仓；胜率低但赔率高场景允许小仓位+严格止损。
  3) Prompt/验证接入（下周）
     - 将策略委员会流程接入 Brain plan/review：先跑委员会→将最终方案与分歧注入 prompt，再由 Brain 生成执行计划（symbols/weights/hedge/风控）。
     - 增加审查角色回合：风险官（最大回撤/滑点/流动性）、执行官（可交易性、IBKR 跨市场可用性）、合规官（限制/持仓上限），提出阻断或调整。
  4) 落地与验证（2 周内形成闭环）
     - 执行路径：策略方案→paper trading（现有 paper_engine 或模拟执行）→写入 `plan_vs_outcome`（含 EV 偏差、对冲效果、回撤）。
     - 通过准则：正期望且回撤<阈值，覆盖率/成交率合格；对冲降低波动的实测证据。
     - 通过后触发通知（email/Slack/日志标记）请求人工确认切实盘；白名单后可自动切换。
 5) 评估与自我改进（持续）
   - 每次策略委员会记录：提案数、交叉点评分歧、最终采纳比例、paper EV vs 真实 EV 偏差、对冲贡献（波动/回撤下降），写入记忆+vector store。
   - 利用记忆检索让下轮委员会优先复用“成功模板/对冲方案”，减少重复踩坑。

## 与 TODO 对齐（执行清单指针）
- M1（T+7 天）：见 `ai/AI_BRAIN_GAP_TODO_2025-12-09.md` 中的 M1 阶段，包含策略委员会雏形、对冲 schema、plan_vs_outcome 写入与记忆强制读取、paper 回测接入。
- M2（T+14 天）：同 TODO 文件，覆盖 tools_ml、anomaly→免疫、自编程/skill 白名单流水、策略委员会接入 Brain 风控审查。
- M3（T+21 天）：同 TODO 文件，覆盖研究守护化、收益/风险门槛、自动上线网关、看板与 paper→live 通知。
- 细粒度接口/数据类/角色、风控与对冲模板、记录与审计要求等实施细节，全部在 `ai/AI_BRAIN_GAP_TODO_2025-12-09.md` 拆解，可直接按该文档执行与验收。

## 预期里程碑
- T+7 天：复盘链路上线，plan 前必读 lessons，工具链支持 jobs/experiments，免疫事件可触发降级动作。
- T+14 天：技能/代码补丁自动流水+白名单上线，异常检测护栏生效，收益/风险门槛嵌入决策。
- T+21 天：research 守护解耦，记忆/工具/免疫/上线全链路可观测，形成“看→想→做→复盘→上线”循环，朝“自我迭代赚钱”逼近。
