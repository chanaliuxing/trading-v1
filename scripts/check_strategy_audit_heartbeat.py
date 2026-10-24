#!/usr/bin/env python3
from __future__ import annotations

import argparse
import glob
import json
from datetime import datetime
from pathlib import Path


def _parse_ts(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description="Check latest strategy heartbeat from local audit files.")
    ap.add_argument("--date", required=True, help="Audit date directory (YYYY-MM-DD), e.g. 2026-04-08")
    ap.add_argument("--strategy-id", required=True, help="Strategy id to check")
    ap.add_argument("--max-age-sec", type=float, default=180.0, help="Max allowed heartbeat age in seconds")
    args = ap.parse_args()

    root = Path("runtime") / "audit" / str(args.date)
    files = sorted(glob.glob(str(root / "strategy-*.jsonl")))
    if not files:
        print(f"FAIL no strategy audit files under {root}")
        return 2

    latest_ts: datetime | None = None
    latest_row: dict | None = None
    for fp in files:
        with open(fp, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if str(row.get("strategy_id") or "") != str(args.strategy_id):
                    continue
                if str(row.get("stage") or "") != "feature_callback_heartbeat":
                    continue
                ts = _parse_ts(str(row.get("ts") or ""))
                if ts is None:
                    continue
                if latest_ts is None or ts > latest_ts:
                    latest_ts = ts
                    latest_row = row

    if latest_ts is None:
        print(f"FAIL no heartbeat rows for strategy_id={args.strategy_id}")
        return 3

    now = datetime.now(latest_ts.tzinfo) if latest_ts.tzinfo is not None else datetime.now()
    age_sec = max(0.0, (now - latest_ts).total_seconds())
    status = "PASS" if age_sec <= float(args.max_age_sec) else "FAIL"
    print(
        json.dumps(
            {
                "status": status,
                "strategy_id": str(args.strategy_id),
                "latest_ts": latest_ts.isoformat(),
                "age_sec": round(age_sec, 3),
                "max_age_sec": float(args.max_age_sec),
                "payload": dict((latest_row or {}).get("payload") or {}),
            },
            ensure_ascii=False,
        )
    )
    return 0 if status == "PASS" else 4


if __name__ == "__main__":
    raise SystemExit(main())
