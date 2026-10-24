import json
import subprocess
from pathlib import Path


def test_build_option_strategy_inventory_cli_outputs_json(tmp_path: Path) -> None:
    output_path = tmp_path / "inventory.json"
    subprocess.run(
        [
            "./venv/bin/python",
            "./tool/report/build_option_strategy_inventory.py",
            "--config",
            "./config.yml",
            "--output",
            str(output_path),
        ],
        check=True,
    )

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert "summary" in payload
    assert "strategies" in payload
    assert "qqq_intraday_option_v1_001" in payload["active_strategy_ids"]
