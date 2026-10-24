.PHONY: help up down verify logs test test-startup-smoke recon1m recon5m recon10m recon15m recon30m recon2h recon3h recon4h recon1d recon-all cost-day compute-features compute-features-all backup-pg backup-pg-full backup-ch backup-ch-daily backup-ch-full backup-pg-schema backup-ch-schema backup-daily backup-restore-ch setup-backup-automation setup-daily-automation setup-midnight-automation backfill-5d nightly-data-refresh
.PHONY: premarket-coach coach-apply coach-report global-gate replay-day nightly-hpo
.PHONY: hpo-apply config-rollback circuit-breaker
.PHONY: compare-ensemble
.PHONY: verify-oof setup-oof-view

PYTHON ?= ./venv_ml/bin/python
PYTEST ?= $(PYTHON) -m pytest

help:
	@echo "Trading System - Makefile Commands"
	@echo "=================================="
	@echo "up          - Start all infrastructure services"
	@echo "down        - Stop all infrastructure services"
	@echo "verify      - Verify all services are healthy"
	@echo "logs        - Tail logs from all services"
	@echo "test        - Run tests"
	@echo "check-env   - Check environment configuration"
	@echo ""
	@echo "Data Reconciliation (Phase 5):"
	@echo "recon1m     - Reconcile 1m bars (last 3 days)"
	@echo "recon-all   - Run all reconciliation tasks (5pm automation)"
	@echo "check-positions - Verify IB vs DB positions (ghost guard)"
	@echo "check-bull-put-health - Quick audit health report (entry ratio/state)"
	@echo "apply-option-schema - Apply option spread trades schema migration (v2)"
	@echo ""
	@echo "Feature Computation:"
	@echo "compute-features     - Compute features for specific resolution (e.g., make compute-features res=1m days=3)"
	@echo "compute-features-all - Compute features for all resolutions (last 3 days)"
	@echo ""
	@echo "Market Cost Profile (Phase 8):"
	@echo "cost-day    - Build market cost profile for yesterday"
	@echo ""
	@echo "Backup & Restore:"
	@echo "backup-pg        - Run a manual backup of the PostgreSQL database (old simple backup)"
	@echo "backup-pg-full   - Run full structured PostgreSQL backup (schema + data separated)"
	@echo "backup-ch        - Run a manual backup of the ClickHouse database"
	@echo "backup-ch-daily  - Run daily ClickHouse backup (to backup/clickhouse/YYYY-MM-DD/)"
	@echo "backup-ch-full   - Create a timestamped ClickHouse snapshot via clickhouse-backup"
	@echo "backup-pg-schema - Dump PostgreSQL schema only"
	@echo "backup-ch-schema - Export ClickHouse schema (CREATE statements)"
	@echo ""
	@echo "AI Dynamic Strategy (Coach/HPO/Replay):"
	@echo "premarket-coach - Run health checks + coach report (+ optional apply)"
	@echo "coach-report    - Generate coach Markdown report"
	@echo "coach-apply     - Apply coach suggestions (respect autosettings)"
	@echo "global-gate     - Apply global volatility gate (use --force to bypass approvals)"
	@echo "replay-day      - Run daily replay for strategy IDs"
	@echo "nightly-hpo     - Generate nightly HPO suggestions (optionally real backtests)"
	@echo "setup-backup-automation - Setup automated daily ClickHouse backups (macOS launchd)"
	@echo "setup-daily-automation  - Setup automated daily recon-all at 4:01 PM (includes backup)"
	@echo "setup-midnight-automation - Setup automated daily recon-all at 00:01 AM (includes backup)"
	@echo ""
	@echo "Backfill Utilities:"
	@echo "backfill-5d  - Use tool/load_hist_multi.py to backfill BAR size (default: 1 day) for 5 days using symbols from config.universe"

up:
	@echo "Starting infrastructure..."
	cd infra && docker compose up -d
	@echo "Waiting for services to be ready..."
	sleep 10

down:
	@echo "Stopping infrastructure..."
	cd infra && docker compose down

verify:
	@echo "Verifying infrastructure health..."
	@./scripts/check_env.sh

# AI Dynamic Strategy targets
premarket-coach:
	@echo "Running premarket coach flow..."
	@python3 tools/premarket_coach_flow.py --report-md runs/ai_trader/$$(date +%Y-%m-%d)/coach_report.md

coach-report:
	@echo "Generating coach report..."
	@python3 tools/strategy_coach.py --report-md runs/ai_trader/$$(date +%Y-%m-%d)/coach_report.md

coach-apply:
	@echo "Applying coach suggestions (respect autosettings)..."
	@python3 tools/strategy_coach.py --apply

global-gate:
	@echo "Applying global volatility gate (dry-run unless you pass FORCE=1)..."
	@if [ "$(FORCE)" = "1" ]; then \
	  python3 tools/global_volatility_gate.py --apply --force; \
	else \
	  python3 tools/global_volatility_gate.py; \
	fi

replay-day:
	@echo "Running daily replay..."
	@if [ -z "$(STRATS)" ]; then \
	  echo "Please set STRATS='ID1 ID2'"; exit 1; \
	fi
	@python3 tools/daily_replay.py --strategies $(STRATS) --out-md runs/ai_trader/$$(date +%Y-%m-%d)/replay.md

nightly-hpo:
	@echo "Running nightly HPO suggestions..."
	@if [ -n "$(REAL)" ]; then \
	  python3 tools/nightly_hpo.py --real-backtest; \
	else \
	  python3 tools/nightly_hpo.py; \
	fi

hpo-apply:
	@echo "Applying HPO suggestions (respect autosettings; use FORCE=1 to bypass)"
	@if [ -n "$(FORCE)" ]; then \
	  python3 tools/apply_hpo_suggestions.py --force; \
	else \
	  python3 tools/apply_hpo_suggestions.py; \
	fi

config-rollback:
	@echo "Usage: make config-rollback SYM=TSLA [FILE=path]"
	@if [ -z "$(SYM)" ]; then \
	  echo "Please set SYM=..."; exit 1; \
	fi
	@if [ -n "$(FILE)" ]; then \
	  python3 tools/config_rollback.py --restore $(SYM) --file $(FILE); \
	else \
	  python3 tools/config_rollback.py --restore $(SYM); \
	fi

circuit-breaker:
	@echo "Running circuit breaker (dry-run; pass APPLY=1 to apply and optionally HEDGE_EXECUTE=1)"
	@if [ -n "$(APPLY)" ]; then \
	  python3 tools/circuit_breaker.py --apply; \
	else \
	  python3 tools/circuit_breaker.py; \
	fi

compare-ensemble:
	@echo "Backtesting XGB-only, LSTM-only, Fusion (requires features_1m_with_oof)"
	@if [ -z "$(SYMS)" ]; then \
	  echo "Please set SYMS='TSLA,AAPL' START='2025-07-01 09:30:00' END='2025-08-31 16:00:00'"; exit 1; \
	fi
	@python3 tools/backtest_ensemble_compare.py --symbols $(SYMS) --start "$(START)" --end "$(END)" --features-table trading.features_1m_with_oof

verify-oof:
	@echo "Verifying features_1m_with_oof view and OOF coverage (set SYMS/START/END to compute coverage)"
	@if [ -n "$(SYMS)" ] && [ -n "$(START)" ] && [ -n "$(END)" ]; then \
	  python3 tools/verify_oof_view.py --symbols $(SYMS) --start "$(START)" --end "$(END)"; \
	else \
	  python3 tools/verify_oof_view.py; \
	fi

setup-oof-view:
	@echo "Creating/refreshing ClickHouse OOF tables and view"
	@python3 tools/verify_oof_view.py --create

nightly-oof:
	@echo "Running nightly OOF flow (policy-driven)"
	@if [ -z "$(SYMS)" ] || [ -z "$(START)" ] || [ -z "$(END)" ]; then \
	  echo "Usage: make nightly-oof SYMS='TSLA,AAPL' START='2025-10-01 09:30:00' END='2025-10-29 16:00:00' APPLY=1"; exit 1; \
	fi
	@python3 tools/nightly_oof_flow.py --symbols $(SYMS) --start "$(START)" --end "$(END)" $${APPLY:+--apply}

apply-ensemble:
	@python3 tools/apply_ensemble_policy.py --policy config/ensemble_policies.json $${SYMS:+--symbols $(SYMS)} $${FORCE:+--force}

apply-gating:
	@python3 tools/apply_gating_policy.py --policy config/gating_policies.json $${SYMS:+--symbols $(SYMS)} $${FORCE:+--force}

logs:
	cd infra && docker compose logs -f

check-env:
	@./scripts/check_env.sh

test: test-startup-smoke
	@echo "Running tests with $(PYTHON)..."
	$(PYTEST) tests/ -v

test-startup-smoke:
	@echo "Running run_system startup smoke..."
	RUN_SYSTEM_SMOKE_CHECK=1 TRADING_MODE=paper DISABLE_TICK_PUBLISHER=1 DISABLE_SFS_1S=1 $(PYTHON) run_system.py
	$(PYTEST) tests/integration/test_run_system_startup_smoke.py -q
	$(PYTEST) tests/integration/test_strategy_engine_real_dependency_dry_wire.py -q

# Backup & Restore
backup-pg:
	@echo "Starting PostgreSQL backup..."
	@chmod +x scripts/backup/backup_postgres.sh
	@scripts/backup/backup_postgres.sh

backup-pg-full:
	@echo "Starting PostgreSQL full structured backup..."
	@chmod +x scripts/backup/postgres_full_backup.sh
	@scripts/backup/postgres_full_backup.sh

backup-ch:
	@echo "Starting ClickHouse backup..."
	@chmod +x scripts/backup/backup_clickhouse.sh
	@scripts/backup/backup_clickhouse.sh

backup-ch-daily:
	@echo "Starting ClickHouse daily backup..."
	@chmod +x scripts/backup/clickhouse_daily_backup.sh
	@scripts/backup/clickhouse_daily_backup.sh

setup-backup-automation:
	@echo "Setting up automated ClickHouse backups..."
	@chmod +x scripts/backup/setup_backup_automation.sh
	@scripts/backup/setup_backup_automation.sh

setup-daily-automation:
	@echo "Setting up daily automation (recon-all at 4:01 PM)..."
	@chmod +x scripts/setup_daily_automation.sh
	@scripts/setup_daily_automation.sh

setup-midnight-automation:
	@echo "Setting up midnight automation (recon-all at 00:01 AM)..."
	@chmod +x scripts/setup_midnight_recon.sh
	@scripts/setup_midnight_recon.sh

backup-pg-schema:
	@echo "Starting PostgreSQL schema backup..."
	@chmod +x scripts/backup/backup_postgres_schema.sh
	@scripts/backup/backup_postgres_schema.sh

backup-ch-schema:
	@echo "Starting ClickHouse schema backup..."
	@chmod +x scripts/backup/backup_clickhouse_schema.sh
	@scripts/backup/backup_clickhouse_schema.sh

CLICKHOUSE_BACKUP_BIN ?= /opt/homebrew/bin/clickhouse-backup
CLICKHOUSE_BACKUP_CONFIG ?= config/clickhouse_backup.yml

backup-ch-full:
	@name=$${name:-full_backup_$$(date +%Y%m%d_%H%M%S)}; \
	 echo "Creating ClickHouse full backup '$$name'..."; \
	 $(CLICKHOUSE_BACKUP_BIN) --config=$(CLICKHOUSE_BACKUP_CONFIG) create $$name

# Phase 5: Data Reconciliation Commands

recon1m:
	@echo "Reconciling 1m bars (last 1 days)..."
	@python3 -m services.reconciliation.offline_data_reconciler --res 1m --days 1

recon5m:
	@echo "Reconciling 5m bars (last 1 days)..."
	@python3 -m services.reconciliation.offline_data_reconciler --res 5m --days 1

recon10m:
	@echo "Reconciling 10m bars (last 1 days)..."
	@python3 -m services.reconciliation.offline_data_reconciler --res 10m --days 1

recon15m:
	@echo "Reconciling 15m bars (last 1 days)..."
	@python3 -m services.reconciliation.offline_data_reconciler --res 15m --days 1

recon30m:
	@echo "Reconciling 30m bars (last 1 days)..."
	@python3 -m services.reconciliation.offline_data_reconciler --res 30m --days 1

recon2h:
	@echo "Reconciling 2h bars (last 1 days)..."
	@python3 -m services.reconciliation.offline_data_reconciler --res 2h --days 1

recon3h:
	@echo "Reconciling 3h bars (last 1 days)..."
	@python3 -m services.reconciliation.offline_data_reconciler --res 3h --days 1

recon4h:
	@echo "Reconciling 4h bars (last 1 days)..."
	@python3 -m services.reconciliation.offline_data_reconciler --res 4h --days 1

recon1d:
	@echo "Reconciling 1d bars (last 365 days)..."
	@python3 -m services.reconciliation.offline_data_reconciler --res 1d --days 1

recon-all:
	@echo "Running all reconciliation tasks (will continue and still run backups even if some steps fail)..."; \
	failures=0; \
	run_make() { target="$$1"; shift; echo "  → $$target"; if ! $(MAKE) "$$target" "$$@"; then echo "    WARN: $$target failed"; failures=1; fi; }; \
	run_cmd() { name="$$1"; shift; echo "  → $$name"; if ! "$$@"; then echo "    WARN: $$name failed"; failures=1; fi; }; \
	run_make recon1m; \
	run_make recon5m; \
	run_make recon10m; \
	run_make recon15m; \
	run_make recon30m; \
	run_make recon2h; \
	run_make recon3h; \
	run_make recon4h; \
	run_make recon1d; \
	run_cmd "Daily PnL/Sharpe report" venv/bin/python scripts/metrics/pnl_report.py --sink ch --days 252; \
	run_cmd "AI Trader health snapshot" venv/bin/python scripts/ai_trader/health_snapshot.py; \
	run_make check-positions; \
	run_make check-bull-put-health; \
	run_make nightly-data-refresh; \
	run_make compute-features-all; \
	echo ""; \
	echo "Creating daily backups..."; \
	run_make backup-pg-full; \
	run_make backup-ch-daily; \
	echo ""; \
	if [ $$failures -ne 0 ]; then \
	  echo "⚠ recon-all finished with errors (see logs above). Backups were still attempted."; \
	  exit 1; \
	else \
	  echo "✅ All reconciliation, PnL report, feature computation, and backups completed!"; \
	fi

check-positions:
	@echo "Reconciling IBKR positions vs DB records..."
	@venv/bin/python scripts/check_positions_consistency.py --symbol QQQ

check-bull-put-health:
	@echo "Checking bull put spread audit metrics..."
	@venv/bin/python scripts/check_bull_put_health.py

apply-option-schema:
	@echo "Applying option_spread_trades schema migration..."
	@venv/bin/python scripts/apply_option_spread_schema.py

# Feature Computation (after backfill)
compute-features:
	@echo "Computing features for resolution: $(res) (last $(days) days)..."
	@python3 scripts/compute_features.py --res $(res) --days $(days)

compute-features-all:
	@echo "Computing features for all resolutions (last 3 days)..."
	@python3 scripts/compute_features.py --res all --days 3

nightly-data-refresh:
	@echo "Refreshing QQQ/SPY/VIX 1m history, features, and active snapshot..."
	@venv/bin/python tool/load_hist_multi.py --symbols QQQ SPY VIX --days 30 --bar-size "1 min" --fill-mode backfill --client-id 931
	@venv/bin/python tool/compute_features.py --bar-size 1m --symbols QQQ SPY VIX --days 45
	@START=$$(venv/bin/python - <<'PY'
	from datetime import datetime, timedelta
	from zoneinfo import ZoneInfo
	now = datetime.now(ZoneInfo("America/New_York"))
	start = (now - timedelta(days=45)).replace(hour=9, minute=30, second=0, microsecond=0)
	print(start.strftime("%Y-%m-%d %H:%M:%S"))
	PY)
	@END=$$(venv/bin/python - <<'PY'
	from datetime import datetime
	from zoneinfo import ZoneInfo
	now = datetime.now(ZoneInfo("America/New_York"))
	end = now.replace(hour=16, minute=0, second=0, microsecond=0)
	print(end.strftime("%Y-%m-%d %H:%M:%S"))
	PY)
	@venv/bin/python tools/sync_features_active.py --symbols QQQ,SPY,VIX --start "$${START}" --end "$${END}" --chunk-days 3
	@echo "Nightly data refresh completed."

# Phase 8: Market Cost Profile
cost-day:
	@echo "Building market cost profile for yesterday..."
	@python3 -m services.analytics.build_market_cost_profile

# ------------------------------
# Backfill (via tool/load_hist_multi.py)
# ------------------------------
# Defaults (can override: make backfill-5d BAR="1 min" DAYS=5)
BAR ?= 1 day
DAYS ?= 5

backfill-5d:
	@echo "Backfilling $(BAR) for $(DAYS) day(s) from config.universe via tool/load_hist_multi.py..."
	@SYMS="$$(python3 -c 'from libs.config import config; print(" ".join(config.universe or []))')"; \
	 if [ -z "$$SYMS" ]; then \
	   echo "No symbols found in config.universe"; \
	   exit 1; \
	 fi; \
	 echo "Symbols: $$SYMS"; \
	 python3 tool/load_hist_multi.py --symbols $$SYMS --days $(DAYS) --bar-size "$(BAR)" --fill-mode backfill
