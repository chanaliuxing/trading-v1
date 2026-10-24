from __future__ import annotations

import importlib
import logging
import sys
from pathlib import Path


def test_boot_logging_falls_back_to_repo_log_when_audit_log_path_unwritable(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, str] = {}

    def _fake_configure_logging(*, json_file_path=None, console_human_readable=True, level="INFO"):
        captured["json_file_path"] = str(json_file_path)
        captured["level"] = str(level)

    class _DummyLogger:
        def info(self, *args, **kwargs):
            return None

    monkeypatch.setattr("libs.logger.configure_logging", _fake_configure_logging)
    monkeypatch.setattr("libs.logger.install_error_hooks", lambda *args, **kwargs: None)
    monkeypatch.setattr("libs.logger.get_logger", lambda *args, **kwargs: _DummyLogger())
    monkeypatch.setattr("logging.getLogger", lambda *args, **kwargs: logging.Logger("boot-test"))
    blocked_parent = tmp_path / "blocked-parent"
    blocked_parent.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("AUDIT_LOG_PATH", str(blocked_parent / "trader.log"))

    sys.modules.pop("boot_logging", None)
    module = importlib.import_module("boot_logging")

    expected = str(Path(module.__file__).resolve().parent / "logs" / "trader.log")
    assert captured["json_file_path"] == expected
    assert module.LOG_PATH == expected


def test_boot_logging_falls_back_on_permission_error_from_configure(monkeypatch, tmp_path: Path) -> None:
    calls: list[str] = []

    def _fake_configure_logging(*, json_file_path=None, console_human_readable=True, level="INFO"):
        path = str(json_file_path)
        calls.append(path)
        if len(calls) == 1:
            raise PermissionError("blocked by os policy")

    class _DummyLogger:
        def info(self, *args, **kwargs):
            return None

    monkeypatch.setattr("libs.logger.configure_logging", _fake_configure_logging)
    monkeypatch.setattr("libs.logger.install_error_hooks", lambda *args, **kwargs: None)
    monkeypatch.setattr("libs.logger.get_logger", lambda *args, **kwargs: _DummyLogger())
    monkeypatch.setattr("logging.getLogger", lambda *args, **kwargs: logging.Logger("boot-test"))

    preferred = tmp_path / "preferred" / "trader.log"
    monkeypatch.setenv("AUDIT_LOG_PATH", str(preferred))

    sys.modules.pop("boot_logging", None)
    module = importlib.import_module("boot_logging")

    expected = str(Path(module.__file__).resolve().parent / "logs" / "trader.log")
    assert calls == [str(preferred), expected]
    assert module.LOG_PATH == expected
