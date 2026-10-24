# LLM Trader V2.1 Upgrade Summary

**Date**: 2025-10-24
**Upgrade**: Commission Awareness + ChatGPT-Optimized Prompts

---

## 概述

本次升级集成了以下两项重要改进：

1. **手续费意识** - AI 现在知道交易成本（万分之一，最低$1）
2. **ChatGPT优化的Prompt** - 更强的时间感知、风险管理和决策质量

---

## 主要变更

### 1. 手续费计算和跟踪

#### 新增文件
- `llm_trader/utils/commission.py` - 手续费计算工具

#### 费用结构
```python
- 费率: 0.01% (万分之一) 每笔交易（买入或卖出）
- 最低费用: $1.00/笔
- 示例:
  - $10,000 交易 → $1.00 手续费（最低收费）
  - $50,000 交易 → $5.00 手续费（0.01% = $50,000 × 0.0001）
```

#### 修改的文件
**`llm_trader/portfolio/portfolio_manager.py`**:
- 导入 `calculate_commission` 和 `estimate_round_trip_commission`
- 添加 `total_commission_paid` 字段跟踪累计手续费
- `open_position()`: 扣除买入手续费
- `close_position()`: 扣除卖出手续费，计算净P&L
- `get_position_state()`: 返回 `total_commission_paid` 给LLM

#### 示例
```python
pm = PortfolioManager(initial_cash=50000)
pm.open_position("TSLA", 100, 440)  # 买入100股@$440
# 成本: $44,000 + $4.40 commission = $44,004.40
# 现金: $50,000 - $44,004.40 = $5,995.60

pm.close_position("TSLA", 100, 450)  # 卖出100股@$450
# 收入: $45,000 - $4.50 commission = $44,995.50
# 净P&L: $44,995.50 - $44,004.40 = $991.10
```

---

### 2. ChatGPT优化的Prompt系统

#### System Prompt变更

**之前（V2.0）**:
```
You are an intraday trading decision agent.
Think step-by-step silently, but output JSON only.
```

**现在（V2.1）**:
```
You are the bot's **intraday portfolio & risk manager**.
Your goal is **risk-adjusted P&L** for today, not trade count.
Favor *fewer, higher-quality* trades.
Use multi-timeframe confluence (1s/5s microstructure + 1m/5m short-term + 15m/1h context)
and **New York session timing**.
```

**核心改进**:
- 强调"风险调整的P&L"而非交易数量
- 明确多周期融合策略
- 纽约时区会话感知
- LLM自主选择风险方法（ATR/VWAP/Kelly等）

#### User Prompt变更

**新增部分**:

**1. 交易成本（Trading Costs）**:
```
**Trading Costs (IMPORTANT):**
- Commission: 0.01% per trade (buy or sell), minimum $1
- Example: 102 shares @ $440.00 = $8.98 round-trip (0.02% of trade value)
- ⚠️ YOU must account for commission when sizing positions
- Small trades (<$10k) pay minimum $1 each way = $2 total
- Break-even requires price move > commission cost
```

**2. 时间感知（Time Awareness）**:
```
**Time awareness (New York):**
Use `time` to drive session discipline:
• `time.ny_iso` (NY timestamp), `time.session_phase` (OPEN/MIDDAY/AFTERNOON/CLOSE),
• `time.minutes_to_close`, `time.api_interval_sec`, `time.decisions_left`,
• `time.should_prevent_new_positions` (late-day), `time.should_flatten` (EOD flatten).
  If `decisions_left` is small, favor exit/defense and avoid new risks.
```

**3. 决策框架（Make a decision & a plan）**:
```
1. Diagnose **regime** (trend/chop) & **volatility** (calm/normal/elevated) from the pack.
2. If edge is strong and gates allow, decide **BUY** or **SELL**; else **HOLD**.
3. If BUY/SELL, set `shares` > 0, consistent with: available cash, `cash_limit`,
   `max_single_order_ratio`, commission costs, and your **chosen risk method** (you pick).
4. Encode a concise lifecycle in `rationale`: entry logic, **stop**, **take-profit**,
   **time-stop**, and scale-in/out cues (one sentence).
```

---

### 3. 增强的时间上下文

#### 修改文件
**`llm_trader/time_manager.py`**:
- `build_time_ctx()` 增强，添加ChatGPT优化字段
- 新增参数 `api_interval_sec` 用于计算 `decisions_left`

#### 新增字段
```python
{
    # 原有字段（向后兼容）
    "now_utc": "2025-10-24 10:00:00",
    "now_et": "2025-10-24 10:00:00",
    "phase": "OPEN",
    "should_flatten": False,
    "allow_new_position": True,

    # V2.1: ChatGPT优化字段
    "ny_iso": "2025-10-24T10:00:00-04:00",           # ISO格式
    "session_phase": "OPEN",                          # OPEN/MIDDAY/AFTERNOON/CLOSE
    "minutes_to_close": 360,                          # 距离收盘分钟数
    "stop_at_ny": "15:55:00",                         # 平仓时间
    "api_interval_sec": 60,                           # LLM调用间隔
    "decisions_left": 355,                            # 剩余决策次数
    "should_prevent_new_positions": False,            # 禁止新仓
}
```

#### 会话阶段映射
```
RTH (9:30-16:00) 细分为:
- OPEN (9:30-10:30)
- MIDDAY (10:30-15:00)
- AFTERNOON (15:00-16:00)
```

---

### 4. 仓位限制调整

#### 修改
**`llm_trader/portfolio/portfolio_manager.py`**:
- `max_position_pct`: 0.20 (20%) → **0.90 (90%)**

#### Prompt中的风控说明
```
**Risk Management (YOUR responsibility):**
- Consider position concentration risk
- Consider volatility and market conditions
- Factor in commission costs for small/frequent trades
- Suggested: 10-30% per position for balance of diversification vs commission efficiency
- No shorting: SELL only reduces/exits existing longs
```

**理由**:
- LLM现在负责风控决策
- 系统提供90%硬限制，但建议10-30%分散
- 平衡分散风险与手续费效率

---

## 测试验证

### 创建的测试文件

1. **`test_commission.py`**
   - 测试手续费计算
   - 验证Portfolio Manager扣费逻辑

2. **`test_new_prompt.py`**
   - 测试新prompt生成
   - 验证手续费和时间信息是否正确包含

3. **`test_integration_v2.py`** ✅ 综合测试
   - 手续费跟踪: PASS
   - 时间上下文: PASS
   - Portfolio状态: PASS
   - ChatGPT优化prompt: PASS
   - 完整工作流: PASS

### 测试结果示例

```
[1] Testing Commission Tracking
--------------------------------------------------------------------------------
Initial cash: $50,000.00
After BUY 100 TSLA @ $440:
  Cash: $5,995.60
  Commission paid: $4.40
After SELL 100 TSLA @ $450:
  Cash: $50,991.10
  Commission paid: $8.90
  Net P&L: $991.10
  Expected P&L: $991.10
  ✓ Match!

[2] Testing Time Context Generation
--------------------------------------------------------------------------------
Test time: 2025-10-24 10:00:00 EDT
Time context:
  session_phase: OPEN
  minutes_to_close: 360
  decisions_left: 355
  ✓ Match!

[4] Testing ChatGPT-Optimized Prompt
--------------------------------------------------------------------------------
Prompt features:
  ✓ Commission info
  ✓ Time awareness
  ✓ Risk-adjusted P&L
  ✓ Multi-timeframe
  ✓ Session timing
```

---

## 向后兼容性

### 保持兼容

- `PortfolioManager` 现有API不变，只增加字段
- `TimeManager.build_time_ctx()` 保留所有原有字段
- `UniversalPromptBuilder.build()` 保持相同接口
- 旧的portfolio state字段仍然返回

### 新增（可选）

- `portfolio["total_commission_paid"]` - 新字段，默认0
- `time_ctx["decisions_left"]` - 新字段，需要传 `api_interval_sec`
- `time_ctx["session_phase"]` - 细化的会话阶段

### 迁移建议

对于现有代码:
1. **Portfolio Manager**: 无需改动，自动计算手续费
2. **Time Context**: 可选传递 `api_interval_sec`，不传默认60
3. **Prompt Builder**: 自动使用新prompt，无需改动

---

## 性能影响

### Token消耗

- **System prompt**: 500 chars → 1160 chars (+132%)
- **User prompt**: ~3500 chars → ~4500 chars (+29%)
- **总体**: 增加约1000 tokens/call

### 计算开销

- **Commission计算**: O(1)，每次交易~0.1ms
- **Time context**: O(1)，增加~5个字段计算
- **Prompt生成**: 增加~50 chars格式化

**结论**: 忽略不计的性能影响

---

## 示例使用

### 1. 运行单个bot（手续费自动扣除）

```bash
python run_single_bot.py \
  --provider gemini \
  --symbol TSLA \
  --interval 60 \
  --cash 50000 \
  --close-at "15:55" \
  --start-trading-at "9:40" \
  --stop-trading-at "10:30"
```

### 2. 查看手续费统计

```python
from llm_trader.portfolio.portfolio_manager import PortfolioManager

pm = PortfolioManager(initial_cash=50000)
# ... 执行交易 ...

state = pm.get_position_state()
print(f"Total commission paid: ${state['total_commission_paid']:.2f}")
print(f"Net P&L: ${state['total_pnl']:.2f}")
```

### 3. 手动计算手续费

```python
from llm_trader.utils.commission import (
    calculate_commission,
    estimate_round_trip_commission,
    commission_as_percentage,
    breakeven_move_bps
)

# 单程手续费
fee = calculate_commission(10000)  # $1.00 (最低收费)

# 往返手续费
round_trip = estimate_round_trip_commission(price=440, quantity=100)  # $8.80

# 手续费占比
pct = commission_as_percentage(price=440, quantity=100)  # 0.02%

# 盈亏平衡所需变动（基点）
bps = breakeven_move_bps(price=440, quantity=100)  # 2.0 bps
```

---

## 文件清单

### 新增文件

```
llm_trader/utils/commission.py              # 手续费计算工具
llm_trader/utils/time_context.py            # 时间上下文辅助函数（独立版）
test_commission.py                          # 手续费测试
test_new_prompt.py                          # 新prompt测试
test_integration_v2.py                      # 综合集成测试
UPGRADE_V2.1_SUMMARY.md                     # 本文档
```

### 修改文件

```
llm_trader/portfolio/portfolio_manager.py   # 添加手续费跟踪
llm_trader/prompt_builder_universal.py      # ChatGPT优化prompt
llm_trader/time_manager.py                  # 增强时间上下文
llm_trader/llm_trader_bot.py                # 传递api_interval_sec
```

### 备份文件

```
llm_trader/prompt_builder_universal.py.backup  # V2.0备份
```

---

## 下一步建议

### 短期（立即可做）

1. **监控手续费影响**
   - 统计每日手续费总额
   - 分析手续费占P&L比例
   - 观察AI是否减少小单交易

2. **A/B测试新prompt**
   - V2.0 vs V2.1性能对比
   - 胜率、夏普比率、最大回撤
   - 交易频率变化

3. **调整建议仓位范围**
   - 当前建议: 10-30%
   - 可根据实际表现调整为10-20%或20-40%

### 中期（1-2周）

1. **手续费优化策略**
   - 分析何时合并订单更高效
   - 研究最优交易规模
   - 实现"手续费阈值"逻辑

2. **时间感知优化**
   - 根据 `decisions_left` 调整风险
   - 临近收盘减少新仓
   - 盘中不同阶段的策略差异

3. **多symbol手续费分摊**
   - 跨symbol的总手续费限制
   - 优先交易手续费占比低的品种

### 长期（1个月+）

1. **高级风控集成**
   - Kelly Criterion sizing
   - ATR-based position sizing
   - 动态max_position_pct

2. **手续费Tier优化**
   - 分级手续费结构（大单优惠）
   - 月度手续费返还
   - 经纪商API集成真实费率

3. **Prompt进一步优化**
   - 根据LLM反馈迭代
   - 添加市场状态（VIX, 趋势强度等）
   - 集成历史表现学习

---

## 常见问题

### Q1: 手续费会影响所有历史交易吗？
**A**: 不会。只影响V2.1之后的新交易。历史数据保持不变。

### Q2: 我可以关闭手续费计算吗？
**A**: 理论上可以，但不建议。可以设置 `commission_rate=0` 模拟零成本。

### Q3: 为什么我的P&L比预期低？
**A**: 检查 `portfolio.total_commission_paid`。每笔交易买卖都有手续费。

### Q4: 90%仓位限制会不会太激进？
**A**: 这是**硬限制**，LLM被建议使用10-30%。实际使用需观察。

### Q5: 时间上下文的 `decisions_left` 如何计算？
**A**: `(flatten_time - now) / api_interval_sec`。例如剩余60分钟，间隔60秒 = 60次决策。

### Q6: 中文prompt是否也更新了？
**A**: 是的。英文和中文prompt都已同步更新。

---

## 回滚方案

如需回滚到V2.0:

```bash
# 1. 恢复prompt builder
cp llm_trader/prompt_builder_universal.py.backup \
   llm_trader/prompt_builder_universal.py

# 2. 恢复Portfolio Manager (需手动删除commission相关代码)
git diff llm_trader/portfolio/portfolio_manager.py  # 查看变更
git checkout llm_trader/portfolio/portfolio_manager.py  # 恢复

# 3. 恢复Time Manager
git checkout llm_trader/time_manager.py

# 4. 恢复Bot
git checkout llm_trader/llm_trader_bot.py
```

**注意**: 回滚后手续费跟踪失效，P&L计算会高估。

---

## 总结

V2.1升级成功集成了手续费意识和ChatGPT优化的prompt系统：

✅ **手续费跟踪**: 0.01%费率，最低$1，自动扣除
✅ **ChatGPT优化**: 强调风险调整P&L，时间感知，LLM自主风控
✅ **时间上下文**: 增强的会话感知，剩余决策次数
✅ **仓位限制**: 90%硬限制，10-30%建议范围
✅ **向后兼容**: 保留所有现有API和字段
✅ **全面测试**: 所有集成测试通过

**下一步**: 监控实盘表现，根据数据迭代优化。

---

**维护者**: Trading Team
**最后更新**: 2025-10-24
**版本**: V2.1.0
