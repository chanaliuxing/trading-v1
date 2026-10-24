"""Enhanced ML signal strategy with multi-model ensemble, dynamic sizing, and risk management.

All functionality is self-contained within this strategy file.
No system files are modified.
"""

from __future__ import annotations

import asyncio  # Added for non-blocking execution
from abc import ABC, abstractmethod
from datetime import datetime, time
from collections import deque
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import pandas as pd
import numpy as np
import os

from obs.audit import REASONS, log_event
try:
    from libs.dal.sfs_facade import SFS1sReader  # type: ignore
except Exception:  # pragma: no cover - optional dependency for backtests
    SFS1sReader = None  # type: ignore
from libs.strategy.registry import register_strategy
from libs.strategy.base import (
    DecisionEnvelope,
    Freshness,
    OrderStyle,
    StrategyAlgorithm,
)
from services.strategy.strategy_base import make_decision_id
from strategies.enhanced_model_config_loader import get_loader

# Real ML Inference Service (Model-based)
try:
    from services.ml import get_model_service
    _USE_REAL_ML = True
except ImportError:
    _USE_REAL_ML = False
    import warnings
    warnings.warn("Real ML service not available. Strategy will run without ML predictions.")

# Optional ML Gateway (outlier/regime filter + decision layer)
try:
    from services.ml.ai_brain_ml_gateway import AiBrainMLGateway
    _USE_ML_GATEWAY = True
except Exception:  # pragma: no cover - gateway is optional
    _USE_ML_GATEWAY = False


# ============================================================================
# Module 1: Position Sizers (仓位计算器)
# ============================================================================


class PositionSizer(ABC):
    """Abstract base class for position sizing algorithms."""

    @abstractmethod
    def calculate(
        self,
        signal: float,
        conf: float,
        price: float,
        atr: float,
        capital: float,
        **kwargs: Any,
    ) -> float:
        """
        Calculate target position size.

        Args:
            signal: Trading signal [-1.0, 1.0]
            conf: Confidence [0.0, 1.0]
            price: Current price
            atr: Average True Range (volatility)
            capital: Available capital

        Returns:
            Target quantity (shares)
        """


class FixedSizer(PositionSizer):
    """Fixed position size (compatible with original ModelSignalStrategy)."""

    def __init__(self, base_qty: int = 100, max_qty: int = 1000, lot: int = 100):
        self.base_qty = base_qty
        self.max_qty = max_qty
        self.lot = lot

    def calculate(
        self,
        signal: float,
        conf: float,
        price: float,
        atr: float,
        capital: float,
        **kwargs: Any,
    ) -> float:
        raw = float(signal) * self.base_qty
        if abs(raw) < self.lot:
            return 0.0
        # Round to lot size
        qty = round(raw / self.lot) * self.lot
        return min(abs(qty), self.max_qty) * (1 if qty > 0 else -1)


class ConfidenceScaledSizer(PositionSizer):
    """Scale position size by confidence level."""

    def __init__(
        self,
        base_qty: int = 100,
        max_qty: int = 1000,
        lot: int = 100,
        multiplier: float = 2.0,
    ):
        self.base_qty = base_qty
        self.max_qty = max_qty
        self.lot = lot
        self.multiplier = multiplier

    def calculate(
        self,
        signal: float,
        conf: float,
        price: float,
        atr: float,
        capital: float,
        **kwargs: Any,
    ) -> float:
        # conf=0.5 -> scale=1.0, conf=1.0 -> scale=1.0+multiplier
        scale = 1.0 + (conf - 0.5) * self.multiplier
        scale = max(0.5, min(scale, 3.0))  # Clamp [0.5, 3.0]

        raw = float(signal) * self.base_qty * scale
        if abs(raw) < self.lot:
            return 0.0

        qty = round(raw / self.lot) * self.lot
        return min(abs(qty), self.max_qty) * (1 if qty > 0 else -1)


class VolTargetSizer(PositionSizer):
    """Target fixed dollar risk based on ATR."""

    def __init__(
        self,
        risk_per_trade: float = 200.0,
        max_qty: int = 1000,
        lot: int = 100,
        atr_multiple: float = 2.0,
    ):
        self.risk_per_trade = risk_per_trade
        self.max_qty = max_qty
        self.lot = lot
        self.atr_multiple = atr_multiple

    def calculate(
        self,
        signal: float,
        conf: float,
        price: float,
        atr: float,
        capital: float,
        **kwargs: Any,
    ) -> float:
        if atr <= 0 or price <= 0:
            return 0.0

        # Risk = shares * (atr_multiple * atr)
        # => shares = risk / (atr_multiple * atr)
        shares = self.risk_per_trade / (self.atr_multiple * atr)
        shares = min(shares, self.max_qty)

        if abs(shares) < self.lot:
            return 0.0

        qty = round(shares / self.lot) * self.lot
        return qty * signal  # Apply signal direction


class OptionsPremiumSizer(PositionSizer):
    """Sizer for options: cap premium spend per trade."""

    def __init__(
        self,
        max_premium_usd: float = 500.0,
        lot: int = 1,  # options contracts
        max_contracts: int = 10,
    ):
        self.max_premium_usd = max_premium_usd
        self.lot = lot
        self.max_contracts = max_contracts

    def calculate(
        self,
        signal: float,
        conf: float,
        price: float,
        atr: float,
        capital: float,
        **kwargs: Any,
    ) -> float:
        # price here is option mid/limit price (per contract)
        if price <= 0:
            return 0.0
        # total spend cap
        contracts = self.max_premium_usd / (price * 100.0)
        # scale by confidence
        scale = max(0.5, min(1.5, conf))  # simple scale
        contracts *= scale
        contracts = min(contracts, self.max_contracts)
        if abs(contracts) < self.lot:
            return 0.0
        qty = round(contracts / self.lot) * self.lot
        return qty * (1 if signal >= 0 else -1)


class KellySizer(PositionSizer):
    """Kelly Criterion position sizing (requires historical stats)."""

    def __init__(
        self,
        win_rate: float = 0.55,
        avg_win: float = 1.5,
        avg_loss: float = 1.0,
        max_qty: int = 1000,
        lot: int = 100,
        kelly_fraction: float = 0.5,  # Half-Kelly for safety
    ):
        self.win_rate = win_rate
        self.avg_win = avg_win
        self.avg_loss = avg_loss
        self.max_qty = max_qty
        self.lot = lot
        self.kelly_fraction = kelly_fraction

    def calculate(
        self,
        signal: float,
        conf: float,
        price: float,
        atr: float,
        capital: float,
        **kwargs: Any,
    ) -> float:
        if price <= 0 or capital <= 0:
            return 0.0

        # Kelly % = (p*W - (1-p)*L) / W
        p = self.win_rate
        kelly_pct = (p * self.avg_win - (1 - p) * self.avg_loss) / self.avg_win
        kelly_pct = max(0, min(kelly_pct * self.kelly_fraction, 0.25))  # Cap at 25%

        # Convert to shares
        shares = (capital * kelly_pct) / price
        shares = min(shares, self.max_qty)

        if abs(shares) < self.lot:
            return 0.0

        qty = round(shares / self.lot) * self.lot
        return qty * signal


# ============================================================================
# Module 2: Multi-Model Ensemble (多模型组合)
# ============================================================================


class MultiModelEnsemble:
    """Combine signals from multiple models."""

    def __init__(self, config: Dict[str, Any]):
        self.models = config.get("models", [{"name": "ml", "weight": 1.0}])
        self.mode = config.get("mode", "weighted_avg")
        self._validate_config()

    def _validate_config(self) -> None:
        if not self.models:
            raise ValueError("MultiModelEnsemble requires at least one model")

        total_weight = sum(m.get("weight", 0) for m in self.models)
        if abs(total_weight - 1.0) > 0.01:
            raise ValueError(f"Model weights must sum to 1.0, got {total_weight}")

    @staticmethod
    def _finite(value: Any, default: float = 0.0) -> float:
        """Convert value to finite float; fallback to default for NaN/inf/invalid."""
        try:
            v = float(value)
        except Exception:
            return default
        return v if np.isfinite(v) else default

    def combine_signals(self, features: Dict[str, Any]) -> Tuple[float, float]:
        """
        Combine multiple model signals.

        Expected feature keys:
            - {model_name}_signal: float [-1, 1]
            - {model_name}_conf: float [0, 1]

        Returns:
            (combined_signal, combined_confidence)
        """
        if self.mode == "weighted_avg":
            signal, conf = self._weighted_avg(features)
        elif self.mode == "vote":
            signal, conf = self._vote(features)
        elif self.mode == "max_conf":
            signal, conf = self._max_conf(features)
        else:
            raise ValueError(f"Unknown ensemble mode: {self.mode}")
        # Defensive clamp to keep downstream sizing/risk stable.
        signal = float(np.clip(self._finite(signal, 0.0), -1.0, 1.0))
        conf = float(np.clip(self._finite(conf, 0.0), 0.0, 1.0))
        return signal, conf

    def _weighted_avg(self, features: Dict[str, Any]) -> Tuple[float, float]:
        """Weighted average of all models."""
        signal = 0.0
        conf = 0.0

        for model in self.models:
            name = model["name"]
            weight = self._finite(model.get("weight", 0.0), 0.0)
            model_signal = self._finite(features.get(f"{name}_signal", 0.0), 0.0)
            model_conf = self._finite(features.get(f"{name}_conf", 0.0), 0.0)
            signal += model_signal * weight
            conf += model_conf * weight
        return signal, conf

    def _vote(self, features: Dict[str, Any]) -> Tuple[float, float]:
        """Majority vote (for classification)."""
        signals = []
        confs = []

        for model in self.models:
            name = model["name"]
            sig = self._finite(features.get(f"{name}_signal", 0.0), 0.0)
            c = self._finite(features.get(f"{name}_conf", 0.0), 0.0)
            signals.append(sig)
            confs.append(c)

        # Count positive vs negative signals
        n_long = sum(1 for s in signals if s > 0)
        n_short = sum(1 for s in signals if s < 0)

        if n_long > n_short:
            return 1.0, max(confs)
        elif n_short > n_long:
            return -1.0, max(confs)
        else:
            return 0.0, max(confs)  # Tie -> neutral

    def _max_conf(self, features: Dict[str, Any]) -> Tuple[float, float]:
        """Select the model with highest confidence."""
        best_model = max(
            self.models, key=lambda m: self._finite(features.get(f"{m['name']}_conf", 0.0), 0.0)
        )
        name = best_model["name"]
        return (
            self._finite(features.get(f"{name}_signal", 0.0), 0.0),
            self._finite(features.get(f"{name}_conf", 0.0), 0.0),
        )


# ============================================================================
# Module 2.5: Adaptive Policy Engine (自适应策略引擎)
# ============================================================================

class AdaptivePolicyEngine:
    """Lightweight rule evaluator producing per-tick overrides.

    Config schema (per symbol JSON):
        {
          "adaptive": {
            "enabled": true,
            "rules": [
              {"when": {"vix_gt": 20},
               "overrides": {"conf_floor": 0.7, "sizer_scale": 0.75}},
              {"when": {"time_lt": "09:50"},
               "overrides": {"sizer_scale": 0.5}},
              {"when": {"gap_up_pct_ge": 3.0},
               "overrides": {"sizer_scale": 0.6}},
              {"when": {"gap_dn_pct_ge": 3.0},
               "overrides": {"risk_stop_loss_pct": 0.015}},
              {"when": {"ret_1m_gt": 1.0},
               "overrides": {"conf_floor": 0.65}},
              {"when": {"spy_abs_ret_1m_gt": 0.8},
               "overrides": {"exec_mode": "TWAP", "exec_horizon_s": 120}},
              {"when": {"halt": true},
               "overrides": {"halt_for_sec": 900}}
            ]
          }
        }
    """

    def __init__(self) -> None:
        pass

    @staticmethod
    def _parse_hhmm(s: str) -> tuple[int, int]:
        try:
            hh, mm = s.split(":")
            return int(hh), int(mm)
        except Exception:
            return 0, 0

    def evaluate(self, symbol: str, sym_cfg: dict, features: dict) -> dict:
        ad = (sym_cfg or {}).get("adaptive") or {}
        if not ad or not ad.get("enabled"):
            return {}

        rules = ad.get("rules") or []
        out: dict = {}

        # Minimal context
        vix = float(features.get("vix", 0.0) or 0.0)
        now_et = datetime.now(ZoneInfo("America/New_York")).time()
        gap_pct = None
        try:
            # convention: gap_pct > 0 is up, <0 is down
            gap_pct = float(features.get("gap_pct")) if features.get("gap_pct") is not None else None
        except Exception:
            gap_pct = None
        ret_1m = None
        try:
            ret_1m = float(features.get("ret_1m")) if features.get("ret_1m") is not None else None
        except Exception:
            ret_1m = None
        spy_ret_1m = None
        for key in ("spy_ret_1m", "index_spy_ret_1m", "qqq_ret_1m"):
            if features.get(key) is not None:
                try:
                    spy_ret_1m = float(features.get(key))
                    break
                except Exception:
                    spy_ret_1m = None

        for r in rules:
            when = (r or {}).get("when") or {}
            ovr = (r or {}).get("overrides") or {}
            if not when or not ovr:
                continue

            ok = True
            if "vix_gt" in when:
                try:
                    if not (vix > float(when["vix_gt"])):
                        ok = False
                except Exception:
                    ok = False
            if ok and "time_lt" in when:
                hh, mm = self._parse_hhmm(str(when["time_lt"]))
                if not (now_et < time(hh, mm)):
                    ok = False
            if ok and "time_ge" in when:
                hh, mm = self._parse_hhmm(str(when["time_ge"]))
                if not (now_et >= time(hh, mm)):
                    ok = False
            if ok and "gap_up_pct_ge" in when:
                try:
                    thr = float(when["gap_up_pct_ge"]) / 100.0 if float(when["gap_up_pct_ge"]) > 1.0 else float(when["gap_up_pct_ge"]) / 100.0
                except Exception:
                    thr = None
                if gap_pct is None or thr is None or not (gap_pct >= thr):
                    ok = False
            if ok and "gap_dn_pct_ge" in when:
                try:
                    thr = float(when["gap_dn_pct_ge"]) / 100.0 if float(when["gap_dn_pct_ge"]) > 1.0 else float(when["gap_dn_pct_ge"]) / 100.0
                except Exception:
                    thr = None
                if gap_pct is None or thr is None or not (gap_pct <= -thr):
                    ok = False
            if ok and "ret_1m_gt" in when:
                try:
                    thr = float(when["ret_1m_gt"]) / 100.0 if float(when["ret_1m_gt"]) > 1.0 else float(when["ret_1m_gt"]) / 100.0
                except Exception:
                    thr = None
                if ret_1m is None or thr is None or not (ret_1m > thr):
                    ok = False
            if ok and "spy_abs_ret_1m_gt" in when:
                try:
                    thr = float(when["spy_abs_ret_1m_gt"]) / 100.0 if float(when["spy_abs_ret_1m_gt"]) > 1.0 else float(when["spy_abs_ret_1m_gt"]) / 100.0
                except Exception:
                    thr = None
                if spy_ret_1m is None or thr is None or not (abs(spy_ret_1m) > thr):
                    ok = False
            if ok and when.get("halt") is True:
                ok = True

            if ok:
                out.update(ovr)

        return out


# ============================================================================
# Module 3: Market Filter (市场过滤器)
# ============================================================================


class MarketFilter:
    """Filter trades based on market conditions."""

    def __init__(self, config: Dict[str, Any]):
        # Defensive helpers to make filter resilient to mis-configured values
        def _num(val: Any, default: float, as_int: bool = False) -> float:
            # Accept legacy dict-style specs: {"type": "int", "low": 5, ...}
            try:
                if isinstance(val, dict):
                    # Prefer explicit "value" if present, otherwise fall back to "low"
                    if "value" in val:
                        val = val["value"]
                    elif "low" in val:
                        val = val["low"]
                    else:
                        return default
                if as_int:
                    return float(int(val))
                return float(val)
            except Exception:
                return float(default)

        self.max_spread_bps = _num(config.get("max_spread_bps", 10.0), 10.0)
        # Volume is naturally integer, but we store as float for comparisons
        self.min_volume = _num(config.get("min_volume", 100000), 100000, as_int=True)
        self.max_vix = _num(config.get("max_vix", 0), 0.0)  # 0 = disabled
        self.check_after_hours = bool(config.get("check_after_hours", True))
        # Confidence thresholds: tolerate either raw numbers or legacy dict specs
        self.conf_floor = _num(config.get("conf_floor", 0.0), 0.0)  # Migrated from old config
        # Soft threshold: below this, allow but tag for size reduction
        self.conf_soft_threshold = _num(
            config.get("conf_soft_threshold", config.get("conf_floor", 0.0)),
            self.conf_floor,
        )
        self.conf_soft_sizedown_scale = _num(
            config.get("conf_soft_sizedown_scale", 0.5),
            0.5,
        )

    async def should_trade(
        self, ctx: Any, symbol: str, features: Dict[str, Any], conf: float
    ) -> Tuple[bool, str, Optional[float]]:
        """
        Check if trading is allowed.

        Returns:
            (allow, reason, size_scale)
            - allow: bool, whether to proceed
            - reason: str, rejection/warning reason
            - size_scale: Optional[float], scale factor for position sizing (None = no scaling, <1.0 = reduce)
        """
        size_scale: Optional[float] = None

        # Confidence handling:
        # Convert hard rejection into soft size-down to avoid "never trade" when models are under-confident.
        # If conf below conf_floor → allow with size_scale; if a higher soft threshold is configured, also size-down.
        try:
            if conf < float(self.conf_floor):
                size_scale = float(self.conf_soft_sizedown_scale)
            elif float(self.conf_soft_threshold) > 0.0 and conf < float(self.conf_soft_threshold):
                size_scale = float(self.conf_soft_sizedown_scale)
        except Exception:
            pass

        # Spread check
        spread_bps: Optional[float] = None
        # Prefer real-time L1 spread when available (live trading)
        try:
            price = ctx.get_price(symbol)
        except Exception:
            price = None
        if price is not None and hasattr(price, "spread_pct"):
            try:
                spread_bps = float(price.spread_pct) * 10000.0
            except Exception:
                spread_bps = None
        # Fallback for backtests / environments without PriceCache:
        # use pre-computed microstructure feature `spread_bps` when present.
        if spread_bps is None:
            try:
                spread_bps = float(features.get("spread_bps", 0.0) or 0.0)
            except Exception:
                spread_bps = None
        if spread_bps is not None and spread_bps > self.max_spread_bps:
            return False, f"spread_too_wide: {spread_bps:.1f}bp", None

        # Volume check
        volume = float(features.get("volume", 0))
        if volume > 0 and volume < self.min_volume:
            return False, f"low_volume: {volume:.0f}", None

        # VIX check (if configured)
        if self.max_vix > 0:
            vix = float(features.get("vix", 0))
            if vix > self.max_vix:
                return False, f"high_vix: {vix:.1f}", None

        # Market hours check
        if self.check_after_hours:
            # Extract bar timestamp (not real-time clock) for backtesting compatibility
            ts = features.get("ts")
            if ts is not None:
                # Convert to datetime if needed
                if isinstance(ts, pd.Timestamp):
                    bar_dt = ts.to_pydatetime()
                elif isinstance(ts, datetime):
                    bar_dt = ts
                else:
                    # Assume integer timestamp in microseconds or seconds
                    try:
                        ts_int = int(ts)
                        if ts_int > 1e12:  # Microseconds
                            bar_dt = datetime.fromtimestamp(ts_int / 1_000_000, tz=ZoneInfo("UTC"))
                        else:  # Seconds
                            bar_dt = datetime.fromtimestamp(ts_int, tz=ZoneInfo("UTC"))
                    except Exception:
                        bar_dt = datetime.now(ZoneInfo("America/New_York"))

                # Convert to Eastern timezone for market hours check
                bar_dt_et = bar_dt.astimezone(ZoneInfo("America/New_York"))
                market_open = time(9, 30)
                market_close = time(16, 0)
                if not (market_open <= bar_dt_et.time() <= market_close):
                    return False, "outside_regular_hours", None
            else:
                # Fallback to real-time clock if no timestamp available (live trading)
                now = datetime.now(ZoneInfo("America/New_York"))
                market_open = time(9, 30)
                market_close = time(16, 0)
                if not (market_open <= now.time() <= market_close):
                    return False, "outside_regular_hours", None

        return True, "ok", size_scale

    # End MarketFilter

    


# ============================================================================
# Module 4: Risk Manager - REMOVED
# ============================================================================
# Risk management is now handled by the system's OCO (One-Cancels-Other) protection
# configured in DecisionPipeline's Translator.
# See run_system.py for OCO configuration:
#   - take_profit_bp: 40 (0.4%)
#   - hard_stop_bp: 80 (0.8%)
#   - trail_atr_mult: 1.5


# ============================================================================
# Main Strategy: EnhancedModelSignalStrategy
# ============================================================================


@register_strategy("enhanced_model_signal")
class EnhancedModelSignalStrategy(StrategyAlgorithm):
    """
    Enhanced ML signal strategy with:
    - Multi-model ensemble
    - Dynamic position sizing
    - Market condition filtering
    - Risk management (stop loss, take profit)

    All functionality is self-contained (no system file modifications).
    """

    async def initialize(self) -> None:
        cfg: Dict[str, Any] = self.params.extras or {}

        # Extract symbols
        symbols = cfg.get("symbols", [])
        if not symbols:
            # Backward compatible: single symbol
            symbol = cfg.get("symbol", "")
            if not symbol:
                raise ValueError("EnhancedModelSignalStrategy requires 'symbols' or 'symbol' in extras")
            symbols = [symbol]
        self.symbols = symbols

        # Initialize config loader for per-symbol configs
        self.config_loader = get_loader(
            config_dir=cfg.get("config_dir", "strategies/configs"),
            prefix=cfg.get("config_prefix", "enhanced_ml"),
            check_interval_sec=cfg.get("config_reload_interval_sec", 60.0),
        )

        # Load per-symbol configs (merge with base config)
        self.symbol_configs: Dict[str, Dict[str, Any]] = {}
        for sym in self.symbols:
            self.symbol_configs[sym] = self.config_loader.load(sym, base_config=cfg)
            self.log.info("Symbol config loaded", symbol=sym, config_keys=list(self.symbol_configs[sym].keys()))

        # 1. Initialize multi-model ensemble
        ensemble_cfg = cfg.get("ensemble", {"models": [{"name": "ml", "weight": 1.0}], "mode": "weighted_avg"})
        self.ensemble = MultiModelEnsemble(ensemble_cfg)

        # 2. Initialize position sizer
        sizing_cfg = cfg.get("sizing", {"mode": "fixed"})
        self.sizer = self._create_sizer(sizing_cfg)

        # 3. Initialize market filter
        market_cfg = cfg.get("market_filter", {})
        # Backward compatible: migrate old conf_floor
        if "conf_floor" not in market_cfg and "conf_floor" in cfg:
            market_cfg["conf_floor"] = cfg["conf_floor"]
        self.market_filter = MarketFilter(market_cfg)

        # 4. Risk management - REMOVED (now handled by system OCO)

        # 4.1 Adaptive policy engine (rule-based)
        self.adaptive = AdaptivePolicyEngine()

        # 5. Order configuration
        self.order_type = cfg.get("order_type", "MKT").upper()
        self.limit_offset_bps = float(cfg.get("limit_offset_bps", 3.0))
        self.lot = int(cfg.get("lot", 100))
        # Optional options execution routing
        self.execution_routing = cfg.get("execution_routing", {})
        self.options_provider = None
        try:
            from services.market.options_data import create_options_provider
            backend = self.execution_routing.get("backend", "stub")
            self.options_provider = create_options_provider(backend)
            self.log.info("options_provider_initialized", backend=backend)
        except Exception as e:
            self.log.warning("options_provider_init_failed", error=str(e))

        # 6. Optional SFS micro-structure confirmation
        self.use_sfs = cfg.get("use_sfs_1s", False)
        if self.use_sfs:
            self.sfs_reader = SFS1sReader()
            self.sfs_lookback = int(cfg.get("sfs_lookback", 20))
            self.sfs_confirm_up = int(cfg.get("sfs_confirm_up", 6))
            self.sfs_confirm_down = int(cfg.get("sfs_confirm_down", 6))

        # 7. Subscribe to features (poll-based)
        bar_sec = cfg.get("bar_sec", 60)
        self.bar_sec = bar_sec
        await self.ctx.subscribe_features(self.symbols, bar_sec=bar_sec)

        # 7.a Optional: enable realtime feature engine (RFE) for low-latency features
        try:
            rfe_cfg = (cfg.get("realtime_feature_engine") or {})
            if bool(rfe_cfg.get("enabled", False)):
                await self.ctx.enable_realtime_feature_engine(self.symbols, spec=rfe_cfg)
                self.log.info("rfe_mode_enabled", symbols=self.symbols, emit_on=rfe_cfg.get("emit_on", "1m"))
        except Exception as e:
            self.log.warning("rfe_enable_failed", error=str(e))

        # 7.2 Initialize Real ML Inference Service (Model-based) and optional Gateway
        self.ml_service = None
        self.ml_gateway = None
        if _USE_REAL_ML:
            try:
                # Use 16-feature optimized model (Sharpe 4.80, IC 0.0161)
                model_dir = cfg.get("model_dir", "models/xgb_h30_tsla_selected_features")
                self.ml_service = get_model_service(model_dir=model_dir)
                if _USE_ML_GATEWAY:
                    try:
                        self.ml_gateway = AiBrainMLGateway(self.ml_service)
                        self.log.info(
                            "model_ml_gateway_enabled",
                            model_dir=model_dir,
                            backend="trained_xgboost",
                        )
                    except Exception as ge:
                        self.log.warning("model_ml_gateway_init_failed", error=str(ge))
                else:
                    self.log.info(
                        "model_ml_service_enabled",
                        model_dir=model_dir,
                        backend="trained_xgboost",
                    )
            except Exception as e:
                self.log.error("model_ml_service_init_failed", error=str(e), exc_info=True)
                self.ml_service = None
        else:
            self.log.warning("real_ml_service_unavailable",
                           message="Strategy will run WITHOUT ML predictions. Check services/ml installation.")

        # 7.1 Optional: subscribe hedge instrument quotes if hedging enabled
        self.hedging_cfg = cfg.get("hedging", {}) or {}
        # Build per-symbol hedging overrides using sector_map
        h_per_symbol = (self.hedging_cfg.get("per_symbol") or {})
        h_per_sector = (self.hedging_cfg.get("per_sector") or {})
        self.hedging_map: Dict[str, Dict[str, Any]] = {}
        # sector_map defined later; temporarily capture and update after computed
        _sector_map_boot = (cfg.get("sector_map") or {})
        for sym in self.symbols:
            sec = _sector_map_boot.get(sym)
            sec_ovr = (h_per_sector.get(sec) or {}) if sec else {}
            sym_ovr = (h_per_symbol.get(sym) or {})
            ov = dict(self.hedging_cfg)
            ov.update(sec_ovr)
            ov.update(sym_ovr)
            self.hedging_map[sym] = ov
        try:
            hedge_syms = set()
            for sym, hc in self.hedging_map.items():
                if hc.get("enabled") and hc.get("hedge_instrument"):
                    hedge_syms.add(hc["hedge_instrument"])
            if hedge_syms:
                await self.ctx.subscribe_features(sorted(list(hedge_syms)), bar_sec=bar_sec)
        except Exception:
            pass

        # 8. Optional per-symbol/per-sector overrides
        per_symbol = (cfg.get("per_symbol") or {})
        per_sector = (cfg.get("per_sector") or {})
        sector_map = (cfg.get("sector_map") or {})
        self.sector_map = sector_map

        self.sizer_map: Dict[str, PositionSizer] = {}
        self.market_filter_map: Dict[str, MarketFilter] = {}
        self.order_cfg_map: Dict[str, Dict[str, Any]] = {}

        def _merge(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
            out = dict(a or {})
            out.update(b or {})
            return out

        for sym in self.symbols:
            sec_ovr = {}
            sec = sector_map.get(sym)
            if sec and isinstance(per_sector, dict):
                sec_ovr = per_sector.get(sec, {}) or {}
            ovr = _merge(sec_ovr, (per_symbol.get(sym) or {}))

            if isinstance(ovr.get("sizing"), dict):
                self.sizer_map[sym] = self._create_sizer(_merge(sizing_cfg, ovr.get("sizing") or {}))
            if isinstance(ovr.get("market_filter"), dict):
                self.market_filter_map[sym] = MarketFilter(_merge(market_cfg, ovr.get("market_filter") or {}))
            if isinstance(ovr.get("order"), dict):
                self.order_cfg_map[sym] = _merge({
                    "order_type": self.order_type,
                    "limit_offset_bps": self.limit_offset_bps,
                    "lot": self.lot,
                }, ovr.get("order") or {})

        # Final runtime overrides: allow tools/tests to force conf_floor regardless of per-symbol JSON
        # Store as durable attribute so on_features() can enforce at evaluation time
        try:
            force_cf = cfg.get("force_conf_floor", None)
            if force_cf is not None:
                # Persist for runtime enforcement (applied after adaptive overrides)
                self._force_conf_floor = float(force_cf)
                # Also apply now to initial filters
                self.market_filter.conf_floor = float(force_cf)
                # per-symbol
                for sym in list(self.market_filter_map.keys()):
                    mf = self.market_filter_map.get(sym)
                    if mf:
                        mf.conf_floor = float(force_cf)
            else:
                self._force_conf_floor = None
        except Exception:
            self._force_conf_floor = None

        # 9. Position tracking (for Guardian monitoring)
        self.positions: Dict[str, Dict[str, Any]] = {}

        # 10. OCO Configuration (for system-level risk management)
        # Prefer explicit OCO config; if absent, derive sensible defaults from risk.*
        oco_cfg = (cfg.get("oco") or {}).copy()
        risk_cfg = (cfg.get("risk") or {}).copy()
        try:
            tp_pct = risk_cfg.get("take_profit_pct")
            sl_pct = risk_cfg.get("stop_loss_pct")
            trail_mult = risk_cfg.get("trail_atr_mult")
            if tp_pct is not None and "take_profit_bp" not in oco_cfg:
                oco_cfg["take_profit_bp"] = float(tp_pct) * 10_000.0
            if sl_pct is not None and "hard_stop_bp" not in oco_cfg:
                oco_cfg["hard_stop_bp"] = float(sl_pct) * 10_000.0
            if trail_mult is not None and "trail_atr_mult" not in oco_cfg:
                oco_cfg["trail_atr_mult"] = float(trail_mult)
        except Exception:
            # Fallback to raw oco_cfg/defaults if risk mapping fails
            pass
        self.oco_config = {
            "take_profit_bp": float(oco_cfg.get("take_profit_bp", 200)),  # Default 2.0%
            "hard_stop_bp": float(oco_cfg.get("hard_stop_bp", 150)),      # Default 1.5%
            "trail_atr_mult": float(oco_cfg.get("trail_atr_mult", 2.0)),  # Default 2.0x ATR
        }
        self.log.info("OCO config loaded",
                     take_profit_bp=self.oco_config["take_profit_bp"],
                     hard_stop_bp=self.oco_config["hard_stop_bp"])

        # Confidence calibration cache (per-symbol calibrators loaded from nightly OOF)
        # Structure: {symbol: {"bins": [(low, high, p), ...], "loaded_at": ts}}
        self._conf_calibrators: Dict[str, Dict[str, Any]] = {}

        # 11. RL Position Manager (optional)
        rl_cfg = (cfg.get("rl") or {})
        self.rl_enabled = bool(rl_cfg.get("enabled", False))
        self.rl_action_mode = str(rl_cfg.get("action_mode", "scale")).lower()  # scale | target_pct
        self.rl_max_scale_amp = float(rl_cfg.get("max_scale_amp", 0.5))
        self.rl_safety = rl_cfg.get("safety", {}) or {}
        self.rl_model = None
        if self.rl_enabled:
            try:
                from stable_baselines3 import SAC  # type: ignore
                model_path = rl_cfg.get("model_path") or os.getenv("RL_MODEL_PATH", "models/rl/sac_position_manager_latest.zip")
                if model_path and os.path.exists(model_path):
                    self.rl_model = SAC.load(model_path)
                    self.log.info("RL model loaded", path=model_path)
                else:
                    self.log.warning("RL model not found; disabling rl.enabled", path=model_path)
                    self.rl_enabled = False
            except Exception as e:
                self.log.warning("RL disabled (missing stable_baselines3 or load error)", error=str(e))
                self.rl_enabled = False

        # 11. Guardian (strategy-level real-time safety circuit)
        gcfg = (cfg.get("guardian") or {})
        self.guardian_enabled = bool(gcfg.get("enabled", False))
        self.guardian_dd_pct = float(gcfg.get("dd_intraday_pct", 0.0))  # 0=disabled
        self.guardian_max_trades_per_min = int(gcfg.get("max_trades_per_min", 0))  # 0=disabled
        self.guardian_max_gross_exposure_pct = float(gcfg.get("max_gross_exposure_pct", 0.0))  # 0=disabled
        self.guardian_on_trip = str(gcfg.get("on_trip", "halt")).lower()  # reduce|halt|flat
        self.guardian_reduce_scale = float(gcfg.get("reduce_scale", 0.5))
        self.guardian_cooldown_sec = int(gcfg.get("cooldown_sec", 1800))
        self.guardian_scale = 1.0
        self.guardian_halt_until = 0.0
        self.guardian_decisions = deque(maxlen=200)
        self.guardian_last_prices: Dict[str, float] = {}
        self.guardian_realized_pnl = 0.0
        try:
            self.guardian_base_capital = float(self.params.capital_limit_usd or 0.0)
        except Exception:
            self.guardian_base_capital = 0.0
        self.guardian_high_water = self.guardian_base_capital

        self.log.info(
            "EnhancedModelSignalStrategy initialized",
            symbols=self.symbols,
            ensemble_mode=ensemble_cfg.get("mode"),
            sizing_mode=sizing_cfg.get("mode"),
            use_sfs=self.use_sfs,
        )

    def _create_sizer(self, config: Dict[str, Any]) -> PositionSizer:
        """Factory method to create position sizer."""
        # Defensive numeric parsing to avoid crashes on bad configs/HPO artifacts.
        def _safe_int(key: str, default: int) -> int:
            raw = config.get(key, default)
            try:
                # Accept legacy dict-style specs: {"type": "int", "low": 50, ...}
                if isinstance(raw, dict):
                    if "value" in raw:
                        raw = raw["value"]
                    elif "low" in raw:
                        raw = raw["low"]
                    else:
                        raise ValueError("missing numeric field in spec")
                return int(raw)
            except Exception:
                try:
                    self.log.warning(
                        "invalid_sizer_param",
                        key=key,
                        value=str(raw),
                        default=default,
                    )
                except Exception:
                    pass
                return int(default)

        def _safe_float(key: str, default: float) -> float:
            raw = config.get(key, default)
            try:
                if isinstance(raw, dict):
                    if "value" in raw:
                        raw = raw["value"]
                    elif "low" in raw:
                        raw = raw["low"]
                    else:
                        raise ValueError("missing numeric field in spec")
                return float(raw)
            except Exception:
                try:
                    self.log.warning(
                        "invalid_sizer_param",
                        key=key,
                        value=str(raw),
                        default=default,
                    )
                except Exception:
                    pass
                return float(default)

        mode = str(config.get("mode", "fixed")).lower()
        base_qty = _safe_int("base_qty", 1000)
        max_qty = _safe_int("max_qty", 1000)
        lot = _safe_int("lot", 1)

        if mode == "fixed":
            return FixedSizer(base_qty=base_qty, max_qty=max_qty, lot=lot)

        elif mode == "confidence_scaled":
            multiplier = _safe_float("multiplier", 2.0)
            return ConfidenceScaledSizer(base_qty=base_qty, max_qty=max_qty, lot=lot, multiplier=multiplier)

        elif mode == "vol_target":
            risk_per_trade = _safe_float("risk_per_trade", 200.0)
            atr_multiple = _safe_float("atr_multiple", 2.0)
            return VolTargetSizer(risk_per_trade=risk_per_trade, max_qty=max_qty, lot=lot, atr_multiple=atr_multiple)

        elif mode == "kelly":
            win_rate = _safe_float("win_rate", 0.55)
            avg_win = _safe_float("avg_win", 1.5)
            avg_loss = _safe_float("avg_loss", 1.0)
            kelly_fraction = _safe_float("kelly_fraction", 0.5)
            return KellySizer(
                win_rate=win_rate,
                avg_win=avg_win,
                avg_loss=avg_loss,
                max_qty=max_qty,
                lot=lot,
                kelly_fraction=kelly_fraction,
            )
        elif mode == "options_premium":
            max_premium_usd = _safe_float("max_premium_usd", 500.0)
            max_contracts = _safe_int("max_contracts", 10)
            lot_contracts = _safe_int("lot", 1)
            return OptionsPremiumSizer(
                max_premium_usd=max_premium_usd,
                lot=lot_contracts,
                max_contracts=max_contracts,
            )

        else:
            raise ValueError(f"Unknown sizing mode: {mode}")

    @staticmethod
    def _ts_us(features: Dict[str, Any]) -> int:
        """Extract timestamp in microseconds."""
        ts = features.get("ts")
        if isinstance(ts, pd.Timestamp):
            return int(ts.value // 1000)
        if isinstance(ts, datetime):
            return int(ts.timestamp() * 1_000_000)
        if ts is None:
            return 0
        try:
            return int(ts)
        except Exception:
            return 0

    async def on_features(self, symbol: str, features: Dict[str, Any]) -> None:
        """Main strategy logic triggered on feature updates."""
        if symbol not in self.symbols:
            return

        # Prefer realtime features when RFE is enabled and warmed
        try:
            rfe_cfg = (self.params.extras or {}).get("realtime_feature_engine") or {}
            if bool(rfe_cfg.get("enabled", False)):
                feats_rfe, ts_us, warm_ok = self.ctx.get_realtime_features(symbol)
                if feats_rfe and warm_ok:
                    # Merge: RFE takes precedence for overlapping keys and timestamp
                    merged = dict(features or {})
                    merged.update(feats_rfe)
                    features = merged
        except Exception:
            pass

        # Get ML predictions via Gateway (preferred) or raw service fallback
        if self.ml_service is not None:
            try:
                loop = asyncio.get_running_loop()
                if self.ml_gateway is not None:
                    # Run blocking ML gateway inference in thread pool
                    gw = await loop.run_in_executor(
                        None, self.ml_gateway.get_decision, symbol, features
                    )
                    features["ml_signal"] = float(gw.get("ml_signal", 0.0) or 0.0)
                    features["ml_conf"] = float(gw.get("ml_conf", 0.0) or 0.0)
                    features["ml_raw_pred"] = float(gw.get("raw_pred", 0.5) or 0.5)
                    features["ml_decision"] = gw.get("decision")
                    features["ml_status"] = gw.get("status")
                    if "drift_score" in gw:
                        features["ml_drift_score"] = float(gw.get("drift_score") or 0.0)
                    if gw.get("decision") == "blocked":
                        features["ml_block_reason"] = gw.get("block_reason")
                    self.log.debug(
                        "ml_gateway_decision",
                        symbol=symbol,
                        decision=gw.get("decision"),
                        ml_signal=features["ml_signal"],
                        ml_conf=round(features["ml_conf"], 3),
                    )
                else:
                    # Fallback: direct model service predictions (legacy path)
                    # Run blocking model inference in thread pool
                    predictions = await loop.run_in_executor(
                        None, self.ml_service.predict, features
                    )
                    features.update(predictions)
                    self.log.debug(
                        "ml_predictions_injected",
                        symbol=symbol,
                        ml_signal=predictions.get("ml_signal"),
                        ml_conf=round(predictions.get("ml_conf", 0), 3),
                    )
            except Exception as e:
                self.log.error("ml_predict_failed", symbol=symbol, error=str(e), exc_info=True)
                # Continue without ML predictions (strategy will use other signals)

        # Timestamp alignment guard (prevent data-mixing between fast price and slower features)
        try:
            # Extract feature timestamp (us)
            f_ts_us = int(self._ts_us(features) or 0)
            # Extract price snapshot and timestamp (best-effort)
            p = self.ctx.get_price(symbol)
            p_ts_us = 0
            if p is not None:
                for attr in ("ts_us", "ts", "timestamp", "last_ts"):
                    if hasattr(p, attr):
                        val = getattr(p, attr)
                        try:
                            # If val is datetime
                            if isinstance(val, datetime):
                                p_ts_us = int(val.timestamp() * 1_000_000)
                                break
                            # If val looks like seconds or microseconds
                            ival = int(val)
                            if ival > 1_000_000_000_000:  # microseconds
                                p_ts_us = ival
                            else:  # seconds
                                p_ts_us = ival * 1_000_000
                            break
                        except Exception:
                            continue
            # When both timestamps are available, enforce max drift
            if p_ts_us > 0 and f_ts_us > 0:
                try:
                    extras = self.params.extras or {}
                    rfe_cfg = (extras.get("realtime_feature_engine") or {})
                    max_drift_ms = int(rfe_cfg.get("ts_alignment_ms_max_drift", 200))
                except Exception:
                    max_drift_ms = 200
                drift_ms = abs(p_ts_us - f_ts_us) / 1000.0
                if drift_ms > max_drift_ms:
                    # Skip this tick to avoid mixing misaligned data
                    try:
                        log_event(
                            "order_skipped",
                            component="Strategy",
                            strategy_id=self.ctx.strategy_id,
                            symbol=symbol,
                            reason="ts_misaligned",
                            reason_detail=f"drift_ms={drift_ms:.1f} > {max_drift_ms}",
                        )
                    except Exception:
                        pass
                    return
        except Exception:
            # Guard must never crash strategy; if anything goes wrong, proceed normally
            pass

        # Hot-reload per-symbol config
        sym_cfg = self.config_loader.load(symbol, base_config=self.params.extras or {})
        if sym_cfg.get("disabled") is True:
            # Per-symbol hard disable (hot-reloadable)
            log_event(
                "order_skipped",
                component="Strategy",
                strategy_id=self.ctx.strategy_id,
                symbol=symbol,
                reason="symbol_disabled",
                reason_detail="disabled=true"
            )
            return

        # Guardian pre-trade: update last price snapshot
        if self.guardian_enabled:
            try:
                p_pre = self.ctx.get_price(symbol)
                if p_pre and hasattr(p_pre, "last") and p_pre.last > 0:
                    self.guardian_last_prices[symbol] = float(p_pre.last)
            except Exception:
                pass

        # === Step 1: Combine model signals ===
        try:
            signal, conf = self.ensemble.combine_signals(features)
        except Exception as e:
            self.log.error("Failed to combine signals", symbol=symbol, error=str(e))
            return

        # Optional: apply per-symbol confidence calibration if available (nightly OOF output)
        try:
            conf_cal = self._apply_conf_calibration(symbol, conf)
            if conf_cal is not None:
                conf = float(conf_cal)
        except Exception:
            pass

        # === Step 1.5: Adaptive overrides (conf_floor / sizer_scale / halt) ===
        overrides: Dict[str, Any] = {}
        try:
            overrides = self.adaptive.evaluate(symbol, sym_cfg, features)
        except Exception:
            overrides = {}
        # Observability: log adaptive overrides when present
        if overrides:
            try:
                from pathlib import Path as _P
                import json as _J
                dstr = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
                p = _P("runs/ai_trader") / dstr / "adaptive_events.jsonl"
                p.parent.mkdir(parents=True, exist_ok=True)
                evt = {
                    "ts": datetime.now(ZoneInfo("America/New_York")).isoformat(),
                    "strategy_id": self.ctx.strategy_id,
                    "symbol": symbol,
                    "overrides": overrides,
                }
                with p.open("a", encoding="utf-8") as f:
                    f.write(_J.dumps(evt) + "\n")
                # Optional: ship to Loki
                try:
                    from libs.obs.loki import push as loki_push
                    loki_push({"job": "ai_trader", "component": "adaptive", "strategy": str(self.ctx.strategy_id)}, evt)
                except Exception:
                    pass
            except Exception:
                pass

        # === Step 2: Market filter ===
        mf = self.market_filter_map.get(symbol, self.market_filter)
        # Apply adaptive MarketFilter overrides if provided
        try:
            override_conf = overrides.get("conf_floor")
            override_spread = overrides.get("max_spread_bps")
            override_soft_thr = overrides.get("conf_soft_threshold")
            override_soft_scale = overrides.get("conf_soft_sizedown_scale")
            if any(v is not None for v in (override_conf, override_spread, override_soft_thr, override_soft_scale)):
                mf = MarketFilter({
                    "max_spread_bps": float(override_spread if override_spread is not None else mf.max_spread_bps),
                    "min_volume": mf.min_volume,
                    "max_vix": mf.max_vix,
                    "check_after_hours": mf.check_after_hours,
                    "conf_floor": float(override_conf if override_conf is not None else mf.conf_floor),
                    "conf_soft_threshold": float(override_soft_thr if override_soft_thr is not None else getattr(mf, "conf_soft_threshold", 0.0)),
                    "conf_soft_sizedown_scale": float(override_soft_scale if override_soft_scale is not None else getattr(mf, "conf_soft_sizedown_scale", 0.5)),
                })
        except Exception:
            pass

        # Enforce force_conf_floor at runtime (overrides adaptive/per-symbol settings)
        # This ensures backtest tools (e.g., compare) can truly disable conf gating
        try:
            if getattr(self, "_force_conf_floor", None) is not None:
                mf.conf_floor = float(self._force_conf_floor)
        except Exception:
            pass

        allow, reason, mf_size_scale = await mf.should_trade(self.ctx, symbol, features, conf)
        if not allow:
            log_event(
                "order_skipped",
                component="Strategy",
                strategy_id=self.ctx.strategy_id,
                symbol=symbol,
                sig=signal,
                conf=conf,
                reason="market_filter",
                reason_detail=reason,
            )
            return

        # Log soft confidence warnings
        if mf_size_scale is not None and mf_size_scale < 1.0:
            log_event(
                "order_size_reduced",
                component="Strategy",
                strategy_id=self.ctx.strategy_id,
                symbol=symbol,
                conf=conf,
                reason="low_conf_soft",
                size_scale=mf_size_scale,
            )

        # === Step 3: Risk management - REMOVED ===
        # Strategy-layer risk management has been removed.
        # Position protection (stop-loss/take-profit) is now handled by the system's OCO
        # which is automatically attached by SafetyKernel after the first fill.

        # Guardian: if halted, block new entries
        if self.guardian_enabled and self._guardian_is_halted():
            log_event(
                "order_skipped",
                component="Strategy",
                strategy_id=self.ctx.strategy_id,
                symbol=symbol,
                reason="guardian_halt",
                reason_detail="halt_window_active",
            )
            return

        # Adaptive immediate halt
        if overrides.get("halt_for_sec"):
            try:
                now_ts = datetime.now(ZoneInfo("America/New_York")).timestamp()
                self.guardian_halt_until = max(float(self.guardian_halt_until or 0.0), now_ts + float(overrides["halt_for_sec"]))
            except Exception:
                pass

        # === Step 4: Calculate target position ===
        if abs(signal) < 1e-9:
            return  # No signal

        price = self.ctx.get_price(symbol)
        if not price or not hasattr(price, "last") or price.last <= 0:
            # Backtest/simulator fallback: derive price from features
            try:
                close_px = float(features.get("close") or 0.0)
                if close_px > 0:
                    class _P:  # minimal struct for backtest
                        __slots__ = ("last", "spread_pct")
                        def __init__(self, last: float, spread_pct: float):
                            self.last = last
                            self.spread_pct = spread_pct
                    sp_bps = 0.0
                    try:
                        sp_bps = float(features.get("spread_bps") or 0.0)
                    except Exception:
                        sp_bps = 0.0
                    price = _P(close_px, (sp_bps / 10000.0) if sp_bps else 0.0)
                else:
                    return
            except Exception:
                return

        atr = float(features.get("atr_14", 0) or features.get("atr14", 0) or 0)
        capital = self.params.capital_limit_usd

        sizer = self.sizer_map.get(symbol, self.sizer)
        try:
            target_qty = sizer.calculate(
                signal=signal, conf=conf, price=price.last, atr=atr, capital=capital
            )
            self.log.info(f"DEBUG: Sizer calculated target_qty={target_qty}, symbol={symbol}")
        except Exception as e:
            self.log.error("Failed to calculate position size", symbol=symbol, error=str(e))
            return

        # Adaptive size scaling
        try:
            scale = float(overrides.get("sizer_scale", 1.0) or 1.0)
            target_qty = float(target_qty) * max(0.0, min(scale, 3.0))
        except Exception:
            pass

        # Market filter soft confidence scaling
        if mf_size_scale is not None and mf_size_scale < 1.0:
            try:
                target_qty = float(target_qty) * float(mf_size_scale)
            except Exception:
                pass

        # RL scaling (queries position for state building)
        if self.rl_enabled and self.rl_model is not None:
            try:
                position_view = await self.ctx.position(symbol)
                base_qty_before_rl = target_qty  # Record base quantity before RL
                target_qty, rl_action, rl_state = await self._apply_rl(symbol, target_qty, price.last, float(capital or 0.0), features, position_view)
                # Log RL behavior for analysis (v2: 9-dim state)
                self.log.info(
                    "rl_scaling",
                    symbol=symbol,
                    base_qty=float(base_qty_before_rl),
                    rl_action=float(rl_action),
                    final_qty=float(target_qty),
                    scale_ratio=float(target_qty / base_qty_before_rl) if base_qty_before_rl != 0 else 0.0,
                    # Core state (v1)
                    state_ml_sig=float(rl_state[0]) if len(rl_state) > 0 else 0.0,
                    state_ml_conf=float(rl_state[1]) if len(rl_state) > 1 else 0.0,
                    state_cur_pct=float(rl_state[4]) if len(rl_state) > 4 else 0.0,
                    # Extended state (v2)
                    state_ret_1=float(rl_state[5]) if len(rl_state) > 5 else 0.0,
                    state_ret_5=float(rl_state[6]) if len(rl_state) > 6 else 0.0,
                    state_spread=float(rl_state[7]) if len(rl_state) > 7 else 0.0,
                )
            except Exception as e:
                self.log.warning("RL scaling failed; fallback to base qty", error=str(e))

        # Guardian scaling (reduce size when tripped)
        if self.guardian_enabled and self.guardian_scale < 1.0:
            try:
                target_qty = float(target_qty) * float(self.guardian_scale)
            except Exception:
                pass

        lot = int((self.order_cfg_map.get(symbol, {}) or {}).get("lot", self.lot))
        if abs(target_qty) < lot:
            return

        # === Step 5: Optional SFS micro-structure confirmation ===
        if self.use_sfs:
            confirmed = await self._sfs_confirm(symbol, target_qty)
            if not confirmed:
                return

        # === Step 5.5: Optional Beta hedging for low-confidence trades ===
        try:
            hedge_cfg = (self.hedging_map.get(symbol) or self.hedging_cfg) or {}
            if hedge_cfg.get("enabled") and conf < float(hedge_cfg.get("confidence_threshold", 0.0)) and abs(target_qty) > 0:
                hedge_sym = hedge_cfg.get("hedge_instrument")
                beta = float(hedge_cfg.get("beta", 1.0))
                if hedge_sym and beta != 0.0:
                    hpx = self.ctx.get_price(hedge_sym)
                    if hpx and hasattr(hpx, "last") and hpx.last > 0 and price and hasattr(price, "last") and price.last > 0:
                        primary_val = float(target_qty) * float(price.last)
                        hedge_val = -1.0 * primary_val * beta  # opposite direction
                        hedge_qty = hedge_val / float(hpx.last)
                        hedge_lot = int((self.order_cfg_map.get(hedge_sym, {}) or {}).get("lot", self.lot))
                        if abs(hedge_qty) >= hedge_lot:
                            # Submit hedge intent (market order)
                            order_style = OrderStyle(kind="MKT")
                            ts_us = self._ts_us(features) or self.ctx.now_us()
                            # Using already imported classes at top of file
                            envelope = DecisionEnvelope(
                                decision_id=make_decision_id(),
                                strategy_id=self.ctx.strategy_id,
                                symbol=hedge_sym,
                                ts_bar_end_us=int(ts_us),
                                target_qty=float(hedge_qty),
                                order_style=order_style,
                                freshness=Freshness(ttl_ms=self.params.ttl_ms, dlb_ms=500),
                                extras={"hedging": True, "hedge_for": symbol, "hedge_beta": beta, "ref_px": float(hpx.last)},
                            )
                            await self.ctx.submit_intent(envelope)
                            # Append local hedging event for later MLflow aggregation (best-effort)
                            try:
                                from pathlib import Path as _P
                                import json as _J
                                dstr = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
                                p = _P("runs/ai_trader") / dstr / "hedging_events.jsonl"
                                p.parent.mkdir(parents=True, exist_ok=True)
                                evt = {
                                    "ts": datetime.now(ZoneInfo("America/New_York")).isoformat(),
                                    "strategy_id": self.ctx.strategy_id,
                                    "symbol": symbol,
                                    "hedge_symbol": hedge_sym,
                                    "qty": float(target_qty),
                                    "hedge_qty": float(hedge_qty),
                                    "beta": float(beta),
                                    "ref_px": float(hpx.last),
                                    "conf": float(conf),
                                }
                                with p.open("a", encoding="utf-8") as f:
                                    f.write(_J.dumps(evt) + "\n")
                            except Exception:
                                pass
        except Exception as e:
            self.log.warning("hedging_failed", error=str(e))

        # === Step 6: Guardian trip checks before submit ===
        if self.guardian_enabled:
            tripped, reason, metrics = self._guardian_check(symbol)
            if tripped:
                await self._guardian_act(reason, metrics)
                # After action, reassess: if halted → block; if reduce → proceed with scaled qty; if flat → early return
                if self._guardian_is_halted() or self.guardian_on_trip == "flat":
                    return

        # === Step 7: Submit decision ===
        await self._submit_decision(symbol, target_qty, signal, conf, features, price.last, overrides)
        # Record decision timestamp for freq monitor
        if self.guardian_enabled:
            try:
                self.guardian_decisions.append(datetime.now(ZoneInfo("America/New_York")).timestamp())
            except Exception:
                pass

    async def _sfs_confirm(self, symbol: str, target_qty: float) -> bool:
        """SFS 1s micro-structure confirmation."""
        try:
            subbars = await self.sfs_reader.last_n(symbol, max(10, self.sfs_lookback))
            if len(subbars) < 5:
                return True  # Not enough data, allow trade

            closes = [float(x.get("close") or 0) for x in subbars if x]
            if len(closes) < 5:
                return True

            # Count upward vs downward moves (newest first in Redis)
            ups = sum(1 for i in range(min(10, len(closes) - 1)) if closes[i] > closes[i + 1])

            if target_qty > 0 and ups < self.sfs_confirm_up:
                log_event(
                    "order_skipped",
                    component="Strategy",
                    strategy_id=self.ctx.strategy_id,
                    symbol=symbol,
                    reason="sfs_confirm_fail",
                    reason_detail=f"sfs_up10={ups} < {self.sfs_confirm_up}",
                )
                return False

            if target_qty < 0 and (10 - ups) < self.sfs_confirm_down:
                log_event(
                    "order_skipped",
                    component="Strategy",
                    strategy_id=self.ctx.strategy_id,
                    symbol=symbol,
                    reason="sfs_confirm_fail",
                    reason_detail=f"sfs_down10={10-ups} < {self.sfs_confirm_down}",
                )
                return False

            return True

        except Exception:
            return True  # Non-fatal: proceed without confirmation

    async def _submit_decision(
        self,
        symbol: str,
        target_qty: float,
        signal: float,
        conf: float,
        features: Dict[str, Any],
        ref_px: float,
        overrides: Dict[str, Any] | None = None,
    ) -> None:
        """Submit trading intent."""
        # Determine order style
        ovr = self.order_cfg_map.get(symbol, {}) or {}
        order_type = (ovr.get("order_type") or self.order_type)
        limit_offset_bps = float(ovr.get("limit_offset_bps", self.limit_offset_bps))
        order_style = OrderStyle(kind="MKT")
        if order_type == "LMT":
            offset = ref_px * (limit_offset_bps / 10_000.0)
            limit_px = ref_px - offset if target_qty > 0 else ref_px + offset
            order_style = OrderStyle(kind="LMT", limit_px=limit_px)

        # Optional: reroute execution to options if enabled
        exec_mode = self.execution_routing or {}
        use_options = bool(exec_mode.get("allow_options", False)) and self.options_provider is not None
        final_symbol = symbol
        final_qty = target_qty
        option_limit_px = None
        instrument_type = "STOCK"
        hedged_selected = False
        # Simple EV filter: requires both conf (as p_win proxy) and risk/reward hints
        ev_cfg = exec_mode.get("ev_filter", {})
        min_rr = float(ev_cfg.get("min_rr", 1.0))
        min_p_win = float(ev_cfg.get("min_p_win", 0.0))
        min_ev = float(ev_cfg.get("min_ev", 0.0))
        # risk_reward from overrides or config; default 2:1
        rr_hint = float((ovr.get("risk_reward") or ev_cfg.get("risk_reward", 2.0)))
        p_win = max(0.0, min(1.0, conf))
        if rr_hint <= 0:
            rr_hint = 1.0
        ev = p_win * rr_hint - (1 - p_win)
        if ev < min_ev or p_win < min_p_win or rr_hint < min_rr:
            self.log.info("ev_filter_blocked",
                          ev=ev,
                          p_win=p_win,
                          rr=rr_hint,
                          min_p_win=min_p_win,
                          min_rr=min_rr,
                          min_ev=min_ev,
                          symbol=symbol)
            return
        self.log.info("DEBUG: hedge check", require_hedge=exec_mode.get("require_hedge_for_high_risk"), use_options=use_options, symbol=symbol)
        # Optional: enforce hedged/structured execution if configured
        if exec_mode.get("require_hedge_for_high_risk") and not use_options:
            self.log.info("hedge_required_blocking_naked_trade",
                          symbol=symbol,
                          target_qty=target_qty,
                          reason="require_hedge_for_high_risk")
            return
        if use_options and abs(target_qty) >= 1:
            try:
                opt_pref = exec_mode.get("option_preferences", {})
                dte_range = opt_pref.get("dte_range", [7, 14, 30])
                min_oi = int(opt_pref.get("min_open_interest", opt_pref.get("min_liquidity", 100)))
                max_spread_pct = float(opt_pref.get("max_spread_pct", 0.05))
                target_delta = float(opt_pref.get("target_delta", 0.6))
                right = "C" if signal >= 0 else "P"
                chain = await self.options_provider.get_chain(symbol, dte_range, right=right)
                contract = self._select_alpha_contract(chain, target_delta, min_oi, max_spread_pct)
                if contract and contract.mid > 0:
                    final_symbol = contract.symbol
                    # convert shares to contracts (100 shares per contract)
                    final_qty = int(target_qty / 100)
                    if final_qty != 0:
                        hedged_selected = True
                        instrument_type = "OPTION"
                        # force limit order around mid
                        slippage = float(opt_pref.get("slippage_pct", 0.01))
                        option_limit_px = contract.mid * (1 + slippage if target_qty > 0 else 1 - slippage)
                        order_style = OrderStyle(kind="LMT", limit_px=option_limit_px)
                        self.log.info("option_execution_selected",
                                      symbol=final_symbol,
                                      contracts=final_qty,
                                      delta=contract.delta,
                                      mid=contract.mid,
                                      right=contract.right,
                                      expiry=contract.expiry_date)
                    else:
                        # if too small, fallback to stock
                        final_symbol = symbol
                        final_qty = target_qty
                # if no contract found, fallback silently to stock
            except Exception as e:
                        self.log.warning("option_routing_failed", error=str(e))
            # If hedge is required but we could not select an option, block to avoid naked trades.
            if exec_mode.get("require_hedge_for_high_risk") and not hedged_selected:
                self.log.info(
                    "hedge_required_blocking_naked_trade",
                    symbol=symbol,
                    target_qty=target_qty,
                    reason="require_hedge_for_high_risk_no_option",
                )
                return

        ts_us = self._ts_us(features)
        if not ts_us:
            ts_us = self.ctx.now_us()

        side = "BUY" if target_qty > 0 else "SELL"
        log_event(
            "decision_made",
            component="Strategy",
            strategy_id=self.ctx.strategy_id,
            symbol=final_symbol,
            side=side,
            qty=abs(final_qty),
            sig=signal,
            conf=conf,
            order_type=order_style.kind,
        )

        envelope = DecisionEnvelope(
            decision_id=make_decision_id(),
            strategy_id=self.ctx.strategy_id,
            symbol=final_symbol,
            ts_bar_end_us=int(ts_us),
            target_qty=final_qty,
            order_style=order_style,
            freshness=Freshness(ttl_ms=self.params.ttl_ms, dlb_ms=500),
            extras={
                "ml_signal": signal,
                "ml_conf": conf,
                "ref_px": ref_px,
                "oco_cfg": self.oco_config,  # Pass strategy-level OCO config to Translator
                "instrument_type": instrument_type,
            },
        )

        # Optional execution hints (TWAP/POV/IOC)
        try:
            exec_cfg = (self.order_cfg_map.get(symbol, {}) or {}).get("exec") or {}
            # Adaptive override takes precedence if present
            if overrides:
                ad_mode = overrides.get("exec_mode")
                if ad_mode:
                    exec_cfg = {"mode": ad_mode,
                                "horizon_s": overrides.get("exec_horizon_s"),
                                "participation": overrides.get("exec_pov")}
            if isinstance(exec_cfg, dict) and exec_cfg.get("mode"):
                mode = str(exec_cfg.get("mode")).upper()
                if mode in ("TWAP", "POV", "IOC"):
                    envelope.extras["exec_child"] = mode
                    if mode == "TWAP" and exec_cfg.get("horizon_s"):
                        envelope.extras["exec_horizon_s"] = int(exec_cfg.get("horizon_s"))
                    if mode == "POV" and exec_cfg.get("participation"):
                        envelope.extras["exec_pov"] = float(exec_cfg.get("participation"))
        except Exception:
            pass

        await self.ctx.submit_intent(envelope)

        # Track active symbols for Guardian monitoring
        self.positions[symbol] = {
            "last_decision_ts": datetime.now(ZoneInfo("America/New_York")),
        }

    def _select_alpha_contract(self, chain, target_delta: float, min_oi: int, max_spread_pct: float):
        """
        Select best option contract for alpha execution.
        """
        if not chain:
            return None
        # Filter by liquidity and spread
        candidates = []
        for c in chain:
            if c.open_interest and c.open_interest < min_oi:
                continue
            mid = c.mid
            spread_pct = c.spread / mid if mid > 0 else 1.0
            if spread_pct > max_spread_pct:
                continue
            candidates.append(c)
        if not candidates:
            return None
        # Sort by delta closeness to target
        candidates.sort(key=lambda c: abs((abs(c.delta or 0)) - target_delta))
        return candidates[0]

        # Guardian: block new entries during halt window (next bars)

    # ---------------- RL helpers ----------------
    def _build_rl_state(self, symbol: str, features: Dict[str, Any], position, price_last: float) -> np.ndarray:
        """
        Build RL state vector (v2: expanded; naming aligned to pipeline).

        State components:
        - ml_signal, ml_conf: ML predictions
        - atr_14, volume: volatility/liquidity
        - cur_pct: current position percentage
        - ret_1m, ret_5m: short-term momentum
        - spread_bps: microstructure (bid-ask spread)
        - spy_ret_1m: market correlation proxy
        """
        cur_qty = float(getattr(position, "pos_qty", 0.0) or 0.0)
        cur_pct = 0.0
        try:
            cap = float(self.params.capital_limit_usd or 0.0)
            if price_last > 0 and cap > 0:
                cur_pct = (cur_qty * price_last) / cap
        except Exception:
            pass

        # Core features (v1)
        ml_sig = float(features.get("ml_signal", 0.0) or 0.0)
        ml_conf = float(features.get("ml_conf", 0.0) or 0.0)
        atr = float(features.get("atr_14", 0.0) or 0.0)
        vol = float(features.get("volume", 0.0) or 0.0)

        # Extended features (v2) with robust naming fallbacks
        try:
            ret_1m = float(
                features.get("ret_1m", features.get("ret_1", 0.0)) or 0.0
            )
        except Exception:
            ret_1m = 0.0
        try:
            ret_5m = float(
                features.get("ret_5m", features.get("ret_5", 0.0)) or 0.0
            )
        except Exception:
            ret_5m = 0.0
        # spread_bps from features or derive from price cache when available
        try:
            spread_bps = float(features.get("spread_bps", 0.0) or 0.0)
            if spread_bps == 0.0:
                px = self.ctx.get_price(symbol)
                if px and getattr(px, "spread_pct", None):
                    spread_bps = float(px.spread_pct) * 10000.0
        except Exception:
            spread_bps = 0.0
        # index 1m return from multiple keys
        spy_ret_1m = 0.0
        for k in ("spy_ret_1m", "index_spy_ret_1m", "qqq_ret_1m", "spy_ret_1"):
            try:
                if features.get(k) is not None:
                    spy_ret_1m = float(features.get(k) or 0.0)
                    break
            except Exception:
                continue

        return np.array([ml_sig, ml_conf, atr, vol, cur_pct, ret_1m, ret_5m, spread_bps, spy_ret_1m], dtype=np.float32)

    async def _apply_rl(self, symbol: str, base_qty: float, price_last: float, capital: float, features: Dict[str, Any], position) -> Tuple[float, float, np.ndarray]:
        """
        Apply RL scaling to base position size.

        Returns:
            (target_qty, rl_action, state): Final quantity, raw RL action, and state vector
        """
        state = self._build_rl_state(symbol, features, position, price_last)
        
        # Run blocking RL model inference in thread pool
        loop = asyncio.get_running_loop()
        # Note: predict returns (action, state) tuple
        action, _ = await loop.run_in_executor(
            None, 
            lambda: self.rl_model.predict(state.reshape(1, -1), deterministic=True)
        )
        
        a = float(np.clip(action[0], -1.0, 1.0))
        if self.rl_action_mode == "target_pct" and price_last > 0 and capital > 0:
            target_pct = a
            target_qty = (target_pct * capital) / price_last
        else:
            target_qty = base_qty * (1.0 + a * self.rl_max_scale_amp)
        # safety clamps
        max_pos_pct = float(self.rl_safety.get("max_position_pct", 0.25))
        max_step_pct = float(self.rl_safety.get("max_step_pct", 0.30))
        cur_qty = float(getattr(position, "pos_qty", 0.0) or 0.0)
        if price_last > 0 and capital > 0 and max_pos_pct > 0:
            max_shares = (max_pos_pct * capital) / price_last
            target_qty = float(np.clip(target_qty, -max_shares, max_shares))
        if price_last > 0 and capital > 0 and max_step_pct > 0:
            max_step = (max_step_pct * capital) / price_last
            target_qty = float(np.clip(target_qty, cur_qty - max_step, cur_qty + max_step))
        return target_qty, a, state

    # _submit_exit method REMOVED - no longer needed
    # Exit logic (stop-loss/take-profit) is handled by system OCO

    async def on_position_update(self, position: Any) -> None:
        """Clean up position tracking when position is closed."""
        if hasattr(position, "symbol") and hasattr(position, "pos_qty"):
            if position.pos_qty == 0 and position.symbol in self.positions:
                del self.positions[position.symbol]

    # ---------------- Guardian helpers ----------------
    def _guardian_is_halted(self) -> bool:
        if not self.guardian_enabled:
            return False
        try:
            now_ts = datetime.now(ZoneInfo("America/New_York")).timestamp()
        except Exception:
            now_ts = datetime.now().timestamp()
        return now_ts < float(self.guardian_halt_until or 0.0)

    def _guardian_equity_and_exposure(self) -> Tuple[float, float]:
        """
        Calculate equity and exposure based on current market prices.

        Note: This is a simplified calculation. For accurate PnL, use PortfolioService.
        """
        cap = float(self.guardian_base_capital or 0.0)
        exposure = 0.0

        # Calculate gross exposure based on current prices
        for s in (self.positions or {}).keys():
            px = float(self.guardian_last_prices.get(s) or 0.0)
            if px > 0:
                # Estimate: each symbol might have a position (need to query for exact qty)
                # For now, use a rough estimate based on tracked symbols
                # This is imperfect but avoids async query in sync method
                # TODO: Refactor to async method and query actual positions
                exposure += abs(px * 100)  # Rough estimate: 100 shares per symbol

        # Simplified equity calculation (cap + realized PnL)
        # Without tracking entry prices, we can't calculate unrealized PnL
        equity = cap + float(self.guardian_realized_pnl or 0.0)
        expo_pct = (exposure / cap) if cap > 0 else 0.0

        if equity > self.guardian_high_water:
            self.guardian_high_water = equity

        return equity, expo_pct

    def _guardian_check(self, symbol: str) -> Tuple[bool, str, Dict[str, float]]:
        # Compute metrics
        equity, expo_pct = self._guardian_equity_and_exposure()
        dd_pct = 0.0
        try:
            if self.guardian_high_water > 0:
                dd_pct = max(0.0, (self.guardian_high_water - equity) / self.guardian_high_water)
        except Exception:
            dd_pct = 0.0
        # trades/min in last 60s
        tpm = 0.0
        try:
            now_ts = datetime.now(ZoneInfo("America/New_York")).timestamp()
        except Exception:
            now_ts = datetime.now().timestamp()
        try:
            window = [t for t in list(self.guardian_decisions) if now_ts - t <= 60.0]
            tpm = float(len(window))
        except Exception:
            tpm = 0.0
        # Evaluate thresholds
        if self.guardian_dd_pct and dd_pct >= self.guardian_dd_pct:
            return True, "drawdown", {"dd_pct": dd_pct, "expo_pct": expo_pct, "tpm": tpm}
        if self.guardian_max_gross_exposure_pct and expo_pct >= self.guardian_max_gross_exposure_pct:
            return True, "exposure", {"dd_pct": dd_pct, "expo_pct": expo_pct, "tpm": tpm}
        if self.guardian_max_trades_per_min and tpm >= float(self.guardian_max_trades_per_min):
            return True, "trade_burst", {"dd_pct": dd_pct, "expo_pct": expo_pct, "tpm": tpm}
        return False, "", {"dd_pct": dd_pct, "expo_pct": expo_pct, "tpm": tpm}

    # ---------------- Confidence calibration helpers ----------------
    def _apply_conf_calibration(self, symbol: str, conf: float) -> Optional[float]:
        """Map raw confidence to calibrated probability if calibrator is available.

        Calibrator format (JSON): {"method":"binning","bins":[{"low":0.0,"high":0.1,"p":0.12}, ...]}
        Files live under runs/ai_trader/YYYY-MM-DD/oof_eval/calibration_<SYM>.json
        """
        try:
            cal = self._get_conf_calibrator(symbol)
            if not cal:
                return None
            bins = cal.get("bins") or []
            x = float(conf)
            best = None
            for b in bins:
                try:
                    low = float(b.get("low")); high = float(b.get("high")); p = float(b.get("p"))
                except Exception:
                    continue
                if low <= x <= high:
                    best = p; break
            if best is None and bins:
                first = bins[0]; last = bins[-1]
                if x < float(first.get("low", 0.0)):
                    best = float(first.get("p", x))
                else:
                    best = float(last.get("p", x))
            return float(best) if best is not None else None
        except Exception:
            return None

    def _get_conf_calibrator(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Load the latest per-symbol calibrator if not cached."""
        try:
            if symbol in self._conf_calibrators:
                return self._conf_calibrators.get(symbol)
            from pathlib import Path as _P
            base = _P("runs/ai_trader")
            if not base.exists():
                return None
            latest = None
            # Iterate dates descending (YYYY-MM-DD)
            for day_dir in sorted([p for p in base.iterdir() if p.is_dir()], reverse=True):
                cand = day_dir / "oof_eval" / f"calibration_{symbol.upper()}.json"
                if cand.exists():
                    latest = cand; break
            if latest is None:
                return None
            import json as _J
            cal = _J.loads(latest.read_text("utf-8"))
            self._conf_calibrators[symbol] = cal
            return cal
        except Exception:
            return None

    async def _guardian_act(self, reason: str, metrics: Dict[str, float]) -> None:
        try:
            now_ts = datetime.now(ZoneInfo("America/New_York")).timestamp()
        except Exception:
            now_ts = datetime.now().timestamp()
        act = self.guardian_on_trip
        if act == "reduce":
            self.guardian_scale = min(self.guardian_scale, max(0.1, self.guardian_reduce_scale))
        elif act == "halt":
            self.guardian_halt_until = now_ts + float(self.guardian_cooldown_sec)
        elif act == "flat":
            # Close all positions best-effort and halt
            try:
                for s in list(self.positions.keys()):
                    pos = await self.ctx.position(s)
                    if pos and hasattr(pos, "pos_qty") and pos.pos_qty != 0:
                        px = self.ctx.get_price(s)
                        ref_px = float(getattr(px, "last", 0.0) or 0.0)
                        qty = abs(float(pos.pos_qty))
                        if qty > 0 and ref_px > 0:
                            # Submit closing order via standard flow
                            target_qty = -qty if pos.pos_qty > 0 else qty
                            envelope = DecisionEnvelope(
                                decision_id=make_decision_id(),
                                strategy_id=self.ctx.strategy_id,
                                symbol=s,
                                ts_bar_end_us=self.ctx.now_us(),
                                target_qty=target_qty,
                                order_style=OrderStyle(kind="MKT"),
                                freshness=Freshness(ttl_ms=self.params.ttl_ms, dlb_ms=500),
                                extras={"guardian_flat": reason, "ref_px": ref_px},
                            )
                            await self.ctx.submit_intent(envelope)
            except Exception:
                pass
            self.guardian_halt_until = now_ts + float(self.guardian_cooldown_sec)
        # write guardian event for observability
        try:
            from pathlib import Path as _P
            import json as _J
            dstr = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
            p = _P("runs/ai_trader") / dstr / "guardian_events.jsonl"
            p.parent.mkdir(parents=True, exist_ok=True)
            evt = {
                "ts": datetime.now(ZoneInfo("America/New_York")).isoformat(),
                "strategy_id": self.ctx.strategy_id,
                "action": act,
                "reason": reason,
                **{k: float(metrics.get(k, 0.0)) for k in ("dd_pct", "expo_pct", "tpm")},
            }
            with p.open("a", encoding="utf-8") as f:
                f.write(_J.dumps(evt) + "\n")
        except Exception:
            pass
