# mtf_aggregator.py
"""
多时间框架聚合器 (Multi-Timeframe Aggregator)

核心特性:
- 严格自然时间桶对齐 (10分钟1s数据 → 恰好10根1m bar)
- 去重机制
- 1s → 5s → 1m → 5m → 15m → 30m → 1h 级联聚合
- O(1) 更新复杂度

基于 ChatGPT 时间桶归一化算法

Author: Trading System Team (based on ChatGPT + Claude proposals)
Version: 1.0.0
"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Deque, Dict, Iterable, List, Optional, Tuple
from collections import deque
import structlog

logger = structlog.get_logger(__name__)

# 支持的时间框架及其秒数
TF_SECONDS: Dict[str, int] = {
    "1s": 1,
    "5s": 5,
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
}


def _to_epoch_seconds(ts_val: Any) -> int:
    """
    将多种时间戳格式转换为epoch秒

    支持:
    - int/float (毫秒或秒)
    - str (ISO 8601格式)
    - datetime对象
    """
    if ts_val is None:
        raise ValueError("ts is required")

    if isinstance(ts_val, (int, float)):
        # 如果是毫秒级时间戳,转换为秒
        return int(ts_val / 1000) if ts_val > 1_000_000_000_000 else int(ts_val)

    if isinstance(ts_val, str):
        dt = datetime.fromisoformat(ts_val.replace("Z", "+00:00"))
        return int(dt.timestamp())

    if isinstance(ts_val, datetime):
        return int(ts_val.timestamp())

    raise TypeError(f"Unsupported ts type: {type(ts_val)!r}")


@dataclass
class _Bucket:
    """
    时间桶: 表示一个时间窗口内的OHLCV数据

    Attributes:
        start: 桶起始时间 (epoch秒)
        end: 桶结束时间 (epoch秒)
        open: 开盘价
        high: 最高价
        low: 最低价
        close: 收盘价
        volume: 成交量
        trades: 交易笔数 (可选)
    """
    start: int
    end: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    trades: int = 0

    @classmethod
    def from_tick(
        cls,
        start: int,
        dur: int,
        o: float,
        h: float,
        l: float,
        c: float,
        v: float,
        t: int = 0
    ) -> "_Bucket":
        """从单个tick创建新桶"""
        return cls(
            start=start,
            end=start + dur,
            open=o,
            high=h,
            low=l,
            close=c,
            volume=v,
            trades=t
        )

    def absorb(self, h: float, l: float, c: float, v: float, t: int = 0) -> None:
        """
        吸收新数据到当前桶 (同一时间桶内累积)

        Args:
            h: 新的高价
            l: 新的低价
            c: 新的收盘价
            v: 新的成交量
            t: 交易笔数
        """
        self.high = max(self.high, float(h))
        self.low = min(self.low, float(l))
        self.close = float(c)
        self.volume += float(v)
        self.trades += int(t)

    def to_bar(self) -> Dict[str, float]:
        """转换为bar字典"""
        return {
            "ts": self.end,
            "open": float(self.open),
            "high": float(self.high),
            "low": float(self.low),
            "close": float(self.close),
            "volume": float(self.volume),
            "trades": self.trades
        }


@dataclass
class _TFState:
    """
    单个时间框架的状态

    Attributes:
        seconds: 时间框架的秒数
        lookback: 保留的历史bar数量
        bars: 历史bars (deque with maxlen)
        current: 当前正在形成的桶
    """
    seconds: int
    lookback: int
    bars: Deque[Dict[str, float]] = field(default_factory=lambda: deque())
    current: Optional[_Bucket] = None

    def ensure_deque(self) -> None:
        """确保bars是正确maxlen的deque"""
        if not isinstance(self.bars, deque):
            self.bars = deque(maxlen=self.lookback)
        elif self.bars.maxlen != self.lookback:
            self.bars = deque(self.bars, maxlen=self.lookback)


class MTFAggregator:
    """
    多时间框架聚合器

    特性:
    - 严格自然时间桶 (每个自然分钟只产生一根1m bar)
    - 去重 (重复时间戳自动忽略)
    - 级联聚合 (1s → 5s → 1m → 5m → 15m → 30m → 1h)

    使用示例:
        agg = MTFAggregator(timeframes=("1s", "1m", "5m"))

        for bar_1s in stream:
            finalized = agg.update_1s("AAPL", bar_1s)
            if "1m" in finalized:
                print(f"完成1m bar: {finalized['1m']}")
    """

    def __init__(
        self,
        *,
        timeframes: Iterable[str] = ("1s", "1m", "5m", "15m"),
        lookbacks: Optional[Dict[str, int]] = None
    ) -> None:
        """
        初始化聚合器

        Args:
            timeframes: 需要聚合的时间框架列表
            lookbacks: 每个时间框架保留的历史bar数量
        """
        tfs = list(timeframes)
        # 确保1s在列表中 (作为基础周期)
        if "1s" not in tfs:
            tfs.insert(0, "1s")

        self._tfs = tfs
        self._seconds = {tf: TF_SECONDS[tf] for tf in tfs}

        # 默认lookback配置
        self._lookbacks = lookbacks or {
            "1s": 3600,   # 1小时的1s数据
            "5s": 720,    # 1小时的5s数据
            "1m": 600,    # 10小时的1m数据
            "5m": 200,    # ~16小时的5m数据
            "15m": 100,   # ~1天的15m数据
            "30m": 100,   # ~2天的30m数据
            "1h": 200,    # ~1周的1h数据
        }

        # 状态: {symbol: {tf: _TFState}}
        self._states: Dict[str, Dict[str, _TFState]] = {}

        # 去抖: 记录每个 (symbol, tf) 最后finalized的桶起点
        self._last_finalized_start: Dict[Tuple[str, str], int] = {}

    def seed_from_5s(self, symbol: str, bars_5s: List[Dict[str, Any]]) -> None:
        """
        从5s bars预热聚合器 (用于冷启动)

        Args:
            symbol: 交易标的代码
            bars_5s: 5秒bars列表
        """
        for b in bars_5s or []:
            self.update_1s(symbol, b)  # 简化: 逐条灌入

    def update_1s(
        self,
        symbol: str,
        bar_1s: Dict[str, Any]
    ) -> Dict[str, Dict[str, float]]:
        """
        推送1s bar并更新所有时间框架

        Args:
            symbol: 交易标的代码
            bar_1s: 1秒bar, 格式:
                {
                    'ts': 1704106200000,  # Unix timestamp (ms or seconds)
                    'open': 175.12,
                    'high': 175.20,
                    'low': 175.11,
                    'close': 175.18,
                    'volume': 2200
                }

        Returns:
            本次完成的bars: {tf: bar_dict}
            例如: {'5s': {...}, '1m': {...}}
        """
        # 1. 标准化时间戳和OHLCV
        ts_sec = _to_epoch_seconds(bar_1s.get("ts"))
        o = float(bar_1s.get("open", bar_1s["close"]))
        h = float(bar_1s.get("high", o))
        l = float(bar_1s.get("low", o))
        c = float(bar_1s.get("close", o))
        v = float(bar_1s.get("volume", 0.0))
        t = int(bar_1s.get("trades", 0))

        # 2. 确保symbol状态存在
        st = self._ensure_symbol(symbol)
        finalized: Dict[str, Dict[str, float]] = {}

        # 3. 更新所有时间框架
        for tf in self._tfs:
            sec = self._seconds[tf]
            tf_state = st[tf]
            tf_state.ensure_deque()

            # 计算时间桶起点 (关键: 自然桶对齐)
            bucket_start = (ts_sec // sec) * sec

            if tf_state.current is None:
                # 情况1: 首次创建桶
                tf_state.current = _Bucket.from_tick(bucket_start, sec, o, h, l, c, v, t)
                logger.debug("bucket_created", symbol=symbol, tf=tf, bucket_start=bucket_start)

            else:
                if bucket_start >= tf_state.current.end:
                    # 情况2: 跨越桶边界 - 完成旧桶,开启新桶
                    bdict = tf_state.current.to_bar()
                    tf_state.bars.append(bdict)

                    # 开启新桶
                    tf_state.current = _Bucket.from_tick(bucket_start, sec, o, h, l, c, v, t)

                    # 去抖: 只在首次完成该桶时添加到finalized
                    last_key = (symbol, tf)
                    if self._last_finalized_start.get(last_key, -1) < bucket_start:
                        finalized[tf] = bdict
                        self._last_finalized_start[last_key] = bucket_start
                        logger.debug(
                            "bar_finalized",
                            symbol=symbol,
                            tf=tf,
                            ts=bdict["ts"],
                            close=bdict["close"],
                            volume=bdict["volume"]
                        )

                elif bucket_start == tf_state.current.start:
                    # 情况3: 同一桶内 - 累积数据
                    tf_state.current.absorb(h, l, c, v, t)

                else:
                    # 情况4: 时间回退 (乱序数据) - 记录警告并忽略
                    logger.warning(
                        "out_of_order_bar",
                        symbol=symbol,
                        tf=tf,
                        current_bucket=tf_state.current.start,
                        input_bucket=bucket_start,
                        skipped=True
                    )

        return finalized

    def snapshot(self, symbol: str) -> Dict[str, Dict[str, Any]]:
        """
        获取当前所有时间框架的形成中的bars (未完成)

        Args:
            symbol: 交易标的代码

        Returns:
            {tf: bar_dict}
        """
        out: Dict[str, Dict[str, Any]] = {}
        st = self._ensure_symbol(symbol)

        for tf, state in st.items():
            if state.current:
                out[tf] = state.current.to_bar()

        return out

    def get_bars(self, symbol: str, tf: str) -> List[Dict[str, float]]:
        """
        获取指定时间框架的历史bars

        Args:
            symbol: 交易标的代码
            tf: 时间框架 (e.g., "1m")

        Returns:
            历史bars列表
        """
        st = self._ensure_symbol(symbol)
        if tf not in st:
            return []
        return list(st[tf].bars)

    def _ensure_symbol(self, symbol: str) -> Dict[str, _TFState]:
        """确保symbol的状态存在"""
        if symbol not in self._states:
            self._states[symbol] = {
                tf: _TFState(
                    seconds=self._seconds[tf],
                    lookback=self._lookbacks.get(tf, 100),
                    bars=deque(maxlen=self._lookbacks.get(tf, 100)),
                    current=None
                )
                for tf in self._tfs
            }
        return self._states[symbol]
