from __future__ import annotations

from libs.strategy.registry import register_strategy
from strategies.options_kernel.base import OptionKernelStrategy
from strategies.options_plugins.qqq_1dte_ma10_reject import QQQ1DTEMa10RejectPlugin


@register_strategy("qqq_1dte_ma10_reject_live_v2")
class QQQ1DTEMA10RejectV2Strategy(OptionKernelStrategy):
    """
    V2 migration entrypoint for the QQQ 1DTE MA strategy.

    Safety properties:
    - New kernel/plugin path only
    - Legacy strategy class remains untouched
    - Disabled by default in config
    - Uses a proxy context so kernel can attach ownership/parity metadata
      without changing shared execution defaults for other strategies
    """

    plugin_cls = QQQ1DTEMa10RejectPlugin
