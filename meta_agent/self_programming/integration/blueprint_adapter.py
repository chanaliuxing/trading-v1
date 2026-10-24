"""
Blueprint Integration Adapter

Converts Blueprint gaps and improvement tasks into ProgrammingTask objects
for the self-programming pipeline.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Dict, Any, Optional
from pathlib import Path

from meta_agent.self_programming.schema import ProgrammingTask


@dataclass
class BlueprintGap:
    """
    Represents a gap identified in the blueprint system.
    This is a placeholder - adapt to your actual Blueprint gap structure.
    """
    gap_id: str
    description: str
    category: str  # "missing_feature", "bug", "refactor", "test_coverage", etc.
    severity: str  # "low", "medium", "high", "critical"
    affected_files: List[str]
    context: str
    acceptance_criteria: List[str]
    estimated_risk: str  # "low", "medium", "high"


# File whitelist - only these files can be auto-modified
SAFE_FILES_WHITELIST = {
    # Meta-agent itself (self-improvement)
    "meta_agent/self_programming/**/*.py",
    "meta_agent/self_programming/*.py",
    "meta_agent/**/*.py",

    # AI Brain components (low-risk)
    "ai/strategy_council.py",
    "ai/plan_outcome_logger.py",

    # Tools and utilities
    "scripts/ai_trader/brain/tools_ml.py",
    "scripts/ai_trader/brain/provider_fallback.py",

    # Monitoring and logging (safe)
    "scripts/ai_trader/monitoring_dashboard.py",
    "scripts/ai_trader/health_snapshot.py",

    # Test files (always safe)
    "tests/**/*.py",
    "tests/*.py",

    # Documentation
    "docs/**/*.md",
    "**/*.md",

    # Demo/example files (safe for testing)
    "demo.py",
    "test_*.py",
}

# High-risk files - NEVER auto-modify
FORBIDDEN_FILES = {
    # Core trading execution
    "services/exec/**/*.py",
    "services/decision/**/*.py",

    # Order management
    "services/order/**/*.py",

    # Risk kernel
    "services/exec/safety_kernel.py",

    # Database schema
    "libs/db/**/*.py",

    # Configuration
    "config.yml",
    ".env",

    # Infrastructure
    "infra/**/*",
}


def is_file_whitelisted(file_path: str) -> bool:
    """
    Check if a file is in the safe whitelist and not in forbidden list.

    Args:
        file_path: Path to check (relative to repo root)

    Returns:
        True if file can be auto-modified
    """
    from pathlib import Path
    import fnmatch

    # Check forbidden first
    for pattern in FORBIDDEN_FILES:
        if fnmatch.fnmatch(file_path, pattern):
            return False

    # Check whitelist
    for pattern in SAFE_FILES_WHITELIST:
        if fnmatch.fnmatch(file_path, pattern):
            return True

    return False


def filter_safe_files(files: List[str]) -> List[str]:
    """
    Filter file list to only include whitelisted files.

    Args:
        files: List of file paths

    Returns:
        List of safe files
    """
    return [f for f in files if is_file_whitelisted(f)]


def estimate_task_risk(gap: BlueprintGap) -> str:
    """
    Estimate risk level for a gap-based task.

    Risk factors:
    - Gap severity
    - Number of affected files
    - Whether files touch core trading logic
    - Gap category

    Returns:
        "low", "medium", or "high"
    """
    risk_score = 0

    # Severity contribution
    severity_map = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    risk_score += severity_map.get(gap.severity, 0)

    # File count contribution
    if len(gap.affected_files) > 5:
        risk_score += 2
    elif len(gap.affected_files) > 2:
        risk_score += 1

    # Check if any forbidden files
    for file in gap.affected_files:
        if not is_file_whitelisted(file):
            risk_score += 3
            break

    # Category contribution
    high_risk_categories = {"refactor", "architecture", "database"}
    if gap.category in high_risk_categories:
        risk_score += 1

    # Convert score to level
    if risk_score >= 4:
        return "high"
    elif risk_score >= 2:
        return "medium"
    else:
        return "low"


def blueprint_gap_to_programming_task(
    gap: BlueprintGap,
    repo_root: str = ".",
    force_whitelist_check: bool = True,
) -> Optional[ProgrammingTask]:
    """
    Convert a Blueprint gap into a ProgrammingTask.

    Args:
        gap: Blueprint gap to convert
        repo_root: Repository root directory
        force_whitelist_check: If True, only include whitelisted files

    Returns:
        ProgrammingTask if convertible, None if gap cannot be automated
    """
    # Filter files to only safe ones
    if force_whitelist_check:
        safe_files = filter_safe_files(gap.affected_files)
        if not safe_files:
            # No safe files to modify - cannot automate
            return None
    else:
        safe_files = gap.affected_files

    # Estimate risk
    risk_level = estimate_task_risk(gap)

    # High-risk tasks should not be automated
    if risk_level == "high" and force_whitelist_check:
        return None

    # Build constraints
    constraints = {
        "risk_level": risk_level,
        "gap_severity": gap.severity,
        "gap_category": gap.category,
        "max_files": 5,  # Limit scope
        "requires_tests": True,  # Always require tests
        "whitelist_enforced": force_whitelist_check,
    }

    # Create task
    task = ProgrammingTask(
        task_id=f"blueprint-{gap.gap_id}",
        origin="blueprint_gap",
        description=gap.description,
        repo_root=repo_root,
        related_files=safe_files,
        context_summary=f"Gap Category: {gap.category}\nSeverity: {gap.severity}\n\n{gap.context}",
        constraints=constraints,
        acceptance_criteria=gap.acceptance_criteria,
    )

    return task


def create_gap_from_todo(
    description: str,
    files: List[str],
    category: str = "improvement",
    severity: str = "medium",
) -> BlueprintGap:
    """
    Helper to create BlueprintGap from simple inputs.
    Useful for manual testing.

    Args:
        description: What needs to be done
        files: Files to modify
        category: Gap category
        severity: Gap severity

    Returns:
        BlueprintGap object
    """
    import hashlib

    # Generate ID from description
    gap_id = hashlib.md5(description.encode()).hexdigest()[:8]

    return BlueprintGap(
        gap_id=gap_id,
        description=description,
        category=category,
        severity=severity,
        affected_files=files,
        context=f"Auto-generated gap for: {description}",
        acceptance_criteria=[
            "Code compiles without errors",
            "Tests pass",
            "No new linting warnings",
        ],
        estimated_risk="low",
    )


# Example usage
def example_integration():
    """
    Example of how to use blueprint integration.
    """
    # Create a sample gap
    gap = create_gap_from_todo(
        description="Add docstrings to all functions in demo.py",
        files=["demo.py"],
        category="documentation",
        severity="low",
    )

    # Convert to task
    task = blueprint_gap_to_programming_task(gap)

    if task:
        print(f"✅ Created task: {task.task_id}")
        print(f"   Risk level: {task.constraints['risk_level']}")
        print(f"   Files: {task.related_files}")
    else:
        print("❌ Gap cannot be automated (no safe files or too risky)")

    return task


__all__ = [
    "BlueprintGap",
    "blueprint_gap_to_programming_task",
    "create_gap_from_todo",
    "is_file_whitelisted",
    "filter_safe_files",
    "SAFE_FILES_WHITELIST",
    "FORBIDDEN_FILES",
]
