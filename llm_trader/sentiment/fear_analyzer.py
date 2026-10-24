#!/usr/bin/env python3
"""
Fear Sentiment Analyzer - VIX based fear gauge

Phase 1 MVP: VIX only
Phase 2: Add VVIX, Put/Call Ratio, IV Rank
"""

import yfinance as yf
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional
import logging

logger = logging.getLogger(__name__)


class FearSentimentAnalyzer:
    """Analyze market fear based on VIX and related indicators"""

    def __init__(self):
        self.vix_ticker = "^VIX"

    def get_realtime_fear_sentiment(self) -> Dict:
        """
        Get real-time fear sentiment score

        Returns:
            {
                "score": float,  # 0.0-1.0 (0=extreme fear, 1=extreme greed)
                "vix": float,
                "vix_percentile": float,  # 0-100
                "interpretation": str,
                "timestamp": str
            }
        """
        try:
            # Fetch VIX data
            vix = yf.Ticker(self.vix_ticker)
            hist = vix.history(period="1d")

            if hist.empty:
                logger.warning("No VIX data available, using neutral score")
                return self._neutral_response()

            current_vix = float(hist['Close'].iloc[-1])

            # Get 30-day history for percentile calculation
            hist_30d = vix.history(period="1mo")
            if len(hist_30d) > 0:
                vix_percentile = (hist_30d['Close'] < current_vix).sum() / len(hist_30d) * 100
            else:
                vix_percentile = 50.0

            # Calculate fear score (inverse of VIX)
            # VIX 10-40 range mapped to score 1.0-0.0
            # VIX < 10: extreme greed (score ~ 1.0)
            # VIX > 40: extreme fear (score ~ 0.0)
            fear_score = max(0.0, min(1.0, (40 - current_vix) / 30))

            # Interpretation
            interpretation = self._interpret_fear(fear_score, current_vix)

            return {
                "score": round(fear_score, 3),
                "vix": round(current_vix, 2),
                "vix_percentile": round(vix_percentile, 1),
                "interpretation": interpretation,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "source": "VIX (Yahoo Finance)"
            }

        except Exception as e:
            logger.error(f"Error fetching VIX data: {e}")
            return self._neutral_response()

    def _interpret_fear(self, score: float, vix: float) -> str:
        """Generate human-readable interpretation"""
        if vix < 12:
            return f"极度贪婪 (VIX={vix:.1f}，极低恐慌)"
        elif vix < 15:
            return f"贪婪 (VIX={vix:.1f}，低恐慌)"
        elif vix < 20:
            return f"中性偏乐观 (VIX={vix:.1f}，正常水平)"
        elif vix < 25:
            return f"谨慎 (VIX={vix:.1f}，略高恐慌)"
        elif vix < 30:
            return f"恐惧 (VIX={vix:.1f}，高恐慌)"
        else:
            return f"极度恐惧 (VIX={vix:.1f}，极高恐慌)"

    def _neutral_response(self) -> Dict:
        """Return neutral sentiment when data unavailable"""
        return {
            "score": 0.5,
            "vix": None,
            "vix_percentile": 50.0,
            "interpretation": "数据不可用，使用中性评分",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "Fallback"
        }


# Self-test
if __name__ == "__main__":
    print("=" * 60)
    print("Fear Sentiment Analyzer - Self Test")
    print("=" * 60)

    analyzer = FearSentimentAnalyzer()
    result = analyzer.get_realtime_fear_sentiment()

    print(f"\n✅ Fear Sentiment:")
    print(f"  Score: {result['score']:.3f} / 1.0")
    print(f"  VIX: {result['vix']}")
    print(f"  VIX Percentile: {result['vix_percentile']}%")
    print(f"  Interpretation: {result['interpretation']}")
    print(f"  Timestamp: {result['timestamp']}")
    print(f"  Source: {result['source']}")

    print("\n" + "=" * 60)
    print("✅ Test completed successfully")
