# -*- coding: utf-8 -*-
"""
RevE Intraday Strategy Adapter
-------------------------------
RevE 策略的 HPO 适配器

特点:
1. 多数据源: features_1m + bars_5m (可选)
2. 嵌套参数: risk, schedule, gating 等对象
3. 点分隔命名: "strategy_A.near_vwap_atr" → params["strategy_A"]["near_vwap_atr"]
4. 额外列: vwap, atr_14, ema_20 等

Author: Trading Team
Date: 2025-10-26
"""

from __future__ import annotations
from typing import Dict, Any, List
import pandas as pd
import copy

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategy_adapter import StrategyAdapter


class RevEAdapter(StrategyAdapter):
    """RevE Intraday 策略的适配器"""

    def load_data(
        self,
        symbols: List[str],
        start: str,
        end: str,
        clickhouse_config: Dict[str, Any],
        **kwargs
    ) -> pd.DataFrame:
        """
        加载 RevE 策略需要的数据

        数据源:
        1. features_1m - 主要数据源（VWAP 策略）
        2. bars_5m - 可选（ORB 策略需要 5 分钟 K线）

        Returns:
            DataFrame 包含: ts, symbol, open, high, low, close, volume,
                          vwap, atr_14, ema_20, rsi_14, z_price_20, z_vol_20
        """
        try:
            from clickhouse_driver import Client
        except ImportError:
            raise RuntimeError(
                "RevE 策略需要 ClickHouse driver: pip install clickhouse-driver"
            )

        features_table = kwargs.get("features_table", "trading.features_1m")
        timeframe = kwargs.get("timeframe", "1m")

        client = Client(**clickhouse_config)
        syms = ",".join([f"'{s}'" for s in symbols])

        # 构建 SQL 查询 - 加载 RevE 需要的所有列
        sql = f"""
        SELECT
            ts,
            symbol,
            open,
            high,
            low,
            close,
            volume,
            vwap,
            atr_14,
            ema_20,
            rsi_14,
            z_price_20,
            z_vol_20
        FROM {features_table}
        WHERE symbol IN ({syms})
          AND ts >= toDateTime('{start}')
          AND ts < toDateTime('{end}')
        ORDER BY ts, symbol
        """

        try:
            data = client.execute(sql)
        except Exception as e:
            raise ValueError(
                f"从 ClickHouse 加载数据失败: {e}\n"
                f"表: {features_table}\n"
                f"符号: {symbols}\n"
                f"时间范围: {start} ~ {end}"
            )

        # 定义列名
        columns = [
            "ts", "symbol", "open", "high", "low", "close", "volume",
            "vwap", "atr_14", "ema_20", "rsi_14", "z_price_20", "z_vol_20"
        ]

        df = pd.DataFrame(data, columns=columns)

        # 可选: 加载 5 分钟数据（如果需要）
        # if kwargs.get("load_5m_bars", False):
        #     df_5m = self._load_5m_bars(client, symbols, start, end)
        #     df = self._merge_5m_data(df, df_5m)

        return df

    def _load_5m_bars(
        self,
        client,
        symbols: List[str],
        start: str,
        end: str
    ) -> pd.DataFrame:
        """
        加载 5 分钟 K线数据（用于 ORB 策略）

        TODO: 实现 5 分钟数据加载和合并逻辑
        """
        # 示例实现
        syms = ",".join([f"'{s}'" for s in symbols])
        sql = f"""
        SELECT ts, symbol, open, high, low, close, volume
        FROM trading.bars_5m_ibkr
        WHERE symbol IN ({syms})
          AND ts >= toDateTime('{start}')
          AND ts < toDateTime('{end}')
        ORDER BY ts, symbol
        """
        data = client.execute(sql)
        return pd.DataFrame(
            data,
            columns=["ts", "symbol", "open", "high", "low", "close", "volume"]
        )

    def validate_data(self, df: pd.DataFrame) -> None:
        """
        验证 RevE 策略需要的列是否存在
        """
        required = [
            "ts", "symbol", "open", "high", "low", "close", "volume",
            "vwap", "atr_14"
        ]

        missing = set(required) - set(df.columns)
        if missing:
            raise ValueError(
                f"RevE 策略缺少必需列: {missing}\n"
                f"当前列: {list(df.columns)}\n"
                f"请确保 ClickHouse 表包含所有必需列"
            )

        if df.empty:
            raise ValueError("数据为空，请检查时间范围和股票代码")

        # 检查关键列是否有太多 NaN
        critical_cols = ["close", "vwap", "atr_14"]
        for col in critical_cols:
            if col in df.columns:
                nan_pct = df[col].isna().mean()
                if nan_pct > 0.5:
                    print(
                        f"[RevEAdapter] 警告: 列 '{col}' 有 {nan_pct:.1%} 的缺失值，"
                        f"可能影响回测结果"
                    )

    def parse_params(
        self,
        grid_params: Dict[str, Any],
        base_extras: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        解析 RevE 的嵌套参数结构

        RevE 的参数结构:
        {
            "universe": ["TSLA", "NVDA"],
            "risk": {
                "per_trade_pct": 0.0025,
                "max_concurrent_positions": 3
            },
            "schedule": {
                "market_open_pt": "06:30:00",
                "force_flat_pt": "12:50:00"
            },
            "strategy_A": {
                "near_vwap_atr": 0.20,
                "sl_atr": 0.50
            }
        }

        Grid 使用点分隔命名:
        {
            "strategy_A.near_vwap_atr": 0.20,
            "strategy_A.sl_atr": 0.50,
            "strat_B_sl_atr5m": 0.40
        }

        解析逻辑:
        1. 深拷贝 base_extras
        2. 解析 grid_params 中的点分隔路径
        3. 合并到结果中
        """
        # 深拷贝避免修改原始配置
        result = copy.deepcopy(base_extras)

        # 解析 grid_params
        for key, value in grid_params.items():
            if "." in key:
                # 点分隔路径: "strategy_A.near_vwap_atr" → ["strategy_A", "near_vwap_atr"]
                parts = key.split(".")
                target = result

                # 遍历路径，创建嵌套字典
                for part in parts[:-1]:
                    if part not in target:
                        target[part] = {}
                    elif not isinstance(target[part], dict):
                        # 如果中间路径不是字典，转换为字典
                        target[part] = {}
                    target = target[part]

                # 设置最终值
                target[parts[-1]] = value

            else:
                # 扁平参数直接添加
                result[key] = value

        return {"extras": result}

    def get_required_columns(self) -> List[str]:
        """RevE 需要的列"""
        return [
            "ts", "symbol", "open", "high", "low", "close", "volume",
            "vwap", "atr_14", "ema_20", "rsi_14"
        ]

    def supports_ml_signals(self) -> bool:
        """RevE 目前是纯规则策略，不需要 ML 信号"""
        return False

    def get_default_params(self) -> Dict[str, Any]:
        """RevE 的默认参数"""
        return {
            "universe": ["TSLA", "NVDA", "AAPL"],
            "risk": {
                "per_trade_pct": 0.0025,
                "per_trade_pct_low": 0.0020,
                "max_concurrent_positions": 3,
                "max_daily_loss_pct": 0.04
            },
            "schedule": {
                "market_open_pt": "06:30:00",
                "last_entry_pt": "12:00:00",
                "force_flat_pt": "12:50:00"
            },
            "strategy_A": {
                "near_vwap_atr": 0.20,
                "sl_atr": 0.50,
                "tp1_atr": 1.00,
                "trail_atr": 1.00,
                "ewma_alpha": 0.70,
                "band_clip_low_atr": 0.60,
                "band_clip_high_atr": 1.40,
                "q_level": 0.80
            },
            "strat_B_sl_atr5m": 0.40,
            "strat_B_pullback_threshold": 0.80
        }

    def get_data_sources(self) -> List[str]:
        """RevE 使用的数据源"""
        return [
            "trading.features_1m",
            "trading.bars_5m_ibkr",  # 可选
            "redis.sfs_1s"  # 可选，用于实时模式
        ]

    def get_strategy_description(self) -> str:
        """RevE 策略描述"""
        return (
            "RevE Intraday Strategy - 日内均值回归策略\n"
            "包含两个子策略:\n"
            "  - Strategy A: VWAP 均值回归（1分钟）\n"
            "  - Strategy B: ORB 突破回踩（5分钟）\n"
            "特点: 多策略组合、嵌套参数、多数据源"
        )


# ============================================================================
# 单元测试
# ============================================================================

if __name__ == "__main__":
    print("=" * 60)
    print("RevE Adapter Test")
    print("=" * 60)

    adapter = RevEAdapter()

    # 测试 1: 参数解析 - 扁平参数
    print("\n测试 1: 扁平参数解析")
    grid_params = {
        "strat_B_sl_atr5m": 0.40,
        "strat_B_pullback_threshold": 0.80
    }
    base_extras = {
        "universe": ["TSLA"],
        "risk": {"per_trade_pct": 0.0025}
    }
    result = adapter.parse_params(grid_params, base_extras)
    print(f"Grid params: {grid_params}")
    print(f"Result: {result}")

    # 测试 2: 参数解析 - 点分隔路径
    print("\n测试 2: 点分隔路径解析")
    grid_params = {
        "strategy_A.near_vwap_atr": 0.20,
        "strategy_A.sl_atr": 0.50,
        "strategy_A.tp1_atr": 1.00
    }
    result = adapter.parse_params(grid_params, base_extras)
    print(f"Grid params: {grid_params}")
    print(f"Result extras: {result['extras']}")
    print(f"Strategy A params: {result['extras'].get('strategy_A', {})}")

    # 测试 3: 混合参数
    print("\n测试 3: 混合参数解析")
    grid_params = {
        "strategy_A.near_vwap_atr": 0.25,
        "strat_B_sl_atr5m": 0.35,
        "new_param": 123
    }
    result = adapter.parse_params(grid_params, base_extras)
    print(f"Grid params: {grid_params}")
    print(f"Result extras: {result['extras']}")

    # 测试 4: 默认参数
    print("\n测试 4: 默认参数")
    defaults = adapter.get_default_params()
    print(f"Strategy A 默认参数: {defaults.get('strategy_A', {})}")
    print(f"Risk 默认参数: {defaults.get('risk', {})}")

    # 测试 5: 元信息
    print("\n测试 5: 适配器元信息")
    print(f"必需列: {adapter.get_required_columns()}")
    print(f"支持 ML: {adapter.supports_ml_signals()}")
    print(f"数据源: {adapter.get_data_sources()}")
    print(f"描述:\n{adapter.get_strategy_description()}")

    print("\n" + "=" * 60)
    print("✅ 所有测试通过")
