# Grafana Dashboards Consolidation — Final Plan v3.1

**Objective:** Replace 12+ fragmented dashboards with **4 cohesive, high‑signal Grafana dashboards** that provide full visibility into infrastructure, trading system operations, strategy pipeline, and financial/market status — **using only existing data** (Loki logs, ClickHouse, Prometheus, PostgreSQL). **No code changes**.

---

## 0) Scope, Constraints, Success Criteria

**Scope**

* Consolidate into 4 dashboards:

  1. **Infrastructure & Platform Health**
  2. **Trading System Operations (run_system)**
  3. **Strategy & Decision Pipeline Insight**
  4. **Financial & Market Overview**

**Constraints**

* No application code edits. Only reuse existing metrics/logs/DB tables.
* Prefer panel reuse from current JSONs.

**Success criteria**

* One‑screen health view for all core services with error/warn visibility.
* Single deep‑dive view for `run_system` with live logs + throughput/lag.
* Strategy funnel (signals→cost gate→risk gate→orders→fills) and PnL per strategy.
* Financial dashboard that shows equity/cash, realized/unrealized PnL, positions, and key market context (where data exists).

---

## 1) Phase‑0: Data Reality Check (prevent dead ends)

Create/maintain `doc/dashboard/IMPLEMENTATION_STATUS.md` with ✅/❌ tables.

### 1.1 Prometheus coverage

* Inspect scrape targets:

  * `curl -s http://localhost:9090/api/v1/targets | jq '.data.activeTargets[] | {job: .labels.job, url: .scrapeUrl, health: .health}'`
* Discover metric families actually present:

  * `curl -s 'http://localhost:9090/api/v1/series?match[]=%7B__name__%3D~%22(kafka.*lag|container_.*|latency_.*|errors_total|service_up)%22%7D' | jq '.data | length'`
* Record whether we have: `container_*`, `kafka_*lag*`, `latency_*`, `errors_total`, `service_up`.

### 1.2 Loki labels/structure

* In Grafana Explore (Loki), verify labels exist: `app`, `component`, `service`, `event_name`, `strategy_id`, `symbol`, `level`.
* Sample:

  * `{app="run_system"} | json`
  * `sum by(component) (rate({level="error"}[5m]))`
* Note exact field names/variants.

### 1.3 ClickHouse tables

* Inventory trading schema:

  * `SHOW TABLES FROM trading;`
  * `SELECT database, name FROM system.tables WHERE database='trading' ORDER BY name;`
* Validate availability of expected tables (mark ✅/❌):

  * `equity_pnl_snapshots`, `positions_snapshots`, `pnl_pos_5s`, `pnl_day`, `pnl_fills`/`trade_pnl`, `order_events`, `exec_events`, `gate_snapshots`, `order_funnel_daily`, `backfill_status`, any `bars_*` (1m/1d etc.), optional `market_index_*`.

### 1.4 PostgreSQL metadata

* Verify existence/utility of `strategies`, `strategy_state` (or equivalent) for active/inactive flags and names.
* If absent, plan to derive from logs.

### 1.5 Panel Migration Matrix (reuse what works)

Create `doc/dashboard/PANEL_MATRIX.csv` mapping **existing → new**. Seed rows:

| From Dashboard                                     | Panel/Purpose                    | To Dashboard                     |
| -------------------------------------------------- | -------------------------------- | -------------------------------- |
| phase0_infrastructure.json                         | Service health tiles (log-based) | Infrastructure & Platform Health |
| good.json                                          | Error/Warn counts by component   | Infrastructure & Platform Health |
| run_system_ops.json                                | Live log stream + filters        | Trading System Operations        |
| trading_observability.json                         | Event rate timelines             | Trading System Operations        |
| strategy_monitor.json                              | Strategy list & basic status     | Strategy & Pipeline              |
| aapl_dividend_wave_monitor.json                    | Per-symbol behavior              | Strategy & Pipeline              |
| hybrid_trading_overview.json                       | Portfolio/PnL snapshots          | Financial & Market               |
| trading_trace_and_logs.json, trading_why_not*.json | Debug sequences/traces           | Trading System Operations        |

---

## 2) Dashboard Designs (final)

### 2.1 Infrastructure & Platform Health ⚙️

**Purpose:** See if everything is **up**, detect **errors/warnings**, and assess **performance** per service.

**Sections & Panels**

1. **Global Status Grid**

* Source: Prometheus `up{job=…}` or `service_up{service=…}` if present.
* Fallback rule (Loki): status =

  * 🟢 if `rate({component="$svc"}[5m]) > 0` and `rate({component="$svc", level="error"}[5m]) == 0`
  * 🟡 if error rate > 0
  * 🔴 if **no logs in last 5m** (degraded/offline signal)

2. **Resource Usage** (per container)

* CPU: `rate(container_cpu_usage_seconds_total{container=~"$service"}[5m])`
* Mem: `container_memory_usage_bytes{container=~"$service"}`
* Net/Disk if available; else omit.

3. **DB & Broker Health**

* ClickHouse replicas/queries: `SELECT * FROM system.replicas;`, `SELECT metric,value FROM system.metrics WHERE metric IN ('Query','QueryPreempted','DelayedInserts');`
* Ingestion/backfill: `SELECT * FROM trading.backfill_status ORDER BY updated_at DESC LIMIT 50;`
* Kafka lag (if present): `kafka_consumer_group_lag{group=~"$group"}` heatmap.

4. **Error Hotspots**

* Loki: `sum by(component) (count_over_time({level="error"}[5m]))`
* Heatmap over time + table of latest critical logs.

5. **IBKR Connectivity**

* Loki: `{component="ibkr"} |~ "disconnect|timeout|connection reset"`
* Stat: reconnects/hour via `count_over_time`.

**Variables**: `$service` (postgres|clickhouse|redis|kafka|loki|grafana|ibkr), `$group` (Kafka), `$env`.

---

### 2.2 Trading System Operations (run_system) 🚀

**Purpose:** A single deep‑dive for runtime behavior, throughput, lag, warnings, and errors.

**Sections & Panels**

1. **Live Logs**

* Loki stream: `{app="run_system"} | json`
* Quick filters: `level`, `component`, `strategy_id`, `symbol`, `order_id`.

2. **Operational Stats**

* Signals processed today: `sum(count_over_time({app="run_system", event_name="SignalReceived"}[1d]))`
* Orders sent today: `… event_name="OrderIntentSent" …`
* Errors today: `… level="error" …`

3. **Throughput Timeline**

* Events/min by `event_name` (SignalReceived, OrderIntentSent, OrderFilled).
* CH overlay (if desired): `SELECT toStartOfInterval(ts, INTERVAL 1m) t, count() FROM trading.exec_events GROUP BY t ORDER BY t;`

4. **Latency & Backlog**

* Prometheus `latency_*` histograms if present; else **derived** latency from log pairs (Signal→Order) via Loki `pattern`/`line_format` and binning.
* Kafka consumer lag for `run_system` groups if available.

5. **Error Diagnostics**

* Table of last N ERROR logs (with parsed `error_type`).
* Bar chart of error categories by `error_type` or substring grouping.

**Variables**: `$component` (System|PriceCache|StrategyEngine|market_data|… ), `$strategy`, `$symbol`, `$group`.

---

### 2.3 Strategy & Decision Pipeline Insight 📈

**Purpose:** Which strategies are active? Where do decisions get stuck? Who performs best/worst?

**Sections & Panels**

1. **Strategy Inventory**

* If PG `strategies`/`strategy_state` exists: name, active flag, symbol, notes.
* Else derive from logs (latest `activated`/`disabled` event per `strategy_id`).
* Join CH for PnL today/total.

2. **Decision Funnel (day or range)**

* Prefer CH tables:

  * `order_funnel_daily` → counts at each stage.
  * `gate_snapshots` → pass/fail per gate + latency_ms.
* Fallback: Loki counts by `event_name` (cost_gate_pass, risk_check_pass, order_skipped, order_sent, fill).

3. **Gate Analysis**

* Pass/Fail ratio per gate over time; top rejection reasons (from CH or parsed logs).

4. **Performance Leaderboard**

* PnL per strategy (time series + table): `pnl_day`, `pnl_fills`/`trade_pnl`.
* Win rate, avg win/loss computed via CH aggregation.

**Variables**: `$strategy`, `$symbol`, `$gate`.

---

### 2.4 Financial & Market Overview 💰

**Purpose:** Equity/cash, realized/unrealized PnL, positions, market context.

**Sections & Panels**

1. **Headline KPIs**

* Equity, Cash, BP: `equity_pnl_snapshots` (latest row per account).
* Realized PnL (today): `pnl_day` sum.
* Unrealized PnL (current): `pnl_pos_5s` latest if available; else mark **Data Pending**.

2. **Equity Curve**

* `SELECT ts, equity FROM trading.equity_pnl_snapshots ORDER BY ts;`

3. **Daily PnL**

* `SELECT day, sum(realized_pnl) FROM trading.pnl_day GROUP BY day ORDER BY day;`

4. **Positions**

* Join `positions_snapshots` with latest price from `bars_1m` (or fallback `bars_1d`): symbol, qty, avg_price, last_price, unrealized.

5. **Market Context**

* Price charts: SPY/QQQ and active symbols from `bars_*`.
* If index data missing → panel labeled **“Index feed not present”**.

**Variables**: `$account`, `$symbol`, `$universe`.

---

## 3) Data Fallback Rules (no exporters? no problem)

* **Service health:** if Prometheus `up`/`service_up` missing, use Loki:

  * **Degraded** if `sum(rate({component="$svc"}[5m])) == 0` (silence) or error bursts.
* **Kafka lag:** if no Prom metric, approximate via CH ingestion lag (`backfill_status`) and event throughput deltas.
* **Strategy active/inactive:** if PG lacks state, use latest `activated/disabled` log per `strategy_id`.
* **Unrealized PnL:** if `pnl_pos_5s` missing, show cost basis + last daily close; label as approximate.

---

## 4) Alerting (initial set, non-invasive)

* **Loki alerts (Grafana/Loki):**

  * `sum by(component)(count_over_time({level="error"}[5m])) > 0` → **Error burst in $component**
  * `sum(rate({component="ibkr"}[5m])) == 0` → **IBKR logs silent** (degraded)
* **Prometheus alerts (if metrics exist):**

  * `max_over_time(kafka_consumer_group_lag{group=~"run_system.*"}[10m]) > 5000`
  * `container_memory_usage_bytes{container=~"clickhouse|postgres"} / container_spec_memory_limit_bytes > 0.85`

(Attach to Infrastructure & Trading Ops dashboards; severity routing to your preferred channel.)

---

## 5) Implementation Plan & Deliverables

### 5.1 Steps

1. **Reality Check:** Fill `IMPLEMENTATION_STATUS.md` sections 1.1–1.4.
2. **Panel Migration:** Complete `PANEL_MATRIX.csv` (map, then prune old dashboards).
3. **Draft JSONs:** Build four dashboards under `grafana/dashboards/`:

   * `infrastructure_platform_health.json`
   * `trading_system_operations.json`
   * `strategy_pipeline_insight.json`
   * `financial_market_overview.json`
4. **QA & Screenshots:** Validate each panel against last 24h data; store screenshots under `doc/dashboard/qa/`.
5. **Alerts:** Add the initial Loki/Prom rules (JSON/YAML), reference from dashboards.
6. **Docs:** Update `GRAFANA_TROUBLESHOOTING_GUIDE.md` (new § “Unified Observability”) and `GRAFANA_QUICK_REFERENCE.md` (query snippets & variables).

### 5.2 Acceptance Criteria (per dashboard)

* **Infra:** All core services have a tile; errors visible; at least one perf chart per DB/Broker; IBKR log heartbeat.
* **Trading Ops:** Live log stream with filters; events/min timeline; error table; lag/latency chart (metric or derived).
* **Strategy:** Strategy roster; funnel stage counts; gate pass/fail; per‑strategy PnL ranking.
* **Financial:** Equity/cash KPIs; equity curve; daily realized PnL; positions table; market price chart(s).

### 5.3 Final Artifacts

* 4 dashboard JSONs (above)
* `doc/dashboard/PANEL_MATRIX.csv`
* `doc/dashboard/IMPLEMENTATION_STATUS.md` (completed)
* Alert rule files (Loki/Prom)
* Doc updates (Troubleshooting & Quick Reference)

---

## 6) Query Blueprints (stubs to accelerate build)

> Replace placeholders with your datasource UIDs and labels (`loki`, `prometheus`, `ch`).

**Loki (LogQL)**

* Error count per service: `sum by(component) (count_over_time({level="error"}[5m]))`
* Live run_system logs: `{app="run_system"} | json`
* Event rate: `sum by(event_name) (rate({app="run_system", event_name!=""}[1m]))`

**Prometheus (PromQL)**

* CPU: `sum by(container) (rate(container_cpu_usage_seconds_total{container=~"$service"}[5m]))`
* Kafka lag: `max by(topic,group) (kafka_consumer_group_lag{group=~"$group"})`

**ClickHouse (SQL)**

* Equity curve: `SELECT ts, equity FROM trading.equity_pnl_snapshots WHERE ts >= now()-INTERVAL 7 DAY ORDER BY ts;`
* Daily realized PnL: `SELECT day, sum(realized_pnl) FROM trading.pnl_day GROUP BY day ORDER BY day;`
* Positions + price: `SELECT p.symbol, p.qty, p.avg_price, anyLast(b.close) AS last_px, (p.qty*(anyLast(b.close)-p.avg_price)) AS unreal FROM trading.positions_snapshots p LEFT JOIN trading.bars_1m b ON b.symbol = p.symbol AND b.ts >= now()-INTERVAL 30 MIN GROUP BY p.symbol, p.qty, p.avg_price ORDER BY abs(unreal) DESC;`
* Funnel counts: `SELECT stage, count() FROM trading.order_funnel_daily WHERE day = today() GROUP BY stage;`
* Gate ratios: `SELECT gate, sum(pass) AS pass, sum(fail) AS fail FROM trading.gate_snapshots WHERE ts >= today() GROUP BY gate;`

---

## 7) Risks & Mitigations

* **Missing exporters/metrics** → use log‑based fallbacks; visibly label approximate panels.
* **Incomplete CH tables** → degrade gracefully; document as gap with next‑step suggestion (ingest or enable writer) — **no code changes now**.
* **Inconsistent log fields** → standardize via Explore queries; use permissive LogQL with `| json` + null‑tolerant filters.

---

## 8) Visual & UX Conventions

* Consistent color thresholds (🟢 healthy, 🟡 warning, 🔴 critical).
* Dashboard variables: `$env`, `$service`, `$strategy`, `$symbol`, `$group`, `$gate`.
* Time defaults: last 24h (Infra/Trading/Strategy), last 7d (Financial).
* Panel links for drill‑down (e.g., from Strategy → Trading Ops logs with pre‑filled filters).

---

### Ready to build

This plan is fully executable with your current stack. Proceed with **Phase‑0 Reality Check**, then implement dashboards in the order: **Infra → Trading Ops → Strategy → Financial**.

---

## 9) Bridge to “Dream Dashboards” — Additive Work Plan (No App Code Changes)

### 9.1 Priorities (impact → effort)

* **P0 – Monitoring foundation:** Prometheus exporters for infra + Kafka; promtail label promotion (no app code).
* **P1 – Ops leverage:** Grafana alert delivery (contact points + policies).
* **P2 – Strategy clarity:** Strategy inventory snapshot (PG/CH helper script).
* **P3 – Market context:** Index feed into ClickHouse (SPY/QQQ); optional backfill.
* **P4 – QA evidence:** Automated screenshots & coverage doc.

---

### 9.2 Prometheus Coverage Beyond `service_up`

**Goal:** CPU/Mem per container, Redis/Postgres availability, Kafka consumer lag, IBKR logs heartbeat panel.

#### a) `docker-compose.yml` (add exporters)

```yaml
services:
  node_exporter:
    image: prom/node-exporter:latest
    container_name: node_exporter
    ports: ["9100:9100"]
    restart: unless-stopped

  cadvisor:
    image: gcr.io/cadvisor/cadvisor:latest
    container_name: cadvisor
    ports: ["8080:8080"]
    volumes:
      - /:/rootfs:ro
      - /var/run:/var/run:ro
      - /sys:/sys:ro
      - /var/lib/docker/:/var/lib/docker:ro
    restart: unless-stopped

  kafka_exporter:
    image: danielqsj/kafka-exporter:latest
    container_name: kafka_exporter
    command:
      - --kafka.server=kafka:9092
    ports: ["9308:9308"]
    restart: unless-stopped

  redis_exporter:
    image: oliver006/redis_exporter:latest
    container_name: redis_exporter
    environment:
      - REDIS_ADDR=redis:6379
    ports: ["9121:9121"]
    restart: unless-stopped

  postgres_exporter:
    image: prometheuscommunity/postgres-exporter:latest
    container_name: postgres_exporter
    environment:
      - DATA_SOURCE_NAME=postgresql://postgres:postgres@postgres:5432/postgres?sslmode=disable
    ports: ["9187:9187"]
    restart: unless-stopped
```

> Note: On macOS Docker Desktop, `cadvisor` provides container metrics (host metrics may be partial). Adjust passwords/hosts per your stack.

#### b) `prometheus.yml` (scrape configs)

```yaml
scrape_configs:
  - job_name: 'node'
    static_configs: [{ targets: ['node_exporter:9100'] }]

  - job_name: 'cadvisor'
    static_configs: [{ targets: ['cadvisor:8080'] }]

  - job_name: 'kafka_exporter'
    static_configs: [{ targets: ['kafka_exporter:9308'] }]

  - job_name: 'redis_exporter'
    static_configs: [{ targets: ['redis_exporter:9121'] }]

  - job_name: 'postgres_exporter'
    static_configs: [{ targets: ['postgres_exporter:9187'] }]
```

#### c) Grafana variables

* Add `$job` to filter Prometheus panels: `label_values(up, job)`.
* Add `$group` for Kafka consumer groups: `label_values(kafka_consumer_group_lag, group)`.

---

### 9.3 Deeper Loki Labels **without touching app code**

**Goal:** Promote JSON fields in logs to labels for filtering/aggregation.

#### a) `promtail/config.yml` additions (pipeline stages)

```yaml
server:
  http_listen_port: 9080
positions:
  filename: /tmp/positions.yaml
clients:
  - url: http://loki:3100/loki/api/v1/push

scrape_configs:
  - job_name: containers
    static_configs:
      - targets: [localhost]
        labels:
          job: docker
          __path__: /var/log/containers/*.log
    pipeline_stages:
      - json
      - labels:
          event: event
          reason: reason
          strategy: strategy
          symbol: symbol
          component: component
          service: service
          app: app
      - replace:
          expression: '^(|unknown|null)$'
          replacement: 'na'
          source: component
      - match:
          selector: '{app="run_system"}'
          stages:
            - labeldrop:
                - order_id        # avoid high cardinality
                - request_id
```

> If fields don’t exist in some events, they’ll be empty; the `replace` sets `component="na"`. Avoid promoting high‑cardinality IDs.

#### b) Dashboard updates

* Switch queries to labels now guaranteed present (`component`, `service`, `app`, `strategy`, `symbol`).

> If app‑side changes become allowed later, add `logger.bind(component=...)` near emit sites for perfect fidelity.

---

### 9.4 Strategy Inventory / Active State (helper snapshot)

**Goal:** Power the Strategy Inventory table without modifying runtime services.

#### a) PostgreSQL schema (lightweight)

```sql
CREATE TABLE IF NOT EXISTS public.strategy_status_snapshots (
  strategy_id   text PRIMARY KEY,
  name          text,
  symbol        text,
  is_active     boolean NOT NULL,
  source        text DEFAULT 'config',
  updated_at    timestamptz NOT NULL DEFAULT now(),
  notes         text
);

CREATE VIEW public.strategy_status AS
SELECT * FROM public.strategy_status_snapshots;
```

#### b) Minimal helper script (pseudo‑Python)

```python
# read configs (yaml/json) → records; no runtime coupling
import os, json, psycopg2, datetime as dt
cfg = json.load(open('configs/strategies.json'))
rows = [(s['id'], s.get('name'), s.get('symbol'), bool(s.get('enabled', True))) for s in cfg]
conn = psycopg2.connect(os.environ['PG_URL'])
cur = conn.cursor()
for sid, name, sym, active in rows:
    cur.execute("""
        INSERT INTO public.strategy_status_snapshots(strategy_id,name,symbol,is_active,updated_at)
        VALUES (%s,%s,%s,%s,now())
        ON CONFLICT (strategy_id)
        DO UPDATE SET name=EXCLUDED.name, symbol=EXCLUDED.symbol,
                      is_active=EXCLUDED.is_active, updated_at=now();
    """, (sid,name,sym,active))
conn.commit()
```

* Schedule via cron/systemd/compose sidecar to refresh on boot and hourly.
* **ClickHouse alternative** (if preferred): create `ReplacingMergeTree` table with `version=toUnixTimestamp(updated_at)` and query `argMax`.

---

### 9.5 Alert Delivery (Grafana provisioning)

**Goal:** Make alerts actionable (Slack/Email) without UI clicks.

#### a) `grafana/provisioning/alerting/contact-points.yaml`

```yaml
apiVersion: 1
contactPoints:
  - orgId: 1
    name: slack-alerts
    receivers:
      - uid: slack-webhook
        type: slack
        settings:
          url: ${SLACK_WEBHOOK_URL}
          mentionChannel: true
  - orgId: 1
    name: email-alerts
    receivers:
      - uid: email-default
        type: email
        settings:
          addresses: you@example.com
```

#### b) `grafana/provisioning/alerting/notification-policies.yaml`

```yaml
apiVersion: 1
policies:
  - orgId: 1
    receiver: slack-alerts
    group_by: ['alertname']
    matchers: []
```

#### c) Permissions

* Create **Service Account** in Grafana → generate token → use for CI or scripted alert rule loads.
* Ensure role allows alerting API; store token as secret env var.

---

### 9.6 Market Index Context (into ClickHouse)

**Goal:** Add SPY/QQQ (and others) for benchmarks and context.

#### a) ClickHouse DDL

```sql
CREATE TABLE IF NOT EXISTS trading.market_index_snapshots (
  ts       DateTime CODEC(Delta, ZSTD),
  symbol   LowCardinality(String),
  open     Float64,
  high     Float64,
  low      Float64,
  close    Float64,
  volume   UInt64
) ENGINE = MergeTree
ORDER BY (symbol, ts)
SETTINGS index_granularity = 8192;
```

#### b) Simple loader (Python + yfinance example)

```python
import pandas as pd, yfinance as yf
from clickhouse_driver import Client
symbols = ['SPY','QQQ']
client = Client(host='clickhouse', user='default')
for s in symbols:
    df = yf.download(s, period='1y', interval='1d').reset_index()
    rows = [(r['Date'].to_pydatetime(), s, float(r['Open']), float(r['High']), float(r['Low']), float(r['Close']), int(r['Volume'])) for _, r in df.iterrows()]
    client.execute('INSERT INTO trading.market_index_snapshots VALUES', rows)
```

> If you already ingest SPY/QQQ to `bars_*`, skip new table and reuse existing.

---

### 9.7 QA Artefacts (evidence of coverage)

**Goal:** Capture proof that each panel renders and reflects recent data.

* Enable Grafana image renderer or use `/render` endpoint.
* Example (replace UID & panelId):

```bash
curl -s -H "Authorization: Bearer $GRAFANA_TOKEN" \
  "http://grafana:3000/render/d-solo/INFRA_UID/_?orgId=1&from=now-6h&to=now&panelId=12&var-service=clickhouse" \
  -o doc/dashboard/qa/infra_clickhouse_status.png
```

* Store 3–5 screenshots per dashboard in `doc/dashboard/qa/` and reference in `IMPLEMENTATION_STATUS.md`.

---

### 9.8 Additive Rollout Sequence

1. **P0**: Add exporters & Prom scrape jobs → validate in Prom (targets up, metrics present). Update Infra dashboard to use new metrics.
2. **P0**: Add promtail pipeline to promote labels → verify in Explore; update run_system/Strategy panels to use labels.
3. **P1**: Provision contact points & policy → add 1–2 critical alerts (Kafka lag, error bursts).
4. **P2**: Deploy strategy snapshot helper → validate Strategy Inventory panel sources.
5. **P3**: Load market indices into CH → enable benchmark charts.
6. **P4**: Generate QA screenshots → attach to docs.

> All steps are **additive** and leave the trading runtime untouched.
Okay, this Python script provides an excellent starting point for the `PANEL_MATRIX.csv`. It correctly identifies key panels from the known dashboards and maps them to the new consolidated structure, including potential blockers and priorities.

Here's an improved version, incorporating refinements based on our overall plan and potential system structure:

```python
import pandas as pd
from datetime import datetime
import io # Needed for creating CSV in memory

# Get current timestamp for tracking
now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

rows = [
    # ============================================
    # == Infrastructure & Platform Health       ==
    # ============================================
    # --- Service Health / Status ---
    {"from_dashboard":"phase0_infrastructure.json","from_panel":"Service Health Tiles","source_type":"Loki","query_hint":"error/warn count_over_time by component","to_dashboard":"Infrastructure & Platform Health","panel_type":"Stat Grid","migration_action":"Combine","blockers":"","priority":"P0","notes":f"Combine LogQL status with Prometheus 'up'/'service_up'. Color thresholds: G/Y/R. Last reviewed: {now}"},
    {"from_dashboard":"phase0_infrastructure_simple.json","from_panel":"Uptime Overview","source_type":"Prometheus","query_hint":"up{job=~'.*'} / service_up{service=~'.*'}", "to_dashboard":"Infrastructure & Platform Health","panel_type":"Stat Grid","migration_action":"Combine","blockers":"Confirm scrape jobs/metrics","priority":"P0","notes":f"Merge logic into Global Status Grid. Verify metric availability in Reality Check. Last reviewed: {now}"},
    {"from_dashboard":"good.json","from_panel":"IBKR Connectivity Alerts","source_type":"Loki","query_hint":"{component=\"ibkr\"} |~ \"disconnect|timeout|connection reset\"","to_dashboard":"Infrastructure & Platform Health","panel_type":"Stat/Logs","migration_action":"Reuse","blockers":"","priority":"P0","notes":f"Add reconnects/hour stat. Verify 'component' label in Reality Check. Last reviewed: {now}"},

    # --- Errors / Warnings ---
    {"from_dashboard":"phase0_infrastructure.json","from_panel":"Errors by Component (5m)","source_type":"Loki","query_hint":"sum by(component) (count_over_time({level=\"error\"}[5m]))","to_dashboard":"Infrastructure & Platform Health","panel_type":"Bar/Heatmap","migration_action":"Reuse","blockers":"Verify 'component' label","priority":"P0","notes":f"Move to Error Hotspots row. Verify 'component' label in Reality Check. Last reviewed: {now}"},
    {"from_dashboard":"phase0_infrastructure.json","from_panel":"Recent Infra Errors","source_type":"Loki","query_hint":"{component=~\"postgres|clickhouse|redis|kafka|loki|grafana\"} |= \"ERROR\"","to_dashboard":"Infrastructure & Platform Health","panel_type":"Logs","migration_action":"Reuse","blockers":"Verify 'component' label","priority":"P0","notes":f"Add quick filters for component/env. Verify label in Reality Check. Last reviewed: {now}"},

    # --- Resource Usage ---
    {"from_dashboard":"phase0_infrastructure.json","from_panel":"Container CPU","source_type":"Prometheus","query_hint":"rate(container_cpu_usage_seconds_total[5m])","to_dashboard":"Infrastructure & Platform Health","panel_type":"Time Series","migration_action":"Rewrite","blockers":"Need cadvisor/node-exporter","priority":"P1","notes":f"Requires exporter (Bridge Plan P0). Group by service. Hide if metric unavailable. Last reviewed: {now}"},
    {"from_dashboard":"phase0_infrastructure.json","from_panel":"Container Memory","source_type":"Prometheus","query_hint":"container_memory_usage_bytes","to_dashboard":"Infrastructure & Platform Health","panel_type":"Time Series","migration_action":"Rewrite","blockers":"Need cadvisor/node-exporter","priority":"P1","notes":f"Requires exporter (Bridge Plan P0). Group by service. Hide if metric unavailable. Last reviewed: {now}"},
    # Add placeholders for Network/Disk if exporters are added

    # --- DB & Broker Health ---
    {"from_dashboard":"good.json","from_panel":"Kafka Consumer Lag (grid)","source_type":"Prometheus","query_hint":"kafka_consumer_group_lag / kafka_topic_lag","to_dashboard":"Infrastructure & Platform Health","panel_type":"Heatmap/Table","migration_action":"Reuse","blockers":"Confirm metric name/exporter","priority":"P0/P1","notes":f"Verify metric in Reality Check. May require kafka_exporter (Bridge Plan P0). Add $group variable. Last reviewed: {now}"},
    {"from_dashboard":"good.json","from_panel":"ClickHouse Replication Status","source_type":"ClickHouse","query_hint":"system.replicas","to_dashboard":"Infrastructure & Platform Health","panel_type":"Table","migration_action":"Reuse","blockers":"","priority":"P0","notes":f"Add row coloring for lag > 0. Verify access to system tables. Last reviewed: {now}"},
    {"from_dashboard":"good.json","from_panel":"Backfill/Ingestion Lag","source_type":"ClickHouse","query_hint":"trading.backfill_status","to_dashboard":"Infrastructure & Platform Health","panel_type":"Table/Stat","migration_action":"Reuse","blockers":"Verify table exists","priority":"P0","notes":f"Show max age by pipeline. Verify table in Reality Check. Last reviewed: {now}"},
    # Add placeholders for Redis/Postgres health if exporters are added

    # ============================================
    # == Trading System Operations (run_system) ==
    # ============================================
    # --- Logs & Events ---
    {"from_dashboard":"run_system_ops.json","from_panel":"Live Log Stream","source_type":"Loki","query_hint":"{app=\"run_system\"} | json","to_dashboard":"Trading System Operations","panel_type":"Logs","migration_action":"Reuse","blockers":"Verify 'app' label","priority":"P0","notes":f"Preserve filters: level, component, strategy_id, symbol, order_id. Verify labels in Reality Check (may need Promtail relabeling - Bridge P0). Last reviewed: {now}"},
    {"from_dashboard":"trading_observability.json","from_panel":"Event Rate by event_name","source_type":"Loki","query_hint":"sum by(event_name) (rate({app=\"run_system\",event_name!=\"\"}[1m]))","to_dashboard":"Trading System Operations","panel_type":"Time Series","migration_action":"Reuse","blockers":"Verify 'event_name' label","priority":"P0","notes":f"Legend maps to Signal/Order/Fill etc. Verify label in Reality Check. Last reviewed: {now}"},
    {"from_dashboard":"trading_trace_and_logs.json","from_panel":"Error Diagnostics Table","source_type":"Loki","query_hint":"{app=\"run_system\", level=\"error\"} | json | line_format","to_dashboard":"Trading System Operations","panel_type":"Table","migration_action":"Combine","blockers":"Verify labels","priority":"P0","notes":f"Append useful columns if parsed: strategy_id, symbol, component, error_type. Verify labels. Last reviewed: {now}"},

    # --- Stats & Throughput ---
    {"from_dashboard":"run_system_ops.json","from_panel":"Errors Today","source_type":"Loki","query_hint":"count_over_time({app=\"run_system\",level=\"error\"}[1d])","to_dashboard":"Trading System Operations","panel_type":"Stat","migration_action":"Reuse","blockers":"Verify 'app' label","priority":"P0","notes":f"Link to error table below. Last reviewed: {now}"},
    {"from_dashboard":"trading_observability.json","from_panel":"Signals Processed (today)","source_type":"Loki","query_hint":"count_over_time({app=\"run_system\", event_name=\"SignalReceived\"}[1d])","to_dashboard":"Trading System Operations","panel_type":"Stat","migration_action":"Reuse","blockers":"Verify 'event_name' label","priority":"P0","notes":f"Verify label. Last reviewed: {now}"},
    {"from_dashboard":"trading_observability.json","from_panel":"Orders Sent (today)","source_type":"Loki","query_hint":"count_over_time({app=\"run_system\", event_name=\"OrderIntentSent\"}[1d])","to_dashboard":"Trading System Operations","panel_type":"Stat","migration_action":"Reuse","blockers":"Verify 'event_name' label","priority":"P0","notes":f"Verify label. Last reviewed: {now}"},
    # Add CH query for Order/Fill throughput (exec_events)

    # --- Latency & Backlog ---
    {"from_dashboard":"good.json","from_panel":"Kafka Lag vs Throughput","source_type":"Prometheus/ClickHouse","query_hint":"kafka_consumer_group_lag + exec_events per min","to_dashboard":"Trading System Operations","panel_type":"Dual Axis","migration_action":"Rewrite","blockers":"Exporter & CH join logic","priority":"P1","notes":f"Overlay lag (Prometheus) and processed events (ClickHouse). Requires exporter/metric verification. Last reviewed: {now}"},
    {"from_dashboard":"trading_why_not_full.json","from_panel":"Signal->Order Derived Latency","source_type":"Loki","query_hint":"pair events by order_id/symbol timestamps","to_dashboard":"Trading System Operations","panel_type":"Histogram/Time Series","migration_action":"Rewrite","blockers":"Consistent IDs in logs","priority":"P1","notes":f"Requires structured logs with consistent correlation IDs. Verify feasibility in Reality Check. Last reviewed: {now}"},
    # Add Prometheus latency_* histogram panels if metrics exist

    # --- Tracing (Optional) ---
    {"from_dashboard":"trading_trace_and_logs.json","from_panel":"Trace Linkouts","source_type":"Tempo","query_hint":"traceID from logs","to_dashboard":"Trading System Operations","panel_type":"Logs/Trace list","migration_action":"Integrate","blockers":"Ensure Tempo datasource uid & traceID propagation","priority":"P2","notes":f"Integrate Tempo links into Logs panel if traceIDs are logged. Last reviewed: {now}"},

    # ============================================
    # == Strategy & Decision Pipeline Insight   ==
    # ============================================
    # --- Inventory & Status ---
    {"from_dashboard":"strategy_monitor.json","from_panel":"Strategy List (basic)","source_type":"Postgres/Loki/Redis","query_hint":"PG 'strategies' table or latest activation logs","to_dashboard":"Strategy & Decision Pipeline Insight","panel_type":"Table","migration_action":"Combine/Rewrite","blockers":"Confirm reliable state source","priority":"P0/P2","notes":f"Prefer dedicated PG table (Bridge Plan P2). Fallback to logs/Redis is less reliable. Add CH PnL columns. Last reviewed: {now}"},

    # --- Funnel & Gates ---
    {"from_dashboard":"good.json","from_panel":"Decision Funnel (counts)","source_type":"ClickHouse/Loki","query_hint":"trading.order_funnel_daily by stage / Loki event counts","to_dashboard":"Strategy & Decision Pipeline Insight","panel_type":"Funnel/Bar","migration_action":"Reuse/Rewrite","blockers":"Confirm CH table/Loki events","priority":"P0","notes":f"Prefer CH 'order_funnel_daily'. Fallback to Loki counts (SignalReceived, cost_gate_pass, risk_check_pass, OrderIntentSent, Fill). Verify data source in Reality Check. Last reviewed: {now}"},
    {"from_dashboard":"good.json","from_panel":"Gate Pass/Fail Ratios","source_type":"ClickHouse/Loki","query_hint":"trading.gate_snapshots / Loki event counts","to_dashboard":"Strategy & Decision Pipeline Insight","panel_type":"Bar/Stat/Table","migration_action":"Reuse/Rewrite","blockers":"Confirm CH table/Loki events","priority":"P0","notes":f"Prefer CH 'gate_snapshots'. Fallback to Loki counts for pass/fail events per gate. Add top rejection reasons table (from logs or CH). Verify data source in Reality Check. Last reviewed: {now}"},
    {"from_dashboard":"trading_why_not.json","from_panel":"Why-Not Filters Table","source_type":"Loki","query_hint":"{event_name=\"order_skipped\"} | json | stats count() by reason","to_dashboard":"Strategy & Decision Pipeline Insight","panel_type":"Table","migration_action":"Combine","blockers":"Verify 'order_skipped' event & 'reason' field","priority":"P1","notes":f"Integrate into Gate Analysis section. Group by reason, show trend over time. Verify logs in Reality Check. Last reviewed: {now}"},

    # --- Performance ---
    {"from_dashboard":"strategy_monitor.json","from_panel":"Signals per Strategy","source_type":"Loki","query_hint":"sum by(strategy_id) (rate({event_name=\"SignalReceived\"}[5m]))","to_dashboard":"Strategy & Decision Pipeline Insight","panel_type":"Time Series","migration_action":"Reuse","blockers":"Verify 'strategy_id' label","priority":"P0","notes":f"Verify label. Last reviewed: {now}"},
    {"from_dashboard":"aapl_dividend_wave_monitor.json","from_panel":"Per-Symbol Strategy Activity","source_type":"Loki/ClickHouse","query_hint":"symbol-scoped counts + pnl_day","to_dashboard":"Strategy & Decision Pipeline Insight","panel_type":"Mixed","migration_action":"Template","blockers":"","priority":"P1","notes":f"Make this a generic template row, filtered by dashboard variable $symbol. Last reviewed: {now}"},
    {"from_dashboard":"good.json","from_panel":"Top/Bottom Strategies by PnL","source_type":"ClickHouse","query_hint":"pnl_day or trade_pnl aggregation","to_dashboard":"Strategy & Decision Pipeline Insight","panel_type":"Table","migration_action":"Reuse","blockers":"Confirm PnL tables","priority":"P0","notes":f"Verify CH tables ('pnl_day', 'trade_pnl', 'pnl_fills'). Add sparkline, win rate, avg win/loss. Last reviewed: {now}"},

    # ============================================
    # == Financial & Market Overview            ==
    # ============================================
    # --- Headline KPIs & Equity Curve ---
    {"from_dashboard":"hybrid_trading_overview.json","from_panel":"Portfolio KPIs (Equity/Cash/TodayPnL)","source_type":"ClickHouse","query_hint":"equity_pnl_snapshots (latest) + pnl_day (sum)","to_dashboard":"Financial & Market Overview","panel_type":"Stat Row","migration_action":"Reuse","blockers":"Confirm tables","priority":"P0","notes":f"Add Unrealized PnL from 'pnl_pos_5s' (if available, else mark N/A). Verify tables. Last reviewed: {now}"},
    {"from_dashboard":"hybrid_trading_overview.json","from_panel":"Equity Curve","source_type":"ClickHouse","query_hint":"equity_pnl_snapshots time series","to_dashboard":"Financial & Market Overview","panel_type":"Time Series","migration_action":"Reuse","blockers":"Confirm table","priority":"P0","notes":f"Verify table. Last reviewed: {now}"},
    {"from_dashboard":"hybrid_trading_overview.json","from_panel":"Daily Realized PnL","source_type":"ClickHouse","query_hint":"pnl_day","to_dashboard":"Financial & Market Overview","panel_type":"Bar Chart","migration_action":"Reuse","blockers":"Confirm table","priority":"P0","notes":f"Verify table. Last reviewed: {now}"},

    # --- Positions & Trades ---
    {"from_dashboard":"hybrid_trading_overview.json","from_panel":"Positions Snapshot","source_type":"ClickHouse","query_hint":"positions_snapshots join bars_*","to_dashboard":"Financial & Market Overview","panel_type":"Table","migration_action":"Combine/Rewrite","blockers":"'bars_*' availability/timeliness","priority":"P0","notes":f"Join 'positions_snapshots' (qty, avg_price) with latest price from 'bars_1m' or 'pnl_pos_5s' to compute Unrealized PnL. Fallback to 'bars_1d' if necessary (mark as EOD approx). Verify tables. Last reviewed: {now}"},
    {"from_dashboard":"hybrid_trading_overview.json","from_panel":"Recent Trades / Fills","source_type":"ClickHouse","query_hint":"fills_live / exec_events where type='fill'","to_dashboard":"Financial & Market Overview","panel_type":"Table","migration_action":"Reuse","blockers":"Confirm table","priority":"P0","notes":f"Verify 'fills_live' or 'exec_events'. Add filters: symbol/strategy/side. Last reviewed: {now}"},

    # --- Market Context ---
    {"from_dashboard":"good.json","from_panel":"Market Index (SPY/QQQ)","source_type":"ClickHouse","query_hint":"market_index_snapshots or bars_* for SPY/QQQ","to_dashboard":"Financial & Market Overview","panel_type":"Time Series","migration_action":"Rewrite","blockers":"Need index data feed","priority":"P2","notes":f"Requires index data ingestion (Bridge Plan P3). Verify if 'bars_*' already contains SPY/QQQ. Hide panel if data absent. Last reviewed: {now}"},
    # Add panel for price charts of actively traded symbols using bars_*

    # ============================================
    # == Shared / Misc / To Deprecate         ==
    # ============================================
    {"from_dashboard":"log_everything.json","from_panel":"All Logs (Debug)","source_type":"Loki","query_hint":"{}","to_dashboard":"N/A (Explore Link)","panel_type":"Logs","migration_action":"Defer","blockers":"","priority":"P3","notes":f"Too noisy for a dashboard panel. Provide link to Grafana Explore instead. Last reviewed: {now}"},
    # Add entries for any other specific panels from legacy dashboards (`dashboards/*.json`) if they provide unique value and need migration.
]

df = pd.DataFrame(rows, columns=[
    "from_dashboard","from_panel","source_type","query_hint","to_dashboard",
    "panel_type","migration_action","blockers","priority","notes"
])

# Create CSV content in memory
csv_buffer = io.StringIO()
df.to_csv(csv_buffer, index=False)
csv_content = csv_buffer.getvalue()

# (Optional) Define path if saving to file system is needed later
# path = "/mnt/data/PANEL_MATRIX.csv"
# df.to_csv(path, index=False)

# Display result (or return content)
# import caas_jupyter_tools as cj # Assuming this is available in the execution env
# cj.display_dataframe_to_user("PANEL_MATRIX (Improved)", df)

# Return the CSV content as a string
# print(csv_content) # Or return it if this were a function

# For display in this environment, let's just show the head
print("Generated PANEL_MATRIX.csv content (first 5 rows):\n")
print(df.head().to_markdown(index=False))
print("\n... (Full CSV generated)")

# If running locally and need the file:
# path = "PANEL_MATRIX.csv"
# df.to_csv(path, index=False)
# print(f"\nCSV saved to: {path}")

```

**Improvements Made:**

1.  **Added Timestamps:** Included a `last_reviewed` timestamp in the `notes` for each row to track when the mapping was assessed.
2.  **Clearer Blocker/Priority References:** Explicitly linked blockers and priorities (P0, P1, etc.) back to the "Bridge Plan" steps (Exporters, Promtail relabeling, Helper scripts) where appropriate.
3.  **Refined Migration Actions:** Adjusted some actions (e.g., `Template` for the per-symbol panel, `Integrate` for trace links).
4.  **Emphasized Reality Check:** Added notes to explicitly mention verifying specific tables or labels during the "Phase 0: Data Reality Check".
5.  **Handling Missing Data:** Made notes clearer about hiding panels or marking data as approximate if underlying metrics/tables are confirmed missing after the Reality Check.
6.  **Placeholders Added:** Added comments indicating where placeholders for future panels (e.g., Network/Disk IO, Redis/Postgres health, actively traded symbols) should be considered once exporters are added.
7.  **Code Structure:** Used `io.StringIO` to generate the CSV content in memory, making it easier to return or display without necessarily writing to a file immediately. Added comments for saving locally if needed.

This revised structure provides a more robust and trackable mapping document to guide the consolidation effort. Remember to update the `notes` and `blockers` based on the actual findings during the "Phase 0: Data Reality Check".