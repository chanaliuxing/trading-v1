# V3.1 Test Plan

## Scope
- Validate core strategy logic (lanes, scoring, exits).
- Validate execution safety guards (Fix-1/2/3) behavior in isolation.
- Ensure config integration does not break YAML parsing.

## Environments
- Local dev (no IB connectivity required).
- Python 3.11 (pytest runner as configured in `tests/pytest.ini`).

## Test Sets
1) Unit tests (logic + guard)
- File: `tests/unit/strategies/v3_1/test_v3_1_strategy.py`
- Coverage:
  - Lane2 pullback trigger
  - Protect → move stop
  - Gap/target ordering
  - Structure scoring
  - NoProgress exit
  - STATE=OFF gating
  - Scarcity expansion
  - Hedge cost reduction
  - Open-order conflict detection
  - Cancel-wait confirmation

2) Config sanity
- Validate `config.yml` parses and includes `v3_1_equity` entry.

## Execution Steps
1) Unit tests
```
pytest tests/unit/strategies/v3_1/test_v3_1_strategy.py
```

2) Config sanity (lightweight parse)
```
python - <<'PY'
import yaml
cfg = yaml.safe_load(open('config.yml','r',encoding='utf-8'))
ids = [s.get('id') for s in cfg.get('strategies', [])]
print('v3_1_equity' in ids)
PY
```

## Pass Criteria
- All unit tests pass.
- Config parse prints `True` for `v3_1_equity`.

## Notes
- IBKR connectivity tests are intentionally excluded; guard logic is covered via unit tests.
