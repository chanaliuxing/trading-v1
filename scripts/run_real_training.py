#!/usr/bin/env python3
"""
真实数据训练测试

使用ClickHouse中的真实市场数据，测试完整ML系统：
1. 从ClickHouse加载数据
2. 应用日历感知标签生成
3. XGB特征工厂（叶子嵌入）
4. 训练与评估
"""
import sys
import json
import yaml
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from libs.features.label_utils import compute_label, make_sample_weight, xgb_params_from_policy
from libs.features.xgb_feature_maker import XGBFeatureMaker


def load_data_from_clickhouse(symbols, start_date, end_date, limit=5000):
    """从ClickHouse加载数据"""
    from clickhouse_driver import Client

    try:
        client = Client(host='localhost', port=9000)
    except Exception as e:
        print(f"❌ 无法连接到ClickHouse: {e}")
        return None

    symbols_str = "','".join(symbols)

    query = f"""
    SELECT
        ts,
        symbol,
        open,
        high,
        low,
        close,
        volume,
        -- 添加ATR作为样本权重的基础
        0.0 as atr_14
    FROM trading.features_1m FINAL
    WHERE symbol IN ('{symbols_str}')
      AND ts >= '{start_date}'
      AND ts <= '{end_date}'
      AND close > 0
      AND volume > 0
    ORDER BY ts, symbol
    LIMIT {limit}
    """

    print(f"\n加载数据: {symbols}, {start_date} ~ {end_date}")

    try:
        df = client.query_dataframe(query)

        # 去重（按ts, symbol）
        df = df.drop_duplicates(subset=['ts', 'symbol'], keep='first')

        print(f"✓ 加载成功: {len(df)} 行（去重后）")
        return df
    except Exception as e:
        print(f"❌ 查询失败: {e}")
        return None


def run_training():
    """运行真实训练"""
    print("=" * 70)
    print("真实数据ML训练测试")
    print("=" * 70)

    # 1. 加载配置
    with open("ml_policy.json") as f:
        ml_policy = json.load(f)

    with open("feature_tools.json") as f:
        feature_tools = json.load(f)

    with open("config.yml") as f:
        config_yaml = yaml.safe_load(f)

    print("\n✓ 配置文件加载成功")

    # 2. 加载真实数据（小窗口测试）
    symbols = ["AAPL"]  # 先用一个标的测试
    start_date = "2025-10-17 09:30:00"  # October trading data
    end_date = "2025-11-05 16:00:00"

    df = load_data_from_clickhouse(symbols, start_date, end_date, limit=10000)

    if df is None or len(df) == 0:
        print("❌ 无可用数据，退出")
        return False

    # 确保时区
    if df['ts'].dt.tz is None:
        df['ts'] = pd.to_datetime(df['ts'], utc=True)

    print(f"\n数据概况:")
    print(f"  - 行数: {len(df)}")
    print(f"  - 时间范围: {df['ts'].min()} ~ {df['ts'].max()}")
    print(f"  - 标的: {df['symbol'].unique()}")

    # 3. 添加简单技术指标作为基础特征
    print("\n生成基础特征...")

    for sym, g in df.groupby('symbol'):
        idx = g.index

        # 价格动量
        df.loc[idx, 'ret_1'] = g['close'].pct_change(1)
        df.loc[idx, 'ret_5'] = g['close'].pct_change(5)
        df.loc[idx, 'ret_10'] = g['close'].pct_change(10)

        # 波动率
        df.loc[idx, 'vol_5'] = g['close'].pct_change().rolling(5).std()
        df.loc[idx, 'vol_10'] = g['close'].pct_change().rolling(10).std()

        # 成交量
        df.loc[idx, 'volume_ratio'] = g['volume'] / g['volume'].rolling(20).mean()

        # ATR (简化版)
        high_low = g['high'] - g['low']
        high_close = (g['high'] - g['close'].shift()).abs()
        low_close = (g['low'] - g['close'].shift()).abs()
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        df.loc[idx, 'atr_14'] = tr.rolling(14).mean()

    # 填充NaN（保留更多数据）
    df = df.ffill().fillna(0.0)

    print(f"✓ 特征生成完成: {len(df)} 行")

    # 4. 分割IS/OOS
    split_idx = int(len(df) * 0.7)
    df_is = df.iloc[:split_idx].copy()
    df_oos = df.iloc[split_idx:].copy()

    print(f"\n数据分割:")
    print(f"  - IS: {len(df_is)} 行 ({df_is['ts'].min()} ~ {df_is['ts'].max()})")
    print(f"  - OOS: {len(df_oos)} 行 ({df_oos['ts'].min()} ~ {df_oos['ts'].max()})")

    # 5. 生成标签
    print(f"\n生成标签（{ml_policy['label']['calendar_mode']}模式）...")

    y_is = compute_label(df_is, ml_policy, config_yaml=config_yaml)
    y_oos = compute_label(df_oos, ml_policy, config_yaml=config_yaml)

    # 样本权重
    w_is = make_sample_weight(df_is, ml_policy)

    # 过滤缺失标签
    mask_is = y_is.notna()
    mask_oos = y_oos.notna()

    print(f"✓ 标签生成完成:")
    print(f"  - IS有效: {mask_is.sum()}/{len(mask_is)} ({mask_is.mean():.1%})")
    print(f"  - OOS有效: {mask_oos.sum()}/{len(mask_oos)} ({mask_oos.mean():.1%})")

    if mask_is.sum() < 50:
        print("❌ IS有效样本不足50，退出")
        return False

    # 6. 准备特征
    base_feat_cols = ['ret_1', 'ret_5', 'ret_10', 'vol_5', 'vol_10', 'volume_ratio']

    X_is = df_is.loc[mask_is, base_feat_cols].copy()
    X_oos = df_oos.loc[mask_oos, base_feat_cols].copy()
    y_is = y_is.loc[mask_is]
    y_oos = y_oos.loc[mask_oos]

    if w_is is not None:
        w_is = w_is.loc[mask_is]

    print(f"\n基础特征矩阵:")
    print(f"  - IS: {X_is.shape}")
    print(f"  - OOS: {X_oos.shape}")

    # 7. 应用XGB特征工厂
    xgb_cfg = feature_tools.get("xgb_feature_maker", {})

    if xgb_cfg.get("enabled", False):
        print(f"\n应用XGB特征工厂（hash_dim={xgb_cfg['leaf_embedding']['hash_dim']}）...")

        xfm = XGBFeatureMaker(xgb_cfg, task=ml_policy["task"])

        # 训练
        xfm.fit(X_is, y_is, sample_weight=w_is)
        print(f"✓ XGB训练完成: {xfm.model.n_estimators}棵树")

        # 转换
        X_is_xgb = xfm.transform(X_is)
        X_oos_xgb = xfm.transform(X_oos)

        # 合并特征
        X_is = pd.concat([X_is.reset_index(drop=True), X_is_xgb], axis=1)
        X_oos = pd.concat([X_oos.reset_index(drop=True), X_oos_xgb], axis=1)

        print(f"✓ 特征扩展: {len(base_feat_cols)}维 → {X_is.shape[1]}维")

    # 8. 训练最终模型
    print(f"\n训练最终模型...")

    from xgboost import XGBRegressor, XGBClassifier

    xgb_params = xgb_params_from_policy(ml_policy)
    xgb_params.update({
        'n_estimators': 100,
        'max_depth': 5,
        'learning_rate': 0.05,
        'subsample': 0.8,
        'colsample_bytree': 0.8,
    })

    if ml_policy["task"] == "binary":
        model = XGBClassifier(**xgb_params)
    else:
        model = XGBRegressor(**xgb_params)

    model.fit(X_is, y_is, sample_weight=w_is, verbose=False)

    print(f"✓ 模型训练完成")

    # 9. 评估
    print(f"\n模型评估:")

    # IS预测
    y_is_pred = model.predict(X_is)

    # OOS预测
    y_oos_pred = model.predict(X_oos)

    # 计算指标
    from sklearn.metrics import mean_squared_error, mean_absolute_error, r2_score

    is_mse = mean_squared_error(y_is, y_is_pred)
    is_mae = mean_absolute_error(y_is, y_is_pred)
    is_r2 = r2_score(y_is, y_is_pred)

    print(f"\nIS性能:")
    print(f"  - MSE: {is_mse:.6f}")
    print(f"  - MAE: {is_mae:.6f}")
    print(f"  - R²: {is_r2:.4f}")

    if len(y_oos) > 0:
        oos_mse = mean_squared_error(y_oos, y_oos_pred)
        oos_mae = mean_absolute_error(y_oos, y_oos_pred)
        oos_r2 = r2_score(y_oos, y_oos_pred)

        print(f"\nOOS性能:")
        print(f"  - MSE: {oos_mse:.6f}")
        print(f"  - MAE: {oos_mae:.6f}")
        print(f"  - R²: {oos_r2:.4f}")

        # IC (如果是回归)
        if ml_policy["task"] == "regression":
            from scipy.stats import spearmanr
            ic, _ = spearmanr(y_oos, y_oos_pred)
            print(f"  - Spearman IC: {ic:.4f}")

    # 10. 特征重要性
    print(f"\nTop 10特征重要性:")

    feature_importance = pd.DataFrame({
        'feature': X_is.columns,
        'importance': model.feature_importances_
    }).sort_values('importance', ascending=False)

    for idx, row in feature_importance.head(10).iterrows():
        print(f"  {row['feature'][:40]:40s}: {row['importance']:.4f}")

    print("\n" + "=" * 70)
    print("✅ 真实数据训练测试完成!")
    print("=" * 70)

    return True


if __name__ == "__main__":
    try:
        success = run_training()
        sys.exit(0 if success else 1)
    except Exception as e:
        print(f"\n❌ 训练失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
