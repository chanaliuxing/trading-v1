# -*- coding: utf-8 -*-
"""
TSLA_V7_2 — 日内做多（简化稳健版，可生产）
遵循《TSLA（日内做多）策略 V7.2·简化稳健版》文档：
- 盘前“参数大脑”：7/60/180日 + 目标触发率 + LKG 回退
- 运行期三模块：A1(VWAP回归,1m) / A2(ATR网格,5m,锚=SMA30) / B1(ORB回踩,5m)
- 风控：市场乘数(SPY/QQQ) + 软/硬回撤 + 极端行情暂停(VIX/点差/ATR spike) + KillSwitch
- 成本后硬门槛：L1 half-spread P95（同时间槽优先，fallback 全日）
- 反饥饿：限权+探测单（120/180分钟）
- 执行：可成交限价（marketable limit），EOD 15:57 强平
"""

import os
import json
import math
import datetime as dt
from typing import Dict, Any, Optional, List, Tuple

import numpy as np
import pandas as pd

from libs.strategy import (
    StrategyAlgorithm,
    DecisionEnvelope,
    OrderStyle,
    Freshness,
    make_decision_id,
)
from libs.logger import get_logger

logger = get_logger(__name__)

# ---------------------- 通用小工具 ----------------------

def _ny_now():
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        return dt.datetime.utcnow() - dt.timedelta(hours=4)

def _atomic_write(path: str, data: dict):
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def _safe_read(path: str) -> dict:
    if not os.path.exists(path): return {}
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def _ema(s: pd.Series, span: int) -> pd.Series:
    return s.ewm(span=span, adjust=False).mean()

def _atr_wilder(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df['high'], df['low'], df['close']
    tr = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean()

def _bbands(df: pd.DataFrame, n: int, k: float) -> pd.DataFrame:
    ma = df['close'].rolling(n).mean()
    sd = df['close'].rolling(n).std(ddof=0)
    return pd.DataFrame({"bb_mid": ma, "bb_up": ma + k*sd, "bb_lo": ma - k*sd}, index=df.index)

def _rsi(series: pd.Series, n: int = 14) -> pd.Series:
    delta = series.diff()
    up = (delta.where(delta > 0, 0.0)).ewm(alpha=1/n, adjust=False).mean()
    down = (-delta.where(delta < 0, 0.0)).ewm(alpha=1/n, adjust=False).mean()
    rs = up / (down + 1e-12)
    return 100.0 - (100.0 / (1.0 + rs))

def _percentile(x: pd.Series, p: float, default=np.nan) -> float:
    x = x.dropna()
    if len(x) == 0: return float(default)
    return float(np.nanpercentile(x.values, p))

def _clean_rth(df: pd.DataFrame) -> pd.DataFrame:
    df = df[['open','high','low','close','volume']].dropna()
    df = df[~(df['close'].pct_change().abs() > 0.25)]
    return df.between_time("09:30","16:00")

def _vwap_session(df: pd.DataFrame) -> pd.Series:
    q = df['volume'].clip(lower=0.0)
    pq = df['close'] * q
    return (pq.cumsum() / q.cumsum()).replace([np.inf, -np.inf], np.nan).ffill()

def _slot_index(ts: pd.Timestamp) -> int:
    # 09:30=slot0, 每5分钟一格; 用于RVOL与点差分布时段
    return int(((ts.hour - 9) * 60 + ts.minute - 30) // 5)

def _intraday_rvol_5m(df_5m: pd.DataFrame, lookback_days: int = 5) -> pd.Series:
    """
    优化版本RVOL计算：向量化 + 滚动窗口缓存
    性能提升：O(n²) -> 接近O(n)

    策略：
    1. 预计算 rolling(2) volume
    2. 使用固定窗口（78*lookback_days）提取历史
    3. groupby slot 计算中位数基准（向量化）
    4. 避免 iterrows() - 使用 numpy数组操作
    """
    if df_5m.empty:
        return pd.Series([], dtype=float)

    d = df_5m.copy()
    d['slot'] = d.index.map(_slot_index)
    d['vol2'] = d['volume'].rolling(2, min_periods=1).sum()

    min_samples = 78 * lookback_days
    n = len(d)
    result = np.full(n, np.nan, dtype=float)

    if n < min_samples:
        # 数据不足，全部返回NaN
        return pd.Series(result, index=d.index)

    # 核心优化：只计算一次历史基准，因为在运行时只需要最新的一个值
    # 对于回测/批处理场景，使用滚动计算

    # 方法：对每个位置，取过去min_samples的数据，按slot分组计算中位数
    slots = d['slot'].values
    vol2_vals = d['vol2'].values

    # 只计算有足够历史的部分
    for i in range(min_samples, n):
        current_slot = slots[i]
        current_vol = vol2_vals[i]

        # 取历史窗口
        hist_slots = slots[i-min_samples:i]
        hist_vols = vol2_vals[i-min_samples:i]

        # 找到同slot的历史volume（向量化）
        mask = (hist_slots == current_slot)
        same_slot_vols = hist_vols[mask]

        if len(same_slot_vols) > 0:
            median_vol = np.median(same_slot_vols)
            if median_vol > 0:
                result[i] = current_vol / median_vol
            else:
                result[i] = 1.0
        else:
            result[i] = 1.0

    return pd.Series(result, index=d.index)

def _ts_us(features: Dict[str, Any]) -> int:
    ts = features.get("ts")
    if isinstance(ts, pd.Timestamp):
        return int(ts.value // 1000)
    if isinstance(ts, dt.datetime):
        return int(ts.timestamp() * 1_000_000)
    if ts is None:
        return 0
    try:
        return int(ts)
    except Exception:
        return 0

# ---------------------- 状态守护（TSLA 专属） ----------------------

class _TslaGuard:
    """
    日切、回撤熔断、反饥饿、成交记账、EOD 强平辅助
    """
    def __init__(self, strategy_id: str, account_equity: float, log_handler, state_store: Dict[str, Any]):
        self.state_key = f"tsla_v7_2_guard::{strategy_id}"
        self.account_equity = float(account_equity)
        self.logger = log_handler
        self.store = state_store
        self.state = self._load_and_init()

    def _load_and_init(self) -> dict:
        st = self.store.get(self.state_key, {}) if isinstance(self.store, dict) else {}
        st.setdefault("position", {"qty": 0, "avg_px": 0.0, "entry_ts": None})
        st.setdefault("pnl_today", 0.0)
        st.setdefault("peak_equity_today", self.account_equity)
        st.setdefault("last_trade_ts", None)
        st.setdefault("starvation_level", 0)
        st.setdefault("last_update_date", _ny_now().strftime("%Y-%m-%d"))
        st.setdefault("a2_layers", 0)
        st.setdefault("last_sig_ts", None)
        if isinstance(self.store, dict):
            self.store[self.state_key] = st
        return st

    def _persist(self) -> None:
        if isinstance(self.store, dict):
            self.store[self.state_key] = self.state

    def daily_reset_check(self) -> bool:
        now_date = _ny_now().strftime("%Y-%m-%d")
        if self.state.get("last_update_date") != now_date:
            self.logger.warning("Cross-day detected. Resetting state.", state_key=self.state_key)
            self.reset_state(force=True)
            return True
        return False

    def reset_state(self, force: bool = False):
        is_trading = (_ny_now().weekday() < 5 and dt.time(9,30) <= _ny_now().time() <= dt.time(16,10))
        if is_trading and not force:
            raise RuntimeError("Cannot reset during trading hours.")
        self.state = self._load_and_init()

    def record_fill(self, qty:int, price:float, side:str):
        pos = self.state.get("position", {"qty":0,"avg_px":0.0,"entry_ts":None})
        q0, p0 = int(pos.get("qty",0)), float(pos.get("avg_px",0.0))
        realized = 0.0
        if side.upper()=="BUY":
            q1 = q0 + qty
            p1 = ((q0*p0) + (qty*price))/q1 if q1>0 else 0.0
            pos = {"qty": q1, "avg_px": p1, "entry_ts": dt.datetime.now().timestamp() if q0==0 else pos.get("entry_ts")}
        else:
            q1 = q0 - qty
            if q0>0: realized = (price - p0) * qty
            pos = {"qty": q1, "avg_px": p0 if q1>0 else 0.0, "entry_ts": None if q1==0 else pos.get("entry_ts")}
        self.state["position"] = pos
        self.state["pnl_today"] = float(self.state.get("pnl_today", 0.0) + realized)
        cur_eq = self.account_equity + self.state["pnl_today"]
        self.state["peak_equity_today"] = max(self.state.get("peak_equity_today", self.account_equity), cur_eq)

        # === NEW: Track recent net PnL for dynamic cost gate ===
        if realized != 0.0:
            lst = list(self.state.get("recent_net_pnl_list", []))
            lst.append(float(realized))
            self.state["recent_net_pnl_list"] = lst[-20:]  # Keep last 20 trades

        # === NEW: Update timestamp for anti-starvation ===
        self.state["last_trade_ts"] = dt.datetime.now().timestamp()
        self.state["starvation_level"] = 0

        # === NEW: Reset A2 layers on position close ===
        if pos["qty"] == 0:
            self.state["a2_layers"] = 0

        self.state["last_update_date"] = _ny_now().strftime("%Y-%m-%d")
        self._persist()

    def risk_multiplier(self, dd_cfg: dict) -> Tuple[float, str]:
        pnl = float(self.state.get("pnl_today", 0.0))
        peak = float(self.state.get("peak_equity_today", self.account_equity))
        cur  = self.account_equity + pnl
        dd = (peak - cur) / peak if peak > 0 else 0.0
        soft = float(dd_cfg.get("soft_pct", 0.018))
        hard = float(dd_cfg.get("hard_pct", 0.028))
        if dd >= hard: return 0.0, f"HARD_DD({dd:.2%})"
        if dd >= soft: return 0.5, f"SOFT_DD({dd:.2%})"
        return 1.0, "NORMAL"

    def time_holding_minutes(self) -> float:
        pos = self.state.get("position", {})
        if pos.get("qty",0) != 0 and pos.get("entry_ts"):
            try:
                from zoneinfo import ZoneInfo
                entry_dt = dt.datetime.fromtimestamp(pos["entry_ts"], tz=ZoneInfo("America/New_York"))
            except Exception:
                # Fallback for older Python or missing zoneinfo
                entry_dt = dt.datetime.fromtimestamp(pos["entry_ts"]) - dt.timedelta(hours=4)
                entry_dt = entry_dt.replace(tzinfo=dt.timezone(dt.timedelta(hours=-4)))
            return (_ny_now() - entry_dt).total_seconds()/60.0
        return 0.0

    def starvation_update(self, no_trade_min: int, max_level: int = 2) -> int:
        last_ts = self.state.get("last_trade_ts")
        if last_ts:
            mins = (dt.datetime.now().timestamp() - last_ts)/60
            if mins > no_trade_min:
                lvl = min(self.state.get("starvation_level",0)+1, max_level)
                if lvl != self.state.get("starvation_level"):
                    self.state["starvation_level"] = lvl
                    self._persist()
                    self.logger.info(f"Anti-starvation level ↑ {lvl}")
        return int(self.state.get("starvation_level",0))

# ---------------------- 策略主体 ----------------------

class TSLASimplifiedV7_2(StrategyAlgorithm):
    """
    TSLA（日内做多）V7.2 简化稳健版
    模块优先级：B1 > A1 > A2（同刻只触发一个，另一个延后5分钟再评估）
    """
    async def initialize(self):
        self.cfg: Dict[str,Any] = (
            self.params.extras.get("tsla_v7_2_config") or
            self.params.extras.get("tsla_config") or
            {}
        )
        self.symbol = self.cfg.get("symbol", "TSLA")

        # 状态
        self.guard = _TslaGuard(self.ctx.strategy_id, self.params.capital_limit_usd, self.logger, self.state)
        self.guard.daily_reset_check()

        # Get initial state values from config
        init_state = self.cfg.get("initial_state", {})
        orb_len = self.cfg.get("B1", {}).get("orb_len", 30)

        self.state.update({
            "dyn": {},                       # 动态参数盘前产出
            "market_regime": init_state.get("market_regime", "NEUTRAL"),
            "market_mult": init_state.get("market_mult", 0.75),
            "latest_vix": init_state.get("latest_vix", 18.0),
            "prev_vix": init_state.get("prev_vix", 18.0),
            "pause_until": None,
            "_last_1m": None,
            "_last_5m": None,
            "_last_dyn_minute": None,
            "_last_regime_minute": None,
            "_cooldown_until": None,        # 同刻防多触发
            "_orb_state": {"len_min":orb_len, "hi":None, "lo":None, "broken":False, "break_ts":None},
        })

        # 订阅：TSLA(1m/5m)、SPY/QQQ(5m)、VIX(1m)
        subs_5m = list(set(self.cfg.get("market_regime_symbols", ["SPY","QQQ"])))
        vix_symbol = self.cfg.get("regime", {}).get("vix_symbol", "VIX")
        await self.ctx.subscribe_features(self.ctx.strategy_id, [self.symbol], bar_sec=60)
        await self.ctx.subscribe_features(self.ctx.strategy_id, [self.symbol], bar_sec=300)
        await self.ctx.subscribe_features(self.ctx.strategy_id, subs_5m, bar_sec=300)
        await self.ctx.subscribe_features(self.ctx.strategy_id, [vix_symbol], bar_sec=60)

        # 盘前参数大脑（若未外部注入 dyn 参数，则本地计算）
        await self._premarket_calibrate()

        self.logger.info("TSLA V7.2 initialized.", dyn=self.state["dyn"], subs={"1m":[self.symbol], "5m":[self.symbol]+subs_5m, "1m_vix":["VIX"]})

    # ---------------------- 事件主循环 ----------------------

    async def on_features(self, symbol: str, features: Dict[str, Any]):
        # 🔥 Reduce logging frequency to prevent memory buildup (only log every 100 bars)
        if not hasattr(self, '_on_features_counter'):
            self._on_features_counter = 0
        self._on_features_counter += 1

        if self._on_features_counter % 100 == 0:
            ts = features.get('ts') or features.get('timestamp')
            self.logger.info("on_features progress", symbol=symbol, ts=ts, close=features.get('close'), counter=self._on_features_counter)

        # 基础健康检查
        for k in ['open','high','low','close']:
            if symbol==self.symbol and (k not in features or features[k] is None or features[k] <= 0):
                self.logger.warning("Invalid bar, skip.", symbol=symbol, field=k, value=features.get(k)); return
        # Volume can be 0, just check it exists
        if symbol==self.symbol and ('volume' not in features or features['volume'] is None):
            self.logger.warning("Invalid bar (missing volume), skip.", symbol=symbol); return

        # 市场守护
        if symbol in self.cfg.get("market_regime_symbols", ["SPY","QQQ"]):
            self.logger.info("Market regime symbol, updating", symbol=symbol)
            await self._update_market_regime(symbol); return
        vix_symbol = self.cfg.get("regime", {}).get("vix_symbol", "VIX")
        if symbol == vix_symbol:
            self.logger.info("VIX symbol, updating", symbol=symbol)
            self.state['latest_vix'] = float(features.get('close', self.state['latest_vix'])); return
        if symbol != self.symbol:
            self.logger.info("Not our symbol, skipping", symbol=symbol, our_symbol=self.symbol)
            return

        # Use timestamp from features if available (backtest mode), otherwise use wall clock
        bar_ts = features.get('ts') or features.get('timestamp')
        if bar_ts and isinstance(bar_ts, pd.Timestamp):
            now = bar_ts.to_pydatetime() if hasattr(bar_ts, 'to_pydatetime') else bar_ts
        else:
            now = _ny_now()

        # 🔥 Reduce frequent logging
        if self._on_features_counter % 100 == 0:
            self.logger.info("Using timestamp", now=now, source="features" if bar_ts else "wall_clock")

        # 小幅再校准（使用配置的间隔）
        recal_interval = self.cfg.get("timing", {}).get("recalibration_interval_min", 15)
        if now.minute % recal_interval == 0 and self.state.get("_last_dyn_minute") != now.minute:
            await self._mini_recalibrate()
            self.state["_last_dyn_minute"] = now.minute

        # 极端行情暂停 & KillSwitch & EOD 平仓
        if self._extreme_pause(features, current_time=now):
            return
        if self._kill_switch(features):
            self.logger.info("Kill switch triggered, returning")
            return
        eod_time_str = self.cfg.get("timing", {}).get("eod_close_time", "15:57")
        eod_hour, eod_min = map(int, eod_time_str.split(":"))
        if now.time() >= dt.time(eod_hour, eod_min):
            self.logger.info("EOD time reached, forcing flat")
            await self._force_flat("EOD", features); return

        # 🔥 Reduce frequent logging
        if self._on_features_counter % 100 == 0:
            self.logger.info("Passed all early checks, proceeding to signal logic")

        # 成本门槛缓存（按时间槽 P95 half-spread）
        eps = await self._estimate_slippage_eps(now)

        # 价位/时间退出先于入场
        await self._exit_management(features)

        # 同刻冷却（防同一bar多模块连发）
        if self.state["_cooldown_until"] and now < self.state["_cooldown_until"]:
            return

        # 风控乘数（回撤 + 市场）
        dd_mul, dd_reason = self.guard.risk_multiplier(self.cfg.get("dd_limits", {"soft_pct":0.018,"hard_pct":0.028}))
        market_mult = float(self.state.get("market_mult", 0.75))
        final_mul = dd_mul * market_mult

        # 🔥 Reduce frequent logging
        if self._on_features_counter % 100 == 0:
            self.logger.info("Risk Multipliers Check",
                            dd_multiplier=dd_mul,
                            dd_reason=dd_reason,
                            market_regime=self.state["market_regime"],
                            market_multiplier=market_mult,
                            final_multiplier=final_mul,
                            pnl_today=self.guard.state.get("pnl_today", 0.0))

        if final_mul == 0.0:
            self.logger.warning("🚫 Blocked by multipliers.", dd=dd_reason, regime=self.state["market_regime"]); return

        # 拉取1m/5m（使用配置的限制）
        data_limits = self.cfg.get("data_limits", {})
        bars_1m = data_limits.get("bars_1m", 240)
        bars_5m = data_limits.get("bars_5m", 240)

        df1 = await self._get_df(self.symbol, '1m', bars_1m)
        df1 = _clean_rth(df1) if df1 is not None else None

        df5 = await self._get_df(self.symbol, '5m', bars_5m)
        df5 = _clean_rth(df5) if df5 is not None else None

        # 🔥 Reduce frequent logging - only log data fetching every 100 bars
        if self._on_features_counter % 100 == 0:
            self.logger.info("Data fetch summary",
                            df1_rows=len(df1) if df1 is not None and not df1.empty else 0,
                            df5_rows=len(df5) if df5 is not None and not df5.empty else 0)

        if df1 is None or df1.empty or df5 is None or df5.empty:
            self.logger.warning("Data check failed - returning early", df1_empty=df1 is None or df1.empty, df5_empty=df5 is None or df5.empty)
            return

        # 模块优先级：B1 > A1 > A2（使用配置的cooldown时间）
        cooldown_cfg = self.cfg.get("timing", {}).get("cooldown", {})

        sigB1 = self._sig_b1_orb(df5)
        if sigB1:
            self.logger.info("Signal Matched: B1_ORB", details=sigB1)
            cost_result = self._pass_cost_gate(sigB1, features, df1, df5, eps)
            self.logger.info("B1_ORB Cost Gate", passed=cost_result["passed"], edge=cost_result.get("net_edge"), details=cost_result)
            if cost_result["passed"]:
                await self._execute(sigB1, features, final_mul, eps)
                self._cooldown(cooldown_cfg.get("B1", 5))
                return
            else:
                self.logger.info("B1_ORB signal REJECTED by cost gate", reason=cost_result.get("reason"))

        sigA1 = self._sig_a1_vwap(df1, df5)
        if sigA1:
            self.logger.info("Signal Matched: A1_VWAP", details=sigA1)
            cost_result = self._pass_cost_gate(sigA1, features, df1, df5, eps)
            self.logger.info("A1_VWAP Cost Gate", passed=cost_result["passed"], edge=cost_result.get("net_edge"), details=cost_result)
            if cost_result["passed"]:
                await self._execute(sigA1, features, final_mul, eps)
                self._cooldown(cooldown_cfg.get("A1", 3))
                return
            else:
                self.logger.info("A1_VWAP signal REJECTED by cost gate", reason=cost_result.get("reason"))

        sigA2 = self._sig_a2_grid(df5)
        if sigA2:
            self.logger.info("Signal Matched: A2_GRID", details=sigA2)
            cost_result = self._pass_cost_gate(sigA2, features, df1, df5, eps)
            self.logger.info("A2_GRID Cost Gate", passed=cost_result["passed"], edge=cost_result.get("net_edge"), details=cost_result)
            if cost_result["passed"]:
                await self._execute(sigA2, features, final_mul, eps)
                self._cooldown(cooldown_cfg.get("A2", 3))
                return
            else:
                self.logger.info("A2_GRID signal REJECTED by cost gate", reason=cost_result.get("reason"))

    async def on_fill(self, symbol: str, qty: int, price: float, side: str):
        if symbol == self.symbol:
            self.guard.record_fill(qty, price, side)

    # ---------------------- 执行与下单 ----------------------

    def _marketable_limit(self, features: Dict[str, Any], side: str, eps: float) -> float:
        bid, ask, close = features.get('bid'), features.get('ask'), features.get('close')
        px = float(close)
        if bid and ask and ask >= bid:
            if side.upper()=="BUY":
                return float(ask * (1 + eps))
            else:
                return float(bid * (1 - eps))
        # BACKTEST MODE: No bid/ask available - use aggressive marketable limit
        # For BUY: place limit above close to increase fill probability
        # For SELL: place limit below close
        if side.upper()=="BUY":
            return float(px * (1 + eps * 3))  # More aggressive than normal marketable limit
        else:
            return float(px * (1 - eps * 3))

    async def _execute(self, sig: Dict[str, Any], features: Dict[str,Any], mult: float, eps: float):
        px = float(features['close'])
        base_pct = float(self.cfg.get("positioning", {}).get("base_unit_pct", 0.012))
        base_usd = self.params.capital_limit_usd * base_pct
        base_qty = max(1, int(base_usd / max(px, 1e-6)))

        # === NEW: A2 pyramid layering with sizing ratios ===
        target_qty = base_qty
        layer_info = ""
        if sig.get("type") == "A2_LAYER":
            layer = int(sig.get("layer", 1))
            # Get sizing ratios from config (default: [1.0, 0.8, 0.6, 0.4])
            sizing_ratios = self.cfg.get("A2", {}).get("layer_ratios", [1.0, 0.8, 0.6, 0.4])
            if layer <= len(sizing_ratios):
                ratio = sizing_ratios[layer - 1]
                target_qty = max(1, int(base_qty * ratio))
                layer_info = f"layer{layer} ratio={ratio}"

        # === NEW: Unified probe sizing (applies to all signal types) ===
        is_probe = sig.get("anti_starvation_probe", False)
        probe_info = ""
        if is_probe:
            probe_ratio = self.cfg.get("anti_starvation", {}).get("probe_ratio", 0.5)
            target_qty = max(1, int(target_qty * probe_ratio))
            probe_info = f"probe={probe_ratio}"

        # Apply risk multiplier
        final_qty = int(target_qty * mult)

        self.logger.info("Position Sizing",
                        current_px=round(px, 2),
                        capital_limit=self.params.capital_limit_usd,
                        base_pct=base_pct,
                        base_usd=round(base_usd, 2),
                        base_qty=base_qty,
                        layer_info=layer_info,
                        probe_info=probe_info,
                        target_qty_after_layers=target_qty,
                        risk_multiplier=mult,
                        final_qty=final_qty,
                        is_anti_starvation_probe=is_probe,
                        notional_usd=round(final_qty * px, 2))

        if final_qty <= 0:
            self.logger.warning("🚫 Final qty is 0 after multipliers", target_qty=target_qty, mult=mult)
            return

        limit_px = self._marketable_limit(features, side="BUY", eps=eps)
        order_style = OrderStyle(kind="LMT", limit_px=limit_px)
        
        ts_us = _ts_us(features)
        if not ts_us:
            ts_us = self.ctx.now_us()
            
        rationale=f"{sig['type']} {sig.get('reason','')}" + f" | Regime={self.state['market_regime']}"
        
        envelope = DecisionEnvelope(
            decision_id=make_decision_id(),
            strategy_id=self.ctx.strategy_id,
            symbol=self.symbol,
            ts_bar_end_us=int(ts_us),
            target_qty=float(final_qty),
            max_notional=float(final_qty * px),
            order_style=order_style,
            freshness=Freshness(ttl_ms=self.params.ttl_ms, dlb_ms=30000),
            extras={
                "rationale": rationale,
                "ref_px": px,
            }
        )

        self.logger.info(
            "Triggering Order",
            strategy_id=self.ctx.strategy_id,
            decision_id=envelope.decision_id,
            symbol=envelope.symbol,
            side="BUY",
            qty=envelope.target_qty,
            limit_px=limit_px,
            rationale=rationale
        )

        await self.ctx.submit_intent(envelope)


    async def _force_flat(self, reason: str, features: Dict[str, Any]):
        pos = self.guard.state.get("position", {})
        qty = int(pos.get("qty", 0))
        if qty <= 0: return

        px = float(features.get('close', pos.get("avg_px", 100.0)))
        order_style = OrderStyle(kind="MKT")

        ts_us = _ts_us(features)
        if not ts_us:
            ts_us = self.ctx.now_us()

        rationale = f"FORCE CLOSE: {reason}"

        envelope = DecisionEnvelope(
            decision_id=make_decision_id(),
            strategy_id=self.ctx.strategy_id,
            symbol=self.symbol,
            ts_bar_end_us=int(ts_us),
            target_qty=float(-qty), # Sell to close
            max_notional=float(qty * px),
            order_style=order_style,
            freshness=Freshness(ttl_ms=self.params.ttl_ms, dlb_ms=30000),
            extras={ "rationale": rationale }
        )
        
        self.logger.info(
            "Triggering Force Close Order",
            strategy_id=self.ctx.strategy_id,
            decision_id=envelope.decision_id,
            symbol=envelope.symbol,
            side="SELL",
            qty=abs(envelope.target_qty),
            rationale=rationale
        )

        await self.ctx.submit_intent(envelope)


    # ---------------------- 模块信号 ----------------------

    def _sig_a1_vwap(self, df1: pd.DataFrame, df5: pd.DataFrame) -> Optional[Dict[str,Any]]:
        p = self.state["dyn"];
        if len(df1) < 10:
            self.logger.info("A1_VWAP: Insufficient 1m bars", bars=len(df1), required=10)
            return None

        vwap = _vwap_session(df1)
        close = float(df1['close'].iloc[-1])
        vwap_val = float(vwap.iloc[-1])
        dev = (close - vwap_val) / vwap_val

        # RVOL（用5m）
        rvol = _intraday_rvol_5m(df5.tail(78), lookback_days=min(7, p.get("windows_short",7)))
        rvol_now = float(rvol.iloc[-1]) if not math.isnan(rvol.iloc[-1]) else 1.0

        thr = float(p['A1_thr_sigma'])
        starv_lvl = int(self.guard.state.get("starvation_level", 0))
        if starv_lvl >= 1:
            thr *= float(self.cfg.get("anti_starvation", {}).get("relax_sigma",[0.9,0.8])[starv_lvl-1])

        rvol_pass = (p['A1_rvol_lo'] <= rvol_now <= p['A1_rvol_hi'])
        dev_pass = (close < vwap_val) and (dev <= -thr)
        paused = self._paused()

        self.logger.info("A1_VWAP Check",
                         close=round(close, 2),
                         vwap=round(vwap_val, 2),
                         dev=round(dev, 4),
                         threshold=-round(thr, 4),
                         rvol=round(rvol_now, 2),
                         rvol_range=f"[{p['A1_rvol_lo']}, {p['A1_rvol_hi']}]",
                         rvol_pass=rvol_pass,
                         dev_pass=dev_pass,
                         paused=paused,
                         starv_lvl=starv_lvl)

        if not rvol_pass:
            self.logger.info("A1_VWAP: RVOL out of range", rvol=rvol_now, range=f"[{p['A1_rvol_lo']}, {p['A1_rvol_hi']}]")
            return None

        if dev_pass and (not paused):
            return {"type":"A1_ENTRY", "reason": f"VWAP dev={dev:.4f}<=-{thr:.4f}"}
        return None

    def _sig_a2_grid(self, df5: pd.DataFrame) -> Optional[Dict[str,Any]]:
        p = self.state["dyn"]
        if len(df5) < 60: return None
        sma30 = _ema(df5['close'], span=30)  # 近似做平滑SMA30
        close = float(df5['close'].iloc[-1])
        anchor = float(sma30.iloc[-1])
        g = float(p['A2_grid_g'])
        starv_lvl = int(self.guard.state.get("starvation_level", 0))
        if starv_lvl >= 1:
            g *= float(self.cfg.get("anti_starvation", {}).get("relax_g",[0.9,0.8])[starv_lvl-1])
            g = max(g, float(p['A2_grid_g_floor']))

        # 单边过滤
        oneway_thr = float(p['A2_oneway_thr'])
        day_open = float(df5['open'].iloc[0])
        oneway_move = abs(close - day_open) / max(1e-6, day_open)
        oneway_pass = oneway_move < (oneway_thr/100.0)

        max_layers = int(p['A2_max_layers'])
        cur_layers = int(self.guard.state.get("a2_layers", 0))
        layers_pass = cur_layers < max_layers

        want_px = anchor - g * max(1, cur_layers+1)
        price_pass = close <= want_px
        paused = self._paused()

        self.logger.info("A2_GRID Check",
                         close=round(close, 2),
                         anchor=round(anchor, 2),
                         grid_g=round(g, 4),
                         want_px=round(want_px, 2),
                         price_pass=price_pass,
                         oneway_move_pct=round(oneway_move * 100, 2),
                         oneway_threshold_pct=oneway_thr,
                         oneway_pass=oneway_pass,
                         cur_layers=cur_layers,
                         max_layers=max_layers,
                         layers_pass=layers_pass,
                         paused=paused,
                         starv_lvl=starv_lvl)

        if not oneway_pass:
            self.logger.info("A2_GRID: Oneway filter blocked", oneway_move_pct=round(oneway_move * 100, 2), threshold_pct=oneway_thr)
            return None

        if not layers_pass:
            self.logger.info("A2_GRID: Max layers reached", cur_layers=cur_layers, max_layers=max_layers)
            return None

        if price_pass and (not paused):
            # 命中新层
            next_layer = cur_layers + 1
            self.guard.state["a2_layers"] = next_layer
            self.guard._persist()
            # === NEW: Return layer number and probe flag ===
            return {
                "type": "A2_LAYER",
                "reason": f"close<=anchor-g*{next_layer}",
                "layer": next_layer,  # Layer number (1-4)
                "anti_starvation_probe": (starv_lvl > 0)
            }
        return None

    def _sig_b1_orb(self, df5: pd.DataFrame) -> Optional[Dict[str,Any]]:
        p = self.state["dyn"]
        b1_cfg = self.cfg.get("B1", {})
        orb_len = int(self.state["_orb_state"]["len_min"])

        # 动态延长 ORB（趋势命中率这里用简化：若近7日单边天较多则延长）
        if p.get("B1_extend_orb", False):
            orb_len = b1_cfg.get("orb_len_extended", 45)

        if len(df5) < (orb_len // 5) + 10:
            self.logger.info("B1_ORB: Insufficient 5m bars", bars=len(df5), required=(orb_len // 5) + 10)
            return None

        orb = df5.iloc[:(orb_len // 5)]
        hi, lo = float(orb['high'].max()), float(orb['low'].min())
        self.state["_orb_state"].update({"hi":hi, "lo":lo})

        last = df5.iloc[-1]
        close = float(last['close'])
        rvol = _intraday_rvol_5m(df5.tail(78), lookback_days=min(7, p.get("windows_short",7)))
        rvol_now = float(rvol.iloc[-1]) if not math.isnan(rvol.iloc[-1]) else 1.0
        rvol_min = float(p['B1_rvol_min'])

        # 突破标记（使用配置的阈值）
        breakout_mult = b1_cfg.get("breakout", {}).get("threshold_mult", 1.001)
        breakout_threshold = hi * breakout_mult
        is_broken = self.state["_orb_state"]["broken"]

        self.logger.info("B1_ORB Check",
                         orb_len=orb_len,
                         orb_hi=round(hi, 2),
                         orb_lo=round(lo, 2),
                         close=round(close, 2),
                         breakout_threshold=round(breakout_threshold, 2),
                         breakout_mult=breakout_mult,
                         rvol=round(rvol_now, 2),
                         rvol_min=rvol_min,
                         rvol_pass=rvol_now >= rvol_min,
                         is_broken=is_broken,
                         paused=self._paused())

        if rvol_now < rvol_min:
            self.logger.info("B1_ORB: RVOL too low", rvol=round(rvol_now, 2), min=rvol_min)
            return None

        # 突破标记
        if (close >= breakout_threshold) and (not is_broken):
            self.state["_orb_state"].update({"broken":True, "break_ts": df5.index[-1]})
            self.logger.info("B1_ORB: Breakout detected", close=round(close, 2), threshold=round(breakout_threshold, 2))
            return None

        # 突破后N根内的首次回踩并收回（使用配置的阈值）
        if is_broken:
            break_ts = self.state["_orb_state"]["break_ts"]
            if break_ts is not None:
                bars_after = df5[df5.index > break_ts].tail(int(p.get("B1_pullback_window_M",6)))
                reclaim_mult = b1_cfg.get("breakout", {}).get("reclaim_mult", 0.998)
                reclaim_threshold = hi * reclaim_mult

                if len(bars_after) > 0:
                    c = float(bars_after['close'].iloc[-1])
                    reclaim_pass = c >= reclaim_threshold
                    paused = self._paused()

                    self.logger.info("B1_ORB Pullback Check",
                                     bars_since_break=len(bars_after),
                                     pullback_window=int(p.get("B1_pullback_window_M",6)),
                                     current_close=round(c, 2),
                                     reclaim_threshold=round(reclaim_threshold, 2),
                                     reclaim_mult=reclaim_mult,
                                     reclaim_pass=reclaim_pass,
                                     paused=paused)

                    if reclaim_pass and (not paused):
                        # 命中回踩做多
                        self.state["_orb_state"]["broken"] = False
                        self.logger.info("B1_ORB: PULLBACK SIGNAL GENERATED",
                                        close=round(c, 2),
                                        orb_hi=round(hi, 2),
                                        reclaim_threshold=round(reclaim_threshold, 2))
                        return {"type":"B1_ORB_PULLBACK", "reason": "Break->Pullback->Reclaim"}
        return None

    # ---------------------- 退出管理 ----------------------

    async def _exit_management(self, features: Dict[str,Any]):
        p = self.state["dyn"]
        data_limits = self.cfg.get("data_limits", {})
        bars_5m_120 = data_limits.get("bars_5m_120", 120)
        df5 = await self._get_df(self.symbol, '5m', bars_5m_120)
        if df5 is None or df5.empty: return
        df5 = _clean_rth(df5)
        pos = self.guard.state.get("position", {})
        qty = int(pos.get("qty", 0))
        if qty <= 0: return

        px = float(features['close'])
        atr5 = float(_atr_wilder(df5, 14).iloc[-1]) if len(df5)>=20 else 0.0
        k_atr = float(p['k_atr'])
        stop_usd = max(k_atr * atr5, 0.5*atr5)

        # A1：回到 VWAP 平，余下跟踪 VWAP+threshold；EOD 清（使用配置）
        a1_exit = self.cfg.get("A1", {}).get("exit", {})
        a1_eod_time_str = a1_exit.get("eod_clean_time", "14:30")
        a1_eod_hour, a1_eod_min = map(int, a1_eod_time_str.split(":"))
        a1_vwap_threshold = a1_exit.get("vwap_threshold_pct", 0.003)

        bars_1m = data_limits.get("bars_1m", 240)
        df1 = await self._get_df(self.symbol, '1m', bars_1m)
        if df1 is not None and not df1.empty:
            df1 = _clean_rth(df1)
            vwap = _vwap_session(df1).iloc[-1]
            if px >= vwap:
                await self._force_flat("A1_VWAP_TP", features); return
            if _ny_now().time() >= dt.time(a1_eod_hour, a1_eod_min) and abs(px - vwap)/max(1e-6, vwap) < a1_vwap_threshold:
                await self._force_flat("A1_1430_VWAP_CLEAN", features); return

        # A2：回到锚平，否则到EOD按配置清（使用配置）
        a2_exit = self.cfg.get("A2", {}).get("exit", {})
        a2_eod_time_str = a2_exit.get("eod_clean_time", "14:30")
        a2_eod_hour, a2_eod_min = map(int, a2_eod_time_str.split(":"))
        a2_grid_mult = a2_exit.get("eod_grid_multiplier", 0.25)

        sma30 = _ema(df5['close'], span=30).iloc[-1]
        g = float(p['A2_grid_g'])
        if px >= sma30:
            await self._force_flat("A2_ANCHOR_TP", features); return
        if _ny_now().time() >= dt.time(a2_eod_hour, a2_eod_min) and px >= (sma30 - a2_grid_mult * g):
            await self._force_flat("A2_1430_CLEAN", features); return

        # B1：使用配置的RVOL阈值
        b1_exit = self.cfg.get("B1", {}).get("exit", {})
        b1_rvol_threshold = b1_exit.get("rvol_threshold", 1.2)

        ema5 = _ema(df5['close'], span=5).iloc[-1]
        rvol = _intraday_rvol_5m(df5.tail(78), lookback_days=min(7, p.get("windows_short",7)))
        rvol_now = float(rvol.iloc[-1]) if not math.isnan(rvol.iloc[-1]) else 1.0
        if (px < ema5) and (rvol_now > b1_rvol_threshold):
            await self._force_flat("B1_EMA5_EXIT", features); return

        # 时间退出（以 A1 的 time_exit_min 为基准）
        max_min = int(p.get("A1_time_exit_min", 40))
        if self.guard.time_holding_minutes() > max_min:
            await self._force_flat("TIME_EXIT", features); return

        # 止损
        avg_px = float(pos.get("avg_px", px))
        if px <= (avg_px - stop_usd):
            await self._force_flat("ATR_STOP", features); return

    # ---------------------- 市场/暂停/门槛 ----------------------

    async def _update_market_regime(self, symbol: str):
        bars = await self._get_df(symbol, '5m', 120)
        if bars is None or bars.empty or len(bars) < 55: return
        ema50 = _ema(bars['close'], 50).iloc[-1]
        px = float(bars['close'].iloc[-1])
        eps = float(self.state["dyn"].get("regime_epsilon", 0.0048))

        # Use configured market multipliers instead of hardcoded values
        regime_mult = self.cfg.get("regime", {}).get("market_mult", {
            "BULL": 1.0, "NEUTRAL": 0.75, "BEAR": 0.5
        })

        if px > ema50 * (1+eps):
            self.state['market_regime'] = "BULL"
            self.state['market_mult'] = regime_mult.get("BULL", 1.0)
        elif px < ema50 * (1-eps):
            self.state['market_regime'] = "BEAR"
            self.state['market_mult'] = regime_mult.get("BEAR", 0.5)
        else:
            self.state['market_regime'] = "NEUTRAL"
            self.state['market_mult'] = regime_mult.get("NEUTRAL", 0.75)

    def _paused(self, current_time: Optional[dt.datetime] = None) -> bool:
        """Check if strategy is paused. Use current_time from bar in backtest mode."""
        pause_until = self.state.get("pause_until")
        if not pause_until:
            return False
        # Use provided time (from bar in backtest) or wall clock (in live mode)
        now = current_time if current_time else _ny_now()
        return now < pause_until

    def _extreme_pause(self, features: Dict[str,Any], current_time: Optional[dt.datetime] = None) -> bool:
        """
        Check for extreme market conditions that trigger pause.
        Args:
            features: Current bar features
            current_time: Current bar timestamp (for backtest) or None (for live, uses wall clock)
        """
        p = self.state["dyn"]
        cfg = p.get("pause", {"theta_vix":0.12, "theta_spread":0.0007, "theta_atr":2.1, "t_pause_min":15})

        # Get current time (bar time in backtest, wall clock in live)
        now = current_time if current_time else _ny_now()

        # FIRST: Check if already paused (avoid re-evaluating conditions)
        paused = self._paused(current_time=now)
        if paused:
            # Log every 100 calls to avoid spam
            if not hasattr(self, '_pause_log_counter'):
                self._pause_log_counter = 0
            self._pause_log_counter += 1
            if self._pause_log_counter % 100 == 0:
                self.logger.info("Still paused", pause_until=self.state.get('pause_until'), current_time=now, counter=self._pause_log_counter)
            return True

        # SECOND: Evaluate spike conditions only if NOT paused
        # VIX spike
        prev_vix = float(self.state.get("prev_vix", self.state['latest_vix']))
        cur_vix  = float(self.state['latest_vix'])
        if prev_vix > 0 and ((cur_vix - prev_vix)/prev_vix) > float(cfg.get("theta_vix",0.12)):
            self.state['pause_until'] = now + dt.timedelta(minutes=int(cfg.get("t_pause_min",15)))
            self.state['prev_vix'] = cur_vix
            self.logger.warning("VIX spike → pause.", vix=cur_vix, pause_until=self.state['pause_until'], current_time=now)
            return True
        self.state['prev_vix'] = cur_vix

        # Spread widen（half-spread%）
        bid, ask = features.get('bid'), features.get('ask')
        if bid and ask and ask>=bid and ask>0:
            half_spread_pct = (ask - bid) / (ask + bid)
            if half_spread_pct > float(cfg.get("theta_spread",0.0007)):
                self.state['pause_until'] = now + dt.timedelta(minutes=int(cfg.get("t_pause_min",15)))
                self.logger.warning("Wide spread → pause.", spread_pct=float(2*half_spread_pct), pause_until=self.state['pause_until'], current_time=now)
                return True

        # 5m ATR spike（需要最近5m缓存）
        bars = self.state.get("_last_5m")
        if isinstance(bars, pd.DataFrame) and len(bars) >= 16:
            atr14 = float(_atr_wilder(bars, 14).iloc[-1])
            last_move = abs(float(bars['close'].iloc[-1]) - float(bars['close'].iloc[-2]))
            if atr14>0 and (last_move/atr14) > float(cfg.get("theta_atr",2.1)):
                self.state['pause_until'] = now + dt.timedelta(minutes=int(cfg.get("t_pause_min",15)))
                self.logger.warning("ATR spike → pause.", ratio=last_move/atr14, pause_until=self.state['pause_until'], current_time=now)
                return True

        # Not paused
        return False

    def _kill_switch(self, features: Dict[str,Any]) -> bool:
        # 简化：若 1m 波动 >= 3×ATR(14,1m)（需要 df1），在 _exit_management 中会触发；此处只做 L1 断流快速停机
        # 若 L1 断流：bid/ask 连续缺失/交叉视为异常（这里仅轻量检测）
        bid, ask = features.get('bid'), features.get('ask')
        # 仅在交易时段内严格要求 L1；非交易时段允许缺失（回测/夜盘环境常见）
        now_local = _ny_now().time()
        in_rth = dt.time(9, 30) <= now_local <= dt.time(16, 10)

        # 若有价差交叉（bid>ask），无论何时都视为异常
        if (bid is not None and ask is not None) and (bid > ask):
            self.logger.error("L1 crossed book detected (bid>ask). Force flat & stop today.")
            return True

        # 仅在 RTH 才将缺失视作致命；非 RTH 直接忽略 L1 缺失（但不入场）
        if (bid is None or ask is None):
            if in_rth:
                self.logger.warning("L1 missing during RTH; skipping entries until recovered.")
            # 非 RTH：不触发终止开关，返回 False 以继续流程（策略其它门槛会限制进场）
            return False

        return False

    async def _estimate_slippage_eps(self, now_ts: dt.datetime) -> float:
        # 先用配置提供的 eps_p95，再裁剪到[min,max]
        slip_cfg = self.cfg.get("slippage", {"eps_p95":0.0005,"min":0.0002,"max":0.0008})
        eps = float(slip_cfg.get("eps_p95", 0.0005))
        return float(np.clip(eps, float(slip_cfg.get("min",0.0002)), float(slip_cfg.get("max",0.0008))))

    def _pass_cost_gate(self, sig: Dict[str,Any], feat: Dict[str,Any], df1: pd.DataFrame, df5: pd.DataFrame, eps: float) -> Dict[str, Any]:
        # 估计单笔"成本后边际"：粗略保守估计
        px = float(feat['close'])
        commission = float(self.cfg.get("commission_per_share", 0.0035))
        slip_cost = px * eps   # half-spread 单边

        # A1：到 VWAP 潜在收益（使用配置的profit_take_pct）
        if sig['type'].startswith("A1"):
            a1_profit_pct = self.cfg.get("A1", {}).get("exit", {}).get("profit_take_pct", 0.7)
            vwap = float(_vwap_session(df1).iloc[-1])
            gross = max(0.0, vwap - px) * a1_profit_pct
            target_px = vwap
            profit_type = "VWAP"
        elif sig['type'].startswith("A2"):
            a2_profit_pct = self.cfg.get("A2", {}).get("exit", {}).get("anchor_profit_pct", 0.6)
            sma30 = float(_ema(df5['close'], span=30).iloc[-1])
            g = float(self.state["dyn"].get("A2_grid_g", 1.2))
            gross = max(0.0, sma30 - px) * a2_profit_pct if px <= (sma30 - g) else 0.0
            target_px = sma30
            profit_type = "SMA30"
        else:  # B1
            b1_profit_mult = self.cfg.get("B1", {}).get("exit", {}).get("profit_mult", 1.002)
            orb_hi = float(self.state["_orb_state"].get("hi") or px)
            gross = max(0.0, orb_hi * b1_profit_mult - px)
            target_px = orb_hi * b1_profit_mult
            profit_type = "ORB_HI"

        # === NEW: Dynamic cost gate based on recent PnL ===
        recent = np.asarray(self.guard.state.get("recent_net_pnl_list", []))
        min_trades = int(self.cfg.get("dynamic_cost_gate", {}).get("min_trades", 10))
        atr_floor_pct = float(self.cfg.get("dynamic_cost_gate", {}).get("atr_floor_pct", 0.02))
        coldstart_weight = float(self.cfg.get("dynamic_cost_gate", {}).get("coldstart_weight", 0.5))

        # Calculate ATR floor from 5m bars
        atr5 = float(_atr_wilder(df5, 14).iloc[-1]) if len(df5) >= 20 else 0.0
        edge_from_atr = atr5 * atr_floor_pct

        if recent.size >= min_trades:
            # Sufficient history: use P60 of recent net PnL
            pnl_p60 = float(np.percentile(recent, 60))
            edge_floor = max(pnl_p60, edge_from_atr)
            floor_source = f"P60={pnl_p60:.4f}, ATR={edge_from_atr:.4f}"
        else:
            # Cold start: blend static baseline with ATR floor
            static_baseline = float(self.cfg.get("edge_floor", 0.18))
            edge_floor = coldstart_weight * static_baseline + (1 - coldstart_weight) * edge_from_atr
            floor_source = f"coldstart blend: {coldstart_weight}*{static_baseline:.4f} + {1-coldstart_weight}*{edge_from_atr:.4f}"

            # BACKTEST MODE: If no recent trades, use very permissive threshold to analyze all signals
            # This allows us to see what signals the strategy would generate even if unprofitable
            if recent.size == 0:
                edge_floor = -0.50  # Allow signals even with negative edge up to -$0.50
                floor_source += " [backtest_mode: ultra_relaxed]"

        net_edge = gross - (slip_cost + commission)
        passed = bool(net_edge >= edge_floor)

        return {
            "passed": passed,
            "net_edge": round(net_edge, 4),
            "gross_profit": round(gross, 4),
            "slip_cost": round(slip_cost, 4),
            "commission": round(commission, 4),
            "edge_floor": round(edge_floor, 4),
            "floor_source": floor_source,
            "recent_trades": recent.size,
            "current_px": round(px, 2),
            "target_px": round(target_px, 2),
            "profit_type": profit_type,
            "reason": f"net_edge={net_edge:.4f} {'>=' if passed else '<'} floor={edge_floor:.4f}"
        }

    def _cooldown(self, minutes: int):
        self.state["_cooldown_until"] = _ny_now() + dt.timedelta(minutes=minutes)

    # ---------------------- 盘前校准 & 小幅再校准 ----------------------

    async def _premarket_calibrate(self):
        """
        轻量"参数大脑"：
        - 外部注入：若存在 param_file，优先读取（参数大脑产出）
        - LKG回退：若注入失败或越界，回退到 LKG
        - 本地计算：若LKG也不存在，才执行本地计算
        - 从 7/60/180 日 1m/5m/60m 计算得到最小可用 dyn 参数
        - 目标触发率 T_target=4 的一级放宽/收紧

        如果 premarket_calibration.use_config_values = true，则直接使用JSON配置值，不重新计算
        """
        # CRITICAL: Check if we should use config values directly
        precal_cfg = self.cfg.get("premarket_calibration", {})
        use_config_values = precal_cfg.get("use_config_values", False)

        # === NEW: Try external parameter injection first ===
        param_file = precal_cfg.get("param_file", None)
        if param_file and os.path.exists(param_file):
            try:
                injected = _safe_read(param_file)
                if injected and self._dyn_in_bounds(injected):
                    self.state["dyn"] = injected
                    self.logger.info("Loaded parameters from external file", file=param_file, params=injected)
                    return
                else:
                    self.logger.warning("External params out of bounds, falling back to LKG", file=param_file)
            except Exception as e:
                self.logger.warning("Failed to load external params, will fallback", file=param_file, error=str(e))

        if use_config_values:
            # Use values directly from JSON config - 100% config-driven mode
            self.logger.info("Using config values directly (use_config_values=true)")
            self.state["dyn"] = {
                "windows_short": self.cfg.get("windows", {}).get("short_days", 7),
                "k_atr": self.cfg.get("k_atr", 2.0),
                "regime_epsilon": self.cfg.get("regime", {}).get("epsilon", 0.0048),
                "pause": self.cfg.get("pause", {}),
                "A1_thr_sigma": self.cfg.get("A1", {}).get("thr_sigma", 0.009),
                "A1_rvol_lo": self.cfg.get("A1", {}).get("rvol_lo", 0.85),
                "A1_rvol_hi": self.cfg.get("A1", {}).get("rvol_hi", 1.80),
                "A1_time_exit_min": self.cfg.get("A1", {}).get("time_exit_min", 40),
                "A2_grid_g": self.cfg.get("A2", {}).get("grid_g", 1.2),
                "A2_grid_g_floor": self.cfg.get("A2", {}).get("grid_g_floor", 0.8),
                "A2_oneway_thr": self.cfg.get("A2", {}).get("oneway_thr", 2.2),
                "A2_max_layers": self.cfg.get("A2", {}).get("max_layers", 4),
                "B1_orb_len": self.cfg.get("B1", {}).get("orb_len", 30),
                "B1_rvol_min": self.cfg.get("B1", {}).get("rvol_min", 0.85),
                "B1_pullback_window_M": self.cfg.get("B1", {}).get("pullback_window_M", 6),
                "B1_extend_orb": self.cfg.get("B1", {}).get("extend_orb", False),
            }
            return

        # Original auto-calibration logic (when use_config_values=false)
        symbol = self.symbol
        # 历史数据
        data_limits = self.cfg.get("data_limits", {})
        premarket_1m = data_limits.get("premarket_1m", 78*7*5)
        premarket_5m = data_limits.get("premarket_5m", 78*60)

        df1 = await self._get_df(symbol, '1m', premarket_1m)
        df5 = await self._get_df(symbol, '5m', premarket_5m)
        if df1 is None or df1.empty or df5 is None or df5.empty:
            self.state["dyn"] = self._lkg_or_default()
            return
        df1 = _clean_rth(df1); df5 = _clean_rth(df5)
        if df1.empty or df5.empty:
            self.state["dyn"] = self._lkg_or_default(); return

        # A1：thr_sigma（以当日 dev 分布 55~75分位候选，简化为用历史 60日 P60）
        vwap1 = _vwap_session(df1)
        dev_series = (df1['close'] - vwap1) / vwap1
        thr_sigma = float(np.clip(_percentile(dev_series.abs(), 60, 0.007), 0.004, 0.020))

        # A1：RVOL 窗口（7日 30~80分位）
        rvol = _intraday_rvol_5m(df5.tail(78*min(7, len(df5)//78)), lookback_days=min(7,5))
        A1_rvol_lo = float(np.clip(_percentile(rvol, 30, 0.85), 0.75, 1.00))
        A1_rvol_hi = float(np.clip(_percentile(rvol, 80, 1.8), 1.3, 2.5))

        # A2：网格步长 g（Pq(TR_5m) 与 D_p80/L 简化为 |ΔC_5m|分位）
        # 使用配置的bounds
        a2_bounds = precal_cfg.get("bounds", {}).get("A2_grid_g", {"min": 0.6, "max": 3.5})
        tr5 = _atr_wilder(df5, 1)  # 用TR近似
        A2_grid_g = float(np.clip(_percentile(tr5, 28, 1.0), a2_bounds["min"], a2_bounds["max"]))
        A2_grid_g_floor = float(np.clip(_percentile(tr5, 20, 0.8), 0.4, 3.0))
        A2_oneway_thr = self.cfg.get("A2", {}).get("oneway_thr", 2.2)
        A2_max_layers = self.cfg.get("A2", {}).get("max_layers", 4)

        # B1：ORB 30/45 选择（使用配置）
        B1_orb_len = self.cfg.get("B1", {}).get("orb_len", 30)
        B1_rvol_min = float(np.clip(_percentile(rvol, 40, 0.85), 0.75, 1.10))
        B1_pullback_M = self.cfg.get("B1", {}).get("pullback_window_M", 6)

        # k_ATR：90分位 |ΔC_5m| / mean(ATR_5m) 投影到 [1.8,2.4]，简化到 2.0
        # 使用配置的bounds
        k_atr_bounds = precal_cfg.get("bounds", {}).get("k_atr", {"min": 1.4, "max": 3.0})
        atr5 = _atr_wilder(df5, 14)
        atr_mean = float(np.nanmean(atr5.tail(78))) if len(atr5)>=78 else float(np.nanmean(atr5))
        ret_abs = df5['close'].diff().abs().dropna()
        ratio = ( _percentile(ret_abs, 90, 1.2) / max(atr_mean, 1e-6) )
        k_atr = float(np.clip(1.2 + 0.4*((ratio-1.2)/(3.0-1.2)), k_atr_bounds["min"], k_atr_bounds["max"]))

        # 市场 ε（使用配置值）
        regime_epsilon = self.cfg.get("regime", {}).get("epsilon", 0.0048)

        dyn = {
            "windows_short": self.cfg.get("windows", {}).get("short_days", 7),
            "k_atr": k_atr,
            "regime_epsilon": regime_epsilon,
            "pause": self.cfg.get("pause", {}),
            "A1_thr_sigma": thr_sigma,
            "A1_rvol_lo": A1_rvol_lo,
            "A1_rvol_hi": A1_rvol_hi,
            "A1_time_exit_min": int(np.clip(np.nanmedian(self._measure_return_time_to_vwap(df1)), 25, 60)),
            "A2_grid_g": A2_grid_g,
            "A2_grid_g_floor": A2_grid_g_floor,
            "A2_oneway_thr": A2_oneway_thr,
            "A2_max_layers": A2_max_layers,
            "B1_orb_len": B1_orb_len,
            "B1_rvol_min": B1_rvol_min,
            "B1_pullback_window_M": B1_pullback_M,
            "B1_extend_orb": self.cfg.get("B1", {}).get("extend_orb", False),
        }

        # 目标触发率（粗略）：若过去7日 A1/A2 触发估算 < 2 ⇒ 放宽一级
        T_target = int(self.cfg.get("target_triggers", 4))
        est_trigs = self._rough_trigger_estimate(df1, df5, dyn)
        if est_trigs < 0.5*T_target:
            dyn["A1_thr_sigma"] *= float(self.cfg.get("anti_starvation",{}).get("relax_sigma",[0.9,0.8])[0])
            dyn["A2_grid_g"]   *= float(self.cfg.get("anti_starvation",{}).get("relax_g",[0.9,0.8])[0])
            dyn["A2_grid_g"] = max(dyn["A2_grid_g"], dyn["A2_grid_g_floor"])

        # 护栏与 LKG 回退
        if not self._dyn_in_bounds(dyn):
            self.state["dyn"] = self._lkg_or_default(adjust=True)
        else:
            self.state["dyn"] = dyn
            if self.cfg.get("lkg",{}).get("enabled", True):
                # Dynamic filename based on symbol
                lkg_file = f"strategies/configs/{self.symbol.lower()}_v7_2.json"
                # Read existing config, update dynamic params, write back
                existing_cfg = _safe_read(lkg_file)
                if existing_cfg:
                    # Update only the dynamic parameter fields
                    existing_cfg.update({
                        "k_atr": dyn.get("k_atr"),
                        "A1_thr_sigma": dyn.get("A1_thr_sigma"),
                    })
                    if "A1" in existing_cfg:
                        existing_cfg["A1"]["thr_sigma"] = dyn.get("A1_thr_sigma")
                        existing_cfg["A1"]["rvol_lo"] = dyn.get("A1_rvol_lo")
                        existing_cfg["A1"]["rvol_hi"] = dyn.get("A1_rvol_hi")
                        existing_cfg["A1"]["time_exit_min"] = dyn.get("A1_time_exit_min")
                    if "A2" in existing_cfg:
                        existing_cfg["A2"]["grid_g"] = dyn.get("A2_grid_g")
                        existing_cfg["A2"]["grid_g_floor"] = dyn.get("A2_grid_g_floor")
                    if "B1" in existing_cfg:
                        existing_cfg["B1"]["rvol_min"] = dyn.get("B1_rvol_min")
                    _atomic_write(lkg_file, existing_cfg)
                else:
                    # Fallback: write dyn params only (shouldn't happen in production)
                    _atomic_write(lkg_file, dyn)

    async def _mini_recalibrate(self):
        """
        日内轻量再校准：
        - 读取 intraday_suggest_file（若存在）
        - 使用 EMA 平滑更新：new = alpha × suggest + (1-alpha) × current
        - 验证边界后应用
        """
        precal_cfg = self.cfg.get("premarket_calibration", {})
        intraday_file = precal_cfg.get("intraday_suggest_file", None)
        if not intraday_file or not os.path.exists(intraday_file):
            return  # No intraday suggestions available

        try:
            suggestion = _safe_read(intraday_file)
            if not suggestion:
                return

            # Get smoothing alpha (default 0.3 = 30% new, 70% old)
            alpha = float(precal_cfg.get("intraday_alpha", 0.3))
            current = self.state.get("dyn", {})

            # Blend parameters with EMA-style smoothing
            blended = {}
            for key in ["k_atr", "A1_thr_sigma", "A2_grid_g", "A1_rvol_lo", "A1_rvol_hi"]:
                if key in suggestion and key in current:
                    new_val = alpha * float(suggestion[key]) + (1 - alpha) * float(current[key])
                    blended[key] = new_val
                elif key in current:
                    blended[key] = current[key]

            # Copy over non-smoothed parameters
            for key in current:
                if key not in blended:
                    blended[key] = current[key]

            # Validate bounds
            if self._dyn_in_bounds(blended):
                self.state["dyn"].update(blended)
                self.logger.info("Applied intraday parameter update", alpha=alpha, updated_keys=list(blended.keys()))
            else:
                self.logger.warning("Intraday suggestion out of bounds, skipped", suggestion=suggestion)

        except Exception as e:
            self.logger.error("Failed to apply intraday recalibration", file=intraday_file, error=str(e))

    # ---------------------- 辅助：VWAP回归时长估计/触发估算/护栏/LKG ----------------------

    def _measure_return_time_to_vwap(self, df1: pd.DataFrame) -> List[int]:
        vwap = _vwap_session(df1)
        bb = _bbands(df1, 20, 2.0)
        spans, on, t0 = [], False, None
        for i in range(21, len(df1)):
            px = float(df1['close'].iloc[i]); vv = float(vwap.iloc[i]); lo = float(bb['bb_lo'].iloc[i])
            if (not on) and (px < lo) and (px < vv):
                on=True; t0=i
            elif on and px >= vv:
                spans.append(i - t0); on=False; t0=None
        return [int(x) for x in spans] if spans else [40]

    def _rough_trigger_estimate(self, df1: pd.DataFrame, df5: pd.DataFrame, dyn: Dict[str,Any]) -> int:
        # 粗略统计近7日中 A1 下穿 & A2 触层的潜在次数（使用配置的权重）
        weights = self.cfg.get("premarket_calibration", {}).get("trigger_estimate_weights", {"a1": 0.1, "a2": 0.05})

        vwap = _vwap_session(df1)
        dev = (df1['close'] - vwap) / vwap
        a1_cnt = int((dev < -float(dyn["A1_thr_sigma"])).sum())

        sma30 = _ema(df5['close'], span=30)
        g = float(dyn["A2_grid_g"])
        a2_cnt = int((df5['close'] <= (sma30 - g)).sum())
        return int(weights["a1"] * a1_cnt + weights["a2"] * a2_cnt)

    def _dyn_in_bounds(self, d: Dict[str,Any]) -> bool:
        # 使用配置的bounds进行验证
        bounds_cfg = self.cfg.get("premarket_calibration", {}).get("bounds", {})

        conds = [
            bounds_cfg.get("A1_thr_sigma", {"min": 0.004, "max": 0.020})["min"] <= d["A1_thr_sigma"] <= bounds_cfg.get("A1_thr_sigma", {"min": 0.004, "max": 0.020})["max"],
            bounds_cfg.get("A2_grid_g", {"min": 0.6, "max": 3.5})["min"] <= d["A2_grid_g"] <= bounds_cfg.get("A2_grid_g", {"min": 0.6, "max": 3.5})["max"],
            bounds_cfg.get("k_atr", {"min": 1.4, "max": 3.0})["min"] <= d.get("k_atr",2.0) <= bounds_cfg.get("k_atr", {"min": 1.4, "max": 3.0})["max"],
            bounds_cfg.get("A1_rvol_lo", {"min": 0.75, "max": 1.10})["min"] <= d["A1_rvol_lo"] <= bounds_cfg.get("A1_rvol_lo", {"min": 0.75, "max": 1.10})["max"],
            bounds_cfg.get("A1_rvol_hi", {"min": 1.0, "max": 2.5})["min"] <= d["A1_rvol_hi"] <= bounds_cfg.get("A1_rvol_hi", {"min": 1.0, "max": 2.5})["max"],
            bounds_cfg.get("A1_time_exit_min", {"min": 25, "max": 60})["min"] <= d["A1_time_exit_min"] <= bounds_cfg.get("A1_time_exit_min", {"min": 25, "max": 60})["max"]
        ]
        return all(conds)

    def _lkg_or_default(self, adjust: bool=False) -> Dict[str,Any]:
        lkg_cfg = self.cfg.get("lkg", {})
        lkg_file = lkg_cfg.get("file", "tsla_v7_2_lkg.json")

        if os.path.exists(lkg_file):
            d = _safe_read(lkg_file)
            if adjust:
                # Use configured adjustment factors
                adj_factors = lkg_cfg.get("adjustment_factors", {
                    "k_atr": 1.10,
                    "A1_thr_sigma": 1.05,
                    "A2_grid_g": 1.05
                })
                d["k_atr"] = float(d.get("k_atr",2.0)) * adj_factors.get("k_atr", 1.10)
                d["A1_thr_sigma"] = float(d.get("A1_thr_sigma",0.009)) * adj_factors.get("A1_thr_sigma", 1.05)
                d["A2_grid_g"] = float(d.get("A2_grid_g",1.2)) * adj_factors.get("A2_grid_g", 1.05)
            return d
        # 极简默认
        return {
            "windows_short":7,
            "k_atr":2.0,
            "regime_epsilon":0.0048,
            "pause":{"theta_vix":0.12,"theta_spread":0.0007,"theta_atr":2.1,"t_pause_min":15},
            "A1_thr_sigma":0.009,
            "A1_rvol_lo":0.85,
            "A1_rvol_hi":1.80,
            "A1_time_exit_min":40,
            "A2_grid_g":1.2,
            "A2_grid_g_floor":0.8,
            "A2_oneway_thr":2.2,
            "A2_max_layers":4,
            "B1_orb_len":30,
            "B1_rvol_min":0.85,
            "B1_pullback_window_M":6
        }

    # ---------------------- 数据接口封装 ----------------------

    async def _get_df(self, symbol: str, bar_type: str, limit: int) -> Optional[pd.DataFrame]:
        raw = await self.ctx.history_features(symbol, bar_type, limit)
        if not raw:
            return None

        rows = []
        for r in raw:
            ts = r.get('ts') or r.get('timestamp') or r.get('time')
            if ts is None: continue
            rows.append([pd.Timestamp(ts), r['open'], r['high'], r['low'], r['close'], r.get('volume', 0)])

        if not rows:
            return None

        df = pd.DataFrame(rows, columns=['ts','open','high','low','close','volume']).set_index('ts').sort_index()

        try:
            df.index = df.index.tz_convert("America/New_York")
        except Exception:
            try: df.index = df.index.tz_localize("America/New_York")
            except Exception: pass

        # 缓存最近5m
        if symbol==self.symbol and bar_type=='5m':
            # 🔥 Memory fix: Delete old DataFrame before assigning new one
            # The .copy() was creating a new DataFrame on every bar without releasing the old one
            # This caused ~20GB memory accumulation during ATR spike conditions
            old_df = self.state.get("_last_5m")
            if old_df is not None:
                del old_df
            # Store reference directly instead of copy (df is already a new object from line above)
            self.state["_last_5m"] = df

        return df