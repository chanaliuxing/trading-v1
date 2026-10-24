Build and validate a generalized multi-strategy execution system for trading.

Constraints:
- Support multiple strategies concurrently.
- Support multiple strategies trading the same symbol concurrently.
- Strong strategy isolation using strategy_id + strategy_run_id.
- Full traceability from trigger -> decision -> intent -> order -> fill.
- Prepare fully before integrating into run_system.
- Require strong tests, regressions, and acceptance gates.

Deliverables:
- PRD, I/O schema contract, acceptance criteria, rollout plan.
- Critical review and adversarial challenge loop.
- Validation report and implementation evidence expectations.
