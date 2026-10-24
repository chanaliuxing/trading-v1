from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from services.production.strategy_data_contract import build_strategy_data_contracts

ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class TradabilityState:
    alive: bool
    configured_live: bool
    session_open: bool
    entry_window_open: bool
    data_ready: bool
    system_ready: bool
    risk_ready: bool
    execution_ready: bool
    tradable_entry_ready: bool
    close_only_allowed: bool = False
    idle_reason: str | None = None
    block_reasons: tuple[str, ...] = ()
    data_blockers: tuple[str, ...] = ()
    system_blockers: tuple[str, ...] = ()
    risk_blockers: tuple[str, ...] = ()
    execution_blockers: tuple[str, ...] = ()
    path_type: str | None = None
    contract_tier: str | None = None
    data_contract_ready: bool = True
    risk_contract_ready: bool = True
    execution_contract_ready: bool = True

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in (
            "block_reasons",
            "data_blockers",
            "system_blockers",
            "risk_blockers",
            "execution_blockers",
        ):
            data[key] = list(data[key])
        return data


@dataclass(frozen=True)
class StrategyTradabilityRecord:
    strategy_id: str
    tradable: bool
    blockers: tuple[str, ...] = ()
    source: str = "unknown"
    details: dict[str, Any] | None = None
    state: TradabilityState | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["blockers"] = list(self.blockers)
        data["details"] = dict(self.details or {})
        if self.state is not None:
            state = self.state.to_dict()
            data["tradability_state"] = state
            data["tradable_entry_ready"] = state["tradable_entry_ready"]
            data["alive"] = state["alive"]
            data["configured_live"] = state["configured_live"]
            data["session_open"] = state["session_open"]
            data["entry_window_open"] = state["entry_window_open"]
            data["data_ready"] = state["data_ready"]
            data["system_ready"] = state["system_ready"]
            data["risk_ready"] = state["risk_ready"]
            data["execution_ready"] = state["execution_ready"]
            data["close_only_allowed"] = state["close_only_allowed"]
            data["idle_reason"] = state["idle_reason"]
            data["block_reasons"] = list(state["block_reasons"])
            data["data_blockers"] = list(state["data_blockers"])
            data["system_blockers"] = list(state["system_blockers"])
            data["risk_blockers"] = list(state["risk_blockers"])
            data["execution_blockers"] = list(state["execution_blockers"])
            data["path_type"] = state["path_type"]
            data["contract_tier"] = state["contract_tier"]
            data["data_contract_ready"] = state["data_contract_ready"]
            data["risk_contract_ready"] = state["risk_contract_ready"]
            data["execution_contract_ready"] = state["execution_contract_ready"]
        return data


def _normalise_strategy_id(value: Any) -> str:
    return str(value or "").strip()


def _contract_strategy_ids(config: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for contract in build_strategy_data_contracts(config):
        if not bool(getattr(contract, "production_blocking", True)):
            continue
        strategy_id = _normalise_strategy_id(getattr(contract, "strategy_id", ""))
        if strategy_id and strategy_id not in result:
            result.append(strategy_id)
    return result


def _parse_iso_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ET)
        return dt.astimezone(ET)
    except Exception:
        return None


def _heartbeat_idle_ok(heartbeat: dict[str, Any]) -> bool:
    blockers = {str(item or "").strip() for item in list(heartbeat.get("blockers") or [])}
    blockers.discard("")
    if blockers != {"idle_not_in_entry_window"}:
        return False
    details = heartbeat.get("details")
    if not isinstance(details, dict):
        details = {}
    if bool(details.get("idle_ok")) is not True:
        return False
    if bool(details.get("has_pending")) is True:
        return False
    if bool(details.get("has_position")) is True:
        # A strategy can be outside its entry window while a broker-truth
        # safety lane owns an existing position.  Tradability should not
        # duplicate the no_go already emitted by EOD/position safety, but the
        # heartbeat must explicitly say the position is supervised.
        return bool(details.get("safety_supervised")) is True
    return True


def _non_pause_blockers(blockers: Any) -> tuple[str, ...]:
    """Remove system-health-derived pause blockers from strategy readiness.

    Hard safety gates such as position safety, manual locks, data availability,
    and ledger integrity are reported by their own production-state checks.  A
    strategy heartbeat that only says `system_paused` is a symptom of those
    gates, not an independent blocker; keeping it here creates a resume latch.
    """
    return tuple(
        str(item)
        for item in list(blockers or [])
        if str(item or "").strip() and str(item or "").strip() != "system_paused"
    )


def _blocker_tail(blocker: str) -> str:
    return str(blocker or "").strip().split(":")[-1]


def _classify_blockers(blockers: Any) -> dict[str, tuple[str, ...]]:
    data: list[str] = []
    system: list[str] = []
    risk: list[str] = []
    execution: list[str] = []
    other: list[str] = []
    for raw in list(blockers or []):
        blocker = str(raw or "").strip()
        if not blocker:
            continue
        tail = _blocker_tail(blocker)
        lower = blocker.lower()
        if tail in {"system_paused", "global_no_go", "runtime_critical_stale"} or lower.startswith("system_"):
            system.append(blocker)
        elif (
            "broker_truth" in lower
            or "position_safety" in lower
            or lower.startswith("risk")
            or "ledger_truth" in lower
            or "quarantine" in lower
            or "manual_intervention" in lower
        ):
            risk.append(blocker)
        elif "execution" in lower or "legacy" in lower or "direct_bypass" in lower:
            execution.append(blocker)
        elif (
            "stream_invalid" in lower
            or "data" in lower
            or "stale" in lower
            or "missing_required" in lower
            or "feature" in lower
            or "market_data" in lower
            or "history" in lower
        ):
            data.append(blocker)
        else:
            other.append(blocker)
    return {
        "data": tuple(data),
        "system": tuple(system),
        "risk": tuple(risk),
        "execution": tuple(execution),
        "other": tuple(other),
    }


def _session_open_from_details(details: dict[str, Any], blockers: set[str]) -> bool:
    if "strategy_session_open" in details:
        return bool(details.get("strategy_session_open"))
    session = str(details.get("session") or "").strip().upper()
    if session == "OFFSESSION" or "offsession" in blockers:
        return False
    return True


def _entry_window_open_from_details(details: dict[str, Any], blockers: set[str]) -> bool:
    if "entry_window_open" in details:
        return bool(details.get("entry_window_open"))
    if "in_entry_window" in details:
        return bool(details.get("in_entry_window"))
    if "idle_not_in_entry_window" in blockers or "not_in_entry_window" in blockers:
        return False
    return True


def _idle_reason(details: dict[str, Any], blockers: set[str], *, session_open: bool, entry_window_open: bool) -> str | None:
    if isinstance(details.get("idle_reason"), str) and str(details.get("idle_reason")).strip():
        return str(details.get("idle_reason")).strip()
    if not session_open:
        return "offsession"
    if not entry_window_open:
        return "not_in_entry_window"
    for blocker in blockers:
        if blocker in {"no_signal", "waiting", "waiting_for_data"}:
            return blocker
    return None


def build_tradability_state(
    *,
    alive: bool,
    configured_live: bool,
    blockers: Any = (),
    details: dict[str, Any] | None = None,
    close_only_allowed: bool | None = None,
) -> TradabilityState:
    detail_map = dict(details or {})
    blocker_values = {str(item or "").strip() for item in list(blockers or []) if str(item or "").strip()}
    session_open = _session_open_from_details(detail_map, blocker_values)
    entry_window_open = _entry_window_open_from_details(detail_map, blocker_values)
    categories = _classify_blockers(blocker_values)
    data_ready = not categories["data"] and bool(detail_map.get("data_ready", True))
    system_ready = not categories["system"] and bool(detail_map.get("system_ready", True))
    risk_ready = not categories["risk"] and bool(detail_map.get("risk_ready", True))
    execution_ready = not categories["execution"] and bool(detail_map.get("execution_ready", True))
    idle = _idle_reason(detail_map, blocker_values, session_open=session_open, entry_window_open=entry_window_open)
    if close_only_allowed is None:
        close_only_allowed = bool(detail_map.get("close_only_allowed", False))
    block_reasons = tuple(
        item
        for group in (
            categories["data"],
            categories["system"],
            categories["risk"],
            categories["execution"],
            categories["other"],
        )
        for item in group
    )
    tradable_entry_ready = (
        bool(alive)
        and bool(configured_live)
        and session_open
        and entry_window_open
        and data_ready
        and system_ready
        and risk_ready
        and execution_ready
    )
    return TradabilityState(
        alive=bool(alive),
        configured_live=bool(configured_live),
        session_open=session_open,
        entry_window_open=entry_window_open,
        data_ready=data_ready,
        system_ready=system_ready,
        risk_ready=risk_ready,
        execution_ready=execution_ready,
        tradable_entry_ready=tradable_entry_ready,
        close_only_allowed=bool(close_only_allowed),
        idle_reason=idle,
        block_reasons=block_reasons,
        data_blockers=categories["data"],
        system_blockers=categories["system"],
        risk_blockers=categories["risk"],
        execution_blockers=categories["execution"],
        path_type=str(detail_map.get("path_type") or "") or None,
        contract_tier=str(detail_map.get("contract_tier") or "") or None,
        data_contract_ready=data_ready,
        risk_contract_ready=risk_ready,
        execution_contract_ready=execution_ready,
    )


def _record(
    *,
    strategy_id: str,
    alive: bool = True,
    configured_live: bool = True,
    blockers: Any = (),
    source: str,
    details: dict[str, Any] | None = None,
    close_only_allowed: bool | None = None,
) -> StrategyTradabilityRecord:
    state = build_tradability_state(
        alive=alive,
        configured_live=configured_live,
        blockers=blockers,
        details=details,
        close_only_allowed=close_only_allowed,
    )
    # Transitional compatibility: legacy `tradable` is now a strict alias for
    # entry readiness.  It no longer means heartbeat alive or idle/offsession OK.
    return StrategyTradabilityRecord(
        strategy_id=strategy_id,
        tradable=state.tradable_entry_ready,
        blockers=tuple(str(item) for item in list(blockers or []) if str(item or "").strip()),
        source=source,
        details=details,
        state=state,
    )


def _is_suppressed_strategy_blocker(record: dict[str, Any], blocker: str) -> bool:
    tail = _blocker_tail(str(blocker))
    if tail in {"system_paused", "offsession"}:
        return True
    if tail in {"idle_not_in_entry_window", "not_in_entry_window"}:
        details = record.get("details") if isinstance(record.get("details"), dict) else {}
        heartbeat = details.get("heartbeat") if isinstance(details.get("heartbeat"), dict) else {}
        heartbeat_details = heartbeat.get("details") if isinstance(heartbeat.get("details"), dict) else {}
        merged = dict(heartbeat_details)
        merged.update({k: v for k, v in details.items() if k != "heartbeat"})
        if bool(merged.get("has_position")) and not bool(merged.get("safety_supervised")):
            return False
        if bool(merged.get("has_pending")):
            return False
        return bool(merged.get("idle_ok")) is True
    return False


def write_strategy_tradability_heartbeat(
    *,
    strategy_id: str,
    tradable: bool,
    blockers: list[str] | tuple[str, ...] | None = None,
    source: str = "strategy",
    details: dict[str, Any] | None = None,
    repo_root: str | Path = ".",
    now: datetime | None = None,
) -> dict[str, Any]:
    ts = (now or datetime.now(ET)).astimezone(ET)
    sid = str(strategy_id or "").strip()
    row = {
        "artifact_type": "strategy_tradability_heartbeat",
        "strategy_id": sid,
        "tradable": bool(tradable),
        "blockers": [str(item) for item in list(blockers or []) if str(item or "").strip()],
        "source": str(source or "strategy"),
        "details": dict(details or {}),
        "generated_at": ts.isoformat(timespec="seconds"),
    }
    if not sid:
        return row
    root = Path(repo_root).resolve()
    out_dir = root / "runtime/readiness/strategy_tradability/heartbeats"
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_sid = "".join(ch if ch.isalnum() or ch in {"_", "-", "."} else "_" for ch in sid)
    latest = out_dir / f"{safe_sid}.json"
    latest.write_text(json.dumps(row, ensure_ascii=False, sort_keys=True, indent=2, default=str), encoding="utf-8")
    day_path = root / "runtime/readiness/strategy_tradability" / f"strategy_tradability_heartbeat_{ts.strftime('%Y%m%d')}.jsonl"
    with day_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True, default=str) + "\n")
    return row


def read_strategy_tradability_heartbeats(
    *,
    repo_root: str | Path,
    now: datetime,
    max_age_sec: float = 90.0,
) -> dict[str, Any]:
    root = Path(repo_root).resolve()
    out_dir = root / "runtime/readiness/strategy_tradability/heartbeats"
    result: dict[str, Any] = {}
    if not out_dir.exists():
        return result
    now_et = now.astimezone(ET) if now.tzinfo else now.replace(tzinfo=ET)
    for path in sorted(out_dir.glob("*.json")):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(row, dict):
            continue
        sid = str(row.get("strategy_id") or "").strip()
        if not sid:
            continue
        ts = _parse_iso_ts(row.get("generated_at"))
        age_sec = None
        stale = True
        if ts is not None:
            age_sec = max(0.0, (now_et - ts.astimezone(ET)).total_seconds())
            stale = age_sec > float(max_age_sec)
        heartbeat = dict(row)
        heartbeat["age_sec"] = age_sec
        heartbeat["max_age_sec"] = float(max_age_sec)
        heartbeat["stale"] = bool(stale)
        if stale:
            heartbeat["tradable"] = False
            blockers = list(heartbeat.get("blockers") or [])
            if "fresh_tradability_heartbeat_stale" not in blockers:
                blockers.append("fresh_tradability_heartbeat_stale")
            heartbeat["blockers"] = blockers
        result[sid] = heartbeat
    return result


def build_strategy_tradability_snapshot(
    *,
    config: dict[str, Any],
    generated_at: datetime,
    data_availability: dict[str, Any] | None = None,
    qqq_liveness: dict[str, Any] | None = None,
    require_fresh_heartbeat: bool = False,
    strategy_heartbeats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a central per-strategy tradability snapshot.

    Platform health being green only proves that the order/safety substrate is
    alive.  This snapshot is stricter: enabled production-blocking strategies
    must not have active strategy/data blockers, and QQQ's live heartbeat must
    be tradable when it is used as the canonical liveness source.
    """

    strategy_ids = _contract_strategy_ids(config)
    records: dict[str, StrategyTradabilityRecord] = {}
    heartbeat_map = dict(strategy_heartbeats or {})
    for strategy_id in strategy_ids:
        heartbeat = heartbeat_map.get(strategy_id)
        if require_fresh_heartbeat and not isinstance(heartbeat, dict):
            records[strategy_id] = _record(
                strategy_id=strategy_id,
                alive=False,
                blockers=("fresh_tradability_heartbeat_missing",),
                source="heartbeat_required",
                details={"required": True},
            )
            continue
        if require_fresh_heartbeat and bool(heartbeat.get("tradable")) is not True:
            if _heartbeat_idle_ok(heartbeat):
                details = {"heartbeat": dict(heartbeat)}
                details.update(dict(heartbeat.get("details") or {}))
                records[strategy_id] = _record(
                    strategy_id=strategy_id,
                    blockers=("idle_not_in_entry_window",),
                    source="heartbeat_idle",
                    details=details,
                )
                continue
            blockers = _non_pause_blockers(heartbeat.get("blockers"))
            if not blockers:
                details = {"heartbeat": dict(heartbeat)}
                details.update(dict(heartbeat.get("details") or {}))
                records[strategy_id] = _record(
                    strategy_id=strategy_id,
                    blockers=tuple(heartbeat.get("blockers") or ()),
                    source="heartbeat_system_pause_derived",
                    details=details,
                )
                continue
            blockers = blockers or ("fresh_tradability_heartbeat_not_ready",)
            details = {"heartbeat": dict(heartbeat)}
            details.update(dict(heartbeat.get("details") or {}))
            records[strategy_id] = _record(
                strategy_id=strategy_id,
                blockers=blockers,
                source="heartbeat",
                details=details,
            )
            continue
        details = {"heartbeat": dict(heartbeat)} if isinstance(heartbeat, dict) else None
        if isinstance(heartbeat, dict):
            details.update(dict(heartbeat.get("details") or {}))
        records[strategy_id] = _record(
            strategy_id=strategy_id,
            source="heartbeat" if require_fresh_heartbeat else "contract",
            details=details,
        )

    allowed_ids = set(strategy_ids)
    for gate in list((data_availability or {}).get("gates") or []):
        if not isinstance(gate, dict):
            continue
        strategy_id = _normalise_strategy_id(gate.get("strategy_id"))
        if not strategy_id or (allowed_ids and strategy_id not in allowed_ids):
            continue
        if str(gate.get("severity") or "entry_blocking") != "entry_blocking":
            continue
        reason = str(gate.get("reason") or "strategy_not_tradable").strip() or "strategy_not_tradable"
        stage = str(gate.get("stage") or "").strip()
        blocker = f"{stage}:{reason}" if stage else reason
        previous = records.get(strategy_id)
        blockers = list(previous.blockers if previous else ())
        if blocker not in blockers:
            blockers.append(blocker)
        details = dict(previous.details or {}) if previous else {}
        details.setdefault("data_availability_gates", []).append(dict(gate))
        records[strategy_id] = _record(
            strategy_id=strategy_id,
            blockers=tuple(blockers),
            source="data_availability",
            details=details,
        )

    qqq = dict(qqq_liveness or {})
    qqq_is_required = "qqq_intraday_001" in allowed_ids or bool(qqq.get("production_blocking"))
    if qqq and qqq_is_required and bool(qqq.get("tradable")) is not True:
        strategy_id = "qqq_intraday_001"
        previous = records.get(strategy_id)
        blockers = list(previous.blockers if previous else ())
        for reason in list(qqq.get("blocking_reasons") or []):
            reason_s = str(reason or "").strip()
            if reason_s and reason_s != "system_paused" and reason_s not in blockers:
                blockers.append(reason_s)
        if not blockers:
            details = dict(previous.details or {}) if previous else {}
            details["qqq_liveness"] = qqq
            records[strategy_id] = _record(
                strategy_id=strategy_id,
                blockers=tuple(qqq.get("blocking_reasons") or ()),
                source="qqq_liveness_system_pause_derived",
                details=details,
            )
        else:
            details = dict(previous.details or {}) if previous else {}
            details["qqq_liveness"] = qqq
            records[strategy_id] = _record(
                strategy_id=strategy_id,
                blockers=tuple(blockers),
                source="qqq_liveness",
                details=details,
            )

    ordered = [records[key].to_dict() for key in sorted(records)]
    blockers = [
        f"{record['strategy_id']}:{blocker}"
        for record in ordered
        for blocker in list(record.get("blockers") or [])
        if not _is_suppressed_strategy_blocker(record, str(blocker))
    ]
    idle_count = sum(1 for record in ordered if record.get("idle_reason"))
    stale_heartbeat_count = sum(
        1
        for record in ordered
        if any(_blocker_tail(str(item)) == "fresh_tradability_heartbeat_stale" for item in list(record.get("blockers") or []))
    )
    tradable_entry_ready_count = sum(1 for record in ordered if bool(record.get("tradable_entry_ready")))
    close_only_allowed_count = sum(1 for record in ordered if bool(record.get("close_only_allowed")))
    return {
        "artifact_type": "strategy_tradability",
        "generated_at": generated_at.isoformat(timespec="seconds"),
        "go_no_go": "go" if not blockers else "no_go",
        "strategy_count": len(ordered),
        "not_tradable_count": sum(
            1
            for record in ordered
            if not bool(record.get("tradable_entry_ready"))
            and any(
                not _is_suppressed_strategy_blocker(record, str(item))
                for item in list(record.get("blockers") or [])
            )
        ),
        "entry_failure_count": len(blockers),
        "idle_count": idle_count,
        "stale_heartbeat_count": stale_heartbeat_count,
        "tradable_entry_ready_count": tradable_entry_ready_count,
        "close_only_allowed_count": close_only_allowed_count,
        "blockers": blockers,
        "records": ordered,
    }
