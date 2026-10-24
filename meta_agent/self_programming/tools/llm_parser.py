"""
Parser for LLM output in SEARCH/====/>>> format.

Expected format:
    <<<< SEARCH
    (original code to find)
    ====
    (new code to replace with)
    >>>>

Multiple blocks can appear in sequence.
"""
from __future__ import annotations

import re
from typing import List
from meta_agent.self_programming.schema import FileModification


def parse_llm_patches(
    llm_output: str,
    file_path: str,
    default_action: str = "modify",
) -> List[FileModification]:
    """
    Parse LLM output containing SEARCH/====/>>> blocks into FileModification list.

    Args:
        llm_output: Raw text from LLM
        file_path: Target file path for all modifications
        default_action: Default action type ("create" or "modify")

    Returns:
        List of FileModification objects
    """
    modifications: List[FileModification] = []

    # Pattern to match blocks:
    # <<<< SEARCH
    # ...original...
    # ====
    # ...new...
    # >>>>
    pattern = r"<<<<\s*SEARCH\s*\n(.*?)\n====\s*\n(.*?)\n>>>>"

    matches = re.findall(pattern, llm_output, re.DOTALL)

    for original, new in matches:
        # Strip leading/trailing whitespace but preserve internal structure
        original_stripped = original.strip()
        new_stripped = new.strip()

        if not new_stripped:
            # Empty replacement - skip (could be deletion but we don't handle that yet)
            continue

        modifications.append(
            FileModification(
                file_path=file_path,
                action=default_action,
                original_snippet=original_stripped if original_stripped else None,
                new_content=new_stripped,
            )
        )

    return modifications


def parse_llm_patches_multi_file(llm_output: str) -> List[FileModification]:
    """
    Parse LLM output that may specify files using FILE: markers.

    Expected format:
        FILE: path/to/file.py
        <<<< SEARCH
        ...
        ====
        ...
        >>>>

        FILE: another/file.py
        <<<< SEARCH
        ...
        ====
        ...
        >>>>

    Returns:
        List of FileModification objects with appropriate file paths
    """
    modifications: List[FileModification] = []

    # Split by FILE: markers
    file_sections = re.split(r'^FILE:\s*(.+)$', llm_output, flags=re.MULTILINE)

    # file_sections[0] is content before first FILE: (usually empty)
    # file_sections[1] = first filepath, file_sections[2] = first content
    # file_sections[3] = second filepath, file_sections[4] = second content, etc.

    for i in range(1, len(file_sections), 2):
        if i + 1 < len(file_sections):
            file_path = file_sections[i].strip()
            content = file_sections[i + 1]

            # Parse patches for this file
            file_mods = parse_llm_patches(content, file_path)
            modifications.extend(file_mods)

    return modifications


def extract_code_blocks(llm_output: str) -> List[str]:
    """
    Extract markdown code blocks from LLM output.
    Useful when LLM returns ```python ... ``` instead of SEARCH format.

    Returns:
        List of code block contents
    """
    pattern = r"```(?:python|py)?\n(.*?)```"
    matches = re.findall(pattern, llm_output, re.DOTALL)
    return [m.strip() for m in matches]


def format_prompt_for_patches(
    file_path: str,
    file_content: str,
    instruction: str,
) -> str:
    """
    Generate a prompt that encourages LLM to use SEARCH/====/>>> format.

    Args:
        file_path: Path to file being modified
        file_content: Current file content
        instruction: What to change

    Returns:
        Formatted prompt string
    """
    return f"""You are a code modification assistant. Your task is to generate precise code patches.

FILE: {file_path}

CURRENT CONTENT:
```python
{file_content}
```

INSTRUCTION:
{instruction}

OUTPUT FORMAT:
You MUST output patches in the following format. You can output multiple blocks if needed:

<<<< SEARCH
(exact code snippet to find in the file)
====
(new code to replace it with)
>>>>

IMPORTANT RULES:
1. The SEARCH block must match existing code exactly (or very closely)
2. Include enough context to uniquely identify the location
3. The replacement (after ====) should be complete, not partial
4. If making multiple changes, output multiple blocks
5. Do NOT include explanations outside the blocks
6. Do NOT use markdown code fences (```python)

Now generate the patches:
"""


__all__ = [
    "parse_llm_patches",
    "parse_llm_patches_multi_file",
    "extract_code_blocks",
    "format_prompt_for_patches",
]
