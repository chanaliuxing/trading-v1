# 缺数降级功能补丁

## 功能说明
当多周期数据样本不足时，自动降级决策：
- 禁止开新仓 (BUY → HOLD)
- 在 Prompt 中明确告知 LLM 数据不完整
- 记录缺数原因到日志

## 修改 1: UniversalPromptBuilder (llm_trader/prompt_builder_universal.py)

### A. 添加缺数检测方法

在类的顶部添加静态方法:

```python
@staticmethod
def _check_missing_frames(mtf_snapshot: Dict, min_requirements: Dict[str, int]) -> List[str]:
    """检测哪些周期的样本数不足

    Args:
        mtf_snapshot: 多周期快照 from aggregator.get_snapshot()
        min_requirements: {"1s": 30, "5s": 12, "1m": 10, "5m": 3, "15m": 1}

    Returns:
        缺数的周期列表, 如 ["1m(5/10)", "5m(1/3)"]
    """
    missing = []
    for tf, min_bars in min_requirements.items():
        actual = len(mtf_snapshot.get(tf, {}).get("bars", []))
        if actual < min_bars:
            missing.append(f"{tf}({actual}/{min_bars})")
    return missing
```

### B. 修改 build() 方法

在 build() 方法中添加缺数检测和 Prompt 注入:

```python
def build(
    self,
    *,
    provider: str,
    symbol: str,
    mtf_snapshot: Dict,
    portfolio: Dict,
    time_ctx: Dict,
    cash_limit: float,
    allow_new_position: bool,
    vix_value: Optional[float] = None,
    higher_tf_context: Optional[Dict] = None,
    sizing_cap_pct: float = 0.10,
    forbid_short: bool = True,
    language: str = "en",
    # 新增参数
    min_bars_requirements: Optional[Dict[str, int]] = None,
    enable_degrade: bool = True,
):
    # 默认最小样本要求
    if min_bars_requirements is None:
        min_bars_requirements = {
            "1s": 30,   # 至少 30 秒
            "5s": 12,   # 至少 1 分钟
            "1m": 10,   # 至少 10 分钟
            "5m": 3,    # 至少 15 分钟
            "15m": 1,   # 至少 1 根
        }

    # 检测缺数
    missing_frames = []
    if enable_degrade:
        missing_frames = self._check_missing_frames(mtf_snapshot, min_bars_requirements)

    # 更新 time_ctx 加入数据质量信息
    time_ctx_extended = {
        **time_ctx,
        "data_quality": {
            "missing_frames": missing_frames,
            "has_critical_missing": any(tf.startswith(("1m", "5m")) for tf in missing_frames),
            "min_requirements": min_bars_requirements,
        }
    }

    # ... 原有 Prompt 构建代码 ...

    # 在 constraints 部分添加缺数警告
    if missing_frames:
        if language == "zh":
            constraints += f"\n\n⚠️ **数据不完整警告**: {', '.join(missing_frames)}"
            constraints += "\n→ 硬规则: 数据不足时禁止开新仓 (BUY)。"
            constraints += "\n→ 只允许: HOLD (观望) 或 SELL (平仓)"
        else:
            constraints += f"\n\n⚠️ **DATA INCOMPLETE**: {', '.join(missing_frames)}"
            constraints += "\n→ HARD RULE: Insufficient data - DO NOT open new positions (BUY)."
            constraints += "\n→ ONLY allowed: HOLD (wait) or SELL (close existing)"

    # ... 继续构建 user_prompt ...

    # 返回值中添加 missing_frames 供主程序使用
    return messages, provider_kwargs, {"missing_frames": missing_frames}
```

## 修改 2: llm_trader_bot.py

### A. 修改调用 UniversalPromptBuilder 的代码

在 run_trading_cycle() 中的 V2.5 分支:

```python
# 原代码:
messages, provider_kwargs = self.universal_prompt_builder.build(...)

# 修改为:
messages, provider_kwargs, extra_info = self.universal_prompt_builder.build(
    provider=self.provider_name,
    symbol=symbol,
    mtf_snapshot=mtf_snapshot,
    portfolio=portfolio_for_prompt,
    time_ctx=time_ctx,
    cash_limit=self.initial_cash,
    allow_new_position=time_ctx['allow_new_position'],
    vix_value=vix_value,
    higher_tf_context=self.higher_cache.get(symbol),
    language="en",
    # 新增配置
    min_bars_requirements={
        "1s": 30,
        "5s": 12,
        "1m": 10,
        "5m": 3,
        "15m": 1,
    },
    enable_degrade=True,  # 可通过 CLI 参数控制
)

missing_frames = extra_info.get("missing_frames", [])

# 打印缺数警告
if missing_frames:
    print(f"  ⚠️  Data incomplete: {', '.join(missing_frames)}")
```

### B. 在决策后应用缺数降级

在获取 LLM 决策后添加:

```python
decision = await self._call_provider_with_messages(messages, provider_kwargs)

if decision:
    action = decision.get("action", "HOLD")
    confidence = decision.get("confidence", 0.0)
    shares = decision.get("shares", 0)

    # 硬规则 1: EOD flatten (已有)
    if time_ctx['should_flatten'] and position_size > 0:
        action = "SELL"
        shares = position_size
        print(f"  🔒 Hard gate: EOD flatten override")

    # 硬规则 2: 禁止新仓窗口 (已有)
    if not time_ctx['allow_new_position'] and position_size == 0 and action == "BUY":
        action = "HOLD"
        shares = 0
        print(f"  🔒 Hard gate: No new positions allowed")

    # 🆕 硬规则 3: 缺数降级
    if missing_frames and position_size == 0 and action == "BUY":
        # 关键周期缺数时禁止开仓
        critical_missing = any(tf.startswith(("1m", "5m")) for tf in missing_frames)
        if critical_missing:
            action = "HOLD"
            shares = 0
            print(f"  🔒 Hard gate: Data incomplete - no new positions")
            print(f"      Missing: {', '.join(missing_frames)}")

    # ... 继续执行 ...
```

## 修改 3: run_single_bot.py (CLI 参数)

添加缺数降级相关参数:

```python
parser.add_argument(
    "--min-bars-1m",
    type=int,
    default=10,
    help="Minimum 1m bars required to allow new positions (default: 10)",
)
parser.add_argument(
    "--min-bars-5m",
    type=int,
    default=3,
    help="Minimum 5m bars required to allow new positions (default: 3)",
)
parser.add_argument(
    "--min-bars-15m",
    type=int,
    default=1,
    help="Minimum 15m bars required to allow new positions (default: 1)",
)
parser.add_argument(
    "--enable-degrade",
    action="store_true",
    default=True,
    help="Enable auto-degrade when data incomplete (default: True)",
)

# 传递给 bot
bot = LLMTraderBot(
    # ... 其他参数 ...
    min_bars_requirements={
        "1s": 30,
        "5s": 12,
        "1m": args.min_bars_1m,
        "5m": args.min_bars_5m,
        "15m": args.min_bars_15m,
    },
    enable_degrade=args.enable_degrade,
)
```

## 测试场景

### 场景 1: 冷启动 (无预填充)

```bash
python run_single_bot.py \
  --provider openai \
  --symbols TSLA \
  --cycles 5 \
  --prefill-5s-mins 0 \
  --enable-degrade
```

**预期日志**:
```
Trading Cycle #1
[TSLA] Updated aggregator with 5 x 1s bars
  ⚠️  Data incomplete: 1s(5/30), 5s(1/12), 1m(0/10), 5m(0/3)
[TSLA] Decision: BUY 10 shares (confidence: 0.75)
  🔒 Hard gate: Data incomplete - no new positions
      Missing: 1m(0/10), 5m(0/3)
[TSLA] Decision: HOLD (confidence: 0.00)
```

### 场景 2: 有预填充

```bash
python run_single_bot.py \
  --provider openai \
  --symbols TSLA \
  --cycles 5 \
  --prefill-5s-mins 20
```

**预期日志**:
```
=== Prefilling aggregator with 20m of 5s bars ===
  ✅ TSLA: Prefilled with 240 x 5s bars

Trading Cycle #1
[TSLA] Updated aggregator with 60 x 1s bars
  ✅ Data sufficient: 1s(60/30), 5s(72/12), 1m(20/10), 5m(4/3)
[TSLA] Decision: BUY 10 shares (confidence: 0.75)
```

### 场景 3: 自定义最小样本要求

```bash
python run_single_bot.py \
  --provider openai \
  --symbols TSLA \
  --min-bars-1m 20 \
  --min-bars-5m 6 \
  --min-bars-15m 2
```

## 验收标准

- [ ] 开盘后前 5 分钟: 即使 LLM 输出 BUY，也被降级为 HOLD
- [ ] 10 分钟后: 1m/5m 样本足够，允许 BUY
- [ ] 日志清晰显示缺数情况和降级原因
- [ ] 预填充后立即满足样本要求
- [ ] 可通过 CLI 参数灵活配置最小样本数

## 优势

1. ✅ **防止冲动交易**: 数据不足时强制观望
2. ✅ **透明可控**: 日志明确显示为什么禁止开仓
3. ✅ **灵活配置**: CLI 参数可调整策略
4. ✅ **向 LLM 明示**: Prompt 中告知数据不完整，让 LLM 也意识到风险
5. ✅ **多层防护**: Prompt 约束 + 主程序硬规则

## 下一步

实现后可继续添加:
- 风险单位系统 (risk_units 0-3)
- 自动回退到方案 A
- 更详细的审计日志
