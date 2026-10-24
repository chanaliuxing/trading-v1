# Runtime Paper/Shadow Real Artifact Collection - 2026-05-12

## Scope

| Field | Value |
|---|---|
| mode | `SHADOW_ONLY` |
| duration | `6 seconds`, bounded |
| runtime command | `TRADING_TEST_ALLOW_LIVE_IBKR=0 venv/bin/python tool/runtime/run_bounded_paper_shadow_runtime.py --mode SHADOW_ONLY --duration-sec 6 --tick-sec 2 --day 20260512 --output-root .` |
| broker policy | live broker disabled and mocked |
| order mutation policy | real order, cancel, and modify disabled |
| reconciliation mode | report only |

This run used a bounded no-live runtime harness because `run_system.py` paper mode initializes active paper execution paths. The harness emits time-sequenced readiness, heartbeat, audit, strategy-surface, and diagnostics inputs without constructing a live IBKR adapter or mutating broker state.

## Baseline

| Command | Result |
|---|---|
| `TRADING_TEST_ALLOW_LIVE_IBKR=0 venv/bin/python -m pytest -q tests/unit/safety/test_no_live_ibkr_in_unit_tests.py` | `4 passed` |
| `TRADING_TEST_ALLOW_LIVE_IBKR=0 venv/bin/python -m pytest -q tests/unit/services` | `656 passed` |
| `TRADING_TEST_ALLOW_LIVE_IBKR=0 venv/bin/python -m pytest -q tests/unit/exec` | `291 passed` |
| `TRADING_TEST_ALLOW_LIVE_IBKR=0 venv/bin/python -m pytest -q tests/unit/reconciliation` | `76 passed` |
| `TRADING_TEST_ALLOW_LIVE_IBKR=0 venv/bin/python -m pytest -q tests/unit/reports` | `35 passed` |
| `TRADING_TEST_ALLOW_LIVE_IBKR=0 venv/bin/python -m pytest -q tests/unit/reports/test_paper_shadow_soak_gate_report.py` | `11 passed` |

## Runtime Artifact Status

| Artifact | Exists | Fresh | Source | Notes |
|---|---:|---:|---|---|
| `runtime/readiness/broker_truth_snapshot_latest.json` | yes | yes | bounded shadow harness | paper-shadow broker truth |
| `runtime/readiness/paper_broker_truth_snapshot_latest.json` | yes | yes | bounded shadow harness | no live broker dependency |
| `runtime/readiness/position_safety_latest.json` | yes | yes | bounded shadow harness | consumes paper-shadow broker truth |
| `runtime/readiness/production_state_latest.json` | yes | yes | bounded shadow harness | explainable `go` artifact |
| `runtime/readiness/runtime_version_latest.json` | yes | yes | bounded shadow harness | loaded-code current |
| `runtime/readiness/strategy_tradability_heartbeat_20260512.jsonl` | yes | yes | bounded shadow harness | time-sequenced heartbeats |
| `runtime/audit/20260512/*.jsonl` | yes | yes | bounded shadow harness | decision, intent, reservation, execution-accept traces |
| `runtime/audit/paper_shadow_runtime_20260512.jsonl` | yes | yes | bounded shadow harness | soak-gate audit input |
| `reports/strategy_surface/live_strategy_surface_report_20260512.json` | yes | yes | bounded shadow harness | canary-only tier |
| `reports/approved_to_fill_conversion/approved_to_fill_conversion_latest.json` | yes | yes | report builder | built from runtime audit |
| `reports/vegas_dlb_edge_measurement/vegas_dlb_edge_measurement_latest.json` | yes | yes | report builder | built from runtime audit |

## Diagnostics

| Report | Sample Count | Classification | Notes |
|---|---:|---|---|
| approved-to-fill conversion | `1` | `BROKER_SUBMISSION_BOTTLENECK` | expected: shadow-only stops before broker submit |
| Vegas DLB/edge measurement | `24` | `MIXED` | generated from bounded audit rows; no parameter changes made |

## Soak Gate Result

| Field | Value |
|---|---|
| verdict | `PASS` |
| blockers | none |
| warnings | none |
| missing artifacts | none |
| no-live leak | true |
| owner identity | pass |
| OMS invariants | pass |

## Safety Scan

| Category | Result |
|---|---|
| live IBKR connection evidence | none |
| real broker mutation evidence | none |
| attribution violations | none |
| scan matches | one report summary key: `symbol_only_attribution_count: 0`; not a runtime violation |

## Decision

`SOAK_PASS_READY_FOR_CANARY_MANUAL_GATE`

This does not approve canary execution by itself. It means the next step can be a manual gate review with bounded shadow artifacts, no-live evidence, and diagnostics in hand.

## Approved Scope

- Proceed to Canary Readiness Manual Gate only.
- Continue bounded paper/shadow artifact collection with broker mutation disabled.
- Use diagnostics for manual review and operator evidence.

## Not Approved

- Full live trading.
- Unattended live.
- Real broker mutation.
- Auto-cancel unknown broker orders.
- Auto-flatten unknown owner positions.
- Canary without manual gate.
