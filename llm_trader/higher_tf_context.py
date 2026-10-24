# llm_trader/higher_tf_context.py
from typing import Dict, Any, List, Optional
import math

class HigherTFContextLoader:
    """
    从 ClickHouse 拉 1h K线（近 N 根）并计算滚动特征，
    同时抓取 1d features 最新一条，作为背景锚点（即便是昨日日终也有参考价值）。
    """
    def __init__(self, ch_client):
        self.ch = ch_client

    # ---------- 指标 ----------
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
    def _z(vals: List[float], lookback: int) -> Optional[float]:
        if len(vals) < lookback:
            return None
        window = vals[-lookback:]
        mu = sum(window) / lookback
        var = sum((x - mu) ** 2 for x in window) / lookback
        sd = math.sqrt(var)
        return 0.0 if sd == 0 else float((vals[-1] - mu) / sd)

    @staticmethod
    def _boll(vals: List[float], n: int = 20):
        if len(vals) < n:
            return None, None, None, None
        w = vals[-n:]
        mu = sum(w) / n
        var = sum((x - mu) ** 2 for x in w) / n
        sd = math.sqrt(var)
        mid = mu
        up = mu + 2 * sd
        dn = mu - 2 * sd
        width = (up - dn) / mid if mid else None
        return float(mid), float(up), float(dn), (None if width is None else float(width))

    @staticmethod
    def _atr_like(highs: List[float], lows: List[float], closes: List[float], n: int = 14) -> Optional[float]:
        if len(closes) < n + 1:
            return None
        trs = []
        for i in range(1, n + 1):
            h, l, pc = highs[-i], lows[-i], closes[-i - 1]
            tr = max(h - l, abs(h - pc), abs(l - pc))
            trs.append(tr)
        return float(sum(trs) / n)

    # ---------- ClickHouse ----------
    def _fetch_1h(self, symbol: str, limit: int) -> List[dict]:
        q = f"""
        SELECT ts_start, open, high, low, close, volume
        FROM trading.bars_1h_current
        WHERE symbol = '{symbol}'
        ORDER BY ts_start DESC
        LIMIT {int(limit)}
        """
        try:
            result = self.ch.query(q)
            rows = result.result_rows or []
        except Exception as e:
            print(f"[HigherTFContextLoader] Error fetching 1h bars for {symbol}: {e}")
            return []

        bars = []
        for r in rows[::-1]:
            # Convert datetime to timestamp (milliseconds)
            ts = r[0]
            if hasattr(ts, 'timestamp'):
                ts = int(ts.timestamp() * 1000)
            bars.append({
                "ts": ts, "open": float(r[1]), "high": float(r[2]),
                "low": float(r[3]), "close": float(r[4]), "volume": float(r[5])
            })
        return bars

    def _fetch_1d_bars(self, symbol: str, limit: int = 60) -> List[dict]:
        """获取日线K线数据"""
        q = f"""
        SELECT ts_start, open, high, low, close, volume, vwap
        FROM trading.bars_1d_current
        WHERE symbol = '{symbol}'
        ORDER BY ts_start DESC
        LIMIT {int(limit)}
        """
        try:
            result = self.ch.query(q)
            rows = result.result_rows or []
        except Exception as e:
            print(f"[HigherTFContextLoader] Error fetching 1d bars for {symbol}: {e}")
            return []

        bars = []
        for r in rows[::-1]:
            ts = r[0]
            if hasattr(ts, 'timestamp'):
                ts = int(ts.timestamp() * 1000)
            bars.append({
                "ts": ts, "open": float(r[1]), "high": float(r[2]),
                "low": float(r[3]), "close": float(r[4]), "volume": float(r[5]),
                "vwap": float(r[6]) if r[6] is not None else None
            })
        return bars

    def _fetch_1d_feature(self, symbol: str) -> Optional[dict]:
        """从日线K线计算特征（兼容旧接口）"""
        bars = self._fetch_1d_bars(symbol, limit=60)
        if not bars:
            return None

        closes = [b["close"] for b in bars]
        highs = [b["high"] for b in bars]
        lows = [b["low"] for b in bars]

        # 计算指标
        ema_20 = self._ema(closes, 20)
        ema_50 = self._ema(closes, 50)
        rsi_14 = self._rsi(closes, 14)
        z20 = self._z(closes, 20)
        z60 = self._z(closes, 60) if len(closes) >= 60 else None
        atr_14 = self._atr_like(highs, lows, closes, 14)

        latest = bars[-1]
        return {
            "ts": latest["ts"],
            "close": latest["close"],
            "vwap": latest["vwap"],
            "atr_14": atr_14,
            "ema_20": ema_20,
            "ema_50": ema_50,
            "rsi_14": rsi_14,
            "zscore_close_20": z20,
            "zscore_close_60": z60,
            "vwap_day": latest["vwap"],  # 日线的vwap就是当日vwap
            "vwap_dev_bps": None,  # 暂不计算
        }

    # ---------- 对外 ----------
    def load_symbol(self, symbol: str, one_hour_limit: int = 120) -> Dict[str, Any]:
        out: Dict[str, Any] = {"1h": {"bars": [], "features": {}}, "1d": {"features": {}}}
        bars = self._fetch_1h(symbol, one_hour_limit)
        out["1h"]["bars"] = bars[-24:]  # 控制体积

        if bars:
            closes = [b["close"] for b in bars]
            highs = [b["high"] for b in bars]
            lows  = [b["low"]  for b in bars]
            ema9 = self._ema(closes, 9)
            ema21 = self._ema(closes, 21)
            rsi14 = self._rsi(closes, 14)
            z20 = self._z(closes, 20)
            z60 = self._z(closes, 60)
            bb_mid, bb_up, bb_dn, bb_w = self._boll(closes, 20)
            atr14 = self._atr_like(highs, lows, closes, 14)
            out["1h"]["features"] = {
                "ema_9": ema9, "ema_21": ema21,
                "ema_cross": (ema9 - ema21) if (ema9 is not None and ema21 is not None) else None,
                "rsi_14": rsi14,
                "zscore_close_20": z20, "zscore_close_60": z60,
                "bb_mid_20": bb_mid, "bb_up_20": bb_up, "bb_dn_20": bb_dn, "bb_bw_20": bb_w,
                "atr_14": atr14,
            }

        f1d = self._fetch_1d_feature(symbol)
        if f1d:
            out["1d"]["features"] = f1d

        return out

    def load(self, symbols: List[str], one_hour_limit: int = 120) -> Dict[str, Dict[str, Any]]:
        return {s: self.load_symbol(s, one_hour_limit=one_hour_limit) for s in symbols}
