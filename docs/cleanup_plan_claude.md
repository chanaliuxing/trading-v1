# 📂 根目录清理方案（Claude 版）

> 本方案是在 `docs/cleanup_plan.md`（Gemini 版）基础上，结合 import 依赖图与运行期启动链路做的安全加固版。核心差异：**按 import 图决定是否能移动，按阶段分 commit，禁止 glob `mv`**。

---

## 1. Gemini 方案的 3 个关键风险

### ⚠️ 风险 1：`*fix.py` 通配会误伤生产模块
Gemini 第二步示例：
```bash
mv *fix.py scripts/tools/db/
```
根目录下名字含 `fix` 的文件会被 shell glob 误匹配，且 `tools/compute_features_phase1_fixed.py`、测试目录 `test_bug10_fix/` 等边界情况无法覆盖。**通配移动必须禁止**，只走"明确文件列表"。

### ⚠️ 风险 2：`PYTHONPATH=.` 并不能救回所有 import
Gemini 承诺"不改代码，靠 `PYTHONPATH=.` 保持兼容"。实测 import 关系：

| 被 import 的模块 | 被谁引用 | 影响 |
|---|---|---|
| `boot_logging` | `run_system.py:37`（**生产入口！**）、`alpha_loop.py`、`paper_engine.py`、`backtest_ml_engine.py`、`scripts/ai_trader/*` | 移走立刻挂生产 |
| `strategy_adapter` | `adapters/*.py`（4 个 adapter，**库代码**而非脚本） | 移走挂掉 adapters 包 |
| `strategy_registry` | `alpha_loop.py`、`wf_hpo.py` | 双向依赖 |
| `sim_adapter` | `broker/router.py`（**库代码**） | 移走挂掉 broker 包 |
| `leakage_guard` / `grid_guard` | `alpha_loop.py`、`wf_hpo.py`、`pi/pi_orchestrator.py`、`backtest_ml_engine.py` | 多处耦合 |
| `wf_hpo` | `alpha_loop.py`、`tests/unit/backtest/*`、`tools/*` | 测试也会断 |
| `backtest_ml_engine` | `premarket_calibrator.py`、7 个 `run_backtest_*.py`、`tools/backtest_ensemble_compare.py`、`scripts/ai_trader/rl/eval_rl.py`、`tests/unit/backtest/*` | 影响面最大 |
| `metrics_extra` / `models_linear` / `models_ensemble` | `wf_hpo.py`、互相引用 | 库级耦合 |

只要用户或 `.command` / cron / launchd / IDE run-config 不带 `PYTHONPATH=.` 启动，立即 ImportError。而 `config.yml:5` 里的命令是 `venv/bin/python alpha_loop.py ...`，没有 `PYTHONPATH`。

### ⚠️ 风险 3：Gemini 把库文件当脚本迁走
Gemini 把 `strategy_adapter.py` / `strategy_registry.py` / `sim_adapter.py` / `leakage_guard.py` / `grid_guard.py` / `models_*.py` / `metrics_extra.py` 归入了可迁移脚本，但这些是**被库代码 import 的库文件**，不是 CLI 脚本。

---

## 2. 核心原则（对 Gemini 的修订）

1. **区分"脚本"与"库"**：被 `adapters/`、`broker/`、`pi/`、`libs/`、`services/`、`strategies/`、`tests/`、`run_system.py` import 的一律**留在根目录**（或走 Stage 2 正式搬进 `libs/`）。
2. **只移动"真·入口脚本"和"纯数据产物"**：没有被别处 import、只作为 CLI / 实验工具的 `.py`，以及 JSON/CSV/LOG/临时目录。
3. **禁止任何 glob `mv`**：全部用显式文件列表，一条一条。
4. **不依赖 `PYTHONPATH=.` 的运行期绕过**：跨层 import 的脚本，改为用 `git mv` 同时检查 import 路径，再跑冒烟测试；否则保留原位。
5. **两阶段：先归档垃圾，再迁移脚本**。日志、备份、临时测试目录删/归档几乎 0 风险，应该先做。

---

## 3. 根目录真正的"锚点"（必须留在根）

| 文件 | 原因 |
|---|---|
| `run_system.py` | 生产入口，`Start_Automation.command` 间接依赖 |
| `alpha_loop.py` | `config.yml:5` 定义的命令直接执行它 |
| `config.yml`, `config_test.yml`, `label_profiles.yml` | 工作目录相对读取 |
| `Makefile`, `Makefile.tests` | 构建入口 |
| `CLAUDE.md`, `GEMINI.md`, `AGENTS.md` | 项目导航（工具会读） |
| `boot_logging.py` | 被 `run_system.py:37` 直接 import，无法移动 |
| `llm_client.py` | 多处 import（需进一步确认后再考虑移入 `libs/`） |
| **`strategy_adapter.py`, `strategy_registry.py`, `sim_adapter.py`** | 被库代码 import，**Gemini 方案里漏了** |
| **`leakage_guard.py`, `grid_guard.py`, `portfolio_overlay.py`** | 被 `alpha_loop.py` / `wf_hpo.py` / `pi/` 直接 import |
| **`metrics_extra.py`, `models_linear.py`, `models_ensemble.py`** | `wf_hpo.py` 直接 import |
| **`backtest_ml_engine.py`, `wf_hpo.py`, `wf_hpo_adapter.py`, `hpo_backends.py`** | 被 7+ 处 import 的"事实库"，短期不要动；长期应移入 `libs/backtest/` + 同步改 import |
| `paper_engine.py`, `premarket_calibrator.py` | 高耦合，短期保留 |
| `requirements*.txt`, `.env*`, `.gitignore`, `.gitattributes` | 工具链 |
| `venv`, `venv_ml`, `.venv_autogen` | Python 环境 |

---

## 4. Stage 0 — 零风险清理（先做，立刻见效）

直接删除或归档到 `scripts/archive/`（不影响任何 import）：

### 4.1 日志/备份文件
纯产物，`.gitignore` 里应已忽略，先确认 git 状态再动：
- `run_system.log`, `run_system_stub.log`, `debug_learn.log`, `feature_recompute.log`
- `december_ml_run*.log`, `learn_final_test.log`, `sell_log.txt`
- `.coverage`, `.DS_Store`, `minimal_test.db`

### 4.2 错乱文件名
明显是误操作产物，名字就是 shell 参数：
- `-------------`, `-E`, `=0.29.0`, `=2.5.0`, `echo`, `grep`, `status`, `uuid`, `passed|failed|error`

### 4.3 备份快照
- `docker-compose.yml.deprecated`, `feature_programs_backup_20251112_150659.json`
- `v1_logic_skeleton.txt`, `venv_py313_broken_20260316/`（整个目录）
- `.env.bak`, `.env.orginal_mac`（⚠️ 含旧凭据，应**安全销毁**而非归档）

### 4.4 临时实验目录
确认无人读取后归档：
- `bug10_real_test/`, `alpha_memfix_test/`, `alpha_minimal_memory_test/`, `alpha_fixed_run/`, `alpha_final_verified/`
- `test_absolute_final/`, `test_bug10_fix/`
- `tmp_test_runs/`, `tmp/`, `.tmp/`, `junk/`
- `backtest_results/`（740 条子目录）、`results/`（728 条）——建议先归档压缩到外部盘，根目录保留空壳或软链

### 4.5 实验结果 JSON
迁到 `scripts/research/artifacts/`：
- 所有 `*_best_params.json` (7 个)、`feature_engineering_suggestion_*.json`、`feature_programs*.json`
- `ml_objectives.json`, `ml_policy.json`, `labels_programs.json`, `loss_programs.json`, `alpha_config.json`
- `completed_trials.jsonl`（2MB）、`resume_state.json`、`judged.csv`, `judged.llm_cache.json`
- `stock_news_sentiment_*.csv`, `tsla_v7_2_lkg.json`, `googl_v7_2_lkg.json`

**Stage 0 预计效果**：根目录文件数从 ~130 → ~70，零 import 风险。

---

## 5. Stage 1 — 低风险脚本迁移（白名单）

先对每个候选跑：
```bash
grep -rn "^from <name>\|^import <name>" --include='*.py' .
```
确认零 import 后才移动。

### 5.1 迁到 `scripts/tools/db/`
均为独立短脚本，<100 行，无被 import：
- `align_db_with_real_ids.py`, `atomic_db_id_fix.py`, `complete_db_fix.py`, `harden_db.py`
- `get_db_schema.py`, `check_internal_pos.py`, `check_strategy_schema.py`
- `test_db_stability.py`, `inject_atr_data.py`, `mock_data_injector.py`
- `verify_constraints.py`, `fix_bars_1m_ibkr_schema.sql`, `fix_bars_1m_ibkr_schema_final.sql`

### 5.2 迁到 `scripts/tools/ops/`
- `cleanup_dead_order.py`, `cancel_sqqq_only.py`, `find_hanging_orders.py`, `find_order_4.py`, `find_any_order_4.py`
- `inspect_ghost_orders.py`, `get_ibkr_execution_ids.py`, `reconcile_positions.py`, `audit_tqqq_order.py`

### 5.3 迁到 `scripts/tools/verify/`
- `verify_system.py`, `verify_count.py`, `verify_ts.py`, `verify_live_atr.py`, `verify_qqq_v2_ultimate.py`
- `verify_meta_brain.sh`, `verify_ml_infrastructure.sh`, `verify_linear_models_deployment.sh`, `verify_test_setup.sh`
- `quick_system_validation.py`, `quick_verify.sh`, `audit_implementation.py`, `diagnose_predictions.py`
- `benchmark_speed.py`, `minimal_hpo_test.py`, `validate_self_programming.py`

### 5.4 迁到 `scripts/research/`
- `apply_hpo_params.py`, `apply_extras_params.py`, `apply_config_patch.py`
- `compare_hpo_params.py`, `export_hpo_report.py`, `prove_hpo_improvement.py`, `validate_hpo_params.py`
- `hpo_cli.py`, `hpo_cli_upgraded.py`（⚠️ 后者 import `hpo_backends`、`wf_hpo_adapter` — 两者留根则用 `PYTHONPATH=.` 启动；或一起移走）
- `hot_reload_test_params.py`, `update_strategies_to_test.py`, `relax_strategies_for_testing.py`
- `debug_backtest.py`, `debug_vwap_signals.py`, `demo.py`, `demo_system.py`, `generate_mock_backtest.py`

### 5.5 回测脚本——不要直接迁
`run_backtest_*.py` 都 import `backtest_ml_engine` 或 `wf_hpo`，必须与被 import 对象同级或改 import：
- `run_backtest_simple.py`, `run_backtest_batch.py`, `run_backtest_batch_relaxed.py`, `run_backtest_reve.py`, `run_backtest_qqq_0dte_options.py`, `run_backtest_with_viz.py`, `run_full_backtest.py`, `run_safe_backtest.py`, `run_single_backtest.py`, `plot_backtest.py`

**两种方案二选一**：
- **A（保守）**：暂留根目录，统一重命名加 `backtest_` 前缀聚集视觉分组。
- **B（一次到位）**：和 `backtest_ml_engine.py`、`wf_hpo.py`、`wf_hpo_adapter.py`、`hpo_backends.py`、`metrics_extra.py`、`models_linear.py`、`models_ensemble.py`、`leakage_guard.py`、`grid_guard.py` 一起迁到 `libs/backtest/` 或 `scripts/backtest/`，**同时**用 `grep -rl` 批量改 import 路径，跑 `pytest tests/unit/backtest/ tests/unit -k backtest` 和一次 `make test` 验证。推荐 B，但需独立的一个 PR。

---

## 6. Stage 2 — 结构性迁移（需要改 import，独立 PR）

把真·库文件从根目录搬进 `libs/`，并修改所有 import。这是**重构**不是"清理"，Gemini 方案忽略了这一步导致它的"不改代码"承诺无法兑现。

建议顺序：
1. `libs/backtest/`：`backtest_ml_engine.py`, `wf_hpo.py`, `wf_hpo_adapter.py`, `hpo_backends.py`, `metrics_extra.py`, `leakage_guard.py`, `grid_guard.py`
2. `libs/models/`：`models_linear.py`, `models_ensemble.py`
3. `libs/strategy/adapters/`：`strategy_adapter.py`, `strategy_registry.py`, `sim_adapter.py`, `paper_engine.py`, `portfolio_overlay.py`, `premarket_calibrator.py`
4. `libs/obs/boot_logging.py`：需要同步改 `run_system.py` 和 4+ 其它入口

每一步都要：
```bash
ruff check .
mypy src/
pytest tests/unit
# 再启一次 shadow 模式 run_system.py 冒烟
```

---

## 7. Stage 3 — 安全护栏

- **先 `git mv` 不是 `mv`**：保留历史、方便 revert。
- **分 commit 分阶段**：Stage 0 一个 commit、Stage 1 按子目录各一个、Stage 2 每类库一个。
- **迁移后即刻跑**：`make test` + `ruff check .` + `python -c "import alpha_loop, run_system"` 三件套。
- **`.command` 文件不要动**：`Start_Automation.command` → `scripts/setup_full_daily_automation.sh` 链路不经过根脚本。
- **`.gitignore` 补一笔**：在 Stage 0 前，加入 `*.log`, `.coverage`, `.DS_Store`, `alpha_out/`, `backtest_results/`, `results/`, `tmp*/`，避免再污染。

---

## 8. 和 Gemini 方案的差异汇总

| 维度 | Gemini | 本方案（Claude） |
|---|---|---|
| 迁移依据 | 按文件名模式 | 按 **import 图**（被 import 的留根） |
| 执行方式 | shell `mv *.py` 通配 | `git mv` 显式列表 |
| 风险控制 | "靠 `PYTHONPATH=.`" | 分阶段 + 跑测试 + 分 commit |
| 是否改 import | "不改代码" | Stage 2 **必须**改，并承认这是重构 |
| 锚点范围 | 6 个文件 | 15+ 个文件（补上了库性质的根脚本） |
| 零风险清理 | 未单独拆 | Stage 0 先干，立刻降熵 |
| `.env.bak` 处理 | 归档 | **安全销毁**（含旧凭据） |

---

## 9. 推荐执行顺序

1. **今天**：Stage 0（归档日志/备份/乱文件/实验 JSON），一个 commit。
2. **本周**：Stage 1 按子目录分 4 个 commit 推进，每次迁完跑 `make test`。
3. **下周独立 PR**：Stage 2 结构性迁移，改 import 路径，大范围回归测试（含 shadow 模式冒烟）。
4. **收尾**：更新 `CLAUDE.md` / `GEMINI.md` / `AGENTS.md` 中提到的路径引用，补 `.gitignore`。
