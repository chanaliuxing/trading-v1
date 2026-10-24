#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_continuation_fast_drift.py
================================
测试新的 A_CONTINUATION = FAST ∪ DRIFT 逻辑

FAST: 爆发型（急拉/急杀/放量）
DRIFT: 持续型（匀速单边/连续3根方向一致）
"""

from __future__ import annotations

import sys
from collections import deque
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from libs.db.ch_pool import get_client
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


@dataclass
class Tick:
    ts: datetime
    close: float
    volume: float
    high: float = 0.0
    low: float = 0.0


@dataclass
class Signal:
    """入场信号"""
    time: str
    path: str  # A_CONT_FAST or A_CONT_DRIFT
    direction: str  # LONG or SHORT
    details: dict
    veto_blocked: bool = False
    veto_reason: str = ""


def load_1m_bars(day: str) -> list[Tick]:
    """加载1分钟K线"""
    client = get_client()
    rows = client.query(
        """
        SELECT ts_start, open, high, low, close, volume
        FROM bars_1m_ibkr
        WHERE symbol = 'QQQ'
          AND d = toDate(%(day)s)
        ORDER BY ts_start ASC
        """,
        {"day": day},
    )

    ticks = []
    for row in rows:
        ts_raw = row.get("ts_start")
        ts = ts_raw if isinstance(ts_raw, datetime) else datetime.fromisoformat(str(ts_raw))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=ET)
        else:
            ts = ts.astimezone(ET)

        ticks.append(Tick(
            ts=ts,
            close=float(row.get("close", 0.0)),
            volume=float(row.get("volume", 0.0)),
            high=float(row.get("high", 0.0)),
            low=float(row.get("low", 0.0)),
        ))

    return ticks


def calc_atr(ticks: list[Tick], period: int = 14) -> float:
    """计算ATR"""
    if len(ticks) < period + 1:
        return 1.0

    trs = []
    for i in range(len(ticks) - period, len(ticks)):
        tr = max(
            ticks[i].high - ticks[i].low,
            abs(ticks[i].high - ticks[i - 1].close),
            abs(ticks[i].low - ticks[i - 1].close),
        )
        trs.append(tr)

    return sum(trs) / len(trs) if trs else 1.0


def check_a_cont_fast(
    close_now: float,
    close_3m_ago: float,
    close_1m: list[float],  # 最近3根1m收盘价 [now, now-1, now-2]
    vwap: float,
    vol_surge_ratio: float,
    atr: float,
) -> Optional[tuple[str, dict]]:
    """
    A_CONTINUATION_FAST 检查

    条件：
    - abs(v3m_atr) >= 0.60
    - vol_surge_ratio >= 1.2
    - dir1m == dir3m

    返回: (direction, details) or None
    """
    v3m = close_now - close_3m_ago
    v3m_atr = abs(v3m) / atr

    # 动量门
    if v3m_atr < 0.60:
        return None

    # 量能门
    if vol_surge_ratio < 1.2:
        return None

    # 方向一致性
    if len(close_1m) < 3:
        return None

    # dir1m: 最近1分钟方向
    dir1m_up = close_1m[0] > close_1m[1]
    dir1m_down = close_1m[0] < close_1m[1]

    # dir3m: 3分钟方向
    dir3m_up = close_now > close_3m_ago
    dir3m_down = close_now < close_3m_ago

    direction = None
    if dir1m_down and dir3m_down and close_now < vwap:
        direction = "SHORT"
    elif dir1m_up and dir3m_up and close_now > vwap:
        direction = "LONG"

    if direction:
        return direction, {
            "v3m_atr": round(v3m_atr, 3),
            "vol_surge": round(vol_surge_ratio, 2),
            "close_now": round(close_now, 2),
            "close_3m_ago": round(close_3m_ago, 2),
            "vwap": round(vwap, 2),
        }

    return None


def check_a_cont_drift(
    close_1m: list[float],  # 最近4根1m收盘价 [now, now-1, now-2, now-3]
    close_3m_ago: float,
    close_now: float,
    vwap: float,
    atr: float,
) -> Optional[tuple[str, dict]]:
    """
    A_CONTINUATION_DRIFT 检查（持续型）

    SHORT条件：
    - 连续3根1m收盘价逐步降低
    - 每根跌幅 >= 0.15 ATR
    - 三根累计跌幅 >= 0.45 ATR
    - 当前价 < VWAP
    - dir3m == DOWN

    LONG对称
    """
    if len(close_1m) < 4:
        return None

    # 计算3根连续K线的变化
    chg1 = close_1m[0] - close_1m[1]  # 最近1根
    chg2 = close_1m[1] - close_1m[2]  # 前2根
    chg3 = close_1m[2] - close_1m[3]  # 前3根

    chg1_atr = chg1 / atr
    chg2_atr = chg2 / atr
    chg3_atr = chg3 / atr

    cumulative = (chg1 + chg2 + chg3) / atr

    # dir3m
    dir3m_down = close_now < close_3m_ago
    dir3m_up = close_now > close_3m_ago

    # SHORT: 连续下跌
    if (chg1 < 0 and chg2 < 0 and chg3 < 0 and
        abs(chg1_atr) >= 0.15 and abs(chg2_atr) >= 0.15 and abs(chg3_atr) >= 0.15 and
        cumulative <= -0.45 and
        close_now < vwap and
        dir3m_down):

        return "SHORT", {
            "chg1_atr": round(chg1_atr, 3),
            "chg2_atr": round(chg2_atr, 3),
            "chg3_atr": round(chg3_atr, 3),
            "cumulative_atr": round(cumulative, 3),
            "close_now": round(close_now, 2),
            "vwap": round(vwap, 2),
        }

    # LONG: 连续上涨
    if (chg1 > 0 and chg2 > 0 and chg3 > 0 and
        chg1_atr >= 0.15 and chg2_atr >= 0.15 and chg3_atr >= 0.15 and
        cumulative >= 0.45 and
        close_now > vwap and
        dir3m_up):

        return "LONG", {
            "chg1_atr": round(chg1_atr, 3),
            "chg2_atr": round(chg2_atr, 3),
            "chg3_atr": round(chg3_atr, 3),
            "cumulative_atr": round(cumulative, 3),
            "close_now": round(close_now, 2),
            "vwap": round(vwap, 2),
        }

    return None


def check_veto(
    direction: str,
    close_now: float,
    close_1m_ago: float,
    low_5m: float,
    high_5m: float,
    t_low_5m: datetime,
    t_high_5m: datetime,
    now: datetime,
    atr: float,
) -> tuple[bool, str]:
    """
    对称VETO检查（09:40-11:50）

    返回: (blocked, reason)
    """
    # 时间窗口
    if not (time(9, 40) <= now.time() <= time(11, 50)):
        return False, ""

    chg_1m = close_now - close_1m_ago

    # SHORT VETO
    if direction == "SHORT":
        dist_from_low_atr = (close_now - low_5m) / atr
        bounce_strength_atr = chg_1m / atr
        time_since_low_sec = (now - t_low_5m).total_seconds()

        if (dist_from_low_atr <= 0.30 and
            bounce_strength_atr >= 0.35 and
            time_since_low_sec <= 120):
            return True, f"short_bounce_veto(dist={dist_from_low_atr:.2f},bounce={bounce_strength_atr:.2f})"

    # LONG VETO
    elif direction == "LONG":
        dist_from_high_atr = (high_5m - close_now) / atr
        pullback_strength_atr = (-chg_1m) / atr
        time_since_high_sec = (now - t_high_5m).total_seconds()

        if (dist_from_high_atr <= 0.30 and
            pullback_strength_atr >= 0.35 and
            time_since_high_sec <= 120):
            return True, f"long_pullback_veto(dist={dist_from_high_atr:.2f},pullback={pullback_strength_atr:.2f})"

    return False, ""


def simulate_day(day: str) -> dict:
    """模拟一天的交易"""
    print(f"\n{'='*80}")
    print(f"测试日期: {day}")
    print(f"{'='*80}")

    ticks = load_1m_bars(day)

    if not ticks:
        print("❌ 无数据")
        return {}

    print(f"✓ 加载 {len(ticks)} 根1分钟K线")

    # 统计
    stats = {
        "day": day,
        "signals_fast": [],
        "signals_drift": [],
        "veto_blocked": [],
        "total_signals": 0,
    }

    # 计算简单VWAP（累积）
    vwap_pv = 0.0
    vwap_v = 0.0

    # 滚动窗口
    vol_queue = deque(maxlen=10)

    for i in range(20, len(ticks)):
        now_tick = ticks[i]
        now = now_tick.ts
        close_now = now_tick.close
        vol_now = now_tick.volume

        # 只在09:40-14:50检查
        if not (time(9, 40) <= now.time() <= time(14, 50)):
            # 更新VWAP（即使不在交易窗口）
            if vol_now > 0:
                vwap_pv += close_now * vol_now
                vwap_v += vol_now
            vol_queue.append(vol_now)
            continue

        # 计算ATR
        atr = calc_atr(ticks[max(0, i - 14):i + 1], period=14)

        # 更新VWAP
        if vol_now > 0:
            vwap_pv += close_now * vol_now
            vwap_v += vol_now

        vwap = vwap_pv / vwap_v if vwap_v > 0 else close_now

        # 成交量比率
        vol_queue.append(vol_now)
        vol_ma10 = sum(vol_queue) / len(vol_queue) if vol_queue else 1.0
        vol_surge_ratio = vol_now / vol_ma10 if vol_ma10 > 0 else 0.0

        # 3分钟前
        idx_3m_ago = i - 3
        if idx_3m_ago < 0:
            continue
        close_3m_ago = ticks[idx_3m_ago].close

        # 最近4根1m收盘价（用于DRIFT）
        close_1m = [ticks[j].close for j in range(i, max(i - 4, -1), -1)]

        # 5分钟窗口（用于VETO）
        window_5m_start = max(0, i - 5)
        prices_5m = [(ticks[j].ts, ticks[j].close) for j in range(window_5m_start, i + 1)]
        low_5m = min(p[1] for p in prices_5m)
        high_5m = max(p[1] for p in prices_5m)

        t_low_5m = [p[0] for p in prices_5m if p[1] == low_5m][0]
        t_high_5m = [p[0] for p in prices_5m if p[1] == high_5m][0]

        if not isinstance(t_low_5m, datetime):
            t_low_5m = datetime.fromisoformat(str(t_low_5m)).replace(tzinfo=ET)
        if not isinstance(t_high_5m, datetime):
            t_high_5m = datetime.fromisoformat(str(t_high_5m)).replace(tzinfo=ET)

        # ========== 检查 A_CONTINUATION ==========

        signal = None

        # 优先检查 FAST
        fast_result = check_a_cont_fast(
            close_now, close_3m_ago, close_1m[:3], vwap, vol_surge_ratio, atr
        )

        if fast_result:
            direction, details = fast_result
            signal = Signal(
                time=now.strftime("%H:%M"),
                path="A_CONT_FAST",
                direction=direction,
                details=details,
            )

        # 如果FAST未触发，检查DRIFT
        elif len(close_1m) >= 4:
            drift_result = check_a_cont_drift(
                close_1m, close_3m_ago, close_now, vwap, atr
            )

            if drift_result:
                direction, details = drift_result
                signal = Signal(
                    time=now.strftime("%H:%M"),
                    path="A_CONT_DRIFT",
                    direction=direction,
                    details=details,
                )

        # 如果有信号，检查VETO
        if signal:
            close_1m_ago = ticks[i - 1].close if i >= 1 else close_now

            blocked, veto_reason = check_veto(
                signal.direction,
                close_now,
                close_1m_ago,
                low_5m,
                high_5m,
                t_low_5m,
                t_high_5m,
                now,
                atr,
            )

            if blocked:
                signal.veto_blocked = True
                signal.veto_reason = veto_reason
                stats["veto_blocked"].append(signal)
                print(f"  ⚠️  {signal.time} {signal.path} {signal.direction} → VETO阻止: {veto_reason}")
            else:
                if signal.path == "A_CONT_FAST":
                    stats["signals_fast"].append(signal)
                else:
                    stats["signals_drift"].append(signal)

                stats["total_signals"] += 1
                print(f"  ✅ {signal.time} {signal.path} {signal.direction}: {signal.details}")

    # 总结
    print(f"\n总结:")
    print(f"  FAST信号: {len(stats['signals_fast'])} 次")
    print(f"  DRIFT信号: {len(stats['signals_drift'])} 次")
    print(f"  VETO阻止: {len(stats['veto_blocked'])} 次")
    print(f"  净入场: {stats['total_signals']} 次")

    return stats


def main():
    print("🧪 测试 A_CONTINUATION = FAST ∪ DRIFT")
    print("="*80)

    test_days = [
        "2026-02-20",
        "2026-02-23",
    ]

    all_stats = {}

    for day in test_days:
        stats = simulate_day(day)
        all_stats[day] = stats

    # 对比报告
    print(f"\n{'='*80}")
    print("📊 对比报告")
    print(f"{'='*80}")

    for day in test_days:
        if day not in all_stats:
            continue

        stats = all_stats[day]
        print(f"\n{day}:")
        print(f"  FAST: {len(stats.get('signals_fast', []))} 次")
        print(f"  DRIFT: {len(stats.get('signals_drift', []))} 次")
        print(f"  VETO拦截: {len(stats.get('veto_blocked', []))} 次")
        print(f"  总入场: {stats.get('total_signals', 0)} 次")

        # 显示FAST信号详情
        if stats.get('signals_fast'):
            print(f"\n  FAST信号明细:")
            for sig in stats['signals_fast'][:5]:
                print(f"    {sig.time} {sig.direction}: v3m={sig.details.get('v3m_atr')} ATR, "
                      f"vol={sig.details.get('vol_surge')}")

        # 显示DRIFT信号详情
        if stats.get('signals_drift'):
            print(f"\n  DRIFT信号明细:")
            for sig in stats['signals_drift'][:5]:
                print(f"    {sig.time} {sig.direction}: cumulative={sig.details.get('cumulative_atr')} ATR")

    print(f"\n{'='*80}")
    print("✅ 测试完成")


if __name__ == "__main__":
    main()
