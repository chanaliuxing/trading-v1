# 🚨 P0 BLOCKING TASK: Implement Tier1 Market Context in Realtime Feature Calculator

**Priority**: P0 (BLOCKING - Must complete before deploying any model using spy_ret_5)
**Status**: NOT STARTED
**Created**: 2025-11-26
**Owner**: TBD

---

## Problem Statement

### Current State (BROKEN):
- ✅ **Offline Pipeline** (`rebuild_features_1m.py`): spy_ret_5 works correctly
- ❌ **Realtime Pipeline** (`feature_calculator.py`): spy_ret_5 returns NULL

### Why This is Blocking:

**Train-Serving Skew** will cause production failures:
1. ML model trains on real spy_ret_5 values (e.g., 0.003333, 0.002214)
2. Model learns that spy_ret_5 is predictive
3. In production, model receives NULL for spy_ret_5
4. Model makes unpredictable decisions (garbage in, garbage out)
5. **Result**: Erratic trading behavior, potential losses

**Example Failure Scenario**:
```python
# Training (Offline)
feature_vector = [atr_14=2.5, rsi_14=45, spy_ret_5=0.0033, ...]
predicted_return = model.predict(feature_vector)  # 0.15% (BUY signal)

# Production (Realtime)
feature_vector = [atr_14=2.5, rsi_14=45, spy_ret_5=NULL, ...]
predicted_return = model.predict(feature_vector)  # 0.87% (STRONG BUY - WRONG!)
# Model misinterprets NULL and makes bad decision
```

---

## What Needs To Be Done

### Task: Integrate Tier1 SQL Market Context into Realtime Feature Calculator

**File**: `services/market/features/feature_calculator.py`

**Current Code** (lines 836-843):
```python
# Position 82-90: NEW Tier-1 Market context (SPY/QQQ) - NULL for Phase 1
NULL AS spy_ret_1,     # 82
NULL AS spy_ret_5,     # 83  ← HARDCODED NULL (BROKEN)
NULL AS qqq_ret_1,     # 84
NULL AS qqq_ret_5,     # 85
NULL AS beta_spy_20,   # 86
NULL AS beta_spy_60,   # 87
NULL AS corr_spy_20,   # 88
NULL AS corr_spy_60,   # 89
NULL AS corr_qqq_20,   # 90
```

**Required Changes**:

### Step 1: Import Tier1 SQL Builder
```python
# Add to imports (line ~38)
from services.market.features.tier1_feature_sql import (
    build_market_context_sql,
    build_spy_qqq_join_cte
)
```

### Step 2: Add SPY/QQQ Join CTE
Modify `_build_feature_sql()` method (around line 551):

```python
sql = f"""
    INSERT INTO {self.feature_table} (...)
    WITH
    filtered_bars AS (...),

    -- ADD THIS: SPY/QQQ JOIN
    {build_spy_qqq_join_cte(
        bar_table=self.bar_table,
        time_filter=f"ts_end < now() - INTERVAL {self.waterline_delay_sec} SECOND AND ts_end > now() - INTERVAL {self.horizon_minutes} MINUTE",
        symbol_filter=symbol_filter
    )}

    price_lags AS (...),
    ...
"""
```

### Step 3: Replace NULL with Tier1 SQL
Replace lines 836-843 with:

```python
# Position 82-90: Tier-1 Market context (SPY/QQQ)
{build_market_context_sql(bar_sec=self.bar_sec)}
```

**Note**: `build_market_context_sql()` returns a string containing all SPY/QQQ features:
- spy_ret_1, spy_ret_5
- qqq_ret_1, qqq_ret_5
- beta_spy_20, beta_spy_60
- corr_spy_20, corr_spy_60, corr_qqq_20

---

## Testing Requirements

### Unit Tests (Required):
```python
# tests/services/market/test_feature_calculator_tier1.py

def test_spy_ret_5_not_null():
    """Verify spy_ret_5 is computed, not NULL"""
    calc = FeatureCalculator(resolution="1m")
    sql = calc._build_feature_sql(symbols_batch=["TSLA"])

    assert "NULL AS spy_ret_5" not in sql
    assert "ln(spy_close / spy_prev_5)" in sql

def test_spy_qqq_join_present():
    """Verify SPY/QQQ data is joined"""
    calc = FeatureCalculator(resolution="1m")
    sql = calc._build_feature_sql(symbols_batch=["TSLA"])

    assert "spy_data AS" in sql
    assert "qqq_data AS" in sql
    assert "LEFT JOIN spy_data" in sql
```

### Integration Tests (Required):
```bash
# 1. Start feature calculator in test mode
venv/bin/python -c "
from services.market.features.feature_calculator import FeatureCalculator
calc = FeatureCalculator(resolution='1m', horizon_minutes=60)
calc.start()
# Let it run for 5 minutes, then check ClickHouse
"

# 2. Verify spy_ret_5 is NOT NULL in features_1m
clickhouse-client --query "
SELECT
    symbol,
    ts,
    spy_ret_5
FROM trading.features_1m
WHERE symbol = 'TSLA'
  AND ts > now() - INTERVAL 10 MINUTE
ORDER BY ts DESC
LIMIT 10
FORMAT Vertical
"

# Expected: spy_ret_5 should have REAL values, not NULL
```

### Performance Tests (Required):
```sql
-- Verify query completes in < 5 seconds (before: ~500ms, after: should be < 2s)
SELECT count()
FROM trading.features_1m
WHERE ts > now() - INTERVAL 15 MINUTE
SETTINGS max_execution_time = 5
```

---

## Risks and Mitigations

### Risk 1: Query Performance Degradation
**Impact**: Feature calculation takes too long, causes backpressure
**Likelihood**: MEDIUM
**Mitigation**:
- Test with full universe (15 symbols)
- Monitor P99 latency before/after
- Ensure SPY/QQQ JOIN is indexed (ts_start)

### Risk 2: Missing SPY/QQQ Data
**Impact**: All symbols get NULL for market context features
**Likelihood**: LOW (SPY/QQQ are highly liquid)
**Mitigation**:
- Add alerting: "SPY data missing from bars table"
- Use `coalesce(spy_close, 0)` as fallback (already in Tier1 SQL)

### Risk 3: Breaking Existing Features
**Impact**: Other features start returning NULL or wrong values
**Likelihood**: LOW (isolated change)
**Mitigation**:
- Run full feature validation suite after deployment
- Compare feature values before/after for non-market-context features

---

## Acceptance Criteria

- [ ] `feature_calculator.py` no longer returns NULL for spy_ret_5
- [ ] Unit tests pass (spy_ret_5 SQL generation)
- [ ] Integration tests pass (real ClickHouse data has non-NULL values)
- [ ] Performance tests pass (P99 latency < 2s)
- [ ] No regressions in existing features (validation suite passes)
- [ ] Updated `feature_status.json`: `spy_ret_5: "enabled"` (remove "research" flag)

---

## Deployment Plan

### Pre-Deployment:
1. Run all tests in staging environment
2. Compare feature values offline vs realtime (should match)
3. Validate with sample model inference (no NaN errors)

### Deployment:
1. Deploy to Paper Trading environment first
2. Monitor for 24 hours (check logs for NULL values)
3. If clean, deploy to Production

### Post-Deployment:
1. Run feature validation daily for 1 week
2. Monitor model performance metrics (IC, Sharpe)
3. Alert if spy_ret_5 coverage drops below 95%

---

## Related Files

- `services/market/features/tier1_feature_sql.py:48-91` - SQL builder (already implemented)
- `services/market/features/feature_calculator.py:836-843` - Target file (needs modification)
- `config/feature_status.json:5` - Status registry (currently "research")
- `pipelines/rebuild_features_1m.py:214-231` - Offline implementation (reference)

---

## Estimated Effort

- Development: 2-3 hours (SQL integration + testing)
- Testing: 2-4 hours (unit + integration + performance)
- Code Review: 1 hour
- Deployment: 1 hour (staged rollout)

**Total**: 6-10 hours

---

## Notes

- This task blocks ANY model that uses spy_ret_5, beta_spy_*, or corr_spy_* features
- Do NOT deploy models with these features until this task is complete
- The Tier1 SQL logic is already correct (tested in offline pipeline)
- This is purely a plumbing task (connect existing logic to realtime pipeline)

---

**Created By**: Claude (2025-11-26)
**Approved By**: TBD
**Target Completion**: TBD
