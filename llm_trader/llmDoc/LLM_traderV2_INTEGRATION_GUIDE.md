# V2.0 集成指南

## 快速开始

### 方式 1: 使用命令行参数 (推荐)

```bash
# 启用所有 V2 功能
python run_single_bot.py \
  --provider openai \
  --symbols AAPL TSLA \
  --enable-memory \
  --enable-time-awareness \
  --enable-adaptive-frequency \
  --cycles 20
```

### 方式 2: 在代码中初始化

```python
from llm_trader.llm_trader_bot import LLMTraderBot
from llm_trader.utils.config import Config

config = Config()

bot = LLMTraderBot(
    provider_name="openai",
    symbols=["AAPL", "TSLA", "NVDA"],
    config=config,
    initial_cash=50000.0,
    dry_run=True,
    enable_memory=True,              # ✅ V2: 启用记忆系统
    enable_time_awareness=True,      # ✅ V2: 启用时间感知
    enable_adaptive_frequency=True,  # ✅ V2: 启用自适应频率
)

await bot.run(interval=60)  # interval 会被自适应覆盖
```

---

## 已完成的集成

### ✅ 1. PortfolioManager

**集成内容**:
- 接受可选的 `memory_system` 参数
- 在 `open_position()` 时记录交易到 pending_episodes
- 在 `close_position()` 时更新 episodic memory 的结果
- `save_state()` 时自动保存 memory system

**使用示例**:
```python
from llm_trader.portfolio.portfolio_manager import PortfolioManager
from llm_trader.utils.memory_system import MemorySystem

# 创建记忆系统
memory = MemorySystem()

# 创建 portfolio (集成记忆)
portfolio = PortfolioManager(
    initial_cash=50000.0,
    memory_system=memory,  # ✅ V2 集成
)

# 正常使用,记忆会自动记录
portfolio.open_position("AAPL", 100, 180.50)
portfolio.close_position("AAPL", 100, 180.05)
# → memory 中自动记录了这笔交易的 P&L
```

### ✅ 2. LLMTraderBot

**新增参数**:
```python
LLMTraderBot(
    ...
    enable_memory=True,              # 启用记忆系统
    enable_time_awareness=True,      # 启用时间感知
    enable_adaptive_frequency=True,  # 启用自适应频率
)
```

**内部自动处理**:
- 如果启用 V2 功能，自动初始化 TimeAwareness, MemorySystem, PromptBuilder
- 向后兼容: 如果 V2 模块不可用，自动降级为 V1 模式
- Portfolio 自动集成 memory_system

---

## 需要手动集成的部分

由于 `llm_trader_bot.py` 文件较大，以下是需要在主循环中添加的 V2 逻辑：

### 1. 在 `run_trading_cycle()` 中使用 Prompt Builder

**原代码** (V1):
```python
async def run_trading_cycle(self):
    # ... 获取市场数据
    market_data = await self.data_fetcher.get_comprehensive_market_data(symbol)
    portfolio_state = self.portfolio.get_position_state()

    # 直接调用 LLM
    decision = await self.llm_provider.analyze_market(
        symbol, market_data, portfolio_state
    )
```

**新代码** (V2 - 需要添加):
```python
async def run_trading_cycle(self):
    # ... 获取市场数据
    market_data = await self.data_fetcher.get_comprehensive_market_data(symbol)
    portfolio_state = self.portfolio.get_position_state()

    # V2: 如果启用了 prompt_builder，使用它来构建 prompt
    if self.prompt_builder:
        # 使用 V2 prompt builder
        prompt = self.prompt_builder.build_decision_prompt(
            symbol, market_data, portfolio_state, include_memory=True
        )
        # 注意: 需要修改 provider 的 analyze_market 接受自定义 prompt
        # 或者在 provider 内部使用 prompt_builder
        decision = await self.llm_provider.analyze_market(
            symbol, market_data, portfolio_state
        )
    else:
        # V1: 直接调用
        decision = await self.llm_provider.analyze_market(
            symbol, market_data, portfolio_state
        )
```

### 2. 执行交易后记录到 Memory

**需要添加** (在 `execute_trade()` 之后):
```python
async def execute_trade(self, decision, current_price):
    # ... 执行交易逻辑 ...

    if action == "BUY" and success:
        # V2: 记录到 episodic memory
        if self.memory_system:
            from llm_trader.utils.trading_helpers import format_decision_for_memory, compress_market_features

            market_phase = self.time_awareness.get_market_phase().value if self.time_awareness else "UNKNOWN"
            compressed_features = compress_market_features(market_data)

            self.memory_system.add_episode(
                symbol=decision.symbol,
                action=decision.decision,
                quantity=decision.quantity,
                price=current_price,
                confidence=decision.confidence,
                reasoning=decision.reasoning,
                market_phase=market_phase,
                market_features=compressed_features,
            )
```

### 3. 自适应调用频率

**原代码** (V1):
```python
async def run(self, interval=60):
    while self.running:
        await self.run_trading_cycle()
        await asyncio.sleep(interval)  # 固定间隔
```

**新代码** (V2 - 需要添加):
```python
async def run(self, interval=60):
    while self.running:
        await self.run_trading_cycle()

        # V2: 自适应间隔
        if self.enable_adaptive_frequency and self.time_awareness:
            has_positions = len(self.portfolio.positions) > 0
            interval = self.time_awareness.get_adaptive_interval(has_positions)
            print(f"Next cycle in {interval}s (adaptive)")

        await asyncio.sleep(interval)
```

### 4. 定期反思触发

**需要添加** (在主循环中):
```python
async def run(self, interval=60):
    while self.running:
        await self.run_trading_cycle()

        # V2: 检查是否需要触发反思
        if self.enable_memory and self.time_awareness:
            from llm_trader.utils.trading_helpers import should_execute_reflection

            current_time = self.time_awareness.now_market_time()
            market_phase = self.time_awareness.get_market_phase().value

            if should_execute_reflection(self.last_reflection_time, current_time, market_phase):
                await self.execute_reflection(period="midday" if market_phase == "MIDDAY" else "end_of_day")
                self.last_reflection_time = current_time

        await asyncio.sleep(interval)
```

### 5. 反思执行逻辑

**需要添加新方法**:
```python
async def execute_reflection(self, period: str):
    """Execute reflection and update strategy

    Args:
        period: Reflection period (midday, end_of_day, weekly)
    """
    if not self.memory_system or not self.prompt_builder:
        return

    from llm_trader.utils.trading_helpers import calculate_reflection_stats

    # 计算统计
    stats = calculate_reflection_stats(self.portfolio.trade_history)

    # 获取最近交易摘要
    recent_episodes = self.memory_system.get_recent_episodes(n=20)
    trade_summaries = [ep.to_summary() for ep in recent_episodes if ep.outcome_verified]

    # 构建反思 prompt
    reflection_prompt = self.prompt_builder.build_reflection_prompt(
        period=period,
        total_trades=stats["total_trades"],
        winning_trades=stats["winning_trades"],
        losing_trades=stats["losing_trades"],
        total_pnl=stats["total_pnl"],
        recent_trades_summary=trade_summaries,
    )

    # TODO: 调用 LLM 获取反思结果
    # reflection_response = await self.llm_provider.reflect(reflection_prompt)

    # 记录反思到 memory
    self.memory_system.add_reflection(
        period=period,
        insights=["Placeholder insight 1", "Placeholder insight 2"],
        mistakes=["Placeholder mistake"],
        improvements=["Placeholder improvement"],
    )

    print(f"[V2] Reflection completed for period: {period}")
```

---

## 完整示例代码片段

### 添加到 llm_trader_bot.py 的 `run()` 方法

```python
async def run(self, interval: int = 60, max_cycles: Optional[int] = None):
    """Run the trading bot

    Args:
        interval: Default seconds between trading cycles (可被自适应覆盖)
        max_cycles: Max number of cycles (None = run forever)
    """
    await self.start()

    try:
        while self.running:
            # V2: 检查市场状态
            if self.time_awareness:
                if not self.time_awareness.should_trade(allow_extended=False):
                    print("[V2] Market closed, waiting...")
                    await asyncio.sleep(300)  # 5 minutes
                    continue

            # 执行交易周期
            await self.run_trading_cycle()

            # V2: 检查反思触发
            if self.enable_memory and self.time_awareness:
                from llm_trader.utils.trading_helpers import should_execute_reflection

                current_time = self.time_awareness.now_market_time()
                market_phase = self.time_awareness.get_market_phase().value

                if should_execute_reflection(self.last_reflection_time, current_time, market_phase):
                    await self.execute_reflection(
                        period="midday" if market_phase == "MIDDAY" else "end_of_day"
                    )
                    self.last_reflection_time = current_time

            # 检查周期限制
            if max_cycles and self.cycle_count >= max_cycles:
                print(f"\nReached max cycles ({max_cycles}), stopping...")
                break

            # V2: 自适应间隔
            if self.enable_adaptive_frequency and self.time_awareness:
                has_positions = len(self.portfolio.positions) > 0
                interval = self.time_awareness.get_adaptive_interval(has_positions)
                print(f"[V2] Next cycle in {interval}s (adaptive)")
            else:
                print(f"Waiting {interval}s for next cycle...")

            await asyncio.sleep(interval)

    except KeyboardInterrupt:
        print("\nReceived interrupt signal")
    finally:
        await self.stop()
```

---

## 测试 V2 功能

### 单元测试

```bash
# 测试时间感知
python -c "
from llm_trader.utils.time_awareness import TimeAwareness
ta = TimeAwareness()
print(ta.get_time_context())
print(f'Adaptive interval: {ta.get_adaptive_interval(has_positions=True)}s')
"

# 测试记忆系统
python -c "
from llm_trader.utils.memory_system import MemorySystem
mem = MemorySystem()
ep = mem.add_episode('AAPL', 'BUY', 100, 180.5, 0.85, 'test', 'OPEN')
print(mem.compress_for_prompt(max_episodes=5))
"

# 测试 Prompt 构建
python -c "
from llm_trader.utils.time_awareness import TimeAwareness
from llm_trader.utils.memory_system import MemorySystem
from llm_trader.utils.prompt_builder import PromptBuilder

ta = TimeAwareness()
mem = MemorySystem()
pb = PromptBuilder(ta, mem)

prompt = pb.build_decision_prompt('AAPL', {}, {})
print(f'Prompt length: {len(prompt)} chars')
"
```

### 集成测试

```bash
# 运行 V2 bot (dry run)
python run_single_bot.py \
  --provider openai \
  --symbols AAPL \
  --enable-memory \
  --enable-time-awareness \
  --enable-adaptive-frequency \
  --cycles 3 \
  --interval 10
```

---

## 升级路径

### 阶段 1: 基础集成 (已完成)
- ✅ 创建 V2 核心模块 (time_awareness, memory_system, prompt_builder)
- ✅ PortfolioManager 集成 memory_system
- ✅ LLMTraderBot 接受 V2 参数

### 阶段 2: 主循环集成 (进行中)
- 🔄 修改 `run_trading_cycle()` 使用 PromptBuilder
- 🔄 添加记忆记录逻辑
- 🔄 实现自适应频率
- 🔄 实现反思触发

### 阶段 3: 完整功能 (待完成)
- ⏳ Feedback Prompt 集成
- ⏳ Reflection Prompt 集成
- ⏳ 策略参数自动调整
- ⏳ 多 LLM 竞赛支持 V2 比较

---

## 常见问题

### Q: 如何只启用部分 V2 功能？

A: 可以选择性启用:
```python
bot = LLMTraderBot(
    ...
    enable_memory=True,              # 只启用记忆
    enable_time_awareness=False,
    enable_adaptive_frequency=False,
)
```

### Q: V2 功能会影响 V1 吗？

A: 不会。V2 是完全向后兼容的。如果不启用 V2 参数，系统会以 V1 模式运行。

### Q: 如何查看记忆系统的内容？

A:
```python
# 在代码中
print(bot.memory_system.compress_for_prompt())

# 或保存后查看
bot.portfolio.save_state("results/test.json")
# 会同时保存 test_memory.json
```

### Q: 自适应频率的间隔范围是？

A: 15秒 (开盘/收盘有持仓) 到 300秒 (闭市)，详见 `TimeAwareness.get_adaptive_interval()`

---

## 下一步

1. **完成主循环集成**: 修改 `llm_trader_bot.py` 的 `run()` 和 `run_trading_cycle()`
2. **测试端到端流程**: 确保记忆正确记录和读取
3. **优化 Prompt**: 根据实际 token 使用情况调整压缩策略
4. **添加监控**: 记录 V2 功能的性能指标

---

**文档版本**: 1.0
**最后更新**: 2025-01-21
**维护者**: Trading AI Team
