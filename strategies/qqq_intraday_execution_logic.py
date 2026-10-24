from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SubmitOutcome:
    approved: bool
    preview_status: str
    audit_stage: str
    audit_status: str
    audit_severity: str
    reason: str


@dataclass(frozen=True)
class StopDedupeDecision:
    should_submit: bool
    is_duplicate: bool


def submit_outcome(*, status: str | None, ok_status: str, rejected_status: str, ok_reason: str) -> SubmitOutcome:
    return submit_outcome_with_stage(
        status=status,
        ok_status=ok_status,
        rejected_status=rejected_status,
        ok_reason=ok_reason,
        ok_audit_stage=ok_status.lower(),
        rejected_audit_stage=rejected_status.lower(),
    )


def submit_outcome_with_stage(
    *,
    status: str | None,
    ok_status: str,
    rejected_status: str,
    ok_reason: str,
    ok_audit_stage: str,
    rejected_audit_stage: str,
) -> SubmitOutcome:
    status_u = str(status or "").strip().upper()
    approved = status_u in {"", "APPROVED"}
    return SubmitOutcome(
        approved=approved,
        preview_status=ok_status if approved else rejected_status,
        audit_stage=ok_audit_stage if approved else rejected_audit_stage,
        audit_status="submitted" if approved else "rejected",
        audit_severity="info" if approved else "warning",
        reason=ok_reason if approved else f"submit:{status_u or 'UNKNOWN'}",
    )


def signed_position_qty(*, live_qty: int | float, side: str | None) -> float:
    qty = float(max(0.0, float(live_qty or 0.0)))
    side_u = str(side or "").strip().upper()
    return -qty if side_u == "SHORT" else qty


def flatten_target_qty(*, qty_signed: int | float) -> float:
    return float(-float(qty_signed or 0.0))


def stop_submit_signature(*, exec_symbol: str, qty: int | float, stop_px: int | float) -> str:
    return f"{str(exec_symbol or '').strip().upper()}:{int(max(0, int(float(qty or 0.0))))}:{round(float(stop_px or 0.0), 4)}"


def should_submit_stop(*, last_sig: str | None, submit_sig: str, last_submit_mono: float, now_mono: float, dedupe_sec: float) -> StopDedupeDecision:
    if str(last_sig or "") == str(submit_sig or "") and float(dedupe_sec) > 0.0 and (float(now_mono) - float(last_submit_mono or 0.0)) < float(dedupe_sec):
        return StopDedupeDecision(should_submit=False, is_duplicate=True)
    return StopDedupeDecision(should_submit=True, is_duplicate=False)


def action_submit_signature(*, exec_symbol: str, qty: int | float, action: str) -> str:
    return f"{str(exec_symbol or '').strip().upper()}:{int(max(0, int(float(qty or 0.0))))}:{str(action or '').strip().upper()}"


def should_submit_action(*, last_sig: str | None, submit_sig: str, last_submit_mono: float, now_mono: float, dedupe_sec: float) -> StopDedupeDecision:
    return should_submit_stop(
        last_sig=last_sig,
        submit_sig=submit_sig,
        last_submit_mono=last_submit_mono,
        now_mono=now_mono,
        dedupe_sec=dedupe_sec,
    )
