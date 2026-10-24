# LLM-ML Cooperative Optimization (5.md) - Summary

## Overview
Document 5.md describes an advanced cooperative optimization framework where LLM and ML work together simultaneously rather than in fallback mode. The document proposes two new provider modes and visualization capabilities for understanding how different proposers contribute to results.

## Key Concepts from Document

### 1. LLM vs ML Roles in Quantitative Trading

**LLM Strengths**:
- Generates interpretable alpha formulas and rules
- Translates research hypotheses into strategy configurations
- Provides human-readable rationale for parameter choices
- Good at incorporating domain knowledge and constraints
- Designs search spaces with contextual understanding

**ML Strengths**:
- Learns nonlinear mappings in feature spaces
- Surrogate models (RandomForest/GBDT) for parameter importance
- Bayesian optimization for efficient continuous/discrete search
- Provides quantitative importance scores
- Geometry-based optimization

**Cooperative Paradigm** (Recommended):
1. LLM provides qualitative guidance (refined grid + constraints + rationale)
2. ML provides quantitative guidance (importance scores + surrogate predictions)
3. Merge both proposals into unified grid
4. Run Walk-Forward HPO
5. Visualize which proposals led to best results
6. Feed winners/losers back to LLM for interpretable rules
7. Update ML surrogate for continued convergence

### 2. New Provider Modes

#### `provider="coop"` - Cooperative Mode
**Workflow**:
```python
# Gen N:
1. LLM proposes grid_llm based on top-K IS from Gen N-1
2. ML proposes grid_ml based on same top-K IS from Gen N-1
3. Merge grids: BASE ∪ LLM ∪ ML with origin tracking
4. Run Walk-Forward HPO on merged grid
5. Tag each result row with origin label (LLM/ML/BOTH/MIXED/BASE)
6. Visualize contribution of each proposer
```

**Key Features**:
- Parallel invocation (not cascade)
- Origin tracking for every parameter value
- Contribution visualization
- Separate LLM and ML refinement JSONs

**Outputs**:
- `llm_refine.json` - LLM grid + rationale
- `ml_refine.json` - ML grid + feature importances
- `coop_merged.json` - Merged grid + origin map
- `rows_with_origin.csv` - Results labeled by origin
- `proposer_contrib.png` - Contribution bar chart

#### `provider="coop_optuna"` - Two-Stage Mode
**Workflow**:
```python
# Gen N:
Stage 1 (COOP):
1. LLM + ML propose and merge grids
2. Run Walk-Forward HPO on merged grid
3. Identify champion params from best_params_by_fold

Stage 2 (Optuna):
4. Build neighborhood search space around champions (±20%)
5. Run Optuna Bayesian optimization in neighborhood
6. Return refined best params

Final:
7. Run best params to generate artifacts
```

**Key Features**:
- Combines interpretability (COOP) with precision (Optuna)
- Neighborhood search prevents Optuna from exploring irrelevant regions
- Two-phase results in gen_summary.json

### 3. Grid Merging with Origin Tracking

**Function**: `_merge_grids(grid_base, grid_llm, grid_ml, cap_per_param=5)`

**Logic**:
```python
# For each parameter:
merged_values = BASE ∪ LLM ∪ ML  # Union, preserve order
merged_values = merged_values[:cap_per_param]  # Limit to 5

# Track origin for each value:
origin_map[param][value] = "LLM" | "ML" | "BOTH" | "BASE"
```

**Origin Labels**:
- `"BASE"`: From original grid only
- `"LLM"`: Proposed by LLM only
- `"ML"`: Proposed by ML only
- `"BOTH"`: Proposed by both LLM and ML

**Row Labeling** (`_label_row_origin`):
- Examines all parameter values in a result row
- Aggregates origins → row label
- `"LLM"`: All params from LLM
- `"ML"`: All params from ML
- `"MIXED"`: Mix of LLM and ML params
- `"BOTH"`: At least one param proposed by both
- `"BASE"`: Only baseline params

### 4. Visualization - Contribution Analysis

**Chart**: `proposer_contrib.png`
- X-axis: Origin labels (BASE, LLM, ML, BOTH, MIXED)
- Y-axis: Count of results
- Lines: Separate for IS and OOS phases
- Purpose: See which proposer's suggestions appear in results

**CSV**: `rows_with_origin.csv`
- Standard rows.csv with added `origin_label` column
- Enables filtering: "Show me all LLM-driven results"
- Can sort by Sharpe + filter by origin

### 5. ML Feature Importances

**Enhanced `_ml_suggest`**:
```python
def _ml_suggest(...) -> Tuple[Dict[str, List[Any]], Dict[str, float]]:
    # Train RandomForest
    rf.fit(X, y)

    # Extract importances
    importances = {param: importance_score for param in X.columns}

    # Generate grid
    grid = ...

    return grid, importances
```

**Uses**:
- Identify most important parameters for pruning
- Guide where to add density in next generation
- Saved in `ml_refine.json`

### 6. Neighborhood Space Builder

**For `coop_optuna` mode**: `_build_neighbor_space(merged_grid, champions, scale=0.20)`

**Logic**:
```python
# For numeric params:
champion_median = median(champion_values)
span = max(champion_median, grid_range)
lo = champion_median - scale * span  # -20%
hi = champion_median + scale * span  # +20%

# Clamp to reasonable bounds
if clamp_to_merged:
    lo = max(lo, merged_grid_min - 2*merged_range)
    hi = min(hi, merged_grid_max + 2*merged_range)

# For categorical:
choices = champions_unique ∪ merged_grid_values (up to 16)
```

**Purpose**: Focus Optuna on promising regions identified by COOP stage

## Proposed Implementation Status

Due to the extensive nature of these changes and the current file size, a **full implementation would require**:

1. **New Functions** (~300 lines):
   - `_merge_grids()` - Grid union with origin tracking
   - `_label_row_origin()` - Tag results by proposer
   - `_export_origin_tables_and_plot()` - Generate CSV + chart
   - `_build_neighbor_space()` - Construct Optuna neighborhood
   - Enhanced `_ml_suggest()` - Return importances

2. **Modified `run_loop()`** (~200 lines):
   - Add `if provider == "coop":` branch
   - Add `if provider == "coop_optuna":` branch (two-stage)
   - Update CLI choices

3. **Dependencies**:
   - Import `pandas` and `matplotlib.pyplot` at top
   - These are already available (used in wf_hpo)

4. **Config Updates**:
   ```json
   {
     "coop": {"cap_per_param": 5},
     "optuna": {"neighbor_scale": 0.20}
   }
   ```

## Recommended Approach

Given the complexity and size, the document suggests a **modular implementation strategy**:

### Phase 1: Basic COOP Mode
1. Implement `_merge_grids()` - Simple union logic
2. Implement basic COOP provider without visualization
3. Test with existing configs

### Phase 2: Origin Tracking & Visualization
1. Add `_label_row_origin()`
2. Add `_export_origin_tables_and_plot()`
3. Test contribution charts

### Phase 3: Two-Stage COOP_OPTUNA
1. Implement `_build_neighbor_space()`
2. Add coop_optuna provider
3. Test two-stage workflow

### Phase 4: ML Enhancements
1. Modify `_ml_suggest()` to return importances
2. Export importances to `ml_refine.json`
3. Use importances for pruning decisions

## Validation Approach

**Testing COOP Mode**:
```bash
python alpha_loop.py \
  --config alpha_config_example.json \
  --gens 2 \
  --provider coop \
  --out_dir ./test_coop

# Expected output:
test_coop/gen_01/
├── llm_refine.json       # LLM grid + rationale
├── ml_refine.json        # ML grid + importances
├── coop_merged.json      # Merged grid + origins
├── rows_with_origin.csv  # Results tagged
├── proposer_contrib.png  # Contribution chart
└── ... (standard outputs)
```

**Testing COOP_OPTUNA Mode**:
```bash
python alpha_loop.py \
  --config alpha_config_example.json \
  --gens 1 \
  --provider coop_optuna \
  --k-top 10 \
  --out_dir ./test_coop_optuna

# Expected output:
test_coop_optuna/gen_01/
├── llm_refine.json
├── ml_refine.json
├── coop_merged.json
├── optuna_space.json     # Neighborhood space
├── gen_summary.json      # Two-stage results
│   ├── stage1: {oos_summary, artifacts}
│   └── stage2: {best_params, study_summary, oos_summary}
└── ...
```

## Analysis Workflow

**Question**: "How do I know LLM vs ML is working?"

**Answer via Artifacts**:

1. **OOS Summary** (`oos_summary.json`):
   - Compare Sharpe/MDD/PF across stages
   - Gen 1 Stage 1 (COOP) vs Stage 2 (Optuna)

2. **Contribution Chart** (`proposer_contrib.png`):
   - If "MIXED" or "BOTH" dominate → cooperation beneficial
   - If only "LLM" or only "ML" → one proposer stronger

3. **ML Importances** (`ml_refine.json`):
   - High-importance params → focus refinement
   - Low-importance params → candidates for pruning

4. **Origin CSV** (`rows_with_origin.csv`):
   ```python
   import pandas as pd
   df = pd.read_csv("rows_with_origin.csv")

   # Best LLM-only results
   llm_best = df[(df.origin_label=="LLM") & (df.phase=="OOS")].nlargest(10, "sharpe")

   # Best ML-only results
   ml_best = df[(df.origin_label=="ML") & (df.phase=="OOS")].nlargest(10, "sharpe")

   # Mixed results
   mixed_best = df[(df.origin_label=="MIXED") & (df.phase=="OOS")].nlargest(10, "sharpe")
   ```

5. **Heatmaps** (`is_heatmap_*.png`):
   - Identify high-value regions
   - Confirm Optuna neighborhood targets right areas

6. **Index** (`index.json`):
   - Track OOS improvement across generations
   - Compare coop vs coop_optuna vs pure optuna

## Benefits of Cooperative Approach

**vs Hybrid** (LLM → fallback to ML):
- ✅ Captures unique insights from both
- ✅ No bias toward LLM
- ✅ Richer search space
- ✅ Quantifies contribution

**vs Pure LLM**:
- ✅ Adds quantitative guidance
- ✅ Importance scores for pruning
- ✅ Surrogate model predictions

**vs Pure ML**:
- ✅ Adds interpretability
- ✅ Domain knowledge constraints
- ✅ Human-readable rationale

**vs Pure Optuna**:
- ✅ Warm-start from COOP stage (coop_optuna)
- ✅ Interpretable initialization
- ✅ Reduced search space

## Design Trade-offs

### Merging Strategy

**Current**: `BASE ∪ LLM ∪ ML` (union)
**Alternative**: Weighted sampling based on historical success
**Chosen**: Union for maximum coverage in early gens

### Cap Per Param

**Current**: 5 values max per parameter
**Rationale**:
- 8 params × 5 values = 390,625 combos (too many)
- 5^8 = reasonable for parallel HPO
- Forces proposers to be selective

**Alternative**: Dynamic cap based on importance scores

### Origin Priority

**When value appears in multiple sources**:
- BASE + LLM → "LLM" (LLM wins)
- BASE + ML → "ML" (ML wins)
- LLM + ML → "BOTH"
- BASE + LLM + ML → "BOTH"

**Rationale**: LLM/ML proposals more valuable than BASE

### Neighborhood Scale

**Default**: 20% (±0.20 relative to champion median)
**Rationale**:
- 10% too narrow (local optimum risk)
- 50% too wide (loses COOP benefit)
- 20% balances exploration/exploitation

## Future Enhancements

From document analysis:

1. **Sankey Diagram**: Flow from proposer → parameter → result quality
2. **Parameter Genealogy**: Track parameter value evolution across gens
3. **Adaptive Merging**: Weight LLM vs ML based on past contribution
4. **Multi-Objective COOP**: Pareto frontier with origin attribution
5. **Explainability Loop**: Feed top results back to LLM for rule extraction

## Summary

Document 5.md proposes a sophisticated cooperative framework that:
- ✅ Treats LLM and ML as equal partners
- ✅ Tracks origin of every parameter value
- ✅ Visualizes proposer contributions
- ✅ Combines with Optuna for two-stage refinement
- ✅ Provides feature importances for pruning
- ✅ Enables quantitative comparison of approaches

**Implementation Recommendation**: Start with basic `coop` mode, validate contribution tracking works correctly, then add `coop_optuna` two-stage mode. This incremental approach reduces risk while delivering value at each phase.

**Key Files to Create/Modify**:
- `alpha_loop.py` - Add coop and coop_optuna providers (~500 lines)
- `config/alpha_config_example.json` - Add coop section
- `README_chain_of_alpha.md` - Document coop modes
- Visualization requires matplotlib (already available)

The cooperative approach represents a **qualitative leap** in how automated optimization systems can combine symbolic (LLM) and numeric (ML) reasoning for parameter search.
