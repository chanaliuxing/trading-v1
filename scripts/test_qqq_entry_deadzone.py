#!/usr/bin/env python3
"""
Lightweight regression script for QQQ intraday entry gates.

Scenarios covered:
1. Direction mismatch should be blocked.
2. Velocity too low should be blocked.
3. Strong directional velocity should pass entry gating.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from libs.strategy import StrategyContext, StrategyParams
from strategies.qqq_intraday_strategy import ET, QQQIntradayStrategy, _OhlcvBar


class _DummyLogger:
    def info(self, *args, **kwargs):
        return None

    def warning(self, *args, **kwargs):
        return None

    def error(self, *args, **kwargs):
        return None

    def debug(self, *args, **kwargs):
        return None


class _DummyEngine:
    pass


def _make_strategy(diag_path: Path) -> QQQIntradayStrategy:
    ctx = StrategyContext(
        engine=_DummyEngine(),
        strategy_id="qqq_intraday_001",
        logger=_DummyLogger(),
        metrics=lambda *args, **kwargs: None,
    )
    params = StrategyParams()
    params.extras["freshness_max_sec"] = 5.0
    params.extras["freshness_max_sec_qqew"] = 5.0
    params.extras["entry_max_data_age_sec"] = 5.0
    params.extras["atr_period_1m"] = 3
    strategy = QQQIntradayStrategy(ctx, params)
    strategy._diag_path = diag_path
    # Keep this script focused on entry gate behavior, not order plumbing.
    strategy._submit_target = _noop_submit_target  # type: ignore[method-assign]
    strategy._submit_protective_stop_intent = _noop_submit_stop  # type: ignore[method-assign]
    # Decouple from minute bars; use fixed ATR for deterministic thresholds.
    strategy._atr_1m_qqq = lambda period=14: 0.30  # type: ignore[method-assign]
    return strategy


async def _noop_submit_target(symbol: str, qty: int) -> None:
    return


async def _noop_submit_stop(reason: str) -> None:
    return


def _prime_common_state(strategy: QQQIntradayStrategy, now: datetime) -> None:
    now_ms = int(now.timestamp() * 1000)
    strategy._day = now.strftime("%Y-%m-%d")
    strategy._is_traded = False
    strategy._side = None
    strategy._exec_symbol = None
    strategy._last_entry_eval_second = None
    strategy._last_entry_block_minute = None
    strategy._success_count = 0
    strategy._pending_decision_id = None

    # Freshness marks
    strategy._last_seen_ts_ms["QQQ"] = now_ms
    strategy._last_seen_ts_ms["QQEW"] = now_ms
    strategy._last_seen_ts_ms["SQQQ"] = now_ms
    strategy._last_seen_ts_ms["TQQQ"] = now_ms

    # Rolling volume stats
    strategy._rolling_vol_queue.clear()
    strategy._rolling_vol_queue.extend([100.0] * 10)
    strategy._today_vol_series.clear()
    strategy._today_vol_series.extend([100.0] * 20)

    # ATR seed is required by entry gate.
    strategy._atr_pre["TQQQ"] = 0.20

    # Default VWAPs (can be overridden per scenario)
    strategy._vwap_pv = 609.0 * 1000.0
    strategy._vwap_v = 1000.0
    strategy._qqew_vwap_pv = 133.0 * 1000.0
    strategy._qqew_vwap_v = 1000.0

    # QQQ and QQEW last prices
    strategy._min_bar = _OhlcvBar(
        ts_start=now.replace(second=0, microsecond=0),
        ts_end=now.replace(second=0, microsecond=0) + timedelta(minutes=1),
        open=611.0,
        high=611.0,
        low=611.0,
        close=611.0,
        volume=1000.0,
    )
    strategy._qqew_last_price = 134.5

    # Exec symbol price and ATR
    strategy._cur_5m["SQQQ"] = _OhlcvBar(
        ts_start=now.replace(minute=(now.minute // 5) * 5, second=0, microsecond=0),
        ts_end=now.replace(minute=(now.minute // 5) * 5, second=0, microsecond=0) + timedelta(minutes=5),
        open=65.0,
        high=65.0,
        low=64.8,
        close=65.0,
        volume=1000.0,
    )
    strategy._cur_5m["TQQQ"] = _OhlcvBar(
        ts_start=now.replace(minute=(now.minute // 5) * 5, second=0, microsecond=0),
        ts_end=now.replace(minute=(now.minute // 5) * 5, second=0, microsecond=0) + timedelta(minutes=5),
        open=80.0,
        high=80.2,
        low=79.8,
        close=80.0,
        volume=1000.0,
    )
    strategy._atr_pre["SQQQ"] = 0.25

    # Continuous 180s buffers (needed by gate)
    strategy._qqq_buffer.clear()
    strategy._qqew_buffer.clear()
    for i in range(181):
        ts = now_ms - (180 - i) * 1000
        # Default: strong LONG (1.0 move over 3m)
        qqq_px = 610.0 + (i / 180.0) * 1.0
        qqew_px = 133.0 + (i / 180.0) * 1.5
        strategy._qqq_buffer.append((ts, qqq_px))
        strategy._qqew_buffer.append((ts, qqew_px))


def _read_last_reason(diag_path: Path) -> str:
    lines = [x for x in diag_path.read_text(encoding="utf-8").splitlines() if x.strip()]
    for line in reversed(lines):
        obj = json.loads(line)
        if obj.get("event") == "entry_gate":
            return str(obj.get("reason") or "")
    return ""


async def _run() -> None:
    with tempfile.TemporaryDirectory(prefix="qqq_deadzone_test_") as td:
        diag = Path(td) / "diag.jsonl"

        # 1) direction mismatch should be blocked
        s1 = _make_strategy(diag)
        now1 = datetime(2026, 2, 10, 10, 5, 0, tzinfo=ET)
        _prime_common_state(s1, now1)
        # qqq above vwap, qqew below vwap
        s1._min_bar.close = 611.0
        s1._vwap_pv = 609.0 * 1000.0
        s1._vwap_v = 1000.0
        s1._qqew_last_price = 132.0
        s1._qqew_vwap_pv = 133.0 * 1000.0
        s1._qqew_vwap_v = 1000.0
        await s1._maybe_enter(now1)
        r1 = _read_last_reason(diag)
        assert r1 == "direction_mismatch", f"expected direction_mismatch, got={r1}"

        # 2) velocity too low should be blocked (threshold floor tv_min_usd=0.20)
        s2 = _make_strategy(diag)
        now2 = datetime(2026, 2, 10, 10, 6, 0, tzinfo=ET)
        _prime_common_state(s2, now2)
        # Keep direction aligned LONG, but force v3m_q very small (0.05).
        now2_ms = int(now2.timestamp() * 1000)
        s2._qqq_buffer.clear()
        s2._qqew_buffer.clear()
        for i in range(181):
            ts = now2_ms - (180 - i) * 1000
            qqq_px = 611.00 + (i / 180.0) * 0.05
            qqew_px = 133.00 + (i / 180.0) * 0.10
            s2._qqq_buffer.append((ts, qqq_px))
            s2._qqew_buffer.append((ts, qqew_px))
        s2._min_bar.close = 611.05
        s2._qqew_last_price = 133.10
        s2._vwap_pv = 609.0 * 1000.0
        s2._vwap_v = 1000.0
        s2._qqew_vwap_pv = 133.0 * 1000.0
        s2._qqew_vwap_v = 1000.0
        await s2._maybe_enter(now2)
        r2 = _read_last_reason(diag)
        assert r2 == "velocity_too_low", f"expected velocity_too_low, got={r2}"

        # 3) strong velocity should enter (LONG -> TQQQ)
        s3 = _make_strategy(diag)
        now3 = datetime(2026, 2, 10, 10, 7, 0, tzinfo=ET)
        _prime_common_state(s3, now3)
        await s3._maybe_enter(now3)
        assert s3._is_traded is True, "expected strategy to enter"
        assert s3._exec_symbol == "TQQQ", f"expected TQQQ entry, got={s3._exec_symbol}"

        print("PASS")
        print(" - direction mismatch block: ok")
        print(" - velocity too low block: ok")
        print(" - strong directional entry: ok")


if __name__ == "__main__":
    asyncio.run(_run())
