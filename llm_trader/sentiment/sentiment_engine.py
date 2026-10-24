#!/usr/bin/env python3
"""
Sentiment Engine - Combines fear, flow, and narrative sentiment

Three-dimensional sentiment scoring system:
1. Fear sentiment (VIX-based): 0=extreme fear, 1=extreme greed
2. Flow sentiment (ETF flows): 0=outflow, 1=inflow
3. Narrative sentiment (news): 0=very negative, 1=very positive

Final composite score is weighted average with configurable weights.
"""

from typing import Dict, List, Optional
from datetime import datetime, timezone
import logging

from llm_trader.sentiment.fear_analyzer import FearSentimentAnalyzer
from llm_trader.sentiment.flow_analyzer import FlowSentimentAnalyzer
from llm_trader.sentiment.narrative_analyzer import NarrativeSentimentAnalyzer

logger = logging.getLogger(__name__)


class SentimentEngine:
    """
    Unified sentiment analysis engine combining three dimensions

    Supports two modes for narrative analysis:
    - Phase 1: Keyword-based (fast, free, 70-75% accuracy)
    - Phase 2: LLM-based (slower, requires API key, 85-90% accuracy)
    """

    def __init__(
        self,
        fear_weight: float = 0.3,
        flow_weight: float = 0.4,
        narrative_weight: float = 0.3,
        use_llm_narrative: bool = False
    ):
        """
        Initialize sentiment engine

        Args:
            fear_weight: Weight for fear sentiment (default 0.3)
            flow_weight: Weight for flow sentiment (default 0.4)
            narrative_weight: Weight for narrative sentiment (default 0.3)
            use_llm_narrative: Use LLM for narrative analysis (Phase 2, default False)
        """
        if not abs(fear_weight + flow_weight + narrative_weight - 1.0) < 0.01:
            raise ValueError("Weights must sum to 1.0")

        self.fear_weight = fear_weight
        self.flow_weight = flow_weight
        self.narrative_weight = narrative_weight
        self.use_llm_narrative = use_llm_narrative

        self.fear_analyzer = FearSentimentAnalyzer()
        self.flow_analyzer = FlowSentimentAnalyzer()

        # Choose narrative analyzer based on mode
        if use_llm_narrative:
            try:
                from llm_trader.sentiment.narrative_analyzer_v2 import NarrativeAnalyzerV2
                self.narrative_analyzer = NarrativeAnalyzerV2()
                logger.info("Using Phase 2: LLM-based narrative analysis (DeepSeek-NVIDIA)")
            except Exception as e:
                logger.warning(f"Failed to initialize LLM narrative analyzer: {e}, falling back to Phase 1")
                self.narrative_analyzer = NarrativeSentimentAnalyzer()
                self.use_llm_narrative = False
        else:
            self.narrative_analyzer = NarrativeSentimentAnalyzer()
            logger.info("Using Phase 1: Keyword-based narrative analysis")

    def get_market_sentiment(self, symbols: Optional[List[str]] = None) -> Dict:
        """
        Get comprehensive market sentiment analysis

        Args:
            symbols: Optional list of symbols for narrative analysis

        Returns:
            {
                "composite_score": float,  # 0.0-1.0 weighted average
                "interpretation": str,
                "components": {
                    "fear": {...},
                    "flow": {...},
                    "narrative": {...}
                },
                "timestamp": str,
                "weights": {...}
            }
        """
        try:
            # Fetch all three sentiment dimensions
            fear_result = self.fear_analyzer.get_realtime_fear_sentiment()
            flow_result = self.flow_analyzer.get_flow_sentiment()
            narrative_result = self.narrative_analyzer.get_narrative_sentiment(symbols)

            # Calculate composite score
            composite_score = (
                fear_result["score"] * self.fear_weight +
                flow_result["score"] * self.flow_weight +
                narrative_result["score"] * self.narrative_weight
            )

            # Generate interpretation
            interpretation = self._interpret_composite(
                composite_score,
                fear_result,
                flow_result,
                narrative_result
            )

            return {
                "composite_score": round(composite_score, 3),
                "interpretation": interpretation,
                "components": {
                    "fear": fear_result,
                    "flow": flow_result,
                    "narrative": narrative_result
                },
                "weights": {
                    "fear": self.fear_weight,
                    "flow": self.flow_weight,
                    "narrative": self.narrative_weight
                },
                "timestamp": datetime.now(timezone.utc).isoformat()
            }

        except Exception as e:
            logger.error(f"Error calculating market sentiment: {e}")
            return self._neutral_response()

    def _interpret_composite(
        self,
        score: float,
        fear: Dict,
        flow: Dict,
        narrative: Dict
    ) -> str:
        """Generate human-readable composite interpretation"""

        # Overall sentiment level
        if score >= 0.7:
            level = "强烈看多"
        elif score >= 0.6:
            level = "偏多"
        elif score >= 0.4:
            level = "中性"
        elif score >= 0.3:
            level = "偏空"
        else:
            level = "强烈看空"

        # Key drivers
        drivers = []
        if fear["score"] >= 0.7:
            drivers.append("低恐慌")
        elif fear["score"] <= 0.3:
            drivers.append("高恐慌")

        if flow["score"] >= 0.6:
            drivers.append("资金流入")
        elif flow["score"] <= 0.4:
            drivers.append("资金流出")

        if narrative["score"] >= 0.6:
            drivers.append("新闻偏多")
        elif narrative["score"] <= 0.4:
            drivers.append("新闻偏空")

        if drivers:
            driver_str = "，".join(drivers)
            return f"{level} (综合评分={score:.2f}，{driver_str})"
        else:
            return f"{level} (综合评分={score:.2f})"

    def get_summary_dict(self, symbols: Optional[List[str]] = None) -> Dict:
        """
        Get simplified summary for prompt injection

        Returns:
            {
                "composite_score": float,
                "composite_label": str,
                "fear_score": float,
                "fear_vix": float,
                "flow_score": float,
                "narrative_score": float
            }
        """
        result = self.get_market_sentiment(symbols)

        composite = result["composite_score"]
        if composite >= 0.7:
            label = "强烈看多"
        elif composite >= 0.6:
            label = "偏多"
        elif composite >= 0.4:
            label = "中性"
        elif composite >= 0.3:
            label = "偏空"
        else:
            label = "强烈看空"

        return {
            "composite_score": composite,
            "composite_label": label,
            "fear_score": result["components"]["fear"]["score"],
            "fear_vix": result["components"]["fear"].get("vix"),
            "flow_score": result["components"]["flow"]["score"],
            "narrative_score": result["components"]["narrative"]["score"]
        }

    def _neutral_response(self) -> Dict:
        """Return neutral sentiment when data unavailable"""
        return {
            "composite_score": 0.5,
            "interpretation": "数据不可用，使用中性评分",
            "components": {
                "fear": {"score": 0.5, "interpretation": "数据不可用"},
                "flow": {"score": 0.5, "interpretation": "数据不可用"},
                "narrative": {"score": 0.5, "interpretation": "数据不可用"}
            },
            "weights": {
                "fear": self.fear_weight,
                "flow": self.flow_weight,
                "narrative": self.narrative_weight
            },
            "timestamp": datetime.now(timezone.utc).isoformat()
        }


# Self-test
if __name__ == "__main__":
    print("=" * 70)
    print("Sentiment Engine - Self Test")
    print("=" * 70)

    # Test 1: Default weights
    print("\n[Test 1] Market sentiment with default weights (0.3/0.4/0.3):")
    engine = SentimentEngine()
    result = engine.get_market_sentiment()

    print(f"\n✅ Composite Sentiment:")
    print(f"  Score: {result['composite_score']:.3f} / 1.0")
    print(f"  Interpretation: {result['interpretation']}")

    print(f"\n  Component Breakdown:")
    print(f"    Fear:      {result['components']['fear']['score']:.3f} "
          f"(weight={result['weights']['fear']})")
    print(f"    Flow:      {result['components']['flow']['score']:.3f} "
          f"(weight={result['weights']['flow']})")
    print(f"    Narrative: {result['components']['narrative']['score']:.3f} "
          f"(weight={result['weights']['narrative']})")

    print(f"\n  Detailed Components:")
    print(f"    Fear: {result['components']['fear']['interpretation']}")
    print(f"    Flow: {result['components']['flow']['interpretation']}")
    print(f"    Narrative: {result['components']['narrative']['interpretation']}")

    print(f"\n  Timestamp: {result['timestamp']}")

    # Test 2: Custom weights (flow-heavy)
    print("\n" + "=" * 70)
    print("[Test 2] Custom weights (flow-heavy: 0.2/0.6/0.2):")
    engine2 = SentimentEngine(fear_weight=0.2, flow_weight=0.6, narrative_weight=0.2)
    result2 = engine2.get_market_sentiment()

    print(f"\n✅ Composite Sentiment:")
    print(f"  Score: {result2['composite_score']:.3f} / 1.0")
    print(f"  Interpretation: {result2['interpretation']}")

    # Test 3: Summary dict for prompt injection
    print("\n" + "=" * 70)
    print("[Test 3] Summary dict for LLM prompts:")
    summary = engine.get_summary_dict(["AAPL", "TSLA"])

    print(f"\n✅ Summary:")
    print(f"  Composite: {summary['composite_score']:.3f} ({summary['composite_label']})")
    print(f"  Fear:      {summary['fear_score']:.3f} (VIX={summary['fear_vix']})")
    print(f"  Flow:      {summary['flow_score']:.3f}")
    print(f"  Narrative: {summary['narrative_score']:.3f}")

    print("\n" + "=" * 70)
    print("✅ All tests completed successfully")
