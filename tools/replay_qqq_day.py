#!/usr/bin/env python3
"""
Replay QQQ intraday strategy events for a given ET day from trader.log.

Examples:
  python tools/replay_qqq_day.py --day 2026-02-09
  python tools/replay_qqq_day.py --day 2026-02-09 --full
  python tools/replay_qqq_day.py --day 2026-02-09 --json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional


DEFAULT_LOG_PATH = Path(os.getenv("AUDIT_LOG_PATH", os.path.expanduser("~/grafana-stack/logs/trader.log")))

LINE_RE = re.compile(
    r"^(?P<ts>\S+)\s+\[(?P<level>[^\]]+)\]\s+(?P<event>.*?)\s+\[(?P<logger>[^\]]+)\](?P<tail>.*)$"
)
KV_RE = re.compile(r"([A-Za-z0-9_]+)=([^ ]+)")

DEFAULT_KEYS = (
    "qqq_intraday_initialized",
    "qqq_state_restored",
    "qqq_intraday_daily_reset",
    "qqq_price_base_locked",
    "qqq_price_0945_locked",
    "qqq_amp_locked",
    "qqq_deadwater",
    "qqq_entered",
    "qqq_break_even_armed",
    "qqq_stop_widened",
    "qqq_flatten_submitted",
    "qqq_position_reconciled_live",
    "qqq_position_reconciled_flat",
    "qqq_stop_audit_ok",
    "qqq_stop_order_missing_kill_switch",
    "qqq_intraday_fatal",
    "EMERGENCY_ATR_ACTIVE",
)


@dataclass
class EventRow:
    ts: str
    level: str
    event: str
    logger: str
    strategy_id: str
    symbol: str
    reason: str
    tail: str
    line_no: int


def _parse_line(line: str, line_no: int) -> Optional[EventRow]:
    m = LINE_RE.match(line.rstrip("\n"))
    if not m:
        return None
    ts = m.group("ts")
    level = m.group("level").strip()
    event = m.group("event").strip()
    logger = m.group("logger").strip()
    tail = m.group("tail").strip()

    strategy_id = ""
    symbol = ""
    reason = ""
    for key, value in KV_RE.findall(tail):
        if key == "strategy_id":
            strategy_id = value
        elif key == "symbol":
            symbol = value
        elif key == "reason":
            reason = value

    return EventRow(
        ts=ts,
        level=level,
        event=event,
        logger=logger,
        strategy_id=strategy_id,
        symbol=symbol,
        reason=reason,
        tail=tail,
        line_no=line_no,
    )


def _is_target_day(ts: str, day: str) -> bool:
    return ts.startswith(day)


def _is_qqq_related(row: EventRow) -> bool:
    if "qqq_" in row.event:
        return True
    if row.event == "EMERGENCY_ATR_ACTIVE":
        return True
    if row.strategy_id.startswith("qqq_intraday"):
        return True
    if "strategies.qqq_intraday_strategy" in row.logger:
        return True
    return False


def _iter_rows(path: Path, day: str) -> Iterable[EventRow]:
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for idx, line in enumerate(fh, start=1):
            if not line.startswith(day):
                continue
            row = _parse_line(line, idx)
            if row is None:
                continue
            if _is_qqq_related(row):
                yield row


def _fmt_row(row: EventRow) -> str:
    extra = []
    if row.symbol:
        extra.append(f"symbol={row.symbol}")
    if row.reason:
        extra.append(f"reason={row.reason}")
    if row.strategy_id:
        extra.append(f"strategy_id={row.strategy_id}")
    joined = (" " + " ".join(extra)) if extra else ""
    return f"{row.ts} [{row.level}] {row.event}{joined}"


def _summarize(rows: list[EventRow], day: str, log_path: Path) -> dict:
    counts = Counter(r.event for r in rows)
    levels = Counter(r.level for r in rows)
    key_events = {}
    for key in DEFAULT_KEYS:
        first = next((r for r in rows if r.event == key), None)
        if first:
            key_events[key] = {
                "ts": first.ts,
                "level": first.level,
                "symbol": first.symbol,
                "reason": first.reason,
                "line_no": first.line_no,
            }
    strategies = sorted({r.strategy_id for r in rows if r.strategy_id})
    symbols = sorted({r.symbol for r in rows if r.symbol})

    return {
        "day": day,
        "log_path": str(log_path),
        "matched_rows": len(rows),
        "levels": dict(levels),
        "event_counts": dict(counts.most_common()),
        "key_events_first_seen": key_events,
        "strategy_ids": strategies,
        "symbols": symbols,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Replay QQQ strategy timeline for one day")
    ap.add_argument("--day", required=True, help="ET day in YYYY-MM-DD")
    ap.add_argument("--log-path", default=str(DEFAULT_LOG_PATH), help="Path to trader log file")
    ap.add_argument("--full", action="store_true", help="Print full timeline rows")
    ap.add_argument("--json", action="store_true", help="Output JSON")
    args = ap.parse_args()

    try:
        datetime.strptime(args.day, "%Y-%m-%d")
    except ValueError:
        raise SystemExit("invalid --day, expected YYYY-MM-DD")

    log_path = Path(args.log_path).expanduser()
    if not log_path.exists():
        raise SystemExit(f"log file not found: {log_path}")

    rows = list(_iter_rows(log_path, args.day))
    summary = _summarize(rows, args.day, log_path)

    if args.json:
        payload = {
            "summary": summary,
            "timeline": [asdict(r) for r in rows] if args.full else [],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    print(f"QQQ Day Replay: {args.day}")
    print(f"Log file: {log_path}")
    print(f"Matched rows: {summary['matched_rows']}")
    print("")
    print("Event Counts")
    for event, n in summary["event_counts"].items():
        print(f"- {event}: {n}")
    print("")
    print("Key Checkpoints (first seen)")
    if not summary["key_events_first_seen"]:
        print("- (no key events found)")
    else:
        for key in DEFAULT_KEYS:
            item = summary["key_events_first_seen"].get(key)
            if not item:
                continue
            print(f"- {key}: {item['ts']} [{item['level']}] line={item['line_no']}")
    print("")
    if summary["strategy_ids"]:
        print("Strategy IDs")
        for sid in summary["strategy_ids"]:
            print(f"- {sid}")
        print("")
    if summary["symbols"]:
        print("Symbols")
        for sym in summary["symbols"]:
            print(f"- {sym}")
        print("")
    if args.full:
        print("Timeline")
        for row in rows:
            print(f"- {_fmt_row(row)}")


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        sys.exit(0)
