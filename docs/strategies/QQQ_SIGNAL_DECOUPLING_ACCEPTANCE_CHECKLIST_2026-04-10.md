# QQQ Signal Decoupling Acceptance Checklist (2026-04-10)

## Functional Acceptance

- [x] QQQ shared signal is published after common signal logic passes.
- [x] QQQ shared signal publication no longer depends on stock open-order gate.
- [x] QQQ shared signal publication no longer depends on stock live-position gate.
- [x] QQQ option upstream consumer can still translate LONG -> CALL and SHORT -> PUT.
- [x] Stock order submission still remains blocked by stock open orders.
- [x] Stock order submission still remains blocked by stock live position.

## Safety Acceptance

- [x] No stock-side risk control is removed.
- [x] No new duplicate stock order path is introduced.
- [x] Additive artifact metadata does not break current readers.
- [x] QQQ stock strategy tests still pass.

## Audit / Operability Acceptance

- [x] Artifact payload clearly identifies shared publication stage.
- [x] Tests explicitly prove decoupling behavior.
- [x] Review confirms this is a signal-layer fix, not a submit-layer rewrite.

## Release Decision

Release is acceptable only if all items above are checked and the focused test plan passes.
