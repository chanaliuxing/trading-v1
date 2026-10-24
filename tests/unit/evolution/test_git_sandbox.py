import subprocess

from libs.evolution.git_sandbox import GitSandbox


def test_git_sandbox_tracks_branch(monkeypatch):
    calls = []

    def fake_run(cmd, check=False, shell=False):
        calls.append((tuple(cmd) if not shell else cmd, check, shell))
        # simulate success
        proc = subprocess.CompletedProcess(cmd, 0)
        return proc

    monkeypatch.setattr(subprocess, "run", fake_run)

    gs = GitSandbox()
    branch = gs.create_branch("test123")
    assert gs.current_branch == branch

    ok = gs.commit_and_test("msg", "echo ok")
    assert ok is True  # fake_run returns returncode 0

    gs.rollback()
    # ensure checkout and branch delete attempted
    assert any("checkout" in " ".join(c[0]) if isinstance(c[0], (list, tuple)) else False for c in calls)
