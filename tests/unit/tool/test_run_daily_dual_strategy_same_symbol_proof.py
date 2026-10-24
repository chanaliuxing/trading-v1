from __future__ import annotations

from pathlib import Path

from tool.replay.run_daily_dual_strategy_same_symbol_proof import _default_output_dir, _default_sample_dir


def test_default_daily_same_symbol_dirs() -> None:
    day = "2026-03-24"
    sample_dir = _default_sample_dir(day)
    output_dir = _default_output_dir(day)

    assert str(sample_dir).endswith("runtime/state/index_super_win_shadow_samples/2026-03-24")
    assert str(output_dir).endswith("runtime/state/index_super_win_same_symbol_proof/2026-03-24")
    assert isinstance(sample_dir, Path)
    assert isinstance(output_dir, Path)
