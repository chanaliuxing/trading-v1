# Stocks Kernel IB Broker Truth Remediation TODO / Test Plan / Acceptance (2026-04-10)

## 1. Purpose

This document closes two different problems that must not be mixed together:

1. `P1` live runtime slowness caused by `IB open/completed orders` refresh and fresh-probe paths.
2. `P2` remaining architecture gap where `stocks_kernel` still picks the default assembly internally instead of receiving it from an outer assembler / DI boundary.

The first problem is a live trading correctness and runtime safety issue.
The second problem is a platform modularity issue.

They must be fixed in that order:

1. stop the live blocking path first
2. preserve rollback and compatibility
3. only then finish the assembly boundary closure

## 2. Evidence

### 2.1 Live evidence for the real `P1`

Observed in `/workspace/grafana-stack/logs/trader.log` on `2026-04-10`:

1. `stocks_kernel_shadow_smh_001` repeatedly logs:
   - `Strategy execution slow`
   - typical spikes around `~10.5s`
2. Immediately before those spikes, the same log shows:
   - `completed orders request timed out`
   - `ib_probe_order_open_fresh_result`
   - `ib_open_orders_snapshot`
3. Sample order refs in those snapshots belong to:
   - `sl:stocks_kernel_shadow_smh_001:SOXS:...`
   - `tp:stocks_kernel_shadow_smh_001:SOXS:...`
4. Earlier in the day, the same adapter path also timed out for:
   - `qqq_1dte_ma10_reject_0324_v2_001`
   - sample refs:
     - `tp:qqq_1dte_ma10_reject_0324_v2_001:OPT:QQQ:2`
     - `sl:qqq_1dte_ma10_reject_0324_v2_001:OPT:QQQ:2`

Conclusion:

1. The slowness is not specific to one strategy family.
2. The shared bottleneck is the IB adapter `force_refresh + per-order fresh probe` path.
3. `stocks_kernel_shadow_smh_001` is the current visible victim because it still invokes that path during RTH while holding active protective orders.

### 2.2 Code evidence for the real `P1`

In `./services/exec/adapters/ib_adapter.py`:

1. `open_orders(force_refresh=True)` iterates all open orders.
2. For each order it calls `probe_order_open_fresh(order_id)`.
3. `probe_order_open_fresh()` opens a new IB session and requests fresh open orders.
4. This is serial, blocking, and multiplicative with open order count.

In `./strategies/stocks_kernel/strategy.py`:

1. `broker_truth_sync` still calls `adapter.open_orders(force_refresh=True)`.
2. During RTH it can run frequently enough to repeatedly hit the expensive path.

### 2.3 Code evidence for the remaining `P2`

In `./strategies/stocks_kernel/strategy.py`:

1. the constructor no longer directly instantiates default gate/policy registries
2. but `initialize()` still calls `build_default_kernel_assembly()`

In `./strategies/stocks_kernel/assembly.py`:

1. `build_default_kernel_assembly()` still directly chooses:
   - `default_gate_registry()`
   - `default_policy_registry()`
   - `default_runtime_stage_registry()`

Conclusion:

1. the original review comment about `strategy.py:91-93` is stale
2. the deeper architectural point is still partially true
3. but this is not the cause of the live slowdown

## 3. Non-Negotiable Constraints

Any remediation must obey all of the following:

1. no destructive behavior to existing live / paper strategies
2. no big-bang switch
3. every `P1` runtime change must have:
   - feature flag
   - fallback path
   - explicit audit signal
4. broker truth cannot be weakened into optimistic guessing
5. slow-path reduction cannot silently suppress real broker mismatches
6. untouched strategies must remain behaviorally equivalent by default

## 4. Target End State

### 4.1 Runtime end state

1. `open_orders(force_refresh=True)` is bounded and non-pathological.
2. the adapter never opens one fresh IB probe connection per open order in the hot path.
3. strategy runtime no longer blocks for `~10s` just to re-check protective children.
4. broker truth still remains authoritative.

### 4.2 Assembly end state

1. `stocks_kernel` accepts an assembly object or assembly provider from outside the core strategy.
2. the strategy core no longer chooses the default assembly by itself.
3. builtin assembly remains available through an outer bootstrap layer for compatibility.

## 5. Remediation Strategy

## 5.1 Phase P1-A: Stop the pathological IB adapter hot path

### Objective

Remove the per-order fresh-probe fan-out from `open_orders(force_refresh=True)`.

### Required changes

1. Introduce a bounded refresh mode in the IB adapter:
   - `force_refresh=True` may refresh broker open orders once
   - it must not open a new IB probe session for each order by default
2. Split semantics:
   - `refresh broker cache`
   - `diagnostic fresh probe`
3. Keep fresh probe only for:
   - explicit anomaly remediation
   - manual diagnostics
   - hard mismatch escalation paths
4. Add a hard cap if any probe fallback remains:
   - max probe order count
   - max total probe budget per call
   - timeout budget

### Safety gate

Default runtime behavior must stay conservative:

1. if cache refresh succeeds, use that result
2. only escalate to diagnostic probe when mismatch classification requires it
3. never silently ignore mismatch without audit

## 5.2 Phase P1-B: Change strategy truth-sync usage from “always heavy” to “tiered”

### Objective

Make `stocks_kernel` use broker truth in a tiered manner.

### Required changes

1. Add strategy/runtime policy for truth sync modes:
   - `light`
   - `diagnostic`
   - `recovery_only`
2. In normal RTH ticks:
   - use `light` refresh only
3. Only switch to `diagnostic` when one of the following is true:
   - broker/order lifecycle mismatch already detected
   - protection invariant broken
   - orphan / stale pending suspicion
   - startup recovery path
4. Add per-strategy throttles for:
   - normal truth sync
   - escalated diagnostic truth sync

### Safety gate

The strategy must still fail closed on unresolved mismatch:

1. freeze entry when broker truth is suspect
2. do not unblock merely because the heavy probe was skipped

## 5.3 Phase P1-C: Add explicit observability proving the fix

### Objective

Make runtime and adapter behavior provable from logs and metrics.

### Required changes

1. Add adapter metrics / logs:
   - `ib_open_orders_refresh_ms`
   - `ib_open_orders_refresh_count`
   - `ib_open_orders_diagnostic_probe_count`
   - `ib_open_orders_diagnostic_probe_ms`
2. Add strategy audit fields:
   - `broker_truth_sync_mode`
   - `broker_truth_sync_reason`
   - `broker_truth_sync_order_count`
   - `broker_truth_sync_escalated`
3. Distinguish:
   - `cache_refresh_path`
   - `diagnostic_probe_path`

### Safety gate

If the new path is active, logs must make it obvious which path ran.

## 5.4 Phase P2-A: Finish the outer assembly boundary

### Objective

Move default assembly selection out of the strategy core.

### Required changes

1. Strategy constructor should accept one of:
   - `assembly`
   - `assembly_provider`
2. If absent, outer bootstrap layer injects builtin assembly
3. `strategy.py` core should no longer call `build_default_kernel_assembly()` directly
4. Builtin assembly remains in a separate bootstrap/pack module

### Safety gate

1. existing config path must continue to work
2. no current strategy should require config changes just to start

## 6. TODO List

## 6.1 P1 live remediation TODO

- [ ] Refactor `./services/exec/adapters/ib_adapter.py` so `open_orders(force_refresh=True)` does not fresh-probe each order.
- [ ] Introduce explicit diagnostic API, e.g. `probe_open_orders_diagnostic()` or equivalent.
- [ ] Add a bounded probe policy:
  - max probe count
  - total timeout budget
  - anomaly-only trigger
- [ ] Refactor `./strategies/stocks_kernel/strategy.py` truth-sync stage to use normal/light mode in steady-state RTH.
- [ ] Restrict heavy broker truth diagnostics to startup recovery, protection mismatch, or explicit anomaly.
- [ ] Add observability fields proving which truth-sync path ran.
- [ ] Add regression coverage for active protective-order scenarios.

## 6.2 P2 assembly closure TODO

- [ ] Introduce injectable assembly boundary in `stocks_kernel` strategy construction.
- [ ] Move builtin default assembly selection to outer bootstrap layer.
- [ ] Keep compatibility adapter for existing strategy loading path.
- [ ] Add tests proving strategy core can run with injected custom assembly without editing core.

## 7. Test Plan

## 7.1 Unit tests

### Adapter tests

File targets:

1. `./tests/unit/services/exec/test_ib_adapter_open_orders.py`
2. add new targeted adapter tests if needed

Cases:

1. `force_refresh=True` refreshes broker cache once and does not per-order probe in normal mode.
2. diagnostic probe path runs only when explicitly requested.
3. multiple open orders do not multiply fresh IB sessions in steady-state path.
4. timeout / exception in diagnostic probe does not corrupt normal open-orders snapshot.
5. local protective pending filtering still works after path split.

### Strategy tests

File targets:

1. `./tests/unit/strategies/test_stocks_kernel_policy_and_strategy.py`
2. `./tests/unit/strategies/test_stocks_kernel_system_bridge.py`

Cases:

1. normal RTH broker truth sync uses light mode.
2. anomaly path escalates to diagnostic mode.
3. unresolved broker mismatch still freezes entry / kill-switches as before.
4. steady-state active protective orders do not trigger heavy diagnostic path on every sync.
5. offsession throttle remains strategy-scoped and unchanged for untouched strategies.

### Assembly tests

File targets:

1. `./tests/unit/strategies/test_stocks_kernel_policy_registry.py`
2. `./tests/unit/strategies/test_stocks_kernel_gates.py`
3. `./tests/unit/strategies/test_stocks_kernel_first_principles_contracts.py`

Cases:

1. strategy accepts injected assembly object.
2. builtin bootstrap still assembles the same default packs.
3. no direct default assembly selection remains inside strategy core.

## 7.2 Integration tests

Target files:

1. `./tests/integration`
2. add dedicated integration test if necessary

Cases:

1. strategy with active stop/tp child orders runs repeated ticks without `~10s` blocking.
2. broker mismatch still transitions into recovery/freeze path.
3. `qqq_1dte_ma10_reject_0324_v2_001` option protection orders remain visible to reconciliation logic after adapter refactor.
4. `stocks_kernel_shadow_smh_001` protective orders remain visible and recoverable after adapter refactor.

## 7.3 Live-paper smoke scripts

These are runbook scripts/commands, not necessarily committed as a single new test file.

### Script group A: targeted unit regression

```bash
./venv_ml/bin/pytest -q \
  tests/unit/services/exec/test_ib_adapter_open_orders.py \
  tests/unit/strategies/test_stocks_kernel_policy_and_strategy.py \
  tests/unit/strategies/test_stocks_kernel_system_bridge.py
```

### Script group B: stocks kernel regression

```bash
./venv_ml/bin/pytest -q \
  tests/unit/strategies/test_stocks_kernel_*.py
```

### Script group C: targeted live log verification after restart

```bash
rg -n 'Strategy execution slow|completed orders request timed out|ib_open_orders_snapshot|stocks_kernel_shadow_smh_001|qqq_1dte_ma10_reject_0324_v2_001' \
  /workspace/grafana-stack/logs/trader.log | tail -n 300
```

### Script group D: strategy-specific slow-count check

```bash
rg -n '"strategy_id": "stocks_kernel_shadow_smh_001".*"Strategy execution slow"' \
  /workspace/grafana-stack/logs/trader.log | tail -n 50
```

## 8. Acceptance Criteria

## 8.1 P1 runtime acceptance

All must pass:

1. For steady-state protective orders, `open_orders(force_refresh=True)` no longer triggers one fresh IB connection per order.
2. In live-paper logs, `stocks_kernel_shadow_smh_001` no longer shows periodic `~10s` slow spikes caused by open-order probing.
3. `completed orders request timed out` frequency from the steady-state open-orders path is reduced to zero or only anomaly-only cases.
4. Broker mismatch handling still works:
   - mismatch is audited
   - entry freeze still applies
   - no optimistic continue
5. Existing protective children for both:
   - `stocks_kernel_shadow_smh_001`
   - `qqq_1dte_ma10_reject_0324_v2_001`
   remain visible/recoverable.

## 8.2 P2 architecture acceptance

All must pass:

1. `strategy.py` core no longer chooses builtin assembly directly.
2. outer loader/bootstrap injects default assembly.
3. custom injected assembly can run without editing strategy core.
4. current config-only startup path still works unchanged.

## 8.3 Compatibility acceptance

All must pass:

1. When new truth-sync modes are disabled or not configured, untouched strategies behave equivalently.
2. No regression in:
   - `qqq_intraday_001`
   - `qqq_1dte_ma10_reject_0324_v2_001`
   - `qqq_intraday_option_v1_001`
   - `spy_1dte_ma10_reject_0324_001`
3. Existing reconciliation, protection, and notification flows still work.

## 9. Rollout Gates

No implementation is allowed to skip these:

1. introduce adapter split behind explicit flag or safe-default bounded behavior
2. land unit tests first
3. land integration tests second
4. enable on `stocks_kernel_shadow_smh_001` first
5. observe live-paper logs for at least one active protective-order window
6. only after proof, expand to other strategies if needed

## 10. Done Definition

This issue is only considered done when:

1. the live `~10s` periodic slow spikes disappear for the target strategy under active protective orders
2. broker truth semantics are preserved
3. no regression is introduced to existing option or legacy stock strategies
4. the remaining assembly default-selection gap is closed with tests

Until all four are true, the work is not complete.
