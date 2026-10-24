from __future__ import annotations

from typing import Any, Dict, List, Protocol


class LearningAdapter(Protocol):
    """Strategy-specific learning adapter contract."""

    name: str

    def discover_symbols(self, strategy_cfg: Dict[str, Any]) -> List[str]:
        """Return symbols used by this strategy for learning/gates."""

    def build_training_context(self, strategy_cfg: Dict[str, Any], run_ctx: Dict[str, Any]) -> Dict[str, Any]:
        """Return adapter-specific context used by training/review."""

    def validate_metrics(self, learning_output: Dict[str, Any], gates_cfg: Dict[str, Any]) -> Dict[str, Any]:
        """Validate learning metrics with adapter-specific policy."""


class GenericLearningAdapter:
    """Default adapter that works for most strategies using params.extras."""

    name = "generic"

    def discover_symbols(self, strategy_cfg: Dict[str, Any]) -> List[str]:
        extras = (((strategy_cfg.get("params") or {}).get("extras")) or {})
        out: List[str] = []

        symbols = extras.get("symbols")
        if isinstance(symbols, list):
            for sym in symbols:
                s = str(sym).strip()
                if s and s not in out:
                    out.append(s)

        sym_single = extras.get("symbol")
        if sym_single is not None:
            s = str(sym_single).strip()
            if s and s not in out:
                out.append(s)

        nested_sym = (((extras.get("tsla_v7_2_config") or {}).get("symbol")) or "")
        if nested_sym:
            s = str(nested_sym).strip()
            if s and s not in out:
                out.append(s)

        return out

    def build_training_context(self, strategy_cfg: Dict[str, Any], run_ctx: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "strategy_id": str(strategy_cfg.get("id") or ""),
            "learning_profile": str(strategy_cfg.get("learning_profile") or "generic"),
            "symbols": self.discover_symbols(strategy_cfg),
            "run_ctx": dict(run_ctx or {}),
        }

    def validate_metrics(self, learning_output: Dict[str, Any], gates_cfg: Dict[str, Any]) -> Dict[str, Any]:
        return {"ok": True, "reason": "generic_adapter_no_extra_checks"}
