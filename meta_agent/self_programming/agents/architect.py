from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from meta_agent.self_programming.schema import ProgrammingTask, DevPlan
from meta_agent.self_programming.tools.local_llm import LocalLLMTool


class ArchitectAgent:
    """
    Produces DevPlan from a ProgrammingTask.

    When use_llm=True, we ask the local ProviderPool-backed LLM to emit a
    structured JSON plan (work_items + test focus). Falls back to a deterministic
    heuristic plan if parsing fails.
    """

    def __init__(
        self,
        use_llm: bool = False,
        style_guide: Optional[str] = None,
        safety_notes: Optional[str] = None,
        tool_config: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.use_llm = use_llm
        self.style_guide = style_guide
        self.safety_notes = safety_notes or ""
        # Filter tool_config to only valid LocalLLMTool params (runs_dir)
        if use_llm:
            filtered_config = {}
            if tool_config and "runs_dir" in tool_config:
                filtered_config["runs_dir"] = tool_config["runs_dir"]
            self.llm = LocalLLMTool(**filtered_config)
        else:
            self.llm = None

    def _extract_json(self, text: str) -> Optional[Dict[str, Any]]:
        """
        Extract JSON payload from raw LLM text (handles fenced code blocks).
        """
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            if cleaned.startswith("json"):
                cleaned = cleaned[4:]
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            return None

    def _llm_plan(self, task: ProgrammingTask) -> Optional[DevPlan]:
        if not self.llm:
            return None

        constraint_text = json.dumps(task.constraints, ensure_ascii=False)
        prompt = f"""You are the Architect agent for a self-programming system.
You must output a concise JSON object describing how to modify code safely.

Task: {task.description}
Files: {task.related_files}
Context: {task.context_summary}
Acceptance criteria: {task.acceptance_criteria}
Constraints: {constraint_text}
Style guide: {self.style_guide or task.style_guide or 'follow PEP8 and repository conventions'}
Safety: {self.safety_notes or 'avoid risky changes, keep scope tight, respect whitelist'}

Required JSON schema:
{{
  "task_summary": "...",
  "work_items": [
     {{"file": "path.py", "goal": "what to change", "patch_instructions": "precise snippet or rule"}}
  ],
  "test_plan": [
     {{"id": "string", "command": ["pytest", "-q", "tests/test_target.py"], "description": "why"}}
  ],
  "notes": "optional extra guidance"
}}

Respond with ONLY JSON (no code fences)."""

        llm_res = self.llm.run(prompt, operation="architect_plan")
        if not llm_res.get("ok"):
            return None

        payload = self._extract_json(str(llm_res.get("raw", "")).strip())
        if not payload:
            return None

        work_items = payload.get("work_items") or [
            {"file": f, "goal": "review_and_edit"} for f in task.related_files
        ]
        test_items = payload.get("test_plan") or []

        return DevPlan(
            task_id=task.task_id,
            task_summary=payload.get("task_summary", task.description),
            impact_analysis={"scope": task.related_files},
            work_items=work_items,
            test_plan={"framework": "pytest", "tests": test_items},
            notes=payload.get("notes", ""),
            safety_notes=self.safety_notes,
            style_guide=self.style_guide or task.style_guide,
        )

    def plan(self, task: ProgrammingTask) -> DevPlan:
        plan = self._llm_plan(task) if self.use_llm else None
        if plan:
            return plan

        # Heuristic fallback
        work_items: List[Dict[str, Any]] = []
        for f in task.related_files:
            work_items.append(
                {
                    "file": f,
                    "goal": "review_and_edit",
                    "patch_instructions": "keep changes minimal and safe",
                }
            )

        return DevPlan(
            task_id=task.task_id,
            task_summary=task.description,
            impact_analysis={"scope": task.related_files},
            work_items=work_items,
            test_plan={"framework": "pytest", "tests": []},
            notes="auto-generated heuristic plan",
            safety_notes=self.safety_notes,
            style_guide=self.style_guide or task.style_guide,
        )


__all__ = ["ArchitectAgent"]
