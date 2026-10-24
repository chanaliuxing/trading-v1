Grafana Dashboards Consolidation — Final Plan v3.1

Objective: Replace 12+ fragmented dashboards with 4 cohesive, high‑signal Grafana dashboards that provide full visibility into infrastructure, trading system operations, strategy pipeline, and financial/market status — using only existing data (Loki logs, ClickHouse, Prometheus, PostgreSQL). No code changes.

0) Scope, Constraints, Success Criteria

Scope



Consolidate into 4 dashboards:

Infrastructure & Platform Health

Trading System Operations (run_system)

Strategy & Decision Pipeline Insight

Financial & Market Overview

Constraints



No application code edits. Only reuse existing metrics/logs/DB tables.

Prefer panel reuse from current JSONs.

Success criteria



One‑screen health view for all core services with error/warn visibility.

Single deep‑dive view for run_system with live logs + throughput/lag.

Strategy funnel (signals→cost gate→risk gate→orders→fills) and PnL per strategy.

Financial dashboard that shows equity/cash, realized/unrealized PnL, positions, and key market context (where data exists).

1) Phase‑0: Data Reality Check (prevent dead ends)

Create/maintain doc/dashboard/IMPLEMENTATION_STATUS.md with ✅/❌ tables.



1.1 Prometheus coverage

Inspect scrape targets:

curl -s http://localhost:9090/api/v1/targets | jq '.data.activeTargets[] | {job: .labels.job, url: .scrapeUrl, health: .health}'

Discover metric families actually present:

curl -s 'http://localhost:9090/api/v1/series?match[]=%7B__name__%3D~%22(kafka.*lag|container_.*|latency_.*|errors_total|service_up)%22%7D' | jq '.data | length'

Record whether we have: container_*, kafka_*lag*, latency_*, errors_total, service_up.

1.2 Loki labels/structure

In Grafana Explore (Loki), verify labels exist: app, component, service, event_name, strategy_id, symbol, level.

Sample:

{app="run_system"} | json

sum by(component) (rate({level="error"}[5m]))

Note exact field names/variants.

1.3 ClickHouse tables

Inventory trading schema:

SHOW TABLES FROM trading;

SELECT database, name FROM system.tables WHERE database='trading' ORDER BY name;

Validate availability of expected tables (mark ✅/❌):

equity_pnl_snapshots, positions_snapshots, pnl_pos_5s, pnl_day, pnl_fills/trade_pnl, order_events, exec_events, gate_snapshots, order_funnel_daily, backfill_status, any bars_* (1m/1d etc.), optional market_index_*.

1.4 PostgreSQL metadata

Verify existence/utility of strategies, strategy_state (or equivalent) for active/inactive flags and names.

If absent, plan to derive from logs.

1.5 Panel Migration Matrix (reuse what works)

Create doc/dashboard/PANEL_MATRIX.csv mapping existing → new. Seed rows:

From DashboardPanel/PurposeTo Dashboardphase0_infrastructure.jsonService health tiles (log-based)Infrastructure & Platform Healthgood.jsonError/Warn counts by componentInfrastructure & Platform Healthrun_system_ops.jsonLive log stream + filtersTrading System Operationstrading_observability.jsonEvent rate timelinesTrading System Operationsstrategy_monitor.jsonStrategy list & basic statusStrategy & Pipelineaapl_dividend_wave_monitor.jsonPer-symbol behaviorStrategy & Pipelinehybrid_trading_overview.jsonPortfolio/PnL snapshotsFinancial & Markettrading_trace_and_logs.json, trading_why_not*.jsonDebug sequences/tracesTrading System Operations2) Dashboard Designs (final)

2.1 Infrastructure & Platform Health ⚙️

Purpose: See if everything is up, detect errors/warnings, and assess performance per service.

Sections & Panels



Global Status Grid

Source: Prometheus up{job=…} or service_up{service=…} if present.

Fallback rule (Loki): status =

🟢 if rate({component="$svc"}[5m]) > 0 and rate({component="$svc", level="error"}[5m]) == 0

🟡 if error rate > 0

🔴 if no logs in last 5m (degraded/offline signal)

Resource Usage (per container)

CPU: rate(container_cpu_usage_seconds_total{container=~"$service"}[5m])

Mem: container_memory_usage_bytes{container=~"$service"}

Net/Disk if available; else omit.

DB & Broker Health

ClickHouse replicas/queries: SELECT * FROM system.replicas;, SELECT metric,value FROM system.metrics WHERE metric IN ('Query','QueryPreempted','DelayedInserts');

Ingestion/backfill: SELECT * FROM trading.backfill_status ORDER BY updated_at DESC LIMIT 50;

Kafka lag (if present): kafka_consumer_group_lag{group=~"$group"} heatmap.

Error Hotspots

Loki: sum by(component) (count_over_time({level="error"}[5m]))

Heatmap over time + table of latest critical logs.

IBKR Connectivity

Loki: {component="ibkr"} |~ "disconnect|timeout|connection reset"

Stat: reconnects/hour via count_over_time.

Variables: $service (postgres|clickhouse|redis|kafka|loki|grafana|ibkr), $group (Kafka), $env.

2.2 Trading System Operations (run_system) 🚀

Purpose: A single deep‑dive for runtime behavior, throughput, lag, warnings, and errors.

Sections & Panels



Live Logs

Loki stream: {app="run_system"} | json

Quick filters: level, component, strategy_id, symbol, order_id.

Operational Stats

Signals processed today: sum(count_over_time({app="run_system", event_name="SignalReceived"}[1d]))

Orders sent today: … event_name="OrderIntentSent" …

Errors today: … level="error" …

Throughput Timeline

Events/min by event_name (SignalReceived, OrderIntentSent, OrderFilled).

CH overlay (if desired): SELECT toStartOfInterval(ts, INTERVAL 1m) t, count() FROM trading.exec_events GROUP BY t ORDER BY t;

Latency & Backlog

Prometheus latency_* histograms if present; else derived latency from log pairs (Signal→Order) via Loki pattern/line_format and binning.

Kafka consumer lag for run_system groups if available.

Error Diagnostics

Table of last N ERROR logs (with parsed error_type).

Bar chart of error categories by error_type or substring grouping.

Variables: $component (System|PriceCache|StrategyEngine|market_data|… ), $strategy, $symbol, $group.

2.3 Strategy & Decision Pipeline Insight 📈

Purpose: Which strategies are active? Where do decisions get stuck? Who performs best/worst?

Sections & Panels



Strategy Inventory

If PG strategies/strategy_state exists: name, active flag, symbol, notes.

Else derive from logs (latest activated/disabled event per strategy_id).

Join CH for PnL today/total.

Decision Funnel (day or range)

Prefer CH tables:

order_funnel_daily → counts at each stage.

gate_snapshots → pass/fail per gate + latency_ms.

Fallback: Loki counts by event_name (cost_gate_pass, risk_check_pass, order_skipped, order_sent, fill).

Gate Analysis

Pass/Fail ratio per gate over time; top rejection reasons (from CH or parsed logs).

Performance Leaderboard

PnL per strategy (time series + table): pnl_day, pnl_fills/trade_pnl.

Win rate, avg win/loss computed via CH aggregation.

Variables: $strategy, $symbol, $gate.

2.4 Financial & Market Overview 💰

Purpose: Equity/cash, realized/unrealized PnL, positions, market context.

Sections & Panels



Headline KPIs

Equity, Cash, BP: equity_pnl_snapshots (latest row per account).

Realized PnL (today): pnl_day sum.

Unrealized PnL (current): pnl_pos_5s latest if available; else mark Data Pending.

Equity Curve

SELECT ts, equity FROM trading.equity_pnl_snapshots ORDER BY ts;

Daily PnL

SELECT day, sum(realized_pnl) FROM trading.pnl_day GROUP BY day ORDER BY day;

Positions

Join positions_snapshots with latest price from bars_1m (or fallback bars_1d): symbol, qty, avg_price, last_price, unrealized.

Market Context

Price charts: SPY/QQQ and active symbols from bars_*.

If index data missing → panel labeled “Index feed not present”.

Variables: $account, $symbol, $universe.

3) Data Fallback Rules (no exporters? no problem)

Service health: if Prometheus up/service_up missing, use Loki:

Degraded if sum(rate({component="$svc"}[5m])) == 0 (silence) or error bursts.

Kafka lag: if no Prom metric, approximate via CH ingestion lag (backfill_status) and event throughput deltas.

Strategy active/inactive: if PG lacks state, use latest activated/disabled log per strategy_id.

Unrealized PnL: if pnl_pos_5s missing, show cost basis + last daily close; label as approximate.

4) Alerting (initial set, non-invasive)

Loki alerts (Grafana/Loki):

sum by(component)(count_over_time({level="error"}[5m])) > 0 → Error burst in $component

sum(rate({component="ibkr"}[5m])) == 0 → IBKR logs silent (degraded)

Prometheus alerts (if metrics exist):

max_over_time(kafka_consumer_group_lag{group=~"run_system.*"}[10m]) > 5000

container_memory_usage_bytes{container=~"clickhouse|postgres"} / container_spec_memory_limit_bytes > 0.85

(Attach to Infrastructure & Trading Ops dashboards; severity routing to your preferred channel.)

5) Implementation Plan & Deliverables

5.1 Steps

Reality Check: Fill IMPLEMENTATION_STATUS.md sections 1.1–1.4.

Panel Migration: Complete PANEL_MATRIX.csv (map, then prune old dashboards).

Draft JSONs: Build four dashboards under grafana/dashboards/:

infrastructure_platform_health.json

trading_system_operations.json

strategy_pipeline_insight.json

financial_market_overview.json

QA & Screenshots: Validate each panel against last 24h data; store screenshots under doc/dashboard/qa/.

Alerts: Add the initial Loki/Prom rules (JSON/YAML), reference from dashboards.

Docs: Update GRAFANA_TROUBLESHOOTING_GUIDE.md (new § “Unified Observability”) and GRAFANA_QUICK_REFERENCE.md (query snippets & variables).

5.2 Acceptance Criteria (per dashboard)

Infra: All core services have a tile; errors visible; at least one perf chart per DB/Broker; IBKR log heartbeat.

Trading Ops: Live log stream with filters; events/min timeline; error table; lag/latency chart (metric or derived).

Strategy: Strategy roster; funnel stage counts; gate pass/fail; per‑strategy PnL ranking.

Financial: Equity/cash KPIs; equity curve; daily realized PnL; positions table; market price chart(s).

5.3 Final Artifacts

4 dashboard JSONs (above)

doc/dashboard/PANEL_MATRIX.csv

doc/dashboard/IMPLEMENTATION_STATUS.md (completed)

Alert rule files (Loki/Prom)

Doc updates (Troubleshooting & Quick Reference)

6) Query Blueprints (stubs to accelerate build)

Replace placeholders with your datasource UIDs and labels (loki, prometheus, ch).

Loki (LogQL)



Error count per service: sum by(component) (count_over_time({level="error"}[5m]))

Live run_system logs: {app="run_system"} | json

Event rate: sum by(event_name) (rate({app="run_system", event_name!=""}[1m]))

Prometheus (PromQL)



CPU: sum by(container) (rate(container_cpu_usage_seconds_total{container=~"$service"}[5m]))

Kafka lag: max by(topic,group) (kafka_consumer_group_lag{group=~"$group"})

ClickHouse (SQL)



Equity curve: SELECT ts, equity FROM trading.equity_pnl_snapshots WHERE ts >= now()-INTERVAL 7 DAY ORDER BY ts;

Daily realized PnL: SELECT day, sum(realized_pnl) FROM trading.pnl_day GROUP BY day ORDER BY day;

Positions + price: SELECT p.symbol, p.qty, p.avg_price, anyLast(b.close) AS last_px, (p.qty*(anyLast(b.close)-p.avg_price)) AS unreal FROM trading.positions_snapshots p LEFT JOIN trading.bars_1m b ON b.symbol = p.symbol AND b.ts >= now()-INTERVAL 30 MIN GROUP BY p.symbol, p.qty, p.avg_price ORDER BY abs(unreal) DESC;

Funnel counts: SELECT stage, count() FROM trading.order_funnel_daily WHERE day = today() GROUP BY stage;

Gate ratios: SELECT gate, sum(pass) AS pass, sum(fail) AS fail FROM trading.gate_snapshots WHERE ts >= today() GROUP BY gate;

7) Risks & Mitigations

Missing exporters/metrics → use log‑based fallbacks; visibly label approximate panels.

Incomplete CH tables → degrade gracefully; document as gap with next‑step suggestion (ingest or enable writer) — no code changes now.

Inconsistent log fields → standardize via Explore queries; use permissive LogQL with | json + null‑tolerant filters.

8) Visual & UX Conventions

Consistent color thresholds (🟢 healthy, 🟡 warning, 🔴 critical).

Dashboard variables: $env, $service, $strategy, $symbol, $group, $gate.

Time defaults: last 24h (Infra/Trading/Strategy), last 7d (Financial).

Panel links for drill‑down (e.g., from Strategy → Trading Ops logs with pre‑filled filters).

Ready to build

This plan is fully executable with your current stack. Proceed with Phase‑0 Reality Check, then implement dashboards in the order: Infra → Trading Ops → Strategy → Financial.Minor Points (Very Minor):



The success criteria for the Strategy Funnel ("signals→cost gate→risk gate→orders→fills") is ambitious and heavily depends on finding gate_snapshots / order_funnel_daily in ClickHouse or very well-structured logs during the "Reality Check". The plan acknowledges this dependency ("Prefer CH tables", "Fallback: Loki counts"), which is good.