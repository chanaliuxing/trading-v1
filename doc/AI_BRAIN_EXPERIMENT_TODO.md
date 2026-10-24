# AI 大脑自动实验系统 TODO 列表

目标：让 AI 大脑像量化研究员一样，自动设计和执行实验，系统性地回答：

- 当前数据 / 标签 / 特征组合下是否存在稳定可交易的 alpha？
- 如果长期 `no_ic`，能否自动尝试不同的问题设定（label / horizon / 特征家族），并得出“暂停 /调整 / 放弃该频率”的结论，而不是盲目 backfill 或人工一条条试。

---

## 一、检测 “长期无 edge” 并进入实验模式

1. **聚合 OOF / HPO 结果到统一上下文**
   - 从 `ml_events.jsonl` / `oof_eval/*.json` 汇总最近 N 次 OOF 的核心指标：
     - `fusion_auc`, `fusion_ic`, `coverage`, `n_bars`。
   - 在 `ml_ctx` 中增加一个 `ml_health_recent` 段，包含：
     - rolling 平均 AUC/IC；
     - 最近多少 job 连续 `auc≈0.5` / `ic≈0 或负`。

2. **定义 “no_edge” 判定规则**
   - 配置化阈值（`config/ml_policy.json` 或 `policies.ml`）：
     - `auc_near_random_thresh`（例如 |AUC−0.5| < 0.02）；
     - `ic_near_zero_thresh`（例如 |IC| < 0.01）；
     - `no_edge_min_jobs`（例如最近 ≥ 20 个 OOF）。
   - 在 Brain 的 stage0 / validator 中导出一个布尔旗标：
     - `derived_flags["no_edge_longrun"] = True/False`。

3. **在 `_execute_ml_actions` 中尊重 no_edge 标志**
   - 当 `no_edge_longrun=True` 时：
     - 降低对 `backfill` / `bulk_backfill` 的信任优先级；
     - 提高 `propose_features` / `fix_config` / `queue_job:hpo_cli/oof` 的权重；
     - 允许 Brain 输出 `pause_ml` / `reduce_window` 等动作时不被简单忽略。

---

## 二、自动设计 Label / Horizon 实验网格

1. **定义安全的实验空间（配置驱动）**
   - 在 `config/ml_policy.json` 或类似文件中增加：
     - `experiment.label_grid`：
       - 例如 `["direction_5m", "direction_10m", "direction_15m", "rel_spy_10m"]`；
     - 对应参数映射到 OOF / 标签系统：
       - `horizon_min`、`label_bps`、`drop_flat`、`target_col`。

2. **Brain 决策：进入实验模式时生成实验任务**
   - 在 `review_ml_async` 的策略层（或 policy/validators）中：
     - 当 `no_edge_longrun=True` 时，输出 `queue_job` 动作列表：
       - 每个 job 对应一个 label/horizon 组合：
         - `{"job_type": "oof", "params": {"symbols": [...], "window_days": X, "horizon_min": H, "label_bps": B}}`；
     - 使用 `fix_config` 或专用 helper 来 patch `oof_policies.json`，确保 OOF 任务采用对应组合。

3. **统一记录实验结果**
   - 设计一个 ClickHouse 表或 JSON 文件用于记录实验：
     - 建议表名：`llm.ai_ml_experiments`。
     - 字段包括：
       - `ts`, `experiment_id`, `label_spec`, `horizon_min`, `label_bps`；
       - `symbols`, `window_days`；
       - `fusion_auc`, `fusion_ic`, `coverage`, `n_bars`；
       - `regime`（可选：由 Brain 或规则标记的市场状态标签）。
   - 在 `nightly_oof_flow.py` 或 OOF 结束事件处理处：
     - 如果当前 job 为实验模式，写一行实验记录。

---

## 三、自动设计特征家族实验（Feature Families）

1. **给特征打标签并分家族**
   - 在 `config/features_catalog.json` 或单独的元数据文件中为候选特征添加家族标签：
     - 例如 `family: "price" | "volume" | "microstructure" | "cross_asset" | "volatility"`。
   - 保持简单：可以先用 `notes` 或新增字段 `family`。

2. **为每个家族定义实验 toggle**
   - 在 OOF / 训练 pipeline 中增加一个简单开关机制：
     - 例如在 policy 中有：
       - `"feature_families": {"price": true, "microstructure": false, ...}`；
     - 训练/OOF 使用时，根据这个开关来决定是否加载对应家族的特征列。

3. **Brain 驱动家族实验**
   - 当 `no_edge_longrun=True` 时，Brain 提出特征家族实验：
     - `fix_config` 动作，修改 `"feature_families"` 开关：
       - 例如：
         - 实验 A：只开 `price`；
         - 实验 B：`price + volume`；
         - 实验 C：`price + microstructure`；
     - 对每个家族组合，queue 一个 OOF 实验 job，并写入实验表。
   - Brain 在后续 review 中读取这些实验结果：
     - 识别哪些家族组合带来了 AUC/IC 的改善；
     - 对“明显拖累表现”的家族（比如某些微观结构特征），主动降权或关闭。

---

## 四、实验结果驱动的 “no edge” 结论与行动

1. **从实验表中归纳结论**
   - 增加一个诊断工具（脚本或 Brain 内部逻辑）：
     - 对最近 N 条实验记录做聚合：
       - 按 label/horizon 组合；
       - 按特征家族组合；
     - 计算每组的实验成功率（AUC≥阈值, IC≥阈值）。
   - 如果所有组合在最近一段时间内都无法通过实验门槛：
     - 将 `derived_flags["no_edge_confirmed"] = True`；
     - 这比简单的 `no_ic` 更强（已系统性试验过）。

2. **Brain 在 no_edge_confirmed 情况下的决策**
   - 当 `no_edge_confirmed=True` 时，Brain 的动作策略改变：
     - 提出 `pause_ml` 或降低风险（通过 `fix_config` 提高阈值、减小杠杆）的动作；
     - 尽量避免再发起高成本的 HPO/回填。

3. **对外反馈（Dashboard / 日志）**
   - 在 `brain_dashboard` / `validate_brain_effectiveness` 中增加新状态：
     - 不再只是 `no_ic`，而是能显示：
       - “已尝试 X 组 label/horizon 组合，Y 组特征家族实验，全部未通过门槛→no_edge_confirmed”。
   - 让你一眼就能看到：这是大脑在“负责地告诉你：当前问题在当前数据上就是没有可交易 edge”，而不是单纯地在原地打转。

---

## 五、落地顺序建议

1. **第一阶段：只做检测与记录，不改变 Brain 行为**
   - 实现：
     - OOF 结果聚合（ml_health_recent）；
     - `tools/test_feature_catalog_sql.py` / `tools/test_data_quality_state.py` 已完成；
     - 实验结果表结构（ClickHouse 或 JSON）。
   - 目标：先把“系统性实验”的数据流打通。

2. **第二阶段：让 Brain 可以显式进入“实验模式”**
   - 在 `review_ml_async` / validators 中加：
     - `no_edge_longrun` 判断；
     - 输出 label/horizon/feature family 实验 job（`queue_job`, `fix_config`）。

3. **第三阶段：用实验结果反向调控 Brain 行为**
   - 从实验表中总结“有效组合”与“无效组合”；
   - 自动调整：
     - 特征家族开关；
     - Label/horizon 设置；
     - 在极端情况下输出“no_edge_confirmed→pause_ml”的决策。

4. **第四阶段：把这些状态暴露在 Dashboard / 日志**
   - 更新 `tools/brain_dashboard.py` 等工具；
   - 让你和 Brain 都能直观看到：
     - 它做了哪些实验；
     - 哪些方向有希望；
     - 什么时候该“暂停这条线，不再硬挖”。

