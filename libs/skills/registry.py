from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Any, Optional


@dataclass
class SkillRecord:
    skill_id: str
    file_path: str
    test_path: str
    description: str = ""
    skill_type: str = "PURE"
    allow_evolution: bool = True
    objective: str = "PERFORMANCE"
    metrics_impact: list[str] = field(default_factory=list)
    category: str = "unknown"
    approved_at: Optional[str] = None
    version: int = 1
    enabled: bool = False
    performance_baseline: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, skill_id: str, raw: Dict[str, Any]) -> "SkillRecord":
        """Create a normalized record from a raw dictionary."""
        version = int(raw.get("version", 1) or 1)
        enabled = bool(raw.get("enabled", raw.get("enable", False)))
        return cls(
            skill_id=skill_id,
            file_path=raw.get("file_path", ""),
            test_path=raw.get("test_path", ""),
            description=raw.get("description", ""),
            skill_type=raw.get("type") or raw.get("skill_type", "PURE"),
            allow_evolution=bool(raw.get("allow_evolution", True)),
            objective=raw.get("objective", "PERFORMANCE"),
            metrics_impact=list(raw.get("metrics_impact", [])),
            category=raw.get("category", "unknown"),
            approved_at=raw.get("approved_at"),
            version=version,
            enabled=enabled,
            performance_baseline=dict(raw.get("performance_baseline", {})),
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialize record back to a dict for persistence."""
        return {
            "file_path": self.file_path,
            "test_path": self.test_path,
            "type": self.skill_type,
            "description": self.description,
            "allow_evolution": self.allow_evolution,
            "objective": self.objective,
            "metrics_impact": self.metrics_impact,
            "category": self.category,
            "approved_at": self.approved_at,
            "version": self.version,
            "enabled": self.enabled,
            "performance_baseline": self.performance_baseline,
        }


class SkillRegistry:
    """Manage the skill registry file with backward-compatible normalization."""

    def __init__(self, path: str | Path = "config/skill_registry.json"):
        self.path = Path(path)
        self._skills: Dict[str, SkillRecord] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            self._skills = {}
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        raw_skills = (data or {}).get("skills", {}) or {}
        normalized = {}
        for skill_id, raw in raw_skills.items():
            record = SkillRecord.from_dict(skill_id, raw or {})
            normalized[skill_id] = record
        self._skills = normalized

    def _persist(self) -> None:
        payload = {"skills": {k: v.to_dict() for k, v in self._skills.items()}}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    def list_enabled(self) -> Dict[str, SkillRecord]:
        return {k: v for k, v in self._skills.items() if v.enabled}

    def list_all(self) -> Dict[str, SkillRecord]:
        return dict(self._skills)

    def upsert(self, record: SkillRecord, enabled: Optional[bool] = None, performance_baseline: Optional[Dict[str, Any]] = None) -> SkillRecord:
        """Insert or update a skill record.

        If the skill already exists, increment the version and optionally update the baseline/enabled state.
        """
        existing = self._skills.get(record.skill_id)
        if existing:
            record.version = existing.version + 1
            if performance_baseline is None:
                performance_baseline = existing.performance_baseline
        record.enabled = bool(record.enabled if enabled is None else enabled)
        if performance_baseline is not None:
            record.performance_baseline = performance_baseline
        self._skills[record.skill_id] = record
        self._persist()
        return record

    def set_enabled(self, skill_id: str, enabled: bool) -> None:
        if skill_id not in self._skills:
            return
        rec = self._skills[skill_id]
        rec.enabled = enabled
        self._persist()

    def record_performance(self, skill_id: str, metrics: Dict[str, Any]) -> None:
        if skill_id not in self._skills:
            return
        rec = self._skills[skill_id]
        rec.performance_baseline.update(metrics or {})
        self._persist()

