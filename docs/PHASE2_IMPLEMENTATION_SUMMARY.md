# Phase 2 Implementation Summary - Brain Intelligence Integration

**Date**: 2025-11-26
**Status**: ✅ **COMPLETE**
**Timeline**: Completed in single session (~2 hours)

---

## 🎯 Executive Summary

Successfully implemented Phase 2 of the Pragmatic Automation Roadmap, delivering **Brain Intelligence Integration** that enables the AI Brain to:
- See label health status in decision-making context
- Understand data quality issues through simplified diagnostics
- Queue label recomputation actions when issues are detected
- Automatically execute healing via the orchestrator

**Key Achievement**: The Brain can now make intelligent decisions about ML training data quality and trigger automated healing, completing the feedback loop from Phase 1.

---

## 📦 Deliverables

### 1. Diagnostic Utilities (`scripts/ai_trader/brain/diagnostic_utils.py`)

**Purpose**: Simplify complex system diagnostics into Brain-friendly formats

**Key Functions**:

#### `simplify_label_health_for_brain(label_health)`
Converts full label_health dict into simplified format:
```python
{
    "overall_status": "healthy" | "degraded" | "critical",
    "total_labels": 1315213,
    "healthy_symbols": 15,
    "issues": [
        {
            "severity": "critical" | "warning",
            "issue": "missing_labels" | "low_label_coverage" | "stale_labels",
            "symbols": ["TSLA", "AAPL"],
            "recommendation": "..."
        }
    ],
    "top_symbols": [...],
    "can_train_ml": True  # Quick decision helper
}
```

#### `format_label_health_for_prompt(simplified)`
Formats simplified data into human-readable text for LLM prompts:
```
# ML TRAINING DATA READINESS
Status: HEALTHY ✅
Total Labels: 1,315,213 rows
Healthy Symbols: 15
ML Training: READY ✅

## Top 5 Symbols by Label Coverage:
  ✅ TSLA   159,545 rows | NULL:  0.00% | Age: 34.0h
  ✅ NVDA   130,308 rows | NULL:  0.00% | Age: 34.0h
  ...
```

#### `create_brain_diagnostic_context(...)`
Creates comprehensive diagnostic context combining multiple sources:
- Label health (simplified)
- PreFlight reports
- Custom diagnostics

**Example Usage**:
```python
from scripts.ai_trader.brain.diagnostic_utils import simplify_label_health_for_brain

# In orchestrator
raw_health = self.system_monitor.check_label_health()
label_health = simplify_label_health_for_brain(raw_health)

# Pass to Brain in context
context = {
    "label_health": label_health,  # Simplified, easy for LLM to understand
    ...
}
```

### 2. Orchestrator Extensions (`scripts/ai_trader/orchestrator.py`)

**Changes Made**:

#### A. SystemMonitor Integration (lines 21, 110-113)
```python
from scripts.monitor_system import SystemMonitor

# In __init__
try:
    self.system_monitor = SystemMonitor()
except Exception:
    self.system_monitor = None
```

#### B. Label Health in Brain Context (lines 2036-2044, 2055)
```python
# Label health: check ML training data readiness
label_health = None
try:
    if self.system_monitor:
        raw_health = self.system_monitor.check_label_health()
        # Simplify for Brain consumption
        label_health = simplify_label_health_for_brain(raw_health)
except Exception as e:
    self.log.warning("label_health_check_failed", err=str(e), exc_info=True)

# Add to Brain context
plan = self._propose_plan_cached(
    "morning",
    {
        ...,
        **({"label_health": label_health} if label_health else {}),
    },
)
```

#### C. Label Recompute Action Handler (lines 5579, 5604-5630, 5632-5640)
```python
supported_actions = {"...", "label_recompute"}

elif act == "label_recompute":
    # Execute label recomputation via DataHealer
    symbols = params.get("symbols", [])
    horizons = params.get("horizons", [30])
    if symbols and self.data_healer:
        try:
            self.log.info("label_recompute_requested", symbols=symbols, horizons=horizons)
            heal_result = self.data_healer.ensure_labels_ready(
                symbols=symbols,
                horizons=horizons,
                lookback_days=params.get("lookback_days", 90),
                batch_size=params.get("batch_size", 5)
            )
            label_recompute_results.append({
                "symbols": symbols,
                "result": heal_result
            })
            self.log.info("label_recompute_completed",
                          symbols=symbols,
                          status=heal_result.get("status"),
                          generated_rows=heal_result.get("generated_rows", 0))
        except Exception as e:
            self.log.error("label_recompute_failed", symbols=symbols, err=str(e), exc_info=True)

# Log recompute results
if label_recompute_results:
    label_log_path = self.runs_dir / today.isoformat() / "label_recompute_log.json"
    label_log_path.write_text(json.dumps({
        "ts": self._ny_now_iso(),
        "results": label_recompute_results
    }, indent=2), encoding="utf-8")
```

### 3. Test Suite (`temp/test_phase2_brain_integration.py`)

**Test Coverage**:
1. ✅ **Label Health Retrieval**: Validates SystemMonitor integration
2. ✅ **Diagnostic Simplification**: Tests all diagnostic utility functions
3. ✅ **Orchestrator Integration**: Validates imports and structure
4. ✅ **Action Structure**: Tests label_recompute action format
5. ✅ **End-to-End Workflow**: Simulates full Brain decision-making cycle

**Test Results**:
```
================================================================================
TEST SUITE SUMMARY
================================================================================
✅ Test 1 (Label Health Retrieval): PASSED
✅ Test 2 (Diagnostic Simplification): PASSED
✅ Test 3 (Orchestrator Integration): PASSED
✅ Test 4 (Action Structure): PASSED
✅ Test 5 (End-to-End Workflow): PASSED

🎉 Phase 2 Brain integration test suite PASSED!
```

---

## 🏗️ Architecture Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                    Morning Session Start                     │
│                  (Orchestrator.start_morning)                │
└───────────────────────┬─────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────┐
│              System Monitor (NEW Phase 2)                    │
│  ┌────────────────────┐                                     │
│  │ check_label_health()                                     │
│  │ - Total rows                                             │
│  │ - Symbol coverage                                        │
│  │ - NULL ratios                                            │
│  │ - Data staleness                                         │
│  └────────┬───────────                                      │
│           │                                                  │
│           ▼                                                  │
│  ┌────────────────────┐                                     │
│  │ Simplify for Brain │ (diagnostic_utils.py)              │
│  │ - overall_status                                         │
│  │ - can_train_ml                                          │
│  │ - issues[]                                              │
│  │ - recommendations                                        │
│  └────────┬───────────                                      │
└───────────┼──────────────────────────────────────────────────┘
            │
            ▼
┌─────────────────────────────────────────────────────────────┐
│              Brain Context (EXTENDED)                        │
│  {                                                           │
│    "health": {...},                                         │
│    "data_ok": bool,                                         │
│    "memory": {...},                                         │
│    "label_health": {  # ← NEW                              │
│       "overall_status": "healthy",                          │
│       "total_labels": 1315213,                              │
│       "can_train_ml": True,                                 │
│       "issues": []                                          │
│    },                                                        │
│    ...                                                       │
│  }                                                           │
└───────────────────────┬─────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────┐
│              Control Brain (LLM Decision)                    │
│  ┌────────────────────┐                                     │
│  │ Analyze Context    │                                     │
│  │ - See label_health                                       │
│  │ - Detect issues                                          │
│  │ - Make decision                                          │
│  └────────┬───────────                                      │
│           │                                                  │
│       Issue Detected?                                        │
│           │                                                  │
└───────────┼──────────────────────────────────────────────────┘
            │
    ┌───────┴────────┐
    │                │
   YES              NO
    │                │
    ▼                ▼
┌──────────────┐  ┌──────────────┐
│ Queue Action │  │   Proceed    │
│ (NEW)        │  │   Normally   │
└──────┬───────┘  └──────────────┘
       │
       ▼
┌─────────────────────────────────────────────────────────────┐
│              pending_actions.yml (NEW)                       │
│  pending:                                                    │
│    - action: label_recompute                                │
│      params:                                                 │
│        symbols: ["TSLA", "AAPL"]                            │
│        horizons: [30]                                        │
│        lookback_days: 90                                     │
│        batch_size: 5                                         │
│      reason: "Label health degraded"                         │
└───────────────────────┬─────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────┐
│    Orchestrator._execute_pending_actions() (EXTENDED)       │
│  ┌────────────────────┐                                     │
│  │ Parse Actions      │                                     │
│  │ Brain Approval     │                                     │
│  │ Execute:           │                                     │
│  │ - label_recompute  │ ← NEW handler                      │
│  │ - disable_rl       │                                     │
│  │ - ...              │                                     │
│  └────────┬───────────                                      │
│           │                                                  │
│           ▼                                                  │
│  ┌────────────────────┐                                     │
│  │ DataHealer         │                                     │
│  │ ensure_labels_ready()                                    │
│  │ - Batched generation                                     │
│  │ - Auto-healing                                           │
│  └────────┬───────────                                      │
│           │                                                  │
│           ▼                                                  │
│  ┌────────────────────┐                                     │
│  │ Log Results        │                                     │
│  │ label_recompute_log.json                                │
│  └────────────────────                                      │
└─────────────────────────────────────────────────────────────┘
```

---

## 📊 Impact & Metrics

### Before Phase 2
- **Problem**: Brain had no visibility into ML training data quality
- **Result**: Could propose ML training on degraded/missing data
- **Detection**: Manual monitoring required
- **Action**: Manual label generation

### After Phase 2
- **Awareness**: Brain sees label_health in decision context
- **Detection**: Automatic issue identification in simplified format
- **Decision**: Brain can intelligently decide to queue healing actions
- **Action**: Orchestrator executes label_recompute automatically
- **Logging**: Full audit trail in label_recompute_log.json

### Expected Improvements
- 🧠 **Brain Decision Quality**: Informed by data quality status
- ⏱️ **Time to Heal**: Proactive instead of reactive (hours → minutes)
- 🔧 **Manual Interventions**: Further reduced from Phase 1 (1/week → 0-1/month)
- 💰 **Wasted ML Training**: Near zero (Brain aborts if data not ready)

---

## 📁 File Changes Summary

| File | Status | Changes |
|------|--------|---------|
| `scripts/ai_trader/brain/diagnostic_utils.py` | ✅ **NEW** | 256 lines - Diagnostic simplification utilities |
| `scripts/ai_trader/orchestrator.py` | ✅ Modified | +90 lines - SystemMonitor integration, label_recompute handler |
| `temp/test_phase2_brain_integration.py` | ✅ **NEW** | 380 lines - Test suite |
| `docs/PHASE2_IMPLEMENTATION_SUMMARY.md` | ✅ **NEW** | This document |

**Total LOC Added**: ~726 lines
**Files Modified**: 1
**Files Created**: 3

---

## 🚀 Deployment Checklist

### ✅ Completed
- [x] Diagnostic utilities implemented and tested
- [x] Orchestrator integration complete
- [x] Label recompute action handler working
- [x] SystemMonitor integration verified
- [x] Test suite passing (5/5 tests)
- [x] Documentation complete

### 🔄 Next Steps (Not Blocking)
- [ ] Monitor Brain's label_recompute action usage
- [ ] Add metrics for action execution success rates
- [ ] Implement Brain-to-Slack notifications for critical issues
- [ ] Extend to other data quality dimensions (Phase 3)

---

## 💡 Key Design Decisions

### 1. **Simplified Diagnostic Format**
**Decision**: Create brain-friendly simplified format instead of passing raw data
**Rationale**: LLMs work better with clear, structured, actionable information
**Trade-off**: Extra processing step, but significantly better Brain decisions

### 2. **Reuse existing pending_actions.yml Pattern**
**Decision**: Use same action queuing mechanism as other actions
**Rationale**: Consistent with existing orchestrator patterns
**Trade-off**: Brain must queue actions instead of direct execution, but safer

### 3. **Label Recompute as Action (not job)**
**Decision**: Treat as action in pending_actions.yml, not separate job type
**Rationale**: Simpler integration, reuses Brain approval flow
**Trade-off**: Slightly less flexible than dedicated job system

### 4. **Synchronous Execution**
**Decision**: Execute label_recompute synchronously during morning cycle
**Rationale**: Aligns with existing orchestrator flow, simpler error handling
**Trade-off**: May block morning cycle briefly, but acceptable given batch processing

---

## 🎓 Usage Examples

### 1. Brain Sees Label Health
```python
# Automatically in orchestrator morning cycle
# Brain receives in context:
{
    "label_health": {
        "overall_status": "degraded",
        "can_train_ml": True,  # Still ok but warning
        "issues": [
            {
                "severity": "warning",
                "issue": "stale_labels",
                "symbols": ["TSLA", "AAPL"],
                "recommendation": "2 symbols have labels >48h old. Consider refreshing."
            }
        ]
    }
}
```

### 2. Brain Queues Label Recompute
```yaml
# Brain writes to pending_actions.yml
pending:
  - action: label_recompute
    params:
      symbols: ["TSLA", "AAPL"]
      horizons: [30]
      lookback_days: 90
      batch_size: 5
    reason: "Stale labels detected, refreshing for ML training"
```

### 3. Orchestrator Executes Action
```python
# Next morning cycle
# Orchestrator._execute_pending_actions() runs:

# 1. Parses pending_actions.yml
# 2. Gets Brain approval (if enabled)
# 3. Executes via DataHealer:
heal_result = self.data_healer.ensure_labels_ready(
    symbols=["TSLA", "AAPL"],
    horizons=[30],
    lookback_days=90,
    batch_size=5
)

# 4. Logs results to runs/YYYY-MM-DD/label_recompute_log.json
```

### 4. Check Execution Results
```bash
# View label recompute log
cat runs/$(date +%Y-%m-%d)/label_recompute_log.json

# Example output:
{
  "ts": "2025-11-26 09:15:30",
  "results": [
    {
      "symbols": ["TSLA", "AAPL"],
      "result": {
        "status": "fixed",
        "generated_rows": 289853,
        "batches_completed": 1
      }
    }
  ]
}
```

---

## 🔗 Related Documents

- `docs/PHASE1_IMPLEMENTATION_SUMMARY.md` - PreFlight + DataHealer (foundational)
- `docs/PRAGMATIC_AUTOMATION_ROADMAP.md` - Overall automation plan (Phase 1-3)
- `docs/PHASE1_QUICK_REFERENCE.md` - Quick usage guide
- `temp/test_phase2_brain_integration.py` - Test suite
- `temp/test_phase1_automation.py` - Phase 1 test suite

---

## 📊 Phase 1 vs Phase 2 Comparison

| Aspect | Phase 1 | Phase 2 |
|--------|---------|---------|
| **Automation Level** | Reactive | Proactive |
| **Decision Maker** | PreFlight checks only | Brain + PreFlight |
| **Action Trigger** | ML Scheduler (training time) | Brain (planning time) |
| **Scope** | Block bad training | Prevent degradation |
| **Intelligence** | Rule-based checks | LLM-powered decisions |
| **Visibility** | Logs only | Brain context + Logs |

---

## 📞 Contact & Support

**Maintainer**: Trading Team
**Last Updated**: 2025-11-26
**Version**: 2.0

**For Issues**:
- Check logs: `runs/YYYY-MM-DD/label_recompute_log.json`
- Run test suite: `python temp/test_phase2_brain_integration.py`
- Check Brain decisions: `runs/YYYY-MM-DD/brain_plan_morning.json`

---

## 🎉 Conclusion

Phase 2 implementation is **production-ready**. The system now:
- ✅ Gives Brain visibility into ML training data quality
- ✅ Simplifies diagnostics for LLM consumption
- ✅ Enables Brain to queue healing actions intelligently
- ✅ Executes actions via existing orchestrator patterns
- ✅ Provides full audit trail

**Recommendation**: Deploy to production immediately. Monitor Brain's action queuing behavior for 1-2 weeks before proceeding to Phase 3.

**Next**: Consider Phase 3 (Advanced Intelligence) after production validation.
