#!/usr/bin/env python3
"""
Narrative Sentiment Analyzer - News & Social sentiment

Phase 1 MVP: Use yfinance news headlines + simple keyword analysis
Phase 2: Add Reddit WallStreetBets, Twitter, earnings transcripts
Phase 3: Use LLM for semantic analysis
"""

import yfinance as yf
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional
import logging
from dateutil.parser import parse as parse_datetime

logger = logging.getLogger(__name__)


class NarrativeSentimentAnalyzer:
    """Analyze market narrative based on news and social sentiment"""

    def __init__(self):
        # Positive and negative keywords for simple sentiment scoring
        self.positive_keywords = {
            "beat", "upgrade", "bullish", "surge", "rally", "gain", "strong",
            "growth", "profit", "positive", "outperform", "buy", "record",
            "breakthrough", "innovation", "expansion", "optimistic"
        }
        self.negative_keywords = {
            "miss", "downgrade", "bearish", "fall", "drop", "weak", "loss",
            "decline", "negative", "underperform", "sell", "concern", "risk",
            "warning", "cut", "layoff", "recession", "crisis"
        }

    def get_narrative_sentiment(self, symbols: List[str] = None) -> Dict:
        """
        Get narrative sentiment for market or specific symbols

        Args:
            symbols: Optional list of symbols to analyze. If None, uses market indices.

        Returns:
            {
                "score": float,  # 0.0-1.0 (0=very negative, 1=very positive)
                "symbol_sentiments": dict,
                "interpretation": str,
                "headline_count": int,
                "timestamp": str
            }
        """
        if symbols is None:
            symbols = ["SPY", "QQQ"]  # Default to market indices

        try:
            symbol_sentiments = {}
            total_headlines = 0
            total_score = 0.0

            for symbol in symbols:
                ticker = yf.Ticker(symbol)

                # Get news headlines
                try:
                    news = ticker.news
                    if not news:
                        continue

                    # Analyze recent headlines (last 24h)
                    # Note: yfinance returns nested structure: item['content']['title']
                    recent_news = []
                    for item in news:
                        content = item.get("content", {})
                        pub_date = content.get("pubDate")

                        if pub_date:
                            try:
                                # Parse ISO datetime string to timestamp
                                dt = parse_datetime(pub_date)
                                timestamp = int(dt.timestamp())

                                if self._is_recent(timestamp):
                                    recent_news.append(item)
                            except Exception as e:
                                logger.debug(f"Failed to parse pubDate {pub_date}: {e}")
                                continue

                    if not recent_news:
                        continue

                    # Calculate sentiment score for this symbol
                    positive_count = 0
                    negative_count = 0

                    for item in recent_news:
                        # Extract title from nested structure
                        content = item.get("content", {})
                        title = content.get("title", "").lower()

                        if not title:
                            continue

                        pos_matches = sum(1 for kw in self.positive_keywords if kw in title)
                        neg_matches = sum(1 for kw in self.negative_keywords if kw in title)

                        if pos_matches > neg_matches:
                            positive_count += 1
                        elif neg_matches > pos_matches:
                            negative_count += 1

                    total_count = len(recent_news)
                    if total_count > 0:
                        # Score: (positive - negative) / total, normalized to 0-1
                        raw_score = (positive_count - negative_count) / total_count
                        symbol_score = (raw_score + 1) / 2  # Map [-1,1] to [0,1]
                    else:
                        symbol_score = 0.5

                    symbol_sentiments[symbol] = {
                        "score": round(symbol_score, 3),
                        "headline_count": total_count,
                        "positive": positive_count,
                        "negative": negative_count,
                        "neutral": total_count - positive_count - negative_count
                    }

                    total_headlines += total_count
                    total_score += symbol_score

                except Exception as e:
                    logger.warning(f"Error fetching news for {symbol}: {e}")
                    continue

            # Calculate overall score
            if symbol_sentiments:
                overall_score = total_score / len(symbol_sentiments)
            else:
                overall_score = 0.5  # Neutral if no data

            interpretation = self._interpret_narrative(overall_score, total_headlines)

            return {
                "score": round(overall_score, 3),
                "symbol_sentiments": symbol_sentiments,
                "interpretation": interpretation,
                "headline_count": total_headlines,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "source": "News Headlines (Yahoo Finance)"
            }

        except Exception as e:
            logger.error(f"Error analyzing narrative sentiment: {e}")
            return self._neutral_response()

    def _is_recent(self, timestamp: int, hours: int = 24) -> bool:
        """Check if timestamp is within recent hours"""
        if not timestamp:
            return False

        news_time = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        return news_time >= cutoff

    def _interpret_narrative(self, score: float, headline_count: int) -> str:
        """Generate human-readable interpretation"""
        if headline_count == 0:
            return "无新闻数据"

        if score >= 0.7:
            return f"非常积极 (评分={score:.2f}，正面新闻占主导)"
        elif score >= 0.6:
            return f"偏积极 (评分={score:.2f}，整体偏正面)"
        elif score >= 0.4:
            return f"中性 (评分={score:.2f}，正负面平衡)"
        elif score >= 0.3:
            return f"偏消极 (评分={score:.2f}，整体偏负面)"
        else:
            return f"非常消极 (评分={score:.2f}，负面新闻占主导)"

    def _neutral_response(self) -> Dict:
        """Return neutral sentiment when data unavailable"""
        return {
            "score": 0.5,
            "symbol_sentiments": {},
            "interpretation": "数据不可用，使用中性评分",
            "headline_count": 0,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "Fallback"
        }


# Self-test
if __name__ == "__main__":
    print("=" * 60)
    print("Narrative Sentiment Analyzer - Self Test")
    print("=" * 60)

    analyzer = NarrativeSentimentAnalyzer()

    # Test 1: Market-level sentiment
    print("\n[Test 1] Market-level sentiment (SPY, QQQ):")
    result = analyzer.get_narrative_sentiment()

    print(f"\n✅ Narrative Sentiment:")
    print(f"  Score: {result['score']:.3f} / 1.0")
    print(f"  Total Headlines: {result['headline_count']}")
    print(f"  Interpretation: {result['interpretation']}")

    print(f"\n  Symbol Breakdown:")
    for symbol, data in result['symbol_sentiments'].items():
        print(f"    {symbol}: Score={data['score']}, "
              f"Headlines={data['headline_count']} "
              f"(+{data['positive']}, -{data['negative']}, ={data['neutral']})")

    print(f"\n  Timestamp: {result['timestamp']}")
    print(f"  Source: {result['source']}")

    # Test 2: Specific symbols
    print("\n" + "=" * 60)
    print("[Test 2] Specific symbols (AAPL, TSLA, NVDA):")
    result2 = analyzer.get_narrative_sentiment(["AAPL", "TSLA", "NVDA"])

    print(f"\n✅ Narrative Sentiment:")
    print(f"  Score: {result2['score']:.3f} / 1.0")
    print(f"  Total Headlines: {result2['headline_count']}")
    print(f"  Interpretation: {result2['interpretation']}")

    print(f"\n  Symbol Breakdown:")
    for symbol, data in result2['symbol_sentiments'].items():
        print(f"    {symbol}: Score={data['score']}, "
              f"Headlines={data['headline_count']} "
              f"(+{data['positive']}, -{data['negative']}, ={data['neutral']})")

    print("\n" + "=" * 60)
    print("✅ Test completed successfully")
