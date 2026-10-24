#!/usr/bin/env python3
"""
Verification Script: End-to-End Skill Synthesis
验证从 Lesson → Skill → Code → Execution 的完整流程

This script provides EVIDENCE that the final gap has been closed.
"""
import sys
import json
from pathlib import Path
from typing import List, Dict, Any

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from libs.evolution.skill_synthesizer import (
    SkillSynthesizer,
    get_pending_skills,
    approve_skill
)
from libs.logger import get_logger

log = get_logger(__name__)


def print_section(title: str):
    """Print section header"""
    print(f"\n{'=' * 80}")
    print(f"  {title}")
    print(f"{'=' * 80}\n")


def verify_skill_synthesizer_exists():
    """Verify SkillSynthesizer module exists and can be imported"""
    print_section("1. VERIFY MODULE EXISTS")

    try:
        from libs.evolution.skill_synthesizer import SkillSynthesizer
        print("✅ SkillSynthesizer module imported successfully")

        # Check class methods
        required_methods = [
            'synthesize_from_lessons',
            'generate_skill_code',
            '_classify_lesson',
            '_generate_skill_spec',
            '_validate_code'
        ]

        for method in required_methods:
            if hasattr(SkillSynthesizer, method):
                print(f"   ✅ Method '{method}' exists")
            else:
                print(f"   ❌ Method '{method}' missing")
                return False

        return True
    except ImportError as e:
        print(f"❌ Failed to import SkillSynthesizer: {e}")
        return False


def verify_lesson_classification():
    """Verify lesson classification works for all categories"""
    print_section("2. VERIFY LESSON CLASSIFICATION")

    synthesizer = SkillSynthesizer()

    test_lessons = [
        ("Reduce position size in high volatility", "risk_control"),
        ("Add momentum indicator for better signals", "feature_engineering"),
        ("Wait for clear price action before entry", "execution_timing"),
        ("Stale data caused model failures", "data_quality"),
        ("Improve model accuracy with feature selection", "model_tuning"),
    ]

    passed = 0
    for lesson, expected_category in test_lessons:
        classification = synthesizer._classify_lesson(lesson, context=None)
        if classification.category == expected_category:
            print(f"✅ '{lesson[:50]}...'")
            print(f"   → Category: {classification.category}, Confidence: {classification.confidence:.2f}")
            passed += 1
        else:
            print(f"❌ '{lesson[:50]}...'")
            print(f"   → Expected: {expected_category}, Got: {classification.category}")

    print(f"\n📊 Classification Accuracy: {passed}/{len(test_lessons)} = {100*passed/len(test_lessons):.0f}%")
    return passed == len(test_lessons)


def verify_skill_synthesis():
    """Verify skill synthesis from lessons"""
    print_section("3. VERIFY SKILL SYNTHESIS")

    synthesizer = SkillSynthesizer()

    lessons = [
        "Reduce position size by 50% when market volatility exceeds normal levels",
        "Add RSI indicator to improve entry timing signals"
    ]

    print(f"Input Lessons: {len(lessons)}")
    for i, lesson in enumerate(lessons, 1):
        print(f"  {i}. {lesson}")

    skill_specs = synthesizer.synthesize_from_lessons(lessons)

    print(f"\n✅ Synthesized {len(skill_specs)} skills")

    for spec in skill_specs:
        print(f"\n  Skill ID: {spec.skill_id}")
        print(f"  Category: {spec.category}")
        print(f"  Name: {spec.name}")
        print(f"  Type: {spec.skill_type}")
        print(f"  Evolution Allowed: {spec.allow_evolution}")
        print(f"  Metrics Impact: {spec.metrics_impact}")
        print(f"  Status: {spec.status}")

    return len(skill_specs) == len(lessons)


def verify_code_generation():
    """Verify code generation produces valid, executable code"""
    print_section("4. VERIFY CODE GENERATION")

    synthesizer = SkillSynthesizer()

    # Create a test skill spec
    from libs.evolution.skill_synthesizer import SkillSpecification
    from datetime import datetime, timezone

    spec = SkillSpecification(
        skill_id="test_verification_001",
        name="Test Verification Skill",
        description="Test skill for verification",
        category="risk_control",
        skill_type="PURE",
        file_path="libs/skills/generated/risk/test_verification_001.py",
        test_path="tests/unit/skills/generated/test_test_verification_001.py",
        allow_evolution=True,
        objective="PERFORMANCE",
        metrics_impact=["sharpe", "drawdown"],
        source_lesson="Reduce risk in volatile conditions",
        generated_at=datetime.now(timezone.utc).isoformat(),
        status="pending_approval"
    )

    print("Generating code for test skill...")
    generated = synthesizer.generate_skill_code(spec, use_llm=False)

    if generated:
        print("✅ Code generation successful")
        print(f"\n  Implementation Lines: {len(generated.implementation_code.splitlines())}")
        print(f"  Test Lines: {len(generated.test_code.splitlines())}")

        # Check validation
        validation = generated.validation_result
        print(f"\n  Validation Results:")
        print(f"    Syntax Valid: {'✅' if validation['syntax_valid'] else '❌'} {validation['syntax_valid']}")
        print(f"    Test Syntax Valid: {'✅' if validation['test_syntax_valid'] else '❌'} {validation['test_syntax_valid']}")
        print(f"    Has Docstrings: {'✅' if validation['has_docstrings'] else '❌'} {validation['has_docstrings']}")
        print(f"    Has Type Hints: {'✅' if validation['has_type_hints'] else '❌'} {validation['has_type_hints']}")

        if validation['errors']:
            print(f"\n  Errors: {validation['errors']}")

        return validation['syntax_valid'] and validation['test_syntax_valid']
    else:
        print("❌ Code generation failed")
        return False


def verify_code_execution():
    """Verify generated code can be executed"""
    print_section("5. VERIFY CODE EXECUTION")

    synthesizer = SkillSynthesizer()

    # Generate code
    from libs.evolution.skill_synthesizer import SkillSpecification
    from datetime import datetime, timezone

    spec = SkillSpecification(
        skill_id="test_execution_001",
        name="Test Execution Skill",
        description="Test",
        category="risk_control",
        skill_type="PURE",
        file_path="libs/skills/generated/risk/test_execution_001.py",
        test_path="tests/unit/skills/generated/test_test_execution_001.py",
        allow_evolution=True,
        objective="PERFORMANCE",
        metrics_impact=["sharpe"],
        source_lesson="Test execution",
        generated_at=datetime.now(timezone.utc).isoformat(),
        status="pending_approval"
    )

    generated = synthesizer.generate_skill_code(spec, use_llm=False)

    if not generated:
        print("❌ Code generation failed")
        return False

    # Execute the code
    try:
        exec_globals = {}
        exec(generated.implementation_code, exec_globals)

        # Verify functions exist
        if "calculate_position_size" in exec_globals:
            print("✅ Function 'calculate_position_size' defined")

            # Call the function
            func = exec_globals["calculate_position_size"]
            result = func(volatility=0.5, base_size=100.0)

            print(f"   → calculate_position_size(volatility=0.5, base_size=100.0) = {result:.2f}")

            # Test logic: high volatility → smaller position
            result_high = func(volatility=2.0, base_size=100.0)
            result_low = func(volatility=0.1, base_size=100.0)

            if result_high < result_low:
                print(f"   ✅ Logic verified: high volatility ({result_high:.2f}) < low volatility ({result_low:.2f})")
                return True
            else:
                print(f"   ❌ Logic error: high volatility ({result_high:.2f}) >= low volatility ({result_low:.2f})")
                return False
        else:
            print("❌ Function 'calculate_position_size' not found")
            return False

    except Exception as e:
        print(f"❌ Execution failed: {e}")
        return False


def verify_sleep_loop_integration():
    """Verify skill synthesis is integrated into sleep_loop.py"""
    print_section("6. VERIFY SLEEP LOOP INTEGRATION")

    sleep_loop_path = Path("scripts/ai_trader/memory/sleep_loop.py")

    if not sleep_loop_path.exists():
        print("❌ sleep_loop.py not found")
        return False

    content = sleep_loop_path.read_text()

    checks = [
        ("SkillSynthesizer import", "from libs.evolution.skill_synthesizer import SkillSynthesizer"),
        ("synthesize_from_lessons call", "synthesizer.synthesize_from_lessons("),
        ("skill_synthesis memory event", '"type": "skill_synthesis"'),
    ]

    passed = 0
    for name, pattern in checks:
        if pattern in content:
            print(f"✅ {name}")
            passed += 1
        else:
            print(f"❌ {name} missing")

    return passed == len(checks)


def verify_approval_queue():
    """Verify approval queue mechanism works"""
    print_section("7. VERIFY APPROVAL QUEUE")

    approval_queue_path = Path("runs/memory/skill_approval_queue.jsonl")

    # Create test approval queue entry (use actionable lesson)
    synthesizer = SkillSynthesizer()
    lessons = ["Reduce position size when high volatility detected"]  # Actionable lesson

    skill_specs = synthesizer.synthesize_from_lessons(lessons)

    if not skill_specs:
        print("❌ No skills synthesized for approval queue test")
        return False

    # Check if approval queue file was created/updated
    if approval_queue_path.exists():
        print(f"✅ Approval queue file exists: {approval_queue_path}")

        # Read last entry
        with approval_queue_path.open("r") as f:
            lines = f.readlines()
            if lines:
                last_entry = json.loads(lines[-1])
                print(f"   Latest Entry:")
                print(f"     Skill ID: {last_entry.get('skill_id')}")
                print(f"     Category: {last_entry.get('category')}")
                print(f"     Status: {last_entry.get('status')}")
                return last_entry.get('status') == 'pending_approval'
            else:
                print("❌ Approval queue is empty")
                return False
    else:
        print(f"❌ Approval queue file not found: {approval_queue_path}")
        return False


def verify_full_e2e():
    """Verify full end-to-end workflow"""
    print_section("8. VERIFY FULL E2E WORKFLOW")

    print("Testing: Lesson → Classify → Synthesize → Generate → Validate → Execute")

    # Step 1: Start with a lesson
    lesson = "Reduce position size by 30% when volatility exceeds 2x normal levels"
    print(f"\n1. Input Lesson:")
    print(f"   '{lesson}'")

    # Step 2: Synthesize
    synthesizer = SkillSynthesizer()
    skill_specs = synthesizer.synthesize_from_lessons([lesson])

    if not skill_specs:
        print("\n❌ Step 2 FAILED: No skills synthesized")
        return False

    spec = skill_specs[0]
    print(f"\n2. Synthesized Skill:")
    print(f"   ✅ Skill ID: {spec.skill_id}")
    print(f"   ✅ Category: {spec.category}")

    # Step 3: Generate code
    generated = synthesizer.generate_skill_code(spec, use_llm=False)

    if not generated:
        print("\n❌ Step 3 FAILED: Code generation failed")
        return False

    print(f"\n3. Generated Code:")
    print(f"   ✅ Implementation: {len(generated.implementation_code.splitlines())} lines")
    print(f"   ✅ Tests: {len(generated.test_code.splitlines())} lines")

    # Step 4: Validate
    if not generated.validation_result['syntax_valid']:
        print("\n❌ Step 4 FAILED: Validation failed")
        return False

    print(f"\n4. Validation:")
    print(f"   ✅ Syntax: {generated.validation_result['syntax_valid']}")
    print(f"   ✅ Docstrings: {generated.validation_result['has_docstrings']}")
    print(f"   ✅ Type Hints: {generated.validation_result['has_type_hints']}")

    # Step 5: Execute
    try:
        exec_globals = {}
        exec(generated.implementation_code, exec_globals)

        if "calculate_position_size" in exec_globals:
            func = exec_globals["calculate_position_size"]

            # Test the logic
            normal_vol = func(volatility=0.5, base_size=100.0)
            high_vol = func(volatility=2.0, base_size=100.0)

            if high_vol < normal_vol:
                print(f"\n5. Execution:")
                print(f"   ✅ Normal volatility (0.5): position = {normal_vol:.2f}")
                print(f"   ✅ High volatility (2.0): position = {high_vol:.2f}")
                print(f"   ✅ Logic verified: {high_vol:.2f} < {normal_vol:.2f}")

                print(f"\n{'=' * 80}")
                print(f"  🎯 FULL E2E SUCCESS: Gap is CLOSED!")
                print(f"{'=' * 80}")
                return True
            else:
                print(f"\n❌ Step 5 FAILED: Logic error")
                return False
        else:
            print(f"\n❌ Step 5 FAILED: Function not found")
            return False

    except Exception as e:
        print(f"\n❌ Step 5 FAILED: Execution error: {e}")
        return False


def main():
    """Run all verification tests"""
    print("\n" + "=" * 80)
    print("  SKILL SYNTHESIS VERIFICATION - EVIDENCE OF GAP CLOSURE")
    print("=" * 80)

    results = []

    # Run all verification tests
    results.append(("Module Exists", verify_skill_synthesizer_exists()))
    results.append(("Lesson Classification", verify_lesson_classification()))
    results.append(("Skill Synthesis", verify_skill_synthesis()))
    results.append(("Code Generation", verify_code_generation()))
    results.append(("Code Execution", verify_code_execution()))
    results.append(("Sleep Loop Integration", verify_sleep_loop_integration()))
    results.append(("Approval Queue", verify_approval_queue()))
    results.append(("Full E2E Workflow", verify_full_e2e()))

    # Summary
    print_section("VERIFICATION SUMMARY")

    passed = sum(1 for _, result in results if result)
    total = len(results)

    for name, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"{status:12} | {name}")

    print(f"\n{'=' * 80}")
    print(f"  TOTAL: {passed}/{total} tests passed ({100*passed/total:.0f}%)")
    print(f"{'=' * 80}")

    if passed == total:
        print("\n🎉 ALL VERIFICATION TESTS PASSED!")
        print("   Evidence: Final gap (Lesson → Skill) is CLOSED ✅")
        print("   System can now 'LEARN' AND 'DO' ✅")
        return 0
    else:
        print(f"\n⚠️  {total - passed} tests failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
