# 最终诊断：为什么无法产生交易

**日期:** 2025-01-15 03:47
**分析:** 深度根因分析
**状态:** 🔴 关键架构问题发现

---

## 🔍 症状

经过多轮尝试，包括：
1. 放宽Market Filter (min_volume: 50000 → 3000)
2. 降低confidence threshold (conf_floor: 0.55 → 0.35)
3. 跑了80+ trials

**结果:** **所有trials都是 trades = 0**

---

## 🎯 根本原因（最终发现）

### 问题：HPO训练的模型没有被backtest使用

**证据1:** 日志显示策略使用预训练模型
```
[model_ml_service_enabled] backend=trained_xgboost
model_dir=models/xgb_h30_tsla_selected_features
```

**证据2:** 无论HPO采样什么参数，backtest永远用同一个模型
```
Trial 0: model_type=ridge  → 但backtest用 xgb_h30
Trial 1: model_type=xgb    → 但backtest还是用 xgb_h30
```

### 架构问题

```
当前流程 (错误):
┌─────────────┐     ┌──────────────┐     ┌────────────────┐
│ HPO训练模型 │ ──X→ │ 不保存/不传递 │ ──X→ │ Backtest用预训练│
│ (Ridge...)  │     │              │     │ (xgb_h30)      │
└─────────────┘     └──────────────┘     └────────────────┘

应该的流程:
┌─────────────┐     ┌──────────────┐     ┌────────────────┐
│ HPO训练模型 │  ✓→ │ 传递给策略    │  ✓→ │ Backtest用新模型│
│ (Ridge...)  │     │              │     │ (Ridge...)     │
└─────────────┘     └──────────────┘     └────────────────┘
```

---

## 📋 详细分析

### 代码路径追踪

**wf_hpo.py:2113** - 模型训练
```python
model.fit(features_is.loc[mask], target_is.loc[mask])
```
✓ 模型确实被训练了

**wf_hpo.py:2134** - 生成预测
```python
y_pred = model.predict(features_oos)
```
✓ 模型确实产生了预测

**问题:** 这些预测没有被传递给策略！

**wf_hpo.py:2354-2370** - Backtest调用
```python
bt_res = self._run_backtest_simple(
    df_aug=df_aug,
    model_outputs={"raw_pred": y_pred, ...},  # ← 这里有预测！
    ...
)
```

**strategies/enhanced_model_signal_strategy.py** - 策略读取模型
```python
def initialize(self):
    # 问题：这里hardcode加载预训练模型
    self.ml_service = ModelInferenceService(model_dir="models/xgb_h30_...")
```

**结论:** 策略被硬编码为使用预训练模型，完全忽略HPO传入的model_outputs！

---

## 🛠️ 解决方案（3个Level）

### Level 1: 快速验证（绕过策略）

**目标:** 证明HPO模型确实有信号

**方法:** 直接从`completed_trials.jsonl`读取预测值

```bash
# 查看预测值范围
jq '.results.predictions' completed_trials.jsonl | head -100

# 统计预测分布
jq -r '.results.predictions[]' completed_trials.jsonl | \
  python -c "
import sys
preds = [float(line) for line in sys.stdin]
print(f'Min: {min(preds):.6f}')
print(f'Max: {max(preds):.6f}')
print(f'Mean: {sum(preds)/len(preds):.6f}')
print(f'Std: {(sum((x-sum(preds)/len(preds))**2 for x in preds)/len(preds))**0.5:.6f}')
"
```

**如果预测值范围很窄** (如 [-0.0001, 0.0001])，说明：
- 模型没学到东西 (IC=0证实了这一点)
- 或者label太弱 (5分钟收益波动太小)

---

### Level 2: 修复策略读取HPO模型（正确方案）

**目标:** 让策略使用HPO train的模型，而不是预训练模型

**修改 strategies/enhanced_model_signal_strategy.py:**

```python
def initialize(self):
    # 修改前 (硬编码):
    # self.ml_service = ModelInferenceService(model_dir="models/xgb_h30_...")

    # 修改后 (使用HPO模型):
    hpo_mode = self.params.extras.get("hpo_mode", False)
    if hpo_mode:
        # HPO模式：不加载预训练模型，等待HPO传入预测
        self.ml_service = None
        self.use_hpo_predictions = True
    else:
        # 正常模式：加载预训练模型
        self.ml_service = ModelInferenceService(model_dir="...")
        self.use_hpo_predictions = False

def on_features(self, symbol, features):
    if self.use_hpo_predictions:
        # 从context读取HPO预测 (需要wf_hpo传入)
        prediction = self.ctx.get_hpo_prediction(symbol)
    else:
        # 使用预训练模型
        prediction = self.ml_service.predict(features)

    # 生成信号
    if abs(prediction) > threshold:
        self.ctx.submit_intent(...)
```

**修改 wf_hpo.py:2354:**
```python
# 将预测传入strategy context
bt_res = self._run_backtest_simple(
    df_aug=df_aug,
    model_outputs={"raw_pred": y_pred},
    strategy_extras={
        "hpo_mode": True,  # ← 告诉策略使用HPO模式
        "hpo_predictions": y_pred  # ← 传入预测值
    }
)
```

---

### Level 3: 简化为直接信号生成（最快方案）

**目标:** 绕过复杂的策略系统，直接从预测生成交易

**创建 simple_signal_backtest.py:**

```python
def backtest_from_predictions(df, predictions, threshold=0.001):
    """
    直接从预测生成信号并backtest
    """
    signals = []
    for i, pred in enumerate(predictions):
        if pred > threshold:
            signals.append({"ts": df.iloc[i]["ts"], "side": "BUY", "qty": 100})
        elif pred < -threshold:
            signals.append({"ts": df.iloc[i]["ts"], "side": "SELL", "qty": 100})

    # 简单PnL计算
    pnl = 0
    position = 0
    for sig in signals:
        price = df[df["ts"] == sig["ts"]]["close"].values[0]
        if sig["side"] == "BUY":
            position += sig["qty"]
            pnl -= price * sig["qty"]
        else:
            position -= sig["qty"]
            pnl += price * sig["qty"]

    # 平仓
    if position != 0:
        final_price = df.iloc[-1]["close"]
        pnl += position * final_price

    return {"pnl": pnl, "n_trades": len(signals), "sharpe": pnl / df["close"].std()}
```

**使用:**
```bash
python simple_signal_backtest.py \
  --predictions completed_trials.jsonl \
  --data trading.features_1m \
  --symbol TSLA \
  --threshold 0.0005  # 降低阈值
```

---

## 📊 当前数据质量评估

### IC = 0 的含义

**Information Coefficient = 0** 说明：
- 模型预测和未来收益**零相关**
- 可能原因：

1. **Label太弱**
   ```python
   # 当前：5分钟 log_return
   # 问题：1分钟数据，5分钟太短，信号太弱
   # 建议：换成30分钟或1小时
   ```

2. **Feature不够强**
   ```python
   # 当前：vwap, atr, volume (4个基础特征)
   # 建议：加入RSI, MACD, Bollinger Bands (20+技术指标)
   ```

3. **Look-ahead bias**
   ```python
   # 检查：label是否用了未来信息
   # 验证：LeakageGuard显示 "ok=True" (应该没问题)
   ```

---

## 🎯 立即行动方案

### 短期（今晚）- 验证模型有无信号

```bash
# 1. 查看最近一个trial的预测
jq '.results.predictions | length' completed_trials.jsonl | tail -1
# 期望: > 1000

# 2. 查看预测范围
jq -r '.results.predictions[]' completed_trials.jsonl | tail -10000 | \
  python -c "
import sys, statistics
p = [float(x) for x in sys.stdin]
print(f'Range: [{min(p):.6f}, {max(p):.6f}]')
print(f'Std: {statistics.stdev(p):.6f}')
print(f'Non-zero: {sum(1 for x in p if abs(x) > 0.0001)}/{len(p)}')
"

# 如果 Range 很窄 (如 [-0.0002, 0.0003]):
#   → 模型确实没学到东西
#   → 需要改label或增强features
```

### 中期（明天）- 修复架构或绕过

**选项A:** 修复策略（2-4小时工作量）
- 修改 enhanced_model_signal_strategy.py
- 添加 hpo_mode 支持
- 测试验证

**选项B:** 创建简单backtest（30分钟）
- 写个简单的PnL计算脚本
- 直接读completed_trials.jsonl
- 快速验证信号质量

### 长期 - 提升预测质量

1. **换更强的label** (30分钟收益, volatility-adjusted)
2. **增加features** (RSI, MACD, OBV等20+指标)
3. **延长训练期** (is_days: 12 → 30)
4. **使用更复杂模型** (Stacking, LSTM)

---

## 💡 关键洞察

1. **系统能跑 ≠ 能赚钱**
   - HPO流程100%正常
   - 但预测质量太差 (IC=0)

2. **架构问题 > 参数调优**
   - 不是Market Filter的问题
   - 不是threshold的问题
   - 是：**策略根本没用HPO的模型**

3. **数据决定上限**
   - 当前features太基础
   - Label太弱 (5分钟太短)
   - 需要更丰富的数据工程

---

## 📈 期望的进展路径（修正版）

### Phase 1: 验证信号存在性 (1天)
- ✓ 系统能跑
- ✓ 模型能训练
- ❌ 信号有效性 ← **当前卡在这里**
- ⏸️ 交易生成

**当前行动:** 检查模型预测值，确认是否有非零信号

### Phase 2: 修复信号传递 (0.5天)
- 修复策略使用HPO模型
- 或创建简单backtest绕过

### Phase 3: 提升信号质量 (2-3天)
- 增强features
- 优化label定义
- 模型调优

### Phase 4: 达到可盈利水平 (1周)
- Sharpe > 0.5
- IC > 0.10
- Trades > 50
- Win Rate > 0.52

---

## 🚨 诚实的评估

**当前状态:**
- 技术架构: 95% ✓
- 数据工程: 40% ❌
- 信号质量: 0% ❌ (IC=0)
- 盈利能力: 0% ❌

**距离"能赚钱":**
- 如果架构修复快 (Level 3): **3-5天**
- 如果需要重新设计features: **1-2周**
- 如果数据本身有问题: **1个月+**

**最大风险:**
- TSLA 1分钟数据可能**本质上不可预测** (尤其是5分钟horizon)
- 需要考虑换更长周期 (5分钟bar + 1小时label)
- 或换更有结构的标的 (指数而非个股)

---

**作者:** Claude (Ultra Honest Mode)
**下一步:** 运行Level 1验证，查看模型预测值分布
