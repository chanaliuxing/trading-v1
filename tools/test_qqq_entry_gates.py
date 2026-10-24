#!/usr/bin/env python3
"""
QQQ entry-gate smoke test for the current intraday strategy.

Scenarios:
1) blocked_data_not_ready
2) entry_allowed
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from libs.strategy import StrategyContext, StrategyParams
import strategies.qqq_intraday_strategy as qmod
from strategies.qqq_intraday_strategy import ET, QQQIntradayStrategy


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


@dataclass
class Result:
    scenario: str
    decision: str
    reason: str
    direction: str


def _make_strategy(diag_path: Path) -> QQQIntradayStrategy:
    ctx = StrategyContext(
        engine=_DummyEngine(),
        strategy_id="qqq_intraday_001",
        logger=_DummyLogger(),
        metrics=lambda *args, **kwargs: None,
    )
    params = StrategyParams()
    params.extras["qty"] = 1
    params.extras["freshness_max_sec"] = 5
    params.extras["freshness_max_sec_ad_nasd"] = 8
    params.extras["atr_period_1m"] = 3
    s = QQQIntradayStrategy(ctx, params)
    s._diag_path = diag_path
    return s


def _read_last_entry_gate(diag_path: Path) -> dict[str, Any]:
    lines = diag_path.read_text(encoding="utf-8").strip().splitlines()
    for line in reversed(lines):
        rec = json.loads(line)
        if rec.get("event") == "entry_gate":
            return rec
    raise RuntimeError("no entry_gate record found")


def _seed_for_allowed(strategy: QQQIntradayStrategy, now: datetime) -> None:
    now_ms = int(now.timestamp() * 1000)
    strategy._last_seen_ts_ms["QQQ"] = now_ms - 1000
    strategy._last_seen_ts_ms["QQEW"] = now_ms - 1000
    strategy._last_seen_ts_ms["TQQQ"] = now_ms - 1000
    strategy._last_seen_ts_ms["SQQQ"] = now_ms - 1000

    # 180s continuous buffers
    for i in range(181):
        ts = now_ms - (180 - i) * 1000
        qqq_px = 611.0 if i > 10 else 610.0
        qqew_px = 134.5 if i > 10 else 133.0
        strategy._qqq_buffer.append((ts, qqq_px))
        strategy._qqew_buffer.append((ts, qqew_px))

    strategy._atr_pre["TQQQ"] = 0.35
    strategy.params.extras["atr_period_1m"] = 3
    strategy._minute_bars.extend(
        [
            qmod._OhlcvBar(now - qmod.timedelta(minutes=4), now - qmod.timedelta(minutes=3), 608.0, 609.0, 607.0, 608.5, 100.0),
            qmod._OhlcvBar(now - qmod.timedelta(minutes=3), now - qmod.timedelta(minutes=2), 608.5, 610.0, 608.0, 609.5, 100.0),
            qmod._OhlcvBar(now - qmod.timedelta(minutes=2), now - qmod.timedelta(minutes=1), 609.5, 610.5, 609.0, 610.2, 100.0),
            qmod._OhlcvBar(now - qmod.timedelta(minutes=1), now, 610.2, 611.0, 610.0, 610.8, 100.0),
        ]
    )
    strategy._rolling_vol_queue.extend([100.0] * 10)
    strategy._today_vol_series.extend([100.0] * 20)
    strategy._min_bar = qmod._OhlcvBar(
        ts_start=now.replace(second=0, microsecond=0),
        ts_end=now.replace(second=0, microsecond=0) + qmod.timedelta(minutes=1),
        open=611.0,
        high=612.0,
        low=610.8,
        close=611.0,
        volume=300.0,
    )
    strategy._vwap_pv = 609.5 * 100.0
    strategy._vwap_v = 100.0
    strategy._qqew_last_price = 134.5
    strategy._qqew_vwap_pv = 133.0 * 100.0
    strategy._qqew_vwap_v = 100.0
    # Provide executable symbol last prices so _maybe_enter can proceed beyond ENTRY_ALLOWED.
    start_5m = now.replace(minute=(now.minute // 5) * 5, second=0, microsecond=0)
    strategy._cur_5m["TQQQ"] = qmod._OhlcvBar(start_5m, start_5m + qmod.timedelta(minutes=5), 80.0, 80.2, 79.8, 80.0, 1000.0)
    strategy._cur_5m["SQQQ"] = qmod._OhlcvBar(start_5m, start_5m + qmod.timedelta(minutes=5), 65.0, 65.2, 64.8, 65.0, 1000.0)


async def _run_scenario(name: str, diag_path: Path) -> Result:
    if diag_path.exists():
        diag_path.unlink()
    strategy = _make_strategy(diag_path)
    now = datetime(2026, 2, 9, 10, 2, 5, tzinfo=ET)

    async def _fake_submit(symbol: str, qty: int) -> None:
        return None

    async def _fake_stop(reason: str) -> None:
        return None

    strategy._submit_target = _fake_submit  # type: ignore[method-assign]
    strategy._submit_protective_stop_intent = _fake_stop  # type: ignore[method-assign]

    if name == "entry_allowed":
        _seed_for_allowed(strategy, now)
    elif name == "blocked_data_not_ready":
        pass
    else:
        raise ValueError(name)

    await strategy._maybe_enter(now)
    rec = _read_last_entry_gate(diag_path)
    return Result(
        scenario=name,
        decision=str(rec.get("decision")),
        reason=str(rec.get("reason")),
        direction=str(rec.get("direction")),
    )


async def _main(scenario: str, out: Path) -> int:
    results: list[Result] = []
    scenarios = [scenario] if scenario != "both" else ["blocked_data_not_ready", "entry_allowed"]
    for s in scenarios:
        r = await _run_scenario(s, out)
        results.append(r)
    print(json.dumps([r.__dict__ for r in results], ensure_ascii=False, indent=2))
    ok = any(r.scenario == "entry_allowed" and r.decision == "ENTRY_ALLOWED" for r in results)
    return 0 if ok else 2


def main() -> None:
    ap = argparse.ArgumentParser(description="Smoke-test QQQ entry gate scenarios.")
    ap.add_argument("--scenario", choices=["blocked_data_not_ready", "entry_allowed", "both"], default="both")
    ap.add_argument("--out", default="runtime/state/qqq_entry_gate_smoke.jsonl")
    args = ap.parse_args()
    rc = asyncio.run(_main(args.scenario, Path(args.out)))
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
