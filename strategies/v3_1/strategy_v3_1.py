"""
V3.1 strategy implementation (plug-in, no core changes).
Hardened version: persistence + sync + stable time tracking + safe fallbacks.
"""

from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Dict, Optional

from libs.logger import get_logger
from libs.strategy import StrategyAlgorithm

from .execution_guard import ExecutionGuard
from .exit_manager import ExitAction, PositionState, build_exit_plan, evaluate_exits
from .hedge import hedge_action
from .indicators import Bar, atr, avg_volume, ema, ema_slope, highest_high, wall_dist_pct
from .lanes import lane1_breakout, lane1_pullback, lane2_pullback
from .market_state import evaluate_state
from .params_store import ParamsStore, ParamSet
from .scarcity import ScarcityController
from .schemas import ExitPlan, ExitTarget, Signal, TradeEvent, TradeLog
from .scoring import (
    entry_quality_breakout,
    entry_quality_pullback,
    grade_from_score,
    options_risk,
    quality_score,
    structure_space,
    trend_strength,
)
from .sizing import compute_sizing, compute_stop0

logger = get_logger(__name__)


@dataclass
class SymbolState:
    bars: Deque[Bar] = field(default_factory=lambda: deque(maxlen=2000))
    total_bars: int = 0  # Monotonically increasing counter for stable time tracking
    last_features: Dict[str, Any] = field(default_factory=dict)

    # For ATR safety: keep last known good ATR to manage positions even if current data is dirty
    last_good_atr: Optional[float] = None

    pullback_anchor: Optional[float] = None
    pullback_low: Optional[float] = None
    pullback_high: Optional[float] = None
    bars_since_touch: int = 999

    position: Optional[PositionState] = None
    exit_plan: Optional[ExitPlan] = None
    trade_log: Optional[TradeLog] = None
    targets_hit: Dict[str, bool] = field(default_factory=dict)


class V31Strategy(StrategyAlgorithm):
    """
    A self-contained plugin strategy class (no core changes assumed).
    If your core framework injects ctx/params_store/paths differently, you can
    pass them via kwargs and this class will still work.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # Be tolerant to unknown base signatures
        try:
            super().__init__(*args, **kwargs)
        except TypeError:
            try:
                super().__init__()
            except Exception:
                pass

        # Required injected objects
        self.ctx = kwargs.get("ctx", getattr(self, "ctx", None))
        if self.ctx is None and len(args) > 0:
             self.ctx = args[0]
        if self.ctx is None:
            raise RuntimeError("V31Strategy requires ctx to be provided (kwarg 'ctx' or base StrategyAlgorithm).")

        self.params_store: ParamsStore = kwargs.get("params_store", getattr(self, "params_store", None))
        
        # Restore compatibility: if params_store not injected, create it from params
        if self.params_store is None:
            # Try to get params from args or attribute
            params = getattr(self, "params", None)
            if params is None and len(args) > 1:
                params = args[1]
            
            if params:
                extras = getattr(params, "extras", {}) or {}
                params_path = extras.get("params_path", "runs/params/v3_1/latest.json")
                reload_sec = int(extras.get("params_reload_sec", 30))
                self.params_store = ParamsStore(params_path, reload_sec=reload_sec)
            else:
                # Fallback default
                self.params_store = ParamsStore("runs/params/v3_1/latest.json", reload_sec=30)

        # Runtime paths setup moved up for ScarcityController dependency
        base_dir = Path(kwargs.get("base_dir", getattr(self, "base_dir", "."))).resolve()
        runtime_dir = Path(kwargs.get("runtime_dir", base_dir / "runtime")).resolve()
        runtime_dir.mkdir(parents=True, exist_ok=True)

        # Initialize ScarcityController with log path
        self.scarcity: ScarcityController = kwargs.get("scarcity", getattr(self, "scarcity", None))
        if self.scarcity is None:
            scarcity_log = kwargs.get("scarcity_log_path") or str(runtime_dir / "v31_scarcity.jsonl")
            # If params has it, prefer that (backward compat)
            if self.params and self.params.extras:
                scarcity_log = self.params.extras.get("scarcity_log", scarcity_log)
            self.scarcity = ScarcityController(scarcity_log)

        self.guard: ExecutionGuard = kwargs.get("guard", getattr(self, "guard", None)) or ExecutionGuard(self.ctx)

        # Execution mode
        self.execution_mode: str = kwargs.get("execution_mode", getattr(self, "execution_mode", "shadow"))
        
        # Default universe
        self.default_universe: list[str] = kwargs.get("default_universe", getattr(self, "default_universe", []))
        if not self.default_universe and self.params and self.params.extras:
            self.default_universe = self.params.extras.get("symbols") or []

        # Strategy ID fallback
        self.strategy_id = kwargs.get("strategy_id", getattr(self, "strategy_id", "v3_1_equity"))

        self.state_path = Path(kwargs.get("state_path", runtime_dir / "v31_state.json"))
        self.signal_log_path = Path(kwargs.get("signal_log_path", runtime_dir / "v31_signals.jsonl"))
        self.trade_log_path = Path(kwargs.get("trade_log_path", runtime_dir / "v31_trades.jsonl"))
        self.trader_log_path = Path(kwargs.get("trader_log_path", runtime_dir / "v31_trader.log"))
        self.system_log_path = Path(kwargs.get("system_log_path", runtime_dir / "v31_system.log"))

        # In-memory state
        self._symbol_state: Dict[str, SymbolState] = {}
        self._features_by_symbol: Dict[str, Dict[str, Any]] = {}
        self._current_open_risk: float = 0.0
        self._daily_risk_used: float = 0.0
        self._last_risk_reset: str = _today_str()
        self.params: ParamSet = self.params_store.get()

        # Load persistence
        self._load_state()

    async def initialize(self) -> None:
        """
        Lifecycle hook called by the engine after instantiation.
        Used for async setup (e.g. connecting guards, loading state).
        """
        # Ensure guard is initialized
        if self.guard:
            await self.guard.init()
        
        # Subscribe to market data
        if self.default_universe:
            # Re-subscribe to ensure data flow
            await self.ctx.subscribe_features(self.default_universe, bar_sec=60)
            
        logger.info("v3_1_initialized", 
                   strategy_id=getattr(self, "strategy_id", "v3_1"),
                   symbols=len(self.default_universe))

    async def on_features(self, symbol: str, features: Dict[str, Any]) -> None:
        self._log_system(
            f"Pipeline [DATA]: Received {symbol}. TS: {features.get('ts') or features.get('ts_utc') or features.get('ts_us')}"
        )

        params = self.params_store.get()
        self.params = params  # keep latest for _account_equity fallback
        self._reset_risk_if_new_day()

        state = self._symbol_state.setdefault(symbol, SymbolState())

        # Build bar
        bar = _bar_from_features(features)
        if bar:
            state.bars.append(bar)
            state.total_bars += 1  # stable monotonic count (also persisted)
        self._features_by_symbol[symbol] = features  # raw features for market_state breadth etc.

        # Update EMAs if not in features
        close_series = [b.close for b in state.bars]
        if close_series:
            features.setdefault("ema10", ema(close_series, 10))
            features.setdefault("ema20", ema(close_series, 20))
            features.setdefault("ema50", ema(close_series, 50))
            features.setdefault("ema20_slope", ema_slope(close_series, 20, 5))

        # -----------------------------
        # P0: ALWAYS manage existing position FIRST
        # -----------------------------
        if state.position:
            # even if ATR is bad, we try to manage with last_good_atr fallback
            await self._manage_position(symbol, state, features, params)
            if state.position:
                # still holding -> do not attempt re-entry on same bar
                state.last_features = features
                return

        # -----------------------------
        # ATR compute + sanity (only affects ENTRY, not management)
        # -----------------------------
        valid_data_for_entry = True

        # Compute ATR if missing
        if features.get("atr14") is None and features.get("atr_14") is None:
            atr_val = atr(list(state.bars), 14)
            if atr_val is not None:
                close = features.get("close")
                if close and atr_val > close * 0.05:
                    self._log_system(
                        f"Pipeline [WARN]: ATR sanity failed for {symbol}. ATR={atr_val}, Close={close}. Entry disabled."
                    )
                    valid_data_for_entry = False
                else:
                    features["atr_14"] = atr_val
                    state.last_good_atr = float(atr_val)

        # If ATR exists, sanity check too
        current_atr = features.get("atr_14") or features.get("atr14")
        if current_atr:
            close = features.get("close")
            if close and float(current_atr) > close * 0.05:
                self._log_system(
                    f"Pipeline [WARN]: Input ATR sanity failed for {symbol}. ATR={current_atr}, Close={close}. Entry disabled."
                )
                valid_data_for_entry = False
            else:
                state.last_good_atr = float(current_atr)

        # If ATR invalid, keep last_good_atr only for (future) management fallback
        if not valid_data_for_entry and state.last_good_atr:
            # Do NOT pretend data is valid for entry; only provide ATR for robustness/logs
            features["atr_14"] = float(state.last_good_atr)
            self._log_system(f"Pipeline [INFO]: {symbol} using last_good_atr={state.last_good_atr} for continuity.")

        self._log_system(
            f"Pipeline [FEATURES]: {symbol} updated. Close={features.get('close')}, ATR={features.get('atr_14') or features.get('atr14')}"
        )

        market_state = evaluate_state(self._features_by_symbol, params)
        scarcity_decision = self.scarcity.decide(params, market_state.state)

        if symbol not in scarcity_decision.watchlist:
            state.last_features = features
            return

        # Open risk limit
        self._recalc_open_risk()
        max_open_risk = float(params.extras.get("max_open_risk_r", 3.0))
        if self._current_open_risk >= max_open_risk:
            state.last_features = features
            return

        # Max concurrent positions
        max_pos = int(params.extras.get("max_concurrent_positions", 8))
        current_pos_count = sum(1 for s in self._symbol_state.values() if s.position)
        if current_pos_count >= max_pos:
            self._log_system(f"RISK LIMIT: Max positions {max_pos} reached. Skipping {symbol}")
            state.last_features = features
            return

        # OFF state rule
        if market_state.state == "OFF" and not params.allow_lane2_in_off:
            state.last_features = features
            return

        # Block new entries if data invalid
        if not valid_data_for_entry:
            state.last_features = features
            return

        # Entry evaluation
        signal = await self._evaluate_entry(symbol, state, features, params, market_state, scarcity_decision)
        if signal is None:
            state.last_features = features
            return

        # Daily cumulative risk cap
        daily_risk_cap = float(params.extras.get("max_daily_risk_usd", 5000.0))
        if self._daily_risk_used + float(signal.execution["risk_budget"]) > daily_risk_cap:
            self._log_system(
                f"RISK LIMIT: Daily risk exceeded. Add={signal.execution['risk_budget']} used={self._daily_risk_used} cap={daily_risk_cap}"
            )
            state.last_features = features
            return

        await self._emit_signal(signal)
        self.scarcity.record_signal(signal.ts)

        self._log_trader(
            f"OPPORTUNITY: {symbol} | Grade: {signal.grade} | Lane: {signal.lane} | Score: {signal.quality_score}"
        )
        self._log_system(f"Pipeline [SIGNAL]: Generated signal for {symbol}: {signal.to_dict()}")

        if self.execution_mode == "shadow":
            state.last_features = features
            return

        # Execute entry
        exec_info = signal.execution
        order_type = "MKT" if exec_info["entry_type"] == "BREAKOUT" else "LMT"

        self._log_trader(
            f"EXECUTING: BUY {symbol} qty={int(exec_info.get('shares', 0))} type={order_type} trigger={exec_info.get('entry_trigger')}"
        )

        order_res = await self.guard.place_equity_order(
            symbol=symbol,
            side="BUY",
            qty=int(exec_info.get("shares", 0)),
            order_type=order_type,
            limit_px=float(exec_info["entry_limit"]) if order_type == "LMT" else None,
            tif="DAY",
            allow_cancel_replace=True,
        )
        self._log_system(f"Pipeline [EXECUTION]: Order result for {symbol}: {order_res}")

        if not order_res.ok:
            logger.warning("v3_1_entry_failed", symbol=symbol, reason=order_res.reason)
            self._log_trader(f"EXECUTION FAILED: {symbol} - {order_res.reason}")
            state.last_features = features
            return

        # Conservative risk accounting: spend daily budget on submission (not fill)
        self._daily_risk_used += float(exec_info["risk_budget"])
        self._log_system(
            f"RISK ACCOUNTING: {symbol} used {exec_info['risk_budget']}. Total daily used: {self._daily_risk_used}"
        )

        # Build PositionState safely (supports optional fields)
        state.position = self._make_position_state(exec_info, signal, state, features)
        state.exit_plan = signal.exit_plan
        state.targets_hit = {}
        self._save_state()

        state.trade_log = _trade_log_from_signal(signal)
        state.trade_log.events.append(TradeEvent(signal.ts, "ENTRY_SUBMIT", {"order_id": getattr(order_res, "order_id", None)}))
        await self._append_trade_log(state.trade_log)

        state.last_features = features

    def _make_position_state(
        self, exec_info: Dict[str, Any], signal: Signal, state: SymbolState, features: Dict[str, Any]
    ) -> PositionState:
        """
        Construct PositionState without assuming exact dataclass fields.
        """
        entry_ts = _ts_us(features) or 0
        base_kwargs: Dict[str, Any] = dict(
            entry_px=float(exec_info["entry_trigger"]),
            stop0=float(exec_info["stop0"]),
            r_per_share=float(exec_info["r_per_share"]),
            lane=signal.lane,
            entry_ts=int(entry_ts),
            entry_bar_idx=int(state.total_bars),
        )

        # Optional fields if exist on PositionState
        fields = getattr(PositionState, "__dataclass_fields__", {}) or {}
        if "shares" in fields:
            base_kwargs["shares"] = int(exec_info.get("shares", 0))
        if "risk_budget" in fields:
            base_kwargs["risk_budget"] = float(exec_info.get("risk_budget", 0.0))

        try:
            return PositionState(**base_kwargs)
        except TypeError:
            # fallback minimal (in case your PositionState doesn't have some keys)
            minimal = {k: v for k, v in base_kwargs.items() if k in ("entry_px", "stop0", "r_per_share", "lane", "entry_ts")}
            pos = PositionState(**minimal)
            # attach extra attrs if needed
            if not hasattr(pos, "entry_bar_idx"):
                setattr(pos, "entry_bar_idx", int(state.total_bars))
            if not hasattr(pos, "shares") and "shares" in base_kwargs:
                setattr(pos, "shares", int(base_kwargs["shares"]))
            return pos

    async def _evaluate_entry(
        self,
        symbol: str,
        state: SymbolState,
        features: Dict[str, Any],
        params: ParamSet,
        market_state: Any,
        scarcity_decision: Any,
    ) -> Optional[Signal]:
        close = features.get("close")
        ema20 = features.get("ema20") or features.get("ema_20")
        ema50 = features.get("ema50") or features.get("ema_50")
        ema20_slope = features.get("ema20_slope")
        atr_val = features.get("atr_14") or features.get("atr14")

        if close is None or ema20 is None or atr_val is None:
            if state.bars and len(state.bars) > 20:
                self._log_system(f"Pipeline [WARN]: Missing core features for {symbol}. Close:{close} EMA20:{ema20} ATR:{atr_val}")
            return None

        # Pullback tracking
        anchor = features.get("vwap") or ema20
        if anchor is None:
            return None

        low = features.get("low") or close
        high = features.get("high") or close
        band = params.pullback_band_atr * float(atr_val)

        touched = abs(float(low) - float(anchor)) <= band
        if touched:
            state.pullback_anchor = float(anchor)
            state.pullback_low = float(low)
            state.pullback_high = float(high)
            state.bars_since_touch = 0
        else:
            state.bars_since_touch += 1

        hhv20 = highest_high(list(state.bars), 20)
        vol_mult = None
        avg_vol = avg_volume(list(state.bars), 20)
        if avg_vol and features.get("volume"):
            vol_mult = float(features.get("volume")) / float(avg_vol)

        lane_signal = None
        if ema50 is not None and ema20_slope is not None and close > ema50 and ema20 > ema50 and float(ema20_slope) > 0:
            lane_signal = lane1_breakout(features, hhv20)
            if lane_signal is None and state.pullback_anchor and state.pullback_high is not None:
                lane_signal = lane1_pullback(
                    features,
                    state.pullback_anchor,
                    state.pullback_low or float(low),
                    state.pullback_high or float(high),
                    state.bars_since_touch,
                )

        if lane_signal is None and close > ema20 and state.pullback_anchor and state.pullback_high is not None:
            lane_signal = lane2_pullback(
                features,
                state.pullback_anchor,
                state.pullback_low or float(low),
                state.pullback_high or float(high),
                state.bars_since_touch,
            )

        if lane_signal is None:
            return None

        # Quality scoring
        trend_score = trend_strength(features)
        if lane_signal.entry_type == "BREAKOUT":
            entry_score = entry_quality_breakout(features, vol_mult)
        else:
            entry_score = entry_quality_pullback(features, lane_signal.anchor_px, float(atr_val), lane_signal.bars_since_touch)

        wall_dist = features.get("wall_dist_pct")
        if wall_dist is None and hhv20 and close > 0:
            wall_dist = wall_dist_pct(float(close), float(hhv20))
        struct_score = structure_space(wall_dist, params)

        ivp = features.get("ivp") or features.get("iv_rank")
        atr_pct_rank = features.get("atr_pct_rank")
        options_score = options_risk(ivp, atr_pct_rank)

        score = quality_score(trend_score, entry_score, struct_score, options_score)
        grade = grade_from_score(score)
        if grade == "D":
            return None

        # Risk sizing
        equity = await self._account_equity()

        stop0 = compute_stop0(
            entry_px=float(close),
            atr=float(atr_val),
            swing_low=min([b.low for b in list(state.bars)[-5:]]) if len(state.bars) >= 5 else float(low),
            pullback_low=state.pullback_low,
            lane=lane_signal.lane,
            params=params,
        )

        sizing = compute_sizing(float(close), float(stop0), float(equity), params)
        if sizing.shares <= 0:
            return None

        # Hedge adjustment
        hedge_cost_ratio = float(features.get("hedge_cost_ratio", 0.0) or 0.0)
        hedge_act = hedge_action(grade, hedge_cost_ratio, params.hedge_cost_ratio)
        if hedge_act.method == "SIZE_REDUCTION":
            sizing_shares = int(sizing.shares * (1.0 - hedge_act.size_reduction_pct))
            sizing = sizing.__class__(
                stop0=sizing.stop0,
                r_per_share=sizing.r_per_share,
                shares=max(0, sizing_shares),
                risk_budget=sizing.risk_budget,
                max_notional_cap=sizing.max_notional_cap,
            )
            if sizing.shares <= 0:
                return None

        # Exit plan
        exit_plan = build_exit_plan(
            float(close),
            float(sizing.r_per_share),
            lane_signal.lane,
            {"T_W1": float(close) * (1 + (wall_dist or 0.0)) if wall_dist else 0.0},
        )

        exec_payload = {
            "entry_type": lane_signal.entry_type,
            "entry_trigger": float(close),
            # buffer helps avoid missed fills on pullbacks (even if later used only for LMT)
            "entry_limit": float(close) * 1.0005,
            "stop0": float(sizing.stop0),
            "r_per_share": float(sizing.r_per_share),
            "risk_budget": float(sizing.risk_budget),
            "shares": int(sizing.shares),
            "max_notional_cap": float(sizing.max_notional_cap),
        }

        feat_payload = {
            "breadth20": getattr(market_state, "breadth20", None),
            "ema10": features.get("ema10"),
            "ema20": features.get("ema20"),
            "ema50": features.get("ema50"),
            "atr14": float(atr_val),
            "wall_dist_pct": wall_dist,
            "ivp": ivp,
        }

        return Signal(
            ticker=symbol,
            ts=_ts_iso(features),
            state=market_state.state,
            lane=lane_signal.lane,
            grade=grade,
            quality_score=int(score),
            features=feat_payload,
            execution=exec_payload,
            hedge_action=hedge_act,
            exit_plan=exit_plan,
        )

    async def _manage_position(self, symbol: str, state: SymbolState, features: Dict[str, Any], params: ParamSet) -> None:
        if not state.position or not state.exit_plan:
            return

        # --- sync shares with broker (P1)
        local_shares = int(getattr(state.position, "shares", 0) or 0)
        actual_qty: Optional[int] = None

        try:
            broker_pos = await self.ctx.position(symbol)
            actual_qty = abs(int(broker_pos.pos_qty))
            if actual_qty == 0:
                self._log_trader(f"SYNC: {symbol} position no longer exists in broker. Resetting state.")
                state.position = None
                state.exit_plan = None
                state.targets_hit = {}
                self._save_state()
                return

            if local_shares and actual_qty != local_shares:
                self._log_system(f"SYNC: {symbol} shares adjusted {local_shares} -> {actual_qty}")
                if hasattr(state.position, "shares"):
                    state.position.shares = actual_qty
            elif local_shares == 0 and hasattr(state.position, "shares"):
                # Fill missing local shares if PositionState supports it
                state.position.shares = actual_qty

        except Exception as exc:
            err_msg = str(exc)
            self._log_system(f"SYNC ERROR: {symbol} - {err_msg}")
            fatal_keywords = ("Connection", "Timeout", "Auth", "Unavailable", "Network")
            if any(k in err_msg for k in fatal_keywords):
                return  # do nothing on unstable connection

        close = features.get("close")
        ema10 = features.get("ema10")
        ema20 = features.get("ema20")

        atr_val = features.get("atr_14") or features.get("atr14") or state.last_good_atr or 0.0
        if close is None or float(atr_val) <= 0:
            return

        bars_held = _bars_held(int(state.position.entry_ts), _ts_us(features) or 0, state)
        range_5d = _range_5d(state.bars)

        mfe_r = max(0.0, (float(close) - float(state.position.entry_px)) / float(state.position.r_per_share))
        actions = evaluate_exits(params, state.position, float(close), ema10, ema20, float(atr_val), bars_held, mfe_r, range_5d)

        # Targets
        trim_pct = 0.0
        for tgt in state.exit_plan.targets:
            if state.targets_hit.get(tgt.id):
                continue
            if float(close) >= float(tgt.price):
                state.targets_hit[tgt.id] = True
                trim_pct += float(tgt.sell_pct)

        if trim_pct > 0:
            actions.append(ExitAction("TRIM", float(close), trim_pct, "TARGETS"))

        if not actions:
            return

        if self.execution_mode == "shadow":
            return

        # Use latest known real_qty for orders
        if actual_qty is None:
            try:
                pos = await self.ctx.position(symbol)
                actual_qty = abs(int(pos.pos_qty))
            except Exception:
                actual_qty = int(getattr(state.position, "shares", 0) or 0)

        # Execute exits
        for act in actions:
            self._log_trader(f"MANAGE: {symbol} -> {act.action} ({act.reason}) close={close}")
            self._log_system(f"Pipeline [EXIT]: {symbol} exit action: {act}")

            # Final reality check only if needed
            real_qty = int(actual_qty or 0)
            if real_qty <= 0:
                self._log_system(f"EXIT ABORTED: {symbol} - No open position in broker/local.")
                state.position = None
                state.exit_plan = None
                state.targets_hit = {}
                self._save_state()
                continue

            if act.action in ("EXIT_ALL", "EXIT_RUNNER"):
                await self.guard.place_equity_order(symbol, "SELL", qty=real_qty, order_type="MKT")
                state.position = None
                state.exit_plan = None
                state.targets_hit = {}
                self._log_trader(f"POSITION CLOSED: {symbol}. Reset state.")
            elif act.action == "TRIM":
                trim_qty = max(1, int(real_qty * float(act.qty_pct)))
                await self.guard.place_equity_order(symbol, "SELL", qty=trim_qty, order_type="MKT")
            elif act.action == "MOVE_STOP":
                state.position.stop0 = float(act.price)

        self._save_state()

        if state.trade_log:
            for act in actions:
                state.trade_log.events.append(TradeEvent(_ts_iso(features), act.action, {"reason": act.reason}))
            await self._append_trade_log(state.trade_log)

    async def _emit_signal(self, signal: Signal) -> None:
        try:
            with self.signal_log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(signal.to_dict()) + "\n")
        except Exception as exc:
            logger.warning("v3_1_signal_log_failed", error=str(exc))

    async def _append_trade_log(self, trade: TradeLog) -> None:
        try:
            with self.trade_log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(trade.to_dict()) + "\n")
        except Exception as exc:
            logger.warning("v3_1_trade_log_failed", error=str(exc))

    def _log_trader(self, msg: str) -> None:
        self._write_log(self.trader_log_path, f"[TRADER] {msg}")

    def _log_system(self, msg: str) -> None:
        self._write_log(self.system_log_path, f"[SYSTEM] {msg}")

    def _write_log(self, path: Path, msg: str) -> None:
        try:
            timestamp = datetime.now().isoformat(sep=" ", timespec="milliseconds")
            with path.open("a", encoding="utf-8") as fh:
                fh.write(f"[{timestamp}] {msg}\n")
        except Exception:
            pass

    def _save_state(self) -> None:
        """
        Persist: position + exit_plan + targets_hit + total_bars.
        Guarantee total_bars >= entry_bar_idx when saving a live position.
        """
        try:
            data: Dict[str, Any] = {}
            for symbol, state in self._symbol_state.items():
                if not (state.position or state.total_bars > 0):
                    continue

                total_bars = int(state.total_bars)
                if state.position and hasattr(state.position, "entry_bar_idx"):
                    try:
                        total_bars = max(total_bars, int(state.position.entry_bar_idx))
                    except Exception:
                        pass

                data[symbol] = {
                    "position": asdict(state.position) if state.position else None,
                    "exit_plan": asdict(state.exit_plan) if state.exit_plan else None,
                    "targets_hit": state.targets_hit,
                    "total_bars": total_bars,
                }

            tmp = self.state_path.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(data, f, default=str, indent=2)
            tmp.replace(self.state_path)
        except Exception as exc:
            self._log_system(f"PERSISTENCE ERROR: Save failed - {exc}")

    def _load_state(self) -> None:
        if not self.state_path.exists():
            return

        try:
            with self.state_path.open("r", encoding="utf-8") as f:
                data = json.load(f)

            for symbol, s_data in data.items():
                state = self._symbol_state.setdefault(symbol, SymbolState())

                pos_data = s_data.get("position")
                if pos_data:
                    state.position = PositionState(**pos_data)

                plan_data = s_data.get("exit_plan")
                if plan_data:
                    targets_data = plan_data.get("targets", []) or []
                    targets: list[ExitTarget] = []
                    for t in targets_data:
                        if isinstance(t, dict):
                            try:
                                targets.append(ExitTarget(**t))
                            except Exception:
                                continue
                    plan_data["targets"] = targets
                    state.exit_plan = ExitPlan(**plan_data)

                state.targets_hit = s_data.get("targets_hit", {}) or {}
                state.total_bars = int(s_data.get("total_bars", 0) or 0)

            self._log_system(f"PERSISTENCE: Loaded state for {len(data)} symbols")
        except Exception as exc:
            self._log_system(f"PERSISTENCE ERROR: Load failed - {exc}")

    def _recalc_open_risk(self) -> None:
        total_r = 0.0
        for _, state in self._symbol_state.items():
            if not state.position:
                continue

            current_px = 0.0
            if state.last_features:
                current_px = float(state.last_features.get("close", 0.0) or 0.0)

            if current_px > 0 and float(state.position.r_per_share) > 0:
                pnl_r = (current_px - float(state.position.entry_px)) / float(state.position.r_per_share)
                current_risk = max(0.0, 1.0 - pnl_r)
                total_r += min(1.0, current_risk)
            else:
                # fallback
                if float(state.position.stop0) >= float(state.position.entry_px):
                    total_r += 0.1
                else:
                    total_r += 1.0

        self._current_open_risk = total_r

    async def _account_equity(self) -> float:
        try:
            portfolio = await self.ctx.portfolio()
            equity = float(portfolio.get("equity", 0.0) or 0.0)
            if equity > 0:
                return equity
        except Exception:
            pass

        limit = float(getattr(self.params, "capital_limit_usd", 0.0) or 0.0)
        if limit > 0:
            return limit

        raise RuntimeError("CRITICAL: Cannot determine account equity and no capital_limit_usd set.")

    def _reset_risk_if_new_day(self) -> None:
        today = _today_str()
        if today != self._last_risk_reset:
            self._daily_risk_used = 0.0
            self._last_risk_reset = today


# -----------------------------
# Helpers
# -----------------------------
def _bar_from_features(features: Dict[str, Any]) -> Optional[Bar]:
    close = features.get("close")
    high = features.get("high")
    low = features.get("low")
    open_px = features.get("open")
    if None in (close, high, low, open_px):
        return None
    ts = _ts_us(features)
    if ts is None:
        return None
    return Bar(
        ts=ts // 1_000_000,
        open=open_px,
        high=high,
        low=low,
        close=close,
        volume=float(features.get("volume", 0.0)),
        vwap=features.get("vwap"),
    )


def _ts_us(features: Dict[str, Any]) -> Optional[int]:
    """
    Extract timestamp in microseconds with strict priority:
    1. ts_us (int/float)
    2. ts_utc (ISO string)
    3. ts (int/float/str)
    4. ts_start (int/float) for aggregates
    Returns None if parsing fails (no silent now()).
    """
    ts = features.get("ts_us")
    if isinstance(ts, (int, float)) and ts > 0:
        return int(ts if ts > 1_000_000_000_000 else ts * 1_000_000)

    ts_str = features.get("ts_utc")
    if isinstance(ts_str, str):
        try:
            dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1_000_000)
        except ValueError:
            pass

    ts = features.get("ts")
    if isinstance(ts, (int, float)) and ts > 0:
        return int(ts if ts > 1_000_000_000_000 else ts * 1_000_000)
    if isinstance(ts, str):
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1_000_000)
        except ValueError:
            pass

    ts_start = features.get("ts_start")
    if isinstance(ts_start, (int, float)) and ts_start > 0:
        return int(ts_start if ts_start > 1_000_000_000_000 else ts_start * 1_000_000)

    return None


def _ts_iso(features: Dict[str, Any]) -> str:
    ts = _ts_us(features)
    if ts is None:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return datetime.fromtimestamp(ts / 1_000_000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _trade_log_from_signal(signal: Signal) -> TradeLog:
    return TradeLog(
        ticker=signal.ticker,
        lane=signal.lane,
        state=signal.state,
        grade=signal.grade,
        quality_score=signal.quality_score,
        entry_px=float(signal.execution["entry_trigger"]),
        stop0=float(signal.execution["stop0"]),
        r_per_share=float(signal.execution["r_per_share"]),
        shares=int(signal.execution["shares"]),
        risk_budget=float(signal.execution["risk_budget"]),
        hedge_action=signal.hedge_action,
    )


def _bars_held(entry_ts_us: int, now_ts_us: int, state: Optional[SymbolState] = None) -> int:
    """
    Stable bars-held:
    - Prefer monotonic counter: total_bars - entry_bar_idx
    - If restart mismatch happens, fallback to physical time.
    """
    if state and state.position and hasattr(state.position, "entry_bar_idx"):
        try:
            entry_bar_idx = int(state.position.entry_bar_idx or 0)
            if entry_bar_idx > 0 and state.total_bars >= entry_bar_idx:
                return max(0, int(state.total_bars) - entry_bar_idx)
        except Exception:
            pass

    if entry_ts_us <= 0 or now_ts_us <= 0:
        return 0
    delta_sec = max(0.0, (now_ts_us - entry_ts_us) / 1_000_000)
    return int(delta_sec // 60)


def _range_5d(bars: Deque[Bar]) -> float:
    if not bars:
        return 0.0
    recent = list(bars)[-1950:]  # 5d * 6.5h/day * 60m/h = 1950
    hi = max(b.high for b in recent)
    lo = min(b.low for b in recent)
    return float(hi - lo)
