from __future__ import annotations

from dataclasses import dataclass
from typing import Dict


@dataclass(frozen=True)
class OptionEvidenceContract:
    contract_id: str
    replay_harness: str
    cutover_policy: str
    parity_profile_required: bool = True
    replay_strategy_required: bool = True
    shadow_toggle_required: bool = True
    state_shadow_required: bool = False
    parity_snapshot_required: bool = True

    def __post_init__(self) -> None:
        if not str(self.contract_id or "").strip():
            raise ValueError("evidence contract_id is required")
        if not str(self.replay_harness or "").strip():
            raise ValueError(f"evidence contract_id={self.contract_id} missing replay_harness")
        if not str(self.cutover_policy or "").strip():
            raise ValueError(f"evidence contract_id={self.contract_id} missing cutover_policy")

    def identity(self) -> Dict[str, str | bool]:
        return {
            "evidence_contract_id": str(self.contract_id or ""),
            "replay_harness": str(self.replay_harness or ""),
            "cutover_policy": str(self.cutover_policy or ""),
            "parity_profile_required": bool(self.parity_profile_required),
            "replay_strategy_required": bool(self.replay_strategy_required),
            "shadow_toggle_required": bool(self.shadow_toggle_required),
            "state_shadow_required": bool(self.state_shadow_required),
            "parity_snapshot_required": bool(self.parity_snapshot_required),
        }
