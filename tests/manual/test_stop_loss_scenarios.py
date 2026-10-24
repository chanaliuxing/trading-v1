#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_stop_loss_scenarios.py
===========================
测试2月23日在不同止损策略下的表现

场景对比：
1. 当前策略: 硬止损2.5 ATR, logic_retreat 6分钟
2. 放宽止损: 硬止损3.0 ATR, logic_retreat 6分钟
3. 取消硬止损: 仅依赖logic_retreat
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, time
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
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class ScenarioResult:
    scenario_name: str
    exit_time: datetime
    exit_reason: str
    exit_px: float
    pnl_qqq: float
    pnl_sqqq: float
    minutes_held: int


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
            open=float(row.get("open", 0.0)),
            high=float(row.get("high", 0.0)),
            low=float(row.get("low", 0.0)),
            close=float(row.get("close", 0.0)),
            volume=float(row.get("volume", 0.0)),
        ))

    return ticks


def calculate_vwap(ticks: list[Tick]) -> float:
    """计算VWAP"""
    sum_pv = sum((t.high + t.low + t.close) / 3.0 * t.volume for t in ticks)
    sum_v = sum(t.volume for t in ticks)
    return sum_pv / sum_v if sum_v > 0 else 0.0


def get_sqqq_price(qqq_px: float, qqq_ref: float = 600.0, sqqq_ref: float = 8.0) -> float:
    """QQQ价格推算SQQQ价格"""
    delta_q = (qqq_px - qqq_ref) / qqq_ref
    return sqqq_ref * (1 - 3 * delta_q)


def check_logic_retreat(
    entry_px: float,
    current_px: float,
    vwap: float,
    direction: str,
) -> bool:
    """
    Logic Retreat检查（基于策略代码）

    条件：
    1. 持仓>=6分钟
    2. 动量反转（QQQ 3分钟方向与持仓方向相反）
    3. 价格在VWAP错误侧
    """
    # 简化版：只检查价格vs VWAP
    if direction == "SHORT":
        # 做空QQQ，期待QQQ下跌，VWAP上方
        # 如果价格回到VWAP上方，说明反转
        wrong_side = current_px > vwap
    else:  # LONG
        wrong_side = current_px < vwap

    return wrong_side


def simulate_scenario(
    ticks: list[Tick],
    entry_idx: int,
    atr: float,
    stop_atr_mult: float,
    enable_hard_stop: bool,
    enable_logic_retreat: bool,
    logic_retreat_min_hold: int = 6,
) -> ScenarioResult:
    """
    模拟单个场景

    Args:
        stop_atr_mult: 硬止损倍数（2.5或3.0）
        enable_hard_stop: 是否启用硬止损
        enable_logic_retreat: 是否启用logic_retreat
        logic_retreat_min_hold: logic_retreat最小持仓分钟数
    """
    entry_tick = ticks[entry_idx]
    entry_px = entry_tick.close
    entry_time = entry_tick.ts

    # 计算止损/目标
    stop_dist = stop_atr_mult * atr
    target_dist = 1.5 * atr

    # SHORT: 止损=entry+dist, 目标=entry-dist
    hard_stop_px = entry_px + stop_dist
    target_px = entry_px - target_dist

    # 模拟逐分钟
    for i in range(entry_idx + 1, len(ticks)):
        tick = ticks[i]
        minutes_held = i - entry_idx

        # 1. 硬止损检查
        if enable_hard_stop and tick.high >= hard_stop_px:
            sqqq_entry = get_sqqq_price(entry_px)
            sqqq_exit = get_sqqq_price(hard_stop_px)
            return ScenarioResult(
                scenario_name="",
                exit_time=tick.ts,
                exit_reason=f"hard_stop (min={minutes_held})",
                exit_px=hard_stop_px,
                pnl_qqq=round(hard_stop_px - entry_px, 2),
                pnl_sqqq=round((sqqq_exit - sqqq_entry) * 15296, 2),
                minutes_held=minutes_held,
            )

        # 2. 目标检查
        if tick.low <= target_px:
            sqqq_entry = get_sqqq_price(entry_px)
            sqqq_exit = get_sqqq_price(target_px)
            return ScenarioResult(
                scenario_name="",
                exit_time=tick.ts,
                exit_reason=f"target_hit (min={minutes_held})",
                exit_px=target_px,
                pnl_qqq=round(target_px - entry_px, 2),
                pnl_sqqq=round((sqqq_exit - sqqq_entry) * 15296, 2),
                minutes_held=minutes_held,
            )

        # 3. Logic Retreat检查（持仓>=6分钟）
        if enable_logic_retreat and minutes_held >= logic_retreat_min_hold:
            vwap = calculate_vwap(ticks[:i+1])
            if check_logic_retreat(entry_px, tick.close, vwap, "SHORT"):
                sqqq_entry = get_sqqq_price(entry_px)
                sqqq_exit = get_sqqq_price(tick.close)
                return ScenarioResult(
                    scenario_name="",
                    exit_time=tick.ts,
                    exit_reason=f"logic_retreat (min={minutes_held})",
                    exit_px=tick.close,
                    pnl_qqq=round(tick.close - entry_px, 2),
                    pnl_sqqq=round((sqqq_exit - sqqq_entry) * 15296, 2),
                    minutes_held=minutes_held,
                )

        # 4. EOD检查
        if tick.ts.time() >= time(15, 59):
            sqqq_entry = get_sqqq_price(entry_px)
            sqqq_exit = get_sqqq_price(tick.close)
            return ScenarioResult(
                scenario_name="",
                exit_time=tick.ts,
                exit_reason=f"eod (min={minutes_held})",
                exit_px=tick.close,
                pnl_qqq=round(tick.close - entry_px, 2),
                pnl_sqqq=round((sqqq_exit - sqqq_entry) * 15296, 2),
                minutes_held=minutes_held,
            )

    # 未出场
    last_tick = ticks[-1]
    minutes_held = len(ticks) - entry_idx - 1
    sqqq_entry = get_sqqq_price(entry_px)
    sqqq_exit = get_sqqq_price(last_tick.close)
    return ScenarioResult(
        scenario_name="",
        exit_time=last_tick.ts,
        exit_reason=f"no_exit (min={minutes_held})",
        exit_px=last_tick.close,
        pnl_qqq=round(last_tick.close - entry_px, 2),
        pnl_sqqq=round((sqqq_exit - sqqq_entry) * 15296, 2),
        minutes_held=minutes_held,
    )


def main():
    day = "2026-02-23"
    entry_time_target = datetime(2026, 2, 23, 9, 45, tzinfo=ET)

    print("🧪 2月23日止损策略对比测试")
    print("="*90)

    # 加载数据
    ticks = load_1m_bars(day)

    # 找到入场时刻
    entry_idx = None
    for i, tick in enumerate(ticks):
        if tick.ts == entry_time_target:
            entry_idx = i
            break

    if entry_idx is None:
        print("❌ 未找到入场时刻")
        return

    print(f"入场时刻: {ticks[entry_idx].ts.strftime('%H:%M')} @ QQQ ${ticks[entry_idx].close:.2f}")
    print(f"ATR: 0.92 (已知)")
    print("")

    # 场景1: 当前策略（硬止损2.5 ATR + logic_retreat 6分钟）
    result1 = simulate_scenario(
        ticks, entry_idx, atr=0.92,
        stop_atr_mult=2.5,
        enable_hard_stop=True,
        enable_logic_retreat=True,
        logic_retreat_min_hold=6,
    )
    result1.scenario_name = "场景1: 当前策略 (2.5 ATR硬止损 + 6分钟retreat)"

    # 场景2: 放宽硬止损（3.0 ATR + logic_retreat 6分钟）
    result2 = simulate_scenario(
        ticks, entry_idx, atr=0.92,
        stop_atr_mult=3.0,
        enable_hard_stop=True,
        enable_logic_retreat=True,
        logic_retreat_min_hold=6,
    )
    result2.scenario_name = "场景2: 放宽硬止损 (3.0 ATR硬止损 + 6分钟retreat)"

    # 场景3: 取消硬止损（仅logic_retreat）
    result3 = simulate_scenario(
        ticks, entry_idx, atr=0.92,
        stop_atr_mult=999.0,  # 极高，相当于禁用
        enable_hard_stop=False,
        enable_logic_retreat=True,
        logic_retreat_min_hold=6,
    )
    result3.scenario_name = "场景3: 取消硬止损 (仅6分钟retreat)"

    # 场景4: 仅硬止损（3.0 ATR，不依赖retreat）
    result4 = simulate_scenario(
        ticks, entry_idx, atr=0.92,
        stop_atr_mult=3.0,
        enable_hard_stop=True,
        enable_logic_retreat=False,
    )
    result4.scenario_name = "场景4: 仅硬止损 (3.0 ATR, 无retreat)"

    # 打印结果
    results = [result1, result2, result3, result4]

    print("="*90)
    print("对比结果:")
    print("="*90)
    print(f"{'场景':<45} {'出场时间':<10} {'持仓':<8} {'出场原因':<20} {'SQQQ盈亏':<12}")
    print("="*90)

    for result in results:
        print(f"{result.scenario_name:<45} "
              f"{result.exit_time.strftime('%H:%M'):<10} "
              f"{result.minutes_held}分钟   "
              f"{result.exit_reason:<20} "
              f"${result.pnl_sqqq:>+10,.2f}")

    print("")
    print("="*90)
    print("关键发现:")
    print("="*90)

    # 找到最佳场景
    best_result = max(results, key=lambda r: r.pnl_sqqq)
    worst_result = min(results, key=lambda r: r.pnl_sqqq)

    print(f"✅ 最佳场景: {best_result.scenario_name}")
    print(f"   出场: {best_result.exit_time.strftime('%H:%M')} ({best_result.exit_reason})")
    print(f"   盈亏: ${best_result.pnl_sqqq:+,.2f}")
    print("")
    print(f"❌ 最差场景: {worst_result.scenario_name}")
    print(f"   出场: {worst_result.exit_time.strftime('%H:%M')} ({worst_result.exit_reason})")
    print(f"   盈亏: ${worst_result.pnl_sqqq:+,.2f}")
    print("")
    print(f"💡 收益差异: ${best_result.pnl_sqqq - worst_result.pnl_sqqq:+,.2f}")

    # 分析结论
    print("")
    print("="*90)
    print("结论:")
    print("="*90)

    if result1.exit_reason.startswith("hard_stop") and result1.minutes_held < 6:
        print("⚠️  场景1（当前策略）在第6分钟前被硬止损踢出")
        print("    → 问题：硬止损(2.5 ATR)太紧，没给logic_retreat机会")

    if result2.pnl_sqqq > result1.pnl_sqqq:
        print(f"✅ 场景2（放宽到3.0 ATR）比当前策略好 ${result2.pnl_sqqq - result1.pnl_sqqq:+,.2f}")

    if result3.pnl_sqqq > result1.pnl_sqqq:
        print(f"✅ 场景3（取消硬止损）比当前策略好 ${result3.pnl_sqqq - result1.pnl_sqqq:+,.2f}")
        print("    → 建议：logic_retreat足够，可以取消或放宽硬止损")


if __name__ == "__main__":
    main()
