"""
StateMemex Component
Handles atomic JSON persistence with concurrency protection.
"""
import os
import json
import asyncio
from pathlib import Path
from typing import Any, Dict, Optional
from datetime import datetime
from .base import StrategyComponent

class StateMemex(StrategyComponent):
    def __init__(self, ctx: Any, state: Dict[str, Any], state_lock: asyncio.Lock, state_dir: str = "runtime/state"):
        super().__init__(ctx, state, state_lock)
        self.strategy_id = ctx.strategy_id
        self.state_dir = Path(state_dir).expanduser()
        self.state_path = self.state_dir / f"state_v2_{self.strategy_id}.json"
        self.state_dir.mkdir(parents=True, exist_ok=True)

    async def initialize(self) -> None:
        self.load()
        self.logger.info("state_memex_initialized", path=str(self.state_path))

    def load(self) -> bool:
        if not self.state_path.exists(): return False
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            today = datetime.now().strftime("%Y-%m-%d")
            if raw.get("_metadata", {}).get("day") == today:
                for k, v in raw.items():
                    if k != "_metadata": self.state[k] = v
                return True
            return False
        except Exception as e:
            self.logger.error("state_load_failed", error=str(e))
            return False

    async def save(self, extra_metadata: Optional[Dict[str, Any]] = None) -> None:
        """Saves state atomically using the shared lock."""
        try:
            async with self.state_lock:
                payload = self.state.copy()
            
            now = datetime.now()
            payload["_metadata"] = {
                "strategy_id": self.strategy_id,
                "day": now.strftime("%Y-%m-%d"),
                "updated_at": now.isoformat()
            }
            if extra_metadata: payload["_metadata"].update(extra_metadata)

            tmp_path = self.state_path.with_name(self.state_path.name + ".tmp")
            with tmp_path.open("w", encoding="utf-8") as f:
                json.dump(payload, f, separators=(",", ":"))
            os.replace(tmp_path, self.state_path)
        except Exception as e:
            self.logger.error("state_save_failed", error=str(e))
