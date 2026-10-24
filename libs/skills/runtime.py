from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from libs.skills.registry import SkillRegistry, SkillRecord


def append_skill_flow_event(event: Dict[str, Any], runs_dir: Path | str = "runs/ai_trader") -> None:
    """Append a lifecycle event to the daily skill_flow.jsonl audit log."""
    ts = event.get("ts") or datetime.now(timezone.utc).isoformat()
    payload = {**event, "ts": ts}
    base = Path(runs_dir)
    day_dir = base / datetime.now(timezone.utc).date().isoformat()
    day_dir.mkdir(parents=True, exist_ok=True)
    flow = day_dir / "skill_flow.jsonl"
    with flow.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


class SkillRuntime:
    """Lightweight runtime for dynamically loading and invoking enabled skills."""

    def __init__(self, registry_path: str | Path = "config/skill_registry.json", runs_dir: str | Path = "runs/ai_trader"):
        self.registry = SkillRegistry(registry_path)
        self.runs_dir = Path(runs_dir)
        self._loaded: Dict[str, Tuple[SkillRecord, Any]] = {}
        self.refresh()

    def refresh(self) -> None:
        """Reload enabled skills from disk."""
        enabled = self.registry.list_enabled()
        loaded: Dict[str, Tuple[SkillRecord, Any]] = {}
        for skill_id, record in enabled.items():
            module = self._load_module(record)
            if module:
                loaded[skill_id] = (record, module)
        self._loaded = loaded

    def _load_module(self, record: SkillRecord):
        path = Path(record.file_path)
        if not path.exists():
            append_skill_flow_event(
                {"skill_id": record.skill_id, "event": "load_failed", "reason": "file_missing", "file_path": record.file_path},
                runs_dir=self.runs_dir,
            )
            return None
        try:
            spec = importlib.util.spec_from_file_location(f"skills.{path.stem}", path)
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = module
                spec.loader.exec_module(module)  # type: ignore[arg-type]
                append_skill_flow_event(
                    {"skill_id": record.skill_id, "event": "loaded", "file_path": record.file_path, "version": record.version},
                    runs_dir=self.runs_dir,
                )
                return module
        except Exception as exc:  # pragma: no cover - defensive
            append_skill_flow_event(
                {"skill_id": record.skill_id, "event": "load_failed", "reason": str(exc)},
                runs_dir=self.runs_dir,
            )
        return None

    def apply_plan_hooks(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        """Allow skills to inspect/modify trading plans prior to execution."""
        updated = dict(plan or {})
        for skill_id, (record, module) in self._loaded.items():
            hook = getattr(module, "on_plan", None)
            if callable(hook):
                self._invoke_hook(skill_id, "on_plan", hook, payload=updated, record=record)
        return updated

    def apply_decision_hooks(self, decision: Dict[str, Any], context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Allow skills to filter or enrich decision payloads."""
        payload = dict(decision or {})
        ctx = context or {}
        for skill_id, (record, module) in self._loaded.items():
            hook = getattr(module, "on_decision", None)
            if callable(hook):
                self._invoke_hook(skill_id, "on_decision", hook, payload=payload, record=record, context=ctx)
        return payload

    def apply_risk_hooks(self, risk_payload: Dict[str, Any], context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Apply risk adjustment hooks (optional)."""
        payload = dict(risk_payload or {})
        ctx = context or {}
        for skill_id, (record, module) in self._loaded.items():
            hook = getattr(module, "adjust_risk", None)
            if callable(hook):
                self._invoke_hook(skill_id, "adjust_risk", hook, payload=payload, record=record, context=ctx)
        return payload

    def _invoke_hook(
        self,
        skill_id: str,
        hook_name: str,
        hook: Callable[..., Any],
        *,
        payload: Dict[str, Any],
        record: SkillRecord,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Invoke a skill hook with auditing. Mutations happen in-place on payload."""
        try:
            result = hook(payload, context or {})
            append_skill_flow_event(
                {
                    "skill_id": skill_id,
                    "event": f"{hook_name}_applied",
                    "status": "ok",
                    "version": record.version,
                    "enabled": record.enabled,
                    "result": result if result is not None else "ok",
                },
                runs_dir=self.runs_dir,
            )
        except Exception as exc:  # pragma: no cover - defensive
            append_skill_flow_event(
                {
                    "skill_id": skill_id,
                    "event": f"{hook_name}_failed",
                    "status": "error",
                    "version": record.version,
                    "error": str(exc),
                },
                runs_dir=self.runs_dir,
            )

