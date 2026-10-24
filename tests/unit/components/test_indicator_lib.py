import pytest
from collections import deque
from libs.strategy.components.indicator_lib import IndicatorLib

class DummyBar:
    def __init__(self, h, l, c, v):
        self.high = h
        self.low = l
        self.close = c
        self.volume = v

def test_atr_calculation_precision():
    # 模拟一组简单的 K 线，测试 ATR 的平滑逻辑
    bars = [
        DummyBar(105, 100, 102, 100),
        DummyBar(106, 101, 103, 100),
        DummyBar(107, 102, 104, 100),
        DummyBar(108, 103, 105, 100),
        DummyBar(109, 104, 106, 100)
    ]
    # 如果周期是 3
    atr = IndicatorLib.atr(bars, period=3)
    assert atr is not None
    # TRs: (106-101)=5, (107-102)=5, (108-103)=5, (109-104)=5
    # 手算预期值应在 5.0 左右
    assert 4.9 < atr < 5.1

def test_vwap_accumulation():
    bars = deque([
        DummyBar(10, 10, 100, 10), # Price 100, Vol 10
        DummyBar(10, 10, 200, 10)  # Price 200, Vol 10
    ])
    vwap = IndicatorLib.vwap(bars)
    # (100*10 + 200*10) / 20 = 150
    assert vwap == 150.0
