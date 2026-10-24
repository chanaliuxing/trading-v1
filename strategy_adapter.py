# -*- coding: utf-8 -*-
"""
Strategy Adapter Interface
--------------------------
通用策略适配器接口，用于解耦 HPO 框架和具体策略实现

每个策略需要实现自己的适配器，定义:
1. 数据加载逻辑
2. 参数解析逻辑
3. 数据验证规则

Author: Trading Team
Date: 2025-10-26
"""

from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional
import pandas as pd


class StrategyAdapter(ABC):
    """
    策略适配器基类

    设计原则:
    - 策略无关: 框架不应假设任何特定策略的数据或参数结构
    - 可插拔: 新策略只需实现此接口
    - 向后兼容: 现有策略通过适配器继续工作
    """

    @abstractmethod
    def load_data(
        self,
        symbols: List[str],
        start: str,
        end: str,
        clickhouse_config: Dict[str, Any],
        **kwargs
    ) -> pd.DataFrame:
        """
        加载策略所需的数据

        框架不对返回的 DataFrame 做任何假设，由策略自己决定列名和结构

        Args:
            symbols: 股票代码列表
            start: 开始时间 "YYYY-MM-DD HH:MM:SS"
            end: 结束时间
            clickhouse_config: ClickHouse 连接配置 {host, port, user, password, database}
            **kwargs: 额外参数，如 features_table, timeframe 等

        Returns:
            DataFrame 包含策略需要的所有列

        Raises:
            ValueError: 数据加载失败

        Example:
            >>> adapter = MyStrategyAdapter()
            >>> df = adapter.load_data(
            ...     symbols=["TSLA", "NVDA"],
            ...     start="2025-07-01 09:30:00",
            ...     end="2025-07-31 16:00:00",
            ...     clickhouse_config={"host": "localhost", "port": 8123},
            ...     features_table="trading.features_1m"
            ... )
            >>> print(df.columns)
            Index(['ts', 'symbol', 'open', 'high', 'low', 'close', 'volume', 'vwap', 'atr_14'])
        """
        pass

    @abstractmethod
    def validate_data(self, df: pd.DataFrame) -> None:
        """
        验证数据是否满足策略要求

        Args:
            df: 待验证的 DataFrame

        Raises:
            ValueError: 数据不符合要求（缺少列、数据为空等）

        Example:
            >>> adapter.validate_data(df)
            ValueError: 缺少必需列: {'vwap', 'atr_14'}
        """
        pass

    @abstractmethod
    def parse_params(
        self,
        grid_params: Dict[str, Any],
        base_extras: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        解析和合并参数

        将 grid 生成的参数组合与基础配置合并，返回策略最终的 params 字典

        Args:
            grid_params: 从 grid 生成的参数组合
                例如: {"rsi_period": 14, "bb_std": 2.0}
                或: {"strategy_A.near_vwap_atr": 0.20, "strategy_A.sl_atr": 0.50}

            base_extras: 基础配置 (strategy_base_extras)
                例如: {"universe": ["TSLA"], "risk": {"per_trade_pct": 0.0025}}

        Returns:
            策略最终的 params 字典，传递给 BacktestEngine
            通常格式: {"extras": {...}}

        Example (扁平参数):
            >>> grid_params = {"rsi_period": 14, "bb_std": 2.0}
            >>> base_extras = {"tsla_config": {"symbol": "TSLA"}}
            >>> result = adapter.parse_params(grid_params, base_extras)
            >>> print(result)
            {"extras": {"tsla_config": {"symbol": "TSLA"}, "rsi_period": 14, "bb_std": 2.0}}

        Example (嵌套参数，点分隔):
            >>> grid_params = {"strategy_A.near_vwap_atr": 0.20, "strategy_A.sl_atr": 0.50}
            >>> base_extras = {"universe": ["TSLA"], "risk": {"per_trade_pct": 0.0025}}
            >>> result = adapter.parse_params(grid_params, base_extras)
            >>> print(result)
            {
                "extras": {
                    "universe": ["TSLA"],
                    "risk": {"per_trade_pct": 0.0025},
                    "strategy_A": {"near_vwap_atr": 0.20, "sl_atr": 0.50}
                }
            }
        """
        pass

    @abstractmethod
    def get_required_columns(self) -> List[str]:
        """
        返回策略必需的数据列

        用于文档和错误提示，也可用于数据验证

        Returns:
            列名列表

        Example:
            >>> adapter.get_required_columns()
            ['ts', 'symbol', 'open', 'high', 'low', 'close', 'volume', 'vwap', 'atr_14']
        """
        pass

    def supports_ml_signals(self) -> bool:
        """
        策略是否支持 ML 信号增强

        如果返回 True，框架会调用 _prepare_model_data() 生成 ml_signal, ml_conf 列

        Returns:
            True 如果策略支持 ML 信号，False 否则

        Example:
            >>> adapter.supports_ml_signals()
            False  # RevE 是纯规则策略，不需要 ML 信号
        """
        return False

    def get_default_params(self) -> Dict[str, Any]:
        """
        返回策略的默认参数

        用于文档和快速测试

        Returns:
            默认参数字典

        Example:
            >>> adapter.get_default_params()
            {"rsi_period": 14, "bb_std": 2.0, "atr_period": 14}
        """
        return {}

    def get_data_sources(self) -> List[str]:
        """
        返回策略需要的数据源列表

        用于文档和依赖检查

        Returns:
            数据源列表（表名或服务名）

        Example:
            >>> adapter.get_data_sources()
            ['trading.features_1m', 'trading.bars_5m', 'redis.sfs_1s']
        """
        return []

    def get_strategy_description(self) -> str:
        """
        返回策略描述

        用于文档和日志

        Returns:
            策略描述字符串
        """
        return ""


class DefaultAdapter(StrategyAdapter):
    """
    默认适配器实现

    提供基本的数据加载和参数传递逻辑，适用于简单策略
    """

    def load_data(
        self,
        symbols: List[str],
        start: str,
        end: str,
        clickhouse_config: Dict[str, Any],
        **kwargs
    ) -> pd.DataFrame:
        """
        从 ClickHouse features_table 加载基础数据
        """
        try:
            from clickhouse_driver import Client
        except ImportError:
            raise RuntimeError("需要安装 clickhouse-driver: pip install clickhouse-driver")

        features_table = kwargs.get("features_table", "trading.features_1m")
        syms = ",".join([f"'{s}'" for s in symbols])

        client = Client(**clickhouse_config)

        sql = f"""
        SELECT ts, symbol, open, high, low, close, volume
        FROM {features_table}
        WHERE symbol IN ({syms})
          AND ts >= toDateTime('{start}')
          AND ts < toDateTime('{end}')
        ORDER BY ts, symbol
        """

        data = client.execute(sql)
        df = pd.DataFrame(data, columns=["ts", "symbol", "open", "high", "low", "close", "volume"])

        return df

    def validate_data(self, df: pd.DataFrame) -> None:
        """验证基础 OHLCV 列"""
        required = ["ts", "symbol", "open", "high", "low", "close", "volume"]
        missing = set(required) - set(df.columns)
        if missing:
            raise ValueError(f"缺少必需列: {missing}")

        if df.empty:
            raise ValueError("数据为空")

    def parse_params(
        self,
        grid_params: Dict[str, Any],
        base_extras: Dict[str, Any]
    ) -> Dict[str, Any]:
        """扁平参数合并"""
        return {
            "extras": {
                **base_extras,
                **grid_params
            }
        }

    def get_required_columns(self) -> List[str]:
        return ["ts", "symbol", "open", "high", "low", "close", "volume"]

    def get_strategy_description(self) -> str:
        return "Default strategy adapter with basic OHLCV data"
