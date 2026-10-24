#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Premarket Momentum V4c — DAY-only, Open-Observation, Trailing Stop OCO
Author: ChatGPT (for Wei)
Last Updated: 2025-10-21

在 V4b 基础上新增：
- CLI: --force-run 跳过晚启动硬拦截，便于测试
- CLI: --dev-fast 将整日时间轴压缩为“从现在起几分钟”的演练模式
保留特性：
- 断线自愈：connected/disconnected 事件；_ensure_connection() 自动重连/重订阅
- 通用超时：_call_with_timeout()；scanner/reqTickers 加超时与重试
- 主循环心跳：每分钟输出 💓 heartbeat
"""

import os
import sys
import json
import math
import argparse
import time
import logging
import atexit
import asyncio
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo
from functools import partial
from dotenv import load_dotenv

from ib_insync import (
    IB, Stock, util, ScannerSubscription,
    LimitOrder, MarketOrder, Order, Trade, BarDataList, Option
)

# ====================== 配置参数 ======================
load_dotenv()

ET = ZoneInfo("America/New_York")

# 时间轴（常规交易日）
SCAN_TIMES = [
    dtime(4, 0, 0), dtime(5, 0, 0), dtime(6, 0, 0),
    dtime(7, 0, 0), dtime(7, 30, 0), dtime(8, 30, 0),
    dtime(9, 0, 0), dtime(9, 15, 0), dtime(9, 26, 0)
]
FREEZE_TIME = dtime(9, 26, 0)
OPEN_TIME = dtime(9, 30, 0)
OBS_END_TIME = dtime(10, 27, 0)      # 观察窗结束（开始下单）
CANCEL_UNFILLED_TIME = dtime(15, 40, 0)   # 未成交/部分成交处理
CLEANUP_BEGIN_TIME = dtime(15, 50, 0)     # 清理序列开始
HARD_EXIT_TIME = dtime(16, 00, 0)        # 硬退出
ENTRY_RETRY_SECONDS = 30

# 候选与交易上限
TOPN_FREEZE = 5
MAX_DAILY_TRADES = 3
ONE_TRADE_PER_SYMBOL = True

# 盘前基础过滤
MIN_PM_CHANGE_PCT = 2.0
MIN_PRICE = 5.0
MIN_MARKET_CAP = 5_000_000_000  # $5B
ETF_BLOCKLIST = {
    'SPY','QQQ','IWM','DIA','EEM','VTI','VOO','VEA','VWO','AGG','BND','LQD','IVV','VTV','IEFA','IEMG'
}

# 风险对冲
EDGE_BASELINE = 0.55
EDGE_HEDGE_TRIGGER = 0.68
EDGE_HEDGE_MAX = 0.92
OPTION_MIN_DTE = 7
OPTION_MAX_DTE = 45

# 观察窗阈值
MAX_SPREAD_PCT_OBS_MEDIAN = 0.0025  # 25bp
MAX_SPREAD_PCT_OBS_P95 = 0.0050     # 50bp
ABS_VOLUME_FALLBACK = 500_000
ABS_NOTIONAL_FALLBACK = 20_000_000

# 资金与风控
CAPITAL = 50_000
RISK_PCT = 0.005            # per trade
TRAILING_PCT = 1.8          # %
TP_PCT = 2.4                # %
LIMIT_EPS_TICKS = 0.0005    # +0.05%
FIXED_SL_FLOOR = 0.012      # 每股风险下限：1.2%
RISK_PER_SHARE_CAP = 0.03   # 每股风险上限：3%

# 观察窗打分权重
W_VOL = 0.3
W_SPREAD = 0.25
W_VWAP = 0.2
W_OR = 0.15
W_TAPE = 0.1

# 秒级环形缓冲
RING_SECONDS = 180
SPREAD_PCT_CAP = 0.05
EPS = 1e-9

# Vref 本地缓存（可用 env 覆盖）
_default_cache_path = os.path.join(Path.home(), ".cache", "premarket", "vref_cache.json")
VREF_CACHE_PATH = os.environ.get("VREF_CACHE_PATH", _default_cache_path)
Path(VREF_CACHE_PATH).parent.mkdir(parents=True, exist_ok=True)

# ====================== 日志器 ======================

try:
    from libs.logger import get_logger
    logger = get_logger(__name__)
except Exception:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logger = logging.getLogger("premarket_v4c")

_TEMP_LOG_DIR = Path(__file__).resolve().parent.parent / "temp"
_TEMP_LOG_DIR.mkdir(parents=True, exist_ok=True)
_TEMP_LOG_FILE = _TEMP_LOG_DIR / "premarketplusoption.log"


def _attach_temp_file_handler(target: logging.Logger) -> logging.Handler:
    for handler in target.handlers:
        if getattr(handler, "_premarket_temp_file", False):
            return handler
    fh = logging.FileHandler(_TEMP_LOG_FILE, encoding="utf-8")
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    fh._premarket_temp_file = True
    target.addHandler(fh)
    return fh


_temp_file_handler = _attach_temp_file_handler(logger)
atexit.register(_temp_file_handler.flush)


class _IBScannerCancelFilter(logging.Filter):
    """Drop the noisy scanner cancellation error IB emits after snapshot scans."""
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if "API scanner subscription cancelled" in msg and "Error 162" in msg:
            logger.debug("Ignoring expected IB scanner auto-cancel: %s", msg)
            return False
        return True


logging.getLogger("ib_insync.wrapper").addFilter(_IBScannerCancelFilter())

# ====================== 数据结构 ======================

@dataclass
class SecondBucket:
    ts: int
    hi: float = -math.inf
    lo: float = math.inf
    last: float = math.nan
    bid: float = math.nan
    ask: float = math.nan
    vol: float = 0.0
    vwap_num: float = 0.0
    vwap_den: float = 0.0
    upticks: int = 0
    downticks: int = 0
    spreads: List[float] = field(default_factory=list)

    def update(self, last: Optional[float], bid: Optional[float], ask: Optional[float],
               size: Optional[float], last_prev: Optional[float]):
        if last is not None and not math.isnan(last):
            self.last = last
            self.hi = max(self.hi, last)
            self.lo = min(self.lo, last)
        if bid is not None and not math.isnan(bid):
            self.bid = bid
        if ask is not None and not math.isnan(ask):
            self.ask = ask

        if size and last:
            self.vol += size
            self.vwap_num += last * size
            self.vwap_den += size

        if last_prev is not None and not math.isnan(last_prev) and last is not None:
            if last > last_prev + EPS:
                self.upticks += 1
            elif last < last_prev - EPS:
                self.downticks += 1

        if self.bid and self.ask and self.bid > 0 and self.ask > self.bid:
            mid = (self.bid + self.ask) / 2
            sp = (self.ask - self.bid) / max(mid, EPS)
            self.spreads.append(min(sp, SPREAD_PCT_CAP))


class RingBuffer:
    def __init__(self, seconds=RING_SECONDS):
        self.seconds = seconds
        self.buckets: Dict[int, SecondBucket] = {}
        self.last_price: Optional[float] = None

    def ingest_tick(self, t: datetime, last: Optional[float], bid: Optional[float],
                    ask: Optional[float], size: Optional[float]):
        epoch = int(t.timestamp())
        b = self.buckets.get(epoch)
        if b is None:
            b = SecondBucket(ts=epoch)
            self.buckets[epoch] = b
        b.update(last, bid, ask, size, self.last_price)
        if last is not None and not math.isnan(last):
            self.last_price = last
        self._prune(epoch)

    def _prune(self, now_epoch: int):
        cutoff = now_epoch - self.seconds
        for ts in list(self.buckets.keys()):
            if ts < cutoff:
                del self.buckets[ts]

    def window_stats(self, start_ts: int, end_ts: int) -> Dict[str, Any]:
        h = -math.inf
        l = math.inf
        vol = 0.0
        vnum = 0.0
        vden = 0.0
        ups = 0
        dws = 0
        all_spreads: List[float] = []
        last = math.nan

        if end_ts < start_ts:
            return {
                "high": math.nan, "low": math.nan, "last": math.nan,
                "volume": 0.0, "vwap": math.nan,
                "spread_median": math.nan, "spread_p95": math.nan, "spread_samples": 0,
                "uptick_ratio": math.nan
            }

        for ts in range(start_ts, end_ts + 1):
            b = self.buckets.get(ts)
            if not b:
                continue
            if not math.isnan(b.hi): h = max(h, b.hi)
            if not math.isnan(b.lo): l = min(l, b.lo)
            if not math.isnan(b.last): last = b.last
            vol += b.vol
            vnum += b.vwap_num
            vden += b.vwap_den
            ups += b.upticks
            dws += b.downticks
            all_spreads.extend(b.spreads)

        vwap = (vnum / vden) if vden > 0 else math.nan
        if h == -math.inf: h = math.nan
        if l == math.inf: l = math.nan

        all_spreads.sort()
        def pct(arr, p):
            if not arr:
                return math.nan
            k = int((len(arr)-1) * p/100.0)
            return arr[k]

        return {
            "high": h, "low": l, "last": last, "volume": vol, "vwap": vwap,
            "spread_median": pct(all_spreads, 50),
            "spread_p95": pct(all_spreads, 95),
            "spread_samples": len(all_spreads),
            "uptick_ratio": (ups / max(ups + dws, 1))
        }


@dataclass
class Candidate:
    symbol: str
    conId: int
    first_seen: datetime
    last_update: datetime
    pm_change_pct: float = 0.0
    last_price: float = 0.0
    pm_score: float = 0.0
    ring: RingBuffer = field(default_factory=RingBuffer)
    vref_open_2m: Optional[float] = None
    traded: bool = False
    # 实时bar（5s）
    bars5s: Optional[BarDataList] = None
    vwap5s_num: float = 0.0
    vwap5s_den: float = 0.0
    vol5s_sum: float = 0.0


# ====================== 策略主体 ======================

class PremarketMomentumV4c:

    def __init__(self, mode=None, dry_run=False, client_id: int = 401):
        if mode is None:
            mode = os.getenv("TRADING_MODE", "paper")
        self.mode = mode
        self.dry_run = dry_run

        self.ib = IB()
        self.host = '127.0.0.1'
        self.port = 4002 if mode == 'paper' else 7497
        self.client_id = client_id

        self.cands: Dict[str, Candidate] = {}
        self.frozen_syms: List[str] = []
        self.subscribed: Dict[str, Any] = {}
        self.vref_cache: Dict[str, Optional[float]] = self._load_vref_cache()

        self.daily_trades_done = 0
        self.positions: Dict[str, Dict] = {}
        self.working_orders: Dict[str, Dict] = {}
        self.parent_trades: Dict[str, Dict[str, Any]] = {}
        self._last_scan_mark: Optional[dtime] = None
        self._entry_orders_sent = False
        self._last_entry_attempt: Optional[datetime] = None

        # 连接状态 & 心跳
        self._is_connected = False
        self._last_heartbeat_ts = time.time()

        # 测试开关（由 CLI 注入）
        self._force_run = False
        self._dev_fast = False

    # ---------- 连接/断开 ----------
    def connect(self) -> bool:
        try:
            logger.info(f"Connecting IB @ {self.host}:{self.port} cid={self.client_id}")
            self.ib.connect(self.host, self.port, clientId=self.client_id, timeout=20)
            # 挂接事件
            self.ib.connectedEvent += self._on_connected
            self.ib.disconnectedEvent += self._on_disconnected
            self._is_connected = True
            logger.info("✅ Connected")
            return True
        except Exception as e:
            logger.exception(f"connect failed: {e}")
            return False

    def _on_connected(self):
        self._is_connected = True
        logger.info("🔌 IB connected")

    def _on_disconnected(self):
        self._is_connected = False
        logger.error("🔌 IB disconnected")

    def _ensure_connection(self) -> bool:
        """若断线则尝试重连并恢复订阅。"""
        try:
            if self._is_connected and self.ib.isConnected():
                return True
            logger.info("Reconnecting IB ...")
            try:
                self.ib.disconnect()
            except Exception:
                pass
            self.ib.connect(self.host, self.port, clientId=self.client_id, timeout=10)
            self._is_connected = True
            # 重新订阅 tick & 5s bars
            for sym in list(self.subscribed.keys()):
                self._unsubscribe_symbol(sym)
            for sym in (self.frozen_syms or list(self.cands.keys())):
                self._subscribe_symbol(sym)
            for sym in self.frozen_syms:
                self._ensure_realtime_bars(sym)
            logger.info("Reconnected and resubscribed.")
            return True
        except Exception as e:
            logger.exception(f"Reconnect failed: {e}")
            return False

    def disconnect(self):
        for sym, tkr in list(self.subscribed.items()):
            try:
                tkr.updateEvent -= self._on_tick
            except Exception:
                pass
        self.subscribed.clear()

        for c in self.cands.values():
            try:
                if c.bars5s:
                    c.bars5s.updateEvent.clear()
            except Exception:
                pass

        if self.ib.isConnected():
            self.ib.disconnect()
            logger.info("Disconnected")
        self.parent_trades.clear()

    # ---------- 通用超时封装 ----------
    def _call_with_timeout(self, coro, timeout: float, label: str):
        """给 ib_insync 协程加超时；失败返回 None。"""
        try:
            return self.ib.run(asyncio.wait_for(coro, timeout))
        except asyncio.TimeoutError:
            logger.warning(f"{label} timed out after {timeout}s")
            return None
        except Exception as e:
            logger.debug(f"{label} error: {e}")
            return None

    # ---------- 启动预热 ----------
    def warmup(self):
        logger.info("Warmup: initial scan and V_ref prefetch ...")
        rows = self.scan_premarket()
        self.refresh_candidates(rows)
        top_syms = [c.symbol for c in sorted(self.cands.values(), key=lambda x: x.pm_score, reverse=True)[:TOPN_FREEZE]]
        for sym in top_syms:
            _ = self.get_vref_open_2m(sym, cache_only=False)
            self._ensure_realtime_bars(sym)
        logger.info("Warmup done.")

    # ---------- 扫描（带超时+重连） ----------
    def scan_premarket(self) -> List[Dict]:
        if not self._ensure_connection():
            return []
        try:
            sub = ScannerSubscription()
            sub.numberOfRows = 50
            sub.instrument = 'STK'
            sub.locationCode = 'STK.US.MAJOR'
            sub.scanCode = 'TOP_PERC_GAIN'
            sub.abovePrice = MIN_PRICE
            sub.marketCapAbove = MIN_MARKET_CAP / 1_000_000

            rows: List[Dict] = []
            for attempt in range(3):
                res = self._call_with_timeout(self.ib.reqScannerData(sub), timeout=8.0, label="reqScannerData")
                if res is None:
                    self._ensure_connection()
                    continue
                out = []
                for d in res:
                    c = d.contractDetails.contract
                    sym = getattr(c, "symbol", None)
                    if not sym or c.secType != 'STK' or sym in ETF_BLOCKLIST:
                        continue
                    out.append({"symbol": sym, "conId": c.conId, "exchange": c.exchange})
                rows = out
                break

            logger.info(f"Scanner {len(rows)} rows after filter")
            return rows
        except Exception as e:
            logger.exception(f"scanner error: {e}")
            return []

    def refresh_candidates(self, rows: List[Dict]):
        symbols = [r["symbol"] for r in rows]
        quotes = self._quotes(symbols)
        now = datetime.now(ET)

        for r in rows:
            sym = r["symbol"]
            q = quotes.get(sym)
            if not q:
                continue
            change = q.get("change_pct", 0.0)
            if change < MIN_PM_CHANGE_PCT:
                continue

            if sym not in self.cands:
                c = Candidate(
                    symbol=sym, conId=r["conId"],
                    first_seen=now, last_update=now,
                    pm_change_pct=change, last_price=q["last"]
                )
                self.cands[sym] = c
                self._subscribe_symbol(sym)
                self._ensure_realtime_bars(sym)
                logger.info(f"✅ New candidate {sym} pm_change={change:.2f}%")
            else:
                c = self.cands[sym]
                c.pm_change_pct = change
                c.last_price = q["last"]
                c.last_update = now

        for c in self.cands.values():
            hrs = max((now - c.first_seen).total_seconds() / 3600.0, 0.0)
            dur_score = min(hrs / 0.5, 1.0) * 0.4
            chg_score = min(max(c.pm_change_pct, 0.0) / 10.0, 1.0) * 0.6
            c.pm_score = dur_score + chg_score

    def freeze_topN(self):
        if self.frozen_syms:
            return
        ranked = sorted(self.cands.values(), key=lambda x: x.pm_score, reverse=True)
        self.frozen_syms = [c.symbol for c in ranked[:TOPN_FREEZE]]
        logger.info(f"❄️  Frozen candidates: {self.frozen_syms}")
        # 只保留冻结者的订阅
        for sym in list(self.subscribed.keys()):
            if sym not in self.frozen_syms:
                self._unsubscribe_symbol(sym)

    # ---------- 订阅 tick & 5s bars ----------
    def _subscribe_symbol(self, sym: str):
        if sym in self.subscribed:
            return
        contract = Stock(sym, 'SMART', 'USD')
        tkr = self.ib.reqMktData(contract, "", False, False)
        tkr.updateEvent += self._on_tick
        self.subscribed[sym] = tkr

    def _unsubscribe_symbol(self, sym: str):
        tkr = self.subscribed.pop(sym, None)
        if tkr:
            try: tkr.updateEvent -= self._on_tick
            except Exception: pass

    def _ensure_realtime_bars(self, sym: str):
        c = self.cands.get(sym)
        if not c or c.bars5s:
            return
        contract = Stock(sym, 'SMART', 'USD')
        bars = self.ib.reqRealTimeBars(contract, barSize=5, whatToShow='TRADES', useRTH=False)
        bars.updateEvent += lambda b=bars, s=sym: self._on_bar5s(s, b)
        c.bars5s = bars

    def _on_tick(self, tkr):
        try:
            sym = tkr.contract.symbol
            c = self.cands.get(sym)
            if not c:
                return
            now = datetime.now(ET)
            last = self._f(getattr(tkr, "last", math.nan))
            bid = self._f(getattr(tkr, "bid", math.nan))
            ask = self._f(getattr(tkr, "ask", math.nan))
            lastSize = getattr(tkr, "lastSize", None)
            size = float(lastSize) if lastSize else None
            c.ring.ingest_tick(now, last, bid, ask, size)
        except Exception as e:
            logger.debug(f"tick error: {e}")

    def _on_bar5s(self, sym: str, bars: BarDataList):
        try:
            c = self.cands.get(sym)
            if not c or not bars: return
            bar = bars[-1]
            px = float(bar.close or 0.0)
            vol = float(bar.volume or 0.0)
            if vol > 0 and px > 0:
                c.vol5s_sum += vol
                c.vwap5s_num += px * vol
                c.vwap5s_den += vol
        except Exception as e:
            logger.debug(f"bar5s error: {e}")

    # ---------- 行情快照（带超时） ----------
    def _quotes(self, symbols: List[str], timeout=2, retries=1) -> Dict[str, Dict]:
        contracts = [Stock(s, 'SMART', 'USD') for s in symbols]
        quotes: Dict[str, Dict] = {}
        for attempt in range(1 + retries):
            try:
                tkrs = self._call_with_timeout(self.ib.reqTickers(*contracts), timeout=5.0, label="reqTickers")
                if tkrs is None:
                    self._ensure_connection()
                    continue
                self.ib.sleep(timeout)
                for t in tkrs:
                    last = self._f(getattr(t, "last", math.nan))
                    bid = self._f(getattr(t, "bid", math.nan))
                    ask = self._f(getattr(t, "ask", math.nan))
                    close = self._f(getattr(t, "close", math.nan))
                    if any(x is None or math.isnan(x) for x in [last, bid, ask]) or close is None or math.isnan(close):
                        continue
                    change_pct = (last - close) / max(close, EPS) * 100.0
                    mid = (bid + ask)/2.0
                    spread_pct = (ask - bid)/max(mid, EPS) if ask > bid > 0 else math.nan
                    vol = getattr(t, "volume", None)
                    quotes[t.contract.symbol] = {
                        "last": last, "bid": bid, "ask": ask, "close": close,
                        "change_pct": change_pct, "spread_pct": spread_pct, "volume": vol
                    }
                if quotes or attempt == retries:
                    return quotes
            except Exception as e:
                logger.debug(f"quotes error attempt#{attempt}: {e}")
        return quotes

    # ---------- V_ref 缓存 ----------
    def _load_vref_cache(self) -> Dict[str, Optional[float]]:
        try:
            if os.path.exists(VREF_CACHE_PATH):
                with open(VREF_CACHE_PATH, 'r') as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    def _save_vref_cache(self):
        try:
            with open(VREF_CACHE_PATH, 'w') as f:
                json.dump(self.vref_cache, f)
        except Exception:
            pass

    def get_vref_open_2m(self, sym: str, cache_only=False) -> Optional[float]:
        if sym in self.vref_cache:
            return self.vref_cache[sym]
        if cache_only:
            return None
        try:
            contract = Stock(sym, 'SMART', 'USD')
            yday = datetime.now(ET).date() - timedelta(days=1)
            end_dt = datetime.combine(yday, dtime(9, 33, 0)).replace(tzinfo=ET)
            bars = self.ib.reqHistoricalData(
                contract=contract, endDateTime=end_dt,
                durationStr="1800 S", barSizeSetting="1 min",
                whatToShow="TRADES", useRTH=True, formatDate=1
            )
            vref = 0.0
            for b in bars:
                bt = b.date if isinstance(b.date, datetime) else datetime.fromtimestamp(b.date, ET)
                bt = bt.replace(tzinfo=ET) if bt.tzinfo is None else bt
                if bt.time() in (dtime(9, 30, 0), dtime(9, 31, 0)):
                    vref += float(b.volume or 0.0)
            self.vref_cache[sym] = vref if vref > 0 else None
            self._save_vref_cache()
            return self.vref_cache[sym]
        except Exception as e:
            logger.debug(f"Vref error {sym}: {e}")
            self.vref_cache[sym] = None
            self._save_vref_cache()
            return None

    # ---------- 观察窗打分 ----------
    def compute_obs_score(self, sym: str) -> Tuple[float, Dict[str, Any]]:
        c = self.cands[sym]
        now = datetime.now(ET)
        open_ts = int(datetime.combine(now.date(), OPEN_TIME, tzinfo=ET).timestamp())
        obs_end_ts = int(datetime.combine(now.date(), OBS_END_TIME, tzinfo=ET).timestamp()) - 1

        # 晚启动保护
        if obs_end_ts < open_ts:
            details = dict(
                vol_today=0.0, vref=None, rvol_open=None,
                spread_median=math.nan, spread_p95=math.nan, spread_samples=0,
                vwap=math.nan, last=math.nan, or_high=math.nan, or_low=math.nan, uptick_ratio=math.nan,
                vol_s=0.0, sp_s=0.0, vwap_s=0.0, or_s=0.0, tape_s=0.0
            )
            return 0.0, details

        tick_stats = c.ring.window_stats(open_ts, obs_end_ts)

        # 量
        vol_today = c.vol5s_sum if c.vol5s_sum > 0 else (tick_stats["volume"] or 0.0)
        # VWAP
        if c.vwap5s_den > 0:
            vwap = c.vwap5s_num / c.vwap5s_den
        else:
            vwap = tick_stats["vwap"]

        # RVOL_open
        vref = c.vref_open_2m if c.vref_open_2m is not None else self.get_vref_open_2m(sym)
        c.vref_open_2m = vref
        rvol = (vol_today / vref) if vref and vref > 0 else None

        def vol_score(x):
            if x is None: return 0.0
            if x < 0.2:   return 0.0
            if x < 0.5:   return (x - 0.2)/0.3 * 0.3
            if x < 1.0:   return 0.3 + (x - 0.5)/0.5 * 0.3
            if x < 2.0:   return 0.6 + (x - 1.0)/1.0 * 0.3
            return 1.0
        vol_s = vol_score(rvol)

        sm = tick_stats["spread_median"]; s95 = tick_stats["spread_p95"]
        spread_samps = tick_stats.get("spread_samples", 0)

        def clamp01(x, hi):
            if x is None or math.isnan(x): return 0.0
            return max(0.0, min(1.0, 1.0 - x/hi))
        sp_s = 0.5*clamp01(sm, MAX_SPREAD_PCT_OBS_MEDIAN) + 0.5*clamp01(s95, MAX_SPREAD_PCT_OBS_P95)
        vwap_s = 1.0 if (vwap and not math.isnan(vwap) and tick_stats["last"] and tick_stats["last"] > vwap) else 0.0
        h = tick_stats["high"]; l = tick_stats["low"]; last = tick_stats["last"]
        or_s = 0.0
        if h and l and not any(map(math.isnan, [h, l])) and (last and not math.isnan(last)):
            or_range = max(h - l, EPS)
            progress = (last - l) / or_range
            or_s = max(0.0, min(1.0, progress))
        upr = tick_stats["uptick_ratio"] if tick_stats["uptick_ratio"] is not None else 0.0
        tape_s = 1.0 if (not math.isnan(upr) and upr >= 0.6) else (0.7 if upr >= 0.55 else (0.4 if upr >= 0.5 else 0.0))

        obs_score = (W_VOL*vol_s + W_SPREAD*sp_s + W_VWAP*vwap_s + W_OR*or_s + W_TAPE*tape_s)

        details = dict(
            vol_today=vol_today, vref=vref, rvol_open=rvol,
            spread_median=sm, spread_p95=s95, spread_samples=spread_samps,
            vwap=vwap, last=last, or_high=h, or_low=l, uptick_ratio=upr,
            vol_s=vol_s, sp_s=sp_s, vwap_s=vwap_s, or_s=or_s, tape_s=tape_s
        )
        return obs_score, details

    def _spread_ok(self, sm: Optional[float], s95: Optional[float], samples: int) -> bool:
        if samples < 60:
            return False
        if sm is None or math.isnan(sm) or s95 is None or math.isnan(s95):
            return False
        return sm <= MAX_SPREAD_PCT_OBS_MEDIAN and s95 <= MAX_SPREAD_PCT_OBS_P95

    # ---------- 9:33 入场 ----------
    def enter_if_ok(self, sym: str, final_score: float) -> bool:
        if self.daily_trades_done >= MAX_DAILY_TRADES:
            return False
        c = self.cands.get(sym)
        if not c:
            logger.debug(f"{sym} skip: candidate missing")
            return False
        if ONE_TRADE_PER_SYMBOL and c.traded:
            return False

        obs_score, det = self.compute_obs_score(sym)

        # RVOL 或绝对量/额兜底
        rvol_ok = det["rvol_open"] is not None and det["rvol_open"] >= 0.2
        abs_ok = (det["vol_today"] >= ABS_VOLUME_FALLBACK) or \
                 (det["last"] and det["vwap"] and (det["last"] > det["vwap"]) and
                  (det["vol_today"] * (det["last"] or 0.0) >= ABS_NOTIONAL_FALLBACK))
        if not rvol_ok and not abs_ok:
            logger.info(f"{sym} skip: RVOL insufficient and abs fallback not met")
            return False

        if not self._spread_ok(det["spread_median"], det["spread_p95"], det["spread_samples"]):
            logger.info(f"{sym} skip: spread too wide (median={det['spread_median']}, p95={det['spread_p95']}, n={det['spread_samples']})")
            return False

        if not (det["vwap"] and det["last"] and det["last"] > det["vwap"]):
            logger.info(f"{sym} skip: not above VWAP")
            return False

        # 触发：突破 OR_high 或接近（<= 0.3*OR）
        trigger_ok = False
        or_h, or_l, last = det["or_high"], det["or_low"], det["last"]
        if or_h and not math.isnan(or_h) and last and not math.isnan(last):
            if last >= or_h - EPS:
                trigger_ok = True
            elif or_l and not math.isnan(or_l):
                or_range = max(or_h - or_l, EPS)
                trigger_ok = (or_h - last) <= (0.3 * or_range)
        if not trigger_ok:
            logger.info(f"{sym} skip: trigger not satisfied")
            return False

        # 快照（带超时）
        q = self._quotes([sym]).get(sym)
        if not q:
            logger.info(f"{sym} skip: no quote")
            return False
        bid, ask = q["bid"], q["ask"]
        if any(x is None or math.isnan(x) for x in [bid, ask]):
            logger.info(f"{sym} skip: invalid bid/ask")
            return False

        mid = (bid + ask) / 2.0
        spread = (ask - bid) / max(mid, EPS)
        if spread > MAX_SPREAD_PCT_OBS_P95:
            logger.info(f"{sym} skip: live spread too large")
            return False

        entry = round(ask * (1.0 + LIMIT_EPS_TICKS), 2)
        vwap_or = max(det["vwap"] or entry, det["or_low"] or entry)
        risk_per_share = max(entry - vwap_or, entry * FIXED_SL_FLOOR)
        if risk_per_share <= 0 or math.isnan(risk_per_share):
            risk_per_share = entry * FIXED_SL_FLOOR
        risk_per_share = min(risk_per_share, entry * RISK_PER_SHARE_CAP)

        risk_budget = CAPITAL * RISK_PCT
        shares = int(risk_per_share and risk_budget / max(risk_per_share, EPS))
        if shares <= 0:
            logger.info(f"{sym} skip: shares=0 (risk too tight)")
            return False

        edge = self._compute_edge_metric(final_score, det)
        logger.info(f"🎯 ENTER {sym} | entry={entry:.2f} shares={shares} edge={edge:.3f} reason=ok_all")

        if self.dry_run:
            self.positions[sym] = dict(symbol=sym, shares=shares, entry=entry,
                                       time=datetime.now(ET), hedge=None, edge=edge,
                                       filled=0)
            c.traded = True
            self.daily_trades_done += 1
            return True

        # 下单：父（BUY LIMIT, DAY, transmit=False）→ 子（TRAIL, LIMIT TP, transmit=True）
        contract = Stock(sym, 'SMART', 'USD')
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
        trail.trailingPercent = TRAILING_PCT
        trail.tif = 'DAY'
        trail.outsideRth = False

        tp_price = round(entry * (1.0 + TP_PCT/100.0), 2)
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

        self.positions[sym] = dict(
            symbol=sym, shares=shares, entry=entry, tp=tp_price,
            oca=oca_group, parentId=parent_id, hedge=None, edge=edge, filled=0
        )
        self.working_orders[sym] = dict(
            parentId=parent_id,
            trailId=trade_trail.order.orderId,
            tpId=trade_tp.order.orderId,
            ocaGroup=oca_group
        )
        self.parent_trades[sym] = dict(
            trade=trade_parent,
            last_filled=0,
            details=det,
            edge=edge
        )
        trade_parent.fillEvent += partial(self._on_parent_fill, sym)
        trade_parent.cancelledEvent += partial(self._on_parent_cancel, sym)
        trade_trail.fillEvent += partial(self._on_child_sell_fill, sym)
        trade_tp.fillEvent += partial(self._on_child_sell_fill, sym)
        c.traded = True
        self.daily_trades_done += 1
        return True

    def _compute_edge_metric(self, final_score: float, details: Dict[str, Any]) -> float:
        edge = max(0.0, min(1.5, final_score))
        rvol = details.get("rvol_open") or 0.0
        upr = details.get("uptick_ratio") or 0.0
        spread_penalty = 0.0
        sm = details.get("spread_median")
        s95 = details.get("spread_p95")
        if sm and not math.isnan(sm):
            spread_penalty += max(0.0, (sm / MAX_SPREAD_PCT_OBS_MEDIAN) - 1.0) * 0.1
        if s95 and not math.isnan(s95):
            spread_penalty += max(0.0, (s95 / MAX_SPREAD_PCT_OBS_P95) - 1.0) * 0.1
        edge += 0.1 * (rvol - 0.5)
        edge += 0.05 * (upr - 0.5)
        edge = max(0.0, edge - spread_penalty)
        return edge

    def _on_parent_fill(self, sym: str, trade: Trade, fill):
        if self.dry_run:
            return
        pos = self.positions.get(sym)
        info = self.parent_trades.get(sym)
        if not pos or not info:
            return
        try:
            filled_total = int(round(float(trade.filled or 0)))
        except Exception:
            filled_total = 0
        last = info.get("last_filled", 0)
        delta = max(0, filled_total - last)
        if delta <= 0:
            return
        info["last_filled"] = filled_total
        pos["filled"] = pos.get("filled", 0) + delta
        edge = pos.get("edge", info.get("edge", EDGE_BASELINE))
        pos["edge"] = edge
        entry = pos.get("entry")
        self._sync_option_hedge(sym, pos["filled"], edge, entry)

    def _on_parent_cancel(self, sym: str, trade: Trade):
        info = self.parent_trades.get(sym)
        if not info:
            return
        if info.get("last_filled", 0) <= 0:
            self._close_option_hedge(sym, "parent_cancelled")
            self.parent_trades.pop(sym, None)

    def _on_child_sell_fill(self, sym: str, trade: Trade, fill):
        if self.dry_run:
            return
        pos = self.positions.get(sym)
        if not pos:
            return
        try:
            shares_delta = int(round(float(getattr(fill.execution, "shares", 0) or 0)))
        except Exception:
            shares_delta = 0
        if shares_delta <= 0:
            return
        shares = max(0, int(pos.get("filled", 0)) - shares_delta)
        pos["filled"] = shares
        if shares <= 0:
            self._close_option_hedge(sym, "position_flat")
            self.parent_trades.pop(sym, None)
            return
        edge = pos.get("edge", EDGE_BASELINE)
        self._sync_option_hedge(sym, shares, edge, pos.get("entry"))

    def _sync_option_hedge(self, sym: str, target_shares: int, edge: float, entry: Optional[float]):
        if self.dry_run:
            return
        pos = self.positions.get(sym)
        if not pos:
            return
        if target_shares <= 0:
            self._close_option_hedge(sym, "position_flat")
            pos["filled"] = 0
            return
        if edge is None:
            edge = pos.get("edge", EDGE_BASELINE)
        if edge >= EDGE_HEDGE_TRIGGER:
            self._close_option_hedge(sym, "edge_recovered")
            return

        hedge = pos.get("hedge")
        if not hedge or not hedge.get("contract"):
            opt_pick = self._select_option_contract(sym, entry or pos.get("entry"))
            if not opt_pick:
                logger.info(f"{sym} hedge skipped: no option contract available")
                return
            contract, multiplier = opt_pick
            hedge = dict(contract=contract, qty=0, multiplier=multiplier,
                         edge=edge, hedge_ratio=None, orderId=None)
            pos["hedge"] = hedge
        else:
            contract = hedge.get("contract")
            multiplier = hedge.get("multiplier", 100)

        denom = max(EPS, EDGE_HEDGE_TRIGGER - EDGE_BASELINE)
        hedge_ratio = max(0.2, min(EDGE_HEDGE_MAX, (EDGE_HEDGE_TRIGGER - edge) / denom))
        target_contracts = max(1, math.ceil((target_shares / max(multiplier, 1)) * hedge_ratio))
        current_contracts = int(hedge.get("qty", 0))
        delta = target_contracts - current_contracts
        if delta == 0:
            return

        try:
            qualified = self.ib.qualifyContracts(hedge["contract"])
            if qualified:
                hedge["contract"] = qualified[0]
        except Exception as e:
            logger.info(f"{sym} hedge adjust skipped: qualifyContracts failed {e}")
            return

        action = 'BUY' if delta > 0 else 'SELL'
        qty = abs(delta)
        try:
            order = MarketOrder(action, qty)
            order.tif = 'DAY'
            trade = self.ib.placeOrder(hedge["contract"], order)
            hedge["qty"] = target_contracts
            hedge["hedge_ratio"] = hedge_ratio
            hedge["orderId"] = trade.order.orderId
            logger.info(
                f"{sym} hedge {'add' if delta > 0 else 'reduce'}: {action} {qty}x "
                f"{hedge['contract'].right}{hedge['contract'].strike} "
                f"{hedge['contract'].lastTradeDateOrContractMonth} "
                f"target={target_contracts} edge={edge:.3f}"
            )
        except Exception as e:
            logger.warning(f"{sym} hedge adjust failed: {e}")

    def _select_option_contract(self, sym: str, ref_price: float):
        cand = self.cands.get(sym)
        if not cand:
            return None
        if ref_price is None or (isinstance(ref_price, float) and math.isnan(ref_price)):
            return None
        try:
            params = self.ib.reqSecDefOptParams(sym, "", "STK", cand.conId)
        except Exception as e:
            logger.info(f"{sym} hedge skipped: reqSecDefOptParams failed {e}")
            return None
        if not params:
            return None
        now_date = datetime.now(ET).date()
        best_param = None
        for p in params:
            if p.exchange in ("SMART", "BOX", "BEX", "CBOE") or not best_param:
                best_param = p
            if p.exchange == "SMART":
                best_param = p
                break
        if not best_param or not best_param.expirations:
            return None
        candidate_exps = []
        for exp in best_param.expirations:
            try:
                d = datetime.strptime(exp, "%Y%m%d").date()
            except ValueError:
                continue
            if d < now_date + timedelta(days=OPTION_MIN_DTE):
                continue
            if d > now_date + timedelta(days=OPTION_MAX_DTE):
                continue
            candidate_exps.append((abs((d - now_date).days), exp))
        if not candidate_exps:
            try:
                exp = min(best_param.expirations)
            except Exception:
                return None
        else:
            candidate_exps.sort()
            exp = candidate_exps[0][1]
        strikes = []
        for s in best_param.strikes:
            try:
                strikes.append(float(s))
            except (TypeError, ValueError):
                continue
        if not strikes:
            return None
        strike = min(strikes, key=lambda s: abs(s - ref_price))
        try:
            multiplier = int(float(best_param.multiplier))
        except (TypeError, ValueError):
            multiplier = 100
        contract = Option(sym, exp, strike, 'P', 'SMART', tradingClass=best_param.tradingClass or sym)
        return contract, multiplier

    def _close_option_hedge(self, sym: str, reason: str):
        pos = self.positions.get(sym)
        if not pos:
            return
        hedge = pos.get("hedge")
        if not hedge:
            return
        contract = hedge.get("contract")
        qty = hedge.get("qty", 0)
        if not contract or qty <= 0:
            pos["hedge"] = None
            return
        try:
            order = MarketOrder('SELL', qty)
            order.tif = 'DAY'
            self.ib.placeOrder(contract, order)
            logger.info(
                f"{sym} hedge exit: SELL {qty}x {contract.right}{contract.strike} "
                f"{contract.lastTradeDateOrContractMonth} reason={reason}"
            )
        except Exception as e:
            logger.warning(f"{sym} hedge exit failed: {e}")
        finally:
            if hedge:
                hedge["qty"] = 0
                hedge["orderId"] = None
            pos["hedge"] = None

    def _should_attempt_entry(self, now: datetime) -> bool:
        if self._entry_orders_sent:
            return False
        if self._last_entry_attempt is None:
            return True
        return (now - self._last_entry_attempt) >= timedelta(seconds=ENTRY_RETRY_SECONDS)

    def _execute_entry_window(self, current_time: dtime, *, is_retry: bool) -> bool:
        attempt_label = "retry" if is_retry else "initial"
        logger.info(f"⏰ Entry window {attempt_label} attempt at {current_time}")

        if not self.frozen_syms:
            logger.warning("Frozen symbols empty at entry time, freezing now")
            self.freeze_topN()

        scored: List[Tuple[str, float]] = []
        for sym in self.frozen_syms:
            cand = self.cands.get(sym)
            if not cand:
                continue
            try:
                obs_s, _ = self.compute_obs_score(sym)
            except Exception:
                obs_s = 0.0
            pm_s = cand.pm_score
            final_s = 0.5 * pm_s + 0.5 * obs_s
            scored.append((sym, final_s))
        scored.sort(key=lambda x: x[1], reverse=True)

        logger.info(f"Scored candidates: {[(s, round(sc, 3)) for s, sc in scored]}")

        orders_sent = False
        for sym, score in scored:
            if self.daily_trades_done >= MAX_DAILY_TRADES:
                break
            placed = self.enter_if_ok(sym, final_score=score)
            if placed:
                orders_sent = True

        if orders_sent:
            logger.info(f"Entry logic completed. Trades executed: {self.daily_trades_done}/{MAX_DAILY_TRADES}")
        else:
            logger.info("Entry attempt finished without qualifying orders")
        return orders_sent

    # ---------- 部分成交/未成交处理 ----------
    def manage_open_orders(self):
        """
        10:00：若父单未成交≥80%，保留；否则撤余；若部分成交，按成交数量重绑 OCO。
        """
        now_t = datetime.now(ET).time()
        if now_t < CANCEL_UNFILLED_TIME or now_t >= CLEANUP_BEGIN_TIME:
            return
        for sym, work in list(self.working_orders.items()):
            try:
                parentId = work.get("parentId")
                if not parentId:
                    continue
                trades = [tr for tr in self.ib.trades() if tr.order.orderId == parentId]
                if not trades:
                    continue
                tr = trades[0]
                filled = float(tr.orderStatus.filled or 0.0)
                total = float(tr.order.totalQuantity or 0.0)
                if total <= 0:
                    continue
                fill_ratio = filled / total if total else 0.0
                if fill_ratio >= 0.8:
                    continue  # OK
                remaining = int(max(total - filled, 0))
                if remaining > 0:
                    try:
                        self.ib.cancelOrder(tr.order)
                    except Exception:
                        pass
                    reason_tag = CANCEL_UNFILLED_TIME.strftime("underfilled_%H:%M:%S")
                    logger.info(f"{sym} cancel remaining parent qty={remaining} reason={reason_tag}")
                    work["parentId"] = None
                if filled > 0:
                    self._rebundle_children(sym, int(filled))
                else:
                    self._close_option_hedge(sym, "parent_cancelled")
                    self._cancel_children(sym)
                    self.positions.pop(sym, None)
                    self.working_orders.pop(sym, None)
                    self.parent_trades.pop(sym, None)
            except Exception as e:
                logger.debug(f"manage_open_orders error {sym}: {e}")

    def _rebundle_children(self, sym: str, new_qty: int):
        """
        用新的成交数量重建OCO（先撤旧，再建新）。
        """
        try:
            pos = self.positions.get(sym)
            if not pos:
                return
            contract = Stock(sym, 'SMART', 'USD')
            # 撤旧子单
            self._cancel_children(sym)
            # 重建
            entry = float(pos["entry"])
            tp_price = round(entry * (1.0 + TP_PCT/100.0), 2)

            trail = Order()
            trail.action = 'SELL'
            trail.totalQuantity = int(new_qty)
            trail.orderType = 'TRAIL'
            trail.trailingPercent = TRAILING_PCT
            trail.tif = 'DAY'
            trail.outsideRth = False

            tp = LimitOrder('SELL', int(new_qty), tp_price)
            tp.tif = 'DAY'
            tp.outsideRth = False

            oca_group = f"OCA_{sym}_{int(time.time())}"
            for ch in (trail, tp):
                ch.ocaGroup = oca_group
                ch.ocaType = 1
                ch.transmit = False
            tp.transmit = True

            trade_trail = self.ib.placeOrder(contract, trail)
            trade_tp = self.ib.placeOrder(contract, tp)
            # 更新记录（shares 与 filled 对齐）
            pos["shares"] = int(new_qty)
            pos["filled"] = int(new_qty)
            pos["tp"] = tp_price
            pos["oca"] = oca_group
            self.working_orders[sym] = dict(
                parentId=pos.get("parentId"),
                trailId=trade_trail.order.orderId,
                tpId=trade_tp.order.orderId,
                ocaGroup=oca_group
            )
            trade_trail.fillEvent += partial(self._on_child_sell_fill, sym)
            trade_tp.fillEvent += partial(self._on_child_sell_fill, sym)
            logger.info(f"{sym} rebind OCO qty={new_qty}")
        except Exception as e:
            logger.debug(f"_rebundle_children error {sym}: {e}")

    def _cancel_children(self, sym: str):
        """
        尝试撤销该 symbol 的全部子单（TP/Trail），幂等。
        """
        try:
            work = self.working_orders.get(sym, {})
            parent_id = work.get("parentId")
            oca_group = work.get("ocaGroup")
            child_ids = {cid for cid in (work.get("trailId"), work.get("tpId")) if cid}
            for tr in self.ib.trades():
                if tr.contract.symbol != sym:
                    continue
                order = tr.order
                if order.action != 'SELL':
                    continue
                matches_parent = parent_id and order.parentId == parent_id
                matches_child_id = order.orderId in child_ids
                matches_oca = oca_group and getattr(order, "ocaGroup", None) == oca_group
                if not (matches_parent or matches_child_id or matches_oca):
                    continue
                if tr.isDone():
                    continue
                try:
                    self.ib.cancelOrder(order)
                except Exception:
                    pass
        except Exception:
            pass

    # ---------- 10:05 清理 & 10:10 硬退出 ----------
    def cleanup_sequence(self):
        now_t = datetime.now(ET).time()
        if now_t < CLEANUP_BEGIN_TIME or now_t >= HARD_EXIT_TIME:
            return
        # 1) 撤父单未成交余量
        for sym, work in list(self.working_orders.items()):
            try:
                parentId = work.get("parentId")
                if not parentId:
                    continue
                trades = [tr for tr in self.ib.trades() if tr.order.orderId == parentId]
                if not trades: continue
                tr = trades[0]
                filled = float(tr.orderStatus.filled or 0.0)
                total = float(tr.order.totalQuantity or 0.0)
                remaining = int(max(total - filled, 0))
                if remaining > 0:
                    try:
                        self.ib.cancelOrder(tr.order)
                    except Exception:
                        pass
                    logger.info(f"{sym} cleanup: cancel remaining parent qty={remaining}")
            except Exception:
                pass
        # 2) 若有持仓：先撤子单，再市价全平（以 filled 优先）
        for sym, pos in list(self.positions.items()):
            try:
                self._cancel_children(sym)
                contract = Stock(sym, 'SMART', 'USD')
                qty_filled = int(pos.get("filled", 0))
                qty_planned = int(pos.get("shares", 0))
                qty = qty_filled if qty_filled > 0 else qty_planned
                if qty > 0:
                    order = MarketOrder('SELL', qty)
                    order.tif = 'DAY'
                    self.ib.placeOrder(contract, order)
                    logger.info(f"{sym} cleanup: market exit qty={qty}")
                self._close_option_hedge(sym, "cleanup_exit")
            except Exception:
                pass
        # 3) 撤所有子单
        for sym in list(self.positions.keys()):
            self._cancel_children(sym)
        # 4) 清空本地状态
        self.positions.clear()
        self.working_orders.clear()
        self.parent_trades.clear()

    def force_exit_all(self, reason: str):
        logger.info(f"⏰ FORCE EXIT ({reason})")
        if self.dry_run:
            self.positions.clear()
            self.working_orders.clear()
            self.parent_trades.clear()
            return
        try:
            self.cleanup_sequence()
        except Exception:
            pass
        try:
            for tr in self.ib.trades():
                try:
                    self.ib.cancelOrder(tr.order)
                except Exception:
                    pass
        except Exception:
            pass
        self.parent_trades.clear()

    # ---------- 主循环 ----------
    def run(self):
        # 启动时间检查 / 开发演练模式
        now_et = datetime.now(ET)
        current_time = now_et.time()

        # 如果启用 dev-fast，把时间轴压缩成“从现在起”的几分钟演练
        # FREEZE=+10s, OPEN=+20s, OBS_END=+60s, CANCEL=+150s, CLEANUP=+210s, HARD_EXIT=+240s
        if getattr(self, "_dev_fast", False):
            def add_seconds(base_dt, sec):
                dt2 = base_dt + timedelta(seconds=sec)
                return dtime(dt2.hour, dt2.minute, dt2.second)
            global FREEZE_TIME, OPEN_TIME, OBS_END_TIME, CANCEL_UNFILLED_TIME, CLEANUP_BEGIN_TIME, HARD_EXIT_TIME
            FREEZE_TIME = add_seconds(now_et, 10)
            OPEN_TIME = add_seconds(now_et, 20)
            OBS_END_TIME = add_seconds(now_et, 60)
            CANCEL_UNFILLED_TIME = add_seconds(now_et, 150)
            CLEANUP_BEGIN_TIME = add_seconds(now_et, 210)
            HARD_EXIT_TIME = add_seconds(now_et, 240)
            logger.warning(
                f"🧪 DEV-FAST schedule: "
                f"FREEZE={FREEZE_TIME}, OPEN={OPEN_TIME}, OBS_END={OBS_END_TIME}, "
                f"CANCEL={CANCEL_UNFILLED_TIME}, CLEANUP={CLEANUP_BEGIN_TIME}, HARD_EXIT={HARD_EXIT_TIME}"
            )

        # 晚启动硬拦截（可通过 --force-run 忽略）
        if current_time >= HARD_EXIT_TIME and not getattr(self, "_force_run", False):
            logger.error(f"❌ Started too late ({current_time}). Hard exit time is {HARD_EXIT_TIME}. Aborting.")
            return
        elif current_time >= HARD_EXIT_TIME and getattr(self, "_force_run", False):
            logger.warning(f"⚠️ Started after HARD_EXIT but --force-run set: continuing for testing.")

        if current_time >= FREEZE_TIME:
            logger.warning(f"⚠️  Started late ({current_time}). Last scan time was {SCAN_TIMES[-1] if SCAN_TIMES else 'N/A'}. May miss candidates.")

        if current_time >= OBS_END_TIME:
            logger.warning(f"⚠️  Started after observation window ({current_time}). Entry logic may not work correctly.")

        if not self.connect():
            return
        self.warmup()

        try:
            while True:
                now_et = datetime.now(ET)
                t = now_et.time()

                # 心跳（每 60s）
                now_ts = time.time()
                if now_ts - self._last_heartbeat_ts >= 60:
                    logger.info("💓 heartbeat alive")
                    self._last_heartbeat_ts = now_ts

                # 断线自愈
                if not self._ensure_connection():
                    self.ib.sleep(1.0)
                    continue

                # 10:10 后，不再交易
                if t >= HARD_EXIT_TIME:
                    self.force_exit_all("HARD_EXIT_10:10")
                    logger.info("Trading day finished. Stop.")
                    break

                # 盘前扫描（带时间窗）
                if t < FREEZE_TIME:
                    for st in SCAN_TIMES:
                        if st <= t < (datetime.combine(datetime.now(ET).date(), st) + timedelta(minutes=1)).time():
                            if self._last_scan_mark != st:
                                if not self._ensure_connection():
                                    break
                                rows = self.scan_premarket()
                                self.refresh_candidates(rows)
                                self._last_scan_mark = st
                                break

                # 冻结候选
                elif FREEZE_TIME <= t < OPEN_TIME:
                    if not self.frozen_syms:
                        self.freeze_topN()

                # 开盘~观察窗：仅聚合
                elif OPEN_TIME <= t < OBS_END_TIME:
                    pass

                # 9:33：尝试入场
                elif OBS_END_TIME <= t < CANCEL_UNFILLED_TIME:
                    if self._should_attempt_entry(now_et):
                        is_retry = self._last_entry_attempt is not None
                        if self._execute_entry_window(t, is_retry=is_retry):
                            self._entry_orders_sent = True
                        self._last_entry_attempt = now_et

                # 10:00：未成交/部分成交处理
                elif CANCEL_UNFILLED_TIME <= t < CLEANUP_BEGIN_TIME:
                    self.manage_open_orders()

                # 10:05：清理序列
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

    # ---------- 小工具 ----------
    @staticmethod
    def _f(x) -> Optional[float]:
        try:
            if x is None: return None
            if isinstance(x, float) and math.isnan(x): return None
            return float(x)
        except Exception:
            return None


# ====================== CLI ======================

def main():
    parser = argparse.ArgumentParser(description="Premarket Momentum V4c (DAY-only, Trailing Stop OCO)")
    parser.add_argument('--mode', choices=['paper', 'live'], default=None)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--client-id', type=int, default=401, help='IB clientId (avoid collision across processes)')
    # 新增测试开关
    parser.add_argument('--force-run', action='store_true', help='Ignore late-start guard (for testing)')
    parser.add_argument('--dev-fast', action='store_true', help='Compress schedule relative to now for a quick end-to-end drill')

    args = parser.parse_args()

    if args.mode == 'live' and not args.dry_run:
        confirm = input("⚠️ LIVE mode with real money. Continue? (yes/no): ")
        if confirm.lower() != 'yes':
            print("Aborted.")
            return

    util.patchAsyncio()
    strat = PremarketMomentumV4c(mode=args.mode, dry_run=args.dry_run, client_id=args.client_id)
    # 把开关传给实例
    strat._force_run = args.force_run
    strat._dev_fast = args.dev_fast

    strat.run()


if __name__ == "__main__":
    main()
