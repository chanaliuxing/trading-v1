# System Auto-Shutdown Fix - 2025-10-10

## Problem

The trading system was automatically shutting down after backfill services completed their work. The system should run forever, but instead it stopped after logs showed:

```
2025-10-11T00:50:43.491924Z [info] Backfill finished [services.market.backfill_service] from_ts=1760102990 symbol=SPY to_ts=1760143843
LeaveGroup request succeeded
2025-10-11T00:50:43.519593Z [info] Async ClickHouse client closed [libs.db.clickhouse]
# System begins shutdown sequence
```

## Root Cause

**File**: `run_system.py`
**Lines**: 478-488

The main event loop was checking if any service task completed (`task.done()`), and treating **all** completions as failures:

```python
# BUGGY CODE - Before Fix
while self.running:
    await asyncio.sleep(1)

    # Check if any service died
    for name, task in self.services:
        if task.done():  # ❌ Triggers on normal completion too!
            logger.error(f"❌ Service {name} died!")
            if task.exception():
                logger.error(f"   Error: {task.exception()}")
            self.running = False  # ❌ Shuts down entire system
            break
```

### Why This Was Wrong

The code didn't distinguish between:
1. **Normal completion**: Backfill service finishes its work (expected behavior)
2. **Crash**: Service dies with an exception (actual error requiring shutdown)

When backfill services completed normally after filling gaps, `task.done()` returned `True`, triggering the shutdown logic even though nothing was wrong.

## Solution Applied

Modified the monitoring logic to only shut down on **actual crashes** (tasks with exceptions), not normal completions:

```python
# FIXED CODE - After Fix
while self.running:
    await asyncio.sleep(1)

    # Check if any service crashed (not just completed)
    for name, task in self.services:
        if task.done():
            exception = task.exception()
            if exception:
                # Task crashed with an error - shut down system
                logger.error(f"❌ Service {name} crashed!")
                logger.error(f"   Error: {exception}")
                self.running = False
                break
            else:
                # Task completed normally (e.g., backfill finished)
                # This is OK - don't stop the system
                logger.info(f"ℹ️  Service {name} completed normally")
                # Remove from monitoring list to avoid repeated logging
                self.services = [(n, t) for n, t in self.services if t != task]
                break
```

### Key Changes

1. **Check for exceptions first**: Only `task.exception()` indicates a crash
2. **Allow normal completion**: Log it but don't shut down the system
3. **Remove completed tasks**: Stop monitoring tasks that finished normally to avoid repeated log spam
4. **Keep system running**: Only `self.running = False` if there's an actual exception

## Service Types

### Long-Running Services (Should Never Complete)
- Market Data Service (IB Connection)
- ClickHouse Bar Writers
- Lake Writers
- Feature Calculator

### Ephemeral Services (Can Complete Normally)
- **Backfill Services**: Fill historical gaps, then become idle
- **Catchup Services**: Periodic checks, can complete between runs

## Expected Behavior After Fix

### Before Fix
```
[info] Backfill finished symbol=SPY
❌ Service backfill.5s.v1 died!  # Wrong - it completed normally!
🛑 STOPPING TRADING SYSTEM       # Wrong - system should keep running!
```

### After Fix
```
[info] Backfill finished symbol=SPY
ℹ️  Service backfill.5s.v1 completed normally  # Correct - just informational
# System continues running                     # Correct - other services keep working
```

## Testing

### Test 1: Normal Backfill Completion
```bash
# Start system
source venv/bin/activate && PYTHONPATH=. python run_system.py

# Wait for backfill to complete
# Expected: System logs "completed normally" but keeps running
```

### Test 2: Actual Service Crash
```bash
# If a service crashes with an exception
# Expected: System logs "crashed!" and shuts down (correct behavior)
```

### Test 3: Long-Running Operation
```bash
# Run system for extended period
# Expected: System stays up indefinitely, even after backfills finish
```

## Status

✅ **FIXED** - System now distinguishes between normal completion and crashes

The trading system will now run forever as intended, only shutting down when:
1. A service crashes with an exception
2. User sends Ctrl+C (SIGINT)
3. System receives SIGTERM signal

## Files Modified

- `run_system.py` (lines 478-497)

## Related Issues

- VIX backfill error (fixed separately in `VIX_FIX.md`)
- Daily bar backfill (fixed separately in `DAILY_BACKFILL_FIX.md`)
- Lake BBO data (fixed separately in `LAKE_SCHEMA_FIX.md`)
