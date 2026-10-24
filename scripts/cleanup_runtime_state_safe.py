#!/usr/bin/env python3
from __future__ import annotations

import argparse
import tarfile
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")
PROTECTED_FILES = {
    "qqq_intraday_atr_seed.json",
    "qqq_intraday_qqq_intraday_001.json",
    "qqq_intraday_observability_qqq_intraday_001.json",
    "qqq_intraday_diag_qqq_intraday_001.jsonl",
    "system_health_check_now.json",
}


@dataclass
class CleanupPlan:
    candidates: list[Path]
    protected: list[Path]
    total_bytes: int


def is_trading_hours(now_et: datetime) -> bool:
    if now_et.weekday() > 4:
        return False
    t = now_et.time()
    return time(9, 30) <= t <= time(16, 0)


def build_plan(state_dir: Path) -> CleanupPlan:
    candidates: list[Path] = []
    protected: list[Path] = []
    total = 0

    for p in sorted(state_dir.iterdir()):
        if not p.is_file():
            continue
        if p.name in PROTECTED_FILES:
            protected.append(p)
            continue

        should_delete = False
        if p.suffix == ".csv":
            should_delete = True
        elif p.name.endswith(".jsonl"):
            should_delete = True
        elif ".jsonl." in p.name:
            should_delete = True

        if should_delete:
            candidates.append(p)
            total += p.stat().st_size

    return CleanupPlan(candidates=candidates, protected=protected, total_bytes=total)


def make_backup(files: list[Path], backup_dir: Path, state_dir: Path) -> Path | None:
    if not files:
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(ET).strftime("%Y%m%d_%H%M%S")
    tar_path = backup_dir / f"runtime_state_cleanup_{stamp}.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tf:
        for fp in files:
            tf.add(fp, arcname=str(fp.relative_to(state_dir.parent)))
    return tar_path


def human_mb(b: int) -> str:
    return f"{b / 1024 / 1024:.2f} MB"


def main() -> int:
    ap = argparse.ArgumentParser(description="Safely clean old runtime/state log artifacts.")
    ap.add_argument("--state-dir", default="runtime/state")
    ap.add_argument("--backup-dir", default="backup/runtime_state_cleanup")
    ap.add_argument("--execute", action="store_true", help="Delete files (default is dry-run).")
    ap.add_argument("--force", action="store_true", help="Allow execution during ET trading hours.")
    args = ap.parse_args()

    state_dir = Path(args.state_dir).resolve()
    backup_dir = Path(args.backup_dir).resolve()
    now_et = datetime.now(ET)

    if not state_dir.exists():
        print(f"state_dir_not_found: {state_dir}")
        return 1

    plan = build_plan(state_dir)
    print(f"now_et: {now_et.isoformat()}")
    print(f"state_dir: {state_dir}")
    print(f"candidate_files: {len(plan.candidates)}")
    print(f"candidate_size: {human_mb(plan.total_bytes)}")
    print(f"protected_files: {len(plan.protected)}")

    preview = plan.candidates[:25]
    for fp in preview:
        print(f"  DELETE {fp.name}")
    if len(plan.candidates) > len(preview):
        print(f"  ... and {len(plan.candidates) - len(preview)} more")

    if not args.execute:
        print("dry_run_only: use --execute to apply")
        return 0

    if is_trading_hours(now_et) and not args.force:
        print("blocked: ET trading hours detected; rerun after hours or pass --force")
        return 2

    backup_tar = make_backup(plan.candidates, backup_dir, state_dir)
    if backup_tar is not None:
        print(f"backup_created: {backup_tar}")

    deleted = 0
    for fp in plan.candidates:
        if fp.exists():
            fp.unlink()
            deleted += 1

    print(f"deleted_files: {deleted}")
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
