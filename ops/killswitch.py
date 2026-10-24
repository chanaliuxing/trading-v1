# ops/killswitch.py
from __future__ import annotations
import os, json, time
from pathlib import Path

DEFAULT_PATH = os.getenv("KILL_FILE", "/tmp/alpha_kill.flag")


def enable(path: str = DEFAULT_PATH, reason: str = "manual"):
    """
    Enable kill-switch to halt all trading.

    Args:
        path: Path to kill-switch flag file
        reason: Reason for activation (logged for audit)
    """
    Path(path).write_text(
        json.dumps({
            "enabled": True,
            "reason": reason,
            "ts": int(time.time())
        }),
        encoding="utf-8"
    )


def disable(path: str = DEFAULT_PATH):
    """Disable kill-switch to resume trading."""
    if Path(path).exists():
        Path(path).unlink()


def active(path: str = DEFAULT_PATH) -> tuple[bool, str]:
    """
    Check if kill-switch is active.

    Returns:
        (is_active, reason) tuple
    """
    p = Path(path)
    if not p.exists():
        return (False, "")

    try:
        j = json.loads(p.read_text("utf-8"))
        return (bool(j.get("enabled", False)), str(j.get("reason","")))
    except Exception:
        return (True, "corrupt_flag")
