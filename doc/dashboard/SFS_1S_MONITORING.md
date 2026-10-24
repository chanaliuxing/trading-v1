SFS-1s Monitoring (Prometheus/Grafana)

Overview
- Metrics exported via the existing Prometheus HTTP server (port defined by `PROMETHEUS_PORT`, default 9108).
- Service: `services/market/sfs_1s_service.py`
- Default labels: `{symbol}` where applicable

Metrics
- `sfs1s_ticks_total{symbol}` — processed tick messages for 1s aggregation
- `sfs1s_bars_flushed_total{symbol}` — flushed 1s bars written to Redis
- `sfs1s_redis_errors_total` — Redis write failures
- `sfs1s_flush_latency_ms_bucket|sum|count` — histogram for flush latency
- `sfs1s_ring_len{symbol}` — current Redis list length per symbol

PromQL Examples
- Tick rate by symbol (1m):
  - `sum by (symbol) (rate(sfs1s_ticks_total[1m]))`
- Bars flushed rate by symbol (1m):
  - `sum by (symbol) (rate(sfs1s_bars_flushed_total[1m]))`
- Redis errors (5m increase):
  - `increase(sfs1s_redis_errors_total[5m])`
- P95 flush latency (5m):
  - `histogram_quantile(0.95, sum(rate(sfs1s_flush_latency_ms_bucket[5m])) by (le))`
- Ring length (current):
  - `sfs1s_ring_len`

Grafana Panels (Suggestions)
- Stat: Total tick rate (sum over all symbols)
  - Query: `sum(rate(sfs1s_ticks_total[1m]))`
- Table: Top-N symbols by ring length
  - Query: `topk(10, sfs1s_ring_len)`
- Time series: Flush latency P95
  - Query: `histogram_quantile(0.95, sum(rate(sfs1s_flush_latency_ms_bucket[$__interval])) by (le))`
- Stat: Redis errors (5m)
  - Query: `increase(sfs1s_redis_errors_total[5m])`

Enable in config.yml
```yaml
services:
  sfs_1s:
    enabled: true
    ttl_sec: 3600
    flush_delay_ms: 250
```

Notes
- Ensure Prometheus job scrapes the app where the HTTP server (`start_metrics_http_server`) runs (already enabled in run_system.py).
- Avoid overly aggressive panels on `sfs1s_ring_len` if universe is large; filter by `$symbol`.

