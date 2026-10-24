# Bull Put Spread 落地 TODO（可打勾）

目标：将 QQQ Bull Put Spread 策略深度集成到现有 `run_system.py` / StrategyEngine 体系，复用已有 OptionsDataProvider 与 IBKR client，形成完整“趋势 → 选腿 → 下单 → 持仓管理 → 风控 → 监控/测试”的闭环。

---

## A. Adapter / 执行层
- [x] 复用 `IBKROptionsDataProvider.place_vertical_spread` 作为唯一期权组合单执行入口（BAG 下单）。
- [x] 在 `services/exec/adapters/ib_adapter.py` 中增加账户摘要缓存与 `available_funds()`，用于 IB 可用资金检查。
- [x] 抽象统一 AccountService（`services/portfolio/account_service.py`），封装 IB 账户查询（可用资金），策略不直接 new `IBInsyncAdapter`。

---

## B. 逻辑层：趋势 + Spread 选择

### B1. 趋势判定（TrendClassifier 思路）
- [x] 使用已有 features（price、ma50/100/200、pct_change_1d、vix）而非在策略中重新拉历史 + pandas。
- [x] 输出 3 态：`UP` / `DOWN_OK` / `DOWN_FAST`，其中：  
  - `DOWN_FAST`：单日跌幅 ≤ crash 阈值（默认 -2%）或明显跌破 MA200 或 VIX ≥ vix_halt。
  - `UP`：price > MA50（健康上涨）。
  - `DOWN_OK`：MA200 < price ≤ MA50（可保守卖 Put）。
- [x] 将 `vix_halt`、`crash_filter_pct` 等阈值暴露到 config（`extras.bull_put`）。

### B2. SpreadSelector（选腿/过滤）
- [x] DTE 窗口：按 `min_dte`~`max_dte` 从 options chain 滤出候选合约。
- [x] strike 约束 + Delta：
  - `UP`：short strike ≤ price × (1 - otm_pct_up)。  
  - `DOWN_OK`：short strike ≤ min(price × (1 - otm_pct_down), MA100)。  
  - 在约束内按 delta 贴近 `short_delta` 选 short leg。
- [x] 宽度优选：
  - 尝试 `width_candidates`（默认 5/10），为每个 width 找 long leg；  
  - 对每个 (short,long) 计算 credit，选 net credit 最大的组合。
- [x] Long leg 风控：
  - 过滤 `open_interest < min_open_interest`；  
  - 过滤 `spread_bps > max_spread_bps`；  
  - 忽略价格无效（mid<=0）的合约。

---

## C. 策略层：StrategyAlgorithm 集成
- [x] 使用 `libs.strategy.StrategyAlgorithm` 实现 `BullPutSpreadStrategy`（位于 `strategies/options/bull_put_spread_strategy.py`）。
- [x] 在 `config.yml` 中注册策略 `bull_put_spread_qqq`，走 StrategyEngine + `run_system.py` 的标准加载路径。
- [x] `initialize()`：  
  - 从 `params.extras.bull_put` 读取全部参数（otm_pct_up/down、width_candidates、max_spread_bps、min_open_interest、vix_halt、max_feature_age_sec、max_margin_usd、trade_window_start/end_et、provider.backend、transmit 等）；  
  - 订阅 60 秒 features；记录 backend=stub/ibkr。
- [x] `on_features()`：  
  - 控制扫描频率 `scan_interval_sec`；  
  - 仅在交易窗口内（10:00–15:45 ET）运行；  
  - 检查特征新鲜度（`max_feature_age_sec`）；  
  - 应用 VIX halt；  
  - 无仓位时调用趋势 + spread 逻辑选腿、评估 credit/保证金/可用资金并发 SELL spread；有仓位时调用 TP/SL 管理。
- [x] TP/SL 管理：  
  - 按当前组合 mid 相对 entry_credit 触发 TP（50% 收益）或 SL（200% 亏损）；  
  - 用 `side="BUY"` + `place_vertical_spread` 平仓；  
  - 保留后续扩展空间（time stop、price walk）。

---

## D. 风控与数据/市场防护
- [x] VIX 崩盘保护：`vix >= vix_halt` 时跳过开仓。
- [x] 行情新鲜度：基于特征内时间戳（ts/ts_bar_end_us/timestamp_us）和 `max_feature_age_sec` 过滤陈旧数据。
- [x] 简易保证金估算：用 `(width - credit) * 100 * qty` 与 `max_margin_usd` 对比，超限不做。
- [x] IB 可用资金检查：在 IB backend 下查询 `AvailableFunds`，若 `margin_req > available_funds` 则跳过（失败非致命，只作保护）。
- [ ] 将 IB 账户/保证金相关逻辑从策略移入独立 AccountService，通过注入使用（解耦 Strategy 与 IB adapter 生命周期）。

---

## E. 配置与参数化
- [x] 在 `config.yml` 的 `bull_put_spread_qqq` 下暴露：  
  - `otm_pct_up` / `otm_pct_down`、`width_candidates`、`max_spread_bps`、`min_open_interest`、`vix_halt`、`max_feature_age_sec`、`max_margin_usd`、`trade_window_start_et` / `trade_window_end_et`；  
  - `provider.backend`（stub/ibkr）、`transmit` 开关；  
  - 依赖 env/`libs.config.ib_config` 的 IB_HOST/PORT/CLIENT_ID/IB_ACCOUNT。
- [ ] 将部分风控/策略参数（如 risk budget、max_open_positions）与全局 risk 管理对齐或集中到统一 risk config（可选）。

---

## F. 监控与审计
- [x] 在 `runs/ai_trader/<date>/` 下增加 bull_put 审计文件（`bull_put_spread.jsonl`），记录 scan/entry/exit/TP/SL/守卫事件。
- [x] 在 ClickHouse 中写入 `bull_put_spread_events`（可配置），并在 `doc/bull_put_spread_monitoring.md` 记录建表/查询建议。
- [x] 新增 Grafana dashboard JSON (`dashboards/bull_put_spread_dashboard.json`) + 简要说明 (`dashboards/BULL_PUT_SPREAD.md`)，可视化事件/TP/SL/skip。

---

## G. 测试与验收
- 单元测试：
  - [x] TrendClassifier 分支（UP/DOWN_OK/DOWN_FAST）
  - [x] SpreadSelector strike/width 选择
  - [x] TP/SL 触发与审计事件（add tests/unit/strategies/test_bull_put_spread_strategy.py）
  - [x] 保证金/可用资金守卫
  - [ ] 集成测试（stub backend）：  
    - stub options provider 提供可控链与 mid/greeks；  
    - 验证不同配置下日志中出现期望的 trend/strike/width 和 TP/SL 行为。
  - [ ] IBKR paper，`transmit=false`：  
    - 验证 `place_vertical_spread` 能创建 BAG 订单请求（status/legs 正确），TP/SL 能给出反向 BUY 请求（即便不传输）。  
  - [ ] IBKR paper，`transmit=true`（人工监控阶段）：  
    - 小仓位验证真实成交流程与 TP/SL 平仓行为；确保风控守卫（VIX/新鲜度/保证金）会阻止不符合规则的开仓。  

## H. 待执行测试计划（指派给 Gemini）

1. **集成测试（stub backend）**
   - 使用 `tests/strategies/fixtures/stub_options_chain.json`（或等效 mock 数据）构建 OptionContract 列表，确保 `_select_legs` 能在 stub provider 下返回预期组合；
   - 通过调整 `params.extras.bull_put`（`scan_interval_sec=0`, `trade_window` 包含当前时刻）直接调用 `BullPutSpreadStrategy.on_features`，并验证 `runs/ai_trader/<today>/bull_put_spread.jsonl` 里出现 `enter_submitted` 与 `exit_orders_submitted`；
   - 校验日志/`strategy._audit_event` 的 payload 包含 trend、width、credit、margin_guard 等预期字段。

2. **IBKR Paper 模式（`transmit=false`）**
   - 启动 `run_system.py`，确保 `config.yml` 中`bull_put_spread_qqq.params.extras.bull_put.transmit=false`；
   - 观察 `logs/trader.log` 与 `runs/ai_trader/<today>/bull_put_spread.jsonl`，确认策略选择的 short/long strike 及 `enter_submitted` 事件；
   - 核对 `PostgreSQL trading.option_spread_trades`（若已建表）记录是否包含 `OPEN`/`EXIT_*` 操作，且 `transmit=false` 字段为 `false`；
   - 人为触发 TP/SL 条件（修改 `_current_mid` 返回函数或等待市场更新），确认自动调用 `_close` 生成 `close_submitted`。

3. **IBKR Paper 模式（`transmit=true`）**
   - 临时切 `transmit=true`（确认仍运行于 paper/准生产账户）；
   - 跟踪 `ib_adapter` 侧发送的 BAG 订单请求（`logs/trader.log`、IBKR TWS/Paper logs）是否满足短腿买入、长腿卖出组合；
   - 在 TP/SL 触发条件附近（通过模拟 mid 值）确保 `_place_exit_orders` 使用 OCA group、两个 BUY 请求，并且 `OptionTradeRepository` 写入的 `meta` 字段记录 IBKR 返回结果；
   - 监控 `runs/ai_trader/<today>/bull_put_spread.jsonl` 以及 `ClickHouse trading.bull_put_spread_events`（如有）确认 `take_profit_triggered`/`stop_loss_triggered` 事件连续性。
