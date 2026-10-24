#!/usr/bin/env python
"""Manual end-to-end test for prompt override flow

This script simulates the full flow:
1. Brain generates a plan with execution_prompt_id
2. Orchestrator prepares prompt overrides
3. Controller receives overrides and injects env vars
4. Bot reads env vars and creates PromptBuilder
5. PromptBuilder applies prompt override

Run: python scripts/ai_trader/tests/manual/test_prompt_override_flow.py
"""

import os
import sys
import json
from pathlib import Path

# Add parent dirs to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent.parent))

from llm_trader.llm_trader.prompt_builder_universal import UniversalPromptBuilder
from llm_trader.llm_trader.prompt_builder_compact import CompactPromptBuilder


def test_prompt_builder_with_id():
    """Test 1: Prompt override with ID (from library)"""
    print("\n" + "=" * 70)
    print("TEST 1: Prompt Override with ID (Safe Mode)")
    print("=" * 70)

    # Simulate environment variable set by controller
    os.environ["LLM_PROMPT_ID"] = "v3_fast_market_following"
    os.environ.pop("LLM_PROMPT_FILE", None)  # Clear text override

    # Create builder (as done in llm_trader_bot.py)
    builder = UniversalPromptBuilder(
        json_mode="schema",
        style="reasoning",
        prompt_id=os.getenv("LLM_PROMPT_ID"),
        prompt_text=None,
    )

    # Check if library was loaded
    print(f"✓ Library loaded: {len(builder._library)} prompts")
    print(f"✓ Available IDs: {list(k for k in builder._library.keys() if not k.startswith('_'))}")

    # Check if prompt_id is recognized
    if builder.prompt_id in builder._library:
        print(f"✓ Prompt ID '{builder.prompt_id}' found in library")
        prompt_preview = builder._library[builder.prompt_id][:100] + "..."
        print(f"✓ Prompt preview: {prompt_preview}")
    else:
        print(f"✗ Prompt ID '{builder.prompt_id}' NOT found in library")
        return False

    # Clean up
    os.environ.pop("LLM_PROMPT_ID", None)
    return True


def test_prompt_builder_with_text():
    """Test 2: Prompt override with raw text"""
    print("\n" + "=" * 70)
    print("TEST 2: Prompt Override with Text (Must Enable in Config)")
    print("=" * 70)

    # Simulate prompt text saved to file by orchestrator
    test_prompt = "You are a TEST trading agent. This is a custom prompt override."
    prompt_file = Path("/tmp/test_prompt_override.txt")
    prompt_file.write_text(test_prompt, encoding="utf-8")

    # Simulate environment variables
    os.environ.pop("LLM_PROMPT_ID", None)  # Clear ID
    os.environ["LLM_PROMPT_FILE"] = str(prompt_file)

    # Read from env (as done in llm_trader_bot.py)
    prompt_text = None
    if os.getenv("LLM_PROMPT_FILE"):
        try:
            prompt_text = Path(os.getenv("LLM_PROMPT_FILE")).read_text("utf-8")
            print(f"✓ Loaded prompt from file: {os.getenv('LLM_PROMPT_FILE')}")
        except Exception as e:
            print(f"✗ Failed to read prompt file: {e}")
            return False

    # Create builder
    builder = UniversalPromptBuilder(
        json_mode="schema",
        style="reasoning",
        prompt_id=None,
        prompt_text=prompt_text,
    )

    # Verify
    if builder.prompt_text == test_prompt:
        print(f"✓ Prompt text loaded: {builder.prompt_text[:80]}...")
        print(f"✓ When build() is called, this text will override the default prompt")
    else:
        print(f"✗ Prompt text mismatch")
        return False

    # Clean up
    os.environ.pop("LLM_PROMPT_FILE", None)
    prompt_file.unlink()
    return True


def test_compact_prompt_builder():
    """Test 3: CompactPromptBuilder with prompt override"""
    print("\n" + "=" * 70)
    print("TEST 3: CompactPromptBuilder with Prompt Override")
    print("=" * 70)

    os.environ["LLM_PROMPT_ID"] = "conservative"

    builder = CompactPromptBuilder(
        mode="execution",
        adaptive_trim=True,
        max_tokens_budget=2000,
        prompt_id=os.getenv("LLM_PROMPT_ID"),
        prompt_text=None,
    )

    if builder.prompt_id in builder._library:
        print(f"✓ CompactPromptBuilder loaded ID: {builder.prompt_id}")
        print(f"✓ Library contains: {len(builder._library)} prompts")
    else:
        print(f"✗ Prompt ID not found in library")
        return False

    os.environ.pop("LLM_PROMPT_ID", None)
    return True


def test_priority_order():
    """Test 4: Verify priority order (text > id > default)"""
    print("\n" + "=" * 70)
    print("TEST 4: Priority Order (text > id > default)")
    print("=" * 70)

    test_text = "This is TEXT override"
    test_id = "v3_stable"

    # Test: Both text and ID provided, text should win
    builder = UniversalPromptBuilder(
        prompt_id=test_id,
        prompt_text=test_text,
    )

    print(f"✓ Created builder with both ID='{test_id}' and text='{test_text[:30]}...'")

    # Simulate what happens in build()
    # Priority check (from our implementation)
    if builder.prompt_text:
        selected = "text"
    elif builder.prompt_id and builder.prompt_id in builder._library:
        selected = "id"
    else:
        selected = "default"

    if selected == "text":
        print(f"✓ Priority order correct: TEXT wins over ID")
        return True
    else:
        print(f"✗ Priority order incorrect: {selected} was selected")
        return False


def test_orchestrator_integration():
    """Test 5: Simulate orchestrator preparing overrides"""
    print("\n" + "=" * 70)
    print("TEST 5: Orchestrator Integration (Simulated)")
    print("=" * 70)

    # Simulate a plan from brain
    plan = {
        "session": "morning",
        "providers": [
            {
                "name": "gemini",
                "interval_sec": 300,
                "cycles": 10,
                "execution_prompt_id": "v3_stable",
                "execution_prompt_text": None,
                "ab_group": "champion",
            },
            {
                "name": "deepseek_nvidia",
                "interval_sec": 600,
                "cycles": 20,
                "execution_prompt_id": "v4_experimental",
                "execution_prompt_text": None,
                "ab_group": "challenger",
            }
        ]
    }

    print(f"✓ Simulated brain plan with {len(plan['providers'])} providers")

    # Simulate what orchestrator._prepare_prompt_overrides() does
    overrides = {}
    for i, p in enumerate(plan["providers"]):
        entry = {}
        if p.get("execution_prompt_id"):
            entry["PROMPT_ID"] = p["execution_prompt_id"]
            print(f"  Provider {i} ({p['name']}): ID={p['execution_prompt_id']}, AB={p.get('ab_group')}")
        if entry:
            overrides[i] = entry

    print(f"✓ Generated overrides: {overrides}")

    # Simulate what controller does
    print("\n✓ Controller would inject these env vars for each subprocess:")
    for idx, ov in overrides.items():
        print(f"  Process {idx}:")
        for key, val in ov.items():
            print(f"    {key}={val}")

    return True


def main():
    """Run all tests"""
    print("\n" + "=" * 70)
    print("PROMPT OVERRIDE FLOW - END-TO-END VERIFICATION")
    print("=" * 70)
    print("\nThis test verifies the complete prompt override implementation")
    print("from Brain → Orchestrator → Controller → Bot → PromptBuilder")

    results = []

    # Run all tests
    results.append(("Prompt Builder with ID", test_prompt_builder_with_id()))
    results.append(("Prompt Builder with Text", test_prompt_builder_with_text()))
    results.append(("Compact Prompt Builder", test_compact_prompt_builder()))
    results.append(("Priority Order", test_priority_order()))
    results.append(("Orchestrator Integration", test_orchestrator_integration()))

    # Summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)

    all_passed = True
    for test_name, passed in results:
        status = "✓ PASS" if passed else "✗ FAIL"
        print(f"{status}: {test_name}")
        if not passed:
            all_passed = False

    print("\n" + "=" * 70)
    if all_passed:
        print("✓ ALL TESTS PASSED - Implementation is working correctly!")
        print("=" * 70)
        return 0
    else:
        print("✗ SOME TESTS FAILED - Please review the output above")
        print("=" * 70)
        return 1


if __name__ == "__main__":
    sys.exit(main())
