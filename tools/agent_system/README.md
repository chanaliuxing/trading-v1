# Codex CLI Agent Orchestrator

Run staged multi-agent workflows using Codex CLI sessions as long-lived memory per role.

## Why
- No direct API orchestration required.
- Each agent role keeps its own persistent session context.
- Workflow outputs and raw event logs are persisted for audit.

## Script
- `tools/agent_system/codex_cli_orchestrator.py`

## Quick Start
```bash
cd .
python3 tools/agent_system/codex_cli_orchestrator.py \
  --project-root . \
  --objective-file docs/architecture/multi_strategy_engine/objective.example.md \
  --run-name mse_round_001
```

## Run only selected stages
```bash
python3 tools/agent_system/codex_cli_orchestrator.py \
  --project-root . \
  --objective "Draft PRD then review it" \
  --stages requirements,review
```

## Resume / continue from interruption
- Reuse the same `--run-name`; orchestrator will reuse saved stage sessions from `sessions.json`.
- By default, stages already marked `PASS` are skipped.

```bash
python3 tools/agent_system/codex_cli_orchestrator.py \
  --project-root . \
  --objective-file docs/architecture/multi_strategy_engine/objective.example.md \
  --run-name mse_round_001 \
  --start-from validate
```

Useful flags:
- `--no-skip-pass-stages`: rerun all selected stages even if already PASS.
- `--no-halt-on-fail`: continue pipeline even when one stage reports FAIL/BLOCKED.

## Outputs
Saved under `runtime/agent_runs/<run_name>/`:
- `sessions.json`: stage -> Codex session ID mapping
- `summary.json`: stage statuses and artifact pointers
- `state.json`: last executed stage/status checkpoint
- `<NN>_<stage>/prompt.txt`
- `<NN>_<stage>/codex_events.jsonl`
- `<NN>_<stage>/agent_message.md`

## Session Reuse
Re-run with the same `--run-name` to keep using the same stage sessions and continue from checkpoints.

## Notes
- Stage status is parsed from first line matching `Status: PASS|FAIL|BLOCKED`.
- By default workflow halts on FAIL/BLOCKED.

- `--stop-before-stage <stage>`: hard stop before a stage (e.g. `dev`).

- Missing explicit `Status:` is auto-classified as `BLOCKED` and halts by default.
