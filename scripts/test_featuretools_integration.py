#!/usr/bin/env python3
"""
测试Featuretools集成完整流程

验证点：
1. feature_tools.json加载
2. FeaturetoolsAdapter初始化
3. IS拟合（fit_on_is）
4. OOS转换（transform_any）
5. 特征定义持久化
6. Pipeline模式切换
"""
import sys
import json
import numpy as np
import pandas as pd
from pathlib import Path

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from libs.features.featuretools_adapter import FeaturetoolsAdapter


def test_featuretools_integration():
    print("=" * 70)
    print("测试 Featuretools 集成完整流程")
    print("=" * 70)

    # 1. 检查配置文件
    config_path = Path("feature_tools.json")
    if not config_path.exists():
        print(f"❌ 配置文件不存在: {config_path}")
        return False

    with open(config_path) as f:
        config = json.load(f)

    print(f"\n✓ 配置文件加载成功")
    print(f"  - Pipeline模式: {config.get('pipeline', {}).get('mode', 'unknown')}")
    print(f"  - Featuretools启用: {config.get('featuretools', {}).get('enabled', False)}")

    # 2. 准备测试数据
    print("\n准备测试数据...")
    np.random.seed(42)
    n_samples = 100
    n_symbols = 2

    data = []
    base_ts = pd.Timestamp("2024-01-01 09:30:00", tz="America/New_York")

    for i in range(n_samples):
        for symbol in ["AAPL", "MSFT"]:
            data.append({
                "ts": base_ts + pd.Timedelta(minutes=i),
                "symbol": symbol,
                "open": 100 + np.random.randn() * 2,
                "high": 102 + np.random.randn() * 2,
                "low": 98 + np.random.randn() * 2,
                "close": 100 + np.random.randn() * 2,
                "volume": int(1000 + np.random.randn() * 100),
            })

    df = pd.DataFrame(data)
    df["row_id"] = range(len(df))

    # 分割IS/OOS
    split = int(len(df) * 0.7)
    df_is = df.iloc[:split].copy()
    df_oos = df.iloc[split:].copy()

    print(f"  - IS样本数: {len(df_is)}")
    print(f"  - OOS样本数: {len(df_oos)}")

    # 3. 测试Featuretools适配器
    print("\n测试 FeaturetoolsAdapter...")

    ft_config = config.get("featuretools", {})
    if not ft_config.get("enabled", False):
        print("⚠️  Featuretools未启用，跳过测试")
        print("  可在feature_tools.json中设置 'featuretools.enabled': true")
        return True

    try:
        # 初始化
        adapter = FeaturetoolsAdapter(ft_config)
        print("✓ 适配器初始化成功")

        # 准备基础列
        base_cols = ["open", "high", "low", "close", "volume"]

        # IS拟合
        print("\nIS拟合...")
        adapter.fit_on_is(df_is, base_cols)
        print(f"✓ DFS生成特征定义: {len(adapter.feature_defs_)} 个")

        # IS转换
        ft_is = adapter.transform_any(df_is)
        print(f"✓ IS转换: {ft_is.shape}")

        # OOS转换
        ft_oos = adapter.transform_any(df_oos)
        print(f"✓ OOS转换: {ft_oos.shape}")

        # 验证列一致性
        assert set(ft_is.columns) == set(ft_oos.columns), "IS/OOS列不一致!"
        print(f"✓ IS/OOS列一致性验证通过")

        # 检查特征定义持久化
        save_path = Path(ft_config.get("save_feature_defs", "runs/feature_registry/ft_defs.json"))
        if save_path.exists():
            with open(save_path) as f:
                defs = json.load(f)
            print(f"\n✓ 特征定义已保存: {save_path}")
            print(f"  - 历史记录数: {len(defs)}")
            if defs:
                latest = defs[-1]
                print(f"  - 最新特征数: {latest['n_features']}")
                print(f"  - 配置: max_depth={ft_config.get('max_depth', 2)}, "
                      f"training_window={ft_config.get('training_window', '30D')}")

        # 显示部分特征名
        print(f"\n生成的特征示例（前10个）:")
        for i, name in enumerate(list(ft_is.columns)[:10]):
            print(f"  {i+1}. {name}")

        print("\n" + "=" * 70)
        print("✅ 所有测试通过!")
        print("=" * 70)

        return True

    except ImportError as e:
        print(f"\n❌ 缺少依赖: {e}")
        print("   请安装: pip install featuretools")
        return False

    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_pipeline_modes():
    """测试不同pipeline模式"""
    print("\n" + "=" * 70)
    print("测试 Pipeline 模式")
    print("=" * 70)

    config_path = Path("feature_tools.json")
    if not config_path.exists():
        print(f"❌ 配置文件不存在")
        return

    with open(config_path) as f:
        config = json.load(f)

    modes = config.get("pipeline", {}).get("modes_available", [])
    current_mode = config.get("pipeline", {}).get("mode", "unknown")

    print(f"\n当前模式: {current_mode}")
    print(f"\n可用模式:")
    for mode in modes:
        status = "✓ (当前)" if mode == current_mode else " "
        print(f"  {status} {mode}")

    print("\n模式说明:")
    print("  - xgb_only: 仅XGB特征工具（叶子嵌入、SHAP交互）")
    print("  - dfs_only: 仅DFS自动特征")
    print("  - xgb_then_dfs: XGB特征→DFS（推荐）")
    print("  - dfs_then_xgb: DFS→XGB（实验性）")


if __name__ == "__main__":
    try:
        success = test_featuretools_integration()

        if success:
            test_pipeline_modes()

        sys.exit(0 if success else 1)

    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
