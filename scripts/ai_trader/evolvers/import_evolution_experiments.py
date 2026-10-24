#!/usr/bin/env python3
"""
Import local evolution experiment JSONL records into ClickHouse trading.evolution_experiments.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Add project root to path to allow importing libs and scripts
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import json
from pathlib import Path
from typing import Any, Dict

from datetime import datetime
from libs.db.ch_pool import get_client
from libs.logger import get_logger

log = get_logger(__name__)


def import_file(cli, path: Path) -> int:
    rows_to_insert = []
    try:
        lines = path.read_text("utf-8").splitlines()
    except Exception as exc:
        log.error("experiment_file_read_failed", file=str(path), error=str(exc))
        return 0

    for line in lines:
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
            if not rec.get("experiment_id"):
                continue
            if not rec.get("test_result"):
                continue
            mb = rec.get("metrics_before") or {}
            ma = rec.get("metrics_after") or {}
            row = {
                "experiment_id": rec.get("experiment_id"),
                "target_skill": rec.get("target_skill"),
                "objective": rec.get("objective", "PERFORMANCE"),
                "git_branch": rec.get("git_branch", ""),
                "base_commit": rec.get("base_commit", ""),
                "new_commit": rec.get("new_commit", ""),
                "test_result": "PASS" if rec.get("test_result") == "PASS" else "FAIL",
                "metrics_before": mb if isinstance(mb, dict) else {},
                "metrics_after": ma if isinstance(ma, dict) else {},
                "status": rec.get("status", "PENDING"),
            }
            rows_to_insert.append(row)
        except Exception as exc:
            log.warning("experiment_record_parse_failed", file=str(path), error=str(exc))
    if not rows_to_insert:
        return 0
    try:
        columns = [
            "experiment_id",
            "target_skill",
            "objective",
            "git_branch",
            "base_commit",
            "new_commit",
            "test_result",
            "metrics_before",
            "metrics_after",
            "status",
        ]
        values = [
            tuple(row.get(col) for col in columns)
            for row in rows_to_insert
        ]
        cli.insert("trading.evolution_experiments", values, column_names=columns)
        return len(rows_to_insert)
    except Exception as exc:
        log.error("experiment_insert_failed", error=str(exc), error_type=type(exc).__name__, error_repr=repr(exc))
        return 0


def main():
    cli = get_client()
    runs_dir = Path("runs/ai_trader")
    count = 0
    for path in runs_dir.glob("evolution_experiments.jsonl"):
        count += import_file(cli, path)
    log.info("experiments_imported", total=count)


if __name__ == "__main__":
    main()
