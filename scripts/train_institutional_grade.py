#!/usr/bin/env python3
"""
机构级ML模型训练 - Phase 1紧急修复
实施所有关键改进：特征标准化、改进标签、正确的时间序列分割
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import json

from clickhouse_driver import Client
import pandas as pd
import numpy as np
from sklearn.model_selection import TimeSeriesSplit
from sklearn.preprocessing import RobustScaler
from xgboost import XGBClassifier
from sklearn.metrics import accuracy_score, roc_auc_score
from scipy.stats import spearmanr
import joblib
import warnings
warnings.filterwarnings('ignore')

def train_institutional_grade(
    symbol='TSLA',
    label_window=10,
    min_accuracy=0.55,
    min_ic=0.05,
    commission_bps=3.5,
    slippage_bps=1.5,
    label_threshold_bps=10.0
):
    """机构级模型训练 - 集成所有最佳实践"""

    print("=" * 80)
    print("机构级ML模型训练 - Phase 1修复")
    print("=" * 80)
    print(f"股票: {symbol}")
    print(f"预测窗口: {label_window}分钟")
    print(f"交易成本: {commission_bps + slippage_bps} bps")
    print(f"标签阈值: ±{label_threshold_bps} bps")
    print(f"质量要求: Accuracy>{min_accuracy}, IC>{min_ic}")
    print()

    # 1. 加载数据 - 使用改进的标签构建方法
    client = Client(host='localhost', port=9000)

    total_cost_bps = commission_bps + slippage_bps

    query = f"""
    WITH features AS (
        SELECT
            ts,
            symbol,
            close,
            open,
            high,
            low,
            volume,
            vwap,
            trades,
            ema_10,
            ema_20,
            ema_50,
            bb_up_20,
            bb_mid_20,
            bb_dn_20,
            atr_5,
            atr_14,
            rsi_14,
            vol_ema_20,
            r2_20
        FROM trading.features_1m FINAL
        WHERE symbol = '{symbol}'
          AND ts >= '2025-10-01'
          AND ts < '2025-11-08'
    ),
    enriched AS (
        SELECT
            *,
            -- 衍生特征（原始值，后续标准化）
            (close - vwap) / NULLIF(vwap, 0) AS close_vwap_spread,
            (ema_10 - ema_20) / NULLIF(ema_20, 0) AS ema_short_long_diff,
            (close - bb_mid_20) / NULLIF((bb_up_20 - bb_dn_20), 0) AS bb_position,
            atr_14 / NULLIF(close, 0) AS volatility_ratio,
            volume / NULLIF(vol_ema_20, 0) AS volume_surge,

            -- 动量特征
            (close - lag(close, 1) OVER w) / NULLIF(lag(close, 1) OVER w, 0) AS ret_1m,
            (close - lag(close, 5) OVER w) / NULLIF(lag(close, 5) OVER w, 0) AS ret_5m,
            (close - lag(close, 10) OVER w) / NULLIF(lag(close, 10) OVER w, 0) AS ret_10m,

            -- 成交量变化
            (volume - lag(volume, 1) OVER w) / NULLIF(lag(volume, 1) OVER w, 0) AS volume_change,
            trades / NULLIF(volume, 0) * 1000 AS avg_trade_size,

            -- ✅ 改进的标签构建
            lead(open, 1) OVER w AS entry_price,
            lead(close, {label_window}) OVER w AS exit_price,
            lead(ts, 1) OVER w AS next_ts,

            -- 检测时段跨越
            CASE
                WHEN toHour(lead(ts, 1) OVER w) < 9 OR toHour(lead(ts, 1) OVER w) >= 16 THEN 1
                WHEN toDayOfWeek(ts) = 5 AND toDayOfWeek(lead(ts, 1) OVER w) = 1 THEN 1
                ELSE 0
            END AS crosses_session

        FROM features
        WINDOW w AS (ORDER BY ts)
    ),
    labeled AS (
        SELECT
            *,
            -- ✅ 净收益（扣除交易成本）
            (exit_price - entry_price) / NULLIF(entry_price, 0) - {total_cost_bps / 10000} AS net_return,

            -- ✅ 分类标签（使用统计显著阈值）
            CASE
                WHEN crosses_session = 1 THEN NULL  -- 过滤跨时段
                WHEN (exit_price - entry_price) / NULLIF(entry_price, 0) - {total_cost_bps / 10000} >= {label_threshold_bps / 10000} THEN 1
                WHEN (exit_price - entry_price) / NULLIF(entry_price, 0) - {total_cost_bps / 10000} <= -{label_threshold_bps / 10000} THEN 0
                ELSE NULL  -- 丢弃噪音区域
            END AS label
        FROM enriched
    )
    SELECT *
    FROM labeled
    WHERE label IS NOT NULL  -- 只保留有效标签
    ORDER BY ts
    """

    df = client.query_dataframe(query)

    print(f"原始数据: {len(df)}行")

    # 2. 清理数据
    df = df.replace([np.inf, -np.inf], np.nan)
    df = df.dropna()

    print(f"清理后数据: {len(df)}行")
    print(f"Label分布: {df['label'].value_counts().to_dict()}")

    label_balance = df['label'].mean()
    if label_balance < 0.30 or label_balance > 0.70:
        print(f"⚠️  警告: Label不平衡 ({label_balance:.1%})，建议调整阈值")
    print()

    # 3. ✅ 特征分组与标准化（机构级最佳实践）
    feature_groups = {
        'price': ['close_vwap_spread', 'ema_short_long_diff', 'bb_position'],
        'volatility': ['volatility_ratio', 'atr_5', 'atr_14'],
        'volume': ['volume_surge', 'volume_change', 'avg_trade_size'],
        'momentum': ['ret_1m', 'ret_5m', 'ret_10m'],
        'indicator': ['rsi_14', 'r2_20']
    }

    all_features = []
    for features in feature_groups.values():
        all_features.extend(features)

    print(f"使用特征: {len(all_features)}个")
    for group, features in feature_groups.items():
        print(f"  [{group}]: {', '.join(features)}")
    print()

    # ✅ 按组标准化（RobustScaler抗异常值）
    X_raw = df[all_features].copy()
    y = df['label'].values

    # 使用RobustScaler（基于中位数和IQR，对异常值鲁棒）
    scaler = RobustScaler()
    X_scaled = scaler.fit_transform(X_raw)
    X_scaled = pd.DataFrame(X_scaled, columns=all_features, index=X_raw.index)

    print("✅ 特征标准化完成 (RobustScaler)")
    print(f"   标准化前范围: {X_raw.min().min():.2f} ~ {X_raw.max().max():.2f}")
    print(f"   标准化后范围: {X_scaled.min().min():.2f} ~ {X_scaled.max().max():.2f}")
    print()

    # 验证数据质量
    if np.any(np.isnan(X_scaled)) or np.any(np.isinf(X_scaled)):
        print("❌ 错误: 标准化后仍有NaN或Inf值")
        return False

    # 4. ✅ 时间序列交叉验证（保持时间顺序）
    print("=" * 80)
    print("时间序列交叉验证（5折）")
    print("=" * 80)

    tscv = TimeSeriesSplit(n_splits=5)
    accuracies, aucs, ics, irs = [], [], [], []
    # 用于校准决策阈值的 out-of-fold 预测
    oof_pred_proba = np.zeros_like(y, dtype=float)
    oof_mask = np.zeros_like(y, dtype=bool)

    for fold, (train_idx, val_idx) in enumerate(tscv.split(X_scaled), 1):
        X_train, X_val = X_scaled.iloc[train_idx], X_scaled.iloc[val_idx]
        y_train, y_val = y[train_idx], y[val_idx]

        # 训练期和验证期时间范围
        train_start = df.iloc[train_idx[0]]['ts']
        train_end = df.iloc[train_idx[-1]]['ts']
        val_start = df.iloc[val_idx[0]]['ts']
        val_end = df.iloc[val_idx[-1]]['ts']

        # XGBoost with improved hyperparameters
        model = XGBClassifier(
            max_depth=5,
            learning_rate=0.05,
            n_estimators=100,
            subsample=0.8,
            colsample_bytree=0.8,
            random_state=42,
            eval_metric='logloss',
            tree_method='hist'  # 更快的训练
        )
        model.fit(X_train, y_train, verbose=False)

        # 评估
        y_pred = model.predict(X_val)
        y_pred_proba = model.predict_proba(X_val)[:, 1]

        acc = accuracy_score(y_val, y_pred)
        auc = roc_auc_score(y_val, y_pred_proba)
        ic, _ = spearmanr(y_pred_proba, y_val)  # ✅ 使用Spearman IC

        # ✅ 计算IR（Information Ratio）
        # 将验证集按天分组计算每日IC
        val_df = df.iloc[val_idx].copy()
        val_df['pred_proba'] = y_pred_proba
        val_df['date'] = pd.to_datetime(val_df['ts']).dt.date

        daily_ics = []
        for date, group in val_df.groupby('date'):
            if len(group) > 10:  # 至少10个样本
                daily_ic, _ = spearmanr(group['pred_proba'], group['label'])
                daily_ics.append(daily_ic)

        ir = np.mean(daily_ics) / np.std(daily_ics) if len(daily_ics) > 1 else 0.0

        accuracies.append(acc)
        aucs.append(auc)
        ics.append(ic)
        irs.append(ir)

        # 累积 OOF 预测，用于后续阈值/风控参数数据驱动校准
        oof_pred_proba[val_idx] = y_pred_proba
        oof_mask[val_idx] = True

        print(f"Fold {fold}: {train_start.strftime('%m-%d')} ~ {train_end.strftime('%m-%d')} → {val_start.strftime('%m-%d')} ~ {val_end.strftime('%m-%d')}")
        print(f"         Acc={acc:.4f}, AUC={auc:.4f}, IC={ic:.4f}, IR={ir:.4f}")

    # 5. 总结
    avg_acc = np.mean(accuracies)
    avg_auc = np.mean(aucs)
    avg_ic = np.mean(ics)
    avg_ir = np.mean(irs)

    print()
    print("=" * 80)
    print("机构级验证结果")
    print("=" * 80)
    print(f"平均 Accuracy: {avg_acc:.4f} (要求: >{min_accuracy})")
    print(f"平均 AUC:      {avg_auc:.4f} (要求: >0.55)")
    print(f"平均 IC:       {avg_ic:.4f} (要求: >{min_ic})")
    print(f"平均 IR:       {avg_ir:.4f} (行业标准: >1.5)")
    print()

    # 与baseline对比
    print("📊 改进对比:")
    print(f"   简单baseline (3特征):    Acc=51.2%, IC=0.030")
    print(f"   机构级特征 (13特征):      Acc=51.6%, IC=0.031")
    print(f"   ✅ Phase 1修复 ({len(all_features)}特征): Acc={avg_acc:.1%}, IC={avg_ic:.3f}")
    print()

    success = (avg_acc > min_accuracy) and (avg_ic > min_ic)

    if success:
        print("✅ 成功！Phase 1修复达到机构级标准")
        print("   - 特征标准化: ✅")
        print("   - 改进标签构建: ✅")
        print("   - 时间序列分割: ✅")
        print()
        print("📈 预期收益:")
        print(f"   IC: {avg_ic:.3f} (提升 {(avg_ic/0.030 - 1)*100:.0f}%)")
        print(f"   AUC: {avg_auc:.3f}")
        print(f"   IR: {avg_ir:.2f}")

        # === Phase 1.5: 构建模型包 (model + scaler + features + metadata) ===
        # 拟合最终模型（使用全部标准化后的数据）
        final_model = XGBClassifier(
            max_depth=5,
            learning_rate=0.05,
            n_estimators=100,
            subsample=0.8,
            colsample_bytree=0.8,
            random_state=42,
            eval_metric='logloss',
            tree_method='hist'
        )
        final_model.fit(X_scaled, y, verbose=False)

        # 只用有效的 OOF 预测来校准阈值
        if oof_mask.any():
            val_preds_all = oof_pred_proba[oof_mask]
        else:
            val_preds_all = final_model.predict_proba(X_scaled)[:, 1]

        # 数据驱动的置信度阈值：以 80/20 分位点为基准，并与保底阈值取极值
        high_thr = float(np.percentile(val_preds_all, 80))
        low_thr = float(np.percentile(val_preds_all, 20))
        high_thr = max(0.55, high_thr)
        low_thr = min(0.45, low_thr)

        # 基准波动水平（用于 Gateway 的 ATR Regime Filter）
        baseline_atr = float(df['atr_14'].mean())

        metadata = {
            "model_type": "xgboost",
            "symbol": symbol,
            "training_period": [
                str(df['ts'].min()),
                str(df['ts'].max()),
            ],
            "feature_names": all_features,
            "baseline_atr_14": baseline_atr,
            "optimized_thresholds": {
                "signal_threshold_high": high_thr,
                "signal_threshold_low": low_thr,
                "regime_max_atr_mult": 4.0,
                "drift_config": {
                    "z_threshold": 10.0,
                    "outlier_ratio_threshold": 0.3,
                },
            },
            "cv_results": {
                "avg_accuracy": float(avg_acc),
                "avg_auc": float(avg_auc),
                "avg_ic": float(avg_ic),
                "avg_ir": float(avg_ir),
            },
        }

        # 将模型包写入 models 目录（按 symbol+窗口命名，避免覆盖其他模型）
        model_dir = Path("models") / f"xgb_h{label_window}_{symbol.lower()}_institutional"
        model_dir.mkdir(parents=True, exist_ok=True)

        final_model.save_model(str(model_dir / "model.json"))
        joblib.dump(scaler, model_dir / "scaler.pkl")
        with open(model_dir / "features.json", "w") as f:
            json.dump({"feature_cols": all_features}, f, indent=2)
        with open(model_dir / "metadata.json", "w") as f:
            json.dump(metadata, f, indent=2)

        print()
        print(f"✅ 模型包构建完成: {model_dir}")
        print("   - model.json")
        print("   - scaler.pkl")
        print("   - features.json")
        print("   - metadata.json")
        return True
    else:
        print("⚠️  部分达标但需进一步优化")
        print(f"   Accuracy: {'✅' if avg_acc > min_accuracy else '❌'} {avg_acc:.1%}")
        print(f"   IC: {'✅' if avg_ic > min_ic else '❌'} {avg_ic:.3f}")
        print()
        print("建议下一步:")
        print("   1. Phase 2: 添加微观结构特征（价格冲击、有效价差）")
        print("   2. Phase 2: 实现订单流特征（VPIN）")
        print("   3. 增加历史数据（当前1个月 → 建议6个月）")
        return False

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--symbol', default='TSLA')
    parser.add_argument('--label-window', type=int, default=10)
    parser.add_argument('--min-accuracy', type=float, default=0.55)
    parser.add_argument('--min-ic', type=float, default=0.05)
    parser.add_argument('--commission-bps', type=float, default=3.5)
    parser.add_argument('--slippage-bps', type=float, default=1.5)
    parser.add_argument('--label-threshold-bps', type=float, default=10.0)
    args = parser.parse_args()

    success = train_institutional_grade(
        symbol=args.symbol,
        label_window=args.label_window,
        min_accuracy=args.min_accuracy,
        min_ic=args.min_ic,
        commission_bps=args.commission_bps,
        slippage_bps=args.slippage_bps,
        label_threshold_bps=args.label_threshold_bps
    )
    sys.exit(0 if success else 1)
