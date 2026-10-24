"""
Rule DSL Engine
Based on doc/65.md

Declarative rule-based strategy execution with:
- YAML-based rule templates
- Safe expression evaluation
- Multi-layer position sizing
- OCO protection rules
- Atomic reversal logic
- Audit trail generation
"""
import math
import uuid
import time
from typing import Dict, List, Any, Optional
import yaml

from libs.logger import get_logger
from services.strategy.strategy_base import (
    DecisionEnvelope,
    OrderStyle,
    Freshness,
    make_decision_id
)

logger = get_logger(__name__)


# Safe functions for expression evaluation
SAFE_GLOBALS = {
    "abs": abs,
    "max": max,
    "min": min,
    "floor": math.floor,
    "ceil": math.ceil,
    "round": round,
    "sign": lambda x: (0 if x == 0 else (1 if x > 0 else -1)),
    "clamp": lambda v, lo, hi: max(lo, min(hi, v))
}


class RuleEngine:
    """
    Rule-based strategy engine

    Evaluates declarative YAML rules to generate trading decisions.
    Supports:
    - Quality gates
    - Multi-layer sizing
    - OCO protection
    - Atomic reversals
    - Audit trails
    """

    def __init__(
        self,
        templates: Dict[str, dict],
        logger_func: Optional[Any] = None,
        metrics_func: Optional[Any] = None
    ):
        """
        Initialize rule engine

        Args:
            templates: Rule templates (name -> template dict)
            logger_func: Logger function
            metrics_func: Metrics function
        """
        self.templates = templates
        self.log = logger_func or logger
        self.metrics = metrics_func or (lambda: {})

        logger.info(f"RuleEngine initialized with {len(templates)} templates")

    @classmethod
    def from_yaml_file(cls, path: str) -> 'RuleEngine':
        """Load templates from YAML file"""
        with open(path, 'r') as f:
            data = yaml.safe_load(f)

        templates = {}
        if isinstance(data, list):
            # Multiple templates in file
            for tpl in data:
                templates[tpl['name']] = tpl
        else:
            # Single template
            templates[data['name']] = data

        return cls(templates)

    async def evaluate(
        self,
        ctx: Any,
        template_name: str,
        symbol: str,
        features: Dict[str, Any],
        signal: Dict[str, Any]
    ) -> List[DecisionEnvelope]:
        """
        Evaluate rules for a symbol

        Args:
            ctx: Strategy context
            template_name: Template name
            symbol: Symbol
            features: Feature dictionary
            signal: Signal from strategy

        Returns:
            List of decision envelopes
        """
        # Get template
        if template_name not in self.templates:
            logger.warning(f"Template not found: {template_name}")
            return []

        template = self.templates[template_name]
        defaults = template.get('defaults', {})
        rules = template.get('rules', [])

        # Get position
        position_data = await ctx.position(symbol)
        position = {
            'qty': position_data.get('qty', 0.0),
            'avg_px': position_data.get('avg_px', 0.0),
            'upnl': position_data.get('unrealized_pnl_usd', 0.0)
        }

        # Build scope
        scope = {
            'f': features,
            'signal': signal,
            'position': position,
            **defaults
        }

        # Trace for audit
        trace = {
            'symbol': symbol,
            'template': template_name,
            'hits': [],
            'vars': {},
            'decisions': []
        }

        # Evaluate rules
        envelopes: List[DecisionEnvelope] = []
        oco_cfg: Dict[str, Any] = {}

        for rule in rules:
            rule_id = rule.get('id', 'unknown')

            # Evaluate condition
            when = rule.get('when')
            if when:
                ok = self._check_condition(when, scope)
                if not ok:
                    continue

            # Record hit
            trace['hits'].append(rule_id)

            # Compute expressions
            compute = rule.get('compute', {})
            for key, expr in compute.items():
                if isinstance(expr, list):
                    # List of objects with expressions
                    scope[key] = [self._map_expr(item, scope) for item in expr]
                else:
                    scope[key] = self._eval(expr, scope)
                    trace['vars'][key] = scope[key]

            # Handle 'then' directive
            then = rule.get('then')
            if then == 'stop':
                break
            elif then == 'continue':
                continue

            # Emit decision
            emit = rule.get('emit')
            if not emit:
                continue

            emit_type = emit.get('type')

            if emit_type == 'target':
                # Generate target position envelope
                env = self._make_target_envelope(
                    ctx.strategy_id,
                    symbol,
                    emit,
                    scope,
                    defaults
                )
                if env:
                    envelopes.append(env)
                    trace['decisions'].append({
                        'type': 'target',
                        'rule': rule_id,
                        'qty': env.target_qty
                    })

            elif emit_type == 'protect':
                # Update OCO configuration
                oco = emit.get('oco', {})
                for key, expr in oco.items():
                    oco_cfg[key] = self._eval(expr, scope)

                trace['decisions'].append({
                    'type': 'protect',
                    'rule': rule_id,
                    'oco': oco_cfg
                })

            elif emit_type == 'reverse':
                # Atomic reversal: flatten + build
                # For simplicity, just create target envelope with reversed qty
                atomic = self._eval(emit.get('atomic', True), scope)

                if atomic:
                    # Create reversal envelope
                    env = self._make_target_envelope(
                        ctx.strategy_id,
                        symbol,
                        emit,
                        scope,
                        defaults,
                        is_reversal=True
                    )
                    if env:
                        envelopes = [env]  # Replace all previous
                        trace['decisions'].append({
                            'type': 'reverse',
                            'rule': rule_id,
                            'qty': env.target_qty
                        })

        # Apply OCO to all envelopes
        for env in envelopes:
            if oco_cfg:
                env.extras['oco_cfg'] = oco_cfg

        # Log trace
        self.log.debug(
            f"Rule evaluation: {template_name}",
            symbol=symbol,
            hits=trace['hits'],
            decisions=len(envelopes)
        )

        return envelopes

    def _check_condition(self, cond: Dict[str, Any], scope: Dict[str, Any]) -> bool:
        """Check condition (supports 'all', 'any', direct expression)"""
        if 'all' in cond:
            return all(self._eval(expr, scope) for expr in cond['all'])
        elif 'any' in cond:
            return any(self._eval(expr, scope) for expr in cond['any'])
        else:
            # Direct expression
            return bool(self._eval(cond, scope))

    def _eval(self, expr: Any, scope: Dict[str, Any]) -> Any:
        """Safely evaluate expression"""
        # Literal values
        if isinstance(expr, (int, float, bool)) or expr is None:
            return expr

        # Non-string: return as-is
        if not isinstance(expr, str):
            return expr

        # String expression - evaluate safely
        try:
            return eval(expr, {"__builtins__": {}}, {**SAFE_GLOBALS, **scope})
        except Exception as e:
            logger.warning(f"Expression evaluation failed: {expr}", error=str(e))
            return None

    def _map_expr(self, item: Dict[str, Any], scope: Dict[str, Any]) -> Dict[str, Any]:
        """Map expressions in dict"""
        result = {}
        for key, value in item.items():
            result[key] = self._eval(value, scope)
        return result

    def _make_target_envelope(
        self,
        strategy_id: str,
        symbol: str,
        emit: Dict[str, Any],
        scope: Dict[str, Any],
        defaults: Dict[str, Any],
        is_reversal: bool = False
    ) -> Optional[DecisionEnvelope]:
        """Create decision envelope from emit spec"""
        # Calculate quantity
        qty_expr = emit.get('qty')
        if not qty_expr:
            return None

        qty = self._eval(qty_expr, scope)
        if qty is None or qty == 0:
            return None

        # Get order style
        style_spec = emit.get('style', {})
        kind = self._eval(style_spec.get('kind', 'MKT'), scope)
        limit_px = self._eval(style_spec.get('limit_px'), scope)
        stop_px = self._eval(style_spec.get('stop_px'), scope)
        tif = self._eval(style_spec.get('tif', 'DAY'), scope)

        order_style = OrderStyle(
            kind=kind,
            limit_px=limit_px,
            stop_px=stop_px,
            tif=tif
        )

        # Freshness
        ttl_ms = defaults.get('ttl_ms', 2000)
        dlb_ms = defaults.get('dlb_ms', 500)

        freshness = Freshness(ttl_ms=ttl_ms, dlb_ms=dlb_ms)

        # Extras
        extras_spec = emit.get('extras', {})
        extras = {}
        for key, expr in extras_spec.items():
            extras[key] = self._eval(expr, scope)

        # Add reversal flag
        if is_reversal:
            extras['is_reversal'] = True

        # Create envelope
        envelope = DecisionEnvelope(
            decision_id=make_decision_id(),
            strategy_id=strategy_id,
            symbol=symbol,
            ts_bar_end_us=int(time.time() * 1_000_000),
            target_qty=float(qty),
            order_style=order_style,
            freshness=freshness,
            extras=extras
        )

        return envelope


def load_template_from_dict(template_dict: Dict[str, Any]) -> RuleEngine:
    """Create engine from template dictionary"""
    name = template_dict.get('name', 'default')
    return RuleEngine({name: template_dict})


def load_templates_from_directory(directory: str) -> RuleEngine:
    """Load all YAML templates from directory"""
    import os
    from pathlib import Path

    templates = {}

    for file_path in Path(directory).glob('*.yml'):
        with open(file_path, 'r') as f:
            data = yaml.safe_load(f)

        if isinstance(data, list):
            for tpl in data:
                templates[tpl['name']] = tpl
        else:
            templates[data['name']] = data

        logger.info(f"Loaded template from {file_path}")

    return RuleEngine(templates)
