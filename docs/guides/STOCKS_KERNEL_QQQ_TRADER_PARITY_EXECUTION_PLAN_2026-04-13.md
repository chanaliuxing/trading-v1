# Stocks Kernel QQQ Trader Parity Execution Plan (2026-04-13)

## 目标（交易员视角）

把 `qqq_intraday_001` 的交易语义复刻到 `stocks_kernel`（应用到 SMH/SOXL/SOXS 等标的）：

1. 同样的入场/方向判断逻辑（不是“看起来像”）。
2. 同样的日内纪律：限制交易次数、禁开新仓窗口、当日清仓。
3. 同样的持仓生命周期：开仓 -> TP1/保护 -> 退出。
4. 同样的风险边界：不因系统抖动误伤，不因模块升级破坏旧策略。

---

## 第一性原理拆解

复刻是否成立，不看“代码看上去像”，只看 4 个证据：

1. **信号一致性证据**：同一时段同一输入，允许/方向/理由一致。
2. **节奏一致性证据**：日内交易频次不失真（不过度放大）。
3. **闭环一致性证据**：当日结束无残留持仓/未闭链路。
4. **安全一致性证据**：升级不破坏非目标策略和现网稳定性。

---

## 落地计划（按执行顺序）

### Phase 1: 建立交易员验收闸门（已开始落地）

1. 新增 trader parity gate：
   - 输入：`identity report`（QQQ 规则等价）、`daily summary`（日内交易闭环）
   - 输出：`GO/HOLD + failed_checks`
2. 固化 4 个硬检查：
   - `signal_allow_parity`
   - `signal_side_parity`
   - `cadence_fill_count_guard`
   - `same_day_flatten_guard`

实现：
- [`tool/replay/run_stocks_kernel_trader_parity_gate.py`](./tool/replay/run_stocks_kernel_trader_parity_gate.py)
- [`tests/unit/scripts/test_run_stocks_kernel_trader_parity_gate.py`](./tests/unit/scripts/test_run_stocks_kernel_trader_parity_gate.py)

### Phase 2: 把策略配置收口为“交易员契约”

1. 对目标 kernel 策略强制契约：
   - `rule_engine_parity=true`
   - `entry_check_on_new_minute_only=true`
   - `no_new_after_et` 明确
   - `flatten_before_close_et` 明确（或启用默认同日 flatten 兜底）
2. 增加启动期审计：
   - 输出“契约是否满足”的单条摘要，避免误配静默运行。

### Phase 3: 做交易日逐笔对拍（关键）

1. 每个交易日输出两份对拍：
   - 信号层：允许/方向/原因差异
   - 成交层：触发链数量、成交笔数、未闭链、EOD 持仓
2. 对拍规则：
   - 超阈值自动 `HOLD`
   - 仅当连续 N 天 `GO` 才允许提升权重/切换

### Phase 4: 发布准入与回滚

1. 发布前清单：
   - parity gate=GO
   - 同票多策略冲突测试通过
   - 非 kernel 冒烟通过
2. 回滚点：
   - 一键回到 legacy 配置路径
   - 不改数据库协议，避免不可逆迁移

---

## 测试方案

### 单元测试

1. parity gate 的 GO/HOLD 分支。
2. 各检查项边界值（阈值上下沿）。
3. 输入兼容性（`metadata` 包裹 vs 直接 summary）。

### 集成测试

1. stocks_kernel runtime cycles（entry/protection/flatten）。
2. e2e sessions（RTH/盘外行为）。
3. 现网关键策略冒烟（确保无旁路污染）。

### 实盘日验证

1. 当日成交笔数、触发链数量、未闭链数量。
2. EOD 持仓是否为 0。
3. 慢执行告警频次是否下降且不影响风险检查。

---

## 验收标准（必须同时满足）

1. `signal_allow_parity >= 0.98`
2. `signal_side_parity >= 0.98`
3. `kernel fills <= legacy fills * 2.0`（可按策略再收紧）
4. `open_trigger_count == 0`
5. `position_notional_abs_usd == 0`（EOD）
6. 非目标策略无回归

---

## 当前状态（2026-04-13）

已完成：

1. Trader parity gate 脚本已落地。
2. GO/HOLD 单测已落地。

下一步执行：

1. 将 parity gate 接入开盘前准入流程。
2. 用最近交易日实际数据跑一轮并产出差异报告。
