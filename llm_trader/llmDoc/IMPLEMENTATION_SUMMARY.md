# LLM Trader 内存增量聚合系统 - 实施总结

## ✅ 已完成的工作

### 1. 核心组件创建

#### `llm_trader/realtime_aggregator.py` - 内存聚合引擎
**功能**：
- 从 1s bars 增量聚合到多个时间周期：1s → 5s → 1m → 5m → 15m → 30m → 1h
- 自动计算滚动技术指标：
  - EMA (9, 21)
  - RSI (14)
  - Z-score (20, 60)
  - VWAP (20 periods)
- 支持冷启动预填充（从 5s bars 合成 1s bars）
- 内存占用优化：使用 deque 限制每个周期的最大长度

**特点**：
- ✅ 完全实时，不依赖 ClickHouse features 表
- ✅ 增量更新，性能高效
- ✅ 自动特征计算，LLM 可直接使用
- ✅ JSON 序列化友好（datetime 自动转换为 ISO 字符串）

---

#### `llm_trader/prompt_builder_universal.py` - 通用 Prompt 构建器
**功能**：
- 统一的 JSON Schema 输出格式（BUY/SELL/HOLD + confidence + shares + rationale）
- 支持多个 LLM Provider：
  - OpenAI（含 DeepSeek-NIM）：`response_format` json_schema
  - Gemini：`response_mime_type` application/json
  - Claude：prompt 严格约束
  - DeepSeek Official：json_object 模式
- 多周期数据压缩（控制 token 使用）
- 硬规则注入：EOD 清仓、禁止开新仓
- 自动 JSON 修复提示

**特点**：
- ✅ 一套 Prompt，多家模型可用
- ✅ Token 优化（短周期多 bars，长周期少 bars）
- ✅ 双语支持（中文/英文）
- ✅ 风控规则明确（prevent misconfigured trades）

---

#### `llm_trader/higher_tf_context.py` - 高级别时间框架加载器
**功能**：
- 从 ClickHouse 获取 1h K线数据并计算特征
- 从 ClickHouse 获取 1d features 最新一条
- 提供"背景锚点"（即使是昨日数据也有参考价值）
- 计算 1h 周期的 EMA、RSI、Z-score、Bollinger Bands、ATR

**特点**：
- ✅ 定期刷新（建议 5-15 分钟刷新一次）
- ✅ 不阻塞实时决策
- ✅ 帮助 LLM 判断市场 regime（趋势/震荡/高波动）

---

#### `llm_trader/time_manager.py` - 时间管理器
**功能**：
- 管理交易时段（PRE/OPEN/RTH/CLOSE/POST/OFF）
- EOD 清仓触发（--close-at "15:55" 或 --close-before 5）
- 禁止开新仓窗口（收盘前 N 分钟）
- 生成时间上下文供 Prompt 使用

**特点**：
- ✅ 美东时区自动转换
- ✅ 灵活配置清仓策略
- ✅ 防止 EOD 追单风险

---

#### `llm_trader/data/market_data_fetcher.py` - 增强版数据获取器
**新增方法**：
1. `prefill_5s_from_clickhouse(symbol, minutes=20)`
   - 从 ClickHouse sfs.sfs_5s 表获取历史 5s bars
   - 用于聚合器冷启动预填充
   - 避免启动后 15 分钟盲区

2. `get_latest_vix()`
   - 从 trading.refs_vix_ticks 获取最新 VIX 值
   - 提供市场情绪指标

**特点**：
- ✅ 异步高效
- ✅ 错误容错（返回空列表而非崩溃）
- ✅ 参数化查询（防止 SQL 注入）

---

### 2. 测试验证

#### `test_aggregator.py` - 综合测试脚本
**测试内容**：
1. ✅ RealtimeAggregator：
   - 生成 300 个 1s bars
   - 验证多周期聚合（1s/5s/1m/5m/15m/30m/1h）
   - 验证特征计算（EMA/RSI/Z-score/VWAP）

2. ✅ UniversalPromptBuilder：
   - 生成 OpenAI 格式 prompt
   - 验证 JSON Schema 输出
   - 验证 token 压缩

3. ✅ TimeManager：
   - 测试不同时间点的阶段判断
   - 测试清仓触发逻辑
   - 测试禁止开新仓窗口

**测试结果**：
```
✅ 所有测试通过
✅ 聚合器成功生成多周期 bars
✅ Prompt 成功生成（~13750 字符）
✅ 时间管理器正常工作
```

---

## 📋 系统架构（方案 B：内存增量聚合）

```
┌─────────────────────────────────────────────────────────────┐
│                    Kafka Ticks (实时)                         │
└─────────────────────┬───────────────────────────────────────┘
                      ↓
           ┌──────────────────────┐
           │  SFS-1s Service      │
           │  (Kafka → Redis)     │
           └──────────┬───────────┘
                      ↓
           ┌──────────────────────┐
           │  Redis                │
           │  sfs:1s:SYMBOL       │
           │  (TTL=3600s)         │
           └──────────┬───────────┘
                      ↓
        ┌─────────────────────────────────────┐
        │    LLM Trader Bot (run_single_bot)  │
        │                                      │
        │  ┌────────────────────────────────┐ │
        │  │  RealtimeAggregator (内存)     │ │
        │  │  ├─ 1s bars (最近 3600)        │ │
        │  │  ├─ 5s bars (聚合自 1s)        │ │
        │  │  ├─ 1m bars (聚合自 5s)        │ │
        │  │  ├─ 5m bars (聚合自 1m)        │ │
        │  │  ├─ 15m bars (聚合自 5m)       │ │
        │  │  ├─ 30m bars (聚合自 15m)      │ │
        │  │  └─ 1h bars (聚合自 30m)       │ │
        │  └────────────────────────────────┘ │
        │                                      │
        │  ┌────────────────────────────────┐ │
        │  │  HigherTFContextLoader         │ │
        │  │  (定期从 ClickHouse 读取)      │ │
        │  │  ├─ 1h features                │ │
        │  │  └─ 1d features                │ │
        │  └────────────────────────────────┘ │
        │                                      │
        │  ┌────────────────────────────────┐ │
        │  │  UniversalPromptBuilder        │ │
        │  │  (多周期 + 背景 + 时间 + 仓位) │ │
        │  └────────────────────────────────┘ │
        │                                      │
        │  ┌────────────────────────────────┐ │
        │  │  LLM Provider                  │ │
        │  │  (OpenAI/Gemini/DeepSeek/etc)  │ │
        │  └────────────────────────────────┘ │
        │                                      │
        │  ┌────────────────────────────────┐ │
        │  │  Decision Execution            │ │
        │  │  (with Hard Gates)             │ │
        │  └────────────────────────────────┘ │
        └──────────────────────────────────────┘
```

---

## 📊 内存占用估算

**每个 symbol**：
- 1s bars (3600 根 × 100 bytes) = 360 KB
- 5s bars (720 根 × 100 bytes) = 72 KB
- 1m bars (600 根 × 100 bytes) = 60 KB
- 5m bars (200 根 × 100 bytes) = 20 KB
- 15m/30m/1h (少量) = 10 KB
- **总计**: ~500 KB/symbol

**10 个 symbols**: ~5 MB
**100 个 symbols**: ~50 MB

**✅ 完全可接受！**

---

## 🎯 下一步工作（集成到 run_single_bot.py）

### 1. 添加命令行参数
```python
parser.add_argument("--use-aggregator", action="store_true", default=True)
parser.add_argument("--prefill-5s-mins", type=int, default=20)
parser.add_argument("--higher-tf-refresh-sec", type=int, default=300)
parser.add_argument("--close-at", type=str, default=None)
parser.add_argument("--close-before", type=int, default=None)
parser.add_argument("--forbid-new-positions-before", type=int, default=5)
```

### 2. 初始化组件
```python
from llm_trader.realtime_aggregator import RealtimeAggregator
from llm_trader.higher_tf_context import HigherTFContextLoader
from llm_trader.time_manager import TimeManager
from llm_trader.prompt_builder_universal import UniversalPromptBuilder

aggregator = RealtimeAggregator()
higher_loader = HigherTFContextLoader(ch_client)
time_manager = TimeManager(
    close_at=args.close_at,
    close_before_min=args.close_before,
    forbid_new_positions_before_min=args.forbid_new_positions_before
)
prompt_builder = UniversalPromptBuilder(json_mode="schema", style="reasoning")
```

### 3. 启动时预填充
```python
if args.prefill_5s_mins > 0:
    for sym in symbols:
        bars_5s = await data_fetcher.prefill_5s_from_clickhouse(sym, args.prefill_5s_mins)
        if bars_5s:
            aggregator.seed_with_5s(sym, bars_5s)
```

### 4. 主循环集成
```python
# 每个 cycle:
for sym in symbols:
    # 1. 读取 Redis 1s bars
    bars_1s = await data_fetcher.get_1s_bars_from_redis(sym, n=60)
    for bar in bars_1s:
        aggregator.update_from_1s(sym, bar)

    # 2. 获取多周期快照
    mtf_snapshot = aggregator.get_snapshot(sym)

    # 3. 获取 VIX
    vix_value = await data_fetcher.get_latest_vix()

    # 4. 时间上下文
    time_ctx = time_manager.build_time_ctx(datetime.now(timezone.utc))

    # 5. 构建 Prompt
    messages, provider_kwargs = prompt_builder.build(
        provider="openai",  # or "gemini", "deepseek_nvidia", etc.
        symbol=sym,
        mtf_snapshot=mtf_snapshot,
        portfolio=portfolio_state,
        time_ctx=time_ctx,
        cash_limit=args.cash,
        allow_new_position=time_ctx["allow_new_position"],
        vix_value=vix_value,
        higher_tf_context=higher_cache.get(sym),
        language="en"
    )

    # 6. 调用 LLM
    decision = await provider.decide(messages=messages, **provider_kwargs)

    # 7. 执行决策（带硬规则 Gate）
    if time_ctx["should_flatten"] and position_size > 0:
        action = "SELL"
        shares = position_size
    elif not time_ctx["allow_new_position"] and position_size == 0 and action == "BUY":
        action = "HOLD"

    # 8. 下单
    if action in ("BUY", "SELL") and shares > 0:
        await execute_trade(sym, action, shares)
```

---

## 🎉 核心优势

### 1. 完全实时的多周期视角
- ✅ 不依赖 ClickHouse features 表是否盘中更新
- ✅ LLM 能看到 1s 到 1h 的完整画面
- ✅ 实时计算技术指标（EMA/RSI/Z-score/VWAP）

### 2. 更稳的开仓门槛
- ✅ 没有 1m/5m 数据就不允许开新仓
- ✅ 减少"假启动"
- ✅ 大级别下跌时不会接飞刀

### 3. 更好的仓位分配
- ✅ `risk_units` 跟随波动因子自动变化
- ✅ 宽带震荡时降低杠杆
- ✅ 多周期一致性时才放大仓位

### 4. 强解释性
- ✅ LLM 会引用 features（如"1h ema_cross<0 且 5m rsi_14 < 35"）
- ✅ 便于回溯分析
- ✅ 决策可审计

### 5. 跨模型兼容
- ✅ 一套 Prompt，OpenAI/Gemini/Claude/DeepSeek 都能用
- ✅ 统一 JSON 输出格式
- ✅ 自动处理各家 Provider 差异

---

## 📝 总结

已成功实施 **方案 B（内存增量聚合 + 多周期拼图）**：

✅ **核心组件**：
- RealtimeAggregator（内存聚合引擎）
- UniversalPromptBuilder（通用 Prompt 构建器）
- HigherTFContextLoader（高级别时间框架加载器）
- TimeManager（时间管理器）
- MarketDataFetcher（增强版数据获取器）

✅ **测试验证**：
- 聚合器功能完整
- Prompt 生成正常
- 时间管理正确
- 所有测试通过

✅ **性能优化**：
- 内存占用小（50MB for 100 symbols）
- Token 使用优化
- 异步高效
- 增量更新

🎯 **下一步**：
- 集成到 run_single_bot.py
- 连接 Redis 获取实时 1s bars
- 连接 ClickHouse 获取 1h/1d 背景
- Paper Trading 验证
- 实盘测试

---

## 📞 需要帮助？

如果在集成过程中遇到问题，检查：
1. Redis 是否有 `sfs:1s:SYMBOL` 数据？
2. SFS-1s Service 是否在运行？
3. ClickHouse 中是否有 sfs.sfs_5s 数据？
4. 时区设置是否正确（America/New_York）？

参考文件：
- `./llm_trader/LL_trader_memorydata.md` - 完整实施方案
- `test_aggregator.py` - 测试示例
- `CLAUDE.md` - 项目开发规则

