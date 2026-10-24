#!/usr/bin/env python3
"""
应用HPO优化参数到策略配置文件

Usage:
    python apply_hpo_params.py \
        --hpo-params my-first-hpo_best_params.json \
        --strategy-config strategies/configs/tsla_v7_2.json \
        --output strategies/configs/tsla_v7_2_optimized.json
"""

import json
import argparse
from pathlib import Path
from datetime import datetime


def apply_hpo_params(hpo_params_file: str, strategy_config_file: str, output_file: str = None, backup: bool = True):
    """
    将HPO优化的参数应用到策略配置文件

    Args:
        hpo_params_file: HPO输出的最佳参数JSON文件
        strategy_config_file: 原始策略配置文件
        output_file: 输出文件路径（可选，默认覆盖原文件）
        backup: 是否备份原文件
    """
    # 读取HPO优化参数
    print(f"[1/4] 读取HPO优化参数: {hpo_params_file}")
    with open(hpo_params_file, 'r') as f:
        hpo_params = json.load(f)

    print(f"      HPO优化参数:")
    for key, value in hpo_params.items():
        print(f"        {key}: {value}")

    # 读取原始策略配置
    print(f"\n[2/4] 读取原始策略配置: {strategy_config_file}")
    with open(strategy_config_file, 'r') as f:
        strategy_config = json.load(f)

    # 备份原文件
    if backup and output_file is None:
        backup_file = f"{strategy_config_file}.backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        print(f"\n[3/4] 备份原配置到: {backup_file}")
        with open(backup_file, 'w') as f:
            json.dump(strategy_config, f, indent=2)

    # 应用参数
    print(f"\n[4/4] 应用优化参数...")

    # 参数映射规则
    param_mapping = {
        # HPO参数名 -> (配置文件位置, 配置键名)
        "A1_thr_sigma": ("A1", "thr_sigma"),
        "A1_rvol_lo": ("A1", "rvol_lo"),
        "A1_rvol_hi": ("A1", "rvol_hi"),
        "A2_grid_g": ("A2", "grid_g"),
        "A2_grid_g_floor": ("A2", "grid_g_floor"),
        "A2_oneway_thr": ("A2", "oneway_thr"),
        "A2_max_layers": ("A2", "max_layers"),
        "B1_rvol_min": ("B1", "rvol_min"),
        "B1_pullback_window_M": ("B1", "pullback_window_M"),
        "k_atr": (None, "k_atr"),  # None表示根级参数
    }

    changes = []
    for hpo_key, hpo_value in hpo_params.items():
        if hpo_key not in param_mapping:
            print(f"      ⚠️  跳过未知参数: {hpo_key}")
            continue

        section, config_key = param_mapping[hpo_key]

        if section is None:
            # 根级参数
            old_value = strategy_config.get(config_key, "N/A")
            strategy_config[config_key] = hpo_value
            changes.append((config_key, old_value, hpo_value))
            print(f"      ✓ {config_key}: {old_value} → {hpo_value}")
        else:
            # 嵌套参数（如A1.thr_sigma）
            if section not in strategy_config:
                strategy_config[section] = {}
            old_value = strategy_config[section].get(config_key, "N/A")
            strategy_config[section][config_key] = hpo_value
            changes.append((f"{section}.{config_key}", old_value, hpo_value))
            print(f"      ✓ {section}.{config_key}: {old_value} → {hpo_value}")

    # 添加元数据
    strategy_config["_hpo_metadata"] = {
        "applied_at": datetime.now().isoformat(),
        "hpo_params_file": hpo_params_file,
        "original_config": strategy_config_file,
        "changes": [{"param": k, "old": old, "new": new} for k, old, new in changes]
    }

    # 保存
    output_path = output_file or strategy_config_file
    print(f"\n[完成] 保存更新配置到: {output_path}")
    with open(output_path, 'w') as f:
        json.dump(strategy_config, f, indent=2)

    print(f"\n{'='*60}")
    print(f"成功应用 {len(changes)} 个HPO优化参数!")
    print(f"{'='*60}\n")

    return output_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="应用HPO优化参数到策略配置")
    parser.add_argument("--hpo-params", required=True, help="HPO最佳参数JSON文件")
    parser.add_argument("--strategy-config", required=True, help="策略配置文件")
    parser.add_argument("--output", help="输出文件路径（可选，默认覆盖原文件）")
    parser.add_argument("--no-backup", action="store_true", help="不备份原文件")

    args = parser.parse_args()

    apply_hpo_params(
        hpo_params_file=args.hpo_params,
        strategy_config_file=args.strategy_config,
        output_file=args.output,
        backup=not args.no_backup
    )
