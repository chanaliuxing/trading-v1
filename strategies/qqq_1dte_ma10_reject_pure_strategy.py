from __future__ import annotations

from libs.strategy.registry import register_strategy
from strategies.options_kernel.base import OptionKernelStrategy
from strategies.options_plugins.qqq_1dte_ma10_reject import QQQ1DTEMa10RejectPlugin


@register_strategy("qqq_1dte_ma10_reject_live_pure")
class QQQ1DTEMA10RejectPureStrategy(OptionKernelStrategy):
    """
    Pure-plugin terminal-state entrypoint for the QQQ 1DTE MA strategy.

    This stays disabled by default and exists for shadow/parity migration only.
    """

    plugin_cls = QQQ1DTEMa10RejectPlugin
