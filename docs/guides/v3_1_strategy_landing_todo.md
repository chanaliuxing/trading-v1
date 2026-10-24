# V3.1 策略落地 TODO（不改核心代码）

目标：把“V3.1 策略 + 卖出规则 + 执行约束”以**可插拔策略包**形式落地到当前仓库（`services/strategy/engine.py` 策略引擎 + IBKR Gateway 多 client 并发），做到：信号密度稳定、参数可数据驱动校准/动态加载、动作幂等、执行安全（Fix-1/2/3）。

约束：不修改 `run_system.py` / `services/*` 核心链路；通过 **新增策略模块 + 新增脚本/表 + config.yml 配置**接入。

---

## 0) 现有系统可用接入点（用于“无侵入落地”）
- 策略加载：`config.yml:strategies[]`（module/class）→ `run_system.py` 动态 import → `services/strategy/engine.py` 执行。
- 策略输入：`on_features(symbol, features)`（来自 `MarketDataFacade.poll_new_bar()` 的 bars + 可选 L1 quote；RFE 开启时可用更丰富指标）。
- 下单与回报：可选两条路（二选一，均不改核心）  
  1) **策略内直连 IB**：复用 `strategies/options/bull_put_spread_strategy.py` 的 guard 模式（open order index + cancel-wait + Redis TTL lock）。  
  2) **走 DecisionPipeline**：`ctx.submit_intent(DecisionEnvelope)`（但 Fix-1/2/3 需在策略侧额外实现“提交前 guard”，否则难满足约束）。
- 账户/订单工具：`services.portfolio.account_service`、`services.portfolio.order_monitor`、`services.market.options_data`（期权报价/IV 可选）。

本落地文档默认采用 **(1) 策略内直连 IB**（最符合 Fix-1/2/3、multi-client 并发、幂等要求）。

---

## A) 代码结构（新增目录/模块）
新增策略包（建议路径）：
```
strategies/v3_1/
  strategy_v3_1.py            # StrategyAlgorithm 入口（唯一需要在 config.yml 引用的类）
  schemas.py                  # Signal / ExitPlan / HedgeAction / TradeLog（JSON 序列化）
  params_store.py             # 参数加载/热更新（从 runs/params/v3_1/latest.json）
  indicators.py               # EMA10/20/50、VWAP、ATR14（日线/降级）+ slope 等
  market_state.py             # Breadth20 + STATE(OFF/TREND_OK/CAUTION)（带缓存/限频）
  scarcity.py                 # 稀缺度控制器：只扩池子/权重，不降核心门槛
  lanes.py                    # Lane1/Lane2/Lane3(默认关闭) 入场条件
  scoring.py                  # QualityScore 0~100 的确定公式（四项各 0~25）
  sizing.py                   # risk_budget / Stop0 / shares / max_notional 等
  hedge.py                    # HedgeCostRatio → SIZE_REDUCTION 默认；可选 put/putspread
  exit_manager.py             # Stop>Protect>Trim>Trail>Time 无冲突链 + 幂等 targets
  execution_guard.py          # Fix-1/2/3（复用 bull_put 的实现风格，适配正股+期权腿）
  replay/
    simulator.py              # 回放撮合/虚拟成交（不下单）
    runner.py                 # main：加载数据→跑策略→产出 TradeLog/指标
tools/v3_1/
  calibrate.py                # walk-forward 参数校准（输出 runs/params/v3_1/latest.json）
  build_watchlist.py          # 扩池子输入（Top50/Top100/sector 强势股）产出 watchlist 文件
sql/
  v3_1_trade_log.sql          # ClickHouse 或 Postgres 的 trade log/schema（二选一落地）
```

---

## B) 关键接口（策略包内，供插拔/测试/回放复用）
必须保证“纯逻辑”与“执行/IO”分离，便于回放与校准：
- `SignalEngine.evaluate(bar_ctx) -> Optional[Signal]`
- `ExitManager.on_bar(bar_ctx, position_state) -> ExitActions`
- `ExecutionGuard.place_equity_order(...) -> ExecResult`（包含 Fix-1/2/3）
- `ParamsStore.get(ts)-> ParamSet`（支持 reload interval + 校准文件原子替换）

策略入口 `V31Strategy(StrategyAlgorithm)` 的职责：
1) 聚合输入（bars/L1/可选 IV/OI）→ `bar_ctx`
2) 计算 STATE + 稀缺度扩池（只动 watchlist）
3) 生成 `Signal JSON` + `ExitPlan JSON`（结构严格按需求）
4) 调用 `ExecutionGuard` 下单；回报驱动 `ExitManager`
5) 写入 `TradeLog`（用于校准与审计）

---

## C) 数据结构（必须落地为 JSON + 可查询表）
1) `Signal JSON` / `ExitPlan JSON`：严格按你给的输出结构（字段齐全、targets 幂等、gap 累计减仓）。
2) `TradeLog`（建议 JSONL + 表结构并行）最少字段：
- `ts, ticker, lane, state, grade, quality_score`
- `entry_px, stop0, r, shares, risk_budget, hedge_action`
- `events[]`（SUBMIT/FILL/CANCEL/TRIM/PROTECT/TRAIL/TIME_EXIT，含 order_id、qty、px）
- `mfe_r, mae_r, realized_r, costs_bp, slippage_bp`

---

## D) 参数校准与动态调整（walk-forward）
实现 `tools/v3_1/calibrate.py`：
- 数据：回放 bars + (可选) IV/OI + 历史 trade log（或模拟成交结果）
- 方法：滚动窗口 walk-forward（例：train 90d / valid 30d / roll 30d）
- 目标函数：`expectancy = avg_R * win_rate - costs`，并加约束（MDD、trade/day、turnover）
- 输出：`runs/params/v3_1/latest.json`（包含 breadth 阈值、ATR 倍数、R targets、NoProgress、HedgeCostRatio、WallDist 边界等）
- 运行时：`ParamsStore` 定时 reload（只读文件，不改核心服务）

---

## E) 测试与回放（至少 10 个用例）
新增 `tests/unit/strategies/v3_1/`，基于 `replay/simulator.py`：
1) Lane2 回踩触发→止损命中（小亏）
2) Lane2 到 T1 后推保本→回撤不亏
3) Lane1 breakout 跳空越过 T1 到 T2→累计减仓正确
4) OI 墙目标在 T2 前/后→targets 排序正确且只触发一次
5) NoProgress：震荡缓涨不误杀；横盘触发退出
6) STATE=OFF：不放宽、不强行出信号
7) 稀缺度低：扩 watchlist 生效但不降核心门槛
8) 对冲贵：触发减仓，不买 put
9) 多 client 并发同 conId：TTL lock 拦截
10) cancel-replace：必须等 Cancelled 再 submit

---

## F) 执行安全 Fix-1/2/3（在策略包 execution_guard.py 内落地）
直接复用 `strategies/options/bull_put_spread_strategy.py` 的 guard 设计并泛化到正股：
- Fix-1：leg-level open order 检查  
  - equity：单 conId；hedge/spread：多 conId legs  
  - index：`open_by_conid_side[conId][BUY/SELL] -> {order_ids}`
- Fix-2：cancel → 等确认 → 再下新单  
  - 通过 IB orderStatus/openOrder 事件 + `OrderMonitor.wait_for_fill(status=Cancelled)` 实现  
  - 超时/重试可配置（带结构化日志 + 审计事件）
- Fix-3：多 client 并发锁（Redis，TTL）  
  - key：`lock:conid:{conId}`（或 `lock:ticker:{symbol}` 作为退化）  
  - TTL：毫秒级可配置；锁过期可抢占；记录 owner 与审计日志

---

## 落地步骤清单（按 PR/提交拆分）
- [x] PR1：新增 `strategies/v3_1/` 纯逻辑（lanes/scoring/exit_plan JSON）+ shadow 回放 runner
- [x] PR2：新增 `execution_guard.py`（Fix-1/2/3）+ paper 下单开关 + 结构化 TradeLog
- [x] PR3：新增 `tools/v3_1/calibrate.py` + 参数文件热加载（ParamsStore）
- [x] PR4：新增 10+ 单测 + 回放测试数据夹具（fixtures）
- [x] PR5：在 `config.yml` 新增 strategy entry（仅配置，不改核心代码），默认 shadow/paper 小仓
