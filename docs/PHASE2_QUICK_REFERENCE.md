# Phase 2 Brain Integration - Quick Reference Guide

**Version**: 2.0 | **Date**: 2025-11-26

---

## 🚀 Quick Start

### Check What Brain Sees
```python
# Brain automatically receives label_health in morning context
# View in Brain plan files:
cat runs/$(date +%Y-%m-%d)/brain_plan_morning.json | jq '.label_health'

# Example output:
{
  "overall_status": "healthy",
  "total_labels": 1315213,
  "healthy_symbols": 15,
  "can_train_ml": true,
  "issues": [],
  "top_symbols": [...]
}
```

### Queue Label Recompute Action (Manual)
```yaml
# Create runs/$(date +%Y-%m-%d)/pending_actions.yml
pending:
  - action: label_recompute
    params:
      symbols: ["TSLA", "AAPL"]
      horizons: [30]
      lookback_days: 90
      batch_size: 5
    reason: "Manual label refresh requested"
```

### Check Action Execution Results
```bash
# View label recompute log
cat runs/$(date +%Y-%m-%d)/label_recompute_log.json
```

### Test Phase 2 Integration
```bash
# Run comprehensive test suite
python temp/test_phase2_brain_integration.py
```

---

## 🎯 Core Components

| Component | File | Purpose |
|-----------|------|---------|
| **Diagnostic Utils** | `scripts/ai_trader/brain/diagnostic_utils.py` | Simplify data for Brain |
| **Orchestrator (Extended)** | `scripts/ai_trader/orchestrator.py` | Label health in context + action handler |
| **Test Suite** | `temp/test_phase2_brain_integration.py` | Validate integration |

---

## 📊 Key Functions

### Diagnostic Simplification
```python
from scripts.ai_trader.brain.diagnostic_utils import (
    simplify_label_health_for_brain,
    format_label_health_for_prompt
)
from scripts.monitor_system import SystemMonitor

# Get label health
monitor = SystemMonitor()
raw_health = monitor.check_label_health()

# Simplify for Brain
simplified = simplify_label_health_for_brain(raw_health)
print(f"Status: {simplified['overall_status']}")
print(f"Can train: {simplified['can_train_ml']}")
print(f"Issues: {len(simplified['issues'])}")

# Format for LLM prompt
text = format_label_health_for_prompt(simplified)
print(text)
```

### Brain Context Structure
```python
# What Brain sees in _propose_plan_cached() context
context = {
    "health": {...},             # System health (Phase 0)
    "data_ok": bool,             # Data gate status
    "memory": {...},             # Historical memory
    "label_health": {            # ← NEW (Phase 2)
        "overall_status": "healthy" | "degraded" | "critical",
        "total_labels": 1315213,
        "healthy_symbols": 15,
        "can_train_ml": True,
        "issues": [
            {
                "severity": "critical" | "warning",
                "issue": "missing_labels" | "low_label_coverage" | "stale_labels",
                "symbols": ["TSLA"],
                "recommendation": "Run label generation immediately"
            }
        ],
        "top_symbols": [
            {"symbol": "TSLA", "rows": 159545, "status": "healthy", ...}
        ]
    }
}
```

---

## ⚙️ Label Recompute Action Format

### Action Structure
```yaml
pending:
  - action: label_recompute
    params:
      symbols:          # Required: List of symbols to recompute
        - TSLA
        - AAPL
      horizons:         # Optional: Default [30]
        - 30
      lookback_days:    # Optional: Default 90
        90
      batch_size:       # Optional: Default 5
        5
    reason: "Descriptive reason for action"
```

### Execution Flow
1. **Brain Detection**: Brain sees label_health.issues in context
2. **Action Queuing**: Brain writes to `pending_actions.yml`
3. **Approval**: Brain reviews pending actions (if enabled)
4. **Execution**: Orchestrator calls `DataHealer.ensure_labels_ready()`
5. **Logging**: Results saved to `label_recompute_log.json`

### Supported Parameters
- `symbols`: **Required** - List of symbols to regenerate labels for
- `horizons`: List of label horizons (default: `[30]`)
- `lookback_days`: Historical data window (default: `90`)
- `batch_size`: Symbols per batch (default: `5`, max: `7`)

---

## 🔧 Troubleshooting

### Issue: Brain not seeing label_health

**Symptom**: `label_health` missing from `brain_plan_morning.json`

**Debug**:
```bash
# Check if SystemMonitor is working
venv/bin/python -c "
from scripts.monitor_system import SystemMonitor
m = SystemMonitor()
h = m.check_label_health()
print(f'Status: {h[\"summary\"][\"overall_status\"]}')
"

# Check orchestrator logs
grep "label_health_check_failed" logs/trading_system.log
```

**Solution**: Verify SystemMonitor initialization in orchestrator

### Issue: Label recompute action not executing

**Symptom**: Action in `pending_actions.yml` but no `label_recompute_log.json`

**Debug**:
```bash
# Check if DataHealer is available
venv/bin/python -c "
from scripts.ai_trader.orchestrator import Orchestrator
o = Orchestrator()
print(f'DataHealer: {o.data_healer is not None}')
"

# Check orchestrator logs
grep "label_recompute" logs/trading_system.log
```

**Solution**:
1. Verify DataHealer initialization
2. Check Brain approval (if enabled)
3. Review error logs

### Issue: Action executes but labels not generated

**Symptom**: `label_recompute_log.json` shows `status: "failed"`

**Root Cause**: Usually missing base features

**Solution**:
```bash
# Check base features exist
venv/bin/python -c "
from libs.db.ch_pool import get_client
client = get_client()
result = client.query('SELECT count() FROM trading.features_1m_active WHERE symbol = \"TSLA\"')
print(f'Base features: {result[0][\"count()\"]}')
"

# If 0, run feature pipeline first
python pipelines/rebuild_features_1m.py --symbols TSLA AAPL
```

---

## 📈 Monitoring

### Key Metrics to Track

```python
# Label health status
- overall_status: "healthy" | "degraded" | "critical"
- can_train_ml: Boolean decision helper
- issues_count: Number of detected problems

# Action execution metrics
- label_recompute_requests: Count per day
- label_recompute_success_rate: % successful
- avg_recompute_time: Average execution time
- symbols_healed: Symbols fixed per action
```

### Log Patterns

**Brain Sees Label Health**:
```
[info] label_health_check_succeeded overall_status=healthy
```

**Brain Queues Action**:
```
# Check brain_plan_morning.json for pending actions queue
```

**Action Execution**:
```
[info] label_recompute_requested symbols=['TSLA', 'AAPL']
[info] label_recompute_completed status=fixed generated_rows=289853
```

**Action Failure**:
```
[error] label_recompute_failed symbols=['INTC'] error='...'
```

---

## 🎯 Best Practices

### 1. Let Brain Decide
```python
# ❌ BAD: Manual action queuing for every minor issue
# Manually create pending_actions.yml daily

# ✅ GOOD: Let Brain see label_health and decide
# Brain evaluates severity and queues actions when needed
```

### 2. Monitor Brain Decisions
```bash
# Check Brain's action queuing behavior
grep -r "label_recompute" runs/*/brain_plan_morning.json

# Review action execution frequency
find runs/ -name "label_recompute_log.json" | wc -l
```

### 3. Use Appropriate Batch Sizes
```yaml
# ❌ BAD: Too many symbols at once (memory overflow)
params:
  symbols: [TSLA, AAPL, NVDA, SPY, MSFT, AMD, META, AMZN, GOOGL, QQQ, TQQQ, SQQQ, VOO, ORCL, INTC]
  batch_size: 15  # Exceeds ClickHouse limit!

# ✅ GOOD: Use default batch size
params:
  symbols: [TSLA, AAPL, NVDA]
  batch_size: 5   # Safe default
```

### 4. Check Results After Execution
```bash
# Always verify action results
cat runs/$(date +%Y-%m-%d)/label_recompute_log.json | jq '.results[].result.status'
```

---

## 🔍 Advanced Usage

### Custom Diagnostic Context
```python
from scripts.ai_trader.brain.diagnostic_utils import create_brain_diagnostic_context
from scripts.ai_trader.brain.preflight import preflight_check_all
from scripts.monitor_system import SystemMonitor

# Comprehensive diagnostic for Brain
monitor = SystemMonitor()
label_health = monitor.check_label_health()
ready, preflight_report = preflight_check_all(["TSLA", "AAPL"])

ctx = create_brain_diagnostic_context(
    label_health=label_health,
    preflight_report=preflight_report,
    custom_metric={"my_metric": 123}
)

# Use in Brain decision-making
# ctx includes: label_health_simplified, label_health_text, preflight_summary
```

### Manual Action Triggering
```python
# For testing or emergency situations
import yaml
from pathlib import Path
from datetime import date

action = {
    "pending": [
        {
            "action": "label_recompute",
            "params": {
                "symbols": ["TSLA"],
                "horizons": [30],
                "lookback_days": 90
            },
            "reason": "Manual emergency refresh"
        }
    ]
}

path = Path(f"runs/{date.today()}/pending_actions.yml")
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(yaml.safe_dump(action))

# Will execute in next orchestrator cycle
```

### Inspect Brain Context (Debug)
```bash
# View full Brain context from last morning session
cat runs/$(date +%Y-%m-%d)/brain_plan_morning.json | jq '{
  label_health,
  health: .health.overall,
  data_ok
}'
```

---

## 📚 Additional Resources

- **Full Implementation Doc**: `docs/PHASE2_IMPLEMENTATION_SUMMARY.md`
- **Phase 1 Reference**: `docs/PHASE1_QUICK_REFERENCE.md`
- **Test Suite**: `temp/test_phase2_brain_integration.py`
- **Pragmatic Roadmap**: `docs/PRAGMATIC_AUTOMATION_ROADMAP.md`

---

## 🆘 Support

**If Brain not making good decisions:**
1. Check label_health accuracy: `python temp/test_phase2_brain_integration.py`
2. Review Brain prompt templates in `scripts/ai_trader/brain/brain.py`
3. Check recent Brain decisions: `cat runs/$(date +%Y-%m-%d)/brain_plan_morning.json`

**If actions not executing:**
1. Verify `pending_actions.yml` format
2. Check Brain approval settings
3. Review DataHealer status: `python temp/test_phase1_automation.py`

**If label generation slow:**
- Normal: 10-60 seconds per batch of 5 symbols
- Reduce batch_size if needed
- Check ClickHouse load

**Emergency contact:**
```bash
# Quick system health check
python scripts/monitor_system.py | grep -A 20 "LABEL HEALTH"

# Force label refresh (bypass Brain)
python pipelines/generate_labels.py --symbols TSLA AAPL --horizons 30
```

---

**Last Updated**: 2025-11-26
**Maintainer**: Trading Team
**Version**: 2.0
