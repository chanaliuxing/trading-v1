#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from libs.db.ch_pool import get_client


def rebuild_daily_atr_context(
    *,
    symbol: str,
    start_date: str = "2025-09-01",
    out_path: str | Path | None = None,
    limit_days: int = 180,
) -> dict:
    et = ZoneInfo("America/New_York")
    symbol_u = str(symbol or "").strip().upper()
    if not symbol_u:
        raise ValueError("symbol is required")
    output = Path(out_path or f"runtime/state/{symbol_u.lower()}_daily_atr_context.json")
    output.parent.mkdir(parents=True, exist_ok=True)

    client = get_client()
    rows = client.query(
        """
        SELECT
          d,
          argMin(open, ts_start) AS o,
          max(high) AS h,
          min(low) AS l,
          argMax(close, ts_start) AS c
        FROM bars_1m_ibkr
        WHERE symbol = %(symbol)s
          AND d >= toDate(%(start_date)s)
        GROUP BY d
        ORDER BY d ASC
        """,
        {"symbol": symbol_u, "start_date": str(start_date)},
    )

    days: list[dict] = []
    prev_close: float | None = None
    tr_hist: list[float] = []
    atr_hist: list[float | None] = []
    for row in rows:
        day = row["d"].strftime("%Y-%m-%d") if hasattr(row["d"], "strftime") else str(row["d"])
        o = float(row.get("o") or 0.0)
        h = float(row.get("h") or 0.0)
        l = float(row.get("l") or 0.0)
        c = float(row.get("c") or 0.0)
        if min(o, h, l, c) <= 0:
            continue

        base_close = prev_close if prev_close is not None else c
        tr = max(h - l, abs(h - base_close), abs(l - base_close))
        tr = float(max(tr, h - l))
        tr_hist.append(tr)

        atr14: float | None = None
        if len(tr_hist) >= 14:
            if atr_hist and atr_hist[-1] is not None:
                atr14 = (float(atr_hist[-1]) * 13.0 + tr) / 14.0
            else:
                atr14 = sum(tr_hist[-14:]) / 14.0
        atr_hist.append(atr14)

        days.append(
            {
                "day": day,
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "tr": tr,
                "atr14": float(atr14) if atr14 is not None else None,
            }
        )
        prev_close = c

    payload = {
        "symbol": symbol_u,
        "days": days[-max(1, int(limit_days)):],
        "updated_at": datetime.now(et).isoformat(),
        "source": "rebuilt_from_bars_1m_ibkr",
    }
    output.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return {"out_path": str(output), "rows": len(payload["days"]), "symbol": symbol_u}


def main() -> int:
    parser = argparse.ArgumentParser(description="Rebuild daily ATR context JSON from bars_1m_ibkr.")
    parser.add_argument("--symbol", default="QQQ")
    parser.add_argument("--start-date", default="2025-09-01")
    parser.add_argument("--out", default="")
    parser.add_argument("--limit-days", type=int, default=180)
    args = parser.parse_args()

    result = rebuild_daily_atr_context(
        symbol=args.symbol,
        start_date=args.start_date,
        out_path=args.out or None,
        limit_days=args.limit_days,
    )
    print(f"rebuilt {result['out_path']} rows={result['rows']} symbol={result['symbol']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
