"""Bull Put Spread strategy with entry and TP/SL management.

Uses the options data provider to place credit put spreads (BAG orders on IBKR).
Defaults to transmit=False and stub backend; flip to ibkr + transmit=True when ready.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import time
from datetime import datetime, time as time_et, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from libs.config import config as CFG
from libs.db.clickhouse import ClickHouseDB
from libs.strategy import StrategyAlgorithm
from services.market.options_data import OptionContract, create_options_provider
from services.portfolio.account_service import get_account_service
from services.portfolio.order_monitor import get_order_monitor, init_global_order_monitor
from services.portfolio.option_trade_repo import OptionTradeRepository

ET = ZoneInfo("America/New_York")


class BullPutSpreadStrategy(StrategyAlgorithm):
    """Bull put spread (credit put) with trend-aware entry and TP/SL management."""

    async def initialize(self) -> None:
        cfg = self.params.extras.get("bull_put", {})

        # Core config
        self.symbol = cfg.get("symbol", "QQQ")
        self.target_delta = float(cfg.get("short_delta", -0.25))
        self.min_credit = float(cfg.get("min_credit", 0.4))
        self.take_profit_pct = float(cfg.get("take_profit_pct", 0.5))
        self.stop_loss_pct = float(cfg.get("stop_loss_pct", 2.0))
        self.qty = int(cfg.get("qty", 1))
        self.scan_interval_sec = int(cfg.get("scan_interval_sec", 300))
        self.min_dte = int(cfg.get("min_dte", 14))
        self.max_dte = int(cfg.get("max_dte", 35))
        self.transmit = bool(cfg.get("transmit", False))
        self.entry_fill_timeout_sec = int(cfg.get("entry_fill_timeout_sec", 900))

        # Fair-value / microstructure controls
        self.tv_edge_min = float(cfg.get("tv_edge_min", 0.0))  # minimum edge over theoretical value
        self.max_iv_skew = float(cfg.get("max_iv_skew", 0.2))  # max allowed (long_iv - short_iv) absolute skew
        self.entry_reprice_interval_sec = int(cfg.get("entry_reprice_interval_sec", 10))
        self.entry_price_step = float(cfg.get("entry_price_step", 0.05))  # minimum price change to trigger modify
        self.entry_max_concession = float(cfg.get("entry_max_concession", 0.3))  # max $ credit concession from initial
        self.entry_imbalance_sticky = float(cfg.get("entry_imbalance_sticky", 0.6))  # bid/ask imbalance to stay firm

        # Trend/strike controls
        self.otm_pct_up = float(cfg.get("otm_pct_up", 0.04))
        self.otm_pct_down = float(cfg.get("otm_pct_down", 0.05))
        self.width_candidates = cfg.get("width_candidates", [5.0, 10.0])
        self.max_spread_bps = float(cfg.get("max_spread_bps", 1000.0))  # 1000 bps = 10%
        self.min_oi = int(cfg.get("min_open_interest", 100))
        self.vix_halt = float(cfg.get("vix_halt", 30.0))
        self.max_feature_age_sec = int(cfg.get("max_feature_age_sec", 180))
        self.max_margin_usd = cfg.get("max_margin_usd")  # optional guard per trade

        # Trading window (ET)
        start_et = cfg.get("trade_window_start_et", "10:00")
        end_et = cfg.get("trade_window_end_et", "15:45")
        self.trade_window_start = self._parse_et(start_et)
        self.trade_window_end = self._parse_et(end_et)

        # Provider setup
        provider_cfg = cfg.get("provider", {})
        backend = provider_cfg.get("backend", "stub")
        provider_kwargs = {k: v for k, v in provider_cfg.items() if k != "backend"}

        # Account/connection defaults from env/config for IBKR backend
        self.account = provider_cfg.get("account") or CFG.get("ib.account", "") or os.getenv("IB_ACCOUNT", "")
        if backend == "ibkr":
            ib_cfg = CFG.ib_config
            provider_kwargs.setdefault("host", ib_cfg["host"])
            provider_kwargs.setdefault("port", ib_cfg["port"])
            provider_kwargs.setdefault("client_id", ib_cfg["client_id"])

        self.options = create_options_provider(backend, **provider_kwargs)
        self.options_backend = backend
        self.order_monitor = None
        if backend == "ibkr":
            try:
                await init_global_order_monitor()
                self.order_monitor = get_order_monitor()
            except Exception:
                self.order_monitor = None
        self.runs_dir = Path(os.getenv("RUNS_DIR", "runs/ai_trader"))
        self.ch_table = cfg.get("clickhouse_table", "bull_put_spread_events")
        self.ch: Optional[ClickHouseDB] = None
        try:
            self.ch = ClickHouseDB()
        except Exception:
            self.ch = None
        self.trade_repo = OptionTradeRepository()

        self.position: Optional[Dict[str, Any]] = None
        self.pending_position: Optional[Dict[str, Any]] = None  # 等待fill确认的订单
        self.pending_exit: Optional[Dict[str, Any]] = None  # 预留：等待 exit fill 的状态记录
        self._open_by_conid_side: Dict[int, Dict[str, set[int]]] = {}
        self._order_index: Dict[int, Dict[str, Any]] = {}
        self._order_index_lock = asyncio.Lock()
        self._pending_reversal: Dict[str, Dict[str, Any]] = {}
        self._conid_cache: Dict[str, int] = {}
        self._order_guard_ready = False
        self._order_guard_bound = False
        self._ib_adapter = None
        self._entry_waiting_cancel = False
        self._conid_lock_enabled = bool(cfg.get("conid_lock_enabled", False))
        self._conid_lock_ttl_ms = int(cfg.get("conid_lock_ttl_ms", 3000))
        self._conid_lock_owner = cfg.get("conid_lock_owner") or f"{os.getenv('HOSTNAME', 'host')}-{os.getpid()}"
        self._conid_lock = None
        self.last_scan_ts = 0.0
        self._daily_ctx_ttl_sec = int(cfg.get("daily_ctx_ttl_sec", 300))
        self._daily_ctx_days = int(cfg.get("daily_ctx_days", 260))
        self._daily_ctx_cache: Dict[str, Any] = {"ts": 0.0, "data": {}}

        await self.ctx.subscribe_features([self.symbol], bar_sec=60)
        self.log.info(
            "bull_put_initialized",
            symbol=self.symbol,
            target_delta=self.target_delta,
            min_credit=self.min_credit,
            qty=self.qty,
            backend=backend,
            transmit=self.transmit,
            width_candidates=self.width_candidates,
            otm_up=self.otm_pct_up,
            otm_down=self.otm_pct_down,
            max_spread_bps=self.max_spread_bps,
            trade_window_start=start_et,
            trade_window_end=end_et,
        )
        if backend == "ibkr":
            await self._init_order_guard()

    async def on_features(self, symbol: str, features: Dict[str, Any]) -> None:
        now = time.time()
        if now - self.last_scan_ts < self.scan_interval_sec:
            return
        self.last_scan_ts = now

        if not self._within_trade_window():
            return

        if not self._fresh_enough(features):
            self.log.info("bull_put_stale_features", symbol=symbol)
            self._audit_event("skip_stale_features")
            return

        # VIX halt if available
        vix = features.get("vix") or features.get("vix_close")
        if vix and vix >= self.vix_halt:
            self.log.info("bull_put_vix_halt", vix=vix, vix_halt=self.vix_halt)
            self._audit_event("skip_vix_halt", vix=vix)
            return

        # Ghost position防护: 检查pending订单状态
        if self.pending_position and not self.position:
            await self._verify_pending_position()
            # Still pending: do not attempt another entry this cycle.
            if self.pending_position and not self.position:
                return

        if not self.position:
            if self._entry_waiting_cancel:
                return
            await self._try_enter(features)
        else:
            await self._manage_position()

    # ------------------------------------------------------------------ Entry
    async def _try_enter(self, features: Dict[str, Any]) -> None:
        daily_ctx = await self._get_daily_context_cached()
        merged_features = dict(features)
        for k, v in daily_ctx.items():
            if merged_features.get(k) is None:
                merged_features[k] = v

        price = self._feature_price(merged_features)
        ma50 = merged_features.get("ma50") or merged_features.get("sma_50") or merged_features.get("ma_50")
        ma100 = merged_features.get("ma100") or merged_features.get("sma_100") or merged_features.get("ma_100")
        ma200 = merged_features.get("ma200") or merged_features.get("sma_200") or merged_features.get("ma_200")

        self._log_data_snapshot(merged_features, price, ma50, ma100, ma200)

        trend = self._classify_trend(price, ma50, ma100, ma200, merged_features)
        if trend == "UNKNOWN":
            self.log.info("bull_put_skip_unknown_trend", price=price, ma50=ma50, ma100=ma100, ma200=ma200)
            self._audit_event("skip_unknown_trend", price=price)
            return
        if trend == "DOWN_FAST":
            self.log.info("bull_put_skip_down_fast", price=price, ma50=ma50, ma100=ma100, ma200=ma200)
            self._audit_event("skip_down_fast", price=price)
            return

        chain = await self._fetch_chain()
        if not chain:
            self.log.warning("bull_put_no_chain", symbol=self.symbol)
            self._audit_event("skip_no_chain", trend=trend)
            return

        legs = self._select_legs(chain, price, ma100, trend)
        if not legs:
            self.log.debug("bull_put_no_candidates", symbol=self.symbol, trend=trend)
            self._audit_event("skip_no_candidate", trend=trend)
            return
        short_leg, long_leg, spread_used = legs
        credit = self._spread_mid(short_leg, long_leg)
        if credit is None or credit < self.min_credit:
            self.log.info(
                "bull_put_credit_too_low",
                credit=credit,
                min_credit=self.min_credit,
                short=short_leg.strike,
                long=long_leg.strike,
                trend=trend,
            )
            self._audit_event(
                "skip_low_credit",
                credit=credit,
                min_credit=self.min_credit,
                short_strike=short_leg.strike,
                long_strike=long_leg.strike,
                trend=trend,
            )
            return

        # Optional: theoretical value guard and IV skew guard to avoid selling
        # obviously mispriced or skew-unfavourable spreads.
        tv_credit = self._estimate_spread_tv(short_leg, long_leg, price)
        if tv_credit is not None and self.tv_edge_min > 0.0:
            edge = credit - tv_credit
            if edge < self.tv_edge_min:
                self.log.info(
                    "bull_put_edge_too_low",
                    credit=credit,
                    tv_credit=tv_credit,
                    edge=edge,
                    tv_edge_min=self.tv_edge_min,
                )
                self._audit_event(
                    "skip_low_edge",
                    credit=credit,
                    tv_credit=tv_credit,
                    edge=edge,
                    tv_edge_min=self.tv_edge_min,
                )
                return

        if not self._iv_skew_ok(short_leg, long_leg):
            self.log.info(
                "bull_put_iv_skew_unfavourable",
                short_iv=short_leg.implied_vol,
                long_iv=long_leg.implied_vol,
                max_iv_skew=self.max_iv_skew,
            )
            self._audit_event(
                "skip_iv_skew_unfavourable",
                short_iv=short_leg.implied_vol,
                long_iv=long_leg.implied_vol,
                max_iv_skew=self.max_iv_skew,
            )
            return

        # Margin guard (approx: (width - credit)*100*qty)
        margin_req = max(spread_used - credit, 0) * 100 * self.qty
        if self.max_margin_usd and margin_req > float(self.max_margin_usd):
            self.log.info(
                "bull_put_margin_guard",
                margin_req=margin_req,
                max_margin=self.max_margin_usd,
                width=spread_used,
                credit=credit,
            )
            self._audit_event(
                "skip_margin_guard",
                margin_req=margin_req,
                max_margin=self.max_margin_usd,
            )
            return

        # IB available funds check if IB backend
        if self.options_backend == "ibkr":
            try:
                account_service = get_account_service()
                af = await account_service.available_funds()
                if af is not None and margin_req > af:
                    self.log.info(
                        "bull_put_ib_funds_guard",
                        margin_req=margin_req,
                        available_funds=af,
                    )
                    self._audit_event(
                        "skip_available_funds_ib",
                        margin_req=margin_req,
                        available_funds=af,
                    )
                    return
            except Exception as exc:
                self.log.warning("bull_put_ib_funds_check_failed", error=str(exc))

        self.log.info(
            "bull_put_enter",
            symbol=self.symbol,
            expiry=short_leg.expiry_date,
            short=short_leg.strike,
            long=long_leg.strike,
            width=spread_used,
            credit=credit,
            qty=self.qty,
            transmit=self.transmit,
            trend=trend,
        )

        async def _on_entry_result(res: Dict[str, Any]) -> None:
            self._entry_waiting_cancel = False
            # transmit=false: only prepare/preview, do not create pending/position.
            if not self.transmit:
                self._audit_event(
                    "enter_prepared",
                    expiry=short_leg.expiry_date,
                    short_strike=short_leg.strike,
                    long_strike=long_leg.strike,
                    width=spread_used,
                    credit=credit,
                    qty=self.qty,
                    transmit=self.transmit,
                    trend=trend,
                    result=res,
                )
                await self.trade_repo.insert_trade(
                    action="OPEN_PREPARED",
                    symbol=self.symbol,
                    expiry=short_leg.expiry_date,
                    short_strike=short_leg.strike,
                    long_strike=long_leg.strike,
                    width=spread_used,
                    credit=credit,
                    qty=self.qty,
                    transmit=self.transmit,
                    meta=res,
                )
                return

            # Ghost position防护: 检查订单是否成功提交
            order_id = None
            if res:
                order_id = res.get("order_id") or res.get("orderId")
            if not order_id:
                self.log.warning("bull_put_no_order_id", result=res)
                self._audit_event("skip_no_order_id", result=res)
                return

            # 标记为pending状态 (等待Fill确认 + 活跃价格管理)
            self.pending_position = {
                "entry_credit": credit,
                "tv_credit": tv_credit,
                "current_limit": credit,
                "expiry": short_leg.expiry_date,
                "short": short_leg,
                "long": long_leg,
                "opened_at": datetime.now(ET).isoformat(),
                "order": res,
                "order_id": int(order_id),
                "width": spread_used,
                "trend": trend,
                "status": "pending",  # 等待fill确认
            }

            await self.trade_repo.insert_trade(
                action="OPEN_SUBMITTED",
                symbol=self.symbol,
                expiry=short_leg.expiry_date,
                short_strike=short_leg.strike,
                long_strike=long_leg.strike,
                width=spread_used,
                credit=credit,
                qty=self.qty,
                transmit=self.transmit,
                meta=res,
                order_id=int(order_id),
                order_status=(res.get("status") if res else None),
            )
            self._audit_event(
                "enter_submitted",
                order_id=int(order_id),
                expiry=short_leg.expiry_date,
                short_strike=short_leg.strike,
                long_strike=long_leg.strike,
                width=spread_used,
                credit=credit,
                qty=self.qty,
                transmit=self.transmit,
                trend=trend,
                order_status=(res.get("status") if res else None),
                state="PENDING",
            )

            # Non-IB backends: if the provider reports Filled immediately, promote to a real position now.
            if self.options_backend != "ibkr":
                if (res.get("status") or "").strip() == "Filled":
                    self.position = {**self.pending_position, "state": "OPEN", "filled_ts": datetime.now(ET).isoformat()}
                    await self._record_state_change("OPEN", "stub_fill", self.position)
                    self.pending_position = None
                    await self.trade_repo.insert_trade(
                        action="OPEN_FILLED",
                        symbol=self.symbol,
                        expiry=self.position["expiry"],
                        short_strike=self.position["short"].strike,
                        long_strike=self.position["long"].strike,
                        width=self.position.get("width") or (self.position["short"].strike - self.position["long"].strike),
                        credit=float(self.position.get("entry_credit") or 0.0),
                        qty=self.qty,
                        transmit=self.transmit,
                        meta=res,
                    )
                    await self._place_exit_orders(short_leg, long_leg, spread_used, float(self.position.get("entry_credit") or 0.0))
                    self._audit_event("entry_filled", order_id=int(order_id), filled_price=None, state="OPEN")
                    return

            # Optional: event-driven wait + active price management to reduce latency
            # and avoid long-lived stale orders.
            if self.order_monitor:
                import asyncio

                asyncio.create_task(self._await_entry_terminal(int(order_id)))
                if self.entry_reprice_interval_sec > 0:
                    asyncio.create_task(self._manage_entry_order(int(order_id)))

        res = await self._place_vertical_spread_guarded(
            context="entry",
            short_leg=short_leg,
            long_leg=long_leg,
            side="SELL",
            limit_credit=credit,
            oca_group=None,
            on_result=_on_entry_result,
        )
        if (res or {}).get("status") == "pending_cancel":
            self._entry_waiting_cancel = True

    # ------------------------------------------------------------------ Manage
    async def _manage_position(self) -> None:
        if not self.position:
            return
        if self.position.get("state") and self.position.get("state") != "OPEN":
            return

        # If exit orders were placed (OCA), prefer monitoring them to avoid double-close.
        if self.options_backend == "ibkr" and hasattr(self.options, "get_order_status"):
            tp_id = self.position.get("tp_order_id")
            sl_id = self.position.get("sl_order_id")
            if tp_id or sl_id:
                for name, oid in (("tp", tp_id), ("sl", sl_id)):
                    if not oid:
                        continue
                    try:
                        st = await self.options.get_order_status(int(oid))
                    except Exception:
                        st = None
                    if st and st.get("status") == "Filled":
                        self.log.info(
                            "bull_put_exit_filled",
                            which=name,
                            order_id=oid,
                            avg_fill_price=st.get("avg_fill_price"),
                        )
                        self._audit_event(
                            f"exit_{name}_filled",
                            order_id=int(oid),
                            avg_fill_price=st.get("avg_fill_price"),
                        )
                        await self.trade_repo.insert_trade(
                            action=f"EXIT_{name.upper()}_FILLED",
                            symbol=self.symbol,
                            expiry=self.position["expiry"],
                            short_strike=self.position["short"].strike,
                            long_strike=self.position["long"].strike,
                            width=self.position["short"].strike - self.position["long"].strike,
                            credit=float(st.get("avg_fill_price") or 0.0),
                            qty=self.qty,
                            transmit=self.transmit,
                            meta=st,
                        )
                        self.position = None
                        self.pending_exit = None
                        return
                # Exit orders exist but none filled yet: don't issue a manual close to avoid duplicates.
                return
        if self.pending_exit:
            return
        if self.pending_exit:
            # Exit orders already staged; wait for their fill handling instead of mid-based close.
            return
        mid = await self._current_mid(self.position["short"], self.position["long"])
        if mid is None:
            self.log.warning("bull_put_mid_missing", symbol=self.symbol)
            return
        entry = self.position["entry_credit"]
        tp_trigger = entry * (1 - self.take_profit_pct)
        sl_trigger = entry * (1 + self.stop_loss_pct)

        if mid <= tp_trigger:
            self.log.info("bull_put_take_profit", entry=entry, mid=mid, tp=tp_trigger)
            self._audit_event("take_profit_triggered", entry_credit=entry, mid=mid, tp=tp_trigger)
            await self._close(mid)
        elif mid >= sl_trigger:
            self.log.info("bull_put_stop_loss", entry=entry, mid=mid, sl=sl_trigger)
            self._audit_event("stop_loss_triggered", entry_credit=entry, mid=mid, sl=sl_trigger)
            await self._close(mid)

    async def _close(self, limit_debit: float) -> None:
        if not self.position:
            return
        short_leg: OptionContract = self.position["short"]
        long_leg: OptionContract = self.position["long"]
        async def _on_close_result(res: Dict[str, Any]) -> None:
            self.log.info("bull_put_close_submitted", result=res)
            close_order_id = (res or {}).get("order_id") or (res or {}).get("orderId")
            await self.trade_repo.insert_trade(
                action="CLOSE_SUBMITTED",
                symbol=self.symbol,
                expiry=short_leg.expiry_date,
                short_strike=short_leg.strike,
                long_strike=long_leg.strike,
                width=short_leg.strike - long_leg.strike,
                credit=limit_debit,
                qty=self.qty,
                transmit=self.transmit,
                meta=res,
                order_id=int(close_order_id) if close_order_id else None,
                order_status=(res.get("status") if res else None),
            )
            if self.position is not None:
                self.position["state"] = "CLOSING"
                await self._record_state_change("CLOSING", "manual_close", self.position)
            self._audit_event(
                "close_submitted",
                result=res,
                limit_debit=limit_debit,
                order_id=(res or {}).get("order_id") or (res or {}).get("orderId"),
            )
            if self.order_monitor and close_order_id:
                import asyncio

                asyncio.create_task(
                    self._await_close_fill(
                        order_id=int(close_order_id),
                        limit_debit=limit_debit,
                        width=short_leg.strike - long_leg.strike,
                        expiry=short_leg.expiry_date,
                    )
                )
            else:
                snapshot = self.position
                self.position = None
                await self._record_state_change("CLOSED", "manual_close_no_monitor", snapshot)

        await self._place_vertical_spread_guarded(
            context="close",
            short_leg=short_leg,
            long_leg=long_leg,
            side="BUY",
            limit_credit=limit_debit,
            oca_group=self.position.get("oca_group") if self.position else None,
            on_result=_on_close_result,
        )

    # ------------------------------------------------------------------ Order guard helpers
    async def _init_order_guard(self) -> None:
        if self.options_backend != "ibkr":
            return
        try:
            from services.exec.adapters.ib_adapter import get_ib_adapter

            adapter = get_ib_adapter()
            if not adapter.is_connected():
                await adapter.start()
            self._ib_adapter = adapter
            if not self._order_guard_bound and getattr(adapter, "ib", None):
                adapter.ib.orderStatusEvent += self._on_order_status_event
                if hasattr(adapter.ib, "openOrderEvent"):
                    adapter.ib.openOrderEvent += self._on_open_order_event
                self._order_guard_bound = True
            await self._sync_open_orders()
            self._order_guard_ready = True
        except Exception as exc:
            self._order_guard_ready = False
            self.log.warning("bull_put_order_guard_init_failed", error=str(exc))

        if self._conid_lock_enabled and self._conid_lock is None:
            try:
                import redis

                self._conid_lock = redis.from_url(CFG.redis_config, decode_responses=True)
            except Exception as exc:
                self._conid_lock = None
                self._conid_lock_enabled = False
                self.log.warning("bull_put_conid_lock_unavailable", error=str(exc))

    def _on_open_order_event(self, trade: Any) -> None:
        asyncio.create_task(self._handle_order_update(trade))

    def _on_order_status_event(self, trade: Any) -> None:
        asyncio.create_task(self._handle_order_update(trade))

    async def _handle_order_update(self, trade: Any) -> None:
        order = getattr(trade, "order", None)
        contract = getattr(trade, "contract", None)
        if not order or not contract:
            return
        order_id = getattr(order, "orderId", None)
        if order_id is None:
            return
        status = getattr(getattr(trade, "orderStatus", None), "status", None) or "UNKNOWN"
        legs = self._extract_legs_from_trade(contract, getattr(order, "action", None))
        if not legs:
            return
        await self._update_open_index(int(order_id), legs, status)
        await self._process_pending_reversals(int(order_id), status)

    async def _sync_open_orders(self) -> None:
        if not self._ib_adapter or not getattr(self._ib_adapter, "ib", None):
            return
        req_all = getattr(self._ib_adapter.ib, "reqAllOpenOrdersAsync", None)
        if callable(req_all):
            await req_all()
        else:
            self._ib_adapter.ib.reqAllOpenOrders()
        for trade in self._ib_adapter.ib.openTrades():
            await self._handle_order_update(trade)

    def _extract_legs_from_trade(self, contract: Any, order_action: Optional[str]) -> List[Tuple[int, str]]:
        action = (order_action or "").upper()
        legs: List[Tuple[int, str]] = []
        combo_legs = getattr(contract, "comboLegs", None)
        if combo_legs:
            for leg in combo_legs:
                leg_action = getattr(leg, "action", None)
                conid = getattr(leg, "conId", None)
                if not leg_action or not conid:
                    continue
                leg_action = leg_action.upper()
                if action == "SELL":
                    if leg_action == "BUY":
                        leg_action = "SELL"
                    elif leg_action == "SELL":
                        leg_action = "BUY"
                legs.append((int(conid), leg_action))
            return legs
        conid = getattr(contract, "conId", None)
        if conid and action in {"BUY", "SELL"}:
            legs.append((int(conid), action))
        return legs

    def _is_open_status(self, status: Optional[str]) -> bool:
        s = (status or "").strip().upper().replace(" ", "")
        return s not in {"FILLED", "CANCELLED", "CANCELED", "INACTIVE", "REJECTED", "APICANCELLED"}

    async def _update_open_index(self, order_id: int, legs: List[Tuple[int, str]], status: str) -> None:
        async with self._order_index_lock:
            prev = self._order_index.get(order_id)
            if prev:
                for conid, side in prev.get("legs", []):
                    bucket = self._open_by_conid_side.get(conid)
                    if bucket and side in bucket:
                        bucket[side].discard(order_id)
                        if not bucket[side]:
                            bucket.pop(side, None)
                    if bucket == {}:
                        self._open_by_conid_side.pop(conid, None)

            if self._is_open_status(status):
                for conid, side in legs:
                    side_u = side.upper()
                    bucket = self._open_by_conid_side.setdefault(conid, {"BUY": set(), "SELL": set()})
                    if side_u not in bucket:
                        bucket[side_u] = set()
                    bucket[side_u].add(order_id)

            self._order_index[order_id] = {"legs": legs, "status": status}

    async def _can_place_order(self, legs: List[Tuple[int, str]]) -> Tuple[bool, Optional[Tuple[int, str, List[int]]]]:
        if not self._order_guard_ready:
            return True, None
        async with self._order_index_lock:
            for conid, side in legs:
                opp = "SELL" if side == "BUY" else "BUY"
                conflict_ids = list(self._open_by_conid_side.get(conid, {}).get(opp, set()))
                if conflict_ids:
                    return False, (conid, opp, conflict_ids)
        return True, None

    async def _cancel_conflict_orders(self, order_ids: List[int]) -> None:
        for oid in sorted(set(int(x) for x in order_ids if x is not None)):
            await self._cancel_order_safely(int(oid))

    async def _process_pending_reversals(self, order_id: int, status: str) -> None:
        if self._is_open_status(status):
            return
        if not self._pending_reversal:
            return
        for task_id, task in list(self._pending_reversal.items()):
            orders_to_cancel = task.get("orders_to_cancel", set())
            if order_id in orders_to_cancel:
                orders_to_cancel.discard(order_id)
                if not orders_to_cancel:
                    asyncio.create_task(self._execute_pending_reversal(task_id))

    async def _execute_pending_reversal(self, task_id: str) -> None:
        task = self._pending_reversal.get(task_id)
        if not task:
            return
        ok, conflict = await self._can_place_order(task["legs"])
        if not ok and conflict:
            _, _, conflict_ids = conflict
            await self._cancel_conflict_orders(conflict_ids)
            task["orders_to_cancel"].update(int(x) for x in conflict_ids)
            return
        res = await self.options.place_vertical_spread(**task["submit_kwargs"])
        await task["on_result"](res)
        self._pending_reversal.pop(task_id, None)

    async def _place_vertical_spread_guarded(
        self,
        *,
        context: str,
        short_leg: OptionContract,
        long_leg: OptionContract,
        side: str,
        limit_credit: float,
        oca_group: Optional[str],
        on_result: Callable[[Dict[str, Any]], Any],
    ) -> Dict[str, Any]:
        submit_kwargs = {
            "underlying": self.symbol,
            "expiry": short_leg.expiry_date,
            "short_strike": short_leg.strike,
            "long_strike": long_leg.strike,
            "qty": self.qty,
            "side": side,
            "right": "P",
            "limit_credit": limit_credit,
            "account": self.account,
            "transmit": self.transmit,
            "oca_group": oca_group,
        }

        if self.options_backend != "ibkr" or not self.transmit:
            res = await self.options.place_vertical_spread(**submit_kwargs)
            await on_result(res)
            return res

        legs = await self._build_guard_legs(short_leg, long_leg, side)
        if not legs:
            res = await self.options.place_vertical_spread(**submit_kwargs)
            await on_result(res)
            return res

        lock_conids = [leg[0] for leg in legs]
        if not await self._acquire_conid_locks(lock_conids):
            self.log.warning("bull_put_conid_lock_busy", context=context, conids=lock_conids)
            self._audit_event("conid_lock_busy", context=context, conids=lock_conids)
            return {"status": "lock_busy", "conids": lock_conids}

        try:
            ok, conflict = await self._can_place_order(legs)
            if ok:
                res = await self.options.place_vertical_spread(**submit_kwargs)
                await on_result(res)
                return res

            conid, opp, conflict_ids = conflict
            await self._cancel_conflict_orders(conflict_ids)
            task_id = f"{context}-{int(time.time() * 1000)}-{conid}"
            self._pending_reversal[task_id] = {
                "orders_to_cancel": set(int(x) for x in conflict_ids),
                "legs": legs,
                "submit_kwargs": submit_kwargs,
                "on_result": on_result,
                "context": context,
            }
            self.log.warning(
                "bull_put_order_conflict",
                context=context,
                conid=conid,
                opposite_side=opp,
                conflict_orders=conflict_ids,
            )
            self._audit_event(
                "order_conflict",
                context=context,
                conid=conid,
                opposite_side=opp,
                conflict_orders=conflict_ids,
            )
            return {"status": "pending_cancel", "conflict_orders": conflict_ids}
        finally:
            await self._release_conid_locks(lock_conids)

    async def _build_guard_legs(
        self,
        short_leg: OptionContract,
        long_leg: OptionContract,
        side: str,
    ) -> List[Tuple[int, str]]:
        conids = await self._resolve_spread_conids(short_leg, long_leg)
        if not conids:
            return []
        short_action = side.upper()
        long_action = "SELL" if short_action == "BUY" else "BUY"
        return [(conids[0], short_action), (conids[1], long_action)]

    async def _resolve_spread_conids(
        self, short_leg: OptionContract, long_leg: OptionContract
    ) -> Optional[Tuple[int, int]]:
        if not self._ib_adapter or not getattr(self._ib_adapter, "ib", None):
            return None
        short_conid = await self._get_conid(self.symbol, short_leg.expiry_date, short_leg.strike, short_leg.right)
        long_conid = await self._get_conid(self.symbol, long_leg.expiry_date, long_leg.strike, long_leg.right)
        if not short_conid or not long_conid:
            return None
        return int(short_conid), int(long_conid)

    async def _get_conid(self, underlying: str, expiry: str, strike: float, right: str) -> Optional[int]:
        key = f"{underlying}:{expiry}:{strike}:{right}"
        if key in self._conid_cache:
            return self._conid_cache[key]
        try:
            from ib_insync import Option

            expiry_ib = expiry.replace("-", "")
            opt = Option(underlying, expiry_ib, float(strike), right, "SMART")
            conn_manager = None
            try:
                from services.ibkr.ib_connection_manager import get_ib_connection_manager

                conn_manager = get_ib_connection_manager()
            except Exception:
                conn_manager = None
            governor_key = f"{underlying}:{expiry}:{strike}:{right}"
            if conn_manager is not None and hasattr(conn_manager, "allow_market_request"):
                decision = conn_manager.allow_market_request(
                    kind="option_contract_qualification",
                    key=governor_key,
                    requester=str(getattr(self, "strategy_id", "bull_put_spread")),
                )
                if not decision.allowed:
                    return None
            await self._ib_adapter.ib.qualifyContractsAsync(opt)
            conid = getattr(opt, "conId", None)
            if conn_manager is not None and hasattr(conn_manager, "record_market_request_result"):
                conn_manager.record_market_request_result(
                    kind="option_contract_qualification",
                    key=governor_key,
                    ok=bool(conid),
                    reason=None if conid else "conid_missing",
                )
            if conid:
                self._conid_cache[key] = int(conid)
                return int(conid)
        except Exception:
            return None
        return None

    async def _acquire_conid_locks(self, conids: List[int]) -> bool:
        if not self._conid_lock_enabled:
            return True
        if not self._conid_lock:
            return False
        acquired: List[int] = []
        for conid in conids:
            key = f"lock:conid:{conid}"
            ok = self._conid_lock.set(key, self._conid_lock_owner, nx=True, px=self._conid_lock_ttl_ms)
            if ok:
                acquired.append(conid)
            else:
                await self._release_conid_locks(acquired)
                return False
        return True

    async def _release_conid_locks(self, conids: List[int]) -> None:
        if not self._conid_lock_enabled or not self._conid_lock:
            return
        if not conids:
            return
        lua = "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) else return 0 end"
        for conid in conids:
            key = f"lock:conid:{conid}"
            try:
                self._conid_lock.eval(lua, 1, key, self._conid_lock_owner)
            except Exception:
                continue

    # ------------------------------------------------------------------ Helpers
    async def _fetch_chain(self) -> List[OptionContract]:
        target_dtes = list(range(self.min_dte, self.max_dte + 1, 5))
        try:
            chain = await self.options.get_chain(
                underlying=self.symbol,
                dte_candidates=target_dtes,
                right="P",
            )
            self.log.debug("bull_put_chain_fetched", symbol=self.symbol, candidates=len(chain or []))
            return chain or []
        except Exception as exc:
            self.log.error("bull_put_chain_error", error=str(exc))
            return []

    def _select_legs(
        self,
        chain: List[OptionContract],
        price: Optional[float],
        ma100: Optional[float],
        trend: str,
    ) -> Optional[tuple[OptionContract, OptionContract, float]]:
        now = datetime.now()
        puts: List[OptionContract] = []
        for c in chain:
            try:
                expiry = datetime.strptime(c.expiry_date, "%Y-%m-%d")
                dte = (expiry - now).days
            except Exception:
                continue
            if dte < self.min_dte or dte > self.max_dte or c.right != "P" or c.delta is None:
                continue
            if c.open_interest is not None and c.open_interest < self.min_oi:
                continue
            if c.spread_bps and c.spread_bps > self.max_spread_bps:
                continue
            puts.append(c)

        self.log.debug("bull_put_put_candidates", symbol=self.symbol, count=len(puts))

        if not puts or price is None:
            return None

        # Determine target short strike bound based on trend
        if trend == "UP":
            target_strike = price * (1 - self.otm_pct_up)
            max_short_strike = target_strike
        else:  # DOWN_OK
            target_strike = price * (1 - self.otm_pct_down)
            max_short_strike = min(target_strike, ma100) if ma100 else target_strike

        # Filter shorts under bound
        short_candidates = [p for p in puts if p.strike <= max_short_strike]
        if not short_candidates:
            return None

        # Pick short leg by delta proximity within bound
        short_leg = min(short_candidates, key=lambda c: abs(abs(c.delta) - abs(self.target_delta)))
        self.log.debug("bull_put_short_selected", short_strike=short_leg.strike, delta=short_leg.delta)

        # Try width candidates, pick best credit
        best: Optional[tuple[OptionContract, OptionContract, float, float]] = None  # (short,long,width,credit)
        for w in self.width_candidates:
            target_long_strike = short_leg.strike - float(w)
            long_leg = min(
                (p for p in puts if abs(p.strike - target_long_strike) < 0.11),
                key=lambda c: abs(c.strike - target_long_strike),
                default=None,
            )
            if not long_leg:
                continue
            if long_leg.spread_bps and long_leg.spread_bps > self.max_spread_bps:
                continue
            credit = self._spread_mid(short_leg, long_leg)
            if credit is None or credit <= 0:
                continue
            if best is None or credit > best[3]:
                best = (short_leg, long_leg, w, credit)
                self.log.debug(
                    "bull_put_candidate",
                    width=w,
                    short_strike=short_leg.strike,
                    long_strike=long_leg.strike,
                    credit=credit,
                )

        if best:
            self.log.info(
                "bull_put_leg_selected",
                short_strike=best[0].strike,
                long_strike=best[1].strike,
                width=best[2],
                credit=best[3],
            )
            return best[0], best[1], best[2]
        return None

    def _spread_mid(self, short_leg: OptionContract, long_leg: OptionContract) -> Optional[float]:
        try:
            if short_leg.mid <= 0 or long_leg.mid <= 0:
                return None
            return short_leg.mid - long_leg.mid
        except Exception:
            return None

    async def _current_mid(self, short_leg: OptionContract, long_leg: OptionContract) -> Optional[float]:
        chain = await self._fetch_chain()
        if not chain:
            return None

        def _match(target: OptionContract) -> Optional[OptionContract]:
            for c in chain:
                if c.right == target.right and c.expiry_date == target.expiry_date and math.isclose(c.strike, target.strike):
                    return c
            return None

        s = _match(short_leg)
        l = _match(long_leg)
        if not s or not l:
            return None
        return self._spread_mid(s, l)

    async def _place_exit_orders(
        self,
        short_leg: OptionContract,
        long_leg: OptionContract,
        width: float,
        entry_credit: float,
    ) -> None:
        """Submit TP/SL combo orders via OCA."""
        if not self.position:
            return
        if self.take_profit_pct <= 0 and self.stop_loss_pct <= 0:
            return

        oca_group = f"bullput-{self.symbol}-{int(time.time())}"
        self.position["oca_group"] = oca_group

        tp_price = max(entry_credit * (1 - self.take_profit_pct), 0.01)
        sl_price = entry_credit * (1 + self.stop_loss_pct)

        exit_state: Dict[str, Any] = {
            "tp_order_id": None,
            "sl_order_id": None,
            "width": width,
            "pending": 0,
            "tp_ready": False,
            "sl_ready": False,
            "finalized": False,
        }

        async def _maybe_finalize_exit_orders() -> None:
            if exit_state["finalized"]:
                return
            if not (exit_state["tp_ready"] and exit_state["sl_ready"]):
                return
            exit_state["finalized"] = True
            if self.position is not None:
                self.position["state"] = "CLOSING"
                await self._record_state_change("CLOSING", "exit_orders_submitted", self.position)
            if self.order_monitor:
                import asyncio

                if exit_state["tp_order_id"]:
                    asyncio.create_task(self._await_exit_order("TP", int(exit_state["tp_order_id"]), width))
                if exit_state["sl_order_id"]:
                    asyncio.create_task(self._await_exit_order("SL", int(exit_state["sl_order_id"]), width))
            self._audit_event(
                "exit_orders_submitted",
                oca_group=oca_group,
                tp_price=tp_price,
                sl_price=sl_price,
                transmit=self.transmit,
            )
            self.log.info(
                "bull_put_exit_orders",
                oca_group=oca_group,
                tp_price=tp_price,
                sl_price=sl_price,
                transmit=self.transmit,
            )

        async def _on_tp_result(tp_res: Dict[str, Any]) -> None:
            tp_id = (tp_res or {}).get("order_id") or (tp_res or {}).get("orderId")
            if tp_id and self.position is not None:
                self.position["tp_order_id"] = int(tp_id)
            exit_state["tp_order_id"] = int(tp_id) if tp_id else None
            exit_state["tp_ready"] = True
            if exit_state["pending"] > 0:
                exit_state["pending"] -= 1
            await self.trade_repo.insert_trade(
                action="EXIT_TP_ORDER",
                symbol=self.symbol,
                expiry=short_leg.expiry_date,
                short_strike=short_leg.strike,
                long_strike=long_leg.strike,
                width=width,
                credit=tp_price,
                qty=self.qty,
                transmit=self.transmit,
                meta=tp_res,
                order_id=int(tp_id) if tp_id else None,
                order_status=(tp_res.get("status") if tp_res else None),
            )
            await _maybe_finalize_exit_orders()

        async def _on_sl_result(sl_res: Dict[str, Any]) -> None:
            sl_id = (sl_res or {}).get("order_id") or (sl_res or {}).get("orderId")
            if sl_id and self.position is not None:
                self.position["sl_order_id"] = int(sl_id)
            exit_state["sl_order_id"] = int(sl_id) if sl_id else None
            exit_state["sl_ready"] = True
            if exit_state["pending"] > 0:
                exit_state["pending"] -= 1
            await self.trade_repo.insert_trade(
                action="EXIT_SL_ORDER",
                symbol=self.symbol,
                expiry=short_leg.expiry_date,
                short_strike=short_leg.strike,
                long_strike=long_leg.strike,
                width=width,
                credit=sl_price,
                qty=self.qty,
                transmit=self.transmit,
                meta=sl_res,
                order_id=int(sl_id) if sl_id else None,
                order_status=(sl_res.get("status") if sl_res else None),
            )
            await _maybe_finalize_exit_orders()

        self.pending_exit = exit_state

        tp_res = await self._place_vertical_spread_guarded(
            context="exit_tp",
            short_leg=short_leg,
            long_leg=long_leg,
            side="BUY",
            limit_credit=tp_price,
            oca_group=oca_group,
            on_result=_on_tp_result,
        )
        if (tp_res or {}).get("status") == "pending_cancel":
            exit_state["pending"] += 1

        sl_res = await self._place_vertical_spread_guarded(
            context="exit_sl",
            short_leg=short_leg,
            long_leg=long_leg,
            side="BUY",
            limit_credit=sl_price,
            oca_group=oca_group,
            on_result=_on_sl_result,
        )
        if (sl_res or {}).get("status") == "pending_cancel":
            exit_state["pending"] += 1

        if exit_state["pending"] == 0 and not exit_state["tp_ready"] and not exit_state["sl_ready"]:
            self.pending_exit = None

    # ------------------------------------------------------------------ Trend & Time helpers
    def _classify_trend(
        self,
        price: Optional[float],
        ma50: Optional[float],
        ma100: Optional[float],
        ma200: Optional[float],
        features: Dict[str, Any],
    ) -> str:
        if price is None or ma50 is None:
            return "UNKNOWN"

        fast_drop = features.get("pct_change_1d")
        if fast_drop is not None and fast_drop <= -2.0:
            return "DOWN_FAST"

        vix = features.get("vix") or features.get("vix_close")
        if vix and vix >= self.vix_halt:
            return "DOWN_FAST"

        if price > ma50:
            return "UP"

        # price below ma50 but not fast drop
        if ma200 and price < ma200:
            return "DOWN_FAST"
        return "DOWN_OK"

    def _within_trade_window(self) -> bool:
        now_et = datetime.now(ET).time()
        return self.trade_window_start <= now_et <= self.trade_window_end

    def _parse_et(self, tstr: str) -> time_et:
        try:
            h, m = [int(x) for x in tstr.split(":")]
            return time_et(hour=h, minute=m, tzinfo=ET)
        except Exception:
            return time_et(hour=10, minute=0, tzinfo=ET)

    def _feature_price(self, features: Dict[str, Any]) -> Optional[float]:
        for key in ("close", "last", "price", "mid"):
            if key in features and features[key]:
                try:
                    return float(features[key])
                except Exception:
                    continue
        return None

    def _estimate_spread_tv(
        self,
        short_leg: OptionContract,
        long_leg: OptionContract,
        underlying_price: Optional[float],
    ) -> Optional[float]:
        """Estimate theoretical credit for the vertical using a simple BS put model.

        Falls back to mid spread when IV or underlying inputs are missing.
        """
        try:
            if underlying_price is None:
                return self._spread_mid(short_leg, long_leg)
            if short_leg.implied_vol is None or long_leg.implied_vol is None:
                return self._spread_mid(short_leg, long_leg)
            expiry = datetime.strptime(short_leg.expiry_date, "%Y-%m-%d")
            now = datetime.now(ET)
            t_days = (expiry - now).days
            if t_days <= 0:
                return self._spread_mid(short_leg, long_leg)
            t_years = t_days / 365.0

            def _n(x: float) -> float:
                return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

            def _bs_put(s: float, k: float, t: float, sigma: float, r: float = 0.0) -> float:
                if t <= 0 or sigma <= 0:
                    return max(k - s, 0.0)
                d1 = (math.log(s / k) + (r + 0.5 * sigma * sigma) * t) / (sigma * math.sqrt(t))
                d2 = d1 - sigma * math.sqrt(t)
                return k * math.exp(-r * t) * _n(-d2) - s * _n(-d1)

            s = float(underlying_price)
            tv_short = _bs_put(s, float(short_leg.strike), t_years, float(short_leg.implied_vol))
            tv_long = _bs_put(s, float(long_leg.strike), t_years, float(long_leg.implied_vol))
            return tv_short - tv_long
        except Exception:
            return self._spread_mid(short_leg, long_leg)

    def _iv_skew_ok(self, short_leg: OptionContract, long_leg: OptionContract) -> bool:
        """Check IV skew between short and long legs is not obviously unfavourable.

        For a bull put, we generally do not want the long leg IV to be dramatically
        higher than the short leg IV (overpaying for protection). We allow a small
        skew tolerance configured via max_iv_skew.
        """
        try:
            if short_leg.implied_vol is None or long_leg.implied_vol is None:
                return True
            short_iv = float(short_leg.implied_vol)
            long_iv = float(long_leg.implied_vol)
            # Allow long_iv slightly higher than short_iv, but cap the difference.
            return (long_iv - short_iv) <= self.max_iv_skew
        except Exception:
            return True

    def _extract_ts_us(self, features: Dict[str, Any]) -> Optional[int]:
        """Best-effort extraction of timestamp (microseconds) from features."""

        def _to_us(val: Any) -> Optional[int]:
            if val is None:
                return None
            if isinstance(val, (int, float)):
                # Epoch microseconds are ~1e15; epoch seconds are ~1e9.
                if val > 1e12:
                    return int(val)
                if val > 1e9:
                    return int(val * 1e6)
                return None
            if isinstance(val, datetime):
                dt = val if val.tzinfo else val.replace(tzinfo=timezone.utc)
                return int(dt.timestamp() * 1e6)
            if isinstance(val, str):
                s = val.strip()
                try:
                    if s.endswith("Z"):
                        s = s[:-1] + "+00:00"
                    dt = datetime.fromisoformat(s)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    return int(dt.timestamp() * 1e6)
                except Exception:
                    return None
            return None

        for key in ("ts_bar_end_us", "timestamp_us", "ts_us", "ts_end_us", "ts_start_us", "ts"):
            if key in features:
                ts_us = _to_us(features.get(key))
                if ts_us:
                    return ts_us
        return None

    async def _get_daily_context_cached(self) -> Dict[str, Any]:
        """Fetch daily MA/pct_change and latest VIX from ClickHouse with caching."""
        now = time.time()
        cache_ts = float(self._daily_ctx_cache.get("ts") or 0.0)
        if now - cache_ts < self._daily_ctx_ttl_sec:
            return dict(self._daily_ctx_cache.get("data") or {})

        ctx: Dict[str, Any] = {}
        if not self.ch:
            self._daily_ctx_cache = {"ts": now, "data": ctx}
            return ctx

        try:
            rows = self.ch.query(
                """
                SELECT ts_start, close
                FROM bars_1d_ibkr
                WHERE symbol = {symbol:String}
                ORDER BY ts_start DESC
                LIMIT {limit:UInt32}
                """,
                {"symbol": self.symbol, "limit": int(self._daily_ctx_days)},
            )
            closes: List[float] = []
            last_ts = None
            if rows:
                last_ts = rows[0].get("ts_start")
            for r in reversed(rows or []):
                try:
                    c = r.get("close")
                    if c is None:
                        continue
                    closes.append(float(c))
                except Exception:
                    continue

            def _sma(n: int) -> Optional[float]:
                if len(closes) < n:
                    return None
                return sum(closes[-n:]) / float(n)

            ctx["ma50"] = _sma(50)
            ctx["ma100"] = _sma(100)
            ctx["ma200"] = _sma(200)
            if len(closes) >= 2 and closes[-2] != 0:
                ctx["pct_change_1d"] = (closes[-1] / closes[-2] - 1.0) * 100.0
            ctx["daily_last_ts"] = last_ts
        except Exception as exc:
            self.log.warning("bull_put_daily_ctx_failed", error=str(exc))

        try:
            vix_rows = self.ch.query(
                """
                SELECT ts, vix
                FROM refs_vix_ticks
                ORDER BY ts DESC
                LIMIT 1
                """
            )
            if vix_rows:
                try:
                    ctx["vix"] = float(vix_rows[0].get("vix"))
                    ctx["vix_ts"] = vix_rows[0].get("ts")
                except Exception:
                    pass
        except Exception as exc:
            self.log.warning("bull_put_vix_fetch_failed", error=str(exc))

        self._daily_ctx_cache = {"ts": now, "data": ctx}
        self.log.info(
            "bull_put_daily_context",
            symbol=self.symbol,
            ma50=ctx.get("ma50"),
            ma100=ctx.get("ma100"),
            ma200=ctx.get("ma200"),
            pct_change_1d=ctx.get("pct_change_1d"),
            vix=ctx.get("vix"),
            daily_last_ts=ctx.get("daily_last_ts"),
        )
        return dict(ctx)

    def _log_data_snapshot(
        self,
        features: Dict[str, Any],
        price: Optional[float],
        ma50: Optional[float],
        ma100: Optional[float],
        ma200: Optional[float],
    ) -> None:
        ts_us = self._extract_ts_us(features)
        age = None
        if ts_us:
            age = time.time() - ts_us / 1e6
        self.log.info(
            "bull_put_data_snapshot",
            price=price,
            ma50=ma50,
            ma100=ma100,
            ma200=ma200,
            vix=features.get("vix") or features.get("vix_close"),
            pct_change_1d=features.get("pct_change_1d"),
            age_sec=age,
            max_age_sec=self.max_feature_age_sec,
        )

    def _fresh_enough(self, features: Dict[str, Any]) -> bool:
        """Check feature age if timestamp available."""
        ts_us = self._extract_ts_us(features)
        if ts_us is None:
            return True
        age_sec = (time.time() - ts_us / 1e6)
        self.log.debug(
            "bull_put_feature_age",
            ts=ts_us,
            age_sec=age_sec,
            max_feature_age_sec=self.max_feature_age_sec,
        )
        return age_sec <= self.max_feature_age_sec

    def _audit_event(self, event: str, **payload: Any) -> None:
        """Write audit event to runs/ai_trader/<date>/bull_put_spread.jsonl."""
        try:
            ts = datetime.now(timezone.utc).isoformat()
            base = self.runs_dir
            day_dir = base / datetime.now(timezone.utc).date().isoformat()
            day_dir.mkdir(parents=True, exist_ok=True)
            # Normalize audit payload with standard keys.
            record = {
                "event": event,
                "ts": ts,
                "symbol": self.symbol,
                "order_id": payload.pop("order_id", None),
                "order_status": payload.pop("order_status", None),
                "state": payload.pop("state", None),
                "filled_price": payload.pop("filled_price", None),
                "reason": payload.pop("reason", None),
                **payload,
            }
            audit_file = day_dir / "bull_put_spread.jsonl"
            with audit_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            if self.ch:
                try:
                    ts_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    row = [
                        ts_dt.replace(tzinfo=None),
                        event,
                        self.symbol,
                        json.dumps(payload, ensure_ascii=False),
                    ]
                    self.ch.insert(self.ch_table, [row], column_names=["ts", "event", "symbol", "payload"])
                except Exception:
                    pass
        except Exception:
            pass
    async def _verify_pending_position(self) -> None:
        """验证pending订单状态,防止ghost position."""
        if not self.pending_position:
            return

        order_id = self.pending_position.get("order_id")
        if not order_id:
            self.log.warning("bull_put_pending_no_order_id")
            self.pending_position = None
            return

        opened_at_str = self.pending_position.get("opened_at")
        if not opened_at_str:
            self.pending_position = None
            return

        try:
            opened_at = datetime.fromisoformat(opened_at_str)
            now = datetime.now(ET)
            elapsed_seconds = (now - opened_at).total_seconds()

            # 增强验证: 查询IBKR订单状态 (如果backend是ibkr)
            if self.options_backend == "ibkr" and hasattr(self.options, 'get_order_status'):
                try:
                    order_status_info = await self.options.get_order_status(order_id)

                    if order_status_info:
                        status = order_status_info.get("status", "Unknown")

                        # 订单成交 → 转为真实position
                        if status == "Filled":
                            self.log.info(
                                "bull_put_order_filled",
                                order_id=order_id,
                                filled_qty=order_status_info.get("filled_qty"),
                                avg_price=order_status_info.get("avg_fill_price"),
                            )

                            # 升级为真实position
                            filled_price = order_status_info.get("avg_fill_price")
                            entry_credit = float(filled_price) if filled_price else float(self.pending_position.get("entry_credit") or 0.0)
                            self.position = {
                                **self.pending_position,
                                "state": "OPEN",
                                "entry_credit": entry_credit,
                                "filled_price": filled_price,
                                "filled_ts": now.isoformat(),
                            }
                            self.pending_position = None

                            self._audit_event(
                                "entry_filled",
                                order_id=order_id,
                                filled_price=order_status_info.get("avg_fill_price"),
                            )
                            await self.trade_repo.insert_trade(
                                action="OPEN_FILLED",
                                symbol=self.symbol,
                                expiry=self.position["expiry"],
                                short_strike=self.position["short"].strike,
                                long_strike=self.position["long"].strike,
                                width=self.position.get("width") or (self.position["short"].strike - self.position["long"].strike),
                                credit=entry_credit,
                                qty=self.qty,
                                transmit=self.transmit,
                                meta=order_status_info,
                                order_id=int(order_id),
                                order_status="Filled",
                                filled_price=float(order_status_info.get("avg_fill_price") or 0.0) if order_status_info.get("avg_fill_price") is not None else None,
                                filled_qty=float(order_status_info.get("filled_qty") or 0.0) if order_status_info.get("filled_qty") is not None else None,
                            )
                            # Only place exit orders after entry is confirmed filled.
                            await self._place_exit_orders(
                                short_leg=self.position["short"],
                                long_leg=self.position["long"],
                                width=self.position.get("width") or (self.position["short"].strike - self.position["long"].strike),
                                entry_credit=entry_credit,
                            )
                            return

                        # 订单被拒绝/取消 → 立即清除pending
                        elif status in ["Cancelled", "Rejected", "Inactive", "ApiCancelled"]:
                            self.log.warning(
                                "bull_put_order_rejected",
                                order_id=order_id,
                                status=status,
                            )
                            self._audit_event(
                                "entry_rejected",
                                order_id=order_id,
                                status=status,
                                state="NONE",
                            )
                            await self.trade_repo.insert_trade(
                                action="OPEN_NOT_FILLED",
                                symbol=self.symbol,
                                expiry=self.pending_position.get("expiry", ""),
                                short_strike=self.pending_position["short"].strike,
                                long_strike=self.pending_position["long"].strike,
                                width=self.pending_position.get("width") or (self.pending_position["short"].strike - self.pending_position["long"].strike),
                                credit=float(self.pending_position.get("entry_credit") or 0.0),
                                qty=self.qty,
                                transmit=self.transmit,
                                meta=order_status_info,
                                order_id=int(order_id),
                                order_status=status,
                            )
                            self.pending_position = None
                            return

                        # 仍在Submitted/PreSubmitted → 继续等待
                        # (will fall through to timeout check below)

                except Exception as query_exc:
                    self.log.error("bull_put_query_status_error",
                                 order_id=order_id,
                                 error=str(query_exc))
                    # 查询失败,继续使用timeout机制

            # Fallback: 超过阈值仍pending,认为订单未成交，清除pending防止 ghost
            if elapsed_seconds > float(getattr(self, "entry_fill_timeout_sec", 300)):
                self.log.warning(
                    "bull_put_pending_timeout",
                    order_id=order_id,
                    elapsed_sec=elapsed_seconds,
                )
                self._audit_event(
                    "pending_order_timeout",
                    order_id=order_id,
                    elapsed_sec=elapsed_seconds,
                    state="NONE",
                )
                # 超时仍未成交,主动尝试撤单,避免IB端长期挂单
                await self._cancel_order_safely(int(order_id))
                await self.trade_repo.insert_trade(
                    action="OPEN_TIMEOUT",
                    symbol=self.symbol,
                    expiry=self.pending_position.get("expiry", ""),
                    short_strike=self.pending_position["short"].strike,
                    long_strike=self.pending_position["long"].strike,
                    width=self.pending_position.get("width") or (self.pending_position["short"].strike - self.pending_position["long"].strike),
                    credit=float(self.pending_position.get("entry_credit") or 0.0),
                    qty=self.qty,
                    transmit=self.transmit,
                    meta={"order_id": order_id, "elapsed_sec": elapsed_seconds},
                    order_id=int(order_id),
                    order_status="TIMEOUT",
                )
                # 清除pending,防止ghost position
                self.pending_position = None

        except Exception as exc:
            self.log.error("bull_put_verify_pending_error", error=str(exc))
            self.pending_position = None

    async def _await_entry_terminal(self, order_id: int) -> None:
        """Wait for entry order to reach terminal status and transition state accordingly."""
        if not self.order_monitor:
            return
        try:
            final = await self.order_monitor.wait_for_fill(order_id, timeout_sec=self.entry_fill_timeout_sec)
        except Exception as exc:
            self.log.warning("bull_put_wait_for_fill_failed", order_id=order_id, error=str(exc))
            return

        # Another cycle may have already resolved/cleared pending.
        if not self.pending_position or int(self.pending_position.get("order_id", -1)) != int(order_id):
            return

        status = (final.status or "").strip()
        if status == "Filled":
            filled_price = final.avg_fill_price
            entry_credit = float(filled_price) if filled_price else float(self.pending_position.get("entry_credit") or 0.0)
            now = datetime.now(ET)
            self.position = {
                **self.pending_position,
                "state": "OPEN",
                "entry_credit": entry_credit,
                "filled_price": filled_price,
                "filled_ts": now.isoformat(),
            }
            await self._record_state_change("OPEN", "entry_filled", self.position)
            self.pending_position = None
            self._audit_event("entry_filled", order_id=order_id, filled_price=filled_price, filled_qty=final.filled_qty, state="OPEN")
            await self.trade_repo.insert_trade(
                action="OPEN_FILLED",
                symbol=self.symbol,
                expiry=self.position["expiry"],
                short_strike=self.position["short"].strike,
                long_strike=self.position["long"].strike,
                width=self.position.get("width") or (self.position["short"].strike - self.position["long"].strike),
                credit=entry_credit,
                qty=self.qty,
                transmit=self.transmit,
                meta={
                    "order_id": order_id,
                    "status": status,
                    "avg_fill_price": final.avg_fill_price,
                    "filled_qty": final.filled_qty,
                },
            )
            await self._place_exit_orders(
                short_leg=self.position["short"],
                long_leg=self.position["long"],
                width=self.position.get("width") or (self.position["short"].strike - self.position["long"].strike),
                entry_credit=entry_credit,
            )
            self.position["state"] = "OPEN"
            return

        # Not filled
        if status == "TIMEOUT":
            # Entry did not fill within timeout window – proactively request cancellation on IBKR.
            await self._cancel_order_safely(int(order_id))
            action = "OPEN_TIMEOUT"
        else:
            action = "OPEN_NOT_FILLED"
        await self.trade_repo.insert_trade(
            action=action,
            symbol=self.symbol,
            expiry=self.pending_position.get("expiry", ""),
            short_strike=self.pending_position["short"].strike,
            long_strike=self.pending_position["long"].strike,
            width=self.pending_position.get("width") or (self.pending_position["short"].strike - self.pending_position["long"].strike),
            credit=float(self.pending_position.get("entry_credit") or 0.0),
            qty=self.qty,
            transmit=self.transmit,
            meta={"order_id": order_id, "final_status": status, "avg_fill_price": final.avg_fill_price},
        )
        self._audit_event("entry_not_filled", order_id=order_id, final_status=status, state="NONE")
        self.pending_position = None

    async def _await_exit_order(self, name: str, order_id: int, width: float) -> None:
        """Wait for TP/SL exit order to reach terminal status via OrderMonitor."""
        if not self.order_monitor:
            return
        final = await self.order_monitor.wait_for_fill(order_id, timeout_sec=self.entry_fill_timeout_sec)
        status = (final.status or "").strip().upper()
        action_prefix = f"EXIT_{name.upper()}"

        if status == "FILLED":
            await self.trade_repo.insert_trade(
                action=f"{action_prefix}_FILLED",
                symbol=self.symbol,
                expiry=self.position["expiry"] if self.position else "",
                short_strike=self.position["short"].strike if self.position else 0.0,
                long_strike=self.position["long"].strike if self.position else 0.0,
                width=width,
                credit=float(final.avg_fill_price or 0.0),
                qty=self.qty,
                transmit=self.transmit,
                meta={"order_id": order_id, "status": status, "avg_fill_price": final.avg_fill_price},
            )
            self._audit_event(
                f"exit_{name.lower()}_filled",
                order_id=order_id,
                avg_fill_price=final.avg_fill_price,
                state="CLOSED",
            )
            if self.position:
                await self._record_state_change("CLOSED", f"{name}_filled", self.position)
            self.position = None
            self.pending_exit = None
            return

        await self.trade_repo.insert_trade(
            action=f"{action_prefix}_NOT_FILLED",
            symbol=self.symbol,
            expiry=self.position["expiry"] if self.position else "",
            short_strike=self.position["short"].strike if self.position else 0.0,
            long_strike=self.position["long"].strike if self.position else 0.0,
            width=width,
            credit=0.0,
            qty=self.qty,
            transmit=self.transmit,
            meta={"order_id": order_id, "status": status},
        )
        self._audit_event(
            f"exit_{name.lower()}_not_filled", order_id=order_id, final_status=status, state="CLOSING"
        )
        self.pending_exit = None
        if self.position:
            self.position["state"] = "OPEN"
            await self._record_state_change("OPEN", f"{name}_not_filled", self.position)

    async def _manage_entry_order(self, order_id: int) -> None:
        """Active entry price management: periodically re-anchor limit to current mid/TV within guards.

        This runs alongside OrderMonitor.wait_for_fill and exits when:
        - pending_position is cleared, or
        - position is promoted to OPEN, or
        - hard timeout elapsed (actual cancellation is handled elsewhere).
        """
        if self.options_backend != "ibkr":
            return
        if self.entry_reprice_interval_sec <= 0:
            return

        start_ts = time.time()
        try:
            from services.exec.adapters.ib_adapter import get_ib_adapter

            adapter = get_ib_adapter()
        except Exception:
            return

        while True:
            await asyncio.sleep(self.entry_reprice_interval_sec)

            # Stop if pending is gone or another order took over.
            pending = self.pending_position
            if not pending or int(pending.get("order_id", -1)) != int(order_id):
                return

            # If entry already promoted to a live position, stop price management.
            if self.position and int(self.position.get("order_id", -1)) == int(order_id):
                return

            elapsed = time.time() - start_ts
            if elapsed >= float(self.entry_fill_timeout_sec):
                # Hard timeout enforcement is handled by wait_for_fill/verify_pending; do not modify further.
                return

            short_leg: OptionContract = pending["short"]
            long_leg: OptionContract = pending["long"]
            mid = await self._current_mid(short_leg, long_leg)
            if mid is None or mid <= 0:
                continue

            initial_credit = float(pending.get("entry_credit") or 0.0)
            current_limit = float(pending.get("current_limit") or initial_credit)
            tv_val = pending.get("tv_credit")
            tv_credit = float(tv_val) if tv_val is not None else initial_credit

            # Compute allowable price band.
            tv_floor = tv_credit + self.tv_edge_min if self.tv_edge_min > 0.0 else tv_credit
            min_allowed = max(self.min_credit, tv_floor, initial_credit - self.entry_max_concession)
            max_allowed = initial_credit

            # Propose new limit anchored to current mid but clipped to band.
            target = max(min_allowed, min(mid, max_allowed))

            # Optional microstructure heuristic: if books show strong bid imbalance for both legs,
            # stay firm (do not walk credit down below current_limit).
            imbalance = None
            total_legs = 0
            for leg in (short_leg, long_leg):
                bid_size = getattr(leg, "bid_size", None) or 0.0
                ask_size = getattr(leg, "ask_size", None) or 0.0
                if bid_size > 0 and ask_size > 0:
                    total_legs += 1
                    imbalance_leg = (bid_size - ask_size) / (bid_size + ask_size)
                    imbalance = (imbalance or 0.0) + imbalance_leg
            if imbalance is not None and total_legs > 0:
                imbalance /= total_legs
                if imbalance >= self.entry_imbalance_sticky and target < current_limit:
                    # Strong demand; do not reduce credit in this case.
                    continue

            if abs(target - current_limit) < self.entry_price_step:
                continue

            try:
                await adapter.modify(order_id=int(order_id), limit_px=float(target))
                if self.pending_position and int(self.pending_position.get("order_id", -1)) == int(order_id):
                    self.pending_position["current_limit"] = float(target)
                self.log.info(
                    "bull_put_entry_reprice",
                    order_id=int(order_id),
                    old_limit=current_limit,
                    new_limit=target,
                    mid=mid,
                    elapsed_sec=int(elapsed),
                )
                self._audit_event(
                    "entry_reprice",
                    order_id=int(order_id),
                    old_limit=current_limit,
                    new_limit=target,
                    mid=mid,
                    elapsed_sec=int(elapsed),
                )
            except Exception as exc:
                self.log.warning("bull_put_entry_reprice_failed", order_id=int(order_id), error=str(exc))

    async def _cancel_order_safely(self, order_id: int) -> None:
        """Best-effort cancel for IBKR orders when we decide a leg should be abandoned.

        This is used on entry TIMEOUT paths to avoid long-lived orphan orders on IBKR.
        """
        if self.options_backend != "ibkr":
            return
        try:
            from services.exec.adapters.ib_adapter import get_ib_adapter

            adapter = get_ib_adapter()
            await adapter.cancel(int(order_id))
            self.log.info("bull_put_order_cancel_requested", order_id=int(order_id))
        except Exception as exc:
            self.log.warning("bull_put_order_cancel_failed", order_id=int(order_id), error=str(exc))

    async def _record_state_change(self, state: str, source: str, position: Optional[Dict[str, Any]] = None) -> None:
        """Persist state transitions to DB and audit log."""
        pos = position or self.position or self.pending_position
        if not pos:
            return
        short_leg = pos.get("short")
        long_leg = pos.get("long")
        width = pos.get("width")
        if width is None and short_leg and long_leg:
            try:
                width = float(getattr(short_leg, "strike") - getattr(long_leg, "strike"))
            except Exception:
                width = 0.0
        await self.trade_repo.insert_trade(
            action=f"STATE_{state}",
            symbol=self.symbol,
            expiry=pos.get("expiry", ""),
            short_strike=getattr(short_leg, "strike", 0.0) if short_leg else 0.0,
            long_strike=getattr(long_leg, "strike", 0.0) if long_leg else 0.0,
            width=float(width or 0.0),
            credit=float(pos.get("entry_credit") or 0.0),
            qty=self.qty,
            transmit=self.transmit,
            meta={"source": source},
            order_id=int(pos.get("order_id")) if pos.get("order_id") else None,
            order_status=state,
        )
        self._audit_event("state_change", order_id=pos.get("order_id"), state=state, source=source)

    async def _await_close_fill(self, order_id: int, limit_debit: float, width: float, expiry: str) -> None:
        """Wait for manual close order to finish via OrderMonitor."""
        if not self.order_monitor:
            self.position = None
            return
        final = await self.order_monitor.wait_for_fill(order_id, timeout_sec=self.entry_fill_timeout_sec)
        status = (final.status or "").strip().upper()
        await self.trade_repo.insert_trade(
            action="CLOSE_FINAL",
            symbol=self.symbol,
            expiry=expiry,
            short_strike=self.position["short"].strike if self.position else 0.0,
            long_strike=self.position["long"].strike if self.position else 0.0,
            width=width,
            credit=limit_debit,
            qty=self.qty,
            transmit=self.transmit,
            meta={"order_id": order_id, "final_status": status, "avg_fill_price": final.avg_fill_price},
        )
        self._audit_event("close_final", order_id=order_id, final_status=status, state="CLOSED")
        if self.position:
            await self._record_state_change("CLOSED", "close_final", self.position)
        self.position = None
        self.pending_exit = None
