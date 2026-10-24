from __future__ import annotations

from pathlib import Path
from typing import Any, List, Optional, Union

from meta_agent.self_programming.schema import (
    CodePatchSet,
    FileModification,
    DevPlan,
    ProgrammingTask,
)
from meta_agent.self_programming.tools.local_llm import LocalLLMTool
from meta_agent.self_programming.tools.cli_wrapper import (
    ExternalCLITool,
    GeminiCLITool,
    CodexCLITool,
    create_cli_tool,
)


class ProgrammerAgent:
    """
    Produces CodePatchSet from a DevPlan using LLM tools.

    Supports:
    - LocalLLMTool (qwen/deepseek via Ollama)
    - GeminiCLITool (Gemini via CLI)
    - CodexCLITool (GPT-4/Codex via CLI)
    - Any custom ExternalCLITool
    """

    def __init__(
        self,
        repo_root: str = ".",
        use_llm: bool = True,
        tool: Optional[str] = None,
        tool_config: Optional[dict] = None,
        style_constraints: Optional[str] = None,
        safety_notes: Optional[str] = None,
    ):
        """
        Initialize ProgrammerAgent.

        Args:
            repo_root: Repository root directory
            use_llm: Enable LLM usage
            tool: Tool name ("local", "gemini", "codex") or None for default
            tool_config: Tool-specific configuration
        """
        self.repo_root = Path(repo_root).resolve()
        self.use_llm = use_llm
        self.llm_tool = None
        self.tool_name = "stub"
        self.style_constraints = style_constraints
        self.safety_notes = safety_notes or "Respect repo safety, avoid risky refactors, keep patches minimal."

        if use_llm:
            tool_config = tool_config or {}

            # Select tool based on name
            if tool == "gemini":
                self.llm_tool = GeminiCLITool(**tool_config)
            elif tool == "codex":
                self.llm_tool = CodexCLITool(**tool_config)
            elif tool and tool != "local":
                # Custom CLI tool
                self.llm_tool = create_cli_tool(tool, **tool_config)
            else:
                # Default: local LLM
                self.llm_tool = LocalLLMTool(**tool_config)
            self.tool_name = getattr(self.llm_tool, "name", tool or "local_llm")

    def code(self, plan: DevPlan, task: ProgrammingTask) -> CodePatchSet:
        """
        Generate code modifications based on DevPlan.

        Args:
            plan: Development plan with work items
            task: Original programming task for context

        Returns:
            CodePatchSet with file modifications
        """
        if not self.use_llm:
            # Stub mode - return empty patch set
            return CodePatchSet(
                task_id=plan.task_id,
                files=[],
                summary="stub patch set (LLM disabled)",
                tool_name=self.tool_name,
            )

        modifications: List[FileModification] = []

        for work_item in plan.work_items:
            file_path = work_item.get("file", work_item.get("file_path"))
            if not file_path:
                continue

            goal = work_item.get("goal", "improve code")
            patch_hint = work_item.get("patch_instructions") or ""

            # Read current file content
            abs_path = self.repo_root / file_path
            if abs_path.exists():
                content = abs_path.read_text(encoding="utf-8")
            else:
                content = ""

            guidance_parts = [
                patch_hint,
                f"Safety: {self.safety_notes}",
                f"Style: {plan.style_guide or task.style_guide or self.style_constraints or 'PEP8'}",
                f"Acceptance: {task.acceptance_criteria}",
                f"Constraints: {task.constraints}",
            ]
            guidance = "\n".join([p for p in guidance_parts if p])

            # Generate modifications using LLM
            try:
                file_mods = self.llm_tool.generate_code_changes(
                    task=task,
                    file_path=file_path,
                    file_content=content,
                    instructions=f"{goal}",
                    extra_guidance=guidance,
                )
                modifications.extend(file_mods)
            except Exception as e:
                print(f"[ProgrammerAgent] Failed to generate changes for {file_path}: {e}")
                continue

        return CodePatchSet(
            task_id=plan.task_id,
            files=modifications,
            summary=f"Generated {len(modifications)} modifications",
            tool_name=self.tool_name,
        )


__all__ = ["ProgrammerAgent"]
