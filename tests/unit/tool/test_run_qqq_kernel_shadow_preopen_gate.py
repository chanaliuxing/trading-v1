from __future__ import annotations

from pathlib import Path

from tool.replay.run_qqq_kernel_shadow_preopen_gate import run_preopen_gate


def test_run_preopen_gate_passes_without_tests(tmp_path: Path):
    output = tmp_path / "preopen.json"

    payload = run_preopen_gate(
        config_path=Path("./config.yml"),
        output=output,
        run_tests=False,
        allow_enabled=True,
    )

    assert payload["ok"] is True
    assert payload["recommendation"] == "GO_FOR_READINESS_ONLY"
    assert output.exists()
