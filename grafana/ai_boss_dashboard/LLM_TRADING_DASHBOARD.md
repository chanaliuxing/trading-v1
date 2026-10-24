# LLM Trading Dashboard - 完整说明

**Status**: ✅ Deployed and Operational
**Date**: 2025-10-31 16:36 EST
**UID**: `llm-trading-dashboard`

---

## 🎯 Overview

完整的 LLM Trading System 数据可视化 Dashboard，基于 `llm.*` 数据库的所有表构建，实时展示交易系统的财务状况、交易执行、AI 决策和持仓详情。

---

## 📊 Dashboard Structure

### Row 1: 💰 Financial Overview (财务概览) - 4 Panels

| Panel | Type | Metrics | Data Source |
|-------|------|---------|-------------|
| **Total Equity** | Stat | 总权益（大数字） | `llm.llm_equity_timeseries` |
| **Cash Available** | Stat | 可用现金 | `llm.llm_equity_timeseries` |
| **Today's PnL** | Stat | 今日盈亏（背景色） | `llm.llm_daily_pnl_view` |
| **Open Positions** | Stat | 持仓数量 | `llm.llm_positions_snapshot` |

**数据状态**: ✅ 全部实时数据

---

### Row 2: 📈 Trade Execution (交易执行) - 3 Panels

| Panel | Type | Metrics | Data Source |
|-------|------|---------|-------------|
| **Today's Fills** | Table | 今日成交明细（时间/Symbol/数量/价格/费用） | `llm.llm_fills` |
| **Fill Stats** | Multi-Stat | 总成交数/总成交额/总手续费 | `llm.llm_fills` |
| **Fill Timeline** | Time Series | 成交时间分布图 | `llm.llm_fills` |

**今日数据**:
- ✅ 5 笔成交
- Symbols: NVDA, AAPL, TSLA
- 时间范围: 11:03 - 15:24

---

### Row 3: 🤖 AI Decision Analysis (AI 决策分析) - 3 Panels

| Panel | Type | Metrics | Data Source |
|-------|------|---------|-------------|
| **Decision Actions** | Pie Chart | 决策分布（BUY/SELL/HOLD） | `llm.llm_decisions.decision_json` |
| **Confidence Distribution** | Histogram | 置信度分布直方图 | `llm.llm_decisions.decision_json` |
| **Recent Decisions** | Table | 最近 20 条决策（时间/Symbol/Action/Confidence/Rationale） | `llm.llm_decisions` |

**今日数据**:
- ✅ 108 个决策
- 大部分为 HOLD（afterhours 保护）
- Confidence 范围: 0.0 - 1.0

**JSON 结构示例**:
```json
{
  "action": "BUY",
  "confidence": 0.85,
  "shares": 50,
  "rationale": "Strong upward momentum with increasing volume..."
}
```

---

### Row 4: 📦 Position Details (持仓详情) - 2 Panels

| Panel | Type | Metrics | Data Source |
|-------|------|---------|-------------|
| **Current Positions** | Table | 持仓明细（Symbol/股数/成本/当前价/盈亏/市值） | `llm.llm_positions_snapshot` |
| **Position Distribution** | Pie Chart | 持仓市值分布 | `llm.llm_positions_snapshot` |

**当前持仓**:
- ✅ NVDA: 100 股 @ $204.38
- ✅ AAPL: 50 股 @ $271.74
- ✅ TSLA: 11 股 @ $453.41

---

### Row 5: 📈 Equity Curve (权益曲线) - 2 Panels

| Panel | Type | Metrics | Data Source |
|-------|------|---------|-------------|
| **Equity Timeseries** | Time Series | 权益走势 + 现金走势 | `llm.llm_equity_timeseries` |
| **Drawdown Gauge** | Gauge | 当前回撤（仪表盘） | `llm.llm_equity_timeseries` |

**今日数据**:
- ✅ 69 个权益快照
- Equity: $50,000 (初始资金)
- Drawdown: 0% (today)

---

## 🎛️ Interactive Variables

Dashboard 提供两个交互式筛选变量：

| Variable | Type | Options | Purpose |
|----------|------|---------|---------|
| **Time Range** | Interval | 1h / 6h / Today / Week | 时间范围筛选 |
| **Symbol** | Query | All / AAPL / NVDA / TSLA / ... | 股票筛选 |

**使用方式**: 在 Dashboard 顶部下拉菜单选择

---

## 📊 Data Summary

### LLM Database Tables Used

| Table | Records (Today) | Purpose |
|-------|-----------------|---------|
| `llm.llm_equity_timeseries` | 69 | 权益时间序列 |
| `llm.llm_fills` | 5 | 交易填充记录 |
| `llm.llm_decisions` | 108 | AI 决策记录 |
| `llm.llm_positions_snapshot` | 10 | 持仓快照 |
| `llm.llm_daily_pnl_view` | 1 | 日盈亏视图 |
| `llm.llm_cash_ledger` | - | 现金流水（未使用） |
| `llm.llm_order_events` | - | 订单事件（未使用） |
| `llm.llm_sessions` | - | Session 信息（未使用） |

### Today's Trading Activity (2025-10-31)

**成交记录**:
```
15:24:04 - NVDA  100 股 @ $204.38 (Fee: $2.04)
15:01:08 - AAPL   50 股 @ $271.74 (Fee: $1.36)
12:26:48 - TSLA   11 股 @ $453.41 (Fee: $1.00)
11:37:46 - TSLA   11 股 @ $454.52 (Fee: $1.00)
11:03:37 - TSLA   10 股 @ $456.64 (Fee: $1.00)
```

**Total**:
- Fills: 5 笔
- Volume: ~$50,000 (各项成交总额)
- Fees: ~$6.40

---

## 🌐 Access Information

### URL
```
http://localhost:3000/d/llm-trading-dashboard/llm-trading-complete-overview
```

### Quick Access
```bash
open http://localhost:3000/d/llm-trading-dashboard
```

### File Location
```
./grafana/dashboards/active/llm_trading_dashboard.json
```

---

## 🔧 Dashboard Configuration

### Data Source
- **Type**: ClickHouse
- **UID**: `ch`
- **Host**: localhost:8123
- **Database**: `llm`

### Refresh Rate
- **Auto-refresh**: 30 seconds
- **Time Range**: Last 24 hours (default)
- **Timezone**: America/New_York

---

## 📈 Panel Details

### 完整 Panel 列表 (19 Components)

**Row 1: Financial Overview**
1. Total Equity (Stat) - 总权益大数字
2. Cash Available (Stat) - 可用现金
3. Today's PnL (Stat) - 今日盈亏（背景色）
4. Open Positions (Stat) - 持仓数量

**Row 2: Trade Execution**
5. Today's Fills (Table) - 今日成交表
6. Fill Stats (Multi-Stat) - 成交统计
7. Fill Timeline (Time Series) - 成交时间线

**Row 3: AI Decision Analysis**
8. Decision Actions (Pie Chart) - 决策分布
9. Confidence Distribution (Histogram) - 置信度分布
10. Recent Decisions (Table) - 最近决策表

**Row 4: Position Details**
11. Current Positions (Table) - 当前持仓表
12. Position Distribution (Pie Chart) - 持仓市值分布

**Row 5: Equity Curve**
13. Equity Timeseries (Time Series) - 权益走势图
14. Drawdown Gauge (Gauge) - 回撤仪表盘

---

## 🚀 Quick Start Queries

### 验证数据

```bash
# 1. 检查今日成交
curl "http://localhost:8123/" --data "SELECT count() FROM llm.llm_fills WHERE toDate(ts_ny) = today() FORMAT Pretty"

# 2. 检查今日决策
curl "http://localhost:8123/" --data "SELECT count() FROM llm.llm_decisions WHERE toDate(ts_ny) = today() FORMAT Pretty"

# 3. 检查当前持仓
curl "http://localhost:8123/" --data "SELECT symbol, anyLast(position) as pos FROM llm.llm_positions_snapshot GROUP BY symbol HAVING pos > 0 FORMAT Pretty"

# 4. 检查当前权益
curl "http://localhost:8123/" --data "SELECT anyLast(equity) FROM llm.llm_equity_timeseries WHERE toDate(ts_ny) = today() FORMAT Pretty"
```

---

## 🎯 Key Features

### ✅ Real-Time Monitoring
- 所有数据实时更新（30秒刷新）
- 无占位符 - 100% 真实数据

### ✅ Comprehensive Coverage
- 财务状况：Equity, Cash, PnL
- 交易执行：Fills, Volume, Fees
- AI 决策：Actions, Confidence, Rationale
- 持仓管理：Positions, Distribution, PnL

### ✅ Interactive Filtering
- 按时间范围筛选
- 按股票 Symbol 筛选
- 支持查看历史数据

### ✅ Visual Clarity
- 颜色编码（绿色盈利/红色亏损）
- 多种图表类型（Table/Chart/Gauge/Pie）
- 清晰的中文标签

---

## 📝 Notes

### 数据完整性
- ✅ 所有数据来自 ClickHouse `llm` 数据库
- ✅ Gemini 分析确认数据存在并正确
- ✅ 今日（2025-10-31）有完整的交易记录

### 性能优化
- 使用 `anyLast()` 优化查询性能
- 限制表格行数（Recent Decisions: 20 rows, Fills: 50 rows）
- 高效的时间筛选（`toDate(ts_ny) = today()`）

### Future Enhancements
可以添加（如果需要）：
- Session 分时段分析（Premarket/Intraday/Afterhours/Overnight）
- 订单事件流程（使用 `llm.llm_order_events`）
- 现金流水明细（使用 `llm.llm_cash_ledger`）
- 策略对比（如有多策略）

---

## 🏆 Success Criteria

### ✅ Deployment Complete
- [x] Dashboard JSON 生成
- [x] 部署到 Grafana active 目录
- [x] Grafana 自动加载成功
- [x] 所有 19 panels 正常显示
- [x] 数据实时刷新

### ✅ Data Validation
- [x] 今日成交：5 笔（NVDA, AAPL, TSLA）
- [x] 今日决策：108 个
- [x] 当前持仓：3 个 symbols
- [x] 权益快照：69 条记录

---

## 📞 Support

### Documentation
- **生成脚本**: `./grafana/ai_boss_dashboard/generate_llm_trading_dashboard.py`
- **Dashboard JSON**: `./grafana/dashboards/active/llm_trading_dashboard.json`
- **This Guide**: `./grafana/ai_boss_dashboard/LLM_TRADING_DASHBOARD.md`

### Quick Commands

```bash
# 重新生成 dashboard
cd ./grafana/ai_boss_dashboard
python3 generate_llm_trading_dashboard.py

# 查看 Grafana 日志
docker logs trading_grafana --tail=50

# 重启 Grafana（如需要）
docker restart trading_grafana

# 验证 ClickHouse 连接
curl "http://localhost:8123/?query=SELECT 1 FORMAT Pretty"
```

---

**Status**: ✅ **OPERATIONAL**
**Last Updated**: 2025-10-31 16:36 EST
**Dashboard URL**: http://localhost:3000/d/llm-trading-dashboard
**Created By**: Claude Code
