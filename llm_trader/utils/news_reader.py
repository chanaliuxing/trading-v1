"""
News Reader Utility for LLM Trader

Reads today's news from news.csv for specific stock symbols
"""
from __future__ import annotations
from typing import List, Dict, Optional
from datetime import date
import csv
import os


def get_today_news(symbol: str, news_csv_path: str = "./llm_trader/scripts/News/news.csv") -> List[Dict[str, str]]:
    """
    Get today's news for a specific stock symbol from news.csv

    Args:
        symbol: Stock ticker symbol (e.g., "TSLA", "AAPL")
        news_csv_path: Path to news.csv file

    Returns:
        List of news dictionaries with keys:
        - date: News date
        - ticker: Stock symbol
        - event_type: Type of event
        - direction: bullish/bearish
        - strength_0to3: Signal strength (0-3)
        - forward_looking: True/False
        - key_reasons: News summary/reasons
        - signal_quality: high/medium/low/rumor

    Example:
        >>> news = get_today_news("TSLA")
        >>> print(news[0])
        {
            'date': '2025-10-25',
            'ticker': 'TSLA',
            'event_type': 'earnings_miss',
            'direction': 'bearish',
            'strength_0to3': '2',
            'forward_looking': 'False',
            'key_reasons': '财报不及市场预期 | 直接影响盈利能力评估',
            'signal_quality': 'high'
        }
    """
    today = date.today().isoformat()  # Format: "2025-10-25"

    if not os.path.exists(news_csv_path):
        # Return empty list if file doesn't exist (graceful degradation)
        return []

    today_news = []

    try:
        with open(news_csv_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Check if date matches today AND ticker matches symbol
                if row.get('date') == today and row.get('ticker') == symbol:
                    today_news.append(row)
    except Exception as e:
        # Graceful degradation - return empty list on any error
        print(f"Warning: Failed to read news.csv: {e}")
        return []

    return today_news


def format_news_for_prompt(news_items: List[Dict[str, str]], language: str = "en") -> str:
    """
    Format news items into a concise string for LLM prompt

    Args:
        news_items: List of news dictionaries from get_today_news()
        language: "en" or "zh"

    Returns:
        Formatted string for prompt inclusion

    Example:
        >>> news = get_today_news("TSLA")
        >>> formatted = format_news_for_prompt(news, language="en")
        >>> print(formatted)
        📰 Today's News (2 items):
        1. [BEARISH ★★☆] Earnings Miss: 财报不及市场预期 | 直接影响盈利能力评估 (Quality: high)
        2. [BEARISH ★☆☆] Other: 第三方未经证实的行业退出预测 | 长期生存能力质疑 (Quality: rumor)
    """
    if not news_items:
        if language == "zh":
            return "📰 今日新闻: 无相关新闻"
        else:
            return "📰 Today's News: No news available"

    # Determine header based on language
    if language == "zh":
        header = f"📰 今日新闻 ({len(news_items)} 条):\n"
    else:
        header = f"📰 Today's News ({len(news_items)} items):\n"

    lines = [header]

    for i, item in enumerate(news_items, 1):
        direction = item.get('direction', 'neutral').upper()
        strength = int(item.get('strength_0to3', '0'))
        event_type = item.get('event_type', 'other').replace('_', ' ').title()
        key_reasons = item.get('key_reasons', 'N/A')
        signal_quality = item.get('signal_quality', 'unknown')

        # Create strength indicator (stars)
        strength_stars = "★" * strength + "☆" * (3 - strength)

        # Format line
        line = f"{i}. [{direction} {strength_stars}] {event_type}: {key_reasons} (Quality: {signal_quality})"
        lines.append(line)

    return "\n".join(lines)


def format_news_compact(news_items: List[Dict[str, str]]) -> str:
    """
    Format news items in ultra-compact format for token efficiency

    Args:
        news_items: List of news dictionaries

    Returns:
        Compact formatted string

    Example:
        >>> news = get_today_news("TSLA")
        >>> compact = format_news_compact(news)
        >>> print(compact)
        NEWS(2): 1)BEARISH★★☆ earnings_miss:财报不及预期|盈利下降 [high] 2)BEARISH★☆☆ other:退出预测|风险溢价 [rumor]
    """
    if not news_items:
        return "NEWS: None"

    parts = [f"NEWS({len(news_items)}):"]

    for i, item in enumerate(news_items, 1):
        direction = item.get('direction', 'neutral').upper()[:4]  # BULL/BEAR/NEUT
        strength = int(item.get('strength_0to3', '0'))
        strength_stars = "★" * strength + "☆" * (3 - strength)
        event = item.get('event_type', 'other')
        # Take first 2 reasons only to save tokens
        reasons = item.get('key_reasons', 'N/A').split('|')[:2]
        reasons_str = '|'.join(r.strip() for r in reasons)
        quality = item.get('signal_quality', 'unk')

        parts.append(f"{i}){direction}{strength_stars} {event}:{reasons_str} [{quality}]")

    return " ".join(parts)
