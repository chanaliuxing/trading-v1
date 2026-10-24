# Trading System Test Suite

Comprehensive test suite for the trading system, organized for CI/CD integration.

## Directory Structure

```
tests/
├── unit/                   # Unit tests (fast, isolated)
│   ├── rl/                # RL environment, rewards, state building
│   ├── strategies/        # Strategy logic, ensemble combination
│   ├── backtest/          # Backtest engine, data loading
│   ├── data/              # Data adapters, OOF loading
│   └── utils/             # Utility functions
│
├── integration/           # Integration tests (require services)
│   ├── test_ensemble_comparison.py  # XGB/LSTM/Fusion comparison
│   └── test_rl_evaluation.py       # RL training and evaluation
│
├── e2e/                   # End-to-end tests (full workflows)
│   └── test_trading_workflow.py    # Complete trading workflows
│
├── pytest.ini             # Pytest configuration
├── run_all_tests.sh       # Test runner script (CI/CD ready)
└── README.md              # This file
```

## Test Categories

### Unit Tests
- **Fast**: < 1 second per test
- **Isolated**: No external dependencies (databases, services)
- **Coverage**: Core logic, calculations, transformations

### Integration Tests
- **Medium speed**: 1-30 seconds per test
- **Services**: Require ClickHouse, Redis, or other services
- **Coverage**: Component interactions, data pipelines

### E2E Tests
- **Slow**: 30+ seconds per test
- **Full stack**: Complete workflows from data to results
- **Coverage**: User scenarios, production-like behavior

## Running Tests

### Quick Start (Fast Tests Only)
```bash
./tests/run_all_tests.sh
```

### Run All Tests (Including Slow)
```bash
./tests/run_all_tests.sh --all
```

### Run Specific Test Category
```bash
./tests/run_all_tests.sh --unit         # Unit tests only
./tests/run_all_tests.sh --integration  # Integration tests only
./tests/run_all_tests.sh --e2e          # E2E tests only
```

### Run Individual Test File
```bash
pytest tests/unit/rl/test_trading_env.py -v
pytest tests/integration/test_ensemble_comparison.py -v
```

### Run Tests by Marker
```bash
pytest -m unit           # Unit tests only
pytest -m integration    # Integration tests only
pytest -m e2e            # E2E tests only
pytest -m "not slow"     # Skip slow tests
```

## Test Markers

Tests are marked with pytest markers for categorization:

- `@pytest.mark.unit`: Unit tests (fast, isolated)
- `@pytest.mark.integration`: Integration tests (require services)
- `@pytest.mark.e2e`: End-to-end tests (full workflows)
- `@pytest.mark.slow`: Slow tests (skip in CI fast mode)
- `@pytest.mark.requires_data`: Tests requiring real data
- `@pytest.mark.requires_model`: Tests requiring trained models

## CI/CD Integration

### GitHub Actions Example
```yaml
name: Tests

on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v2
      - name: Set up Python
        uses: actions/setup-python@v2
        with:
          python-version: 3.9
      - name: Install dependencies
        run: |
          pip install -r requirements.txt
          pip install pytest
      - name: Run fast tests
        run: ./tests/run_all_tests.sh
      - name: Run all tests (weekly)
        if: github.event_name == 'schedule'
        run: ./tests/run_all_tests.sh --all
```

### GitLab CI Example
```yaml
test:
  stage: test
  script:
    - pip install -r requirements.txt pytest
    - ./tests/run_all_tests.sh
  only:
    - merge_requests
    - master
```

## Test Coverage

To generate coverage reports:

```bash
pytest tests/ --cov=. --cov-report=html --cov-report=term
```

View HTML report:
```bash
open htmlcov/index.html
```

## Writing New Tests

### Unit Test Template
```python
import pytest

class TestMyModule:
    @pytest.fixture
    def mock_data(self):
        return {"key": "value"}

    def test_my_function(self, mock_data):
        result = my_function(mock_data)
        assert result == expected_value
```

### Integration Test Template
```python
import pytest

class TestMyIntegration:
    @pytest.mark.integration
    @pytest.mark.slow
    def test_database_integration(self):
        # Test with real database
        pass
```

### Test Naming Conventions
- Test files: `test_*.py`
- Test classes: `Test*`
- Test functions: `test_*`

## Troubleshooting

### Tests fail with import errors
```bash
# Ensure virtual environment is activated
source venv/bin/activate

# Install test dependencies
pip install pytest pytest-asyncio
```

### Tests fail with database connection errors
```bash
# Ensure services are running
make up

# Verify services
make verify
```

### Slow tests timeout
```bash
# Increase timeout in pytest.ini
timeout = 600  # 10 minutes
```

## Test Statistics

Total test files: 7
- Unit tests: 4 files (RL, strategies, backtest, data adapters)
- Integration tests: 2 files (ensemble comparison, RL evaluation)
- E2E tests: 1 file (complete workflows)

Estimated run times:
- Fast mode (unit tests): ~10-30 seconds
- Integration tests: ~1-5 minutes
- All tests (including E2E): ~5-15 minutes

## Maintenance

### Adding New Tests
1. Create test file in appropriate directory (unit/integration/e2e)
2. Add appropriate markers (@pytest.mark.unit, etc.)
3. Update this README if adding new test categories

### Updating Tests
- When modifying code, update corresponding tests
- Keep test coverage above 80% for core modules
- Run `./tests/run_all_tests.sh --all` before committing

### Test Review Checklist
- [ ] Tests are isolated (no side effects)
- [ ] Tests are deterministic (no flaky tests)
- [ ] Tests have clear names and docstrings
- [ ] Mock external dependencies appropriately
- [ ] Tests run in < 1 second (unit) or marked as slow

## Contact

For questions about tests or CI/CD integration, contact the development team.
