#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Run repeated index_super_win shadow sampling for one session/window.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tool.build_index_super_win_shadow_session_report import build_report
from tool.capture_index_super_win_shadow_sample import (
    _default_index_diag_snapshot_path,
    _default_output_dir,
    _default_qqq_obs_path,
    capture_sample,
)


ET = ZoneInfo("America/New_York")


def _iter_symbols(raw: str) -> list[str]:
    out: list[str] = []
    for item in str(raw or "").split(","):
        symbol = str(item or "").strip().upper()
        if symbol:
            out.append(symbol)
    return out


def run_session(
    *,
    symbols: Iterable[str],
    qqq_obs_path: Path,
    index_diag_snapshot_path: Path,
    output_dir: Path,
    iterations: int,
    sleep_interval_sec: float,
    clock: callable | None = None,
    sleeper: callable | None = None,
) -> dict:
    now_fn = clock or (lambda: datetime.now(ET))
    sleep_fn = sleeper or time.sleep
    symbols_list = [str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()]
    manifest: dict[str, object] = {
        "started_at_et": now_fn().isoformat(),
        "symbols": list(symbols_list),
        "iterations": int(iterations),
        "sleep_interval_sec": float(sleep_interval_sec),
        "samples": [],
    }
    output_dir.mkdir(parents=True, exist_ok=True)

    for idx in range(max(1, int(iterations))):
        iteration_ts = now_fn()
        for symbol in symbols_list:
            payload, sample_path = capture_sample(
                symbol=symbol,
                qqq_obs_path=qqq_obs_path,
                index_diag_snapshot_path=index_diag_snapshot_path,
                output_dir=output_dir,
                now=iteration_ts,
            )
            manifest["samples"].append(
                {
                    "iteration": idx + 1,
                    "symbol": symbol,
                    "captured_at_et": payload.get("captured_at_et"),
                    "exit_code": int(payload.get("exit_code", 2)),
                    "sample_path": str(sample_path),
                }
            )
        if idx + 1 < int(iterations):
            sleep_fn(max(0.0, float(sleep_interval_sec)))

    manifest["finished_at_et"] = now_fn().isoformat()
    report = build_report(output_dir)
    report_path = output_dir / "session_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    manifest["report_path"] = str(report_path)
    manifest_path = output_dir / "session_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "manifest_path": str(manifest_path),
        "report_path": str(report_path),
        "ready_for_cutin": bool(report.get("ready_for_cutin", False)),
        "sample_count": int(report.get("sample_count", 0)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run repeated shadow sampling for index_super_win")
    parser.add_argument("--symbols", required=True, help="Comma separated symbols, e.g. TQQQ,AAPL")
    parser.add_argument("--iterations", type=int, default=6, help="Number of capture rounds")
    parser.add_argument("--sleep-interval-sec", type=float, default=300.0, help="Sleep between rounds")
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
    result = run_session(
        symbols=_iter_symbols(args.symbols),
        qqq_obs_path=qqq_obs_path,
        index_diag_snapshot_path=index_diag_path,
        output_dir=output_dir,
        iterations=max(1, int(args.iterations)),
        sleep_interval_sec=max(0.0, float(args.sleep_interval_sec)),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if bool(result["ready_for_cutin"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
