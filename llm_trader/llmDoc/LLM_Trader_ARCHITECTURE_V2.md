```
# 🧠 LLM Auto Trader - Architecture V2.0

## 架构升级总结

从 **"LLM 决策助手"** → **"自主型智能交易体"**

---

## 核心变化

### V1.0 (原 Claude 版本)
```
用户 → LLM → 决策 → 执行 → 结束
      ↑
   单次调用，无上下文
```

### V2.0 (升级版)
```
时间感知 ──→ 三层记忆 ──→ Prompt构建器
   ↓            ↓              ↓
市场数据 ──→  LLM决策  ──→   执行
   ↓            ↓              ↓
交易反馈 ──→  结果验证 ──→  记忆更新
   ↓                          ↓
定期反思 ──→  策略调整  ←─── 自我进化
```

---

## 新增核心模块

### 1. 时间感知系统 (`time_awareness.py`)

**功能**:
- 识别当前市场阶段 (PREMARKET, OPEN, MIDDAY, CLOSE, AFTERHOURS, CLOSED)
- 计算距开盘/收盘的分钟数
- 提供市场时间上下文给 LLM
- 自动调整交易频率

**核心类**:
```python
class TimeAwareness:
    - get_market_phase() → MarketPhase
    - minutes_to_open() → int
    - minutes_to_close() → int
    - get_time_context() → Dict[str, Any]
    - get_adaptive_interval(has_positions) → int
```

**LLM Prompt 示例**:
```
TIME CONTEXT:
Current Time: 2025-01-21 09:45:00 ET (14:45:00 UTC)
Market Phase: OPEN - Market just opened - high volatility expected
Day: Tuesday
Minutes to Market Close: 375
```

**自适应频率**:
| 市场阶段 | 无持仓 | 有持仓 |
|----------|--------|--------|
| OPEN | 30s | 15s |
| MIDDAY | 60s | 30s |
| CLOSE | 30s | 15s |
| PREMARKET (30min内) | 60s | 30s |
| AFTERHOURS | 120s | 60s |
| CLOSED | 300s | 300s |

---

### 2. 三层记忆系统 (`memory_system.py`)

#### 2.1 Episodic Memory (情节记忆)
**存储**: 每次具体交易的详情

```python
@dataclass
class EpisodicMemory:
    timestamp: str
    symbol: str
    action: str  # BUY, SELL, HOLD
    quantity: int
    price: float
    confidence: float
    reasoning: str
    market_phase: str
    # 结果反馈
    realized_pnl: Optional[float]
    hold_duration_minutes: Optional[int]
    outcome_verified: bool
```

**示例**:
```
[2025-01-21T09:35:00] BUY AAPL x100 @$180.50 (OPEN) → P&L=$-45.00
[2025-01-21T10:15:00] SELL AAPL x100 @$180.05 (MIDDAY) → P&L=$-45.00
```

#### 2.2 Policy Memory (策略记忆)
**存储**: 策略参数和偏好

```python
@dataclass
class PolicyMemory:
    preferred_position_size_pct: float = 0.10
    confidence_threshold: float = 0.6
    max_hold_duration_minutes: int = 240
    preferred_symbols: List[str]
    avoid_symbols: List[str]
    win_rate: float
    avg_pnl_per_trade: float
```

**LLM Prompt 示例**:
```
STRATEGY POLICY:
- Position Size: 10% per trade
- Confidence Threshold: 0.6
- Win Rate: 65.0%
- Avg P&L: $12.50
- Preferred Symbols: AAPL, TSLA, NVDA
- Best Strategy: Momentum following in OPEN phase
```

#### 2.3 Reflection Memory (反思记忆)
**存储**: 定期自我评估结果

```python
@dataclass
class ReflectionMemory:
    period: str  # "midday", "end_of_day", "weekly"
    total_trades: int
    winning_trades: int
    total_pnl: float
    insights: List[str]
    mistakes_identified: List[str]
    improvements_planned: List[str]
```

**示例**:
```
[MIDDAY REFLECTION - 2025-01-21T12:00:00]
Trades: 8 (W:5, L:3, WR:62.5%)
P&L: $125.50
Key Insights: AAPL responds well to ORB5 breakout; TSLA too volatile in OPEN
Planned Improvements: Reduce TSLA position size; Focus on AAPL in first hour
```

---

### 3. Prompt 构建系统 (`prompt_builder.py`)

#### 3.1 三种 Prompt 类型

| Prompt 类型 | 用途 | 调用时机 |
|-------------|------|----------|
| **Decision Prompt** | 实时交易决策 | 每个交易周期 |
| **Feedback Prompt** | 交易结果反馈 | 交易平仓后 |
| **Reflection Prompt** | 策略反思与调整 | 午间/收盘/周末 |

#### 3.2 Decision Prompt 结构

```
1. System Header (Agent Identity)
   - 你是一个自主学习的量化交易智能体
   - 核心原则: 时间感知、从记忆学习、风控优先

2. Time Context (时间上下文)
   - 当前时间 (ET + UTC)
   - 市场阶段描述
   - 距开盘/收盘分钟数

3. Memory System (记忆系统)
   - Policy Memory (策略参数、胜率、偏好)
   - Recent Reflections (最近 2 次反思)
   - Recent Episodes (最近 15 笔交易)

4. Market Data (行情数据 - 压缩版)
   - 当前价格
   - 最新 5s Bar (OHLCV, 量能, 回报)
   - 技术指标 (VWAP, ATR, Regime, ORB5)

5. Portfolio State (持仓状态)
   - 现金、总权益、P&L
   - 持仓数/最大持仓数
   - 可用资金、最大单仓大小

6. Decision Request (决策请求)
   - 要求 JSON 格式返回
   - decision, quantity, confidence, reasoning
```

**Token 优化**:
- 只保留核心统计摘要 (不传完整 K 线数据)
- 记忆只保留最近 15-20 笔交易
- 反思只保留最近 2 次
- 预估单次 Prompt ~1500-2000 tokens

#### 3.3 Feedback Prompt (交易后反馈)

**目的**: 让 LLM 验证预期 vs 实际结果

```
TRADE OUTCOME FEEDBACK

Trade Details:
- Symbol: AAPL
- Action: BUY
- Quantity: 100 shares
- Entry Price: $180.50
- Exit Price: $180.05
- Price Change: -0.25%
- Realized P&L: $-45.00
- Outcome: ❌ LOSS

Your Original Reasoning:
"Strong upward momentum, ORB5 breakout, high volume"

Self-Assessment:
1. Was your prediction accurate?
2. What factors did you miss?
3. What would you do differently?

Respond in JSON:
{
  "assessment": "inaccurate",
  "missed_factors": ["market-wide selloff", "ignored low ATR"],
  "lessons_learned": "Don't trust single indicator, check broader market",
  "confidence_calibration": "overconfident"
}
```

#### 3.4 Reflection Prompt (定期反思)

**时机**:
- 午间 12:00 (midday)
- 收盘后 16:05 (end_of_day)
- 周五收盘 (weekly)

```
MIDDAY REFLECTION SESSION

Performance Summary:
- Total Trades: 12
- Winning Trades: 8
- Losing Trades: 4
- Win Rate: 66.7%
- Total P&L: $245.50
- Avg P&L per Trade: $20.46

Recent Trades:
  [09:35] BUY AAPL x100 @$180.50 (OPEN) → P&L=$+12.00
  [10:15] SELL TSLA x50 @$250.00 (MIDDAY) → P&L=$-25.00
  ...

Reflection Questions:
1. What patterns did you notice in your winning trades?
2. What mistakes were repeated in losing trades?
3. Which market conditions suited your strategy best?
4. What adjustments should you make?

Respond in JSON:
{
  "key_insights": [...],
  "repeated_mistakes": [...],
  "strategy_adjustments": {
    "confidence_threshold": 0.65,
    "preferred_symbols": ["AAPL", "NVDA"],
    "avoid_symbols": ["TSLA"]
  }
}
```

---

### 4. LLM 日志记录器 (`llm_logger.py`)

**功能**:
- 记录每次 LLM 调用 (provider, model, input, output, latency)
- 统计 Token 使用量
- 计算成本
- 异常追踪

**使用方式** (已集成到各 provider):
```python
@log_llm_call("openai", "gpt-4o-mini")
async def analyze_market(...) -> LLMResponse:
    ...
```

**日志示例**:
```json
{
  "timestamp": "2025-01-21T09:35:15.123Z",
  "provider": "openai",
  "model": "gpt-4o-mini",
  "symbol": "AAPL",
  "input_tokens": 1850,
  "output_tokens": 120,
  "latency_ms": 1250,
  "estimated_cost": 0.0012,
  "success": true
}
```

---

## 完整执行流程

### 单次交易周期 (Enhanced)

```
┌─────────────────────────────────────────────────────────┐
│ 1. 时间检查                                             │
│    - 获取当前市场阶段                                    │
│    - 判断是否应该交易 (should_trade)                     │
│    - 获取自适应调用间隔 (get_adaptive_interval)          │
└─────────────────────────────────────────────────────────┘
                        ↓
┌─────────────────────────────────────────────────────────┐
│ 2. 数据获取                                             │
│    - ClickHouse: 5s features, SFS features             │
│    - Redis: 1s bars                                     │
│    - 时间上下文: get_time_context()                     │
└─────────────────────────────────────────────────────────┘
                        ↓
┌─────────────────────────────────────────────────────────┐
│ 3. Prompt 构建                                          │
│    - PromptBuilder.build_decision_prompt()             │
│    - 包含: 时间 + 记忆 + 行情 + 持仓                     │
└─────────────────────────────────────────────────────────┘
                        ↓
┌─────────────────────────────────────────────────────────┐
│ 4. LLM 决策                                             │
│    - 调用 LLM API (带 @log_llm_call 装饰器)             │
│    - 解析 JSON 响应: decision, quantity, confidence     │
└─────────────────────────────────────────────────────────┘
                        ↓
┌─────────────────────────────────────────────────────────┐
│ 5. 决策验证                                             │
│    - 检查 confidence >= threshold                       │
│    - 检查仓位限制                                        │
│    - 检查时间约束 (如收盘前不新开仓)                     │
└─────────────────────────────────────────────────────────┘
                        ↓
┌─────────────────────────────────────────────────────────┐
│ 6. 执行交易                                             │
│    - IBKR 下单 (或 dry run 模拟)                        │
│    - 记录到 Episodic Memory                             │
└─────────────────────────────────────────────────────────┘
                        ↓
┌─────────────────────────────────────────────────────────┐
│ 7. 交易反馈 (平仓后)                                     │
│    - 计算 realized_pnl, hold_duration                   │
│    - 更新 Episodic Memory (outcome_verified=True)       │
│    - 调用 Feedback Prompt (可选)                        │
│    - 更新 Policy Memory 统计                            │
└─────────────────────────────────────────────────────────┘
                        ↓
┌─────────────────────────────────────────────────────────┐
│ 8. 定期反思 (午间/收盘)                                  │
│    - 统计期间表现                                        │
│    - 调用 Reflection Prompt                            │
│    - 更新 Policy Memory (策略调整)                      │
│    - 保存 Reflection Memory                            │
└─────────────────────────────────────────────────────────┘
```

---

## 关键特性对比

| 特性 | V1.0 (原版) | V2.0 (升级版) |
|------|-------------|--------------|
| **时间感知** | ❌ 无 | ✅ 市场阶段识别 + 自适应频率 |
| **记忆系统** | ❌ 无上下文 | ✅ 三层记忆 (Episodic/Policy/Reflection) |
| **学习能力** | ❌ 单次决策 | ✅ 持续学习 + 策略调整 |
| **Prompt** | ✅ 单一决策 Prompt | ✅ 三种 Prompt (Decision/Feedback/Reflection) |
| **调用频率** | ✅ 固定间隔 | ✅ 自适应 (15s - 300s) |
| **反馈闭环** | ❌ 无 | ✅ 结果验证 + 自我评估 |
| **策略进化** | ❌ 静态 | ✅ 动态调整 (偏好、阈值) |
| **Token 优化** | ⚠️ 未优化 | ✅ 压缩行情 + 限制记忆数量 |
| **日志追踪** | ⚠️ 基础 | ✅ 详细 LLM 调用日志 + 成本统计 |

---

## 后续扩展方向

### Phase 3: 强化学习集成
- 将 LLM 决策作为 RL 的先验知识
- 使用 PPO/A3C 优化策略参数
- 在线学习 + 离线回测结合

### Phase 4: 多智能体协作
- 多个 LLM 投票决策
- DeepSeek R1 (深度推理) vs GPT-4o (快速决策)
- 集成学习提升稳定性

### Phase 5: 情绪/新闻分析
- 接入实时新闻 API
- LLM 分析市场情绪
- 结合技术面 + 基本面

---

## 使用示例

### 启动单个 Bot (V2.0)
```bash
python run_single_bot.py \
  --provider openai \
  --symbols AAPL TSLA NVDA \
  --enable-memory \      # 启用记忆系统
  --enable-reflection \  # 启用定期反思
  --adaptive-frequency   # 自适应调用频率
```

### 竞赛模式 (V2.0)
```bash
python run_llm_race.py \
  --providers openai gemini deepseek deepseek_nvidia \
  --enable-memory \
  --compare-learning     # 比较各 LLM 的学习能力
```

### 查看记忆系统
```bash
python -m llm_trader.utils.memory_viewer \
  --memory-file results/openai_memory.json \
  --show-episodes \
  --show-reflections
```

---

## 文件结构 (新增)

```
llm_trader/
├── llm_trader/
│   ├── utils/
│   │   ├── time_awareness.py       # ✨ 新增: 时间感知
│   │   ├── memory_system.py        # ✨ 新增: 三层记忆
│   │   ├── prompt_builder.py       # ✨ 新增: Prompt 构建器
│   │   ├── llm_logger.py           # ✨ 新增: LLM 日志记录
│   │   └── config.py
│   ├── providers/
│   │   ├── openai_provider.py      # ✅ 已集成 @log_llm_call
│   │   ├── gemini_provider.py      # ✅ 已集成 @log_llm_call
│   │   └── ...
│   ├── portfolio/
│   │   └── portfolio_manager.py    # 🔄 待更新: 集成 Memory
│   └── llm_trader_bot.py           # 🔄 待更新: 集成新架构
├── ARCHITECTURE_V2.md              # ✨ 本文档
└── ...
```

---

## 总结

**V2.0 核心升级**:
1. ✅ 时间感知 - 模型知道"现在几点"
2. ✅ 三层记忆 - 模型有"历史意识"
3. ✅ Prompt 体系 - 决策/反馈/反思三位一体
4. ✅ 自适应频率 - 根据市场阶段调节节奏
5. ✅ 学习闭环 - 结果验证 → 策略调整

**从助手到智能体的本质变化**:
- 原版: "告诉我现在该买还是卖?"
- 新版: "我根据过去的经验、当前的时间和市场状态，认为应该..."

**适用场景**:
- ✅ Paper Trading 测试
- ✅ LLM 能力评估
- ✅ 策略快速迭代
- ⚠️ 实盘需增加风控层

---

**文档版本**: 2.0
**最后更新**: 2025-01-21
**作者**: Trading AI Team
```
