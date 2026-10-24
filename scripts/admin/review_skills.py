#!/usr/bin/env python3
from __future__ import annotations

"""
Skill review CLI.
 - Interactive review: approve/reject pending skills in the queue.
 - Auto-approve mode: run EvolutionChamber validation and register passing skills.
"""

import argparse
import json
from pathlib import Path
from typing import List

from libs.evolution.evolution_chamber import EvolutionChamber
from libs.skills.runtime import append_skill_flow_event

PENDING_PATH = Path("runs/memory/skill_approval_queue.jsonl")
REGISTRY_PATH = Path("config/skill_registry.json")


def load_pending() -> list:
    if not PENDING_PATH.exists():
        return []
    try:
        # Handle JSONL format (one JSON per line)
        skills = []
        with PENDING_PATH.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    skills.append(json.loads(line))
        return skills
    except Exception:
        return []


def save_pending(skills: list) -> None:
    PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Write as JSONL format (one JSON per line)
    with PENDING_PATH.open("w", encoding="utf-8") as f:
        for skill in skills:
            f.write(json.dumps(skill, ensure_ascii=False) + "\n")


def load_registry() -> dict:
    if not REGISTRY_PATH.exists():
        return {"skills": {}}
    try:
        return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"skills": {}}


def save_registry(reg: dict) -> None:
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY_PATH.write_text(json.dumps(reg, indent=2, ensure_ascii=False), encoding="utf-8")


def _auto_approve(enable_on_pass: bool = False) -> None:
    chamber = EvolutionChamber(
        queue_path=PENDING_PATH,
        registry_path=REGISTRY_PATH,
        runs_dir="runs/ai_trader",
    )
    append_skill_flow_event({"event": "auto_approve_started"}, runs_dir="runs/ai_trader")
    outcomes = chamber.process_queue(auto_register=True, enable_on_pass=enable_on_pass)
    for outcome in outcomes:
        status = "✅" if outcome.status == "approved" else "❌"
        print(f"{status} {outcome.skill_id} {outcome.reason} coverage={outcome.coverage_pct}")
    if not outcomes:
        print("No pending skills to auto-approve.")


def review():
    pending = load_pending()
    if not pending:
        print("No pending skills found.")
        return
    registry = load_registry()
    skills = registry.get("skills", {})

    keep = []
    for skill in pending:
        # Filter out already approved skills
        if skill.get("status") == "approved":
            continue

        skill_id = skill.get("skill_id") or skill.get("name") or skill.get("id") or "unknown_skill"
        name = skill.get("name", skill_id)
        print("\n--- Pending Skill ---")
        print(f"ID: {skill_id}")
        print(f"Name: {name}")
        print(f"Category: {skill.get('category', 'N/A')}")
        print(f"Type: {skill.get('skill_type', 'N/A')}")
        print(f"Description: {skill.get('description','')}")
        print(f"Source Lesson: {skill.get('source_lesson', 'N/A')}")
        print(f"File Path: {skill.get('file_path', 'N/A')}")
        print(f"Test Path: {skill.get('test_path', 'N/A')}")

        choice = input("Approve? [y/N]: ").strip().lower()
        if choice == "y":
            skills[skill_id] = {
                "file_path": skill.get("file_path", ""),
                "test_path": skill.get("test_path", ""),
                "type": skill.get("skill_type", "PURE"),
                "description": skill.get("description", ""),
                "allow_evolution": skill.get("allow_evolution", True),
                "objective": skill.get("objective", "PERFORMANCE"),
                "metrics_impact": skill.get("metrics_impact", []),
                "category": skill.get("category", "unknown"),
                "approved_at": skill.get("generated_at", ""),
            }
            # Mark as approved
            skill["status"] = "approved"
            keep.append(skill)  # Keep approved skills in queue with status updated
            print(f"✅ Approved {skill_id}")
        else:
            # Keep for next review
            keep.append(skill)

    registry["skills"] = skills
    save_registry(registry)
    save_pending(keep)
    print("Review complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Review or auto-approve skills in the pending queue.")
    parser.add_argument("--auto-approve", action="store_true", help="Run automated validation and register passing skills.")
    parser.add_argument("--enable-on-pass", action="store_true", help="Enable skills immediately when auto-approving.")
    args = parser.parse_args()

    if args.auto_approve:
        _auto_approve(enable_on_pass=args.enable_on_pass)
    else:
        review()
