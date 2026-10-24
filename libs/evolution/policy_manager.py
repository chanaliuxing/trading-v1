from __future__ import annotations

"""
Lightweight policy manager to apply AI-proposed policy patches with basic semantic validation.
"""

import logging
from typing import Dict, Any, List

from scripts.ai_trader.research.policy import ResearchPolicy, save_research_policy

logger = logging.getLogger(__name__)


class PolicyManager:
    def apply_patches(self, current_policy: ResearchPolicy, patches: List[Dict[str, Any]]) -> ResearchPolicy:
        """
        Apply a list of patch candidates to the current policy with semantic validation.
        Patch format: {"path": "objectives.trading.risk_aversion", "value": 0.7}
        """
        updated = current_policy.to_json_dict()
        for patch in patches or []:
            try:
                if not self._validate(patch):
                    logger.warning(f"policy_patch_rejected: {patch}")
                    continue
                updated = self._apply_json_path(updated, patch.get("path"), patch.get("value"))
                logger.info(f"policy_patch_applied: path={patch.get('path')}, value={patch.get('value')}")
            except Exception as e:
                logger.warning(f"policy_patch_failed: patch={patch}, error={e}")
        new_policy = ResearchPolicy.from_json_dict(updated)
        try:
            save_research_policy(new_policy)
        except Exception as e:
            logger.warning(f"policy_save_failed: error={e}")
        return new_policy

    def _validate(self, patch: Dict[str, Any]) -> bool:
        """
        Add simple semantic guards; expand as needed.
        """
        path = patch.get("path", "")
        val = patch.get("value")
        if "risk_aversion" in path:
            try:
                v = float(val)
                return 0.0 <= v <= 1.0
            except Exception:
                return False
        if "exploration_ratio" in path:
            try:
                v = float(val)
                return 0.0 <= v <= 1.0
            except Exception:
                return False
        return True

    def _apply_json_path(self, data: Dict[str, Any], path: str, value: Any) -> Dict[str, Any]:
        if not path:
            return data
        keys = path.split(".")
        ref = data
        for k in keys[:-1]:
            if k not in ref or not isinstance(ref[k], dict):
                ref[k] = {}
            ref = ref[k]
        ref[keys[-1]] = value
        return data
