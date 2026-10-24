import subprocess
import shlex


class DataGate:
    """Validates recent ticks and yesterday 1m bar completeness in ClickHouse."""

    def __init__(self, ck: dict, policy: dict):
        self.ck = ck or {}
        self.policy = policy or {}

    def _run(self, sql: str) -> str:
        client = self.ck.get("client", "clickhouse-client")
        dsn = self.ck.get("dsn", "").strip()
        cmd = f'{client} {dsn} --query "{sql}"'
        return subprocess.check_output(shlex.split(cmd), text=True)

    def check(self, symbols: list[str] | None = None) -> dict:
        ok = True
        reasons = []

        # 1) Recent ticks in last 10 minutes
        if self.policy.get("last_10min_ticks", True):
            try:
                sql = (
                    "SELECT count() FROM lake.ticks_trades "
                    "WHERE ts >= now() - INTERVAL 10 MINUTE"
                )
                rows = int(self._run(sql).strip() or 0)
                if rows <= 0:
                    ok = False
                    reasons.append("no_recent_ticks")
            except Exception:
                ok = False
                reasons.append("clickhouse_query_error_ticks")

        # 2) Yesterday bars completeness (NY day): symbols must have >= need bars
        need = int(self.policy.get("last_day_1m_bars_ge", 390) or 390)
        try:
            # Compute yesterday in NY timezone to avoid server TZ skew
            import datetime as _dt
            import pytz as _pytz
            ny = _pytz.timezone('America/New_York')
            y = (_dt.datetime.now(ny) - _dt.timedelta(days=1)).date().isoformat()
            sym_filter = ""
            if symbols:
                syms = ",".join(["'" + s.replace("'", "") + "'" for s in symbols])
                sym_filter = f" AND symbol IN ({syms})"
            sql_missing = (
                "WITH bars AS ("
                " SELECT symbol, count() AS c"
                " FROM trading.bars_1m_ibkr"
                " WHERE toDate(toTimeZone(ts_end, 'America/New_York')) = toDate('{day}')"
                f"{sym_filter}"
                " GROUP BY symbol)"
                " SELECT count() FROM bars WHERE c < {need}"
            ).format(day=y, need=need)
            missing = int(self._run(sql_missing).strip() or 0)
            if missing > 0:
                ok = False
                reasons.append(f"bars_1m_yesterday_missing_symbols={missing}")
        except Exception:
            ok = False
            reasons.append("clickhouse_query_error_bars")
        
        return {"ok": ok, "reasons": reasons}
