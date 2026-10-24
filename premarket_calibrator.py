#!/usr/bin/env python3
"""
TSLA V7.2 参数大脑 (Parameter Brain)
遵循《TSLA（日内做多）策略 V7.2·简化稳健版》文档 2.盘前"参数大脑"

核心流程：
1. 候选集生成 (CandidateGenerator)
2. 30日前瞻验证 (WalkForwardValidator)
3. Top-3 softmax加权
4. 基线收缩
5. 目标触发率调整
6. 护栏与LKG回退
"""

import os
import sys
import json
import asyncio
import numpy as np
import pandas as pd
from typing import Dict, List, Tuple, Any, Optional
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict
from itertools import product
from concurrent.futures import ThreadPoolExecutor

# 导入回测引擎
from backtest_ml_engine import BacktestEngine, BacktestConfig


# ==================== 数据类定义 ====================

@dataclass
class ParameterCandidate:
    """单个参数组合候选"""
    # 市场regime参数
    regime_epsilon_pct: int  # {25, 30, 35} -> P25, P30, P35

    # 极端暂停参数
    pause_vix_pct: int  # {90, 95}
    pause_spread_pct: int  # {98, 99}
    pause_atr_pct: int  # {90, 95}
    pause_duration_min: int  # {10, 15}

    # A1参数
    a1_thr_sigma_pct: int  # {55, 60, 65, 70, 75}
    a1_rvol_window: str  # {'P30_P80', 'P25_P75'}
    a1_time_exit_min: int  # 从历史计算，这里用默认范围

    # A2参数
    a2_grid_q_pct: int  # {25, 28, 32}
    a2_grid_L: int  # {3, 4, 5}
    a2_oneway_thr: float  # 从历史P90计算

    # B1参数
    b1_orb_len: int  # {30, 45}
    b1_rvol_min_pct: int  # {40} -> 从rvol P40计算实际值

    # k_ATR参数
    k_atr_lambda: float  # {0.9, 1.0, 1.1}

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return asdict(self)

    def __hash__(self):
        """使候选可哈希，用于去重"""
        return hash(tuple(sorted(self.to_dict().items())))


@dataclass
class BacktestResult:
    """回测结果"""
    candidate: ParameterCandidate
    daily_pnl: List[float]  # 每日净PnL
    median_pnl: float
    turnover: float  # 总换手额
    dd_95: float  # 95分位最大回撤
    score: float  # 综合得分
    trades: int
    win_rate: float

    def to_dict(self) -> Dict[str, Any]:
        result = {
            'candidate': self.candidate.to_dict(),
            'median_pnl': self.median_pnl,
            'turnover': self.turnover,
            'dd_95': self.dd_95,
            'score': self.score,
            'trades': self.trades,
            'win_rate': self.win_rate
        }
        return result


# ==================== 候选集生成器 ====================

class CandidateGenerator:
    """
    根据文档 2.1 生成参数候选集
    """

    # 候选值定义（从文档提取）
    REGIME_EPSILON_PCTS = [25, 30, 35]
    PAUSE_VIX_PCTS = [90, 95]
    PAUSE_SPREAD_PCTS = [98, 99]
    PAUSE_ATR_PCTS = [90, 95]
    PAUSE_DURATIONS = [10, 15]

    A1_THR_SIGMA_PCTS = [55, 60, 65, 70, 75]
    A1_RVOL_WINDOWS = ['P30_P80', 'P25_P75']

    A2_GRID_Q_PCTS = [25, 28, 32]
    A2_GRID_LS = [3, 4, 5]

    B1_ORB_LENS = [30, 45]
    B1_RVOL_MIN_PCTS = [40]  # 从rvol分布P40计算

    K_ATR_LAMBDAS = [0.9, 1.0, 1.1]

    @classmethod
    def generate_all_candidates(cls) -> List[ParameterCandidate]:
        """
        生成所有参数组合

        组合数估算：
        3 * 2 * 2 * 2 * 2 * 5 * 2 * 3 * 3 * 2 * 1 * 3 = 25,920 个

        为了计算可行性，我们可以启用采样或减少候选集
        """
        candidates = []

        for combo in product(
            cls.REGIME_EPSILON_PCTS,
            cls.PAUSE_VIX_PCTS,
            cls.PAUSE_SPREAD_PCTS,
            cls.PAUSE_ATR_PCTS,
            cls.PAUSE_DURATIONS,
            cls.A1_THR_SIGMA_PCTS,
            cls.A1_RVOL_WINDOWS,
            cls.A2_GRID_Q_PCTS,
            cls.A2_GRID_LS,
            cls.B1_ORB_LENS,
            cls.B1_RVOL_MIN_PCTS,
            cls.K_ATR_LAMBDAS
        ):
            candidate = ParameterCandidate(
                regime_epsilon_pct=combo[0],
                pause_vix_pct=combo[1],
                pause_spread_pct=combo[2],
                pause_atr_pct=combo[3],
                pause_duration_min=combo[4],
                a1_thr_sigma_pct=combo[5],
                a1_rvol_window=combo[6],
                a1_time_exit_min=40,  # 固定值
                a2_grid_q_pct=combo[7],
                a2_grid_L=combo[8],
                a2_oneway_thr=2.2,  # 固定值
                b1_orb_len=combo[9],
                b1_rvol_min_pct=combo[10],
                k_atr_lambda=combo[11]
            )
            candidates.append(candidate)

        return candidates

    @classmethod
    def generate_reduced_candidates(cls, reduction_factor: int = 5) -> List[ParameterCandidate]:
        """
        生成精简候选集（用于快速测试）

        策略：每个维度只取中间值或两个极值
        """
        # 精简版候选值
        regime_eps = [cls.REGIME_EPSILON_PCTS[1]]  # 只取P30
        pause_vix = [cls.PAUSE_VIX_PCTS[0]]  # 只取P90
        pause_spread = [cls.PAUSE_SPREAD_PCTS[0]]  # 只取P98
        pause_atr = [cls.PAUSE_ATR_PCTS[0]]  # 只取P90
        pause_dur = [cls.PAUSE_DURATIONS[0]]  # 只取10

        a1_thr = [cls.A1_THR_SIGMA_PCTS[i] for i in [1, 2, 3]]  # 取60, 65, 70
        a1_rvol = cls.A1_RVOL_WINDOWS  # 保留2个

        a2_q = [cls.A2_GRID_Q_PCTS[1]]  # 只取28
        a2_L = [cls.A2_GRID_LS[i] for i in [0, 2]]  # 取3, 5

        b1_orb = cls.B1_ORB_LENS  # 保留2个
        b1_rvol = cls.B1_RVOL_MIN_PCTS  # 保留1个

        k_atr = cls.K_ATR_LAMBDAS  # 保留3个

        # 组合数：1*1*1*1*1*3*2*1*2*2*1*3 = 72 个
        candidates = []
        for combo in product(
            regime_eps, pause_vix, pause_spread, pause_atr, pause_dur,
            a1_thr, a1_rvol, a2_q, a2_L, b1_orb, b1_rvol, k_atr
        ):
            candidate = ParameterCandidate(
                regime_epsilon_pct=combo[0],
                pause_vix_pct=combo[1],
                pause_spread_pct=combo[2],
                pause_atr_pct=combo[3],
                pause_duration_min=combo[4],
                a1_thr_sigma_pct=combo[5],
                a1_rvol_window=combo[6],
                a1_time_exit_min=40,  # 固定值或从历史计算
                a2_grid_q_pct=combo[7],
                a2_grid_L=combo[8],
                a2_oneway_thr=2.2,  # 固定值
                b1_orb_len=combo[9],
                b1_rvol_min_pct=combo[10],
                k_atr_lambda=combo[11]
            )
            candidates.append(candidate)

        return candidates


# ==================== 参数转换器 ====================

class ParameterTranslator:
    """
    将抽象候选参数转换为具体策略配置
    需要历史数据来计算分位数
    """

    def __init__(self, symbol: str = "TSLA"):
        self.symbol = symbol
        self.hist_data_cache = {}

    async def translate_candidate_to_config(
        self,
        candidate: ParameterCandidate,
        historical_data: Dict[str, pd.DataFrame]
    ) -> Dict[str, Any]:
        """
        将候选参数转换为完整的策略配置

        Args:
            candidate: 参数候选
            historical_data: 历史数据 {'1m': df1, '5m': df5}

        Returns:
            完整的策略配置字典
        """
        df1 = historical_data.get('1m')
        df5 = historical_data.get('5m')

        # 计算具体参数值
        config = {
            "symbol": self.symbol,
            "premarket_calibration": {
                "use_config_values": True,
                "enabled": False
            },
            "initial_state": {
                "market_regime": "BULL",
                "market_mult": 1.0,
                "latest_vix": 15.0,
                "prev_vix": 15.0
            },
            "regime": {
                "epsilon": self._calc_regime_epsilon(df5, candidate.regime_epsilon_pct),
                "market_mult": {
                    "BULL": 1.0,
                    "NEUTRAL": 0.75,
                    "BEAR": 0.5
                },
                "vix_symbol": "VIX"
            },
            "windows": {
                "short_days": 7
            },
            "k_atr": self._calc_k_atr(df5, candidate.k_atr_lambda),
            "pause": self._calc_pause_params(df1, df5, candidate),
            "A1": self._calc_a1_params(df1, df5, candidate),
            "A2": self._calc_a2_params(df5, candidate),
            "B1": self._calc_b1_params(df5, candidate),
            "positioning": {
                "base_unit_pct": 0.012
            },
            "timing": {
                "recalibration_interval_min": 15,
                "eod_close_time": "15:57",
                "cooldown": {
                    "B1": 5,
                    "A1": 3,
                    "A2": 3
                }
            },
            "data_limits": {
                "bars_1m": 240,
                "bars_5m": 240,
                "bars_5m_120": 120,
                "premarket_1m": 2730,
                "premarket_5m": 4680
            },
            "slippage": {
                "eps_p95": 0.0005,
                "min": 0.0002,
                "max": 0.0008
            },
            "commission_per_share": 0.0035,
            "edge_floor": 0.18,
            "dd_limits": {
                "soft_pct": 0.018,
                "hard_pct": 0.028
            },
            "dynamic_cost_gate": {
                "min_trades": 10,
                "atr_floor_pct": 0.02,
                "coldstart_weight": 0.5
            },
            "anti_starvation": {
                "L1_min": 120,
                "L2_min": 180,
                "relax_sigma": [0.9, 0.8],
                "relax_g": [0.9, 0.8],
                "probe_ratio": 0.5,
                "revert_after_losses": 3,
                "cutoff": "14:00"
            },
            "target_triggers": 4,
            "market_regime_symbols": ["SPY", "QQQ"]
        }

        return config

    def _calc_regime_epsilon(self, df5: pd.DataFrame, pct: int) -> float:
        """
        计算市场噪声带 ε
        Doc §2.1: 从SPY/QQQ 5m 60日数据计算相对EMA50的偏离分布，取"更弱"的指数
        """
        if df5 is None or df5.empty or len(df5) < 55:
            # Fallback to hardcoded values if insufficient data
            pct_to_val = {25: 0.0040, 30: 0.0048, 35: 0.0055}
            return pct_to_val.get(pct, 0.0048)

        # Calculate EMA50
        close = df5['close']
        ema50 = close.ewm(span=50, adjust=False).mean()

        # Calculate deviation from EMA50
        deviation = (close - ema50) / ema50
        deviation_abs = deviation.abs()

        # Take the specified percentile
        epsilon = float(np.nanpercentile(deviation_abs.dropna().values, pct))

        # Clip to reasonable range
        return float(np.clip(epsilon, 0.003, 0.010))

    def _calc_k_atr(self, df5: pd.DataFrame, lambda_val: float) -> float:
        """
        计算k_ATR
        Doc §2.1: k_ATR = λ × P90(|ΔC_5m|)/mean(ATR_5m)
        """
        if df5 is None or df5.empty or len(df5) < 20:
            # Fallback: simple scaling
            return 2.0 * lambda_val

        # Calculate ATR(14, 5m)
        h, l, c = df5['high'], df5['low'], df5['close']
        tr = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1/14, adjust=False).mean()
        atr_mean = float(atr.mean())

        # Calculate |ΔC_5m| distribution
        delta_close = df5['close'].diff().abs()
        p90_delta = float(np.nanpercentile(delta_close.dropna().values, 90))

        # k_ATR = λ × P90(|ΔC|) / mean(ATR)
        if atr_mean > 0:
            k_atr = lambda_val * (p90_delta / atr_mean)
        else:
            k_atr = 2.0 * lambda_val

        # Clip to bounds [1.4, 3.0]
        return float(np.clip(k_atr, 1.4, 3.0))

    def _calc_pause_params(
        self,
        df1: pd.DataFrame,
        df5: pd.DataFrame,
        candidate: ParameterCandidate
    ) -> Dict[str, Any]:
        """
        计算极端暂停参数
        Doc §2.1:
        - VIX θ_vix ∈ {P90, P95} of 60-day VIX spike rate
        - Spread θ_spread ∈ {P98, P99} of 7-day same-slot half-spread%
        - ATR θ_atr ∈ {P90, P95} of |ΔC_5m|/ATR_5m (60-day)
        """
        # VIX spike threshold (percentage change)
        # TODO: Need actual VIX data to compute this properly
        # For now, use reasonable defaults based on percentile choice
        theta_vix = 0.12 if candidate.pause_vix_pct == 90 else 0.15

        # Spread threshold (half-spread%)
        # TODO: Need L1 bid/ask data to compute actual spread distributions
        # For now, use reasonable defaults
        theta_spread = 0.0006 if candidate.pause_spread_pct == 98 else 0.0008

        # ATR spike threshold from 60-day 5m data
        if df5 is not None and not df5.empty and len(df5) >= 20:
            # Calculate ATR
            h, l, c = df5['high'], df5['low'], df5['close']
            tr = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
            atr = tr.ewm(alpha=1/14, adjust=False).mean()

            # Calculate |ΔC_5m|/ATR ratio
            delta_c = df5['close'].diff().abs()
            ratio = delta_c / atr
            ratio_clean = ratio.replace([np.inf, -np.inf], np.nan).dropna()

            if len(ratio_clean) > 0:
                theta_atr = float(np.nanpercentile(ratio_clean.values, candidate.pause_atr_pct))
                theta_atr = float(np.clip(theta_atr, 1.5, 3.0))
            else:
                theta_atr = 2.0 if candidate.pause_atr_pct == 90 else 2.2
        else:
            theta_atr = 2.0 if candidate.pause_atr_pct == 90 else 2.2

        return {
            "theta_vix": theta_vix,
            "theta_spread": theta_spread,
            "theta_atr": theta_atr,
            "t_pause_min": candidate.pause_duration_min
        }

    def _calc_a1_params(
        self,
        df1: pd.DataFrame,
        df5: pd.DataFrame,
        candidate: ParameterCandidate
    ) -> Dict[str, Any]:
        """
        计算A1参数
        Doc §2.1:
        - thr_sigma从当日VWAP偏离分布的指定分位数计算
        - RVOL窗口从7日时间槽分布的指定分位数计算
        """
        # Calculate thr_sigma from VWAP deviation distribution
        if df1 is not None and not df1.empty and len(df1) >= 10:
            # Calculate VWAP
            q = df1['volume'].clip(lower=0.0)
            pq = df1['close'] * q
            vwap = (pq.cumsum() / q.cumsum()).replace([np.inf, -np.inf], np.nan).ffill()

            # Calculate deviation
            deviation = (df1['close'] - vwap) / vwap
            dev_abs = deviation.abs().dropna()

            if len(dev_abs) > 0:
                thr_sigma = float(np.nanpercentile(dev_abs.values, candidate.a1_thr_sigma_pct))
                thr_sigma = float(np.clip(thr_sigma, 0.004, 0.020))
            else:
                # Fallback to default mapping
                pct_to_sigma = {55: 0.007, 60: 0.009, 65: 0.011, 70: 0.013, 75: 0.015}
                thr_sigma = pct_to_sigma.get(candidate.a1_thr_sigma_pct, 0.009)
        else:
            # Fallback to default mapping
            pct_to_sigma = {55: 0.007, 60: 0.009, 65: 0.011, 70: 0.013, 75: 0.015}
            thr_sigma = pct_to_sigma.get(candidate.a1_thr_sigma_pct, 0.009)

        # Calculate RVOL window from 7-day distribution
        # Parse window spec: 'P30_P80' means [P30, P80]
        if candidate.a1_rvol_window == 'P30_P80':
            lo_pct, hi_pct = 30, 80
        elif candidate.a1_rvol_window == 'P25_P75':
            lo_pct, hi_pct = 25, 75
        else:
            lo_pct, hi_pct = 30, 80

        # Compute RVOL from 5m data (approximate - ideally need time-slot method)
        if df5 is not None and not df5.empty and len(df5) >= 78:
            # Simple RVOL approximation: rolling volume / median volume
            rolling_vol = df5['volume'].rolling(2, min_periods=1).sum()
            median_vol = rolling_vol.median()
            if median_vol > 0:
                rvol = rolling_vol / median_vol
                rvol_clean = rvol.replace([np.inf, -np.inf], np.nan).dropna()
                if len(rvol_clean) > 0:
                    rvol_lo = float(np.nanpercentile(rvol_clean.values, lo_pct))
                    rvol_hi = float(np.nanpercentile(rvol_clean.values, hi_pct))
                    rvol_lo = float(np.clip(rvol_lo, 0.75, 1.10))
                    rvol_hi = float(np.clip(rvol_hi, 1.0, 2.5))
                else:
                    # Fallback
                    window_to_range = {'P30_P80': (0.85, 1.8), 'P25_P75': (0.82, 1.85)}
                    rvol_lo, rvol_hi = window_to_range.get(candidate.a1_rvol_window, (0.85, 1.8))
            else:
                window_to_range = {'P30_P80': (0.85, 1.8), 'P25_P75': (0.82, 1.85)}
                rvol_lo, rvol_hi = window_to_range.get(candidate.a1_rvol_window, (0.85, 1.8))
        else:
            # Fallback to default mapping
            window_to_range = {'P30_P80': (0.85, 1.8), 'P25_P75': (0.82, 1.85)}
            rvol_lo, rvol_hi = window_to_range.get(candidate.a1_rvol_window, (0.85, 1.8))

        return {
            "thr_sigma": thr_sigma,
            "rvol_lo": rvol_lo,
            "rvol_hi": rvol_hi,
            "time_exit_min": 40,  # 从历史回归时长计算，这里用默认
            "exit": {
                "vwap_threshold_pct": 0.003,
                "profit_take_pct": 0.7,
                "eod_clean_time": "14:30"
            }
        }

    def _calc_a2_params(
        self,
        df5: pd.DataFrame,
        candidate: ParameterCandidate
    ) -> Dict[str, Any]:
        """
        计算A2参数
        Doc §2.1: g = max(Pq(TR_5m), D_p80/L)
        - Pq(TR_5m): q分位数的true range
        - D_p80: 日波幅的80分位
        - L: 层数参数
        """
        q_pct = candidate.a2_grid_q_pct
        L = candidate.a2_grid_L

        # Calculate TR from 5m data
        if df5 is not None and not df5.empty and len(df5) >= 20:
            h, l, c = df5['high'], df5['low'], df5['close']
            tr = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
            tr_clean = tr.dropna()

            if len(tr_clean) > 0:
                tr_val = float(np.nanpercentile(tr_clean.values, q_pct))
                tr_val = float(np.clip(tr_val, 0.5, 3.0))
            else:
                # Fallback to default mapping
                q_to_tr = {25: 0.8, 28: 1.2, 32: 1.5}
                tr_val = q_to_tr.get(q_pct, 1.2)

            # Calculate D_p80 (daily range 80th percentile)
            # Group by date and calculate daily range
            df5_copy = df5.copy()
            df5_copy['date'] = pd.to_datetime(df5_copy.index if isinstance(df5.index, pd.DatetimeIndex) else df5.index).date
            daily_range = df5_copy.groupby('date').apply(lambda x: x['high'].max() - x['low'].min())

            if len(daily_range) > 0:
                d_p80 = float(np.nanpercentile(daily_range.values, 80))
                d_p80 = float(np.clip(d_p80, 3.0, 15.0))
            else:
                d_p80 = 8.0  # TSLA typical daily range
        else:
            # Fallback to defaults
            q_to_tr = {25: 0.8, 28: 1.2, 32: 1.5}
            tr_val = q_to_tr.get(q_pct, 1.2)
            d_p80 = 8.0

        g_from_D = d_p80 / L
        grid_g = max(tr_val, g_from_D)

        return {
            "grid_g": round(grid_g, 2),
            "grid_g_floor": 0.8,
            "oneway_thr": 2.2,  # 从历史P90计算
            "max_layers": 4,
            "layer_ratios": [0.25, 0.2, 0.15, 0.1],
            "exit": {
                "anchor_profit_pct": 0.6,
                "eod_grid_multiplier": 0.25,
                "eod_clean_time": "14:30"
            }
        }

    def _calc_b1_params(
        self,
        df5: pd.DataFrame,
        candidate: ParameterCandidate
    ) -> Dict[str, Any]:
        """
        计算B1参数
        Doc §2.1: rvol_min从RVOL分布的指定分位数计算
        """
        # Calculate rvol_min from specified percentile (default P40)
        pct = candidate.b1_rvol_min_pct

        if df5 is not None and not df5.empty and len(df5) >= 78:
            # Simple RVOL approximation
            rolling_vol = df5['volume'].rolling(2, min_periods=1).sum()
            median_vol = rolling_vol.median()
            if median_vol > 0:
                rvol = rolling_vol / median_vol
                rvol_clean = rvol.replace([np.inf, -np.inf], np.nan).dropna()
                if len(rvol_clean) > 0:
                    rvol_min = float(np.nanpercentile(rvol_clean.values, pct))
                    rvol_min = float(np.clip(rvol_min, 0.75, 1.10))
                else:
                    rvol_min = 0.85
            else:
                rvol_min = 0.85
        else:
            rvol_min = 0.85

        return {
            "orb_len": candidate.b1_orb_len,
            "rvol_min": rvol_min,
            "pullback_window_M": 6,
            "extend_orb": False,
            "breakout": {
                "threshold_mult": 1.001,
                "reclaim_mult": 0.998
            },
            "exit": {
                "rvol_threshold": 1.2,
                "profit_mult": 1.002
            }
        }


# ==================== 目标函数计算器 ====================

class ScoreCalculator:
    """
    根据文档 2.2 计算目标函数
    Score = Median(daily PnL_net) - α·Turnover - β·DD_95
    """

    def __init__(self, alpha: float = 0.00001, beta: float = 0.5):
        """
        Args:
            alpha: Turnover惩罚系数 (默认0.00001，意味着每100万换手扣10分)
            beta: 回撤惩罚系数 (默认0.5，意味着每1%回撤扣0.5分)
        """
        self.alpha = alpha
        self.beta = beta

    def calculate_score(
        self,
        daily_pnl: List[float],
        turnover: float,
        dd_95: float
    ) -> float:
        """计算综合得分"""
        if not daily_pnl:
            return -float('inf')

        median_pnl = float(np.median(daily_pnl))
        score = median_pnl - self.alpha * turnover - self.beta * dd_95

        return score

    def extract_metrics_from_backtest(
        self,
        backtest_results: Dict[str, Any]
    ) -> Tuple[List[float], float, float]:
        """
        从回测结果中提取指标

        Returns:
            (daily_pnl, turnover, dd_95)
        """
        # 从equity curve计算每日PnL
        equity_curve = backtest_results.get('equity_curve', [])
        if not equity_curve:
            return ([], 0.0, 0.0)

        # 按日期分组计算每日PnL
        df_eq = pd.DataFrame(equity_curve)
        df_eq['ts'] = pd.to_datetime(df_eq['ts'], unit='ms')
        df_eq['date'] = df_eq['ts'].dt.date

        daily_equity = df_eq.groupby('date')['equity'].last()
        daily_pnl = daily_equity.diff().dropna().tolist()

        # 计算turnover（总成交额）
        trades = backtest_results.get('trades', [])
        turnover = sum(abs(t.get('qty', 0) * t.get('price', 0)) for t in trades)

        # 计算95分位回撤
        equity_values = df_eq['equity'].values
        running_max = np.maximum.accumulate(equity_values)
        drawdowns = (running_max - equity_values) / running_max
        dd_95 = float(np.percentile(drawdowns, 95)) if len(drawdowns) > 0 else 0.0

        return (daily_pnl, turnover, dd_95)


# ==================== 30日前瞻验证器 ====================

class WalkForwardValidator:
    """
    实现文档 2.2 的30日滚动前瞻验证
    """

    def __init__(
        self,
        symbol: str = "TSLA",
        validation_days: int = 30,
        test_days: int = 1
    ):
        self.symbol = symbol
        self.validation_days = validation_days
        self.test_days = test_days
        self.translator = ParameterTranslator(symbol)
        self.scorer = ScoreCalculator()

    async def evaluate_candidate(
        self,
        candidate: ParameterCandidate,
        start_date: str,
        end_date: str,
        historical_data: Dict[str, pd.DataFrame]
    ) -> BacktestResult:
        """
        在指定时间段评估单个候选参数

        Args:
            candidate: 参数候选
            start_date: 开始日期 "YYYY-MM-DD"
            end_date: 结束日期 "YYYY-MM-DD"
            historical_data: 历史数据

        Returns:
            BacktestResult
        """
        # 1. 将候选转换为配置
        config_dict = await self.translator.translate_candidate_to_config(
            candidate,
            historical_data
        )

        # 2. 运行回测
        try:
            config = BacktestConfig(
                symbols=[self.symbol],
                start=f"{start_date} 09:30:00",
                end=f"{end_date} 16:00:00",
                timeframe="1m",
                features_table="trading.features_1m",
                strategy_path="strategies.tsla_v7_2.TSLASimplifiedV7_2",
                strategy_params={"tsla_v7_2_config": config_dict},
                initial_cash=100000,
                commission_per_share=0.0035,
                commission_min=1.0,
                slippage_bps=1.0,
                fill_ref="open"
            )

            engine = BacktestEngine(config)
            # Run in thread pool to avoid "asyncio.run() cannot be called from running loop" error
            loop = asyncio.get_event_loop()
            results = await loop.run_in_executor(None, engine.run)

            # 3. 提取指标
            daily_pnl, turnover, dd_95 = self.scorer.extract_metrics_from_backtest(results)

            # 4. 计算得分
            score = self.scorer.calculate_score(daily_pnl, turnover, dd_95)

            # 5. 构造结果
            median_pnl = float(np.median(daily_pnl)) if daily_pnl else 0.0
            metrics = results.get('metrics', {})

            return BacktestResult(
                candidate=candidate,
                daily_pnl=daily_pnl,
                median_pnl=median_pnl,
                turnover=turnover,
                dd_95=dd_95,
                score=score,
                trades=metrics.get('trades', 0),
                win_rate=metrics.get('win_rate', 0.0)
            )

        except Exception as e:
            print(f"回测失败: {e}")
            # 返回最差结果
            return BacktestResult(
                candidate=candidate,
                daily_pnl=[],
                median_pnl=-float('inf'),
                turnover=0.0,
                dd_95=1.0,
                score=-float('inf'),
                trades=0,
                win_rate=0.0
            )

    async def run_walk_forward(
        self,
        candidates: List[ParameterCandidate],
        end_date: str = "2025-10-14",
        historical_data: Dict[str, pd.DataFrame] = None,
        max_workers: int = 4
    ) -> List[Tuple[ParameterCandidate, float]]:
        """
        对所有候选进行30日前瞻验证

        Args:
            candidates: 参数候选列表
            end_date: 验证结束日期
            historical_data: 历史数据
            max_workers: 并行worker数量

        Returns:
            [(candidate, score), ...] 按得分排序
        """
        # 计算验证期间
        end_dt = pd.to_datetime(end_date)
        start_dt = end_dt - pd.Timedelta(days=self.validation_days + 10)  # 多取10天以防节假日

        start_date_str = start_dt.strftime("%Y-%m-%d")
        end_date_str = end_date

        print(f"\n验证期间: {start_date_str} 至 {end_date_str}")
        print(f"候选数量: {len(candidates)}")
        print(f"并行度: {max_workers}")

        # 评估所有候选
        results = []
        for i, candidate in enumerate(candidates):
            if i % 10 == 0:
                print(f"进度: {i+1}/{len(candidates)}")

            result = await self.evaluate_candidate(
                candidate,
                start_date_str,
                end_date_str,
                historical_data or {}
            )
            results.append(result)

        # 按得分排序
        results.sort(key=lambda r: r.score, reverse=True)

        # 返回 (candidate, score) 列表
        return [(r.candidate, r.score) for r in results]


# ==================== Top-3 Softmax加权 ====================

class ParameterBlender:
    """
    实现文档 2.2 的 Top-3 softmax加权
    """

    @staticmethod
    def blend_top3(
        ranked_results: List[Tuple[ParameterCandidate, float]]
    ) -> Dict[str, Any]:
        """
        对Top-3候选进行softmax加权

        连续参数：加权平均
        离散参数：多数票

        Args:
            ranked_results: [(candidate, score), ...] 已按得分排序

        Returns:
            混合后的参数字典
        """
        if len(ranked_results) < 3:
            raise ValueError("需要至少3个候选")

        top3 = ranked_results[:3]
        scores = np.array([score for _, score in top3])

        # Softmax加权
        exp_scores = np.exp(scores - np.max(scores))  # 数值稳定
        weights = exp_scores / exp_scores.sum()

        print(f"\nTop-3权重: {weights}")

        # 提取候选
        c1, c2, c3 = [candidate for candidate, _ in top3]

        # 连续参数加权平均
        blended = {
            # 这些用加权平均
            'regime_epsilon_pct': int(round(
                weights[0] * c1.regime_epsilon_pct +
                weights[1] * c2.regime_epsilon_pct +
                weights[2] * c3.regime_epsilon_pct
            )),
            'a1_thr_sigma_pct': int(round(
                weights[0] * c1.a1_thr_sigma_pct +
                weights[1] * c2.a1_thr_sigma_pct +
                weights[2] * c3.a1_thr_sigma_pct
            )),
            'a1_time_exit_min': int(round(
                weights[0] * c1.a1_time_exit_min +
                weights[1] * c2.a1_time_exit_min +
                weights[2] * c3.a1_time_exit_min
            )),
            'a2_oneway_thr': float(
                weights[0] * c1.a2_oneway_thr +
                weights[1] * c2.a2_oneway_thr +
                weights[2] * c3.a2_oneway_thr
            ),
            'k_atr_lambda': float(
                weights[0] * c1.k_atr_lambda +
                weights[1] * c2.k_atr_lambda +
                weights[2] * c3.k_atr_lambda
            ),

            # 离散参数用多数票
            'pause_vix_pct': ParameterBlender._majority_vote([c1.pause_vix_pct, c2.pause_vix_pct, c3.pause_vix_pct]),
            'pause_spread_pct': ParameterBlender._majority_vote([c1.pause_spread_pct, c2.pause_spread_pct, c3.pause_spread_pct]),
            'pause_atr_pct': ParameterBlender._majority_vote([c1.pause_atr_pct, c2.pause_atr_pct, c3.pause_atr_pct]),
            'pause_duration_min': ParameterBlender._majority_vote([c1.pause_duration_min, c2.pause_duration_min, c3.pause_duration_min]),
            'a1_rvol_window': ParameterBlender._majority_vote([c1.a1_rvol_window, c2.a1_rvol_window, c3.a1_rvol_window]),
            'a2_grid_q_pct': ParameterBlender._majority_vote([c1.a2_grid_q_pct, c2.a2_grid_q_pct, c3.a2_grid_q_pct]),
            'a2_grid_L': ParameterBlender._majority_vote([c1.a2_grid_L, c2.a2_grid_L, c3.a2_grid_L]),
            'b1_orb_len': ParameterBlender._majority_vote([c1.b1_orb_len, c2.b1_orb_len, c3.b1_orb_len]),
            'b1_rvol_min_pct': ParameterBlender._majority_vote([c1.b1_rvol_min_pct, c2.b1_rvol_min_pct, c3.b1_rvol_min_pct]),
        }

        # 转换回ParameterCandidate
        blended_candidate = ParameterCandidate(**blended)

        return blended_candidate

    @staticmethod
    def _majority_vote(values: List[Any]) -> Any:
        """多数票决策"""
        from collections import Counter
        counter = Counter(values)
        return counter.most_common(1)[0][0]


# ==================== 基线收缩 ====================

class BaselineShrinkage:
    """
    实现文档 2.3 的基线收缩
    p_today = w·p_t + (1-w)·p_base
    w = clip(S, 0.5, 0.9)
    S = 1 - |p_t - p_{t-1}|/MAD(p)
    """

    @staticmethod
    def shrink_to_baseline(
        current_params: ParameterCandidate,
        previous_params: Optional[ParameterCandidate],
        baseline_params: ParameterCandidate,
        history_params: List[ParameterCandidate] = None
    ) -> ParameterCandidate:
        """
        将当前参数向基线收缩

        Args:
            current_params: 当前优化得到的参数
            previous_params: 昨日参数 (用于计算稳定性)
            baseline_params: 180日基线参数
            history_params: 历史参数列表 (用于计算MAD)

        Returns:
            收缩后的参数
        """
        # 如果没有历史，直接返回当前参数
        if previous_params is None:
            return current_params

        # 计算稳定性分数
        stability = BaselineShrinkage._calculate_stability(
            current_params,
            previous_params,
            history_params or []
        )

        # 确定权重
        weight = np.clip(stability, 0.5, 0.9)
        print(f"\n基线收缩: 稳定性={stability:.3f}, 权重={weight:.3f}")

        # 对连续参数进行加权
        shrunk = {}
        for field in ['regime_epsilon_pct', 'a1_thr_sigma_pct', 'a1_time_exit_min',
                      'a2_oneway_thr', 'k_atr_lambda']:
            curr_val = getattr(current_params, field)
            base_val = getattr(baseline_params, field)
            shrunk[field] = type(curr_val)(
                weight * curr_val + (1 - weight) * base_val
            )

        # 离散参数保持当前值（或可以用更复杂的逻辑）
        for field in ['pause_vix_pct', 'pause_spread_pct', 'pause_atr_pct',
                      'pause_duration_min', 'a1_rvol_window', 'a2_grid_q_pct',
                      'a2_grid_L', 'b1_orb_len', 'b1_rvol_min_pct']:
            shrunk[field] = getattr(current_params, field)

        return ParameterCandidate(**shrunk)

    @staticmethod
    def _calculate_stability(
        current: ParameterCandidate,
        previous: ParameterCandidate,
        history: List[ParameterCandidate]
    ) -> float:
        """
        计算稳定性分数
        S = 1 - |p_t - p_{t-1}|/MAD(p)
        """
        # 只对连续参数计算
        fields = ['regime_epsilon_pct', 'a1_thr_sigma_pct', 'k_atr_lambda']

        stabilities = []
        for field in fields:
            curr_val = float(getattr(current, field))
            prev_val = float(getattr(previous, field))

            # 计算MAD (Median Absolute Deviation)
            if history and len(history) >= 3:
                hist_vals = [float(getattr(p, field)) for p in history]
                median = np.median(hist_vals)
                mad = np.median([abs(v - median) for v in hist_vals])
            else:
                mad = abs(curr_val - prev_val) or 1.0  # fallback

            if mad > 0:
                stability = 1 - abs(curr_val - prev_val) / mad
            else:
                stability = 1.0

            stabilities.append(max(0.0, stability))  # 确保非负

        return float(np.mean(stabilities))


# ==================== 目标触发率调整器 ====================

class TriggerRateAdjuster:
    """
    实现文档 2.4 的目标触发率调整
    T_target = 4
    若 H(p) < 0.5*T_target → 调松一级
    若 H(p) > 2*T_target → 收紧一级
    """

    def __init__(self, target_triggers: int = 4):
        self.target_triggers = target_triggers

    def adjust_for_trigger_rate(
        self,
        params: ParameterCandidate,
        historical_data: Dict[str, pd.DataFrame],
        estimated_triggers: Optional[int] = None
    ) -> ParameterCandidate:
        """
        根据触发率估算调整参数

        Args:
            params: 当前参数
            historical_data: 历史数据
            estimated_triggers: 估算的触发次数 (可选，否则自动计算)

        Returns:
            调整后的参数
        """
        # 如果未提供估算值，则计算
        if estimated_triggers is None:
            estimated_triggers = self._estimate_triggers(params, historical_data)

        print(f"\n触发率调整: 估算触发={estimated_triggers}, 目标={self.target_triggers}")

        adjusted = params.to_dict()

        # 太少触发 → 放宽
        if estimated_triggers < 0.5 * self.target_triggers:
            print("  → 触发过少，放宽一级")
            # A1阈值降低一档 (从{55,60,65,70,75}降低5)
            adjusted['a1_thr_sigma_pct'] = max(55, params.a1_thr_sigma_pct - 5)

            # A2网格步长减小 (从{25,28,32}降低3)
            adjusted['a2_grid_q_pct'] = max(25, params.a2_grid_q_pct - 3)

        # 太多触发 → 收紧
        elif estimated_triggers > 2 * self.target_triggers:
            print("  → 触发过多，收紧一级")
            adjusted['a1_thr_sigma_pct'] = min(75, params.a1_thr_sigma_pct + 5)
            adjusted['a2_grid_q_pct'] = min(32, params.a2_grid_q_pct + 3)

        else:
            print("  → 触发率适中，保持不变")

        return ParameterCandidate(**adjusted)

    def _estimate_triggers(
        self,
        params: ParameterCandidate,
        historical_data: Dict[str, pd.DataFrame]
    ) -> int:
        """
        粗略估算参数在历史数据上会产生多少触发

        简化：统计 A1 + A2 的潜在触发次数
        """
        df1 = historical_data.get('1m')
        df5 = historical_data.get('5m')

        if df1 is None or df5 is None or df1.empty or df5.empty:
            return 0

        # A1: 统计VWAP偏离满足阈值的次数
        # 简化：假设触发权重0.1
        a1_count = 0  # TODO: 实现具体计算

        # A2: 统计价格触及网格的次数
        # 简化：假设触发权重0.05
        a2_count = 0  # TODO: 实现具体计算

        return int(a1_count * 0.1 + a2_count * 0.05)


# ==================== 护栏验证器 ====================

class GuardrailValidator:
    """
    实现文档 2.5 的护栏验证和LKG回退
    """

    # 硬边界定义 (从文档提取)
    BOUNDS = {
        'A1_thr_sigma': (0.004, 0.020),  # 0.4%-2.0%
        'A2_grid_g': (0.6, 3.5),  # USD
        'k_atr': (1.4, 3.0),
        'A1_rvol_lo': (0.75, 1.10),
        'A1_rvol_hi': (1.0, 2.5),
        'A1_time_exit_min': (25, 60),
    }

    @staticmethod
    def validate_and_adjust(
        params: ParameterCandidate,
        lkg_params: Optional[ParameterCandidate] = None,
        adjustment_factors: Optional[Dict[str, float]] = None
    ) -> Tuple[ParameterCandidate, bool]:
        """
        验证参数是否在边界内，越界则回退到LKG

        Args:
            params: 待验证参数
            lkg_params: Last Known Good参数
            adjustment_factors: 调整系数 {param: factor}

        Returns:
            (validated_params, is_valid)
        """
        # TODO: 将候选参数转换为实际值后验证
        # 这里简化为直接返回
        is_valid = True

        # 简单检查：a1_thr_sigma_pct是否在合理范围
        if not (55 <= params.a1_thr_sigma_pct <= 75):
            is_valid = False

        if not is_valid and lkg_params is not None:
            print("\n[警告] 参数越界，回退到LKG")
            if adjustment_factors:
                # 应用调整因子 (k_atr +10%, thr_sigma +5%, g +5%)
                adjusted = lkg_params.to_dict()
                # TODO: 应用具体调整
                return ParameterCandidate(**adjusted), False
            return lkg_params, False

        return params, is_valid


# ==================== 完整优化流程编排 ====================

class ParameterBrainOrchestrator:
    """
    完整参数优化流程编排器
    """

    def __init__(self, symbol: str = "TSLA"):
        self.symbol = symbol
        self.generator = CandidateGenerator()
        self.validator = WalkForwardValidator(symbol)
        self.blender = ParameterBlender()
        self.shrinker = BaselineShrinkage()
        self.adjuster = TriggerRateAdjuster()
        self.guardrail = GuardrailValidator()

    async def run_full_calibration(
        self,
        end_date: str = "2025-10-14",
        use_reduced_candidates: bool = True,
        baseline_params: Optional[ParameterCandidate] = None,
        lkg_params: Optional[ParameterCandidate] = None,
        previous_params: Optional[ParameterCandidate] = None
    ) -> Dict[str, Any]:
        """
        执行完整的参数优化流程

        Returns:
            {
                'final_params': ParameterCandidate,
                'final_config': Dict,
                'top3_results': List,
                'validation_passed': bool
            }
        """
        print("=" * 80)
        print("开始完整参数优化流程")
        print("=" * 80)

        # 1. 生成候选集
        print("\n[1/7] 生成候选集...")
        if use_reduced_candidates:
            candidates = self.generator.generate_reduced_candidates()
        else:
            candidates = self.generator.generate_all_candidates()
        print(f"  候选数量: {len(candidates)}")

        # 2. 30日前瞻验证
        print("\n[2/7] 执行前瞻验证...")
        # REAL VALIDATION - test with first 3 candidates only
        test_candidates = candidates[:3]
        print(f"  [测试模式] 只验证前3个候选: {len(test_candidates)}")
        ranked_results = await self.validator.run_walk_forward(test_candidates, end_date)

        # 3. Top-3 softmax加权
        print("\n[3/7] Top-3加权...")
        blended_params = self.blender.blend_top3(ranked_results)
        print(f"  混合参数: a1_sigma_pct={blended_params.a1_thr_sigma_pct}")

        # 4. 基线收缩
        print("\n[4/7] 基线收缩...")
        if baseline_params and previous_params:
            shrunk_params = self.shrinker.shrink_to_baseline(
                blended_params,
                previous_params,
                baseline_params
            )
        else:
            shrunk_params = blended_params
            print("  无基线/历史参数，跳过收缩")

        # 5. 目标触发率调整
        print("\n[5/7] 触发率调整...")
        adjusted_params = self.adjuster.adjust_for_trigger_rate(
            shrunk_params,
            {},  # 需要历史数据
            estimated_triggers=2  # Mock
        )

        # 6. 护栏验证
        print("\n[6/7] 护栏验证...")
        final_params, is_valid = self.guardrail.validate_and_adjust(
            adjusted_params,
            lkg_params
        )
        print(f"  验证结果: {'通过' if is_valid else '回退到LKG'}")

        # 7. 生成最终配置
        print("\n[7/7] 生成最终配置...")
        translator = ParameterTranslator(self.symbol)
        final_config = await translator.translate_candidate_to_config(
            final_params,
            {}  # 需要历史数据
        )

        result = {
            'final_params': final_params,
            'final_config': final_config,
            'top3_results': ranked_results[:3],
            'validation_passed': is_valid
        }

        print("\n" + "=" * 80)
        print("参数优化完成！")
        print("=" * 80)

        return result


# ==================== 主函数 ====================

async def main():
    """参数优化主流程"""
    orchestrator = ParameterBrainOrchestrator(symbol="TSLA")

    # 运行完整优化
    result = await orchestrator.run_full_calibration(
        end_date="2025-10-14",
        use_reduced_candidates=True
    )

    # 保存结果
    output_file = "strategies/configs/tsla_optimized_params.json"
    with open(output_file, 'w') as f:
        # 只保存配置，候选参数不能直接序列化
        json.dump(result['final_config'], f, indent=2)

    print(f"\n最终配置已保存到: {output_file}")

    # 打印关键参数
    params = result['final_params']
    print(f"\n关键参数:")
    print(f"  A1 thr_sigma_pct: {params.a1_thr_sigma_pct}")
    print(f"  A2 grid_q_pct: {params.a2_grid_q_pct}")
    print(f"  k_ATR lambda: {params.k_atr_lambda}")
    print(f"  验证通过: {result['validation_passed']}")


if __name__ == "__main__":
    asyncio.run(main())
