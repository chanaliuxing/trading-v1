from __future__ import annotations

from libs.strategy.registry import register_strategy

from strategies.stocks_kernel.strategy import ComposableStocksKernelStrategy


@register_strategy("stocks_kernel")
class StocksKernelStrategy(ComposableStocksKernelStrategy):
    pass
