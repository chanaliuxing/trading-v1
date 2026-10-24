SFS 高周期派生（1m/5m/…）设计与落地指南

目标
- 在不改变现有 1s Redis 与 5s 滑动入库（sfs.sfs_5s）的前提下，派生出 1m 或更长周期（5m、15m、1h…）的 SFS 数据，用于策略融合、看板与回放。

前置条件（已具备）
- 1s 微bar 环缓存（Redis）：`services/market/sfs_1s_service.py`（键 `sfs:1s:<symbol>`）。
- 5s 滑动入库（1s 步进）：`services/market/sfs_5s_writer.py` 写入 `sfs.sfs_5s`（见 `schema/clickhouse/sfs_5s.sql`）。
- 指标与开关：`config.yml: services.sfs_1s / sfs_5s`，Prometheus 指标见 `doc/dashboard/SFS_1S_MONITORING.md` 与 `doc/dashboard/SFS_5S_MONITORING.md`。

术语与口径
- “SFS 5s”= 滑动 5 秒窗口，1 秒步进（每秒一行），来自最近 5×1s 子bar 的聚合；表：`sfs.sfs_5s`。
- “features_5s”= 5 秒整窗（tumbling）特征，每 5 秒一行，与 SFS 5s 不是一类数据。
- “SFS 1m/5m/…” 可有两种口径：
  - tumbling：按周期边界产出（例如 1m 的 00、01、02…），适合指标与回放；
  - sliding：每秒（或更细步进）产出上一周期的滑窗，适合更细的触发与对齐。

方案 A（推荐）：ClickHouse 物化视图生成 1m tumbling
- 核心思路：从 `sfs.sfs_5s` 以 `toStartOfMinute(ts)` 聚合得到 1m OHLCV + VWAP 等；进一步周期（5m/15m/1h）再从 1m 表聚合，可减少计算链路。

1) 表结构（1m tumbling）
```sql
-- 1m tumbling from sfs.sfs_5s
CREATE TABLE IF NOT EXISTS sfs.sfs_1m_tumble
(
  symbol LowCardinality(String),
  ts     DateTime64(3, 'UTC'),  -- minute start
  open   Float64,
  high   Float64,
  low    Float64,
  close  Float64,
  volume Float64,
  trades UInt32,
  vwap_1m Float64,
  ingest_ts DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(ts)
ORDER BY (symbol, ts)
TTL ts + INTERVAL 365 DAY
SETTINGS index_granularity = 8192;
```

2) 物化视图（5s→1m）
```sql
CREATE MATERIALIZED VIEW IF NOT EXISTS sfs.mv_5s_to_1m_tumble
TO sfs.sfs_1m_tumble
AS
SELECT
  symbol,
  toStartOfMinute(ts) AS ts,
  argMin(open,  ts) AS open,          -- 最早 5s 的 open
  max(high)         AS high,
  min(low)          AS low,
  argMax(close, ts) AS close,         -- 最晚 5s 的 close
  sum(volume)       AS volume,
  sum(trades)       AS trades,
  sum(vwap * volume) / nullIf(sum(volume), 0) AS vwap_1m,
  now64(3)          AS ingest_ts
FROM sfs.sfs_5s
GROUP BY symbol, ts;
```

3) 更长周期（5m/15m/1h）
```sql
-- 从 1m 聚合到 5m tumbling（推荐链式聚合，减少成本）
CREATE TABLE IF NOT EXISTS sfs.sfs_5m_tumble
(
  symbol LowCardinality(String),
  ts     DateTime64(3, 'UTC'),  -- 5m interval start
  open   Float64,
  high   Float64,
  low    Float64,
  close  Float64,
  volume Float64,
  trades UInt32,
  vwap_5m Float64,
  ingest_ts DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(ts)
ORDER BY (symbol, ts)
TTL ts + INTERVAL 730 DAY;

CREATE MATERIALIZED VIEW IF NOT EXISTS sfs.mv_1m_to_5m_tumble
TO sfs.sfs_5m_tumble
AS
SELECT
  symbol,
  toStartOfInterval(ts, INTERVAL 5 MINUTE) AS ts,
  argMin(open,  ts) AS open,
  max(high)         AS high,
  min(low)          AS low,
  argMax(close, ts) AS close,
  sum(volume)       AS volume,
  sum(trades)       AS trades,
  sum(vwap_1m * volume) / nullIf(sum(volume), 0) AS vwap_5m,
  now64(3)          AS ingest_ts
FROM sfs.sfs_1m_tumble
GROUP BY symbol, ts;
```

说明
- `argMin/argMax` 以时间选开收盘，`max/min` 聚合高低，`sum` 聚合量与成交笔数；VWAP 按权重求和。
- 分区与 TTL 可按数据量调整（1m 留 1 年；5m 留 2 年示例）。

方案 B：滑动 1m（每秒产出）
- 适用：需要更细触发（例如每秒检查“过去 1 分钟”路径），与 1m tumbling 不同。
- 实现建议：新增 `services/market/sfs_1m_sliding_writer.py` 从 `sfs.sfs_5s` 每秒读取最近 12 条 5s 滑窗，写入 `sfs.sfs_1m_sliding`。

1) 表结构（滑动 1m）
```sql
CREATE TABLE IF NOT EXISTS sfs.sfs_1m_sliding
(
  symbol LowCardinality(String),
  ts     DateTime64(3, 'UTC'),  -- 窗口结束时刻（每秒一条）
  open   Float64,
  high   Float64,
  low    Float64,
  close  Float64,
  volume Float64,
  trades UInt32,
  vwap_1m Float64,
  ingest_ts DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree
PARTITION BY toYYYYMMDD(ts)
ORDER BY (symbol, ts)
TTL ts + INTERVAL 90 DAY;
```

2) Writer 逻辑（与 5s writer 相同思路）：
```text
for each second:
  for sym in symbols:
    rows = SELECT last 12 rows FROM sfs.sfs_5s ORDER BY ts DESC
    compute 1m OHLCV, vwap
    INSERT INTO sfs.sfs_1m_sliding
```

对比与取舍
- tumbling（MV）：低成本、CH 自动计算，适合大多数看板与策略；
- sliding（writer）：更细粒度，但额外服务与写入量增加（约等于 1s×symbol），谨慎开启（可仅对白名单标的）。

回填（Backfill）
- 1m tumbling：
```sql
INSERT INTO sfs.sfs_1m_tumble
SELECT
  symbol,
  toStartOfMinute(ts) AS ts,
  argMin(open, ts)  AS open,
  max(high)         AS high,
  min(low)          AS low,
  argMax(close, ts) AS close,
  sum(volume)       AS volume,
  sum(trades)       AS trades,
  sum(vwap * volume) / nullIf(sum(volume), 0) AS vwap_1m,
  now64(3)          AS ingest_ts
FROM sfs.sfs_5s
WHERE ts >= toDateTime('2025-10-10 09:30:00')
  AND ts <  toDateTime('2025-10-10 16:00:00')
GROUP BY symbol, ts;
```
- 更长周期（5m/15m…）：改 `toStartOfInterval(ts, INTERVAL X MINUTE)` + 目标表即可。

监控与看板
- 写入速率/延迟：复用 `sfs5s_rows_written_total` 与新增的 1m/sliding writer 指标；
- 表体量：使用 ClickHouse `system.parts` 监控 `rows/bytes`；参考 `doc/dashboard/SFS_5S_MONITORING.md` 的查询模板。

最佳实践
- 用 `sfs.sfs_5s` 作为唯一“原子层”，所有更高周期**尽量用 CH MV 链式聚合**，减少重复计算与一致性风险；
- 若对 VWAP 精度敏感，可在 `sfs.sfs_5s` 中增加 `vwap_num/vwap_den` 两列（精确聚合），当前以 `vwap*volume` 近似亦可；
- RTH 过滤（如需）可在 MV 内新增 `WHERE toHour(toTimeZone(ts,'America/New_York')) BETWEEN 9 AND 15` 等条件；
- TTL/保留策略按业务需求调优：1m（≥1年），5m（≥2年），sliding 1m（90 天内）。

落地步骤（建议）
1. 启用/稳定 `sfs.sfs_5s`（小范围白名单 → 全量）；
2. 部署 1m tumbling 表与 MV（低成本、优先上线）；
3. 需要更细对齐时，再按白名单启用 1m sliding writer；
4. 补充 Grafana 面板（写入速率、延迟、表 rows/bytes，异常错误计数）。

