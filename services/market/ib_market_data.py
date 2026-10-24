"""
IB Market Data Subscription Adapter
Based on doc/36.md and Phase 2 requirements

Subscribes to real-time market data from IBKR and feeds the aggregator
"""
import asyncio
import os
import time
from typing import List, Callable, Optional, Dict, Tuple, Any, Iterable
from dataclasses import dataclass
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo

from ib_insync import IB, Stock, Index, Ticker, util as ibutil

from libs.logger import get_logger
from libs.config import config
from services.ibkr.client_id_registry import get_client_id_registry

logger = get_logger(__name__)


def _market_data_type_for_now(now: Optional[datetime] = None) -> int:
    ts = (now or datetime.now(ET)).astimezone(ET)
    is_rth = dt_time(9, 30) <= ts.time() < dt_time(16, 0)
    env_key = "IB_MARKET_DATA_TYPE_RTH" if is_rth else "IB_MARKET_DATA_TYPE_OFFHOURS"
    default_value = "1" if is_rth else "4"
    try:
        return int(os.getenv(env_key, default_value))
    except Exception:
        return int(default_value)


def _market_data_type_mode(market_data_type: int) -> str:
    return {
        1: "live",
        2: "frozen",
        3: "delayed",
        4: "delayed-frozen",
    }.get(int(market_data_type), f"type-{market_data_type}")


def _try_set_delayed_frozen_market_data(ib: IB) -> None:
    """Best-effort guard for shared IB sessions.

    When the adapter receives an already-connected shared IB instance, `_connect()`
    returns early and would otherwise skip the historical market-data-type call.
    Re-applying it here keeps later `reqMktData` calls on the intended type for
    the current ET session.
    """
    market_data_type = _market_data_type_for_now()
    try:
        ib.reqMarketDataType(market_data_type)
        logger.info(
            "Market data type set",
            market_data_type=market_data_type,
            mode=_market_data_type_mode(market_data_type),
        )
    except Exception as md_err:
        logger.warning("Failed to set market data type", market_data_type=market_data_type, error=str(md_err))
ET = ZoneInfo("America/New_York")


@dataclass
class TickData:
    """Normalized tick data"""
    symbol: str
    ts_us: int  # Microsecond timestamp
    last: Optional[float] = None
    last_size: Optional[float] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    bid_size: Optional[float] = None
    ask_size: Optional[float] = None

    @classmethod
    def from_ib_ticker(cls, ticker: Ticker, ts_us: int, symbol_override: Optional[str] = None) -> 'TickData':
        """Create from IB ticker"""
        # Prefer last; fallback to close; fallback to marketPrice()
        last_val = None
        try:
            if ticker.last and ticker.last > 0:
                last_val = float(ticker.last)
            elif ticker.close and ticker.close > 0:
                last_val = float(ticker.close)
            else:
                mp = ticker.marketPrice()
                if mp and mp > 0:
                    last_val = float(mp)
        except Exception:
            pass

        return cls(
            symbol=symbol_override or ticker.contract.symbol,
            ts_us=ts_us,
            last=last_val,
            last_size=float(ticker.lastSize) if ticker.lastSize and ticker.lastSize > 0 else None,
            bid=float(ticker.bid) if ticker.bid and ticker.bid > 0 else None,
            ask=float(ticker.ask) if ticker.ask and ticker.ask > 0 else None,
            bid_size=float(ticker.bidSize) if ticker.bidSize and ticker.bidSize > 0 else None,
            ask_size=float(ticker.askSize) if ticker.askSize and ticker.askSize > 0 else None
        )


class IBMarketDataAdapter:
    """
    IB Market Data Subscription Adapter

    Subscribes to real-time market data and delivers ticks to callbacks.
    Features:
    - Automatic reconnection
    - Health monitoring
    - Subscription management
    - Tick normalization
    """

    def __init__(
        self,
        symbols: List[str],
        tick_callback: Optional[Callable[[TickData], None]] = None,
        host: Optional[str] = None,
        port: Optional[int] = None,
        client_id: Optional[int] = None,
        ib: Optional[IB] = None,
        use_shared_manager: bool = False,
        rth_only: bool = False,
        trading_hours: Optional[Tuple[float, float]] = None,
        allowed_sessions: Optional[Iterable[str]] = None,
    ):
        """
        Initialize adapter

        Args:
            symbols: List of symbols to subscribe
            tick_callback: Callback for tick data
            host: IB host
            port: IB port
            client_id: IB client ID
            rth_only: If True, drop ticks outside configured regular trading hours
            trading_hours: Decimal hour tuple (open, close) for RTH gating
            allowed_sessions: Allowed ET session phases when rth_only is False
        """
        self.symbols = symbols
        self.tick_callback = tick_callback
        self.rth_only = bool(rth_only)
        self.rth_window = trading_hours or (9.5, 16.0)
        self.allowed_sessions = {
            str(s).strip().upper() for s in (allowed_sessions or ("PREMARKET", "RTH", "AFTERHOURS", "OVERNIGHT"))
            if str(s).strip()
        }

        # IB connection params
        ib_cfg = config.ib_config
        self.ib_host = host or ib_cfg['host']
        self.ib_port = port or ib_cfg['port']
        self.ib_client_id = client_id if client_id is not None else int(get_client_id_registry().resolve("market"))

        # IB connection
        self.ib = ib or IB()
        self._owns_ib = ib is None and not bool(use_shared_manager)
        if ib is not None:
            try:
                existing_id = getattr(getattr(self.ib, "client", None), "clientId", None)
                if existing_id:
                    self.ib_client_id = int(existing_id)
            except Exception:
                pass

        # Subscription state
        self.contracts: Dict[str, Any] = {}
        self.tickers: Dict[str, Ticker] = {}
        # Map ticker object id -> requested symbol (for internals/index aliasing)
        self._ticker_id_to_symbol: Dict[int, str] = {}

        # Concurrency protection for state modifications
        self._state_lock = asyncio.Lock()

        self._running = False
        self._reconnect_task = None
        self._health_task: Optional[asyncio.Task] = None
        self._last_vix_log_ts: Dict[str, float] = {}
        self._deferred_symbols: set[str] = set()
        self._deferred_last_try_ts: float = 0.0
        self._symbol_subscription_ts: Dict[str, float] = {}
        self._symbol_last_update_ts: Dict[str, float] = {}
        self._stale_resubscribe_last_try_ts: Dict[str, float] = {}
        self._stale_resubscribe_sec: float = float(os.getenv("IB_SYMBOL_STALE_RESUBSCRIBE_SEC", "90"))
        self._stale_resubscribe_cooldown_sec: float = float(
            os.getenv("IB_SYMBOL_STALE_RESUBSCRIBE_COOLDOWN_SEC", "60")
        )
        self._connect_failures: int = 0
        self._premarket_port_fallback_failures: int = int(os.getenv("IB_PREMARKET_PORT_FALLBACK_FAILS", "3"))
        self._enable_port_fallback_scan: bool = os.getenv("IB_MARKET_ENABLE_PORT_FALLBACK_SCAN", "0").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self._index_like_rth_only = bool(
            config.get("ibkr.index_like_rth_only", config.get("ib.index_like_rth_only", True))
        )
        symbols_cfg = config.get("ibkr.index_like_symbols", config.get("ib.index_like_symbols", [])) or []
        symbols_default = {"AD-NASD", "ADD-NASD", "TRIN", "TICK-NYSE", "TICK"}
        self._index_like_symbols = {str(s).strip().upper() for s in symbols_cfg if str(s).strip()} or symbols_default

        logger.info("IBMarketDataAdapter initialized",
                   symbols=symbols,
                   host=self.ib_host,
                   port=self.ib_port,
                   client_id=self.ib_client_id,
                   rth_only=self.rth_only,
                   rth_window=self.rth_window,
                   index_like_rth_only=self._index_like_rth_only)

    async def start(self):
        """Start adapter and subscribe to market data"""
        if self._running:
            logger.info("IBMarketDataAdapter already running")
            return

        self._running = True
        start_started_at = time.time()
        logger.info(
            "ib_market_data_adapter_start_begin",
            host=self.ib_host,
            port=self.ib_port,
            client_id=self.ib_client_id,
            owns_ib=self._owns_ib,
            ib_connected=bool(self.ib and self.ib.isConnected()),
            ib_id=id(self.ib),
            symbols=len(self.symbols),
        )

        try:
            # Connect to IB
            await self._connect()
            logger.info(
                "ib_market_data_adapter_connect_done",
                host=self.ib_host,
                port=self.ib_port,
                client_id=self.ib_client_id,
                owns_ib=self._owns_ib,
                ib_connected=bool(self.ib and self.ib.isConnected()),
                ib_id=id(self.ib),
                latency_ms=round((time.time() - start_started_at) * 1000.0, 1),
            )

            # Subscribe to symbols
            await self._subscribe_all()

            # Start health check
            self._health_task = asyncio.create_task(self._health_monitor(), name="ib_market_data_health")

            logger.info("IBMarketDataAdapter started")
        except Exception:
            self._running = False
            raise

    async def stop(self):
        """Stop adapter"""
        logger.info(
            "ib_market_data_adapter_stop_begin",
            owns_ib=self._owns_ib,
            ib_connected=bool(self.ib and self.ib.isConnected()),
            ib_id=id(self.ib),
            contracts=len(self.contracts),
            tickers=len(self.tickers),
        )
        self._running = False

        if self._health_task:
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning("Health monitor stop error", error=str(e))
            finally:
                self._health_task = None

        if self._reconnect_task:
            self._reconnect_task.cancel()

        if self.ib and self.ib.isConnected():
            # Cancel all subscriptions (use lock to protect state access)
            async with self._state_lock:
                # Iterate over a copy to avoid modification during iteration
                contracts_to_cancel = list(self.contracts.values())

            for contract in contracts_to_cancel:
                try:
                    self.ib.cancelMktData(contract)
                except Exception as e:
                    logger.warning("Failed to cancel market data",
                                 symbol=contract.symbol if hasattr(contract, 'symbol') else 'unknown',
                                 error=str(e))

            if self._owns_ib:
                self.ib.disconnect()

        async with self._state_lock:
            self.contracts.clear()
            self.tickers.clear()
            self._ticker_id_to_symbol.clear()
            self._deferred_symbols.clear()

        logger.info(
            "IBMarketDataAdapter stopped",
            owns_ib=self._owns_ib,
            ib_connected=bool(self.ib and self.ib.isConnected()),
            ib_id=id(self.ib),
        )

    async def _connect(self):
        """Connect to IB with small set of common port fallbacks."""
        if self.ib and self.ib.isConnected():
            _try_set_delayed_frozen_market_data(self.ib)
            logger.info(
                "ib_market_data_adapter_reuse_connected_ib",
                host=self.ib_host,
                port=self.ib_port,
                client_id=self.ib_client_id,
                owns_ib=self._owns_ib,
                ib_id=id(self.ib),
            )
            return
        if not self._owns_ib:
            from services.ibkr.ib_connection_manager import get_ib_connection_manager

            previous_ib_id = id(self.ib)
            logger.info(
                "ib_market_data_adapter_request_shared_ib",
                host=self.ib_host,
                port=self.ib_port,
                client_id=self.ib_client_id,
                previous_ib_id=previous_ib_id,
                previous_connected=bool(self.ib and self.ib.isConnected()),
            )
            manager = get_ib_connection_manager()
            handle_epoch = None
            if hasattr(manager, "get_market_ib_handle"):
                handle = await manager.get_market_ib_handle(host=self.ib_host, port=self.ib_port)
                self.ib = handle.ib
                handle_epoch = getattr(handle, "epoch", None)
            else:
                self.ib = await manager.get_market_ib(host=self.ib_host, port=self.ib_port)
            if not self.ib or not self.ib.isConnected():
                raise ConnectionError("shared_market_ib_not_connected")
            try:
                existing_id = getattr(getattr(self.ib, "client", None), "clientId", None)
                if existing_id:
                    self.ib_client_id = int(existing_id)
            except Exception:
                pass
            _try_set_delayed_frozen_market_data(self.ib)
            logger.info(
                "ib_market_data_adapter_refreshed_shared_ib",
                host=self.ib_host,
                port=self.ib_port,
                client_id=self.ib_client_id,
                previous_ib_id=previous_ib_id,
                new_ib_id=id(self.ib),
                market_epoch=handle_epoch,
                ib_connected=bool(self.ib and self.ib.isConnected()),
            )
            return
        phase = self._session_phase_et()
        ports = self._candidate_ports(session_phase=phase)
        attempt_mode = "sticky" if len(ports) == 1 else "fallback_scan"

        last_err = None
        for p in ports:
            gatekeeper = None
            try:
                logger.info(
                    "ib_market_data_adapter_connect_port_begin",
                    host=self.ib_host,
                    port=int(p),
                    client_id=self.ib_client_id,
                    session_phase=phase,
                    port_attempt_mode=attempt_mode,
                    owns_ib=self._owns_ib,
                    ib_id=id(self.ib),
                )
                try:
                    from services.ibkr.ib_connection_manager import get_ib_connection_manager
                    gatekeeper = get_ib_connection_manager().reconnect_gatekeeper
                    allowed = await gatekeeper.acquire(
                        caller="market_data_adapter",
                        host=self.ib_host,
                        port=int(p),
                    )
                    if not allowed:
                        last_err = ConnectionError("reconnect_gatekeeper_denied:market_data_adapter")
                        continue
                except ConnectionError:
                    raise
                except Exception as gate_exc:
                    logger.warning("market_data_adapter_gatekeeper_unavailable", error=str(gate_exc))
                    raise ConnectionError("market_data_adapter_gatekeeper_unavailable") from gate_exc

                await self.ib.connectAsync(
                    host=self.ib_host,
                    port=p,
                    clientId=self.ib_client_id,
                    timeout=10.0
                )
                self.ib_port = p  # remember the working port
                logger.info(
                    "Connected to IB",
                    host=self.ib_host,
                    port=p,
                    client_id=self.ib_client_id,
                    session_phase=phase,
                    port_attempt_mode=attempt_mode,
                    consecutive_failures=self._connect_failures,
                )
                if self._connect_failures > 0:
                    logger.info(
                        "ib_market_port_recovered",
                        session_phase=phase,
                        recovered_port=p,
                        previous_failures=self._connect_failures,
                    )
                self._connect_failures = 0
                _try_set_delayed_frozen_market_data(self.ib)
                if gatekeeper is not None:
                    gatekeeper.report_success(caller="market_data_adapter")
                return
            except Exception as e:
                last_err = e
                if self._owns_ib:
                    try:
                        self.ib.disconnect()
                    except Exception as disconnect_exc:
                        logger.warning(
                            "ib_market_data_adapter_disconnect_after_failure_failed",
                            host=self.ib_host,
                            port=p,
                            client_id=self.ib_client_id,
                            error=str(disconnect_exc),
                        )
                    self.ib = IB()
                if gatekeeper is not None:
                    err_text = str(e)
                    if self._is_passive_recovery_connect_error(err_text):
                        logger.info(
                            "ib_market_data_adapter_gatekeeper_failure_suppressed",
                            error=err_text,
                            host=self.ib_host,
                            port=p,
                            client_id=self.ib_client_id,
                        )
                    else:
                        gatekeeper.report_failure(caller="market_data_adapter", error=err_text)
                logger.warning(
                    "IB connect failed on port, trying next",
                    host=self.ib_host,
                    port=p,
                    error=str(e),
                    session_phase=phase,
                    port_attempt_mode=attempt_mode,
                )

        # If all attempts failed, bubble up the last error
        self._connect_failures += 1
        logger.error(
            "Failed to connect to IB on all candidate ports",
            host=self.ib_host,
            ports=ports,
            error=str(last_err),
            session_phase=phase,
            port_attempt_mode=attempt_mode,
            consecutive_failures=self._connect_failures,
        )
        raise last_err  # type: ignore

    @staticmethod
    def _is_passive_recovery_connect_error(error_text: str) -> bool:
        msg = str(error_text or "").lower()
        return (
            "reconnect_gatekeeper_denied:" in msg
            or "ib_recovery_short_circuit_active:" in msg
            or "ib_api_handshake_not_ready" in msg
        )

    @staticmethod
    def _session_phase_et(now: Optional[datetime] = None) -> str:
        t = (now or datetime.now(ET)).astimezone(ET).time()
        if dt_time(7, 0) <= t < dt_time(9, 30):
            return "PREMARKET"
        if dt_time(9, 30) <= t < dt_time(16, 0):
            return "RTH"
        if dt_time(16, 0) <= t < dt_time(20, 0):
            return "AFTERHOURS"
        return "OFFHOURS"

    def _candidate_ports(self, *, session_phase: str) -> List[int]:
        primary = int(self.ib_port)
        if not self._enable_port_fallback_scan:
            return [primary]
        if session_phase == "PREMARKET" and self._connect_failures < self._premarket_port_fallback_failures:
            return [primary]

        ports = [primary]
        for alt in (4002, 7497, 7496):
            if alt not in ports:
                ports.append(alt)
        return ports

    async def _subscribe_all(self):
        """Subscribe to all symbols"""
        # Get a copy of symbols list to avoid modification during iteration
        async with self._state_lock:
            symbols_to_subscribe = list(self.symbols)
            self._deferred_symbols.clear()

        for symbol in symbols_to_subscribe:
            try:
                if self._should_defer_symbol(symbol):
                    async with self._state_lock:
                        self._deferred_symbols.add(symbol)
                    logger.info(
                        "Deferring symbol subscription until RTH",
                        symbol=symbol,
                        index_like_rth_only=self._index_like_rth_only,
                    )
                    continue
                await self._subscribe_symbol(symbol)
            except Exception as e:
                logger.error("Failed to subscribe",
                           symbol=symbol,
                           error=str(e))

    def _is_index_like_symbol(self, symbol: str) -> bool:
        s = str(symbol).strip().upper()
        if s.startswith("^"):
            return True
        if s in self._index_like_symbols:
            return True

        # Respect explicit index overrides from config.
        overrides = (
            config.get("ibkr.contract_overrides", {})
            or config.get("ib.contract_overrides", {})
            or {}
        )
        if isinstance(overrides, dict):
            o = overrides.get(s) or {}
            ctype = str(o.get("type", "") or "").lower()
            if ctype == "index":
                return True
        return False

    def _should_defer_symbol(self, symbol: str) -> bool:
        if not self._index_like_rth_only:
            return False
        if not self._is_index_like_symbol(symbol):
            return False
        return not self._is_rth()

    async def _try_subscribe_deferred(self) -> None:
        """Try subscribing deferred symbols once RTH is open."""
        if not self._deferred_symbols:
            return
        if not self._is_rth():
            return

        import time
        now = time.time()
        if now - self._deferred_last_try_ts < 60.0:
            return
        self._deferred_last_try_ts = now

        pending = list(self._deferred_symbols)
        for symbol in pending:
            try:
                await self._subscribe_symbol(symbol)
                async with self._state_lock:
                    self._deferred_symbols.discard(symbol)
                logger.info("Deferred symbol subscription succeeded", symbol=symbol)
            except Exception as e:
                logger.warning("Deferred symbol subscription failed", symbol=symbol, error=str(e))

    async def _resolve_contract(self, symbol: str):
        """Resolve IB contract for a given symbol, handling special cases like VIX."""
        symbol = str(symbol).strip().upper()
        if symbol == "VIX":
            for ex in ("CBOE", "CBOEIND", "SMART"):
                logger.info("Resolving VIX contract attempt", exchange=ex)
                candidate = Index("VIX", ex, "USD")
                try:
                    details = await self.ib.reqContractDetailsAsync(candidate)
                except Exception as exc:
                    logger.warning("Contract details lookup failed", symbol=symbol, exchange=ex, error=str(exc))
                    continue
                if details:
                    contract = details[0].contract
                    logger.info("Resolved VIX contract", exchange=ex, con_id=contract.conId)
                    return contract
            logger.warning("Unable to resolve VIX contract on preferred exchanges, falling back to VIXY ETF")
            fallback = Stock("VIXY", "ARCA", "USD")
            details = await self.ib.reqContractDetailsAsync(fallback)
            if details:
                return details[0].contract
            logger.error("Unable to resolve VIXY fallback contract")
            raise RuntimeError("Unable to resolve VIX contract (check market data subscriptions)")

        # Contract overrides (optional; allows pinning non-stock instruments)
        # Example config:
        #   ibkr:
        #     contract_overrides:
        #       AD-NASD: {type: index, exchange: NASDAQ, currency: USD, symbol: AD-NASD}
        overrides = (
            config.get("ibkr.contract_overrides", {})
            or config.get("ib.contract_overrides", {})
            or {}
        )
        if isinstance(overrides, dict) and symbol in overrides:
            o = overrides.get(symbol) or {}
            ctype = str(o.get("type", "") or "").lower()
            ex = str(o.get("exchange", "SMART") or "SMART")
            cur = str(o.get("currency", "USD") or "USD")
            sym = str(o.get("symbol", symbol) or symbol)
            if ctype == "index":
                return Index(sym, ex, cur)
            if ctype == "stock":
                return Stock(sym, ex, cur)

        # Heuristic: index / market internals (often contains "-": AD-NASD, TRIN, etc.)
        if "-" in symbol or symbol.startswith("^"):
            try:
                return await self._resolve_index_like_symbol(symbol)
            except Exception as exc:
                logger.warning("Index-like resolve failed; falling back to Stock", symbol=symbol, error=str(exc))

        # Default equity contract
        return Stock(symbol, "SMART", "USD")

    async def _resolve_index_like_symbol(self, symbol: str):
        """
        Resolve index-like / market-internals symbols via reqContractDetailsAsync(Index(...)).

        Examples:
        - AD-NASD (NASDAQ advance-decline)
        - ADD-NASD (alias sometimes used; we also try AD-*)
        """
        s = str(symbol).strip().upper()
        candidate_symbols: list[str] = [s]

        # Common alias flip: "ADD-XXX" vs "AD-XXX"
        if s.startswith("ADD-") and "-" in s:
            candidate_symbols.append("AD-" + s.split("-", 1)[1])
        if s.startswith("AD-") and "-" in s:
            candidate_symbols.append("ADD-" + s.split("-", 1)[1])

        suffix = s.split("-", 1)[1] if "-" in s else ""
        exchange_hints: list[str] = []
        if suffix in ("NASD", "NASDAQ"):
            exchange_hints.extend(["NASDAQ", "SMART", "NASD"])
        elif suffix == "NYSE":
            exchange_hints.extend(["NYSE", "SMART"])
        else:
            exchange_hints.extend(["SMART", "NASDAQ", "NYSE", "CBOE", "CBOEIND"])

        tried = set()
        last_err: Optional[Exception] = None
        for sym in candidate_symbols:
            for ex in exchange_hints:
                key = (sym, ex)
                if key in tried:
                    continue
                tried.add(key)
                candidate = Index(sym, ex, "USD")
                try:
                    details = await self.ib.reqContractDetailsAsync(candidate)
                except Exception as exc:
                    last_err = exc
                    continue
                if details:
                    contract = details[0].contract
                    logger.info(
                        "Resolved index-like contract",
                        symbol=s,
                        requested_symbol=sym,
                        requested_exchange=ex,
                        resolved_symbol=getattr(contract, "symbol", None),
                        resolved_exchange=getattr(contract, "exchange", None),
                        con_id=getattr(contract, "conId", None),
                    )
                    return contract

        raise RuntimeError(f"Unable to resolve index-like symbol: {s} ({last_err})")

    async def _subscribe_symbol(self, symbol: str):
        """Subscribe to single symbol"""
        contract = await self._resolve_contract(symbol)

        # Qualify contract
        qualified = await self.ib.qualifyContractsAsync(contract)
        if not qualified:
            logger.warning("Failed to qualify contract", symbol=symbol)
            return

        contract = qualified[0]
        try:
            logger.info("Qualified contract", symbol=symbol, exchange=getattr(contract, 'exchange', ''), con_id=getattr(contract, 'conId', None))
        except Exception:
            pass

        # Subscribe to market data
        ticker = self.ib.reqMktData(
            contract,
            genericTickList='',
            snapshot=False,
            regulatorySnapshot=False
        )
        if str(os.getenv("IB_LOG_TICKER_ID", "")).lower() in ("1", "true", "yes", "on"):
            try:
                logger.info(
                    "IB ticker subscribed",
                    symbol=symbol,
                    ticker_id=getattr(ticker, "tickerId", None),
                    sec_type=getattr(contract, "secType", None),
                    exchange=getattr(contract, "exchange", None),
                )
            except Exception:
                pass

        # Store (protect with lock)
        import time
        async with self._state_lock:
            self.contracts[symbol] = contract
            self.tickers[symbol] = ticker
            self._ticker_id_to_symbol[id(ticker)] = symbol
            self._symbol_subscription_ts[str(symbol).strip().upper()] = float(time.time())

        # Set up callback
        ticker.updateEvent += self._on_ticker_update

        logger.info("Subscribed to market data", symbol=symbol)

        # Kick a one-time snapshot for VIX/VIXY to prime the feed if no ticks yet
        if symbol in ("VIX", "VIXY"):
            try:
                self.ib.reqMktData(contract, genericTickList='', snapshot=True, regulatorySnapshot=False)
                logger.info("Requested VIX snapshot to prime feed", symbol=symbol)
            except Exception as exc:
                logger.warning("VIX snapshot request failed", symbol=symbol, error=str(exc))

    def _is_market_open(self) -> bool:
        """Check if current time is within configured sessions."""
        from datetime import datetime
        from zoneinfo import ZoneInfo

        # Get current time in US/Eastern
        et_tz = ZoneInfo('America/New_York')
        now_et = datetime.now(et_tz)
        hour = now_et.hour
        minute = now_et.minute
        time_decimal = hour + minute / 60.0

        if self.rth_only:
            return self._is_rth()
        return self._current_session_phase(time_decimal) in self.allowed_sessions

    @staticmethod
    def _current_session_phase(time_decimal: float) -> str:
        if time_decimal >= 20.0 or time_decimal < 3.833333:
            return "OVERNIGHT"
        if 4.0 <= time_decimal < 9.5:
            return "PREMARKET"
        if 9.5 <= time_decimal < 16.0:
            return "RTH"
        if 16.0 <= time_decimal < 20.0:
            return "AFTERHOURS"
        return "OFF"

    def _is_rth(self) -> bool:
        """Check if current time is within regular trading hours (RTH) only"""
        from datetime import datetime
        from zoneinfo import ZoneInfo

        # Get current time in US/Eastern
        et_tz = ZoneInfo('America/New_York')
        now_et = datetime.now(et_tz)
        hour = now_et.hour
        minute = now_et.minute
        time_decimal = hour + minute / 60.0

        open_dec, close_dec = self.rth_window if isinstance(self.rth_window, tuple) else (9.5, 16.0)
        return open_dec <= time_decimal < close_dec

    def _on_ticker_update(self, ticker: Ticker):
        """IB ticker update callback"""
        try:
            # Filter: only process ticks during extended trading hours (pre-market + RTH + after-hours)
            if not self._is_market_open():
                # Always allow volatility feed (VIX/VIXY) through for refs_vix_ticks
                sym = getattr(getattr(ticker, 'contract', None), 'symbol', '') or ''
                if sym not in ('VIX', 'VIXY'):
                    return

            # Get current timestamp in microseconds (wall clock time)
            import time
            ts_us = int(time.time() * 1_000_000)

            # Normalize tick
            alias = self._ticker_id_to_symbol.get(id(ticker))
            tick = TickData.from_ib_ticker(ticker, ts_us, symbol_override=alias)
            symbol_u = str(tick.symbol or "").strip().upper()
            if symbol_u:
                self._symbol_last_update_ts[symbol_u] = float(time.time())

            # For VIX/VIXY, log sampled tick fields to aid diagnosis
            sym = getattr(getattr(ticker, 'contract', None), 'symbol', '') or ''
            if sym in ('VIX', 'VIXY'):
                import time as _t
                last = self._last_vix_log_ts.get(sym, 0.0)
                if _t.time() - last > 5.0:
                    try:
                        logger.info("VIX feed tick",
                                    symbol=sym,
                                    last=getattr(ticker, 'last', None),
                                    bid=getattr(ticker, 'bid', None),
                                    ask=getattr(ticker, 'ask', None),
                                    time=str(getattr(ticker, 'time', '')))
                    except Exception:
                        pass
                    self._last_vix_log_ts[sym] = _t.time()

            # Deliver to callback
            if self.tick_callback:
                # Call in thread pool to avoid blocking IB event loop
                asyncio.create_task(self._deliver_tick(tick))

        except Exception as e:
            logger.error("Ticker update error", error=str(e))

    async def _deliver_tick(self, tick: TickData):
        """Deliver tick to callback"""
        try:
            if asyncio.iscoroutinefunction(self.tick_callback):
                await self.tick_callback(tick)
            else:
                self.tick_callback(tick)
        except Exception as e:
            logger.error("Tick callback error", error=str(e), symbol=tick.symbol)

    async def _health_monitor(self):
        """Monitor connection health and reconnect if needed"""
        while self._running:
            try:
                await asyncio.sleep(10)  # Check every 10 seconds

                # Re-check _running after sleep (shutdown may have started)
                if not self._running:
                    break

                if not self.ib.isConnected():
                    logger.warning("IB connection lost, reconnecting...")
                    await self._reconnect()
                    continue

                await self._try_subscribe_deferred()
                await self._check_stale_subscriptions()

            except Exception as e:
                logger.error("Health monitor error", error=str(e))

    async def _check_stale_subscriptions(self) -> None:
        """Best-effort per-symbol stale feed recovery without full reconnect."""
        if self._stale_resubscribe_sec <= 0:
            return
        if not self._is_market_open():
            return

        # Outside RTH, sparse or absent ticks are expected. Avoid aggressive
        # resubscribe churn in after-hours/overnight sessions where IB farms
        # are more likely to flap and the feed may legitimately stay quiet.
        if self._session_phase_et() != "RTH":
            return

        import time

        now_ts = float(time.time())
        async with self._state_lock:
            symbols_to_check = list(self.symbols)

        for symbol in symbols_to_check:
            symbol_u = str(symbol or "").strip().upper()
            if not symbol_u or self._is_index_like_symbol(symbol_u):
                continue

            last_tick_ts = self._symbol_last_update_ts.get(symbol_u)
            if last_tick_ts is not None:
                stale_age_sec = now_ts - float(last_tick_ts)
            else:
                subscribed_at = self._symbol_subscription_ts.get(symbol_u)
                if subscribed_at is None:
                    continue
                stale_age_sec = now_ts - float(subscribed_at)
            if stale_age_sec < self._stale_resubscribe_sec:
                continue

            last_try_ts = float(self._stale_resubscribe_last_try_ts.get(symbol_u) or 0.0)
            if (now_ts - last_try_ts) < self._stale_resubscribe_cooldown_sec:
                continue

            self._stale_resubscribe_last_try_ts[symbol_u] = now_ts
            logger.warning(
                "IB symbol feed stale, resubscribing",
                symbol=symbol_u,
                stale_age_sec=round(stale_age_sec, 2),
                stale_threshold_sec=self._stale_resubscribe_sec,
            )
            await self._resubscribe_symbol(symbol_u)

    async def _resubscribe_symbol(self, symbol: str) -> None:
        symbol_u = str(symbol or "").strip().upper()
        if not symbol_u:
            return

        contract = None
        ticker = None
        async with self._state_lock:
            contract = self.contracts.get(symbol_u)
            ticker = self.tickers.get(symbol_u)

        if contract is not None:
            try:
                self.ib.cancelMktData(contract)
            except Exception as exc:
                logger.warning("Failed to cancel stale market data before resubscribe", symbol=symbol_u, error=str(exc))

        if ticker is not None:
            try:
                ticker.updateEvent -= self._on_ticker_update
            except Exception:
                pass

        async with self._state_lock:
            self.contracts.pop(symbol_u, None)
            old_ticker = self.tickers.pop(symbol_u, None)
            if old_ticker is not None:
                self._ticker_id_to_symbol.pop(id(old_ticker), None)

        try:
            await self._subscribe_symbol(symbol_u)
            async with self._state_lock:
                subscribed_ok = symbol_u in self.contracts and symbol_u in self.tickers
            if not subscribed_ok:
                raise RuntimeError("subscribe_symbol_missing_state")
            logger.info("IB symbol resubscribe completed", symbol=symbol_u)
        except Exception as exc:
            logger.error("IB symbol resubscribe failed, restoring previous subscription", symbol=symbol_u, error=str(exc))
            async with self._state_lock:
                if contract is not None:
                    self.contracts[symbol_u] = contract
                if ticker is not None:
                    self.tickers[symbol_u] = ticker
                    self._ticker_id_to_symbol[id(ticker)] = symbol_u
            if ticker is not None:
                try:
                    ticker.updateEvent += self._on_ticker_update
                except Exception:
                    pass

    async def _reconnect(self):
        """Reconnect to IB and resubscribe"""
        # Don't reconnect if shutting down
        if not self._running:
            logger.debug("Skipping reconnect - service is stopping")
            return

        try:
            # Clear state (protect with lock)
            async with self._state_lock:
                self.contracts.clear()
                self.tickers.clear()

            # Reconnect
            await self._connect()

            # Resubscribe
            await self._subscribe_all()

            logger.info("Reconnection successful")

        except Exception as e:
            logger.error("Reconnection failed", error=str(e))
            # Will retry on next health check

    def get_ticker(self, symbol: str) -> Optional[Ticker]:
        """Get current ticker for symbol"""
        return self.tickers.get(symbol)

    def get_last_price(self, symbol: str) -> Optional[float]:
        """Get last trade price for symbol"""
        ticker = self.tickers.get(symbol)
        if ticker and ticker.last and ticker.last > 0:
            return float(ticker.last)
        return None

    def is_connected(self) -> bool:
        """Check if connected"""
        return self.ib.isConnected()

    # ========== Dynamic Subscription API (Thread-Safe) ==========

    async def subscribe_symbol(self, symbol: str) -> bool:
        """
        Dynamically subscribe to a new symbol's tick data.

        This is thread-safe and can be called while the adapter is running.

        Args:
            symbol: Stock symbol to subscribe (e.g., 'AAPL', 'TSLA')

        Returns:
            bool: True if subscription successful, False otherwise

        Note:
            - Data will start flowing after 2-5 seconds (IBKR delay)
            - Subscription will persist until restart or explicit unsubscribe
            - IBKR free accounts limited to ~100 concurrent subscriptions
        """
        symbol = str(symbol).strip().upper()
        if not symbol:
            logger.warning("Cannot subscribe empty symbol")
            return False

        symbol_added = False

        # Check if already subscribed
        async with self._state_lock:
            if symbol in self.contracts:
                logger.info("Symbol already subscribed", symbol=symbol)
                return True

            # Add to symbols list
            if symbol not in self.symbols:
                self.symbols.append(symbol)
                symbol_added = True

        # When disconnected, accept request and defer to reconnect path.
        # This avoids noisy hard failures during transient IB restarts.
        if not self.ib.isConnected():
            if self._should_defer_symbol(symbol):
                async with self._state_lock:
                    self._deferred_symbols.add(symbol)
            logger.warning(
                "Dynamic subscription deferred - IB not connected",
                symbol=symbol,
                deferred=True,
            )
            return True

        # Subscribe (outside lock to avoid blocking)
        try:
            await self._subscribe_symbol(symbol)
            logger.info("✅ Dynamic subscription successful",
                       symbol=symbol,
                       total_subscriptions=len(self.contracts))
            return True
        except Exception as e:
            logger.error("Failed to dynamically subscribe",
                        symbol=symbol,
                        error=str(e))
            # Roll back symbol insertion on immediate subscription failure.
            async with self._state_lock:
                if symbol_added and symbol in self.symbols:
                    self.symbols.remove(symbol)
            return False

    async def unsubscribe_symbol(self, symbol: str) -> bool:
        """
        Unsubscribe from a symbol's tick data.

        Args:
            symbol: Stock symbol to unsubscribe

        Returns:
            bool: True if unsubscription successful, False otherwise
        """
        async with self._state_lock:
            if symbol not in self.contracts:
                logger.warning("Symbol not subscribed, cannot unsubscribe", symbol=symbol)
                return False

            contract = self.contracts[symbol]
            ticker = self.tickers.get(symbol)

        # Unsubscribe (outside lock)
        try:
            self.ib.cancelMktData(contract)

            # Remove callback if ticker exists
            if ticker:
                try:
                    ticker.updateEvent -= self._on_ticker_update
                except Exception:
                    pass  # Ignore callback removal errors

            # Clean up state
            async with self._state_lock:
                del self.contracts[symbol]
                if symbol in self.tickers:
                    del self.tickers[symbol]
                if symbol in self.symbols:
                    self.symbols.remove(symbol)

            logger.info("✅ Unsubscribed from symbol",
                       symbol=symbol,
                       remaining_subscriptions=len(self.contracts))
            return True

        except Exception as e:
            logger.error("Failed to unsubscribe",
                        symbol=symbol,
                        error=str(e))
            return False

    def get_subscribed_symbols(self) -> List[str]:
        """
        Get list of currently subscribed symbols.

        Returns:
            List[str]: List of subscribed stock symbols

        Note:
            This returns a snapshot. The list may change if concurrent
            subscribe/unsubscribe operations occur.
        """
        # No need for lock here - dict.keys() is atomic in CPython
        # But we return a copy to avoid external modification
        return list(self.contracts.keys())


async def main():
    """Test main"""
    import time

    def tick_handler(tick: TickData):
        """Print ticks"""
        print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] "
              f"{tick.symbol}: last={tick.last} size={tick.last_size} "
              f"bid={tick.bid} ask={tick.ask}")

    # Create adapter
    adapter = IBMarketDataAdapter(
        symbols=['AAPL', 'MSFT'],
        tick_callback=tick_handler
    )

    try:
        # Start
        await adapter.start()

        print("\nReceiving market data (Ctrl+C to stop)...\n")

        # Run for 60 seconds
        start = time.time()
        while time.time() - start < 60:
            await asyncio.sleep(1)

            # Print connection status
            if adapter.is_connected():
                for symbol in adapter.symbols:
                    price = adapter.get_last_price(symbol)
                    print(f"{symbol}: ${price:.2f}" if price else f"{symbol}: N/A")

    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        await adapter.stop()


if __name__ == '__main__':
    asyncio.run(main())
