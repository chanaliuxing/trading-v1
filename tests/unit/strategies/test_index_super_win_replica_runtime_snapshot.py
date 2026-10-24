from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from libs.strategy import StrategyParams
from strategies.index_super_win_replica_strategy import IndexSuperWinReplicaStrategy


@pytest.fixture
def replica_ctx_snapshot():
    ctx = MagicMock()
    ctx.log = MagicMock()
    ctx.metrics = MagicMock()
    ctx.strategy_id = "index_super_win_replica_001"
    ctx.subscribe_features = AsyncMock()
    ctx.set_cadence = AsyncMock()
    ctx.submit_intent = AsyncMock(return_value={"status": "APPROVED", "reasons": []})
    return ctx


@pytest.mark.asyncio
async def test_replica_strategy_persists_runtime_snapshot_on_initialize(replica_ctx_snapshot, tmp_path: Path):
    runtime_snapshot_path = tmp_path / "runtime_snapshot.json"
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx_snapshot,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "runtime_snapshot_path": str(runtime_snapshot_path),
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()

    assert runtime_snapshot_path.exists()
    payload = json.loads(runtime_snapshot_path.read_text(encoding="utf-8"))
    assert payload["strategy_id"] == "index_super_win_replica_001"
    assert "QQQ" in payload["replica_runtime"]
    assert strategy.state["runtime_snapshot_path"] == str(runtime_snapshot_path)
