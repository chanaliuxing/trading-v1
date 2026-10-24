from __future__ import annotations

import json
import os
import re
import hashlib
import subprocess
import fcntl
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator


TERMINAL_STATES = {
    "verified_flat",
    "verified_protected",
    "manual_required_acknowledged",
    "broker_truth_unavailable_halt",
}


def _repo_root_from_artifact_dir(path: Path) -> Path:
    current = path.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return Path.cwd()


def _git_head(root: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return ""


def _config_hash(root: Path) -> str:
    digest = hashlib.sha256()
    patterns = ("config.yml", "config.yaml", "configs/**/*.yml", "configs/**/*.yaml", "configs/**/*.json")
    for path in sorted({item for pattern in patterns for item in root.glob(pattern) if item.is_file()}):
        try:
            digest.update(str(path.relative_to(root)).encode("utf-8"))
            digest.update(b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).hexdigest().encode("ascii"))
        except Exception:
            digest.update(str(path).encode("utf-8"))
            digest.update(b"\0unreadable")
    return digest.hexdigest()


def _runtime_process_start_time(root: Path) -> str:
    value = str(os.getenv("PROCESS_START_TIME", "") or "").strip()
    if value:
        return value
    try:
        pid_text = (root / "runtime/locks/run_system.lock").read_text(encoding="utf-8").strip().split()[0]
        output = subprocess.check_output(["ps", "-p", pid_text, "-o", "lstart="], text=True, stderr=subprocess.DEVNULL).strip()
        if not output:
            return ""
        return datetime.strptime(output, "%a %b %d %H:%M:%S %Y").replace(tzinfo=datetime.now().astimezone().tzinfo).isoformat(timespec="seconds")
    except Exception:
        return ""


class RecoveryIncidentService:
    """Durable file-backed recovery incident ledger.

    This is intentionally small and dependency-free: production safety code can
    publish incident state even when DB reconciliation is degraded.  The stable
    key enforces one active incident per strategy/symbol lane.
    """

    def __init__(self, *, artifact_dir: str | Path = "runtime/readiness/recovery_incidents") -> None:
        self.artifact_dir = Path(artifact_dir)
        self.repo_root = _repo_root_from_artifact_dir(self.artifact_dir)

    def _runtime_fingerprint(self) -> dict[str, Any]:
        return {
            "source_process_pid": os.getpid(),
            "source_pid": os.getpid(),
            "process_start_time": _runtime_process_start_time(self.repo_root),
            "git_head": _git_head(self.repo_root),
            "config_hash": _config_hash(self.repo_root),
        }

    def upsert_active(
        self,
        *,
        strategy_id: str,
        symbol: str,
        qty: float,
        reason: str,
        now_et: datetime,
        source: str,
        active_quarantine: bool = False,
        protected_or_flattening: bool = False,
    ) -> dict[str, Any]:
        incident = self._load_incident(strategy_id=strategy_id, symbol=symbol)
        incident_id = incident.get("incident_id") or self._incident_id(strategy_id=strategy_id, symbol=symbol)
        now_s = now_et.isoformat()
        state = "protected_pending_verified_flat" if protected_or_flattening else "detected"
        if str(incident.get("state") or "") in TERMINAL_STATES:
            self._archive_terminal_incident(incident=incident, now_et=now_et)
            incident = {}
        existing_state = str(incident.get("state") or "")
        awaiting_market_reopen = existing_state == "awaiting_market_reopen"
        existing_intent = incident.get("active_recovery_intent") if isinstance(incident.get("active_recovery_intent"), dict) else None
        payload = {
            **incident,
            **self._runtime_fingerprint(),
            "artifact_type": "recovery_incident",
            "incident_id": incident_id,
            "strategy_id": str(strategy_id or "").strip(),
            "symbol": str(symbol or "").strip().upper(),
            "state": existing_state if existing_state and existing_state != "detected" else state,
            "source": incident.get("source") if awaiting_market_reopen else source,
            "reason": reason,
            "qty": float(qty or 0.0),
            "active_quarantine": bool(active_quarantine),
            "protected_or_flattening": True if awaiting_market_reopen else bool(protected_or_flattening),
            "active_recovery_intent": existing_intent if awaiting_market_reopen else incident.get("active_recovery_intent"),
            "awaiting_market_reopen": bool(incident.get("awaiting_market_reopen")) if awaiting_market_reopen else incident.get("awaiting_market_reopen", False),
            "awaiting_market_reopen_not_before": incident.get("awaiting_market_reopen_not_before", ""),
            "first_seen": incident.get("first_seen") or now_s,
            "last_seen": now_s,
            "updated_at": now_s,
            "terminal": False,
        }
        self._write_incident(payload)
        return payload

    def mark_recovery_submitted(
        self,
        *,
        strategy_id: str,
        symbol: str,
        now_et: datetime,
        decision_id: str,
        side: str,
        qty: float,
    ) -> dict[str, Any]:
        incident = self._load_incident(strategy_id=strategy_id, symbol=symbol)
        previous_intent = incident.get("active_recovery_intent")
        previous_attempts = 0
        if isinstance(previous_intent, dict):
            try:
                previous_attempts = int(previous_intent.get("attempt_count") or 0)
            except Exception:
                previous_attempts = 0
        attempt_count = previous_attempts + 1
        intent = {
            "intent_id": str(decision_id or "").strip(),
            "decision_id": str(decision_id or "").strip(),
            "side": str(side or "").upper(),
            "qty": float(qty or 0.0),
            "attempt_count": attempt_count,
            "max_attempts": int(incident.get("max_recovery_attempts") or 3),
            "submitted_at": now_et.isoformat(),
            "last_attempt_at": now_et.isoformat(),
            "status": "submitted",
            "next_action": "verify_flat_or_protected",
        }
        incident.update(
            {
                "state": "recovery_submitted",
                "recovery_decision_id": decision_id,
                "recovery_side": str(side or "").upper(),
                "recovery_qty": float(qty or 0.0),
                "active_recovery_intent": intent,
                "recovery_attempt_count": attempt_count,
                "last_recovery_submitted_at": now_et.isoformat(),
                "updated_at": now_et.isoformat(),
                "terminal": False,
            }
        )
        self._write_incident(incident)
        return incident

    def mark_recovery_attempt_result(
        self,
        *,
        strategy_id: str,
        symbol: str,
        now_et: datetime,
        success: bool,
        broker_order_id: str | None = None,
        error: str | None = None,
        retry_after_sec: float = 30.0,
    ) -> dict[str, Any]:
        incident = self._load_incident(strategy_id=strategy_id, symbol=symbol)
        intent = dict(incident.get("active_recovery_intent") or {})
        attempts = int(intent.get("attempt_count") or incident.get("recovery_attempt_count") or 0)
        max_attempts = int(intent.get("max_attempts") or incident.get("max_recovery_attempts") or 3)
        if success:
            intent.update(
                {
                    "status": "submitted_to_broker",
                    "broker_order_id": str(broker_order_id or "").strip(),
                    "last_success_at": now_et.isoformat(),
                    "next_action": "verify_flat_or_protected",
                }
            )
            incident.update(
                {
                    "state": "protected_pending_verified_flat",
                    "protected_or_flattening": True,
                    "active_recovery_intent": intent,
                    "updated_at": now_et.isoformat(),
                    "terminal": False,
                }
            )
        else:
            retry_exhausted = attempts >= max_attempts
            next_retry_after = now_et.timestamp() + max(0.0, float(retry_after_sec or 0.0))
            intent.update(
                {
                    "status": "retry_exhausted" if retry_exhausted else "retry_wait",
                    "last_error": str(error or "recovery_attempt_failed"),
                    "last_failure_at": now_et.isoformat(),
                    "next_retry_after": datetime.fromtimestamp(next_retry_after, tz=now_et.tzinfo).isoformat(),
                    "next_action": "manual_required" if retry_exhausted else "retry_recovery",
                }
            )
            incident.update(
                {
                    "state": "manual_required_pending" if retry_exhausted else "recovery_retry_wait",
                    "active_recovery_intent": intent,
                    "last_recovery_error": str(error or "recovery_attempt_failed"),
                    "updated_at": now_et.isoformat(),
                    "terminal": False,
                }
            )
        self._write_incident(incident)
        return incident

    def mark_awaiting_market_reopen(
        self,
        *,
        strategy_id: str,
        symbol: str,
        now_et: datetime,
        qty: float,
        reason: str,
        not_before: datetime | None,
        trigger_id: str = "",
        side: str = "",
    ) -> dict[str, Any]:
        incident = self._load_incident(strategy_id=strategy_id, symbol=symbol)
        if not incident:
            incident = self.upsert_active(
                strategy_id=strategy_id,
                symbol=symbol,
                qty=qty,
                reason=reason,
                now_et=now_et,
                source="platform_eod_supervisor",
                active_quarantine=False,
                protected_or_flattening=True,
            )
        wait_until = not_before.astimezone(now_et.tzinfo).isoformat() if not_before is not None else ""
        intent = {
            "intent_id": str(trigger_id or f"await_market_reopen:{strategy_id}:{symbol}").strip(),
            "decision_id": str(trigger_id or "").strip(),
            "side": str(side or "").upper(),
            "qty": float(qty or 0.0),
            "attempt_count": int((incident.get("active_recovery_intent") or {}).get("attempt_count") or 0),
            "max_attempts": int(incident.get("max_recovery_attempts") or 3),
            "submitted_at": str((incident.get("active_recovery_intent") or {}).get("submitted_at") or ""),
            "last_attempt_at": str((incident.get("active_recovery_intent") or {}).get("last_attempt_at") or ""),
            "status": "awaiting_market_reopen",
            "next_action": "wait_until_market_reopen",
            "next_retry_after": wait_until,
            "awaiting_market_reopen_not_before": wait_until,
            "reason": str(reason or "manual_flatten_awaiting_market_reopen"),
        }
        incident.update(
            {
                "state": "awaiting_market_reopen",
                "source": "platform_eod_supervisor",
                "reason": str(reason or "manual_flatten_awaiting_market_reopen"),
                "qty": float(qty or incident.get("qty") or 0.0),
                "active_recovery_intent": intent,
                "active_quarantine": bool(incident.get("active_quarantine")),
                "protected_or_flattening": True,
                "awaiting_market_reopen": True,
                "awaiting_market_reopen_not_before": wait_until,
                "last_seen": now_et.isoformat(),
                "updated_at": now_et.isoformat(),
                "terminal": False,
            }
        )
        self._write_incident(incident)
        return incident

    def mark_symbol_awaiting_market_reopen(
        self,
        *,
        symbol: str,
        now_et: datetime,
        qty: float,
        reason: str,
        not_before: datetime | None,
        trigger_id: str = "",
        side: str = "",
    ) -> list[dict[str, Any]]:
        symbol_s = str(symbol or "").strip().upper()
        if not symbol_s:
            return []
        updated: list[dict[str, Any]] = []
        for path in self.artifact_dir.glob("incident_*.json"):
            try:
                incident = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if str(incident.get("state") or "") in TERMINAL_STATES:
                continue
            if str(incident.get("symbol") or "").strip().upper() != symbol_s:
                continue
            updated.append(
                self.mark_awaiting_market_reopen(
                    strategy_id=str(incident.get("strategy_id") or ""),
                    symbol=symbol_s,
                    now_et=now_et,
                    qty=qty or float(incident.get("qty") or 0.0),
                    reason=reason,
                    not_before=not_before,
                    trigger_id=trigger_id,
                    side=side,
                )
            )
        return updated

    def awaiting_market_reopen_context(self, *, symbol: str) -> dict[str, Any]:
        symbol_s = str(symbol or "").strip().upper()
        if not symbol_s:
            return {}
        contexts: list[dict[str, Any]] = []
        for path in self.artifact_dir.glob("incident_*.json"):
            try:
                incident = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if str(incident.get("state") or "") in TERMINAL_STATES:
                continue
            if str(incident.get("symbol") or "").strip().upper() != symbol_s:
                continue
            intent = incident.get("active_recovery_intent")
            if not isinstance(intent, dict):
                continue
            if str(intent.get("next_action") or "") != "wait_until_market_reopen":
                continue
            not_before = str(intent.get("next_retry_after") or incident.get("awaiting_market_reopen_not_before") or "")
            contexts.append(
                {
                    "incident_id": str(incident.get("incident_id") or ""),
                    "awaiting_market_reopen_not_before": not_before,
                }
            )
        if not contexts:
            return {}
        not_before_values = [item["awaiting_market_reopen_not_before"] for item in contexts if item.get("awaiting_market_reopen_not_before")]
        return {
            "awaiting_market_reopen": True,
            "awaiting_market_reopen_not_before": min(not_before_values) if not_before_values else "",
            "awaiting_market_reopen_incident_ids": [
                item["incident_id"] for item in contexts if item.get("incident_id")
            ],
        }

    def mark_cancel_replace_requested(
        self,
        *,
        strategy_id: str,
        symbol: str,
        now_et: datetime,
        reason: str,
        replacement_decision_id: str | None = None,
    ) -> dict[str, Any]:
        incident = self._load_incident(strategy_id=strategy_id, symbol=symbol)
        intent = dict(incident.get("active_recovery_intent") or {})
        cancel_replace_count = int(incident.get("cancel_replace_count") or 0) + 1
        intent.update(
            {
                "status": "cancel_replace_requested",
                "cancel_replace_reason": str(reason or "").strip(),
                "replacement_decision_id": str(replacement_decision_id or "").strip(),
                "cancel_replace_requested_at": now_et.isoformat(),
                "next_action": "cancel_replace_recovery_order",
            }
        )
        incident.update(
            {
                "state": "recovery_cancel_replace_requested",
                "active_recovery_intent": intent,
                "cancel_replace_count": cancel_replace_count,
                "updated_at": now_et.isoformat(),
                "terminal": False,
            }
        )
        self._write_incident(incident)
        return incident

    def mark_manual_required(
        self,
        *,
        strategy_id: str,
        symbol: str,
        now_et: datetime,
        manual_lock_ids: list[str],
        reason: str,
    ) -> dict[str, Any]:
        incident = self._load_incident(strategy_id=strategy_id, symbol=symbol)
        incident.update(
            {
                "state": "manual_required_acknowledged",
                "manual_lock_ids": list(manual_lock_ids or []),
                "manual_required_reason": reason,
                "manual_required_at": now_et.isoformat(),
                "active_recovery_intent": None,
                "active_quarantine": False,
                "protected_or_flattening": False,
                "updated_at": now_et.isoformat(),
                "terminal": True,
            }
        )
        self._write_incident(incident)
        return incident

    def get_incident(self, *, strategy_id: str, symbol: str) -> dict[str, Any]:
        return dict(self._load_incident(strategy_id=strategy_id, symbol=symbol) or {})

    def resolve_verified_flat(
        self,
        *,
        nonflat_symbols: set[str],
        active_quarantine_symbols: set[str],
        protected_symbols: set[str] | None = None,
        now_et: datetime,
    ) -> list[dict[str, Any]]:
        nonflat = {str(item or "").strip().upper() for item in nonflat_symbols or set()}
        quarantined = {str(item or "").strip().upper() for item in active_quarantine_symbols or set()}
        protected = {str(item or "").strip().upper() for item in protected_symbols or set()}
        resolved: list[dict[str, Any]] = []
        for path in self.artifact_dir.glob("incident_*.json"):
            try:
                incident = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            state = str(incident.get("state") or "")
            if state in TERMINAL_STATES:
                continue
            symbol = str(incident.get("symbol") or "").strip().upper()
            if not symbol or symbol in quarantined:
                continue
            if symbol in nonflat:
                if symbol not in protected:
                    continue
                terminal_state = "verified_protected"
                terminal_at_field = "verified_protected_at"
            else:
                terminal_state = "verified_flat"
                terminal_at_field = "verified_flat_at"
            incident.update(
                {
                    "state": terminal_state,
                    terminal_at_field: now_et.isoformat(),
                    "active_recovery_intent": None,
                    "active_quarantine": False,
                    "protected_or_flattening": terminal_state == "verified_protected",
                    "updated_at": now_et.isoformat(),
                    "terminal": True,
                }
            )
            self._write_incident(incident)
            resolved.append(incident)
        return resolved

    def resolve_source_verified_flat(self, *, source: str, now_et: datetime) -> list[dict[str, Any]]:
        resolved: list[dict[str, Any]] = []
        source_s = str(source or "").strip()
        for path in self.artifact_dir.glob("incident_*.json"):
            try:
                incident = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            state = str(incident.get("state") or "")
            if state in TERMINAL_STATES:
                continue
            if str(incident.get("source") or "").strip() != source_s:
                continue
            incident.update(
                {
                    "state": "verified_flat",
                    "verified_flat_at": now_et.isoformat(),
                    "active_recovery_intent": None,
                    "active_quarantine": False,
                    "protected_or_flattening": False,
                    "updated_at": now_et.isoformat(),
                    "terminal": True,
                }
            )
            self._write_incident(incident)
            resolved.append(incident)
        return resolved

    def resolve_symbol_verified_flat(
        self,
        *,
        symbol: str,
        now_et: datetime,
        source: str | None = None,
    ) -> list[dict[str, Any]]:
        resolved: list[dict[str, Any]] = []
        symbol_s = str(symbol or "").strip().upper()
        source_s = str(source or "").strip()
        if not symbol_s:
            return resolved
        for path in self.artifact_dir.glob("incident_*.json"):
            try:
                incident = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            state = str(incident.get("state") or "")
            if state in TERMINAL_STATES:
                continue
            if str(incident.get("symbol") or "").strip().upper() != symbol_s:
                continue
            if source_s and str(incident.get("source") or "").strip() != source_s:
                continue
            incident.update(
                {
                    "state": "verified_flat",
                    "verified_flat_at": now_et.isoformat(),
                    "active_recovery_intent": None,
                    "active_quarantine": False,
                    "protected_or_flattening": False,
                    "updated_at": now_et.isoformat(),
                    "terminal": True,
                }
            )
            self._write_incident(incident)
            resolved.append(incident)
        return resolved

    def write_index(self, *, now_et: datetime, resolved_verified_flat_count: int = 0) -> dict[str, Any]:
        incidents = []
        for path in sorted(self.artifact_dir.glob("incident_*.json")):
            try:
                incident = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            normalized = self._normalize_terminal_incident(incident)
            if normalized != incident:
                self._write_incident(normalized)
            incidents.append(normalized)
        active = [item for item in incidents if str(item.get("state") or "") not in TERMINAL_STATES]
        active_intents = [
            item
            for item in active
            if isinstance(item.get("active_recovery_intent"), dict)
        ]
        retry_due_count = 0
        action_queue: list[dict[str, Any]] = []
        for item in active_intents:
            intent = item.get("active_recovery_intent") or {}
            next_action = str(intent.get("next_action") or "").strip()
            next_retry_after = self._parse_dt(intent.get("next_retry_after"))
            retry_due = next_action == "retry_recovery" and next_retry_after is not None and next_retry_after <= now_et
            if retry_due:
                retry_due_count += 1
            if next_action:
                action_queue.append(
                    {
                        "incident_id": str(item.get("incident_id") or ""),
                        "strategy_id": str(item.get("strategy_id") or ""),
                        "symbol": str(item.get("symbol") or ""),
                        "state": str(item.get("state") or ""),
                        "intent_id": str(intent.get("intent_id") or ""),
                        "decision_id": str(intent.get("decision_id") or ""),
                        "next_action": next_action,
                        "retry_due": bool(retry_due),
                        "next_retry_after": str(intent.get("next_retry_after") or ""),
                        "attempt_count": int(intent.get("attempt_count") or 0),
                        "max_attempts": int(intent.get("max_attempts") or 0),
                        "side": str(intent.get("side") or ""),
                        "qty": float(intent.get("qty") or 0.0),
                    }
                )
        manual_required_pending_count = sum(
            1
            for item in active_intents
            if str((item.get("active_recovery_intent") or {}).get("next_action") or "") == "manual_required"
        )
        cancel_replace_pending_count = sum(
            1
            for item in active_intents
            if str((item.get("active_recovery_intent") or {}).get("next_action") or "") == "cancel_replace_recovery_order"
        )
        awaiting_market_reopen_count = sum(
            1
            for item in active_intents
            if str((item.get("active_recovery_intent") or {}).get("next_action") or "") == "wait_until_market_reopen"
        )
        payload = {
            "artifact_type": "recovery_incidents",
            "generated_at": now_et.isoformat(),
            **self._runtime_fingerprint(),
            "active_count": len(active),
            "active_intent_count": len(active_intents),
            "retry_due_count": retry_due_count,
            "cancel_replace_pending_count": cancel_replace_pending_count,
            "awaiting_market_reopen_count": awaiting_market_reopen_count,
            "manual_required_pending_count": manual_required_pending_count,
            "action_required_count": len(action_queue),
            "action_queue": action_queue,
            "terminal_count": len(incidents) - len(active),
            "resolved_verified_flat_count": int(resolved_verified_flat_count or 0),
            "incidents": incidents,
        }
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        index_path = self.artifact_dir / "recovery_incidents_latest.json"
        with self._locked_artifact(index_path):
            self._atomic_write_json(index_path, payload)
        return payload

    @staticmethod
    def _normalize_terminal_incident(incident: dict[str, Any]) -> dict[str, Any]:
        state = str(incident.get("state") or "").strip()
        if state not in TERMINAL_STATES and not bool(incident.get("terminal")):
            return incident
        has_terminal_evidence = False
        if state == "verified_flat":
            has_terminal_evidence = bool(incident.get("verified_flat_at"))
        elif state == "manual_required_acknowledged":
            has_terminal_evidence = bool(incident.get("manual_required_at")) and bool(list(incident.get("manual_lock_ids") or []))
        elif state in {"verified_protected", "broker_truth_unavailable_halt"}:
            has_terminal_evidence = True
        if not has_terminal_evidence:
            return incident
        normalized = dict(incident)
        normalized["active_quarantine"] = False
        normalized["protected_or_flattening"] = False
        normalized["active_recovery_intent"] = None
        normalized["terminal"] = True
        return normalized

    @staticmethod
    def _parse_dt(value: Any) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value))
        except Exception:
            return None

    def _load_incident(self, *, strategy_id: str, symbol: str) -> dict[str, Any]:
        path = self._path(strategy_id=strategy_id, symbol=symbol)
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _write_incident(self, incident: dict[str, Any]) -> None:
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        path = self._path(strategy_id=str(incident.get("strategy_id") or ""), symbol=str(incident.get("symbol") or ""))
        with self._locked_artifact(path):
            if str(incident.get("state") or "") in TERMINAL_STATES or bool(incident.get("terminal")):
                self._archive_terminal_incident(incident=incident, now_et=self._parse_dt(incident.get("updated_at")) or datetime.now().astimezone())
            payload = {**incident, **self._runtime_fingerprint()}
            self._atomic_write_json(path, payload)

    def _archive_terminal_incident(self, *, incident: dict[str, Any], now_et: datetime) -> None:
        if not incident or bool(incident.get("history_archived_at")):
            return
        state = str(incident.get("state") or "").strip()
        if state not in TERMINAL_STATES and not bool(incident.get("terminal")):
            return
        archived = dict(incident)
        archived["history_archived_at"] = now_et.isoformat()
        try:
            history_path = self.artifact_dir / "recovery_incident_history.jsonl"
            self.artifact_dir.mkdir(parents=True, exist_ok=True)
            with self._locked_artifact(history_path):
                with history_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(archived, ensure_ascii=False, sort_keys=True) + "\n")
            incident["history_archived_at"] = archived["history_archived_at"]
        except Exception:
            return

    @staticmethod
    def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
        os.replace(tmp_path, path)

    @contextmanager
    def _locked_artifact(self, path: Path) -> Iterator[None]:
        lock_dir = self.artifact_dir / ".locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_path = lock_dir / f"{path.name}.lock"
        with lock_path.open("a", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def _path(self, *, strategy_id: str, symbol: str) -> Path:
        return self.artifact_dir / f"incident_{self._safe_key(strategy_id)}__{self._safe_key(symbol)}.json"

    @staticmethod
    def _incident_id(*, strategy_id: str, symbol: str) -> str:
        return f"recovery_incident:{str(strategy_id or '').strip()}:{str(symbol or '').strip().upper()}"

    @staticmethod
    def _safe_key(value: str) -> str:
        key = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip().upper())
        return key or "UNKNOWN"
