import json
import hashlib
from pathlib import Path
from datetime import date, datetime
from zoneinfo import ZoneInfo
from typing import Optional, Dict, Any

NY_TZ = ZoneInfo("America/New_York")

try:
    from libs.db.clickhouse import ClickHouseDB
except Exception:  # pragma: no cover
    ClickHouseDB = None  # type: ignore
try:
    from libs.db.postgres import PostgresDB
    import asyncio
except Exception:  # pragma: no cover
    PostgresDB = None  # type: ignore


CH_TABLE = "ai_trader_memory_episodes"


def _stable_hash(obj: Any) -> str:
    try:
        s = json.dumps(obj, sort_keys=True, separators=(",", ":"))
    except Exception:
        s = str(obj)
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def _read_reflection(runs_dir: str, d: Optional[str] = None) -> Dict[str, Any]:
    d = d or date.today().isoformat()
    fp = Path(runs_dir) / d / "reflection.json"
    if fp.exists():
        try:
            return json.loads(fp.read_text("utf-8"))
        except Exception:
            return {}
    return {}


def _symbols_from_reflection(ref: Dict[str, Any]) -> list:
    details = (ref or {}).get("details") or []
    syms = sorted({str(it.get("symbol")) for it in details if it.get("symbol")})
    return syms


def _providers_from_reflection(ref: Dict[str, Any]) -> list:
    details = (ref or {}).get("details") or []
    provs = sorted({str(it.get("provider")) for it in details if it.get("provider")})
    return provs


def _majority_provider(ref: Dict[str, Any], sym: Optional[str] = None) -> str:
    from collections import Counter
    details = (ref or {}).get("details") or []
    if sym:
        details = [d for d in details if str(d.get("symbol")) == sym]
    c = Counter([str(d.get("provider")) for d in details if d.get("provider")])
    return c.most_common(1)[0][0] if c else ""


def _ch_sector(symbol: str) -> str:
    if ClickHouseDB is None:
        return ""
    ch = ClickHouseDB()
    try:
        rows = ch.query("SELECT sector FROM trading.dim_symbol WHERE symbol={s:String} LIMIT 1", {"s": symbol})
        if rows:
            return rows[0].get("sector") or ""
    except Exception:
        return ""
    return ""


def _ch_regime(target_date: str, symbol: str) -> str:
    """Compute a simple regime label for the day: rvol_{low|mid|high}|trend_{up|down}."""
    if ClickHouseDB is None:
        return ""
    ch = ClickHouseDB()
    try:
        sql = f"""
        SELECT
          avg(rvol20) AS avg_rvol20,
          anyLast(ma20_close) AS ma20_last,
          anyLast(close) AS close_last
        FROM (
          SELECT
            ts_end,
            close,
            avg(close) OVER (ORDER BY ts_end ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS ma20_close,
            volume / nullIf(avg(volume) OVER (ORDER BY ts_end ROWS BETWEEN 19 PRECEDING AND CURRENT ROW), 0) AS rvol20
          FROM trading.bars_1m
          WHERE toDate(ts_end)=toDate('{target_date}') AND symbol='{symbol}'
          ORDER BY ts_end
        )
        """
        rows = ch.query(sql)
        if not rows:
            return ""
        r = rows[0]
        avg_rvol = float(r.get("avg_rvol20") or 0.0)
        ma20_last = float(r.get("ma20_last") or 0.0)
        close_last = float(r.get("close_last") or 0.0)
        if avg_rvol >= 2.0:
            vol_tag = "rvol_high"
        elif avg_rvol >= 1.2:
            vol_tag = "rvol_mid"
        else:
            vol_tag = "rvol_low"
        trend_tag = "trend_up" if (ma20_last and close_last > ma20_last) else "trend_down"
        return f"{vol_tag}|{trend_tag}"
    except Exception:
        return ""


def build_episode(
    runs_dir: str,
    session: str,
    best: Optional[Dict[str, Any]] = None,
    hpo_gate: Optional[Dict[str, Any]] = None,
    notes: Optional[str] = None,
    d: Optional[str] = None,
) -> Dict[str, Any]:
    ref = _read_reflection(runs_dir, d)
    agg = (ref or {}).get("aggregate") or {}
    oos = (best or {}).get("oos") or {}
    params = (best or {}).get("best_params") or {}

    episode = {
        "date": (d or date.today().isoformat()),
        "session": session,
        "provider": "",  # unknown at this stage
        "symbol": "",  # optional; keep blank unless specified later
        "sector": "",
        "regime": "",
        "oos_sharpe": float(oos.get("oos_sharpe", oos.get("sharpe", 0.0)) or 0.0),
        "oos_mdd": float(oos.get("oos_avg_max_drawdown", oos.get("max_drawdown", oos.get("mdd", 0.0))) or 0.0),
        "oos_trades": int(oos.get("oos_trades", oos.get("trades", oos.get("n", 0))) or 0),
        "consistency": float(agg.get("overall_consistency", 0.0) or 0.0),
        "applied": bool((hpo_gate or {}).get("ok", False)),
        "params_id": _stable_hash(params) if params else "",
        "notes": notes or "",
        "created_at": datetime.now(NY_TZ).isoformat(),
    }
    return episode


def write_clickhouse(episode: Dict[str, Any]) -> None:
    if ClickHouseDB is None:
        return
    ch = ClickHouseDB()
    # Create table if not exists (in current database)
    ddl = f"""
    CREATE TABLE IF NOT EXISTS {CH_TABLE} (
        date Date,
        session LowCardinality(String),
        provider LowCardinality(String),
        symbol String,
        sector LowCardinality(String),
        regime LowCardinality(String),
        oos_sharpe Float64,
        oos_mdd Float64,
        oos_trades UInt32,
        consistency Float32,
        applied UInt8,
        params_id String,
        notes String,
        created_at DateTime
    )
    ENGINE = MergeTree
    ORDER BY (date, session, symbol, provider)
    """
    ch.execute(ddl)
    cols = [
        "date",
        "session",
        "provider",
        "symbol",
        "sector",
        "regime",
        "oos_sharpe",
        "oos_mdd",
        "oos_trades",
        "consistency",
        "applied",
        "params_id",
        "notes",
        "created_at",
    ]
    # Convert date string to date object for ClickHouse
    from datetime import datetime as dt
    date_val = episode.get("date")
    if isinstance(date_val, str):
        date_val = dt.strptime(date_val, "%Y-%m-%d").date()

    # Convert created_at ISO string to datetime object
    created_at_val = episode.get("created_at", "")
    if isinstance(created_at_val, str):
        # Parse ISO format with timezone
        created_at_val = dt.fromisoformat(created_at_val.replace("Z", "+00:00"))

    row = [
        date_val,
        episode.get("session"),
        episode.get("provider", ""),
        episode.get("symbol", ""),
        episode.get("sector", ""),
        episode.get("regime", ""),
        float(episode.get("oos_sharpe", 0.0) or 0.0),
        float(episode.get("oos_mdd", 0.0) or 0.0),
        int(episode.get("oos_trades", 0) or 0),
        float(episode.get("consistency", 0.0) or 0.0),
        1 if bool(episode.get("applied", False)) else 0,
        episode.get("params_id", ""),
        episode.get("notes", ""),
        created_at_val,
    ]
    ch.insert(CH_TABLE, [row], column_names=cols)


async def _write_postgres_async(episode: Dict[str, Any]) -> None:
    if PostgresDB is None:
        return
    pg = PostgresDB()
    await pg.connect()
    await pg.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_trader_memory_episodes (
            id BIGSERIAL PRIMARY KEY,
            date DATE,
            session TEXT,
            provider TEXT,
            symbol TEXT,
            sector TEXT,
            regime TEXT,
            oos_sharpe DOUBLE PRECISION,
            oos_mdd DOUBLE PRECISION,
            oos_trades INTEGER,
            consistency REAL,
            applied BOOLEAN,
            params_id TEXT,
            notes TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )
    await pg.execute(
        """
        INSERT INTO ai_trader_memory_episodes
        (date, session, provider, symbol, sector, regime, oos_sharpe, oos_mdd, oos_trades, consistency, applied, params_id, notes, created_at)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
        """,
        episode.get("date"),
        episode.get("session"),
        episode.get("provider", ""),
        episode.get("symbol", ""),
        episode.get("sector", ""),
        episode.get("regime", ""),
        float(episode.get("oos_sharpe", 0.0) or 0.0),
        float(episode.get("oos_mdd", 0.0) or 0.0),
        int(episode.get("oos_trades", 0) or 0),
        float(episode.get("consistency", 0.0) or 0.0),
        bool(episode.get("applied", False)),
        episode.get("params_id", ""),
        episode.get("notes", ""),
        episode.get("created_at"),
    )
    await pg.close()


def write_postgres(episode: Dict[str, Any]) -> None:
    if PostgresDB is None:
        return
    try:
        asyncio.run(_write_postgres_async(episode))
    except Exception:
        # Swallow errors to avoid blocking the pipeline
        pass


def store_episode(
    runs_dir: str,
    session: str,
    best: Optional[Dict[str, Any]] = None,
    hpo_gate: Optional[Dict[str, Any]] = None,
    notes: Optional[str] = None,
    d: Optional[str] = None,
) -> Dict[str, Any]:
    """Build episode from artifacts and persist to ClickHouse.

    PostgreSQL sink can be added similarly when needed.
    """
    ep = build_episode(runs_dir, session, best=best, hpo_gate=hpo_gate, notes=notes, d=d)
    try:
        write_clickhouse(ep)
        ep["saved"] = True
    except Exception as e:  # pragma: no cover
        ep["saved"] = False
        ep["save_error"] = str(e)
    # Best-effort Postgres sink (optional)
    try:
        write_postgres(ep)
    except Exception:
        pass
    # Additionally, write per-symbol variants with sector/regime/provider filled (best-effort)
    try:
        ref = _read_reflection(runs_dir, d)
        syms = _symbols_from_reflection(ref)
        for s in syms:
            ep_s = dict(ep)
            ep_s["symbol"] = s
            ep_s["sector"] = _ch_sector(s)
            ep_s["regime"] = _ch_regime(ep["date"], s)
            ep_s["provider"] = _majority_provider(ref, sym=s)
            write_clickhouse(ep_s)
            write_postgres(ep_s)
    except Exception:
        pass
    return ep
