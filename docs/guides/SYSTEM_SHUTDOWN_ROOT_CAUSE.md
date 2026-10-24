# System Auto-Shutdown - Root Cause Analysis

## Problem Summary

The trading system shuts down after ~40 seconds, even though it should run forever. Multiple backfill operations complete successfully but trigger system shutdown.

## Investigation Timeline

### Initial Diagnosis (INCORRECT)
**Hypothesis**: Backfill services complete normally, monitoring loop treats completion as failure
**Fix Applied**: Modified `run_system.py:478-497` to distinguish between crashes and normal completions
**Result**: System still shuts down after 40 seconds

### Deeper Investigation (CURRENT)
**Finding**: The shutdown happens at 00:55:57, but backfill completions are logged at 00:55:58 (AFTER shutdown started)

**This means**: A different service task triggered the shutdown BEFORE backfills finished.

## Services and Task Lifecycle

### Real-Time Services (Should Run Forever)
1. **MarketDataService** (`market_data_task`) - Lines 142-143
   - Has `while self._running:` loop (line 128-222 in market_data_service.py)
   - Should run forever with keep-alive checks every 10 seconds
   - **BUT**: If an exception occurs, it breaks out of the loop

2. **ClickHouse Writers** (`ch_writer_*` tasks) - Lines 178-180
   - Kafka consumers with infinite loops
   - Should run forever

3. **Lake Writers** (`lake_writer_*` tasks) - Lines 226-229
   - Kafka consumers with infinite loops
   - Should run forever

### Non-Task Services (Not Monitored)
4. **BackfillService** instances - Started at lines 290-292
   - **CRITICAL**: Created with `await svc.start()` but NOT added to `self.services` monitoring list!
   - Stored in `self._backfills` list
   - Have internal event loops, run independently

5. **CatchupService** instances - Started at lines 330-331
   - **CRITICAL**: Created with `await catchup_svc.start()` but NOT added to `self.services` monitoring list!
   - Stored in `self._catchups` list
   - Run periodic checks independently

## Root Cause Hypothesis

The `market_data` task (line 142-143) is the only long-running service being monitored. If `MarketDataService.start()`:

1. Starts all child services (aggregator, IB adapter, backfill, tick publisher)
2. Enters the keep-alive loop (`while self._running:` at line 128)
3. **Encounters an exception** in the keep-alive loop (line 203)
4. Breaks out of both while loops
5. Returns from `start()` method
6. The task completes

When the task completes, the monitoring loop in `run_system.py:482-497` detects it and:
- Checks if there's an exception
- If yes: Logs "crashed" and shuts down
- If no: Logs "completed normally" and removes from monitoring

**But we don't see "completed normally" in the logs!** This suggests an exception occurred.

## Comparison with Backtest Engine

The user mentioned checking "what we did today from the backtest implementation":

### Backtest Pattern (Correct for Batch Processing)
```python
async def run(self) -> Dict[str, Any]:
    # Process all historical data
    for _, row in df.iterrows():
        await self.sim.on_market_data(feat)
        await on_features(symbol, feat)

    # Return results when done
    return self._build_result(df)
```

**Lifecycle**: Start → Process → Complete → Return
**Expected**: Task completes normally

### Real-Time Pattern (Should Run Forever)
```python
async def start(self):
    while self._running:
        try:
            # Start services
            await self.aggregator.start()
            await self.ib_adapter.start()

            # Keep-alive loop
            while self._running:
                await asyncio.sleep(10)
                # Health checks

        except Exception as e:
            # Reconnect logic
            await asyncio.sleep(reconnect_delay)
```

**Lifecycle**: Start → Run Forever (until stopped)
**Expected**: Task never completes (runs in infinite loop)

## The Bug

Looking at `MarketDataService.start()` line 128-222:

```python
while self._running:  # Outer reconnect loop
    try:
        # Start all services...

        while self._running:  # Inner keep-alive loop
            await asyncio.sleep(10)

            if not self.ib_adapter.is_connected():
                break  # Break to reconnect

    except Exception as e:
        logger.error(f"Market data service error: {e}", exc_info=True)
        # Clean up and reconnect
```

**If an exception occurs** between lines 129-177 (during service startup), it:
1. Gets caught at line 203
2. Logs the error
3. Cleans up (lines 207-213)
4. If `self._running` is still True, sleeps and retries (lines 215-221)
5. **But if `self._running` becomes False during the exception, it breaks out at line 222!**

## Next Steps to Fix

1. **Add better logging** to see which exception is occurring
2. **Check if `self._running` is being set to False** somewhere unexpected
3. **Consider not adding `market_data_task` to monitoring** if it's designed to handle its own restarts
4. **Or redesign the monitoring** to only shut down on specific critical failures

## Files Involved

- `run_system.py` - Main system orchestration
- `services/market/market_data_service.py` - MarketDataService with keep-alive loop
- `services/market/backfill_service.py` - BackfillService (independent, not monitored)
- `services/market/catchup_service.py` - CatchupService (independent, not monitored)

## Status

❌ **NOT FULLY FIXED** - Initial fix applied but root cause is an exception in MarketDataService, not backfill completion.

Need to identify the specific exception causing `MarketDataService.start()` to exit.
