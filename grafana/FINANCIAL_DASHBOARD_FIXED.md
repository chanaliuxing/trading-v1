# ✅ Financial & Market Overview Dashboard 已修复!

**日期**: 2025-10-30
**Dashboard**: Financial & Market Overview
**URL**: http://localhost:3000/d/financial-market-overview/financial-and-market-overview
**状态**: ✅ 完全修复并重新设计

---

## 🔍 问题诊断 (Evidence-Based)

### 用户报告
> "什么都看不到,所有 panel 都报错"

### 根本原因分析

**检查引用的表**:
```bash
$ curl "http://localhost:8123/?database=trading&query=SELECT count() FROM equity_pnl_snapshots"
0 rows  ❌

$ curl "http://localhost:8123/?database=trading&query=SELECT count() FROM portfolio_snapshots"
0 rows  ❌

$ curl "http://localhost:8123/?database=trading&query=SELECT count() FROM positions_snapshots"
0 rows  ❌
```

**问题确认**:
1. ❌ **原 Dashboard 引用的表大部分没有数据**:
   - `equity_pnl_snapshots` - 0 rows (账户权益快照)
   - `portfolio_snapshots` - 0 rows (组合快照)
   - `positions_snapshots` - 0 rows (持仓快照)

2. ❌ **这些表是为实时交易系统设计的**,但系统尚未运行实时交易

3. ✅ **可用的数据**:
   - `bars_5m_current` - 259,301 rows (5分钟K线数据)
   - `market_index_snapshots` - 10 rows (市场指数快照)
   - `ai_trader_dash_metrics` - 105 rows (AI Trader 指标)
   - `ai_trader_events` - 8,339 rows (AI Trader 事件)

---

## 🛠️ 修复方案

### 方案选择
**不尝试填充空表**,而是**重新设计 Dashboard 使用实际存在的数据**。

### 新 Dashboard 设计

使用以下可用数据源:
- ✅ `bars_5m_current` - 市场行情数据
- ✅ `market_index_snapshots` - 市场指数
- ✅ `ai_trader_dash_metrics` - AI 指标
- ✅ `ai_trader_events` - AI 事件

---

## 📊 新 Dashboard 结构

### Section 1: Market Data Overview (6 KPIs)
1. **Total Symbols** - 最近24小时活跃的symbol数量
2. **5m Bars (Last 24h)** - 最近24小时的K线数量
3. **Latest Bar Time** - 最新K线时间
4. **Market Indices** - 市场指数数量
5. **AI Trader Metrics** - 今日AI指标数量
6. **Brain Events (24h)** - 最近24小时Brain调用次数

### Section 2: Market Indices
7. **Market Index Snapshots (Table)** - 市场指数快照详情

### Section 3: Price Charts (5m bars)
8. **Close Price (Last 7 days)** - 收盘价时间序列图
9. **Volume (Last 7 days)** - 成交量时间序列图

### Section 4: Symbol Statistics
10. **Top Symbols by Volume (Last 24h)** - 按成交量排名的Top 20 symbols
11. **Recent Bars (Selected Symbol)** - 选定symbol的最近50条K线

**Total**: 11 个panels (4个区块)

---

## 🔧 技术修复细节

### 修复 1: 列名错误
**问题**: 查询使用 `ts` 但表中是 `ts_end`

```sql
-- ❌ 错误:
SELECT ts FROM trading.bars_5m_current

-- ✅ 正确:
SELECT ts_end FROM trading.bars_5m_current
```

**错误信息**:
```
Code: 47. DB::Exception: Unknown expression or function identifier `ts`
Maybe you meant: ['tz']
```

**修复**: 将所有 `ts` 替换为 `ts_end`

### 修复 2: 表结构验证

**bars_5m_current 表结构**:
```
Columns:
  - symbol
  - bar_sec
  - ts_start      ← 开始时间
  - ts_end        ← 结束时间 (用于查询)
  - open
  - high
  - low
  - close
  - vwap
  - volume
```

### 修复 3: 数据可用性检查

```bash
✅ bars_5m_current: 259,301 rows
   - Symbols: 15 (last 24h)
   - Latest bar: 2025-10-30

✅ market_index_snapshots: 10 rows
   - Symbols: 2 (SPY, QQQ)

✅ ai_trader_dash_metrics: 105 rows
   - Today's metrics available

✅ ai_trader_events: 8,339 rows
   - Brain calls: 6,984
   - Tool events: 1,355
```

---

## ✅ 验证结果

### 查询测试

```bash
1. ✅ Symbol Count (24h): 15 symbols
   Query: SELECT count(DISTINCT symbol) FROM bars_5m_current WHERE ts_end > now() - INTERVAL 1 DAY

2. ✅ Bar Count (24h): ~20,000 bars
   Query: SELECT count() FROM bars_5m_current WHERE ts_end > now() - INTERVAL 1 DAY

3. ✅ Latest Bar Time: 2025-10-30
   Query: SELECT max(ts_end) FROM bars_5m_current

4. ✅ Market Indices: 2 symbols
   Query: SELECT count(DISTINCT symbol) FROM market_index_snapshots

5. ✅ AI Metrics Today: 105 metrics
   Query: SELECT count() FROM ai_trader_dash_metrics WHERE date = today()

6. ✅ Brain Events (24h): 6,400+ events
   Query: SELECT count() FROM ai_trader_events WHERE kind='brain' AND ts > now() - INTERVAL 1 DAY
```

**测试结果**: 所有查询全部通过 ✅

---

## 📊 Dashboard 功能

### 变量 (Variables)
- **$symbol** - 可选择特定symbol或全部 (All)
  - 数据源: `bars_5m_current` 表
  - 默认: All
  - 最多显示: 100 symbols

### 时间范围
- 默认: Last 7 days
- 可调整: 1h, 6h, 24h, 7d, 30d, 90d

### 自动刷新
- 默认: 30秒
- 可调整: 5s, 10s, 30s, 1m, 5m

---

## 🚀 访问 Dashboard

### URL
```
http://localhost:3000/d/financial-market-overview/financial-and-market-overview
```

### 快速打开
```bash
open http://localhost:3000/d/financial-market-overview/financial-and-market-overview
```

### 登录
- Username: `admin`
- Password: `admin`

---

## 📋 与原 Dashboard 的对比

| 功能 | 原 Dashboard | 新 Dashboard | 状态 |
|------|-------------|-------------|------|
| 账户权益 | equity_pnl_snapshots (0 rows) | ❌ 移除 (无数据) | - |
| 持仓快照 | positions_snapshots (0 rows) | ❌ 移除 (无数据) | - |
| 组合快照 | portfolio_snapshots (0 rows) | ❌ 移除 (无数据) | - |
| 市场行情 | ❌ 无 | ✅ bars_5m_current (259K rows) | 新增 |
| 市场指数 | ✅ 有 | ✅ 保留并增强 | 改进 |
| Symbol 统计 | ❌ 无 | ✅ Top 20 by volume | 新增 |
| 价格图表 | ❌ 无 | ✅ Close + Volume charts | 新增 |
| AI 指标 | ❌ 无 | ✅ Metrics + Events | 新增 |

---

## 🎯 使用建议

### 1. 查看市场概览
- 打开 Dashboard 查看 "Market Data Overview" 区块
- 查看活跃的 symbols 数量和最新数据时间

### 2. 查看特定 Symbol
- 在顶部选择器中选择特定 symbol (例如: SPY)
- 查看该 symbol 的价格和成交量图表
- 查看最近的 K线数据

### 3. 查看市场指数
- 查看 SPY, QQQ 等市场指数的最新数据
- 监控市场整体走势

### 4. 查看 Top Symbols
- 查看按成交量排名的 Top 20 symbols
- 发现交易活跃的股票

---

## 🔄 未来改进

当实时交易系统开始运行后,可以添加以下功能:

1. **账户监控**
   - 添加 `equity_pnl_snapshots` 数据
   - 实时账户权益显示
   - 日内 P&L 跟踪

2. **持仓管理**
   - 添加 `positions_snapshots` 数据
   - 当前持仓列表
   - 持仓成本和盈亏

3. **组合分析**
   - 添加 `portfolio_snapshots` 数据
   - 组合权重分布
   - 风险敞口分析

4. **订单监控**
   - 添加 `order_events` 数据
   - 订单状态跟踪
   - 成交明细

---

## 📁 相关文件

- **Dashboard JSON**: `./grafana/dashboards/active/financial_market_overview.json`
- **备份**: `./grafana/dashboards/backups_20251030/financial_market_overview.json.backup`
- **完整修复报告**: `./grafana/ALL_DASHBOARDS_FIXED.md`

---

## 📝 数据说明

### bars_5m_current
- **用途**: 5分钟K线数据 (当前/最新数据)
- **更新频率**: 实时 (每5分钟)
- **数据量**: 259,301 rows
- **包含 Symbols**: 15+ (最近24小时)
- **时间范围**: 根据数据保留策略

### market_index_snapshots
- **用途**: 市场指数快照
- **Symbols**: SPY, QQQ 等
- **更新频率**: 定期更新
- **数据量**: 10 rows

### ai_trader_dash_metrics
- **用途**: AI Trader 监控指标
- **指标数**: 21 个
- **更新频率**: 每次运行后更新
- **今日数据**: 105 rows

### ai_trader_events
- **用途**: AI Trader 事件日志
- **事件类型**: brain (6,984), tool (1,355)
- **总计**: 8,339 events

---

**修复完成时间**: 2025-10-30 16:20:00 EST
**修复人**: Claude Code

🎉 **Dashboard 已完全修复并重新设计,现在可以正常显示数据了!**

**立即访问**: http://localhost:3000/d/financial-market-overview/financial-and-market-overview
