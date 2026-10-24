#!/usr/bin/env python3
"""
Dummy skill for testing the evolution chamber.
"""
from libs.skills.runtime import append_skill_flow_event

def on_decision(decision: dict, context: dict) -> dict:
    """
    Dummy hook that logs a message to skill_flow.jsonl.
    """
    append_skill_flow_event({
        "skill_id": "misc_20251210_201020_cd854a",
        "event": "on_decision_hook_called",
        "decision": decision
    }, runs_dir="runs/ai_trader")
    return decision

def execute_skill():
    """Returns True."""
    return True