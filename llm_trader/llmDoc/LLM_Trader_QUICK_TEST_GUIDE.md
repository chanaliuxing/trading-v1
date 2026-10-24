# V2.5 快速测试指南

## 前置检查

### 1. 检查 Redis 数据可用性
```bash
# 检查是否有 1s bars
redis-cli LINDEX "sfs:1s:AAPL" 0

# 预期输出: JSON 格式的 bar 数据
# {"ts": 1729513845000, "open": 175.12, "high": 175.20, ...}
```

### 2. 检查 ClickHouse 5s bars
```bash
clickhouse-client --query "
SELECT count()
FROM sfs.sfs_5s
WHERE symbol='AAPL'
  AND ts > now() - INTERVAL 1 HOUR
"

# 预期输出: > 0 (说明有历史数据)
```

### 3. 检查 VIX 数据 (可选)
```bash
clickhouse-client --query "
SELECT *
FROM trading.refs_vix_ticks
ORDER BY ts DESC
LIMIT 1
"

# 预期输出: 最新 VIX 值
```

---

## 测试场景

### 场景 1: 基础功能测试 (3 个周期)

**目的**: 验证聚合器基本运作

```bash
python run_single_bot.py \
  --provider openai \
  --symbols TSLA \
  --cycles 3 \
  --use-aggregator \
  --prefill-5s-mins 20
```

**预期日志**:
```
=== Prefilling aggregator with 20m of 5s bars ===
  ✅ TSLA: Prefilled with 240 x 5s bars

Trading Cycle #1
================================================================================
[TSLA] Updated aggregator with 60 x 1s bars
[TSLA] Analyzing... (price: $250.00)
[TSLA] Decision: HOLD (confidence: 0.55)
```

**检查点**:
- [ ] Prefill 成功 (看到 ✅)
- [ ] 每个周期都更新 aggregator (60 x 1s bars)
- [ ] 有 LLM 决策输出

---

### 场景 2: 高级别数据刷新测试

**目的**: 验证 1h/1d 背景数据定期刷新

```bash
python run_single_bot.py \
  --provider openai \
  --symbols AAPL TSLA \
  --cycles 5 \
  --higher-tf-refresh-sec 60 \
  --interval 30
```

**预期日志**:
```
Trading Cycle #2
[Higher TF] Refreshing 1h/1d context...
[Higher TF] ✅ Refreshed for 2 symbols
```

**检查点**:
- [ ] 第 2-3 个周期看到 Higher TF 刷新
- [ ] 刷新成功 (✅ Refreshed)

---

### 场景 3: EOD 清仓测试

**目的**: 验证收盘前清仓逻辑

```bash
# 先手动开仓 (修改 portfolio_manager 初始 cash)
python run_single_bot.py \
  --provider openai \
  --symbols TSLA \
  --cycles 5 \
  --close-before 5 \
  --forbid-new-positions-before 10
```

**预期日志**:
```
[Time] Phase: RTH, Allow New: False, Should Flatten: True
⚠️  EOD Flatten triggered - closing all positions
  Flattening TSLA: 10 shares @ $250.00
  🔒 Hard gate: EOD flatten override
```

**检查点**:
- [ ] 看到 "Should Flatten: True"
- [ ] 所有持仓被平仓
- [ ] 看到 "Hard gate: EOD flatten override"

---

### 场景 4: 禁止开新仓测试

**目的**: 验证收盘前不允许开新仓

**模拟时间**: 修改系统时间到 15:52 ET (或等到真实交易时段)

```bash
python run_single_bot.py \
  --provider openai \
  --symbols NVDA \
  --cycles 3 \
  --forbid-new-positions-before 10
```

**预期日志**:
```
[Time] Phase: RTH, Allow New: False, Should Flatten: False
[NVDA] Decision: BUY 5 shares (confidence: 0.80)
  🔒 Hard gate: No new positions allowed
[NVDA] Decision: HOLD (confidence: 0.00)
```

**检查点**:
- [ ] Allow New: False
- [ ] BUY 决策被转为 HOLD
- [ ] 看到 "Hard gate: No new positions allowed"

---

### 场景 5: 多 Provider 兼容性测试

**目的**: 验证不同 LLM provider 都能工作

```bash
# OpenAI
python run_single_bot.py --provider openai --symbols AAPL --cycles 1

# Gemini
python run_single_bot.py --provider gemini --symbols AAPL --cycles 1

# DeepSeek (NIM)
python run_single_bot.py --provider deepseek_nvidia --symbols AAPL --cycles 1

# DeepSeek (Official)
python run_single_bot.py --provider deepseek --symbols AAPL --cycles 1
```

**检查点**:
- [ ] 所有 provider 都能成功调用
- [ ] 都能返回 JSON 格式决策
- [ ] 没有 JSON 解析错误

---

### 场景 6: 完整功能集成测试

**目的**: V2.0 + V2.5 所有功能同时启用

```bash
python run_single_bot.py \
  --provider deepseek_nvidia \
  --symbols AAPL TSLA NVDA \
  --enable-v2 \
  --use-aggregator \
  --prefill-5s-mins 20 \
  --higher-tf-refresh-sec 300 \
  --close-at "15:55" \
  --forbid-new-positions-before 5 \
  --cycles 10 \
  --interval 60
```

**预期日志**:
```
[V2] Using R1-optimized prompt builder for DeepSeek R1

=== Prefilling aggregator with 20m of 5s bars ===
  ✅ AAPL: Prefilled with 240 x 5s bars
  ✅ TSLA: Prefilled with 240 x 5s bars
  ✅ NVDA: Prefilled with 240 x 5s bars

Trading Cycle #1
================================================================================
Market Phase: RTH - Regular trading hours

[Time] Phase: RTH, Allow New: True, Should Flatten: False

[AAPL] Updated aggregator with 60 x 1s bars
[AAPL] Analyzing... (price: $175.00)
[AAPL] Decision: BUY 10 shares (confidence: 0.75)
[V2] Decision recorded to episodic memory

[TSLA] Updated aggregator with 60 x 1s bars
[TSLA] Analyzing... (price: $250.00)
[TSLA] Decision: HOLD (confidence: 0.60)
```

**检查点**:
- [ ] V2 memory/time-awareness 启用
- [ ] V2.5 aggregator 启用
- [ ] 所有 symbols 都预填充成功
- [ ] 每个周期更新 aggregator
- [ ] LLM 决策记录到 memory

---

## 常见问题排查

### 问题 1: No 1s bars from Redis

**原因**: SFS-1s Service 未运行或 Redis 中无数据

**解决**:
```bash
# 检查 SFS-1s Service
ps aux | grep sfs_1s

# 启动 SFS-1s Service
cd services/market
python sfs_1s_service.py
```

---

### 问题 2: No 5s bars available for prefill

**原因**: ClickHouse sfs.sfs_5s 表无历史数据

**解决**:
```bash
# 检查 5s bars
clickhouse-client --query "
SELECT symbol, count() as cnt
FROM sfs.sfs_5s
WHERE ts > now() - INTERVAL 1 HOUR
GROUP BY symbol
"

# 如果没有,需要启动 SFS-5s Service
cd services/market
python sfs_5s_service.py
```

---

### 问题 3: Higher TF Refresh failed

**原因**: ClickHouse 连接问题或表不存在

**解决**:
```bash
# 检查表是否存在
clickhouse-client --query "SHOW TABLES FROM sfs"

# 检查 1h bars 表
clickhouse-client --query "
SELECT count()
FROM sfs.sfs_1h
WHERE ts > now() - INTERVAL 1 DAY
"
```

---

### 问题 4: Provider call error / JSON parse error

**原因**: LLM 返回格式不符合预期

**解决**:
1. 检查 API key 是否正确
2. 查看完整错误日志
3. 尝试切换其他 provider 测试
4. 检查 `_call_provider_with_messages()` 的 JSON 解析逻辑

---

### 问题 5: Hard gate not triggering

**原因**: 时间设置不正确或时区问题

**解决**:
```bash
# 检查系统时区
date
timedatectl  # Linux

# 确保 TimeManager 使用正确的 ET 时区
# 查看日志中的 "Phase:" 输出
```

---

## 性能基准

| 指标 | 预期值 | 说明 |
|------|--------|------|
| Prefill 时间 | < 2s per symbol | 从 CH 读取 20 分钟 5s bars |
| Aggregator 更新 | < 100ms | 60 个 1s bars 增量聚合 |
| Higher TF 刷新 | < 1s | 刷新所有 symbols 的 1h/1d 数据 |
| LLM 调用 | 2-10s | 取决于 provider 和网络 |
| 总周期耗时 | 5-15s | 包含所有 symbols 分析 |

---

## 日志关键字

成功运行时应看到的关键日志:

```
✅ Prefilled with N x 5s bars          # 预填充成功
Updated aggregator with N x 1s bars    # 聚合器更新
✅ Refreshed for N symbols             # 高级别刷新成功
Phase: RTH, Allow New: True            # 时间上下文正常
Decision: BUY/SELL/HOLD                # LLM 决策
🔒 Hard gate: ...                      # 硬规则触发
✅ All imports successful              # 导入测试通过
```

错误日志关键字:

```
❌ Prefill error                       # 预填充失败
⚠️  No 1s bars from Redis              # Redis 无数据
⚠️  No 5s bars available               # ClickHouse 无数据
⚠️  Refresh failed                     # 高级别刷新失败
❌ Provider call error                 # LLM 调用失败
❌ Aggregator update failed            # 聚合器更新失败
```

---

## 成功标准

一个完整的测试周期应该:

1. ✅ **启动阶段**:
   - Prefill 所有 symbols (看到 ✅)
   - 所有 imports 成功
   - LLM health check 通过

2. ✅ **运行阶段**:
   - 每个周期更新 aggregator
   - LLM 返回有效决策
   - 没有 Python 异常

3. ✅ **时间管理**:
   - Time Phase 正确
   - EOD flatten 触发 (如果到时间)
   - 禁止开新仓生效 (如果到时间)

4. ✅ **性能**:
   - 周期耗时 < 15s
   - 内存占用 < 100MB
   - 没有明显延迟

---

**测试完成后**: 检查 `results/` 目录下的 JSON 文件,验证交易记录是否正确保存。
