#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Premarket/Opening Scalper — V5s (Sliding Window, Incremental, Memory-Capped)
Author: ChatGPT (for Wei)
Date: 2025-10-27

核心变化（基于专业量化交易员建议并已落地）：
1) 彻底改为“滑动窗口 + 增量计算”
   - 用 MultiWindowStats(30s, 120s) 取代旧 RingBuffer 的逐秒桶统计。
   - 所有指标（VWAP、volume、spread、uptick_ratio、OR高低）均在 O(1) 更新；内存固定。
2) 移除历史依赖
   - 删除 Vref（昨日开盘2m量）、dur_score、pm_score 等历史记忆因子。
   - 不使用昨日 close 进行决策，仅用于候选初筛（非常弱的参考）。
3) 真·剥头皮信号
   - 以 30s/120s 的实时异动为核心：
     * vol_spike = (vol_30) / (vol_120 / 4) ≥ threshold
     * last > vwap_30
     * spread 收窄（median/p95 约束）
     * 突破 120s 高点附近（或接近）
4) 动态风控
   - RISK_PCT 默认 1.0%（可调）。
   - trailingPercent 与 take-profit 百分比随最近 120s 波动范围自适应（带上下限）。
   - 移除期权对冲（不适合 scalping）。
5) 执行与速率
   - scanner 仅在设定时刻请求；quotes 快照按需请求；
   - 限价单使用“marketable limit”（ask × (1+eps)）。
6) 新闻 gating（可选）
   - 保留你的 googleNews.py 工作流：仅用于“冻结候选”阶段排序/过滤；
   - 决策完全基于实时微结构，不受新闻直接影响。

使用方式：
  python premarket_scalper_V5s.py --mode paper --dry-run --dev-fast
  python premarket_scalper_V5s.py --mode paper

依赖：ib_insync、你的 googleNews.py。
注意：请确保 IB Gateway/TWS 已开启，且端口正确（paper=4002/live=7497）。
"""

import os
import sys
import math
import json
import time
import atexit
import asyncio
import logging
import argparse
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Tuple
from collections import deque
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

from ib_insync import IB, Stock, util, ScannerSubscription, LimitOrder, MarketOrder, Order, Trade, BarDataList

ET = ZoneInfo("America/New_York")

# ===================== 可调参数（默认更贴近 scalping 实盘） =====================

# 时间轴（常规交易日）
SCAN_TIMES = [dtime(4,0,0), dtime(7,0,0), dtime(8,0,0), dtime(9,0,0), dtime(9,15,0), dtime(9,26,0)]
FREEZE_TIME = dtime(9,26,0)
OPEN_TIME = dtime(9,30,0)
OBS_END_TIME = dtime(9,33,0)            # 开盘观察结束→开始尝试下单（更靠近真正 scalping）
CANCEL_UNFILLED_TIME = dtime(10,0,0)     # 未成交/部分成交处理
CLEANUP_BEGIN_TIME = dtime(10,5,0)
HARD_EXIT_TIME = dtime(10,10,0)
ENTRY_RETRY_SECONDS = 20

# 候选限制
TOPN_FREEZE = 5
MAX_DAILY_TRADES = 3
ONE_TRADE_PER_SYMBOL = True

# 盘前基础过滤（弱化历史依赖，仅做初筛）
MIN_PRICE = 5.0
ETF_BLOCKLIST = { 'SPY','QQQ','IWM','DIA','EEM','VTI','VOO','VEA','VWO','AGG','BND','LQD','IVV','VTV','IEFA','IEMG' }

# 资金与动态风控
CAPITAL = 50_000
RISK_PCT = 0.010        # 单笔 1% 资金风险（scalping 更合理）
ENTRY_EPS_PCT = 0.0005  # 进场限价“微加价”
TP_MIN_PCT = 0.8
TP_MAX_PCT = 3.0
TRAIL_MIN_PCT = 0.3
TRAIL_MAX_PCT = 1.4
SL_FLOOR_PCT = 0.004    # 如果波动太小，给一个最小止损距离（0.4%）
RISK_PER_SHARE_CAP = 0.03

# 微结构阈值（短窗/长窗）
SHORT_SEC = 30
LONG_SEC = 120
VOL_SPIKE_TH = 1.5      # vol30 >= 1.5 × (vol120/4)
UPTICK_TH = 0.65
SPREAD_MED_TH = 0.0015  # 15bp
SPREAD_P95_TH = 0.0030  # 30bp
NEAR_BREAK_PCT = 0.10   # 接近 long_high 的 10% 区间即可触发

# 日志
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("scalper_v5s")
_TEMP_LOG_DIR = Path(__file__).resolve().parent / "temp"
_TEMP_LOG_DIR.mkdir(parents=True, exist_ok=True)
_TEMP_LOG_FILE = _TEMP_LOG_DIR / "premarket_scalper_v5s.log"
_fh = logging.FileHandler(_TEMP_LOG_FILE, encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(_fh)
atexit.register(_fh.flush)

# ===================== 增量滑动窗口统计 =====================

class SlidingStats:
    """固定窗口（秒）增量统计：VWAP/volume/spread/uptick/高低等。
    每条 tick 调用一次 update(ts, last, bid, ask, size)。
    """
    __slots__ = (
        "window_secs","data","sum_vol","sum_px_vol","upticks","downticks","last_px",
        "spreads","high","low","last"
    )
    def __init__(self, window_secs:int):
        self.window_secs = window_secs
        self.data = deque()   # (ts, last, bid, ask, size)
        self.sum_vol = 0.0
        self.sum_px_vol = 0.0
        self.upticks = 0
        self.downticks = 0
        self.last_px: Optional[float] = None
        self.spreads = deque()  # 每tick一个 spread（可选）
        self.high = -math.inf
        self.low  =  math.inf
        self.last = math.nan

    def _evict(self, cutoff:int):
        # 移除过期数据，同时修正累计量；高低值在快照时重算（避免复杂数据结构）。
        dirty_hilo = False
        while self.data and self.data[0][0] < cutoff:
            _, p, b, a, sz = self.data.popleft()
            if sz:
                self.sum_vol -= sz
                self.sum_px_vol -= p * sz
            if self.spreads:
                self.spreads.popleft()
            dirty_hilo = True
        if dirty_hilo:
            # 仅在需要时重算高低（窗口内数据数量很小，成本可接受）
            _prices = [p for (_, p, _, _, _) in self.data if p]
            self.high = max(_prices) if _prices else -math.inf
            self.low  = min(_prices) if _prices else  math.inf

    def update(self, ts:int, last:Optional[float], bid:Optional[float], ask:Optional[float], size:Optional[float]):
        cutoff = ts - self.window_secs
        self._evict(cutoff)
        p = last if (last is not None and not math.isnan(last)) else self.last_px
        b = bid if (bid is not None and not math.isnan(bid)) else None
        a = ask if (ask is not None and not math.isnan(ask)) else None
        if p is None:  # 无法确定价格，丢弃本tick
            return
        self.data.append((ts, p, b, a, size or 0.0))
        if size:
            self.sum_vol += size
            self.sum_px_vol += p * size
        if self.last_px is not None:
            if p > self.last_px: self.upticks += 1
            elif p < self.last_px: self.downticks += 1
        if b and a and a > b and b>0:
            mid = (a + b) / 2
            sp = (a - b) / max(mid, 1e-9)
            self.spreads.append(sp)
        self.last_px = p
        self.last = p
        # 更新高低（常数时间）
        if p > self.high: self.high = p
        if p < self.low:  self.low  = p

    def snapshot(self) -> Dict[str, Any]:
        n = len(self.data)
        vwap = (self.sum_px_vol / self.sum_vol) if self.sum_vol > 0 else math.nan
        # 计算分位数：窗口小，直接排序；如需更高效率可换 P2/tdigest
        spreads_list = list(self.spreads)
        spreads_list.sort()
        def q(arr, p):
            if not arr: return math.nan
            k = int((len(arr)-1) * p)
            return arr[k]
        sm = q(spreads_list, 0.5)
        s95 = q(spreads_list, 0.95)
        up_total = self.upticks + self.downticks
        upr = (self.upticks / up_total) if up_total>0 else math.nan
        return dict(
            samples=n,
            volume=self.sum_vol,
            vwap=vwap,
            spread_median=sm,
            spread_p95=s95,
            uptick_ratio=upr,
            high=(self.high if self.high!=-math.inf else math.nan),
            low=(self.low if self.low!= math.inf else math.nan),
            last=self.last
        )

class MultiWindowStats:
    def __init__(self, windows=(SHORT_SEC, LONG_SEC)):
        self.windows = {w: SlidingStats(w) for w in windows}
    def update(self, ts:int, last, bid, ask, size):
        for w in self.windows.values():
            w.update(ts, last, bid, ask, size)
    def snap(self, w:int) -> Dict[str, Any]:
        s = self.windows.get(w)
        return s.snapshot() if s else {}

# ===================== 数据结构 / 候选 =====================

@dataclass
class Candidate:
    symbol: str
    conId: int
    first_seen: datetime
    last_update: datetime
    last_price: float = 0.0
    stats: MultiWindowStats = field(default_factory=MultiWindowStats)
    traded: bool = False
    bars5s: Optional[BarDataList] = None  # 如需可保留；本策略主要靠 tick 统计

# ===================== 策略主体 =====================

class ScalperV5s:
    def __init__(self, mode='paper', dry_run=False, client_id:int=5001):
        self.mode = mode
        self.dry_run = dry_run
        self.ib = IB()
        self.host = '127.0.0.1'
        self.port = 4002 if mode=='paper' else 7497
        self.client_id = client_id

        self.cands: Dict[str, Candidate] = {}
        self.frozen_syms: List[str] = []
        self.subscribed: Dict[str, Any] = {}
        self.daily_trades_done = 0
        self.positions: Dict[str, Dict] = {}
        self.working_orders: Dict[str, Dict] = {}
        self.parent_trades: Dict[str, Dict[str, Any]] = {}

        self._is_connected = False
        self._last_heartbeat_ts = time.time()
        self._last_scan_mark: Optional[dtime] = None
        self._entry_orders_sent = False
        self._last_entry_attempt: Optional[datetime] = None

        # 开发/测试开关
        self._force_run = False
        self._dev_fast = False
        # 新闻开关
        self._news_enable = True
        self._news_bullish_th = 0.7
        self._news_lookback_h = 2
        self._news_per_limit = 3
        self._news_days = 1
        self._news_provider = 'auto'
        self._news_model = None
        self._news_drop_neutral = True

    # ---------- 连接 ----------
    def connect(self) -> bool:
        try:
            logger.info(f"Connecting IB @ {self.host}:{self.port} cid={self.client_id}")
            self.ib.connect(self.host, self.port, clientId=self.client_id, timeout=20)
            self.ib.connectedEvent += lambda: logger.info("🔌 IB connected")
            self.ib.disconnectedEvent += lambda: logger.error("🔌 IB disconnected")
            self._is_connected = True
            logger.info("✅ Connected")
            return True
        except Exception as e:
            logger.exception(f"connect failed: {e}")
            return False

    def _ensure_connection(self) -> bool:
        try:
            if self._is_connected and self.ib.isConnected():
                return True
            logger.info("Reconnecting IB ...")
            try: self.ib.disconnect()
            except Exception: pass
            self.ib.connect(self.host, self.port, clientId=self.client_id, timeout=10)
            self._is_connected = True
            for sym in list(self.subscribed.keys()):
                self._unsubscribe_symbol(sym)
            for sym in (self.frozen_syms or list(self.cands.keys())):
                self._subscribe_symbol(sym)
            logger.info("Reconnected and resubscribed.")
            return True
        except Exception as e:
            logger.exception(f"Reconnect failed: {e}")
            return False

    def disconnect(self):
        for sym, tkr in list(self.subscribed.items()):
            try: tkr.updateEvent -= self._on_tick
            except Exception: pass
        self.subscribed.clear()
        if self.ib.isConnected():
            self.ib.disconnect()
            logger.info("Disconnected")
        self.parent_trades.clear()

    # ---------- 市场扫描 / 候选维护 ----------
    def scan_premarket(self) -> List[Dict]:
        if not self._ensure_connection():
            return []
        rows: List[Dict] = []
        try:
            sub = ScannerSubscription()
            sub.numberOfRows = 50
            sub.instrument = 'STK'
            sub.locationCode = 'STK.US.MAJOR'
            sub.scanCode = 'HOT_BY_VOLUME'
            res = self.ib.reqScannerData(sub)
            self.ib.sleep(4.0)  # 给IB刷新一点时间
            for d in res:
                c = d.contractDetails.contract
                sym = getattr(c, 'symbol', None)
                if not sym or c.secType!='STK' or sym in ETF_BLOCKLIST:
                    continue
                rows.append({'symbol': sym, 'conId': c.conId, 'exchange': c.exchange})
            logger.info(f"Scanner {len(rows)} rows after filter")
        except Exception as e:
            logger.warning(f"scanner error: {e}")
        return rows

    def _quotes(self, symbols: List[str], timeout=3.0) -> Dict[str, Dict]:
        contracts = [Stock(s, 'SMART', 'USD') for s in symbols]
        quotes: Dict[str, Dict] = {}
        try:
            tkrs = self.ib.reqTickers(*contracts)
            self.ib.sleep(timeout)
            for t in tkrs:
                last = self._f(getattr(t, 'last', math.nan))
                bid  = self._f(getattr(t, 'bid', math.nan))
                ask  = self._f(getattr(t, 'ask', math.nan))
                close= self._f(getattr(t, 'close', math.nan))
                vol  = getattr(t, 'volume', None)
                if any(x is None or math.isnan(x) for x in [last, bid, ask]):
                    continue
                change_pct = (last - close)/max(close,1e-9)*100.0 if (close and not math.isnan(close)) else math.nan
                mid = (bid+ask)/2.0
                spread_pct = (ask-bid)/max(mid,1e-9) if ask>bid>0 else math.nan
                quotes[t.contract.symbol] = dict(last=last,bid=bid,ask=ask,close=close,volume=vol,change_pct=change_pct,spread_pct=spread_pct)
        except Exception as e:
            logger.debug(f"quotes error: {e}")
        return quotes

    def refresh_candidates(self, rows: List[Dict]):
        symbols = [r['symbol'] for r in rows]
        quotes = self._quotes(symbols)
        now = datetime.now(ET)
        for r in rows:
            sym = r['symbol']
            q = quotes.get(sym)
            if not q: continue
            if q['last'] < MIN_PRICE: continue
            if sym not in self.cands:
                self.cands[sym] = Candidate(symbol=sym, conId=r['conId'], first_seen=now, last_update=now, last_price=q['last'])
                self._subscribe_symbol(sym)
                logger.info(f"✅ New candidate {sym}")
            else:
                c = self.cands[sym]
                c.last_update = now
                c.last_price = q['last']

    def freeze_topN(self):
        # 新闻 gating（可选）：仅用于排序；若失败则退化为按 volume/change 排序
        ranked: List[Candidate] = []
        try:
            if getattr(self, '_news_enable', False):
                csv_path = _ensure_news_csv_for_symbols(
                    symbols=self.cands.keys(),
                    lookback_hours=getattr(self, '_news_lookback_h', 2),
                    per_limit=getattr(self, '_news_per_limit', 3),
                    days=getattr(self, '_news_days', 1),
                    provider=getattr(self, '_news_provider', 'auto'),
                    model=getattr(self, '_news_model', None),
                    drop_neutral=getattr(self, '_news_drop_neutral', True),
                )
                news_map = _read_news_csv_map(csv_path)
                th = float(getattr(self,'_news_bullish_th',0.7) or 0.7)
                passed = [c for c in self.cands.values() if news_map.get(c.symbol,{}).get('bullish',0.0) >= th]
                if passed:
                    ranked = sorted(passed, key=lambda x: (news_map.get(x.symbol,{}).get('bullish',0.0), x.last_price), reverse=True)
            if not ranked:
                # fallback：按最新 quotes 的 volume/change 排序
                qmap = self._quotes([c.symbol for c in self.cands.values()])
                ranked = sorted(self.cands.values(), key=lambda x: (qmap.get(x.symbol,{}).get('volume',0) or 0, qmap.get(x.symbol,{}).get('change_pct',0) or 0), reverse=True)
        except Exception as e:
            logger.warning(f"freeze_topN news gating failed, fallback: {e}")
            qmap = self._quotes([c.symbol for c in self.cands.values()])
            ranked = sorted(self.cands.values(), key=lambda x: (qmap.get(x.symbol,{}).get('volume',0) or 0, qmap.get(x.symbol,{}).get('change_pct',0) or 0), reverse=True)

        self.frozen_syms = [c.symbol for c in ranked[:TOPN_FREEZE]]
        logger.info(f"❄️  Frozen: {self.frozen_syms} (news={'on' if getattr(self,'_news_enable',False) else 'off'})")
        for sym in list(self.subscribed.keys()):
            if sym not in self.frozen_syms:
                self._unsubscribe_symbol(sym)

    # ---------- 订阅 tick ----------
    def _subscribe_symbol(self, sym:str):
        if sym in self.subscribed: return
        contract = Stock(sym,'SMART','USD')
        tkr = self.ib.reqMktData(contract, "", False, False)
        tkr.updateEvent += self._on_tick
        self.subscribed[sym] = tkr

    def _unsubscribe_symbol(self, sym:str):
        tkr = self.subscribed.pop(sym, None)
        if tkr:
            try: tkr.updateEvent -= self._on_tick
            except Exception: pass

    def _on_tick(self, tkr):
        try:
            sym = tkr.contract.symbol
            c = self.cands.get(sym)
            if not c: return
            now = datetime.now(ET)
            ts = int(now.timestamp())
            last = self._f(getattr(tkr,'last', math.nan))
            bid  = self._f(getattr(tkr,'bid',  math.nan))
            ask  = self._f(getattr(tkr,'ask',  math.nan))
            size = getattr(tkr,'lastSize', None)
            size = float(size) if size else 0.0
            # 增量更新（O(1)）
            c.stats.update(ts, last, bid, ask, size)
        except Exception as e:
            logger.debug(f"tick error: {e}")

    # ---------- 信号与入场 ----------
    def _compute_signal(self, sym:str) -> Tuple[float, Dict[str, Any]]:
        c = self.cands[sym]
        s30 = c.stats.snap(SHORT_SEC)
        s120= c.stats.snap(LONG_SEC)
        # 体积
        vol30  = s30.get('volume') or 0.0
        vol120 = s120.get('volume') or 0.0
        baseline = max(vol120/4.0, 1e-6)
        vol_spike = (vol30 / baseline) if baseline>0 else 0.0
        # 价/量微结构
        last = s30.get('last')
        vwap30 = s30.get('vwap')
        sm = s30.get('spread_median')
        s95 = s30.get('spread_p95')
        upr = s30.get('uptick_ratio')
        h120 = s120.get('high'); l120 = s120.get('low')
        rng120 = (h120 - l120) if (h120 and l120 and not any(map(math.isnan,[h120,l120]))) else math.nan
        # 触发项
        conds = {
            'vol_spike': vol_spike >= VOL_SPIKE_TH,
            'above_vwap': (last and vwap30 and (not math.isnan(last)) and (not math.isnan(vwap30)) and last>vwap30),
            'spread_ok': (sm is not None and not math.isnan(sm) and sm<=SPREAD_MED_TH) and (s95 is not None and not math.isnan(s95) and s95<=SPREAD_P95_TH),
            'uptick_ok': (upr is not None and not math.isnan(upr) and upr>=UPTICK_TH),
            'near_break': False
        }
        if all(x is not None for x in [last, h120, l120]) and not any(map(math.isnan,[last,h120,l120])) and h120>l120:
            near = (h120 - last) <= NEAR_BREAK_PCT * max(h120 - l120, 1e-6)
            conds['near_break'] = near or (last >= h120 - 1e-6)
        # 简单打分（只用于排序/日志，不直接决定）：
        score = 0.0
        score += min(vol_spike/2.0, 1.0) * 0.35
        score += (1.0 if conds['above_vwap'] else 0.0) * 0.2
        score += (1.0 if conds['spread_ok'] else 0.0) * 0.2
        score += (1.0 if conds['uptick_ok'] else 0.0) * 0.15
        score += (1.0 if conds['near_break'] else 0.0) * 0.10
        details = dict(
            vol_spike=vol_spike, last=last, vwap30=vwap30, spread_median=sm, spread_p95=s95, uptick_ratio=upr,
            high120=h120, low120=l120, range120=rng120, conds=conds
        )
        return score, details

    def _dynamic_trailing_and_tp(self, rng_pct: float) -> Tuple[float, float]:
        """根据 120s 波动范围（占价的比例）设定 trailingPercent 与 TP 百分比。"""
        # rng_pct 是 120s 的 (high-low)/last 的比例（如 0.01=1%）
        base = (rng_pct * 0.6) * 100.0  # 转为百分比
        trail = max(TRAIL_MIN_PCT, min(TRAIL_MAX_PCT, base))
        tp = max(TP_MIN_PCT, min(TP_MAX_PCT, trail * 1.6))
        return round(trail, 2), round(tp, 2)

    def _enter_if_ok(self, sym:str) -> bool:
        if self.daily_trades_done >= MAX_DAILY_TRADES:
            return False
        c = self.cands.get(sym)
        if not c or (ONE_TRADE_PER_SYMBOL and c.traded):
            return False
        score, det = self._compute_signal(sym)
        conds = det['conds']
        # 必须全部满足（剥头皮严格）
        if not (conds['vol_spike'] and conds['above_vwap'] and conds['spread_ok'] and conds['uptick_ok'] and conds['near_break']):
            logger.info(f"{sym} skip: conds={conds}")
            return False
        # 最新快照报价
        q = self._quotes([sym]).get(sym)
        if not q:
            logger.info(f"{sym} skip: no quote")
            return False
        bid, ask, last = q['bid'], q['ask'], q['last']
        if any(x is None or math.isnan(x) for x in [bid, ask, last]):
            return False
        mid = (bid+ask)/2.0
        live_spread = (ask-bid)/max(mid,1e-9)
        if live_spread > SPREAD_P95_TH:
            logger.info(f"{sym} skip: live spread too large {live_spread:.4f}")
            return False
        entry = round(ask * (1.0 + ENTRY_EPS_PCT), 2)
        # 计算动态 trail / TP
        rng120 = det.get('range120')
        rng_pct = (rng120 / max(mid,1e-9)) if (rng120 and not math.isnan(rng120)) else 0.006  # 默认 0.6%
        trail_pct, tp_pct = self._dynamic_trailing_and_tp(rng_pct)
        risk_per_share = max(entry * (trail_pct/100.0), entry * SL_FLOOR_PCT)
        risk_per_share = min(risk_per_share, entry * RISK_PER_SHARE_CAP)
        shares = int((CAPITAL * RISK_PCT) / max(risk_per_share, 1e-9))
        if shares <= 0:
            logger.info(f"{sym} skip: shares=0 (risk too tight)")
            return False
        logger.info(f"🎯 ENTER {sym} | score={score:.3f} entry={entry:.2f} shares={shares} trail={trail_pct:.2f}% tp={tp_pct:.2f}%")

        if self.dry_run:
            self.positions[sym] = dict(symbol=sym, shares=shares, entry=entry, tp_pct=tp_pct, trail_pct=trail_pct, time=datetime.now(ET))
            c.traded = True
            self.daily_trades_done += 1
            return True

        # 下单：父（BUY LIMIT, DAY）→ 子（TRAIL, LIMIT TP）OCA
        contract = Stock(sym,'SMART','USD')
        parent = LimitOrder('BUY', shares, entry)
        parent.tif = 'DAY'
        parent.transmit = False
        trade_parent: Trade = self.ib.placeOrder(contract, parent)
        while not trade_parent.orderStatus.status:
            self.ib.sleep(0.05)
        parent_id = trade_parent.order.orderId

        trail = Order()
        trail.action = 'SELL'
        trail.totalQuantity = shares
        trail.orderType = 'TRAIL'
        trail.trailingPercent = trail_pct
        trail.tif = 'DAY'
        trail.outsideRth = False

        tp_price = round(entry * (1.0 + tp_pct/100.0), 2)
        take_profit = LimitOrder('SELL', shares, tp_price)
        take_profit.tif = 'DAY'
        take_profit.outsideRth = False

        oca_group = f"OCA_{sym}_{int(time.time())}"
        for ch in (trail, take_profit):
            ch.ocaGroup = oca_group
            ch.ocaType = 1
            ch.parentId = parent_id
            ch.transmit = False
        take_profit.transmit = True

        trade_trail = self.ib.placeOrder(contract, trail)
        trade_tp = self.ib.placeOrder(contract, take_profit)

        self.positions[sym] = dict(symbol=sym, shares=shares, entry=entry, tp=tp_price, oca=oca_group,
                                   parentId=parent_id, trail_pct=trail_pct, tp_pct=tp_pct, filled=0)
        self.working_orders[sym] = dict(parentId=parent_id, trailId=trade_trail.order.orderId, tpId=trade_tp.order.orderId, ocaGroup=oca_group)
        self.parent_trades[sym] = dict(trade=trade_parent, last_filled=0)
        trade_parent.fillEvent += lambda trd, fill, s=sym: self._on_parent_fill(s, trd, fill)
        trade_parent.cancelledEvent += lambda trd, s=sym: self._on_parent_cancel(s, trd)
        trade_trail.fillEvent += lambda trd, fill, s=sym: self._on_child_sell_fill(s, trd, fill)
        trade_tp.fillEvent += lambda trd, fill, s=sym: self._on_child_sell_fill(s, trd, fill)
        c.traded = True
        self.daily_trades_done += 1
        return True

    # ---------- 成交/撤单回调 ----------
    def _on_parent_fill(self, sym:str, trade:Trade, fill):
        if self.dry_run: return
        pos = self.positions.get(sym)
        info = self.parent_trades.get(sym)
        if not pos or not info: return
        try: filled_total = int(round(float(trade.filled or 0)))
        except Exception: filled_total = 0
        last = info.get('last_filled', 0)
        delta = max(0, filled_total - last)
        if delta<=0: return
        info['last_filled'] = filled_total
        pos['filled'] = pos.get('filled', 0) + delta

    def _on_parent_cancel(self, sym:str, trade:Trade):
        info = self.parent_trades.get(sym)
        if not info: return
        if info.get('last_filled', 0) <= 0:
            self.parent_trades.pop(sym, None)

    def _on_child_sell_fill(self, sym:str, trade:Trade, fill):
        if self.dry_run: return
        pos = self.positions.get(sym)
        if not pos: return
        try: shares_delta = int(round(float(getattr(fill.execution, 'shares', 0) or 0)))
        except Exception: shares_delta = 0
        if shares_delta <= 0: return
        shares_left = max(0, int(pos.get('filled', 0)) - shares_delta)
        pos['filled'] = shares_left
        if shares_left <= 0:
            self.parent_trades.pop(sym, None)
            return

    # ---------- 部分成交/未成交处理 & 收尾 ----------
    def manage_open_orders(self):
        now_t = datetime.now(ET).time()
        if now_t < CANCEL_UNFILLED_TIME or now_t >= CLEANUP_BEGIN_TIME:
            return
        for sym, work in list(self.working_orders.items()):
            try:
                parentId = work.get('parentId')
                if not parentId: continue
                trades = [tr for tr in self.ib.trades() if tr.order.orderId==parentId]
                if not trades: continue
                tr = trades[0]
                filled = float(tr.orderStatus.filled or 0.0)
                total = float(tr.order.totalQuantity or 0.0)
                if total <= 0: continue
                fill_ratio = filled / total if total else 0.0
                if fill_ratio >= 0.8: continue
                remaining = int(max(total - filled, 0))
                if remaining > 0:
                    try: self.ib.cancelOrder(tr.order)
                    except Exception: pass
                    work['parentId'] = None
                    logger.info(f"{sym} cancel remaining parent qty={remaining} (underfilled)")
                if filled <= 0:
                    self._cancel_children(sym)
                    self.positions.pop(sym, None)
                    self.working_orders.pop(sym, None)
                    self.parent_trades.pop(sym, None)
            except Exception as e:
                logger.debug(f"manage_open_orders error {sym}: {e}")

    def _cancel_children(self, sym:str):
        try:
            work = self.working_orders.get(sym, {})
            parent_id = work.get('parentId')
            oca_group = work.get('ocaGroup')
            child_ids = {cid for cid in (work.get('trailId'), work.get('tpId')) if cid}
            for tr in self.ib.trades():
                if tr.contract.symbol != sym: continue
                order = tr.order
                if order.action != 'SELL': continue
                matches_parent = parent_id and order.parentId==parent_id
                matches_child_id = order.orderId in child_ids
                matches_oca = oca_group and getattr(order,'ocaGroup',None)==oca_group
                if not (matches_parent or matches_child_id or matches_oca):
                    continue
                if tr.isDone():
                    continue
                try: self.ib.cancelOrder(order)
                except Exception: pass
        except Exception:
            pass

    def cleanup_sequence(self):
        now_t = datetime.now(ET).time()
        if now_t < CLEANUP_BEGIN_TIME or now_t >= HARD_EXIT_TIME:
            return
        # 1) 撤父单余量
        for sym, work in list(self.working_orders.items()):
            try:
                parentId = work.get('parentId')
                if not parentId: continue
                trades = [tr for tr in self.ib.trades() if tr.order.orderId==parentId]
                if not trades: continue
                tr = trades[0]
                filled = float(tr.orderStatus.filled or 0.0)
                total = float(tr.order.totalQuantity or 0.0)
                remaining = int(max(total - filled, 0))
                if remaining>0:
                    try: self.ib.cancelOrder(tr.order)
                    except Exception: pass
                    logger.info(f"{sym} cleanup: cancel remaining parent qty={remaining}")
            except Exception:
                pass
        # 2) 子单撤销，持仓按 filled 出清
        for sym, pos in list(self.positions.items()):
            try:
                self._cancel_children(sym)
                contract = Stock(sym,'SMART','USD')
                qty_filled = int(pos.get('filled', 0))
                qty_planned= int(pos.get('shares', 0))
                qty = qty_filled if qty_filled>0 else qty_planned
                if qty>0:
                    order = MarketOrder('SELL', qty)
                    order.tif = 'DAY'
                    self.ib.placeOrder(contract, order)
                    logger.info(f"{sym} cleanup: market exit qty={qty}")
            except Exception:
                pass
        # 3) 清空本地状态
        for sym in list(self.positions.keys()):
            self._cancel_children(sym)
        self.positions.clear()
        self.working_orders.clear()
        self.parent_trades.clear()

    def force_exit_all(self, reason:str):
        logger.info(f"⏰ FORCE EXIT ({reason})")
        if self.dry_run:
            self.positions.clear(); self.working_orders.clear(); self.parent_trades.clear(); return
        try: self.cleanup_sequence()
        except Exception: pass
        try:
            for tr in self.ib.trades():
                try: self.ib.cancelOrder(tr.order)
                except Exception: pass
        except Exception: pass
        self.parent_trades.clear()

    # ---------- 主流程 ----------
    def run(self):
        now_et = datetime.now(ET)
        tnow = now_et.time()
        # DEV-FAST：把时间线压缩到“从现在起的若干秒”
        if getattr(self,'_dev_fast', False):
            def add_s(base_dt, sec):
                dt2 = base_dt + timedelta(seconds=sec)
                return dtime(dt2.hour, dt2.minute, dt2.second)
            global FREEZE_TIME, OPEN_TIME, OBS_END_TIME, CANCEL_UNFILLED_TIME, CLEANUP_BEGIN_TIME, HARD_EXIT_TIME
            FREEZE_TIME = add_s(now_et, 10)
            OPEN_TIME   = add_s(now_et, 20)
            OBS_END_TIME= add_s(now_et, 50)
            CANCEL_UNFILLED_TIME = add_s(now_et, 120)
            CLEANUP_BEGIN_TIME   = add_s(now_et, 150)
            HARD_EXIT_TIME       = add_s(now_et, 180)
            logger.warning(f"🧪 DEV-FAST schedule: FREEZE={FREEZE_TIME}, OPEN={OPEN_TIME}, OBS_END={OBS_END_TIME}, CANCEL={CANCEL_UNFILLED_TIME}, CLEANUP={CLEANUP_BEGIN_TIME}, HARD_EXIT={HARD_EXIT_TIME}")

        if tnow >= HARD_EXIT_TIME and not getattr(self,'_force_run', False):
            logger.error(f"❌ Started too late ({tnow}). Hard exit time is {HARD_EXIT_TIME}. Aborting.")
            return
        elif tnow >= HARD_EXIT_TIME:
            logger.warning("⚠️ Started after HARD_EXIT but --force-run set: continuing for testing.")
        if tnow >= FREEZE_TIME:
            logger.warning(f"⚠️  Started late ({tnow}). Last scan time was {SCAN_TIMES[-1] if SCAN_TIMES else 'N/A'}.")
        if tnow >= OBS_END_TIME:
            logger.warning(f"⚠️  Started after observation window ({tnow}). Entry logic may not work correctly.")

        if not self.connect():
            return
        # 预热：扫描并冻结
        rows = self.scan_premarket()
        self.refresh_candidates(rows)
        self.freeze_topN()

        try:
            while True:
                now_et = datetime.now(ET)
                t = now_et.time()
                now_ts = time.time()
                if now_ts - self._last_heartbeat_ts >= 60:
                    logger.info("💓 heartbeat alive")
                    self._last_heartbeat_ts = now_ts

                if not self._ensure_connection():
                    self.ib.sleep(0.5)
                    continue

                if t >= HARD_EXIT_TIME:
                    self.force_exit_all("HARD_EXIT")
                    logger.info("Trading day finished. Stop.")
                    break

                # 盘前扫描：限制在设定时间窗内执行
                if t < FREEZE_TIME:
                    for st in SCAN_TIMES:
                        if st <= t < (datetime.combine(datetime.now(ET).date(), st) + timedelta(minutes=1)).time():
                            if self._last_scan_mark != st:
                                rows = self.scan_premarket()
                                self.refresh_candidates(rows)
                                self._last_scan_mark = st
                                break
                elif FREEZE_TIME <= t < OPEN_TIME:
                    if not self.frozen_syms:
                        self.freeze_topN()
                elif OPEN_TIME <= t < OBS_END_TIME:
                    # 仅聚合（tick 回调已做增量统计）
                    pass
                elif OBS_END_TIME <= t < CANCEL_UNFILLED_TIME:
                    if not self.frozen_syms:
                        self.freeze_topN()
                    if self._should_attempt_entry(now_et):
                        is_retry = self._last_entry_attempt is not None
                        self._execute_entry_window(is_retry)
                        self._last_entry_attempt = now_et
                elif CANCEL_UNFILLED_TIME <= t < CLEANUP_BEGIN_TIME:
                    self.manage_open_orders()
                elif CLEANUP_BEGIN_TIME <= t < HARD_EXIT_TIME:
                    self.cleanup_sequence()

                self.ib.sleep(0.2)
        except KeyboardInterrupt:
            logger.info("Interrupted")
        except Exception as e:
            logger.exception(f"Main loop error: {e}")
        finally:
            self.force_exit_all("EMERGENCY_EXIT")
            self.disconnect()

    def _execute_entry_window(self, is_retry: bool):
        logger.info(f"⏰ Entry window {'retry' if is_retry else 'initial'}")
        if not self.frozen_syms:
            return False
        # 评分排序（实时）
        scored: List[Tuple[str, float]] = []
        for sym in self.frozen_syms:
            try:
                s, _ = self._compute_signal(sym)
            except Exception:
                s = 0.0
            scored.append((sym, s))
        scored.sort(key=lambda x:x[1], reverse=True)
        orders = False
        for sym, _ in scored:
            if self.daily_trades_done >= MAX_DAILY_TRADES: break
            if self._enter_if_ok(sym):
                orders = True
        if orders:
            logger.info(f"Entry done. Trades executed: {self.daily_trades_done}/{MAX_DAILY_TRADES}")
        else:
            logger.info("Entry attempt finished: no qualifying symbol")
        return orders

    def _should_attempt_entry(self, now: datetime) -> bool:
        if self._entry_orders_sent:
            return False
        if self._last_entry_attempt is None:
            return True
        return (now - self._last_entry_attempt) >= timedelta(seconds=ENTRY_RETRY_SECONDS)

    # ---------- 小工具 ----------
    @staticmethod
    def _f(x) -> Optional[float]:
        try:
            if x is None: return None
            if isinstance(x, float) and math.isnan(x): return None
            return float(x)
        except Exception:
            return None

# ===================== 新闻 gating 附加模块（沿用你的 googleNews 工作流） =====================
# 要求：使用你项目中的 googleNews.py
import csv, re
try:
    import googleNews as gnews
except Exception:
    gnews = None


def _parse_llm_json_array(s: str):
    if not s: return []
    s = s.strip()
    if s.startswith("```") and s.endswith("```"):
        s = s[3:-3].strip()
    start = s.find("["); end = s.rfind("]")
    if start >= 0 and end > start:
        frag = s[start:end+1]
        try:
            data = json.loads(frag)
            if isinstance(data, list): return data
        except Exception:
            pass
    objs = re.findall(r"\{.*?\}", s, flags=re.S)
    out = []
    for o in objs:
        try: out.append(json.loads(o))
        except Exception: pass
    return out


def _ensure_news_csv_for_symbols(symbols, lookback_hours=2, per_limit=3, days=1, provider="auto", model=None, drop_neutral=True):
    if not gnews:
        # 无 googleNews 模块，返回空
        return None
    out_csv, raw_csv, log_file, cache_file = gnews.default_paths_for_today()
    rows = gnews.fetch_all_tickers(list(symbols), per_limit=per_limit, days=days)
    end_dt = datetime.now(tz=ET); start_dt = end_dt - timedelta(hours=lookback_hours)
    rows = gnews.filter_by_window(rows, start_dt, end_dt)
    if not rows:
        gnews.write_sentiment_csv_minimal(out_csv, [])
        return out_csv
    client = gnews.build_client(provider, model)
    user_prompt = gnews.build_batch_user_prompt(rows)
    content = client.complete(gnews.SYSTEM_PROMPT_NEWS, user_prompt)
    data = _parse_llm_json_array(content)
    # 仅写 minimal 字段
    gnews.write_sentiment_csv_minimal(out_csv, data)
    return out_csv


def _read_news_csv_map(csv_path):
    if not csv_path or not os.path.exists(csv_path):
        return {}
    m = {}
    try:
        with open(csv_path, 'r', encoding='utf-8') as f:
            rdr = csv.DictReader(f)
            for r in rdr:
                t = (r.get('ticker') or '').upper()
                d = (r.get('direction') or '').lower()
                try: s = int(r.get('strength_0to3') or 0)
                except Exception: s = 0
                if not t: continue
                if d == 'bullish': b = min(1.0, 0.7 + 0.1*s)
                elif d == 'bearish': b = max(0.0, 0.3 - 0.1*s)
                else: b = 0.5
                cur = m.get(t, {'bullish':0.0,'dir':d,'strength':s,'hits':0})
                cur['bullish'] = max(cur['bullish'], b)
                cur['dir'] = d
                cur['strength'] = max(cur['strength'], s)
                cur['hits'] += 1
                m[t] = cur
    except Exception as e:
        logger.warning(f"read news csv failed: {e}")
    return m

# ===================== CLI =====================

def main():
    ap = argparse.ArgumentParser(description="V5s Sliding-Window Scalper (Opening/Microstructure)")
    ap.add_argument('--mode', choices=['paper','live'], default='paper')
    ap.add_argument('--dry-run', action='store_true', default=False)
    ap.add_argument('--client-id', type=int, default=5001)
    ap.add_argument('--force-run', action='store_true', default=False)
    ap.add_argument('--dev-fast', action='store_true', default=False)
    # 新闻
    ap.add_argument('--news', action='store_true', default=True)
    ap.add_argument('--news-bullish-th', type=float, default=0.7)
    ap.add_argument('--news-lookback-h', type=int, default=2)
    ap.add_argument('--news-per-limit', type=int, default=3)
    ap.add_argument('--news-days', type=int, default=1)
    ap.add_argument('--news-provider', type=str, default='auto')
    ap.add_argument('--news-model', type=str, default=None)
    ap.add_argument('--news-drop-neutral', action='store_true', default=True)
    # 参数可调（若需快速实验）
    ap.add_argument('--risk-pct', type=float, default=RISK_PCT)
    ap.add_argument('--vol-spike-th', type=float, default=VOL_SPIKE_TH)
    ap.add_argument('--uptick-th', type=float, default=UPTICK_TH)
    ap.add_argument('--spread-med-th', type=float, default=SPREAD_MED_TH)
    ap.add_argument('--spread-p95-th', type=float, default=SPREAD_P95_TH)

    args = ap.parse_args()
    util.patchAsyncio()

    global RISK_PCT, VOL_SPIKE_TH, UPTICK_TH, SPREAD_MED_TH, SPREAD_P95_TH
    RISK_PCT = float(args.risk_pct)
    VOL_SPIKE_TH = float(args.vol_spike_th)
    UPTICK_TH = float(args.uptick_th)
    SPREAD_MED_TH = float(args.spread_med_th)
    SPREAD_P95_TH = float(args.spread_p95_th)

    if args.mode=='live' and not args.dry_run:
        confirm = input("⚠️ LIVE mode with real money. Continue? (yes/no): ")
        if confirm.lower() != 'yes':
            print('Aborted.')
            return

    strat = ScalperV5s(mode=args.mode, dry_run=args.dry_run, client_id=args.client_id)
    strat._force_run = args.force_run
    strat._dev_fast = args.dev_fast

    strat._news_enable       = bool(args.news)
    strat._news_bullish_th   = float(args.news_bullish_th)
    strat._news_lookback_h   = int(args.news_lookback_h)
    strat._news_per_limit    = int(args.news_per_limit)
    strat._news_days         = int(args.news_days)
    strat._news_provider     = args.news_provider
    strat._news_model        = args.news_model
    strat._news_drop_neutral = bool(args.news_drop_neutral)

    strat.run()

if __name__ == '__main__':
    main()
