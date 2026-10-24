from __future__ import annotations

from pathlib import Path

from tool.build_qqq_replica_baseline import build_baseline, render_markdown


def test_build_baseline_contains_expected_contracts() -> None:
    root = Path(".")
    baseline = build_baseline(
        root=root,
        config_path=root / "config.yml",
        strategy_id="qqq_intraday_001",
    )

    assert baseline["strategy_id"] == "qqq_intraday_001"
    assert baseline["strategy"]["module"] == "strategies.qqq_intraday_strategy"
    assert baseline["strategy"]["class"] == "QQQIntradayStrategy"
    assert baseline["execution_contract"]["entry_isolation"]["enabled"] is True
    assert baseline["execution_contract"]["entry_isolation"]["symbols"] == []
    assert "strategy_file" in baseline["file_hashes"]
    assert "gate_allow_or_block" in baseline["parity_fields"]


def test_render_markdown_mentions_frozen_contracts() -> None:
    root = Path(".")
    baseline = build_baseline(
        root=root,
        config_path=root / "config.yml",
        strategy_id="qqq_intraday_001",
    )

    rendered = render_markdown(baseline)
    assert "QQQ Intraday Replica Baseline" in rendered
    assert "Frozen Files" in rendered
    assert "Shared Execution Contract" in rendered
    assert "Minimum Replay Fixtures" in rendered
