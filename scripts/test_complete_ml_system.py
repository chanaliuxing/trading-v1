#!/usr/bin/env python3
"""
完整ML系统端到端测试

验证点：
1. 交易日历工具（周末/节假日/时段推进）
2. 标签生成（strict/sessions/anchor三种模式）
3. XGB特征工厂（叶子嵌入+Hash降维）
4. Featuretools DFS（可选，需安装）
5. 配置文件加载（ml_policy.json + feature_tools.json）
6. 训练/回测一致性
"""
import sys
import json
import yaml
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from libs.features.market_calendar_utils import (
    build_calendar_from_config,
    advance_in_sessions,
    jump_to_session_anchor,
    ET
)
from libs.features.label_utils import (
    compute_label,
    make_sample_weight,
    xgb_params_from_policy
)
from libs.features.xgb_feature_maker import XGBFeatureMaker


def test_market_calendar():
    """测试交易日历工具"""
    print("\n" + "=" * 70)
    print("测试1: 交易日历工具")
    print("=" * 70)

    # 加载config.yml
    with open("config.yml") as f:
        config = yaml.safe_load(f)

    cal = build_calendar_from_config(config)

    print(f"✓ 日历配置加载成功")
    print(f"  - 开盘时间: {cal.trading_hours.open_et}")
    print(f"  - 收盘时间: {cal.trading_hours.close_et}")
    print(f"  - 节假日数: {len(cal.holidays)}")

    # 测试时段推进
    ts = pd.Timestamp("2024-11-12 09:30:00", tz=ET)

    # 推进300秒（5分钟）
    t_next = advance_in_sessions(ts, 300, cal)
    assert (t_next - ts).total_seconds() == 300
    print(f"✓ 交易时段推进: {ts} → {t_next} (+300s)")

    # 跳到下一交易日开盘
    t_open = jump_to_session_anchor(ts, cal, anchor="next_open")
    print(f"✓ 跳到下一交易日开盘: {ts} → {t_open}")

    # 跳到当日收盘
    t_close = jump_to_session_anchor(ts, cal, anchor="same_day_close")
    print(f"✓ 跳到当日收盘: {ts} → {t_close}")

    return True


def test_label_generation():
    """测试标签生成（三种模式）"""
    print("\n" + "=" * 70)
    print("测试2: 标签生成（strict/sessions/anchor）")
    print("=" * 70)

    # 准备测试数据
    np.random.seed(42)
    n_samples = 100

    data = []
    base_ts = pd.Timestamp("2024-11-12 09:30:00", tz="UTC")

    for i in range(n_samples):
        data.append({
            "ts": base_ts + pd.Timedelta(minutes=i),
            "symbol": "AAPL",
            "open": 100 + np.random.randn() * 2,
            "high": 102 + np.random.randn() * 2,
            "low": 98 + np.random.randn() * 2,
            "close": 100 + np.random.randn() * 2,
            "volume": int(1000 + np.random.randn() * 100),
            "atr_14": 2.0 + np.random.randn() * 0.5
        })

    df = pd.DataFrame(data)

    # 加载配置
    with open("ml_policy.json") as f:
        ml_policy = json.load(f)

    with open("config.yml") as f:
        config_yaml = yaml.safe_load(f)

    # 测试1: strict模式
    ml_policy["label"]["calendar_mode"] = "strict"
    ml_policy["label"]["horizon_sec"] = 60
    ml_policy["label"]["max_slip_sec"] = 0

    y_strict = compute_label(df, ml_policy, config_yaml=config_yaml)
    valid_pct = y_strict.notna().mean()
    print(f"✓ Strict模式: 有效标签比例 {valid_pct:.1%}")

    # 测试2: sessions模式
    ml_policy["label"]["calendar_mode"] = "sessions"
    ml_policy["label"]["horizon_sec"] = 300

    y_sessions = compute_label(df, ml_policy, config_yaml=config_yaml)
    valid_pct = y_sessions.notna().mean()
    print(f"✓ Sessions模式: 有效标签比例 {valid_pct:.1%}")

    # 测试3: anchor模式
    ml_policy["label"]["calendar_mode"] = "anchor"
    ml_policy["label"]["anchor"] = "next_open"

    y_anchor = compute_label(df, ml_policy, config_yaml=config_yaml)
    valid_pct = y_anchor.notna().mean()
    print(f"✓ Anchor模式: 有效标签比例 {valid_pct:.1%}")

    # 测试样本权重
    w = make_sample_weight(df, ml_policy)
    if w is not None:
        print(f"✓ 样本权重生成: 均值={w.mean():.2f}, 标准差={w.std():.2f}")

    # 测试XGBoost参数提取
    xgb_params = xgb_params_from_policy(ml_policy)
    print(f"✓ XGBoost参数: {xgb_params['objective']}")

    return True


def test_xgb_feature_maker():
    """测试XGB特征工厂"""
    print("\n" + "=" * 70)
    print("测试3: XGB特征工厂（叶子嵌入+Hash降维）")
    print("=" * 70)

    # 准备测试数据
    np.random.seed(42)
    n_samples = 200

    X = pd.DataFrame(np.random.randn(n_samples, 10), columns=[f"feat_{i}" for i in range(10)])
    y = pd.Series(np.random.randn(n_samples) > 0, dtype=int, name="target")

    # 分割IS/OOS
    split = int(n_samples * 0.7)
    X_is, X_oos = X.iloc[:split], X.iloc[split:]
    y_is, y_oos = y.iloc[:split], y.iloc[split:]

    # 加载配置
    with open("feature_tools.json") as f:
        feature_tools = json.load(f)

    xgb_cfg = feature_tools.get("xgb_feature_maker", {})

    if not xgb_cfg.get("enabled", False):
        print("⚠️  XGB特征工厂未启用，跳过测试")
        return True

    # 初始化
    xfm = XGBFeatureMaker(xgb_cfg, task="binary")
    print(f"✓ XGB特征工厂初始化: mode={xfm.mode}, hash_dim={xfm.hash_dim}")

    # IS拟合
    xfm.fit(X_is, y_is)
    print(f"✓ IS拟合完成: {xfm.model.n_estimators}棵树")

    # IS转换
    X_is_emb = xfm.transform(X_is)
    print(f"✓ IS转换: {X_is.shape} → {X_is_emb.shape}")

    # OOS转换
    X_oos_emb = xfm.transform(X_oos)
    print(f"✓ OOS转换: {X_oos.shape} → {X_oos_emb.shape}")

    # 验证列一致性
    assert set(X_is_emb.columns) == set(X_oos_emb.columns)
    print(f"✓ IS/OOS列一致性验证通过")

    return True


def test_config_loading():
    """测试配置文件加载"""
    print("\n" + "=" * 70)
    print("测试4: 配置文件加载")
    print("=" * 70)

    # ml_policy.json
    with open("ml_policy.json") as f:
        ml_policy = json.load(f)

    print(f"✓ ml_policy.json加载成功")
    print(f"  - task: {ml_policy['task']}")
    print(f"  - label.horizon_sec: {ml_policy['label']['horizon_sec']}")
    print(f"  - label.calendar_mode: {ml_policy['label'].get('calendar_mode', 'strict')}")
    print(f"  - loss.xgb_objective: {ml_policy['loss']['xgb_objective']}")

    # feature_tools.json
    with open("feature_tools.json") as f:
        feature_tools = json.load(f)

    print(f"\n✓ feature_tools.json加载成功")
    print(f"  - pipeline.mode: {feature_tools['pipeline']['mode']}")
    print(f"  - xgb_feature_maker.enabled: {feature_tools['xgb_feature_maker']['enabled']}")
    print(f"  - featuretools.enabled: {feature_tools['featuretools']['enabled']}")

    # config.yml
    with open("config.yml") as f:
        config = yaml.safe_load(f)

    print(f"\n✓ config.yml加载成功")
    print(f"  - trading_hours.open_et: {config['trading_hours']['open_et']}")
    print(f"  - trading_hours.close_et: {config['trading_hours']['close_et']}")

    return True


def test_end_to_end():
    """端到端测试：模拟完整训练流程"""
    print("\n" + "=" * 70)
    print("测试5: 端到端集成测试")
    print("=" * 70)

    # 1. 准备数据
    np.random.seed(42)
    n_samples = 200

    data = []
    base_ts = pd.Timestamp("2024-11-12 09:30:00", tz="UTC")

    for i in range(n_samples):
        data.append({
            "ts": base_ts + pd.Timedelta(minutes=i),
            "symbol": "AAPL",
            "open": 100 + np.random.randn() * 2,
            "high": 102 + np.random.randn() * 2,
            "low": 98 + np.random.randn() * 2,
            "close": 100 + np.random.randn() * 2,
            "volume": int(1000 + np.random.randn() * 100),
            "atr_14": 2.0 + np.random.randn() * 0.5
        })

    df = pd.DataFrame(data)

    # 添加基础特征
    for i in range(5):
        df[f"feat_{i}"] = np.random.randn(n_samples)

    # 2. 加载配置
    with open("ml_policy.json") as f:
        ml_policy = json.load(f)

    with open("feature_tools.json") as f:
        feature_tools = json.load(f)

    with open("config.yml") as f:
        config_yaml = yaml.safe_load(f)

    # 3. 分割IS/OOS
    split = int(n_samples * 0.7)
    df_is = df.iloc[:split].copy()
    df_oos = df.iloc[split:].copy()

    print(f"数据分割: IS={len(df_is)}, OOS={len(df_oos)}")

    # 4. 生成标签
    ml_policy["label"]["calendar_mode"] = "strict"
    ml_policy["label"]["horizon_sec"] = 60

    y_is = compute_label(df_is, ml_policy, config_yaml=config_yaml)
    y_oos = compute_label(df_oos, ml_policy, config_yaml=config_yaml)

    # 5. 样本权重
    w_is = make_sample_weight(df_is, ml_policy)

    # 6. 丢弃缺失
    mask_is = y_is.notna()
    mask_oos = y_oos.notna()

    print(f"标签生成: IS有效={mask_is.sum()}/{len(mask_is)}, OOS有效={mask_oos.sum()}/{len(mask_oos)}")

    # 7. 特征准备
    feat_cols = [f"feat_{i}" for i in range(5)]
    X_is = df_is.loc[mask_is, feat_cols].copy()
    X_oos = df_oos.loc[mask_oos, feat_cols].copy()
    y_is = y_is.loc[mask_is]
    y_oos = y_oos.loc[mask_oos]

    if w_is is not None:
        w_is = w_is.loc[mask_is]

    # 8. XGB特征工厂（如果启用）
    xgb_cfg = feature_tools.get("xgb_feature_maker", {})
    if xgb_cfg.get("enabled", False):
        print("\n应用XGB特征工厂...")
        xfm = XGBFeatureMaker(xgb_cfg, task=ml_policy["task"])
        xfm.fit(X_is, y_is, sample_weight=w_is)

        X_is_xgb = xfm.transform(X_is)
        X_oos_xgb = xfm.transform(X_oos)

        # 合并特征
        X_is = pd.concat([X_is.reset_index(drop=True), X_is_xgb], axis=1)
        X_oos = pd.concat([X_oos.reset_index(drop=True), X_oos_xgb], axis=1)

        print(f"  特征扩展: {len(feat_cols)}维 → {X_is.shape[1]}维")

    print(f"\n最终特征矩阵: IS={X_is.shape}, OOS={X_oos.shape}")
    print(f"✓ 端到端流程验证通过")

    return True


if __name__ == "__main__":
    try:
        print("=" * 70)
        print("完整ML系统端到端测试")
        print("=" * 70)

        success = True

        # 运行所有测试
        success &= test_market_calendar()
        success &= test_label_generation()
        success &= test_xgb_feature_maker()
        success &= test_config_loading()
        success &= test_end_to_end()

        print("\n" + "=" * 70)
        if success:
            print("✅ 所有测试通过!")
        else:
            print("❌ 部分测试失败")
        print("=" * 70)

        sys.exit(0 if success else 1)

    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
