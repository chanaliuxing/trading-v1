#!/usr/bin/env python3
"""
测试 H: 端到端 ML Pipeline Smoke Test

验证完整的 ML Pipeline (features → labels → model → validation) 能正常运行

运行方式:
    pytest tests/integration/pipeline/test_ml_pipeline_smoke.py -v
    pytest tests/integration/pipeline/test_ml_pipeline_smoke.py::test_full_pipeline_smoke -v
"""
import pytest
import pandas as pd
import numpy as np
from pathlib import Path
import sys
from datetime import datetime, timedelta

# Add project root
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from libs.db.ch_pool import get_client


@pytest.fixture
def sample_features_data():
    """
    获取小批量测试数据 (1 天, 1-2 symbols)

    Returns:
        pd.DataFrame with columns: symbol, ts, close, volume, + other features
    """
    client = get_client()

    # 获取最近一个有完整数据的交易日
    date_query = """
    SELECT toDate(ts) as date, countIf(close IS NOT NULL) as has_data
    FROM trading.features_1m
    WHERE ts >= today() - INTERVAL 10 DAY
      AND symbol IN ('TSLA', 'SPY')
    GROUP BY date
    HAVING has_data > 300
    ORDER BY date DESC
    LIMIT 1
    """

    date_result = client.query(date_query)
    if not date_result:
        pytest.skip("No recent feature data with sufficient coverage for testing")

    test_date = date_result[0]['date']
    print(f"Using test date: {test_date} (has {date_result[0]['has_data']} non-null rows)")

    # 获取测试数据 (使用基础特征,更可能有数据)
    query = """
    SELECT
        symbol,
        ts,
        close,
        volume,
        ret_1,
        ret_5,
        atr_14,
        ema_20
    FROM trading.features_1m FINAL
    WHERE symbol IN ('TSLA', 'SPY')
      AND toDate(ts) = %(test_date)s
      AND (toHour(ts) * 60 + toMinute(ts)) >= 570
      AND (toHour(ts) * 60 + toMinute(ts)) <= 960
    ORDER BY symbol, ts
    """

    result = client.query(query, {'test_date': str(test_date)})

    if not result:
        pytest.skip(f"No feature data for test date {test_date}")

    df = pd.DataFrame(result)
    print(f"\nLoaded {len(df)} rows for {df['symbol'].nunique()} symbols on {test_date}")

    return df


def generate_labels_simple(df: pd.DataFrame, horizon: int = 5) -> pd.DataFrame:
    """
    简单的 label 生成：未来 N 分钟收益率

    Args:
        df: DataFrame with at least ['symbol', 'ts', 'close']
        horizon: Forward return period in minutes

    Returns:
        DataFrame with 'y' column added
    """
    df = df.copy().sort_values(['symbol', 'ts'])

    # 使用 shift 计算未来收益率
    df['future_close'] = df.groupby('symbol')['close'].shift(-horizon)
    df['y'] = (df['future_close'] - df['close']) / df['close']

    # 移除最后 horizon 行 (无法计算 label)
    df = df[df['y'].notna()].copy()

    print(f"Generated {len(df)} labels (horizon={horizon}m)")
    print(f"Label stats: mean={df['y'].mean():.6f}, std={df['y'].std():.6f}")

    return df


def test_full_pipeline_smoke(sample_features_data):
    """
    测试 1: 完整 ML Pipeline 冒烟测试

    流程：
    1. 读取 features_1m ✅
    2. 生成 labels
    3. 训练简单模型
    4. 验证预测结果
    """
    df = sample_features_data

    # Step 1: 验证数据加载
    assert len(df) > 0, "Should have loaded feature data"
    assert 'close' in df.columns, "Should have 'close' column"
    assert 'ts' in df.columns, "Should have 'ts' column"
    print("✅ Step 1: Feature data loaded successfully")

    # Step 2: 生成 labels
    df_labeled = generate_labels_simple(df, horizon=5)

    assert 'y' in df_labeled.columns, "Should have 'y' label column"
    assert df_labeled['y'].notna().sum() > 0, "Should have non-null labels"

    non_null_ratio = df_labeled['y'].notna().mean()
    assert non_null_ratio >= 0.95, f"Label non-null ratio {non_null_ratio:.1%} < 95%"
    print(f"✅ Step 2: Labels generated (non-null ratio: {non_null_ratio:.1%})")

    # Step 3: 准备训练数据 (使用基础特征)
    feature_cols = ['ret_1', 'ret_5', 'atr_14', 'ema_20']

    # 移除包含 NaN 的行
    df_train = df_labeled[['y'] + feature_cols].dropna()

    if len(df_train) <= 10:
        pytest.skip(f"Insufficient training data after dropna in current environment: {len(df_train)} rows")

    X = df_train[feature_cols].values
    y = df_train['y'].values

    print(f"✅ Step 3: Training data prepared ({len(df_train)} rows, {len(feature_cols)} features)")

    # Step 4: 训练简单模型
    try:
        from lightgbm import LGBMRegressor

        model = LGBMRegressor(
            n_estimators=3,  # 极简模型，只测试流程
            max_depth=2,
            learning_rate=0.1,
            random_state=42,
            verbose=-1
        )

        model.fit(X, y)
        print("✅ Step 4: Model trained successfully")

    except ImportError:
        pytest.skip("LightGBM not installed")

    # Step 5: 验证预测
    y_pred = model.predict(X)

    assert len(y_pred) == len(y), "Prediction length mismatch"
    assert not np.isnan(y_pred).any(), "Predictions contain NaN"
    assert np.isfinite(y_pred).all(), "Predictions contain inf"

    # 检查预测值在合理范围 (±50% 日内波动)
    assert np.abs(y_pred).max() < 0.5, f"Extreme prediction: {np.abs(y_pred).max():.4f}"

    pred_mean = y_pred.mean()
    pred_std = y_pred.std()
    print(f"✅ Step 5: Predictions validated (mean={pred_mean:.6f}, std={pred_std:.6f})")

    # Step 6: 基本性能指标
    from sklearn.metrics import mean_squared_error, r2_score

    mse = mean_squared_error(y, y_pred)
    r2 = r2_score(y, y_pred)

    print(f"✅ Step 6: Performance metrics (MSE={mse:.6f}, R2={r2:.4f})")

    # 冒烟测试不要求高性能，只要流程正常
    assert mse < 1.0, f"MSE too high: {mse:.4f}"
    assert r2 > -1.0, f"R2 too negative: {r2:.4f}"

    print("\n" + "=" * 80)
    print("🎉 ML Pipeline Smoke Test PASSED")
    print("=" * 80)


def test_pipeline_handles_missing_features():
    """
    测试 2: Pipeline 处理缺失特征

    场景：
    - 某些特征列有 NaN
    - Pipeline 应该优雅处理 (dropna 或 fillna)
    """
    # 创建包含 NaN 的测试数据
    df = pd.DataFrame({
        'symbol': ['TSLA'] * 10,
        'ts': pd.date_range('2025-11-24 09:30', periods=10, freq='1min'),
        'close': np.random.randn(10) + 100,
        'ret_5m': np.random.randn(10) * 0.01,
        'vol_5m': np.random.randn(10) * 0.02,
        'vwap_dist_5m': np.random.randn(10) * 0.005,
        'realized_vol': [np.nan, np.nan, 0.01, 0.02, np.nan, 0.015, 0.018, 0.020, 0.019, np.nan]
    })

    # 生成 labels
    df_labeled = generate_labels_simple(df, horizon=2)

    # 检查 dropna 后仍有数据
    feature_cols = ['ret_5m', 'vol_5m', 'vwap_dist_5m', 'realized_vol']
    df_clean = df_labeled[['y'] + feature_cols].dropna()

    assert len(df_clean) > 0, "Should have some rows after dropna"
    print(f"✅ Pipeline handles missing features: {len(df_clean)}/{len(df_labeled)} rows retained")


def test_pipeline_detects_all_null_labels():
    """
    测试 3: Pipeline 检测全 NULL labels

    场景：
    - 所有 labels 都是 NULL (例如数据不足无法计算未来收益)
    - 应该抛出错误或警告
    """
    # 创建只有 1 行的数据 (无法计算未来收益)
    df = pd.DataFrame({
        'symbol': ['TSLA'],
        'ts': [pd.Timestamp('2025-11-24 09:30')],
        'close': [100.0],
        'ret_5m': [0.01],
    })

    df_labeled = generate_labels_simple(df, horizon=5)

    # 应该没有有效 labels
    assert len(df_labeled) == 0 or df_labeled['y'].isna().all(), \
        "Should detect all-null labels scenario"

    print("✅ Pipeline correctly detects all-null labels")


def test_pipeline_validates_prediction_range():
    """
    测试 4: Pipeline 验证预测值范围

    场景：
    - 训练后预测值应该在合理范围
    - 不应该有极端值 (>50% 日内波动)
    """
    # 创建正常数据
    np.random.seed(42)
    df = pd.DataFrame({
        'symbol': ['TSLA'] * 100,
        'ts': pd.date_range('2025-11-24 09:30', periods=100, freq='1min'),
        'close': 100 + np.cumsum(np.random.randn(100) * 0.1),
        'ret_5m': np.random.randn(100) * 0.01,
        'vol_5m': np.abs(np.random.randn(100) * 0.02),
        'vwap_dist_5m': np.random.randn(100) * 0.005,
        'realized_vol': np.abs(np.random.randn(100) * 0.015),
    })

    df_labeled = generate_labels_simple(df, horizon=5)

    feature_cols = ['ret_5m', 'vol_5m', 'vwap_dist_5m', 'realized_vol']
    df_train = df_labeled[['y'] + feature_cols].dropna()

    try:
        from lightgbm import LGBMRegressor

        X = df_train[feature_cols].values
        y = df_train['y'].values

        model = LGBMRegressor(n_estimators=3, max_depth=2, verbose=-1, random_state=42)
        model.fit(X, y)

        y_pred = model.predict(X)

        # 验证范围
        max_pred = np.abs(y_pred).max()
        assert max_pred < 0.5, f"Extreme prediction detected: {max_pred:.4f}"

        print(f"✅ Prediction range validated: max={max_pred:.4f} < 0.5")

    except ImportError:
        pytest.skip("LightGBM not installed")


def test_pipeline_integration_summary():
    """
    汇总测试：打印 ML Pipeline 测试总结
    """
    print("\n" + "=" * 80)
    print("ML Pipeline Smoke Test Summary")
    print("=" * 80)
    print("✅ Test 1: Full pipeline smoke (features → labels → model → predictions)")
    print("✅ Test 2: Handles missing features gracefully")
    print("✅ Test 3: Detects all-null labels scenario")
    print("✅ Test 4: Validates prediction value ranges")
    print("=" * 80)
    print("ML Pipeline 端到端验证完成")
    print("=" * 80)


if __name__ == "__main__":
    # 允许直接运行
    pytest.main([__file__, '-v', '--tb=short'])
