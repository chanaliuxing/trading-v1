"""
Rule DSL Engine
Based on doc/65.md specification

Declarative rule engine for trading tactics (stop-loss, take-profit, layering, reversal).

Features:
- YAML-based rule templates
- Safe expression evaluation
- Multi-rule composition with priority
- OCO/reversal/TTL integration
- Audit trail for rule execution
"""
import math
import uuid
import time
from typing import Any, Dict, List, Optional
from dataclasses import dataclass

from libs.logger import get_logger

logger = get_logger(__name__)


# Safe globals for expression evaluation
SAFE_GLOBALS = {
    "abs": abs,
    "max": max,
    "min": min,
    "floor": lambda x, r=1: math.floor(x / r) * r,  # floor(value, round_to)
    "ceil": math.ceil,
    "round": round,
    "sign": lambda x: (0 if x == 0 else (1 if x > 0 else -1)),
    "clamp": lambda v, lo, hi: max(lo, min(hi, v)),
    "sqrt": math.sqrt,
    "pow": math.pow,
    "int": int,
    "float": float
}


@dataclass
class RuleContext:
    """Context for rule evaluation"""
    strategy_id: str
    symbol: str
    features: Dict[str, Any]
    signal: Dict[str, Any]
    position: Dict[str, Any]
    limits: Dict[str, Any]
    defaults: Dict[str, Any]


@dataclass
class RuleTrace:
    """Audit trace for rule execution"""
    symbol: str
    template: str
    hits: List[str]
    vars: Dict[str, Any]
    decisions: List[Dict[str, Any]]
    qty_before: int
    qty_after: int
    duration_ms: float


class RuleEngine:
    """
    Declarative rule engine for trading tactics

    Evaluates YAML-based rule templates to generate DecisionEnvelopes.
    """

    def __init__(
        self,
        templates: Dict[str, dict],
        audit_writer: Optional[Any] = None
    ):
        """
        Initialize rule engine

        Args:
            templates: Dict of template_name -> template_config (from YAML)
            audit_writer: Optional audit log writer
        """
        self.templates = templates
        self.audit = audit_writer

        logger.info(
            "RuleEngine initialized",
            templates=list(templates.keys())
        )

    def _eval(self, expr: Any, scope: Dict[str, Any]) -> Any:
        """
        Safely evaluate expression

        Args:
            expr: Expression (string or literal)
            scope: Variable scope

        Returns:
            Evaluated value
        """
        # Literals
        if isinstance(expr, (int, float, bool)) or expr is None:
            return expr

        if not isinstance(expr, str):
            return expr

        # Safe eval with no __builtins__
        try:
            return eval(expr, {"__builtins__": {}}, {**SAFE_GLOBALS, **scope})
        except Exception as e:
            logger.error("Expression eval failed", expr=expr, error=str(e))
            return None

    def _check_condition(self, cond: Any, scope: Dict[str, Any]) -> bool:
        """
        Check condition (supports all/any/single expression)

        Args:
            cond: Condition dict or expression
            scope: Variable scope

        Returns:
            True if condition passes
        """
        if isinstance(cond, dict):
            # Logical all
            if "all" in cond:
                return all(self._eval(e, scope) for e in cond["all"])

            # Logical any
            if "any" in cond:
                return any(self._eval(e, scope) for e in cond["any"])

        # Single expression
        return bool(self._eval(cond, scope))

    def _map_expr(self, d: Dict[str, Any], scope: Dict[str, Any]) -> Dict[str, Any]:
        """
        Recursively evaluate expressions in dict (for layers)

        Args:
            d: Dict with expressions
            scope: Variable scope

        Returns:
            Dict with evaluated values
        """
        out = {}
        for k, v in d.items():
            if isinstance(v, str):
                out[k] = self._eval(v, scope)
            elif isinstance(v, dict):
                out[k] = self._map_expr(v, scope)
            elif isinstance(v, list):
                out[k] = [self._map_expr(item, scope) if isinstance(item, dict) else self._eval(item, scope) for item in v]
            else:
                out[k] = v
        return out

    def _apply_lot_constraints(
        self,
        qty: int,
        min_lot: int,
        lot_round: int
    ) -> int:
        """
        Apply lot size constraints

        Args:
            qty: Raw quantity
            min_lot: Minimum lot size
            lot_round: Lot rounding

        Returns:
            Constrained quantity
        """
        # Round down to lot_round
        if qty > 0:
            qty = (qty // lot_round) * lot_round
        else:
            qty = -((abs(qty) // lot_round) * lot_round)

        # Check minimum
        if abs(qty) < min_lot:
            return 0

        return qty

    def _build_oco_config(
        self,
        scope: Dict[str, Any],
        oco_cfg: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Build OCO configuration from scope and rule config

        Args:
            scope: Variable scope (with defaults)
            oco_cfg: Optional OCO config from rule

        Returns:
            OCO configuration dict
        """
        # Default OCO from scope
        default_oco = {
            "trail_atr_mult": float(scope.get("trail_atr_mult", 1.4)),
            "time_stop_s": int(scope.get("time_limit_s", 900))
        }

        # If no custom config, return default
        if not oco_cfg:
            return default_oco

        # Evaluate custom config expressions
        custom = {}
        for k, v in oco_cfg.items():
            if isinstance(v, str):
                custom[k] = float(self._eval(v, scope))
            else:
                custom[k] = v

        # Merge (custom overrides default)
        return {**default_oco, **custom}

    def _make_envelope(
        self,
        ctx: RuleContext,
        emit: Dict[str, Any],
        scope: Dict[str, Any],
        target_qty: int,
        protect: Optional[Dict[str, Any]] = None,
        reverse: bool = False
    ) -> Dict[str, Any]:
        """
        Create DecisionEnvelope from rule emit config

        Args:
            ctx: Rule context
            emit: Emit config from rule
            scope: Variable scope
            target_qty: Target quantity
            protect: Optional OCO protection config
            reverse: If True, this is a reversal

        Returns:
            DecisionEnvelope dict
        """
        # Order style
        style = emit.get("style", {"kind": "LMT", "limit_px": "signal.ref_px"})
        kind = style.get("kind", "LMT")

        limit_px = None
        if kind in ("LMT", "LMTMKT"):
            limit_px = float(self._eval(style.get("limit_px", "signal.ref_px"), scope))

        # Build envelope
        envelope = {
            "decision_id": str(uuid.uuid4()),
            "strategy_id": ctx.strategy_id,
            "symbol": ctx.symbol,
            "ts_bar_end_us": int(ctx.signal["ts_end"]),
            "target_qty": float(target_qty),
            "tif": "DAY",
            "order_style": {
                "kind": kind,
                "limit_px": limit_px
            },
            "freshness": {
                "ttl_ms": int(scope.get("ttl_ms", 2000))
            },
            "extras": {
                "ref_px": float(ctx.signal["ref_px"]),
                "edge_bp": float(abs(ctx.features.get("zscore_20", 0)) * 10),
                "oco_cfg": self._build_oco_config(scope, protect),
                "reverse_atomic": bool(reverse and scope.get("reversal_atomic", True))
            }
        }

        return envelope

    async def evaluate(
        self,
        ctx: RuleContext
    ) -> List[Dict[str, Any]]:
        """
        Evaluate rule template and generate DecisionEnvelopes

        Args:
            ctx: Rule context

        Returns:
            List of DecisionEnvelope dicts
        """
        start_ms = time.time() * 1000

        # Get template
        template_name = ctx.defaults.get("template_name")
        if not template_name or template_name not in self.templates:
            logger.warning("Template not found", template=template_name)
            return []

        template = self.templates[template_name]

        # Build scope
        scope = {
            "f": ctx.features,
            "signal": ctx.signal,
            "position": ctx.position,
            **template.get("defaults", {}),
            **ctx.defaults
        }

        # Trace for audit
        trace = RuleTrace(
            symbol=ctx.symbol,
            template=template_name,
            hits=[],
            vars={},
            decisions=[],
            qty_before=ctx.position.get("qty", 0),
            qty_after=0,
            duration_ms=0
        )

        out_envs: List[Dict[str, Any]] = []

        # Evaluate rules in order
        rules = template.get("rules", [])

        for rule in rules:
            rule_id = rule.get("id", "unknown")

            # Check condition
            cond = rule.get("when")
            if cond:
                ok = self._check_condition(cond, scope)
                if not ok:
                    logger.debug("Rule condition not met", rule_id=rule_id)
                    continue

            # Compute variables
            compute = rule.get("compute", {})
            for k, v in compute.items():
                if isinstance(v, list):
                    # List of dicts (e.g., layers)
                    scope[k] = [self._map_expr(item, scope) if isinstance(item, dict) else self._eval(item, scope) for item in v]
                else:
                    scope[k] = self._eval(v, scope)

            # Emit decision
            emit = rule.get("emit")
            if emit:
                emit_type = emit.get("type")

                if emit_type == "target":
                    # Target quantity
                    qty_expr = emit.get("qty", 0)
                    qty_raw = int(self._eval(qty_expr, scope) or 0)

                    # Apply lot constraints
                    qty = self._apply_lot_constraints(
                        qty_raw,
                        min_lot=int(scope.get("min_lot", 1)),
                        lot_round=int(scope.get("lot_round", 1))
                    )

                    # Only emit if different from current position
                    if qty != ctx.position.get("qty", 0):
                        env = self._make_envelope(ctx, emit, scope, qty)
                        out_envs = [env]  # Last target wins
                        trace.decisions.append({"type": "target", "qty": qty, "qty_raw": qty_raw})

                elif emit_type == "protect":
                    # Protection (OCO) update
                    oco_cfg = emit.get("oco", {})
                    env = self._make_envelope(
                        ctx,
                        {"style": {"kind": "NOOP"}},
                        scope,
                        target_qty=ctx.position.get("qty", 0),
                        protect=oco_cfg
                    )
                    out_envs.append(env)
                    trace.decisions.append({"type": "protect", "oco": oco_cfg})

                elif emit_type == "reverse":
                    # Reversal (atomic)
                    qty_raw = -ctx.position.get("qty", 0)
                    qty = self._apply_lot_constraints(
                        qty_raw,
                        min_lot=int(scope.get("min_lot", 1)),
                        lot_round=int(scope.get("lot_round", 1))
                    )

                    env = self._make_envelope(
                        ctx,
                        emit,
                        scope,
                        target_qty=qty,
                        reverse=True
                    )
                    out_envs = [env]  # Reversal overrides
                    trace.decisions.append({"type": "reverse", "qty": qty})

            # Mark hit
            trace.hits.append(rule_id)

            # Check for stop
            if rule.get("then") == "stop":
                break

        # Update trace
        trace.qty_after = out_envs[0]["target_qty"] if out_envs else trace.qty_before
        trace.duration_ms = time.time() * 1000 - start_ms

        # Audit
        if self.audit:
            try:
                await self.audit.write_rule_trace(trace)
            except Exception as e:
                logger.error("Failed to write audit trace", error=str(e))

        logger.info(
            "Rule evaluation complete",
            symbol=ctx.symbol,
            template=template_name,
            hits=len(trace.hits),
            decisions=len(trace.decisions),
            duration_ms=round(trace.duration_ms, 2)
        )

        return out_envs
