# Scoped Tradability Contract

Date: 2026-05-12

This contract defines the target capability-based tradability architecture. It
does not remove the existing global halt path. It separates platform trust from
scoped trading permission and makes `TradabilityService` the only authority that
answers whether a specific flow may proceed for an account, strategy instance,
symbol, and session.

## Definitions

`GLOBAL_HALT` means the platform or broker truth substrate is not trustworthy.
No strategy may open new risk. Flow overrides cannot bypass it except an
explicit emergency/manual/admin flow where the contract says so.

`SCOPED_BLOCK` means a blocker applies only to a specific account, strategy,
strategy instance, symbol, session, or flow. It must not freeze unrelated
strategy instances by implication.

`FLOW_OVERRIDE` means a close, reduce, stop, repair, or flatten flow may proceed
while entry is blocked, provided owner identity is complete and no hard global
halt forbids it.

## Scope Key

Every authorization request and decision must carry:

```text
account_id
strategy_id
strategy_instance_id
symbol
asset_type
session
flow_type
order_role
lot_id optional
```

`strategy_id + symbol` is not a sufficient scope. It cannot disambiguate
same-symbol multi-strategy ownership, account routing, or owner-scoped flatten.

## Flow Types

```text
ENTRY
SCALE_IN
EXIT
SCALE_OUT
STOP_LOSS
TAKE_PROFIT
TRAILING_STOP
STRATEGY_FLATTEN
ACCOUNT_EMERGENCY_FLATTEN
PROTECTIVE_REPAIR
RECONCILIATION_REPORT
```

## Global Halt Blockers

These blockers remain hard platform blockers:

- IB connection or gateway recovery is not trustworthy.
- Broker truth is missing, stale, lookup-failed, or not broker-backed.
- Position safety is missing, stale, lookup-failed, or `no_go`.
- Runtime freshness is stale, unknown, missing manifest, or build-mismatched.
- Manual recovery is required.
- Unknown owner quarantine when configured at global or account scope.
- Ledger/broker divergence unresolved at account or symbol scope.
- Order truth cannot reconcile.
- Safety kernel global emergency halt.

## Scoped Blockers

These blockers should become scoped by account, strategy, strategy instance,
symbol, session, or flow:

- Strategy manual lock.
- Strategy instance manual lock.
- Symbol lock.
- Session closed for a strategy/session policy.
- RTH-only EOD close policy.
- Data stale for one strategy/symbol.
- Quote missing for one option strategy/contract.
- Per-strategy risk cap.
- Per-symbol/account risk cap.

## Flow Override Rules

`ENTRY` and `SCALE_IN` require scoped permission.

`EXIT`, `SCALE_OUT`, `STOP_LOSS`, `TAKE_PROFIT`, `TRAILING_STOP`, and
`STRATEGY_FLATTEN` may be allowed when entry is blocked if owner identity is
complete and the decision evidence shows the override.

`ACCOUNT_EMERGENCY_FLATTEN` requires explicit emergency/manual/admin scope. It
must not be inferred from a normal strategy EOD or manual lock.

Flow overrides cannot bypass broker truth missing, runtime stale, or manual
recovery required unless the flow is explicitly emergency and manually
authorized.

## Session Close Policy

`RTH_ONLY_EQUITY` blocks `ENTRY` after RTH close and allows close-only or
strategy flatten.

`EXTENDED_EQUITY` allows `ENTRY` in configured `PRE`, `POST`, or `OVERNIGHT`
sessions when data, risk, broker truth, and account constraints are good.

`OPTIONS_RTH_ONLY_DEFAULT` blocks extended-hours entry unless contract, broker,
and quote policy explicitly allow extended trading.

`TEST_EXTENDED` allows configured off-hours tests only under explicit
account/symbol risk caps.

EOD/session policy is session-scoped. It must not default to all-strategy or
all-account flatten unless invoked by an explicit account emergency flow.

## Manual Lock Rules

Manual locks must include `account_id` and `strategy_instance_id` when they are
intended to block one strategy instance. Legacy `strategy_id + symbol` locks are
conservative until migrated, but the target contract treats them as scoped
blockers, not platform halts.

Manual lock after strategy flatten blocks new entry for the locked scope. It
does not block owner-identified exits, stops, or reduce-only flows for that same
owner.

## Production State Output Contract

`ProductionState` answers platform trust and scoped blockers. It is not the
execution authorization source.

Target shape:

```json
{
  "global_halt": false,
  "entry_go_no_go": "go|no_go|partial",
  "safety_go_no_go": "go|no_go",
  "blocked_scopes": [
    {
      "scope_type": "STRATEGY_INSTANCE|ACCOUNT_SYMBOL|SESSION|GLOBAL",
      "account_id": "...",
      "strategy_instance_id": "...",
      "symbol": "...",
      "flow_type": "ENTRY",
      "blockers": [],
      "authority": "TradabilityService"
    }
  ],
  "flow_overrides": [
    {
      "flow_type": "STRATEGY_FLATTEN",
      "scope": "...",
      "allowed": true,
      "reason": "close_only_allowed"
    }
  ]
}
```

`production_state.go_no_go=go` does not mean every strategy may enter.
`entry_go_no_go=partial` is valid when at least one scoped entry is blocked and
at least one scoped entry remains allowed. `global_halt=true` blocks all entry.

## TradabilityDecision Schema

```python
class HaltClass(str, Enum):
    GLOBAL_HALT = "GLOBAL_HALT"
    SCOPED_BLOCK = "SCOPED_BLOCK"
    NONE = "NONE"

class FlowType(str, Enum):
    ENTRY = "ENTRY"
    SCALE_IN = "SCALE_IN"
    EXIT = "EXIT"
    SCALE_OUT = "SCALE_OUT"
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    TRAILING_STOP = "TRAILING_STOP"
    STRATEGY_FLATTEN = "STRATEGY_FLATTEN"
    ACCOUNT_EMERGENCY_FLATTEN = "ACCOUNT_EMERGENCY_FLATTEN"
    PROTECTIVE_REPAIR = "PROTECTIVE_REPAIR"
    RECONCILIATION_REPORT = "RECONCILIATION_REPORT"

@dataclass(frozen=True)
class TradabilityScope:
    account_id: str
    strategy_id: str
    strategy_instance_id: str
    symbol: str
    flow_type: FlowType
    session: str
    asset_type: Optional[str] = None
    order_role: Optional[str] = None
    lot_id: Optional[str] = None

@dataclass(frozen=True)
class TradabilityDecision:
    allow: bool
    halt_class: HaltClass
    authority: str
    scope: TradabilityScope
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]
    overrides: tuple[str, ...]
    close_only_allowed: bool
    reduce_only_allowed: bool
    reason: str
    generated_at: str
```

Every decision must include authority, scope, flow type, session, allow/deny,
blockers, overrides, owner identity, and timestamp.

## Implementation Status

Phase 1 is implemented as a pure, side-effect-free evaluator in
`services/production/scoped_tradability.py`.

Implemented:

- `HaltClass`, `FlowType`, `TradabilityScope`, and `TradabilityDecision`.
- `ScopedBlocker`, `SessionPolicy`, and `TradabilityInputs` helper contracts.
- `evaluate_tradability(scope, inputs, now=None)`.
- Hard `GLOBAL_HALT` decisions for broker truth, position safety, runtime
  freshness, manual recovery, order truth, ledger/broker divergence, and safety
  kernel halt blockers.
- Scoped blocker matching for strategy instance, account symbol, strategy,
  account, session, symbol, and exact scopes.
- Session policy handling for `RTH_ONLY_EQUITY`, `EXTENDED_EQUITY`,
  `OPTIONS_RTH_ONLY_DEFAULT`, and `TEST_EXTENDED`.
- Flow overrides for owner-identified close/reduce/protective/strategy-flatten
  flows when only scoped entry/session blockers are present.
- Explicit authorization requirement for `ACCOUNT_EMERGENCY_FLATTEN`.
- Report-only allowance for `RECONCILIATION_REPORT` with blockers surfaced as
  warnings.

Phases 2-6 are now partially implemented:

- `tool/ops/run_runtime_operations_watchdog.py` emits `global_halt`,
  `blocked_scopes`, `flow_overrides`, and `production_state_authorizes_execution=false`.
- `entry_go_no_go=partial` is emitted when platform safety is go, at least one
  scoped entry is blocked, and at least one strategy entry scope remains ready.
- `ExecutionService` performs a pure `TradabilityDecision` second-check before
  risk approval, persistence, or order placement. A denial returns at validation
  with the decision evidence in metadata.
- `run_system.py` injects a read-only readiness-artifact provider into
  `ExecutionService`; the provider maps broker truth, position safety, runtime
  freshness, manual recovery, and production-state scoped blockers into
  `TradabilityInputs`.
- `strategies/stocks_kernel/strategy.py` records a `TradabilityDecision` while
  paused instead of treating `_system_paused` as an evidence-free return path.
- Session policy is represented by `SessionPolicy` and enforced by the pure
  evaluator for RTH-only, extended equity, default options RTH-only, and test
  extended policies.
- Monitoring evidence is available through serializable decision payloads,
  production-state scoped fields, and execution validation metadata.

Still intentionally constrained:

- ProductionState is not an execution authorizer.
- EOD/platform flatten behavior is not broadened beyond existing close-only
  flows.
- Strategy order generation remains guarded by existing runtime gates plus the
  ExecutionService second-check.

## Execution Second Check

`ExecutionService` must call `TradabilityService` immediately before order
persistence/placement for entry and scale-in flows. Close, reduce, stop,
take-profit, trailing-stop, strategy-flatten, and protective-repair flows must
also request a decision so the override evidence is recorded.

`ProductionState` partial or `go` is never sufficient to place an order.

## Runtime Artifact Input Mapping

The production runtime uses `build_tradability_inputs_from_readiness_artifacts`
as a read-only adapter. It reads local JSON artifacts only and must not connect
to IBKR or query the database.

Mapped hard blockers:

- Missing/stale/not broker-backed broker truth -> `broker_truth_missing`,
  `broker_truth_stale`, or `broker_truth_not_broker_backed`.
- Position safety missing/stale/no_go -> `position_safety_missing`,
  `position_safety_stale`, or `position_safety_no_go`.
- Runtime freshness stale or loaded code mismatch ->
  `runtime_version_loaded_code_stale`.
- Manual recovery evidence in production_state failures ->
  `manual_recovery_required`.
- Order truth and ledger/broker divergence labels ->
  `order_truth_unreconciled` and `ledger_broker_divergence_global`.

Mapped scoped blockers:

- `production_state.blocked_scopes` entries with account, strategy instance,
  symbol, or session scope become `ScopedBlocker` records and are matched
  against the current `TradabilityScope`.

## Monitoring Examples

- `tradability_decision_total{flow_type,allow,halt_class,authority}`
- `tradability_blocked_scope_total{scope_type,blocker,flow_type}`
- `tradability_flow_override_total{flow_type,override}`
- `production_state_global_halt`
- `production_state_blocked_scope_count`
- `execution_tradability_second_check_rejected_total{flow_type,reason}`

## Failure Examples

NVDA post-market can be allowed under an `EXTENDED_EQUITY` strategy while an
RTH-only strategy on the same symbol is blocked for entry.

Broker truth missing is a global halt for all entry and normal flow overrides.

Manual lock for Strategy A blocks only Strategy A's matching strategy instance,
not Strategy B on the same symbol.

A protective stop can be allowed while entry is blocked by session policy when
owner identity is complete.

Runtime stale blocks all entry and normal close/repair overrides until restart,
except an explicitly manually authorized account emergency flow.

Account emergency flatten is explicit only and must carry manual/admin authority.

## Final Hardening Status

Final hardening moves the remaining coarse behavior into scoped evidence paths:

- `PlatformEODSupervisor` supports `SessionClosePolicy` inputs. When policies
  are present it calls strategy-scoped flatten paths for selected
  RTH/default-close policies instead of defaulting to account-wide flatten.
  Runtime startup derives those policies from enabled strategy production
  manifests so extended-session strategies are not selected by the RTH close
  pass.
- `MANUAL_LOCK` and `PLATFORM_EOD` failures are classified as entry/scoped
  blockers in production_state, not safety global halts. Broker truth, position
  safety, runtime freshness, manual recovery, order truth, ledger divergence,
  and safety-kernel emergency halt remain hard blockers.
- Stocks and options paused strategy paths emit `TradabilityDecision` evidence
  before returning from entry processing.
- Broker truth convergence inherits `strategy_instance_id`, `lot_id`, and
  `position_owner_key` from durable order notes when applying legacy/adopted
  fills to the position ledger.
- Alert rule draft artifact:
  `docs/reports/artifacts/scoped_tradability_alert_rules_20260512.json`.

Phase status:

- Phase 1 evaluator: done.
- Phase 2 production_state scoped output: done.
- Phase 3 execution second-check: done.
- Phase 4 strategy pause handling: hardened for stocks and options.
- Phase 5 EOD/session policy: hardened and wired through runtime startup.
- Phase 6 monitoring: artifact-level monitoring and alert draft documented.

Remaining architecture risk: none known. Operational risk remains around canary
review and dashboard deployment before broad production use.
