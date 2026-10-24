# Multi-Strategy Isolation Monitor Runbook

## Purpose

This monitor is designed to stay quiet during normal trading and only surface high-value cross-strategy risks.

It produces three local files:

- `runtime/state/multi_strategy_isolation_snapshot.json`
- `runtime/state/multi_strategy_isolation_alerts.json`
- `runtime/state/multi_strategy_isolation_daily_summary.json`

## Noise Model

- Normal events stay in `runtime/audit/YYYY-MM-DD/exec-*.jsonl`
- `snapshot.json` is refreshed at low frequency
- `alerts.json` keeps only high-value anomalies
- `daily_summary.json` is for post-close review

High-value anomaly stages:

- `entry_isolation_blocked_pending_order`
- `entry_isolation_blocked_live_position`
- `fill_attribution_fallback_broker_order_id`
- `fill_attribution_ambiguous`
- `broker_open_orders_quarantined`
- `broker_position_quarantined`

## Recommended Frequency

- Poll interval: `5s`
- Snapshot refresh: `60s`
- Daily summary: once after close

This means:

- the monitor checks for new high-value anomalies every 5 seconds
- it refreshes the main snapshot at most once per 60 seconds during quiet periods
- if a new high-value anomaly appears, it refreshes immediately

## Commands

Intraday low-noise monitor:

```bash
bash scripts/automation/isolation_monitor.sh
```

Custom cadence:

```bash
SNAPSHOT_INTERVAL_SEC=60 POLL_INTERVAL_SEC=5 ITERATIONS=720 bash scripts/automation/isolation_monitor.sh
```

Post-close daily summary:

```bash
bash scripts/automation/isolation_daily_summary.sh
```

Specific day:

```bash
bash scripts/automation/isolation_daily_summary.sh 2026-03-14
```

Install launchd jobs:

```bash
bash scripts/setup_isolation_monitor_launchd.sh
```

Uninstall launchd jobs:

```bash
bash scripts/uninstall_isolation_monitor_launchd.sh
```

## How To Read

### `multi_strategy_isolation_alerts.json`

Use this first.

It answers:

- was there any cross-strategy isolation block
- was there any dangerous fill attribution fallback
- did we hit ambiguous broker attribution

### `multi_strategy_isolation_snapshot.json`

Use this second.

It answers:

- what each symbol currently looks like from the qqq/index/system view
- whether index was blocked by ownership, open orders, or entry isolation
- the latest fill attribution risk on that symbol

### `multi_strategy_isolation_daily_summary.json`

Use this after the session.

It answers:

- how many high-value isolation events happened today
- which symbols were affected
- what the dominant failure mode was

## Operational Recommendation

- Do not send these files to Telegram by default
- Keep them local and quiet
- Only escalate manually when `alerts.json` shows non-empty high-risk entries

## launchd Schedule

- `com.trading.isolation.monitor.intraday`: starts at `09:31` local time
- `com.trading.isolation.summary.postclose`: runs at `16:10` local time
- If `run_system` is not running, these jobs stay fail-open:
  - monitor keeps writing quiet empty/partial snapshots
  - summary writes a quiet empty/partial daily summary
- Weekend runs are skipped by default inside the wrapper scripts
