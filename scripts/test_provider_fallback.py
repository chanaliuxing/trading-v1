#!/usr/bin/env python3
"""
Test script for Provider Fallback mechanism

Usage:
    python scripts/test_provider_fallback.py                  # Basic test
    python scripts/test_provider_fallback.py --simulate-fail  # Simulate deepseek_nvidia failure
"""

import sys
import os
import argparse
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from scripts.ai_trader.brain.brain import ControlBrain


def test_basic():
    """Test basic provider fallback functionality"""
    print("=" * 80)
    print("Testing Provider Fallback - Basic Test")
    print("=" * 80)
    print()

    # Initialize brain
    brain = ControlBrain(
        runs_dir="./runs",
        provider="deepseek_nvidia",
    )

    print("✅ ControlBrain initialized with ProviderPool")
    print(f"   Providers configured: {len(brain.pool.configs)}")
    print(f"   Priority order: {[c.name for c in brain.pool.configs]}")
    print()

    # Test propose_plan (simple context)
    print("🧪 Testing propose_plan...")
    try:
        plan = brain.propose_plan(
            session="test",
            context={
                "test_mode": True,
                "message": "This is a test call to verify provider fallback works"
            }
        )
        print("✅ propose_plan succeeded")
        print(f"   Result keys: {list(plan.keys())}")
        print()
    except Exception as e:
        print(f"❌ propose_plan failed: {e}")
        print()

    # Print statistics
    print("📊 Provider Statistics:")
    print()
    brain.print_provider_stats()
    print()

    return True


def test_simulate_failure():
    """Test fallback when primary provider fails"""
    print("=" * 80)
    print("Testing Provider Fallback - Simulated Failure")
    print("=" * 80)
    print()

    # Temporarily save and unset NVIDIA_API_KEY to simulate failure
    old_key = os.environ.get("NVIDIA_API_KEY")
    if old_key:
        print("⚠️  Temporarily unsetting NVIDIA_API_KEY to simulate failure...")
        del os.environ["NVIDIA_API_KEY"]
        print("   (Will be restored after test)")
        print()

    try:
        brain = ControlBrain(
            runs_dir="./runs",
            provider="deepseek_nvidia",
        )

        print("🧪 Testing propose_plan with deepseek_nvidia disabled...")
        print("   Expected: Should fallback to gemini")
        print()

        try:
            plan = brain.propose_plan(
                session="test_fallback",
                context={
                    "test_mode": True,
                    "message": "Testing fallback when primary provider fails"
                }
            )
            print("✅ propose_plan succeeded (fallback worked!)")
            print()

            # Print statistics
            print("📊 Provider Statistics (should show fallback usage):")
            print()
            brain.print_provider_stats()
            print()

        except Exception as e:
            print(f"❌ propose_plan failed even with fallback: {e}")
            print()

    finally:
        # Restore API key
        if old_key:
            os.environ["NVIDIA_API_KEY"] = old_key
            print("✅ NVIDIA_API_KEY restored")
            print()

    return True


def test_stats():
    """Test statistics methods"""
    print("=" * 80)
    print("Testing Provider Statistics Methods")
    print("=" * 80)
    print()

    brain = ControlBrain(runs_dir="./runs")

    # Test get_stats (returns dict)
    print("🧪 Testing get_provider_stats()...")
    stats = brain.get_provider_stats()
    print(f"✅ Stats returned: {type(stats)}")
    print(f"   Keys: {list(stats.keys())}")
    print()

    # Test print_stats (prints to console)
    print("🧪 Testing print_provider_stats()...")
    brain.print_provider_stats()
    print()

    return True


def main():
    parser = argparse.ArgumentParser(
        description="Test Provider Fallback mechanism"
    )
    parser.add_argument(
        "--simulate-fail",
        action="store_true",
        help="Simulate primary provider failure"
    )
    parser.add_argument(
        "--stats-only",
        action="store_true",
        help="Only test statistics methods"
    )

    args = parser.parse_args()

    try:
        if args.stats_only:
            test_stats()
        elif args.simulate_fail:
            test_simulate_failure()
        else:
            test_basic()

        print("=" * 80)
        print("✅ All tests completed")
        print("=" * 80)
        print()
        print("💡 Next steps:")
        print("   1. Check logs: runs/$(date +%Y-%m-%d)/provider_fallback.jsonl")
        print("   2. Check brain calls: runs/$(date +%Y-%m-%d)/brain_calls.jsonl")
        print("   3. Review PROVIDER_FALLBACK_GUIDE.md for usage instructions")
        print()

    except KeyboardInterrupt:
        print()
        print("⚠️  Test interrupted by user")
        sys.exit(130)
    except Exception as e:
        print(f"❌ Test failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
