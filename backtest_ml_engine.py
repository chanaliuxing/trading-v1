# -*- coding: utf-8 -*-
"""
note.this backtest_engine.py is used for machine learning model training and evaluation.
Backtest Engine (vectorized bar-by-bar, async strategy API)
----------------------------------------------------------
- Loads historical features (ClickHouse or DataFrame)
- Calls strategy.on_features() per bar
- Routes intents -> SimAdapter orders
- Metrics from strict trade ledger (win rate, profit factor) + equity curve KPIs
- Appends equity once per timestamp (after processing ALL symbols of that ts)

Author: ChatGPT
"""

from __future__ import annotations
import os, math, importlib, asyncio, statistics, json, gc, weakref
from contextlib import nullcontext
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Callable, Iterable, Tuple
from datetime import datetime, timezone
import pandas as pd
import numpy as np

import boot_logging  # noqa: F401
import structlog

try:
    from opentelemetry import trace
except Exception:  # pragma: no cover
    trace = None  # type: ignore

_TRACER = trace.get_tracer("trader.loop") if trace else None

from leakage_guard import enforce_point_in_time
from libs.logger import get_logger
from types import SimpleNamespace
from libs.strategy import StrategyContext, StrategyParams

# Optional portfolio overlay
try:
    from portfolio_overlay import PortfolioOverlay, OverlayConfig
    HAS_OVERLAY = True
except ImportError:
    HAS_OVERLAY = False
    PortfolioOverlay = None
    OverlayConfig = None

# Optional ClickHouse clients (lazy import)
def _get_clickhouse():
    try:
        import clickhouse_connect as ch  # pip install clickhouse-connect
        return ("connect", ch)
    except Exception:
        try:
            from clickhouse_driver import Client  # pip install clickhouse-driver
            return ("driver", Client)
        except Exception:
            return (None, None)

# ---- Engine Data Structures ----

@dataclass
class BacktestConfig:
    symbols: List[str]
    start: str                   # "YYYY-MM-DD HH:MM:SS"
    end: str                     # "
    timeframe: str = "1m"
    features_table: Optional[str] = None
    bars_table: Optional[str] = None
    clickhouse: Dict[str, Any] = field(default_factory=dict)
    initial_cash: float = 100_000.0
    commission_per_share: float = 0.005
    commission_min: float = 1.0
    slippage_bps: float = 1.0
    fill_ref: str = "open"
    strategy_path: str = "tsla_v7_2.TSLASimplifiedV7_2"
    strategy_params: Dict[str, Any] = field(default_factory=dict)
    overlay_config: Optional[Dict[str, Any]] = None  # PortfolioOverlay configuration
    seed: int = 42

@dataclass
class TradeRecord:
    ts: int
    symbol: str
    side: str
    qty: float
    price: float
    commission: float

@dataclass
class Metrics:
    total_return: float
    sharpe: float
    max_drawdown: float
    win_rate: float
    profit_factor: float
    trades: int


def build_labels_from_profile(df: pd.DataFrame, label_cfg: Dict[str, Any], target_col: str = "y") -> pd.DataFrame:
    """
    根据 label_cfg (从 label_profiles.yml 加载) 生成标签列。
    支持多种转换、剪裁、winsorize、分类化。

    Args:
        df: DataFrame with at least ['symbol', 'ts', 'close'] columns
        label_cfg: Label profile configuration from label_profiles.yml
        target_col: Target column name to create (default: "y")

    Returns:
        df with new target_col added

    Example label_cfg:
        {
          "task": "reg",
          "horizon_bars": 5,
          "base": {
            "from_col": "close",
            "to_col": "close",
            "transform": "log_return",  # pct_change / log_return / diff / use_column
            "agg": "sum"
          },
          "clip": {"enabled": true, "lower_pct": 0.005, "upper_pct": 0.995},
          "winsorize": {"enabled": true, "lower_pct": 0.01, "upper_pct": 0.99},
          "binarize": {"type": "sign", "labels": {"pos": 1, "neg": 0}}  # optional for classification
        }
    """
    base = label_cfg.get("base", {})
    transform = base.get("transform", "log_return")

    # 1. 计算原始标签
    if transform == "use_column":
        # 直接使用已有列
        col_name = base.get("column", target_col)
        if col_name not in df.columns:
            raise KeyError(f"use_column mode: column '{col_name}' not found in DataFrame")
        df[target_col] = df[col_name].astype(float)
    else:
        # 计算未来收益
        horizon = label_cfg.get("horizon_bars", 1)
        from_col = base.get("from_col", "close")
        to_col = base.get("to_col", "close")
        agg = base.get("agg", "last")

        if from_col not in df.columns:
            raise KeyError(f"from_col '{from_col}' not found in DataFrame")
        if to_col not in df.columns:
            raise KeyError(f"to_col '{to_col}' not found in DataFrame")

        def _compute_return_series(col_series):
            """对单列数据计算未来收益 (for transform)"""
            now_vals = col_series.astype(float)
            future_vals = col_series.astype(float).shift(-horizon)

            if transform == "pct_change":
                ret = (future_vals / now_vals) - 1.0
            elif transform == "log_return":
                ret = np.log(future_vals) - np.log(now_vals)
            elif transform == "diff":
                ret = future_vals - now_vals
            else:
                raise ValueError(f"Unknown transform: {transform}")

            # 如果需要聚合多根bar（horizon > 1）
            if agg == "sum" and horizon > 1:
                # Rolling sum over horizon
                ret = ret.rolling(horizon, min_periods=1).sum()
            elif agg == "mean" and horizon > 1:
                ret = ret.rolling(horizon, min_periods=1).mean()
            # agg == "last": 不需要额外处理

            return ret

        if "symbol" in df.columns:
            # Use transform to maintain index alignment
            if from_col == to_col:
                # Same column, can use transform directly
                df[target_col] = df.groupby("symbol", dropna=False, sort=False)[from_col].transform(_compute_return_series)
            else:
                # Different columns - need to handle separately per symbol
                results = []
                for symbol, group in df.groupby("symbol", dropna=False, sort=False):
                    group = group.copy()
                    group[target_col] = _compute_return_series(group[from_col])
                    results.append(group[[target_col]])
                df[target_col] = pd.concat(results, axis=0)[target_col]
        else:
            df[target_col] = _compute_return_series(df[from_col])

    # 2. Clip 极端值
    clip_cfg = label_cfg.get("clip", {})
    if clip_cfg.get("enabled", False):
        lower_pct = clip_cfg.get("lower_pct", 0.01)
        upper_pct = clip_cfg.get("upper_pct", 0.99)
        lower = df[target_col].quantile(lower_pct)
        upper = df[target_col].quantile(upper_pct)
        df[target_col] = df[target_col].clip(lower, upper)

    # 3. Winsorize
    winsorize_cfg = label_cfg.get("winsorize", {})
    if winsorize_cfg.get("enabled", False):
        lower_pct = winsorize_cfg.get("lower_pct", 0.01)
        upper_pct = winsorize_cfg.get("upper_pct", 0.99)
        lower = df[target_col].quantile(lower_pct)
        upper = df[target_col].quantile(upper_pct)
        df[target_col] = np.where(df[target_col] < lower, lower, df[target_col])
        df[target_col] = np.where(df[target_col] > upper, upper, df[target_col])

    # 4. 分类化（仅当 task=clf 时）
    task = label_cfg.get("task", "reg")
    if task == "clf":
        binarize = label_cfg.get("binarize", {})
        btype = binarize.get("type", "sign")

        if btype == "sign":
            # 简单正负号
            labels = binarize.get("labels", {"pos": 1, "neg": 0})
            df[target_col] = np.where(df[target_col] > 0, labels["pos"], labels["neg"])

        elif btype == "threshold":
            # 单阈值二分类
            threshold = binarize.get("threshold", 0.0)
            labels = binarize.get("labels", {"pos": 1, "neg": 0})
            df[target_col] = np.where(df[target_col] > threshold, labels["pos"], labels["neg"])

        elif btype == "three_class":
            # 三分类: up / flat / down
            pos_th = binarize.get("pos_threshold", 0.001)
            neg_th = binarize.get("neg_threshold", -0.001)
            labels = binarize.get("labels", {"up": 1, "flat": 0, "down": -1})
            df[target_col] = np.where(
                df[target_col] > pos_th, labels["up"],
                np.where(df[target_col] < neg_th, labels["down"], labels["flat"])
            )

    return df


def _import_strategy_class(path: str):
    mod_name, cls_name = path.rsplit(".", 1)
    mod = importlib.import_module(mod_name)
    return getattr(mod, cls_name)

class _EngineShim:
    def __init__(self, outer):
        # 🔥 Use weakref.proxy to break circular reference: Engine → Strategy → Context → Engine
        try:
            self._outer = weakref.proxy(outer)
        except TypeError:
            # Fallback for objects that don't support weakref
            self._outer = outer

    async def submit_intent(self, strategy_id: str, decision) -> None:
        self._outer._pending_decisions.append((strategy_id, decision))

    async def subscribe_features(self, strategy_id: str, symbols: List[str], bar_sec: int) -> None:
        # Backtest: no-op, strategy will receive features from loaded DataFrame
        return None

    async def history_features(self, symbol: str, resolution: str, limit: int, fields: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        # Serve history from the loaded dataset (_data)
        # If 5m data is requested but only 1m data is available, resample it
        import pandas as pd
        df = getattr(self._outer, "_data", None)
        if df is None or symbol not in set(df.get("symbol", [])):
            return []

        # Get current simulation time from the last processed bar
        current_ts = getattr(self._outer, "_current_ts", None)

        sub = df[df["symbol"] == symbol].sort_values("ts").copy()

        # Filter to data up to current simulation time (point-in-time correctness)
        if current_ts is not None and not sub.empty:
            sub = sub[sub["ts"] <= current_ts].copy()

        # Handle multi-timeframe: resample 1m to 5m if needed
        if resolution == "5m" and not sub.empty:
            # Check if we have 1m data (detect by checking frequency)
            if len(sub) > 1:
                time_diff = (sub.iloc[1]["ts"] - sub.iloc[0]["ts"]).total_seconds()
                if time_diff <= 120:  # If data is 1m or 2m frequency, resample to 5m
                    sub = sub.set_index("ts")
                    # Resample OHLCV data
                    resampled = pd.DataFrame()
                    resampled["open"] = sub["open"].resample("5T").first()
                    resampled["high"] = sub["high"].resample("5T").max()
                    resampled["low"] = sub["low"].resample("5T").min()
                    resampled["close"] = sub["close"].resample("5T").last()
                    resampled["volume"] = sub["volume"].resample("5T").sum() if "volume" in sub.columns else 0

                    # Resample other features (take last value in 5m window)
                    feature_cols = [c for c in sub.columns if c not in ["open", "high", "low", "close", "volume", "symbol", "ts_ms"]]
                    for col in feature_cols:
                        if col in sub.columns:
                            resampled[col] = sub[col].resample("5T").last()

                    resampled["symbol"] = symbol
                    resampled = resampled.dropna(subset=["close"]).reset_index()
                    resampled.rename(columns={"index": "ts"}, inplace=True)
                    sub = resampled

        # For 1m resolution, return all bars from session start (09:30) to enable VWAP calculation
        # For 5m resolution or when limit is specified, apply limit
        if resolution == "1m" and not sub.empty and current_ts is not None:
            # Get all bars from today's session start (09:30)
            current_date = current_ts.date()
            session_start = pd.Timestamp(f"{current_date} 09:30:00", tz=current_ts.tz)
            sub = sub[sub["ts"] >= session_start].copy()
        elif limit and not sub.empty:
            sub = sub.tail(int(limit))

        if sub.empty:
            return []

        records = sub.to_dict(orient="records")
        if fields:
            filt = []
            keep = set(fields) | {"ts", "symbol"}
            for r in records:
                filt.append({k: v for k, v in r.items() if k in keep})
            return filt
        return records

    async def portfolio_view(self, strategy_id: str) -> Dict[str, Any]:
        snap = self._outer.sim.snapshot()
        return {"cash": snap.get("cash", 0.0), "equity": snap.get("equity", 0.0), "positions": []}

    async def position_view(self, strategy_id: str, symbol: str) -> Dict[str, Any]:
        # Simple backtest position view
        return {"strategy_id": strategy_id, "symbol": symbol, "pos_qty": 0.0, "avg_px": None, "realized_pnl_usd": 0.0, "unrealized_pnl_usd": 0.0}

    async def open_orders_view(self, strategy_id: str) -> List[Dict[str, Any]]:
        return []

    async def set_cadence(self, strategy_id: str, seconds: int) -> None:
        return None

class BacktestEngine:
    def __init__(self, cfg: BacktestConfig) -> None:
        self.cfg = cfg
        self.logger = structlog.get_logger("backtest_engine")

        # Create overlay if configuration provided
        overlay = None
        if HAS_OVERLAY and cfg.overlay_config:
            overlay_cfg = OverlayConfig(**cfg.overlay_config)
            overlay = PortfolioOverlay(overlay_cfg)

        from sim_adapter import SimAdapter
        self.sim = SimAdapter(
            initial_cash=cfg.initial_cash,
            commission_per_share=cfg.commission_per_share,
            commission_min=cfg.commission_min,
            slippage_bps=cfg.slippage_bps,
            fill_ref=cfg.fill_ref,
            overlay=overlay
        )
        self._pending_decisions: List[Tuple[str, Any]] = []
        self._pending_fills: List[Any] = []  # Queue fills for async strategy notification
        self._trades: List[TradeRecord] = []
        self._equity_curve: List[Tuple[int, float]] = []  # (ts_ms, equity)
        # Lightweight OCO state so that strategy-provided take-profit / stop-loss
        # (via extras.oco_cfg) influence backtest PnL similarly to live trading.
        self._pos_by_symbol: Dict[str, float] = {}
        self._oco_state: Dict[str, Dict[str, Any]] = {}
        self._oco_cfg_by_order: Dict[str, Dict[str, Any]] = {}

        self.sim.on_fill(self._on_fill)

        StrategyCls = _import_strategy_class(cfg.strategy_path)
        # Build StrategyContext for new-style strategies
        ctx = StrategyContext(
            engine=_EngineShim(self),
            strategy_id="STRAT_1",
            logger=get_logger("backtest.strategy"),
            metrics=SimpleNamespace(),
        )
        # Extract strategy extras; tolerate both {"extras": {...}} and direct extras dict.
        extras_dict: Dict[str, Any] = {}
        if isinstance(self.cfg.strategy_params, dict):
            raw = self.cfg.strategy_params
            if isinstance(raw.get("extras"), dict):
                extras_dict = raw["extras"]  # Preferred shape
            else:
                extras_dict = raw

        # Defensive: ensure symbols present in extras for multi-symbol strategies
        try:
            if extras_dict is None:
                extras_dict = {}
            if not extras_dict.get("symbols"):
                syms = list(self.cfg.symbols or [])
                if syms:
                    extras_dict["symbols"] = syms
        except Exception:
            pass

        sp = StrategyParams(extras=extras_dict or {})
        self.strategy = StrategyCls(ctx=ctx, params=sp)

    def _on_fill(self, fill) -> None:
        self._trades.append(
            TradeRecord(ts=fill.ts, symbol=fill.symbol, side=fill.side,
                        qty=fill.qty, price=fill.price, commission=fill.commission)
        )
        # Queue fill for async strategy notification
        self._pending_fills.append(fill)

        # Update lightweight position book and attach/reset OCO state
        try:
            sym = getattr(fill, "symbol", None)
            if not sym:
                return
            side = str(getattr(fill, "side", "")).upper()
            qty = float(getattr(fill, "qty", 0.0) or 0.0)
            if qty <= 0.0:
                return

            prev_qty = float(self._pos_by_symbol.get(sym, 0.0))
            delta = qty if side == "BUY" else -qty
            new_qty = prev_qty + delta
            self._pos_by_symbol[sym] = new_qty

            # New entry: attach OCO using the config of the originating order (if any)
            if prev_qty == 0.0 and new_qty != 0.0:
                order_id = getattr(fill, "order_id", None)
                oco_cfg = self._oco_cfg_by_order.get(order_id or "", {})
                if oco_cfg:
                    entry_px = float(getattr(fill, "price", 0.0) or 0.0)
                    if entry_px > 0.0:
                        direction = 1.0 if new_qty > 0 else -1.0
                        tp_bp = float(oco_cfg.get("take_profit_bp", 0.0) or 0.0)
                        sl_bp = float(oco_cfg.get("hard_stop_bp", 0.0) or 0.0)
                        tp_px = entry_px * (1.0 + direction * tp_bp / 10_000.0) if tp_bp > 0 else 0.0
                        sl_px = entry_px * (1.0 - direction * sl_bp / 10_000.0) if sl_bp > 0 else 0.0
                        self._oco_state[sym] = {
                            "direction": direction,
                            "entry_px": entry_px,
                            "tp_px": tp_px,
                            "sl_px": sl_px,
                        }
            # Position fully closed: clear any OCO state
            elif new_qty == 0.0 and sym in self._oco_state:
                self._oco_state.pop(sym, None)
        except Exception:
            # OCO bookkeeping must never break the backtest loop
            pass

    def _features_table_name(self) -> str:
        if self.cfg.features_table:
            return self.cfg.features_table
        tf = self.cfg.timeframe
        return f"trading.features_{tf}"

    def _clickhouse_query(self) -> Optional[pd.DataFrame]:
        kind, client_or_mod = _get_clickhouse()
        if kind is None:
            return None
        start = self.cfg.start.replace("T"," ")
        end = self.cfg.end.replace("T"," ")
        tbl = self._features_table_name()
        syms = ",".join([f"'{s}'" for s in self.cfg.symbols])

        # Use bars_1m_ibkr FINAL if available, otherwise fallback to features table
        # Filter to trading hours only (is_rth=1 or 09:30-16:00 ET)
        if self.cfg.bars_table:
            sql = f"""
                SELECT *
                FROM {self.cfg.bars_table} FINAL
                WHERE symbol IN ({syms})
                  AND ts_start >= toDateTime('{start}') AND ts_start < toDateTime('{end}')
                  AND is_rth = 1
                ORDER BY ts_start, symbol
            """
        else:
            # Inline JOIN with OOF tables to avoid ClickHouse view alias issues
            # Enriched feature set for improved model performance
            sql = f"""
                SELECT
                  f.symbol AS symbol,
                  f.ts AS ts,
                  f.open, f.high, f.low, f.close,
                  f.volume,
                  -- Multi-horizon momentum (ret_15 added if exists in table)
                  f.ret_1, f.ret_5,
                  -- Volatility and trend
                  f.atr_14, f.rsi_14, f.slope_20, f.vwap,
                  -- Microstructure
                  f.spread_bps,
                  -- Z-scores (z_vol_20 will be computed post-load if missing)
                  f.z_price_20,
                  -- Moving averages
                  f.ema_10, f.ema_20, f.sma_20,
                  -- Bollinger bands
                  f.bb_mid_20, f.bb_up_20, f.bb_dn_20,
                  -- Momentum indicators
                  f.stoch_k_14, f.adx_14,
                  -- Volume
                  f.vol_ema_20, f.vol_spike,
                  -- OOF predictions
                  l.pred AS lstm_oof_h5,
                  l.conf AS lstm_oof_conf_h5,
                  x.pred AS xgb_oof_h5,
                  x.conf AS xgb_oof_conf_h5
                FROM {tbl} AS f
                LEFT JOIN (
                  SELECT ts, symbol, pred, conf
                  FROM (
                    SELECT ts, symbol, pred, conf,
                      row_number() OVER (PARTITION BY symbol, ts ORDER BY updated_at DESC) AS rn
                    FROM trading.oof_lstm_h5
                  )
                  WHERE rn = 1
                ) AS l ON l.symbol = f.symbol AND l.ts = f.ts
                LEFT JOIN (
                  SELECT ts, symbol, pred, conf
                  FROM (
                    SELECT ts, symbol, pred, conf,
                      row_number() OVER (PARTITION BY symbol, ts ORDER BY updated_at DESC) AS rn
                    FROM trading.oof_xgb_h5
                  )
                  WHERE rn = 1
                ) AS x ON x.symbol = f.symbol AND x.ts = f.ts
                WHERE f.symbol IN ({syms})
                  AND f.ts >= toDateTime('{start}') AND f.ts < toDateTime('{end}')
                  AND toHour(f.ts) >= 9 AND toHour(f.ts) < 16
                ORDER BY f.ts, f.symbol
            """
        if kind == "connect":
            conn = client_or_mod.get_client(**self.cfg.clickhouse)
            df = conn.query_df(sql)
            return df
        else:
            client = client_or_mod(**self.cfg.clickhouse)
            data = client.execute(sql)
            cols = [c[0] for c in client.execute("DESCRIBE TABLE " + tbl)]
            import pandas as pd
            return pd.DataFrame(data, columns=cols)

    def load_data(self, df: Optional[pd.DataFrame] = None) -> pd.DataFrame:
        if df is not None:
            out = df.copy()
        else:
            out = self._clickhouse_query()
            if out is None:
                raise RuntimeError("ClickHouse client not available and no DataFrame provided.")
        if out.empty:
            raise ValueError("No data returned from features query for selected symbols/date range")

        # Handle different timestamp column names
        if "ts_start" in out.columns and "ts" not in out.columns:
            out["ts"] = out["ts_start"]
        elif "ts_bar_end" in out.columns and "ts" not in out.columns:
            out["ts"] = out["ts_bar_end"]

        needed = {"ts","symbol","open","high","low","close"}
        missing = needed - set(out.columns)
        if missing:
            raise ValueError(f"Missing required columns: {missing}")

        # Fill NULL OHLC values (features_1m has Nullable columns)
        # Use forward-fill then back-fill to handle NULLs at start/end
        for col in ["open", "high", "low", "close"]:
            if out[col].isnull().any():
                out[col] = out.groupby("symbol")[col].ffill().bfill()

        # Drop rows with remaining NULLs (no valid data)
        out = out.dropna(subset=["open", "high", "low", "close"])

        if pd.api.types.is_datetime64_any_dtype(out["ts"]) is False:
            out["ts"] = pd.to_datetime(out["ts"], utc=True)
        out["ts_ms"] = (out["ts"].astype("int64") // 10**6).astype("int64")
        out = out.sort_values(["ts","symbol"]).reset_index(drop=True)

        # Apply point-in-time data leakage guard
        # If the DataFrame has a "feature_cutoff_ts" or similar column,
        # enforce_point_in_time() will filter out rows where ts < cutoff
        out = enforce_point_in_time(out, ts_col="ts", cutoff_col="feature_cutoff_ts")

        # Convenience: map common OOF columns to model-specific keys for ensemble experiments
        try:
            if "lstm_oof_h5" in out.columns and "lstm_signal" not in out.columns:
                # Map probability [0,1] to signal [-1,1], but only if conf > 0
                oof_vals = out["lstm_oof_h5"].astype(float)
                oof_conf = out.get("lstm_oof_conf_h5", pd.Series(0.0, index=out.index)).astype(float)
                out["lstm_signal"] = np.where(
                    oof_conf > 0,  # Only if conf > 0
                    (oof_vals * 2.0) - 1.0,  # Valid data: [0,1] → [-1,1]
                    0.0  # Invalid data: neutral
                )
            if "lstm_oof_conf_h5" in out.columns and "lstm_conf" not in out.columns:
                out["lstm_conf"] = out["lstm_oof_conf_h5"].astype(float)

            # Map XGBoost OOF predictions to signals
            if "xgb_oof_h5" in out.columns and "xgb_signal" not in out.columns:
                # Map probability [0,1] to signal [-1,1], but only if conf > 0
                oof_vals = out["xgb_oof_h5"].astype(float)
                oof_conf = out.get("xgb_oof_conf_h5", pd.Series(0.0, index=out.index)).astype(float)
                out["xgb_signal"] = np.where(
                    oof_conf > 0,  # Only if conf > 0
                    (oof_vals * 2.0) - 1.0,  # Valid data: [0,1] → [-1,1]
                    0.0  # Invalid data: neutral
                )
            if "xgb_oof_conf_h5" in out.columns and "xgb_conf" not in out.columns:
                out["xgb_conf"] = out["xgb_oof_conf_h5"].astype(float)

            # MOCK ML SIGNALS: Generate technical-based signals when ML data is missing
            # This allows testing the ensemble/RL infrastructure without trained models
            # Only enabled when ML_MOCK=1 environment variable is set
            needs_mock = ("ml_signal" not in out.columns or
                         (out["ml_signal"].fillna(0.0).abs() < 1e-6).all())
            if needs_mock and os.getenv("ML_MOCK") == "1":
                # Create mock XGB signals based on momentum + trend
                ret_5 = out.get("ret_5", out.get("ret_5m", pd.Series(0.0, index=out.index))).fillna(0.0)
                slope_20 = out.get("slope_20", pd.Series(0.0, index=out.index)).fillna(0.0)
                rsi_14 = out.get("rsi_14", pd.Series(50.0, index=out.index)).fillna(50.0)

                # Momentum score: normalize ret_5 to [-1, 1] range
                momentum_score = np.tanh(ret_5 * 100)  # Scale to make it sensitive

                # Trend score: normalize slope to [-1, 1]
                trend_score = np.tanh(slope_20 * 10)

                # RSI divergence score: overbought/oversold
                rsi_score = np.where(rsi_14 > 70, -0.5, np.where(rsi_14 < 30, 0.5, 0.0))

                # Combined signal: weighted average
                mock_signal = (0.4 * momentum_score + 0.4 * trend_score + 0.2 * rsi_score)

                # Confidence: based on signal strength and consistency
                # Boosted to 0.75 baseline to pass typical conf_floor thresholds (0.6-0.7)
                mock_conf = np.clip(abs(mock_signal) * 0.5 + 0.75, 0.6, 0.95)

                out["ml_signal"] = mock_signal
                out["ml_conf"] = mock_conf
                out["xgb_signal"] = mock_signal  # Alias for XGB
                out["xgb_conf"] = mock_conf
                # Also expose as LSTM signals for LSTM-only/fusion mode testing
                out["lstm_signal"] = mock_signal
                out["lstm_conf"] = mock_conf

                self.logger.info(
                    "Generated mock ML signals",
                    avg_signal=float(mock_signal.mean()),
                    avg_conf=float(mock_conf.mean()),
                    rows=len(out)
                )
        except Exception as e:
            self.logger.error("Error in OOF/mock signal mapping", error=str(e))
            pass
        return out

    @staticmethod
    def _max_drawdown(values: List[float]) -> float:
        peak = -math.inf
        mdd = 0.0
        for v in values:
            if v > peak:
                peak = v
            dd = (peak - v) / peak if peak != 0 else 0
            mdd = max(mdd, dd)
        return mdd

    @staticmethod
    def _sharpe(returns: List[float], risk_free: float = 0.0) -> float:
        if not returns:
            return 0.0
        avg = statistics.mean(returns)
        std = statistics.pstdev(returns) or 1e-9
        return (avg - risk_free) / std

    def _apply_oco_exits(self, ts_ms: int, symbol: str, bar: Dict[str, float]) -> None:
        """
        Best-effort OCO emulation for backtests.

        Uses strategy-provided extras.oco_cfg (take_profit_bp / hard_stop_bp) to
        close positions when bar high/low breaches the configured levels.
        """
        state = self._oco_state.get(symbol)
        if not state:
            return

        direction = float(state.get("direction", 0.0))
        if direction == 0.0:
            return

        qty = float(self._pos_by_symbol.get(symbol, 0.0))
        if qty == 0.0:
            # Position already flat; clear stale state
            self._oco_state.pop(symbol, None)
            return

        high = float(bar.get("high", bar.get("close", 0.0)) or 0.0)
        low = float(bar.get("low", bar.get("close", 0.0)) or 0.0)
        tp_px = float(state.get("tp_px", 0.0) or 0.0)
        sl_px = float(state.get("sl_px", 0.0) or 0.0)

        triggered_side: Optional[str] = None
        if direction > 0:
            # Long: stop if price trades <= sl_px; take-profit if trades >= tp_px
            if sl_px > 0.0 and low <= sl_px:
                triggered_side = "SELL"
            elif tp_px > 0.0 and high >= tp_px:
                triggered_side = "SELL"
        else:
            # Short: stop if price trades >= sl_px; take-profit if trades <= tp_px
            if sl_px > 0.0 and high >= sl_px:
                triggered_side = "BUY"
            elif tp_px > 0.0 and low <= tp_px:
                triggered_side = "BUY"

        if not triggered_side:
            return

        try:
            order = self.sim.build_order(
                decision_id=f"OCO_EXIT_{symbol}_{ts_ms}",
                symbol=symbol,
                side=triggered_side,
                qty=abs(qty),
                type_="MARKET",
                limit_price=None,
                stop_price=None,
                tag="oco_exit",
                ts_ms=ts_ms,
            )
            self.sim.submit(order)
            # Clear local OCO state; position/ledger will update on fill
            self._oco_state.pop(symbol, None)
        except Exception:
            # OCO exits are advisory; never break the engine
            pass

    def _compute_metrics(self) -> Metrics:
        eq = [e for _, e in self._equity_curve]
        if not eq:
            return Metrics(0,0,0,0,0,0)
        rets = [(eq[i]-eq[i-1]) / max(eq[i-1],1e-9) for i in range(1, len(eq))]
        total_ret = (eq[-1] - eq[0]) / max(eq[0], 1e-9)
        sharpe = self._sharpe(rets)
        mdd = self._max_drawdown(eq)

        # Accurate trade stats from simulator ledger
        ledger = getattr(self.sim, "get_trade_ledger", lambda: [])()
        wins = sum(1 for t in ledger if t.get("pnl", 0.0) > 0)
        losses = sum(1 for t in ledger if t.get("pnl", 0.0) <= 0)
        gross_win = sum(max(t.get("pnl", 0.0), 0.0) for t in ledger)
        gross_loss = sum(max(-t.get("pnl", 0.0), 0.0) for t in ledger)

        win_rate = wins / max(wins+losses, 1) if (wins+losses) > 0 else 0.0
        profit_factor = (gross_win / max(gross_loss, 1e-9)) if gross_loss > 0 else float('inf')
        trades_n = len(ledger)

        return Metrics(
            total_return=float(total_ret),
            sharpe=float(sharpe),
            max_drawdown=float(mdd),
            win_rate=float(win_rate),
            profit_factor=float(profit_factor),
            trades=int(trades_n)
        )

    async def _run_async(self, df: pd.DataFrame) -> Metrics:
        try:
            if hasattr(self.strategy, "initialize") and asyncio.iscoroutinefunction(self.strategy.initialize):
                self.logger.info("Initializing strategy")
                await self.strategy.initialize()
                self.logger.info("Strategy initialized successfully")
        except Exception as e:
            self.logger.error("Strategy initialization failed", error=str(e), exc_info=True)
            raise

        # Iterate rows; append equity AFTER all symbols of same ts processed
        n = len(df)
        self.logger.info("Starting backtest loop", total_rows=n)

        last_log_time = 0
        for i in range(n):
            try:
                # Log progress every 1000 rows
                if i % 1000 == 0 and i > 0:
                    self.logger.info("Backtest progress", row=i, total=n, pct=f"{100*i/n:.1f}%")

                row = df.iloc[i]
                ts_ms = int(row["ts_ms"])
                symbol = row["symbol"]

                # Track current timestamp for point-in-time history access
                self._current_ts = row["ts"]

                bar = {
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                }

                span_cm = _TRACER.start_as_current_span(
                    "bar",
                    attributes={
                        "symbol": str(symbol),
                        "ts_ms": ts_ms,
                    },
                ) if _TRACER else nullcontext()

                with span_cm:
                    try:
                        # 1) Update simulator with bar
                        self.sim.on_bar(ts_ms, symbol, bar)
                    except Exception as e:
                        self.logger.error("Error in sim.on_bar", row=i, ts_ms=ts_ms, symbol=symbol, error=str(e), exc_info=True)
                        raise

                    try:
                        # 1.5) Notify strategy of any fills from sim.on_bar()
                        while self._pending_fills:
                            fill = self._pending_fills.pop(0)
                            if hasattr(self.strategy, "on_fill"):
                                await self.strategy.on_fill(fill.symbol, int(fill.qty), fill.price, fill.side)
                    except Exception as e:
                        self.logger.error("Error in on_fill", row=i, ts_ms=ts_ms, symbol=symbol, error=str(e), exc_info=True)
                        raise

                    try:
                        # 2) Strategy callback with full features
                        features = row.to_dict()
                        if hasattr(self.strategy, "on_features"):
                            await self.strategy.on_features(symbol, features)
                    except Exception as e:
                        self.logger.error("Error in on_features", row=i, ts_ms=ts_ms, symbol=symbol, close=bar.get("close"), error=str(e), exc_info=True)
                        raise

                    try:
                        # 3) Drain decisions -> orders
                        while self._pending_decisions:
                            strat_id, decision = self._pending_decisions.pop(0)
                            raw_qty = float(getattr(decision, "target_qty", 0.0) or 0.0)
                            symbol_d = str(getattr(decision, "symbol", symbol))
                            decision_id = getattr(decision, "decision_id", f"DEC_{ts_ms}")

                            # Map quantity sign to side for simulator
                            side = "BUY" if raw_qty >= 0 else "SELL"
                            qty = abs(raw_qty)

                            # Map order style (best-effort)
                            typ = "MARKET"
                            limit_px = None
                            stop_px = None
                            try:
                                os_obj = getattr(decision, "order_style", None)
                                if os_obj is not None and hasattr(os_obj, "kind"):
                                    kind = str(getattr(os_obj, "kind", "MARKET")).upper()
                                    if kind in ("MARKET", "MKT"):
                                        typ = "MARKET"
                                    elif kind in ("LIMIT", "LMT"):
                                        typ = "LIMIT"
                                        limit_px = getattr(os_obj, "limit_px", None)
                                    elif kind in ("STOP", "STP"):
                                        typ = "STOP"
                                        stop_px = getattr(os_obj, "stop_px", None)
                            except Exception:
                                pass

                            # Capture per-order OCO configuration (if provided by strategy)
                            extras = None
                            try:
                                extras = getattr(decision, "extras", {}) or {}
                                oco_cfg = extras.get("oco_cfg")
                                if isinstance(oco_cfg, dict):
                                    # Store mapping so _on_fill can attach OCO state on entry fills
                                    # (order_id will be known after build_order)
                                    pass
                            except Exception:
                                extras = {}
                                oco_cfg = None

                            order = self.sim.build_order(
                                decision_id=decision_id,
                                symbol=symbol_d,
                                side=side,
                                qty=qty,
                                type_=typ,
                                limit_price=limit_px,
                                stop_price=stop_px,
                                ts_ms=ts_ms,
                            )
                            # Now that order_id is known, persist OCO config (if any)
                            try:
                                if "oco_cfg" in (extras or {}) and isinstance(oco_cfg, dict):
                                    self._oco_cfg_by_order[order.order_id] = oco_cfg
                            except Exception:
                                pass
                            self.sim.submit(order)
                    except Exception as e:
                        self.logger.error("Error processing decisions", row=i, ts_ms=ts_ms, symbol=symbol, error=str(e), exc_info=True)
                        raise

                    # 3.5) Apply OCO exits after processing new decisions
                    try:
                        self._apply_oco_exits(ts_ms, symbol, bar)
                    except Exception as e:
                        self.logger.error("Error in oco_exit", row=i, ts_ms=ts_ms, symbol=symbol, error=str(e), exc_info=True)
                        raise

                    # 🔥 Explicit memory cleanup
                    del features

                # 4) Append equity only when next ts changes (or last row)
                next_ts = int(df.iloc[i+1]["ts_ms"]) if i+1 < n else None
                if next_ts is None or next_ts != ts_ms:
                    snap = self.sim.snapshot()
                    self._equity_curve.append((ts_ms, float(snap["equity"])))

                # 🔥 Periodic GC to prevent memory buildup (every 500 bars)
                if i % 500 == 0 and i > 0:
                    gc.collect()

            except Exception as e:
                self.logger.error("Fatal error in backtest loop", row=i, total_rows=n, error=str(e), exc_info=True)
                raise

        self.logger.info("Backtest loop completed successfully", total_rows=n)

        # FIX Bug #10: Force close all positions at end of backtest to create round-trip trades
        # This ensures trades count > 0 even if strategy only goes one direction
        snapshot = self.sim.snapshot()
        open_positions = snapshot.get("positions", {})
        if open_positions:
            # Use last bar's close price for final liquidation
            last_row = df.iloc[-1]
            last_price = float(last_row.get("close", last_row.get("open", 0.0)))
            last_ts_ms = int(last_row.get("ts_ms", 0))

            for symbol, pos_data in open_positions.items():
                # pos_data is a dict from asdict(Position), extract qty field
                if isinstance(pos_data, dict):
                    pos_qty = pos_data.get("qty", 0)
                else:
                    pos_qty = pos_data  # fallback if it's already a number

                if pos_qty != 0:
                    # Close position by submitting opposite order
                    close_side = "BUY" if pos_qty < 0 else "SELL"
                    close_order = self.sim.build_order(
                        decision_id=f"force_close_{symbol}_{last_ts_ms}",
                        symbol=symbol,
                        side=close_side,
                        qty=abs(pos_qty),
                        type_="MARKET",
                        limit_price=None,
                        stop_price=None,
                        ts_ms=last_ts_ms,
                    )
                    self.sim.submit(close_order)
                    self.logger.info(
                        "backtest_end_force_close",
                        symbol=symbol,
                        position=pos_qty,
                        close_side=close_side,
                        close_qty=abs(pos_qty),
                        price=last_price,
                    )

        # Don't call shutdown() here - it can cause issues if strategy.shutdown() is not async
        # Cleanup will be done in run()'s finally block via close()
        try:
            metrics = self._compute_metrics()
            self.logger.info("Metrics computed successfully", sharpe=metrics.sharpe, trades=metrics.trades)
            return metrics
        except Exception as e:
            self.logger.error("Error computing metrics", error=str(e), exc_info=True)
            raise

    async def shutdown(self) -> None:
        """
        DEPRECATED: This method is no longer used to avoid event loop issues.
        Cleanup is done synchronously in close() instead.
        """
        pass

    def close(self) -> None:
        """
        Synchronous cleanup - release resources.
        Call this after backtest completes to ensure memory is freed.
        """
        # Clear large collections
        self._trades.clear()
        self._equity_curve.clear()
        self._pending_fills.clear()

        # Clear data reference
        if hasattr(self, '_data'):
            del self._data

        # Clear simulator state
        if hasattr(self, 'sim'):
            # If sim has trade ledger, clear it
            if hasattr(self.sim, 'get_trade_ledger'):
                try:
                    ledger = self.sim.get_trade_ledger()
                    if hasattr(ledger, 'clear'):
                        ledger.clear()
                except Exception:
                    pass

        # Break circular references explicitly (from shutdown logic)
        if hasattr(self, 'strategy') and self.strategy:
            if hasattr(self.strategy, 'ctx'):
                self.strategy.ctx.engine = None
            self.strategy = None

        # Final GC pass
        gc.collect()

    def run(self, df: Optional[pd.DataFrame] = None) -> Dict[str, Any]:
        try:
            data = self.load_data(df=df)
            # Expose dataset for strategy history() requests
            self._data = data
            metrics = asyncio.run(self._run_async(data))

            result = {
                "metrics": asdict(metrics),
                "trades": [asdict(t) for t in self._trades],
                "equity_curve": self._equity_curve.copy(),  # Return copy, not reference
                "final_snapshot": self.sim.snapshot(),
                "trade_ledger_size": len(getattr(self.sim, "get_trade_ledger", lambda: [])())
            }

            return result
        finally:
            # Always cleanup after run completes (success or failure)
            # Don't call asyncio.run() in finally - it can cause event loop conflicts
            # Just do synchronous cleanup
            self.close()

if __name__ == "__main__":
    import argparse, json
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+", required=True)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--timeframe", default="1m")
    ap.add_argument("--features_table", default=None)
    ap.add_argument("--bars_table", default=None)
    ap.add_argument("--strategy_path", default="tsla_v7_2.TSLASimplifiedV7_2")
    ap.add_argument("--strategy_params", default=None, help="JSON string for extras")
    ap.add_argument("--initial_cash", type=float, default=100000)
    ap.add_argument("--commission_per_share", type=float, default=0.005)
    ap.add_argument("--commission_min", type=float, default=1.0)
    ap.add_argument("--slippage_bps", type=float, default=1.0)
    ap.add_argument("--fill_ref", default="open")
    args = ap.parse_args()

    extras = json.loads(args.strategy_params) if args.strategy_params else {}
    cfg = BacktestConfig(
        symbols=args.symbols, start=args.start, end=args.end,
        timeframe=args.timeframe, features_table=args.features_table,
        bars_table=args.bars_table,
        initial_cash=args.initial_cash,
        commission_per_share=args.commission_per_share,
        commission_min=args.commission_min,
        slippage_bps=args.slippage_bps, fill_ref=args.fill_ref,
        strategy_path=args.strategy_path, strategy_params=extras,
    )
    eng = BacktestEngine(cfg)
    out = eng.run()
    print(json.dumps(out["metrics"], indent=2, ensure_ascii=False))
