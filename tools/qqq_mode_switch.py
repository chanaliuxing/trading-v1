#!/usr/bin/env python3
"""
Quick switch for qqq_intraday_001 run mode in config.yml.

Modes:
- test: all-day test mode (premarket/afterhours/overnight)
- intraday: normal RTH mode
- status: print current mode and key switches
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path


def _replace_bool_line(line: str, key: str, value: bool) -> tuple[str, bool]:
    pat = rf"^(\s*{re.escape(key)}:\s*)(true|false)(\s*)$"
    m = re.match(pat, line)
    if not m:
        return line, False
    v = "true" if value else "false"
    return f"{m.group(1)}{v}{m.group(3)}", True


def _update_config(text: str, *, all_day_test_mode: bool, rth_only: bool) -> tuple[str, dict[str, bool]]:
    lines = text.splitlines()
    changed = {"market_data_service.rth_only": False, "sfs_1s.rth_only": False, "qqq.all_day_test_mode": False}

    in_services = False
    in_mds = False
    in_sfs1s = False
    in_qqq = False
    saw_qqq_mode = False
    qqq_extras_idx = None

    for i, line in enumerate(lines):
        # top-level strategy block start/end
        if line.startswith("- id: "):
            if in_qqq and not line.startswith("- id: qqq_intraday_001"):
                in_qqq = False
            if line.startswith("- id: qqq_intraday_001"):
                in_qqq = True
                saw_qqq_mode = False
                qqq_extras_idx = None

        if line.startswith("services:"):
            in_services = True
            in_mds = False
            in_sfs1s = False
            continue

        if in_services:
            if re.match(r"^[A-Za-z0-9_].*:$", line) and not line.startswith("services:"):
                in_services = False
                in_mds = False
                in_sfs1s = False
            else:
                if line.startswith("  market_data_service:"):
                    in_mds = True
                    in_sfs1s = False
                    continue
                if line.startswith("  sfs_1s:"):
                    in_mds = False
                    in_sfs1s = True
                    continue
                if line.startswith("  ") and not line.startswith("    "):
                    in_mds = False
                    in_sfs1s = False

        if in_mds:
            new_line, ok = _replace_bool_line(line, "rth_only", rth_only)
            if ok:
                lines[i] = new_line
                changed["market_data_service.rth_only"] = True
                continue

        if in_sfs1s:
            new_line, ok = _replace_bool_line(line, "rth_only", rth_only)
            if ok:
                lines[i] = new_line
                changed["sfs_1s.rth_only"] = True
                continue

        if in_qqq:
            if re.match(r"^\s+extras:\s*$", line):
                qqq_extras_idx = i
            new_line, ok = _replace_bool_line(line, "all_day_test_mode", all_day_test_mode)
            if ok:
                lines[i] = new_line
                saw_qqq_mode = True
                changed["qqq.all_day_test_mode"] = True

    if in_qqq and (not saw_qqq_mode) and qqq_extras_idx is not None:
        indent = re.match(r"^(\s*)extras:\s*$", lines[qqq_extras_idx]).group(1)  # type: ignore[union-attr]
        lines.insert(qqq_extras_idx + 1, f"{indent}  all_day_test_mode: {'true' if all_day_test_mode else 'false'}")
        changed["qqq.all_day_test_mode"] = True

    return "\n".join(lines) + "\n", changed


def _read_status(text: str) -> dict[str, str]:
    out = {
        "market_data_service.rth_only": "unknown",
        "sfs_1s.rth_only": "unknown",
        "qqq.all_day_test_mode": "unknown",
    }
    for line in text.splitlines():
        m = re.match(r"^\s*rth_only:\s*(true|false)\s*$", line)
        if m:
            v = m.group(1)
            if out["market_data_service.rth_only"] == "unknown":
                out["market_data_service.rth_only"] = v
            elif out["sfs_1s.rth_only"] == "unknown":
                out["sfs_1s.rth_only"] = v
            continue
        m2 = re.match(r"^\s*all_day_test_mode:\s*(true|false)\s*$", line)
        if m2:
            out["qqq.all_day_test_mode"] = m2.group(1)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Switch QQQ mode quickly")
    ap.add_argument("mode", choices=["test", "intraday", "status"])
    ap.add_argument("--config", default="config.yml")
    args = ap.parse_args()

    path = Path(args.config)
    text = path.read_text(encoding="utf-8")

    if args.mode == "status":
        status = _read_status(text)
        for k, v in status.items():
            print(f"{k}={v}")
        return

    if args.mode == "test":
        new_text, changed = _update_config(text, all_day_test_mode=True, rth_only=False)
    else:
        new_text, changed = _update_config(text, all_day_test_mode=False, rth_only=True)

    path.write_text(new_text, encoding="utf-8")

    status = _read_status(new_text)
    print(f"mode={args.mode}")
    for k, v in status.items():
        print(f"{k}={v}")
    for k, v in changed.items():
        if not v:
            print(f"warn: {k} not updated")


if __name__ == "__main__":
    main()

