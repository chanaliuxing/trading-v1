# DSL Test Cases

This directory contains test cases for the Feature DSL to SQL compiler.

## Files

- `dsl_test_cases.json` - Comprehensive test cases covering all DSL operations

## Test Case Structure

Each test case has the following structure:

```json
{
  "name": "unique_test_name",
  "description": "Human-readable description",
  "dsl": {
    "op": "operation_name",
    ...
  }
}
```

## Supported Operations

### Basic Operations

1. **ratio** - Division with epsilon protection
   - Fields: `num`, `den`, `eps`
   - Example: `f.ret_5 / nullIf(f.atr_14, 1e-6)`

2. **ratio_minus_1** - Ratio minus 1 (for percentage change)
   - Fields: `num`, `den`, `eps`
   - Example: `(f.close / nullIf(f.vwap, 1e-6)) - 1`

3. **log1p** - Natural log of (1 + x)
   - Fields: `col`
   - Example: `log1p(f.volume)`

4. **product** - Multiplication
   - Fields: `col1`, `col2` OR `cols` (array)
   - Example: `f.ret_5 * f.atr_14`

5. **sum** - Addition
   - Fields: `col1`, `col2` OR `cols` (array)
   - Example: `f.ret_5 + f.ret_15`

6. **abs** - Absolute value
   - Fields: `col`
   - Example: `abs(f.ret_5)`

### Nested Operations

Operations can be nested to create complex expressions:

```json
{
  "op": "ratio",
  "num": {
    "op": "product",
    "cols": ["f.ret_5", {"op": "abs", "col": "f.atr_20"}]
  },
  "den": {"op": "sum", "cols": ["f.atr_14", 1e-06]},
  "eps": 1e-06
}
```

This generates: `(f.ret_5 * abs(f.atr_20)) / nullIf((f.atr_14 + 1e-06), 1e-06)`

## Column Aliases

The DSL supports three table aliases:

- `f.*` - 1-minute features (trading.features_1m)
- `m5.*` - 5-minute features (trading.features_5m)
- `d.*` - Daily/previous-day features (trading.features_1d_prev)

## Running Tests

### Using the DSL test tool directly:

```bash
venv/bin/python tools/test_feature_dsl_from_llm.py tests/fixtures/dsl_test_cases.json
```

### Using pytest:

```bash
pytest tests/test_feature_dsl.py -v
```

## Adding New Test Cases

When adding new test cases:

1. Ensure the `name` is unique and descriptive
2. Add a clear `description`
3. Verify the DSL structure matches the supported operations
4. Test with the tool before committing
5. Update this README if introducing new patterns

## Expected Output

When tests pass, you should see:

```
✅ PASSED
SQL: (f.ret_5) / nullIf(f.atr_14, 1e-06)
```

When tests fail, you should see:

```
❌ FAILED
Error: Unknown DSL op: invalid_op
```

## Integration with Brain

These test cases serve as examples for the AI Brain when generating new features.
The Brain can use these patterns to:

1. Understand supported DSL operations
2. Learn proper nesting syntax
3. Generate valid feature expressions
4. Avoid compilation errors

## Validation Pipeline

New features proposed by the Brain go through:

1. **DSL Compilation** (this tool) - Syntax validation
2. **Schema Validation** (brain.tools_sql.dry_run_feature_expr) - Column existence
3. **Data Quality** (tools/verify_feature_experiment.py) - Coverage & value range
4. **OOF/HPO** - Model performance validation

Only features passing all stages are promoted to production.
