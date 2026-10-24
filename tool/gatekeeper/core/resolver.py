from __future__ import annotations

from datetime import datetime
from dataclasses import dataclass
from typing import Any
import time

from core.cache import GatekeeperCache, Scope, make_field_id
from core.features import (
    ChopDetails,
    compute_chop,
    compute_vwap,
    gap_from_open_pct,
    last3_relation_to_vwap,
    range_pct,
    vwap_deviation_pct,
)
from core.time_utils import iso_to_dt, now_iso, us_equity_session_lookback_min
from core.types import ResolvedInput, ResolvedInputs
from providers.ibkr import IBKRConnectionParams, IBKRProvider


def _missing(ts: str) -> ResolvedInput:
    return ResolvedInput(value=None, source="MISSING", ts=ts, is_manual_lock=False)


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _age_seconds(ts: str) -> float | None:
    try:
        dt = iso_to_dt(ts)
    except Exception:
        return None
    try:
        return (datetime.now().astimezone() - dt).total_seconds()
    except Exception:
        return None


@dataclass(slots=True)
class GlobalInputs:
    anchor_symbol: str
    event_confirmed: bool
    event_level: str | None
    bofa_9_3: float | None
    spread_width_usd: float | None


class GatekeeperResolver:
    def __init__(
        self,
        *,
        config: dict[str, Any],
        provider: IBKRProvider,
        cache: GatekeeperCache,
        manual_overrides: dict[str, ResolvedInput],
    ) -> None:
        self.config = config
        self.provider = provider
        self.cache = cache
        self.manual_overrides = manual_overrides

    def connect_ibkr(self) -> tuple[bool, str]:
        ibkr_cfg = self.config.get("data", {}).get("ibkr", {})
        params = IBKRConnectionParams(
            host=str(ibkr_cfg.get("host", "127.0.0.1")),
            port=int(ibkr_cfg.get("port", 4002)),
            client_id=int(ibkr_cfg.get("clientId", 7)),
            market_data_type=int(ibkr_cfg.get("market_data_type", 1)),
        )
        return self.provider.connect(params)

    def _manual(self, field_id: str) -> ResolvedInput | None:
        return self.manual_overrides.get(field_id)

    def _cache(self, field_id: str) -> ResolvedInput | None:
        return self.cache.get(field_id)

    def _set_cache(self, field_id: str, value: ResolvedInput) -> None:
        self.cache.set(field_id, value)

    def _resolve_auto(
        self,
        *,
        scope: Scope,
        symbol: str,
        key: str,
        fetch_auto: bool,
        max_age_s: float | None = None,
        fetcher,
    ) -> ResolvedInput:
        ts = now_iso()
        field_id = make_field_id(scope, key, symbol=symbol)
        error_field_id = make_field_id(scope, f"{key}__error", symbol=symbol)

        manual = self._manual(field_id)
        if manual is not None:
            return manual

        if fetch_auto:
            timing_field_id = make_field_id(scope, f"{key}__timing", symbol=symbol)
            cached = self._cache(field_id)
            if cached is not None and max_age_s is not None:
                age_s = _age_seconds(str(cached.ts))
                if age_s is not None and age_s <= float(max_age_s):
                    return cached

            start = time.perf_counter()
            value, err = fetcher()
            elapsed = round(time.perf_counter() - start, 3)
            self._set_cache(
                timing_field_id,
                ResolvedInput(
                    value={"elapsed_s": elapsed, "error": str(err) if err else None},
                    source="CACHE",
                    ts=ts,
                    is_manual_lock=False,
                ),
            )
            if err is None and value is not None:
                ri = ResolvedInput(value=value, source="IBKR", ts=ts, is_manual_lock=False)
                self._set_cache(field_id, ri)
                self.cache.delete(error_field_id)
                return ri
            if err:
                self._set_cache(
                    error_field_id,
                    ResolvedInput(value=str(err), source="CACHE", ts=ts, is_manual_lock=False),
                )

            cached = self._cache(field_id)
            if cached is not None:
                return ResolvedInput(
                    value=cached.value,
                    source="CACHE_STALE",
                    ts=cached.ts,
                    is_manual_lock=cached.is_manual_lock,
                )
            return _missing(ts)

        timing_field_id = make_field_id(scope, f"{key}__timing", symbol=symbol)
        self._set_cache(
            timing_field_id,
            ResolvedInput(
                value={"skipped": True, "reason": "fetch_auto_false"},
                source="CACHE",
                ts=ts,
                is_manual_lock=False,
            ),
        )
        cached = self._cache(field_id)
        if cached is not None:
            return cached
        return _missing(ts)

    def _resolve_computed(
        self,
        *,
        scope: Scope,
        symbol: str,
        key: str,
        manual_allowed: bool,
        compute_fn,
    ) -> ResolvedInput:
        ts = now_iso()
        field_id = make_field_id(scope, key, symbol=symbol)

        if manual_allowed:
            manual = self._manual(field_id)
            if manual is not None:
                return manual

        value = compute_fn()
        if value is None:
            cached = self._cache(field_id)
            if cached is not None:
                return cached
            return _missing(ts)

        ri = ResolvedInput(value=value, source="CACHE", ts=ts, is_manual_lock=False)
        self._set_cache(field_id, ri)
        return ri

    def resolve_global(self, global_inputs: GlobalInputs) -> ResolvedInputs:
        ts = now_iso()

        resolved: ResolvedInputs = {}
        resolved["anchor_symbol"] = ResolvedInput(
            value=global_inputs.anchor_symbol, source="MANUAL", ts=ts, is_manual_lock=False
        )
        resolved["event_confirmed"] = ResolvedInput(
            value=bool(global_inputs.event_confirmed), source="MANUAL", ts=ts, is_manual_lock=False
        )
        resolved["event_level"] = ResolvedInput(
            value=str(global_inputs.event_level or "None"),
            source="MANUAL",
            ts=ts,
            is_manual_lock=False,
        )

        if global_inputs.bofa_9_3 is None:
            resolved["bofa_9_3"] = _missing(ts)
        else:
            resolved["bofa_9_3"] = ResolvedInput(
                value=float(global_inputs.bofa_9_3), source="MANUAL", ts=ts, is_manual_lock=False
            )

        if global_inputs.spread_width_usd is None:
            resolved["spread_width_usd"] = _missing(ts)
        else:
            resolved["spread_width_usd"] = ResolvedInput(
                value=float(global_inputs.spread_width_usd),
                source="MANUAL",
                ts=ts,
                is_manual_lock=False,
            )

        resolved["run_mode"] = ResolvedInput(value="MANUAL_RUN", source="MANUAL", ts=ts, is_manual_lock=False)
        return resolved

    def resolve_anchor(self, anchor_symbol: str, *, fetch_auto: bool) -> ResolvedInputs:
        bars_cfg = self.config.get("data", {}).get("bars_1m", {})
        lookback_min = int(bars_cfg.get("lookback_min", 90))
        bar_size = str(bars_cfg.get("bar_size", "1 min"))
        what_to_show = str(bars_cfg.get("what_to_show", "TRADES"))
        timeout_s = float(bars_cfg.get("timeout_s", 8))
        session_lookback_min = us_equity_session_lookback_min()
        vwap_lookback_min = max(lookback_min, session_lookback_min)
        vwap_use_rth = True
        effective_timeout = timeout_s

        resolved: ResolvedInputs = {}
        resolved["anchor_price"] = self._resolve_auto(
            scope="ANCHOR",
            symbol=anchor_symbol,
            key="anchor_price",
            fetch_auto=fetch_auto,
            max_age_s=15,
            fetcher=lambda: self.provider.fetch_last_price(
                anchor_symbol,
                timeout_s=effective_timeout,
                history_lookback_min=min(lookback_min, 5),
                history_use_rth=False,
            ),
        )

        resolved["anchor_bars_1m"] = self._resolve_auto(
            scope="ANCHOR",
            symbol=anchor_symbol,
            key="anchor_bars_1m",
            fetch_auto=fetch_auto,
            max_age_s=60,
            fetcher=lambda: self.provider.fetch_bars_1m(
                symbol=anchor_symbol,
                lookback_min=vwap_lookback_min,
                use_rth=vwap_use_rth,
                bar_size=bar_size,
                what_to_show=what_to_show,
                timeout_s=effective_timeout,
            ),
        )

        anchor_bars = resolved["anchor_bars_1m"].value if resolved["anchor_bars_1m"].source != "MISSING" else []
        if resolved["anchor_price"].source == "MISSING" and isinstance(anchor_bars, list) and anchor_bars:
            last_close = anchor_bars[-1].get("close")
            if last_close is not None:
                resolved["anchor_price"] = ResolvedInput(
                    value=float(last_close), source="CACHE", ts=now_iso(), is_manual_lock=False
                )

        resolved["anchor_vwap"] = self._resolve_computed(
            scope="ANCHOR",
            symbol=anchor_symbol,
            key="anchor_vwap",
            manual_allowed=True,
            compute_fn=lambda: compute_vwap(anchor_bars) if isinstance(anchor_bars, list) else None,
        )
        if isinstance(anchor_bars, list) and anchor_bars:
            last_bar_time = anchor_bars[-1].get("time")
            if last_bar_time:
                self._set_cache(
                    make_field_id("ANCHOR", "anchor_vwap_asof", symbol=anchor_symbol),
                    ResolvedInput(value=str(last_bar_time), source="CACHE", ts=now_iso(), is_manual_lock=False),
                )

        vix_fetch_auto = fetch_auto
        resolved["vix"] = self._resolve_auto(
            scope="ANCHOR",
            symbol=anchor_symbol,
            key="vix",
            fetch_auto=vix_fetch_auto,
            max_age_s=60,
            fetcher=lambda: self.provider.fetch_last_price(
                "VIX",
                timeout_s=effective_timeout,
                history_lookback_min=10,
                history_use_rth=False,
            ),
        )
        vvix_fetch_auto = fetch_auto
        resolved["vvix"] = self._resolve_auto(
            scope="ANCHOR",
            symbol=anchor_symbol,
            key="vvix",
            fetch_auto=vvix_fetch_auto,
            max_age_s=60,
            fetcher=lambda: self.provider.fetch_last_price(
                "VVIX",
                timeout_s=effective_timeout,
                history_lookback_min=10,
                history_use_rth=False,
            ),
        )

        def _compute_drop_15m_pct() -> float | None:
            if not isinstance(anchor_bars, list) or len(anchor_bars) < 16:
                return None
            try:
                first_dt = iso_to_dt(str(anchor_bars[0].get("time")))
            except Exception:
                return None
            assumed_ny = first_dt
            if assumed_ny.tzinfo is None:
                # Heuristic: IBKR bars often come without tz; assume NY if matching market open.
                assumed_ny = assumed_ny.replace(tzinfo=None)
            if not (assumed_ny.hour == 9 and assumed_ny.minute == 30):
                return None
            open_px = float(anchor_bars[0].get("open") or 0.0)
            close_15 = float(anchor_bars[14].get("close") or 0.0)
            if open_px <= 0:
                return None
            return (open_px - close_15) / open_px * 100.0

        resolved["anchor_drop_15m_pct"] = self._resolve_computed(
            scope="ANCHOR",
            symbol=anchor_symbol,
            key="anchor_drop_15m_pct",
            manual_allowed=True,
            compute_fn=_compute_drop_15m_pct,
        )

        chop_cfg = self.config.get("chop", {})
        weights = chop_cfg.get("weights", {})

        vwap_value = _as_float(resolved["anchor_vwap"].value)
        chop_details = compute_chop(
            anchor_bars if isinstance(anchor_bars, list) else [],
            vwap_value,
            window_min=int(chop_cfg.get("window_min", 30)),
            band_pct=float(chop_cfg.get("band_pct", 0.0015)),
            cross_min=int(chop_cfg.get("cross_min", 4)),
            touch_min=int(chop_cfg.get("touch_min", 10)),
            trend_cap_pct=float(chop_cfg.get("trend_cap_pct", 0.004)),
            w_cross=float(weights.get("cross", 0.5)),
            w_touch=float(weights.get("touch", 0.3)),
            w_trend=float(weights.get("trend", 0.2)),
        )

        def _compute_chop_score() -> float | None:
            return chop_details.chop_score if chop_details else None

        resolved["anchor_chop_score"] = self._resolve_computed(
            scope="ANCHOR",
            symbol=anchor_symbol,
            key="anchor_chop_score",
            manual_allowed=True,
            compute_fn=_compute_chop_score,
        )

        score_trigger = float(chop_cfg.get("score_trigger", 1.0))

        def _compute_is_choppy() -> bool | None:
            if chop_details is None:
                return None
            return chop_details.chop_score >= score_trigger

        resolved["anchor_is_choppy"] = self._resolve_computed(
            scope="ANCHOR",
            symbol=anchor_symbol,
            key="anchor_is_choppy",
            manual_allowed=True,
            compute_fn=_compute_is_choppy,
        )

        if chop_details:
            debug_id = make_field_id("ANCHOR", "anchor_chop_details", symbol=anchor_symbol)
            self._set_cache(
                debug_id,
                ResolvedInput(
                    value={
                        "cross_count": chop_details.cross_count,
                        "touch_count": chop_details.touch_count,
                        "trend_pct": chop_details.trend_pct,
                        "cross_score": chop_details.cross_score,
                        "touch_score": chop_details.touch_score,
                        "trend_score": chop_details.trend_score,
                        "chop_score": chop_details.chop_score,
                    },
                    source="CACHE",
                    ts=now_iso(),
                    is_manual_lock=False,
                ),
            )

        return resolved

    def resolve_ticker(self, ticker_symbol: str, *, fetch_auto: bool, include_options: bool = True) -> ResolvedInputs:
        bars_cfg = self.config.get("data", {}).get("bars_1m", {})
        lookback_min = int(bars_cfg.get("lookback_min", 90))
        bar_size = str(bars_cfg.get("bar_size", "1 min"))
        what_to_show = str(bars_cfg.get("what_to_show", "TRADES"))
        timeout_s = float(bars_cfg.get("timeout_s", 8))
        session_lookback_min = us_equity_session_lookback_min()
        vwap_lookback_min = max(lookback_min, session_lookback_min)
        vwap_use_rth = True
        effective_timeout = timeout_s

        resolved: ResolvedInputs = {}
        ts = now_iso()

        resolved["ticker_symbol"] = ResolvedInput(value=ticker_symbol, source="MANUAL", ts=ts, is_manual_lock=False)

        resolved["ticker_price"] = self._resolve_auto(
            scope="TICKER",
            symbol=ticker_symbol,
            key="ticker_price",
            fetch_auto=fetch_auto,
            max_age_s=15,
            fetcher=lambda: self.provider.fetch_last_price(
                ticker_symbol,
                timeout_s=effective_timeout,
                history_lookback_min=min(lookback_min, 5),
                history_use_rth=False,
            ),
        )

        resolved["ticker_bars_1m"] = self._resolve_auto(
            scope="TICKER",
            symbol=ticker_symbol,
            key="ticker_bars_1m",
            fetch_auto=fetch_auto,
            max_age_s=60,
            fetcher=lambda: self.provider.fetch_bars_1m(
                symbol=ticker_symbol,
                lookback_min=vwap_lookback_min,
                use_rth=vwap_use_rth,
                bar_size=bar_size,
                what_to_show=what_to_show,
                timeout_s=effective_timeout,
            ),
        )

        ticker_bars = resolved["ticker_bars_1m"].value if resolved["ticker_bars_1m"].source != "MISSING" else []
        if resolved["ticker_price"].source == "MISSING" and isinstance(ticker_bars, list) and ticker_bars:
            last_close = ticker_bars[-1].get("close")
            if last_close is not None:
                resolved["ticker_price"] = ResolvedInput(
                    value=float(last_close), source="CACHE", ts=now_iso(), is_manual_lock=False
                )

        resolved["ticker_vwap"] = self._resolve_computed(
            scope="TICKER",
            symbol=ticker_symbol,
            key="ticker_vwap",
            manual_allowed=True,
            compute_fn=lambda: compute_vwap(ticker_bars) if isinstance(ticker_bars, list) else None,
        )
        if isinstance(ticker_bars, list) and ticker_bars:
            last_bar_time = ticker_bars[-1].get("time")
            if last_bar_time:
                self._set_cache(
                    make_field_id("TICKER", "ticker_vwap_asof", symbol=ticker_symbol),
                    ResolvedInput(value=str(last_bar_time), source="CACHE", ts=now_iso(), is_manual_lock=False),
                )

        earnings_id = make_field_id("TICKER", "earnings_lt_5d", symbol=ticker_symbol)
        manual_earnings = self._manual(earnings_id)
        if manual_earnings is None:
            resolved["earnings_lt_5d"] = ResolvedInput(value=False, source="MANUAL", ts=ts, is_manual_lock=False)
        else:
            resolved["earnings_lt_5d"] = manual_earnings

        options_fetch = bool(fetch_auto and include_options)

        resolved["ivr"] = self._resolve_auto(
            scope="TICKER",
            symbol=ticker_symbol,
            key="ivr",
            fetch_auto=options_fetch,
            max_age_s=20 * 3600,
            fetcher=lambda: self.provider.fetch_ivr(ticker_symbol),
        )

        resolved["skew_value"] = self._resolve_auto(
            scope="TICKER",
            symbol=ticker_symbol,
            key="skew_value",
            fetch_auto=options_fetch,
            max_age_s=20 * 3600,
            fetcher=lambda: self.provider.fetch_skew_value(ticker_symbol),
        )

        resolved["atm_spread_pct"] = self._resolve_auto(
            scope="TICKER",
            symbol=ticker_symbol,
            key="atm_spread_pct",
            fetch_auto=options_fetch,
            max_age_s=15 * 60,
            fetcher=lambda: self.provider.fetch_atm_option_spread_pct(ticker_symbol),
        )

        anchor_symbol_cfg = str(self.config.get("data", {}).get("anchor_symbol", "QQQ")).upper()

        def _compute_css() -> int | None:
            ivr_val = _as_float(resolved.get("ivr").value if resolved.get("ivr") else None)
            spread_pct = _as_float(
                resolved.get("atm_spread_pct").value if resolved.get("atm_spread_pct") else None
            )
            if ivr_val is None or spread_pct is None:
                return None

            anchor_vvix = self.cache.get(make_field_id("ANCHOR", "vvix", symbol=anchor_symbol_cfg))
            vvix_field_id = make_field_id("ANCHOR", "vvix", symbol=anchor_symbol_cfg)
            anchor_vvix = self._manual(vvix_field_id) or self._cache(vvix_field_id)
            vvix_val = _as_float(anchor_vvix.value) if anchor_vvix and anchor_vvix.source != "MISSING" else None
            stability_method = "VVIX"
            if vvix_val is None:
                jumpy, err = self.provider.fetch_jumpy(ticker_symbol)
                if err or jumpy is None:
                    return None
                stability_method = "JUMPY"
                if jumpy < 2.0:
                    stability_score = 1.00
                elif jumpy < 4.0:
                    stability_score = 0.70
                elif jumpy < 6.0:
                    stability_score = 0.45
                else:
                    stability_score = 0.20
            else:
                if vvix_val <= 95:
                    stability_score = 1.00
                elif vvix_val <= 105:
                    stability_score = 0.75
                elif vvix_val <= 115:
                    stability_score = 0.45
                else:
                    stability_score = 0.20

            if ivr_val < 20:
                premium_score = 0.20
            elif ivr_val < 50:
                premium_score = 0.60
            elif ivr_val <= 80:
                premium_score = 1.00
            else:
                premium_score = 0.55

            if spread_pct <= 0.8:
                liquidity_score = 1.00
            elif spread_pct <= 1.5:
                liquidity_score = 0.70
            elif spread_pct <= 2.5:
                liquidity_score = 0.40
            else:
                liquidity_score = 0.20

            css = round(100.0 * (0.45 * premium_score + 0.35 * stability_score + 0.20 * liquidity_score))
            css = max(0, min(100, int(css)))

            debug_id = make_field_id("TICKER", "css_debug", symbol=ticker_symbol)
            self._set_cache(
                debug_id,
                ResolvedInput(
                    value={
                        "ivr": ivr_val,
                        "vvix": vvix_val,
                        "stability_method": stability_method,
                        "weights": {"premium": 0.45, "stability": 0.35, "liquidity": 0.20},
                        "atm_spread_pct": spread_pct,
                        "premium_score": premium_score,
                        "stability_score": stability_score,
                        "liquidity_score": liquidity_score,
                        "css": css,
                    },
                    source="CACHE",
                    ts=now_iso(),
                    is_manual_lock=False,
                ),
            )
            return css

        resolved["css"] = self._resolve_computed(
            scope="TICKER",
            symbol=ticker_symbol,
            key="css",
            manual_allowed=True,
            compute_fn=_compute_css,
        )

        vwap_value = _as_float(resolved["ticker_vwap"].value)
        last_close = None
        if isinstance(ticker_bars, list) and ticker_bars:
            last_close = _as_float(ticker_bars[-1].get("close"))

        debug_id = make_field_id("TICKER", "ticker_debug", symbol=ticker_symbol)
        self._set_cache(
            debug_id,
            ResolvedInput(
                value={
                    "last3_close_relation": last3_relation_to_vwap(
                        ticker_bars if isinstance(ticker_bars, list) else [], vwap_value
                    ),
                    "range_5m_pct": range_pct(ticker_bars if isinstance(ticker_bars, list) else [], 5),
                    "range_15m_pct": range_pct(ticker_bars if isinstance(ticker_bars, list) else [], 15),
                    "gap_from_open_pct": gap_from_open_pct(ticker_bars if isinstance(ticker_bars, list) else []),
                    "vwap_dev_pct": vwap_deviation_pct(last_close, vwap_value),
                },
                source="CACHE",
                ts=now_iso(),
                is_manual_lock=False,
            ),
        )

        return resolved
