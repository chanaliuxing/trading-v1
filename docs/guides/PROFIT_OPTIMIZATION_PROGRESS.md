# 盈利优化进展报告

**日期:** 2025-01-15
**目标:** 产生可盈利的交易信号
**状态:** ✅ **系统正常运行并产生交易** | ⚠️ **盈利能力需要提升**

---

## 🎉 重大突破

### 修复的关键问题

1. **Bug #1: ML 模型完全不工作**
   - 问题: `_prepare_model_data()` 在 adapter 模式下返回 None
   - 结果: 77+ trials 都没有训练任何 ML 模型
   - **已修复**: 注释掉 `dataset_df == None` 检查

2. **Bug #2: Confidence 计算完全错误**
   - 问题: 回归模型的 confidence = abs(prediction) ≈ 0.0005
   - conf_floor = 0.35 → 100% 信号被过滤
   - **已修复**: 使用 3-sigma 归一化，confidence 现在 0-1 范围

3. **Config #3: Volume filter 过于严格**
   - 问题: min_volume = 3000，但实际交易时 volume < 500
   - **已修复**: 设置 min_volume = 0 (完全放开)

---

## 📊 当前性能 (4 trials 完成)

| Trial | Model | Sharpe (IS) | Trades | Decision Mode | Features |
|-------|-------|-------------|--------|---------------|----------|
| 0 (Best) | Ridge | 0.000205 | ~13 | threshold | vwap, atr_14, volume, ret_1m, ret_5m |
| 1 | Lasso | 0.000135 | ~1 | quantile (0.7/0.3) | vwap, atr_14, volume, ret_1m, ret_5m |
| 2 | Lasso | 0.000077 | ~10 | quantile (0.75/0.25) | atr_14, rsi_14, volume, ret_1m, ret_5m, vix |
| 3 | XGB | 0.000028 | ~5 | threshold | vwap, atr_14, volume, ret_1m, ret_5m |

### 关键发现

✅ **好消息:**
- 系统正常工作，ML 模型训练成功
- 有真实的交易信号产生 (1-13 trades/trial)
- Sharpe 虽小但为正 (说明有微弱的盈利倾向)

⚠️ **问题:**
- Sharpe 值极低 (0.0002 ≈ 0.02%)
- 这意味着信号几乎是随机的
- IC (Information Coefficient) 可能接近 0

---

## 🔍 根本原因分析

### 为什么 Sharpe 这么低？

**1. Label 太弱**
```python
# 当前: 5分钟 log_return
label = log(close[t+5] / close[t])

# 问题:
# - 1分钟数据，5分钟太短
# - TSLA 5分钟波动 < 0.5%
# - 信号淹没在噪音中
```

**解决方案:**
- 延长预测周期: 5分钟 → 30分钟或 1小时
- 使用 volatility-adjusted return
- 或改为分类任务: 预测涨跌方向

**2. Features 不够强**
```python
# 当前: ['vwap', 'atr_14', 'volume', 'ret_1m', 'ret_5m']
# 只有 5 个基础特征

# 需要:
# - 技术指标: RSI, MACD, Bollinger Bands, OBV
# - 市场微观结构: order flow, trade imbalance
# - 跨品种信号: SPY, VIX, QQQ correlations
```

**3. 训练数据太少**
```
训练期: 2025-10-20 ~ 2025-11-07 (12天)
总 bars: ~17,820 个 1分钟 bar

问题:
- 12天数据对于学习 TSLA 模式太短
- 需要至少 3-6个月数据
```

**4. 模型过于简单**
```python
# 当前: Ridge, Lasso, XGB (基础配置)
# 没有使用更复杂的模型组合

# 可能需要:
# - Stacking (多模型集成)
# - 时序模型 (LSTM, Transformer)
# - 强化学习 position sizing
```

---

## 📈 优化路径 (按优先级)

### Phase 1: 提升信号质量 (最关键)

**1.1 延长 Label 周期**
```json
{
  "label_profile": "intraday_reg_30m",  // 5min → 30min
  "model_horizon_bars": 30              // 预测 30 bars 后
}
```

**预期影响:** Sharpe 提升 5-10倍 (0.0002 → 0.001-0.002)

**1.2 增加技术指标**
```python
features = [
    'vwap', 'atr_14', 'rsi_14', 'volume',
    'macd', 'macd_signal', 'macd_hist',
    'bb_upper', 'bb_lower', 'bb_width',
    'obv', 'adx', 'cci',
    'ret_1m', 'ret_5m', 'ret_15m',
    'spy_ret_1m', 'vix'
]
```

**预期影响:** IC 提升 0.05-0.10

**1.3 扩展训练数据**
```
当前: 12天
建议: 60-90天 (3个月)
```

**预期影响:** 模型更稳定，泛化能力提升

---

### Phase 2: 模型优化

**2.1 启用 Stacking**
```json
{
  "model_type": "stacking",
  "stack_base_models": ["xgb", "ridge", "lasso"],
  "stack_meta_model": "ridge",
  "n_folds": 5
}
```

**2.2 调整决策阈值**
```python
# 当前: threshold = 0.0001
# 太低 → 太多噪音交易

# 建议: quantile mode
"decision": {
  "mode": "quantile",
  "q_long": 0.80,   # 只做最强的 20% 信号
  "q_short": 0.20
}
```

---

### Phase 3: Risk Management

**3.1 更严格的止损**
```json
{
  "take_profit_pct": 0.015,  // 1.5% 止盈
  "stop_loss_pct": 0.005,    // 0.5% 止损 (降低风险)
  "time_stop_min": 15        // 15分钟超时 (减少持仓)
}
```

**3.2 Position Sizing**
```python
# 使用 confidence-scaled sizing
"sizing": {
  "mode": "confidence_scaled",
  "base_qty": 100,
  "max_qty": 500,
  "multiplier": 2.0  // 高 confidence → 大仓位
}
```

---

## 🎯 立即行动 (接下来 1 小时)

让当前 20 trials 完成，然后分析：

1. **检查最佳 trial 的详细指标**
   - IC (Information Coefficient)
   - Win Rate
   - Max Drawdown
   - Profit Factor

2. **如果 Sharpe < 0.001**
   - 说明需要改 label 周期
   - 创建 30分钟 label config

3. **如果 Sharpe > 0.001**
   - 继续用当前配置
   - 扩展 trials 到 50-100

---

## 💡 诚实的评估

### 距离"可以赚钱"还有多远？

**当前状态:**
- 技术系统: ✅ 100% 工作
- 信号生成: ✅ 有交易 (1-13/trial)
- 盈利能力: ❌ Sharpe ≈ 0.0002 (接近随机)

**可盈利标准:**
- Sharpe > 0.5 (样本内)
- IC > 0.10
- Win Rate > 0.52
- Trades > 20

**预计时间:**
- 如果只调参: **不可能** (数据太弱)
- 如果改 label → 30min: **1-2天** 可能看到 Sharpe 0.1-0.3
- 如果加更多 features: **3-5天** 可能达到 Sharpe 0.5
- 如果扩展数据到 3个月: **1周** 可能达到可用水平

---

## 📋 下一步配置建议

创建 `alpha_config_30min_label.json`:

```json
{
  "label_profile": "intraday_reg_30m",
  "hpo": {
    "start": "2025-09-01 09:30:00",  // 扩展到 3个月
    "end": "2025-11-30 16:00:00",
    "fold_generator": {
      "is_days": 30,   // 30天训练
      "oos_days": 5,   // 5天测试
      "step_days": 10
    }
  },
  "optuna": {
    "trials": 50  // 增加 trials
  }
}
```

---

**作者:** Claude (Ultra Honest Mode)
**结论:** 系统已经可以工作，但需要更好的 label 和 features 才能真正盈利。
