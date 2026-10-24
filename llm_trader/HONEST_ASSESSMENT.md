# 诚实评估 - 测试覆盖盲点分析

## ⚠️ 发现的问题

### 问题: Layer 3 (时间戳递增检查) 未真正测试

**证据**:

查看测试场景 2 的日志输出:
```
Cycle 3: [AAPL] ⚠️  Stale: 60.1s old (threshold: 10s)
Cycle 4: [AAPL] ⚠️  Stale: 60.2s old (threshold: 10s)
...
```

**缺失的警告**:
- ❌ 没有看到 `⚠️  Timestamp not advancing` 警告
- ❌ 没有看到 `Cached data!` 提示

---

## 🔍 根因分析

### 代码逻辑检查

查看 `_check_data_health()` 的执行顺序:

```python
# Line 456-461: Layer 2 - 新鲜度检查
if data_age_sec > self.data_health_config["stale_threshold_sec"]:
    is_healthy = False
    failure_reason = f"Stale: {data_age_sec:.1f}s old"
    self.logger.warning(f"[{symbol}] ⚠️  {failure_reason}")

# Line 464-479: Layer 3 - 递增检查
if last_timestamp and is_healthy:  # ← 关键条件: and is_healthy
    ...
    if data_timestamp <= last_timestamp:
        is_healthy = False
        self.logger.warning(f"[{symbol}] ⚠️  Timestamp not advancing - Cached data!")
```

**问题原因**:
- Layer 2 检测到数据陈旧 (60s > 10s) → 设置 `is_healthy = False`
- Layer 3 的条件是 `if last_timestamp and is_healthy`
- 由于 `is_healthy = False`，Layer 3 **不会执行**

---

## 📊 测试场景 2 实际验证了什么？

### ✅ 已验证的功能

1. **Layer 2: 新鲜度检查** ✅
   - 证据: `⚠️  Stale: 60.1s old`
   - 阈值: 10 秒
   - 状态: 工作正常

2. **状态机转换** ✅
   - 证据: HEALTHY → DEGRADED → UNHEALTHY
   - 连续失败检测: 正常

3. **恢复验证** ✅
   - 证据: RECOVERING (1/3, 2/3, 3/3)
   - 连续成功检查: 正常

### ❌ 未验证的功能

**Layer 3: 时间戳递增检查**
- 场景: 数据新鲜 (age < 10s) 但时间戳不变
- 状态: **未测试** ⚠️

---

## 🎯 真正的 Layer 3 测试场景

### 应该测试的场景

```
Cycle 1: timestamp = 10:30:00, age = 3s
         → Layer 2: PASS (3s < 10s)
         → Layer 3: PASS (第一次，无 last_timestamp)
         → 结果: HEALTHY ✓

Cycle 2: timestamp = 10:30:00, age = 5s  ← 时间戳相同但仍新鲜!
         → Layer 2: PASS (5s < 10s)
         → Layer 3: FAIL (10:30:00 <= 10:30:00)
         → 警告: "⚠️  Timestamp not advancing - Cached data!"
         → 结果: UNHEALTHY ✗

Cycle 3: timestamp = 10:30:05, age = 2s  ← 时间戳递增
         → Layer 2: PASS (2s < 10s)
         → Layer 3: PASS (10:30:05 > 10:30:00)
         → 结果: HEALTHY ✓
```

**关键**: 数据**看起来新鲜** (age < 10s)，但实际是**缓存** (timestamp 不变)

---

## 🧪 补充测试: 真正验证 Layer 3

我现在写一个补充测试来验证这个场景：

```python
async def test_layer3_timestamp_advancing():
    """测试 Layer 3: 时间戳递增检查 (数据新鲜但缓存)"""

    print("\n=== Layer 3 专项测试 ===")

    config = Config()
    bot = LLMTraderBot(...)

    now = datetime.now(timezone.utc)

    # Cycle 1: 新鲜数据
    ts1 = now - timedelta(seconds=3)
    data1 = create_test_data("AAPL", ts1)

    is_healthy = bot._check_data_health({"AAPL": data1})
    print(f"Cycle 1: timestamp={ts1.isoformat()}, healthy={is_healthy}")
    assert is_healthy, "第一次应该通过"

    # 模拟时间流逝 5 秒
    await asyncio.sleep(0.1)

    # Cycle 2: 相同时间戳 (模拟缓存)，但由于时间流逝，age 仍 < 10s
    # 这里是关键: Redis 返回的数据 timestamp 不变，但系统时间在前进
    now2 = datetime.now(timezone.utc)

    # 创建数据，timestamp 和 Cycle 1 相同，但 age 根据当前时间计算
    data2 = {
        "AAPL": {
            "symbol": "AAPL",
            "timestamp": ts1.isoformat(),  # ← 相同的时间戳!
            "current_price": 150.0,
            "bars_1s": [...] * 10
        }
    }

    is_healthy = bot._check_data_health(data2)
    print(f"Cycle 2: timestamp={ts1.isoformat()} (same!), healthy={is_healthy}")

    # 应该检测到时间戳不递增
    assert not is_healthy, "应该检测到缓存数据"
    assert bot.data_health['consecutive_failures'] >= 1
```

让我创建这个补充测试...
