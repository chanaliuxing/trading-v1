from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any, Dict, Optional, Type

from libs.options.internal_symbol import parse_internal_opt_symbol
from libs.strategy.base import StrategyAlgorithm, StrategyContext, StrategyParams
from services.market.options_quote_store import get_quote_for_opt_symbol, latest_quote_status, quote_minute_coverage
from strategies.options_kernel.execution_profile import build_execution_profile_from_extras
from strategies.options_kernel.plugin import BaseOptionPlugin
from strategies.options_kernel.types import EntryPlan, ExecutionProfile, ExitPlan, KernelBarContext, PluginRuntimeState, Selection, Signal
from strategies.options_plugins.ma_1dte_reject_shared import select_option_quote_for_underlying, safe_float


@dataclass
class _PendingEntry:
    direction: str
    level: float
    stage: str
    created_at: datetime
    bars_waited: int = 0


@dataclass
class _MinuteBuilder:
    minute_start: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap_num: float
    last_vwap: float
    atr_d: float = 0.0

    def ingest(self, bar: Dict[str, Any]) -> None:
        self.high = max(self.high, float(bar.get("high") or self.close))
        self.low = min(self.low, float(bar.get("low") or self.close))
        self.close = float(bar.get("close") or self.close)
        vol = float(bar.get("volume") or 0.0)
        self.volume += vol
        vwap = float(bar.get("vwap") or 0.0)
        if vol > 0 and vwap > 0:
            self.vwap_num += vwap * vol
            self.last_vwap = vwap
        elif vwap > 0:
            self.last_vwap = vwap
        atr_d = float(bar.get("atr_d") or 0.0)
        if atr_d > 0:
            self.atr_d = atr_d

    def finalize(self) -> Dict[str, Any]:
        vwap = (self.vwap_num / self.volume) if self.volume > 0 else self.last_vwap
        return {
            "ts": self.minute_start + timedelta(minutes=1),
            "open": float(self.open),
            "high": float(self.high),
            "low": float(self.low),
            "close": float(self.close),
            "vwap": float(vwap or self.close),
            "volume": float(self.volume),
            "atr_d": float(self.atr_d or 0.0),
            "atr_1m": 0.0,
            "atr_1m_ma": 0.0,
            "vol_ma": None,
            "rvol": None,
        }


def _parse_hms(raw: Any, default: time) -> time:
    try:
        s = str(raw or "").strip()
        if not s:
            return default
        return time.fromisoformat(s)
    except Exception:
        return default


def _minute_floor(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


def _live_strategy_cls() -> Type[StrategyAlgorithm]:
    from strategies.qqq_0dte_opt_live_strategy import QQQ0DTEOptionsLiveStrategy

    return QQQ0DTEOptionsLiveStrategy


class QQQ0DTEOptionsLivePlugin(BaseOptionPlugin):
    component_name = "qqq_0dte_opt_live_plugin"
    supports_pure_kernel = True

    def __init__(self) -> None:
        self._last_extras: Dict[str, Any] = {}
        self._pure_bars = deque(maxlen=600)
        self._pure_trs = deque(maxlen=200)
        self._pure_vols = deque(maxlen=200)
        self._pure_minute_builder: Optional[_MinuteBuilder] = None
        self._pure_pending: Optional[_PendingEntry] = None
        self._pure_last_day: Optional[str] = None
        self._pure_last_completed_minute_ts: Optional[datetime] = None
        self._pure_current_completed_bar: Optional[Dict[str, Any]] = None
        self._pure_trades_filled_today: int = 0
        self._pure_first_trade_exit_reason: Optional[str] = None
        self._pure_quote_hb_pause_new_entries: bool = False
        self._pure_quote_hb_fail_streak: int = 0
        self._pure_quote_hb_fresh_streak: int = 0
        self._pure_quote_health_checked: bool = False
        self._pure_quote_health_block_day: bool = False
        self._pure_quote_health_cov_pct: float = 0.0
        self._pure_strategy_id: str = ""
        self._pure_underlying: str = "QQQ"
        self._pure_log: Any = None
        self._pure_day_block_counts: Dict[str, int] = {}

    def apply_default_extras(self, extras: Dict[str, Any]) -> Dict[str, Any]:
        merged = dict(extras or {})
        merged.setdefault("underlying", "QQQ")
        merged.setdefault("risk_symbol", "OPT:QQQ:0DTE")
        merged.setdefault("enable_live_parity_snapshot", True)
        merged.setdefault("enable_live_shadow_mode", False)
        merged.setdefault("live_parity_profile_id", "qqq_0dte_option_live_v2")
        merged.setdefault("market_data_profile_id", "standard_close_extended_sma")
        merged.setdefault("kernel_mode", "hybrid")
        merged.setdefault("strict_scope_validation", True)
        merged.setdefault("dte_preferred", 0)
        merged.setdefault("dte_fallback_max", 0)
        self._last_extras = dict(merged)
        return merged

    def build_delegate(self, ctx: StrategyContext, params: StrategyParams) -> StrategyAlgorithm:
        return _live_strategy_cls()(ctx, params)

    async def initialize_pure(self, ctx: StrategyContext, params: StrategyParams, kernel: Any) -> None:
        extras = dict(params.extras or {})
        underlying = str(extras.get("underlying", "QQQ")).strip().upper()
        self._pure_strategy_id = str(ctx.strategy_id)
        self._pure_underlying = underlying
        self._pure_log = getattr(ctx, "log", None)
        self._last_extras = dict(extras)
        self._pure_last_day = None
        self._pure_reset_if_needed(
            PluginRuntimeState(
                strategy_id=str(ctx.strategy_id),
                kernel_mode="pure_plugin",
                symbol=underlying,
                now=datetime.now(),
                wall_now=datetime.now(),
                spot=0.0,
                bar=KernelBarContext(ts=datetime.now(), symbol=underlying, close=0.0),
                payload={},
            )
        )
        preload_bars = max(0, int(extras.get("preload_history_bars", 260) or 260))
        if preload_bars > 0:
            try:
                rows = await ctx.history(underlying, "1m", preload_bars)
                loaded = 0
                for row in sorted(
                    list(rows or []),
                    key=lambda item: item.get("ts").timestamp() if isinstance(item.get("ts"), datetime) else float("-inf"),
                ):
                    ts = row.get("ts")
                    if not isinstance(ts, datetime):
                        continue
                    bar = {
                        "ts": ts,
                        "open": safe_float(row.get("open"), safe_float(row.get("close"))),
                        "high": safe_float(row.get("high"), safe_float(row.get("close"))),
                        "low": safe_float(row.get("low"), safe_float(row.get("close"))),
                        "close": safe_float(row.get("close")),
                        "vwap": safe_float(row.get("vwap"), safe_float(row.get("close"))),
                        "volume": safe_float(row.get("volume")),
                        "atr_d": safe_float(row.get("atr_d")),
                        "atr_1m": safe_float(row.get("atr_1m")),
                        "atr_1m_ma": safe_float(row.get("atr_1m_ma")),
                        "vol_ma": row.get("vol_ma"),
                        "rvol": row.get("rvol"),
                    }
                    self._enrich_bar(bar)
                    self._pure_bars.append(dict(bar))
                    self._pure_last_completed_minute_ts = ts
                    self._pure_current_completed_bar = dict(bar)
                    loaded += 1
                ctx.log.info(
                    "option_kernel_pure_preload_ok",
                    strategy_id=str(ctx.strategy_id),
                    plugin=str(self.component_name),
                    underlying=underlying,
                    resolution="1m",
                    loaded_bars=int(loaded),
                )
            except Exception as exc:
                ctx.log.warning(
                    "option_kernel_pure_preload_failed",
                    strategy_id=str(ctx.strategy_id),
                    plugin=str(self.component_name),
                    underlying=underlying,
                    resolution="1m",
                    error=str(exc),
                )
        if bool(extras.get("rfe_primary_enabled", True)):
            rfe_spec = dict(extras.get("realtime_feature_engine") or {})
            rfe_spec.setdefault("emit_on", "1m")
            rfe_spec.setdefault("sink", {"enabled": False})
            try:
                await ctx.enable_realtime_feature_engine([underlying], spec=rfe_spec)
            except Exception as exc:
                ctx.log.warning(
                    "option_kernel_pure_rfe_enable_failed",
                    strategy_id=str(ctx.strategy_id),
                    plugin=str(self.component_name),
                    underlying=underlying,
                    error=str(exc),
                )
        source_bar_sec = max(5, int(extras.get("realtime_bar_sec", 5) or 5))
        await ctx.subscribe_features([underlying], bar_sec=source_bar_sec)
        ctx.log.info(
            "option_kernel_pure_subscribed",
            strategy_id=str(ctx.strategy_id),
            plugin=str(self.component_name),
            underlying=underlying,
            bar_sec=int(source_bar_sec),
        )

    def build_signal(self, strategy: StrategyAlgorithm, ctx: Dict[str, Any]) -> Optional[Signal]:
        if not isinstance(strategy, _live_strategy_cls()):
            return None
        pending = getattr(strategy, "_pending", None)
        if pending is None:
            return None
        side = "CALL" if str(getattr(pending, "direction", "")).upper() == "LONG" else "PUT"
        return Signal(
            ts=ctx.get("now") or datetime.now(),
            side=side,
            signal_kind="qqq_0dte_confirm",
            payload={
                "pending_stage": str(getattr(pending, "stage", "") or ""),
                "pending_level": float(getattr(pending, "level", 0.0) or 0.0),
            },
        )

    def build_entry_plan(self, strategy: StrategyAlgorithm, ctx: Dict[str, Any], signal: Signal) -> Optional[EntryPlan]:
        if not isinstance(strategy, _live_strategy_cls()):
            return None
        extras = strategy.params.extras or {}
        contracts = int(extras.get("contracts", extras.get("contracts_per_trade", 3)) or 3)
        return EntryPlan(
            ts=ctx.get("now") or datetime.now(),
            side=str(signal.side),
            signal_kind=str(signal.signal_kind),
            structure_stop=0.0,
            target_1r=0.0,
            target_1p2r=0.0,
            r_per_share=0.0,
            contracts=contracts,
            payload=dict(signal.payload or {}),
        )

    def select_contract(self, strategy: StrategyAlgorithm, ctx: Dict[str, Any], entry_plan: EntryPlan) -> Optional[Selection]:
        if not isinstance(strategy, _live_strategy_cls()):
            return None
        now = ctx.get("now")
        if not isinstance(now, datetime):
            return None
        spot = float(ctx.get("spot") or 0.0)
        spread_max_pct = float((strategy.params.extras or {}).get("spread_max_pct", 0.15) or 0.15)
        quote, reason = strategy._select_0dte_quote(
            now,
            side=str(entry_plan.side),
            spot=spot,
            spread_max_pct=spread_max_pct,
            now_for_quote_age=now,
        )
        if quote is None:
            return None
        return Selection(
            opt_symbol=str(quote.get("opt_symbol") or ""),
            expiry=str(quote.get("expiry") or ""),
            strike=float(quote.get("strike") or 0.0),
            side=str(entry_plan.side),
            bid=float(quote.get("bid") or 0.0),
            ask=float(quote.get("ask") or 0.0),
            mid=float(quote.get("mid") or 0.0),
            last=float(quote.get("last") or 0.0),
            quote_age_sec=float(quote.get("quote_age_sec") or 0.0),
            payload={"reject_reason": str(reason or "")},
        )

    def select_quote(self, strategy: StrategyAlgorithm, req: Dict[str, Any]) -> Optional[Selection]:
        if not isinstance(strategy, _live_strategy_cls()):
            return None
        ctx = dict(req.get("ctx") or {})
        entry_plan = req.get("entry_plan")
        if isinstance(entry_plan, dict):
            side = str(entry_plan.get("side") or "")
            signal_kind = str(entry_plan.get("signal_kind") or "qqq_0dte_confirm")
            contracts = int(entry_plan.get("contracts") or int((strategy.params.extras or {}).get("contracts", 3) or 3))
            plan = EntryPlan(
                ts=ctx.get("now") or datetime.now(),
                side=side,
                signal_kind=signal_kind,
                structure_stop=float(entry_plan.get("structure_stop") or 0.0),
                target_1r=float(entry_plan.get("target_1r") or 0.0),
                target_1p2r=float(entry_plan.get("target_1p2r") or 0.0),
                r_per_share=float(entry_plan.get("r_per_share") or 0.0),
                contracts=contracts,
                payload=dict(entry_plan.get("payload") or {}),
            )
            return self.select_contract(strategy, ctx, plan)
        return None

    def build_execution_profile(
        self,
        strategy: StrategyAlgorithm,
        ctx: Dict[str, Any],
        entry_plan: EntryPlan,
        selection: Optional[Selection],
    ) -> ExecutionProfile:
        extras = getattr(strategy, "params", None).extras if getattr(strategy, "params", None) is not None else {}
        return build_execution_profile_from_extras(
            profile_id=str((extras or {}).get("live_parity_profile_id") or "qqq_0dte_option_live_v2"),
            market_data_profile_id=str((extras or {}).get("market_data_profile_id") or "standard_close_extended_sma"),
            extras=extras,
        )

    def build_exit_plan(
        self,
        strategy: StrategyAlgorithm,
        ctx: Dict[str, Any],
        market_state: Dict[str, Any],
    ) -> Optional[ExitPlan]:
        if not isinstance(strategy, _live_strategy_cls()):
            return None
        pos = getattr(strategy, "_pos", None)
        if pos is None:
            return None
        reason = str(getattr(pos, "last_exit_reason", "") or "").strip()
        if not reason:
            return None
        return ExitPlan(
            ts=ctx.get("now") or datetime.now(),
            position_lot_id=str(getattr(pos, "position_id", "") or ""),
            qty=float(market_state.get("qty") or 0.0),
            reason=reason,
            payload={
                "opt_symbol": str(getattr(pos, "opt_symbol", "") or ""),
                "entry_snapshot_id": str(getattr(pos, "entry_snapshot_id", "") or ""),
            },
        )

    async def maybe_enter(self, strategy: StrategyAlgorithm, ctx: Dict[str, Any]) -> bool:
        if not isinstance(strategy, _live_strategy_cls()):
            return False
        now = ctx.get("now")
        bar = ctx.get("bar")
        if not isinstance(now, datetime) or not isinstance(bar, dict):
            return False
        await strategy._maybe_enter(now, dict(bar), wall_now=ctx.get("wall_now"))
        return True

    async def sync_and_manage_position(self, strategy: StrategyAlgorithm, ctx: Dict[str, Any]) -> bool:
        if not isinstance(strategy, _live_strategy_cls()):
            return False
        now = ctx.get("now")
        bar = ctx.get("bar")
        if not isinstance(now, datetime) or not isinstance(bar, dict):
            return False
        return bool(await strategy._sync_and_manage_position(now, dict(bar)))

    def _pure_reset_if_needed(self, runtime: PluginRuntimeState) -> None:
        day = runtime.now.astimezone(runtime.now.tzinfo).strftime("%Y-%m-%d") if runtime.now.tzinfo else runtime.now.strftime("%Y-%m-%d")
        if self._pure_last_day == day:
            return
        self._pure_last_day = day
        self._pure_bars.clear()
        self._pure_trs.clear()
        self._pure_vols.clear()
        self._pure_minute_builder = None
        self._pure_pending = None
        self._pure_last_completed_minute_ts = None
        self._pure_current_completed_bar = None
        self._pure_trades_filled_today = 0
        self._pure_first_trade_exit_reason = None
        self._pure_quote_hb_pause_new_entries = False
        self._pure_quote_hb_fail_streak = 0
        self._pure_quote_hb_fresh_streak = 0
        self._pure_quote_health_checked = False
        self._pure_quote_health_block_day = False
        self._pure_quote_health_cov_pct = 0.0
        self._pure_day_block_counts = {}

    def _emit_minute_observability(self, runtime: PluginRuntimeState, *, bar: Dict[str, Any], reason: Optional[str]) -> None:
        log = self._pure_log
        if log is None:
            return
        ts = runtime.now
        minute = ts.strftime("%Y-%m-%d %H:%M")
        quote_status = self._latest_quote_status(ts)
        quote_source = str((quote_status or {}).get("source") or "unknown")
        quote_age_sec = None if quote_status is None else float(quote_status.get("age_sec") or 0.0)
        try:
            log.info(
                "qqq_0dte_minute_heartbeat",
                ts=minute + ":00",
                strategy_id=str(self._pure_strategy_id or runtime.strategy_id),
                underlying=str(self._pure_underlying or runtime.symbol),
                bars=int(len(self._pure_bars)),
                has_pending=bool(self._pure_pending is not None),
                has_position=bool(runtime.active_position is not None),
                trades_filled_today=int(self._pure_trades_filled_today),
                quote_health_block_day=bool(self._pure_quote_health_block_day),
                close=float(bar.get("close") or 0.0),
                vwap=float(bar.get("vwap") or 0.0),
                atr_1m=float(bar.get("atr_1m") or 0.0),
                rvol=float(bar.get("rvol") or 0.0) if bar.get("rvol") is not None else None,
                quote_source=quote_source,
                quote_age_sec=quote_age_sec,
            )
        except Exception:
            pass
        if not reason:
            return
        day_count = int(self._pure_day_block_counts.get(reason, 0)) + 1
        self._pure_day_block_counts[reason] = day_count
        try:
            log.info(
                "qqq_0dte_entry_block",
                ts=minute + ":00",
                minute=minute,
                reason=str(reason),
                minute_count=1,
                day_count=day_count,
                strategy_id=str(self._pure_strategy_id or runtime.strategy_id),
                underlying=str(self._pure_underlying or runtime.symbol),
                quote_source=quote_source,
                quote_age_sec=quote_age_sec,
            )
        except Exception:
            pass

    @staticmethod
    def _classify_feature_source(features: Dict[str, Any]) -> str:
        ts = features.get("ts")
        try:
            raw = float(ts)
            if raw > 1_000_000_000_000_00:
                return "rfe_1m"
        except Exception:
            pass
        if any(str(k).endswith("_1m") for k in features.keys()):
            return "rfe_1m"
        return "subminute"

    @staticmethod
    def _features_to_bar(now: datetime, features: Dict[str, Any]) -> Dict[str, Any]:
        def f(key: str) -> float:
            try:
                return float(features.get(key, 0.0) or 0.0)
            except Exception:
                return 0.0

        def f_any(keys: tuple[str, ...], default: float = 0.0) -> float:
            for key in keys:
                try:
                    raw = features.get(key)
                    if raw is None:
                        continue
                    return float(raw or 0.0)
                except Exception:
                    continue
            return float(default)

        close = f("close")
        vwap = f_any(("vwap", "vwap_1m"), default=close)
        if vwap <= 0.0 and close > 0.0:
            vwap = close
        return {
            "ts": now,
            "open": f("open"),
            "high": f("high"),
            "low": f("low"),
            "close": close,
            "vwap": vwap,
            "volume": f_any(("volume", "vol", "bar_volume", "total_volume"), default=0.0),
            "atr_d": f("atr_d"),
            "atr_1m": 0.0,
            "atr_1m_ma": 0.0,
            "vol_ma": None,
            "rvol": None,
        }

    @staticmethod
    def _subminute_parent_minute(now: datetime) -> datetime:
        return _minute_floor(now - timedelta(microseconds=1))

    @staticmethod
    def _new_minute_builder(minute_start: datetime, bar: Dict[str, Any]) -> _MinuteBuilder:
        close = float(bar.get("close") or 0.0)
        vol = float(bar.get("volume") or 0.0)
        vwap = float(bar.get("vwap") or close)
        return _MinuteBuilder(
            minute_start=minute_start,
            open=float(bar.get("open") or close),
            high=float(bar.get("high") or close),
            low=float(bar.get("low") or close),
            close=close,
            volume=vol,
            vwap_num=(vwap * vol) if (vol > 0 and vwap > 0) else 0.0,
            last_vwap=vwap if vwap > 0 else close,
            atr_d=float(bar.get("atr_d") or 0.0),
        )

    def _ingest_subminute_bar(self, now: datetime, features: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        subbar = self._features_to_bar(now, features)
        minute_start = self._subminute_parent_minute(now)
        if self._pure_minute_builder is None:
            self._pure_minute_builder = self._new_minute_builder(minute_start, subbar)
            return None
        if minute_start == self._pure_minute_builder.minute_start:
            self._pure_minute_builder.ingest(subbar)
            return None
        completed = self._pure_minute_builder.finalize()
        self._pure_minute_builder = self._new_minute_builder(minute_start, subbar)
        return completed

    def _enrich_bar(self, bar: Dict[str, Any]) -> None:
        close = float(bar.get("close") or 0.0)
        high = float(bar.get("high") or close)
        low = float(bar.get("low") or close)
        prev_close = float(self._pure_bars[-1].get("close") or close) if self._pure_bars else close
        tr = max(abs(high - low), abs(high - prev_close), abs(low - prev_close))
        self._pure_trs.append(float(tr))
        vol = float(bar.get("volume") or 0.0)
        self._pure_vols.append(float(vol))
        if len(self._pure_trs) >= 14:
            bar["atr_1m"] = float(sum(list(self._pure_trs)[-14:]) / 14.0)
        extras = self._last_extras or {}
        rvol_window = int(extras.get("rvol_window", 20) or 20)
        if len(self._pure_vols) >= max(5, min(rvol_window, len(self._pure_vols))):
            window = list(self._pure_vols)[-rvol_window:]
            vol_ma = sum(window) / max(1, len(window))
            bar["vol_ma"] = float(vol_ma)
            bar["rvol"] = (float(vol) / float(vol_ma)) if vol_ma > 0 else None
        atr_ma_window = int(extras.get("atr_expansion_window", 20) or 20)
        if len(self._pure_trs) >= max(5, atr_ma_window):
            atr_series = list(self._pure_trs)[-atr_ma_window:]
            bar["atr_1m_ma"] = float(sum(atr_series) / max(1, len(atr_series)))

    def _complete_minute_bar(self, now: datetime, features: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        source = self._classify_feature_source(features)
        if source == "rfe_1m":
            return self._features_to_bar(now, features)
        return self._ingest_subminute_bar(now, features)

    def _resolve_atr_d(self, bar: Dict[str, Any]) -> float:
        atr_d = float(bar.get("atr_d") or 0.0)
        if atr_d > 0:
            return atr_d
        extras = self._last_extras or {}
        atr_1m = float(bar.get("atr_1m") or 0.0)
        mult = float(extras.get("atr_d_mult_from_atr_1m", 40.0) or 40.0)
        return atr_1m * mult

    def _price_at_time(self, target: time) -> Optional[float]:
        for bar in self._pure_bars:
            ts = bar.get("ts")
            if isinstance(ts, datetime) and ts.time() == target:
                return float(bar.get("close") or 0.0)
        return None

    def _early_override_ready(self, now: datetime) -> bool:
        extras = self._last_extras or {}
        if now.time() < time(9, 39) or not self._pure_bars:
            return False
        atr_d = self._resolve_atr_d(self._pure_bars[-1])
        if atr_d <= 0:
            return False
        open_0930 = self._price_at_time(time(9, 30))
        px_0938 = self._price_at_time(time(9, 38))
        if open_0930 is None or px_0938 is None:
            return False
        early_move_mult = float(extras.get("early_override_move_mult", 0.16) or 0.16)
        early_vol_min = float(extras.get("early_override_vol_min", 0.60) or 0.60)
        if abs(float(px_0938) - float(open_0930)) < early_move_mult * atr_d:
            return False
        side_sign = None
        vols = []
        vol_0939 = None
        for bar in self._pure_bars:
            ts = bar.get("ts")
            if not isinstance(ts, datetime):
                continue
            bt = ts.time()
            if time(9, 31) <= bt <= time(9, 39):
                c = float(bar.get("close") or 0.0)
                v = float(bar.get("vwap") or 0.0)
                if c <= 0 or v <= 0:
                    return False
                s = 1 if c > v else (-1 if c < v else 0)
                if s == 0:
                    return False
                if side_sign is None:
                    side_sign = s
                elif side_sign != s:
                    return False
                if bt == time(9, 39):
                    vol_0939 = float(bar.get("volume") or 0.0)
                else:
                    vols.append(float(bar.get("volume") or 0.0))
        if vol_0939 is None or len(vols) < 7:
            return False
        base_vol = sum(vols) / max(1, len(vols))
        return base_vol > 0 and (float(vol_0939) / float(base_vol)) >= early_vol_min

    def _enter_with_gates(self, now: datetime, bar: Dict[str, Any], *, direction: str, breakout_level: Optional[float]) -> bool:
        extras = self._last_extras or {}
        if len(self._pure_bars) < 6:
            return False
        close = float(bar.get("close") or 0.0)
        atr_1m = float(bar.get("atr_1m") or 0.0)
        v3m_signed = close - float(self._pure_bars[-4].get("close") or close)
        deadzone_px = max(
            float(extras.get("momentum_deadzone", 0.05) or 0.05),
            float(extras.get("momentum_deadzone_atr_mult", 0.25) or 0.25) * atr_1m,
        )
        if direction == "LONG" and v3m_signed <= deadzone_px:
            return False
        if direction == "SHORT" and v3m_signed >= -deadzone_px:
            return False
        if bool(extras.get("anti_chase_enabled", False)) and breakout_level is not None:
            atr_d = self._resolve_atr_d(bar)
            if atr_d > 0:
                max_ext_atr = float(extras.get("anti_chase_max_extension_atr", 0.12) or 0.12)
                ext_signed = ((close - float(breakout_level)) / atr_d) if direction == "LONG" else ((float(breakout_level) - close) / atr_d)
                if ext_signed > max_ext_atr:
                    return False
        atr_rank = extras.get("atr_rank_today")
        if atr_rank is not None:
            try:
                if float(atr_rank) <= float(extras.get("atr_rank_skip_max", 0.15) or 0.15):
                    return False
            except Exception:
                pass
        if bool(extras.get("early_override_enabled", True)) and now.time() < time(9, 45):
            window_start = _parse_hms(extras.get("early_override_window_start", "09:39:00"), time(9, 39))
            window_end = _parse_hms(extras.get("early_override_window_end", "09:45:00"), time(9, 45))
            if not (window_start <= now.time() <= window_end and self._early_override_ready(now)):
                return False
        delayed_confirm_end = _parse_hms(extras.get("delayed_confirm_end", "10:00:00"), time(10, 0))
        if time(9, 45) <= now.time() <= delayed_confirm_end:
            atr_d = self._resolve_atr_d(bar)
            if atr_d <= 0 or len(self._pure_bars) < 6:
                return False
            close_5m_ago = float(self._pure_bars[-6].get("close") or close)
            move5m = abs(close - close_5m_ago) / atr_d
            if move5m < float(extras.get("delayed_confirm_move5m_min", 0.155) or 0.155):
                return False
        return True

    def _select_0dte_quote_pure(self, now: datetime, *, side: str, spot: float, spread_max_pct: float) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
        extras = dict(self._last_extras or {})
        extras["dte_preferred"] = 0
        extras["dte_fallback_max"] = 0
        return select_option_quote_for_underlying(
            underlying=str(extras.get("underlying") or "QQQ"),
            extras=extras,
            now=now,
            side=side,
            spot=spot,
            spread_max_pct=spread_max_pct,
            now_for_quote_age=now,
        )

    def _latest_quote_status(self, now: datetime) -> Optional[Dict[str, Any]]:
        extras = self._last_extras or {}
        expiry = now.strftime("%Y-%m-%d")
        try:
            return latest_quote_status(
                underlying=str(extras.get("underlying") or "QQQ"),
                now=now,
                day_start=datetime.combine(now.date(), time(0, 0), tzinfo=now.tzinfo),
                day_end=datetime.combine(now.date() + timedelta(days=1), time(0, 0), tzinfo=now.tzinfo),
                dte_min=0,
                dte_max=0,
                expiry=expiry,
            )
        except Exception:
            return None

    def _latest_quote_age_sec(self, now: datetime) -> Optional[float]:
        status = self._latest_quote_status(now)
        if not status:
            return None
        return float(status.get("age_sec") or 0.0)

    def _query_quote_minute_coverage(self, window_start: datetime, window_end: datetime) -> Dict[str, int]:
        extras = self._last_extras or {}
        return quote_minute_coverage(
            underlying=str(extras.get("underlying") or "QQQ"),
            window_start=window_start,
            window_end=window_end,
            dte_min=0,
            dte_max=0,
            expiry=window_start.strftime("%Y-%m-%d"),
        )

    def _quote_heartbeat_allows_entry(self, now: datetime) -> bool:
        extras = self._last_extras or {}
        if not bool(extras.get("quote_hb_gate_enabled", True)):
            return True
        start = _parse_hms(extras.get("quote_hb_check_start", "09:45:00"), time(9, 45))
        end = _parse_hms(extras.get("quote_hb_check_end", extras.get("no_new_entry_after", "14:50:00")), time(14, 50))
        if now.time() < start or now.time() >= end:
            return True
        warmup_min = max(0, int(extras.get("quote_hb_warmup_min", 5) or 5))
        warmup_end = datetime.combine(now.date(), start, tzinfo=now.tzinfo) + timedelta(minutes=warmup_min)
        if now < warmup_end:
            return True
        max_age_sec = max(10.0, float(extras.get("quote_hb_max_age_sec", 120.0) or 120.0))
        fail_to_pause = max(1, int(extras.get("quote_hb_fail_streak_to_pause", 4) or 4))
        resume_streak = max(1, int(extras.get("quote_hb_resume_fresh_streak", 2) or 2))
        age_sec = self._latest_quote_age_sec(now)
        fresh = age_sec is not None and age_sec <= max_age_sec
        if fresh:
            self._pure_quote_hb_fail_streak = 0
            self._pure_quote_hb_fresh_streak += 1
            if self._pure_quote_hb_pause_new_entries and self._pure_quote_hb_fresh_streak >= resume_streak:
                self._pure_quote_hb_pause_new_entries = False
            return True
        self._pure_quote_hb_fresh_streak = 0
        self._pure_quote_hb_fail_streak += 1
        if (not self._pure_quote_hb_pause_new_entries) and self._pure_quote_hb_fail_streak >= fail_to_pause:
            self._pure_quote_hb_pause_new_entries = True
        return not self._pure_quote_hb_pause_new_entries

    def _update_quote_data_health_gate(self, now: datetime) -> None:
        if self._pure_quote_health_checked:
            return
        extras = self._last_extras or {}
        start = _parse_hms(extras.get("quote_health_window_start", "09:45:00"), time(9, 45))
        end = _parse_hms(extras.get("quote_health_window_end", "10:00:00"), time(10, 0))
        if now.time() < end:
            return
        dt_start = datetime.combine(now.date(), start, tzinfo=now.tzinfo)
        dt_end = datetime.combine(now.date(), end, tzinfo=now.tzinfo)
        expected = max(1, int((dt_end - dt_start).total_seconds() // 60))
        counts = self._query_quote_minute_coverage(dt_start, dt_end)
        covered = int(counts.get("covered_minutes", 0))
        coverage = float(covered) / float(expected)
        threshold = float(extras.get("quote_health_min_coverage", 0.70) or 0.70)
        self._pure_quote_health_checked = True
        self._pure_quote_health_cov_pct = coverage
        self._pure_quote_health_block_day = coverage < threshold

    def _maybe_release_quote_data_health_gate(self, now: datetime) -> None:
        if not self._pure_quote_health_block_day:
            return
        extras = self._last_extras or {}
        if not bool(extras.get("quote_health_recovery_enabled", True)):
            return
        no_new_after = _parse_hms(extras.get("no_new_entry_after", "14:50:00"), time(14, 50))
        if now.time() >= no_new_after:
            return
        recovery_window_min = max(3, int(extras.get("quote_health_recovery_window_min", 10) or 10))
        recovery_min_coverage = float(extras.get("quote_health_recovery_min_coverage", 0.90) or 0.90)
        recovery_min_nbbo_coverage = float(extras.get("quote_health_recovery_min_nbbo_coverage", recovery_min_coverage) or recovery_min_coverage)
        recovery_max_age_sec = max(10.0, float(extras.get("quote_health_recovery_max_last_age_sec", 120.0) or 120.0))
        window_end = _minute_floor(now)
        window_start = window_end - timedelta(minutes=recovery_window_min)
        expected = max(1, recovery_window_min)
        counts = self._query_quote_minute_coverage(window_start, window_end)
        covered = int(counts.get("covered_minutes", 0))
        nbbo = int(counts.get("nbbo_minutes", 0))
        coverage = float(covered) / float(expected)
        nbbo_coverage = float(nbbo) / float(expected)
        last_age_sec = self._latest_quote_age_sec(now)
        if coverage >= recovery_min_coverage and nbbo_coverage >= recovery_min_nbbo_coverage and last_age_sec is not None and last_age_sec <= recovery_max_age_sec:
            self._pure_quote_health_block_day = False
            self._pure_quote_health_cov_pct = max(self._pure_quote_health_cov_pct, coverage)

    def _get_mark_for_contract_pure(self, now: datetime, opt_symbol: str) -> Optional[float]:
        extras = self._last_extras or {}
        try:
            parse_internal_opt_symbol(opt_symbol)
        except Exception:
            return None
        lookback_min = max(1, int(extras.get("mark_quote_lookback_min", 2) or 2))
        max_age_sec = max(1.0, float(extras.get("mark_quote_max_age_sec", extras.get("entry_quote_max_age_sec", 90.0)) or 90.0))
        mark_allow_ch_fallback = bool(extras.get("mark_quote_allow_ch_fallback", True))
        row = get_quote_for_opt_symbol(
            now=now,
            opt_symbol=opt_symbol,
            lookback_min=lookback_min,
            max_age_sec=max_age_sec,
            dte_min=0,
            dte_max=0,
            fallback_to_ch=mark_allow_ch_fallback,
        )
        if row is None:
            return None
        mid = float(row.get("mid") or 0.0)
        if mid > 0:
            return mid
        bid = float(row.get("bid") or 0.0)
        ask = float(row.get("ask") or 0.0)
        if bid > 0 and ask > 0:
            return (bid + ask) / 2.0
        return None

    def build_signal_pure(self, runtime: PluginRuntimeState) -> Optional[Signal]:
        self._pure_reset_if_needed(runtime)
        completed = self._complete_minute_bar(runtime.now, runtime.features)
        self._pure_current_completed_bar = None
        if completed is None:
            return None
        minute_ts = completed.get("ts")
        if isinstance(minute_ts, datetime) and self._pure_last_completed_minute_ts == minute_ts:
            return None
        self._pure_last_completed_minute_ts = minute_ts if isinstance(minute_ts, datetime) else runtime.now
        self._enrich_bar(completed)
        self._pure_bars.append(completed)
        self._pure_current_completed_bar = completed

        extras = self._last_extras or {}
        if runtime.active_position is not None:
            self._pure_pending = None
            self._emit_minute_observability(runtime, bar=completed, reason="POSITION_OPEN")
            return None
        self._update_quote_data_health_gate(runtime.now)
        self._maybe_release_quote_data_health_gate(runtime.now)
        gate_start = _parse_hms(extras.get("ab_time_start", "09:30:00"), time(9, 30))
        gate_end = _parse_hms(extras.get("ab_time_end", "15:59:00"), time(15, 59))
        if runtime.now.time() < gate_start or runtime.now.time() > gate_end:
            self._pure_pending = None
            self._emit_minute_observability(runtime, bar=completed, reason="OUTSIDE_ENTRY_WINDOW")
            return None
        no_new_after = _parse_hms(extras.get("no_new_entry_after", "15:00:00"), time(15, 0))
        if runtime.now.time() >= no_new_after or len(self._pure_bars) < 3:
            self._pure_pending = None
            reason = "AFTER_NO_NEW_ENTRY" if runtime.now.time() >= no_new_after else "INSUFFICIENT_BARS"
            self._emit_minute_observability(runtime, bar=completed, reason=reason)
            return None
        max_trades = int(extras.get("max_trades_per_day", 2) or 2)
        if self._pure_trades_filled_today >= max_trades:
            self._pure_pending = None
            self._emit_minute_observability(runtime, bar=completed, reason="MAX_TRADES_REACHED")
            return None
        if self._pure_quote_health_block_day:
            self._pure_pending = None
            self._emit_minute_observability(runtime, bar=completed, reason="QUOTE_HEALTH_BLOCK_DAY")
            return None
        if not self._quote_heartbeat_allows_entry(runtime.now):
            self._pure_pending = None
            self._emit_minute_observability(runtime, bar=completed, reason="QUOTE_HEARTBEAT_PAUSE")
            return None
        if self._pure_trades_filled_today >= 1 and max_trades >= 2 and bool(extras.get("second_trade_only_if_first_bad_exit", True)):
            allow = {str(x).strip().upper() for x in (extras.get("second_trade_first_exit_allow", ["HARD_STOP", "LATE_NO_MOVE_EXIT"]) or []) if str(x).strip()}
            if allow and str(self._pure_first_trade_exit_reason or "").upper() not in allow:
                self._pure_pending = None
                self._emit_minute_observability(runtime, bar=completed, reason="SECOND_TRADE_GATE_BLOCK")
                return None
        reserve_after_raw = extras.get("reserve_trade_after_time", "10:30:00")
        reserve_count = int(extras.get("reserve_trade_count", 1) or 1)
        if reserve_after_raw and reserve_count > 0 and max_trades > reserve_count:
            reserve_after = _parse_hms(reserve_after_raw, time(10, 30))
            early_cap = max_trades - reserve_count
            if runtime.now.time() < reserve_after and self._pure_trades_filled_today >= early_cap:
                self._pure_pending = None
                self._emit_minute_observability(runtime, bar=completed, reason="RESERVE_TRADE_GATE_BLOCK")
                return None
        bar = completed
        close = float(bar.get("close") or 0.0)
        vwap = float(bar.get("vwap") or 0.0)
        if close <= 0 or vwap <= 0:
            return None
        prev = self._pure_bars[-2]
        prev_high = float(prev.get("high") or 0.0)
        prev_low = float(prev.get("low") or 0.0)
        confirm_loc_min = float(extras.get("confirm_bar_close_loc_min", 0.55) or 0.55)
        rvol = bar.get("rvol")
        rvol_ok = rvol is not None and float(rvol) >= float(extras.get("rvol_min", 1.5) or 1.5)
        atr_1m = float(bar.get("atr_1m") or 0.0)
        atr_1m_ma = float(bar.get("atr_1m_ma") or 0.0)
        atr_ok = atr_1m_ma > 0 and atr_1m >= float(extras.get("atr_expansion_mult", 1.2) or 1.2) * atr_1m_ma
        trend_ok = bool(rvol_ok or atr_ok)

        if self._pure_pending is not None:
            direction = self._pure_pending.direction
            level = float(self._pure_pending.level)
            self._pure_pending = None
            if direction == "LONG":
                if close > level and close > vwap and self._close_loc(bar) >= confirm_loc_min and trend_ok and self._enter_with_gates(runtime.now, bar, direction="LONG", breakout_level=level):
                    self._emit_minute_observability(runtime, bar=completed, reason=None)
                    return Signal(ts=runtime.now, side="CALL", signal_kind="qqq_0dte_confirm", payload={"path": "A", "level": level})
            else:
                if close < level and close < vwap and self._close_loc(bar) <= (1.0 - confirm_loc_min) and trend_ok and self._enter_with_gates(runtime.now, bar, direction="SHORT", breakout_level=level):
                    self._emit_minute_observability(runtime, bar=completed, reason=None)
                    return Signal(ts=runtime.now, side="PUT", signal_kind="qqq_0dte_confirm", payload={"path": "A", "level": level})
            self._emit_minute_observability(runtime, bar=completed, reason="CONFIRM_NOT_MET")
            return None

        path_b_enabled = bool(extras.get("path_b_enabled", True))
        path_b_start = _parse_hms(extras.get("path_b_start", "10:00:00"), time(10, 0))
        path_b_dist_thr = float(extras.get("path_b_dist_atr_threshold", 0.15) or 0.15)
        if path_b_enabled and runtime.now.time() >= path_b_start and len(self._pure_bars) >= 6:
            hist = list(self._pure_bars)[-6:-1]
            dist_vals = []
            for hb in hist:
                hb_close = float(hb.get("close") or 0.0)
                hb_vwap = float(hb.get("vwap") or 0.0)
                hb_atr_d = self._resolve_atr_d(hb)
                if hb_close > 0 and hb_vwap > 0 and hb_atr_d > 0:
                    dist_vals.append((hb_close - hb_vwap) / hb_atr_d)
            if dist_vals:
                dist_roll_min = float(min(dist_vals))
                dist_roll_max = float(max(dist_vals))
                mom1_atr = (close - float(prev.get("close") or close)) / max(1e-9, atr_1m)
                if (dist_roll_min <= -path_b_dist_thr) and (close > prev_high) and (mom1_atr > 0.0):
                    self._emit_minute_observability(runtime, bar=completed, reason=None)
                    return Signal(ts=runtime.now, side="CALL", signal_kind="qqq_0dte_path_b", payload={"path": "B"})
                if (dist_roll_max >= path_b_dist_thr) and (close < prev_low) and (mom1_atr < 0.0):
                    self._emit_minute_observability(runtime, bar=completed, reason=None)
                    return Signal(ts=runtime.now, side="PUT", signal_kind="qqq_0dte_path_b", payload={"path": "B"})

        if close > prev_high and close > vwap:
            self._pure_pending = _PendingEntry(direction="LONG", level=prev_high, stage="await_confirm", created_at=runtime.now, bars_waited=0)
        elif close < prev_low and close < vwap:
            self._pure_pending = _PendingEntry(direction="SHORT", level=prev_low, stage="await_confirm", created_at=runtime.now, bars_waited=0)
        self._emit_minute_observability(runtime, bar=completed, reason="NO_SIGNAL")
        return None

    @staticmethod
    def _close_loc(bar: Dict[str, Any]) -> float:
        hi = float(bar.get("high") or 0.0)
        lo = float(bar.get("low") or 0.0)
        cl = float(bar.get("close") or 0.0)
        rng = max(1e-9, hi - lo)
        return (cl - lo) / rng

    def build_entry_plan_pure(self, runtime: PluginRuntimeState, signal: Signal) -> Optional[EntryPlan]:
        bar = dict(self._pure_current_completed_bar or {})
        if not bar:
            return None
        extras = self._last_extras or {}
        contracts = int(extras.get("contracts", extras.get("contracts_per_trade", 3)) or 3)
        return EntryPlan(
            ts=runtime.now,
            side=str(signal.side),
            signal_kind=str(signal.signal_kind),
            structure_stop=0.0,
            target_1r=0.0,
            target_1p2r=0.0,
            r_per_share=0.0,
            contracts=contracts,
            payload={
                "entry_vwap": float(bar.get("vwap") or 0.0),
                "entry_atr_1m": float(bar.get("atr_1m") or 0.0),
                "entry_counter_vwap": bool(float(bar.get("close") or 0.0) < float(bar.get("vwap") or bar.get("close") or 0.0)) if signal.side == "CALL" else bool(float(bar.get("close") or 0.0) > float(bar.get("vwap") or bar.get("close") or 0.0)),
            },
        )

    def on_entry_submitted_pure(
        self,
        *,
        runtime: PluginRuntimeState,
        signal: Optional[Signal],
        entry_plan: EntryPlan,
        selection: Selection,
        position_lot_id: str,
    ) -> None:
        self._pure_trades_filled_today += 1

    def select_contract_pure(self, runtime: PluginRuntimeState, entry_plan: EntryPlan) -> Optional[Selection]:
        quote, reason = self._select_0dte_quote_pure(
            runtime.now,
            side=str(entry_plan.side),
            spot=float(runtime.spot or 0.0),
            spread_max_pct=float((self._last_extras or {}).get("spread_max_pct", 0.15) or 0.15),
        )
        if quote is None:
            return None
        expiry = str(quote.get("expiry") or runtime.now.strftime("%Y%m%d"))
        return Selection(
            opt_symbol=str(quote.get("opt_symbol") or ""),
            expiry=expiry,
            strike=float(quote.get("strike") or 0.0),
            side=str(entry_plan.side),
            bid=float(quote.get("bid") or 0.0),
            ask=float(quote.get("ask") or 0.0),
            mid=float(quote.get("mid") or 0.0),
            last=float(quote.get("last") or 0.0),
            quote_age_sec=float(quote.get("quote_age_sec") or 0.0),
            payload={"reject_reason": str(reason or "")},
        )

    def build_execution_profile_pure(
        self,
        runtime: PluginRuntimeState,
        entry_plan: EntryPlan,
        selection: Optional[Selection],
    ) -> ExecutionProfile:
        extras = self._last_extras or {}
        return build_execution_profile_from_extras(
            profile_id=str(extras.get("live_parity_profile_id") or "qqq_0dte_option_live_v2"),
            market_data_profile_id=str(extras.get("market_data_profile_id") or "standard_close_extended_sma"),
            extras=extras,
        )

    def build_exit_plan_pure(self, runtime: PluginRuntimeState) -> Optional[ExitPlan]:
        pos = runtime.active_position
        bar = dict(self._pure_current_completed_bar or {})
        if pos is None or not bar:
            return None
        extras = self._last_extras or {}
        force_flatten_time = _parse_hms(extras.get("force_flatten_time", "15:58:00"), time(15, 58))
        if runtime.now.time() >= force_flatten_time:
            if self._pure_first_trade_exit_reason is None:
                self._pure_first_trade_exit_reason = "EOD_FORCE_FLATTEN"
            return ExitPlan(ts=runtime.now, position_lot_id=str(pos.position_lot_id), qty=float(pos.remaining_qty), reason="EOD_FORCE_FLATTEN")

        mark = self._get_mark_for_contract_pure(runtime.now, pos.opt_symbol)
        if mark is None or mark <= 0:
            return None
        payload = dict(pos.payload or {})
        entry_payload = dict(payload.get("entry_plan_payload") or {})
        entry_premium = safe_float(payload.get("entry_mid"), pos.avg_entry_px)
        if entry_premium <= 0:
            return None
        held_min = max(0.0, (runtime.now - pos.opened_at).total_seconds() / 60.0)
        opt_return = (float(mark) - entry_premium) / entry_premium
        initial_qty = max(1.0, safe_float(payload.get("initial_qty"), pos.qty))
        tp1_done = float(pos.remaining_qty) < float(initial_qty)

        tp1_mult = float(extras.get("tp1_mult", 1.20) or 1.20)
        tp2_mult = float(extras.get("tp2_mult", 2.50) or 2.50)
        stop_mult = float(extras.get("stop_loss_mult", 0.65) or 0.65)

        if tp1_done and mark >= entry_premium * tp2_mult:
            if self._pure_first_trade_exit_reason is None:
                self._pure_first_trade_exit_reason = "TP2"
            return ExitPlan(ts=runtime.now, position_lot_id=str(pos.position_lot_id), qty=float(pos.remaining_qty), reason="TP2")
        if (not tp1_done) and mark >= entry_premium * tp1_mult:
            return ExitPlan(ts=runtime.now, position_lot_id=str(pos.position_lot_id), qty=min(2.0, float(pos.remaining_qty)), reason="TP1")
        if tp1_done:
            floor = entry_premium * tp1_mult
            if mark >= entry_premium * 1.60:
                floor = max(floor, entry_premium * 1.40)
            if mark >= entry_premium * 2.00:
                floor = max(floor, entry_premium * 1.70)
            if mark <= floor:
                if self._pure_first_trade_exit_reason is None:
                    self._pure_first_trade_exit_reason = "RUNNER_PROTECT"
                return ExitPlan(ts=runtime.now, position_lot_id=str(pos.position_lot_id), qty=float(pos.remaining_qty), reason="RUNNER_PROTECT")
        if mark <= entry_premium * stop_mult:
            if self._pure_first_trade_exit_reason is None:
                self._pure_first_trade_exit_reason = "HARD_STOP"
            return ExitPlan(ts=runtime.now, position_lot_id=str(pos.position_lot_id), qty=float(pos.remaining_qty), reason="HARD_STOP")

        atr_d = self._resolve_atr_d(bar)
        no_move_min_hold = float(extras.get("no_move_min_hold_min", 5.0) or 5.0)
        no_move_atr = float(extras.get("no_move_underlying_move_atr", 0.15) or 0.15)
        no_move_min_ret = float(extras.get("no_move_min_opt_return", 0.10) or 0.10)
        if held_min >= no_move_min_hold and atr_d > 0:
            move = abs(float(bar.get("close") or 0.0) - safe_float(payload.get("entry_underlying"), runtime.spot)) / atr_d
            if move < no_move_atr and opt_return < no_move_min_ret:
                if self._pure_first_trade_exit_reason is None:
                    self._pure_first_trade_exit_reason = "LATE_NO_MOVE_EXIT"
                return ExitPlan(ts=runtime.now, position_lot_id=str(pos.position_lot_id), qty=float(pos.remaining_qty), reason="LATE_NO_MOVE_EXIT")

        if (not bool(extras.get("vwap_exit_only_pre_tp1", True))) or (not tp1_done):
            vwap_exit_minutes = float(extras.get("vwap_exit_minutes", 12.0) or 12.0)
            vwap = float(bar.get("vwap") or 0.0)
            close = float(bar.get("close") or 0.0)
            if held_min >= vwap_exit_minutes and vwap > 0:
                if pos.side == "CALL" and close < vwap:
                    if self._pure_first_trade_exit_reason is None:
                        self._pure_first_trade_exit_reason = "VWAP_EXIT"
                    return ExitPlan(ts=runtime.now, position_lot_id=str(pos.position_lot_id), qty=float(pos.remaining_qty), reason="VWAP_EXIT")
                if pos.side == "PUT" and close > vwap:
                    if self._pure_first_trade_exit_reason is None:
                        self._pure_first_trade_exit_reason = "VWAP_EXIT"
                    return ExitPlan(ts=runtime.now, position_lot_id=str(pos.position_lot_id), qty=float(pos.remaining_qty), reason="VWAP_EXIT")
        return None
