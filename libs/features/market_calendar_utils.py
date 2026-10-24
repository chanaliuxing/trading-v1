# market_calendar_utils.py
# SPDX-License-Identifier: Apache-2.0
#
# 交易日历工具：无依赖的美股交易日/时段工具
# 支持周末跳过、自定义节假日、美东时区、交易时段推进/锚点跳跃
from __future__ import annotations
from dataclasses import dataclass
from typing import Iterable, Optional, List
import pandas as pd
import datetime as dt
import pytz

ET = pytz.timezone("America/New_York")


@dataclass
class TradingHours:
    """交易时段定义"""
    open_et: dt.time   # e.g., 09:30
    close_et: dt.time  # e.g., 16:00


@dataclass
class CalendarConfig:
    """交易日历配置"""
    trading_hours: TradingHours
    holidays: List[dt.date]  # list of holiday dates (ET)
    tz: str = "America/New_York"


def _parse_holidays(holiday_strs: Iterable[str]) -> List[dt.date]:
    """解析节假日字符串列表"""
    out = []
    for s in holiday_strs or []:
        try:
            out.append(dt.datetime.strptime(s, "%Y-%m-%d").date())
        except Exception:
            pass
    return out


def build_calendar_from_config(cfg: dict) -> CalendarConfig:
    """
    从config.yml构建日历配置

    Args:
        cfg: config.yml解析后的字典

    Returns:
        CalendarConfig对象
    """
    mh = cfg.get("market_calendar", {}) or {}
    th = cfg.get("trading_hours", {}) or {}

    # 交易时段
    open_str = th.get("open_et", "09:30")
    close_str = th.get("close_et", "16:00")
    open_et = dt.datetime.strptime(open_str, "%H:%M").time()
    close_et = dt.datetime.strptime(close_str, "%H:%M").time()

    # 节假日
    holidays = _parse_holidays(mh.get("holidays", []))

    # 时区
    tz = mh.get("source", "America/New_York")

    return CalendarConfig(
        trading_hours=TradingHours(open_et=open_et, close_et=close_et),
        holidays=holidays,
        tz=tz
    )


def _is_trading_day(d: dt.date, cal: CalendarConfig) -> bool:
    """
    判断是否为交易日

    Args:
        d: 日期
        cal: 日历配置

    Returns:
        是否为交易日
    """
    # 周末
    if d.weekday() >= 5:  # 5=Sat, 6=Sun
        return False

    # 节假日
    if d in cal.holidays:
        return False

    return True


def _next_trading_day(d: dt.date, cal: CalendarConfig) -> dt.date:
    """
    获取下一个交易日

    Args:
        d: 当前日期
        cal: 日历配置

    Returns:
        下一个交易日期
    """
    cur = d + dt.timedelta(days=1)
    while not _is_trading_day(cur, cal):
        cur += dt.timedelta(days=1)
    return cur


def _session_bounds(date_et: dt.date, cal: CalendarConfig) -> tuple:
    """
    获取交易时段边界

    Args:
        date_et: 日期（美东）
        cal: 日历配置

    Returns:
        (开盘时间, 收盘时间)元组
    """
    open_dt = ET.localize(dt.datetime.combine(date_et, cal.trading_hours.open_et))
    close_dt = ET.localize(dt.datetime.combine(date_et, cal.trading_hours.close_et))
    return open_dt, close_dt


def clamp_to_session(ts: pd.Timestamp, cal: CalendarConfig) -> pd.Timestamp:
    """
    将时间戳限制到交易时段

    若在非交易时段：落到最近未来的交易时刻（当日开盘或下一交易日开盘）

    Args:
        ts: 时间戳
        cal: 日历配置

    Returns:
        限制后的时间戳
    """
    # 统一转换为ET时区
    if ts.tzinfo is None:
        ts_et = ET.localize(ts.to_pydatetime())
    else:
        ts_et = ts.tz_convert(ET)

    d = ts_et.date()

    # 如果今天不是交易日 → 下一个交易日开盘
    if not _is_trading_day(d, cal):
        nd = _next_trading_day(d, cal)
        open_dt, _ = _session_bounds(nd, cal)
        return pd.Timestamp(open_dt)

    open_dt, close_dt = _session_bounds(d, cal)

    if ts_et < pd.Timestamp(open_dt):
        return pd.Timestamp(open_dt)

    if ts_et >= pd.Timestamp(close_dt):
        nd = _next_trading_day(d, cal)
        nod, _ = _session_bounds(nd, cal)
        return pd.Timestamp(nod)

    return ts_et


def advance_in_sessions(ts: pd.Timestamp, horizon_sec: int, cal: CalendarConfig) -> pd.Timestamp:
    """
    沿着交易时段向前推进horizon_sec秒

    跨日会自动滚入下一交易日开盘继续推进

    Args:
        ts: 起始时间戳
        horizon_sec: 向前推进的秒数
        cal: 日历配置

    Returns:
        推进后的时间戳
    """
    ts = clamp_to_session(ts, cal)
    remain = int(horizon_sec)
    cur = ts.to_pydatetime().astimezone(ET)

    while remain > 0:
        open_dt, close_dt = _session_bounds(cur.date(), cal)

        # 当前剩余交易时段秒数
        left = int((close_dt - cur).total_seconds())

        if remain <= left:
            cur = cur + dt.timedelta(seconds=remain)
            remain = 0
            break

        # 用完今天 → 下一交易日开盘
        remain -= left
        nd = _next_trading_day(cur.date(), cal)
        open_dt, _ = _session_bounds(nd, cal)
        cur = open_dt

    return pd.Timestamp(cur)


def jump_to_session_anchor(ts: pd.Timestamp, cal: CalendarConfig, anchor: str = "next_open") -> pd.Timestamp:
    """
    直接跳到某个交易时段锚点

    Args:
        ts: 当前时间戳
        cal: 日历配置
        anchor: 锚点类型
            - "next_open": 下一交易时段开盘（若当前在交易时段内，也跳到"下一天开盘"）
            - "same_day_close": 当日收盘；若已过收盘，则下一交易日收盘

    Returns:
        锚点时间戳
    """
    # 统一转换为ET时区
    if ts.tzinfo is None:
        ts_et = ET.localize(ts.to_pydatetime())
    else:
        ts_et = ts.tz_convert(ET)

    d = ts_et.date()

    if anchor == "next_open":
        if _is_trading_day(d, cal):
            open_dt, close_dt = _session_bounds(d, cal)
            if ts_et < pd.Timestamp(open_dt):
                return pd.Timestamp(open_dt)

        # 下一交易日开盘
        nd = _next_trading_day(d, cal)
        nod, _ = _session_bounds(nd, cal)
        return pd.Timestamp(nod)

    elif anchor == "same_day_close":
        if not _is_trading_day(d, cal):
            nd = _next_trading_day(d, cal)
            _, ncd = _session_bounds(nd, cal)
            return pd.Timestamp(ncd)

        od, cd = _session_bounds(d, cal)
        if ts_et <= pd.Timestamp(cd):
            return pd.Timestamp(cd)

        # 已过收盘 → 下一交易日收盘
        nd = _next_trading_day(d, cal)
        _, ncd = _session_bounds(nd, cal)
        return pd.Timestamp(ncd)

    else:
        # 默认 next_open
        return jump_to_session_anchor(ts, cal, anchor="next_open")
