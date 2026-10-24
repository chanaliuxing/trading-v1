from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional


def build_snapshot_id(
    *,
    strategy_id: str,
    bar_close_ts: datetime,
    signal_kind: str,
    side: str,
    opt_symbol: str,
) -> str:
    ts_key = bar_close_ts.isoformat()
    raw = "|".join(
        [
            str(strategy_id or "").strip(),
            str(ts_key or "").strip(),
            str(signal_kind or "").strip(),
            str(side or "").strip().upper(),
            str(opt_symbol or "").strip().upper(),
        ]
    )
    return f"parity:{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:20]}"


@dataclass(frozen=True)
class DecisionSnapshotStore:
    root_dir: Path

    def snapshot_path(self, *, strategy_id: str, ts: datetime, snapshot_id: str) -> Path:
        day = ts.strftime("%Y-%m-%d")
        return self.root_dir / str(strategy_id or "").strip() / day / f"{snapshot_id}.json"

    def exists(self, *, strategy_id: str, ts: datetime, snapshot_id: str) -> bool:
        return self.snapshot_path(strategy_id=strategy_id, ts=ts, snapshot_id=snapshot_id).exists()

    def write(
        self,
        *,
        strategy_id: str,
        ts: datetime,
        snapshot_id: str,
        payload: Dict[str, Any],
    ) -> Path:
        path = self.snapshot_path(strategy_id=strategy_id, ts=ts, snapshot_id=snapshot_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str),
            encoding="utf-8",
        )
        os.replace(tmp, path)
        return path

    def read(
        self,
        *,
        strategy_id: str,
        ts: datetime,
        snapshot_id: str,
    ) -> Optional[Dict[str, Any]]:
        path = self.snapshot_path(strategy_id=strategy_id, ts=ts, snapshot_id=snapshot_id)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
