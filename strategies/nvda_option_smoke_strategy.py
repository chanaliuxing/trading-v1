from __future__ import annotations

import os
from datetime import datetime, timedelta, time
from typing import Any, Dict, Optional

from libs.options.internal_symbol import format_internal_opt_symbol, parse_internal_opt_symbol
from libs.strategy.registry import register_strategy
from services.market.options_quote_store import latest_quote_status, query_option_rows
from strategies.qqq_0dte_opt_live_strategy import _PositionState
from strategies.options_universal import (
    OptionBarContext,
    OptionQuoteRequest,
    OptionQuoteSelection,
    UniversalOptionLiveStrategy,
)
from strategies.options_universal.plugin import OptionStrategyPlugin


def _parse_hms(raw: Any, default: time) -> time:
    try:
        value = str(raw or "").strip()
        return time.fromisoformat(value) if value else default
    except Exception:
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value if value is not None else default)
    except Exception:
        return default


def _quote_age_seconds(row: Dict[str, Any], ref_now: datetime) -> float:
    for key in ("created_at", "ts"):
        value = row.get(key)
        if isinstance(value, datetime):
            try:
                src = value.astimezone(ref_now.tzinfo) if value.tzinfo and ref_now.tzinfo else value
                return max(0.0, (ref_now - src).total_seconds())
            except Exception:
                continue
    return 9_999.0


def _expiry_ymd(value: Any, now: datetime, dte: int) -> str:
    if hasattr(value, "strftime"):
        return value.strftime("%Y%m%d")
    raw = str(value or "").strip()
    if raw:
        return raw.replace("-", "")[:8]
    return (now + timedelta(days=max(0, int(dte)))).strftime("%Y%m%d")


class _NVDASmokeBasePlugin(OptionStrategyPlugin):
    component_name = "nvda_option_smoke_plugin"

    def apply_default_extras(self, extras: dict) -> None:
        extras.setdefault("underlying", "NVDA")
        extras.setdefault("risk_symbol", "OPT:NVDA:SMOKE")
        extras.setdefault("contracts", 1)
        extras.setdefault("max_trades_per_day", 999)
        extras.setdefault("second_trade_only_if_first_bad_exit", False)
        extras.setdefault("reserve_trade_after_time", "")
        extras.setdefault("ab_time_start", "09:35:00")
        extras.setdefault("ab_time_end", "15:50:00")
        extras.setdefault("no_new_entry_after", "15:50:00")
        extras.setdefault("force_flatten_time", "15:55:00")
        extras.setdefault("spread_max_pct", 0.35)
        extras.setdefault("entry_quote_lookback_min", 3)
        extras.setdefault("entry_quote_max_age_sec", 120.0)
        extras.setdefault("entry_quote_allow_ch_fallback", False)
        extras.setdefault("allow_stale_entry_quotes", False)
        extras.setdefault("dte_min", 0)
        extras.setdefault("dte_max", 7)
        extras.setdefault("dte_preferred", 1)
        extras.setdefault("rfe_primary_enabled", True)
        extras.setdefault("paper_smoke_only", True)
        extras.setdefault("allow_live_smoke_entry", False)
        extras.setdefault("allow_degraded_smoke_entry", False)
        extras.setdefault("shared_cap_group_id", "NVDA_OPTION_SMOKE")
        extras.setdefault("shared_cap_group_limit_usd", 2_000)

    def _entry_allowed_now(self, strategy: UniversalOptionLiveStrategy, now: datetime) -> bool:
        extras = strategy.params.extras or {}
        start = _parse_hms(extras.get("ab_time_start"), time(9, 35))
        end = _parse_hms(extras.get("ab_time_end"), time(15, 50))
        no_new_after = _parse_hms(extras.get("no_new_entry_after"), time(15, 50))
        return start <= now.time() <= end and now.time() < no_new_after

    async def _has_working_option_order(self, strategy: UniversalOptionLiveStrategy) -> bool:
        try:
            return bool(strategy._has_strategy_owned_open_option_orders(await strategy.ctx.open_orders()))
        except Exception:
            return False

    def _live_smoke_entry_disabled(self, strategy: UniversalOptionLiveStrategy) -> bool:
        extras = strategy.params.extras or {}
        trading_mode = str(os.getenv("TRADING_MODE", "live") or "live").strip().lower()
        return (
            trading_mode == "live"
            and bool(extras.get("paper_smoke_only", False))
            and not bool(extras.get("allow_live_smoke_entry", False))
        )

    async def _core_position_qty(self, strategy: UniversalOptionLiveStrategy, opt_symbol: str) -> float:
        engine = getattr(strategy.ctx, "engine", None)
        pg = getattr(engine, "pg", None)
        fetchrow = getattr(pg, "fetchrow", None)
        if fetchrow is None:
            return 0.0
        try:
            row = await fetchrow(
                """
                SELECT pos_qty
                FROM core.core_position
                WHERE strategy_id = $1 AND symbol = $2
                """,
                str(strategy.strategy_id),
                str(opt_symbol),
            )
        except Exception:
            return 0.0
        try:
            return max(0.0, float((row or {}).get("pos_qty") or 0.0))
        except Exception:
            return 0.0

    async def _adopt_open_smoke_position(
        self,
        strategy: UniversalOptionLiveStrategy,
        ctx: OptionBarContext,
    ) -> bool:
        engine = getattr(strategy.ctx, "engine", None)
        pg = getattr(engine, "pg", None)
        fetchall = getattr(pg, "fetchall", None)
        if fetchall is None:
            return False
        try:
            rows = await fetchall(
                """
                SELECT symbol, pos_qty, avg_px, updated_at
                FROM core.core_position
                WHERE strategy_id = $1
                  AND pos_qty > 0
                  AND symbol LIKE 'OPT:NVDA:%'
                ORDER BY updated_at ASC NULLS LAST
                """,
                str(strategy.strategy_id),
            )
        except Exception:
            return False
        if not rows:
            return False

        row = dict(rows[0])
        opt_symbol = str(row.get("symbol") or "")
        if not opt_symbol:
            return False
        right = "CALL" if opt_symbol.upper().endswith(":C") else "PUT"
        extras = strategy.params.extras or {}
        hold_seconds = max(0.0, _safe_float(extras.get("smoke_hold_seconds"), 65.0))
        strategy._pos = _PositionState(
            opt_symbol=opt_symbol,
            side=right,
            entry_time=ctx.now - timedelta(seconds=hold_seconds + 1.0),
            entry_underlying=_safe_float(ctx.bar.get("close")),
            entry_close=_safe_float(ctx.bar.get("close")),
            entry_vwap=_safe_float(ctx.bar.get("vwap"), _safe_float(ctx.bar.get("close"))),
            trace_id="nvda_smoke_adopted",
            filled=True,
        )
        strategy.ctx.log.warning(
            "nvda_option_smoke_position_adopted",
            ts=ctx.now.strftime("%Y-%m-%d %H:%M:%S"),
            strategy_id=str(strategy.strategy_id),
            symbol=opt_symbol,
            qty=_safe_float(row.get("pos_qty")),
        )
        return True

    async def _submit_entry_if_idle(
        self,
        strategy: UniversalOptionLiveStrategy,
        ctx: OptionBarContext,
        *,
        side: str,
        reason: str,
    ) -> bool:
        if not self._entry_allowed_now(strategy, ctx.now):
            return True
        if getattr(strategy, "_pos", None) is not None:
            return True
        if await self._has_working_option_order(strategy):
            return True
        if self._live_smoke_entry_disabled(strategy):
            strategy._emit_local_audit(
                stage="entry_signal_blocked",
                status="blocked",
                reason="live_smoke_entry_disabled",
                symbol=strategy.underlying,
                payload={
                    "trading_mode": str(os.getenv("TRADING_MODE", "live") or "live"),
                    "paper_smoke_only": bool((strategy.params.extras or {}).get("paper_smoke_only", False)),
                    "allow_live_smoke_entry": bool((strategy.params.extras or {}).get("allow_live_smoke_entry", False)),
                },
            )
            return True

        close = _safe_float(ctx.bar.get("close"))
        if close <= 0.0:
            return True

        spread_max_pct = _safe_float((strategy.params.extras or {}).get("spread_max_pct"), 0.35)
        trigger_id, decision_id, trace_id = strategy._make_audit_ids()
        strategy._emit_local_audit(
            stage="entry_signal_observed",
            status="observed",
            reason=reason,
            symbol=strategy.underlying,
            decision_id=decision_id,
            trigger_id=trigger_id,
            trace_id=trace_id,
            payload={"side": side, "close": close, "paper_smoke_only": True},
        )
        await strategy._submit_entry(
            ctx.now,
            side=side,
            spot=close,
            entry_bar=ctx.bar,
            spread_max_pct=spread_max_pct,
            wall_now=ctx.wall_now,
            audit_ids=(trigger_id, decision_id, trace_id),
        )
        return True

    async def sync_and_manage_position(self, strategy: UniversalOptionLiveStrategy, ctx: OptionBarContext) -> bool:
        pos = getattr(strategy, "_pos", None)
        if pos is None:
            adopted = await self._adopt_open_smoke_position(strategy, ctx)
            if not adopted:
                return False
            pos = getattr(strategy, "_pos", None)
            if pos is None:
                return False

        extras = strategy.params.extras or {}
        hold_seconds = max(0.0, _safe_float(extras.get("smoke_hold_seconds"), 65.0))
        held = max(0.0, (ctx.now - pos.entry_time).total_seconds())
        if held < hold_seconds:
            return True

        opt_symbol = str(pos.opt_symbol)
        try:
            ps = await strategy.ctx.position(opt_symbol)
            qty = strategy._strategy_owned_qty_cap(
                opt_symbol=opt_symbol,
                account_qty=_safe_float(getattr(ps, "pos_qty", 0.0)),
            )
        except Exception:
            qty = 0.0
        if qty <= 0.0:
            qty = await self._core_position_qty(strategy, opt_symbol)
        if qty <= 0.0:
            if held < max(hold_seconds + 30.0, 45.0):
                return True
            return False

        await strategy._submit_exit(
            ctx.now,
            opt_symbol,
            sell_qty=int(round(qty)),
            reason="SMOKE_CYCLE_EXIT",
            current_qty=qty,
        )
        return True

    def select_quote(
        self,
        strategy: UniversalOptionLiveStrategy,
        req: OptionQuoteRequest,
    ) -> OptionQuoteSelection | None:
        extras = strategy.params.extras or {}
        right = "C" if str(req.side).upper() == "CALL" else "P"
        dte_min = max(0, int(extras.get("dte_min", 0) or 0))
        dte_max = max(dte_min, int(extras.get("dte_max", 7) or 7))
        dte_preferred = int(extras.get("dte_preferred", min(max(1, dte_min), dte_max)) or dte_min)
        lookback_min = max(1, int(extras.get("entry_quote_lookback_min", 3) or 3))
        quote_max_age_sec = max(1.0, _safe_float(extras.get("entry_quote_max_age_sec"), 120.0))
        allow_ch = bool(extras.get("entry_quote_allow_ch_fallback", False))
        allow_stale = bool(extras.get("allow_stale_entry_quotes", False))
        ts_now = req.now.replace(second=0, microsecond=0)
        rows = query_option_rows(
            underlying=strategy.underlying,
            right=right,
            dte_min=dte_min,
            dte_max=dte_max,
            ts_lb=ts_now - timedelta(minutes=lookback_min),
            ts_now=ts_now,
            limit=256,
            fallback_to_ch=allow_ch,
        )
        if (not rows) and (not allow_ch):
            rows = query_option_rows(
                underlying=strategy.underlying,
                right=right,
                dte_min=dte_min,
                dte_max=dte_max,
                ts_lb=ts_now - timedelta(minutes=lookback_min),
                ts_now=ts_now,
                limit=256,
                fallback_to_ch=True,
            )

        valid_rows: list[Dict[str, Any]] = []
        stale = wide = invalid = 0
        ref_now = req.now_for_quote_age or req.now
        for row in rows or []:
            bid = _safe_float(row.get("bid"))
            ask = _safe_float(row.get("ask"))
            mid = _safe_float(row.get("mid"))
            if bid <= 0.0 or ask <= 0.0 or mid <= 0.0:
                invalid += 1
                continue
            age_sec = _quote_age_seconds(row, ref_now)
            if (not allow_stale) and age_sec > quote_max_age_sec:
                stale += 1
                continue
            if (ask - bid) / max(mid, 1e-9) > float(req.spread_max_pct):
                wide += 1
                continue
            payload = dict(row)
            payload["quote_age_sec"] = float(age_sec)
            valid_rows.append(payload)

        if not valid_rows:
            if stale:
                return OptionQuoteSelection(None, "STALE_QUOTE")
            if wide:
                return OptionQuoteSelection(None, "WIDE_SPREAD")
            if invalid:
                return OptionQuoteSelection(None, "INVALID_PRICE")
            return OptionQuoteSelection(None, "NO_QUOTES")

        best = min(
            valid_rows,
            key=lambda row: (
                abs(int(row.get("dte") or dte_preferred) - dte_preferred),
                abs(_safe_float(row.get("strike"), req.spot) - float(req.spot)),
            ),
        )
        dte = int(best.get("dte") or dte_preferred)
        strike = _safe_float(best.get("strike"), req.spot)
        opt_symbol = format_internal_opt_symbol(
            underlying=strategy.underlying,
            expiry_ymd=_expiry_ymd(best.get("expiry"), req.now, dte),
            strike=strike,
            right=right,
        )
        return OptionQuoteSelection(
            {
                "opt_symbol": opt_symbol,
                "strike": strike,
                "bid": _safe_float(best.get("bid")),
                "ask": _safe_float(best.get("ask")),
                "mid": _safe_float(best.get("mid")),
                "last": _safe_float(best.get("last"), _safe_float(best.get("mid"))),
                "quote_age_sec": _safe_float(best.get("quote_age_sec")),
                "quote_source": str(best.get("quote_source") or "unknown"),
            },
            None,
        )


class _NVDASmokeBaseStrategy(UniversalOptionLiveStrategy):
    def _latest_quote_status(self, now: datetime) -> Optional[Dict[str, Any]]:
        extras = self.params.extras or {}
        dte_min = max(0, int(extras.get("dte_min", 0) or 0))
        dte_max = max(dte_min, int(extras.get("dte_max", 7) or 7))
        try:
            return latest_quote_status(
                underlying=self.underlying,
                now=now,
                day_start=now.replace(hour=0, minute=0, second=0, microsecond=0),
                day_end=(now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0),
                dte_min=dte_min,
                dte_max=dte_max,
                expiry=None,
            )
        except Exception:
            return None

    def _tradability_blockers(self, now: datetime, bar: Optional[Dict[str, Any]] = None):
        blockers, details = super()._tradability_blockers(now, bar=bar)
        extras = self.params.extras or {}
        manifest = extras.get("production_manifest") if isinstance(extras.get("production_manifest"), dict) else {}
        eod_contract = manifest.get("eod_contract") if isinstance(manifest.get("eod_contract"), dict) else {}
        recovery_contract = manifest.get("recovery_contract") if isinstance(manifest.get("recovery_contract"), dict) else {}
        idle_with_position = (
            bool(details.get("idle_ok")) is True
            and bool(details.get("has_position")) is True
            and bool(eod_contract.get("no_overnight")) is True
            and bool(eod_contract.get("platform_eod_required")) is True
            and str(recovery_contract.get("policy_id") or "").strip() == "option_close_only"
        )
        if idle_with_position:
            details["safety_supervised"] = True
            details["position_safety_owner"] = "platform_eod_position_safety"
            details["entry_ready"] = False
            details["safety_ready"] = True
        else:
            details.setdefault("safety_supervised", False)
            details.setdefault("entry_ready", bool(details.get("tradable_for_entry")))
            details.setdefault("safety_ready", not bool(details.get("has_position")))
        return blockers, details

    def _get_quote_for_contract(
        self,
        now: datetime,
        opt_symbol: str,
        *,
        lookback_min: int,
        max_age_sec: float,
        fallback_to_ch: bool = True,
    ) -> Dict[str, Any] | None:
        try:
            parts = parse_internal_opt_symbol(opt_symbol)
        except Exception:
            return super()._get_quote_for_contract(
                now,
                opt_symbol,
                lookback_min=lookback_min,
                max_age_sec=max_age_sec,
                fallback_to_ch=fallback_to_ch,
            )
        try:
            expiry_dt = datetime.strptime(parts.expiry_ymd, "%Y%m%d").date()
            dte = max(0, (expiry_dt - now.date()).days)
        except Exception:
            dte = int((self.params.extras or {}).get("dte_preferred", 1) or 1)

        rows = query_option_rows(
            underlying=parts.underlying,
            right=parts.right,
            dte_min=dte,
            dte_max=dte,
            ts_lb=now.replace(second=0, microsecond=0) - timedelta(minutes=max(1, int(lookback_min))),
            ts_now=now.replace(second=0, microsecond=0),
            limit=128,
            fallback_to_ch=fallback_to_ch,
        )
        if (not rows) and (not fallback_to_ch):
            rows = query_option_rows(
                underlying=parts.underlying,
                right=parts.right,
                dte_min=dte,
                dte_max=dte,
                ts_lb=now.replace(second=0, microsecond=0) - timedelta(minutes=max(1, int(lookback_min))),
                ts_now=now.replace(second=0, microsecond=0),
                limit=128,
                fallback_to_ch=True,
            )
        ref_now = now
        candidates: list[Dict[str, Any]] = []
        for row in rows or []:
            strike = _safe_float(row.get("strike"))
            if abs(strike - float(parts.strike)) > 1e-6:
                continue
            bid = _safe_float(row.get("bid"))
            ask = _safe_float(row.get("ask"))
            mid = _safe_float(row.get("mid"))
            if bid <= 0.0 or ask <= 0.0:
                continue
            if mid <= 0.0:
                mid = (bid + ask) / 2.0
            age_sec = _quote_age_seconds(row, ref_now)
            if age_sec > max_age_sec:
                continue
            payload = dict(row)
            payload.update(
                {
                    "bid": bid,
                    "ask": ask,
                    "mid": mid,
                    "last": _safe_float(row.get("last"), mid),
                    "age_sec": float(age_sec),
                    "quote_age_sec": float(age_sec),
                    "quote_source": str(row.get("quote_source") or "nvda_smoke_quote_store"),
                }
            )
            candidates.append(payload)
        if not candidates:
            return super()._get_quote_for_contract(
                now,
                opt_symbol,
                lookback_min=lookback_min,
                max_age_sec=max_age_sec,
                fallback_to_ch=fallback_to_ch,
            )
        return min(candidates, key=lambda row: _safe_float(row.get("age_sec"), 9_999.0))

    def _prepare_entry_intent(
        self,
        *,
        now: datetime,
        side: str,
        spot: float,
        entry_bar: Dict[str, Any],
        quote: Dict[str, Any],
        trigger_id: str,
        decision_id: str,
        trace_id: str,
    ) -> tuple[bool, Dict[str, Any]]:
        proceed, intent_extras = super()._prepare_entry_intent(
            now=now,
            side=side,
            spot=spot,
            entry_bar=entry_bar,
            quote=quote,
            trigger_id=trigger_id,
            decision_id=decision_id,
            trace_id=trace_id,
        )
        intent_extras = dict(intent_extras or {})
        if bool((self.params.extras or {}).get("paper_smoke_only", False)):
            intent_extras["paper_smoke_only"] = True
            if bool((self.params.extras or {}).get("allow_degraded_smoke_entry", False)):
                intent_extras["allow_system_health_degraded_entry"] = True
        return proceed, intent_extras


class NVDAOptionChurnSmokePlugin(_NVDASmokeBasePlugin):
    component_name = "nvda_option_churn_smoke_plugin"

    async def maybe_enter(self, strategy: UniversalOptionLiveStrategy, ctx: OptionBarContext) -> bool:
        extras = strategy.params.extras or {}
        cadence_minutes = max(1, int(extras.get("smoke_entry_cadence_minutes", 1) or 1))
        minute_index = int(ctx.now.timestamp() // 60)
        if minute_index % cadence_minutes != 0:
            return True
        side = "CALL" if (minute_index // cadence_minutes) % 2 == 0 else "PUT"
        return await self._submit_entry_if_idle(
            strategy,
            ctx,
            side=side,
            reason="NVDA_CHURN_SMOKE_TICK",
        )


class NVDAStockSignalOptionPlugin(_NVDASmokeBasePlugin):
    component_name = "nvda_stock_signal_option_plugin"

    async def maybe_enter(self, strategy: UniversalOptionLiveStrategy, ctx: OptionBarContext) -> bool:
        bars = list(getattr(strategy, "_bars", []) or [])
        if len(bars) < 2:
            return True

        prev = bars[-2]
        close = _safe_float(ctx.bar.get("close"))
        prev_close = _safe_float(prev.get("close"))
        if close <= 0.0 or prev_close <= 0.0:
            return True

        extras = strategy.params.extras or {}
        move_bps = ((close - prev_close) / prev_close) * 10_000.0
        threshold_bps = max(0.0, _safe_float(extras.get("stock_signal_move_bps"), 5.0))
        require_vwap = bool(extras.get("stock_signal_require_vwap_side", True))
        vwap = _safe_float(ctx.bar.get("vwap"), close)

        if move_bps >= threshold_bps and ((not require_vwap) or close >= vwap):
            return await self._submit_entry_if_idle(
                strategy,
                ctx,
                side="CALL",
                reason="NVDA_STOCK_SIGNAL_CALL",
            )
        if move_bps <= -threshold_bps and ((not require_vwap) or close <= vwap):
            return await self._submit_entry_if_idle(
                strategy,
                ctx,
                side="PUT",
                reason="NVDA_STOCK_SIGNAL_PUT",
            )
        return True


@register_strategy("nvda_option_churn_smoke")
class NVDAOptionChurnSmokeStrategy(_NVDASmokeBaseStrategy):
    def build_plugin(self) -> OptionStrategyPlugin:
        return NVDAOptionChurnSmokePlugin()


@register_strategy("nvda_stock_signal_option_smoke")
class NVDAStockSignalOptionSmokeStrategy(_NVDASmokeBaseStrategy):
    def build_plugin(self) -> OptionStrategyPlugin:
        return NVDAStockSignalOptionPlugin()
