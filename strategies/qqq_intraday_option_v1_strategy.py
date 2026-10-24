from __future__ import annotations

from libs.strategy.registry import register_strategy
from strategies.options_kernel.base import OptionKernelStrategy
from strategies.options_plugins.qqq_intraday_option import QQQIntradayOptionPlugin


@register_strategy("qqq_intraday_option_live_v1")
class QQQIntradayOptionV1Strategy(OptionKernelStrategy):
    plugin_cls = QQQIntradayOptionPlugin
