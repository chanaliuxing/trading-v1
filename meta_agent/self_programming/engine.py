from __future__ import annotations

import json
import os
import fnmatch
from typing import Any, Dict, Optional, List

from meta_agent.self_programming.schema import (
    ProgrammingTask,
    ProgrammingResult,
    DevPlan,
    CodePatchSet,
    TestPlan,
    TestReport,
    ReviewDecision,
)
from meta_agent.self_programming.infra.git_ops import GitSafetyLayer
from meta_agent.self_programming.infra.file_patcher import FilePatcher
from meta_agent.self_programming.agents.architect import ArchitectAgent
from meta_agent.self_programming.agents.programmer import ProgrammerAgent
from meta_agent.self_programming.agents.tester import TesterAgent
from meta_agent.self_programming.agents.reviewer import ReviewerAgent

DEFAULT_AUTO_MERGE_WHITELIST = [
    "meta_agent/self_programming/**",
    "scripts/run_self_programming.py",
    "docs/**/*.md",
    "tests/**",
    "demo.py",
]


class SelfProgrammingEngine:
    """
    Orchestrates the self-programming pipeline.
    Current implementation is a stub wired for future expansion.
    """

    def __init__(
        self,
        repo_root: str = ".",
        use_llm: bool = True,
        tool: Optional[str] = None,
        tool_config: Optional[Dict[str, Any]] = None,
        style_guide: Optional[str] = None,
        safety_notes: Optional[str] = None,
        static_checker: str = "ruff",
        auto_merge: bool = True,
        auto_merge_whitelist: Optional[List[str]] = None,
        auto_merge_target: Optional[str] = None,
    ) -> None:
        """
        Initialize SelfProgrammingEngine.

        Args:
            repo_root: Repository root directory
            use_llm: Enable LLM usage
            tool: LLM tool to use ("local", "gemini", "codex", or custom)
            tool_config: Tool-specific configuration dict
            style_guide: Optional style guidance for Architect/Programmer
            safety_notes: Safety guidance injected into prompts
            static_checker: Which static tool to run (ruff/flake8/mypy)
            auto_merge: Whether to auto-merge sandbox branches for safe paths
            auto_merge_whitelist: Glob patterns eligible for auto-merge
            auto_merge_target: Branch to merge into (defaults to current base)
        """
        self.repo_root = repo_root
        self.use_llm = use_llm
        self.auto_merge = auto_merge
        self.auto_merge_target = auto_merge_target
        self.auto_merge_whitelist = auto_merge_whitelist or DEFAULT_AUTO_MERGE_WHITELIST
        self.audit_dir = os.path.join(repo_root, "runs", "self_programming")
        self.git = GitSafetyLayer(repo_root)
        self.patcher = FilePatcher()
        self.architect = ArchitectAgent(
            use_llm=use_llm,
            style_guide=style_guide,
            safety_notes=safety_notes,
            tool_config=tool_config,
        )
        self.programmer = ProgrammerAgent(
            repo_root=repo_root,
            use_llm=use_llm,
            tool=tool,
            tool_config=tool_config,
            style_constraints=style_guide,
            safety_notes=safety_notes,
        )
        self.tester = TesterAgent(repo_root=repo_root)
        self.reviewer = ReviewerAgent(
            repo_root=repo_root,
            static_checker=static_checker,
            use_llm=use_llm,
            llm_tool=getattr(self.programmer, "llm_tool", None),
        )

    def _should_auto_merge(self, patch_set: CodePatchSet) -> bool:
        if not patch_set.files:
            return False
        for mod in patch_set.files:
            if not any(fnmatch.fnmatch(mod.file_path, pattern) for pattern in self.auto_merge_whitelist):
                return False
        return True

    def _record_audit(self, payload: Dict[str, Any]) -> str:
        os.makedirs(self.audit_dir, exist_ok=True)
        path = os.path.join(self.audit_dir, "audit_log.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return path

    def _collect_tools(self) -> List[str]:
        tools = []
        if self.use_llm and getattr(self.programmer, "tool_name", None):
            tools.append(self.programmer.tool_name)
        tools.append("pytest")
        if self.reviewer.static_checker:
            tools.append(self.reviewer.static_checker)
        return tools

    def run(self, task: ProgrammingTask, use_sandbox: bool = False, dry_run: bool = False) -> ProgrammingResult:
        sandbox_branch: Optional[str] = None
        audit_path: Optional[str] = None
        patch_reports: List[Dict[str, Any]] = []
        try:
            if use_sandbox:
                sandbox_branch = self.git.create_sandbox(task.task_id)

            dev_plan: DevPlan = self.architect.plan(task)
            patch_set: CodePatchSet = self.programmer.code(dev_plan, task)

            if patch_set.files:
                patch_reports = [r.__dict__ for r in self.patcher.apply(patch_set, repo_root=self.repo_root, dry_run=dry_run)]

            test_plan: TestPlan = self.tester.build_test_plan(dev_plan, task)
            test_report: TestReport = self.tester.run_tests(test_plan)
            review: ReviewDecision = self.reviewer.review(patch_set, test_report)

            if use_sandbox and patch_set.files and not dry_run:
                self.git.commit_work(f"self-programming: {task.task_id}")
                if self.auto_merge and review.status == "approve" and self._should_auto_merge(patch_set):
                    merged = self.git.merge_to_branch(
                        sandbox_branch,
                        target_branch=self.auto_merge_target,
                    )
                    audit_path = self._record_audit(
                        {
                            "task_id": task.task_id,
                            "status": "auto_merged" if merged else "merge_failed",
                            "target_branch": self.auto_merge_target or self.git._base_branch,
                            "sandbox_branch": sandbox_branch,
                            "review_status": review.status,
                            "test_status": test_report.overall_status,
                        }
                    )
                else:
                    audit_path = self._record_audit(
                        {
                            "task_id": task.task_id,
                            "status": "pending_review",
                            "sandbox_branch": sandbox_branch,
                            "review_status": review.status,
                            "test_status": test_report.overall_status,
                        }
                    )
            return ProgrammingResult(
                task=task,
                dev_plan=dev_plan,
                patch_set=patch_set,
                test_plan=test_plan,
                test_report=test_report,
                review=review,
                tools_used=self._collect_tools(),
                patch_reports=patch_reports,
                audit_log_path=audit_path,
            )
        finally:
            if use_sandbox and sandbox_branch:
                self.git.cleanup_sandbox(sandbox_branch)


__all__ = ["SelfProgrammingEngine"]
