#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试Feature Engineering Agent

这个脚本演示如何使用AI Brain的特征工程建议功能
"""

import json
import sys
import os
from pathlib import Path
from dotenv import load_dotenv

# 添加项目根目录到Python路径
project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))

# 加载.env文件
load_dotenv(project_root / ".env")

from scripts.ai_trader.brain.brain import ControlBrain


def load_current_feature_programs():
    """加载当前的feature_programs.json"""
    fp_path = project_root / "feature_programs.json"
    if not fp_path.exists():
        print(f"⚠️  feature_programs.json not found at {fp_path}")
        return {}

    with open(fp_path, "r") as f:
        return json.load(f)


def load_alpha_hpo_config():
    """加载alpha_config_enhanced.json中与特征相关的HPO配置"""
    config_path = project_root / "alpha_config_enhanced.json"
    if not config_path.exists():
        print(f"⚠️  alpha_config_enhanced.json not found at {config_path}")
        return {}

    with open(config_path, "r") as f:
        full_config = json.load(f)

    # 提取特征相关的HPO空间
    grid = full_config.get("hpo", {}).get("grid", {})
    return {
        "feature_program_id": grid.get("feature_program_id"),
        "feature_subset": grid.get("feature_subset"),
        "model_scale_mode": grid.get("model_scale_mode")
    }


def create_mock_trials():
    """创建模拟的HPO试验结果 (用于演示)"""
    return [
        {
            "trial_id": 1,
            "params": {
                "feature_program_id": "baseline_v1",
                "feature_subset": ["ret_1", "atr_14", "volume"],
                "model_scale_mode": "robust"
            },
            "oos_summary": {
                "oos_avg_sharpe": 1.2,
                "oos_avg_ic": 0.08,
                "profit_factor": 1.5,
                "max_dd": -0.12
            }
        },
        {
            "trial_id": 2,
            "params": {
                "feature_program_id": "momentum_v1",
                "feature_subset": ["ret_1", "ret_5", "feat_ret_1m_roll_10", "feat_vwap_dev"],
                "model_scale_mode": "zscore"
            },
            "oos_summary": {
                "oos_avg_sharpe": 0.85,
                "oos_avg_ic": 0.04,
                "profit_factor": 1.1,
                "max_dd": -0.18
            }
        },
        {
            "trial_id": 3,
            "params": {
                "feature_program_id": "microstructure_v1",
                "feature_subset": ["ret_1", "feat_price_impact", "feat_effective_spread", "atr_14"],
                "model_scale_mode": "robust"
            },
            "oos_summary": {
                "oos_avg_sharpe": 0.65,
                "oos_avg_ic": 0.02,
                "profit_factor": 0.95,
                "max_dd": -0.22
            }
        },
        {
            "trial_id": 4,
            "params": {
                "feature_program_id": "comprehensive_v1",
                "feature_subset": ["ret_1", "vwap", "atr_14", "feat_vwap_dev", "feat_ret1m_over_atr"],
                "model_scale_mode": "zscore"
            },
            "oos_summary": {
                "oos_avg_sharpe": 1.45,
                "oos_avg_ic": 0.11,
                "profit_factor": 1.8,
                "max_dd": -0.09
            }
        },
        {
            "trial_id": 5,
            "params": {
                "feature_program_id": "volatility_v1",
                "feature_subset": ["ret_1", "atr_14", "volume", "feat_log_volume"],
                "model_scale_mode": "robust"
            },
            "oos_summary": {
                "oos_avg_sharpe": 0.92,
                "oos_avg_ic": 0.05,
                "profit_factor": 1.25,
                "max_dd": -0.15
            }
        }
    ]


def main():
    print("=" * 80)
    print("Feature Engineering Agent 测试")
    print("=" * 80)
    print()

    # 初始化AI Brain
    print("📡 初始化AI Brain...")
    brain = ControlBrain(
        runs_dir=str(project_root / "runs" / "ai_trader"),
        provider="deepseek_nvidia",
        model="deepseek-ai/deepseek-r1"
    )
    print("✓ AI Brain initialized")
    print()

    # 加载当前配置
    print("📂 加载当前配置...")
    current_feature_programs = load_current_feature_programs()
    current_alpha_hpo_config = load_alpha_hpo_config()

    print(f"   - 当前feature programs: {len(current_feature_programs)} programs")
    print(f"   - HPO配置加载完成")
    print()

    # 获取可用特征列表 (从当前配置中推断)
    available_features = [
        "close", "high", "low", "open", "volume",
        "vwap", "ret_1", "ret_5", "ret_10",
        "atr_5", "atr_14", "atr_20",
        "rsi_14", "bb_upper", "bb_lower", "bb_width",
        "ema_12", "ema_26", "ema_50",
        "vol_avg_20", "spread_bps", "tick_imbalance"
    ]

    # 创建模拟试验结果
    recent_trials = create_mock_trials()
    print(f"📊 模拟试验数据: {len(recent_trials)} trials")
    print()

    # 构建上下文
    context = {
        "recent_trials": recent_trials,
        "available_features": available_features,
        "current_feature_programs": current_feature_programs,
        "current_alpha_hpo_config": current_alpha_hpo_config
    }

    # 调用AI Brain获取特征工程建议
    print("🤖 调用AI Brain获取特征工程建议...")
    print("   (这可能需要30-60秒,请耐心等待)")
    print()

    try:
        suggestion = brain.suggest_feature_engineering(context)

        print("✓ AI Brain 建议已生成!")
        print("=" * 80)
        print()

        # 显示分析
        print("📝 AI Brain 分析:")
        print("-" * 80)
        print(suggestion.get("analysis", "No analysis provided"))
        print()

        # 显示特征程序修改建议
        fp_patch = suggestion.get("feature_programs_patch", {})
        add_or_update = fp_patch.get("add_or_update", {})
        delete = fp_patch.get("delete", [])

        print("🔧 Feature Programs 修改建议:")
        print("-" * 80)

        if add_or_update:
            print(f"新增/更新 {len(add_or_update)} 个程序:")
            for prog_id, prog_def in add_or_update.items():
                print(f"  • {prog_id}: {prog_def.get('description', 'No description')}")
                print(f"    步骤数: {len(prog_def.get('steps', []))}")
        else:
            print("  无新增/更新程序")

        if delete:
            print(f"\n删除 {len(delete)} 个程序:")
            for prog_id in delete:
                print(f"  • {prog_id}")
        else:
            print("\n  无删除程序")
        print()

        # 显示HPO配置修改建议
        hpo_patch = suggestion.get("alpha_hpo_patch", {})

        print("⚙️  HPO配置修改建议:")
        print("-" * 80)

        prog_id_choices = hpo_patch.get("feature_program_id_choices", [])
        print(f"feature_program_id 候选 ({len(prog_id_choices)}):")
        print(f"  {prog_id_choices}")
        print()

        subset_choices = hpo_patch.get("feature_subset_choices", [])
        print(f"feature_subset 候选 ({len(subset_choices)}):")
        for i, subset in enumerate(subset_choices[:3], 1):  # 只显示前3个
            print(f"  {i}. {subset}")
        if len(subset_choices) > 3:
            print(f"  ... (还有 {len(subset_choices) - 3} 个)")
        print()

        scale_mode_choices = hpo_patch.get("model_scale_mode_choices", [])
        print(f"model_scale_mode 候选:")
        print(f"  {scale_mode_choices}")
        print()

        # 保存完整建议到文件
        output_file = project_root / "feature_engineering_suggestion_latest.json"
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(suggestion, f, indent=2, ensure_ascii=False)

        print("=" * 80)
        print(f"✓ 完整建议已保存到: {output_file}")
        print()
        print("下一步:")
        print("  1. 审查AI建议,验证合理性")
        print("  2. 手动应用到 feature_programs.json 和 alpha_config_enhanced.json")
        print("  3. 运行 HPO 测试新特征程序")
        print("=" * 80)

    except Exception as e:
        print(f"❌ 错误: {e}")
        import traceback
        traceback.print_exc()
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
