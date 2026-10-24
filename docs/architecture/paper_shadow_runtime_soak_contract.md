# Paper/Shadow Runtime Soak Contract

## Soak Mode

Supported modes:

- `PAPER_ONLY`
- `SHADOW_ONLY`
- `PAPER_AND_SHADOW`

The default validation mode is `PAPER_AND_SHADOW`. A soak run is not a live or canary execution approval.

## Required Duration

The default minimum is one full market session or a simulated equivalent. Shorter runs may produce a report, but they should be treated as incomplete evidence for canary readiness.

## Required Artifacts

The soak gate expects fresh artifacts for:

- `broker_truth_snapshot_latest.json`
- `broker_truth_live_snapshot_latest.json` or `paper_broker_truth_snapshot_latest.json`
- `position_safety_latest.json`
- `production_state_latest.json`
- strategy tradability heartbeat JSONL
- live strategy surface report
- OMS owner/quarantine/reconciliation reports
- approved-to-fill conversion report
- Vegas DLB/edge measurement report
- runtime version freshness artifact

## Required Invariants

- No live IBKR connection.
- No real broker order, cancel, or modify.
- Broker truth must be from paper/shadow broker source or explicitly marked paper.
- Production state must be explainable.
- Owner identity must be complete for all paper/shadow intents: `account_id`, `strategy_id`, `strategy_instance_id`, `symbol`, `intent_id` or `client_order_id`, and `order_role`.
- Unknown broker/manual orders remain manual-review only.
- No symbol-only attribution.
- No `order_ref` prefix or substring ownership.
- `ghost_count = 0` or explicitly quarantined/report-only.
- `orphan_count = 0` or explicitly quarantined/report-only.
- Strategy-instance ownership is preserved across restart/replay evidence.

## Verdict Rules

- `PASS`: required artifacts are fresh, no live leak, no real mutation markers, and OMS invariants are green.
- `FAIL`: live leak, real mutation marker, stale critical artifact, missing owner identity, symbol-only attribution, order-ref heuristic ownership, unknown-owner auto-cancel, unknown-owner auto-flatten, or broker aggregate overwrite evidence is present.
- `INSUFFICIENT_DATA`: required artifacts are missing and no stronger failure is present.

## Explicit Limits

This soak gate does not approve full live trading, unattended production live, auto-cancel of unknown broker orders, or auto-flatten of unknown owner positions. Canary still requires a separate manual gate with fresh runtime artifacts.
