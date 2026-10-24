from types import SimpleNamespace

from strategies.stocks_kernel.modules.safety_invariants import (
    FlattenCompletionVerifier,
    ProtectionInvariantWatchdog,
)


def test_flatten_completion_verifier_breach_after_timeout():
    verifier = FlattenCompletionVerifier(timeout_sec=90.0)
    verifier.mark_flatten_submitted(symbol="SOXL", position_lot_id="lot-1", now_ts_epoch_s=100.0)

    breaches = verifier.sweep(now_ts_epoch_s=191.0)

    assert len(breaches) == 1
    assert breaches[0].symbol == "SOXL"
    assert breaches[0].reason == "flatten_not_converged_timeout"
    assert verifier.sweep(now_ts_epoch_s=260.0) == ()


def test_flatten_completion_verifier_can_clear_specific_lot():
    verifier = FlattenCompletionVerifier(timeout_sec=90.0)
    verifier.mark_flatten_submitted(symbol="SOXL", position_lot_id="lot-1", now_ts_epoch_s=100.0)
    verifier.clear_lot(symbol="SOXL", position_lot_id="lot-1")

    assert verifier.sweep(now_ts_epoch_s=191.0) == ()


def test_protection_invariant_watchdog_escalates_missing_stop():
    watchdog = ProtectionInvariantWatchdog(detect_sec=10.0, escalate_sec=30.0)
    lot = SimpleNamespace(
        position_lot_id="lot-2",
        symbol="SOXS",
        state="live",
        live_qty=3.0,
        protection_order_refs=[],
    )

    assert watchdog.evaluate(lots=(lot,), now_ts_epoch_s=100.0) == ()
    warning = watchdog.evaluate(lots=(lot,), now_ts_epoch_s=111.0)
    error = watchdog.evaluate(lots=(lot,), now_ts_epoch_s=131.0)

    assert len(warning) == 1 and warning[0].escalate is False
    assert len(error) == 1 and error[0].escalate is True


def test_protection_invariant_watchdog_allows_active_flatten():
    watchdog = ProtectionInvariantWatchdog(detect_sec=10.0, escalate_sec=30.0)
    lot = SimpleNamespace(
        position_lot_id="lot-3",
        symbol="NVDA",
        state="pending_exit",
        live_qty=1.0,
        protection_order_refs=[],
        flatten_order_refs=["nvda_flip:alpha:NVDA:FLATTEN:lot-3"],
    )

    assert watchdog.evaluate(lots=(lot,), now_ts_epoch_s=100.0) == ()
    assert watchdog.evaluate(lots=(lot,), now_ts_epoch_s=200.0) == ()
