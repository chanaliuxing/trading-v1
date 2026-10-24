#!/usr/bin/env python3
"""
Risk control demo skill for runtime hooks.
- on_decision: injects a note and reduces cycles by 1 for safety.
- adjust_risk: enforces min interval and adds safe_mode flag.
"""
from typing import Dict, Any


def on_decision(decision: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    decision = dict(decision or {})
    # reduce cycles slightly to be conservative
    if isinstance(decision.get("cycles"), int) and decision["cycles"] > 1:
        decision["cycles"] = decision["cycles"] - 1
    notes = decision.get("notes", "")
    decision["notes"] = f"{notes} | demo_skill_safety" if notes else "demo_skill_safety"
    return decision


def adjust_risk(risk_payload: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(risk_payload or {})
    flags = dict(payload.get("llm_trader_flags", {}))
    # enforce a minimum interval
    if payload.get("interval_sec") and payload["interval_sec"] < 300:
        payload["interval_sec"] = 300
    flags["forbid_new_before_min"] = max(flags.get("forbid_new_before_min", 0) or 0, 5)
    payload["llm_trader_flags"] = flags
    payload["safe_mode"] = True
    return payload
