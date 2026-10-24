# llm_trader/prompt_builder_compact.py
"""
Compact Two-Tier Prompt System for Token Optimization

Based on ChatGPT's design:
- Tier 1: High-frequency execution (15-30s, ~1.2-1.8k tokens)
- Tier 2: Low-frequency reflection (5-10min, ~3-4k tokens)

Unified I/O contract across all LLM providers.
"""

from __future__ import annotations
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime
import json
from pathlib import Path


# ============================================================================
# Unified JSON Schemas
# ============================================================================

EXECUTION_SCHEMA = {
    "name": "trade_execution",
    "schema": {
        "type": "object",
        "properties": {
            "a": {"type": "string", "enum": ["B", "S", "H"]},  # Buy/Sell/Hold
            "q": {"type": "integer", "minimum": 0},  # Quantity (shares)
            "conf": {"type": "number", "minimum": 0.0, "maximum": 1.0},  # Confidence
            "tp": {"type": "integer"},  # Take profit (bps)
            "sl": {"type": "integer"},  # Stop loss (bps)
            "tw_s": {"type": "integer", "minimum": 0},  # Time window (seconds)
            "px": {"type": ["number", "null"]}  # Suggested limit price for planned entry
        },
        "required": ["a", "q", "conf"],
        "additionalProperties": False
    },
    "strict": True
}

REFLECTION_SCHEMA = {
    "name": "strategy_reflection",
    "schema": {
        "type": "object",
        "properties": {
            "style": {"type": "string", "enum": ["scalp", "intra", "avoid"]},
            "size_curve": {"type": "string", "enum": ["conservative", "balanced", "aggressive"]},
            "hold_s_range": {
                "type": "array",
                "items": {"type": "integer"},
                "minItems": 2,
                "maxItems": 2
            },
            "open_filters": {"type": "array", "items": {"type": "string"}},
            "close_rules": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0}
        },
        "required": ["style", "size_curve", "confidence"],
        "additionalProperties": False
    },
    "strict": True
}


# ============================================================================
# Data Transformation Utilities
# ============================================================================

def to_dbps(price: float, base_price: float) -> int:
    """Convert price to delta basis points relative to base

    Args:
        price: Current price
        base_price: Reference price (e.g., day open, VWAP)

    Returns:
        Delta in basis points (rounded integer)
    """
    if base_price == 0:
        return 0
    return round(((price - base_price) / base_price) * 10000)


def to_volk(volume: float) -> int:
    """Convert volume to thousands (volk)

    Args:
        volume: Raw volume

    Returns:
        Volume in thousands (rounded integer)
    """
    return round(volume / 1000)


def format_sparse_series(bars: List[Dict[str, Any]], base_price: float,
                        now_ts_ms: int, tf_seconds: int) -> str:
    """Format bars as compact sparse CSV

    Args:
        bars: List of bar dicts with ts, close, volume
        base_price: Reference price for dbps calculation
        now_ts_ms: Current timestamp in milliseconds
        tf_seconds: Timeframe in seconds (1, 5, 60, etc.)

    Returns:
        Compact CSV string with relative time, dbps, volk
    """
    if not bars:
        return ""

    lines = []
    for bar in bars:
        ts_ms = bar.get('ts', 0)
        # Relative time in seconds (negative for past, 0 for latest)
        rel_t = (ts_ms - now_ts_ms) // 1000

        close = bar.get('close', base_price)
        dbps = to_dbps(close, base_price)

        volume = bar.get('volume', 0)
        volk = to_volk(volume)

        lines.append(f"{rel_t},{dbps},{volk}")

    return "\n".join(lines)


def format_features_compact(features: Dict[str, Any],
                           timeframe: str = "1m",
                           current_price: Optional[float] = None) -> str:
    """Format features as compact key-value string

    Args:
        features: Dict of feature values
        timeframe: Timeframe label (1m, 5m, 15m, etc.)

    Returns:
        Compact string like "FEAT_1m|rsi=48,e9=0,ex=-3,..."
    """
    if not features:
        return ""

    # Map full feature names to compact keys
    key_map = {
        'rsi_14': 'rsi',
        'ema_9': 'e9',
        'ema_21': 'e21',
        'ema_50': 'e50',
        'ema_cross': 'ex',
        # Encode VWAP deviation as basis points relative to current price
        'vwap_20': 'vdev_bps',
        'z_20': 'z20',
        'z_60': 'z60',
        'atr': 'atr',
        'volume_ratio': 'rvol',
        'spread_bps': 'sp',
        'tick_imbalance': 'ti',
        'slope_20': 'sl20',
        'r2': 'r2'
    }

    parts = []
    for full_key, compact_key in key_map.items():
        if full_key in features:
            value = features[full_key]
            if value is None:
                continue

            # Special case: convert VWAP to deviation bps if current price provided
            if full_key == 'vwap_20' and current_price is not None and isinstance(value, (int, float)) and value > 0:
                vdev_bps = int(round(((float(current_price) - float(value)) / float(value)) * 10000))
                formatted = str(vdev_bps)
            else:
                # Round to 2-3 decimals
                if isinstance(value, float):
                    if abs(value) < 1:
                        formatted = f"{value:.3f}".rstrip('0').rstrip('.')
                    else:
                        formatted = f"{value:.2f}".rstrip('0').rstrip('.')
                else:
                    formatted = str(value)

            parts.append(f"{compact_key}={formatted}")

    if not parts:
        return ""

    return f"FEAT_{timeframe}|" + ",".join(parts)


# ============================================================================
# Compact Prompt Builder
# ============================================================================

class CompactPromptBuilder:
    """Two-tier compact prompt system with unified I/O contract

    Usage:
        builder = CompactPromptBuilder(mode="execution")
        messages, kwargs = builder.build(
            provider="deepseek_nvidia",
            symbol="AAPL",
            mtf_snapshot=snapshot,
            portfolio=portfolio,
            time_ctx=time_ctx,
            ...
        )
    """

    def __init__(
        self,
        mode: str = "execution",  # "execution" or "reflection"
        adaptive_trim: bool = True,
        max_tokens_budget: int = 2000,
        prompt_id: Optional[str] = None,  # 外部 Prompt ID (从白名单库加载)
        prompt_text: Optional[str] = None,  # 外部 Prompt 文本 (直接覆盖)
    ):
        """Initialize compact prompt builder

        Args:
            mode: "execution" (high-freq) or "reflection" (low-freq)
            adaptive_trim: Enable automatic trimming when near token limit
            max_tokens_budget: Target token budget for prompt
            prompt_id: External prompt ID (loaded from whitelist library)
            prompt_text: External prompt text (direct override)
        """
        self.mode = mode
        self.adaptive_trim = adaptive_trim
        self.max_tokens_budget = max_tokens_budget
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

    def build_execution_prompt(
        self,
        *,
        provider: str,
        symbol: str,
        mtf_snapshot: Dict[str, Any],
        portfolio: Dict[str, Any],
        time_ctx: Dict[str, Any],
        market_ctx: Optional[Dict[str, Any]] = None,
        trade_memory: Optional[Dict[str, Any]] = None,
        base_price: Optional[float] = None,
        language: str = "en"
    ) -> Tuple[List[dict], dict]:
        """Build high-frequency execution prompt (~1.2-1.8k tokens)

        Returns:
            (messages, provider_kwargs)
        """
        # 1. META
        now_epoch = int(datetime.now().timestamp())
        phase = time_ctx.get('phase', 'RTH')
        allow_new = 1 if time_ctx.get('allow_new_position', True) else 0
        flatten_now = 1 if time_ctx.get('should_flatten', False) else 0
        minutes_to_close = int(time_ctx.get('minutes_to_close', 999))
        forbid_new_before_min = int(time_ctx.get('forbid_new_positions_before_min', 5))
        cash_cap = int(portfolio.get('cash', 0))

        meta = (
            f"META|now_utc={now_epoch}, phase={phase}, allow_new={allow_new}, "
            f"flatten_now={flatten_now}, min_to_close={minutes_to_close}, cash_cap={cash_cap}"
        )

        # 2. POSITION
        position_size = portfolio.get('position_size', 0)
        avg_cost = portfolio.get('avg_cost', 0.0)
        current_price = base_price or portfolio.get('current_price', avg_cost)

        if position_size > 0:
            pnl_pct = ((current_price - avg_cost) / avg_cost * 100) if avg_cost > 0 else 0
            avail = portfolio.get('cash', 0)
            pos = f"POS|{symbol}|sh={position_size},cost={avg_cost:.2f},val={current_price:.2f},pnl_pct={pnl_pct:+.2f},avail={int(avail)}"
        else:
            avail = portfolio.get('cash', 0)
            pos = f"POS|{symbol}|sh=0,cost=0,val={current_price:.2f},pnl_pct=0.00,avail={int(avail)}"

        # 3. MARKET
        vix = market_ctx.get('vix', 0) if market_ctx else 0
        d1_atr_bps = market_ctx.get('d1_atr_bps', 0) if market_ctx else 0
        d1_trend = market_ctx.get('d1_trend', 'flat') if market_ctx else 'flat'

        # V2.7: Include sentiment if available
        sentiment_str = ""
        if market_ctx and 'sentiment_composite' in market_ctx:
            sc = market_ctx.get('sentiment_composite', 0.5)
            sl = market_ctx.get('sentiment_label', 'neutral')
            fs = market_ctx.get('fear_score', 0.5)
            fls = market_ctx.get('flow_score', 0.5)
            ns = market_ctx.get('narrative_score', 0.5)
            sentiment_str = f", sent={sc:.2f}({sl}), fear={fs:.2f}, flow={fls:.2f}, narr={ns:.2f}"

        mkt = f"MKT|vix={vix:.1f}, d1_atr_bps={d1_atr_bps}, d1_trend={d1_trend}{sentiment_str}"

        # 4. Multi-timeframe sparse series
        if base_price is None:
            # Use current price as base
            base_price = current_price

        now_ts_ms = int(datetime.now().timestamp() * 1000)

        series_parts = []
        for tf, tf_seconds in [("1s", 1), ("5s", 5), ("1m", 60), ("5m", 300), ("15m", 900)]:
            if tf in mtf_snapshot:
                tf_data = mtf_snapshot[tf]
                bars = tf_data.get('bars', [])
                if not bars:
                    # Try bars_csv
                    bars_csv = tf_data.get('bars_csv', '')
                    if bars_csv:
                        # Parse CSV back to bars (for now, skip to save complexity)
                        continue
                    continue

                csv_data = format_sparse_series(bars, base_price, now_ts_ms, tf_seconds)
                if csv_data:
                    series_parts.append(f"{tf}:t,dbps,volk\n{csv_data}")

        # 5. Features
        feat_parts = []
        for tf in ["1m", "5m", "15m"]:
            if tf in mtf_snapshot:
                features = mtf_snapshot[tf].get('features', {})
                feat_str = format_features_compact(features, tf, current_price)
                if feat_str:
                    feat_parts.append(feat_str)

        # 6. Memory (compressed)
        if trade_memory:
            n = trade_memory.get('total_trades', 0)
            win = trade_memory.get('winning_trades', 0)
            wr = int((win / n * 100) if n > 0 else 0)
            aw = trade_memory.get('avg_win_bps', 0)
            al = trade_memory.get('avg_loss_bps', 0)
            rr = trade_memory.get('risk_reward', 0.0)
            maxdd = trade_memory.get('max_drawdown_pct', 0.0)
            hold_med_s = trade_memory.get('median_hold_seconds', 0)
            last3 = trade_memory.get('last3', [])
            last3_str = ",".join([f"{x:+d}" for x in last3]) if last3 else "0,0,0"
            plan = trade_memory.get('current_plan', 'neutral')

            mem = f"MEM|n={n},win={win},wr={wr},aw={aw:+d},al={al:+d},rr={rr:.2f},maxdd={maxdd:-.1f},hold_med_s={hold_med_s},last3={last3_str},plan={plan}"
        else:
            mem = "MEM|n=0,win=0,wr=0,aw=0,al=0,rr=0.0,maxdd=0.0,hold_med_s=0,last3=0,plan=neutral"

        # 7. Constraints
        max_trade_value = int(portfolio.get('max_position_size', cash_cap * 0.1))
        min_conf = 0.60
        # Runtime-evaluated guardrails
        forbid_new_now = 1 if (not allow_new or phase == "CLOSE" or minutes_to_close <= forbid_new_before_min or flatten_now) else 0
        constraint = (
            f"CONSTRAINT|max_trade_value={max_trade_value},min_conf={min_conf:.2f},"
            f"forbid_new_now={forbid_new_now}"
        )

        # Assemble user message
        user_parts = [meta, pos, mkt] + series_parts + feat_parts + [mem, constraint]

        # Add explicit output format for R1 (doesn't support JSON schema enforcement)
        output_format = """# OUTPUT FORMAT (REQUIRED):
Return EXACTLY this JSON structure with no extra text:
{"a":"B"|"S"|"H", "q":integer, "conf":0.0-1.0}

Optional fields: "tp":int (bps), "sl":int (bps), "tw_s":int (seconds)

Example HOLD: {"a":"H","q":0,"conf":0.45}
Example BUY (planned): {"a":"B","q":15,"conf":0.72,"px":245.30,"tp":50,"sl":-30,"tw_s":900}  # limit @ $245.30 valid 15m"""

        user_parts.append(output_format)
        user_message = "\n\n".join(user_parts)

        # System message
        if provider.lower() in ("deepseek_nvidia", "deepseek_official", "deepseek"):
            # R1 strict mode
            system = (
                "You are a latency-aware INTRADAY-ONLY trading agent (day trading). "
                "Follow the INPUT CONTRACT strictly. Use ONLY provided data. "
                "Hard rules: if flatten_now=1 and holding >0 → SELL to flat; never BUY when forbid_new_now=1. "
                "Avoid chasing: if VWAP deviation (vdev_bps) ≥ 250, prefer HOLD/SELL unless clear pullback+reclaim. "
                "If a better entry is expected within minutes, propose a planned limit via px and tw_s (valid window). "
                "Output a single-line minified JSON matching the schema. No extra text."
            )
        else:
            system = (
                "You are a latency-aware INTRADAY-ONLY trading agent (day trading). "
                "Follow the INPUT CONTRACT strictly; use ONLY provided data. "
                "Hard rules: if flatten_now=1 and holding >0 → SELL to flat; never BUY when forbid_new_now=1. "
                "Risk: avoid chasing extended moves (VWAP deviation vdev_bps ≥ 250). Prefer pullback/retest entries. "
                "If a better entry is expected soon, propose a planned limit via px and tw_s; otherwise provide immediate plan. "
                "Respond ONLY with the OUTPUT JSON; if constraints not met or conf < min_conf, output HOLD with q=0."
            )

        # Apply prompt overrides if present
        # Priority: 1) prompt_text (direct override) > 2) prompt_id (from library) > 3) default
        if self.prompt_text:
            system = self.prompt_text
        elif self.prompt_id and self.prompt_id in self._library:
            system = self._library[self.prompt_id]

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_message}
        ]

        # Provider kwargs
        provider_kwargs = {}
        if provider.lower() in ("openai", "deepseek_nvidia", "deepseek"):
            provider_kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": EXECUTION_SCHEMA
            }
        elif provider.lower() == "gemini":
            provider_kwargs["generation_config"] = {"response_mime_type": "application/json"}

        return messages, provider_kwargs

    def build_reflection_prompt(
        self,
        *,
        provider: str,
        symbol: str,
        mtf_snapshot: Dict[str, Any],
        portfolio: Dict[str, Any],
        time_ctx: Dict[str, Any],
        market_ctx: Optional[Dict[str, Any]] = None,
        trade_memory: Optional[Dict[str, Any]] = None,
        language: str = "en"
    ) -> Tuple[List[dict], dict]:
        """Build low-frequency reflection prompt (~3-4k tokens)

        Returns:
            (messages, provider_kwargs)
        """
        # Similar to execution but with more features and expanded memory
        now_epoch = int(datetime.now().timestamp())
        phase = time_ctx.get('phase', 'RTH')
        cash_cap = int(portfolio.get('cash', 0))

        meta = f"META|now_utc={now_epoch}, phase={phase}, cash_cap={cash_cap}"

        # POSITION (same as execution)
        position_size = portfolio.get('position_size', 0)
        avg_cost = portfolio.get('avg_cost', 0.0)
        current_price = portfolio.get('current_price', avg_cost)

        if position_size > 0:
            pnl_pct = ((current_price - avg_cost) / avg_cost * 100) if avg_cost > 0 else 0
            avail = portfolio.get('cash', 0)
            pos = f"POS|{symbol}|sh={position_size},cost={avg_cost:.2f},val={current_price:.2f},pnl_pct={pnl_pct:+.2f},avail={int(avail)}"
        else:
            avail = portfolio.get('cash', 0)
            pos = f"POS|{symbol}|sh=0,cost=0,val={current_price:.2f},pnl_pct=0.00,avail={int(avail)}"

        # MARKET
        vix = market_ctx.get('vix', 0) if market_ctx else 0
        d1_atr_bps = market_ctx.get('d1_atr_bps', 0) if market_ctx else 0
        d1_trend = market_ctx.get('d1_trend', 'flat') if market_ctx else 'flat'
        mkt = f"MKT|vix={vix:.1f}, d1_atr_bps={d1_atr_bps}, d1_trend={d1_trend}"

        # Features (more comprehensive for reflection)
        feat_parts = []
        for tf in ["1m", "5m", "15m", "30m", "1h"]:
            if tf in mtf_snapshot:
                features = mtf_snapshot[tf].get('features', {})
                feat_str = format_features_compact(features, tf)
                if feat_str:
                    feat_parts.append(feat_str)

        # Expanded memory with last 20 trades bucketed
        if trade_memory:
            n = trade_memory.get('total_trades', 0)
            win = trade_memory.get('winning_trades', 0)
            wr = int((win / n * 100) if n > 0 else 0)
            aw = trade_memory.get('avg_win_bps', 0)
            al = trade_memory.get('avg_loss_bps', 0)
            rr = trade_memory.get('risk_reward', 0.0)
            maxdd = trade_memory.get('max_drawdown_pct', 0.0)
            hold_med_s = trade_memory.get('median_hold_seconds', 0)
            last20 = trade_memory.get('last20_bucket', {})
            plan = trade_memory.get('current_plan', 'neutral')

            mem = (f"MEM|n={n},win={win},wr={wr},aw={aw:+d},al={al:+d},rr={rr:.2f},"
                  f"maxdd={maxdd:-.1f},hold_med_s={hold_med_s},plan={plan}")

            if last20:
                mem += f"\nLAST20|wins={last20.get('wins', 0)},losses={last20.get('losses', 0)},avg_hold_s={last20.get('avg_hold_s', 0)}"
        else:
            mem = "MEM|n=0,win=0,wr=0,aw=0,al=0,rr=0.0,maxdd=0.0,hold_med_s=0,plan=neutral"

        # Assemble user message
        user_parts = [meta, pos, mkt] + feat_parts + [mem]
        user_parts.append("# OUTPUT JSON ONLY:")
        user_message = "\n\n".join(user_parts)

        # System message
        system = (
            "You are a strategy tuner for the same trading agent. "
            "Use INPUT data + compressed trade memory to adjust short-term style: "
            "position sizing curve vs. risk, preferred holding window, "
            "when to avoid trading (e.g., high VIX, lunch), and trend regime behavior. "
            "Propose PARAM deltas only; do not produce trades here. "
            "Return JSON with keys: "
            '{"style":"scalp|intra|avoid", "size_curve":"conservative|balanced|aggressive", '
            '"hold_s_range":[min,max], "open_filters":["..."], "close_rules":["..."], '
            '"confidence":0.xx}. '
            "No explanations."
        )

        # Apply prompt overrides if present
        # Priority: 1) prompt_text (direct override) > 2) prompt_id (from library) > 3) default
        if self.prompt_text:
            system = self.prompt_text
        elif self.prompt_id and self.prompt_id in self._library:
            system = self._library[self.prompt_id]

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_message}
        ]

        # Provider kwargs
        provider_kwargs = {}
        if provider.lower() in ("openai", "deepseek_nvidia", "deepseek"):
            provider_kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": REFLECTION_SCHEMA
            }
        elif provider.lower() == "gemini":
            provider_kwargs["generation_config"] = {"response_mime_type": "application/json"}

        return messages, provider_kwargs

    def build(self, **kwargs) -> Tuple[List[dict], dict]:
        """Main build method that routes to execution or reflection

        Returns:
            (messages, provider_kwargs)
        """
        if self.mode == "execution":
            return self.build_execution_prompt(**kwargs)
        elif self.mode == "reflection":
            return self.build_reflection_prompt(**kwargs)
        else:
            raise ValueError(f"Unknown mode: {self.mode}")


# ============================================================================
# Response Parsing
# ============================================================================

def parse_execution_response(response_text: str) -> Dict[str, Any]:
    """Parse execution response JSON

    Args:
        response_text: Raw response text from LLM

    Returns:
        Parsed dict with keys: action, quantity, confidence, tp, sl, tw_s
    """
    try:
        data = json.loads(response_text)

        # Map compact keys to full names
        return {
            'action': data.get('a', 'H'),  # B/S/H -> BUY/SELL/HOLD
            'quantity': data.get('q', 0),
            'confidence': data.get('conf', 0.0),
            'take_profit_bps': data.get('tp'),
            'stop_loss_bps': data.get('sl'),
            'time_window_seconds': data.get('tw_s')
        }
    except json.JSONDecodeError as e:
        # Fallback to HOLD
        return {
            'action': 'H',
            'quantity': 0,
            'confidence': 0.0,
            'take_profit_bps': None,
            'stop_loss_bps': None,
            'time_window_seconds': None,
            'error': str(e)
        }


def parse_reflection_response(response_text: str) -> Dict[str, Any]:
    """Parse reflection response JSON

    Args:
        response_text: Raw response text from LLM

    Returns:
        Parsed dict with strategy tuning parameters
    """
    try:
        data = json.loads(response_text)
        return {
            'style': data.get('style', 'intra'),
            'size_curve': data.get('size_curve', 'balanced'),
            'hold_s_range': data.get('hold_s_range', [30, 300]),
            'open_filters': data.get('open_filters', []),
            'close_rules': data.get('close_rules', []),
            'confidence': data.get('confidence', 0.5)
        }
    except json.JSONDecodeError as e:
        # Fallback to neutral
        return {
            'style': 'avoid',
            'size_curve': 'conservative',
            'hold_s_range': [60, 180],
            'open_filters': [],
            'close_rules': [],
            'confidence': 0.0,
            'error': str(e)
        }
