from __future__ import annotations

from core.context import GatekeeperContext
from core.types import StepResult


RULE_META = {
    "step_id": "v1_1_market_mode",
    "step_title": "v1.1 Market Mode",
    "requires": ["vvix", "anchor_price", "anchor_vwap", "anchor_chop_score"],
    "optional": ["vix", "event_confirmed", "bofa_9_3", "anchor_drop_15m_pct"],
    "provides": ["market_mode", "flags.event_high", "flags.weekly_red"],
    "stop_on_fail": True,
    "config_keys": [
        "thresholds.vvix.hedge_min",
        "thresholds.vvix.crash_min",
        "thresholds.bofa_9_3.weekly_red_min",
        "chop.score_trigger",
    ],
}


def run(context: GatekeeperContext) -> StepResult:
    missing = [k for k in RULE_META["requires"] if context.is_missing(k)]
    if missing:
        if missing == ["vvix"]:
            return StepResult(
                step_id="v1_1_market_mode",
                step_title="v1.1 Market Mode",
                status="PASS",
                stop=True,
                severity="WARN",
                missing_required=missing,
                outputs_set={
                    "market_mode": "UNKNOWN",
                    "structure": "FLAT",
                    "params": {},
                    "flags.need_manual_inputs": True,
                    "flags.missing_inputs": missing,
                },
                explain=["WAIT: VVIX missing (need manual input or fix data)."],
                warnings=["Missing VVIX -> output WAIT/FLAT to avoid false signal."],
            )
        return StepResult(
            step_id="v1_1_market_mode",
            step_title="v1.1 Market Mode",
            status="FAIL",
            stop=True,
            severity="ERROR",
            missing_required=missing,
            explain=[f"NO_SIGNAL: missing required inputs for market mode: {', '.join(missing)}"],
        )

    vvix = float(context.value("vvix"))
    anchor_price = float(context.value("anchor_price"))
    anchor_vwap = float(context.value("anchor_vwap"))
    chop_score = float(context.value("anchor_chop_score"))

    event_confirmed = bool(context.value("event_confirmed", False))
    bofa_val = context.value("bofa_9_3", 0.0)
    try:
        bofa_9_3 = float(bofa_val) if bofa_val is not None else 0.0
    except (TypeError, ValueError):
        bofa_9_3 = 0.0

    weekly_red_min = float(context.config.get("thresholds", {}).get("bofa_9_3", {}).get("weekly_red_min", 8))
    vvix_hedge_min = float(context.config.get("thresholds", {}).get("vvix", {}).get("hedge_min", 105))
    vvix_crash_min = float(context.config.get("thresholds", {}).get("vvix", {}).get("crash_min", 110))
    chop_trigger = float(context.config.get("chop", {}).get("score_trigger", 1.0))

    event_high = event_confirmed is True
    weekly_red = bofa_9_3 >= weekly_red_min

    anchor_below_vwap = anchor_price < anchor_vwap
    vvix_gt_110 = vvix > vvix_crash_min
    vvix_gt_105 = vvix > vvix_hedge_min

    drop15m = context.value("anchor_drop_15m_pct", None)
    drop15m_gt_0p8 = False
    if drop15m is not None:
        try:
            drop15m_gt_0p8 = float(drop15m) > 0.8
        except (TypeError, ValueError):
            drop15m_gt_0p8 = False

    choppy = chop_score >= chop_trigger

    crash_hits = 0
    if vvix_gt_110:
        crash_hits += 1
    if event_high and anchor_below_vwap:
        crash_hits += 1
    if anchor_below_vwap and drop15m_gt_0p8:
        crash_hits += 1

    if crash_hits >= 2:
        market_mode = "CRASH"
    elif vvix_gt_105 or event_high or weekly_red:
        market_mode = "HEDGE"
    else:
        market_mode = "CHOP" if choppy else "CARRY"

    explain = [
        f"event_confirmed={'YES' if event_confirmed else 'NO'} -> event_high={event_high}",
        f"bofa_9_3={bofa_9_3:.2f} (weekly_red_min={weekly_red_min}) -> weekly_red={weekly_red}",
        f"VVIX={vvix:.2f} (hedge_min={vvix_hedge_min}, crash_min={vvix_crash_min}), below_vwap={anchor_below_vwap}, crash_hits={crash_hits}",
        f"chop_score={chop_score:.3f} (trigger={chop_trigger}) -> choppy={choppy}",
        f"MarketMode -> {market_mode}",
    ]
    if drop15m is None:
        explain.insert(3, "anchor_drop_15m_pct missing or not enabled")

    return StepResult(
        step_id="v1_1_market_mode",
        step_title="v1.1 Market Mode",
        status="PASS",
        stop=False,
        severity="INFO",
        outputs_set={
            "market_mode": market_mode,
            "flags.event_high": event_high,
            "flags.weekly_red": weekly_red,
        },
        metrics={
            "vvix": vvix,
            "anchor_below_vwap": anchor_below_vwap,
            "crash_hits": crash_hits,
            "chop_score": chop_score,
            "drop15m_pct": drop15m,
        },
        explain=explain,
        warnings=["HEDGE/CRASH risk: avoid aggressive BPS"] if market_mode in {"HEDGE", "CRASH"} else [],
    )
