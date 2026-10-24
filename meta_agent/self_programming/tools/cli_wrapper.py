"""
CLI Wrapper for external code generation tools (Gemini CLI, Codex CLI, etc.)

This module provides adapters for calling external CLI tools and parsing their outputs
into FileModification objects compatible with the self-programming pipeline.
"""
from __future__ import annotations

import subprocess
import json
from typing import List, Dict, Any, Optional
from pathlib import Path

from meta_agent.self_programming.schema import FileModification, ProgrammingTask
from meta_agent.self_programming.tools.llm_parser import (
    parse_llm_patches,
    format_prompt_for_patches,
)


class ExternalCLITool:
    """
    Base class for external CLI code generation tools.
    """

    def __init__(self, name: str, cli_command: str):
        self.name = name
        self.cli_command = cli_command

    def _run_cli(
        self,
        prompt: str,
        timeout: int = 120,
        extra_args: List[str] = None,
    ) -> Dict[str, Any]:
        """
        Run the CLI tool with a prompt.

        Args:
            prompt: The prompt to send
            timeout: Command timeout in seconds
            extra_args: Additional CLI arguments

        Returns:
            Dict with 'ok', 'stdout', 'stderr', 'returncode'
        """
        cmd = [self.cli_command]
        if extra_args:
            cmd.extend(extra_args)

        try:
            proc = subprocess.run(
                cmd,
                input=prompt,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )

            return {
                "ok": proc.returncode == 0,
                "returncode": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
            }

        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "error": f"CLI timeout after {timeout}s",
            }
        except Exception as e:
            return {
                "ok": False,
                "error": str(e),
            }

    def generate_code_changes(
        self,
        task: ProgrammingTask,
        file_path: str,
        file_content: str,
        instructions: str,
    ) -> List[FileModification]:
        """
        Generate code modifications using the external CLI tool.

        Args:
            task: Programming task context
            file_path: Target file
            file_content: Current file content
            instructions: What to change

        Returns:
            List of FileModification objects
        """
        raise NotImplementedError("Subclass must implement generate_code_changes")


class GeminiCLITool(ExternalCLITool):
    """
    Wrapper for Gemini CLI tool (gemini-cli, aichat, etc.)

    Assumes the CLI tool:
    - Reads prompt from stdin
    - Outputs response to stdout
    - Supports a flag to set the model (optional)

    Example CLI tools:
    - `aichat` (https://github.com/sigoden/aichat)
    - `gemini-cli` (custom)
    """

    def __init__(
        self,
        cli_command: str = "aichat",
        model: Optional[str] = "gemini-pro",
        extra_args: List[str] = None,
    ):
        super().__init__(name="gemini_cli", cli_command=cli_command)
        self.model = model
        self.extra_args = extra_args or []

    def generate_code_changes(
        self,
        task: ProgrammingTask,
        file_path: str,
        file_content: str,
        instructions: str,
    ) -> List[FileModification]:
        """
        Generate patches using Gemini via CLI.
        """
        # Build prompt using standard format
        prompt = format_prompt_for_patches(
            file_path=file_path,
            file_content=file_content,
            instruction=f"{task.description}\n\nSpecific: {instructions}",
        )

        # Add model selection if available
        args = self.extra_args.copy()
        if self.model:
            args.extend(["--model", self.model])

        # Call CLI
        result = self._run_cli(prompt, extra_args=args)

        if not result.get("ok"):
            print(f"[GeminiCLI] Failed: {result.get('error', 'unknown error')}")
            return []

        # Parse output
        llm_output = result.get("stdout", "")
        modifications = parse_llm_patches(llm_output, file_path)

        return modifications


class CodexCLITool(ExternalCLITool):
    """
    Wrapper for OpenAI Codex CLI tools.

    Example CLI tools:
    - `openai` CLI (official)
    - `aider` (https://github.com/paul-gauthier/aider)
    - Custom wrapper scripts
    """

    def __init__(
        self,
        cli_command: str = "openai",
        model: str = "gpt-4",
        extra_args: List[str] = None,
    ):
        super().__init__(name="codex_cli", cli_command=cli_command)
        self.model = model
        self.extra_args = extra_args or []

    def generate_code_changes(
        self,
        task: ProgrammingTask,
        file_path: str,
        file_content: str,
        instructions: str,
    ) -> List[FileModification]:
        """
        Generate patches using Codex/GPT-4 via CLI.
        """
        # Build prompt
        prompt = format_prompt_for_patches(
            file_path=file_path,
            file_content=file_content,
            instruction=f"{task.description}\n\n{instructions}",
        )

        # Build CLI args (depends on tool)
        args = self.extra_args.copy()

        # For official OpenAI CLI:
        # openai api completions.create -m gpt-4 -p "..."
        if "openai" in self.cli_command:
            args.extend([
                "api",
                "completions.create",
                "-m", self.model,
            ])

        # Call CLI
        result = self._run_cli(prompt, extra_args=args)

        if not result.get("ok"):
            print(f"[CodexCLI] Failed: {result.get('error', 'unknown')}")
            return []

        # Parse output
        llm_output = result.get("stdout", "")

        # Try to extract from JSON if OpenAI CLI format
        if "openai" in self.cli_command:
            try:
                response = json.loads(llm_output)
                llm_output = response.get("choices", [{}])[0].get("text", "")
            except (json.JSONDecodeError, KeyError, IndexError):
                pass  # Use raw output

        modifications = parse_llm_patches(llm_output, file_path)

        return modifications


class AiderTool(ExternalCLITool):
    """
    Wrapper for Aider (https://github.com/paul-gauthier/aider)

    Aider is a specialized CLI tool for AI pair programming that directly
    edits files. This wrapper adapts it to our FileModification format.
    """

    def __init__(
        self,
        cli_command: str = "aider",
        model: str = "gpt-4",
    ):
        super().__init__(name="aider", cli_command=cli_command)
        self.model = model

    def generate_code_changes(
        self,
        task: ProgrammingTask,
        file_path: str,
        file_content: str,
        instructions: str,
    ) -> List[FileModification]:
        """
        Generate patches using Aider.

        NOTE: Aider directly modifies files, so this is a special case.
        We'll run it in a temp directory and capture the diff.
        """
        # Build aider command:
        # aider --model gpt-4 --message "instructions" file.py
        args = [
            "--model", self.model,
            "--message", f"{task.description}\n{instructions}",
            "--yes",  # Auto-confirm
            file_path,
        ]

        result = self._run_cli("", extra_args=args)

        if not result.get("ok"):
            print(f"[Aider] Failed: {result.get('error', 'unknown')}")
            return []

        # Aider modifies files directly - we'd need to capture git diff
        # This is a simplified version - production would use temp dir
        print("[Aider] Note: Aider modifies files directly")
        print("[Aider] Consider using git diff to extract changes")

        # For now, return empty - would need more integration work
        return []


class GenericCLITool(ExternalCLITool):
    """
    Generic wrapper for any CLI tool that:
    - Reads prompt from stdin
    - Outputs code to stdout

    Examples:
    - Custom shell scripts
    - llm CLI (https://github.com/simonw/llm)
    - sgpt (https://github.com/TheR1D/shell_gpt)
    """

    def __init__(
        self,
        cli_command: str,
        name: str = "generic_cli",
        extra_args: List[str] = None,
    ):
        super().__init__(name=name, cli_command=cli_command)
        self.extra_args = extra_args or []

    def generate_code_changes(
        self,
        task: ProgrammingTask,
        file_path: str,
        file_content: str,
        instructions: str,
    ) -> List[FileModification]:
        """
        Generate patches using generic CLI tool.
        """
        prompt = format_prompt_for_patches(
            file_path=file_path,
            file_content=file_content,
            instruction=f"{task.description}\n\n{instructions}",
        )

        result = self._run_cli(prompt, extra_args=self.extra_args)

        if not result.get("ok"):
            print(f"[{self.name}] Failed: {result.get('error', 'unknown')}")
            return []

        llm_output = result.get("stdout", "")
        modifications = parse_llm_patches(llm_output, file_path)

        return modifications


# Factory function
def create_cli_tool(tool_name: str, **kwargs) -> ExternalCLITool:
    """
    Factory to create CLI tool instances.

    Args:
        tool_name: One of "gemini", "codex", "aider", or a custom command
        **kwargs: Tool-specific arguments

    Returns:
        ExternalCLITool instance

    Examples:
        >>> tool = create_cli_tool("gemini", model="gemini-pro")
        >>> tool = create_cli_tool("codex", model="gpt-4")
        >>> tool = create_cli_tool("custom", cli_command="my-llm-tool")
    """
    tool_map = {
        "gemini": GeminiCLITool,
        "codex": CodexCLITool,
        "aider": AiderTool,
    }

    if tool_name in tool_map:
        return tool_map[tool_name](**kwargs)
    else:
        # Custom CLI command
        return GenericCLITool(cli_command=tool_name, **kwargs)


__all__ = [
    "ExternalCLITool",
    "GeminiCLITool",
    "CodexCLITool",
    "AiderTool",
    "GenericCLITool",
    "create_cli_tool",
]
