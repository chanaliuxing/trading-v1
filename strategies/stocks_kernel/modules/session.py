from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SessionDecision:
    allowed: bool
    reason: str
    payload: dict[str, Any]


class SessionModule:
    """
    Session/time-window module.

    This module centralizes session-based entry/flatten decisions so strategy
    behavior does not need to hardcode time gates inline.
    """

    def __init__(self, *, strategy) -> None:
        self._strategy = strategy

    def evaluate_new_entry_window(self, *, ts_epoch_s: float, signal_side: str) -> SessionDecision:
        config = getattr(self._strategy, "config", None)
        if config is None:
            return SessionDecision(allowed=True, reason="ready", payload={})
        cutoff = str(config.raw_extras.get("no_new_after_et") or "").strip()
        if not cutoff:
            return SessionDecision(allowed=True, reason="ready", payload={})
        blocked = self._strategy._is_after_et_cutoff(ts_epoch_s=float(ts_epoch_s or 0.0), cutoff_hhmm=cutoff)
        if blocked:
            return SessionDecision(
                allowed=False,
                reason="no_new_after_et",
                payload={
                    "signal_side": signal_side,
                    "cutoff_et": cutoff,
                    "ts_epoch_s": float(ts_epoch_s or 0.0),
                },
            )
        return SessionDecision(allowed=True, reason="ready", payload={})

    def eod_flatten_due(self, *, ts_epoch_s: float) -> tuple[bool, str]:
        config = getattr(self._strategy, "config", None)
        if config is None:
            return False, ""
        cutoff = str(config.raw_extras.get("flatten_before_close_et") or "").strip()
        if not cutoff:
            return False, ""
        due = self._strategy._is_after_et_cutoff(ts_epoch_s=float(ts_epoch_s or 0.0), cutoff_hhmm=cutoff)
        return due, cutoff

    def execution_session_extras(self, *, snapshot) -> dict[str, Any]:
        config = getattr(self._strategy, "config", None)
        if config is None or snapshot is None:
            return {}
        profile = dict(getattr(config, "execution_profile", {}) or {})
        session = str(getattr(snapshot, "session", None) or "").strip().upper()
        payload: dict[str, Any] = {}
        if session in {"PRE", "POST"}:
            payload["outside_rth"] = bool(profile.get("offhours_outside_rth", True))
        elif session == "OVERNIGHT":
            payload["outside_rth"] = bool(profile.get("overnight_outside_rth", True))
            exchange_override = str(profile.get("overnight_exchange") or "OVERNIGHT").strip().upper()
            if exchange_override:
                payload["exchange_override"] = exchange_override
        return payload

    def protective_execution_session_extras(self, *, snapshot) -> dict[str, Any]:
        config = getattr(self._strategy, "config", None)
        if config is None or snapshot is None:
            return {}
        profile = dict(getattr(config, "execution_profile", {}) or {})
        session = str(getattr(snapshot, "session", None) or "").strip().upper()
        payload: dict[str, Any] = {}
        if session in {"PRE", "POST"}:
            payload["outside_rth"] = bool(
                profile.get("protection_offhours_outside_rth", profile.get("offhours_outside_rth", True))
            )
        elif session == "OVERNIGHT":
            payload["outside_rth"] = bool(
                profile.get("protection_overnight_outside_rth", profile.get("overnight_outside_rth", True))
            )
            exchange_override = str(
                profile.get("protection_overnight_exchange")
                or profile.get("overnight_exchange")
                or "OVERNIGHT"
            ).strip().upper()
            if exchange_override:
                payload["exchange_override"] = exchange_override
        return payload
