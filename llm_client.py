# -*- coding: utf-8 -*-
"""
llm_client.py
-------------
LLM wrapper supporting multiple providers (OpenAI, Anthropic, OpenRouter, etc.)
Handles different response formats including <thinking> tags, code blocks, etc.

Environment variables (.env):
  OPENAI_API_KEY         - OpenAI API key
  ANTHROPIC_API_KEY      - Anthropic (Claude) API key
  OPENROUTER_API_KEY     - OpenRouter API key
  DEEPSEEK_API_KEY       - DeepSeek official API key
  DEEPSEEK_NVIDIA_API_KEY - DeepSeek via NVIDIA API key
  GEMINI_API_KEY         - Google Gemini API key
  LLM_PROVIDER           - Default provider: "openai" | "anthropic" | "openrouter" | "deepseek" | "deepseek-nvidia" | "gemini"
  LLM_MODEL              - Default model name
  LLM_BASE_URL           - Optional base URL for custom endpoints
"""

from __future__ import annotations
import os, json, re, logging
from typing import Any, Dict, List, Tuple, Optional
from datetime import datetime

# Load environment from .env
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Setup logging
logger = logging.getLogger("llm_client")
logger.setLevel(logging.INFO)

# Create logs directory if needed
os.makedirs("logs", exist_ok=True)

# File handler for detailed LLM logs
llm_log_file = f"logs/llm_client_{datetime.now().strftime('%Y%m%d')}.log"
file_handler = logging.FileHandler(llm_log_file)
file_handler.setLevel(logging.INFO)
file_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
file_handler.setFormatter(file_formatter)
logger.addHandler(file_handler)

# Console handler for important messages
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_formatter = logging.Formatter('[LLM] %(message)s')
console_handler.setFormatter(console_formatter)
logger.addHandler(console_handler)

SYSTEM_PROMPT = """You are a quantitative research assistant for walk‑forward HPO in algorithmic trading.

INPUT will include:
- objective: primary score and guardrails (e.g., OOS_sharpe target, OOS_mdd max)
- base_grid: current parameter grid (param -> [values])
- top_is: top in‑sample summaries
- top_oos: top out‑of‑sample summaries (preferred)
- constraints: {
    param_whitelist: [allowed_param_names],
    bounds: {param: [min,max]},
    types: {param: "float|int|cat"},
    min_trades: <int>, min_coverage: <float 0..1>
  }
- symbols: symbols under test

Your task:
Return a refined parameter grid that prioritizes OUT‑OF‑SAMPLE generalization.

STRICT RULES:
1) Output ONLY valid JSON, exactly:
   {"grid": {<param>: [values...]}, "notes": "<brief rationale>"}
   - No markdown, no code blocks, no extra text
2) Change ONLY parameters in constraints.param_whitelist. Do NOT introduce new names.
3) For numeric params, propose 3–5 values centered around OOS winners and within bounds; default spread ≈ ±20%.
4) For categorical params, keep top candidates (≤5) observed to generalize OOS.
5) Enforce guardrails: prefer higher OOS Sharpe; keep OOS MDD <= constraint; ensure trades >= min_trades and coverage >= min_coverage. If data is sparse, shrink exploration (narrow around stable values).
6) Prefer simple grids over wide exploration unless OOS evidence shows robust gains.

Example:
{"grid": {"lookback": [14, 18, 22], "threshold": [0.18, 0.2, 0.22]}, "notes": "Centered on OOS winners; clipped to bounds; narrowed due to low coverage"}
"""

def _extract_json_from_response(text: str) -> Dict[str, Any]:
    """
    Extract JSON from LLM response, handling various formats:
    - <thinking>...</thinking> tags (Claude, some models)
    - Markdown code blocks ```json ... ```
    - Plain JSON
    - Mixed text with JSON embedded
    """
    if not text or not text.strip():
        return {}

    # 1. Remove <thinking> tags if present
    text = re.sub(r'<thinking>.*?</thinking>', '', text, flags=re.DOTALL | re.IGNORECASE)

    # 2. Remove markdown code blocks
    text = re.sub(r'```json\s*', '', text, flags=re.IGNORECASE)
    text = re.sub(r'```\s*', '', text)

    # 3. Try to find JSON object with { ... }
    # Find outermost braces by counting depth
    start_idx = text.find('{')
    if start_idx != -1:
        depth = 0
        for i in range(start_idx, len(text)):
            if text[i] == '{':
                depth += 1
            elif text[i] == '}':
                depth -= 1
                if depth == 0:
                    text = text[start_idx:i+1]
                    break

    # 4. Clean up common issues
    text = text.strip()

    # 5. Try to parse
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        # Last resort: try to find and extract just the grid and notes
        grid_match = re.search(r'"grid"\s*:\s*(\{[^}]+\})', text)
        notes_match = re.search(r'"notes"\s*:\s*"([^"]*)"', text)

        if grid_match:
            try:
                grid = json.loads(grid_match.group(1))
                notes = notes_match.group(1) if notes_match else ""
                return {"grid": grid, "notes": notes}
            except Exception:
                pass

        # Give up
        raise RuntimeError(f"Failed to parse JSON from LLM response: {e}\nResponse:\n{text[:500]}")


def _call_openai(messages: List[Dict[str, str]], model: str, api_key: str, base_url: Optional[str] = None) -> str:
    """Call OpenAI API.

    - Uses Responses API for GPT-5 (or when LLM_API_STYLE=responses)
    - Falls back to Chat Completions for GPT-4 family
    """
    try:
        # Prefer the new client import style
        from openai import OpenAI  # type: ignore
    except Exception:
        try:
            import openai  # type: ignore
            OpenAI = openai.OpenAI  # type: ignore
        except Exception:
            raise RuntimeError("openai package not installed. Run: pip install openai")

    client_kwargs = {"api_key": api_key}
    if base_url:
        # Pass through provided base_url without forcing "/v1" to avoid
        # duplicating paths like "/v1/responses/v1/responses" when the SDK
        # constructs endpoint URLs.
        bu = base_url.rstrip("/")
        client_kwargs["base_url"] = bu
    client = OpenAI(**client_kwargs)  # type: ignore

    # Decide which API surface to use
    style = (os.getenv("LLM_API_STYLE", "").lower())
    if model.lower().startswith("gpt-5") or style in {"responses", "response"}:
        # Responses API: use 'instructions' for system prompt and simple string input for user content
        system_msg = next((m.get("content", "") for m in messages if m.get("role") == "system"), "")
        user_parts = [m.get("content", "") for m in messages if m.get("role") != "system"]
        user_input = "\n\n".join([p for p in user_parts if p]) or ""
        # Only include reasoning/text knobs for models that support reasoning.
        # Many non-reasoning models (e.g., gpt-4o-mini) reject reasoning.effort.
        m = (model or "").lower()
        supports_reasoning = (
            m.startswith("gpt-5") or m.startswith("o4") or m.startswith("o3") or "reason" in m
        )
        create_kwargs = {
            "model": model,
            "input": user_input or "",
        }
        if system_msg:
            create_kwargs["instructions"] = system_msg
        if supports_reasoning:
            create_kwargs["reasoning"] = {"effort": os.getenv("LLM_REASONING_EFFORT", "low")}
            create_kwargs["text"] = {"verbosity": os.getenv("LLM_VERBOSITY", "low")}
        resp = client.responses.create(  # type: ignore
            **create_kwargs
        )
        # Try various shapes to extract text
        text = getattr(resp, "output_text", None)
        if text:
            return text
        data = resp if isinstance(resp, dict) else getattr(resp, "model_dump", lambda: {} )()  # type: ignore
        if not data:
            # Last chance: stringify
            return str(resp)
        # Walk common fields
        for path in (["output"], ["response"], ["message", "content"]):
            cur = data
            ok = True
            for p in path:
                if isinstance(cur, dict) and p in cur:
                    cur = cur[p]
                else:
                    ok = False
                    break
            if ok:
                if isinstance(cur, list):
                    for item in cur:
                        if isinstance(item, dict):
                            if item.get("type") in ("output_text", "text") and item.get("text"):
                                return item["text"]
                            if isinstance(item.get("content"), list):
                                for part in item["content"]:
                                    if part.get("type") in ("output_text", "text") and part.get("text"):
                                        return part["text"]
                elif isinstance(cur, str):
                    return cur
        # Fallback
        return json.dumps(data)
    else:
        # Chat Completions (GPT-4 family)
        try:
            resp = client.chat.completions.create(  # type: ignore
                model=model,
                messages=messages,
                temperature=0.2,
                response_format={"type": "json_object"}
            )
            return resp.choices[0].message.content or ""
        except Exception:
            # Fallback without response_format
            resp = client.chat.completions.create(  # type: ignore
                model=model,
                messages=messages,
                temperature=0.2
            )
            return resp.choices[0].message.content or ""


def _call_anthropic(messages: List[Dict[str, str]], model: str, api_key: str) -> str:
    """Call Anthropic Claude API."""
    try:
        import anthropic
    except ImportError:
        raise RuntimeError("anthropic package not installed. Run: pip install anthropic")

    client = anthropic.Anthropic(api_key=api_key)

    # Convert messages format (Anthropic uses slightly different structure)
    system_msg = next((m["content"] for m in messages if m["role"] == "system"), "")
    user_messages = [m for m in messages if m["role"] != "system"]

    response = client.messages.create(
        model=model,
        max_tokens=4096,
        temperature=0.2,
        system=system_msg,
        messages=user_messages
    )

    return response.content[0].text


def _call_openrouter(messages: List[Dict[str, str]], model: str, api_key: str) -> str:
    """Call OpenRouter API (uses OpenAI-compatible format)."""
    return _call_openai(
        messages=messages,
        model=model,
        api_key=api_key,
        base_url="https://openrouter.ai/api/v1"
    )


def _call_deepseek(messages: List[Dict[str, str]], model: str, api_key: str) -> str:
    """Call DeepSeek official API (uses OpenAI-compatible format)."""
    return _call_openai(
        messages=messages,
        model=model,
        api_key=api_key,
        base_url="https://api.deepseek.com/v1"
    )


def _call_deepseek_nvidia(messages: List[Dict[str, str]], model: str, api_key: str) -> str:
    """Call DeepSeek via NVIDIA API (uses async httpx, adapted from working provider)."""
    logger.info("="*80)
    logger.info(f"LLM REQUEST: DeepSeek NVIDIA - Model: {model}")
    logger.info(f"Endpoint: https://integrate.api.nvidia.com/v1/chat/completions")
    logger.info(f"Messages: {json.dumps(messages, indent=2)}")

    try:
        import httpx
        import asyncio
    except ImportError:
        raise RuntimeError("httpx package not installed. Run: pip install httpx")

    # Use async function but run it synchronously
    async def _async_call():
        request_payload = {
            "model": model,
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": 4096,
            "top_p": 0.9,
            "stream": False,
        }

        logger.info(f"Request payload: {json.dumps(request_payload, indent=2)}")

        async with httpx.AsyncClient(timeout=300.0) as client:
            response = await client.post(
                "https://integrate.api.nvidia.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",  # Full key for actual request
                    "Content-Type": "application/json",
                },
                json=request_payload
            )
            logger.info(f"Used API key: {api_key[:20]}...{api_key[-4:]}")  # Log masked key after request

            logger.info(f"Response status: {response.status_code}")

            response.raise_for_status()
            data = response.json()

            # NVIDIA's R1 API puts reasoning in separate field
            message = data["choices"][0]["message"]
            reasoning_content = message.get("reasoning_content", "")
            raw_content = message.get("content", "")

            logger.info("-"*80)
            if reasoning_content:
                logger.info(f"LLM REASONING (DeepSeek R1): {reasoning_content[:500]}...")
            logger.info(f"LLM RESPONSE: {raw_content[:1000]}...")
            logger.info("="*80)

            # R1 sometimes puts the answer in reasoning_content, especially if reasoning is long
            # If content is empty but reasoning exists, try to extract JSON from reasoning
            if not raw_content.strip() and reasoning_content:
                logger.warning("⚠ Content field is empty, attempting to extract JSON from reasoning_content")
                # Return full reasoning to let parser extract JSON
                return reasoning_content

            # Return content (reasoning is logged but not returned for grid generation)
            return raw_content

    # Run async function in sync context
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    try:
        result = loop.run_until_complete(_async_call())
        logger.info(f"✓ DeepSeek NVIDIA call succeeded, response length: {len(result)} chars")
        return result
    except Exception as e:
        logger.error(f"✗ DeepSeek NVIDIA call failed: {e}")
        raise


def _call_gemini(messages: List[Dict[str, str]], model: str, api_key: str) -> str:
    """Call Google Gemini API."""
    try:
        import google.generativeai as genai
    except ImportError:
        raise RuntimeError("google-generativeai package not installed. Run: pip install google-generativeai")

    genai.configure(api_key=api_key)

    # Convert messages to Gemini format
    system_msg = next((m["content"] for m in messages if m["role"] == "system"), "")
    user_messages = [m for m in messages if m["role"] != "system"]

    # Combine system prompt and user message
    prompt = system_msg + "\n\n" + "\n\n".join([m["content"] for m in user_messages])

    # Create model and generate
    gemini_model = genai.GenerativeModel(model)
    response = gemini_model.generate_content(
        prompt,
        generation_config=genai.GenerationConfig(
            temperature=0.2,
            response_mime_type="application/json"
        )
    )

    return response.text


def _parse_fallbacks(env_val: str) -> List[Tuple[str, str]]:
    pairs: List[Tuple[str, str]] = []
    for item in (env_val or "").split(','):
        item = item.strip()
        if not item:
            continue
        if ':' in item:
            p, m = item.split(':', 1)
            pairs.append((p.strip(), m.strip()))
        else:
            pairs.append(("openai", item))
    return pairs


def ask_for_grid(context: Dict[str, Any],
                 model: Optional[str] = None,
                 provider: Optional[str] = None) -> Tuple[Dict[str, List[Any]], str]:
    """
    Ask LLM to propose a refined parameter grid.

    Args:
        context: Dictionary with objective, base_grid, top_is, symbols, notes
        model: Model name (e.g., "gpt-4o-mini", "claude-3-5-sonnet-20241022")
        provider: LLM provider ("openai", "anthropic", "openrouter")

    Returns:
        (grid_dict, rationale_text)

    Raises:
        RuntimeError: If API key missing or LLM call fails
    """
    # Determine provider and model
    provider = provider or os.getenv("LLM_PROVIDER", "openai")

    # Default models per provider
    default_models = {
        "openai": "gpt-4o-mini",
        "anthropic": "claude-3-5-sonnet-20241022",
        "openrouter": "anthropic/claude-3.5-sonnet",
        "deepseek": "deepseek-chat",
        "deepseek-nvidia": "deepseek/deepseek-r1",
        "gemini": "gemini-2.0-flash-exp"
    }
    model = model or os.getenv("LLM_MODEL") or default_models.get(provider, "gpt-4o-mini")

    # Get API key
    api_key_env_map = {
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
        "deepseek-nvidia": "DEEPSEEK_NVIDIA_API_KEY",
        "gemini": "GEMINI_API_KEY"
    }
    api_key = os.getenv(api_key_env_map.get(provider, "OPENAI_API_KEY"))
    if not api_key:
        raise RuntimeError(
            f"API key not found for provider '{provider}'. "
            f"Set {api_key_env_map.get(provider, 'OPENAI_API_KEY')} in .env file"
        )

    # Build messages
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(context, ensure_ascii=False, indent=2)}
    ]

    def _invoke(pv: str, md: str) -> str:
        if pv == "anthropic":
            return _call_anthropic(messages, md, os.getenv("ANTHROPIC_API_KEY", ""))
        if pv == "openrouter":
            return _call_openrouter(messages, md, os.getenv("OPENROUTER_API_KEY", ""))
        if pv == "deepseek":
            return _call_deepseek(messages, md, os.getenv("DEEPSEEK_API_KEY", ""))
        if pv == "deepseek-nvidia":
            return _call_deepseek_nvidia(messages, md, os.getenv("DEEPSEEK_NVIDIA_API_KEY", ""))
        if pv == "gemini":
            return _call_gemini(messages, md, os.getenv("GEMINI_API_KEY", ""))
        # default openai
        return _call_openai(messages, md, os.getenv("OPENAI_API_KEY", "") or api_key, os.getenv("LLM_BASE_URL"))

    # Primary attempt
    logger.info("="*80)
    logger.info(f"ask_for_grid() called - Provider: {provider}, Model: {model}")
    logger.info(f"Context summary: symbols={context.get('symbols')}, objective={context.get('objective', {}).get('metric')}")
    logger.info("="*80)

    last_err: Optional[Exception] = None
    try:
        logger.info(f"Attempting primary provider: {provider}/{model}")
        response_text = _invoke(provider, model)
        logger.info(f"✓ Primary provider succeeded")
    except Exception as e:
        logger.warning(f"✗ Primary provider failed: {e}")
        last_err = e
        # Try fallbacks
        fb_list = _parse_fallbacks(os.getenv("LLM_FALLBACKS", ""))
        if fb_list:
            logger.info(f"Trying {len(fb_list)} fallback provider(s): {fb_list}")
        for pv, md in fb_list:
            try:
                logger.info(f"Attempting fallback: {pv}/{md}")
                response_text = _invoke(pv or provider, md)
                provider, model = pv or provider, md or model
                logger.info(f"✓ Fallback provider succeeded: {provider}/{model}")
                last_err = None
                break
            except Exception as e2:
                logger.warning(f"✗ Fallback provider failed: {pv}/{md} - {e2}")
                last_err = e2
        if last_err is not None:
            logger.error(f"✗ All providers failed. Last error: {last_err}")
            raise RuntimeError(f"LLM call failed ({provider}/{model}): {last_err}") from last_err

    # Parse response (handles <thinking>, markdown, etc.)
    logger.info("-"*80)
    logger.info("Parsing LLM response...")
    try:
        data = _extract_json_from_response(response_text)
        grid = data.get("grid", {})
        notes = data.get("notes", "")

        # Validation
        if not isinstance(grid, dict):
            raise ValueError("Grid must be a dictionary")

        logger.info(f"✓ Successfully parsed grid with {len(grid)} parameters")
        logger.info(f"Grid parameters: {list(grid.keys())}")
        logger.info(f"Notes: {notes[:200]}...")
        logger.info("="*80)

        return grid, notes

    except Exception as e:
        # Log the raw response for debugging
        logger.error(f"✗ Failed to parse response from {provider}/{model}")
        logger.error(f"Raw response (first 500 chars): {response_text[:500]}")
        logger.error("="*80)
        print(f"[LLM] Failed to parse response from {provider}/{model}")
        print(f"[LLM] Raw response (first 500 chars): {response_text[:500]}")
        raise RuntimeError(f"Failed to parse LLM response: {e}") from e


def test_llm_connection(provider: Optional[str] = None, model: Optional[str] = None) -> bool:
    """
    Test LLM connection with a simple query.

    Returns:
        True if connection successful, False otherwise
    """
    try:
        context = {
            "objective": {"metric": "sharpe", "maximize": True},
            "base_grid": {"test_param": [1, 2, 3]},
            "top_is": [{"params": {"test_param": 2}, "sharpe": 1.5}],
            "symbols": ["TEST"],
            "notes": "Connection test"
        }
        grid, notes = ask_for_grid(context, model=model, provider=provider)
        print(f"[LLM] Connection test successful!")
        print(f"[LLM] Grid: {grid}")
        print(f"[LLM] Notes: {notes}")
        return True
    except Exception as e:
        print(f"[LLM] Connection test failed: {e}")
        return False


if __name__ == "__main__":
    # Test connection
    import sys
    provider = sys.argv[1] if len(sys.argv) > 1 else None
    model = sys.argv[2] if len(sys.argv) > 2 else None

    print(f"Testing LLM connection...")
    print(f"Provider: {provider or os.getenv('LLM_PROVIDER', 'openai')}")
    print(f"Model: {model or os.getenv('LLM_MODEL', 'default')}")

    success = test_llm_connection(provider=provider, model=model)
    sys.exit(0 if success else 1)
