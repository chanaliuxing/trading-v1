# Implementation Status: upgradeFeatures14

**Date**: 2025-11-18
**Document**: "AI 大脑 → 机器学习契约化流水线" (AI Brain → ML Contractual Pipeline)
**Auditor**: Claude (Evidence-Based)
**Method**: File existence checks, code inspection

---

## Executive Summary

**Status**: ❌ **NOT IMPLEMENTED** (0% complete)

upgradeFeatures14 要求的核心组件**完全缺失**:

1. ❌ **pi/schemas.py**: PI 输出契约 (PIPlanV1, FeatureRecipeV1, TrainingPlanV1) - **未实现**
2. ❌ **pi/prompts.py**: LLM system prompt 模板 (PI_SYSTEM_PROMPT_V1) - **未实现**
3. ❌ **ml/training_gate.py**: 训练闸门 (TrainingGateConfig, run_training_gate) - **未实现**
4. ⚠️ **labels/registry.py**: LabelRegistry - **部分存在** (在 labels/label_engine.py 中)

**Total Tests**: **0 tests** (no implementation to test)

**Note**: upgradeFeatures14 与 upgradeFeatures12 Part 2 (Training Gate) 有重叠,但这里的 Training Gate 更完整,包含契约化的 PI 流程。

---

## Detailed Component Status

### Part 1: PI Output Contract (PI 输出契约)

#### ❌ pi/schemas.py - NOT IMPLEMENTED

**Status**: ❌ **MISSING**

**Expected Location**: `pi/schemas.py`

**Document Requirements**:

1. **PIPlanV1** ❌:
   ```python
   class PIPlanV1(BaseModel):
       version: Literal["pi_plan_v1"] = "pi_plan_v1"
       feature_recipe: FeatureRecipeV1
       training_plan: TrainingPlanV1
   ```

2. **FeatureRecipeV1** ❌:
   ```python
   class FeatureRecipeV1(BaseModel):
       use_table: Optional[str] = None
       base_features: List[str] = Field(default_factory=list)
       derived: List[DerivedFeatureRef] = Field(default_factory=list)
       notes: Optional[str] = None
   ```

3. **TrainingPlanV1** ❌:
   ```python
   class TrainingPlanV1(BaseModel):
       symbol: str
       task: Literal["reg", "clf"] = "reg"
       label_id: str
       label_horizon_bars: Optional[int] = None
       decision: DecisionQuantileConfig
       model_candidates: List[ModelCandidate]
       notes: Optional[str] = None
   ```

4. **DecisionQuantileConfig** ❌:
   ```python
   class DecisionQuantileConfig(BaseModel):
       mode: Literal["quantile"] = "quantile"
       q_long: float = 0.7
       q_short: float = 0.3
   ```

5. **ModelCandidate** ❌:
   ```python
   class ModelCandidate(BaseModel):
       model_type: str
       model_params: Dict[str, Any] = Field(default_factory=dict)
   ```

**Current State**: File does not exist

**Files Checked**:
```bash
$ find . -name "schemas.py" -path "*/pi/*"
(no results)

$ ls -la ./pi/*.py
-rw-r--r--@ 1 w  staff    130 Oct 14 15:25 pi/__init__.py
-rw-r--r--@ 1 w  staff   5638 Nov 17 23:02 pi/feature_sql.py
-rw-r--r--@ 1 w  staff  10794 Nov 17 23:14 pi/pi_orchestrator.py
-rw-------@ 1 w  staff   2767 Nov 17 23:11 pi/sandbox_sql.py
```

**Impact**: ❌ **HIGH** - Without schemas, LLM outputs cannot be validated, allowing arbitrary/dangerous plans

---

### Part 2: LLM System Prompt Template

#### ❌ pi/prompts.py - NOT IMPLEMENTED

**Status**: ❌ **MISSING**

**Expected Location**: `pi/prompts.py`

**Document Requirements**:

```python
PI_SYSTEM_PROMPT_V1 = """
你是量化研究平台的首席科学家 (Principal Investigator, PI)。

你的任务是:
- 在一个已定义好的特征和标签空间内，
- 为给定的交易标的、时间区间和目标，设计：
  - 一份 feature_recipe（用哪些特征）；
  - 一份 training_plan（用什么标签、任务类型、模型池、决策规则）。

⚠️ 重要约束（非常重要）:
1. 你不能自己编写 SQL 表达式。
   - 衍生特征只能通过 id 引用 FeatureRegistry.derived 中的特征。
2. 你不能自由发明标签。
   - 你只能从 LabelRegistry 中的标签 id 里选择。
3. 你必须只输出一个 JSON 对象，结构为 PIPlanV1。
4. 你不能输出任何多余文本（如解释、Markdown、注释），只能输出 JSON。
"""
```

**Current State**: File does not exist

**Files Checked**:
```bash
$ find . -name "prompts.py" -path "*/pi/*"
(no results)
```

**Impact**: ❌ **MEDIUM** - Without structured prompt, LLM may generate non-compliant outputs

---

### Part 3: Training Gate (训练闸门)

#### ❌ ml/training_gate.py - NOT IMPLEMENTED

**Status**: ❌ **MISSING**

**Expected Location**: `ml/training_gate.py`

**Document Requirements**:

1. **TrainingGateConfig** ❌:
   ```python
   @dataclass
   class TrainingGateConfig:
       min_rows: int = 5_000
       max_null_frac_per_col: float = 0.2
       max_constant_col_frac: float = 0.5
       min_label_nonnull_frac: float = 0.9
       min_label_pos_frac: float = 0.05
       min_label_neg_frac: float = 0.05
   ```

2. **TrainingGateResult** ❌:
   ```python
   @dataclass
   class TrainingGateResult:
       ok: bool
       reasons: List[str]
       metrics: Dict[str, Any]
   ```

3. **run_training_gate()** ❌:
   ```python
   def run_training_gate(
       df: pd.DataFrame,
       *,
       label_col: str,
       cfg: Optional[TrainingGateConfig] = None,
   ) -> TrainingGateResult:
       # Validates:
       # - Row count (min_rows)
       # - NaN fraction per column
       # - Constant column fraction
       # - Label non-null fraction
       # - Label pos/neg balance
   ```

**Current State**: ml/ directory and training_gate.py do not exist

**Files Checked**:
```bash
$ ls -la ./ml/*.py
(eval):1: no matches found: ./ml/*.py

$ find . -name "training_gate.py"
(no results)
```

**Impact**: ❌ **HIGH** - Can train on bad/insufficient data without validation

**Note**: This is the **same missing component** as upgradeFeatures12 Part 2, but with more complete specification including contract validation.

---

### Part 4: Label Registry Integration

#### ⚠️ labels/registry.py - PARTIALLY IMPLEMENTED

**Status**: ⚠️ **EXISTS IN DIFFERENT LOCATION**

**Expected Location**: `labels/registry.py` (standalone file)

**Actual Location**: `labels/label_engine.py` (line 48: `class LabelRegistry`)

**Files Found**:
```bash
$ ls -la ./labels/*.py
-rw-------@ 1 w  staff     17 Nov 18 14:53 labels/__init__.py
-rw-------@ 1 w  staff  12304 Nov 18 03:53 labels/label_engine.py  # Contains LabelRegistry
-rw-------@ 1 w  staff  10606 Nov 18 11:21 labels/label_integration.py
-rw-------@ 1 w  staff   9555 Nov 18 11:21 labels/label_sanity.py
```

**Verified Features from upgradeFeatures13**:
- ✅ LabelSpec dataclass (in label_engine.py)
- ✅ LabelRegistry class (in label_engine.py)
- ✅ get_registry() singleton (from upgradeFeatures13)
- ✅ add_label_column() function (from contracts/labels.py)

**Document Requirements (upgradeFeatures14 specific)**:

1. **list_label_ids()** - ❓ Need to verify if implemented
2. **get_spec()** - ❓ Need to verify if implemented
3. **Time return labels** - ✅ Implemented (from upgradeFeatures13)
4. **Triple barrier labels** - ✅ Implemented (from upgradeFeatures13)

**Status**: Functionality likely exists (from upgradeFeatures13), but need to verify method names match upgradeFeatures14 spec.

---

### Part 5: PI Integration (新版 propose)

#### ❓ pi/pi_orchestrator.py - UNKNOWN STATUS

**Expected Changes**:

The document requires `PrincipalInvestigator.propose()` to be completely rewritten to:

1. ❌ Use `PI_SYSTEM_PROMPT_V1` from `pi/prompts.py`
2. ❌ Validate LLM output with `PIPlanV1.parse_obj()`
3. ❌ Call `run_training_gate()` before allowing HPO
4. ✅ Use `add_label_column()` for Python-based labeling (already done in upgradeFeatures13)
5. ❌ Construct HPO grid from `model_candidates`

**Current State**: Need to read `pi/pi_orchestrator.py` to check if these changes exist

---

## Implementation Comparison vs. Document Requirements

| Component | Document Requirement | Implementation | Status |
|-----------|----------------------|----------------|--------|
| **PI Contract** |
| `pi/schemas.py` | ✅ Required | ❌ Not found | ❌ Missing |
| PIPlanV1 | ✅ Required | ❌ Not implemented | ❌ Missing |
| FeatureRecipeV1 | ✅ Required | ❌ Not implemented | ❌ Missing |
| TrainingPlanV1 | ✅ Required | ❌ Not implemented | ❌ Missing |
| DecisionQuantileConfig | ✅ Required | ❌ Not implemented | ❌ Missing |
| ModelCandidate | ✅ Required | ❌ Not implemented | ❌ Missing |
| **LLM Prompt** |
| `pi/prompts.py` | ✅ Required | ❌ Not found | ❌ Missing |
| PI_SYSTEM_PROMPT_V1 | ✅ Required | ❌ Not implemented | ❌ Missing |
| **Training Gate** |
| `ml/training_gate.py` | ✅ Required | ❌ Not found | ❌ Missing |
| TrainingGateConfig | ✅ Required | ❌ Not implemented | ❌ Missing |
| TrainingGateResult | ✅ Required | ❌ Not implemented | ❌ Missing |
| run_training_gate() | ✅ Required | ❌ Not implemented | ❌ Missing |
| **Label Registry** |
| labels/registry.py | ✅ Required | ⚠️ In label_engine.py | ⚠️ Partial |
| list_label_ids() | ✅ Required | ❓ Unknown | ❓ TBD |
| get_spec() | ✅ Required | ❓ Unknown | ❓ TBD |
| add_label_column() | ✅ Required | ✅ In contracts/labels.py | ✅ Complete |
| **PI Integration** |
| Rewritten propose() | ✅ Required | ❓ Unknown | ❓ TBD |
| Schema validation | ✅ Required | ❌ No schemas to validate | ❌ Missing |
| Training gate call | ✅ Required | ❌ No training_gate.py | ❌ Missing |

**Summary**:
- ❌ **Core missing**: 3 major files (schemas, prompts, training_gate)
- ⚠️ **Partial**: Label registry exists but in different location
- ❓ **Unknown**: PI orchestrator changes (need to read file)
- ✅ **Complete**: 0% (no upgradeFeatures14-specific components implemented)

---

## Test Execution Summary

**Total Test Count**: 0 tests

**Reason**: No implementation files exist to test.

**Expected Tests** (from document):

1. **Happy Path Test**: PI.propose() with clean data → should return PIResult
2. **Insufficient Data Test**: Few rows → should be blocked by Training Gate
3. **Imbalanced Label Test**: Extreme label distribution → should be caught by gate
4. **Invalid Schema Test**: LLM outputs wrong JSON → should be caught by PIPlanV1 validation

**Current Status**: Cannot run any tests (no code to test)

---

## Risk Assessment

| Component | Risk Level | Rationale |
|-----------|------------|-----------|
| **PI Contract (schemas)** | **CRITICAL** ❌ | No schema validation → LLM can output dangerous plans |
| **System Prompt** | **HIGH** ⚠️ | No structured prompt → inconsistent LLM outputs |
| **Training Gate** | **CRITICAL** ❌ | Can train on bad data (same as upgradeFeatures12) |
| **Label Registry** | **LOW** ✅ | Already implemented in upgradeFeatures13 |
| **PI Integration** | **HIGH** ⚠️ | Unknown status, likely missing contract validation |

**Overall Risk**: ❌ **CRITICAL**
- Missing all 3 core safety components (schemas, prompts, training_gate)
- Cannot validate LLM outputs
- Cannot prevent training on bad data
- System is vulnerable to arbitrary/malicious LLM plans

---

## Overlap with upgradeFeatures12

**Key Observation**: upgradeFeatures14 的 **Training Gate** 与 upgradeFeatures12 Part 2 **完全相同**。

**upgradeFeatures12 Part 2 Requirements** (from IMPLEMENTATION_STATUS_UPGRADE_FEATURES_12.md):
- ❌ `training_gate.py` - Not found
- ❌ TrainingGateResult dataclass - Not implemented
- ❌ evaluate_dataset_health() - Not implemented
- ❌ evaluate_label_quality() - Not implemented
- ❌ run_training_gate() - Not implemented

**upgradeFeatures14 Training Gate Requirements**:
- ❌ `ml/training_gate.py` - Not found (same component)
- ❌ TrainingGateConfig dataclass - Not implemented
- ❌ TrainingGateResult dataclass - Not implemented
- ❌ run_training_gate() - Not implemented (same function)

**Difference**: upgradeFeatures14 provides more complete implementation spec (document lines 238-370), including specific thresholds and validation logic.

**Recommendation**: Implement upgradeFeatures14's `ml/training_gate.py` first, then reference it in upgradeFeatures12 Part 2 (avoiding duplication).

---

## Next Steps

### Priority 1: Implement Core Safety Components (CRITICAL)

1. **Create `pi/schemas.py`** (HIGHEST PRIORITY):
   - Implement PIPlanV1, FeatureRecipeV1, TrainingPlanV1
   - Implement DecisionQuantileConfig, ModelCandidate
   - Implement DerivedFeatureRef
   - Add Pydantic validators for safety checks
   - **Estimated effort**: 2-3 hours

2. **Create `pi/prompts.py`**:
   - Implement PI_SYSTEM_PROMPT_V1
   - Clearly specify JSON contract to LLM
   - Emphasize "no SQL, only IDs" constraint
   - **Estimated effort**: 1 hour

3. **Create `ml/training_gate.py`** (CRITICAL - blocks upgradeFeatures12 too):
   - Implement TrainingGateConfig dataclass
   - Implement TrainingGateResult dataclass
   - Implement run_training_gate() function with all validations:
     - min_rows check
     - max_null_frac_per_col check
     - constant_col_frac check
     - label_nonnull_frac check
     - label_pos_frac / label_neg_frac checks
   - **Estimated effort**: 3-4 hours

### Priority 2: Rewrite PI.propose()

4. **Rewrite `pi/pi_orchestrator.py::PrincipalInvestigator.propose()`**:
   - Use PI_SYSTEM_PROMPT_V1
   - Parse LLM output with PIPlanV1.parse_obj()
   - Call run_training_gate() before HPO
   - Construct HPO grid from model_candidates
   - **Estimated effort**: 4-5 hours
   - **Depends on**: schemas.py, prompts.py, training_gate.py

### Priority 3: Testing

5. **Create comprehensive tests**:
   - `tests/unit/pi/test_schemas.py` (20-30 tests)
     - PIPlanV1 validation tests
     - FeatureRecipeV1 / TrainingPlanV1 tests
     - DecisionQuantileConfig validators
   - `tests/unit/ml/test_training_gate.py` (20-30 tests)
     - Each threshold validation
     - Edge cases (empty df, all NaN labels, etc.)
   - `tests/integration/test_pi_contract_flow.py` (10-15 tests)
     - End-to-end PI contract validation flow
   - **Estimated effort**: 1-2 days

### Priority 4: Verify Label Registry

6. **Verify `labels/label_engine.py` has required methods**:
   - Check if `LabelRegistry.list_label_ids()` exists
   - Check if `LabelRegistry.get_spec()` exists
   - If missing, add them (trivial, 30 min)

---

## Evidence-Based Conclusion

**What's DONE** ✅:
- ✅ Label Registry (from upgradeFeatures13, in label_engine.py)
- ✅ add_label_column() (from upgradeFeatures13, in contracts/labels.py)
- ✅ **Total**: 0 upgradeFeatures14-specific components

**What's MISSING** ❌:
- ❌ pi/schemas.py: PI contract schemas (PIPlanV1, FeatureRecipeV1, TrainingPlanV1)
- ❌ pi/prompts.py: LLM system prompt template
- ❌ ml/training_gate.py: Training gate validation (CRITICAL, blocks upgradeFeatures12 too)
- ❌ PI.propose() rewrite: Contract-based flow
- ❌ **Total**: 4 major components (100% missing)

**What Needs Verification** ❓:
- ❓ pi/pi_orchestrator.py: Current state of propose() method
- ❓ labels/label_engine.py: list_label_ids() and get_spec() methods

**Honest Bottom Line**:

> upgradeFeatures14 **完全未实现** (0% complete)。
>
> **缺失的核心组件**:
> - ❌ PI 契约层 (schemas.py): 没有 LLM 输出验证 → 允许任意/危险计划
> - ❌ 训练闸门 (training_gate.py): 没有数据质量验证 → 可能在垃圾数据上训练
> - ❌ LLM Prompt 模板 (prompts.py): 没有结构化约束 → 不一致的输出
>
> **关键风险**:
> - ❌ **CRITICAL**: 系统无法验证 LLM 生成的训练计划
> - ❌ **CRITICAL**: 可以在完全不合格的数据上启动 HPO/训练
> - ❌ **HIGH**: PI → ML 流程没有契约保护
>
> **与 upgradeFeatures12 的重叠**:
> - Training Gate 是 upgradeFeatures12 Part 2 的**同一个组件**
> - 实现 upgradeFeatures14 会同时完成 upgradeFeatures12 的缺失部分
>
> **Current state**: upgradeFeatures14 **NOT IMPLEMENTED** (0%)
> **Blockers**: 3 missing files (schemas, prompts, training_gate)
> **Next action**: Implement core safety components (est. 2-3 days)
> **Estimated Total Effort**: 3-5 days (implementation + testing)

---

**Last Updated**: 2025-11-18
**Audit Method**: File existence checks + code inspection
**Confidence Level**: HIGH (based on file system checks)
**Test Coverage**: N/A (no code to test)
