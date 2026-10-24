from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

from libs.db.ch_pool import get_client
from libs.strategy import StrategyParams
from .parity import OptionParityRunner
from strategies.qqq_1dte_ma10_reject_live_strategy import QQQ1DTEMA10RejectLiveStrategy
from strategies.qqq_1dte_ma10_reject_v2_strategy import QQQ1DTEMA10RejectV2Strategy
from strategies.spy_1dte_ma10_reject_live_strategy import SPY1DTEMA10RejectLiveStrategy
from strategies.spy_1dte_ma10_reject_v2_strategy import SPY1DTEMA10RejectV2Strategy


ET = ZoneInfo("America/New_York")


def _resolve_strategy_classes(scenario: Dict[str, Any]):
    variant = str(scenario.get("strategy_variant") or "qqq_option_v2").strip().lower()
    if variant == "spy_option_ma":
        return (
            SPY1DTEMA10RejectLiveStrategy,
            SPY1DTEMA10RejectV2Strategy,
            "spy_1dte_ma10_reject_legacy_harness",
            "spy_1dte_ma10_reject_v2_harness",
        )
    return (
        QQQ1DTEMA10RejectLiveStrategy,
        QQQ1DTEMA10RejectV2Strategy,
        "qqq_1dte_ma10_reject_legacy_harness",
        "qqq_1dte_ma10_reject_v2_harness",
    )


class _DummyLogger:
    def __init__(self) -> None:
        self.warning = MagicMock()
        self.info = MagicMock()
        self.error = MagicMock()
        self.debug = MagicMock()


def _make_ctx(strategy_id: str):
    ctx = MagicMock()
    ctx.log = _DummyLogger()
    ctx.engine = MagicMock()
    ctx.subscribe_features = AsyncMock()
    ctx.enable_realtime_feature_engine = AsyncMock()
    ctx.disable_realtime_feature_engine = AsyncMock()
    ctx.portfolio = AsyncMock(return_value={"positions": []})
    ctx.position = AsyncMock(return_value=MagicMock(pos_qty=0.0, avg_px=0.0))
    ctx.open_orders = AsyncMock(return_value=[])
    ctx.submit_intent = AsyncMock()
    ctx.strategy_id = strategy_id
    return ctx


def _disable_quote_health(strategy: Any) -> None:
    strategy._update_quote_data_health_gate = lambda now: None
    strategy._maybe_release_quote_data_health_gate = lambda now: None
    strategy._quote_heartbeat_allows_entry = lambda now: True


def _normalize_bar(raw: Dict[str, Any]) -> Dict[str, Any]:
    ts = datetime.fromisoformat(str(raw["ts"])).astimezone(ET)
    close = float(raw["close"])
    return {
        "ts": ts,
        "open": float(raw.get("open", close)),
        "high": float(raw.get("high", close)),
        "low": float(raw.get("low", close)),
        "close": close,
        "vwap": float(raw.get("vwap", close)),
        "volume": float(raw.get("volume", 0.0)),
        "atr_d": float(raw.get("atr_d", 0.0)),
        "atr_1m": float(raw.get("atr_1m", 0.0)),
        "atr_1m_ma": float(raw.get("atr_1m_ma", 0.0)),
        "vol_ma": raw.get("vol_ma"),
        "rvol": raw.get("rvol"),
    }


def _load_day_bars_from_clickhouse(*, symbol: str, day: str, session_mode: str = "rth") -> List[Dict[str, Any]]:
    ch = get_client()
    day_start = datetime.fromisoformat(f"{day}T00:00:00-04:00").astimezone(ET)
    day_end = day_start + timedelta(days=1)
    sql = """
        SELECT
          ts_start AS ts,
          open,
          high,
          low,
          close,
          vwap,
          volume,
          is_rth
        FROM trading.bars_1m_ibkr FINAL
        WHERE symbol = {symbol:String}
          AND ts_start >= {start:DateTime64(3, 'America/New_York')}
          AND ts_start < {end:DateTime64(3, 'America/New_York')}
        ORDER BY ts_start ASC
    """
    rows = ch.query(
        sql,
        parameters={
            "symbol": str(symbol).strip().upper(),
            "start": day_start,
            "end": day_end,
        },
    )
    out: List[Dict[str, Any]] = []
    use_extended = str(session_mode or "rth").strip().lower() == "extended"
    for row in rows:
        if not use_extended and not bool(row.get("is_rth", 0)):
            continue
        ts = row.get("ts")
        if not isinstance(ts, datetime):
            continue
        out.append(
            {
                "ts": ts.astimezone(ET).isoformat(),
                "open": float(row.get("open") or 0.0),
                "high": float(row.get("high") or 0.0),
                "low": float(row.get("low") or 0.0),
                "close": float(row.get("close") or 0.0),
                "vwap": float(row.get("vwap") or row.get("close") or 0.0),
                "volume": float(row.get("volume") or 0.0),
            }
        )
    return out


async def run_direct_entry_parity_scenario(scenario: Dict[str, Any]) -> Dict[str, Any]:
    now = datetime.fromisoformat(str(scenario["now"])).astimezone(ET)
    side = str(scenario["side"])
    spot = float(scenario["spot"])
    entry_bar = dict(scenario.get("entry_bar") or {})
    plan = dict(scenario.get("plan") or {})
    audit_ids = tuple(scenario.get("audit_ids") or ("t-fixed", "d-fixed", "x-fixed"))
    snapshot_root = str(scenario.get("snapshot_root") or "runtime/state/decision_snapshots_harness")

    old_cls, new_cls, old_strategy_id, new_strategy_id = _resolve_strategy_classes(scenario)
    extras = dict(scenario.get("strategy_extras") or {})
    extras.setdefault("preload_history_bars", 0)

    old_ctx = _make_ctx(old_strategy_id)
    v2_ctx = _make_ctx(new_strategy_id)

    old = old_cls(old_ctx, StrategyParams(extras=dict(extras)))
    new = new_cls(
        v2_ctx,
        StrategyParams(
            extras={
                **dict(extras),
                "live_parity_snapshot_root": snapshot_root,
                "kernel_mode": "hybrid",
            }
        ),
    )
    await old.initialize()
    await new.initialize()

    if "quote" in scenario:
        quote = dict(scenario.get("quote") or {})
        old._select_0dte_quote = lambda *args, **kwargs: (quote, None)  # type: ignore[method-assign]
        new.kernel.delegate._select_0dte_quote = lambda *args, **kwargs: (quote, None)  # type: ignore[attr-defined]
    old._pending_entry_plan = dict(plan)
    new.kernel.delegate._pending_entry_plan = dict(plan)  # type: ignore[attr-defined]

    await old._submit_entry(
        now,
        side=side,
        spot=spot,
        entry_bar=entry_bar,
        spread_max_pct=float(scenario.get("spread_max_pct", 0.12) or 0.12),
        wall_now=now,
        audit_ids=audit_ids,  # type: ignore[arg-type]
    )
    await new.kernel.delegate._submit_entry(  # type: ignore[attr-defined]
        now,
        side=side,
        spot=spot,
        entry_bar=entry_bar,
        spread_max_pct=float(scenario.get("spread_max_pct", 0.12) or 0.12),
        wall_now=now,
        audit_ids=audit_ids,  # type: ignore[arg-type]
    )

    old_env = old_ctx.submit_intent.await_args.args[0]
    new_env = v2_ctx.submit_intent.await_args.args[0]
    diff = OptionParityRunner().compare_envelopes([old_env], [new_env])
    return {
        "matched": bool(diff.get("matched")),
        "diff": diff,
        "old_kernel": None,
        "new_kernel": new.kernel.state_snapshot(),
        "old_envs": [old_env],
        "new_envs": [new_env],
    }


def run_direct_entry_parity_scenario_sync(scenario: Dict[str, Any]) -> Dict[str, Any]:
    return asyncio.run(run_direct_entry_parity_scenario(scenario))


async def run_minute_session_parity_scenario(scenario: Dict[str, Any]) -> Dict[str, Any]:
    snapshot_root = str(scenario.get("snapshot_root") or "runtime/state/decision_snapshots_harness")
    old_cls, new_cls, old_strategy_id, new_strategy_id = _resolve_strategy_classes(scenario)
    old_ctx = _make_ctx(old_strategy_id)
    v2_ctx = _make_ctx(new_strategy_id)
    extras = dict(scenario.get("strategy_extras") or {})
    extras.setdefault("preload_history_bars", 0)
    old = old_cls(old_ctx, StrategyParams(extras=dict(extras)))
    v2_extras = dict(extras)
    v2_extras["live_parity_snapshot_root"] = snapshot_root
    v2_extras["kernel_mode"] = "hybrid"
    new = new_cls(v2_ctx, StrategyParams(extras=v2_extras))
    await old.initialize()
    await new.initialize()
    _disable_quote_health(old)
    _disable_quote_health(new.kernel.delegate)  # type: ignore[attr-defined]

    if "quote" in scenario:
        quote = dict(scenario.get("quote") or {})
        old._select_0dte_quote = lambda *args, **kwargs: (quote, None)  # type: ignore[method-assign]
        new.kernel.delegate._select_0dte_quote = lambda *args, **kwargs: (quote, None)  # type: ignore[attr-defined]

    for raw in list(scenario.get("seed_bars") or []):
        bar = _normalize_bar(raw)
        old._enrich_bar(bar)
        old._bars.append(dict(bar))
        delegate = new.kernel.delegate  # type: ignore[attr-defined]
        delegate._enrich_bar(dict(bar))
        delegate._bars.append(dict(bar))

    for raw in list(scenario.get("bars") or []):
        bar = _normalize_bar(raw)
        await old._process_completed_minute_bar(bar["ts"], dict(bar), wall_now=bar["ts"])
        await new.kernel.delegate._process_completed_minute_bar(bar["ts"], dict(bar), wall_now=bar["ts"])  # type: ignore[attr-defined]

    old_envs = [call.args[0] for call in old_ctx.submit_intent.await_args_list]
    new_envs = [call.args[0] for call in v2_ctx.submit_intent.await_args_list]
    diff = OptionParityRunner().compare_envelopes(old_envs, new_envs)
    return {
        "matched": bool(diff.get("matched")),
        "diff": diff,
        "old_count": len(old_envs),
        "new_count": len(new_envs),
        "new_kernel": new.kernel.state_snapshot(),
        "old_envs": old_envs,
        "new_envs": new_envs,
    }


def run_minute_session_parity_scenario_sync(scenario: Dict[str, Any]) -> Dict[str, Any]:
    return asyncio.run(run_minute_session_parity_scenario(scenario))


async def run_clickhouse_day_parity_scenario(scenario: Dict[str, Any]) -> Dict[str, Any]:
    day = str(scenario["day"])
    symbol = str(scenario.get("symbol") or "QQQ").strip().upper()
    session_mode = str(scenario.get("session_mode") or "rth").strip().lower()
    bars = _load_day_bars_from_clickhouse(symbol=symbol, day=day, session_mode=session_mode)
    merged = dict(scenario)
    merged["bars"] = bars
    merged.setdefault("seed_bars", [])
    return await run_minute_session_parity_scenario(merged)


def run_clickhouse_day_parity_scenario_sync(scenario: Dict[str, Any]) -> Dict[str, Any]:
    return asyncio.run(run_clickhouse_day_parity_scenario(scenario))
