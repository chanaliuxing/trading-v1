from __future__ import annotations

from strategies.stocks_kernel.types import FeatureResult, HistoryBundle, MarketSnapshot


def _close_series(history: HistoryBundle) -> list[float]:
    return [float(bar.close) for bar in history.bars_1m]


def _high_series(history: HistoryBundle) -> list[float]:
    return [float(bar.high) for bar in history.bars_1m]


def _low_series(history: HistoryBundle) -> list[float]:
    return [float(bar.low) for bar in history.bars_1m]


def calc_vwap(snapshot: MarketSnapshot, history: HistoryBundle, params: dict[str, object]) -> FeatureResult:
    if snapshot.vwap is not None:
        return FeatureResult(value=float(snapshot.vwap), ready=True, reason="ready")
    total_pv = 0.0
    total_vol = 0.0
    for bar in history.bars_1m:
        typical = (bar.high + bar.low + bar.close) / 3.0
        total_pv += typical * bar.volume
        total_vol += bar.volume
    if total_vol <= 0:
        return FeatureResult(value=None, ready=False, reason="vwap_not_ready")
    return FeatureResult(value=total_pv / total_vol, ready=True, reason="ready")


def calc_atr_eff(snapshot: MarketSnapshot, history: HistoryBundle, params: dict[str, object]) -> FeatureResult:
    window = int(params.get("window") or 14)
    highs = _high_series(history)
    lows = _low_series(history)
    closes = _close_series(history)
    if len(closes) < max(2, window):
        return FeatureResult(value=None, ready=False, reason="atr_eff_not_ready")
    trs: list[float] = []
    for idx in range(1, len(closes)):
        tr = max(highs[idx] - lows[idx], abs(highs[idx] - closes[idx - 1]), abs(lows[idx] - closes[idx - 1]))
        trs.append(tr)
    tail = trs[-window:]
    if not tail:
        return FeatureResult(value=None, ready=False, reason="atr_eff_not_ready")
    return FeatureResult(value=sum(tail) / len(tail), ready=True, reason="ready")


def calc_v3m(snapshot: MarketSnapshot, history: HistoryBundle, params: dict[str, object]) -> FeatureResult:
    bars = int(params.get("bars") or 3)
    closes = _close_series(history)
    if len(closes) < bars + 1:
        return FeatureResult(value=None, ready=False, reason="v3m_not_ready")
    base = closes[-(bars + 1)]
    if base == 0:
        return FeatureResult(value=None, ready=False, reason="v3m_not_ready")
    return FeatureResult(value=(closes[-1] - base) / base, ready=True, reason="ready")


def calc_pos_day(snapshot: MarketSnapshot, history: HistoryBundle, params: dict[str, object]) -> FeatureResult:
    closes = _close_series(history)
    if len(closes) < 2:
        return FeatureResult(value=None, ready=False, reason="pos_day_not_ready")
    day_low = min(closes)
    day_high = max(closes)
    last = closes[-1]
    if day_high <= day_low:
        return FeatureResult(value=0.5, ready=True, reason="ready")
    return FeatureResult(value=(last - day_low) / (day_high - day_low), ready=True, reason="ready")


def calc_vol_surge(snapshot: MarketSnapshot, history: HistoryBundle, params: dict[str, object]) -> FeatureResult:
    window = int(params.get("window") or 5)
    volumes = [float(bar.volume) for bar in history.bars_1m]
    if len(volumes) < max(2, window):
        return FeatureResult(value=None, ready=False, reason="vol_surge_not_ready")
    base = sum(volumes[-window:-1]) / max(1, len(volumes[-window:-1]))
    if base <= 0:
        return FeatureResult(value=None, ready=False, reason="vol_surge_not_ready")
    return FeatureResult(value=volumes[-1] / base, ready=True, reason="ready")


def calc_distance_from_vwap_atr(snapshot: MarketSnapshot, history: HistoryBundle, params: dict[str, object]) -> FeatureResult:
    closes = _close_series(history)
    if not closes:
        return FeatureResult(value=None, ready=False, reason="distance_from_vwap_atr_not_ready")
    vwap = float(snapshot.vwap) if snapshot.vwap is not None else None
    if vwap is None:
        vwap_result = calc_vwap(snapshot, history, {})
        if not vwap_result.ready:
            return FeatureResult(value=None, ready=False, reason="distance_from_vwap_atr_not_ready")
        vwap = float(vwap_result.value)
    atr_result = calc_atr_eff(snapshot, history, params)
    if not atr_result.ready or not atr_result.value:
        return FeatureResult(value=None, ready=False, reason="distance_from_vwap_atr_not_ready")
    return FeatureResult(value=(closes[-1] - vwap) / float(atr_result.value), ready=True, reason="ready")
