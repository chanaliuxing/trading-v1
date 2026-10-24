from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from tool.replay.switch_qqq_kernel_shadow import switch_shadow_enabled


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "tool" / "replay" / "switch_qqq_kernel_shadow.py"


def test_switch_shadow_enabled_updates_only_shadow_blocks():
    text = """strategies:
- id: qqq_intraday_001
  enabled: true
- id: qqq_kernel_shadow_legacy_on_001
  enabled: false
  params: {}
- id: qqq_kernel_shadow_legacy_off_001
  enabled: false
  params: {}
"""

    updated, changed = switch_shadow_enabled(text, enabled=True)

    assert changed == {
        "qqq_kernel_shadow_legacy_on_001": True,
        "qqq_kernel_shadow_legacy_off_001": True,
    }
    assert "- id: qqq_intraday_001\n  enabled: true" in updated
    assert "- id: qqq_kernel_shadow_legacy_on_001\n  enabled: true" in updated
    assert "- id: qqq_kernel_shadow_legacy_off_001\n  enabled: true" in updated


def test_switch_shadow_cli_enable_then_disable(tmp_path: Path):
    config = tmp_path / "config.yml"
    config.write_text(
        """strategies:
- id: qqq_intraday_001
  enabled: true
- id: qqq_kernel_shadow_legacy_on_001
  enabled: false
  params: {}
- id: qqq_kernel_shadow_legacy_off_001
  enabled: false
  params: {}
""",
        encoding="utf-8",
    )

    enable = subprocess.run(
        [sys.executable, str(SCRIPT), "enable", "--config", str(config)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert enable.returncode == 0
    assert "qqq_kernel_shadow_legacy_on_001.enabled=true" in enable.stdout

    disable = subprocess.run(
        [sys.executable, str(SCRIPT), "disable", "--config", str(config)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert disable.returncode == 0
    assert "qqq_kernel_shadow_legacy_off_001.enabled=false" in disable.stdout
