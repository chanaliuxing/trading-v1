# -*- coding: utf-8 -*-
"""
TSLA V7_2 Strategy Adapter
---------------------------
TSLA Simplified V7_2 策略的 HPO 适配器

特点:
1. 单数据源: features_1m
2. 扁平参数: 使用 strategy_config 包装
3. ML 增强: 支持 ml_signal, ml_conf 列

Author: Trading Team
Date: 2025-10-26
"""

from __future__ import annotations
from typing import Dict, Any, List
import pandas as pd

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategy_adapter import StrategyAdapter


class TSLAV72Adapter(StrategyAdapter):
    """TSLA V7_2 策略的适配器"""

    def load_data(
        self,
        symbols: List[str],
        start: str,
        end: str,
        clickhouse_config: Dict[str, Any],
        **kwargs
    ) -> pd.DataFrame:
        """
        加载 TSLA V7_2 策略需要的数据

        数据源: features_1m (基础 OHLCV + 技术指标)

        Returns:
            DataFrame 包含: ts, symbol, open, high, low, close, volume
                          + 技术指标 (rsi_14, bb_upper, bb_lower 等)
        """
        # Prefer HTTP client if available; fall back to native TCP driver
        features_table = kwargs.get("features_table", "trading.features_1m")
        timeframe = kwargs.get("timeframe", "1m")
        syms = ",".join([f"'{s}'" for s in symbols])

        # 构建 SQL 查询 - 加载所有可用列
        # TSLA V7_2 主要使用: RSI, Bollinger Bands, ATR, EMA 等
        # Note: Column names adjusted for features_5s table schema
        sql = f"""
        SELECT
            ts,
            symbol,
            open,
            high,
            low,
            close,
            volume,
            rsi_14,
            bb_up_20 as bb_upper,
            bb_mid_20 as bb_middle,
            bb_dn_20 as bb_lower,
            atr_14,
            ema_20,
            ema_50,
            sma_20,
            sma_60 as sma_50,
            stoch_k_14 as stoch_k,
            stoch_d_3 as stoch_d
        FROM {features_table}
        WHERE symbol IN ({syms})
          AND ts >= toDateTime('{start}')
          AND ts < toDateTime('{end}')
        ORDER BY ts, symbol
        """

        def _query_via_http(cfg: Dict[str, Any], q: str):
            try:
                import clickhouse_connect  # type: ignore
            except Exception:
                return None
            try:
                client = clickhouse_connect.get_client(
                    host=cfg.get("host", "localhost"),
                    port=int(cfg.get("port", 8123) or 8123),
                    username=cfg.get("user") or cfg.get("username") or "default",
                    password=cfg.get("password", ""),
                    database=cfg.get("database", "default"),
                    secure=bool(cfg.get("secure", False)),
                    connect_timeout=5,
                    send_receive_timeout=30,
                )
                res = client.query(q)
                # clickhouse_connect returns a QueryResult; .result_rows yields data
                return res.result_rows
            except Exception:
                return None

        def _query_via_native(cfg: Dict[str, Any], q: str):
            try:
                from clickhouse_driver import Client  # type: ignore
            except Exception:
                return None
            # Auto-correct common misconfig: HTTP port provided to native driver
            cfg2 = dict(cfg)
            try:
                if int(cfg2.get("port", 9000) or 9000) == 8123:
                    cfg2["port"] = 9000
            except Exception:
                pass
            try:
                client = Client(**cfg2)
                return client.execute(q)
            except Exception:
                return None

        data = _query_via_http(clickhouse_config, sql)
        if data is None:
            data = _query_via_native(clickhouse_config, sql)
        if data is None:
            raise ValueError(
                "从 ClickHouse 加载数据失败: 无法通过 HTTP(clickhouse-connect) 或原生驱动(clickhouse-driver) 查询。\n"
                f"请检查端口与驱动是否匹配 (HTTP=8123, 原生=9000)。\n表: {features_table}\n符号: {symbols}\n时间范围: {start} ~ {end}"
            )

        # 定义列名 (adjusted for features_5s schema)
        columns = [
            "ts", "symbol", "open", "high", "low", "close", "volume",
            "rsi_14", "bb_upper", "bb_middle", "bb_lower",
            "atr_14", "ema_20", "ema_50", "sma_20", "sma_50",
            "stoch_k", "stoch_d"
        ]

        df = pd.DataFrame(data, columns=columns)

        return df

    def validate_data(self, df: pd.DataFrame) -> None:
        """
        验证 TSLA V7_2 策略需要的列是否存在
        """
        # 最小要求: OHLCV + RSI
        required = [
            "ts", "symbol", "open", "high", "low", "close", "volume",
            "rsi_14"
        ]

        missing = set(required) - set(df.columns)
        if missing:
            raise ValueError(
                f"TSLA V7_2 策略缺少必需列: {missing}\n"
                f"当前列: {list(df.columns)}\n"
                f"请确保 ClickHouse 表包含所有必需列"
            )

        if df.empty:
            raise ValueError("数据为空，请检查时间范围和股票代码")

        # 检查关键列是否有太多 NaN
        critical_cols = ["close", "rsi_14"]
        for col in critical_cols:
            if col in df.columns:
                nan_pct = df[col].isna().mean()
                if nan_pct > 0.5:
                    print(
                        f"[TSLAV72Adapter] 警告: 列 '{col}' 有 {nan_pct:.1%} 的缺失值，"
                        f"可能影响回测结果"
                    )

    def parse_params(
        self,
        grid_params: Dict[str, Any],
        base_extras: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        解析 TSLA V7_2 的扁平参数结构 (向后兼容原有格式)

        TSLA V7_2 的参数结构 (原始格式):
        {
            "extras": {
                "tsla_config": {...},
                "strategy_config": {
                    "rsi_period": 14,
                    "bb_std": 2.0,
                    "atr_period": 14
                }
            }
        }

        Grid 使用扁平参数:
        {
            "rsi_period": 14,
            "bb_std": 2.0,
            "atr_period": 14
        }

        解析逻辑: 将 grid_params 合并到 strategy_config 中
        """
        # 深拷贝避免修改原始配置
        import copy
        result = copy.deepcopy(base_extras)

        # 确保 strategy_config 存在
        if "strategy_config" not in result:
            result["strategy_config"] = {}

        # 合并 grid_params 到 strategy_config
        result["strategy_config"].update(grid_params)

        return {"extras": result}

    def get_required_columns(self) -> List[str]:
        """TSLA V7_2 需要的列"""
        return [
            "ts", "symbol", "open", "high", "low", "close", "volume",
            "rsi_14", "bb_upper", "bb_middle", "bb_lower", "atr_14"
        ]

    def supports_ml_signals(self) -> bool:
        """TSLA V7_2 支持 ML 信号增强"""
        return True

    def get_default_params(self) -> Dict[str, Any]:
        """TSLA V7_2 的默认参数"""
        return {
            "tsla_config": {
                "symbol": "TSLA"
            },
            "strategy_config": {
                "rsi_period": 14,
                "rsi_oversold": 30,
                "rsi_overbought": 70,
                "bb_period": 20,
                "bb_std": 2.0,
                "atr_period": 14,
                "atr_multiplier": 2.0,
                "ema_fast": 12,
                "ema_slow": 26,
                "position_size_pct": 0.10
            }
        }

    def get_data_sources(self) -> List[str]:
        """TSLA V7_2 使用的数据源"""
        return [
            "trading.features_1m"
        ]

    def get_strategy_description(self) -> str:
        """TSLA V7_2 策略描述"""
        return (
            "TSLA Simplified V7_2 - 技术指标策略\n"
            "基于 RSI, Bollinger Bands, ATR 等技术指标\n"
            "支持 ML 信号增强 (XGBoost/LSTM)"
        )


# ============================================================================
# 单元测试
# ============================================================================

if __name__ == "__main__":
    print("=" * 60)
    print("TSLA V7_2 Adapter Test")
    print("=" * 60)

    adapter = TSLAV72Adapter()

    # 测试 1: 参数解析 - 扁平参数
    print("\n测试 1: 扁平参数解析")
    grid_params = {
        "rsi_period": 14,
        "bb_std": 2.5,
        "atr_multiplier": 1.5
    }
    base_extras = {
        "tsla_config": {"symbol": "TSLA"},
        "strategy_config": {
            "position_size_pct": 0.10
        }
    }
    result = adapter.parse_params(grid_params, base_extras)
    print(f"Grid params: {grid_params}")
    print(f"Base extras: {base_extras}")
    print(f"Result: {result}")
    print(f"Strategy config: {result['extras']['strategy_config']}")

    # 测试 2: 默认参数
    print("\n测试 2: 默认参数")
    defaults = adapter.get_default_params()
    print(f"TSLA config: {defaults.get('tsla_config', {})}")
    print(f"Strategy config: {defaults.get('strategy_config', {})}")

    # 测试 3: 元信息
    print("\n测试 3: 适配器元信息")
    print(f"必需列: {adapter.get_required_columns()}")
    print(f"支持 ML: {adapter.supports_ml_signals()}")
    print(f"数据源: {adapter.get_data_sources()}")
    print(f"描述:\n{adapter.get_strategy_description()}")

    print("\n" + "=" * 60)
    print("✅ 所有测试通过")
