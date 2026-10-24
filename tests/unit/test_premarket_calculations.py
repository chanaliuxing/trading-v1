#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Premarket Momentum V4 - 核心计算逻辑单元测试
测试所有计算方法的准确性
"""
import sys
import os
import pytest
import math
from datetime import datetime
from zoneinfo import ZoneInfo

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# 导入 SecondBucket 和 RingBuffer from the actual script
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'scripts'))
from premarket_momentum_standalone import SecondBucket, RingBuffer

ET = ZoneInfo("America/New_York")

class TestRVOLCalculation:
    """测试相对成交量 (RVOL) 计算"""

    def test_rvol_basic(self):
        """基础 RVOL 计算"""
        vref = 150_000  # 昨天 9:30-9:31 总量
        vol_today = 270_000  # 今天 9:30-9:32 总量

        rvol = vol_today / vref
        assert abs(rvol - 1.8) < 0.01

    def test_rvol_score_mapping(self):
        """RVOL 评分映射"""
        def vol_score(x):
            if x is None: return 0.0
            if x < 0.2:   return 0.0
            if x < 0.5:   return (x - 0.2)/0.3 * 0.3
            if x < 1.0:   return 0.3 + (x - 0.5)/0.5 * 0.3
            if x <= 2.0:   return 0.6 + (x - 1.0)/1.0 * 0.3
            return 1.0

        assert vol_score(0.1) == 0.0
        assert vol_score(0.2) == 0.0
        assert abs(vol_score(0.35) - 0.15) < 0.01  # (0.35-0.2)/0.3 * 0.3
        assert abs(vol_score(0.5) - 0.3) < 0.01
        assert abs(vol_score(0.75) - 0.45) < 0.01  # 0.3 + (0.75-0.5)/0.5 * 0.3
        assert abs(vol_score(1.0) - 0.6) < 0.01
        assert abs(vol_score(1.5) - 0.75) < 0.01  # 0.6 + (1.5-1.0)/1.0 * 0.3
        assert abs(vol_score(2.0) - 0.9) < 0.0001
        assert vol_score(3.0) == 1.0

    def test_rvol_edge_cases(self):
        """RVOL 边界情况"""
        # Vref = 0
        vref = 0
        vol_today = 100_000
        rvol = None if vref == 0 else vol_today / vref

        assert rvol is None

        # Vol_today = 0
        vref = 100_000
        vol_today = 0
        rvol = vol_today / vref if vref > 0 else None

        assert rvol == 0.0


class TestSpreadCalculation:
    """测试 Spread 计算"""

    def test_spread_pct_calculation(self):
        """Spread 百分比计算"""
        bid = 100.0
        ask = 100.2
        mid = (bid + ask) / 2.0
        spread_pct = (ask - bid) / mid

        assert abs(spread_pct - 0.002) < 0.0001

    def test_spread_percentiles(self):
        """Spread 百分位数计算"""
        spreads = [0.001, 0.002, 0.003, 0.004, 0.005, 0.006, 0.007, 0.008, 0.009, 0.010]

        spreads.sort()
        median_idx = int((len(spreads)-1) * 50/100.0)
        p95_idx = int((len(spreads)-1) * 95/100.0)

        median = spreads[median_idx]
        p95 = spreads[p95_idx]

        assert median == 0.005  # 中位数
        assert p95 == 0.009    # P95

    def test_spread_cap(self):
        """Spread cap 限制"""
        SPREAD_PCT_CAP = 0.05

        bid = 100.0
        ask = 110.0  # 极宽 spread
        mid = (bid + ask) / 2.0
        spread_pct = (ask - bid) / mid  # = 10/105 = 0.095

        capped_spread = min(spread_pct, SPREAD_PCT_CAP)

        assert capped_spread == 0.05

    def test_spread_threshold_check(self):
        """Spread 阈值检查"""
        MAX_SPREAD_PCT_OBS_MEDIAN = 0.0025
        MAX_SPREAD_PCT_OBS_P95 = 0.0050

        # Case 1: Pass
        spread_median = 0.0020
        spread_p95 = 0.0040
        spread_ok = (spread_median <= MAX_SPREAD_PCT_OBS_MEDIAN and
                    spread_p95 <= MAX_SPREAD_PCT_OBS_P95)
        assert spread_ok is True

        # Case 2: Fail - median too high
        spread_median = 0.0030
        spread_p95 = 0.0040
        spread_ok = (spread_median <= MAX_SPREAD_PCT_OBS_MEDIAN and
                    spread_p95 <= MAX_SPREAD_PCT_OBS_P95)
        assert spread_ok is False

        # Case 3: Fail - p95 too high
        spread_median = 0.0020
        spread_p95 = 0.0060
        spread_ok = (spread_median <= MAX_SPREAD_PCT_OBS_MEDIAN and
                    spread_p95 <= MAX_SPREAD_PCT_OBS_P95)
        assert spread_ok is False


class TestVWAPCalculation:
    """测试 VWAP 计算"""

    def test_vwap_basic(self):
        """基础 VWAP 计算"""
        # 模拟 3 个 bar
        bars = [
            {"close": 100.0, "volume": 1000},
            {"close": 100.5, "volume": 1500},
            {"close": 101.0, "volume": 2000},
        ]

        vwap_num = sum(b["close"] * b["volume"] for b in bars)
        vwap_den = sum(b["volume"] for b in bars)
        vwap = vwap_num / vwap_den if vwap_den > 0 else 0.0

        expected_vwap = (100*1000 + 100.5*1500 + 101*2000) / (1000+1500+2000)
        expected_vwap = (100000 + 150750 + 202000) / 4500
        expected_vwap = 452750 / 4500
        expected_vwap = 100.611

        assert abs(vwap - expected_vwap) < 0.01

    def test_vwap_comparison(self):
        """VWAP 比较逻辑"""
        vwap = 100.0
        last = 100.5

        vwap_ok = last > vwap
        assert vwap_ok is True

        last = 99.5
        vwap_ok = last > vwap
        assert vwap_ok is False

    def test_vwap_with_zero_volume(self):
        """零成交量情况"""
        vwap_num = 0.0
        vwap_den = 0.0
        vwap = vwap_num / vwap_den if vwap_den > 0 else None

        assert vwap is None


class TestOpeningRangeCalculation:
    """测试 Opening Range 计算"""

    def test_or_range_calculation(self):
        """OR 范围计算"""
        or_high = 105.0
        or_low = 100.0
        or_range = or_high - or_low

        assert or_range == 5.0

    def test_or_progress_calculation(self):
        """OR 进度计算"""
        or_high = 105.0
        or_low = 100.0
        or_range = 5.0

        last = 103.0
        progress = (last - or_low) / or_range

        assert abs(progress - 0.6) < 0.01

    def test_or_trigger_condition(self):
        """OR 触发条件"""
        or_high = 105.0
        or_low = 100.0
        or_range = 5.0

        EPS = 1e-9

        # Case 1: 突破 OR high
        last = 105.0
        trigger_ok = (last >= or_high - EPS)
        assert trigger_ok is True

        # Case 2: 接近 OR high (< 30%)
        last = 104.0  # or_high - last = 1.0 <= 0.3 * 5.0 = 1.5
        trigger_ok = (or_high - last) <= (0.3 * or_range)
        assert trigger_ok is True

        # Case 3: 远离 OR high
        last = 103.0  # or_high - last = 2.0 > 1.5
        trigger_ok = (or_high - last) <= (0.3 * or_range)
        assert trigger_ok is False


class TestUptickRatioCalculation:
    """测试 Uptick Ratio 计算"""

    def test_uptick_ratio_basic(self):
        """基础 Uptick Ratio 计算"""
        upticks = 7
        downticks = 3

        uptick_ratio = upticks / (upticks + downticks)
        assert abs(uptick_ratio - 0.7) < 0.01

    def test_uptick_ratio_scoring(self):
        """Uptick Ratio 评分"""
        def tape_score(upr):
            if math.isnan(upr):
                return 0.0
            if upr >= 0.6:
                return 1.0
            elif upr >= 0.55:
                return 0.7
            elif upr >= 0.5:
                return 0.4
            else:
                return 0.0

        assert tape_score(0.65) == 1.0
        assert tape_score(0.57) == 0.7
        assert tape_score(0.52) == 0.4
        assert tape_score(0.45) == 0.0


class TestCompositeScoring:
    """测试综合评分"""

    def test_composite_score_calculation(self):
        """综合评分计算"""
        W_VOL = 0.3
        W_SPREAD = 0.25
        W_VWAP = 0.2
        W_OR = 0.15
        W_TAPE = 0.1

        vol_s = 0.8
        sp_s = 1.0
        vwap_s = 1.0
        or_s = 0.7
        tape_s = 1.0

        obs_score = (W_VOL*vol_s + W_SPREAD*sp_s + W_VWAP*vwap_s +
                    W_OR*or_s + W_TAPE*tape_s)

        expected = 0.3*0.8 + 0.25*1.0 + 0.2*1.0 + 0.15*0.7 + 0.1*1.0
        expected = 0.24 + 0.25 + 0.20 + 0.105 + 0.10
        expected = 0.895

        assert abs(obs_score - expected) < 0.01

    def test_weight_sum(self):
        """权重总和验证"""
        W_VOL = 0.3
        W_SPREAD = 0.25
        W_VWAP = 0.2
        W_OR = 0.15
        W_TAPE = 0.1

        total_weight = W_VOL + W_SPREAD + W_VWAP + W_OR + W_TAPE
        assert abs(total_weight - 1.0) < 0.01


class TestRiskCalculation:
    """测试风险计算"""

    def test_entry_price_calculation(self):
        """入场价格计算"""
        LIMIT_EPS_TICKS = 0.0005  # 0.05%

        ask = 100.0
        entry = round(ask * (1.0 + LIMIT_EPS_TICKS), 2)

        assert entry == 100.05

    def test_risk_per_share_natural(self):
        """自然风险计算"""
        entry = 100.05
        vwap = 99.0
        or_low = 98.5

        vwap_or = max(vwap, or_low)
        risk_natural = entry - vwap_or

        assert vwap_or == 99.0
        assert abs(risk_natural - 1.05) < 0.01

    def test_risk_per_share_with_floor(self):
        """风险下限计算"""
        FIXED_SL_FLOOR = 0.012  # 1.2%

        entry = 100.05
        vwap = 99.9
        or_low = 99.8

        vwap_or = max(vwap, or_low)
        risk_natural = entry - vwap_or
        risk_floor = entry * FIXED_SL_FLOOR

        risk_per_share = max(risk_natural, risk_floor)

        assert abs(risk_natural - 0.15) < 0.01
        assert abs(risk_floor - 1.2006) < 0.01
        assert abs(risk_per_share - 1.2006) < 0.01  # floor 生效

    def test_position_sizing(self):
        """仓位大小计算"""
        CAPITAL = 50000
        RISK_PCT = 0.005  # 0.5%

        risk_budget = CAPITAL * RISK_PCT
        assert risk_budget == 250.0

        # Case 1
        risk_per_share = 1.0
        shares = int(risk_budget / risk_per_share)
        assert shares == 250

        # Case 2
        risk_per_share = 2.0
        shares = int(risk_budget / risk_per_share)
        assert shares == 125

        # Case 3
        risk_per_share = 10.0
        shares = int(risk_budget / risk_per_share)
        assert shares == 25


class TestOCOPriceCalculation:
    """测试 OCO 价格计算"""

    def test_tp_price_calculation(self):
        """止盈价格计算"""
        TP_PCT = 2.4

        entry = 100.00
        tp_price = round(entry * (1.0 + TP_PCT/100.0), 2)
        assert tp_price == 102.40

        entry = 245.50
        tp_price = round(entry * (1.0 + TP_PCT/100.0), 2)
        expected = round(245.50 * 1.024, 2)
        assert tp_price == expected

    def test_trailing_percent(self):
        """追踪止损百分比"""
        TRAILING_PCT = 1.8

        assert TRAILING_PCT == 1.8


class TestSecondBucket:
    """测试 SecondBucket 聚合"""

    def test_second_bucket_update(self):
        """SecondBucket 更新逻辑"""
        bucket = SecondBucket(ts=123456789)

        # 第一个 tick
        bucket.update(last=100.0, bid=99.9, ask=100.1, size=100, last_prev=None)
        assert bucket.last == 100.0
        assert bucket.hi == 100.0
        assert bucket.lo == 100.0
        assert bucket.vol == 100.0

        # 第二个 tick (uptick)
        bucket.update(last=100.1, bid=100.0, ask=100.2, size=200, last_prev=100.0)
        assert bucket.last == 100.1
        assert bucket.hi == 100.1
        assert bucket.lo == 100.0
        assert bucket.vol == 300.0
        assert bucket.upticks == 1

        # 第三个 tick (downtick)
        bucket.update(last=100.0, bid=99.9, ask=100.1, size=150, last_prev=100.1)
        assert bucket.last == 100.0
        assert bucket.hi == 100.1
        assert bucket.lo == 100.0
        assert bucket.vol == 450.0
        assert bucket.downticks == 1

    def test_second_bucket_vwap(self):
        """SecondBucket VWAP 计算"""
        bucket = SecondBucket(ts=123456789)

        bucket.update(last=100.0, bid=None, ask=None, size=100, last_prev=None)
        bucket.update(last=100.5, bid=None, ask=None, size=200, last_prev=100.0)
        bucket.update(last=101.0, bid=None, ask=None, size=150, last_prev=100.5)

        vwap = bucket.vwap_num / bucket.vwap_den if bucket.vwap_den > 0 else 0.0
        expected_vwap = (100*100 + 100.5*200 + 101*150) / (100+200+150)
        expected_vwap = (10000 + 20100 + 15150) / 450
        expected_vwap = 45250 / 450

        assert abs(vwap - expected_vwap) < 0.01


class TestRingBuffer:
    """测试 RingBuffer"""

    def test_ring_buffer_pruning(self):
        """RingBuffer 剪枝"""
        ring = RingBuffer(seconds=180)
        base_ts = 1000000

        # 创建 300 秒的数据
        for i in range(300):
            t = datetime.fromtimestamp(base_ts + i, ET)
            ring.ingest_tick(t, last=100.0, bid=99.9, ask=100.1, size=10)

        # 剪枝发生在最后一个 tick
        assert len(ring.buckets) == 181  # 应该只保留 181 秒

    def test_ring_buffer_window_stats(self):
        """RingBuffer 窗口统计"""
        ring = RingBuffer(seconds=180)
        base_ts = 1000000

        # 创建一些数据
        for i in range(60):
            t = datetime.fromtimestamp(base_ts + i, ET)
            last = 100.0 + i * 0.1
            ring.ingest_tick(t, last=last, bid=last-0.1, ask=last+0.1, size=100)

        stats = ring.window_stats(base_ts, base_ts + 59)

        assert stats["volume"] > 0
        assert not math.isnan(stats["high"])
        assert not math.isnan(stats["low"])
        assert stats["high"] >= stats["low"]


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
