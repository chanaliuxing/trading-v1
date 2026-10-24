# Skill Registry Guidelines

Fields (per `config/skill_registry.json`):
- `file_path` (required): source file implementing the skill.
- `test_path` (recommended): unit test path.
- `type`: `PURE` | `DB_READ` | `SIDE_EFFECT`.
- `allow_evolution`: whether automated evolvers may modify this skill.
- `objective`: `PERFORMANCE` | `ALPHA` | `ROBUSTNESS` | `GOVERNANCE`.
- `metrics_impact`: list of metrics the skill can affect.
- `description`: short human-readable summary.

Safety defaults:
- `allow_evolution=false` for governance/brain core/risk/execution/schema.
- Start evolver trials on sandbox skills (e.g., `dummy_math`) before production skills.
- PURE skills may use golden IO fixtures for regression checks.
- Denylist: evolution is blocked for paths under `libs/exec`, `services/execution`, `services/risk`, `schema`, `infra`.
