"""
Central data-availability gate for production readiness.

The contract is intentionally simple: data outages can block entries and mark
runtime health degraded, but they must never be used as a reason to block
cancel/protection/flatten/EOD safety actions.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, time
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Dict, Iterable, Optional
from zoneinfo import ZoneInfo

from libs.logger import get_logger

logger = get_logger(__name__)
ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class DataAvailabilityGate:
    dataset: str
    symbol: str
    resolution: str
    reason: str
    source: str = ""
    strategy_id: str = ""
    stage: str = ""
    severity: str = "entry_blocking"
    rth: bool = False
    error: str = ""
    first_seen: str = ""
    last_seen: str = ""


class DataAvailabilityService:
    def __init__(self, *, gate_ttl_sec: float = 180.0, durable_state_path: str | Path | None = None) -> None:
        self._lock = RLock()
        self._gates: dict[tuple[str, str, str, str, str], DataAvailabilityGate] = {}
        self._gate_ttl_sec = max(0.0, float(gate_ttl_sec))
        self._durable_state_path = Path(durable_state_path) if durable_state_path else None
        self._required_data_keys: dict[tuple[str, str], set[bool]] = {}
        self._required_strategy_ids: set[str] = set()
        self._required_strategy_rth_flags: dict[str, set[bool]] = {}
        self._listeners: list[Callable[[Dict[str, Any]], None]] = []
        self._audit_writer: Optional[Any] = None
        self._durable_state_write_failed = False
        self._durable_state_error = ""
        self._load_durable_state()

    def set_durable_state_path(self, path: str | Path | None) -> None:
        with self._lock:
            self._durable_state_path = Path(path) if path else None
            self._gates = {}
            self._load_durable_state_locked()

    def set_audit_writer(self, audit_writer: Optional[Any]) -> None:
        self._audit_writer = audit_writer

    def add_listener(self, listener: Callable[[Dict[str, Any]], None]) -> None:
        if not callable(listener):
            return
        with self._lock:
            if listener not in self._listeners:
                self._listeners.append(listener)

    def remove_listener(self, listener: Callable[[Dict[str, Any]], None]) -> None:
        with self._lock:
            self._listeners = [item for item in self._listeners if item != listener]

    @staticmethod
    def is_rth(now: Optional[datetime] = None) -> bool:
        now_et = now.astimezone(ET) if now else datetime.now(ET)
        return now_et.weekday() < 5 and time(9, 30) <= now_et.time() <= time(16, 0)

    def mark_unavailable(
        self,
        *,
        dataset: str,
        symbol: str,
        resolution: str = "",
        reason: str,
        source: str = "",
        strategy_id: str = "",
        stage: str = "",
        severity: str = "entry_blocking",
        error: str = "",
        now: Optional[datetime] = None,
    ) -> DataAvailabilityGate:
        now_et = now.astimezone(ET) if now else datetime.now(ET)
        ts = now_et.isoformat()
        key = (
            str(strategy_id or ""),
            str(dataset or "").strip(),
            str(symbol or "").strip().upper(),
            str(resolution or "").strip(),
            str(stage or "").strip(),
        )
        with self._lock:
            previous = self._gates.get(key)
            gate = DataAvailabilityGate(
                dataset=key[1],
                symbol=key[2],
                resolution=key[3],
                reason=str(reason or "data_unavailable"),
                source=str(source or ""),
                strategy_id=key[0],
                stage=key[4],
                severity=str(severity or "entry_blocking"),
                rth=self.is_rth(now_et),
                error=str(error or ""),
                first_seen=previous.first_seen if previous else ts,
                last_seen=ts,
            )
            self._gates[key] = gate
            listeners = list(self._listeners)
            self._write_durable_state_locked()
        self._emit_event(
            event_type="data_unavailable",
            action="mark_unavailable",
            gate=gate,
            previous=previous,
            listeners=listeners,
            ts=ts,
        )
        return gate

    def clear(
        self,
        *,
        dataset: str,
        symbol: str,
        resolution: str = "",
        strategy_id: str = "",
        stage: str = "",
    ) -> None:
        key = (
            str(strategy_id or ""),
            str(dataset or "").strip(),
            str(symbol or "").strip().upper(),
            str(resolution or "").strip(),
            str(stage or "").strip(),
        )
        with self._lock:
            previous = self._gates.pop(key, None)
            listeners = list(self._listeners)
            if previous is not None:
                self._write_durable_state_locked()
        if previous is not None:
            self._emit_event(
                event_type="data_available",
                action="clear",
                gate=previous,
                previous=previous,
                listeners=listeners,
                ts=datetime.now(ET).isoformat(),
            )

    def clear_symbol(self, *, dataset: str, symbol: str, resolution: str = "") -> None:
        dataset_s = str(dataset or "").strip()
        symbol_s = str(symbol or "").strip().upper()
        resolution_s = str(resolution or "").strip()
        cleared: list[DataAvailabilityGate] = []
        with self._lock:
            for key in list(self._gates.keys()):
                _, dataset_k, symbol_k, resolution_k, _ = key
                if dataset_k == dataset_s and symbol_k == symbol_s and (
                    not resolution_s or resolution_k == resolution_s
                ):
                    gate = self._gates.pop(key, None)
                    if gate is not None:
                        cleared.append(gate)
            listeners = list(self._listeners)
            if cleared:
                self._write_durable_state_locked()
        ts = datetime.now(ET).isoformat()
        for gate in cleared:
            self._emit_event(
                event_type="data_available",
                action="clear_symbol",
                gate=gate,
                previous=gate,
                listeners=listeners,
                ts=ts,
            )

    def _is_gate_expired(self, gate: DataAvailabilityGate, *, now: datetime) -> bool:
        if self._gate_ttl_sec <= 0:
            return False
        try:
            last_seen = datetime.fromisoformat(str(gate.last_seen or ""))
            if last_seen.tzinfo is None:
                last_seen = last_seen.replace(tzinfo=ET)
            age_sec = (now.astimezone(ET) - last_seen.astimezone(ET)).total_seconds()
        except Exception:
            return False
        return age_sec > self._gate_ttl_sec

    def _prune_expired_locked(self, *, now: datetime) -> list[DataAvailabilityGate]:
        expired: list[DataAvailabilityGate] = []
        if self._gate_ttl_sec <= 0:
            return expired
        for key, gate in list(self._gates.items()):
            if self._is_gate_expired(gate, now=now):
                removed = self._gates.pop(key, None)
                if removed is not None:
                    expired.append(removed)
        if expired:
            self._write_durable_state_locked()
        return expired

    def _load_durable_state(self) -> None:
        with self._lock:
            self._load_durable_state_locked()

    def _load_durable_state_locked(self) -> None:
        path = self._durable_state_path
        if path is None or not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("data_availability_durable_state_load_failed", path=str(path), exc_info=True)
            return
        gates = payload.get("gates") if isinstance(payload, dict) else None
        if not isinstance(gates, list):
            return
        loaded: dict[tuple[str, str, str, str, str], DataAvailabilityGate] = {}
        for row in gates:
            if not isinstance(row, dict):
                continue
            try:
                gate = DataAvailabilityGate(**{field: row.get(field, "") for field in DataAvailabilityGate.__dataclass_fields__})
            except Exception:
                continue
            key = (
                str(gate.strategy_id or ""),
                str(gate.dataset or "").strip(),
                str(gate.symbol or "").strip().upper(),
                str(gate.resolution or "").strip(),
                str(gate.stage or "").strip(),
            )
            loaded[key] = gate
        self._gates.update(loaded)

    def _write_durable_state_locked(self) -> None:
        path = self._durable_state_path
        if path is None:
            return
        payload = {
            "artifact_type": "data_availability_gates",
            "generated_at": datetime.now(ET).isoformat(),
            "active_gate_count": len(self._gates),
            "gates": [asdict(gate) for gate in self._gates.values()],
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            tmp.replace(path)
            self._durable_state_write_failed = False
            self._durable_state_error = ""
        except Exception as exc:
            self._durable_state_write_failed = True
            self._durable_state_error = str(exc)
            logger.warning("data_availability_durable_state_write_failed", path=str(path), exc_info=True)

    def set_strategy_data_contracts(self, contracts: Iterable[Any]) -> None:
        required: dict[tuple[str, str], set[bool]] = {}
        required_strategy_ids: set[str] = set()
        strategy_rth_flags: dict[str, set[bool]] = {}
        for contract in contracts or []:
            if bool(getattr(contract, "production_blocking", True)) is not True:
                continue
            strategy_id = str(getattr(contract, "strategy_id", "") or "").strip()
            if strategy_id:
                required_strategy_ids.add(strategy_id)
            symbols = getattr(contract, "symbols", None)
            requirements = getattr(contract, "requirements", None)
            if requirements:
                for requirement in requirements or []:
                    req_symbols = getattr(requirement, "symbols", None) or symbols or []
                    dataset_s = str(getattr(requirement, "dataset", "") or "").strip()
                    resolution_s = str(getattr(requirement, "resolution", "") or "").strip()
                    if dataset_s == "market_bars" and resolution_s:
                        dataset_s = f"bars_{resolution_s}"
                    if not dataset_s:
                        continue
                    rth_required = bool(getattr(requirement, "rth", True))
                    if strategy_id:
                        strategy_rth_flags.setdefault(strategy_id, set()).add(rth_required)
                    for symbol in req_symbols or []:
                        symbol_s = str(symbol or "").strip().upper()
                        if symbol_s:
                            required.setdefault((symbol_s, dataset_s), set()).add(rth_required)
            else:
                datasets = getattr(contract, "datasets", None)
                if strategy_id:
                    strategy_rth_flags.setdefault(strategy_id, set()).add(True)
                for symbol in symbols or []:
                    symbol_s = str(symbol or "").strip().upper()
                    if not symbol_s:
                        continue
                    for dataset in datasets or []:
                        dataset_s = str(dataset or "").strip()
                        if dataset_s:
                            required.setdefault((symbol_s, dataset_s), set()).add(True)
        with self._lock:
            self._required_data_keys = required
            self._required_strategy_ids = required_strategy_ids
            self._required_strategy_rth_flags = strategy_rth_flags

    @staticmethod
    def _canonical_dataset(gate: DataAvailabilityGate) -> str:
        if gate.dataset == "market_bars" and gate.resolution:
            return f"bars_{gate.resolution}"
        return str(gate.dataset or "")

    def _rth_flags_active(self, flags: set[bool] | None, *, now: datetime) -> bool:
        if not flags:
            return True
        # rth=False means this requirement is valid outside RTH too.
        if False in flags:
            return True
        return self.is_rth(now)

    def _gate_matches_required_contract_locked(self, gate: DataAvailabilityGate, *, now: datetime) -> bool:
        if gate.dataset == "strategy_tradability":
            if gate.strategy_id and self._required_strategy_ids and gate.strategy_id not in self._required_strategy_ids:
                return False
            if gate.strategy_id:
                return self._rth_flags_active(self._required_strategy_rth_flags.get(gate.strategy_id), now=now)
            return True
        if not self._required_data_keys:
            return True
        flags = self._required_data_keys.get((str(gate.symbol or "").upper(), self._canonical_dataset(gate)))
        if flags is None:
            return False
        return self._rth_flags_active(flags, now=now)

    def snapshot(self, *, now: Optional[datetime] = None) -> dict[str, Any]:
        now_et = now.astimezone(ET) if now else datetime.now(ET)
        with self._lock:
            expired = self._prune_expired_locked(now=now_et)
            listeners = list(self._listeners)
            all_gates = list(self._gates.values())
            gates = [
                asdict(gate)
                for gate in all_gates
                if self._gate_matches_required_contract_locked(gate, now=now_et)
            ]
            ignored_gates = [
                asdict(gate)
                for gate in all_gates
                if not self._gate_matches_required_contract_locked(gate, now=now_et)
            ]
        for gate in expired:
            self._emit_event(
                event_type="data_available",
                action="expire",
                gate=gate,
                previous=gate,
                listeners=listeners,
                ts=now_et.isoformat(),
            )
        platform_gates = [
            gate
            for gate in gates
            if str(gate.get("dataset") or "") != "strategy_tradability"
        ]
        strategy_tradability_gates = [
            gate
            for gate in gates
            if str(gate.get("dataset") or "") == "strategy_tradability"
        ]
        durable_write_failed = bool(self._durable_state_write_failed)
        return {
            "ok": len(gates) == 0 and not durable_write_failed,
            "durable_state_path": str(self._durable_state_path) if self._durable_state_path is not None else "",
            "durable_state_write_failed": durable_write_failed,
            "durable_state_error": self._durable_state_error,
            "active_gate_count": len(gates),
            "ignored_gate_count": len(ignored_gates),
            "entry_blocking_count": sum(1 for gate in gates if gate.get("severity") == "entry_blocking"),
            "rth_entry_blocking_count": sum(
                1 for gate in gates if gate.get("severity") == "entry_blocking" and bool(gate.get("rth"))
            ),
            "platform_entry_blocking_count": sum(
                1 for gate in platform_gates if gate.get("severity") == "entry_blocking"
            ),
            "rth_platform_entry_blocking_count": sum(
                1
                for gate in platform_gates
                if gate.get("severity") == "entry_blocking" and bool(gate.get("rth"))
            ),
            "strategy_tradability_entry_blocking_count": sum(
                1 for gate in strategy_tradability_gates if gate.get("severity") == "entry_blocking"
            ),
            "rth_strategy_tradability_entry_blocking_count": sum(
                1
                for gate in strategy_tradability_gates
                if gate.get("severity") == "entry_blocking" and bool(gate.get("rth"))
            ),
            "gates": gates,
            "ignored_gates": ignored_gates,
        }

    def entry_allowed(self, *, strategy_id: str = "", datasets: Optional[Iterable[str]] = None) -> bool:
        dataset_filter = {str(item).strip() for item in (datasets or []) if str(item).strip()}
        now_et = datetime.now(ET)
        allowed = True
        with self._lock:
            if self._durable_state_write_failed:
                return False
            expired = self._prune_expired_locked(now=now_et)
            listeners = list(self._listeners)
            for gate in self._gates.values():
                if not self._gate_matches_required_contract_locked(gate, now=now_et):
                    continue
                if gate.severity != "entry_blocking":
                    continue
                if strategy_id and gate.strategy_id and gate.strategy_id != strategy_id:
                    continue
                if dataset_filter and gate.dataset not in dataset_filter:
                    continue
                allowed = False
                break
        for gate in expired:
            self._emit_event(
                event_type="data_available",
                action="expire",
                gate=gate,
                previous=gate,
                listeners=listeners,
                ts=now_et.isoformat(),
            )
        return allowed

    def _emit_event(
        self,
        *,
        event_type: str,
        action: str,
        gate: DataAvailabilityGate,
        previous: Optional[DataAvailabilityGate],
        listeners: Iterable[Callable[[Dict[str, Any]], None]],
        ts: str,
    ) -> None:
        payload = {
            "event_type": event_type,
            "action": action,
            "dataset": gate.dataset,
            "canonical_dataset": self._canonical_dataset(gate),
            "symbol": gate.symbol,
            "resolution": gate.resolution,
            "strategy_id": gate.strategy_id,
            "stage": gate.stage,
            "severity": gate.severity,
            "reason": gate.reason,
            "source": gate.source,
            "rth": gate.rth,
            "first_seen": gate.first_seen,
            "last_seen": gate.last_seen,
            "ts_et": ts,
            "previous_last_seen": previous.last_seen if previous else "",
        }
        writer = getattr(self, "_audit_writer", None)
        if writer is not None:
            try:
                writer.emit(
                    domain="system",
                    component="data_availability",
                    stage=action,
                    status="blocked" if event_type == "data_unavailable" else "clear",
                    reason=gate.reason,
                    strategy_id=gate.strategy_id or None,
                    symbol=gate.symbol or None,
                    payload=payload,
                )
            except Exception:
                logger.warning("data_availability_audit_emit_failed", action=action, reason=gate.reason)
        for listener in listeners:
            try:
                listener(dict(payload))
            except Exception as exc:
                logger.warning(
                    "data_availability_listener_failed",
                    action=action,
                    dataset=gate.dataset,
                    symbol=gate.symbol,
                    listener=repr(listener),
                    error=str(exc),
                    exc_info=True,
                )
                continue


_DATA_AVAILABILITY_SERVICE = DataAvailabilityService()


def configure_data_availability_service(*, durable_state_path: str | Path | None = None) -> DataAvailabilityService:
    service = get_data_availability_service()
    if durable_state_path is not None:
        service.set_durable_state_path(durable_state_path)
    return service


def get_data_availability_service() -> DataAvailabilityService:
    return _DATA_AVAILABILITY_SERVICE
