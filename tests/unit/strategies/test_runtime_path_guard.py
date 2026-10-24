from pathlib import Path

import pytest

from strategies.runtime_path_guard import claim_paths, release_paths


def test_claim_paths_rejects_cross_strategy_conflict(tmp_path: Path) -> None:
    shared = tmp_path / "shared.json"
    claim_paths(strategy_id="s1", paths=[shared])
    try:
        with pytest.raises(RuntimeError):
            claim_paths(strategy_id="s2", paths=[shared])
    finally:
        release_paths(strategy_id="s1", paths=[shared])


def test_claim_paths_allows_same_strategy_reclaim(tmp_path: Path) -> None:
    p = tmp_path / "state.json"
    claim_paths(strategy_id="s1", paths=[p])
    try:
        claim_paths(strategy_id="s1", paths=[p])
    finally:
        release_paths(strategy_id="s1", paths=[p])
