from __future__ import annotations

from libs.strategy.registry import register_strategy
from strategies.options_kernel.base import OptionKernelStrategy
from strategies.options_plugins.spy_intraday_momentum_option import SPYIntradayMomentumOptionPlugin


@register_strategy("spy_intraday_momentum_option_paper_v1")
class SPYIntradayMomentumOptionPaperStrategy(OptionKernelStrategy):
    plugin_cls = SPYIntradayMomentumOptionPlugin
