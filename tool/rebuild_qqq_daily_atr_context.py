#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from libs.db.ch_pool import get_client


def main() -> int:
    et = ZoneInfo("America/New_York")
    out = Path("runtime/state/qqq_daily_atr_context.json")
    out.parent.mkdir(parents=True, exist_ok=True)

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
        WHERE symbol = 'QQQ'
          AND d >= toDate('2025-09-01')
        GROUP BY d
        ORDER BY d ASC
        """
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
        "days": days[-180:],
        "updated_at": datetime.now(et).isoformat(),
        "source": "rebuilt_from_bars_1m_ibkr",
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"rebuilt {out} rows={len(payload['days'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
