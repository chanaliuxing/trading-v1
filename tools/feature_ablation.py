#!/usr/bin/env python3
from __future__ import annotations
"""
Feature Ablation (P2 minimal)

Goal: Identify noisy/redundant candidates by testing single-feature removals.

Process per trial (single ablation):
  - Clone catalog and remove one candidate feature
  - Run feature_build.py (VIEW over baseline)
  - Run feature_eval.py (compute auc_lift/ic_lift/coverage/leakage/corr)
  - Record metrics and pick the best removal that improves lifts and passes gates

Outputs:
  - runs/ai_trader/<today>/feature_ablation/ablation_report.json
  - stdout: JSON {ok, tested:int, winner:{removed:str|None, metrics:dict, pass:bool}, suggest_remove:[names]}
"""
import argparse
import json
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Dict, Any, List, Optional


def _run(cmd: List[str]) -> tuple[int, str, str]:
    p = subprocess.run(cmd, text=True, capture_output=True)
    return p.returncode, p.stdout, p.stderr


def _load_catalog(p: Path) -> Dict[str, Any]:
    try:
        return json.loads(p.read_text("utf-8"))
    except Exception:
        return {"version": date.today().isoformat(), "active": [], "candidate": []}


def _write_catalog(p: Path, data: Dict[str, Any]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalog", default="config/features_catalog.json")
    ap.add_argument("--out", default=None, help="Output dir (defaults to runs/ai_trader/<today>/feature_ablation)")
    ap.add_argument("--max-trials", type=int, default=5, help="Max single-feature removals to test")
    # Feature gates (should match consumer defaults for alignment)
    ap.add_argument("--auc-lift-min", type=float, default=0.0)
    ap.add_argument("--ic-lift-min", type=float, default=0.0)
    ap.add_argument("--coverage-min", type=float, default=0.90)
    ap.add_argument("--max-inter-corr", type=float, default=0.95)
    ap.add_argument("--leakage-forbidden", action="store_true")
    args = ap.parse_args()

    runs = Path("runs/ai_trader") / date.today().isoformat()
    out_dir = Path(args.out) if args.out else (runs / "feature_ablation")
    out_dir.mkdir(parents=True, exist_ok=True)
    report_fp = out_dir / "ablation_report.json"

    catalog_path = Path(args.catalog)
    cat = _load_catalog(catalog_path)
    cand: List[dict] = [c for c in (cat.get("candidate") or []) if isinstance(c, dict) and c.get("name")]
    names: List[str] = [str(c.get("name")) for c in cand]

    tested: List[Dict[str, Any]] = []
    winner: Optional[Dict[str, Any]] = None
    suggest_remove: List[str] = []

    # Limit trials
    trial_names = names[: args.max_trials]
    for name in trial_names:
        # Build a temp catalog with this feature removed
        tmp_cat = dict(cat)
        tmp_cat_candidates = [c for c in cand if str(c.get("name")) != name]
        tmp_cat["candidate"] = tmp_cat_candidates
        tmp_cat["version"] = f"{str(cat.get('version') or date.today().isoformat())}-ab_{name}"
        tmp_path = out_dir / f"catalog_ab_{name}.json"
        _write_catalog(tmp_path, tmp_cat)

        # Build and evaluate
        build_rc, build_out, build_err = _run([sys.executable, "tools/feature_build.py", "--catalog", str(tmp_path), "--out", str(out_dir / f"build_{name}")])
        eval_rc, eval_out, eval_err = _run([sys.executable, "tools/feature_eval.py", "--catalog", str(tmp_path), "--out", str(out_dir / f"eval_{name}")])
        ok = (build_rc == 0 and eval_rc == 0)
        res: Dict[str, Any] = {"removed": name, "ok": ok}
        try:
            # Parse eval report
            rep_files = list((out_dir / f"eval_{name}").glob("report_*.json"))
            if rep_files:
                rep = json.loads(rep_files[0].read_text("utf-8"))
                metrics = rep.get("metrics") or {}
                res["metrics"] = metrics
                # Gate pass heuristic (local):
                auc_ok = float(metrics.get("auc_lift", 0.0) or 0.0) >= float(args.auc_lift_min)
                ic_ok = float(metrics.get("ic_lift", 0.0) or 0.0) >= float(args.ic_lift_min)
                cov_ok = float(metrics.get("coverage", 0.0) or 0.0) >= float(args.coverage_min)
                mic = float(metrics.get("max_inter_feature_corr", 0.0) or 0.0)
                corr_ok = mic <= float(args.max_inter_corr)
                leak_ok = not bool(metrics.get("leakage", False)) if args.leakage_forbidden else True
                res["pass"] = bool(auc_ok and ic_ok and cov_ok and corr_ok and leak_ok)
            else:
                res["metrics"] = {}
                res["pass"] = False
        except Exception:
            res["metrics"] = {}
            res["pass"] = False
        tested.append(res)
        # Choose winner by best (auc_lift, ic_lift) lexicographic if pass
        if res["pass"]:
            suggest_remove.append(name)
            if winner is None:
                winner = res
            else:
                m_old = winner.get("metrics") or {}
                m_new = res.get("metrics") or {}
                key_old = (float(m_old.get("auc_lift", 0.0)), float(m_old.get("ic_lift", 0.0)))
                key_new = (float(m_new.get("auc_lift", 0.0)), float(m_new.get("ic_lift", 0.0)))
                if key_new > key_old:
                    winner = res

    report = {
        "ok": bool(winner is not None),
        "tested": len(tested),
        "winner": winner,
        "suggest_remove": suggest_remove,
    }
    report_fp.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"ok": report["ok"], "tested": report["tested"], "winner_removed": (winner or {}).get("removed")}))


if __name__ == "__main__":
    main()

