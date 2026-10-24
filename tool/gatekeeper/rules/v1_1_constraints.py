from __future__ import annotations

from core.context import GatekeeperContext
from core.types import StepResult


RULE_META = {
    "step_id": "v1_1_constraints",
    "step_title": "v1.1 Constraints (Event/Skew/BofA)",
    "requires": ["event_level", "skew_value"],
    "optional": ["bofa_9_3", "event_confirmed"],
    "provides": [
        "flags.forbid_credit",
        "flags.event_defensive_overlay",
        "flags.bofa_defensive_overlay",
        "flags.skew_put_elevated",
        "flags.prefer_ic",
        "constraints.forbidden_structures",
        "constraints.notes",
    ],
    "stop_on_fail": False,
    "config_keys": [],
}


def _norm_event_level(raw: object) -> str:
    val = str(raw or "").strip().lower()
    if val in {"high", "h", "true", "yes", "1"}:
        return "High"
    if val in {"medium", "m"}:
        return "Medium"
    return "None"


def run(context: GatekeeperContext) -> StepResult:
    event_level = _norm_event_level(context.value("event_level", "None"))
    event_confirmed = bool(context.value("event_confirmed", False))
    if event_confirmed:
        event_level = "High"

    bofa_9_3 = context.value("bofa_9_3", None)
    bofa_val = None
    try:
        if bofa_9_3 is not None:
            bofa_val = float(bofa_9_3)
    except (TypeError, ValueError):
        bofa_val = None

    rr25 = context.value("skew_value", None)
    rr25_val = None
    try:
        if rr25 is not None:
            rr25_val = float(rr25)
    except (TypeError, ValueError):
        rr25_val = None

    forbid_credit = event_level == "High"
    event_defensive_overlay = event_level == "Medium"
    bofa_defensive_overlay = bool(bofa_val is not None and bofa_val >= 8.0)

    skew_put_elevated = False
    prefer_ic = False
    forbidden: set[str] = set()
    notes: list[str] = []

    symbol = context.symbol.upper().strip()
    if rr25_val is None:
        notes.append("RR25 missing: please input manually (skew_value)")
    else:
        if symbol in {"QQQ", "SPY"}:
            if rr25_val > 7:
                forbidden |= {"BPS", "IC", "TINY_IC"}
                notes.append("RR25>7 (Index): extreme put skew -> forbid BPS + IC")
            elif 5 <= rr25_val <= 7:
                skew_put_elevated = True
                notes.append("RR25 5-7 (Index): put skew elevated -> defensive overlay")
        elif symbol in {"META", "TSLA", "NVDA"}:
            if rr25_val > 6:
                forbidden |= {"BPS"}
                notes.append("RR25>6 (High vol): extreme put skew -> forbid BPS")
            elif rr25_val < -2:
                forbidden |= {"BCS"}
                notes.append("RR25<-2 (High vol): call FOMO -> forbid BCS")
            elif -2 <= rr25_val <= 2:
                prefer_ic = True
                notes.append("RR25 flat (-2~2): prefer IC (not forced)")

    if event_level == "High":
        forbidden |= {"BPS", "BCS", "IC", "TINY_IC"}
        notes.append("Event=High -> forbid all credit structures (BPS/BCS/IC)")
    elif event_level == "Medium":
        notes.append("Event=Medium -> defensive overlay; IC not recommended (alt only)")

    if bofa_defensive_overlay:
        notes.append("BofA>=8 -> defensive overlay (smaller size / farther delta)")

    outputs_set = {
        "flags.forbid_credit": forbid_credit,
        "flags.event_defensive_overlay": event_defensive_overlay,
        "flags.bofa_defensive_overlay": bofa_defensive_overlay,
        "flags.skew_put_elevated": skew_put_elevated,
        "flags.prefer_ic": prefer_ic,
        "constraints.forbidden_structures": sorted(forbidden),
        "constraints.notes": notes,
    }

    explain = [
        f"EventLevel={event_level} (confirmed={'YES' if event_confirmed else 'NO'}) -> forbid_credit={forbid_credit}",
        f"RR25={rr25_val if rr25_val is not None else 'MISSING'} -> forbidden={sorted(forbidden) if forbidden else '[]'}",
        f"BofA={bofa_val if bofa_val is not None else 'MISSING'} -> bofa_defensive_overlay={bofa_defensive_overlay}",
    ]
    return StepResult(
        step_id="v1_1_constraints",
        step_title="v1.1 Constraints (Event/Skew/BofA)",
        status="PASS",
        stop=False,
        severity="INFO" if not notes else "WARN",
        outputs_set=outputs_set,
        metrics={
            "event_level": event_level,
            "rr25": rr25_val,
            "bofa_9_3": bofa_val,
            "forbid_credit": forbid_credit,
        },
        explain=explain,
        warnings=notes if notes else [],
    )

