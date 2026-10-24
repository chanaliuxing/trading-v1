# AI Brain 自迭代落地 TODO（2025-12-09）

目标：让 AI Brain 能自我迭代策略与代码，基于本地多 LLM 策略委员会、对冲+风控、paper→live 验证闭环，为实盘赚钱做准备。

## 里程碑
- **M1（T+7 天）**：复盘链路 + 记忆强制读取 + 多 LLM 策略委员会雏形（提案/互评/聚合），对冲 schema 固化，paper 回测接入。
- **M2（T+14 天）**：工具链扩展（tools_ml/job/experiment）、anomaly→免疫行动、Skill/patch auto-flow 白名单、策略委员会接入 Brain 回路与风控审查。
- **M3（T+21 天）**：research 守护解耦、收益/风险门槛嵌入、自动上线网关（白名单）、可观测性看板，paper→live 通知路径打通。

## 工作分解（细化）
### 1) 策略委员会与对冲（高优先，M1 交付雏形）
- 架构与代码（拆解到函数/接口）
  - [x] 新建 `ai/strategy_council.py`，暴露：
    - `generate_proposals(ctx) -> List[StrategyProposal]`
    - `cross_review(proposals, ctx) -> List[ReviewFeedback]`
    - `synthesize_plan(proposals, feedbacks, ctx) -> FinalPlan`
    - `run_paper_plan(plan, ctx) -> PaperResult`
    - `run_council(ctx)` 端到端（提案→互评→合成→paper占位→落盘+记忆）
  - [x] 数据类字段（显式）：
    - StrategyProposal: `{symbols, side, weights, entry_rule, exit_rule, hold_horizon_min, expected_ev, confidence, vol_target, max_drawdown, win_rate_est, payoff_ratio_est, hedge_plan, risk_limits, liquidity_note, rationale}`
    - HedgePlan: `{hedge_type(pair/min_var/beta/vega), instrument(ETF/FUT/OPT/FX), ratio_or_target_beta, trigger_rules, fallback}`
    - ReviewFeedback: `{target_proposal_id, concerns, improvements, feasibility_score, hedge_score, risk_score}`
    - FinalPlan: `{base_proposal_id, merged_changes, hedge_plan, risk_limits, open_risks, approvals}`
  - [x] 聚合器逻辑：丢弃 `expected_ev<=0` 或 `max_drawdown>limit`；根据 `confidence`/`vol_target`/`beta_exposure` 生成最小方差或目标 beta 对冲；标记原因。（已实现基础过滤+低信心自动 hedge）
- Prompt 与角色（可直接引用）
  - [x] 第一轮角色：策略官/量化研究员/对冲专员，各自产出 1 方案。
  - [x] 第二轮角色：风险官/执行官/合规官，对每案打分并列出阻断条件；执行官需检查流动性/IBKR 可交易占位。
  - [x] 第三轮：合成官生成 FinalPlan（含 open_risks），必须满足：EV>0、risk_limits 满足、若信心<阈值则必须有 hedge_plan。
  - [x] 风控约束模板：`max_dd_pct<=5%`，`pos_limit_pct<=2%`，`slippage_bps<=10`，低胜率高赔率→`size<=0.5x` 且硬止损。
  - [x] 对冲规则模板：若 `confidence<0.6` 或 `realized_vol>threshold` → 启用 hedge；若相关性高→用 pair/min_var；若市场暴露明显→目标 beta/vega 中和。
- 验证与记录（可运行步骤）
  - [x] 将 FinalPlan 投喂 paper engine/backtest runner（占位接口，先用 `paper_engine.py`）执行，获取：`pnl, ev_realized, mdd, sharpe, hit_rate, payoff_ratio, hedge_pnl, hedge_var_reduction`。（已提供 `paper_runner` 钩子：在 ctx 传入 callable(plan, ctx)->metrics）
  - [x] 生成 `plan_vs_outcome` 记录：原 EV vs 实际 EV、回撤、对冲减波效果；写 STS + vector store。（`run_council`/`log_outcome_from_paper` 已写）
  - [x] 会议日志写入 `runs/ai_trader/<date>/strategy_council.jsonl`，包含提案/评分/分歧/采纳理由；同时写记忆（`record_meeting` 已写 STS）。
  - [x] CLI 验证：`python scripts/run_strategy_council.py --disable-llm` 生成 fallback 提案/plan_vs_outcome（paper stub），用于本地干跑。
- [x] CLI 支持自定义 `paper_runner`：`python scripts/run_strategy_council.py --paper-runner module.sub:func`；填充 ev_plan/ev_realized 进入 plan_vs_outcome。
- [x] 接入真实 paper/backtest：支持从外部 backtest/paper JSON 指标（`--paper-metrics-path` 或 ctx.paper_metrics_path/paper_metrics）生成 `PaperResult`，或通过 ctx 传 `paper_runner`（返回 metrics dict）并在 `run_council` 结束后把 ev_plan/ev_realized 写入 plan_vs_outcome（`log_outcome_from_paper(ev_plan_override=...)` 支持外部 EV）。

### 2) 复盘与记忆强化（M1 必须完成）
- [x] 在 orchestrator/brain 执行后写 `plan_vs_outcome`：`{plan_id, summary, exec_result, ev_plan, ev_realized, mdd, hedge_effect, issues, lessons, timestamp}`；写 STS+vector store。（已有辅助：`ai/plan_outcome_logger.py::log_plan_vs_outcome`）
- [x] plan 前强制读取：最近 N 条 `daily_summaries` + `plan_vs_outcome`，调用 `mark_accessed`，注入 prompt（含 lessons 摘要）。
- [x] 记忆校验/回收：schema 不全丢弃；统计命中率/访问次数，暴露到看板。

### 3) 工具链与免疫（M1→M2）
- [x] 实现 `scripts/ai_trader/brain/tools_ml.py`：`list_recent_jobs(job_type, limit)`、`get_job_details(job_id)`、`list_valid_experiment_ids()`；在 `control_event_consumer` 注册 tool_calls。
- [x] Tool schema 扩展：`{tool, args, required, retryable, on_error}`；支持 jobs/experiments/DSL 校验。
- [x] 接入 `anomaly_detector`：异常→写免疫案例→触发 `reduce_sizing/pause` 事件；把免疫案例写记忆并提示 Brain。

### 4) 自编程与安全上线（M2）
- [x] SkillSynth/code patch 流水：编译/单测/静态检查→白名单 auto-merge（非核心模块）；其余生成待审 PR；产出审计记录。
  - [x] code_evolver/git：增加 `coverage>=X`、`backtest_ev>0`、`mdd<阈值` 门槛；未达门槛禁止 auto-merge；记录理由/收益预估/回滚信息。
  - [x] 自编程骨架落地：`meta_agent/self_programming`（schema/engine/agents/infra/tools）可跑，含 GitSafetyLayer、FilePatcher、ToolRegistry、local LLM stub、Architect/Programmer/Tester/Reviewer stub、SelfProgrammingEngine stub。
  - [x] 占位逻辑实质化（详细落地清单）：
    - Architect/Programmer：接入 ProviderPool 本地 LLM 生成 DevPlan/CodePatchSet，输出具体修改建议（文件+片段）；支持安全/风格约束注入。
    - FilePatcher：增加模糊匹配、冲突检测、dry-run 报告（不直接覆盖时返回 diff）；默认在 GitSafetyLayer 沙盒中执行。
    - Tester：真实运行 pytest/子集；捕获 returncode/stdout/stderr，填充 TestReport（passed/failed/partial）。
    - Reviewer：增加静态检查（ruff/flake8/mypy 三选一）+ LLM 审查，输出 issues/阻断原因；结合 TestReport 生成 ReviewDecision。
    - Engine：串联 Architect→Programmer→Patcher→Tester→Reviewer，记录 tools_used，use_sandbox=True 时自动 commit/cleanup，失败回滚。
    - auto-merge 白名单：配置可 auto-merge 的路径/模块；通过测试+审查才 auto-merge 到 dev，其他生成待审 PR；落审计（理由/收益预估/回滚信息）。

### 5) 守护化与收益驱动（M2→M3）
- [x] 拆出 research/self-evolution 守护进程（事件驱动），与交易主进程解耦；事件总线传递状态/metrics/记忆。
- [x] plan/review prompt 注入收益/风险目标（每日/周度目标、回撤阈值）；无正期望或低置信度自动降级为数据/特征/修复动作。
- [x] paper→live 通知：日志/邮件/Slack 标记通过策略，人工确认后切换；白名单策略可自动切换。

### 6) 可观测性（并行）
- [x] 看板：sleep_loop 成功率、daily_summary 数量、vector_store 命中率/大小、tool_call 成功率、auto-evolution 成功率与收益影响、策略委员会采纳率/对冲减波效果。
- [x] 健康检查：vector_store 可用性/索引大小/碎片率；sleep_loop 成功/失败原因；anomaly/免疫事件频度。

## 依赖与注意事项
- 保持本地 LLM（ollama/qwen/deepseek）优先，避免外网依赖。
- 对冲需依赖可交易性校验（流动性/持仓限制/IBKR 跨资产可用性），执行官角色需明确否决条件。
- 所有自动上线动作必须有白名单和收益/风控门槛；核心交易/风控逻辑仍需人工确认。

## 验证与验收
- M1 验收：策略委员会可产出可运行的 paper 方案；plan 前必读记忆；plan_vs_outcome 持续写入。
- M2 验收：工具链完善、anomaly→免疫触发、auto-merge 白名单生效；策略方案经过风控/合规审查。
- M3 验收：守护解耦、收益/风险门槛内嵌、看板上线；paper→live 通知链路可用，正期望且受控回撤的策略可进入白名单自动切换。
