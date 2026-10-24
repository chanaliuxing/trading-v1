from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List, Dict, Any

from meta_agent.self_programming.schema import TestPlan, TestReport, DevPlan, ProgrammingTask


class TesterAgent:
    """
    Generates test plans and executes tests using pytest.
    """

    def __init__(self, repo_root: str = "."):
        self.repo_root = str(Path(repo_root).resolve())

    def build_test_plan(self, plan: DevPlan, task: ProgrammingTask | None = None) -> TestPlan:
        """
        Build a test plan from development plan.

        For now, runs all tests. Future: could be more selective based on work_items.
        """
        if task and not task.constraints.get("requires_tests", True):
            return TestPlan(
                task_id=plan.task_id,
                framework="none",
                tests=[],
            )

        if isinstance(plan.test_plan, dict) and plan.test_plan.get("tests"):
            return TestPlan(
                task_id=plan.task_id,
                framework=plan.test_plan.get("framework", "pytest"),
                tests=plan.test_plan["tests"],
            )

        return TestPlan(
            task_id=plan.task_id,
            framework="pytest",
            tests=[
                {
                    "id": "full_suite",
                    "file": "tests/",
                    "description": "Run full test suite",
                    "command": ["pytest", "-q"],
                }
            ],
        )

    def run_tests(self, test_plan: TestPlan) -> TestReport:
        """
        Execute tests according to test plan.

        Returns:
            TestReport with pass/fail status and output
        """
        if test_plan.framework in ("none", None):
            return TestReport(
                task_id=test_plan.task_id,
                overall_status="skipped",
                results=[],
                raw_output="tests skipped by configuration",
            )

        if test_plan.framework != "pytest":
            return TestReport(
                task_id=test_plan.task_id,
                overall_status="skipped",
                results=[],
                raw_output=f"Unsupported test framework: {test_plan.framework}",
            )

        results: List[Dict[str, Any]] = []
        all_passed = True
        any_executed = False
        combined_output = []
        last_return_code: int | None = None

        for test in test_plan.tests:
            test_id = test.get("id", "unknown")
            command = test.get("command", ["pytest", "-q"])
            any_executed = True

            try:
                proc = subprocess.run(
                    command,
                    cwd=self.repo_root,
                    text=True,
                    capture_output=True,
                    timeout=60,  # 60 second timeout
                )

                last_return_code = proc.returncode
                passed = proc.returncode == 0
                all_passed = all_passed and passed

                results.append({
                    "id": test_id,
                    "status": "passed" if passed else "failed",
                    "returncode": proc.returncode,
                    "stdout": proc.stdout,
                    "stderr": proc.stderr,
                })

                combined_output.append(f"=== Test: {test_id} ===")
                combined_output.append(proc.stdout)
                if proc.stderr:
                    combined_output.append("STDERR:")
                    combined_output.append(proc.stderr)

            except subprocess.TimeoutExpired:
                all_passed = False
                results.append({
                    "id": test_id,
                    "status": "timeout",
                    "error": "Test execution timeout (60s)",
                })
                combined_output.append(f"=== Test: {test_id} ===")
                combined_output.append("ERROR: Timeout after 60 seconds")

            except Exception as e:
                all_passed = False
                results.append({
                    "id": test_id,
                    "status": "error",
                    "error": str(e),
                })
                combined_output.append(f"=== Test: {test_id} ===")
                combined_output.append(f"ERROR: {str(e)}")

        if not any_executed:
            overall_status = "skipped"
        else:
            failure_states = {r.get("status") for r in results} - {"passed"}
            if not failure_states:
                overall_status = "passed"
            elif failure_states and any(r.get("status") == "passed" for r in results):
                overall_status = "partial"
            elif "timeout" in failure_states:
                overall_status = "timeout"
            elif "error" in failure_states:
                overall_status = "error"
            else:
                overall_status = "failed"

        return TestReport(
            task_id=test_plan.task_id,
            overall_status=overall_status,
            results=results,
            raw_output="\n".join(combined_output),
            return_code=last_return_code,
        )


__all__ = ["TesterAgent"]
