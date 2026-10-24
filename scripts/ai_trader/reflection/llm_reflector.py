from __future__ import annotations
import json
from typing import Dict, Any

from scripts.ai_trader.brain.brain import ControlBrain


PROMPT = (
    "You are a trading reflection assistant.\n"
    "Return JSON ONLY (no markdown), minified, with keys: \n"
    "{\"diagnosis\": <string>, \"prescription\": [ {\"action\": <string>, \"params\": <object>, \"confidence\": <float>} ] }\n"
    "Action whitelist (choose from): increase_conf_floor, reduce_sizing_multiplier, disable_rl, enable_rl, switch_prompt_id.\n"
    "Param schema per action:\n"
    "- increase_conf_floor: {\"delta\": <float in 0..0.2>}\n"
    "- reduce_sizing_multiplier: {\"factor\": <float in 0.5..0.95>}\n"
    "- disable_rl: {} | enable_rl: {}\n"
    "- switch_prompt_id: {\"id\": <string>}  // must reference an approved prompt ID (whitelist)\n"
    "Guidance:\n"
    "- If market_alignment or claim_consistency is low under high volatility, prefer reduce_sizing_multiplier or increase_conf_floor.\n"
    "- Enable RL only when recent RL gate/metrics are OK; otherwise prefer disable_rl.\n"
    "- For prompt changes, suggest switch_prompt_id with an existing candidate ID (no raw prompt text).\n"
)


def llm_reflect(facts: Dict[str, Any], market_ctx: Dict[str, Any]) -> Dict[str, Any]:
    brain = ControlBrain(runs_dir="runs/ai_trader")
    payload = {"facts": facts or {}, "market": market_ctx or {}}
    prompt = PROMPT + "\nINPUT:\n" + json.dumps(payload)
    try:
        out = brain.propose_plan("reflection", {"prompt": prompt})  # reuse ask path
        # propose_plan returns model JSON; ensure structure
        if isinstance(out, dict) and ("diagnosis" in out or "prescription" in out):
            return out
    except Exception:
        pass
    return {"diagnosis": "", "prescription": []}
