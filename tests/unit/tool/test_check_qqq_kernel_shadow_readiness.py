from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "tool" / "replay" / "check_qqq_kernel_shadow_readiness.py"


def test_check_qqq_kernel_shadow_readiness_current_config_passes_with_enable_override(tmp_path: Path) -> None:
    output = tmp_path / "readiness.json"

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--output", str(output), "--allow-enabled"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["recommendation"] == "GO_FOR_READINESS_ONLY"
    assert payload["failed_checks"] == []


def test_check_qqq_kernel_shadow_readiness_blocks_live_shadow_without_override(tmp_path: Path):
    config = tmp_path / "config.yml"
    config.write_text(
        """
strategies:
- id: qqq_intraday_001
  enabled: true
- id: qqq_kernel_shadow_legacy_on_001
  enabled: true
  params:
    extras:
      dry_run: true
      shadow_only: true
      policy_id: qqq_intraday_policy_v1
      enable_shared_underlying_data_plane: true
      shared_underlying_read_mode: plane_only
      shadow_origin_strategy_id: qqq_intraday_001
      signal_symbol: QQQ
      long_exec_symbol: TQQQ
      short_exec_symbol: SQQQ
      policy_params:
        legacy_trader_parity_mode: true
- id: qqq_kernel_shadow_legacy_off_001
  enabled: false
  params:
    extras:
      dry_run: true
      shadow_only: true
      policy_id: qqq_intraday_policy_v1
      enable_shared_underlying_data_plane: true
      shared_underlying_read_mode: plane_only
      shadow_origin_strategy_id: qqq_intraday_001
      signal_symbol: QQQ
      long_exec_symbol: TQQQ
      short_exec_symbol: SQQQ
      policy_params:
        legacy_trader_parity_mode: false
""",
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--config", str(config)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert "qqq_kernel_shadow_legacy_on_001_disabled_until_go" in payload["failed_checks"]


def test_check_qqq_kernel_shadow_readiness_blocks_invalid_shared_plane_mode(tmp_path: Path):
    config = tmp_path / "config.yml"
    config.write_text(
        """
strategies:
- id: qqq_intraday_001
  enabled: true
- id: qqq_kernel_shadow_legacy_on_001
  enabled: false
  params:
    extras:
      dry_run: true
      shadow_only: true
      policy_id: qqq_intraday_policy_v1
      enable_shared_underlying_data_plane: true
      shared_underlying_read_mode: global_only
      shadow_origin_strategy_id: qqq_intraday_001
      signal_symbol: QQQ
      long_exec_symbol: TQQQ
      short_exec_symbol: SQQQ
      policy_params:
        legacy_trader_parity_mode: true
- id: qqq_kernel_shadow_legacy_off_001
  enabled: false
  params:
    extras:
      dry_run: true
      shadow_only: true
      policy_id: qqq_intraday_policy_v1
      enable_shared_underlying_data_plane: true
      shared_underlying_read_mode: global_only
      shadow_origin_strategy_id: qqq_intraday_001
      signal_symbol: QQQ
      long_exec_symbol: TQQQ
      short_exec_symbol: SQQQ
      policy_params:
        legacy_trader_parity_mode: false
""",
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--config", str(config)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert "qqq_kernel_shadow_legacy_on_001_shared_plane_mode_allowed" in payload["failed_checks"]
    assert "qqq_kernel_shadow_legacy_off_001_shared_plane_mode_allowed" in payload["failed_checks"]
