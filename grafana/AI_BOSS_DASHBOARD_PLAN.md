# AI Boss Dashboard 实施计划

**目标**: 为 AI 交易系统创建一个老板视角的驾驶舱 Dashboard
**受众**: AI大脑老板 - 关注盈利、风险、稳定性
**Dashboard 名称**: "AI Boss - Trading Command Center"

**状态**: 🟡 Phase 1 Complete - MVP Scope Defined
**完成日期**: 2025-10-30

---

## ⚠️ PHASE 1 完成 - 重要发现

### 数据可用性评估结果 (2025-10-30)

✅ **可用数据**:
- ai_trader_dash_metrics (105 rows, 21 metrics)
- ai_trader_events (12,371 events: 10,340 brain + 2,031 tool)
- bars_5m_current (259,301 rows - 市场数据)
- market_index_snapshots (10 rows)

🔴 **缺失数据** (需要实盘交易):
- equity_pnl_snapshots (0 rows) - 账户权益
- portfolio_snapshots (0 rows) - 组合快照
- positions_snapshots (0 rows) - 持仓快照
- order_events, fills_live, exec_events (0 rows) - 订单执行数据
- gate_snapshots (0 rows) - 风控门状态

🎯 **MVP 可行性**: 53% (8/15 panels 可构建)
- Row 1: 80% 可构建 (4/5 panels)
- Row 2: 100% 可构建 (4/4 替代 panels)
- Row 3: 0% - 推迟到 Phase 3
- Row 4: 0% - 推迟到 Phase 3

📁 **详细报告**:
- [DATA_AVAILABILITY_REPORT.md](ai_boss_dashboard/DATA_AVAILABILITY_REPORT.md)
- [DATA_GAP_ANALYSIS.md](ai_boss_dashboard/DATA_GAP_ANALYSIS.md)

---

## 📊 Dashboard 架构设计

### 核心理念
> "像飞机驾驶舱一样，一眼看清所有关键指标，出问题时能立即下钻到细节"

### 三大核心关注点
1. **盈利能力 (Profitability)** - 我们赚钱了吗？
2. **风险控制 (Risk)** - 我们会爆仓吗？
3. **系统稳定性 (Stability)** - 系统在正常工作吗？

---

## 🗺️ Dashboard 区块布局 (Rows)

### Row 1: 顶级宏观概览 (Executive Summary)
**目标**: 3秒内知道系统整体状态

| Panel | 类型 | 指标 | 优先级 |
|-------|------|------|--------|
| Total PnL | Stat (超大) | 总盈亏 (今日/本周/总计) | P0 |
| Max Drawdown | Gauge | 最大回撤 % | P0 |
| Sharpe Ratio | Stat | 夏普比率 (30天滚动) | P0 |
| System Status | Polystat/Stat | 核心服务状态 | P0 |
| Guardian Status | Stat | 守护者状态 | P0 |

### Row 2: AI 模型表现 (Brain Intelligence)
**目标**: 验证 AI 的"智商"

| Panel | 类型 | 指标 | 优先级 |
|-------|------|------|--------|
| Signal Distribution | Histogram | 信号分布 (-1到1) | P1 |
| Confidence Distribution | Histogram | 置信度分布 (0到1) | P1 |
| High-Confidence Performance | Bar Chart | 按置信度区间的胜率 | P0 |
| Feature Drift | Time Series | 关键特征漂移监控 | P1 |

### Row 3: 投资组合 (Portfolio)
**目标**: 知道我们把钱投在哪里

| Panel | 类型 | 指标 | 优先级 |
|-------|------|------|--------|
| PnL by Strategy | Table/Bar | 各策略盈亏贡献 | P0 |
| Current Positions | Table | 实时持仓明细 | P0 |
| Exposure Trend | Time Series | 总风险暴露趋势 | P1 |

### Row 4: 交易执行 (Execution)
**目标**: 确保交易高效低成本

| Panel | 类型 | 指标 | 优先级 |
|-------|------|------|--------|
| Slippage Analysis | Histogram | 滑点分布 (bps) | P1 |
| Decision Funnel | Funnel/Sankey | 决策→订单→成交转化 | P1 |
| Fill Rate | Stat | 订单成交率 | P1 |

### Variables (交互筛选)
- **$timerange** - 时间范围 (1h, 1d, 1w, 1m, all)
- **$strategy_id** - 策略ID筛选
- **$symbol** - 股票代码筛选

---

## 📋 实施计划 (分阶段)

### Phase 1: 数据可用性评估 ✅ COMPLETE (2025-10-30)
**目标**: 确认现有数据能支持哪些 panels

#### Step 1.1: 检查现有表和数据 ✅ DONE
```
检查结果:
✅ ai_trader_dash_metrics - 105 rows (21 metrics, all from 打点清单.md)
✅ ai_trader_events - 12,371 events (10,340 brain + 2,031 tool)
✅ bars_5m_current - 259,301 rows
✅ market_index_snapshots - 10 rows

⚪ equity_pnl_snapshots - 0 rows (需要实盘交易)
⚪ portfolio_snapshots - 0 rows (需要实盘交易)
⚪ positions_snapshots - 0 rows (需要实盘交易)
⚪ order_events - 0 rows (需要实盘交易)
⚪ fills_live - 0 rows (需要实盘交易)
⚪ exec_events - 0 rows (需要实盘交易)
⚪ gate_snapshots - 0 rows (需要实盘交易)
```

**输出**: ✅ `ai_boss_dashboard/DATA_AVAILABILITY_REPORT.md`
- ✅ 4 tables with data
- ⚪ 7 tables empty (waiting for live trading)
- 详细统计: 21 metrics 全部可用, 12K+ events

#### Step 1.2: 数据缺口分析 ✅ DONE
**输出**: ✅ `ai_boss_dashboard/DATA_GAP_ANALYSIS.md`
- 15 panels 逐一分析
- 8 panels 可立即构建
- 7 panels 需等待实盘交易数据
- 提供 MVP 替代方案和优先级

---

### Phase 2: 创建 MVP Dashboard (最小可用版本) ⏭️ NEXT
**目标**: 使用现有数据创建 8-panel MVP
**可用数据**: ai_trader_dash_metrics + ai_trader_events

#### Step 2.1: Row 1 - System Health (4 panels) - Priority P0/P1
✅ **可构建的 panels**:
1. **OOS Sharpe Ratio** (P0) - Stat panel, metric: `oos_sharpe`
2. **OOS Max Drawdown** (P0) - Gauge, metric: `oos_mdd * 100` (%)
3. **System Status** (P1) - Multi-stat, metrics: `dq_ok`, `tools_running`
4. **Guardian Status** (P1) - Text panel, metric: `guardian_events` count

**文件**: `ai_boss_dashboard/row1_system_health.json`

#### Step 2.2: Row 2 - Brain Activity (4 panels) - Priority P0/P1
✅ **可构建的 panels**:
1. **Brain Calls (24h)** (P0) - Stat, count from `ai_trader_events` where kind='brain'
2. **Brain Call Frequency** (P0) - Time series, group by hour + session
3. **Tool Executions (24h)** (P0) - Stat, count from `ai_trader_events` where kind='tool'
4. **OOS Performance Table** (P1) - Table, show oos_sharpe/oos_mdd/oos_trades

**文件**: `ai_boss_dashboard/row2_brain_activity.json`

#### Step 2.3: Row 3 & 4 - Portfolio & Execution ⏭️ DEFER
🔴 **无法构建** - 需要实盘交易数据:
- Row 3: positions_snapshots, portfolio_snapshots (0 rows)
- Row 4: order_events, fills_live, exec_events (0 rows)

**推迟到 Phase 3** (实盘交易启动后)

#### Step 2.4: 整合 MVP Dashboard (8 panels)
**文件**: `ai_boss_dashboard/ai_boss_mvp.json`
**结构**:
- Row 1: System Health (4 panels)
- Row 2: Brain Activity (4 panels)
- 时区: America/New_York
- 刷新: 30s
- 时间范围: Last 24 hours (default)

---

### Phase 3: 完整功能实现 (实时交易数据)
**前提**: 实时交易系统已启动，数据开始流入

#### Step 3.1: 完善 Row 1 - 真实盈亏数据
数据源: `equity_pnl_snapshots`, `portfolio_snapshots`
- Total PnL - 实时账户权益
- Max Drawdown - 计算真实回撤
- Sharpe Ratio - 滚动计算

#### Step 3.2: 完善 Row 2 - AI 决策分析
数据源: `gate_snapshots`, 新增的 signal/confidence 记录
- Signal Distribution
- Confidence Distribution
- High-Confidence Performance

#### Step 3.3: 完善 Row 3 - 实时持仓
数据源: `positions_snapshots`, `portfolio_snapshots`
- Current Positions - 实时持仓表
- PnL by Strategy - 策略贡献分析
- Exposure Trend - 风险暴露

#### Step 3.4: 完善 Row 4 - 交易执行
数据源: `order_events`, `fills_live`, `exec_events`
- Slippage Analysis - 滑点统计
- Decision Funnel - 决策漏斗
- Fill Rate - 成交率

**文件**: `ai_boss_command_center_full.json`

---

### Phase 4: 高级功能 (可选)
- Alert Rules - 关键指标告警
- Annotations - 重要事件标注
- Dashboard Links - 下钻到详细页面
- Mobile View - 移动端优化

---

## 📊 数据需求详细规格

### Row 1: Executive Summary

#### Panel 1.1: Total PnL
**数据源**: `equity_pnl_snapshots`
```sql
-- 今日盈亏
SELECT
    anyLast(equity) - anyFirst(equity) as day_pnl
FROM equity_pnl_snapshots
WHERE toDate(ts) = today()

-- 总权益
SELECT anyLast(equity) as total_equity
FROM equity_pnl_snapshots
```

**字段需求**:
- `ts` - 时间戳
- `equity` - 总权益
- `day_pnl` - 日盈亏
- `realized_pnl` - 已实现盈亏
- `unrealized_pnl` - 未实现盈亏

**阈值设置**:
- Green: PnL > 0
- Red: PnL < 0
- Yellow: PnL = 0

#### Panel 1.2: Max Drawdown
**数据源**: `equity_pnl_snapshots` 或 `ai_trader_dash_metrics`
```sql
-- 计算最大回撤
WITH equity_series AS (
    SELECT
        ts,
        equity,
        max(equity) OVER (ORDER BY ts) as peak_equity
    FROM equity_pnl_snapshots
    WHERE ts > now() - INTERVAL 30 DAY
)
SELECT
    min((equity - peak_equity) / peak_equity * 100) as max_dd_pct
FROM equity_series
```

**字段需求**:
- `equity` - 历史权益数据
- 或直接使用 `guardian_max_dd_pct`

**阈值设置**:
- Green: < 5%
- Yellow: 5-10%
- Orange: 10-15%
- Red: > 15%

#### Panel 1.3: Sharpe Ratio
**数据源**: `ai_trader_dash_metrics`
```sql
SELECT anyLast(value) as sharpe
FROM ai_trader_dash_metrics
WHERE name = 'oos_sharpe'
AND date >= today() - INTERVAL 30 DAY
```

**字段需求**:
- `oos_sharpe` - 样本外夏普比率

**阈值设置**:
- Excellent: > 2.0
- Good: 1.0-2.0
- Fair: 0.5-1.0
- Poor: < 0.5

#### Panel 1.4: System Status
**数据源**: `ai_trader_dash_metrics`
```sql
SELECT
    name,
    anyLast(value) as status
FROM ai_trader_dash_metrics
WHERE name IN (
    'tools_running',
    'dq_ok',
    'rl_gate_ok'
)
AND date = today()
GROUP BY name
```

**字段需求**:
- `tools_running` - LLM工具状态
- `dq_ok` - 数据质量
- `rl_gate_ok` - RL门控状态

**显示逻辑**:
- All Green (1): System OK
- Any Red (0): System Issues

#### Panel 1.5: Guardian Status
**数据源**: `ai_trader_dash_metrics`
```sql
SELECT
    anyLast(value) as guardian_events,
    anyLast(guardian_max_dd_pct) as current_dd
FROM ai_trader_dash_metrics
WHERE name IN ('guardian_events', 'guardian_max_dd_pct')
AND date = today()
```

**状态映射**:
- `guardian_events = 0` → "Active" (Green)
- `guardian_events > 0` → "TRIPPED" (Red)
- 显示触发原因和次数

---

### Row 2: AI Brain Intelligence

#### Panel 2.1 & 2.2: Signal & Confidence Distribution
**数据源**: 需要新增字段或从 `gate_snapshots` 提取

**理想数据结构**:
```sql
CREATE TABLE ai_decisions (
    ts DateTime,
    strategy_id String,
    symbol String,
    signal Float64,      -- -1 to 1
    confidence Float64,  -- 0 to 1
    action String,       -- BUY/SELL/HOLD
    ...
)
```

**查询**:
```sql
-- Signal 分布
SELECT
    floor(signal * 10) / 10 as signal_bucket,
    count() as count
FROM ai_decisions
WHERE ts > now() - INTERVAL 1 DAY
GROUP BY signal_bucket
ORDER BY signal_bucket

-- Confidence 分布
SELECT
    floor(confidence * 10) / 10 as conf_bucket,
    count() as count
FROM ai_decisions
WHERE ts > now() - INTERVAL 1 DAY
GROUP BY conf_bucket
ORDER BY conf_bucket
```

**MVP 替代方案** (如果数据不可用):
- 使用 `ai_trader_events` 中的 brain events
- 从 payload 中解析相关信息

#### Panel 2.3: High-Confidence Performance
**数据源**: `ai_decisions` + `fills_live`

```sql
SELECT
    floor(d.confidence * 10) / 10 as conf_bucket,
    count() as total_trades,
    countIf(f.realized_pnl > 0) as winning_trades,
    avg(f.realized_pnl) as avg_pnl
FROM ai_decisions d
LEFT JOIN fills_live f ON d.decision_id = f.decision_id
WHERE d.ts > now() - INTERVAL 7 DAY
GROUP BY conf_bucket
ORDER BY conf_bucket
```

**MVP 替代方案**:
- 使用回测数据中的 confidence vs performance 统计

#### Panel 2.4: Feature Drift
**数据源**: 需要新表 `feature_monitor`

**理想数据结构**:
```sql
CREATE TABLE feature_monitor (
    ts DateTime,
    feature_name String,
    mean Float64,
    std Float64,
    baseline_mean Float64,
    baseline_std Float64
)
```

**查询**:
```sql
SELECT
    ts as time,
    feature_name,
    (mean - baseline_mean) / baseline_std as drift_score
FROM feature_monitor
WHERE feature_name IN ('zscore_20', 'atr_14', 'rsi_14')
AND ts > now() - INTERVAL 7 DAY
ORDER BY ts
```

**MVP 替代方案**:
- 暂时跳过，或显示占位符

---

### Row 3: Portfolio & Exposure

#### Panel 3.1: PnL by Strategy
**数据源**: `portfolio_snapshots` 或 `fills_live`

```sql
SELECT
    strategy_id,
    sum(realized_pnl) as total_pnl,
    sumIf(realized_pnl, toDate(ts) = today()) as day_pnl,
    count() as num_trades
FROM fills_live
WHERE ts > now() - INTERVAL 30 DAY
GROUP BY strategy_id
ORDER BY total_pnl DESC
```

**字段需求**:
- `strategy_id`
- `realized_pnl`
- `ts`

#### Panel 3.2: Current Positions
**数据源**: `positions_snapshots`

```sql
SELECT
    symbol,
    quantity,
    avg_price,
    current_price,
    (current_price - avg_price) * quantity as unrealized_pnl,
    formatDateTime(ts, '%H:%M:%S') as last_update
FROM positions_snapshots
WHERE ts = (SELECT max(ts) FROM positions_snapshots)
AND quantity != 0
ORDER BY abs(unrealized_pnl) DESC
```

**字段需求**:
- `symbol`, `quantity`, `avg_price`, `current_price`
- `unrealized_pnl`

#### Panel 3.3: Exposure Trend
**数据源**: `positions_snapshots` 或 `portfolio_snapshots`

```sql
SELECT
    ts as time,
    sum(abs(quantity * current_price)) as gross_exposure,
    sum(quantity * current_price) as net_exposure
FROM positions_snapshots
WHERE ts > now() - INTERVAL 7 DAY
GROUP BY ts
ORDER BY ts
```

---

### Row 4: Execution Quality

#### Panel 4.1: Slippage Analysis
**数据源**: `fills_live` + `order_events`

```sql
SELECT
    (f.fill_price - o.limit_price) / o.limit_price * 10000 as slippage_bps,
    count() as count
FROM fills_live f
JOIN order_events o ON f.order_id = o.order_id
WHERE f.ts > now() - INTERVAL 1 DAY
GROUP BY floor(slippage_bps * 10) / 10
ORDER BY slippage_bps
```

**字段需求**:
- `fill_price` - 实际成交价
- `limit_price` - 预期价格

#### Panel 4.2: Decision Funnel
**数据源**: 多表关联

```sql
-- Stage 1: Decisions Generated
SELECT count() as decisions FROM ai_decisions WHERE ts > now() - INTERVAL 1 DAY

-- Stage 2: Passed Risk Gates
SELECT count() as passed_gates FROM gate_snapshots WHERE passed = 1 AND ts > now() - INTERVAL 1 DAY

-- Stage 3: Orders Submitted
SELECT count() as orders_submitted FROM order_events WHERE status = 'SUBMITTED' AND ts > now() - INTERVAL 1 DAY

-- Stage 4: Orders Filled
SELECT count() as orders_filled FROM fills_live WHERE ts > now() - INTERVAL 1 DAY
```

#### Panel 4.3: Fill Rate
**数据源**: `order_events` + `fills_live`

```sql
SELECT
    countIf(status = 'FILLED') * 100.0 / count() as fill_rate_pct
FROM order_events
WHERE ts > now() - INTERVAL 1 DAY
```

---

## 🎯 实施优先级

### P0 - 立即实现 (MVP 核心)
1. Row 1: Executive Summary (使用现有 `ai_trader_dash_metrics`)
2. Row 2: Brain Intelligence (基础版，使用 `ai_trader_events`)
3. Variables: Time Range, Strategy ID, Symbol

### P1 - 短期目标 (实时数据可用后)
1. Row 3: Portfolio (真实持仓数据)
2. Row 4: Execution Quality (订单和成交数据)
3. 完善 Row 2 的高级分析

### P2 - 中期增强
1. Alert Rules 配置
2. Drill-down 链接
3. Feature Drift 监控

### P3 - 长期优化
1. 预测性分析
2. 异常检测
3. 自动化报告

---

## 📁 文件组织

```
./grafana/
├── ai_boss_dashboard/
│   ├── AI_BOSS_DASHBOARD_PLAN.md (本文件)
│   ├── DATA_AVAILABILITY_REPORT.md (Phase 1 输出)
│   ├── DATA_GAP_ANALYSIS.md (Phase 1 输出)
│   ├── dashboard_mvp_row1.json (Phase 2)
│   ├── dashboard_mvp_row2.json (Phase 2)
│   ├── dashboard_mvp_row3.json (Phase 2)
│   ├── ai_boss_command_center_mvp.json (Phase 2 最终)
│   ├── ai_boss_command_center_full.json (Phase 3 最终)
│   └── queries/ (SQL 查询测试文件)
│       ├── test_pnl_queries.sql
│       ├── test_brain_queries.sql
│       ├── test_portfolio_queries.sql
│       └── test_execution_queries.sql
```

---

## ✅ 下一步行动

### Phase 1 完成 ✅ (2025-10-30)
1. ✅ 创建本计划文档
2. ✅ 执行 Phase 1.1: 数据可用性评估
3. ✅ 生成 DATA_AVAILABILITY_REPORT.md
4. ✅ 生成 DATA_GAP_ANALYSIS.md
5. ✅ 根据数据可用性调整 Phase 2 计划

### Phase 2 - 立即可开始 ⏭️
6. ⏭️ 创建 Row 1 - System Health (4 panels)
   - OOS Sharpe Ratio (P0)
   - OOS Max Drawdown (P0)
   - System Status (P1)
   - Guardian Status (P1)

7. ⏭️ 创建 Row 2 - Brain Activity (4 panels)
   - Brain Calls (24h) (P0)
   - Brain Call Frequency (P0)
   - Tool Executions (24h) (P0)
   - OOS Performance Table (P1)

8. ⏭️ 整合 MVP Dashboard (8 panels)
   - 文件: `ai_boss_dashboard/ai_boss_mvp.json`
   - 测试所有 panels
   - 部署到 Grafana

### Phase 3 - 待实盘交易启动后
9. ⏭️ 添加 Row 1 实时 PnL 数据
10. ⏭️ 添加 Row 2 信号/置信度分布 (如果数据可用)
11. ⏭️ 实现 Row 3 - Portfolio (3 panels)
12. ⏭️ 实现 Row 4 - Execution Quality (3 panels)

---

**创建时间**: 2025-10-30
**最后更新**: 2025-10-30 14:16 EDT
**创建者**: Claude Code
**状态**: 🟡 Phase 1 Complete - Ready for Phase 2

**下一步**: Phase 2.1 - 创建 Row 1 System Health (4 panels)

---

## 📊 进度追踪

| Phase | Status | Completion | Date |
|-------|--------|------------|------|
| Phase 1.1 - 数据可用性检查 | ✅ DONE | 100% | 2025-10-30 |
| Phase 1.2 - 数据缺口分析 | ✅ DONE | 100% | 2025-10-30 |
| Phase 2.1 - Row 1 (4 panels) | ⏭️ NEXT | 0% | - |
| Phase 2.2 - Row 2 (4 panels) | ⏭️ PENDING | 0% | - |
| Phase 2.4 - MVP 整合 | ⏭️ PENDING | 0% | - |
| Phase 3 - 完整版 | ⏭️ FUTURE | 0% | - |

**总体进度**: Phase 1 Complete (20%), Phase 2 Ready to Start
