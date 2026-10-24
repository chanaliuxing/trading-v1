from __future__ import annotations
import json
from pathlib import Path
from typing import Dict, Any, List, Tuple
from datetime import datetime
from typing import Optional
try:
    from libs.db.clickhouse import ClickHouseDB  # optional
except Exception:  # pragma: no cover
    ClickHouseDB = None  # type: ignore


def _is_iso_date(name: str) -> bool:
    try:
        datetime.strptime(name, "%Y-%m-%d")
        return True
    except Exception:
        return False


def _parse_ts(s: str | None) -> float:
    if not s:
        return 0.0
    try:
        # Accept ISO strings
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def summarize_actions(runs_dir: str, days: int = 7, max_files: int = 14) -> Dict[str, Dict[str, Any]]:
    """Summarize recent ml_actions outcomes per action type.

    Returns dict per action: {
      attempts, successes, success_rate, recent_fail_streak
    }
    """
    base = Path(runs_dir)
    if not base.exists():
        return {}
    # List date dirs sorted descending
    dirs = [p for p in base.iterdir() if p.is_dir() and _is_iso_date(p.name)]
    dirs.sort(key=lambda p: p.name, reverse=True)
    if days > 0:
        dirs = dirs[:max_files]

    by_action: Dict[str, List[Dict[str, Any]]] = {}
    for d in dirs:
        fp = d / "ml_actions.jsonl"
        if not fp.exists():
            continue
        try:
            for line in fp.read_text(encoding="utf-8").splitlines():
                try:
                    j = json.loads(line)
                except Exception:
                    continue
                act = str(j.get("action") or "")
                if not act:
                    continue
                status = str(j.get("status") or "")
                ts = _parse_ts(j.get("ts"))
                if act not in by_action:
                    by_action[act] = []
                by_action[act].append({"status": status, "ts": ts})
        except Exception:
            continue

    summary: Dict[str, Dict[str, Any]] = {}
    for act, entries in by_action.items():
        # Sort by timestamp ascending
        entries.sort(key=lambda e: e.get("ts", 0.0))
        attempts = 0
        successes = 0
        recent_outcomes: List[bool] = []
        for e in entries[-200:]:  # bound work
            st = str(e.get("status") or "")
            if st == "skipped":
                continue
            attempts += 1
            ok = (st == "ok")
            if ok:
                successes += 1
            recent_outcomes.append(ok)
        if attempts == 0:
            success_rate = 0.5
        else:
            success_rate = successes / max(1, attempts)
        # Recent fail streak (trailing falses)
        fail_streak = 0
        for v in reversed(recent_outcomes):
            if v:
                break
            fail_streak += 1
        summary[act] = {
            "attempts": attempts,
            "successes": successes,
            "success_rate": success_rate,
            "recent_fail_streak": fail_streak,
        }
    return summary


def summarize_actions_by_symbol(runs_dir: str, days: int = 7, max_files: int = 14) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """Summarize outcomes per (action, symbol) when symbols are present in params.

    Returns dict: {action: {SYMBOL: {attempts, successes, success_rate, recent_fail_streak}}}
    """
    base = Path(runs_dir)
    if not base.exists():
        return {}
    dirs = [p for p in base.iterdir() if p.is_dir() and _is_iso_date(p.name)]
    dirs.sort(key=lambda p: p.name, reverse=True)
    if days > 0:
        dirs = dirs[:max_files]
    by_act_sym: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    for d in dirs:
        fp = d / "ml_actions.jsonl"
        if not fp.exists():
            continue
        try:
            for line in fp.read_text(encoding="utf-8").splitlines():
                try:
                    j = json.loads(line)
                except Exception:
                    continue
                act = str(j.get("action") or "")
                if not act:
                    continue
                status = str(j.get("status") or "")
                ts = _parse_ts(j.get("ts"))
                params = j.get("params") or {}
                syms: List[str] = []
                if isinstance(params, dict):
                    if isinstance(params.get("symbols"), list):
                        syms = [str(s).upper() for s in params.get("symbols") if s]
                    elif isinstance(params.get("symbols"), str):
                        syms = [s.strip().upper() for s in params.get("symbols").split(",") if s.strip()]
                    elif isinstance(params.get("symbol"), str):
                        syms = [str(params.get("symbol")).upper()]
                if not syms:
                    continue
                if act not in by_act_sym:
                    by_act_sym[act] = {}
                for s in syms:
                    by_act_sym[act].setdefault(s, []).append({"status": status, "ts": ts})
        except Exception:
            continue
    summary: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for act, sym_map in by_act_sym.items():
        summary[act] = {}
        for sym, entries in sym_map.items():
            entries.sort(key=lambda e: e.get("ts", 0.0))
            attempts = 0
            successes = 0
            outcomes: List[bool] = []
            for e in entries[-200:]:
                st = str(e.get("status") or "")
                if st == "skipped":
                    continue
                attempts += 1
                ok = (st == "ok")
                if ok:
                    successes += 1
                outcomes.append(ok)
            if attempts == 0:
                sr = 0.5
            else:
                sr = successes / max(1, attempts)
            fs = 0
            for v in reversed(outcomes):
                if v:
                    break
                fs += 1
            summary[act][sym] = {
                "attempts": attempts,
                "successes": successes,
                "success_rate": sr,
                "recent_fail_streak": fs,
            }
    return summary


def summarize_actions_clickhouse(
    table: str,
    lookback_days: int = 7,
    max_rows: int = 10000,
    action_col: str = "action",
    status_col: str = "status",
    ts_col: str = "ts",
    params_col: Optional[str] = "params",
) -> tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Dict[str, Any]]]]:
    """Attempt to summarize per-action and per-(action,symbol) stats from ClickHouse.

    Returns (by_action, by_action_symbol). On error, returns ({}, {}).
    """
    if ClickHouseDB is None:
        return {}, {}
    try:
        ch = ClickHouseDB()
    except Exception:
        return {}, {}
    try:
        # Basic per-action summary
        sql_act = f"""
        SELECT {action_col} AS action,
               countIf({status_col} = 'ok') AS successes,
               countIf({status_col} != 'skipped') AS attempts
        FROM {table}
        WHERE {ts_col} >= now() - INTERVAL {int(max(1, lookback_days))} DAY
        GROUP BY action
        ORDER BY action
        LIMIT {int(max_rows)}
        """
        rows = ch.query(sql_act)
        by_action: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            att = int(r.get("attempts", 0) or 0)
            suc = int(r.get("successes", 0) or 0)
            sr = (suc / att) if att > 0 else 0.5
            by_action[str(r.get("action"))] = {
                "attempts": att,
                "successes": suc,
                "success_rate": sr,
                # fail streak not easily computed in SQL; leave 0 here
                "recent_fail_streak": 0,
            }
        by_action_symbol: Dict[str, Dict[str, Dict[str, Any]]] = {}
        if params_col:
            # Attempt to explode symbols from params JSON (supports array or string)
            sql_sym = f"""
            WITH
            CASE
              WHEN JSONHas({params_col}, 'symbols') THEN arrayMap(x -> upper(toString(x)), JSONExtractArrayRaw({params_col}, 'symbols'))
              WHEN JSONHas({params_col}, 'symbol') THEN [upper(JSONExtractString({params_col}, 'symbol'))]
              ELSE []
            END AS syms
            SELECT {action_col} AS action, arrayJoin(syms) AS symbol,
                   countIf({status_col} = 'ok') AS successes,
                   countIf({status_col} != 'skipped') AS attempts
            FROM {table}
            WHERE {ts_col} >= now() - INTERVAL {int(max(1, lookback_days))} DAY
              AND length(syms) > 0
            GROUP BY action, symbol
            ORDER BY action, symbol
            LIMIT {int(max_rows)}
            """
            rows2 = ch.query(sql_sym)
            for r in rows2:
                act = str(r.get("action"))
                sym = str(r.get("symbol"))
                att = int(r.get("attempts", 0) or 0)
                suc = int(r.get("successes", 0) or 0)
                sr = (suc / att) if att > 0 else 0.5
                by_action_symbol.setdefault(act, {})[sym] = {
                    "attempts": att,
                    "successes": suc,
                    "success_rate": sr,
                    "recent_fail_streak": 0,
                }
        return by_action, by_action_symbol
    except Exception:
        return {}, {}
    finally:
        try:
            ch.close()
        except Exception:
            pass


# ============================================================================
# New functions for metric-based success rate (Brain upgrade)
# ============================================================================

def get_model_success_rate_from_ch(
    symbol: str,
    model_family: str = "",
    horizon: str = "",
    window_days: int = 0,
    lookback_days: int = 30
) -> float:
    """
    Query ClickHouse ml_eval_with_delta to get success rate based on actual
    performance improvement (delta_sharpe, delta_pf, etc.), not just status=ok.

    Returns success_rate in [0.0, 1.0], or 0.5 (neutral) if no data.
    """
    if ClickHouseDB is None:
        return 0.5

    try:
        ch = ClickHouseDB()
    except Exception:
        return 0.5

    try:
        # Build WHERE clause
        where_parts = [f"updated_at >= now() - INTERVAL {int(max(1, lookback_days))} DAY"]
        if symbol:
            where_parts.append(f"symbol = '{symbol}'")
        if model_family:
            where_parts.append(f"model_family = '{model_family}'")
        if horizon:
            where_parts.append(f"horizon = '{horizon}'")
        if window_days > 0:
            where_parts.append(f"window_days = {int(window_days)}")

        where_clause = " AND ".join(where_parts)

        sql = f"""
        SELECT
            count(*) AS attempts,
            sum(is_success::UInt8) AS successes
        FROM trading.ml_eval_with_delta
        WHERE {where_clause}
        """

        rows = ch.query(sql)
        if not rows:
            return 0.5

        attempts = int(rows[0].get("attempts", 0) or 0)
        successes = int(rows[0].get("successes", 0) or 0)

        if attempts == 0:
            return 0.5

        return successes / attempts

    except Exception:
        return 0.5
    finally:
        try:
            ch.close()
        except Exception:
            pass


def get_model_fail_streak_from_ch(
    symbol: str,
    model_family: str = "",
    horizon: str = "",
    window_days: int = 0,
    max_lookback: int = 20
) -> int:
    """
    Query ClickHouse ml_eval_with_delta to get recent consecutive failure streak.

    Returns number of consecutive recent trials where is_success = 0.
    """
    if ClickHouseDB is None:
        return 0

    try:
        ch = ClickHouseDB()
    except Exception:
        return 0

    try:
        # Build WHERE clause
        where_parts = []
        if symbol:
            where_parts.append(f"symbol = '{symbol}'")
        if model_family:
            where_parts.append(f"model_family = '{model_family}'")
        if horizon:
            where_parts.append(f"horizon = '{horizon}'")
        if window_days > 0:
            where_parts.append(f"window_days = {int(window_days)}")

        where_clause = " AND ".join(where_parts) if where_parts else "1=1"

        # Get recent trials ordered by time (most recent first)
        sql = f"""
        SELECT is_success
        FROM trading.ml_eval_with_delta
        WHERE {where_clause}
        ORDER BY updated_at DESC
        LIMIT {int(max_lookback)}
        """

        rows = ch.query(sql)
        if not rows:
            return 0

        # Count consecutive failures from the most recent trial
        fail_streak = 0
        for row in rows:
            is_success = int(row.get("is_success", 0) or 0)
            if is_success == 1:
                break  # Stop at first success
            fail_streak += 1

        return fail_streak

    except Exception:
        return 0
    finally:
        try:
            ch.close()
        except Exception:
            pass


def get_model_stats_summary_from_ch(
    symbol: str = "",
    model_family: str = "",
    horizon: str = "",
    window_days: int = 0
) -> Dict[str, Any]:
    """
    Get comprehensive model performance statistics from ClickHouse.

    Returns dict with:
    - attempts_30d: total trials in last 30 days
    - successes_30d: successful trials
    - success_rate_30d: success rate
    - big_successes_30d: big success count
    - latest_sharpe: most recent Sharpe ratio
    - latest_pf: most recent profit factor
    - fail_streak: consecutive recent failures
    """
    if ClickHouseDB is None:
        return {
            "attempts_30d": 0,
            "successes_30d": 0,
            "success_rate_30d": 0.5,
            "big_successes_30d": 0,
            "latest_sharpe": 0.0,
            "latest_pf": 0.0,
            "fail_streak": 0,
        }

    try:
        ch = ClickHouseDB()
    except Exception:
        return {
            "attempts_30d": 0,
            "successes_30d": 0,
            "success_rate_30d": 0.5,
            "big_successes_30d": 0,
            "latest_sharpe": 0.0,
            "latest_pf": 0.0,
            "fail_streak": 0,
        }

    try:
        # Build WHERE clause for stats query
        where_parts = ["updated_at >= now() - INTERVAL 30 DAY"]
        if symbol:
            where_parts.append(f"symbol = '{symbol}'")
        if model_family:
            where_parts.append(f"model_family = '{model_family}'")
        if horizon:
            where_parts.append(f"horizon = '{horizon}'")
        if window_days > 0:
            where_parts.append(f"window_days = {int(window_days)}")

        where_clause = " AND ".join(where_parts)

        # Query for statistics
        sql = f"""
        SELECT
            count(*) AS attempts_30d,
            sum(is_success::UInt8) AS successes_30d,
            sum(is_big_success::UInt8) AS big_successes_30d,
            argMax(oos_avg_sharpe, updated_at) AS latest_sharpe,
            argMax(oos_avg_profit_factor, updated_at) AS latest_pf
        FROM trading.ml_eval_with_delta
        WHERE {where_clause}
        """

        rows = ch.query(sql)
        if not rows:
            return {
                "attempts_30d": 0,
                "successes_30d": 0,
                "success_rate_30d": 0.5,
                "big_successes_30d": 0,
                "latest_sharpe": 0.0,
                "latest_pf": 0.0,
                "fail_streak": 0,
            }

        row = rows[0]
        attempts = int(row.get("attempts_30d", 0) or 0)
        successes = int(row.get("successes_30d", 0) or 0)
        big_successes = int(row.get("big_successes_30d", 0) or 0)
        latest_sharpe = float(row.get("latest_sharpe", 0.0) or 0.0)
        latest_pf = float(row.get("latest_pf", 0.0) or 0.0)

        success_rate = (successes / attempts) if attempts > 0 else 0.5

        # Get fail streak separately
        fail_streak = get_model_fail_streak_from_ch(symbol, model_family, horizon, window_days)

        return {
            "attempts_30d": attempts,
            "successes_30d": successes,
            "success_rate_30d": success_rate,
            "big_successes_30d": big_successes,
            "latest_sharpe": latest_sharpe,
            "latest_pf": latest_pf,
            "fail_streak": fail_streak,
        }

    except Exception:
        return {
            "attempts_30d": 0,
            "successes_30d": 0,
            "success_rate_30d": 0.5,
            "big_successes_30d": 0,
            "latest_sharpe": 0.0,
            "latest_pf": 0.0,
            "fail_streak": 0,
        }
    finally:
        try:
            ch.close()
        except Exception:
            pass
