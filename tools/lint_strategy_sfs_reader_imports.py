#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Dict, List, Tuple


BASELINE_SFS_READER_REFERENCES: Dict[str, int] = {
    "strategies/enhanced_model_signal_strategy.py": 3,
    "strategies/model_signal_strategy.py": 2,
    "strategies/reve_intraday_strategy.py": 4,
    "strategies/sfs_fusion_demo.py": 2,
}


def _is_python_file(path: Path) -> bool:
    return path.is_file() and path.suffix == ".py"


def collect_sfs_reader_references(repo_root: Path) -> Dict[str, int]:
    strategies_dir = repo_root / "strategies"
    references: Dict[str, int] = {}
    if not strategies_dir.exists():
        return references
    for path in sorted(strategies_dir.rglob("*.py")):
        if not _is_python_file(path):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        count = text.count("SFS1sReader")
        if count <= 0:
            continue
        references[str(path.relative_to(repo_root))] = count
    return references


def find_violations(repo_root: Path) -> List[str]:
    current = collect_sfs_reader_references(repo_root)
    violations: List[str] = []
    for rel_path, count in sorted(current.items()):
        baseline_count = int(BASELINE_SFS_READER_REFERENCES.get(rel_path, 0))
        if count > baseline_count:
            violations.append(
                f"{rel_path}: SFS1sReader references increased from {baseline_count} to {count}"
            )
    removed_paths = sorted(set(BASELINE_SFS_READER_REFERENCES) - set(current))
    if removed_paths:
        # Removal is allowed. Keep this branch explicit so future maintainers do
        # not re-add deleted direct readers without updating the baseline down.
        pass
    return violations


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Fail if strategies add new direct SFS1sReader references. "
            "The baseline is temporary and must only move downward."
        )
    )
    parser.add_argument("--repo-root", default=".", help="Repository root")
    args = parser.parse_args(argv)
    repo_root = Path(args.repo_root).resolve()
    violations = find_violations(repo_root)
    if violations:
        print("Direct strategy SFS1sReader usage may not increase:")
        for item in violations:
            print(f"- {item}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
