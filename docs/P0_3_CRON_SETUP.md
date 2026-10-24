# P0.3 Implementation: Cron Setup for Snapshot Refresh

**Status**: ✅ **READY TO ENABLE**

**Date**: 2025-12-03

## Summary

P0.3 配置定时任务（cron）每30分钟刷新一次 mutable_targets 健康度快照，确保 ActiveTableManager 始终基于最新的健康评估做决策。

## Implementation

### 1. Cron Script ✅

**File**: `tools/cron/generate_mutable_targets_snapshot.sh`

**Content**:
```bash
#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT_DIR"

PYTHONPATH=. venv/bin/python scripts/ai_trader/research/mutable_targets_snapshot.py
```

**Purpose**: Wrapper script that ensures correct environment and PYTHONPATH

### 2. Snapshot Generator ✅

**File**: `scripts/ai_trader/research/mutable_targets_snapshot.py`

**Output**: `runs/memory/mutable_targets.json`

**Refresh Interval**: Every 30 minutes (recommended)

### 3. Cron Configuration

#### Manual Setup (One-Time)

```bash
# Open crontab editor
crontab -e

# Add this line (adjust path as needed):
*/30 * * * * cd . && tools/cron/generate_mutable_targets_snapshot.sh >> logs/cron_snapshot.log 2>&1
```

**Explanation**:
- `*/30 * * * *`: Run every 30 minutes
- `cd .`: Change to project root
- `>> logs/cron_snapshot.log 2>&1`: Append output to log file

#### Verify Cron Job

```bash
# List active cron jobs
crontab -l

# Check execution
tail -f logs/cron_snapshot.log
```

### 4. Monitoring

#### Check Snapshot Freshness

```bash
# Check last update time
ls -lh runs/memory/mutable_targets.json
stat -f "%Sm" -t "%Y-%m-%d %H:%M:%S" runs/memory/mutable_targets.json

# View content
cat runs/memory/mutable_targets.json | jq .
```

#### Expected Behavior

- Snapshot updates every 30 minutes
- File size: ~1-5 KB (depends on number of tables)
- Age: Should never be >35 minutes old

## Verification

### Test Execution ✅

```bash
$ cd .
$ tools/cron/generate_mutable_targets_snapshot.sh
✅ Snapshot generated at runs/memory/mutable_targets.json

$ cat runs/memory/mutable_targets.json | jq .
{
  "data:features_1m_active": {
    "metrics": {
      "health_score": 0.85,
      "recommendation": "healthy",
      "rows": 45230,
      "max_ts": "2025-12-03T16:00:00",
      "stale_days": 0
    }
  },
  ...
}
```

### Permissions ✅

```bash
$ ls -l tools/cron/generate_mutable_targets_snapshot.sh
-rwxr-xr-x  1 w  staff  247 Dec  3 03:30 generate_mutable_targets_snapshot.sh
# ✅ Executable permissions set
```

## Integration with P0.1 and P0.2

```
┌────────────────────────────────────────────────────┐
│ Cron (every 30 min)                                 │
│ → generate_mutable_targets_snapshot.sh              │
│   → mutable_targets_snapshot.py                     │
│     → runs/memory/mutable_targets.json (updated)   │
└────────────────────┬───────────────────────────────┘
                     │
                     ▼
┌────────────────────────────────────────────────────┐
│ ml_scheduler._execute_oof (on job execution)        │
│                                                      │
│ 1. ActiveTableManager.check_should_sync()           │
│    ↓ Reads mutable_targets.json (fresh!)           │
│    ↓ Checks recommendation                          │
│                                                      │
│ 2. If sync_or_fallback:                             │
│    → ActiveTableManager.auto_sync_or_fallback()     │
│    → Sync or fallback to baseline                   │
│                                                      │
│ 3. DataReadinessChecker.check()                     │
│    → Verify training window has data                │
│    → Adjust window if needed                        │
│                                                      │
│ 4. Execute training with correct table              │
└────────────────────────────────────────────────────┘
```

## Troubleshooting

### Cron Not Running

**Check cron service**:
```bash
# macOS
sudo launchctl list | grep cron

# Linux
systemctl status cron
```

### Permission Denied

```bash
chmod +x tools/cron/generate_mutable_targets_snapshot.sh
```

### Python Path Issues

Ensure cron job includes:
```bash
cd . && PYTHONPATH=. ...
```

### Snapshot Not Updating

**Check logs**:
```bash
tail -50 logs/cron_snapshot.log
```

**Manual test**:
```bash
cd .
PYTHONPATH=. venv/bin/python scripts/ai_trader/research/mutable_targets_snapshot.py
```

## Configuration Options

### Adjust Refresh Interval

```bash
# Every 15 minutes (more frequent)
*/15 * * * * cd . && tools/cron/generate_mutable_targets_snapshot.sh >> logs/cron_snapshot.log 2>&1

# Every hour (less frequent, saves resources)
0 * * * * cd . && tools/cron/generate_mutable_targets_snapshot.sh >> logs/cron_snapshot.log 2>&1

# During market hours only (9:00 AM - 5:00 PM ET)
*/30 9-17 * * 1-5 cd . && tools/cron/generate_mutable_targets_snapshot.sh >> logs/cron_snapshot.log 2>&1
```

### Log Rotation

Add to cron for log management:
```bash
# Rotate logs daily at midnight
0 0 * * * find ./logs -name "cron_snapshot.log" -mtime +7 -delete
```

## Completion Checklist

- [x] Cron script exists and is executable
- [x] Snapshot generator script exists
- [x] Script tested manually
- [x] Permissions verified
- [ ] **Cron job added to crontab** (USER ACTION REQUIRED)
- [ ] **First execution verified** (check logs after 30 min)
- [ ] **Snapshot freshness monitored** (verify updates every 30 min)

## Next Steps (User Action Required)

1. **Add cron job**:
   ```bash
   crontab -e
   # Add: */30 * * * * cd . && tools/cron/generate_mutable_targets_snapshot.sh >> logs/cron_snapshot.log 2>&1
   ```

2. **Create logs directory**:
   ```bash
   mkdir -p logs
   ```

3. **Wait 30 minutes and verify**:
   ```bash
   tail logs/cron_snapshot.log
   ls -lh runs/memory/mutable_targets.json
   ```

## References

- **Cron Script**: `tools/cron/generate_mutable_targets_snapshot.sh`
- **Snapshot Generator**: `scripts/ai_trader/research/mutable_targets_snapshot.py`
- **Output**: `runs/memory/mutable_targets.json`
- **Consumer**: `libs/ml/active_table_manager.py:check_should_sync()`

## Conclusion

✅ **P0.3 is ready to enable:**

1. ✅ Cron script prepared and tested
2. ✅ Snapshot generator functional
3. ✅ Integration points defined
4. ⏳ **Awaiting user to add cron job** (one command)

**Next Step**: Proceed to P1 (ResearchPolicy/State Deep Integration)
