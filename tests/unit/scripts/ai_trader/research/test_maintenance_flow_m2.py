from __future__ import annotations

import json
from pathlib import Path

from scripts.ai_trader.research.freeze_change_gate import evaluate_freeze_change_gate
from scripts.ai_trader.research.release_delivery_index import build_release_delivery_index
from scripts.ai_trader.research.release_handoff import build_release_handoff_artifacts
from scripts.ai_trader.research.release_maintenance_verification import (
    MAINTENANCE_REQUIRED_SEQUENCE,
    run_maintenance_verification,
)
from scripts.ai_trader.research.release_signoff_summary import build_release_signoff_summary


def test_maintenance_flow_doc_consistency() -> None:
    text = Path("docs/README.md").read_text(encoding="utf-8")
    required_lines = [
        "test -f runs/ai_trader/research/state/release_prechange_checklist_template.json",
        "venv/bin/python -m scripts.ai_trader.research.freeze_change_gate",
        "venv/bin/python -m scripts.ai_trader.research.release_signoff_summary",
        "venv/bin/python -m scripts.ai_trader.research.release_delivery_index --index-id <index_id>",
        "venv/bin/python -m scripts.ai_trader.research.release_maintenance_verification --verification-id <verification_id>",
    ]
    for line in required_lines:
        assert line in text


def test_no_change_maintenance_sequence_reproducibility() -> None:
    build_release_handoff_artifacts(handoff_id="m2-seq-setup")
    assert Path("runs/ai_trader/research/state/release_prechange_checklist_template.json").exists()
    gate = evaluate_freeze_change_gate()
    build_release_signoff_summary()
    index_bundle = build_release_delivery_index(index_id="m2-seq-test")
    verify_bundle = run_maintenance_verification(verification_id="maintenance_baseline_confirmed-m2-test")

    assert gate["gate_status"] == "pass"
    assert bool(gate.get("execution_guard_status", {}).get("passed", False)) is True

    index_payload = index_bundle["release_delivery_index"]
    assert index_payload["status"] == "ok"

    latest = verify_bundle["entry"]
    assert latest["verification_id"].startswith("maintenance_baseline_confirmed")
    assert latest["freeze_gate_status"] == "pass"
    assert latest["delivery_index_status"] == "ok"
    assert latest["path_integrity"]["missing_paths"] == []

    persisted = json.loads(Path(verify_bundle["release_maintenance_verifications_json"]).read_text(encoding="utf-8"))
    assert persisted.get("latest", {}).get("verification_id") == latest["verification_id"]


def test_maintenance_mode_state_record_generation() -> None:
    build_release_handoff_artifacts(handoff_id="m2-mode-setup")
    out = run_maintenance_verification(verification_id="maintenance_baseline_confirmed-m2-state")
    mode_path = Path(out["release_maintenance_mode_state_json"])
    assert mode_path.exists()
    payload = json.loads(mode_path.read_text(encoding="utf-8"))
    assert payload["maintenance_mode"] is True
    assert payload["baseline_confirmed"] is True
    assert payload["required_sequence"] == MAINTENANCE_REQUIRED_SEQUENCE
    assert payload["last_verification_id"] == "maintenance_baseline_confirmed-m2-state"


def test_required_sequence_consistency_and_no_new_rule_source() -> None:
    text = Path("docs/README.md").read_text(encoding="utf-8")
    for command in MAINTENANCE_REQUIRED_SEQUENCE:
        assert command in text
    run_maintenance_verification(verification_id="maintenance_baseline_confirmed-m2-rules")
    mode_state = json.loads(Path("runs/ai_trader/research/state/release_maintenance_mode_state.json").read_text(encoding="utf-8"))
    assert set(mode_state.keys()) == {
        "maintenance_mode",
        "baseline_confirmed",
        "required_sequence",
        "last_verification_id",
        "last_verified_at",
        "notes",
    }
