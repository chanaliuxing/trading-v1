#!/usr/bin/env python3
"""
Skill Synthesizer: Convert lessons_learned into executable skills (procedural knowledge).

This is the final gap closure that enables true autonomous growth:
- Lesson (declarative) → Skill (procedural)
- "System learns" → "System can do"

Flow:
1. Analyze lessons_learned from daily summary
2. Classify lesson type (risk_control, feature_engineering, execution, etc.)
3. Generate skill specification (name, type, file_path, test_path)
4. Generate code template using LLM
5. Validate generated skill (syntax, tests)
6. Add to skill approval queue
7. If approved → Register skill → Enable for evolution
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Dict, Any, List, Optional, Literal
from dataclasses import dataclass, asdict
from datetime import datetime, timezone

from libs.logger import get_logger

log = get_logger(__name__)


@dataclass
class LessonClassification:
    """Classification of a lesson into actionable categories"""
    lesson_text: str
    category: Literal["risk_control", "feature_engineering", "execution_timing",
                     "data_quality", "model_tuning", "system_optimization", "other"]
    confidence: float  # 0.0-1.0
    suggested_skill_type: Literal["PURE", "SIDE_EFFECT"]
    rationale: str


@dataclass
class SkillSpecification:
    """Specification for a new skill to be synthesized"""
    skill_id: str  # e.g., "risk_volatility_reducer_v1"
    name: str
    description: str
    category: str
    skill_type: Literal["PURE", "SIDE_EFFECT"]
    file_path: str  # e.g., "libs/skills/generated/risk_volatility_reducer_v1.py"
    test_path: str
    allow_evolution: bool
    objective: str  # "PERFORMANCE", "CORRECTNESS", "DO_NOT_TOUCH"
    metrics_impact: List[str]
    source_lesson: str
    generated_at: str
    status: Literal["pending_approval", "approved", "rejected", "active", "deprecated"]


@dataclass
class GeneratedSkillCode:
    """Generated code for a skill"""
    skill_spec: SkillSpecification
    implementation_code: str
    test_code: str
    validation_result: Dict[str, Any]


class SkillSynthesizer:
    """
    Core synthesizer that converts lessons into executable skills.
    """

    def __init__(self, llm_provider: Optional[Any] = None):
        self.llm_provider = llm_provider
        self.approval_queue_path = Path("runs/memory/skill_approval_queue.jsonl")
        self.approval_queue_path.parent.mkdir(parents=True, exist_ok=True)

    def synthesize_from_lessons(
        self,
        lessons: List[str],
        context: Optional[Dict[str, Any]] = None
    ) -> List[SkillSpecification]:
        """
        Main entry point: Convert list of lessons into skill specifications.

        Args:
            lessons: List of lesson strings from daily summary
            context: Optional context (statistics, event types, etc.)

        Returns:
            List of skill specifications (pending approval)
        """
        if not lessons:
            return []

        log.info("synthesizing_skills_from_lessons", num_lessons=len(lessons))

        skill_specs: List[SkillSpecification] = []

        for lesson in lessons:
            # Step 1: Classify lesson
            classification = self._classify_lesson(lesson, context)
            log.debug("lesson_classified", lesson=lesson[:100], category=classification.category,
                      confidence=classification.confidence)

            # Step 2: Check if lesson is actionable
            if classification.category == "other" or classification.confidence < 0.5:
                log.debug("lesson_not_actionable", lesson=lesson[:100],
                         category=classification.category, confidence=classification.confidence)
                continue

            # Step 3: Generate skill specification
            skill_spec = self._generate_skill_spec(lesson, classification, context)
            log.debug("skill_spec_generated", skill_id=skill_spec.skill_id if skill_spec else "None")

            if skill_spec:
                skill_specs.append(skill_spec)

                # Add to approval queue
                self._add_to_approval_queue(skill_spec)

                log.info("skill_synthesized", skill_id=skill_spec.skill_id,
                        category=skill_spec.category, status=skill_spec.status)

        return skill_specs

    def _classify_lesson(
        self,
        lesson: str,
        context: Optional[Dict[str, Any]]
    ) -> LessonClassification:
        """
        Classify lesson into actionable categories.

        Uses simple heuristics (can be enhanced with LLM later).
        """
        lesson_lower = lesson.lower()

        # Heuristic classification (order matters - more specific checks first)
        # Check data quality first (specific patterns)
        if any(kw in lesson_lower for kw in ["data quality", "missing data", "sync", "stale"]):
            return LessonClassification(
                lesson_text=lesson,
                category="data_quality",
                confidence=0.85,
                suggested_skill_type="SIDE_EFFECT",
                rationale="Contains data quality keywords"
            )
        # Check model tuning (specific patterns, avoid "model failures" → use improve/accuracy)
        elif any(kw in lesson_lower for kw in ["improve model", "model accuracy", "model auc", "model ic", "model prediction", "tune model", "hyperparameter"]):
            return LessonClassification(
                lesson_text=lesson,
                category="model_tuning",
                confidence=0.7,
                suggested_skill_type="PURE",
                rationale="Contains model tuning keywords"
            )
        elif any(kw in lesson_lower for kw in ["volatility", "risk", "position size", "stop", "loss"]):
            return LessonClassification(
                lesson_text=lesson,
                category="risk_control",
                confidence=0.8,
                suggested_skill_type="PURE",
                rationale="Contains risk management keywords"
            )
        elif any(kw in lesson_lower for kw in ["feature", "indicator", "signal", "momentum", "trend"]):
            return LessonClassification(
                lesson_text=lesson,
                category="feature_engineering",
                confidence=0.75,
                suggested_skill_type="PURE",
                rationale="Contains feature engineering keywords"
            )
        elif any(kw in lesson_lower for kw in ["timing", "entry", "exit", "execution", "fill", "wait", "patient", "price action"]):
            return LessonClassification(
                lesson_text=lesson,
                category="execution_timing",
                confidence=0.75,
                suggested_skill_type="SIDE_EFFECT",
                rationale="Contains execution timing keywords"
            )
        # Check system optimization
        elif any(kw in lesson_lower for kw in ["test events", "system readiness", "system health", "monitor system"]):
            return LessonClassification(
                lesson_text=lesson,
                category="system_optimization",
                confidence=0.7,
                suggested_skill_type="SIDE_EFFECT",
                rationale="Contains system optimization keywords"
            )
        else:
            return LessonClassification(
                lesson_text=lesson,
                category="other",
                confidence=0.3,
                suggested_skill_type="PURE",
                rationale="No clear category match"
            )

    def _generate_skill_spec(
        self,
        lesson: str,
        classification: LessonClassification,
        context: Optional[Dict[str, Any]]
    ) -> Optional[SkillSpecification]:
        """
        Generate skill specification from classified lesson.
        """
        # Generate unique skill ID
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        skill_id = f"{classification.category}_{timestamp}_{uuid.uuid4().hex[:6]}"

        # Determine file paths based on category
        if classification.category == "risk_control":
            base_dir = "libs/skills/generated/risk"
        elif classification.category == "feature_engineering":
            base_dir = "libs/skills/generated/features"
        elif classification.category == "execution_timing":
            base_dir = "libs/skills/generated/execution"
        elif classification.category == "data_quality":
            base_dir = "libs/skills/generated/data"
        elif classification.category == "model_tuning":
            base_dir = "libs/skills/generated/models"
        else:
            base_dir = "libs/skills/generated/misc"

        file_path = f"{base_dir}/{skill_id}.py"
        test_path = f"tests/unit/skills/generated/test_{skill_id}.py"

        # Determine metrics impact based on category
        metrics_impact = []
        if classification.category == "risk_control":
            metrics_impact = ["sharpe", "drawdown", "max_loss"]
        elif classification.category == "feature_engineering":
            metrics_impact = ["ic", "auc", "prediction_accuracy"]
        elif classification.category == "execution_timing":
            metrics_impact = ["slippage", "fill_rate", "execution_cost"]

        # Determine evolution settings
        allow_evolution = classification.category not in ["data_quality", "system_optimization"]
        objective = "PERFORMANCE" if allow_evolution else "CORRECTNESS"

        # Generate human-readable name
        name = self._generate_skill_name(lesson, classification)

        spec = SkillSpecification(
            skill_id=skill_id,
            name=name,
            description=f"Auto-generated skill from lesson: {lesson[:200]}",
            category=classification.category,
            skill_type=classification.suggested_skill_type,
            file_path=file_path,
            test_path=test_path,
            allow_evolution=allow_evolution,
            objective=objective,
            metrics_impact=metrics_impact,
            source_lesson=lesson,
            generated_at=datetime.now(timezone.utc).isoformat(),
            status="pending_approval"
        )

        return spec

    def _generate_skill_name(self, lesson: str, classification: LessonClassification) -> str:
        """Generate human-readable skill name from lesson"""
        # Extract key action words
        lesson_lower = lesson.lower()

        if "reduce" in lesson_lower and "volatility" in lesson_lower:
            return "Volatility-Aware Position Sizer"
        elif "wider stop" in lesson_lower:
            return "Adaptive Stop Width Calculator"
        elif "avoid" in lesson_lower and "trading" in lesson_lower:
            return "Market Condition Filter"
        elif "patient" in lesson_lower or "wait" in lesson_lower:
            return "Entry Timing Optimizer"
        else:
            # Generic name based on category
            return f"{classification.category.replace('_', ' ').title()} Skill"

    def _add_to_approval_queue(self, skill_spec: SkillSpecification) -> None:
        """Add skill specification to approval queue"""
        try:
            with self.approval_queue_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(skill_spec), ensure_ascii=False) + "\n")
            log.info("skill_added_to_approval_queue", skill_id=skill_spec.skill_id)
        except Exception as e:
            log.error("failed_to_add_to_approval_queue", skill_id=skill_spec.skill_id, error=str(e))

    def generate_skill_code(
        self,
        skill_spec: SkillSpecification,
        use_llm: bool = True
    ) -> Optional[GeneratedSkillCode]:
        """
        Generate actual code for a skill specification.

        Args:
            skill_spec: Skill specification
            use_llm: Whether to use LLM for code generation (fallback to templates)

        Returns:
            GeneratedSkillCode with implementation and tests
        """
        if use_llm and self.llm_provider:
            return self._generate_code_with_llm(skill_spec)
        else:
            return self._generate_code_with_template(skill_spec)

    def _generate_code_with_template(self, skill_spec: SkillSpecification) -> GeneratedSkillCode:
        """Generate code using predefined templates (safe fallback)"""

        # Template based on category
        if skill_spec.category == "risk_control":
            impl_code = self._template_risk_control(skill_spec)
            test_code = self._template_risk_control_test(skill_spec)
        elif skill_spec.category == "feature_engineering":
            impl_code = self._template_feature_engineering(skill_spec)
            test_code = self._template_feature_engineering_test(skill_spec)
        else:
            impl_code = self._template_generic(skill_spec)
            test_code = self._template_generic_test(skill_spec)

        # Validate syntax
        validation = self._validate_code(impl_code, test_code)

        return GeneratedSkillCode(
            skill_spec=skill_spec,
            implementation_code=impl_code,
            test_code=test_code,
            validation_result=validation
        )

    def _template_risk_control(self, spec: SkillSpecification) -> str:
        """Template for risk control skills"""
        return f'''#!/usr/bin/env python3
"""
{spec.name}

Auto-generated from lesson: {spec.source_lesson}
Category: {spec.category}
Generated: {spec.generated_at}
"""
from typing import Dict, Any


def calculate_position_size(
    volatility: float,
    base_size: float = 100.0,
    max_size: float = 1000.0,
    min_size: float = 10.0
) -> float:
    """
    Calculate position size based on volatility.

    Args:
        volatility: Market volatility (e.g., ATR, historical volatility)
        base_size: Base position size
        max_size: Maximum position size
        min_size: Minimum position size

    Returns:
        Adjusted position size
    """
    if volatility <= 0:
        return base_size

    # Inverse relationship: higher volatility → smaller position
    adjusted_size = base_size / (1 + volatility)

    # Clamp to min/max
    return max(min_size, min(adjusted_size, max_size))


def calculate_stop_width(
    volatility: float,
    base_width: float = 0.02,
    min_width: float = 0.01,
    max_width: float = 0.05
) -> float:
    """
    Calculate stop width based on volatility.

    Args:
        volatility: Market volatility
        base_width: Base stop width (as fraction of price)
        min_width: Minimum stop width
        max_width: Maximum stop width

    Returns:
        Adjusted stop width
    """
    if volatility <= 0:
        return base_width

    # Direct relationship: higher volatility → wider stop
    adjusted_width = base_width * (1 + volatility)

    # Clamp to min/max
    return max(min_width, min(adjusted_width, max_width))
'''

    def _template_risk_control_test(self, spec: SkillSpecification) -> str:
        """Template for risk control skill tests"""
        module_path = spec.file_path.replace("/", ".").replace(".py", "")
        return f'''#!/usr/bin/env python3
"""
Tests for {spec.name}

Generated: {spec.generated_at}
"""
import pytest
from {module_path} import calculate_position_size, calculate_stop_width


def test_position_size_normal_volatility():
    """Test position sizing with normal volatility"""
    size = calculate_position_size(volatility=0.5, base_size=100.0)
    assert 50 < size < 100, f"Expected size between 50-100, got {{size}}"


def test_position_size_high_volatility():
    """Test position sizing with high volatility"""
    size = calculate_position_size(volatility=2.0, base_size=100.0)
    assert size < 50, f"Expected size < 50 for high volatility, got {{size}}"


def test_position_size_zero_volatility():
    """Test position sizing with zero volatility"""
    size = calculate_position_size(volatility=0.0, base_size=100.0)
    assert size == 100.0, f"Expected base_size (100.0), got {{size}}"


def test_position_size_clamping():
    """Test position size clamping to min/max"""
    # Test max clamping
    size = calculate_position_size(volatility=0.01, base_size=1500.0, max_size=1000.0)
    assert size <= 1000.0, f"Expected size <= 1000.0, got {{size}}"

    # Test min clamping
    size = calculate_position_size(volatility=10.0, base_size=100.0, min_size=10.0)
    assert size >= 10.0, f"Expected size >= 10.0, got {{size}}"


def test_stop_width_normal_volatility():
    """Test stop width with normal volatility"""
    width = calculate_stop_width(volatility=0.5, base_width=0.02)
    assert 0.02 < width < 0.05, f"Expected width between 0.02-0.05, got {{width}}"


def test_stop_width_high_volatility():
    """Test stop width with high volatility"""
    width = calculate_stop_width(volatility=2.0, base_width=0.02)
    assert width > 0.03, f"Expected width > 0.03 for high volatility, got {{width}}"


def test_stop_width_zero_volatility():
    """Test stop width with zero volatility"""
    width = calculate_stop_width(volatility=0.0, base_width=0.02)
    assert width == 0.02, f"Expected base_width (0.02), got {{width}}"
'''

    def _template_feature_engineering(self, spec: SkillSpecification) -> str:
        """Template for feature engineering skills"""
        return f'''#!/usr/bin/env python3
"""
{spec.name}

Auto-generated from lesson: {spec.source_lesson}
Category: {spec.category}
Generated: {spec.generated_at}
"""
import numpy as np
from typing import Optional


def calculate_feature(
    prices: np.ndarray,
    volumes: Optional[np.ndarray] = None,
    window: int = 20
) -> np.ndarray:
    """
    Calculate custom feature from price and volume data.

    Args:
        prices: Array of prices
        volumes: Optional array of volumes
        window: Lookback window

    Returns:
        Feature array (same length as input)
    """
    if len(prices) < window:
        return np.full_like(prices, np.nan)

    # Example: Simple momentum indicator
    result = np.full_like(prices, np.nan, dtype=float)

    for i in range(window, len(prices)):
        pct_change = (prices[i] - prices[i - window]) / prices[i - window]
        result[i] = pct_change

    return result
'''

    def _template_feature_engineering_test(self, spec: SkillSpecification) -> str:
        """Template for feature engineering skill tests"""
        module_path = spec.file_path.replace("/", ".").replace(".py", "")
        return f'''#!/usr/bin/env python3
"""
Tests for {spec.name}

Generated: {spec.generated_at}
"""
import pytest
import numpy as np
from {module_path} import calculate_feature


def test_feature_calculation_basic():
    """Test basic feature calculation"""
    prices = np.array([100, 101, 102, 103, 104, 105])
    result = calculate_feature(prices, window=3)

    assert len(result) == len(prices)
    assert np.isnan(result[:3]).all(), "First window values should be NaN"
    assert not np.isnan(result[3:]).any(), "Remaining values should not be NaN"


def test_feature_calculation_short_input():
    """Test feature calculation with input shorter than window"""
    prices = np.array([100, 101])
    result = calculate_feature(prices, window=5)

    assert len(result) == len(prices)
    assert np.isnan(result).all(), "All values should be NaN when input < window"


def test_feature_calculation_increasing_prices():
    """Test feature with increasing prices"""
    prices = np.linspace(100, 120, 50)
    result = calculate_feature(prices, window=10)

    # Feature should be positive for increasing prices
    assert (result[10:] > 0).all(), "Feature should be positive for increasing prices"


def test_feature_calculation_decreasing_prices():
    """Test feature with decreasing prices"""
    prices = np.linspace(120, 100, 50)
    result = calculate_feature(prices, window=10)

    # Feature should be negative for decreasing prices
    assert (result[10:] < 0).all(), "Feature should be negative for decreasing prices"
'''

    def _template_generic(self, spec: SkillSpecification) -> str:
        """Generic template for other skill types"""
        return f'''#!/usr/bin/env python3
"""
{spec.name}

Auto-generated from lesson: {spec.source_lesson}
Category: {spec.category}
Generated: {spec.generated_at}
"""


def execute_skill(*args, **kwargs):
    """
    Generic skill execution placeholder.

    TODO: Implement specific logic based on lesson requirements.
    """
    raise NotImplementedError("Skill implementation pending manual review")
'''

    def _template_generic_test(self, spec: SkillSpecification) -> str:
        """Generic test template"""
        return f'''#!/usr/bin/env python3
"""
Tests for {spec.name}

Generated: {spec.generated_at}
"""
import pytest


def test_placeholder():
    """Placeholder test"""
    assert True, "Placeholder test - implement actual tests"
'''

    def _validate_code(self, impl_code: str, test_code: str) -> Dict[str, Any]:
        """Validate generated code (syntax, basic checks)"""
        validation = {
            "syntax_valid": False,
            "test_syntax_valid": False,
            "has_docstrings": False,
            "has_type_hints": False,
            "errors": []
        }

        # Check implementation syntax
        try:
            compile(impl_code, "<string>", "exec")
            validation["syntax_valid"] = True
        except SyntaxError as e:
            validation["errors"].append(f"Implementation syntax error: {str(e)}")

        # Check test syntax
        try:
            compile(test_code, "<string>", "exec")
            validation["test_syntax_valid"] = True
        except SyntaxError as e:
            validation["errors"].append(f"Test syntax error: {str(e)}")

        # Check for docstrings
        validation["has_docstrings"] = '"""' in impl_code or "'''" in impl_code

        # Check for type hints
        validation["has_type_hints"] = "->" in impl_code or ": " in impl_code

        return validation

    def _generate_code_with_llm(self, skill_spec: SkillSpecification) -> Optional[GeneratedSkillCode]:
        """
        Generate code using LLM (future enhancement).

        This would allow more sophisticated code generation based on:
        - Lesson context
        - Existing codebase patterns
        - Best practices
        """
        # TODO: Implement LLM-based code generation
        log.warning("llm_code_generation_not_implemented", skill_id=skill_spec.skill_id)
        return self._generate_code_with_template(skill_spec)


def get_pending_skills() -> List[SkillSpecification]:
    """Get all skills pending approval"""
    approval_queue_path = Path("runs/memory/skill_approval_queue.jsonl")

    if not approval_queue_path.exists():
        return []

    pending_skills = []
    with approval_queue_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                spec_data = json.loads(line)
                if spec_data.get("status") == "pending_approval":
                    pending_skills.append(SkillSpecification(**spec_data))
            except Exception:
                continue

    return pending_skills


def approve_skill(skill_id: str) -> bool:
    """
    Approve a skill and register it.

    Args:
        skill_id: ID of skill to approve

    Returns:
        True if approved successfully
    """
    approval_queue_path = Path("runs/memory/skill_approval_queue.jsonl")
    registry_path = Path("config/skill_registry.json")

    if not approval_queue_path.exists():
        return False

    # Find skill in approval queue
    skill_spec = None
    with approval_queue_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                spec_data = json.loads(line)
                if spec_data.get("skill_id") == skill_id:
                    skill_spec = SkillSpecification(**spec_data)
                    break
            except Exception:
                continue

    if not skill_spec:
        return False

    # Update status
    skill_spec.status = "approved"

    # Add to skill registry
    try:
        if registry_path.exists():
            registry = json.loads(registry_path.read_text())
        else:
            registry = {"skills": {}}

        registry["skills"][skill_spec.skill_id] = {
            "file_path": skill_spec.file_path,
            "test_path": skill_spec.test_path,
            "type": skill_spec.skill_type,
            "description": skill_spec.description,
            "metrics_impact": skill_spec.metrics_impact,
            "allow_evolution": skill_spec.allow_evolution,
            "objective": skill_spec.objective,
            "source_lesson": skill_spec.source_lesson,
            "generated_at": skill_spec.generated_at,
            "approved_at": datetime.now(timezone.utc).isoformat()
        }

        registry_path.write_text(json.dumps(registry, indent=2, ensure_ascii=False))

        log.info("skill_approved_and_registered", skill_id=skill_id)
        return True
    except Exception as e:
        log.error("failed_to_approve_skill", skill_id=skill_id, error=str(e))
        return False
