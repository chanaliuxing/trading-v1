# LLM Trader Bot 集成补丁

## 修改文件：`llm_trader/llm_trader_bot.py`

### 1. 在文件顶部添加新的 imports（第 22 行之后）

```python
# V2.5: Multi-timeframe aggregator imports
try:
    from llm_trader.realtime_aggregator import RealtimeAggregator
    from llm_trader.higher_tf_context import HigherTFContextLoader
    from llm_trader.time_manager import TimeManager
    from llm_trader.prompt_builder_universal import UniversalPromptBuilder
    AGGREGATOR_AVAILABLE = True
except ImportError:
    RealtimeAggregator = None
    HigherTFContextLoader = None
    TimeManager = None
    UniversalPromptBuilder = None
    AGGREGATOR_AVAILABLE = False
```

### 2. 修改 `__init__` 方法签名（约第 37-50 行）

在现有参数后添加：

```python
def __init__(
    self,
    provider_name: str,
    symbols: List[str],
    config: Config,
    initial_cash: float = 50000.0,
    dry_run: bool = True,
    enable_memory: bool = False,
    enable_time_awareness: bool = False,
    enable_adaptive_frequency: bool = False,
    # V2.5: Aggregator parameters
    use_aggregator: bool = True,
    prefill_5s_mins: int = 20,
    higher_tf_refresh_sec: int = 300,
    close_at: Optional[str] = None,
    close_before: Optional[int] = None,
    forbid_new_positions_before: int = 5,
):
```

### 3. 在 `__init__` 方法中添加新组件初始化（约第 80 行之后）

```python
        # V2.5: Aggregator setup
        self.use_aggregator = use_aggregator and AGGREGATOR_AVAILABLE
        self.prefill_5s_mins = prefill_5s_mins
        self.higher_tf_refresh_sec = higher_tf_refresh_sec

        # V2.5: Initialize aggregator components
        if self.use_aggregator:
            self.aggregator = RealtimeAggregator()
            self.higher_loader = None  # Will be initialized in connect()
            self.higher_cache = {}
            self.last_higher_refresh = 0

            # Time manager for EOD/opening hours logic
            self.time_manager = TimeManager(
                close_at=close_at,
                close_before_min=close_before,
                forbid_new_positions_before_min=forbid_new_positions_before,
            )

            # Universal prompt builder
            self.universal_prompt_builder = UniversalPromptBuilder(
                json_mode="schema" if provider_name in ("openai", "deepseek_nvidia") else "object",
                style="reasoning",
            )
        else:
            self.aggregator = None
            self.time_manager = None
            self.universal_prompt_builder = None
```

### 4. 修改 `connect()` 方法（约第 120-140 行）

在连接 MarketDataFetcher 之后添加：

```python
async def connect(self):
    """Connect to data sources and brokers"""
    print("\n=== Connecting to services ===")

    # Connect to market data
    self.data_fetcher = MarketDataFetcher(...)
    await self.data_fetcher.connect()

    # V2.5: Initialize higher TF loader (needs CH client)
    if self.use_aggregator:
        self.higher_loader = HigherTFContextLoader(self.data_fetcher.ch_client)

        # Prefill aggregator with historical 5s data
        if self.prefill_5s_mins > 0:
            print(f"\n=== Prefilling aggregator with {self.prefill_5s_mins}m of 5s bars ===")
            for symbol in self.symbols:
                try:
                    bars_5s = await self.data_fetcher.prefill_5s_from_clickhouse(
                        symbol, minutes=self.prefill_5s_mins
                    )
                    if bars_5s:
                        self.aggregator.seed_with_5s(symbol, bars_5s)
                        print(f"  ✅ {symbol}: Prefilled with {len(bars_5s)} x 5s bars")
                    else:
                        print(f"  ⚠️  {symbol}: No 5s bars available for prefill")
                except Exception as e:
                    print(f"  ❌ {symbol}: Prefill error: {e}")

    # ... rest of connection code
```

### 5. 在 `run()` 方法的主循环中集成聚合器（约第 200-300 行）

找到主循环部分，在处理每个 symbol 之前添加：

```python
async def run(self, interval: int = 60, max_cycles: Optional[int] = None):
    """Main trading loop"""
    # ... 初始化代码 ...

    while cycle < max_cycles if max_cycles else True:
        try:
            print(f"\n{'='*80}")
            print(f"Trading Cycle {cycle + 1}")
            print(f"{'='*80}")

            # V2.5: Refresh higher TF context periodically
            import time
            if self.use_aggregator and self.higher_loader:
                now = time.time()
                if now - self.last_higher_refresh >= self.higher_tf_refresh_sec:
                    print("\n[Higher TF] Refreshing 1h/1d context...")
                    try:
                        self.higher_cache = self.higher_loader.load(
                            self.symbols, one_hour_limit=120
                        )
                        print(f"[Higher TF] ✅ Refreshed for {len(self.higher_cache)} symbols")
                    except Exception as e:
                        print(f"[Higher TF] ⚠️  Refresh failed: {e}")
                    self.last_higher_refresh = now

            # V2.5: Get time context
            from datetime import datetime, timezone
            now_utc = datetime.now(timezone.utc)
            if self.use_aggregator and self.time_manager:
                time_ctx = self.time_manager.build_time_ctx(now_utc)
                print(f"\n[Time] Phase: {time_ctx['phase']}, "
                      f"Allow New: {time_ctx['allow_new_position']}, "
                      f"Should Flatten: {time_ctx['should_flatten']}")

                # EOD flatten check
                if time_ctx['should_flatten']:
                    print("\n⚠️  EOD Flatten triggered - closing all positions")
                    for symbol in self.symbols:
                        pos_size = self.portfolio_manager.get_position_size(symbol)
                        if pos_size > 0:
                            print(f"  Flattening {symbol}: {pos_size} shares")
                            # Execute flatten logic here
                    # After flattening, break or continue based on your logic
                    # break  # Exit after EOD flatten

            # For each symbol...
            for symbol in self.symbols:
                # V2.5: Update aggregator with latest 1s bars
                if self.use_aggregator:
                    try:
                        bars_1s = await self.data_fetcher.get_1s_bars_from_redis(symbol, n=60)
                        if bars_1s:
                            for bar in bars_1s:
                                self.aggregator.update_from_1s(symbol, bar)
                            print(f"[{symbol}] Updated aggregator with {len(bars_1s)} x 1s bars")
                        else:
                            print(f"[{symbol}] ⚠️  No 1s bars from Redis")
                    except Exception as e:
                        print(f"[{symbol}] ❌ Aggregator update failed: {e}")

                # V2.5: Build prompt using UniversalPromptBuilder
                if self.use_aggregator and self.universal_prompt_builder:
                    # Get multi-timeframe snapshot
                    mtf_snapshot = self.aggregator.get_snapshot(symbol)

                    # Get VIX
                    vix_value = await self.data_fetcher.get_latest_vix()

                    # Get portfolio state
                    position_size = self.portfolio_manager.get_position_size(symbol)
                    avg_cost = self.portfolio_manager.get_avg_cost(symbol)
                    cash = self.portfolio_manager.get_cash()
                    equity = self.portfolio_manager.get_equity()
                    portfolio_state = {
                        "position_size": position_size,
                        "avg_cost": avg_cost,
                        "cash": cash,
                        "equity": equity,
                        "pnl": (position_size * mtf_snapshot.get("1s", {}).get("bars", [{}])[-1].get("close", 0) - avg_cost * position_size) if position_size > 0 else 0
                    }

                    # Build prompt
                    messages, provider_kwargs = self.universal_prompt_builder.build(
                        provider=self.provider_name,
                        symbol=symbol,
                        mtf_snapshot=mtf_snapshot,
                        portfolio=portfolio_state,
                        time_ctx=time_ctx,
                        cash_limit=self.initial_cash,
                        allow_new_position=time_ctx['allow_new_position'],
                        vix_value=vix_value,
                        higher_tf_context=self.higher_cache.get(symbol),
                        language="en"
                    )

                    # Call provider (you'll need to adapt your provider to accept messages + kwargs)
                    try:
                        # This assumes your provider has a method to accept messages
                        # You may need to adapt this based on your provider interface
                        response = await self._call_provider_with_messages(messages, provider_kwargs)

                        # Parse decision and execute
                        if response:
                            action = response.get("action", "HOLD")
                            confidence = response.get("confidence", 0.0)
                            shares = response.get("shares", 0)

                            # Hard gates from time manager
                            if time_ctx['should_flatten'] and position_size > 0:
                                action = "SELL"
                                shares = position_size
                                print(f"  🔒 Hard gate: EOD flatten override")

                            if not time_ctx['allow_new_position'] and position_size == 0 and action == "BUY":
                                action = "HOLD"
                                shares = 0
                                print(f"  🔒 Hard gate: No new positions allowed")

                            # Execute decision
                            print(f"\n[{symbol}] Decision: {action} {shares} shares (confidence: {confidence:.2f})")
                            # ... rest of execution logic ...

                    except Exception as e:
                        print(f"[{symbol}] ❌ Provider call failed: {e}")
                        traceback.print_exc()

                else:
                    # Fall back to old method if aggregator not enabled
                    # ... existing code ...
                    pass

        except Exception as e:
            print(f"❌ Cycle error: {e}")
            traceback.print_exc()

        # ... rest of cycle code ...
```

### 6. 添加 helper 方法（在类的底部）

```python
async def _call_provider_with_messages(self, messages: List[Dict], provider_kwargs: Dict) -> Optional[Dict]:
    """Call LLM provider with messages format

    This method adapts the universal prompt format to your provider's interface.
    """
    try:
        # Extract system and user messages
        system_msg = next((m['content'] for m in messages if m['role'] == 'system'), "")
        user_msg = next((m['content'] for m in messages if m['role'] == 'user'), "")

        # Call provider based on type
        if hasattr(self.provider, 'chat_completion'):
            # Modern provider with messages support
            response = await self.provider.chat_completion(messages=messages, **provider_kwargs)
        else:
            # Legacy provider - use analyze_market
            response = await self.provider.analyze_market(
                symbol="",  # Symbol is in the prompt
                market_data={},  # Data is in the prompt
                portfolio_state={},  # Portfolio is in the prompt
                system_prompt=system_msg,
                user_prompt=user_msg
            )

        # Parse response
        if isinstance(response, dict):
            return response
        elif isinstance(response, LLMResponse):
            return {
                "action": response.action,
                "confidence": response.confidence,
                "shares": getattr(response, 'shares', 0),
                "rationale": response.reasoning
            }
        else:
            # Try to parse as JSON
            import json
            return json.loads(str(response))

    except Exception as e:
        print(f"❌ Provider call error: {e}")
        return None
```

## 总结

这些修改将：
1. ✅ 集成 RealtimeAggregator - 实时多周期聚合
2. ✅ 集成 TimeManager - EOD 清仓和开仓控制
3. ✅ 集成 HigherTFContextLoader - 1h/1d 背景数据
4. ✅ 集成 UniversalPromptBuilder - 统一 Prompt 构建
5. ✅ 实现硬规则 Gates - 防止误操作

## 下一步

修改完成后运行测试：

```bash
# 测试基础功能（dry run）
python run_single_bot.py --provider openai --symbols TSLA --cycles 3 --use-aggregator

# 测试 EOD 清仓
python run_single_bot.py --provider openai --symbols TSLA --close-before 5

# 测试完整功能
python run_single_bot.py --provider deepseek_nvidia --symbols AAPL TSLA --enable-v2 --use-aggregator
```
