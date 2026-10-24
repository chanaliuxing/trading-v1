# QQQ Loop Stall Protective Stop Hardening TODO / Test / Acceptance (2026-04-10)

## Scope

Fix only the `qqq_intraday_001` live-loop stall risk observed after `entry_intent_submitted`, where the loop remains stuck in `maybe_enter`.

Do not change:

- entry gate semantics
- entry sizing
- position ownership rules
- flatten logic
- broker reconciliation policy

## Root Cause Target

Current evidence shows one concrete stall path:

1. entry intent is submitted
2. `_maybe_enter()` continues into `_submit_protective_stop_intent("entry_protective_stop")`
3. protective stop path awaits `ctx.submit_intent(...)` without a timeout
4. if downstream blocks, the whole strategy loop blocks inside `maybe_enter`

## Implementation TODO

1. Add bounded timeout wrapper for `ctx.submit_intent(...)` in `QQQIntradayStrategy`.
2. Use that timeout wrapper for protective stop submit path first.
3. On timeout:
   - emit local audit event
   - emit protection-state failure evidence
   - return control to the strategy loop without raising out of `_maybe_enter()`
4. Keep existing rejection / dedupe behavior unchanged.
5. Do not broaden the timeout change to entry submit / flatten submit in this patch.

## Test Plan

### Unit

1. Protective stop submit timeout does not raise out of `_submit_protective_stop_intent()`.
2. Protective stop submit timeout does not mark dedupe signature as successfully submitted.
3. Generic submit-intent timeout wrapper emits `submit_intent_timeout` audit evidence.
4. Existing protective stop dedupe tests still pass.
5. Existing ctx-position timeout observability tests still pass.

### Runtime evidence

1. During next live session, if downstream stop submit blocks, audit must show:
   - `submit_intent_timeout`
   - `protective_stop_submit_timeout`
2. Strategy loop must continue producing later audit rows after the timeout event.
3. If entry filled but initial stop submit times out, stop-audit/self-heal path must still be able to place protection later.

## Acceptance Criteria

1. No code path in protective stop submit can block the strategy loop indefinitely on `ctx.submit_intent`.
2. `qqq_intraday_001` no longer remains stuck in one `iteration_seq` because of protective stop submit wait.
3. On timeout, there is explicit audit evidence naming:
   - symbol
   - timeout key
   - timeout seconds
   - order ref
4. Existing entry and flatten behavior remains unchanged in unit coverage.
5. Patch stays local to `qqq_intraday_strategy` plus targeted tests/documentation.
