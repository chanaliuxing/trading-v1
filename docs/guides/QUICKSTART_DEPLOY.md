# Quick Start Deployment Guide

**Purpose**: Step-by-step instructions to deploy the trading system from current state to production.

**Current State**: Code complete (97.5% tests passing), infrastructure ready to deploy.

---

## Prerequisites

- Docker & Docker Compose installed
- PostgreSQL, ClickHouse, Redis, Kafka running (verified ✅)
- Python 3.13 with virtual environment
- IB Gateway/TWS account (paper or live)

---

## Phase 1: Immediate Setup (30 minutes)

### Step 1: Environment Configuration

```bash
# Copy environment template
cp .env.example .env

# Edit with your credentials
vim .env
```

**Required Variables**:
```bash
# Trading Mode
TRADING_MODE=shadow

# IB Credentials
IB_USERNAME=your_ib_username
IB_PASSWORD=your_ib_password
IB_TRADING_MODE=paper
IB_GATEWAY_PORT=4002

# Database (already set)
POSTGRES_PASSWORD=postgres
POSTGRES_HOST=localhost
POSTGRES_PORT=5432

# Optional: Monitoring
SLACK_WEBHOOK_URL=your_slack_webhook
PAGERDUTY_SERVICE_KEY=your_pagerduty_key
```

### Step 2: Verify Current System

```bash
# Activate virtual environment
source venv/bin/activate

# Run smoke test
python3 scripts/smoke_test.py

# Expected: 5/5 checks passing
```

### Step 3: Run Core Tests

```bash
# Run all tests
export PYTHONPATH=.
pytest tests/ -v --tb=short

# Expected: 234/240 passing (97.5%)
```

### Step 4: Initialize Shadow Mode

```bash
# Set shadow mode
export TRADING_MODE=shadow
export SHADOW_SYMBOLS=AAPL,MSFT,NVDA

# Initialize deployment stage
psql -h localhost -U postgres -d test << 'EOF'
INSERT INTO deployment_stages (stage, started_at, active, capital_pct, symbols)
VALUES ('shadow', NOW(), TRUE, 0, 5);
EOF
```

---

## Phase 2: HA/DR Deployment (1-2 hours)

### Option A: Deploy HA Infrastructure (Recommended for Production)

```bash
# Stop single-node services (if running in Docker)
docker compose -f infra/compose.yml down

# Start HA infrastructure
docker compose -f infra/compose.ha.yml up -d

# Wait for services to start (2-3 minutes)
sleep 180

# Verify all services healthy
docker compose -f infra/compose.ha.yml ps

# Expected: All services "running" with "healthy" status
```

**HA Services Started**:
- PostgreSQL: 1 primary + 2 replicas (ports 5432, 5433, 5434)
- ClickHouse: 3 nodes (ports 8123, 8124, 8125)
- Kafka: 3 brokers (ports 9092, 9094, 9096)
- Redis: Sentinel with 3 sentinels (ports 6379, 6380, 26379-26381)

### Option B: Keep Current Single-Node (For Testing)

```bash
# Current setup already working
# Skip to Phase 3
```

### Verify HA Deployment

```bash
# Run smoke test against HA infrastructure
python3 scripts/smoke_test.py

# Check replication status
psql -h localhost -p 5432 -U postgres -d test -c "SELECT * FROM pg_stat_replication;"

# Check ClickHouse cluster
curl "http://localhost:8123/?query=SELECT%20*%20FROM%20system.clusters"

# Check Kafka brokers
docker exec trading_kafka_1 kafka-topics --bootstrap-server localhost:9092 --list
```

---

## Phase 3: Vault Deployment (30 minutes)

### Step 1: Start Vault

```bash
# Start Vault service
docker compose -f infra/vault/docker-compose.vault.yml up -d

# Wait for Vault to initialize
sleep 10

# Check Vault logs
docker logs trading_vault_init

# Copy the RoleID and SecretID from output
```

### Step 2: Configure Vault Credentials

```bash
# Add to .env
export VAULT_ADDR=http://localhost:8200
export VAULT_ROLE_ID=<role_id_from_logs>
export VAULT_SECRET_ID=<secret_id_from_logs>

# Test Vault access
python3 << 'EOF'
import asyncio
from libs.vault_client import get_vault_client

async def test():
    vault = get_vault_client()
    await vault.authenticate()
    config = await vault.get_postgres_config()
    print(f"Vault working! Got config: {config}")

asyncio.run(test())
EOF
```

### Step 3: Migrate Secrets (Optional)

```bash
# Update service startup scripts to use Vault
# For now, keep using .env for simplicity
```

---

## Phase 4: Monitoring Setup (30 minutes)

### Step 1: Start Prometheus/Alertmanager

```bash
# Already configured in compose.ha.yml
# If using single-node, add monitoring:

docker compose -f infra/compose.yml up -d prometheus grafana

# Verify Prometheus
curl http://localhost:9090/-/healthy

# Verify Grafana
curl http://localhost:3000/api/health
```

### Step 2: Configure Alerting

```bash
# Edit alertmanager config with your webhook URLs
vim infra/prometheus/alertmanager.yml

# Replace placeholders:
# - SLACK_WEBHOOK_URL
# - PAGERDUTY_SERVICE_KEY

# Restart alertmanager
docker restart trading_alertmanager
```

### Step 3: Verify Alerts

```bash
# Check alert rules loaded
curl http://localhost:9090/api/v1/rules | python3 -m json.tool

# Check alertmanager status
curl http://localhost:9093/api/v2/status | python3 -m json.tool
```

---

## Phase 5: Shadow Mode Testing (2 weeks)

### Day 1: Start Shadow Mode

```bash
# Ensure shadow mode enabled
export TRADING_MODE=shadow
export SHADOW_SYMBOLS=AAPL,MSFT,NVDA,GOOGL,TSLA

# Start shadow trading service (example)
# (You'll need to create the main service entry point)

# For now, test the trading mode manager:
python3 << 'EOF'
import asyncio
from services.execution.trading_mode import TradingModeManager, TradingMode
import asyncpg

async def test():
    db = await asyncpg.create_pool(
        host="localhost",
        user="postgres",
        password="postgres",
        database="test"
    )

    manager = TradingModeManager(
        mode=TradingMode.SHADOW,
        db_pool=db,
        shadow_symbols=["AAPL", "MSFT", "NVDA"]
    )

    # Test decision
    decision = {
        "decision_id": "test_001",
        "strategy_id": "test_strategy",
        "symbol": "AAPL",
        "target_qty": 100.0,
        "order_style": {"kind": "LMT", "limit_px": 150.0},
        "extras": {"ref_px": 150.0}
    }

    result = await manager.process_decision(decision)
    print(f"Shadow decision: {result}")

    # Check shadow orders
    rows = await db.fetch("SELECT * FROM shadow_orders ORDER BY created_at DESC LIMIT 5")
    print(f"\nShadow orders: {len(rows)}")
    for row in rows:
        print(f"  {row['symbol']} {row['side']} {row['qty']} @ {row['limit_px']}")

    await db.close()

asyncio.run(test())
EOF
```

### Daily: Monitor Shadow Reports

```bash
# Generate daily shadow report
python3 << 'EOF'
import asyncio
from services.execution.trading_mode import TradingModeManager, TradingMode
from datetime import date
import asyncpg

async def report():
    db = await asyncpg.create_pool(
        host="localhost",
        user="postgres",
        password="postgres",
        database="test"
    )

    manager = TradingModeManager(mode=TradingMode.SHADOW, db_pool=db)

    report = await manager.get_shadow_report(date.today().isoformat())
    print(f"Shadow Report for {report['date']}:")
    print(f"Total Decisions: {report['total_decisions']}")
    print(f"\nBy Symbol:")
    for sym in report['symbols']:
        print(f"  {sym['symbol']}: {sym['decisions']} decisions, {sym['total_qty']} shares")

    await db.close()

asyncio.run(report())
EOF
```

---

## Phase 6: EOD Reconciliation (1 day setup)

### Step 1: Configure IB FlexQuery

1. Log in to IB Account Management
2. Navigate to Reports → Flex Queries
3. Create new Activity Flex Query:
   - Include: Trades, Positions, Cash Transactions
   - Date: T (trade date)
   - Format: XML
4. Note the Query ID
5. Generate Token in Settings → FlexWeb Service

### Step 2: Configure EOD Job

```bash
# Add to .env
export IB_FLEX_TOKEN=your_flex_token
export IB_FLEX_QUERY_ID=your_query_id

# Test EOD reconciliation
python3 << 'EOF'
import asyncio
from services.compliance.eod_reconciliation import EODReconciler
from datetime import date, timedelta
import asyncpg
from ib_insync import IB

async def test():
    db = await asyncpg.create_pool(
        host="localhost",
        user="postgres",
        password="postgres",
        database="test"
    )

    ib = IB()
    # await ib.connectAsync('localhost', 4002, clientId=1)  # Connect to IB

    reconciler = EODReconciler(
        db_pool=db,
        ib_client=ib,
        flex_token="your_token",
        flex_query_id="your_query_id"
    )

    # Test reconciliation for yesterday
    yesterday = date.today() - timedelta(days=1)
    # result = await reconciler.reconcile_eod(yesterday)
    # print(f"Reconciliation: {'PASSED' if result.passed else 'FAILED'}")
    # print(f"Issues: {result.total_issues}")

    await db.close()

asyncio.run(test())
EOF

# Schedule daily job (example cron)
# 30 16 * * 1-5 cd /workspace/Documents/trading && ./venv/bin/python3 -c "from services.compliance.eod_reconciliation import run_daily_reconciliation; import asyncio; asyncio.run(run_daily_reconciliation())"
```

---

## Phase 7: GameDay Testing (1 day)

### Run Full GameDay Suite

```bash
# Requires Docker environment
# Run each scenario manually:

# Scenario 1: Broker Disconnect
# 1. Disconnect IB Gateway
# 2. Wait 5 minutes
# 3. Reconnect
# 4. Verify: Orders queued, no data loss

# Scenario 2: PostgreSQL Failover
docker stop trading_postgres_primary
# Verify: Replica promoted <30s, queries continue working
docker start trading_postgres_primary

# Scenario 3: Kafka Broker Failure
docker stop trading_kafka_2
# Verify: Producer continues, consumer rebalances
docker start trading_kafka_2

# Document results in GAMEDAY_RESULTS.md
```

---

## Phase 8: Canary Deployment (Weeks 6-10)

### Week 6: Canary (5% capital)

```bash
# Update deployment stage
psql -h localhost -U postgres -d test << 'EOF'
UPDATE deployment_stages SET active = FALSE WHERE active = TRUE;
INSERT INTO deployment_stages (stage, started_at, active, capital_pct, symbols)
VALUES ('canary', NOW(), TRUE, 5, 2);
EOF

# Set to paper trading
export TRADING_MODE=paper

# Deploy with limited capital
export CANARY_CAPITAL_PCT=5
export CANARY_SYMBOLS=AAPL,MSFT

# Run daily rollout evaluation
python3 services/deployment/canary_rollout.py
```

### Week 7-10: Gradual Ramp

```bash
# Each week, run rollout evaluation
# Auto-promotes if metrics pass:
# - Error rate < threshold
# - Latency < p95 threshold
# - Fill rate > 95%
# - Uptime > 99%

# Manual promotion if needed:
psql -h localhost -U postgres -d test << 'EOF'
UPDATE deployment_stages SET active = FALSE WHERE active = TRUE;
INSERT INTO deployment_stages (stage, started_at, active, capital_pct, symbols)
VALUES ('ramp25', NOW(), TRUE, 25, 10);
EOF
```

---

## Troubleshooting

### Issue: Smoke Test Fails

```bash
# Check services status
psql -h localhost -U postgres -d test -c "SELECT 1;"
curl http://localhost:8123/ping
redis-cli ping
kafka-topics --bootstrap-server localhost:9092 --list

# Restart services if needed
# For single-node:
brew services restart postgresql@16
brew services restart redis
# etc.
```

### Issue: Tests Failing

```bash
# Run specific test with verbose output
export PYTHONPATH=.
pytest tests/test_reservation.py -v -s

# Check logs
tail -f logs/*.log
```

### Issue: HA Services Not Starting

```bash
# Check Docker logs
docker compose -f infra/compose.ha.yml logs postgres-primary
docker compose -f infra/compose.ha.yml logs clickhouse-01

# Verify network
docker network ls | grep trading

# Restart specific service
docker compose -f infra/compose.ha.yml restart postgres-primary
```

---

## Monitoring & Dashboards

### Access Points

- **Prometheus**: http://localhost:9090
- **Grafana**: http://localhost:3000 (admin/admin)
- **Alertmanager**: http://localhost:9093
- **ClickHouse**: http://localhost:8123
- **Kafka Schema Registry**: http://localhost:8081

### Key Metrics to Watch

```promql
# Error rate
rate(exec_order_errors_total[5m])

# Latency
histogram_quantile(0.95, rate(exec_latency_ms_bucket[5m]))

# Capital utilization
(pf_capital_reserved / pf_capital_total) * 100

# Reg-Lite blocks
rate(risk_reglite_blocks_total[5m])
```

---

## Daily Operations Checklist

### Morning (Market Open)
- [ ] Check system health: `python3 scripts/smoke_test.py`
- [ ] Review overnight alerts
- [ ] Check shadow/paper trading reports
- [ ] Verify IB Gateway connected

### During Market Hours
- [ ] Monitor Grafana dashboard
- [ ] Watch Slack alerts
- [ ] Check position reports every hour

### Evening (Market Close)
- [ ] Run EOD reconciliation
- [ ] Review day's performance
- [ ] Check for any anomalies
- [ ] Generate daily report

### Weekly
- [ ] Review canary rollout metrics
- [ ] Update deployment stage if passing
- [ ] Review GameDay scenarios
- [ ] Backup database

---

## Emergency Procedures

### Kill Switch (Emergency Stop)

```bash
# Stop all trading immediately
psql -h localhost -U postgres -d test << 'EOF'
UPDATE capital_config SET allocation_pct = 0;
UPDATE symbol_config SET enabled = FALSE;
EOF

# Cancel all open orders (via IB API)
# Flatten all positions (manual via TWS)
```

### Rollback Deployment

```bash
# Revert to previous stage
psql -h localhost -U postgres -d test << 'EOF'
UPDATE deployment_stages SET active = FALSE WHERE active = TRUE;
INSERT INTO deployment_stages (stage, started_at, active, capital_pct, symbols)
VALUES ('shadow', NOW(), TRUE, 0, 5);
EOF

export TRADING_MODE=shadow
```

---

## Summary

**Quick Start Path**:
1. Configure .env (5 min)
2. Run smoke test (1 min)
3. Start shadow mode (10 min)
4. Monitor for 2 weeks
5. Switch to paper trading (Week 3)
6. Deploy canary (Week 6)
7. Ramp to full (Week 6-10)

**Minimum Before Production**:
- ✅ 2 weeks shadow mode
- ✅ 1 week paper trading validation
- ✅ EOD reconciliation passing
- ✅ GameDay scenarios passing
- ✅ Monitoring/alerting active

**Total Time to Production**: 10 weeks conservative, 6 weeks aggressive

---

**Last Updated**: 2025-10-04
**Status**: Ready to Deploy
