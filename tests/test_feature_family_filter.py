from __future__ import annotations

import json
from pathlib import Path

from tools.feature_family_filter import filter_feature_columns


def test_feature_family_filter_respects_config(tmp_path: Path, monkeypatch):
    # Prepare minimal registry and families configs in a temp directory
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(parents=True)

    registry = {
        "features": {
            "open": {"tags": ["price", "ohlc"]},
            "volume": {"tags": ["volume"]},
            "day_of_week": {"tags": ["time"]},
            "vol_20b": {"tags": ["volatility"]},
        }
    }
    (cfg_dir / "feature_registry.json").write_text(json.dumps(registry), encoding="utf-8")

    families = {
        "families": {
            "price": {"tags": ["price", "ohlc"], "enabled": True},
            "volume": {"tags": ["volume"], "enabled": False},
            "time": {"tags": ["time"], "enabled": False},
        }
    }
    (cfg_dir / "feature_families.json").write_text(json.dumps(families), encoding="utf-8")

    # Point the filter module to our temp config dir by monkeypatching Path
    import tools.feature_family_filter as fff  # type: ignore

    def _cfg_path(name: str) -> Path:
        return cfg_dir / name

    monkeypatch.setattr(fff, "Path", lambda p="": _cfg_path(p.split("/")[-1]))

    cols = ["open", "volume", "day_of_week", "unknown"]
    filtered = filter_feature_columns(cols)
    # With only 'price' family enabled, we expect to keep 'open' and 'unknown'
    # (unknown not in registry → kept), and drop volume/time features.
    assert "open" in filtered
    assert "unknown" in filtered
    assert "volume" not in filtered
    assert "day_of_week" not in filtered

