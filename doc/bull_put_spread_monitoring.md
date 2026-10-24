# Bull Put Spread 监控 & ClickHouse 表

策略 `strategies.options.bull_put_spread_strategy.BullPutSpreadStrategy` 在运行时会将所有关键事件写入：

- 本地审计文件：`runs/ai_trader/<date>/bull_put_spread.jsonl`
- ClickHouse 表（默认）：`trading.bull_put_spread_events`
- PostgreSQL 表：`trading.option_spread_trades`（记录开平仓订单，含 meta JSON）

## ClickHouse 表结构

在 ClickHouse 中执行：

```sql
CREATE TABLE IF NOT EXISTS trading.bull_put_spread_events (
    ts        DateTime            DEFAULT now(),
    event     String,
    symbol    String,
    payload   String
) ENGINE = MergeTree()
ORDER BY (ts, symbol);
```

- `ts`：事件时间（UTC），策略会写入毫秒精度。
- `event`：事件名称，例如 `enter_submitted`, `take_profit_triggered`, `skip_vix_halt`。
- `symbol`：当前仅支持 `QQQ`，但表结构允许未来扩展。
- `payload`：JSON 字符串，包含 credit、width、trend、守卫原因等详情，可在 Grafana/SQL 中 `JSONExtract` 使用。

如需不同的表名，可在 `config.yml -> bull_put_spread_qqq -> extras.bull_put.clickhouse_table` 修改。

## PostgreSQL 表结构

```sql
CREATE TABLE IF NOT EXISTS trading.option_spread_trades (
    id           BIGSERIAL PRIMARY KEY,
    ts           timestamptz DEFAULT now(),
    action       text,               -- OPEN / CLOSE / EXIT_TP_ORDER / EXIT_SL_ORDER
    symbol       text,
    expiry       text,
    short_strike double precision,
    long_strike  double precision,
    width        double precision,
    credit       double precision,
    qty          integer,
    transmit     boolean,
    meta         jsonb
);
```

策略使用 `services.portfolio.option_trade_repo.OptionTradeRepository` 写入，字段 `meta` 包含下单返回值（orderId、status、oca_group 等）。

### Schema v2（订单状态/成交落库）

为彻底避免“幽灵仓位”，并能在 DB 中追踪从 `SUBMITTED → FILLED/CANCELLED/REJECTED` 的全生命周期，建议执行：

- `sql/option_spread_trades_v2.sql`

这会为 `trading.option_spread_trades` 增加：

- `order_id`, `order_status`
- `filled_price`, `filled_qty`, `filled_ts`
- `reason`

执行示例（本机 Postgres）：

`psql -d <your_db> -f sql/option_spread_trades_v2.sql`

## Grafana/Dashboard 建议

1. **持仓状态**：统计 `enter_submitted` 与 `close_submitted` 之间的事件，得出活跃仓位数。
2. **TP/SL 命中率**：按事件 `take_profit_triggered`/`stop_loss_triggered` 分组计数。
3. **风险守卫**：统计 `skip_*` 类型事件（VIX、保证金、资金不足、行情陈旧等），作为健康度指标。
4. **最近错误**：筛选 `event LIKE 'skip_%'` 并按时间排序，方便快速定位为何跳过交易。
5. **Entry Fill Ratio**：`entry_filled / enter_submitted`，用于触发“策略失效”报警（面板 `Entry Fill Ratio (24h)`）。  
6. **State 分布**：`state` 字段（OPEN/CLOSING/CLOSED/NONE）可用于识别 ghost position（面板 `State Distribution (24h)`）。  

可以将 `payload` 解析为结构化字段，例如：

```sql
SELECT
    ts,
    event,
    symbol,
    JSONExtractFloat(payload, 'credit') AS credit,
    JSONExtractString(payload, 'trend') AS trend
FROM trading.bull_put_spread_events
WHERE ts >= today()
ORDER BY ts DESC;
```

### 监控查询示例

1. **Entry Filled Ratio**

```sql
WITH events AS (
    SELECT
        ts,
        event,
        JSONExtractString(payload, 'state')    AS state,
        JSONExtractString(payload, 'order_status') AS order_status
    FROM trading.bull_put_spread_events
    WHERE event IN ('enter_submitted', 'entry_filled')
      AND ts >= today()
)
SELECT
    countIf(event = 'entry_filled') / NULLIF(countIf(event = 'enter_submitted'), 0) AS filled_ratio,
    countIf(event = 'enter_submitted') AS submitted,
    countIf(event = 'entry_filled')    AS filled
FROM events;
```

2. **Position State Mismatch（检测幽灵仓位）**

```sql
SELECT
    JSONExtractString(payload, 'state') AS state,
    count() AS events
FROM trading.bull_put_spread_events
WHERE event IN ('entry_filled','exit_tp_filled','exit_sl_filled','close_final')
  AND ts >= today()
GROUP BY state
ORDER BY events DESC;
```

可在 Grafana 里直接引用上述 SQL，并将 `filled_ratio`、`state` 等指标做成面板 + 告警。

### Grafana Dashboard

`dashboards/bull_put_spread_dashboard.json` 包含以下面板：

1. Entry Fill Ratio (24h) — stat：`entry_filled / enter_submitted`
2. State Distribution (24h) — table：`state` 计数
3. Recent Bull Put Events — table：最近 24 小时的事件（含 state/order_status/reason）
4. Pending Entries — stat：状态为 PENDING 的 enter_submitted 计数
5. Recent Skip Events — table

导入方式：Grafana → Dashboards → Manage → Import → 上传 JSON；数据源需配置指向 ClickHouse。

### 快速 CLI 健康检查

`scripts/check_bull_put_health.py` 会读取 `runs/ai_trader/<date>/bull_put_spread.jsonl` 并输出：

- `enter_submitted` / `entry_filled` 计数及占比
- `state` 分布（OPEN/CLOSING/CLOSED/NONE）

默认阈值为 0.5，比例低于阈值则退出码为 1，可挂在 `cron` 或 `make recon-all`（已对接）。

示例：

```bash
make check-bull-put-health
```
