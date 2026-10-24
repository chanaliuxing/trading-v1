# Paper/Shadow Soak Gate Checklist

## Before Soak

- Confirm the no-live IBKR guard passes.
- Confirm paper/shadow mode is configured.
- Confirm live broker access is disabled, mocked, or routed to a paper-only source.
- Confirm strategy surface tiers are generated and direct/legacy/private paths are not production eligible.
- Confirm production state contract tests are green.
- Confirm unknown owner auto-cancel and auto-flatten remain disabled.

## During Soak

- Confirm artifacts continue refreshing.
- Confirm production state is explainable.
- Confirm no unknown-owner auto action occurs.
- Confirm no symbol-only attribution appears.
- Confirm no `order_ref` prefix or substring owner attribution appears.
- Confirm approved-to-fill conversion report is generated.
- Confirm Vegas DLB/edge measurement report is generated.
- Confirm ghost/orphan events are zero or quarantined/report-only.

## After Soak

- Review `reports/paper_shadow_soak_gate/paper_shadow_soak_gate_latest.json`.
- Review the generated markdown report under `docs/reports/`.
- Review ghost, orphan, unknown-owner, and manual-review counts.
- Review approved-to-fill conversion.
- Review Vegas DLB/edge measurement.
- Review production state blockers and readiness buckets.
- Decide the next gate.

## Explicit Non-Approval

This checklist does not approve unattended live trading. Canary still requires manual gates, fresh runtime artifacts, operator readiness, emergency stop proof, and explicit approval.
