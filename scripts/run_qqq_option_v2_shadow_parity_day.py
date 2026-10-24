#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.reports.option_live_parity_report import generate_option_live_parity_report


def _next_day(day: str) -> str:
    return (date.fromisoformat(day) + timedelta(days=1)).isoformat()


def main() -> None:
    ap = argparse.ArgumentParser(description="Run replay for a day and generate legacy/v2 live-shadow parity report.")
    ap.add_argument("--date", required=True, help="ET trading day, e.g. 2026-03-25")
    ap.add_argument("--strategy-id", default="qqq_1dte_ma10_reject_0324_v2_001")
    ap.add_argument("--symbol", default="QQQ")
    ap.add_argument("--strategy", default="ma10_reject_0324")
    ap.add_argument("--snapshot-root", default="runtime/state/decision_snapshots_live_v2")
    ap.add_argument("--audit-root", default="runtime/audit")
    ap.add_argument("--replay-root", default="runtime/state/live_shadow_replay")
    ap.add_argument("--quote-mode", default="real", choices=["real", "synthetic", "auto"])
    ap.add_argument("--session-mode", default="extended", choices=["rth", "extended"])
    ap.add_argument("--config-json", default=None, help="Optional replay config override JSON")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    replay_dir = Path(args.replay_root).expanduser() / args.strategy_id / args.date
    replay_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "backtest_spy_option_0324.py"),
        "--start",
        args.date,
        "--end",
        _next_day(args.date),
        "--symbol",
        args.symbol,
        "--strategy",
        args.strategy,
        "--quote-mode",
        args.quote_mode,
        "--out-dir",
        str(replay_dir),
    ]
    if args.config_json:
        cmd.extend(["--config-json", args.config_json])

    # Session mode is controlled via ReplayConfig; use a temporary inline JSON when needed.
    if args.session_mode != "extended" and not args.config_json:
        tmp_cfg = replay_dir / "_shadow_parity_cfg.json"
        tmp_cfg.write_text(json.dumps({"market_data_session": args.session_mode}, ensure_ascii=False), encoding="utf-8")
        cmd.extend(["--config-json", str(tmp_cfg)])

    subprocess.run(cmd, cwd=str(ROOT), check=True)

    report = generate_option_live_parity_report(
        strategy_id=args.strategy_id,
        report_date=args.date,
        snapshot_root=args.snapshot_root,
        audit_root=args.audit_root,
        replay_dir=replay_dir,
    )
    payload = report.to_dict()
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    if args.out:
        out = Path(args.out).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)


if __name__ == "__main__":
    main()
