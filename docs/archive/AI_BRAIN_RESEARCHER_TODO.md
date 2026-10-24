# AI Brain “量化研究员”升级 TODO

> 目标：让 AI Brain 具备“研究员”能力——能看清环境、用工具、自我试错与修正，而不是盲写 SQL + 盲撞 ClickHouse。

说明：
- `[x]` 为本仓库已实现的事项（本次会话内或之前完成且已验证）。
- `[ ]` 为尚未完成的 TODO，可按优先级逐步落地。

---

## 0. 紧急止血层（已完成）

- [x] **防止 AI 生成的特征 SQL 直接打爆 ClickHouse**
  - [x] 在 `tools/feature_build.py` 中过滤不兼容 SQL 片段：
    - `ROWS BETWEEN` / `stddevPop` / `stddevSamp` / `movingAverage`
    - 不存在的 microstructure 列：`buy_volume`/`sell_volume`/`total_volume`/`vwap_ma_5`/`volatility_30`/`avg_spread`/`bid_ask_imbalance`/`avg_daily_spread` 等
  - [x] `tools/test_feature_catalog_sql.py` 与 runtime 过滤规则对齐，校验 `config/features_catalog.json` 不再引用不存在列。
  - [x] 用 `tools/feature_build.py --catalog config/features_catalog.json` 在真实 ClickHouse 上验证：当前 candidate 特征能成功创建 `trading.features_1m_candidate_v*` 视图。

- [x] **放宽 propose_features 配额，避免一晚就被“禁言”**
  - [x] 在 `scripts/ai_trader/control_event_consumer.py::_check_daily_quota` 中，将：
    - 一般动作日配额从 `999` 提升为 `9999`；
    - `propose_features` 日配额提升为 `20000`。

- [x] **避免 Brain Loop 在失败时疯狂重试打爆端口**
  - [x] 在 `tools/ml_brain_loop.py` 主循环外层加入 `try/except + 指数退避`：
    - 第一次失败 sleep 5s，随后每次翻倍，最大 60s；
    - 一旦某轮成功，重置 backoff。

- [x] **降低验证器对 numbers_used 的误杀率**
  - [x] 在 `scripts/ai_trader/brain/policy/validators.py::validate_llm_review_output` 中：
    - 保留 `action` 白名单校验；
    - 将 `numbers_used` 从“必须出现在 allowed_numbers，否则报 fabricated_number_detected”改为：
      - 仅做类型 sanity check（能否转成 float），不再因为“数字不在上下文”而直接拒绝。

---

## 1. 感知层（Perception Layer）——让大脑“看见”系统

### 1.1 数据库 Schema 注入

- [x] 在 `scripts/ai_trader/control_event_consumer.py` 构建 `ml_ctx` 时，增加 `schemas` 字段：
  - [x] 使用 `libs.db.ch_pool.get_client()` 查询：
    - `DESCRIBE TABLE trading.features_1m`
    - `DESCRIBE TABLE trading.features_5m`
    - （当前通过 ClickHouse introspection 获取真实 schema，等价于 Gemini 方案中从 `_FEATURE_INSERT_COLUMNS` 暴露列名）
  - [x] 将列名 + 类型摘要写入：
    - `ml_ctx["schemas"]["trading.features_1m"] = [{"name": ..., "type": ...}, ...]`
    - `ml_ctx["schemas"]["trading.features_5m"] = [...]`
  - [x] 在 Brain prompt 中明确说明：“只能使用这些 schema 中存在的列名；不要凭空造列。”（已在 `CONTROL_PROMPT` / `ML_RESULT_REVIEW_PROMPT` 中加入约束 + 工具说明）

### 1.2 实验与健康状态注入（已部分完成）

- [x] 最近 OOF 健康状态：
  - [x] `ml_ctx["ml_health_recent"]`：包含 `fusion_auc_mean` / `fusion_ic_mean` / `coverage_mean` / `no_edge_simple`。
- [x] OOF 实验摘要：
  - [x] `ml_ctx["experiments_summary"]`：`total_experiments` / `mean_auc` / `mean_ic` / `no_edge_confirmed`。
  - [x] `ml_ctx["experiments_combos"]`：每个 `(window_days, horizon_min, label_bps)` 的效果 + `best_combo`。
- [ ] 数据可用性 / gap 摘要：
  - [x] `ml_ctx["data_availability"]`：当前已有的 `features_1m_with_labels` 历史天数（已通过 docker exec 查询实现）。
  - [x] 增强 gap 摘要：在 `ml_ctx["data_gaps"]` 中提供最近窗口的缺失情况汇总（每个 symbol 的缺失天数、最早/最晚缺口日期），供 Brain 避免“盲目 backfill 3 天”。

---

## 2. 工具层（Tools Layer）——给大脑一套“研究工具”

> 目标：在不直接开放任意 SQL 的前提下，提供受控的工具函数，让 Brain 能自查 schema、dry-run 特征表达式、查看 job 状态，而不是盲写再等日志。

### 2.1 SQL / Schema 工具

- [x] 新建 `scripts/ai_trader/brain/tools_sql.py`，提供：
  - [x] `inspect_table_schema(table: str) -> dict`：
    - 用 `ch_pool.get_client().query("DESCRIBE TABLE ...")` 返回列名/类型；
    - 支持 `trading.features_1m` / `trading.features_5m` / `trading.features_1d_prev` / 自定义表；
    - 结果写入 `runs/ai_trader/<date>/brain_tools/inspect_schema_<table>.json`，供 Brain / 人类查看。
  - [x] `dry_run_feature_expr(expr: str, sources: List[str]) -> dict`：
    - 在受控环境构造一个最小 `SELECT`：
      - FROM `trading.features_1m` AS f；
      - 可选 JOIN `trading.features_5m` AS m5 / `trading.features_1d_prev` AS d；
      - SELECT `(expr) AS value LIMIT 10`；
    - 先进行 schema 级别校验（别名列必须存在于对应表中），再执行 SQL；
    - 捕获 ClickHouse 报错（`UNKNOWN_IDENTIFIER` / `SYNTAX_ERROR` 等），返回结构化结果：
      - `{ "ok": False, "error_type": "...", "error_message": "...", "expr": expr }`
      - 成功则返回示例值预览 `rows_preview`。
    - 写入 `runs/ai_trader/<date>/brain_tools/dry_run_<suffix>_<hash>.json`。

### 2.2 Job / Experiment 工具

- [ ] 新建 `scripts/ai_trader/brain/tools_ml.py`，封装：
  - [ ] `list_recent_jobs(job_type: str, limit: int) -> list`：
    - 解析 `runs/ai_trader/<date>/ml_events.jsonl`，列出最近的 job_id / 类型 / 状态。
  - [ ] `get_job_details(job_id: str) -> dict`：
    - 回查对应事件 + artifacts（例如 features_build/status.json）。
  - [ ] `list_valid_experiment_ids() -> list`：
    - 基于 `ml_experiments.jsonl` 或 ClickHouse 表，列出合法的实验 ID / 版本号，供 Brain 引用。

> 短期：这些工具主要用于人类/离线调试；中期：在 Brain Loop 中通过“工具调用协议”暴露给 LLM。

---

## 3. Brain 主循环（Agentic / ReAct）——从“一次问答”升级为“多轮研究员”

### 3.1 多轮决策接口设计

- [ ] 在 `scripts/ai_trader/brain/brain.py` 中，设计一个高层接口：
  - [ ] `async def research_step(context: dict) -> dict`：
    - 输入：`ml_ctx`（包含 schemas / experiments / data_availability 等）；
    - 输出：`{"next_actions": [...], "numbers_used": [...], "tool_calls": [...]}`。
- [ ] 设计 tool-call 协议（JSON 约定）：
  - [ ] LLM 可在输出中请求调用工具，例如：
    ```json
    {
      "tool_calls": [
        {"tool": "inspect_table_schema", "args": {"table": "trading.features_5m"}},
        {"tool": "dry_run_feature_expr", "args": {"expr": "m5.tick_imbalance / nullIf(f.avg_trade_size,0)", "sources": ["m5"]}}
      ]
    }
    ```

### 3.2 控制层集成（control_event_consumer）

- [x] 在 `_handle_ml_completed` 中，引入一个“多轮 Brain 回合”的框架（当前直接基于 `review_ml_async` 实现，而不是单独的 `research_step`）：
  - [x] 初始轮：构造 `ml_ctx`，调用 `o.brain.review_ml_async(ml_ctx_cur)`；
  - [x] 如果返回 `tool_calls` 非空：
    - [x] 在 host 侧执行这些工具函数（`inspect_table_schema` / `dry_run_feature_expr`），将结果写入 `ml_ctx_cur["tool_results"]`；
    - [x] 再次调用 `review_ml_async`，附带新的 `ml_ctx_cur`；
    - [x] 限制最大回合数（当前为 3），防止无限循环；
  - [x] 最后一轮才取 `next_actions`，经过 `validate_llm_review_output` 和 `_translate_brain_actions`，再走 `_execute_ml_actions` 流程。

### 3.3 Prompt 更新（让 Brain 正确使用新能力）

- [x] 更新 `CONTROL_PROMPT` / `ML_RESULT_REVIEW_PROMPT` 中的相关段落：
  - [x] 明确告知：
    - 上下文中有 `schemas` / `experiments_summary` / `data_availability` / `stage0` 等可用；
    - 不要凭空发明列名或 ID，优先使用工具或给定 schema（已在 prompt 中加入“永远不要发明列名，使用 inspect_table_schema/dry_run_feature_expr 工具”说明）；
    - 对新特征表达式，先请求 `dry_run_feature_expr`，看到 ClickHouse 报错后再修正，并在下一轮 review 中利用 `INPUT.tool_results`；
    - （job/experiment ID 工具仍在规划中，后续通过 `tools_ml` 提供 `list_recent_jobs` / `list_valid_experiment_ids` 再更新 prompt）。

---

## 4. 特征 / SQL 安全与 DSL 方向

### 4.1 加强特征安全层（在 SQL 之上再加一层 DSL）

- [x] 设计一个轻量 DSL 表达 `features`，例如：
  - `ret_5 / ATR(14)` → 映射到 ClickHouse SQL：
    ```sql
    f.ret_5 / nullIf(f.atr_14, 0)
    ```
- [x] 在 `libs/features`（或新建 `ai/feature_dsl`）中：
  - [x] 定义一套允许的原子操作（目前支持 ratio / ratio_minus_1 / log1p / product / sum / abs，后续可扩展）；
  - [x] 实现 `dsl_to_sql(dsl_spec: dict) -> str`（`libs/features/feature_dsl_sql.py`），统一生成 ClickHouse 兼容 SQL。
- [ ] 在 Brain 与 host 之间协定：
  - [ ] Brain 首选输出 DSL（结构化 JSON），而不是原始 SQL：
    ```json
    {
      "name": "ret_5_vol_adj",
      "dsl": {"op": "div", "lhs": "f.ret_5", "rhs": {"op": "null_if_zero", "arg": "f.atr_14"}},
      "dtype": "Float32"
    }
    ```
  - [ ] Host 侧用 DSL 转换器生成 SQL，再走 `dry_run` + `feature_build` 流程。

> 注：DSL 是中期目标，短期可以先用“schema + dry_run + banned_fragments”把 SQL 时代稳定住。

---

## 5. 监控与可视化（Dashboard）

- [ ] 扩展 `tools/brain_dashboard.py`：
  - [ ] 增加：
    - 每日 `validation_failed` 计数按原因拆分（特别是 `fabricated_number_detected` / `action_not_allowed` 等）；
    - 连续 feature_build 失败次数及最后错误；
    - Brain 工具调用统计（多少次 inspect_schema / dry_run 成功 / 失败）。
- [ ] 为 “研究员模式” 增加单独区块：
  - [ ] 展示最近 N 次 `feature_experiment` / `feature_build` 的 AUC/IC 改善情况；
  - [ ] 统计哪些 feature families / label 组合产生了正 IC / 正 AUC。

---

## 6. 渐进落地顺序建议

1. **已经完成 / 今天已做**：
   - 特征 SQL 止血（banned_fragments + feature_build 实测）。
   - 验证器放宽 `numbers_used`（不再动不动 fabricated_number_detected）。
   - Brain Loop 指数退避（避免端口耗尽）。
   - propose_features 配额上调。

2. **下一步（可在一两个晚上内完成）**：
   - [ ] 感知层：往 `ml_ctx` 注入 `schemas`（features_1m/5m）；
   - [ ] 工具层：实现 `tools_sql.inspect_table_schema` + `dry_run_feature_expr`，先给人类用；
   - [ ] Prompt 微调：明确告知 Brain 不要凭空造列名，必须在 schema 内选择。

3. **中期（1–2 周）**：
   - [ ] 引入工具调用协议 + 多轮 `research_step`；
   - [ ] 将 dry_run / inspect_schema / list_jobs 等工具真正暴露给 LLM 使用；
   - [ ] 初始版本的 Feature DSL + SQL 生成器。

4. **后续优化**：
   - [ ] 整合 ClickHouse 实验表（`llm.ai_ml_experiments`）到 Brain 上下文，用长期实验结果指导策略；
   - [ ] 增加“无 edge 时自动收手”的决策逻辑（例如多日 `no_edge_confirmed` 时自动建议 pause_ml 或切换任务）。
