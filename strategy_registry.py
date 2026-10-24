# -*- coding: utf-8 -*-
"""
Strategy Registry
-----------------
策略适配器注册表，用于管理所有可用的策略适配器

Usage:
    # 注册适配器
    from strategy_registry import StrategyRegistry
    from adapters.my_adapter import MyAdapter

    StrategyRegistry.register("strategies.my_strategy.MyStrategy", MyAdapter)

    # 获取适配器
    adapter = StrategyRegistry.get_adapter("strategies.my_strategy.MyStrategy")

    # 列出所有策略
    strategies = StrategyRegistry.list_strategies()

Author: Trading Team
Date: 2025-10-26
"""

from __future__ import annotations
from typing import Dict, Type, List
from strategy_adapter import StrategyAdapter


class StrategyRegistry:
    """
    策略适配器注册表

    使用单例模式管理所有策略适配器的注册和获取
    """

    _adapters: Dict[str, Type[StrategyAdapter]] = {}

    @classmethod
    def register(
        cls,
        strategy_path: str,
        adapter_class: Type[StrategyAdapter],
        overwrite: bool = False
    ) -> None:
        """
        注册策略适配器

        Args:
            strategy_path: 策略的模块路径，如 "strategies.tsla_v7_2.TSLASimplifiedV7_2"
            adapter_class: 适配器类（未实例化）
            overwrite: 是否允许覆盖已存在的注册，默认 False

        Raises:
            ValueError: 策略已注册且 overwrite=False
            TypeError: adapter_class 不是 StrategyAdapter 的子类

        Example:
            >>> from adapters.tsla_adapter import TSLAAdapter
            >>> StrategyRegistry.register("strategies.tsla_v7_2.TSLASimplifiedV7_2", TSLAAdapter)
        """
        # 类型检查
        if not issubclass(adapter_class, StrategyAdapter):
            raise TypeError(
                f"adapter_class 必须是 StrategyAdapter 的子类，"
                f"但收到: {adapter_class}"
            )

        # 重复检查
        if strategy_path in cls._adapters and not overwrite:
            existing = cls._adapters[strategy_path]
            raise ValueError(
                f"策略 '{strategy_path}' 已注册 (适配器: {existing})。"
                f"使用 overwrite=True 覆盖"
            )

        cls._adapters[strategy_path] = adapter_class
        print(f"[StrategyRegistry] 已注册策略: {strategy_path} → {adapter_class.__name__}")

    @classmethod
    def get_adapter(cls, strategy_path: str) -> StrategyAdapter:
        """
        获取策略适配器实例

        Args:
            strategy_path: 策略的模块路径

        Returns:
            适配器实例

        Raises:
            ValueError: 未找到策略的适配器

        Example:
            >>> adapter = StrategyRegistry.get_adapter("strategies.tsla_v7_2.TSLASimplifiedV7_2")
            >>> print(type(adapter))
            <class 'adapters.tsla_adapter.TSLAAdapter'>
        """
        if strategy_path not in cls._adapters:
            available = cls.list_strategies()
            raise ValueError(
                f"未找到策略 '{strategy_path}' 的适配器。\n"
                f"已注册的策略 ({len(available)}个):\n" +
                "\n".join(f"  - {s}" for s in available)
            )

        adapter_class = cls._adapters[strategy_path]
        return adapter_class()

    @classmethod
    def list_strategies(cls) -> List[str]:
        """
        列出所有已注册的策略

        Returns:
            策略路径列表

        Example:
            >>> strategies = StrategyRegistry.list_strategies()
            >>> for s in strategies:
            ...     print(s)
            strategies.tsla_v7_2.TSLASimplifiedV7_2
            strategies.reve_intraday_strategy.RevEIntradayStrategyV2
        """
        return sorted(cls._adapters.keys())

    @classmethod
    def is_registered(cls, strategy_path: str) -> bool:
        """
        检查策略是否已注册

        Args:
            strategy_path: 策略的模块路径

        Returns:
            True 如果已注册

        Example:
            >>> StrategyRegistry.is_registered("strategies.tsla_v7_2.TSLASimplifiedV7_2")
            True
        """
        return strategy_path in cls._adapters

    @classmethod
    def unregister(cls, strategy_path: str) -> bool:
        """
        取消注册策略

        Args:
            strategy_path: 策略的模块路径

        Returns:
            True 如果成功取消注册，False 如果策略不存在

        Example:
            >>> StrategyRegistry.unregister("strategies.old_strategy.OldStrategy")
            True
        """
        if strategy_path in cls._adapters:
            del cls._adapters[strategy_path]
            print(f"[StrategyRegistry] 已取消注册策略: {strategy_path}")
            return True
        return False

    @classmethod
    def clear(cls) -> None:
        """
        清空所有注册（主要用于测试）

        Example:
            >>> StrategyRegistry.clear()
            >>> StrategyRegistry.list_strategies()
            []
        """
        cls._adapters.clear()
        print("[StrategyRegistry] 已清空所有策略注册")

    @classmethod
    def get_adapter_info(cls, strategy_path: str) -> Dict[str, any]:
        """
        获取适配器的详细信息

        Args:
            strategy_path: 策略的模块路径

        Returns:
            适配器信息字典

        Example:
            >>> info = StrategyRegistry.get_adapter_info("strategies.tsla_v7_2.TSLASimplifiedV7_2")
            >>> print(info)
            {
                'adapter_class': 'TSLAAdapter',
                'description': 'TSLA V7_2 strategy adapter',
                'required_columns': ['ts', 'symbol', 'open', ...],
                'supports_ml': True,
                'data_sources': ['trading.features_1m']
            }
        """
        adapter = cls.get_adapter(strategy_path)

        return {
            "strategy_path": strategy_path,
            "adapter_class": adapter.__class__.__name__,
            "description": adapter.get_strategy_description(),
            "required_columns": adapter.get_required_columns(),
            "supports_ml": adapter.supports_ml_signals(),
            "data_sources": adapter.get_data_sources(),
            "default_params": adapter.get_default_params()
        }


# ============================================================================
# 自动注册内置适配器
# ============================================================================

def _register_builtin_adapters():
    """
    注册内置的策略适配器

    在模块加载时自动执行
    """
    try:
        # 尝试加载 DefaultAdapter
        from strategy_adapter import DefaultAdapter
        StrategyRegistry.register("default", DefaultAdapter)

    except ImportError as e:
        print(f"[StrategyRegistry] 警告: 无法加载默认适配器: {e}")

    # 尝试加载其他内置适配器
    # 注意: 这里使用 try-except 避免在适配器未实现时导致模块加载失败

    try:
        # TSLA V7_2 适配器
        from adapters.tsla_v7_2_adapter import TSLAV72Adapter
        StrategyRegistry.register(
            "strategies.tsla_v7_2.TSLASimplifiedV7_2",
            TSLAV72Adapter
        )
    except ImportError:
        pass  # 适配器未实现，跳过

    try:
        # RevE 适配器
        from adapters.reve_adapter import RevEAdapter
        StrategyRegistry.register(
            "strategies.reve_intraday_strategy.RevEIntradayStrategyV2",
            RevEAdapter
        )
    except ImportError:
        pass  # 适配器未实现，跳过

    try:
        # EnhancedModelSignalStrategy 适配器
        from adapters.enhanced_model_adapter import EnhancedModelAdapter
        StrategyRegistry.register(
            "strategies.enhanced_model_signal_strategy.EnhancedModelSignalStrategy",
            EnhancedModelAdapter
        )
    except ImportError:
        pass


# 模块加载时自动注册
_register_builtin_adapters()


if __name__ == "__main__":
    # 测试代码
    print("=" * 60)
    print("Strategy Registry Test")
    print("=" * 60)

    print(f"\n已注册的策略 ({len(StrategyRegistry.list_strategies())} 个):")
    for strategy in StrategyRegistry.list_strategies():
        print(f"  - {strategy}")

    # 测试获取适配器
    if StrategyRegistry.list_strategies():
        first_strategy = StrategyRegistry.list_strategies()[0]
        print(f"\n测试获取适配器: {first_strategy}")
        adapter = StrategyRegistry.get_adapter(first_strategy)
        print(f"  适配器类型: {type(adapter)}")
        print(f"  必需列: {adapter.get_required_columns()}")
        print(f"  支持 ML: {adapter.supports_ml_signals()}")

    # 测试错误处理
    print("\n测试错误处理:")
    try:
        StrategyRegistry.get_adapter("strategies.nonexistent.Strategy")
    except ValueError as e:
        print(f"  预期错误: {e}")

    print("\n" + "=" * 60)
