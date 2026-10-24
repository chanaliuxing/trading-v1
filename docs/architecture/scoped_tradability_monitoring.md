# Scoped Tradability Monitoring

This document defines artifact-level monitoring for scoped tradability. It does
not authorize execution. `TradabilityService` remains the authority for exact
account, strategy instance, symbol, session, and flow decisions.

## Metrics

| Metric | Source | Purpose |
| --- | --- | --- |
| `global_halt_active` | production_state.global_halt | Detect hard platform halt. |
| `entry_go_no_go_partial` | production_state.entry_go_no_go | Detect scoped entry blocks while platform remains trusted. |
| `blocked_scope_count` | production_state.blocked_scopes | Track scoped blocker volume. |
| `blocked_scope_by_strategy_instance` | production_state.blocked_scopes | Identify impacted strategy instances. |
| `flow_override_count` | production_state.flow_overrides | Track close/reduce/protective override usage. |
| `tradability_decision_denied_total` | TradabilityDecision audit/heartbeat | Count denied decisions by blocker and flow. |
| `tradability_decision_allowed_total` | TradabilityDecision audit/heartbeat | Count allowed decisions by flow. |
| `strategy_system_paused_block_count` | strategy heartbeat/audit | Detect system pause entry blockers that were converted to decisions. |
| `session_policy_block_count` | EOD/session policy artifact and production_state | Detect session-scoped entry blocks. |
| `eod_owner_flatten_count` | platform_eod_flatten artifact | Count owner-scoped EOD flatten attempts. |
| `unknown_owner_fill_count` | broker truth convergence/quarantine | Detect owner attribution gaps. |
| `owner_inheritance_failure_count` | broker truth convergence/position ledger | Detect fills that cannot inherit durable owner lane. |
| `partial_state_misread_guard` | production_state | Detect consumers treating `entry_go_no_go=partial` as global go/no-go. |

## Alert Drafts

| Alert | Condition | Action |
| --- | --- | --- |
| `global_halt_active` | `global_halt=true` | Stop new entry and page operator. |
| `entry_go_no_go_partial_persists` | `entry_go_no_go=partial` beyond expected window | Inspect blocked scopes and session policy. |
| `blocked_scope_count_spike` | blocked scopes exceed baseline | Inspect strategy instance and account/symbol distribution. |
| `flow_override_without_owner_identity` | override exists with missing account or strategy instance | Block override and require owner review. |
| `strategy_still_using_system_paused_hard_return` | heartbeat lacks `TradabilityDecision` evidence | Treat as migration regression. |
| `owner_inheritance_failure` | adopted/legacy fill lacks owner lane | Quarantine fill and require manual review. |
| `symbol-only attribution detected` | audit scan finds symbol-only attribution | Block release until attribution path is removed. |

## Required Dashboards

- Global halt and hard blocker panel.
- Partial entry state panel with blocked scope table.
- Flow override panel grouped by flow type and owner identity.
- EOD/session policy panel with selected owner-scoped flatten targets.
- Broker truth owner inheritance panel with unknown-owner fills.

## Invariants

- `go_no_go=go` means the platform is trusted, not that every strategy may enter.
- `entry_go_no_go=partial` must always include explainable blocked scopes.
- `blocked_scopes` must include account and strategy instance when known.
- `flow_overrides` cannot bypass broker truth, runtime freshness, manual recovery,
  order truth, or safety-kernel global halt.
- Symbol-only and order-ref prefix/substring attribution are monitoring failures.
