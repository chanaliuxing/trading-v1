# OCO 配置指南 (One-Cancels-Other)

## 概述

系统级的止盈止损保护,通过 SafetyKernel 在第一笔成交后自动下 OCO 订单。

**优势:**
- ✅ 响应速度快 (SafetyKernel 层面)
- ✅ 每个策略可自定义参数
- ✅ 配置在 JSON 文件中,不污染 config.yml
- ✅ 无需修改策略逻辑代码

---

## 配置方式

### 在策略 JSON 文件中添加 `oco` 字段

```json
{
  "symbols": ["TSLA"],
  "sizing": { ... },
  "market_filter": { ... },

  "oco": {
    "take_profit_bp": 250,      // 止盈: 2.5% (250 basis points)
    "hard_stop_bp": 180,         // 止损: 1.8% (180 basis points)
    "trail_atr_mult": 2.5        // 追踪止损: 2.5倍ATR
  },

  "adaptive": { ... }
}
```

**注意**: 1 基点 (bp) = 0.01% = 0.0001

---

## 参数说明

| 参数 | 类型 | 默认值 | 推荐范围 | 说明 |
|------|------|--------|----------|------|
| `take_profit_bp` | int | 200 (2%) | 100-500 | 止盈点位 (基点) |
| `hard_stop_bp` | int | 150 (1.5%) | 80-300 | 止损点位 (基点) |
| `trail_atr_mult` | float | 2.0 | 1.5-5.0 | 追踪止损 (ATR倍数) |

---

## 示例配置

### 保守型 (AAPL - 低波动)

```json
"oco": {
  "take_profit_bp": 150,    // 1.5% 止盈
  "hard_stop_bp": 120,      // 1.2% 止损
  "trail_atr_mult": 2.0     // 风险回报比 1.25:1
}
```

### 平衡型 (TSLA - 中波动)

```json
"oco": {
  "take_profit_bp": 250,    // 2.5% 止盈
  "hard_stop_bp": 180,      // 1.8% 止损
  "trail_atr_mult": 2.5     // 风险回报比 1.39:1
}
```

### 激进型 (NVDA - 高波动)

```json
"oco": {
  "take_profit_bp": 300,    // 3.0% 止盈
  "hard_stop_bp": 200,      // 2.0% 止损
  "trail_atr_mult": 3.0     // 风险回报比 1.5:1
}
```

---

## 工作流程

```
1. 策略计算 signal → 提交 DecisionEnvelope
                              ↓
2. DecisionPipeline 处理 → Translator 读取 extras.oco_cfg
                              ↓
3. Translator 生成 ExecPlan (包含 OCO 参数)
                              ↓
4. ExecutionService 下主订单
                              ↓
5. SafetyKernel 监听第一笔成交 → 自动下 TP/SL OCO 订单
```

**关键时间线:**
- T0: 主订单成交
- T0 + <1s: OCO 订单下单完成 (target: <100ms)
- T0 + 监控: SafetyKernel 持续监控 OCO 状态

---

## 验证配置生效

### 1. 查看策略启动日志

```bash
grep "OCO config loaded" logs/trading_system.log | tail -5
```

应该看到:
```
strategy_id=enhanced_ml_001 take_profit_bp=250 hard_stop_bp=180
```

### 2. 查看数据库 OCO 订单

```sql
SELECT
    o.symbol,
    o.side,
    o.order_type,
    o.qty,
    o.limit_px,
    o.stop_px,
    o.oca_group,
    o.parent_order_id,
    o.state
FROM core.core_order o
WHERE o.strategy_id = 'enhanced_ml_001'
  AND o.created_at > now() - INTERVAL '1 hour'
  AND o.oca_group IS NOT NULL
ORDER BY o.created_at DESC;
```

**预期结果:**
```
symbol | side | order_type | qty | limit_px | stop_px | oca_group | parent_order_id | state
-------|------|------------|-----|----------|---------|-----------|-----------------|----------
TSLA   | BUY  | MKT        | 100 | NULL     | NULL    | NULL      | NULL            | FILLED
TSLA   | SELL | LMT        | 100 | 153.00   | NULL    | OCO_abc   | <parent_id>     | SUBMITTED
TSLA   | SELL | STP        | 100 | NULL     | 147.75  | OCO_abc   | <parent_id>     | SUBMITTED
```

### 3. 验证价格计算

假设买入 TSLA @ $150.00, 配置 `take_profit_bp: 250, hard_stop_bp: 180`:

- **止盈价**: $150 × (1 + 0.025) = **$153.75**
- **止损价**: $150 × (1 - 0.018) = **$147.30**

---

## 常见问题

### Q1: 如果不配置 `oco` 字段会怎样?

A: 使用系统默认值 (在 `services/decision/pipeline.py`):
```python
"take_profit_bp": 40,    # 0.4%
"hard_stop_bp": 80,      # 0.8%
"trail_atr_mult": 1.5
```

### Q2: 可以禁用 OCO 吗?

A: 可以,设置为 `null`:
```json
"oco": null
```

或者设置非常大的值 (相当于不触发):
```json
"oco": {
  "take_profit_bp": 10000,   // 100% (基本不会触发)
  "hard_stop_bp": 10000,
  "trail_atr_mult": 100.0
}
```

### Q3: 为什么删除了 `risk` 字段?

A: 旧的 `risk` 配置是策略层自管理风控,会导致:
- ❌ 双重风控冲突 (策略手动 + 系统 OCO)
- ❌ 可能导致意外持仓
- ❌ 响应速度慢 (依赖策略层轮询)

新的 `oco` 配置是系统级保护:
- ✅ 单一风控点,无冲突
- ✅ SafetyKernel 快速响应
- ✅ 自动管理 OCO 订单生命周期

### Q4: 追踪止损 (trail_atr_mult) 如何工作?

A: 当价格向有利方向移动超过 `trail_atr_mult × ATR` 时,止损价会自动跟随移动,保护浮盈。

**示例:**
- 买入 @ $150, ATR = $2, trail_atr_mult = 2.5
- 初始止损: $147.30 (固定)
- 价格涨到 $155 (移动 $5 > 2.5×$2)
- 新止损: $152.30 (跟随上移 $5)

---

## 配置更新流程

1. **修改 JSON 文件**
   ```bash
   vim strategies/configs/enhanced_ml_TSLA.json
   # 修改 oco 字段
   ```

2. **重启策略进程** (无需重启 Docker)
   ```bash
   # 方式1: 重启整个系统
   pkill -f run_system.py
   python run_system.py

   # 方式2: 热重载 (如果策略支持 config_reload_interval_sec)
   # 等待策略自动重载配置 (默认60秒)
   ```

3. **验证生效**
   ```bash
   # 查看启动日志
   tail -f logs/trading_system.log | grep "OCO config"
   ```

---

## 性能影响

- **内存**: 每个 OCO 订单增加 ~1KB (可忽略)
- **延迟**: OCO 下单 < 100ms (SafetyKernel 优化)
- **频率**: 每笔成交触发一次 OCO 下单

---

## 最后更新

- **日期**: 2025-10-31
- **版本**: v2.0
- **作者**: Trading Team

---

## 相关文档

- `CLAUDE.md` - 策略开发指南 (Section 四)
- `services/exec/safety_kernel.py` - OCO 实现代码
- `services/decision/translator.py:295-318` - OCO 配置读取
