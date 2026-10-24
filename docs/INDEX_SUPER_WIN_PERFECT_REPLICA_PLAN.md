# INDEX_SUPER_WIN Perfect Replica Plan

## 1. Objective

Build a new `index_super_win` that is behavior-equivalent to `qqq_intraday` while remaining isolated and safe to run in parallel.

Hard requirements:

1. Do not break current `qqq_intraday_001` production behavior.
2. Allow `index_super_win` to run alone or with `qqq` concurrently.
3. No position conflicts, no ghost orders, no repeated entries, no reverse entries.
4. Full-chain observability for trader-level "why entered / why blocked".

## 2. Current Gap (Fact-based)

Current `index_super_win` is not yet a perfect replica:

1. `qqq_intraday` has a loop/state-machine engine (`_run_loop`), but current `index_super_win` is feature-driven only.
2. `qqq_intraday` contains full exit/risk/protection-stop lifecycle; current `index_super_win` focuses on entry orchestration and isolation.
3. `qqq_intraday` has ATR/VWAP/QQEW-rich gating, restart recovery, stop-audit kill-switch; these are not fully mirrored.

References:

- `./strategies/qqq_intraday_strategy.py`
- `./strategies/index_super_win_strategy.py`
- `./strategies/index_super_win_components.py`

## 3. Strategy for "Perfect Replica"

Use a two-layer approach:

1. **Replica Core (behavior-equal)**: copy `qqq_intraday` decision and risk lifecycle into a dedicated replica core module, preserving behavior.
2. **Isolation Adapter (safety boundary)**: keep runtime/order namespace/state paths isolated from `qqq`.

This avoids editing existing `qqq` logic directly while still achieving parity.

## 4. Architecture Plan

### 4.1 New modules

1. `strategies/index_super_win_replica_core.py`
2. `strategies/index_super_win_replica_strategy.py`
3. `services/strategy_runtime_v2/replica_parity_recorder.py`
4. `tool/replay/qqq_index_parity_replay.py`

### 4.2 Boundary rules

1. Keep `qqqv1:*` untouched for `qqq_intraday`.
2. New strategy uses `iswv2:*` namespace only.
3. Separate runtime scope/state files for replica.
4. No shared mutable strategy-level state between qqq and index.

## 5. Phased Execution

## Phase A: Parity Spec Freeze

Goal: define exactly what "perfect replica" means.

Tasks:

1. Freeze `qqq_intraday` strategy version hash and config baseline.
2. Freeze comparison fields:
   - Entry signal decision (enter/no-enter).
   - Side.
   - Target qty.
   - Entry timestamp bucket.
   - Exit reason (`stop_hit`, `tp1_then_trailing_stop`, `force_flatten_1559_EOD`, etc.).
   - Protective stop intent submit / dedupe / rebuild behavior.
   - Pending-entry clear / timeout / cancel behavior.
   - Position reconcile transitions (`flat -> live`, `live -> flat`, restart restore).
   - Open-order cleanup and orphan-cancel behavior.
3. Define tolerances:
   - Side equality: 100%
   - Entry block reason equality: 100%
   - Qty tolerance: <= 1 share
   - Decision time tolerance: <= 1 feature/tick cycle
   - Protective-stop side effect equality: 100%
   - Restart recovery state transition equality: 100%

Deliverable:

- parity spec doc + fixed baseline config.

## Phase B: Replica Core Build

Goal: implement behavior-equivalent core without touching live `qqq`.

Tasks:

1. Port `qqq_intraday` decision engine sections into `index_super_win_replica_core.py`.
2. Port lifecycle logic:
   - entry gating
   - stop/TP/trailing
   - flatten flow
   - protective stop submit + audit loop
   - restart recovery state load/save
3. Keep function signatures deterministic and testable.

Guardrails:

1. No behavior "optimization" during porting.
2. All differences must be explicit and reviewed.

## Phase C: Isolation Adapter

Goal: ensure parallel safety and no conflict with `qqq`.

Tasks:

1. Use independent `strategy_id` and runtime scope.
2. Use independent `order_ref` namespace (`iswv2`).
3. Enforce ownership/quarantine on unknown broker positions/open orders.
4. Ensure one IB connection manager shared process-level, but strategy-level order ownership isolated.

## Phase D: Deterministic Parity Test Suite

Goal: prove equivalence before shadow.

Tasks:

1. Add replay harness that feeds identical market snapshots/features into:
   - `qqq_intraday`
   - `index_super_win_replica`
2. Replay input must include:
   - tick/SFS snapshots used by `qqq_intraday`
   - broker positions snapshots
   - open orders snapshots
   - order update events
   - deterministic clock / loop cadence
   - restart checkpoints
3. Compare emitted intents/actions and side effects event-by-event.
4. Add mismatch reports by category:
   - signal gate mismatch
   - qty mismatch
   - timing mismatch
   - exit reason mismatch
   - protective-stop mismatch
   - reconcile/recovery mismatch
   - orphan cleanup mismatch

Pass criteria:

1. Zero side mismatch.
2. Zero block-reason mismatch.
3. Qty mismatch <= tolerance.
4. No extra/duplicate entry actions.

## Phase E: Shadow in Live Runtime

Goal: validate under real-time noise.

Tasks:

1. Run `index_super_win` in dry-run shadow with `qqq` live.
2. Collect full-day parity reports for at least 3 trading days.
3. Validate no cross-strategy conflicts:
   - no ownership stealing
   - no wrong orphan cleanup
   - no reverse/duplicate intents

Pass criteria:

1. Parity ratio >= 99.9% by action count.
2. Zero critical mismatch (side/reason conflict).
3. Zero operational incidents (ghost orders, cross-cancel, forced quarantine storm).

## Phase F: Controlled Live Cut-in

Goal: low-risk activation.

Tasks:

1. Single-symbol, small notional live.
2. Multi-symbol dry-run parallel.
3. Multi-symbol low-notional live.
4. Keep immediate rollback switch at config level.

Rollback triggers:

1. Any position conflict.
2. Any unexpected reverse entry.
3. Any protective stop missing > threshold.
4. Any broker/runtime divergence not auto-healed.

## 6. Test Plan

## 6.1 Unit

1. Gate-equivalence tests (entry blocked reasons).
2. Sizing-equivalence tests.
3. Stop/TP/trailing transition tests.
4. Restart recovery state transition tests.
5. Order namespace/identity isolation tests.

## 6.2 Integration

1. Parallel run with qqq under same symbols.
2. Broker non-zero unknown inventory -> quarantine path.
3. Open-order unknown namespace -> quarantine path.
4. Reconnect/restart with pending state.

## 6.3 E2E Replay

1. Replay historical sessions with known qqq actions.
2. Generate daily parity reports:
   - total decisions
   - matched decisions
   - mismatch list with root-cause tags.

## 7. Acceptance Checklist

All must be true:

1. `qqq_intraday` unchanged in behavior and code path.
2. `index_super_win` reproduces qqq decisions/exits under defined tolerance.
3. Can run standalone when qqq disabled.
4. Can run concurrently with qqq without conflicts.
5. Observability can answer trader questions for every blocked/opened decision.
6. Smoke + unit + replay suites all green.

## 8. Risk Notes (Critical)

1. "Perfect replica" may also replicate existing qqq weaknesses.
2. If we change logic while porting, parity and safety both degrade.
3. Therefore, first target is **behavior parity**, then separate hardening phase.

## 9. Proposed Execution Order (for approval)

1. Approve parity spec and tolerances.
2. Implement minimal Phase C boundary first:
   - namespace
   - state path isolation
   - runtime scope isolation
   - ownership/quarantine guard
3. Then implement Phase B + D (core + deterministic parity).
4. Then do Phase E (shadow).
5. Finally controlled live cut-in (Phase F).

## 10. Optimization Notes

1. Do not start from a generic feature adapter. Start from a `qqq_intraday` behavioral slice map and port in bounded slices:
   - startup/recovery
   - entry gate
   - order submit/pending
   - position reconcile
   - stop protection
   - exit management
2. Every slice must land with parity tests before the next slice starts.
3. Shadow mode must compare not only "entered / not entered" but also:
   - stop placement timing
   - flatten cause
   - open-order cleanup actions
4. No live cut-in until same-symbol parallel shadow passes; different-symbol success is not sufficient proof.
