# AI Trader Test Suite

## Overview

Comprehensive test suite for the AI Trader control plane, covering:
- **Unit tests**: Individual components (gates, controllers, orchestrator logic)
- **Integration tests**: End-to-end workflows (morning sequence, learning cycle)

## Running Tests

### Run all tests
```bash
cd .
pytest scripts/ai_trader/tests/ -v
```

### Run specific test categories
```bash
# Unit tests only
pytest scripts/ai_trader/tests/unit/ -v

# Integration tests only
pytest scripts/ai_trader/tests/integration/ -v

# Tests by marker
pytest scripts/ai_trader/tests/ -v -m unit
pytest scripts/ai_trader/tests/ -v -m integration
```

### Run with coverage
```bash
# Generate coverage report
pytest scripts/ai_trader/tests/ --cov=scripts.ai_trader --cov-report=html

# View HTML report
open scripts/ai_trader/htmlcov/index.html
```

### Run specific test file or function
```bash
# Single file
pytest scripts/ai_trader/tests/unit/test_gates.py -v

# Single test class
pytest scripts/ai_trader/tests/unit/test_gates.py::TestHealthGate -v

# Single test function
pytest scripts/ai_trader/tests/unit/test_gates.py::TestHealthGate::test_check_healthy_status -v
```

## Test Structure

```
tests/
├── conftest.py                 # Shared fixtures and configuration
├── unit/                       # Unit tests (fast, isolated)
│   ├── test_gates.py          # Gate modules (health, data, exec, llm)
│   ├── test_orchestrator.py   # Core orchestrator logic
│   ├── test_controllers.py    # LLM and RunSystem controllers
│   └── test_ab_deployer.py    # AB testing logic
├── integration/                # Integration tests (slower)
│   └── test_morning_sequence.py  # End-to-end workflows
└── fixtures/                   # Test data and mocks
```

## Key Fixtures

### `temp_dir`
Temporary directory for test artifacts, automatically cleaned up.

### `mock_health_report`
Mock `system_health.json` file with healthy status.

### `mock_config`
Mock `ai_trader.yaml` configuration file.

### `mock_gen_summary`
Mock HPO output (`gen_summary.json`) with best parameters.

### `mock_env_vars`
Mock API keys for all LLM providers (auto-applied to all tests).

## Coverage Goals

| Module | Target Coverage | Current |
|--------|----------------|---------|
| Gates | 90%+ | ✅ |
| Orchestrator | 80%+ | ✅ |
| Controllers | 85%+ | ✅ |
| Brain | 70%+ | ⏳ |
| Memory | 70%+ | ⏳ |
| AB Deployer | 90%+ | ✅ |

## Writing New Tests

### Example: Unit Test
```python
from scripts.ai_trader.gates.health_gate import HealthGate

class TestHealthGate:
    def test_check_healthy(self, mock_health_report):
        gate = HealthGate(str(mock_health_report))
        result = gate.check()

        assert result["ok"] is True
        assert result["status"] == "healthy"
```

### Example: Integration Test
```python
from scripts.ai_trader.orchestrator import Orchestrator

class TestMorningSequence:
    def test_full_workflow(self, mock_config):
        orch = Orchestrator(cfg_path=str(mock_config))

        with patch.object(orch.health, 'check', return_value={"ok": True}):
            result = orch.morning_sequence()

        assert result["status"] == "ok"
```

## Test Best Practices

1. **Use fixtures**: Leverage shared fixtures in `conftest.py`
2. **Mock external dependencies**: Don't call real APIs or databases
3. **Test edge cases**: Include tests for error conditions
4. **Clear assertions**: Use descriptive assertion messages
5. **Fast tests**: Unit tests should run in < 1 second
6. **Isolation**: Tests should not depend on execution order

## CI/CD Integration

Add to your CI pipeline:
```bash
# Install test dependencies
pip install pytest pytest-cov pytest-asyncio pytest-timeout

# Run tests with coverage
pytest scripts/ai_trader/tests/ \
    --cov=scripts.ai_trader \
    --cov-report=xml \
    --cov-fail-under=70

# Upload coverage to Codecov (optional)
codecov -f coverage.xml
```

## Troubleshooting

### Import errors
Make sure you're running pytest from the project root:
```bash
cd .
pytest scripts/ai_trader/tests/
```

### ClickHouse connection errors
Unit tests should mock all ClickHouse calls. If you see connection errors:
```python
with patch('libs.db.clickhouse.ClickHouseDB'):
    # Your test code
```

### Async tests failing
For async functions, use pytest-asyncio:
```python
import pytest

@pytest.mark.asyncio
async def test_async_function():
    result = await my_async_function()
    assert result is not None
```

## Next Steps

- [ ] Add Brain module tests (LLM mocking)
- [ ] Add Memory module tests (ClickHouse mocking)
- [ ] Add evaluators tests (reflection, scorecard)
- [ ] Add end-to-end smoke tests
- [ ] Set up CI/CD pipeline with GitHub Actions
