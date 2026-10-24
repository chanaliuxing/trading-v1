ML 升级方案（hpo_cli_upgraded 兼容，最终落地版）

目标
- 降本增效：在现有 hpo_cli_upgraded.py + wf_hpo.py 架构上，最小改动落地 L1/L3 评估与自动化部署链条。
- 保持兼容：不引入大规模 refactor（例如新包的大迁移）；优先复用现有模块与事件总线。
- 结果可用：产出稳定的 OOF 基线、HPO 参数与成功门控；自动应用/回退透明可控。

不做的事（本期不建议）
- 不新增大体量 quant_metrics 包或替换 backtest 引擎。
- 不改动策略主线（EnhancedModelSignalStrategy）结构与注册体系。
- 不引入新的长链路服务（只扩充分层指标与事件处理）。

架构对齐
- HPO CLI：`hpo_cli_upgraded.py` 作为统一入口，调用 `wf_hpo:evaluate_trial` 评估单个 trial。
- Trial Runner：`wf_hpo.py` 已输出扩展指标（sortino/omega/calmar/PSR/DSR/PBO），并通过 StrategyAdapter 生成策略参数。
- 调度链路：`ml_scheduler`（job）→ Kafka `internal.ml.status`（事件）→ `control_event_consumer`（处理/门控/应用）。
- 成功门控：OOF 用回测指标门控；alpha_loop 用 HPO 专属门控（不依赖回测）。

P0（立即落地，已验证）
- 参数构造修正
  - 在 `wf_hpo.evaluate_trial` 中使用 `StrategyRegistry.get_adapter().parse_params` 合并参数，确保构造 `{"extras": {...}}`；若缺失 `extras.symbols` 则回退到 `config.symbols`。
  - 在 `ml_scheduler._execute_alpha_loop` 若缺失 `params.symbols`：回退 `config.universe` 或 `["TSLA"]`（并记录 warning）。
- 事件驱动自动化
  - OOF/ensemble_sweep/alpha_loop 任务完成后，`control_event_consumer` 处理：
    - OOF：对比指标通过 `success_gates` 时应用 ensemble 并写入审计。
    - ensemble_sweep：存在 `best_weights` 时直接应用权重。
    - alpha_loop：应用最佳参数（HPO 专属门控，见 P1 配置）。
- 成本控制与稳定性
  - `wf_hpo` 强制 workers clamp（Darwin 下 `spawn`），避免多进程内存膨胀。
  - 过长/僵尸进程：`ml_scheduler` 超时与孤儿锁自愈逻辑启用。

P1（本周完成）
- 成功门控配置
  - 在 `config.yml` 加入 `policies.hpo.success_gates`（仅 alpha_loop 使用），示例：
    - `min_trials: 8`
    - `min_unique_params: 6`
    - `convergence_score_max: 0.30`（基于 top-5 参数的标准差/熵）
  - `control_event_consumer` 在 alpha_loop 完成时检查上述门槛，满足后写回参数（`apply_extras_params.py`）并失效计划缓存。
- L1 指标（可选）
  - 在 `tools/nightly_oof_flow.py` 生成 `compare.json` 的同时，附带 AUC/IC/coverage（若存在真实标签/OOF 列）。
  - 这些指标仅用于报表与长期分析，不作为上线硬门。
- 运行时参数
  - 在 `scripts/ai_trader/config/ai_trader.yaml` 中增加 `features.success_gates`（特征评估链已具备，P0 已接入）。

P2（已完成 - Model Layer Infrastructure）
- ✅ P2.1: IC/IR/Coverage 计算（wf_hpo.py, 208行）
  - IC (Information Coefficient): Spearman相关性，衡量预测质量
  - IR (Information Ratio): IC的一致性，衡量预测稳定性
  - Coverage: 预测覆盖率
- ✅ P2.2: IC→Sharpe 相关性分析工具（tools/report_ic_to_sharpe.py, 332行）
  - 验证IC预测力对Sharpe优化的作用
- ✅ P2.3: ML诊断引擎（scripts/ai_trader/brain/ml_diagnostics.py, 293行）
  - 自动诊断: IC < 0.10 → feature_build, IC < 0.20 → run_hpo_cli
  - 6小时冷却机制，防止action spam
  - 集成到 control_event_consumer.py (+130行)

P3.B（已完成 - Multi-Objective HPO）
- ✅ P3.B.1: 公式标量化（hpo_cli_upgraded.py, +109行）
  - AST-based安全公式解析（不使用eval()）
  - 示例: `--objective-formula "0.7*sharpe + 0.3*ic"`
  - 支持运算符: +, -, *, /, ()
  - 支持函数: abs(), min(), max()
- ✅ P3.B.2: Pareto多目标优化（+395行）
  - NSGA-II遗传算法生成Pareto前沿
  - 词典序选择最佳参数
  - 输出 {study_name}_pareto.json（Pareto前沿）
  - 输出 {study_name}_best_params.json（选出的最佳解）
- ✅ P3.B.3: L1 门控支持（control_event_consumer.py, +76行）
  - Model Layer质量门控: min_ic, min_coverage
  - 若IC/coverage低于阈值，阻止参数应用
  - 审计日志包含多目标元数据（selection_rule, pareto_size）

三层架构（已实现）
```
Model Layer (L1)     → IC, IR, Coverage
     ↓
Multi-Obj Optimizer  → Formula: "0.7*sharpe + 0.3*ic"
                      → Pareto: NSGA-II front
     ↓
Quality Gates        → L1: min_ic, min_coverage
                      → L3: min_trials, convergence
     ↓
Strategy Layer (L3)  → Sharpe, Sortino, MDD
```

实现细节（与代码路径）
- 参数构造（已完成）
  - `wf_hpo.py:evaluate_trial`：使用适配器生成 `strategy_params`；缺失 `extras.symbols` 回退 `config.symbols`。
  - `scripts/ai_trader/ml_scheduler.py:_execute_alpha_loop`：缺失 symbols 安全回退并记录日志。
- OOF 门控与应用（已存在）
  - `scripts/ai_trader/control_event_consumer.py:_evaluate_and_apply_on_success`：OOF `compare.json` → metrics → gates 通过 → `choose_ensemble` + `apply_ensemble_policy`。
- HPO 专属门控（新增配置）
  - `config.yml: policies.hpo.success_gates`；consumer 的 `alpha_loop` 分支读取并生效。
- 事件与回退
  - `ml_scheduler._publish_ml_event`：确认 Kafka 发布结果并总是写入 `runs/.../ml_events.jsonl`；失败清晰记录。
- 稳定性与自愈
  - `ml_scheduler`：超时守护 + 孤儿锁恢复；每轮 requeue stale running；启动清锁。
  - `wf_hpo`：Darwin 下 `multiprocessing.set_start_method('spawn')` + workers clamp。

配置要点
- `config.yml`
  - `event_bus.topics.ml: internal.ml.status`
  - `policies.loop_sleep_sec`：weekend/morning/intraday/afterhours/overnight
  - `policies.hpo.success_gates`（新增）：`min_trials`、`min_unique_params`、`convergence_score_max`
  - `universe`：供 alpha_loop 缺省使用的 symbols 来源
- `scripts/ai_trader/config/ai_trader.yaml`
  - `policies.propose_plan`：TTL + quiet hours + 触发器
  - `policies.risk_pretrade`：per‑symbol TTL/阈值（已接入）
  - `features.success_gates`：特征链门控（已接入）

验证与回滚
- 本地验证
  - 启动：`venv/bin/python scripts/ai_trader/auto_loop.py`
  - 观察：`runs/ai_trader/<date>/ml_events.jsonl`、`ml_success.jsonl`、ensemble policy 与 `config.yml` 变化。
  - 任务：
    ```python
    from scripts.ai_trader.ml_scheduler import enqueue_job
    enqueue_job({"job_type":"alpha_loop","priority":80,"params":{"symbols":["TSLA","AAPL"],"days":30,"samples":10}})
    ```
- 回滚策略
  - 关闭 autosettings 或将 `policies.hpo.success_gates` 提升至不可达（立即阻断参数上线）。
  - 手工恢复 `ensemble_policies.json` 或 `config.yml` 备份（工具会保留审计）。

上线清单
- 配置：补充 `policies.hpo.success_gates`、确认 `event_bus.topics.ml` 与 `universe`。
- 进程：`auto_loop`（建议 venv）、`control_event_consumer`、`ml_scheduler`。
- 观测：`runs/ai_trader/<date>/ml_events.jsonl`、`ml_actions.jsonl`、`ml_success.jsonl`、`oof_eval/compare.json`。

为何不做大包重构
- 现有 wf_hpo/backtest/consumer/scheduler 已具备完整链路；引入大包会增加维护负担与风险。
- 通过适配器与门控配置即可满足 L1/L3 评估与上线自动化，收益/投入比更优。

附：`policies.hpo.success_gates` 示例（包含L1门控）
```yaml
policies:
  hpo:
    success_gates:
      # L3 Gates (Strategy Layer) - 必须
      min_trials: 8
      min_unique_params: 6
      convergence_score_max: 0.30

      # L1 Gates (Model Layer) - 可选
      min_ic: 0.15          # IC (Information Coefficient) 最低阈值
      min_coverage: 0.90    # 预测覆盖率最低阈值

      audit_note: "alpha_loop stability gates with L1 quality control"
```

**说明**:
- L3 Gates: 基于HPO过程的收敛性指标（trials, unique_params, convergence）
- L1 Gates: 基于Model Layer质量指标（IC, coverage）
- 若设置L1 Gates: HPO参数仅在 `ic >= min_ic` 且 `coverage >= min_coverage` 时应用
- 若不设置L1 Gates: 仅使用L3 Gates（向后兼容）

状态（最新更新：2025-11-06）
- ✅ P0: 参数构造修复已落地（wf_hpo.py、ml_scheduler.py）
- ✅ P0: 事件链条已稳定（发布 + 审计 JSONL）
- ✅ P1: HPO 成功门控配置已实现（L3 Gates）
- ✅ P2: Model Layer Infrastructure 完成（IC/IR/Coverage + ML Diagnostics）
- ✅ P3.B: Multi-Objective HPO 完成（Formula + Pareto + L1 Gates）
- ⏸️ P3.A: OOF Flow 功能完整（幂等性优化可延期）

**Commit**: 192292cd - feat(ml): Add P2+P3 ML infrastructure (1360 lines, 5 files)

附录 A：必备环境与启动方式
- 强制使用本仓库 venv 运行（避免系统 Python 的 NumPy/Pandas ABI 冲突）
  - 启动主循环：`venv/bin/python scripts/ai_trader/auto_loop.py`
  - 单次 HPO（示例）：
    - `venv/bin/python hpo_cli_upgraded.py --config config/hpo_enhanced.json --runner wf_hpo:evaluate_trial --n-trials 5 --seed 2025`
  - 必备服务：Kafka、ClickHouse（读取自 `config.yml:kafka` 与 `clickhouse`）。

附录 B：配置差异清单（可复制粘贴）
- config.yml（新增或确认）：
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

    hpo:
      success_gates:           # alpha_loop 专属门控（P1 + P3.B.3）
        # L3 Gates (Strategy Layer)
        min_trials: 8
        min_unique_params: 6
        convergence_score_max: 0.30

        # L1 Gates (Model Layer) - 可选
        min_ic: 0.15           # IC 最低阈值（P2.1）
        min_coverage: 0.90     # 预测覆盖率最低阈值（P2.1）

        audit_note: "alpha_loop stability gates with L1 quality control"

  universe: [TSLA, AAPL, NVDA, SPY]  # alpha_loop 缺省 symbols 来源
  ```
- scripts/ai_trader/config/ai_trader.yaml（参考项）：
  - `policies.propose_plan`（TTL + 静默时段 + 触发器）
  - `policies.risk_pretrade`（per‑symbol TTL/阈值）
  - `features.success_gates`（特征评估链门控）

附录 C：代码任务 TODO（逐条落地）
- wf_hpo.py
  - ensure：`evaluate_trial` 使用 `StrategyRegistry.get_adapter().parse_params` 生成 `{"extras": {...}}`；若缺失 `extras.symbols`，回退 `config["symbols"]`。
- scripts/ai_trader/ml_scheduler.py
  - ensure：`_execute_alpha_loop` 若缺失 `params.symbols`，回退 `config.universe` → `["TSLA"]`，记录 `alpha_loop_missing_symbols_fallback`。
- scripts/ai_trader/control_event_consumer.py（P1）
  - 新增：alpha_loop 分支读取 `policies.hpo.success_gates` 门控；通过则调用 `apply_extras_params.py`，审计到 `ml_success.jsonl` 并失效计划缓存。
- tools/nightly_oof_flow.py（可选）
  - 生成 compare.json 同时追加 L1 指标（AUC/IC/coverage）到 `params.metrics_l1`（若可得）。

附录 D：事件契约与样例 payload
- OOF 完成（ml.job.completed/oof）：
  ```json
  {
    "event": "ml.job",
    "status": "completed",
    "job_type": "oof",
    "job_id": "20251105021753-pvtawv",
    "artifacts": ["runs/ai_trader/2025-11-05/oof_eval/compare.json"],
    "metrics": {
      "bt": {
        "fusion": {"sharpe": 1.52, "profit_factor": 1.28, "trades": 164}
      },
      "best_mode": "fusion"
    }
  }
  ```
- alpha_loop 完成（ml.job.completed/alpha_loop）：
  ```json
  {
    "event": "ml.job",
    "status": "completed",
    "job_type": "alpha_loop",
    "job_id": "20251105020245-lxhqjo",
    "metrics": {
      "trials": 12,
      "unique_params": 10,
      "convergence_score": 0.22,
      "best_params": {"market_filter.conf_floor": 0.6, "sizing.fixed.base_qty": 200}
    },
    "artifacts": ["runs/ai_trader/2025-11-05/hpo_20251105020245-lxhqjo/"]
  }
  ```

附录 E：可执行验证剧本（命令 + 预期产物）
1) 启动链路
   - `venv/bin/python scripts/ai_trader/auto_loop.py`
   - 预期：`control_event_consumer.py` 与 `ml_scheduler.py` 被拉起（`scripts/ai_trader/status.sh` 可见）。
2) 触发 OOF（测试）
   - Python：
     ```python
     from scripts.ai_trader.ml_scheduler import enqueue_job
     enqueue_job({"job_type":"oof","priority":100,"params":{"symbols":["TSLA","AAPL"],"window_days":30}})
     ```
   - 预期：
     - `runs/ai_trader/<date>/oof_eval/compare.json` 存在
     - `runs/ai_trader/<date>/ml_events.jsonl` 出现 completed 事件
     - 若门控通过：ensemble 被应用；`ml_success.jsonl` 记录一条 `job_type=oof, applied=true`
3) 触发 alpha_loop（测试）
   - Python：
     ```python
     from scripts.ai_trader.ml_scheduler import enqueue_job
     enqueue_job({"job_type":"alpha_loop","priority":80,
                  "params":{"symbols":["TSLA","AAPL"],"days":30,"samples":10,"timeout_hours":0.5}})
     ```
   - 预期：
     - `runs/ai_trader/<date>/hpo_*` 目录生成
     - completed 事件携带 `trials/unique_params/convergence_score`
     - 若门控通过：`apply_extras_params.py` 改写 `config.yml`；`cache/propose_plan.invalidate` 生成；`ml_success.jsonl` 记录 applied

4) 触发 hpo_cli（独立 Optuna CLI 集成）
   - Python：
     ```python
     from scripts.ai_trader.ml_scheduler import enqueue_job
     enqueue_job({
       "job_type":"hpo_cli",
       "priority":75,
       "params":{
         "config":"config/hpo_enhanced.json",
         "runner":"wf_hpo:evaluate_trial",
         "backend":"optuna-tpe",
         "study_name":"HPO-Enhanced",
         "storage":"sqlite:///study.db",
         "n_trials":10,
         "n_jobs":1,
         "pruner":"asha",
         "seed":2025
       }
     })
     ```
   - 预期：
     - `runs/ai_trader/<date>/hpo_cli_<job_id>/HPO-Enhanced_best_params.json` 生成
     - 若 `storage` 为 sqlite，`study.db` 生成于上述目录
     - `ml_events.jsonl` 有 `ml.job.completed/hpo_cli` 事件；metrics.best_params 携带最佳参数

5) 触发 hpo_cli（公式标量化 - P3.B.1）
   - Python：
     ```python
     from scripts.ai_trader.ml_scheduler import enqueue_job
     enqueue_job({
       "job_type":"hpo_cli",
       "priority":75,
       "params":{
         "config":"config/hpo_enhanced.json",
         "runner":"wf_hpo:evaluate_trial",
         "backend":"optuna-tpe",
         "study_name":"HPO-Formula",
         "storage":"sqlite:///formula.db",
         "n_trials":20,
         "n_jobs":1,
         "seed":2025,
         "objective-formula":"0.7*sharpe + 0.3*ic"
       }
     })
     ```
   - 预期：
     - `HPO-Formula_best_params.json` 生成，包含：
       - `formula_value`: 标量化后的最优值
       - `formula`: "0.7*sharpe + 0.3*ic"
       - `sharpe`, `ic`: 各指标原始值
       - `selection_rule`: "formula_scalarized"
     - L1 门控检查：若 `ic < min_ic` 或 `coverage < min_coverage`，参数不应用
     - 审计记录包含 `selection_rule` 和公式信息

6) 触发 hpo_cli（Pareto 多目标优化 - P3.B.2）
   - Python：
     ```python
     from scripts.ai_trader.ml_scheduler import enqueue_job
     enqueue_job({
       "job_type":"hpo_cli",
       "priority":75,
       "params":{
         "config":"config/hpo_enhanced.json",
         "runner":"wf_hpo:evaluate_trial",
         "backend":"optuna-tpe",
         "study_name":"HPO-Pareto",
         "storage":"sqlite:///pareto.db",
         "n_trials":30,
         "n_jobs":1,
         "seed":2025,
         "multi-objective":'[{"metric":"sharpe","maximize":true},{"metric":"ic","maximize":true}]'
       }
     })
     ```
   - 预期：
     - `HPO-Pareto_pareto.json` 生成（Pareto 前沿），包含：
       - `n_pareto`: Pareto 最优解数量
       - `objectives`: ["sharpe", "ic"]
       - `pareto_front`: 所有非支配解的目标值
     - `HPO-Pareto_best_params.json` 生成（词典序选出的最佳解），包含：
       - `selection_rule`: "pareto_lexicographic_first_objective"
       - `pareto_size`: Pareto 集合大小
       - `sharpe`, `ic`: 选出解的目标值
     - L1 门控检查：`ic >= min_ic` 且 `coverage >= min_coverage`
     - 审计记录包含 `selection_rule`, `pareto_size`, `is_multi_objective`

附录 F：日志与产物对照表
- 关键日志键：`ml_event_publish_result`、`ml_event_jsonl_written`、`alpha_loop_missing_symbols_fallback`、`success_apply_ensemble`、`apply_hpo_params`
- 关键产物：
  - `runs/ai_trader/<date>/ml_events.jsonl`、`ml_actions.jsonl`、`ml_success.jsonl`
  - `runs/ai_trader/<date>/oof_eval/compare.json`、`hpo_*` 输出目录

附录 G：失败排障与回退
- Kafka 不可用：`_publish_ml_event` 已写本地 JSONL；允许后续消费或手动触发。
- HPO 超时/僵尸：检查 `ml_slot.lock` 与队列；scheduler 启动清锁 + 周期 requeue；必要时重投 `enqueue_job`。
- 误应用回退：关闭 autosettings 或调高 success_gates；恢复 `ensemble_policies.json`/`config.yml` 最近备份。

附录 H：验收标准（Definition of Done）
- OOF：completed → 门控通过 → ensemble 应用且有审计（ml_success.jsonl）。
- alpha_loop：completed → HPO success_gates 通过 → `config.yml` 被 `apply_extras_params.py` 更新并下轮生效（invalidate 文件存在）。
- 全链路在 venv 下运行，日志/产物路径齐全，失败有审计记录。

附录 I：资源/配额与阈值（抖动保护）
- `ml_scheduler`：建议 `job_timeout_sec=3600`；`running_stale_sec=900`；`slot_orphan_sec=900`（按需参数化）。
- `wf_hpo` 并发：Darwin 建议 `workers=1`；Linux 可逐步提升，前提是内存预算充足。

附录 J：最小变更补丁清单（便于 CR）
- `wf_hpo.py`：`evaluate_trial` 参数构造 + symbols 回退（已实现）。
- `scripts/ai_trader/ml_scheduler.py`：alpha_loop symbols fallback（已实现）。
- `scripts/ai_trader/control_event_consumer.py`：alpha_loop 成功门控分支（待实现/验证 P1）。
- `tools/nightly_oof_flow.py`：可选 L1 指标导出（可选）。
