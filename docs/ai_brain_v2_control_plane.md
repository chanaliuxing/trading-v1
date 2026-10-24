# AI Brain V2 Control Plane Architecture

**Version**: 2.0
**Status**: Implemented

This document describes the architecture of the AI Brain V2 control plane, which establishes a unified "Health-Awareness -> Diagnosis -> Self-Healing -> Decision" loop.

## Core Protocols

The control plane is built on three standard protocols defined in `libs/health/`:

1.  **`SystemHealthSnapshot`**: A unified data structure representing the health of the entire system at a point in time.
2.  **`Issue`**: A standardized description of a problem that can be consumed by the `DataHealer`.
3.  **`Diagnosis`**: The output of a diagnostic tool, containing the root cause of a problem and recommended `Actions`.

### 1. SystemHealthSnapshot

-   **File**: `libs/health/snapshot.py`
-   **Dataclass**: `SystemHealthSnapshot`
-   **Description**: Consolidates health status from all major components (Bars, Features, Labels, Config) into a single object.
-   **Key Fields**:
    -   `overall_status`: Literal["healthy", "degraded", "blocked"]
    -   `blocking_issues`: A list of strings describing what is blocking the system.
    -   `bars`, `features`, `labels`, `config`: Detailed status objects for each component.

### 2. Issue Protocol

-   **File**: `libs/health/issue.py`
-   **Dataclass**: `Issue`
-   **Description**: A standardized, machine-readable description of a single problem (e.g., missing data, poor quality labels). This is the primary input for the `DataHealer`.
-   **Key Fields**:
    -   `issue_type`: `IssueType` enum (e.g., `DATA_STALE`, `CONFIG_MISMATCH`)
    -   `severity`: `Severity` enum (e.g., `CRITICAL`, `HIGH`)
    -   `affected_symbols`: List of symbols this issue impacts.

### 3. Diagnosis Protocol

-   **File**: `libs/health/issue.py`
-   **Dataclass**: `Diagnosis`
-   **Description**: The standard output from any diagnostic tool. It provides a root cause analysis and a list of `Action` objects that can be executed to fix the identified issues.
-   **Key Fields**:
    -   `root_causes`: A list of `RootCause` objects.
    -   `primary_root_cause`: The most likely root cause.
    -   `recommended_actions`: A list of `Action` objects for the `DataHealer`.

## Key Components

### 1. Preflight Checks

-   **File**: `tools/preflight_check.py`
-   **Interface**: `preflight_check_all()`
-   **Function**: Acts as a gatekeeper for all training pipelines. It builds a `SystemHealthSnapshot` and prevents training from starting if the system is not in a "healthy" state. All training scripts are required to call this function before execution.

### 2. DataHealer V2

-   **File**: `libs/healer/data_healer_v2.py`
-   **Class**: `DataHealerV2`
-   **Interface**: `heal(target)`
-   **Function**: The unified self-healing engine. It consumes `Issue`, `Action`, `Diagnosis`, or `SystemHealthSnapshot` objects and executes the necessary fixes automatically. It has built-in safety checks and only runs actions marked as `is_safe_auto_execute`.

### 3. Standardized Diagnostics

-   **File**: `tools/diagnose_oof_failure.py`
-   **Interface**: `diagnose_oof_failure()`
-   **Function**: An example of a standardized diagnostic tool. When an Out-of-Fold (OOF) training fails, this tool is called to determine the root cause. It returns a `Diagnosis` object, which includes the identified issues and recommended actions for the `DataHealer`.

### 4. Orchestrator and AI Brain Integration

-   **Orchestrator**: `scripts/ai_trader/control_event_consumer.py`
    -   The `_handle_ml_completed` function now triggers `diagnose_oof_failure` on poor performance.
    -   If auto-healing is enabled, it calls `DataHealerV2.heal()` with the `Diagnosis` object.
    -   The `diagnosis` and `heal_result` are attached to the context for the AI Brain.
-   **AI Brain**: `scripts/ai_trader/brain/brain.py`
    -   The `ML_RESULT_REVIEW_PROMPT` has been updated with a "PHASE 4: DIAGNOSIS PROTOCOL UNDERSTANDING" section.
    -   The Brain can now interpret the `oof_diagnosis` and `heal_result` to make more intelligent decisions, such as retrying a job after a successful heal or escalating to a human if healing fails.

## Architectural Flow

1.  A training job (e.g., `pipelines/train_xgb_with_oof.py`) starts and calls `tools.preflight_check.preflight_check_all()`.
2.  `preflight_check_all` builds a `SystemHealthSnapshot`. If the system is not healthy, the job exits.
3.  The job runs and completes. The `control_event_consumer` receives an `ml_completed` event.
4.  If the job performance was poor, `control_event_consumer` calls `tools.diagnose_oof_failure.py`.
5.  `diagnose_oof_failure` returns a standardized `Diagnosis` object.
6.  `control_event_consumer` calls `DataHealerV2.heal()` with the `Diagnosis` object.
7.  `DataHealerV2` executes the recommended safe actions (e.g., regenerating labels, backfilling data).
8.  The `diagnosis` and `heal_result` are passed to the `AI Brain`.
9.  The `AI Brain` uses this context to decide the next action (e.g., retry the job, try a different experiment, or escalate).

This closed-loop system allows the AI Brain to intelligently diagnose and recover from common failures, moving the system from "blind fixing" to "intelligent self-healing".
