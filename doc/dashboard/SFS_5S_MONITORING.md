SFS-5s Monitoring (Prometheus/Grafana)

Metrics (exported by services/market/sfs_5s_writer.py)
- `sfs5s_rows_written_total{group}` — rows inserted to `sfs.sfs_5s`
- `sfs5s_errors_total{stage,group}` — error counters by stage (`redis`|`clickhouse`)
- `sfs5s_flush_latency_ms_bucket|sum|count` — histogram for per-cycle latency

PromQL Examples
- Write rate per minute:
  - `rate(sfs5s_rows_written_total[1m])`
- Errors over 5 minutes by stage:
  - `sum by (stage) (increase(sfs5s_errors_total[5m]))`
- Flush latency p95 (panel interval):
  - `histogram_quantile(0.95, sum(rate(sfs5s_flush_latency_ms_bucket[$__interval])) by (le))`

ClickHouse Table Size Panels (Grafana ClickHouse datasource)
- Rows (recent):
  - `SELECT toStartOfInterval(max_time, INTERVAL $__interval SECOND) AS t, sum(rows) AS rows FROM system.parts WHERE database='sfs' AND table='sfs_5s' AND active GROUP BY t ORDER BY t`
- Bytes:
  - `SELECT toStartOfInterval(max_time, INTERVAL $__interval SECOND) AS t, sum(bytes) AS bytes FROM system.parts WHERE database='sfs' AND table='sfs_5s' AND active GROUP BY t ORDER BY t`

Suggested Panels
- Time series: `rate(sfs5s_rows_written_total[1m])` (write throughput)
- Stat: `increase(sfs5s_errors_total[5m])`
- Time series: p95 flush latency
- Stat/Table: system.parts rows/bytes for `sfs.sfs_5s`

Enable Services
```yaml
services:
  sfs_1s:
    enabled: true
  sfs_5s:
    enabled: true
    flush_period_ms: 1000
    lookback_n: 5
```

