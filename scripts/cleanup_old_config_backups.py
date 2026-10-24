#!/usr/bin/env python3
"""
Cleanup old config.yml backup files

Consolidates backups from multiple locations and removes old files.

Actions:
1. Move old config.yml.backup_* files to backups/config/
2. Keep only last N backups (default: 30)
3. List backup statistics

Usage:
    python scripts/cleanup_old_config_backups.py --dry-run
    python scripts/cleanup_old_config_backups.py --keep 30
"""

import argparse
from pathlib import Path
from datetime import datetime
import shutil

def cleanup_config_backups(dry_run=True, keep_count=30):
    """Cleanup old config backups."""

    print("=" * 80)
    print("Config Backup Cleanup")
    print("=" * 80)
    print()

    # 1. Find all backup files
    print("1. Scanning for backup files...")
    print("-" * 40)

    # Old location: config.yml.backup_*
    old_backups = list(Path('.').glob('config.yml.backup_*'))
    print(f"Found {len(old_backups)} old-style backups (config.yml.backup_*)")

    # New unified location: backups/config.yml.*
    new_location = Path('backups')
    if new_location.exists():
        new_backups = list(new_location.glob('config.yml.*.yaml'))
        print(f"Found {len(new_backups)} backups in backups/ (old format)")
    else:
        new_backups = []

    # Unified location: backups/config/config.yml.*
    unified_location = Path('backups/config')
    if unified_location.exists():
        unified_backups = list(unified_location.glob('config.yml.*.yaml'))
        print(f"Found {len(unified_backups)} backups in backups/config/ (unified)")
    else:
        unified_backups = []
        unified_location.mkdir(parents=True, exist_ok=True)
        print("Created backups/config/ directory")

    print()

    # 2. Move old backups to unified location
    if old_backups:
        print("2. Moving old-style backups to backups/config/...")
        print("-" * 40)

        moved = 0
        for backup in old_backups:
            # Extract timestamp from filename
            # config.yml.backup_20251102_114322 -> 20251102-114322
            parts = backup.name.split('_')
            if len(parts) >= 3:
                timestamp = f"{parts[-2]}-{parts[-1]}"
                new_name = f"config.yml.{timestamp}.yaml"
                target = unified_location / new_name

                if dry_run:
                    print(f"  [DRY RUN] Would move: {backup.name} -> {target}")
                else:
                    try:
                        shutil.move(str(backup), str(target))
                        print(f"  ✅ Moved: {backup.name} -> {target.name}")
                        moved += 1
                    except Exception as e:
                        print(f"  ❌ Failed to move {backup.name}: {e}")

        if not dry_run:
            print(f"\nMoved {moved} files")
        print()

    # 3. Move backups from old backups/ location
    if new_backups:
        print("3. Moving backups/ files to backups/config/...")
        print("-" * 40)

        moved = 0
        for backup in new_backups:
            target = unified_location / backup.name

            if dry_run:
                print(f"  [DRY RUN] Would move: {backup} -> {target}")
            else:
                try:
                    shutil.move(str(backup), str(target))
                    print(f"  ✅ Moved: {backup.name} -> {target.name}")
                    moved += 1
                except Exception as e:
                    print(f"  ❌ Failed to move {backup.name}: {e}")

        if not dry_run:
            print(f"\nMoved {moved} files")
        print()

    # 4. Remove old backups (keep only last N)
    all_unified = sorted(unified_location.glob('config.yml.*.yaml'))

    print(f"4. Cleanup old backups (keeping last {keep_count})...")
    print("-" * 40)
    print(f"Total backups in backups/config/: {len(all_unified)}")

    if len(all_unified) > keep_count:
        to_remove = all_unified[:-keep_count]  # Remove all except last N
        print(f"Will remove {len(to_remove)} old backups")

        removed = 0
        for backup in to_remove:
            if dry_run:
                print(f"  [DRY RUN] Would remove: {backup.name}")
            else:
                try:
                    backup.unlink()
                    removed += 1
                except Exception as e:
                    print(f"  ❌ Failed to remove {backup.name}: {e}")

        if not dry_run:
            print(f"\nRemoved {removed} old backups")
    else:
        print("No backups to remove")

    print()

    # 5. Final statistics
    print("5. Final statistics...")
    print("-" * 40)

    if not dry_run:
        all_unified = sorted(unified_location.glob('config.yml.*.yaml'))

    print(f"Backups in backups/config/: {len(all_unified)}")

    if all_unified:
        oldest = all_unified[0]
        newest = all_unified[-1]

        # Try to parse timestamps
        try:
            oldest_time = datetime.strptime(oldest.stem.split('.')[-1], '%Y%m%d-%H%M%S')
            newest_time = datetime.strptime(newest.stem.split('.')[-1], '%Y%m%d-%H%M%S')

            print(f"Oldest: {oldest.name} ({oldest_time.strftime('%Y-%m-%d %H:%M:%S')})")
            print(f"Newest: {newest.name} ({newest_time.strftime('%Y-%m-%d %H:%M:%S')})")
        except:
            print(f"Oldest: {oldest.name}")
            print(f"Newest: {newest.name}")

    print()
    print("=" * 80)
    print("Cleanup complete!" if not dry_run else "DRY RUN complete - no changes made")
    print("=" * 80)


def main():
    parser = argparse.ArgumentParser(description='Cleanup old config.yml backups')
    parser.add_argument('--dry-run', action='store_true', help='Show what would be done without actually doing it')
    parser.add_argument('--keep', type=int, default=30, help='Number of recent backups to keep (default: 30)')
    args = parser.parse_args()

    cleanup_config_backups(dry_run=args.dry_run, keep_count=args.keep)


if __name__ == '__main__':
    main()
