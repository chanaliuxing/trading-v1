# 📂 系统架构清理计划：根目录“脚本丛林”重整

## 1. 目标与原则 (Objectives & Principles)

*   **目标：** 降低根目录熵值，消除 LLM 上下文噪音，提高系统可维护性。
*   **清理原则：**
    *   **不删除：** 所有脚本仅做物理移动（`mv`），确保历史工具可查。
    *   **不改代码：** 移动后通过 `PYTHONPATH=.` 保持导入兼容性。
    *   **命名空间隔离：** 将功能重叠或临时的脚本按职责归入 `scripts/` 子目录。

## 2. 现状分析 (Entropy Evidence)

根目录目前堆积了 100+ 文件，主要问题点：
*   **回测脚本爆炸：** 存在 8 个以上的 `run_backtest_*.py` 变体，增加了选择难度。
*   **状态补丁堆积：** 大量 `*_fix.py` 和 `reconcile_*.py` 脚本散落，反映了系统在不同阶段的稳定性补救。
*   **实验噪音：** 50+ 个临时任务目录（如 `bug10_test/`）和 JSON 参数文件干扰了代码调查工具。

## 3. 目标目录结构 (Target Taxonomy)

我们将建立以下标准目录体系：

| 目录路径 | 职责描述 |
| :--- | :--- |
| `scripts/backtest/` | 各种维度的回测入口、回测数据生成与可视化工具。 |
| `scripts/research/` | HPO 参数搜索、Alpha 生成、特征建议及实验结果 (JSON)。 |
| `scripts/tools/db/` | 数据库 ID 修复、Schema 校验、对账工具、数据迁移脚本。 |
| `scripts/tools/ops/` | 订单清理、挂单排查、仓位对冲、Telegram 辅助工具。 |
| `scripts/tools/verify/` | 系统验证 (Verify)、基准测试 (Benchmark)、逻辑审计 (Audit)。 |
| `scripts/archive/` | 废弃文件 (.deprecated)、历史备份、过时的临时测试目录。 |

## 4. 迁移详细清单 (Detailed Migration List)

### 4.1 回测丛林 -> `scripts/backtest/`
*   `run_backtest_batch.py`, `run_backtest_simple.py`, `run_backtest_reve.py`, `run_backtest_with_viz.py`
*   `run_backtest_batch_relaxed.py`, `run_backtest_qqq_0dte_options.py`, `run_full_backtest.py`
*   `run_single_backtest.py`, `backtest_ml_engine.py`, `generate_mock_backtest.py`, `plot_backtest.py`

### 4.2 研究与 HPO -> `scripts/research/`
*   **脚本：** `hpo_cli.py`, `hpo_backends.py`, `wf_hpo.py`, `wf_hpo_adapter.py`, `validate_hpo_params.py`
*   **配置：** `*best_params.json`, `feature_engineering_suggestion_*.json`, `feature_programs_v2.json`, `ml_objectives.json`
*   **其他：** `ml_policy.json`, `labels_programs.json`, `loss_programs.json`, `alpha_config.json`

### 4.3 数据库与一致性维护 -> `scripts/tools/db/`
*   `complete_db_fix.py`, `atomic_db_id_fix.py`, `align_db_with_real_ids.py`, `harden_db.py`
*   `get_db_schema.py`, `test_db_stability.py`, `inject_atr_data.py`, `mock_data_injector.py`

### 4.4 运维与故障排查 -> `scripts/tools/ops/`
*   `cleanup_dead_order.py`, `cancel_sqqq_only.py`, `find_hanging_orders.py`, `inspect_ghost_orders.py`
*   `find_order_4.py`, `find_any_order_4.py`, `get_ibkr_execution_ids.py`, `reconcile_positions.py`
*   `grid_guard.py`, `leakage_guard.py`, `portfolio_overlay.py`

### 4.5 验证与审计 -> `scripts/tools/verify/`
*   `verify_system.py`, `verify_count.py`, `verify_ts.py`, `verify_live_atr.py`, `verify_qqq_v2_ultimate.py`
*   `verify_meta_brain.sh`, `verify_ml_infrastructure.sh`, `quick_system_validation.py`
*   `audit_implementation.py`, `audit_tqqq_order.py`, `diagnose_predictions.py`

### 4.6 归档死代码 -> `scripts/archive/`
*   **临时目录：** `bug10_real_test/`, `alpha_memfix_test/`, `alpha_minimal_memory_test/`, `alpha_fixed_run/`
*   **废弃文件：** `*.deprecated`, `feature_programs_backup_*.json`, `v1_logic_skeleton.txt`, `resume_state.json`

## 5. 根目录保留“锚点” (The Clean Root)

清理完成后，根目录应仅保留：
*   `run_system.py` (生产环境入口)
*   `alpha_loop.py` (主逻辑循环)
*   `config.yml` (全局配置)
*   `GEMINI.md`, `AGENTS.md`, `Makefile` (项目导航)
*   `boot_logging.py`, `llm_client.py` (基础公共库)

## 6. 执行安全指引 (Execution Guide)

### 第一步：创建目录结构
```bash
mkdir -p scripts/backtest scripts/research scripts/tools/db scripts/tools/ops scripts/tools/verify scripts/archive
```

### 第二步：物理移动 (示例)
```bash
mv run_backtest_*.py scripts/backtest/
mv *fix.py scripts/tools/db/
mv bug10_real_test/ scripts/archive/
```

### 第三步：运行验证
迁移后，如需运行回测脚本，需在根目录下执行：
```bash
PYTHONPATH=. venv/bin/python scripts/backtest/run_backtest_simple.py
```
*注：由于 CWD（当前工作目录）依然是根目录，脚本内部引用的 `config.yml` 或 `data/` 路径无需修改即可正常工作。*
