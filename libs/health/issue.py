"""
Issue and Diagnosis Protocols

Standard protocols for describing system problems and their diagnoses,
making them consumable by DataHealer and AI Brain.
"""

from typing import Dict, List, Any, Literal, Optional
from dataclasses import dataclass, asdict, field
from datetime import datetime
from enum import Enum


class IssueType(str, Enum):
    """Type of system issue"""
    DATA_MISSING = "data_missing"
    DATA_STALE = "data_stale"
    DATA_QUALITY_POOR = "data_quality_poor"
    CONFIG_MISMATCH = "config_mismatch"
    SCHEMA_MISMATCH = "schema_mismatch"
    PERFORMANCE_DEGRADED = "performance_degraded"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    CONNECTIVITY_FAILED = "connectivity_failed"
    UNKNOWN = "unknown"


class Severity(str, Enum):
    """Severity level of issue"""
    CRITICAL = "critical"  # Blocks production, requires immediate fix
    HIGH = "high"          # Degrades performance significantly
    MEDIUM = "medium"      # Minor degradation, can be scheduled
    LOW = "low"            # Cosmetic or informational


class RootCauseType(str, Enum):
    """Root cause categories for issues"""
    # Data issues
    DATA_PIPELINE_FAILED = "data_pipeline_failed"
    DATA_SOURCE_UNAVAILABLE = "data_source_unavailable"
    DATA_GENERATION_FAILED = "data_generation_failed"

    # Configuration issues
    CONFIG_SCHEMA_MISMATCH = "config_schema_mismatch"
    CONFIG_INVALID = "config_invalid"
    CONFIG_OUTDATED = "config_outdated"

    # Label/Feature issues
    LABEL_COLUMN_MISSING = "label_column_missing"
    LABEL_QUALITY_POOR = "label_quality_poor"
    FEATURE_NULL_HIGH = "feature_null_high"
    FEATURE_STALE = "feature_stale"

    # ML Training issues
    MODEL_OVERFITTING = "model_overfitting"
    MODEL_UNDERFITTING = "model_underfitting"
    TRAINING_DATA_INSUFFICIENT = "training_data_insufficient"
    TRAINING_DATA_LEAKAGE = "training_data_leakage"

    # Infrastructure issues
    DATABASE_CONNECTION_FAILED = "database_connection_failed"
    MEMORY_EXHAUSTED = "memory_exhausted"
    DISK_FULL = "disk_full"

    # Unknown
    UNKNOWN = "unknown"


@dataclass
class RootCause:
    """Root cause analysis result"""
    type: RootCauseType
    confidence: float  # 0.0 to 1.0
    evidence: Dict[str, Any]
    description: str

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for JSON serialization"""
        return {
            "type": self.type.value,
            "confidence": self.confidence,
            "evidence": self.evidence,
            "description": self.description
        }


@dataclass
class Issue:
    """
    Standard issue description for DataHealer consumption

    All diagnostic tools should output Issues in this format,
    making them easy for DataHealer to parse and act on.
    """
    issue_id: str
    timestamp: str
    component: str  # e.g., "bars", "features", "labels", "config"
    issue_type: IssueType
    severity: Severity

    title: str
    description: str

    affected_symbols: List[str] = field(default_factory=list)
    affected_tables: List[str] = field(default_factory=list)

    metrics: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for JSON serialization"""
        return {
            "issue_id": self.issue_id,
            "timestamp": self.timestamp,
            "component": self.component,
            "issue_type": self.issue_type.value,
            "severity": self.severity.value,
            "title": self.title,
            "description": self.description,
            "affected_symbols": self.affected_symbols,
            "affected_tables": self.affected_tables,
            "metrics": self.metrics,
            "metadata": self.metadata
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Issue":
        """Construct from dict"""
        return cls(
            issue_id=data["issue_id"],
            timestamp=data["timestamp"],
            component=data["component"],
            issue_type=IssueType(data["issue_type"]),
            severity=Severity(data["severity"]),
            title=data["title"],
            description=data["description"],
            affected_symbols=data.get("affected_symbols", []),
            affected_tables=data.get("affected_tables", []),
            metrics=data.get("metrics", {}),
            metadata=data.get("metadata", {})
        )


@dataclass
class Action:
    """
    Recommended action to fix an issue

    DataHealer can execute actions automatically based on action_type.
    """
    action_id: str
    action_type: str  # e.g., "generate_labels", "rebuild_features", "update_config"
    priority: int  # 1 = highest priority

    title: str
    description: str
    command: Optional[str] = None  # Shell command to execute

    # Parameters for programmatic execution
    params: Dict[str, Any] = field(default_factory=dict)

    # Prerequisites and constraints
    requires: List[str] = field(default_factory=list)  # Required actions first
    estimated_duration_sec: Optional[int] = None

    # Safety checks
    is_safe_auto_execute: bool = False  # Can DataHealer run automatically?
    requires_approval: bool = True      # Needs human approval?

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for JSON serialization"""
        return {
            "action_id": self.action_id,
            "action_type": self.action_type,
            "priority": self.priority,
            "title": self.title,
            "description": self.description,
            "command": self.command,
            "params": self.params,
            "requires": self.requires,
            "estimated_duration_sec": self.estimated_duration_sec,
            "is_safe_auto_execute": self.is_safe_auto_execute,
            "requires_approval": self.requires_approval
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Action":
        """Construct from dict"""
        return cls(
            action_id=data["action_id"],
            action_type=data["action_type"],
            priority=data["priority"],
            title=data["title"],
            description=data["description"],
            command=data.get("command"),
            params=data.get("params", {}),
            requires=data.get("requires", []),
            estimated_duration_sec=data.get("estimated_duration_sec"),
            is_safe_auto_execute=data.get("is_safe_auto_execute", False),
            requires_approval=data.get("requires_approval", True)
        )


@dataclass
class Diagnosis:
    """
    Complete diagnosis result with root cause analysis and recommended actions

    This is the standard output format for all diagnostic tools.
    DataHealer consumes Diagnosis objects to plan and execute fixes.
    """
    diagnosis_id: str
    timestamp: str

    # What was diagnosed
    target_component: str
    target_symbols: List[str]

    # Issues found
    issues: List[Issue]

    # Root cause analysis
    root_causes: List[RootCause]

    # Recommended fixes
    recommended_actions: List[Action]

    # Optional fields
    primary_root_cause: Optional[RootCause] = None
    summary: str = ""
    confidence: float = 0.0  # Overall diagnosis confidence (0.0 to 1.0)
    diagnostic_tool: str = ""  # Tool that generated this diagnosis
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for JSON serialization"""
        return {
            "diagnosis_id": self.diagnosis_id,
            "timestamp": self.timestamp,
            "target_component": self.target_component,
            "target_symbols": self.target_symbols,
            "issues": [issue.to_dict() for issue in self.issues],
            "root_causes": [rc.to_dict() for rc in self.root_causes],
            "primary_root_cause": self.primary_root_cause.to_dict() if self.primary_root_cause else None,
            "recommended_actions": [action.to_dict() for action in self.recommended_actions],
            "summary": self.summary,
            "confidence": self.confidence,
            "diagnostic_tool": self.diagnostic_tool,
            "metadata": self.metadata
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Diagnosis":
        """Construct from dict"""
        return cls(
            diagnosis_id=data["diagnosis_id"],
            timestamp=data["timestamp"],
            target_component=data["target_component"],
            target_symbols=data["target_symbols"],
            issues=[Issue.from_dict(i) for i in data.get("issues", [])],
            root_causes=[
                RootCause(
                    type=RootCauseType(rc["type"]),
                    confidence=rc["confidence"],
                    evidence=rc["evidence"],
                    description=rc["description"]
                )
                for rc in data.get("root_causes", [])
            ],
            primary_root_cause=RootCause(
                type=RootCauseType(data["primary_root_cause"]["type"]),
                confidence=data["primary_root_cause"]["confidence"],
                evidence=data["primary_root_cause"]["evidence"],
                description=data["primary_root_cause"]["description"]
            ) if data.get("primary_root_cause") else None,
            recommended_actions=[Action.from_dict(a) for a in data.get("recommended_actions", [])],
            summary=data.get("summary", ""),
            confidence=data.get("confidence", 0.0),
            diagnostic_tool=data.get("diagnostic_tool", ""),
            metadata=data.get("metadata", {})
        )

    def get_critical_issues(self) -> List[Issue]:
        """Get all critical severity issues"""
        return [issue for issue in self.issues if issue.severity == Severity.CRITICAL]

    def get_high_priority_actions(self) -> List[Action]:
        """Get actions with priority 1 or 2"""
        return sorted(
            [action for action in self.recommended_actions if action.priority <= 2],
            key=lambda a: a.priority
        )

    def get_auto_executable_actions(self) -> List[Action]:
        """Get actions that can be auto-executed by DataHealer"""
        return [
            action for action in self.recommended_actions
            if action.is_safe_auto_execute and not action.requires_approval
        ]


def make_issue_id(component: str, issue_type: IssueType) -> str:
    """Generate unique issue ID"""
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    return f"{component}_{issue_type.value}_{timestamp}"


def make_diagnosis_id(component: str) -> str:
    """Generate unique diagnosis ID"""
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    import random
    import string
    suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"diag_{component}_{timestamp}_{suffix}"


def make_action_id(action_type: str) -> str:
    """Generate unique action ID"""
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    return f"{action_type}_{timestamp}"


# Example usage and helper functions

def create_label_missing_diagnosis(
    symbol: str,
    horizon_min: int,
    expected_label: str,
    existing_labels: List[str]
) -> Diagnosis:
    """
    Helper function to create a standard diagnosis for missing label column

    This is an example of how diagnostic tools should structure their output.
    """

    # Create issue
    issue = Issue(
        issue_id=make_issue_id("labels", IssueType.SCHEMA_MISMATCH),
        timestamp=datetime.now().isoformat(),
        component="labels",
        issue_type=IssueType.SCHEMA_MISMATCH,
        severity=Severity.CRITICAL,
        title=f"Label column {expected_label} not found",
        description=f"Config requires {expected_label} for {symbol}, but only {existing_labels} exist in database",
        affected_symbols=[symbol],
        affected_tables=["trading.features_1m_with_labels"],
        metrics={
            "expected_label": expected_label,
            "existing_labels": existing_labels,
            "horizon_min": horizon_min
        }
    )

    # Root cause
    root_cause = RootCause(
        type=RootCauseType.LABEL_COLUMN_MISSING,
        confidence=0.95,
        evidence={
            "expected_label": expected_label,
            "existing_labels": existing_labels,
            "schema_query": f"SELECT name FROM system.columns WHERE table='features_1m_with_labels' AND name LIKE 'label%'"
        },
        description=f"Label column {expected_label} does not exist in database schema"
    )

    # Recommended actions
    action1 = Action(
        action_id=make_action_id("generate_label"),
        action_type="generate_label",
        priority=1,
        title=f"Generate {expected_label} column",
        description=f"Create {expected_label} column with {horizon_min}-minute forward return",
        command=f"venv/bin/python tools/generate_labels.py --horizon {horizon_min} --symbols {symbol}",
        params={
            "horizon_min": horizon_min,
            "symbols": [symbol],
            "label_col": expected_label
        },
        estimated_duration_sec=300,
        is_safe_auto_execute=True,
        requires_approval=False
    )

    action2 = Action(
        action_id=make_action_id("verify_label"),
        action_type="verify_label",
        priority=2,
        title="Verify label quality",
        description=f"Check quality of generated {expected_label}",
        command=f"venv/bin/python tools/diagnose_label_quality.py --symbols {symbol} --horizons {horizon_min}",
        requires=[action1.action_id],
        estimated_duration_sec=60,
        is_safe_auto_execute=True,
        requires_approval=False
    )

    # Create diagnosis
    diagnosis = Diagnosis(
        diagnosis_id=make_diagnosis_id("labels"),
        timestamp=datetime.now().isoformat(),
        target_component="labels",
        target_symbols=[symbol],
        issues=[issue],
        root_causes=[root_cause],
        primary_root_cause=root_cause,
        recommended_actions=[action1, action2],
        summary=f"Label column {expected_label} missing for {symbol}. Auto-fix available: generate column.",
        confidence=0.95,
        diagnostic_tool="check_ml_config_health"
    )

    return diagnosis


# Phase 3: Helper function to convert SystemHealthSnapshot to Diagnosis
def snapshot_to_diagnosis(snapshot: "SystemHealthSnapshot") -> Diagnosis:
    """
    Convert SystemHealthSnapshot (Phase 1) to Diagnosis (Phase 3)

    This bridges Phase 1 health monitoring with Phase 3 diagnostic protocol,
    allowing DataHealer and AI Brain to consume snapshot data.

    Args:
        snapshot: SystemHealthSnapshot from build_system_health()

    Returns:
        Diagnosis object with issues and recommended actions
    """
    from .snapshot import SystemHealthSnapshot

    issues = []
    root_causes = []
    recommended_actions = []
    primary_root_cause = None

    # Process bars issues
    if snapshot.bars.status == "blocked":
        issue = Issue(
            issue_id=make_issue_id("bars", IssueType.DATA_STALE),
            timestamp=datetime.now().isoformat(),
            component="bars",
            issue_type=IssueType.DATA_STALE if snapshot.bars.age_hours > 48 else IssueType.DATA_MISSING,
            severity=Severity.CRITICAL,
            title="Bars data stale or missing",
            description=f"Bars data is {snapshot.bars.age_hours:.1f} hours old; Missing bars for: {', '.join(snapshot.bars.missing_symbols)}",
            affected_symbols=snapshot.bars.missing_symbols,
            affected_tables=["trading.bars_1m"],
            metrics={
                "age_hours": snapshot.bars.age_hours,
                "coverage_pct": snapshot.bars.coverage_pct,
                "missing_symbols": snapshot.bars.missing_symbols
            }
        )
        issues.append(issue)

        rc = RootCause(
            type=RootCauseType.DATA_PIPELINE_FAILED,
            confidence=0.9,
            evidence={
                "latest_ts": snapshot.bars.latest_ts,
                "age_hours": snapshot.bars.age_hours,
                "missing_symbols": snapshot.bars.missing_symbols
            },
            description=f"Bars data pipeline has not updated in {snapshot.bars.age_hours:.1f} hours"
        )
        root_causes.append(rc)

        if not primary_root_cause:
            primary_root_cause = rc

        # Recommended action: backfill bars
        action = Action(
            action_id=make_action_id("backfill_bars"),
            action_type="backfill_bars",
            priority=1,
            title="Backfill missing bars data",
            description=f"Download historical bars for {', '.join(snapshot.bars.missing_symbols)}",
            command=f"venv/bin/python tools/load_hist_multi.py --symbols {' '.join(snapshot.bars.missing_symbols)}",
            params={
                "symbols": snapshot.bars.missing_symbols,
                "lookback_days": 90
            },
            estimated_duration_sec=600,
            is_safe_auto_execute=True,
            requires_approval=False
        )
        recommended_actions.append(action)

    # Process features issues
    if snapshot.features.status == "blocked":
        issue = Issue(
            issue_id=make_issue_id("features", IssueType.DATA_STALE),
            timestamp=datetime.now().isoformat(),
            component="features",
            issue_type=IssueType.DATA_STALE,
            severity=Severity.CRITICAL,
            title="Features data quality issue",
            description=f"Features data is {snapshot.features.age_hours:.1f} hours old",
            affected_symbols=snapshot.symbols,
            affected_tables=["trading.features_1m_with_labels"],
            metrics={
                "age_hours": snapshot.features.age_hours,
                "null_ratio": snapshot.features.null_ratio,
                "rebuild_needed": snapshot.features.rebuild_needed
            }
        )
        issues.append(issue)

        rc = RootCause(
            type=RootCauseType.FEATURE_STALE,
            confidence=0.85,
            evidence={
                "latest_ts": snapshot.features.latest_ts,
                "age_hours": snapshot.features.age_hours,
                "null_ratio": snapshot.features.null_ratio
            },
            description=f"Features are {snapshot.features.age_hours:.1f} hours stale"
        )
        root_causes.append(rc)

        if not primary_root_cause:
            primary_root_cause = rc

        # Recommended action: rebuild features
        action = Action(
            action_id=make_action_id("rebuild_features"),
            action_type="rebuild_features",
            priority=1,
            title="Rebuild features table",
            description="Recompute features from latest bars data",
            command="venv/bin/python pipelines/rebuild_features_1m.py",
            params={"symbols": snapshot.symbols},
            estimated_duration_sec=1800,
            is_safe_auto_execute=True,
            requires_approval=False
        )
        recommended_actions.append(action)

    # Process labels issues
    if snapshot.labels.status == "blocked":
        for key, label_status in snapshot.labels.by_symbol.items():
            if label_status.status == "blocked":
                issue = Issue(
                    issue_id=make_issue_id("labels", IssueType.DATA_QUALITY_POOR),
                    timestamp=datetime.now().isoformat(),
                    component="labels",
                    issue_type=IssueType.DATA_QUALITY_POOR,
                    severity=Severity.CRITICAL,
                    title=f"Label quality issue for {key}",
                    description="; ".join(label_status.issues),
                    affected_symbols=[label_status.symbol],
                    affected_tables=["trading.features_1m_with_labels"],
                    metrics={
                        "horizon_min": label_status.horizon_min,
                        "rows": label_status.rows,
                        "null_ratio": label_status.null_ratio,
                        "zero_ratio": label_status.zero_ratio,
                        "std_bps": label_status.std_bps,
                        "mean_magnitude": label_status.mean_magnitude,
                        "age_hours": label_status.age_hours
                    }
                )
                issues.append(issue)

                rc = RootCause(
                    type=RootCauseType.LABEL_QUALITY_POOR,
                    confidence=0.8,
                    evidence={
                        "null_ratio": label_status.null_ratio,
                        "zero_ratio": label_status.zero_ratio,
                        "std_bps": label_status.std_bps,
                        "issues": label_status.issues
                    },
                    description=f"Label {key} quality is poor: {', '.join(label_status.issues)}"
                )
                root_causes.append(rc)

                if not primary_root_cause:
                    primary_root_cause = rc

                # Recommended action: generate labels
                action = Action(
                    action_id=make_action_id("generate_label"),
                    action_type="generate_label",
                    priority=1,
                    title=f"Generate/fix labels for {key}",
                    description=f"Regenerate labels for {label_status.symbol} with horizon {label_status.horizon_min}m",
                    command=f"venv/bin/python tools/generate_labels.py --symbol {label_status.symbol} --horizon {label_status.horizon_min}",
                    params={
                        "symbol": label_status.symbol,
                        "horizon_min": label_status.horizon_min
                    },
                    estimated_duration_sec=300,
                    is_safe_auto_execute=True,
                    requires_approval=False
                )
                recommended_actions.append(action)

    # Create Diagnosis
    diagnosis = Diagnosis(
        diagnosis_id=make_diagnosis_id("system_health"),
        timestamp=snapshot.timestamp,
        target_component="system",
        target_symbols=snapshot.symbols,
        issues=issues,
        root_causes=root_causes,
        primary_root_cause=primary_root_cause,
        recommended_actions=recommended_actions,
        summary=_generate_summary_from_snapshot(snapshot, issues),
        confidence=_calculate_confidence_from_snapshot(snapshot, root_causes),
        diagnostic_tool="build_system_health",
        metadata={
            "overall_status": snapshot.overall_status,
            "blocking_issues": snapshot.blocking_issues,
            "horizons": snapshot.horizons
        }
    )

    return diagnosis


def _generate_summary_from_snapshot(
    snapshot: "SystemHealthSnapshot",
    issues: List[Issue]
) -> str:
    """Generate summary from snapshot"""
    if not issues:
        return "System health is good. No blocking issues detected."

    component_issues = {}
    for issue in issues:
        component_issues.setdefault(issue.component, []).append(issue)

    summary_parts = [f"System status: {snapshot.overall_status}."]

    for component, comp_issues in component_issues.items():
        summary_parts.append(f"{component.capitalize()}: {len(comp_issues)} issue(s).")

    return " ".join(summary_parts)


def _calculate_confidence_from_snapshot(
    snapshot: "SystemHealthSnapshot",
    root_causes: List[RootCause]
) -> float:
    """Calculate confidence from snapshot data"""
    if not root_causes:
        return 1.0  # High confidence in "no issues"

    # Average confidence of all root causes
    return sum(rc.confidence for rc in root_causes) / len(root_causes)
