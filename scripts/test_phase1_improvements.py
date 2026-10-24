#!/usr/bin/env python3
"""
测试Phase 1改进效果

对比标准化前后的模型质量：
- IC (Information Coefficient)
- AUC (Area Under Curve)
- Accuracy
- 特征重要性分布
"""

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pandas as pd
import numpy as np
from clickhouse_driver import Client
from sklearn.preprocessing import RobustScaler
from sklearn.model_selection import TimeSeriesSplit
import xgboost as xgb
from scipy.stats import spearmanr
from sklearn.metrics import roc_auc_score, accuracy_score

def fetch_data(symbol="TSLA", start="2025-10-01", end="2025-11-08"):
    """从ClickHouse获取数据"""
    client = Client(host='localhost', port=9000)

    query = f"""
    SELECT
        ts,
        symbol,
        close,
        vwap,
        volume,
        ret_1,
        ret_5,
        atr_5,
        atr_14,
        rsi_14,
        bb_mid_20
    FROM trading.features_1m
    WHERE symbol = '{symbol}'
      AND ts >= toDateTime('{start}')
      AND ts <= toDateTime('{end}')
    ORDER BY ts
    """

    rows = client.execute(query)
    df = pd.DataFrame(rows, columns=[
        "ts", "symbol", "close", "vwap", "volume",
        "ret_1", "ret_5", "atr_5", "atr_14", "rsi_14", "bb_mid_20"
    ])

    # Rename to match expected names
    df = df.rename(columns={
        "ret_1": "ret_1m",
        "ret_5": "ret_5m"
    })

    return df

def create_features(df):
    """创建派生特征"""
    df = df.copy()

    # Phase 1 派生特征
    if "close" in df.columns and "vwap" in df.columns:
        df["feat_vwap_dev"] = (df["close"] / df["vwap"]) - 1.0

    if "ret_1m" in df.columns and "atr_14" in df.columns:
        df["feat_ret1m_over_atr"] = df["ret_1m"] / (df["atr_14"] + 1e-6)

    if "volume" in df.columns:
        df["feat_log_volume"] = np.log1p(df["volume"].astype(float))

    if "ret_1m" in df.columns:
        df["feat_ret_1m_roll_10"] = (
            df["ret_1m"]
            .rolling(window=10, min_periods=3)
            .sum()
        )

    return df

def create_labels(df, horizon=20):
    """创建标签（回归任务：预测未来收益）"""
    df = df.copy()
    df = df.sort_values("ts").reset_index(drop=True)

    # 未来20分钟收益
    df["y"] = (df["close"].shift(-horizon) / df["close"]) - 1.0

    return df

def test_baseline(df, feature_cols):
    """测试未标准化模型（Baseline）"""
    print("\n" + "="*80)
    print("测试 Baseline（无标准化）")
    print("="*80)

    # 准备数据
    X = df[feature_cols].fillna(method="ffill").fillna(method="bfill").fillna(0.0)
    y = df["y"]

    # 过滤缺失标签
    mask = y.notna()
    X = X[mask]
    y = y[mask]

    if len(X) == 0:
        print("❌ 没有有效样本")
        return None

    # Walk-Forward Split
    tscv = TimeSeriesSplit(n_splits=3)

    results = []
    for fold_idx, (train_idx, test_idx) in enumerate(tscv.split(X)):
        X_train, X_test = X.iloc[train_idx], X.iloc[test_idx]
        y_train, y_test = y.iloc[train_idx], y.iloc[test_idx]

        # 训练XGBoost
        model = xgb.XGBRegressor(
            n_estimators=200,
            max_depth=5,
            learning_rate=0.1,
            subsample=0.8,
            colsample_bytree=0.8,
            random_state=42
        )
        model.fit(X_train, y_train, verbose=False)

        # 预测
        y_pred = model.predict(X_test)

        # 评估
        ic, _ = spearmanr(y_test, y_pred)

        # 分类指标（转换为涨跌）
        y_test_clf = (y_test > 0).astype(int)
        y_pred_clf = (y_pred > 0).astype(int)

        acc = accuracy_score(y_test_clf, y_pred_clf)
        try:
            auc = roc_auc_score(y_test_clf, y_pred)
        except:
            auc = 0.5

        results.append({
            "fold": fold_idx + 1,
            "ic": ic,
            "auc": auc,
            "acc": acc
        })

        print(f"Fold {fold_idx + 1}: IC={ic:.4f}, AUC={auc:.4f}, Acc={acc:.4f}")

    # 平均结果
    avg_ic = np.mean([r["ic"] for r in results])
    avg_auc = np.mean([r["auc"] for r in results])
    avg_acc = np.mean([r["acc"] for r in results])

    print(f"\n平均结果: IC={avg_ic:.4f}, AUC={avg_auc:.4f}, Acc={avg_acc:.4f}")

    # 特征重要性
    feature_importance = pd.DataFrame({
        "feature": feature_cols,
        "importance": model.feature_importances_
    }).sort_values("importance", ascending=False)

    print("\n特征重要性（Top 5）:")
    print(feature_importance.head())

    return {
        "avg_ic": avg_ic,
        "avg_auc": avg_auc,
        "avg_acc": avg_acc,
        "feature_importance": feature_importance
    }

def test_standardized(df, feature_cols):
    """测试标准化模型（Phase 1）"""
    print("\n" + "="*80)
    print("测试 Phase 1（RobustScaler标准化）")
    print("="*80)

    # 准备数据
    X = df[feature_cols].fillna(method="ffill").fillna(method="bfill").fillna(0.0)
    y = df["y"]

    # 过滤缺失标签
    mask = y.notna()
    X = X[mask]
    y = y[mask]

    if len(X) == 0:
        print("❌ 没有有效样本")
        return None

    # Walk-Forward Split
    tscv = TimeSeriesSplit(n_splits=3)

    results = []
    for fold_idx, (train_idx, test_idx) in enumerate(tscv.split(X)):
        X_train, X_test = X.iloc[train_idx], X.iloc[test_idx]
        y_train, y_test = y.iloc[train_idx], y.iloc[test_idx]

        # ========== Phase 1: 标准化 ==========
        scaler = RobustScaler()
        X_train_scaled = pd.DataFrame(
            scaler.fit_transform(X_train),
            columns=X_train.columns,
            index=X_train.index
        )
        X_test_scaled = pd.DataFrame(
            scaler.transform(X_test),
            columns=X_test.columns,
            index=X_test.index
        )

        # Clip [-5, 5]
        X_train_scaled = X_train_scaled.clip(-5.0, 5.0)
        X_test_scaled = X_test_scaled.clip(-5.0, 5.0)

        # 训练XGBoost
        model = xgb.XGBRegressor(
            n_estimators=200,
            max_depth=5,
            learning_rate=0.1,
            subsample=0.8,
            colsample_bytree=0.8,
            random_state=42
        )
        model.fit(X_train_scaled, y_train, verbose=False)

        # 预测
        y_pred = model.predict(X_test_scaled)

        # 评估
        ic, _ = spearmanr(y_test, y_pred)

        # 分类指标
        y_test_clf = (y_test > 0).astype(int)
        y_pred_clf = (y_pred > 0).astype(int)

        acc = accuracy_score(y_test_clf, y_pred_clf)
        try:
            auc = roc_auc_score(y_test_clf, y_pred)
        except:
            auc = 0.5

        results.append({
            "fold": fold_idx + 1,
            "ic": ic,
            "auc": auc,
            "acc": acc
        })

        print(f"Fold {fold_idx + 1}: IC={ic:.4f}, AUC={auc:.4f}, Acc={acc:.4f}")

    # 平均结果
    avg_ic = np.mean([r["ic"] for r in results])
    avg_auc = np.mean([r["auc"] for r in results])
    avg_acc = np.mean([r["acc"] for r in results])

    print(f"\n平均结果: IC={avg_ic:.4f}, AUC={avg_auc:.4f}, Acc={avg_acc:.4f}")

    # 特征重要性
    feature_importance = pd.DataFrame({
        "feature": feature_cols,
        "importance": model.feature_importances_
    }).sort_values("importance", ascending=False)

    print("\n特征重要性（Top 5）:")
    print(feature_importance.head())

    return {
        "avg_ic": avg_ic,
        "avg_auc": avg_auc,
        "avg_acc": avg_acc,
        "feature_importance": feature_importance
    }

def main():
    print("="*80)
    print("Phase 1 特征标准化效果验证")
    print("="*80)

    # 1. 获取数据
    print("\n📊 获取数据...")
    df = fetch_data(symbol="TSLA", start="2025-10-01", end="2025-11-08")
    print(f"原始数据: {len(df)} 行")

    # 2. 创建派生特征
    print("\n🛠️ 创建派生特征...")
    df = create_features(df)

    # 3. 创建标签
    print("\n🏷️ 创建标签（horizon=20分钟）...")
    df = create_labels(df, horizon=20)

    # 去除缺失
    df = df.replace([np.inf, -np.inf], np.nan)
    df = df.dropna()
    print(f"清理后数据: {len(df)} 行")

    if len(df) < 100:
        print("❌ 数据量不足100行，无法测试")
        return

    # 4. 定义特征组合
    feature_cols = [
        "ret_1m", "ret_5m", "vwap", "atr_14", "rsi_14", "volume",
        "feat_vwap_dev", "feat_ret1m_over_atr", "feat_log_volume", "feat_ret_1m_roll_10"
    ]

    # 确保所有特征存在
    feature_cols = [f for f in feature_cols if f in df.columns]
    print(f"\n使用特征: {len(feature_cols)}个")
    print(feature_cols)

    # 5. 测试Baseline（无标准化）
    baseline_results = test_baseline(df, feature_cols)

    # 6. 测试Phase 1（标准化）
    phase1_results = test_standardized(df, feature_cols)

    # 7. 对比结果
    if baseline_results and phase1_results:
        print("\n" + "="*80)
        print("📊 对比结果")
        print("="*80)

        print(f"\n| 指标 | Baseline | Phase 1 | 改进 |")
        print(f"|------|----------|---------|------|")

        ic_improvement = (phase1_results["avg_ic"] - baseline_results["avg_ic"]) / max(abs(baseline_results["avg_ic"]), 0.001) * 100
        auc_improvement = (phase1_results["avg_auc"] - baseline_results["avg_auc"]) / baseline_results["avg_auc"] * 100
        acc_improvement = (phase1_results["avg_acc"] - baseline_results["avg_acc"]) / baseline_results["avg_acc"] * 100

        print(f"| IC   | {baseline_results['avg_ic']:.4f} | {phase1_results['avg_ic']:.4f} | {ic_improvement:+.1f}% |")
        print(f"| AUC  | {baseline_results['avg_auc']:.4f} | {phase1_results['avg_auc']:.4f} | {auc_improvement:+.1f}% |")
        print(f"| Acc  | {baseline_results['avg_acc']:.4f} | {phase1_results['avg_acc']:.4f} | {acc_improvement:+.1f}% |")

        # 判断是否达标
        print("\n" + "="*80)
        print("✅ 达标检查")
        print("="*80)

        target_ic = 0.08
        target_auc = 0.65

        if phase1_results["avg_ic"] >= target_ic:
            print(f"✅ IC >= {target_ic}: {phase1_results['avg_ic']:.4f}")
        else:
            print(f"⚠️  IC < {target_ic}: {phase1_results['avg_ic']:.4f}")

        if phase1_results["avg_auc"] >= target_auc:
            print(f"✅ AUC >= {target_auc}: {phase1_results['avg_auc']:.4f}")
        else:
            print(f"⚠️  AUC < {target_auc}: {phase1_results['avg_auc']:.4f}")

if __name__ == "__main__":
    main()
