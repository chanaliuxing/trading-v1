#!/usr/bin/env python3
"""Test Gemini chat_completion with compact prompt"""

import asyncio
import os
from dotenv import load_dotenv

# Load environment
load_dotenv("./.env")

from providers.gemini_provider import GeminiProvider
from prompt_builder_compact import CompactPromptBuilder


async def test_gemini_compact():
    """Test Gemini with compact prompt"""

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("❌ GEMINI_API_KEY not found in .env")
        return

    print("=" * 80)
    print("Testing Gemini Compact Prompt Support")
    print("=" * 80)

    # Initialize provider
    provider = GeminiProvider(
        api_key=api_key,
        model="gemini-2.0-flash-exp"
    )

    # Build compact prompt
    builder = CompactPromptBuilder(provider="gemini")

    market_data = {
        "symbol": "AAPL",
        "base_price": 175.50,
        "current_price": 175.80,
        "recent_bars_csv": "175.60,175.70,175.75,175.78,175.80",
        "delta_high_dbps": 15,
        "delta_low_dbps": -10,
        "volume_k": 250,
        "sfs_features": {
            "vwap_1m": 175.65,
            "atr_1m": 0.25,
            "regime": "UP_TREND",
        }
    }

    portfolio_state = {
        "cash": 50000.0,
        "positions": {"AAPL": 0},
        "max_position_size": 10000.0,
    }

    messages = builder.build_messages(
        symbol="AAPL",
        market_data=market_data,
        portfolio_state=portfolio_state,
    )

    print("\n📨 Messages Format:")
    for msg in messages:
        role = msg["role"]
        content = msg["content"][:100] + "..." if len(msg["content"]) > 100 else msg["content"]
        print(f"  {role}: {content}")

    print(f"\n📏 Total prompt length: {sum(len(m['content']) for m in messages)} characters")

    # Test with JSON schema
    print("\n🔧 Testing with JSON schema enforcement...")

    response_format = {
        "type": "json_schema",
        "json_schema": {
            "name": "trading_decision",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "a": {"type": "string", "enum": ["B", "S", "H"]},
                    "q": {"type": "integer"},
                    "conf": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": ["a", "q", "conf"],
                "additionalProperties": False,
            },
        },
    }

    try:
        result = await provider.chat_completion(
            messages=messages,
            response_format=response_format
        )

        print(f"\n✅ Success!")
        print(f"Result: {result}")

        # Validate format
        assert "a" in result, "Missing 'a' field"
        assert "q" in result, "Missing 'q' field"
        assert "conf" in result, "Missing 'conf' field"
        assert result["a"] in ["B", "S", "H"], f"Invalid action: {result['a']}"
        assert isinstance(result["q"], int), f"Quantity must be int: {result['q']}"
        assert 0 <= result["conf"] <= 1, f"Confidence out of range: {result['conf']}"

        print(f"\n🎉 All validations passed!")
        print(f"   Action: {result['a']}")
        print(f"   Quantity: {result['q']}")
        print(f"   Confidence: {result['conf']:.2f}")

    except Exception as e:
        print(f"\n❌ Error: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(test_gemini_compact())
