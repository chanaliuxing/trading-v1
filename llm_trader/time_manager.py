"""Time Manager (NY-native)

Accepts New York local time for all computations to eliminate ambiguity and
reduce conversion complexity in business logic. Convert external UTC inputs at
entry points, not inside this module.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Dict, Any
import datetime as dt
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
from datetime import timezone as _tz
UTC = _tz.utc

@dataclass
class TimeManager:
    """
    交易时段管理：
    - --close-at "15:55" （美东时间）
    - --close-before 5    （收盘前 N 分钟关闭/清仓）
    - 收盘前禁止开新仓（--forbid-new-positions-before N）
    - --start-trading-at "9:40" （何时开始允许交易，美东时间）
    - --stop-trading-at "10:30" （何时停止新交易，美东时间）
    """
    close_at: Optional[str] = None          # "HH:MM" in ET
    close_before_min: Optional[int] = None  # minutes before 16:00 ET
    forbid_new_positions_before_min: int = 5
    start_trading_at: Optional[str] = None  # "HH:MM" in ET, e.g. "9:40"
    stop_trading_at: Optional[str] = None   # "HH:MM" in ET, e.g. "10:30"

    def _today_close_et(self, now_et: dt.datetime) -> dt.datetime:
        return now_et.replace(hour=16, minute=0, second=0, microsecond=0)

    def _parse_close_at(self, now_et: dt.datetime) -> Optional[dt.datetime]:
        if not self.close_at:
            return None
        h, m = self.close_at.split(":")
        return now_et.replace(hour=int(h), minute=int(m), second=0, microsecond=0)

    def session_phase(self, now_ny: dt.datetime) -> str:
        """
        完整交易时段分类 (V2.9):
        - OVERNIGHT: 20:00 - 03:50 (隔夜交易,IBKR等券商提供)
        - PRE: 04:00 - 09:30 (盘前交易)
        - OPEN: 09:30 (开盘时刻)
        - RTH: 09:30 - 16:00 (常规交易时段)
        - CLOSE: 16:00 - 16:15 (收盘时刻)
        - AFTERHOURS: 16:00 - 20:00 (盘后交易)
        - OFF: 03:50 - 04:00 (系统维护窗口)
        """
        # Normalize to NY timezone (supports different tzinfo passed by callers)
        now_et = now_ny.astimezone(ET)
        t = now_et.time()

        # OVERNIGHT: 20:00 - 03:50 (next day)
        if t >= dt.time(20, 0) or t < dt.time(3, 50):
            return "OVERNIGHT"

        # OFF: 3:50 - 4:00 (系统维护窗口)
        if dt.time(3, 50) <= t < dt.time(4, 0):
            return "OFF"

        # PRE: 4:00 - 9:30
        if dt.time(4, 0) <= t < dt.time(9, 30):
            return "PRE"

        # OPEN: 9:30 (exact)
        if t == dt.time(9, 30):
            return "OPEN"

        # RTH: 9:30 - 16:00
        if dt.time(9, 30) < t < dt.time(16, 0):
            return "RTH"

        # CLOSE: 16:00 (exact opening of after-hours)
        if t == dt.time(16, 0):
            return "CLOSE"

        # AFTERHOURS: 16:00 - 20:00
        if dt.time(16, 0) < t < dt.time(20, 0):
            return "AFTERHOURS"

        # Fallback (should not reach here)
        return "OFF"

    def should_flatten_now(self, now_ny: dt.datetime) -> bool:
        """是否立即触发清仓退出。优先级：close_at > close_before_min。"""
        now_et = now_ny.astimezone(ET)
        # close_at
        tgt = self._parse_close_at(now_et)
        if tgt and now_et >= tgt:
            return True
        # close_before_min
        if self.close_before_min is not None:
            close_et = self._today_close_et(now_et)
            if now_et >= (close_et - dt.timedelta(minutes=self.close_before_min)):
                return True
        return False

    def allow_new_position(self, now_ny: dt.datetime) -> bool:
        """
        判断是否允许开新仓：
        1. 若 should_flatten_now=True，不允许
        2. 若设置了 start_trading_at，需要时间 >= 开始时间
        3. 若设置了 stop_trading_at，需要时间 < 停止时间
        4. 收盘前 N 分钟禁开新仓
        """
        if self.should_flatten_now(now_ny):
            return False

        now_et = now_ny.astimezone(ET)

        # Check start_trading_at (e.g., "9:40")
        if self.start_trading_at:
            h, m = self.start_trading_at.split(":")
            start_time = now_et.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
            if now_et < start_time:
                return False

        # Check stop_trading_at (e.g., "10:30")
        if self.stop_trading_at:
            h, m = self.stop_trading_at.split(":")
            stop_time = now_et.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
            if now_et >= stop_time:
                return False

        # Original close-time gate
        close_et = self._today_close_et(now_et)
        gate_from = close_et - dt.timedelta(minutes=max(0, self.forbid_new_positions_before_min))
        return now_et < gate_from

    def seconds_until_market_open(self, now_ny: dt.datetime) -> Optional[int]:
        """
        计算到下一个市场开盘(9:30 ET)的秒数

        Returns:
            None if already in trading hours (RTH)
            int seconds to wait if before market open
        """
        now_et = now_ny.astimezone(ET)
        phase = self.session_phase(now_ny)

        # 如果已经在 RTH (9:30-16:00)，不需要等待
        if phase == "RTH":
            return None

        # 计算今天的 9:30 ET
        today_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)

        # 如果当前时间 >= 今天的 9:30，说明市场已经开盘或收盘，等到明天
        if now_et >= today_open:
            # 等到明天 9:30
            tomorrow_open = today_open + dt.timedelta(days=1)
            seconds = int((tomorrow_open - now_et).total_seconds())
        else:
            # 等到今天 9:30
            seconds = int((today_open - now_et).total_seconds())

        return seconds if seconds > 0 else None

    def seconds_until_premarket_open(self, now_ny: dt.datetime) -> Optional[int]:
        """
        计算到下一个盘前开盘(4:00 ET)的秒数

        Returns:
            None if already in premarket or later (PRE/RTH)
            int seconds to wait if before premarket open
        """
        now_et = now_ny.astimezone(ET)
        phase = self.session_phase(now_ny)

        # 如果已经在盘前或RTH，不需要等待
        if phase in ("PRE", "OPEN", "RTH"):
            return None

        # 计算今天的 4:00 ET
        today_premarket = now_et.replace(hour=4, minute=0, second=0, microsecond=0)

        # 如果当前时间 >= 今天的 4:00，说明盘前已开始或已结束，等到明天
        if now_et >= today_premarket:
            # 等到明天 4:00
            tomorrow_premarket = today_premarket + dt.timedelta(days=1)
            seconds = int((tomorrow_premarket - now_et).total_seconds())
        else:
            # 等到今天 4:00
            seconds = int((today_premarket - now_et).total_seconds())

        return seconds if seconds > 0 else None

    def seconds_until_afterhours_open(self, now_ny: dt.datetime) -> Optional[int]:
        """
        计算到下一个盘后交易开始(16:00 ET)的秒数

        Returns:
            None if already in after-hours or later (AFTERHOURS/OVERNIGHT/CLOSE)
            int seconds to wait if before after-hours open
        """
        now_et = now_ny.astimezone(ET)
        phase = self.session_phase(now_ny)

        # 如果已经在盘后、隔夜或收盘时刻，不需要等待
        if phase in ("AFTERHOURS", "OVERNIGHT", "CLOSE"):
            return None

        # 计算今天的 16:00 ET
        today_afterhours = now_et.replace(hour=16, minute=0, second=0, microsecond=0)

        # 如果当前时间 >= 今天的 16:00，说明盘后已开始或已结束，等到明天
        if now_et >= today_afterhours:
            # 等到明天 16:00
            tomorrow_afterhours = today_afterhours + dt.timedelta(days=1)
            seconds = int((tomorrow_afterhours - now_et).total_seconds())
        else:
            # 等到今天 16:00
            seconds = int((today_afterhours - now_et).total_seconds())

        return seconds if seconds > 0 else None

    def seconds_until_overnight_open(self, now_ny: dt.datetime) -> Optional[int]:
        """
        计算到下一个隔夜交易开始(20:00 ET)的秒数

        Returns:
            None if already in overnight trading (OVERNIGHT)
            int seconds to wait if before overnight trading
        """
        now_et = now_ny.astimezone(ET)
        phase = self.session_phase(now_ny)

        # 如果已经在隔夜时段，不需要等待
        if phase == "OVERNIGHT":
            return None

        # 计算今天的 20:00 ET
        today_overnight = now_et.replace(hour=20, minute=0, second=0, microsecond=0)

        # 如果当前时间 >= 今天的 20:00，说明隔夜交易已开始，不需要等待
        if now_et >= today_overnight:
            return None

        # 等到今天 20:00
        seconds = int((today_overnight - now_et).total_seconds())

        return seconds if seconds > 0 else None

    def build_time_ctx(
        self,
        now_ny: dt.datetime,
        api_interval_sec: int = 60
    ) -> Dict[str, Any]:
        """供 Prompt 使用的时间上下文

        V2.1: Enhanced with ChatGPT-optimized fields for better session awareness

        Args:
            now_ny: Current NY datetime
            api_interval_sec: Seconds between LLM calls (for decisions_left calculation)

        Returns:
            Enhanced time context with session phase, timing gates, and decision cadence
        """
        now_et = now_ny.astimezone(ET)

        # Calculate flatten time
        flatten_time = None
        if self.close_at:
            h, m = self.close_at.split(":")
            flatten_time = now_et.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
        elif self.close_before_min:
            close_dt = self._today_close_et(now_et)
            flatten_time = close_dt - dt.timedelta(minutes=self.close_before_min)

        # Calculate decisions left until flatten
        decisions_left = 0
        if flatten_time:
            seconds_to_flatten = max(0, (flatten_time - now_et).total_seconds())
            decisions_left = max(0, int(seconds_to_flatten / max(1, api_interval_sec)))

        # Calculate minutes to close
        close_dt = self._today_close_et(now_et)
        minutes_to_close = int((close_dt - now_et).total_seconds() / 60)

        # Determine session phase (ChatGPT-compatible: OPEN/MIDDAY/AFTERNOON/CLOSE)
        phase = self.session_phase(now_ny)
        # Map to ChatGPT-expected values
        if phase == "RTH":
            t = now_et.time()
            if t < dt.time(10, 30):
                phase = "OPEN"
            elif t < dt.time(15, 0):
                phase = "MIDDAY"
            else:
                phase = "AFTERNOON"

        # V2.1: Enhanced context with ChatGPT-optimized fields
        return {
            # Original fields (for backward compatibility)
            # Backward-compatible UTC string (derived from NY now)
            "now_utc": now_et.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S"),
            "now_et": now_et.strftime("%Y-%m-%d %H:%M:%S"),
            "phase": phase,
            "should_flatten": self.should_flatten_now(now_ny),
            "allow_new_position": self.allow_new_position(now_ny),
            "forbid_new_positions_before_min": self.forbid_new_positions_before_min,
            "close_policy": {
                "close_at": self.close_at,
                "close_before_min": self.close_before_min
            },
            # V2.1: ChatGPT-optimized fields
            "ny_iso": now_et.isoformat(),                    # ISO format for clarity
            "session_phase": phase,                          # OPEN/MIDDAY/AFTERNOON/CLOSE
            "minutes_to_close": max(0, minutes_to_close),
            "stop_at_ny": flatten_time.strftime("%H:%M:%S") if flatten_time else None,
            "api_interval_sec": api_interval_sec,
            "decisions_left": decisions_left,
            "should_prevent_new_positions": not self.allow_new_position(now_ny),
        }
