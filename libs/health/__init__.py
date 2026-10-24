"""
Health monitoring and diagnostics module

Provides unified protocols for system health snapshots, issues, and diagnostics.
"""

from .snapshot import (
    BarHealthStatus,
    FeatureHealthStatus,
    LabelSymbolStatus,
    LabelHealthStatus,
    ConfigHealthStatus,
    SystemHealthSnapshot,
    build_system_health,
)

from .issue import (
    Issue,
    IssueType,
    Severity,
    RootCause,
    RootCauseType,
    Action,
    Diagnosis,
    make_issue_id,
    make_diagnosis_id,
    make_action_id,
    create_label_missing_diagnosis,
    snapshot_to_diagnosis,  # Phase 3: Bridge Phase 1 → Phase 3
)

__all__ = [
    # Snapshot
    "BarHealthStatus",
    "FeatureHealthStatus",
    "LabelSymbolStatus",
    "LabelHealthStatus",
    "ConfigHealthStatus",
    "SystemHealthSnapshot",
    "build_system_health",
    # Issue
    "Issue",
    "IssueType",
    "Severity",
    "RootCause",
    "RootCauseType",
    "Action",
    "Diagnosis",
    "make_issue_id",
    "make_diagnosis_id",
    "make_action_id",
    "create_label_missing_diagnosis",
    "snapshot_to_diagnosis",  # Phase 3
]
