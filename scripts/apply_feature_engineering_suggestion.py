#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
应用Feature Engineering Agent的建议

这个脚本自动应用AI Brain的特征工程建议到配置文件
"""

import json
import sys
import shutil
from pathlib import Path
from datetime import datetime

# 添加项目根目录到Python路径
project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))


def backup_file(file_path: Path) -> Path:
    """备份文件"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = file_path.parent / f"{file_path.stem}_backup_{timestamp}{file_path.suffix}"
    shutil.copy2(file_path, backup_path)
    return backup_path


def apply_feature_programs_patch(
    programs: dict,
    patch: dict,
    dry_run: bool = False
) -> tuple[dict, list]:
    """
    应用feature_programs patch

    Args:
        programs: 当前的feature programs字典
        patch: patch字典 (包含add_or_update和delete)
        dry_run: 是否为dry run模式

    Returns:
        (updated_programs, changes_log)
    """
    changes = []

    # 复制一份用于修改
    updated_programs = programs.copy() if not dry_run else programs

    # 添加/更新程序
    add_or_update = patch.get("add_or_update", {})
    for prog_id, prog_def in add_or_update.items():
        action = "Updated" if prog_id in updated_programs else "Added"
        if not dry_run:
            updated_programs[prog_id] = prog_def
        changes.append(f"  {action}: {prog_id} ({prog_def.get('description', 'No description')})")

    # 删除程序
    delete = patch.get("delete", [])
    for prog_id in delete:
        if prog_id in updated_programs:
            if not dry_run:
                del updated_programs[prog_id]
            changes.append(f"  Deleted: {prog_id}")
        else:
            changes.append(f"  Warning: {prog_id} not found, cannot delete")

    return updated_programs, changes


def apply_alpha_hpo_patch(
    config: dict,
    patch: dict,
    dry_run: bool = False
) -> tuple[dict, list]:
    """
    应用alpha_config_enhanced.json的HPO patch

    Args:
        config: 当前的alpha config字典
        patch: HPO patch字典
        dry_run: 是否为dry run模式

    Returns:
        (updated_config, changes_log)
    """
    changes = []

    # 复制一份用于修改
    updated_config = config.copy() if not dry_run else config

    grid = updated_config["hpo"]["grid"]

    # 更新feature_program_id
    if "feature_program_id_choices" in patch:
        old_choices = grid.get("feature_program_id", {}).get("values", [])
        new_choices = patch["feature_program_id_choices"]

        if not dry_run:
            grid["feature_program_id"]["values"] = new_choices

        changes.append(f"  feature_program_id: {len(old_choices)} → {len(new_choices)} choices")
        changes.append(f"    Old: {old_choices}")
        changes.append(f"    New: {new_choices}")

    # 更新feature_subset
    if "feature_subset_choices" in patch:
        old_subsets = grid.get("feature_subset", {}).get("values", [])
        new_subsets = patch["feature_subset_choices"]

        if not dry_run:
            grid["feature_subset"]["values"] = new_subsets

        changes.append(f"  feature_subset: {len(old_subsets)} → {len(new_subsets)} subsets")

    # 更新model_scale_mode
    if "model_scale_mode_choices" in patch:
        old_modes = grid.get("model_scale_mode", {}).get("values", [])
        new_modes = patch["model_scale_mode_choices"]

        if not dry_run:
            grid["model_scale_mode"]["values"] = new_modes

        changes.append(f"  model_scale_mode: {old_modes} → {new_modes}")

    return updated_config, changes


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="应用Feature Engineering Agent的建议到配置文件"
    )
    parser.add_argument(
        "--suggestion",
        type=str,
        default="feature_engineering_suggestion_latest.json",
        help="建议文件路径 (默认: feature_engineering_suggestion_latest.json)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Dry run模式,只显示变更不实际修改文件"
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="不创建备份文件 (不推荐)"
    )

    args = parser.parse_args()

    print("=" * 80)
    print("应用Feature Engineering建议")
    print("=" * 80)
    print()

    # 加载建议
    suggestion_path = project_root / args.suggestion
    if not suggestion_path.exists():
        print(f"❌ 建议文件不存在: {suggestion_path}")
        return 1

    print(f"📂 加载建议: {suggestion_path}")
    with open(suggestion_path, "r", encoding="utf-8") as f:
        suggestion = json.load(f)

    # 显示AI分析
    print()
    print("📝 AI Brain 分析:")
    print("-" * 80)
    print(suggestion.get("analysis", "No analysis provided"))
    print()

    # 加载当前配置
    fp_path = project_root / "feature_programs.json"
    alpha_path = project_root / "alpha_config_enhanced.json"

    if not fp_path.exists():
        print(f"❌ feature_programs.json不存在: {fp_path}")
        return 1

    if not alpha_path.exists():
        print(f"❌ alpha_config_enhanced.json不存在: {alpha_path}")
        return 1

    with open(fp_path, "r", encoding="utf-8") as f:
        current_programs = json.load(f)

    with open(alpha_path, "r", encoding="utf-8") as f:
        current_alpha = json.load(f)

    # 应用feature_programs patch
    print("🔧 应用Feature Programs变更...")
    print("-" * 80)

    fp_patch = suggestion.get("feature_programs_patch", {})
    updated_programs, fp_changes = apply_feature_programs_patch(
        current_programs,
        fp_patch,
        dry_run=args.dry_run
    )

    if fp_changes:
        for change in fp_changes:
            print(change)
    else:
        print("  无变更")
    print()

    # 应用alpha_config HPO patch
    print("⚙️  应用HPO配置变更...")
    print("-" * 80)

    hpo_patch = suggestion.get("alpha_hpo_patch", {})
    updated_alpha, hpo_changes = apply_alpha_hpo_patch(
        current_alpha,
        hpo_patch,
        dry_run=args.dry_run
    )

    if hpo_changes:
        for change in hpo_changes:
            print(change)
    else:
        print("  无变更")
    print()

    # 保存或显示dry run结果
    if args.dry_run:
        print("=" * 80)
        print("🔍 Dry Run模式 - 未实际修改文件")
        print("=" * 80)
        print()
        print("如果确认无误,请去掉 --dry-run 参数重新运行")
        return 0

    # 备份原文件
    if not args.no_backup:
        print("💾 创建备份...")
        fp_backup = backup_file(fp_path)
        alpha_backup = backup_file(alpha_path)
        print(f"  ✓ {fp_path.name} → {fp_backup.name}")
        print(f"  ✓ {alpha_path.name} → {alpha_backup.name}")
        print()

    # 保存更新后的配置
    print("💾 保存更新...")
    with open(fp_path, "w", encoding="utf-8") as f:
        json.dump(updated_programs, f, indent=2, ensure_ascii=False)
    print(f"  ✓ {fp_path.name}")

    with open(alpha_path, "w", encoding="utf-8") as f:
        json.dump(updated_alpha, f, indent=2, ensure_ascii=False)
    print(f"  ✓ {alpha_path.name}")
    print()

    # 完成
    print("=" * 80)
    print("✅ 建议已成功应用!")
    print("=" * 80)
    print()
    print("下一步:")
    print("  1. 验证配置文件语法: python -m json.tool feature_programs.json > /dev/null")
    print("  2. 运行HPO测试新特征: venv/bin/python alpha_loop.py --config alpha_config_enhanced.json --gens 1")
    print("  3. 对比结果,决定是否保留新特征")
    print()

    if not args.no_backup:
        print("💡 如需回滚:")
        print(f"  cp {fp_backup.name} feature_programs.json")
        print(f"  cp {alpha_backup.name} alpha_config_enhanced.json")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
