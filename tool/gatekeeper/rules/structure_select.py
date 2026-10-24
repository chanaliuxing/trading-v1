from __future__ import annotations

from typing import Any

from core.context import GatekeeperContext
from core.types import StepResult


RULE_META = {
    "step_id": "structure_select",
    "step_title": "Structure Select",
    "requires": [
        "vvix",
        "event_confirmed",
        "bofa_9_3",
        "spread_width_usd",
        "ivr",
        "skew_value",
        "earnings_lt_5d",
    ],
    "optional": [],
    "provides": [
        "structure",
        "params",
        "flags.skew_high",
        "flags.earnings_lock",
        "constraints.forbidden_structures",
        "constraints.notes",
    ],
    "stop_on_fail": True,
    "config_keys": [
        "thresholds.ivr.ic_min",
        "thresholds.vvix.hedge_min",
        "skew.high_threshold",
        "templates.bps_bcs",
        "templates.ic",
        "generic_gate.actions",
    ],
}


def _get(cfg: dict[str, Any], path: list[str], default: Any = None) -> Any:
    cur: Any = cfg
    for part in path:
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def run(context: GatekeeperContext) -> StepResult:
    missing = [k for k in RULE_META["requires"] if context.is_missing(k)]
    if missing:
        return StepResult(
            step_id="structure_select",
            step_title="Structure Select",
            status="FAIL",
            stop=True,
            severity="ERROR",
            missing_required=missing,
            explain=[f"NO_SIGNAL: missing required inputs for structure select: {', '.join(missing)}"],
        )

    market_mode = context.state.market_mode
    bias = context.state.bias
    if not market_mode or not bias:
        return StepResult(
            step_id="structure_select",
            step_title="Structure Select",
            status="FAIL",
            stop=True,
            severity="ERROR",
            missing_required=["market_mode", "bias"],
            explain=["NO_SIGNAL: market_mode/bias missing (pipeline order issue)."],
        )

    vvix = float(context.value("vvix"))
    event_confirmed = bool(context.value("event_confirmed"))
    bofa_9_3 = float(context.value("bofa_9_3"))
    spread_width_usd = float(context.value("spread_width_usd"))
    ivr = float(context.value("ivr"))
    skew_value = float(context.value("skew_value"))
    earnings_lt_5d = bool(context.value("earnings_lt_5d"))

    weekly_red_min = float(_get(context.config, ["thresholds", "bofa_9_3", "weekly_red_min"], 8))
    vvix_hedge_min = float(_get(context.config, ["thresholds", "vvix", "hedge_min"], 105))
    ic_min = float(_get(context.config, ["thresholds", "ivr", "ic_min"], 20))
    skew_high_th = float(_get(context.config, ["skew", "high_threshold"], 0.04))

    event_high = bool(context.state.flags.get("event_high", event_confirmed))
    weekly_red = bool(context.state.flags.get("weekly_red", bofa_9_3 >= weekly_red_min))
    skew_high = skew_value >= skew_high_th

    warnings: list[str] = []
    if weekly_red:
        warnings.append("weekly_red: defensive posture")
    if event_high:
        warnings.append("event_high: elevated volatility risk")
    if skew_high:
        warnings.append("skew_high: consider farther OTM / smaller size")

    if earnings_lt_5d:
        return StepResult(
            step_id="structure_select",
            step_title="Structure Select",
            status="PASS",
            stop=True,
            severity="WARN",
            outputs_set={
                "structure": "FLAT",
                "params": {},
                "flags.skew_high": skew_high,
                "flags.earnings_lock": True,
            },
            explain=[
                "EARNINGS_LOCK: Earnings within 5 days. Selling premium disabled.",
                "Structure -> FLAT",
            ],
            warnings=warnings + ["EARNINGS_LOCK: no credit trades"],
        )

    structure: str
    if market_mode == "CRASH":
        structure = "DEBIT_PUT_SPREAD"
        stop = True
        warnings.append("CRASH: credit structures disabled")
    else:
        stop = False
        if market_mode == "HEDGE":
            if bias == "BEARISH":
                structure = "BCS"
            elif bias == "BULLISH":
                if weekly_red:
                    structure = "BCS"
                else:
                    structure = "BPS"
                    warnings.append("HEDGE bullish: prefer small BPS far OTM (or consider debit)")
            else:
                structure = "FLAT"
        elif market_mode == "CARRY":
            if bias == "BULLISH":
                if weekly_red:
                    structure = "BCS"
                    warnings.append("weekly_red: prefer BCS or debit vs aggressive BPS")
                else:
                    structure = "BPS"
                    if skew_high:
                        warnings.append("skew_high: BPS farther OTM; don't chase premium")
            elif bias == "BEARISH":
                structure = "BCS"
            else:
                if (ivr >= ic_min) and (not skew_high) and (vvix <= vvix_hedge_min) and (not event_high) and (
                    not weekly_red
                ):
                    structure = "IC"
                else:
                    structure = "FLAT"
        elif market_mode == "CHOP":
            if (ivr >= ic_min) and (vvix <= vvix_hedge_min) and (not event_high) and (not weekly_red):
                structure = "TINY_IC"
            else:
                structure = "FLAT"
        else:
            structure = "FLAT"

    generic_actions = _get(context.config, ["generic_gate", "actions"], {}) or {}
    when_credit_blocked = str(generic_actions.get("when_credit_blocked", "FLAT_OR_DEBIT"))
    when_ic_blocked = str(generic_actions.get("when_ic_blocked", "FLAT"))

    forbidden = context.state.constraints.forbidden_structures
    if structure in forbidden:
        warnings.append("Structure blocked by constraints: fallback applied")
        if structure in {"IC", "TINY_IC"}:
            if when_ic_blocked == "FLAT":
                structure = "FLAT"
            else:
                structure = "DEBIT_PUT_SPREAD"
        elif structure in {"BPS", "BCS", "IC", "TINY_IC"}:
            if when_credit_blocked == "FLAT_OR_DEBIT":
                structure = "DEBIT_PUT_SPREAD" if (market_mode in {"CRASH", "HEDGE"} or bias == "BEARISH") else "FLAT"
            else:
                structure = "FLAT"

    templates = context.config.get("templates", {}) or {}
    params: dict[str, Any] = {}
    if structure in {"BPS", "BCS"}:
        params["dte"] = int(_get(templates, ["bps_bcs", "dte"], 14))
        params["width_usd"] = spread_width_usd
        delta_range = _get(templates, ["bps_bcs", "short_delta_range"], [0.1, 0.2])
        if market_mode == "HEDGE" or weekly_red or skew_high:
            delta_range = _get(templates, ["bps_bcs", "defensive_short_delta_range"], [0.08, 0.15])
        params["short_delta_range"] = delta_range
    elif structure in {"IC", "TINY_IC"}:
        params["dte_range"] = _get(templates, ["ic", "dte_range"], [14, 21])
        params["width_usd"] = spread_width_usd
        params["short_delta_range"] = _get(templates, ["ic", "short_delta_range"], [0.1, 0.15])
    elif structure == "DEBIT_PUT_SPREAD":
        params["width_usd"] = spread_width_usd

    explain = [
        f"Structure -> {structure} ({market_mode} + {bias})",
        f"Params -> {params}" if params else "Params -> {}",
    ]

    if context.state.constraints.notes:
        warnings.extend(context.state.constraints.notes)

    return StepResult(
        step_id="structure_select",
        step_title="Structure Select",
        status="PASS",
        stop=stop,
        severity="INFO" if structure not in {"FLAT"} else "WARN",
        outputs_set={
            "structure": structure,
            "params": params,
            "flags.skew_high": skew_high,
            "flags.earnings_lock": False,
        },
        metrics={
            "market_mode": market_mode,
            "bias": bias,
            "ivr": ivr,
            "skew_value": skew_value,
            "skew_high": skew_high,
            "vvix": vvix,
            "weekly_red": weekly_red,
            "event_high": event_high,
        },
        explain=explain,
        warnings=warnings,
    )

