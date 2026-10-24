from __future__ import annotations

from core.context import GatekeeperContext
from core.features import gap_from_open_pct, range_pct
from core.time_utils import minutes_to_us_equity_close
from core.types import StepResult


RULE_META = {
    "step_id": "tsla_extra_gate",
    "step_title": "TSLA Extra Gate",
    "requires": ["ticker_bars_1m"],
    "optional": [],
    "provides": [
        "flags.tsla_credit_blocked",
        "constraints.forbidden_structures",
        "constraints.notes",
    ],
    "stop_on_fail": False,
    "config_keys": ["tsla"],
}


def run(context: GatekeeperContext) -> StepResult:
    if context.symbol.upper() != "TSLA":
        return StepResult(
            step_id="tsla_extra_gate",
            step_title="TSLA Extra Gate",
            status="SKIP",
            stop=False,
            severity="INFO",
            explain=["SKIP: not TSLA"],
        )

    tsla_cfg = context.config.get("tsla", {}) or {}
    if not bool(tsla_cfg.get("enabled", True)):
        return StepResult(
            step_id="tsla_extra_gate",
            step_title="TSLA Extra Gate",
            status="SKIP",
            stop=False,
            severity="INFO",
            explain=["SKIP: tsla.enabled=false"],
        )

    if context.is_missing("ticker_bars_1m"):
        return StepResult(
            step_id="tsla_extra_gate",
            step_title="TSLA Extra Gate",
            status="PASS",
            stop=False,
            severity="WARN",
            missing_required=["ticker_bars_1m"],
            explain=["TSLA gate skipped: missing ticker_bars_1m"],
        )

    bars = context.value("ticker_bars_1m")
    if not isinstance(bars, list) or not bars:
        return StepResult(
            step_id="tsla_extra_gate",
            step_title="TSLA Extra Gate",
            status="PASS",
            stop=False,
            severity="WARN",
            missing_required=["ticker_bars_1m"],
            explain=["TSLA gate skipped: empty ticker_bars_1m"],
        )

    gate = tsla_cfg.get("gate", {}) if isinstance(tsla_cfg.get("gate", {}), dict) else {}

    range_5m = range_pct(bars, int(gate.get("window_5m", 5)))
    range_15m = range_pct(bars, int(gate.get("window_15m", 15)))
    gap_open = gap_from_open_pct(bars)
    minutes_to_close = minutes_to_us_equity_close()

    cap_5m = float(gate.get("range_5m_cap_pct", 0.012))
    cap_15m = float(gate.get("range_15m_cap_pct", 0.02))
    gap_cap = float(gate.get("gap_cap_pct", 0.025))
    close_block = int(gate.get("minutes_to_close_block_credit", 30))

    credit_blocked = False
    reasons: list[str] = []

    if range_5m is not None and range_5m >= cap_5m:
        credit_blocked = True
        reasons.append(f"TSLA 5m range too large ({range_5m:.3%} >= {cap_5m:.3%})")
    if range_15m is not None and range_15m >= cap_15m:
        credit_blocked = True
        reasons.append(f"TSLA 15m range too large ({range_15m:.3%} >= {cap_15m:.3%})")
    if gap_open is not None and gap_open >= gap_cap:
        credit_blocked = True
        reasons.append(f"TSLA gap from open too large ({gap_open:.3%} >= {gap_cap:.3%})")
    if minutes_to_close <= close_block:
        credit_blocked = True
        reasons.append(f"Too close to close for TSLA credit (minutes_to_close={minutes_to_close} <= {close_block})")

    forbid = {"BPS", "BCS", "IC", "TINY_IC"} if credit_blocked else set()
    notes = [f"TSLA gate: {r}" for r in reasons]

    warnings: list[str] = []
    if credit_blocked:
        warnings.append("TSLA gate: credit structures disabled")

    explain = ["PASS: no TSLA constraints applied."] if not reasons else reasons

    return StepResult(
        step_id="tsla_extra_gate",
        step_title="TSLA Extra Gate",
        status="PASS",
        stop=False,
        severity="WARN" if credit_blocked else "INFO",
        outputs_set={
            "flags.tsla_credit_blocked": credit_blocked,
            "constraints.forbidden_structures": sorted(forbid),
            "constraints.notes": notes,
        },
        metrics={
            "range_5m_pct": range_5m,
            "range_15m_pct": range_15m,
            "gap_from_open_pct": gap_open,
            "minutes_to_close": minutes_to_close,
        },
        explain=explain,
        warnings=warnings,
        debug={"tsla_gate": gate},
    )

