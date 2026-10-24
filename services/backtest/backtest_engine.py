"""Lightweight backtest engine aligned with research workflow (doc/backtest/9)."""

from __future__ import annotations

import importlib.util
import inspect
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

import pandas as pd

from services.backtest.sim_adapter import SimAdapter
from libs.logger import get_logger

try:
    from portfolio_overlay import PortfolioOverlay, OverlayConfig
    PORTFOLIO_OVERLAY_AVAILABLE = True
except Exception:  # pragma: no cover - optional dependency
    PORTFOLIO_OVERLAY_AVAILABLE = False

    class OverlayConfig:  # type: ignore
        def __init__(self, **kwargs: Any) -> None:
            for k, v in kwargs.items():
                setattr(self, k, v)

    class PortfolioOverlay:  # type: ignore
        def __init__(self, cfg: Any = None, **_: Any) -> None:
            pass

        def adjust_order(self, symbol: str, side: str, qty: int, ref_px: float) -> int:
            return qty

        def on_fill(self, symbol: str, side: str, qty: int, price: float) -> None:
            return

        @property
        def halted(self) -> bool:
            return False

        class _State:
            peak_nav = 0.0
            halted = False

        @property
        def state(self):
            class _State:
                peak_nav = 0.0
                halted = False

            return _State()

try:
    from leakage_guard import enforce_point_in_time
except Exception:  # pragma: no cover - optional dependency
    def enforce_point_in_time(df: pd.DataFrame, ts_col: str = "ts", cutoff_col: str = "feature_cutoff_ts") -> pd.DataFrame:
        return df


class BacktestMode(Enum):
    BACKTEST = "backtest"


@dataclass
class BacktestConfig:
    """Compatibility wrapper so existing helpers/tests keep working."""

    symbols: List[str]
    start: str
    end: str
    data: Optional[pd.DataFrame] = None
    csv: Optional[str] = None
    overlay: Dict[str, Any] = field(default_factory=dict)
    strategy_path: str = ""
    strategy_class: str = ""
    strategy_params: Dict[str, Any] = field(default_factory=dict)
    starting_cash: float = 1_000_000.0
    commission_per_share: float = 0.005
    commission_min: float = 1.0
    slippage_bps: float = 1.0
    dynamic_spread_bps: float = 0.0
    borrow_rate: float = 0.0
    participation_cap: float = 0.1
    fill_ref: str = "open"
    extras: Dict[str, Any] = field(default_factory=dict)
    mode: BacktestMode = BacktestMode.BACKTEST
    run_id: str = "bt"

    def to_engine_config(self) -> Dict[str, Any]:
        return {
            "symbols": self.symbols,
            "data": self.data,
            "csv": self.csv,
            "overlay": self.overlay,
            "strategy": {
                "path": self.strategy_path,
                "class": self.strategy_class,
                "params": self.strategy_params,
            },
            "starting_cash": self.starting_cash,
            "commission_per_share": self.commission_per_share,
            "commission_min": self.commission_min,
            "slippage_bps": self.slippage_bps,
            "dynamic_spread_bps": self.dynamic_spread_bps,
            "borrow_rate": self.borrow_rate,
            "participation_cap": self.participation_cap,
            "fill_ref": self.fill_ref,
            "run_id": self.run_id,
            "extras": self.extras,
        }


class BacktestEngine:
    """Minimal event-driven backtest runner that mirrors research stack."""

    def __init__(self, config: Dict[str, Any] | BacktestConfig):
        if isinstance(config, BacktestConfig):
            self.cfg = config.to_engine_config()
        else:
            self.cfg = dict(config)

        overlay_cfg_dict = self.cfg.get("overlay", {}) or {}
        if PORTFOLIO_OVERLAY_AVAILABLE and overlay_cfg_dict:
            overlay_cfg = OverlayConfig(**overlay_cfg_dict)
            self.overlay = PortfolioOverlay(cfg=overlay_cfg)
        elif PORTFOLIO_OVERLAY_AVAILABLE:
            overlay_cfg = OverlayConfig()
            self.overlay = PortfolioOverlay(cfg=overlay_cfg)
        else:
            self.overlay = PortfolioOverlay()
        self.sim = SimAdapter(
            starting_cash=float(self.cfg.get("starting_cash", 1_000_000.0)),
            commission_per_share=float(self.cfg.get("commission_per_share", 0.005)),
            commission_min=float(self.cfg.get("commission_min", 1.0)),
            slippage_bps=float(self.cfg.get("slippage_bps", 1.0)),
            dynamic_spread_bps=float(self.cfg.get("dynamic_spread_bps", 0.0)),
            borrow_rate=float(self.cfg.get("borrow_rate", 0.0)),
            participation_cap=float(self.cfg.get("participation_cap", 0.1)),
            fill_ref=str(self.cfg.get("fill_ref", "open")),
            overlay=self.overlay,
        )

        self.logger = get_logger(__name__)
        self.strategy = self._load_strategy()

    def _get_logger(self):
        return self.logger

    # ------------------------------------------------------------------
    def _load_strategy(self):
        strat_cfg = self.cfg.get("strategy") or {}
        module_path = strat_cfg.get("path")
        class_name = strat_cfg.get("class")
        if not module_path or not class_name:
            raise ValueError("Strategy path/class required in config")

        spec = importlib.util.spec_from_file_location("research_strategy", module_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot load strategy module from {module_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[arg-type]
        klass = getattr(module, class_name)

        # Create backtest-compatible context and params
        run_id = self.cfg.get("run_id", "bt")

        # Check if we need to use the new base class (with ctx parameter)
        try:
            from libs.strategy import StrategyParams
            # New base class - create params object
            params = StrategyParams(
                capital_limit_usd=float(self.cfg.get("starting_cash", 100000.0)),
                extras=strat_cfg.get("params", {})
            )

            # Create minimal context for backtest
            class BacktestContext:
                def __init__(self, strategy_id: str, engine: Any, params: Any):
                    self.strategy_id = strategy_id
                    self.engine = engine
                    self.params = params
                    self.log = engine._get_logger() if hasattr(engine, "_get_logger") else None
                    self.metrics = None
                    self.price_cache = None

                def param(self, key: str, default: Any = None) -> Any:
                    """Get strategy parameter from extras"""
                    return self.params.extras.get(key, default)

                # Stub methods for backtest compatibility
                async def subscribe_features(self, *args: Any, **kwargs: Any):
                    """Stub - backtest loads all data upfront"""
                    return None

                async def subscribe_tick_data(self, *args: Any, **kwargs: Any):
                    symbols = []
                    if args:
                        if isinstance(args[0], list):
                            symbols = list(args[0])
                        elif len(args) >= 2 and isinstance(args[1], list):
                            symbols = list(args[1])
                    if not symbols:
                        symbols = list(kwargs.get("symbols") or [])
                    return {str(symbol): True for symbol in symbols}

                async def history(self, *, symbol: str, resolution: str = "1m", limit: int = 15):
                    """Return the latest backtest rows for a symbol up to the current cursor."""
                    if self.engine.df is None or self.engine.df.empty:
                        return []
                    symbol_u = str(symbol or "").strip().upper()
                    df = self.engine.df
                    subset = df[df["symbol"].astype(str).str.upper() == symbol_u]
                    if getattr(self.engine, "_current_ts", None) is not None and "ts" in subset.columns:
                        subset = subset[subset["ts"] <= self.engine._current_ts]
                    if subset.empty:
                        return []
                    tail = subset.tail(int(limit)).copy()
                    rows = []
                    for _, row in tail.iterrows():
                        rows.append(
                            {
                                "ts": row.get("ts"),
                                "open": row.get("open", row.get("o")),
                                "high": row.get("high", row.get("h")),
                                "low": row.get("low", row.get("l")),
                                "close": row.get("close", row.get("c")),
                                "volume": row.get("volume", row.get("v")),
                            }
                        )
                    return rows

                async def submit_intent(self, envelope: Any):
                    """Forward intent to engine for execution"""
                    await self.engine.submit_intent(self.strategy_id, envelope)

                async def get_price(self, symbol: str) -> Optional[Any]:
                    """Stub - backtest uses bar data, not L1 cache"""
                    return None

                async def history_features(self, symbol: str, bar_type: str, limit: int) -> Optional[List[Dict[str, Any]]]:
                    """Stub - provide historical features from pre-loaded DataFrame."""
                    if self.engine.df is None or self.engine.df.empty:
                        return None
                    # Filter for the specific symbol
                    symbol_df = self.engine.df[self.engine.df["symbol"] == symbol]
                    if symbol_df.empty:
                        return None
                    # Take the last 'limit' rows, convert to dicts
                    # NOTE: bar_type is ignored here as only one bar type is loaded in simple backtest
                    return symbol_df.tail(limit).to_dict(orient="records")

                def get_minute_stats(self, symbol: str) -> Optional[Any]:
                    """Stub - not available in backtest"""
                    return None

                def portfolio(self) -> Optional[Any]:
                    """Get current portfolio state from sim adapter"""
                    if hasattr(self.engine, "sim"):
                        return {
                            "nav": self.engine.sim.current_nav(),
                            "cash": self.engine.sim.cash,
                            "positions": self.engine.sim.positions,
                        }
                    return None

                def position(self, symbol: str) -> Optional[Any]:
                    """Get position for specific symbol"""
                    if hasattr(self.engine, "sim"):
                        return self.engine.sim.positions.get(symbol)
                    return None

            ctx = BacktestContext(run_id, self, params)
            return klass(ctx, params)
        except (ImportError, TypeError):
            # Old base class - use legacy initialization
            params = {"extras": strat_cfg.get("params", {})}
            return klass(strategy_id=run_id, engine=self, params=params)

    # ------------------------------------------------------------------
    def _load_data(self) -> pd.DataFrame:
        if self.cfg.get("data") is not None:
            df = pd.DataFrame(self.cfg["data"])  # defensive copy
        else:
            csv_path = self.cfg.get("csv")
            if not csv_path:
                raise ValueError("Backtest config requires 'data' DataFrame or 'csv' path")
            if csv_path.endswith(".parquet"):
                df = pd.read_parquet(csv_path)
            else:
                df = pd.read_csv(csv_path)

        if "ts" not in df.columns:
            raise ValueError("Input data must contain 'ts' column")

        df["ts"] = pd.to_datetime(df["ts"], utc=False, errors="coerce")
        df = df.sort_values(["ts", "symbol" if "symbol" in df.columns else "ts"]).reset_index(drop=True)

        if "feature_cutoff_ts" in df.columns:
            df["feature_cutoff_ts"] = pd.to_datetime(df["feature_cutoff_ts"], utc=False, errors="coerce")
            df = enforce_point_in_time(df, ts_col="ts", cutoff_col="feature_cutoff_ts")

        symbols = self.cfg.get("symbols")
        if symbols and "symbol" in df.columns:
            df = df[df["symbol"].isin(symbols)].copy()

        if "symbol" not in df.columns:
            df["symbol"] = symbols[0] if symbols else "SYMBOL"

        return df

    # ------------------------------------------------------------------
    async def submit_intent(self, strategy_id: str, decision: Any) -> None:
        # Handle both dataclass (DecisionEnvelope) and dict
        symbol = getattr(decision, "symbol", None)
        side = getattr(decision, "side", None)
        qty = getattr(decision, "target_qty", 0)
        order_style = getattr(decision, "order_style", None)

        # Determine order type and limit price from order_style
        order_type = "MKT"
        limit_px = None
        if order_style:
            order_type = getattr(order_style, "kind", "MKT")
            limit_px = getattr(order_style, "limit_price", None)

        # Default to BUY if side not specified
        if not side:
            # Infer from qty: positive = BUY, negative = SELL
            side = "BUY" if qty > 0 else "SELL"
        side = side.upper()
        ts = int(getattr(decision, "ts_bar_end_us", 0) or 0) // 1000  # Convert microseconds to milliseconds

        order = {
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "type": str(order_type),
            "limit_price": limit_px,
            "ts": ts,
        }
        await self.sim.submit_order(order)

    # ------------------------------------------------------------------
    async def run(self) -> Dict[str, Any]:
        df = self._load_data()
        self.df = df # Make df accessible to BacktestContext

        init = getattr(self.strategy, "initialize", None)
        if init:
            if inspect.iscoroutinefunction(init):
                await init()
            else:
                init()

        on_features = getattr(self.strategy, "on_features", None)
        if not on_features:
            raise AttributeError("Strategy must implement on_features")

        # Track equity curve over time
        equity_curve = []
        starting_cash = self.cfg.get("starting_cash", 1_000_000.0)

        for idx, row in df.iterrows():
            feat = row.to_dict()
            self._current_ts = feat.get("ts")
            await self.sim.on_market_data(feat)
            symbol = str(feat.get("symbol"))

            # Record equity before strategy decision
            ts = feat.get("ts")
            if ts:
                # Build price map for NAV calculation
                px_map = {symbol: float(feat.get("close", feat.get("open", 0.0)))}
                equity = self.sim.current_nav(px_map)
                equity_curve.append({
                    "ts": int(pd.Timestamp(ts).timestamp() * 1000) if isinstance(ts, pd.Timestamp) else int(ts),
                    "equity": float(equity)
                })

            if inspect.iscoroutinefunction(on_features):
                await on_features(symbol, feat)
            else:
                on_features(symbol, feat)

        # FIX Bug #10: Force close all positions at end of backtest to create round-trip trades
        # This ensures trades count > 0 even if strategy only goes one direction
        snapshot = self.sim.snapshot()
        open_positions = snapshot.get("positions", {})
        if open_positions and df is not None and not df.empty:
            # Use last bar's close price for final liquidation
            last_row = df.iloc[-1]
            last_symbol = str(last_row.get("symbol", ""))
            last_price = float(last_row.get("close", last_row.get("open", 0.0)))

            for symbol, pos_details in open_positions.items():
                pos_qty = pos_details.get("qty", 0) # Extract the quantity
                if pos_qty != 0:
                    # Close position by submitting opposite order
                    close_order = {
                        "symbol": symbol,
                        "qty": abs(pos_qty),
                        "side": "BUY" if pos_qty < 0 else "SELL",
                        "order_type": "MKT",
                        "price": last_price,  # Market order uses last close price
                    }
                    await self.sim.submit_order(close_order)
                    self.logger.info(
                        "backtest_end_close_position",
                        symbol=symbol,
                        position=pos_qty,
                        close_side=close_order["side"],
                        close_qty=close_order["qty"],
                        price=last_price,
                    )

        return self._build_result(df, equity_curve)

    # ------------------------------------------------------------------
    def _build_result(self, df: pd.DataFrame, equity_curve: List[Dict[str, Any]]) -> Dict[str, Any]:
        ledger = self.sim.get_trade_ledger()
        snapshot = self.sim.snapshot()

        total_fee = sum(entry.get("fee", 0.0) for entry in ledger)
        realized = sum(entry.get("realized_pnl", 0.0) for entry in ledger)
        trades = [entry for entry in ledger if entry.get("qty", 0) > 0]
        wins = sum(1 for entry in trades if entry.get("realized_pnl", 0.0) > 0)
        losses = sum(1 for entry in trades if entry.get("realized_pnl", 0.0) < 0)
        win_rate = wins / (wins + losses) if (wins + losses) else 0.0
        gross_profit = sum(max(entry.get("realized_pnl", 0.0), 0.0) for entry in trades)
        gross_loss = sum(-min(entry.get("realized_pnl", 0.0), 0.0) for entry in trades)
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")

        metrics = {
            "final_nav": snapshot.get("equity", 0.0),
            "cash": snapshot.get("cash", 0.0),
            "total_fee": total_fee,
            "realized_pnl": realized,
            "trades": len(trades),
            "win_rate": win_rate,
            "profit_factor": profit_factor,
        }

        return {
            "metrics": metrics,
            "ledger": ledger,
            "snapshot": snapshot,
            "bars_processed": int(len(df)),
            "equity_curve": equity_curve,
            "trades": trades,  # Add trades list for compatibility
        }
