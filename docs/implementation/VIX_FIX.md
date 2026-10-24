# VIX Backfill Error Fix - 2025-10-10

## Problem

VIX backfill was failing with error:
```
name 'Index' is not defined
```

Error appeared during backfill segment processing:
```
2025-10-11T00:33:13.048655Z [warning] Backfill segment failed
  symbol=VIX
  error="name 'Index' is not defined"
  seg_end=1760018390
  seg_start=1760016590
```

## Root Cause

**File**: `services/market/backfill_service.py`

**Line 377** references `Index('VIX', 'CBOE', 'USD')` to create a VIX contract:
```python
# Create contract - handle VIX as Index
if symbol == 'VIX':
    contract = Index('VIX', 'CBOE', 'USD')  # ❌ Index not imported!
else:
    contract = Stock(symbol, "SMART", "USD")
```

**But line 17** only imports `Stock`, not `Index`:
```python
try:
    from ib_insync import IB, BarData, Stock  # ❌ Missing Index
except Exception:
    IB = object  # type: ignore
    BarData = object  # type: ignore
    Stock = object  # type: ignore
```

## Solution Applied

Added `Index` to the import statement and fallback:

**Before**:
```python
try:
    from ib_insync import IB, BarData, Stock
except Exception:
    IB = object  # type: ignore
    BarData = object  # type: ignore
    Stock = object  # type: ignore
```

**After**:
```python
try:
    from ib_insync import IB, BarData, Stock, Index
except Exception:
    IB = object  # type: ignore
    BarData = object  # type: ignore
    Stock = object  # type: ignore
    Index = object  # type: ignore
```

## VIX Configuration

VIX is configured in `config.yml`:
```yaml
vix_alias: "VIXY"  # Fallback symbol when VIX not available
universe:
  - VIX
  - AAPL
  - NVDA
  # ...
```

## Contract Type Differences

### VIX (Index)
- **Contract Type**: `Index`
- **Exchange**: CBOE
- **Symbol**: VIX
- **Example**: `Index('VIX', 'CBOE', 'USD')`

### Stocks
- **Contract Type**: `Stock`
- **Exchange**: SMART (routing)
- **Example**: `Stock('AAPL', 'SMART', 'USD')`

## Other Services

Checked other services that handle VIX:

### ✅ `services/market/ib_market_data.py` - OK
Already has correct import (line 12):
```python
from ib_insync import IB, Stock, Index, Ticker, util as ibutil
```

Also handles VIX correctly (line 164):
```python
if symbol == 'VIX':
    contract = Index('VIX', 'CBOE', 'USD')
else:
    contract = Stock(symbol, 'SMART', 'USD')
```

### ✅ `tool/testvix.py` - Standalone test tool
This is a testing tool, not part of the main service.

## Verification

After applying the fix, VIX backfill should work correctly. To verify:

```bash
# Check VIX data in ClickHouse
/opt/homebrew/bin/clickhouse client --query "
SELECT
    symbol,
    toDate(ts_start) as d,
    count(*) as bars,
    min(close) as low,
    max(close) as high
FROM trading.bars_5s_ibkr
WHERE symbol = 'VIX'
  AND d >= today() - 7
GROUP BY symbol, d
ORDER BY d DESC
"
```

Expected: VIX bars should appear alongside stock bars.

## Status

✅ **FIXED** - `Index` import added to `services/market/backfill_service.py`

The VIX backfill should now work correctly for all bar sizes (5s, 1m, 1d).
