#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategies.index_super_win_manifest import ReplicaManifest


def _load_yaml(path: Path) -> Dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def build_replica_flat_broker_snapshot(*, manifest_path: Path, output_path: Path) -> Dict[str, float]:
    manifest = ReplicaManifest.from_params(_load_yaml(manifest_path))
    payload: Dict[str, float] = {}
    for item in manifest.symbols:
        if not item.enabled:
            continue
        for symbol in {
            str(item.signal_symbol or "").strip().upper(),
            str(item.long_exec_symbol or item.signal_symbol or "").strip().upper(),
            str(item.short_exec_symbol or item.long_exec_symbol or item.signal_symbol or "").strip().upper(),
        }:
            if symbol:
                payload[symbol] = 0.0
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a flat broker positions snapshot for replica preflight when strategy is disabled.")
    parser.add_argument("--manifest", default=str(ROOT / "configs" / "index_super_win" / "qqq_replica_manifest.example.yml"))
    parser.add_argument("--output-path", default=str(ROOT / "runtime" / "state" / "index_super_win_replica_broker_positions_flat.json"))
    args = parser.parse_args()

    payload = build_replica_flat_broker_snapshot(
        manifest_path=Path(args.manifest),
        output_path=Path(args.output_path),
    )
    print(json.dumps({"output_path": str(Path(args.output_path)), "symbols": sorted(payload)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
