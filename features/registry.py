# features/registry.py

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple


@dataclass
class FeatureDef:
    name: str
    kind: str                    # "native" | "python" | "sandbox"
    freq: List[str]
    base_cols: List[str]
    tags: List[str]
    desc: str
    leak_safe: bool = True
    default_exposed_to_llm: bool = True

    # UF 17 v2 fields (optional for backward compatibility)
    table: Optional[str] = None
    dtype: Optional[str] = None
    role: Optional[str] = None
    granularity: Optional[str] = None
    source: Optional[str] = None
    depends_on: Optional[List[str]] = None
    formula: Optional[str] = None
    allowed_tasks: Optional[List[str]] = None
    allowed_labels: Optional[List[str]] = None
    horizon_min: Optional[int] = None
    horizon_max: Optional[int] = None

    def supports_label(self, label_id: str) -> bool:
        """Check if feature supports given label_id."""
        if not self.allowed_labels:
            return True  # No restrictions
        return "*" in self.allowed_labels or label_id in self.allowed_labels

    def supports_horizon(self, horizon_bars: Optional[int]) -> bool:
        """Check if feature supports given horizon."""
        if horizon_bars is None:
            return True
        if self.horizon_min is not None and horizon_bars < self.horizon_min:
            return False
        if self.horizon_max is not None and horizon_bars > self.horizon_max:
            return False
        return True

    def supports_task(self, task: str) -> bool:
        """Check if feature supports given task type."""
        if not self.allowed_tasks:
            return True  # No restrictions
        return task in self.allowed_tasks


class FeatureRegistry:
    """
    Lightweight feature registry.

    当前用途：
      - 给 LLM 一份"推荐特征列表"（不强制）
      - 以后可以逐步扩展为更严格的 contract
    """

    def __init__(self, features: Dict[str, FeatureDef], version: str = "1.0") -> None:
        self._features = features
        self._version = version

    @classmethod
    def from_json(cls, path: str | Path) -> "FeatureRegistry":
        p = Path(path)
        if not p.exists():
            # 没有文件时，给一个空 registry，避免直接炸
            return cls(features={}, version="0.0")

        raw = json.loads(p.read_text(encoding="utf-8"))
        version = str(raw.get("version", "1.0"))
        feats: Dict[str, FeatureDef] = {}

        for name, f in raw.get("features", {}).items():
            feats[name] = FeatureDef(
                name=name,
                kind=f.get("kind", "native"),
                freq=list(f.get("freq", [])),
                base_cols=list(f.get("base_cols", [])),
                tags=list(f.get("tags", [])),
                desc=f.get("desc", ""),
                leak_safe=bool(f.get("leak_safe", True)),
                default_exposed_to_llm=bool(f.get("default_exposed_to_llm", True)),
                # UF 17 v2 fields
                table=f.get("table"),
                dtype=f.get("dtype"),
                role=f.get("role"),
                granularity=f.get("granularity"),
                source=f.get("source"),
                depends_on=f.get("depends_on"),
                formula=f.get("formula"),
                allowed_tasks=f.get("allowed_tasks"),
                allowed_labels=f.get("allowed_labels"),
                horizon_min=f.get("horizon_min"),
                horizon_max=f.get("horizon_max"),
            )

        return cls(features=feats, version=version)

    @property
    def version(self) -> str:
        return self._version

    def get(self, name: str) -> Optional[FeatureDef]:
        return self._features.get(name)

    def all_names(self) -> List[str]:
        return sorted(self._features.keys())

    def list_for_freq(
        self,
        freq: str,
        *,
        exposed_only: bool = True,
    ) -> List[FeatureDef]:
        out: List[FeatureDef] = []
        for f in self._features.values():
            if freq not in f.freq:
                continue
            if exposed_only and not f.default_exposed_to_llm:
                continue
            out.append(f)
        # 稍微按 name 排个序，方便阅读
        out.sort(key=lambda x: x.name)
        return out

    def summarize_for_llm(self, freq: str) -> str:
        """
        把给定 freq 下建议的特征，变成一段简短的自然语言摘要，
        用在 system / user prompt 中，指导 LLM 使用。
        """
        feats = self.list_for_freq(freq=freq, exposed_only=True)
        if not feats:
            return "No curated features registered for this frequency yet."

        lines: List[str] = []
        for f in feats:
            tags = ", ".join(f.tags) if f.tags else ""
            tag_str = f" (tags: {tags})" if tags else ""
            lines.append(f"- {f.name}{tag_str}: {f.desc}")
        return "\n".join(lines)

    def filter_for_task_and_label(
        self,
        *,
        candidate_features: List[str],
        task: str,
        label_id: str,
        horizon_bars: Optional[int] = None,
    ) -> Tuple[List[str], List[str]]:
        """
        Filter candidate features based on task, label, and horizon constraints.

        Returns:
            Tuple of (allowed_features, rejected_features_with_reasons)

        This is the key v2 method that enforces feature constraints.
        """
        allowed: List[str] = []
        rejected: List[str] = []

        for f in candidate_features:
            if f not in self._features:
                rejected.append(f"{f} (not in registry)")
                continue

            spec = self._features[f]

            # Check task support
            if not spec.supports_task(task):
                rejected.append(f"{f} (not allowed for task={task})")
                continue

            # Check label support
            if not spec.supports_label(label_id):
                rejected.append(f"{f} (not allowed for label={label_id})")
                continue

            # Check horizon support
            if not spec.supports_horizon(horizon_bars):
                rejected.append(f"{f} (not allowed for horizon={horizon_bars})")
                continue

            # All checks passed
            allowed.append(f)

        return allowed, rejected


# 默认加载器，可以在其他模块中直接使用
def get_default_registry(config_dir: str | Path = "config") -> FeatureRegistry:
    path = Path(config_dir) / "feature_registry.json"
    return FeatureRegistry.from_json(path)
