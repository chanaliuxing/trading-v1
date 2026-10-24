#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Premarket Gap Momentum — TESTABLE (force scan/order) + Same-Day Exit
Author: ChatGPT (for Wei)
Last Updated: 2025-10-21

测试增强：
- --force-test-scan / --force-test-order 下，放宽 RVOL（可忽略），放宽最小 spread 样本，触发条件可用涨幅+站上VWAP替代滚动高点
- 使用“日内成交量/成交额快照”作为 ABS fallback，保证盘中测试也能触发
- 保证当日清仓（默认 15:55 ET，或 --test-exit 指定）
"""

import os
import sys
import json
import math
import argparse
import time
import logging
import atexit
import random
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from ib_insync import (
    IB, Stock, util, ScannerSubscription,
    LimitOrder, MarketOrder, Order, Trade, BarDataList
)

# ====================== 全局配置 ======================
load_dotenv()

ET = ZoneInfo("America/New_York")

# 盘前时段（生产逻辑）
TRADE_START_TIME = dtime(4, 0, 0)
OPEN_TIME = dtime(9, 30, 0)
OBS_END_TIME = dtime(9, 33, 0)
CANCEL_UNFILLED_TIME = dtime(9, 25, 0)
CLEANUP_BEGIN_TIME = dtime(9, 27, 0)
HARD_EXIT_TIME = dtime(9, 29, 50)
GOOD_TILL_TIME = dtime(9, 29, 30)

# 流程节奏
SCAN_INTERVAL_SECONDS = 60
ENTRY_ATTEMPT_INTERVAL = 30
PM_ROLL_WINDOW_SEC = 15 * 60     # 观察窗（盘前滚动15分钟）

# 候选与交易上限
TOPN_FREEZE = 5
MAX_DAILY_TRADES = 3
ONE_TRADE_PER_SYMBOL = True

# 篩选
MIN_PM_CHANGE_PCT_DEFAULT = 2.0
MIN_PM_CHANGE_PCT_EARLY   = 1.0          # 08:00 ET 前放宽
MIN_PRICE = 5.0
MIN_MARKET_CAP = 5_000_000_000
ETF_BLOCKLIST = {
    'SPY','QQQ','IWM','DIA','EEM','VTI','VOO','VEA','VWO','AGG','BND','LQD','IVV','VTV','IEFA','IEMG'
}

# 观察窗阈值
MAX_SPREAD_PCT_OBS_MEDIAN = 0.0025  # 25bp
MAX_SPREAD_PCT_OBS_P95 = 0.0050     # 50bp
ABS_VOLUME_FALLBACK = 500_000
ABS_NOTIONAL_FALLBACK = 20_000_000

# 资金与风控
CAPITAL = 50_000
RISK_PCT = 0.005
TRAILING_PCT = 1.8        # %
TP_PCT = 2.4              # %
LIMIT_EPS_TICKS = 0.0005  # +0.05%
FIXED_SL_FLOOR = 0.012    # 最小风险每股：1.2%

# 打分权重
W_VOL = 0.3
W_SPREAD = 0.25
W_VWAP = 0.2
W_OR = 0.15
W_TAPE = 0.1

# 环形缓冲
RING_SECONDS = 180
SPREAD_PCT_CAP = 0.05
EPS = 1e-9

# 跨日盘前量缓存（可选）
CROSSDAY_CACHE_PATH = os.path.join(os.path.dirname(__file__), 'crossday_pmref.json')

# ====================== 日志 ======================

_RUN_DATE = datetime.now(ET).strftime('%Y%m%d')

def _build_logger(debug: bool) -> logging.Logger:
    log_dir = Path(__file__).resolve().parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("premarket_test_v8")
    logger.setLevel(logging.DEBUG if debug else logging.INFO)

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.DEBUG if debug else logging.INFO)
    sh.setFormatter(fmt)
    if not any(isinstance(h, logging.StreamHandler) for h in logger.handlers):
        logger.addHandler(sh)

    fh = logging.FileHandler(log_dir / f"premarket_{_RUN_DATE}.log", encoding="utf-8")
    fh.setLevel(logging.DEBUG if debug else logging.INFO)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # 屏蔽 IB 扫描 162 噪声
    class _IBScannerCancelFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            msg = record.getMessage()
            if "API scanner subscription cancelled" in msg and "Error 162" in msg:
                logger.debug("Ignore expected scanner auto-cancel: %s", msg)
                return False
            return True
    logging.getLogger("ib_insync.wrapper").addFilter(_IBScannerCancelFilter())

    logging.getLogger("ib_insync").setLevel(logging.WARNING)
    logging.getLogger("ib_insync.wrapper").setLevel(logging.WARNING)
    logging.getLogger("ib_insync.client").setLevel(logging.WARNING)

    atexit.register(lambda: [h.flush() for h in logger.handlers])
    return logger

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
        self.cum_spread_samples = 0

    def ingest_tick(self, t: datetime, last: Optional[float], bid: Optional[float],
                    ask: Optional[float], size: Optional[float]):
        epoch = int(t.timestamp())
        b = self.buckets.get(epoch)
        if b is None:
            b = SecondBucket(ts=epoch)
            self.buckets[epoch] = b
        pre_len = len(b.spreads)
        b.update(last, bid, ask, size, self.last_price)
        if len(b.spreads) > pre_len:
            self.cum_spread_samples += (len(b.spreads) - pre_len)
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
    traded: bool = False
    # 5s bar增强
    bars5s: Optional[BarDataList] = None
    vwap5s_num: float = 0.0
    vwap5s_den: float = 0.0
    vol5s_sum: float = 0.0
    # 日内快照（用于测试 ABS fallback）
    daily_volume: float = 0.0
    daily_notional: float = 0.0


# ====================== 策略主体 ======================

class PremarketMomentumTest:

    def __init__(self, logger: logging.Logger, mode=None, dry_run=False,
                 use_crossday_ref=False, force_test_scan=False, force_test_order=False,
                 test_exit_time: Optional[str] = None):
        self.log = logger
        if mode is None:
            mode = os.getenv("TRADING_MODE", "paper")
        self.mode = mode
        self.dry_run = dry_run
        self.use_crossday_ref = use_crossday_ref
        self.force_test_scan = force_test_scan
        self.force_test_order = force_test_order

        # 解析测试硬退出时间（ET）
        self.test_exit_time: Optional[dtime] = None
        if test_exit_time:
            try:
                hh, mm = test_exit_time.split(":")
                self.test_exit_time = dtime(int(hh), int(mm), 0)
            except Exception:
                self.log.warning(f"Invalid --test-exit '{test_exit_time}', fallback to default 15:55")
        if self.test_exit_time is None:
            self.test_exit_time = dtime(15, 55, 0)

        self.ib = IB()
        self.host = '127.0.0.1'
        self.port = 4002 if mode == 'paper' else 7497
        self.client_id = self._pick_client_id()
        self.log.info(f"Using clientId={self.client_id} (mode={mode}, dry_run={dry_run}, "
                      f"force_test_scan={force_test_scan}, force_test_order={force_test_order}, "
                      f"test_exit={self.test_exit_time})")

        self.cands: Dict[str, Candidate] = {}
        self.frozen_syms: List[str] = []
        self.subscribed: Dict[str, Any] = {}

        self.daily_trades_done = 0
        self.positions: Dict[str, Dict] = {}
        self.working_orders: Dict[str, Dict] = {}
        self._entry_attempt_log: Dict[str, datetime] = {}
        self._next_scan_at: Optional[datetime] = None

        self.crossday_cache: Dict[str, Dict[str, float]] = self._load_crossday_cache()

    # ---------- 有效时点（测试模式动态覆盖） ----------
    def _effective_times(self):
        if not (self.force_test_scan or self.force_test_order):
            return dict(
                trade_start=TRADE_START_TIME,
                cancel_unfilled=CANCEL_UNFILLED_TIME,
                cleanup_begin=CLEANUP_BEGIN_TIME,
                hard_exit=HARD_EXIT_TIME,
                good_till=GOOD_TILL_TIME
            )
        now = datetime.now(ET)
        hard_exit_dt = datetime.combine(now.date(), self.test_exit_time, tzinfo=ET)
        if now >= hard_exit_dt:
            hard_exit_dt = now + timedelta(minutes=20)
        cancel_unfilled_dt = hard_exit_dt - timedelta(minutes=30)
        cleanup_begin_dt   = hard_exit_dt - timedelta(minutes=10)
        good_till_dt       = hard_exit_dt - timedelta(minutes=1)

        return dict(
            trade_start=(now - timedelta(minutes=1)).time(),
            cancel_unfilled=cancel_unfilled_dt.time(),
            cleanup_begin=cleanup_begin_dt.time(),
            hard_exit=hard_exit_dt.time(),
            good_till=good_till_dt.time()
        )

    # ---------- 连接/断开 ----------
    def connect(self) -> bool:
        try:
            self.log.info(f"Connecting IB @ {self.host}:{self.port} cid={self.client_id}")
            self.ib.connect(self.host, self.port, clientId=self.client_id, timeout=20)
            for tr in self.ib.trades():
                self._bind_trade_update(tr)
            try:
                self.ib.orderStatusEvent -= self._on_order_status
            except Exception:
                pass
            self.ib.orderStatusEvent += self._on_order_status
            self.log.info("✅ Connected")
            return True
        except Exception as e:
            self.log.exception(f"connect failed: {e}")
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
            self.log.info("Disconnected")
        self._entry_attempt_log.clear()

    def _pick_client_id(self) -> int:
        env_id = os.getenv("IB_CLIENT_ID")
        if env_id:
            try:
                cid = int(env_id)
                if cid > 0:
                    return cid
            except ValueError:
                self.log.warning(f"Invalid IB_CLIENT_ID env '{env_id}', falling back to random")
        rng = random.SystemRandom()
        while True:
            cid = rng.randint(1000, 60000)
            if cid not in (0, 401):
                return cid

    # ---------- 时间与阈值 ----------
    def _effective_min_change(self) -> float:
        now = datetime.now(ET).time()
        return MIN_PM_CHANGE_PCT_EARLY if now < dtime(8, 0, 0) else MIN_PM_CHANGE_PCT_DEFAULT

    # ---------- 启动预热 ----------
    def warmup(self):
        self.log.info("Warmup: initial scan (no 9:30 history) ...")
        rows = self.scan_premarket()
        self.refresh_candidates(rows)
        ranked = sorted(self.cands.values(), key=lambda x: x.pm_score, reverse=True)[:TOPN_FREEZE]
        for c in ranked:
            self._ensure_realtime_bars(c.symbol)
        self.log.info("Warmup done.")

    # ---------- 扫描 ----------
    def scan_premarket(self) -> List[Dict]:
        res: List[Dict] = []
        tried = []
        for code in ["TOP_PERC_GAIN", "MOST_ACTIVE"]:
            try:
                sub = ScannerSubscription()
                sub.numberOfRows = 30
                sub.instrument = 'STK'
                sub.locationCode = 'STK.US.MAJOR'
                sub.scanCode = code
                sub.abovePrice = MIN_PRICE
                sub.marketCapAbove = MIN_MARKET_CAP / 1_000_000
                data = self.ib.reqScannerData(sub)
                self.log.info(f"{code}: {len(data)} raw rows")
                for d in data:
                    c = d.contractDetails.contract
                    sym = c.symbol
                    if sym in ETF_BLOCKLIST or c.secType != 'STK':
                        continue
                    res.append({"symbol": sym, "conId": c.conId, "exchange": c.exchange})
                tried.append(code)
                if res:
                    break
            except Exception as e:
                self.log.debug(f"scanner error ({code}): {e}")
                continue
        uniq = {r["symbol"]: r for r in res}
        rows = list(uniq.values())
        self.log.info(f"Scanner total {len(rows)} rows after ETF/filters (tried: {tried})")
        if not rows:
            self.log.warning("⚠️ Scanner returned 0 symbols after filter.")
        return rows

    def refresh_candidates(self, rows: List[Dict]):
        self.log.info(f"Refreshing candidates: got {len(rows)} rows")
        if not rows:
            return

        symbols = [r["symbol"] for r in rows]
        quotes = self._quotes_batched(symbols)
        self.log.info(f"Quotes received for {len(quotes)}/{len(symbols)} symbols")

        now = datetime.now(ET)
        min_change = self._effective_min_change()
        accepted = 0
        rejected_change = 0

        for r in rows:
            sym = r["symbol"]
            q = quotes.get(sym)
            if not q:
                continue
            change = q.get("change_pct", 0.0)
            last = q["last"]
            spread_pct = q.get("spread_pct")
            vol = float(q.get("volume") or 0.0)
            notional = vol * (last or 0.0)

            if change < min_change and not self.force_test_scan:
                rejected_change += 1
                continue

            if sym not in self.cands:
                c = Candidate(
                    symbol=sym, conId=r["conId"],
                    first_seen=now, last_update=now,
                    pm_change_pct=change, last_price=last,
                    daily_volume=vol, daily_notional=notional
                )
                self.cands[sym] = c
                self._subscribe_symbol(sym)
                self._ensure_realtime_bars(sym)
                self.log.info(f"✅ New candidate {sym} pm_change={change:.2f}%")
            else:
                c = self.cands[sym]
                c.pm_change_pct = change
                c.last_price = last
                c.last_update = now
                c.daily_volume = vol
                c.daily_notional = notional
            accepted += 1

        for c in self.cands.values():
            hrs = max((now - c.first_seen).total_seconds() / 3600.0, 0.0)
            dur_score = min(hrs / 0.5, 1.0) * 0.4
            chg_score = min(max(c.pm_change_pct, 0.0) / 10.0, 1.0) * 0.6
            c.pm_score = dur_score + chg_score

        self.log.info(f"Candidates accepted={accepted}, rejected_by_change={rejected_change}, total_pool={len(self.cands)}")

        if not self.frozen_syms and self.cands:
            self.freeze_topN()
        self._attempt_immediate_entries(list(self.cands.keys()))

    def freeze_topN(self):
        if self.frozen_syms:
            return
        ranked = sorted(self.cands.values(), key=lambda x: x.pm_score, reverse=True)
        self.frozen_syms = [c.symbol for c in ranked[:TOPN_FREEZE]]
        self.log.info(f"❄️  Frozen candidates: {self.frozen_syms}")
        for sym in list(self.subscribed.keys()):
            if sym not in self.frozen_syms:
                self._unsubscribe_symbol(sym)

    # ---------- 订阅 / Bars ----------
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
        try:
            contract = Stock(sym, 'SMART', 'USD')
            bars = self.ib.reqRealTimeBars(contract, barSize=5, whatToShow='TRADES', useRTH=False)
            bars.updateEvent += lambda b=bars, s=sym: self._on_bar5s(s, b)
            c.bars5s = bars
        except Exception as e:
            self.log.debug(f"reqRealTimeBars failed for {sym}: {e}")

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
            self.log.debug(f"tick error: {e}")

    def _on_bar5s(self, sym: str, bars: BarDataList):
        try:
            c = self.cands.get(sym)
            if not c or not bars: return
            bar = bars[-1]
            px = float(getattr(bar, "close", 0.0) or 0.0)
            vol = float(getattr(bar, "volume", 0.0) or 0.0)
            if vol > 0 and px > 0:
                c.vol5s_sum += vol
                c.vwap5s_num += px * vol
                c.vwap5s_den += vol
        except Exception as e:
            self.log.debug(f"bar5s error: {e}")

    # ---------- 报价批量 ----------
    def _quotes_batched(self, symbols: List[str], batch=10, timeout=1.2) -> Dict[str, Dict]:
        quotes: Dict[str, Dict] = {}
        if not symbols:
            return quotes
        for i in range(0, len(symbols), batch):
            batch_syms = symbols[i:i+batch]
            contracts = [Stock(s, 'SMART', 'USD') for s in batch_syms]
            try:
                tkrs = self.ib.reqTickers(*contracts)
                self.ib.waitOnUpdate(timeout=timeout)
                for t in tkrs:
                    last = self._f(getattr(t, "last", math.nan))
                    bid  = self._f(getattr(t, "bid",  math.nan))
                    ask  = self._f(getattr(t, "ask",  math.nan))
                    close= self._f(getattr(t, "close",math.nan))
                    if any(x is None or math.isnan(x) for x in [last, bid, ask, close]):
                        continue
                    change_pct = (last - close) / max(close, EPS) * 100.0
                    mid = (bid + ask)/2.0
                    spread_pct = (ask - bid)/max(mid, EPS) if ask > bid > 0 else math.nan
                    vol = getattr(t, "volume", None)
                    quotes[t.contract.symbol] = {
                        "last": last, "bid": bid, "ask": ask, "close": close,
                        "change_pct": change_pct, "spread_pct": spread_pct, "volume": vol
                    }
            except Exception as e:
                self.log.debug(f"quotes batch error: {e}")
        return quotes

    # ---------- 跨日盘前量（可选） ----------
    def _load_crossday_cache(self) -> Dict[str, Dict[str, float]]:
        try:
            if os.path.exists(CROSSDAY_CACHE_PATH):
                with open(CROSSDAY_CACHE_PATH, 'r') as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    def _save_crossday_cache(self):
        try:
            with open(CROSSDAY_CACHE_PATH, 'w') as f:
                json.dump(self.crossday_cache, f)
        except Exception:
            pass

    def _get_crossday_pmref(self, sym: str) -> Optional[float]:
        today = datetime.now(ET).date()
        yday = today - timedelta(days=1)
        key = f"{sym}:{yday.isoformat()}"
        hit = self.crossday_cache.get(key)
        if hit is not None:
            return float(hit)
        try:
            contract = Stock(sym, 'SMART', 'USD')
            start_dt = datetime.combine(yday, dtime(4, 0, 0), tzinfo=ET)
            end_dt   = datetime.combine(yday, dtime(9, 30, 0), tzinfo=ET)
            bars = self.ib.reqHistoricalData(
                contract, endDateTime=end_dt, durationStr="1 D",
                barSizeSetting="5 mins", whatToShow="TRADES", useRTH=False, formatDate=1
            )
            vol_sum = 0.0
            for b in bars:
                bt = b.date if isinstance(b.date, datetime) else datetime.fromtimestamp(b.date, tz=ET)
                if bt.tzinfo is None:
                    bt = bt.replace(tzinfo=ET)
                if start_dt <= bt <= end_dt:
                    vol_sum += float(getattr(b, "volume", 0.0) or 0.0)
            self.crossday_cache[key] = vol_sum if vol_sum > 0 else None
            self._save_crossday_cache()
            return self.crossday_cache[key]
        except Exception as e:
            self.log.debug(f"crossday pmref error {sym}: {e}")
            self.crossday_cache[key] = None
            self._save_crossday_cache()
            return None

    # ---------- RVOL（方案A：滚动、方案B：跨日） ----------
    def _compute_rvol_intraday(self, ring: RingBuffer) -> Optional[float]:
        now = datetime.now(ET)
        end_ts = int(now.timestamp())
        mid_ts = end_ts - 15*60
        start_ts = mid_ts - 15*60
        curr = ring.window_stats(mid_ts, end_ts)
        prev = ring.window_stats(start_ts, mid_ts)
        v_curr = curr["volume"]
        v_prev = prev["volume"]
        if v_prev and v_prev > 0:
            return v_curr / max(v_prev, EPS)
        return None

    # ---------- 观察窗打分 ----------
    def compute_obs_score(self, sym: str) -> Tuple[float, Dict[str, Any]]:
        c = self.cands[sym]
        now = datetime.now(ET)
        if self.force_test_scan or (now.time() < OPEN_TIME):
            end_ts = int(now.timestamp())
            start_ts = end_ts - PM_ROLL_WINDOW_SEC
            tick_stats = c.ring.window_stats(start_ts, end_ts)
            or_high = tick_stats["high"]
            or_low = tick_stats["low"]
        else:
            open_ts = int(datetime.combine(now.date(), OPEN_TIME, tzinfo=ET).timestamp())
            obs_end_ts = int(datetime.combine(now.date(), OBS_END_TIME, tzinfo=ET).timestamp()) - 1
            tick_stats = c.ring.window_stats(open_ts, obs_end_ts)
            or_high = tick_stats["high"]
            or_low = tick_stats["low"]

        # 成交量/VWAP
        vol_today = c.vol5s_sum if c.vol5s_sum > 0 else (tick_stats["volume"] or 0.0)
        # 测试时，用日内快照兜底（便于触发）
        if self.force_test_order or self.force_test_scan:
            vol_today = max(vol_today, c.daily_volume or 0.0)
        vwap = (c.vwap5s_num / c.vwap5s_den) if c.vwap5s_den > 0 else tick_stats["vwap"]

        if self.use_crossday_ref:
            pmref = self._get_crossday_pmref(sym)
            rvol = (vol_today / pmref) if pmref and pmref > 0 else None
        else:
            rvol = self._compute_rvol_intraday(c.ring)

        sm = tick_stats["spread_median"]; s95 = tick_stats["spread_p95"]
        spread_samps = tick_stats.get("spread_samples", 0)

        def clamp01(x, hi):
            if x is None or math.isnan(x): return 0.0
            return max(0.0, min(1.0, 1.0 - x/hi))

        vwap_s = 1.0 if (vwap and not math.isnan(vwap) and tick_stats["last"] and tick_stats["last"] > vwap) else 0.0

        h, l, last = or_high, or_low, tick_stats["last"]
        or_s = 0.0
        if h and l and not any(map(math.isnan, [h, l])) and (last and not math.isnan(last)):
            or_range = max(h - l, EPS)
            or_s = max(0.0, min(1.0, (last - l) / or_range))

        upr = tick_stats["uptick_ratio"] if tick_stats["uptick_ratio"] is not None else 0.0
        tape_s = 1.0 if (not math.isnan(upr) and upr >= 0.6) else (0.7 if upr >= 0.55 else (0.4 if upr >= 0.5 else 0.0))

        def vol_score(x):
            if x is None: return 0.0
            if x < 0.2:   return 0.0
            if x < 0.5:   return (x - 0.2)/0.3 * 0.3
            if x < 1.0:   return 0.3 + (x - 0.5)/0.5 * 0.3
            if x < 2.0:   return 0.6 + (x - 1.0)/1.0 * 0.3
            return 1.0
        vol_s = vol_score(rvol)

        sp_s = 0.5*clamp01(sm, MAX_SPREAD_PCT_OBS_MEDIAN) + 0.5*clamp01(s95, MAX_SPREAD_PCT_OBS_P95)
        obs_score = (W_VOL*vol_s + W_SPREAD*sp_s + W_VWAP*vwap_s + W_OR*or_s + W_TAPE*tape_s)

        return obs_score, dict(
            vol_today=vol_today, rvol_open=rvol,
            spread_median=sm, spread_p95=s95, spread_samples=spread_samps,
            vwap=vwap, last=last, or_high=h, or_low=l, uptick_ratio=upr
        )

    def _spread_ok(self, sm: Optional[float], s95: Optional[float], samples: int, cum_samples: int) -> bool:
        if sm is None or math.isnan(sm) or s95 is None or math.isnan(s95):
            return False
        # 测试时放宽 spread 样本数
        if self.force_test_order or self.force_test_scan:
            min_samples = 5
        else:
            min_samples = 30 if datetime.now(ET).time() < OPEN_TIME else 60
        if (samples + cum_samples) < min_samples:
            return False
        return sm <= MAX_SPREAD_PCT_OBS_MEDIAN and s95 <= MAX_SPREAD_PCT_OBS_P95

    def _good_till_str(self) -> str:
        eff = self._effective_times()
        today = datetime.now(ET).date()
        expires = datetime.combine(today, eff["good_till"], tzinfo=ET)
        return expires.strftime("%Y%m%d %H:%M:%S")

    # ---------- 入场 ----------
    def enter_if_ok(self, sym: str) -> bool:
        if self.daily_trades_done >= MAX_DAILY_TRADES:
            return False
        c = self.cands.get(sym)
        if not c:
            self.log.debug(f"{sym} skip: candidate missing")
            return False
        if ONE_TRADE_PER_SYMBOL and c.traded:
            return False

        obs_score, det = self.compute_obs_score(sym)

        # 1) RVOL / ABS —— 测试模式可放宽
        if self.force_test_order or self.force_test_scan:
            rvol_ok = True
            abs_ok = (c.daily_notional >= 10_000_000) or (c.daily_volume >= 300_000)  # 放宽，盘中易触发
        else:
            rvol_ok = det["rvol_open"] is not None and det["rvol_open"] >= 0.2
            abs_ok = (det["vol_today"] >= ABS_VOLUME_FALLBACK) or \
                     (det["last"] and det["vwap"] and (det["last"] > det["vwap"]) and
                      (det["vol_today"] * (det["last"] or 0.0) >= ABS_NOTIONAL_FALLBACK))
        if not (rvol_ok or abs_ok):
            self.log.info(f"{sym} skip: RVOL insufficient and abs fallback not met")
            return False

        # 2) 点差
        if not self._spread_ok(det["spread_median"], det["spread_p95"], det["spread_samples"], c.ring.cum_spread_samples):
            self.log.info(f"{sym} skip: spread too wide (median={det['spread_median']}, p95={det['spread_p95']}, n={det['spread_samples']}, cum={c.ring.cum_spread_samples})")
            return False

        # 3) 趋势 / 触发条件
        if not (det["vwap"] and det["last"] and det["last"] > det["vwap"]):
            self.log.info(f"{sym} skip: not above VWAP")
            return False

        trigger_ok = False
        or_h, or_l, last = det["or_high"], det["or_low"], det["last"]
        if or_h and not math.isnan(or_h) and last and not math.isnan(last):
            if last >= or_h - EPS:
                trigger_ok = True
            elif or_l and not math.isnan(or_l):
                or_range = max(or_h - or_l, EPS)
                prox = 0.4 if (self.force_test_scan or datetime.now(ET).time() < OPEN_TIME) else 0.3
                trigger_ok = (or_h - last) <= (prox * or_range)
        # 测试模式兜底：若 OR 统计不足，但涨幅达标且站上VWAP，也允许触发
        if (self.force_test_order or self.force_test_scan) and not trigger_ok:
            if c.pm_change_pct >= 2.0 and det["last"] and det["vwap"] and det["last"] > det["vwap"]:
                trigger_ok = True

        if not trigger_ok:
            self.log.info(f"{sym} skip: trigger not satisfied")
            return False

        # 4) 价格与仓位
        q = self._quotes_batched([sym]).get(sym)
        if not q:
            self.log.info(f"{sym} skip: no quote")
            return False
        bid, ask, last_snap = q["bid"], q["ask"], q["last"]
        if any(x is None or math.isnan(x) for x in [bid, ask]):
            self.log.info(f"{sym} skip: invalid bid/ask")
            return False

        mid = (bid + ask) / 2.0
        live_spread = (ask - bid) / max(mid, EPS)
        if live_spread > MAX_SPREAD_PCT_OBS_P95:
            self.log.info(f"{sym} skip: live spread too large")
            return False

        entry = round(ask * (1.0 + LIMIT_EPS_TICKS), 2)
        vwap_or = max(det["vwap"] or entry, det["or_low"] or entry)
        risk_per_share = max(entry - vwap_or, entry * FIXED_SL_FLOOR)
        if risk_per_share <= 0 or math.isnan(risk_per_share):
            risk_per_share = entry * FIXED_SL_FLOOR
        risk_budget = CAPITAL * RISK_PCT
        shares = int(risk_budget / max(risk_per_share, EPS))
        if shares <= 0:
            self.log.info(f"{sym} skip: shares=0 (risk too tight)")
            return False

        self.log.info(f"🎯 ENTER {sym} | entry={entry:.2f} shares={shares} rvol={det['rvol_open']} (dry_run={self.dry_run})")

        if self.dry_run:
            self.positions[sym] = dict(symbol=sym, shares=shares, entry=entry,
                                       time=datetime.now(ET), good_till=self._good_till_str())
            c.traded = True
            self.daily_trades_done += 1
            self._entry_attempt_log.pop(sym, None)
            return True

        # 5) 下单：父（BUY LIMIT, GTD）+ 子（TRAIL, LIMIT TP, OCA）
        contract = Stock(sym, 'SMART', 'USD')

        parent = LimitOrder('BUY', shares, entry)
        parent.tif = 'GTD'
        parent.goodTillDate = self._good_till_str()
        parent.outsideRth = True
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
        trail.outsideRth = True
        trail.transmit = False

        tp_price = round(entry * (1.0 + TP_PCT/100.0), 2)
        take_profit = LimitOrder('SELL', shares, tp_price)
        take_profit.tif = 'DAY'
        take_profit.outsideRth = True
        take_profit.transmit = True

        oca_group = f"OCA_{sym}_{int(time.time())}"
        for ch in (trail, take_profit):
            ch.ocaGroup = oca_group
            ch.ocaType = 1
            ch.parentId = parent_id

        trade_trail = self.ib.placeOrder(contract, trail)
        trade_tp = self.ib.placeOrder(contract, take_profit)

        self.positions[sym] = dict(
            symbol=sym, shares=shares, entry=entry, tp=tp_price,
            oca=oca_group, parentId=parent_id, good_till=parent.goodTillDate
        )
        self.working_orders[sym] = dict(
            parentId=parent_id,
            trailId=trade_trail.order.orderId,
            tpId=trade_tp.order.orderId,
            ocaGroup=oca_group
        )
        c.traded = True
        self.daily_trades_done += 1
        self._entry_attempt_log.pop(sym, None)
        return True

    def _attempt_immediate_entries(self, symbols: Optional[List[str]] = None):
        now = datetime.now(ET)
        now_time = now.time()
        eff = self._effective_times()

        # 非测试：严格限制入场时间
        if not self.force_test_order:
            if now_time < eff["trade_start"] or now_time >= eff["cancel_unfilled"]:
                return
        if self.daily_trades_done >= MAX_DAILY_TRADES:
            return

        pending_syms = symbols if symbols is not None else list(self.cands.keys())
        tried = 0
        placed = 0
        for sym in pending_syms:
            now = datetime.now(ET)
            cand = self.cands.get(sym)
            if not cand or cand.traded:
                continue
            if self.daily_trades_done >= MAX_DAILY_TRADES:
                break
            last_try = self._entry_attempt_log.get(sym)
            if last_try and (now - last_try).total_seconds() < ENTRY_ATTEMPT_INTERVAL:
                continue
            self._entry_attempt_log[sym] = now
            tried += 1
            if self.enter_if_ok(sym):
                cand.traded = True
                placed += 1
        if tried:
            self.log.info(f"Entry attempt summary: tried={tried}, placed={placed}, daily_trades_done={self.daily_trades_done}")

    # ---------- Trade 监听 ----------
    def _on_order_status(self, trade: Trade):
        try:
            self._bind_trade_update(trade)
        except Exception:
            self.log.debug("order status bind error")

    def _bind_trade_update(self, trade: Trade):
        if not trade:
            return
        if getattr(trade, "_premarket_bound", False):
            return
        try:
            trade.updateEvent += self._on_trade_update
            trade._premarket_bound = True  # type: ignore[attr-defined]
        except Exception:
            pass

    def _on_trade_update(self, trade: Trade):
        try:
            sym = trade.contract.symbol
            st = trade.orderStatus
            if not st:
                return
            work = self.working_orders.get(sym)
            if not work:
                return
            parent_id = work.get("parentId")
            if trade.order.action != 'BUY' or trade.order.orderId != parent_id:
                return
            filled = int(st.filled or 0)
            total  = int(trade.order.totalQuantity or 0)
            if 0 < filled < total:
                self._rebundle_children(sym, filled)
        except Exception as e:
            self.log.debug(f"trade update error: {e}")

    # ---------- 部分成交/未成交 ----------
    def manage_open_orders(self):
        eff = self._effective_times()
        now_t = datetime.now(ET).time()
        if now_t < eff["cancel_unfilled"] or now_t >= eff["cleanup_begin"]:
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
                filled = tr.orderStatus.filled or 0.0
                total = tr.order.totalQuantity or 0.0
                if total <= 0:
                    continue
                fill_ratio = filled / total
                if fill_ratio >= 0.8:
                    continue
                remaining = int(max(total - filled, 0))
                if remaining > 0:
                    try:
                        self.ib.cancelOrder(tr.order)
                    except Exception:
                        pass
                    self.log.info(f"{sym} cancel remaining parent qty={remaining} reason=underfilled")
                if filled > 0:
                    self._rebundle_children(sym, int(filled))
                else:
                    self._cancel_children(sym)
                    self.positions.pop(sym, None)
                    self.working_orders.pop(sym, None)
                    self._entry_attempt_log.pop(sym, None)
            except Exception as e:
                self.log.debug(f"manage_open_orders error {sym}: {e}")

    def _rebundle_children(self, sym: str, new_qty: int):
        try:
            pos = self.positions.get(sym)
            if not pos:
                return
            contract = Stock(sym, 'SMART', 'USD')
            self._cancel_children(sym)
            entry = pos["entry"]
            tp_price = round(entry * (1.0 + TP_PCT/100.0), 2)
            trail = Order()
            trail.action = 'SELL'
            trail.totalQuantity = int(new_qty)
            trail.orderType = 'TRAIL'
            trail.trailingPercent = TRAILING_PCT
            trail.tif = 'DAY'
            trail.outsideRth = True
            trail.transmit = False

            tp = LimitOrder('SELL', int(new_qty), tp_price)
            tp.tif = 'DAY'
            tp.outsideRth = True
            tp.transmit = True

            oca_group = f"OCA_{sym}_{int(time.time())}"
            for ch in (trail, tp):
                ch.ocaGroup = oca_group
                ch.ocaType = 1

            trade_trail = self.ib.placeOrder(contract, trail)
            trade_tp = self.ib.placeOrder(contract, tp)
            pos["shares"] = int(new_qty)
            pos["tp"] = tp_price
            pos["oca"] = oca_group
            self.working_orders[sym] = dict(
                parentId=pos.get("parentId"),
                trailId=trade_trail.order.orderId,
                tpId=trade_tp.order.orderId,
                ocaGroup=oca_group
            )
            self.log.info(f"{sym} rebind OCO qty={new_qty}")
        except Exception as e:
            self.log.debug(f"_rebundle_children error {sym}: {e}")

    def _cancel_children(self, sym: str):
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

    # ---------- 清理 & 硬退出 ----------
    def cleanup_sequence(self):
        eff = self._effective_times()
        now_t = datetime.now(ET).time()
        if now_t < eff["cleanup_begin"] or now_t >= eff["hard_exit"]:
            return
        # 1) 撤父单余量
        for sym, work in list(self.working_orders.items()):
            try:
                parentId = work.get("parentId")
                if not parentId:
                    continue
                trades = [tr for tr in self.ib.trades() if tr.order.orderId == parentId]
                if not trades:
                    continue
                tr = trades[0]
                filled = tr.orderStatus.filled or 0.0
                total  = tr.order.totalQuantity or 0.0
                remaining = int(max(total - filled, 0))
                if remaining > 0:
                    try:
                        self.ib.cancelOrder(tr.order)
                    except Exception:
                        pass
                    self.log.info(f"{sym} cleanup: cancel remaining parent qty={remaining}")
            except Exception:
                pass
        # 2) 市价全平
        for sym, pos in list(self.positions.items()):
            try:
                qty = int(pos["shares"])
                if qty > 0:
                    contract = Stock(sym, 'SMART', 'USD')
                    order = MarketOrder('SELL', qty)
                    order.tif = 'DAY'
                    order.outsideRth = True
                    self.ib.placeOrder(contract, order)
                    self.log.info(f"{sym} cleanup: market exit qty={qty}")
            except Exception:
                pass
        # 3) 撤所有子单
        for sym in list(self.positions.keys()):
            self._cancel_children(sym)
        # 4) 清空本地状态
        self.positions.clear()
        self.working_orders.clear()
        self._entry_attempt_log.clear()

    def force_exit_all(self, reason: str):
        self.log.info(f"⏰ FORCE EXIT ({reason})")
        if self.dry_run:
            self.positions.clear()
            self.working_orders.clear()
            self._entry_attempt_log.clear()
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
        self._entry_attempt_log.clear()

    # ---------- 断线重订阅 ----------
    def _resubscribe_all(self):
        for sym in list(self.frozen_syms or self.cands.keys()):
            self._subscribe_symbol(sym)
            self._ensure_realtime_bars(sym)

    # ---------- 主循环 ----------
    def run(self):
        eff = self._effective_times()
        now_et = datetime.now(ET)

        # 非测试模式：迟启动保护
        if not (self.force_test_scan or self.force_test_order):
            if now_et.time() >= eff["hard_exit"]:
                self.log.error(f"❌ Started too late ({now_et.time()}). Hard exit time is {eff['hard_exit']}. Aborting.")
                self.force_exit_all("LATE_START")
                self.disconnect()
                return
            while True:
                now_et = datetime.now(ET)
                if now_et.time() >= eff["trade_start"]:
                    break
                time.sleep(0.25)
        else:
            self.log.info(f"TEST MODE active. Effective times (ET): "
                          f"trade_start={eff['trade_start']}, cancel_unfilled={eff['cancel_unfilled']}, "
                          f"cleanup_begin={eff['cleanup_begin']}, hard_exit={eff['hard_exit']}")

        if not self.connect():
            return
        self.warmup()
        self._attempt_immediate_entries()
        self._next_scan_at = datetime.now(ET) + timedelta(seconds=SCAN_INTERVAL_SECONDS)

        try:
            while True:
                now_et = datetime.now(ET)
                t = now_et.time()

                if not self.ib.isConnected():
                    self.log.warning("IB disconnected, attempting reconnect...")
                    if self.connect():
                        self._resubscribe_all()

                if t >= eff["hard_exit"]:
                    self.force_exit_all("HARD_EXIT")
                    self.log.info("Test/Session window finished (all flat). Stop.")
                    break

                if t >= eff["cleanup_begin"]:
                    self.cleanup_sequence()
                elif t >= eff["cancel_unfilled"]:
                    self.manage_open_orders()
                else:
                    if not self._next_scan_at or now_et >= self._next_scan_at:
                        rows = self.scan_premarket()
                        self.refresh_candidates(rows)
                        self._next_scan_at = now_et + timedelta(seconds=SCAN_INTERVAL_SECONDS)
                    self._attempt_immediate_entries()

                self.ib.sleep(1.0)

        except KeyboardInterrupt:
            self.log.info("Interrupted")
        except Exception as e:
            self.log.exception(f"Main loop error: {e}")
        finally:
            self.force_exit_all("EMERGENCY_EXIT")
            self.disconnect()

    # ---------- 工具 ----------
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
    parser = argparse.ArgumentParser(description="Premarket Momentum (TEST-mode friendly: force scan/order & same-day exit)")
    parser.add_argument('--mode', choices=['paper', 'live'], default=None)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--use-crossday-ref', action='store_true', help='使用“今天盘前 vs 昨天盘前总量”作为 RVOL 参考（方案B）')
    parser.add_argument('--debug', action='store_true', help='开启 DEBUG 日志')
    parser.add_argument('--force-test-scan', action='store_true', help='强制把现在当盘前：扫描/触发在 RTH 亦生效')
    parser.add_argument('--force-test-order', action='store_true', help='强制现在就尝试下单（paper 会真实挂单）')
    parser.add_argument('--test-exit', type=str, default=None, help='测试硬退出时间（ET，格式 HH:MM，默认 15:55）')
    args = parser.parse_args()

    util.patchAsyncio()
    logger = _build_logger(debug=args.debug)

    if args.mode == 'live' and not args.dry_run:
        confirm = input("⚠️ LIVE mode with real money. Continue? (yes/no): ")
        if confirm.lower() != 'yes':
            print("Aborted.")
            return

    strat = PremarketMomentumTest(
        logger,
        mode=args.mode,
        dry_run=args.dry_run,
        use_crossday_ref=args.use_crossday_ref,
        force_test_scan=args.force_test_scan,
        force_test_order=args.force_test_order,
        test_exit_time=args.test_exit
    )
    strat.run()


if __name__ == "__main__":
    main()
