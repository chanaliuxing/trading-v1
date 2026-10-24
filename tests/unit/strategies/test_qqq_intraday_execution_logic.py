from strategies.qqq_intraday_execution_logic import (
    action_submit_signature,
    should_submit_action,
    flatten_target_qty,
    should_submit_stop,
    signed_position_qty,
    stop_submit_signature,
    submit_outcome,
)


def test_submit_outcome_approved_and_rejected() -> None:
    ok = submit_outcome(status="APPROVED", ok_status="FLATTEN_SUBMITTED", rejected_status="FLATTEN_REJECTED", ok_reason="flatten")
    bad = submit_outcome(status="REJECTED", ok_status="FLATTEN_SUBMITTED", rejected_status="FLATTEN_REJECTED", ok_reason="flatten")
    assert ok.approved is True
    assert ok.preview_status == "FLATTEN_SUBMITTED"
    assert ok.reason == "flatten"
    assert bad.approved is False
    assert bad.preview_status == "FLATTEN_REJECTED"
    assert bad.reason == "submit:REJECTED"


def test_signed_position_qty_and_flatten_target_qty() -> None:
    assert signed_position_qty(live_qty=10, side="LONG") == 10.0
    assert signed_position_qty(live_qty=10, side="SHORT") == -10.0
    assert flatten_target_qty(qty_signed=-10.0) == 10.0


def test_stop_dedupe_signature_and_decision() -> None:
    sig = stop_submit_signature(exec_symbol="tqqq", qty=10, stop_px=48.1234)
    assert sig == "TQQQ:10:48.1234"
    deduped = should_submit_stop(last_sig=sig, submit_sig=sig, last_submit_mono=100.0, now_mono=101.0, dedupe_sec=3.0)
    fresh = should_submit_stop(last_sig=sig, submit_sig=sig, last_submit_mono=100.0, now_mono=104.5, dedupe_sec=3.0)
    assert deduped.should_submit is False and deduped.is_duplicate is True
    assert fresh.should_submit is True and fresh.is_duplicate is False


def test_action_dedupe_signature_and_decision() -> None:
    sig = action_submit_signature(exec_symbol="sqqq", qty=8, action="kill_switch")
    assert sig == "SQQQ:8:KILL_SWITCH"
    deduped = should_submit_action(last_sig=sig, submit_sig=sig, last_submit_mono=10.0, now_mono=11.0, dedupe_sec=3.0)
    fresh = should_submit_action(last_sig=sig, submit_sig=sig, last_submit_mono=10.0, now_mono=14.2, dedupe_sec=3.0)
    assert deduped.should_submit is False and deduped.is_duplicate is True
    assert fresh.should_submit is True and fresh.is_duplicate is False
