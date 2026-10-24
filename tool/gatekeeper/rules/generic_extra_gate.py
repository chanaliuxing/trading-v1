from __future__ import annotations

from core.context import GatekeeperContext
from core.features import gap_from_open_pct, range_pct, vwap_deviation_pct
from core.time_utils import minutes_to_us_equity_close
from core.types import StepResult


RULE_META = {
    "step_id": "generic_extra_gate",
    "step_title": "Generic Extra Gate",
    "requires": ["ticker_bars_1m", "ticker_vwap"],
    "optional": [],
    "provides": [
        "flags.generic_credit_blocked",
        "flags.generic_ic_blocked",
        "constraints.forbidden_structures",
        "constraints.notes",
    ],
    "stop_on_fail": False,
    "config_keys": ["generic_gate"],
}


def _merge_rules(defaults: dict, overrides: dict) -> dict:
    merged = dict(defaults)
    merged.update(overrides)
    return merged


def run(context: GatekeeperContext) -> StepResult:
    gate_cfg = context.config.get("generic_gate", {}) or {}
    if not bool(gate_cfg.get("enabled", True)):
        return StepResult(
            step_id="generic_extra_gate",
            step_title="Generic Extra Gate",
            status="SKIP",
            stop=False,
            severity="INFO",
            explain=["SKIP: generic_gate.enabled=false"],
        )

    missing = [k for k in RULE_META["requires"] if context.is_missing(k)]
    if missing:
        return StepResult(
            step_id="generic_extra_gate",
            step_title="Generic Extra Gate",
            status="PASS",
            stop=False,
            severity="WARN",
            missing_required=missing,
            explain=[f"Generic gate skipped (missing inputs): {', '.join(missing)}"],
        )

    symbol = context.symbol.upper()
    defaults = gate_cfg.get("defaults", {}) if isinstance(gate_cfg.get("defaults", {}), dict) else {}
    symbol_rules = gate_cfg.get("symbol_rules", {}) if isinstance(gate_cfg.get("symbol_rules", {}), dict) else {}
    overrides = symbol_rules.get(symbol, {}) if isinstance(symbol_rules.get(symbol, {}), dict) else {}
    rule = _merge_rules(defaults, overrides)

    bars = context.value("ticker_bars_1m")
    if not isinstance(bars, list) or not bars:
        return StepResult(
            step_id="generic_extra_gate",
            step_title="Generic Extra Gate",
            status="PASS",
            stop=False,
            severity="WARN",
            missing_required=["ticker_bars_1m"],
            explain=["Generic gate skipped: empty ticker_bars_1m."],
            debug={"effective_rule": rule},
        )

    vwap = float(context.value("ticker_vwap"))
    last_close = float(bars[-1].get("close") or 0.0)

    range_5m = range_pct(bars, 5)
    range_15m = range_pct(bars, 15)
    gap_open = gap_from_open_pct(bars)
    vwap_dev = vwap_deviation_pct(last_close, vwap)
    minutes_to_close = minutes_to_us_equity_close()

    credit_blocked = False
    ic_blocked = False
    reasons_credit: list[str] = []
    reasons_ic: list[str] = []

    max_range_5m = float(rule.get("max_range_5m_pct", 0.01))
    max_range_15m = float(rule.get("max_range_15m_pct", 0.018))
    max_gap = float(rule.get("max_gap_from_open_pct", 0.02))
    close_block = int(rule.get("minutes_to_close_block_credit", 20))
    max_vwap_dev_ic = float(rule.get("max_vwap_deviation_pct_for_ic", 0.003))

    if range_5m is not None and range_5m >= max_range_5m:
        credit_blocked = True
        reasons_credit.append(f"5m range too large ({range_5m:.3%} >= {max_range_5m:.3%})")

    if range_15m is not None and range_15m >= max_range_15m:
        credit_blocked = True
        reasons_credit.append(f"15m range too large ({range_15m:.3%} >= {max_range_15m:.3%})")

    if gap_open is not None and gap_open >= max_gap:
        credit_blocked = True
        reasons_credit.append(f"gap from open too large ({gap_open:.3%} >= {max_gap:.3%})")

    if minutes_to_close <= close_block:
        credit_blocked = True
        reasons_credit.append(f"too close to close for credit (minutes_to_close={minutes_to_close} <= {close_block})")

    if vwap_dev is not None and vwap_dev >= max_vwap_dev_ic:
        ic_blocked = True
        reasons_ic.append(f"too far from VWAP for IC ({vwap_dev:.3%} >= {max_vwap_dev_ic:.3%})")

    forbid: set[str] = set()
    if credit_blocked:
        forbid.update(rule.get("block_credit_structures", ["BPS", "BCS", "IC", "TINY_IC"]))
    if ic_blocked:
        forbid.update(rule.get("block_ic_structures", ["IC", "TINY_IC"]))

    notes: list[str] = []
    if credit_blocked:
        notes.extend([f"Generic gate: {r}" for r in reasons_credit])
    if ic_blocked:
        notes.extend([f"Generic gate: {r}" for r in reasons_ic])

    severity = "WARN" if (credit_blocked or ic_blocked) else "INFO"
    warnings: list[str] = []
    if credit_blocked:
        warnings.append("Generic gate: credit structures disabled")
    if ic_blocked:
        warnings.append("Generic gate: IC disabled (too far from VWAP)")

    explain: list[str] = []
    if credit_blocked:
        explain.append("Credit blocked: " + "; ".join(reasons_credit))
    if ic_blocked:
        explain.append("IC blocked: " + "; ".join(reasons_ic))
    if not explain:
        explain = ["PASS: no extra constraints applied."]

    return StepResult(
        step_id="generic_extra_gate",
        step_title="Generic Extra Gate",
        status="PASS",
        stop=False,
        severity=severity,  # type: ignore[arg-type]
        outputs_set={
            "flags.generic_credit_blocked": credit_blocked,
            "flags.generic_ic_blocked": ic_blocked,
            "constraints.forbidden_structures": sorted(forbid),
            "constraints.notes": notes,
        },
        metrics={
            "range_5m_pct": range_5m,
            "range_15m_pct": range_15m,
            "gap_from_open_pct": gap_open,
            "vwap_dev_pct": vwap_dev,
            "minutes_to_close": minutes_to_close,
        },
        explain=explain,
        warnings=warnings,
        debug={"effective_rule": rule},
    )

