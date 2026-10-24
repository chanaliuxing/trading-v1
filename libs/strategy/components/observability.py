"""
Observability Component (V1 Port)
Generates periodic JSON snapshots of the full strategy state for monitoring.
"""
import json
import os
import asyncio
from pathlib import Path
from typing import Any, Dict, Optional
from datetime import datetime
from .base import StrategyComponent

class Observability(StrategyComponent):
    def __init__(self, ctx: Any, state: Dict[str, Any], state_lock: asyncio.Lock, obs_path: str):
        super().__init__(ctx, state, state_lock)
        self.obs_path = Path(obs_path).expanduser()
        self.obs_path.parent.mkdir(parents=True, exist_ok=True)
        self._last_write_min = None

    async def initialize(self) -> None:
        pass

    def write_snapshot(self, now: datetime, components: Dict[str, Any]) -> None:
        """Periodic state dump (V1: _maybe_write_observability_file)."""
        current_min = now.replace(second=0, microsecond=0)
        if self._last_write_min == current_min: return
        self._last_write_min = current_min

        try:
            snapshot = {
                "ts_now_et": now.isoformat(),
                "strategy_id": self.ctx.strategy_id,
                "strategy_state": self.state.copy(),
                "components": {}
            }
            # Collect component snapshots
            for name, comp in components.items():
                if hasattr(comp, "get_state_snapshot"):
                    snapshot["components"][name] = comp.get_state_snapshot()

            tmp = self.obs_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")))
            os.replace(tmp, self.obs_path)
        except Exception as e:
            self.logger.warning("obs_snapshot_failed", error=str(e))
