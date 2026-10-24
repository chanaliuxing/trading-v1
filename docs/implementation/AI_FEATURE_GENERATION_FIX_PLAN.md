# AI Feature Generation System: Diagnosis and Recovery Plan

## 1. Diagnosis Summary (问题诊断总结)

The system is currently in a persistent failure loop, primarily driven by the AI feature proposal service (`propose_features`). The AI engine is repeatedly generating invalid feature expressions, causing the downstream feature build process (`feature_build`) to fail with `UNKNOWN_IDENTIFIER` errors in ClickHouse. This, in turn, causes the `data_quality_check` to fail due to a lack of new feature data.

The root cause is not a single bug, but a fundamental flaw in the AI's reasoning process, which stems from three distinct error patterns:

1.  **Contextual Confusion**: The AI incorrectly uses names of **Labels** (e.g., `ret_10`) as if they were source **Features**, failing to distinguish between model inputs and outputs.
2.  **Conceptual Leaps**: The AI assumes common financial concepts (e.g., `mid_price`) exist as pre-computed columns in the database, when in fact they need to be calculated from more basic fields (`bid_price`, `ask_price`).
3.  **Convention Mismatch**: The AI assumes common industry naming conventions (e.g., `ask_size_1` for top-of-book size) that differ from this system's specific schema (which uses `ask_size`).

The AI's self-correction mechanism is ineffective; it only fixes one error at a time without learning the underlying rules, leading to an endless cycle of proposing slightly different but still-invalid features.

## 2. Phase 1: Immediate Stabilization Plan (第一阶段：紧急止血方案)

**Objective**: Immediately stop the system's failure loop to restore basic data processing functionality. This plan is low-risk, fully reversible, and will stabilize the system, although it will temporarily halt the creation of new AI-generated features.

**Action**: Clean the "toxic" feature candidate pool.

**Implementation Steps**:
1.  Locate the feature candidate file at `config/features_catalog.json`.
2.  Edit the file to reset the feature proposal queue. Specifically:
    *   Set the value of the `"candidate"` key to an empty list (`[]`).
    *   Remove any other non-standard, top-level keys that the AI may have added (e.g., `specs`, `vol_adj_ret_1_final`).
    *   Ensure the file remains a valid JSON, retaining essential keys like `"version"` and `"updated_at"`.

**Expected Outcome**:
*   The `propose_features` job will no longer feed invalid expressions to the feature builder.
*   The `feature_build` job will cease to crash.
*   The `data_quality_check` job will pass, as the feature tables will now be populated correctly by the base feature calculation process.
*   The system will exit the failure loop and become stable.

---

## 3. Phase 2: Long-Term Solution - Architecting a Robust AI (第二阶段：长期根治方案)

**Objective**: Implement a robust, permanent solution by building a "guardrail" system that forces the AI to operate within the strict boundaries of the existing database schema.

**Architectural Principle**: The database schema is the single source of truth. The AI, as an auxiliary tool, must adapt to the system, not the other way around.

### Step 3.1: Create a Centralized Schema Provider (建立“事实数据源”)

**Goal**: Create a single, reliable source for the list of all valid, queryable feature columns.

**Recommended Implementation**:
1.  Create a new utility file: `libs/schema_provider.py`.
2.  This utility will import the definitive list of feature columns directly from the feature calculator, which is the component that actually creates them. This ensures the AI's knowledge base is always in sync with the data pipeline.

**Example Code (`libs/schema_provider.py`):**
```python
# libs/schema_provider.py
from services.market.features.feature_calculator import _FEATURE_INSERT_COLUMNS

# This list is the ground truth for all available base features.
ALL_FEATURE_COLUMNS = _FEATURE_INSERT_COLUMNS

def get_valid_feature_columns() -> list[str]:
    """
    Returns the definitive list of all column names produced by the
    core feature calculator. This is the exclusive "allow list" of
    raw materials for the AI feature generator.
    """
    return ALL_FEATURE_COLUMNS
```

### Step 3.2: Enhance the AI Prompt with Schema Injection (改造AI调用逻辑)

**Goal**: Explicitly "teach" the AI the rules and available tools *before* it generates a feature.

**Recommended Implementation**:
1.  Locate the code responsible for calling the AI model within the `propose_features` logic (likely in `scripts/ai_trader/orchestrator.py` or a similar file).
2.  Before sending the request to the AI, call `get_valid_feature_columns()` from the new schema provider.
3.  Dynamically insert the list of valid columns into the AI's prompt, transforming it from a vague request into a strictly constrained task.

**Prompt Engineering Example**:

**_Old Prompt:_**
> "Please create a new microstructure feature."

**_New, Guardrailed Prompt:_**
> "You are a world-class quantitative analyst. Your task is to create a novel and valuable feature by writing a single, valid ClickHouse SQL expression.
>
> **== CRITICAL RULES ==**
> 1.  **Allowed Columns**: The expression **MUST ONLY** use columns from the following verified list: `['ts', 'close', 'open', 'high', 'low', 'vwap', 'volume', 'ret_1', 'ret_5', 'atr_14', ...]` *(This list should be dynamically inserted here)*. Do not invent columns.
> 2.  **Allowed Functions**: The expression **MUST ONLY** use standard ClickHouse functions (e.g., `avg() OVER (...)`, `stddevPop() OVER (...)`, `lagInFrame()`, `nullIf()`). Do not use invalid functions like `movingAvg()`, `runningDifference()`, or `mid_price()`.
> 3.  **Aliases**: The source table is aliased as `f` for 1-minute features (e.g., `f.close`), `m5` for 5-minute features (e.g., `m5.vwap`), and `d` for daily features (e.g., `d.atr14`).
>
> Propose a new feature expression based *only* on these rules."

### Step 3.3: Implement a Pre-Commit Validator (建立“安全门”)

**Goal**: Create a final, automated safety check to reject any invalid feature proposed by the AI *before* it is saved to the configuration file.

**Recommended Implementation**:
1.  After the AI model returns its proposed feature expression, but before saving it, pass the expression to a new validation function.
2.  Create a function `validate_ai_expression(expression: str, valid_columns: list) -> bool`.
3.  This function will:
    *   Use regular expressions to parse all `f.column`, `m5.column`, etc., identifiers from the expression string.
    *   For each identifier found, check if it exists in the `valid_columns` list.
    *   If any column is not in the list, the function returns `False`.
    *   If all columns are valid, it returns `True`.
4.  The orchestrator will only save the feature to `config/features_catalog.json` if the validator returns `True`. If it returns `False`, the proposal is discarded, and the error is logged for analysis (e.g., "AI proposal rejected: used non-existent column 'f.mid_price'").

## 4. Conclusion (总结)

Executing **Phase 1** will bring immediate stability to the system.

Implementing the three steps in **Phase 2** is the critical long-term solution. It will transform the AI from an unstable, error-prone component into a robust and reliable engine for feature discovery, ensuring the long-term health and progress of the entire ML trading system.
