from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple


@dataclass(frozen=True)
class FlipGuardVerdict:
    allowed: bool
    reason: str
    metadata: Dict[str, Any]


class FlipGuardRegistry:
    """
    System-level reopen/reversal guard.

    This intentionally sits below strategy code so entry throttling does not rely
    on per-strategy local state.
    """

    _DEFAULT_ROOT_OVERRIDES = {
        "TQQQ": "QQQ",
        "SQQQ": "QQQ",
        "SOXL": "SOX",
        "SOXS": "SOX",
        "TNA": "RUT",
        "TZA": "RUT",
        "NUGT": "GOLD",
        "DUST": "GOLD",
        "LABU": "XBI",
        "LABD": "XBI",
    }

    def __init__(
        self,
        *,
        cooldown_sec: float = 60.0,
        same_bar_reversal_block: bool = True,
        exempt_strategy_ids: Optional[Iterable[Any]] = None,
        root_symbol_overrides: Optional[Dict[str, str]] = None,
        state_path: str | None = "runtime/state/flip_guard_registry.json",
    ) -> None:
        self.cooldown_sec = max(0.0, float(cooldown_sec))
        self.same_bar_reversal_block = bool(same_bar_reversal_block)
        self.exempt_strategy_ids = self._normalize_exemptions(exempt_strategy_ids or [])
        overrides = dict(self._DEFAULT_ROOT_OVERRIDES)
        overrides.update(
            {
                str(k or "").strip().upper(): str(v or "").strip().upper()
                for k, v in dict(root_symbol_overrides or {}).items()
                if str(k or "").strip() and str(v or "").strip()
            }
        )
        self.root_symbol_overrides = overrides
        self._state_path = Path(str(state_path)).expanduser() if state_path else None
        self._last_flat: Dict[str, Dict[str, Any]] = {}
        self._last_open: Dict[str, Dict[str, Any]] = {}
        self._pg_hydrated = False
        self._load_state()

    @classmethod
    def _parse_expires_at(cls, value: Any) -> Optional[float]:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return float(text)
        except Exception:
            pass
        try:
            normalized = text.replace("Z", "+00:00")
            return datetime.fromisoformat(normalized).astimezone(timezone.utc).timestamp()
        except Exception:
            return None

    @classmethod
    def _normalize_exemptions(cls, raw_items: Iterable[Any]) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for item in raw_items or []:
            if isinstance(item, dict):
                strategy_id = str(item.get("strategy_id") or item.get("id") or "").strip()
                expires_at = cls._parse_expires_at(item.get("expires_at") or item.get("expires_ts"))
                reason = str(item.get("reason") or "configured_exemption").strip()
            else:
                text = str(item or "").strip()
                if "@" in text:
                    strategy_id, raw_expiry = text.split("@", 1)
                    strategy_id = strategy_id.strip()
                    expires_at = cls._parse_expires_at(raw_expiry)
                else:
                    strategy_id = text
                    expires_at = None
                reason = "configured_exemption"
            if not strategy_id:
                continue
            # No-expiry exemptions are intentionally not active; they remain visible
            # in metadata so config audits can find and remove permanent bypasses.
            out[strategy_id] = {"expires_at": expires_at, "reason": reason}
        return out

    def _active_exemption(self, *, strategy_id: str, now_epoch_s: float) -> Optional[Dict[str, Any]]:
        sid = str(strategy_id or "").strip()
        if not sid:
            return None
        item = self.exempt_strategy_ids.get(sid)
        if not item:
            return None
        expires_at = item.get("expires_at")
        if expires_at is None:
            return None
        try:
            expires_f = float(expires_at)
        except Exception:
            return None
        if expires_f <= float(now_epoch_s):
            return None
        return {"strategy_id": sid, "expires_at": expires_f, "reason": str(item.get("reason") or "configured_exemption")}

    def _load_state(self) -> None:
        if self._state_path is None or (not self._state_path.exists()):
            return
        try:
            raw = self._state_path.read_text(encoding="utf-8").strip()
            if not raw:
                return
            payload = json.loads(raw)
            if isinstance(payload, dict):
                self._last_flat = dict(payload.get("last_flat") or {})
                self._last_open = dict(payload.get("last_open") or {})
        except Exception:
            pass

    def _save_state(self) -> None:
        if self._state_path is None:
            return
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(
                    {
                        "last_flat": self._last_flat,
                        "last_open": self._last_open,
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            tmp.replace(self._state_path)
        except Exception:
            pass

    async def ensure_pg_schema(self, pg: Any) -> None:
        if pg is None:
            return
        ddl_path = Path("db/pg/migration_20260421_flip_guard.ddl")
        statements = [
            stmt.strip()
            for stmt in ddl_path.read_text(encoding="utf-8").split(";")
            if stmt.strip()
        ]
        for statement in statements:
            await pg.execute(statement)

    async def hydrate_from_pg(self, pg: Any) -> None:
        if pg is None or self._pg_hydrated:
            return
        await self.ensure_pg_schema(pg)
        rows = await pg.fetchall(
            """
            SELECT root_symbol, action, symbol, side, strategy_id, bar_id, ts_epoch_s, metadata
            FROM core.core_flip_guard
            WHERE action IN ('entry', 'flat')
            """
        )
        for row in rows or []:
            root_symbol = str((row or {}).get("root_symbol") or "").strip().upper()
            action = str((row or {}).get("action") or "").strip().lower()
            if not root_symbol or action not in {"entry", "flat"}:
                continue
            item = {
                "ts_epoch_s": float((row or {}).get("ts_epoch_s") or 0.0),
                "bar_id": str((row or {}).get("bar_id") or ""),
                "side": str((row or {}).get("side") or "").strip().upper(),
                "strategy_id": str((row or {}).get("strategy_id") or ""),
                "symbol": str((row or {}).get("symbol") or "").strip().upper(),
            }
            metadata = (row or {}).get("metadata")
            if isinstance(metadata, dict):
                item.update({k: v for k, v in metadata.items() if k not in item})
            if action == "entry":
                self._last_open[root_symbol] = item
            else:
                self._last_flat[root_symbol] = item
        self._pg_hydrated = True
        self._save_state()

    async def persist_submission_to_pg(self, pg: Any, record: Optional[Dict[str, Any]]) -> None:
        if pg is None or not record:
            return
        await self.ensure_pg_schema(pg)
        await pg.execute(
            """
            INSERT INTO core.core_flip_guard (
                root_symbol, action, symbol, side, strategy_id, bar_id, ts_epoch_s, metadata, updated_at
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8::jsonb, now()
            )
            ON CONFLICT (root_symbol, action) DO UPDATE
            SET symbol = EXCLUDED.symbol,
                side = EXCLUDED.side,
                strategy_id = EXCLUDED.strategy_id,
                bar_id = EXCLUDED.bar_id,
                ts_epoch_s = EXCLUDED.ts_epoch_s,
                metadata = EXCLUDED.metadata,
                updated_at = now()
            """,
            str(record.get("root_symbol") or ""),
            str(record.get("action") or ""),
            str(record.get("symbol") or ""),
            str(record.get("side") or ""),
            str(record.get("strategy_id") or ""),
            str(record.get("bar_id") or ""),
            float(record.get("ts_epoch_s") or 0.0),
            json.dumps(dict(record.get("metadata") or {}), ensure_ascii=False),
        )

    @staticmethod
    def _bar_id_from_ts(ts_epoch_s: float) -> str:
        bucket = int(max(0.0, float(ts_epoch_s)) // 60)
        return f"m1:{bucket}"

    @staticmethod
    def _get_nested_value(payload: Any, *keys: str) -> Any:
        current = payload
        for key in keys:
            if current is None:
                return None
            if isinstance(current, dict):
                current = current.get(key)
            else:
                current = getattr(current, key, None)
        return current

    def _normalize_root_candidate(self, value: Any) -> str:
        candidate = str(value or "").strip().upper()
        if not candidate:
            return ""
        if candidate.startswith("OPT:"):
            parts = candidate.split(":")
            if len(parts) >= 2 and parts[1]:
                candidate = parts[1].strip().upper()
        return self.root_symbol_overrides.get(candidate, candidate)

    def _metadata_root_symbol(self, extras: Dict[str, Any]) -> str:
        for key in (
            "contract_under_symbol",
            "under_symbol",
            "underSymbol",
            "ib_under_symbol",
            "primary_underlying",
            "underlying_symbol",
            "underlyingSymbol",
        ):
            root = self._normalize_root_candidate(extras.get(key))
            if root:
                return root

        contract_details = extras.get("contract_details") or extras.get("ib_contract_details")
        for key in ("underSymbol", "under_symbol", "underlyingSymbol", "underlying_symbol"):
            root = self._normalize_root_candidate(self._get_nested_value(contract_details, key))
            if root:
                return root

        contract = extras.get("contract") or self._get_nested_value(contract_details, "contract")
        for key in ("underSymbol", "under_symbol", "underlyingSymbol", "underlying_symbol"):
            root = self._normalize_root_candidate(self._get_nested_value(contract, key))
            if root:
                return root

        return ""

    def resolve_root_symbol(self, symbol: str, *, extras: Optional[Dict[str, Any]] = None) -> str:
        extras = dict(extras or {})
        for key in ("root_symbol", "underlying", "risk_symbol"):
            explicit = self._normalize_root_candidate(extras.get(key))
            if explicit:
                return explicit

        metadata_root = self._metadata_root_symbol(extras)
        if metadata_root:
            return metadata_root

        sym = self._normalize_root_candidate(symbol)
        if sym.startswith("OPT:"):
            parts = sym.split(":")
            if len(parts) >= 2 and parts[1]:
                sym = parts[1].strip().upper()
        return self.root_symbol_overrides.get(sym, sym)

    def classify_action(self, *, extras: Optional[Dict[str, Any]] = None) -> str:
        payload = dict(extras or {})
        if bool(payload.get("flatten_action")):
            return "flatten"
        if bool(payload.get("protective_stop")):
            return "protective_stop"
        if bool(payload.get("close_only")):
            return "close_only"
        return "entry"

    def check_open_allowed(
        self,
        *,
        strategy_id: str,
        symbol: str,
        side: str,
        extras: Optional[Dict[str, Any]] = None,
        now_epoch_s: Optional[float] = None,
    ) -> FlipGuardVerdict:
        ts_now = float(now_epoch_s if now_epoch_s is not None else time.time())
        active_exemption = self._active_exemption(strategy_id=strategy_id, now_epoch_s=ts_now)
        if active_exemption is not None:
            return FlipGuardVerdict(True, "exempt_strategy", active_exemption)
        root_symbol = self.resolve_root_symbol(symbol, extras=extras)
        bar_id = str((extras or {}).get("bar_id") or self._bar_id_from_ts(ts_now))
        current_side = str(side or "").strip().upper()
        last_flat = self._last_flat.get(root_symbol) or {}
        if last_flat:
            flat_bar_id = str(last_flat.get("bar_id") or "")
            flat_ts = float(last_flat.get("ts_epoch_s") or 0.0)
            elapsed = max(0.0, ts_now - flat_ts)
            if self.same_bar_reversal_block and flat_bar_id and flat_bar_id == bar_id:
                return FlipGuardVerdict(
                    False,
                    "same_bar_reversal_block",
                    {
                        "root_symbol": root_symbol,
                        "bar_id": bar_id,
                        "last_flat_ts_epoch_s": flat_ts,
                        "cooldown_sec": self.cooldown_sec,
                    },
                )
            if self.cooldown_sec > 0.0 and elapsed < self.cooldown_sec:
                return FlipGuardVerdict(
                    False,
                    "flip_reopen_cooldown",
                    {
                        "root_symbol": root_symbol,
                        "bar_id": bar_id,
                        "elapsed_sec": round(elapsed, 6),
                        "cooldown_sec": self.cooldown_sec,
                    },
                )
        last_open = self._last_open.get(root_symbol) or {}
        if self.same_bar_reversal_block and last_open:
            last_side = str(last_open.get("side") or "").strip().upper()
            last_bar_id = str(last_open.get("bar_id") or "")
            if last_bar_id and last_bar_id == bar_id and last_side and last_side != current_side:
                return FlipGuardVerdict(
                    False,
                    "same_bar_side_flip",
                    {
                        "root_symbol": root_symbol,
                        "bar_id": bar_id,
                        "last_open_side": last_side,
                        "requested_side": current_side,
                    },
                )
        return FlipGuardVerdict(True, "allowed", {"root_symbol": root_symbol, "bar_id": bar_id})

    def record_submission(
        self,
        *,
        strategy_id: str,
        symbol: str,
        side: str,
        extras: Optional[Dict[str, Any]] = None,
        now_epoch_s: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        ts_now = float(now_epoch_s if now_epoch_s is not None else time.time())
        if self._active_exemption(strategy_id=strategy_id, now_epoch_s=ts_now) is not None:
            return None
        payload = dict(extras or {})
        root_symbol = self.resolve_root_symbol(symbol, extras=payload)
        bar_id = str(payload.get("bar_id") or self._bar_id_from_ts(ts_now))
        action = self.classify_action(extras=payload)
        side_u = str(side or "").strip().upper()
        stored_action = "entry" if action == "entry" else "flat"
        record = {
            "root_symbol": root_symbol,
            "action": stored_action,
            "symbol": str(symbol or "").strip().upper(),
            "side": side_u,
            "strategy_id": str(strategy_id or ""),
            "bar_id": bar_id,
            "ts_epoch_s": ts_now,
            "metadata": {"raw_action": action},
        }
        if action == "entry":
            self._last_open[root_symbol] = {
                "ts_epoch_s": ts_now,
                "bar_id": bar_id,
                "side": side_u,
                "strategy_id": str(strategy_id or ""),
            }
            self._save_state()
            return record
        self._last_flat[root_symbol] = {
            "ts_epoch_s": ts_now,
            "bar_id": bar_id,
            "side": side_u,
            "strategy_id": str(strategy_id or ""),
            "action": action,
        }
        self._save_state()
        return record
