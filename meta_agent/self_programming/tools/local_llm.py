from __future__ import annotations

from typing import Any, Dict, List

from meta_agent.self_programming.schema import FileModification, ProgrammingTask
from meta_agent.self_programming.tools.llm_parser import (
    parse_llm_patches,
    parse_llm_patches_multi_file,
    format_prompt_for_patches,
)


class LocalLLMTool:
    """
    Thin wrapper around provider pool for local LLM calls.
    Generates code patches using SEARCH/====/>>> format.
    """

    name = "local_llm"

    def __init__(self, runs_dir: str = "runs/ai_trader") -> None:
        self.runs_dir = runs_dir

    def run(self, prompt: str, operation: str = "self_programming") -> Dict[str, Any]:
        """
        Low-level LLM call - returns raw response.
        """
        try:
            from scripts.ai_trader.brain.provider_fallback import ProviderPool, ProviderConfig  # type: ignore
        except Exception as exc:
            return {"ok": False, "error": f"provider_import_failed: {exc}"}
        cfgs = [
            ProviderConfig(name="ollama", model="qwen2.5:7b-instruct", priority=1, timeout=120),
            ProviderConfig(name="ollama", model="deepseek-r1:1.5b", priority=2, timeout=120),
        ]
        pool = ProviderPool(configs=cfgs, runs_dir=self.runs_dir)
        try:
            res = pool.call_with_fallback(prompt, operation=operation)
            return {"ok": True, "raw": res.raw, "data": getattr(res, "data", None)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def generate_code_changes(
        self,
        task: ProgrammingTask,
        file_path: str,
        file_content: str,
        instructions: str,
        extra_guidance: str | None = None,
    ) -> List[FileModification]:
        """
        Generate code modifications for a specific file.

        Args:
            task: The programming task context
            file_path: Target file path
            file_content: Current file content
            instructions: What changes to make

        Returns:
            List of FileModification objects
        """
        # Build prompt
        guidance = extra_guidance or ""
        prompt = format_prompt_for_patches(
            file_path=file_path,
            file_content=file_content,
            instruction=(
                f"{task.description}\n\nSpecific instruction: {instructions}\n\n"
                f"Acceptance criteria: {task.acceptance_criteria}\n"
                f"Constraints: {task.constraints}\n"
                f"{guidance}"
            ),
        )

        # Call LLM
        result = self.run(prompt, operation="code_generation")

        if not result.get("ok"):
            # LLM call failed - return empty list
            print(f"[LocalLLMTool] LLM call failed: {result.get('error')}")
            return []

        llm_output = result.get("raw", "")

        # Parse patches
        if "FILE:" in llm_output:
            modifications = parse_llm_patches_multi_file(llm_output)
        else:
            modifications = parse_llm_patches(llm_output, file_path)

        return modifications

    def generate_tests(
        self,
        task: ProgrammingTask,
        context: str,
    ) -> List[FileModification]:
        """
        Generate test file modifications.

        Args:
            task: The programming task
            context: Context about what was changed

        Returns:
            List of FileModification for test files
        """
        # Simplified: generate a test file name
        # In production, this would analyze existing test structure
        test_file = "tests/test_generated.py"

        prompt = f"""Generate pytest unit tests for the following changes:

TASK: {task.description}

CONTEXT:
{context}

Generate complete test file content with:
1. Import statements
2. Test class or functions
3. At least 2-3 test cases covering main functionality

Output only the complete Python test code, no explanations.
"""

        result = self.run(prompt, operation="test_generation")

        if not result.get("ok"):
            return []

        test_code = result.get("raw", "").strip()

        # Wrap in FileModification
        return [
            FileModification(
                file_path=test_file,
                action="create",
                original_snippet=None,
                new_content=test_code,
            )
        ]


__all__ = ["LocalLLMTool"]
