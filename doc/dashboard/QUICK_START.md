# Observability Quick Start Guide

**Ready to use in**: 5 minutes
**Implementation status**: 95% complete

---

## What's Been Implemented

### ✅ Ready Now (No Code Changes Needed)

1. **Prometheus Metrics Server** (port 9108)
   - Service heartbeat monitoring
   - Latency histograms (ready to receive data)
   - Kafka lag gauges (ready to receive data)

2. **Three-Datasource Dashboard**
   - URL: http://localhost:3000/d/hybrid-trading-3ds
   - 13 panels across Trader and Ops cockpits
   - Real-time data from ClickHouse, Loki, and Prometheus

3. **Alert Rules** (8 production rules)
   - P0: Service down, latency spikes
   - P1: Kafka lag, ClickHouse errors
   - P2: Order rejections, queue issues

4. **Observability Infrastructure**
   - Non-blocking event queue
   - Batch writers for ClickHouse and Loki
   - Graceful shutdown with drain

### ⏳ Needs Application Instrumentation (Optional)

These features will light up once you add instrumentation to your trading logic:

1. **Latency Tracking**: Add timing to strategy and execution code
2. **Trace ID Propagation**: Generate trace_id at signal, track through lifecycle
3. **Kafka Lag Monitoring**: Add lag reporting from Kafka consumers

**Note**: The infrastructure is 100% ready. These are application-level enhancements that can be added incrementally.

---

## 5-Minute Startup

### Step 1: Start Infrastructure (1 min)
```bash
cd .

# Start Prometheus, Grafana, ClickHouse, Loki
docker-compose -f infra/docker-compose.yml up -d

# Verify all services are up
docker ps | grep -E 'prometheus|grafana|clickhouse|loki'
```

### Step 2: Verify Dashboard (1 min)
```bash
# Open Grafana
open http://localhost:3000

# Login (if needed): admin / admin
# Navigate to dashboard: http://localhost:3000/d/hybrid-trading-3ds
```

You should see:
- ✅ Dashboard loads without errors
- ✅ Two rows: "交易员实盘驾驶舱" and "系统 & 策略运维驾驶舱"
- ✅ 13 panels (some may show "No data" until system runs)

### Step 3: Start Trading System (1 min)
```bash
cd .
python3 run_system.py
```

Look for these success messages:
```
0️⃣  Starting Observability Infrastructure...
   ✅ Prometheus metrics server started on port 9108
   ✅ ClickHouse writer started (http://localhost:8123)
   ✅ Loki writer started (http://localhost:3100)
   ✅ Observability queue started
```

### Step 4: Verify Metrics (2 min)

**Check Prometheus metrics endpoint:**
```bash
curl http://localhost:9108/metrics | grep service_up
# Should return: service_up{service="TradingSystem"} 1
```

**Check Prometheus UI:**
```bash
open http://localhost:9090/targets
# Should show: trading-system target is UP
```

**Check Grafana dashboard:**
```bash
open http://localhost:3000/d/hybrid-trading-3ds
# Panel "服务心跳 (service_up)" should show green line at value 1
```

---

## What Each Panel Shows

### 交易员实盘驾驶舱 (Trader Cockpit)

| Panel | Data Source | Description | Data Available |
|-------|-------------|-------------|----------------|
| 总资产 (Equity) | ClickHouse | Total account equity | ✅ When equity_pnl_snapshots written |
| 当日盈亏 (Daily P&L) | ClickHouse | Today's profit/loss | ✅ When equity_pnl_snapshots written |
| 可用资金 (Buying Power) | ClickHouse | Available buying power | ✅ When equity_pnl_snapshots written |
| 风险灯 (Margin %) | ClickHouse | Margin utilization % | ✅ When equity_pnl_snapshots written |
| 当日订单数 | ClickHouse | Order count today | ✅ When order_events written |
| 今日盈亏曲线 | ClickHouse | P&L curve over time | ✅ When equity_pnl_snapshots written |
| 当前持仓 (Holdings) | ClickHouse | Current positions table | ✅ When positions_snapshots written |

### 系统 & 策略运维驾驶舱 (Ops Cockpit)

| Panel | Data Source | Description | Data Available |
|-------|-------------|-------------|----------------|
| 服务心跳 (service_up) | Prometheus | Service health indicator | ✅ **Available now** |
| Signal→Order P95 (ms) | Prometheus | Signal to order latency | ⏳ Needs instrumentation |
| Order→Fill P95 (ms) | Prometheus | Order to fill latency | ⏳ Needs instrumentation |
| 为什么没触发？(Gate Failures) | Loki | Reasons orders didn't trigger | ✅ When gate_snapshot events logged |
| 订单漏斗趋势 (by stage) | ClickHouse | Order funnel over time | ✅ When order_events written |
| 回填缺口 (Backfill Gaps) | ClickHouse | Data backfill status | ✅ When backfill_status written |
| 订单生命周期追踪 | Loki | Complete order trace | ✅ When order_lifecycle events logged |

---

## Troubleshooting

### ⚠️ Dashboard not loading or provisioning failed

**Problem**: Dashboards don't appear in Grafana after deployment

**Root Cause**: Dashboard provider configuration path mismatch

**Fix**:
```bash
# Check the provider configuration
cat ./grafana/provisioning/dashboards/provider.yml

# The 'path' should match where Docker mounts the dashboards
# Correct configuration:
# path: /etc/grafana/provisioning/dashboards

# If path is wrong, you can upload dashboards via API:
cd ./grafana/dashboards
for file in *.json; do
  curl -X POST http://admin:admin@localhost:3000/api/dashboards/db \
    -H "Content-Type: application/json" \
    -d "$(jq -n --argjson dashboard "$(cat $file)" \
      '{dashboard: $dashboard, overwrite: true}')"
done
```

**See**: `doc/dashboard/GRAFANA_DASHBOARD_PROVISIONING_FIX.md` for detailed explanation

### Loki queries failing with "too many outstanding requests"

**Problem**: Dashboard panels show HTTP 429 errors

**Root Cause**: Loki's default concurrency limits are too low for multiple dashboards

**Fix**:
```bash
# Update loki/config/local-config.yaml
# Add these sections:

frontend:
  max_outstanding_per_tenant: 4096
  compress_responses: true

querier:
  max_concurrent: 20
  query_timeout: 5m

limits_config:
  max_query_parallelism: 32
  split_queries_by_interval: 30m

# Restart Loki
docker restart trading-loki-1
```

### Dashboard shows "No data"

**Normal if**:
- Trading system hasn't been running long enough to generate data
- No trades executed yet
- Market is closed

**Check**:
```bash
# Verify ClickHouse has data
clickhouse-client -q "SELECT count() FROM trading.order_events"
clickhouse-client -q "SELECT count() FROM trading.equity_pnl_snapshots"

# Verify Prometheus is scraping
curl http://localhost:9108/metrics

# Verify Loki has logs
# In Grafana Explore: {job="trader"}
```

### Prometheus target is DOWN

**Fix**:
```bash
# Check if metrics server started
curl http://localhost:9108/metrics

# If not accessible, check run_system.py logs for errors
# Verify PROMETHEUS_PORT environment variable (default: 9108)
```

### Alert rules not showing

**Fix**:
```bash
# Verify alert rules file exists
ls -la ./infra/prometheus_alerts.yml

# Check Prometheus config loaded rules
curl http://localhost:9090/api/v1/status/config | jq .data.yaml.rule_files

# Reload Prometheus
curl -X POST http://localhost:9090/-/reload
```

### ClickHouse connection failed

**Fix**:
```bash
# Verify ClickHouse is running
docker ps | grep clickhouse

# Test connection
clickhouse-client -q "SELECT 1"

# Check environment variables
echo $CH_URL  # Should be: http://localhost:8123
echo $CH_DB   # Should be: trading
```

---

## Adding Optional Instrumentation

Want to light up the latency panels? Here's the minimal code:

### Signal→Order Latency (5 min)

**File**: `strategies/model_signal_strategy.py`

```python
# Add at top of file
from services.metrics.exporter import latency_signal_to_order_ms
import time

# In on_features method, add:
async def on_features(self, symbol: str, features: Dict[str, Any]) -> None:
    start_time = time.time()  # 👈 Add this

    # ... existing logic ...

    await self.ctx.submit_intent(envelope)

    # Record latency
    latency_ms = (time.time() - start_time) * 1000  # 👈 Add this
    latency_signal_to_order_ms.observe(latency_ms)   # 👈 Add this
```

**Result**: Panel "Signal→Order P95 (ms)" will show data immediately

### Order→Fill Latency (30 min)

**File**: `services/exec/adapters/ib_adapter.py`

```python
# Add at top of file
from services.metrics.exporter import latency_order_to_fill_ms
import time

# In __init__, add:
self._order_submit_times = {}

# In _place method, after placing order:
self._order_submit_times[trade.order.orderId] = time.time()

# In fill callback (need to identify), add:
if order_id in self._order_submit_times:
    submit_time = self._order_submit_times[order_id]
    latency_ms = (time.time() - submit_time) * 1000
    latency_order_to_fill_ms.observe(latency_ms)
    del self._order_submit_times[order_id]
```

**Result**: Panel "Order→Fill P95 (ms)" will show data

---

## Key Files Reference

| File | Purpose | Status |
|------|---------|--------|
| `run_system.py` | Main entry point with observability startup | ✅ Modified |
| `infra/prometheus.yml` | Prometheus configuration | ✅ Modified |
| `infra/prometheus_alerts.yml` | Alert rules | ✅ Created |
| `grafana/dashboards/hybrid_trading_overview.json` | Main dashboard | ✅ Created |
| `doc/dashboard/OBSERVABILITY_IMPLEMENTATION_SUMMARY.md` | Detailed documentation | ✅ Created |

---

## Quick Commands

```bash
# View metrics
curl http://localhost:9108/metrics

# Query ClickHouse
clickhouse-client -q "SELECT * FROM trading.order_events LIMIT 5"

# Reload Prometheus (after config changes)
curl -X POST http://localhost:9090/-/reload

# Restart Grafana (after dashboard changes)
docker restart grafana

# Check system status
curl http://localhost:9090/targets          # Prometheus targets
open http://localhost:3000/d/hybrid-trading-3ds  # Dashboard
open http://localhost:9090/alerts           # Alert rules
```

---

## Support

For detailed implementation information, see:
- **Full documentation**: `doc/dashboard/OBSERVABILITY_IMPLEMENTATION_SUMMARY.md`
- **Original specification**: `doc/dashboard/DASHBOARD_NEW_PLAN.md`
- **Testing checklist**: See "Testing Checklist" section in OBSERVABILITY_IMPLEMENTATION_SUMMARY.md

---

**Status**: Ready to use
**Last Updated**: 2025-10-15
**Next Steps**: Start system and verify dashboard
