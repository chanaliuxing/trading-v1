#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_single_trade_per_day.py
============================
完整回测：每日只开一单，模拟真实出场逻辑

测试逻辑：
1. 入场：使用 A_CONTINUATION (FAST + DRIFT)，每日第一个信号入场
2. 出场：模拟真实策略的止盈止损规则
   - Target Hit: 盈利达到目标（锁定在09:45的target）
   - Stop Hit: 价格触及止损位（ATR动态调整 + breakeven shield）
   - Breakeven Shield: 盈利>=1.0时，止损移至entry+0.20
   - Logic Retreat: 持仓6分钟后，动量反转+价格在VWAP错误侧
   - EOD Flatten: 15:59强制平仓

输出：每日交易详情 + 盈亏
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
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Trade:
    """单笔交易记录"""
    entry_time: datetime
    entry_px: float  # QQQ价格（观察标的）
    entry_mode: str  # "FAST" or "DRIFT"
    entry_direction: str  # "LONG" or "SHORT"
    exit_time: Optional[datetime] = None
    exit_px: Optional[float] = None  # QQQ价格
    exit_reason: Optional[str] = None
    pnl: Optional[float] = None  # QQQ价格差
    stop_px: Optional[float] = None
    target_px: Optional[float] = None
    breakeven_armed: bool = False
    # 实际执行标的
    exec_symbol: Optional[str] = None  # "TQQQ" or "SQQQ"
    exec_entry_px: Optional[float] = None  # 实际买入价格
    exec_exit_px: Optional[float] = None  # 实际卖出价格
    qty: Optional[int] = None  # 购买数量
    notional: Optional[float] = None  # 使用资金
    exec_pnl: Optional[float] = None  # 实际盈亏（3x杠杆）


def load_1m_bars_from_clickhouse(day: str, symbol: str = "QQQ") -> list[Tick]:
    """从ClickHouse加载1分钟K线（完整OHLCV）"""
    client = get_client()
    rows = client.query(
        """
        SELECT ts_start, open, high, low, close, volume
        FROM bars_1m_ibkr
        WHERE symbol = %(symbol)s
          AND d = toDate(%(day)s)
        ORDER BY ts_start ASC
        """,
        {"day": day, "symbol": symbol},
    )

    ticks = []
    for row in rows:
        ts_raw = row.get("ts_start")
        ts = ts_raw if isinstance(ts_raw, datetime) else datetime.fromisoformat(str(ts_raw))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=ET)
        else:
            ts = ts.astimezone(ET)

        open_px = float(row.get("open", 0.0))
        high_px = float(row.get("high", 0.0))
        low_px = float(row.get("low", 0.0))
        close_px = float(row.get("close", 0.0))
        vol = float(row.get("volume", 0.0))

        if close_px > 0:
            ticks.append(Tick(ts=ts, open=open_px, high=high_px, low=low_px, close=close_px, volume=vol))

    return ticks


def calculate_atr(ticks: list[Tick], period: int = 14) -> Optional[float]:
    """计算ATR（使用完整OHLC）"""
    if len(ticks) < period + 1:
        return None

    trs = []
    for i in range(len(ticks) - period, len(ticks)):
        tr = max(
            ticks[i].high - ticks[i].low,
            abs(ticks[i].high - ticks[i - 1].close),
            abs(ticks[i].low - ticks[i - 1].close),
        )
        trs.append(tr)

    return sum(trs) / len(trs) if trs else None


def calculate_vwap(ticks: list[Tick]) -> float:
    """计算当前VWAP"""
    if not ticks:
        return 0.0

    sum_pv = 0.0
    sum_v = 0.0
    for tick in ticks:
        typical_price = (tick.high + tick.low + tick.close) / 3.0
        sum_pv += typical_price * tick.volume
        sum_v += tick.volume

    return sum_pv / sum_v if sum_v > 0 else 0.0


def check_a_cont_fast(
    close_now: float,
    close_3m_ago: float,
    close_1m: list[float],  # [0]=latest, [1]=1m ago
    vwap: float,
    vol_surge_ratio: float,
    atr: float,
) -> Optional[tuple[str, dict]]:
    """
    A_CONTINUATION_FAST: 爆发趋势

    条件：
    1. abs(v3m_atr) >= 0.60
    2. vol_surge >= 1.2
    3. dir1m == dir3m
    4. 价格 vs VWAP 正确侧
    """
    v3m_atr = abs(close_now - close_3m_ago) / atr

    if v3m_atr < 0.60:
        return None
    if vol_surge_ratio < 1.2:
        return None

    # 方向一致性
    dir1m_down = close_1m[0] < close_1m[1]
    dir3m_down = close_now < close_3m_ago

    # SHORT: 下跌 + 价格<VWAP
    if dir1m_down and dir3m_down and close_now < vwap:
        return "SHORT", {
            "v3m_atr": round(v3m_atr, 3),
            "vol_surge": round(vol_surge_ratio, 2),
            "close_now": round(close_now, 2),
            "vwap": round(vwap, 2),
        }

    # LONG: 上涨 + 价格>VWAP
    if not dir1m_down and not dir3m_down and close_now > vwap:
        return "LONG", {
            "v3m_atr": round(v3m_atr, 3),
            "vol_surge": round(vol_surge_ratio, 2),
            "close_now": round(close_now, 2),
            "vwap": round(vwap, 2),
        }

    return None


def check_a_cont_drift(
    close_1m: list[float],  # [0]=latest, [1]=1m ago, [2]=2m ago, [3]=3m ago
    close_now: float,
    vwap: float,
    atr: float,
) -> Optional[tuple[str, dict]]:
    """
    A_CONTINUATION_DRIFT: 稳定趋势

    条件：
    1. 连续3根1m K线同向
    2. 每根 >= 0.15 ATR
    3. 累计 >= 0.45 ATR
    4. 价格 vs VWAP 正确侧
    """
    if len(close_1m) < 4:
        return None

    chg1 = close_1m[0] - close_1m[1]
    chg2 = close_1m[1] - close_1m[2]
    chg3 = close_1m[2] - close_1m[3]

    chg1_atr = chg1 / atr
    chg2_atr = chg2 / atr
    chg3_atr = chg3 / atr
    cumulative_atr = (chg1 + chg2 + chg3) / atr

    # SHORT: 3根连阴
    if (chg1 < 0 and chg2 < 0 and chg3 < 0 and
        abs(chg1_atr) >= 0.15 and abs(chg2_atr) >= 0.15 and abs(chg3_atr) >= 0.15 and
        cumulative_atr <= -0.45 and
        close_now < vwap):
        return "SHORT", {
            "chg1_atr": round(chg1_atr, 3),
            "chg2_atr": round(chg2_atr, 3),
            "chg3_atr": round(chg3_atr, 3),
            "cumulative_atr": round(cumulative_atr, 3),
            "close_now": round(close_now, 2),
            "vwap": round(vwap, 2),
        }

    # LONG: 3根连阳
    if (chg1 > 0 and chg2 > 0 and chg3 > 0 and
        chg1_atr >= 0.15 and chg2_atr >= 0.15 and chg3_atr >= 0.15 and
        cumulative_atr >= 0.45 and
        close_now > vwap):
        return "LONG", {
            "chg1_atr": round(chg1_atr, 3),
            "chg2_atr": round(chg2_atr, 3),
            "chg3_atr": round(chg3_atr, 3),
            "cumulative_atr": round(cumulative_atr, 3),
            "close_now": round(close_now, 2),
            "vwap": round(vwap, 2),
        }

    return None


def check_short_veto(
    close_now: float,
    close_1m_ago: float,
    low_5m: float,
    t_low_5m: datetime,
    now: datetime,
    atr_live: float,
    veto_window: tuple[time, time] = (time(9, 40), time(11, 50)),
) -> bool:
    """SHORT VETO检查"""
    veto_start, veto_end = veto_window
    if not (veto_start <= now.time() <= veto_end):
        return False

    dist_from_low_atr = (close_now - low_5m) / atr_live
    chg_1m = close_now - close_1m_ago
    bounce_strength_atr = chg_1m / atr_live
    time_since_low_sec = (now - t_low_5m).total_seconds()

    return (
        dist_from_low_atr <= 0.30 and
        bounce_strength_atr >= 0.35 and
        time_since_low_sec <= 120
    )


def check_long_veto(
    close_now: float,
    close_1m_ago: float,
    high_5m: float,
    t_high_5m: datetime,
    now: datetime,
    atr_live: float,
    veto_window: tuple[time, time] = (time(9, 40), time(11, 50)),
) -> bool:
    """LONG VETO检查"""
    veto_start, veto_end = veto_window
    if not (veto_start <= now.time() <= veto_end):
        return False

    dist_from_high_atr = (high_5m - close_now) / atr_live
    chg_1m = close_now - close_1m_ago
    pullback_strength_atr = (-chg_1m) / atr_live
    time_since_high_sec = (now - t_high_5m).total_seconds()

    return (
        dist_from_high_atr <= 0.30 and
        pullback_strength_atr >= 0.35 and
        time_since_high_sec <= 120
    )


def simulate_exit(
    trade: Trade,
    ticks: list[Tick],
    start_idx: int,
    atr_at_entry: float,
    day: str,
) -> Trade:
    """
    模拟出场逻辑（基于QQQ价格变化，映射到TQQQ/SQQQ）

    规则（按优先级）：
    1. Stop Hit: QQQ价格变化 -2.5 ATR → TQQQ/SQQQ触发止损
    2. Breakeven Shield: 盈利>=1.0时，止损移至entry+0.20
    3. Target Hit: QQQ价格变化 +1.5 ATR → TQQQ/SQQQ达到目标
    4. EOD Flatten: 15:59强制平仓

    关键：基于QQQ的价格变化（ATR归一化），映射到TQQQ/SQQQ的3x效果
    """
    # QQQ价格的止损/止盈目标（关键：方向取决于LONG/SHORT）
    stop_atr_mult = 2.5
    stop_min_abs = 0.50
    qqq_stop_dist = max(stop_atr_mult * atr_at_entry, stop_min_abs)

    target_atr_mult = 1.5
    qqq_target_dist = target_atr_mult * atr_at_entry

    if trade.entry_direction == "LONG":
        # LONG: 买入TQQQ，期待QQQ上涨
        # 止损：QQQ跌破 entry - stop_dist
        # 止盈：QQQ涨过 entry + target_dist
        qqq_stop_px = round(trade.entry_px - qqq_stop_dist, 2)
        qqq_target_px = round(trade.entry_px + qqq_target_dist, 2)
    else:  # SHORT
        # SHORT: 买入SQQQ，期待QQQ下跌
        # 止损：QQQ涨破 entry + stop_dist（QQQ涨 → SQQQ跌 → 触发止损）
        # 止盈：QQQ跌破 entry - target_dist（QQQ跌 → SQQQ涨 → 达到目标）
        qqq_stop_px = round(trade.entry_px + qqq_stop_dist, 2)
        qqq_target_px = round(trade.entry_px - qqq_target_dist, 2)

    # 逐分钟检查（基于QQQ价格，映射到执行标的）
    for i in range(start_idx, len(ticks)):
        tick = ticks[i]  # QQQ的tick
        now = tick.ts
        qqq_close = tick.close
        qqq_high = tick.high
        qqq_low = tick.low

        # 1. Stop Hit检查（QQQ触及止损）
        stop_triggered = False
        if trade.entry_direction == "LONG":
            # LONG: QQQ跌破止损
            stop_triggered = qqq_low <= qqq_stop_px
        else:  # SHORT
            # SHORT: QQQ涨破止损
            stop_triggered = qqq_high >= qqq_stop_px

        if stop_triggered:
            trade.exit_time = now
            trade.exit_px = qqq_stop_px
            trade.exit_reason = "stop_hit"
            trade.pnl = round(trade.exit_px - trade.entry_px, 2)

            # 映射到执行标的
            trade.exec_exit_px = get_exec_price_from_qqq(qqq_stop_px, trade.exec_symbol)
            trade.exec_pnl = round((trade.exec_exit_px - trade.exec_entry_px) * trade.qty, 2)
            return trade

        # 2. Breakeven Shield（盈利>=1.0，止损移至有利位置）
        # 基于QQQ的盈利（方向相关）
        if not trade.breakeven_armed:
            if trade.entry_direction == "LONG":
                unrealized_qqq = qqq_close - trade.entry_px
                if unrealized_qqq >= 1.0:
                    new_stop_qqq = round(trade.entry_px + 0.20, 2)
                    if new_stop_qqq < qqq_close - 0.05:  # 安全检查
                        qqq_stop_px = new_stop_qqq
                        trade.breakeven_armed = True
            else:  # SHORT
                unrealized_qqq = trade.entry_px - qqq_close  # 反向计算
                if unrealized_qqq >= 1.0:
                    new_stop_qqq = round(trade.entry_px - 0.20, 2)
                    if new_stop_qqq > qqq_close + 0.05:  # 安全检查（反向）
                        qqq_stop_px = new_stop_qqq
                        trade.breakeven_armed = True

        # 3. Target Hit检查（QQQ达到目标）
        target_triggered = False
        if trade.entry_direction == "LONG":
            # LONG: QQQ涨破目标
            target_triggered = qqq_high >= qqq_target_px
        else:  # SHORT
            # SHORT: QQQ跌破目标
            target_triggered = qqq_low <= qqq_target_px

        if target_triggered:
            trade.exit_time = now
            trade.exit_px = qqq_target_px
            trade.exit_reason = "target_hit"
            trade.pnl = round(trade.exit_px - trade.entry_px, 2)

            # 映射到执行标的
            trade.exec_exit_px = get_exec_price_from_qqq(qqq_target_px, trade.exec_symbol)
            trade.exec_pnl = round((trade.exec_exit_px - trade.exec_entry_px) * trade.qty, 2)
            return trade

        # 4. EOD Flatten（15:59）
        if now.time() >= time(15, 59):
            trade.exit_time = now
            trade.exit_px = qqq_close
            trade.exit_reason = "eod_flatten"
            trade.pnl = round(trade.exit_px - trade.entry_px, 2)

            # 映射到执行标的
            trade.exec_exit_px = get_exec_price_from_qqq(qqq_close, trade.exec_symbol)
            trade.exec_pnl = round((trade.exec_exit_px - trade.exec_entry_px) * trade.qty, 2)
            return trade

    # 如果没有出场，使用收盘价
    last_tick = ticks[-1]
    qqq_exit_px = last_tick.close
    exec_exit_px = get_exec_price_from_qqq(qqq_exit_px, trade.exec_symbol)

    trade.exit_time = last_tick.ts
    trade.exit_px = qqq_exit_px
    trade.exec_exit_px = exec_exit_px
    trade.exit_reason = "eod_no_exit"
    trade.pnl = round(trade.exit_px - trade.entry_px, 2)
    trade.exec_pnl = round((exec_exit_px - trade.exec_entry_px) * trade.qty, 2)

    return trade


def get_exec_price_from_qqq(qqq_px: float, exec_symbol: str, qqq_ref: float = 600.0, tqqq_ref: float = 70.0, sqqq_ref: float = 8.0) -> float:
    """
    从QQQ价格推算TQQQ/SQQQ价格（3x杠杆）

    公式：
    - QQQ变动: delta_q = (qqq_px - qqq_ref) / qqq_ref
    - TQQQ: tqqq_ref * (1 + 3 * delta_q)
    - SQQQ: sqqq_ref * (1 - 3 * delta_q)

    参考价格（2026-02-20典型值）:
    - QQQ: ~600
    - TQQQ: ~70
    - SQQQ: ~8
    """
    delta_q = (qqq_px - qqq_ref) / qqq_ref

    if exec_symbol == "TQQQ":
        return tqqq_ref * (1 + 3 * delta_q)
    elif exec_symbol == "SQQQ":
        return sqqq_ref * (1 - 3 * delta_q)
    else:
        return qqq_px


def simulate_day(day: str, notional_usd: float = 120000.0) -> Optional[Trade]:
    """
    模拟一天的交易

    Args:
        day: 交易日期
        notional_usd: 使用资金（默认$120,000）
    """
    print(f"\n{'='*80}")
    print(f"测试日期: {day}")
    print(f"{'='*80}")

    ticks = load_1m_bars_from_clickhouse(day, "QQQ")

    if not ticks:
        print(f"❌ 没有数据")
        return None

    print(f"✓ 加载 {len(ticks)} 根1分钟K线")

    # 逐分钟扫描入场信号
    for i in range(14, len(ticks)):  # 至少14根计算ATR
        now_tick = ticks[i]
        now = now_tick.ts
        close_now = now_tick.close

        # 只在交易时段
        if not (time(9, 40) <= now.time() <= time(14, 50)):
            continue

        # 计算ATR（使用过去14根）
        atr_live = calculate_atr(ticks[max(0, i-14):i+1], period=14)
        if not atr_live or atr_live <= 0:
            continue

        # 计算VWAP（当日所有数据）
        vwap = calculate_vwap(ticks[:i+1])

        # 3分钟前的价格
        close_3m_ago = ticks[i-3].close if i >= 3 else close_now

        # 1分钟价格序列
        close_1m = [ticks[j].close for j in range(i, max(-1, i-4), -1)]
        if len(close_1m) < 2:
            continue

        close_1m_ago = close_1m[1]

        # 计算成交量放大（简化：当前vs平均）
        vol_avg = sum(t.volume for t in ticks[max(0, i-14):i]) / min(14, i) if i > 0 else 1.0
        vol_surge_ratio = now_tick.volume / vol_avg if vol_avg > 0 else 1.0

        # 5分钟窗口（用于VETO）
        window_5m_start = max(0, i - 5)
        prices_5m = [t.close for t in ticks[window_5m_start:i+1]]
        low_5m = min(prices_5m)
        high_5m = max(prices_5m)

        t_low_5m = next((t.ts for t in ticks[window_5m_start:i+1] if t.close == low_5m), now)
        t_high_5m = next((t.ts for t in ticks[window_5m_start:i+1] if t.close == high_5m), now)

        # 检查 FAST
        fast_result = check_a_cont_fast(close_now, close_3m_ago, close_1m, vwap, vol_surge_ratio, atr_live)
        if fast_result:
            direction, details = fast_result

            # VETO检查
            if direction == "SHORT":
                if check_short_veto(close_now, close_1m_ago, low_5m, t_low_5m, now, atr_live):
                    continue  # 被VETO拦截
            elif direction == "LONG":
                if check_long_veto(close_now, close_1m_ago, high_5m, t_high_5m, now, atr_live):
                    continue

            # 入场！确定执行标的和数量
            exec_symbol = "TQQQ" if direction == "LONG" else "SQQQ"
            exec_entry_px = get_exec_price_from_qqq(close_now, exec_symbol)

            qty = int(notional_usd / exec_entry_px)
            actual_notional = qty * exec_entry_px

            trade = Trade(
                entry_time=now,
                entry_px=close_now,  # QQQ价格
                entry_mode="FAST",
                entry_direction=direction,
                exec_symbol=exec_symbol,
                exec_entry_px=exec_entry_px,
                qty=qty,
                notional=actual_notional,
            )
            print(f"  ✅ {now.strftime('%H:%M')} 入场: FAST {direction} @ QQQ ${close_now:.2f}")
            print(f"     执行: {exec_symbol} @ ${exec_entry_px:.2f} × {qty}股 = ${actual_notional:,.0f}")
            print(f"     详情: {details}")

            # 模拟出场
            trade = simulate_exit(trade, ticks, i+1, atr_live, day)
            print(f"  🔚 {trade.exit_time.strftime('%H:%M')} 出场: {trade.exit_reason} @ QQQ ${trade.exit_px:.2f}")
            print(f"     QQQ盈亏: ${trade.pnl:.2f}")
            print(f"     实际盈亏: ${trade.exec_pnl:.2f} ({exec_symbol} ${trade.exec_entry_px:.2f} → ${trade.exec_exit_px:.2f})")

            return trade

        # 检查 DRIFT
        drift_result = check_a_cont_drift(close_1m, close_now, vwap, atr_live)
        if drift_result:
            direction, details = drift_result

            # VETO检查
            if direction == "SHORT":
                if check_short_veto(close_now, close_1m_ago, low_5m, t_low_5m, now, atr_live):
                    continue
            elif direction == "LONG":
                if check_long_veto(close_now, close_1m_ago, high_5m, t_high_5m, now, atr_live):
                    continue

            # 入场！确定执行标的和数量
            exec_symbol = "TQQQ" if direction == "LONG" else "SQQQ"
            exec_entry_px = get_exec_price_from_qqq(close_now, exec_symbol)

            qty = int(notional_usd / exec_entry_px)
            actual_notional = qty * exec_entry_px

            trade = Trade(
                entry_time=now,
                entry_px=close_now,  # QQQ价格
                entry_mode="DRIFT",
                entry_direction=direction,
                exec_symbol=exec_symbol,
                exec_entry_px=exec_entry_px,
                qty=qty,
                notional=actual_notional,
            )
            print(f"  ✅ {now.strftime('%H:%M')} 入场: DRIFT {direction} @ QQQ ${close_now:.2f}")
            print(f"     执行: {exec_symbol} @ ${exec_entry_px:.2f} × {qty}股 = ${actual_notional:,.0f}")
            print(f"     详情: {details}")

            # 模拟出场
            trade = simulate_exit(trade, ticks, i+1, atr_live, day)
            print(f"  🔚 {trade.exit_time.strftime('%H:%M')} 出场: {trade.exit_reason} @ QQQ ${trade.exit_px:.2f}")
            print(f"     QQQ盈亏: ${trade.pnl:.2f}")
            print(f"     实际盈亏: ${trade.exec_pnl:.2f} ({exec_symbol} ${trade.exec_entry_px:.2f} → ${trade.exec_exit_px:.2f})")

            return trade

    print(f"  ⚠️  全天无入场信号")
    return None


def main():
    print("🧪 单日单笔完整回测：A_CONTINUATION + 真实出场逻辑")
    print("="*80)
    print("资金配置: $120,000")
    print("观察标的: QQQ")
    print("执行标的: TQQQ (LONG) / SQQQ (SHORT)")
    print("="*80)

    # 从2月3日开始到2月23日
    import datetime as dt
    start_date = dt.date(2026, 2, 3)
    end_date = dt.date(2026, 2, 23)

    test_days = []
    current = start_date
    while current <= end_date:
        # 排除周末
        if current.weekday() < 5:
            test_days.append(current.strftime("%Y-%m-%d"))
        current += dt.timedelta(days=1)

    print(f"测试范围: {test_days[0]} 至 {test_days[-1]} ({len(test_days)}个交易日)")

    results = []

    for day in test_days:
        trade = simulate_day(day)
        results.append((day, trade))

    # 汇总报告
    print(f"\n{'='*80}")
    print("📊 逐日交易明细")
    print(f"{'='*80}")

    total_exec_pnl = 0.0
    win_count = 0
    loss_count = 0
    no_trade_count = 0

    for day, trade in results:
        if trade:
            total_exec_pnl += trade.exec_pnl if trade.exec_pnl else 0.0
            if trade.exec_pnl and trade.exec_pnl > 0:
                win_count += 1
            elif trade.exec_pnl and trade.exec_pnl < 0:
                loss_count += 1

            print(f"\n{day}:")
            print(f"  入场: {trade.entry_time.strftime('%H:%M')} {trade.entry_mode} {trade.entry_direction}")
            print(f"  观察: QQQ ${trade.entry_px:.2f} → ${trade.exit_px:.2f} (QQQ盈亏: ${trade.pnl:.2f})")
            print(f"  执行: {trade.exec_symbol} ${trade.exec_entry_px:.2f} → ${trade.exec_exit_px:.2f}")
            print(f"  数量: {trade.qty}股 × ${trade.exec_entry_px:.2f} = ${trade.notional:,.0f}")
            print(f"  实际盈亏: ${trade.exec_pnl:.2f} {'✅' if trade.exec_pnl > 0 else '❌'}")
            print(f"  出场: {trade.exit_time.strftime('%H:%M')} {trade.exit_reason}")
        else:
            no_trade_count += 1
            print(f"\n{day}: 无交易")

    print(f"\n{'='*80}")
    print("📈 汇总统计")
    print(f"{'='*80}")
    print(f"测试天数: {len(test_days)} 天")
    print(f"交易天数: {win_count + loss_count} 天")
    print(f"无交易天数: {no_trade_count} 天")
    print(f"")
    print(f"盈利次数: {win_count} 次")
    print(f"亏损次数: {loss_count} 次")
    if win_count + loss_count > 0:
        print(f"胜率: {win_count / (win_count + loss_count) * 100:.1f}%")
    print(f"")
    print(f"总盈亏: ${total_exec_pnl:,.2f}")
    if win_count + loss_count > 0:
        print(f"平均每笔: ${total_exec_pnl / (win_count + loss_count):.2f}")
    print(f"")
    print(f"资金使用: $120,000")
    if total_exec_pnl != 0:
        print(f"收益率: {total_exec_pnl / 120000 * 100:.2f}%")
    print(f"{'='*80}")
    print("✅ 测试完成")


if __name__ == "__main__":
    main()
