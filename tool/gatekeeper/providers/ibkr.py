from __future__ import annotations

import math
import logging
from logging.handlers import RotatingFileHandler
import os
import threading
import time
from datetime import datetime
from datetime import date as date_type
from datetime import timedelta
from dataclasses import dataclass
import json
from typing import Any, TYPE_CHECKING
import asyncio
from zoneinfo import ZoneInfo
from pathlib import Path

if TYPE_CHECKING:
    from ib_insync import IB, Index, Option, Stock
    from ib_insync.contract import Contract
    from ib_insync.objects import ContractDetails


def _is_valid_price(value: float | None) -> bool:
    if value is None:
        return False
    if isinstance(value, float) and math.isnan(value):
        return False
    return value > 0


_ET = ZoneInfo("America/New_York")


def _ensure_asyncio_event_loop() -> None:
    asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())
    try:
        loop = asyncio.get_running_loop()
        if not loop.is_closed():
            return
    except RuntimeError:
        pass
    try:
        loop = asyncio.get_event_loop()
        if not loop.is_closed():
            return
    except RuntimeError:
        pass
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)


_IBKR_LOGGER: logging.Logger | None = None


def _ibkr_logger() -> logging.Logger:
    global _IBKR_LOGGER  # noqa: PLW0603
    if _IBKR_LOGGER is not None:
        return _IBKR_LOGGER

    logger = logging.getLogger("gatekeeper.ibkr")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    log_dir = Path(__file__).resolve().parent.parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "ibkr.jsonl"

    if not logger.handlers:
        handler = RotatingFileHandler(str(log_path), maxBytes=10_000_000, backupCount=5, encoding="utf-8")
        handler.setLevel(logging.INFO)
        logger.addHandler(handler)

    _IBKR_LOGGER = logger
    return logger


def _ibkr_log_json(event: str, **fields: Any) -> None:
    payload = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "event": str(event),
        "ui_session_id": os.environ.get("GATEKEEPER_UI_SESSION_ID"),
        "thread": threading.get_ident(),
        "fields": fields,
    }
    try:
        _ibkr_logger().info(json.dumps(payload, ensure_ascii=False, default=str))
    except Exception:
        return


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clean_price(value: Any) -> float | None:
    v = _as_float(value)
    if not _is_valid_price(v):
        return None
    return float(v)

def _has_nan_prices(tk: Any | None) -> bool:
    if tk is None:
        return False
    for key in ("bid", "ask", "last"):
        v = _as_float(getattr(tk, key, None))
        if v is not None and isinstance(v, float) and math.isnan(v):
            return True
    return False


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
) -> float | None:
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
) -> tuple[float, float, float, float]:
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
        delta = -disc_q * _norm_cdf(-d1)
        theta = (
            -disc_q * spot * pdf * sigma / (2.0 * sqrt_t)
            + r * disc_r * strike * _norm_cdf(-d2)
            - q * disc_q * spot * _norm_cdf(-d1)
        )
    return delta, gamma, vega, theta


def _contract_for_symbol(symbol: str):
    symbol = symbol.upper().strip()
    from ib_insync import Index, Stock

    if symbol in {"VIX", "VVIX"}:
        return Index(symbol, "CBOE")
    return Stock(symbol, "SMART", "USD")


def _what_to_show_for_contract(contract, default: str) -> str:
    sec_type = getattr(contract, "secType", "")
    if sec_type == "IND":
        return "TRADES"
    return default


def _bars_to_records(bars) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for bar in bars:
        date = getattr(bar, "date", None)
        date_str = date.isoformat() if hasattr(date, "isoformat") else str(date)
        records.append(
            {
                "time": date_str,
                "open": float(bar.open),
                "high": float(bar.high),
                "low": float(bar.low),
                "close": float(bar.close),
                "volume": float(getattr(bar, "volume", 0.0) or 0.0),
            }
        )
    return records


def _log(event: str, **fields: Any) -> None:
    ts = datetime.now().isoformat(timespec="seconds")
    details = " ".join(f"{k}={fields[k]}" for k in sorted(fields.keys()))
    print(f"[IBKR] {ts} {event} {details}".strip())
    _ibkr_log_json(event, **fields)


def _parse_yyyymmdd(raw: str) -> date_type | None:
    raw = str(raw).strip()
    if len(raw) != 8 or not raw.isdigit():
        return None
    try:
        return datetime.strptime(raw, "%Y%m%d").date()
    except ValueError:
        return None


def _is_third_friday(day: date_type) -> bool:
    if day.weekday() != 4:  # Friday
        return False
    first = day.replace(day=1)
    first_friday_offset = (4 - first.weekday()) % 7
    first_friday = first + timedelta(days=first_friday_offset)
    third_friday = first_friday + timedelta(days=14)
    return day == third_friday


def _choose_expiry(expirations: list[str], *, min_dte: int = 25, max_dte: int = 45, target_dte: int = 30) -> str | None:
    today = datetime.now().date()
    candidates: list[tuple[int, bool, str]] = []
    for exp in expirations:
        day = _parse_yyyymmdd(exp)
        if day is None:
            continue
        dte = (day - today).days
        if dte < min_dte or dte > max_dte:
            continue
        candidates.append((abs(dte - target_dte), _is_third_friday(day), exp))
    if not candidates:
        return None
    monthly = [c for c in candidates if c[1]]
    best_pool = monthly if monthly else candidates
    best_pool.sort(key=lambda x: x[0])
    return best_pool[0][2]


@dataclass(slots=True)
class IBKRConnectionParams:
    host: str
    port: int
    client_id: int
    market_data_type: int = 1


class IBKRProvider:
    def __init__(self, ib: "IB | None" = None) -> None:
        self._owner_thread_id = threading.get_ident()
        self._connection_thread_id: int | None = None
        self._error_handler_registered = False
        self._error_events: list[dict[str, Any]] = []
        self._last_price_sources: dict[str, str] = {}
        self._market_open_cache: dict[tuple[str, str], bool] = {}
        self._contract_cache: dict[str, "Contract"] = {}
        self._contract_details_cache: dict[str, "ContractDetails"] = {}
        self._secdef_cache: dict[str, list[Any]] = {}
        self._ivr_range_cache: dict[str, dict[str, Any]] = {}
        self._market_data_type: int | None = None
        self._last_connect_params: IBKRConnectionParams | None = None
        self._conid_cache_path = (Path(__file__).resolve().parents[1] / ".cache" / "ibkr_conids.json")
        self._conid_cache: dict[str, int] = {}
        self._ivr_range_cache_path = (Path(__file__).resolve().parents[1] / ".cache" / "ibkr_ivr_ranges.json")
        self._load_conid_cache()
        self._load_ivr_range_cache()
        self._secdef_snapshot_path = (Path(__file__).resolve().parents[1] / ".cache" / "ibkr_secdef_snapshots.json")
        self._secdef_snapshots: dict[str, dict[str, Any]] = {}
        self._load_secdef_snapshots()
        self._spot_cache: dict[str, tuple[float, float]] = {}
        self._spot_cache_ttl_s: float = 300.0
        self._ensure_event_loop()
        if ib is None:
            from ib_insync import IB

            self.ib = IB()
        else:
            self.ib = ib
        self._register_error_handler()
        _log("init", thread=self._owner_thread_id)

    def _secdef_snapshot_exists(self, symbol: str) -> bool:
        sym = symbol.upper().strip()
        raw = self._secdef_snapshots.get(sym)
        if not isinstance(raw, dict):
            return False
        return bool(raw.get("expirations")) and bool(raw.get("strikes"))

    def _load_conid_cache(self) -> None:
        try:
            path = self._conid_cache_path
            if not path.exists():
                return
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._conid_cache = {str(k).upper(): int(v) for k, v in raw.items() if str(k) and int(v) > 0}
        except Exception:
            return

    def _load_ivr_range_cache(self) -> None:
        try:
            path = self._ivr_range_cache_path
            if not path.exists():
                return
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return
            out: dict[str, dict[str, Any]] = {}
            for sym, payload in raw.items():
                if not isinstance(payload, dict):
                    continue
                low = _as_float(payload.get("low"))
                high = _as_float(payload.get("high"))
                asof = str(payload.get("asof") or "").strip()
                if not sym or low is None or high is None or high <= low or not asof:
                    continue
                out[str(sym).upper()] = {"date": asof, "low": float(low), "high": float(high), "n": payload.get("n")}
            if out:
                self._ivr_range_cache.update(out)
        except Exception:
            return

    def _save_ivr_range_cache(self) -> None:
        try:
            path = self._ivr_range_cache_path
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {}
            for sym, row in self._ivr_range_cache.items():
                try:
                    low = float(row.get("low"))
                    high = float(row.get("high"))
                    asof = str(row.get("date") or "").strip()
                    if not sym or not asof or high <= low:
                        continue
                    payload[str(sym).upper()] = {
                        "asof": asof,
                        "low": low,
                        "high": high,
                        "n": row.get("n"),
                    }
                except Exception:
                    continue
            path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        except Exception:
            return

    def _load_secdef_snapshots(self) -> None:
        try:
            path = self._secdef_snapshot_path
            if not path.exists():
                return
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._secdef_snapshots = {str(k).upper(): v for k, v in raw.items() if isinstance(v, dict)}
        except Exception:
            return

    def _save_secdef_snapshots(self) -> None:
        try:
            path = self._secdef_snapshot_path
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {k: v for k, v in sorted(self._secdef_snapshots.items())}
            path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        except Exception:
            return

    def _get_secdef_snapshot(self, symbol: str) -> Any | None:
        sym = symbol.upper().strip()
        raw = self._secdef_snapshots.get(sym)
        if not raw:
            return None
        expirations = raw.get("expirations")
        strikes = raw.get("strikes")
        if not isinstance(expirations, list) or not isinstance(strikes, list):
            return None
        try:
            exchange = str(raw.get("exchange") or "SMART")
            trading_class = str(raw.get("tradingClass") or sym)
            multiplier = str(raw.get("multiplier") or "")
            exp = [str(x) for x in expirations if str(x)]
            st = [float(x) for x in strikes if _as_float(x) is not None]
        except Exception:
            return None

        @dataclass(slots=True)
        class _Chain:
            exchange: str
            tradingClass: str
            multiplier: str
            expirations: list[str]
            strikes: list[float]

        return _Chain(exchange=exchange, tradingClass=trading_class, multiplier=multiplier, expirations=exp, strikes=st)

    def _set_secdef_snapshot(self, symbol: str, chain: Any) -> None:
        sym = symbol.upper().strip()
        try:
            expirations = list(getattr(chain, "expirations", []) or [])
            strikes = list(getattr(chain, "strikes", []) or [])
            payload = {
                "asof": datetime.now(_ET).isoformat(timespec="seconds"),
                "exchange": str(getattr(chain, "exchange", "SMART") or "SMART"),
                "tradingClass": str(getattr(chain, "tradingClass", sym) or sym),
                "multiplier": str(getattr(chain, "multiplier", "") or ""),
                "expirations": [str(x) for x in expirations if str(x)],
                "strikes": [float(x) for x in strikes if _as_float(x) is not None],
            }
            if payload["expirations"] and payload["strikes"]:
                self._secdef_snapshots[sym] = payload
                self._save_secdef_snapshots()
        except Exception:
            return

    def _save_conid_cache(self) -> None:
        try:
            path = self._conid_cache_path
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {k: int(v) for k, v in sorted(self._conid_cache.items())}
            path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        except Exception:
            return

    def _get_cached_conid(self, symbol: str) -> int | None:
        try:
            cid = int(self._conid_cache.get(symbol.upper().strip(), 0))
        except Exception:
            return None
        return cid if cid > 0 else None

    def _set_cached_conid(self, symbol: str, con_id: int) -> None:
        try:
            sym = symbol.upper().strip()
            if not sym:
                return
            cid = int(con_id)
            if cid <= 0:
                return
            if self._conid_cache.get(sym) == cid:
                return
            self._conid_cache[sym] = cid
            self._save_conid_cache()
        except Exception:
            return

    def is_connected(self) -> bool:
        try:
            return bool(self.ib.isConnected()) and self._connection_thread_id == threading.get_ident()
        except Exception:
            return False

    def _ensure_thread(self, *, force_rebind: bool = False) -> None:
        if threading.get_ident() == self._owner_thread_id and not force_rebind:
            return
        self._owner_thread_id = threading.get_ident()
        try:
            if self.ib.isConnected():
                self.ib.disconnect()
        except Exception:
            pass
        self._connection_thread_id = None
        self._ensure_event_loop()
        from ib_insync import IB

        self.ib = IB()
        self._error_handler_registered = False
        self._register_error_handler()
        _log("thread_rebind", thread=self._owner_thread_id, forced=force_rebind)

    def connect(self, params: IBKRConnectionParams, timeout_s: float = 3.0) -> tuple[bool, str]:
        self._ensure_event_loop()
        self._last_connect_params = params
        if bool(self.ib.isConnected()):
            if self._connection_thread_id == threading.get_ident():
                _log("connect_skip", host=params.host, port=params.port, client_id=params.client_id)
                return True, "Already connected"
            _log("connect_thread_mismatch", host=params.host, port=params.port, client_id=params.client_id)
            self._connection_thread_id = None
        self._ensure_thread(force_rebind=True)
        self._ensure_event_loop()
        if self.is_connected():
            _log("connect_skip", host=params.host, port=params.port, client_id=params.client_id)
            return True, "Already connected"
        try:
            _log("connect_start", host=params.host, port=params.port, client_id=params.client_id)
            self.ib.connect(params.host, params.port, clientId=params.client_id, timeout=timeout_s)
            self.ib.RequestTimeout = 10
            try:
                self.ib.reqMarketDataType(int(params.market_data_type))
            except Exception:
                pass
            self._market_data_type = int(params.market_data_type)
        except Exception as exc:  # noqa: BLE001
            _log("connect_error", error=exc)
            return False, f"Connect failed: {exc}"
        self._connection_thread_id = threading.get_ident()
        _log("connect_ok", host=params.host, port=params.port, client_id=params.client_id)
        return True, "Connected"

    def _reconnect(self, *, reason: str) -> tuple[bool, str]:
        params = self._last_connect_params
        if params is None:
            return False, "No previous connection params"
        _log("reconnect_start", reason=reason, host=params.host, port=params.port, client_id=params.client_id)
        try:
            self.disconnect()
        except Exception:
            pass
        ok, msg = self.connect(params)
        if ok:
            _log("reconnect_ok", reason=reason)
        else:
            _log("reconnect_error", reason=reason, error=msg)
        return ok, msg

    def _switch_market_data_type(self, new_type: int) -> None:
        try:
            self.ib.reqMarketDataType(int(new_type))
            self._market_data_type = int(new_type)
            _log("market_data_type_set", market_data_type=int(new_type))
        except Exception as exc:
            _log("market_data_type_error", market_data_type=int(new_type), error=repr(exc))

    def _saw_error_code(self, events: list[dict[str, Any]], code: int) -> bool:
        for ev in events:
            try:
                if int(ev.get("code", -9999)) == int(code):
                    return True
            except Exception:
                continue
        return False

    def _register_error_handler(self) -> None:
        if self._error_handler_registered:
            return
        try:
            self.ib.errorEvent += self._on_error
            self._error_handler_registered = True
        except Exception:
            pass

    def _on_error(self, req_id, error_code, error_string, contract) -> None:
        symbol = getattr(contract, "symbol", None) if contract is not None else None
        sec_type = getattr(contract, "secType", None) if contract is not None else None
        exchange = getattr(contract, "exchange", None) if contract is not None else None
        event = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "req_id": req_id,
            "code": error_code,
            "message": error_string,
            "symbol": symbol,
            "sec_type": sec_type,
            "exchange": exchange,
        }
        self._error_events.append(event)
        _log(
            "ib_error",
            req_id=req_id,
            code=error_code,
            message=error_string,
            symbol=symbol,
            sec_type=sec_type,
            exchange=exchange,
        )

    def drain_errors(self) -> list[dict[str, Any]]:
        if not self._error_events:
            return []
        events = list(self._error_events)
        self._error_events.clear()
        return events

    @staticmethod
    def _ensure_event_loop() -> None:
        _ensure_asyncio_event_loop()

    def disconnect(self) -> None:
        try:
            self.ib.disconnect()
        except Exception:
            pass
        self._connection_thread_id = None

    def fetch_last_price(
        self,
        symbol: str,
        *,
        timeout_s: float = 6,
        history_lookback_min: int = 2,
        history_use_rth: bool = False,
        check_market_hours: bool = False,
        allow_daily_fallback: bool = True,
    ) -> tuple[float | None, str | None]:
        self._ensure_thread()
        if not self.is_connected():
            _log("last_price_error", symbol=symbol, error="not_connected")
            self._last_price_sources[symbol] = "error"
            return None, "IBKR not connected"

        # For prices/bars we keep this lightweight (no contractDetails), to avoid secdef pacing/timeouts.
        contract = _contract_for_symbol(symbol)

        if check_market_hours:
            market_open = self._is_market_open_today(contract, symbol)
            if market_open is not None:
                _log("market_open_status", symbol=symbol, is_open=market_open)
            if market_open is False:
                return self._fetch_daily_close(symbol, contract, timeout_s)

        start = time.perf_counter()
        try:
            _log(
                "last_price_start",
                symbol=symbol,
                sec_type=getattr(contract, "secType", ""),
                exchange=getattr(contract, "exchange", ""),
            )
            ticker = self.ib.reqMktData(contract, "", False, False)
            try:
                self.ib.sleep(0.4)
                last = getattr(ticker, "last", None)
                price = last if _is_valid_price(last) else ticker.marketPrice()
                if _is_valid_price(price):
                    elapsed = round(time.perf_counter() - start, 3)
                    _log("last_price_ok", symbol=symbol, price=price, source="mktdata")
                    _log("last_price_timing", symbol=symbol, stage="mktdata", elapsed_s=elapsed)
                    self._last_price_sources[symbol] = "mktdata"
                    try:
                        self._spot_cache[symbol.upper().strip()] = (time.time() + self._spot_cache_ttl_s, float(price))
                    except Exception:
                        pass
                    return float(price), None
            finally:
                try:
                    self.ib.cancelMktData(contract)
                except Exception:
                    pass
        except Exception:
            pass
        _log("last_price_fallback_bars", symbol=symbol, lookback_min=history_lookback_min, what_to_show="TRADES")
        bars, err = self._fetch_bars_1m(
            symbol=symbol,
            lookback_min=history_lookback_min,
            use_rth=history_use_rth,
            bar_size="1 min",
            what_to_show="TRADES",
            allow_retry=False,
            timeout_s=timeout_s,
        )
        if err:
            _log("last_price_error", symbol=symbol, error=err)
            self._last_price_sources[symbol] = "error"
            bars = None
        if not bars:
            if not allow_daily_fallback:
                _log("last_price_skip_daily", symbol=symbol, reason="no_daily_fallback")
                self._last_price_sources[symbol] = "error"
                return None, "No bars returned"
            return self._fetch_daily_close(symbol, contract, timeout_s)
        _log("last_price_ok", symbol=symbol, price=bars[-1]["close"], source="bars")
        _log("last_price_timing", symbol=symbol, stage="bars", elapsed_s=round(time.perf_counter() - start, 3))
        self._last_price_sources[symbol] = "bars"
        try:
            self._spot_cache[symbol.upper().strip()] = (time.time() + self._spot_cache_ttl_s, float(bars[-1]["close"]))
        except Exception:
            pass
        return float(bars[-1]["close"]), None

    def get_last_price_source(self, symbol: str) -> str | None:
        return self._last_price_sources.get(symbol)

    def fetch_daily_close(self, symbol: str, timeout_s: float = 10) -> tuple[float | None, str | None]:
        self._ensure_thread()
        self._ensure_event_loop()
        if not self.is_connected():
            _log("last_price_error", symbol=symbol, error="not_connected")
            self._last_price_sources[symbol] = "error"
            return None, "IBKR not connected"
        contract = _contract_for_symbol(symbol)
        return self._fetch_daily_close(symbol, contract, timeout_s)

    def _fetch_daily_close(self, symbol: str, contract, timeout_s: float) -> tuple[float | None, str | None]:
        _log("last_price_fallback_daily", symbol=symbol, lookback_days=5)
        daily_bars, daily_err = self._fetch_bars_1m(
            symbol=symbol,
            lookback_min=5 * 1440,
            use_rth=False,
            bar_size="1 day",
            what_to_show="TRADES",
            allow_retry=False,
            timeout_s=max(timeout_s, 10),
        )
        if daily_err:
            _log("last_price_error", symbol=symbol, error=daily_err)
            self._last_price_sources[symbol] = "error"
            return None, daily_err
        if not daily_bars:
            _log("last_price_error", symbol=symbol, error="no_bars")
            self._last_price_sources[symbol] = "error"
            return None, "No bars returned"
        _log("last_price_ok", symbol=symbol, price=daily_bars[-1]["close"], source="daily_bars")
        self._last_price_sources[symbol] = "daily_bars"
        return float(daily_bars[-1]["close"]), None

    def _is_market_open_today(self, contract, symbol: str) -> bool | None:
        today = datetime.now().strftime("%Y%m%d")
        key = (symbol, today)
        cached = self._market_open_cache.get(key)
        if cached is not None:
            return cached
        details = self._get_contract_details(symbol, contract)
        if details is None:
            return None
        try:
            trading_hours = getattr(details, "tradingHours", "") or ""
            for part in trading_hours.split(";"):
                if ":" not in part:
                    continue
                date_str, hours = part.split(":", 1)
                if date_str != today:
                    continue
                if "CLOSED" in hours:
                    self._market_open_cache[key] = False
                    return False
                if hours.strip():
                    self._market_open_cache[key] = True
                    return True
        except Exception:
            return None
        return None

    def _get_contract(self, symbol: str, *, ensure_qualify: bool = False):
        symbol = symbol.upper().strip()
        cached = self._contract_cache.get(symbol)
        if cached is not None:
            if ensure_qualify and int(getattr(cached, "conId", 0) or 0) <= 0:
                cached = None
            else:
                _log("contract_cache_hit", symbol=symbol)
                return cached
        contract = _contract_for_symbol(symbol)
        sec_type = getattr(contract, "secType", "")
        if sec_type in {"STK", "IND"} and not ensure_qualify:
            _log("qualify_skip", symbol=symbol, sec_type=sec_type)
            self._contract_cache[symbol] = contract
            return contract

        if ensure_qualify and int(getattr(contract, "conId", 0) or 0) <= 0:
            cached_conid = self._get_cached_conid(symbol)
            if cached_conid:
                try:
                    setattr(contract, "conId", int(cached_conid))
                    _log("conid_cache_hit", symbol=symbol, con_id=int(cached_conid))
                except Exception:
                    pass
        if ensure_qualify and int(getattr(contract, "conId", 0) or 0) > 0:
            # Contract has a usable conId already (from cache); avoid secdef qualification unless needed.
            self._contract_cache[symbol] = contract
            return contract
        prev_timeout = None
        try:
            start = time.perf_counter()
            _log(
                "qualify_start",
                symbol=symbol,
                sec_type=sec_type,
                exchange=getattr(contract, "exchange", ""),
            )
            if ensure_qualify:
                try:
                    prev_timeout = getattr(self.ib, "RequestTimeout", None)
                    self.ib.RequestTimeout = 30
                except Exception:
                    prev_timeout = None
            qualified = self.ib.qualifyContracts(contract)
            if qualified:
                contract = qualified[0]
            elapsed = round(time.perf_counter() - start, 3)
            con_id = int(getattr(contract, "conId", 0) or 0)
            if con_id > 0:
                _log("qualify_ok", symbol=symbol, con_id=con_id, elapsed_s=elapsed)
                if ensure_qualify and sec_type == "STK":
                    self._set_cached_conid(symbol, con_id)
            else:
                _log("qualify_failed", symbol=symbol, con_id=con_id, elapsed_s=elapsed)
        except Exception as exc:
            err = str(exc).strip()
            _log("qualify_error", symbol=symbol, error=err or repr(exc))
            if ensure_qualify and "Timeout" in (err or repr(exc)):
                try:
                    self.ib.sleep(0.2)
                except Exception:
                    pass
                try:
                    start = time.perf_counter()
                    _log("qualify_retry", symbol=symbol)
                    qualified = self.ib.qualifyContracts(contract)
                    if qualified:
                        contract = qualified[0]
                    con_id = int(getattr(contract, "conId", 0) or 0)
                    _log(
                        "qualify_retry_ok" if con_id > 0 else "qualify_retry_failed",
                        symbol=symbol,
                        con_id=con_id,
                        elapsed_s=round(time.perf_counter() - start, 3),
                    )
                    if con_id > 0 and ensure_qualify and sec_type == "STK":
                        self._set_cached_conid(symbol, con_id)
                except Exception as exc2:
                    _log("qualify_retry_error", symbol=symbol, error=repr(exc2))
        finally:
            if ensure_qualify and prev_timeout is not None:
                try:
                    self.ib.RequestTimeout = prev_timeout
                except Exception:
                    pass
        if int(getattr(contract, "conId", 0) or 0) <= 0 and ensure_qualify:
            details = self._get_contract_details(symbol, contract)
            if details and getattr(details, "contract", None) is not None:
                contract = details.contract
                _log("contract_details_ok", symbol=symbol, con_id=getattr(contract, "conId", 0))
        con_id = int(getattr(contract, "conId", 0) or 0)
        if (not ensure_qualify) or con_id > 0:
            self._contract_cache[symbol] = contract
        return contract

    def _get_contract_details(self, symbol: str, contract):
        symbol = symbol.upper().strip()
        cached = self._contract_details_cache.get(symbol)
        if cached is not None:
            return cached
        try:
            details = self.ib.reqContractDetails(contract)
        except Exception as exc:
            _log("contract_details_error", symbol=symbol, error=repr(exc))
            return None
        if not details:
            return None
        self._contract_details_cache[symbol] = details[0]
        return details[0]

    def _get_option_chain_params(self, symbol: str, contract):
        symbol = symbol.upper().strip()
        cached = self._secdef_cache.get(symbol)
        if cached is not None:
            return cached
        snap = self._get_secdef_snapshot(symbol)
        if snap is not None:
            # Prefer the persisted snapshot to avoid long secdef timeouts in Streamlit/UI runs.
            _log("secdef_skip_use_snapshot", symbol=symbol, asof=str(self._secdef_snapshots.get(symbol, {}).get("asof", "")))
            self._secdef_cache[symbol] = [snap]
            return [snap]
        try:
            cached_conid = self._get_cached_conid(symbol)
            if int(getattr(contract, "conId", 0) or 0) <= 0 and cached_conid:
                try:
                    setattr(contract, "conId", int(cached_conid))
                    _log("conid_cache_hit", symbol=symbol, con_id=int(cached_conid))
                except Exception:
                    pass
            if int(getattr(contract, "conId", 0) or 0) <= 0:
                _log("qualify_start", symbol=symbol, sec_type=getattr(contract, "secType", ""))
                qualified = self.ib.qualifyContracts(contract)
                if qualified:
                    contract = qualified[0]
                    self._contract_cache[symbol] = contract
                    _log("qualify_ok", symbol=symbol, con_id=getattr(contract, "conId", 0))
                    try:
                        con_id = int(getattr(contract, "conId", 0) or 0)
                        if con_id > 0:
                            self._set_cached_conid(symbol, con_id)
                    except Exception:
                        pass
                else:
                    _log("qualify_empty", symbol=symbol, sec_type=getattr(contract, "secType", ""))
        except Exception as exc:
            err = str(exc).strip()
            _log("qualify_error", symbol=symbol, error=err or repr(exc))
        if int(getattr(contract, "conId", 0) or 0) <= 0:
            details = self._get_contract_details(symbol, contract)
            if details and getattr(details, "contract", None) is not None:
                contract = details.contract
                self._contract_cache[symbol] = contract
                _log("contract_details_ok", symbol=symbol, con_id=getattr(contract, "conId", 0))
                try:
                    con_id = int(getattr(contract, "conId", 0) or 0)
                    if con_id > 0:
                        self._set_cached_conid(symbol, con_id)
                except Exception:
                    pass
            else:
                _log("contract_details_empty", symbol=symbol)
        try:
            _log(
                "secdef_start",
                symbol=symbol,
                underlying_symbol=str(getattr(contract, "symbol", symbol)),
                sec_type=str(getattr(contract, "secType", "STK")),
                con_id=int(getattr(contract, "conId", 0) or 0),
            )
            prev_timeout = getattr(self.ib, "RequestTimeout", None)
            try:
                # Keep this tight; if it fails we fall back to the persisted snapshot.
                self.ib.RequestTimeout = 8
            except Exception:
                prev_timeout = None
            try:
                params_list = self.ib.reqSecDefOptParams(
                    underlyingSymbol=str(getattr(contract, "symbol", symbol)),
                    futFopExchange="",
                    underlyingSecType=str(getattr(contract, "secType", "STK")),
                    underlyingConId=int(getattr(contract, "conId", 0) or 0),
                )
            finally:
                if prev_timeout is not None:
                    try:
                        self.ib.RequestTimeout = prev_timeout
                    except Exception:
                        pass
        except Exception as exc:
            _log("secdef_error", symbol=symbol, error=repr(exc))
            if snap is not None:
                _log("secdef_cache_hit", symbol=symbol, asof=str(self._secdef_snapshots.get(symbol, {}).get("asof", "")))
                self._secdef_cache[symbol] = [snap]
                return [snap]
            return None
        if not params_list:
            _log("secdef_empty", symbol=symbol)
            if snap is not None:
                _log("secdef_cache_hit", symbol=symbol, asof=str(self._secdef_snapshots.get(symbol, {}).get("asof", "")))
                self._secdef_cache[symbol] = [snap]
                return [snap]
            return None
        _log("secdef_ok", symbol=symbol, chains=len(params_list))
        try:
            best = None
            for c in params_list:
                if getattr(c, "exchange", "") == "SMART":
                    best = c
                    break
            if best is None:
                best = params_list[0]
            if best is not None:
                self._set_secdef_snapshot(symbol, best)
        except Exception:
            pass
        self._secdef_cache[symbol] = params_list
        return params_list

    def fetch_bars_1m(
        self,
        symbol: str,
        lookback_min: int,
        use_rth: bool,
        bar_size: str,
        what_to_show: str,
        timeout_s: float = 8,
    ) -> tuple[list[dict[str, Any]] | None, str | None]:
        self._ensure_thread()
        _log(
            "bars_start",
            symbol=symbol,
            lookback_min=lookback_min,
            use_rth=use_rth,
            bar_size=bar_size,
            what_to_show=what_to_show,
            timeout_s=timeout_s,
        )
        return self._fetch_bars_1m(
            symbol=symbol,
            lookback_min=lookback_min,
            use_rth=use_rth,
            bar_size=bar_size,
            what_to_show=what_to_show,
            allow_retry=True,
            timeout_s=timeout_s,
        )

    def _fetch_bars_1m(
        self,
        *,
        symbol: str,
        lookback_min: int,
        use_rth: bool,
        bar_size: str,
        what_to_show: str,
        allow_retry: bool,
        timeout_s: float,
    ) -> tuple[list[dict[str, Any]] | None, str | None]:
        if not self.is_connected():
            _log("bars_error", symbol=symbol, error="not_connected")
            return None, "IBKR not connected"

        contract = self._get_contract(symbol)

        duration = _duration_str_minutes(lookback_min, bar_size=bar_size)
        what_to_show = _what_to_show_for_contract(contract, what_to_show)
        start = time.perf_counter()
        try:
            self.drain_errors()
            _log(
                "bars_request",
                symbol=symbol,
                sec_type=getattr(contract, "secType", ""),
                exchange=getattr(contract, "exchange", ""),
                duration=duration,
                bar_size=bar_size,
                what_to_show=what_to_show,
                use_rth=use_rth,
                timeout_s=timeout_s,
            )
            bars = self._req_historical_data(
                contract=contract,
                end_date_time="",
                duration=duration,
                bar_size=bar_size,
                what_to_show=what_to_show,
                use_rth=use_rth,
                timeout_s=timeout_s,
            )
            if not bars:
                elapsed = round(time.perf_counter() - start, 3)
                _log("bars_ok", symbol=symbol, count=0, elapsed_s=elapsed)
                if allow_retry and lookback_min > 2:
                    _log("bars_empty_retry", symbol=symbol, from_lookback_min=lookback_min, to_lookback_min=2)
                    return self._fetch_bars_1m(
                        symbol=symbol,
                        lookback_min=2,
                        use_rth=False,
                        bar_size=bar_size,
                        what_to_show="MIDPOINT",
                        allow_retry=False,
                        timeout_s=min(timeout_s, 5),
                    )
                return [], None
            records = _bars_to_records(bars)
            last_time = records[-1]["time"] if records else None
            elapsed = round(time.perf_counter() - start, 3)
            _log("bars_ok", symbol=symbol, count=len(records), last_time=last_time, elapsed_s=elapsed)
            return records, None
        except Exception as exc:  # noqa: BLE001
            err_msg = str(exc).strip()
            if not err_msg:
                err_msg = f"{type(exc).__name__} {repr(exc)}"
            err = f"HistoricalData failed: {err_msg}"
            elapsed = round(time.perf_counter() - start, 3)
            _log(
                "bars_error",
                symbol=symbol,
                error=err,
                error_type=type(exc).__name__,
                error_repr=repr(exc),
                elapsed_s=elapsed,
            )
            if allow_retry and ("Timeout" in err or "cancelled" in err.lower()):
                return self._fetch_bars_1m(
                    symbol=symbol,
                    lookback_min=min(lookback_min, 2),
                    use_rth=False,
                    bar_size=bar_size,
                    what_to_show="MIDPOINT",
                    allow_retry=False,
                    timeout_s=min(timeout_s, 5),
                )
            return None, err

    def _req_historical_data(
        self,
        *,
        contract,
        end_date_time: str,
        duration: str,
        bar_size: str,
        what_to_show: str,
        use_rth: bool,
        timeout_s: float,
    ):
        return self.ib.reqHistoricalData(
            contract,
            endDateTime=end_date_time,
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow=what_to_show,
            useRTH=use_rth,
            formatDate=1,
            keepUpToDate=False,
            timeout=timeout_s,
        )

    def _req_mktdata_batch(
        self,
        contracts: list[Any],
        *,
        wait_sec: float = 0.8,
        generic_ticks: str = "",
        want_greeks: bool = False,
        want_implied_vol: bool = False,
    ) -> list[Any]:
        tickers: list[Any] = []
        if not contracts:
            return tickers
        for c in contracts:
            try:
                tk = self.ib.reqMktData(c, genericTickList=str(generic_ticks or ""), snapshot=False, regulatorySnapshot=False)
                tickers.append(tk)
            except Exception:
                tickers.append(None)
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
                    greeks = (
                        getattr(tk, "modelGreeks", None)
                        or getattr(tk, "lastGreeks", None)
                        or getattr(tk, "bidGreeks", None)
                        or getattr(tk, "askGreeks", None)
                    )
                    implied_vol = getattr(greeks, "impliedVol", None) if greeks is not None else None
                    delta = getattr(greeks, "delta", None) if greeks is not None else None
                    stock_iv = getattr(tk, "impliedVolatility", None)

                    has_price = (bid and bid > 0 and ask and ask > 0) or (last and last > 0)
                    has_greeks = bool(want_greeks and delta is not None and implied_vol is not None and _is_valid_price(implied_vol))
                    has_iv = bool(want_implied_vol and _is_valid_price(_as_float(stock_iv)))

                    if has_price or has_greeks or has_iv:
                        ready += 1
                if ready >= max(1, len([t for t in tickers if t])):
                    break
                self.ib.sleep(0.1)
        except Exception:
            pass
        finally:
            for c in contracts:
                try:
                    self.ib.cancelMktData(c)
                except Exception:
                    continue
        return tickers

    def _maybe_fallback_to_delayed(self, *, reason: str) -> bool:
        current = int(getattr(self, "_market_data_type", 1) or 1)
        if current != 1:
            return False
        _log("market_data_type_fallback", from_type=1, to_type=3, reason=reason)
        self._switch_market_data_type(3)
        return True

    def _has_option_price(self, tk: Any | None) -> bool:
        if tk is None:
            return False
        bid = _as_float(getattr(tk, "bid", None))
        ask = _as_float(getattr(tk, "ask", None))
        last = _as_float(getattr(tk, "last", None))
        return bool((bid and ask and bid > 0 and ask > 0) or (last and last > 0))

    def _fetch_underlying_price_mktdata(self, symbol: str, *, timeout_s: float = 2.0) -> tuple[float | None, str | None]:
        if not self.is_connected():
            return None, "IBKR not connected"
        contract = _contract_for_symbol(symbol)
        _log("underlying_mktdata_start", symbol=symbol, timeout_s=timeout_s)

        start = time.perf_counter()
        last_err: str | None = None
        self.drain_errors()
        for attempt in (1, 2):
            try:
                wait = float(timeout_s) if attempt == 1 else max(1.2, float(timeout_s) * 1.8)
                ticker = self.ib.reqMktData(contract, "", False, False)
                try:
                    self.ib.sleep(wait)
                    bid = _as_float(getattr(ticker, "bid", None))
                    ask = _as_float(getattr(ticker, "ask", None))
                    last = _as_float(getattr(ticker, "last", None))
                    price = None
                    if bid and ask and bid > 0 and ask > 0:
                        price = (bid + ask) / 2.0
                    elif last and last > 0:
                        price = last
                    else:
                        try:
                            price = float(ticker.marketPrice())
                        except Exception:
                            price = None
                    _log(
                        "underlying_mktdata_values",
                        symbol=symbol,
                        bid=bid,
                        ask=ask,
                        last=last,
                        market_price=price,
                    )
                finally:
                    try:
                        self.ib.cancelMktData(contract)
                    except Exception:
                        pass

                events = self.drain_errors()
                if self._saw_error_code(events, 354) and int(getattr(self, "_market_data_type", 1) or 1) == 1:
                    _log("market_data_type_fallback", from_type=1, to_type=3, reason="error_354_not_subscribed")
                    self._switch_market_data_type(3)
                    continue
                if self._saw_error_code(events, 2119):
                    _log("market_data_farm_connecting", symbol=symbol, code=2119)

                if _is_valid_price(_as_float(price)):
                    _log(
                        "underlying_mktdata_ok",
                        symbol=symbol,
                        price=round(float(price), 4),
                        attempt=attempt,
                        elapsed_s=round(time.perf_counter() - start, 3),
                    )
                    try:
                        self._spot_cache[symbol.upper().strip()] = (time.time() + self._spot_cache_ttl_s, float(price))
                    except Exception:
                        pass
                    return float(price), None
                last_err = "Underlying price failed: missing bid/ask/last"
            except Exception as exc:  # noqa: BLE001
                last_err = f"Underlying price failed: {exc}"
                _log("underlying_mktdata_error", symbol=symbol, attempt=attempt, error=repr(exc))

        return None, last_err or "Underlying price failed"

    def _fetch_underlying_price_for_options(self, symbol: str) -> tuple[float | None, str | None]:
        sym = symbol.upper().strip()
        cached = self._spot_cache.get(sym)
        if cached is not None:
            expires_at, px = cached
            if time.time() < float(expires_at) and _is_valid_price(_as_float(px)):
                _log("underlying_spot_cache_hit", symbol=sym, price=round(float(px), 4))
                return float(px), None
        px, err = self._fetch_underlying_price_mktdata(symbol, timeout_s=2.0)
        if _is_valid_price(px):
            return px, None
        # As a last resort, fall back to the most recent intraday bar close (still observable, tagged via logs).
        bars, bars_err = self._fetch_bars_1m(
            symbol=symbol,
            lookback_min=5,
            use_rth=False,
            bar_size="1 min",
            what_to_show="TRADES",
            allow_retry=True,
            timeout_s=12.0,
        )
        if bars_err:
            return None, err or bars_err
        if not bars:
            return None, err or "Underlying price fallback failed: no bars"
        close_px = _as_float(bars[-1].get("close"))
        if not _is_valid_price(close_px):
            return None, err or "Underlying price fallback failed: invalid close"
        _log("underlying_price_fallback_ok", symbol=symbol, price=round(float(close_px), 4), source="bars_1m")
        return float(close_px), None

    def _atm_option_metrics(self, symbol: str) -> tuple[dict[str, Any] | None, str | None]:
        from ib_insync import Option

        contract = self._get_contract(symbol, ensure_qualify=True)
        spot, spot_err = self._fetch_underlying_price_for_options(symbol)
        if spot_err or not _is_valid_price(spot):
            return None, f"ATM metrics failed: underlying price unavailable ({spot_err or 'invalid'})"

        params_list = self._get_option_chain_params(symbol, contract)
        if params_list is None:
            return None, "ATM metrics failed: option chain params empty"

        chain = None
        for c in params_list:
            if getattr(c, "exchange", "") == "SMART":
                chain = c
                break
        if chain is None:
            chain = params_list[0]

        expirations = sorted(list(getattr(chain, "expirations", []) or []))
        strikes = sorted(list(getattr(chain, "strikes", []) or []))
        expiry = _choose_expiry(expirations)
        if not expiry:
            return None, "ATM metrics failed: no expiry found in DTE 25-45"
        if not strikes:
            return None, "ATM metrics failed: strikes empty"

        atm_strike = float(min(strikes, key=lambda s: abs(float(s) - float(spot))))
        exch = "SMART"
        multiplier = str(getattr(chain, "multiplier", "") or "")
        trading_class = str(getattr(chain, "tradingClass", "") or "")

        prev_timeout = getattr(self.ib, "RequestTimeout", None)
        qualify_timeout_s = 3

        def _mk(right: str, strike: float):
            return Option(
                symbol,
                expiry,
                float(strike),
                right,
                exch,
                currency="USD",
                multiplier=multiplier,
                tradingClass=trading_class,
            )

        # Keep it tight: we only need one workable ATM pair.
        probe_order = [atm_strike, atm_strike + 5.0, atm_strike - 5.0]

        chosen_strike = None
        call = None
        put = None
        tickers: list[Any] | None = None
        reconnected = False
        strike_set = {float(s) for s in strikes}
        for strike in probe_order:
            if float(strike) not in strike_set:
                continue
            c = _mk("C", float(strike))
            p = _mk("P", float(strike))
            _log("atm_metrics_mktdata_start", symbol=symbol, expiry=expiry, strike=float(strike))
            self.drain_errors()
            tks = self._req_mktdata_batch([c, p], wait_sec=1.0, generic_ticks="", want_greeks=False)
            events = self.drain_errors()
            if self._saw_error_code(events, 354):
                self._maybe_fallback_to_delayed(reason="error_354_not_subscribed_options_probe")
                tks = self._req_mktdata_batch([c, p], wait_sec=3.0, generic_ticks="", want_greeks=False)
            if not (self._has_option_price(tks[0] if tks else None) and self._has_option_price(tks[1] if tks and len(tks) > 1 else None)):
                if self._saw_error_code(events, 2119) or self._saw_error_code(events, 2108):
                    _log("options_farm_connecting_retry", symbol=symbol, expiry=expiry, strike=float(strike))
                try:
                    self.ib.sleep(0.8)
                except Exception:
                    pass
                tks = self._req_mktdata_batch([c, p], wait_sec=3.0, generic_ticks="", want_greeks=False)
            call_tk = tks[0] if tks else None
            put_tk = tks[1] if tks and len(tks) > 1 else None
            _log(
                "atm_metrics_quotes",
                symbol=symbol,
                expiry=expiry,
                strike=float(strike),
                call_bid=_as_float(getattr(call_tk, "bid", None)) if call_tk is not None else None,
                call_ask=_as_float(getattr(call_tk, "ask", None)) if call_tk is not None else None,
                call_last=_as_float(getattr(call_tk, "last", None)) if call_tk is not None else None,
                put_bid=_as_float(getattr(put_tk, "bid", None)) if put_tk is not None else None,
                put_ask=_as_float(getattr(put_tk, "ask", None)) if put_tk is not None else None,
                put_last=_as_float(getattr(put_tk, "last", None)) if put_tk is not None else None,
            )
            def _mid_from_ticker(tk) -> float | None:
                bid = _clean_price(getattr(tk, "bid", None))
                ask = _clean_price(getattr(tk, "ask", None))
                last = _clean_price(getattr(tk, "last", None))
                if bid is not None and ask is not None and ask > bid:
                    return (bid + ask) / 2.0
                if last is not None:
                    return last
                return None
            call_mid = _mid_from_ticker(call_tk) if call_tk is not None else None
            put_mid = _mid_from_ticker(put_tk) if put_tk is not None else None
            if call_mid is None or put_mid is None:
                # Some sessions return NaN until contracts are qualified; do a quick qualify+retry.
                try:
                    self.ib.RequestTimeout = qualify_timeout_s
                except Exception:
                    pass
                try:
                    qc, qp = _mk("C", float(strike)), _mk("P", float(strike))
                    qualified = self.ib.qualifyContracts(qc, qp)
                except Exception as exc:
                    _log("atm_metrics_qualify_retry_error", symbol=symbol, expiry=expiry, strike=float(strike), error=repr(exc))
                    qualified = []
                finally:
                    if prev_timeout is not None:
                        try:
                            self.ib.RequestTimeout = prev_timeout
                        except Exception:
                            pass
                if len(qualified) >= 2:
                    _log("atm_metrics_qualify_retry_ok", symbol=symbol, expiry=expiry, strike=float(strike))
                    c, p = qualified[0], qualified[1]
                    tks = self._req_mktdata_batch([c, p], wait_sec=2.5, generic_ticks="", want_greeks=False)
                    call_tk = tks[0] if tks else None
                    put_tk = tks[1] if tks and len(tks) > 1 else None
                    call_mid = _mid_from_ticker(call_tk) if call_tk is not None else None
                    put_mid = _mid_from_ticker(put_tk) if put_tk is not None else None
            if call_mid is None or put_mid is None:
                # If we still only see NaN, it's often a stale/broken session; reconnect once and retry.
                if (not reconnected) and (_has_nan_prices(call_tk) or _has_nan_prices(put_tk)):
                    ok, _ = self._reconnect(reason="options_quotes_nan")
                    reconnected = bool(ok)
                    if ok:
                        self.drain_errors()
                        tks = self._req_mktdata_batch([c, p], wait_sec=3.0, generic_ticks="", want_greeks=False)
                        call_tk = tks[0] if tks else None
                        put_tk = tks[1] if tks and len(tks) > 1 else None
                        call_mid = _mid_from_ticker(call_tk) if call_tk is not None else None
                        put_mid = _mid_from_ticker(put_tk) if put_tk is not None else None
            if call_mid is not None and put_mid is not None:
                chosen_strike = float(strike)
                call = c
                put = p
                tickers = tks
                break

        if chosen_strike is None or tickers is None or call is None or put is None:
            return None, "ATM metrics failed: option quotes unavailable (no bid/ask/last)"

        events = self.drain_errors()
        if self._saw_error_code(events, 354):
            self._maybe_fallback_to_delayed(reason="error_354_not_subscribed_options")
            tickers = self._req_mktdata_batch([call, put], wait_sec=3.0, generic_ticks="", want_greeks=False)
        if not tickers or all(t is None for t in tickers):
            return None, "ATM metrics failed: no option tickers"

        call_tk = tickers[0]
        put_tk = tickers[1] if len(tickers) > 1 else None
        def _mid_from_ticker(tk) -> float | None:
            bid = _as_float(getattr(tk, "bid", None))
            ask = _as_float(getattr(tk, "ask", None))
            last = _as_float(getattr(tk, "last", None))
            if bid and ask and bid > 0 and ask > 0:
                return (bid + ask) / 2.0
            if last and last > 0:
                return last
            return None
        call_mid = _mid_from_ticker(call_tk) if call_tk is not None else None
        put_mid = _mid_from_ticker(put_tk) if put_tk is not None else None
        if call_mid is None or put_mid is None:
            return None, "ATM metrics failed: missing option prices"

        try:
            expiry_dt = datetime.strptime(expiry, "%Y%m%d").replace(hour=16, minute=0, second=0, tzinfo=_ET)
            now = datetime.now(_ET)
            seconds = (expiry_dt - now).total_seconds()
            if seconds <= 0:
                return None, "ATM metrics failed: expiry already passed"
            t_years = seconds / 31557600.0
        except Exception:
            return None, "ATM metrics failed: expiry parse error"

        call_iv = _implied_vol_bisect(float(call_mid), float(spot), float(chosen_strike), t_years, 0.0, 0.0, "C")
        put_iv = _implied_vol_bisect(float(put_mid), float(spot), float(chosen_strike), t_years, 0.0, 0.0, "P")
        if call_iv is None or put_iv is None:
            return None, "ATM metrics failed: implied vol calc failed"

        spread_pcts: list[float] = []
        for tk in (call_tk, put_tk):
            bid = _as_float(getattr(tk, "bid", None)) if tk is not None else None
            ask = _as_float(getattr(tk, "ask", None)) if tk is not None else None
            if bid and ask and bid > 0 and ask > 0 and ask > bid:
                mid = (bid + ask) / 2.0
                if mid > 0:
                    spread_pcts.append((ask - bid) / mid * 100.0)
        spread_pct = float(sum(spread_pcts) / len(spread_pcts)) if spread_pcts else None

        out = {
            "spot": float(spot),
            "expiry": expiry,
            "strike": float(chosen_strike),
            "call_mid": float(call_mid),
            "put_mid": float(put_mid),
            "call_iv": float(call_iv),
            "put_iv": float(put_iv),
            "atm_iv": float((float(call_iv) + float(put_iv)) / 2.0),
            "atm_spread_pct": float(spread_pct) if spread_pct is not None else None,
        }
        _log(
            "atm_metrics_ok",
            symbol=symbol,
            spot=round(out["spot"], 4),
            expiry=expiry,
            strike=out["strike"],
            atm_iv=round(out["atm_iv"], 4),
            spread_pct=round(out["atm_spread_pct"], 3) if out["atm_spread_pct"] is not None else None,
        )
        return out, None

    def _warm_iv_range_1y(self, symbol: str) -> tuple[float | None, float | None, str | None]:
        if not self.is_connected():
            return None, None, "IBKR not connected"
        symbol = symbol.upper().strip()
        today = datetime.now(_ET).date().isoformat()
        cached = self._ivr_range_cache.get(symbol)
        if cached and cached.get("date") == today and cached.get("low") is not None and cached.get("high") is not None:
            return float(cached["low"]), float(cached["high"]), None

        # IV history (OPTION_IMPLIED_VOLATILITY) works without conId; avoid secdef qualify here.
        contract = _contract_for_symbol(symbol)
        _log("ivr_range_warm_start", symbol=symbol, date=today)
        start = time.perf_counter()
        prev_timeout = getattr(self.ib, "RequestTimeout", None)
        try:
            self.ib.RequestTimeout = max(int(prev_timeout or 10), 30)
        except Exception:
            prev_timeout = None
        try:
            bars = self.ib.reqHistoricalData(
                contract,
                endDateTime="",
                durationStr="1 Y",
                barSizeSetting="1 day",
                whatToShow="OPTION_IMPLIED_VOLATILITY",
                useRTH=True,
                formatDate=1,
                keepUpToDate=False,
                timeout=30,
            )
            values = [float(getattr(b, "close", 0.0) or 0.0) for b in (bars or [])]
            values = [v for v in values if v > 0]
            if len(values) < 5:
                return None, None, "IVR range warm failed: insufficient IV history"
            low = float(min(values))
            high = float(max(values))
            self._ivr_range_cache[symbol] = {"date": today, "low": low, "high": high, "n": len(values)}
            self._save_ivr_range_cache()
            _log(
                "ivr_range_warm_ok",
                symbol=symbol,
                low=round(low, 4),
                high=round(high, 4),
                n=len(values),
                elapsed_s=round(time.perf_counter() - start, 3),
            )
            return low, high, None
        except Exception as exc:  # noqa: BLE001
            _log("ivr_range_warm_error", symbol=symbol, error=repr(exc))
            prev = self._ivr_range_cache.get(symbol)
            if prev and prev.get("low") is not None and prev.get("high") is not None and float(prev["high"]) > float(prev["low"]):
                _log("ivr_range_warm_stale", symbol=symbol, asof=str(prev.get("date") or ""))
                return float(prev["low"]), float(prev["high"]), None
            return None, None, f"IVR range warm failed: {exc}"
        finally:
            if prev_timeout is not None:
                try:
                    self.ib.RequestTimeout = prev_timeout
                except Exception:
                    pass

    def _fetch_current_iv_mktdata(self, symbol: str, *, timeout_s: float = 2.0) -> tuple[float | None, str | None]:
        if not self.is_connected():
            return None, "IBKR not connected"
        contract = _contract_for_symbol(symbol)
        _log("current_iv_start", symbol=symbol, timeout_s=timeout_s)
        start = time.perf_counter()
        try:
            tickers = self._req_mktdata_batch(
                [contract],
                wait_sec=float(timeout_s),
                generic_ticks="106",
                want_implied_vol=True,
            )
            tk = tickers[0] if tickers else None
            iv = _as_float(getattr(tk, "impliedVolatility", None)) if tk is not None else None
            if iv is None or not _is_valid_price(iv):
                return None, "Current IV missing from mktdata (impliedVolatility)"
            _log("current_iv_ok", symbol=symbol, iv=round(float(iv), 4), elapsed_s=round(time.perf_counter() - start, 3))
            return float(iv), None
        except Exception as exc:  # noqa: BLE001
            _log("current_iv_error", symbol=symbol, error=repr(exc))
            return None, f"Current IV failed: {exc}"

    def fetch_ivr(self, symbol: str) -> tuple[float | None, str | None]:
        self._ensure_thread()
        self._ensure_event_loop()
        if not self.is_connected():
            _log("ivr_error", symbol=symbol, error="not_connected")
            return None, "IBKR not connected"

        try:
            low, high, range_err = self._warm_iv_range_1y(symbol)
            if range_err:
                return None, f"IVR failed: {range_err}"
            if low is None or high is None or high <= low:
                return None, "IVR failed: invalid IV range"

            current_iv, cur_err = self._fetch_current_iv_mktdata(symbol, timeout_s=2.0)
            if cur_err or current_iv is None or not _is_valid_price(current_iv):
                metrics, metrics_err = self._atm_option_metrics(symbol)
                if metrics_err or not metrics:
                    return None, f"IVR failed: {metrics_err or cur_err or 'current_iv missing'}"
                current_iv = _as_float(metrics.get("atm_iv"))
                if current_iv is None or not _is_valid_price(current_iv):
                    return None, "IVR failed: current ATM IV missing"

            ivr = (float(current_iv) - float(low)) / (float(high) - float(low)) * 100.0
            ivr = max(0.0, min(100.0, float(ivr)))
            _log(
                "ivr_ok",
                symbol=symbol,
                ivr=round(ivr, 2),
                current_iv=round(float(current_iv), 4),
                low=round(float(low), 4),
                high=round(float(high), 4),
            )
            return round(ivr, 2), None
        except Exception as exc:  # noqa: BLE001
            err = f"IVR failed: {exc}"
            _log("ivr_error", symbol=symbol, error=err)
            return None, err

    def fetch_skew_value(self, symbol: str) -> tuple[float | None, str | None]:
        self._ensure_thread()
        self._ensure_event_loop()
        if not self.is_connected():
            _log("rr25_error", symbol=symbol, error="not_connected")
            return None, "IBKR not connected"

        from ib_insync import Option

        contract = self._get_contract(symbol, ensure_qualify=True)

        underlying_px, px_err = self._fetch_underlying_price_for_options(symbol)
        if px_err or not _is_valid_price(underlying_px):
            return None, f"RR25 failed: underlying price unavailable ({px_err or 'invalid'})"

        params_list = self._get_option_chain_params(symbol, contract)
        if params_list is None:
            return None, "RR25 failed: option chain params empty"

        chain = None
        for c in params_list:
            if getattr(c, "exchange", "") == "SMART":
                chain = c
                break
        if chain is None:
            chain = params_list[0]

        expirations = sorted(list(getattr(chain, "expirations", []) or []))
        strikes = sorted(list(getattr(chain, "strikes", []) or []))
        expiry = _choose_expiry(expirations)
        if not expiry:
            return None, "RR25 failed: no expiry found in DTE 25-45"
        if not strikes:
            return None, "RR25 failed: strikes empty"

        atm_strike = float(min(strikes, key=lambda s: abs(float(s) - float(underlying_px))))
        target_frac = round(atm_strike % 1.0, 2)
        strikes_sorted = sorted([float(s) for s in strikes if round(float(s) % 1.0, 2) == target_frac])
        if not strikes_sorted:
            return None, "RR25 failed: strikes filtered empty"
        atm_idx = min(range(len(strikes_sorted)), key=lambda i: abs(strikes_sorted[i] - float(atm_strike)))
        window = 5
        call_strikes = strikes_sorted[atm_idx : min(len(strikes_sorted), atm_idx + window)]
        put_strikes = strikes_sorted[max(0, atm_idx - window) : atm_idx + 1]

        exch = "SMART"
        multiplier = str(getattr(chain, "multiplier", "") or "")
        trading_class = str(getattr(chain, "tradingClass", "") or "")

        def _mk(right: str, strike: float):
            return Option(
                symbol,
                expiry,
                float(strike),
                right,
                exch,
                currency="USD",
                multiplier=multiplier,
                tradingClass=trading_class,
            )

        call_contracts = [_mk("C", s) for s in call_strikes]
        put_contracts = [_mk("P", s) for s in put_strikes]

        # Strikes returned by SecDef are not guaranteed to exist for every expiry; qualify quickly to filter.
        qualified = list(call_contracts + put_contracts)
        prev_timeout = getattr(self.ib, "RequestTimeout", None)
        try:
            self.ib.RequestTimeout = 3
        except Exception:
            prev_timeout = None
        try:
            filtered = self.ib.qualifyContracts(*qualified)
            if filtered:
                qualified = list(filtered)
        except Exception:
            pass
        finally:
            if prev_timeout is not None:
                try:
                    self.ib.RequestTimeout = prev_timeout
                except Exception:
                    pass
        if not qualified:
            return None, "RR25 failed: no option contracts qualified"

        try:
            _log(
                "rr25_tickers_request",
                symbol=symbol,
                expiry=expiry,
                calls=sum(1 for c in qualified if getattr(c, "right", "") == "C"),
                puts=sum(1 for c in qualified if getattr(c, "right", "") == "P"),
            )
            tickers = self._req_mktdata_batch(qualified, wait_sec=2.0, generic_ticks="", want_greeks=False)
            events = self.drain_errors()
            if self._saw_error_code(events, 354):
                self._maybe_fallback_to_delayed(reason="error_354_not_subscribed_options_rr25")
                tickers = self._req_mktdata_batch(qualified, wait_sec=2.5, generic_ticks="", want_greeks=False)
            elif self._saw_error_code(events, 2119) or self._saw_error_code(events, 2108):
                _log("options_farm_connecting_retry", symbol=symbol, expiry=expiry, strikes=len(qualified))
                try:
                    self.ib.sleep(0.8)
                except Exception:
                    pass
                tickers = self._req_mktdata_batch(qualified, wait_sec=4.0, generic_ticks="", want_greeks=False)
        except Exception as exc:  # noqa: BLE001
            return None, f"RR25 failed: reqMktData error ({exc})"

        try:
            priced = sum(1 for tk in tickers if self._has_option_price(tk))
            nan_count = sum(1 for tk in tickers if _has_nan_prices(tk))
        except Exception:
            priced = 0
            nan_count = 0
        _log("rr25_quotes", symbol=symbol, expiry=expiry, tickers=len(tickers), priced=priced, nan=nan_count)
        if priced == 0 and nan_count > 0:
            ok, _ = self._reconnect(reason="rr25_quotes_nan")
            if ok:
                tickers = self._req_mktdata_batch(qualified, wait_sec=3.0, generic_ticks="", want_greeks=False)
                try:
                    priced = sum(1 for tk in tickers if self._has_option_price(tk))
                except Exception:
                    priced = 0
                _log("rr25_quotes_retry", symbol=symbol, expiry=expiry, tickers=len(tickers), priced=priced)

        def _delta_iv(contract_obj, ticker_obj) -> tuple[float | None, float | None]:
            bid = _as_float(getattr(ticker_obj, "bid", None))
            ask = _as_float(getattr(ticker_obj, "ask", None))
            last = _as_float(getattr(ticker_obj, "last", None))
            if bid and ask and bid > 0 and ask > 0:
                option_price = (bid + ask) / 2.0
            elif last and last > 0:
                option_price = last
            else:
                return None, None
            try:
                expiry_dt = datetime.strptime(expiry, "%Y%m%d").replace(hour=16, minute=0, second=0, tzinfo=_ET)
                now = datetime.now(_ET)
                seconds = (expiry_dt - now).total_seconds()
                if seconds <= 0:
                    return None, None
                t_years = seconds / 31557600.0
            except Exception:
                return None, None
            right = str(getattr(contract_obj, "right", "") or "").upper()
            if right not in {"C", "P"}:
                return None, None
            iv = _implied_vol_bisect(
                float(option_price),
                float(underlying_px),
                float(getattr(contract_obj, "strike", 0.0) or 0.0),
                t_years,
                0.0,
                0.0,
                right,
            )
            if iv is None:
                return None, None
            delta, _, _, _ = _bs_greeks(
                float(underlying_px),
                float(getattr(contract_obj, "strike", 0.0) or 0.0),
                t_years,
                0.0,
                0.0,
                float(iv),
                right,
            )
            return delta, float(iv)

        best_call = None
        best_put = None

        greeks_count = 0
        for tk in tickers:
            c = getattr(tk, "contract", None)
            if c is None:
                continue
            right = str(getattr(c, "right", "") or "")
            strike = float(getattr(c, "strike", 0.0) or 0.0)
            delta, iv = _delta_iv(c, tk)
            if delta is None or iv is None or iv <= 0:
                continue
            greeks_count += 1
            if right == "C":
                dist = abs(delta - 0.25)
                if best_call is None or dist < best_call["dist"]:
                    best_call = {"dist": dist, "delta": delta, "iv": iv, "strike": strike}
            elif right == "P":
                dist = abs(delta - (-0.25))
                if best_put is None or dist < best_put["dist"]:
                    best_put = {"dist": dist, "delta": delta, "iv": iv, "strike": strike}

        if not best_call or not best_put:
            _log(
                "rr25_missing_quotes",
                symbol=symbol,
                expiry=expiry,
                tickers=len(tickers),
                greeks=greeks_count,
            )
            return None, "RR25 failed: option quotes unavailable (no usable prices to infer IV/delta)"

        rr25 = (float(best_put["iv"]) - float(best_call["iv"])) * 100.0
        rr25 = round(rr25, 2)
        _log(
            "rr25_ok",
            symbol=symbol,
            expiry=expiry,
            rr25=rr25,
            call_strike=best_call["strike"],
            call_delta=round(best_call["delta"], 3),
            call_iv=round(best_call["iv"], 4),
            put_strike=best_put["strike"],
            put_delta=round(best_put["delta"], 3),
            put_iv=round(best_put["iv"], 4),
        )
        return rr25, None

    def fetch_atm_option_spread_pct(self, symbol: str) -> tuple[float | None, str | None]:
        self._ensure_thread()
        self._ensure_event_loop()
        if not self.is_connected():
            _log("atm_spread_error", symbol=symbol, error="not_connected")
            return None, "IBKR not connected"

        metrics, err = self._atm_option_metrics(symbol)
        if err or not metrics:
            return None, f"ATM spread failed: {err or 'ATM metrics missing'}"
        spread_pct = _as_float(metrics.get("atm_spread_pct"))
        if spread_pct is None:
            return None, "ATM spread failed: bid/ask missing"
        spread_pct = round(float(spread_pct), 3)
        _log(
            "atm_spread_ok",
            symbol=symbol,
            expiry=str(metrics.get("expiry") or ""),
            strike=str(metrics.get("strike") or ""),
            spread_pct=spread_pct,
        )
        return spread_pct, None

    def fetch_jumpy(self, symbol: str) -> tuple[float | None, str | None]:
        self._ensure_thread()
        self._ensure_event_loop()
        if not self.is_connected():
            _log("jumpy_error", symbol=symbol, error="not_connected")
            return None, "IBKR not connected"

        contract = self._get_contract(symbol)

        try:
            px_bars = self.ib.reqHistoricalData(
                contract,
                endDateTime="",
                durationStr="1 D",
                barSizeSetting="1 min",
                whatToShow="TRADES",
                useRTH=True,
                formatDate=1,
                keepUpToDate=False,
                timeout=12,
            )
        except Exception as exc:  # noqa: BLE001
            return None, f"Jumpy failed: price bars error ({exc})"

        if not px_bars:
            return None, "Jumpy failed: price bars empty"

        try:
            price_open = float(getattr(px_bars[0], "open", 0.0) or 0.0)
            price_now = float(getattr(px_bars[-1], "close", 0.0) or 0.0)
        except Exception:
            return None, "Jumpy failed: price bars parse error"
        if price_open <= 0 or price_now <= 0:
            return None, "Jumpy failed: invalid price bars"

        try:
            iv_bars = self.ib.reqHistoricalData(
                contract,
                endDateTime="",
                durationStr="1 D",
                barSizeSetting="1 min",
                whatToShow="OPTION_IMPLIED_VOLATILITY",
                useRTH=True,
                formatDate=1,
                keepUpToDate=False,
                timeout=12,
            )
        except Exception as exc:  # noqa: BLE001
            return None, f"Jumpy failed: IV bars error ({exc})"

        if not iv_bars:
            return None, "Jumpy failed: IV bars empty"

        try:
            iv_open = float(getattr(iv_bars[0], "close", 0.0) or 0.0)
            iv_now = float(getattr(iv_bars[-1], "close", 0.0) or 0.0)
        except Exception:
            return None, "Jumpy failed: IV bars parse error"
        if iv_open <= 0 or iv_now <= 0:
            return None, "Jumpy failed: invalid IV bars"

        price_move_pct = abs((price_now - price_open) / price_open) * 100.0
        iv_move_pts = abs(iv_now - iv_open) * 100.0
        jumpy = iv_move_pts / max(price_move_pct, 0.10)
        jumpy = round(float(jumpy), 3)
        _log(
            "jumpy_ok",
            symbol=symbol,
            price_open=round(price_open, 2),
            price_now=round(price_now, 2),
            iv_open=round(iv_open, 4),
            iv_now=round(iv_now, 4),
            price_move_pct=round(price_move_pct, 3),
            iv_move_pts=round(iv_move_pts, 3),
            jumpy=jumpy,
        )
        return jumpy, None

    def _qualify(self, contract) -> None:
        try:
            self.ib.qualifyContracts(contract)
        except Exception:
            pass


def _duration_str_minutes(lookback_min: int, *, bar_size: str | None = None) -> str:
    if lookback_min <= 0:
        return "60 S"
    if lookback_min <= 120:
        return f"{lookback_min * 60} S"
    days = max(1, int((lookback_min + 1439) / 1440))
    return f"{days} D"
