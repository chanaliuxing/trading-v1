# persistence/state_store.py
from __future__ import annotations
import json, os, time
from pathlib import Path
from typing import Dict, Any


class StateStore:
    """
    Persistent state storage for crash recovery.

    Provides atomic writes of cash/positions/ledger state
    to enable recovery after process crashes.
    """
    def __init__(self, run_id: str, base_dir: str = "/tmp/alpha_state"):
        self.run_id = run_id
        self.dir = Path(base_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / f"{run_id}.json"

    def save(self, cash: float, positions: Dict[str, Dict[str, float]], ledger_tail: list[dict]|None=None):
        """
        Save current state atomically.

        Args:
            cash: Current cash balance
            positions: Current positions dict
            ledger_tail: Recent ledger entries (keeps last 1000)
        """
        snap = {
            "ts": int(time.time()*1000),
            "cash": float(cash),
            "positions": positions,
            "ledger_tail": (ledger_tail or [])[-1000:],  # Keep last 1000
        }

        # Atomic write: write to temp file then rename
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(snap, ensure_ascii=False, indent=0), encoding="utf-8")
        tmp.replace(self.path)

    def load(self) -> dict:
        """
        Load saved state.

        Returns:
            State dict with ts, cash, positions, ledger_tail
            Empty dict if no saved state exists
        """
        if not self.path.exists():
            return {}

        try:
            return json.load(open(self.path,"r",encoding="utf-8"))
        except Exception:
            return {}
