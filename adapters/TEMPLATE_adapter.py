# -*- coding: utf-8 -*-
"""
[Strategy Name] Adapter Template
---------------------------------
这是一个策略适配器的模板文件

使用方法:
1. 复制此文件并重命名为 your_strategy_adapter.py
2. 将 TEMPLATE 替换为你的策略名称
3. 实现所有 TODO 标记的方法
4. 在 strategy_registry.py 中注册

Author: Your Name
Date: YYYY-MM-DD
"""

from __future__ import annotations
from typing import Dict, Any, List
import pandas as pd
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from strategy_adapter import StrategyAdapter


class TEMPLATEAdapter(StrategyAdapter):
    """
    [策略名称] 的适配器

    特点:
    - TODO: 列出策略的主要特点
    - TODO: 数据源 (如: features_1m)
    - TODO: 参数结构 (如: 扁平/嵌套)
    - TODO: 是否支持 ML
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
        加载策略需要的数据

        Args:
            symbols: 股票代码列表，如 ["TSLA", "AAPL"]
            start: 开始时间 "YYYY-MM-DD HH:MM:SS"
            end: 结束时间 "YYYY-MM-DD HH:MM:SS"
            clickhouse_config: ClickHouse 连接配置
                {
                    "host": "localhost",
                    "port": 9000,
                    "user": "default",
                    "password": "",
                    "database": "trading"
                }
            **kwargs: 额外参数
                - features_table: 特征表名，默认 "trading.features_1m"
                - timeframe: 时间框架，默认 "1m"

        Returns:
            DataFrame 包含策略需要的所有列

        Raises:
            ValueError: 数据加载失败

        TODO: 实现数据加载逻辑
        """
        try:
            from clickhouse_driver import Client
        except ImportError:
            raise RuntimeError(
                "需要安装 ClickHouse driver: pip install clickhouse-driver"
            )

        # TODO: 根据你的需求修改表名和列
        features_table = kwargs.get("features_table", "trading.features_1m")
        timeframe = kwargs.get("timeframe", "1m")

        client = Client(**clickhouse_config)
        syms = ",".join([f"'{s}'" for s in symbols])

        # TODO: 修改 SQL 查询，添加你需要的列
        sql = f"""
        SELECT
            ts,
            symbol,
            open,
            high,
            low,
            close,
            volume
            -- TODO: 添加策略需要的其他列
            -- 例如: vwap, atr_14, ema_20, rsi_14 等
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

        # TODO: 修改列名列表，与 SQL 中的列对应
        columns = [
            "ts", "symbol", "open", "high", "low", "close", "volume"
            # TODO: 添加其他列名
        ]

        df = pd.DataFrame(data, columns=columns)

        return df

    def validate_data(self, df: pd.DataFrame) -> None:
        """
        验证数据是否满足策略要求

        Args:
            df: 待验证的 DataFrame

        Raises:
            ValueError: 数据不符合要求

        TODO: 实现数据验证逻辑
        """
        # TODO: 修改必需列列表
        required = [
            "ts", "symbol", "open", "high", "low", "close", "volume"
            # TODO: 添加策略必需的其他列
        ]

        missing = set(required) - set(df.columns)
        if missing:
            raise ValueError(
                f"策略缺少必需列: {missing}\n"
                f"当前列: {list(df.columns)}\n"
                f"请确保 ClickHouse 表包含所有必需列"
            )

        if df.empty:
            raise ValueError("数据为空，请检查时间范围和股票代码")

        # TODO: 可选 - 检查关键列是否有太多 NaN
        critical_cols = ["close", "volume"]  # TODO: 修改关键列
        for col in critical_cols:
            if col in df.columns:
                nan_pct = df[col].isna().mean()
                if nan_pct > 0.5:
                    print(
                        f"[{self.__class__.__name__}] 警告: 列 '{col}' 有 {nan_pct:.1%} 的缺失值，"
                        f"可能影响回测结果"
                    )

    def parse_params(
        self,
        grid_params: Dict[str, Any],
        base_extras: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        解析和合并参数

        根据你的策略参数结构选择以下模式之一：

        模式 1: 扁平参数（简单合并）
            grid_params = {"rsi_period": 14, "bb_std": 2.0}
            → {"extras": {"rsi_period": 14, "bb_std": 2.0}}

        模式 2: 嵌套参数（点分隔路径）
            grid_params = {"strategy_A.param1": 0.5}
            → {"extras": {"strategy_A": {"param1": 0.5}}}

        模式 3: 向后兼容（保持 strategy_config 包装器）
            grid_params = {"rsi_period": 14}
            → {"extras": {"strategy_config": {"rsi_period": 14}}}

        Args:
            grid_params: 从 grid 生成的参数组合
            base_extras: 基础配置 (strategy_base_extras)

        Returns:
            策略最终的 params 字典，格式: {"extras": {...}}

        TODO: 选择并实现适合你策略的参数解析模式
        """
        import copy
        result = copy.deepcopy(base_extras)

        # TODO: 选择以下 3 种模式之一

        # ===== 模式 1: 扁平参数（推荐用于简单策略）=====
        # result.update(grid_params)
        # return {"extras": result}

        # ===== 模式 2: 嵌套参数（推荐用于复杂策略）=====
        # for key, value in grid_params.items():
        #     if "." in key:
        #         # 解析 "strategy_A.param1" → strategy_A["param1"]
        #         parts = key.split(".")
        #         target = result
        #         for part in parts[:-1]:
        #             if part not in target:
        #                 target[part] = {}
        #             elif not isinstance(target[part], dict):
        #                 target[part] = {}
        #             target = target[part]
        #         target[parts[-1]] = value
        #     else:
        #         result[key] = value
        # return {"extras": result}

        # ===== 模式 3: 向后兼容（用于迁移旧策略）=====
        # if "strategy_config" not in result:
        #     result["strategy_config"] = {}
        # result["strategy_config"].update(grid_params)
        # return {"extras": result}

        # TODO: 取消注释上面的其中一种模式
        raise NotImplementedError("请选择并实现参数解析模式")

    def get_required_columns(self) -> List[str]:
        """
        返回策略必需的数据列

        Returns:
            列名列表

        TODO: 修改为你的策略需要的列
        """
        return [
            "ts", "symbol", "open", "high", "low", "close", "volume"
            # TODO: 添加策略必需的其他列
        ]

    def supports_ml_signals(self) -> bool:
        """
        策略是否支持 ML 信号增强

        - True: 框架会调用 _prepare_model_data() 生成 ml_signal, ml_conf 列
        - False: 纯规则策略，跳过 ML 流程

        Returns:
            是否支持 ML

        TODO: 根据你的策略修改
        """
        return False  # TODO: 如果策略需要 ML 增强，改为 True

    def get_default_params(self) -> Dict[str, Any]:
        """
        返回策略的默认参数

        用于文档和快速测试

        Returns:
            默认参数字典

        TODO: 添加你的策略的默认参数
        """
        return {
            # TODO: 添加默认参数
            # 例如:
            # "rsi_period": 14,
            # "bb_std": 2.0,
            # "atr_period": 14
        }

    def get_data_sources(self) -> List[str]:
        """
        返回策略需要的数据源列表

        用于文档和依赖检查

        Returns:
            数据源列表（表名或服务名）

        TODO: 修改为你的策略使用的数据源
        """
        return [
            "trading.features_1m"
            # TODO: 添加其他数据源
            # 例如: "trading.bars_5m_ibkr", "redis.sfs_1s"
        ]

    def get_strategy_description(self) -> str:
        """
        返回策略描述

        用于文档和日志

        Returns:
            策略描述字符串

        TODO: 修改为你的策略描述
        """
        return (
            "[策略名称] - [策略类型]\n"
            "TODO: 添加策略描述\n"
            "特点: TODO"
        )


# ============================================================================
# 单元测试（可选但推荐）
# ============================================================================

if __name__ == "__main__":
    print("=" * 60)
    print("TEMPLATE Adapter Test")
    print("=" * 60)

    adapter = TEMPLATEAdapter()

    # 测试 1: 参数解析
    print("\n测试 1: 参数解析")
    grid_params = {
        # TODO: 添加测试参数
        "test_param": 1.0
    }
    base_extras = {
        # TODO: 添加基础配置
        "base_config": {"key": "value"}
    }
    try:
        result = adapter.parse_params(grid_params, base_extras)
        print(f"Grid params: {grid_params}")
        print(f"Result: {result}")
        print("✅ 参数解析测试通过")
    except NotImplementedError:
        print("⚠️  参数解析未实现 (请实现 parse_params 方法)")

    # 测试 2: 必需列
    print("\n测试 2: 必需列")
    required = adapter.get_required_columns()
    print(f"必需列: {required}")
    print("✅ 必需列测试通过")

    # 测试 3: ML 支持
    print("\n测试 3: ML 支持")
    ml_support = adapter.supports_ml_signals()
    print(f"支持 ML: {ml_support}")
    print("✅ ML 支持测试通过")

    # 测试 4: 默认参数
    print("\n测试 4: 默认参数")
    defaults = adapter.get_default_params()
    print(f"默认参数: {defaults}")
    print("✅ 默认参数测试通过")

    # 测试 5: 元信息
    print("\n测试 5: 适配器元信息")
    print(f"数据源: {adapter.get_data_sources()}")
    print(f"描述:\n{adapter.get_strategy_description()}")
    print("✅ 元信息测试通过")

    print("\n" + "=" * 60)
    print("📝 TODO 清单:")
    print("  [ ] 实现 load_data() 方法")
    print("  [ ] 实现 validate_data() 方法")
    print("  [ ] 实现 parse_params() 方法（选择参数解析模式）")
    print("  [ ] 更新 get_required_columns()")
    print("  [ ] 更新 supports_ml_signals()")
    print("  [ ] 更新 get_default_params()")
    print("  [ ] 更新 get_data_sources()")
    print("  [ ] 更新 get_strategy_description()")
    print("  [ ] 在 strategy_registry.py 中注册适配器")
    print("  [ ] 创建配置文件")
    print("  [ ] 运行测试")
    print("=" * 60)
