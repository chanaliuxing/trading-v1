# Run Multi-Agent Workflow with Codex CLI Sessions

## Full pipeline
```bash
cd .
python3 tools/agent_system/codex_cli_orchestrator.py \
  --project-root . \
  --objective-file docs/architecture/multi_strategy_engine/objective.example.md \
  --run-name mse_round_001
```

## Continue from interruption
- Re-run with same `--run-name`.
- Stages with `PASS` are skipped by default.

```bash
python3 tools/agent_system/codex_cli_orchestrator.py \
  --project-root . \
  --objective-file docs/architecture/multi_strategy_engine/objective.example.md \
  --run-name mse_round_001
```

## Force rerun specific stage onward
```bash
python3 tools/agent_system/codex_cli_orchestrator.py \
  --project-root . \
  --objective-file docs/architecture/multi_strategy_engine/objective.example.md \
  --run-name mse_round_001 \
  --start-from validate \
  --no-skip-pass-stages
```

## Artifacts
- `runtime/agent_runs/<run>/sessions.json`
- `runtime/agent_runs/<run>/summary.json`
- `runtime/agent_runs/<run>/state.json`
- `runtime/agent_runs/<run>/<NN>_<stage>/agent_message.md`
- `runtime/agent_runs/<run>/<NN>_<stage>/codex_events.jsonl`
