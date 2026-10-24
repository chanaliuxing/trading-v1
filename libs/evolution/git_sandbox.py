from __future__ import annotations

import subprocess
from contextlib import contextmanager
from typing import Optional

from libs.logger import get_logger

log = get_logger(__name__)


class GitSandbox:
    def __init__(self) -> None:
        self.current_branch: Optional[str] = None
        self.should_auto_merge: bool = False  # 🆕 P1.2: Auto-merge flag

    def create_branch(self, task_id: str) -> str:
        branch = f"ai-evolve/{task_id}"
        subprocess.run(["git", "checkout", "-b", branch], check=False)
        self.current_branch = branch
        return branch

    def commit_and_test(self, message: str, test_cmd: str) -> bool:
        subprocess.run(["git", "add", "."], check=False)
        subprocess.run(["git", "commit", "-m", message], check=False)
        result = subprocess.run(test_cmd, shell=True)
        return result.returncode == 0

    def auto_merge(self, target_branch: str = "main") -> bool:
        """🆕 P1.2: Auto-merge current branch into target after tests pass."""
        if not self.current_branch:
            log.warning("auto_merge_skipped_no_branch")
            return False

        try:
            # Switch to target branch
            subprocess.run(["git", "checkout", target_branch], check=True)

            # Merge the evolution branch
            result = subprocess.run(
                ["git", "merge", "--no-ff", self.current_branch, "-m",
                 f"Auto-merge: {self.current_branch} (tests passed)"],
                check=False
            )

            if result.returncode == 0:
                log.info("auto_merge_success", branch=self.current_branch, target=target_branch)
                # Clean up merged branch
                subprocess.run(["git", "branch", "-d", self.current_branch], check=False)
                return True
            else:
                log.error("auto_merge_failed_conflict", branch=self.current_branch)
                # Rollback to evolution branch on conflict
                subprocess.run(["git", "checkout", self.current_branch], check=False)
                return False
        except Exception as e:
            log.error("auto_merge_exception", error=str(e))
            return False

    def rollback(self) -> None:
        # 🆕 P1.2: If auto-merge requested and successful, skip rollback
        if self.should_auto_merge:
            log.info("rollback_skipped_auto_merge_enabled")
            return

        # Safety: best effort return to main; ignore failures.
        subprocess.run(["git", "checkout", "main"], check=False)
        if self.current_branch:
            subprocess.run(["git", "branch", "-D", self.current_branch], check=False)


@contextmanager
def sandbox(task_id: str):
    gs = GitSandbox()
    try:
        gs.create_branch(task_id)
        yield gs
    finally:
        gs.rollback()
