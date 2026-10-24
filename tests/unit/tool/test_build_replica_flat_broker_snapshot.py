from __future__ import annotations

import json
from pathlib import Path

import yaml

from tool.build_replica_flat_broker_snapshot import build_replica_flat_broker_snapshot


def test_build_replica_flat_broker_snapshot_writes_enabled_symbols(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.yml"
    output_path = tmp_path / "broker.json"
    manifest_path.write_text(
        yaml.safe_dump(
            {
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                        "enabled": True,
                    },
                    {
                        "key": "msft",
                        "signal_symbol": "MSFT",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "MSFT",
                        "short_exec_symbol": "SH",
                        "enabled": False,
                    },
                ]
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    payload = build_replica_flat_broker_snapshot(manifest_path=manifest_path, output_path=output_path)

    assert payload == {"QQQ": 0.0, "SQQQ": 0.0, "TQQQ": 0.0}
    assert json.loads(output_path.read_text(encoding="utf-8")) == payload
