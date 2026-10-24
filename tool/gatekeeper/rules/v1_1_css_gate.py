from __future__ import annotations

from core.context import GatekeeperContext
from core.types import StepResult


RULE_META = {
    "step_id": "v1_1_css_gate",
    "step_title": "v1.1 CSS Gate",
    "requires": ["css", "event_level"],
    "optional": ["market_mode", "event_confirmed"],
    "provides": ["flags.carry_gate", "flags.size_factor", "flags.delta_overlay", "flags.css"],
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
    market_mode = context.state.market_mode or "UNKNOWN"
    event_level = _norm_event_level(context.value("event_level", "None"))
    if bool(context.value("event_confirmed", False)):
        event_level = "High"

    if market_mode != "CARRY" or event_level == "High":
        return StepResult(
            step_id="v1_1_css_gate",
            step_title="v1.1 CSS Gate",
            status="SKIP",
            stop=False,
            severity="INFO",
            explain=[f"SKIP: market_mode={market_mode}, event_level={event_level}"],
        )

    css_val = context.value("css", None)
    try:
        css = int(float(css_val))
    except (TypeError, ValueError):
        css = None

    if css is None:
        return StepResult(
            step_id="v1_1_css_gate",
            step_title="v1.1 CSS Gate",
            status="PASS",
            stop=True,
            severity="WARN",
            missing_required=["css"],
            outputs_set={
                "structure": "FLAT",
                "params": {},
                "flags.carry_gate": "UNKNOWN",
                "flags.need_manual_inputs": True,
                "flags.missing_inputs": ["css"],
            },
            explain=["WAIT: css missing (need manual override or fix option data)."],
            warnings=["CSS missing -> output WAIT/FLAT to avoid false carry signal."],
        )

    if css < 30:
        carry_gate = "NO_CARRY"
        size_factor = 1.0
        delta_overlay = "NONE"
    elif css < 50:
        carry_gate = "DEFENSIVE"
        size_factor = 0.5
        delta_overlay = "FARTHER"
    else:
        carry_gate = "NORMAL"
        size_factor = 1.0
        delta_overlay = "NONE"

    return StepResult(
        step_id="v1_1_css_gate",
        step_title="v1.1 CSS Gate",
        status="PASS",
        stop=False,
        severity="INFO",
        outputs_set={
            "flags.css": css,
            "flags.carry_gate": carry_gate,
            "flags.size_factor": size_factor,
            "flags.delta_overlay": delta_overlay,
        },
        metrics={"css": css, "carry_gate": carry_gate, "size_factor": size_factor},
        explain=[f"CSS={css} -> carry_gate={carry_gate}"],
        warnings=["CSS<30: carry veto (default switch to debit by bias)"] if carry_gate == "NO_CARRY" else [],
    )

