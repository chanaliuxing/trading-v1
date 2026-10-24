"""
Integration adapters for connecting self-programming to external systems.
"""
from meta_agent.self_programming.integration.blueprint_adapter import (
    BlueprintGap,
    blueprint_gap_to_programming_task,
    create_gap_from_todo,
    is_file_whitelisted,
    filter_safe_files,
)

__all__ = [
    "BlueprintGap",
    "blueprint_gap_to_programming_task",
    "create_gap_from_todo",
    "is_file_whitelisted",
    "filter_safe_files",
]
