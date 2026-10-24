from __future__ import annotations

from core.context import GatekeeperContext
from core.types import StepResult


RULE_META = {
    "step_id": "data_checks",
    "step_title": "Data Checks",
    "requires": [
        "anchor_price",
        "anchor_vwap",
        "vix",
        "vvix",
        "ticker_price",
        "ticker_vwap",
        "ivr",
        "skew_value",
        "spread_width_usd",
    ],
    "optional": [],
    "provides": [],
    "stop_on_fail": True,
    "config_keys": [],
}


def run(context: GatekeeperContext) -> StepResult:
    missing_core = [
        k
        for k in ("anchor_price", "anchor_vwap", "vix", "vvix", "ticker_price", "ticker_vwap")
        if context.is_missing(k)
    ]
    if missing_core:
        return StepResult(
            step_id="data_checks",
            step_title="Data Checks",
            status="PASS",
            stop=True,
            severity="WARN",
            missing_required=missing_core,
            outputs_set={
                "structure": "FLAT",
                "params": {},
                "flags.need_manual_inputs": True,
                "flags.missing_inputs": missing_core,
            },
            explain=[f"WAIT: core data missing. Refresh data and retry. Missing: {', '.join(missing_core)}"],
        )

    missing_ivr_skew = [k for k in ("ivr", "skew_value") if context.is_missing(k)]
    if missing_ivr_skew:
        return StepResult(
            step_id="data_checks",
            step_title="Data Checks",
            status="PASS",
            stop=True,
            severity="WARN",
            missing_required=missing_ivr_skew,
            outputs_set={
                "structure": "FLAT",
                "params": {},
                "flags.need_manual_inputs": True,
                "flags.missing_inputs": missing_ivr_skew,
            },
            explain=[
                f"WAIT: IVR/SKEW missing. Provide manual override or refresh options. Missing: {', '.join(missing_ivr_skew)}"
            ],
        )

    if context.is_missing("spread_width_usd"):
        return StepResult(
            step_id="data_checks",
            step_title="Data Checks",
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
            explain=["WAIT: spread_width_usd required (manual input)."],
        )

    return StepResult(
        step_id="data_checks",
        step_title="Data Checks",
        status="PASS",
        stop=False,
        severity="INFO",
        explain=["PASS: required inputs present."],
    )
