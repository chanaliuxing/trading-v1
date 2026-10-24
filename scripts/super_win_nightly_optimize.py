#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


ET = ZoneInfo("America/New_York")


def _et_now() -> dt.datetime:
    return dt.datetime.now(ET)


def _is_trading_day(d: dt.date) -> bool:
    if d.weekday() >= 5:
        return False
    try:
        from libs.market_calendar import is_trading_day as _is_td

        return bool(_is_td(d))
    except Exception:
        return d.weekday() < 5


def _recent_weekdays(max_days: int, lookback_days: int, include_today: bool) -> list[str]:
    out: list[str] = []
    today = _et_now().date()
    start = today if include_today else (today - dt.timedelta(days=1))
    for i in range(lookback_days):
        d = start - dt.timedelta(days=i)
        if not _is_trading_day(d):
            continue
        out.append(d.isoformat())
        if len(out) >= max_days:
            break
    return sorted(out)


def _read_backtest_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _extract_trade_pnls(rows: list[dict[str, Any]]) -> list[float]:
    trades = [
        r
        for r in rows
        if str(r.get("exit_reason", "")) != "no_trade"
        and str(r.get("qty", "0")).strip() not in {"", "0"}
    ]
    return [float(t.get("pnl_usd") or 0.0) for t in trades]


def _extract_trade_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        r
        for r in rows
        if str(r.get("exit_reason", "")) != "no_trade"
        and str(r.get("qty", "0")).strip() not in {"", "0"}
    ]


def _data_quality_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    # Penalize synthetic modes; prefer clickhouse/sfs.
    mode_weight = {
        "real_clickhouse": 1.0,
        "real_sfs": 1.0,
        "yfinance_1m": 0.7,
        "clickhouse_unavailable": 0.4,
        "synthetic_from_signal": 0.0,
    }
    modes: dict[str, int] = {}
    for r in rows:
        m = str(r.get("data_mode") or "unknown")
        modes[m] = int(modes.get(m, 0) + 1)

    total = int(sum(modes.values()) or 0)
    if total <= 0:
        return {
            "mode_counts": modes,
            "quality_score": 0.0,
            "synthetic_ratio": 1.0,
            "real_data_ratio": 0.0,
        }

    synthetic_ratio = float(modes.get("synthetic_from_signal", 0) / total)
    real_rows = int(modes.get("real_clickhouse", 0) + modes.get("real_sfs", 0) + modes.get("yfinance_1m", 0))
    real_data_ratio = float(real_rows / total)
    weighted = 0.0
    for m, c in modes.items():
        weighted += float(mode_weight.get(m, 0.3)) * float(c)
    quality_score = float(weighted / total)

    return {
        "mode_counts": modes,
        "quality_score": quality_score,
        "synthetic_ratio": synthetic_ratio,
        "real_data_ratio": real_data_ratio,
    }


def _estimate_round_trip_cost_bps(row: dict[str, Any], commission_bps: float) -> float:
    """Simple trader-oriented execution cost model (round-trip bps)."""
    symbol = str(row.get("symbol") or "").upper()
    entry_ts = str(row.get("entry_ts") or "")
    try:
        ts = dt.datetime.fromisoformat(entry_ts.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=ET)
        ts = ts.astimezone(ET)
    except Exception:
        ts = _et_now()

    hhmm = ts.strftime("%H:%M")
    # Base one-way slippage by session microstructure.
    if "09:30" <= hhmm < "10:00":
        slip_one_way = 5.0
    elif "15:30" <= hhmm <= "16:00":
        slip_one_way = 4.0
    else:
        slip_one_way = 2.0
    # Leveraged ETFs are usually wider.
    if symbol in {"TQQQ", "SQQQ"}:
        slip_one_way += 1.0
    round_trip_slip = 2.0 * slip_one_way
    return float(round_trip_slip + max(0.0, commission_bps))


def _calc_metrics(rows: list[dict[str, Any],], commission_bps: float) -> dict[str, float]:
    trade_rows = _extract_trade_rows(rows)
    pnls = [float(t.get("pnl_usd") or 0.0) for t in trade_rows]
    if not pnls:
        return {
            "trades": 0.0,
            "pnl_gross_total": 0.0,
            "pnl_net_total": 0.0,
            "cost_total_usd": 0.0,
            "win_rate": 0.0,
            "avg_pnl": 0.0,
            "max_drawdown": 0.0,
            "profit_factor": 0.0,
            "score": -1e9,
        }

    pnl_net_seq: list[float] = []
    cost_total_usd = 0.0
    for row, pnl_gross in zip(trade_rows, pnls, strict=True):
        qty = float(row.get("qty") or 0.0)
        entry_px = float(row.get("entry_px") or 0.0)
        notional = abs(qty * entry_px)
        rt_bps = _estimate_round_trip_cost_bps(row=row, commission_bps=commission_bps)
        cost_usd = notional * rt_bps / 10000.0
        cost_total_usd += cost_usd
        pnl_net_seq.append(float(pnl_gross - cost_usd))

    wins = sum(1 for p in pnl_net_seq if p > 0)
    gross_profit = float(sum(p for p in pnl_net_seq if p > 0))
    gross_loss = float(abs(sum(p for p in pnl_net_seq if p < 0)))
    pf = float(gross_profit / gross_loss) if gross_loss > 1e-9 else (999.0 if gross_profit > 0 else 0.0)

    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnl_net_seq:
        cum += p
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd

    pnl_gross_total = float(sum(pnls))
    pnl_net_total = float(sum(pnl_net_seq))
    trades_n = float(len(pnl_net_seq))
    win_rate = float(wins / len(pnl_net_seq)) if pnl_net_seq else 0.0
    avg_pnl = float(pnl_net_total / len(pnl_net_seq)) if pnl_net_seq else 0.0
    # Score now based on net PnL (after costs/slippage).
    score = pnl_net_total - (0.5 * max_dd) + (100.0 * win_rate)
    return {
        "trades": trades_n,
        "pnl_gross_total": pnl_gross_total,
        "pnl_net_total": pnl_net_total,
        "cost_total_usd": float(cost_total_usd),
        "win_rate": win_rate,
        "avg_pnl": avg_pnl,
        "max_drawdown": max_dd,
        "profit_factor": pf,
        "score": score,
    }


def _live_param_patch(best: dict[str, Any]) -> dict[str, Any]:
    model = str(best.get("model") or "baseline")
    notional_usd = float(best.get("notional_usd") or 10000.0)
    model_map = {
        "baseline": {"entry_model": "v47_fast_drift", "elite_decouple_qqew": True},
        "gemini_v44_min": {"entry_model": "v44_elite", "elite_decouple_qqew": False},
        "elite_a_pullback": {"entry_model": "v46_ab", "elite_decouple_qqew": False},
        "elite_b_decoupled": {"entry_model": "v44_elite", "elite_decouple_qqew": True},
    }
    patch = dict(model_map.get(model, model_map["baseline"]))
    patch["notional_usd"] = notional_usd
    return patch


def _check_clickhouse_coverage(days: list[str], symbols: list[str]) -> dict[str, dict[str, int]]:
    try:
        from libs.db.clickhouse import ClickHouseDB
    except Exception:
        return {d: {s: 0 for s in symbols} for d in days}

    ch = ClickHouseDB()
    out: dict[str, dict[str, int]] = {d: {s: 0 for s in symbols} for d in days}

    for day in days:
        # bars_1m_current(ts_start)
        try:
            rows = ch.query(
                """
                SELECT symbol, count() AS c
                FROM trading.bars_1m_current
                WHERE symbol IN {symbols:Array(String)}
                  AND toDate(ts_start) = toDate({d:String})
                GROUP BY symbol
                """,
                parameters={"symbols": symbols, "d": day},
            )
            for r in rows or []:
                s = str(r.get("symbol") or "")
                if s in out[day]:
                    out[day][s] = max(int(out[day][s]), int(r.get("c") or 0))
        except Exception:
            pass

        # bars_1m_ibkr(ts_end)
        try:
            rows = ch.query(
                """
                SELECT symbol, count() AS c
                FROM trading.bars_1m_ibkr
                WHERE symbol IN {symbols:Array(String)}
                  AND toDate(ts_end) = toDate({d:String})
                GROUP BY symbol
                """,
                parameters={"symbols": symbols, "d": day},
            )
            for r in rows or []:
                s = str(r.get("symbol") or "")
                if s in out[day]:
                    out[day][s] = max(int(out[day][s]), int(r.get("c") or 0))
        except Exception:
            pass

    return out


def _missing_coverage(
    coverage: dict[str, dict[str, int]],
    min_bars_per_symbol: int,
    ignore_days: set[str] | None = None,
) -> dict[str, list[str]]:
    miss: dict[str, list[str]] = {}
    ignore = ignore_days or set()
    for day, counts in coverage.items():
        if day in ignore:
            continue
        bad = [s for s, c in counts.items() if int(c) < int(min_bars_per_symbol)]
        if bad:
            miss[day] = bad
    return miss


def _trigger_backfill(
    days_missing: dict[str, list[str]],
    python_bin: str,
    *,
    blocking: bool,
    timeout_sec: int,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    try:
        import os
        env = os.environ.copy()
    except Exception:
        env = {}
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])

    for day, syms in sorted(days_missing.items()):
        if not syms:
            continue
        cmd = [
            python_bin,
            "docs/daily/trigger_backfill.py",
            "--date",
            day,
            "--bar-size",
            "1m",
            "--symbols",
            *syms,
        ]
        if blocking:
            try:
                p = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    env=env,
                    timeout=max(10, int(timeout_sec)),
                )
                events.append(
                    {
                        "day": day,
                        "symbols": syms,
                        "returncode": int(p.returncode),
                        "stdout": (p.stdout or "").strip()[-400:],
                        "stderr": (p.stderr or "").strip()[-400:],
                    }
                )
            except subprocess.TimeoutExpired:
                events.append(
                    {
                        "day": day,
                        "symbols": syms,
                        "returncode": 124,
                        "stdout": "",
                        "stderr": "backfill timeout",
                    }
                )
        else:
            p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
            events.append(
                {
                    "day": day,
                    "symbols": syms,
                    "returncode": None,
                    "pid": int(p.pid),
                    "started_async": True,
                }
            )
    return events


def _ensure_data_readiness(
    *,
    days: list[str],
    symbols: list[str],
    min_bars_per_symbol: int,
    auto_backfill: bool,
    backfill_retries: int,
    backfill_wait_sec: int,
    python_bin: str,
    backfill_blocking: bool,
    backfill_timeout_sec: int,
    proceed_on_partial_data: bool,
) -> dict[str, Any]:
    rounds: list[dict[str, Any]] = []
    ignore_days: set[str] = set()
    # Current trading day may still be in active/sfs pipeline and not yet persisted to ClickHouse 1m table.
    ignore_days.add(_et_now().date().isoformat())

    for idx in range(max(1, backfill_retries + 1)):
        coverage = _check_clickhouse_coverage(days=days, symbols=symbols)
        missing = _missing_coverage(
            coverage=coverage,
            min_bars_per_symbol=min_bars_per_symbol,
            ignore_days=ignore_days,
        )
        rec: dict[str, Any] = {
            "round": idx,
            "coverage": coverage,
            "missing": missing,
            "backfill_triggered": False,
            "backfill_events": [],
        }

        if not missing:
            rec["ready"] = True
            rounds.append(rec)
            return {
                "ready": True,
                "rounds": rounds,
                "final_coverage": coverage,
                "final_missing": {},
            }

        can_backfill = auto_backfill and idx < backfill_retries
        if can_backfill:
            rec["backfill_triggered"] = True
            rec["backfill_events"] = _trigger_backfill(
                days_missing=missing,
                python_bin=python_bin,
                blocking=backfill_blocking,
                timeout_sec=backfill_timeout_sec,
            )
            rounds.append(rec)
            if proceed_on_partial_data and not backfill_blocking:
                return {
                    "ready": False,
                    "rounds": rounds,
                    "final_coverage": coverage,
                    "final_missing": missing,
                    "proceeded_with_partial_data": True,
                }
            time.sleep(max(0, int(backfill_wait_sec)))
            continue

        rec["ready"] = False
        rounds.append(rec)
        return {
            "ready": False,
            "rounds": rounds,
            "final_coverage": coverage,
            "final_missing": missing,
        }

    return {"ready": False, "rounds": rounds, "final_coverage": {}, "final_missing": {}}


def _trial_to_brain_oos(t: dict[str, Any]) -> dict[str, float]:
    pnl_total = float(t.get("pnl_net_total") or 0.0)
    max_dd = float(t.get("max_drawdown") or 0.0)
    trades = int(float(t.get("trades") or 0.0))
    ret_proxy = pnl_total / max(1.0, float(t.get("notional_usd") or 1.0))
    sharpe_proxy = float(t.get("selection_score") or 0.0) / 100.0
    mdd_ratio = float(max_dd / max(1.0, abs(pnl_total))) if pnl_total != 0 else 0.0
    return {
        "oos_sharpe": sharpe_proxy,
        "oos_mdd": mdd_ratio,
        "oos_trades": trades,
        "coverage": float(t.get("real_data_ratio") or 0.0),
        "profit_factor": float(t.get("profit_factor") or 0.0),
        "expectancy_usd": float(t.get("avg_pnl") or 0.0),
        "return": ret_proxy,
    }


def _brain_select_between(top_by_perf: dict[str, Any], top_by_reliability: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "enabled": True,
        "used": False,
        "error": "",
        "decision": {},
        "selected_mode": "policy",
        "selected_trial_id": int(top_by_perf.get("trial_id") or 0),
        "engine": "auto_loop_orchestrator",
    }

    try:
        from scripts.ai_trader.orchestrator import Orchestrator

        o = Orchestrator()
        if not bool(getattr(o, "brain_enabled", False)):
            out["error"] = "orchestrator_brain_disabled"
            out["selected_mode"] = "policy_brain_disabled"
            return out

        ctx = {
            "champion": _trial_to_brain_oos(top_by_reliability),
            "challenger": _trial_to_brain_oos(top_by_perf),
            "oos": _trial_to_brain_oos(top_by_perf),
            "same_candidate": bool(int(top_by_perf.get("trial_id") or 0) == int(top_by_reliability.get("trial_id") or 0)),
        }
        gate_cfg = ((o.cfg.get("policies") or {}).get("hpo_deploy") or {})
        gate = o._hpo_deploy_gate(ctx.get("oos") or {}, gate_cfg)
        raw_decision = o._brain_decide("hpo_deploy", ctx) or {}
        decision = o._validate_hpo_deploy_decision(raw_decision)
        if not bool(gate.get("ok", False)):
            decision["apply_params"] = False
            decision["notes"] = f"{decision.get('notes', '')} | gate_blocked={';'.join(gate.get('reasons', []))}".strip(" |")
        out["decision"] = decision
        out["gate"] = gate
        out["used"] = True
        if bool(decision.get("apply_params", False)):
            out["selected_mode"] = "brain_challenger"
            out["selected_trial_id"] = int(top_by_perf.get("trial_id") or 0)
        else:
            out["selected_mode"] = "brain_champion"
            out["selected_trial_id"] = int(top_by_reliability.get("trial_id") or 0)
        return out
    except Exception as e:
        out["error"] = str(e)
        return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Nightly optimizer for index_super_win backtest.")
    ap.add_argument("--max-days", type=int, default=8, help="How many recent weekdays to test.")
    ap.add_argument("--lookback-days", type=int, default=20, help="Calendar lookback window.")
    ap.add_argument("--include-today", action="store_true", help="Include current ET day if market data already complete.")
    ap.add_argument("--exclude-today", action="store_true", help="Force exclude current ET day from backtest window.")
    ap.add_argument("--python-bin", default=sys.executable, help="Python interpreter for child scripts.")
    ap.add_argument("--strategy-id", default="index_super_win_001")
    ap.add_argument("--apply-live", action="store_true", help="Apply best params to config.yml.")
    ap.add_argument("--use-brain", action="store_true", help="Use ControlBrain to gate final champion/challenger pick.")
    ap.add_argument("--out-root", default="")
    ap.add_argument("--signal-symbol", default="QQQ")
    ap.add_argument("--confirm-symbol", default="QQEW")
    ap.add_argument("--long-exec-symbol", default="TQQQ")
    ap.add_argument("--short-exec-symbol", default="SQQQ")
    ap.add_argument("--auto-backfill", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--min-bars-per-symbol", type=int, default=320)
    ap.add_argument("--backfill-retries", type=int, default=2)
    ap.add_argument("--backfill-wait-sec", type=int, default=25)
    ap.add_argument(
        "--backfill-blocking",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Wait for backfill completion before continuing (default: off, run async and proceed).",
    )
    ap.add_argument("--backfill-timeout-sec", type=int, default=600, help="Timeout per backfill task when blocking.")
    ap.add_argument(
        "--proceed-on-partial-data",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Run backtests on currently available data even when coverage is incomplete.",
    )
    ap.add_argument("--commission-bps", type=float, default=2.0, help="Round-trip explicit commission/fee bps.")
    ap.add_argument(
        "--min-trades",
        type=int,
        default=0,
        help="Minimum number of trades required for a trial to be eligible (0 means auto by tested days).",
    )
    args = ap.parse_args()

    now = _et_now()
    include_today = bool(args.include_today or now.hour >= 16)
    if bool(args.exclude_today):
        include_today = False
    days = _recent_weekdays(
        max_days=max(1, int(args.max_days)),
        lookback_days=max(1, int(args.lookback_days)),
        include_today=include_today,
    )
    if not days:
        print("no trading days selected", file=sys.stderr)
        return 2

    run_day = now.date().isoformat()
    root = Path(args.out_root) if str(args.out_root).strip() else Path("runs/ai_trader") / run_day / "super_win_nightly"
    bt_dir = root / "backtests"
    bt_dir.mkdir(parents=True, exist_ok=True)

    symbols = [
        str(args.signal_symbol).strip().upper(),
        str(args.confirm_symbol).strip().upper(),
        str(args.long_exec_symbol).strip().upper(),
    ]
    short_sym = str(args.short_exec_symbol or "").strip().upper()
    if short_sym:
        symbols.append(short_sym)
    symbols = [s for s in symbols if s]

    readiness = _ensure_data_readiness(
        days=days,
        symbols=symbols,
        min_bars_per_symbol=max(50, int(args.min_bars_per_symbol)),
        auto_backfill=bool(args.auto_backfill),
        backfill_retries=max(0, int(args.backfill_retries)),
        backfill_wait_sec=max(0, int(args.backfill_wait_sec)),
        python_bin=str(args.python_bin),
        backfill_blocking=bool(args.backfill_blocking),
        backfill_timeout_sec=max(10, int(args.backfill_timeout_sec)),
        proceed_on_partial_data=bool(args.proceed_on_partial_data),
    )
    (root / "data_readiness.json").write_text(json.dumps(readiness, ensure_ascii=True, indent=2), encoding="utf-8")
    planned_days = list(days)
    missing_days = set((readiness.get("final_missing") or {}).keys())
    selected_days = [d for d in planned_days if d not in missing_days]
    if bool(args.proceed_on_partial_data) and selected_days:
        days = selected_days
    if not days:
        print("no usable trading days after readiness filter", file=sys.stderr)
        return 2

    models = ["baseline", "gemini_v44_min", "elite_a_pullback", "elite_b_decoupled"]
    notionals = [8000.0, 10000.0, 12000.0]
    baseline_guards = [None, 1.2, 1.5, 2.0]
    baseline_dev_mults = [None, 1.0, 1.25]

    trials: list[dict[str, Any]] = []
    trial_id = 0

    for model in models:
        guard_space = baseline_guards if model == "baseline" else [None]
        dev_space = baseline_dev_mults if model == "baseline" else [None]
        for notional in notionals:
            for guard in guard_space:
                for dev_mult in dev_space:
                    trial_id += 1
                    out_csv = bt_dir / f"trial_{trial_id:03d}_{model}.csv"
                    cmd = [
                        str(args.python_bin),
                        "scripts/backtest_index_super_win.py",
                        "--model",
                        model,
                        "--notional-usd",
                        str(notional),
                        "--signal-symbol",
                        str(args.signal_symbol),
                        "--confirm-symbol",
                        str(args.confirm_symbol),
                        "--long-exec-symbol",
                        str(args.long_exec_symbol),
                        "--short-exec-symbol",
                        str(args.short_exec_symbol),
                        "--out",
                        str(out_csv),
                    ]
                    for d in days:
                        cmd.extend(["--day", d])
                    if guard is not None:
                        cmd.extend(["--counter-guard-mult", str(guard)])
                    if dev_mult is not None:
                        cmd.extend(["--dev-threshold-mult", str(dev_mult)])

                    proc = subprocess.run(cmd, capture_output=True, text=True)
                    if proc.returncode != 0:
                        trials.append(
                            {
                                "trial_id": trial_id,
                                "model": model,
                                "notional_usd": notional,
                                "counter_guard_mult": guard,
                                "dev_threshold_mult": dev_mult,
                                "status": "failed",
                                "error": (proc.stderr or proc.stdout or "").strip()[:400],
                                "trades": 0.0,
                                "pnl_gross_total": 0.0,
                                "pnl_net_total": 0.0,
                                "pnl_total": 0.0,
                                "cost_total_usd": 0.0,
                                "win_rate": 0.0,
                                "avg_pnl": 0.0,
                                "max_drawdown": 0.0,
                                "profit_factor": 0.0,
                                "score": -1e9,
                                "quality_score": 0.0,
                                "synthetic_ratio": 1.0,
                                "real_data_ratio": 0.0,
                                "selection_score": -1e9,
                                "report_csv": str(out_csv),
                            }
                        )
                        continue

                    rows = _read_backtest_rows(out_csv)
                    m = _calc_metrics(rows, commission_bps=float(args.commission_bps))
                    q = _data_quality_stats(rows)
                    selection_score = float(m["score"] + (100.0 * q["quality_score"]) - (200.0 * q["synthetic_ratio"]))
                    trials.append(
                        {
                            "trial_id": trial_id,
                            "model": model,
                            "notional_usd": notional,
                            "counter_guard_mult": guard,
                            "dev_threshold_mult": dev_mult,
                            "status": "ok",
                            "error": "",
                            "trades": m["trades"],
                            "pnl_gross_total": m["pnl_gross_total"],
                            "pnl_net_total": m["pnl_net_total"],
                            "pnl_total": m["pnl_net_total"],
                            "cost_total_usd": m["cost_total_usd"],
                            "win_rate": m["win_rate"],
                            "avg_pnl": m["avg_pnl"],
                            "max_drawdown": m["max_drawdown"],
                            "profit_factor": m["profit_factor"],
                            "score": m["score"],
                            "quality_score": q["quality_score"],
                            "synthetic_ratio": q["synthetic_ratio"],
                            "real_data_ratio": q["real_data_ratio"],
                            "selection_score": selection_score,
                            "mode_counts": q["mode_counts"],
                            "report_csv": str(out_csv),
                        }
                    )

    successful = [t for t in trials if t.get("status") == "ok"]
    if not successful:
        print("all trials failed", file=sys.stderr)
        return 3

    min_trades_required = int(args.min_trades)
    if min_trades_required <= 0:
        min_trades_required = max(2, int(round(len(days) * 0.8)))

    # Hard trader gates:
    # 1) synthetic rows cannot be champion
    # 2) insufficient trade count cannot be champion
    def _f(v: Any, default: float) -> float:
        return default if v is None else float(v)

    eligible = [
        t
        for t in successful
        if _f(t.get("synthetic_ratio"), 1.0) <= 0.0
        and _f(t.get("trades"), 0.0) >= float(min_trades_required)
    ]
    if not eligible:
        # Persist diagnostics even when hard gates block deployment.
        trials_csv = root / "trials_summary.csv"
        cols = [
            "trial_id",
            "model",
            "notional_usd",
            "counter_guard_mult",
            "dev_threshold_mult",
            "status",
            "error",
            "trades",
            "pnl_gross_total",
            "pnl_net_total",
            "pnl_total",
            "cost_total_usd",
            "win_rate",
            "avg_pnl",
            "max_drawdown",
            "profit_factor",
            "score",
            "quality_score",
            "synthetic_ratio",
            "real_data_ratio",
            "selection_score",
            "report_csv",
        ]
        with trials_csv.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for t in trials:
                row = {k: t.get(k, "") for k in cols}
                w.writerow(row)

        reason = f"no eligible trials after hard gates (synthetic_ratio==0 and trades>={min_trades_required})"
        best_json = root / "best_result.json"
        best_json.write_text(
            json.dumps(
                {
                    "generated_at_et": now.isoformat(),
                    "strategy_id": str(args.strategy_id),
                    "days": days,
                    "planned_days": planned_days,
                    "symbols": symbols,
                    "status": "blocked_by_hard_gates",
                    "reason": reason,
                    "selection": {
                        "hard_gates": {
                            "synthetic_disallowed": True,
                            "min_trades_required": int(min_trades_required),
                        },
                        "eligible_trials": 0,
                    },
                    "trials_summary_csv": str(trials_csv),
                    "data_readiness": {
                        "ready": bool(readiness.get("ready", False)),
                        "final_missing": readiness.get("final_missing", {}),
                        "report": str(root / "data_readiness.json"),
                    },
                },
                ensure_ascii=True,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(reason, file=sys.stderr)
        return 4

    # Policy default: prioritize selection_score (performance + data quality).
    best_by_perf = sorted(
        eligible,
        key=lambda x: (float(x.get("selection_score", -1e9)), float(x.get("pnl_net_total", -1e9))),
        reverse=True,
    )[0]
    # Conservative candidate: prioritize real data quality and enough trades.
    best_by_reliability = sorted(
        eligible,
        key=lambda x: (
            float(x.get("real_data_ratio", 0.0)),
            float(x.get("quality_score", 0.0)),
            float(x.get("trades", 0.0)),
            float(x.get("selection_score", -1e9)),
        ),
        reverse=True,
    )[0]

    brain_pick: dict[str, Any] = {
        "enabled": bool(args.use_brain),
        "used": False,
        "error": "",
        "decision": {},
        "selected_mode": "policy",
        "selected_trial_id": int(best_by_perf.get("trial_id") or 0),
    }
    if bool(args.use_brain):
        brain_pick = _brain_select_between(best_by_perf, best_by_reliability)

    selected_trial_id = int(brain_pick.get("selected_trial_id") or int(best_by_perf.get("trial_id") or 0))
    selected = next((t for t in eligible if int(t.get("trial_id") or -1) == selected_trial_id), best_by_perf)

    trials_csv = root / "trials_summary.csv"
    cols = [
        "trial_id",
        "model",
        "notional_usd",
        "counter_guard_mult",
        "dev_threshold_mult",
        "status",
        "error",
        "trades",
        "pnl_gross_total",
        "pnl_net_total",
        "pnl_total",
        "cost_total_usd",
        "win_rate",
        "avg_pnl",
        "max_drawdown",
        "profit_factor",
        "score",
        "quality_score",
        "synthetic_ratio",
        "real_data_ratio",
        "selection_score",
        "report_csv",
    ]
    with trials_csv.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for t in trials:
            row = {k: t.get(k, "") for k in cols}
            w.writerow(row)

    best_live_patch = _live_param_patch(selected)
    best_json = root / "best_result.json"
    best_json.write_text(
        json.dumps(
            {
                "generated_at_et": now.isoformat(),
                "strategy_id": str(args.strategy_id),
                "days": days,
                "planned_days": planned_days,
                "symbols": symbols,
                "data_readiness": {
                    "ready": bool(readiness.get("ready", False)),
                    "final_missing": readiness.get("final_missing", {}),
                    "report": str(root / "data_readiness.json"),
                },
                "selection": {
                    "hard_gates": {
                        "synthetic_disallowed": True,
                        "min_trades_required": int(min_trades_required),
                    },
                    "eligible_trials": int(len(eligible)),
                    "policy_best_trial_id": int(best_by_perf.get("trial_id") or 0),
                    "reliability_best_trial_id": int(best_by_reliability.get("trial_id") or 0),
                    "brain": brain_pick,
                    "selected_trial_id": int(selected.get("trial_id") or 0),
                },
                "best_trial": selected,
                "recommended_live_extras_patch": best_live_patch,
                "trials_summary_csv": str(trials_csv),
            },
            ensure_ascii=True,
            indent=2,
        ),
        encoding="utf-8",
    )

    if args.apply_live:
        params_path = root / "best_params_for_apply.json"
        params_path.write_text(json.dumps(best_live_patch, ensure_ascii=True, indent=2), encoding="utf-8")
        apply_cmd = [
            str(args.python_bin),
            "apply_extras_params.py",
            "--hpo-params",
            str(params_path),
            "--config-yaml",
            "config.yml",
            "--strategy-id",
            str(args.strategy_id),
        ]
        proc = subprocess.run(apply_cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            print(f"[warn] apply failed: {(proc.stderr or proc.stdout or '').strip()}", file=sys.stderr)
        else:
            print("[info] applied best params to config.yml")

    print(str(best_json))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
