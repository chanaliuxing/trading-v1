# tests/unit/features/test_registry_v2.py

"""
Tests for Feature Registry v2 enhancements (UF 17).

Tests new constraint-based filtering: task/label/horizon validation.
"""

import pytest

from features.registry import FeatureDef, FeatureRegistry


class TestFeatureDefV2:
    """Test FeatureDef v2 fields and methods."""

    def test_supports_label_with_wildcard(self):
        """Feature with allowed_labels=["*"] supports all labels."""
        feat = FeatureDef(
            name="close",
            kind="native",
            freq=["1m"],
            base_cols=["close"],
            tags=["price"],
            desc="Close price",
            allowed_labels=["*"],
        )

        assert feat.supports_label("ret_10m") is True
        assert feat.supports_label("ret_30m") is True
        assert feat.supports_label("tb_10m_atr1") is True

    def test_supports_label_with_whitelist(self):
        """Feature with specific allowed_labels only supports those labels."""
        feat = FeatureDef(
            name="special_feature",
            kind="python",
            freq=["1m"],
            base_cols=["close"],
            tags=["special"],
            desc="Special feature",
            allowed_labels=["ret_10m", "ret_30m"],
        )

        assert feat.supports_label("ret_10m") is True
        assert feat.supports_label("ret_30m") is True
        assert feat.supports_label("tb_10m_atr1") is False

    def test_supports_label_no_restriction(self):
        """Feature with no allowed_labels supports all labels."""
        feat = FeatureDef(
            name="volume",
            kind="native",
            freq=["1m"],
            base_cols=["volume"],
            tags=["volume"],
            desc="Volume",
            allowed_labels=None,
        )

        assert feat.supports_label("ret_10m") is True
        assert feat.supports_label("any_label") is True

    def test_supports_horizon_no_restriction(self):
        """Feature with no horizon constraints supports all horizons."""
        feat = FeatureDef(
            name="close",
            kind="native",
            freq=["1m"],
            base_cols=["close"],
            tags=["price"],
            desc="Close price",
            horizon_min=None,
            horizon_max=None,
        )

        assert feat.supports_horizon(None) is True
        assert feat.supports_horizon(1) is True
        assert feat.supports_horizon(1000) is True

    def test_supports_horizon_with_min(self):
        """Feature with horizon_min only allows horizons >= min."""
        feat = FeatureDef(
            name="ma_20",
            kind="python",
            freq=["1m"],
            base_cols=["close"],
            tags=["trend"],
            desc="20-bar MA",
            horizon_min=20,
        )

        assert feat.supports_horizon(None) is True
        assert feat.supports_horizon(10) is False
        assert feat.supports_horizon(20) is True
        assert feat.supports_horizon(30) is True

    def test_supports_horizon_with_max(self):
        """Feature with horizon_max only allows horizons <= max."""
        feat = FeatureDef(
            name="ret_1b",
            kind="python",
            freq=["1m"],
            base_cols=["close"],
            tags=["return"],
            desc="1-bar return",
            horizon_max=60,
        )

        assert feat.supports_horizon(None) is True
        assert feat.supports_horizon(30) is True
        assert feat.supports_horizon(60) is True
        assert feat.supports_horizon(100) is False

    def test_supports_horizon_with_range(self):
        """Feature with both min and max enforces range."""
        feat = FeatureDef(
            name="vwap_5m",
            kind="python",
            freq=["1m"],
            base_cols=["close", "volume"],
            tags=["price"],
            desc="5-minute VWAP",
            horizon_min=5,
            horizon_max=120,
        )

        assert feat.supports_horizon(None) is True
        assert feat.supports_horizon(1) is False
        assert feat.supports_horizon(5) is True
        assert feat.supports_horizon(60) is True
        assert feat.supports_horizon(120) is True
        assert feat.supports_horizon(200) is False

    def test_supports_task_no_restriction(self):
        """Feature with no allowed_tasks supports all tasks."""
        feat = FeatureDef(
            name="close",
            kind="native",
            freq=["1m"],
            base_cols=["close"],
            tags=["price"],
            desc="Close price",
            allowed_tasks=None,
        )

        assert feat.supports_task("regression") is True
        assert feat.supports_task("classification") is True

    def test_supports_task_with_whitelist(self):
        """Feature with allowed_tasks only supports those tasks."""
        feat = FeatureDef(
            name="reg_only_feature",
            kind="python",
            freq=["1m"],
            base_cols=["close"],
            tags=["special"],
            desc="Regression-only feature",
            allowed_tasks=["regression"],
        )

        assert feat.supports_task("regression") is True
        assert feat.supports_task("classification") is False


class TestFilterForTaskAndLabel:
    """Test FeatureRegistry.filter_for_task_and_label() method."""

    def setup_method(self):
        """Create test registry with various constraints."""
        features = {
            "close": FeatureDef(
                name="close",
                kind="native",
                freq=["1m"],
                base_cols=["close"],
                tags=["price"],
                desc="Close price",
                allowed_tasks=None,
                allowed_labels=["*"],
                horizon_min=None,
                horizon_max=None,
            ),
            "ret_1b": FeatureDef(
                name="ret_1b",
                kind="python",
                freq=["1m"],
                base_cols=["close"],
                tags=["return"],
                desc="1-bar return",
                allowed_tasks=["regression", "classification"],
                allowed_labels=["*"],
                horizon_min=1,
                horizon_max=60,
            ),
            "ma_20": FeatureDef(
                name="ma_20",
                kind="python",
                freq=["1m"],
                base_cols=["close"],
                tags=["trend"],
                desc="20-bar MA",
                allowed_tasks=["regression"],
                allowed_labels=["ret_10m", "ret_30m"],
                horizon_min=20,
                horizon_max=None,
            ),
            "tb_indicator": FeatureDef(
                name="tb_indicator",
                kind="python",
                freq=["1m"],
                base_cols=["close", "volume"],
                tags=["classification"],
                desc="Triple barrier indicator",
                allowed_tasks=["classification"],
                allowed_labels=["tb_10m_atr1", "tb_30m_atr1"],
                horizon_min=None,
                horizon_max=None,
            ),
        }
        self.registry = FeatureRegistry(features, version="2.0")

    def test_filter_all_allowed(self):
        """All features pass constraints."""
        allowed, rejected = self.registry.filter_for_task_and_label(
            candidate_features=["close", "ret_1b"],
            task="regression",
            label_id="ret_10m",
            horizon_bars=30,
        )

        assert allowed == ["close", "ret_1b"]
        assert rejected == []

    def test_filter_task_rejection(self):
        """Feature rejected due to task constraint."""
        allowed, rejected = self.registry.filter_for_task_and_label(
            candidate_features=["close", "ma_20", "tb_indicator"],
            task="classification",
            label_id="tb_10m_atr1",
            horizon_bars=10,
        )

        assert "close" in allowed
        assert "tb_indicator" in allowed
        assert "ma_20" not in allowed
        assert any("ma_20" in r and "task=classification" in r for r in rejected)

    def test_filter_label_rejection(self):
        """Feature rejected due to label constraint."""
        allowed, rejected = self.registry.filter_for_task_and_label(
            candidate_features=["close", "ma_20"],
            task="regression",
            label_id="ret_1h",  # Not in ma_20's allowed_labels
            horizon_bars=60,
        )

        assert "close" in allowed
        assert "ma_20" not in allowed
        assert any("ma_20" in r and "label=ret_1h" in r for r in rejected)

    def test_filter_horizon_rejection(self):
        """Feature rejected due to horizon constraint."""
        allowed, rejected = self.registry.filter_for_task_and_label(
            candidate_features=["close", "ret_1b"],
            task="regression",
            label_id="ret_10m",
            horizon_bars=100,  # Exceeds ret_1b's horizon_max=60
        )

        assert "close" in allowed
        assert "ret_1b" not in allowed
        assert any("ret_1b" in r and "horizon=100" in r for r in rejected)

    def test_filter_not_in_registry(self):
        """Unknown feature is rejected."""
        allowed, rejected = self.registry.filter_for_task_and_label(
            candidate_features=["close", "unknown_feature"],
            task="regression",
            label_id="ret_10m",
            horizon_bars=10,
        )

        assert "close" in allowed
        assert "unknown_feature" not in allowed
        assert any("unknown_feature" in r and "not in registry" in r for r in rejected)

    def test_filter_multiple_rejections(self):
        """Multiple features rejected for different reasons."""
        allowed, rejected = self.registry.filter_for_task_and_label(
            candidate_features=["close", "ret_1b", "ma_20", "tb_indicator", "fake"],
            task="classification",
            label_id="tb_10m_atr1",
            horizon_bars=10,
        )

        assert "close" in allowed
        assert "tb_indicator" in allowed
        assert "ret_1b" in allowed  # ret_1b supports classification
        # ma_20: wrong task (only supports regression)
        # fake: not in registry
        assert len(rejected) >= 2
        assert any("ma_20" in r for r in rejected)
        assert any("fake" in r for r in rejected)

    def test_filter_empty_candidates(self):
        """Empty candidate list returns empty results."""
        allowed, rejected = self.registry.filter_for_task_and_label(
            candidate_features=[],
            task="regression",
            label_id="ret_10m",
            horizon_bars=10,
        )

        assert allowed == []
        assert rejected == []


class TestPIIntegration:
    """Test Feature Registry integration with PI orchestrator."""

    def test_registry_filters_llm_features(self):
        """PI should filter LLM-proposed features through Registry."""
        # Create a registry with specific constraints
        features = {
            "close": FeatureDef(
                name="close",
                kind="native",
                freq=["1m"],
                base_cols=["close"],
                tags=["price"],
                desc="Close price",
                allowed_tasks=None,  # All tasks allowed
                allowed_labels=["*"],  # All labels allowed
                horizon_min=None,
                horizon_max=None,
            ),
            "ma_20": FeatureDef(
                name="ma_20",
                kind="python",
                freq=["1m"],
                base_cols=["close"],
                tags=["trend"],
                desc="20-bar MA",
                allowed_tasks=["regression"],  # Only regression
                allowed_labels=["ret_10m", "ret_30m"],  # Specific labels only
                horizon_min=20,  # Min horizon 20 bars
                horizon_max=None,
            ),
        }
        registry = FeatureRegistry(features, version="2.0")

        # Simulate LLM proposing features
        llm_proposed = ["close", "ma_20", "non_existent_feature"]

        # Test 1: Valid regression task with correct label and horizon
        allowed, rejected = registry.filter_for_task_and_label(
            candidate_features=llm_proposed,
            task="regression",
            label_id="ret_10m",
            horizon_bars=30,  # > 20, OK
        )

        assert "close" in allowed
        assert "ma_20" in allowed
        assert "non_existent_feature" not in allowed
        assert any("non_existent_feature" in r for r in rejected)

        # Test 2: Classification task should reject ma_20
        allowed, rejected = registry.filter_for_task_and_label(
            candidate_features=llm_proposed,
            task="classification",
            label_id="ret_10m",
            horizon_bars=30,
        )

        assert "close" in allowed
        assert "ma_20" not in allowed  # Rejected due to task
        assert any("ma_20" in r and "task=classification" in r for r in rejected)

        # Test 3: Wrong label should reject ma_20
        allowed, rejected = registry.filter_for_task_and_label(
            candidate_features=llm_proposed,
            task="regression",
            label_id="ret_1h",  # Not in allowed_labels
            horizon_bars=30,
        )

        assert "close" in allowed
        assert "ma_20" not in allowed  # Rejected due to label
        assert any("ma_20" in r and "label=ret_1h" in r for r in rejected)

        # Test 4: Insufficient horizon should reject ma_20
        allowed, rejected = registry.filter_for_task_and_label(
            candidate_features=llm_proposed,
            task="regression",
            label_id="ret_10m",
            horizon_bars=10,  # < 20, NOT OK
        )

        assert "close" in allowed
        assert "ma_20" not in allowed  # Rejected due to horizon
        assert any("ma_20" in r and "horizon=10" in r for r in rejected)
