"""
QQQDataFabric (QQQ V2)
Specialized data management for QQQ:
- ATR Seed loading/persisting (Daily memory loop)
- Price Base (09:30 window) recovery with Startup Anchor & IBKR Tiering
- Intraday warmup for mid-session restarts
"""
import json
import asyncio
from pathlib import Path
from datetime import datetime, time, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo
from libs.strategy.components.market_fabric import MarketFabric
from libs.strategy.components.history_provider import HistoryProvider
from libs.strategy.components.indicator_lib import IndicatorLib

ET = ZoneInfo("America/New_York")

class QQQDataFabric:
    def __init__(self, ctx: Any, state: Dict[str, Any], fabric: MarketFabric, history: HistoryProvider, seed_path: str):
        self.ctx = ctx
        self.state = state
        self.fabric = fabric
        self.history = history
        self.seed_path = Path(seed_path).expanduser()
        self.logger = ctx.log
        
        # State keys initialization
        if "atr_pre" not in self.state:
            self.state["atr_pre"] = {"TQQQ": None, "SQQQ": None}
        if "price_base" not in self.state:
            self.state["price_base"] = None

    async def initialize(self) -> None:
        """Restores ATR seeds and initiates tiered warmup sequences."""
        now = datetime.now(ET)
        self._load_atr_seeds()
        
        # --- STAGE 1: ATR Readiness (Pre-market or Anytime) ---
        for sym in ["TQQQ", "SQQQ"]:
            if len(self.fabric.bars_5m[sym]) < 14:
                self.logger.info("stage1_atr_warmup_started", symbol=sym)
                bars, source, count = await self.history.fetch_ibkr_historical_bars(
                    sym, duration_str="1 D", bar_size="5 mins", use_rth=False
                )
                if not bars or count < 14:
                    self.logger.warning("stage1_ibkr_failed_trying_clickhouse", symbol=sym)
                    yesterday = now - timedelta(days=1)
                    bars, source, count = await self.history.fetch_local_db_5m_bars(sym, yesterday, now)
                    if source == "ch_error":
                        self._mark_data_unavailable(symbol=sym, source=source, stage="stage1_atr_warmup")
                        continue
                
                if bars:
                    self.fabric.inject_historical_5m(sym, bars)
                    # FINAL LENGTH CHECK: Verify actual buffer state
                    actual_count = len(self.fabric.bars_5m[sym])
                    if actual_count >= 14:
                        self.logger.info("stage1_atr_warmup_success", symbol=sym, count=actual_count, source=source)
                    else:
                        self.logger.warning("stage1_atr_insufficient_after_injection", symbol=sym, count=actual_count)
                        self.state["data_integrity_warning"] = True
                else:
                    self.logger.error("ATR_WARMUP_FAILED_CRITICAL", symbol=sym)
                    self.state["data_integrity_warning"] = True

        # --- ATR HEALTH CHECK (Mandatory for 09:30-09:45) ---
        # V1 Port: Retry until success or timeout (L1660)
        if time(9, 30) <= now.time() <= time(9, 45):
            atr_ok = False
            for attempt in range(5): # Best effort within initialize
                atr_ok = True
                for sym in ["TQQQ", "SQQQ"]:
                    atr = IndicatorLib.atr(list(self.fabric.bars_5m[sym]))
                    if atr is None or atr <= 0.05:
                        atr_ok = False
                        break
                if atr_ok: break
                self.logger.info("atr_health_check_retry", attempt=attempt+1)
                await asyncio.sleep(2.0)
            
            if not atr_ok:
                self.logger.error("ATR_HEALTH_CHECK_FAILED_PERMANENT", reason="insufficient_data_after_retries")
                self.state["data_integrity_warning"] = True
            else:
                self.state["data_integrity_warning"] = False # Recovered!

        # --- STAGE 2: Intraday Memory Recovery (09:30+) ---
        if now.time() >= time(9, 30):
            last_warmup = self.state.get("last_warmup_at")
            if last_warmup:
                try:
                    if (now - datetime.fromisoformat(last_warmup)).total_seconds() < 60:
                        return
                except Exception: pass
            await self._warmup_for_restart(now)
            self.state["last_warmup_at"] = now.isoformat()

    def _load_atr_seeds(self) -> None:
        """Loads and validates historical ATR seeds from disk."""
        if not self.seed_path.exists():
            self.logger.warning("atr_seed_file_missing", path=str(self.seed_path))
            return

        try:
            data = json.loads(self.seed_path.read_text())
            latest = data.get("latest", {})
            seed_day = latest.get("day", "1970-01-01")
            
            # FRESHNESS CHECK
            today = datetime.now(ET).date()
            seed_date = datetime.strptime(seed_day, "%Y-%m-%d").date()
            age_days = (today - seed_date).days
            
            if age_days > 3:
                self.logger.warning("STALE_ATR_SEED_WARNING", age_days=age_days, seed_day=seed_day)
            
            atr_data = latest.get("atr_pre", {})
            self.state["atr_pre"]["TQQQ"] = atr_data.get("TQQQ")
            self.state["atr_pre"]["SQQQ"] = atr_data.get("SQQQ")
            self.logger.info("atr_seeds_loaded", day=seed_day, age=age_days)
        except Exception as e:
            self.logger.error("atr_seed_load_failed", error=str(e))

    def persist_atr_seeds(self) -> None:
        """Calculates current ATR and saves as seed for tomorrow (V1: Fallback pollution guard)."""
        try:
            # V1 Port: If ATR was based on Emergency Fallback, DON'T PERSIST!
            if self.state.get("data_integrity_warning"):
                self.logger.warning("skipping_atr_persist_integrity_failure")
                return

            now = datetime.now(ET)
            atr_pre = {}
            for sym in ["TQQQ", "SQQQ"]:
                val = IndicatorLib.atr(list(self.fabric.bars_5m[sym]))
                if val and val > 0:
                    atr_pre[sym] = round(float(val), 4)
            
            if not atr_pre: return

            payload = {
                "latest": {
                    "day": now.strftime("%Y-%m-%d"),
                    "atr_pre": atr_pre,
                    "updated_at": now.isoformat()
                }
            }
            # Atomic write (Ensure tmp is in the same folder as target)
            tmp_path = self.seed_path.with_name(self.seed_path.name + ".tmp")
            with tmp_path.open("w") as f:
                json.dump(payload, f, indent=2)
            tmp_path.replace(self.seed_path)
            self.logger.info("atr_seeds_persisted", seeds=atr_pre)
        except Exception as e:
            self.logger.error("atr_seed_persist_failed", error=str(e))

    async def _warmup_for_restart(self, now: datetime) -> None:
        """Intraday restart recovery."""
        if self.state["price_base"] is None:
            await self.recover_price_base(now)
        await self.warmup_vwap_data(now)

    async def warmup_vwap_data(self, now: datetime) -> None:
        """Tiered recovery for 1m bars from 09:30 to now."""
        open_time = now.replace(hour=9, minute=30, second=0, microsecond=0)
        if now <= open_time: return
        for sym in ["QQQ", "QQEW"]:
            bars, source, count = await self.history.fetch_ibkr_1m_bars(sym, open_time, now)
            if not bars or count < 1:
                bars, source, count = await self.history.fetch_local_db_1m_bars(sym, open_time, now)
                if source == "ch_error":
                    self._mark_data_unavailable(symbol=sym, source=source, stage="warmup_vwap_data")
                    continue
            if bars and count > 0:
                self.fabric.inject_historical_1m(sym, bars)

    def _mark_data_unavailable(self, *, symbol: str, source: str, stage: str) -> None:
        payload = {
            "dataset": "historical_bars",
            "symbol": str(symbol or "").strip().upper(),
            "source": source,
            "stage": stage,
            "reason": "history_provider_data_unavailable",
            "severity": "entry_blocking",
        }
        self.state["data_unavailable_gate"] = payload
        self.state["data_integrity_warning"] = True
        try:
            reporter = getattr(self.ctx, "report_data_unavailable", None)
            if callable(reporter):
                reporter(payload)
        except Exception:
            pass
        self.logger.error("qqq_v2_data_unavailable_gate", **payload)

    async def recover_price_base(self, now: datetime) -> bool:
        """V1 Port: Three-Tier Price Base Recovery."""
        open_time = now.replace(hour=9, minute=30, second=0, microsecond=0)
        lookback = int((now - open_time).total_seconds()) + 600
        rows = await self.fabric._sfs.last_n("QQQ", lookback)
        if not rows: return False
        
        # --- Tier 1: SFS 09:30 Window (V1 Port: ws_ms <= ts < we_ms) ---
        w_start = int(open_time.timestamp() * 1000)
        w_end = w_start + 10000
        pv, vv = 0.0, 0.0
        for r in rows:
            ts = int(r.get("ts", 0))
            if w_start <= ts < w_end: # V1 Alignment: Strict <
                px, vol = float(r.get("close", 0.0)), float(r.get("volume", 0.0))
                if px > 0: pv += px * vol; vv += vol
        if vv > 0:
            self.state["price_base"] = pv / vv
            self.logger.info("price_base_recovered", source="sfs_window")
            return True
        
        # --- Tier 2: Startup Anchor (V1 Port: ws <= ts < we) ---
        init_time = self.state.get("_init_at")
        if init_time:
            init_dt = datetime.fromisoformat(init_time)
            i_end_ms = int(init_dt.timestamp() * 1000)
            i_start_ms = i_end_ms - 10000
            pv_i, vv_i = 0.0, 0.0
            for r in rows:
                ts = int(r.get("ts", 0))
                if i_start_ms <= ts < i_end_ms: # V1 Alignment: Strict <
                    px, vol = float(r.get("close", 0.0)), float(r.get("volume", 0.0))
                    if px > 0: pv_i += px * vol; vv_i += vol
            if vv_i > 0:
                self.state["price_base"] = pv_i / vv_i
                self.logger.warning("price_base_recovered", source="startup_anchor")
                return True

        # --- Tier 3: IBKR Historical (Rate Limited) ---
        if hasattr(self.ctx, "analyzer") and not self.ctx.analyzer.can_attempt_ibkr(asyncio.get_running_loop().time()):
            return False
        
        ib_price, source, _ = await self.history.fetch_ibkr_price_base("QQQ", now)
        if ib_price:
            self.state["price_base"] = ib_price
            self.logger.info("price_base_recovered", source=source)
            return True
        return False
