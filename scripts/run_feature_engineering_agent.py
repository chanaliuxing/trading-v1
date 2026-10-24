#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
运行Feature Engineering Agent - 生产版本

基于真实HPO结果，调用LLM生成特征工程建议
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


def load_recent_hpo_trials():
    """
    TODO: 从实际HPO结果加载trial数据

    这里使用模拟数据，你需要替换为从以下来源之一加载真实数据:
    1. alpha_out/daily/*.json (HPO输出)
    2. ClickHouse trading.hpo_trials表
    3. Optuna study数据库
    """
    print("⚠️  使用模拟HPO试验数据 (TODO: 从实际HPO结果加载)")

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
            "trial_id": 4,
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
        },
        {
            "trial_id": 5,
            "params": {
                "feature_program_id": "mean_revert_v1",
                "feature_subset": ["ret_1", "feat_vwap_dev", "feat_zscore_close_20"],
                "model_scale_mode": "robust"
            },
            "oos_summary": {
                "oos_avg_sharpe": 1.1,
                "oos_avg_ic": 0.07,
                "profit_factor": 1.4,
                "max_dd": -0.13
            }
        }
    ]


def main():
    print("=" * 80)
    print("Feature Engineering Agent - 生产运行")
    print("=" * 80)
    print()

    # 验证API keys
    print("🔑 验证API keys...")
    keys_ok = True
    for key in ['NVIDIA_API_KEY', 'GEMINI_API_KEY', 'OPENAI_API_KEY']:
        val = os.getenv(key)
        if val:
            print(f"   ✓ {key}: {val[:10]}...")
        else:
            print(f"   ✗ {key}: NOT SET")
            keys_ok = False

    if not keys_ok:
        print("\n⚠️  部分API keys未设置，但会使用provider fallback机制")
    print()

    # 初始化AI Brain
    print("📡 初始化AI Brain...")
    brain = ControlBrain(
        runs_dir=str(project_root / "runs" / "ai_trader"),
        provider="deepseek_nvidia",
        model="deepseek-ai/deepseek-r1"
    )
    print("✓ AI Brain initialized")
    print(f"   Provider: {brain.provider}")
    print(f"   Model: {brain.model}")
    print(f"   Trading universe: {len(brain.trading_universe)} symbols")
    print()

    # 加载当前配置
    print("📂 加载当前配置...")
    current_feature_programs = load_current_feature_programs()
    current_alpha_hpo_config = load_alpha_hpo_config()
    print(f"   ✓ 当前feature programs: {len(current_feature_programs)} programs")
    print(f"   ✓ HPO配置加载完成")
    print()

    # 加载HPO试验结果
    print("📊 加载HPO试验结果...")
    recent_trials = load_recent_hpo_trials()
    print(f"   ✓ 加载了 {len(recent_trials)} 个trials")
    print()

    # 获取可用特征列表 (从实际数据推断)
    available_features = [
        "close", "high", "low", "open", "volume",
        "vwap", "ret_1", "ret_5", "ret_10",
        "atr_5", "atr_14", "atr_20",
        "rsi_14", "bb_upper", "bb_lower", "bb_width",
        "ema_12", "ema_26", "ema_50",
        "vol_avg_20", "spread_bps", "tick_imbalance"
    ]

    # 构建上下文
    context = {
        "recent_trials": recent_trials,
        "available_features": available_features,
        "current_feature_programs": current_feature_programs,
        "current_alpha_hpo_config": current_alpha_hpo_config
    }

    # 调用AI Brain获取特征工程建议
    print("🤖 调用AI Brain (真实LLM)...")
    print("   Provider priority: DeepSeek R1 → Gemini → OpenAI")
    print("   这可能需要30-60秒，请耐心等待...")
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
                # 显示步骤详情
                for i, step in enumerate(prog_def.get('steps', [])[:3], 1):
                    print(f"      {i}. {step.get('op')}: {step.get('name')}")
                if len(prog_def.get('steps', [])) > 3:
                    print(f"      ... (还有 {len(prog_def.get('steps', [])) - 3} 个步骤)")
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
        for i, subset in enumerate(subset_choices[:5], 1):
            print(f"  {i}. {subset}")
        if len(subset_choices) > 5:
            print(f"  ... (还有 {len(subset_choices) - 5} 个)")
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
        print("  1. 审查AI建议，验证合理性")
        print("  2. Dry run预览变更:")
        print(f"     venv/bin/python scripts/apply_feature_engineering_suggestion.py --dry-run")
        print("  3. 应用建议:")
        print(f"     venv/bin/python scripts/apply_feature_engineering_suggestion.py")
        print("  4. 运行HPO验证新特征:")
        print(f"     venv/bin/python alpha_loop.py --config alpha_config_enhanced.json --gens 1")
        print("=" * 80)

        return 0

    except Exception as e:
        print(f"❌ 错误: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
