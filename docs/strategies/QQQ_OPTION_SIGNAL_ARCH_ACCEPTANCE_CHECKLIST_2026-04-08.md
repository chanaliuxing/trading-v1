# QQQ Option Signal Architecture Acceptance Checklist (2026-04-08)

## A. Architecture Invariants

- [x] A1. `QQQIntradayStrategy` trading logic remains the stable baseline during `P0`.
- [x] A2. Stock signal publication is added without changing stock-side entry/exit semantics.
- [x] A3. `QQQ option` live entry authority uses exactly one `signal_source.mode`.
- [x] A4. `fallback_mode` does not silently become a live writer by default.
- [x] A5. Signal artifact is distinct from stock order intent / order envelope.
- [x] A6. `OptionKernel` execution modules do not depend on signal source implementation details.

## B. Signal Source Contract

- [x] B1. `upstream_stock_signal` emits normalized direction and source identity.
- [x] B2. `independent_rule_replay` emits the same normalized schema.
- [x] B3. Live source mode is visible in heartbeat / audit.
- [x] B4. Source signal age is visible in heartbeat / audit.
- [x] B5. Missing source signal produces explicit non-trade reason.
- [x] B6. Stale source signal produces explicit non-trade reason.

## C. Shared State / Fabric Discipline

- [x] C1. Shared memory contains reusable market truth only.
- [x] C2. Shared memory does not contain cross-strategy order ownership state.
- [x] C3. Replay source reads from shared market state, not a private duplicated runtime.
- [x] C4. Stock and option can both read the same underlying feature context.
- [x] C5. One underlying subscription path can serve multiple strategy consumers in the same process/fabric.
- [x] C6. Stock-truth logic is not derived from option-chain state in production.

## D. Live Trading Safety

- [x] D1. No duplicate option entries occur from dual signal writers.
- [x] D2. Shadow replay mode cannot submit live orders.
- [x] D3. Source-mode failure does not silently trigger same-minute replay entry.
- [x] D4. Option contract selection and execution still run after valid source signal.
- [x] D5. Non-trade minutes remain explainable after architecture change.

## E. QQQ Behavior Consistency

- [x] E1. Stock `entry_allowed` still appears on the same known baseline samples.
- [x] E2. Stock strategy replay/parity regression tests remain green.
- [x] E3. When stock emits `LONG`, option maps to `CALL`.
- [x] E4. When stock emits `SHORT`, option maps to `PUT`.
- [x] E5. Option no longer misses stock signal minutes due to missing signal-source input path.

## F. Audit / Explainability

- [x] F1. Heartbeat includes `signal_source_mode`.
- [x] F2. Heartbeat includes `signal_source_status`.
- [x] F3. Heartbeat includes `signal_source_age_sec`.
- [x] F4. Heartbeat includes `signal_source_strategy_id` or equivalent source identity.
- [x] F5. Downstream result includes one of: `signal_consumed`, `source_signal_missing`, `source_signal_stale`, `no_contract_selection`, `entry_intent_submitted`.
- [x] F6. There is no silent "heartbeat only, no reason" failure mode.

## G. Test Evidence

- [x] G1. Signal artifact unit tests pass.
- [x] G2. Signal source contract unit tests pass.
- [x] G3. Single-writer / fallback rules tests pass.
- [x] G4. `QQQ` stock stable-path regression tests pass.
- [x] G5. Option consumer integration tests pass.
- [x] G6. Upstream-signal flow integration tests pass.
- [x] G7. Shadow drift integration tests pass.

## H. Runtime Acceptance (Paper Trading)

- [ ] H1. Pre-open both stock and option strategies are alive.
- [ ] H2. Option strategy reports `signal_source.mode=upstream_stock_signal`.
- [ ] H3. During open-session, stock signal artifacts are visible.
- [ ] H4. For each stock signal minute, option strategy emits a source-aware downstream result.
- [ ] H5. At least one known valid stock signal minute reaches option submit path, unless blocked by explicit downstream reason.
- [ ] H6. No duplicate submit caused by source ambiguity is observed.
- [ ] H7. No duplicate underlying `QQQ` subscription path is observed in the same runtime process.

## Final Go / No-Go Rule

- [ ] GO only if sections A-G pass and H has no critical runtime failure.
- [ ] NO-GO if any of these remain: more than one live signal writer, silent fallback live entry, stock baseline drift, unexplained non-trade minutes.
