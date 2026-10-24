from __future__ import annotations

from .agent_paper_bridge import AgentPaperBridgeStockPlugin
from .example_breakout import ExampleBreakoutStockPlugin
from .nvda_minute_flip import NVDAMinuteFlipStockPlugin
from .qqq_intraday import QQQIntradayStockPlugin

__all__ = [
    "AgentPaperBridgeStockPlugin",
    "ExampleBreakoutStockPlugin",
    "NVDAMinuteFlipStockPlugin",
    "QQQIntradayStockPlugin",
]
