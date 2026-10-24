#!/usr/bin/env python3
"""
Example: How to use Self-Programming in your workflow

This demonstrates practical usage patterns for the self-programming system.
"""
import sys
from pathlib import Path

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from meta_agent.self_programming.engine import SelfProgrammingEngine
from meta_agent.self_programming.integration.blueprint_adapter import (
    create_gap_from_todo,
    blueprint_gap_to_programming_task,
)


def example_1_simple_refactoring():
    """
    Example 1: Simple refactoring task (safe, no LLM needed for demo).
    """
    print("\n" + "=" * 70)
    print("EXAMPLE 1: Simple Refactoring (Dry Run)")
    print("=" * 70)

    # Create a gap from a simple TODO item
    gap = create_gap_from_todo(
        description="Add comprehensive docstrings to all functions in demo.py",
        files=["demo.py"],
        category="documentation",
        severity="low",
    )

    # Convert to programming task
    task = blueprint_gap_to_programming_task(gap, force_whitelist_check=True)

    if not task:
        print("❌ Task rejected: Files not in whitelist or too risky")
        return

    print(f"✅ Task created: {task.task_id}")
    print(f"   Description: {task.description}")
    print(f"   Files: {task.related_files}")
    print(f"   Risk: {task.constraints['risk_level']}")

    # Run WITHOUT LLM (dry run)
    engine = SelfProgrammingEngine(use_llm=False)
    result = engine.run(task, use_sandbox=False)

    print(f"\n📊 Result:")
    print(f"   Patch files: {len(result.patch_set.files)}")
    print(f"   Test status: {result.test_report.overall_status}")
    print(f"   Review: {result.review.status}")

    return result


def example_2_with_llm():
    """
    Example 2: Using LLM for actual code generation.

    NOTE: This requires local LLM (qwen/deepseek) running via Ollama.
    """
    print("\n" + "=" * 70)
    print("EXAMPLE 2: Code Generation with LLM")
    print("=" * 70)

    gap = create_gap_from_todo(
        description="Add type hints to calculate_sum function parameters and return type",
        files=["demo.py"],
        category="refactor",
        severity="low",
    )

    task = blueprint_gap_to_programming_task(gap)

    if not task:
        print("❌ Task rejected")
        return

    print(f"✅ Task: {task.description}")
    print(f"⚠️  This would call local LLM - skipping for demo")

    # Uncomment to actually run with LLM:
    # engine = SelfProgrammingEngine(use_llm=True)
    # result = engine.run(task, use_sandbox=True)  # Uses Git sandbox
    # print(f"Review: {result.review.status}")

    return None


def example_3_multiple_files():
    """
    Example 3: Task affecting multiple files.
    """
    print("\n" + "=" * 70)
    print("EXAMPLE 3: Multi-File Task")
    print("=" * 70)

    gap = create_gap_from_todo(
        description="Add logging to health snapshot and monitoring dashboard",
        files=[
            "scripts/ai_trader/health_snapshot.py",
            "scripts/ai_trader/monitoring_dashboard.py",
        ],
        category="improvement",
        severity="low",
    )

    task = blueprint_gap_to_programming_task(gap)

    if not task:
        print("❌ Some files not whitelisted")
        return

    print(f"✅ Multi-file task: {len(task.related_files)} files")
    for f in task.related_files:
        print(f"   - {f}")

    return task


def example_4_blueprint_integration():
    """
    Example 4: How you would integrate with Blueprint system.
    """
    print("\n" + "=" * 70)
    print("EXAMPLE 4: Blueprint Integration Pattern")
    print("=" * 70)

    print("""
This shows how to integrate with your BlueprintImprovementLoop:

```python
# In your blueprint improvement code:

from meta_agent.self_programming.engine import SelfProgrammingEngine
from meta_agent.self_programming.integration import blueprint_gap_to_programming_task

def process_blueprint_gaps(gaps: List[BlueprintGap]):
    engine = SelfProgrammingEngine(use_llm=True)

    results = []
    for gap in gaps:
        # Convert gap to task
        task = blueprint_gap_to_programming_task(gap)

        if not task:
            logger.info(f"Gap {gap.gap_id} cannot be automated (too risky)")
            continue

        # Run self-programming
        try:
            result = engine.run(task, use_sandbox=True)

            # Log to Memory Fabric
            if result.review.status == "approve":
                logger.info(f"✅ Gap {gap.gap_id} auto-fixed!")
                log_to_memory_fabric(gap, result)
                results.append(result)
            else:
                logger.warning(f"⚠️ Gap {gap.gap_id} rejected: {result.review.summary}")

        except Exception as e:
            logger.error(f"❌ Gap {gap.gap_id} failed: {e}")

    return results
```
    """)


def example_5_safety_features():
    """
    Example 5: Demonstrating safety features.
    """
    print("\n" + "=" * 70)
    print("EXAMPLE 5: Safety Features")
    print("=" * 70)

    # Try to modify a forbidden file
    gap = create_gap_from_todo(
        description="Optimize order execution logic",
        files=["services/exec/execution_service.py"],  # FORBIDDEN
        category="optimization",
        severity="medium",
    )

    task = blueprint_gap_to_programming_task(gap, force_whitelist_check=True)

    if not task:
        print("✅ Safety check PASSED: Forbidden file rejected")
        print("   File: services/exec/execution_service.py")
        print("   Reason: Core trading execution - NEVER auto-modify")
    else:
        print("❌ Safety check FAILED: Should have rejected this file!")

    # Show risk assessment
    from meta_agent.self_programming.integration.blueprint_adapter import estimate_task_risk

    low_risk = create_gap_from_todo("Add docstring", ["demo.py"], "docs", "low")
    high_risk = create_gap_from_todo(
        "Refactor risk kernel",
        ["services/exec/safety_kernel.py"] * 10,  # Many files
        "refactor",
        "high"
    )

    print(f"\n📊 Risk Assessment:")
    print(f"   Low-risk task: {estimate_task_risk(low_risk)}")
    print(f"   High-risk task: {estimate_task_risk(high_risk)}")


def main():
    """Run all examples."""
    print("\n" + "=" * 70)
    print("SELF-PROGRAMMING SYSTEM - USAGE EXAMPLES")
    print("=" * 70)
    print("\nThese examples demonstrate how to use the self-programming system")
    print("in various scenarios, from simple refactoring to Blueprint integration.")

    try:
        example_1_simple_refactoring()
        example_2_with_llm()
        example_3_multiple_files()
        example_4_blueprint_integration()
        example_5_safety_features()

        print("\n" + "=" * 70)
        print("✅ ALL EXAMPLES COMPLETED")
        print("=" * 70)
        print("\nNext steps:")
        print("  1. Run with actual LLM: scripts/run_self_programming.py --use-llm")
        print("  2. Integrate with your Blueprint system")
        print("  3. Add to Sleep Loop for continuous improvement")
        print("=" * 70 + "\n")

    except Exception as e:
        print(f"\n❌ Example failed: {e}")
        import traceback
        traceback.print_exc()
        return 1

    return 0


if __name__ == "__main__":
    exit(main())
