from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping

STAGES: tuple[str, ...] = (
    "intake",
    "task_compile",
    "execute",
    "evaluate",
    "policy_compile",
    "completed",
    "failed",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


class CycleStateStore:
    def __init__(self, *, cycle_dir: str | Path, cycle_id: str):
        self.cycle_dir = Path(cycle_dir)
        self.cycle_id = str(cycle_id)
        self.cycle_state_path = self.cycle_dir / "cycle_state.json"
        self.stage_history_path = self.cycle_dir / "stage_history.jsonl"

    def load_or_init(self) -> Dict[str, Any]:
        if self.cycle_state_path.exists():
            try:
                payload = json.loads(self.cycle_state_path.read_text(encoding="utf-8"))
                if isinstance(payload, dict) and payload.get("cycle_id") == self.cycle_id:
                    payload.setdefault("stages", {})
                    return payload
            except Exception:
                pass
        payload = {
            "cycle_id": self.cycle_id,
            "status": "running",
            "current_stage": "intake",
            "failed_stage": None,
            "error_summary": None,
            "started_at": _now_iso(),
            "updated_at": _now_iso(),
            "completed_at": None,
            "stages": {},
        }
        self.write(payload)
        self.append_history(event="stage_enter", stage="intake", status="running", detail={"reason": "init_cycle_state"})
        return payload

    def write(self, payload: Mapping[str, Any]) -> None:
        self.cycle_dir.mkdir(parents=True, exist_ok=True)
        self.cycle_state_path.write_text(
            json.dumps(dict(payload), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def append_history(self, *, event: str, stage: str, status: str, detail: Mapping[str, Any] | None = None) -> None:
        self.cycle_dir.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": _now_iso(),
            "cycle_id": self.cycle_id,
            "event": str(event),
            "stage": str(stage),
            "status": str(status),
            "detail": _as_dict(detail),
        }
        with self.stage_history_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def stage_completed(self, payload: Mapping[str, Any], stage: str) -> bool:
        stages = _as_dict(_as_dict(payload).get("stages"))
        return str(_as_dict(stages.get(stage)).get("status") or "") == "completed"

    def enter_stage(self, payload: Mapping[str, Any], stage: str, *, detail: Mapping[str, Any] | None = None) -> Dict[str, Any]:
        state = _as_dict(payload)
        stages = _as_dict(state.get("stages"))
        stage_row = _as_dict(stages.get(stage))
        stage_row.update({"status": "running", "entered_at": _now_iso()})
        stages[stage] = stage_row
        state["stages"] = stages
        state["current_stage"] = stage
        state["status"] = "running"
        state["updated_at"] = _now_iso()
        self.write(state)
        self.append_history(event="stage_enter", stage=stage, status="running", detail=detail)
        return state

    def complete_stage(self, payload: Mapping[str, Any], stage: str, *, detail: Mapping[str, Any] | None = None) -> Dict[str, Any]:
        state = _as_dict(payload)
        stages = _as_dict(state.get("stages"))
        stage_row = _as_dict(stages.get(stage))
        stage_row.update({"status": "completed", "completed_at": _now_iso()})
        stages[stage] = stage_row
        state["stages"] = stages
        state["current_stage"] = stage
        state["updated_at"] = _now_iso()
        self.write(state)
        self.append_history(event="stage_exit", stage=stage, status="completed", detail=detail)
        return state

    def fail(self, payload: Mapping[str, Any], *, failed_stage: str, error_summary: str) -> Dict[str, Any]:
        state = _as_dict(payload)
        stages = _as_dict(state.get("stages"))
        stage_row = _as_dict(stages.get(failed_stage))
        stage_row.update({"status": "failed", "failed_at": _now_iso()})
        stages[failed_stage] = stage_row
        state["stages"] = stages
        state["status"] = "failed"
        state["current_stage"] = "failed"
        state["failed_stage"] = failed_stage
        state["error_summary"] = str(error_summary)
        state["updated_at"] = _now_iso()
        self.write(state)
        self.append_history(
            event="stage_exit",
            stage=failed_stage,
            status="failed",
            detail={"error_summary": str(error_summary)},
        )
        return state

    def complete_cycle(self, payload: Mapping[str, Any], *, detail: Mapping[str, Any] | None = None) -> Dict[str, Any]:
        state = _as_dict(payload)
        state["status"] = "completed"
        state["current_stage"] = "completed"
        state["failed_stage"] = None
        state["error_summary"] = None
        state["completed_at"] = _now_iso()
        state["updated_at"] = _now_iso()
        self.write(state)
        self.append_history(event="stage_exit", stage="completed", status="completed", detail=detail)
        return state

