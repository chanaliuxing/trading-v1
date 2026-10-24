# 💰 资金分配说明 - Capital Allocation

## 重要概念：每个 LLM 独立的资金池

### ✅ 正确理解

**每个 LLM bot 都有自己独立的资金池，互不共享！**

```
┌─────────────────────────────────────────────────────────┐
│              LLM Trading Race 资金分配                  │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  ┌──────────────┐  ┌──────────────┐                   │
│  │   OpenAI     │  │   Gemini     │                   │
│  │  $50,000     │  │  $50,000     │                   │
│  └──────────────┘  └──────────────┘                   │
│                                                         │
│  ┌──────────────┐  ┌──────────────┐                   │
│  │  DeepSeek    │  │DeepSeek-NVDA │                   │
│  │  $50,000     │  │  $50,000     │                   │
│  └──────────────┘  └──────────────┘                   │
│                                                         │
│  总计：4 × $50,000 = $200,000                          │
│  (但每个 LLM 只能用自己的 $50,000)                     │
└─────────────────────────────────────────────────────────┘
```

### 代码实现验证

#### 1. 每个 bot 独立创建

```python
# run_llm_race.py
for provider in ["openai", "gemini", "deepseek", "deepseek_nvidia"]:
    bot = LLMTraderBot(
        provider_name=provider,
        initial_cash=50000.0,  # 🎯 每个 bot 各自的 50k
        ...
    )
```

#### 2. 独立的 PortfolioManager

```python
# llm_trader_bot.py
class LLMTraderBot:
    def __init__(self, initial_cash=50000.0):
        # 🎯 每个 bot 创建自己的 portfolio 实例
        self.portfolio = PortfolioManager(initial_cash=initial_cash)
```

#### 3. 并行运行不共享

```python
# asyncio.gather 并行运行
# 每个 bot 在自己的 task 中运行
# 资金状态完全隔离
tasks = [run_bot_instance(...) for provider in providers]
await asyncio.gather(*tasks)
```

## 实际示例

### 场景 1: 单个 Bot

```bash
python run_single_bot.py --provider openai --cash 50000
```

**资金使用**: $50,000

```
OpenAI Bot
├── Initial Cash: $50,000
├── Can use: $50,000
└── Portfolio independent: Yes
```

### 场景 2: 竞赛模式 (默认)

```bash
python run_llm_race.py --cash 50000
```

**资金使用**: 4 × $50,000 = **$200,000 total**

```
Race with 4 LLMs:
├── OpenAI:         $50,000 (independent)
├── Gemini:         $50,000 (independent)
├── DeepSeek:       $50,000 (independent)
└── DeepSeek-NVDA:  $50,000 (independent)
─────────────────────────────────────────
Total:              $200,000 (sum of all)
```

### 场景 3: 自定义资金

```bash
python run_llm_race.py --cash 100000 --providers openai gemini
```

**资金使用**: 2 × $100,000 = **$200,000 total**

```
Race with 2 LLMs:
├── OpenAI:   $100,000 (independent)
└── Gemini:   $100,000 (independent)
─────────────────────────────────
Total:        $200,000 (sum of all)
```

## 为什么这样设计？

### 1. 公平竞赛
所有 LLM 起点相同，可以公平比较表现

### 2. 风险隔离
- OpenAI 爆仓不影响 Gemini
- 每个 LLM 的损失封顶在自己的资金池

### 3. 独立决策
- 不会相互干扰
- 没有资源竞争
- 真实模拟独立运行的策略

### 4. 可比较性
最终 P&L 可以直接比较：

```
Results:
🥇 Gemini:     +$2,450 (+4.9%)  ← 最佳表现
🥈 OpenAI:     +$1,832 (+3.7%)
🥉 DeepSeek:   +$1,205 (+2.4%)
4. DeepSeek-N: -$125   (-0.3%)  ← 唯一亏损
```

## 常见问题 FAQ

### Q1: 如果我只想用总共 $50,000 怎么办？

```bash
# 4 个 LLM 平分 $50,000
python run_llm_race.py --cash 12500
# 每个 LLM: $12,500
# 总计: 4 × $12,500 = $50,000
```

### Q2: 他们会竞争同一只股票吗？

**会！** 每个 LLM 独立决策：
- OpenAI 可能买入 AAPL
- Gemini 也可能同时买入 AAPL
- 但他们用的是各自的资金池

**实际交易影响**：
- 在 dry run 模式：没有影响（各自模拟）
- 在 live 模式：可能同时下单（需要考虑滑点）

### Q3: 可以让 LLM 共享资金池吗？

**当前设计不支持**，因为：
1. 需要复杂的资金分配逻辑
2. 会产生竞争条件（谁先抢到资金）
3. 不公平（慢的 LLM 可能没钱可用）

如果需要共享资金池，建议：
- 只运行一个 LLM
- 或者使用投票/集成机制（需要额外开发）

### Q4: 如何查看每个 LLM 的资金使用情况？

竞赛结束后查看 `results/race_results_*.json`：

```json
{
  "results": [
    {
      "provider": "openai",
      "final_equity": 51832.20,
      "total_pnl": 1832.20,
      "num_trades": 12
    },
    ...
  ]
}
```

或者查看单独的结果文件：
```bash
cat results/openai_final_*.json
cat results/gemini_final_*.json
```

## 验证脚本

运行验证脚本确认资金独立性：

```bash
python verify_independent_capital.py
```

输出：
```
✅ 每个 LLM 都有独立的 $50,000 资金池
✅ 总资金 = 4 × $50,000 = $200,000
✅ 资金是独立的，不共享
```

## 总结

| 特性 | 说明 |
|------|------|
| 资金池 | 每个 LLM 独立 |
| 默认金额 | $50,000 per LLM |
| 竞赛总金额 | $50,000 × LLM 数量 |
| 是否共享 | ❌ 不共享 |
| 风险隔离 | ✅ 完全隔离 |
| 可比较性 | ✅ 公平比较 |

---

**如有疑问，请查看代码或运行 `verify_independent_capital.py` 验证！**
