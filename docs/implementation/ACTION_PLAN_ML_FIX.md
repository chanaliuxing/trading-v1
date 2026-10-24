# ML交易系统修复行动计划

**创建时间**: 2025-11-12
**状态**: 🚨 紧急 - 模型完全无效
**目标**: 将模型Accuracy从50.85%提升到>55%，IC从0.016提升到>0.05

---

## ✅ 已完成的诊断

### 1. 数据质量 ⚠️ 基本可用但有缺陷
- ✅ **有数据**: 2342根1分钟bar (2025-11-03至11-06, 4个交易日)
- ✅ **无缺失**: close, volume 100%完整
- ⚠️ **部分缺失**: atr_14缺失16.7% (391/2342)
- ⚠️ **不完整**: 11-04只有391根bar (应该780根)

### 2. 模型质量 ❌ 完全失败
```json
{
  "accuracy": 0.5085,     // ❌ = 抛硬币
  "auc": 0.514,           // ❌ ≈ 随机
  "ic": 0.016,            // ❌ 零相关性
  "sharpe": 4.8,          // ⚠️ 与低Acc矛盾 → 可能数据泄漏
  "label": "label_h30"    // 30分钟后方向
}
```

### 3. 特征工程 ❌ 严重问题
**Feature Importance分析**:
```
minute_of_day: 98.5   ← 🚨 模型只学到"几点钟交易"
close/BB/EMA:  30-40  ← 技术指标贡献极低
```

**问题**:
1. `minute_of_day`不应该是预测特征 → 导致过拟合到时间规律
2. 大量冗余特征 (3个EMA, 3个BB) → 高共线性
3. 缺少关键特征: 订单流、微观结构、市场regime

### 4. 策略问题 ❌ 不交易
- Backtest运行 ✅
- 但 **0笔订单** ❌
- 原因: 模型置信度太低 + ATR缺失

---

## 🎯 修复优先级

### P0 - 今天必须完成 (2-3小时)

#### 任务1: 移除minute_of_day特征 ✋ **立即**
```python
# 位置: 训练脚本或特征工程pipeline
# 动作: 从feature_cols中删除'minute_of_day'

# 原因:
# - 导致模型学到"10点做多，2点做空"这种无意义规律
# - 完全无泛化能力
# - Feature importance占98% → 其他特征被忽略
```

#### 任务2: 缩短预测窗口 ⚠️ **立即**
```python
# 当前: label_h30 (30分钟后)
# 改为: label_m10 (10分钟后)

# 原因:
# - 30分钟太长，技术指标无法预测
# - 10分钟更适合intraday交易
# - 更短窗口 = 更高信噪比
```

#### 任务3: 训练最简baseline模型 📊 **今天**
```bash
# 只用3个最基本特征
python scripts/train_baseline_model.py \
  --features "ret_1m,volume_ratio,spread_bps" \
  --label "label_m10" \
  --symbol TSLA \
  --min_accuracy 0.55 \
  --min_ic 0.05
```

**成功标准**:
- Accuracy > 55% (至少比随机好5%)
- IC > 0.05 (有一定相关性)
- AUC > 0.55

**如果失败** → 说明该市场/时间框架根本不可预测，放弃ML路线

---

### P1 - 本周完成 (1-2天)

#### 任务4: 改进特征工程
**移除冗余特征**:
- 只保留1个EMA (ema_20)
- 只保留1个BB (bb_width_pct)
- 移除高共线性特征

**添加关键特征**:
```python
# 订单流特征
- buy_sell_ratio      # 主动买/主动卖比例
- large_trade_pct     # 大单占比
- trade_imbalance     # 成交不平衡度

# 微观结构
- spread_bps          # 买卖价差 (基点)
- depth_imbalance     # 订单簿深度不平衡
- tick_rule_signal    # Tick rule方向

# 市场regime
- volatility_regime   # 高/低波动状态
- volume_surprise     # 成交量异常
- correlation_spy     # 与SPY相关性

# 价格动量
- ret_acceleration    # 收益率加速度
- reversal_signal     # 反转信号
```

#### 任务5: 重新训练生产模型
```bash
python wf_hpo.py \
  --model xgboost \
  --features improved_features.json \
  --label label_m10 \
  --cv-folds 5 \
  --optuna-trials 100 \
  --objective sharpe \
  --min-trades 20
```

---

### P2 - 如果baseline成功 (本周末)

#### 任务6: 优化模型架构
- Ensemble: XGBoost + LightGBM
- 添加meta-features (model uncertainty, prediction stability)
- 使用更复杂的label (风险调整收益而非单纯方向)

#### 任务7: 优化策略逻辑
- 动态conf_floor (根据market regime调整)
- Multi-timeframe确认
- Position sizing by Kelly criterion

---

## 📋 具体执行步骤

### 第一步: 创建baseline训练脚本 (30分钟)

```bash
cd .
cat > scripts/train_baseline_model.py << 'EOF'
#!/usr/bin/env python3
"""
Baseline模型训练 - 最简单的3特征模型
用于验证市场是否可预测
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from clickhouse_driver import Client
import pandas as pd
import numpy as np
from sklearn.model_selection import TimeSeriesSplit
from xgboost import XGBClassifier
from sklearn.metrics import accuracy_score, roc_auc_score
import warnings
warnings.filterwarnings('ignore')

def train_baseline(symbol='TSLA', label_window=10, min_accuracy=0.55, min_ic=0.05):
    """训练baseline模型验证可预测性"""

    print("=" * 80)
    print("Baseline模型训练 - 验证市场可预测性")
    print("=" * 80)
    print(f"股票: {symbol}")
    print(f"预测窗口: {label_window}分钟")
    print(f"最低要求: Accuracy>{min_accuracy}, IC>{min_ic}")
    print()

    # 1. 加载数据
    client = Client(host='localhost', port=9000)
    query = f"""
    SELECT
        ts,
        close,
        volume,

        -- 计算最简单的3个特征
        (close - lag(close, 1) OVER w) / lag(close, 1) OVER w AS ret_1m,
        volume / avg(volume) OVER (ORDER BY ts ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS volume_ratio,
        0.0 AS spread_bps,  -- 暂时用0，后续添加真实spread

        -- 计算label: {label_window}分钟后的方向
        if(lead(close, {label_window}) OVER w > close, 1, 0) AS label

    FROM trading.features_1m
    WHERE symbol = '{symbol}'
      AND ts >= '2025-10-01'
      AND ts < '2025-11-08'
    WINDOW w AS (ORDER BY ts)
    ORDER BY ts
    """

    df = client.query_dataframe(query)
    df = df.dropna()

    print(f"数据加载完成: {len(df)}行")
    print(f"Label分布: {df['label'].value_counts().to_dict()}")
    print()

    # 2. 准备特征
    feature_cols = ['ret_1m', 'volume_ratio', 'spread_bps']
    X = df[feature_cols].values
    y = df['label'].values

    # 3. 时间序列交叉验证
    tscv = TimeSeriesSplit(n_splits=5)
    accuracies, aucs, ics = [], [], []

    for fold, (train_idx, val_idx) in enumerate(tscv.split(X), 1):
        X_train, X_val = X[train_idx], X[val_idx]
        y_train, y_val = y[train_idx], y[val_idx]

        # 训练最简单的XGBoost
        model = XGBClassifier(
            max_depth=3,
            learning_rate=0.05,
            n_estimators=50,
            random_state=42
        )
        model.fit(X_train, y_train)

        # 评估
        y_pred = model.predict(X_val)
        y_pred_proba = model.predict_proba(X_val)[:, 1]

        acc = accuracy_score(y_val, y_pred)
        auc = roc_auc_score(y_val, y_pred_proba)
        ic = np.corrcoef(y_pred_proba, y_val)[0, 1]

        accuracies.append(acc)
        aucs.append(auc)
        ics.append(ic)

        print(f"Fold {fold}: Acc={acc:.4f}, AUC={auc:.4f}, IC={ic:.4f}")

    # 4. 总结
    avg_acc = np.mean(accuracies)
    avg_auc = np.mean(aucs)
    avg_ic = np.mean(ics)

    print()
    print("=" * 80)
    print("结果总结")
    print("=" * 80)
    print(f"平均 Accuracy: {avg_acc:.4f} (要求: >{min_accuracy})")
    print(f"平均 AUC:      {avg_auc:.4f} (要求: >0.55)")
    print(f"平均 IC:       {avg_ic:.4f} (要求: >{min_ic})")
    print()

    # 5. 判断
    success = (avg_acc > min_accuracy) and (avg_ic > min_ic)

    if success:
        print("✅ 成功！市场具有可预测性，可以继续优化特征和模型")
    else:
        print("❌ 失败！即使最简单的baseline也无法预测")
        print("   建议:")
        print("   1. 尝试更短的预测窗口 (5分钟)")
        print("   2. 检查数据质量和标签定义")
        print("   3. 考虑放弃ML，使用规则based策略")

    return success

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--symbol', default='TSLA')
    parser.add_argument('--label-window', type=int, default=10)
    parser.add_argument('--min-accuracy', type=float, default=0.55)
    parser.add_argument('--min-ic', type=float, default=0.05)
    args = parser.parse_args()

    success = train_baseline(
        symbol=args.symbol,
        label_window=args.label_window,
        min_accuracy=args.min_accuracy,
        min_ic=args.min_ic
    )
    sys.exit(0 if success else 1)
EOF

chmod +x scripts/train_baseline_model.py
```

### 第二步: 运行baseline测试 (5分钟)

```bash
# 测试10分钟预测窗口
venv/bin/python scripts/train_baseline_model.py --label-window 10

# 如果失败，尝试5分钟
venv/bin/python scripts/train_baseline_model.py --label-window 5
```

### 第三步: 根据结果决定下一步

**如果Baseline成功** (Acc>55%, IC>0.05):
1. 添加更多特征 (订单流、微观结构)
2. 优化模型超参数
3. 训练ensemble

**如果Baseline失败** (Acc≈50%):
1. 该市场/时间框架不可预测
2. 改用规则based策略:
   - Mean reversion (RSI极值反转)
   - Momentum (突破+成交量确认)
   - Statistical arbitrage (pairs trading)

---

## 🎓 核心经验教训

### 1. 为什么Accuracy=50%是致命的？

```
准确率50% = 抛硬币
↓
模型完全无用
↓
所有信号都是噪音
↓
即使严格的置信度门槛也没用 (garbage in)
```

### 2. 为什么需要严格的质量标准？

你说得对：**置信度要严格，因为不想garbage in garbage out**

但关键是：
- ✅ **严格门槛是对的** → 过滤低质量信号
- ❌ **当前问题**: 源头就是garbage (模型Acc=50%)
- 💡 **解决方案**: 先修源头，再设门槛

类比：
```
金矿选矿:
- 严格纯度标准 ✅ (你的想法)
- 但矿石里没有金子 ❌ (当前问题)
- 解决: 找含金矿石 💡 (修模型)
```

### 3. minute_of_day为什么是毒药？

```python
# 模型学到的pattern:
if hour == 10:
    return "做多"  # 开盘上涨
elif hour == 14:
    return "做空"  # 午后下跌
```

这不是预测能力，是过拟合历史时间规律！
→ 新数据完全无效
→ Accuracy=50%

---

## ✅ 下一次会话的检查清单

1. [ ] 已移除minute_of_day特征
2. [ ] 已将label改为label_m10 (10分钟)
3. [ ] Baseline Accuracy > 55%
4. [ ] Baseline IC > 0.05
5. [ ] 如baseline失败，已决定改用规则策略

---

**最后更新**: 2025-11-12
**负责人**: Trading Team
**优先级**: 🔥 P0 - 立即执行
