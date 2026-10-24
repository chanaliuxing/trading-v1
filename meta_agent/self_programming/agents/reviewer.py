from __future__ import annotations

import subprocess
from typing import List, Dict, Any, Optional

from meta_agent.self_programming.schema import ReviewDecision, TestReport, CodePatchSet
from meta_agent.self_programming.tools.local_llm import LocalLLMTool


class ReviewerAgent:
    """
    Reviews patches and test results to approve/reject changes.
    """

    def __init__(
        self,
        repo_root: str = ".",
        static_checker: str = "ruff",
        use_llm: bool = False,
        llm_tool: Optional[LocalLLMTool] = None,
    ) -> None:
        self.repo_root = repo_root
        self.static_checker = static_checker
        self.use_llm = use_llm
        self.llm_tool = llm_tool

    def _run_static_checker(self) -> Dict[str, Any]:
        cmd_map = {
            "ruff": ["ruff", "check"],
            "flake8": ["flake8"],
            "mypy": ["mypy", "."],
        }
        cmd = cmd_map.get(self.static_checker)
        if not cmd:
            return {"status": "skipped", "reason": "unsupported_checker"}

        try:
            proc = subprocess.run(
                cmd,
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )
        except FileNotFoundError:
            return {"status": "skipped", "reason": "not_installed"}
        except subprocess.TimeoutExpired:
            return {"status": "failed", "reason": "timeout", "command": cmd}

        if proc.returncode == 0:
            return {"status": "passed", "command": cmd}

        return {
            "status": "failed",
            "command": cmd,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "returncode": proc.returncode,
        }

    def _llm_review(self, patch_set: CodePatchSet, test_report: TestReport) -> Optional[str]:
        if not (self.use_llm and self.llm_tool):
            return None

        patch_summary = [f"{m.file_path} ({m.action})" for m in patch_set.files]
        tests_status = f"overall={test_report.overall_status}, details={[r.get('status') for r in test_report.results]}"
        prompt = f"""You are a reviewer. Evaluate the safety of these patches.
Patches: {patch_summary}
Tests: {tests_status}
Provide 2-3 bullet risks or state 'no blockers'. Keep it concise."""

        try:
            res = self.llm_tool.run(prompt, operation="review")
        except Exception:
            return None

        if not res.get("ok"):
            return None
        return str(res.get("raw", "")).strip()

    def review(self, patch_set: CodePatchSet, test_report: TestReport) -> ReviewDecision:
        """
        Review code patches and test results.
        """
        issues: List[Dict[str, Any]] = []
        follow_up_actions: List[str] = []

        # Check 1: Test outcomes
        if test_report.overall_status in ("failed", "timeout", "error", "partial"):
            for result in test_report.results:
                if result.get("status") != "passed":
                    issues.append({
                        "type": "test_failure",
                        "test_id": result.get("id", "unknown"),
                        "description": f"Test status={result.get('status')}: {result.get('error', '')}",
                        "severity": "high" if result.get("status") == "failed" else "medium",
                    })
            follow_up_actions.append("Fix failing/unstable tests or revert changes")
            return ReviewDecision(
                task_id=patch_set.task_id,
                status="reject" if test_report.overall_status == "failed" else "request_changes",
                severity="high",
                issues=issues,
                summary=f"Tests not clean ({test_report.overall_status}). Changes blocked.",
                follow_up_actions=follow_up_actions,
            )

        # Check 2: No modifications generated
        if not patch_set.files:
            issues.append({
                "type": "no_changes",
                "description": "No code modifications were generated",
                "severity": "medium",
            })

            follow_up_actions.append("Review task requirements and retry")

            return ReviewDecision(
                task_id=patch_set.task_id,
                status="request_changes",
                severity="medium",
                issues=issues,
                summary="No modifications generated. Request changes.",
                follow_up_actions=follow_up_actions,
            )

        # Check 3: Static analysis
        static_report = self._run_static_checker()
        if static_report.get("status") == "failed":
            issues.append({
                "type": "static_check",
                "description": f"{self.static_checker} failed",
                "severity": "medium",
                "stdout": static_report.get("stdout", ""),
                "stderr": static_report.get("stderr", ""),
            })
            follow_up_actions.append(f"Fix {self.static_checker} findings")
            return ReviewDecision(
                task_id=patch_set.task_id,
                status="request_changes",
                severity="medium",
                issues=issues,
                summary=f"{self.static_checker} reported issues.",
                follow_up_actions=follow_up_actions,
                static_report=static_report,
            )

        # LLM review for additional risks (non-blocking)
        llm_notes = self._llm_review(patch_set, test_report)

        return ReviewDecision(
            task_id=patch_set.task_id,
            status="approve",
            severity="low",
            issues=issues,
            summary=f"Tests passed and {self.static_checker if static_report.get('status') == 'passed' else 'static check skipped'} clean. Changes approved.",
            follow_up_actions=follow_up_actions,
            static_report=static_report,
            llm_notes=llm_notes,
        )


__all__ = ["ReviewerAgent"]
