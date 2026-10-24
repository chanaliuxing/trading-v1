#!/usr/bin/env python3
"""
Flow Sentiment Analyzer - ETF money flow analysis

Phase 1 MVP: SPY/QQQ simple volume analysis
Phase 2: Add actual fund flow data, smart money index
"""

import yfinance as yf
from datetime import datetime, timezone, timedelta
from typing import Dict, List
import logging
import numpy as np

logger = logging.getLogger(__name__)


class FlowSentimentAnalyzer:
    """Analyze market money flow based on ETF activity"""

    def __init__(self):
        self.etf_symbols = ["SPY", "QQQ", "IWM"]  # Large cap, Tech, Small cap

    def get_flow_sentiment(self, symbol: str = None) -> Dict:
        """
        Get money flow sentiment

        Args:
            symbol: Optional specific symbol to analyze

        Returns:
            {
                "score": float,  # 0.0-1.0 (0=outflow, 1=inflow)
                "etf_flows": dict,
                "interpretation": str,
                "timestamp": str
            }
        """
        try:
            # Fetch ETF data
            etf_data = {}
            for etf in self.etf_symbols:
                ticker = yf.Ticker(etf)
                hist = ticker.history(period="5d")

                if len(hist) > 0:
                    # Calculate volume trend (5d vs 20d average)
                    hist_long = ticker.history(period="1mo")
                    if len(hist_long) > 10:
                        recent_vol = hist['Volume'].tail(5).mean()
                        avg_vol = hist_long['Volume'].mean()
                        vol_ratio = recent_vol / avg_vol if avg_vol > 0 else 1.0
                    else:
                        vol_ratio = 1.0

                    # Price momentum (5d)
                    price_change = (hist['Close'].iloc[-1] / hist['Close'].iloc[0] - 1) * 100

                    etf_data[etf] = {
                        "volume_ratio": round(vol_ratio, 2),
                        "price_change_5d": round(price_change, 2),
                        "signal": "流入" if vol_ratio > 1.1 and price_change > 0 else
                                  "流出" if vol_ratio < 0.9 or price_change < -1 else
                                  "中性"
                    }

            # Calculate overall flow score
            if etf_data:
                # Average volume ratio across ETFs
                avg_vol_ratio = np.mean([d['volume_ratio'] for d in etf_data.values()])
                # Average price change
                avg_price_change = np.mean([d['price_change_5d'] for d in etf_data.values()])

                # Score calculation
                # vol_ratio > 1.2 and price up -> high score
                # vol_ratio < 0.8 or price down -> low score
                vol_score = max(0, min(1, (avg_vol_ratio - 0.8) / 0.6))  # 0.8-1.4 -> 0-1
                price_score = max(0, min(1, (avg_price_change + 2) / 4))  # -2% to +2% -> 0-1

                flow_score = (vol_score * 0.6 + price_score * 0.4)
            else:
                flow_score = 0.5

            interpretation = self._interpret_flow(flow_score, etf_data)

            return {
                "score": round(flow_score, 3),
                "etf_flows": etf_data,
                "avg_volume_ratio": round(avg_vol_ratio, 2) if etf_data else None,
                "avg_price_change": round(avg_price_change, 2) if etf_data else None,
                "interpretation": interpretation,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "source": "ETF Volume/Price Analysis (Yahoo Finance)"
            }

        except Exception as e:
            logger.error(f"Error analyzing flow sentiment: {e}")
            return self._neutral_response()

    def _interpret_flow(self, score: float, etf_data: Dict) -> str:
        """Generate human-readable interpretation"""
        if score >= 0.7:
            return f"强劲流入 (评分={score:.2f}，多ETF高成交+上涨)"
        elif score >= 0.6:
            return f"温和流入 (评分={score:.2f}，整体偏多)"
        elif score >= 0.4:
            return f"中性 (评分={score:.2f}，多空平衡)"
        elif score >= 0.3:
            return f"温和流出 (评分={score:.2f}，整体偏空)"
        else:
            return f"强劲流出 (评分={score:.2f}，成交萎缩+下跌)"

    def _neutral_response(self) -> Dict:
        """Return neutral sentiment when data unavailable"""
        return {
            "score": 0.5,
            "etf_flows": {},
            "avg_volume_ratio": None,
            "avg_price_change": None,
            "interpretation": "数据不可用，使用中性评分",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "Fallback"
        }


# Self-test
if __name__ == "__main__":
    print("=" * 60)
    print("Flow Sentiment Analyzer - Self Test")
    print("=" * 60)

    analyzer = FlowSentimentAnalyzer()
    result = analyzer.get_flow_sentiment()

    print(f"\n✅ Flow Sentiment:")
    print(f"  Score: {result['score']:.3f} / 1.0")
    print(f"  Avg Volume Ratio: {result['avg_volume_ratio']}")
    print(f"  Avg Price Change (5d): {result['avg_price_change']}%")
    print(f"  Interpretation: {result['interpretation']}")

    print(f"\n  ETF Breakdown:")
    for etf, data in result['etf_flows'].items():
        print(f"    {etf}: Vol Ratio={data['volume_ratio']}, "
              f"Price Δ={data['price_change_5d']}%, "
              f"Signal={data['signal']}")

    print(f"\n  Timestamp: {result['timestamp']}")
    print(f"  Source: {result['source']}")

    print("\n" + "=" * 60)
    print("✅ Test completed successfully")
