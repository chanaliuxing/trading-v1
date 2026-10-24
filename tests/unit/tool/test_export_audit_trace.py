from __future__ import annotations

import json
from pathlib import Path

from tool.replay.export_audit_trace import build_trace_payload


def test_build_trace_payload_exports_filtered_rows(tmp_path: Path) -> None:
    day_dir = tmp_path / "2026-03-16"
    day_dir.mkdir(parents=True)
    path = day_dir / "exec-10.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps({"ts": "2026-03-16T10:00:00-04:00", "domain": "exec", "stage": "entry_intent_requested", "trace_id": "t1", "symbol": "AAPL"}),
                json.dumps({"ts": "2026-03-16T10:00:01-04:00", "domain": "exec", "stage": "entry_intent_submitted", "trace_id": "t1", "symbol": "AAPL"}),
                json.dumps({"ts": "2026-03-16T10:00:02-04:00", "domain": "exec", "stage": "entry_intent_submitted", "trace_id": "t2", "symbol": "MSFT"}),
            ])
        + "\n",
        encoding="utf-8",
    )

    payload = build_trace_payload(
        root=tmp_path,
        date="2026-03-16",
        domain="exec",
        hour="10",
        trace_id="t1",
        symbol="AAPL",
        stage=None,
    )

    assert payload["meta"]["row_count"] == 2
    assert [row["stage"] for row in payload["events"]] == ["entry_intent_requested", "entry_intent_submitted"]
