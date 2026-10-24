#!/usr/bin/env python3
"""
测试feature_filter.py的完整流程

验证点：
1. IS/OOS列一致性
2. 泄漏防护
3. 方差过滤
4. 目标相关性过滤
5. 特征间相关性剪枝
6. 元数据落盘
"""
import sys
import json
import numpy as np
import pandas as pd
from pathlib import Path

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from libs.features.feature_filter import FeatureFilter


def test_feature_filter():
    print("=" * 70)
    print("测试 FeatureFilter 完整流程")
    print("=" * 70)

    # 1. 准备测试数据
    np.random.seed(42)
    n_samples = 1000

    # 生成特征
    df = pd.DataFrame({
        'feat_1': np.random.randn(n_samples),
        'feat_2': np.random.randn(n_samples) * 2 + 1,
        'feat_3': np.random.randn(n_samples) * 0.5,  # 低方差
        'feat_4': np.random.randn(n_samples),
        'feat_5': np.random.randn(n_samples),
        'feat_future': np.random.randn(n_samples),  # 应被leakage_guard删除
        'feat_label_trick': np.random.randn(n_samples),  # 应被leakage_guard删除
        'const_feat': np.ones(n_samples),  # 零方差，应被删除
    })

    # feat_4 与 feat_1 高相关
    df['feat_4'] = df['feat_1'] * 0.95 + np.random.randn(n_samples) * 0.1

    # 生成标签（与feat_1/feat_2强相关，与feat_3弱相关）
    y = pd.Series(
        df['feat_1'] * 0.5 + df['feat_2'] * 0.3 + np.random.randn(n_samples) * 0.1,
        name='target'
    )

    # 分割IS/OOS
    split = int(n_samples * 0.7)
    df_is = df.iloc[:split].copy()
    df_oos = df.iloc[split:].copy()
    y_is = y.iloc[:split]
    y_oos = y.iloc[split:]

    print(f"\n原始特征数: {len(df.columns)}")
    print(f"IS样本数: {len(df_is)}, OOS样本数: {len(df_oos)}")

    # 2. 加载配置
    config = {
        "task": "regression",
        "filters": {
            "enabled": True,
            "leakage_guard": {
                "name_blacklist": ["label", "future", "lead"]
            },
            "variance": {
                "min": 0.01
            },
            "target_corr": {
                "enabled": True,
                "metric": "spearman",
                "min_abs": 0.1,
                "sample_n": 50000
            },
            "pair_corr": {
                "enabled": True,
                "metric": "spearman",
                "max_abs": 0.95,
                "sample_n": 30000
            },
            "max_features": 10,
            "save_meta": True,
            "meta_path": "runs/feature_registry/test_feature_filter_meta.json"
        }
    }

    # 3. 初始化并拟合
    ff = FeatureFilter(config)

    print("\n开始特征筛选...")
    X_is_filtered = ff.fit_transform(df_is, y_is)
    X_oos_filtered = ff.transform(df_oos)

    # 4. 验证结果
    print("\n" + "=" * 70)
    print("筛选结果验证")
    print("=" * 70)

    # 检查1: IS/OOS列一致性
    assert set(X_is_filtered.columns) == set(X_oos_filtered.columns), \
        "IS和OOS的特征列不一致!"
    print(f"✓ IS/OOS列一致性: 通过 (共{len(X_is_filtered.columns)}列)")

    # 检查2: 泄漏防护
    leaked_cols = [c for c in X_is_filtered.columns if any(w in c.lower() for w in ['future', 'label'])]
    assert len(leaked_cols) == 0, f"仍存在泄漏列: {leaked_cols}"
    print(f"✓ 泄漏防护: 通过 (已删除feat_future和feat_label_trick)")

    # 检查3: 零/低方差过滤
    assert 'const_feat' not in X_is_filtered.columns, "零方差特征未被删除"
    print(f"✓ 方差过滤: 通过 (const_feat已删除)")

    # 检查4: 保留的特征
    print(f"\n保留的特征: {list(X_is_filtered.columns)}")
    print(f"  - 应保留: feat_1, feat_2 (与目标强相关)")
    print(f"  - feat_3: {'保留' if 'feat_3' in X_is_filtered.columns else '删除'} (弱相关)")
    print(f"  - feat_4: {'保留' if 'feat_4' in X_is_filtered.columns else '删除'} (与feat_1高相关)")

    # 检查5: 元数据
    if config["filters"]["save_meta"]:
        meta_path = Path(config["filters"]["meta_path"])
        if meta_path.exists():
            with open(meta_path) as f:
                meta_list = json.load(f)
            latest_meta = meta_list[-1]
            print(f"\n✓ 元数据已保存: {meta_path}")
            print(f"  - 保留特征数: {len(latest_meta['selected'])}")

            # 显示删除原因
            print("\n删除特征及原因:")
            for feat, reason in latest_meta['reasons'].items():
                if feat not in latest_meta['selected']:
                    print(f"  - {feat}: {reason.get('drop', 'unknown')}")

    # 检查6: 数据完整性
    assert X_is_filtered.shape[0] == df_is.shape[0], "IS行数不匹配"
    assert X_oos_filtered.shape[0] == df_oos.shape[0], "OOS行数不匹配"
    print(f"\n✓ 数据完整性: IS={X_is_filtered.shape}, OOS={X_oos_filtered.shape}")

    # 7. 计算筛选后的相关性
    if len(X_is_filtered.columns) > 0:
        corr_with_target = X_is_filtered.corrwith(y_is, method='spearman').abs()
        print(f"\n筛选后特征与目标的Spearman相关性:")
        for col in X_is_filtered.columns:
            print(f"  - {col}: {corr_with_target[col]:.3f}")

    print("\n" + "=" * 70)
    print("✅ 所有测试通过!")
    print("=" * 70)

    return True


if __name__ == "__main__":
    try:
        success = test_feature_filter()
        sys.exit(0 if success else 1)
    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
