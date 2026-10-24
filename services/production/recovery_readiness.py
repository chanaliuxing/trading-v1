from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import os
from threading import RLock
from typing import Any, Callable, Dict, Iterable, Optional
from zoneinfo import ZoneInfo

from services.data_availability import get_data_availability_service
from libs.logger import get_logger

logger = get_logger(__name__)

ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class RecoveryComponent:
    name: str
    ready: bool
    reason: str = ""
    updated_at: str = ""
    metadata: Dict[str, Any] | None = None


class RecoveryReadinessService:
    """Central runtime recovery go/no-go evidence.

    Runtime resume must mean the broker truth and the data plane are both ready.
    This service keeps a small in-process readiness snapshot that can be updated
    by market-data, execution, and reconciler components without creating a hard
    dependency cycle between those services.
    """

    def __init__(self, *, data_availability_service: Any = None) -> None:
        self._lock = RLock()
        self._components: dict[str, RecoveryComponent] = {}
        self._listeners: list[Callable[[Dict[str, Any]], None]] = []
        self._data_availability_service = data_availability_service or get_data_availability_service()
        self._default_ttl_sec: dict[str, float] = {
            "market_data_service": 30.0,
            "market_data_ib": 30.0,
            "market_data_subscriptions": 30.0,
            "bar_pipeline": 20.0,
            "tick_producer": 30.0,
        }
        self._market_data_freshness_components = {
            "market_data_service",
            "market_data_ib",
            "market_data_subscriptions",
            "bar_pipeline",
            "tick_producer",
        }
        raw_required_phases = os.getenv("RECOVERY_READINESS_REQUIRE_MARKET_DATA_FRESHNESS_PHASES", "RTH")
        self._market_data_freshness_required_phases = {
            str(item).strip().upper()
            for item in raw_required_phases.split(",")
            if str(item).strip()
        }
        try:
            self._data_availability_service.add_listener(self._on_data_availability_event)
        except Exception as exc:
            logger.warning("recovery_readiness_data_availability_listener_failed", error=str(exc))

    def add_listener(self, listener: Callable[[Dict[str, Any]], None]) -> None:
        if not callable(listener):
            return
        with self._lock:
            if listener not in self._listeners:
                self._listeners.append(listener)

    def remove_listener(self, listener: Callable[[Dict[str, Any]], None]) -> None:
        with self._lock:
            self._listeners = [item for item in self._listeners if item != listener]

    def mark(
        self,
        name: str,
        *,
        ready: bool,
        reason: str = "",
        metadata: Optional[Dict[str, Any]] = None,
        now: Optional[datetime] = None,
    ) -> None:
        key = str(name or "").strip()
        if not key:
            return
        ts = (now.astimezone(ET) if now else datetime.now(ET)).isoformat()
        listeners: list[Callable[[Dict[str, Any]], None]] = []
        event: Dict[str, Any] = {}
        with self._lock:
            previous = self._components.get(key)
            self._components[key] = RecoveryComponent(
                name=key,
                ready=bool(ready),
                reason=str(reason or ""),
                updated_at=ts,
                metadata=dict(metadata or {}),
            )
            listeners = list(self._listeners)
            event = {
                "component": key,
                "ready": bool(ready),
                "previous_ready": bool(previous.ready) if previous is not None else None,
                "reason": str(reason or ""),
                "updated_at": ts,
                "metadata": dict(metadata or {}),
            }
        for listener in listeners:
            try:
                listener(dict(event))
            except Exception as exc:
                # Readiness updates must never fail the component that reported them.
                logger.warning(
                    "recovery_readiness_listener_failed",
                    component=key,
                    ready=bool(ready),
                    reason=str(reason or ""),
                    listener=repr(listener),
                    error=str(exc),
                    exc_info=True,
                )
                continue

    def _on_data_availability_event(self, event: Dict[str, Any]) -> None:
        """Fan out central data gate changes as recovery-readiness events.

        Data availability is evaluated inside snapshot(), not stored as a normal
        component.  The synthetic component event exists so RuntimeReconciler can
        immediately re-check health on data gate mark/clear instead of waiting for
        its next periodic run.
        """
        try:
            snapshot = self._data_availability_service.snapshot()
            ready = int(snapshot.get("rth_platform_entry_blocking_count") or 0) <= 0
        except Exception as exc:
            snapshot = {"ok": False, "error": str(exc)}
            ready = False
        fanout = {
            "component": "data_availability",
            "ready": bool(ready),
            "previous_ready": None,
            "reason": str((event or {}).get("event_type") or "data_availability_changed"),
            "updated_at": datetime.now(ET).isoformat(),
            "metadata": {
                "event": dict(event or {}),
                "snapshot": snapshot,
            },
        }
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(dict(fanout))
            except Exception as exc:
                logger.warning(
                    "recovery_readiness_listener_failed",
                    component="data_availability",
                    ready=bool(ready),
                    reason=str(fanout.get("reason") or ""),
                    listener=repr(listener),
                    error=str(exc),
                    exc_info=True,
                )
                continue

    def clear(self) -> None:
        with self._lock:
            self._components.clear()

    @staticmethod
    def _session_phase(now_et: datetime) -> str:
        if now_et.weekday() >= 5:
            return "CLOSED"
        minute = now_et.hour * 60 + now_et.minute
        if 9 * 60 + 30 <= minute < 16 * 60:
            return "RTH"
        if 4 * 60 <= minute < 20 * 60:
            return "EXTENDED"
        return "CLOSED"

    def _market_data_freshness_required(self, *, component: str, metadata: Dict[str, Any], now_et: datetime) -> bool:
        if component not in self._market_data_freshness_components:
            return True
        if not bool(metadata.get("allow_offhours_stale")):
            return True
        return self._session_phase(now_et) in self._market_data_freshness_required_phases

    def snapshot(
        self,
        *,
        required_components: Optional[Iterable[str]] = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        required = [str(item).strip() for item in (required_components or []) if str(item).strip()]
        with self._lock:
            components = {name: asdict(component) for name, component in self._components.items()}
        now_et = now.astimezone(ET) if now else datetime.now(ET)
        blockers: list[str] = []
        for name in required:
            component = components.get(name)
            if not component:
                blockers.append(f"{name}_missing")
            elif not bool(component.get("ready")):
                blockers.append(f"{name}_not_ready")
            else:
                metadata = dict(component.get("metadata") or {})
                ttl_raw = metadata.get("ttl_sec", self._default_ttl_sec.get(name))
                try:
                    ttl_sec = float(ttl_raw) if ttl_raw is not None else 0.0
                except Exception:
                    ttl_sec = 0.0
                if ttl_sec > 0:
                    try:
                        updated_at = datetime.fromisoformat(str(component.get("updated_at") or ""))
                        if updated_at.tzinfo is None:
                            updated_at = updated_at.replace(tzinfo=ET)
                        age_sec = (now_et - updated_at.astimezone(ET)).total_seconds()
                    except Exception:
                        age_sec = ttl_sec + 1.0
                    component["age_sec"] = max(0.0, round(float(age_sec), 3))
                    component["ttl_sec"] = ttl_sec
                    if age_sec > ttl_sec:
                        component["stale"] = True
                        component["session_phase"] = self._session_phase(now_et)
                        if self._market_data_freshness_required(
                            component=name,
                            metadata=metadata,
                            now_et=now_et,
                        ):
                            component["ready"] = False
                            blockers.append(f"{name}_stale")
                        else:
                            component["stale_allowed"] = True
        data_snapshot = self._data_availability_service.snapshot(now=now_et)
        if int(data_snapshot.get("rth_platform_entry_blocking_count") or 0) > 0:
            blockers.append("data_availability_entry_blocking")
        return {
            "ready": len(blockers) == 0,
            "blockers": blockers,
            "components": components,
            "data_availability": data_snapshot,
        }


_RECOVERY_READINESS_SERVICE = RecoveryReadinessService()


def get_recovery_readiness_service() -> RecoveryReadinessService:
    return _RECOVERY_READINESS_SERVICE
