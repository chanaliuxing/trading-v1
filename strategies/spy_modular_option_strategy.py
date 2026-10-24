from __future__ import annotations

from strategies.options_universal import UniversalOptionLiveStrategy
from strategies.options_universal.template_plugin import TemplateOptionPlugin


class SPYModularOptionStrategy(UniversalOptionLiveStrategy):
    """
    Example modular strategy entrypoint.

    This class uses template plugin and shared kernel;
    replace plugin implementation to ship real signal logic.
    """

    def build_plugin(self):
        return TemplateOptionPlugin()
