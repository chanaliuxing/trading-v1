# Repository Guidelines

## Project Structure & Module Organization
Core trading services live in `services/`, grouped by domain (execution, risk, market intake, reconciliation). Shared utilities are in `libs/`, while model helpers sit in `ai/` and strategy definitions in `strategies/`. Configuration defaults are tracked in `config/` with `config.example.yml` as the handoff template. Operational scripts reside in `scripts/`, infrastructure manifests in `infra/`, dashboards and reference docs in `dashboards/` and `doc/`, and scenario data under `db/`. Tests mirror the runtime layout inside `tests/`, with `e2e_*` folders covering cross-service flows.

## Build, Test, and Development Commands
Rely on the Makefile to keep workflows repeatable:
- `make up` — launch the local stack defined in `infra/docker-compose.yml`.
- `make verify` — validate environment variables and service liveness via `scripts/check_env.sh`.
- `make test` — execute the full pytest suite in `tests/`.
Use `python run_system.py` for the live orchestration loop and `python demo_system.py` for the bundled simulation. Run commands from the repo root to keep relative imports intact.

## Coding Style & Naming Conventions
Default to PEP 8 with 4-space indentation and snake_case modules and functions (see `libs/config.py`). Prefer explicit type hints on public methods, centralize constants instead of sprinkling literals, and route logging through the helpers in `libs/logger.py`. Prefix feature flags with `exp_` and keep config keys kebab-case to match existing YAML files.

## Strategy Integration
- Default flow is config-driven (no decorator). Add a class under `strategies/` inheriting `libs.strategy.base.StrategyAlgorithm`, then enable it in `config.yml` under `strategies:` with `module` and `class`.
- In `initialize()`, subscribe to data using `await ctx.subscribe_features([...], bar_sec=60|300)`; do not call internal engine methods directly.
- Use `libs.strategy` exports (Algorithm, DecisionEnvelope, OrderStyle, Freshness, make_decision_id). Avoid importing from `services/strategy/strategy_base.py` (legacy).
- Optional: you can register a strategy alias via `@register_strategy("name")` and map it in `strategy_registry:`; current loader still expects `module` and `class`. You may set `registry_name` per strategy entry if a custom alias is needed.
 - Legacy examples like `tsla_v7_2` predate the current loader; do not use them as a template. Prefer config‑driven loading without decorators.

## Testing Guidelines
Place unit tests beside their domain counterparts (`services/strategy` ⇒ `tests/test_strategy*.py`) and name files `test_<component>.py`. Favour descriptive test names such as `test_quota_denies_oversized_order`. Use `pytest tests -k <keyword>` for scoped runs, and extend the suites in `tests/e2e_*` whenever adding inter-service behaviour. Keep new features covered before requesting review; highlight any gaps in the PR description.

## Commit & Pull Request Guidelines
Frame commits as imperative sentences with an optional scope prefix (`strategy: prevent duplicate fills`). Keep each commit focused and include schema or config changes together with their code. Pull requests should summarise motivation, outline the solution, reference Jira/GitHub issues, and note rollout steps (migrations, toggles, dashboards). Attach screenshots or log excerpts whenever dashboards or alerts change.

## Security & Configuration Tips
Avoid committing secrets; load credentials via `.env` files derived from `config.example.yml`. Document new external dependencies, ports, or certificates in `doc/`, and update `scripts/check_env.sh` if the readiness contract changes. When rotating keys for vault-backed services, update consumers in `libs/vault_client.py` instead of storing tokens in strategy modules.
