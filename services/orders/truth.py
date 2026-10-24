from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as dt_time, timedelta, timezone
import os
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

from libs.logger import get_logger
from libs.strategy import OrderUpdate, PositionSnapshot

logger = get_logger(__name__)


@dataclass(frozen=True)
class EffectiveOpenOrdersSnapshot:
    strategy_id: str
    source: str
    orders: list[OrderUpdate]
    broker_available: bool
    broker_count: int
    db_count: int


@dataclass(frozen=True)
class EffectivePositionSnapshot:
    strategy_id: str
    symbol: str
    source: str
    position: PositionSnapshot
    broker_available: bool
    broker_qty: Optional[float]
    db_qty: float


@dataclass(frozen=True)
class PublishedOpenOrderTruthSnapshot:
    published_at: datetime
    by_strategy: dict[str, list[OrderUpdate]]


@dataclass(frozen=True)
class PublishedPositionTruthSnapshot:
    published_at: datetime
    ib_nonzero_by_symbol: dict[str, float]
    ledger_nonzero_by_symbol: dict[str, float]
    mismatched_symbols: set[str]


class OrderTruthService:
    """
    Runtime read-side order truth for strategy gating.

    First slice:
    - broker open orders are primary truth when available
    - very recent DB rows are retained as a bounded local submit grace
    - stale DB-only rows are hidden from strategy runtime
    """

    def __init__(
        self,
        *,
        pg: Any,
        broker_open_order_source: Any = None,
        pending_grace_sec: float = 30.0,
        runtime_snapshot_ttl_sec: float = 90.0,
        account_id: Optional[str] = None,
        session_tz: str = "America/New_York",
    ) -> None:
        self.pg = pg
        self.broker_open_order_source = broker_open_order_source
        self.pending_grace_sec = max(0.0, float(pending_grace_sec))
        self.runtime_snapshot_ttl_sec = max(0.0, float(runtime_snapshot_ttl_sec))
        self.account_id = str(account_id or os.getenv("IB_ACCOUNT", "") or "").strip()
        self._session_tz = ZoneInfo(session_tz)
        self._published_open_order_truth: PublishedOpenOrderTruthSnapshot | None = None
        self._published_position_truth: PublishedPositionTruthSnapshot | None = None

    async def effective_open_orders(
        self,
        *,
        strategy_id: str,
        force_refresh: bool = False,
    ) -> EffectiveOpenOrdersSnapshot:
        strategy_id = str(strategy_id or "").strip()
        published = self._published_open_orders_snapshot()
        if published is not None:
            return EffectiveOpenOrdersSnapshot(
                strategy_id=strategy_id,
                source="runtime_reconciled_truth",
                orders=list(published.by_strategy.get(strategy_id, [])),
                broker_available=True,
                broker_count=sum(len(v) for v in published.by_strategy.values()),
                db_count=0,
            )
        db_rows = await self._fetch_db_open_orders(strategy_id=strategy_id)
        broker_orders = await self._fetch_broker_open_orders(force_refresh=force_refresh)
        if broker_orders is None:
            return EffectiveOpenOrdersSnapshot(
                strategy_id=strategy_id,
                source="db_fallback",
                orders=[self._order_update_from_db_row(row) for row in db_rows],
                broker_available=False,
                broker_count=0,
                db_count=len(db_rows),
            )

        db_by_perm: dict[str, dict[str, Any]] = {}
        db_by_ref: dict[str, dict[str, Any]] = {}
        db_by_broker_id: dict[str, dict[str, Any]] = {}
        unmatched_db_rows: list[dict[str, Any]] = []
        for row in db_rows:
            perm_id = self._normalize_perm_id(row.get("ib_perm_id"))
            if perm_id is not None:
                db_by_perm[perm_id] = row
            order_ref = str(row.get("order_ref") or "").strip()
            if order_ref:
                db_by_ref[order_ref] = row
            broker_order_id = self._normalize_broker_order_id(row.get("broker_order_id"))
            if broker_order_id is not None and self._is_current_session_order(row):
                db_by_broker_id[broker_order_id] = row
            unmatched_db_rows.append(row)

        effective_orders: list[OrderUpdate] = []
        matched_order_ids: set[str] = set()
        for broker_order in broker_orders:
            db_row = self._match_db_row_for_broker_order(
                broker_order=broker_order,
                db_by_perm=db_by_perm,
                db_by_ref=db_by_ref,
                db_by_broker_id=db_by_broker_id,
            )
            broker_strategy_id = str(getattr(broker_order, "strategy_id", "") or "").strip()
            matched_strategy_id = str((db_row or {}).get("strategy_id") or broker_strategy_id).strip()
            if matched_strategy_id != strategy_id:
                continue
            if db_row is not None:
                matched_order_ids.add(str(db_row.get("order_id") or ""))
            effective_orders.append(self._order_update_from_broker_order(broker_order=broker_order, db_row=db_row))

        for row in unmatched_db_rows:
            order_id = str(row.get("order_id") or "")
            if order_id in matched_order_ids:
                continue
            if not self._is_within_pending_grace(row):
                continue
            effective_orders.append(self._order_update_from_db_row(row))

        effective_orders.sort(key=lambda order: order.ts or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return EffectiveOpenOrdersSnapshot(
            strategy_id=strategy_id,
            source="broker_plus_local_grace",
            orders=effective_orders,
            broker_available=True,
            broker_count=len(broker_orders),
            db_count=len(db_rows),
        )

    def publish_reconciled_open_orders(
        self,
        *,
        db_orders: Iterable[dict[str, Any]],
        ib_orders: Iterable[Any],
        published_at: Optional[datetime] = None,
    ) -> None:
        rows = [dict(row) for row in (db_orders or [])]
        broker_orders = list(ib_orders or [])
        db_by_perm: dict[str, dict[str, Any]] = {}
        db_by_ref: dict[str, dict[str, Any]] = {}
        db_by_broker_id: dict[str, dict[str, Any]] = {}
        matched_order_ids: set[str] = set()
        by_strategy: dict[str, list[OrderUpdate]] = {}

        for row in rows:
            perm_id = self._normalize_perm_id(row.get("ib_perm_id"))
            if perm_id is not None:
                db_by_perm[perm_id] = row
            order_ref = str(row.get("order_ref") or "").strip()
            if order_ref:
                db_by_ref[order_ref] = row
            broker_order_id = self._normalize_broker_order_id(row.get("broker_order_id"))
            if broker_order_id is not None and self._is_current_session_order(row):
                db_by_broker_id[broker_order_id] = row

        for broker_order in broker_orders:
            db_row = self._match_db_row_for_broker_order(
                broker_order=broker_order,
                db_by_perm=db_by_perm,
                db_by_ref=db_by_ref,
                db_by_broker_id=db_by_broker_id,
            )
            strategy_id = str((db_row or {}).get("strategy_id") or getattr(broker_order, "strategy_id", "") or "").strip()
            if not strategy_id:
                continue
            if db_row is not None:
                matched_order_ids.add(str(db_row.get("order_id") or ""))
            by_strategy.setdefault(strategy_id, []).append(
                self._order_update_from_broker_order(broker_order=broker_order, db_row=db_row)
            )

        for row in rows:
            order_id = str(row.get("order_id") or "")
            if order_id in matched_order_ids:
                continue
            if not self._is_within_pending_grace(row):
                continue
            strategy_id = str(row.get("strategy_id") or "").strip()
            if not strategy_id:
                continue
            by_strategy.setdefault(strategy_id, []).append(self._order_update_from_db_row(row))

        for orders in by_strategy.values():
            orders.sort(key=lambda order: order.ts or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

        ts = published_at or datetime.now(timezone.utc)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        self._published_open_order_truth = PublishedOpenOrderTruthSnapshot(
            published_at=ts.astimezone(timezone.utc),
            by_strategy=by_strategy,
        )

    def _published_open_orders_snapshot(self) -> PublishedOpenOrderTruthSnapshot | None:
        snapshot = self._published_open_order_truth
        if snapshot is None:
            return None
        ttl_sec = float(self.runtime_snapshot_ttl_sec or 0.0)
        if ttl_sec <= 0.0:
            return None
        now_utc = datetime.now(timezone.utc)
        age_sec = max(0.0, (now_utc - snapshot.published_at.astimezone(timezone.utc)).total_seconds())
        if age_sec > ttl_sec:
            return None
        return snapshot

    def _published_position_snapshot(self) -> PublishedPositionTruthSnapshot | None:
        snapshot = self._published_position_truth
        if snapshot is None:
            return None
        ttl_sec = float(self.runtime_snapshot_ttl_sec or 0.0)
        if ttl_sec <= 0.0:
            return None
        now_utc = datetime.now(timezone.utc)
        age_sec = max(0.0, (now_utc - snapshot.published_at.astimezone(timezone.utc)).total_seconds())
        if age_sec > ttl_sec:
            return None
        return snapshot

    async def effective_position(
        self,
        *,
        strategy_id: str,
        symbol: str,
    ) -> EffectivePositionSnapshot:
        strategy_id = str(strategy_id or "").strip()
        symbol = str(symbol or "").strip().upper()
        db_row = await self._fetch_db_position(strategy_id=strategy_id, symbol=symbol)
        if self._is_runtime_published_flat(strategy_id=strategy_id, symbol=symbol):
            return EffectivePositionSnapshot(
                strategy_id=strategy_id,
                symbol=symbol,
                source="runtime_reconciled_flat",
                position=PositionSnapshot(
                    strategy_id=strategy_id,
                    symbol=symbol,
                    pos_qty=0.0,
                    avg_px=None,
                    realized_pnl_usd=0.0,
                    unrealized_pnl_usd=0.0,
                    ts=datetime.now(timezone.utc),
                ),
                broker_available=True,
                broker_qty=0.0,
                db_qty=float((db_row or {}).get("pos_qty") or 0.0),
            )
        broker_qty = await self._fetch_broker_position_qty(symbol=symbol)
        db_qty = float((db_row or {}).get("pos_qty") or 0.0)
        db_ts = self._row_ts(db_row or {})

        if broker_qty is None:
            position = self._position_snapshot_from_db(strategy_id=strategy_id, symbol=symbol, row=db_row)
            return EffectivePositionSnapshot(
                strategy_id=strategy_id,
                symbol=symbol,
                source="db_fallback",
                position=position,
                broker_available=False,
                broker_qty=None,
                db_qty=db_qty,
            )

        use_db_grace = abs(broker_qty) <= 1e-9 and abs(db_qty) > 1e-9 and self._is_timestamp_within_pending_grace(db_ts)
        if use_db_grace:
            position = self._position_snapshot_from_db(strategy_id=strategy_id, symbol=symbol, row=db_row)
            return EffectivePositionSnapshot(
                strategy_id=strategy_id,
                symbol=symbol,
                source="db_recent_grace",
                position=position,
                broker_available=True,
                broker_qty=float(broker_qty),
                db_qty=db_qty,
            )

        position = self._position_snapshot_from_broker(
            strategy_id=strategy_id,
            symbol=symbol,
            broker_qty=float(broker_qty),
            db_row=db_row,
        )
        return EffectivePositionSnapshot(
            strategy_id=strategy_id,
            symbol=symbol,
            source="broker_primary",
            position=position,
            broker_available=True,
            broker_qty=float(broker_qty),
            db_qty=db_qty,
        )

    def publish_reconciled_positions(
        self,
        *,
        ib_positions: dict[str, Any],
        ledger_positions: dict[str, Any],
        mismatches: Iterable[dict[str, Any]],
        published_at: Optional[datetime] = None,
    ) -> None:
        ts = published_at or datetime.now(timezone.utc)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        self._published_position_truth = PublishedPositionTruthSnapshot(
            published_at=ts.astimezone(timezone.utc),
            ib_nonzero_by_symbol={
                str(symbol or "").strip().upper(): float(qty or 0.0)
                for symbol, qty in dict(ib_positions or {}).items()
                if str(symbol or "").strip() and abs(float(qty or 0.0)) > 1e-9
            },
            ledger_nonzero_by_symbol={
                str(symbol or "").strip().upper(): float(qty or 0.0)
                for symbol, qty in dict(ledger_positions or {}).items()
                if str(symbol or "").strip() and abs(float(qty or 0.0)) > 1e-9
            },
            mismatched_symbols={
                str((item or {}).get("symbol") or "").strip().upper()
                for item in (mismatches or [])
                if str((item or {}).get("symbol") or "").strip()
            },
        )

    async def _fetch_db_open_orders(self, *, strategy_id: str) -> list[dict[str, Any]]:
        if self.pg is None:
            return []
        rows = await self.pg.fetchall(
            """
            SELECT *
            FROM core.core_order
            WHERE strategy_id = $1
              AND state IN ('NEW', 'SUBMITTED', 'PARTIAL')
            ORDER BY created_at DESC
            """,
            strategy_id,
        )
        return [dict(row) for row in (rows or [])]

    async def _fetch_db_position(self, *, strategy_id: str, symbol: str) -> Optional[dict[str, Any]]:
        if self.pg is None:
            return None
        row = await self.pg.fetchone(
            """
            SELECT
                COALESCE(SUM(qty), 0) AS pos_qty,
                CASE
                    WHEN COALESCE(SUM(qty), 0) = 0 THEN NULL
                    ELSE ABS(COALESCE(SUM(notional_sum), 0) / NULLIF(SUM(qty), 0))
                END AS avg_px,
                0::numeric AS realized_pnl_usd,
                MAX(last_event_ts) AS updated_at,
                MIN(last_event_ts) AS created_at
            FROM core.position_ledger_current
            WHERE strategy_id = $1
              AND symbol = $2
              AND ($3::text = '' OR account_id = $3)
            """,
            strategy_id,
            symbol,
            self.account_id,
        )
        return dict(row) if row else None

    async def _fetch_broker_open_orders(self, *, force_refresh: bool) -> Optional[list[Any]]:
        source = self.broker_open_order_source
        if source is None:
            return None
        try:
            rows = await source.open_orders(force_refresh=force_refresh)
            return list(rows or [])
        except TypeError:
            rows = await source.open_orders()
            return list(rows or [])
        except Exception as exc:
            logger.warning("order_truth_broker_open_orders_unavailable", error=str(exc))
            return None

    async def _fetch_broker_position_qty(self, *, symbol: str) -> Optional[float]:
        source = self.broker_open_order_source
        if source is None:
            return None
        fn = getattr(source, "position_qty", None)
        if not callable(fn):
            return None
        try:
            qty = await fn(symbol)
            return float(qty)
        except Exception as exc:
            logger.warning("order_truth_broker_position_unavailable", symbol=symbol, error=str(exc))
            return None

    def _match_db_row_for_broker_order(
        self,
        *,
        broker_order: Any,
        db_by_perm: dict[str, dict[str, Any]],
        db_by_ref: dict[str, dict[str, Any]],
        db_by_broker_id: dict[str, dict[str, Any]],
    ) -> Optional[dict[str, Any]]:
        perm_id = self._normalize_perm_id(getattr(broker_order, "perm_id", None))
        if perm_id is not None and perm_id in db_by_perm:
            return db_by_perm[perm_id]
        order_ref = str(getattr(broker_order, "order_ref", "") or "").strip()
        if order_ref and order_ref in db_by_ref:
            return db_by_ref[order_ref]
        broker_order_id = self._normalize_broker_order_id(getattr(broker_order, "broker_order_id", None))
        if broker_order_id is not None:
            return db_by_broker_id.get(broker_order_id)
        return None

    def _order_update_from_broker_order(self, *, broker_order: Any, db_row: Optional[dict[str, Any]]) -> OrderUpdate:
        ts = self._coerce_datetime(getattr(broker_order, "ts_us", None))
        if ts is None and db_row is not None:
            ts = self._row_ts(db_row)
        return OrderUpdate(
            order_id=str((db_row or {}).get("order_id") or getattr(broker_order, "order_id", "")),
            strategy_id=str((db_row or {}).get("strategy_id") or getattr(broker_order, "strategy_id", "") or ""),
            symbol=str((db_row or {}).get("symbol") or getattr(broker_order, "symbol", "") or ""),
            state=str(getattr(broker_order, "status", "") or (db_row or {}).get("state") or "SUBMITTED"),
            order_type=str((db_row or {}).get("order_type") or getattr(broker_order, "order_type", "") or "") or None,
            order_ref=str((db_row or {}).get("order_ref") or getattr(broker_order, "order_ref", "") or "") or None,
            note=(db_row or {}).get("note"),
            stop_px=self._to_float((db_row or {}).get("stop_px"), getattr(broker_order, "stop_px", None)),
            limit_px=self._to_float((db_row or {}).get("limit_px"), getattr(broker_order, "limit_px", None)),
            broker_order_id=str(
                (db_row or {}).get("broker_order_id") or getattr(broker_order, "broker_order_id", "") or ""
            )
            or None,
            ib_perm_id=self._to_int((db_row or {}).get("ib_perm_id"), getattr(broker_order, "perm_id", None)),
            oca_group=str((db_row or {}).get("oca_group") or getattr(broker_order, "oca_group", "") or "") or None,
            last_error=(db_row or {}).get("last_error"),
            ts=ts,
        )

    def _order_update_from_db_row(self, row: dict[str, Any]) -> OrderUpdate:
        return OrderUpdate(
            order_id=str(row.get("order_id") or ""),
            strategy_id=str(row.get("strategy_id") or ""),
            symbol=str(row.get("symbol") or ""),
            state=str(row.get("state") or ""),
            order_type=str(row.get("order_type") or "") or None,
            order_ref=str(row.get("order_ref") or "") or None,
            note=row.get("note"),
            stop_px=self._to_float(row.get("stop_px")),
            limit_px=self._to_float(row.get("limit_px")),
            broker_order_id=str(row.get("broker_order_id") or "") or None,
            ib_perm_id=self._to_int(row.get("ib_perm_id")),
            parent_order_id=str(row.get("parent_order_id") or "") or None,
            oca_group=str(row.get("oca_group") or "") or None,
            last_error=str(row.get("last_error") or "") or None,
            ts=self._row_ts(row),
        )

    def _position_snapshot_from_db(self, *, strategy_id: str, symbol: str, row: Optional[dict[str, Any]]) -> PositionSnapshot:
        if not row or abs(float(row.get("pos_qty") or 0.0)) <= 1e-9:
            return PositionSnapshot(
                strategy_id=strategy_id,
                symbol=symbol,
                pos_qty=0.0,
                avg_px=None,
                realized_pnl_usd=0.0,
                unrealized_pnl_usd=0.0,
                ts=datetime.now(timezone.utc),
            )
        return PositionSnapshot(
            strategy_id=strategy_id,
            symbol=symbol,
            pos_qty=float(row.get("pos_qty") or 0.0),
            avg_px=self._to_float(row.get("avg_px")),
            realized_pnl_usd=float(row.get("realized_pnl_usd") or 0.0),
            unrealized_pnl_usd=0.0,
            ts=self._row_ts(row),
        )

    def _position_snapshot_from_broker(
        self,
        *,
        strategy_id: str,
        symbol: str,
        broker_qty: float,
        db_row: Optional[dict[str, Any]],
    ) -> PositionSnapshot:
        avg_px = self._to_float((db_row or {}).get("avg_px")) if abs(broker_qty) > 1e-9 else None
        return PositionSnapshot(
            strategy_id=strategy_id,
            symbol=symbol,
            pos_qty=float(broker_qty),
            avg_px=avg_px,
            realized_pnl_usd=float((db_row or {}).get("realized_pnl_usd") or 0.0),
            unrealized_pnl_usd=0.0,
            ts=self._row_ts(db_row or {}) or datetime.now(timezone.utc),
        )

    def _is_runtime_published_flat(self, *, strategy_id: str, symbol: str) -> bool:
        snapshot = self._published_position_snapshot()
        if snapshot is None:
            return False
        symbol_u = str(symbol or "").strip().upper()
        if not symbol_u:
            return False
        if symbol_u in snapshot.mismatched_symbols:
            return False
        if symbol_u in snapshot.ib_nonzero_by_symbol or symbol_u in snapshot.ledger_nonzero_by_symbol:
            return False
        open_orders = self._published_open_orders_snapshot()
        if open_orders is not None:
            for order in open_orders.by_strategy.get(str(strategy_id or "").strip(), []):
                if str(order.symbol or "").strip().upper() == symbol_u:
                    return False
        return True

    def _is_within_pending_grace(self, row: dict[str, Any]) -> bool:
        return self._is_timestamp_within_pending_grace(self._row_ts(row))

    def _is_timestamp_within_pending_grace(self, ts: Optional[datetime]) -> bool:
        if self.pending_grace_sec <= 0:
            return False
        if ts is None:
            return True
        return (datetime.now(timezone.utc) - ts) <= timedelta(seconds=self.pending_grace_sec)

    def _is_current_session_order(self, row: dict[str, Any]) -> bool:
        session_start = datetime.combine(datetime.now(self._session_tz).date(), dt_time.min, tzinfo=self._session_tz)
        ts = self._row_ts(row)
        if ts is None:
            return True
        return ts >= session_start.astimezone(timezone.utc)

    def _row_ts(self, row: dict[str, Any]) -> Optional[datetime]:
        return self._coerce_datetime(row.get("updated_at")) or self._coerce_datetime(row.get("created_at"))

    @staticmethod
    def _normalize_perm_id(value: Any) -> Optional[str]:
        try:
            parsed = int(value)
        except Exception:
            return None
        return str(parsed) if parsed > 0 else None

    @staticmethod
    def _normalize_broker_order_id(value: Any) -> Optional[str]:
        raw = str(value or "").strip()
        return raw or None

    @staticmethod
    def _coerce_datetime(value: Any) -> Optional[datetime]:
        if value is None:
            return None
        if isinstance(value, datetime):
            dt = value
            return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        if isinstance(value, (int, float)):
            if value > 1_000_000_000_000:
                return datetime.fromtimestamp(float(value) / 1_000_000.0, tz=timezone.utc)
            if value > 0:
                return datetime.fromtimestamp(float(value), tz=timezone.utc)
        if isinstance(value, str) and value.strip():
            raw = value.strip()
            if raw.endswith("Z"):
                raw = raw[:-1] + "+00:00"
            try:
                parsed = datetime.fromisoformat(raw)
            except ValueError:
                return None
            return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        return None

    @staticmethod
    def _to_float(*values: Any) -> Optional[float]:
        for value in values:
            if value in (None, ""):
                continue
            try:
                return float(value)
            except Exception:
                continue
        return None

    @staticmethod
    def _to_int(*values: Any) -> Optional[int]:
        for value in values:
            if value in (None, ""):
                continue
            try:
                return int(value)
            except Exception:
                continue
        return None
