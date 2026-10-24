# AI Boss Dashboard - 理想结构实施状态

**按照用户理想 Dashboard 规格重新构建**
**Date**: 2025-10-30 15:30 EDT
**Strategy**: Be honest, use evidence - 有数据就用,没数据就占位

---

## 📊 Dashboard 结构总览

完全按照"驾驶舱"理念设计,分为 4 个核心区域:

### Row 1: 顶级宏观概览 (Am I Rich or Poor?)
**目标**: 第一眼就知道盈亏和风险状况

### Row 2: AI 模型表现 (Is the Brain Smart?)
**目标**: 了解 AI 决策质量和"思考过程"

### Row 3: 投资组合风险 (What Are We Betting On?)
**目标**: 知道钱投在哪里,承担多大风险

### Row 4: 交易执行分析 (Are We Trading Efficiently?)
**目标**: 确保交易高效、低成本执行

---

## 📋 Panel 实施详情

### Row 1: 顶级宏观概览 (6 panels)

| # | Panel | 类型 | 尺寸 | 数据状态 | 数据源 |
|---|-------|------|------|----------|--------|
| 1.1 | **Total PnL** (总盈亏) | Stat | 8x6 | 🟡 PLACEHOLDER | equity_pnl_snapshots |
| 1.2 | **Max Drawdown** (最大回撤) | Gauge | 5x6 | ✅ REAL DATA | ai_trader_dash_metrics.oos_mdd |
| 1.3 | **Sharpe Ratio** (夏普比率) | Stat | 4x6 | ✅ REAL DATA | ai_trader_dash_metrics.oos_sharpe |
| 1.4 | **System Status** (系统状态) | Stat | 4x3 | ✅ REAL DATA | ai_trader_dash_metrics (dq_ok, tools_running) |
| 1.5 | **Guardian Status** (守护者) | Stat | 3x3 | ✅ REAL DATA | ai_trader_dash_metrics.guardian_events |
| 1.6 | **Equity Chart** (权益曲线) | Time Series | 7x3 | 🟡 PLACEHOLDER | equity_pnl_snapshots |

**Row 1 Status**: 4/6 panels 有真实数据 (67%)

---

### Row 2: AI 模型表现 (3 panels)

| # | Panel | 类型 | 尺寸 | 数据状态 | 数据源 |
|---|-------|------|------|----------|--------|
| 2.1 | **Signal Distribution** (信号分布) | Histogram | 8x7 | 🟡 PLACEHOLDER | trading_signals |
| 2.2 | **Confidence Distribution** (置信度分布) | Histogram | 8x7 | 🟡 PLACEHOLDER | trading_signals |
| 2.3 | **High-Confidence Performance** (高置信度表现) | Bar Chart | 8x7 | 🟡 PLACEHOLDER | trading_signals + outcomes |

**Row 2 Status**: 0/3 panels 有数据 (0%) - **需要新增 trading_signals 表**

**为什么重要**:
- **Signal Distribution**: 看模型是否给出明确信号 (±1附近) vs 犹豫不决 (0附近)
- **Confidence Distribution**: 看模型对市场判断有多确定
- **High-Conf Performance**: **验证模型有效性的关键!** 高置信度应该有更高胜率

---

### Row 3: 投资组合风险 (2 panels)

| # | Panel | 类型 | 尺寸 | 数据状态 | 数据源 |
|---|-------|------|------|----------|--------|
| 3.1 | **Strategy PnL** (各策略盈亏) | Table | 12x7 | 🟡 PLACEHOLDER | portfolio_snapshots |
| 3.2 | **Current Positions** (当前持仓) | Table | 12x7 | 🟡 PLACEHOLDER | positions_snapshots |

**Row 3 Status**: 0/2 panels 有数据 (0%) - **需要实盘交易数据**

**为什么重要**:
- **Strategy PnL**: 快速识别明星策略和累赘策略
- **Current Positions**: 精确了解钱放在哪里,每个持仓的盈亏

---

### Row 4: 交易执行分析 (2 panels)

| # | Panel | 类型 | 尺寸 | 数据状态 | 数据源 |
|---|-------|------|------|----------|--------|
| 4.1 | **Slippage Analysis** (滑点分析) | Time Series | 12x6 | 🟡 PLACEHOLDER | fills_live + order_events |
| 4.2 | **Decision Funnel** (决策漏斗) | Bar Gauge | 12x6 | 🟡 PLACEHOLDER | exec_events |

**Row 4 Status**: 0/2 panels 有数据 (0%) - **需要订单执行数据**

**为什么重要**:
- **Slippage**: 隐性成本,过高会严重侵蚀利润
- **Decision Funnel**: 如果大量决策在中间环节被丢弃,说明风控过严或连接有问题

---

## 🎛️ Interactive Variables (交互功能)

Dashboard 顶部有 3 个下拉菜单,可以筛选数据:

| Variable | 类型 | 选项 | 作用 |
|----------|------|------|------|
| **Time Range** | Interval | 1h / 6h / Today / Week / Month | 选择时间范围 |
| **Strategy ID** | Query | All / specific strategy | 筛选特定策略 |
| **Symbol** | Query | All / specific symbol | 筛选特定股票 |

**状态**: ✅ 已配置,所有 panels 查询都支持变量

---

## 📈 Overall Implementation Status

| Category | Total | Implemented | Placeholders | Percentage |
|----------|-------|-------------|--------------|------------|
| **Row 1** | 6 | 4 | 2 | 🟢 67% |
| **Row 2** | 3 | 0 | 3 | 🔴 0% |
| **Row 3** | 2 | 0 | 2 | 🔴 0% |
| **Row 4** | 2 | 0 | 2 | 🔴 0% |
| **Variables** | 3 | 3 | 0 | 🟢 100% |
| **TOTAL** | **14 panels** | **6** | **8** | **🟡 43%** |

---

## 🚨 Priority Implementation Queue

### P0 - 最关键 (立即实施)

#### 1. Total PnL Panel (Row 1.1)
**Why**: 这是**最重要的指标** - 立刻知道是赚是赔!

**需要**:
```sql
CREATE TABLE equity_pnl_snapshots (
    date Date,
    ts DateTime,
    equity Float64,
    realized_pnl Float64,
    unrealized_pnl Float64
) ENGINE = ReplacingMergeTree()
ORDER BY (date, ts);
```

**实施**: 详见 `CRITICAL_METRICS_GUIDE.md`

---

#### 2. Current Positions Table (Row 3.2)
**Why**: 必须知道钱投在哪里!

**需要**:
```sql
CREATE TABLE positions_snapshots (
    date Date,
    ts DateTime,
    symbol String,
    qty Float64,
    entry_price Float64,
    current_price Float64,
    unrealized_pnl Float64,
    strategy_id String
) ENGINE = ReplacingMergeTree()
ORDER BY (date, ts, symbol);
```

**实施**: 详见 `PLACEHOLDER_IMPLEMENTATION_GUIDE.md` Section "Row 3"

---

### P1 - 重要 (尽快实施)

#### 3. Signal/Confidence Distributions (Row 2.1, 2.2)
**Why**: 理解 AI "思考过程"

**需要**:
```sql
CREATE TABLE trading_signals (
    date Date,
    ts DateTime,
    strategy_id String,
    symbol String,
    signal Float64,          -- -1 to 1
    confidence Float64,      -- 0 to 1
    decision Enum8('BUY'=1, 'SELL'=-1, 'HOLD'=0),
    executed Boolean
) ENGINE = MergeTree()
ORDER BY (date, ts, strategy_id, symbol);
```

**实施**: 详见 `PLACEHOLDER_IMPLEMENTATION_GUIDE.md` Section "Row 2"

---

#### 4. High-Confidence Performance (Row 2.3)
**Why**: **验证模型有效性的关键!**

**需要**: trading_signals 表 + outcome 字段

**实施**: 在 trading_signals 表中添加 outcome 字段,记录每笔交易的实际盈亏

---

#### 5. Strategy PnL (Row 3.1)
**Why**: 快速识别明星/累赘策略

**需要**: portfolio_snapshots 表

---

### P2 - 增强 (后续实施)

#### 6. Slippage Analysis (Row 4.1)
**Why**: 控制隐性成本

**需要**: fills_live + order_events 表

---

#### 7. Decision Funnel (Row 4.2)
**Why**: 优化执行流程

**需要**: exec_events 表

---

## 📝 Quick Implementation Guide

### For P0 Metrics (30 minutes)

```bash
# 1. Create tables
curl "http://localhost:8123/?database=trading" --data-binary "$(cat << 'SQL'
CREATE TABLE IF NOT EXISTS equity_pnl_snapshots (
    date Date,
    ts DateTime,
    equity Float64,
    realized_pnl Float64,
    unrealized_pnl Float64
) ENGINE = ReplacingMergeTree()
ORDER BY (date, ts);

CREATE TABLE IF NOT EXISTS positions_snapshots (
    date Date,
    ts DateTime,
    symbol String,
    qty Float64,
    entry_price Float64,
    current_price Float64,
    unrealized_pnl Float64,
    strategy_id String
) ENGINE = ReplacingMergeTree()
ORDER BY (date, ts, symbol);
SQL
)"

# 2. Implement logging in trading system
# See CRITICAL_METRICS_GUIDE.md for detailed code

# 3. Restart trading system

# 4. Verify data flows

# 5. Refresh Grafana Dashboard
```

### For P1 Metrics (1 hour)

```bash
# 1. Create trading_signals table
curl "http://localhost:8123/?database=trading" --data-binary "
CREATE TABLE IF NOT EXISTS trading_signals (
    date Date,
    ts DateTime,
    strategy_id String,
    symbol String,
    signal Float64,
    confidence Float64,
    decision Enum8('BUY'=1, 'SELL'=-1, 'HOLD'=0),
    executed Boolean,
    outcome Nullable(Float64)
) ENGINE = MergeTree()
ORDER BY (date, ts, strategy_id, symbol);
"

# 2. Implement signal logging in strategies
# See PLACEHOLDER_IMPLEMENTATION_GUIDE.md

# 3. Restart and verify

# 4. Refresh Dashboard
```

---

## 🎯 Success Criteria

### When All P0 Implemented (Row 1 Complete)
- ✅ 一眼看到总盈亏 (大数字,绿/红背景)
- ✅ 知道最大回撤 (仪表盘)
- ✅ 看到夏普比率 (风险调整收益)
- ✅ 确认系统正常运行
- ✅ 守护者状态清晰

### When All P1 Implemented (Row 2 + Row 3 Complete)
- ✅ 看到模型信号分布 (明确 vs 犹豫)
- ✅ 看到模型置信度分布 (确定 vs 不确定)
- ✅ **验证模型有效性** (高置信度 = 高胜率?)
- ✅ 识别明星/累赘策略
- ✅ 知道每个持仓的盈亏

### When All P2 Implemented (Row 4 Complete)
- ✅ 监控滑点成本
- ✅ 优化决策转化率
- ✅ **完整的驾驶舱视图!**

---

## 📞 References

详细实施指南:
- **CRITICAL_METRICS_GUIDE.md** - P0 指标 (总盈亏, 持仓)
- **PLACEHOLDER_IMPLEMENTATION_GUIDE.md** - P1/P2 指标 (信号, 执行)
- **DATA_AVAILABILITY_REPORT.md** - 完整数据分析

---

**Dashboard Access**: http://localhost:3000/d/ai-boss-command-center
**Status**: ✅ 结构完成, 43% panels 有数据
**Next**: 按 P0 → P1 → P2 顺序实施数据打点

**Last Updated**: 2025-10-30 15:30 EDT
