from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any, Dict, List

import yaml

from .registry import LearningAdapterRegistry


class LearningPipeline:
    """Resolve learning targets and route strategy-specific behavior via adapters."""

    def __init__(
        self,
        control_cfg: Dict[str, Any],
        runtime_config_path: str = "config.yml",
        registry: LearningAdapterRegistry | None = None,
    ) -> None:
        self.control_cfg = control_cfg or {}
        self.runtime_config_path = Path(runtime_config_path)
        self.registry = registry or LearningAdapterRegistry()

    def _load_runtime_cfg(self) -> Dict[str, Any]:
        try:
            if not self.runtime_config_path.exists():
                return {}
            return yaml.safe_load(self.runtime_config_path.read_text("utf-8")) or {}
        except Exception:
            return {}

    def _strategies(self) -> List[Dict[str, Any]]:
        data = self._load_runtime_cfg()
        items = data.get("strategies") or []
        return [x for x in items if isinstance(x, dict)]

    def _strategy_by_id(self, strategy_id: str) -> Dict[str, Any]:
        sid = str(strategy_id or "")
        for s in self._strategies():
            if str(s.get("id") or "") == sid:
                return s
        return {}

    def _configured_targets(self) -> List[str]:
        learning_cfg = (((self.control_cfg.get("policies") or {}).get("learning")) or {})
        targets = learning_cfg.get("default_targets") or []
        if not isinstance(targets, list):
            return []
        out: List[str] = []
        known = {str(s.get("id") or "") for s in self._strategies()}
        for t in targets:
            sid = str(t or "").strip()
            if sid and sid in known and sid not in out:
                out.append(sid)
        return out

    def _enabled_targets(self) -> List[str]:
        out: List[str] = []
        for s in self._strategies():
            sid = str(s.get("id") or "").strip()
            if not sid:
                continue
            if bool(s.get("enabled", True)) and sid not in out:
                out.append(sid)
        return out

    def _fallback_from_apply_params(self) -> str:
        cmds = self.control_cfg.get("commands") or {}
        cmd = str(cmds.get("apply_params") or "")
        if not cmd:
            return ""
        try:
            parts = shlex.split(cmd)
        except Exception:
            return ""
        for i, p in enumerate(parts):
            if p == "--strategy-id" and i + 1 < len(parts):
                return str(parts[i + 1]).strip()
        return ""

    def resolve_targets(self) -> List[str]:
        targets = self._configured_targets()
        if targets:
            return targets

        enabled = self._enabled_targets()
        if enabled:
            return enabled

        fallback = self._fallback_from_apply_params()
        if fallback and self._strategy_by_id(fallback):
            return [fallback]
        return []

    def primary_target(self) -> str:
        targets = self.resolve_targets()
        if targets:
            return targets[0]
        fallback = self._fallback_from_apply_params()
        return fallback or "unknown"

    def strategy_cfg(self, strategy_id: str) -> Dict[str, Any]:
        return self._strategy_by_id(strategy_id)

    def strategy_extras(self, strategy_id: str) -> Dict[str, Any]:
        s = self._strategy_by_id(strategy_id)
        return (((s.get("params") or {}).get("extras")) or {})

    def apply_command_for(self, strategy_id: str) -> str:
        strategy_cfg = self._strategy_by_id(strategy_id)
        learning_node = strategy_cfg.get("learning") or {}
        override = str(
            learning_node.get("apply_params_cmd")
            or strategy_cfg.get("apply_params_cmd")
            or ""
        ).strip()
        if override:
            return override
        return str((self.control_cfg.get("commands") or {}).get("apply_params") or "")

    def apply_strategy_config_for(self, strategy_id: str) -> str:
        strategy_cfg = self._strategy_by_id(strategy_id)
        learning_node = strategy_cfg.get("learning") or {}
        return str(learning_node.get("strategy_config") or strategy_cfg.get("strategy_config") or "").strip()

    def adapter_for(self, strategy_id: str):
        strategy_cfg = self._strategy_by_id(strategy_id)
        profile = self.profile_for(strategy_id)
        return self.registry.create(profile)

    def profile_for(self, strategy_id: str) -> str:
        strategy_cfg = self._strategy_by_id(strategy_id)
        learning_cfg = (((self.control_cfg.get("policies") or {}).get("learning")) or {})
        return str(
            strategy_cfg.get("learning_profile")
            or learning_cfg.get("fallback_profile")
            or "generic"
        ).strip() or "generic"

    def adapter_name_for(self, strategy_id: str) -> str:
        try:
            adapter = self.adapter_for(strategy_id)
            return str(getattr(adapter, "name", "") or "generic").strip() or "generic"
        except Exception:
            return "generic"

    def pipeline_version(self) -> str:
        learning_cfg = (((self.control_cfg.get("policies") or {}).get("learning")) or {})
        return str(learning_cfg.get("pipeline_version") or "learning-pipeline-v1").strip() or "learning-pipeline-v1"

    def collect_symbols(self, targets: List[str] | None = None) -> List[str]:
        out: List[str] = []
        for sid in (targets or self.resolve_targets()):
            strategy_cfg = self._strategy_by_id(sid)
            if not strategy_cfg:
                continue
            adapter = self.adapter_for(sid)
            for sym in adapter.discover_symbols(strategy_cfg):
                s = str(sym).strip()
                if s and s not in out:
                    out.append(s)
        return out
