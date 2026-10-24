# Risk Autosettings Governance

> **Owner**: AI Trading System
> **Version**: 1.0
> **Last Updated**: 2025-11-02

## Overview

The Risk Autosettings system allows the AI Brain (DeepSeek R1) to automatically adjust risk management parameters within a whitelisted set of keys. This document defines the governance model for Paper vs Production environments.

---

## Configuration Location

**File**: `scripts/ai_trader/config/ai_trader.yaml`

**Section**: `policies.autosettings`

```yaml
policies:
  autosettings:
    enabled: true              # Enable/disable autosettings system
    require_approval: false    # Whether changes require manual approval
```

---

## Environment-Specific Policies

### Paper Trading Environment (Current)

**Configuration**:
```yaml
policies:
  autosettings:
    enabled: true
    require_approval: false
```

**Behavior**:
- ✅ AI Brain can automatically adjust risk parameters
- ✅ Changes are applied immediately without human approval
- ✅ All changes are audited in `runs/YYYY-MM-DD/audits/risk_policy_*.json`
- ✅ Backups created in `backups/config.yml.TIMESTAMP.yaml`

**Rationale**: Paper environment is safe for experimentation. Automated risk adjustments allow the system to learn optimal parameters without manual intervention.

### Production Environment (Recommended)

**Configuration**:
```yaml
policies:
  autosettings:
    enabled: true
    require_approval: true
```

**Behavior**:
- ✅ AI Brain can propose risk parameter changes
- ⚠️ Changes are **NOT applied automatically**
- ✅ Proposals are logged in audit files with `"errors": ["pending_approval"]`
- 🔒 Manual approval required via environment variable: `AI_TRADER_APPROVE_AUTOS=1`

**Approval Process**:
1. AI Brain proposes changes → logged in `risk_policy_*.json`
2. Operator reviews audit file for:
   - Proposed changes (`applied` section)
   - Brain's reasoning (`received.notes`)
   - Current vs proposed values (`from` → `to`)
3. If approved, set environment variable: `export AI_TRADER_APPROVE_AUTOS=1`
4. Restart orchestrator to apply changes
5. Unset variable after application: `unset AI_TRADER_APPROVE_AUTOS`

**Rationale**: Production requires human oversight for risk parameter changes to prevent unexpected losses from AI miscalculations.

---

## Whitelisted Risk Parameters

The system only allows changes to these specific keys (defined in `orchestrator.py:461-468`):

```python
{
    "risk_management.guardian.dd_intraday_pct",    # Intraday drawdown threshold
    "risk_management.guardian.dd_5d_pct",          # 5-day drawdown threshold
    "risk_management.hedge.auto_execute",          # Auto-hedge execution flag
    "risk_management.hedge.mode",                  # Hedge mode (delta_neutral, etc.)
}
```

**Non-whitelisted keys are rejected** with error: `"key_not_whitelisted"`

---

## Audit Trail

### Audit File Location
```
runs/ai_trader/YYYY-MM-DD/audits/risk_policy_HHMMSS.json
```

### Audit Schema

```json
{
  "received": {
    "apply": [
      {"key": "risk_management.guardian.dd_intraday_pct", "value": 1.5}
    ],
    "notes": "conservative thresholds due to no performance history"
  },
  "enabled": true,
  "require_approval": false,
  "applied": [
    {"key": "risk_management.guardian.dd_intraday_pct", "from": 3.0, "to": 1.5}
  ],
  "denied": [],
  "errors": []
}
```

### Error Codes

| Error | Meaning | Action |
|-------|---------|--------|
| `autosettings_disabled` | `enabled: false` in config | Enable autosettings or accept manual configuration |
| `pending_approval` | `require_approval: true` and no approval env var | Review and approve via `AI_TRADER_APPROVE_AUTOS=1` |
| `key_not_whitelisted` | Requested key not in whitelist | Reject or update whitelist in code |
| `write_failed:...` | File system error writing config.yml | Check permissions, disk space |

---

## Verification

### Check Current Configuration

```bash
# View current autosettings config
grep -A 3 "autosettings:" scripts/ai_trader/config/ai_trader.yaml

# Expected output (Paper):
# autosettings:
#     enabled: true
#     require_approval: false
```

### Verify Audit Logs

```bash
# Find recent risk policy audits
find runs/ai_trader -name "risk_policy_*.json" -type f -mtime -1 | head -5

# Check for errors
find runs/ai_trader -name "risk_policy_*.json" -type f -mtime -1 -exec jq -r 'select(.errors | length > 0) | .errors[]' {} \;

# Expected (Paper): No "autosettings_disabled" errors
# Expected (Paper): "applied" section should be non-empty when Brain proposes changes
```

### Verify Backups

```bash
# List recent config backups
ls -lht backups/config.yml.*.yaml | head -5
```

---

## Validation Criteria

### Paper Environment (Current)

- ✅ `enabled: true` in `ai_trader.yaml:86`
- ✅ `require_approval: false` in `ai_trader.yaml:88`
- ✅ No `"autosettings_disabled"` errors in recent `risk_policy_*.json` files
- ✅ `"applied"` section is non-empty when Brain proposes changes
- ✅ Backups created in `backups/` directory

### Production Environment (Future)

- ✅ `enabled: true`
- ✅ `require_approval: true`
- ✅ `"pending_approval"` error appears when Brain proposes changes (expected)
- ✅ Manual approval process documented and tested
- ✅ Operator runbook includes approval workflow

---

## Implementation Details

### Code Reference

**File**: `scripts/ai_trader/orchestrator.py`

**Key Functions**:

1. **`_autosettings_enabled()` (line 489-500)**
   - Reloads `ai_trader.yaml` to catch runtime config updates
   - Returns `(enabled, require_approval)` tuple

2. **`_risk_policy_whitelist()` (line 461-468)**
   - Defines allowed risk parameter keys
   - Returns set of whitelisted dotted keys

3. **`_apply_risk_policy_update()` (line 502-564)**
   - Validates requests against whitelist
   - Applies changes to `config.yml`
   - Creates backups before modification
   - Persists audit trail

### Safety Mechanisms

1. **Whitelist Validation**: Only predefined keys can be modified
2. **Backup Before Write**: `config.yml` backed up to `backups/config.yml.TIMESTAMP.yaml`
3. **Audit Trail**: All requests logged regardless of success/failure
4. **In-Memory Reload**: Config reloaded after successful write to prevent stale data
5. **Approval Gate**: Production requires explicit human approval

---

## Migration Guide: Paper → Production

When transitioning from Paper to Production:

1. **Update Configuration**:
   ```yaml
   # In scripts/ai_trader/config/ai_trader.yaml
   policies:
     autosettings:
       enabled: true
       require_approval: true  # Changed from false
   ```

2. **Update Runbooks**: Document approval process for operators

3. **Test Approval Workflow**:
   ```bash
   # Wait for Brain to propose changes
   tail -f runs/ai_trader/$(date +%Y-%m-%d)/audits/risk_policy_*.json

   # Verify pending_approval error appears
   jq '.errors' <latest_risk_policy_file>

   # Approve and restart
   export AI_TRADER_APPROVE_AUTOS=1
   ./scripts/ai_trader/restart_orchestrator.sh
   unset AI_TRADER_APPROVE_AUTOS
   ```

4. **Monitor First Week**: Review all audit files daily to ensure system behaves as expected

---

## Monitoring & Alerts

### Prometheus Metrics (Future)

```
# Number of autosettings proposals
ai_trader_autosettings_proposals_total{status="applied|denied|pending"}

# Number of autosettings errors
ai_trader_autosettings_errors_total{error="autosettings_disabled|pending_approval|key_not_whitelisted"}
```

### Grafana Dashboard (Future)

- Panel: Autosettings proposals over time
- Panel: Approval lag (time from proposal to approval in Production)
- Alert: Autosettings disabled when Brain active

---

## FAQ

### Q: Can I disable autosettings completely?

Yes, set `enabled: false` in `ai_trader.yaml`. The Brain will stop proposing risk changes.

### Q: What happens if I delete an audit file?

Nothing critical - audit files are write-only logs. Config and backups remain intact.

### Q: Can I manually edit config.yml while autosettings is enabled?

Yes, but:
1. Stop the orchestrator first to prevent race conditions
2. Manual edits will be preserved unless Brain proposes overlapping changes
3. Backups will capture both manual and automated changes

### Q: How do I revert a bad autosettings change?

```bash
# Find the backup before the change
ls -lt backups/config.yml.*.yaml

# Restore it
cp backups/config.yml.20251102_013547.yaml config.yml

# Restart orchestrator
./scripts/ai_trader/restart_orchestrator.sh
```

---

## Change Log

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2025-11-02 | Initial documentation; Paper config verified; Production policy defined |

---

## Related Documents

- [CLAUDE.md](../CLAUDE.md) - Overall development guidelines
- [orchestrator.py](../scripts/ai_trader/orchestrator.py:461-564) - Implementation code
- [ai_trader.yaml](../scripts/ai_trader/config/ai_trader.yaml:86-88) - Configuration file
