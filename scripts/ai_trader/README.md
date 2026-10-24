AI Trader 控制平面（升级版）

概览
- 统一编排两条主线：run_system（HPO/回测/参数）与 llm_trader（盘前/盘中/盘后交易）
- 四道门：健康/数据/执行/LLM；通过后启动对应会话
- 盘后学习：反思→处方→HPO→参数回写→AB；同时训练/评估 RL 并按门槛自动启停
- 认知与记忆：市场语境（Regime/VIX）+ 历史记忆（ClickHouse）驱动大脑计划
- 可观测：MLflow 打点关键指标与工件；每日生成 Markdown 报告与决策/处方归档
- 盘中保护：策略内置 Guardian（熔断/降风险/强平），并产出 guardian_events.jsonl，日报与 MLflow 汇总观察
 - 组合层：盘后 Portfolio Optimizer 计算 per_symbol 尺度（scales），并落地到 config；Overlay 产出 Beta 对冲计划（plan-only）

快速开始
1) 三道门 + 盘前：
   - `PYTHONPATH=$PWD venv/bin/python scripts/ai_trader/cli.py morning`
2) 盘中：
   - `PYTHONPATH=$PWD venv/bin/python scripts/ai_trader/cli.py intraday --mode fast`（第一小时）或 `--mode full`
3) 盘后学习：
   - `PYTHONPATH=$PWD venv/bin/python scripts/ai_trader/cli.py learn`
4) 自运行（可选）：
   - `venv/bin/python scripts/ai_trader/auto_loop.py`（建议交由 launchd/pm2 守护；务必使用仓库自带 venv 以避免系统 Python 的 NumPy/Pandas ABI 冲突）

5) 看门狗（监控 alpha_loop 是否卡住/内存飙升）
   - 监控 PID + 日志增长：
     ```bash
     PYTHONPATH=$PWD venv/bin/python -m scripts.ai_trader.tools.ml_watchdog \
       --match "alpha_loop.py --config alpha_config_enhanced.json" \
       --log-file alpha_out/daily/run.log \
       --stuck-mins 6 --rss-limit-mb 10000
     ```
   - 监控并自动重启（提供安全的重启命令）：
     ```bash
     PYTHONPATH=$PWD venv/bin/python -m scripts.ai_trader.tools.ml_watchdog \
       --match "alpha_loop.py --config alpha_config_enhanced.json" \
       --log-file alpha_out/daily/run.log \
       --stuck-mins 6 --rss-limit-mb 10000 \
       --restart-cmd "PYTHONUNBUFFERED=1 DISABLE_EVENT_BUS=1 PYTHONPATH=$PWD venv/bin/python -X faulthandler alpha_loop.py --config alpha_config_enhanced.json --gens 1 --provider optuna --out_dir alpha_out/daily"
     ```
   - 说明：
     - 无 psutil 时自动用 `ps` 解析 RSS
     - 违例时先发 `SIGTERM`（触发 CRASH-HOOK 与 faulthandler 打印堆栈），若仍未退出且提供了 `--restart-cmd` 则 `KILL` 后重启
     - 默认每 60s 检查一次，日志 6 分钟无增长视为卡住

输出位置
- 计划与记录/报告：`runs/ai_trader/YYYY-MM-DD/*.json|.yaml|.md`
- 日志：使用系统已有 `logs/`
- 记忆（Memory）：ClickHouse 表 `ai_trader_memory_episodes`；`hedging_events.jsonl`（对冲事件）
- 日报（daily_report.md）：包含 Hedging 汇总（事件数/名义金额）与 per-symbol / per-sector 分布
- Guardian：`guardian_events.jsonl`（熔断/降风险/强平事件；日报会汇总事件数/最大日内 DD）
- Portfolio：`portfolio_weights.json`（权重/尺度）/`portfolio_apply.json`（写回参数）/`overlay_hedge.json`（对冲计划）
- Brain Trace：`brain_calls.jsonl`（大脑每次 LLM 调用的输入摘要与输出结果，便于审计）

RL（内环）
- 训练脚本：`python rl_core/train.py --symbols TSLA,AAPL,NVDA --start <...> --end <...> --features-table trading.features_1m --timesteps 200000 --out models/rl/sac_position_manager_latest`
- 依赖：`pip install gymnasium stable-baselines3[extra] clickhouse-driver pandas numpy`
- 策略启用：在 `config.yml` 的 `strategies[*].params.extras` 下设置 `rl.enabled: true`（按策略生效，Paper-only 建议）。

MLflow（可选）
- 配置：`scripts/ai_trader/config/ai_trader.yaml` 的 `mlflow:`（enabled、tracking_uri、experiment）
- learn 阶段会将 HPO 指标、最佳参数摘要和主要工件（gen_summary、hpo_decision、context、reflection_dxp、pending_actions、daily_report、rl_eval）打点到 MLflow。
- RL 训练/评估脚本也会分别打点：最优 reward、模型文件与对比指标。
- 默认存储：`file:./mlruns`（本地目录），可通过 `MLFLOW_TRACKING_URI`/`MLFLOW_EXPERIMENT` 环境变量覆盖。

Paper 模式
- 在 `scripts/ai_trader/config/ai_trader.yaml` 里启用：`modes.paper: true`
- 启用后：
  - 执行门（IBKR）跳过检查，仅跑 Paper；
  - `llm_trader` 走 fallback Python 命令，并在 `modes.llm_paper_flag` 存在时自动追加该标志（默认 `--dry-run`）；
  - 不进行 shadow/canary 等灰度流程，直接以模拟交易运行。

交互式决策点（大脑控制）
- 早盘：
  - 执行 pending actions（带大脑批准），写回 config.yml；打点 MLflow（pending_exec_{date}）
  - 门控有异常时，大脑决定是否“带病运行”，结果落盘 `morning_decision_*.json`
- 夜间：
  - HPO 上线需同时满足门槛与大脑批准；AB 权重可由大脑覆盖，结果落盘 `hpo_decision.json`
  - RL：训练+评估；按 `policies.rl_deploy` 门槛 + 大脑批准自动启停；落盘 `rl_decision.json`
  - 若 `commands.alpha_loop` 为空，learn 会跳过 HPO 训练环节，仅保留其余学习流程（反思/处方/报告/治理等）。

文档
- 使用指南：`scripts/ai_trader/HOW_TO_USE.md`
- 系统设计：`scripts/ai_trader/SYSTEM_DESIGN.md`
- 风控：Guardian（策略级熔断/降风险/强平），详见 HOW_TO_USE 配置示例

配置归属与放置位置（重要，避免错配）
- 根配置：config.yml（由 libs.config 读取）
  - 谁读取：auto_loop、事件总线、writers/consumers、策略与执行层等运行时组件
  - 放哪些键：
    - `event_bus.topics`（含 `ml: internal.ml.status`）
    - `policies.loop_sleep_sec`（各时段主循环休眠时间：weekend/morning/intraday/afterhours/overnight）
    - `policies.hpo_deploy`（门槛：Sharpe/MDD/Trades/Coverage/PF/期望/收益）
    - `autosettings`（参数回写开关/审批）
    - `kafka`/`clickhouse`/`ib`/`universe` 等基础运行参数
  - 示例：
    ```yaml
    event_bus:
      topics:
        ml: internal.ml.status
    policies:
      loop_sleep_sec:
        weekend: 600
        morning: 600
        intraday: 300
        afterhours: 900
        overnight: 900
      hpo_deploy:
        min_oos_sharpe: 1.40
        max_oos_mdd: 0.10
        min_oos_trades: 80
        min_coverage: 0.85
        min_pf: 1.20
        min_expectancy_usd: 0
        min_return: 0
      autosettings:
        enabled: true
        require_approval: false  # Paper
    ```

- 控制平面配置：scripts/ai_trader/config/ai_trader.yaml（由 Orchestrator 读取）
  - 谁读取：Orchestrator（控制流：计划、学习、风控、提示词策略）
  - 放哪些键：
    - `policies.propose_plan`（缓存 + 触发器 + 静默时段）
    - `policies.premarket.scan_slots`（盘前扫描时隙，默认：[07:00, 08:00, 08:30, 09:00, 09:27]，仅这些时间点附近触发盘前重计划与下单）
    - `policies.risk_pretrade`（per-symbol 缓存 TTL、全局阈值）
    - `policies.learning`（盘后最早触发时间/TTL、心跳参数）
    - `policies.tools.llm_trader`（并发上限/心跳等）
  - 示例：
    ```yaml
    policies:
      propose_plan:
        use_triggers: true
        min_interval_sec:
          morning: 900
          intraday: 1200
          afterhours: 1800
          overnight: 3600
        quiet_hours_et:
          start: "02:00:00"
          end:   "06:00:00"
      risk_pretrade:
        per_symbol_ttl_sec: 1800
        thresholds:
          max_spread_bps: 50.0
          min_adv_usd: 10000000
          var_kupiec_p_min: 0.05
          var_christoffersen_p_min: 0.05
      learning:
        earliest_afterhours_start_et: '16:20:00'
        min_interval_sec: 3600
    ```

常见陷阱与建议
- 休眠频率（loop_sleep_sec）必须配置在根 `config.yml` 下的 `policies`，auto_loop 才能生效；放在 `ai_trader.yaml` 不会被读取。
- 计划频率/风控频率不靠调大 sleep，而靠缓存/触发器/静默时段：
  - `propose_plan` 采用 TTL + 触发器 + 静默时段（02:00–06:00）；事件（OOF/HPO/RL 完成/失败）会写缓存失效文件，促使下一轮刷新。
  - `risk_pretrade` 采用 per‑symbol 缓存与全局签名；仅对“新增/签名变化”的符号评审；命中缓存不调大脑。
- 事件驱动：确保 `event_bus.topics.ml` 在根 `config.yml` 中配置为 `internal.ml.status`，并已启动控制事件消费者（auto_loop 会拉起）。
- 一定要使用仓库自带的 `venv` 运行控制面（如：`venv/bin/python scripts/ai_trader/auto_loop.py`）。系统/Anaconda Python 可能与已编译的第三方包（如 Pandas/PyArrow）在 NumPy 版本上不兼容，会导致导入期崩溃（常见报错："A module that was compiled using NumPy 1.x cannot be run in NumPy 2.x"）。

对齐说明
- 健康门：读取 `logs/system_health.json`（由 `docs/daily/system_health_check.py` 生成）
- 数据门：复用 ClickHouse 表：`lake.ticks.ts_recv`、`trading.bars_1m.ts_end`
- 执行门：`python temp/check_ibkr_status.py` 成功视为通过
- LLM 门（可选）：`scripts/ai_trader/gates/llm_gate.py` 直接引用 `llm_trader/llm_trader/providers/*` 并校验所需 API Key；默认不做网络连通测试，避免额外调用成本，可按需开启
- llm_trader：直接调用 `llm_trader/*.sh` 现有脚本或内置 flags 作为后备
- run_system：调用仓库已有 `alpha_loop.py`、`apply_hpo_params.py`、`docs/daily/trigger_backfill.py`
- 模块化学习链路：见 `scripts/ai_trader/LEARNING_PIPELINE_PLUGINS.md`（adapter 接口、profile 注册、多策略接入）。

注意
- 所有外部命令默认从仓库根目录执行；如用 launchd，请确保 venv 与 .env 已在 wrapper 中激活（我们的“一键自动化”脚本已处理）。
- 可选 Fail‑Fast：在 `scripts/ai_trader/config/ai_trader.yaml` 的 `policies.fail_fast: true` 时，learn 流程在关键环节失败（反思链/参数写回/报告链）会立即中止并返回 `status:error`，避免“带病继续”。
