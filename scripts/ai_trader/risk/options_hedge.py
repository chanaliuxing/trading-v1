from __future__ import annotations
import json
from pathlib import Path
from datetime import date
from typing import Dict, Any

try:
    from libs.db.clickhouse import ClickHouseDB
except Exception:  # pragma: no cover
    ClickHouseDB = None  # type: ignore


def _portfolio_exposure() -> Dict[str, float]:
    """Best-effort gross/net exposure from ClickHouse; fallback zeros."""
    if ClickHouseDB is None:
        return {"gross": 0.0, "net": 0.0}
    ch = ClickHouseDB()
    try:
        sql = """
        SELECT
          sum(abs(pos_qty*mark_px)) AS gross,
          sum(pos_qty*mark_px) AS net
        FROM pnl_pos_5s
        WHERE ts_end > now() - INTERVAL 10 MINUTE
        """
        rows = ch.query(sql)
        if rows:
            return {"gross": float(rows[0].get("gross") or 0.0), "net": float(rows[0].get("net") or 0.0)}
    except Exception:
        pass
    return {"gross": 0.0, "net": 0.0}


def propose_options_hedge(runs_dir: str, intent: str = "vol_spike") -> Dict[str, Any]:
    """Propose a minimal options hedge plan (placeholder).

    Strategy: For positive net exposure, propose SPY puts notional ~ 30% of gross exposure.
    For negative net exposure, propose SPY calls similarly.
    Writes options_hedge.json under runs/YYYY-MM-DD.
    """
    exp = _portfolio_exposure()
    gross = float(exp.get("gross") or 0.0)
    net = float(exp.get("net") or 0.0)
    if gross <= 0.0:
        plan = {"need_hedge": False, "reason": "no_exposure"}
    else:
        notional = 0.3 * gross
        direction = "PUT" if net > 0 else "CALL"
        plan = {
            "need_hedge": True,
            "instrument": "SPY",
            "option_type": direction,
            "target_notional_usd": round(notional, 2),
            "intent": intent,
            "assumptions": {"delta_abs": 0.5},
        }
    outd = Path(runs_dir) / date.today().isoformat()
    outd.mkdir(parents=True, exist_ok=True)
    (outd / "options_hedge.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
    return plan

