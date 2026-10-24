"""
QQQ Intraday Strategy V2 (Modular & Pluggable)
----------------------------------------------
Architecture: Quentin V2 (Component-Based)
Logic: 100% Aligned with Hardened QQQ Spec
- FULL-FREQ: Stop/Target hit checks (0.5s loop).
- PER-TICK: Analysis sampling & toxic counting.
- DAILY-LOCKED: Success confirmation (Single-trade).
"""
from __future__ import annotations
import asyncio
import os
import signal
import subprocess
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from libs.strategy import StrategyAlgorithm, StrategyParams, OrderUpdate, PositionSnapshot
from libs.strategy.components.execution_guard import ExecutionGuard
from libs.strategy.components.protective_shield import ProtectiveShield
from libs.strategy.components.reality_auditor import RealityAuditor
from libs.strategy.components.state_memex import StateMemex
from libs.strategy.components.market_fabric import MarketFabric
from libs.strategy.components.indicator_lib import IndicatorLib
from libs.strategy.components.history_provider import HistoryProvider
from libs.strategy.components.observability import Observability

from .engines.entry_gatekeeper import EntryGatekeeper
from .engines.position_supervisor import PositionSupervisor
from .engines.data_fabric_qqq import QQQDataFabric
from .engines.strategic_analyzer import StrategicAnalyzer

ET = ZoneInfo("America/New_York")

class QQQIntradayStrategyV2(StrategyAlgorithm):
    async def initialize(self) -> None:
        self.persistent_state = {}
        self.state_lock = asyncio.Lock()
        try:
            sha = subprocess.check_output(['git', 'rev-parse', '--short', 'HEAD']).decode().strip()
            self.persistent_state["_version"] = f"v2-modular-{sha}"
        except Exception: self.persistent_state["_version"] = "v2-modular-unknown"

        now_et = datetime.now(ET)
        self.persistent_state["_day"] = now_et.strftime("%Y-%m-%d")
        self.persistent_state["_init_at"] = now_et.isoformat()
        self.persistent_state["_diag_path"] = self.params.extras.get("diag_log_path", "runtime/state/qqq_v2_diag.jsonl")
        
        self.memex = StateMemex(self.ctx, self.persistent_state, self.state_lock)
        await self.memex.initialize()

        self.fabric = MarketFabric(self.ctx, self.persistent_state, self.state_lock, symbols=["QQQ", "QQEW", "TQQQ", "SQQQ"])
        self.indicator_lib = IndicatorLib()
        self.history_provider = HistoryProvider(self.ctx, self.persistent_state, self.state_lock)
        self.analyzer = StrategicAnalyzer(self.ctx, self.persistent_state, self.state_lock, self.params)
        obs_path = self.params.extras.get("obs_snapshot_path", "runtime/state/qqq_v2_obs.json")
        self.obs = Observability(self.ctx, self.persistent_state, self.state_lock, obs_path)
        
        self.guard = ExecutionGuard(self.ctx, self.persistent_state, self.state_lock, max_trades=1)
        self.shield = ProtectiveShield(self.ctx, self.persistent_state, self.state_lock)
        self.auditor = RealityAuditor(self.ctx, self.persistent_state, self.state_lock, audit_interval=1.2, max_miss_count=8)
        
        atr_seed_path = self.params.extras.get("atr_seed_path", "runtime/state/qqq_intraday_atr_seed.json")
        self.qqq_fabric = QQQDataFabric(self.ctx, self.persistent_state, self.fabric, self.history_provider, atr_seed_path)
        self.gatekeeper = EntryGatekeeper(self.ctx, self.persistent_state, self.fabric, self.indicator_lib)
        self.supervisor = PositionSupervisor(self.ctx, self.persistent_state, self.guard, self.shield)

        await self.fabric.initialize(); await self.qqq_fabric.initialize()
        await self.guard.sync_with_broker(); await self.auditor.start(flatten_callback=self.supervisor.flatten)

        # V1: 0.5s loop for extreme responsiveness
        loop_interval = float(self.params.extras.get("loop_interval_sec", 0.5))
        self._main_task = asyncio.create_task(self._run_loop(loop_interval))
        self.logger.info("qqq_v2_initialized", version=self.persistent_state["_version"])

    async def _run_loop(self, interval: float) -> None:
        while True:
            try:
                now = datetime.now(ET)
                await self.guard.sync_with_broker()
                await self._run_loop_step(now)
                self.fabric.check_health(now, timeout_sec=120)
            except RuntimeError as e:
                self.logger.error("fail_fast_suicide", error=str(e))
                os.kill(os.getpid(), signal.SIGTERM); break
            except Exception as e:
                self.logger.error("qqq_v2_loop_error", error=str(e), exc_info=True)
                await asyncio.sleep(1.0)
            await asyncio.sleep(interval)

    async def _run_loop_step(self, now: datetime) -> None:
        day = now.strftime("%Y-%m-%d")
        if self.persistent_state.get("_day") != day:
            self.logger.info("DAILY_RESET_SEQUENCE")
            async with self.state_lock:
                self.fabric.reset(); self.analyzer.reset_daily()
                for k in list(self.persistent_state.keys()):
                    if k not in {"_version", "_diag_path", "_day", "_init_at", "atr_seed_path"}: self.persistent_state.pop(k)
                
                # Re-initialize mandatory trading keys
                self.persistent_state["_day"] = day
                self.persistent_state["is_traded"] = False
                self.persistent_state["success_count"] = 0
                self.persistent_state["pending_decision_id"] = None
                self.persistent_state["atr_pre"] = {"TQQQ": None, "SQQQ": None}
            await self.qqq_fabric.initialize()

        if self.persistent_state.get("data_integrity_warning"):
            if time(9, 30) <= now.time() <= time(9, 45): await self.qqq_fabric.initialize()

        await self.fabric.ingest()
        
        # --- TOP LEVEL HEARTBEAT ---
        q_last = self.fabric.get_last_price("QQQ"); e_last = self.fabric.get_last_price("QQEW")
        q_vwap = self.fabric.get_anchored_vwap("QQQ"); e_vwap = self.fabric.get_anchored_vwap("QQEW")
        v3m_s = self.fabric.get_v3m("QQQ"); age = self.fabric.get_freshness_age("QQQ", now)
        await self.gatekeeper.emit_diagnostic(now, q_last, e_last, q_vwap, e_vwap, v3m_s, 0, 0, 0, False, False, age, 0)

        # B. Strategic Analysis (REPLAY EVERY TICK)
        for ts_ms, price in self.fabric.consume_new_ticks("QQQ"):
            qv = self.fabric.get_anchored_vwap("QQQ")
            dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=ET)
            if time(9, 30) <= dt.time() < time(9, 45):
                self.analyzer.update_toxic_count(price, qv)
                self.analyzer.collect_seed_sample(self.fabric.get_v3m("QQQ", at_ts_ms=ts_ms))
            if dt.time() >= time(9, 45) and not self.persistent_state.get("amp_locked"):
                self.analyzer.lock_at_0945(price, self.persistent_state.get("price_base", 0.0))

        # C. Evaluation Phase (Entry)
        if self.guard.can_enter():
            allowed, reason, side, signal_px = await self.gatekeeper.can_enter(now)
            if allowed and side and self.guard.can_enter():
                exec_symbol = "TQQQ" if side == "LONG" else "SQQQ"
                atr_pre = self.persistent_state["atr_pre"].get(exec_symbol, 0.35)
                await self.supervisor.enter(exec_symbol, side, self._calc_qty(signal_px), atr_pre, signal_px)

        # D. Management Phase (FULL FREQUENCY - V1 Port L2000)
        # Position management MUST run at loop freq (0.5s) for stop/target hits.
        if self.persistent_state.get("is_traded"):
            exec_symbol = self.persistent_state.get("exec_symbol")
            last_px = self.fabric.get_last_price(exec_symbol); qv = self.fabric.get_anchored_vwap("QQQ")
            atr_live = self.indicator_lib.atr(list(self.fabric.bars_5m[exec_symbol]))
            # PositionSupervisor.manage handles internal throttling for ModifyOrder.
            await self.supervisor.manage(last_px, qv, atr_live)

        if now.time() >= time(15, 59):
            if self.persistent_state.get("is_traded"): await self.supervisor.flatten("EOD_FORCE_FLATTEN")
            if self.persistent_state.get("last_atr_persist_day") != day:
                self.qqq_fabric.persist_atr_seeds(); self.persistent_state["last_atr_persist_day"] = day
        
        self.obs.write_snapshot(now, {"guard": self.guard, "shield": self.shield, "auditor": self.auditor, "gatekeeper": self.gatekeeper})
        await self.memex.save()

    def _calc_qty(self, price: float) -> int:
        if price <= 0: return 0
        return int((float(self.params.extras.get("notional_usd", 10000.0)) + 1e-6) // price)

    async def on_features(self, symbol: str, features: dict) -> None: pass
    async def on_order_update(self, update: OrderUpdate) -> None: await self.guard.on_order_update(update)
    async def on_position_update(self, position: PositionSnapshot) -> None:
        if abs(position.pos_qty) == 0:
            if self.persistent_state.get("is_flattening"): self.persistent_state["is_flattening"] = False
            self.persistent_state["exec_symbol"] = self.persistent_state["side"] = None
            # V1 Port Fix: Once a trade succeeds, is_traded STAYS True until midnight.
            # Only success_count and memory state matter for today's quota.

    async def on_shutdown(self, reason: str) -> None:
        await self.auditor.stop(); await self.fabric.stop()
        if datetime.now(ET).time() > time(15, 55): self.qqq_fabric.persist_atr_seeds()
        await self.memex.save(extra_metadata={"shutdown_reason": reason})
        self.logger.info("qqq_v2_shutdown", reason=reason)
