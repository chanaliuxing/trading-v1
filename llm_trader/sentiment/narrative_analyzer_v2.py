#!/usr/bin/env python3
"""
Narrative Sentiment Analyzer V2 - Yahoo News + LLM Analysis

Phase 2: Yahoo News (yfinance) + LLM深度分析 (直接复制googleNews.py的代码)
"""

import os
import re
import json
from typing import Dict, List, Optional, Any
from datetime import datetime, timezone, timedelta
import logging
import yfinance as yf
from dateutil.parser import parse as parse_datetime

# Load .env file (直接复制 googleNews.py)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logger = logging.getLogger(__name__)

# ==========================================================
# 直接复制 googleNews.py 的 SYSTEM_PROMPT
# ==========================================================
SYSTEM_PROMPT_NEWS = """你是一名严谨的美股基本面与事件驱动分析师。你的任务是：对给定的目标股票的新闻，按照现金流与风险的因果链来判断对该股票的经济方向（利好/利空/中性/混合），并输出结构化结果。
重要禁止：不得以"今天股价涨/跌""市场情绪热/冷"作为理由；不得把"价格回顾/复述"当作利好/利空。

[判定总则 / Principles]
1) 对象一致性：先明确新闻的主语公司是否就是目标股票；若只是顺带提及或仅为行业/宏观，按"相关/间接/无关"标注。
2) 因果为王：仅依据"对未来现金流、利润率、风险敞口、资本结构"的方向性影响来判定。
3) 面向未来：优先识别前瞻性信号（指引、合约、监管批复、并购要约、产能变化、法律最终判决等）。
4) 禁止价格当证据：禁止用"今日股价表现""盘中波动"作为利好/利空依据。
5) 混合情形：同一新闻含正负因素时，标注为"混合"，并拆分要点。
6) 时区：所有时间均以 America/New_York (ET) 理解。

[事件类型 → 方向映射（默认） / Event Taxonomy → Baseline Direction]
（仅作基础方向；最终以文字事实为准）
- 指引/展望：上调=利好；下调=利空
- 财报：显著超预期=利好；显著不及=利空
- 分析师评级/目标价：升级/上调=利好；降级/下调=利空
- 监管/法律：拿到许可/胜诉=利好；调查/不利裁决=利空
- 并购/要约：
  - 收购方（出资方）短期多为利空/中性偏空（融资/整合风险/摊薄）
  - 被收购方（目标）为利好（溢价预期）
- 股本动作：回购/增分红=利好；增发/可转债=利空（摊薄）
- 产能/供应：扩产、关键部件到位=利好；停产/召回/事故=利空
- 客户/合约：大单签约/续约=利好；大客户流失=利空
- 人事：关键高管离职=利空；强将加盟=利好
- 成本/组织：大规模裁员=短期边际利好（降本），长期视业务健康度，中性为宜
- 合作/生态：与强势伙伴实质绑定=小幅利好；仅口头"探索/谅解备忘录"偏中性

[必须识别并标注的"非信号/弱信号" / Ignore-or-Weak]
- 纯价格复述（如"因上涨而上涨""股价因大涨再创新高"）→ signal_quality="price_recap"，direction="neutral"。
- 营销炒作/模糊表述（"震撼""革命性""行业颠覆"且无可验证事实）→ signal_quality="promotional"。
- 二手转述/无新信息 → signal_quality="duplicate_or_no_novelty"。
- 传闻/匿名消息（未被权威来源佐证）→ signal_quality="rumor"。

[输入格式 / Input]
<INPUT>
ticker: <目标股票, 例如 AMD>
time_et: <新闻发布时间(ET), 可空>
source: <来源, 可空>
headline: <标题>
summary: <摘要或正文片段, 可空>
context: <可选：行业/对手/客户/并购对象等补充，若有>
</INPUT>

[输出格式 / Output JSON]
只输出合法 JSON：
{
  "ticker": "AMD",
  "subject_match": "exact|related|unrelated",
  "event_type": "guidance_up|guidance_down|earnings_beat|earnings_miss|rating_up|rating_down|reg_approval|legal_win|legal_loss|mna_acquirer|mna_target|share_buyback|equity_offering|dividend_change|capacity_up|capacity_down|recall|outage|major_contract|customer_loss|exec_join|exec_exit|layoffs|partnership|other",
  "direction": "bullish|bearish|neutral|mixed",
  "strength_0to3": 0,
  "forward_looking": true,
  "time_horizon": "hours|days|quarters",
  "key_reasons": [
    "事实点1（引用新闻中的可验证事实，不提股价表现）",
    "事实点2",
    "事实点3"
  ],
  "signal_quality": "high|medium|low|price_recap|promotional|duplicate_or_no_novelty|rumor",
  "notes": "可选：例如并购中收购方与目标方的方向不同等"
}

[少量示例 / Few-shot]
示例 1：评级上调（TSLA）
Input:
ticker: TSLA
time_et: 2025-10-24 17:32
source: Reuters
headline: BigBank upgrades Tesla to 'Buy', raises target to $310 on margin outlook
summary: The analyst cites improving production efficiency and energy storage growth.

Output:
{
  "ticker": "TSLA",
  "subject_match": "exact",
  "event_type": "rating_up",
  "direction": "bullish",
  "strength_0to3": 2,
  "forward_looking": true,
  "time_horizon": "days",
  "key_reasons": [
    "权威机构上调评级与目标价",
    "理由指向未来利润率改善与新业务增长（前瞻性）",
    "信息可验证且与核心财务因子相关"
  ],
  "signal_quality": "high",
  "notes": ""
}

示例 2：并购（A 收购 B）
Input:
ticker: A
headline: A to acquire B in $5B cash-and-stock deal
summary: The transaction will be funded partly via new share issuance.

Output（A 为收购方）:
{
  "ticker": "A",
  "subject_match": "exact",
  "event_type": "mna_acquirer",
  "direction": "bearish",
  "strength_0to3": 1,
  "forward_looking": true,
  "time_horizon": "quarters",
  "key_reasons": [
    "收购方承担整合与执行风险",
    "部分对价为增发，存在摊薄",
    "对未来现金流不确定性上升"
  ],
  "signal_quality": "high",
  "notes": "若目标为 B，则 B 的方向相反（利好）"
}

示例 3：价格复述（应判中性/不使用）
Input:
ticker: AMD
headline: AMD shares jump 5% today as investors cheer AI optimism
summary: No new guidance or contracts were provided.

Output:
{
  "ticker": "AMD",
  "subject_match": "exact",
  "event_type": "other",
  "direction": "neutral",
  "strength_0to3": 0,
  "forward_looking": false,
  "time_horizon": "hours",
  "key_reasons": [
    "仅为价格表现的复述",
    "未提供可验证的新事实（指引、合约、批文等）",
    "不满足因果链到现金流/风险的证据"
  ],
  "signal_quality": "price_recap",
  "notes": ""
}
"""


# ==========================================================
# 直接复制 googleNews.py 的 LLM Client 代码
# ==========================================================
class DeepseekNvidiaClient:
    """直接复制自 googleNews.py"""

    def __init__(self, model: Optional[str] = None):
        from openai import OpenAI

        api_key = os.getenv("DEEPSEEK_NVIDIA_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("缺少 DEEPSEEK_NVIDIA_API_KEY 环境变量")

        base = os.getenv("DEEPSEEK_NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")
        self._cli = OpenAI(api_key=api_key, base_url=base)
        self._model = model or os.getenv("DEEPSEEK_NVIDIA_MODEL", "deepseek-ai/deepseek-r1")
        logger.info(f"LLM: DeepSeek-NVIDIA model={self._model} base={base}")

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        r = self._cli.chat.completions.create(
            model=self._model,
            temperature=0.2,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        content = ""
        try:
            content = (r.choices[0].message.content or "").strip()
        except Exception:
            pass
        if not content:
            try:
                rc = r.choices[0].message.reasoning_content
                if rc:
                    content = rc.strip()
            except Exception:
                pass
        return content


def build_batch_user_prompt(rows: List[Dict[str, Any]]) -> str:
    """直接复制自 googleNews.py"""
    lines = []
    lines.append("共有以下多条 INPUT，请严格为每条 INPUT 输出一条 JSON，并以 JSON 数组返回（只输出 JSON 数组）：")
    for r in rows:
        lines.append("<INPUT>")
        lines.append(f"ticker: {(r.get('ticker') or '').upper()}")
        lines.append(f"time_et: {r.get('time_et','')}")
        lines.append(f"source: {r.get('source','')}")
        headline = re.sub(r"\s+", " ", (r.get("headline") or "").strip())[:300]
        summary = re.sub(r"\s+", " ", (r.get("summary") or "").strip())[:700]
        lines.append(f"headline: {headline}")
        lines.append(f"summary: {summary}")
        lines.append(f"context: {r.get('query','')}")
        lines.append("</INPUT>")
    return "\n".join(lines)


def extract_json_array(text: str) -> Optional[List[Dict[str, Any]]]:
    """直接复制自 googleNews.py"""
    if not text:
        return None
    s = text.strip()
    if "```json" in s:
        try:
            s = s.split("```json", 1)[1].split("```", 1)[0].strip()
        except Exception:
            pass
    elif s.startswith("```") and s.endswith("```"):
        s = s[3:-3].strip()
    start = s.find("[")
    end = s.rfind("]")
    if start >= 0 and end > start:
        frag = s[start : end + 1]
        try:
            data = json.loads(frag)
            if isinstance(data, list):
                return data
        except Exception:
            pass
    return None


# ==========================================================
# Narrative Analyzer V2
# ==========================================================
class NarrativeAnalyzerV2:
    """Phase 2: Yahoo News (yfinance) + LLM分析 (直接复制代码)"""

    def __init__(self, provider: str = "deepseek-nvidia", model: Optional[str] = None):
        """
        Initialize analyzer

        Args:
            provider: 只支持 deepseek-nvidia (其他provider可以扩展)
            model: Optional model override
        """
        self.provider = provider
        self.model = model
        self.llm_client = None

        try:
            self.llm_client = DeepseekNvidiaClient(model=model)
            logger.info(f"LLM client initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize LLM client: {e}")

    def get_narrative_sentiment(self, symbols: List[str] = None) -> Dict:
        """
        Get narrative sentiment using yfinance news + LLM analysis

        Returns:
        {
            "score": float,  # 0.0-1.0
            "symbol_sentiments": dict,
            "interpretation": str,
            "headline_count": int,
            "timestamp": str,
            "source": str
        }
        """
        if not self.llm_client:
            logger.error("LLM not available, 回退到 Phase 1")
            return self._fallback_to_phase1(symbols)

        if symbols is None:
            symbols = ["SPY", "QQQ"]

        try:
            # Step 1: 用 yfinance 抓取新闻
            logger.info(f"Fetching Yahoo news for {len(symbols)} symbols...")
            news_rows = self._fetch_yahoo_news(symbols)

            if not news_rows:
                logger.warning("No Yahoo news fetched, falling back to Phase 1")
                return self._fallback_to_phase1(symbols)

            logger.info(f"Fetched {len(news_rows)} Yahoo news items")

            # Step 2: 用 LLM 分析
            logger.info("Analyzing news with LLM...")
            llm_results = self._analyze_with_llm(news_rows)

            if not llm_results:
                logger.warning("LLM analysis returned empty, falling back to Phase 1")
                return self._fallback_to_phase1(symbols)

            # Step 3: 转换为 sentiment engine 格式
            return self._convert_to_sentiment_format(llm_results, symbols)

        except Exception as e:
            logger.error(f"Error in narrative analysis: {e}", exc_info=True)
            return self._fallback_to_phase1(symbols)

    def _fetch_yahoo_news(self, symbols: List[str]) -> List[Dict[str, Any]]:
        """用 yfinance 抓取新闻"""
        news_rows = []
        cutoff_time = datetime.now(timezone.utc) - timedelta(hours=24)

        for symbol in symbols:
            try:
                ticker = yf.Ticker(symbol)
                news = ticker.news

                if not news:
                    logger.debug(f"No news for {symbol}")
                    continue

                for item in news:
                    content = item.get("content", {})
                    pub_date = content.get("pubDate")

                    if not pub_date:
                        continue

                    try:
                        dt = parse_datetime(pub_date)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                    except Exception as e:
                        logger.debug(f"Failed to parse pubDate {pub_date}: {e}")
                        continue

                    # 只保留24小时内的新闻
                    if dt < cutoff_time:
                        continue

                    title = content.get("title", "")
                    summary = item.get("content", {}).get("summary", "")

                    news_rows.append(
                        {
                            "ticker": symbol,
                            "time_et": dt.strftime("%Y-%m-%d %H:%M:%S"),
                            "source": item.get("source", {}).get("name", "Yahoo Finance"),
                            "headline": title,
                            "summary": summary,
                            "query": f"{symbol} stock",
                        }
                    )

            except Exception as e:
                logger.warning(f"Failed to fetch news for {symbol}: {e}")
                continue

        return news_rows

    def _analyze_with_llm(self, news_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """用 LLM 分析新闻"""
        if not news_rows:
            return []

        try:
            # 构建批量 user prompt
            user_prompt = build_batch_user_prompt(news_rows)

            # 调用 LLM
            logger.debug(f"Calling LLM with {len(news_rows)} news items...")
            response = self.llm_client.complete(
                system_prompt=SYSTEM_PROMPT_NEWS, user_prompt=user_prompt
            )

            # 提取 JSON 数组
            results = extract_json_array(response)

            if not results:
                logger.warning("Failed to extract JSON from LLM response")
                logger.debug(f"LLM response: {response[:500]}...")
                return []

            logger.info(f"LLM analyzed {len(results)} news items")

            # 过滤掉 neutral 和低质量信号
            filtered = []
            for r in results:
                direction = r.get("direction", "neutral")
                quality = r.get("signal_quality", "low")

                # 只保留 bullish/bearish 且质量不是 price_recap/promotional 的信号
                if direction in ("bullish", "bearish") and quality not in (
                    "price_recap",
                    "promotional",
                    "duplicate_or_no_novelty",
                    "rumor",
                ):
                    filtered.append(r)

            logger.info(f"Filtered to {len(filtered)} high-quality signals")
            return filtered

        except Exception as e:
            logger.error(f"LLM analysis failed: {e}", exc_info=True)
            return []

    def _convert_to_sentiment_format(
        self, llm_results: List[Dict], symbols: List[str]
    ) -> Dict:
        """将 LLM 输出转换为 sentiment engine 格式"""

        symbol_sentiments = {}
        total_headlines = 0

        # 按 ticker 分组
        by_ticker = {}
        for item in llm_results:
            ticker = item.get("ticker", "").upper()
            if ticker not in by_ticker:
                by_ticker[ticker] = []
            by_ticker[ticker].append(item)

        # 计算每个 symbol 的评分
        for symbol in symbols:
            if symbol not in by_ticker:
                continue

            items = by_ticker[symbol]
            positive = sum(1 for x in items if x.get("direction") == "bullish")
            negative = sum(1 for x in items if x.get("direction") == "bearish")
            neutral = len(items) - positive - negative

            # 加权评分 (考虑 strength_0to3)
            weighted_score = 0.0
            for item in items:
                direction = item.get("direction", "neutral")
                strength = item.get("strength_0to3", 1) / 3.0  # Normalize to 0-1

                if direction == "bullish":
                    weighted_score += strength * 1.0
                elif direction == "bearish":
                    weighted_score += strength * 0.0
                else:
                    weighted_score += 0.5

            if len(items) > 0:
                symbol_score = weighted_score / len(items)
            else:
                symbol_score = 0.5

            symbol_sentiments[symbol] = {
                "score": round(symbol_score, 3),
                "headline_count": len(items),
                "positive": positive,
                "negative": negative,
                "neutral": neutral,
                "method": "llm_semantic_v2",
                "events": [
                    {
                        "type": x.get("event_type"),
                        "direction": x.get("direction"),
                        "strength": x.get("strength_0to3"),
                        "quality": x.get("signal_quality"),
                    }
                    for x in items
                ],
            }

            total_headlines += len(items)

        # 计算综合评分
        if symbol_sentiments:
            overall_score = sum(d["score"] for d in symbol_sentiments.values()) / len(
                symbol_sentiments
            )
        else:
            overall_score = 0.5

        interpretation = self._interpret_narrative(overall_score, total_headlines)

        return {
            "score": round(overall_score, 3),
            "symbol_sentiments": symbol_sentiments,
            "interpretation": interpretation,
            "headline_count": total_headlines,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "Yahoo News (yfinance) + LLM Deep Analysis (DeepSeek-NVIDIA)",
        }

    def _interpret_narrative(self, score: float, headline_count: int) -> str:
        """Generate human-readable interpretation"""
        if headline_count == 0:
            return "无新闻数据"

        if score >= 0.7:
            return f"非常积极 (LLM深度分析={score:.2f}，正面新闻占主导)"
        elif score >= 0.6:
            return f"偏积极 (LLM深度分析={score:.2f}，整体偏正面)"
        elif score >= 0.4:
            return f"中性 (LLM深度分析={score:.2f}，正负面平衡)"
        elif score >= 0.3:
            return f"偏消极 (LLM深度分析={score:.2f}，整体偏负面)"
        else:
            return f"非常消极 (LLM深度分析={score:.2f}，负面新闻占主导)"

    def _fallback_to_phase1(self, symbols: List[str]) -> Dict:
        """Fallback to Phase 1 keyword analysis"""
        logger.warning("Falling back to Phase 1 keyword analysis")
        from llm_trader.sentiment.narrative_analyzer import NarrativeSentimentAnalyzer

        phase1 = NarrativeSentimentAnalyzer()
        return phase1.get_narrative_sentiment(symbols)


# Self-test
if __name__ == "__main__":
    import sys

    print("=" * 70)
    print("Narrative Sentiment Analyzer V2 - Self Test")
    print("Yahoo News (yfinance) + LLM 深度分析 (代码直接复制)")
    print("=" * 70)

    analyzer = NarrativeAnalyzerV2()

    if not analyzer.llm_client:
        print("\n❌ LLM client not available")
        print("   Set DEEPSEEK_NVIDIA_API_KEY environment variable")
        sys.exit(1)

    print("\n[Test] Analyzing AAPL, TSLA, NVDA...")
    result = analyzer.get_narrative_sentiment(["AAPL", "TSLA", "NVDA"])

    print(f"\n✅ Narrative Sentiment (LLM Deep Analysis):")
    print(f"  Score: {result['score']:.3f} / 1.0")
    print(f"  Total Headlines: {result['headline_count']}")
    print(f"  Interpretation: {result['interpretation']}")
    print(f"  Source: {result['source']}")

    print(f"\n  Symbol Breakdown:")
    for symbol, data in result["symbol_sentiments"].items():
        print(
            f"    {symbol}: Score={data['score']}, "
            f"Headlines={data['headline_count']} "
            f"(+{data['positive']}, -{data['negative']}, ={data['neutral']}) "
            f"Method={data.get('method', 'unknown')}"
        )

        # Show event details
        if data.get("events"):
            print(f"      Events:")
            for evt in data["events"]:
                print(
                    f"        - {evt['type']}: {evt['direction']} "
                    f"(strength={evt['strength']}, quality={evt['quality']})"
                )

    print(f"\n  Timestamp: {result['timestamp']}")

    print("\n" + "=" * 70)
    print("✅ Test completed")
