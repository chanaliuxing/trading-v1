import json
from datetime import datetime, timedelta, timezone

import pytest

from tool.ops import broker_truth_convergence as ops


def test_broker_qty_from_snapshot_treats_missing_target_as_flat() -> None:
    snapshot = {
        "target_positions": {
            "TQQQ": {"symbol": "TQQQ", "qty": 0.0, "missing_from_broker_positions": True},
        },
        "all_nonzero_positions": [{"symbol": "SOXS", "qty": -8448.0}],
    }

    assert ops._broker_qty_from_snapshot(snapshot, symbol="TQQQ") == 0.0
    assert ops._broker_qty_from_snapshot(snapshot, symbol="SOXS") == -8448.0


@pytest.mark.asyncio
async def test_apply_snapshot_broker_flat_rejects_stale_snapshot(tmp_path) -> None:
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(
        json.dumps(
            {
                "generated_at": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),
                "target_positions": {"TQQQ": {"symbol": "TQQQ", "qty": 0.0}},
                "all_nonzero_positions": [],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit, match="snapshot too old"):
        await ops.apply_snapshot_broker_flat(
            symbol="TQQQ",
            snapshot_path=snapshot_path,
            max_age_sec=1,
            confirm=True,
        )


@pytest.mark.asyncio
async def test_apply_snapshot_broker_flat_rejects_nonflat_broker_snapshot(tmp_path) -> None:
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "target_positions": {"TQQQ": {"symbol": "TQQQ", "qty": 106.0}},
                "all_nonzero_positions": [{"symbol": "TQQQ", "qty": 106.0}],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit, match="broker is not flat"):
        await ops.apply_snapshot_broker_flat(
            symbol="TQQQ",
            snapshot_path=snapshot_path,
            max_age_sec=60,
            confirm=True,
        )


@pytest.mark.asyncio
async def test_apply_snapshot_broker_flat_reads_current_ledger_and_adjusts(monkeypatch, tmp_path) -> None:
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "target_positions": {"TQQQ": {"symbol": "TQQQ", "qty": 0.0}},
                "all_nonzero_positions": [],
            }
        ),
        encoding="utf-8",
    )
    calls = {}

    class FakePG:
        async def fetchone(self, _sql, symbol):
            calls["ledger_symbol"] = symbol
            return {"ledger_qty": 106.0}

        async def close(self):
            calls["closed"] = True

    class FakeService:
        def __init__(self, *, pg):
            calls["service_pg"] = pg

        async def converge_position_mismatch(self, **kwargs):
            calls["converge_kwargs"] = kwargs
            return type(
                "Result",
                (),
                {"ok": True, "reason": "position_mismatch_adjusted", "metadata": {"inserted": True}},
            )()

    async def fake_connect_pg():
        return FakePG()

    monkeypatch.setattr(ops, "_connect_pg", fake_connect_pg)
    monkeypatch.setattr(ops, "BrokerTruthConvergenceService", FakeService)

    result = await ops.apply_snapshot_broker_flat(
        symbol="tqqq",
        snapshot_path=snapshot_path,
        max_age_sec=60,
        confirm=True,
    )

    assert result["ok"] is True
    assert result["metadata"]["ledger_qty_read_from_db"] == 106.0
    assert calls["ledger_symbol"] == "TQQQ"
    assert calls["converge_kwargs"]["symbol"] == "tqqq"
    assert calls["converge_kwargs"]["ib_qty"] == 0.0
    assert calls["converge_kwargs"]["ledger_qty"] == 106.0
    assert calls["closed"] is True
