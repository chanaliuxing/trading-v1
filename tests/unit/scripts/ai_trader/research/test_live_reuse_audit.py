from __future__ import annotations

import json
from pathlib import Path

from scripts.ai_trader.research.live_reuse_audit import generate_live_reuse_artifacts


def test_live_reuse_audit_outputs(tmp_path: Path) -> None:
    artifacts = generate_live_reuse_artifacts(
        cycle_id="2026-04-05",
        output_dir=tmp_path,
    )
    for key in [
        "live_capability_inventory_json",
        "live_reuse_plan_json",
        "live_integration_report_json",
        "live_gap_list_json",
    ]:
        assert Path(artifacts[key]).exists()

    inventory = json.loads(Path(artifacts["live_capability_inventory_json"]).read_text(encoding="utf-8"))
    direct_or_adapter = [
        x
        for x in inventory.get("items", [])
        if x.get("reuse_mode") in {"direct", "adapter"} and bool(x.get("exists"))
    ]
    assert len(direct_or_adapter) >= 5

    gaps = json.loads(Path(artifacts["live_gap_list_json"]).read_text(encoding="utf-8"))
    names = {x.get("capability") for x in gaps.get("items", [])}
    assert "real_broker_sdk_profile" in names

