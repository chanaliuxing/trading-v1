#!/usr/bin/env python3
from __future__ import annotations
import argparse
import json
from pathlib import Path
from datetime import date
from typing import Dict, Any


def _load_json(p: Path) -> Dict[str, Any]:
    try:
        return json.loads(p.read_text("utf-8"))
    except Exception:
        return {}


def _fallback_diagnosis(facts: Dict[str, Any], market: Dict[str, Any]) -> Dict[str, Any]:
    diag = []
    pres = []
    try:
        cons_market = float(facts.get("market_alignment", facts.get("consistency", 0.0)) or 0.0)
        cons_claim = float(facts.get("claim_consistency", 0.0) or 0.0)
        regime = str(market.get("regime", "unknown"))
        if cons_market < 0.6 and regime == "sideways":
            diag.append("Trend-like rules may overfit in sideways markets.")
            pres.append({"action": "increase_conf_floor", "params": {"delta": 0.05}, "confidence": 0.7})
        if cons_market < 0.6 and regime == "volatile":
            diag.append("Signals unstable under high volatility.")
            pres.append({"action": "reduce_sizing_multiplier", "params": {"factor": 0.8}, "confidence": 0.7})
        if cons_claim < 0.5:
            diag.append("Reasoning claims often unmet; tighten gating or require explicit rule evidence.")
            pres.append({"action": "no_new_positions", "params": {}, "confidence": 0.6})
    except Exception:
        pass
    return {"diagnosis": "; ".join(diag), "prescription": pres}


def build_reflection_dxp(runs_dir: str, d: str) -> Dict[str, Any]:
    rd = Path(runs_dir) / d
    reflection = _load_json(rd / "reflection.json")
    market = _load_json(rd / "context.json")
    # Facts (simplified): aggregate metrics from reflection.json
    agg = (reflection.get("aggregate") if isinstance(reflection, dict) else {}) or {}
    facts = {
        "overall_pnl": None,
        "consistency": float(agg.get("overall_consistency", 0.0) or 0.0),
        "market_alignment": float(agg.get("overall_market_alignment", agg.get("overall_consistency", 0.0)) or 0.0),
        "claim_consistency": float(agg.get("overall_claim_consistency", 0.0) or 0.0),
        "by_provider": agg.get("by_provider", {}),
        "by_provider_market_alignment": agg.get("by_provider_market_alignment", {}),
        "by_provider_claim_consistency": agg.get("by_provider_claim_consistency", {}),
        "n_decisions": int(agg.get("n", 0)),
    }
    # Optional LLM diagnosis/prescription
    try:
        from scripts.ai_trader.reflection.llm_reflector import llm_reflect
        llm_out = llm_reflect(facts, market)
    except Exception:
        llm_out = {"diagnosis": "", "prescription": []}
    # Fallback heuristics
    fallback = _fallback_diagnosis(facts, market)
    dxp = {
        "facts": facts,
        "market": market,
        "diagnosis": llm_out.get("diagnosis") or fallback.get("diagnosis"),
        "prescription": llm_out.get("prescription") or fallback.get("prescription", []),
    }
    (rd / "reflection_dxp.json").write_text(json.dumps(dxp, indent=2), encoding="utf-8")
    return dxp


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=date.today().isoformat())
    ap.add_argument("--runs-dir", default="runs/ai_trader")
    args = ap.parse_args()
    build_reflection_dxp(args.runs_dir, args.date)


if __name__ == "__main__":
    main()
