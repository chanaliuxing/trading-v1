# 🚨 Critical Metrics Implementation Guide

**Priority**: P0 - HIGHEST
**Target**: 最核心的财务指标
**Date**: 2025-10-30 15:00 EDT

---

## 📋 Overview

您完全正确!这些是**最重要**的指标:

1. **总共有多少钱** (Total Equity)
2. **分时段盈亏** (Session PnL):
   - 盘前 (Premarket)
   - 盘中 (Intraday)
   - 盘后 (Afterhours)
   - 隔夜 (Overnight)

这些指标已添加到 Dashboard,但需要实盘交易数据才能显示。

---

## 💰 Metric 1: Total Equity (总资金) - THE MOST IMPORTANT!

### 为什么重要
这是**最核心**的指标 - 告诉您账户现在有多少钱!

### 当前状态
🟡 **Placeholder** - 等待 `equity_pnl_snapshots` 表数据

### Dashboard 位置
- **Row 1, Position 1** (最显眼的位置!)
- **Type**: Stat panel (大数字显示)
- **Size**: 8 grid width x 4 grid height

### 所需表结构

```sql
CREATE TABLE IF NOT EXISTS trading.equity_pnl_snapshots (
    date Date,
    ts DateTime('America/New_York'),  -- 使用 EST timezone
    equity Float64,                    -- 总权益 (现金 + 持仓市值)
    cash Float64,                      -- 可用现金
    realized_pnl Float64,              -- 已实现盈亏
    unrealized_pnl Float64,            -- 未实现盈亏
    buying_power Float64,              -- 购买力
    margin_used Float64                -- 已用保证金
) ENGINE = ReplacingMergeTree()
PARTITION BY toYYYYMM(date)
ORDER BY (date, ts);
```

### 实施代码

#### Step 1: 在 IBKR Broker 中添加账户监控

**文件**: `src/brokers/ibkr/account_monitor.py` (如果不存在则创建)

```python
import asyncio
from datetime import datetime
from typing import Dict, Any
from clickhouse_driver import Client as ClickHouseClient

class AccountMonitor:
    """Monitor account equity and log to ClickHouse"""

    def __init__(self, broker, clickhouse_client: ClickHouseClient):
        self.broker = broker
        self.ch = clickhouse_client
        self.running = False

    async def start(self):
        """Start monitoring account equity"""
        self.running = True
        logger.info("AccountMonitor started")

        while self.running:
            try:
                await self.log_equity_snapshot()
                await asyncio.sleep(60)  # Log every minute
            except Exception as e:
                logger.error(f"Error logging equity: {e}", exc_info=True)
                await asyncio.sleep(60)

    async def log_equity_snapshot(self):
        """Get account value and log to ClickHouse"""

        # Get account summary from IBKR
        account_summary = await self.broker.get_account_summary()

        # Extract values
        equity = float(account_summary.get('NetLiquidation', 0))
        cash = float(account_summary.get('TotalCashValue', 0))
        realized_pnl = float(account_summary.get('RealizedPnL', 0))
        unrealized_pnl = float(account_summary.get('UnrealizedPnL', 0))
        buying_power = float(account_summary.get('BuyingPower', 0))
        margin_used = float(account_summary.get('InitMarginReq', 0))

        # Insert to ClickHouse
        query = """
            INSERT INTO trading.equity_pnl_snapshots
            (date, ts, equity, cash, realized_pnl, unrealized_pnl,
             buying_power, margin_used)
            VALUES
        """

        values = [(
            datetime.now().date(),
            datetime.now(),
            equity,
            cash,
            realized_pnl,
            unrealized_pnl,
            buying_power,
            margin_used
        )]

        self.ch.execute(query, values)

        logger.debug(
            f"Logged equity snapshot: ${equity:,.2f} "
            f"(Cash: ${cash:,.2f}, PnL: ${realized_pnl + unrealized_pnl:,.2f})"
        )

    def stop(self):
        """Stop monitoring"""
        self.running = False
        logger.info("AccountMonitor stopped")
```

#### Step 2: 启动 AccountMonitor

**文件**: `src/main.py` 或 trading system 启动文件

```python
from brokers.ibkr.account_monitor import AccountMonitor

async def main():
    # ... existing setup ...

    # Initialize broker
    broker = IBKRBroker(...)
    await broker.connect()

    # Initialize ClickHouse
    ch_client = ClickHouseClient(host='localhost', port=9000, database='trading')

    # Start Account Monitor
    account_monitor = AccountMonitor(broker, ch_client)
    monitor_task = asyncio.create_task(account_monitor.start())

    # ... rest of trading system ...

    try:
        await asyncio.gather(
            monitor_task,
            # ... other tasks ...
        )
    finally:
        account_monitor.stop()
```

### Dashboard Query (已配置)

```sql
SELECT
  anyLast(equity) as total_equity
FROM trading.equity_pnl_snapshots
WHERE date = today()
```

### 验证

```bash
# 1. 检查数据是否写入
curl "http://localhost:8123/?database=trading&query=SELECT count() FROM equity_pnl_snapshots WHERE date = today()"

# 2. 查看最新记录
curl "http://localhost:8123/?database=trading&query=SELECT * FROM equity_pnl_snapshots ORDER BY ts DESC LIMIT 5 FORMAT Pretty"

# 3. 测试 Dashboard query
curl "http://localhost:8123/?database=trading&query=SELECT anyLast(equity) as total_equity FROM equity_pnl_snapshots WHERE date = today() FORMAT Pretty"
```

### 预期结果

一旦实施,Dashboard 将显示:
- 💰 **大数字**: 当前总权益 (e.g., $523,450.75)
- **背景色**: 绿色 (盈利) / 红色 (亏损)
- **趋势图**: 24小时权益曲线

---

## 📊 Metric 2: Session PnL (分时段盈亏)

### 为什么重要
了解**什么时段赚钱,什么时段亏钱** - 优化交易时间!

### 当前状态
🟡 **Placeholder** - 等待 `session_pnl` 表数据

### Dashboard 位置
- **Row 1, Position 2** (紧跟总资金后面)
- **Type**: Multi-stat panel (4个session并排显示)
- **Size**: Full width (24 grid)

### Session 定义

| Session | 中文 | 时间 (EST) | 描述 |
|---------|------|-----------|------|
| **premarket** | 盘前 | 04:00 - 09:30 | 开盘前交易 |
| **intraday** | 盘中 | 09:30 - 16:00 | 正常交易时段 |
| **afterhours** | 盘后 | 16:00 - 20:00 | 收盘后交易 |
| **overnight** | 隔夜 | 20:00 - 04:00 | 夜间持仓 |

### 所需表结构

```sql
CREATE TABLE IF NOT EXISTS trading.session_pnl (
    date Date,
    ts DateTime('America/New_York'),
    session Enum8('premarket' = 1, 'intraday' = 2, 'afterhours' = 3, 'overnight' = 4),
    pnl Float64,              -- 该时段总盈亏
    trades UInt32,            -- 该时段交易次数
    winning_trades UInt32,    -- 盈利交易数
    losing_trades UInt32,     -- 亏损交易数
    largest_win Float64,      -- 最大单笔盈利
    largest_loss Float64      -- 最大单笔亏损
) ENGINE = MergeTree()
PARTITION BY toYYYYMM(date)
ORDER BY (date, session, ts);
```

### 实施方案 (2 种方式)

#### 方案 A: 从 trades 表计算 (推荐)

如果您已经记录了每笔交易的时间和盈亏:

```python
async def calculate_session_pnl(date: datetime.date):
    """Calculate PnL by session from trades"""

    query = """
        INSERT INTO trading.session_pnl
        SELECT
            date,
            now() as ts,
            CASE
                WHEN toHour(ts, 'America/New_York') >= 4 AND toHour(ts, 'America/New_York') < 9.5 THEN 'premarket'
                WHEN toHour(ts, 'America/New_York') >= 9.5 AND toHour(ts, 'America/New_York') < 16 THEN 'intraday'
                WHEN toHour(ts, 'America/New_York') >= 16 AND toHour(ts, 'America/New_York') < 20 THEN 'afterhours'
                ELSE 'overnight'
            END as session,
            sum(pnl) as pnl,
            count() as trades,
            countIf(pnl > 0) as winning_trades,
            countIf(pnl < 0) as losing_trades,
            max(pnl) as largest_win,
            min(pnl) as largest_loss
        FROM trading.trade_pnl
        WHERE date = ?
        GROUP BY date, session
    """

    await ch_client.execute(query, [date])
```

#### 方案 B: 实时累积 (更准确)

在交易系统中实时记录:

```python
class SessionPnLTracker:
    """Track PnL by session in real-time"""

    def __init__(self, ch_client):
        self.ch = ch_client
        self.current_session_pnl = {
            'premarket': 0.0,
            'intraday': 0.0,
            'afterhours': 0.0,
            'overnight': 0.0
        }
        self.session_trades = {s: 0 for s in self.current_session_pnl}

    def get_current_session(self) -> str:
        """Determine current trading session"""
        now = datetime.now(ZoneInfo("America/New_York"))
        hour = now.hour + now.minute / 60.0

        if 4 <= hour < 9.5:
            return 'premarket'
        elif 9.5 <= hour < 16:
            return 'intraday'
        elif 16 <= hour < 20:
            return 'afterhours'
        else:
            return 'overnight'

    async def record_trade_pnl(self, pnl: float):
        """Record a trade's PnL to current session"""
        session = self.get_current_session()
        self.current_session_pnl[session] += pnl
        self.session_trades[session] += 1

        # Log to ClickHouse
        await self.log_session_snapshot(session)

    async def log_session_snapshot(self, session: str):
        """Log current session PnL snapshot"""
        query = """
            INSERT INTO trading.session_pnl
            (date, ts, session, pnl, trades, winning_trades, losing_trades)
            VALUES
        """

        values = [(
            datetime.now().date(),
            datetime.now(),
            session,
            self.current_session_pnl[session],
            self.session_trades[session],
            0,  # Can track these separately if needed
            0
        )]

        await self.ch.execute(query, values)

    async def end_of_session_summary(self, session: str):
        """Log final session summary"""
        logger.info(
            f"Session {session} ended: "
            f"PnL ${self.current_session_pnl[session]:,.2f}, "
            f"Trades: {self.session_trades[session]}"
        )

        await self.log_session_snapshot(session)

        # Reset for next day if overnight session ends
        if session == 'overnight':
            self.current_session_pnl = {s: 0.0 for s in self.current_session_pnl}
            self.session_trades = {s: 0 for s in self.session_trades}
```

### Dashboard Queries (已配置)

```sql
-- Premarket PnL
SELECT sum(pnl) as session_pnl
FROM trading.session_pnl
WHERE date = today() AND session = 'premarket';

-- Intraday PnL
SELECT sum(pnl) as session_pnl
FROM trading.session_pnl
WHERE date = today() AND session = 'intraday';

-- Afterhours PnL
SELECT sum(pnl) as session_pnl
FROM trading.session_pnl
WHERE date = today() AND session = 'afterhours';

-- Overnight PnL
SELECT sum(pnl) as session_pnl
FROM trading.session_pnl
WHERE date = today() AND session = 'overnight';
```

### 验证

```bash
# 1. 检查数据
curl "http://localhost:8123/?database=trading&query=SELECT session, sum(pnl) as total_pnl FROM session_pnl WHERE date = today() GROUP BY session FORMAT Pretty"

# 2. 查看详情
curl "http://localhost:8123/?database=trading&query=SELECT * FROM session_pnl WHERE date = today() ORDER BY ts DESC FORMAT Pretty"
```

### 预期结果

Dashboard 将显示 4 个并排的 stat panels:
- 📈 **Premarket**: $+2,450.50 (green)
- 📈 **Intraday**: $+15,320.75 (green)
- 📉 **Afterhours**: $-850.25 (red)
- 📊 **Overnight**: $+450.00 (yellow)

---

## 🚀 Quick Start Implementation

### 最快实施路径 (30分钟)

```bash
# 1. 创建表
curl "http://localhost:8123/?database=trading" --data-binary "
CREATE TABLE IF NOT EXISTS equity_pnl_snapshots (
    date Date,
    ts DateTime,
    equity Float64,
    cash Float64,
    realized_pnl Float64,
    unrealized_pnl Float64,
    buying_power Float64,
    margin_used Float64
) ENGINE = ReplacingMergeTree()
ORDER BY (date, ts);

CREATE TABLE IF NOT EXISTS session_pnl (
    date Date,
    ts DateTime,
    session Enum8('premarket' = 1, 'intraday' = 2, 'afterhours' = 3, 'overnight' = 4),
    pnl Float64,
    trades UInt32,
    winning_trades UInt32,
    losing_trades UInt32,
    largest_win Float64,
    largest_loss Float64
) ENGINE = MergeTree()
ORDER BY (date, session, ts);
"

# 2. 创建 account_monitor.py (see code above)

# 3. 修改 main.py 启动 monitor (see code above)

# 4. 创建 session_pnl_tracker.py (see code above)

# 5. 在交易系统中集成 SessionPnLTracker

# 6. 重启交易系统

# 7. 验证数据流入

# 8. 刷新 Grafana Dashboard
```

---

## 📋 Implementation Checklist

### Phase 1: Total Equity (最快 - 15分钟)
- [ ] Create `equity_pnl_snapshots` table in ClickHouse
- [ ] Create `AccountMonitor` class
- [ ] Start `AccountMonitor` in main trading loop
- [ ] Verify data flows to ClickHouse
- [ ] Refresh Grafana dashboard
- [ ] ✅ See total equity display!

### Phase 2: Session PnL (20分钟)
- [ ] Create `session_pnl` table in ClickHouse
- [ ] Choose implementation: real-time tracker OR calculate from trades
- [ ] Implement chosen method
- [ ] Integrate into trading system
- [ ] Verify session PnL data
- [ ] Refresh Grafana dashboard
- [ ] ✅ See session breakdown!

---

## 🎯 Success Criteria

### When Complete, You Will See:

1. **Top of Dashboard**:
   - 💰 **$523,450.75** (Your total equity in BIG numbers)
   - 📈 Equity curve showing today's journey

2. **Below That**:
   - 📊 **Premarket**: $+2,450 (green)
   - 📊 **Intraday**: $+15,320 (green)
   - 📊 **Afterhours**: $-850 (red)
   - 📊 **Overnight**: $+450 (yellow)

3. **Instant Answers**:
   - "我有多少钱?" → 一眼看到!
   - "哪个时段赚钱?" → 一目了然!

---

## 📞 Need Help?

如有问题:
1. 查看 `PLACEHOLDER_IMPLEMENTATION_GUIDE.md` - 详细实施指南
2. 检查 ClickHouse 连接: `docker ps | grep clickhouse`
3. 查看表结构: `DESCRIBE TABLE equity_pnl_snapshots`
4. 测试查询: 见上面的 "验证" 部分

---

**Priority**: 🚨 P0 - CRITICAL - HIGHEST
**Estimated Time**: 30-45 minutes
**Impact**: 立即可见账户状态和分时段表现!

**Last Updated**: 2025-10-30 15:00 EDT
