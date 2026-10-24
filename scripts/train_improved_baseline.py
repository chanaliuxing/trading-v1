#!/usr/bin/env python3
"""
改进的Baseline模型训练
按照机构级标准进行特征工程和模型训练
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

def train_improved_baseline(symbol='TSLA', label_window=10, min_accuracy=0.55, min_ic=0.05):
    """使用机构级特征工程训练baseline模型"""

    print("=" * 80)
    print("改进Baseline - 机构级特征工程")
    print("=" * 80)
    print(f"股票: {symbol}")
    print(f"预测窗口: {label_window}分钟")
    print(f"最低要求: Accuracy>{min_accuracy}, IC>{min_ic}")
    print()

    # 1. 加载数据 - 使用与生产模型相同的特征
    client = Client(host='localhost', port=9000)
    query = f"""
    SELECT
        ts,
        close,
        volume,
        vwap,
        trades,

        -- 技术指标
        ema_10,
        ema_20,
        ema_50,
        bb_up_20,
        bb_mid_20,
        bb_dn_20,
        atr_5,
        atr_14,
        vol_ema_20,
        r2_20,

        -- 衍生特征
        (close - vwap) / vwap AS close_vwap_spread,
        (ema_10 - ema_20) / ema_20 AS ema_short_long_diff,
        (close - bb_mid_20) / (bb_up_20 - bb_dn_20 + 0.0001) AS bb_position,
        volume / vol_ema_20 AS volume_surge,
        atr_14 / close AS volatility_ratio,

        -- 动量特征
        (close - lag(close, 1) OVER w) / lag(close, 1) OVER w AS ret_1m,
        (close - lag(close, 5) OVER w) / lag(close, 5) OVER w AS ret_5m,
        (close - lag(close, 10) OVER w) / lag(close, 10) OVER w AS ret_10m,

        -- 成交量特征
        (volume - lag(volume, 1) OVER w) / (lag(volume, 1) OVER w + 1) AS volume_change,
        trades / (volume + 1) * 1000 AS avg_trade_size,

        -- Label: {label_window}分钟后的方向
        if(lead(close, {label_window}) OVER w > close, 1, 0) AS label

    FROM trading.features_1m
    WHERE symbol = '{symbol}'
      AND ts >= '2025-10-01'
      AND ts < '2025-11-08'
    WINDOW w AS (ORDER BY ts)
    ORDER BY ts
    """

    df = client.query_dataframe(query)

    print(f"原始数据: {len(df)}行")

    # 2. 清理数据
    df = df.replace([np.inf, -np.inf], np.nan)
    df = df.dropna()

    print(f"清理后数据: {len(df)}行")
    print(f"Label分布: {df['label'].value_counts().to_dict()}")
    print()

    # 3. 准备特征（移除time-based特征）
    feature_cols = [
        # 价格特征
        'close_vwap_spread',
        'ema_short_long_diff',
        'bb_position',

        # 波动率特征
        'volatility_ratio',
        'atr_5',
        'atr_14',

        # 动量特征
        'ret_1m',
        'ret_5m',
        'ret_10m',

        # 成交量特征
        'volume_surge',
        'volume_change',
        'avg_trade_size',

        # 技术指标
        'r2_20'
    ]

    print(f"使用特征: {len(feature_cols)}个")
    for feat in feature_cols:
        print(f"  - {feat}")
    print()

    X = df[feature_cols].values
    y = df['label'].values

    # 验证数据质量
    if np.any(np.isnan(X)) or np.any(np.isinf(X)):
        print("❌ 错误: 特征中仍有NaN或Inf值")
        return False

    # 4. 时间序列交叉验证
    tscv = TimeSeriesSplit(n_splits=5)
    accuracies, aucs, ics = [], [], []

    for fold, (train_idx, val_idx) in enumerate(tscv.split(X), 1):
        X_train, X_val = X[train_idx], X[val_idx]
        y_train, y_val = y[train_idx], y[val_idx]

        # 使用更好的XGBoost参数
        model = XGBClassifier(
            max_depth=5,           # 增加深度
            learning_rate=0.05,
            n_estimators=100,      # 增加树数量
            subsample=0.8,         # 防止过拟合
            colsample_bytree=0.8,  # 特征采样
            random_state=42,
            eval_metric='logloss'
        )
        model.fit(X_train, y_train, verbose=False)

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

    # 5. 总结
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

    success = (avg_acc > min_accuracy) and (avg_ic > min_ic)

    if success:
        print("✅ 成功！改进特征显示市场具有可预测性")
        return True
    else:
        print("❌ 失败！即使使用机构级特征也无法达到要求")
        print("   与简单baseline对比分析...")
        return False

    return False

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--symbol', default='TSLA')
    parser.add_argument('--label-window', type=int, default=10)
    parser.add_argument('--min-accuracy', type=float, default=0.55)
    parser.add_argument('--min-ic', type=float, default=0.05)
    args = parser.parse_args()

    success = train_improved_baseline(
        symbol=args.symbol,
        label_window=args.label_window,
        min_accuracy=args.min_accuracy,
        min_ic=args.min_ic
    )
    sys.exit(0 if success else 1)
