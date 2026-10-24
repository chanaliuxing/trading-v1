"""
Intent Translator
Converts decision envelopes into execution plans
Based on doc/50.md specification

Responsibilities:
- Select order type (LMT/STP_LMT/IOC) based on market conditions
- Calculate limit/stop prices with tick rounding
- Apply price collars (prevent excessive deviation)
- Configure OCO protection levels
- Plan child orders (TWAP/POV for large orders)
- Apply regulatory corrections (SSR, LULD)
"""
import asyncio
from dataclasses import dataclass
from typing import Dict, Any, Optional

from libs.strategy.base import DecisionEnvelope, validate_order_style
from libs.logger import get_logger

logger = get_logger(__name__)


@dataclass
class ExecPlan:
    """Execution plan"""
    order_type: str  # MKT/LMT/STP/STP_LMT/TRAIL/TRAIL_LIMIT
    side: str  # BUY/SELL/SELL_SHORT
    qty: float
    limit_px: Optional[float]
    stop_px: Optional[float]
    tif: str  # DAY/IOC/GTC
    price_collar_pct: float
    oco: Dict[str, Any]  # {tp_px, sl_px, trail_atr}
    child: Dict[str, Any]  # TWAP/POV plan or NONE
    notes: Dict[str, Any]
    trail_amount: Optional[float] = None
    trail_percent: Optional[float] = None
    trail_stop_px: Optional[float] = None
    lmt_price_offset: Optional[float] = None


class IntentTranslator:
    """
    Intent translator

    Translates high-level decision envelopes into concrete execution plans
    based on market conditions (spread, volatility, liquidity).
    """

    def __init__(
        self,
        policy_cfg: Dict[str, Any],
        market_state_provider: Any,
        tick_size_provider: Any
    ):
        """
        Initialize translator

        Args:
            policy_cfg: Translation policy configuration
            market_state_provider: Market state provider
            tick_size_provider: Tick size provider
        """
        self.cfg = policy_cfg
        self.ms = market_state_provider
        self.tick = tick_size_provider

        logger.info("IntentTranslator initialized")

    def _px_from_ref(self, m: Any, ref: str) -> Optional[float]:
        """
        Get price from reference

        Args:
            m: Market state (dict)
            ref: Reference ('mid', 'last', 'best')

        Returns:
            Reference price
        """
        if ref == "mid":
            return m.get('mid_px')
        if ref == "last":
            return m.get('last_px')
        if ref == "best":
            # Caller decides based on side
            return None
        return m.get('mid_px')

    def _clamp_collar(self, ref_px: float, limit_px: float, collar_pct: float) -> float:
        """
        Clamp price to collar limits

        Args:
            ref_px: Reference price
            limit_px: Proposed limit price
            collar_pct: Collar percentage

        Returns:
            Clamped price
        """
        if not (ref_px and limit_px):
            return limit_px

        dev = abs(limit_px - ref_px) / ref_px * 100.0
        if dev > collar_pct:
            if limit_px > ref_px:
                return ref_px * (1.0 + collar_pct / 100.0)
            else:
                return ref_px * (1.0 - collar_pct / 100.0)

        return limit_px

    def _round_tick(self, px: Optional[float], tick: float) -> Optional[float]:
        """
        Round price to tick size

        Args:
            px: Price
            tick: Tick size

        Returns:
            Rounded price
        """
        if px is None:
            return None
        return round(round(px / tick) * tick, 10)

    async def translate(self, env: DecisionEnvelope) -> ExecPlan:
        """
        Translate decision envelope to execution plan

        Args:
            env: Decision envelope

        Returns:
            Execution plan
        """
        extras = env.extras or {}
        validate_order_style(env.order_style)
        is_option = bool(
            str(extras.get("instrument_type") or "").strip().upper() == "OPTION"
            or str(env.symbol or "").strip().upper().startswith("OPT:")
        )

        # Get market state (options may not be subscribed in MarketStateProvider).
        m = await self.ms.get(env.symbol)
        m = m or {}
        if is_option:
            try:
                opt_bid = float(extras.get("opt_bid_px") or 0.0)
                opt_ask = float(extras.get("opt_ask_px") or 0.0)
                if opt_bid > 0 and opt_ask > 0:
                    m = dict(m)
                    m["bid_px"] = opt_bid
                    m["ask_px"] = opt_ask
                    m["mid_px"] = (opt_bid + opt_ask) / 2.0
                    if m.get("last_px") is None or float(m.get("last_px") or 0.0) <= 0:
                        m["last_px"] = float(extras.get("opt_last_px") or m["mid_px"])
            except Exception:
                pass

        tick = float(m.get('tick_size', 0.01) or 0.01)
        if is_option:
            try:
                opt_tick = extras.get("opt_tick_size")
                if opt_tick is not None and float(opt_tick) > 0:
                    tick = float(opt_tick)
            except Exception:
                pass

        # CRITICAL: Check if market data is available
        # If all prices are zero, try to get fallback from recent bars
        if m.get('bid_px', 0.0) == 0.0 and m.get('ask_px', 0.0) == 0.0 and m.get('last_px', 0.0) == 0.0:
            logger.warning("L1 quote data unavailable, attempting bar data fallback",
                         symbol=env.symbol,
                         decision_id=env.decision_id)

            try:
                # Try to get recent bar data as fallback
                from libs.db.ch_pool import get_client

                sql = """
                SELECT close
                FROM bars_5s FINAL
                WHERE symbol = {symbol:String}
                  AND ts_end >= now() - INTERVAL 60 SECOND
                ORDER BY ts_end DESC
                LIMIT 1
                """

                results = await asyncio.to_thread(
                    lambda: get_client().query(sql, parameters={'symbol': env.symbol})
                )
                if results and results[0].get('close', 0) > 0:
                    fallback_px = float(results[0]['close'])
                    logger.info("Using bar close as fallback price",
                              symbol=env.symbol,
                              fallback_px=fallback_px,
                              decision_id=env.decision_id)

                    # Update market state with fallback price
                    m['last_px'] = fallback_px
                    m['mid_px'] = fallback_px
                    m['bid_px'] = fallback_px * 0.9995  # Estimate bid as -5 bps
                    m['ask_px'] = fallback_px * 1.0005  # Estimate ask as +5 bps
                else:
                    logger.error("Bar data fallback failed - no recent bars found",
                               symbol=env.symbol,
                               decision_id=env.decision_id)
            except Exception as e:
                logger.error("Bar data fallback exception",
                           symbol=env.symbol,
                           decision_id=env.decision_id,
                           error=str(e))

        # Determine side
        side = "BUY" if env.target_qty > 0 else "SELL"
        qty = abs(env.target_qty)
        respect_order_style = bool(
            self.cfg.get("respect_order_style", False)
            or extras.get("force_order_style", False)
            or extras.get("protective_stop", False)
            or extras.get("flatten_action", False)
            or extras.get("close_only", False)
        )
        fallback_to_market_on_missing_l1 = bool(self.cfg.get("fallback_to_market_on_missing_l1", False))

        # Calculate market metrics
        spread_bp = 0.0
        bid_px = m.get('bid_px')
        ask_px = m.get('ask_px')
        if bid_px and ask_px:
            spread_bp = (ask_px - bid_px) / ((ask_px + bid_px) / 2) * 1e4

        atr_bp = env.extras.get("atr_bp", 6.0)
        top_liq = env.extras.get("top_liquidity_shares", 0)
        target_notional = env.max_notional

        # Select policy bucket, unless strategy explicitly requested order style
        bucket_name = "tight_spread_low_vol"  # Default
        action = None
        if respect_order_style and env.order_style and env.order_style.kind:
            forced_type = str(env.order_style.kind).strip().upper().replace(" ", "_")
            action = {
                "order_type": forced_type,
                "ref": "mid",
                "offset_ticks": 0,
                "stop_offset_ticks": 0,
                "tif": env.tif or "DAY",
                "child": "NONE",
            }
            bucket_name = "strategy_order_style"
            logger.info(
                "Translator using strategy order_style",
                decision_id=env.decision_id,
                symbol=env.symbol,
                order_type=forced_type,
            )
        else:
            for b in self.cfg.get("buckets", []):
                ok = True
                w = b.get("when", {})

                # Check conditions
                metrics = {
                    "spread_bp": spread_bp,
                    "atr_bp": atr_bp,
                    "top_liquidity_shares": top_liq,
                    "target_notional_usd": target_notional
                }

                for k, cond in w.items():
                    v = metrics.get(k)
                    if v is None:
                        ok = False
                        break

                    if "lte" in cond and not (v <= cond["lte"]):
                        ok = False
                    if "lt" in cond and not (v < cond["lt"]):
                        ok = False
                    if "gte" in cond and not (v >= cond["gte"]):
                        ok = False
                    if "gt" in cond and not (v > cond["gt"]):
                        ok = False

                if ok:
                    bucket_name = b["name"]
                    action = b["action"]
                    break

            # Use default action if no match
            if action is None:
                action = self.cfg.get("buckets", [{}])[0].get("action", {})
                action = action or {
                    "order_type": "LMT",
                    "ref": "mid",
                    "offset_ticks": 1,
                    "tif": "DAY",
                    "child": "NONE"
                }

        # Get reference price
        ref_px = self._px_from_ref(m, action.get("ref", "mid"))
        if action.get("ref") == "best":
            ref_px = m.get('ask_px') if side == "BUY" else m.get('bid_px')

        # Calculate limit, stop, and native trailing fields.
        limit_px, stop_px = None, None
        trail_amount, trail_percent, trail_stop_px, lmt_price_offset = None, None, None, None

        if action["order_type"] in ("LMT", "STP_LMT"):
            # Strategy-specified explicit limit price has highest priority.
            explicit_limit_px = None
            if env.order_style and env.order_style.limit_px is not None:
                explicit_limit_px = float(env.order_style.limit_px)
            if explicit_limit_px and explicit_limit_px > 0:
                limit_px = explicit_limit_px
            else:
            # CRITICAL: Require valid reference price for limit orders
                base_px = ref_px or m.get('last_px')
                if not base_px or base_px <= 0:
                    if fallback_to_market_on_missing_l1:
                        logger.warning(
                            "Missing L1 for limit order, downgrading to MKT",
                            symbol=env.symbol,
                            decision_id=env.decision_id,
                            requested_order_type=action["order_type"],
                        )
                        action["order_type"] = "MKT"
                    else:
                        logger.error("Cannot create limit order without valid market price",
                                    symbol=env.symbol,
                                    decision_id=env.decision_id,
                                    ref_px=ref_px,
                                    last_px=m.get('last_px'),
                                    market_state=m)
                        raise ValueError(f"Missing market data for {env.symbol} - cannot price limit order")

                if action["order_type"] in ("LMT", "STP_LMT"):
                    offset_ticks = action.get("offset_ticks", 1)
                    offset = offset_ticks * tick * (1 if side == "BUY" else -1)
                    limit_px = base_px + offset

        if action["order_type"] in ("STP", "STP_LMT"):
            explicit_stop_px = None
            if env.order_style and env.order_style.stop_px is not None:
                explicit_stop_px = float(env.order_style.stop_px)
            if explicit_stop_px and explicit_stop_px > 0:
                stop_px = explicit_stop_px
            else:
            # CRITICAL: Require valid reference price for stop orders
                base_px = ref_px or m.get('last_px')
                if not base_px or base_px <= 0:
                    if fallback_to_market_on_missing_l1:
                        logger.warning(
                            "Missing L1 for stop order, downgrading to MKT",
                            symbol=env.symbol,
                            decision_id=env.decision_id,
                            requested_order_type=action["order_type"],
                        )
                        action["order_type"] = "MKT"
                    else:
                        logger.error("Cannot create stop order without valid market price",
                                    symbol=env.symbol,
                                    decision_id=env.decision_id,
                                    ref_px=ref_px,
                                    last_px=m.get('last_px'))
                        raise ValueError(f"Missing market data for {env.symbol} - cannot price stop order")

                if action["order_type"] in ("STP", "STP_LMT"):
                    stop_ticks = action.get("stop_offset_ticks", 3)
                    offset = stop_ticks * tick * (1 if side == "BUY" else -1)
                    stop_px = base_px + offset

        if action["order_type"] in ("TRAIL", "TRAIL_LIMIT"):
            if not env.order_style:
                raise ValueError(f"{action['order_type']} requires explicit order_style")
            trail_amount = env.order_style.trail_amount
            trail_percent = env.order_style.trail_percent
            trail_stop_px = env.order_style.trail_stop_px
            lmt_price_offset = env.order_style.lmt_price_offset

        # SSR correction: Short sales must be above bid
        if m.get("ssr_active", False) and side == "SELL":
            bid_px = m.get('bid_px')
            if limit_px and bid_px:
                limit_px = max(limit_px, bid_px + tick)

        # Apply price collar
        collar = self.cfg.get("defaults", {}).get("price_collar_pct", 3.0)
        limit_px = self._round_tick(self._clamp_collar(ref_px, limit_px, collar), tick)
        stop_px = self._round_tick(stop_px, tick)
        trail_stop_px = self._round_tick(trail_stop_px, tick)
        lmt_price_offset = self._round_tick(lmt_price_offset, tick)

        # Configure OCO protection
        # Priority: 1) Strategy-level config from extras  2) System default config
        oco_cfg = (env.extras or {}).get("oco_cfg") or self.cfg.get("defaults", {}).get("oco", {})
        tp_px, sl_px = None, None

        # Stop loss
        if env.risk_overrides and env.risk_overrides.catastrophic_stop_px:
            sl_px = env.risk_overrides.catastrophic_stop_px
        else:
            # Calculate from hard_stop_bp
            ref = ref_px or m.get('last_px')
            if ref:
                hard_bp = oco_cfg.get("hard_stop_bp", 80) / 1e4
                sl_px = ref * (1 - hard_bp) if side == "BUY" else ref * (1 + hard_bp)

        # Take profit
        last_px = m.get('last_px')
        if ref_px or last_px:
            bp = oco_cfg.get("take_profit_bp", 40) / 1e4
            ref = ref_px or last_px
            tp_px = ref * (1 + bp) if side == "BUY" else ref * (1 - bp)

        tp_px = self._round_tick(tp_px, tick)
        sl_px = self._round_tick(sl_px, tick)

        # Child order planning (TWAP/POV)
        child = {"type": "NONE"}

        # Strategy explicit hint takes precedence if provided via extras
        try:
            exec_child = (env.extras or {}).get("exec_child")
            if isinstance(exec_child, str) and exec_child.upper() in ("TWAP", "POV", "IOC"):
                if exec_child.upper() == "TWAP":
                    horizon_s = int((env.extras or {}).get("exec_horizon_s") or 600)
                    child = {"type": "TWAP", "horizon_s": max(300, horizon_s)}
                elif exec_child.upper() == "POV":
                    pov = float((env.extras or {}).get("exec_pov") or 0.1)
                    child = {"type": "POV", "participation": max(0.01, min(0.5, pov))}
                elif exec_child.upper() == "IOC":
                    child = {"type": "IOC", "sweep_levels": 1}
        except Exception:
            pass

        if child["type"] == "NONE" and action.get("child") == "TWAP_OR_POV":
            time_stop = env.risk_overrides.time_stop_s if env.risk_overrides else None
            horizon_s = max(300, time_stop or 600)
            child = {"type": "TWAP", "horizon_s": horizon_s}
        elif child["type"] == "NONE" and action.get("child") == "IOC_IF_LIQ_LOW" and top_liq < 200:
            child = {"type": "IOC", "sweep_levels": 1}

        tif = action.get("tif", "DAY")
        if env.order_style and env.order_style.tif:
            tif = env.order_style.tif

        # Build execution plan
        trailing_atr = env.risk_overrides.trailing_atr_mult if env.risk_overrides else None

        plan = ExecPlan(
            order_type=action["order_type"],
            side=side,
            qty=qty,
            limit_px=limit_px,
            stop_px=stop_px,
            trail_amount=trail_amount,
            trail_percent=trail_percent,
            trail_stop_px=trail_stop_px,
            lmt_price_offset=lmt_price_offset,
            tif=tif,
            price_collar_pct=collar,
            oco={
                "tp_px": tp_px,
                "sl_px": sl_px,
                "trail_atr": trailing_atr
            },
            child=child,
            notes={
                "bucket": bucket_name,
                "spread_bp": spread_bp,
                "atr_bp": atr_bp,
                "top_liq": top_liq
            }
        )

        logger.debug("Intent translated",
                    decision_id=env.decision_id,
                    symbol=env.symbol,
                    bucket=bucket_name,
                    order_type=plan.order_type,
                    side=plan.side,
                    qty=plan.qty)

        return plan
