# AI Trader 使用指南（Paper 优先）

本指南覆盖准备环境、配置关键项、运行日常流程、查看输出与常见开关。

## 环境准备
- Python 3.10+，已安装本仓库依赖（含 ClickHouse 驱动）
- .env 配置好 ClickHouse/Postgres/LLM Provider 的连接参数（参考 `.env.example`）
- 可选（RL）：`pip install gymnasium stable-baselines3[extra]`（未安装会自动降级）

## 关键配置（scripts/ai_trader/config/ai_trader.yaml）
- `modes.paper: true`：启用 Paper；`modes.llm_paper_flag: --dry-run`（或改为 `--paper`）
- `policies.hpo_deploy`：HPO 上线门槛（oos_sharpe/oos_mdd/最小样本量）
- `policies.rl_deploy`：RL 启停门槛（min_sharpe/max_mdd）
- `mlflow`：enabled/tracking_uri/experiment
- `brain.enabled: true`：启用 AI 大脑控制面（Director + Alpha/Model/Execution 专家）
- `brain.autopatch_*`：控制 AI 是否自动落地调参结果：
  - `autopatch_model_space`：自动 patch HPO 搜索空间 (`alpha_config_enhanced.json.hpo.grid.*`)
  - `autopatch_features`：自动更新特征 DSL (`feature_programs.json`) 与特征相关 HPO 空间
  - `autopatch_execution`：自动收窄持仓时间搜索空间 (`risk.time_stop_min` HPO grid)
  - `autopatch_alpha`：自动切换 `alpha_config_enhanced.json` 顶层 `label_profile`

## 策略配置（config.yml → strategies[*]）
- 学习与运行都以 `config.yml` 的 `strategies` 列表为准，不再假设单一策略。
- 策略通用关键信息在 `params.extras`：
  - RL：`rl.enabled|model_path|action_mode|safety.*`
  - 对冲（可选）：`hedging.enabled|confidence_threshold|hedge_instrument|beta|per_sector|per_symbol`
  - Guardian（可选，盘中熔断/降风险/强平）：
    ```yaml
    guardian:
      enabled: true
      dd_intraday_pct: 0.03           # 日内最大回撤阈值（相对当日高水位）
      max_trades_per_min: 8           # 每分钟最大交易次数
      max_gross_exposure_pct: 0.6     # 最大总风险敞口（名义/资本）
      on_trip: halt                   # reduce|halt|flat（三选一：降规模/熔断/强平）
      reduce_scale: 0.5               # reduce 时规模缩放系数
      cooldown_sec: 1800              # 熔断/强平后的冷却时间（秒）
    ```
    - 触发后行为：
      - reduce：即时下调下单规模（不改配置），持续生效直至进程重启或手工恢复
      - halt：阻止新开仓，允许风控/止损平仓；冷却期后自动解除
      - flat：强平所有持仓并进入冷却期
  - per_symbol/per_sector 覆盖：`sizing|market_filter|risk|order|hedging` 均可按 sector/symbol 定制
- 学习目标建议通过 `learning_profile`/`learning_targets` 管理（详见 `LEARNING_PIPELINE_PLUGINS.md`）。

## 常用命令
- 盘前：`PYTHONPATH=$PWD venv/bin/python scripts/ai_trader/cli.py morning`
  - 门控→执行 pending（带大脑批准）→大脑计划→运行 llm_trader
- 盘中：`PYTHONPATH=$PWD venv/bin/python scripts/ai_trader/cli.py intraday --mode fast|full`
  - 大脑计划→运行 llm_trader→每小时写记忆
- 盘后学习：`PYTHONPATH=$PWD venv/bin/python scripts/ai_trader/cli.py learn`
  - 反思→处方→HPO→参数回写（门槛+大脑批准）→AB→写记忆→RL 训练与评估（门槛+大脑批准自动启停）→生成 context/dxp/pending/report→MLflow 打点
- 自动轮转：`PYTHONPATH=$PWD venv/bin/python scripts/ai_trader/auto_loop.py`
- 注意：若 `commands.alpha_loop` 为空，learn 会跳过 HPO 训练，仅执行其余学习步骤。

### （可选）工作流模式（Prefect）
- 开关：`scripts/ai_trader/config/ai_trader.yaml` → `policies.use_workflow: true`
- 安装与本地启动（可视化/调度/重跑）：
  ```bash
  pip install prefect
  prefect server start &
  prefect agent start -q 'default' &
  ```
- 运行：`PYTHONPATH=$PWD venv/bin/python scripts/ai_trader/cli.py learn`
  - 当 `use_workflow=true` 且 `prefect` 可用时，将运行 Prefect 版本的 evening_learning Flow（其余命令不变）。
  - 出错或未安装 Prefect 时，会自动回退到内置 Orchestrator 路径。

## 输出目录（runs/ai_trader/YYYY-MM-DD）
- `brain_plan_*.json`：大脑计划（按 session 划分）
- `ml_review.json` / `ml_actions.jsonl`：盘后 ML 评审结果与实际执行的 ML 动作（含 stage/branch_id/delta_ic/delta_auc/delta_coverage）
- `ml_branches_summary.json`：最近 N 日 ML 动作的按阶段聚合结果（供 Director 决策用）
- `ml_research_plan.json`：Director 本轮研究计划（focus=alpha/model/execution + success criteria）
- `ml_model_space_suggestion.json` / `ml_alpha_suggestion.json` / `ml_execution_suggestion.json`：
  - AI 专家给出的模型空间 / 标签 / 执行参数建议，以及 Critic 校验结果与 `autopatch` 实际应用情况
- `feature_engineering_suggestion.json`：特征工程建议（含自动应用情况 `_autopatch`）
- `morning_decision*.json`/`hpo_decision.json`/`rl_decision.json`：交互式决策点
- `reflection.json`/`reflection_dxp.json`：一致性与诊断/处方
- `context.json`：市场语境（Regime/VIX/趋势）
- `pending_actions.yml` → 晨间执行；`decision_log.yaml`：执行记录
- `daily_report.md`：当天小结
- `rl_eval.json`：RL 评估结果
- `hedging_events.jsonl`：对冲事件
- `guardian_events.jsonl`：Guardian 熔断/降风险/强平事件
- `portfolio_weights.json` / `portfolio_apply.json`：组合优化权重与写回参数
- `overlay_hedge.json` / `overlay_events.jsonl`：组合级对冲计划（plan-only）

## Grafana 仪表盘（可选）
- `grafana/dashboards/active/ai_brain_intraday_pnl.json`：专注 AI 调参后的 **日内 PnL / trades/day / intraday drawdown** 监控

## RL（Paper 优先）
- 训练（可选）：
  ```bash
  PYTHONPATH=$PWD venv/bin/python rl_core/train.py --symbols TSLA,AAPL,NVDA \
    --start 2025-07-01 09:30:00 --end 2025-08-31 16:00:00 \
    --features-table trading.features_1m --timesteps 200000 \
    --out models/rl/sac_position_manager_latest
  ```
- 策略启用 RL：`config.yml` → `extras.rl.enabled: true`（默认已开启）
- 学习阶段会自动训练/评估与启停，无需手动介入

## 对冲（可选）
- 开启：`config.yml` → `extras.hedging.enabled: true`
- 推荐先 Paper 验证后再放开；可按 sector/symbol 自定义 beta/阈值/对冲工具

## MLflow（可选）
- 配置：`scripts/ai_trader/config/ai_trader.yaml` → `mlflow`
- 启动 UI：`mlflow ui --backend-store-uri file:./mlruns`
- 查看 run：
  - learn_{date}：HPO/RL/处方/对冲统计与工件
  - pending_exec_{date}：晨间 pending 实际执行与工件

## 常见问题
- LLM/Gym 未安装或网络不可用？→ 所有环节 best-effort，不阻断；日志里会标注跳过
- ClickHouse/数据门失败？→ 晨间先调用回补脚本重试；仍失败由大脑裁决是否“带病运行”
- 参数未回写？→ 查看 `hpo_deploy_check` 与 `hpo_decision`，是否门槛/批准失败
- RL 未启用？→ 查看 `rl_eval.json` 与 `rl_decision.json`，确认门槛与批准结果
- Guardian 总是触发？→ 检查 `config.yml` 中的阈值是否过紧；日报的 Guardian 小节会展示最大 DD 与最近一次触发原因
- 是否需要工作流 UI？→ 建议在开发/调试阶段开启 Prefect UI；生产也可使用它进行告警与重跑。

更多细节与扩展点，参见 `SYSTEM_DESIGN.md`。
