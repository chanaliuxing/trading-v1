#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Capture one qqq vs index_super_win shadow comparison sample to disk.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tool.compare_index_super_win_shadow import build_payload


ET = ZoneInfo("America/New_York")


def _default_output_dir() -> Path:
    day = datetime.now(ET).strftime("%Y-%m-%d")
    return ROOT / "runtime" / "state" / "index_super_win_shadow_samples" / day


def _default_qqq_obs_path(strategy_id: str) -> Path:
    return ROOT / "runtime" / "state" / f"qqq_intraday_observability_{strategy_id}.json"


def _default_index_diag_snapshot_path(strategy_id: str) -> Path:
    return ROOT / "runtime" / "state" / f"index_super_win_diag_snapshot_{strategy_id}.json"


def capture_sample(
    *,
    symbol: str,
    qqq_obs_path: Path,
    index_diag_snapshot_path: Path,
    output_dir: Path,
    now: datetime | None = None,
) -> tuple[dict, Path]:
    ts_now = now or datetime.now(ET)
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = build_payload(
        qqq_obs_path=qqq_obs_path,
        index_diag_snapshot_path=index_diag_snapshot_path,
        symbol=symbol,
    )
    payload["captured_at_et"] = ts_now.isoformat()
    payload["captured_day_et"] = ts_now.strftime("%Y-%m-%d")
    output_path = output_dir / f"{str(symbol).strip().upper()}_{ts_now.strftime('%Y%m%dT%H%M%S_%f')}.json"
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload, output_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture one index_super_win shadow comparison sample")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--qqq-strategy-id", default="qqq_intraday_001")
    parser.add_argument("--index-strategy-id", default="index_super_win_001")
    parser.add_argument("--qqq-obs-path", default="")
    parser.add_argument("--index-diag-snapshot-path", default="")
    parser.add_argument("--output-dir", default="")
    args = parser.parse_args()

    qqq_obs_path = Path(args.qqq_obs_path) if str(args.qqq_obs_path).strip() else _default_qqq_obs_path(args.qqq_strategy_id)
    index_diag_path = (
        Path(args.index_diag_snapshot_path)
        if str(args.index_diag_snapshot_path).strip()
        else _default_index_diag_snapshot_path(args.index_strategy_id)
    )
    output_dir = Path(args.output_dir) if str(args.output_dir).strip() else _default_output_dir()
    payload, output_path = capture_sample(
        symbol=str(args.symbol),
        qqq_obs_path=qqq_obs_path,
        index_diag_snapshot_path=index_diag_path,
        output_dir=output_dir,
    )
    print(str(output_path))
    return int(payload["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
