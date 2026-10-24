from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")
AUTHORITY = "TradabilityService"


class HaltClass(str, Enum):
    GLOBAL_HALT = "GLOBAL_HALT"
    SCOPED_BLOCK = "SCOPED_BLOCK"
    NONE = "NONE"


class FlowType(str, Enum):
    ENTRY = "ENTRY"
    SCALE_IN = "SCALE_IN"
    EXIT = "EXIT"
    SCALE_OUT = "SCALE_OUT"
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    TRAILING_STOP = "TRAILING_STOP"
    STRATEGY_FLATTEN = "STRATEGY_FLATTEN"
    ACCOUNT_EMERGENCY_FLATTEN = "ACCOUNT_EMERGENCY_FLATTEN"
    PROTECTIVE_REPAIR = "PROTECTIVE_REPAIR"
    RECONCILIATION_REPORT = "RECONCILIATION_REPORT"


class SessionPolicyType(str, Enum):
    RTH_ONLY_EQUITY = "RTH_ONLY_EQUITY"
    EXTENDED_EQUITY = "EXTENDED_EQUITY"
    OPTIONS_RTH_ONLY_DEFAULT = "OPTIONS_RTH_ONLY_DEFAULT"
    TEST_EXTENDED = "TEST_EXTENDED"


class BlockerScopeType(str, Enum):
    GLOBAL = "GLOBAL"
    ACCOUNT = "ACCOUNT"
    STRATEGY = "STRATEGY"
    STRATEGY_INSTANCE = "STRATEGY_INSTANCE"
    ACCOUNT_SYMBOL = "ACCOUNT_SYMBOL"
    SYMBOL = "SYMBOL"
    SESSION = "SESSION"
    EXACT = "EXACT"


@dataclass(frozen=True)
class TradabilityScope:
    account_id: str
    strategy_id: str
    strategy_instance_id: str
    symbol: str
    flow_type: FlowType
    session: str
    asset_type: Optional[str] = None
    order_role: Optional[str] = None
    lot_id: Optional[str] = None


@dataclass(frozen=True)
class TradabilityDecision:
    allow: bool
    halt_class: HaltClass
    authority: str
    scope: TradabilityScope
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]
    overrides: tuple[str, ...]
    close_only_allowed: bool
    reduce_only_allowed: bool
    reason: str
    generated_at: str


@dataclass(frozen=True)
class ScopedBlocker:
    blocker: str
    scope_type: BlockerScopeType | str
    account_id: Optional[str] = None
    strategy_id: Optional[str] = None
    strategy_instance_id: Optional[str] = None
    symbol: Optional[str] = None
    session: Optional[str] = None
    flow_types: tuple[FlowType, ...] = ()


@dataclass(frozen=True)
class SessionPolicy:
    policy_type: SessionPolicyType | str
    allowed_sessions: tuple[str, ...] = ("RTH",)
    allow_extended_options: bool = False


@dataclass(frozen=True)
class TradabilityInputs:
    global_blockers: tuple[str, ...] = ()
    scoped_blockers: tuple[ScopedBlocker, ...] = ()
    session_policy: Optional[SessionPolicy] = None
    owner_identity_complete: bool = True
    close_only_requested: bool = False
    reduce_only_requested: bool = False
    emergency_authorized: bool = False
    manual_authorized: bool = False
    admin_authorized: bool = False


@dataclass(frozen=True)
class ProductionBlockedScope:
    scope_type: str
    account_id: Optional[str]
    strategy_id: Optional[str]
    strategy_instance_id: Optional[str]
    symbol: Optional[str]
    session: Optional[str]
    flow_type: str
    blockers: tuple[str, ...]
    authority: str = AUTHORITY

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope_type": self.scope_type,
            "account_id": self.account_id,
            "strategy_id": self.strategy_id,
            "strategy_instance_id": self.strategy_instance_id,
            "symbol": self.symbol,
            "session": self.session,
            "flow_type": self.flow_type,
            "blockers": list(self.blockers),
            "authority": self.authority,
        }


ENTRY_FLOWS = {FlowType.ENTRY, FlowType.SCALE_IN}
OVERRIDE_FLOWS = {
    FlowType.EXIT,
    FlowType.SCALE_OUT,
    FlowType.STOP_LOSS,
    FlowType.TAKE_PROFIT,
    FlowType.TRAILING_STOP,
    FlowType.STRATEGY_FLATTEN,
    FlowType.PROTECTIVE_REPAIR,
}
REPORT_FLOWS = {FlowType.RECONCILIATION_REPORT}

GLOBAL_HALT_BLOCKERS = {
    "broker_truth_missing",
    "broker_truth_stale",
    "broker_truth_lookup_failed",
    "broker_truth_not_broker_backed",
    "position_safety_missing",
    "position_safety_no_go",
    "position_safety_stale",
    "runtime_version_loaded_code_stale",
    "manual_recovery_required",
    "order_truth_unreconciled",
    "ledger_broker_divergence_global",
    "safety_kernel_global_halt",
}

NORMAL_OVERRIDE_HARD_BLOCKERS = {
    "broker_truth_missing",
    "broker_truth_stale",
    "broker_truth_lookup_failed",
    "broker_truth_not_broker_backed",
    "position_safety_missing",
    "position_safety_no_go",
    "position_safety_stale",
    "runtime_version_loaded_code_stale",
    "manual_recovery_required",
    "order_truth_unreconciled",
    "ledger_broker_divergence_global",
    "safety_kernel_global_halt",
}
NON_BYPASSABLE_EMERGENCY_BLOCKERS = {
    "safety_kernel_global_halt",
}


def evaluate_tradability(
    scope: TradabilityScope,
    inputs: TradabilityInputs | None = None,
    now: Optional[datetime] = None,
) -> TradabilityDecision:
    data = inputs or TradabilityInputs()
    generated_at = _iso_now(now)
    global_blockers = _dedupe(
        blocker for blocker in data.global_blockers if str(blocker or "").strip() in GLOBAL_HALT_BLOCKERS
    )
    scoped_blockers = _dedupe(
        blocker.blocker
        for blocker in data.scoped_blockers
        if _blocker_matches(scope=scope, blocker=blocker)
    )
    session_blocker = _session_blocker(scope=scope, policy=data.session_policy)
    if session_blocker:
        scoped_blockers = _dedupe((*scoped_blockers, session_blocker))

    if scope.flow_type in REPORT_FLOWS:
        warnings = tuple(global_blockers + scoped_blockers)
        return _decision(
            scope=scope,
            allow=True,
            halt_class=HaltClass.NONE,
            blockers=(),
            warnings=warnings,
            overrides=(),
            close_only_allowed=False,
            reduce_only_allowed=False,
            reason="report_only_allowed",
            generated_at=generated_at,
        )

    if scope.flow_type == FlowType.ACCOUNT_EMERGENCY_FLATTEN:
        authorized = bool(data.emergency_authorized or data.manual_authorized or data.admin_authorized)
        non_bypassable = tuple(item for item in global_blockers if item in NON_BYPASSABLE_EMERGENCY_BLOCKERS)
        if non_bypassable:
            return _decision(
                scope=scope,
                allow=False,
                halt_class=HaltClass.GLOBAL_HALT,
                blockers=non_bypassable,
                warnings=tuple(item for item in global_blockers + scoped_blockers if item not in non_bypassable),
                overrides=(),
                close_only_allowed=False,
                reduce_only_allowed=False,
                reason=non_bypassable[0],
                generated_at=generated_at,
            )
        if not authorized:
            return _decision(
                scope=scope,
                allow=False,
                halt_class=HaltClass.SCOPED_BLOCK,
                blockers=("account_emergency_flatten_not_authorized",),
                warnings=tuple(global_blockers + scoped_blockers),
                overrides=(),
                close_only_allowed=False,
                reduce_only_allowed=False,
                reason="account_emergency_flatten_not_authorized",
                generated_at=generated_at,
            )
        return _decision(
            scope=scope,
            allow=True,
            halt_class=HaltClass.NONE,
            blockers=(),
            warnings=tuple(global_blockers + scoped_blockers),
            overrides=("account_emergency_authorized",),
            close_only_allowed=True,
            reduce_only_allowed=True,
            reason="account_emergency_flatten_authorized",
            generated_at=generated_at,
        )

    if global_blockers:
        return _decision(
            scope=scope,
            allow=False,
            halt_class=HaltClass.GLOBAL_HALT,
            blockers=tuple(global_blockers),
            warnings=(),
            overrides=(),
            close_only_allowed=False,
            reduce_only_allowed=False,
            reason=global_blockers[0],
            generated_at=generated_at,
        )

    if scope.flow_type in ENTRY_FLOWS:
        if scoped_blockers:
            return _decision(
                scope=scope,
                allow=False,
                halt_class=HaltClass.SCOPED_BLOCK,
                blockers=tuple(scoped_blockers),
                warnings=(),
                overrides=(),
                close_only_allowed=False,
                reduce_only_allowed=False,
                reason=scoped_blockers[0],
                generated_at=generated_at,
            )
        return _decision(
            scope=scope,
            allow=True,
            halt_class=HaltClass.NONE,
            blockers=(),
            warnings=(),
            overrides=(),
            close_only_allowed=False,
            reduce_only_allowed=False,
            reason="allowed",
            generated_at=generated_at,
        )

    if scope.flow_type in OVERRIDE_FLOWS:
        close_only_allowed = scope.flow_type in {
            FlowType.EXIT,
            FlowType.SCALE_OUT,
            FlowType.STOP_LOSS,
            FlowType.TAKE_PROFIT,
            FlowType.TRAILING_STOP,
            FlowType.STRATEGY_FLATTEN,
            FlowType.PROTECTIVE_REPAIR,
        }
        reduce_only_allowed = scope.flow_type in {
            FlowType.SCALE_OUT,
            FlowType.STOP_LOSS,
            FlowType.TAKE_PROFIT,
            FlowType.TRAILING_STOP,
            FlowType.STRATEGY_FLATTEN,
            FlowType.PROTECTIVE_REPAIR,
        }
        if scoped_blockers:
            if not data.owner_identity_complete:
                return _decision(
                    scope=scope,
                    allow=False,
                    halt_class=HaltClass.SCOPED_BLOCK,
                    blockers=("owner_identity_incomplete", *tuple(scoped_blockers)),
                    warnings=(),
                    overrides=(),
                    close_only_allowed=False,
                    reduce_only_allowed=False,
                    reason="owner_identity_incomplete",
                    generated_at=generated_at,
                )
            override = f"flow_override:{scope.flow_type.value}:entry_blocked"
            return _decision(
                scope=scope,
                allow=True,
                halt_class=HaltClass.NONE,
                blockers=tuple(scoped_blockers),
                warnings=(),
                overrides=(override,),
                close_only_allowed=close_only_allowed,
                reduce_only_allowed=reduce_only_allowed,
                reason="flow_override_allowed",
                generated_at=generated_at,
            )
        return _decision(
            scope=scope,
            allow=True,
            halt_class=HaltClass.NONE,
            blockers=(),
            warnings=(),
            overrides=(),
            close_only_allowed=close_only_allowed or bool(data.close_only_requested),
            reduce_only_allowed=reduce_only_allowed or bool(data.reduce_only_requested),
            reason="allowed",
            generated_at=generated_at,
        )

    return _decision(
        scope=scope,
        allow=False,
        halt_class=HaltClass.SCOPED_BLOCK,
        blockers=("unsupported_flow_type",),
        warnings=(),
        overrides=(),
        close_only_allowed=False,
        reduce_only_allowed=False,
        reason="unsupported_flow_type",
        generated_at=generated_at,
    )


def decision_to_dict(decision: TradabilityDecision) -> dict[str, Any]:
    data = asdict(decision)
    data["halt_class"] = decision.halt_class.value
    data["scope"]["flow_type"] = decision.scope.flow_type.value
    data["blockers"] = list(decision.blockers)
    data["warnings"] = list(decision.warnings)
    data["overrides"] = list(decision.overrides)
    return data


def build_production_state_scoped_contract(
    *,
    safety_go_no_go: str,
    entry_go_no_go: str,
    buckets: dict[str, Any] | None = None,
    strategy_tradability: dict[str, Any] | None = None,
    global_blockers: tuple[str, ...] = (),
    entry_failures: tuple[dict[str, Any], ...] = (),
) -> dict[str, Any]:
    """Build scoped-tradability fields for production_state artifacts.

    ProductionState remains an operator artifact: it can express platform halt
    and blocked entry scopes, but it is not an execution authorizer.
    """

    safety_state = str(safety_go_no_go or "").strip().lower()
    global_halt = safety_state != "go" or bool(tuple(global_blockers))
    bucket_map = dict(buckets or {})
    strategy_map = dict(strategy_tradability or {})
    blocked_scopes = _production_blocked_scopes(
        bucket_map=bucket_map,
        strategy_tradability=strategy_map,
        global_blockers=tuple(global_blockers),
        global_halt=global_halt,
        entry_failures=tuple(item for item in entry_failures if isinstance(item, dict)),
    )
    flow_overrides = _production_flow_overrides(strategy_map=strategy_map, blocked_scopes=blocked_scopes)
    scoped_entry_blocked = any(item.flow_type in {FlowType.ENTRY.value, FlowType.SCALE_IN.value} for item in blocked_scopes)
    has_entry_ready = int(strategy_map.get("tradable_entry_ready_count") or 0) > 0
    entry_state = str(entry_go_no_go or "").strip().lower() or "no_go"
    if safety_state == "go" and scoped_entry_blocked and has_entry_ready:
        entry_state = "partial"
    return {
        "global_halt": bool(global_halt),
        "entry_go_no_go": entry_state,
        "blocked_scopes": [item.to_dict() for item in blocked_scopes],
        "flow_overrides": flow_overrides,
        "authority": AUTHORITY,
        "production_state_authorizes_execution": False,
    }


def inputs_from_metadata(metadata: dict[str, Any] | None) -> TradabilityInputs:
    """Parse optional test/runtime metadata into pure evaluator inputs.

    Unknown shapes fail closed only when they explicitly describe blockers; no
    external runtime calls are made here.
    """

    raw = dict(metadata or {})
    if "scoped_tradability_inputs" in raw and isinstance(raw.get("scoped_tradability_inputs"), dict):
        raw = dict(raw.get("scoped_tradability_inputs") or {})
    blockers = tuple(str(item) for item in list(raw.get("global_blockers") or ()) if str(item or "").strip())
    scoped_blockers = tuple(_scoped_blocker_from_dict(item) for item in list(raw.get("scoped_blockers") or ()) if isinstance(item, dict))
    session_policy = None
    if isinstance(raw.get("session_policy"), dict):
        policy = dict(raw.get("session_policy") or {})
        session_policy = SessionPolicy(
            policy_type=str(policy.get("policy_type") or SessionPolicyType.RTH_ONLY_EQUITY.value),
            allowed_sessions=tuple(str(item).strip().upper() for item in list(policy.get("allowed_sessions") or ("RTH",)) if str(item).strip()),
            allow_extended_options=bool(policy.get("allow_extended_options", False)),
        )
    return TradabilityInputs(
        global_blockers=blockers,
        scoped_blockers=scoped_blockers,
        session_policy=session_policy,
        owner_identity_complete=bool(raw.get("owner_identity_complete", True)),
        close_only_requested=bool(raw.get("close_only_requested", False)),
        reduce_only_requested=bool(raw.get("reduce_only_requested", False)),
        emergency_authorized=bool(raw.get("emergency_authorized", False)),
        manual_authorized=bool(raw.get("manual_authorized", False)),
        admin_authorized=bool(raw.get("admin_authorized", False)),
    )


def build_tradability_inputs_from_readiness_artifacts(
    *,
    repo_root: str | Path,
    scope: TradabilityScope,
    max_artifact_age_sec: float = 180.0,
    now: Optional[datetime] = None,
) -> TradabilityInputs:
    """Build evaluator inputs from local readiness artifacts only.

    This is intentionally read-only. It must not connect to IBKR or query the
    database; stale/missing hard-safety artifacts fail closed for entry.
    """

    root = Path(repo_root).resolve()
    now_et = (now or datetime.now(ET)).astimezone(ET)
    broker_truth = _read_first_json(
        root / "runtime/readiness/broker_truth_snapshot/broker_truth_snapshot_latest.json",
        root / "runtime/readiness/broker_truth_snapshot_latest.json",
    )
    position_safety = _read_first_json(
        root / "runtime/readiness/position_safety/position_safety_latest.json",
        root / "runtime/readiness/position_safety_latest.json",
    )
    production_state = _read_first_json(
        root / "runtime/readiness/production_state/production_state_latest.json",
        root / "runtime/readiness/production_state_latest.json",
    )
    production_runtime_version = (
        production_state.get("runtime_version")
        if isinstance(production_state.get("runtime_version"), dict)
        else {}
    )
    runtime_version = production_runtime_version or _read_first_json(root / "runtime/readiness/runtime_version_latest.json")
    global_blockers: list[str] = []
    global_blockers.extend(_broker_truth_global_blockers(broker_truth, now_et=now_et, max_age_sec=max_artifact_age_sec))
    global_blockers.extend(_position_safety_global_blockers(position_safety, now_et=now_et, max_age_sec=max_artifact_age_sec))
    global_blockers.extend(_runtime_version_global_blockers(runtime_version, now_et=now_et, max_age_sec=max_artifact_age_sec))
    global_blockers.extend(_production_state_global_blockers(production_state))
    scoped_blockers = _production_state_scoped_blockers(production_state, scope=scope)
    return TradabilityInputs(
        global_blockers=tuple(_dedupe(global_blockers)),
        scoped_blockers=tuple(scoped_blockers),
        owner_identity_complete=bool(scope.account_id and scope.strategy_id and scope.strategy_instance_id and scope.symbol),
    )


def _decision(
    *,
    scope: TradabilityScope,
    allow: bool,
    halt_class: HaltClass,
    blockers: tuple[str, ...],
    warnings: tuple[str, ...],
    overrides: tuple[str, ...],
    close_only_allowed: bool,
    reduce_only_allowed: bool,
    reason: str,
    generated_at: str,
) -> TradabilityDecision:
    return TradabilityDecision(
        allow=bool(allow),
        halt_class=halt_class,
        authority=AUTHORITY,
        scope=scope,
        blockers=tuple(blockers),
        warnings=tuple(warnings),
        overrides=tuple(overrides),
        close_only_allowed=bool(close_only_allowed),
        reduce_only_allowed=bool(reduce_only_allowed),
        reason=str(reason or ("allowed" if allow else "blocked")),
        generated_at=generated_at,
    )


def _production_blocked_scopes(
    *,
    bucket_map: dict[str, Any],
    strategy_tradability: dict[str, Any],
    global_blockers: tuple[str, ...],
    global_halt: bool,
    entry_failures: tuple[dict[str, Any], ...] = (),
) -> list[ProductionBlockedScope]:
    scopes: list[ProductionBlockedScope] = []
    if global_halt:
        blockers = tuple(global_blockers or tuple(bucket_map.get("safety_blockers") or ()) or ("global_halt",))
        scopes.append(
            ProductionBlockedScope(
                scope_type=BlockerScopeType.GLOBAL.value,
                account_id=None,
                strategy_id=None,
                strategy_instance_id=None,
                symbol=None,
                session=None,
                flow_type=FlowType.ENTRY.value,
                blockers=blockers,
            )
        )
    for record in list(strategy_tradability.get("records") or []):
        if not isinstance(record, dict):
            continue
        blockers = tuple(str(item) for item in list(record.get("block_reasons") or record.get("blockers") or ()) if str(item or "").strip())
        if not blockers:
            continue
        details = record.get("details") if isinstance(record.get("details"), dict) else {}
        scopes.append(
            ProductionBlockedScope(
                scope_type=BlockerScopeType.STRATEGY_INSTANCE.value,
                account_id=_optional_text(details.get("account_id")),
                strategy_id=_optional_text(record.get("strategy_id")),
                strategy_instance_id=_optional_text(details.get("strategy_instance_id") or record.get("strategy_instance_id") or record.get("strategy_id")),
                symbol=_optional_text(details.get("symbol")),
                session=_optional_text(details.get("session")),
                flow_type=FlowType.ENTRY.value,
                blockers=blockers,
            )
        )
    for failure in entry_failures:
        scopes.extend(_blocked_scopes_from_entry_failure(failure))
    for blocker in list(bucket_map.get("entry_blockers") or ()):
        text = str(blocker or "").strip()
        if not text:
            continue
        lower = text.lower()
        if (
            lower.startswith("manual_intervention_lock")
            or lower.startswith("platform_eod")
            or lower.startswith("option_trigger_")
            or lower.startswith("strategy_tradability:")
        ):
            continue
        scope_type = _scope_type_from_blocker(text)
        strategy_id = _strategy_id_from_blocker(text)
        symbol = _symbol_from_blocker(text)
        if scope_type == BlockerScopeType.STRATEGY_INSTANCE.value and not strategy_id:
            continue
        if scope_type == BlockerScopeType.ACCOUNT_SYMBOL.value and not symbol:
            continue
        scopes.append(
            ProductionBlockedScope(
                scope_type=scope_type,
                account_id=None,
                strategy_id=strategy_id,
                strategy_instance_id=strategy_id,
                symbol=symbol,
                session=None,
                flow_type=FlowType.ENTRY.value,
                blockers=(text,),
            )
        )
    return _dedupe_blocked_scopes(scopes)


def _blocked_scopes_from_entry_failure(failure: dict[str, Any]) -> list[ProductionBlockedScope]:
    service = str(failure.get("service") or "").strip().upper()
    key = str(failure.get("issue_key") or failure.get("error_type") or "").strip()
    if service == "MANUAL_LOCK" or key.startswith("manual_intervention_lock"):
        locks = [item for item in list(failure.get("locks") or ()) if isinstance(item, dict)]
        if not locks:
            return [
                ProductionBlockedScope(
                    scope_type=BlockerScopeType.EXACT.value,
                    account_id=None,
                    strategy_id=None,
                    strategy_instance_id=None,
                    symbol=None,
                    session=None,
                    flow_type=FlowType.ENTRY.value,
                    blockers=(key or "manual_intervention_lock_active",),
                )
            ]
        out: list[ProductionBlockedScope] = []
        blocker = key or "manual_intervention_lock_active"
        for lock in locks:
            context = lock.get("context") if isinstance(lock.get("context"), dict) else {}
            account_id = _optional_text(lock.get("account_id") or context.get("account_id"))
            strategy_id = _optional_text(lock.get("strategy_id") or context.get("strategy_id"))
            instance_id = _optional_text(lock.get("strategy_instance_id") or context.get("strategy_instance_id"))
            symbol = _optional_text(lock.get("symbol") or context.get("symbol"))
            if instance_id or strategy_id:
                scope_type = BlockerScopeType.STRATEGY_INSTANCE.value
                instance_id = instance_id or strategy_id
            elif account_id and symbol:
                scope_type = BlockerScopeType.ACCOUNT_SYMBOL.value
            else:
                scope_type = BlockerScopeType.EXACT.value
            out.append(
                ProductionBlockedScope(
                    scope_type=scope_type,
                    account_id=account_id,
                    strategy_id=strategy_id,
                    strategy_instance_id=instance_id,
                    symbol=symbol,
                    session=_optional_text(context.get("session")),
                    flow_type=FlowType.ENTRY.value,
                    blockers=(blocker,),
                )
            )
        return out
    if service == "PLATFORM_EOD" or key.startswith("platform_eod"):
        locks = [item for item in list(failure.get("manual_locks") or ()) if isinstance(item, dict)]
        manual_lock_ids = tuple(str(item) for item in list(failure.get("manual_lock_ids") or ()) if str(item or "").strip())
        blocker = key or "platform_eod_no_go"
        scoped_lock_ids = tuple(item for item in manual_lock_ids if _parse_platform_eod_lock_id(item) is not None)
        session_lock_ids = tuple(item for item in manual_lock_ids if item not in scoped_lock_ids)
        out: list[ProductionBlockedScope] = []
        # Legacy artifacts may contain both a day-level lock and newer scoped locks.
        # When scoped locks are available, prefer them over widening the blocker
        # back into a session/account-wide halt.
        if not scoped_lock_ids:
            session_blockers = (blocker,) + session_lock_ids
            out.append(
                ProductionBlockedScope(
                    scope_type=BlockerScopeType.SESSION.value,
                    account_id=None,
                    strategy_id=None,
                    strategy_instance_id=None,
                    symbol=None,
                    session=_optional_text(failure.get("session") or "RTH"),
                    flow_type=FlowType.ENTRY.value,
                    blockers=session_blockers,
                )
            )
        for lock_id in scoped_lock_ids:
            parsed = _parse_platform_eod_lock_id(lock_id)
            if parsed is None:
                continue
            strategy_instance_id, symbol = parsed
            out.append(
                ProductionBlockedScope(
                    scope_type=BlockerScopeType.STRATEGY_INSTANCE.value,
                    account_id=None,
                    strategy_id=strategy_instance_id,
                    strategy_instance_id=strategy_instance_id,
                    symbol=symbol,
                    session=_optional_text(failure.get("session") or "RTH"),
                    flow_type=FlowType.ENTRY.value,
                    blockers=(blocker, lock_id),
                )
            )
        for lock in locks:
            context = lock.get("context") if isinstance(lock.get("context"), dict) else {}
            out.append(
                ProductionBlockedScope(
                    scope_type=BlockerScopeType.STRATEGY_INSTANCE.value,
                    account_id=_optional_text(lock.get("account_id") or context.get("account_id")),
                    strategy_id=_optional_text(lock.get("strategy_id") or context.get("strategy_id")),
                    strategy_instance_id=_optional_text(
                        lock.get("strategy_instance_id")
                        or context.get("strategy_instance_id")
                        or lock.get("strategy_id")
                        or context.get("strategy_id")
                    ),
                    symbol=_optional_text(lock.get("symbol") or context.get("symbol")),
                    session=_optional_text(context.get("session") or "RTH"),
                    flow_type=FlowType.ENTRY.value,
                    blockers=(blocker,),
                )
            )
        return out
    if service == "OPTION_TRIGGER_LEDGER" or key.startswith("option_trigger_"):
        blocker = key or "option_trigger_ledger_no_go"
        required_strategy_ids = tuple(
            str(item or "").strip()
            for item in list(failure.get("required_strategy_ids") or ())
            if str(item or "").strip()
        )
        if not required_strategy_ids:
            return [
                ProductionBlockedScope(
                    scope_type=BlockerScopeType.EXACT.value,
                    account_id=None,
                    strategy_id=None,
                    strategy_instance_id=None,
                    symbol=None,
                    session=None,
                    flow_type=FlowType.ENTRY.value,
                    blockers=(blocker,),
                )
            ]
        return [
            ProductionBlockedScope(
                scope_type=BlockerScopeType.STRATEGY_INSTANCE.value,
                account_id=None,
                strategy_id=strategy_id,
                strategy_instance_id=strategy_id,
                symbol=None,
                session=None,
                flow_type=FlowType.ENTRY.value,
                blockers=(blocker,),
            )
            for strategy_id in required_strategy_ids
        ]
    if service == "STRATEGY_TRADABILITY" or key.startswith("strategy_tradability:"):
        blocker = _strategy_tradability_blocker_from_failure(failure)
        strategy_id = _strategy_tradability_strategy_id_from_failure(failure)
        symbol = _optional_text(failure.get("symbol"))
        if not strategy_id:
            return []
        return [
            ProductionBlockedScope(
                scope_type=BlockerScopeType.STRATEGY_INSTANCE.value,
                account_id=_optional_text(failure.get("account_id")),
                strategy_id=strategy_id,
                strategy_instance_id=_optional_text(failure.get("strategy_instance_id") or strategy_id),
                symbol=symbol,
                session=_optional_text(failure.get("session")),
                flow_type=FlowType.ENTRY.value,
                blockers=(blocker,),
            )
        ]
    return []


def _parse_platform_eod_lock_id(lock_id: str) -> tuple[str, str] | None:
    parts = str(lock_id or "").strip().split(":")
    if len(parts) < 4 or parts[0] != "platform_eod_manual_lock":
        return None
    strategy_instance_id = str(parts[2] or "").strip()
    symbol = str(parts[3] or "").strip().upper()
    if not strategy_instance_id or not symbol:
        return None
    return strategy_instance_id, symbol


def _strategy_tradability_strategy_id_from_failure(failure: dict[str, Any]) -> Optional[str]:
    for key in ("strategy_instance_id", "strategy_id"):
        value = _optional_text(failure.get(key))
        if value:
            return value
    for key in ("issue_key", "detail", "error_type"):
        text = str(failure.get(key) or "").strip()
        parts = [part for part in text.split(":") if part]
        if parts and parts[0] == "strategy_tradability" and len(parts) >= 2:
            return _optional_text(parts[1])
        if parts and parts[0] != "strategy_tradability":
            return _optional_text(parts[0])
    return None


def _strategy_tradability_blocker_from_failure(failure: dict[str, Any]) -> str:
    for key in ("blocker", "reason"):
        value = _optional_text(failure.get(key))
        if value:
            return value
    for key in ("issue_key", "detail", "error_type"):
        text = str(failure.get(key) or "").strip()
        parts = [part for part in text.split(":") if part]
        if parts and parts[0] == "strategy_tradability" and len(parts) >= 3:
            return parts[2]
        if len(parts) >= 2 and parts[0] != "strategy_tradability":
            return parts[1]
    return "strategy_tradability_blocked"


def _production_flow_overrides(
    *,
    strategy_map: dict[str, Any],
    blocked_scopes: list[ProductionBlockedScope],
) -> list[dict[str, Any]]:
    close_only_allowed = int(strategy_map.get("close_only_allowed_count") or 0) > 0
    if not close_only_allowed:
        close_only_allowed = any(item.scope_type != BlockerScopeType.GLOBAL.value for item in blocked_scopes)
    if not close_only_allowed:
        return []
    overrides: list[dict[str, Any]] = []
    for item in blocked_scopes:
        if item.scope_type == BlockerScopeType.GLOBAL.value:
            continue
        overrides.append(
            {
                "flow_type": FlowType.STRATEGY_FLATTEN.value,
                "scope": {
                    "scope_type": item.scope_type,
                    "account_id": item.account_id,
                    "strategy_id": item.strategy_id,
                    "strategy_instance_id": item.strategy_instance_id,
                    "symbol": item.symbol,
                    "session": item.session,
                },
                "allowed": True,
                "reason": "close_only_allowed",
                "authority": AUTHORITY,
            }
        )
    if overrides:
        return overrides
    return [
        {
            "flow_type": FlowType.STRATEGY_FLATTEN.value,
            "scope": "blocked_scopes",
            "allowed": True,
            "reason": "close_only_allowed",
            "authority": AUTHORITY,
        }
    ]


def _scoped_blocker_from_dict(data: dict[str, Any]) -> ScopedBlocker:
    return ScopedBlocker(
        blocker=str(data.get("blocker") or "scoped_blocker"),
        scope_type=str(data.get("scope_type") or BlockerScopeType.EXACT.value),
        account_id=_optional_text(data.get("account_id")),
        strategy_id=_optional_text(data.get("strategy_id")),
        strategy_instance_id=_optional_text(data.get("strategy_instance_id")),
        symbol=_optional_text(data.get("symbol")),
        session=_optional_text(data.get("session")),
        flow_types=tuple(_flow_type(item) for item in list(data.get("flow_types") or ()) if str(item or "").strip()),
    )


def _flow_type(value: Any) -> FlowType:
    try:
        return value if isinstance(value, FlowType) else FlowType(str(value))
    except Exception:
        return FlowType.ENTRY


def _optional_text(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def _scope_type_from_blocker(blocker: str) -> str:
    lower = blocker.lower()
    if "symbol" in lower or lower.count(":") >= 2:
        return BlockerScopeType.ACCOUNT_SYMBOL.value
    if "strategy" in lower:
        return BlockerScopeType.STRATEGY_INSTANCE.value
    return BlockerScopeType.EXACT.value


def _strategy_id_from_blocker(blocker: str) -> Optional[str]:
    parts = [part for part in str(blocker or "").split(":") if part]
    for part in parts:
        if "strategy" in part.lower() or part.endswith("_001"):
            return part
    return None


def _symbol_from_blocker(blocker: str) -> Optional[str]:
    parts = [part.strip().upper() for part in str(blocker or "").split(":") if part.strip()]
    for part in parts:
        if part.isalpha() and 1 <= len(part) <= 6 and part not in {"ENTRY", "DATA"}:
            return part
    return None


def _dedupe_blocked_scopes(scopes: list[ProductionBlockedScope]) -> list[ProductionBlockedScope]:
    seen: set[tuple[Any, ...]] = set()
    out: list[ProductionBlockedScope] = []
    for item in scopes:
        key = (
            item.scope_type,
            item.account_id,
            item.strategy_id,
            item.strategy_instance_id,
            item.symbol,
            item.session,
            item.flow_type,
            item.blockers,
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _read_first_json(*paths: Path) -> dict[str, Any]:
    for path in paths:
        try:
            if not path.exists():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            return {"_read_error": str(path)}
    return {}


def _artifact_stale(payload: dict[str, Any], *, now_et: datetime, max_age_sec: float) -> bool:
    if not payload:
        return True
    if bool(payload.get("stale")):
        return True
    generated_at = payload.get("generated_at")
    if not generated_at and isinstance(payload.get("freshness"), dict):
        generated_at = payload.get("freshness", {}).get("generated_at")
    ts = _parse_artifact_ts(generated_at)
    if ts is None:
        return False
    return (now_et - ts.astimezone(ET)).total_seconds() > float(max_age_sec)


def _parse_artifact_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        text = str(value)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ET)
        return parsed.astimezone(ET)
    except Exception:
        return None


def _go_no_go(payload: dict[str, Any]) -> str:
    return str(payload.get("go_no_go") or payload.get("status") or "").strip().lower()


def _broker_truth_global_blockers(payload: dict[str, Any], *, now_et: datetime, max_age_sec: float) -> list[str]:
    if not payload:
        return ["broker_truth_missing"]
    if payload.get("_read_error"):
        return ["broker_truth_missing"]
    blockers: list[str] = []
    if _artifact_stale(payload, now_et=now_et, max_age_sec=max_age_sec):
        blockers.append("broker_truth_stale")
    if _go_no_go(payload) == "no_go":
        raw_blockers = {str(item or "").strip() for item in list(payload.get("blockers") or [])}
        if any("order_truth" in item for item in raw_blockers):
            blockers.append("order_truth_unreconciled")
        if any("ledger" in item and ("broker" in item or "divergence" in item) for item in raw_blockers):
            blockers.append("ledger_broker_divergence_global")
        if bool(payload.get("lookup_failed") or payload.get("live_broker_lookup_failed")):
            blockers.append("broker_truth_missing")
        elif bool(payload.get("broker_truth_backed", payload.get("live_broker_truth_available", True))) is not True:
            blockers.append("broker_truth_not_broker_backed")
        else:
            blockers.append("broker_truth_missing")
    if bool(payload.get("broker_truth_backed", True)) is not True:
        blockers.append("broker_truth_not_broker_backed")
    if bool(payload.get("live_broker_truth_available", True)) is not True:
        blockers.append("broker_truth_missing")
    return blockers


def _position_safety_global_blockers(payload: dict[str, Any], *, now_et: datetime, max_age_sec: float) -> list[str]:
    if not payload:
        return ["position_safety_missing"]
    if payload.get("_read_error"):
        return ["position_safety_missing"]
    blockers: list[str] = []
    if _artifact_stale(payload, now_et=now_et, max_age_sec=max_age_sec):
        blockers.append("position_safety_stale")
    if _go_no_go(payload) == "no_go":
        blockers.append("position_safety_no_go")
    if bool(payload.get("lookup_failed")):
        blockers.append("position_safety_no_go")
    for item in list(payload.get("blockers") or []):
        text = str(item or "").strip().lower()
        if "position_safety" in text and "stale" in text:
            blockers.append("position_safety_stale")
        elif text:
            blockers.append("position_safety_no_go")
    return blockers


def _runtime_version_global_blockers(payload: dict[str, Any], *, now_et: datetime, max_age_sec: float) -> list[str]:
    if not payload:
        return ["runtime_version_loaded_code_stale"]
    if payload.get("_read_error"):
        return ["runtime_version_loaded_code_stale"]
    blockers: list[str] = []
    if _artifact_stale(payload, now_et=now_et, max_age_sec=max_age_sec):
        blockers.append("runtime_version_loaded_code_stale")
    freshness = payload.get("freshness") if isinstance(payload.get("freshness"), dict) else {}
    if bool(payload.get("runtime_version_loaded_code_stale") or freshness.get("runtime_version_loaded_code_stale")):
        blockers.append("runtime_version_loaded_code_stale")
    if bool(payload.get("loaded_code_current", freshness.get("loaded_code_current", True))) is not True:
        blockers.append("runtime_version_loaded_code_stale")
    if any(str(item or "").strip() == "runtime_version_loaded_code_stale" for item in list(payload.get("blockers") or [])):
        blockers.append("runtime_version_loaded_code_stale")
    return blockers


def _production_state_global_blockers(payload: dict[str, Any]) -> list[str]:
    if not payload:
        return []
    blockers: list[str] = []
    for failure in list(payload.get("failures") or []):
        if not isinstance(failure, dict):
            continue
        text = " ".join(
            str(failure.get(key) or "").strip().lower()
            for key in ("issue_key", "error_type", "detail", "reason")
        )
        if "manual_recovery_required" in text or "manual_gateway_api_recovery_required" in text:
            blockers.append("manual_recovery_required")
    if bool(payload.get("global_halt")):
        for item in list(payload.get("blocked_scopes") or []):
            if not isinstance(item, dict) or str(item.get("scope_type") or "").upper() != BlockerScopeType.GLOBAL.value:
                continue
            for blocker in list(item.get("blockers") or []):
                mapped = _global_blocker_from_label(str(blocker))
                if mapped:
                    blockers.append(mapped)
    return blockers


def _global_blocker_from_label(label: str) -> str:
    lower = str(label or "").strip().lower()
    if "broker_truth" in lower:
        return "broker_truth_missing"
    if "position_safety" in lower:
        return "position_safety_no_go"
    if "runtime_version" in lower or "runtime" in lower and "stale" in lower:
        return "runtime_version_loaded_code_stale"
    if "manual_recovery" in lower or "manual_gateway_api_recovery" in lower:
        return "manual_recovery_required"
    if "order_truth" in lower:
        return "order_truth_unreconciled"
    if "ledger" in lower and ("broker" in lower or "divergence" in lower):
        return "ledger_broker_divergence_global"
    if "safety_kernel_global_halt" in lower:
        return "safety_kernel_global_halt"
    return ""


def _production_state_scoped_blockers(payload: dict[str, Any], *, scope: TradabilityScope) -> list[ScopedBlocker]:
    blockers: list[ScopedBlocker] = []
    for item in list((payload or {}).get("blocked_scopes") or []):
        if not isinstance(item, dict):
            continue
        scope_type = str(item.get("scope_type") or BlockerScopeType.EXACT.value).strip().upper()
        if scope_type == BlockerScopeType.GLOBAL.value:
            continue
        raw_blockers = tuple(str(raw) for raw in list(item.get("blockers") or []) if str(raw or "").strip())
        if not raw_blockers:
            continue
        candidate = ScopedBlocker(
            blocker=raw_blockers[0],
            scope_type=scope_type,
            account_id=_optional_text(item.get("account_id")),
            strategy_id=_optional_text(item.get("strategy_id")),
            strategy_instance_id=_optional_text(item.get("strategy_instance_id")),
            symbol=_optional_text(item.get("symbol")),
            session=_optional_text(item.get("session")),
            flow_types=(),
        )
        if _blocker_matches(scope=scope, blocker=candidate):
            blockers.append(candidate)
    return blockers


def _blocker_matches(*, scope: TradabilityScope, blocker: ScopedBlocker) -> bool:
    if blocker.flow_types and scope.flow_type not in set(blocker.flow_types):
        return False
    scope_type = _scope_type(blocker.scope_type)
    account_match = _matches(blocker.account_id, scope.account_id)
    strategy_match = _matches(blocker.strategy_id, scope.strategy_id)
    instance_match = _matches(blocker.strategy_instance_id, scope.strategy_instance_id)
    symbol_match = _matches(blocker.symbol, scope.symbol, upper=True)
    session_match = _matches(blocker.session, scope.session, upper=True)

    if scope_type == BlockerScopeType.GLOBAL:
        return True
    if scope_type == BlockerScopeType.ACCOUNT:
        return account_match
    if scope_type == BlockerScopeType.STRATEGY:
        return account_match and strategy_match
    if scope_type == BlockerScopeType.STRATEGY_INSTANCE:
        return account_match and instance_match
    if scope_type == BlockerScopeType.ACCOUNT_SYMBOL:
        return account_match and symbol_match
    if scope_type == BlockerScopeType.SYMBOL:
        return symbol_match
    if scope_type == BlockerScopeType.SESSION:
        return session_match and (
            (blocker.strategy_id is None and blocker.strategy_instance_id is None)
            or strategy_match
            or instance_match
        )
    if scope_type == BlockerScopeType.EXACT:
        return account_match and strategy_match and instance_match and symbol_match and session_match
    return False


def _session_blocker(*, scope: TradabilityScope, policy: SessionPolicy | None) -> str:
    if policy is None:
        return ""
    policy_type = _policy_type(policy.policy_type)
    session = str(scope.session or "").strip().upper()
    allowed_sessions = {str(item or "").strip().upper() for item in policy.allowed_sessions if str(item or "").strip()}
    if not allowed_sessions:
        allowed_sessions = {"RTH"}

    if policy_type == SessionPolicyType.RTH_ONLY_EQUITY:
        if scope.flow_type in ENTRY_FLOWS and session != "RTH":
            return "session_policy_rth_only_entry_blocked"
        if scope.flow_type in OVERRIDE_FLOWS and session != "RTH":
            return "session_policy_rth_only_entry_blocked"
        return ""
    if policy_type == SessionPolicyType.EXTENDED_EQUITY:
        if scope.flow_type in ENTRY_FLOWS and session not in allowed_sessions:
            return "session_policy_extended_entry_not_allowed"
        if scope.flow_type in OVERRIDE_FLOWS and session not in allowed_sessions:
            return "session_policy_extended_entry_not_allowed"
        return ""
    if policy_type == SessionPolicyType.OPTIONS_RTH_ONLY_DEFAULT:
        if scope.flow_type in ENTRY_FLOWS and session != "RTH" and not policy.allow_extended_options:
            return "session_policy_options_rth_only_entry_blocked"
        if scope.flow_type in ENTRY_FLOWS and session not in allowed_sessions and policy.allow_extended_options:
            return "session_policy_options_extended_session_not_allowed"
        if scope.flow_type in OVERRIDE_FLOWS and session != "RTH" and not policy.allow_extended_options:
            return "session_policy_options_rth_only_entry_blocked"
        if scope.flow_type in OVERRIDE_FLOWS and session not in allowed_sessions and policy.allow_extended_options:
            return "session_policy_options_extended_session_not_allowed"
        return ""
    if policy_type == SessionPolicyType.TEST_EXTENDED:
        if scope.flow_type in ENTRY_FLOWS and session not in allowed_sessions:
            return "session_policy_test_extended_entry_not_allowed"
        if scope.flow_type in OVERRIDE_FLOWS and session not in allowed_sessions:
            return "session_policy_test_extended_entry_not_allowed"
        return ""
    return ""


def _scope_type(value: BlockerScopeType | str) -> BlockerScopeType:
    try:
        return value if isinstance(value, BlockerScopeType) else BlockerScopeType(str(value))
    except Exception:
        return BlockerScopeType.EXACT


def _policy_type(value: SessionPolicyType | str) -> SessionPolicyType:
    try:
        return value if isinstance(value, SessionPolicyType) else SessionPolicyType(str(value))
    except Exception:
        return SessionPolicyType.RTH_ONLY_EQUITY


def _matches(expected: Optional[str], actual: str, *, upper: bool = False) -> bool:
    if expected is None or str(expected).strip() == "":
        return True
    left = str(expected or "").strip()
    right = str(actual or "").strip()
    if upper:
        left = left.upper()
        right = right.upper()
    return left == right


def _dedupe(values) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in values:
        value = str(raw or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _iso_now(now: Optional[datetime]) -> str:
    value = now or datetime.now(ET)
    if value.tzinfo is None:
        value = value.replace(tzinfo=ET)
    return value.astimezone(ET).isoformat(timespec="seconds")
