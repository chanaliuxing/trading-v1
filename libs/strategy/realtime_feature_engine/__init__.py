# __init__.py
"""
RealtimeFeatureEngine - 实时特征引擎

融合 Claude + Codex + Gemini 方案的最终版本:
- 多周期严格时间桶聚合 (ChatGPT方案)
- 插件化指标注册 (Codex方案)
- O(1)增量计算 (Claude方案)
- 异步数据流接口 (Gemini方案)

Author: Trading System Team
Version: 1.0.0
"""

from .realtime_feature_engine import RealtimeFeatureEngine
from .feature_registry import FeatureRegistry
from .indicator_registry import IndicatorRegistry
from .mtf_aggregator import MTFAggregator

__all__ = [
    "RealtimeFeatureEngine",
    "FeatureRegistry",
    "IndicatorRegistry",
    "MTFAggregator",
]

__version__ = "1.0.0"
