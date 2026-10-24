from __future__ import annotations
from typing import Dict, Any, List, Tuple
from datetime import datetime, date, timedelta

try:
    from libs.db.clickhouse import ClickHouseDB
except Exception:  # pragma: no cover
    ClickHouseDB = None  # type: ignore


def _ch() -> Any:
    if ClickHouseDB is None:
        return None
    try:
        return ClickHouseDB()
    except Exception:
        return None


def atr14(symbol: str, on_date: str | None = None) -> float | None:
    """Approximate intraday ATR14 using 1m bars for the given date (NY session)."""
    d = on_date or date.today().isoformat()
    ch = _ch()
    if ch is None:
        return None
    try:
        sql = f"""
        WITH bars AS (
          SELECT ts_end, close, anyLast(close) OVER (PARTITION BY symbol ORDER BY ts_end ROWS BETWEEN 1 PRECEDING AND 1 PRECEDING) AS prev_close
          FROM trading.bars_1m_current FINAL WHERE toDate(ts_end)=toDate('{d}') AND symbol='{symbol}' ORDER BY ts_end
        )
        SELECT avg(tr) AS atr14 FROM (
          SELECT ts_end, abs(close - ifNull(prev_close, close)) as tr
          FROM bars
          ORDER BY ts_end
          LIMIT 14 BY 1
        )
        """
        rows = ch.query(sql)
        if rows:
            v = rows[0].get("atr14")
            if v is None:
                return None
            try:
                x = float(v)
                return x if x == x else None  # NaN check
            except Exception:
                return None
    except Exception:
        return None
    return None


def rvol20(symbol: str, on_date: str | None = None) -> float | None:
    d = on_date or date.today().isoformat()
    ch = _ch()
    if ch is None:
        return None
    try:
        sql = f"""
        WITH d1 AS (
          SELECT ts_end, volume,
                 avg(volume) OVER (ORDER BY ts_end ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS ma20
          FROM trading.bars_1m_current FINAL WHERE toDate(ts_end)=toDate('{d}') AND symbol='{symbol}' ORDER BY ts_end
        )
        SELECT anyLast(volume/ma20) AS rvol FROM d1
        """
        rows = ch.query(sql)
        if rows:
            v = rows[0].get("rvol")
            if v is None:
                return None
            try:
                x = float(v)
                return x if x == x else None
            except Exception:
                return None
    except Exception:
        return None
    return None


def spread_bps(symbol: str) -> float | None:
    ch = _ch()
    if ch is None:
        return None
    try:
        sql = f"""
        SELECT anyLast(ask - bid) AS sp, anyLast((ask+bid)/2) AS mid
        FROM trading.bbo WHERE symbol='{symbol}' AND ts > now() - INTERVAL 5 MINUTE
        """
        rows = ch.query(sql)
        if rows:
            sp = float(rows[0].get("sp") or 0.0)
            mid = float(rows[0].get("mid") or 0.0)
            if mid > 0:
                return sp / mid * 10000.0
    except Exception:
        return None
    return None


def adv_usd(symbol: str) -> float | None:
    """Approx ADV (20d) in USD using 1m bars aggregated to daily volume * last close."""
    ch = _ch()
    if ch is None:
        return None
    try:
        sql = f"""
        WITH daily AS (
          SELECT toDate(ts_end) AS d, sum(volume) AS vol, anyLast(close) AS last
          FROM trading.bars_1m_current FINAL WHERE symbol='{symbol}' AND ts_end > now() - INTERVAL 30 DAY
          GROUP BY d
          ORDER BY d DESC
          LIMIT 20
        )
        SELECT avg(vol*last) AS adv FROM daily
        """
        rows = ch.query(sql)
        if rows:
            v = rows[0].get("adv")
            if v is None:
                return None
            try:
                x = float(v)
                return x if x == x else None
            except Exception:
                return None
    except Exception:
        return None
    return None


def portfolio_exposure() -> Dict[str, float]:
    """Placeholder: expose basic portfolio exposure metrics (gross, net)."""
    # TODO: wire real portfolio service; return placeholders for now.
    return {"gross": 0.0, "net": 0.0}


def collect_symbol_metrics(symbols: List[str], on_date: str | None = None) -> Dict[str, Dict[str, Any]]:
    """Collect symbol metrics. If on_date is None, use most recent available date from bars_1m_current."""
    # If no date specified, find most recent date with data
    if on_date is None:
        ch = _ch()
        if ch:
            try:
                sql = "SELECT toDate(max(ts_end)) as d FROM trading.bars_1m_current FINAL"
                rows = ch.query(sql)
                if rows:
                    on_date = str(rows[0].get("d"))
            except Exception:
                pass

    out: Dict[str, Dict[str, Any]] = {}
    for s in symbols:
        out[s] = {
            "atr14": atr14(s, on_date=on_date),
            "rvol20": rvol20(s, on_date=on_date),
            "spread_bps": spread_bps(s),
            "adv_usd": adv_usd(s),
        }
    return out


def trade_stats(symbol: str, lookback_days: int = 60) -> Dict[str, Any] | None:
    """Compute simple trade performance stats from trading.pnl_fills.

    Returns: {n, hit_rate, avg_win, avg_loss, pf, ev}
    """
    ch = _ch()
    if ch is None:
        return None
    try:
        sql = f"""
        SELECT
          count() AS n,
          sum(if(realized_pnl>0,1,0)) AS wins,
          avgIf(realized_pnl, realized_pnl>0) AS avg_win,
          abs(avgIf(realized_pnl, realized_pnl<=0)) AS avg_loss,
          sumIf(realized_pnl, realized_pnl>0) AS sum_win,
          abs(sumIf(realized_pnl, realized_pnl<=0)) AS sum_loss
        FROM trading.pnl_fills
        WHERE symbol='{symbol}' AND ts > now() - INTERVAL {int(lookback_days)} DAY
        """
        rows = ch.query(sql)
        if not rows:
            return None
        r = rows[0]
        n = int(r.get("n") or 0)
        wins = float(r.get("wins") or 0)
        hit = wins / n if n > 0 else 0.0
        avg_win = float(r.get("avg_win") or 0.0)
        avg_loss = float(r.get("avg_loss") or 0.0)
        sum_win = float(r.get("sum_win") or 0.0)
        sum_loss = float(r.get("sum_loss") or 0.0)
        pf = (sum_win / max(1e-9, sum_loss)) if sum_loss > 0 else (float("inf") if sum_win>0 else 0.0)
        ev = hit * avg_win - (1 - hit) * avg_loss
        return {"n": n, "hit_rate": hit, "avg_win": avg_win, "avg_loss": avg_loss, "pf": pf, "ev": ev}
    except Exception:
        return None


def collect_trade_stats(symbols: List[str], lookback_days: int = 60) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for s in symbols:
        out[s] = trade_stats(s, lookback_days=lookback_days) or {}
    return out


# --- Risk analytics (VaR/ES/Backtests/Beta) ---

def _fetch_1m_returns(symbol: str, lookback_days: int = 60) -> List[Tuple[str, float]]:
    """Return list of (ts, ret) using 1m bars: r_t = close_t/close_{t-1} - 1."""
    ch = _ch()
    if ch is None:
        return []
    try:
        sql = f"""
        WITH d AS (
          SELECT ts_end, close,
                 anyLast(close) OVER (ORDER BY ts_end ROWS BETWEEN 1 PRECEDING AND 1 PRECEDING) AS prev
          FROM trading.bars_1m
          WHERE symbol='{symbol}' AND ts_end > now() - INTERVAL {int(lookback_days)} DAY
          ORDER BY ts_end
        )
        SELECT ts_end, ifNull(close/NULLIF(prev,0)-1,0) AS ret FROM d WHERE prev IS NOT NULL
        """
        rows = ch.query(sql)
        out: List[Tuple[str,float]] = []
        for r in rows or []:
            try:
                out.append((str(r.get("ts_end")), float(r.get("ret") or 0.0)))
            except Exception:
                continue
        return out
    except Exception:
        return []


def _percentile(values: List[float], q: float) -> float:
    if not values:
        return 0.0
    vs = sorted(values)
    k = (len(vs)-1) * q
    f = int(k)
    c = min(f+1, len(vs)-1)
    if f == c:
        return vs[int(k)]
    return vs[f] + (vs[c]-vs[f]) * (k - f)


def var_es_from_returns(returns: List[float], alpha: float = 0.99) -> Tuple[float, float]:
    """Compute left-tail VaR/ES for returns (negative losses)."""
    if not returns:
        return 0.0, 0.0
    q = 1.0 - alpha
    var = _percentile(returns, q)
    tail = [r for r in returns if r <= var]
    es = sum(tail)/max(1, len(tail)) if tail else var
    return var, es


def kupiec_pvalue(returns: List[float], var_level: float, alpha: float = 0.99) -> float:
    """Kupiec unconditional coverage LR test p-value (approx, chi^2 1df)."""
    import math
    if not returns:
        return 1.0
    breaches = sum(1 for r in returns if r <= var_level)
    n = len(returns)
    pi = breaches / max(1, n)
    p = 1.0 - alpha
    # LRuc = -2 [ ln L(pi) - ln L(p) ]
    def _ll(prob):
        prob = min(max(prob, 1e-12), 1-1e-12)
        return breaches*math.log(prob) + (n-breaches)*math.log(1-prob)
    lruc = -2.0 * (_ll(pi) - _ll(p))
    return math.exp(-lruc/2.0)


def christoffersen_pvalue(returns: List[float], var_level: float) -> float:
    """Christoffersen independence test p-value (approx chi^2 1df)."""
    import math
    if not returns:
        return 1.0
    hits = [1 if r <= var_level else 0 for r in returns]
    if len(hits) < 2:
        return 1.0
    n00=n01=n10=n11=0
    for a,b in zip(hits[:-1], hits[1:]):
        if a==0 and b==0: n00+=1
        elif a==0 and b==1: n01+=1
        elif a==1 and b==0: n10+=1
        else: n11+=1
    pi0 = n01 / max(1, (n00+n01))
    pi1 = n11 / max(1, (n10+n11))
    pi = (n01+n11) / max(1, (n00+n01+n10+n11))
    def _ll(p, x, n):
        p = min(max(p, 1e-12), 1-1e-12)
        return x*math.log(p) + (n-x)*math.log(1-p)
    ll_ind = _ll(pi, n01+n11, n00+n01+n10+n11)
    ll_dep = _ll(pi0, n01, n00+n01) + _ll(pi1, n11, n10+n11)
    lrind = -2.0 * (ll_ind - ll_dep)
    return math.exp(-lrind/2.0)


def beta_vs_spy(symbol: str, lookback_days: int = 60) -> float | None:
    """OLS beta of symbol returns vs SPY returns."""
    if symbol.upper() == 'SPY':
        return 1.0
    sym = _fetch_1m_returns(symbol, lookback_days)
    spy = _fetch_1m_returns('SPY', lookback_days)
    if not sym or not spy:
        return None
    smap = {t:r for t,r in sym}
    x=[]; y=[]
    for t,r in spy:
        if t in smap:
            x.append(r)
            y.append(smap[t])
    if len(x) < 10:
        return None
    n = len(x)
    mx = sum(x)/n
    my = sum(y)/n
    varx = sum((xi-mx)**2 for xi in x)/max(1,(n-1))
    if varx <= 0:
        return None
    cov = sum((xi-mx)*(yi-my) for xi,yi in zip(x,y))/max(1,(n-1))
    return cov/varx


def collect_risk_analytics(symbols: List[str], lookback_days: int = 60, alpha: float = 0.99) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for s in symbols:
        rets = [r for _,r in _fetch_1m_returns(s, lookback_days)]
        var, es = var_es_from_returns(rets, alpha=alpha)
        out[s] = {
            "var": var,
            "es": es,
            "kupiec_p": kupiec_pvalue(rets, var, alpha=alpha),
            "christoffersen_p": christoffersen_pvalue(rets, var),
            "beta_spy": beta_vs_spy(s, lookback_days),
        }
    return out
