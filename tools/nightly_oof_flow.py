#!/usr/bin/env python3
from __future__ import annotations
"""
Nightly OOF Automation Flow (policy-driven)

Loop:
  - Generate OOF (XGB + LSTM) using config/oof_policies.json
  - Evaluate OOF quality (AUC/IC/coverage)
  - If data issue (coverage too low): attempt backfill via tool/load_hist_multi.py, then retry
  - If quality issue: adjust policy (grid over horizon_min/label_bps/drop_flat) and retry
  - If pass: run compare with gating/ensemble policies and choose best; update policies and apply to strategy JSON
  - Persist status and emit an event file runs/ai_trader/YYYY-MM-DD/ai_events.jsonl for AI brain

This tool is JSON-policy driven so AI brain can modify policies and re-run autonomously.
"""
import argparse
import json
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Any, List, Tuple


# Ensure project root is on sys.path for local module imports (libs/*, tools/*)
_PROJ_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJ_ROOT))

RUNS = Path("runs/ai_trader") / date.today().isoformat()


def _get_policy_label(policy_path: str) -> str:
    """读取策略文件，决定使用哪个 label"""
    try:
        p = Path(policy_path)
        if p.exists():
            pol = json.loads(p.read_text("utf-8"))
            # 获取默认 horizon，默认为 5
            h = pol.get("defaults", {}).get("horizon_min", 30)
            return f"label_h{h}"
    except Exception as e:
        print(f"[flow] Warning: failed to read policy horizon: {e}")
    return "label_h5" # fallback


def _run(cmd: List[str], check: bool = True) -> Tuple[int, str, str]:
    p = subprocess.run(cmd, text=True, capture_output=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"cmd failed: {' '.join(cmd)}\n{p.stderr}")
    return p.returncode, p.stdout, p.stderr


def _clamp_to_trading_days(start: str, end: str, symbols: List[str] | None = None, window_days: int = 30) -> Tuple[str, str]:
    """Clamp start/end to last data day (bars) or calendar last trading day; recompute start if needed."""
    from datetime import datetime as _dt, timedelta as _td
    from libs.utils.market_calendar import is_market_closed, last_trading_day, last_data_day

    def _parse_aware(ts: str) -> _dt:
        """Parse ISO string into UTC-aware datetime, preserving offset if present."""
        try:
            # Robust UTC conversion
            import pandas as pd
            return pd.to_datetime(ts, utc=True).to_pydatetime()
        except Exception:
            return _dt.now(timezone.utc)

    # Ensure timezone-aware datetime objects for comparison
    end_dt = _parse_aware(end)
    data_day = None
    try:
        data_day = last_data_day(symbols=symbols)
    except Exception:
        data_day = None
    if data_day and data_day < end_dt.date():
        end_dt = _dt.combine(data_day, _dt.strptime("16:00:00", "%H:%M:%S").time()).replace(tzinfo=timezone.utc)
        print(f"[info] end clamped to last_data_day={data_day}")
    else:
        try:
            if is_market_closed():
                ltd = last_trading_day()
                if ltd < end_dt.date():
                    end_dt = _dt.combine(ltd, _dt.strptime("16:00:00", "%H:%M:%S").time()).replace(tzinfo=timezone.utc)
                    print(f"[info] end clamped to last_trading_day={ltd}")
        except Exception:
            pass
    try:
        start_dt = pd.to_datetime(start, utc=True).to_pydatetime()
    except Exception:
        start_dt = end_dt - _td(days=window_days)
        print(f"[info] start shifted with clamp to {start_dt}")
    return start_dt.strftime("%Y-%m-%d %H:%M:%S"), end_dt.strftime("%Y-%m-%d %H:%M:%S")


def _write_json(p: Path, obj: Dict[str, Any]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")


def _append_event(evt: Dict[str, Any]) -> None:
    p = RUNS / "ai_events.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(evt) + "\n")
    # Also publish to event bus (best-effort, local time to match structlog)
    try:
        from libs.event_bus import publish
        publish('control', {"ts": datetime.now().isoformat(), **evt}, key="ai_trader")
    except Exception:
        pass


def _ml_topic_from_config(default: str = "internal.ml.status") -> str:
    try:
        from libs.config import config
        return ((config.get('event_bus') or {}).get('topics') or {}).get('ml', default)
    except Exception:
        return default


def publish_ml_event(status: str, job_type: str, job_id: str, **kwargs) -> None:
    """Best-effort ML event publisher with JSONL fallback.

    Writes to runs/..../ml_events.jsonl and attempts Kafka via libs.event_bus.publish.
    """
    evt = {
        "event": "ml.job",
        "status": status,
        "job_type": job_type,
        "job_id": job_id,
        # Use local time (America/New_York) to align with structlog logs
        "ts": datetime.now().isoformat(),
        **kwargs,
        # include features table hint if present
        "features_table": kwargs.get("features_table"),
    }
    # propagate ev/beta_corr into top-level for downstream consumers
    metrics = kwargs.get("metrics") or {}
    if "ev" in metrics:
        evt["ev"] = metrics.get("ev")
    if "beta_corr" in metrics:
        evt["beta_corr"] = metrics.get("beta_corr")

    # 1. Publish to Kafka (for control_event_consumer)
    try:
        from libs.event_bus import publish
        topic = _ml_topic_from_config("internal.ml.status")
        publish(topic, evt)
    except Exception as e:
        # Log warning but continue (JSONL is fallback)
        print(f"[WARN] ML event Kafka publish failed: {e}", file=sys.stderr)

    # 2. JSONL audit log (always write)
    p = RUNS / "ml_events.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(evt) + "\n")
    except Exception:
        pass


def _generate_job_id() -> str:
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    import random, string
    rand = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"{ts}-{rand}"


def _emit_completed(job_id: str, start_time: float | None, ok: bool,
                    metrics: Dict[str, Any] | None = None,
                    artifacts: List[str] | None = None,
                    features_table: str | None = None) -> None:
    """Publish a completed event with duration and safe defaults."""
    metrics = metrics or {}
    # Ensure ev/beta_corr keys exist for downstream gates even if evaluation failed/missing.
    if "ev" not in metrics:
        metrics["ev"] = None
    if "beta_corr" not in metrics:
        metrics["beta_corr"] = None
    try:
        duration_sec = (time.time() - start_time) if start_time else 0.0
    except Exception:
        duration_sec = 0.0
    publish_ml_event(
        "completed",
        "oof",
        job_id,
        duration_sec=round(duration_sec, 2),
        metrics=(metrics or {}),
        ev=(metrics or {}).get("ev"),
        beta_corr=(metrics or {}).get("beta_corr"),
        artifacts=(artifacts or []),
        ok=ok,
        features_table=features_table,
    )


def _ch_client():
    try:
        import clickhouse_connect  # type: ignore
    except Exception:
        return None
    try:
        from libs.config import config
        ccfg = config.clickhouse_config
        return clickhouse_connect.get_client(
            host=ccfg.get("host", "localhost"),
            port=int(ccfg.get("port", 8123)),
            database=ccfg.get("database", "trading"),
            username=ccfg.get("user", "default"),
            password=ccfg.get("password", "")
        )
    except Exception:
        try:
            import clickhouse_connect  # type: ignore
            return clickhouse_connect.get_client(host="localhost", port=8123, database="trading")
        except Exception:
            return None


def _resolve_features_table(symbols: List[str], start: str, end: str, override: str | None) -> str:
    """Return features table to use. If override is set but empty for window, fallback to baseline.

    Baseline: trading.features_1m
    """
    baseline = "trading.features_1m"
    if not override:
        return baseline
    cli = _ch_client()
    if cli is None:
        # Cannot verify, best-effort use override
        return override
    try:
        # Check a couple of symbols quickly
        check_syms = symbols[:2] if symbols else []
        if not check_syms:
            return override
        syms = ",".join(f"'%s'" % s for s in check_syms)
        q = f"SELECT count() FROM {override} WHERE symbol IN ({syms}) AND ts >= toDateTime(%(s)s) AND ts < toDateTime(%(e)s) LIMIT 1"
        n = cli.query(q, parameters={"s": start, "e": end}).result_rows[0][0]
        if int(n) > 0:
            return override
        print(f"[flow] Warning: override table {override} empty for window; falling back to {baseline}")
        return baseline
    except Exception as e:
        print(f"[flow] Warning: failed to verify override table {override}: {e}")
        return override


def _eval_ok(metrics: Dict[str, Any], auc_thr: float, ic_thr: float, cov_thr: float, ev_thr: float, beta_corr_thr: float) -> bool:
    try:
        auc = float(metrics.get("auc", 0.0) or 0.0)
        ic = float(metrics.get("ic", 0.0) or 0.0)
        cov = float(metrics.get("coverage", 0.0) or 0.0)
        ev = metrics.get("ev")
        beta_corr = metrics.get("beta_corr")

        if auc < auc_thr:
            return False
        if ic <= ic_thr:
            return False
        if cov < cov_thr:
            return False
        if ev is not None:
            try:
                if float(ev) <= ev_thr:
                    return False
            except Exception:
                pass
        if beta_corr is not None:
            try:
                if float(beta_corr) > beta_corr_thr:
                    return False
            except Exception:
                pass
        return True
    except Exception:
        return False


def _read_brain_hints() -> Dict[str, Any]:
    """Read hints from Brain's latest ML review.

    Returns:
        Dict with keys:
            - window_days: Suggested training window (from extend_window)
            - horizon_values: List of horizon_min values to try (from fix_config)
            - label_bps_values: List of label_bps values to try (from fix_config)
            - is_days_values: List of is_days values to try (from fix_config)
    """
    hints = {
        "window_days": None,
        "horizon_values": [],
        "label_bps_values": [],
        "is_days_values": []
    }

    try:
        # Read latest ml_review.json
        ml_review_file = RUNS / "ml_review.json"
        if not ml_review_file.exists():
            return hints

        review_data = json.loads(ml_review_file.read_text())
        next_actions = review_data.get("review", {}).get("next_actions", [])

        for action in next_actions:
            action_type = action.get("action")
            params = action.get("params", {})

            # Extract extend_window hint
            if action_type == "extend_window":
                window_days = params.get("window_days")
                if window_days and isinstance(window_days, (int, float)):
                    hints["window_days"] = int(window_days)
                    print(f"[hint] Brain suggests extend_window: {window_days} days")

            # Extract fix_config hints for OOF parameters
            elif action_type == "fix_config":
                path = params.get("path", "")
                key = params.get("key", "")
                value = params.get("value")

                if "oof_policies.json" in path and value is not None:
                    if "horizon_min" in key:
                        hints["horizon_values"].append(int(value))
                        print(f"[hint] Brain suggests horizon_min: {value}")
                    elif "label_bps" in key:
                        hints["label_bps_values"].append(int(value))
                        print(f"[hint] Brain suggests label_bps: {value}")
                    elif "is_days" in key:
                        hints["is_days_values"].append(int(value))
                        print(f"[hint] Brain suggests is_days: {value}")

    except Exception as e:
        print(f"[hint] Warning: failed to read Brain hints: {e}")

    return hints


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", required=True, help="Comma-separated symbols")
    ap.add_argument("--start", required=False)
    ap.add_argument("--end", required=False)
    ap.add_argument("--policy-oof", default="config/oof_policies.json")
    ap.add_argument("--policy-gating", default="config/gating_policies.json")
    ap.add_argument("--policy-ensemble", default="config/ensemble_policies.json")
    ap.add_argument("--features-table-override", default=None, help="Override features table for backtests (e.g., trading.features_1m_active)")
    ap.add_argument("--auc-thr", type=float, default=0.52)
    ap.add_argument("--ic-thr", type=float, default=0.02)
    ap.add_argument("--cov-thr", type=float, default=0.60)
    ap.add_argument("--ev-thr", type=float, default=0.0, help="Minimum EV threshold for OOF evaluation")
    ap.add_argument("--beta-corr-thr", type=float, default=0.8, help="Maximum absolute Beta correlation threshold for OOF evaluation")
    ap.add_argument("--max-iters", type=int, default=3)
    ap.add_argument("--apply", action="store_true", help="Apply selected ensemble to strategy JSON (respect autosettings)")
    # Optional knobs (from scheduler or Brain hints)
    ap.add_argument("--window-days", type=int, default=None, help="Training window days for OOF policy (overrides policy defaults.is_days)")
    ap.add_argument("--top-k", type=int, default=None, help="Max candidate grid size per round (hint; may be ignored by generators)")
    ap.add_argument("--quick", action="store_true", help="Enable quick mode (lighter generation/eval)")
    args = ap.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(',') if s.strip()]

    # Compute default start/end with clamp to last data day if not provided
    if not args.start or not args.end:
        today = datetime.now().replace(hour=16, minute=0, second=0, microsecond=0)
        window = int(args.window_days) if args.window_days else 30
        end_dt = today.strftime("%Y-%m-%d %H:%M:%S")
        start_dt = (today - timedelta(days=window)).strftime("%Y-%m-%d %H:%M:%S")
    else:
        end_dt = args.end
        start_dt = args.start
    start_dt, end_dt = _clamp_to_trading_days(start_dt, end_dt, symbols=symbols, window_days=int(args.window_days) if args.window_days else 30)
    args.start, args.end = start_dt, end_dt

    # Pre-flight data quality gate: abort early if coverage/off-RTH is unhealthy (reuse preflight)
    try:
        from scripts.ai_trader.brain.preflight import preflight_check_all
        ready, report = preflight_check_all(
            symbols=symbols,
            horizons=[eval_horizon] if 'eval_horizon' in locals() else [30],
            strict=True,
            lookback_days=int(args.window_days) if args.window_days else 30,
            features_table=args.features_table_override or "trading.features_1m_active",
            labels_table="trading.features_1m_with_labels",
            data_quality_gate=True,
            data_quality_days=3,
            start=args.start,
            end=args.end,
        )
        if not ready:
            print(f"[flow] abort: preflight data quality blocking {report.get('blocking_issues')}")
            sys.exit(1)
    except Exception as e:
        print(f"[flow] abort: preflight data quality gate failed with error: {e}")
        sys.exit(1)

    # Prepare job context up-front so it is available for any failure path
    job_id = _generate_job_id()
    start_time = time.time()

    # Publish ML started event (best-effort)
    try:
        publish_ml_event("started", "oof", job_id, params={"symbols": symbols, "start": args.start, "end": args.end}, features_table=args.features_table_override)
    except Exception as e:
        print(f"[flow] Warning: failed to publish started event: {e}")

    status: Dict[str, Any] = {"symbols": symbols, "start": args.start, "end": args.end, "iters": [], "features_table": args.features_table_override}

    # Read hints from Brain's ML review (if available)
    brain_hints = _read_brain_hints()

    # Build param grid for auto-tune when quality fails
    # Priority: Brain hints > default grid
    # Default horizon grid avoids ultra-short horizons (h5) that are noisy and often sparse;
    # bias toward more stable signals and better label coverage unless Brain overrides.
    grid_h = brain_hints["horizon_values"] if brain_hints["horizon_values"] else [30, 60, 120]
    grid_bps = brain_hints["label_bps_values"] if brain_hints["label_bps_values"] else [10, 5, 15]

    print(f"[flow] Using horizon grid: {grid_h}")
    print(f"[flow] Using label_bps grid: {grid_bps}")

    # Apply window_days hint if available (and clamp start/end using latest data day)
    if brain_hints["window_days"]:
        print(f"[flow] Brain suggests window_days={brain_hints['window_days']}, adjusting start/end with clamp")
        try:
            wd_hint = int(brain_hints["window_days"])
            end_dt = args.end or datetime.now().replace(hour=16, minute=0, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
            start_dt = (datetime.fromisoformat(end_dt) - timedelta(days=wd_hint)).strftime("%Y-%m-%d %H:%M:%S")
            start_dt, end_dt = _clamp_to_trading_days(start_dt, end_dt, symbols=args.symbols, window_days=wd_hint)
            args.start, args.end = start_dt, end_dt
            print(f"[flow] Adjusted window: start={args.start}, end={args.end}")
        except Exception as e:
            print(f"[flow] Warning: failed to apply window_days hint: {e}")

    tried: List[Tuple[int, int]] = []

    # Optionally apply window-days hint to OOF policy before iterations
    if args.window_days:
        try:
            pol_p = Path(args.policy_oof)
            pol = json.loads(pol_p.read_text("utf-8")) if pol_p.exists() else {}
            pol.setdefault("defaults", {})
            # Clamp window in a safe range
            wd = max(21, min(int(args.window_days), 180))
            pol["defaults"]["is_days"] = wd
            _write_json(pol_p, pol)
            print(f"[flow] applied window_days hint to OOF policy: is_days={wd}")
        except Exception as e:
            print(f"[flow] Warning: failed to apply window_days hint: {e}")

    # Also try to read daily hints file produced by Brain actions
    try:
        hints_file = RUNS / "hpo_hints.json"
        if hints_file.exists():
            hints = json.loads(hints_file.read_text("utf-8"))
            wd = int(hints.get("window_days")) if hints.get("window_days") is not None else None
            if wd:
                pol_p = Path(args.policy_oof)
                pol = json.loads(pol_p.read_text("utf-8")) if pol_p.exists() else {}
                pol.setdefault("defaults", {})
                wd = max(21, min(wd, 180))
                pol["defaults"]["is_days"] = wd
                _write_json(pol_p, pol)
                print(f"[flow] applied window_days from hints: is_days={wd}")
    except Exception as e:
        print(f"[flow] Warning: failed to read/apply hpo_hints.json: {e}")

    ok = False
    for it in range(1, args.max_iters + 1):
        # 1) Generate OOF per policies
        print(f"[flow] iter {it}: generate XGB/LSTM OOF")
        # Resolve features table (respect override with fallback)
        ft = _resolve_features_table(symbols, args.start, args.end, args.features_table_override)
        
        # 🟢 动态获取当前策略想要使用的 Label
        target_label = _get_policy_label(args.policy_oof)
        print(f"[flow] enforcing label column: {target_label}")

        gen_common = [
            "--symbols", ",".join(symbols),
            "--start", args.start,
            "--end", args.end,
            "--policy-file", args.policy_oof,
            "--features-table", ft,
            # 🟢 显式传递参数，覆盖底层脚本的默认值
            "--label-col", target_label, 
        ]
        _run([sys.executable, "tools/generate_xgb_oof.py", *gen_common], check=False)
        _run([sys.executable, "tools/generate_lstm_oof.py", *gen_common], check=False)

        # 2) Evaluate quality (aggregate quick summary across symbols)
        print(f"[flow] iter {it}: evaluate OOF quality")
        eval_out = RUNS / "oof_eval" / f"eval_iter_{it}.json"
        # Align evaluation horizon with the enforced label column to avoid
        # mismatched metrics (previously hard-coded to h5).
        eval_horizon = 30
        try:
            if target_label.startswith("label_h"):
                eval_horizon = int(target_label.replace("label_h", ""))
        except Exception:
            eval_horizon = 30

        code, out, err = _run([
            sys.executable,
            "tools/evaluate_oof_quality.py",
            "--symbols", ",".join(symbols),
            "--start", args.start,
            "--end", args.end,
            "--features-table", ft,
            "--horizon-min", str(eval_horizon),
            "--out", str(RUNS / "oof_eval")
        ], check=False)
        # tools/evaluate_oof_quality.py writes per-symbol files; extract fusion metrics for pass/fail check
        result = {"iter": it, "code": code}
        # Heuristic pass if any symbol passes
        per_symbol_ok = {}
        for sym in symbols:
            sym_fp = RUNS / "oof_eval" / f"{sym}.json"
            if not sym_fp.exists():
                continue
            try:
                raw = sym_fp.read_text(encoding="utf-8").strip()
                if not raw:
                    print(f"[flow] Warning: empty OOF eval file for {sym}: {sym_fp}")
                    continue
                sym_data = json.loads(raw)
            except json.JSONDecodeError as e:
                # Treat invalid JSON as "no data" for this symbol instead of crashing entire flow
                print(f"[flow] Warning: invalid JSON in OOF eval file for {sym}: {sym_fp} ({e})")
                continue
            except Exception as e:
                print(f"[flow] Warning: failed to read OOF eval file for {sym}: {sym_fp} ({e})")
                continue
            # Extract fusion model metrics (primary model) and coverage for _eval_ok
            fusion = sym_data.get("fusion", {})
            eval_metrics = {
                "auc": fusion.get("auc"),
                "ic": fusion.get("ic"),
                "coverage": sym_data.get("coverage", 0.0),  # Coverage at symbol level
                "ev": fusion.get("ev") or sym_data.get("ev"),
                "beta_corr": fusion.get("beta_corr") or sym_data.get("beta_corr") or fusion.get("corr_to_spy"),
            }
            passed = _eval_ok(eval_metrics, args.auc_thr, args.ic_thr, args.cov_thr, args.ev_thr, args.beta_corr_thr)
            per_symbol_ok[sym] = {"passed": passed, "metrics": eval_metrics, "full_data": sym_data}
        result["per_symbol_ok"] = per_symbol_ok
        status["iters"].append(result)
        _write_json(RUNS / "oof_flow_status.json", status)

        ok = any(v.get("passed") for v in per_symbol_ok.values())
        # Fallback: if no per-symbol files present, use aggregate oof_eval.json
        if (not ok) and (not per_symbol_ok):
            aggregate_file = RUNS / "oof_eval" / "oof_eval.json"
            if aggregate_file.exists():
                try:
                    agg = json.loads(aggregate_file.read_text())
                    fusion = (agg.get("fusion") or {})
                    n_bars = float((agg.get("params") or {}).get("n_bars", 0) or 0)
                    n_valid = float(fusion.get("n", 0) or 0)
                    cov = (n_valid / n_bars) if n_bars > 0 else 0.0
                    eval_metrics = {
                        "auc": fusion.get("auc"),
                        "ic": fusion.get("ic"),
                        "coverage": cov,
                        "ev": fusion.get("ev") or agg.get("ev"),
                        "beta_corr": fusion.get("beta_corr") or fusion.get("corr_to_spy") or agg.get("beta_corr"),
                    }
                    ok = _eval_ok(eval_metrics, args.auc_thr, args.ic_thr, args.cov_thr, args.ev_thr, args.beta_corr_thr)
                    # Synthesize a single entry keyed by "AGG"
                    per_symbol_ok["AGG"] = {"passed": ok, "metrics": eval_metrics, "full_data": agg}
                    result["per_symbol_ok"] = per_symbol_ok
                    status["iters"][-1] = result
                    _write_json(RUNS / "oof_flow_status.json", status)
                except Exception as e:
                    print(f"[flow] Warning: aggregate fallback parse failed: {e}")
        if ok:
            break

        # 3) Diagnose cause: coverage too low → data backfill; else → policy tweak
        need_backfill = any((v.get("metrics", {}).get("coverage", 0.0) or 0.0) < args.cov_thr for v in per_symbol_ok.values()) if per_symbol_ok else False
        if need_backfill:
            print("[flow] coverage low → attempt backfill 5 days 1m")
            _run([sys.executable, "tool/load_hist_multi.py", "--symbols", ",".join(symbols), "--days", "5", "--bar-size", "1m", "--fill-mode", "backfill"], check=False)
        else:
            # Policy tweak: try next (horizon,label_bps) combo not tried
            print("[flow] quality low → try next policy combo")
            combo = None
            for h in grid_h:
                for b in grid_bps:
                    if (h, b) not in tried:
                        combo = (h, b); break
                if combo: break
            if combo:
                tried.append(combo)
                # Update oof_policies.json defaults.horizon_min/label_bps
                pol_p = Path(args.policy_oof)
                pol = json.loads(pol_p.read_text("utf-8")) if pol_p.exists() else {}
                pol.setdefault("defaults", {})
                pol["defaults"]["horizon_min"] = combo[0]
                pol["defaults"]["label_bps"] = combo[1]
                _write_json(pol_p, pol)
                print(f"[flow] updated policy: horizon_min={combo[0]}, label_bps={combo[1]}")

    if not ok:
        print("[flow] max iterations reached; no passing model")
        _append_event({"ts": str(date.today()), "type": "oof_nopass", "symbols": symbols})
        # Build minimal metrics from aggregate if available and emit a unified completed event
        avg_metrics = {}
        aggregate_file = RUNS / "oof_eval" / "oof_eval.json"
        if aggregate_file.exists():
            try:
                agg_data = json.loads(aggregate_file.read_text())
                for model in ["xgb", "lstm", "fusion"]:
                    if model in agg_data and isinstance(agg_data[model], dict):
                        m = agg_data[model]
                        if "auc" in m:
                            avg_metrics[f"{model}_auc"] = m["auc"]
                        if "ic" in m:
                            avg_metrics[f"{model}_ic"] = m["ic"]
                        if "n" in m:
                            avg_metrics[f"{model}_n"] = m["n"]
                        if model == "fusion":
                            if "ev" in m:
                                avg_metrics["ev"] = m["ev"]
                            if "beta_corr" in m:
                                avg_metrics["beta_corr"] = m["beta_corr"]
                if "params" in agg_data and isinstance(agg_data["params"], dict):
                    p = agg_data["params"]
                    if "horizon_min" in p:
                        avg_metrics["horizon_min"] = p["horizon_min"]
                    if "n_bars" in p:
                        avg_metrics["n_bars"] = p["n_bars"]
            except Exception as e:
                print(f"[flow] Warning: failed to read aggregate OOF results: {e}")
        # Pass features_table from status or args (available in outer scope)
        ft_used = status.get("features_table") or args.features_table_override or "trading.features_1m"
        _emit_completed(job_id, start_time, ok=False, metrics=avg_metrics, artifacts=[str(RUNS / "oof_eval")], features_table=ft_used)
        return

    # 4) Compare and choose ensemble (policy-driven gating applied in compare tool)
    print("[flow] compare ensemble with gating policy")
    cmp_out_dir = RUNS / "ensemble_compare"
    # Backtest compare requires the WITH_OOF view that exposes prediction columns
    ft_bt = "trading.features_1m_with_oof"
    _run([sys.executable, "tools/backtest_ensemble_compare.py", "--symbols", ",".join(symbols), "--start", args.start, "--end", args.end, "--features-table", ft_bt, "--policy-file", args.policy_gating, "--out", str(cmp_out_dir)], check=False)

    # 5) Choose best weights and update/apply
    print("[flow] choose & apply ensemble weights")
    cmp_json = cmp_out_dir / "compare.json"
    if cmp_json.exists():
        _run([sys.executable, "tools/choose_ensemble.py", "--compare", str(cmp_json), "--policy", args.policy_ensemble, "--update-policy"], check=False)
        if args.apply:
            _run([sys.executable, "tools/apply_ensemble_policy.py", "--policy", args.policy_ensemble, "--force"], check=False)
            # Apply gating as well
            _run([sys.executable, "tools/apply_gating_policy.py", "--policy", args.policy_gating, "--force"], check=False)

    # 6) Emit event for AI brain
    _append_event({
        "ts": str(date.today()),
        "type": "oof_complete",
        "symbols": symbols,
        "compare": str(cmp_json),
        "policies": {"oof": args.policy_oof, "gating": args.policy_gating, "ensemble": args.policy_ensemble},
        "applied": bool(args.apply),
    })
    print("[flow] nightly OOF flow complete")

    # Publish ML completed event (unified)
    try:
        # Read actual OOF evaluation results from aggregate file
        avg_metrics = {}
        aggregate_file = RUNS / "oof_eval" / "oof_eval.json"
        if aggregate_file.exists():
            try:
                agg_data = json.loads(aggregate_file.read_text())
                for model in ["xgb", "lstm", "fusion"]:
                    if model in agg_data and isinstance(agg_data[model], dict):
                        m = agg_data[model]
                        if "auc" in m:
                            avg_metrics[f"{model}_auc"] = m["auc"]
                        if "ic" in m:
                            avg_metrics[f"{model}_ic"] = m["ic"]
                        if "brier" in m:
                            avg_metrics[f"{model}_brier"] = m["brier"]
                        if "n" in m:
                            avg_metrics[f"{model}_n"] = m["n"]
                        if model == "fusion":
                            if "ev" in m:
                                avg_metrics["ev"] = m["ev"]
                            if "beta_corr" in m:
                                avg_metrics["beta_corr"] = m["beta_corr"]
                if "params" in agg_data and isinstance(agg_data["params"], dict):
                    p = agg_data["params"]
                    if "horizon_min" in p:
                        avg_metrics["horizon_min"] = p["horizon_min"]
                    if "n_bars" in p:
                        avg_metrics["n_bars"] = p["n_bars"]
            except Exception as e:
                print(f"[flow] Warning: failed to read aggregate OOF results: {e}")
        else:
            print(f"[flow] Warning: aggregate file not found: {aggregate_file}")

        # Read backtest/compare metrics for ensembles
        bt_metrics = {}
        best_mode = None
        if cmp_json.exists():
            try:
                cmp_data = json.loads(cmp_json.read_text())
                for mode in ["xgb_only", "lstm_only", "fusion"]:
                    if isinstance(cmp_data.get(mode), dict):
                        m = cmp_data[mode]
                        bt_metrics[mode] = {
                            k: m.get(k) for k in [
                                "sharpe", "profit_factor", "total_return", "max_drawdown", "win_rate", "trades"
                            ] if k in m
                        }
                def _tuple(mode: str):
                    m = cmp_data.get(mode) or {}
                    return (float(m.get("profit_factor", 0.0)), float(m.get("sharpe", 0.0)), float(m.get("total_return", 0.0)))
                best_mode = sorted(["xgb_only", "lstm_only", "fusion"], key=lambda mo: _tuple(mo), reverse=True)[0]
            except Exception as e:
                print(f"[flow] Warning: failed to read compare metrics: {e}")

        artifacts = [str(RUNS / "oof_eval")]
        if cmp_json.exists():
            artifacts.insert(0, str(cmp_json))
        metrics_payload = {**avg_metrics, **({"bt": bt_metrics, "best_mode": best_mode} if bt_metrics else {})}
        # Pass features_table from status (set in main loop)
        ft_used = status.get("features_table") or args.features_table_override or "trading.features_1m"
        _emit_completed(job_id, start_time, ok=ok, metrics=metrics_payload, artifacts=artifacts, features_table=ft_used)
    except Exception as e:
        print(f"[flow] Warning: failed to publish completed event: {e}")


if __name__ == "__main__":
    # Handle any unexpected exception with an event, using a synthetic job_id if needed
    _fallback_job_id = datetime.now().strftime("%Y%m%d%H%M%S-err")
    _fallback_start = time.time()
    try:
        main()
    except Exception as e:
        try:
            publish_ml_event(
                "failed",
                "oof",
                _fallback_job_id,
                duration_sec=round(time.time() - _fallback_start, 2),
                error_type=type(e).__name__,
                error_message=str(e),
            )
        except Exception:
            pass
        raise
