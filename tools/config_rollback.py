#!/usr/bin/env python3
from __future__ import annotations
"""
Config rollback utility for per-symbol JSON configs (enhanced_ml_{SYM}.json).

Usage:
  # List backups
  python tools/config_rollback.py --list TSLA

  # Restore the most recent backup for TSLA
  python tools/config_rollback.py --restore TSLA

  # Restore a specific backup file
  python tools/config_rollback.py --restore TSLA --file strategies/configs/backups/enhanced_ml_TSLA.json.2025-11-01
"""
import argparse
from pathlib import Path
from typing import List


def _list_backups(sym: str) -> List[Path]:
    bdir = Path("strategies/configs/backups")
    patt = f"enhanced_ml_{sym.upper()}.json."
    return sorted([p for p in bdir.glob(f"{patt}*")], key=lambda p: p.name, reverse=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", default=None, help="Symbol to list backups for")
    ap.add_argument("--restore", default=None, help="Symbol to restore (from latest or --file)")
    ap.add_argument("--file", default=None, help="Specific backup file to restore")
    args = ap.parse_args()

    if args.list:
        backups = _list_backups(args.list)
        if not backups:
            print("No backups found")
            return
        for p in backups:
            print(p)
        return

    if args.restore:
        sym = args.restore.upper()
        target = None
        if args.file:
            target = Path(args.file)
            if not target.exists():
                print(f"Backup file not found: {target}")
                return
        else:
            backups = _list_backups(sym)
            if not backups:
                print("No backups to restore")
                return
            target = backups[0]
        dest = Path("strategies/configs") / f"enhanced_ml_{sym}.json"
        content = target.read_text("utf-8")
        dest.write_text(content, encoding="utf-8")
        print(f"Restored {dest} from {target}")


if __name__ == "__main__":
    main()

