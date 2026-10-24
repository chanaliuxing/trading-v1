-- trading.bars_1d_current source

CREATE VIEW trading.bars_1d_current
(

    `symbol` String,

    `bar_sec` UInt32,

    `ts_start` DateTime,

    `ts_end` DateTime,

    `open` Float64,

    `high` Float64,

    `low` Float64,

    `close` Float64,

    `vwap` Nullable(Float64),

    `volume` Float64,

    `trades` UInt64,

    `bid_last` Nullable(Float64),

    `ask_last` Nullable(Float64),

    `mid_last` Nullable(Float64),

    `what_to_show` LowCardinality(String),

    `is_rth` UInt8,

    `source` LowCardinality(String),

    `exchange` LowCardinality(String),

    `currency` LowCardinality(String),

    `tz` LowCardinality(String),

    `d` Date,

    `minute_of_day` UInt16,

    `ver` UInt64,

    `is_backfill` UInt8,

    `created_at` DateTime,

    `ingest_ts` DateTime64(3)
)
AS SELECT
    symbol,

    bar_sec,

    ts_start,

    ts_end,

    open,

    high,

    low,

    close,

    vwap,

    volume,

    trades,

    bid_last,

    ask_last,

    mid_last,

    what_to_show,

    is_rth,

    source,

    exchange,

    currency,

    tz,

    d,

    minute_of_day,

    ver,

    is_backfill,

    created_at,

    ingest_ts
FROM trading.bars_1d_ibkr
ORDER BY
    symbol ASC,

    ts_start ASC,

    ver DESC
LIMIT 1 BY
    symbol,

    ts_start;


-- trading.bars_1h_current source

CREATE VIEW trading.bars_1h_current
(

    `symbol` String,

    `bar_sec` UInt16,

    `ts_start` DateTime,

    `ts_end` DateTime,

    `open` Float64,

    `high` Float64,

    `low` Float64,

    `close` Float64,

    `vwap` Nullable(Float64),

    `volume` Float64,

    `trades` UInt64,

    `bid_last` Nullable(Float64),

    `ask_last` Nullable(Float64),

    `mid_last` Nullable(Float64),

    `ver` UInt64,

    `is_backfill` UInt8,

    `created_at` DateTime,

    `what_to_show` LowCardinality(String),

    `is_rth` UInt8,

    `source` LowCardinality(String),

    `exchange` LowCardinality(String),

    `currency` LowCardinality(String),

    `tz` LowCardinality(String),

    `d` Date,

    `minute_of_day` UInt16,

    `ingest_ts` DateTime64(3)
)
AS SELECT *
FROM trading.bars_1h
FINAL;


-- trading.bars_1m_agg source

CREATE VIEW trading.bars_1m_agg
(

    `symbol` String,

    `bar_sec` UInt16,

    `ts_start` DateTime,

    `ts_end` DateTime,

    `open` Float64,

    `high` Float64,

    `low` Float64,

    `close` Float64,

    `vwap` Nullable(Float64),

    `volume` Float64,

    `trades` UInt64,

    `bid_last` Nullable(Float64),

    `ask_last` Nullable(Float64),

    `mid_last` Nullable(Float64),

    `what_to_show` LowCardinality(String),

    `is_rth` UInt8,

    `source` LowCardinality(String),

    `exchange` LowCardinality(String),

    `currency` LowCardinality(String),

    `tz` LowCardinality(String),

    `d` Date,

    `minute_of_day` UInt16,

    `ver` UInt64,

    `is_backfill` UInt8,

    `created_at` DateTime,

    `ingest_ts` DateTime64(3)
)
AS SELECT *
FROM trading.bars_1m;


-- trading.bars_1m_current source

CREATE VIEW trading.bars_1m_current
(

    `symbol` String,

    `bar_sec` UInt16,

    `ts_start` DateTime,

    `ts_end` DateTime,

    `open` Float64,

    `high` Float64,

    `low` Float64,

    `close` Float64,

    `vwap` Nullable(Float64),

    `volume` Float64,

    `trades` UInt64,

    `bid_last` Nullable(Float64),

    `ask_last` Nullable(Float64),

    `mid_last` Nullable(Float64),

    `what_to_show` LowCardinality(String),

    `is_rth` UInt8,

    `source` LowCardinality(String),

    `exchange` LowCardinality(String),

    `currency` LowCardinality(String),

    `tz` LowCardinality(String),

    `d` Date,

    `minute_of_day` UInt16,

    `ver` UInt64,

    `is_backfill` UInt8,

    `created_at` DateTime,

    `ingest_ts` DateTime64(3)
)
AS WITH unioned AS
    (
        SELECT
            symbol,

            bar_sec,

            ts_start,

            ts_end,

            open,

            high,

            low,

            close,

            vwap,

            volume,

            trades,

            bid_last,

            ask_last,

            mid_last,

            what_to_show,

            is_rth,

            source,

            exchange,

            currency,

            tz,

            d,

            minute_of_day,

            ver,

            is_backfill,

            created_at,

            ingest_ts,

            2 AS src_priority
        FROM trading.bars_1m_ibkr
        UNION ALL
        SELECT
            symbol,

            bar_sec,

            ts_start,

            ts_end,

            open,

            high,

            low,

            close,

            vwap,

            volume,

            trades,

            bid_last,

            ask_last,

            mid_last,

            what_to_show,

            is_rth,

            source,

            exchange,

            currency,

            tz,

            d,

            minute_of_day,

            ver,

            is_backfill,

            created_at,

            ingest_ts,

            1 AS src_priority
        FROM trading.bars_1m_agg
    )
SELECT
    symbol,

    bar_sec,

    ts_start,

    ts_end,

    open,

    high,

    low,

    close,

    vwap,

    volume,

    trades,

    bid_last,

    ask_last,

    mid_last,

    what_to_show,

    is_rth,

    source,

    exchange,

    currency,

    tz,

    d,

    minute_of_day,

    ver,

    is_backfill,

    created_at,

    ingest_ts
FROM unioned
ORDER BY
    symbol ASC,

    ts_start ASC,

    src_priority DESC,

    ver DESC
LIMIT 1 BY
    symbol,

    ts_start;


-- trading.bars_2h_current source

CREATE VIEW trading.bars_2h_current
(

    `symbol` String,

    `bar_sec` UInt32,

    `ts_start` DateTime,

    `ts_end` DateTime,

    `open` Float64,

    `high` Float64,

    `low` Float64,

    `close` Float64,

    `vwap` Nullable(Float64),

    `volume` Float64,

    `trades` UInt64,

    `bid_last` Nullable(Float64),

    `ask_last` Nullable(Float64),

    `mid_last` Nullable(Float64),

    `what_to_show` LowCardinality(String),

    `is_rth` UInt8,

    `source` LowCardinality(String),

    `exchange` LowCardinality(String),

    `currency` LowCardinality(String),

    `tz` LowCardinality(String),

    `d` Date,

    `minute_of_day` UInt16,

    `ver` UInt64,

    `is_backfill` UInt8,

    `created_at` DateTime,

    `ingest_ts` DateTime64(3)
)
AS SELECT
    symbol,

    bar_sec,

    ts_start,

    ts_end,

    open,

    high,

    low,

    close,

    vwap,

    volume,

    trades,

    bid_last,

    ask_last,

    mid_last,

    what_to_show,

    is_rth,

    source,

    exchange,

    currency,

    tz,

    d,

    minute_of_day,

    ver,

    is_backfill,

    created_at,

    ingest_ts
FROM trading.bars_2h_ibkr
ORDER BY
    symbol ASC,

    ts_start ASC,

    ver DESC
LIMIT 1 BY
    symbol,

    ts_start;


-- trading.bars_3h_current source

CREATE VIEW trading.bars_3h_current
(

    `symbol` String,

    `bar_sec` UInt32,

    `ts_start` DateTime,

    `ts_end` DateTime,

    `open` Float64,

    `high` Float64,

    `low` Float64,

    `close` Float64,

    `vwap` Nullable(Float64),

    `volume` Float64,

    `trades` UInt64,

    `bid_last` Nullable(Float64),

    `ask_last` Nullable(Float64),

    `mid_last` Nullable(Float64),

    `what_to_show` LowCardinality(String),

    `is_rth` UInt8,

    `source` LowCardinality(String),

    `exchange` LowCardinality(String),

    `currency` LowCardinality(String),

    `tz` LowCardinality(String),

    `d` Date,

    `minute_of_day` UInt16,

    `ver` UInt64,

    `is_backfill` UInt8,

    `created_at` DateTime,

    `ingest_ts` DateTime64(3)
)
AS SELECT
    symbol,

    bar_sec,

    ts_start,

    ts_end,

    open,

    high,

    low,

    close,

    vwap,

    volume,

    trades,

    bid_last,

    ask_last,

    mid_last,

    what_to_show,

    is_rth,

    source,

    exchange,

    currency,

    tz,

    d,

    minute_of_day,

    ver,

    is_backfill,

    created_at,

    ingest_ts
FROM trading.bars_3h_ibkr
ORDER BY
    symbol ASC,

    ts_start ASC,

    ver DESC
LIMIT 1 BY
    symbol,

    ts_start;


-- trading.bars_4h_current source

CREATE VIEW trading.bars_4h_current
(

    `symbol` String,

    `bar_sec` UInt32,

    `ts_start` DateTime,

    `ts_end` DateTime,

    `open` Float64,

    `high` Float64,

    `low` Float64,

    `close` Float64,

    `vwap` Nullable(Float64),

    `volume` Float64,

    `trades` UInt64,

    `bid_last` Nullable(Float64),

    `ask_last` Nullable(Float64),

    `mid_last` Nullable(Float64),

    `what_to_show` LowCardinality(String),

    `is_rth` UInt8,

    `source` LowCardinality(String),

    `exchange` LowCardinality(String),

    `currency` LowCardinality(String),

    `tz` LowCardinality(String),

    `d` Date,

    `minute_of_day` UInt16,

    `ver` UInt64,

    `is_backfill` UInt8,

    `created_at` DateTime,

    `ingest_ts` DateTime64(3)
)
AS SELECT
    symbol,

    bar_sec,

    ts_start,

    ts_end,

    open,

    high,

    low,

    close,

    vwap,

    volume,

    trades,

    bid_last,

    ask_last,

    mid_last,

    what_to_show,

    is_rth,

    source,

    exchange,

    currency,

    tz,

    d,

    minute_of_day,

    ver,

    is_backfill,

    created_at,

    ingest_ts
FROM trading.bars_4h_ibkr
ORDER BY
    symbol ASC,

    ts_start ASC,

    ver DESC
LIMIT 1 BY
    symbol,

    ts_start;


-- trading.bars_5m_agg source

CREATE VIEW trading.bars_5m_agg
(

    `symbol` String,

    `bar_sec` UInt16,

    `ts_start` DateTime,

    `ts_end` DateTime,

    `open` Float64,

    `high` Float64,

    `low` Float64,

    `close` Float64,

    `vwap` Nullable(Float64),

    `volume` Float64,

    `trades` UInt64,

    `bid_last` Nullable(Float64),

    `ask_last` Nullable(Float64),

    `mid_last` Nullable(Float64),

    `ver` UInt64,

    `is_backfill` UInt8,

    `created_at` DateTime,

    `what_to_show` LowCardinality(String),

    `is_rth` UInt8,

    `source` LowCardinality(String),

    `exchange` LowCardinality(String),

    `currency` LowCardinality(String),

    `tz` LowCardinality(String),

    `d` Date,

    `minute_of_day` UInt16,

    `ingest_ts` DateTime64(3)
)
AS SELECT *
FROM trading.bars_5m;


-- trading.bars_5m_current source

CREATE VIEW trading.bars_5m_current
(

    `symbol` String,

    `bar_sec` UInt16,

    `ts_start` DateTime,

    `ts_end` DateTime,

    `open` Float64,

    `high` Float64,

    `low` Float64,

    `close` Float64,

    `vwap` Nullable(Float64),

    `volume` Float64,

    `trades` UInt64,

    `bid_last` Nullable(Float64),

    `ask_last` Nullable(Float64),

    `mid_last` Nullable(Float64),

    `ver` UInt64,

    `is_backfill` UInt8,

    `created_at` DateTime,

    `what_to_show` LowCardinality(String),

    `is_rth` UInt8,

    `source` LowCardinality(String),

    `exchange` LowCardinality(String),

    `currency` LowCardinality(String),

    `tz` LowCardinality(String),

    `d` Date,

    `minute_of_day` UInt16,

    `ingest_ts` DateTime64(3)
)
AS SELECT
    symbol,

    bar_sec,

    ts_start,

    ts_end,

    open,

    high,

    low,

    close,

    vwap,

    volume,

    trades,

    bid_last,

    ask_last,

    mid_last,

    ver,

    is_backfill,

    created_at,

    what_to_show,

    is_rth,

    source,

    exchange,

    currency,

    tz,

    d,

    minute_of_day,

    ingest_ts
FROM
(
    SELECT
        *,

        2 AS src_priority
    FROM trading.bars_5m_ibkr
    UNION ALL
    SELECT
        *,

        1 AS src_priority
    FROM trading.bars_5m_agg
)
ORDER BY
    symbol ASC,

    ts_start ASC,

    src_priority DESC,

    ver DESC
LIMIT 1 BY
    symbol,

    ts_start;


-- trading.bars_5s_current source

CREATE VIEW trading.bars_5s_current
(

    `symbol` String,

    `bar_sec` UInt16,

    `ts_start` DateTime,

    `ts_end` DateTime,

    `open` Float64,

    `high` Float64,

    `low` Float64,

    `close` Float64,

    `vwap` Nullable(Float64),

    `volume` Float64,

    `trades` UInt64,

    `bid_last` Nullable(Float64),

    `ask_last` Nullable(Float64),

    `mid_last` Nullable(Float64),

    `what_to_show` LowCardinality(String),

    `is_rth` UInt8,

    `source` LowCardinality(String),

    `exchange` LowCardinality(String),

    `currency` LowCardinality(String),

    `tz` LowCardinality(String),

    `d` Date,

    `minute_of_day` UInt16,

    `ver` UInt64,

    `is_backfill` UInt8,

    `created_at` DateTime,

    `ingest_ts` DateTime64(3)
)
AS SELECT
    symbol,

    bar_sec,

    ts_start,

    ts_end,

    open,

    high,

    low,

    close,

    vwap,

    volume,

    trades,

    bid_last,

    ask_last,

    mid_last,

    what_to_show,

    is_rth,

    source,

    exchange,

    currency,

    tz,

    d,

    minute_of_day,

    ver,

    is_backfill,

    created_at,

    ingest_ts
FROM trading.bars_5s
ORDER BY
    symbol ASC,

    ts_start ASC,

    ver DESC
LIMIT 1 BY
    symbol,

    ts_start;


-- trading.bars_10m_current source

CREATE VIEW trading.bars_10m_current
(

    `symbol` String,

    `bar_sec` UInt16,

    `ts_start` DateTime,

    `ts_end` DateTime,

    `open` Float64,

    `high` Float64,

    `low` Float64,

    `close` Float64,

    `vwap` Nullable(Float64),

    `volume` Float64,

    `trades` UInt64,

    `bid_last` Nullable(Float64),

    `ask_last` Nullable(Float64),

    `mid_last` Nullable(Float64),

    `what_to_show` LowCardinality(String),

    `is_rth` UInt8,

    `source` LowCardinality(String),

    `exchange` LowCardinality(String),

    `currency` LowCardinality(String),

    `tz` LowCardinality(String),

    `d` Date,

    `minute_of_day` UInt16,

    `ver` UInt64,

    `is_backfill` UInt8,

    `created_at` DateTime,

    `ingest_ts` DateTime64(3)
)
AS SELECT
    symbol,

    bar_sec,

    ts_start,

    ts_end,

    open,

    high,

    low,

    close,

    vwap,

    volume,

    trades,

    bid_last,

    ask_last,

    mid_last,

    what_to_show,

    is_rth,

    source,

    exchange,

    currency,

    tz,

    d,

    minute_of_day,

    ver,

    is_backfill,

    created_at,

    ingest_ts
FROM trading.bars_10m_ibkr
ORDER BY
    symbol ASC,

    ts_start ASC,

    ver DESC
LIMIT 1 BY
    symbol,

    ts_start;


-- trading.bars_15m_agg source

CREATE VIEW trading.bars_15m_agg
(

    `symbol` String,

    `bar_sec` UInt16,

    `ts_start` DateTime,

    `ts_end` DateTime,

    `open` Float64,

    `high` Float64,

    `low` Float64,

    `close` Float64,

    `vwap` Nullable(Float64),

    `volume` Float64,

    `trades` UInt64,

    `bid_last` Nullable(Float64),

    `ask_last` Nullable(Float64),

    `mid_last` Nullable(Float64),

    `ver` UInt64,

    `is_backfill` UInt8,

    `created_at` DateTime,

    `what_to_show` LowCardinality(String),

    `is_rth` UInt8,

    `source` LowCardinality(String),

    `exchange` LowCardinality(String),

    `currency` LowCardinality(String),

    `tz` LowCardinality(String),

    `d` Date,

    `minute_of_day` UInt16,

    `ingest_ts` DateTime64(3)
)
AS SELECT *
FROM trading.bars_15m;


-- trading.bars_15m_current source

CREATE VIEW trading.bars_15m_current
(

    `symbol` String,

    `bar_sec` UInt16,

    `ts_start` DateTime,

    `ts_end` DateTime,

    `open` Float64,

    `high` Float64,

    `low` Float64,

    `close` Float64,

    `vwap` Nullable(Float64),

    `volume` Float64,

    `trades` UInt64,

    `bid_last` Nullable(Float64),

    `ask_last` Nullable(Float64),

    `mid_last` Nullable(Float64),

    `ver` UInt64,

    `is_backfill` UInt8,

    `created_at` DateTime,

    `what_to_show` LowCardinality(String),

    `is_rth` UInt8,

    `source` LowCardinality(String),

    `exchange` LowCardinality(String),

    `currency` LowCardinality(String),

    `tz` LowCardinality(String),

    `d` Date,

    `minute_of_day` UInt16,

    `ingest_ts` DateTime64(3)
)
AS SELECT
    symbol,

    bar_sec,

    ts_start,

    ts_end,

    open,

    high,

    low,

    close,

    vwap,

    volume,

    trades,

    bid_last,

    ask_last,

    mid_last,

    ver,

    is_backfill,

    created_at,

    what_to_show,

    is_rth,

    source,

    exchange,

    currency,

    tz,

    d,

    minute_of_day,

    ingest_ts
FROM
(
    SELECT
        *,

        2 AS src_priority
    FROM trading.bars_15m_ibkr
    UNION ALL
    SELECT
        *,

        1 AS src_priority
    FROM trading.bars_15m_agg
)
ORDER BY
    symbol ASC,

    ts_start ASC,

    src_priority DESC,

    ver DESC
LIMIT 1 BY
    symbol,

    ts_start;


-- trading.bars_30m_current source

CREATE VIEW trading.bars_30m_current
(

    `symbol` String,

    `bar_sec` UInt16,

    `ts_start` DateTime,

    `ts_end` DateTime,

    `open` Float64,

    `high` Float64,

    `low` Float64,

    `close` Float64,

    `vwap` Nullable(Float64),

    `volume` Float64,

    `trades` UInt64,

    `bid_last` Nullable(Float64),

    `ask_last` Nullable(Float64),

    `mid_last` Nullable(Float64),

    `ver` UInt64,

    `is_backfill` UInt8,

    `created_at` DateTime,

    `what_to_show` LowCardinality(String),

    `is_rth` UInt8,

    `source` LowCardinality(String),

    `exchange` LowCardinality(String),

    `currency` LowCardinality(String),

    `tz` LowCardinality(String),

    `d` Date,

    `minute_of_day` UInt16,

    `ingest_ts` DateTime64(3)
)
AS SELECT *
FROM trading.bars_30m
FINAL;


-- trading.mv_bars_1h source

CREATE MATERIALIZED VIEW trading.mv_bars_1h TO trading.bars_1h
(

    `symbol` String,

    `bar_sec` UInt16,

    `ts_start` DateTime,

    `ts_end` DateTime,

    `open` Float64,

    `high` Float64,

    `low` Float64,

    `close` Float64,

    `vwap` Nullable(Float64),

    `volume` Float64,

    `trades` UInt64,

    `bid_last` Nullable(Float64),

    `ask_last` Nullable(Float64),

    `mid_last` Nullable(Float64),

    `what_to_show` String,

    `is_rth` UInt8,

    `source` String,

    `exchange` String,

    `currency` String,

    `tz` String,

    `d` Date,

    `minute_of_day` UInt32,

    `ver` UInt64,

    `is_backfill` UInt8,

    `created_at` DateTime,

    `ingest_ts` DateTime64(3)
)
AS SELECT
    b.symbol,

    3600 AS bar_sec,

    b.win_start AS ts_start,

    b.win_start + toIntervalHour(1) AS ts_end,

    argMin(b.open,
 b.ts_start) AS open,

    max(b.high) AS high,

    min(b.low) AS low,

    argMax(b.close,
 b.ts_start) AS close,

    sum(b.vwap * b.volume) / nullIf(sum(b.volume),
 0) AS vwap,

    sum(b.volume) AS volume,

    sum(b.trades) AS trades,

    argMax(b.bid_last,
 b.ts_start) AS bid_last,

    argMax(b.ask_last,
 b.ts_start) AS ask_last,

    argMax(b.mid_last,
 b.ts_start) AS mid_last,

    any(b.what_to_show) AS what_to_show,

    any(b.is_rth) AS is_rth,

    any(b.source) AS source,

    any(b.exchange) AS exchange,

    any(b.currency) AS currency,

    any(b.tz) AS tz,

    toDate(b.win_start) AS d,

    (toHour(b.win_start) * 60) + toMinute(b.win_start) AS minute_of_day,

    max(b.ver) AS ver,

    max(b.is_backfill) AS is_backfill,

    now() AS created_at,

    now64(3) AS ingest_ts
FROM
(
    SELECT
        *,

        toStartOfInterval(ts_start,
 toIntervalHour(1)) AS win_start
    FROM trading.bars_5s
) AS b
GROUP BY
    b.symbol,

    b.win_start;


-- trading.mv_bars_15m source

CREATE MATERIALIZED VIEW trading.mv_bars_15m TO trading.bars_15m
(

    `symbol` String,

    `bar_sec` UInt16,

    `ts_start` DateTime,

    `ts_end` DateTime,

    `open` Float64,

    `high` Float64,

    `low` Float64,

    `close` Float64,

    `vwap` Nullable(Float64),

    `volume` Float64,

    `trades` UInt64,

    `bid_last` Nullable(Float64),

    `ask_last` Nullable(Float64),

    `mid_last` Nullable(Float64),

    `what_to_show` String,

    `is_rth` UInt8,

    `source` String,

    `exchange` String,

    `currency` String,

    `tz` String,

    `d` Date,

    `minute_of_day` UInt32,

    `ver` UInt64,

    `is_backfill` UInt8,

    `created_at` DateTime,

    `ingest_ts` DateTime64(3)
)
AS SELECT
    b.symbol,

    900 AS bar_sec,

    b.win_start AS ts_start,

    b.win_start + toIntervalMinute(15) AS ts_end,

    argMin(b.open,
 b.ts_start) AS open,

    max(b.high) AS high,

    min(b.low) AS low,

    argMax(b.close,
 b.ts_start) AS close,

    sum(b.vwap * b.volume) / nullIf(sum(b.volume),
 0) AS vwap,

    sum(b.volume) AS volume,

    sum(b.trades) AS trades,

    argMax(b.bid_last,
 b.ts_start) AS bid_last,

    argMax(b.ask_last,
 b.ts_start) AS ask_last,

    argMax(b.mid_last,
 b.ts_start) AS mid_last,

    any(b.what_to_show) AS what_to_show,

    any(b.is_rth) AS is_rth,

    any(b.source) AS source,

    any(b.exchange) AS exchange,

    any(b.currency) AS currency,

    any(b.tz) AS tz,

    toDate(b.win_start) AS d,

    (toHour(b.win_start) * 60) + toMinute(b.win_start) AS minute_of_day,

    max(b.ver) AS ver,

    max(b.is_backfill) AS is_backfill,

    now() AS created_at,

    now64(3) AS ingest_ts
FROM
(
    SELECT
        *,

        toStartOfInterval(ts_start,
 toIntervalMinute(15)) AS win_start
    FROM trading.bars_5s
) AS b
GROUP BY
    b.symbol,

    b.win_start;


-- trading.mv_bars_15s source

CREATE MATERIALIZED VIEW trading.mv_bars_15s TO trading.bars_15s
(

    `symbol` String,

    `bar_sec` UInt8,

    `ts_start` DateTime,

    `ts_end` DateTime,

    `open` Float64,

    `high` Float64,

    `low` Float64,

    `close` Float64,

    `vwap` Nullable(Float64),

    `volume` Float64,

    `trades` UInt64,

    `bid_last` Nullable(Float64),

    `ask_last` Nullable(Float64),

    `mid_last` Nullable(Float64),

    `what_to_show` String,

    `is_rth` UInt8,

    `source` String,

    `exchange` String,

    `currency` String,

    `tz` String,

    `d` Date,

    `minute_of_day` UInt32,

    `ver` UInt64,

    `is_backfill` UInt8,

    `created_at` DateTime,

    `ingest_ts` DateTime64(3)
)
AS SELECT
    b.symbol,

    15 AS bar_sec,

    b.win_start AS ts_start,

    b.win_start + toIntervalSecond(15) AS ts_end,

    argMin(b.open,
 b.ts_start) AS open,

    max(b.high) AS high,

    min(b.low) AS low,

    argMax(b.close,
 b.ts_start) AS close,

    sum(b.vwap * b.volume) / nullIf(sum(b.volume),
 0) AS vwap,

    sum(b.volume) AS volume,

    sum(b.trades) AS trades,

    argMax(b.bid_last,
 b.ts_start) AS bid_last,

    argMax(b.ask_last,
 b.ts_start) AS ask_last,

    argMax(b.mid_last,
 b.ts_start) AS mid_last,

    any(b.what_to_show) AS what_to_show,

    any(b.is_rth) AS is_rth,

    any(b.source) AS source,

    any(b.exchange) AS exchange,

    any(b.currency) AS currency,

    any(b.tz) AS tz,

    toDate(b.win_start) AS d,

    (toHour(b.win_start) * 60) + toMinute(b.win_start) AS minute_of_day,

    max(b.ver) AS ver,

    max(b.is_backfill) AS is_backfill,

    now() AS created_at,

    now64(3) AS ingest_ts
FROM
(
    SELECT
        *,

        toStartOfInterval(ts_start,
 toIntervalSecond(15)) AS win_start
    FROM trading.bars_5s
) AS b
GROUP BY
    b.symbol,

    b.win_start;