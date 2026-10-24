from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
import os
import platform
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import streamlit as st
import asyncio
import pandas as pd


GATEKEEPER_DIR = Path(__file__).resolve().parent
if str(GATEKEEPER_DIR) not in sys.path:
    sys.path.insert(0, str(GATEKEEPER_DIR))


from core.cache import GatekeeperCache, make_field_id  # noqa: E402
from core.engine import GatekeeperEngine  # noqa: E402
from core.resolver import GlobalInputs  # noqa: E402
from core.time_utils import now_iso  # noqa: E402
from core.types import ResolvedInput  # noqa: E402
from providers.ibkr import IBKRProvider  # noqa: E402


DEFAULT_CONFIG_PATH = GATEKEEPER_DIR / "config.json"


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _ui_log_path() -> Path:
    if "ui_log_path" not in st.session_state:
        log_dir = GATEKEEPER_DIR / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%S", time.localtime())
        session_id = str(st.session_state.get("ui_session_id") or "unknown")
        st.session_state.ui_log_path = str(log_dir / f"ui_{stamp}_{session_id}.jsonl")
    return Path(str(st.session_state.ui_log_path))


def _ui_logger() -> logging.Logger:
    session_id = str(st.session_state.get("ui_session_id") or "unknown")
    logger = logging.getLogger(f"gatekeeper.ui.{session_id}")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    log_path = _ui_log_path()
    for handler in list(logger.handlers):
        if isinstance(handler, logging.FileHandler) and getattr(handler, "baseFilename", None) == str(log_path):
            return logger

    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(str(log_path), maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    handler.setLevel(logging.INFO)
    logger.addHandler(handler)
    return logger


def _ui_event(event: str, *, level: str = "INFO", **data: Any) -> None:
    try:
        st.session_state.ui_seq = int(st.session_state.get("ui_seq", 0)) + 1
        payload = {
            "ts": now_iso(),
            "session_id": str(st.session_state.get("ui_session_id")),
            "seq": int(st.session_state.ui_seq),
            "event": str(event),
            "level": str(level),
            "data": data,
        }
        _ui_logger().info(_json_dumps(payload))
    except Exception:
        # Never break the UI due to audit logging failures.
        return


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _save_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _get_nested(config: dict[str, Any], path: list[str], default: Any = None) -> Any:
    cur: Any = config
    for part in path:
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _set_nested(config: dict[str, Any], path: list[str], value: Any) -> None:
    cur: Any = config
    for part in path[:-1]:
        if part not in cur or not isinstance(cur[part], dict):
            cur[part] = {}
        cur = cur[part]
    cur[path[-1]] = value


def _init_state() -> None:
    st.session_state.setdefault("ui_session_id", uuid.uuid4().hex[:12])
    st.session_state.setdefault("ui_seq", 0)
    os.environ["GATEKEEPER_UI_SESSION_ID"] = str(st.session_state.ui_session_id)
    if "config" not in st.session_state:
        if not DEFAULT_CONFIG_PATH.exists():
            st.error("Missing config.json. Expected: tool/gatekeeper/config.json")
            st.stop()
        st.session_state.config = _load_json(DEFAULT_CONFIG_PATH)
        st.session_state.config_path = str(DEFAULT_CONFIG_PATH)

    st.session_state.setdefault("cache", GatekeeperCache())
    st.session_state.setdefault("manual_overrides", {})  # field_id -> ResolvedInput
    st.session_state.setdefault("edit_fields", set())  # field_id currently editing override
    if "provider" not in st.session_state:
        _ensure_event_loop()
        try:
            st.session_state.provider = IBKRProvider()
        except RuntimeError:
            st.session_state.provider = None
    st.session_state.setdefault("profile_overrides", {})  # symbol -> profile
    st.session_state.setdefault("selected_symbols", {})  # symbol -> bool
    st.session_state.setdefault("run_results", {})  # symbol -> RunResult
    st.session_state.setdefault("last_cards", {})  # symbol -> SignalCard
    st.session_state.setdefault("last_cards_prev", {})  # symbol -> SignalCard
    st.session_state.setdefault("refresh_before_run", False)
    st.session_state.setdefault("last_refresh_status", None)
    st.session_state.setdefault("health_check", None)
    st.session_state.setdefault("last_connect_ts", None)
    st.session_state.setdefault("last_run_ts", None)
    st.session_state.setdefault("last_run_symbol", None)
    st.session_state.setdefault("last_saved_ts", None)
    st.session_state.setdefault("status_log", [])  # list of dicts
    if not st.session_state.get("ui_session_started"):
        st.session_state.ui_session_started = True
        _ui_event(
            "ui_session_start",
            config_path=str(st.session_state.get("config_path")),
            pid=os.getpid(),
            python=sys.version.split()[0],
            platform=platform.platform(),
            streamlit=getattr(st, "__version__", None),
        )


def _log_status(action: str, message: str, level: str = "INFO") -> None:
    entry = {"ts": now_iso(), "action": action, "level": level, "message": message}
    st.session_state.status_log.insert(0, entry)
    st.session_state.status_log = st.session_state.status_log[:50]
    _ui_event("status", action=action, message=message, level=level)


def _engine() -> GatekeeperEngine:
    if st.session_state.provider is None:
        st.error("IBKR provider failed to initialize (event loop). Use Connect to retry.")
        st.stop()
    return GatekeeperEngine(
        config=st.session_state.config,
        provider=st.session_state.provider,
        cache=st.session_state.cache,
        manual_overrides=st.session_state.manual_overrides,
    )


def _ensure_provider() -> None:
    if st.session_state.provider is not None:
        return
    _ensure_event_loop()
    try:
        st.session_state.provider = IBKRProvider()
    except RuntimeError as exc:
        st.session_state.last_refresh_status = {"ts": now_iso(), "errors": [str(exc)]}
        _log_status("connect", f"Provider init failed: {exc}", "ERROR")


def _ensure_connected(engine: GatekeeperEngine, *, action: str) -> bool:
    if engine.provider.is_connected():
        return True
    _log_status("connect", f"Auto-connect for {action}", "INFO")
    ok, msg = engine.resolver.connect_ibkr()
    if ok:
        st.session_state.last_connect_ts = now_iso()
        _log_status("connect", "Connected to IBKR", "INFO")
        return True
    _log_status("connect", msg, "ERROR")
    st.session_state.last_refresh_status = {"ts": now_iso(), "errors": [msg]}
    st.error(f"Connect failed: {msg}")
    return False


def _run_symbols(
    *,
    symbols: list[str],
    profile_map: dict[str, str],
    global_inputs: GlobalInputs,
    label: str,
) -> None:
    if not symbols:
        st.warning("No symbols selected.")
        return
    run_started = time.perf_counter()
    _ui_event(
        "run_start",
        label=label,
        symbols=list(symbols),
        refresh_before_run=bool(st.session_state.refresh_before_run),
        global_inputs={
            "anchor_symbol": global_inputs.anchor_symbol,
            "event_level": global_inputs.event_level,
            "event_confirmed": bool(global_inputs.event_confirmed),
            "bofa_9_3": global_inputs.bofa_9_3,
            "spread_width_usd": global_inputs.spread_width_usd,
        },
    )
    _log_status(
        "run_inputs",
        f"anchor={global_inputs.anchor_symbol} event_level={global_inputs.event_level} "
        f"event_confirmed={global_inputs.event_confirmed} bofa_9_3={global_inputs.bofa_9_3} "
        f"spread_width_usd={global_inputs.spread_width_usd}",
        "INFO",
    )
    if not _ensure_connected(engine, action=label):
        st.stop()
    if st.session_state.refresh_before_run:
        st.session_state.last_refresh_status = engine.refresh_data(anchor_symbol=global_inputs.anchor_symbol, tickers=symbols)
    for sym in symbols:
        sym_started = time.perf_counter()
        profile = profile_map.get(sym) or engine.default_profile_for_symbol(sym)
        _ui_event("run_symbol_start", label=label, symbol=sym, profile=profile)
        if sym in st.session_state.run_results:
            st.session_state.last_cards_prev[sym] = st.session_state.run_results[sym].card
        try:
            st.session_state.run_results[sym] = engine.run_symbol(
                symbol=sym, profile=profile, global_inputs=global_inputs, fetch_auto=True
            )
            st.session_state.last_cards[sym] = st.session_state.run_results[sym].card
            _ui_event(
                "run_symbol_end",
                label=label,
                symbol=sym,
                profile=profile,
                elapsed_s=round(time.perf_counter() - sym_started, 3),
                card=st.session_state.run_results[sym].card.to_dict(),
                steps=[s.to_dict() for s in st.session_state.run_results[sym].steps],
            )
        except Exception as exc:  # noqa: BLE001
            _ui_event(
                "run_symbol_error",
                level="ERROR",
                label=label,
                symbol=sym,
                profile=profile,
                elapsed_s=round(time.perf_counter() - sym_started, 3),
                error=str(exc),
            )
            _log_status(label, f"{sym}: run failed: {exc}", "ERROR")
            raise
    st.session_state.last_run_ts = now_iso()
    st.session_state.last_run_symbol = symbols[0] if len(symbols) == 1 else None
    _log_status(label, f"Ran {len(symbols)} symbols", "INFO")
    _ui_event("run_end", label=label, elapsed_s=round(time.perf_counter() - run_started, 3))


def _refresh_symbols(symbols: list[str]) -> None:
    st.session_state.last_refresh_status = _refresh_data_with_progress(engine, anchor_symbol, symbols)
    if st.session_state.last_refresh_status.get("errors"):
        _log_status(
            "refresh_data",
            "Errors: " + "; ".join(st.session_state.last_refresh_status["errors"]),
            "ERROR",
        )
    else:
        elapsed = st.session_state.last_refresh_status.get("elapsed_s", "?")
        _log_status("refresh_data", f"OK (elapsed {elapsed}s)", "INFO")


def _badge(source: str, is_lock: bool) -> str:
    if source == "MISSING":
        return "MISSING"
    if source == "MANUAL":
        return "MANUAL_LOCK" if is_lock else "MANUAL"
    return source


def _display_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.4g}"
    if isinstance(value, bool):
        return "YES" if value else "NO"
    if isinstance(value, (list, dict)):
        return f"<{type(value).__name__}>"
    return str(value)


def _style_missing(df: pd.DataFrame) -> pd.io.formats.style.Styler:
    def _row_style(row: pd.Series) -> list[str]:
        if str(row.get("source")) == "MISSING":
            return ["background-color: #ffe5e5"] * len(row)
        return ["" for _ in row]

    return df.style.apply(_row_style, axis=1)


def _ib_error_level(code: Any) -> str:
    try:
        code_i = int(code)
    except Exception:
        return "ERROR"

    # Common farm connectivity messages (noise, not actionable)
    if code_i in {2104, 2106, 2107, 2108, 2119, 2158}:
        return "INFO"

    # Common "no subscription / internal cleanup" noise when falling back to bars
    if code_i in {300, 354}:
        return "WARN"

    # Historical data cancelled / pacing-ish behavior: often recoverable via fallback/retry
    if code_i in {162}:
        return "WARN"

    return "ERROR"


def _get_effective_field(field_id: str) -> ResolvedInput:
    manual = st.session_state.manual_overrides.get(field_id)
    if manual is not None:
        return manual
    cached = st.session_state.cache.get(field_id)
    if cached is not None:
        return cached
    return ResolvedInput(value=None, source="MISSING", ts=now_iso(), is_manual_lock=False)


def _audit_rows(field_ids: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for fid in field_ids:
        try:
            ri = _get_effective_field(fid)
        except Exception:
            continue
        rows.append(
            {
                "field_id": fid,
                "value": _display_value(ri.value),
                "source": ri.source,
                "manual_lock": bool(ri.is_manual_lock),
                "ts": ri.ts,
            }
        )
    return rows


def _set_manual(field_id: str, value: Any) -> None:
    st.session_state.manual_overrides[field_id] = ResolvedInput(
        value=value, source="MANUAL", ts=now_iso(), is_manual_lock=True
    )
    _log_status("override_set", f"{field_id}={_display_value(value)}", "INFO")


def _clear_manual(field_id: str) -> None:
    st.session_state.manual_overrides.pop(field_id, None)
    st.session_state.edit_fields.discard(field_id)
    _log_status("override_clear", f"{field_id}", "INFO")


def _render_field_row(
    *,
    label: str,
    field_id: str,
    value_parser,
    input_placeholder: str | None = None,
) -> None:
    ri = _get_effective_field(field_id)

    cols = st.columns([2.2, 1.2, 1.2, 2.0, 3.4], vertical_alignment="center")
    cols[0].write(label)
    cols[1].code(_display_value(ri.value) or "-", language=None)
    cols[2].code(_badge(ri.source, ri.is_manual_lock), language=None)
    cols[3].write(ri.ts)

    action_container = cols[4]
    src = ri.source

    if src == "MISSING":
        raw = action_container.text_input(
            "value",
            value="",
            placeholder=input_placeholder,
            label_visibility="collapsed",
            key=f"input_{field_id}",
        )
        if action_container.button("Update", key=f"update_{field_id}"):
            parsed = value_parser(raw)
            if parsed is None:
                st.error(f"Invalid value for {label}")
            else:
                _set_manual(field_id, parsed)
                st.rerun()
        return

    if src == "MANUAL":
        raw = action_container.text_input(
            "value",
            value=_display_value(ri.value),
            placeholder=input_placeholder,
            label_visibility="collapsed",
            key=f"input_{field_id}",
        )
        bcols = action_container.columns([1, 1, 1], vertical_alignment="center")
        if bcols[0].button("Update", key=f"update_{field_id}"):
            parsed = value_parser(raw)
            if parsed is None:
                st.error(f"Invalid value for {label}")
            else:
                _set_manual(field_id, parsed)
                st.rerun()
        if bcols[1].button("Clear Override", key=f"clear_{field_id}"):
            _clear_manual(field_id)
            st.rerun()
        if bcols[2].button("Hide", key=f"hide_{field_id}"):
            st.session_state.edit_fields.discard(field_id)
            st.rerun()
        return

    if field_id not in st.session_state.edit_fields:
        if action_container.button("Override", key=f"override_{field_id}"):
            st.session_state.edit_fields.add(field_id)
            st.rerun()
        return

    raw = action_container.text_input(
        "value",
        value=_display_value(ri.value),
        placeholder=input_placeholder,
        label_visibility="collapsed",
        key=f"input_{field_id}",
    )
    bcols = action_container.columns([1, 1], vertical_alignment="center")
    if bcols[0].button("Set", key=f"set_{field_id}"):
        parsed = value_parser(raw)
        if parsed is None:
            st.error(f"Invalid value for {label}")
        else:
            _set_manual(field_id, parsed)
            st.rerun()
    if bcols[1].button("Cancel", key=f"cancel_{field_id}"):
        st.session_state.edit_fields.discard(field_id)
        st.rerun()


def _render_readonly_row(label: str, field_id: str) -> None:
    ri = _get_effective_field(field_id)
    cols = st.columns([2.2, 1.2, 1.2, 2.0], vertical_alignment="center")
    cols[0].write(label)
    cols[1].code(_display_value(ri.value) or "-", language=None)
    cols[2].code(_badge(ri.source, ri.is_manual_lock), language=None)
    cols[3].write(ri.ts)


def _refresh_data_with_progress(engine: GatekeeperEngine, anchor_symbol: str, tickers: list[str]) -> dict[str, Any]:
    started = time.perf_counter()
    status: dict[str, Any] = {"ts": now_iso(), "anchor_symbol": anchor_symbol, "tickers": tickers, "errors": [], "details": {}, "trace": []}
    progress = st.progress(0, text="Refresh: starting...")
    step = st.empty()

    total_steps = 2 + max(1, len(tickers))
    current = 0

    def _advance(label: str) -> None:
        nonlocal current
        current += 1
        progress.progress(min(1.0, current / total_steps), text=label)
        step.write(label)

    _log_status("refresh_data", "Start", "INFO")
    data_cfg = engine.config.get("data", {})
    bars_cfg = data_cfg.get("bars_1m", {})
    status["details"]["params"] = {
        "bars_1m": {
            "lookback_min": int(bars_cfg.get("lookback_min", 90)),
            "use_rth": bool(bars_cfg.get("use_rth", True)),
            "bar_size": str(bars_cfg.get("bar_size", "1 min")),
            "what_to_show": str(bars_cfg.get("what_to_show", "TRADES")),
            "timeout_s": float(bars_cfg.get("timeout_s", 8)),
        },
    }
    _log_status("refresh_params", f"bars_1m={status['details']['params']['bars_1m']}", "INFO")
    _ui_event(
        "refresh_start",
        anchor_symbol=anchor_symbol,
        tickers=list(tickers),
        params=status["details"]["params"],
    )
    if hasattr(engine.provider, "drain_errors"):
        engine.provider.drain_errors()
    status["trace"].append({"ts": now_iso(), "event": "refresh_start"})

    if not engine.provider.is_connected():
        _advance("Connecting to IBKR...")
        ok, msg = engine.resolver.connect_ibkr()
        if not ok:
            status["errors"].append(msg)
            status["trace"].append({"ts": now_iso(), "event": "connect_failed", "detail": msg})
            _log_status("refresh_data", f"Connect failed: {msg}", "ERROR")
            status["elapsed_s"] = round(time.perf_counter() - started, 3)
            _ui_event("refresh_end", level="ERROR", **status)
            progress.empty()
            step.empty()
            return status
        status["trace"].append({"ts": now_iso(), "event": "connect_ok"})
        _log_status("refresh_data", "Connected", "INFO")

    _advance("Fetching anchor data...")
    try:
        status["trace"].append({"ts": now_iso(), "event": "anchor_fetch_start"})
        _log_status("refresh_anchor", f"{anchor_symbol}: fetch start", "INFO")
        anchor_start = time.perf_counter()
        anchor_resolved = engine.resolver.resolve_anchor(anchor_symbol, fetch_auto=True)
        anchor_elapsed = round(time.perf_counter() - anchor_start, 3)
        status["details"]["anchor"] = {
            "anchor_price": anchor_resolved.get("anchor_price").source if anchor_resolved.get("anchor_price") else None,
            "anchor_bars_1m": anchor_resolved.get("anchor_bars_1m").source if anchor_resolved.get("anchor_bars_1m") else None,
            "anchor_vwap": anchor_resolved.get("anchor_vwap").source if anchor_resolved.get("anchor_vwap") else None,
            "vix": anchor_resolved.get("vix").source if anchor_resolved.get("vix") else None,
            "vvix": anchor_resolved.get("vvix").source if anchor_resolved.get("vvix") else None,
        }
        status["details"].setdefault("prices", {})
        anchor_price = anchor_resolved.get("anchor_price")
        if anchor_price and anchor_price.value is not None:
            status["details"]["prices"][anchor_symbol] = {
                "price": anchor_price.value,
                "source": anchor_price.source,
                "ts": anchor_price.ts,
            }
        status["details"].setdefault("timing", {})
        for key in ("anchor_price", "anchor_bars_1m", "vix", "vvix"):
            timing = st.session_state.cache.get(make_field_id("ANCHOR", f"{key}__timing", symbol=anchor_symbol))
            if timing:
                status["details"]["timing"][f"ANCHOR:{key}"] = timing.value
        status["trace"].append({"ts": now_iso(), "event": "anchor_fetch_end"})
        status["trace"].append({"ts": now_iso(), "event": "anchor_fetch_elapsed", "elapsed_s": anchor_elapsed})
        _log_status(
            "refresh_anchor",
            f"{anchor_symbol}: " + ", ".join(f"{k}={v}" for k, v in status["details"]["anchor"].items() if v is not None)
            + f" (elapsed {anchor_elapsed}s)",
            "INFO",
        )
    except Exception as exc:  # noqa: BLE001
        status["errors"].append(f"Anchor refresh failed: {exc}")
        status["trace"].append({"ts": now_iso(), "event": "anchor_fetch_error", "detail": str(exc)})
        _log_status("refresh_anchor", f"Anchor error: {exc}", "ERROR")

    status["details"]["tickers"] = {}
    for symbol in tickers:
        _advance(f"Fetching {symbol}...")
        try:
            status["trace"].append({"ts": now_iso(), "event": "ticker_fetch_start", "symbol": symbol})
            _log_status("refresh_ticker", f"{symbol}: fetch start", "INFO")
            ticker_start = time.perf_counter()
            ticker_resolved = engine.resolver.resolve_ticker(symbol, fetch_auto=True, include_options=False)
            ticker_elapsed = round(time.perf_counter() - ticker_start, 3)
            status["details"]["tickers"][symbol] = {
                "ticker_price": ticker_resolved.get("ticker_price").source if ticker_resolved.get("ticker_price") else None,
                "ticker_bars_1m": ticker_resolved.get("ticker_bars_1m").source if ticker_resolved.get("ticker_bars_1m") else None,
                "ticker_vwap": ticker_resolved.get("ticker_vwap").source if ticker_resolved.get("ticker_vwap") else None,
            }
            status["details"].setdefault("prices", {})
            price_ri = ticker_resolved.get("ticker_price")
            if price_ri and price_ri.value is not None:
                status["details"]["prices"][symbol] = {
                    "price": price_ri.value,
                    "source": price_ri.source,
                    "ts": price_ri.ts,
                }
            status["details"].setdefault("timing", {})
            for key in ("ticker_price", "ticker_bars_1m"):
                timing = st.session_state.cache.get(make_field_id("TICKER", f"{key}__timing", symbol=symbol))
                if timing:
                    status["details"]["timing"][f"TICKER:{symbol}:{key}"] = timing.value
            status["trace"].append({"ts": now_iso(), "event": "ticker_fetch_end", "symbol": symbol})
            status["trace"].append(
                {"ts": now_iso(), "event": "ticker_fetch_elapsed", "symbol": symbol, "elapsed_s": ticker_elapsed}
            )
            _log_status(
                "refresh_ticker",
                f"{symbol}: "
                + ", ".join(f"{k}={v}" for k, v in status["details"]["tickers"][symbol].items() if v is not None)
                + f" (elapsed {ticker_elapsed}s)",
                "INFO",
            )
        except Exception as exc:  # noqa: BLE001
            status["errors"].append(f"{symbol} refresh failed: {exc}")
            status["trace"].append({"ts": now_iso(), "event": "ticker_fetch_error", "symbol": symbol, "detail": str(exc)})
            _log_status("refresh_ticker", f"{symbol} error: {exc}", "ERROR")

    _advance("Refresh complete.")
    if hasattr(engine.provider, "drain_errors"):
        errors = engine.provider.drain_errors()
        if errors:
            status["details"]["ibkr_errors"] = errors
            for err in errors:
                level = _ib_error_level(err.get("code"))
                _log_status(
                    "ib_error",
                    f"req_id={err.get('req_id')} code={err.get('code')} symbol={err.get('symbol')} message={err.get('message')}",
                    level,
                )
    status["elapsed_s"] = round(time.perf_counter() - started, 3)
    _ui_event("refresh_end", level="ERROR" if status["errors"] else "INFO", **status)
    progress.empty()
    step.empty()
    return status


def _parse_float(raw: str) -> float | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _parse_bool(raw: str) -> bool | None:
    raw = (raw or "").strip().lower()
    if raw in {"1", "true", "yes", "y"}:
        return True
    if raw in {"0", "false", "no", "n"}:
        return False
    return None


def _ensure_event_loop() -> None:
    try:
        asyncio.get_running_loop()
        return
    except RuntimeError:
        pass

    try:
        asyncio.get_event_loop()
        return
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)


def _current_global_inputs(*, anchor_symbol: str) -> GlobalInputs:
    event_level = str(st.session_state.get("gk_event_level", "None") or "None")
    event_confirmed = bool(event_level == "High")
    bofa_9_3 = _parse_float(str(st.session_state.get("gk_bofa_9_3_raw", "") or ""))
    spread_width_usd = _parse_float(str(st.session_state.get("gk_spread_width_raw", "") or ""))
    return GlobalInputs(
        anchor_symbol=str(anchor_symbol).upper().strip(),
        event_confirmed=event_confirmed,
        event_level=event_level,
        bofa_9_3=bofa_9_3,
        spread_width_usd=spread_width_usd,
    )


st.set_page_config(page_title="Gatekeeper v1", layout="wide")
st.markdown(
    """
<style>
@import url('https://fonts.googleapis.com/css2?family=Sora:wght@400;600;700&family=JetBrains+Mono:wght@400;600&display=swap');
:root {
  --gk-bg: #f7f8fa;
  --gk-card: #ffffff;
  --gk-ink: #0f172a;
  --gk-muted: #6b7280;
  --gk-accent: #0f766e;
  --gk-accent-2: #b45309;
  --gk-border: #e5e7eb;
  --gk-shadow: 0 10px 24px rgba(15, 23, 42, 0.08);
}
html, body, [class*="stApp"] {
  font-family: "Sora", system-ui, -apple-system, sans-serif;
  color: var(--gk-ink);
  background:
    radial-gradient(900px 400px at 10% 0%, rgba(15, 118, 110, 0.08), transparent 60%),
    radial-gradient(900px 400px at 90% 0%, rgba(180, 83, 9, 0.06), transparent 55%),
    var(--gk-bg);
}
[data-testid="stAppViewContainer"] {
  background: transparent;
}
.block-container {
  padding-top: 1.1rem;
  padding-bottom: 4rem;
  max-width: 1400px;
}
h1, h2, h3, h4 {
  font-weight: 700;
  letter-spacing: 0.1px;
}
code, .stCodeBlock, [data-testid="stCode"] {
  font-family: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
}
.stButton button {
  background: var(--gk-ink);
  color: #fff;
  border: 1px solid var(--gk-ink);
  border-radius: 10px;
  padding: 0.4rem 1rem;
  font-weight: 600;
}
.stButton button:hover {
  background: var(--gk-accent);
  border-color: var(--gk-accent);
}
[data-testid="stMetric"] {
  background: var(--gk-card);
  border: 1px solid var(--gk-border);
  border-radius: 14px;
  padding: 14px 16px;
  box-shadow: var(--gk-shadow);
}
[data-testid="stMetricLabel"] {
  color: var(--gk-muted);
  text-transform: uppercase;
  letter-spacing: 0.08em;
  font-size: 0.7rem;
}
[data-testid="stMetricValue"] {
  color: var(--gk-ink);
}
div[data-testid="stVerticalBlock"] > div[data-testid="stExpander"] {
  border: 1px solid var(--gk-border);
  border-radius: 14px;
  background: var(--gk-card);
  box-shadow: var(--gk-shadow);
}
.stDataFrame, .stTable {
  border: 1px solid var(--gk-border);
  border-radius: 14px;
  overflow: hidden;
  background: var(--gk-card);
  box-shadow: none;
}
.stDataFrame [role="columnheader"], .stTable [role="columnheader"] {
  background: #f3f4f6;
  color: var(--gk-ink);
  font-weight: 600;
}
.stDataFrame [role="row"] {
  border-bottom: 1px solid var(--gk-border);
}
.gk-strip {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
  margin: 12px 0 18px;
}
.gk-command {
  position: sticky;
  top: 8px;
  z-index: 10;
  margin: 6px 0 16px;
}
.gk-command .gk-card {
  border: 1px solid var(--gk-border);
  background: rgba(255, 255, 255, 0.96);
  backdrop-filter: blur(8px);
}
.gk-command-actions .stButton button {
  width: 100%;
  padding: 0.6rem 1.2rem;
  border-radius: 12px;
  font-size: 0.95rem;
  letter-spacing: 0.02em;
  background: #0f172a;
  border-color: #0f172a;
}
.gk-command-actions .gk-btn-primary .stButton button {
  background: var(--gk-accent);
  border-color: var(--gk-accent);
}
.gk-command-actions .gk-btn-secondary .stButton button {
  background: #111827;
  border-color: #111827;
}
.gk-command-actions .gk-btn-ghost .stButton button {
  background: #ffffff;
  color: #111827;
  border-color: var(--gk-border);
}
.gk-command-status {
  display: flex;
  gap: 8px;
  margin-top: 8px;
}
.gk-command-status .gk-pill {
  font-size: 0.7rem;
}
.gk-command-title {
  font-size: 0.78rem;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--gk-muted);
  margin-bottom: 6px;
}
.gk-card {
  border: 1px solid var(--gk-border);
  border-radius: 14px;
  padding: 12px 14px;
  background: var(--gk-card);
  box-shadow: var(--gk-shadow);
}
.gk-title {
  font-size: 0.72rem;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--gk-muted);
}
.gk-value {
  font-size: 1.1rem;
  font-weight: 600;
  margin-top: 4px;
}
.gk-pills {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 6px;
}
.gk-pill {
  border-radius: 999px;
  padding: 4px 10px;
  font-size: 0.72rem;
  font-weight: 600;
  border: 1px solid var(--gk-border);
  background: #f8fafc;
}
.gk-pill.allow { background: rgba(15, 118, 110, 0.12); color: #0f766e; border-color: rgba(15, 118, 110, 0.4); }
.gk-pill.no-trade { background: rgba(148, 163, 184, 0.2); color: #475569; border-color: rgba(148, 163, 184, 0.4); }
.gk-pill.no-signal { background: rgba(239, 68, 68, 0.12); color: #b91c1c; border-color: rgba(239, 68, 68, 0.4); }
.gk-pill.mode { background: rgba(30, 64, 175, 0.08); color: #1e40af; border-color: rgba(30, 64, 175, 0.25); }
.gk-pill.bias { background: rgba(234, 179, 8, 0.12); color: #a16207; border-color: rgba(234, 179, 8, 0.35); }
.gk-pill.structure { background: rgba(99, 102, 241, 0.12); color: #4338ca; border-color: rgba(99, 102, 241, 0.35); }
.gk-ticker {
  border: 1px solid var(--gk-border);
  border-radius: 14px;
  padding: 10px 12px;
  background: var(--gk-card);
  box-shadow: var(--gk-shadow);
  margin-bottom: 12px;
}
.gk-ticker.allow { border-left: 4px solid #0f766e; }
.gk-ticker.no-trade { border-left: 4px solid #94a3b8; }
.gk-ticker.no-signal { border-left: 4px solid #ef4444; }
.gk-ticker-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}
.gk-ticker-badge {
  width: 10px;
  height: 10px;
  border-radius: 999px;
  margin-right: 8px;
}
.gk-ticker-badge.allow { background: #0f766e; }
.gk-ticker-badge.no-trade { background: #94a3b8; }
.gk-ticker-badge.no-signal { background: #ef4444; }
.gk-ticker-title {
  font-size: 1.1rem;
  font-weight: 700;
}
.gk-ticker-sub {
  color: var(--gk-muted);
  font-size: 0.85rem;
}
.gk-ticker-summary {
  font-size: 0.92rem;
  color: #1f2937;
  margin-top: 4px;
}
.gk-section-title {
  padding: 6px 10px;
  border-radius: 10px;
  background: #f3f4f6;
  font-size: 0.72rem;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: #475569;
  margin: 10px 0 8px;
}
.stTextInput input, .stTextArea textarea, .stNumberInput input, .stSelectbox div[data-baseweb="select"] {
  border-radius: 10px;
  border-color: var(--gk-border);
}
.stTextInput input:focus, .stTextArea textarea:focus, .stNumberInput input:focus {
  border-color: var(--gk-accent);
  box-shadow: 0 0 0 2px rgba(15, 118, 110, 0.15);
}
.stCaption, .stMarkdown p {
  color: var(--gk-muted);
}
</style>
""",
    unsafe_allow_html=True,
)
asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())
_init_state()
engine = _engine()

anchor_symbol_from_cfg = str(_get_nested(st.session_state.config, ["data", "anchor_symbol"], "QQQ")).upper()
st.session_state.setdefault("gk_anchor_symbol", anchor_symbol_from_cfg)
st.session_state.setdefault("gk_event_level", "None")
st.session_state.setdefault("gk_bofa_9_3_raw", "")
st.session_state.setdefault("gk_spread_width_raw", "")

# Use session_state for these values so Command Bar reads the latest values on the first click.
anchor_symbol = str(st.session_state.get("gk_anchor_symbol") or anchor_symbol_from_cfg).upper()
_set_nested(st.session_state.config, ["data", "anchor_symbol"], anchor_symbol)
event_level = str(st.session_state.get("gk_event_level", "None") or "None")
event_confirmed = bool(event_level == "High")
bofa_9_3 = _parse_float(str(st.session_state.get("gk_bofa_9_3_raw", "") or ""))
spread_width_usd = _parse_float(str(st.session_state.get("gk_spread_width_raw", "") or ""))

st.title("Gatekeeper Signal Engine v1")
st.caption(f"Config: `{st.session_state.config_path}`")
for sym in engine.get_watchlist():
    st.session_state.selected_symbols.setdefault(sym, True)

last_refresh_ts = (
    st.session_state.last_refresh_status.get("ts")
    if st.session_state.last_refresh_status
    else "-"
)
last_run_ts = st.session_state.last_run_ts or "-"
conn_state = "CONNECTED" if engine.provider.is_connected() else "DISCONNECTED"
summary_symbol = st.session_state.last_run_symbol
summary_result = st.session_state.run_results.get(summary_symbol) if summary_symbol else None
summary_card = summary_result.card if summary_result else None

status_class = "no-trade"
status_label = "NO_TRADE"
mode_label = "-"
bias_label = "-"
structure_label = "-"
if summary_card:
    status_label = summary_card.status or "NO_TRADE"
    mode_label = summary_card.market_mode or "-"
    bias_label = summary_card.bias or "-"
    structure_label = summary_card.structure or "-"
    if status_label == "ALLOW":
        status_class = "allow"
    elif status_label == "NO_SIGNAL":
        status_class = "no-signal"

st.markdown(
    f"""
<div class="gk-strip">
  <div class="gk-card">
    <div class="gk-title">Connection</div>
    <div class="gk-value">{conn_state}</div>
    <div class="gk-pills">
      <span class="gk-pill">{anchor_symbol}</span>
    </div>
  </div>
  <div class="gk-card">
    <div class="gk-title">Last Refresh</div>
    <div class="gk-value">{last_refresh_ts}</div>
    <div class="gk-pills">
      <span class="gk-pill">Health: {"OK" if engine.provider.is_connected() else "CHECK"}</span>
    </div>
  </div>
  <div class="gk-card">
    <div class="gk-title">Last Run</div>
    <div class="gk-value">{last_run_ts}</div>
    <div class="gk-pills">
      <span class="gk-pill">{summary_symbol or "-"}</span>
    </div>
  </div>
  <div class="gk-card">
    <div class="gk-title">Decision</div>
    <div class="gk-pills">
      <span class="gk-pill {status_class}">{status_label}</span>
      <span class="gk-pill mode">{mode_label}</span>
      <span class="gk-pill bias">{bias_label}</span>
      <span class="gk-pill structure">{structure_label}</span>
    </div>
  </div>
</div>
""",
    unsafe_allow_html=True,
)

# --- Command Bar (prominent actions)
with st.container():
    st.markdown('<div class="gk-command">', unsafe_allow_html=True)
    with st.container(border=True):
        st.markdown('<div class="gk-command-title">Command Bar</div>', unsafe_allow_html=True)
        st.markdown('<div class="gk-command-actions">', unsafe_allow_html=True)
        cmd_cols = st.columns([1.2, 1.2, 1.2, 2.0])
        with cmd_cols[0]:
            st.caption("Refresh")
            st.markdown('<div class="gk-btn-primary">', unsafe_allow_html=True)
            if st.button("Refresh Data", key="cmd_refresh"):
                _ensure_provider()
                _refresh_symbols(engine.get_watchlist())
                st.rerun()
            st.markdown("</div>", unsafe_allow_html=True)
        with cmd_cols[1]:
            st.caption("Run")
            st.markdown('<div class="gk-btn-secondary">', unsafe_allow_html=True)
            if st.button("Run Selected", key="cmd_run_selected"):
                _ensure_provider()
                selected = [s for s in engine.get_watchlist() if st.session_state.selected_symbols.get(s)]
                _run_symbols(
                    symbols=selected,
                    profile_map=st.session_state.profile_overrides,
                    global_inputs=_current_global_inputs(anchor_symbol=anchor_symbol),
                    label="run_selected",
                )
                st.rerun()
            st.markdown("</div>", unsafe_allow_html=True)
        with cmd_cols[2]:
            st.caption("Run All")
            st.markdown('<div class="gk-btn-ghost">', unsafe_allow_html=True)
            if st.button("Run All", key="cmd_run_all"):
                _ensure_provider()
                _run_symbols(
                    symbols=engine.get_watchlist(),
                    profile_map=st.session_state.profile_overrides,
                    global_inputs=_current_global_inputs(anchor_symbol=anchor_symbol),
                    label="run_all",
                )
                st.rerun()
            st.markdown("</div>", unsafe_allow_html=True)
        with cmd_cols[3]:
            st.caption("Quick Run")
            quick_symbol = st.text_input("Quick Run", value="", placeholder="e.g., AAPL", key="cmd_quick_symbol")
            if st.button("Run Quick", key="cmd_quick_run"):
                sym = quick_symbol.strip().upper()
                if not sym:
                    st.warning("Enter a symbol to run.")
                    st.stop()
                _ensure_provider()
                _run_symbols(
                    symbols=[sym],
                    profile_map=st.session_state.profile_overrides,
                    global_inputs=_current_global_inputs(anchor_symbol=anchor_symbol),
                    label="run_quick",
                )
                st.rerun()
        st.markdown("</div>", unsafe_allow_html=True)

        st.markdown('<div class="gk-command-status">', unsafe_allow_html=True)
        st.markdown(
            f"<span class='gk-pill'>Selected: {sum(1 for s in engine.get_watchlist() if st.session_state.selected_symbols.get(s))}</span>"
            f"<span class='gk-pill'>Conn: {conn_state}</span>"
            f"<span class='gk-pill'>Last Run: {last_run_ts}</span>",
            unsafe_allow_html=True,
        )
        st.markdown("</div>", unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)

# --- Top bar: connection / globals / actions
with st.container(border=True):
    c1, c2, c3 = st.columns([3, 4, 3])

    with c1:
        st.subheader("Connection")
        host = st.text_input(
            "host",
            value=str(_get_nested(st.session_state.config, ["data", "ibkr", "host"], "127.0.0.1")),
        )
        port = st.number_input(
            "port",
            min_value=1,
            max_value=65535,
            value=int(_get_nested(st.session_state.config, ["data", "ibkr", "port"], 4002)),
        )
        client_id = st.number_input(
            "clientId",
            min_value=1,
            max_value=999999,
            value=int(_get_nested(st.session_state.config, ["data", "ibkr", "clientId"], 7)),
        )
        market_data_type = st.selectbox(
            "market_data_type",
            options=[1, 2, 3, 4],
            index=[1, 2, 3, 4].index(
                int(_get_nested(st.session_state.config, ["data", "ibkr", "market_data_type"], 1))
                if int(_get_nested(st.session_state.config, ["data", "ibkr", "market_data_type"], 1)) in {1, 2, 3, 4}
                else 1
            ),
            format_func=lambda v: {1: "LIVE", 2: "FROZEN", 3: "DELAYED", 4: "DELAYED_FROZEN"}.get(v, str(v)),
            help="IBKR market data type. If you have no live subscription, try DELAYED (3).",
        )
        _set_nested(st.session_state.config, ["data", "ibkr", "host"], host)
        _set_nested(st.session_state.config, ["data", "ibkr", "port"], int(port))
        _set_nested(st.session_state.config, ["data", "ibkr", "clientId"], int(client_id))
        _set_nested(st.session_state.config, ["data", "ibkr", "market_data_type"], int(market_data_type))

        st.write(f"Status: **{'Connected' if engine.provider.is_connected() else 'Disconnected'}**")
        if st.session_state.last_connect_ts:
            st.caption(f"Last connect: {st.session_state.last_connect_ts}")
        if st.button("Connect / Reconnect"):
            _ui_event(
                "connect_click",
                host=str(host),
                port=int(port),
                client_id=int(client_id),
                market_data_type=int(market_data_type),
            )
            if st.session_state.provider is None:
                _ensure_event_loop()
                try:
                    st.session_state.provider = IBKRProvider()
                except RuntimeError as exc:
                    st.session_state.last_refresh_status = {"ts": now_iso(), "errors": [str(exc)]}
                    _log_status("connect", f"Provider init failed: {exc}", "ERROR")
                    st.rerun()
            _ensure_event_loop()
            ok, msg = engine.resolver.connect_ibkr()
            if ok:
                st.session_state.last_connect_ts = now_iso()
                _log_status("connect", "Connected to IBKR", "INFO")
            else:
                _log_status("connect", msg, "ERROR")
            st.session_state.last_refresh_status = {"ts": now_iso(), "errors": [] if ok else [msg]}
            st.rerun()

    with c2:
        st.subheader("Global Controls")
        anchor_symbol = st.selectbox(
            "Anchor",
            options=["QQQ", "SPY"],
            index=0 if str(st.session_state.get("gk_anchor_symbol", "QQQ")).upper() == "QQQ" else 1,
            key="gk_anchor_symbol",
        )
        _set_nested(st.session_state.config, ["data", "anchor_symbol"], str(anchor_symbol).upper())

        event_level = st.selectbox(
            "event_level",
            options=["None", "Medium", "High"],
            index=["None", "Medium", "High"].index(str(st.session_state.get("gk_event_level", "None") or "None")),
            key="gk_event_level",
        )
        event_confirmed = bool(event_level == "High")
        bofa_9_3_raw = st.text_input(
            "bofa_9_3 (>=8 weekly_red)",
            value=str(st.session_state.get("gk_bofa_9_3_raw", "") or ""),
            placeholder="e.g., 9.3",
            key="gk_bofa_9_3_raw",
        )
        spread_width_raw = st.text_input(
            "spread_width_usd (required)",
            value=str(st.session_state.get("gk_spread_width_raw", "") or ""),
            placeholder="e.g., 5/10/20",
            key="gk_spread_width_raw",
        )

        bofa_9_3 = _parse_float(bofa_9_3_raw)
        spread_width_usd = _parse_float(spread_width_raw)

    with c3:
        st.subheader("Actions")
        st.session_state.refresh_before_run = st.toggle(
            "Refresh before Run", value=bool(st.session_state.refresh_before_run)
        )

        if st.button("Health Check"):
            anchor_symbol = str(_get_nested(st.session_state.config, ["data", "anchor_symbol"], "QQQ")).upper()
            _ui_event("health_check_start", anchor_symbol=anchor_symbol)
            if st.session_state.provider is None:
                try:
                    _ensure_event_loop()
                    st.session_state.provider = IBKRProvider()
                except RuntimeError as exc:
                    st.session_state.health_check = {"error": str(exc)}
                    _log_status("health_check", f"Provider init failed: {exc}", "ERROR")
                    st.rerun()
            _ensure_event_loop()
            ok, msg = engine.resolver.connect_ibkr()
            if not ok:
                st.session_state.health_check = {"error": msg}
                _log_status("health_check", msg, "ERROR")
                st.rerun()
            price, price_err = engine.provider.fetch_last_price(anchor_symbol)
            bars, bars_err = engine.provider.fetch_bars_1m(
                symbol=anchor_symbol,
                lookback_min=2,
                use_rth=False,
                bar_size="1 min",
                what_to_show="TRADES",
                timeout_s=6,
            )
            vix, vix_err = engine.provider.fetch_last_price("VIX")
            vvix, vvix_err = engine.provider.fetch_last_price("VVIX")
            st.session_state.health_check = {
                "anchor_symbol": anchor_symbol,
                "price_ok": price is not None,
                "bars_ok": bool(bars),
                "vix_ok": vix is not None,
                "vvix_ok": vvix is not None,
                "errors": [e for e in (price_err, bars_err, vix_err, vvix_err) if e],
            }
            _ui_event("health_check_end", **st.session_state.health_check)
            if st.session_state.health_check.get("errors"):
                _log_status("health_check", "Errors: " + "; ".join(st.session_state.health_check["errors"]), "ERROR")
            else:
                _log_status("health_check", "OK", "INFO")
            st.rerun()

        if st.button("Clear All Overrides"):
            st.session_state.manual_overrides.clear()
            st.session_state.edit_fields.clear()
            _log_status("override_clear_all", "Cleared all manual overrides", "INFO")
            st.rerun()

        if st.session_state.last_refresh_status:
            errors = st.session_state.last_refresh_status.get("errors", [])
            if errors:
                st.error("\n".join(errors))
            else:
                st.success(f"Last refresh OK @ {st.session_state.last_refresh_status.get('ts')}")
        with st.expander("Last refresh details", expanded=False):
            st.json(st.session_state.last_refresh_status)
        if st.session_state.health_check:
            with st.expander("Health check", expanded=False):
                st.json(st.session_state.health_check)

# --- Decision summary
st.subheader("Decision Summary")
with st.container(border=True):
    conn_state = "Connected" if engine.provider.is_connected() else "Disconnected"
    refresh_ts = (
        st.session_state.last_refresh_status.get("ts")
        if st.session_state.last_refresh_status
        else "-"
    )
    run_ts = st.session_state.last_run_ts or "-"
    st.caption(f"IBKR: {conn_state} | Last refresh: {refresh_ts} | Last run: {run_ts}")

    summary_options = list(st.session_state.run_results.keys())
    summary_symbol = None
    if summary_options:
        last_sym = st.session_state.last_run_symbol
        if last_sym in summary_options:
            default_idx = summary_options.index(last_sym)
        else:
            default_idx = 0
        summary_symbol = st.selectbox("Summary symbol", options=summary_options, index=default_idx)
    else:
        st.info("No run results yet.")

    if summary_symbol:
        result = st.session_state.run_results.get(summary_symbol)
        if result:
            missing_required = []
            for step in result.steps:
                if step.step_id == "data_checks" and step.missing_required:
                    missing_required = step.missing_required
                    break
            if missing_required:
                st.error(f"Missing required: {', '.join(missing_required)}")

            card = result.card
            cols = st.columns(4)
            cols[0].metric("Status", card.status)
            cols[1].metric("Market Mode", card.market_mode or "-")
            cols[2].metric("Bias", card.bias or "-")
            cols[3].metric("Structure", card.structure or "-")
            headline = card.reason_chain[0] if card.reason_chain else "-"
            st.write(f"Reason: {headline}")

            rows = []
            for key in (
                "anchor_price",
                "anchor_vwap",
                "anchor_vwap_asof",
                "vix",
                "vvix",
                "ticker_price",
                "ticker_vwap",
                "ticker_vwap_asof",
                "ivr",
                "skew_value",
                "atm_spread_pct",
                "css",
            ):
                ri = result.resolved_inputs.get(key)
                if ri is None:
                    continue
                rows.append(
                    {
                        "key": key,
                        "value": _display_value(ri.value),
                        "source": ri.source,
                        "ts": ri.ts,
                    }
                )
            if rows:
                st.caption("Inputs (as-of)")
                st.dataframe(rows, width="stretch", hide_index=True)

            option_errors = []
            for key in ("ivr", "skew_value", "atm_spread_pct"):
                err = st.session_state.cache.get(make_field_id("TICKER", f"{key}__error", symbol=summary_symbol))
                if err:
                    option_errors.append({"field": key, "error": err.value})
            if option_errors:
                st.caption("Option data errors")
                st.dataframe(option_errors, width="stretch", hide_index=True)

# --- Config panel
with st.expander("Config (Load/Save + thresholds)", expanded=False):
    col_load, col_save = st.columns([1, 1], vertical_alignment="center")
    if st.session_state.last_saved_ts:
        st.caption(f"Last saved: {st.session_state.last_saved_ts}")
    uploaded = col_load.file_uploader("Load config.json", type=["json"])
    if uploaded is not None:
        try:
            st.session_state.config = json.loads(uploaded.read().decode("utf-8"))
            st.session_state.config_path = "<uploaded>"
            _log_status("config_load", "Loaded config from upload", "INFO")
            st.success("Loaded config from upload")
            st.rerun()
        except Exception as exc:  # noqa: BLE001
            _log_status("config_load", f"Invalid JSON: {exc}", "ERROR")
            st.error(f"Invalid JSON: {exc}")

    if col_save.button("Save config.json"):
        _save_json(DEFAULT_CONFIG_PATH, st.session_state.config)
        st.session_state.config_path = str(DEFAULT_CONFIG_PATH)
        st.session_state.last_saved_ts = now_iso()
        _log_status("config_save", f"Saved to {DEFAULT_CONFIG_PATH}", "INFO")
        st.success(f"Saved to {DEFAULT_CONFIG_PATH}")

    st.markdown("**Thresholds**")
    t1, t2, t3, t4 = st.columns(4)
    vvix_hedge = t1.number_input(
        "vvix.hedge_min",
        value=float(_get_nested(st.session_state.config, ["thresholds", "vvix", "hedge_min"], 105)),
    )
    vvix_crash = t2.number_input(
        "vvix.crash_min",
        value=float(_get_nested(st.session_state.config, ["thresholds", "vvix", "crash_min"], 110)),
    )
    weekly_red_min = t3.number_input(
        "bofa_9_3.weekly_red_min",
        value=float(_get_nested(st.session_state.config, ["thresholds", "bofa_9_3", "weekly_red_min"], 8)),
    )
    skew_high = t4.number_input(
        "skew.high_threshold",
        value=float(_get_nested(st.session_state.config, ["skew", "high_threshold"], 0.04)),
        format="%.4f",
    )
    _set_nested(st.session_state.config, ["thresholds", "vvix", "hedge_min"], float(vvix_hedge))
    _set_nested(st.session_state.config, ["thresholds", "vvix", "crash_min"], float(vvix_crash))
    _set_nested(st.session_state.config, ["thresholds", "bofa_9_3", "weekly_red_min"], float(weekly_red_min))
    _set_nested(st.session_state.config, ["skew", "high_threshold"], float(skew_high))

    st.markdown("**Chop**")
    c1, c2, c3 = st.columns(3)
    window_min = c1.slider(
        "chop.window_min", min_value=10, max_value=90, value=int(_get_nested(st.session_state.config, ["chop", "window_min"], 30))
    )
    band_pct = c2.number_input(
        "chop.band_pct",
        value=float(_get_nested(st.session_state.config, ["chop", "band_pct"], 0.0015)),
        format="%.6f",
    )
    score_trigger = c3.number_input(
        "chop.score_trigger",
        value=float(_get_nested(st.session_state.config, ["chop", "score_trigger"], 1.0)),
        format="%.3f",
    )
    _set_nested(st.session_state.config, ["chop", "window_min"], int(window_min))
    _set_nested(st.session_state.config, ["chop", "band_pct"], float(band_pct))
    _set_nested(st.session_state.config, ["chop", "score_trigger"], float(score_trigger))

    c4, c5, c6, c7 = st.columns(4)
    cross_min = c4.number_input(
        "chop.cross_min", value=int(_get_nested(st.session_state.config, ["chop", "cross_min"], 4)), step=1
    )
    touch_min = c5.number_input(
        "chop.touch_min", value=int(_get_nested(st.session_state.config, ["chop", "touch_min"], 10)), step=1
    )
    trend_cap_pct = c6.number_input(
        "chop.trend_cap_pct",
        value=float(_get_nested(st.session_state.config, ["chop", "trend_cap_pct"], 0.004)),
        format="%.6f",
    )
    w_cross = c7.number_input(
        "chop.weights.cross",
        value=float(_get_nested(st.session_state.config, ["chop", "weights", "cross"], 0.5)),
        format="%.3f",
    )
    _set_nested(st.session_state.config, ["chop", "cross_min"], int(cross_min))
    _set_nested(st.session_state.config, ["chop", "touch_min"], int(touch_min))
    _set_nested(st.session_state.config, ["chop", "trend_cap_pct"], float(trend_cap_pct))
    _set_nested(st.session_state.config, ["chop", "weights", "cross"], float(w_cross))

    w_touch = st.number_input(
        "chop.weights.touch",
        value=float(_get_nested(st.session_state.config, ["chop", "weights", "touch"], 0.3)),
        format="%.3f",
    )
    w_trend = st.number_input(
        "chop.weights.trend",
        value=float(_get_nested(st.session_state.config, ["chop", "weights", "trend"], 0.2)),
        format="%.3f",
    )
    _set_nested(st.session_state.config, ["chop", "weights", "touch"], float(w_touch))
    _set_nested(st.session_state.config, ["chop", "weights", "trend"], float(w_trend))

    st.markdown("**Raw config preview**")
    st.json(st.session_state.config)

    st.markdown("**Watchlist**")
    watchlist_raw = ",".join(engine.get_watchlist())
    watchlist_edit = st.text_input("watchlist (comma-separated)", value=watchlist_raw)
    if watchlist_edit != watchlist_raw:
        updated = [s.strip().upper() for s in watchlist_edit.split(",") if s.strip()]
        _set_nested(st.session_state.config, ["watchlist"], updated)

# --- Anchor panel
st.subheader("Anchor Data")
anchor_resolved = engine.resolver.resolve_anchor(anchor_symbol, fetch_auto=False)

with st.container(border=True):
    _render_field_row(
        label="anchor_price",
        field_id=make_field_id("ANCHOR", "anchor_price", symbol=anchor_symbol),
        value_parser=_parse_float,
        input_placeholder="e.g., 399.12",
    )
    _render_field_row(
        label="anchor_vwap",
        field_id=make_field_id("ANCHOR", "anchor_vwap", symbol=anchor_symbol),
        value_parser=_parse_float,
        input_placeholder="e.g., 398.50",
    )
    _render_field_row(
        label="vix",
        field_id=make_field_id("ANCHOR", "vix", symbol=anchor_symbol),
        value_parser=_parse_float,
        input_placeholder="e.g., 19.2",
    )
    _render_field_row(
        label="vvix",
        field_id=make_field_id("ANCHOR", "vvix", symbol=anchor_symbol),
        value_parser=_parse_float,
        input_placeholder="e.g., 108.0",
    )
    _render_field_row(
        label="anchor_drop_15m_pct (optional)",
        field_id=make_field_id("ANCHOR", "anchor_drop_15m_pct", symbol=anchor_symbol),
        value_parser=_parse_float,
        input_placeholder="e.g., 0.9",
    )
    _render_field_row(
        label="anchor_chop_score",
        field_id=make_field_id("ANCHOR", "anchor_chop_score", symbol=anchor_symbol),
        value_parser=_parse_float,
        input_placeholder="e.g., 0.8",
    )
    _render_field_row(
        label="anchor_is_choppy",
        field_id=make_field_id("ANCHOR", "anchor_is_choppy", symbol=anchor_symbol),
        value_parser=_parse_bool,
        input_placeholder="true/false",
    )
    for key in ("anchor_price", "anchor_bars_1m", "vix", "vvix"):
        err = st.session_state.cache.get(make_field_id("ANCHOR", f"{key}__error", symbol=anchor_symbol))
        if err:
            st.warning(f"{key} fetch error: {err.value}")

    chop_score = anchor_resolved.get("anchor_chop_score")
    chop_details = st.session_state.cache.get(make_field_id("ANCHOR", "anchor_chop_details", symbol=anchor_symbol))
    with st.expander("Chop details", expanded=False):
        st.json(chop_details.to_dict() if chop_details else {})

# --- Data dashboard (fetched vs computed)
st.subheader("Data Dashboard")
with st.container(border=True):
    prices = (
        st.session_state.last_refresh_status.get("details", {}).get("prices", {})
        if st.session_state.last_refresh_status
        else {}
    )
    if prices:
        st.caption("Latest Prices")
        rows = [
            {"symbol": sym, "price": _display_value(info.get("price")), "source": info.get("source"), "ts": info.get("ts")}
            for sym, info in prices.items()
        ]
        st.dataframe(rows, width="stretch", hide_index=True)

    resolved_anchor = engine.resolver.resolve_anchor(anchor_symbol, fetch_auto=False)
    resolved_global = engine.resolver.resolve_global(
        GlobalInputs(
            anchor_symbol=anchor_symbol,
            event_confirmed=event_confirmed,
            event_level=event_level,
            bofa_9_3=bofa_9_3,
            spread_width_usd=spread_width_usd,
        )
    )

    fetched_rows = []
    computed_rows = []

    for key in ("anchor_price", "anchor_bars_1m", "vix", "vvix"):
        ri = resolved_anchor.get(key)
        if ri is None:
            continue
        fetched_rows.append(
            {
                "scope": "ANCHOR",
                "key": key,
                "value": _display_value(ri.value),
                "source": ri.source,
                "ts": ri.ts,
            }
        )

    for key in ("anchor_vwap", "anchor_vwap_asof", "anchor_chop_score", "anchor_is_choppy", "anchor_drop_15m_pct"):
        ri = resolved_anchor.get(key)
        if ri is None:
            continue
        computed_rows.append(
            {
                "scope": "ANCHOR",
                "key": key,
                "value": _display_value(ri.value),
                "source": ri.source,
                "ts": ri.ts,
            }
        )

    for key in ("event_confirmed", "bofa_9_3", "spread_width_usd"):
        ri = resolved_global.get(key)
        computed_rows.append(
            {
                "scope": "GLOBAL",
                "key": key,
                "value": _display_value(ri.value),
                "source": ri.source,
                "ts": ri.ts,
            }
        )

    col_f, col_c = st.columns(2)
    col_f.markdown("**IBKR / Fetched**")
    if fetched_rows:
        fetched_df = pd.DataFrame(fetched_rows)
        col_f.dataframe(_style_missing(fetched_df), width="stretch", hide_index=True)
    else:
        col_f.write("No fetched data yet.")

    col_c.markdown("**Computed / Manual**")
    if computed_rows:
        computed_df = pd.DataFrame(computed_rows)
        col_c.dataframe(_style_missing(computed_df), width="stretch", hide_index=True)
    else:
        col_c.write("No computed data yet.")

# --- Run buttons
st.subheader("Tickers")
watchlist = engine.get_watchlist()

with st.container(border=True):
    if not watchlist:
        st.info("No watchlist configured. Edit `watchlist` in config.json.")
        st.stop()

    expand_all = st.toggle("Expand all details", value=False, key="expand_all_details")

    def _headline(result) -> str:
        if not result:
            return "-"
        chain = result.card.reason_chain
        return chain[0] if chain else "-"

    for sym in watchlist:
        st.session_state.selected_symbols.setdefault(sym, True)

        profile_default = st.session_state.profile_overrides.get(sym) or engine.default_profile_for_symbol(sym)
        result = st.session_state.run_results.get(sym)
        status = result.card.status if result else "NO_TRADE"
        structure = result.card.structure if result else "-"
        status_class = "no-trade"
        if status == "ALLOW":
            status_class = "allow"
        elif status == "NO_SIGNAL":
            status_class = "no-signal"

        profile_options = [
            k
            for k, v in st.session_state.config.get("profiles", {}).items()
            if isinstance(v, dict) and "pipeline" in v
        ]
        if not profile_options:
            profile_options = ["gatekeeper_v1"]
        default_profile = profile_default if profile_default in profile_options else profile_options[0]

        st.markdown(f'<div class="gk-ticker {status_class}">', unsafe_allow_html=True)
        header_cols = st.columns([0.6, 2.6, 2.2, 2.6])
        header_cols[0].checkbox("sel", key=f"sel_{sym}", value=st.session_state.selected_symbols[sym])
        st.session_state.selected_symbols[sym] = st.session_state.get(f"sel_{sym}", False)
        badge_class = status_class
        header_cols[1].markdown(
            f"<div style='display:flex;align-items:center;gap:8px;'>"
            f"<span class='gk-ticker-badge {badge_class}'></span>"
            f"<div class='gk-ticker-title'>{sym}</div></div>",
            unsafe_allow_html=True,
        )
        profile = header_cols[2].selectbox(
            "profile",
            options=profile_options,
            index=profile_options.index(default_profile),
            key=f"profile_{sym}",
        )
        if profile != profile_default:
            st.session_state.profile_overrides[sym] = profile
        header_cols[3].markdown(
            f"<div class='gk-pills'><span class='gk-pill {status_class}'>{status}</span>"
            f"<span class='gk-pill structure'>{structure}</span></div>",
            unsafe_allow_html=True,
        )

        summary_line = "-"
        if result:
            change = ""
            prev = st.session_state.last_cards_prev.get(sym)
            if prev:
                if (
                    prev.market_mode != result.card.market_mode
                    or prev.bias != result.card.bias
                    or prev.structure != result.card.structure
                ):
                    change = " • changed"
            summary_line = f"{result.card.market_mode}/{result.card.bias} — {_headline(result)}{change}"
        st.markdown(f"<div class='gk-ticker-summary'>{summary_line}</div>", unsafe_allow_html=True)

        with st.expander(f"{sym} details", expanded=expand_all):
            action_cols = st.columns([1, 1, 3])
            if action_cols[0].button("Run", key=f"run_{sym}"):
                _ensure_provider()
                global_inputs = _current_global_inputs(anchor_symbol=anchor_symbol)
                if not _ensure_connected(engine, action=f"run_{sym}"):
                    st.stop()
                if st.session_state.refresh_before_run:
                    st.session_state.last_refresh_status = engine.refresh_data(anchor_symbol=anchor_symbol, tickers=[sym])
                if sym in st.session_state.run_results:
                    st.session_state.last_cards_prev[sym] = st.session_state.run_results[sym].card
                st.session_state.run_results[sym] = engine.run_symbol(
                    symbol=sym, profile=profile, global_inputs=global_inputs, fetch_auto=True
                )
                st.session_state.last_cards[sym] = st.session_state.run_results[sym].card
                st.session_state.last_run_ts = now_iso()
                st.session_state.last_run_symbol = sym
                _log_status("run_single", f"Ran {sym}", "INFO")
                st.rerun()

            if action_cols[1].button("Refresh", key=f"refresh_{sym}"):
                st.session_state.last_refresh_status = _refresh_data_with_progress(engine, anchor_symbol, [sym])
                if st.session_state.last_refresh_status.get("errors"):
                    _log_status(
                        "refresh_symbol",
                        f"{sym} errors: " + "; ".join(st.session_state.last_refresh_status["errors"]),
                        "ERROR",
                    )
                else:
                    _log_status("refresh_symbol", f"{sym} OK", "INFO")
                    details = st.session_state.last_refresh_status.get("details", {})
                    ticker_details = details.get("tickers", {}).get(sym, {})
                    if ticker_details:
                        _log_status(
                            "refresh_symbol",
                            f"{sym} sources: " + ", ".join(f"{k}={v}" for k, v in ticker_details.items() if v is not None),
                            "INFO",
                        )
                st.rerun()

            st.markdown('<div class="gk-section-title">Inputs</div>', unsafe_allow_html=True)
            ticker_resolved = engine.resolver.resolve_ticker(sym, fetch_auto=False)

            global_resolved = engine.resolver.resolve_global(
                GlobalInputs(
                    anchor_symbol=anchor_symbol,
                    event_confirmed=event_confirmed,
                    event_level=event_level,
                    bofa_9_3=bofa_9_3,
                    spread_width_usd=spread_width_usd,
                )
            )
            global_missing = []
            if global_resolved["bofa_9_3"].source == "MISSING":
                global_missing.append("bofa_9_3")
            if global_resolved["spread_width_usd"].source == "MISSING":
                global_missing.append("spread_width_usd")
            with st.expander("Global Required (read-only)", expanded=False):
                global_rows = []
                for key in ("event_level", "event_confirmed", "bofa_9_3", "spread_width_usd"):
                    ri = global_resolved[key]
                    global_rows.append(
                        {
                            "key": key,
                            "value": _display_value(ri.value),
                            "source": ri.source,
                            "ts": ri.ts,
                        }
                    )
                st.dataframe(global_rows, width="stretch")
                if global_missing:
                    st.error(f"Missing global required: {', '.join(global_missing)}")

            missing_required = []
            required_keys = [
                ("ticker_price", make_field_id("TICKER", "ticker_price", symbol=sym)),
                ("ticker_vwap", make_field_id("TICKER", "ticker_vwap", symbol=sym)),
                ("ivr", make_field_id("TICKER", "ivr", symbol=sym)),
                ("skew_value", make_field_id("TICKER", "skew_value", symbol=sym)),
                ("atm_spread_pct", make_field_id("TICKER", "atm_spread_pct", symbol=sym)),
                ("css", make_field_id("TICKER", "css", symbol=sym)),
            ]
            for key_name, fid in required_keys:
                if _get_effective_field(fid).source == "MISSING":
                    missing_required.append(key_name)
            if result:
                for step in result.steps:
                    if step.step_id == "data_checks" and step.missing_required:
                        missing_required.extend([k for k in step.missing_required if k not in missing_required])

            if missing_required:
                st.error(f"Missing required: {', '.join(missing_required)}")

            _render_field_row(
                label="ticker_price",
                field_id=make_field_id("TICKER", "ticker_price", symbol=sym),
                value_parser=_parse_float,
                input_placeholder="e.g., 320.15",
            )
            _render_field_row(
                label="ticker_vwap",
                field_id=make_field_id("TICKER", "ticker_vwap", symbol=sym),
                value_parser=_parse_float,
                input_placeholder="e.g., 319.80",
            )
            for key in ("ticker_price", "ticker_bars_1m", "ivr", "skew_value", "atm_spread_pct"):
                err = st.session_state.cache.get(make_field_id("TICKER", f"{key}__error", symbol=sym))
                if err:
                    st.warning(f"{key} fetch error: {err.value}")
            _render_field_row(
                label="ivr (required)",
                field_id=make_field_id("TICKER", "ivr", symbol=sym),
                value_parser=_parse_float,
                input_placeholder="e.g., 25",
            )
            _render_field_row(
                label="RR25 (skew_value, pts)",
                field_id=make_field_id("TICKER", "skew_value", symbol=sym),
                value_parser=_parse_float,
                input_placeholder="e.g., 6.2",
            )
            _render_field_row(
                label="css (optional)",
                field_id=make_field_id("TICKER", "css", symbol=sym),
                value_parser=_parse_float,
                input_placeholder="e.g., 55",
            )
            _render_field_row(
                label="atm_spread_pct (auto, %)",
                field_id=make_field_id("TICKER", "atm_spread_pct", symbol=sym),
                value_parser=_parse_float,
                input_placeholder="e.g., 1.2",
            )

            option_errors = []
            for key in ("ivr", "skew_value", "atm_spread_pct"):
                err = st.session_state.cache.get(make_field_id("TICKER", f"{key}__error", symbol=sym))
                if err:
                    option_errors.append({"field": key, "error": err.value})
            if option_errors:
                with st.expander("Option data errors", expanded=False):
                    st.dataframe(option_errors, width="stretch", hide_index=True)

            earnings_fid = make_field_id("TICKER", "earnings_lt_5d", symbol=sym)
            earnings_ri = ticker_resolved.get("earnings_lt_5d")
            if earnings_ri is None:
                earnings_ri = ResolvedInput(value=False, source="MISSING", ts=now_iso(), is_manual_lock=False)
            ecols = st.columns([2, 1, 1, 2], vertical_alignment="center")
            ecols[0].write("earnings_lt_5d (toggle)")
            earnings_val = ecols[1].toggle(
                "earnings_lt_5d",
                value=bool(earnings_ri.value) if earnings_ri.source != "MISSING" else False,
                key=f"earnings_toggle_{sym}",
                label_visibility="collapsed",
            )
            if earnings_val != bool(earnings_ri.value) or earnings_ri.source != "MANUAL":
                _set_manual(earnings_fid, bool(earnings_val))
            ecols[2].code(_badge(earnings_ri.source, bool(earnings_ri.is_manual_lock)), language=None)
            if ecols[3].button("Clear Override", key=f"clear_earnings_{sym}"):
                _clear_manual(earnings_fid)
                st.rerun()

            st.markdown('<div class="gk-section-title">Steps</div>', unsafe_allow_html=True)
            if result:
                first_fail_or_stop = None
                for idx, step in enumerate(result.steps):
                    if step.status == "FAIL" or step.stop:
                        first_fail_or_stop = idx
                        break

                for idx, step in enumerate(result.steps):
                    expanded = first_fail_or_stop == idx
                    with st.expander(f"{step.step_id}: {step.status} ({step.severity})", expanded=expanded):
                        st.json(step.to_dict())
            else:
                st.info("No run result yet. Click Run.")

            st.markdown('<div class="gk-section-title">Signal Card</div>', unsafe_allow_html=True)
            if result:
                st.json(result.card.to_dict())

            st.markdown('<div class="gk-section-title">Debug</div>', unsafe_allow_html=True)
            debug = st.session_state.cache.get(make_field_id("TICKER", "ticker_debug", symbol=sym))
            with st.expander("Ticker debug (last3/range/gap/vwap_dev)", expanded=False):
                st.json(debug.to_dict() if debug else {})
            bars = ticker_resolved.get("ticker_bars_1m")
            with st.expander("Last 30 1m bars", expanded=False):
                if bars and isinstance(bars.value, list):
                    st.dataframe(bars.value[-30:], width="stretch")
                else:
                    st.write("-")

            with st.expander("Ticker Data Dashboard", expanded=False):
                fetched = []
                computed = []
                for key in ("ticker_price", "ticker_bars_1m", "ivr", "skew_value", "atm_spread_pct"):
                    ri = ticker_resolved.get(key)
                    if ri:
                        fetched.append(
                            {
                                "scope": sym,
                                "key": key,
                                "value": _display_value(ri.value),
                                "source": ri.source,
                                "ts": ri.ts,
                            }
                        )
                for key in ("ticker_vwap", "ticker_vwap_asof", "css", "earnings_lt_5d"):
                    ri = ticker_resolved.get(key)
                    if ri:
                        computed.append(
                            {
                                "scope": sym,
                                "key": key,
                                "value": _display_value(ri.value),
                                "source": ri.source,
                                "ts": ri.ts,
                            }
                        )
                fcol, ccol = st.columns(2)
                fcol.markdown("**IBKR / Fetched**")
                if fetched:
                    fcol.dataframe(_style_missing(pd.DataFrame(fetched)), width="stretch", hide_index=True)
                else:
                    fcol.write("No fetched data yet.")
                ccol.markdown("**Computed / Manual**")
                if computed:
                    ccol.dataframe(_style_missing(pd.DataFrame(computed)), width="stretch", hide_index=True)
                else:
                    ccol.write("No computed data yet.")

        st.markdown("</div>", unsafe_allow_html=True)

with st.container(border=True):
    st.subheader("Status")
    st.caption(
        f"UI audit log: `{str(_ui_log_path())}` | session_id={st.session_state.get('ui_session_id')} (JSONL, rotated)"
    )
    if st.session_state.status_log:
        st.dataframe(st.session_state.status_log, width="stretch", hide_index=True)
    else:
        st.write("No recent actions.")
    last_errors = (
        st.session_state.last_refresh_status.get("details", {}).get("ibkr_errors", [])
        if st.session_state.last_refresh_status
        else []
    )
    if last_errors:
        st.caption("Latest IBKR errors")
        st.dataframe(last_errors, width="stretch", hide_index=True)

# --- Audit trail (reviewer / validator)
with st.container(border=True):
    st.subheader("Audit Trail (Reviewer / Validator)")
    st.caption("目的：把当前“有效输入（含手动覆盖）/抓取耗时与错误/每一步 rule 的 metrics 与 explain”集中展示，方便你手工复核。")

    audit_tabs = st.tabs(["Effective Inputs", "Fetch Timing & Errors", "Run Steps"])

    watchlist = engine.get_watchlist()
    known_symbols = sorted(set(watchlist) | set(st.session_state.run_results.keys()))
    anchor_symbol_cfg = str(_get_nested(st.session_state.config, ["data", "anchor_symbol"], "QQQ")).upper()

    with audit_tabs[0]:
        st.markdown("**GLOBAL**")
        ui_global_rows = [
            {
                "key": "event_level",
                "value": str(st.session_state.get("gk_event_level", "None") or "None"),
                "source": "UI",
            },
            {
                "key": "event_confirmed",
                "value": bool(str(st.session_state.get("gk_event_level", "None") or "None") == "High"),
                "source": "UI",
            },
            {"key": "bofa_9_3_raw", "value": str(st.session_state.get("gk_bofa_9_3_raw", "") or ""), "source": "UI"},
            {
                "key": "bofa_9_3",
                "value": _parse_float(str(st.session_state.get("gk_bofa_9_3_raw", "") or "")),
                "source": "UI",
            },
            {
                "key": "spread_width_raw",
                "value": str(st.session_state.get("gk_spread_width_raw", "") or ""),
                "source": "UI",
            },
            {
                "key": "spread_width_usd",
                "value": _parse_float(str(st.session_state.get("gk_spread_width_raw", "") or "")),
                "source": "UI",
            },
        ]
        st.dataframe(ui_global_rows, width="stretch", hide_index=True)
        global_ids = [
            make_field_id("GLOBAL", "event_level"),
            make_field_id("GLOBAL", "event_confirmed"),
            make_field_id("GLOBAL", "bofa_9_3"),
            make_field_id("GLOBAL", "spread_width_usd"),
        ]
        gdf = pd.DataFrame(_audit_rows(global_ids))
        if not gdf.empty:
            st.dataframe(_style_missing(gdf), width="stretch", hide_index=True)

        st.markdown("**ANCHOR**")
        anchor_ids = [
            make_field_id("ANCHOR", "anchor_price", symbol=anchor_symbol_cfg),
            make_field_id("ANCHOR", "anchor_vwap", symbol=anchor_symbol_cfg),
            make_field_id("ANCHOR", "anchor_vwap_asof", symbol=anchor_symbol_cfg),
            make_field_id("ANCHOR", "vix", symbol=anchor_symbol_cfg),
            make_field_id("ANCHOR", "vvix", symbol=anchor_symbol_cfg),
            make_field_id("ANCHOR", "anchor_drop_15m_pct", symbol=anchor_symbol_cfg),
            make_field_id("ANCHOR", "anchor_chop_score", symbol=anchor_symbol_cfg),
            make_field_id("ANCHOR", "anchor_is_choppy", symbol=anchor_symbol_cfg),
        ]
        adf = pd.DataFrame(_audit_rows(anchor_ids))
        if not adf.empty:
            st.dataframe(_style_missing(adf), width="stretch", hide_index=True)

        st.markdown("**TICKERS**")
        if not known_symbols:
            st.write("No symbols yet.")
        else:
            sym = st.selectbox("Symbol", options=known_symbols, index=0, key="audit_symbol_select")
            ticker_ids = [
                make_field_id("TICKER", "ticker_price", symbol=sym),
                make_field_id("TICKER", "ticker_vwap", symbol=sym),
                make_field_id("TICKER", "ticker_vwap_asof", symbol=sym),
                make_field_id("TICKER", "ivr", symbol=sym),
                make_field_id("TICKER", "skew_value", symbol=sym),
                make_field_id("TICKER", "atm_spread_pct", symbol=sym),
                make_field_id("TICKER", "css", symbol=sym),
                make_field_id("TICKER", "earnings_lt_5d", symbol=sym),
            ]
            tdf = pd.DataFrame(_audit_rows(ticker_ids))
            if not tdf.empty:
                st.dataframe(_style_missing(tdf), width="stretch", hide_index=True)

            debug_ids = [
                make_field_id("TICKER", "css_debug", symbol=sym),
                make_field_id("TICKER", "ticker_debug", symbol=sym),
            ]
            st.markdown("**Debug payloads**")
            for fid in debug_ids:
                ri = _get_effective_field(fid)
                with st.expander(f"{fid} ({_badge(ri.source, ri.is_manual_lock)})", expanded=False):
                    st.json(ri.to_dict())

        with st.expander("Raw store (manual_overrides + cache)", expanded=False):
            st.json(
                {
                    "manual_overrides": {k: v.to_dict() for k, v in st.session_state.manual_overrides.items()},
                    "cache": {k: v.to_dict() for k, v in st.session_state.cache.store.items()},
                }
            )

    with audit_tabs[1]:
        st.markdown("**Refresh trace**")
        if st.session_state.last_refresh_status:
            trace = st.session_state.last_refresh_status.get("trace", []) or []
            if trace:
                st.dataframe(trace, width="stretch", hide_index=True)
            else:
                st.write("No trace yet.")
        else:
            st.write("No refresh yet.")

        st.markdown("**Resolver timing fields**")
        timing_rows: list[dict[str, Any]] = []
        for fid, ri in st.session_state.cache.store.items():
            if fid.endswith("__timing"):
                timing_rows.append({"field_id": fid, "value": ri.value, "ts": ri.ts})
        if timing_rows:
            st.dataframe(timing_rows, width="stretch", hide_index=True)
        else:
            st.write("No timing entries yet.")

        st.markdown("**Resolver error fields**")
        err_rows: list[dict[str, Any]] = []
        for fid, ri in st.session_state.cache.store.items():
            if fid.endswith("__error"):
                err_rows.append({"field_id": fid, "error": ri.value, "ts": ri.ts})
        if err_rows:
            st.dataframe(err_rows, width="stretch", hide_index=True)
        else:
            st.write("No cached errors.")

    with audit_tabs[2]:
        if not st.session_state.run_results:
            st.write("No runs yet.")
        else:
            run_syms = sorted(st.session_state.run_results.keys())
            sym = st.selectbox("Run symbol", options=run_syms, index=0, key="audit_run_symbol")
            result = st.session_state.run_results.get(sym)
            if not result:
                st.write("Missing result.")
            else:
                st.markdown("**Signal card**")
                st.json(result.card.to_dict())

                st.markdown("**Steps summary**")
                rows: list[dict[str, Any]] = []
                for step in result.steps:
                    headline = step.explain[0] if step.explain else ""
                    rows.append(
                        {
                            "step_id": step.step_id,
                            "status": step.status,
                            "severity": step.severity,
                            "stop": bool(step.stop),
                            "headline": headline,
                            "missing_required": ", ".join(step.missing_required or []),
                        }
                    )
                st.dataframe(rows, width="stretch", hide_index=True)

                st.markdown("**Step details (metrics/explain/debug)**")
                for step in result.steps:
                    with st.expander(f"{step.step_id}: {step.status} ({step.severity})", expanded=False):
                        st.json(step.to_dict())
