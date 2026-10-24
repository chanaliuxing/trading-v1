# strategies/runtime.py

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import json


@dataclass
class StrategyManifest:
    run_id: str
    symbol: str
    freq: str
    horizon: str
    label_id: str
    model_type: str
    model_path: str
    feature_subset: List[str]
    params: Dict[str, Any]
    train_window_start: Optional[str] = None
    train_window_end: Optional[str] = None
    metrics: Optional[Dict[str, Any]] = None

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "StrategyManifest":
        tw = d.get("train_window") or {}
        return cls(
            run_id=d["run_id"],
            symbol=d["symbol"],
            freq=d["freq"],
            horizon=d["horizon"],
            label_id=d["label_id"],
            model_type=d["model_type"],
            model_path=d["model_path"],
            feature_subset=list(d.get("feature_subset", [])),
            params=d.get("params", {}),
            train_window_start=tw.get("start_ts"),
            train_window_end=tw.get("end_ts"),
            metrics=d.get("metrics"),
        )


class StrategyManifestError(Exception):
    pass


def load_strategy_manifest(manifest_path: str | Path) -> StrategyManifest:
    p = Path(manifest_path)
    if not p.exists():
        raise StrategyManifestError(f"Manifest file not found: {p}")

    with p.open("r", encoding="utf-8") as f:
        try:
            raw = json.load(f)
        except json.JSONDecodeError as e:
            raise StrategyManifestError(f"Invalid JSON in manifest: {p}") from e

    # 基本字段检查
    required = [
        "run_id",
        "symbol",
        "freq",
        "horizon",
        "label_id",
        "model_type",
        "model_path",
    ]
    missing = [k for k in required if k not in raw]
    if missing:
        raise StrategyManifestError(f"Missing fields in manifest {p}: {missing}")

    return StrategyManifest.from_dict(raw)
