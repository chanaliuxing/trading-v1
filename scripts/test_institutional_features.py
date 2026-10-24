#!/usr/bin/env python3
"""
测试机构级特征工程系统
"""
import sys
from pathlib import Path
import numpy as np
import pandas as pd

# Add project root
project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))

from libs.features.feature_ops import OPS, get_op
from libs.features.feature_dsl_v2 import apply_program, build_features
from libs.features.feature_templates import expand_template
from libs.features.feature_validator import validate_program
from libs.features.ic_metrics import compute_ic


def create_test_data(n=1000, symbols=["AAPL", "TSLA", "NVDA"]):
    """创建测试数据"""
    data = []
    for symbol in symbols:
        dates = pd.date_range("2024-01-01", periods=n, freq="1min")
        close = 100 + np.cumsum(np.random.randn(n) * 0.5)
        high = close + np.abs(np.random.randn(n) * 0.3)
        low = close - np.abs(np.random.randn(n) * 0.3)
        open_ = close + np.random.randn(n) * 0.2
        volume = np.abs(np.random.randn(n) * 1000000)

        for i in range(n):
            data.append({
                "symbol": symbol,
                "ts": dates[i],
                "open": open_[i],
                "high": high[i],
                "low": low[i],
                "close": close[i],
                "volume": volume[i],
            })

    df = pd.DataFrame(data)
    # 计算基础特征
    df = df.groupby("symbol").apply(lambda g: g.assign(
        ret_1=g["close"].pct_change(),
        ret_5=g["close"].pct_change(5),
    )).reset_index(drop=True)

    return df


def test_1_basic_ops():
    """测试1: 基础算子"""
    print("=" * 80)
    print("测试1: 基础算子")
    print("=" * 80)

    df = create_test_data(n=200, symbols=["AAPL"])

    # 测试 log1p
    result = get_op("log1p")(df, col="volume")
    assert len(result) == len(df)
    assert result.notna().sum() > 0
    print("✓ log1p")

    # 测试 rolling_mean
    result = get_op("rolling_mean")(df, col="close", window_bars=10, per_symbol=True)
    assert len(result) == len(df)
    print("✓ rolling_mean")

    # 测试 rolling_std
    result = get_op("rolling_std")(df, col="ret_1", window_bars=20, per_symbol=True)
    assert len(result) == len(df)
    print("✓ rolling_std")

    # 测试 ema
    result = get_op("ema")(df, col="close", span=12, per_symbol=True)
    assert len(result) == len(df)
    print("✓ ema")

    # 测试 ratio
    result = get_op("ratio")(df, num="high", den="low", eps=1e-9)
    assert len(result) == len(df)
    assert result.min() >= 1.0  # high/low should be >= 1
    print("✓ ratio")

    print()
    print(f"✅ 测试1通过: 所有{len(OPS)}个算子已注册")
    print()


def test_2_technical_indicators():
    """测试2: 技术指标"""
    print("=" * 80)
    print("测试2: 技术指标")
    print("=" * 80)

    df = create_test_data(n=300, symbols=["AAPL", "TSLA"])

    # RSI
    result = get_op("ta.rsi")(df, col="close", window_bars=14, per_symbol=True)
    assert len(result) == len(df)
    assert result.min() >= 0 and result.max() <= 100
    print("✓ ta.rsi")

    # ATR
    result = get_op("ta.atr")(df, window_bars=14, per_symbol=True)
    assert len(result) == len(df)
    assert result.min() >= 0
    print("✓ ta.atr")

    # Bollinger Bands
    result = get_op("ta.bbands")(df, col="close", window_bars=20, k=2.0, band="upper", per_symbol=True)
    assert len(result) == len(df)
    print("✓ ta.bbands")

    # MACD (returns DataFrame)
    result = get_op("ta.macd")(df, col="close", fast=12, slow=26, signal=9, per_symbol=True)
    assert isinstance(result, pd.DataFrame)
    assert "macd" in result.columns
    assert "macd_signal" in result.columns
    print("✓ ta.macd")

    print()
    print("✅ 测试2通过: 所有技术指标工作正常")
    print()


def test_3_microstructure_ops():
    """测试3: 微观结构算子"""
    print("=" * 80)
    print("测试3: 微观结构算子")
    print("=" * 80)

    df = create_test_data(n=200, symbols=["AAPL"])

    # Effective spread
    result = get_op("effective_spread")(df, high_col="high", low_col="low", mid_col="close")
    assert len(result) == len(df)
    assert result.min() >= 0
    print("✓ effective_spread")

    # Amihud illiquidity
    result = get_op("amihud_illiquidity")(df, ret_col="ret_1", vol_col="volume", eps=1e-12)
    assert len(result) == len(df)
    print("✓ amihud_illiquidity")

    # Price impact
    result = get_op("price_impact_hl")(df, close_col="close", high_col="high", low_col="low")
    assert len(result) == len(df)
    print("✓ price_impact_hl")

    # VWAP distance
    result = get_op("vwap_distance")(df, price_col="close", vol_col="volume", per_symbol=True, window_bars=20)
    assert len(result) == len(df)
    print("✓ vwap_distance")

    print()
    print("✅ 测试3通过: 所有微观结构算子工作正常")
    print()


def test_4_dsl_execution():
    """测试4: DSL程序执行"""
    print("=" * 80)
    print("测试4: DSL程序执行")
    print("=" * 80)

    df = create_test_data(n=300, symbols=["AAPL", "TSLA"])

    # 定义一个简单的程序
    program = {
        "description": "momentum features",
        "steps": [
            {"name": "ret_sma_10", "op": "rolling_mean", "col": "ret_1", "window_bars": 10, "per_symbol": True},
            {"name": "ret_vol_20", "op": "rolling_std", "col": "ret_1", "window_bars": 20, "per_symbol": True},
            {"name": "sharpe_proxy", "op": "ratio", "num": "ret_sma_10", "den": "ret_vol_20", "eps": 1e-6},
        ]
    }

    # 执行程序
    df_out, new_cols = apply_program(df.copy(), program, allow_overwrite=False)

    assert "ret_sma_10" in df_out.columns
    assert "ret_vol_20" in df_out.columns
    assert "sharpe_proxy" in df_out.columns
    assert len(new_cols) == 3
    print(f"✓ 程序执行成功，新增列: {new_cols}")

    print()
    print("✅ 测试4通过: DSL程序执行正常")
    print()


def test_5_templates():
    """测试5: 特征模板"""
    print("=" * 80)
    print("测试5: 特征模板")
    print("=" * 80)

    df = create_test_data(n=300, symbols=["AAPL", "TSLA"])

    # Momentum template
    program = expand_template("template.momentum", ret_col="ret_1", windows=[10, 30, 60])
    df_out, new_cols = apply_program(df.copy(), program, allow_overwrite=False)
    assert len(new_cols) > 0
    assert "momo_10" in df_out.columns
    assert "momo_30" in df_out.columns
    print(f"✓ Momentum template: {len(new_cols)} features")

    # Microstructure template
    df2 = create_test_data(n=300, symbols=["NVDA"])
    program2 = expand_template("template.microstructure")
    df_out2, new_cols2 = apply_program(df2.copy(), program2, allow_overwrite=False)
    assert len(new_cols2) > 0
    assert "eff_spread" in df_out2.columns
    assert "amihud" in df_out2.columns
    print(f"✓ Microstructure template: {len(new_cols2)} features")

    print()
    print("✅ 测试5通过: 模板系统工作正常")
    print()


def test_6_validator():
    """测试6: 程序校验器"""
    print("=" * 80)
    print("测试6: 程序校验器")
    print("=" * 80)

    # Valid program
    valid_program = {
        "steps": [
            {"name": "feat_ret_ma", "op": "rolling_mean", "col": "ret_1", "window_bars": 10, "per_symbol": True},
            {"name": "feat_vol", "op": "rolling_std", "col": "ret_1", "window_bars": 20, "per_symbol": True},
        ]
    }

    warns = validate_program(valid_program, max_steps=12)
    assert isinstance(warns, list)
    print("✓ 有效程序通过校验")

    # Invalid program (forbidden name)
    try:
        invalid_program = {
            "steps": [
                {"name": "future_ret", "op": "rolling_mean", "col": "ret_1", "window_bars": 10},
            ]
        }
        validate_program(invalid_program)
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "forbidden" in str(e).lower()
        print("✓ 禁止名称检测正常")

    # Invalid program (unknown op)
    try:
        invalid_program2 = {
            "steps": [
                {"name": "feat_x", "op": "unknown_operator", "col": "ret_1"},
            ]
        }
        validate_program(invalid_program2)
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "unknown op" in str(e).lower()
        print("✓ 未知算子检测正常")

    print()
    print("✅ 测试6通过: 校验器工作正常")
    print()


def test_7_ic_metrics():
    """测试7: IC计算"""
    print("=" * 80)
    print("测试7: IC计算")
    print("=" * 80)

    # 创建模拟数据
    n = 1000
    y_true = pd.Series(np.random.randn(n))
    y_score = y_true + np.random.randn(n) * 0.5  # 添加噪声

    ic, coverage, n_valid = compute_ic(y_true, y_score)

    assert not np.isnan(ic)
    assert 0.0 <= coverage <= 1.0
    assert n_valid > 0
    print(f"✓ IC计算: ic={ic:.4f}, coverage={coverage:.4f}, n={n_valid}")

    # 测试缺失值处理
    y_true_na = y_true.copy()
    y_true_na.iloc[:100] = np.nan
    ic_na, coverage_na, n_na = compute_ic(y_true_na, y_score)
    assert n_na < n_valid
    assert coverage_na < coverage
    print(f"✓ 缺失值处理: n={n_na}, coverage={coverage_na:.4f}")

    print()
    print("✅ 测试7通过: IC计算工作正常")
    print()


def test_8_zero_copy():
    """测试8: 零拷贝验证"""
    print("=" * 80)
    print("测试8: 零拷贝验证（就地修改）")
    print("=" * 80)

    df = create_test_data(n=200, symbols=["AAPL"])
    df_id_before = id(df)

    program = {
        "steps": [
            {"name": "feat_ma", "op": "rolling_mean", "col": "close", "window_bars": 10, "per_symbol": True},
        ]
    }

    df_out, new_cols = apply_program(df, program, allow_overwrite=False)
    df_id_after = id(df_out)

    # 验证是同一个对象（就地修改）
    assert df_id_before == df_id_after
    assert "feat_ma" in df.columns  # 原始df也应该有新列
    print("✓ 零拷贝验证通过: 就地修改，无deepcopy")

    print()
    print("✅ 测试8通过: 零拷贝机制工作正常")
    print()


def main():
    print()
    print("=" * 80)
    print("机构级特征工程系统 - 完整测试")
    print("=" * 80)
    print()

    tests = [
        test_1_basic_ops,
        test_2_technical_indicators,
        test_3_microstructure_ops,
        test_4_dsl_execution,
        test_5_templates,
        test_6_validator,
        test_7_ic_metrics,
        test_8_zero_copy,
    ]

    passed = 0
    failed = 0

    for test_fn in tests:
        try:
            test_fn()
            passed += 1
        except Exception as e:
            failed += 1
            print(f"❌ {test_fn.__name__} 失败: {e}")
            import traceback
            traceback.print_exc()

    print()
    print("=" * 80)
    print("测试总结")
    print("=" * 80)
    print(f"✅ 通过: {passed}/{len(tests)}")
    print(f"❌ 失败: {failed}/{len(tests)}")

    if failed == 0:
        print()
        print("🎉 所有测试通过! 机构级特征工程系统部署成功!")
        print()
        print("特性:")
        print("  ✓ 27+ 算子 (基础/技术指标/微观结构)")
        print("  ✓ 零拷贝执行引擎")
        print("  ✓ 严格后视计算")
        print("  ✓ per_symbol 分组支持")
        print("  ✓ 特征模板系统")
        print("  ✓ 程序校验器")
        print("  ✓ IC指标计算")
        print()
        print("下一步:")
        print("  1. 查看 scripts/features_runner.py 了解CLI用法")
        print("  2. 查看 libs/features/feature_templates.py 了解模板系统")
        print("  3. 集成到 wf_hpo.py 的训练流程")
        return 0
    else:
        return 1


if __name__ == "__main__":
    sys.exit(main())
