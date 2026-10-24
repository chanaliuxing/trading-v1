#!/usr/bin/env python3
"""
Generate prompt optimization suggestions from reflection.json.

Improvements:
- Keep suggestions in JSON for audit (prompt_suggestions.json)
- If --auto-apply: materialize a candidate Prompt ID into prompts/prompt_library.json
  so next session can A/B test via execution_prompt_id (safe, whitelist‑based)

Notes:
- We do NOT directly modify arbitrary prompt text used at runtime unless through the
  approved prompt library. Text overrides remain governed by
  policies.prompts.allow_text_override (default: false).
"""

import argparse
import json
from pathlib import Path
import shutil
import datetime as _dt
import json as _J


LIB_PATH_DEFAULT = Path("prompts/prompt_library.json")


def _today() -> str:
    return _dt.date.today().isoformat()


def _load_json(p: Path) -> dict:
    try:
        return _J.loads(p.read_text("utf-8"))
    except Exception:
        return {}


def _save_json(p: Path, obj: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_J.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")


def suggest_from_reflection(reflection: dict) -> dict:
    agg = reflection.get('aggregate', {})
    overall = float(agg.get('overall_consistency', 0.0))
    # Heuristic suggestions based on low consistency
    suggestions = []
    if overall < 0.65:
        suggestions.append("Increase RVOL gate (e.g., rvol20 > 2.5) during PRE and high-vol periods.")
        suggestions.append("Avoid claiming premarket breakout unless close >= premarket_high * 1.002.")
        suggestions.append("When MA20(close) > close, prefer HOLD unless strong momentum confirmation.")
        suggestions.append("Raise confidence threshold to 0.7 when spread widens.")
    else:
        suggestions.append("Maintain current tactical rules; consistency acceptable.")
    return {
        'overall_consistency': overall,
        'tactical_notes': suggestions,
    }


def _load_prompt_library(p: Path) -> dict:
    if not p.exists():
        return {}
    try:
        return _J.loads(p.read_text("utf-8"))
    except Exception:
        return {}


def _save_prompt_library(p: Path, lib: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    # backup
    try:
        b = p.with_suffix(p.suffix + f".bak_{_today().replace('-', '')}")
        shutil.copy2(p, b)
    except Exception:
        pass
    p.write_text(_J.dumps(lib, indent=2, ensure_ascii=False), encoding="utf-8")


def _choose_base_id(runs_dir: Path) -> str:
    """Best-effort detect today's execution_prompt_id used in plans; fallback to v3_fast_market_following."""
    try:
        d = runs_dir / _today()
        for f in sorted(d.glob("brain_plan_*.json")):
            j = _load_json(f)
            for p in (j.get("providers") or []):
                eid = p.get("execution_prompt_id")
                if isinstance(eid, str) and eid:
                    return eid
    except Exception:
        pass
    return "v3_fast_market_following"


def _compose_prompt(base_text: str, suggestions: dict) -> str:
    notes = suggestions.get('tactical_notes') or []
    if not notes:
        return base_text
    header = "\n\n[Auto‑evolved tactical notes for next session]\n"
    bullets = "\n".join(f"- {n}" for n in notes)
    return f"{base_text}{header}{bullets}\n"


def _create_prompt_metadata(base_id: str, source: str = "auto_evolver") -> dict:
    """Create metadata for prompt library entries."""
    return {
        "created_at": _dt.datetime.now(tz=_dt.timezone.utc).isoformat(),
        "source": source,  # "auto_evolver" | "manual" | "external"
        "base_id": base_id,
        "reviewer": "",  # Can be filled manually
        "signature": "",  # Can be filled manually for compliance
        "version": 1
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--reflection', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--library', default=str(LIB_PATH_DEFAULT))
    ap.add_argument('--auto-apply', action='store_true')
    args = ap.parse_args()

    ref = json.loads(Path(args.reflection).read_text('utf-8'))
    runs_dir = Path(args.out).resolve().parent
    sugg = suggest_from_reflection(ref)
    # Try detect base prompt id
    base_id = _choose_base_id(runs_dir=runs_dir.parent if runs_dir.name else Path("runs/ai_trader"))
    sugg['base_prompt_id'] = base_id

    lib_path = Path(args.library)
    applied_id = None
    if args.auto_apply:
        lib = _load_prompt_library(lib_path)
        # Ensure library has proper structure with prompts and meta
        if not isinstance(lib, dict):
            lib = {}
        if 'prompts' not in lib:
            lib['prompts'] = {}
        if 'meta' not in lib:
            lib['meta'] = {}

        prompts = lib['prompts']
        meta = lib['meta']

        # Resolve base text
        base_text = None
        if base_id in prompts:
            base_text = prompts.get(base_id)
        elif 'v3_stable' in prompts:
            base_text = prompts.get('v3_stable')
        # If library empty, bootstrap minimal header
        if not base_text:
            base_text = "You are an intraday trading agent. Respect risk gates. Output JSON only."

        # Compose new prompt text with tactical notes
        new_text = _compose_prompt(base_text, sugg)

        # Build unique ID - only one candidate per day (覆盖升级策略)
        dtag = _today().replace('-', '')
        cand = f"{base_id}_tactical_{dtag}"

        # Check if already exists - if yes, upgrade version
        if cand in prompts:
            existing_meta = meta.get(cand, {})
            ver = existing_meta.get('version', 1) + 1
            cand = f"{base_id}_tactical_{dtag}_v{ver}"

        # Store prompt and metadata
        prompts[cand] = new_text
        meta[cand] = _create_prompt_metadata(base_id, source="auto_evolver")

        lib['prompts'] = prompts
        lib['meta'] = meta
        _save_prompt_library(lib_path, lib)

        applied_id = cand
        sugg['applied_prompt_id'] = applied_id
        sugg['library'] = str(lib_path)

    Path(args.out).write_text(json.dumps(sugg, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
