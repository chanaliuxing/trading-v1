#!/usr/bin/env python3
from __future__ import annotations

"""
Feature Eval (P1 minimal)

Evaluates candidate features (ClickHouse view) against baseline OOF results,
computes AUC/IC lifts and coverage, and writes a report for feature success gate.
"""

import argparse
import json
from datetime import date, datetime
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd

try:
    import clickhouse_connect  # type: ignore
except Exception:
    clickhouse_connect = None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalog", default="config/features_catalog.json")
    ap.add_argument("--out", default=None, help="Output dir (defaults to runs/ai_trader/<today>/feature_eval)")
    # Optional gates (can be overridden by config at consumer side)
    ap.add_argument("--max-inter-feature-corr", type=float, default=0.95, help="Max allowed absolute correlation among candidate features")
    ap.add_argument("--leak-diff-thr", type=float, default=0.10, help="Flag leakage if corr(cand, fwd_ret) - corr(cand, ret) exceeds this")
    args = ap.parse_args()

    dstr = date.today().isoformat()
    out_dir = Path(args.out) if args.out else (Path("runs/ai_trader") / dstr / "feature_eval")
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        catalog = json.loads(Path(args.catalog).read_text("utf-8"))
    except Exception:
        catalog = {"version": date.today().isoformat(), "candidate": []}
    version = str(catalog.get("version") or date.today().isoformat())

    # Baseline metrics (must exist): runs/.../oof_eval/oof_eval.json
    runs = Path("runs/ai_trader") / date.today().isoformat()
    base_oof = runs / "oof_eval" / "oof_eval.json"
    if not base_oof.exists():
        # Fallback: skip with ok False
        report = {"ok": False, "version": version, "error": "baseline_missing"}
        (out_dir / f"report_{version}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report))
        return

    base = json.loads(base_oof.read_text("utf-8"))
    base_sym = base.get("params", {}).get("symbols") or []
    base_start = base.get("params", {}).get("start")
    base_end = base.get("params", {}).get("end")
    base_fusion = base.get("fusion") or {}
    base_auc = float(base_fusion.get("auc", 0.0) or 0.0)
    base_ic = float(base_fusion.get("ic", 0.0) or 0.0)

    # Candidate features table/view
    view_name = f"trading.features_1m_candidate_v{version.replace('-', '')}"

    # Evaluate candidate using existing evaluator
    cand_dir = out_dir / f"candidate_v{version.replace('-', '')}"
    cand_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "tools/evaluate_oof_quality.py",
        "--symbols", ",".join(base_sym) if base_sym else "TSLA,AAPL",
        "--start", str(base_start or (date.today().isoformat() + " 09:30:00")),
        "--end", str(base_end or (date.today().isoformat() + " 16:00:00")),
        "--features-table", view_name,
        "--horizon-min", "5",
        "--out", str(cand_dir)
    ]
    p = subprocess.run(cmd, text=True, capture_output=True)

    ok = (p.returncode == 0)
    cand_oof_fp = cand_dir / "oof_eval.json"
    if ok and cand_oof_fp.exists():
        cand = json.loads(cand_oof_fp.read_text("utf-8"))
        cand_fusion = cand.get("fusion") or {}
        cand_auc = float(cand_fusion.get("auc", 0.0) or 0.0)
        cand_ic = float(cand_fusion.get("ic", 0.0) or 0.0)
        # coverage proxy: candidate params n_bars / baseline n_bars
        base_n = float(base.get("params", {}).get("n_bars", 0) or 0)
        cand_n = float(cand.get("params", {}).get("n_bars", 0) or 0)
        coverage = (cand_n / base_n) if base_n > 0 else 0.0
        # Optional: compute redundancy and leakage metrics from candidate view over base window
        max_inter_corr = None
        leakage_flag = False
        try:
            if clickhouse_connect is not None and base_sym and base_start and base_end:
                # Load candidate columns from catalog
                catalog = json.loads(Path(args.catalog).read_text("utf-8"))
                cand_specs: List[dict] = [c for c in (catalog.get("candidate") or []) if isinstance(c, dict) and c.get("name")]
                cand_cols: List[str] = [str(c.get("name")) for c in cand_specs]
                if cand_cols:
                    # Fetch ts,symbol,close and candidate cols for base window
                    from libs.config import config as _cfg  # type: ignore
                    ccfg = _cfg.clickhouse_config if hasattr(_cfg, "clickhouse_config") else {}
                    cli = clickhouse_connect.get_client(
                        host=ccfg.get("host", "localhost"),
                        port=int(ccfg.get("port", 8123)),
                        database=ccfg.get("database", "trading"),
                        username=ccfg.get("user", "default"),
                        password=ccfg.get("password", "")
                    )
                    cols_sql = ", ".join([f"`{c}`" for c in cand_cols])
                    syms = [str(s) for s in base_sym]
                    sql = f"""
                        SELECT ts, symbol, close, {cols_sql}
                        FROM {view_name}
                        WHERE symbol IN %(syms)s AND ts >= toDateTime(%(st)s) AND ts < toDateTime(%(et)s)
                        ORDER BY symbol, ts
                    """
                    rows = cli.query(sql, parameters={"syms": syms, "st": base_start, "et": base_end}).named_results()
                    if rows:
                        df = pd.DataFrame(rows)
                        df["ts"] = pd.to_datetime(df["ts"])
                        # Compute simple ret and fwd_ret per symbol
                        all_frames = []
                        for sym, g in df.groupby("symbol"):
                            g = g.sort_values("ts").reset_index(drop=True)
                            g["ret"] = g["close"].pct_change()
                            g["fwd_ret"] = g["close"].pct_change().shift(-1)
                            all_frames.append(g)
                        dfa = pd.concat(all_frames, ignore_index=True)
                        # Corr matrix among candidate features
                        cand_df = dfa[cand_cols].select_dtypes(include=[float, int]).copy()
                        cand_df = cand_df.replace([float("inf"), float("-inf")], pd.NA).astype(float)
                        cand_df = cand_df.dropna(how="any")
                        if not cand_df.empty and len(cand_cols) > 1:
                            corr = cand_df.corr(method="pearson").abs()
                            # Max off-diagonal correlation
                            upper = corr.where(~pd.np.tril(pd.np.ones(corr.shape)).astype(bool))  # type: ignore
                            max_inter_corr = float(upper.max().max()) if not upper.empty else 0.0
                        else:
                            max_inter_corr = 0.0
                        # Leakage heuristic: cand corr with fwd_ret much bigger than with ret
                        suspicious = False
                        for c in cand_cols:
                            try:
                                s = dfa[[c, "ret", "fwd_ret"]].dropna()
                                if len(s) < 100:
                                    continue
                                c1 = float(s[c].corr(s["ret"]))
                                c2 = float(s[c].corr(s["fwd_ret"]))
                                if (c2 - c1) > float(args.leak_diff_thr) and c2 > 0:
                                    suspicious = True
                                    break
                            except Exception:
                                continue
                        leakage_flag = suspicious
        except Exception:
            max_inter_corr = None
            leakage_flag = False
        report = {
            "ok": True,
            "version": version,
            # Local wall-clock time (aligned with logs)
            "ts": datetime.now().isoformat(),
            "metrics": {
                "auc_lift": round(cand_auc - base_auc, 6),
                "ic_lift": round(cand_ic - base_ic, 6),
                "coverage": round(coverage, 6),
                "leakage": bool(leakage_flag),
                **({"max_inter_feature_corr": float(max_inter_corr)} if max_inter_corr is not None else {})
            },
            "artifacts": [str(cand_oof_fp)]
        }
    else:
        report = {
            "ok": False,
            "version": version,
            "ts": datetime.now().isoformat(),
            "error": "candidate_eval_failed",
            "stderr_tail": (p.stderr or "")[-500:]
        }
    (out_dir / f"report_{version}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"ok": report.get("ok", False), **report.get("metrics", {})}))


if __name__ == "__main__":
    main()
