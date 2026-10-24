from __future__ import annotations

import json
from datetime import datetime

from services.production.fill_attribution_service import (
    adjudicate_fill_attribution,
    query_fill_attribution_readiness,
    write_fill_attribution_artifact,
)


class _FakePg:
    def __init__(self, rows):
        self.rows = list(rows)
        self.calls = []

    async def fetchall(self, sql, *args):
        self.calls.append((str(sql), args))
        return list(self.rows)

    async def fetchone(self, sql, *args):
        self.calls.append((str(sql), args))
        return None


class _FakeLedgerPg(_FakePg):
    async def fetchval(self, sql, *args):
        self.calls.append((str(sql), args))
        return True

    async def fetchall(self, sql, *args):
        self.calls.append((str(sql), args))
        if "GROUP BY attribution_status" in str(sql):
            return [
                {
                    "status": "ambiguous",
                    "count": 1,
                    "pnl_eligible_count": 0,
                    "ledger_eligible_count": 0,
                    "manual_required_count": 1,
                },
                {
                    "status": "verified",
                    "count": 2,
                    "pnl_eligible_count": 2,
                    "ledger_eligible_count": 2,
                    "manual_required_count": 0,
                },
            ]
        if "core.fill_attribution_ledger" in str(sql):
            return list(self.rows)
        return []


class _FakeAdjudicationPg(_FakePg):
    async def execute(self, sql, *args):
        self.calls.append((str(sql), args))
        return "1"

    async def fetchone(self, sql, *args):
        self.calls.append((str(sql), args))
        if "UPDATE core.fill_attribution_ledger" in str(sql):
            return {
                "event_key": args[0],
                "symbol": "TQQQ",
                "attribution_status": args[1],
                "attribution_reason": "manual_adjudicated_verified",
                "matched_core_order_id": args[2],
                "matched_strategy_id": args[3],
                "pnl_eligible": args[4],
                "ledger_eligible": args[5],
                "manual_adjudication_required": args[6],
                "adjudicated_by": args[7],
                "adjudication_note": args[8],
            }
        return {"exists": True}


async def test_fill_attribution_blocks_active_unknown_fill():
    pg = _FakePg([{"symbol": "TQQQ", "reason": "unattributed_broker_fill", "count": 2}])

    status = await query_fill_attribution_readiness(
        pg=pg,
        generated_at=datetime.fromisoformat("2026-05-05T10:00:00-04:00"),
        strategy_ids=["qqq_intraday_001"],
        symbols=["TQQQ"],
    )

    assert status["go_no_go"] == "no_go"
    assert status["fill_truth_clean"] is False
    assert status["unknown_fill_count"] == 2
    assert status["blockers"] == ["unknown_or_ambiguous_broker_fills_active"]
    assert "strategy_id" not in pg.calls[0][0]


async def test_fill_attribution_prefers_first_class_ledger_when_present():
    pg = _FakeLedgerPg([{"symbol": "SOXL", "reason": "ambiguous", "count": 1, "candidate_count": 3}])

    status = await query_fill_attribution_readiness(
        pg=pg,
        generated_at=datetime.fromisoformat("2026-05-05T10:00:00-04:00"),
        strategy_ids=[],
        symbols=["SOXL"],
    )

    assert status["go_no_go"] == "no_go"
    assert status["source_table"] == "core.fill_attribution_ledger"
    assert status["ledger_present"] is True
    assert status["ledger_unknown_fill_count"] == 1
    assert status["legacy_unknown_fill_count"] == 0
    assert status["ambiguous_fill_count"] == 1
    assert status["daily_stats"]["total_count"] == 3
    assert status["daily_stats"]["verified_count"] == 2
    assert status["daily_stats"]["manual_required_count"] == 1
    assert status["daily_stats"]["pnl_eligible_count"] == 2
    assert status["clear_conditions"] == ["adjudicate_or_resolve_fill_attribution_ledger"]


async def test_fill_attribution_go_when_no_unknown_fill():
    status = await query_fill_attribution_readiness(
        pg=_FakePg([]),
        generated_at=datetime.fromisoformat("2026-05-05T10:00:00-04:00"),
        strategy_ids=[],
        symbols=[],
    )

    assert status["go_no_go"] == "go"
    assert status["fill_truth_clean"] is True
    assert status["unknown_fill_count"] == 0


def test_fill_attribution_artifact_write(tmp_path):
    status = {
        "artifact_type": "fill_attribution",
        "go_no_go": "go",
        "unknown_fill_count": 0,
    }

    path = write_fill_attribution_artifact(repo_root=tmp_path, status=status)
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert path.name == "fill_attribution_latest.json"
    assert payload["artifact_type"] == "fill_attribution"


async def test_adjudicate_fill_attribution_manual_resolved_requires_order_and_strategy():
    pg = _FakeAdjudicationPg([])

    result = await adjudicate_fill_attribution(
        pg=pg,
        event_key="DU123:TQQQ:exec-1:SELL:1.00000000:50.00000000",
        decision="manual_resolved",
        adjudicated_by="ops",
        matched_core_order_id="00000000-0000-0000-0000-000000000001",
        matched_strategy_id="qqq_intraday_001",
        note="verified in IB activity",
    )

    update_call = next((sql, args) for sql, args in pg.calls if "UPDATE core.fill_attribution_ledger" in sql)
    assert result["attribution_status"] == "manual_resolved"
    assert result["pnl_eligible"] is True
    assert result["ledger_eligible"] is True
    assert result["manual_adjudication_required"] is False
    assert update_call[1][7] == "ops"


async def test_adjudicate_fill_attribution_rejects_manual_resolved_without_match():
    pg = _FakeAdjudicationPg([])

    try:
        await adjudicate_fill_attribution(
            pg=pg,
            event_key="event-1",
            decision="manual_resolved",
            adjudicated_by="ops",
        )
    except ValueError as exc:
        assert "matched_core_order_id" in str(exc)
    else:
        raise AssertionError("expected ValueError")
