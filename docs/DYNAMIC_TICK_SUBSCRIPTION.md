# Dynamic Tick Data Subscription - User Guide

## 概述

本文档介绍**动态 Tick 数据订阅功能**，允许策略在运行时动态订阅新股票的原始 tick 数据，无需重启系统。

---

## 功能特性

### ✅ 支持的功能

| 功能 | 说明 |
|------|------|
| **运行时订阅** | 策略可在 `initialize()` 或 `on_features()` 中随时订阅 |
| **线程安全** | 使用 `asyncio.Lock` 保护，支持并发调用 |
| **自动聚合** | SFS-1s 自动将 tick 聚合为 1秒 bar 存入 Redis |
| **订阅确认** | 返回每个 symbol 的订阅结果 (success/failure) |
| **去重保护** | 重复订阅同一 symbol 会自动跳过 |

### ⚠️ 限制条件

| 限制 | 说明 | 影响 |
|------|------|------|
| **IBKR 延迟** | 订阅后 2-5 秒才有数据 | 策略需要等待 |
| **连接限制** | 免费账户 ~100 个并发订阅 | 超过会失败 |
| **不持久化** | 重启后需要重新订阅 | 策略需在 `initialize()` 中订阅 |
| **需要连接** | IB Gateway 断线时无法订阅 | 等待重连后自动恢复 |

---

## API 文档

### 策略层 API

#### `ctx.subscribe_tick_data(symbols: List[str]) -> Dict[str, bool]`

**用途**：动态订阅原始 tick 数据（触发 IB 订阅 + Kafka 发布）

**参数**：
- `symbols`: 股票代码列表，例如 `["NVDA", "AMD"]`

**返回**：
- `Dict[str, bool]`: 每个 symbol 的订阅结果
  - `True`: 订阅成功
  - `False`: 订阅失败

**示例**：
```python
async def initialize(self):
    # 订阅多个股票
    results = await self.ctx.subscribe_tick_data(["NVDA", "AMD", "INTC"])

    for symbol, success in results.items():
        if success:
            self.log.info(f"✅ {symbol} 订阅成功")
        else:
            self.log.error(f"❌ {symbol} 订阅失败")
```

---

## 使用场景

### 场景 1：盘前扫描 + 动态订阅

```python
class PremarketScanner(StrategyAlgorithm):
    async def initialize(self):
        # 初始订阅 SPY 作为市场基准
        await self.ctx.subscribe_features(["SPY"], bar_sec=60)

    async def on_features(self, symbol: str, features: Dict):
        if symbol == "SPY":
            # 扫描 gap up 股票
            gappers = await self.scan_gappers()  # 自定义扫描逻辑

            # 动态订阅发现的股票
            if gappers:
                self.log.info(f"发现 {len(gappers)} 个 gap up 股票")
                results = await self.ctx.subscribe_tick_data(gappers)

                self.tracked_symbols = [s for s, ok in results.items() if ok]
```

### 场景 2：微观结构分析

```python
class MicrostructureStrategy(StrategyAlgorithm):
    async def initialize(self):
        # 订阅 1 分钟 bar（宏观）
        await self.ctx.subscribe_features(["AAPL"], bar_sec=60)

        # 订阅 tick 数据（微观）
        await self.ctx.subscribe_tick_data(["AAPL"])

        # 初始化 SFS reader
        from libs.dal.sfs_facade import SFS1sReader
        self.sfs = SFS1sReader()

    async def on_features(self, symbol: str, features: Dict):
        # 读取最近 30 秒的 1 秒 bar
        subbars = await self.sfs.last_n(symbol, n=30)

        # 分析订单流
        upticks = sum(1 for i in range(1, len(subbars))
                     if subbars[i]["close"] > subbars[i-1]["close"])

        uptick_ratio = upticks / max(len(subbars) - 1, 1)

        if uptick_ratio > 0.7:
            self.log.info(f"{symbol} 强势上涨，买入信号")
            # 提交订单...
```

### 场景 3：基于新闻事件的动态订阅

```python
class NewsEventStrategy(StrategyAlgorithm):
    async def initialize(self):
        # 监听新闻事件
        self.news_queue = asyncio.Queue()

    async def on_news_alert(self, ticker: str, headline: str):
        """外部新闻回调"""
        self.log.info(f"新闻: {ticker} - {headline}")

        # 动态订阅相关股票
        result = await self.ctx.subscribe_tick_data([ticker])

        if result[ticker]:
            self.log.info(f"✅ 已订阅 {ticker} 的 tick 数据")
```

---

## 数据流架构

```
策略调用 ctx.subscribe_tick_data(["NVDA"])
    ↓
RealEngine.subscribe_tick_data()
    ↓
MarketDataService.subscribe_symbol()
    ↓
IBMarketDataAdapter.subscribe_symbol()
    ├─ 检查连接状态
    ├─ 检查是否已订阅
    ├─ 添加到 symbols 列表（使用锁保护）
    └─ 调用 IB reqMktData()
        ↓
    IB Gateway 开始发送 tick 数据（2-5 秒延迟）
        ↓
    IBMarketDataAdapter._on_ticker_update()
        ↓
    MarketDataService._on_tick()
        ├─ 标记 is_rth 字段（动态判断）
        └─ 发布到 Kafka (md.tick.trade.v1)
            ↓
        SFS-1s 服务消费 Kafka
            └─ 聚合为 1秒 bar → Redis (sfs:1s:NVDA)
                ↓
            策略使用 SFS1sReader 读取
```

---

## 与 `subscribe_features()` 的区别

| 特性 | `subscribe_features()` | `subscribe_tick_data()` |
|------|----------------------|----------------------|
| **数据源** | Kafka 聚合 bar | IB Gateway 原始 tick |
| **触发订阅** | ❌ 否（只消费 Kafka） | ✅ 是（触发 IB reqMktData） |
| **数据延迟** | 取决于 bar 周期（5s/60s） | ~100-500ms |
| **数据粒度** | 聚合 OHLCV | Tick-by-tick |
| **存储位置** | ClickHouse | Redis (TTL=3600s) |
| **用途** | 策略决策（宏观） | 微观结构分析 |

**推荐用法**：
- **宏观趋势**：使用 `subscribe_features()` 订阅 1 分钟或 5 分钟 bar
- **微观执行**：使用 `subscribe_tick_data()` + SFS1sReader 分析 order flow

---

## 错误处理

### 订阅失败的常见原因

| 错误 | 原因 | 解决方案 |
|------|------|---------|
| `IB not connected` | IB Gateway 断线 | 等待重连或检查 Gateway |
| `Symbol already subscribed` | 重复订阅 | 忽略（已订阅） |
| `Failed to qualify contract` | Symbol 不存在或不可交易 | 检查 symbol 拼写 |
| `Subscription limit exceeded` | 超过 IBKR 限制 | 取消不需要的订阅 |

### 错误处理示例

```python
async def initialize(self):
    symbols = ["AAPL", "INVALID_SYMBOL", "NVDA"]
    results = await self.ctx.subscribe_tick_data(symbols)

    # 检查失败的订阅
    failed = [s for s, ok in results.items() if not ok]
    if failed:
        self.log.error(f"以下股票订阅失败: {failed}")

    # 只使用成功订阅的股票
    self.active_symbols = [s for s, ok in results.items() if ok]
    self.log.info(f"活跃股票: {self.active_symbols}")
```

---

## 监控与调试

### 验证订阅是否成功

#### 1. 检查日志
```bash
tail -f logs/trader.log | grep -E "Dynamic subscription|tick subscription"
```

**预期输出**：
```
✅ Dynamic subscription successful symbol=NVDA total_subscriptions=5
✅ Strategy subscribed to tick data strategy_id=test_001 symbol=NVDA
```

#### 2. 检查 Kafka tick 数据
```bash
docker exec -it trading_kafka kafka-console-consumer \
  --bootstrap-server localhost:9092 \
  --topic md.tick.trade.v1 \
  --from-beginning | jq '.data | select(.symbol=="NVDA") | {symbol, price, is_rth}'
```

**预期输出**：
```json
{
  "symbol": "NVDA",
  "price": 135.42,
  "is_rth": 1
}
```

#### 3. 检查 Redis 1秒 bar
```bash
redis-cli LINDEX "sfs:1s:NVDA" 0
```

**预期输出**（JSON 字符串）：
```json
{
  "ts": 1730000000,
  "open": 135.40,
  "high": 135.45,
  "low": 135.38,
  "close": 135.42,
  "volume": 1200,
  "trades": 8
}
```

#### 4. 查看当前订阅列表
```python
# 在策略中
subscribed = await self.ctx.engine.market_data_service.get_subscribed_symbols()
self.log.info(f"当前订阅: {subscribed}")
```

---

## 性能考虑

### 内存占用

| 组件 | 每个 symbol | 估算（10 symbols） |
|------|------------|------------------|
| **IB 连接** | ~10 KB | ~100 KB |
| **Redis SFS-1s** | ~360 KB (1小时) | ~3.6 MB |
| **Kafka buffer** | ~50 KB/秒 | ~500 KB/秒 |

**建议**：
- 限制同时订阅数量 < 50 个（免费账户）
- Redis TTL 设置为 3600秒（1小时）足够

### CPU 占用

- 每个 tick: ~0.1ms 处理时间
- 活跃股票（100 tick/s）: ~1% CPU
- 10 个活跃股票: ~10% CPU

---

## 最佳实践

### ✅ 推荐做法

1. **在 initialize() 中订阅**
   ```python
   async def initialize(self):
       # 确保订阅在策略启动时完成
       await self.ctx.subscribe_tick_data(self.watchlist)
   ```

2. **检查订阅结果**
   ```python
   results = await self.ctx.subscribe_tick_data(symbols)
   self.active_symbols = [s for s, ok in results.items() if ok]
   ```

3. **设置订阅超时**
   ```python
   # 等待 5 秒确保数据开始流动
   await asyncio.sleep(5)
   ```

4. **限制订阅数量**
   ```python
   MAX_SUBSCRIPTIONS = 50
   if len(self.active_symbols) >= MAX_SUBSCRIPTIONS:
       self.log.warning("达到订阅上限")
   ```

### ❌ 避免的做法

1. **不要在高频循环中订阅**
   ```python
   # ❌ 错误
   async def on_features(self, symbol, features):
       await self.ctx.subscribe_tick_data([symbol])  # 每次都订阅
   ```

2. **不要忽略订阅结果**
   ```python
   # ❌ 错误
   await self.ctx.subscribe_tick_data(["XYZ"])
   subbars = await self.sfs.last_n("XYZ", n=30)  # 可能为空
   ```

3. **不要假设立即有数据**
   ```python
   # ❌ 错误
   await self.ctx.subscribe_tick_data(["NVDA"])
   subbars = await self.sfs.last_n("NVDA", n=30)  # 可能为空（需等待）
   ```

---

## 测试与验证

### 单元测试

参考测试示例：`temp/test_dynamic_subscription.py`

### 集成测试步骤

1. **启动系统**
   ```bash
   python run_system.py
   ```

2. **加载测试策略**（在 config.yml 中配置）
   ```yaml
   strategies:
     - id: "dynamic_sub_test"
       enabled: true
       module: "temp.test_dynamic_subscription"
       class: "DynamicSubscriptionTestStrategy"
   ```

3. **观察日志**
   ```bash
   tail -f logs/trader.log | grep -E "Dynamic|subscription"
   ```

4. **验证数据**
   - Kafka: 检查 `md.tick.trade.v1` topic
   - Redis: 检查 `sfs:1s:*` keys
   - ClickHouse: 查询 `sfs.sfs_5s` 表（如果 SFS-5s 启用）

---

## 故障排除

### 问题：订阅返回 False

**可能原因**：
1. IB Gateway 未连接
2. Symbol 不存在或不可交易
3. 超过 IBKR 订阅限制

**解决方案**：
```bash
# 检查 IB 连接
tail -f logs/trader.log | grep "IB.*connected"

# 检查当前订阅数
redis-cli KEYS "sfs:1s:*" | wc -l
```

### 问题：有订阅但无数据

**可能原因**：
1. 市场未开盘（非交易时段）
2. 订阅刚完成（需等待 2-5 秒）
3. SFS-1s 服务未运行

**解决方案**：
```bash
# 检查市场时段
date  # 确认当前是交易时段

# 检查 SFS-1s 服务
docker ps | grep sfs

# 等待数据
sleep 5
redis-cli LINDEX "sfs:1s:NVDA" 0
```

---

## 更新日志

| 版本 | 日期 | 变更 |
|------|------|------|
| **1.0** | 2025-10-27 | 初始版本，支持动态 tick 订阅 |

---

## 相关文档

- `CLAUDE.md` - 系统架构和修改规则
- `SFS_SYSTEM_UPGRADE_ANALYSIS.md` - SFS 架构文档
- `temp/test_dynamic_subscription.py` - 测试示例代码
