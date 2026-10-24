# INDEX_SUPER_WIN QQQ Session Enablement Runbook

## Scope

This runbook enables three `index_super_win`-based `QQQ` session strategies as relay sessions:

1. `qqq_premarket_001`
2. `qqq_overnight_001`
3. `qqq_afterhours_001`

They are designed to coexist as deployable instances, but not to hold live inventory on the same account/symbol at the same time.

## Safety Model

The relay model is:

1. Only the active session window can open new inventory.
2. Session close triggers `force_flatten_time`.
3. The next session can open only when all of the following are true:
   - previous runtime snapshot is flat
   - broker position is flat
   - previous session open orders are clear
4. If any proof is missing, the next session blocks entry fail-closed.

## Config Templates

Templates:

1. [qqq_premarket_001.example.yml](./configs/index_super_win/qqq_premarket_001.example.yml)
2. [qqq_overnight_001.example.yml](./configs/index_super_win/qqq_overnight_001.example.yml)
3. [qqq_afterhours_001.example.yml](./configs/index_super_win/qqq_afterhours_001.example.yml)

Required uniqueness:

1. `strategy_id`
2. `runtime_scope`
3. `runtime_snapshot_path`
4. `diag_snapshot_path`
5. per-instance audit output path

## Preflight Commands

### 0. One-shot check runner

```bash
./venv/bin/python ./tool/run_index_super_win_session_enablement_checks.py \
  --session-config ./configs/index_super_win/qqq_premarket_001.example.yml \
  --session-config ./configs/index_super_win/qqq_overnight_001.example.yml \
  --session-config ./configs/index_super_win/qqq_afterhours_001.example.yml \
  --broker-positions ./runtime/state/index_super_win_replica_broker_positions.json
```

This writes:

1. `runtime/state/index_super_win_session_bundle_preflight.json`
2. `runtime/state/index_super_win_session_preflight/*.json`
3. `runtime/state/index_super_win_same_symbol_proof/final_enablement_checklist_sessions.json`

### 1. Bundle preflight

```bash
./venv/bin/python ./tool/index_super_win_session_bundle_preflight.py \
  --config ./configs/index_super_win/qqq_premarket_001.example.yml \
  --config ./configs/index_super_win/qqq_overnight_001.example.yml \
  --config ./configs/index_super_win/qqq_afterhours_001.example.yml \
  --output-path ./runtime/state/index_super_win_session_bundle_preflight.json
```

Expected:

1. `"ready": true`
2. `"ready_for_enablement": true`
3. `"errors": []`

### 2. Per-instance preflight

Run once per instance with its own manifest/runtime/broker snapshot:

```bash
./venv/bin/python ./tool/index_super_win_replica_preflight.py \
  --manifest ./configs/index_super_win/qqq_replica_manifest.example.yml \
  --runtime-snapshot ./runtime/state/index_super_win_replica_runtime_qqq_premarket_001.json \
  --broker-positions ./runtime/state/index_super_win_replica_broker_positions.json \
  --output-path ./runtime/state/index_super_win_replica_preflight_qqq_premarket_001.json \
  --json
```

Repeat for `qqq_overnight_001` and `qqq_afterhours_001`.

Expected:

1. `"ready_for_enablement": true`

### 3. Final checklist

```bash
./venv/bin/python ./tool/replay/build_replica_final_enablement_checklist.py \
  --config ./config.yml \
  --strategy-id index_super_win_001 \
  --preflight-report ./runtime/state/index_super_win_replica_preflight.json \
  --real-session-summary ./runtime/state/index_super_win_same_symbol_proof/real_session_proof_daily_summary.json \
  --batch-report ./runtime/state/index_super_win_same_symbol_proof/real_session_dual_proof_batch_report.json \
  --isolation-daily-summary ./runtime/state/multi_strategy_isolation_daily_summary.json \
  --trace-smoke-gate ./runtime/state/index_super_win_trace_smoke_gate.json \
  --session-bundle-report ./runtime/state/index_super_win_session_bundle_preflight.json \
  --output-path ./runtime/state/index_super_win_same_symbol_proof/final_enablement_checklist.json
```

Expected:

1. `"checks.session_bundle_ready_for_enablement": true`
2. `"ready_to_enable": true`
3. no `session_bundle_not_green` blocker

## Runtime Audit Expectations

For session handoff, audit should show:

1. `session_handover_ready`
2. `session_end_flatten_requested`
3. `session_end_flatten_deduped` only if repeated submit was suppressed
4. `session_end_flatten_confirmed`
5. `previous_session_not_flat_blocked` when handoff proof is missing

## Go / No-Go Rules

Go only if:

1. Bundle preflight is green
2. Instance preflight is green
3. Final checklist is green
4. Current broker position is flat for the symbol
5. Current open orders are clear for the previous session

No-Go if:

1. Any session window overlaps
2. Any previous-session flat proof is missing
3. Any open order from the previous session remains live
4. Any checklist blocker remains red

## Paper Deployment Order

1. Keep existing `qqq_intraday_001` unchanged.
2. Enable only one new session instance at a time for first paper verification.
3. Observe:
   - entry allowed only inside its session
   - end-of-session flatten requested
   - broker flat confirmation
   - next session handoff ready
4. Then enable the full relay chain.

## Acceptance

The relay session deployment is acceptable only if:

1. No session instance cancels or flattens `qqq_intraday_001`
2. No session instance touches option strategy orders
3. No repeated flatten submit loop appears
4. No reverse open appears at session boundary
5. Broker/runtime/open-order proof is consistent across handoff
