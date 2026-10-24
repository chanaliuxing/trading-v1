# Runtime Paper/Shadow Soak Execution - 2026-05-12

## Scope

| Field | Value |
|---|---|
| mode | `SIMULATED_SHADOW` |
| duration | `0 seconds` simulated artifact generation |
| command | `TRADING_TEST_ALLOW_LIVE_IBKR=0 venv/bin/python tool/report/run_simulated_paper_shadow_soak.py --repo-root . --day 20260512 --mode SIMULATED_SHADOW` |
| broker policy | live broker disabled / mocked |
| order mutation policy | real order, cancel, and modify disabled |
| reconciliation mode | report only |

## Results

| Check | Result |
|---|---|
| pre-soak safety verdict | `PASS` |
| soak gate verdict | `PASS` for simulated shadow artifacts |
| decision | `SOAK_INSUFFICIENT_DATA_COLLECT_MORE` |
| blockers | none |
| warnings | none from the soak gate |
| OMS invariants | ghost `0`, orphan `0`, symbol-only attribution `0`, order-ref heuristic owner `0`, paper/shadow intent `1` |
| diagnostics | approved-to-fill and Vegas measurement reports present |

The soak gate passed against freshly generated simulated shadow artifacts. This is useful for validating the artifact contract and report wiring, but it is not a real bounded paper/shadow runtime session. It does not upgrade the system to canary.

## Artifacts

| Artifact | Status |
|---|---|
| `runtime/readiness/broker_truth_snapshot_latest.json` | fresh |
| `runtime/readiness/paper_broker_truth_snapshot_latest.json` | fresh |
| `runtime/readiness/position_safety_latest.json` | fresh |
| `runtime/readiness/production_state_latest.json` | fresh |
| `runtime/readiness/runtime_version_latest.json` | fresh |
| `runtime/readiness/strategy_tradability_heartbeat_20260512.jsonl` | fresh |
| `reports/strategy_surface/live_strategy_surface_report_20260512.json` | fresh |
| `reports/approved_to_fill_conversion/approved_to_fill_conversion_latest.json` | fresh |
| `reports/vegas_dlb_edge_measurement/vegas_dlb_edge_measurement_latest.json` | fresh |
| `runtime/audit/paper_shadow_soak_20260512.jsonl` | present |

## Safety Scan

No actual runtime live broker or broker mutation event was found. Matches were limited to report summary keys or test names in captured search output, not runtime events.

## Approved Scope

- Continue collecting missing real paper/shadow runtime artifacts.
- Use simulated shadow output to validate report wiring and manual gate preparation.
- Keep broker and mutation paths disabled unless a later manual gate explicitly changes scope.

## Not Approved

- Full live trading.
- Unattended production live.
- Real broker mutation from this soak path.
- Auto-cancel unknown broker orders.
- Auto-flatten unknown owner positions.
- Canary upgrade based only on simulated shadow artifacts.

## Next Gate

`Collect Missing Paper/Shadow Artifacts`
