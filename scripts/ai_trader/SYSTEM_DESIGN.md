# AI Trader 系统设计（控制平面）

## 目标
- 以“门控→计划→执行→评估→进化”的闭环方式，统一调度 run_system 与 llm_trader，实现持续提升 Sharpe、受控回撤与成本管理。
- 外环（HPO/反思）决定参数与策略的长期演进；内环（RL）在分钟级对仓位执行动态优化；两者均带门槛与大脑批准。

## 组件与职责
- Orchestrator（scripts/ai_trader/orchestrator.py）
  - 会话编排：morning/intraday/afterhours/overnight/learn
  - 门控：健康/数据/执行/LLM
  - 大脑：计划（propose_plan）与决策（decide）
  - 学习：反思→处方→HPO→参数回写→AB→写记忆→RL 训练/评估/启停→报告与 MLflow
  - Pending：处方缓冲，晨间执行（带大脑批准）
- Gates（scripts/ai_trader/gates/*）
  - health/data/exec/llm：各自独立校验
- Controllers（scripts/ai_trader/controllers/*）
  - llm_trader_controller：计划到命令映射与 Paper 注入
  - run_system_controller：HPO/参数写回、RL 训练/评估等命令
- Brain（scripts/ai_trader/brain/brain.py）
  - propose_plan：带 MemoryContext 的结构化计划（JSON）
  - decide：交互式决策点（start_morning / hpo_deploy / execute_actions）
- Memory（ClickHouse + 本地）
  - episodes：learn/intraday 每小时写入（含 symbol/sector/regime）
  - hedging_events.jsonl：对冲事件记录
- Evolvers & Reflection
  - reflection/analyzer.py：facts+market → diagnosis/prescription
  - evolvers/decision_engine.py：promotion_policy + dxp → pending_actions.yml
  - reflection/report_builder.py：daily_report.md
- AB（deploy/ab_deployer.py）
  - Champion/Challenger 权重建议；可被大脑覆盖
- MLflow（mlflow_utils.py）
  - learn/pending_exec/RL 训练/评估：统一打点指标、参数与工件

## 数据流与工件
- runs/ai_trader/YYYY-MM-DD/
  - brain_plan_*.json / morning_decision*.json / hpo_decision.json / rl_decision.json
  - reflection.json / reflection_dxp.json / pending_actions.yml / decision_log.yaml
  - context.json / daily_report.md / rl_eval.json / hedging_events.jsonl
  - portfolio_weights.json / portfolio_apply.json / overlay_hedge.json / overlay_events.jsonl

## 内环（RL）
- 训练与评估：rl_core/train.py / scripts/ai_trader/rl/eval_rl.py
- 门槛配置：policies.rl_deploy（min_sharpe/max_mdd）
- 启停逻辑：评估 gate.ok + 大脑批准 → 写回 config.yml 的 extras.rl.enabled
- 策略内安全门：max_position_pct/max_step_pct 等

## 外环（HPO/反思）
- HPO：alpha_loop.py + wf_hpo.py（Walk-Forward/OOS 指标）
- 上线门槛：policies.hpo_deploy（Sharpe/MDD/最小样本量）
- 大脑批准：decide('hpo_deploy')；AB 权重可被覆盖
- 反思：consistency_job.py → analyzer + LLM → dxp（处方）
- Pending：处方不直接生效，写入 pending_actions.yml，晨间执行（带大脑批准），并记录 MLflow 运行 pending_exec_{date}

## 计划与命令映射
- Control Plan JSON：`doc/AI_TRADER_PLAN_SCHEMA.json`
- run_with_plan：providers/interval/cycles/wait_for/symbols → llm_trader/run_single_bot.py 参数
- 回退策略：若计划缺省，使用记忆（top_providers/top_symbols）自动补齐

## 对冲（战术）
- Enhanced 策略内置 Beta Hedging：低置信度时用 hedge_instrument 做反向对冲
- 支持 per_symbol/per_sector 覆盖（不同 beta/阈值/对冲工具）
- 事件落盘到 hedging_events.jsonl，并在 learn 阶段做聚合打点；日报汇总包含 per-symbol / per-sector 分布

## 安全与回滚
- 全部关键动作均 best-effort；失败不阻断主流程
- 可选 fail_fast：`policies.fail_fast: true` 时，盘后 learn 对关键步骤（反思→处方、参数写回、日报链）失败将立即中止并返回错误，用于 Paper 阶段快速暴露问题
- 交互式决策点：大脑可否决；所有决定落盘工件
- 参数写回与 pending 执行均写日志与 MLflow
- Paper-only 默认；Live 前需关闭 paper 并检查执行门

## 盘中风控（Guardian）
- 策略内置 Guardian：
  - 指标：日内最大回撤（相对当日高水位）、1 分钟交易频率、总风险敞口（名义/资本）
  - 动作：reduce（降规模）、halt（熔断新开仓）、flat（强平+冷却）
  - 事件：写入 `guardian_events.jsonl`，日报与 MLflow 汇总（事件数/最大日内 DD）
- Orchestrator 联动：intraday 周期性处理最新 Guardian 事件，调用大脑 approve 后立即落参（支持 `increase_conf_floor`、`reduce_sizing_multiplier`、`disable_rl`）

## 扩展点
- RL 组合动作：将动作空间从 (1,) 扩展为 (主仓位, 对冲仓位)
- Hedging Meta Strategy：独立的对冲策略订阅组合事件
- MLflow lineage：为 learn/pending_exec/RL 训练/评估统一打 tag（market_regime、dataset_version）

## 附：配置摘录
- ai_trader.yaml：modes / policies.hpo_deploy / policies.rl_deploy / mlflow
- config.yml（策略通用）：`strategies[*].params.extras` 下的 `rl` / `hedging` / `per_symbol` / `per_sector`
- 学习链路扩展：`learning_profile` + adapter 注册机制（见 `LEARNING_PIPELINE_PLUGINS.md`）
## 组合与对冲（Portfolio & Overlay）
- Portfolio Optimizer：基于近期 Sharpe 排名为 symbols 生成尺度（scales），落盘 portfolio_weights.json，并写回 `per_symbol.<sym>.sizing.multiplier`
- Overlay（plan-only）：估算组合 Beta，与目标/容忍区间比较，生成对冲建议（默认 SPY），落盘 overlay_hedge.json；日报与 MLflow 聚合
