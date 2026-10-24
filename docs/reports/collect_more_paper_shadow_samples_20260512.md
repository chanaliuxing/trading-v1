# Collect More Paper/Shadow Samples

Generated at: `2026-05-12T17:52:09-04:00`

## Scope

- Mode coverage: `SHADOW_ONLY` and `PAPER_ONLY`
- Live broker policy: `TRADING_TEST_ALLOW_LIVE_IBKR=0`; live broker disabled/mocked
- Order mutation policy: no real order/cancel/modify; paper broker events are audit-only
- Strategy parameters: unchanged
- Strategy entry/exit logic: unchanged

## Commands

| Mode | Duration | Command | Sample Count | Notes |
|---|---:|---|---:|---|
| `SHADOW_ONLY` | 30s bounded / no sleep | `TRADING_TEST_ALLOW_LIVE_IBKR=0 venv/bin/python tool/runtime/run_bounded_paper_shadow_runtime.py --mode SHADOW_ONLY --duration-sec 30 --tick-sec 2 --sample-count 10 --day 20260512 --output-root . --no-sleep` | 10 | Emits `uom_skipped_shadow` with `execution_mode_shadow_no_delegate`. |
| `PAPER_ONLY` | 30s bounded / no sleep | `TRADING_TEST_ALLOW_LIVE_IBKR=0 venv/bin/python tool/runtime/run_bounded_paper_shadow_runtime.py --mode PAPER_ONLY --duration-sec 30 --tick-sec 2 --sample-count 10 --emit-paper-broker-events true --append --day 20260512 --output-root . --no-sleep` | 10 | Emits fake `paper_broker` submit/ack/fill/PnL audit events only. |

## Diagnostics Summary

| Report | Sample Count | Classification | Key Counts |
|---|---:|---|---|
| `strategy_execution_reliability_latest.json` | 20 | `NO_BOTTLENECK` | UOM `delegated=10`, `skipped=10`; paper submit/ack/fill/attribution/PnL each `10`; drop stage `none=20`. |
| `approved_to_fill_conversion_latest.json` | 20 | `NO_BOTTLENECK` | `approved=20`, `intent_created=20`, `shadow_no_broker_submit=10`, `broker_submitted=10`, `broker_acknowledged=10`, `filled=10`, `pnl_replay_ready=10`; broker submit drops `0`. |
| `vegas_dlb_edge_measurement_latest.json` | 280 | `NO_BOTTLENECK` | DLB breaches `0`, edge insufficient `0`, stale decisions `0`; no parameter calibration signal. |
| `paper_shadow_soak_gate_latest.json` | n/a | `PASS` | Freshness, diagnostics, no-live leak, OMS invariants, owner identity all pass. |

## Safety Scan

No actual runtime live broker or mutation event was found. The only matches were report text/summary keys, such as `symbol_only_attribution_count: 0`, or historical report sections listing scan patterns.

## Decision

`MORE_SAMPLES_READY_FOR_NEXT_DIAGNOSTIC`

The sample count is no longer one, UOM is not a silent loss, shadow-only skip is explicitly classified, and fake paper broker submit/ack/fill/attribution/PnL evidence exists. Current bounded samples show no report-level bottleneck; the next step is to validate the paper broker submit/ack boundary with a more realistic paper adapter path.

## Next Batch

Recommended next batch: `Paper Broker Submit/Ack Reliability`.

The fake paper path now proves the report chain can represent submit and ack. The next useful work is to harden the paper broker adapter/audit boundary itself with more realistic paper submit/ack behavior, still without live IBKR.
