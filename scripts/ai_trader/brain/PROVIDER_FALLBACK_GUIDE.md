# LLM Provider Fallback Mechanism - User Guide

**Created**: 2025-11-01 21:00 EST
**Status**: ✅ **INTEGRATED**
**Version**: 1.0

---

## 🎯 **Overview**

The AI Trading Brain now has **automatic provider fallback** to prevent single points of failure when calling LLM providers.

**Problem Solved**:
- **Before**: 100% dependency on `deepseek_nvidia` - if it fails/times out, entire AI Brain stops
- **After**: 3-provider fallback chain (deepseek_nvidia → gemini → openai) with automatic retry

**Key Benefits**:
- ✅ **High Availability**: If primary provider fails, automatically try backup providers
- ✅ **Fault Tolerance**: System continues working even if 1-2 providers are down
- ✅ **Statistics Tracking**: Monitor which providers are used, success rates, fallback frequency
- ✅ **Logging**: All fallback events logged to ClickHouse for analysis

---

## 📊 **Architecture**

### Provider Priority Chain

```
Request → ProviderPool
            ↓
            Try Provider #1: deepseek_nvidia (timeout: 60s, retries: 1)
            ├─ Success → Return result
            └─ Fail/Timeout
                ↓
                Try Provider #2: gemini (timeout: 30s, retries: 1)
                ├─ Success → Return result + Log fallback event
                └─ Fail/Timeout
                    ↓
                    Try Provider #3: openai (timeout: 45s, retries: 1)
                    ├─ Success → Return result + Log fallback event
                    └─ Fail/Timeout
                        ↓
                        🚨 ALL PROVIDERS FAILED → Return error
```

### File Structure

| File | Purpose | Status |
|------|---------|--------|
| `brain/provider_fallback.py` | ProviderPool implementation | ✅ Created |
| `brain/brain.py` | ControlBrain integration | ✅ Integrated |
| `brain/PROVIDER_FALLBACK_GUIDE.md` | This guide | ✅ Created |

---

## 🔧 **Configuration**

### Default Configuration (brain.py lines 124-132)

```python
self.pool = ProviderPool(
    configs=[
        ProviderConfig(name="deepseek_nvidia", priority=1, timeout=60, max_retries=1),
        ProviderConfig(name="gemini", priority=2, timeout=30, max_retries=1),
        ProviderConfig(name="openai", priority=3, timeout=45, max_retries=1),
    ],
    runs_dir=str(self.runs_dir),
    log_failures=True
)
```

**Parameters**:
- `name`: Provider name (must match `PROV` registry in brain.py)
- `priority`: 1 = highest priority (try first)
- `timeout`: Seconds to wait before timeout
- `max_retries`: Retry attempts per provider before fallback
- `model`: Optional model override (None = use default)

### Custom Configuration

If you want to change provider priorities or timeouts, modify `brain.py`:

**Example 1: Gemini as Primary**
```python
self.pool = ProviderPool(
    configs=[
        ProviderConfig(name="gemini", priority=1, timeout=30, max_retries=2),
        ProviderConfig(name="deepseek_nvidia", priority=2, timeout=60, max_retries=1),
        ProviderConfig(name="openai", priority=3, timeout=45, max_retries=1),
    ],
    runs_dir=str(self.runs_dir)
)
```

**Example 2: Single Provider Only (No Fallback)**
```python
self.pool = ProviderPool(
    configs=[
        ProviderConfig(name="deepseek_nvidia", priority=1, timeout=120, max_retries=3),
    ],
    runs_dir=str(self.runs_dir)
)
```

**Example 3: Different Models per Provider**
```python
self.pool = ProviderPool(
    configs=[
        ProviderConfig(name="deepseek_nvidia", priority=1, timeout=60, model="deepseek-ai/deepseek-r1"),
        ProviderConfig(name="gemini", priority=2, timeout=30, model="gemini-1.5-pro-002"),  # Override to Pro
        ProviderConfig(name="openai", priority=3, timeout=45, model="gpt-4o"),  # Override to 4o
    ],
    runs_dir=str(self.runs_dir)
)
```

---

## 🚀 **Usage**

### Basic Usage (Automatic)

**No code changes required!** The ControlBrain class now automatically uses the ProviderPool for all LLM calls.

```python
from brain.brain import ControlBrain

# Initialize brain (fallback is automatic)
brain = ControlBrain(
    runs_dir="./runs",
    provider="deepseek_nvidia",  # This is now just the "default" name
)

# All these methods now use automatic fallback:
plan = brain.propose_plan("morning", context={...})
decision = brain.decide("start_morning", context={...})
evaluation = brain.evaluate_plan("morning", context={...}, plan={...})
```

### Check Provider Statistics

```python
# Print statistics to console
brain.print_provider_stats()

# Get statistics as dict
stats = brain.get_provider_stats()
print(stats)
```

**Example Output**:
```
================================================================================
Provider Pool Statistics
================================================================================

Provider             Calls    Success  Fail     Timeout  Fallback Success %
--------------------------------------------------------------------------------
deepseek_nvidia      42       38       2        2        4        90.5%
gemini               4        3        1        0        1        75.0%
openai               1        1        0        0        0        100.0%

Total Calls: 47
================================================================================
```

**Interpretation**:
- `deepseek_nvidia`: 42 calls, 38 successful (90.5%), 4 fallbacks to next provider
- `gemini`: 4 calls (all were fallbacks from deepseek_nvidia), 3 successful, 1 fallback to openai
- `openai`: 1 call (was a fallback from gemini), 1 successful

---

## 📁 **Logging & Monitoring**

### 1. ClickHouse Logs (Recommended)

**Table**: `llm.session_cleanup_log` (reused for provider events)

**Query - Recent Fallback Events**:
```sql
SELECT
    formatDateTime(ts_ny, '%Y-%m-%d %H:%M:%S') as time,
    action,
    details
FROM llm.session_cleanup_log
WHERE action = 'FALLBACK_EVENT'
ORDER BY ts_ny DESC
LIMIT 20
FORMAT Pretty
```

**Example Output**:
```
┌─time────────────────┬─action──────────┬─details──────────────────────────────────────────┐
│ 2025-11-01 21:05:42 │ FALLBACK_EVENT  │ propose_plan: deepseek_nvidia → gemini (success) │
│ 2025-11-01 20:58:11 │ FALLBACK_EVENT  │ decide_start: deepseek_nvidia → openai (success) │
│ 2025-11-01 20:45:33 │ FALLBACK_EVENT  │ evaluate: ALL PROVIDERS FAILED                   │
└─────────────────────┴─────────────────┴──────────────────────────────────────────────────┘
```

### 2. File Logs (Backup)

**File**: `runs/<YYYY-MM-DD>/provider_fallback.jsonl`

**Format**:
```json
{
  "ts": "2025-11-01T21:05:42.123456",
  "operation": "propose_plan",
  "failed_providers": ["deepseek_nvidia"],
  "success_provider": "gemini",
  "attempt_number": 2,
  "all_failed": false
}
```

**View Recent Logs**:
```bash
# View today's fallback log
tail -20 runs/$(date +%Y-%m-%d)/provider_fallback.jsonl | jq .

# Count fallbacks today
cat runs/$(date +%Y-%m-%d)/provider_fallback.jsonl | jq -s 'length'

# Count by success provider
cat runs/$(date +%Y-%m-%d)/provider_fallback.jsonl | jq -r '.success_provider' | sort | uniq -c
```

### 3. Brain Call Traces

**File**: `runs/<YYYY-MM-DD>/brain_calls.jsonl`

Now includes the **actual provider used** (after fallback):

```json
{
  "ts": "2025-11-01T21:05:42.123456",
  "kind": "propose_plan",
  "session": "morning",
  "provider": "gemini",  // ← Actual provider used (was deepseek_nvidia → gemini fallback)
  "model": "deepseek-ai/deepseek-r1",
  "duration_ms": 8524,
  "output": {...}
}
```

**Query - Which providers were actually used?**
```bash
cat runs/$(date +%Y-%m-%d)/brain_calls.jsonl | jq -r '.provider' | sort | uniq -c
```

**Example Output**:
```
  38 deepseek_nvidia
   4 gemini
   1 openai
```

---

## 🧪 **Testing Fallback**

### Test 1: Simulate Provider Failure

**Method**: Temporarily remove API key to simulate failure

```bash
# Save current key
export OLD_NVIDIA_KEY=$NVIDIA_API_KEY

# Remove key to simulate failure
unset NVIDIA_API_KEY

# Run brain - should fallback to gemini
./venv/bin/python scripts/ai_trader/orchestrator.py

# Check logs for fallback event
tail runs/$(date +%Y-%m-%d)/provider_fallback.jsonl

# Restore key
export NVIDIA_API_KEY=$OLD_NVIDIA_KEY
```

**Expected Log**:
```json
{
  "ts": "2025-11-01T21:10:15.789",
  "operation": "propose_plan",
  "failed_providers": ["deepseek_nvidia"],
  "success_provider": "gemini",
  "attempt_number": 2,
  "all_failed": false
}
```

### Test 2: Simulate Timeout

**Method**: Modify timeout to very short value (e.g., 1 second)

```python
# In brain.py, temporarily change:
ProviderConfig(name="deepseek_nvidia", priority=1, timeout=1, max_retries=1),  # Very short timeout
```

**Expected Behavior**: Should timeout quickly and fallback to gemini

### Test 3: Print Statistics

```python
from brain.brain import ControlBrain

brain = ControlBrain(runs_dir="./runs")

# Make some calls
brain.propose_plan("morning", {})
brain.decide("start_morning", {})

# Print stats
brain.print_provider_stats()
```

---

## ⚠️ **Important Notes**

### 1. API Key Requirements

**All providers in the fallback chain MUST have valid API keys**, otherwise fallback will fail.

**Check API Keys**:
```bash
echo "NVIDIA_API_KEY: ${NVIDIA_API_KEY:0:10}..."
echo "GEMINI_API_KEY: ${GEMINI_API_KEY:0:10}..."
echo "OPENAI_API_KEY: ${OPENAI_API_KEY:0:10}..."
```

**If a key is missing**, the ProviderPool will throw an error when trying to create that provider.

**Solution**: Either:
- Add the missing API key to environment
- Remove that provider from the ProviderPool config

### 2. Cost Implications

**Fallback = Additional API Costs**

If `deepseek_nvidia` fails frequently and falls back to `openai`, you'll pay OpenAI rates.

**Monitor Costs**:
```bash
# Count which providers were actually used
cat runs/$(date +%Y-%m-%d)/brain_calls.jsonl | jq -r '.provider' | sort | uniq -c
```

**Adjust Priorities** if cost is a concern:
- Put cheaper providers first (gemini, deepseek_nvidia)
- Put expensive providers last (openai)

### 3. Performance Impact

**Fallback adds latency** (timeout + retry time)

**Example**:
- deepseek_nvidia timeout: 60s
- If it times out, you wait 60s + gemini call time

**Total latency**: Up to 60s + 30s + 45s = 135s worst case (if all providers timeout)

**Mitigate**:
- Use shorter timeouts for backup providers
- Monitor `duration_ms` in brain_calls.jsonl

### 4. Model Differences

Different providers have different model capabilities:
- **deepseek_nvidia**: DeepSeek R1 (reasoning model, REASONING/CONTENT format)
- **gemini**: Gemini 1.5 Flash (fast, JSON-friendly)
- **openai**: GPT-4o-mini (balanced)

**JSON Extraction**: The ProviderPool handles DeepSeek R1's REASONING/CONTENT format automatically (copied from brain.py).

### 5. Backward Compatibility

The `ControlBrain` class maintains backward compatibility:
- `self.provider` still exists (for logging)
- `self._prov` still exists (for direct access if needed)
- `_ensure_provider()` still works

**But**: The new `_ask()` method now uses ProviderPool by default.

---

## 📊 **Monitoring Dashboard (Future Enhancement)**

### Grafana Panel Ideas

**Panel 1: Provider Usage Distribution (Pie Chart)**
```sql
SELECT
    provider,
    count() as calls
FROM (
    SELECT JSONExtractString(data, 'provider') as provider
    FROM llm.ai_brain_calls
    WHERE ts_ny > now() - INTERVAL 24 HOUR
)
GROUP BY provider
```

**Panel 2: Fallback Frequency (Time Series)**
```sql
SELECT
    toStartOfInterval(ts_ny, INTERVAL 1 HOUR) as hour,
    count() as fallback_count
FROM llm.session_cleanup_log
WHERE action = 'FALLBACK_EVENT'
  AND ts_ny > now() - INTERVAL 24 HOUR
GROUP BY hour
ORDER BY hour
```

**Panel 3: Provider Success Rate (Table)**
```sql
-- To be implemented in P1.4 with ai_brain_calls table extension
```

---

## 🐛 **Troubleshooting**

### Problem 1: `ImportError: cannot import name 'ProviderPool'`

**Cause**: Python path issue

**Solution**:
```bash
# Add project root to PYTHONPATH
export PYTHONPATH=.:$PYTHONPATH

# Or use venv Python
./venv/bin/python scripts/ai_trader/orchestrator.py
```

### Problem 2: "Missing API key for gemini: env GEMINI_API_KEY"

**Cause**: Gemini API key not set

**Solution 1** (Add key):
```bash
export GEMINI_API_KEY="your_gemini_api_key"
```

**Solution 2** (Remove from fallback):
```python
# In brain.py, remove gemini from ProviderPool:
self.pool = ProviderPool(
    configs=[
        ProviderConfig(name="deepseek_nvidia", priority=1, timeout=60),
        ProviderConfig(name="openai", priority=2, timeout=45),  # Skip gemini
    ],
    runs_dir=str(self.runs_dir)
)
```

### Problem 3: "All 3 providers failed"

**Cause**: All providers are timing out or failing

**Solution**:
1. Check API keys are valid
2. Check network connectivity
3. Increase timeouts
4. Check provider status pages:
   - DeepSeek: https://status.deepseek.com
   - Gemini: https://status.cloud.google.com/
   - OpenAI: https://status.openai.com

**Debug**:
```bash
# Check recent errors
tail -50 runs/$(date +%Y-%m-%d)/provider_fallback.jsonl | jq .

# Check brain logs
tail -50 runs/$(date +%Y-%m-%d)/brain_calls.jsonl | jq .
```

### Problem 4: High Latency (>120s)

**Cause**: Multiple timeout cycles

**Solution**:
```python
# Reduce timeouts
self.pool = ProviderPool(
    configs=[
        ProviderConfig(name="deepseek_nvidia", priority=1, timeout=30, max_retries=1),  # Reduced from 60
        ProviderConfig(name="gemini", priority=2, timeout=15, max_retries=1),           # Reduced from 30
        ProviderConfig(name="openai", priority=3, timeout=30, max_retries=1),           # Reduced from 45
    ],
    runs_dir=str(self.runs_dir)
)
```

---

## 📚 **Reference**

### API - ProviderConfig

```python
@dataclass
class ProviderConfig:
    name: str                # Provider name (must match PROV registry)
    priority: int            # 1 = highest (try first)
    timeout: float = 60.0    # Timeout in seconds
    max_retries: int = 1     # Retries before fallback
    model: Optional[str] = None  # Model override (None = default)
```

### API - ProviderPool

```python
class ProviderPool:
    def __init__(
        self,
        configs: List[ProviderConfig],
        runs_dir: Optional[str] = None,
        log_failures: bool = True
    )

    async def call_with_fallback(
        self,
        prompt: str,
        operation: str = "unknown"
    ) -> Tuple[Dict[str, Any], str, str]:
        """
        Returns: (parsed_json, raw_response, provider_used)
        Raises: RuntimeError if all providers fail
        """

    def get_stats(self) -> Dict[str, Any]:
        """Get provider statistics"""

    def print_stats(self) -> None:
        """Print provider statistics to console"""
```

### API - ControlBrain (Extended)

```python
class ControlBrain:
    def __init__(
        self,
        runs_dir: str,
        provider: str = "deepseek_nvidia",  # Backward compatibility
        model: str = "deepseek-ai/deepseek-r1"
    )

    # New methods:
    def print_provider_stats(self) -> None:
        """Print provider pool statistics"""

    def get_provider_stats(self) -> Dict[str, Any]:
        """Get provider pool statistics as dict"""

    # Existing methods now use ProviderPool internally:
    def propose_plan(self, session: str, context: Dict[str, Any]) -> Dict[str, Any]
    def decide(self, action: str, context: Dict[str, Any]) -> Dict[str, Any]
    def evaluate_plan(self, session: str, context: Dict[str, Any], plan: Dict[str, Any]) -> Dict[str, Any]
```

---

## ⏭️ **Next Steps**

### Immediate (Done ✅)
- ✅ Integrate ProviderPool into brain.py
- ✅ Update all `_ask()` callers
- ✅ Add statistics methods
- ✅ Create this guide

### Phase 2 (P1.4) - Error Tracking
- [ ] Extend `ai_brain_calls` table with:
  - `success` (boolean)
  - `error_type` (timeout/api_error/json_parse_error)
  - `fallback_used` (boolean)
  - `provider_attempted` (list)
- [ ] Query provider success rates from ClickHouse
- [ ] Create Grafana dashboard panel

### Phase 3 (P1.5) - Optimization
- [ ] Analyze which providers have best latency/success rate
- [ ] Adjust priorities based on historical data
- [ ] Implement adaptive timeout (increase if success rate drops)
- [ ] Add circuit breaker (skip provider if failure rate > 80%)

---

**Status**: ✅ P1.3 Complete - Provider Fallback Integrated
**Next**: P1.4 - Extend ai_brain_calls table with error tracking
**Documentation**: Complete
**Time Taken**: ~45 minutes

