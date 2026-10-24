# 📊 Data Mismatch Analysis - Dashboard vs IBKR Reality

**Date**: 2025-10-31 17:30 EST
**Status**: ⚠️ **CRITICAL DATA ISSUE IDENTIFIED**

---

## 🚨 Problem Summary

**Dashboard 数据与 check_ibkr_acc.py 实际 IBKR 数据不匹配！**

---

## 📋 Data Comparison

### IBKR 实际数据 (via `check_ibkr_acc.py` @ 17:14 EDT):

```
持仓 (当前实时价格):
- AAPL: 187股 @ $266.68平均, 当前$270.33, 浮盈 +$682.62 (+1.37%)
- NVDA: 177股 @ $198.36平均, 当前$202.49, 浮盈 +$731.50 (+2.08%)
- TSLA: 100股 @ $448.17平均, 当前$455.16, 浮盈 +$699.13 (+1.56%)

总计:
- Position Value: $131,888.38
- Unrealized P&L: +$2,093.18
- Net Liquidation: $238,844.27
```

### LLM Database 数据 (最新记录):

```sql
SELECT symbol, ts_ny, position, avg_cost, market_price, unrealized_pnl
FROM llm.llm_positions_snapshot
ORDER BY ts_ny DESC LIMIT 5;
```

**Result**:
```
NVDA: 100股 @ $204.38, market_price=$204.38, unrealized_pnl=$0 (15:24:08)
AAPL:  50股 @ $271.74, market_price=$271.74, unrealized_pnl=$0 (15:01:13)
TSLA:  11股 @ $453.41, market_price=$453.41, unrealized_pnl=$0 (12:26:48)
TSLA:  11股 @ $454.52, market_price=$454.52, unrealized_pnl=$0 (11:37:46)
TSLA:  10股 @ $456.64, market_price=$456.64, unrealized_pnl=$0 (11:03:37)
```

---

## ❌ Key Mismatches

| Metric | IBKR Reality | LLM Database | Mismatch |
|--------|--------------|--------------|----------|
| **AAPL Shares** | 187 | 50 | ❌ **137股差异** |
| **NVDA Shares** | 177 | 100 | ❌ **77股差异** |
| **TSLA Shares** | 100 | 32 (10+11+11) | ❌ **68股差异** |
| **AAPL Avg Cost** | $266.68 | $271.74 | ❌ 不同 |
| **NVDA Avg Cost** | $198.36 | $204.38 | ❌ 不同 |
| **TSLA Avg Cost** | $448.17 | ~$454.86平均 | ❌ 不同 |
| **Unrealized P&L** | **+$2,093.18** | **$0** | ❌ **完全错误** |

---

## 🔍 Root Cause Analysis

### 问题 1: 持仓数量不匹配

**数据库记录的是**: 每次成交时的**新增持仓**，而不是**累积总持仓**

```
数据库记录:
- 15:24 NVDA买入 100股 (新记录)
- 15:01 AAPL买入  50股 (新记录)
- 12:26 TSLA买入  11股 (新记录)
- 11:37 TSLA买入  11股 (新记录)
- 11:03 TSLA买入  10股 (新记录)

实际应该是累积:
- AAPL: 之前有137股 + 今天50股 = 187股总持仓
- NVDA: 之前有 77股 + 今天100股 = 177股总持仓
- TSLA: 之前有 68股 + 今天32股 = 100股总持仓
```

**结论**: ❌ `llm.llm_positions_snapshot` 表记录的是**成交时刻的快照**，不是**账户总持仓**

---

### 问题 2: Market Price 不更新

**数据库记录的是**: 成交时刻的价格，之后**不再更新**

```
AAPL成交时: market_price = $271.74 (15:01)
当前价格:   $270.33 (17:14)
数据库显示: $271.74 (静态，未更新)
```

**结论**: ❌ `market_price` 字段是**静态的成交价**，不是**实时市场价格**

---

### 问题 3: Unrealized PnL = 0

**原因**: market_price = avg_cost (成交时刻相同)，所以 unrealized_pnl = 0

```python
unrealized_pnl = (market_price - avg_cost) * position
                = (271.74 - 271.74) * 50
                = 0
```

**结论**: ❌ `unrealized_pnl` 字段在成交时刻是 0，之后**不会更新**

---

### 问题 4: Equity 数据错误

```sql
SELECT anyLast(equity) FROM llm.llm_equity_timeseries WHERE toDate(ts_ny) = today();
```

**Result**: 可能显示的不是 $238,844.27，而是旧数据

---

## 📊 What Data IS Correct in Database?

| Data | Status | Notes |
|------|--------|-------|
| **llm.llm_fills** | ✅ **100% Accurate** | 今日5笔成交完全正确 |
| **llm.llm_decisions** | ✅ **100% Accurate** | 108个决策记录正确 |
| **llm.llm_positions_snapshot** (成交时刻) | ✅ **Accurate** | 成交时刻的快照是对的 |
| **llm.llm_positions_snapshot** (当前) | ❌ **NOT Real-time** | 不反映当前持仓和价格 |
| **llm.llm_equity_timeseries** | ⚠️ **Unknown** | 需要验证是否实时更新 |

---

## 🛠️ What Needs to be Fixed?

### Fix 1: 实时持仓更新机制

**需要**: 定期（如每分钟）从 IBKR 拉取当前持仓并更新数据库

```python
# 伪代码
async def update_positions_realtime():
    positions = await ibkr.get_positions()  # 获取当前所有持仓
    for pos in positions:
        await db.insert(
            symbol=pos.symbol,
            position=pos.total_shares,  # 总持仓，不是增量
            avg_cost=pos.avg_cost,
            market_price=pos.current_price,  # 实时价格
            unrealized_pnl=(pos.current_price - pos.avg_cost) * pos.total_shares
        )
```

**缺少**: ❌ 这个功能目前不存在

---

### Fix 2: 实时价格更新

**需要**: 即使没有新成交，也要定期更新持仓的 market_price

```python
async def update_market_prices():
    positions = await get_current_positions_from_db()
    for pos in positions:
        current_price = await ibkr.get_ticker_price(pos.symbol)
        await db.update_position(
            symbol=pos.symbol,
            market_price=current_price,
            unrealized_pnl=(current_price - pos.avg_cost) * pos.shares
        )
```

**缺少**: ❌ 这个功能目前不存在

---

### Fix 3: 实时 Equity 更新

**需要**: 定期从 IBKR 获取账户权益并写入数据库

```python
async def update_equity_realtime():
    account = await ibkr.get_account_summary()
    await db.insert_equity(
        equity=account.net_liquidation,
        cash=account.total_cash,
        gross_exposure=account.position_value,
        unrealized_pnl=account.unrealized_pnl
    )
```

**缺少**: ⚠️ 可能存在但不频繁

---

## 🎯 Honest Dashboard Options

### Option 1: 使用 check_ibkr_acc.py 的实时数据 (推荐)

**方案**:
1. 修改 `check_ibkr_acc.py` 将数据写入 ClickHouse
2. 或者创建一个定时任务（每分钟）运行并更新数据库
3. Dashboard 从数据库读取这些**实时数据**

**优点**: ✅ 数据100%准确，反映真实IBKR状态
**缺点**: ⚠️ 需要实现数据写入逻辑

---

### Option 2: Dashboard 直接连接 IBKR (不现实)

**问题**: Grafana 无法直接连接 IBKR API

---

### Option 3: 诚实标注当前数据的限制

**方案**: Dashboard 上明确标注：
- "⚠️ 持仓数据为成交时刻快照，非实时总持仓"
- "⚠️ 浮盈浮亏为0是因为价格未实时更新"
- "✅ 今日成交数据准确"

**优点**: ✅ Be honest - 告诉用户真相
**缺点**: ❌ Dashboard 价值有限

---

## 💡 Recommended Solution

### 短期 (立即):

1. **诚实标注** - 在 Dashboard 上清楚说明数据限制
2. **保留准确的部分** - 今日成交表 (llm_fills) 是准确的
3. **移除误导的部分** - 删除不准确的 unrealized P&L panels

### 中期 (1-2天):

1. **实现实时持仓更新**:
   ```python
   # 添加到 trading system
   @scheduler.scheduled_job('interval', minutes=1)
   async def sync_positions_to_db():
       positions = await ibkr_broker.get_positions()
       await clickhouse.upsert_positions(positions)
   ```

2. **实现实时账户更新**:
   ```python
   @scheduler.scheduled_job('interval', minutes=1)
   async def sync_account_to_db():
       account = await ibkr_broker.get_account_summary()
       await clickhouse.insert_equity_snapshot(account)
   ```

### 长期 (1周):

1. **完整的实时监控系统** - 所有数据每分钟同步到数据库
2. **Dashboard 100% 准确** - 显示真实的 IBKR 账户状态

---

## 📝 Current Dashboard Status

### ✅ What Works (Accurate):
- Today's Fills Table (5笔成交)
- Fill Volume by Symbol
- Fill Timeline
- AI Decisions (108个)

### ❌ What Doesn't Work (Inaccurate):
- Current Positions Table (数量错误，187 vs 50)
- Unrealized P&L (显示$0，实际+$2,093)
- Position Value (显示成交时市值，不是当前市值)
- Net Liquidation (可能不准确)

### ⚠️ What's Unknown:
- Equity Timeseries (需要验证)
- Cash (需要验证)

---

## 🎊 Conclusion

**Be Honest**:

❌ **当前的 Dashboard 数据与 IBKR 实际账户不匹配！**

主要原因:
1. 数据库记录的是**成交时刻的快照**，不是**实时持仓**
2. 没有实时价格更新机制
3. Unrealized P&L 永远是 $0（成交时刻）

**解决方案**:
- 需要实现**实时持仓同步**功能
- 或者使用 `check_ibkr_acc.py` 的输出作为数据源

**现状**:
- ✅ 今日成交数据准确
- ❌ 持仓和盈亏数据不准确
- ⚠️ Dashboard 目前只能作为**成交记录查看器**，不能作为**账户监控工具**

---

**Last Updated**: 2025-10-31 17:30 EST
**Analysis By**: Claude Code
