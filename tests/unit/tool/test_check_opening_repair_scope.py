from __future__ import annotations

from types import SimpleNamespace

import tool.ops.check_opening_repair_scope as scope


def test_scope_checker_blocks_strategy_and_service_changes(monkeypatch) -> None:
    monkeypatch.setattr(
        scope,
        "_git_changed_files",
        lambda repo_root: [
            "tool/ops/run_opening_supervisor.py",
            "strategies/qqq_intraday_strategy.py",
            "services/health/controller.py",
        ],
    )

    payload = scope.check_opening_repair_scope(repo_root=".")

    assert payload["ok"] is False
    assert "tool/ops/run_opening_supervisor.py" in payload["allowed_files"]
    assert "strategies/qqq_intraday_strategy.py" in payload["forbidden_files"]
    assert "services/health/controller.py" in payload["forbidden_files"]


def test_scope_checker_allows_generated_reports_when_explicitly_permitted(monkeypatch) -> None:
    monkeypatch.setattr(
        scope,
        "_git_changed_files",
        lambda repo_root: [
            "tool/ops/build_opening_health_report.py",
            "runtime/state/opening_day/20260422_open_supervisor.json",
            "reports/qqq_kernel_shadow_parity/20260422_trace_coverage.json",
        ],
    )

    payload = scope.check_opening_repair_scope(
        repo_root=".",
        extra_allowed_prefixes=["runtime/state/opening_day", "reports/qqq_kernel_shadow_parity"],
    )

    assert payload["ok"] is True
    assert "tool/ops/build_opening_health_report.py" in payload["allowed_files"]
    assert "runtime/state/opening_day/20260422_open_supervisor.json" in payload["allowed_files"]
    assert "reports/qqq_kernel_shadow_parity/20260422_trace_coverage.json" in payload["allowed_files"]


def test_git_changed_files_preserves_leading_path_character(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        scope.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout=" M scripts/run_system_autostart.sh\n?? ops/launchd/com.trading.run_system.0925.plist\n",
        ),
    )

    changed = scope._git_changed_files(tmp_path)

    assert changed == [
        "ops/launchd/com.trading.run_system.0925.plist",
        "scripts/run_system_autostart.sh",
    ]
