# 双大屏落地 v2（精简可执行版）

> 目标：在不重构业务逻辑的前提下，最短路径点亮两套大屏。把**事实/统计**放 ClickHouse、**健康/SLA**放 Prometheus、**诊断/审计**放 Loki；统一事件契约与字段口径；全链路 trace 可串起“为什么没触发”。

---

## 0. 本版相较原稿的关键取舍

* ✅ **数据分层归位**：PnL/持仓/订单漏斗 → **ClickHouse**；SLA/CPU/Lag/延迟分布 → **Prometheus**；“为何没触发/拒单原因/异常” → **Loki**。避免在 Loki 做重聚合。
* ✅ **统一事件契约 + trace**：6 类核心事件 + `trace_id`/`span_id`。`gate_snapshot` 补充 `threshold_version` / `feature_hash`。
* ✅ **非阻塞采集**：应用侧写入一律**排队批量 flush**，并**默认仅记录 Gate 失败**（通过按采样）。
* ✅ **高并发表设计**：ClickHouse 改为 `ReplacingMergeTree`/`AggregatingMergeTree` + TTL + 轻量索引；漏斗用 MV 聚合，查询 1 秒返回。
* ✅ **SLO 一次定清**：Tick→Signal P95 ≤ 300ms；Signal→Order P95 ≤ 150ms；Order→Fill P95 ≤ 2s；关键 topic lag ≤ 50。

---

## 1) 事件契约（events.md 摘要 v1.1）

> 标签（labels）只保留低基数：`job`, `service_name`, `event`, （可选）`strategy_id`；**禁止**把 `symbol`/`order_id` 放 label，放到日志体。

### 共通字段

* `event` (String)：事件名（见下）
* `ts` (ISO8601 / ms)
* `trace_id` (String)，`span_id` (可选)

### 1. position_snapshot

* `account_id`, `symbol`, `qty`, `avg_px`, `last_px`, `unreal_pnl`, `unreal_pnl_pct`, `vwap_delta`, `risk_bucket`

### 2. account_snapshot

* `account_id`, `equity`, `buying_power`, `margin_used_pct`, `day_pnl`, `day_mdd`, `core_pct`, `t_pct`, `hedge_pct`

### 3. gate_snapshot

* `strategy_id`, `symbol`, `gate` ∈ {A,B,C}, `passed` (bool), `threshold`, `value`, `reason`,
* **新增**：`threshold_version`, `feature_hash`, `decider_version`

### 4. order_lifecycle

* `order_id`, `strategy_id`, `symbol`, `side`, `qty`, `px`, `stage` ∈ {created,risk_ok,risk_fail,submitted,filled,partially_filled,rejected,cancelled}, `venue`, `code`, `msg`, `latency_ms`

### 5. backfill_status

* `symbol`, `resolution`, `expected_rows`, `actual_rows`, `gap`, `last_ok_ts`, `last_err`

### 6. market_leaders（可选落 CH）

* `window`, `leaders`（数组：`symbol`, `chg_pct`, `rvol_5m`, `vwap_delta`, `atr`）

---

## 2) ClickHouse DDL（observability_schema.sql v2）

> 数据库：`trading`。压缩：ZSTD。保留期：7–30 天（可配）。

```sql
CREATE DATABASE IF NOT EXISTS trading;

-- 账户权益 & PnL（高频事实）
CREATE TABLE IF NOT EXISTS trading.equity_pnl_snapshots
(
  ts                  DateTime64(3, 'UTC') CODEC(Delta, ZSTD),
  account_id          String CODEC(ZSTD),
  equity              Float64 CODEC(T64, ZSTD),
  buying_power        Float64 CODEC(T64, ZSTD),
  margin_used_pct     Float64 CODEC(T64, ZSTD),
  day_pnl             Float64 CODEC(T64, ZSTD),
  day_mdd             Float64 CODEC(T64, ZSTD),
  core_pct            Float64 CODEC(T64, ZSTD),
  t_pct               Float64 CODEC(T64, ZSTD),
  hedge_pct           Float64 CODEC(T64, ZSTD),
  ver                 UInt64  DEFAULT toUnixTimestamp64Milli(ts),
  trace_id            String CODEC(ZSTD)
)
ENGINE = ReplacingMergeTree(ver)
PARTITION BY toYYYYMMDD(ts)
ORDER BY (account_id, ts)
TTL toStartOfDay(ts) + INTERVAL 14 DAY;

-- 持仓快照（高频事实）
CREATE TABLE IF NOT EXISTS trading.positions_snapshots
(
  ts              DateTime64(3, 'UTC') CODEC(Delta, ZSTD),
  account_id      String CODEC(ZSTD),
  symbol          String CODEC(ZSTD),
  qty             Int64  CODEC(T64, ZSTD),
  avg_px          Float64 CODEC(T64, ZSTD),
  last_px         Float64 CODEC(T64, ZSTD),
  unreal_pnl      Float64 CODEC(T64, ZSTD),
  unreal_pnl_pct  Float64 CODEC(T64, ZSTD),
  vwap_delta      Float64 CODEC(T64, ZSTD),
  risk_bucket     LowCardinality(String) CODEC(ZSTD),
  ver             UInt64  DEFAULT toUnixTimestamp64Milli(ts),
  trace_id        String CODEC(ZSTD)
)
ENGINE = ReplacingMergeTree(ver)
PARTITION BY toYYYYMMDD(ts)
ORDER BY (account_id, symbol, ts)
TTL toStartOfDay(ts) + INTERVAL 14 DAY;

-- 订单事件（细粒度事实）
CREATE TABLE IF NOT EXISTS trading.order_events
(
  ts          DateTime64(3, 'UTC') CODEC(Delta, ZSTD),
  order_id    String CODEC(ZSTD),
  strategy_id String CODEC(ZSTD),
  symbol      String CODEC(ZSTD),
  side        LowCardinality(String) CODEC(ZSTD),
  qty         UInt64 CODEC(T64, ZSTD),
  px          Float64 CODEC(T64, ZSTD),
  stage       LowCardinality(String) CODEC(ZSTD),
  venue       LowCardinality(String) CODEC(ZSTD),
  code        String CODEC(ZSTD),
  msg         String CODEC(ZSTD),
  latency_ms  UInt32 CODEC(T64, ZSTD),
  trace_id    String CODEC(ZSTD)
)
ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(ts)
ORDER BY (order_id, ts);

-- Gate 事件（可选：便于漏斗补齐）
CREATE TABLE IF NOT EXISTS trading.gate_snapshots
(
  ts                DateTime64(3, 'UTC'),
  strategy_id       String,
  symbol            String,
  gate              LowCardinality(String),
  passed            UInt8,
  threshold         Float64,
  value             Float64,
  reason            String,
  threshold_version String,
  feature_hash      String,
  decider_version   String,
  trace_id          String
)
ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(ts)
ORDER BY (strategy_id, symbol, ts);

-- 回填完整性（系统驾驶舱）
CREATE TABLE IF NOT EXISTS trading.backfill_status
(
  ts            DateTime64(3, 'UTC'),
  symbol        String,
  resolution    LowCardinality(String),
  expected_rows UInt64,
  actual_rows   UInt64,
  gap           Int64,
  last_ok_ts    DateTime64(3, 'UTC'),
  last_err      String,
  trace_id      String
)
ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(ts)
ORDER BY (symbol, resolution, ts);
```

### 订单漏斗物化视图（改为 AggregatingMergeTree）

```sql
CREATE TABLE IF NOT EXISTS trading.order_funnel_daily
(
  day           Date,
  strategy_id   String,
  symbol        String,
  orders_created     AggregateFunction(count),
  orders_submitted   AggregateFunction(count),
  orders_filled      AggregateFunction(count),
  orders_rejected    AggregateFunction(count),
  signals            AggregateFunction(count),
  candidates         AggregateFunction(count)
)
ENGINE = AggregatingMergeTree()
PARTITION BY day
ORDER BY (day, strategy_id, symbol);

CREATE MATERIALIZED VIEW IF NOT EXISTS trading.mv_order_funnel
TO trading.order_funnel_daily AS
SELECT
  toDate(ts) AS day,
  strategy_id,
  symbol,
  countStateIf(stage = 'created')   AS orders_created,
  countStateIf(stage = 'submitted') AS orders_submitted,
  countStateIf(stage = 'filled')    AS orders_filled,
  countStateIf(stage = 'rejected')  AS orders_rejected,
  countState() AS signals,          -- 预留；或由 gate_snapshots 另建 MV
  countState() AS candidates        -- 预留
FROM trading.order_events
GROUP BY day, strategy_id, symbol;
```

> 查询时使用 `… SELECT day, strategy_id, symbol, countMerge(orders_created) …`。

---

## 3) Prometheus 指标（SLA & 健康）

> 使用 `prometheus_client`：

* Gauges：`service_up{service_name=…}`, `kafka_lag{topic=…}`, `clickhouse_write_latency_p95`（也可 Histogram）
* Histograms：`latency_tick_to_signal`, `latency_signal_to_order`, `latency_order_to_fill`
* Counters：`orders_rejected_total{code=…}`, `errors_total{service=…}`

**Python 片段**（示意）：

```python
from prometheus_client import Gauge, Histogram, Counter, start_http_server

service_up = Gauge('service_up', 'service heartbeat', ['service'])
lat_sig = Histogram('latency_signal_to_order_ms', 'ms', buckets=(10,25,50,100,150,250,500,1000))
orders_rej = Counter('orders_rejected_total', 'rejects', ['code'])

start_http_server(9108)  # /metrics 暴露
```

---

## 4) 采集与写入（应用侧）

### 非阻塞写入约定

* **应用线程**只 `queue.put_nowait()`；**后端 flush 协程**按表批量写入（Loki 批量 / ClickHouse `insert_batch`）。
* 队列满：**丢弃低价值事件**（如通过的 Gate），记录一条 `queue_full` 告警。
* 关机顺序：先停服务 → drain 队列 → 停 flusher。

### 字段与幂等

* CH 表使用 `ReplacingMergeTree(ver)`，防重复写入（同 `account_id,symbol,ts` 的最新 `ver` 生效）。
* `order_events` 可重复写（按 `order_id,ts` 排序即顺序视图）。

---

## 5) 代码骨架（修正 & 强化）

> 仅列出要点与易错修正；具体实现按你代码基线调整。

### 5.1 事件枚举（修正 Enum 引用）

```python
# libs/obs/events.py
from enum import Enum

class EventName(str, Enum):
    POSITION_SNAPSHOT = 'position_snapshot'
    ACCOUNT_SNAPSHOT = 'account_snapshot'
    MARKET_LEADERS   = 'market_leaders'
    GATE_SNAPSHOT    = 'gate_snapshot'
    ORDER_LIFECYCLE  = 'order_lifecycle'
    BACKFILL_STATUS  = 'backfill_status'
    SERVICE_HEARTBEAT= 'service_heartbeat'
    SERVICE_START    = 'service_start'
    SERVICE_STOP     = 'service_stop'
    ERROR            = 'error'
    WARNING          = 'warning'

class OrderLifecycleStage(str, Enum):
    CREATED='created'; RISK_OK='risk_ok'; RISK_FAIL='risk_fail';
    SUBMITTED='submitted'; FILLED='filled'; PARTIALLY_FILLED='partially_filled';
    REJECTED='rejected'; CANCELLED='cancelled'

class Gate(str, Enum):
    A='A'; B='B'; C='C'
```

### 5.2 队列与 Flusher（补全 Enum & dt 引入，分表批量）

```python
# libs/obs/queue.py（摘）
import asyncio, datetime as dt
from enum import Enum
from typing import Dict, Any, List

class Channel(str, Enum):
    LOGS='logs'; CLICKHOUSE='clickhouse'

class ObservabilityQueue:
    ...
    async def _clickhouse_flusher(self, ch_writer, batch_size=500, flush_interval=2):
        while True:
            items = await self._gather_batch(self.queues[Channel.CLICKHOUSE], batch_size, flush_interval)
            if not items: continue
            by_table: Dict[str, List[Dict[str, Any]]] = {}
            for it in items:
                tbl = it.get('_table'); dat = it.get('data')
                if not tbl or not dat: continue
                by_table.setdefault(tbl, []).append(dat)
            for tbl, batch in by_table.items():
                await ch_writer.write_batch(tbl, batch)
```

### 5.3 ServiceBase（心跳 → Prometheus；Loki 仅审计）

```python
# libs/service_base.py（摘）
from prometheus_client import Gauge
service_up = Gauge('service_up', 'service heartbeat', ['service'])
...
async def _heartbeat_loop(self):
    while True:
        service_up.labels(self.service_name).set(1)
        # 可选：也写一条 Loki 审计
        await obs_queue.put(Channel.LOGS, {
            'event': EventName.SERVICE_HEARTBEAT, 'service_name': self.service_name,
            'status':'healthy', 'ts': dt.datetime.now(dt.timezone.utc).isoformat()
        })
        await asyncio.sleep(self.heartbeat_interval)
```

### 5.4 StrategyEngine（只记失败 + 版本/哈希）

```python
async def _log_gate_snapshot(..., passed: bool, details: dict):
    if passed and not self.config.get('log_passed_gates', False):
        return
    ev = {
        'event': EventName.GATE_SNAPSHOT,
        'strategy_id': strategy_id, 'symbol': symbol, 'gate': gate.value,
        'passed': passed, 'threshold': details.get('threshold'), 'value': details.get('value'),
        'reason': details.get('reason'), 'threshold_version': details.get('threshold_version'),
        'feature_hash': details.get('feature_hash'), 'decider_version': details.get('decider_version'),
        'ts': dt.datetime.now(dt.timezone.utc).isoformat(), 'trace_id': trace_id
    }
    await obs_queue.put(Channel.LOGS, ev)
```

### 5.5 Execution（严格测时，映射拒单码）

```python
import time
start = time.perf_counter_ns()
# 提交前后打点 …
lat_ms = (time.perf_counter_ns()-start)//1_000_000
await obs_queue.put(Channel.CLICKHOUSE, {
  '_table':'order_events', 'data':{ 'ts': now, 'order_id': oid, 'strategy_id': sid,
  'symbol': sym, 'side': side, 'qty': qty, 'px': px, 'stage': 'submitted',
  'venue': 'IBKR', 'code': code, 'msg': msg, 'latency_ms': lat_ms, 'trace_id': trace }
})
```

---

## 6) Grafana 查询模板（三源混合）

> 下列为**核心面板的查询**，实现团队据此生成 JSON（或直接用你现有 JSON 替换查询）。

### 大屏一（交易员） – ClickHouse

* **当日盈亏曲线**

```sql
SELECT ts, day_pnl FROM trading.equity_pnl_snapshots WHERE $__timeFilter(ts) ORDER BY ts
```

* **账户总览（Stat）**

```sql
SELECT anyLast(equity) FROM trading.equity_pnl_snapshots WHERE $__timeFilter(ts)
```

* **持仓表**

```sql
SELECT symbol,
       anyLast(qty) AS qty,
       round(anyLast(avg_px),2) AS avg_px,
       round(anyLast(last_px),2) AS last_px,
       round(anyLast(unreal_pnl),2) AS unreal_pnl,
       round(anyLast(vwap_delta),2) AS vwap_delta
FROM trading.positions_snapshots
WHERE $__timeFilter(ts)
GROUP BY symbol
HAVING qty != 0
ORDER BY unreal_pnl DESC
```

### 大屏二（运维） – Prometheus + Loki + ClickHouse

* **链路延迟直方图（Prometheus）**

```
latency_signal_to_order_ms_bucket{job="strategy"}
```

* **为何没触发（Loki 饼图）**

```
sum by (reason) (count_over_time({event="gate_snapshot", passed="false"}[$__range]))
```

* **订单漏斗趋势（CH）**

```sql
SELECT toStartOfInterval(ts, toIntervalMinute(5)) AS t, stage, count() AS c
FROM trading.order_events
WHERE $__timeFilter(ts)
GROUP BY t, stage ORDER BY t
```

* **回填缺口（CH 表格）**

```sql
SELECT symbol, resolution, max(ts) AS last_ts, anyLast(expected_rows) AS exp, anyLast(actual_rows) AS act,
       max(gap) AS gap
FROM trading.backfill_status
WHERE $__timeFilter(ts)
GROUP BY symbol, resolution
HAVING gap > 0
ORDER BY gap DESC
```

---

## 7) 告警矩阵（建议直接导入 Grafana Alerting）

* **P0**：`service_up{service=~"(order|strategy|exec)"} == 0` 持续 30s
* **P0**：`latency_order_to_fill_ms_bucket` P95 > 2000ms 持续 5 分钟
* **P1**：`kafka_lag{topic=~"events.*"} > 50` 持续 10 分钟
* **P1**：ClickHouse 写入失败率 > 0.1%（需在应用侧暴露 Counter）
* **P2**：`count_over_time({event="order_lifecycle", stage="rejected"}[15m]) > 5`

---

## 8) 上线步骤（D1–D3）

1. **部署表结构**：执行本页 DDL（数据库 `trading`）。
2. **接入采集框架**：落地 `obs_queue` 单例、ClickHouse/Loki writer，服务继承 `ServiceBase`。
3. **埋点与透传 trace**：Strategy/Execution/Portfolio 加事件埋点；上下游透传 `trace_id`。
4. **暴露 Prometheus** `/metrics`，Grafana 加数据源（CH/Loki/Prom）。
5. **导入仪表盘**：用“查询模板”生成 JSON 或在现有 JSON 中替换查询。
6. **压测与阈值校准**：观察 1 日数据量与成本；调整 TTL/采样率/桶边界。

---

## 9) 成功判定（回归测试用例）

* 开盘 1min 内：大屏一显示**实时** PnL 曲线与持仓明细；VWAP 偏离与止损距离正确。
* 任意一单从 Signal→Fill：大屏二可从 `trace_id` 追完整生命周期；拒单原因可见。
* Kafka lag 飙升或 ClickHouse 写入异常：5 分钟内触发 P1 告警；
* 关停任一核心服务：30s 内触发 P0 告警。

---

> 备注：如需，我可在下一版提供**完整 Grafana JSON**（三数据源版）与**最小 Python Writer**（clickhouse-connect + Loki HTTP 批量）的可拷贝代码。
我把你的落地方案提炼成一份更稳、更快、更易上线的《**双大屏落地 v2（精简可执行版）**》，已经放到右侧文档里了。重点包括：

* 数据分层归位（CH/Prom/Loki 各司其职）
* 统一的 6 类事件契约（补充 `threshold_version`/`feature_hash`）
* ClickHouse 表 & 漏斗 MV 的更优 DDL
* 非阻塞采集与幂等写入约定
* Prometheus 指标与告警矩阵
* 两套大屏的查询模板（可直接替你 JSON 里的查询）

要我再补一版**三数据源 Grafana 完整 JSON**和**最小 Python Writer（CH/Loki 批量）**吗？我可以直接追加到这个文档里。
# 三数据源 Grafana 完整 JSON + 最小 Python Writer（批量版）

> 说明：本页包含两部分交付物，可直接用于上线。
>
> 1. **Grafana Dashboard**（三数据源：ClickHouse + Loki + Prometheus）
> 2. **最小 Python Writer**：支持 ClickHouse 批量写入（JSONEachRow）与 Loki 批量推送（/loki/api/v1/push）
>
> 约定：事实/统计（PnL、持仓、订单漏斗）→ **ClickHouse**；SLA/健康（直方图、lag、service up）→ **Prometheus**；诊断/审计（gate/订单生命周期/错误）→ **Loki**。

---

## 1) Grafana 完整 JSON（导入即用）

> 将下方 JSON 复制为 `hybrid_trading_overview.json` 并在 Grafana 中 **Import**。
> 需要已配置三种数据源：`ClickHouse`、`Loki`、`Prometheus`。导入时选择对应数据源实例。

```json
{
  "__inputs": [
    { "name": "DS_CH",   "label": "ClickHouse",  "type": "datasource", "pluginId": "grafana-clickhouse-datasource", "pluginName": "ClickHouse" },
    { "name": "DS_LOKI", "label": "Loki",        "type": "datasource", "pluginId": "loki",                          "pluginName": "Loki" },
    { "name": "DS_PROM", "label": "Prometheus",  "type": "datasource", "pluginId": "prometheus",                    "pluginName": "Prometheus" }
  ],
  "__requires": [
    { "type": "grafana", "id": "grafana", "name": "Grafana", "version": "10.0.0" },
    { "type": "datasource", "id": "grafana-clickhouse-datasource", "name": "ClickHouse", "version": "4.0.0" },
    { "type": "datasource", "id": "loki", "name": "Loki", "version": "2.9.0" },
    { "type": "datasource", "id": "prometheus", "name": "Prometheus", "version": "2.0.0" }
  ],
  "annotations": { "list": [ { "builtIn": 1, "datasource": { "type": "grafana", "uid": "-- Grafana --" }, "enable": true, "hide": true, "iconColor": "rgba(0,211,255,1)", "name": "Annotations & Alerts", "type": "dashboard" } ] },
  "editable": true,
  "fiscalYearStartMonth": 0,
  "graphTooltip": 0,
  "links": [],
  "liveNow": false,
  "panels": [
    { "collapsed": false, "gridPos": { "h": 1, "w": 24, "x": 0, "y": 0 }, "id": 1, "panels": [], "title": "交易员实盘驾驶舱 (Trader Cockpit)", "type": "row" },

    { "type": "stat", "title": "总资产 (Equity)", "id": 2, "gridPos": { "h": 5, "w": 5, "x": 0, "y": 1 },
      "datasource": { "type": "grafana-clickhouse-datasource", "uid": "${DS_CH}" },
      "fieldConfig": { "defaults": { "unit": "currencyUSD", "decimals": 2 } },
      "targets": [ { "refId": "A", "query": "SELECT anyLast(equity) FROM trading.equity_pnl_snapshots WHERE $__timeFilter(ts)" } ] },

    { "type": "stat", "title": "当日盈亏 (Daily P&L)", "id": 3, "gridPos": { "h": 5, "w": 5, "x": 5, "y": 1 },
      "datasource": { "type": "grafana-clickhouse-datasource", "uid": "${DS_CH}" },
      "fieldConfig": { "defaults": { "unit": "currencyUSD", "decimals": 2 } },
      "targets": [ { "refId": "A", "query": "SELECT anyLast(day_pnl) FROM trading.equity_pnl_snapshots WHERE $__timeFilter(ts)" } ] },

    { "type": "stat", "title": "可用资金 (Buying Power)", "id": 4, "gridPos": { "h": 5, "w": 5, "x": 10, "y": 1 },
      "datasource": { "type": "grafana-clickhouse-datasource", "uid": "${DS_CH}" },
      "fieldConfig": { "defaults": { "unit": "currencyUSD", "decimals": 2 } },
      "targets": [ { "refId": "A", "query": "SELECT anyLast(buying_power) FROM trading.equity_pnl_snapshots WHERE $__timeFilter(ts)" } ] },

    { "type": "stat", "title": "风险灯 (Margin %)", "id": 5, "gridPos": { "h": 5, "w": 4, "x": 15, "y": 1 },
      "datasource": { "type": "grafana-clickhouse-datasource", "uid": "${DS_CH}" },
      "fieldConfig": { "defaults": { "unit": "percent", "decimals": 0 } },
      "targets": [ { "refId": "A", "query": "SELECT anyLast(margin_used_pct) FROM trading.equity_pnl_snapshots WHERE $__timeFilter(ts)" } ] },

    { "type": "stat", "title": "当日订单数", "id": 6, "gridPos": { "h": 5, "w": 5, "x": 19, "y": 1 },
      "datasource": { "type": "grafana-clickhouse-datasource", "uid": "${DS_CH}" },
      "targets": [ { "refId": "A", "query": "SELECT countDistinct(order_id) FROM trading.order_events WHERE $__timeFilter(ts)" } ] },

    { "type": "timeseries", "title": "今日盈亏曲线 (P&L Curve)", "id": 7, "gridPos": { "h": 10, "w": 12, "x": 0, "y": 6 },
      "datasource": { "type": "grafana-clickhouse-datasource", "uid": "${DS_CH}" },
      "targets": [ { "refId": "A", "query": "SELECT ts, day_pnl FROM trading.equity_pnl_snapshots WHERE $__timeFilter(ts) ORDER BY ts" } ] },

    { "type": "table", "title": "当前持仓 (Holdings)", "id": 8, "gridPos": { "h": 10, "w": 12, "x": 12, "y": 6 },
      "datasource": { "type": "grafana-clickhouse-datasource", "uid": "${DS_CH}" },
      "fieldConfig": { "defaults": { "custom": { "align": "auto" } } },
      "targets": [ { "refId": "A", "query": "SELECT symbol, anyLast(qty) AS qty, round(anyLast(avg_px),2) AS avg_px, round(anyLast(last_px),2) AS last_px, round(anyLast(unreal_pnl),2) AS unreal_pnl, round(anyLast(vwap_delta),2) AS vwap_delta FROM trading.positions_snapshots WHERE $__timeFilter(ts) GROUP BY symbol HAVING qty != 0 ORDER BY unreal_pnl DESC" } ] },

    { "collapsed": false, "gridPos": { "h": 1, "w": 24, "x": 0, "y": 16 }, "id": 20, "panels": [], "title": "系统 & 策略运维驾驶舱 (Ops Cockpit)", "type": "row" },

    { "type": "timeseries", "title": "服务心跳 (service_up)", "id": 21, "gridPos": { "h": 6, "w": 8, "x": 0, "y": 17 },
      "datasource": { "type": "prometheus", "uid": "${DS_PROM}" },
      "targets": [ { "refId": "A", "expr": "service_up{service=~\"$service\"}" } ],
      "options": { "legend": { "displayMode": "table", "placement": "right" } } },

    { "type": "timeseries", "title": "Signal→Order P95 (ms)", "id": 22, "gridPos": { "h": 6, "w": 8, "x": 8, "y": 17 },
      "datasource": { "type": "prometheus", "uid": "${DS_PROM}" },
      "targets": [ { "refId": "A", "expr": "histogram_quantile(0.95, sum(rate(latency_signal_to_order_ms_bucket[$__rate_interval])) by (le))" } ] },

    { "type": "timeseries", "title": "Order→Fill P95 (ms)", "id": 23, "gridPos": { "h": 6, "w": 8, "x": 16, "y": 17 },
      "datasource": { "type": "prometheus", "uid": "${DS_PROM}" },
      "targets": [ { "refId": "A", "expr": "histogram_quantile(0.95, sum(rate(latency_order_to_fill_ms_bucket[$__rate_interval])) by (le))" } ] },

    { "type": "piechart", "title": "为什么没触发？(Gate Failures)", "id": 24, "gridPos": { "h": 8, "w": 10, "x": 0, "y": 23 },
      "datasource": { "type": "loki", "uid": "${DS_LOKI}" },
      "targets": [ { "refId": "A", "expr": "sum by (reason) (count_over_time({event=\"gate_snapshot\", passed=\"false\"}[$__range]))" } ] },

    { "type": "timeseries", "title": "订单漏斗趋势 (by stage)", "id": 25, "gridPos": { "h": 8, "w": 14, "x": 10, "y": 23 },
      "datasource": { "type": "grafana-clickhouse-datasource", "uid": "${DS_CH}" },
      "targets": [ { "refId": "A", "query": "SELECT toStartOfInterval(ts, toIntervalMinute(5)) AS t, stage, count() AS c FROM trading.order_events WHERE $__timeFilter(ts) GROUP BY t, stage ORDER BY t" } ] },

    { "type": "table", "title": "回填缺口 (Backfill Gaps)", "id": 26, "gridPos": { "h": 7, "w": 12, "x": 0, "y": 31 },
      "datasource": { "type": "grafana-clickhouse-datasource", "uid": "${DS_CH}" },
      "targets": [ { "refId": "A", "query": "SELECT symbol, resolution, max(ts) AS last_ts, anyLast(expected_rows) AS exp, anyLast(actual_rows) AS act, max(gap) AS gap FROM trading.backfill_status WHERE $__timeFilter(ts) GROUP BY symbol, resolution HAVING gap > 0 ORDER BY gap DESC" } ] },

    { "type": "logs", "title": "订单生命周期追踪 (按 trace_id)", "id": 27, "gridPos": { "h": 7, "w": 12, "x": 12, "y": 31 },
      "datasource": { "type": "loki", "uid": "${DS_LOKI}" },
      "options": { "enableLogDetails": true, "prettifyLogMessage": true, "showLabels": true, "wrapLogMessage": true },
      "targets": [ { "refId": "A", "expr": "{event=\"order_lifecycle\"} | json | trace_id=\"$trace_id\"" } ] }
  ],
  "refresh": "10s",
  "schemaVersion": 38,
  "style": "dark",
  "tags": ["trading", "hybrid", "ops"],
  "templating": { "list": [
    { "name": "DS_CH",   "type": "datasource", "label": "ClickHouse", "query": "grafana-clickhouse-datasource" },
    { "name": "DS_LOKI", "type": "datasource", "label": "Loki",       "query": "loki" },
    { "name": "DS_PROM", "type": "datasource", "label": "Prometheus", "query": "prometheus" },

    { "name": "service",   "type": "query", "label": "Service",   "datasource": { "type": "prometheus", "uid": "${DS_PROM}" },
      "query": "label_values(service_up, service)", "refresh": 2, "hide": 0 },

    { "name": "strategy",  "type": "query", "label": "Strategy",  "datasource": { "type": "grafana-clickhouse-datasource", "uid": "${DS_CH}" },
      "query": "SELECT DISTINCT strategy_id FROM trading.order_events WHERE $__timeFilter(ts) ORDER BY strategy_id", "refresh": 2, "hide": 0 },

    { "name": "symbol",    "type": "query", "label": "Symbol",    "datasource": { "type": "grafana-clickhouse-datasource", "uid": "${DS_CH}" },
      "query": "SELECT DISTINCT symbol FROM trading.order_events WHERE $__timeFilter(ts) ORDER BY symbol", "refresh": 2, "hide": 0 },

    { "name": "trace_id",  "type": "query", "label": "Trace ID",  "datasource": { "type": "loki", "uid": "${DS_LOKI}" },
      "query": "label_values({event=\"order_lifecycle\"}, trace_id)", "refresh": 2, "hide": 0 }
  ] },
  "time": { "from": "now/d", "to": "now" },
  "timepicker": {},
  "timezone": "browser",
  "title": "混合全功能大屏 (ClickHouse + Loki + Prometheus)",
  "uid": "hybrid-trading-3ds",
  "version": 1
}
```

---

## 2) 最小 Python Writer（ClickHouse & Loki 批量）

> 放置路径建议：`services/obs/writers.py`。
> 依赖：`aiohttp`, `orjson`（可改用 `json`），Python ≥ 3.9。

```python
# services/obs/writers.py
import asyncio
import datetime as dt
import os
from typing import Dict, List, Any, Tuple

import aiohttp
try:
    import orjson as json
except Exception:  # fallback
    import json

# --------------------------
# ClickHouse Batch Writer
# --------------------------
class ClickHouseWriter:
    """Minimal async writer using HTTP interface + JSONEachRow."""
    def __init__(self,
                 base_url: str,
                 database: str = "trading",
                 user: str | None = None,
                 password: str | None = None,
                 timeout: int = 10):
        self.base_url = base_url.rstrip("/")
        self.database = database
        self.auth = aiohttp.BasicAuth(user, password) if user else None
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    async def start(self):
        if not self._session:
            self._session = aiohttp.ClientSession(timeout=self.timeout)

    async def close(self):
        if self._session:
            await self._session.close()
            self._session = None

    async def write_batch(self, table: str, rows: List[Dict[str, Any]]):
        if not rows:
            return
        if not self._session:
            await self.start()
        # build payload for JSONEachRow (newline-delimited)
        payload_lines = []
        for r in rows:
            # ClickHouse expects RFC3339 for DateTime64 via JSONEachRow (or numbers)
            # You can pre-convert dt objects to isoformat
            def default(o):
                if isinstance(o, (dt.datetime,)):
                    if o.tzinfo is None:
                        o = o.replace(tzinfo=dt.timezone.utc)
                    return o.isoformat()
                return str(o)
            if json.__name__ == 'orjson':
                payload_lines.append(json.dumps(r).decode())
            else:
                payload_lines.append(json.dumps(r, default=default))
        payload = "\n".join(payload_lines) + "\n"
        query = f"INSERT INTO {self.database}.{table} FORMAT JSONEachRow"
        url = f"{self.base_url}/?database={self.database}&query="
        async with self._session.post(url, data=query + "\n" + payload, auth=self.auth) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"CH insert failed: {resp.status} - {text}")

# --------------------------
# Loki Batch Writer
# --------------------------
class LokiWriter:
    """Minimal async Loki writer with JSON streams."""
    def __init__(self, loki_url: str, default_job: str = "trader", default_service: str = "app", timeout: int = 10):
        self.loki_url = loki_url.rstrip("/")
        self.default_job = default_job
        self.default_service = default_service
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    async def start(self):
        if not self._session:
            self._session = aiohttp.ClientSession(timeout=self.timeout)

    async def close(self):
        if self._session:
            await self._session.close()
            self._session = None

    @staticmethod
    def _ns(ts: Any) -> str:
        if ts is None:
            return str(int(dt.datetime.now(dt.timezone.utc).timestamp() * 1_000_000_000))
        if isinstance(ts, (int, float)):
            # assume seconds
            return str(int(float(ts) * 1_000_000_000))
        if isinstance(ts, dt.datetime):
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=dt.timezone.utc)
            return str(int(ts.timestamp() * 1_000_000_000))
        # iso8601 string
        try:
            parsed = dt.datetime.fromisoformat(str(ts).replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            return str(int(parsed.timestamp() * 1_000_000_000))
        except Exception:
            return str(int(dt.datetime.now(dt.timezone.utc).timestamp() * 1_000_000_000))

    async def write_batch(self, events: List[Dict[str, Any]]):
        if not events:
            return
        if not self._session:
            await self.start()
        # group by low-cardinality label set
        streams: Dict[Tuple[str, str, str], List[List[str]]] = {}
        for ev in events:
            labels = {
                'job': ev.get('job', self.default_job),
                'service_name': ev.get('service_name', self.default_service),
                'event': ev.get('event', 'log')
            }
            # DO NOT put high-cardinality ids into labels (e.g., symbol, order_id)
            key = (labels['job'], labels['service_name'], labels['event'])
            ts_ns = self._ns(ev.get('ts'))
            # store full JSON in line
            line = json.dumps(ev).decode() if json.__name__ == 'orjson' else json.dumps(ev)
            streams.setdefault(key, []).append([ts_ns, line])
        payload = {
            'streams': [
                { 'stream': { 'job': k[0], 'service_name': k[1], 'event': k[2] }, 'values': v }
                for k, v in streams.items()
            ]
        }
        url = f"{self.loki_url}/loki/api/v1/push"
        async with self._session.post(url, json=payload) as resp:
            if resp.status not in (200, 204):
                text = await resp.text()
                raise RuntimeError(f"Loki push failed: {resp.status} - {text}
{payload}")

# --------------------------
# Quick demo / manual test
# --------------------------
async def _demo():
    ch = ClickHouseWriter(base_url=os.getenv('CH_URL', 'http://localhost:8123'),
                          database=os.getenv('CH_DB', 'trading'),
                          user=os.getenv('CH_USER', None),
                          password=os.getenv('CH_PASS', None))
    loki = LokiWriter(loki_url=os.getenv('LOKI_URL', 'http://localhost:3100'))
    await ch.start(); await loki.start()

    now = dt.datetime.now(dt.timezone.utc)

    # ClickHouse: insert two rows
    await ch.write_batch('equity_pnl_snapshots', [
        { 'ts': now, 'account_id': 'DU123', 'equity': 1_000_000, 'buying_power': 500_000,
          'margin_used_pct': 35.0, 'day_pnl': 1200.5, 'day_mdd': -800.2, 'core_pct': 0.6,
          't_pct': 0.3, 'hedge_pct': 0.1, 'trace_id': 'demo-trace-1' }
    ])

    # Loki: push two events
    await loki.write_batch([
        { 'event': 'service_heartbeat', 'service_name': 'PortfolioService', 'status': 'healthy', 'ts': now.isoformat(), 'trace_id': 'demo-trace-1' },
        { 'event': 'order_lifecycle', 'service_name': 'ExecService', 'order_id': 'OID123', 'stage': 'submitted', 'symbol': 'AAPL', 'qty': 100, 'px': 175.2, 'ts': now.isoformat(), 'trace_id': 'demo-trace-1' }
    ])

    await ch.close(); await loki.close()

if __name__ == '__main__':
    asyncio.run(_demo())
```

### 使用建议

* **ClickHouse**：建议将 `DateTime` 字段统一写入 **UTC** ISO8601（或数值 epoch），并在表 DDL 中指定 `DateTime64(3, 'UTC')`。
* **Loki**：`labels` 只包含 `job|service_name|event`（低基数）；高基数字段（如 `symbol|order_id|trace_id`）放日志体。
* **批量大小**：建议 `100~1000`；失败重试时务必做好幂等（快照表用 `ReplacingMergeTree`，事件表允许重复）。
* **关停顺序**：先停业务服务 → drain 队列 → 关闭 writers，避免数据丢失。

---

## 3) 与队列对接（示例）

> 若你使用了我之前的 `obs_queue`，只需要把这两个 writer 实例传进去即可：

```python
# run_system.py (片段)
obs_queue.start_flushers(loki_writer=loki_writer, ch_writer=ch_writer)
# Loki flusher 期望：write_batch(events: List[dict])
# CH flusher   期望：write_batch(table: str, rows: List[dict])
```

---

## 4) 依赖与环境

```bash
pip install aiohttp orjson
# Prometheus 暴露在你的服务里使用 prometheus_client（非本文件范畴）
```

环境变量（仅示例）：

```
CH_URL=http://localhost:8123
CH_DB=trading
CH_USER=default
CH_PASS=
LOKI_URL=http://localhost:3100
```

---

## 5) 验证步骤（5 分钟冒烟）

1. 执行 CH DDL，确保 `trading.equity_pnl_snapshots` 存在。
2. 运行 `writers.py`（`python services/obs/writers.py`）写入一行 CH、推送两条 Loki。
3. Grafana：导入本 JSON，选择数据源；在“P&L 曲线”看到一条点，在“订单生命周期追踪”能搜到 demo 日志。
4. 接入真实服务后，面板应每 10 秒刷新，一天内观察查询时延与成本，按需调整 TTL/采样与分辨率。
已把**三数据源 Grafana 完整 JSON**和**最小 Python Writer（ClickHouse/Loki 批量）**放到右侧文档里了，可直接复制导入/落地。
好的！下面给你一套**即插即用**的 Prometheus `/metrics` 暴露样例，覆盖 **Histogram + Gauge + Counter**，并给出两种集成方式（独立端口 vs. 挂到现有 ASGI 框架）。命名、单位与我们之前的 Grafana 查询保持一致（**毫秒 ms**）。

---

# 1) 指标导出模块（放到 `services/metrics/exporter.py`）

```python
# services/metrics/exporter.py
from __future__ import annotations
import time
import asyncio
from typing import Callable, Awaitable, Optional, Dict

from prometheus_client import (
    Counter, Gauge, Histogram,
    start_http_server, CollectorRegistry, REGISTRY, make_asgi_app
)

# ============ 建议的桶（毫秒） ============
MS_BUCKETS_FAST   = (5, 10, 25, 50, 100, 150, 250, 500, 1000)                      # 0–1s
MS_BUCKETS_SLOW   = (10, 25, 50, 100, 150, 250, 500, 1000, 2000, 5000, 10000)      # 0–10s
MS_BUCKETS_DB     = (1, 2, 5, 10, 20, 50, 100, 200, 500)                           # DB 写入

# ============ 指标定义 ============
# 健康/状态
service_up = Gauge(
    "service_up",
    "Service heartbeat: 1=up, 0=down",
    labelnames=("service",)
)

kafka_lag = Gauge(
    "kafka_topic_lag",
    "Kafka consumer lag (records behind the head)",
    labelnames=("group", "topic", "partition")
)

# 关键链路延迟（毫秒）
latency_tick_to_signal_ms = Histogram(
    "latency_tick_to_signal_ms",
    "End-to-end latency from market tick ingestion to signal (ms)",
    buckets=MS_BUCKETS_FAST
)

latency_signal_to_order_ms = Histogram(
    "latency_signal_to_order_ms",
    "End-to-end latency from signal emitted to order submitted (ms)",
    buckets=MS_BUCKETS_FAST
)

latency_order_to_fill_ms = Histogram(
    "latency_order_to_fill_ms",
    "End-to-end latency from order submitted to fill (ms)",
    buckets=MS_BUCKETS_SLOW
)

# 数据通道/存储
clickhouse_write_latency_ms = Histogram(
    "clickhouse_write_latency_ms",
    "Batch write latency to ClickHouse (ms)",
    buckets=MS_BUCKETS_DB,
    labelnames=("table",)
)

# 业务计数
orders_rejected_total = Counter(
    "orders_rejected_total",
    "Total rejected orders",
    labelnames=("code", "venue", "strategy")
)

errors_total = Counter(
    "errors_total",
    "Unhandled errors",
    labelnames=("service", "etype")
)

# ============ 暴露方式 1：独立端口（最简单） ============
def start_metrics_http_server(port: int = 9108, addr: str = "0.0.0.0") -> None:
    """
    在独立线程启动 /metrics HTTP 端口。
    """
    start_http_server(port, addr=addr)

# ============ 暴露方式 2：挂到 ASGI（FastAPI/Starlette 可用） ============
def get_asgi_metrics_app():
    """
    返回一个可挂载到 /metrics 的 ASGI 应用：
      from fastapi import FastAPI
      from services.metrics.exporter import get_asgi_metrics_app
      app = FastAPI()
      app.mount("/metrics", get_asgi_metrics_app())
    """
    return make_asgi_app()

# ============ 小工具：计时器（同步/异步） ============
class MsTimer:
    """上下文管理器：以 ms 记录 histogram"""
    def __init__(self, hist: Histogram, labels: Optional[Dict[str, str]] = None):
        self.hist = hist
        self.labels = labels or {}
        self._start_ns = 0

    def __enter__(self):
        self._start_ns = time.perf_counter_ns()
        return self

    def __exit__(self, exc_type, exc, tb):
        dur_ms = (time.perf_counter_ns() - self._start_ns) / 1_000_000
        self.hist.labels(**self.labels).observe(dur_ms) if self.labels else self.hist.observe(dur_ms)

def time_async(hist: Histogram, labels: Optional[Dict[str, str]] = None):
    """
    装饰器：测量 async 函数耗时并 observe(ms)
    用法：
      @time_async(latency_signal_to_order_ms)
      async def route_signal(...): ...
    或带标签：
      @time_async(clickhouse_write_latency_ms, {"table":"order_events"})
    """
    labels = labels or {}
    def deco(fn: Callable[..., Awaitable]):
        async def wrapper(*args, **kwargs):
            start = time.perf_counter_ns()
            try:
                return await fn(*args, **kwargs)
            finally:
                dur_ms = (time.perf_counter_ns() - start) / 1_000_000
                hist.labels(**labels).observe(dur_ms) if labels else hist.observe(dur_ms)
        return wrapper
    return deco

# ============ 方便调用的 API ============
def mark_service_up(service_name: str, up: bool = True) -> None:
    service_up.labels(service_name).set(1 if up else 0)

def inc_rejected(code: str, venue: str, strategy: str) -> None:
    orders_rejected_total.labels(code, venue, strategy).inc()

def inc_error(service: str, etype: str) -> None:
    errors_total.labels(service, etype).inc()

def observe_kafka_lag(group: str, topic: str, partition: int, lag_value: int) -> None:
    kafka_lag.labels(group, topic, str(partition)).set(lag_value)

async def observe_ch_batch(table: str, coro: Awaitable):
    """
    包裹一次 ClickHouse 批量写：await observe_ch_batch('positions_snapshots', ch_writer.write_batch(...))
    """
    start = time.perf_counter_ns()
    try:
        return await coro
    finally:
        dur_ms = (time.perf_counter_ns() - start) / 1_000_000
        clickhouse_write_latency_ms.labels(table).observe(dur_ms)
```

---

# 2) 在你的服务里如何用

## 2.1 独立端口方式（最快上手）

```python
# run_system.py（片段）
from services.metrics.exporter import start_metrics_http_server, mark_service_up

def boot():
    # 1) 启动 /metrics 端口（默认 9108）
    start_metrics_http_server(port=9108)

    # 2) 服务启动时标记 up
    mark_service_up("StrategyEngine", True)
    mark_service_up("ExecService", True)
    # ...
```

## 2.2 挂到 FastAPI/Starlette

```python
# app.py
from fastapi import FastAPI
from services.metrics.exporter import get_asgi_metrics_app, mark_service_up

app = FastAPI()
app.mount("/metrics", get_asgi_metrics_app())

@app.on_event("startup")
async def _up():
    mark_service_up("StrategyEngine", True)

@app.on_event("shutdown")
async def _down():
    mark_service_up("StrategyEngine", False)
```

---

# 3) 典型埋点示例

## 3.1 三段延迟（Histogram）

```python
# 策略侧：Signal -> Order
from services.metrics.exporter import latency_signal_to_order_ms, MsTimer

async def route_signal_to_order(signal):
    with MsTimer(latency_signal_to_order_ms):
        # 你的路由逻辑
        await submit_order(signal)
```

```python
# 执行侧：Order -> Fill（在 IB/经纪商回调里测提交到成交的耗时）
from services.metrics.exporter import latency_order_to_fill_ms
import time

_submit_ts_ns_by_oid = {}  # 简化示例：提交时记录

async def submit_order(order_id, ...):
    _submit_ts_ns_by_oid[order_id] = time.perf_counter_ns()
    # ... 提交逻辑 ...

async def on_filled(order_id, ...):
    start_ns = _submit_ts_ns_by_oid.pop(order_id, None)
    if start_ns:
        dur_ms = (time.perf_counter_ns() - start_ns) / 1_000_000
        latency_order_to_fill_ms.observe(dur_ms)
```

## 3.2 ClickHouse 批量写（Histogram + label: table）

```python
from services.metrics.exporter import observe_ch_batch

# 写入 positions_snapshots
await observe_ch_batch("positions_snapshots", ch_writer.write_batch("positions_snapshots", batch_rows))
```

## 3.3 Kafka Lag（Gauge）

```python
from services.metrics.exporter import observe_kafka_lag

# 假设你从 consumer metrics 拿到 lag
observe_kafka_lag(group="strategy-consumer", topic="events.order", partition=0, lag_value=42)
```

## 3.4 订单拒绝（Counter）

```python
from services.metrics.exporter import inc_rejected

# 拒单处
inc_rejected(code=str(ib_err_code), venue="IBKR", strategy=strategy_id)
```

## 3.5 异常计数（Counter）

```python
from services.metrics.exporter import inc_error

try:
    # 你的逻辑
    ...
except Exception as e:
    inc_error("PortfolioService", type(e).__name__)
    raise
```

---

# 4) Prometheus 抓取配置（`prometheus.yml` 片段）

```yaml
scrape_configs:
  - job_name: 'trading-services'
    scrape_interval: 10s
    static_configs:
      - targets: ['strategy:9108', 'executor:9108', 'portfolio:9108']
        labels:
          env: prod
```

> 如果是 ASGI 挂载在主 Web 服务：把 `targets` 换成你的 Web 端口，并确保路径是 `/metrics`（可用 `metrics_path` 指定）。

---

# 5) 与 Grafana 查询对齐（已兼容）

* P95 查询（示例）
  `histogram_quantile(0.95, sum(rate(latency_signal_to_order_ms_bucket[$__rate_interval])) by (le))`
* 订单拒绝计数：
  `sum(rate(orders_rejected_total[$__rate_interval])) by (code)`
* Kafka lag：
  `kafka_topic_lag{topic="events.order"}`

---

## 小贴士（避免踩坑）

* **直方图单位**：我们使用 **毫秒**，与仪表盘一致；注意你的观察值也要按毫秒传入。
* **label 控制**：对 Histogram 尽量**不加高基数 label**（或固定少量如 `table`）。
* **线程/协程**：`start_http_server` 会启一个线程；ASGI 模式则与现有服务共端口。选其一即可。
* **关停时**：可以把 `service_up{service=...}` 设为 0，便于告警。
