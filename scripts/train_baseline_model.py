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

    # 清理数据: 移除NaN和inf
    print(f"原始数据: {len(df)}行")
    df = df.replace([np.inf, -np.inf], np.nan)
    df = df.dropna()
    print(f"清理后数据: {len(df)}行")
    print(f"Label分布: {df['label'].value_counts().to_dict()}")
    print()

    # 2. 准备特征
    feature_cols = ['ret_1m', 'volume_ratio', 'spread_bps']
    X = df[feature_cols].values
    y = df['label'].values

    # 验证没有inf/nan
    if np.any(np.isnan(X)) or np.any(np.isinf(X)):
        print("❌ 错误: 特征中仍有NaN或Inf值")
        return False

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
