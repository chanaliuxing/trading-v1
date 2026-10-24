#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
googleNews.py  —  从 config.yml 读取 tickers → 少量 Google News → 一次性调用 LLM → 输出
对齐旧版：
  - ticker 来源：config.yml（递归提取），也支持 watchlist_file/tickers_file
  - 每只仅抓 2~3 条（默认 3，可在 config 或 --limit 改）
  - 只调一次 LLM（把全部 INPUT 打包）
  - 输出到 llm_trader/scripts/news/
  - DeepSeek-NVIDIA reasoning 兼容；也支持 openai/gemini/deepseek
本版变更：
  - 最终 CSV 仅包含：ticker,event_type,direction,strength_0to3,forward_looking,key_reasons,signal_quality
  - 最终 JSON 继续过滤 neutral（默认开启）
依赖：feedparser, pyyaml, python-dotenv, openai(若用), google-generativeai(若用)
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

# ---- 依赖（尽量宽松）----
try:
    import feedparser  # pip install feedparser
except Exception:
    feedparser = None

try:
    import yaml  # pip install pyyaml
except Exception:
    yaml = None

try:
    from dotenv import load_dotenv  # pip install python-dotenv
    load_dotenv()
except Exception:
    pass

ET = ZoneInfo("America/New_York")

# ==========================================================
# 不改动的系统 Prompt（保持原样）
# ==========================================================
SYSTEM_PROMPT_NEWS = """你是一名严谨的美股基本面与事件驱动分析师。你的任务是：对给定的目标股票的新闻，按照现金流与风险的因果链来判断对该股票的经济方向（利好/利空/中性/混合），并输出结构化结果。
重要禁止：不得以“今天股价涨/跌”“市场情绪热/冷”作为理由；不得把“价格回顾/复述”当作利好/利空。

[判定总则 / Principles]
1) 对象一致性：先明确新闻的主语公司是否就是目标股票；若只是顺带提及或仅为行业/宏观，按“相关/间接/无关”标注。
2) 因果为王：仅依据“对未来现金流、利润率、风险敞口、资本结构”的方向性影响来判定。
3) 面向未来：优先识别前瞻性信号（指引、合约、监管批复、并购要约、产能变化、法律最终判决等）。
4) 禁止价格当证据：禁止用“今日股价表现”“盘中波动”作为利好/利空依据。
5) 混合情形：同一新闻含正负因素时，标注为“混合”，并拆分要点。
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
- 合作/生态：与强势伙伴实质绑定=小幅利好；仅口头“探索/谅解备忘录”偏中性

[必须识别并标注的“非信号/弱信号” / Ignore-or-Weak]
- 纯价格复述（如“因上涨而上涨”“股价因大涨再创新高”）→ signal_quality="price_recap"，direction="neutral"。
- 营销炒作/模糊表述（“震撼”“革命性”“行业颠覆”且无可验证事实）→ signal_quality="promotional"。
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

# ============================
# 日志
# ============================
class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(tz=ET).strftime("%Y-%m-%d %H:%M:%S"),
            "level": record.levelname,
            "msg": record.getMessage(),
            "logger": record.name,
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)

def setup_logging(level: str = "INFO", log_file: Optional[str] = None, json_logs: bool = False) -> None:
    lvl = getattr(logging, level.upper(), logging.INFO)
    logger = logging.getLogger()
    logger.setLevel(lvl)
    logger.handlers.clear()
    fmt = JsonFormatter() if json_logs else logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(lvl)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    if log_file:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(lvl)
        fh.setFormatter(fmt)
        logger.addHandler(fh)

log = logging.getLogger("googleNews")

# ============================
# 目录 & 默认路径
# ============================
def script_root() -> Path:
    return Path(__file__).resolve().parent

def news_root() -> Path:
    return script_root() / "news"

def ensure_dirs() -> Tuple[Path, Path, Path]:
    out_dir = news_root()
    logs_dir = out_dir / "logs"
    cache_dir = out_dir / "cache"
    for d in (out_dir, logs_dir, cache_dir):
        d.mkdir(parents=True, exist_ok=True)
    return out_dir, logs_dir, cache_dir

def default_paths_for_today() -> Tuple[Path, Path, Path, Path]:
    out_dir, logs_dir, cache_dir = ensure_dirs()
    now = datetime.now(tz=ET)
    d = now.strftime("%Y%m%d")
    ts = now.strftime("%Y%m%d_%H%M")
    out_csv = out_dir / "news.csv"
    raw_csv = out_dir / f"google_news_raw_{d}.csv"
    log_file = logs_dir / f"googleNews_{ts}.log"
    cache_file = cache_dir / f"googleNews_{d}.json"
    return out_csv, raw_csv, log_file, cache_file

# ============================
# 配置读取（递归提取 tickers）
# ============================
def locate_config_file() -> Optional[Path]:
    candidates = [
        script_root() / "config.yml",
        script_root().parent / "config.yml",
        script_root().parent / "data" / "config.yml",
        Path(os.getcwd()) / "config.yml",
    ]
    for p in candidates:
        if p.exists():
            return p
    return None

_TICKER_KEYS = {
    "tickers","symbols","watchlist","universe","positions","portfolio","holdings","stocks"
}
_FILE_KEYS = {"watchlist_file","tickers_file","universe_file"}

def _normalize_ticker(s: str) -> Optional[str]:
    if not s: return None
    t = re.sub(r"[^A-Za-z.\-]", "", str(s)).upper().strip()
    return t or None

def _extract_from_iterable(it: Iterable[Any], out: List[str]) -> None:
    for x in it:
        if isinstance(x, (list,tuple,set)):
            _extract_from_iterable(x, out)
        elif isinstance(x, dict):
            _extract_from_cfg_dict(x, out)
        else:
            t = _normalize_ticker(str(x))
            if t: out.append(t)

def _extract_from_cfg_dict(cfg: Dict[str, Any], out: List[str]) -> None:
    for k, v in cfg.items():
        key = str(k).lower()
        if key in _TICKER_KEYS:
            if isinstance(v, (list,tuple,set)):
                _extract_from_iterable(v, out)
            elif isinstance(v, str):
                parts = re.split(r"[,\n\r]+", v)
                _extract_from_iterable(parts, out)
        elif key in _FILE_KEYS:
            path = Path(str(v)).expanduser()
            if path.exists():
                out.extend(read_tickers_from_file(path))
        else:
            if isinstance(v, dict):
                _extract_from_cfg_dict(v, out)
            elif isinstance(v, (list,tuple,set)):
                _extract_from_iterable(v, out)

def read_tickers_from_file(path: Path) -> List[str]:
    tickers: List[str] = []
    try:
        if path.suffix.lower() == ".csv":
            with open(path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    t = row.get("ticker") or row.get("symbol") or row.get("股票代码")
                    t = _normalize_ticker(t)
                    if t: tickers.append(t)
        else:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    t = _normalize_ticker(line.strip())
                    if t: tickers.append(t)
    except Exception as e:
        log.warning("读取外部 ticker 文件失败：%s (%s)", path, e)
    return tickers

def load_tickers_from_config() -> List[str]:
    cfg_path = locate_config_file()
    if not cfg_path or yaml is None:
        log.warning("未找到 config.yml 或缺少 pyyaml，回退到默认大盘股。")
        return ["AAPL","MSFT","NVDA","TSLA","AMD","AMZN","META","GOOGL"]
    with open(cfg_path, "r", encoding="utf-8") as f:
        try:
            cfg = yaml.safe_load(f) or {}
        except Exception as e:
            log.error("解析 config.yml 失败：%s", e)
            return ["AAPL","MSFT","NVDA","TSLA","AMD","AMZN","META","GOOGL"]

    raw: List[str] = []
    _extract_from_cfg_dict(cfg, raw)
    uniq: List[str] = []
    seen = set()
    for t in raw:
        if t not in seen:
            uniq.append(t); seen.add(t)
    log.info("Config: %s | 读取 tickers=%d", cfg_path, len(uniq))
    return uniq if uniq else ["AAPL","MSFT","NVDA","TSLA","AMD","AMZN","META","GOOGL"]

def load_news_settings_from_config() -> Dict[str, Any]:
    cfg_path = locate_config_file()
    out = {}
    if not cfg_path or yaml is None:
        return out
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except Exception:
        return out
    def search(d: Any):
        if isinstance(d, dict):
            for k,v in d.items():
                if str(k).lower() in {"news","googlenews","google_news"} and isinstance(v, dict):
                    out.update(v)
                search(v)
        elif isinstance(d, (list,tuple,set)):
            for x in d: search(x)
    search(cfg)
    return out

# ============================
# Google News 抓取 (RSS)
# ============================
def build_rss_url(query: str, days: int = 1, lang: str = "en", country: str = "US") -> str:
    term = query.strip().replace(" ", "+")
    return f"https://news.google.com/rss/search?q={term}+when%3A{days}d&hl={lang}-{country}&gl={country}&ceid={country}%3A{lang}"

def to_et_from_struct(tm_struct) -> Optional[datetime]:
    if not tm_struct:
        return None
    try:
        dt_utc = datetime.fromtimestamp(time.mktime(tm_struct), tz=timezone.utc)
        return dt_utc.astimezone(ET)
    except Exception:
        return None

def clean_html(s: str) -> str:
    if not s: return ""
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def fetch_for_ticker(ticker: str, limit: int, days: int) -> List[Dict[str, Any]]:
    if feedparser is None:
        log.error("缺少 feedparser，请 `pip install feedparser`")
        return []
    q = f"{ticker} stock"
    url = build_rss_url(q, days=days)
    feed = feedparser.parse(url)
    entries = feed.entries or []
    if limit > 0:
        entries = entries[:limit]
    rows: List[Dict[str, Any]] = []
    for e in entries:
        title = (getattr(e, "title", "") or "").strip()
        summary = clean_html(getattr(e, "summary", "") or "")
        pub = getattr(e, "published_parsed", None)
        dt = to_et_from_struct(pub)
        link = getattr(e, "link", "") or ""
        source = ""
        try:
            source = e.source.title
        except Exception:
            pass
        rows.append({
            "ticker": ticker.upper(),
            "time_et": dt.strftime("%Y-%m-%d %H:%M:%S") if dt else "",
            "source": source,
            "headline": title,
            "summary": summary,
            "link": link,
            "query": q,
        })
    log.info("GoogleNews[%s]: 抓取 %d 条 (days=%d, limit=%d)", ticker, len(rows), days, limit)
    return rows

def fetch_all_tickers(tickers: List[str], per_limit: int, days: int, delay: float = 0.4) -> List[Dict[str, Any]]:
    all_rows: List[Dict[str, Any]] = []
    for i, t in enumerate(tickers):
        all_rows.extend(fetch_for_ticker(t, per_limit, days))
        if i < len(tickers) - 1 and delay > 0:
            time.sleep(delay)
    return all_rows

# ============================
# CSV I/O
# ============================
def write_raw_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return
    cols = ["ticker","time_et","source","headline","summary","link","query"]
    os.makedirs(path.parent, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})
    log.info("RawNews: 写出 %d 条 → %s", len(rows), path)

def write_sentiment_csv_minimal(path: Path, rows: List[Dict[str, Any]]) -> None:
    """
    最终情绪 CSV：仅保留以下列，满足你的二次 LLM 省 token 需求：
    date,ticker,event_type,direction,strength_0to3,forward_looking,key_reasons,signal_quality
    其中 key_reasons 为 " | " 拼接的短句列表。
    """
    cols = ["date","ticker","event_type","direction","strength_0to3","forward_looking","key_reasons","signal_quality"]
    os.makedirs(path.parent, exist_ok=True)
    # 获取今天日期 (ET)
    today = datetime.now(tz=ET).strftime("%Y-%m-%d")
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            # key_reasons 可能是 list/str/None，统一转成 " | " 字符串
            kr = r.get("key_reasons", [])
            if isinstance(kr, list):
                kr_str = " | ".join([re.sub(r"\s+"," ", str(x)).strip() for x in kr if str(x).strip()])
            elif isinstance(kr, str):
                kr_str = re.sub(r"\s+"," ", kr).strip()
            else:
                kr_str = ""
            w.writerow({
                "date": today,
                "ticker": r.get("ticker",""),
                "event_type": r.get("event_type",""),
                "direction": r.get("direction",""),
                "strength_0to3": r.get("strength_0to3",""),
                "forward_looking": r.get("forward_looking",""),
                "key_reasons": kr_str,
                "signal_quality": r.get("signal_quality",""),
            })
    log.info("CSV(MIN): 写出 %d 条 → %s", len(rows), path)

# ============================
# 时间窗口 & 过滤
# ============================
def parse_et(s: str) -> Optional[datetime]:
    if not s: return None
    fmts = [
        "%Y-%m-%d %H:%M:%S","%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S","%Y-%m-%dT%H:%M",
        "%a, %d %b %Y %H:%M:%S %Z",
    ]
    for fmt in fmts:
        try:
            dt = datetime.strptime(s, fmt)
            if "%Z" in fmt:
                return dt.replace(tzinfo=timezone.utc).astimezone(ET)
            return dt.replace(tzinfo=ET)
        except Exception:
            continue
    return None

def filter_by_window(rows: List[Dict[str, Any]], start: datetime, end: datetime) -> List[Dict[str, Any]]:
    kept = []
    drop = 0
    for r in rows:
        dt = parse_et(r.get("time_et",""))
        if not dt or dt < start or dt >= end:
            drop += 1
            continue
        kept.append(r)
    log.info("TimeFilter: %d → %d（丢弃 %d）窗口: %s ~ %s",
             len(rows), len(kept), drop,
             start.strftime("%Y-%m-%d %H:%M:%S"),
             end.strftime("%Y-%m-%d %H:%M:%S"))
    return kept

# ============================
# LLM 适配（DeepSeek-NVIDIA 优先）
# ============================
class LLMClientBase:
    def complete(self, system_prompt: str, user_prompt: str) -> str:
        raise NotImplementedError

def _try_import_user_providers():
    mods = {}
    for name in ("openai_provider","gemini_provider","deepseek_provider","deepseek_nvidia_provider"):
        try:
            mods[name] = __import__(name)
            log.debug("导入 provider 成功: %s", name)
        except Exception:
            try:
                mods[name] = __import__(f"llm_trader.providers.{name}", fromlist=["*"])
                log.debug("导入 llm_trader.providers.%s 成功", name)
            except Exception:
                pass
    return mods

USER_PROVIDERS = _try_import_user_providers()

class OpenAIClient(LLMClientBase):
    def __init__(self, model: Optional[str]=None):
        if "openai_provider" in USER_PROVIDERS:
            self._prov = USER_PROVIDERS["openai_provider"]; self._mode="user"; self._model=model
            log.info("LLM: OpenAI（使用你的 openai_provider 封装）"); return
        from openai import OpenAI
        api_key = os.getenv("OPENAI_API_KEY","").strip()
        if not api_key: raise RuntimeError("缺少 OPENAI_API_KEY")
        self._cli = OpenAI(api_key=api_key)
        self._model = model or os.getenv("OPENAI_MODEL","gpt-4o-mini")
        self._mode="sdk"; log.info("LLM: OpenAI（官方 SDK） model=%s", self._model)
    def complete(self, system_prompt: str, user_prompt: str) -> str:
        if self._mode=="user":
            for m in ("call","chat","complete"):
                if hasattr(self._prov,m):
                    return getattr(self._prov,m)(system_prompt, user_prompt, model=self._model)
            raise RuntimeError("openai_provider 未提供兼容方法")
        r = self._cli.chat.completions.create(
            model=self._model, temperature=0.2,
            messages=[{"role":"system","content":system_prompt},{"role":"user","content":user_prompt}]
        )
        return (r.choices[0].message.content or "").strip()

class GeminiClient(LLMClientBase):
    def __init__(self, model: Optional[str]=None):
        if "gemini_provider" in USER_PROVIDERS:
            self._prov = USER_PROVIDERS["gemini_provider"]; self._mode="user"; self._model=model
            log.info("LLM: Gemini（使用你的 gemini_provider 封装）"); return
        import google.generativeai as genai
        api_key = os.getenv("GEMINI_API_KEY","").strip()
        if not api_key: raise RuntimeError("缺少 GEMINI_API_KEY")
        genai.configure(api_key=api_key)
        self._model_name = model or os.getenv("GEMINI_MODEL","gemini-1.5-flash")
        self._model = genai.GenerativeModel(self._model_name)
        self._mode="sdk"; log.info("LLM: Gemini（官方 SDK） model=%s", self._model_name)
    def complete(self, system_prompt: str, user_prompt: str) -> str:
        if self._mode=="user":
            for m in ("call","chat","complete"):
                if hasattr(self._prov,m):
                    return getattr(self._prov,m)(system_prompt, user_prompt, model=self._model_name)
            raise RuntimeError("gemini_provider 未提供兼容方法")
        text = f"{system_prompt.strip()}\n\n{user_prompt.strip()}"
        r = self._model.generate_content(text)
        return (getattr(r,"text","") or "").strip()

class DeepseekClient(LLMClientBase):
    def __init__(self, model: Optional[str]=None):
        if "deepseek_provider" in USER_PROVIDERS:
            self._prov = USER_PROVIDERS["deepseek_provider"]; self._mode="user"; self._model=model
            log.info("LLM: DeepSeek（使用你的 deepseek_provider 封装）"); return
        from openai import OpenAI
        api_key = os.getenv("DEEPSEEK_API_KEY","").strip()
        if not api_key: raise RuntimeError("缺少 DEEPSEEK_API_KEY")
        base = os.getenv("DEEPSEEK_BASE_URL","https://api.deepseek.com")
        self._cli = OpenAI(api_key=api_key, base_url=base)
        self._model = model or os.getenv("DEEPSEEK_MODEL","deepseek-chat")
        self._mode="sdk"; log.info("LLM: DeepSeek（OpenAI兼容） model=%s base=%s", self._model, base)
    def complete(self, system_prompt: str, user_prompt: str) -> str:
        if self._mode=="user":
            for m in ("call","chat","complete"):
                if hasattr(self._prov,m):
                    return getattr(self._prov,m)(system_prompt, user_prompt, model=self._model)
            raise RuntimeError("deepseek_provider 未提供兼容方法")
        r = self._cli.chat.completions.create(
            model=self._model, temperature=0.2,
            messages=[{"role":"system","content":system_prompt},{"role":"user","content":user_prompt}]
        )
        return (r.choices[0].message.content or "").strip()

class DeepseekNvidiaClient(LLMClientBase):
    def __init__(self, model: Optional[str]=None):
        if "deepseek_nvidia_provider" in USER_PROVIDERS:
            self._prov = USER_PROVIDERS["deepseek_nvidia_provider"]; self._mode="user"; self._model=model
            log.info("LLM: DeepSeek-NVIDIA（使用你的封装）"); return
        from openai import OpenAI
        api_key = os.getenv("DEEPSEEK_NVIDIA_API_KEY","").strip()
        if not api_key: raise RuntimeError("缺少 DEEPSEEK_NVIDIA_API_KEY")
        base = os.getenv("DEEPSEEK_NVIDIA_BASE_URL","https://integrate.api.nvidia.com/v1")
        self._cli = OpenAI(api_key=api_key, base_url=base)
        self._model = model or os.getenv("DEEPSEEK_NVIDIA_MODEL","deepseek-ai/deepseek-r1")
        self._mode="sdk"; log.info("LLM: DeepSeek-NVIDIA（reasoning） model=%s base=%s", self._model, base)
    def complete(self, system_prompt: str, user_prompt: str) -> str:
        if self._mode=="user":
            for m in ("call","chat","complete"):
                if hasattr(self._prov,m):
                    return getattr(self._prov,m)(system_prompt, user_prompt, model=self._model)
            raise RuntimeError("deepseek_nvidia_provider 未提供兼容方法")
        r = self._cli.chat.completions.create(
            model=self._model, temperature=0.2,
            messages=[{"role":"system","content":system_prompt},{"role":"user","content":user_prompt}]
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

def build_client(provider: Optional[str], model: Optional[str]) -> LLMClientBase:
    p = (provider or "").strip().lower()
    order = []
    if p in ("", "auto"):
        order = ["deepseek_nvidia","openai","gemini","deepseek"]
    elif p in ("deepseek-nvidia","deepseek_nvidia","nim"):
        order = ["deepseek_nvidia","openai","gemini","deepseek"]
    elif p in ("openai","oai"):
        order = ["openai","gemini","deepseek","deepseek_nvidia"]
    elif p in ("gemini","google","googleai"):
        order = ["gemini","openai","deepseek","deepseek_nvidia"]
    elif p in ("deepseek","ds"):
        order = ["deepseek","openai","gemini","deepseek_nvidia"]
    else:
        order = ["deepseek_nvidia","openai","gemini","deepseek"]
    last = None
    for cand in order:
        try:
            if cand=="deepseek_nvidia": return DeepseekNvidiaClient(model=model)
            if cand=="openai": return OpenAIClient(model=model)
            if cand=="gemini": return GeminiClient(model=model)
            if cand=="deepseek": return DeepseekClient(model=model)
        except Exception as e:
            log.warning("Provider 尝试失败：%s (%s)", cand, e)
            last = e
    raise RuntimeError(f"无法创建任何 LLM 客户端：{last}")

# ============================
# 一次性 User Prompt（多 INPUT，要求返回 JSON 数组）
# ============================
def build_batch_user_prompt(rows: List[Dict[str, Any]]) -> str:
    lines = []
    lines.append("共有以下多条 INPUT，请严格为每条 INPUT 输出一条 JSON，并以 JSON 数组返回（只输出 JSON 数组）：")
    for r in rows:
        lines.append("<INPUT>")
        lines.append(f"ticker: {(r.get('ticker') or '').upper()}")
        lines.append(f"time_et: {r.get('time_et','')}")
        lines.append(f"source: {r.get('source','')}")
        headline = re.sub(r"\s+"," ", (r.get('headline') or "").strip())[:300]
        summary  = re.sub(r"\s+"," ", (r.get('summary') or "").strip())[:700]
        lines.append(f"headline: {headline}")
        lines.append(f"summary: {summary}")
        lines.append(f"context: {r.get('query','')}")
        lines.append("</INPUT>")
    return "\n".join(lines)

def extract_json_array(text: str) -> Optional[List[Dict[str, Any]]]:
    if not text: return None
    s = text.strip()
    if "```json" in s:
        try:
            s = s.split("```json",1)[1].split("```",1)[0].strip()
        except Exception:
            pass
    elif s.startswith("```") and s.endswith("```"):
        s = s[3:-3].strip()
    start = s.find("[")
    end   = s.rfind("]")
    if start >= 0 and end > start:
        frag = s[start:end+1]
        try:
            data = json.loads(frag)
            if isinstance(data, list):
                return data
        except Exception:
            pass
    # 退化：逐对象抓取
    objs = re.findall(r"\{.*?\}", s, flags=re.S)
    if objs:
        out = []
        for o in objs:
            try:
                out.append(json.loads(o))
            except Exception:
                continue
        if out:
            return out
    return None

# ============================
# 主流程
# ============================
def main():
    out_dir, logs_dir, cache_dir = ensure_dirs()
    auto_out_csv, auto_raw_csv, auto_log_file, _ = default_paths_for_today()

    ap = argparse.ArgumentParser(description="从 config.yml 读取 tickers → 少量新闻 → 一次性 LLM → 输出情绪（CSV 最小字段，JSON 过滤 neutral）")
    ap.add_argument("--limit", type=int, default=None, help="每只最多抓取条数；若未提供，优先用 config.yml 的 news.per_ticker_limit，最后默认 3")
    ap.add_argument("--days", type=int, default=None, help="Google News when:Nd；若未提供，优先用 config.yml 的 news.days，最后默认 1")
    ap.add_argument("--provider", type=str, default=None, help="LLM 提供商（auto/openai/gemini/deepseek/deepseek-nvidia）")
    ap.add_argument("--model", type=str, default=None, help="LLM 模型名")
    ap.add_argument("--max-rows", type=int, default=None, help="送入 LLM 的最大行数（保护 token）")
    ap.add_argument("--out-csv", type=str, default=None, help="结果 CSV（默认自动）")
    ap.add_argument("--raw-csv", type=str, default=None, help="原始新闻 CSV（默认自动）")
    # JSON 过滤 neutral
    ap.add_argument("--json-drop-neutral", action="store_true", default=True, help="最终 JSON 是否过滤 neutral（默认 是）")
    ap.add_argument("--log-level", type=str, default="INFO")
    ap.add_argument("--log-file", type=str, default=None)
    ap.add_argument("--log-json", action="store_true")
    # 可选：直接指定要抓取的 tickers（逗号分隔或多次传递）
    ap.add_argument("--tickers", type=str, default=None, help="仅抓取这些tickers（逗号分隔），若未提供则从 config.yml 递归提取")
    args = ap.parse_args()

    if not args.log_file:
        args.log_file = str(auto_log_file)
    setup_logging(args.log_level, args.log_file, args.log_json)

    log.info("=== 启动 googleNews.py ===")

    # 1) 配置与参数合并
    news_cfg = load_news_settings_from_config()
    per_limit = int(args.limit or news_cfg.get("per_ticker_limit", 3))
    days = int(args.days or news_cfg.get("days", 1))
    lookback_hours = int(news_cfg.get("lookback_hours", 24))

    if not args.out_csv:
        args.out_csv = str(auto_out_csv)
        log.info("Auto: 未提供 --out-csv，自动设为：%s", args.out_csv)
    if not args.raw_csv:
        args.raw_csv = str(auto_raw_csv)

    # 2) 从 config.yml 读取 tickers（递归提取），或使用 --tickers 覆盖
    if args.tickers:
        # 支持逗号分隔
        tickers = [t.strip().upper() for t in re.split(r"[,\s]+", args.tickers) if t.strip()]
    else:
        tickers = load_tickers_from_config()
    if not tickers:
        log.error("未能从 config.yml 提取任何 ticker；退出。")
        sys.exit(2)
    log.info("Tickers: %s", ",".join(tickers))

    # 3) 抓取新闻（每只 limit 条；默认 3）
    rows = fetch_all_tickers(tickers, per_limit=per_limit, days=days, delay=0.4)

    # 4) 时间窗口严格过滤（默认近 lookback_hours 小时）
    end_dt = datetime.now(tz=ET)
    start_dt = end_dt - timedelta(hours=lookback_hours)
    rows = filter_by_window(rows, start_dt, end_dt)

    # 5) 落原始 CSV（不过滤）
    write_raw_csv(Path(args.raw_csv), rows)

    if not rows:
        log.warning("无新闻可供分析。")
        # 即使为空，也生成一个空的最小 CSV，避免后续任务找不到文件
        write_sentiment_csv_minimal(Path(args.out_csv), [])
        return

    # 若设置 max_rows，保护 token
    if args.max_rows and len(rows) > args.max_rows:
        log.warning("rows=%d 超过 max_rows=%d，将截断。", len(rows), args.max_rows)
        rows = rows[:args.max_rows]

    # 6) 一次性调用 LLM
    client = build_client(args.provider, args.model)
    user_prompt = build_batch_user_prompt(rows)
    log.info("LLM: 开始一次性调用（rows=%d, per_ticker_limit=%d, days=%d, lookback=%dh）",
             len(rows), per_limit, days, lookback_hours)

    t0 = time.perf_counter()
    try:
        out_text = client.complete(SYSTEM_PROMPT_NEWS, user_prompt)
    except Exception as e:
        log.exception("LLM 调用失败：%s", e)
        write_sentiment_csv_minimal(Path(args.out_csv), [])
        return
    dur = time.perf_counter() - t0
    log.info("LLM: 完成，一次性耗时 %.2fs，返回长度=%d", dur, len(out_text or ""))

    # 7) 解析 JSON 数组并与原行对齐
    data = extract_json_array(out_text or "")
    if not data:
        log.error("解析 JSON 数组失败，原文前 600 字：%s", (out_text or "")[:600])
        dbg = Path(args.out_csv).with_suffix(".llm_raw.txt")
        with open(dbg, "w", encoding="utf-8") as f:
            f.write(out_text or "")
        write_sentiment_csv_minimal(Path(args.out_csv), [])
        return

    # 8) 组装最小字段列表（CSV 用）+ 生成 JSON（按需过滤 neutral）
    csv_rows: List[Dict[str, Any]] = []
    json_rows: List[Dict[str, Any]] = []
    n = min(len(rows), len(data))
    for i in range(n):
        jd = data[i] if isinstance(data[i], dict) else {}
        direction = jd.get("direction", "")

        # 过滤掉 neutral（CSV 和 JSON 都过滤）
        if direction == "neutral":
            continue

        # 最小 CSV 行
        csv_rows.append({
            "ticker": (jd.get("ticker") or (rows[i].get("ticker") or "")).upper(),
            "event_type": jd.get("event_type",""),
            "direction": direction,
            "strength_0to3": jd.get("strength_0to3",""),
            "forward_looking": jd.get("forward_looking",""),
            "key_reasons": jd.get("key_reasons", []),
            "signal_quality": jd.get("signal_quality",""),
        })
        # JSON（也过滤 neutral）
        if not args.json_drop_neutral or direction != "neutral":
            json_rows.append(jd)

    # 9) 写 CSV（仅最小字段）
    out_csv_path = Path(args.out_csv)
    write_sentiment_csv_minimal(out_csv_path, csv_rows)

    # 10) 写 JSON（默认过滤 neutral）
    json_path = out_csv_path.with_suffix(".json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(json_rows, f, ensure_ascii=False, indent=2)
    log.info("JSON: 写出 %d 条（%s 过滤 neutral）→ %s",
             len(json_rows), "已" if args.json_drop_neutral else "未", json_path)

    log.info("=== 完成 === 共输入新闻: %d | JSON解析: %d | 输出 CSV: %s", len(rows), len(data), args.out_csv)

if __name__ == "__main__":
    main()
