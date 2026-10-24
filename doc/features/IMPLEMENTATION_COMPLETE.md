# ML Feature Engineering Upgrade - Implementation Complete

**Date:** 2025-11-17
**Status:** ✅ **FULLY IMPLEMENTED AND TESTED**
**Test Coverage:** 44 passing tests (8 manifest + 14 sanity + 15 label + 7 integration)

## Overview

Successfully implemented a comprehensive ML feature engineering upgrade based on the architecture plan in `upgradeFeatures.md`. This implementation provides:

1. **Experiment Manifest System** - Every ML experiment is now tracked with a unique ID and complete metadata
2. **Dataset Sanity Checks** - Automated data quality validation before training
3. **Label Contract System** - Standardized, leakage-free label construction
4. **Safe SQL Builder** - Prevents LLM from writing unsafe target expressions
5. **Updated PrincipalInvestigator** - Integrated all new safety features with backward compatibility

## What Was Implemented

### 1. Experiment Manifest (`experiments/manifest.py`)

**Purpose:** Track every ML experiment with complete metadata for reproducibility and audit.

**Key Features:**
- Unique job ID generation: `SYMBOL_FREQ_LABEL_STARTDATE_ENDDATE`
- Complete experiment metadata: data source, features, training plan, sanity results
- JSON serialization for easy storage and retrieval
- Automatic manifest saving to disk

**Example:**
```python
from experiments.manifest import create_manifest, save_manifest

manifest = create_manifest(
    symbol="TSLA",
    freq="1m",
    label_id="ret_10b_fwd",
    start_ts="2024-01-01",
    end_ts="2024-03-01",
    data_source={"table": "trading.bars_1m"},
    feature_recipe={"base_features": ["close", "volume"]},
    training_plan={"task": "reg", "model": "xgb"},
    sanity_summary={"overall_status": "ok"}
)

path = save_manifest(manifest)  # Saves to manifests/TSLA_1m_ret_10b_fwd_2024-01-01_2024-03-01.json
```

### 2. Dataset Sanity Checks (`quality/dataset_sanity.py`)

**Purpose:** Validate data quality before training to prevent garbage-in-garbage-out.

**Checks Performed:**
- **Missing values:** Detects columns with >30% missing data
- **Zero variance:** Identifies constant columns (useless features)
- **Non-finite values:** Finds inf/NaN values
- **High correlation:** Detects multicollinearity (|r| > 0.98)
- **Label distribution:** Checks for heavy class imbalance (>95% one class)
- **Label validity:** Ensures label columns exist and aren't all NaN

**Output:**
- **Detailed report:** Column-level statistics, correlation pairs, label distributions
- **Summary:** Overall status (ok/warn/fail) + list of issues

**Example:**
```python
from quality.dataset_sanity import run_dataset_sanity

report, summary = run_dataset_sanity(df, label_cols=["label"])

print(summary["overall_status"])  # "ok", "warn", or "fail"
print(summary["issues"])  # ["high_missing:volume:35%", "zero_variance:constant_col"]
```

**Performance Optimization:**
- Automatically samples large dataframes (>50k rows) to avoid performance issues
- Skips expensive correlation calculation for >60 columns

### 3. Label Contract System (`contracts/labels.py`)

**Purpose:** Standardize label construction and prevent data leakage.

**Key Features:**
- **Label Registry:** Centralized definition of all labels
- **Python-based construction:** Labels computed in Python, not SQL (prevents LLM-generated leakage)
- **Future-proof architecture:** Easy to add new label types (triple barrier, etc.)
- **Built-in leakage prevention:** Uses proper shift operations to prevent looking into the future

**Current Labels:**
- `ret_10b_fwd`: 10-bar forward return (close[t+10] / close[t] - 1)

**Example:**
```python
from contracts.labels import attach_label, get_label_config

# Get label configuration
config = get_label_config("ret_10b_fwd")
print(config.horizon_bars)  # 10
print(config.price_col)     # "close"

# Attach label to dataframe
df_with_label = attach_label(df, label_id="ret_10b_fwd", colname="label")
```

**Adding New Labels:**
```python
from contracts.labels import LABEL_REGISTRY, LabelConfig

# Add a new 5-minute forward return label
LABEL_REGISTRY["ret_5m_fwd"] = LabelConfig(
    id="ret_5m_fwd",
    kind="horizon_return",
    horizon_bars=5,
    price_col="close",
    task_types=["reg", "rank"]
)
```

### 4. Safe SQL Builder (`pi/feature_sql.py`)

**Purpose:** Prevent LLM from writing unsafe SQL that could leak future information.

**How It Works:**
1. LLM provides feature recipe (base features + derived expressions)
2. Safe SQL builder:
   - Selects only required columns (from label config + feature recipe)
   - Validates derived expressions (no future references, no dangerous functions)
   - Does NOT include target/label in SQL
3. Label is attached in Python layer using contract system

**Example:**
```python
from pi.feature_sql import build_select_sql_safe

sql = build_select_sql_safe(
    table="trading.bars_1m",
    symbol="TSLA",
    start_ts="2024-01-01",
    end_ts="2024-02-01",
    recipe={
        "base_features": ["close", "volume"],
        "derived": [
            {"alias": "price_vol_ratio", "expr": "close / volume"}
        ]
    },
    label_id="ret_10b_fwd",
    feature_catalog=["close", "volume", "vwap", "open", "high", "low"]
)

# SQL will only select: close, symbol, ts, volume, (close / volume) AS price_vol_ratio
# Label will be computed separately in Python
```

### 5. Updated PrincipalInvestigator (`pi/pi_orchestrator.py`)

**Purpose:** Orchestrate the entire ML pipeline with new safety features.

**New Features:**
- **Safe mode** (default): Uses safe SQL builder + label contracts + sanity checks
- **Backward compatibility mode**: Old behavior for existing code
- **Automatic manifest generation**: Every experiment gets a manifest
- **Data quality gates**: Sanity checks run before returning data

**Example:**
```python
from pi.pi_orchestrator import PrincipalInvestigator

pi = PrincipalInvestigator(
    ch_cfg={"host": "localhost", "port": 8123},
    llm_cfg={"model": "gpt-4"}
)

result = pi.propose(
    table="trading.bars_1m",
    symbol="TSLA",
    start_ts="2024-01-01",
    end_ts="2024-02-01",
    goal="Predict short-term price movement",
    horizon="10m",
    use_safe_mode=True  # Enable safety features
)

# Result contains:
# - dataset_df: DataFrame with features + label (no leakage)
# - sanity_summary: {"overall_status": "ok", "issues": []}
# - sanity_report: Detailed statistics
# - label_id: "ret_10b_fwd"
# - manifest_path: "manifests/TSLA_1m_ret_10b_fwd_2024-01-01_2024-02-01.json"
# - hpo_grid: Sanitized hyperparameter grid
```

## Test Coverage

### Unit Tests (37 tests)

**Manifest Tests (8):**
- ✅ Job ID generation
- ✅ Special character handling
- ✅ Manifest creation with all fields
- ✅ Manifest serialization
- ✅ File saving and loading
- ✅ Directory creation

**Dataset Sanity Tests (14):**
- ✅ Empty dataframe handling
- ✅ Healthy dataframe validation
- ✅ High missing value detection
- ✅ Zero variance detection
- ✅ Non-finite value detection
- ✅ High correlation detection
- ✅ Label classification vs regression detection
- ✅ Label imbalance detection
- ✅ Missing label column handling
- ✅ All-NaN label detection
- ✅ Large dataframe sampling
- ✅ Multiple label support

**Label Contract Tests (15):**
- ✅ Label config creation
- ✅ Registry operations
- ✅ Horizon return calculation (single symbol)
- ✅ Horizon return calculation (multiple symbols)
- ✅ Index preservation
- ✅ Label attachment
- ✅ Custom column names
- ✅ Missing column validation
- ✅ Original dataframe preservation
- ✅ Required columns retrieval
- ✅ Leakage prevention verification

### Integration Tests (7)

**PI Pipeline Tests:**
- ✅ Safe mode basic workflow
- ✅ Sanity checks execution
- ✅ Label attachment in Python layer
- ✅ Manifest creation and saving
- ✅ Backward compatibility mode
- ✅ HPO grid construction
- ✅ Data quality issue detection

## File Structure

```
./
├── experiments/
│   ├── __init__.py
│   └── manifest.py                    # Experiment tracking
├── quality/
│   ├── __init__.py
│   └── dataset_sanity.py              # Data quality checks
├── contracts/
│   ├── __init__.py
│   └── labels.py                      # Label contracts
├── pi/
│   ├── __init__.py
│   ├── pi_orchestrator.py             # Updated PI (integrated)
│   └── feature_sql.py                 # Safe SQL builder (updated)
└── tests/
    ├── unit/
    │   ├── experiments/
    │   │   └── test_manifest.py       # 8 tests
    │   ├── quality/
    │   │   └── test_dataset_sanity.py # 14 tests
    │   └── contracts/
    │       └── test_labels.py         # 15 tests
    └── integration/
        └── test_pi_pipeline_safe_mode.py  # 7 tests
```

## Key Design Decisions

### 1. Label Construction in Python, Not SQL

**Why:** SQL generated by LLM is high-risk for data leakage. Python layer gives us:
- Type safety with Pydantic models
- Clear shift operations (no ambiguity)
- Easy testing and validation
- Centralized label definitions

### 2. Sanity Checks with Sampling

**Why:** Large dataframes (millions of rows) would make sanity checks too slow.
- Sample to 50k rows for expensive operations
- Still compute exact statistics (row count, etc.) on full data
- Balances speed and accuracy

### 3. Manifest Auto-Save

**Why:** Every experiment should be traceable.
- Saves automatically to `manifests/` directory
- Uses consistent naming: `{symbol}_{freq}_{label}_{start}_{end}.json`
- Includes data source, features, training plan, and sanity results

### 4. Backward Compatibility

**Why:** Don't break existing code.
- `use_safe_mode` parameter (default=True)
- Old behavior still available with `use_safe_mode=False`
- Gradual migration path

## Evidence of Correctness

### 1. Leakage Prevention Test

```python
def test_no_leakage_in_horizon_return():
    """Test that horizon return doesn't use data before current bar."""
    df = pd.DataFrame({
        "symbol": ["TSLA"] * 20,
        "ts": pd.date_range("2024-01-01", periods=20, freq="1min"),
        "close": [100.0] * 10 + [200.0] * 10,  # Price jump at bar 10
    })

    label_series = build_label_series(df, config)

    # Label at bar 0 should only depend on future prices (bars 1-5)
    # Not on the price jump at bar 10
    assert label_series.iloc[0] == 0.0  # ✅ PASSES - No leakage

    # Label at bar 5 should see the price jump
    assert label_series.iloc[5] == pytest.approx(1.0)  # ✅ PASSES
```

### 2. Data Quality Detection Test

```python
def test_sanity_detects_data_issues():
    """Test that sanity checks detect problematic data."""
    # Create data with 40% NaN and constant column
    data_df = pd.DataFrame({
        "close": [np.nan] * 40 + list(range(60)),
        "bad_feature": [1.0] * 100,  # Constant
    })

    result = pi.propose(..., use_safe_mode=True)

    # Should detect issues
    assert result.sanity_summary["overall_status"] in ["warn", "fail"]  # ✅ PASSES
    assert "zero_variance:bad_feature" in result.sanity_summary["issues"]  # ✅ PASSES
    assert "high_missing:close" in result.sanity_summary["issues"]  # ✅ PASSES
```

### 3. Safe SQL Validation

The safe SQL builder:
- ✅ Only selects required columns (symbol, ts, close for ret_10b_fwd)
- ✅ Validates derived expressions against whitelist
- ✅ Never includes target/label in SQL
- ✅ Uses parameterized queries (no SQL injection)

## Usage Guide

### Quick Start: Use PrincipalInvestigator

```python
from pi.pi_orchestrator import PrincipalInvestigator

# Initialize
pi = PrincipalInvestigator(
    ch_cfg={"host": "localhost", "port": 8123, "database": "trading"},
    llm_cfg={"model": "gpt-4"}
)

# Run ML experiment with safety features
result = pi.propose(
    table="trading.bars_1m",
    symbol="TSLA",
    start_ts="2024-01-01 09:30:00",
    end_ts="2024-01-31 16:00:00",
    goal="Predict intraday price movements for mean reversion",
    horizon="10 minutes",
    freq="1m",
    default_label_id="ret_10b_fwd",
    use_safe_mode=True,  # Enable all safety features
)

# Check data quality
if result.sanity_summary["overall_status"] == "fail":
    print("Data quality issues detected:")
    print(result.sanity_summary["issues"])
    # Don't proceed with training
elif result.sanity_summary["overall_status"] == "warn":
    print("Data quality warnings:")
    print(result.sanity_summary["issues"])
    # Proceed with caution

# Access clean data with safe labels
df = result.dataset_df  # Has 'label' column with no leakage
hpo_grid = result.hpo_grid  # Sanitized hyperparameter grid

# Review experiment manifest
print(f"Manifest saved to: {result.manifest_path}")
```

### Advanced: Standalone Usage

```python
# 1. Dataset Sanity Check
from quality.dataset_sanity import run_dataset_sanity

report, summary = run_dataset_sanity(
    df,
    label_cols=["target"],
    high_missing_threshold=0.30,
    high_corr_threshold=0.98
)

# 2. Label Attachment
from contracts.labels import attach_label

df_with_label = attach_label(
    df,
    label_id="ret_10b_fwd",
    colname="target"
)

# 3. Safe SQL Building
from pi.feature_sql import build_select_sql_safe

sql = build_select_sql_safe(
    table="trading.bars_1m",
    symbol="TSLA",
    start_ts="2024-01-01",
    end_ts="2024-02-01",
    recipe={"base_features": ["close", "volume"]},
    label_id="ret_10b_fwd",
    feature_catalog=["close", "volume", "vwap"]
)
```

## Next Steps (From Original Plan)

The current implementation covers **Steps 0-2** of the original plan:
- ✅ Step 0: Experiment Manifest & ID规范
- ✅ Step 1: Dataset Sanity (ClickHouse + 采样 + Summary)
- ✅ Step 2: Label Guard & Label Registry (基础版)

**Recommended Next Steps:**
- **Step 3:** Feature Registry + ClickHouse View + Sandbox Mechanism
- **Step 4:** AI 大脑视图升级 (integrate sanity_summary into prompts)
- **Step 5:** 高级 Label (Triple Barrier 等)

## References

- **Architecture Document:** `./doc/features/upgradeFeatures.md`
- **Code Location:** `./{experiments,quality,contracts,pi}/`
- **Test Location:** `./tests/{unit,integration}/`

## Summary

This implementation provides a **production-ready foundation** for safe ML feature engineering:

1. ✅ **Prevents data leakage** through label contracts and safe SQL
2. ✅ **Ensures data quality** through automated sanity checks
3. ✅ **Enables reproducibility** through experiment manifests
4. ✅ **Maintains safety** through guard rails and validation
5. ✅ **Preserves compatibility** with existing code

**All 44 tests passing** - Ready for use in production ML workflows.
