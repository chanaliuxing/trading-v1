#!/usr/bin/env python3
"""
Quick regression for data_quality gate + EV/Beta thresholds.

1) Runs data_quality_check for given symbols/days and expects 'healthy' status.
2) Validates _eval_ok logic from nightly_oof_flow with synthetic metrics.

Usage:
    PYTHONPATH=. venv/bin/python tools/test_quality_gates.py --symbols TSLA NVDA
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys

from tools.nightly_oof_flow import _eval_ok


def run_data_quality(symbols, days=3):
    dq_cmd = [sys.executable, "tools/data_quality_check.py", "--symbols", *symbols, "--days", str(days), "--json"]
    out = subprocess.run(dq_cmd, capture_output=True, text=True)
    if not out.stdout:
        raise SystemExit("data_quality_check produced no output")
    # data_quality_check may emit log lines before JSON; use last non-empty line
    lines = [ln for ln in out.stdout.splitlines() if ln.strip()]
    dq = json.loads(lines[-1])
    for item in dq.get("symbols", []):
        if item.get("status") not in ("healthy",):
            raise SystemExit(f"Data quality unhealthy: {dq}")
    print("[ok] data quality healthy", dq)


def run_eval_ok_tests(ev_thr=0.0, beta_thr=0.8):
    # Should pass
    good = {"auc": 0.6, "ic": 0.05, "coverage": 0.7, "ev": 0.1, "beta_corr": 0.5}
    assert _eval_ok(good, 0.52, 0.02, 0.6, ev_thr, beta_thr) is True, "good metrics should pass"
    # Fail EV
    bad_ev = {"auc": 0.6, "ic": 0.05, "coverage": 0.7, "ev": -0.1, "beta_corr": 0.5}
    assert _eval_ok(bad_ev, 0.52, 0.02, 0.6, ev_thr, beta_thr) is False, "negative ev should fail"
    # Fail Beta
    bad_beta = {"auc": 0.6, "ic": 0.05, "coverage": 0.7, "ev": 0.2, "beta_corr": 0.9}
    assert _eval_ok(bad_beta, 0.52, 0.02, 0.6, ev_thr, beta_thr) is False, "high beta should fail"
    # Fail coverage
    bad_cov = {"auc": 0.6, "ic": 0.05, "coverage": 0.5, "ev": 0.2, "beta_corr": 0.5}
    assert _eval_ok(bad_cov, 0.52, 0.02, 0.6, ev_thr, beta_thr) is False, "low coverage should fail"
    print("[ok] _eval_ok gate tests passed")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+", default=["TSLA", "NVDA"])
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--ev-thr", type=float, default=0.0)
    ap.add_argument("--beta-thr", type=float, default=0.8)
    args = ap.parse_args()

    run_data_quality(args.symbols, args.days)
    run_eval_ok_tests(args.ev_thr, args.beta_thr)


if __name__ == "__main__":
    main()
