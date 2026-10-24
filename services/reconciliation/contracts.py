from __future__ import annotations

import inspect
import os
import time
from dataclasses import dataclass
from datetime import datetime, time as dt_time, timezone
from typing import Any, Dict
from zoneinfo import ZoneInfo

from .runtime_reconciler import ReconciliationReport


@dataclass
class ReconcilerComponentResult:
    component: str
    ok: bool
    reason: str
    metadata: Dict[str, Any]


@dataclass
class OrderTruthSnapshot:
    db_orders: list[dict[str, Any]]
    ib_orders: list[Any]
    ib_by_id: dict[Any, Any]
    ib_by_perm_id: dict[str, Any]
    db_by_broker_id: dict[Any, dict[str, Any]]
    db_by_perm_id: dict[str, dict[str, Any]]
    ghosts: list[dict[str, Any]]
    orphans: list[Any]
    mismatches: list[tuple[dict[str, Any], Any]]


class StartupReconciler:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    async def ensure_ready(self, *, raise_gate: bool = True) -> ReconcilerComponentResult:
        order_result = await self.runtime.order_truth_reconciler.reconcile_once(apply_runtime_filters=True)
        position_result = await self.runtime.position_reconciler.reconcile_once()
        active_quarantine = await self.runtime.order_quarantine_handler.active_quarantine_summary()
        order_meta = order_result.metadata
        position_mismatches = int(position_result.metadata.get("mismatch_count") or 0)
        active_quarantine_count = int(active_quarantine.get("active_count") or 0)
        report = ReconciliationReport(
            timestamp=None,
            db_open_count=int(order_meta.get("db_open_count") or 0),
            ib_open_count=int(order_meta.get("ib_open_count") or 0),
            ghosts_found=int(order_meta.get("ghosts_found") or 0),
            orphans_found=int(order_meta.get("orphans_found") or 0),
            mismatches=int(order_meta.get("mismatches") or 0),
            duration_ms=0.0,
            position_mismatches=position_mismatches,
        )
        if raise_gate:
            self.runtime._raise_startup_gate_failure(report=report)
            if active_quarantine_count > 0:
                raise RuntimeError(
                    "runtime_reconciler_startup_blocked:active_quarantine:"
                    f"active_quarantine_count={active_quarantine_count}"
                )
        return ReconcilerComponentResult(
            component="startup",
            ok=bool(order_result.ok and position_result.ok and active_quarantine_count <= 0),
            reason=(
                "startup_reconcile_clean"
                if order_result.ok and position_result.ok and active_quarantine_count <= 0
                else "startup_reconcile_diverged"
            ),
            metadata={
                "ghosts_found": int(report.ghosts_found or 0),
                "orphans_found": int(report.orphans_found or 0),
                "mismatches": int(report.mismatches or 0),
                "position_mismatches": int(report.position_mismatches or 0),
                "active_quarantine_count": active_quarantine_count,
                "order_truth": order_meta,
                "position": position_result.metadata,
                "active_quarantine": active_quarantine,
            },
        )


class OrderTruthReconciler:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self._session_tz = ZoneInfo("America/New_York")

    async def collect_snapshot(self, *, apply_runtime_filters: bool = False) -> OrderTruthSnapshot:
        db_orders = await self.runtime._fetch_db_open_orders()
        ib_orders = await self.runtime._fetch_ib_open_orders(force_refresh=True)

        ib_by_id: dict[Any, Any] = {}
        ib_by_perm_id: dict[str, Any] = {}
        for order in ib_orders or []:
            broker_id = self.runtime._normalize_broker_order_id(getattr(order, "order_id", None))
            if broker_id is not None:
                ib_by_id[broker_id] = order
            perm_id = self.runtime._normalize_perm_id(getattr(order, "perm_id", None))
            if perm_id is not None:
                ib_by_perm_id[perm_id] = order

        db_by_broker_id: dict[Any, dict[str, Any]] = {}
        db_by_perm_id: dict[str, dict[str, Any]] = {}
        for order in db_orders or []:
            broker_id = self.runtime._normalize_broker_order_id((order or {}).get("broker_order_id"))
            if broker_id is not None and self._is_current_session_order(order):
                db_by_broker_id[broker_id] = order
            perm_id = self.runtime._normalize_perm_id((order or {}).get("ib_perm_id"))
            if perm_id is not None:
                db_by_perm_id[perm_id] = order

        ghosts: list[dict[str, Any]] = []
        for broker_id, db_order in db_by_broker_id.items():
            if apply_runtime_filters:
                suppress_key = self.runtime._suppress_key_from_broker_order_id(broker_id)
                suppress_until = self.runtime._ghost_suppress_until.get(suppress_key, 0.0) if suppress_key is not None else 0.0
                if suppress_until > time.time():
                    continue
            db_perm_id = self.runtime._normalize_perm_id((db_order or {}).get("ib_perm_id"))
            missing_by_id = broker_id not in ib_by_id
            missing_by_perm = db_perm_id is None or db_perm_id not in ib_by_perm_id
            stale_enough = True
            if apply_runtime_filters:
                stale_enough = self.runtime._is_order_stale(db_order, self.runtime.ghost_grace_seconds)
            if missing_by_id and missing_by_perm and stale_enough:
                ghosts.append(db_order)

        orphans: list[Any] = []
        for broker_id, ib_order in ib_by_id.items():
            if apply_runtime_filters:
                suppress_key = self.runtime._suppress_key_from_broker_order_id(broker_id)
                suppress_until = self.runtime._orphan_suppress_until.get(suppress_key, 0.0) if suppress_key is not None else 0.0
                if suppress_until > time.time():
                    continue
            if self.runtime._is_unconfirmed_pending_ib_order(ib_order):
                continue
            ib_perm_id = self.runtime._normalize_perm_id(getattr(ib_order, "perm_id", None))
            if broker_id not in db_by_broker_id and (ib_perm_id is None or ib_perm_id not in db_by_perm_id):
                orphans.append(ib_order)

        mismatches: list[tuple[dict[str, Any], Any]] = []
        matched_pairs: dict[tuple[str, str], tuple[dict[str, Any], Any]] = {}
        for perm_id in sorted(set(db_by_perm_id.keys()) & set(ib_by_perm_id.keys()), key=str):
            db_order = db_by_perm_id[perm_id]
            ib_order = ib_by_perm_id[perm_id]
            matched_pairs[(str(db_order.get("order_id") or ""), str(getattr(ib_order, "order_id", "") or ""))] = (
                db_order,
                ib_order,
            )
        for broker_id in sorted(set(db_by_broker_id.keys()) & set(ib_by_id.keys()), key=str):
            db_order = db_by_broker_id[broker_id]
            ib_order = ib_by_id[broker_id]
            matched_pairs.setdefault(
                (str(db_order.get("order_id") or ""), str(getattr(ib_order, "order_id", "") or "")),
                (db_order, ib_order),
            )

        for db_order, ib_order in matched_pairs.values():
            if self.runtime._state_mismatch(str(db_order.get("state") or ""), str(getattr(ib_order, "status", "") or "")):
                mismatches.append((db_order, ib_order))

        return OrderTruthSnapshot(
            db_orders=list(db_orders or []),
            ib_orders=list(ib_orders or []),
            ib_by_id=ib_by_id,
            ib_by_perm_id=ib_by_perm_id,
            db_by_broker_id=db_by_broker_id,
            db_by_perm_id=db_by_perm_id,
            ghosts=ghosts,
            orphans=orphans,
            mismatches=mismatches,
        )

    def _is_current_session_order(self, order: dict[str, Any]) -> bool:
        """IB order_id is not globally stable; only trust it for current-session DB rows."""
        session_start_fn = getattr(self.runtime, "current_order_session_start", None)
        if callable(session_start_fn):
            session_start = session_start_fn()
        else:
            now_et = datetime.now(self._session_tz)
            session_start = datetime.combine(now_et.date(), dt_time.min, tzinfo=self._session_tz)

        if session_start.tzinfo is None:
            session_start = session_start.replace(tzinfo=self._session_tz)

        for field in ("updated_at", "created_at", "submitted_at"):
            raw = (order or {}).get(field)
            ts = self._coerce_datetime(raw)
            if ts is None:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=self._session_tz)
            return ts.astimezone(timezone.utc) >= session_start.astimezone(timezone.utc)
        return True

    @staticmethod
    def _coerce_datetime(value: Any) -> datetime | None:
        if isinstance(value, datetime):
            return value
        if isinstance(value, str) and value.strip():
            raw = value.strip()
            if raw.endswith("Z"):
                raw = raw[:-1] + "+00:00"
            try:
                return datetime.fromisoformat(raw)
            except ValueError:
                return None
        return None

    async def reconcile_once(self, *, apply_runtime_filters: bool = False) -> ReconcilerComponentResult:
        snapshot = await self.collect_snapshot(apply_runtime_filters=apply_runtime_filters)
        ghosts = snapshot.ghosts
        orphans = snapshot.orphans
        mismatches = snapshot.mismatches
        ok = not ghosts and not orphans and not mismatches
        return ReconcilerComponentResult(
            component="order_truth",
            ok=ok,
            reason="order_truth_clean" if ok else "order_truth_diverged",
            metadata={
                "db_open_count": len(snapshot.db_orders),
                "ib_open_count": len(snapshot.ib_orders),
                "ghosts_found": len(ghosts),
                "orphans_found": len(orphans),
                "mismatches": len(mismatches),
                "ghost_samples": [self._db_order_sample(order) for order in ghosts[:20]],
                "orphan_samples": [self._ib_order_sample(order) for order in orphans[:20]],
                "mismatch_samples": [self._mismatch_sample(db_order, ib_order) for db_order, ib_order in mismatches[:20]],
            },
        )

    @staticmethod
    def _db_order_sample(order: dict[str, Any]) -> dict[str, Any]:
        return {
            "order_id": str((order or {}).get("order_id") or ""),
            "broker_order_id": str((order or {}).get("broker_order_id") or ""),
            "perm_id": str((order or {}).get("ib_perm_id") or ""),
            "strategy_id": str((order or {}).get("strategy_id") or ""),
            "symbol": str((order or {}).get("symbol") or ""),
            "state": str((order or {}).get("state") or ""),
        }

    @staticmethod
    def _ib_order_sample(order: Any) -> dict[str, Any]:
        return {
            "order_id": str(getattr(order, "order_id", "") or ""),
            "perm_id": str(getattr(order, "perm_id", "") or ""),
            "symbol": str(getattr(order, "symbol", "") or ""),
            "status": str(getattr(order, "status", "") or ""),
            "order_ref": str(getattr(order, "order_ref", "") or ""),
        }

    def _mismatch_sample(self, db_order: dict[str, Any], ib_order: Any) -> dict[str, Any]:
        sample = self._db_order_sample(db_order)
        sample.update(
            {
                "ib_order_id": str(getattr(ib_order, "order_id", "") or ""),
                "ib_perm_id": str(getattr(ib_order, "perm_id", "") or ""),
                "ib_status": str(getattr(ib_order, "status", "") or ""),
            }
        )
        return sample


class PositionReconciler:
    def __init__(self, runtime: Any, *, account_id: str | None = None, tolerance: float = 0.001) -> None:
        self.runtime = runtime
        self.account_id = str(account_id or os.getenv("IB_ACCOUNT") or "").strip()
        self.tolerance = float(tolerance)
        self._last_ledger_lots: list[dict[str, Any]] = []

    async def reconcile_once(self) -> ReconcilerComponentResult:
        ib_map = await self._fetch_ib_positions()
        ledger_map = await self._fetch_ledger_positions()
        mismatches: list[dict[str, Any]] = []
        for symbol in sorted(set(ib_map.keys()) | set(ledger_map.keys())):
            ib_qty = float(ib_map.get(symbol, 0.0))
            ledger_qty = float(ledger_map.get(symbol, 0.0))
            diff = ib_qty - ledger_qty
            if abs(diff) > self.tolerance:
                mismatch = {
                    "symbol": symbol,
                    "ib_qty": ib_qty,
                    "ledger_qty": ledger_qty,
                    "diff": diff,
                }
                owner_lane = await self._resolve_owner_lane(symbol)
                if owner_lane:
                    mismatch.update(owner_lane)
                    mismatch["owner_strategy_id"] = owner_lane["strategy_id"]
                    mismatch["owner_strategy_source"] = "ledger_owner_lane"
                owner = await self._resolve_owner_strategy_id(symbol, broker_qty=ib_qty)
                if owner:
                    mismatch.setdefault("strategy_id", owner)
                    mismatch.setdefault("owner_strategy_id", owner)
                    mismatch.setdefault("owner_strategy_source", self._owner_source(symbol, owner))
                mismatches.append(mismatch)
        ok = len(mismatches) == 0
        return ReconcilerComponentResult(
            component="position",
            ok=ok,
            reason="position_ledger_clean" if ok else "position_ledger_diverged",
            metadata={
                "account_id": self.account_id,
                "tolerance": self.tolerance,
                "ib_positions": ib_map,
                "ledger_positions": ledger_map,
                "ledger_lots": self._last_ledger_lots[:500],
                "ledger_lot_count_by_symbol": self._ledger_lot_count_by_symbol(),
                "mismatch_count": len(mismatches),
                "mismatches": mismatches[:100],
            },
        )

    async def _fetch_ib_positions(self) -> Dict[str, float]:
        adapter = self.runtime.adapter
        if hasattr(adapter, "positions"):
            positions = adapter.positions()
            if inspect.isawaitable(positions):
                positions = await positions
        elif hasattr(adapter, "ib"):
            positions = adapter.ib.positions()
            if inspect.isawaitable(positions):
                positions = await positions
        else:
            return {}
        out: Dict[str, float] = {}
        for position in positions or []:
            try:
                symbol = str(self.runtime._ib_position_symbol(position) or "").strip().upper()
                if not symbol:
                    continue
                qty = float(getattr(position, "position", 0.0) or 0.0)
            except Exception:
                continue
            if abs(qty) <= 0.0:
                continue
            out[symbol] = float(out.get(symbol, 0.0) + qty)
        return out

    async def _fetch_ledger_positions(self) -> Dict[str, float]:
        query = """
            SELECT
                symbol,
                COALESCE(account_id, '') AS account_id,
                COALESCE(strategy_id, '') AS strategy_id,
                COALESCE(strategy_instance_id, '') AS strategy_instance_id,
                COALESCE(lot_id, '') AS lot_id,
                COALESCE(position_owner_key, '') AS position_owner_key,
                COALESCE(SUM(qty), 0) AS qty
            FROM core.position_ledger_current
            WHERE ($1::text = '' OR account_id = $1)
            GROUP BY symbol, account_id, strategy_id, strategy_instance_id, lot_id, position_owner_key
            HAVING ABS(COALESCE(SUM(qty), 0)) > 0.000001
        """
        fetchall = getattr(self.runtime.pg, "fetchall", None) or getattr(self.runtime.pg, "fetch", None)
        if fetchall is None:
            return {}
        rows = fetchall(query, self.account_id)
        if inspect.isawaitable(rows):
            rows = await rows
        out: Dict[str, float] = {}
        lots: list[dict[str, Any]] = []
        for row in rows or []:
            try:
                if hasattr(row, "get"):
                    symbol_raw = row.get("symbol")
                    account_id_raw = row.get("account_id")
                    strategy_id_raw = row.get("strategy_id")
                    strategy_instance_id_raw = row.get("strategy_instance_id")
                    lot_id_raw = row.get("lot_id")
                    position_owner_key_raw = row.get("position_owner_key")
                    qty_raw = row.get("qty")
                else:
                    symbol_raw = row[0]
                    if len(row) >= 7:
                        account_id_raw = row[1]
                        strategy_id_raw = row[2]
                        strategy_instance_id_raw = row[3]
                        lot_id_raw = row[4]
                        position_owner_key_raw = row[5]
                        qty_raw = row[6]
                    elif len(row) >= 4:
                        account_id_raw = ""
                        strategy_id_raw = row[1]
                        strategy_instance_id_raw = ""
                        lot_id_raw = row[2]
                        position_owner_key_raw = ""
                        qty_raw = row[3]
                    else:
                        account_id_raw = ""
                        strategy_id_raw = ""
                        strategy_instance_id_raw = ""
                        lot_id_raw = ""
                        position_owner_key_raw = ""
                        qty_raw = row[1]
                symbol = str(symbol_raw or "").strip().upper()
                account_id = str(account_id_raw or "").strip()
                strategy_id = str(strategy_id_raw or "").strip()
                strategy_instance_id = str(strategy_instance_id_raw or "").strip()
                lot_id = str(lot_id_raw or "").strip()
                position_owner_key = str(position_owner_key_raw or "").strip()
                qty = float(qty_raw or 0.0)
            except Exception:
                continue
            if symbol:
                out[symbol] = float(out.get(symbol, 0.0) + qty)
                lots.append(
                    {
                        "symbol": symbol,
                        "account_id": account_id,
                        "strategy_id": strategy_id,
                        "strategy_instance_id": strategy_instance_id,
                        "lot_id": lot_id,
                        "position_owner_key": position_owner_key,
                        "qty": qty,
                    }
                )
        self._last_ledger_lots = lots
        return out

    async def _resolve_owner_lane(self, symbol: str) -> dict[str, Any]:
        sym = str(symbol or "").strip().upper()
        if not sym:
            return {}
        lanes = [
            dict(lot or {})
            for lot in self._last_ledger_lots
            if str((lot or {}).get("symbol") or "").strip().upper() == sym
            and abs(float((lot or {}).get("qty") or 0.0)) > self.tolerance
        ]
        if len(lanes) != 1:
            return {}
        lane = lanes[0]
        account_id = str(lane.get("account_id") or self.account_id or "").strip()
        strategy_id = str(lane.get("strategy_id") or "").strip()
        strategy_instance_id = str(lane.get("strategy_instance_id") or "").strip()
        lot_id = str(lane.get("lot_id") or "").strip()
        position_owner_key = str(lane.get("position_owner_key") or "").strip()
        if not (account_id and strategy_id and strategy_instance_id and lot_id and position_owner_key):
            return {}
        return {
            "account_id": account_id,
            "strategy_id": strategy_id,
            "strategy_instance_id": strategy_instance_id,
            "lot_id": lot_id,
            "position_lot_id": lot_id,
            "position_owner_key": position_owner_key,
            "owner_lane_qty": float(lane.get("qty") or 0.0),
        }

    def _ledger_lot_count_by_symbol(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for lot in self._last_ledger_lots:
            symbol = str(lot.get("symbol") or "").strip().upper()
            if symbol:
                counts[symbol] = int(counts.get(symbol, 0) + 1)
        return counts

    async def _resolve_owner_strategy_id(self, symbol: str, *, broker_qty: float = 0.0) -> str:
        sym = str(symbol or "").strip().upper()
        if not sym:
            return ""
        lot_strategy_ids = {
            str((lot or {}).get("strategy_id") or "").strip()
            for lot in self._last_ledger_lots
            if str((lot or {}).get("symbol") or "").strip().upper() == sym
            and str((lot or {}).get("strategy_id") or "").strip()
            and str((lot or {}).get("account_id") or "").strip()
            and str((lot or {}).get("strategy_instance_id") or "").strip()
            and str((lot or {}).get("lot_id") or "").strip()
            and str((lot or {}).get("position_owner_key") or "").strip()
        }
        if len(lot_strategy_ids) == 1:
            return next(iter(lot_strategy_ids))
        if len(lot_strategy_ids) > 1:
            return ""
        configured = await self._resolve_configured_owner(sym)
        if configured:
            return configured
        pg = getattr(self.runtime, "pg", None)
        fetchall = getattr(pg, "fetchall", None) or getattr(pg, "fetch", None)
        if fetchall is None:
            return ""
        query = """
            SELECT strategy_id
            FROM core.core_order
            WHERE symbol = $1
              AND state IN ('NEW', 'SUBMITTED', 'PARTIAL')
              AND strategy_id IS NOT NULL
              AND btrim(strategy_id) <> ''
              AND strategy_id <> 'adopted_reconciler'
              AND (
                COALESCE(order_ref, '') ILIKE '%RECOVERY%'
                OR COALESCE(order_ref, '') ILIKE '%FLATTEN%'
                OR COALESCE(order_ref, '') ILIKE '%PROTECT%'
                OR COALESCE(order_ref, '') ILIKE '%STOP%'
                OR COALESCE(note::text, '') LIKE '%runtime_recovery_action%'
                OR COALESCE(note::text, '') LIKE '%flatten_action%'
                OR COALESCE(note::text, '') LIKE '%protective_stop%'
                OR COALESCE(note::text, '') LIKE '%close_only%'
              )
            ORDER BY updated_at DESC NULLS LAST
        """
        try:
            rows = fetchall(query, sym)
            if inspect.isawaitable(rows):
                rows = await rows
        except Exception:
            return ""
        owners = set()
        for row in rows or []:
            if hasattr(row, "get"):
                owner = str(row.get("strategy_id") or "").strip()
            else:
                try:
                    owner = str(row[0] or "").strip()
                except Exception:
                    owner = ""
            if owner:
                owners.add(owner)
        if len(owners) == 1:
            return next(iter(owners))
        if len(owners) > 1:
            return ""
        exact_option_owner = await self._resolve_exact_option_order_owner(sym, broker_qty=broker_qty)
        if exact_option_owner:
            return exact_option_owner
        return ""

    async def _resolve_exact_option_order_owner(self, symbol: str, *, broker_qty: float = 0.0) -> str:
        sym = str(symbol or "").strip().upper()
        if not sym.startswith("OPT:"):
            return ""
        pg = getattr(self.runtime, "pg", None)
        fetchall = getattr(pg, "fetchall", None) or getattr(pg, "fetch", None)
        if fetchall is None:
            return ""
        expected_side = "BUY" if float(broker_qty or 0.0) > 0 else "SELL"
        query = """
            SELECT strategy_id
            FROM core.core_order
            WHERE symbol = $1
              AND strategy_id IS NOT NULL
              AND btrim(strategy_id) <> ''
              AND strategy_id <> 'adopted_reconciler'
              AND COALESCE(state, '') NOT IN ('REJECTED', 'EXPIRED', 'FAILED')
              AND ($2::text = '' OR COALESCE(side, '') = $2)
              AND created_at >= now() - interval '14 days'
            ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST
            LIMIT 50
        """
        try:
            rows = fetchall(query, sym, expected_side)
            if inspect.isawaitable(rows):
                rows = await rows
        except Exception:
            return ""
        owners = set()
        for row in rows or []:
            if hasattr(row, "get"):
                owner = str(row.get("strategy_id") or "").strip()
            else:
                try:
                    owner = str(row[0] or "").strip()
                except Exception:
                    owner = ""
            if owner:
                owners.add(owner)
        if len(owners) == 1:
            return next(iter(owners))
        return ""

    async def _resolve_configured_owner(self, symbol: str) -> str:
        sym = str(symbol or "").strip().upper()
        provider = getattr(self.runtime, "recovery_strategy_owner_provider", None)
        if callable(provider):
            try:
                owner = provider(sym)
                if inspect.isawaitable(owner):
                    owner = await owner
                owner_s = str(owner or "").strip()
                if owner_s:
                    return owner_s
            except Exception:
                return ""
        mapping = getattr(self.runtime, "symbol_strategy_owner_map", None)
        if isinstance(mapping, dict):
            return str(mapping.get(sym) or mapping.get(sym.lower()) or "").strip()
        return ""

    def _owner_source(self, symbol: str, strategy_id: str) -> str:
        sym = str(symbol or "").strip().upper()
        sid = str(strategy_id or "").strip()
        if any(
            str((lot or {}).get("symbol") or "").strip().upper() == sym
            and str((lot or {}).get("strategy_id") or "").strip() == sid
            and str((lot or {}).get("account_id") or "").strip()
            and str((lot or {}).get("strategy_instance_id") or "").strip()
            and str((lot or {}).get("lot_id") or "").strip()
            and str((lot or {}).get("position_owner_key") or "").strip()
            for lot in self._last_ledger_lots
        ):
            return "ledger_lot"
        mapping = getattr(self.runtime, "symbol_strategy_owner_map", None)
        if isinstance(mapping, dict) and str(mapping.get(sym) or mapping.get(sym.lower()) or "").strip() == sid:
            return "configured_owner"
        if sym.startswith("OPT:"):
            return "exact_option_order"
        return "working_order"


class AckReconciler:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    async def acknowledge_order_quarantine(self, **kwargs: Any) -> ReconcilerComponentResult:
        ok = await self.runtime.acknowledge_order_quarantine(**kwargs)
        return ReconcilerComponentResult(
            component="ack",
            ok=bool(ok),
            reason="quarantine_acknowledged" if ok else "quarantine_ack_not_applied",
            metadata=dict(kwargs),
        )
