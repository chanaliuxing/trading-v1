from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Dict, Optional

from libs.logger import get_logger

logger = get_logger(__name__)


class ManualInterventionLockStore:
    """File-backed strategy/symbol lock to prevent immediate re-entry after manual flatten."""

    def __init__(self, *, file_path: Path) -> None:
        self.file_path = Path(file_path)
        self._lock = asyncio.Lock()

    @staticmethod
    def _key(strategy_id: str, symbol: str) -> str:
        return f"{str(strategy_id).strip()}::{str(symbol).strip().upper()}"

    async def lock(
        self,
        *,
        strategy_id: str,
        symbol: str,
        ttl_sec: int,
        reason: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        now = time.time()
        rec = {
            "strategy_id": str(strategy_id).strip(),
            "symbol": str(symbol).strip().upper(),
            "reason": str(reason),
            "created_ts": now,
            "expire_ts": now + max(1, int(ttl_sec)),
            "context": dict(context or {}),
        }
        key = self._key(rec["strategy_id"], rec["symbol"])
        async with self._lock:
            data = await self._load()
            self._prune_expired(data, now=now)
            data[key] = rec
            await self._save(data)
        return rec

    async def get_active(
        self,
        *,
        strategy_id: str,
        symbol: str,
        strategy_instance_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        key = self._key(strategy_id, symbol)
        now = time.time()
        async with self._lock:
            data = await self._load()
            changed = self._prune_expired(data, now=now)
            rec = data.get(key)
            if changed:
                await self._save(data)
            if isinstance(rec, dict) and not self._lock_matches_strategy_instance(
                rec,
                strategy_instance_id=strategy_instance_id,
            ):
                return None
            return dict(rec) if isinstance(rec, dict) else None

    async def unlock(self, *, strategy_id: str, symbol: str) -> bool:
        key = self._key(strategy_id, symbol)
        async with self._lock:
            data = await self._load()
            existed = key in data
            if existed:
                data.pop(key, None)
                await self._save(data)
            return bool(existed)

    async def _load(self) -> Dict[str, Any]:
        try:
            if not self.file_path.exists():
                return {}
            raw = self.file_path.read_text(encoding="utf-8").strip()
            if not raw:
                return {}
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except Exception as exc:
            logger.warning("manual_intervention_lock_load_failed", file=str(self.file_path), error=str(exc))
        return {}

    async def _save(self, data: Dict[str, Any]) -> None:
        try:
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.file_path.with_suffix(self.file_path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            tmp.replace(self.file_path)
        except Exception as exc:
            logger.warning("manual_intervention_lock_save_failed", file=str(self.file_path), error=str(exc))

    @staticmethod
    def _prune_expired(data: Dict[str, Any], *, now: Optional[float] = None) -> bool:
        ts = float(now if now is not None else time.time())
        changed = False
        for k in list(data.keys()):
            rec = data.get(k)
            if not isinstance(rec, dict):
                data.pop(k, None)
                changed = True
                continue
            try:
                exp = float(rec.get("expire_ts") or 0.0)
            except Exception:
                exp = 0.0
            if exp <= ts:
                data.pop(k, None)
                changed = True
        return changed

    @staticmethod
    def _lock_matches_strategy_instance(rec: Dict[str, Any], *, strategy_instance_id: Optional[str] = None) -> bool:
        requested = str(strategy_instance_id or "").strip()
        if not requested:
            return True
        context = rec.get("context") if isinstance(rec.get("context"), dict) else {}
        locked = str(context.get("strategy_instance_id") or rec.get("strategy_instance_id") or "").strip()
        return not locked or locked == requested


class PostgresManualInterventionLockStore:
    """PostgreSQL-backed lock store for multi-process consistency."""

    def __init__(self, *, pg: Any, table_name: str = "core.manual_intervention_lock") -> None:
        self.pg = pg
        self.table_name = str(table_name or "core.manual_intervention_lock")
        self._init_lock = asyncio.Lock()
        self._initialized = False

    async def _ensure_table(self) -> None:
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            await self.pg.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self.table_name} (
                    strategy_id text NOT NULL,
                    symbol text NOT NULL,
                    reason text NOT NULL,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    expire_at timestamptz NOT NULL,
                    context jsonb NOT NULL DEFAULT '{{}}'::jsonb,
                    PRIMARY KEY (strategy_id, symbol)
                )
                """
            )
            await self.pg.execute(
                f"CREATE INDEX IF NOT EXISTS manual_intervention_lock_expire_idx ON {self.table_name} (expire_at)"
            )
            self._initialized = True

    async def lock(
        self,
        *,
        strategy_id: str,
        symbol: str,
        ttl_sec: int,
        reason: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        await self._ensure_table()
        strategy_id = str(strategy_id).strip()
        symbol = str(symbol).strip().upper()
        ttl = max(1, int(ttl_sec))
        ctx = dict(context or {})
        await self.pg.execute(
            f"""
            INSERT INTO {self.table_name} (strategy_id, symbol, reason, created_at, expire_at, context)
            VALUES ($1, $2, $3, now(), now() + ($4::int * interval '1 second'), $5::jsonb)
            ON CONFLICT (strategy_id, symbol) DO UPDATE
            SET reason = EXCLUDED.reason,
                created_at = now(),
                expire_at = EXCLUDED.expire_at,
                context = EXCLUDED.context
            """,
            strategy_id,
            symbol,
            str(reason),
            ttl,
            json.dumps(ctx, ensure_ascii=False),
        )
        row = await self.pg.fetchone(
            f"""
            SELECT extract(epoch from created_at) AS created_ts,
                   extract(epoch from expire_at) AS expire_ts
            FROM {self.table_name}
            WHERE strategy_id = $1 AND symbol = $2
            """,
            strategy_id,
            symbol,
        )
        return {
            "strategy_id": strategy_id,
            "symbol": symbol,
            "reason": str(reason),
            "created_ts": float((row or {}).get("created_ts") or time.time()),
            "expire_ts": float((row or {}).get("expire_ts") or (time.time() + ttl)),
            "context": ctx,
        }

    async def get_active(
        self,
        *,
        strategy_id: str,
        symbol: str,
        strategy_instance_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        await self._ensure_table()
        strategy_id = str(strategy_id).strip()
        symbol = str(symbol).strip().upper()
        # prune expired first
        await self.pg.execute(f"DELETE FROM {self.table_name} WHERE expire_at <= now()")
        row = await self.pg.fetchone(
            f"""
            SELECT reason,
                   extract(epoch from created_at) AS created_ts,
                   extract(epoch from expire_at) AS expire_ts,
                   context
            FROM {self.table_name}
            WHERE strategy_id = $1 AND symbol = $2
              AND expire_at > now()
            """,
            strategy_id,
            symbol,
        )
        if not row:
            return None
        context = row.get("context") if isinstance(row, dict) else row["context"]
        if isinstance(context, str):
            try:
                context = json.loads(context)
            except Exception:
                context = {}
        requested_instance = str(strategy_instance_id or "").strip()
        locked_instance = str((context or {}).get("strategy_instance_id") or "").strip()
        if requested_instance and locked_instance and requested_instance != locked_instance:
            return None
        return {
            "reason": str((row.get("reason") if isinstance(row, dict) else row["reason"]) or ""),
            "created_ts": float((row.get("created_ts") if isinstance(row, dict) else row["created_ts"]) or 0.0),
            "expire_ts": float((row.get("expire_ts") if isinstance(row, dict) else row["expire_ts"]) or 0.0),
            "context": dict(context or {}),
        }

    async def unlock(self, *, strategy_id: str, symbol: str) -> bool:
        await self._ensure_table()
        strategy_id = str(strategy_id).strip()
        symbol = str(symbol).strip().upper()
        result = await self.pg.execute(
            f"DELETE FROM {self.table_name} WHERE strategy_id = $1 AND symbol = $2",
            strategy_id,
            symbol,
        )
        try:
            tag = str(result or "")
            if tag.startswith("DELETE "):
                return int(tag.split()[-1]) > 0
        except Exception:
            pass
        return True
