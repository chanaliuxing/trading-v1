# Replica Baselines

This directory stores frozen baseline artifacts used by the `index_super_win` replica program.

Current baseline:

- `qqq_intraday_001_replica_baseline.json`
- `qqq_intraday_option_v1_001_replica_baseline.json`
- `QQQ_INTRADAY_001_REPLICA_BASELINE.md`

These artifacts freeze:

1. current `qqq_intraday` strategy configuration
2. current shared execution-chain version
3. current replay parity field contract
4. minimum replay fixture set for first proof

Generate or refresh the baseline:

```bash
./venv/bin/python tool/build_qqq_replica_baseline.py
```

Use this baseline for:

1. replay harness input contract
2. parity mismatch triage
3. shadow report reference
4. cut-in rollback reference

Do not update the baseline casually during replica work.

If `qqq_intraday` behavior or shared execution-chain behavior changes intentionally, regenerate the baseline in the same change and record the reason.
