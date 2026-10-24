# Premarket Momentum V4 - 综合测试计划

**目标**: 全面测试策略启动后的运行逻辑和计算方法
**测试日期**: 2025-10-19
**版本**: V4 (DAY-only, Trailing Stop OCO)

---

## 测试范围

### 1. 核心运行逻辑测试
- [x] 时间轴状态机转换
- [x] 候选股票发现与冻结
- [x] 观察窗数据聚合
- [x] 入场决策执行（只执行一次）
- [x] 订单管理（未成交处理）
- [x] 清理与退出序列

### 2. 计算方法验证
- [ ] **观察窗打分逻辑**
  - [ ] 相对成交量 (RVOL) 计算
  - [ ] Spread 百分位数计算
  - [ ] VWAP 计算与比较
  - [ ] Opening Range (OR) 进度计算
  - [ ] Uptick Ratio 计算
  - [ ] 综合评分加权

- [ ] **风险与仓位计算**
  - [ ] 每股风险计算 (entry - max(VWAP, OR_low))
  - [ ] 仓位大小计算
  - [ ] OCO 价格计算 (TP + Trailing Stop)

- [ ] **数据聚合方法**
  - [ ] SecondBucket tick 聚合
  - [ ] 5s RealTimeBars VWAP 计算
  - [ ] 环形缓冲区 (RingBuffer) 剪枝

### 3. 边界条件测试
- [ ] 启动时间检查
- [ ] 数据缺失处理
- [ ] 极端市场条件
- [ ] 并发安全

---

## 第一部分: 时间轴与状态机测试

### Test 1.1: 时间边界逻辑
**目标**: 验证策略在不同时间点的行为

**测试场景**:
```
8:00  → 盘前扫描开始
9:26  → 冻结 Top5 候选
9:30  → 观察窗开始 (收集数据)
9:33  → 入场窗口 (决策并下单)
9:34:30 → 未成交订单处理
9:35:50 → 清理序列
9:36  → 硬退出
9:37  → 应该已停止
```

**验证点**:
- [ ] 每个时间段的状态转换正确
- [ ] 跨时间段时无重复执行
- [ ] 日志输出符合预期

**执行**: `python scripts/test_premarket_standalone.py`

---

### Test 1.2: 启动时间检查
**目标**: 验证不同启动时间的警告机制

**测试场景**:
| 启动时间 | 预期行为 | 验证点 |
|---------|---------|--------|
| 8:00 AM | 正常运行 | 无警告 |
| 9:26 AM | 运行但警告 | WARNING: Late scan |
| 9:33 AM | 运行但警告 | WARNING: Entry may fail |
| 9:36 AM | 中止运行 | ERROR: Abort |

**验证点**:
- [ ] 警告日志正确输出
- [ ] 9:36 后自动中止
- [ ] warmup() 在晚启动时仍执行

---

## 第二部分: 计算方法验证

### Test 2.1: RVOL (相对成交量) 计算
**目标**: 验证 RVOL_open 计算的准确性

**计算公式**:
```python
RVOL_open = vol_today_9:30-9:32 / vref_9:30-9:31_previous_day
```

**测试数据**:
```python
# 模拟数据
vref_9:30 = 100,000 shares  # 昨天 9:30
vref_9:31 = 50,000 shares   # 昨天 9:31
vref_total = 150,000 shares

vol_today_9:30 = 120,000 shares
vol_today_9:31 = 90,000 shares
vol_today_9:32 = 60,000 shares
vol_today_total = 270,000 shares

expected_rvol = 270,000 / 150,000 = 1.8
```

**验证点**:
- [ ] Vref 缓存读写正确
- [ ] 历史数据获取时间范围准确 (9:30-9:31)
- [ ] RVOL 计算结果正确
- [ ] RVOL 评分逻辑正确:
  - RVOL < 0.2 → score = 0.0
  - RVOL = 0.5 → score = 0.3
  - RVOL = 1.0 → score = 0.6
  - RVOL = 2.0 → score = 1.0

**测试方法**: 单元测试 + 实际数据验证

---

### Test 2.2: Spread 百分位数计算
**目标**: 验证 spread 中位数和 P95 计算

**计算公式**:
```python
spread_pct = (ask - bid) / ((ask + bid) / 2)
median = percentile(spreads, 50)
p95 = percentile(spreads, 95)
```

**测试数据**:
```python
# 模拟 100 个 spread 样本
spreads = [
    0.001, 0.002, 0.0015, 0.0025, 0.003, ...  # 100 samples
]

expected_median = 0.0020  # 假设值
expected_p95 = 0.0045     # 假设值
```

**验证点**:
- [ ] Spread 样本收集正确
- [ ] 排序后百分位数计算准确
- [ ] Cap 限制生效 (max = 5%)
- [ ] 评分逻辑正确:
  - median <= 0.25% and p95 <= 0.50% → 通过
  - 否则 → 拒绝

**测试方法**: 单元测试 + Mock 数据

---

### Test 2.3: VWAP 计算
**目标**: 验证 VWAP 计算的准确性

**计算公式 (两种来源)**:

**方法 1: 5s RealTimeBars**
```python
vwap = sum(close_i * volume_i) / sum(volume_i)
```

**方法 2: Tick SecondBucket**
```python
# 每个 tick
vwap_num += last * size
vwap_den += size

vwap = vwap_num / vwap_den
```

**测试数据**:
```python
# 5s bars (9:30:00 - 9:32:59, 共 36 bars)
bars = [
    {"close": 100.0, "volume": 1000},
    {"close": 100.5, "volume": 1500},
    {"close": 101.0, "volume": 2000},
    ...
]

expected_vwap = (100*1000 + 100.5*1500 + 101*2000 + ...) / (1000+1500+2000+...)
```

**验证点**:
- [ ] 5s bars VWAP 累加正确
- [ ] Tick VWAP 累加正确
- [ ] 两种方法结果一致性
- [ ] VWAP 评分逻辑: last > vwap → score = 1.0

**测试方法**: 单元测试 + 手工计算验证

---

### Test 2.4: Opening Range (OR) 计算
**目标**: 验证 OR 进度和触发条件

**计算逻辑**:
```python
or_high = max(ticks_9:30-9:32)
or_low = min(ticks_9:30-9:32)
or_range = or_high - or_low
progress = (last - or_low) / or_range

# 触发条件
trigger_ok = (last >= or_high) or ((or_high - last) <= 0.3 * or_range)
```

**测试数据**:
```python
or_high = 105.0
or_low = 100.0
or_range = 5.0

# 测试不同 last 价格
test_cases = [
    (105.0, True, "At OR high"),          # last >= or_high
    (104.5, True, "Within 30% of high"),  # 105 - 104.5 = 0.5 <= 1.5
    (103.0, False, "Below 30% threshold"), # 105 - 103 = 2.0 > 1.5
    (100.0, False, "At OR low"),
]
```

**验证点**:
- [ ] OR high/low 正确跟踪
- [ ] Progress 百分比计算正确
- [ ] 触发条件判断准确
- [ ] OR 评分逻辑正确

**测试方法**: 单元测试

---

### Test 2.5: Uptick Ratio 计算
**目标**: 验证 uptick/downtick 统计

**计算逻辑**:
```python
for each tick:
    if last > last_prev:
        upticks += 1
    elif last < last_prev:
        downticks += 1

uptick_ratio = upticks / (upticks + downticks)
```

**测试数据**:
```python
# 模拟 tick 序列
ticks = [
    100.0,  # baseline
    100.1,  # uptick (1)
    100.2,  # uptick (2)
    100.1,  # downtick (1)
    100.2,  # uptick (3)
    100.2,  # unchanged
    100.0,  # downtick (2)
]

expected_upticks = 3
expected_downticks = 2
expected_ratio = 3 / 5 = 0.6
```

**验证点**:
- [ ] Uptick 统计正确
- [ ] Downtick 统计正确
- [ ] Ratio 计算准确
- [ ] 评分逻辑:
  - ratio >= 0.6 → score = 1.0
  - ratio >= 0.55 → score = 0.7
  - ratio >= 0.5 → score = 0.4
  - 否则 → score = 0.0

**测试方法**: 单元测试

---

### Test 2.6: 综合评分加权
**目标**: 验证观察窗综合评分计算

**评分公式**:
```python
obs_score = (
    W_VOL * vol_score +      # 0.3
    W_SPREAD * spread_score + # 0.25
    W_VWAP * vwap_score +    # 0.2
    W_OR * or_score +        # 0.15
    W_TAPE * tape_score      # 0.1
)
```

**测试数据**:
```python
# 模拟各项评分
vol_score = 0.8      # RVOL = 1.5
spread_score = 1.0   # Spread 优秀
vwap_score = 1.0     # Last > VWAP
or_score = 0.7       # 接近 OR high
tape_score = 1.0     # Uptick ratio = 0.65

expected_obs_score = 0.3*0.8 + 0.25*1.0 + 0.2*1.0 + 0.15*0.7 + 0.1*1.0
                   = 0.24 + 0.25 + 0.20 + 0.105 + 0.10
                   = 0.895
```

**验证点**:
- [ ] 权重系数正确 (总和 = 1.0)
- [ ] 加权计算准确
- [ ] 最终评分范围 [0, 1]

**测试方法**: 单元测试

---

## 第三部分: 风险与仓位计算

### Test 3.1: 每股风险计算
**目标**: 验证风险计算逻辑

**计算公式**:
```python
entry = ask * (1 + LIMIT_EPS_TICKS)  # 0.05% 抬价
vwap_or = max(vwap, or_low)
risk_per_share = max(entry - vwap_or, entry * FIXED_SL_FLOOR)
# FIXED_SL_FLOOR = 1.2%
```

**测试数据**:
```python
test_cases = [
    # Case 1: 正常情况
    {
        "ask": 100.0,
        "vwap": 99.0,
        "or_low": 98.5,
        "entry": 100.05,
        "vwap_or": 99.0,
        "risk_natural": 100.05 - 99.0 = 1.05,
        "risk_floor": 100.05 * 0.012 = 1.2006,
        "expected_risk": 1.2006  # max(1.05, 1.2006)
    },
    # Case 2: VWAP 很接近 entry
    {
        "ask": 100.0,
        "vwap": 99.9,
        "or_low": 99.8,
        "entry": 100.05,
        "vwap_or": 99.9,
        "risk_natural": 100.05 - 99.9 = 0.15,
        "risk_floor": 100.05 * 0.012 = 1.2006,
        "expected_risk": 1.2006  # floor 生效
    },
]
```

**验证点**:
- [ ] Entry 价格抬价正确 (0.05%)
- [ ] vwap_or 取 max(vwap, or_low)
- [ ] 风险下限生效 (1.2%)
- [ ] 边界情况处理正确

**测试方法**: 单元测试

---

### Test 3.2: 仓位大小计算
**目标**: 验证根据风险计算仓位

**计算公式**:
```python
risk_budget = CAPITAL * RISK_PCT  # $50,000 * 0.5% = $250
shares = int(risk_budget / risk_per_share)
```

**测试数据**:
```python
CAPITAL = 50000
RISK_PCT = 0.005  # 0.5%
risk_budget = 250

test_cases = [
    {"risk_per_share": 1.0, "expected_shares": 250},
    {"risk_per_share": 2.0, "expected_shares": 125},
    {"risk_per_share": 0.5, "expected_shares": 500},
    {"risk_per_share": 10.0, "expected_shares": 25},
]
```

**验证点**:
- [ ] 风险预算计算正确
- [ ] 向下取整 (int)
- [ ] shares > 0 检查
- [ ] 极端情况 (risk 过大导致 shares=0)

**测试方法**: 单元测试

---

### Test 3.3: OCO 价格计算
**目标**: 验证止盈和追踪止损价格

**计算公式**:
```python
tp_price = round(entry * (1 + TP_PCT/100), 2)
# TP_PCT = 2.4%

trail.trailingPercent = TRAILING_PCT  # 1.8%
```

**测试数据**:
```python
test_cases = [
    {
        "entry": 100.00,
        "tp_price": round(100.00 * 1.024, 2) = 102.40,
        "trailing_pct": 1.8
    },
    {
        "entry": 245.50,
        "tp_price": round(245.50 * 1.024, 2) = 251.39,
        "trailing_pct": 1.8
    },
]
```

**验证点**:
- [ ] TP 价格计算准确
- [ ] 四舍五入到 2 位小数
- [ ] Trailing percent 设置正确
- [ ] OCO group 命名唯一

**测试方法**: 单元测试

---

## 第四部分: 数据聚合与缓冲

### Test 4.1: SecondBucket 聚合
**目标**: 验证秒级 tick 聚合逻辑

**聚合逻辑**:
```python
class SecondBucket:
    def update(tick):
        # 更新 high/low/last
        # 累加 volume, vwap_num, vwap_den
        # 统计 upticks/downticks
        # 收集 spread 样本
```

**测试数据**:
```python
# 同一秒内的多个 tick
ticks_in_second = [
    {"ts": 1234567890, "last": 100.0, "size": 100, "bid": 99.9, "ask": 100.1},
    {"ts": 1234567890, "last": 100.1, "size": 200, "bid": 100.0, "ask": 100.2},
    {"ts": 1234567890, "last": 100.2, "size": 150, "bid": 100.1, "ask": 100.3},
]

expected_result = {
    "high": 100.2,
    "low": 100.0,
    "last": 100.2,
    "volume": 450,
    "vwap": (100*100 + 100.1*200 + 100.2*150) / 450,
    "upticks": 2,
    "downticks": 0,
    "spreads": [spread1, spread2, spread3]
}
```

**验证点**:
- [ ] High/Low 正确追踪
- [ ] Volume 正确累加
- [ ] VWAP 分子分母正确
- [ ] Uptick/Downtick 统计准确
- [ ] Spread 样本收集完整

**测试方法**: 单元测试

---

### Test 4.2: RingBuffer 剪枝
**目标**: 验证环形缓冲区内存管理

**剪枝逻辑**:
```python
RING_SECONDS = 180  # 3 minutes

def _prune(now_epoch):
    cutoff = now_epoch - RING_SECONDS
    for ts in buckets.keys():
        if ts < cutoff:
            del buckets[ts]
```

**测试数据**:
```python
# 创建 300 秒的数据
for i in range(300):
    bucket = SecondBucket(ts=base_ts + i)
    ring.buckets[base_ts + i] = bucket

# 在 now_epoch = base_ts + 300 时剪枝
ring._prune(base_ts + 300)

# 预期：只保留 [base_ts+121, base_ts+300] 共 180 个 bucket
expected_count = 180
```

**验证点**:
- [ ] 剪枝后保留正确数量
- [ ] 保留最近 180 秒数据
- [ ] 旧数据正确删除
- [ ] 内存占用稳定

**测试方法**: 单元测试

---

### Test 4.3: window_stats 聚合
**目标**: 验证时间窗口统计计算

**计算逻辑**:
```python
def window_stats(start_ts, end_ts):
    # 遍历 [start_ts, end_ts] 的所有 bucket
    # 聚合 high, low, volume, vwap, spreads, upticks/downticks
```

**测试数据**:
```python
# 创建 9:30:00 - 9:32:59 共 180 个 bucket
open_ts = int(datetime(2025, 10, 18, 9, 30, 0, tzinfo=ET).timestamp())
obs_end_ts = int(datetime(2025, 10, 18, 9, 33, 0, tzinfo=ET).timestamp()) - 1

# 填充测试数据
...

stats = ring.window_stats(open_ts, obs_end_ts)

expected_stats = {
    "high": 105.5,
    "low": 100.0,
    "volume": 270000,
    "vwap": 102.3,
    "spread_median": 0.0020,
    "spread_p95": 0.0045,
    "uptick_ratio": 0.62
}
```

**验证点**:
- [ ] 时间范围正确 (包含 start_ts, 包含 end_ts)
- [ ] High/Low 聚合正确
- [ ] Volume 总和正确
- [ ] VWAP 加权平均正确
- [ ] Spread 百分位数正确
- [ ] Uptick ratio 正确

**测试方法**: 单元测试

---

## 第五部分: 入场决策逻辑

### Test 5.1: 入场条件检查
**目标**: 验证所有入场条件逻辑

**入场条件**:
```python
1. RVOL >= 0.2 OR (abs_volume >= 500K OR abs_notional >= $20M)
2. spread_median <= 0.25% AND spread_p95 <= 0.50%
3. last > vwap
4. (last >= or_high) OR ((or_high - last) <= 0.3 * or_range)
5. daily_trades_done < 3
6. symbol not traded yet (if ONE_TRADE_PER_SYMBOL)
```

**测试数据**:
```python
test_cases = [
    {
        "name": "All conditions pass",
        "rvol": 1.5,
        "spread_median": 0.0020,
        "spread_p95": 0.0040,
        "last": 102.0,
        "vwap": 101.0,
        "or_high": 102.0,
        "daily_trades": 0,
        "traded": False,
        "expected": True
    },
    {
        "name": "RVOL too low, but abs volume ok",
        "rvol": 0.1,
        "abs_volume": 600000,
        "expected": True  # Fallback 生效
    },
    {
        "name": "Spread too wide",
        "spread_p95": 0.0060,
        "expected": False
    },
    {
        "name": "Last below VWAP",
        "last": 100.0,
        "vwap": 101.0,
        "expected": False
    },
]
```

**验证点**:
- [ ] 每个条件独立验证
- [ ] 组合条件逻辑正确
- [ ] RVOL fallback 生效
- [ ] 拒绝时日志清晰

**测试方法**: 单元测试 + Integration 测试

---

### Test 5.2: 入场只执行一次
**目标**: 确认入场逻辑不会重复执行

**测试逻辑**:
```python
_entry_executed = False

# 模拟 9:33:00 - 9:34:30 期间的循环 (450 iterations)
entry_count = 0
for i in range(450):
    if not _entry_executed:
        execute_entry()
        entry_count += 1
        _entry_executed = True

assert entry_count == 1
```

**验证点**:
- [ ] 只执行 1 次入场逻辑
- [ ] 标志位正确设置
- [ ] 后续循环不再执行

**测试方法**: 单元测试 (已完成)

---

## 第六部分: 订单管理与退出

### Test 6.1: 未成交订单处理
**目标**: 验证 9:34:30 的未成交处理逻辑

**处理逻辑**:
```python
if fill_ratio >= 0.8:
    # OK, 不处理
    pass
elif fill_ratio > 0:
    # 部分成交，重绑 OCO
    cancel_remaining()
    rebundle_children(filled_qty)
else:
    # 零成交，撤单并清理
    cancel_all()
    remove_from_tracking()
```

**测试数据**:
```python
test_cases = [
    {
        "name": "80%+ filled",
        "total": 100,
        "filled": 85,
        "expected_action": "no_action"
    },
    {
        "name": "Partial fill",
        "total": 100,
        "filled": 50,
        "expected_action": "rebundle_oco",
        "expected_new_qty": 50
    },
    {
        "name": "No fill",
        "total": 100,
        "filled": 0,
        "expected_action": "cancel_and_remove"
    },
]
```

**验证点**:
- [ ] Fill ratio 计算正确
- [ ] 不同情况处理正确
- [ ] OCO 重绑数量正确
- [ ] 零成交时清理完整

**测试方法**: Mock 测试

---

### Test 6.2: 清理序列
**目标**: 验证 9:35:50 的清理逻辑

**清理步骤**:
```python
1. 撤销父单未成交余量
2. 市价平仓所有持仓
3. 撤销所有子单 (TP/Trail)
4. 清空本地状态
```

**验证点**:
- [ ] 所有订单被撤销
- [ ] 持仓被平仓
- [ ] positions 字典清空
- [ ] working_orders 字典清空

**测试方法**: Mock 测试

---

### Test 6.3: 硬退出
**目标**: 验证 9:36 强制退出

**退出逻辑**:
```python
1. 执行 cleanup_sequence()
2. 二次保险：撤销所有残留订单
3. 断开连接
4. 退出主循环
```

**验证点**:
- [ ] cleanup_sequence 被调用
- [ ] 所有订单确实被撤销
- [ ] IB 连接断开
- [ ] 主循环退出

**测试方法**: Integration 测试

---

## 第七部分: 边界条件与异常处理

### Test 7.1: 数据缺失处理
**目标**: 验证数据不可用时的处理

**测试场景**:
```python
1. Scanner 返回空结果
2. 实时行情获取失败
3. 历史数据 (Vref) 不可用
4. Tick 数据长时间无更新
5. 5s bars 未收到数据
```

**验证点**:
- [ ] 空 Scanner 不影响运行
- [ ] Quote 失败时跳过该股票
- [ ] Vref 缺失时使用 fallback
- [ ] 数据超时有日志警告
- [ ] 不会因数据问题崩溃

**测试方法**: Mock 测试 + Fault injection

---

### Test 7.2: 极端市场条件
**目标**: 验证极端情况处理

**测试场景**:
```python
1. Spread 极宽 (>5%)
2. 价格跳空 (gap up/down >10%)
3. 零成交量股票
4. 价格 = 0 或 NaN
5. Ask < Bid (异常数据)
```

**验证点**:
- [ ] Spread cap 限制生效
- [ ] 异常价格被过滤
- [ ] 零成交量被拒绝
- [ ] NaN 值不引发异常
- [ ] Ask < Bid 被检测

**测试方法**: 单元测试 + Mock 数据

---

### Test 7.3: 并发安全
**目标**: 验证异步代码无竞争条件

**测试场景**:
```python
1. 多个 ticker.updateEvent 回调并发
2. bar5s.updateEvent 与 tick 回调并发
3. _on_tick 修改 ring buffer 时的线程安全
```

**验证点**:
- [ ] RingBuffer 无竞争条件
- [ ] Candidate 对象修改安全
- [ ] 订单 ID 生成唯一
- [ ] 无 race condition

**测试方法**: 并发测试 + 压力测试

---

## 第八部分: 集成测试

### Test 8.1: 完整流程 Dry-run
**目标**: 端到端测试完整策略流程

**测试步骤**:
```bash
1. 启动策略 (dry-run 模式)
2. 等待 warmup 完成
3. 观察扫描与冻结
4. 等待观察窗
5. 验证入场决策
6. 检查 positions 记录
7. 等待硬退出
8. 验证清理完成
```

**验证点**:
- [ ] 所有阶段按时间推进
- [ ] 日志输出完整清晰
- [ ] dry-run 模式不下真单
- [ ] 最终状态正确清理

**测试方法**: 手动测试 + 自动化脚本

---

### Test 8.2: Paper Trading 测试
**目标**: 在 Paper 环境验证订单流

**测试步骤**:
```bash
1. 连接 Paper Trading (4002)
2. 运行完整策略
3. 观察订单状态
4. 验证 OCO 创建
5. 检查成交情况
6. 验证平仓执行
```

**验证点**:
- [ ] 父单正确提交 (DAY, LIMIT)
- [ ] 子单正确创建 (TP + Trail, OCA)
- [ ] OCO group 设置正确
- [ ] 订单状态追踪准确
- [ ] 平仓订单执行

**测试方法**: Paper Trading 环境测试

---

## 测试执行计划

### Phase 1: 单元测试 (2-3 小时)
```bash
# 创建测试文件
tests/unit/test_calculations.py
tests/unit/test_data_aggregation.py
tests/unit/test_risk_sizing.py
tests/unit/test_entry_logic.py

# 执行
pytest tests/unit/ -v --cov=scripts
```

### Phase 2: 集成测试 (1-2 小时)
```bash
# 逻辑验证
python scripts/test_premarket_standalone.py

# 数据获取
python scripts/test_premarket_data_fetch.py

# Dry-run 完整流程
python scripts/premarket_momentum_standalone.py --dry-run
```

### Phase 3: Paper Trading 验证 (开盘时 1 天)
```bash
# 9:00 AM 启动
python scripts/premarket_momentum_standalone.py --mode paper

# 监控日志
tail -f logs/*.log

# 9:40 AM 检查结果
python scripts/check_ibkr_status.py
```

---

## 测试通过标准

### 最低要求
- [ ] 所有单元测试通过 (100%)
- [ ] 集成测试通过 (时间轴 + 数据获取)
- [ ] Dry-run 无异常完成
- [ ] Paper Trading 至少 1 次成功入场

### 理想目标
- [ ] 代码覆盖率 >= 80%
- [ ] 所有边界条件测试通过
- [ ] Paper Trading 3 天稳定运行
- [ ] P&L 符合预期 (RVOL > 1.5 的股票)

---

## 测试报告模板

```markdown
# 测试报告

**测试日期**: YYYY-MM-DD
**测试人员**: XXX
**策略版本**: V4

## 测试结果摘要
- 单元测试: XX/XX passed
- 集成测试: XX/XX passed
- Paper Trading: XX days, XX trades

## 发现的问题
1. [严重] XXX
2. [中等] XXX
3. [轻微] XXX

## 性能指标
- 入场次数: XX
- 成功率: XX%
- 平均 P&L: $XX
- 最大回撤: $XX

## 建议
1. XXX
2. XXX
```

---

**测试计划版本**: 1.0
**创建日期**: 2025-10-19
**负责人**: Trading Team
