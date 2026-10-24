from __future__ import annotations

from libs.strategy.registry import register_strategy
from strategies.options_kernel.base import OptionKernelStrategy
from strategies.options_plugins.qqq_0dte_opt_live import QQQ0DTEOptionsLivePlugin


@register_strategy("qqq_0dte_opt_live_pure")
class QQQ0DTEOptionsLivePureStrategy(OptionKernelStrategy):
    """
    Pure-plugin terminal-state entrypoint for the QQQ 0DTE options strategy.
    """

    plugin_cls = QQQ0DTEOptionsLivePlugin
