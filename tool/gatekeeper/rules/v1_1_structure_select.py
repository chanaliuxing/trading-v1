from __future__ import annotations

from typing import Any

from core.context import GatekeeperContext
from core.types import StepResult


RULE_META = {
    "step_id": "v1_1_structure_select",
    "step_title": "v1.1 Structure Select",
    "requires": ["spread_width_usd", "event_level"],
    "optional": ["ivr", "skew_value", "css", "market_mode", "event_confirmed"],
    "provides": ["structure", "params", "flags.alternatives"],
    "stop_on_fail": True,
    "config_keys": ["templates"],
}


def _get(cfg: dict[str, Any], path: list[str], default: Any = None) -> Any:
    cur: Any = cfg
    for part in path:
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _norm_event_level(raw: object) -> str:
    val = str(raw or "").strip().lower()
    if val in {"high", "h", "true", "yes", "1"}:
        return "High"
    if val in {"medium", "m"}:
        return "Medium"
    return "None"


def _debit_by_bias(bias: str) -> str:
    if bias == "BULLISH":
        return "DEBIT_CALL_SPREAD"
    if bias == "BEARISH":
        return "DEBIT_PUT_SPREAD"
    return "FLAT"


def run(context: GatekeeperContext) -> StepResult:
    market_mode = context.state.market_mode or "UNKNOWN"
    bias = context.state.bias or "NEUTRAL"

    event_level = _norm_event_level(context.value("event_level", "None"))
    if bool(context.value("event_confirmed", False)):
        event_level = "High"

    spread_width_raw = context.value("spread_width_usd", None)
    try:
        spread_width_usd = float(spread_width_raw) if spread_width_raw is not None else None
    except (TypeError, ValueError):
        spread_width_usd = None

    forbidden = set(context.state.constraints.forbidden_structures or set())

    forbid_credit = bool(context.state.flags.get("forbid_credit", event_level == "High"))
    event_defensive = bool(context.state.flags.get("event_defensive_overlay", event_level == "Medium"))
    bofa_defensive = bool(context.state.flags.get("bofa_defensive_overlay", False))
    skew_put_elevated = bool(context.state.flags.get("skew_put_elevated", False))

    css_gate = str(context.state.flags.get("carry_gate", "UNKNOWN"))
    css_val = context.state.flags.get("css", None)
    try:
        css = int(float(css_val)) if css_val is not None else None
    except (TypeError, ValueError):
        css = None

    ivr_raw = context.value("ivr", None)
    try:
        ivr = float(ivr_raw) if ivr_raw is not None else None
    except (TypeError, ValueError):
        ivr = None

    # --- Guard: width required for any actionable params
    if spread_width_usd is None:
        return StepResult(
            step_id="v1_1_structure_select",
            step_title="v1.1 Structure Select",
            status="PASS",
            stop=True,
            severity="WARN",
            missing_required=["spread_width_usd"],
            outputs_set={
                "structure": "FLAT",
                "params": {},
                "flags.need_manual_inputs": True,
                "flags.missing_inputs": ["spread_width_usd"],
            },
            explain=["WAIT: spread_width_usd required (manual)."],
        )

    templates = context.config.get("templates", {}) or {}

    def _params_for(structure: str) -> dict[str, Any]:
        size_factor = 1.0
        delta_overlay = "NONE"

        if css_gate == "DEFENSIVE":
            size_factor *= 0.5
            delta_overlay = "FARTHER"
        if event_defensive:
            size_factor *= 0.5
            delta_overlay = "FARTHER"

        params: dict[str, Any] = {"width_usd": spread_width_usd, "size_factor": round(size_factor, 3)}

        if structure in {"BPS", "BCS"}:
            params["dte"] = int(_get(templates, ["bps_bcs", "dte"], 14))
            delta_range = _get(templates, ["bps_bcs", "short_delta_range"], [0.1, 0.2])
            if delta_overlay == "FARTHER" or skew_put_elevated:
                delta_range = _get(templates, ["bps_bcs", "defensive_short_delta_range"], [0.08, 0.15])
            if bofa_defensive and structure == "BPS":
                delta_range = _get(templates, ["bps_bcs", "defensive_short_delta_range"], [0.08, 0.15])
                params["size_factor"] = round(float(params["size_factor"]) * 0.5, 3)
            params["short_delta_range"] = delta_range
        elif structure == "IC":
            params["dte_range"] = _get(templates, ["ic", "dte_range"], [14, 21])
            delta_range = _get(templates, ["ic", "short_delta_range"], [0.1, 0.15])
            if delta_overlay == "FARTHER" or skew_put_elevated or bofa_defensive:
                delta_range = _get(templates, ["bps_bcs", "defensive_short_delta_range"], [0.08, 0.15])
                params["size_factor"] = round(float(params["size_factor"]) * 0.5, 3)
            params["short_delta_range"] = delta_range
        else:
            # Debit structures keep minimal params; width is manual.
            params["dte_range"] = _get(templates, ["ic", "dte_range"], [14, 21])
        return params

    # --- Mode: Event High => forbid all credit; recommend debit by bias
    if forbid_credit:
        structure = _debit_by_bias(bias)
        params = _params_for(structure) if structure != "FLAT" else {}
        return StepResult(
            step_id="v1_1_structure_select",
            step_title="v1.1 Structure Select",
            status="PASS",
            stop=True,
            severity="INFO" if structure != "FLAT" else "WARN",
            outputs_set={
                "structure": structure,
                "params": params,
                "flags.alternatives": [],
            },
            explain=[
                f"Event=High -> credit forbidden",
                f"Structure -> {structure} (bias={bias})",
            ],
            warnings=["Major event: avoid selling premium"],
        )

    # --- Mode: non-carry => no CSS; recommend buy/flat (simple)
    if market_mode != "CARRY":
        if market_mode in {"CRASH", "HEDGE"}:
            structure = _debit_by_bias("BEARISH" if market_mode == "CRASH" else bias)
        else:
            structure = "FLAT"
        params = _params_for(structure) if structure != "FLAT" else {}
        return StepResult(
            step_id="v1_1_structure_select",
            step_title="v1.1 Structure Select",
            status="PASS",
            stop=True,
            severity="INFO" if structure != "FLAT" else "WARN",
            outputs_set={
                "structure": structure,
                "params": params,
                "flags.alternatives": [],
            },
            explain=[f"MarketMode={market_mode} -> {structure}"],
            warnings=["Mode != CARRY: CSS not used"] if market_mode != "UNKNOWN" else [],
        )

    # --- Mode: CARRY
    if css_gate == "NO_CARRY":
        structure = _debit_by_bias(bias)
        params = _params_for(structure) if structure != "FLAT" else {}
        return StepResult(
            step_id="v1_1_structure_select",
            step_title="v1.1 Structure Select",
            status="PASS",
            stop=True,
            severity="INFO" if structure != "FLAT" else "WARN",
            outputs_set={
                "structure": structure,
                "params": params,
                "flags.alternatives": [{"structure": "FLAT", "why": "CSS<30 carry veto"}],
            },
            explain=[f"CSS={css} < 30 -> NO_CARRY", f"Structure -> {structure} (bias={bias})"],
            warnings=["Carry vetoed by CSS<30 (default switch to debit)."],
        )

    if context.is_missing("skew_value"):
        alts = [{"structure": _debit_by_bias(bias), "why": "RR25 missing -> debit by bias"}]
        return StepResult(
            step_id="v1_1_structure_select",
            step_title="v1.1 Structure Select",
            status="PASS",
            stop=True,
            severity="WARN",
            missing_required=["skew_value"],
            outputs_set={
                "structure": "FLAT",
                "params": {},
                "flags.need_manual_inputs": True,
                "flags.missing_inputs": ["skew_value"],
                "flags.alternatives": alts,
            },
            explain=["WAIT: RR25 missing (skew_value)."],
            warnings=["Missing RR25 -> output WAIT/FLAT to avoid false carry signal."],
        )

    # Ideal structure by bias
    if bias == "BULLISH":
        ideal = "BPS"
    elif bias == "BEARISH":
        ideal = "BCS"
    else:
        ideal = "IC"

    # Event=Medium: IC not allowed to be Recommended
    if ideal == "IC" and event_level == "Medium":
        alts: list[dict[str, Any]] = []
        if "IC" not in forbidden and ivr is not None and ivr >= 20:
            alts.append({"structure": "IC", "why": "Event=Medium: IC allowed only as alternative"})
        alts.append({"structure": _debit_by_bias(bias), "why": "Fallback debit by bias"})
        return StepResult(
            step_id="v1_1_structure_select",
            step_title="v1.1 Structure Select",
            status="PASS",
            stop=True,
            severity="WARN",
            outputs_set={
                "structure": "FLAT",
                "params": {},
                "flags.alternatives": alts[:2],
            },
            explain=["Event=Medium: IC not recommended -> WAIT/FLAT"],
            warnings=["Event=Medium defensive: avoid IC as recommended"],
        )

    # IC gating by IVR
    if ideal == "IC" and (ivr is None or ivr < 20):
        alts: list[dict[str, Any]] = []
        if "BPS" not in forbidden:
            alts.append({"structure": "BPS", "why": "Alt: single-side spread if allowed"})
        if "BCS" not in forbidden and len(alts) < 2:
            alts.append({"structure": "BCS", "why": "Alt: single-side spread if allowed"})
        if len(alts) < 2:
            alts.append({"structure": _debit_by_bias(bias), "why": "Alt: debit by bias"})
        return StepResult(
            step_id="v1_1_structure_select",
            step_title="v1.1 Structure Select",
            status="PASS",
            stop=True,
            severity="WARN",
            outputs_set={
                "structure": "FLAT",
                "params": {},
                "flags.alternatives": alts[:2],
            },
            explain=[f"IVR<{20} or missing -> IC not recommended -> WAIT/FLAT"],
            warnings=["IC gating: IVR<20 (or missing)"],
        )

    # Constraint conflict: ideal blocked -> recommended WAIT, alternatives debit
    if ideal in forbidden:
        alts = [{"structure": _debit_by_bias(bias), "why": "Ideal blocked -> debit by bias"}]
        return StepResult(
            step_id="v1_1_structure_select",
            step_title="v1.1 Structure Select",
            status="PASS",
            stop=True,
            severity="WARN",
            outputs_set={
                "structure": "FLAT",
                "params": {},
                "flags.alternatives": alts,
            },
            explain=[f"Ideal {ideal} blocked by constraints -> WAIT/FLAT"],
            warnings=["Constraints conflict: ideal credit structure forbidden"],
        )

    # Recommended ideal
    structure = ideal
    params = _params_for(structure)
    alternatives: list[dict[str, Any]] = []
    alternatives.append({"structure": _debit_by_bias(bias), "why": "Alt: debit by bias"})
    alternatives.append({"structure": "FLAT", "why": "Alt: wait"})

    return StepResult(
        step_id="v1_1_structure_select",
        step_title="v1.1 Structure Select",
        status="PASS",
        stop=True,
        severity="INFO",
        outputs_set={
            "structure": structure,
            "params": params,
            "flags.alternatives": alternatives[:2],
        },
        metrics={
            "market_mode": market_mode,
            "bias": bias,
            "event_level": event_level,
            "css": css,
            "carry_gate": css_gate,
            "ivr": ivr,
        },
        explain=[f"Recommended -> {structure} ({market_mode} + {bias})", f"Params -> {params}"],
    )
