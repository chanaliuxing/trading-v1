#!/usr/bin/env python3
from __future__ import annotations

"""
Nightly sleep loop (Meta v3 enhanced):
- Collect recent ml_events/brain_calls
- Generate LLM-based daily summary (Cognition Layer)
- Write compressed summary into STS
- Run consolidation to promote STS->MTS->LTS
"""

import argparse
import json
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Any, List

from scripts.ai_trader.memory.memory_fabric import write_memory, consolidate
from scripts.ai_trader.brain.provider_fallback import ProviderPool, ProviderConfig
from libs.skills.runtime import append_skill_flow_event

logger = logging.getLogger(__name__)


def _collect_recent_events(days: int = 1) -> list[dict]:
    runs_dir = Path("runs/ai_trader")
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    items: list[dict] = []
    for day_dir in sorted(runs_dir.glob("*")):
        try:
            day_date = datetime.fromisoformat(day_dir.name).replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if day_date < cutoff:
            continue
        ml_events = day_dir / "ml_events.jsonl"
        brain_calls = day_dir / "brain_calls.jsonl"
        execution_trace = day_dir / "execution_trace.jsonl"
        for fp in (ml_events, brain_calls, execution_trace):
            if not fp.exists():
                continue
            with fp.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        items.append(json.loads(line))
                    except Exception:
                        continue
    return items


async def _generate_daily_summary(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Meta v3.1: Generate LLM-based daily summary for memory consolidation.

    This is the "Cognition Layer" enhancement that allows the system to:
    - Reflect on daily activities
    - Extract key insights and patterns
    - Identify successes and failures
    - Generate compressed, actionable memory

    Args:
        events: List of ml_events, brain_calls, execution_trace

    Returns:
        Dict with summary, key_observations, lessons_learned
    """
    if not events:
        return {
            "summary": "No events recorded today.",
            "key_observations": [],
            "lessons_learned": [],
            "recommendations": [],
            "statistics": {}
        }

    # Prepare event statistics
    event_types = {}
    for event in events:
        etype = event.get("event_type") or event.get("type", "unknown")
        event_types[etype] = event_types.get(etype, 0) + 1

    # Sample key events for LLM analysis
    sampled_events = events[-50:]  # Last 50 events

    # Build LLM prompt
    prompt = f"""Analyze the following day's AI trading system activities and generate a concise summary (max 200 words).

Today's Event Statistics:
{json.dumps(event_types, indent=2)}

Sample Events (last 50):
{json.dumps(sampled_events, indent=2, default=str)[:5000]}  # Truncate to 5000 chars

Generate a structured summary with:
1. **Summary**: One-paragraph overview of the day's activities
2. **Key Observations**: 3-5 bullet points of notable patterns or events
3. **Lessons Learned**: 1-3 insights that should guide future decisions
4. **Recommendations**: Actions to take based on today's experience

Focus on:
- Model performance changes (accuracy, errors, latency)
- Data quality issues (missing data, sync failures, staleness)
- Evolution attempts (patches, experiments, approval decisions)
- System health (errors, timeouts, resource issues)

Output JSON format:
{{
  "summary": "string",
  "key_observations": ["string", ...],
  "lessons_learned": ["string", ...],
  "recommendations": ["string", ...]
}}
"""
    logger.debug(f"LLM Prompt: {prompt}")

    # Call LLM with fallback
    pool = _build_llm_pool()

    try:
        loop = asyncio.get_event_loop()
        result = await pool.call_with_fallback(prompt, operation="daily_summary")
        logger.debug(f"LLM Raw Response: {result.raw}")

        if result and result.raw:
            # Try to parse JSON response
            try:
                # Extract JSON from markdown code blocks if present
                text = result.raw.strip()
                if "```json" in text:
                    text = text.split("```json")[1].split("```")[0].strip()
                elif "```" in text:
                    text = text.split("```")[1].split("```")[0].strip()

                summary_data = json.loads(text)

                # Add statistics
                summary_data["statistics"] = {
                    "total_events": len(events),
                    "event_types": event_types,
                    "date": datetime.now(timezone.utc).isoformat()
                }

                return summary_data
            except json.JSONDecodeError:
                logger.warning("Failed to parse LLM summary as JSON, using raw text")
                return {
                    "summary": result.raw[:500],  # Truncate
                    "key_observations": [],
                    "lessons_learned": [],
                    "recommendations": [],
                    "statistics": {
                        "total_events": len(events),
                        "event_types": event_types
                    }
                }
    except Exception as e:
        logger.error(f"Failed to generate LLM summary: {e}")

    # Fallback: Rule-based summary
    return _fallback_summary(events, event_types)


def _build_llm_pool() -> ProviderPool:
    """Build LLM provider pool for daily summary generation"""
    import os

    env_chain = os.getenv("LLM_PROVIDERS_RESEARCH")
    if env_chain:
        try:
            pool = ProviderPool(configs=[], runs_dir="runs/ai_trader")
            pool.configs = pool._build_configs_from_env(env_chain)
            return pool
        except Exception:
            pass

    # Default: Local models for cost efficiency
    default_cfgs = [
        ProviderConfig(name="ollama", model="qwen2.5:7b-instruct", priority=1, timeout=300),
        ProviderConfig(name="ollama", model="deepseek-r1:1.5b", priority=2, timeout=120),
    ]
    return ProviderPool(configs=default_cfgs, runs_dir="runs/ai_trader")


def _fallback_summary(events: List[Dict[str, Any]], event_types: Dict[str, int]) -> Dict[str, Any]:
    """Fallback summary when LLM is unavailable"""
    failures = sum(1 for e in events if e.get("status") == "FAIL" or e.get("event_type") == "failed")
    successes = sum(1 for e in events if e.get("status") == "PASS" or e.get("event_type") == "completed")

    return {
        "summary": f"Processed {len(events)} events today. {successes} successful, {failures} failed.",
        "key_observations": [
            f"Most frequent event type: {max(event_types, key=event_types.get) if event_types else 'N/A'}",
            f"Success rate: {successes}/{len(events)} ({100*successes/len(events):.1f}%)" if events else "N/A"
        ],
        "lessons_learned": [
            "LLM unavailable for detailed analysis" if failures > 0 else "System operating normally"
        ],
        "recommendations": [],
        "statistics": {
            "total_events": len(events),
            "event_types": event_types,
            "successes": successes,
            "failures": failures
        }
    }


def _acquire_daily_lock(force: bool = False) -> bool:
    """Ensure sleep loop runs once per day (unless forced)."""
    runs = Path("runs/ai_trader")
    dstr = datetime.now(timezone.utc).date().isoformat()
    locks = runs / dstr / "locks"
    locks.mkdir(parents=True, exist_ok=True)
    lock = locks / "sleep_loop.lock"
    if lock.exists() and not force:
        return False
    lock.write_text(str(int(datetime.now(timezone.utc).timestamp())), encoding="utf-8")
    return True


async def main_async() -> None:
    """Main sleep loop with Meta v3 enhancements + Skill Synthesis"""
    logger.debug("Debug logging is active!")
    logger.info("Starting nightly sleep loop (Meta v3 + Skill Synthesis)")

    # Collect events
    events = _collect_recent_events(days=1)
    logger.info(f"Collected {len(events)} events from last 24h")

    if events:
        # Generate LLM summary (Meta v3.1)
        summary = await _generate_daily_summary(events)

        # Write compressed summary to STS
        write_memory("STS", {
            "type": "daily_summary",
            "date": datetime.now(timezone.utc).isoformat(),
            "summary": summary.get("summary", ""),
            "key_observations": summary.get("key_observations", []),
            "lessons_learned": summary.get("lessons_learned", []),
            "recommendations": summary.get("recommendations", []),
            "statistics": summary.get("statistics", {})
        })

        logger.info("Daily summary written to STS")

        # Also save to daily_summaries.jsonl for easy access
        summary_path = Path("runs/memory/daily_summaries.jsonl")
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        with summary_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "date": datetime.now(timezone.utc).isoformat(),
                **summary
            }, ensure_ascii=False) + "\n")

        # 🆕 SKILL SYNTHESIS: Convert lessons_learned into executable skills
        lessons = summary.get("lessons_learned", [])
        if lessons:
            logger.info(f"synthesizing_skills_from_lessons: {len(lessons)} lessons")
            try:
                from libs.evolution.skill_synthesizer import SkillSynthesizer

                synthesizer = SkillSynthesizer()
                skill_specs = synthesizer.synthesize_from_lessons(
                    lessons=lessons,
                    context=summary.get("statistics", {})
                )

                if skill_specs:
                    logger.info(f"skills_synthesized_pending_approval: {len(skill_specs)} skills: {[s.skill_id for s in skill_specs]}")

                    # Generate and write skill code before queueing
                    for spec in skill_specs:
                        generated_code = synthesizer.generate_skill_code(spec, use_llm=False)  # Use templates
                        if generated_code:
                            try:
                                # Write implementation file
                                impl_path = Path(generated_code.skill_spec.file_path)
                                impl_path.parent.mkdir(parents=True, exist_ok=True)
                                impl_path.write_text(generated_code.implementation_code, encoding="utf-8")
                                logger.info(f"Wrote skill implementation: {impl_path}")

                                # Write test file
                                test_path = Path(generated_code.skill_spec.test_path)
                                test_path.parent.mkdir(parents=True, exist_ok=True)
                                test_path.write_text(generated_code.test_code, encoding="utf-8")
                                logger.info(f"Wrote skill test: {test_path}")
                            except Exception as e:
                                logger.error(f"Failed to write skill files for {spec.skill_id}: {e}")
                                continue # Skip queueing this skill if file writing fails

                    queue_file = Path("runs/memory/skill_approval_queue.jsonl")
                    queue_file.parent.mkdir(parents=True, exist_ok=True)
                    existing_ids = set()
                    if queue_file.exists():
                        try:
                            for line in queue_file.read_text(encoding="utf-8").splitlines():
                                if not line.strip():
                                    continue
                                rec = json.loads(line)
                                sid = rec.get("skill_id") or rec.get("id") or rec.get("name")
                                if sid:
                                    existing_ids.add(sid)
                        except Exception:
                            existing_ids = set()

                    queued_ids: list[str] = []
                    with queue_file.open("a", encoding="utf-8") as f:
                        for s in skill_specs:
                            skill_id = getattr(s, "skill_id", None) or getattr(s, "name", None)
                            if not skill_id:
                                continue
                            if skill_id in existing_ids:
                                continue
                            payload = {
                                "skill_id": skill_id,
                                "name": getattr(s, "name", skill_id),
                                "description": getattr(s, "description", ""),
                                "code": getattr(s, "code", ""),
                                "file_path": getattr(s, "file_path", ""),
                                "test_path": getattr(s, "test_path", ""),
                                "skill_type": getattr(s, "type", "PURE"),
                                "allow_evolution": getattr(s, "allow_evolution", True),
                                "objective": getattr(s, "objective", "PERFORMANCE"),
                                "metrics_impact": getattr(s, "metrics_impact", []),
                                "reason": "Synthesized by sleep loop",
                                "generated_at": datetime.now(timezone.utc).isoformat(),
                                "source_lesson": getattr(s, "source_lesson", ""),
                                "category": getattr(s, "category", "unknown"),
                            }
                            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
                            queued_ids.append(skill_id)
                            append_skill_flow_event(
                                {"skill_id": skill_id, "event": "queued_for_approval", "source": "sleep_loop"},
                                runs_dir="runs/ai_trader",
                            )

                    # Write skill synthesis event to memory
                    write_memory("STS", {
                        "type": "skill_synthesis",
                        "date": datetime.now(timezone.utc).isoformat(),
                        "num_lessons": len(lessons),
                        "num_skills_synthesized": len(skill_specs),
                        "skill_ids": [s.skill_id for s in skill_specs],
                        "categories": [s.category for s in skill_specs]
                    })
                    # Persist lessons/skills to runs/ai_trader for monitoring
                    day_dir = Path("runs/ai_trader") / datetime.now(timezone.utc).date().isoformat()
                    day_dir.mkdir(parents=True, exist_ok=True)
                    (day_dir / "lessons.json").write_text(
                        json.dumps(
                            {
                                "ts": datetime.now(timezone.utc).isoformat(),
                                "lessons": lessons,
                                "queued_skill_ids": queued_ids,
                            },
                            indent=2,
                            ensure_ascii=False,
                        ),
                        encoding="utf-8",
                    )
                else:
                    logger.info("no_actionable_skills_synthesized")
            except Exception as e:
                logger.error("skill_synthesis_failed", error=str(e), exc_info=True)
    else:
        logger.info("No events to summarize")

    # Run consolidation (STS->MTS->LTS promotion)
    logger.info("Running memory consolidation")
    consolidate()
    logger.info("Sleep loop completed")


def main() -> None:
    """Synchronous entry point"""
    parser = argparse.ArgumentParser(description="Nightly sleep loop (lessons + skill synthesis)")
    parser.add_argument("--force", action="store_true", help="Ignore daily lock and run regardless")
    args = parser.parse_args()

    if not _acquire_daily_lock(force=args.force):
        logger.info("sleep_loop_already_ran_today")
        append_skill_flow_event({"event": "sleep_loop_skipped", "reason": "lock"}, runs_dir="runs/ai_trader")
        return

    append_skill_flow_event({"event": "sleep_loop_started"}, runs_dir="runs/ai_trader")
    asyncio.run(main_async())
    append_skill_flow_event({"event": "sleep_loop_completed"}, runs_dir="runs/ai_trader")


if __name__ == "__main__":
    main()
