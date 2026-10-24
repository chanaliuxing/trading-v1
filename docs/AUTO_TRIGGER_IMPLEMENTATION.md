# Auto-Trigger Implementation for Brain ML Optimization Loop

**Date**: 2025-11-08
**Status**: ✅ Implemented and deployed

## Problem

The AI Brain's optimization loop required manual intervention to continue after `reduce_window` or `extend_window` actions. These actions only recorded hints to `hpo_hints.json` but didn't trigger new OOF evaluations, causing 1+ hour gaps in the autonomous loop.

**Symptoms**:
- Brain suggests `reduce_window` from 60→30 days
- Consumer records hint to hpo_hints.json
- ❌ No new OOF is triggered automatically
- System waits indefinitely for manual intervention
- User must manually run: `venv/bin/python tools/nightly_oof_flow.py --window-days 30`

## Solution

Modified `control_event_consumer.py` to automatically queue OOF jobs when Brain suggests window size changes.

### Changes Made

#### 1. Updated `_record_reduce_window_hint` (lines 1615-1674)

**Before**: Only recorded hint to JSON file
**After**: Records hint + automatically queues OOF job with new window size

```python
# Auto-trigger OOF evaluation with new window size
symbols = params.get("symbols", ["TSLA"])
oof_params = {
    "job_type": "oof",
    "priority": "high",
    "params": {
        "symbols": symbols,
        "window_days": window_days,
    }
}
trigger_result = await _enqueue_ml_job(oof_params, job_id)
if trigger_result.get("status") == "ok":
    result["auto_triggered"] = True
    result["queued_job_id"] = trigger_result.get("queued_job_id")
```

#### 2. Updated `_record_extend_window_hint` (lines 1560-1636)

**Before**: Whitelist was `[21, 30, 45, 60]`, only recorded hints
**After**:
- Whitelist expanded to `[30, 60, 90, 120, 150, 180]` to match Brain's capabilities
- Auto-triggers OOF evaluation with same logic as `reduce_window`

#### 3. Updated Execution Points (lines 2309-2327)

Both `extend_window` and `reduce_window` action handlers now capture:
- `auto_triggered`: Boolean flag indicating if OOF was queued
- `queued_job_id`: The ID of the automatically triggered OOF job

**Example audit entry**:
```json
{
  "ts": "2025-11-08T01:35:00.000000",
  "job_id": "20251108063500-abc123",
  "action": "reduce_window",
  "params": {"window_days": 30},
  "confidence": 0.85,
  "status": "ok",
  "hint_recorded": true,
  "auto_triggered": true,
  "queued_job_id": "20251108013500-xyz789"
}
```

## Testing Instructions

### 1. System Status Check

```bash
# Check Brain Consumer is running with new code
ps aux | grep control_event_consumer.py | grep -v grep

# Check ML Scheduler is running
ps aux | grep ml_scheduler.py | grep -v grep

# Verify Consumer has auto-trigger code loaded (check PID is recent)
ls -lt runs/ai_trader/*/consumer_auto_trigger.log | head -1
```

### 2. Monitor the Autonomous Loop

**Expected Flow**:
1. OOF evaluation completes → sends Kafka event to `internal.ml.status`
2. Brain Consumer receives event → Brain reviews results
3. Brain suggests `reduce_window(window_days=30)` or `extend_window(window_days=60)`
4. 🆕 **Consumer automatically queues OOF job** with new window size
5. ML Scheduler picks up OOF job → executes nightly_oof_flow.py
6. OOF completes → sends Kafka event → **Loop repeats from step 2**

**Monitor Commands**:
```bash
# Watch Brain decisions
tail -f runs/ai_trader/$(date +%Y-%m-%d)/ml_actions.jsonl

# Watch for auto_triggered field
tail -f runs/ai_trader/$(date +%Y-%m-%d)/ml_actions.jsonl | grep auto_triggered

# Check ML Scheduler queue
tail -f runs/ai_trader/$(date +%Y-%m-%d)/ml_scheduler.log | grep -E "(job_queued|job_dequeued|oof)"

# Monitor OOF execution
ls -lt runs/ai_trader/*/oof_*_brain_suggested.log | head -5
```

### 3. Verify Auto-Trigger Works

**Test Scenario**: Wait for Brain to suggest a window change after the current 60-day OOF completes.

**Expected Behavior**:
```bash
# 1. Check latest ml_review.json
cat runs/ai_trader/$(date +%Y-%m-%d)/ml_review.json | jq '.review.next_actions'
# Example output:
# [
#   {
#     "action": "reduce_window",
#     "params": {"window_days": 30},
#     "confidence": 0.85
#   }
# ]

# 2. Wait for Brain to execute action
tail -f runs/ai_trader/$(date +%Y-%m-%d)/ml_actions.jsonl
# Look for:
# {"action": "reduce_window", "auto_triggered": true, "queued_job_id": "..."}

# 3. Verify OOF was queued
grep "20251108013500-xyz789" runs/ai_trader/$(date +%Y-%m-%d)/ml_scheduler.log
# Should show: "job_queued" and "job_dequeued" entries

# 4. Confirm OOF is running
ps aux | grep nightly_oof_flow.py | grep -v grep
```

### 4. Manual Trigger (if needed for testing)

```bash
# Simulate an OOF completion to test the cycle
venv/bin/python tools/nightly_oof_flow.py \
  --symbols TSLA \
  --start "2025-09-29 09:30:00" \
  --end "2025-11-06 16:00:00" \
  --window-days 60 \
  --quick \
  --max-iters 2 > runs/ai_trader/$(date +%Y-%m-%d)/oof_60d_test.log 2>&1 &

echo "OOF triggered with PID: $!"
```

## Files Modified

| File | Lines | Change Summary |
|------|-------|----------------|
| `scripts/ai_trader/control_event_consumer.py` | 1615-1674 | Added auto-trigger to `_record_reduce_window_hint` |
| `scripts/ai_trader/control_event_consumer.py` | 1560-1636 | Added auto-trigger to `_record_extend_window_hint` |
| `scripts/ai_trader/control_event_consumer.py` | 2309-2327 | Updated execution handlers to capture auto-trigger results |

## Deployment

```bash
# 1. Stop old Brain Consumer
OLD_PID=$(ps aux | grep control_event_consumer.py | grep -v grep | awk '{print $2}')
kill $OLD_PID

# 2. Start new Brain Consumer with auto-trigger
PYTHONPATH=. venv/bin/python scripts/ai_trader/control_event_consumer.py \
  > runs/ai_trader/$(date +%Y-%m-%d)/consumer_auto_trigger.log 2>&1 &
echo "✅ Brain Consumer restarted with auto-trigger (PID: $!)"

# 3. Verify startup
sleep 3
tail -20 runs/ai_trader/$(date +%Y-%m-%d)/consumer_auto_trigger.log

# 4. Check process status
ps aux | grep control_event_consumer.py | grep -v grep
```

**Current Deployment**:
- Brain Consumer PID: 19592 (started 2025-11-08 01:29:39)
- Log: `runs/ai_trader/2025-11-08/consumer_auto_trigger.log`

## Expected Results

### Before Auto-Trigger
```
[01:24] Brain suggests reduce_window(30)
[01:24] Consumer records hint to hpo_hints.json
[01:25-02:30] ⏸️  System idle, waiting for manual trigger
[02:30] Human manually runs: venv/bin/python tools/nightly_oof_flow.py --window-days 30
[02:40] OOF completes, Brain reviews
```

### After Auto-Trigger ✅
```
[01:24] Brain suggests reduce_window(30)
[01:24] Consumer records hint AND queues OOF job (job_id: 20251108012400-xyz)
[01:25] Scheduler dequeues OOF job, starts execution
[01:35] OOF completes, Brain reviews results
[01:36] Brain suggests extend_window(60)
[01:36] Consumer auto-triggers 60-day OOF (job_id: 20251108013600-abc)
[01:45] Loop continues autonomously...
```

## Success Metrics

1. **Autonomous Loop**: Brain → OOF → Brain → OOF without manual intervention
2. **No Idle Gaps**: Time between Brain decision and next OOF < 5 minutes
3. **Audit Trail**: `ml_actions.jsonl` shows `auto_triggered: true` and `queued_job_id`
4. **Scheduler Activity**: `ml_scheduler.log` shows continuous job processing

## Troubleshooting

### Auto-Trigger Not Working

**Symptom**: `ml_actions.jsonl` shows `hint_recorded: true` but `auto_triggered: false`

**Diagnosis**:
```bash
# Check Consumer is running new code
ps -p 19592 -o etime,command
# If etime > 10 minutes, Consumer is using old code

# Check for errors in Consumer log
tail -50 runs/ai_trader/$(date +%Y-%m-%d)/consumer_auto_trigger.log | grep -i error
```

**Fix**:
1. Restart Brain Consumer (see Deployment section)
2. Verify new code is loaded by checking log for "auto_triggered" keyword

### OOF Job Not Queued

**Symptom**: `auto_triggered: true` but no job appears in Scheduler queue

**Diagnosis**:
```bash
# Check if _enqueue_ml_job returned error
grep "ml_job_enqueue_failed" runs/ai_trader/$(date +%Y-%m-%d)/consumer_auto_trigger.log

# Check Scheduler is running
ps aux | grep ml_scheduler.py | grep -v grep
```

**Fix**:
1. Check Scheduler log for errors: `tail -50 runs/ai_trader/$(date +%Y-%m-%d)/ml_scheduler.log`
2. Verify Kafka is running: `docker ps | grep kafka`

## Next Steps

1. ✅ Monitor first autonomous cycle (60d → Brain review → auto-triggered 30d OOF)
2. ⏳ Verify system runs for 24 hours without manual intervention
3. ⏳ Expand to multi-symbol optimization (AAPL, NVDA, SPY)
4. ⏳ Add Prometheus metrics for auto-trigger success rate

## Related Documentation

- `docs/BRAIN_ML_QUICKSTART.md`: How to start and monitor the Brain ML system
- `docs/BRAIN_SYSTEM_SUMMARY.md`: Technical summary of all Brain fixes
- `tools/monitor_brain_ml.sh`: Real-time monitoring script

---

**Last Updated**: 2025-11-08 01:36:00
**Maintainer**: AI Trading Team
