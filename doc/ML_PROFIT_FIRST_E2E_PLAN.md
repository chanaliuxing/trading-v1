# 利润优先 · 端到端 ML 升级落地计划（Data → Features → ML/HPO/RL → Backtest → Deploy → Trade）

本文档给出“利润优先、端到端闭环”的彻底升级方案，专门解决数据/特征质量（GIGO）、训练与实盘对齐、MLOps 闭环、AI 大脑自助能力与赚钱目标对齐。内容可直接交付给工程同学与 Claude Code 执行。

---

## 总目标
- 数据优先：统一数据契约与特征定义，杜绝错位与泄漏，端到端一致（训练=实盘）。
- 利润优先：上线门控以赚钱为目标（Sharpe、PF、期望、风险），拒绝“为跑通而跑通”。
- 大脑可自助：提供“获取数据/构建特征/发起 HPO/评估/落地”的工具与接口，逐步接入另类数据。
- 无缝落地：从 Data 到 Trade 的“可观测+可回滚+可冷却+可审计”的闭环生产线。

---

## 一、数据与特征层（Data → Features）

### 1) 统一数据契约（ClickHouse + 实时）
- 事实表对齐：bars_1m_ibkr、bars_5s、ticks（统一 ts、symbol、时区、闭合窗口）。
- Schema 约束：每次写入/更新产生 schema 审计与 DQ（缺口/重复/非单调 ts）。
- 行为准则：训练/回测/实盘都只能消费“规范表/视图”，防泄漏。

### 2) Feature Catalog + 应用流水线
- Catalog：config/features_catalog.json（表达式、窗口、依赖、校验）。
- 作业：feature_build → feature_eval（AUC/IC/coverage + 泄漏/冗余）→ feature_apply（promotion 到 active）。
- 活动视图：trading.features_1m_active_v{N}，训练/实盘统一引用 active。

### 3) 实时特征引擎（RFE：Realtime Feature Engine）
- 在线：libs/strategy/realtime_feature_engine（atr/ret/ema/vwap/rvol 等，emit 1s 帧）。
- 决策对齐守卫：on_features 强制 price.ts 与 features.ts 漂移 ≤ 200ms，超限跳过并审计。
- 离线等价：backtest_ml_engine 生成 RFE‑equiv 特征（按 Catalog），保证训练/实盘一致。

【配置开关（已落地）】
- 策略侧（启用 RFE 与可选落盘）：
  - 路径：config.yml → strategies.<id>.params.extras.realtime_feature_engine
  - 关键项：
    - enabled: true            # 开启 RFE 模式（策略改为 emit 驱动）
    - emit_on: "1m"            # 仅 1m 时支持落盘 sink（训练用）
    - features: []             # 可选，自定义指标（未提供则使用默认 EMA/RSI/ATR/VWAP）
    - sink.enabled: true       # 可选，开启 RFE→CH 落盘
    - sink.table: trading.features_1m_active  # 目标表

  示例：
  ```yaml
  strategies:
  - id: enhanced_ml_001
    params:
      extras:
        realtime_feature_engine:
          enabled: true
          emit_on: "1m"
          sink:
            enabled: true
            table: trading.features_1m_active
  ```

- 训练侧（夜间 OOF 使用 active 表）：
  - 路径：config.yml → policies.features.use_rfe_active_table
  - 作用：为 nightly OOF/compare 覆盖特征表，优先使用 trading.features_1m_active（与线上一致）。
  - 示例：
  ```yaml
  policies:
    features:
      use_rfe_active_table: true
  ```

- 引擎与服务（自动化时段管理）：
  - RTH 自动禁用旧轮询 FeatureCalculator（15s/5s），保留 1h；若检测到“存在未启用 RFE 的策略”，RTH 下自动保留 1m 作为兜底。
  - Off‑hours 自动启用 15s/5s/1m 等计算器，保障夜间训练/回测供数。
  - 策略启用 RFE 时，引擎自动跳过轮询订阅，避免重复与错配（无需额外配置）。

### 4) 数据获取工具（Brain 可自助）
- 新动作：request_backfill、request_feature_build/eval/apply、request_extend_window、diagnose_leakage、analyze_drift。
- 冷却锁 + 幂等锁：避免抖动与重复。

### Claude Code TODO（数据/特征）
- 实现/完善 RFE（emit 1s 帧、滑窗计算、warmup gating、ctx.get_realtime_features）。
- backtest_ml_engine：加入 RFE‑equiv 生成（离线与在线一致）。
- 扩充 feature_eval：泄漏检测、冗余检测（Spearman/互信息）、覆盖率报告。
- control_event_consumer：新增对 Brain 的特征类 next_actions 执行器（含 TTL、审计）。

---

## 二、机器学习层（Features → ML/HPO/RL）

### 1) HPO 入口一体化
- hpo_cli_upgraded：保留 TPE/BoTorch、ASHA、多目标（NSGA-II）/公式标量化支持。
- wf_hpo.evaluate_trial：返回 L1（IC/IR/coverage）+ L3（Sharpe/PF/Trades/Drawdown）指标。

### 2) 上线门控（利润优先）
- OOF 门控：Sharpe ≥ X、PF ≥ Y、Trades ≥ N、Coverage ≥ Z、MDD ≤ U。
- HPO 门控：min_trials、min_unique_params、convergence_score_max；可选 L1 门（min_ic/min_coverage）。
- 失败治理：fail_streak ≥ 阈值 → extend_window/horizon/label_bps/feature_build（自动化）。

### 3) 诊断闭环（自助建议）
- report_ic_to_sharpe：输出 L1→L3 因果归因报告；Brain 指挥下一步（模型退化 vs 策略参数退化）。
- ml_diagnostics：基于 IC/Sharpe 收敛与盈利状况选择 feature_build/hpo_cli/alpha_loop 优先级与冷却 TTL。

### Claude Code TODO（ML/HPO/RL）
- wf_hpo：确保 evaluate_trial 输出 ic/ir/coverage 并在 hpo_cli 打印/聚合。
- hpo_cli_upgraded：完善 --objective-formula 与 --multi-objective（NSGA-II）、pareto.json 导出。
- control_event_consumer：alpha_loop/hpo_cli 成功分支读取 L1 门（如配置），通过则 apply_extras_params，审计元数据（pareto_size/selection_rule）。
- tools/report_ic_to_sharpe.py：生成长期趋势与建议 JSON/MD。

---

## 三、AI 大脑的“赚钱工具箱”（Brain → Tools）

### 1) 数据/特征/训练工具
- list_features、request_feature_build/eval/apply、request_backfill、request_extend_window、diagnose_leakage、analyze_drift。
- 训练/优化：queue_job(oof/hpo_cli/alpha_loop/ensemble_sweep/feature_*），自动发 Kafka 事件，consumer 门控与应用。
- 成功/失败：ml_events.jsonl + ml_success.jsonl 审计归档。

### 2) 利润优先策略（禁止“为跑通而跑通”）
- 禁止自动下调信心阈值绕开门控：未达标进入 manage_only 或 pause，不放宽硬门。
- 自动化止损：连续 N 次失败 → 冷却/回退（恢复上一个稳定版本/权重）。

### Claude Code TODO（Brain/Tools）
- control_event_consumer：扩展 next_actions 执行器（数据/特征/训练全覆盖），统一审计与冷却锁。
- docs：Brain 接口（动作清单与 payload 样例）、门控政策（利润优先）。

---

## 四、交易落地（Trade）

### 1) 策略落地
- EnhancedModelSignalStrategy：RFE 优先、对齐守卫、warmup gating、可配置 fallback。
- 校准消费：读取 runs/.../oof_eval/calibration_X.json，保证概率校准一致。
- 尺度与风控：置信度软阈值 = sizing；硬阈值 = 禁止交易；Guardian 与 kill‑switch 延续。

### 2) 条件上线（Paper → Live）
- 在 config.yml 配置部署门槛 + 滚动窗口要求（最近 X 天 pass 率、日 Sharpe）。
- 自动化回滚路径：成功冷却锁、失败阈值触发回退。

### Claude Code TODO（Trade）
- EnhancedModelSignalStrategy：RFE 接入 + 对齐守卫。
- 策略配置：软/硬阈值与 calibration 文件消费。
- Orchestrator：门控不足 → manage_only；成功 → 渐进放大规模。

---

## 五、观测与回滚（Observability & Rollback）

### 审计产物
- ml_events.jsonl、ml_success.jsonl、ml_actions.jsonl、oof_eval/compare.json、pareto.json、report_ic_to_sharpe.json/MD、rfe_consistency_report.json。

### 监控指标
- 成功率、部署次数、冷却命中、回滚次数、收益曲线与超额收益。

### 回滚策略
- 一键回滚上一个稳定的 ensemble_policies.json/config.yml。
- 冷却 TTL（6h–24h）防重复。

---

## 六、阶段化实施与 DoD（Definition of Done）

### Phase 0（2–3 天）
- RFE 接入 + 对齐守卫；训练/回测 RFE‑equiv；OOF 一体化（compare.json + status.json + 幂等）。
- DoD：策略以 RFE 决策（漂移≤200ms），compare.json 标准化，门控与部署闭环跑通。

### Phase 1（3–5 天）
- hpo_cli 多目标/公式标量化；HPO 与 OOF 门控（含 L1 可选）；ml_diagnostics/IC→Sharpe 报告。
- DoD：多目标 HPO 产物 pareto.json；门控通过自动落参，审计完整。

### Phase 2（3–5 天）
- Brain 工具箱（数据/特征/训练动作）、数据连接器插件架构、特征评估/应用全面化。
- DoD：Brain 能自助请求数据/特征；特征通过成功门控进入 active，策略使用 active，无缝落地。

### Phase 3（2–4 天）
- 停用轮询 Calculator（生产），RFE 成为唯一在线特征源；加“链式触发”（OOF→hpo_cli→alpha_loop）与冷却锁。
- DoD：端到端一致，利润优先上线策略（Sharpe/PF/期望满足），回滚与冷却生效。

---

## 立即执行的最小清单（Kickoff）
1) 实现 RFE 与对齐守卫；EnhancedModelSignalStrategy 切换到 RFE 优先。
2) backtest_ml_engine 增加 RFE‑equiv；完成 OOF compare.json 标准化与 status.json 幂等。
3) hpo_cli_upgraded 支持 --objective-formula 与 --multi-objective（NSGA-II）并导出 pareto.json。
4) control_event_consumer 接入 HPO L1 可选门（min_ic/min_coverage），通过自动落参 → invalidate。
5) 写入 doc/ml/mlupgrade_final_landing.md 与本文，作为开发与运维执行指南。

---

# Phase-by-Phase 实施计划（可执行）

## Phase 0 · 数据对齐与 OOF 一体化（2–3 天）
### 目标
- 消除数据错位（Price vs Features 同步），训练=实盘特征一致。
- 统一 OOF 产物，标准化 compare.json + status.json，并具备幂等。

### 交付物
- Realtime Feature Engine（RFE）在线 1s 帧：atr/ret/ema/vwap/rvol。
- RFE‑equiv 离线特征（回测/训练用，定义与在线一致）。
- 标准化 OOF 产物：`oof_eval/compare.json`、`oof_flow_status.json`（幂等）。

### 任务清单（代码路径）
- RFE 接入（在线）
  - [ ] libs/strategy/realtime_feature_engine/realtime_feature_engine.py：滑窗、emit_interval、warmup gating
  - [ ] libs/strategy/base.py / libs/strategy/__init__.py：ctx.enable_realtime_feature_engine() / ctx.get_realtime_features()
  - [ ] strategies/enhanced_model_signal_strategy.py：RFE 优先、对齐守卫（price.ts 与 features.ts 漂移 ≤ 200ms 超限 skip + 审计）
- RFE‑equiv（离线）
  - [ ] backtest_ml_engine.py：按 Feature Catalog 生成同构特征（与 RFE 定义一致）
- OOF 一体化/幂等
  - [ ] tools/nightly_oof_flow.py：输出 `runs/<date>/oof_eval/compare.json` 与 `runs/<date>/oof_flow_status.json`；DROP IF EXISTS 防重复
  - [ ] scripts/ai_trader/ml_scheduler.py:_execute_oof：artifacts 指向 compare.json；metrics.bt=compare['fusion']; ok 由 status.json + returncode 决定

### 验收标准（DoD）
- 策略基于 RFE 决策（features.ts 与 price.ts 漂移 ≤ 200ms）；超限有审计。
- compare.json 结构齐全；oof_flow_status.json 必存在；重复执行幂等。

### 风险与回滚
- RFE 未暖机/心跳中断：`manage_only`；临时回退 polling 仅限开发态，生产默认禁用混用。

### 指标/监控
- RFE 心跳 TTL、漂移超限次数、compare.json 完整率、幂等重跑通过率。

## Phase 1 · HPO 多目标与门控闭环（3–5 天）
### 目标
- 将 HPO 结果与 L1/L3 同时纳入；门控以利润优先；自动落参闭环稳定。

### 交付物
- hpo_cli_upgraded.py 支持 `--objective-formula` 与 `--multi-objective`（NSGA-II）。
- 产物：pareto.json、best_params.json（多目标下导出前沿集与选择结果）。
- HPO 成功门控（含 L1 可选）：min_trials/min_unique_params/convergence_score_max/[min_ic,min_coverage]。

### 任务清单
- [ ] wf_hpo.py:evaluate_trial：确保输出 ic/ir/coverage（训练/评估）
- [ ] hpo_cli_upgraded.py：
  - 解析 `--objective-formula`（AST 安全子集）→ 单目标标量化
  - 解析 `--multi-objective`（JSON）→ NSGA-II；导出 pareto.json + best_params.json（词典序/权重规则）
- [ ] scripts/ai_trader/control_event_consumer.py：
  - alpha_loop/hpo_cli 统一门控；读取 L1 门（若配置且 metrics 提供）
  - 通过则执行 `apply_extras_params.py`；审计 metadata（pareto_size/selection_rule）
- [ ] 文档：doc/ml/mlupgrade_final_landing.md / 本文更新多目标使用说明与样例

### 验收标准（DoD）
- 公式标量化/多目标两种 HPO 路径均可跑通；pareto.json 导出；门控通过→自动落参→invalidate；ml_success.jsonl 审计完整。

### 风险与回滚
- 学习不收敛：门控失败=不落参；冷却 TTL；回滚到上一稳定版本。

### 指标/监控
- trials 完成率、unique_params、convergence_score、IC/coverage 统计；门控通过率；apply 成功率。

## Phase 2 · Brain 工具箱与特征治理（3–5 天）
### 目标
- Brain 具备“自助赚钱工具”：能请求高质量数据/特征/训练并自动执行/审计/冷却。

### 交付物
- 动作：request_backfill、request_feature_build/eval/apply、request_extend_window、run_hpo_cli/alpha_loop/oof、diagnose_leakage/analyze_drift。
- 特征评估/应用全闭环（feature_* 三作业 + success_gates），IC→Sharpe 报告。

### 任务清单
- [ ] scripts/ai_trader/control_event_consumer.py：扩展 next_actions 执行器覆盖数据/特征/训练动作；TTL + 幂等锁 + 审计（ml_actions.jsonl）
- [ ] tools/report_ic_to_sharpe.py：生成历史区间相关性报告（JSON/MD）
- [ ] 数据连接器插件（可选）：预留新闻/日历/社媒接口

### 验收标准（DoD）
- Brain 能独立触发 feature_build/eval/apply 与回补；报告生成；动作有审计与冷却锁。

### 风险与回滚
- 大脑重复触发：冷却与幂等指纹锁防抖。

### 指标/监控
- 动作执行数、冷却命中率、失败与重试次数、report 覆盖率。

## Phase 3 · 生产切换与链式触发（2–4 天）
### 目标
- 生产停用 polling Calculator，RFE 成为唯一在线特征源；OOF→HPO→alpha_loop 链式触发自动推进。

### 交付物
- features.polling.enabled=false（生产）
- 链式触发：OOF 成功 → 条件化 enqueue hpo_cli（冷却≥6h + 指纹幂等）→ 条件化 enqueue alpha_loop。

### 任务清单
- [ ] run_system.py / feature 相关管线：生产配置禁用轮询 FeatureCalculator；保留离线训练用 ClickHouse 特征
- [ ] scripts/ai_trader/control_event_consumer.py：链式触发逻辑 + 冷却与幂等

### 验收标准（DoD）
- 生产特征只来源 RFE；链式触发自动推进且无抖动；收益/风险指标达标（部署门槛/滚动窗口）。

### 风险与回滚
- RFE 故障：`manage_only` + 冷却；必要时临时启用 polling（仅故障期）；自动回退权重/参数。

### 指标/监控
- 触发链各环成功率、冷却命中率；上线后 Sharpe/PF/期望收益改善；回滚次数。

---

# 细化 TODO（按文件）

## libs/strategy/realtime_feature_engine/realtime_feature_engine.py
- [ ] 滑窗管理与 emit_interval（默认 1s）
- [ ] 计算 atr_14/ret_1m/ema/vwap/rvol 等核心指标
- [ ] warmup gating（warmup_bars）与心跳 TTL

## strategies/enhanced_model_signal_strategy.py
- [ ] initialize：extras.realtime_feature_engine.enabled 开关
- [ ] on_features：优先 RFE，price.ts 与 features.ts 漂移 ≤ 200ms；超限 skip + 审计
- [ ] _build_rl_state：使用 RFE 输出；未就绪时 fallback（开发态可用，生产默认关闭）

## backtest_ml_engine.py
- [ ] RFE‑equiv 生成（按 Catalog 定义）
- [ ] 训练/回测路径调用等价特征，保证一致性

## tools/nightly_oof_flow.py
- [ ] compare.json 标准化（含 xgb/lstm/fusion，symbols、window、可选 L1）
- [ ] oof_flow_status.json 写入；DROP IF EXISTS/try-except 防冲突

## wf_hpo.py
- [ ] evaluate_trial：返回 ic/ir/coverage 与 L3 指标

## hpo_cli_upgraded.py
- [ ] `--objective-formula`：AST 安全解析与标量化
- [ ] `--multi-objective`：NSGA-II；导出 pareto.json + best_params.json；选择规则清晰

## scripts/ai_trader/control_event_consumer.py
- [ ] alpha_loop/hpo_cli：HPO 门控（min_trials/min_unique_params/convergence_score_max）+ L1 可选门（min_ic/min_coverage）
- [ ] hpo_cli：优先 metrics.best_params 落参；fallback 工件
- [ ] 审计 ml_success.jsonl：记录 pareto_size/selection_rule（如有）
- [ ] next_actions 执行器：数据/特征/训练动作（TTL + 幂等 + 审计）
- [ ] 链式触发：OOF→hpo_cli→alpha_loop（冷却≥6h + 指纹幂等）

## tools/report_ic_to_sharpe.py（新增）
- [ ] 读取历史期内的 L1（IC/IR）与 L3（Sharpe 等）
- [ ] 输出 JSON/MD 报告与建议

---

# 配置建议（摘录）
```yaml
policies:
  features:
    realtime_feature_engine:
      enabled: true
      emit_interval_sec: 1
      warmup_bars: 60
      ts_alignment_ms_max_drift: 200
  hpo:
    success_gates:
      min_trials: 8
      min_unique_params: 6
      convergence_score_max: 0.30
      min_ic: 0.15        # 可选
      min_coverage: 0.90  # 可选
event_bus:
  topics:
    ml: internal.ml.status
```

---

# 接口与 Schema 约定（必须遵守）

## Realtime Feature Engine（RFE）API
- 启用：
  - `await ctx.enable_realtime_feature_engine(symbols: list[str], spec: dict | None = None, emit_interval_sec: int = 1, warmup_bars: int = 60)`
- 读取：
  - `features, ts_us, warmup_ok = ctx.get_realtime_features(symbol)`
    - `features`: dict（见下）
    - `ts_us`: int（微秒时间戳，同一帧内所有特征使用该 ts）
    - `warmup_ok`: bool（滑窗已满足 warmup_bars）
- 推荐最小特征集合（在线+离线一致）：
  - `atr_14`（基于 1m Bars，Wilder 平滑或 classic：请统一与离线实现）
  - `ret_1m`（上一分钟收盘收益：`ret_1m = close_t / close_{t-1} - 1`）
  - `ema_20`（1m 上的 EMA-20）
  - `vwap`（当日成交额/当日成交量，或滑窗 vwap，需与离线一致）
  - `rvol_10m`（10 分钟已实现波动率：`std(ret_1m, window=10)` 或同量纲定义）
- 示例返回：
```json
{
  "atr_14": 2.13,
  "ret_1m": 0.0012,
  "ema_20": 451.23,
  "vwap": 452.10,
  "rvol_10m": 0.0078
}
```
- 决策对齐守卫（策略侧）：若 `abs(price.ts - features.ts) > ts_alignment_ms_max_drift`（默认 200ms）→ 跳过该帧并审计。

## compare.json（OOF 对比）Schema（最小集）
```json
{
  "params": {
    "symbols": ["TSLA","AAPL"],
    "start": "2025-10-01 09:30:00",
    "end": "2025-10-31 16:00:00"
  },
  "xgb": {"sharpe": 1.10, "profit_factor": 1.25, "trades": 176, "max_drawdown": 0.12, "win_rate": 0.56, "auc": 0.54, "ic": 0.021},
  "lstm": {"sharpe": 0.95, "profit_factor": 1.12, "trades": 162, "max_drawdown": 0.15, "win_rate": 0.53, "auc": 0.52, "ic": 0.017},
  "fusion": {"sharpe": 1.32, "profit_factor": 1.31, "trades": 188, "max_drawdown": 0.10, "win_rate": 0.58}
}
```
- consumer 使用 `fusion` 作为门控输入；如果 compare.json 不存在或字段缺失 → 日志错误并早退（不影响其他事件）。

## oof_flow_status.json（OOF 状态）Schema（最小集）
```json
{
  "symbols": ["TSLA","AAPL"],
  "start": "2025-10-01 09:30:00",
  "end": "2025-10-31 16:00:00",
  "iters": [
    {
      "iter": 1,
      "code": 0,
      "per_symbol_ok": {
        "TSLA": {"passed": true, "metrics": {"auc": 0.55, "ic": 0.025, "coverage": 0.82}},
        "AAPL": {"passed": false, "metrics": {"auc": 0.51, "ic": 0.015, "coverage": 0.60}}
      }
    }
  ]
}
```
- 幂等：重复运行应仅追加迭代条目或覆盖同 iter 索引。

## 事件与审计 JSON（最小集）
- `ml_events.jsonl`（一行一事件）
  - started：`{"event":"ml.job","status":"started","job_type":"oof","job_id":"...","params":{...},"ts":"..."}`
  - completed：`{"event":"ml.job","status":"completed","job_type":"oof","job_id":"...","ok":true,"metrics":{...},"artifacts":[...],"duration_sec":123.4,"ts":"..."}`
- `ml_success.jsonl`（一行一审计）：
```json
{
  "ts": "2025-11-05T11:22:33-05:00",
  "job_id": "20251105112233-abc123",
  "job_type": "hpo_cli",
  "metrics": {"trials": 20, "unique_params": 15, "convergence_score": 0.22, "ic": 0.024, "coverage": 0.92},
  "gates": {"min_trials": 8, "min_unique_params": 6, "convergence_score_max": 0.3, "min_ic": 0.15, "min_coverage": 0.9},
  "checks": {"min_trials": true, "min_unique_params": true, "convergence_score_max": true, "min_ic": false, "min_coverage": true},
  "pass": false,
  "selection_rule": "lexicographic",
  "pareto_size": 7
}
```

---

# Phase 验收脚本（可直接执行）

## Phase 0
1) 启动：`venv/bin/python scripts/ai_trader/auto_loop.py`
2) 触发 OOF：
```python
from scripts.ai_trader.ml_scheduler import enqueue_job
enqueue_job({"job_type":"oof","priority":100,"params":{"symbols":["TSLA","AAPL"],"window_days":30}})
```
3) 预期：
- runs/<date>/oof_eval/compare.json 存在且包含 xgb/lstm/fusion
- runs/<date>/oof_flow_status.json 存在且迭代更新
- 策略日志/审计遇到 ts 漂移>200ms 时出现 order_skipped(ts_misaligned)

## Phase 1
1) 公式标量化 HPO：
```python
from scripts.ai_trader.ml_scheduler import enqueue_job
enqueue_job({"job_type":"hpo_cli","priority":75,"params":{
  "config":"config/hpo_enhanced.json","runner":"wf_hpo:evaluate_trial","backend":"optuna-tpe",
  "study_name":"HPO-Enhanced","storage":"sqlite:///study.db","n_trials":10,"n_jobs":1,
  "pruner":"asha","seed":2025,"objective-formula":"0.7*sharpe+0.3*ic"
}})
```
2) 多目标 HPO：
```python
enqueue_job({"job_type":"hpo_cli","priority":75,"params":{
  "config":"config/hpo_enhanced.json","runner":"wf_hpo:evaluate_trial","backend":"optuna-tpe",
  "study_name":"HPO-MO","storage":"sqlite:///mo.db","n_trials":20,"n_jobs":1,
  "pruner":"asha","seed":2025,
  "multi-objective":"[{\"metric\":\"sharpe\",\"maximize\":true},{\"metric\":\"ic\",\"maximize\":true}]"
}})
```
3) 预期：
- pareto.json 与 best_params.json 生成（多目标）
- ml_success.jsonl 审计包含 gates/checks 与 selection_rule/pareto_size
- 门控通过→ `apply_extras_params.py` 落参 → `cache/propose_plan.invalidate`

## Phase 2
1) 特征构建/评估/应用：
```python
# 由大脑 next_actions 或直接调用 enqueue_job
enqueue_job({"job_type":"feature_build","priority":60,"params":{"catalog":"config/features_catalog.json"}})
```
2) 预期：feature_eval → gates 通过 → feature_apply；ml_actions.jsonl 记录动作链与冷却命中。

## Phase 3
1) 生产禁用 polling：`features.polling.enabled=false`
2) 链式触发（OOF→hpo_cli→alpha_loop）由 consumer 条件化触发，设定冷却≥6h 与指纹幂等。
