# llm_trader/prompt_builder_universal.py
from __future__ import annotations
from typing import Dict, Any, Optional, Tuple
import json
import math
from pathlib import Path

# V2.1: Import commission utilities for cost awareness
from llm_trader.utils.commission import estimate_round_trip_commission, commission_as_percentage

# V2.7: Import news reader for fundamental analysis
from llm_trader.utils.news_reader import get_today_news, format_news_for_prompt, format_news_compact

# ---- 统一 JSON Schema（所有 LLM 输出必须匹配） -------------------------------
TRADE_JSON_SCHEMA = {
    "name": "trade_decision",
    "schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["BUY", "SELL", "HOLD"]},
            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "shares": {"type": ["integer", "null"], "minimum": 0},
            "rationale": {"type": "string"},
            "hard_gate": {
                "type": ["string", "null"],
                "enum": ["NO_NEW_POSITION", "FLATTEN_EOD", "DAILY_DRAWDOWN_LIMIT", "DAILY_PROFIT_LIMIT", None]
            }
        },
        "required": ["action", "confidence", "shares", "rationale", "hard_gate"],
        "additionalProperties": False
    },
    "strict": True
}

def _short_bars(bars, keep: int) -> list:
    if not bars:
        return []
    rows = bars[-keep:]
    out = []
    for b in rows:
        out.append({
            k: b[k] for k in ("ts", "open", "high", "low", "close", "volume") if k in b
        })
    return out

def _bars_to_csv(bars, keep: int) -> str:
    """Convert bars to compact CSV format to reduce token usage

    Example output: "ts,o,h,l,c,v\n1729598619000,258.52,258.53,258.52,258.52,2200\n..."

    Token savings: ~53% per bar
    - Before: {"ts": 1729598619000, "open": 258.52, ...} (~107 chars)
    - After: 1729598619000,258.52,258.53,258.52,258.52,2200 (~50 chars)
    """
    if not bars:
        return ""
    rows = bars[-keep:]

    # CSV header with short column names
    lines = ["ts,o,h,l,c,v"]

    for b in rows:
        # Format: timestamp,open,high,low,close,volume
        line = f"{b.get('ts', 0)},{b.get('open', 0):.2f},{b.get('high', 0):.2f},{b.get('low', 0):.2f},{b.get('close', 0):.2f},{b.get('volume', 0)}"
        lines.append(line)

    return "\n".join(lines)

def _compact_mtf(mtf_snapshot: Dict[str, Any], use_csv_encoding: bool = True) -> Dict[str, Any]:
    """Compact multi-timeframe snapshot with optional CSV encoding

    Args:
        mtf_snapshot: Raw multi-timeframe data
        use_csv_encoding: If True, encode 1s/5s bars as CSV (saves ~50% tokens)

    Returns:
        Compacted data structure
    """
    # 控制 token：优化后的 bar 数量（减少高频数据以节省 token）
    # 1s: 30s 数据足够捕捉微观结构
    # 5s: 2.5min 数据足够捕捉短期动量
    keep_map = {
        "1s": 30, "5s": 30, "1m": 40, "5m": 24, "15m": 16, "30m": 12, "1h": 12
    }

    # High-frequency timeframes benefit most from CSV encoding
    csv_timeframes = {"1s", "5s"}

    out = {}
    for tf, pack in mtf_snapshot.items():
        bars = pack.get("bars") or []
        feats = pack.get("features") or {}
        keep = keep_map.get(tf, 12)

        # Use CSV for high-frequency data to save tokens
        if use_csv_encoding and tf in csv_timeframes:
            out[tf] = {
                "bars_csv": _bars_to_csv(bars, keep),
                "features": feats
            }
        else:
            out[tf] = {
                "bars": _short_bars(bars, keep),
                "features": feats
            }
    return out

def _truncate(text: str, max_chars: int) -> str:
    return text if len(text) <= max_chars else text[:max_chars] + "...(trunc)"

class UniversalPromptBuilder:
    """
    一套 Prompt，适配多家 LLM：
    - OpenAI（含 DeepSeek-NIM/OpenAI 兼容端点）：支持 response_format json_schema 或 json_object
    - DeepSeek Official：多数兼容 OpenAI，若不支持 json_schema，回退"只输出 JSON"的硬约束
    - Gemini：使用 generation_config.response_mime_type = application/json
    - Claude（Anthropic）：没有原生 json_schema，就用"必须输出 JSON"+ 严格格式说明

    产出：
      - messages: List[dict]  (system + user)
      - provider_kwargs: Dict[str, Any]  （各家专用的 response_format / generation_config 等）
    """
    def __init__(
        self,
        json_mode: str = "schema",   # "schema" | "object" | "none"
        style: str = "reasoning",    # "reasoning" | "concise"
        max_user_json_chars: int = 15000,  # 控制 data_pack JSON 体积
        prompt_id: Optional[str] = None,  # 外部 Prompt ID (从白名单库加载)
        prompt_text: Optional[str] = None,  # 外部 Prompt 文本 (直接覆盖)
    ):
        self.json_mode = json_mode
        self.style = style
        self.max_user_json_chars = max_user_json_chars
        self.prompt_id = prompt_id
        self.prompt_text = prompt_text
        self._library = self._load_prompt_library()

    def _load_prompt_library(self) -> Dict[str, str]:
        """Load prompt library from prompts/prompt_library.json (whitelist)

        Returns:
            Dictionary mapping prompt_id -> prompt_text
        """
        try:
            lib_path = Path("prompts/prompt_library.json")
            if lib_path.exists():
                return json.loads(lib_path.read_text("utf-8"))
            return {}
        except Exception:
            return {}

    @staticmethod
    def _check_missing_frames(mtf_snapshot: Dict, min_requirements: Dict[str, int]) -> list:
        """检测哪些周期的样本数不足

        Args:
            mtf_snapshot: 多周期快照 from aggregator.get_snapshot()
            min_requirements: {"1s": 30, "5s": 12, "1m": 10, "5m": 3, "15m": 1}

        Returns:
            缺数的周期列表, 如 ["1m(5/10)", "5m(1/3)"]
        """
        missing = []
        for tf, min_bars in min_requirements.items():
            actual = len(mtf_snapshot.get(tf, {}).get("bars", []))
            if actual < min_bars:
                missing.append(f"{tf}({actual}/{min_bars})")
        return missing

    # ----------- 主构建函数 ----------------------------------------------------
    def build(
        self,
        *,
        provider: str,                       # "openai" | "anthropic" | "gemini" | "deepseek_official" | "deepseek_nvidia"
        symbol: str,
        mtf_snapshot: Dict[str, Any],
        portfolio: Dict[str, Any],
        time_ctx: Dict[str, Any],
        cash_limit: float,
        allow_new_position: bool,
        vix_value: Optional[float] = None,
        higher_tf_context: Optional[Dict[str, Any]] = None,
        sizing_cap_pct: float = 0.10,        # 单笔不超过 cash_limit 的比例
        forbid_short: bool = True,           # 当前模式仅做多：SELL 仅指减仓/清仓
        language: str = "en",                # "en" | "zh"
        # V2.5: 缺数降级参数
        min_bars_requirements: Optional[Dict[str, int]] = None,
        enable_degrade: bool = True,
    ) -> Tuple[list, dict, dict]:
        # V2.5: 缺数检测
        if min_bars_requirements is None:
            min_bars_requirements = {
                "1s": 30,   # 至少 30 秒
                "5s": 12,   # 至少 1 分钟
                "1m": 10,   # 至少 10 分钟
                "5m": 3,    # 至少 15 分钟
                "15m": 1,   # 至少 1 根
            }

        missing_frames = []
        if enable_degrade:
            missing_frames = self._check_missing_frames(mtf_snapshot, min_bars_requirements)

        # 1) 压缩多周期数据
        mtf_view = _compact_mtf(mtf_snapshot)
        higher_view = higher_tf_context or {}

        # V2.7: 获取今日新闻
        news_items = get_today_news(symbol)
        news_text = format_news_for_prompt(news_items, language=language)

        # V2.8: 每日 PnL 风控计算
        daily_pnl = portfolio.get("daily_pnl", 0.0)
        total_equity = portfolio.get("equity", portfolio.get("cash", 1.0))

        # 避免除以零
        if total_equity <= 0:
            daily_pnl_pct = 0.0
        else:
            daily_pnl_pct = daily_pnl / total_equity

        # 风控规则: 日亏损 1% 停止, 日盈利 1% 停止
        daily_loss_limit_pct = -0.01
        daily_profit_limit_pct = 0.01

        # 检查是否触及门控
        gate_hit_loss_limit = daily_pnl_pct <= daily_loss_limit_pct
        gate_hit_profit_limit = daily_pnl_pct >= daily_profit_limit_pct

        # 2) 组装信息包（给 LLM 消费）
        data_pack = {
            "symbol": symbol,
            "time": time_ctx,
            "market": {"vix": vix_value},
            "news": news_text,  # V2.7: 新闻分析
            "portfolio": portfolio,  # {position_size, avg_cost, cash, equity, pnl}
            "higher_tf": higher_view,  # 移到 mtf 之前，避免被截断
            "mtf": mtf_view,
            "constraints": {
                "cash_limit": cash_limit,
                "max_single_order_ratio": sizing_cap_pct,
                "no_new_position": (not allow_new_position),
                "forbid_short": forbid_short,
                # V2.8: 每日 PnL 风控状态
                "daily_pnl": daily_pnl,
                "daily_pnl_pct": round(daily_pnl_pct, 6),
                "daily_loss_limit_pct": daily_loss_limit_pct,
                "daily_profit_limit_pct": daily_profit_limit_pct,
                "gate_hit_loss_limit": gate_hit_loss_limit,
                "gate_hit_profit_limit": gate_hit_profit_limit
            },
            # V2.5: 数据质量信息
            "data_quality": {
                "missing_frames": missing_frames,
                "has_critical_missing": any(tf.startswith(("1m", "5m")) for tf in missing_frames),
                "min_requirements": min_bars_requirements,
            }
        }
        data_json = json.dumps(data_pack, ensure_ascii=False)
        data_json = _truncate(data_json, self.max_user_json_chars)

        # 3) 语言/措辞
        if language == "zh":
            instr_header = (
                "你是一个日内交易决策智能体。先在心里逐步推理，但**只输出 JSON**。\n"
                "**务必遵守硬规则:**\n"
                "1. 若 `constraints.gate_hit_loss_limit=true` (日亏损达1%): 必须 **SELL** (清仓) 或 **HOLD** (空仓)，停止开新仓。设置 `hard_gate=\"DAILY_DRAWDOWN_LIMIT\"`。\n"
                "2. 若 `constraints.gate_hit_profit_limit=true` (日盈利达1%): 必须 **SELL** (清仓) 或 **HOLD** (空仓)，停止开新仓。设置 `hard_gate=\"DAILY_PROFIT_LIMIT\"`。\n"
                "3. 若 `constraints.no_new_position=true` 且当前无仓，则必须 HOLD。\n"
                "4. 若 `time.should_flatten=true` 且持有多头，则必须 SELL 至空仓。\n\n"
                "⚠️ 数据格式说明：高频数据(1s/5s)使用 CSV 格式以减少 token 消耗。\n"
                "格式: `bars_csv` 字段包含 CSV 文本，首行为列名 (ts,o,h,l,c,v)，后续每行为一个 bar。\n"
                "列说明: ts=时间戳(毫秒), o=开盘价, h=最高价, l=最低价, c=收盘价, v=成交量\n\n"
                "📰 新闻分析: `news` 字段包含今日相关新闻，包括事件类型、方向(bullish/bearish)、强度评级(0-3星)、"
                "关键原因和信号质量。结合新闻情绪与技术面综合判断。"
            )
            # Extract portfolio constraints
            max_pos_size = portfolio.get("max_position_size", cash_limit * 0.90)
            avail_bp = portfolio.get("available_buying_power", portfolio.get("cash", 0))
            cash = portfolio.get("cash", 0)
            total_commission = portfolio.get("total_commission_paid", 0)

            # V2.1: 计算手续费示例
            current_price = portfolio.get("avg_cost", 100)
            example_qty = int(max_pos_size / current_price) if current_price > 0 else 100
            example_commission = estimate_round_trip_commission(current_price, example_qty)
            commission_pct = commission_as_percentage(current_price, example_qty)

            sizing_rules = (
                "**仓位与风控规则:**\n"
                f"- 硬性上限: ${max_pos_size:,.2f} (权益的90%)\n"
                f"- 可用购买力: ${avail_bp:,.2f}\n"
                f"- 账户现金: ${cash:,.2f}\n"
                f"- 累计手续费: ${total_commission:.2f}\n"
                "\n"
                "**交易成本(重要):**\n"
                "- 手续费: 万分之一/笔(买入或卖出), 最低$1\n"
                f"- 示例: {example_qty}股 @ ${current_price:.2f} = 往返${example_commission:.2f} ({commission_pct:.2f}%)\n"
                "- ⚠️ 你必须考虑手续费成本\n"
                "- 小单(<$10k)最低收费$1×2=$2往返\n"
                "- 盈亏平衡需要价格变动 > 手续费成本\n"
                "\n"
                "**风控(你的责任):**\n"
                "- 考虑仓位集中度风险\n"
                "- 考虑市场波动与流动性\n"
                "- 考虑小单/高频的手续费成本\n"
                "- 建议: 单笔10-30%,平衡分散与手续费效率\n"
                "- 当前模式不做空：SELL 仅表示减仓/清仓\n"
            )
            # V2.5: 缺数警告
            if missing_frames:
                sizing_rules += (
                    f"\n⚠️ **数据不完整警告**: {', '.join(missing_frames)}\n"
                    "→ 硬规则: 数据不足时禁止开新仓 (BUY)。\n"
                    "→ 只允许: HOLD (观望) 或 SELL (平仓已有仓位)\n"
                )
            task = (
                "给定结构化的多周期数据（1s~1h）与 1h/1d 背景、今日新闻、仓位与会话时间，"
                "结合技术面与基本面(新闻情绪)判断短期/中期方向与波动风险，输出 BUY/SELL/HOLD、confidence、shares、rationale。"
            )
            fmt_rules = (
                "严格以**单个 JSON 对象**输出，字段如下：\n"
                "{\n"
                '  "action": "BUY" | "SELL" | "HOLD",\n'
                "  \"confidence\": 0.0-1.0,\n"
                "  \"shares\": <int|null>,    // BUY/SELL 时必须 >0, HOLD 时为 null\n"
                "  \"rationale\": <简短一句话>,\n"
                '  "hard_gate": "NO_NEW_POSITION"|"FLATTEN_EOD"|null\n'
                "}\n"
                "⚠️ 重要：如果 action 是 BUY 或 SELL，必须计算并提供 shares > 0。\n"
                "若 `time.should_flatten=true` 且有仓：action 必须为 SELL，hard_gate=FLATTEN_EOD；\n"
                "若 `constraints.no_new_position=true` 且无仓：action 必须为 HOLD，hard_gate=NO_NEW_POSITION。"
            )
        else:
            # ChatGPT-optimized system prompt (V2.1)
            instr_header = (
                "You are the bot's **intraday portfolio & risk manager**.\n"
                "Your goal is **risk-adjusted P&L** for today, not trade count. Favor *fewer, higher-quality* trades.\n"
                "Use multi-timeframe confluence (1s/5s microstructure + 1m/5m short-term + 15m/1h context) and **New York session timing**.\n"
                "\n"
                "**Respect hard gates from `time` AND `constraints`:**\n"
                "• If `constraints.gate_hit_loss_limit=true` (daily loss ≥1%): If `portfolio.position_size>0`, **SELL to flat**. If `portfolio.position_size==0`, **HOLD**. Set `hard_gate=\"DAILY_DRAWDOWN_LIMIT\"`.\n"
                "• If `constraints.gate_hit_profit_limit=true` (daily profit ≥1%): If `portfolio.position_size>0`, **SELL to flat**. If `portfolio.position_size==0`, **HOLD**. Set `hard_gate=\"DAILY_PROFIT_LIMIT\"`.\n"
                "• If `time.should_prevent_new_positions=true` and `portfolio.position_size==0` → **HOLD** (no new longs).\n"
                "• If `time.should_flatten=true` and `portfolio.position_size>0` → **SELL to flat** and set `hard_gate=\"FLATTEN_EOD\"`.\n"
                "\n"
                "When data is incomplete (`data_quality.missing_frames`), or edge is weak, **HOLD** is preferred.\n"
                "Choose your own risk method (ATR/VWAP/structure/vol-targeting/Kelly-lite); **don't print your reasoning**, "
                "but reflect the plan concisely in `rationale`.\n"
                "\n"
                "Output **JSON only** matching the required schema.\n"
                "\n"
                "⚠️ DATA FORMAT: High-frequency data (1s/5s) uses CSV format to reduce token usage.\n"
                "Format: `bars_csv` field contains CSV text. First line = column names (ts,o,h,l,c,v), "
                "subsequent lines = bar data.\n"
                "Columns: ts=timestamp(ms), o=open, h=high, l=low, c=close, v=volume\n"
                "\n"
                "📰 NEWS ANALYSIS: The `news` field contains today's relevant news including event type, direction (bullish/bearish), "
                "strength rating (0-3 stars), key reasons, and signal quality. Combine news sentiment with technical analysis for decisions."
            )
            # Extract portfolio constraints
            max_pos_size = portfolio.get("max_position_size", cash_limit * 0.90)
            avail_bp = portfolio.get("available_buying_power", portfolio.get("cash", 0))
            cash = portfolio.get("cash", 0)
            total_commission = portfolio.get("total_commission_paid", 0)

            # V2.1: Calculate commission impact for example trade
            current_price = portfolio.get("avg_cost", 100)  # Use avg_cost or fallback
            example_qty = int(max_pos_size / current_price) if current_price > 0 else 100
            example_commission = estimate_round_trip_commission(current_price, example_qty)
            commission_pct = commission_as_percentage(current_price, example_qty)

            sizing_rules = (
                "**Position Sizing & Risk Control:**\n"
                f"- Hard limit: ${max_pos_size:,.2f} (90% of equity)\n"
                f"- Available buying power: ${avail_bp:,.2f}\n"
                f"- Cash on hand: ${cash:,.2f}\n"
                f"- Total commission paid (lifetime): ${total_commission:.2f}\n"
                "\n"
                "**Trading Costs (IMPORTANT):**\n"
                "- Commission: 0.01% per trade (buy or sell), minimum $1\n"
                f"- Example: {example_qty} shares @ ${current_price:.2f} = ${example_commission:.2f} round-trip ({commission_pct:.2f}% of trade value)\n"
                "- ⚠️ YOU must account for commission when sizing positions\n"
                "- Small trades (<$10k) pay minimum $1 each way = $2 total\n"
                "- Break-even requires price move > commission cost\n"
                "\n"
                "**Risk Management (YOUR responsibility):**\n"
                "- Consider position concentration risk\n"
                "- Consider volatility and market conditions\n"
                "- Factor in commission costs for small/frequent trades\n"
                "- Suggested: 10-30% per position for balance of diversification vs commission efficiency\n"
                "- No shorting: SELL only reduces/exits existing longs\n"
            )
            # V2.5: 缺数警告
            if missing_frames:
                sizing_rules += (
                    f"\n⚠️ **DATA INCOMPLETE**: {', '.join(missing_frames)}\n"
                    "→ HARD RULE: Insufficient data - DO NOT open new positions (BUY).\n"
                    "→ ONLY allowed: HOLD (wait) or SELL (close existing positions)\n"
                )
            # ChatGPT-optimized task description (V2.1)
            task = (
                "**Objective:**\n"
                "Maximize risk-adjusted return *today* while avoiding large drawdowns. "
                "Prefer quality over frequency. You control: entry/exit choice (BUY/SELL/HOLD) and **position size** via `shares`. "
                "Encode stop/TP/time-stop/scale rules succinctly inside `rationale`.\n"
                "\n"
                "**Time awareness (New York):**\n"
                "Use `time` to drive session discipline:\n"
                "• `time.ny_iso` (NY timestamp), `time.session_phase` (OPEN/MIDDAY/AFTERNOON/CLOSE),\n"
                "• `time.minutes_to_close`, `time.api_interval_sec`, `time.decisions_left`,\n"
                "• `time.should_prevent_new_positions` (late-day), `time.should_flatten` (EOD flatten).\n"
                "  If `decisions_left` is small, favor exit/defense and avoid new risks.\n"
                "\n"
                "**Data pack (compact):**\n"
                "• `portfolio`: equity, cash, current `position_size/avg_cost/pnl` (this symbol), commission paid, rolling win-rate/avgR/DD if present.\n"
                "• `market`: VIX (if present).\n"
                "• `news`: Today's relevant news with sentiment (bullish/bearish), strength (0-3★), event type, key reasons, and quality rating.\n"
                "• `mtf`: 1s/5s **CSV bars** (ts,o,h,l,c,v) with microstructure features (spread bps, uptick ratio, rvol) when present, "
                "1m/5m/15m/1h **features** (VWAP dev bps, ATR, EMA slopes, RSI, z-scores, regime flags).\n"
                "• `higher_tf`: (yesterday's 1h/1d context if present).\n"
                "• `constraints`: `cash_limit`, `max_single_order_ratio`, `no_new_position`, `forbid_short` (no shorting).\n"
                "• `data_quality`: `missing_frames`, `has_critical_missing`.\n"
                "\n"
                "**Make a decision & a plan:**\n"
                "1. Diagnose **regime** (trend/chop) & **volatility** (calm/normal/elevated) from the pack.\n"
                "2. If edge is strong and gates allow, decide **BUY** or **SELL**; else **HOLD**.\n"
                "3. If BUY/SELL, set `shares` > 0, consistent with: available cash, `cash_limit`, `max_single_order_ratio`, "
                "commission costs, and your **chosen risk method** (you pick).\n"
                "4. Encode a concise lifecycle in `rationale`: entry logic, **stop**, **take-profit**, **time-stop**, and scale-in/out cues (one sentence).\n"
                "5. If `time.should_flatten=true` and `position_size>0`, you **must** SELL to flat with `hard_gate=\"FLATTEN_EOD\"`.\n"
                "6. If `constraints.no_new_position=true` or `time.should_prevent_new_positions=true` and `position_size==0`, "
                "you **must** HOLD with `hard_gate=\"NO_NEW_POSITION\"`."
            )
            fmt_rules = (
                "\n**Return JSON only** with:\n"
                "```json\n"
                "{\n"
                '  "action": "BUY | SELL | HOLD",\n'
                '  "confidence": 0.0-1.0,\n'
                '  "shares": <int|null>,     // >0 for BUY/SELL, null for HOLD\n'
                '  "rationale": "<one short sentence that also states stop/take-profit/time-stop/scale plan>",\n'
                '  "hard_gate": "NO_NEW_POSITION" | "FLATTEN_EOD" | null\n'
                "}\n"
                "```\n"
                "*(Your reasoning stays internal; output only the JSON object above.)*"
            )

        # 4) system / user
        system = instr_header
        if self.style == "reasoning":
            system += " Do all reasoning internally and never print it."

        # Apply prompt overrides if present
        # Priority: 1) prompt_text (direct override) > 2) prompt_id (from library) > 3) default
        if self.prompt_text:
            system = self.prompt_text
        elif self.prompt_id and self.prompt_id in self._library:
            system = self._library[self.prompt_id]

        user = (
            f"{task}\n\n"
            "Rules:\n"
            f"{sizing_rules}"
            f"{fmt_rules}\n\n"
            f"DATA:\n{data_json}"
        )

        # 5) 各 provider 的 json/输出约束参数
        provider_kwargs: Dict[str, Any] = {}
        p = provider.lower()

        if p in ("openai", "deepseek_nvidia"):
            # OpenAI & 兼容：优先 json_schema；若端点不支持，可退化到 json_object。
            if self.json_mode == "schema":
                provider_kwargs["response_format"] = {
                    "type": "json_schema",
                    "json_schema": TRADE_JSON_SCHEMA
                }
            elif self.json_mode == "object":
                provider_kwargs["response_format"] = {"type": "json_object"}
            # else: none -> rely on prompt

        elif p in ("deepseek_official", "deepseek"):
            # 多数 DeepSeek Official 端点兼容 OpenAI；若不支持 json_schema，保留纯提示约束
            if self.json_mode == "object":
                provider_kwargs["response_format"] = {"type": "json_object"}

        elif p == "gemini":
            # Gemini：使用 response_mime_type=application/json
            provider_kwargs["generation_config"] = {"response_mime_type": "application/json"}

        elif p in ("anthropic", "claude"):
            # Claude：没有 json_schema，可使用 tool schema（取决于你的 SDK）。
            # 这里统一用提示硬约束；你的 provider 里也可用 tools API 来强约束。
            pass

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user}
        ]

        # V2.5: 返回额外信息供主程序使用
        extra_info = {"missing_frames": missing_frames}

        return messages, provider_kwargs, extra_info

    # ----------- JSON 修复提示（当 LLM 返回非法 JSON 时二次追问） ---------------
    def repair_messages(self, bad_text: str, language: str = "en") -> Tuple[list, dict]:
        if language == "zh":
            repair_user = (
                "你上一次的输出不是合法 JSON 或没有匹配指定 schema。"
                "请**仅返回**一个 JSON 对象，不要包含任何多余文本、代码块或解释。"
                f"\n必须匹配如下 JSON Schema（简述）：{json.dumps(TRADE_JSON_SCHEMA['schema'], ensure_ascii=False)}"
                f"\n上次原文：\n{_truncate(bad_text, 4000)}"
            )
        else:
            repair_user = (
                "Your previous output was not valid JSON or did not match the required schema. "
                "Return ONLY a single JSON object, with no extra text or markdown. "
                f"\nSchema (summary): {json.dumps(TRADE_JSON_SCHEMA['schema'])}"
                f"\nPrevious raw text:\n{_truncate(bad_text, 4000)}"
            )
        messages = [{"role": "user", "content": repair_user}]
        return messages, {}
