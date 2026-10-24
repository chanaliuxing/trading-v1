from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional


@dataclass
class FileModification:
    """
    Single-file change unit for the self-programming pipeline.
    """

    file_path: str
    action: Literal["create", "modify", "delete"]
    original_snippet: Optional[str]
    new_content: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CodePatchSet:
    task_id: str
    files: List[FileModification]
    summary: str
    tool_name: Optional[str] = None


@dataclass
class ProgrammingTask:
    task_id: str
    origin: Literal["blueprint_gap", "bugfix", "refactor", "experiment"]
    description: str
    repo_root: str
    related_files: List[str]
    context_summary: str
    constraints: Dict[str, Any]
    acceptance_criteria: List[str]
    style_guide: Optional[str] = None


@dataclass
class DevPlan:
    task_id: str
    task_summary: str
    impact_analysis: Dict[str, Any]
    work_items: List[Dict[str, Any]]
    test_plan: Dict[str, Any]
    notes: str = ""
    safety_notes: str = ""
    style_guide: Optional[str] = None


@dataclass
class TestPlan:
    task_id: str
    framework: Literal["pytest", "unittest", "none"]
    tests: List[Dict[str, Any]]


@dataclass
class TestReport:
    task_id: str
    overall_status: Literal["passed", "failed", "partial", "skipped", "error", "timeout"]
    results: List[Dict[str, Any]]
    raw_output: str
    return_code: Optional[int] = None


@dataclass
class ReviewDecision:
    task_id: str
    status: Literal["approve", "request_changes", "reject"]
    severity: Literal["low", "medium", "high"]
    issues: List[Dict[str, Any]]
    summary: str
    follow_up_actions: List[str]
    static_report: Optional[Dict[str, Any]] = None
    llm_notes: Optional[str] = None


@dataclass
class ProgrammingResult:
    task: ProgrammingTask
    dev_plan: DevPlan
    patch_set: CodePatchSet
    test_plan: TestPlan
    test_report: TestReport
    review: ReviewDecision
    tools_used: List[str]
    patch_reports: List[Dict[str, Any]] = field(default_factory=list)
    audit_log_path: Optional[str] = None
