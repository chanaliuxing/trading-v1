#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试Feature Engineering Agent (Mock模式)

不调用真实LLM,而是使用预定义的mock响应来测试整个流程
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# 添加项目根目录到Python路径
project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))

from scripts.ai_trader.brain.brain import ControlBrain, LLMCallResult


def create_mock_llm_response():
    """创建一个模拟的LLM响应"""
    return {
        "analysis": "基于模拟的5次试验数据,comprehensive_v1表现最佳(OOS Sharpe=1.45)。microstructure_v1过拟合严重。建议新增momentum_v2,使用波动率归一化的多时间框架动量特征。",
        "feature_programs_patch": {
            "add_or_update": {
                "momentum_v2": {
                    "description": "Multi-horizon momentum with volatility scaling",
                    "steps": [
                        {
                            "name": "feat_ret_1m_roll_10",
                            "op": "rolling_sum",
                            "col": "ret_1",
                            "window_bars": 10,
                            "min_periods": 3,
                            "per_symbol": True
                        },
                        {
                            "name": "feat_ret_5m_roll_30",
                            "op": "rolling_sum",
                            "col": "ret_5",
                            "window_bars": 30,
                            "min_periods": 10,
                            "per_symbol": True
                        },
                        {
                            "name": "feat_vol_20",
                            "op": "rolling_std",
                            "col": "ret_1",
                            "window_bars": 20,
                            "min_periods": 5,
                            "per_symbol": True
                        },
                        {
                            "name": "feat_momo_vol_ratio",
                            "op": "ratio",
                            "num": "feat_ret_1m_roll_10",
                            "den": "feat_vol_20",
                            "eps": 1e-6
                        }
                    ]
                }
            },
            "delete": ["microstructure_v1"]
        },
        "alpha_hpo_patch": {
            "feature_program_id_choices": [
                "none",
                "baseline_v1",
                "momentum_v1",
                "momentum_v2",
                "mean_revert_v1",
                "volatility_v1",
                "comprehensive_v1"
            ],
            "feature_subset_choices": [
                ["ret_1", "atr_14", "volume"],
                ["ret_1", "vwap", "atr_14", "volume"],
                ["feat_ret_1m_roll_10", "feat_vol_20", "feat_momo_vol_ratio", "atr_14"],
                ["ret_1", "feat_vwap_dev", "feat_ret1m_over_atr", "feat_log_volume"]
            ],
            "model_scale_mode_choices": ["zscore", "robust"]
        }
    }


def main():
    print("=" * 80)
    print("Feature Engineering Agent 测试 (Mock模式)")
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

    fp_path = project_root / "feature_programs.json"
    with open(fp_path, "r") as f:
        current_feature_programs = json.load(f)

    config_path = project_root / "alpha_config_enhanced.json"
    with open(config_path, "r") as f:
        full_config = json.load(f)

    grid = full_config.get("hpo", {}).get("grid", {})
    current_alpha_hpo_config = {
        "feature_program_id": grid.get("feature_program_id"),
        "feature_subset": grid.get("feature_subset"),
        "model_scale_mode": grid.get("model_scale_mode")
    }

    print(f"   - 当前feature programs: {len(current_feature_programs)} programs")
    print(f"   - HPO配置加载完成")
    print()

    # 获取可用特征列表
    available_features = [
        "close", "high", "low", "open", "volume",
        "vwap", "ret_1", "ret_5", "ret_10",
        "atr_5", "atr_14", "atr_20",
        "rsi_14", "bb_upper", "bb_lower", "bb_width",
        "ema_12", "ema_26", "ema_50",
        "vol_avg_20", "spread_bps", "tick_imbalance"
    ]

    # 创建模拟试验结果
    recent_trials = [
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
        }
    ]

    print(f"📊 模拟试验数据: {len(recent_trials)} trials")
    print()

    # 构建上下文
    context = {
        "recent_trials": recent_trials,
        "available_features": available_features,
        "current_feature_programs": current_feature_programs,
        "current_alpha_hpo_config": current_alpha_hpo_config
    }

    # Mock LLM调用
    print("🤖 使用Mock模式测试 (不调用真实LLM)...")
    print()

    mock_response = create_mock_llm_response()
    mock_llm_result = LLMCallResult(
        data=mock_response,
        raw=json.dumps(mock_response, indent=2),
        provider_used="mock",
        success=True,
        error_type=None,
        error_message=None,
        fallback_used=False,
        providers_attempted=["mock"]
    )

    # Patch _ask方法
    with patch.object(brain, '_ask', return_value=mock_llm_result):
        try:
            suggestion = brain.suggest_feature_engineering(context)

            print("✓ AI Brain 建议已生成 (Mock)!")
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
            for i, subset in enumerate(subset_choices[:3], 1):
                print(f"  {i}. {subset}")
            if len(subset_choices) > 3:
                print(f"  ... (还有 {len(subset_choices) - 3} 个)")
            print()

            scale_mode_choices = hpo_patch.get("model_scale_mode_choices", [])
            print(f"model_scale_mode 候选:")
            print(f"  {scale_mode_choices}")
            print()

            # 保存完整建议到文件
            output_file = project_root / "feature_engineering_suggestion_mock.json"
            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(suggestion, f, indent=2, ensure_ascii=False)

            print("=" * 80)
            print(f"✓ Mock建议已保存到: {output_file}")
            print()
            print("✅ Mock测试成功!")
            print()
            print("下一步:")
            print("  1. 配置API keys后测试真实LLM调用")
            print("  2. 或直接使用这个mock建议测试应用脚本")
            print("=" * 80)

        except Exception as e:
            print(f"❌ 错误: {e}")
            import traceback
            traceback.print_exc()
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
