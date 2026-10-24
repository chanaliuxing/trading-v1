from datetime import date
from typing import Dict, Any, List

try:
    from libs.db.clickhouse import ClickHouseDB
except Exception:  # pragma: no cover
    ClickHouseDB = None  # type: ignore


def _ch() -> Any:
    if ClickHouseDB is None:
        return None
    return ClickHouseDB()


def top_providers(session: str, lookback_days: int = 14, min_trades: int = 10, top_k: int = 3) -> List[Dict[str, Any]]:
    """Return top providers by avg OOS sharpe for a session in recent days."""
    ch = _ch()
    if ch is None:
        return []
    sql = f"""
    SELECT
      provider,
      round(avg(oos_sharpe), 3) AS avg_sharpe,
      round(avg(oos_mdd), 4) AS avg_mdd,
      round(avg(consistency), 3) AS avg_consistency,
      sum(oos_trades) AS trades,
      count() AS episodes
    FROM ai_trader_memory_episodes
    WHERE session = {{session:String}}
      AND date >= today() - INTERVAL {lookback_days} DAY
    GROUP BY provider
    HAVING trades >= {int(min_trades)}
    ORDER BY avg_sharpe DESC
    LIMIT {int(top_k)}
    """
    rows = ch.query(sql, {"session": session})
    return rows


def memory_summary(session: str, lookback_days: int = 14) -> Dict[str, Any]:
    """Produce a lightweight summary for Control Brain context."""
    return {
        "as_of": date.today().isoformat(),
        "session": session,
        "top_providers": top_providers(session, lookback_days=lookback_days),
        "top_by_sector": top_by_sector(session, lookback_days=lookback_days),
        "top_by_regime": top_by_regime(session, lookback_days=lookback_days),
        "top_symbols": top_symbols(session, lookback_days=lookback_days),
    }


def top_by_sector(session: str, lookback_days: int = 14, min_trades: int = 5, top_k_per_sector: int = 2) -> List[Dict[str, Any]]:
    ch = _ch()
    if ch is None:
        return []
    sql = f"""
    SELECT sector, provider,
           round(avg(oos_sharpe),3) AS avg_sharpe,
           sum(oos_trades) AS trades,
           count() AS episodes
    FROM ai_trader_memory_episodes
    WHERE session = {{session:String}}
      AND sector != ''
      AND date >= today() - INTERVAL {lookback_days} DAY
    GROUP BY sector, provider
    HAVING trades >= {int(min_trades)}
    ORDER BY sector, avg_sharpe DESC
    """
    rows = ch.query(sql, {"session": session})
    # take top_k per sector
    out: List[Dict[str, Any]] = []
    cur_sec = None
    cnt = 0
    for r in rows:
        sec = r.get("sector")
        if sec != cur_sec:
            cur_sec = sec
            cnt = 0
        if cnt < top_k_per_sector:
            out.append(r)
            cnt += 1
    return out


def top_by_regime(session: str, lookback_days: int = 14, min_trades: int = 5, top_k_per_regime: int = 2) -> List[Dict[str, Any]]:
    ch = _ch()
    if ch is None:
        return []
    sql = f"""
    SELECT regime, provider,
           round(avg(oos_sharpe),3) AS avg_sharpe,
           sum(oos_trades) AS trades,
           count() AS episodes
    FROM ai_trader_memory_episodes
    WHERE session = {{session:String}}
      AND regime != ''
      AND date >= today() - INTERVAL {lookback_days} DAY
    GROUP BY regime, provider
    HAVING trades >= {int(min_trades)}
    ORDER BY regime, avg_sharpe DESC
    """
    rows = ch.query(sql, {"session": session})
    out: List[Dict[str, Any]] = []
    cur = None
    cnt = 0
    for r in rows:
        key = r.get("regime")
        if key != cur:
            cur = key
            cnt = 0
        if cnt < top_k_per_regime:
            out.append(r)
            cnt += 1
    return out


def top_symbols(session: str, lookback_days: int = 14, min_trades: int = 5, top_k: int = 10) -> List[Dict[str, Any]]:
    ch = _ch()
    if ch is None:
        return []
    sql = f"""
    SELECT symbol, anyLast(sector) AS sector, anyLast(regime) AS regime, provider,
           round(avg(oos_sharpe),3) AS avg_sharpe,
           sum(oos_trades) AS trades,
           count() AS episodes
    FROM ai_trader_memory_episodes
    WHERE session = {{session:String}}
      AND symbol != ''
      AND date >= today() - INTERVAL {lookback_days} DAY
    GROUP BY symbol, provider
    HAVING trades >= {int(min_trades)}
    ORDER BY avg_sharpe DESC
    LIMIT {int(top_k)}
    """
    return ch.query(sql, {"session": session})
