from __future__ import annotations

from libs.strategy.registry import register_strategy
from strategies.options_kernel.base import OptionKernelStrategy
from strategies.options_plugins.spy_1dte_ma10_reject import SPY1DTEMa10RejectPlugin


@register_strategy("spy_1dte_ma10_reject_live_pure")
class SPY1DTEMA10RejectPureStrategy(OptionKernelStrategy):
    """
    Pure-plugin terminal-state entrypoint for the SPY 1DTE MA strategy.
    """

    plugin_cls = SPY1DTEMa10RejectPlugin
