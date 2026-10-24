## Option ML Intraday Long-Only Plan (SPY/QQQ + Tech7, 1m, Single-Trade 0.5%, Daily 1%)

### Scope & Goal
- Universe: SPY, QQQ, and 7 tech mega-cap stocks.
- Timeframe: 1-minute bars (RTH).
- Strategy: Intraday **long options only** (buy calls/puts or debit spreads), no naked short premium.
- Risk: max **0.5%** of account equity per trade (initial), **1%** max daily drawdown for this strategy.
- Control: Fully observable and controllable by the AI Brain (enable/disable, thresholds, model version, risk caps).

### Risk/Reward Alignment (Target vs Budget)
- Per-trade risk budget (r): ~0.5% of equity.
- To avoid forcing very high gamma trades, the **per-trade profit target** should be smaller than the risk budget:
  - Recommended: per-trade target 0.15%–0.30% of equity (corresponding to roughly +30%–60% premium on the option position).
  - Daily target 0.5%–1% is achieved by **multiple high-quality trades** (e.g. 2–4), not a single 200% premium trade.
- Controls must enforce:
  - Max trades per day and/or max consecutive losses.
  - Hard daily drawdown cap (1%) that stops new trades regardless of model signals.

### Sign-off (Team Confirmation)
- Status: pending
- Owner:
- Date:
- Notes:

---

## Phase 0 – Interfaces & Control Plane

**Goal:** Define the control and telemetry interfaces so the AI Brain can manage the option ML strategy without touching low-level code.

### Responsibilities
- Define core tables/APIs:
  - `trading.option_intraday_signals`: model outputs (signals).
  - `trading.option_intraday_perf`: realized performance and per-trade stats.
  - `trading.option_ml_models`: model registry + metrics.
  - `trading.option_ml_controls`: AI Brain control plane (enable flags, thresholds, risk caps).
- Add a structure catalog to make multi-leg options enumerable and controllable:
  - `trading.option_structure_catalog`: structure templates + leg rules + defaults.
- Agree on how execution and AI Brain interact:
  - Execution only reads `signals` + `controls`.
  - AI Brain only writes to `controls` and reads `models` + `perf`.

### Deliverables
- Table schemas for the 4 core tables.
- Documentation of control semantics (what each control flag/field means).

---

## Phase 1 – Data & Labels

**Goal:** Build a labeled 1m dataset in ClickHouse that connects **underlying behaviour** and **structure-level option trade outcomes**, enabling multi-leg ML.

### Phase 1a – Underlying-Based Labels (MVP, already implemented)
- Horizon `H` (initial): 30 minutes.
- For each symbol and each 1m timestamp `t` (RTH-only):
  - Direction label `y_dir ∈ {Up, Down, Flat}` based on future return:
    - `ret_H = (P_{t+H} - P_t) / P_t`.
    - Up if `ret_H > +k%`, Down if `ret_H < -k%`, else Flat.
  - Move label `y_move ∈ {Move, NoMove}` based on future range:
    - `rng_H = (High_max - Low_min) / P_t` over `[t, t+H]`.
    - Move if `rng_H > m%`, else NoMove.
- These labels live in `trading.features_1m_labeled` and are suitable for a first pass on **direction + move** models.

### Phase 1b – Option PnL-Based Trade Labels (recommended upgrade)
- Fix a concrete **contract selection rule** (same as Phase 4 execution, e.g. DTE 5–10 days, delta ~0.3–0.4, liquidity filters).
- For each 1m decision point `t` and symbol:
  - Simulate entering the chosen option at `t`:
    - Entry price: Ask (or mid + slippage).
  - Simulate intraday path over `[t, t+H]`:
    - Exit at:
      - Take-profit TP on premium（如 +25%/+40%）;
      - Stop-loss SL on premium（如 -30%/-40%）;
      - Time stop at `t+H` if TP/SL 都未触发.
  - Record trade outcome labels, for example:
    - `y_win ∈ {0,1}`：是否在 H 内触发 TP。
    - `y_rr`：预期 R 值 / 收益（回归）。
    - `y_best_action ∈ {CALL, PUT, NO_TRADE}`：方向与是否值得出手。
- These labels directly reflect **option PnL** under realistic cost assumptions and should replace/augment underlying-based labels when the MVP pipeline is stable.

### Note on k / m Thresholds
- Instead of hard-coded `k`/`m` (e.g. 0.15%/0.4%) per symbol, use **data-driven quantiles**:
  - `k = percentile(|ret_H|, q_ret)` over a rolling window (e.g. 60–90 days, RTH only).
  - `m = percentile(rng_H, q_rng)` similarly.
- This stabilises label balance across symbols (SPY vs NVDA) and regimes（高/低波动），reducing drift.

### Option Quotes / Chain Layer (required for multi-leg)
- Introduce a 1m option chain layer in ClickHouse, e.g.:
  - `trading.options_quotes_1m` (or `trading.options_chain_1m`)
    - Columns: underlying, ts, expiry, strike, right, DTE, bid/ask/mid, volume, open_interest, IV, delta, spread_bps, etc.
  - This table supports:
    - Phase 1b structure-level outcome simulation.
    - Phase 2 structure-level feature engineering.
    - Phase 4 structure selection and liquidity checks.

### Responsibilities
- Phase 1a:
  - Build and maintain `trading.features_1m_labeled` based on `bars_1m_ibkr` (already done for initial horizon).
  - Ensure labels and thresholds are stored explicitly（ret_threshold/range_threshold）。
- Phase 1b (upgrade):
  - Design and implement `options_quotes_1m` or equivalent data source.
  - Implement backtestable option trade outcome labeling（TP/SL/time-stop）。
  - Ensure contract selection rule is deterministic and shared with execution.

### Deliverables
- Phase 1a:
  - `trading.features_1m_labeled` populated for SPY/QQQ + Tech7 with ret/range labels.
- Phase 1b:
  - `options_quotes_1m` (or equivalent) populated.
  - Option PnL-based labels and associated schema for training.

---

## Phase 2 – Model Training & Registry

**Goal:** Train baseline models and register them so AI Brain can reason about versions and performance.

### Models
- `clf_dir`: multi-class classifier for `y_dir` (Up, Down, Flat).
- `clf_move`: binary classifier for `y_move` (Move, NoMove).
- **Multi-leg**: `clf_structure` / `rank_structure` for structure selection.
- Implementation: tree-based models (LightGBM/XGBoost/CatBoost) for robustness.

### Training Script
- New script: `ml/train_intraday_long_options.py`:
  - Pull labeled data from CH (`features_1m_labeled`) for a given date range.
  - Split train/validation by time (no random shuffle).
  - Train `clf_dir` and `clf_move`.
  - Evaluate on validation set:
    - Conditional hit rate when signals are “strong enough”.
    - Coverage rate (fraction of minutes with valid signals).
    - If using PnL-based labels: simple PnL proxy metrics per trade.
  - Apply probability calibration（Platt / isotonic 等）以保证 `p_up` / `p_move` 在不同模型版本之间具有可比性。
  - Choose reasonable thresholds (`theta_dir`, `theta_move`) and store them with the model metadata.
  - Multi-leg upgrade:
    - **Route A (classification)**: `y_best_structure ∈ {NO_TRADE, SINGLE_CALL, SINGLE_PUT, VERTICAL_DEBIT_CALL, ...}`.
    - **Route B (ranking)**: one row per structure candidate, label = realized R / TP hit, output `edge_score`.
    - Register structure set and selection policy (Top-N, min score).

### Model Registry
- `trading.option_ml_models`:
  - model_version, train_start, train_end, universe, feature_set, horizon_min.
  - validation metrics: accuracy, precision/recall for Up/Down, Move, coverage, simulated PnL proxy.
  - suggested thresholds: `theta_dir`, `theta_move` for production.
  - `structure_set`, `selection_policy`, `cost_model` (bid/ask + slippage assumptions).

### Deliverables
- `ml/train_intraday_long_options.py` skeleton.
- ClickHouse table DDL for `trading.option_ml_models`.

---

## Phase 3 – Online Inference & Signal Generation

**Goal:** Run models in real-time, generate structured signals into ClickHouse, and expose them to execution + AI Brain.

### Service Responsibilities
- New service: `services/options/ml_intraday_signal_service.py`:
  - Every minute:
    - Read freshest features for universe from `features_1m`.
    - Load active model version + thresholds from `option_ml_controls` / `option_ml_models`.
    - Generate structure candidates using `option_structure_catalog` + controls.
    - Score each structure (`edge_score`) and rank within the same minute.
    - Only output Top-N structures (N from controls).
  - Write signals into `trading.option_intraday_signals` with structure fields (`structure_id`, `legs_spec_json`, `edge_score`, `rank`).

### Signal Schema (high level)
- `ts, symbol, structure_id, structure_type, legs_spec_json`
- `edge_score, rank, max_loss_est, net_debit_est`
- `p_up, p_down, p_move, horizon_min, model_version, reason_code`

### Deliverables
- Skeleton for `ml_intraday_signal_service.py`.
- DDL for `trading.option_intraday_signals`.

---

## Phase 4 – Execution & Risk (0.5% per trade, 1% per day)

**Goal:** Turn signals into option orders with strict risk controls; all configurable via controls table.

### Execution Adapter (multi-leg)
- New adapter: `services/exec/options_long_adapter.py`:
  - Poll `option_intraday_signals` for actionable **structure** signals.
  - Read `option_ml_controls` for risk parameters and allowed structures.
  - For each signal:
    - If strategy is disabled (controls), skip.
    - Compute per-trade budget:
      - `budget = max_trade_risk_pct * equity`.
    - Build legs from `legs_spec_json` (or from structure params + chain snapshot).
    - Compute net debit/credit and max loss:
      - Debit structures: `max_loss = net_debit * 100 * contracts`.
      - `contracts = floor(budget / (net_debit * 100))`; if size < 1, skip.
    - **Submit a combo order** (single order with all legs) to avoid legging risk.
    - Enforce daily DD, max trades per day, max consecutive losses.
  - Manage exits:
    - Hard stop-loss on premium（如 -30% / -40%）。
    - Time stop: close after horizon_min (e.g., 30–45min).
    - Profit target on premium（如 +30% / +50%）。
    - Cooldown window after a loss.

### Performance Tracking
- Table `trading.option_intraday_perf`:
  - Base fields: symbol, direction, entry_ts, exit_ts, model_version, p_up/p_down/p_move at entry, size, entry/exit price, gross/net PnL, holding_time, reason_exit.
  - Multi-leg fields: `structure_id`, `legs_json`, `net_debit_entry/exit`, `max_loss_defined`, `fees`, `slippage_est`.

### Deliverables
- Skeleton for `options_long_adapter.py`.
- DDL for `trading.option_intraday_perf`.

---

## Phase 5 – AI Brain Management & Autonomy

**Goal:** Let the AI Brain manage this ML strategy as a first-class citizen: enable/disable, adjust thresholds, govern risk, and respond to performance.

### Control Plane (`option_ml_controls`)
- Fields (example):
  - `enabled` (bool): whether intraday option ML is active.
  - `active_model_version`: model version to use for inference.
  - `theta_dir`, `theta_move`: minimum probabilities required for action.
  - `max_trade_risk_pct`, `max_daily_dd_pct`.
  - `max_positions_per_symbol`, `max_total_positions`.
  - `mode` (e.g., paper/live/sandbox).
- Multi-leg additions:
  - `allowed_structures` (list / JSON).
  - `structure_param_caps_json` (DTE/width/delta caps per structure).
  - `max_leg_count`, `top_n`, `cooldown_min_after_loss`.

### AI Brain Tasks
- Periodic evaluation job (daily/weekly):
  - Summarize performance from `option_intraday_perf` per model_version:
    - hit rate, avg R/R, max drawdown, daily DD distribution, per-symbol stats, coverage.
  - Write summary back into `option_ml_models` and/or a markdown report.
- Decision logic:
  - If performance degrades beyond thresholds:
    - AI Brain can:
      - lower risk caps, raise thresholds, limit to subset of symbols, or disable strategy (set `enabled=false`).
  - If new model shows superior performance:
    - AI Brain can switch `active_model_version` after sandbox validation.

### Retraining & Deployment
- Periodic retrain (e.g., every 7–14 days):
  - Re-run `train_intraday_long_options.py` using recent data.
  - Register new model in `option_ml_models` with metrics.
  - Optionally run a shadow evaluation phase.
- AI Brain chooses when to promote a new model to `active_model_version`.

### Deliverables
- DDL for `trading.option_ml_controls`.
- A small management script (or AI Brain playbook) for:
  - Generating performance summaries.
  - Proposing/performing control updates.

### Structure Versioning Rules
- `structure_id` is stable across versions (do not reuse for incompatible templates).
- `version` increments on any template change (legs or params).
- Only one version of a `structure_id` should be `enabled=1` at a time.
- Templates are append-only: create a new row for new versions, never update in place.

---

## Phase 6 – Iterative Enhancements (Post-MVP)

**Ideas (not required for initial rollout):**
- Start with `SINGLE_CALL/PUT` + `VERTICAL_DEBIT_CALL/PUT`, then expand to:
  - Calendar / Diagonal (more IV/term-structure sensitivity).
  - Butterfly / Condor (larger structure space).
- Add 60-minute horizon models in parallel with 30-minute models.
- Keep structures gated by AI Brain controls and liquidity constraints.

---

## Detailed TODO Checklist

Use this checklist to track implementation progress. Items are grouped by phase.

### Phase 0 – Interfaces & Control Plane

- [ ] P0-01 确认本计划文档内容并在团队内达成共识（Scope/Phase/表名）。
- [x] P0-02 明确 ClickHouse 实例与库名（默认 `trading`）以及环境（本地/远程）。
- [x] P0-03 在架构层确认：AI Brain 仅通过 `trading.option_ml_controls` 管理策略，不直接调用训练/执行代码。
- [x] P0-04 确认 AI Brain 可以读取：`option_intraday_signals` / `option_intraday_perf` / `option_ml_models` / `option_ml_controls`。
- [x] P0-05 确认策略名约定（如 `intraday_options_long`）并在文档和代码中统一。
- [x] P0-06 设计并创建 `trading.option_structure_catalog`（结构字典 + legs 模板 + 参数默认值）。
- [x] P0-07 定义结构模板版本管理规则（`structure_id` / `version` / `enabled` 管控）。

### Phase 1 – Data & Labels (`features_1m_labeled`)

Schema & 创建：
- [x] P1-01 审阅 `sql/option_ml_schema.sql` 中 `trading.features_1m_labeled` 结构，确认字段满足需求。
- [x] P1-02 在目标 ClickHouse 上执行 `sql/option_ml_schema.sql`（或其中 `features_1m_labeled` 部分）创建表。
- [x] P1-03 在 `doc/CLICKHOUSE_SCHEMA.md` 中补充 `features_1m_labeled` 的表结构说明（可选）。
- [x] P1-03a 创建 `trading.options_quotes_1m` / `trading.options_chain_1m`（期权链报价层，含 Greeks/流动性字段）。

标签逻辑：
- [x] P1-04 确定 horizon 初始值（如 `H=30` 分钟），并决定是否支持多 horizon（30/60）。
- [x] P1-05 使用历史分位数为每个 symbol 动态校准方向阈值 `ret_threshold`（如 |ret_H| 的 60%–70% 分位数），并写入配置/脚本默认参数。
- [x] P1-06 使用历史分位数为每个 symbol 动态校准振幅阈值 `range_threshold`（如 rng_H 的 60%–70% 分位数），并写入配置/脚本默认参数。
- [x] P1-07 明确 Universe 列表（SPY, QQQ + Tech7），在配置源（YAML/JSON/表）中固定。

标签生成实现（ClickHouse / Python）：
- [x] P1-08 编写 ClickHouse SQL：基于 1m bars（如 `bars_1m_ibkr`）计算给定 `H` 的 `ret_h` 和 `rng_h`。
- [x] P1-09 在 SQL 中根据 `ret_h` 生成 `y_dir ∈ {-1,0,1}`（Down/Flat/Up）。
- [x] P1-10 在 SQL 中根据 `rng_h` 生成 `y_move ∈ {0,1}`（NoMove/Move）。
- [x] P1-11 将上述 SQL 封装为可重复执行的脚本（按日期区间增量写入 `features_1m_labeled`）。
- [x] P1-12 新建 `tools/build_features_1m_labeled.py`，在 Python 中从 CH 拉 1m 数据计算标签并回写 `features_1m_labeled`。
- [x] P1-13 用少量历史数据运行标签生成脚本，检查 y_dir / y_move 分布是否合理（不极端失衡），并确认 ret/rng 与标签逻辑严格对应。

质量检查：
- [x] P1-14 编写简单查询/Notebook，按 symbol 检查 `features_1m_labeled` 标签分布。
- [x] P1-15 确认仅包含 RTH 时间，且不跨越收盘后的窗口。
- [x] P1-16（Phase 1b）实现期权 PnL 结果标签（TP/SL/time-stop），并验证模拟交易曲线与真实期权行情一致性。
- [x] P1-17（Phase 1b）实现结构级标签：`y_best_structure` / `y_hit_tp` / `y_maxdd`（按 catalog 结构回放生成）。
- [x] P1-18（Phase 1b）确保结构生成规则可复现（同一 t、同一规则必选同一结构）。

### Phase 2 – Model Training & Registry

Schema & Registry：
- [x] P2-01 审阅 `sql/option_ml_schema.sql` 中 `trading.option_ml_models` 结构，确认字段满足指标与阈值记录需求。
- [x] P2-02 在 ClickHouse 上确认 `option_ml_models` 已创建并可 INSERT/SELECT。
- [x] P2-03 在文档中补充 `option_ml_models` 各字段含义与示例。

训练脚本（`ml/train_intraday_long_options.py`）：
- [x] P2-04 审阅训练脚本，确认数据来源为 `features_1m` + `features_1m_labeled` 且 JOIN 条件正确。
- [x] P2-05 明确特征列集合，在 `_prepare_xy` 中显式排除非特征列（symbol/ts/标签/辅助列）。
- [x] P2-06 决定使用的模型类型（RandomForest / LightGBM / XGBoost），并在脚本中固定实现。
- [x] P2-07 在 `_fetch_labeled_data` 中增加 Universe 和 horizon 的行数检查，数据不足时给出清晰日志。

训练 gate 与评估：
- [x] P2-08 根据期权场景调整 `TrainingGateConfig`（`ml/training_gate.py`），包括 `min_rows` / `min_label_pos_frac` / `min_label_neg_frac`。
- [x] P2-09 如有需要，对 y_move 也增加单独的 gate 校验。
- [x] P2-10 在 `_evaluate_models` 中增加更细指标（Up/Down precision/recall、Move AUC），并写入 metrics。
- [x] P2-11 设计初始 `theta_dir` / `theta_move` 的选择流程（简单 grid search 或手动多组试验）。
- [x] P2-11a 设计结构模型训练格式（分类 or 排序），确定标签（`y_best_structure` 或 `y_rr`）。
- [x] P2-11b 生成结构候选训练集（同一 t 多结构样本）并验证覆盖率与分布。
- [x] P2-12 增加概率校准步骤（Platt / isotonic），并将校准后概率分布的稳定性写入 `option_ml_models` meta。

模型注册与持久化：
- [x] P2-13 确定模型存储路径规范（如 `models/{model_version}_clf_dir.pkl`），并在训练脚本中实际调用 joblib/lightgbm 保存两个模型。
- [x] P2-15 运行一次训练脚本，验证 `option_ml_models` 中成功写入一条记录（包含 config meta）。

### Phase 3 – Online Inference & Signals

Schema：
- [x] P3-01 审阅 `trading.option_intraday_signals` 结构，确认字段满足当前与未来需求（action/概率/阈值/标签）。
- [x] P3-02 在 ClickHouse 上确认 `option_intraday_signals` 已创建且 TTL/索引设置合理。
- [x] P3-03 如有需要，扩展 schema 以显式包含 `edge_score` 与 intra-minute `rank` 字段（或在 `tags` JSON 中标准化这两个键）。
- [x] P3-03a 扩展 signals schema 支持 `structure_id` / `structure_type` / `legs_spec_json` / `net_debit_est` / `max_loss_est`。

服务骨架（`services/options/ml_intraday_signal_service.py`）：
- 线下核对数据流：`option_ml_controls` → `features_1m` → 模型 → `option_intraday_signals`。
- [x] P3-04 审阅整体流程（controls → features_1m → 模型 → signals）是否与文档一致。
- [x] P3-05 确认 `_load_controls` 中策略名与控制表一致（如 `intraday_options_long`）。
- [x] P3-06 根据模型存储方案实现 `_load_models`（joblib 路径 / 自有 ModelStore）。
- [x] P3-07 调整 `_fetch_latest_features` 的 SQL，使之与实际 `trading.features_1m` 表结构兼容。
- [x] P3-08 在 `_prepare_X` 中保证特征列与训练阶段完全一致（避免 train/online mismatch）。

决策 & 写入：
- [x] P3-09 根据回测结果校准 `BUY_CALL` / `BUY_PUT` / `NO_TRADE` 的决策逻辑和 `delta` 阈值。
- [x] P3-10 在决策层计算 `edge_score` 与 intra-minute rank，并只为 Top-N 标的输出 actionable 信号。
- [x] P3-11 设计并实现 `reason_code` / `tags` 的基础枚举，并在决策时填入（包括因风险/排序被过滤的原因）。
- [x] P3-12 以测试模式运行一次 `run_once()`，检查 `option_intraday_signals` 中写入记录结构与数值。

部署 & 监控：
- [x] P3-13 将 `main()` 循环接入现有的服务框架或调度系统，而不是直接运行 while True。
- [x] P3-14 为服务增加基本监控（每分钟信号数、NO_TRADE 比例、Top-N 使用率、异常日志告警）。

### Phase 4 – Execution & Risk (0.5% per trade, 1% per day)

Schema：
- [x] P4-01 审阅 `trading.option_intraday_perf` 结构，确认是否需要增加账户 ID、多策略标签等字段。
- [x] P4-02 在 ClickHouse 上确认该表已创建并可写入。

风险控制 & 控制表：
- [x] P4-03 审阅 `trading.option_ml_controls` 默认值（`max_trade_risk_pct=0.005`, `max_daily_dd_pct=0.01`）是否符合当前风险偏好。
- [x] P4-04 在 `option_ml_controls` 中插入一条初始配置（enabled=0，先用于测试）。
- [x] P4-05 在 controls 中增加 `stop_loss_pct_on_premium` / `take_profit_pct_on_premium` / `cooldown_min_after_loss` / `max_trades_per_day` / `max_consecutive_losses` 等字段，并更新 DDL。

执行适配器（`services/exec/options_long_adapter.py`）：
- [x] P4-06 审阅适配器结构，确认入口 `process_signals_once` 与现有执行框架兼容。
- [x] P4-07 实现 `_select_option_contract`：从期权链/行情源选择 DTE 5–10 天、Delta~0.3–0.4、点差与流动性达标的合约（写成纯函数，可回放）。
- [x] P4-08 将 `_get_today_realized_pnl` 替换为真实日内 PnL 数据源（或确认以 `option_intraday_perf` 为准）。
- [x] P4-09 将硬编码的 `equity = 100_000.0` 替换为实际权益数据源（风控模块/账户服务）。
- [x] P4-10 在 “TODO: insert order via existing exec infrastructure” 处接入现有 IB / 执行栈（提交订单、追踪成交）。
- [x] P4-11 新建平仓/更新 PnL 任务：根据成交与实时价格更新 `option_intraday_perf` 的 exit_ts/exit_price/PnL/exit_reason`与`exit_reason`（TIME_STOP/TP/SL/MANUAL 等）。
- [x] P4-11a 将执行逻辑升级为 combo order（多腿同时成交），禁用默认 legging。
- [x] P4-11b 在 perf 记录中写入 `structure_id`、`legs_json`、`net_debit_entry/exit`、`max_loss_defined`、`fees`、`slippage_est`。

验证 & 回放：
- [x] P4-12 在模拟 / paper 环境中用一段历史数据回放，验证单笔风险约 0.5%、日内回撤 CAP≈1% 是否生效。
- [x] P4-13 编写统计脚本/Notebook，查看 `option_intraday_perf` 的日内回撤分布，验证 risk cap 效果，并输出“coverage vs PnL vs DD” 曲线供 AI Brain 使用。

### Phase 5 – AI Brain Management & Autonomy

控制表使用：
- [x] P5-01 为 AI Brain 编写使用说明：如何读写 `option_ml_controls`（字段语义与更新规则）。
- [x] P5-02 在 AI Brain 的配置/skills registry 中登记该策略名称及其控制表路径。
- [x] P5-02a 在 `option_ml_controls` 中加入 `allowed_structures` / `structure_param_caps_json` / `max_leg_count` / `top_n` / `cooldown_min_after_loss`。

性能总结脚本：
- [x] P5-03 新建脚本（如 `tools/option_ml_perf_summary.py`），按 model_version 从 `option_intraday_perf` 聚合 Hit 率/RR/最大 DD/coverage。
- [x] P5-04 在脚本中将摘要写回 `option_ml_models`（更新性能相关字段和 meta），并生成 Markdown 报告供 AI Brain 审阅。
- [x] P5-05 将该脚本接入 AI Brain 的定期任务（cron/调度），作为调参/启停决策输入。
- [x] P5-05a 接入结构选择器：基于 `option_intraday_perf` 自动更新 `allowed_structures`（Top-K + 过滤条件）。

AI 决策逻辑：
- [x] P5-06 定义“表现退化”的判定规则（例如连续 N 天 DD、Hit 率下降阈值）。
- [x] P5-07 在 AI Brain 内实现自动调参逻辑：退化时降低风险上限/提高阈值，严重退化时禁用策略。
- [x] P5-08 定义并实现新模型上线流程：sandbox → paper → live，AI Brain 负责切换 `active_model_version`。
- [x] P5-09 在 AI Brain 中引入“重大事件过滤”（CPI/FOMC/财报等），作为 hard filter（即使模型给信号也不交易），相关规则写入 controls 或 extra_config。

### Phase 6 – Iterative Enhancements (Post-MVP)

- [x] P6-01 增加 60min horizon 的标签与模型（扩展 `features_1m_labeled` 和训练脚本）。
- [x] P6-02 扩展 Universe 到更多标的，并通过控制表限制活跃标的数量。
- [x] P6-03 在执行层加入 debit spreads 等更复杂结构，但保持“单笔定义式风险”原则。
- [x] P6-04 将部分参数接入 HPO/自动配置，让 AI Brain 可以对阈值和仓位参数做小幅自动调优。
