#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_veto_logic.py
==================
快速测试：对称VETO逻辑（SHORT + LONG）

测试目标：
1. 验证2026-02-23（今天）的VETO触发情况
2. 验证2026-02-20的VETO触发情况
3. 对比：有VETO vs 无VETO的入场信号差异
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


@dataclass
class VetoCheckResult:
    """VETO检查结果"""
    triggered: bool
    reason: str
    details: dict


def load_1m_bars_from_clickhouse(day: str) -> list[Tick]:
    """从ClickHouse加载1分钟K线"""
    client = get_client()
    rows = client.query(
        """
        SELECT ts_start, close, volume
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

        px = float(row.get("close", 0.0))
        vol = float(row.get("volume", 0.0))

        if px > 0:
            ticks.append(Tick(ts=ts, close=px, volume=vol))

    return ticks


def calculate_atr_1m(closes: list[float], highs: list[float], lows: list[float], period: int = 14) -> Optional[float]:
    """计算1分钟ATR"""
    if len(closes) < period + 1:
        return None

    trs = []
    for i in range(len(closes) - period, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1])
        )
        trs.append(tr)

    return sum(trs) / len(trs) if trs else None


def check_short_veto(
    close_now: float,
    close_1m_ago: float,
    low_5m: float,
    t_low_5m: datetime,
    now: datetime,
    atr_live: float,
    veto_window: tuple[time, time] = (time(9, 40), time(11, 50)),
) -> VetoCheckResult:
    """
    SHORT VETO检查

    条件（AND）：
    1. dist_from_low <= 0.30 ATR
    2. bounce_strength >= 0.35 ATR
    3. time_since_low <= 120秒
    """
    veto_start, veto_end = veto_window

    # 时间窗口检查
    if not (veto_start <= now.time() <= veto_end):
        return VetoCheckResult(
            triggered=False,
            reason="outside_veto_window",
            details={"now_time": now.time()}
        )

    # 计算三个条件
    dist_from_low_atr = (close_now - low_5m) / atr_live
    chg_1m = close_now - close_1m_ago
    bounce_strength_atr = chg_1m / atr_live
    time_since_low_sec = (now - t_low_5m).total_seconds()

    # 判断
    cond1 = dist_from_low_atr <= 0.30
    cond2 = bounce_strength_atr >= 0.35
    cond3 = time_since_low_sec <= 120

    triggered = cond1 and cond2 and cond3

    return VetoCheckResult(
        triggered=triggered,
        reason="a_cont_short_bounce_veto" if triggered else "pass",
        details={
            "dist_from_low_atr": round(dist_from_low_atr, 3),
            "bounce_strength_atr": round(bounce_strength_atr, 3),
            "time_since_low_sec": round(time_since_low_sec, 1),
            "cond1_near_low": cond1,
            "cond2_bouncing": cond2,
            "cond3_recent": cond3,
            "low_5m": round(low_5m, 2),
            "close_now": round(close_now, 2),
            "close_1m_ago": round(close_1m_ago, 2),
        }
    )


def check_long_veto(
    close_now: float,
    close_1m_ago: float,
    high_5m: float,
    t_high_5m: datetime,
    now: datetime,
    atr_live: float,
    veto_window: tuple[time, time] = (time(9, 40), time(11, 50)),
) -> VetoCheckResult:
    """
    LONG VETO检查（对称）

    条件（AND）：
    1. dist_from_high <= 0.30 ATR
    2. pullback_strength >= 0.35 ATR
    3. time_since_high <= 120秒
    """
    veto_start, veto_end = veto_window

    if not (veto_start <= now.time() <= veto_end):
        return VetoCheckResult(
            triggered=False,
            reason="outside_veto_window",
            details={"now_time": now.time()}
        )

    dist_from_high_atr = (high_5m - close_now) / atr_live
    chg_1m = close_now - close_1m_ago
    pullback_strength_atr = (-chg_1m) / atr_live  # 注意负号
    time_since_high_sec = (now - t_high_5m).total_seconds()

    cond1 = dist_from_high_atr <= 0.30
    cond2 = pullback_strength_atr >= 0.35
    cond3 = time_since_high_sec <= 120

    triggered = cond1 and cond2 and cond3

    return VetoCheckResult(
        triggered=triggered,
        reason="a_cont_long_pullback_veto" if triggered else "pass",
        details={
            "dist_from_high_atr": round(dist_from_high_atr, 3),
            "pullback_strength_atr": round(pullback_strength_atr, 3),
            "time_since_high_sec": round(time_since_high_sec, 1),
            "cond1_near_high": cond1,
            "cond2_pulling_back": cond2,
            "cond3_recent": cond3,
            "high_5m": round(high_5m, 2),
            "close_now": round(close_now, 2),
            "close_1m_ago": round(close_1m_ago, 2),
        }
    )


def simulate_day(day: str, enable_veto: bool = True) -> dict:
    """模拟一天的交易，统计VETO触发情况"""
    print(f"\n{'='*80}")
    print(f"模拟日期: {day} | VETO: {'启用' if enable_veto else '禁用'}")
    print(f"{'='*80}")

    ticks = load_1m_bars_from_clickhouse(day)

    if not ticks:
        print(f"❌ 没有数据")
        return {}

    print(f"✓ 加载 {len(ticks)} 根1分钟K线")

    # 统计
    stats = {
        "day": day,
        "total_minutes": len(ticks),
        "veto_enabled": enable_veto,
        "short_veto_triggers": [],
        "long_veto_triggers": [],
        "potential_signals": [],
    }

    # 模拟逐分钟评估
    for i in range(14, len(ticks)):  # 需要至少14根K线计算ATR
        now_tick = ticks[i]
        now = now_tick.ts
        close_now = now_tick.close

        # 计算ATR（简化：用close作为high/low）
        closes = [t.close for t in ticks[max(0, i-15):i+1]]
        atr_live = calculate_atr_1m(closes, closes, closes, period=14)

        if not atr_live or atr_live <= 0:
            continue

        # 1分钟前
        close_1m_ago = ticks[i-1].close if i >= 1 else close_now

        # 5分钟窗口
        window_5m_start = i - 5
        if window_5m_start < 0:
            continue

        prices_5m = [t.close for t in ticks[window_5m_start:i+1]]
        low_5m = min(prices_5m)
        high_5m = max(prices_5m)

        # 找到低点/高点的时间
        t_low_5m = None
        t_high_5m = None
        for t in ticks[window_5m_start:i+1]:
            if t.close == low_5m and t_low_5m is None:
                t_low_5m = t.ts
            if t.close == high_5m and t_high_5m is None:
                t_high_5m = t.ts

        if t_low_5m is None:
            t_low_5m = now
        if t_high_5m is None:
            t_high_5m = now

        # 简化的方向判断（这里只检查VETO，不做完整策略）
        chg_1m = close_now - close_1m_ago

        # SHORT方向
        if chg_1m > 0:  # 1分钟上涨（可能是反弹）
            if enable_veto:
                veto_result = check_short_veto(
                    close_now, close_1m_ago, low_5m, t_low_5m, now, atr_live
                )
                if veto_result.triggered:
                    stats["short_veto_triggers"].append({
                        "time": now.strftime("%H:%M"),
                        "details": veto_result.details
                    })
                    print(f"  ⚠️  {now.strftime('%H:%M')} SHORT VETO触发: {veto_result.details}")

        # LONG方向
        if chg_1m < 0:  # 1分钟下跌（可能是回调）
            if enable_veto:
                veto_result = check_long_veto(
                    close_now, close_1m_ago, high_5m, t_high_5m, now, atr_live
                )
                if veto_result.triggered:
                    stats["long_veto_triggers"].append({
                        "time": now.strftime("%H:%M"),
                        "details": veto_result.details
                    })
                    print(f"  ⚠️  {now.strftime('%H:%M')} LONG VETO触发: {veto_result.details}")

    print(f"\n总结:")
    print(f"  SHORT VETO触发: {len(stats['short_veto_triggers'])} 次")
    print(f"  LONG VETO触发: {len(stats['long_veto_triggers'])} 次")

    return stats


def main():
    print("🧪 测试对称VETO逻辑")
    print("="*80)

    test_days = [
        "2026-02-23",  # 今天
        "2026-02-20",  # 2月20日
    ]

    results = {}

    for day in test_days:
        # 测试启用VETO
        stats_with_veto = simulate_day(day, enable_veto=True)
        results[f"{day}_with_veto"] = stats_with_veto

    # 生成对比报告
    print(f"\n{'='*80}")
    print("📊 对比报告")
    print(f"{'='*80}")

    for day in test_days:
        key = f"{day}_with_veto"
        if key not in results:
            continue

        stats = results[key]
        print(f"\n{day}:")
        print(f"  SHORT VETO: {len(stats.get('short_veto_triggers', []))} 次")
        print(f"  LONG VETO: {len(stats.get('long_veto_triggers', []))} 次")

        # 显示具体触发时间
        if stats.get('short_veto_triggers'):
            print(f"  SHORT VETO触发时刻:")
            for trigger in stats['short_veto_triggers'][:5]:  # 最多显示5个
                print(f"    - {trigger['time']}: dist={trigger['details']['dist_from_low_atr']:.3f}, "
                      f"bounce={trigger['details']['bounce_strength_atr']:.3f}, "
                      f"time_since={trigger['details']['time_since_low_sec']:.1f}s")

        if stats.get('long_veto_triggers'):
            print(f"  LONG VETO触发时刻:")
            for trigger in stats['long_veto_triggers'][:5]:
                print(f"    - {trigger['time']}: dist={trigger['details']['dist_from_high_atr']:.3f}, "
                      f"pullback={trigger['details']['pullback_strength_atr']:.3f}, "
                      f"time_since={trigger['details']['time_since_high_sec']:.1f}s")

    print(f"\n{'='*80}")
    print("✅ 测试完成")


if __name__ == "__main__":
    main()
