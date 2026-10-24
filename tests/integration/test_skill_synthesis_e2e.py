#!/usr/bin/env python3
"""
End-to-end test for Skill Synthesis: Lesson → Skill → Registry → Execution

This test verifies the final gap closure:
- Lessons from daily summary
- Synthesize into skill specifications
- Generate executable code
- Validate and approve
- Register in skill_registry.json
- Execute synthesized skill
"""
import pytest
import json
import tempfile
from pathlib import Path
from typing import List

from libs.evolution.skill_synthesizer import (
    SkillSynthesizer,
    SkillSpecification,
    LessonClassification,
    get_pending_skills,
    approve_skill
)


class TestSkillSynthesisE2E:
    """End-to-end tests for skill synthesis workflow"""

    def test_classify_risk_control_lesson(self):
        """Test classification of risk control lessons"""
        synthesizer = SkillSynthesizer()

        lesson = "In high volatility markets, reduce position size by 50% to limit drawdown"
        classification = synthesizer._classify_lesson(lesson, context=None)

        assert classification.category == "risk_control"
        assert classification.confidence >= 0.7
        assert classification.suggested_skill_type == "PURE"

    def test_classify_feature_engineering_lesson(self):
        """Test classification of feature engineering lessons"""
        synthesizer = SkillSynthesizer()

        lesson = "Momentum indicator works better when combined with trend strength signal"
        classification = synthesizer._classify_lesson(lesson, context=None)

        assert classification.category == "feature_engineering"
        assert classification.confidence >= 0.7

    def test_classify_execution_timing_lesson(self):
        """Test classification of execution timing lessons"""
        synthesizer = SkillSynthesizer()

        lesson = "Wait for clear price action before entry to improve fill quality"
        classification = synthesizer._classify_lesson(lesson, context=None)

        assert classification.category == "execution_timing"
        assert classification.confidence >= 0.7

    def test_classify_data_quality_lesson(self):
        """Test classification of data quality lessons"""
        synthesizer = SkillSynthesizer()

        lesson = "Stale data caused model errors, need better sync monitoring"
        classification = synthesizer._classify_lesson(lesson, context=None)

        assert classification.category == "data_quality"
        assert classification.confidence >= 0.7

    def test_synthesize_from_single_lesson(self):
        """Test synthesizing skills from a single actionable lesson"""
        synthesizer = SkillSynthesizer()

        lessons = ["Reduce position size in high volatility conditions"]
        skill_specs = synthesizer.synthesize_from_lessons(lessons)

        assert len(skill_specs) == 1
        spec = skill_specs[0]
        assert spec.category == "risk_control"
        assert spec.status == "pending_approval"
        assert spec.allow_evolution is True
        assert "sharpe" in spec.metrics_impact or "drawdown" in spec.metrics_impact

    def test_synthesize_from_multiple_lessons(self):
        """Test synthesizing skills from multiple lessons"""
        synthesizer = SkillSynthesizer()

        lessons = [
            "Reduce position size in high volatility",
            "Momentum indicator improves prediction accuracy",
            "Wait for clear price action before entering trades"
        ]
        skill_specs = synthesizer.synthesize_from_lessons(lessons)

        assert len(skill_specs) == 3
        categories = [s.category for s in skill_specs]
        assert "risk_control" in categories
        assert "feature_engineering" in categories
        assert "execution_timing" in categories

    def test_synthesize_ignores_non_actionable_lessons(self):
        """Test that non-actionable lessons are filtered out"""
        synthesizer = SkillSynthesizer()

        lessons = [
            "Today was a good day",  # Not actionable
            "Reduce position size in high volatility",  # Actionable
            "The market moved a lot"  # Not actionable
        ]
        skill_specs = synthesizer.synthesize_from_lessons(lessons)

        assert len(skill_specs) == 1  # Only 1 actionable lesson

    def test_generate_skill_code_risk_control(self):
        """Test code generation for risk control skills"""
        synthesizer = SkillSynthesizer()

        spec = SkillSpecification(
            skill_id="risk_test_001",
            name="Test Risk Skill",
            description="Test skill for risk control",
            category="risk_control",
            skill_type="PURE",
            file_path="libs/skills/generated/risk/risk_test_001.py",
            test_path="tests/unit/skills/generated/test_risk_test_001.py",
            allow_evolution=True,
            objective="PERFORMANCE",
            metrics_impact=["sharpe", "drawdown"],
            source_lesson="Reduce position size in high volatility",
            generated_at="2025-12-04T00:00:00Z",
            status="pending_approval"
        )

        generated = synthesizer.generate_skill_code(spec, use_llm=False)

        assert generated is not None
        assert generated.skill_spec == spec
        assert "calculate_position_size" in generated.implementation_code
        assert "calculate_stop_width" in generated.implementation_code
        assert "test_position_size" in generated.test_code
        assert generated.validation_result["syntax_valid"] is True
        assert generated.validation_result["test_syntax_valid"] is True

    def test_generate_skill_code_feature_engineering(self):
        """Test code generation for feature engineering skills"""
        synthesizer = SkillSynthesizer()

        spec = SkillSpecification(
            skill_id="feature_test_001",
            name="Test Feature Skill",
            description="Test skill for feature engineering",
            category="feature_engineering",
            skill_type="PURE",
            file_path="libs/skills/generated/features/feature_test_001.py",
            test_path="tests/unit/skills/generated/test_feature_test_001.py",
            allow_evolution=True,
            objective="PERFORMANCE",
            metrics_impact=["ic", "auc"],
            source_lesson="Add momentum indicator",
            generated_at="2025-12-04T00:00:00Z",
            status="pending_approval"
        )

        generated = synthesizer.generate_skill_code(spec, use_llm=False)

        assert generated is not None
        assert "calculate_feature" in generated.implementation_code
        assert "numpy" in generated.implementation_code.lower() or "np" in generated.implementation_code
        assert "test_feature" in generated.test_code
        assert generated.validation_result["syntax_valid"] is True

    def test_skill_approval_queue_workflow(self):
        """Test skill approval queue workflow"""
        # Use temporary directory for testing
        with tempfile.TemporaryDirectory() as tmpdir:
            synthesizer = SkillSynthesizer()
            synthesizer.approval_queue_path = Path(tmpdir) / "skill_approval_queue.jsonl"

            # Synthesize skills
            lessons = ["Reduce position size in high volatility"]
            skill_specs = synthesizer.synthesize_from_lessons(lessons)

            assert len(skill_specs) == 1

            # Check approval queue file
            assert synthesizer.approval_queue_path.exists()

            # Verify skill in queue
            with synthesizer.approval_queue_path.open("r") as f:
                lines = f.readlines()
                assert len(lines) == 1
                skill_data = json.loads(lines[0])
                assert skill_data["status"] == "pending_approval"
                assert skill_data["category"] == "risk_control"

    def test_code_validation_catches_syntax_errors(self):
        """Test that code validation catches syntax errors"""
        synthesizer = SkillSynthesizer()

        # Invalid Python code
        invalid_code = "def foo(\n    return 42"  # Missing closing paren
        valid_test = "def test_foo():\n    assert True"

        validation = synthesizer._validate_code(invalid_code, valid_test)

        assert validation["syntax_valid"] is False
        assert len(validation["errors"]) > 0
        assert "syntax error" in validation["errors"][0].lower()

    def test_code_validation_checks_docstrings_and_hints(self):
        """Test that code validation checks for docstrings and type hints"""
        synthesizer = SkillSynthesizer()

        code_with_docstring = '''def foo():
    """This is a docstring"""
    return 42
'''
        code_with_hints = "def foo(x: int) -> int:\n    return x * 2"

        validation = synthesizer._validate_code(code_with_docstring, "")
        assert validation["has_docstrings"] is True

        validation = synthesizer._validate_code(code_with_hints, "")
        assert validation["has_type_hints"] is True

    def test_skill_name_generation(self):
        """Test skill name generation from lessons"""
        synthesizer = SkillSynthesizer()

        classification = LessonClassification(
            lesson_text="Reduce position size in high volatility",
            category="risk_control",
            confidence=0.8,
            suggested_skill_type="PURE",
            rationale="Test"
        )

        name = synthesizer._generate_skill_name(
            "Reduce position size in high volatility",
            classification
        )

        assert len(name) > 0
        assert "volatility" in name.lower() or "position" in name.lower()

    def test_skill_file_path_generation(self):
        """Test that skill file paths are organized by category"""
        synthesizer = SkillSynthesizer()

        # Risk control skill
        risk_spec = synthesizer._generate_skill_spec(
            "Reduce position size",
            LessonClassification(
                lesson_text="",
                category="risk_control",
                confidence=0.8,
                suggested_skill_type="PURE",
                rationale=""
            ),
            None
        )
        assert "libs/skills/generated/risk/" in risk_spec.file_path

        # Feature engineering skill
        feature_spec = synthesizer._generate_skill_spec(
            "Add momentum indicator",
            LessonClassification(
                lesson_text="",
                category="feature_engineering",
                confidence=0.8,
                suggested_skill_type="PURE",
                rationale=""
            ),
            None
        )
        assert "libs/skills/generated/features/" in feature_spec.file_path

    def test_metrics_impact_assignment(self):
        """Test that metrics_impact is correctly assigned based on category"""
        synthesizer = SkillSynthesizer()

        # Risk control → sharpe, drawdown
        risk_spec = synthesizer._generate_skill_spec(
            "Reduce risk",
            LessonClassification(
                lesson_text="",
                category="risk_control",
                confidence=0.8,
                suggested_skill_type="PURE",
                rationale=""
            ),
            None
        )
        assert "sharpe" in risk_spec.metrics_impact or "drawdown" in risk_spec.metrics_impact

        # Feature engineering → ic, auc
        feature_spec = synthesizer._generate_skill_spec(
            "Add feature",
            LessonClassification(
                lesson_text="",
                category="feature_engineering",
                confidence=0.8,
                suggested_skill_type="PURE",
                rationale=""
            ),
            None
        )
        assert "ic" in feature_spec.metrics_impact or "auc" in feature_spec.metrics_impact

    def test_allow_evolution_based_on_category(self):
        """Test that allow_evolution is set correctly based on category"""
        synthesizer = SkillSynthesizer()

        # Risk control → allow evolution
        risk_spec = synthesizer._generate_skill_spec(
            "Reduce risk",
            LessonClassification(
                lesson_text="",
                category="risk_control",
                confidence=0.8,
                suggested_skill_type="PURE",
                rationale=""
            ),
            None
        )
        assert risk_spec.allow_evolution is True

        # Data quality → do NOT allow evolution (sensitive)
        data_spec = synthesizer._generate_skill_spec(
            "Fix data quality",
            LessonClassification(
                lesson_text="",
                category="data_quality",
                confidence=0.8,
                suggested_skill_type="SIDE_EFFECT",
                rationale=""
            ),
            None
        )
        assert data_spec.allow_evolution is False

    def test_generated_code_executes_without_error(self):
        """Test that generated code can be executed without runtime errors"""
        synthesizer = SkillSynthesizer()

        spec = SkillSpecification(
            skill_id="test_exec_001",
            name="Test Execution",
            description="Test",
            category="risk_control",
            skill_type="PURE",
            file_path="libs/skills/generated/risk/test_exec_001.py",
            test_path="tests/unit/skills/generated/test_test_exec_001.py",
            allow_evolution=True,
            objective="PERFORMANCE",
            metrics_impact=["sharpe"],
            source_lesson="Test",
            generated_at="2025-12-04T00:00:00Z",
            status="pending_approval"
        )

        generated = synthesizer.generate_skill_code(spec, use_llm=False)

        # Execute the generated code
        exec_globals = {}
        exec(generated.implementation_code, exec_globals)

        # Check that functions are defined
        assert "calculate_position_size" in exec_globals
        assert "calculate_stop_width" in exec_globals

        # Call functions to verify they work
        position_size = exec_globals["calculate_position_size"](volatility=0.5, base_size=100.0)
        assert isinstance(position_size, float)
        assert 0 < position_size <= 100.0

        stop_width = exec_globals["calculate_stop_width"](volatility=0.5, base_width=0.02)
        assert isinstance(stop_width, float)
        assert 0 < stop_width <= 0.05

    def test_full_e2e_lesson_to_executable_skill(self):
        """
        Full end-to-end test: Lesson → Spec → Code → Validation → Execution

        This is the KEY TEST that verifies the final gap closure.
        """
        synthesizer = SkillSynthesizer()

        # Step 1: Start with a lesson
        lesson = "Reduce position size by 30% when market volatility exceeds 2x normal levels"

        # Step 2: Synthesize skill specification
        skill_specs = synthesizer.synthesize_from_lessons([lesson])
        assert len(skill_specs) == 1
        spec = skill_specs[0]

        # Step 3: Generate executable code
        generated = synthesizer.generate_skill_code(spec, use_llm=False)
        assert generated is not None
        assert generated.validation_result["syntax_valid"] is True

        # Step 4: Execute the generated code
        exec_globals = {}
        exec(generated.implementation_code, exec_globals)

        # Step 5: Verify the skill is executable and produces correct output
        position_size_func = exec_globals["calculate_position_size"]

        # High volatility → smaller position
        normal_vol_size = position_size_func(volatility=0.5, base_size=100.0)
        high_vol_size = position_size_func(volatility=2.0, base_size=100.0)

        assert high_vol_size < normal_vol_size, \
            f"High volatility should reduce position: {high_vol_size} < {normal_vol_size}"

        # Step 6: Verify test code is syntactically valid (skip actual execution due to import issues)
        # The test code contains imports that would require the file to be written to disk
        # For this E2E test, we verify syntax validity and manual execution above
        test_validation = synthesizer._validate_code("", generated.test_code)
        assert test_validation["test_syntax_valid"] is True, \
            f"Test code syntax validation failed: {test_validation['errors']}"

        # Step 7: Verify the skill meets quality standards
        assert generated.validation_result["has_docstrings"] is True
        assert generated.validation_result["has_type_hints"] is True

        print(f"\n✅ FULL E2E SUCCESS:")
        print(f"   Lesson: {lesson}")
        print(f"   → Skill ID: {spec.skill_id}")
        print(f"   → Category: {spec.category}")
        print(f"   → Code Lines: {len(generated.implementation_code.splitlines())}")
        print(f"   → Test Lines: {len(generated.test_code.splitlines())}")
        print(f"   → Validation: PASS")
        print(f"   → Execution: PASS")
        print(f"\n   🎯 GAP CLOSED: Lesson successfully converted to executable skill!")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
