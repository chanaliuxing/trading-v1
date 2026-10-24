from __future__ import annotations

from libs.strategy.registry import register_strategy
from strategies.options_kernel.base import OptionKernelStrategy
from strategies.options_plugins.qqq_0dte_opt_live import QQQ0DTEOptionsLivePlugin


@register_strategy("qqq_0dte_opt_live_v2")
class QQQ0DTEOptionsLiveV2Strategy(OptionKernelStrategy):
    """
    Deprecated migration entrypoint for the QQQ 0DTE options strategy.

    Kept only for rollback/reference. New development and live enablement should
    use the pure-plugin entrypoint instead.
    """

    plugin_cls = QQQ0DTEOptionsLivePlugin

