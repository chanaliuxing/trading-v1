#!/usr/bin/env python3
"""
Predictive Immunity System - Risk Predictor

This module implements "Predictive Immunity" - the ability to detect and prevent
system failures BEFORE they happen, rather than reacting after the fact.

Key Concept:
- Traditional systems: Error occurs → Log → Alert → Manual fix
- Predictive Immunity: Detect anomalies → Predict risk → Auto-freeze → Prevent error

Design Philosophy:
- Simple statistical models (no complex ML needed for MVP)
- Real-time monitoring of key metrics
- Automatic throttling of risky operations
- Graceful degradation under stress

Monitored Metrics:
1. Error rate (recent spike = high risk)
2. Latency (P99 degradation = system stress)
3. Memory usage (approaching limit = crash risk)
4. Test failures (code quality degradation)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from datetime import datetime, timezone
from collections import deque
import statistics

logger = logging.getLogger(__name__)


@dataclass
class RiskScore:
    """Risk assessment result"""
    overall_score: float  # 0.0 - 1.0 (0 = safe, 1 = critical)
    level: str  # "safe", "warning", "danger", "critical"
    components: Dict[str, float]  # Individual metric contributions
    recommendations: List[str]  # Suggested actions
    timestamp: str


class RiskPredictor:
    """
    Statistical risk predictor using sliding window analysis.

    Methods:
    - ingest_metric(): Feed real-time metrics
    - predict_risk_score(): Calculate current risk level
    - should_freeze_exploration(): Check if exploration should stop
    - should_freeze_evolution(): Check if code evolution should stop
    """

    def __init__(
        self,
        window_size: int = 100,
        thresholds: Optional[Dict[str, float]] = None
    ):
        """
        Initialize risk predictor.

        Args:
            window_size: Number of recent samples to analyze
            thresholds: Risk thresholds from config.yml
        """
        # Sliding windows for different metrics
        self.error_window: deque = deque(maxlen=window_size)
        self.latency_window: deque = deque(maxlen=window_size)
        self.memory_usage_window: deque = deque(maxlen=window_size)
        self.test_failure_window: deque = deque(maxlen=window_size)

        # Risk thresholds
        self.thresholds = thresholds or {
            "freeze_exploration": 0.7,
            "freeze_evolution": 0.8,
            "freeze_all": 0.9
        }

        # Counters for rate calculation
        self.total_operations = 0
        self.recent_errors = 0

        logger.info(f"RiskPredictor initialized with window_size={window_size}")

    def ingest_metric(self, metric_type: str, value: float, metadata: Optional[Dict] = None):
        """
        Ingest a new metric sample.

        Args:
            metric_type: Type of metric ("error_rate", "latency_p99", "memory_usage", "test_failure")
            value: Metric value
            metadata: Optional metadata (timestamp, context, etc.)

        Example:
            >>> predictor.ingest_metric("latency_p99", 250.5)  # 250.5ms
            >>> predictor.ingest_metric("error_rate", 0.05)     # 5% error rate
        """
        try:
            if metric_type == "error_rate":
                self.error_window.append(value)
            elif metric_type == "latency_p99":
                self.latency_window.append(value)
            elif metric_type == "memory_usage":
                self.memory_usage_window.append(value)
            elif metric_type == "test_failure":
                self.test_failure_window.append(value)
            else:
                logger.warning(f"Unknown metric type: {metric_type}")

        except Exception as e:
            logger.error(f"Failed to ingest metric {metric_type}: {e}")

    def predict_risk_score(self) -> RiskScore:
        """
        Calculate overall risk score based on recent metrics.

        Returns:
            RiskScore object with overall score and breakdown

        Risk Calculation Logic:
        1. Error rate spike: +0.4 if recent > baseline * 2
        2. Latency degradation: +0.3 if recent > baseline * 1.5
        3. Memory pressure: +0.2 if usage > 80%
        4. Test failures: +0.3 if failure rate > 10%
        """
        components = {}
        overall = 0.0
        recommendations = []

        # 1. Check error rate
        if len(self.error_window) > 10:
            recent_errors = list(self.error_window)[-5:]
            baseline_errors = list(self.error_window)[:-5]

            recent_avg = statistics.mean(recent_errors) if recent_errors else 0
            baseline_avg = statistics.mean(baseline_errors) if baseline_errors else 0

            if baseline_avg > 0 and recent_avg > baseline_avg * 2:
                error_risk = 0.4
                overall += error_risk
                components["error_spike"] = error_risk
                recommendations.append("Error rate spiking - consider rolling back recent changes")
            elif recent_avg > 0.1:  # 10% error rate is always risky
                error_risk = 0.3
                overall += error_risk
                components["error_rate"] = error_risk
                recommendations.append("High error rate detected - investigate logs")

        # 2. Check latency degradation
        if len(self.latency_window) > 10:
            recent_latency = list(self.latency_window)[-5:]
            baseline_latency = list(self.latency_window)[:-5]

            if recent_latency and baseline_latency:
                recent_avg = statistics.mean(recent_latency)
                baseline_avg = statistics.mean(baseline_latency)

                if baseline_avg > 0 and recent_avg > baseline_avg * 1.5:
                    latency_risk = 0.3
                    overall += latency_risk
                    components["latency_degradation"] = latency_risk
                    recommendations.append(f"Latency increased {recent_avg/baseline_avg:.1f}x - system under stress")

        # 3. Check memory pressure
        if len(self.memory_usage_window) > 0:
            recent_memory = list(self.memory_usage_window)[-5:]
            if recent_memory:
                avg_memory = statistics.mean(recent_memory)
                if avg_memory > 0.8:  # 80% memory usage
                    memory_risk = 0.2 + (avg_memory - 0.8) * 1.0  # Scale up to 0.4
                    overall += memory_risk
                    components["memory_pressure"] = memory_risk
                    recommendations.append(f"Memory usage at {avg_memory*100:.0f}% - approaching limits")

        # 4. Check test failures
        if len(self.test_failure_window) > 5:
            recent_failures = list(self.test_failure_window)[-5:]
            failure_rate = sum(1 for f in recent_failures if f > 0) / len(recent_failures)

            if failure_rate > 0.1:  # 10% failure rate
                test_risk = 0.3
                overall += test_risk
                components["test_failures"] = test_risk
                recommendations.append(f"Test failure rate at {failure_rate*100:.0f}% - code quality degrading")

        # Cap overall score at 1.0
        overall = min(overall, 1.0)

        # Determine risk level
        if overall >= 0.9:
            level = "critical"
        elif overall >= 0.7:
            level = "danger"
        elif overall >= 0.5:
            level = "warning"
        else:
            level = "safe"

        return RiskScore(
            overall_score=overall,
            level=level,
            components=components,
            recommendations=recommendations,
            timestamp=datetime.now(timezone.utc).isoformat()
        )

    def should_freeze_exploration(self) -> bool:
        """
        Check if exploration experiments should be frozen.

        Exploration = risky experiments (new strategies, aggressive params)
        Freeze when risk >= threshold to prevent making things worse.

        Returns:
            True if exploration should be frozen
        """
        risk = self.predict_risk_score()
        threshold = self.thresholds.get("freeze_exploration", 0.7)

        if risk.overall_score >= threshold:
            logger.warning(f"🚫 Freezing exploration: risk={risk.overall_score:.2f} >= {threshold}")
            return True

        return False

    def should_freeze_evolution(self) -> bool:
        """
        Check if code/policy evolution should be frozen.

        Evolution = code patches, policy updates
        Freeze when risk is high to prevent cascading failures.

        Returns:
            True if evolution should be frozen
        """
        risk = self.predict_risk_score()
        threshold = self.thresholds.get("freeze_evolution", 0.8)

        if risk.overall_score >= threshold:
            logger.warning(f"🚫 Freezing evolution: risk={risk.overall_score:.2f} >= {threshold}")
            return True

        return False

    def should_freeze_all(self) -> bool:
        """
        Check if all autonomous operations should be frozen.

        Emergency freeze - system is in critical state.
        Only manual operations allowed.

        Returns:
            True if all operations should be frozen
        """
        risk = self.predict_risk_score()
        threshold = self.thresholds.get("freeze_all", 0.9)

        if risk.overall_score >= threshold:
            logger.critical(f"🛑 EMERGENCY FREEZE: risk={risk.overall_score:.2f} >= {threshold}")
            logger.critical(f"Recommendations: {risk.recommendations}")
            return True

        return False

    def get_status_summary(self) -> Dict:
        """
        Get current risk status summary.

        Returns:
            Dict with risk score and operational status
        """
        risk = self.predict_risk_score()

        return {
            "risk_score": risk.overall_score,
            "risk_level": risk.level,
            "exploration_allowed": not self.should_freeze_exploration(),
            "evolution_allowed": not self.should_freeze_evolution(),
            "emergency_freeze": self.should_freeze_all(),
            "components": risk.components,
            "recommendations": risk.recommendations,
            "timestamp": risk.timestamp,
            "window_stats": {
                "error_samples": len(self.error_window),
                "latency_samples": len(self.latency_window),
                "memory_samples": len(self.memory_usage_window),
                "test_samples": len(self.test_failure_window)
            }
        }

    def reset(self):
        """Reset all windows (useful for testing or after recovery)"""
        self.error_window.clear()
        self.latency_window.clear()
        self.memory_usage_window.clear()
        self.test_failure_window.clear()
        logger.info("RiskPredictor reset")


def get_risk_predictor_from_config(config: Optional[Dict] = None) -> RiskPredictor:
    """
    Load risk predictor from config.yml.

    Args:
        config: Config dict (or load from config.yml if None)

    Returns:
        RiskPredictor instance
    """
    if config is None:
        import yaml
        from pathlib import Path
        config_path = Path("config.yml")
        if config_path.exists():
            with open(config_path) as f:
                config = yaml.safe_load(f)
        else:
            config = {}

    immunity_config = config.get("immunity", {})

    window_size = immunity_config.get("lookback_window_size", 100)
    thresholds = immunity_config.get("thresholds", {
        "freeze_exploration": 0.7,
        "freeze_evolution": 0.8,
        "freeze_all": 0.9
    })

    return RiskPredictor(window_size=window_size, thresholds=thresholds)
