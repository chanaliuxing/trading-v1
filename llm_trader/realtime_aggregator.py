# llm_trader/realtime_aggregator.py
from collections import deque
from typing import Dict, Deque, Any, List, Optional
import math
import datetime as dt

class RealtimeAggregator:
    """
    从 1s bars 增量聚合出 5s/1m/5m/15m/30m/1h，并计算一批轻量滚动特征。
    只依赖传入的 1s bar（字典：ts, open, high, low, close, volume）。
    """

    def __init__(self, lookbacks: Dict[str, int] = None):
        # 保存每个周期的 deque；长度设为覆盖交易日 + 少量冗余
        self.lookbacks = lookbacks or {
            "1s":  3600,   # 1 小时
            "5s":  720,    # 1 小时
            "1m":  600,    # ~10 小时
            "5m":  200,    # ~16 小时
            "15m": 100,    # ~25 小时
            "30m": 100,
            "1h":  200,
        }
        self.bars: Dict[str, Dict[str, Deque[dict]]] = {}
        self._bucket = {"1s": 1, "5s": 5, "1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600}

    # ----------------- 工具函数（技术指标） -----------------
    @staticmethod
    def _ema(vals: List[float], period: int) -> Optional[float]:
        if period <= 1 or len(vals) < period:
            return None
        k = 2.0 / (period + 1)
        e = vals[-period]
        for x in vals[-period+1:]:
            e = x * k + e * (1 - k)
        return float(e)

    @staticmethod
    def _rsi(vals: List[float], period: int = 14) -> Optional[float]:
        if len(vals) < period + 1:
            return None
        gains = 0.0
        losses = 0.0
        for i in range(1, period + 1):
            chg = vals[-i] - vals[-i - 1]
            gains += max(0.0, chg)
            losses += max(0.0, -chg)
        gains /= period
        losses /= period
        if losses == 0:
            return 100.0
        rs = gains / losses
        return 100.0 - (100.0 / (1.0 + rs))

    @staticmethod
    def _zscore(vals: List[float], lookback: int) -> Optional[float]:
        if len(vals) < lookback:
            return None
        window = vals[-lookback:]
        mu = sum(window) / lookback
        var = sum((x - mu) ** 2 for x in window) / lookback
        sd = math.sqrt(var)
        if sd == 0:
            return 0.0
        return float((vals[-1] - mu) / sd)

    @staticmethod
    def _vwap(bars: List[dict], n: int) -> Optional[float]:
        if len(bars) < n:
            return None
        wsum = 0.0
        vsum = 0.0
        for b in bars[-n:]:
            price = (b["high"] + b["low"] + b["close"]) / 3.0
            vol = b.get("volume", 0.0) or 0.0
            wsum += price * vol
            vsum += vol
        return float(wsum / vsum) if vsum > 0 else None

    # ----------------- 数据结构初始化 -----------------
    def _ensure_symbol(self, symbol: str):
        if symbol not in self.bars:
            self.bars[symbol] = {tf: deque(maxlen=self.lookbacks[tf]) for tf in self.lookbacks}

    # ----------------- 写入 1s，向上聚合 -----------------
    def update_from_1s(self, symbol: str, bar_1s: dict):
        """
        bar_1s 必须包含 keys: ts (datetime/iso), open, high, low, close, volume
        """
        self._ensure_symbol(symbol)
        # 1) 追加 1s
        self._append_bar(symbol, "1s", bar_1s)

        # 2) 触发聚合到更大周期
        self._cascade_aggregate(symbol, base="1s", up="5s")
        self._cascade_aggregate(symbol, base="5s", up="1m")
        self._cascade_aggregate(symbol, base="1m", up="5m")
        self._cascade_aggregate(symbol, base="5m", up="15m")
        self._cascade_aggregate(symbol, base="15m", up="30m")
        self._cascade_aggregate(symbol, base="30m", up="1h")

    def _append_bar(self, symbol: str, tf: str, bar: dict):
        self.bars[symbol][tf].append(bar)

    def _cascade_aggregate(self, symbol: str, base: str, up: str):
        """
        每有 _bucket[up]/_bucket[base] 根 base bar，聚合出 1 根 up bar。
        增量方式：只处理最新的 ratio 根 bars，如果能凑成完整窗口就聚合
        """
        if up not in self._bucket or base not in self._bucket:
            return
        base_bucket = self._bucket[base]
        up_bucket = self._bucket[up]
        ratio = up_bucket // base_bucket
        base_deque = self.bars[symbol][base]

        # 需要至少 ratio 根 bars 才能聚合
        if len(base_deque) < ratio:
            return

        # 只处理最后 ratio 根 bars
        recent = list(base_deque)[-ratio:]

        # 聚合成一根 up bar
        o = recent[0]["open"]
        h = max(b["high"] for b in recent)
        l = min(b["low"] for b in recent)
        c = recent[-1]["close"]
        v = sum((b.get("volume") or 0.0) for b in recent)

        up_bar = {
            "ts": recent[-1]["ts"],
            "open": o, "high": h, "low": l, "close": c, "volume": v
        }

        # 检查是否已经有这个时间戳的 bar（避免重复）
        up_deque = self.bars[symbol][up]
        if len(up_deque) > 0 and up_deque[-1]["ts"] == up_bar["ts"]:
            # 更新最后一根（正在形成中的 bar）
            up_deque[-1] = up_bar
        else:
            # 添加新 bar
            up_deque.append(up_bar)

    # ----------------- 取快照 + 计算特征 -----------------
    def _build_features(self, bars: Deque[dict]) -> Dict[str, Optional[float]]:
        closes = [b["close"] for b in bars]
        feats = {
            "ema_9":   self._ema(closes, 9)   if len(closes) >= 9  else None,
            "ema_21":  self._ema(closes, 21)  if len(closes) >= 21 else None,
            "rsi_14":  self._rsi(closes, 14)  if len(closes) >= 15 else None,
            "z_20":    self._zscore(closes, 20) if len(closes) >= 20 else None,
            "z_60":    self._zscore(closes, 60) if len(closes) >= 60 else None,
            "vwap_20": self._vwap(list(bars), 20) if len(bars) >= 20 else None,
        }
        if feats["ema_9"] is not None and feats["ema_21"] is not None:
            feats["ema_cross"] = feats["ema_9"] - feats["ema_21"]
        else:
            feats["ema_cross"] = None
        return feats

    def get_snapshot(self, symbol: str) -> Dict[str, Any]:
        """返回多周期 bars + 每个周期滚动特征。"""
        self._ensure_symbol(symbol)
        out: Dict[str, Any] = {}
        for tf in ["1s","5s","1m","5m","15m","30m","1h"]:
            bq = self.bars[symbol][tf]
            # 转换 bars 列表，将 datetime 对象转为 ISO string
            bars_list = []
            for b in list(bq)[-120:]:
                bar_copy = b.copy()
                if isinstance(bar_copy.get("ts"), dt.datetime):
                    bar_copy["ts"] = bar_copy["ts"].isoformat()
                bars_list.append(bar_copy)

            out[tf] = {
                "bars": bars_list,
                "features": self._build_features(bq) if len(bq) else {}
            }
        return out

    def seed_with_5s(self, symbol: str, bars_5s: List[Dict[str, Any]]) -> None:
        """
        Cold start: expand 5s bars into 5x 1s 'synthetic' bars to populate context quickly.
        This is acceptable for medium/longer TF background; real-time will soon override.
        """
        if not bars_5s:
            return
        self._ensure_symbol(symbol)
        for b in bars_5s:
            # 简化：每个 5s bar 拆成 5 个 1s bar
            ts = b.get("ts")
            if isinstance(ts, str):
                # Parse ISO format
                ts = dt.datetime.fromisoformat(ts.replace('Z', '+00:00'))
            elif isinstance(ts, (int, float)):
                ts = dt.datetime.fromtimestamp(float(ts), tz=dt.timezone.utc)

            o = b.get("open", b.get("close"))
            h = b.get("high", b.get("close"))
            l = b.get("low", b.get("close"))
            c = b.get("close")
            vol = float(b.get("volume", 0.0))
            vol_each = vol / 5.0

            for i in range(5):
                # Linear interpolation
                ts_i = ts + dt.timedelta(seconds=i)
                close_i = o + (c - o) * (i / 4.0) if i < 4 else c
                self.update_from_1s(symbol, {
                    "ts": ts_i,
                    "open": close_i,
                    "high": close_i,
                    "low": close_i,
                    "close": close_i,
                    "volume": vol_each
                })
