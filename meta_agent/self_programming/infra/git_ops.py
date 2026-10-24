from __future__ import annotations

import subprocess
import uuid
from pathlib import Path
from typing import Optional


class GitSafetyLayer:
    """
    Minimal git sandbox helper.

    Usage:
        git = GitSafetyLayer(repo_path=".")
        branch = git.create_sandbox(task_id)
        ... apply patches ...
        git.commit_work("self-programming: task_id")
        git.cleanup_sandbox(branch)

    This is intentionally light; callers should handle exceptions and avoid
    destructive operations outside the sandbox.
    """

    def __init__(self, repo_path: str = "."):
        self.repo_path = str(Path(repo_path).resolve())
        self._base_branch = self._get_current_branch()

    def _run(self, *args: str) -> None:
        subprocess.check_call(list(args), cwd=self.repo_path)

    def _get_current_branch(self) -> str:
        out = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=self.repo_path,
        )
        return out.decode().strip()

    def create_sandbox(self, task_id: str) -> str:
        branch_name = f"ai-dev/{task_id[:8]}-{uuid.uuid4().hex[:6]}"
        self._run("git", "checkout", "-b", branch_name)
        return branch_name

    def commit_work(self, message: str) -> None:
        self._run("git", "add", ".")
        try:
            self._run("git", "commit", "-m", message)
        except subprocess.CalledProcessError:
            # allow empty commit
            pass

    def cleanup_sandbox(self, sandbox_branch: str) -> None:
        try:
            self._run("git", "checkout", self._base_branch)
            self._run("git", "branch", "-D", sandbox_branch)
        except subprocess.CalledProcessError:
            # ignore failures to delete
            pass

    def merge_to_branch(self, source_branch: str, target_branch: Optional[str] = None) -> bool:
        """
        Attempt to merge the sandbox branch into target_branch (default: base branch).
        Returns True on success, False otherwise.
        """
        target = target_branch or self._base_branch
        try:
            self._run("git", "checkout", target)
            self._run("git", "merge", "--no-ff", "--no-edit", source_branch)
            self._run("git", "checkout", self._base_branch)
            return True
        except subprocess.CalledProcessError:
            # Leave working tree on base branch to avoid being stuck mid-merge
            try:
                self._run("git", "merge", "--abort")
            except subprocess.CalledProcessError:
                pass
            try:
                self._run("git", "checkout", self._base_branch)
            except subprocess.CalledProcessError:
                pass
            return False
