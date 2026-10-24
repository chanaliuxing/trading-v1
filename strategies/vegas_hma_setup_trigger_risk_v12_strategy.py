from __future__ import annotations

from strategies.options_kernel.base import OptionKernelStrategy
from strategies.options_plugins.vegas_hma_setup_trigger_risk_v12 import VegasHMASetupTriggerRiskV12Plugin


class VegasHMASetupTriggerRiskV12Strategy(OptionKernelStrategy):
    """OptionKernel wrapper for the Vegas HMA setup/trigger/risk V12 plugin."""

    plugin_cls = VegasHMASetupTriggerRiskV12Plugin
