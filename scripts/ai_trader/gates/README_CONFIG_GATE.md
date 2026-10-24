# ConfigGate - Configuration Validation Layer

## Overview

ConfigGate provides comprehensive configuration validation before system startup, preventing runtime failures caused by invalid configurations.

## Features

- ✅ **Schema Validation**: Type checking, required fields, enum values
- ✅ **File Reference Validation**: Verify existence of referenced files
- ✅ **Consistency Validation**: Cross-configuration consistency checks
- ✅ **Grid Guard Integration**: Reuses existing HPO parameter validation
- ✅ **Fast Execution**: <2 seconds validation time
- ✅ **Fail-Fast**: Block startup on critical errors

## Architecture

```
ConfigGate
├── validators/
│   ├── schema.py          # Schema & type validation
│   ├── file_ref.py        # File existence checks
│   └── consistency.py     # Cross-config consistency
└── config_gate.py         # Main gate implementation
```

## Usage

### 1. Standalone CLI Tool

```bash
# Basic validation
python tools/validate_all_configs.py

# Validate with HPO config
python tools/validate_all_configs.py --hpo-config config/hpo_enhanced.json

# Strict mode (warnings = errors)
python tools/validate_all_configs.py --strict

# Export JSON report
python tools/validate_all_configs.py --report-json validation_report.json
```

### 2. Programmatic Usage

```python
from scripts.ai_trader.gates.config_gate import ConfigGate, ConfigGatePolicy

# Create policy
policy = ConfigGatePolicy(
    schema_validation=True,
    grid_validation=True,
    file_ref_validation=True,
    consistency_validation=True,
    fail_on_schema_error=True,
    fail_on_file_not_found=True,
    fail_on_inconsistency=False,  # Warnings only
)

# Create gate
gate = ConfigGate(policy)

# Run validation
result = gate.check(
    config_path="config.yml",
    hpo_config_path="config/hpo_enhanced.json"
)

if not result["ok"]:
    print(f"Validation failed: {result['reasons']}")
    exit(1)
```

### 3. Integration with Orchestrator

The ConfigGate is automatically invoked when the Orchestrator starts (see next section).

## Validation Rules

### Schema Validation

- **Required fields**: mode, universe, strategies, risk, execution, kafka, clickhouse
- **Mode enum**: Must be 'paper' or 'live'
- **Universe**: Must be list of uppercase symbols
- **Strategies**: Must have id, module, class, params.extras

### File Reference Validation

- **LKG files**: Validates Last Known Good configuration files
- **Param files**: Validates premarket_calibration.param_file
- **Model files**: Validates RL model paths (if enabled)
- **Symbol configs**: Warns if per-symbol configs missing

**Smart Handling**:
- Enabled strategies: Missing files → ERROR
- Disabled strategies: Missing files → WARNING

### Consistency Validation

- **Symbol consistency**: Strategy symbols must be in universe
- **Unique IDs**: No duplicate strategy IDs
- **HPO gates**: min_unique_params ≤ min_trials
- **Risk parameters**: Reasonable ranges for per_trade_risk_pct, etc.

### Grid Validation (HPO)

Reuses `grid_guard.py` to validate:
- **Parameter bounds**: Stop loss, take profit, sizing, etc.
- **Type checking**: int vs float
- **Relational constraints**: stop_loss < take_profit
- **Grid size**: Warns if combinations > 20,000

## Configuration Policy

```python
@dataclass
class ConfigGatePolicy:
    # Validation toggles
    schema_validation: bool = True
    grid_validation: bool = True
    file_ref_validation: bool = True
    consistency_validation: bool = True

    # Failure policies
    fail_on_schema_error: bool = True
    fail_on_file_not_found: bool = True
    fail_on_inconsistency: bool = False  # Warnings only
    fail_on_grid_violation: bool = True

    # Grid constraints
    default_grid_bounds: Dict[str, tuple] = {
        "risk.stop_loss_pct": (0.005, 0.15),
        "risk.take_profit_pct": (0.005, 0.25),
        "sizing.multiplier": (0.5, 5.0),
        # ... more bounds
    }
```

## Output Format

```json
{
  "ok": true,
  "reasons": [],
  "warnings": [
    "lkg_file_not_found_disabled_strategy: strategies/configs/tsla_v7_2_lkg.json (strategy: tsla_v7_2_001)"
  ],
  "details": {
    "schema": {"ok": true, "errors": []},
    "grid": {"ok": true, "reasons": [], "details": {...}},
    "file_refs": {"ok": true, "missing": [], "warnings": [...]},
    "consistency": {"ok": true, "issues": [], "warnings": []}
  }
}
```

## Exit Codes

- `0`: Validation passed
- `1`: Validation failed (errors found)

In strict mode, warnings also cause exit code 1.

## Examples

### Example 1: Detect Missing Universe Symbol

**Error**:
```
symbol_not_in_universe: 'GOOG' used by strategy 'my_strategy_001'
is not in universe ['AAPL', 'TSLA', 'NVDA']
```

**Fix**:
```yaml
universe:
  - AAPL
  - TSLA
  - NVDA
  - GOOG  # Add missing symbol
```

### Example 2: HPO Grid Too Large

**Warning**:
```
grid_too_large: 50000 combinations (max recommended: 20000).
This may waste compute resources.
```

**Fix**: Reduce grid granularity or use fewer parameters.

### Example 3: Invalid Risk Parameter

**Error**:
```
risk.per_trade_risk_pct: 0.15 outside safe range (0, 0.1]
```

**Fix**:
```yaml
risk:
  per_trade_risk_pct: 0.02  # Reduce to 2%
```

## Performance

- **Validation time**: <2 seconds for typical config
- **Memory usage**: <50 MB
- **No external dependencies**: Uses stdlib + existing libs

## Testing

```bash
# Run unit tests
pytest scripts/ai_trader/gates/tests/

# Run with coverage
pytest --cov=scripts.ai_trader.gates scripts/ai_trader/gates/tests/
```

## Roadmap

### Phase 1: Core Validation (✅ Completed)
- Schema validation
- File reference validation
- Consistency validation
- Grid guard integration
- CLI tool

### Phase 2: Smart Review (Planned)
- Brain (LLM) integration for config reviews
- Risk scoring for config changes
- Auto-suggestions for fixes

### Phase 3: Monitoring (Planned)
- Prometheus metrics (config_validation_errors_total)
- Grafana dashboard
- Auto-rollback on validation failure

## FAQ

**Q: Can I skip validation for quick tests?**

A: Yes, use `--no-file-validation` or `--no-grid-validation` flags:

```bash
python tools/validate_all_configs.py --no-file-validation
```

**Q: How do I add custom validation rules?**

A: Extend the validators or add custom checks in `ConsistencyValidator.validate_consistency()`.

**Q: What if I have a false positive?**

A: File an issue or adjust the policy settings to treat it as a warning instead of an error.

## Support

- **Issues**: File at GitHub Issues
- **Docs**: See `docs/CONFIG_VALIDATION.md` (TODO)
- **Examples**: See `config/examples/`

## License

Internal trading system component.
