"""
Decision Pipeline Service
Integrates Validator → Arbiter → Translator into a single service

This service can run in two modes:
1. DryRun mode: Log decisions but don't execute
2. Active mode: Actually process decisions
"""
import asyncio
from dataclasses import dataclass
import os
from typing import Dict, Any, Optional
import uuid
from datetime import datetime

from libs.strategy.base import DecisionEnvelope
from libs.logger import get_logger
from libs.obs.audit_writer import LocalAuditWriter
from prometheus_client import Counter, Histogram, Gauge

from .validator import PreDecisionValidator, ValidateConfig, SimpleCostModel
from .arbiter import ArbitrationEngine
from .quantity_semantics import conservative_execution_qty, stabilize_requested_qty
from .translator import IntentTranslator, ExecPlan

logger = get_logger(__name__)

# Metrics
pipeline_decisions_total = Counter(
    'pipeline_decisions_total',
    'Total decisions processed',
    ['strategy_id', 'status']
)

pipeline_latency_seconds = Histogram(
    'pipeline_latency_seconds',
    'Pipeline processing latency',
    ['stage'],
    buckets=[0.001, 0.005, 0.01, 0.05, 0.1, 0.2, 0.5]
)

pipeline_active_decisions = Gauge(
    'pipeline_active_decisions',
    'Currently active decisions',
    ['symbol']
)


@dataclass
class PipelineResult:
    """Result of pipeline processing"""
    status: str  # APPROVED/REJECTED/DEFERRED
    decision_id: str
    strategy_id: str
    symbol: str
    stage: Optional[str] = None  # validator/arbiter/translator
    plan: Optional[ExecPlan] = None
    reasons: list = None
    metadata: dict = None

    def __post_init__(self):
        if self.reasons is None:
            self.reasons = []
        if self.metadata is None:
            self.metadata = {}


class DecisionPipelineService:
    """
    Decision Pipeline Service

    Processes trading decisions through:
    1. Validator (freshness, data quality, cost coverage)
    2. Arbiter (multi-strategy coordination)
    3. Translator (envelope → execution plan)

    Can run in dry_run mode for testing.
    """

    def __init__(
        self,
        market_state_provider: Any,
        bar_store: Any,
        feature_store: Any,
        reserve_ledger: Any,
        adv_provider: Optional[Any] = None,
        tick_size_provider: Optional[Any] = None,
        validator_cfg: Optional[ValidateConfig] = None,
        arbiter_cfg: Optional[Dict[str, Any]] = None,
        translator_cfg: Optional[Dict[str, Any]] = None,
        dry_run_mode: bool = True,
        shadow_mode: Optional[bool] = None,
        bypass_validator: bool = False,
        bypass_arbiter: bool = False,
        trace_validation_blocking: bool = False,
        audit_writer: Optional[LocalAuditWriter] = None,
    ):
        """
        Initialize pipeline service

        Args:
            market_state_provider: Market state (bid/ask/halt)
            bar_store: Historical bar access
            feature_store: Feature access
            reserve_ledger: Capital reservation ledger
            adv_provider: Average daily volume provider
            tick_size_provider: Tick size provider
            validator_cfg: Validator configuration
            arbiter_cfg: Arbiter configuration
            translator_cfg: Translator configuration
            dry_run_mode: If True, log only (don't execute)
        """
        if shadow_mode is not None:
            dry_run_mode = bool(shadow_mode)
        self.shadow_mode = bool(shadow_mode) if shadow_mode is not None else bool(dry_run_mode)
        self.dry_run_mode = dry_run_mode
        self.bypass_validator = bool(bypass_validator)
        self.bypass_arbiter = bool(bypass_arbiter)
        self.trace_validation_blocking = bool(trace_validation_blocking)
        self.audit_writer = audit_writer

        # Validator
        if validator_cfg is None:
            validator_cfg = ValidateConfig()

        cost_model = SimpleCostModel()
        self.validator = PreDecisionValidator(
            validator_cfg,
            market_state_provider,
            bar_store,
            feature_store,
            cost_model
        )

        # Arbiter
        if arbiter_cfg is None:
            arbiter_cfg = self._default_arbiter_config()

        self.arbiter = ArbitrationEngine(
            arbiter_cfg,
            reserve_ledger,
            adv_provider
        )

        # Translator
        if translator_cfg is None:
            translator_cfg = self._default_translator_config()
        else:
            merged = self._default_translator_config()
            merged.update(translator_cfg)
            translator_cfg = merged

        self.translator = IntentTranslator(
            translator_cfg,
            market_state_provider,
            tick_size_provider
        )

        # Stats
        self.stats = {
            "decisions_total": 0,
            "decisions_approved": 0,
            "decisions_rejected": 0,
            "decisions_deferred": 0,
            "validator_rejects": 0,
            "arbiter_rejects": 0,
        }

        # Execution service (wired after initialization)
        self.execution_service = None

        mode = "DRY_RUN" if dry_run_mode else "ACTIVE"
        logger.info(f"DecisionPipelineService initialized (mode={mode})",
                   validator_cfg=validator_cfg,
                   dry_run_mode=dry_run_mode,
                   bypass_validator=self.bypass_validator,
                   bypass_arbiter=self.bypass_arbiter)

    def _emit_local_audit(
        self,
        *,
        stage: str,
        envelope: DecisionEnvelope,
        status: Optional[str] = None,
        reason: Optional[str] = None,
        severity: str = "info",
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        if self.audit_writer is None:
            return
        extras = dict(envelope.extras or {})
        try:
            self.audit_writer.emit(
                domain="exec",
                component="decision_pipeline",
                stage=stage,
                severity=severity,
                strategy_id=envelope.strategy_id,
                strategy_run_id=extras.get("strategy_run_id"),
                trigger_id=extras.get("trigger_id"),
                decision_id=envelope.decision_id,
                trace_id=extras.get("trace_id"),
                causation_id=extras.get("causation_id"),
                symbol=envelope.symbol,
                status=status,
                reason=reason,
                payload=payload,
            )
        except Exception as exc:
            logger.warning("local_pipeline_audit_emit_failed", stage=stage, error=str(exc))

    def _default_arbiter_config(self) -> Dict[str, Any]:
        """Default arbiter configuration"""
        return {
            "arbiter": {
                "policy": "priority_then_marginal",
                "preempt": "never",
                "coexist_same_side": True,
                "ttl_ms": 1500,
                "symbol_cap": {
                    "max_notional_usd": 500000,
                    "max_participation": 0.2
                },
                "strategy_priority": {},  # Will be populated at runtime
                "scoring": {
                    "w_edge": 1.0,
                    "w_cost": 1.0,
                    "penalty_bp": 0.0
                }
            }
        }

    def _ensure_trace_extras(self, envelope: DecisionEnvelope, extras: Dict[str, Any]) -> Dict[str, Any]:
        missing: list[str] = []
        if not str(extras.get("strategy_run_id") or "").strip():
            extras["strategy_run_id"] = str(uuid.uuid4())
            missing.append("strategy_run_id")
        if not str(extras.get("trace_id") or "").strip():
            # Use decision_id as a stable fallback trace_id.
            extras["trace_id"] = str(envelope.decision_id or uuid.uuid4())
            missing.append("trace_id")
        if not str(extras.get("trigger_id") or "").strip():
            extras["trigger_id"] = str(envelope.decision_id or uuid.uuid4())
            missing.append("trigger_id")
        if missing:
            extras["trace_compat_mode"] = True
            extras["trace_missing_fields"] = missing
            extras.setdefault("trace_source", "legacy_decision_id")
        return extras

    def _ensure_intent_meta(self, extras: Dict[str, Any]) -> Dict[str, Any]:
        if not str(extras.get("intent_source") or "").strip():
            if extras.get("manual_intervention") or extras.get("manual_source"):
                extras["intent_source"] = "manual"
            else:
                extras["intent_source"] = "strategy"
        if not str(extras.get("intent_action") or "").strip():
            if extras.get("flatten_action"):
                extras["intent_action"] = "flatten"
            elif extras.get("protective_stop"):
                extras["intent_action"] = "protective_stop"
            elif extras.get("close_only"):
                extras["intent_action"] = "close_only"
            else:
                extras["intent_action"] = "entry"
        return extras

    def _execution_delegate_account_id(self) -> str:
        delegate = self.execution_service
        candidates = [
            getattr(delegate, "account_id", None),
            getattr(delegate, "account", None),
        ]
        execution_service = getattr(delegate, "execution_service", None)
        candidates.extend(
            [
                getattr(execution_service, "account_id", None),
                getattr(execution_service, "account", None),
            ]
        )
        for owner in (delegate, execution_service):
            if owner is None:
                continue
            for kernel_attr in ("safety_kernel", "kernel"):
                kernel = getattr(owner, kernel_attr, None)
                if kernel is None:
                    continue
                candidates.extend(
                    [
                        getattr(kernel, "account_id", None),
                        getattr(kernel, "account", None),
                    ]
                )
                broker_adapter = getattr(kernel, "broker_adapter", None)
                broker = getattr(kernel, "broker", None)
                for broker_owner in (broker_adapter, broker):
                    candidates.extend(
                        [
                            getattr(broker_owner, "account", None),
                            getattr(broker_owner, "account_id", None),
                        ]
                    )
                    ib = getattr(broker_owner, "ib", None)
                    managed_accounts_fn = getattr(ib, "managedAccounts", None)
                    if callable(managed_accounts_fn):
                        try:
                            accounts = [str(item or "").strip() for item in list(managed_accounts_fn() or [])]
                        except Exception:
                            accounts = []
                        non_empty_accounts = [item for item in accounts if item and item != "UNKNOWN_ACCOUNT"]
                        if len(non_empty_accounts) == 1:
                            candidates.append(non_empty_accounts[0])
                resolved_account_fn = getattr(kernel, "_resolved_account_id", None)
                if callable(resolved_account_fn):
                    try:
                        candidates.append(resolved_account_fn())
                    except Exception:
                        pass
        for value in candidates:
            text = str(value or "").strip()
            if text and text != "UNKNOWN_ACCOUNT":
                return text
        return ""

    def _ensure_owner_identity(self, envelope: DecisionEnvelope, extras: Dict[str, Any]) -> Dict[str, Any]:
        """
        Normalize live-order owner identity at the pipeline boundary.

        Strategies should express trading logic, not broker ownership plumbing.
        The execution layer still validates this contract strictly; this method
        makes the contract available from stable runtime/account context before
        the intent is delegated.
        """
        account_id = str(
            extras.get("account_id")
            or extras.get("broker_account")
            or extras.get("account")
            or self._execution_delegate_account_id()
            or os.getenv("IB_ACCOUNT", "")
            or ""
        ).strip()
        if account_id:
            extras["account_id"] = account_id
            extras.setdefault("owner_account_source", "pipeline_context")

        strategy_run_id = str(extras.get("strategy_run_id") or "").strip()
        generated_strategy_run = "strategy_run_id" in set(extras.get("trace_missing_fields") or [])
        strategy_instance_id = str(extras.get("strategy_instance_id") or "").strip()
        if not strategy_instance_id:
            if strategy_run_id and not generated_strategy_run:
                strategy_instance_id = strategy_run_id
                source = "strategy_run_id"
            else:
                strategy_instance_id = str(envelope.strategy_id or "").strip()
                source = "strategy_id"
            if strategy_instance_id:
                extras["strategy_instance_id"] = strategy_instance_id
                extras.setdefault("owner_strategy_instance_source", source)
        return extras

    async def _ensure_ref_px_and_notional(self, envelope: DecisionEnvelope, extras: Dict[str, Any]) -> Dict[str, Any]:
        is_option = bool(
            str(extras.get("instrument_type") or "").strip().upper() == "OPTION"
            or str(envelope.symbol or "").strip().upper().startswith("OPT:")
        )
        contracts_mult = 1.0
        if is_option:
            try:
                contracts_mult = float(extras.get("contracts_mult") or 100.0)
            except Exception:
                contracts_mult = 100.0

        ref_px = extras.get("ref_px")
        ref_px_source = str(extras.get("ref_px_source") or "").strip()
        if not (ref_px and float(ref_px) > 0):
            if is_option:
                try:
                    opt_mid = extras.get("opt_mid_px")
                    if opt_mid is not None and float(opt_mid) > 0:
                        ref_px = float(opt_mid)
                        ref_px_source = "opt_mid_px"
                except Exception:
                    pass
            try:
                ms = None
                if self.validator is not None and hasattr(self.validator, "ms"):
                    ms = await self.validator.ms.get(envelope.symbol)
                elif self.translator is not None and hasattr(self.translator, "ms"):
                    ms = await self.translator.ms.get(envelope.symbol)
                ms = ms or {}
                bid_px = ms.get("bid_px") or 0.0
                ask_px = ms.get("ask_px") or 0.0
                mid_px = ms.get("mid_px") or 0.0
                last_px = ms.get("last_px") or 0.0
                if mid_px and mid_px > 0:
                    ref_px = mid_px
                    ref_px_source = "mid_px"
                elif last_px and last_px > 0:
                    ref_px = last_px
                    ref_px_source = "last_px"
                elif bid_px and ask_px and bid_px > 0 and ask_px > 0:
                    ref_px = (bid_px + ask_px) / 2.0
                    ref_px_source = "bid_ask_mid"
            except Exception:
                ref_px = ref_px if ref_px else 0.0

        if ref_px and float(ref_px) > 0:
            extras.setdefault("ref_px", float(ref_px))
            if ref_px_source:
                extras.setdefault("ref_px_source", ref_px_source)

        intent_notional = 0.0
        try:
            if ref_px and float(ref_px) > 0:
                intent_notional = abs(float(envelope.target_qty)) * float(ref_px) * float(contracts_mult)
        except Exception:
            intent_notional = 0.0
        if intent_notional > 0:
            extras.setdefault("intent_notional_usd", intent_notional)
            if float(getattr(envelope, "max_notional", 0.0) or 0.0) <= 0:
                envelope.max_notional = float(intent_notional)
        return extras

    def _default_translator_config(self) -> Dict[str, Any]:
        """Default translator configuration"""
        return {
            "buckets": [
                {
                    "name": "tight_spread_low_vol",
                    "when": {
                        "spread_bp": {"lte": 5},
                        "atr_bp": {"lte": 8}
                    },
                    "action": {
                        "order_type": "LMT",
                        "ref": "mid",
                        "offset_ticks": 1,
                        "tif": "DAY",
                        "child": "NONE"
                    }
                },
                {
                    "name": "tight_spread_hi_vol",
                    "when": {
                        "spread_bp": {"lte": 5},
                        "atr_bp": {"gt": 8}
                    },
                    "action": {
                        "order_type": "STP_LMT",
                        "ref": "mid",
                        "offset_ticks": 2,
                        "stop_offset_ticks": 3,
                        "tif": "DAY",
                        "child": "NONE"
                    }
                },
                {
                    "name": "wide_spread",
                    "when": {
                        "spread_bp": {"gt": 5}
                    },
                    "action": {
                        "order_type": "LMT",
                        "ref": "best",
                        "offset_ticks": 0,
                        "tif": "DAY",
                        "child": "NONE"
                    }
                }
            ],
            "defaults": {
                "price_collar_pct": 3.0,
                "oco": {
                    "take_profit_bp": 40,
                    "hard_stop_bp": 80,
                    "trail_atr_mult": 1.5
                }
            }
        }

    async def process_decision(self, envelope: DecisionEnvelope) -> PipelineResult:
        """
        Process a decision through the pipeline

        Args:
            envelope: Decision envelope

        Returns:
            Pipeline result
        """
        start_time = asyncio.get_event_loop().time()
        self.stats["decisions_total"] += 1

        decision_id = envelope.decision_id
        strategy_id = envelope.strategy_id
        symbol = envelope.symbol
        envelope_extras = dict(envelope.extras or {})
        envelope_extras = self._ensure_trace_extras(envelope, envelope_extras)
        envelope_extras = self._ensure_intent_meta(envelope_extras)
        envelope_extras = self._ensure_owner_identity(envelope, envelope_extras)
        envelope.extras = envelope_extras
        self._emit_local_audit(
            stage="pipeline_received",
            envelope=envelope,
            status="received",
            payload={
                "intent_action": envelope_extras.get("intent_action"),
                "intent_source": envelope_extras.get("intent_source"),
                "instrument_type": envelope_extras.get("instrument_type"),
                "target_qty": float(envelope.target_qty or 0.0),
                "order_style": getattr(getattr(envelope, "order_style", None), "kind", None),
            },
        )
        is_option = bool(
            str(envelope_extras.get("instrument_type") or "").strip().upper() == "OPTION"
            or str(envelope.symbol or "").strip().upper().startswith("OPT:")
        )
        bars_symbol = str(
            (envelope_extras.get("bars_symbol") or envelope_extras.get("underlying") or envelope.symbol) or ""
        ).strip().upper()
        if envelope_extras.get("trace_compat_mode"):
            logger.warning(
                "trace_fields_autofilled",
                decision_id=decision_id,
                strategy_id=strategy_id,
                symbol=symbol,
                missing=envelope_extras.get("trace_missing_fields"),
                trace_source=envelope_extras.get("trace_source"),
            )
            if self.trace_validation_blocking:
                reason = "trace_fields_missing"
                result = PipelineResult(
                    status="REJECTED",
                    decision_id=decision_id,
                    strategy_id=strategy_id,
                    symbol=symbol,
                    stage="trace",
                    reasons=[reason],
                    metadata={
                        "missing": envelope_extras.get("trace_missing_fields"),
                        "trace_source": envelope_extras.get("trace_source"),
                    },
                )
                pipeline_decisions_total.labels(
                    strategy_id=strategy_id,
                    status="rejected_trace"
                ).inc()
                if self.dry_run_mode:
                    logger.info(
                        "Pipeline decision (DRY_RUN)",
                        decision_id=decision_id,
                        strategy_id=strategy_id,
                        symbol=symbol,
                        status="REJECTED",
                        stage="trace",
                        reasons=[reason],
                    )
                else:
                    logger.info(
                        "Pipeline decision rejected",
                        decision_id=decision_id,
                        strategy_id=strategy_id,
                        symbol=symbol,
                        stage="trace",
                        reasons=[reason],
                    )
                self._emit_local_audit(
                    stage="pipeline_rejected",
                    envelope=envelope,
                    status="rejected",
                    reason=reason,
                    payload={
                        "stage": "trace",
                        "missing": envelope_extras.get("trace_missing_fields"),
                        "trace_source": envelope_extras.get("trace_source"),
                    },
                )
                return result
        is_safety_intent = bool(
            envelope_extras.get("flatten_action")
            or envelope_extras.get("protective_stop")
            or envelope_extras.get("close_only")
            or str(envelope_extras.get("intent_action") or "").strip().lower() in {"cancel", "protection", "exit", "flatten", "close_only"}
            or str(envelope_extras.get("uom_intent_kind") or "").strip().lower() in {"cancel", "protection", "exit", "flatten"}
        )
        if not is_safety_intent:
            try:
                from services.data_availability import get_data_availability_service

                data_snapshot = get_data_availability_service().snapshot()
                scoped_gates = []
                for gate in list(data_snapshot.get("gates") or []):
                    if not isinstance(gate, dict):
                        continue
                    if str(gate.get("severity") or "") != "entry_blocking":
                        continue
                    if str(gate.get("dataset") or "") == "strategy_tradability":
                        gate_strategy_id = str(gate.get("strategy_id") or "").strip()
                        if gate_strategy_id and gate_strategy_id != strategy_id:
                            continue
                    elif not bool(gate.get("rth")):
                        continue
                    scoped_gates.append(gate)
                if bool(data_snapshot.get("durable_state_write_failed")) or scoped_gates:
                    scoped_snapshot = dict(data_snapshot)
                    scoped_snapshot["gates"] = scoped_gates
                    scoped_snapshot["entry_blocking_count"] = len(scoped_gates)
                    scoped_snapshot["rth_entry_blocking_count"] = len(scoped_gates)
                    reason = "data_availability_entry_blocking"
                    result = PipelineResult(
                        status="REJECTED",
                        decision_id=decision_id,
                        strategy_id=strategy_id,
                        symbol=symbol,
                        stage="data_availability",
                        reasons=[reason],
                        metadata={"data_availability": scoped_snapshot},
                    )
                    pipeline_decisions_total.labels(
                        strategy_id=strategy_id,
                        status="rejected_data_availability",
                    ).inc()
                    self._emit_local_audit(
                        stage="pipeline_rejected",
                        envelope=envelope,
                        status="rejected",
                        reason=reason,
                        severity="warning",
                        payload={"stage": "data_availability", "data_availability": scoped_snapshot},
                    )
                    return result
            except Exception as exc:
                logger.warning(
                    "data_availability_gate_check_failed",
                    decision_id=decision_id,
                    strategy_id=strategy_id,
                    symbol=symbol,
                    error=str(exc),
                )

        validate_result = None
        if not self.bypass_validator:
            # Stage 1: Validate
            with pipeline_latency_seconds.labels(stage="validator").time():
                validate_result = await self.validator.validate(envelope)

            if not validate_result.ok:
                self.stats["decisions_rejected"] += 1
                self.stats["validator_rejects"] += 1

                result = PipelineResult(
                    status="REJECTED",
                    decision_id=decision_id,
                    strategy_id=strategy_id,
                    symbol=symbol,
                    stage="validator",
                    reasons=validate_result.skip_reasons,
                    metadata={"ttl_ms": validate_result.effective_ttl_ms}
                )

                pipeline_decisions_total.labels(
                    strategy_id=strategy_id,
                    status="rejected_validator"
                ).inc()

                if self.dry_run_mode:
                    logger.info("Pipeline decision (DRY_RUN)",
                               decision_id=decision_id,
                               strategy_id=strategy_id,
                               symbol=symbol,
                               status="REJECTED",
                               stage="validator",
                               reasons=validate_result.skip_reasons,
                               trace_id=envelope_extras.get("trace_id"),
                               trigger_id=envelope_extras.get("trigger_id"))
                else:
                    logger.info("Pipeline decision rejected",
                               decision_id=decision_id,
                               strategy_id=strategy_id,
                               symbol=symbol,
                               stage="validator",
                               reasons=validate_result.skip_reasons,
                               trace_id=envelope_extras.get("trace_id"),
                               trigger_id=envelope_extras.get("trigger_id"))
                    logger.warning(
                        "pipeline_gate",
                        decision_id=decision_id,
                        strategy_id=strategy_id,
                        symbol=symbol,
                        stage="validator",
                        reasons=validate_result.skip_reasons,
                        trace_id=envelope_extras.get("trace_id"),
                        trigger_id=envelope_extras.get("trigger_id"),
                    )
                self._emit_local_audit(
                    stage="pipeline_rejected",
                    envelope=envelope,
                    status="rejected",
                    reason="validator_rejected",
                    payload={
                        "stage": "validator",
                        "reasons": list(validate_result.skip_reasons or []),
                        "ttl_ms": validate_result.effective_ttl_ms,
                    },
                )

                return result
        else:
            logger.warning(
                "Pipeline validator bypassed",
                decision_id=decision_id,
                strategy_id=strategy_id,
                symbol=symbol,
            )

        # Set cost_estimates on envelope (used by arbiter/translator)
        from libs.strategy.base import CostEstimates
        if not self.bypass_validator:
            if is_option and bars_symbol:
                market = await self.validator.ms.get(bars_symbol) or {}
                bars = await self.validator.bars.window(
                    bars_symbol,
                    end_us=envelope.ts_bar_end_us,
                    count=self.validator.cfg.min_hist_bars
                )
                # Use option bid/ask when provided for spread-based slippage.
                market_for_cost = dict(market)
                try:
                    opt_bid = float(envelope_extras.get("opt_bid_px") or 0.0)
                    opt_ask = float(envelope_extras.get("opt_ask_px") or 0.0)
                    if opt_bid > 0 and opt_ask > 0:
                        market_for_cost["bid_px"] = opt_bid
                        market_for_cost["ask_px"] = opt_ask
                        market_for_cost["mid_px"] = (opt_bid + opt_ask) / 2.0
                except Exception:
                    market_for_cost = market
                cost_dict = self.validator.cost.estimate(envelope, market=market_for_cost, bars=bars)
            else:
                market = await self.validator.ms.get(envelope.symbol) or {}
                bars = await self.validator.bars.window(
                    envelope.symbol,
                    end_us=envelope.ts_bar_end_us,
                    count=self.validator.cfg.min_hist_bars
                )
                cost_dict = self.validator.cost.estimate(envelope, market=market, bars=bars)
            envelope.cost_estimates = CostEstimates(
                slippage_bp=cost_dict.get("slippage_bp", 5.0),
                fees_bp=cost_dict.get("fees_bp", 0.3),
                impact_bp=cost_dict.get("impact_bp", 0.0),
                confidence=cost_dict.get("confidence", 0.6)
            )
        elif envelope.cost_estimates is None:
            envelope.cost_estimates = CostEstimates(
                slippage_bp=5.0,
                fees_bp=0.3,
                impact_bp=0.0,
                confidence=0.6,
            )

        # Ensure ref_px + notional before arbiter/risk.
        envelope_extras = await self._ensure_ref_px_and_notional(envelope, envelope_extras)
        envelope.extras = envelope_extras
        envelope.target_qty = stabilize_requested_qty(
            envelope.target_qty,
            symbol=str(envelope.symbol or ""),
            extras=envelope_extras,
        )

        admit_ok = True
        admit_qty = float(envelope.target_qty)
        admit_meta: Dict[str, Any] = {"reason": "bypass_arbiter"}
        arbiter_admitted = False
        if is_safety_intent:
            # Safety intents are risk-reducing and must not be blocked by
            # multi-strategy coordination/capacity gates.
            admit_ok = True
            admit_qty = abs(float(envelope.target_qty))
            admit_meta = {"reason": "safety_bypass_arbiter"}
        elif not self.bypass_arbiter:
            # Stage 2: Arbitrate
            with pipeline_latency_seconds.labels(stage="arbiter").time():
                admit_ok, admit_qty, admit_meta = await self.arbiter.admit(envelope)

            if not admit_ok:
                self.stats["decisions_deferred"] += 1
                self.stats["arbiter_rejects"] += 1

                result = PipelineResult(
                    status="DEFERRED",
                    decision_id=decision_id,
                    strategy_id=strategy_id,
                    symbol=symbol,
                    stage="arbiter",
                    reasons=[admit_meta.get("reason", "unknown")],
                    metadata=admit_meta
                )

                pipeline_decisions_total.labels(
                    strategy_id=strategy_id,
                    status="deferred_arbiter"
                ).inc()

                if self.dry_run_mode:
                    logger.info("Pipeline decision (DRY_RUN)",
                               decision_id=decision_id,
                               strategy_id=strategy_id,
                               symbol=symbol,
                               status="DEFERRED",
                               stage="arbiter",
                               reasons=[admit_meta.get("reason")],
                               trace_id=envelope_extras.get("trace_id"),
                               trigger_id=envelope_extras.get("trigger_id"))
                else:
                    logger.info("Pipeline decision deferred",
                               decision_id=decision_id,
                               strategy_id=strategy_id,
                               symbol=symbol,
                               stage="arbiter",
                               reason=admit_meta.get("reason"),
                               trace_id=envelope_extras.get("trace_id"),
                               trigger_id=envelope_extras.get("trigger_id"))
                    logger.warning(
                        "pipeline_gate",
                        decision_id=decision_id,
                        strategy_id=strategy_id,
                        symbol=symbol,
                        stage="arbiter",
                        reasons=[admit_meta.get("reason")],
                        trace_id=envelope_extras.get("trace_id"),
                        trigger_id=envelope_extras.get("trigger_id"),
                    )
                self._emit_local_audit(
                    stage="pipeline_deferred",
                    envelope=envelope,
                    status="deferred",
                    reason=str(admit_meta.get("reason") or "unknown"),
                    payload={
                        "stage": "arbiter",
                        "metadata": dict(admit_meta or {}),
                    },
                )

                return result
            arbiter_admitted = True
        else:
            logger.warning(
                "Pipeline arbiter bypassed",
                decision_id=decision_id,
                strategy_id=strategy_id,
                symbol=symbol,
                target_qty=admit_qty,
            )

        admit_qty = abs(
            conservative_execution_qty(
                admit_qty,
                symbol=str(envelope.symbol or ""),
                extras=envelope_extras,
            )
        )

        # Update envelope with arbitrated quantity.
        # IMPORTANT: Preserve direction (SELL must not flip to BUY).
        orig_sign = 1.0 if float(envelope.target_qty) >= 0 else -1.0
        envelope.target_qty = orig_sign * float(admit_qty)

        # Stage 3: Translate
        with pipeline_latency_seconds.labels(stage="translator").time():
            exec_plan = await self.translator.translate(envelope)

        self.stats["decisions_approved"] += 1

        result = PipelineResult(
            status="APPROVED",
            decision_id=decision_id,
            strategy_id=strategy_id,
            symbol=symbol,
            stage="translator",
            plan=exec_plan,
            metadata={
                "validate_result": validate_result,
                "arbiter_meta": admit_meta,
                "admitted_qty": admit_qty
                ,
                # Pass through original intent hints for downstream execution controls
                # (e.g., disable OCO, flatten_action, protective_stop).
                "envelope_extras": envelope_extras,
                "envelope_order_style": {
                    "kind": getattr(getattr(envelope, "order_style", None), "kind", None),
                    "limit_px": getattr(getattr(envelope, "order_style", None), "limit_px", None),
                    "stop_px": getattr(getattr(envelope, "order_style", None), "stop_px", None),
                },
                # Trace context pass-through for execution ingress enrichment.
                "strategy_run_id": (envelope.extras or {}).get("strategy_run_id"),
                "trigger_id": (envelope.extras or {}).get("trigger_id"),
                "trace_id": (envelope.extras or {}).get("trace_id"),
                "causation_id": (envelope.extras or {}).get("causation_id"),
                "ts_bar_end_us": envelope.ts_bar_end_us,
            }
        )

        pipeline_decisions_total.labels(
            strategy_id=strategy_id,
            status="approved"
        ).inc()

        total_latency = asyncio.get_event_loop().time() - start_time

        if self.dry_run_mode:
            logger.info("Pipeline decision (DRY_RUN)",
                       decision_id=decision_id,
                       strategy_id=strategy_id,
                       symbol=symbol,
                       status="APPROVED",
                       order_type=exec_plan.order_type,
                       side=exec_plan.side,
                       qty=exec_plan.qty,
                       limit_px=exec_plan.limit_px,
                       latency_ms=total_latency * 1000,
                       trace_id=envelope_extras.get("trace_id"),
                       trigger_id=envelope_extras.get("trigger_id"))
            self._emit_uom_route_audit(
                envelope=envelope,
                result=result,
                status="skipped",
                target=None,
                reason="execution_mode_shadow_no_delegate",
                shadow_mode=True,
            )
            if arbiter_admitted:
                await self.arbiter.finalize(
                    decision_id,
                    "SHADOW_APPROVED" if self.shadow_mode else "DRY_RUN_APPROVED",
                )
        else:
            logger.info("Pipeline decision approved",
                       decision_id=decision_id,
                       strategy_id=strategy_id,
                       symbol=symbol,
                       order_type=exec_plan.order_type,
                       side=exec_plan.side,
                       qty=exec_plan.qty,
                       limit_px=exec_plan.limit_px,
                       latency_ms=total_latency * 1000,
                       trace_id=envelope_extras.get("trace_id"),
                       trigger_id=envelope_extras.get("trigger_id"))
            self._emit_local_audit(
                stage="pipeline_approved",
                envelope=envelope,
                status="approved",
                payload={
                    "order_type": exec_plan.order_type,
                    "side": exec_plan.side,
                    "qty": exec_plan.qty,
                    "limit_px": exec_plan.limit_px,
                    "latency_ms": total_latency * 1000,
                },
            )

            # Execute the order if an execution delegate is wired.
            if self.execution_service is not None:
                try:
                    exec_result = await self._execute_with_uom_delegate(result, envelope=envelope)
                    logger.info("Order execution completed",
                               decision_id=decision_id,
                               success=exec_result.success,
                               order_id=exec_result.order_id,
                               reason=exec_result.reason)
                    self._emit_local_audit(
                        stage="pipeline_execution_result",
                        envelope=envelope,
                        status="submitted" if exec_result.success else "failed",
                        reason=str(exec_result.reason or ""),
                        severity="info" if exec_result.success else "warning",
                        payload={
                            "success": bool(exec_result.success),
                            "execution_stage": str(exec_result.stage or ""),
                            "order_id": exec_result.order_id,
                            "idempotency_key": exec_result.idempotency_key,
                            "metadata": dict(exec_result.metadata or {}),
                        },
                    )
                    result.metadata["execution_result"] = {
                        "success": bool(exec_result.success),
                        "stage": str(exec_result.stage or ""),
                        "reason": str(exec_result.reason or ""),
                        "order_id": exec_result.order_id,
                        "idempotency_key": exec_result.idempotency_key,
                        "metadata": dict(exec_result.metadata or {}),
                    }
                    # IMPORTANT: For active trading, arbiter allocation must be released on
                    # terminal order state (FILLED/CANCELLED/REJECTED), not at SUBMITTED.
                    # Terminal finalization is handled by execution/reconciliation callbacks.
                    if arbiter_admitted and (not exec_result.success):
                        await self.arbiter.finalize(decision_id, "EXEC_REJECTED")
                except Exception as e:
                    logger.error("Order execution failed",
                                decision_id=decision_id,
                                error=str(e),
                                exc_info=True)
                    self._emit_local_audit(
                        stage="pipeline_execution_error",
                        envelope=envelope,
                        status="error",
                        reason=str(e),
                        severity="error",
                        payload={"stage": "execution_service"},
                    )
                    if arbiter_admitted:
                        await self.arbiter.finalize(decision_id, "EXEC_ERROR")
            else:
                self._emit_uom_route_audit(
                    envelope=envelope,
                    result=result,
                    status="skipped" if self.dry_run_mode else "rejected",
                    target=None,
                    reason="execution_mode_shadow_no_delegate" if self.dry_run_mode else "no_delegate_registered",
                    shadow_mode=bool(self.dry_run_mode),
                )
                # No execution service wired (e.g. dry_run-style integration):
                # release arbiter slot to avoid stale in-memory blockers.
                if arbiter_admitted:
                    await self.arbiter.finalize(decision_id, "NO_EXEC")

        return result

    def set_execution_service(self, execution_service) -> None:
        """
        Wire the execution delegate to the pipeline

        Args:
            execution_service: execution delegate instance
        """
        self.execution_service = execution_service
        logger.info("Execution delegate wired to DecisionPipeline",
                   dry_run_mode=self.dry_run_mode)

    def _uom_owner_identity_complete(self, result: PipelineResult) -> bool:
        metadata = dict(result.metadata or {})
        extras = dict(metadata.get("envelope_extras") or {})
        return bool(
            str(extras.get("account_id") or "").strip()
            and str(extras.get("strategy_instance_id") or extras.get("strategy_run_id") or "").strip()
            and str(result.symbol or "").strip()
            and (
                str(extras.get("intent_id") or "").strip()
                or str(extras.get("client_order_id") or "").strip()
                or str(result.decision_id or "").strip()
            )
        )

    def _emit_uom_route_audit(
        self,
        *,
        envelope: DecisionEnvelope,
        result: PipelineResult,
        status: str,
        target: Optional[str],
        reason: Optional[str] = None,
        shadow_mode: bool = False,
    ) -> None:
        metadata = dict(result.metadata or {})
        extras = dict(metadata.get("envelope_extras") or {})
        self._emit_local_audit(
            stage={
                "delegated": "uom_delegated",
                "delegated_shadow": "uom_delegated_shadow",
                "skipped": "uom_skipped_shadow",
                "rejected": "uom_rejected",
            }.get(status, "uom_route"),
            envelope=envelope,
            status=status,
            reason=reason,
            severity="info" if status in {"delegated", "delegated_shadow", "skipped"} else "warning",
            payload={
                "intent_id": extras.get("intent_id") or result.decision_id,
                "client_order_id": extras.get("client_order_id"),
                "trace_id": extras.get("trace_id"),
                "decision_id": result.decision_id,
                "strategy_id": result.strategy_id,
                "strategy_instance_id": extras.get("strategy_instance_id") or extras.get("strategy_run_id"),
                "symbol": result.symbol,
                "asset_type": "option" if str(result.symbol or "").upper().startswith("OPT:") else str(extras.get("instrument_type") or "stock").lower(),
                "uom_route_status": status,
                "uom_delegate_target": target,
                "uom_reject_reason": reason,
                "uom_shadow_mode": bool(shadow_mode),
                "owner_identity_complete": self._uom_owner_identity_complete(result),
            },
        )

    async def _execute_with_uom_delegate(self, result: PipelineResult, *, envelope: DecisionEnvelope | None = None):
        delegate = self.execution_service
        if delegate is None:
            raise RuntimeError("execution_delegate_missing")
        metadata = dict(result.metadata or {})
        extras = dict(metadata.get("envelope_extras") or {})
        explicit_kind = str(
            extras.get("uom_intent_kind")
            or extras.get("intent_action")
            or extras.get("action_type")
            or extras.get("order_role")
            or ""
        ).strip().lower()
        if explicit_kind in {"entry", "add", "open", "buy_to_open", "sell_to_open"} and hasattr(delegate, "submit_entry"):
            if envelope is not None:
                self._emit_uom_route_audit(envelope=envelope, result=result, status="delegated", target="submit_entry")
            return await delegate.submit_entry(result)
        if explicit_kind in {"exit", "reduce", "close", "buy_to_close", "sell_to_close"} and hasattr(delegate, "submit_exit"):
            if envelope is not None:
                self._emit_uom_route_audit(envelope=envelope, result=result, status="delegated", target="submit_exit")
            return await delegate.submit_exit(result)
        if bool(extras.get("flatten_action")) and hasattr(delegate, "submit_flatten"):
            if envelope is not None:
                self._emit_uom_route_audit(envelope=envelope, result=result, status="delegated", target="submit_flatten")
            return await delegate.submit_flatten(result)
        if bool(extras.get("protective_stop")) and hasattr(delegate, "ensure_protection"):
            if envelope is not None:
                self._emit_uom_route_audit(envelope=envelope, result=result, status="delegated", target="ensure_protection")
            return await delegate.ensure_protection(result)
        if bool(extras.get("close_only")) and hasattr(delegate, "submit_exit"):
            if envelope is not None:
                self._emit_uom_route_audit(envelope=envelope, result=result, status="delegated", target="submit_exit")
            return await delegate.submit_exit(result)
        plan = result.plan
        if plan is not None:
            try:
                side = str(getattr(plan, "side", "") or "").strip().upper()
                qty = float(getattr(plan, "qty", 0.0) or 0.0)
            except Exception:
                side = ""
                qty = 0.0
            if side == "SELL" and qty > 0.0 and hasattr(delegate, "submit_exit"):
                if envelope is not None:
                    self._emit_uom_route_audit(envelope=envelope, result=result, status="delegated", target="submit_exit")
                return await delegate.submit_exit(result)
        if hasattr(delegate, "submit_entry"):
            if envelope is not None:
                self._emit_uom_route_audit(envelope=envelope, result=result, status="delegated", target="submit_entry")
            return await delegate.submit_entry(result)
        if envelope is not None:
            self._emit_uom_route_audit(
                envelope=envelope,
                result=result,
                status="delegated" if hasattr(delegate, "execute") else "rejected",
                target="execute" if hasattr(delegate, "execute") else None,
                reason=None if hasattr(delegate, "execute") else "no_supported_delegate_method",
            )
        return await delegate.execute(result)

    async def finalize_decision(self, decision_id: str, status: str) -> None:
        """
        Finalize a decision (notify arbiter)

        Args:
            decision_id: Decision ID
            status: FILLED/CANCELLED/REJECTED
        """
        await self.arbiter.finalize(decision_id, status)

    def get_stats(self) -> Dict[str, Any]:
        """Get pipeline statistics"""
        return self.stats.copy()

    async def shutdown(self) -> None:
        """Shutdown pipeline service"""
        logger.info("DecisionPipelineService shutting down")
