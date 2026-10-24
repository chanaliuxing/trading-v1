#!/usr/bin/env python3
from __future__ import annotations

"""
Simulate AI Brain actions from a professional quant perspective.

The script fabricates review outcomes for common failure modes (data gaps,
label drift, feature collapse) and drives control_event_consumer._execute_ml_actions
with safe stubs so we can verify that each action path uses the correct tools.

Usage:
  python scripts/test_brain_action_flow.py            # run all scenarios
  python scripts/test_brain_action_flow.py --list     # list scenarios
  python scripts/test_brain_action_flow.py --scenario data_gaps --scenario feature_flat
"""

import argparse
import asyncio
import json
import os
import sys
from collections import defaultdict
from contextlib import ExitStack, contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List

os.environ.setdefault("ML_BRAIN_LOOP", "1")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.ai_trader.control_event_consumer import _execute_ml_actions


DEFAULT_RUNS_DIR = PROJECT_ROOT / "runs" / "ai_trader"


def _ensure_runs_dir() -> Path:
    DEFAULT_RUNS_DIR.mkdir(parents=True, exist_ok=True)
    return DEFAULT_RUNS_DIR


# ----------------------------
# Scenario definitions
# ----------------------------
SCENARIOS: Dict[str, Dict[str, Any]] = {
    "data_gaps": {
        "title": "Data quality swings → DQ tooling + recompute/enqueue ladder",
        "cycles": [
            {
                "name": "coverage_drop",
                "summary": "TSLA lost ~25% of bars intraday; expect data_quality_check and automatic feature recompute.",
                "review_result": {
                    "summary": {"oos_sharpe": 0.05, "coverage": 0.62},
                    "diagnosis": [
                        "Coverage collapsed to 62% after 11:20-11:45 outage",
                        "Need tape validation before re-running models",
                    ],
                    "action_rationale": "Missing bars render IC unlabeled; request DQ tooling with auto-remediation.",
                    "next_actions": [
                        {
                            "action": "data_quality_check",
                            "params": {"symbols": ["TSLA", "AAPL"], "min_coverage": 0.98},
                            "confidence": 0.83,
                        }
                    ],
                },
                "context": {
                    "dq_report": {
                        "start": "2025-11-18T09:30:00Z",
                        "end": "2025-11-18T16:00:00Z",
                        "min_coverage": 0.98,
                        "symbols": [
                            {"symbol": "TSLA", "coverage": 0.74},
                            {"symbol": "AAPL", "coverage": 0.992},
                        ],
                    }
                },
            },
            {
                "name": "coverage_recovered",
                "summary": "After recompute, coverage back above 99%; expect DQ check to trigger auto HPO enqueue.",
                "review_result": {
                    "summary": {"oos_sharpe": 0.08, "coverage": 0.97},
                    "diagnosis": [
                        "Manual patch uploaded new files; verifying that coverage is green before resuming HPO",
                    ],
                    "action_rationale": "Double-check tape, then if all green schedule alpha_loop automatically.",
                    "next_actions": [
                        {
                            "action": "data_quality_check",
                            "params": {"symbols": ["TSLA", "AAPL"], "min_coverage": 0.98},
                            "confidence": 0.72,
                        }
                    ],
                },
                "context": {
                    "dq_report": {
                        "start": "2025-11-18T09:30:00Z",
                        "end": "2025-11-18T16:00:00Z",
                        "min_coverage": 0.98,
                        "symbols": [
                            {"symbol": "TSLA", "coverage": 0.995},
                            {"symbol": "AAPL", "coverage": 0.991},
                        ],
                    }
                },
            },
        ],
    },
    "label_drift": {
        "title": "Label horizon mismatch → config patch + targeted HPO",
        "cycles": [
            {
                "name": "widen_label_window",
                "summary": "PF fell below 0.8 with many zero-label bars; broaden horizon via ml_policy patch.",
                "review_result": {
                    "summary": {"oos_sharpe": -0.32, "trades": 15},
                    "diagnosis": [
                        "Zero_frac exceeds 0.92 because labels are too short for the current volatility regime",
                    ],
                    "action_rationale": "Raise label.horizon_sec to 900s before re-running HPO.",
                    "next_actions": [
                        {
                            "action": "fix_config",
                            "params": {"path": "ml_policy.json", "key": "label.horizon_sec", "value": 900},
                            "confidence": 0.77,
                        }
                    ],
                },
            },
            {
                "name": "rehpo_after_label_change",
                "summary": "After fixing horizon, kick off hpo_cli to rebuild the stack.",
                "review_result": {
                    "summary": {"oos_sharpe": 0.0, "trades": 15},
                    "diagnosis": [
                        "Need fresh hpo_cli run with updated labels to recalibrate gating",
                    ],
                    "action_rationale": "High quality data restored, now run narrow HPO search.",
                    "next_actions": [
                        {
                            "action": "queue_job",
                            "params": {
                                "job_type": "hpo_cli",
                                "params": {"symbols": ["TSLA"], "n_trials": 32, "timeout_hours": 1.2},
                            },
                            "confidence": 0.78,
                        }
                    ],
                },
            },
        ],
    },
    "feature_flat": {
        "title": "Structural feature change → Feature catalog DSL enforcement",
        "cycles": [
            {
                "name": "propose_catalog_specs",
                "summary": "AUC stuck at 0.5; propose catalog-compliant specs and ensure feature_build is queued.",
                "review_result": {
                    "summary": {"oos_sharpe": -0.1, "ic": 0.002},
                    "diagnosis": [
                        "Existing candidate set is flat; need causal, catalog-managed features",
                    ],
                    "action_rationale": "Propose safe DSL specs (short/mid momentum + liquidity zscore) via catalog.",
                    "next_actions": [
                        {
                            "action": "propose_features",
                            "params": {
                                "version": "v2025-11-18",
                                "specs": [
                                    {
                                        "name": "mom_ratio_3_20",
                                        "expr": "avg(f.ret_1) OVER (PARTITION BY f.symbol ORDER BY f.ts ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) /\n"
                                                "        nullIf(avg(f.ret_1) OVER (PARTITION BY f.symbol ORDER BY f.ts ROWS BETWEEN 19 PRECEDING AND CURRENT ROW), 0)",
                                        "dtype": "Float64",
                                        "notes": "Short vs medium momentum strength ratio",
                                    },
                                    {
                                        "name": "volume_gap_zscore",
                                        "expr": "(f.volume - avg(f.volume) OVER (PARTITION BY f.symbol ORDER BY f.ts ROWS BETWEEN 29 PRECEDING AND CURRENT ROW)) /\n"
                                                "        nullIf(stddevPop(f.volume) OVER (PARTITION BY f.symbol ORDER BY f.ts ROWS BETWEEN 29 PRECEDING AND CURRENT ROW), 0)",
                                        "dtype": "Float64",
                                        "notes": "Rolling 30-bar liquidity shock detector",
                                    },
                                ],
                            },
                            "confidence": 0.88,
                        }
                    ],
                },
            }
        ],
    },
    "multi_freq": {
        "title": "Cross-frequency catalog specs → ensure extra sources and joins work",
        "cycles": [
            {
                "name": "mf_catalog",
                "summary": "Use 5m + daily aliases to craft catalog spec with required_sources.",
                "review_result": {
                    "summary": {"oos_sharpe": -0.2, "ic": 0.0},
                    "diagnosis": [
                        "Single-frequency ratios exhausted; need regime detector combining 5m EMA with yesterday RVOL",
                    ],
                    "action_rationale": "Catalog can now reference m5/d aliases; propose guarded multi-frequency spec.",
                    "next_actions": [
                        {
                            "action": "propose_features",
                            "params": {
                                "version": "v2025-11-18-mf",
                                "specs": [
                                    {
                                        "name": "mf_ema_gap",
                                        "expr": "(m5.ema_close_20 - f.close) / nullIf(abs(d.rvol20) + 1e-6, 0)",
                                        "dtype": "Float64",
                                        "notes": "5m EMA vs spot normalized by prev-day RVOL",
                                        "required_sources": ["m5", "d"],
                                    }
                                ],
                            },
                            "confidence": 0.9,
                        }
                    ],
                },
            }
        ],
    },
    "label_fix": {
        "title": "Degenerate labels → queue label_recompute job",
        "cycles": [
            {
                "name": "recompute_labels",
                "summary": "zero_frac≈1; rerun label pipeline with lower threshold.",
                "review_result": {
                    "summary": {"oos_sharpe": 0.0, "trades": 0},
                    "diagnosis": [
                        "Label stats zero_frac=1.0 from horizon mismatch/label_bps too high",
                    ],
                    "action_rationale": "Regenerate labels via tools/recompute_labels_1m.py before next ML attempt.",
                    "next_actions": [
                        {
                            "action": "queue_job",
                            "params": {
                                "job_type": "label_recompute",
                                "params": {
                                    "symbols": ["TSLA"],
                                    "start": "2025-11-19 09:30:00",
                                    "end": "2025-11-19 16:00:00",
                                    "horizon_min": 5,
                                    "label_bps": 5,
                                },
                            },
                            "confidence": 0.85,
                        }
                    ],
                },
            }
        ],
    },
    "label_diag": {
        "title": "Label diagnostics job wiring",
        "cycles": [
            {
                "name": "inspect_labels",
                "summary": "Dispatch label_diagnostics job for TSLA",
                "review_result": {
                    "summary": {"oos_sharpe": 0.0},
                    "diagnosis": [
                        "Need label_diagnostics to inspect zero_frac before deciding on label_recompute",
                    ],
                    "action_rationale": "Use label_diagnostics job to get per-symbol label stats.",
                    "next_actions": [
                        {
                            "action": "queue_job",
                            "params": {
                                "job_type": "label_diagnostics",
                                "params": {"symbols": ["TSLA"]},
                            },
                            "confidence": 0.8,
                        }
                    ],
                },
            }
        ],
    },
    "feature_experiment": {
        "title": "Sandbox feature experiment job",
        "cycles": [
            {
                "name": "experiment_specs",
                "summary": "Run build+eval in sandbox before promoting specs",
                "review_result": {
                    "summary": {"oos_sharpe": 0.0},
                    "diagnosis": [
                        "Need to experiment with speculative features before touching production catalog",
                    ],
                    "action_rationale": "Use feature_experiment job to run build+eval in isolation",
                    "next_actions": [
                        {
                            "action": "queue_job",
                            "params": {
                                "job_type": "feature_experiment",
                                "params": {
                                    "specs": [
                                        {
                                            "name": "experiment_feat",
                                            "expr": "(f.close - f.vwap) / nullIf(f.vwap, 0)",
                                            "dtype": "Float64",
                                            "notes": "sandbox feature"
                                        }
                                    ]
                                }
                            },
                            "confidence": 0.8,
                        }
                    ],
                },
            }
        ],
    },
    "feature_promote": {
        "title": "Promote sandbox specs into production catalog",
        "cycles": [
            {
                "name": "promote_specs",
                "summary": "Promote experiment_feat from sandbox catalog to candidate set",
                "review_result": {
                    "summary": {"oos_sharpe": 0.6, "auc_lift": 0.02},
                    "diagnosis": [
                        "Sandbox experiment shows positive AUC/IC lift; safe to promote spec into production catalog.",
                    ],
                    "action_rationale": "Use feature_promote to merge sandbox catalog into config/features_catalog.json.",
                    "next_actions": [
                        {
                            "action": "queue_job",
                            "params": {
                                "job_type": "feature_promote",
                                "params": {
                                    "from_catalog": "runs/ai_trader/feature_experiments/feature_experiment-01/catalog.json",
                                    "spec_names": ["experiment_feat"],
                                    "promote_to": "candidate",
                                },
                            },
                            "confidence": 0.9,
                        }
                    ],
                },
            }
        ],
    },
}


# ----------------------------
# Test harness plumbing
# ----------------------------
CALL_LOG: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
CURRENT_CONTEXT: Dict[str, Any] = {}


def _set_context(ctx: Dict[str, Any]) -> None:
    global CURRENT_CONTEXT
    CURRENT_CONTEXT = ctx or {}


def _log_call(kind: str, job_id: str, **extra: Any) -> Dict[str, Any]:
    entry = {"job_id": job_id, **extra}
    CALL_LOG[kind].append(entry)
    return entry


async def _fake_enqueue_ml_job(params: dict, job_id: str) -> dict:
    queue_id = f"{job_id}-{params.get('job_type', 'unknown')}-{len(CALL_LOG['queue_job']) + 1}"
    _log_call("queue_job", job_id, job_type=params.get("job_type"), params=params, queued_job_id=queue_id)
    return {"status": "ok", "queued_job_id": queue_id}


async def _fake_execute_fix_config(params: dict, job_id: str) -> dict:
    _log_call("fix_config", job_id, params=params)
    return {
        "status": "ok",
        "path": params.get("path", ""),
        "key": params.get("key", ""),
        "backup": "simulated_backup.json",
        "old_value": "<simulated>",
        "new_value": params.get("value"),
    }


async def _fake_execute_feature_recompute(params: dict, job_id: str) -> dict:
    symbols = params.get("symbols") or ["TSLA"]
    if isinstance(symbols, str):
        symbols = [s.strip() for s in symbols.split(",") if s.strip()]
    start = params.get("start")
    end = params.get("end")
    _log_call("feature_recompute", job_id, params={**params, "symbols": symbols, "start": start, "end": end})
    return {
        "status": "ok",
        "details": [{"symbol": sym, "rc": 0} for sym in symbols],
        "window": {"start": start, "end": end},
    }


async def _fake_execute_backfill(params: dict, job_id: str) -> dict:
    symbols = params.get("symbols") or []
    if isinstance(symbols, str):
        symbols = [s.strip() for s in symbols.split(",") if s.strip()]
    _log_call("backfill", job_id, params={**params, "symbols": symbols})
    return {
        "status": "ok",
        "symbols": symbols,
    }


def _dq_template(params: dict) -> Dict[str, Any]:
    template = (CURRENT_CONTEXT or {}).get("dq_report")
    if template:
        return template
    symbols = params.get("symbols") or ["TSLA"]
    return {
        "start": f"{date.today()}T09:30:00Z",
        "end": f"{date.today()}T16:00:00Z",
        "min_coverage": float(params.get("min_coverage", 0.98)),
        "symbols": [{"symbol": sym, "coverage": 0.99} for sym in symbols],
    }


async def _fake_execute_data_quality_check(params: dict, job_id: str) -> dict:
    template = _dq_template(params)
    report_dir = _ensure_runs_dir() / date.today().isoformat()
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"data_quality_sim_{job_id}.json"
    payload = {
        "ts": datetime.now().isoformat(),
        "job_id": job_id,
        "active_report": template,
        "health_rc": 0,
        "active_rc": 1 if any(s.get("coverage", 1.0) < template.get("min_coverage", 0.98) for s in template.get("symbols", [])) else 0,
    }
    report_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    _log_call("data_quality_check", job_id, params=params, template=template, report=str(report_path))
    return {"status": "ok", "health_rc": 0, "active_rc": payload["active_rc"], "report": str(report_path)}


def _validate_catalog_specs(specs: Iterable[dict]) -> None:
    for spec in specs:
        name = spec.get("name")
        expr = spec.get("expr", "")
        dtype = spec.get("dtype")
        notes = spec.get("notes", "")
        if not name or not expr or not dtype:
            raise ValueError("Catalog spec missing required fields")
        if not any(prefix in expr for prefix in ("f.", "m5.", "d.")):
            raise ValueError(f"Spec {name} must reference at least one allowed alias (f./m5./d.)")
        if "select" in expr.lower() or "create" in expr.lower():
            raise ValueError(f"Spec {name} contains forbidden SQL keywords")
        if not notes.strip():
            raise ValueError(f"Spec {name} must include reviewer notes")


def _fake_write_features_catalog(params: dict) -> tuple[bool, str]:
    specs = params.get("specs") or []
    _validate_catalog_specs(specs)
    job_id = (CURRENT_CONTEXT or {}).get("job_id", "<unknown>")
    _log_call("propose_features", job_id, version=params.get("version"), spec_count=len(specs))
    return True, "simulated_catalog_update"


def _fake_write_features_catalog_with_sources(params: dict) -> tuple[bool, str, list[str]]:
    specs = params.get("specs") or []
    req = sorted(
        {
            src
            for spec in specs
            if isinstance(spec, dict)
            for src in spec.get("required_sources", [])
        }
    )
    job_id = (CURRENT_CONTEXT or {}).get("job_id", "<unknown>")
    _log_call("propose_features_write", job_id, spec_count=len(specs), required_sources=req)
    return True, "ok", req


def _read_audit_entries(job_id: str) -> List[Dict[str, Any]]:
    actions_file = _ensure_runs_dir() / date.today().isoformat() / "ml_actions.jsonl"
    if not actions_file.exists():
        return []
    entries: List[Dict[str, Any]] = []
    for line in actions_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if data.get("job_id") == job_id:
            entries.append(data)
    return entries


@contextmanager
def _patched_actions():
    from unittest.mock import patch

    with ExitStack() as stack:
        stack.enter_context(patch("scripts.ai_trader.control_event_consumer._enqueue_ml_job", new=_fake_enqueue_ml_job))
        stack.enter_context(patch("scripts.ai_trader.control_event_consumer._execute_fix_config", new=_fake_execute_fix_config))
        stack.enter_context(patch("scripts.ai_trader.control_event_consumer._execute_feature_recompute", new=_fake_execute_feature_recompute))
        stack.enter_context(patch("scripts.ai_trader.control_event_consumer._execute_data_quality_check", new=_fake_execute_data_quality_check))
        stack.enter_context(patch("scripts.ai_trader.control_event_consumer._write_features_catalog", new=_fake_write_features_catalog_with_sources))
        stack.enter_context(patch("scripts.ai_trader.control_event_consumer._execute_backfill", new=_fake_execute_backfill))
        stack.enter_context(patch("scripts.ai_trader.control_event_consumer._runs_dir", new=_ensure_runs_dir))
        stack.enter_context(patch("scripts.ai_trader.control_event_consumer._ttl_lock", new=lambda *_, **__: False))
        stack.enter_context(patch("scripts.ai_trader.control_event_consumer._check_daily_quota", new=lambda *_: (True, "")))
        stack.enter_context(patch("scripts.ai_trader.control_event_consumer._ensure_resample_sources", new=lambda *_: None))
        yield


async def _run_cycle(scenario: str, cycle_idx: int, cycle: Dict[str, Any], verbose: bool) -> None:
    job_id = f"{scenario}-{cycle_idx:02d}"
    ctx = dict(cycle.get("context") or {})
    ctx.setdefault("job_id", job_id)
    ctx.setdefault("scenario", scenario)
    ctx.setdefault("cycle", cycle.get("name"))
    _set_context(ctx)

    await _execute_ml_actions(cycle["review_result"], job_id, cycle.get("prev_oof_metrics"), cycle.get("cur_oof_metrics"))

    entries = _read_audit_entries(job_id)
    header = f"  Cycle {cycle_idx:02d} ({cycle.get('name')}): {cycle.get('summary')}"
    print(header)
    if not entries:
        print("    No audit entries found (action may have been skipped).")
        return
    for entry in entries:
        action = entry.get("action")
        status = entry.get("status")
        reason = entry.get("reason", "")
        extra = {k: v for k, v in entry.items() if k in {"queued_job_id", "stage", "result"} and v}
        extra_str = f" {json.dumps(extra)}" if extra else ""
        reason_str = f", reason={reason}" if reason else ""
        print(f"    - action={action}, status={status}{reason_str}{extra_str}")
    if verbose:
        related_logs = {
            kind: [item for item in items if item.get("job_id") == job_id]
            for kind, items in CALL_LOG.items()
            if any(item.get("job_id") == job_id for item in items)
        }
        for kind, items in related_logs.items():
            print(f"      stub[{kind}]: {json.dumps(items, indent=2)}")


async def _run_scenarios(scenarios: Iterable[str], verbose: bool) -> None:
    for name in scenarios:
        scenario = SCENARIOS[name]
        print(f"\n=== Scenario: {name} — {scenario['title']} ===")
        for idx, cycle in enumerate(scenario["cycles"], start=1):
            await _run_cycle(name, idx, cycle, verbose)


def _list_scenarios() -> None:
    for key, value in SCENARIOS.items():
        print(f"{key:12s} - {value['title']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Simulate AI Brain actions and validate tooling usage.")
    parser.add_argument("--scenario", action="append", help="Scenario(s) to run (default: all).")
    parser.add_argument("--list", action="store_true", help="List scenarios and exit.")
    parser.add_argument("--verbose", action="store_true", help="Print stub call payloads for debugging.")
    args = parser.parse_args()

    if args.list:
        _list_scenarios()
        return

    targets = args.scenario or list(SCENARIOS.keys())
    unknown = [s for s in targets if s not in SCENARIOS]
    if unknown:
        raise SystemExit(f"Unknown scenario(s): {', '.join(unknown)}")

    with _patched_actions():
        asyncio.run(_run_scenarios(targets, args.verbose))


if __name__ == "__main__":
    main()
