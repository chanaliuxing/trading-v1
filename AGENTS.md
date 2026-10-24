# Repository Guidelines

## Project Structure & Modules
- Core trading and RL logic lives in `ai_brain`, `llm_trader`, `ml`, `rl_core`, and `strategies`.
- Data, features, and labels are under `data`, `features`, `labels`, `configs`, and `schema`.
- Services, infra, and monitoring live in `infra`, `services`, `monitoring`, `grafana`, and `promtail`.
- Tests are organized in `tests/unit`, `tests/integration`, and `tests/e2e`; documentation lives under `docs`.

## Build, Test & Development
- Use `make up` / `make down` to start and stop local infra (Docker services in `infra`).
- Use `make verify` or `./scripts/check_env.sh` to validate environment and dependencies.
- Run the main test suite with `make test` (alias for `pytest tests/ -v`).
- For data-quality and pipeline checks, use `make -f Makefile.tests test-quick` or the layer-specific targets.

## Coding Style & Naming
- Python 3 code uses 4-space indentation, `snake_case` for functions and variables, and `PascalCase` for classes.
- Keep modules focused and small; group domain logic with its data and configs (e.g., strategy-specific code under `strategies/...`).
- There is no enforced project-wide formatter; follow PEP 8 and keep diffs minimal. Type hints are encouraged for new or refactored code.

## Testing Guidelines
- Prefer colocated tests: mirror package structure inside `tests/unit`, `tests/integration`, or `tests/e2e`.
- Name test files `test_*.py` and test functions `test_*`; keep tests deterministic and fast.
- Before pushing, at minimum run `make test`; for data/feature changes, also run the relevant `Makefile.tests` targets.

## Commits & Pull Requests
- Git history uses short one-line summaries, often focused on the affected area (e.g., `makefile fix`, `option bug fix`).
- Use clear summaries like `backtest: fix 1m recon bug`; English or Chinese is acceptable, but be consistent within a PR.
- PRs should describe the motivation, major changes, and how to reproduce tests (commands like `make test` or `make -f Makefile.tests test-quick`).
- Link to any related issues or experiment notes and attach screenshots for UI-facing changes (Grafana, Streamlit dashboards, etc.).

## AI / Agent-Specific Notes
- Agent and LLM-related orchestration lives in `ai_brain`, `llm_trader`, and `prompts`, with additional instructions in `CLAUDE.md`, `GEMINI.md`, and `docs/guides/AGENTS.md`.
- When modifying prompts or agent flows, keep changes minimal, document new entrypoints, and add or update tests in the closest `tests/unit/ai_*` or `tests/unit/llm_*` modules.

