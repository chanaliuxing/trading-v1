"""Options chain data provider with multiple backends (stub, IBKR, ClickHouse)."""

from __future__ import annotations
import asyncio
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta
import math
from typing import List, Dict, Any, Optional, Tuple
from zoneinfo import ZoneInfo
import json
from pathlib import Path
import time

from libs.db.ch_pool import threadsafe_query
from services.exec.models import OrderState
from services.ibkr.client_id_registry import get_client_id_registry
from libs.logger import get_logger

ET = ZoneInfo("America/New_York")

@dataclass
class OptionContract:
    """Single option contract with market data and Greeks."""

    symbol: str  # OCC symbol
    underlying: str
    expiry_date: str  # YYYY-MM-DD
    strike: float
    right: str  # 'C' or 'P'
    bid: float
    ask: float
    last: Optional[float] = None
    volume: Optional[int] = None
    open_interest: Optional[int] = None
    delta: Optional[float] = None
    gamma: Optional[float] = None
    vega: Optional[float] = None
    theta: Optional[float] = None
    implied_vol: Optional[float] = None
    last_trade_time: Optional[datetime] = None
    bid_size: Optional[float] = None
    ask_size: Optional[float] = None

    @property
    def mid(self) -> float:
        """Mid price between bid and ask."""
        if self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2.0
        return self.last or 0.0

    @property
    def spread(self) -> float:
        """Bid-ask spread in dollars."""
        if self.bid > 0 and self.ask > 0:
            return self.ask - self.bid
        return 0.0

    @property
    def spread_bps(self) -> float:
        """Bid-ask spread in basis points relative to mid."""
        mid = self.mid
        if mid > 0:
            return (self.spread / mid) * 10000
        return 0.0


class OptionsDataProvider(ABC):
    """Abstract interface for fetching option chain data."""

    @abstractmethod
    async def get_chain(
        self,
        underlying: str,
        dte_candidates: List[int],
        right: Optional[str] = None,
    ) -> List[OptionContract]:
        """Fetch option chain for given underlying and DTE preferences.

        Args:
            underlying: Underlying symbol (e.g., 'SPY')
            dte_candidates: List of preferred days-to-expiration (e.g., [7, 14, 30])
            right: Optional filter for 'C' (calls) or 'P' (puts), None for both

        Returns:
            List of OptionContract objects matching criteria
        """
        pass

    @abstractmethod
    async def get_underlying_price(self, symbol: str) -> Optional[float]:
        """Get current price of underlying symbol."""
        pass

    async def place_vertical_spread(
        self,
        underlying: str,
        expiry: str,
        short_strike: float,
        long_strike: float,
        qty: int,
        *,
        side: str = "SELL",
        right: str = "P",
        limit_credit: Optional[float] = None,
        account: Optional[str] = None,
        transmit: bool = False,
             oca_group: Optional[str] = None,
     ) -> Dict[str, Any]:
        """Place a vertical spread order. Default stub: not implemented."""
        raise NotImplementedError("place_vertical_spread not implemented for this provider")

    @staticmethod
    def _normalize_status(status: Optional[str]) -> str:
        """Normalize provider-specific status strings to a small, uppercase set."""
        if not status:
            return "UNKNOWN"
        s = status.strip().upper()
        if s in {"PRESUBMITTED", "SUBMITTED", "NEW"}:
            return OrderState.SUBMITTED.value
        if s in {"FILLED", "FILLED-COMPLETE"}:
            return OrderState.FILLED.value
        if s in {"PARTIALLY FILLED", "PARTIAL"}:
            return OrderState.PARTIAL.value
        if s in {"CANCELLED", "CANCELED", "INACTIVE", "APICANCELLED"}:
            return OrderState.CANCELLED.value
        if s in {"REJECTED", "INVALID_CONTRACT", "ERROR"}:
            return OrderState.REJECTED.value
        if s == "NEW":
            return OrderState.NEW.value
        return s


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return (1.0 / math.sqrt(2.0 * math.pi)) * math.exp(-0.5 * x * x)


def _bs_price(
    spot: float,
    strike: float,
    t_years: float,
    r: float,
    q: float,
    sigma: float,
    right: str,
) -> float:
    if spot <= 0 or strike <= 0 or t_years <= 0 or sigma <= 0:
        return float("nan")
    sqrt_t = math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (r - q + 0.5 * sigma * sigma) * t_years) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    disc_q = math.exp(-q * t_years)
    disc_r = math.exp(-r * t_years)
    if right == "C":
        return disc_q * spot * _norm_cdf(d1) - disc_r * strike * _norm_cdf(d2)
    return disc_r * strike * _norm_cdf(-d2) - disc_q * spot * _norm_cdf(-d1)


def _implied_vol_bisect(
    target_price: float,
    spot: float,
    strike: float,
    t_years: float,
    r: float,
    q: float,
    right: str,
    lo: float = 1e-6,
    hi: float = 5.0,
    iters: int = 60,
) -> Optional[float]:
    if target_price <= 0 or spot <= 0 or strike <= 0 or t_years <= 0:
        return None
    p_lo = _bs_price(spot, strike, t_years, r, q, lo, right)
    p_hi = _bs_price(spot, strike, t_years, r, q, hi, right)
    if math.isnan(p_lo) or math.isnan(p_hi):
        return None
    if not (min(p_lo, p_hi) <= target_price <= max(p_lo, p_hi)):
        return None
    low = lo
    high = hi
    for _ in range(iters):
        mid = 0.5 * (low + high)
        p_mid = _bs_price(spot, strike, t_years, r, q, mid, right)
        if math.isnan(p_mid):
            return None
        if p_mid > target_price:
            high = mid
        else:
            low = mid
    return 0.5 * (low + high)


def _bs_greeks(
    spot: float,
    strike: float,
    t_years: float,
    r: float,
    q: float,
    sigma: float,
    right: str,
) -> Tuple[float, float, float, float]:
    sqrt_t = math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (r - q + 0.5 * sigma * sigma) * t_years) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    disc_q = math.exp(-q * t_years)
    disc_r = math.exp(-r * t_years)
    pdf = _norm_pdf(d1)

    gamma = disc_q * pdf / (spot * sigma * sqrt_t)
    vega = disc_q * spot * pdf * sqrt_t
    if right == "C":
        delta = disc_q * _norm_cdf(d1)
        theta = (
            -disc_q * spot * pdf * sigma / (2.0 * sqrt_t)
            - r * disc_r * strike * _norm_cdf(d2)
            + q * disc_q * spot * _norm_cdf(d1)
        )
    else:
        delta = disc_q * (_norm_cdf(d1) - 1.0)
        theta = (
            -disc_q * spot * pdf * sigma / (2.0 * sqrt_t)
            + r * disc_r * strike * _norm_cdf(-d2)
            - q * disc_q * spot * _norm_cdf(-d1)
        )
    return delta, gamma, vega, theta


class StubOptionsDataProvider(OptionsDataProvider):
    """Stub provider for testing - generates synthetic option chain data."""

    def __init__(self, stub_data_path: Optional[str] = None):
        """Initialize stub provider.

        Args:
            stub_data_path: Optional path to JSON file with pre-canned data
        """
        self.stub_data_path = stub_data_path
        self._cache: Dict[str, List[OptionContract]] = {}

    async def get_chain(
        self,
        underlying: str,
        dte_candidates: List[int],
        right: Optional[str] = None,
    ) -> List[OptionContract]:
        """Generate synthetic option chain."""
        # Try loading from file if provided
        if self.stub_data_path:
            data = self._load_stub_data(underlying)
            if data:
                return self._filter_contracts(data, dte_candidates, right)

        # Generate synthetic data
        underlying_px = await self.get_underlying_price(underlying) or 450.0
        contracts = []

        for dte in dte_candidates:
            expiry = (datetime.now() + timedelta(days=dte)).strftime('%Y-%m-%d')

            # Generate strikes around current price (±10%)
            strikes = self._generate_strikes(underlying_px)

            for strike in strikes:
                for opt_right in ['C', 'P']:
                    if right and opt_right != right:
                        continue

                    # Synthetic Greeks and prices
                    delta = self._calculate_synthetic_delta(underlying_px, strike, opt_right, dte)
                    mid_price = self._calculate_synthetic_price(underlying_px, strike, opt_right, dte)

                    # Realistic tight spread: 10-20 bps for liquid options
                    # For mid=5.0, spread of 0.01 = 20 bps
                    spread_bps = 15.0  # Target 15 bps
                    spread_dollars = mid_price * (spread_bps / 10000.0)
                    half_spread = spread_dollars / 2.0

                    contract = OptionContract(
                        symbol=f"{underlying}_{expiry}_{opt_right}_{strike}",  # Simplified, not OCC
                        underlying=underlying,
                        expiry_date=expiry,
                        strike=strike,
                        right=opt_right,
                        bid=mid_price - half_spread,
                        ask=mid_price + half_spread,
                        last=mid_price,
                        volume=1000 + int(dte * 100),
                        open_interest=5000 + int(dte * 500),
                        delta=delta,
                        gamma=0.01,
                        vega=0.15,
                        theta=-0.05,
                        implied_vol=0.20 + (dte / 365) * 0.05,
                        last_trade_time=datetime.now() - timedelta(minutes=2),
                    )
                    contracts.append(contract)

        return contracts

    def _generate_strikes(self, spot: float, pct_range: float = 0.10, step_pct: float = 0.01) -> List[float]:
        """Generate strike prices around spot."""
        strikes = []
        lower = spot * (1 - pct_range)
        upper = spot * (1 + pct_range)
        step = spot * step_pct

        current = lower
        while current <= upper:
            # Round to nearest 0.5 or 1.0 depending on price level
            if spot < 100:
                strikes.append(round(current * 2) / 2)
            else:
                strikes.append(round(current))
            current += step

        return sorted(set(strikes))

    def _calculate_synthetic_delta(self, spot: float, strike: float, right: str, dte: int) -> float:
        """Calculate approximate delta using simple heuristic."""
        moneyness = (spot - strike) / spot if right == 'C' else (strike - spot) / spot

        # Very rough delta approximation
        if right == 'C':
            delta = 0.5 + moneyness * 2
        else:
            delta = -0.5 + moneyness * 2

        # Time decay adjustment
        delta *= (1 - 0.1 * (dte / 365))

        return max(-1.0, min(1.0, delta))

    def _calculate_synthetic_price(self, spot: float, strike: float, right: str, dte: int) -> float:
        """Calculate synthetic option price."""
        intrinsic = max(0, spot - strike) if right == 'C' else max(0, strike - spot)
        time_value = spot * 0.02 * (dte / 30) ** 0.5
        return intrinsic + time_value

    def _load_stub_data(self, underlying: str) -> List[OptionContract]:
        """Load pre-canned data from JSON file."""
        if not self.stub_data_path or not Path(self.stub_data_path).exists():
            return []

        try:
            with open(self.stub_data_path, 'r') as f:
                data = json.load(f)
                if underlying not in data:
                    return []

                contracts = []
                for item in data[underlying]:
                    contract = OptionContract(**item)
                    contracts.append(contract)
                return contracts
        except Exception:
            return []

    def _filter_contracts(
        self,
        contracts: List[OptionContract],
        dte_candidates: List[int],
        right: Optional[str],
    ) -> List[OptionContract]:
        """Filter contracts by DTE and right."""
        now = datetime.now()
        filtered = []

        for contract in contracts:
            # Filter by right
            if right and contract.right != right:
                continue

            # Calculate DTE
            expiry = datetime.strptime(contract.expiry_date, '%Y-%m-%d')
            dte = (expiry - now).days

            # Check if within any DTE candidate range (±2 days tolerance)
            for target_dte in dte_candidates:
                if abs(dte - target_dte) <= 2:
                    filtered.append(contract)
                    break

        return filtered

    async def get_underlying_price(self, symbol: str) -> Optional[float]:
        """Return synthetic underlying price."""
        # Hardcoded prices for common symbols
        prices = {
            'SPY': 450.0,
            'QQQ': 380.0,
            'IWM': 195.0,
            'DIA': 350.0,
        }
        return prices.get(symbol, 100.0)

    async def place_vertical_spread(
        self,
        underlying: str,
        expiry: str,
        short_strike: float,
        long_strike: float,
        qty: int,
        *,
        side: str = "SELL",
        right: str = "P",
        limit_credit: Optional[float] = None,
        account: Optional[str] = None,
        transmit: bool = False,
        oca_group: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Stub placement: returns a synthetic order record."""
        order_id = -int(datetime.now().timestamp() * 1000)
        order_ref = f"stub_vertical_{underlying}_{expiry}_{short_strike}_{long_strike}"
        status = "FILLED" if transmit else "SUBMITTED"
        return {
            "status": status,
            "order_id": order_id,
            "order_ref": order_ref,
            "underlying": underlying,
            "expiry": expiry,
            "short_strike": short_strike,
            "long_strike": long_strike,
            "qty": qty,
            "side": side,
            "right": right,
            "limit_credit": limit_credit,
            "account": account,
            "transmit": transmit,
            "oca_group": oca_group,
        }


class IBKROptionsDataProvider(OptionsDataProvider):
    """IBKR-backed options data provider with real option chain data."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 4002,  # Paper trading port
        client_id: Optional[int] = None,
        allow_direct_fallback: Optional[bool] = None,
    ):
        """Initialize IBKR provider.

        Args:
            host: IBKR Gateway/TWS host
            port: Port (4002 for paper, 4001 for live)
            client_id: Unique client ID (auto-generated if None)
        """
        self.host = host
        self.port = port
        # If client_id is not supplied, prefer the shared market IB session (2-connection model).
        self.client_id = client_id if client_id is not None else int(get_client_id_registry().resolve("options_quotes"))
        if allow_direct_fallback:
            self._log = get_logger(__name__)
            self._log.warning(
                "ibkr_options_direct_fallback_ignored",
                host=host,
                port=port,
                client_id=self.client_id,
            )
        self.ib = None
        self._connected = False
        self._conn_manager = None
        self._market_handle_epoch: Optional[int] = None
        # Lightweight cache for qualified option contracts to avoid repeated
        # SecDef/qualification on every strategy tick.
        # Key: (underlying, dte_key, right_key) -> (expires_at_ts, contracts_to_fetch, qualified_contracts)
        self._chain_cache: Dict[Tuple[str, str, str], Tuple[float, List[Tuple[Any, str, int]], List[Any]]] = {}
        self._chain_cache_ttl_sec: int = 300  # 5 minutes
        # Short-term price cache to avoid hammering IBKR when greeks/prices are missing.
        # Key: same as _chain_cache -> (expires_at_ts, results)
        self._chain_price_cache: Dict[Tuple[str, str, str], Tuple[float, List[OptionContract]]] = {}
        self._chain_failure_backoff_until: Dict[Tuple[str, str, str], float] = {}
        self._chain_price_cache_ttl_sec: int = int(os.getenv("OPTION_CHAIN_REFRESH_SEC", "30"))  # min refresh window
        self._chain_empty_backoff_sec: int = int(os.getenv("OPTION_CHAIN_EMPTY_BACKOFF_SEC", "120"))  # backoff when empty/NaN
        # Throttle reqMktData to stay under IBKR pacing limits (0 disables).
        self._req_mktdata_spacing_sec: float = float(os.getenv("IBKR_MKTDATA_SPACING_SEC", "0.02"))
        self._session_revalidate_cooldown_sec: float = float(os.getenv("IBKR_SESSION_REVALIDATE_COOLDOWN_SEC", "15"))
        self._last_session_revalidate_ts: float = 0.0
        self._rejected_contract_cooldown_sec: float = float(os.getenv("IBKR_REJECTED_CONTRACT_COOLDOWN_SEC", "300"))
        self._rejected_contracts: Dict[str, float] = {}
        self._rejected_contract_warn_minute: Dict[str, str] = {}
        # Per-chain lock to serialize expensive refreshes when multiple callers ask at once.
        self._chain_locks: Dict[Tuple[str, str, str], "asyncio.Lock"] = {}
        self._chain_watchdog: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        # Always compute Greeks locally to avoid IBKR "API-specific option analytics" dependencies.
        # (IBKR modelGreeks may be NaN without additional subscriptions; we treat it as non-authoritative.)
        self.use_ibkr_model_greeks = False
        self.risk_free_rate = 0.0
        self.dividend_yield = 0.0
        self._log = getattr(self, "_log", get_logger(__name__))

    @staticmethod
    def _chain_cache_key(
        underlying: str,
        dte_candidates: List[int],
        right: Optional[str],
    ) -> Tuple[str, str, str]:
        dte_key = ",".join(str(int(x)) for x in sorted(set(dte_candidates or []))) or "any"
        right_key = right if right in ("C", "P") else "BOTH"
        return (underlying, dte_key, right_key)

    def _backoff_option_chain_failure(
        self,
        cache_key: Tuple[str, str, str],
        *,
        reason: str,
        now_ts: Optional[float] = None,
    ) -> None:
        now = float(now_ts if now_ts is not None else time.time())
        self._chain_failure_backoff_until[cache_key] = now + max(1.0, float(self._chain_empty_backoff_sec))
        self._chain_price_cache[cache_key] = (
            now + max(1.0, float(self._chain_empty_backoff_sec)),
            [],
        )
        self._log.warning(
            "ibkr_option_chain_failure_backoff",
            underlying=cache_key[0],
            dte_key=cache_key[1],
            right_key=cache_key[2],
            reason=reason,
            backoff_sec=self._chain_empty_backoff_sec,
        )

    @staticmethod
    def _is_adjusted_contract(contract: Any) -> bool:
        local = str(getattr(contract, "localSymbol", "") or "")
        trading_class = str(getattr(contract, "tradingClass", "") or "")
        return local.startswith("2") or trading_class.startswith("2")

    @staticmethod
    def _contract_reject_key(contract: Any) -> str:
        sec_type = str(getattr(contract, "secType", "") or "")
        symbol = str(getattr(contract, "symbol", "") or "")
        local_symbol = str(getattr(contract, "localSymbol", "") or "")
        expiry = str(getattr(contract, "lastTradeDateOrContractMonth", "") or "")
        strike = str(getattr(contract, "strike", "") or "")
        right = str(getattr(contract, "right", "") or "")
        exchange = str(getattr(contract, "exchange", "") or "")
        trading_class = str(getattr(contract, "tradingClass", "") or "")
        return "|".join([sec_type, symbol, local_symbol, expiry, strike, right, exchange, trading_class])

    def _contract_in_reject_cooldown(self, contract: Any, *, now_ts: Optional[float] = None) -> bool:
        now_ts = float(now_ts if now_ts is not None else time.time())
        key = self._contract_reject_key(contract)
        expires_at = float(self._rejected_contracts.get(key) or 0.0)
        if expires_at <= 0.0:
            return False
        if expires_at <= now_ts:
            self._rejected_contracts.pop(key, None)
            self._rejected_contract_warn_minute.pop(key, None)
            return False
        minute_key = datetime.fromtimestamp(now_ts, ET).strftime("%Y-%m-%d %H:%M")
        if self._rejected_contract_warn_minute.get(key) != minute_key:
            self._rejected_contract_warn_minute[key] = minute_key
            self._log.warning(
                "ibkr_option_contract_reject_cooldown_skip",
                underlying=getattr(contract, "symbol", None),
                local_symbol=getattr(contract, "localSymbol", None),
                expiry=getattr(contract, "lastTradeDateOrContractMonth", None),
                strike=getattr(contract, "strike", None),
                right=getattr(contract, "right", None),
                cooldown_remaining_sec=round(expires_at - now_ts, 1),
            )
        return True

    def _mark_contract_rejected(self, contract: Any, *, now_ts: Optional[float] = None) -> None:
        now_ts = float(now_ts if now_ts is not None else time.time())
        key = self._contract_reject_key(contract)
        self._rejected_contracts[key] = now_ts + max(1.0, self._rejected_contract_cooldown_sec)

    async def _req_mktdata_batch(self, contracts: List[Any], wait_sec: float = 0.8) -> List[Any]:
        """Request streaming market data for a batch and cancel quickly.

        We intentionally avoid snapshot requests (reqTickers/reqTickersAsync) because those can
        trigger IBKR "API-specific market data package" prompts.
        """
        tickers: List[Any] = []
        if not self.ib or not contracts:
            return tickers

        active_subscriptions: List[Tuple[Any, Any]] = []
        for c in contracts:
            if self._contract_in_reject_cooldown(c):
                tickers.append(None)
                continue
            try:
                tk = self.ib.reqMktData(c, genericTickList="", snapshot=False, regulatorySnapshot=False)
                if str(os.getenv("IB_LOG_TICKER_ID", "")).lower() in ("1", "true", "yes", "on"):
                    try:
                        sym = getattr(c, "symbol", None)
                        local = getattr(c, "localSymbol", None)
                        logger = get_logger(__name__)
                        logger.info(
                            "IB option ticker subscribed",
                            symbol=sym,
                            local_symbol=local,
                            ticker_id=getattr(tk, "tickerId", None),
                            sec_type=getattr(c, "secType", None),
                            exchange=getattr(c, "exchange", None),
                        )
                    except Exception:
                        pass
                tickers.append(tk)
                active_subscriptions.append((c, tk))
            except Exception:
                tickers.append(None)
            if self._req_mktdata_spacing_sec > 0:
                await asyncio.sleep(self._req_mktdata_spacing_sec)

        # Give IB a moment to populate bid/ask/last (delayed data can be slower).
        try:
            deadline = time.time() + float(wait_sec)
            while time.time() < deadline:
                ready = 0
                for tk in tickers:
                    if not tk:
                        continue
                    bid = getattr(tk, "bid", None)
                    ask = getattr(tk, "ask", None)
                    last = getattr(tk, "last", None)
                    if (bid and bid > 0 and ask and ask > 0) or (last and last > 0):
                        ready += 1
                if ready >= max(1, len([t for t in tickers if t])):
                    break
                await asyncio.sleep(0.1)
        except Exception:
            pass
        finally:
            for c, tk in active_subscriptions:
                try:
                    ticker_id = getattr(tk, "tickerId", None)
                    if self._conn_manager and self._conn_manager.was_market_req_rejected(ticker_id, error_codes=(354,)):
                        self._mark_contract_rejected(c)
                        continue
                    self.ib.cancelMktData(c)
                except Exception:
                    continue

        return tickers

    def _update_chain_watchdog(self, cache_key: Tuple[str, str, str], *, result_count: int) -> None:
        now_ts = time.time()
        state = self._chain_watchdog.setdefault(
            cache_key,
            {"last_success_ts": 0.0, "empty_streak": 0, "last_warn_minute": None},
        )
        if result_count > 0:
            state["last_success_ts"] = now_ts
            state["empty_streak"] = 0
            return

        state["empty_streak"] = int(state.get("empty_streak", 0)) + 1
        last_success_ts = float(state.get("last_success_ts") or 0.0)
        stale_age_sec = (now_ts - last_success_ts) if last_success_ts > 0 else None
        should_warn = state["empty_streak"] >= 2 or (stale_age_sec is not None and stale_age_sec >= 180.0)
        if not should_warn:
            return

        minute_key = datetime.fromtimestamp(now_ts, ET).strftime("%Y-%m-%d %H:%M")
        if state.get("last_warn_minute") == minute_key:
            return
        state["last_warn_minute"] = minute_key
        self._chain_price_cache.pop(cache_key, None)
        if state["empty_streak"] >= 3:
            self._chain_cache.pop(cache_key, None)
        self._log.warning(
            "ibkr_option_chain_watchdog",
            underlying=cache_key[0],
            dte_key=cache_key[1],
            right_key=cache_key[2],
            empty_streak=int(state["empty_streak"]),
            stale_age_sec=round(float(stale_age_sec), 2) if stale_age_sec is not None else None,
        )

    async def _validate_market_session_if_needed(self, symbol: str) -> None:
        if not self._conn_manager or not self._conn_manager.market_revalidation_required():
            return
        now_ts = time.time()
        if now_ts - self._last_session_revalidate_ts < self._session_revalidate_cooldown_sec:
            return
        self._last_session_revalidate_ts = now_ts
        try:
            from ib_insync import Stock

            stock = Stock(symbol, "SMART", "USD")
            await self.ib.qualifyContractsAsync(stock)
            tickers = await self._req_mktdata_batch([stock], wait_sec=0.4)
            ticker = tickers[0] if tickers else None
            has_price = bool(
                ticker and (
                    (
                        getattr(ticker, "bid", None)
                        and getattr(ticker, "ask", None)
                        and getattr(ticker, "bid", 0) > 0
                        and getattr(ticker, "ask", 0) > 0
                    )
                    or (getattr(ticker, "last", None) and getattr(ticker, "last", 0) > 0)
                )
            )
            self._conn_manager.mark_market_revalidated(
                source="options_data",
                ok=has_price,
                detail=f"{symbol}_L1_OK" if has_price else f"{symbol}_L1_MISSING",
            )
        except Exception as exc:
            self._conn_manager.mark_market_revalidated(
                source="options_data",
                ok=False,
                detail=f"{symbol}_L1_EXCEPTION:{exc}",
            )

    def _compute_local_greeks(
        self,
        *,
        spot: float,
        strike: float,
        expiry_date: str,
        right: str,
        option_price: float,
    ) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float], Optional[float]]:
        if option_price <= 0 or spot <= 0 or strike <= 0:
            return None, None, None, None, None
        try:
            expiry_dt = datetime.strptime(expiry_date, "%Y-%m-%d").replace(hour=16, minute=0, second=0, tzinfo=ET)
            now = datetime.now(ET)
            seconds = (expiry_dt - now).total_seconds()
            if seconds <= 0:
                return None, None, None, None, None
            t_years = seconds / 31557600.0
        except Exception:
            return None, None, None, None, None

        right_u = right.upper()
        if right_u not in {"C", "P"}:
            return None, None, None, None, None

        iv = _implied_vol_bisect(
            option_price,
            spot,
            strike,
            t_years,
            self.risk_free_rate,
            self.dividend_yield,
            right_u,
        )
        if iv is None:
            return None, None, None, None, None
        delta, gamma, vega, theta = _bs_greeks(
            spot,
            strike,
            t_years,
            self.risk_free_rate,
            self.dividend_yield,
            iv,
            right_u,
        )
        return iv, delta, gamma, vega, theta

    async def _ensure_connected(self):
        """Ensure IBKR connection is active."""
        if self._connected and self.ib and self.ib.isConnected():
            if self._conn_manager is not None and self._market_handle_epoch is not None:
                if int(self._market_handle_epoch) == int(self._conn_manager.market_connection_epoch()):
                    return
            else:
                return

        try:
            from services.ibkr.ib_connection_manager import get_ib_connection_manager

            self._conn_manager = get_ib_connection_manager()
            handle = await self._conn_manager.get_market_ib_handle(host=self.host, port=self.port)
            self.ib = handle.ib
            self._market_handle_epoch = int(handle.epoch)
            self._connected = True
        except Exception as e:
            raise RuntimeError(f"Failed to connect to IBKR: {e}")

    async def get_chain(
        self,
        underlying: str,
        dte_candidates: List[int],
        right: Optional[str] = None,
    ) -> List[OptionContract]:
        """Fetch option chain from IBKR.

        Args:
            underlying: Underlying symbol (e.g., 'SPY')
            dte_candidates: List of preferred DTEs (e.g., [7, 14, 30])
            right: Optional filter for 'C' or 'P'

        Returns:
            List of OptionContract objects with real market data and Greeks
        """
        await self._ensure_connected()

        try:
            from ib_insync import Stock, Option
            from libs.options.occ import build_occ_symbol

            now_ts = time.time()
            cache_key = self._chain_cache_key(underlying, dte_candidates, right)
            governor_key = ":".join(cache_key)

            if self._conn_manager is not None and hasattr(self._conn_manager, "allow_market_request"):
                decision = self._conn_manager.allow_market_request(
                    kind="option_chain_secdef",
                    key=governor_key,
                    requester="options_data",
                    allow_during_revalidation=True,
                )
                if not getattr(decision, "allowed", False):
                    self._backoff_option_chain_failure(
                        cache_key,
                        reason=f"governor_blocked:{getattr(decision, 'reason', 'unknown')}",
                        now_ts=now_ts,
                    )
                    return []

            if self._conn_manager is not None and self._conn_manager.market_revalidation_required():
                if now_ts - self._last_session_revalidate_ts < self._session_revalidate_cooldown_sec:
                    self._backoff_option_chain_failure(
                        cache_key,
                        reason="market_revalidation_cooling_down",
                        now_ts=now_ts,
                    )
                    return []
                await self._validate_market_session_if_needed(underlying)
                if self._conn_manager.market_revalidation_required():
                    self._backoff_option_chain_failure(
                        cache_key,
                        reason="market_revalidation_required",
                        now_ts=now_ts,
                    )
                    return []

            # Serialize per-chain to prevent concurrent storm.
            lock = self._chain_locks.setdefault(cache_key, asyncio.Lock())
            async with lock:
                # Re-evaluate cache after acquiring lock to avoid duplicate work.
                now_ts = time.time()
                price_cached = self._chain_price_cache.get(cache_key)
                if price_cached and now_ts < price_cached[0]:
                    return list(price_cached[1])

            contracts_to_fetch: List[Tuple[Any, str, int]]
            qualified: List[Any]

            cached = self._chain_cache.get(cache_key)
            underlying_price: Optional[float] = None
            if cached and now_ts < cached[0]:
                # Reuse previously qualified contracts; only refresh tick data.
                _, contracts_to_fetch, qualified = cached
            else:
                # Get underlying contract
                stock = Stock(underlying, 'SMART', 'USD')
                await self.ib.qualifyContractsAsync(stock)
                if int(getattr(stock, "conId", 0) or 0) <= 0:
                    self._backoff_option_chain_failure(
                        cache_key,
                        reason="underlying_conid_missing",
                        now_ts=now_ts,
                    )
                    if self._conn_manager is not None and hasattr(self._conn_manager, "record_market_request_result"):
                        self._conn_manager.record_market_request_result(
                            kind="option_chain_secdef",
                            key=governor_key,
                            ok=False,
                            reason="underlying_conid_missing",
                        )
                    return []

                # Request option chain parameters
                try:
                    chains = await self.ib.reqSecDefOptParamsAsync(
                        underlyingSymbol=underlying,
                        futFopExchange='',
                        underlyingSecType='STK',
                        underlyingConId=stock.conId
                    )
                    if self._conn_manager is not None and hasattr(self._conn_manager, "record_market_request_result"):
                        self._conn_manager.record_market_request_result(
                            kind="option_chain_secdef",
                            key=governor_key,
                            ok=bool(chains),
                            reason=None if chains else "empty_secdef",
                        )
                except Exception:
                    if self._conn_manager is not None and hasattr(self._conn_manager, "record_market_request_result"):
                        self._conn_manager.record_market_request_result(
                            kind="option_chain_secdef",
                            key=governor_key,
                            ok=False,
                            reason="secdef_exception",
                        )
                    raise

                if not chains:
                    self._log.warning("IBKR returned no option chains", underlying=underlying, conId=stock.conId)
                    self._backoff_option_chain_failure(
                        cache_key,
                        reason="empty_secdef",
                        now_ts=now_ts,
                    )
                    return []

                now = datetime.now()
                chain_candidates: List[Tuple[Any, List[Tuple[str, int]]]] = []
                for candidate in chains:
                    candidate_expirations = sorted(candidate.expirations)
                    matches: List[Tuple[str, int]] = []
                    for exp_str in candidate_expirations:
                        try:
                            exp_date = datetime.strptime(exp_str, '%Y%m%d')
                            dte = (exp_date - now).days

                            # Find closest matching DTE from candidates
                            for target_dte in dte_candidates:
                                if abs(dte - target_dte) <= 2:  # ±2 days tolerance
                                    matches.append((exp_str, dte))
                                    break

                            if len(matches) >= 2:  # tighter: limit expirations to reduce downstream load
                                break
                        except Exception:
                            continue
                    chain_candidates.append((candidate, matches))

                def _chain_score(item: Tuple[Any, List[Tuple[str, int]]]) -> Tuple[int, int, int, int, int]:
                    candidate, matches = item
                    trading_class_raw = str(getattr(candidate, "tradingClass", "") or "").upper()
                    multiplier_raw = str(getattr(candidate, "multiplier", "") or "")
                    starts_with_digit = 1 if trading_class_raw[:1].isdigit() else 0
                    return (
                        1 if matches else 0,
                        1 if trading_class_raw == underlying.upper() else 0,
                        1 if multiplier_raw == "100" else 0,
                        0 if starts_with_digit else 1,
                        len(matches),
                    )

                chain, target_expirations = max(chain_candidates, key=_chain_score)
                expirations = sorted(chain.expirations)
                strikes = sorted(chain.strikes)
                trading_class = chain.tradingClass
                multiplier = chain.multiplier

                self._log.info(
                    "Using option chain",
                    underlying=underlying,
                    exchange=chain.exchange,
                    tradingClass=trading_class,
                    multiplier=multiplier,
                    matching_expirations=len(target_expirations),
                    available_exps=len(expirations),
                )

                if not target_expirations:
                    self._log.warning(
                        "No matching expirations found",
                        underlying=underlying,
                        candidates=dte_candidates,
                        available_exps=len(expirations),
                        tradingClass=trading_class,
                    )
                    return []

                # Keep at most two expirations to reduce downstream load.
                target_expirations = target_expirations[:2]
                for exp_str, dte in target_expirations:
                    try:
                        self._log.debug("Selected option expiration", underlying=underlying, expiry=exp_str, dte=dte)
                    except Exception:
                        continue

                # Get underlying price for filtering strikes
                underlying_price = await self.get_underlying_price(underlying)
                if not underlying_price:
                    self._log.warning("Could not get underlying price", underlying=underlying)
                    return []

                # Filter strikes around current price (±15%)
                lower_bound = underlying_price * 0.85
                upper_bound = underlying_price * 1.15
                filtered_strikes = [s for s in strikes if lower_bound <= s <= upper_bound]

                # Coarse strike-step filtering is configurable. Keeping only $5 intervals
                # can starve 0.45-0.55 delta availability and force stale-quote fallback.
                # Default to 1 (no coarse filter) for better live entry freshness.
                high_price_strike_step = float(os.getenv("IBKR_HIGH_PRICE_STRIKE_STEP", "1") or 1)
                if underlying_price > 100 and high_price_strike_step > 1:
                    filtered_strikes = [
                        s
                        for s in filtered_strikes
                        if abs((s / high_price_strike_step) - round(s / high_price_strike_step)) <= 1e-9
                    ]

                # Keep strike selection centered on ATM so both CALL/PUT remain tradable for 0DTE.
                # We still cap the total list to control reqMktData load.
                strike_count = max(4, int(os.getenv("IBKR_OPTION_STRIKE_COUNT", "8") or 8))
                filtered_strikes = sorted(
                    filtered_strikes,
                    key=lambda x: abs(x - underlying_price)
                )[:strike_count]

                # Build option contracts
                contracts_to_fetch = []
                for exp_str, dte in target_expirations:
                    for strike in filtered_strikes[:8]:  # limit strikes per expiration
                        rights = ['C', 'P'] if not right else [right]
                        for r in rights:
                            # Use SMART routing for options.
                            # We explicitly pass tradingClass and multiplier from the chain definition.
                            # Use keyword arguments to avoid positional mismatch (e.g. currency vs multiplier).
                            opt = Option(
                                symbol=underlying,
                                lastTradeDateOrContractMonth=exp_str,
                                strike=strike,
                                right=r,
                                exchange="SMART",
                                currency="USD",
                                multiplier=str(multiplier),
                                tradingClass=trading_class
                            )
                            contracts_to_fetch.append((opt, exp_str, dte))

                # Qualify contracts (validate they exist)
                opt_contracts = [c[0] for c in contracts_to_fetch]
                if not opt_contracts:
                    self._log.warning("No option contracts built to fetch", underlying=underlying)
                    return []
                
                self._log.debug("Qualifying contracts sample", underlying=underlying, sample=opt_contracts[0])
                
                qualified = await self.ib.qualifyContractsAsync(*opt_contracts)
                if qualified:
                    before = len(qualified)
                    qualified = [c for c in qualified if not self._is_adjusted_contract(c)]
                    dropped = before - len(qualified)
                    if dropped:
                        self._log.info(
                            "Dropped adjusted option contracts",
                            dropped=dropped,
                            underlying=underlying,
                        )
                else:
                    self._log.warning("No contracts qualified", underlying=underlying, attempted=len(opt_contracts))

                # Cache the qualified contracts and metadata for a few minutes.
                # We do not cache prices; those are refreshed via tickers below.
                self._chain_cache[cache_key] = (
                    now_ts + self._chain_cache_ttl_sec,
                    contracts_to_fetch,
                    list(qualified),
                )
            if underlying_price is None:
                underlying_price = await self.get_underlying_price(underlying)

            # Request market data and Greeks in batches
            results = []
            batch_size = 20  # Lower to reduce concurrent load

            for i in range(0, len(qualified), batch_size):
                batch = qualified[i:i + batch_size]

                # Request streaming market data briefly (avoid snapshot API-specific packages).
                tickers = await self._req_mktdata_batch(batch)

                for j, ticker_result in enumerate(tickers):
                    if not ticker_result:
                        continue

                    ticker = ticker_result
                    contract = batch[j]

                    # Find corresponding metadata
                    metadata = None
                    for c, exp_str, dte in contracts_to_fetch:
                        if (c.symbol == contract.symbol and
                            c.strike == contract.strike and
                            c.right == contract.right and
                            c.lastTradeDateOrContractMonth == contract.lastTradeDateOrContractMonth):
                            metadata = (exp_str, dte)
                            break

                    if not metadata:
                        continue

                    exp_str, dte = metadata

                    # Extract market data
                    bid = ticker.bid if ticker.bid and ticker.bid > 0 else None
                    ask = ticker.ask if ticker.ask and ticker.ask > 0 else None
                    last = ticker.last if ticker.last and ticker.last > 0 else None

                    # For delayed data, bid/ask may not be available immediately
                    # Accept contracts with either (bid+ask) OR last price
                    if not ((bid and ask) or last):
                        continue  # Skip contracts without any valid pricing

                    # If delayed data only has last price, estimate bid/ask with small spread
                    if last and not (bid and ask):
                        spread = last * 0.02  # 2% spread estimate for delayed data
                        bid = last - spread / 2
                        ask = last + spread / 2

                    # Build OCC symbol + expiry formatting
                    exp_date = datetime.strptime(exp_str, '%Y%m%d').strftime('%Y-%m-%d')

                    # Extract Greeks (prefer local calc; IBKR model Greeks are optional).
                    option_price = None
                    if bid and ask:
                        option_price = (bid + ask) / 2.0
                    elif last:
                        option_price = last

                    delta = None
                    gamma = None
                    vega = None
                    theta = None
                    impl_vol = None
                    if underlying_price and option_price:
                        impl_vol, delta, gamma, vega, theta = self._compute_local_greeks(
                            spot=float(underlying_price),
                            strike=float(contract.strike),
                            expiry_date=exp_date,
                            right=str(contract.right),
                            option_price=float(option_price),
                        )

                    # Build OCC symbol
                    occ_symbol = build_occ_symbol(
                        underlying=contract.symbol,
                        expiry_date=exp_str,
                        right=contract.right,
                        strike=contract.strike,
                    )

                    # Extract open interest (attribute names differ for options vs stocks)
                    oi = None
                    if hasattr(ticker, 'putOpenInterest') and contract.right == 'P':
                        oi = ticker.putOpenInterest
                    elif hasattr(ticker, 'callOpenInterest') and contract.right == 'C':
                        oi = ticker.callOpenInterest

                    option_contract = OptionContract(
                        symbol=occ_symbol,
                        underlying=contract.symbol,
                        expiry_date=exp_date,
                        strike=contract.strike,
                        right=contract.right,
                        bid=bid,
                        ask=ask,
                        last=last,
                        volume=ticker.volume if ticker.volume else None,
                        open_interest=oi,
                        delta=delta,
                        gamma=gamma,
                        vega=vega,
                        theta=theta,
                        implied_vol=impl_vol,
                        last_trade_time=ticker.time if ticker.time else None,
                        bid_size=getattr(ticker, "bidSize", None) or None,
                        ask_size=getattr(ticker, "askSize", None) or None,
                    )

                    results.append(option_contract)

                # Small delay between batches to avoid rate limiting
                if i + batch_size < len(qualified):
                    await asyncio.sleep(1.0)

            self._update_chain_watchdog(cache_key, result_count=len(results))
            # Cache price snapshot for a short window to avoid tight retries.
            ttl = self._chain_price_cache_ttl_sec if results else self._chain_empty_backoff_sec
            self._chain_price_cache[cache_key] = (now_ts + ttl, list(results))

            return results

        except Exception as e:
            # Log error but don't crash - return empty list
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"IBKR option chain fetch failed: {e}", exc_info=True)
            return []

    async def get_underlying_price(self, symbol: str) -> Optional[float]:
        """Get underlying price from IBKR.

        Args:
            symbol: Stock symbol

        Returns:
            Current mid price or None if unavailable
        """
        await self._ensure_connected()

        try:
            from ib_insync import Stock

            stock = Stock(symbol, 'SMART', 'USD')
            await self.ib.qualifyContractsAsync(stock)

            tickers = await self._req_mktdata_batch([stock], wait_sec=0.8)
            if not tickers or not tickers[0]:
                return None
            ticker = tickers[0]

            # Try mid price first
            if ticker.bid and ticker.ask and ticker.bid > 0 and ticker.ask > 0:
                return (ticker.bid + ticker.ask) / 2.0

            # Fallback to last price
            if ticker.last and ticker.last > 0:
                return ticker.last

            return None

        except Exception:
            return None

    async def place_vertical_spread(
        self,
        underlying: str,
        expiry: str,
        short_strike: float,
        long_strike: float,
        qty: int,
        *,
        side: str = "SELL",
        right: str = "P",
        limit_credit: Optional[float] = None,
        account: Optional[str] = None,
        transmit: bool = False,
        oca_group: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Prepare or place a vertical spread.

        Safety defaults:
          - transmit=False by default; caller must set transmit=True explicitly to send.
          - If limit_credit is None, returns a prepared payload without submitting.

        Live submission from this market-data service is blocked by default. Production
        order flow must route through UnifiedOrderManager, not direct broker adapter calls.
        """
        if limit_credit is None:
            return {"status": "prepared_no_limit", "order_id": None, "side": side.upper()}
        if transmit:
            return {
                "status": "blocked",
                "order_id": None,
                "side": side.upper(),
                "underlying": underlying,
                "expiry": expiry,
                "short_strike": short_strike,
                "long_strike": long_strike,
                "qty": qty,
                "right": right,
                "limit_credit": limit_credit,
                "account": account,
                "transmit": transmit,
                "oca_group": oca_group,
                "reason": "direct_vertical_spread_execution_disabled_route_through_uom",
            }
        return {
            "status": "prepared_no_transmit",
            "order_id": None,
            "side": side.upper(),
            "underlying": underlying,
            "expiry": expiry,
            "short_strike": short_strike,
            "long_strike": long_strike,
            "qty": qty,
            "right": right,
            "limit_credit": limit_credit,
            "account": account,
            "transmit": transmit,
            "oca_group": oca_group,
        }

    async def disconnect(self):
        """Detach provider state from the shared IB session without closing it."""
        self._connected = False
        self.ib = None
        self._conn_manager = None
        self._market_handle_epoch = None

    async def get_order_status(self, order_id: int) -> Optional[Dict[str, Any]]:
        """Query order status from IBKR using unified adapter.

        Args:
            order_id: The IBKR order ID to query

        Returns:
            Dict with keys: status, filled_qty, avg_fill_price, remaining
            None if order not found

        PHASE 2 CHANGE: order-status reads use the shared IB connection manager.
        """
        try:
            from services.ibkr.ib_connection_manager import get_ib_connection_manager

            ib_cfg = {}
            try:
                from libs.config import config as CFG

                ib_cfg = CFG.ib_config
            except Exception:
                ib_cfg = {}
            adapter = await get_ib_connection_manager().get_exec_adapter(
                host=ib_cfg.get("host", "127.0.0.1"),
                port=int(ib_cfg.get("port", 4002)),
                account=None,
            )

            # Query all trades to find the one matching this order_id
            trades = adapter.ib.trades()

            for trade in trades:
                if trade.order.orderId == order_id:
                    order_status = trade.orderStatus

                    return {
                        "order_id": order_id,
                        "status": order_status.status,  # Submitted/Filled/Cancelled/Inactive/etc
                        "filled_qty": order_status.filled,
                        "remaining_qty": order_status.remaining,
                        "avg_fill_price": order_status.avgFillPrice,
                        "perm_id": trade.order.permId if hasattr(trade.order, "permId") else None,
                    }

            # Order not found in trades
            return None

        except Exception as exc:
            from libs.logger import get_logger
            logger = get_logger(__name__)
            logger.error("get_order_status_failed", order_id=order_id, error=str(exc))
            return None


class ClickHouseOptionsDataProvider(OptionsDataProvider):
    """ClickHouse-backed provider for historical/cached option data."""

    def __init__(self):
        """Initialize ClickHouse provider."""
        try:
            from libs.db.clickhouse import ClickHouseDB
            self.ch = ClickHouseDB()
        except Exception:
            self.ch = None

    async def get_chain(
        self,
        underlying: str,
        dte_candidates: List[int],
        right: Optional[str] = None,
    ) -> List[OptionContract]:
        """Fetch option chain from ClickHouse cache.
        """
        if not self.ch:
            return []

        try:
            from libs.options.occ import build_occ_symbol
        except Exception:
            build_occ_symbol = None

        right_filter = ""
        if right in ("C", "P"):
            right_filter = "AND right = {right:String}"

        dte_filter = ""
        if dte_candidates:
            dte_list = ",".join(str(int(d)) for d in dte_candidates)
            dte_filter = f"AND dte IN ({dte_list})"

        sql = f"""
        SELECT
            underlying,
            expiry,
            strike,
            right,
            dte,
            argMax(bid, ts) AS bid,
            argMax(ask, ts) AS ask,
            argMax(last, ts) AS last,
            argMax(volume, ts) AS volume,
            argMax(open_interest, ts) AS open_interest,
            argMax(delta, ts) AS delta,
            argMax(gamma, ts) AS gamma,
            argMax(vega, ts) AS vega,
            argMax(theta, ts) AS theta,
            argMax(iv, ts) AS iv,
            max(ts) AS last_ts
        FROM trading.options_quotes_1m
        WHERE underlying = {{underlying:String}}
          {dte_filter}
          {right_filter}
          AND ts >= now() - INTERVAL 2 DAY
        GROUP BY underlying, expiry, strike, right, dte
        """

        params = {"underlying": underlying}
        if right in ("C", "P"):
            params["right"] = right

        rows = await asyncio.to_thread(threadsafe_query, self.ch, sql, params)
        contracts: List[OptionContract] = []
        for r in rows:
            right_val = r.get("right")
            right_str = right_val if isinstance(right_val, str) else ("C" if int(right_val) == 1 else "P")
            expiry_date = r.get("expiry")
            expiry_str = expiry_date.isoformat() if hasattr(expiry_date, "isoformat") else str(expiry_date)

            occ_symbol = None
            if build_occ_symbol:
                try:
                    occ_symbol = build_occ_symbol(underlying, expiry_str, right_str, float(r["strike"]))
                except Exception:
                    occ_symbol = None

            contracts.append(
                OptionContract(
                    symbol=occ_symbol or f"{underlying}_{expiry_str}_{right_str}_{r['strike']}",
                    underlying=underlying,
                    expiry_date=expiry_str,
                    strike=float(r["strike"]),
                    right=right_str,
                    bid=float(r.get("bid") or 0.0),
                    ask=float(r.get("ask") or 0.0),
                    last=float(r.get("last") or 0.0) if r.get("last") is not None else None,
                    volume=int(r.get("volume") or 0),
                    open_interest=int(r.get("open_interest") or 0),
                    delta=float(r.get("delta") or 0.0) if r.get("delta") is not None else None,
                    gamma=float(r.get("gamma") or 0.0) if r.get("gamma") is not None else None,
                    vega=float(r.get("vega") or 0.0) if r.get("vega") is not None else None,
                    theta=float(r.get("theta") or 0.0) if r.get("theta") is not None else None,
                    implied_vol=float(r.get("iv") or 0.0) if r.get("iv") is not None else None,
                    last_trade_time=r.get("last_ts"),
                )
            )

        return contracts

    async def get_underlying_price(self, symbol: str) -> Optional[float]:
        """Get underlying price from ClickHouse."""
        if not self.ch:
            return None

        try:
            rows = await asyncio.to_thread(
                threadsafe_query,
                self.ch,
                "SELECT anyLast((bid+ask)/2) AS mid FROM trading.bbo "
                "WHERE symbol={s:String} AND ts > now() - INTERVAL 5 MINUTE",
                {"s": symbol},
            )
            if rows and rows[0].get("mid"):
                return float(rows[0]["mid"])
        except Exception:
            pass

        return None


def create_options_provider(backend: str = 'stub', **kwargs) -> OptionsDataProvider:
    """Factory function to create options data provider.

    Args:
        backend: One of 'stub', 'ibkr', 'clickhouse'
        **kwargs: Additional arguments for the provider
            For 'ibkr': host, port, client_id
            For 'stub': stub_data_path

    Returns:
        OptionsDataProvider instance

    Examples:
        >>> # Stub provider (default for testing)
        >>> provider = create_options_provider('stub')

        >>> # IBKR provider for paper trading
        >>> provider = create_options_provider('ibkr', host='127.0.0.1', port=4002)

        >>> # IBKR provider for live trading
        >>> provider = create_options_provider('ibkr', host='127.0.0.1', port=4001)
    """
    if backend == 'stub':
        return StubOptionsDataProvider(**kwargs)
    elif backend == 'ibkr':
        return IBKROptionsDataProvider(**kwargs)
    elif backend == 'clickhouse':
        return ClickHouseOptionsDataProvider()
    else:
        raise ValueError(f"Unknown backend: {backend}")
