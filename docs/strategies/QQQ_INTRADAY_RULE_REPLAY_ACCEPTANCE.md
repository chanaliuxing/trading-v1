# QQQ Intraday Rule Replay Acceptance

## Scope

Decision-by-decision replay acceptance is implemented with sampled replay fixtures for:

- `baseline`
- `v47_fast_drift`
- `v46_ab`
- `v44_elite`

Implementation:

- `./strategies/qqq_intraday_rules/parity.py`
- `./tests/unit/strategies/test_qqq_intraday_rule_replay_acceptance.py`

## What Is Verified

- Branch-level replay outcomes for sampled scenarios.
- Shared-upstream parity between stock recipe and option recipe (translator-only difference).
- Stable trader-readable reason codes for blocked decisions.
- Recipe composability without creating new strategy forks.

## Retirement Guard

Legacy QQQ option paths are guarded by config tests:

- `./tests/unit/strategies/test_qqq_intraday_option_retirement_config.py`

Checks include:

- legacy IDs remain disabled
- legacy IDs are not in telegram strategy allowlist
- enabled QQQ strategies do not reference legacy IDs in shared cap lists
