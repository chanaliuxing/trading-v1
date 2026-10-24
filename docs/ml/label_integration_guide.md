# Label Integration Guide

## Overview

This guide explains how to integrate the new **Label Engine** and **Label Sanity** systems into existing ML workflows, specifically `wf_hpo.py`.

The integration provides:
- ✅ **Standardized label computation** via Label Engine (replacing LLM-generated SQL)
- ✅ **Automated quality validation** via Label Sanity checks
- ✅ **Backward compatibility** with existing `label_profiles.yml` format
- ✅ **Drop-in replacement** for `build_labels_from_profile()`

---

## Architecture

### Current Flow (Before Integration)

```
wf_hpo.py
  ↓
  ├── load_label_profile() → label_cfg from label_profiles.yml
  ↓
  ├── build_labels_from_profile(df, label_cfg, "y")
  ↓
  └── train model on df["y"]
```

### New Flow (After Integration)

```
wf_hpo.py
  ↓
  ├── load_label_profile() → label_cfg (supports both formats)
  ↓
  ├── build_labels_with_sanity(df, label_cfg, "y")
  │     ├── Detect format: label_id or legacy profile?
  │     ├── If label_id → use Label Engine
  │     ├── If legacy → use build_labels_from_profile()
  │     └── Run label sanity checks
  ↓
  ├── Check label_sanity.is_ok
  ↓
  └── train model (or skip if quality issues)
```

---

## Integration Steps

### Step 1: Import New Functions

Replace the old import:

```python
# Old
from backtest_ml_engine import build_labels_from_profile

# New
from labels.label_integration import build_labels_with_sanity
```

### Step 2: Update Label Building Code

In `wf_hpo.py` around line 1680-1690, replace:

```python
# Old code
if label_cfg:
    from backtest_ml_engine import build_labels_from_profile
    df_is = build_labels_from_profile(df_is, label_cfg, target_col)
```

With:

```python
# New code
if label_cfg:
    from labels.label_integration import build_labels_with_sanity

    df_is, label_sanity = build_labels_with_sanity(
        df_is,
        label_cfg,
        target_col,
        enable_sanity=True,
    )

    # Check label quality
    if label_sanity and not label_sanity.is_ok:
        print(f"⚠️  Label quality issues detected:")
        for issue_type, details in (label_sanity.issues or {}).items():
            print(f"  - {issue_type}: {details}")

        # Optional: abort training if labels are bad
        # raise ValueError(f"Label quality check failed: {label_sanity.issues}")
```

### Step 3: Log Label Sanity Summary (Optional)

Add logging to track label quality over time:

```python
from labels.label_integration import summarize_label_sanity_for_logging

# After building labels
if label_sanity:
    summary = summarize_label_sanity_for_logging(label_sanity)
    print(f"[Label Quality]\n{summary}")
```

---

## Configuration Formats

### Format 1: New Label Engine Format (Recommended)

Use standardized label IDs from the Label Registry:

```yaml
# In your HPO config
label_cfg:
  label_id: "ret_10m"        # Standardized label ID
  task: "reg"                # Task type
  params: {}                 # Optional label-specific parameters
  registry_path: "config/label_registry.json"  # Optional, defaults to this path
```

**Available Label IDs** (from `config/label_registry.json`):
- `ret_1m`, `ret_5m`, `ret_10m`, `ret_30m`, `ret_1h` - Forward returns
- `tb_10m_atr1`, `tb_30m_pct2` - Triple barrier labels

### Format 2: Legacy Profile Format (Backward Compatible)

Continue using existing `label_profiles.yml`:

```yaml
# In your HPO config
label_profile: "intraday_reg_5m"
label_profiles_path: "label_profiles.yml"  # Optional
```

The integration layer will automatically detect this format and use `build_labels_from_profile()`.

---

## Advanced Usage

### Custom Sanity Configuration

Adjust quality thresholds per experiment:

```python
from labels.label_sanity import LabelSanityConfig

custom_cfg = LabelSanityConfig(
    min_samples=1000,            # Lower threshold for small datasets
    max_abs_mean_z_reg=2.0,      # More lenient mean bias check
    max_majority_ratio_cls=0.95,  # Allow 95% majority for classification
    extreme_tail_ratio_reg=0.05,  # Allow 5% extreme tails
    enable_debug=True,            # Print detailed debug info
)

df_is, label_sanity = build_labels_with_sanity(
    df_is,
    label_cfg,
    target_col,
    sanity_cfg=custom_cfg,
)
```

### Integrate with Dataset Manifest

For AI brain integration:

```python
from labels.label_integration import integrate_sanity_into_dataset_manifest

# Existing dataset manifest
dataset_manifest = {
    "dataset_sanity": {...},
    "feature_sanity": {...},
}

# Add label sanity
dataset_manifest = integrate_sanity_into_dataset_manifest(
    dataset_manifest,
    label_sanity
)

# Now dataset_manifest["label_sanity"] contains quality metrics
# Pass this to DatasetGate for AI brain decision
```

### Simplified API

For quick prototyping:

```python
from labels.label_integration import build_and_check_labels

# One-liner: build labels + sanity check
df, sanity = build_and_check_labels(
    df,
    label_id="ret_10m",
    target_col="y",
    task_type="reg",
)

if not sanity.is_ok:
    print(f"Quality issues: {sanity.issues}")
```

---

## Migration Path

### Phase 1: Add Parallel Validation (No Risk)

Run both systems in parallel to verify consistency:

```python
# Old path (keep for now)
df_old = build_labels_from_profile(df.copy(), label_cfg, target_col)

# New path
df_new, sanity = build_labels_with_sanity(df.copy(), label_cfg, target_col)

# Verify they produce the same results
assert df_old[target_col].equals(df_new[target_col]), "Label mismatch!"

# Log sanity results
print(summarize_label_sanity_for_logging(sanity))

# Continue with old df_old for training
df_is = df_old
```

### Phase 2: Switch to New System

Once validated, remove the old path:

```python
# New path only
df_is, label_sanity = build_labels_with_sanity(df_is, label_cfg, target_col)

# Optional: abort on quality issues
if label_sanity and not label_sanity.is_ok:
    severity = max((issue.get("severity", "warning") for issue in label_sanity.issues.values()), default="warning")
    if severity == "critical":
        raise ValueError(f"Critical label quality issues: {label_sanity.issues}")
```

### Phase 3: Migrate to Label IDs

Gradually replace legacy profiles with label IDs:

```yaml
# Before (label_profiles.yml)
intraday_reg_5m:
  task: "reg"
  horizon_bars: 5
  base:
    transform: "log_return"

# After (direct in config)
label_cfg:
  label_id: "ret_5m"
  task: "reg"
```

---

## Quality Checks Performed

### Regression Tasks

1. **Insufficient Samples**: `n_samples < min_samples` (default: 5000)
2. **Mean Bias**: `|mean/std| > max_abs_mean_z_reg` (default: 1.5)
3. **Heavy Tails**: Extreme tail ratio > threshold (default: 2%)
4. **High NaN Ratio**: Reported but not flagged as issue (unless < min_samples)

### Classification Tasks

1. **Insufficient Samples**: Same as regression
2. **Label Imbalance**: Majority class ratio > threshold (default: 99%)
3. **Missing Side Column**: No side column for classification tasks

---

## Example Output

### Healthy Labels

```
[Label Quality]
Label [ret_10m] summary:
- task_type=reg, n_samples=9500, nan_ratio=0.050
- value_stats: mean=0.00100, std=0.02000, p1=-0.04000, p50=0.00000, p99=0.04000
- abs_mean_z=0.050
- issues: none
```

### Problematic Labels

```
⚠️  Label quality issues detected:
  - too_few_samples: {'n_samples': 1000, 'min_required': 5000}
  - mean_bias: {'abs_mean_z': 5.0, 'max_allowed': 1.5}

[Label Quality]
Label [ret_10m] summary:
- task_type=reg, n_samples=1000, nan_ratio=0.100
- value_stats: mean=0.05000, std=0.01000, p1=0.03000, p50=0.05000, p99=0.07000
- abs_mean_z=5.000
- issues: {'too_few_samples': {...}, 'mean_bias': {...}}
```

---

## Testing

Run integration tests:

```bash
# Unit tests
pytest tests/unit/labels/test_label_integration.py -v

# All label tests
pytest tests/unit/labels/ -v

# With coverage
pytest tests/unit/labels/ --cov=labels --cov-report=html
```

Current test coverage:
- `label_engine.py`: 22 tests ✅
- `label_sanity.py`: 17 tests ✅
- `label_integration.py`: 16 tests ✅
- **Total**: 55 tests, all passing

---

## Troubleshooting

### Issue: `TypeError: LabelRegistry.__init__() missing 1 required positional argument`

**Solution**: Ensure `config/label_registry.json` exists and is accessible:

```python
# Verify registry path
import os
registry_path = "config/label_registry.json"
assert os.path.exists(registry_path), f"Registry not found: {registry_path}"
```

### Issue: Label ID not found in registry

**Solution**: Check available label IDs:

```python
from labels.label_engine import LabelRegistry

registry = LabelRegistry("config/label_registry.json")
print(f"Available labels: {list(registry.list_labels())}")
```

### Issue: `ImportError: backtest_ml_engine module not found`

**Context**: This error occurs when using legacy label profiles.

**Solution**: Either:
1. Install/ensure `backtest_ml_engine.py` is available, OR
2. Migrate to new label_id format

### Issue: Sanity check always fails with "too_few_samples"

**Solution**: Lower the threshold for small datasets:

```python
from labels.label_sanity import LabelSanityConfig

cfg = LabelSanityConfig(min_samples=1000)  # Lower from default 5000
df, sanity = build_labels_with_sanity(df, label_cfg, "y", sanity_cfg=cfg)
```

---

## Performance Considerations

- **Label Engine**: ~0.1ms per row for forward returns, ~0.5ms for triple barriers
- **Sanity Checks**: ~1-5ms per check (negligible overhead)
- **Memory**: No significant increase (copies DataFrame once)

For large datasets (>1M rows), consider:
- Run sanity checks on a sample first
- Disable sanity checks in production (`enable_sanity=False`)
- Use parallel processing for multi-symbol datasets

---

## Best Practices

### 1. Always Run Sanity Checks in Development

```python
# Development
df, sanity = build_labels_with_sanity(df, label_cfg, "y", enable_sanity=True)
assert sanity.is_ok, f"Label quality issues: {sanity.issues}"
```

### 2. Log Sanity Results to Experiment Tracker

```python
# Log to experiments.jsonl or MLflow
exp_logger.log_metadata({
    "label_sanity": sanity.to_dict(),
})
```

### 3. Use Label IDs for New Experiments

```python
# Preferred
label_cfg = {"label_id": "ret_10m", "task": "reg"}

# Avoid (unless legacy compatibility required)
label_cfg = {"horizon_bars": 10, "base": {"transform": "log_return"}, ...}
```

### 4. Document Custom Labels in Registry

Add new labels to `config/label_registry.json`:

```json
{
  "ret_custom": {
    "func": "forward_return",
    "params": {"horizon": 15},
    "output": {
      "name": "ret_custom",
      "desc": "15-minute forward return"
    }
  }
}
```

### 5. Version Control Label Configurations

Track label config changes in git:

```bash
git add config/label_registry.json
git commit -m "feat: add ret_15m label for intraday experiments"
```

---

## Next Steps

1. ✅ **Integration Complete**: `label_integration.py` with 16 passing tests
2. ✅ **Documentation Complete**: This guide
3. 🔄 **Migration Pending**: Update `wf_hpo.py` to use new system
4. 🔄 **Dataset Gate Integration**: Connect label sanity to AI brain decisions
5. 🔄 **Experiment Audit**: Log label quality metrics to experiments.jsonl

See also:
- `examples/complete_ml_flow_example.py` - End-to-end integration demo
- `docs/ml/upgradeFeatures8.md` - Label Sanity design document
- `docs/ml/upgradeFeatures6.md` - Label Engine design document
