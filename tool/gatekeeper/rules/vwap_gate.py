from __future__ import annotations

from core.context import GatekeeperContext
from core.features import last3_relation_to_vwap
from core.types import StepResult


RULE_META = {
    "step_id": "vwap_gate",
    "step_title": "VWAP Gate",
    "requires": ["ticker_bars_1m", "ticker_vwap"],
    "optional": ["ticker_price"],
    "provides": ["bias"],
    "stop_on_fail": True,
    "config_keys": ["vwap_gate.neutral_band_pct"],
}


def run(context: GatekeeperContext) -> StepResult:
    missing = [k for k in RULE_META["requires"] if context.is_missing(k)]
    if missing:
        return StepResult(
            step_id="vwap_gate",
            step_title="VWAP Gate",
            status="FAIL",
            stop=True,
            severity="ERROR",
            missing_required=missing,
            explain=[f"NO_SIGNAL: missing required inputs for vwap gate: {', '.join(missing)}"],
        )

    bars = context.value("ticker_bars_1m")
    vwap = float(context.value("ticker_vwap"))
    vwap_cfg = context.config.get("vwap_gate", {})
    neutral_band_pct = float(vwap_cfg.get("neutral_band_pct", 0.0015))

    if not isinstance(bars, list) or len(bars) < 1:
        return StepResult(
            step_id="vwap_gate",
            step_title="VWAP Gate",
            status="FAIL",
            stop=True,
            severity="ERROR",
            missing_required=["ticker_bars_1m"],
            explain=["NO_SIGNAL: insufficient 1m bars for VWAP gate."],
        )

    last_close = float(bars[-1].get("close") or 0.0)
    last_price_val = context.value("ticker_price", None)
    try:
        last_price = float(last_price_val) if last_price_val is not None else None
    except (TypeError, ValueError):
        last_price = None
    price = last_price if last_price is not None and last_price > 0 else last_close
    if vwap == 0 or price == 0:
        bias = "NEUTRAL"
        distance_pct = 0.0
    else:
        distance_pct = (price - vwap) / vwap
        if distance_pct > neutral_band_pct:
            bias = "BULLISH"
        elif distance_pct < -neutral_band_pct:
            bias = "BEARISH"
        else:
            bias = "NEUTRAL"

    relation = last3_relation_to_vwap(bars, vwap)
    return StepResult(
        step_id="vwap_gate",
        step_title="VWAP Gate",
        status="PASS",
        stop=False,
        severity="INFO",
        outputs_set={"bias": bias},
        metrics={
            "last3_close_relation": relation,
            "ticker_vwap": vwap,
            "last_close": last_close,
            "last_price": price,
            "vwap_distance_pct": distance_pct,
            "vwap_neutral_band_pct": neutral_band_pct,
        },
        explain=[f"VWAPGate -> {bias} (dist={distance_pct:.3%}, band={neutral_band_pct:.3%})"],
    )
