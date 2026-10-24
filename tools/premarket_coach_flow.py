#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Premarket Coach Flow

Runs health checks, generates strategy coach suggestions + report, and
optionally applies approved changes (with autosettings guard).

Steps:
1) Health checks (Redis 1s + CH freshness) and print summary
2) Generate coach report Markdown
3) Optional apply suggestions (only if health checks pass and flags allow)
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from datetime import datetime


def _run(cmd: list[str]) -> tuple[int, str, str]:
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out, err = p.communicate()
    return p.returncode, out, err


def main():
    ap = argparse.ArgumentParser(description="Premarket coach flow")
    ap.add_argument("--apply", action="store_true", help="Apply coach suggestions if health checks pass")
    ap.add_argument("--force", action="store_true", help="Bypass require_approval on apply")
    ap.add_argument("--report-md", default=None, help="Coach report output path")
    ap.add_argument("--symbols", nargs="*", default=None)
    args = ap.parse_args()

    today = datetime.now().date().isoformat()
    report_md = args.report_md or str(Path("runs/ai_trader") / today / "coach_report.md")

    # 1) Health checks
    print("== Health: LLM Trader realtime ==")
    code, out, err = _run(["python", "tools/check_llm_trader_realtime.py", "--json"])
    llm_ok = False
    if code == 0:
        try:
            llm_ok = bool(json.loads(out).get("ok"))
        except Exception:
            pass
    print(out)

    print("\n== Health: Strategy CH freshness ==")
    code, out, err = _run(["python", "tools/check_clickhouse_strategy_freshness.py", "--stale-sec", "180", "--json"])
    ch_ok = False
    ch_ratio = 0.0
    if code == 0:
        try:
            ch_payload = json.loads(out)
            ch_ok = bool(ch_payload.get("ok"))
            # Compute ratio of ok items across all symbol/table entries
            total = 0
            good = 0
            for sym, per_table in (ch_payload.get("results") or {}).items():
                for tname, entry in (per_table or {}).items():
                    total += 1
                    if entry.get("ok"):
                        good += 1
            if total > 0:
                ch_ratio = good / total
        except Exception:
            pass
    print(out)

    # 2) Generate coach report
    print("\n== Coach: generating suggestions report ==")
    coach_cmd = ["python", "tools/strategy_coach.py", "--report-md", report_md]
    if args.symbols:
        coach_cmd.extend(["--symbols", *args.symbols])
    code, out, err = _run(coach_cmd)
    print(out)
    print(f"Report: {report_md}")

    # 3) Optional apply
    if args.apply:
        # Require LLMT ok and CH ok ratio >= 0.8
        if not llm_ok or ch_ratio < 0.8:
            print(f"\nApply aborted: health checks not OK (llm_ok={llm_ok}, ch_ok_ratio={ch_ratio:.2f}). Use --force on coach if you want to override at your own risk.")
            return
        print("\n== Coach: applying suggestions ==")
        apply_cmd = ["python", "tools/strategy_coach.py", "--apply"]
        if args.force:
            apply_cmd.append("--force")
        if args.symbols:
            apply_cmd.extend(["--symbols", *args.symbols])
        code, out, err = _run(apply_cmd)
        print(out)


if __name__ == "__main__":
    main()
